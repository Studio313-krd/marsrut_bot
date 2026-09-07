from __future__ import annotations

import asyncio
import contextlib
import hmac
import logging
import re
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager

from fastapi import FastAPI, Header, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse

from app.config import Settings, load_settings
from app.domain import Platform
from app.messengers.base import Messenger
from app.messengers.max import MaxMessenger
from app.messengers.telegram import TelegramMessenger
from app.service import BotService
from app.site_client import SiteApiError, SiteClient
from app.storage import Storage

logger = logging.getLogger(__name__)


async def _forever(name: str, interval: float, action: Callable[[], Awaitable[object]]) -> None:
    while True:
        try:
            await action()
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("Background task %s failed", name)
        await asyncio.sleep(interval)


def build_service(settings: Settings) -> BotService:
    storage = Storage(settings.database_path)
    storage.initialize()
    messengers: dict[Platform, Messenger] = {}
    cms_media_dir = settings.database_path.parent / "cms-media"
    if settings.telegram_enabled:
        messengers[Platform.TELEGRAM] = TelegramMessenger(
            settings.telegram_token,
            cms_media_dir=cms_media_dir,
            public_base_url=settings.public_base_url,
        )
    if settings.max_enabled:
        messengers[Platform.MAX] = MaxMessenger(
            settings.max_token,
            cms_media_dir=cms_media_dir,
            public_base_url=settings.public_base_url,
        )
    service = BotService(
        settings=settings,
        storage=storage,
        site=SiteClient(settings.site_base_url, settings.bot_api_key_id, settings.bot_api_secret),
        messengers=messengers,
    )
    service.bootstrap_admins()
    return service


async def _configure_bot_interfaces(service: BotService) -> None:
    for messenger in service.messengers.values():
        try:
            await messenger.configure_bot()
        except Exception:
            # A temporary platform API outage must not make the webhook service unavailable.
            logger.warning("Could not configure %s bot interface", messenger.platform.value)


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    settings = load_settings()
    logging.basicConfig(
        level=getattr(logging, settings.log_level, logging.INFO),
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    service = build_service(settings)
    app.state.settings = settings
    app.state.service = service
    tasks = [
        asyncio.create_task(_configure_bot_interfaces(service)),
        asyncio.create_task(
            _forever("site-events", settings.event_poll_interval, service.process_site_events_once)
        ),
        asyncio.create_task(_forever("delivery", 0.25, service.process_delivery_once)),
        asyncio.create_task(
            _forever("reminders", settings.reminder_poll_interval, service.process_reminders_once)
        ),
        asyncio.create_task(_forever("digest", 60, service.process_daily_digest_once)),
    ]
    service.storage.cleanup(settings.outbox_retention_days)
    logger.info(
        "Bot service started with platforms: %s", ", ".join(platform.value for platform in service.messengers)
    )
    try:
        yield
    finally:
        for task in tasks:
            task.cancel()
        for task in tasks:
            with contextlib.suppress(asyncio.CancelledError):
                await task
        await service.site.close()
        await asyncio.gather(*(messenger.close() for messenger in service.messengers.values()))


app = FastAPI(title="Маршрут построен — bot gateway", docs_url=None, redoc_url=None, lifespan=lifespan)


def _secret_matches(received: str | None, expected: str) -> bool:
    return bool(received and hmac.compare_digest(received, expected))


@app.get("/health")
async def health() -> dict[str, str]:
    return {"status": "ok"}


@app.get("/health/details")
async def health_details(
    request: Request, x_health_token: str | None = Header(default=None)
) -> dict[str, object]:
    settings: Settings = request.app.state.settings
    if not _secret_matches(x_health_token, settings.app_secret):
        raise HTTPException(status_code=404)
    service: BotService = request.app.state.service
    try:
        site = await service.site.health()
    except SiteApiError as exc:
        site = {"ok": False, "error": str(exc)}
    return {"status": "ok", "site": site, "queue": service.storage.queue_stats()}


@app.get("/cms-media/{filename}", include_in_schema=False)
async def cms_media(request: Request, filename: str) -> FileResponse:
    if not re.fullmatch(r"[a-f0-9]{32}\.(?:jpg|png|gif|webp)", filename):
        raise HTTPException(status_code=404)
    settings: Settings = request.app.state.settings
    path = settings.database_path.parent / "cms-media" / filename
    if not path.is_file():
        raise HTTPException(status_code=404)
    return FileResponse(
        path,
        headers={
            "Cache-Control": "public, max-age=31536000, immutable",
            "X-Content-Type-Options": "nosniff",
        },
    )


@app.post("/webhooks/telegram")
async def telegram_webhook(
    request: Request,
    x_telegram_bot_api_secret_token: str | None = Header(default=None),
) -> JSONResponse:
    settings: Settings = request.app.state.settings
    if not settings.telegram_enabled or not _secret_matches(
        x_telegram_bot_api_secret_token, settings.telegram_webhook_secret
    ):
        raise HTTPException(status_code=403, detail="Invalid webhook secret")
    service: BotService = request.app.state.service
    payload = await request.json()
    event = service.messengers[Platform.TELEGRAM].parse_update(payload)
    if event:
        try:
            await service.handle(event)
        except Exception:
            service.storage.unmark_update(event.platform, event.update_id)
            raise
    return JSONResponse({"ok": True})


@app.post("/webhooks/max")
async def max_webhook(
    request: Request,
    x_max_bot_api_secret: str | None = Header(default=None),
) -> JSONResponse:
    settings: Settings = request.app.state.settings
    if not settings.max_enabled or not _secret_matches(x_max_bot_api_secret, settings.max_webhook_secret):
        raise HTTPException(status_code=403, detail="Invalid webhook secret")
    service: BotService = request.app.state.service
    payload = await request.json()
    updates = payload.get("updates") if isinstance(payload, dict) else None
    for update in updates if isinstance(updates, list) else [payload]:
        event = service.messengers[Platform.MAX].parse_update(update)
        if event:
            try:
                await service.handle(event)
            except Exception:
                service.storage.unmark_update(event.platform, event.update_id)
                raise
    return JSONResponse({"ok": True})
