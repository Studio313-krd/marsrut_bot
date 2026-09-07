from __future__ import annotations

import json
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
        self.incoming_image: tuple[bytes, str] | None = None
        self.failed_images: list[str] = []

    def parse_update(self, payload: dict[str, Any]) -> IncomingEvent | None:
        del payload
        return None

    async def send(self, recipient_id: str, message: OutgoingMessage) -> list[str]:
        del recipient_id
        self.messages.append(message)
        return self.failed_images

    async def download_image(self, event: IncomingEvent) -> tuple[bytes, str] | None:
        del event
        return self.incoming_image

    async def register_webhook(self, public_base_url: str, secret: str) -> None:
        del public_base_url, secret

    async def close(self) -> None:
        return None


class FakeSite:
    def __init__(self) -> None:
        self.created: dict[str, Any] | None = None
        self.content_kinds: list[str] = []

    async def requests(self, **filters: Any) -> dict[str, Any]:
        del filters
        return {"items": [], "pagination": {"total": 0, "hasMore": False}}

    async def create_request(self, body: dict[str, Any]) -> dict[str, Any]:
        self.created = body
        return {"created": True, "request": {**body, "id": "request-1", "requestNumber": "MP-TEST"}}

    async def add_activity(self, request_id: str, body: dict[str, Any]) -> dict[str, Any]:
        del request_id, body
        return {}

    async def content(self, kind: str, **filters: Any) -> dict[str, Any]:
        del filters
        self.content_kinds.append(kind)
        return {"items": [], "pagination": {"total": 0, "hasMore": False}}


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
        telegram_admin_ids=(),
        max_token="",
        max_webhook_secret="",
        max_username="",
        max_admin_ids=(),
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
    assert any(button.callback == "menu" for row in messenger.messages[-1].buttons for button in row)


@pytest.mark.asyncio
async def test_cancelled_flow_always_offers_main_menu(tmp_path) -> None:
    storage = Storage(tmp_path / "bot.sqlite3")
    storage.initialize()
    messenger = FakeMessenger()
    service = BotService(
        settings(tmp_path / "bot.sqlite3"), storage, FakeSite(), {Platform.TELEGRAM: messenger}
    )  # type: ignore[arg-type]

    await service.handle(incoming(1, callback="apply:start"))
    await service.handle(incoming(2, callback="flow:cancel"))

    message = messenger.messages[-1]
    assert message.text == "Действие отменено."
    assert any(button.callback == "menu" for row in message.buttons for button in row)
    assert storage.conversation(Platform.TELEGRAM, "100") is None


@pytest.mark.asyncio
async def test_contact_step_has_complete_navigation(tmp_path) -> None:
    storage = Storage(tmp_path / "bot.sqlite3")
    storage.initialize()
    messenger = FakeMessenger()
    service = BotService(
        settings(tmp_path / "bot.sqlite3"), storage, FakeSite(), {Platform.TELEGRAM: messenger}
    )  # type: ignore[arg-type]

    await service.handle(incoming(1, callback="apply:start"))
    await service.handle(incoming(2, text="Иван Иванов"))
    await service.handle(incoming(3, text="Проект"))
    await service.handle(incoming(4, text="Основатель"))

    callbacks = {
        button.callback for row in messenger.messages[-1].buttons for button in row if button.callback
    }
    kinds = {button.kind for row in messenger.messages[-1].buttons for button in row}
    assert "request_contact" in kinds
    assert {"apply:back", "flow:cancel", "menu"} <= callbacks


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
async def test_old_interviews_button_uses_videos_source(tmp_path) -> None:
    storage = Storage(tmp_path / "bot.sqlite3")
    storage.initialize()
    messenger = FakeMessenger()
    site = FakeSite()
    service = BotService(settings(tmp_path / "bot.sqlite3"), storage, site, {Platform.TELEGRAM: messenger})  # type: ignore[arg-type]

    await service.handle(incoming(1, callback="content:interviews:0"))

    assert site.content_kinds == ["videos"]


@pytest.mark.asyncio
async def test_admin_edits_main_message_from_bot_panel(tmp_path) -> None:
    storage = Storage(tmp_path / "bot.sqlite3")
    storage.initialize()
    storage.ensure_admin(Platform.TELEGRAM, "100", "Администратор")
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
    assert "Редактор сообщений" in messenger.messages[-1].text
    main = next(item for item in storage.content_entries("main") if item["content_key"] == "main.menu")

    await service.handle(incoming(3, callback=f"cms:text:{main['id']}"))
    assert "{default}" not in messenger.messages[-1].text
    assert "HTML" not in messenger.messages[-1].text
    await service.handle(incoming(4, callback=f"cms:text-replace:{main['id']}"))
    await service.handle(incoming(5, text="Новое главное сообщение"))
    await service.handle(incoming(6, text="/menu"))

    assert messenger.messages[-1].text == "Новое главное сообщение"


