from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from zoneinfo import ZoneInfo

from dotenv import load_dotenv


def _csv_ids(value: str) -> tuple[str, ...]:
    return tuple(item.strip() for item in value.split(",") if item.strip())


def _integer(name: str, default: int) -> int:
    try:
        return int(os.getenv(name, str(default)))
    except ValueError as exc:
        raise RuntimeError(f"{name} must be an integer") from exc


@dataclass(frozen=True, slots=True)
class Settings:
    app_env: str
    host: str
    port: int
    app_secret: str
    public_base_url: str
    site_base_url: str
    site_public_url: str
    privacy_url: str
    database_path: Path
    log_level: str
    timezone: ZoneInfo
    bot_api_key_id: str
    bot_api_secret: str
    telegram_token: str
    telegram_webhook_secret: str
    telegram_username: str
    telegram_admin_ids: tuple[str, ...]
    max_token: str
    max_webhook_secret: str
    max_username: str
    max_admin_ids: tuple[str, ...]
    event_poll_interval: int
    reminder_poll_interval: int
    daily_digest_hour: int
    outbox_retention_days: int

    @property
    def telegram_enabled(self) -> bool:
        return bool(self.telegram_token)

    @property
    def max_enabled(self) -> bool:
        return bool(self.max_token)


def load_settings(*, require_platform: bool = True) -> Settings:
    load_dotenv()
    settings = Settings(
        app_env=os.getenv("APP_ENV", "development"),
        host=os.getenv("APP_HOST", "127.0.0.1"),
        port=_integer("APP_PORT", 8080),
        app_secret=os.getenv("APP_SECRET", ""),
        public_base_url=os.getenv("PUBLIC_BASE_URL", "").rstrip("/"),
        site_base_url=os.getenv("SITE_BASE_URL", "").rstrip("/"),
        site_public_url=os.getenv("SITE_PUBLIC_URL", os.getenv("SITE_BASE_URL", "")).rstrip("/"),
        privacy_url=os.getenv("PRIVACY_URL", "").strip(),
        database_path=Path(os.getenv("DATABASE_PATH", "data/bot.sqlite3")),
        log_level=os.getenv("LOG_LEVEL", "INFO").upper(),
        timezone=ZoneInfo(os.getenv("TIMEZONE", "Europe/Moscow")),
        bot_api_key_id=os.getenv("BOT_API_KEY_ID", ""),
        bot_api_secret=os.getenv("BOT_API_SECRET", ""),
        telegram_token=os.getenv("TELEGRAM_BOT_TOKEN", ""),
        telegram_webhook_secret=os.getenv("TELEGRAM_WEBHOOK_SECRET", ""),
        telegram_username=os.getenv("TELEGRAM_BOT_USERNAME", "").lstrip("@"),
        telegram_admin_ids=_csv_ids(os.getenv("TELEGRAM_ADMIN_IDS", os.getenv("TELEGRAM_OWNER_IDS", ""))),
        max_token=os.getenv("MAX_BOT_TOKEN", ""),
        max_webhook_secret=os.getenv("MAX_WEBHOOK_SECRET", ""),
        max_username=os.getenv("MAX_BOT_USERNAME", "").lstrip("@"),
        max_admin_ids=_csv_ids(os.getenv("MAX_ADMIN_IDS", os.getenv("MAX_OWNER_IDS", ""))),
        event_poll_interval=max(2, _integer("EVENT_POLL_INTERVAL_SECONDS", 5)),
        reminder_poll_interval=max(15, _integer("REMINDER_POLL_INTERVAL_SECONDS", 60)),
        daily_digest_hour=min(23, max(0, _integer("DAILY_DIGEST_HOUR", 9))),
        outbox_retention_days=max(7, _integer("OUTBOX_RETENTION_DAYS", 30)),
    )
    required = {
        "APP_SECRET": settings.app_secret,
        "PUBLIC_BASE_URL": settings.public_base_url,
        "SITE_BASE_URL": settings.site_base_url,
        "SITE_PUBLIC_URL": settings.site_public_url,
        "PRIVACY_URL": settings.privacy_url,
        "BOT_API_KEY_ID": settings.bot_api_key_id,
        "BOT_API_SECRET": settings.bot_api_secret,
    }
    missing = [name for name, value in required.items() if not value]
    if missing:
        raise RuntimeError(f"Missing required settings: {', '.join(missing)}")
    if len(settings.app_secret) < 32 or len(settings.bot_api_secret) < 32:
        raise RuntimeError("APP_SECRET and BOT_API_SECRET must contain at least 32 characters")
    if require_platform and not (settings.telegram_enabled or settings.max_enabled):
        raise RuntimeError("Configure at least one messenger token")
    if settings.telegram_enabled and len(settings.telegram_webhook_secret) < 16:
        raise RuntimeError("TELEGRAM_WEBHOOK_SECRET must contain at least 16 characters")
    if settings.max_enabled and len(settings.max_webhook_secret) < 16:
        raise RuntimeError("MAX_WEBHOOK_SECRET must contain at least 16 characters")
    return settings
