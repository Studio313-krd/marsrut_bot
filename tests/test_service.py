from __future__ import annotations

from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import pytest

from app.config import Settings
from app.domain import IncomingEvent, OutgoingMessage, Platform
from app.messengers.base import Messenger
from app.service import BotService
from app.storage import Storage


class FakeMessenger(Messenger):
    platform = Platform.TELEGRAM

    def __init__(self) -> None:
        self.messages: list[OutgoingMessage] = []

    def parse_update(self, payload: dict[str, Any]) -> IncomingEvent | None:
        del payload
        return None

    async def send(self, recipient_id: str, message: OutgoingMessage) -> None:
        del recipient_id
        self.messages.append(message)

    async def register_webhook(self, public_base_url: str, secret: str) -> None:
        del public_base_url, secret

    async def close(self) -> None:
        return None


class FakeSite:
    def __init__(self) -> None:
        self.created: dict[str, Any] | None = None

    async def requests(self, **filters: Any) -> dict[str, Any]:
        del filters
        return {"items": [], "pagination": {"total": 0, "hasMore": False}}

    async def create_request(self, body: dict[str, Any]) -> dict[str, Any]:
        self.created = body
        return {"created": True, "request": {**body, "id": "request-1", "requestNumber": "MP-TEST"}}

    async def add_activity(self, request_id: str, body: dict[str, Any]) -> dict[str, Any]:
        del request_id, body
        return {}


def settings(path: Path) -> Settings:
    return Settings(
        app_env="test",
        host="127.0.0.1",
        port=8080,
        app_secret="a" * 32,
        public_base_url="https://bot.example.test",
        site_base_url="https://site.example.test",
        site_public_url="https://site.example.test",
        privacy_url="https://site.example.test/privacy-policy",
        database_path=path,
        log_level="INFO",
        timezone=ZoneInfo("Europe/Moscow"),
        bot_api_key_id="test",
        bot_api_secret="b" * 32,
        telegram_token="token",
        telegram_webhook_secret="c" * 16,
        telegram_username="test_bot",
        telegram_owner_ids=(),
        max_token="",
        max_webhook_secret="",
        max_username="",
        max_owner_ids=(),
        event_poll_interval=5,
        reminder_poll_interval=60,
        daily_digest_hour=9,
        outbox_retention_days=30,
    )


def incoming(update_id: int, text: str | None = None, callback: str | None = None) -> IncomingEvent:
    return IncomingEvent(
        platform=Platform.TELEGRAM,
        update_id=str(update_id),
        user_id="100",
        chat_id="100",
        display_name="Иван Иванов",
        text=text,
        callback=callback,
    )


@pytest.mark.asyncio
async def test_application_flow_creates_site_request(tmp_path) -> None:
    storage = Storage(tmp_path / "bot.sqlite3")
    storage.initialize()
    messenger = FakeMessenger()
    site = FakeSite()
    service = BotService(settings(tmp_path / "bot.sqlite3"), storage, site, {Platform.TELEGRAM: messenger})  # type: ignore[arg-type]

    await service.handle(incoming(1, callback="apply:start"))
    await service.handle(incoming(2, text="Иван Иванов"))
    await service.handle(incoming(3, text="Проект"))
    await service.handle(incoming(4, text="Основатель"))
    await service.handle(incoming(5, text="+7 999 123-45-67"))
    await service.handle(incoming(6, callback="apply:skip:email"))
    await service.handle(incoming(7, text="Создаём полезный городской сервис"))
    await service.handle(incoming(8, callback="apply:submit"))

    assert site.created
    assert site.created["source"] == "TELEGRAM"
    assert site.created["name"] == "Иван Иванов"
    assert storage.conversation(Platform.TELEGRAM, "100") is None
    assert "MP-TEST" in messenger.messages[-1].text


@pytest.mark.asyncio
async def test_menu_has_no_removed_features_and_custom_page_opens(tmp_path) -> None:
    storage = Storage(tmp_path / "bot.sqlite3")
    storage.initialize()
    messenger = FakeMessenger()
    service = BotService(
        settings(tmp_path / "bot.sqlite3"), storage, FakeSite(), {Platform.TELEGRAM: messenger}
    )  # type: ignore[arg-type]

    await service.handle(incoming(1, text="/menu"))
    callbacks = {
        button.callback for row in messenger.messages[-1].buttons for button in row if button.callback
    }
    assert not any(callback.startswith(("save:", "saved:", "subscription")) for callback in callbacks)

    entry = next(item for item in storage.content_entries("main") if item["content_key"] == "main.menu")
    _button_id, page_id = storage.create_custom_button(entry["id"], "Новая кнопка", "Новый ответ")

    await service.handle(incoming(2, text="/menu"))
    assert messenger.messages[-1].buttons[-1][0].text == "Новая кнопка"
    await service.handle(incoming(3, callback=f"page:show:{page_id}"))
    assert messenger.messages[-1].text == "Новый ответ"


@pytest.mark.asyncio
async def test_disabled_feature_rejects_callback_from_old_message(tmp_path) -> None:
    storage = Storage(tmp_path / "bot.sqlite3")
    storage.initialize()
    messenger = FakeMessenger()
    service = BotService(
        settings(tmp_path / "bot.sqlite3"), storage, FakeSite(), {Platform.TELEGRAM: messenger}
    )  # type: ignore[arg-type]
    storage.toggle_feature("application")

    await service.handle(incoming(1, callback="apply:start"))

    assert messenger.messages[-1].content_key == "system.feature_disabled"
    assert storage.conversation(Platform.TELEGRAM, "100") is None


@pytest.mark.asyncio
async def test_admin_edits_main_message_from_bot_panel(tmp_path) -> None:
    storage = Storage(tmp_path / "bot.sqlite3")
    storage.initialize()
    storage.ensure_owner(Platform.TELEGRAM, "100", "Владелец")
    messenger = FakeMessenger()
    service = BotService(
        settings(tmp_path / "bot.sqlite3"), storage, FakeSite(), {Platform.TELEGRAM: messenger}
    )  # type: ignore[arg-type]

    await service.handle(incoming(1, text="/admin"))
    callbacks = {
        button.callback for row in messenger.messages[-1].buttons for button in row if button.callback
    }
    assert "cms:home" in callbacks

    await service.handle(incoming(2, callback="cms:home"))
    assert "Тексты, кнопки и изображения" in messenger.messages[-1].text
    main = next(item for item in storage.content_entries("main") if item["content_key"] == "main.menu")

    await service.handle(incoming(3, callback=f"cms:text:{main['id']}"))
    await service.handle(incoming(4, text="Новое главное сообщение"))
    await service.handle(incoming(5, text="/menu"))

    assert messenger.messages[-1].text == "Новое главное сообщение"
