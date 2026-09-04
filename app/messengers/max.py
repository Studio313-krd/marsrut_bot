from __future__ import annotations

import base64
import hashlib
import hmac
import json
import logging
import re
from html import unescape
from typing import Any
from urllib.parse import urlparse

import httpx

from app.domain import Button, IncomingEvent, OutgoingMessage, Platform
from app.messengers.base import Messenger

_PHONE_RE = re.compile(r"^TEL(?:;[^:]*)?:(.+)$", re.MULTILINE | re.IGNORECASE)
logger = logging.getLogger(__name__)


def _image_urls(value: Any) -> list[str]:
    if isinstance(value, dict):
        direct = [item for key, item in value.items() if "url" in str(key).lower() and isinstance(item, str)]
        nested = [url for item in value.values() for url in _image_urls(item)]
        return direct + nested
    if isinstance(value, list):
        return [url for item in value for url in _image_urls(item)]
    return []


class MaxMessenger(Messenger):
    platform = Platform.MAX

    def __init__(self, token: str) -> None:
        self._token = token
        self._client = httpx.AsyncClient(
            base_url="https://platform-api2.max.ru",
            headers={"Authorization": token},
            timeout=httpx.Timeout(20.0, connect=7.0),
        )

    def _verified_contact(self, message: dict[str, Any]) -> str | None:
        body = message.get("body") or {}
        for attachment in body.get("attachments") or []:
            if attachment.get("type") != "contact":
                continue
            payload = attachment.get("payload") or {}
            vcf_info = str(payload.get("vcf_info") or "").replace("\\r\\n", "\r\n")
            supplied = str(payload.get("hash") or "")
            digest = hmac.new(self._token.encode(), vcf_info.encode(), hashlib.sha256).digest()
            valid = hmac.compare_digest(supplied.lower(), digest.hex())
            if not valid:
                try:
                    valid = hmac.compare_digest(supplied, base64.b64encode(digest).decode())
                except ValueError:
                    valid = False
            match = _PHONE_RE.search(vcf_info)
            if valid and match:
                return match.group(1).strip()
        return None

    def parse_update(self, payload: dict[str, Any]) -> IncomingEvent | None:
        update_type = payload.get("update_type")
        if update_type not in {"message_created", "message_callback", "bot_started"}:
            return None
        message = payload.get("message") or {}
        callback = payload.get("callback") or {}
        user = payload.get("user") or callback.get("user") or message.get("sender") or {}
        user_id = user.get("user_id") or user.get("id")
        if not user_id:
            return None
        # The service operates in direct dialogs and sends through the MAX user_id parameter.
        chat_id = user_id
        body = message.get("body") or {}
        canonical = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        update_id = str(
            payload.get("marker")
            or message.get("mid")
            or callback.get("callback_id")
            or hashlib.sha256(canonical.encode()).hexdigest()[:32]
        )
        return IncomingEvent(
            platform=self.platform,
            update_id=update_id,
            user_id=str(user_id),
            chat_id=str(chat_id),
            display_name=str(user.get("name") or "Пользователь"),
            username=user.get("username"),
            text=body.get("text")
            if update_type == "message_created"
            else ("/start" if update_type == "bot_started" else None),
            callback=callback.get("payload") if update_type == "message_callback" else None,
            callback_id=str(callback.get("callback_id") or "") or None,
            phone=self._verified_contact(message),
            raw=payload,
        )

    @staticmethod
    def _button(button: Button) -> dict[str, Any]:
        if button.kind == "request_contact":
            return {"type": "request_contact", "text": button.text}
        if button.url:
            return {"type": "link", "text": button.text, "url": button.url}
        return {"type": "callback", "text": button.text, "payload": button.callback or "noop"}

    async def send(self, recipient_id: str, message: OutgoingMessage) -> None:
        for image_url in message.images:
            try:
                response = await self._client.post(
                    "/messages",
                    params={"user_id": recipient_id},
                    json={"attachments": [{"type": "image", "payload": {"url": image_url}}]},
                )
                response.raise_for_status()
            except httpx.HTTPError:
                logger.warning("Could not send a configured image", exc_info=True)

        body: dict[str, Any] = {
            "text": message.text[:4000],
            "format": "html",
            "notify": True,
        }
        if message.buttons:
            body["attachments"] = [
                {
                    "type": "inline_keyboard",
                    "payload": {
                        "buttons": [[self._button(button) for button in row] for row in message.buttons]
                    },
                }
            ]
        response = await self._client.post("/messages", params={"user_id": recipient_id}, json=body)
        if response.status_code == 400 and body.get("format"):
            fallback = dict(body)
            fallback.pop("format", None)
            fallback["text"] = unescape(re.sub(r"<[^>]*>", "", message.text))[:4000]
            response = await self._client.post("/messages", params={"user_id": recipient_id}, json=fallback)
        response.raise_for_status()

    async def answer_callback(self, callback_id: str | None) -> None:
        if not callback_id:
            return
        response = await self._client.post("/answers", params={"callback_id": callback_id}, json={})
        response.raise_for_status()

    async def send_document(self, recipient_id: str, filename: str, content: bytes, caption: str) -> None:
        allocation = await self._client.post("/uploads", params={"type": "file"})
        allocation.raise_for_status()
        upload_url = allocation.json()["url"]
        async with httpx.AsyncClient(timeout=httpx.Timeout(60.0, connect=10.0)) as uploader:
            uploaded = await uploader.post(upload_url, files={"data": (filename, content, "text/csv")})
            uploaded.raise_for_status()
        result = uploaded.json()
        token = result.get("token") or (result.get("retval") or {}).get("token")
        if not token:
            raise RuntimeError("MAX upload response does not contain a file token")
        response = await self._client.post(
            "/messages",
            params={"user_id": recipient_id},
            json={
                "text": caption,
                "format": "html",
                "attachments": [{"type": "file", "payload": {"token": token}}],
            },
        )
        response.raise_for_status()

    async def download_image(self, event: IncomingEvent) -> tuple[bytes, str] | None:
        message = event.raw.get("message") if isinstance(event.raw, dict) else None
        body = message.get("body") if isinstance(message, dict) else None
        attachments = body.get("attachments") if isinstance(body, dict) else None
        if not isinstance(attachments, list):
            return None
        image_url = None
        for attachment in attachments:
            if not isinstance(attachment, dict) or attachment.get("type") != "image":
                continue
            payload = attachment.get("payload") or {}
            candidates = _image_urls(payload)
            image_url = candidates[-1] if candidates else None
            if image_url:
                break
        if not image_url:
            return None
        parsed = urlparse(image_url)
        hostname = (parsed.hostname or "").lower()
        trusted = any(
            hostname == domain or hostname.endswith(f".{domain}") for domain in ("oneme.ru", "okcdn.ru")
        )
        if parsed.scheme != "https" or not trusted:
            return None
        async with httpx.AsyncClient(timeout=httpx.Timeout(20.0, connect=7.0)) as downloader:
            response = await downloader.get(image_url)
            response.raise_for_status()
        return response.content, str(response.headers.get("content-type") or "image/jpeg")

    async def register_webhook(self, public_base_url: str, secret: str) -> None:
        response = await self._client.post(
            "/subscriptions",
            json={
                "url": f"{public_base_url}/webhooks/max",
                "update_types": ["message_created", "message_callback", "bot_started"],
                "secret": secret,
            },
        )
        response.raise_for_status()

    async def close(self) -> None:
        await self._client.aclose()
