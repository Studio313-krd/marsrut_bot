from __future__ import annotations

import logging
import re
from html import unescape
from typing import Any

import httpx

from app.domain import Button, IncomingEvent, OutgoingMessage, Platform
from app.messengers.base import Messenger

logger = logging.getLogger(__name__)

_REPLY_CALLBACK_LABELS = {
    "menu": "Главное меню",
    "flow:cancel": "Отменить",
    "apply:back": "Назад",
}


class TelegramMessenger(Messenger):
    platform = Platform.TELEGRAM

    def __init__(self, token: str) -> None:
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

    async def send(self, recipient_id: str, message: OutgoingMessage) -> None:
        for image_url in message.images:
            try:
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
            except (httpx.HTTPError, RuntimeError):
                logger.warning("Could not send a configured image", exc_info=True)

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
