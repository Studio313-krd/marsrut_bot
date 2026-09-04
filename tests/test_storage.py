from __future__ import annotations

import sqlite3

from app.domain import AdminRole, Button, IncomingEvent, OutgoingMessage, Platform
from app.storage import Storage


def event(user_id: str = "100") -> IncomingEvent:
    return IncomingEvent(
        platform=Platform.TELEGRAM,
        update_id="1",
        user_id=user_id,
        chat_id=user_id,
        display_name="Иван Иванов",
        username="ivan",
    )


def test_invite_is_single_use_and_last_admin_cannot_be_removed(tmp_path) -> None:
    storage = Storage(tmp_path / "bot.sqlite3")
    storage.initialize()
    storage.ensure_admin(Platform.TELEGRAM, "1", "Первый администратор")
    first = storage.admin_for(Platform.TELEGRAM, "1")
    assert first and first["role"] == "ADMIN"

    try:
        storage.update_admin(first["id"], active=False)
    except ValueError as exc:
        assert "последнего администратора" in str(exc)
    else:
        raise AssertionError("The last administrator must not be disabled")

    token = storage.create_invite(first["id"], AdminRole.ADMIN)
    invited = storage.redeem_invite(token, event("2"))
    assert invited and invited["role"] == "ADMIN"
    assert storage.redeem_invite(token, event("3")) is None
    assert storage.update_admin(first["id"], active=False)
    assert storage.admin_for(Platform.TELEGRAM, "1") is None
    assert storage.admin_for(Platform.TELEGRAM, "2")
    storage.ensure_admin(Platform.TELEGRAM, "1")
    assert storage.admin_for(Platform.TELEGRAM, "1")


def test_legacy_roles_are_migrated_to_admin(tmp_path) -> None:
    path = tmp_path / "bot.sqlite3"
    with sqlite3.connect(path) as db:
        db.execute(
            """
            CREATE TABLE admins (
                id TEXT PRIMARY KEY,
                display_name TEXT NOT NULL,
                role TEXT NOT NULL CHECK(role IN ('OWNER','ADMIN','VIEWER')),
                is_active INTEGER NOT NULL DEFAULT 1,
                created_by TEXT,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            )
            """
        )
        db.executemany(
            "INSERT INTO admins VALUES(?,?,?,1,NULL,?,?)",
            [
                ("owner", "Старый владелец", "OWNER", "now", "now"),
                ("viewer", "Старый наблюдатель", "VIEWER", "now", "now"),
            ],
        )

    storage = Storage(path)
    storage.initialize()

    assert {item["role"] for item in storage.list_admins()} == {"ADMIN"}


def test_delivery_queue_is_idempotent_and_recoverable(tmp_path) -> None:
    storage = Storage(tmp_path / "bot.sqlite3")
    storage.initialize()
    payload = {"text": "hello", "buttons": []}
    assert storage.enqueue(Platform.TELEGRAM, "10", payload, dedupe_key="same")
    assert not storage.enqueue(Platform.TELEGRAM, "10", payload, dedupe_key="same")

    delivery = storage.claim_delivery()
    assert delivery and delivery["payload"]["text"] == "hello"
    storage.fail_delivery(delivery["id"], delivery["attempts"], "temporary")
    assert storage.queue_stats()["PENDING"] == 1


def test_user_deletion_cascades_private_bot_data(tmp_path) -> None:
    storage = Storage(tmp_path / "bot.sqlite3")
    storage.initialize()
    incoming = event()
    storage.upsert_user(incoming)
    storage.set_conversation(incoming.platform, incoming.user_id, "application", "name", {"name": "Иван"})
    storage.delete_user_data(incoming.platform, incoming.user_id)
    assert storage.get_user(incoming.platform, incoming.user_id) is None
    assert storage.conversation(incoming.platform, incoming.user_id) is None


def test_content_overrides_images_buttons_and_custom_page(tmp_path) -> None:
    storage = Storage(tmp_path / "bot.sqlite3")
    storage.initialize()
    original = OutgoingMessage(
        "Заявка MP-TEST",
        [[Button("Открыть", callback="requests:mine")]],
        content_key="test.answer",
        content_title="Тестовый ответ",
        content_category="requests",
    )
    storage.customize_message(original)
    entry = storage.content_entries("requests")[0]

    storage.set_content_text(entry["id"], "До {default} после")
    storage.set_content_images(entry["id"], ["https://example.test/image.jpg"])
    button_id, page_id = storage.create_custom_button(entry["id"], "Подробнее", "Новая страница")

    rendered = storage.customize_message(original)
    assert rendered.text == "До Заявка MP-TEST после"
    assert rendered.images == ["https://example.test/image.jpg"]
    assert rendered.buttons[-1][0].callback == f"page:show:{page_id}"
    assert storage.custom_content_message(page_id)

    preview = storage.content_preview_message(entry["id"])
    assert preview and preview.text == rendered.text
    assert preview.images == rendered.images
    assert preview.buttons[-1][0].callback == f"page:show:{page_id}"

    storage.toggle_content_button(button_id)
    rendered = storage.customize_message(original)
    assert all(button.callback != f"page:show:{page_id}" for row in rendered.buttons for button in row)


def test_disabled_feature_hides_button(tmp_path) -> None:
    storage = Storage(tmp_path / "bot.sqlite3")
    storage.initialize()
    storage.toggle_feature("application")
    rendered = storage.customize_message(
        OutgoingMessage(
            "Меню",
            [[Button("Стать героем", callback="apply:start"), Button("О проекте", callback="about")]],
            content_key="test.menu",
        )
    )
    assert [button.text for row in rendered.buttons for button in row] == ["О проекте"]


def test_default_placeholder_is_only_expanded_in_an_override(tmp_path) -> None:
    storage = Storage(tmp_path / "bot.sqlite3")
    storage.initialize()
    original = OutgoingMessage(
        "Подсказка про {default}", content_key="test.placeholder", content_title="Подсказка"
    )

    assert storage.customize_message(original).text == "Подсказка про {default}"


def test_custom_page_from_admin_content_is_not_public(tmp_path) -> None:
    storage = Storage(tmp_path / "bot.sqlite3")
    storage.initialize()
    storage.customize_message(
        OutgoingMessage(
            "Служебный ответ",
            content_key="test.admin",
            content_title="Служебный ответ",
            content_category="admin",
        )
    )
    parent = next(item for item in storage.content_entries("admin") if item["content_key"] == "test.admin")
    _button_id, page_id = storage.create_custom_button(parent["id"], "Внутренняя", "Секрет")

    assert storage.custom_content_message(page_id) is None
    assert storage.custom_content_message(page_id, allow_admin=True)
