from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any


class Platform(StrEnum):
    TELEGRAM = "TELEGRAM"
    MAX = "MAX"


class AdminRole(StrEnum):
    ADMIN = "ADMIN"


@dataclass(frozen=True, slots=True)
class Button:
    text: str
    callback: str | None = None
    url: str | None = None
    kind: str = "callback"


@dataclass(slots=True)
class OutgoingMessage:
    text: str
    buttons: list[list[Button]] = field(default_factory=list)
    disable_preview: bool = True
    remove_keyboard: bool = False
    images: list[str] = field(default_factory=list)
    content_key: str | None = None
    content_title: str | None = None
    content_category: str = "other"


@dataclass(frozen=True, slots=True)
class IncomingEvent:
    platform: Platform
    update_id: str
    user_id: str
    chat_id: str
    display_name: str
    username: str | None = None
    text: str | None = None
    callback: str | None = None
    callback_id: str | None = None
    phone: str | None = None
    raw: dict[str, Any] = field(default_factory=dict)
