from __future__ import annotations

import base64
import hashlib
import hmac

import httpx
import pytest
import respx

from app.domain import OutgoingMessage
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
