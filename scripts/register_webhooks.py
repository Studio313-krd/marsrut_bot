from __future__ import annotations

import asyncio

from app.config import load_settings
from app.messengers.max import MaxMessenger
from app.messengers.telegram import TelegramMessenger


async def main() -> None:
    settings = load_settings()
    messengers = []
    try:
        if settings.telegram_enabled:
            telegram = TelegramMessenger(settings.telegram_token)
            messengers.append(telegram)
            await telegram.register_webhook(settings.public_base_url, settings.telegram_webhook_secret)
            print("Telegram webhook registered")
        if settings.max_enabled:
            max_messenger = MaxMessenger(settings.max_token)
            messengers.append(max_messenger)
            await max_messenger.register_webhook(settings.public_base_url, settings.max_webhook_secret)
            print("MAX webhook registered")
    finally:
        await asyncio.gather(*(messenger.close() for messenger in messengers))


if __name__ == "__main__":
    asyncio.run(main())