@pytest.mark.asyncio
async def test_admin_can_add_photo_without_a_url(tmp_path) -> None:
    storage = Storage(tmp_path / "bot.sqlite3")
    storage.initialize()
    storage.ensure_admin(Platform.TELEGRAM, "100", "Администратор")
    messenger = FakeMessenger()
    messenger.incoming_image = (b"\x89PNG\r\n\x1a\nimage-data", "image/png")
    service = BotService(
        settings(tmp_path / "bot.sqlite3"), storage, FakeSite(), {Platform.TELEGRAM: messenger}
    )  # type: ignore[arg-type]
    main = next(item for item in storage.content_entries("main") if item["content_key"] == "main.menu")

    await service.handle(incoming(1, callback=f"cms:images:{main['id']}"))
    assert "как обычное фото" in messenger.messages[-1].text
    assert "HTTPS" not in messenger.messages[-1].text
    await service.handle(incoming(2))

    entry = storage.content_entry(main["id"])
    assert entry
    image_url = json.loads(entry["images_json"])[0]
    assert image_url.startswith("https://bot.example.test/cms-media/")
    assert (tmp_path / "cms-media" / image_url.rsplit("/", 1)[1]).is_file()
    assert "Картинка добавлена" in messenger.messages[-1].text

    await service.handle(incoming(3, callback=f"cms:images-done:{main['id']}"))
    assert storage.conversation(Platform.TELEGRAM, "100") is None
    assert "Картинки сохранены" in messenger.messages[-1].text

    await service.handle(incoming(4, callback=f"cms:images:{main['id']}"))
    await service.handle(incoming(5, callback=f"cms:images-clear:{main['id']}"))
    assert not (tmp_path / "cms-media" / image_url.rsplit("/", 1)[1]).exists()
    assert json.loads(storage.content_entry(main["id"])["images_json"]) == []


@pytest.mark.asyncio
async def test_admin_can_add_text_before_dynamic_message_without_technical_marker(tmp_path) -> None:
    storage = Storage(tmp_path / "bot.sqlite3")
    storage.initialize()
    storage.ensure_admin(Platform.TELEGRAM, "100", "Администратор")
    messenger = FakeMessenger()
    service = BotService(
        settings(tmp_path / "bot.sqlite3"), storage, FakeSite(), {Platform.TELEGRAM: messenger}
    )  # type: ignore[arg-type]
    entry = next(item for item in storage.content_entries("main") if item["content_key"] == "main.menu")

    await service.handle(incoming(1, callback=f"cms:text-before:{entry['id']}"))
    assert "{default}" not in messenger.messages[-1].text
    await service.handle(incoming(2, text="Спасибо за обращение!"))

    updated = storage.content_entry(entry["id"])
    assert updated and updated["text_override"] == "Спасибо за обращение!\n\n{default}"
    assert "{default}" not in messenger.messages[-1].text
    assert "Спасибо за обращение!" in messenger.messages[-1].text

    await service.handle(incoming(3, callback=f"cms:preview:{entry['id']}"))
    preview = messenger.messages[-2]
    assert preview.text.count("Спасибо за обращение!") == 1
    assert "МАРШРУТ ПОСТРОЕН" in preview.text


@pytest.mark.asyncio
async def test_admin_preview_reports_image_delivery_failure(tmp_path) -> None:
    storage = Storage(tmp_path / "bot.sqlite3")
    storage.initialize()
    storage.ensure_admin(Platform.TELEGRAM, "100", "Администратор")
    messenger = FakeMessenger()
    service = BotService(
        settings(tmp_path / "bot.sqlite3"), storage, FakeSite(), {Platform.TELEGRAM: messenger}
    )  # type: ignore[arg-type]
    entry = next(item for item in storage.content_entries("main") if item["content_key"] == "main.menu")
    storage.set_content_images(entry["id"], ["https://bot.example.test/cms-media/missing.jpg"])
    messenger.failed_images = ["https://bot.example.test/cms-media/missing.jpg"]

    await service.handle(incoming(1, callback=f"cms:preview:{entry['id']}"))

    assert "картинки не отправились" in messenger.messages[-1].text


@pytest.mark.asyncio
async def test_every_admin_can_manage_access_without_role_choices(tmp_path) -> None:
    storage = Storage(tmp_path / "bot.sqlite3")
    storage.initialize()
    storage.ensure_admin(Platform.TELEGRAM, "100", "Первый администратор")
    messenger = FakeMessenger()
    service = BotService(
        settings(tmp_path / "bot.sqlite3"), storage, FakeSite(), {Platform.TELEGRAM: messenger}
    )  # type: ignore[arg-type]
    admin = storage.admin_for(Platform.TELEGRAM, "100")
    assert admin

    await service.show_admins(incoming(1), admin)

    callbacks = {
        button.callback for row in messenger.messages[-1].buttons for button in row if button.callback
    }
    assert "adm:add:ADMIN" in callbacks
    assert any(callback.startswith("adm:view:") for callback in callbacks)
    assert not any(callback.startswith("adm:role:") for callback in callbacks)
