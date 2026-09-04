from __future__ import annotations

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


def test_invite_is_single_use_and_owner_cannot_be_removed(tmp_path) -> None:
    storage = Storage(tmp_path / "bot.sqlite3")
    storage.initialize()
    storage.ensure_owner(Platform.TELEGRAM, "1", "Владелец")
    owner = storage.admin_for(Platform.TELEGRAM, "1")
    assert owner and owner["role"] == "OWNER"

    token = storage.create_invite(owner["id"], AdminRole.ADMIN)
    invited = storage.redeem_invite(token, event("2"))
    assert invited and invited["role"] == "ADMIN"
    assert storage.redeem_invite(token, event("3")) is None

    try:
        storage.update_admin(owner["id"], active=False)
    except ValueError as exc:
        assert "последнего владельца" in str(exc)
    else:
        raise AssertionError("The last owner must not be disabled")


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
