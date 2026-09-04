from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any

from app.domain import IncomingEvent, OutgoingMessage, Platform


class Messenger(ABC):
    platform: Platform

    @abstractmethod
    def parse_update(self, payload: dict[str, Any]) -> IncomingEvent | None: ...

    @abstractmethod
    async def send(self, recipient_id: str, message: OutgoingMessage) -> None: ...

    async def answer_callback(self, callback_id: str | None) -> None:
        del callback_id

    async def send_document(self, recipient_id: str, filename: str, content: bytes, caption: str) -> None:
        del recipient_id, filename, content, caption
        raise NotImplementedError("File sending is not supported by this adapter")

    async def download_image(self, event: IncomingEvent) -> tuple[bytes, str] | None:
        """Return an image attached to an incoming message, if the platform exposes one."""
        del event
        return None

    async def configure_bot(self) -> None:
        """Configure platform-native commands and navigation when supported."""
        return None

    @abstractmethod
    async def register_webhook(self, public_base_url: str, secret: str) -> None: ...

    @abstractmethod
    async def close(self) -> None: ...
