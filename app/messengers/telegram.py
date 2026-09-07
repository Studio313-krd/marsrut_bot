from __future__ import annotations

import logging
import mimetypes
import re
from html import unescape
from pathlib import Path
from typing import Any

import httpx

from app.domain import Button, IncomingEvent, OutgoingMessage, Platform
from app.messengers.base import Messenger, local_cms_image_path

logger = logging.getLogger(__name__)

_REPLY_CALLBACK_LABELS = {
    "menu": "Главное меню",
    "flow:cancel": "Отменить",
    "apply:back": "Назад",
}


class TelegramMessenger(Messenger):
    platform = Platform.TELEGRAM

    def __init__(
        self,
        token: str,
        *,
        cms_media_dir: Path | None = None,
        public_base_url: str = "",
    ) -> None:
        self._token = token
        self._cms_media_dir = cms_media_dir
        self._public_base_url = public_base_url.rstrip("/")
        self._base_url = f"https://api.telegram.org/bot{token}"
        self._client = httpx.AsyncClient(timeout=httpx.Timeout(15.0, connect=5.0))

    def parse_update(self, payload: dict[str, Any]) -> IncomingEvent | None:
        update_id = str(payload.get("update_id", ""))
        callback = payload.get("callback_query")
        if isinstance(callback, dict):
            user = callback.get("from") or {}
            message = callback.get("message") or {}
            chat = message.get("chat") or {}
            if not user.get("id"):
                return None
            return IncomingEvent(
                platform=self.platform,
                update_id=update_id or str(callback.get("id", "")),
                user_id=str(user["id"]),
                chat_id=str(chat.get("id") or user["id"]),
                display_name=" ".join(filter(None, [user.get("first_name"), user.get("last_name")])).strip()
                or "Пользователь",
                username=user.get("username"),
                callback=str(callback.get("data") or ""),
                callback_id=str(callback.get("id") or ""),
                raw=payload,
            )

        message = payload.get("message")
        if not isinstance(message, dict):
            return None
        user = message.get("from") or {}
        chat = message.get("chat") or {}
        if not user.get("id") or not chat.get("id"):
            return None
        contact = message.get("contact") or {}
        phone = None
        if contact.get("phone_number") and str(contact.get("user_id", user["id"])) == str(user["id"]):
            phone = str(contact["phone_number"])
        return IncomingEvent(
            platform=self.platform,
            update_id=update_id or str(message.get("message_id", "")),
            user_id=str(user["id"]),
            chat_id=str(chat["id"]),
            display_name=" ".join(filter(None, [user.get("first_name"), user.get("last_name")])).strip()
            or "Пользователь",
            username=user.get("username"),
            text=message.get("text"),
            phone=phone,
            raw=payload,
        )

    @staticmethod
    def _inline_button(button: Button) -> dict[str, Any]:
        if button.url:
            return {"text": button.text, "url": button.url}
        return {"text": button.text, "callback_data": button.callback or "noop"}

    @staticmethod
    def _reply_button(button: Button) -> dict[str, Any] | None:
        if button.kind == "request_contact":
            return {"text": button.text, "request_contact": True}
        if button.callback in _REPLY_CALLBACK_LABELS:
            return {"text": _REPLY_CALLBACK_LABELS[button.callback]}
        return None

    async def send(self, recipient_id: str, message: OutgoingMessage) -> list[str]:
        failed_images: list[str] = []
        for image_url in message.images:
            try:
                local_path = local_cms_image_path(
                    image_url,
                    public_base_url=self._public_base_url,
                    cms_media_dir=self._cms_media_dir,
                )
                if local_path:
                    media_type = mimetypes.guess_type(local_path.name)[0] or "application/octet-stream"
                    response = await self._client.post(
                        f"{self._base_url}/sendPhoto",
                        data={"chat_id": recipient_id},
                        files={"photo": (local_path.name, local_path.read_bytes(), media_type)},
                    )
                else:
                    response = await self._client.post(
                        f"{self._base_url}/sendPhoto",
                        json={"chat_id": recipient_id, "photo": image_url},
                    )
                response.raise_for_status()
                result = response.json()
                if not result.get("ok"):
                    raise RuntimeError(
                        f"Telegram sendPhoto failed: {result.get('description', 'unknown error')}"
                    )
            except (httpx.HTTPError, OSError, RuntimeError) as exc:
                logger.warning("Could not send a configured image: %s", type(exc).__name__)
                failed_images.append(image_url)

        body: dict[str, Any] = {
            "chat_id": recipient_id,
            "text": message.text[:4096],
            "parse_mode": "HTML",
            "disable_web_page_preview": message.disable_preview,
        }
        has_contact_button = any(
            button.kind == "request_contact" for row in message.buttons for button in row
        )
        if has_contact_button:
            keyboard = []
            for row in message.buttons:
                rendered_row = [self._reply_button(button) for button in row]
                rendered_row = [button for button in rendered_row if button]
                if rendered_row:
                    keyboard.append(rendered_row)
            body["reply_markup"] = {
                "keyboard": keyboard,
                "resize_keyboard": True,
                "one_time_keyboard": True,
                "input_field_placeholder": "Или введите номер вручную",
            }
        elif message.buttons:
            body["reply_markup"] = {
                "inline_keyboard": [
                    [self._inline_button(button) for button in row] for row in message.buttons
                ]
            }
        elif message.remove_keyboard:
            body["reply_markup"] = {"remove_keyboard": True}
        response = await self._client.post(f"{self._base_url}/sendMessage", json=body)
        if response.status_code == 400 and body.get("parse_mode"):
            fallback = dict(body)
            fallback.pop("parse_mode", None)
            fallback["text"] = unescape(re.sub(r"<[^>]*>", "", message.text))[:4096]
            response = await self._client.post(f"{self._base_url}/sendMessage", json=fallback)
        response.raise_for_status()
        result = response.json()
        if not result.get("ok"):
            raise RuntimeError(f"Telegram sendMessage failed: {result.get('description', 'unknown error')}")
        return failed_images

    async def answer_callback(self, callback_id: str | None) -> None:
        if not callback_id:
            return
        response = await self._client.post(
            f"{self._base_url}/answerCallbackQuery", json={"callback_query_id": callback_id}
        )
        response.raise_for_status()

    async def send_document(self, recipient_id: str, filename: str, content: bytes, caption: str) -> None:
        response = await self._client.post(
            f"{self._base_url}/sendDocument",
            data={"chat_id": recipient_id, "caption": caption, "parse_mode": "HTML"},
            files={"document": (filename, content, "text/csv")},
        )
        response.raise_for_status()
        if not response.json().get("ok"):
            raise RuntimeError("Telegram rejected document")

    async def download_image(self, event: IncomingEvent) -> tuple[bytes, str] | None:
        message = event.raw.get("message") if isinstance(event.raw, dict) else None
        if not isinstance(message, dict):
            return None
        photos = message.get("photo")
        file_id = None
        if isinstance(photos, list) and photos:
            largest = max(
                (item for item in photos if isinstance(item, dict) and item.get("file_id")),
                key=lambda item: (
                    int(item.get("file_size") or 0),
                    int(item.get("width") or 0) * int(item.get("height") or 0),
                ),
                default=None,
            )
            if largest:
                file_id = largest["file_id"]
        document = message.get("document")
        if (
            not file_id
            and isinstance(document, dict)
            and str(document.get("mime_type") or "").startswith("image/")
        ):
            file_id = document.get("file_id")
        if not file_id:
            return None

        metadata = await self._client.get(f"{self._base_url}/getFile", params={"file_id": file_id})
        metadata.raise_for_status()
        result = metadata.json()
        file_path = (result.get("result") or {}).get("file_path") if result.get("ok") else None
        if not file_path:
            return None
        downloaded = await self._client.get(f"https://api.telegram.org/file/bot{self._token}/{file_path}")
        downloaded.raise_for_status()
        return downloaded.content, str(downloaded.headers.get("content-type") or "image/jpeg")

    async def configure_bot(self) -> None:
        requests = (
            (
                "setMyCommands",
                {"commands": [{"command": "start", "description": "Перейти к главной"}]},
            ),
            ("setChatMenuButton", {"menu_button": {"type": "commands"}}),
        )
        for method, payload in requests:
            response = await self._client.post(f"{self._base_url}/{method}", json=payload)
            if not response.is_success:
                raise RuntimeError(f"Telegram rejected {method} with HTTP {response.status_code}")
            result = response.json()
            if not result.get("ok"):
                raise RuntimeError(
                    f"Telegram rejected {method}: {result.get('description', 'unknown error')}"
                )

    async def register_webhook(self, public_base_url: str, secret: str) -> None:
        response = await self._client.post(
            f"{self._base_url}/setWebhook",
            json={
                "url": f"{public_base_url}/webhooks/telegram",
                "secret_token": secret,
                "allowed_updates": ["message", "callback_query"],
                "drop_pending_updates": False,
            },
        )
        response.raise_for_status()
        if not response.json().get("ok"):
            raise RuntimeError("Telegram rejected webhook registration")

    async def close(self) -> None:
        await self._client.aclose()
