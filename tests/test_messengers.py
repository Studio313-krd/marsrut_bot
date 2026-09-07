from __future__ import annotations

import base64
import hashlib
import hmac
import json

import httpx
import pytest
import respx

from app.domain import Button, IncomingEvent, OutgoingMessage, Platform
from app.messengers.max import MaxMessenger
from app.messengers.telegram import TelegramMessenger


def test_telegram_rejects_someone_elses_contact() -> None:
    messenger = TelegramMessenger("token")
    payload = {
        "update_id": 10,
        "message": {
            "message_id": 20,
            "from": {"id": 1, "first_name": "Иван"},
            "chat": {"id": 1},
            "contact": {"user_id": 2, "phone_number": "+79991234567"},
        },
    }
    parsed = messenger.parse_update(payload)
    assert parsed and parsed.phone is None


def test_max_accepts_verified_contact() -> None:
    token = "secret-token"
    messenger = MaxMessenger(token)
    vcf = "BEGIN:VCARD\r\nVERSION:3.0\r\nTEL;TYPE=cell:+79991234567\r\nFN:Ivan\r\nEND:VCARD\r\n"
    digest = hmac.new(token.encode(), vcf.encode(), hashlib.sha256).digest()
    payload = {
        "update_type": "message_created",
        "message": {
            "mid": "abc",
            "sender": {"user_id": 5, "name": "Иван"},
            "recipient": {"chat_id": 7},
            "body": {
                "attachments": [
                    {
                        "type": "contact",
                        "payload": {"vcf_info": vcf, "hash": base64.b64encode(digest).decode()},
                    }
                ]
            },
        },
    }
    parsed = messenger.parse_update(payload)
    assert parsed and parsed.phone == "+79991234567"


@pytest.mark.asyncio
@respx.mock
async def test_telegram_sends_multiple_images_before_text() -> None:
    photos = respx.post("https://api.telegram.org/bottoken/sendPhoto").mock(
        return_value=httpx.Response(200, json={"ok": True, "result": {}})
    )
    text = respx.post("https://api.telegram.org/bottoken/sendMessage").mock(
        return_value=httpx.Response(200, json={"ok": True, "result": {}})
    )
    messenger = TelegramMessenger("token")
    try:
        await messenger.send(
            "1",
            OutgoingMessage("Ответ", images=["https://example.test/1.jpg", "https://example.test/2.jpg"]),
        )
    finally:
        await messenger.close()

    assert photos.call_count == 2
    assert text.called


@pytest.mark.asyncio
@respx.mock
async def test_telegram_uploads_bot_owned_image_directly(tmp_path) -> None:
    media_dir = tmp_path / "cms-media"
    media_dir.mkdir()
    filename = f"{'a' * 32}.jpg"
    (media_dir / filename).write_bytes(b"\xff\xd8\xfflocal-photo")
    photo = respx.post("https://api.telegram.org/bottoken/sendPhoto").mock(
        return_value=httpx.Response(200, json={"ok": True, "result": {}})
    )
    respx.post("https://api.telegram.org/bottoken/sendMessage").mock(
        return_value=httpx.Response(200, json={"ok": True, "result": {}})
    )
    messenger = TelegramMessenger(
        "token",
        cms_media_dir=media_dir,
        public_base_url="https://bot.example.test",
    )
    try:
        failed = await messenger.send(
            "1",
            OutgoingMessage("Ответ", images=[f"https://bot.example.test/cms-media/{filename}"]),
        )
    finally:
        await messenger.close()

    assert failed == []
    assert b"local-photo" in photo.calls.last.request.content
    assert b"multipart/form-data" in photo.calls.last.request.headers["content-type"].encode()


@pytest.mark.asyncio
@respx.mock
async def test_telegram_downloads_photo_sent_by_admin() -> None:
    metadata = respx.get("https://api.telegram.org/bottoken/getFile?file_id=large").mock(
        return_value=httpx.Response(200, json={"ok": True, "result": {"file_path": "photos/admin.jpg"}})
    )
    image = respx.get("https://api.telegram.org/file/bottoken/photos/admin.jpg").mock(
        return_value=httpx.Response(200, content=b"\xff\xd8\xffphoto", headers={"content-type": "image/jpeg"})
    )
    messenger = TelegramMessenger("token")
    event = IncomingEvent(
        platform=Platform.TELEGRAM,
        update_id="1",
        user_id="1",
        chat_id="1",
        display_name="Администратор",
        raw={
            "message": {
                "photo": [
                    {"file_id": "small", "file_size": 100},
                    {"file_id": "large", "file_size": 1000},
                ]
            }
        },
    )
    try:
        downloaded = await messenger.download_image(event)
    finally:
        await messenger.close()

    assert metadata.called and image.called
    assert downloaded == (b"\xff\xd8\xffphoto", "image/jpeg")


