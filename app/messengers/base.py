from __future__ import annotations

import re
from abc import ABC, abstractmethod
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from app.domain import IncomingEvent, OutgoingMessage, Platform


def local_cms_image_path(
    image_url: str,
    *,
    public_base_url: str,
    cms_media_dir: Path | None,
) -> Path | None:
    """Resolve only bot-owned CMS URLs to an existing local image file."""
    if not public_base_url or cms_media_dir is None:
        return None
    image = urlparse(image_url)
    public = urlparse(public_base_url)
    if image.scheme != public.scheme or image.netloc != public.netloc:
        return None
    public_path = public.path.rstrip("/")
    prefix = f"{public_path}/cms-media/"
    if not image.path.startswith(prefix):
        return None
    filename = image.path.removeprefix(prefix)
    if not re.fullmatch(r"[a-f0-9]{32}\.(?:jpg|png|gif|webp)", filename):
        return None
    path = cms_media_dir / filename
    return path if path.is_file() else None


class Messenger(ABC):
    platform: Platform

    @abstractmethod
    def parse_update(self, payload: dict[str, Any]) -> IncomingEvent | None: ...

    @abstractmethod
    async def send(self, recipient_id: str, message: OutgoingMessage) -> list[str]:
        """Send a message and return image URLs that could not be delivered."""
        ...

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