@pytest.mark.asyncio
@respx.mock
async def test_telegram_configures_start_command_and_menu_button() -> None:
    commands = respx.post("https://api.telegram.org/bottoken/setMyCommands").mock(
        return_value=httpx.Response(200, json={"ok": True, "result": True})
    )
    menu = respx.post("https://api.telegram.org/bottoken/setChatMenuButton").mock(
        return_value=httpx.Response(200, json={"ok": True, "result": True})
    )
    messenger = TelegramMessenger("token")
    try:
        await messenger.configure_bot()
    finally:
        await messenger.close()

    command_payload = json.loads(commands.calls.last.request.content)
    menu_payload = json.loads(menu.calls.last.request.content)
    assert command_payload == {"commands": [{"command": "start", "description": "Перейти к главной"}]}
    assert menu_payload == {"menu_button": {"type": "commands"}}


@pytest.mark.asyncio
@respx.mock
async def test_telegram_keeps_inline_home_button_when_reply_keyboard_is_removed() -> None:
    sent = respx.post("https://api.telegram.org/bottoken/sendMessage").mock(
        return_value=httpx.Response(200, json={"ok": True, "result": {}})
    )
    messenger = TelegramMessenger("token")
    try:
        await messenger.send(
            "1",
            OutgoingMessage(
                "Действие отменено.",
                [[Button("Главное меню", callback="menu")]],
                remove_keyboard=True,
            ),
        )
    finally:
        await messenger.close()

    payload = json.loads(sent.calls.last.request.content)
    assert payload["reply_markup"] == {
        "inline_keyboard": [[{"text": "Главное меню", "callback_data": "menu"}]]
    }


@pytest.mark.asyncio
@respx.mock
async def test_telegram_contact_keyboard_has_back_cancel_and_home() -> None:
    sent = respx.post("https://api.telegram.org/bottoken/sendMessage").mock(
        return_value=httpx.Response(200, json={"ok": True, "result": {}})
    )
    messenger = TelegramMessenger("token")
    try:
        await messenger.send(
            "1",
            OutgoingMessage(
                "Укажите телефон",
                [
                    [Button("Поделиться контактом", kind="request_contact")],
                    [Button("Назад", callback="apply:back"), Button("Отменить", callback="flow:cancel")],
                    [Button("Главное меню", callback="menu")],
                ],
            ),
        )
    finally:
        await messenger.close()

    payload = json.loads(sent.calls.last.request.content)
    keyboard = payload["reply_markup"]["keyboard"]
    assert keyboard == [
        [{"text": "Поделиться контактом", "request_contact": True}],
        [{"text": "Назад"}, {"text": "Отменить"}],
        [{"text": "Главное меню"}],
    ]


@pytest.mark.asyncio
@respx.mock
async def test_max_sends_multiple_images_before_text() -> None:
    messages = respx.post("https://platform-api2.max.ru/messages").mock(
        return_value=httpx.Response(200, json={"message": {}})
    )
    messenger = MaxMessenger("token")
    try:
        await messenger.send(
            "1",
            OutgoingMessage("Ответ", images=["https://example.test/1.jpg", "https://example.test/2.jpg"]),
        )
    finally:
        await messenger.close()

    assert messages.call_count == 3


@pytest.mark.asyncio
@respx.mock
async def test_max_uploads_bot_owned_image_directly(tmp_path) -> None:
    media_dir = tmp_path / "cms-media"
    media_dir.mkdir()
    filename = f"{'b' * 32}.png"
    (media_dir / filename).write_bytes(b"\x89PNG\r\n\x1a\nlocal-photo")
    respx.post("https://platform-api2.max.ru/uploads?type=image").mock(
        return_value=httpx.Response(200, json={"url": "https://iu.oneme.ru/upload.do"})
    )
    upload = respx.post("https://iu.oneme.ru/upload.do").mock(
        return_value=httpx.Response(200, json={"token": "image-token"})
    )
    messages = respx.post("https://platform-api2.max.ru/messages").mock(
        return_value=httpx.Response(200, json={"message": {}})
    )
    messenger = MaxMessenger(
        "token",
        cms_media_dir=media_dir,
        public_base_url="https://bot.example.test",
    )
    try:
        failed = await messenger.send(
            "1",
            OutgoingMessage("Ответ", images=[f"https://bot.example.test/cms-media/{filename}"]),
        )
    finally:
        await messenger.close()

    assert failed == []
    assert b"local-photo" in upload.calls.last.request.content
    image_payload = json.loads(messages.calls[0].request.content)
    assert image_payload["attachments"][0]["payload"] == {"token": "image-token"}


@pytest.mark.asyncio
@respx.mock
async def test_max_downloads_photo_sent_by_admin() -> None:
    image = respx.get("https://iu.oneme.ru/photo.jpg").mock(
        return_value=httpx.Response(200, content=b"\xff\xd8\xffphoto", headers={"content-type": "image/jpeg"})
    )
    messenger = MaxMessenger("token")
    event = IncomingEvent(
        platform=Platform.MAX,
        update_id="1",
        user_id="1",
        chat_id="1",
        display_name="Администратор",
        raw={
            "message": {
                "body": {
                    "attachments": [{"type": "image", "payload": {"url": "https://iu.oneme.ru/photo.jpg"}}]
                }
            }
        },
    )
    try:
        downloaded = await messenger.download_image(event)
    finally:
        await messenger.close()

    assert image.called
    assert downloaded == (b"\xff\xd8\xffphoto", "image/jpeg")
