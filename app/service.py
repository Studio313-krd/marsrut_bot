from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import re
import secrets
from collections import defaultdict, deque
from datetime import UTC, datetime, timedelta
from html import escape
from typing import Any

from app.config import Settings
from app.content import CATEGORY_LABELS, CONTENT_CATALOG, feature_for_callback
from app.domain import AdminRole, Button, IncomingEvent, OutgoingMessage, Platform
from app.messengers.base import Messenger
from app.site_client import SiteApiError, SiteClient
from app.storage import Storage
from app.templates import (
    CONTENT_LABELS,
    ROLE_LABELS,
    SOURCE_LABELS,
    STATUS_LABELS,
    admin_menu,
    application_prompt,
    application_review,
    content_list,
    editable_default_messages,
    format_datetime,
    plain_text,
    request_card,
    safe,
    user_menu,
)

logger = logging.getLogger(__name__)

_PHONE_RE = re.compile(r"^[+\d][\d\s()\-]{4,39}$")
_EMAIL_RE = re.compile(r"^[^\s@]+@[^\s@]+\.[^\s@]+$")
_APPLICATION_STEPS = ["name", "company", "position", "phone", "email", "message"]
_CMS_IMAGE_LIMIT = 10 * 1024 * 1024


def _image_suffix(payload: bytes) -> str | None:
    if payload.startswith(b"\xff\xd8\xff"):
        return ".jpg"
    if payload.startswith(b"\x89PNG\r\n\x1a\n"):
        return ".png"
    if payload.startswith((b"GIF87a", b"GIF89a")):
        return ".gif"
    if len(payload) >= 12 and payload.startswith(b"RIFF") and payload[8:12] == b"WEBP":
        return ".webp"
    return None


def _ensure_home_navigation(message: OutgoingMessage) -> None:
    """Ensure every non-home response has an obvious escape route."""
    if message.content_key == "main.menu":
        return
    if any(button.callback == "menu" for row in message.buttons for button in row):
        return
    message.buttons.append([Button("Главное меню", callback="menu")])


def outgoing_to_dict(message: OutgoingMessage) -> dict[str, Any]:
    _ensure_home_navigation(message)
    _ensure_content_identity(message)
    return {
        "text": message.text,
        "buttons": [
            [
                {"text": button.text, "callback": button.callback, "url": button.url, "kind": button.kind}
                for button in row
            ]
            for row in message.buttons
        ],
        "disable_preview": message.disable_preview,
        "remove_keyboard": message.remove_keyboard,
        "images": message.images,
        "content_key": message.content_key,
        "content_title": message.content_title,
        "content_category": message.content_category,
    }


def outgoing_from_dict(value: dict[str, Any]) -> OutgoingMessage:
    return OutgoingMessage(
        text=str(value["text"]),
        buttons=[[Button(**button) for button in row] for row in value.get("buttons", [])],
        disable_preview=bool(value.get("disable_preview", True)),
        remove_keyboard=bool(value.get("remove_keyboard", False)),
        images=[str(image) for image in value.get("images", [])],
        content_key=value.get("content_key"),
        content_title=value.get("content_title"),
        content_category=str(value.get("content_category", "other")),
    )


def _ensure_content_identity(message: OutgoingMessage) -> None:
    """Give even rare/error responses an editable, deterministic content record."""
    if message.content_key:
        return
    normalized = re.sub(r"https?://\S+", "{url}", message.text)
    normalized = re.sub(r"\b[0-9a-f]{8}-[0-9a-f-]{27,}\b", "{id}", normalized, flags=re.I)
    normalized = re.sub(r"\b\d+\b", "{number}", normalized)
    first_line = normalized.splitlines()[0] if normalized else ""
    if first_line.startswith("Не удалось") and ":" in first_line:
        first_line = first_line.partition(":")[0] + ": {detail}"
    actions = []
    for row in message.buttons:
        for button in row:
            action = button.callback or button.url or button.kind
            action = re.sub(r"https?://\S+", "{url}", action)
            action = re.sub(r"\b[0-9a-f]{8}-[0-9a-f-]{27,}\b", "{id}", action, flags=re.I)
            action = re.sub(r"\b\d+\b", "{number}", action)
            action = re.sub(
                r"^(req|request|adm):(view|take|assign|status|message|comment|remind|history|block|toggle|link):.+$",
                r"\1:\2:{value}",
                action,
            )
            actions.append(action)
    digest = hashlib.sha256((first_line + "|" + "|".join(actions)).encode()).hexdigest()[:16]
    plain_title = re.sub(r"<[^>]+>", "", message.text).splitlines()[0].strip()
    message.content_key = f"auto.{digest}"
    message.content_title = plain_title[:120] or "Служебный ответ"
    callbacks = [button.callback or "" for row in message.buttons for button in row]
    if any(
        value.startswith(("admin:", "req:", "admins:", "adm:", "broadcast:", "cms:")) for value in callbacks
    ):
        message.content_category = "admin"
    elif any(value.startswith("apply:") for value in callbacks):
        message.content_category = "application"
    elif any(value.startswith(("content:", "city:")) for value in callbacks):
        message.content_category = "catalog"
    elif any(value.startswith(("request:", "requests:")) for value in callbacks):
        message.content_category = "requests"
    elif any(value.startswith("privacy:") or value == "about" for value in callbacks):
        message.content_category = "privacy"
    else:
        message.content_category = "system"


class BotService:
    def __init__(
        self,
        settings: Settings,
        storage: Storage,
        site: SiteClient,
        messengers: dict[Platform, Messenger],
    ) -> None:
        self.settings = settings
        self.storage = storage
        self.site = site
        self.messengers = messengers
        self.cms_media_dir = settings.database_path.parent / "cms-media"
        self._rate_windows: dict[tuple[Platform, str], deque[float]] = defaultdict(deque)
        self.storage.seed_content_catalog(list(CONTENT_CATALOG))
        for message in editable_default_messages(settings.privacy_url, settings.site_public_url):
            _ensure_home_navigation(message)
            self.storage.customize_message(message)

    def bootstrap_admins(self) -> None:
        for user_id in self.settings.telegram_admin_ids:
            self.storage.ensure_admin(Platform.TELEGRAM, user_id)
        for user_id in self.settings.max_admin_ids:
            self.storage.ensure_admin(Platform.MAX, user_id)

    async def send(self, event: IncomingEvent, message: OutgoingMessage) -> None:
        _ensure_home_navigation(message)
        _ensure_content_identity(message)
        await self.messengers[event.platform].send(event.chat_id, self.storage.customize_message(message))

    async def send_to(self, platform: Platform, recipient_id: str, message: OutgoingMessage) -> None:
        messenger = self.messengers.get(platform)
        if messenger:
            _ensure_home_navigation(message)
            _ensure_content_identity(message)
            await messenger.send(recipient_id, self.storage.customize_message(message))

    def _allowed(self, event: IncomingEvent) -> bool:
        now = asyncio.get_running_loop().time()
        window = self._rate_windows[(event.platform, event.user_id)]
        while window and window[0] < now - 30:
            window.popleft()
        window.append(now)
        return len(window) <= 24

    async def handle(self, event: IncomingEvent) -> None:
        if not self.storage.mark_update(event.platform, event.update_id):
            return
        self.storage.upsert_user(event)
        user = self.storage.get_user(event.platform, event.user_id)
        if user and user["is_blocked"]:
            return
        if not self._allowed(event):
            await self.send(
                event,
                OutgoingMessage(
                    "Слишком много сообщений подряд. Подождите полминуты и попробуйте снова.",
                    content_key="system.rate_limit",
                    content_title="Слишком много сообщений",
                    content_category="system",
                ),
            )
            return
        if event.callback_id:
            try:
                await self.messengers[event.platform].answer_callback(event.callback_id)
            except Exception:
                logger.warning("Could not acknowledge callback", exc_info=True)

        text = (event.text or "").strip()
        command_token, separator, command_payload = text.partition(" ")
        command = command_token.split("@", 1)[0].casefold() if command_token.startswith("/") else ""
        normalized_text = text.casefold()
        if command == "/start":
            payload = command_payload.strip() if separator else ""
            if payload:
                redeemed = self.storage.redeem_invite(payload, event)
                if redeemed:
                    self.storage.audit(redeemed["id"], "admin.invite.redeemed", "admin", redeemed["id"])
                    await self.send(
                        event,
                        OutgoingMessage(
                            f"<b>Доступ подключён</b>\n\nВаша роль: {safe(ROLE_LABELS.get(redeemed['role'], redeemed['role']))}.",
                            [[Button("Открыть панель", callback="admin:home")]],
                            content_key="admin.access_connected",
                            content_title="Доступ администратора подключён",
                            content_category="admin",
                        ),
                    )
                    return
                if payload.upper().startswith("ADM-"):
                    await self.send(
                        event,
                        OutgoingMessage(
                            "Код приглашения недействителен, истёк или уже использован.",
                            content_key="admin.invite_invalid",
                            content_title="Недействительное приглашение",
                            content_category="admin",
                        ),
                    )
                    return
                if re.fullmatch(r"[A-Za-z0-9_-]{2,120}", payload):
                    self.storage.set_referral(event.platform, event.user_id, payload)
                    await self.show_menu(event)
                    return
                await self.send(event, OutgoingMessage("Ссылка запуска некорректна. Откройте главное меню."))
                return
            self.storage.clear_conversation(event.platform, event.user_id)
            await self.show_menu(event)
            return
        if command == "/menu" or normalized_text in {"меню", "главное меню"} or event.callback == "menu":
            self.storage.clear_conversation(event.platform, event.user_id)
            await self.show_menu(event)
            return
        conversation = self.storage.conversation(event.platform, event.user_id)
        if (
            command == "/cancel"
            or event.callback == "flow:cancel"
            or (conversation and normalized_text in {"отменить", "отмена"})
        ):
            self.storage.clear_conversation(event.platform, event.user_id)
            await self.send(
                event,
                OutgoingMessage(
                    "Действие отменено.",
                    [[Button("Главное меню", callback="menu")]],
                    remove_keyboard=True,
                    content_key="system.action_cancelled",
                    content_title="Действие отменено",
                    content_category="system",
                ),
            )
            return
        if conversation and normalized_text == "назад":
            if conversation["flow"] == "application":
                await self.application_back(event)
            else:
                self.storage.clear_conversation(event.platform, event.user_id)
                await self.show_menu(event)
            return
        if command == "/admin" or normalized_text == "админ" or event.callback == "admin:home":
            await self.show_admin_home(event)
            return
        if command == "/delete_my_data":
            if self.storage.feature_enabled("data_deletion"):
                await self.confirm_delete_data(event)
            else:
                await self.send(
                    event,
                    OutgoingMessage(
                        "Этот раздел сейчас временно отключён.",
                        [[Button("Главное меню", callback="menu")]],
                        content_key="system.feature_disabled",
                        content_title="Раздел временно отключён",
                        content_category="system",
                    ),
                )
            return

        if event.callback:
            try:
                await self.handle_callback(event, event.callback)
            except (KeyError, SiteApiError, ValueError) as exc:
                logger.info("User callback could not be completed: %s", type(exc).__name__)
                await self.send(
                    event,
                    OutgoingMessage(
                        "Не удалось выполнить действие. Возможно, данные изменились или кнопка устарела. "
                        "Вернитесь в главное меню и попробуйте снова.",
                        content_key="system.action_error",
                        content_title="Не удалось выполнить действие",
                        content_category="system",
                    ),
                )
            return
        if conversation:
            try:
                await self.handle_conversation(event, conversation)
            except (KeyError, SiteApiError, ValueError) as exc:
                logger.info("Conversation step could not be completed: %s", type(exc).__name__)
                await self.send(
                    event,
                    OutgoingMessage(
                        "Не удалось продолжить действие. Вернитесь в главное меню и попробуйте снова.",
                        content_key="system.action_error",
                        content_title="Не удалось выполнить действие",
                        content_category="system",
                    ),
                )
            return
        await self.show_menu(event)

    async def show_menu(self, event: IncomingEvent) -> None:
        user = self.storage.get_user(event.platform, event.user_id) or {}
        await self.send(
            event,
            user_menu(
                is_admin=bool(self.storage.admin_for(event.platform, event.user_id)),
                city_name=user.get("city_name"),
            ),
        )

    async def handle_callback(self, event: IncomingEvent, callback: str) -> None:
        if callback == "noop":
            return
        if callback.startswith("page:show:"):
            try:
                content_id = int(callback.rsplit(":", 1)[1])
            except ValueError:
                content_id = 0
            message = self.storage.custom_content_message(
                content_id,
                allow_admin=bool(self.storage.admin_for(event.platform, event.user_id)),
            )
            if message:
                await self.send(event, message)
            else:
                await self.send(
                    event,
                    OutgoingMessage(
                        "Эта страница больше недоступна.",
                        [[Button("Главное меню", callback="menu")]],
                        content_key="system.custom_page_missing",
                        content_title="Дополнительная страница недоступна",
                        content_category="system",
                    ),
                )
            return
        feature = feature_for_callback(callback)
        if feature and not self.storage.feature_enabled(feature):
            await self.send(
                event,
                OutgoingMessage(
                    "Этот раздел сейчас временно отключён.",
                    [[Button("Главное меню", callback="menu")]],
                    content_key="system.feature_disabled",
                    content_title="Раздел временно отключён",
                    content_category="system",
                ),
            )
            return
        if callback == "apply:start":
            await self.start_application(event)
        elif callback.startswith("apply:skip:"):
            await self.skip_application_step(event, callback.rsplit(":", 1)[1])
        elif callback == "apply:back":
            await self.application_back(event)
        elif callback == "apply:edit":
            conversation = self.storage.conversation(event.platform, event.user_id)
            data = conversation["data"] if conversation and conversation["flow"] == "application" else {}
            self.storage.set_conversation(event.platform, event.user_id, "application", "name", data)
            await self.send(event, application_prompt("name", data))
        elif callback == "apply:submit":
            await self.submit_application(event)
        elif callback.startswith("content:"):
            _, kind, offset = callback.split(":", 2)
            await self.show_content(event, kind, int(offset))
        elif callback == "city:menu":
            await self.show_cities(event)
        elif callback.startswith("city:set:"):
            await self.set_city(event, callback.split(":", 2)[2])
        elif callback == "requests:mine":
            await self.show_my_requests(event)
        elif callback == "request:link":
            self.storage.set_conversation(event.platform, event.user_id, "request_link", "number", {})
            await self.send(
                event,
                OutgoingMessage(
                    "<b>Привязать заявку с сайта</b>\n\nВведите номер заявки, например MP-260903-ABC123.",
                    [[Button("Отменить", callback="flow:cancel")]],
                ),
            )
        elif callback.startswith("request:view:"):
            await self.show_user_request(event, callback.rsplit(":", 1)[1])
        elif callback.startswith("request:add:"):
            await self.start_request_message(event, callback.rsplit(":", 1)[1], admin=False)
        elif callback.startswith("request:cancel-confirm:"):
            await self.confirm_cancel_request(event, callback.rsplit(":", 1)[1])
        elif callback.startswith("request:cancel:"):
            await self.cancel_request(event, callback.rsplit(":", 1)[1])
        elif callback == "about":
            await self.show_about(event)
        elif callback == "privacy:delete-confirm":
            await self.confirm_delete_data(event)
        elif callback == "privacy:delete":
            await self.delete_user_data(event)
        elif callback.startswith(("admin:", "req:", "admins:", "adm:", "broadcast:", "cms:")):
            await self.handle_admin_callback(event, callback)
        else:
            await self.send(
                event,
                OutgoingMessage(
                    "Эта кнопка устарела. Откройте актуальное меню.",
                    [[Button("Главное меню", callback="menu")]],
                    content_key="system.stale_button",
                    content_title="Устаревшая кнопка",
                    content_category="system",
                ),
            )

    async def start_application(self, event: IncomingEvent) -> None:
        user = self.storage.get_user(event.platform, event.user_id) or {}
        data = {
            "idempotency": f"{event.platform.value.lower()}:{event.user_id}:{secrets.token_hex(8)}",
            "campaign": user.get("referral") or "",
        }
        self.storage.set_conversation(event.platform, event.user_id, "application", "name", data)
        await self.send(event, application_prompt("name", data))

    async def skip_application_step(self, event: IncomingEvent, step: str) -> None:
        conversation = self.storage.conversation(event.platform, event.user_id)
        if not conversation or conversation["flow"] != "application" or conversation["step"] != step:
            await self.send(event, OutgoingMessage("Этот шаг уже завершён."))
            return
        data = conversation["data"]
        data[step] = ""
        await self.advance_application(event, step, data)

    async def application_back(self, event: IncomingEvent) -> None:
        conversation = self.storage.conversation(event.platform, event.user_id)
        if not conversation or conversation["flow"] != "application":
            await self.show_menu(event)
            return
        step = conversation["step"]
        if step == "review":
            previous = "message"
        else:
            index = _APPLICATION_STEPS.index(step) if step in _APPLICATION_STEPS else 0
            previous = _APPLICATION_STEPS[max(0, index - 1)]
        self.storage.set_conversation(
            event.platform, event.user_id, "application", previous, conversation["data"]
        )
        await self.send(event, application_prompt(previous, conversation["data"]))

    async def advance_application(self, event: IncomingEvent, step: str, data: dict[str, Any]) -> None:
        index = _APPLICATION_STEPS.index(step)
        if index + 1 < len(_APPLICATION_STEPS):
            next_step = _APPLICATION_STEPS[index + 1]
            self.storage.set_conversation(event.platform, event.user_id, "application", next_step, data)
            await self.send(event, application_prompt(next_step, data))
            return
        self.storage.set_conversation(event.platform, event.user_id, "application", "review", data)
        await self.send(event, application_review(data, self.settings.privacy_url))

    async def handle_application_input(self, event: IncomingEvent, conversation: dict[str, Any]) -> None:
        step = conversation["step"]
        if step == "review":
            await self.send(event, application_review(conversation["data"], self.settings.privacy_url))
            return
        value = event.phone or (event.text or "").strip()
        limits = {"name": 120, "company": 160, "position": 160, "phone": 40, "email": 254, "message": 4000}
        if not value:
            await self.send(
                event, OutgoingMessage("Введите значение или воспользуйтесь кнопкой под сообщением.")
            )
            return
        if len(value) > limits[step]:
            await self.send(
                event, OutgoingMessage(f"Слишком длинный текст. Максимум {limits[step]} символов.")
            )
            return
        if step == "name" and len(value) < 2:
            await self.send(event, OutgoingMessage("Имя должно содержать не менее двух символов."))
            return
        if step == "phone" and not _PHONE_RE.fullmatch(value):
            await self.send(
                event, OutgoingMessage("Не удалось распознать номер. Например: +7 999 123-45-67.")
            )
            return
        if step == "email" and not _EMAIL_RE.fullmatch(value.lower()):
            await self.send(event, OutgoingMessage("Проверьте email или нажмите «Пропустить»."))
            return
        data = conversation["data"]
        data[step] = value.lower() if step == "email" else value
        await self.advance_application(event, step, data)

    async def submit_application(self, event: IncomingEvent) -> None:
        conversation = self.storage.conversation(event.platform, event.user_id)
        if not conversation or conversation["flow"] != "application" or conversation["step"] != "review":
            await self.send(event, OutgoingMessage("Начните новую заявку из главного меню."))
            return
        data = conversation["data"]
        missing = [field for field in ("name", "phone", "idempotency") if not data.get(field)]
        if missing:
            await self.send(
                event, OutgoingMessage("В заявке не хватает обязательных данных. Вернитесь к редактированию.")
            )
            return
        try:
            possible_duplicates = await self.site.requests(q=data["phone"], limit=5, offset=0)
            result = await self.site.create_request(
                {
                    "name": data["name"],
                    "company": data.get("company", ""),
                    "position": data.get("position", ""),
                    "phone": data["phone"],
                    "email": data.get("email", ""),
                    "message": data.get("message", ""),
                    "source": event.platform.value,
                    "externalUserId": event.user_id,
                    "externalChatId": event.chat_id,
                    "externalRequestKey": data["idempotency"],
                    "campaign": data.get("campaign", ""),
                    "consentAt": datetime.now(UTC).isoformat(),
                }
            )
            item = result["request"]
            self.storage.link_request(item["id"], event.platform, event.user_id)
            if possible_duplicates.get("pagination", {}).get("total", 0):
                await self.site.add_activity(
                    item["id"],
                    {
                        "type": "COMMENT",
                        "body": "Система обнаружила возможную повторную заявку с тем же телефоном.",
                        "actor": {"name": "Бот"},
                    },
                )
        except SiteApiError as exc:
            await self.send(
                event,
                OutgoingMessage(
                    f"<b>Заявка пока не отправлена</b>\n\n{safe(exc)}",
                    [
                        [
                            Button("Повторить", callback="apply:submit"),
                            Button("Изменить", callback="apply:edit"),
                        ]
                    ],
                    content_key="application.submit_error",
                    content_title="Ошибка отправки заявки",
                    content_category="application",
                ),
            )
            return
        self.storage.clear_conversation(event.platform, event.user_id)
        await self.send(
            event,
            OutgoingMessage(
                f"<b>Заявка {safe(item['requestNumber'])} принята</b>\n\n"
                "Команда проекта увидит её в общей системе. Об изменении статуса мы сообщим здесь.",
                [
                    [Button("Посмотреть заявку", callback=f"request:view:{item['id']}")],
                    [Button("Главное меню", callback="menu")],
                ],
                remove_keyboard=True,
                content_key="application.submitted",
                content_title="Заявка успешно отправлена",
                content_category="application",
            ),
        )

    async def show_content(self, event: IncomingEvent, kind: str, offset: int = 0) -> None:
        # Old Telegram/MAX messages used content:interviews for this menu item.
        # Keep them useful while switching the source to the site's /videos page.
        if kind == "interviews":
            kind = "videos"
        if kind not in CONTENT_LABELS or kind == "all":
            kind = "latest"
        user = self.storage.get_user(event.platform, event.user_id) or {}
        try:
            result = await self.site.content(
                kind, limit=5, offset=max(0, offset), city=user.get("city_slug") or ""
            )
        except SiteApiError as exc:
            await self.send(
                event,
                OutgoingMessage(
                    f"Не удалось загрузить материалы: {safe(exc)}",
                    [[Button("Повторить", callback=f"content:{kind}:{offset}")]],
                    content_key="catalog.load_error",
                    content_title="Ошибка загрузки материалов",
                    content_category="catalog",
                ),
            )
            return
        self.storage.cache_content(result["items"])
        await self.send(
            event,
            content_list(
                kind,
                result["items"],
                self.settings.site_public_url,
                max(0, offset),
                bool(result["pagination"]["hasMore"]),
            ),
        )

    async def show_cities(self, event: IncomingEvent) -> None:
        try:
            cities = await self.site.cities()
        except SiteApiError as exc:
            await self.send(
                event,
                OutgoingMessage(
                    f"Не удалось загрузить города: {safe(exc)}",
                    content_key="cities.load_error",
                    content_title="Ошибка загрузки городов",
                    content_category="catalog",
                ),
            )
            return
        buttons = [[Button("Все города", callback="city:set:all")]]
        buttons.extend(
            [[Button(str(city["name"]), callback=f"city:set:{city['slug']}")] for city in cities[:24]]
        )
        buttons.append([Button("Главное меню", callback="menu")])
        await self.send(
            event,
            OutgoingMessage(
                "<b>Выберите город</b>\n\nБудем показывать материалы, связанные с выбранным городом.",
                buttons,
                content_key="cities.menu",
                content_title="Выбор города",
                content_category="catalog",
            ),
        )

    async def set_city(self, event: IncomingEvent, slug: str) -> None:
        if slug == "all":
            self.storage.set_city(event.platform, event.user_id, None, None)
        else:
            cities = await self.site.cities()
            selected = next((city for city in cities if city["slug"] == slug), None)
            if not selected:
                await self.send(event, OutgoingMessage("Город не найден. Откройте список заново."))
                return
            self.storage.set_city(event.platform, event.user_id, slug, selected["name"])
        await self.show_menu(event)

    async def show_my_requests(self, event: IncomingEvent) -> None:
        try:
            result = await self.site.requests(
                externalPlatform=event.platform.value,
                externalUserId=event.user_id,
                limit=20,
                offset=0,
            )
        except SiteApiError as exc:
            await self.send(event, OutgoingMessage(f"Не удалось загрузить заявки: {safe(exc)}"))
            return
        items = result["items"]
        if not items:
            await self.send(
                event,
                OutgoingMessage(
                    "<b>Мои заявки</b>\n\nУ вас пока нет заявок.",
                    [
                        [Button("Стать героем", callback="apply:start")],
                        [Button("Привязать заявку с сайта", callback="request:link")],
                        [Button("Главное меню", callback="menu")],
                    ],
                    content_key="requests.empty",
                    content_title="У пользователя нет заявок",
                    content_category="requests",
                ),
            )
            return
        lines = ["<b>Мои заявки</b>", ""]
        buttons = []
        for item in items:
            lines.append(
                f"{safe(item['requestNumber'])} — <b>{safe(STATUS_LABELS.get(item['status'], item['status']))}</b>"
            )
            buttons.append([Button(str(item["requestNumber"]), callback=f"request:view:{item['id']}")])
        buttons.append([Button("Привязать заявку с сайта", callback="request:link")])
        buttons.append([Button("Главное меню", callback="menu")])
        await self.send(
            event,
            OutgoingMessage(
                "\n".join(lines),
                buttons,
                content_key="requests.list",
                content_title="Мои заявки",
                content_category="requests",
            ),
        )

    async def _owned_request(self, event: IncomingEvent, request_id: str) -> dict[str, Any] | None:
        try:
            item = await self.site.request(request_id)
        except SiteApiError:
            return None
        return (
            item
            if item.get("externalPlatform") == event.platform.value
            and str(item.get("externalUserId")) == event.user_id
            else None
        )

    async def show_user_request(self, event: IncomingEvent, request_id: str) -> None:
        item = await self._owned_request(event, request_id)
        if not item:
            await self.send(event, OutgoingMessage("Заявка не найдена или принадлежит другому пользователю."))
            return
        await self.send(event, request_card(item))

    async def start_request_message(self, event: IncomingEvent, request_id: str, *, admin: bool) -> None:
        if not admin and not await self._owned_request(event, request_id):
            await self.send(event, OutgoingMessage("Нет доступа к этой заявке."))
            return
        flow = "admin_message" if admin else "request_message"
        self.storage.set_conversation(event.platform, event.user_id, flow, "text", {"request_id": request_id})
        prompt = (
            "Введите сообщение заявителю."
            if admin
            else "Введите дополнительную информацию для команды проекта."
        )
        await self.send(
            event,
            OutgoingMessage(
                f"<b>{prompt}</b>\n\nДо 4000 символов.", [[Button("Отменить", callback="flow:cancel")]]
            ),
        )

    async def confirm_cancel_request(self, event: IncomingEvent, request_id: str) -> None:
        if not await self._owned_request(event, request_id):
            await self.send(event, OutgoingMessage("Заявка не найдена или больше недоступна."))
            return
        await self.send(
            event,
            OutgoingMessage(
                "Отменить заявку? После этого восстановить её через бота не получится.",
                [
                    [Button("Да, отменить", callback=f"request:cancel:{request_id}")],
                    [Button("Не отменять", callback=f"request:view:{request_id}")],
                ],
                content_key="requests.cancel_confirm",
                content_title="Подтверждение отмены заявки",
                content_category="requests",
            ),
        )

    async def cancel_request(self, event: IncomingEvent, request_id: str) -> None:
        try:
            await self.site.cancel_request(request_id, event.platform.value, event.user_id)
        except SiteApiError as exc:
            await self.send(event, OutgoingMessage(f"Не удалось отменить заявку: {safe(exc)}"))
            return
        await self.send(
            event,
            OutgoingMessage(
                "Заявка отменена и перемещена в архив.",
                [[Button("Мои заявки", callback="requests:mine")]],
                content_key="requests.cancelled",
                content_title="Заявка отменена",
                content_category="requests",
            ),
        )

    async def show_about(self, event: IncomingEvent) -> None:
        await self.send(
            event,
            OutgoingMessage(
                "<b>О проекте</b>\n\n«МАРШРУТ ПОСТРОЕН» — медиагид о брендах, компаниях, местах и проектах, которые создают предприниматели России.",
                [
                    [Button("Открыть сайт", url=self.settings.site_public_url)],
                    [Button("Политика конфиденциальности", url=self.settings.privacy_url)],
                    [Button("Удалить мои данные", callback="privacy:delete-confirm")],
                    [Button("Главное меню", callback="menu")],
                ],
                content_key="privacy.about",
                content_title="О проекте",
                content_category="privacy",
            ),
        )

    async def confirm_delete_data(self, event: IncomingEvent) -> None:
        await self.send(
            event,
            OutgoingMessage(
                "<b>Удаление данных</b>\n\nЗаявки будут обезличены и перенесены в архив. История диалога и локальные данные пользователя будут удалены.",
                [
                    [Button("Удалить мои данные", callback="privacy:delete")],
                    [Button("Отмена", callback="menu")],
                ],
                content_key="privacy.delete_confirm",
                content_title="Подтверждение удаления данных",
                content_category="privacy",
            ),
        )

    async def delete_user_data(self, event: IncomingEvent) -> None:
        if self.storage.admin_for(event.platform, event.user_id):
            await self.send(
                event,
                OutgoingMessage(
                    "Сначала другой администратор должен отключить вашу административную учётную запись."
                ),
            )
            return
        try:
            await self.site.delete_user_data(event.platform.value, event.user_id)
        except SiteApiError as exc:
            await self.send(event, OutgoingMessage(f"Не удалось удалить данные: {safe(exc)}"))
            return
        self.storage.delete_user_data(event.platform, event.user_id)
        await self.send(
            event,
            OutgoingMessage(
                "Ваши данные удалены. Если захотите вернуться, перейдите в главное меню.",
                content_key="privacy.deleted",
                content_title="Данные пользователя удалены",
                content_category="privacy",
            ),
        )

    async def handle_conversation(self, event: IncomingEvent, conversation: dict[str, Any]) -> None:
        flow = conversation["flow"]
        if flow == "application":
            await self.handle_application_input(event, conversation)
        elif flow in {"request_message", "reply_admin"}:
            await self.handle_user_request_message(event, conversation)
        elif flow == "request_link":
            await self.handle_request_link(event, conversation)
        elif flow in {"admin_comment", "admin_message", "admin_search"}:
            await self.handle_admin_conversation(event, conversation)
        elif flow.startswith("cms_"):
            await self.handle_cms_conversation(event, conversation)
        else:
            self.storage.clear_conversation(event.platform, event.user_id)
            await self.show_menu(event)

    async def handle_request_link(self, event: IncomingEvent, conversation: dict[str, Any]) -> None:
        value = event.phone or (event.text or "").strip()
        if conversation["step"] == "number":
            request_number = value.upper()
            if not re.fullmatch(r"MP-[A-Z0-9-]{6,32}", request_number):
                await self.send(event, OutgoingMessage("Проверьте номер заявки. Он начинается с MP-."))
                return
            data = {"request_number": request_number}
            self.storage.set_conversation(event.platform, event.user_id, "request_link", "phone", data)
            await self.send(
                event,
                OutgoingMessage(
                    "Теперь укажите телефон из заявки.",
                    [
                        [Button("Поделиться контактом", kind="request_contact")],
                        [Button("Отменить", callback="flow:cancel")],
                    ],
                ),
            )
            return
        if not _PHONE_RE.fullmatch(value):
            await self.send(event, OutgoingMessage("Проверьте номер телефона и попробуйте снова."))
            return
        try:
            item = await self.site.link_request(
                {
                    "requestNumber": conversation["data"]["request_number"],
                    "phone": value,
                    "platform": event.platform.value,
                    "externalUserId": event.user_id,
                    "externalChatId": event.chat_id,
                }
            )
        except SiteApiError:
            await self.send(
                event,
                OutgoingMessage(
                    "Номер заявки и телефон не совпали либо заявка уже привязана к другому аккаунту.",
                    [[Button("Начать заново", callback="request:link"), Button("Отмена", callback="menu")]],
                ),
            )
            return
        self.storage.link_request(item["id"], event.platform, event.user_id)
        self.storage.clear_conversation(event.platform, event.user_id)
        await self.send(
            event,
            OutgoingMessage(
                f"Заявка <b>{safe(item['requestNumber'])}</b> привязана. Теперь её статус доступен в боте.",
                [[Button("Открыть заявку", callback=f"request:view:{item['id']}")]],
                remove_keyboard=True,
            ),
        )

    async def handle_user_request_message(self, event: IncomingEvent, conversation: dict[str, Any]) -> None:
        text = (event.text or "").strip()
        if not text or len(text) > 4000:
            await self.send(event, OutgoingMessage("Введите сообщение длиной от 1 до 4000 символов."))
            return
        request_id = conversation["data"]["request_id"]
        if not await self._owned_request(event, request_id):
            self.storage.clear_conversation(event.platform, event.user_id)
            await self.send(event, OutgoingMessage("Заявка больше недоступна."))
            return
        await self.site.add_activity(
            request_id,
            {
                "type": "MESSAGE_FROM_USER",
                "body": text,
                "actor": {"key": event.user_id, "name": event.display_name},
            },
        )
        self.storage.clear_conversation(event.platform, event.user_id)
        await self.send(
            event,
            OutgoingMessage(
                "Сообщение отправлено команде проекта.",
                [[Button("Открыть заявку", callback=f"request:view:{request_id}")]],
            ),
        )

    async def show_admin_home(self, event: IncomingEvent) -> None:
        admin = self.storage.admin_for(event.platform, event.user_id)
        if not admin:
            await self.send(
                event,
                OutgoingMessage(
                    "У вас нет доступа к панели администратора.",
                    content_key="admin.denied",
                    content_title="Нет доступа в панель",
                    content_category="admin",
                ),
            )
            return
        try:
            results = await asyncio.gather(
                *(self.site.requests(status=status, limit=1, offset=0) for status in STATUS_LABELS)
            )
            counts = {
                status: result["pagination"]["total"]
                for status, result in zip(STATUS_LABELS, results, strict=True)
            }
        except SiteApiError as exc:
            await self.send(
                event,
                OutgoingMessage(
                    f"Не удалось загрузить панель: {safe(exc)}",
                    content_key="admin.load_error",
                    content_title="Ошибка загрузки панели",
                    content_category="admin",
                ),
            )
            return
        await self.send(event, admin_menu(counts))

    async def handle_admin_callback(self, event: IncomingEvent, callback: str) -> None:
        admin = self.storage.admin_for(event.platform, event.user_id)
        if not admin:
            await self.send(
                event,
                OutgoingMessage(
                    "Доступ администратора не найден.",
                    content_key="admin.account_missing",
                    content_title="Учётная запись администратора не найдена",
                    content_category="admin",
                ),
            )
            return
        try:
            if callback == "cms:home":
                await self.show_cms_home(event, admin)
            elif callback == "cms:features":
                await self.show_cms_features(event, admin)
            elif callback.startswith("cms:feature:"):
                await self.toggle_cms_feature(event, admin, callback.rsplit(":", 1)[1])
            elif callback.startswith("cms:list:"):
                _, _, category, offset = callback.split(":", 3)
                await self.show_cms_entries(event, admin, category, int(offset))
            elif callback.startswith("cms:view:"):
                await self.show_cms_entry(event, admin, int(callback.rsplit(":", 1)[1]))
            elif callback.startswith("cms:preview:"):
                await self.preview_cms_entry(event, admin, int(callback.rsplit(":", 1)[1]))
            elif callback.startswith("cms:text:"):
                await self.show_cms_text_menu(event, admin, int(callback.rsplit(":", 1)[1]))
            elif callback.startswith("cms:text-replace:"):
                await self.start_cms_text(event, admin, int(callback.rsplit(":", 1)[1]), "replace")
            elif callback.startswith("cms:text-before:"):
                await self.start_cms_text(event, admin, int(callback.rsplit(":", 1)[1]), "before")
            elif callback.startswith("cms:text-after:"):
                await self.start_cms_text(event, admin, int(callback.rsplit(":", 1)[1]), "after")
            elif callback.startswith("cms:images:"):
                await self.start_cms_images(event, admin, int(callback.rsplit(":", 1)[1]))
            elif callback.startswith("cms:images-done:"):
                content_id = int(callback.rsplit(":", 1)[1])
                self.storage.clear_conversation(event.platform, event.user_id)
                await self.show_cms_entry(event, admin, content_id, notice="Картинки сохранены")
            elif callback.startswith("cms:images-clear:"):
                content_id = int(callback.rsplit(":", 1)[1])
                entry = self.storage.content_entry(content_id)
                previous_images = self._cms_entry_images(entry)
                self.storage.set_content_images(content_id, [])
                self._delete_managed_cms_images(previous_images)
                self.storage.clear_conversation(event.platform, event.user_id)
                self.storage.audit(
                    admin["id"], "bot_content.images.changed", "bot_content", str(content_id), {"count": 0}
                )
                await self.show_cms_entry(event, admin, content_id, notice="Все картинки удалены")
            elif callback.startswith("cms:edit-back:"):
                content_id = int(callback.rsplit(":", 1)[1])
                self.storage.clear_conversation(event.platform, event.user_id)
                await self.show_cms_entry(event, admin, content_id)
            elif callback.startswith("cms:reset-text:"):
                content_id = int(callback.rsplit(":", 1)[1])
                self.storage.set_content_text(content_id, None)
                self.storage.clear_conversation(event.platform, event.user_id)
                self.storage.audit(admin["id"], "bot_content.text.reset", "bot_content", str(content_id))
                await self.show_cms_entry(event, admin, content_id, notice="Стандартный текст восстановлен")
            elif callback.startswith("cms:buttons:"):
                _, _, content_id, offset = callback.split(":", 3)
                await self.show_cms_buttons(event, admin, int(content_id), int(offset))
            elif callback.startswith("cms:button:"):
                await self.show_cms_button(event, admin, int(callback.rsplit(":", 1)[1]))
            elif callback.startswith("cms:button-text:"):
                await self.start_cms_button_text(event, admin, int(callback.rsplit(":", 1)[1]))
            elif callback.startswith("cms:button-reset:"):
                button_id = int(callback.rsplit(":", 1)[1])
                self.storage.set_content_button_text(button_id, None)
                self.storage.clear_conversation(event.platform, event.user_id)
                self.storage.audit(admin["id"], "bot_content.button.renamed", "bot_button", str(button_id))
                await self.show_cms_button(event, admin, button_id, notice="Название восстановлено")
            elif callback.startswith("cms:button-back:"):
                button_id = int(callback.rsplit(":", 1)[1])
                self.storage.clear_conversation(event.platform, event.user_id)
                await self.show_cms_button(event, admin, button_id)
            elif callback.startswith("cms:button-toggle:"):
                button_id = int(callback.rsplit(":", 1)[1])
                button = self.storage.content_button(button_id)
                if not button:
                    raise ValueError("Кнопка не найдена")
                self.storage.toggle_content_button(button_id)
                self.storage.audit(admin["id"], "bot_content.button.toggled", "bot_button", str(button_id))
                await self.show_cms_button(event, admin, button_id)
            elif callback.startswith("cms:button-delete:"):
                button_id = int(callback.rsplit(":", 1)[1])
                button = self.storage.content_button(button_id)
                if not button or not button["is_custom"]:
                    raise ValueError("Дополнительная кнопка не найдена")
                parent_id = int(button["content_id"])
                self.storage.delete_custom_button(button_id)
                self.storage.audit(admin["id"], "bot_content.button.deleted", "bot_button", str(button_id))
                await self.show_cms_entry(event, admin, parent_id)
            elif callback.startswith("cms:add:"):
                await self.start_cms_button(event, admin, int(callback.rsplit(":", 1)[1]))
            elif callback.startswith("admin:requests:"):
                _, _, filter_value, offset = callback.split(":", 3)
                await self.show_admin_requests(event, admin, filter_value, int(offset))
            elif callback == "admin:search":
                self.storage.set_conversation(event.platform, event.user_id, "admin_search", "query", {})
                await self.send(
                    event,
                    OutgoingMessage(
                        "<b>Поиск заявок</b>\n\nВведите номер заявки, имя, компанию, телефон или email.",
                        [[Button("Отменить", callback="flow:cancel")]],
                    ),
                )
            elif callback.startswith("req:view:"):
                await self.show_admin_request(event, callback.rsplit(":", 1)[1])
            elif callback.startswith("req:take:"):
                await self.take_request(event, admin, callback.rsplit(":", 1)[1])
            elif callback.startswith("req:assign-set:"):
                _, _, request_id, admin_prefix = callback.split(":", 3)
                await self.assign_request(event, admin, request_id, admin_prefix)
            elif callback.startswith("req:assign:"):
                await self.show_assignment_choices(event, admin, callback.rsplit(":", 1)[1])
            elif callback.startswith("req:status-set:"):
                _, _, request_id, status = callback.split(":", 3)
                await self.set_request_status(event, admin, request_id, status)
            elif callback.startswith("req:status:"):
                await self.show_status_choices(event, callback.rsplit(":", 1)[1])
            elif callback.startswith("req:comment:"):
                await self.start_admin_text(event, callback.rsplit(":", 1)[1], "admin_comment")
            elif callback.startswith("req:message:"):
                await self.start_admin_text(event, callback.rsplit(":", 1)[1], "admin_message")
            elif callback.startswith("req:remind-set:"):
                _, _, request_id, preset = callback.split(":", 3)
                await self.set_reminder(event, admin, request_id, preset)
            elif callback.startswith("req:remind:"):
                await self.show_reminder_choices(event, callback.rsplit(":", 1)[1])
            elif callback.startswith("req:history:"):
                await self.show_request_history(event, callback.rsplit(":", 1)[1])
            elif callback.startswith("req:block-confirm:"):
                await self.confirm_block_sender(event, admin, callback.rsplit(":", 1)[1])
            elif callback.startswith("req:block:"):
                await self.block_sender(event, admin, callback.rsplit(":", 1)[1])
            elif callback == "admins:list":
                await self.show_admins(event, admin)
            elif callback.startswith("adm:add:"):
                await self.create_admin_invite(event, admin, callback.rsplit(":", 1)[1])
            elif callback.startswith("adm:view:"):
                await self.show_admin_detail(event, admin, callback.rsplit(":", 1)[1])
            elif callback.startswith("adm:toggle:"):
                await self.toggle_admin(event, admin, callback.rsplit(":", 1)[1])
            elif callback.startswith("adm:link:"):
                await self.create_link_invite(event, admin, callback.rsplit(":", 1)[1])
            elif callback == "admin:preferences":
                await self.show_preferences(event, admin)
            elif callback.startswith("admin:pref:"):
                self.storage.toggle_admin_preference(
                    event.platform, event.user_id, callback.rsplit(":", 1)[1]
                )
                await self.show_preferences(
                    event, self.storage.admin_for(event.platform, event.user_id) or admin
                )
            elif callback == "admin:system":
                await self.show_system(event, admin)
            elif callback == "admin:blocked":
                await self.show_blocked_users(event, admin)
            elif callback.startswith("admin:unblock:"):
                _, _, platform, user_id = callback.split(":", 3)
                await self.unblock_user(event, admin, platform, user_id)
            elif callback == "admin:export":
                await self.export_requests(event, admin)
            elif callback == "admin:audit":
                await self.show_audit(event, admin)
            elif callback == "broadcast:menu":
                await self.show_broadcast_menu(event, admin)
            elif callback.startswith("broadcast:list:"):
                await self.show_broadcast_items(event, admin, callback.rsplit(":", 1)[1])
            elif callback.startswith("broadcast:preview:"):
                _, _, kind, item_id = callback.split(":", 3)
                await self.preview_broadcast(event, admin, kind, item_id)
            elif callback.startswith("broadcast:send:"):
                _, _, kind, item_id = callback.split(":", 3)
                await self.send_broadcast(event, admin, kind, item_id)
            else:
                await self.send(
                    event,
                    OutgoingMessage(
                        "Команда устарела. Вернитесь в панель администратора.",
                        [[Button("Панель", callback="admin:home")]],
                    ),
                )
        except (SiteApiError, ValueError) as exc:
            await self.send(
                event,
                OutgoingMessage(
                    f"Не удалось выполнить действие: {safe(exc)}",
                    [[Button("Панель", callback="admin:home")]],
                    content_key="admin.action_error",
                    content_title="Ошибка действия администратора",
                    content_category="admin",
                ),
            )

    @staticmethod
    def _can_mutate(admin: dict[str, Any]) -> bool:
        return admin["role"] == AdminRole.ADMIN.value

    def _require_content_editor(self, admin: dict[str, Any]) -> None:
        if not self._can_mutate(admin):
            raise ValueError("Редактор доступен администраторам")

    async def show_cms_home(self, event: IncomingEvent, admin: dict[str, Any]) -> None:
        self._require_content_editor(admin)
        counts = {row["category"]: int(row["count"]) for row in self.storage.content_categories()}
        buttons = [
            [
                Button(
                    f"{title} · {counts.get(category, 0)}",
                    callback=f"cms:list:{category}:0",
                )
            ]
            for category, title in CATEGORY_LABELS.items()
            if counts.get(category, 0)
        ]
        buttons.extend(
            [
                [Button("Включение возможностей", callback="cms:features")],
                [Button("Панель администратора", callback="admin:home")],
            ]
        )
        await self.send(
            event,
            OutgoingMessage(
                "<b>Редактор сообщений</b>\n\n"
                "Здесь можно изменить сообщения и картинки, которые видят пользователи.\n\n"
                "Выберите нужный раздел:",
                buttons,
                content_key="admin.content.home",
                content_title="Редактор контента",
                content_category="admin",
            ),
        )

    async def show_cms_entries(
        self,
        event: IncomingEvent,
        admin: dict[str, Any],
        category: str,
        offset: int,
    ) -> None:
        self._require_content_editor(admin)
        if category not in CATEGORY_LABELS:
            raise ValueError("Неизвестный раздел")
        page_size = 8
        offset = max(0, offset)
        entries = self.storage.content_entries(category, offset, page_size + 1)
        has_more = len(entries) > page_size
        entries = entries[:page_size]
        buttons = [
            [
                Button(
                    f"{'✏️ ' if item['text_override'] is not None else ''}"
                    f"{'🖼 ' if json.loads(item['images_json']) else ''}{item['title'][:46]}",
                    callback=f"cms:view:{item['id']}",
                )
            ]
            for item in entries
        ]
        navigation: list[Button] = []
        if offset:
            navigation.append(Button("Назад", callback=f"cms:list:{category}:{max(0, offset - page_size)}"))
        if has_more:
            navigation.append(Button("Далее", callback=f"cms:list:{category}:{offset + page_size}"))
        if navigation:
            buttons.append(navigation)
        buttons.append([Button("К разделам", callback="cms:home")])
        await self.send(
            event,
            OutgoingMessage(
                f"<b>{safe(CATEGORY_LABELS[category])}</b>\n\n"
                "Выберите сообщение. Значки ✏️ и 🖼 показывают, что оно уже изменено.",
                buttons,
                content_key="admin.content.list",
                content_title="Список редактируемых ответов",
                content_category="admin",
            ),
        )

    async def show_cms_entry(
        self,
        event: IncomingEvent,
        admin: dict[str, Any],
        content_id: int,
        *,
        notice: str | None = None,
    ) -> None:
        self._require_content_editor(admin)
        entry = self.storage.content_entry(content_id)
        if not entry:
            raise ValueError("Ответ не найден")
        try:
            images = json.loads(entry["images_json"])
        except (TypeError, ValueError):
            images = []
        current_text = entry["text_override"]
        if current_text is None:
            text_preview = entry["default_text"]
        else:
            text_preview = (
                str(current_text)
                .replace("{{default}}", str(entry["default_text"]))
                .replace("{default}", str(entry["default_text"]))
            )
        text_preview = str(text_preview or "Ответ ещё не показывался пользователям.")
        lines = []
        if notice:
            lines.extend([f"✅ <b>{safe(notice)}</b>", ""])
        lines.extend(
            [
                f"<b>{safe(entry['title'])}</b>",
                "",
                "<b>Сейчас пользователь увидит:</b>",
                safe(plain_text(text_preview)[:1100]),
                "",
                f"Картинки: <b>{len(images) if images else 'нет'}</b>",
            ]
        )
        content_buttons = entry["buttons"]
        buttons = [
            [Button("✏️ Изменить текст", callback=f"cms:text:{content_id}")],
            [
                Button(
                    "🖼 Добавить картинку" if not images else f"🖼 Картинки ({len(images)})",
                    callback=f"cms:images:{content_id}",
                )
            ],
            [Button("👀 Показать как пользователю", callback=f"cms:preview:{content_id}")],
            [Button(f"⚙️ Настроить кнопки ({len(content_buttons)})", callback=f"cms:buttons:{content_id}:0")],
        ]
        if current_text is not None:
            buttons.append([Button("↩️ Вернуть стандартный текст", callback=f"cms:reset-text:{content_id}")])
        buttons.append([Button("Назад к списку", callback=f"cms:list:{entry['category']}:0")])
        await self.send(
            event,
            OutgoingMessage(
                "\n".join(lines),
                buttons,
                content_key="admin.content.detail",
                content_title="Карточка редактируемого ответа",
                content_category="admin",
            ),
        )

    async def show_cms_buttons(
        self,
        event: IncomingEvent,
        admin: dict[str, Any],
        content_id: int,
        offset: int,
    ) -> None:
        self._require_content_editor(admin)
        entry = self.storage.content_entry(content_id)
        if not entry:
            raise ValueError("Ответ не найден")
        page_size = 12
        offset = max(0, offset)
        content_buttons = entry["buttons"]
        current = content_buttons[offset : offset + page_size]
        buttons = []
        for item in current:
            state = "✅" if item["is_visible"] else "🚫"
            label = str(item.get("text_override") or item["default_text"])
            buttons.append([Button(f"{state} {label[:42]}", callback=f"cms:button:{item['id']}")])
        navigation: list[Button] = []
        if offset:
            navigation.append(
                Button("Назад", callback=f"cms:buttons:{content_id}:{max(0, offset - page_size)}")
            )
        if offset + page_size < len(content_buttons):
            navigation.append(Button("Далее", callback=f"cms:buttons:{content_id}:{offset + page_size}"))
        if navigation:
            buttons.append(navigation)
        buttons.append([Button("Добавить новую кнопку", callback=f"cms:add:{content_id}")])
        buttons.append([Button("К ответу", callback=f"cms:view:{content_id}")])
        await self.send(
            event,
            OutgoingMessage(
                f"<b>Кнопки ответа «{safe(entry['title'])}»</b>\n\n"
                f"Показаны {offset + 1 if current else 0}–{offset + len(current)} из {len(content_buttons)}.",
                buttons,
                content_key="admin.content.buttons",
                content_title="Список кнопок ответа",
                content_category="admin",
            ),
        )

    async def preview_cms_entry(self, event: IncomingEvent, admin: dict[str, Any], content_id: int) -> None:
        self._require_content_editor(admin)
        message = self.storage.content_preview_message(content_id)
        if not message:
            raise ValueError("Ответ не найден")
        _ensure_home_navigation(message)
        failed_images = await self.messengers[event.platform].send(event.chat_id, message)
        if failed_images:
            result_text = (
                "Текст показан выше, но картинки не отправились. "
                "Удалите их, добавьте заново и повторите предпросмотр."
            )
        else:
            result_text = "Предпросмотр показан выше."
        await self.send(
            event,
            OutgoingMessage(
                result_text,
                [[Button("Вернуться к редактированию", callback=f"cms:view:{content_id}")]],
                content_key="admin.content.preview_return",
                content_title="Возврат из предпросмотра",
                content_category="admin",
            ),
        )

    async def show_cms_text_menu(self, event: IncomingEvent, admin: dict[str, Any], content_id: int) -> None:
        self._require_content_editor(admin)
        entry = self.storage.content_entry(content_id)
        if not entry:
            raise ValueError("Ответ не найден")
        buttons = [
            [Button("Заменить весь текст", callback=f"cms:text-replace:{content_id}")],
            [Button("Добавить текст в начало", callback=f"cms:text-before:{content_id}")],
            [Button("Добавить текст в конец", callback=f"cms:text-after:{content_id}")],
        ]
        if entry["text_override"] is not None:
            buttons.append([Button("Вернуть стандартный текст", callback=f"cms:reset-text:{content_id}")])
        buttons.append([Button("Назад", callback=f"cms:view:{content_id}")])
        await self.send(
            event,
            OutgoingMessage(
                f"<b>Изменить текст</b>\n\nСообщение: «{safe(entry['title'])}»\n\nЧто вы хотите сделать?",
                buttons,
                content_key="admin.content.edit_text",
                content_title="Выбор изменения текста",
                content_category="admin",
            ),
        )

    async def start_cms_text(
        self,
        event: IncomingEvent,
        admin: dict[str, Any],
        content_id: int,
        mode: str,
    ) -> None:
        self._require_content_editor(admin)
        if not self.storage.content_entry(content_id) or mode not in {"replace", "before", "after"}:
            raise ValueError("Ответ не найден")
        self.storage.set_conversation(
            event.platform, event.user_id, f"cms_text_{mode}", "value", {"content_id": content_id}
        )
        prompts = {
            "replace": (
                "<b>Новый текст</b>\n\n"
                "Напишите и отправьте новый текст сообщения. Он полностью заменит текущий."
            ),
            "before": (
                "<b>Текст в начале</b>\n\n"
                "Напишите, что нужно добавить перед основным сообщением. "
                "Имена, номера заявок и статусы продолжат обновляться автоматически."
            ),
            "after": (
                "<b>Текст в конце</b>\n\n"
                "Напишите, что нужно добавить после основного сообщения. "
                "Имена, номера заявок и статусы продолжат обновляться автоматически."
            ),
        }
        await self.send(
            event,
            OutgoingMessage(
                prompts[mode],
                [[Button("Отмена и назад", callback=f"cms:edit-back:{content_id}")]],
                content_key=f"admin.content.edit_text_{mode}",
                content_title="Ввод нового текста",
                content_category="admin",
            ),
        )

    async def start_cms_images(self, event: IncomingEvent, admin: dict[str, Any], content_id: int) -> None:
        self._require_content_editor(admin)
        entry = self.storage.content_entry(content_id)
        if not entry:
            raise ValueError("Ответ не найден")
        self.storage.set_conversation(
            event.platform, event.user_id, "cms_images", "value", {"content_id": content_id}
        )
        await self.send(
            event,
            OutgoingMessage(
                "<b>Картинки сообщения</b>\n\n"
                "Отправьте картинку сюда как обычное фото — она сразу добавится к сообщению. "
                "Можно добавить до 10 картинок по одной.",
                [
                    [Button("Готово, вернуться", callback=f"cms:images-done:{content_id}")],
                    *(
                        [[Button("Удалить все картинки", callback=f"cms:images-clear:{content_id}")]]
                        if json.loads(entry["images_json"])
                        else []
                    ),
                ],
                content_key="admin.content.edit_images",
                content_title="Добавление картинок",
                content_category="admin",
            ),
        )

    async def show_cms_button(
        self,
        event: IncomingEvent,
        admin: dict[str, Any],
        button_id: int,
        *,
        notice: str | None = None,
    ) -> None:
        self._require_content_editor(admin)
        button = self.storage.content_button(button_id)
        if not button:
            raise ValueError("Кнопка не найдена")
        label = str(button.get("text_override") or button["default_text"])
        buttons = [
            [Button("Переименовать", callback=f"cms:button-text:{button_id}")],
            [
                Button(
                    "Скрыть кнопку" if button["is_visible"] else "Показывать кнопку",
                    callback=f"cms:button-toggle:{button_id}",
                )
            ],
        ]
        if button.get("text_override"):
            buttons.insert(
                1,
                [Button("Вернуть стандартное название", callback=f"cms:button-reset:{button_id}")],
            )
        if button["is_custom"]:
            buttons.append(
                [
                    Button(
                        "Редактировать сообщение кнопки",
                        callback=f"cms:view:{button['target_content_id']}",
                    )
                ]
            )
            buttons.append([Button("Удалить кнопку и сообщение", callback=f"cms:button-delete:{button_id}")])
        buttons.append([Button("Назад к ответу", callback=f"cms:view:{button['content_id']}")])
        await self.send(
            event,
            OutgoingMessage(
                f"{'✅ ' + safe(notice) + chr(10) + chr(10) if notice else ''}"
                f"<b>Кнопка «{safe(label)}»</b>\n\n"
                f"Сообщение: {safe(button['content_title'])}\n"
                f"Сейчас кнопка <b>{'показывается' if button['is_visible'] else 'скрыта'}</b>.",
                buttons,
                content_key="admin.content.edit_button",
                content_title="Редактор кнопки",
                content_category="admin",
            ),
        )

    async def start_cms_button_text(
        self, event: IncomingEvent, admin: dict[str, Any], button_id: int
    ) -> None:
        self._require_content_editor(admin)
        if not self.storage.content_button(button_id):
            raise ValueError("Кнопка не найдена")
        self.storage.set_conversation(
            event.platform, event.user_id, "cms_button_text", "value", {"button_id": button_id}
        )
        await self.send(
            event,
            OutgoingMessage(
                "<b>Новое название кнопки</b>\n\nНапишите и отправьте новое название.",
                [[Button("Отмена и назад", callback=f"cms:button-back:{button_id}")]],
                content_key="admin.content.edit_button_text",
                content_title="Подсказка переименования кнопки",
                content_category="admin",
            ),
        )

    async def start_cms_button(self, event: IncomingEvent, admin: dict[str, Any], content_id: int) -> None:
        self._require_content_editor(admin)
        if not self.storage.content_entry(content_id):
            raise ValueError("Ответ не найден")
        self.storage.set_conversation(
            event.platform, event.user_id, "cms_button_label", "value", {"content_id": content_id}
        )
        await self.send(
            event,
            OutgoingMessage(
                "<b>Новая кнопка</b>\n\nСначала отправьте название кнопки (до 64 символов).",
                [[Button("Отмена", callback="flow:cancel")]],
                content_key="admin.content.add_button_label",
                content_title="Новая кнопка: название",
                content_category="admin",
            ),
        )

    async def _save_incoming_cms_image(self, event: IncomingEvent) -> str | None:
        messenger = self.messengers.get(event.platform)
        if not messenger:
            return None
        try:
            downloaded = await messenger.download_image(event)
        except Exception as exc:
            logger.warning("Could not download an administrator image: %s", type(exc).__name__)
            return None
        if not downloaded:
            return None
        payload, _content_type = downloaded
        suffix = _image_suffix(payload)
        if not suffix or not payload or len(payload) > _CMS_IMAGE_LIMIT:
            raise ValueError("Подойдёт картинка JPG, PNG, GIF или WEBP размером до 10 МБ")
        self.cms_media_dir.mkdir(parents=True, exist_ok=True)
        filename = f"{secrets.token_hex(16)}{suffix}"
        (self.cms_media_dir / filename).write_bytes(payload)
        return f"{self.settings.public_base_url}/cms-media/{filename}"

    @staticmethod
    def _cms_entry_images(entry: dict[str, Any] | None) -> list[str]:
        if not entry:
            return []
        try:
            return [str(url) for url in json.loads(entry["images_json"])]
        except (KeyError, TypeError, ValueError):
            return []

    def _delete_managed_cms_images(self, image_urls: list[str]) -> None:
        prefix = f"{self.settings.public_base_url.rstrip('/')}/cms-media/"
        for image_url in image_urls:
            if not image_url.startswith(prefix):
                continue
            filename = image_url.removeprefix(prefix)
            if re.fullmatch(r"[a-f0-9]{32}\.(?:jpg|png|gif|webp)", filename):
                (self.cms_media_dir / filename).unlink(missing_ok=True)

    async def handle_cms_conversation(self, event: IncomingEvent, conversation: dict[str, Any]) -> None:
        admin = self.storage.admin_for(event.platform, event.user_id)
        if not admin:
            self.storage.clear_conversation(event.platform, event.user_id)
            await self.send(event, OutgoingMessage("Доступ администратора не найден."))
            return
        self._require_content_editor(admin)
        flow = conversation["flow"]
        value = (event.text or "").strip()
        data = conversation["data"]
        if flow in {"cms_text", "cms_text_replace", "cms_text_before", "cms_text_after"}:
            if not value or len(value) > 4000:
                await self.send(
                    event,
                    OutgoingMessage(
                        "Сообщение получилось пустым или слишком длинным. Отправьте текст короче."
                    ),
                )
                return
            content_id = int(data["content_id"])
            entry = self.storage.content_entry(content_id)
            if not entry:
                raise ValueError("Сообщение не найдено")
            existing_text = str(entry["text_override"] or "{default}")
            if flow == "cms_text_before":
                updated_text = f"{value}\n\n{existing_text}"
            elif flow == "cms_text_after":
                updated_text = f"{existing_text}\n\n{value}"
            else:
                updated_text = None if value == "-" else value
            self.storage.set_content_text(content_id, updated_text)
            self.storage.clear_conversation(event.platform, event.user_id)
            self.storage.audit(admin["id"], "bot_content.text.changed", "bot_content", str(content_id))
            await self.show_cms_entry(event, admin, content_id, notice="Текст сохранён")
            return
        if flow == "cms_images":
            content_id = int(data["content_id"])
            entry = self.storage.content_entry(content_id)
            if not entry:
                raise ValueError("Сообщение не найдено")
            images = self._cms_entry_images(entry)
            if value == "-":
                self._delete_managed_cms_images(images)
                images = []
            else:
                if len(images) >= 10:
                    await self.send(
                        event,
                        OutgoingMessage(
                            "У сообщения уже 10 картинок. Удалите их или нажмите «Готово».",
                            [
                                [Button("Готово, вернуться", callback=f"cms:images-done:{content_id}")],
                                [
                                    Button(
                                        "Удалить все картинки",
                                        callback=f"cms:images-clear:{content_id}",
                                    )
                                ],
                            ],
                        ),
                    )
                    return
                image_url = await self._save_incoming_cms_image(event)
                supplied_urls = [
                    line.strip()
                    for line in value.splitlines()
                    if re.fullmatch(r"https://[^\s]+", line.strip())
                ]
                additions = ([image_url] if image_url else []) + supplied_urls
                if not additions:
                    await self.send(
                        event,
                        OutgoingMessage(
                            "Не получилось добавить картинку. Отправьте её как обычное фото.",
                            [
                                [Button("Готово, вернуться", callback=f"cms:images-done:{content_id}")],
                                [Button("Назад", callback=f"cms:edit-back:{content_id}")],
                            ],
                        ),
                    )
                    return
                images.extend(url for url in additions if url not in images)
            if len(images) > 10:
                await self.send(
                    event,
                    OutgoingMessage(
                        "У сообщения уже 10 картинок. Удалите их или нажмите «Готово».",
                        [
                            [Button("Готово, вернуться", callback=f"cms:images-done:{content_id}")],
                            [Button("Удалить все картинки", callback=f"cms:images-clear:{content_id}")],
                        ],
                    ),
                )
                return
            self.storage.set_content_images(content_id, images)
            self.storage.audit(
                admin["id"],
                "bot_content.images.changed",
                "bot_content",
                str(content_id),
                {"count": len(images)},
            )
            change_message = (
                "✅ Все картинки удалены."
                if value == "-"
                else f"✅ Картинка добавлена. Сейчас их: <b>{len(images)}</b>."
            )
            await self.send(
                event,
                OutgoingMessage(
                    f"{change_message}\n\nМожно отправить следующую или вернуться к сообщению.",
                    [
                        [Button("Готово, вернуться", callback=f"cms:images-done:{content_id}")],
                        [Button("Удалить все картинки", callback=f"cms:images-clear:{content_id}")],
                    ],
                ),
            )
            return
        if flow == "cms_button_text":
            if not value or len(value) > 64:
                await self.send(event, OutgoingMessage("Название должно содержать от 1 до 64 символов."))
                return
            button_id = int(data["button_id"])
            self.storage.set_content_button_text(button_id, None if value == "-" else value)
            self.storage.clear_conversation(event.platform, event.user_id)
            self.storage.audit(admin["id"], "bot_content.button.renamed", "bot_button", str(button_id))
            await self.show_cms_button(event, admin, button_id)
            return
        if flow == "cms_button_label":
            if not value or len(value) > 64:
                await self.send(event, OutgoingMessage("Название должно содержать от 1 до 64 символов."))
                return
            data["label"] = value
            self.storage.set_conversation(event.platform, event.user_id, "cms_button_body", "value", data)
            await self.send(
                event,
                OutgoingMessage(
                    f"Название: <b>{safe(value)}</b>\n\nТеперь отправьте сообщение, которое откроется по этой кнопке.",
                    [[Button("Отмена", callback="flow:cancel")]],
                    content_key="admin.content.add_button_body",
                    content_title="Новая кнопка: сообщение",
                    content_category="admin",
                ),
            )
            return
        if flow == "cms_button_body":
            if not value or len(value) > 4000:
                await self.send(event, OutgoingMessage("Сообщение должно содержать от 1 до 4000 символов."))
                return
            content_id = int(data["content_id"])
            button_id, target_id = self.storage.create_custom_button(content_id, str(data["label"]), value)
            self.storage.clear_conversation(event.platform, event.user_id)
            self.storage.audit(
                admin["id"],
                "bot_content.button.created",
                "bot_button",
                str(button_id),
                {"target_content_id": target_id},
            )
            await self.show_cms_button(event, admin, button_id)

    async def show_cms_features(self, event: IncomingEvent, admin: dict[str, Any]) -> None:
        self._require_content_editor(admin)
        features = self.storage.features()
        buttons = [
            [
                Button(
                    f"{'✅' if item['is_enabled'] else '🚫'} {item['title']}",
                    callback=f"cms:feature:{item['feature_key']}",
                )
            ]
            for item in features
        ]
        buttons.append([Button("К редактору", callback="cms:home")])
        await self.send(
            event,
            OutgoingMessage(
                "<b>Включение возможностей</b>\n\n"
                "Отключённая возможность исчезает из всех актуальных сообщений. "
                "Старые кнопки также перестают открывать этот раздел.",
                buttons,
                content_key="admin.content.features",
                content_title="Управление возможностями",
                content_category="admin",
            ),
        )

    async def toggle_cms_feature(self, event: IncomingEvent, admin: dict[str, Any], feature_key: str) -> None:
        self._require_content_editor(admin)
        enabled = self.storage.toggle_feature(feature_key)
        if enabled is None:
            raise ValueError("Возможность не найдена")
        self.storage.audit(
            admin["id"], "bot_feature.toggled", "bot_feature", feature_key, {"enabled": enabled}
        )
        await self.show_cms_features(event, admin)

    async def show_admin_requests(
        self, event: IncomingEvent, admin: dict[str, Any], filter_value: str, offset: int
    ) -> None:
        filters: dict[str, Any] = {"limit": 10, "offset": max(0, offset)}
        if filter_value == "mine":
            filters["assignedAdminKey"] = admin["id"]
        elif filter_value in STATUS_LABELS:
            filters["status"] = filter_value
        result = await self.site.requests(**filters)
        items = result["items"]
        label = "Мои заявки" if filter_value == "mine" else STATUS_LABELS.get(filter_value, "Все заявки")
        lines = [f"<b>{safe(label)}</b>", f"Всего: {result['pagination']['total']}", ""]
        buttons = []
        for item in items:
            lines.append(
                f"{safe(item['requestNumber'])} · {safe(item['name'])} · {safe(SOURCE_LABELS.get(item['source'], item['source']))}"
            )
            buttons.append(
                [Button(f"{item['requestNumber']} — {item['name'][:24]}", callback=f"req:view:{item['id']}")]
            )
        if not items:
            lines.append("Подходящих заявок нет.")
        nav = []
        if offset > 0:
            nav.append(Button("Назад", callback=f"admin:requests:{filter_value}:{max(0, offset - 10)}"))
        if result["pagination"]["hasMore"]:
            nav.append(Button("Далее", callback=f"admin:requests:{filter_value}:{offset + 10}"))
        if nav:
            buttons.append(nav)
        buttons.append([Button("Панель", callback="admin:home")])
        await self.send(event, OutgoingMessage("\n".join(lines), buttons))

    async def show_admin_request(self, event: IncomingEvent, request_id: str) -> None:
        await self.send(event, request_card(await self.site.request(request_id), admin=True))

    async def take_request(self, event: IncomingEvent, admin: dict[str, Any], request_id: str) -> None:
        if not self._can_mutate(admin):
            raise ValueError("Недостаточно прав для изменения заявки")
        await self.site.update_request(
            request_id,
            {
                "status": "IN_PROGRESS",
                "assignedAdminKey": admin["id"],
                "assignedAdminName": admin["display_name"],
                "actor": {"key": admin["id"], "name": admin["display_name"]},
            },
        )
        self.storage.audit(admin["id"], "request.taken", "request", request_id)
        await self.show_admin_request(event, request_id)

    async def show_assignment_choices(
        self, event: IncomingEvent, admin: dict[str, Any], request_id: str
    ) -> None:
        if not self._can_mutate(admin):
            raise ValueError("Недостаточно прав")
        candidates = [item for item in self.storage.list_admins() if item["is_active"]]
        buttons = [
            [
                Button(
                    item["display_name"][:40],
                    callback=f"req:assign-set:{request_id}:{item['id'][:8]}",
                )
            ]
            for item in candidates
        ]
        buttons.append([Button("Назад", callback=f"req:view:{request_id}")])
        await self.send(event, OutgoingMessage("<b>Назначить ответственного</b>", buttons))

    async def assign_request(
        self,
        event: IncomingEvent,
        admin: dict[str, Any],
        request_id: str,
        admin_prefix: str,
    ) -> None:
        if not self._can_mutate(admin):
            raise ValueError("Недостаточно прав")
        assignee = self.storage.admin_by_prefix(admin_prefix)
        if not assignee or not assignee["is_active"]:
            raise ValueError("Администратор не найден")
        await self.site.update_request(
            request_id,
            {
                "assignedAdminKey": assignee["id"],
                "assignedAdminName": assignee["display_name"],
                "actor": {"key": admin["id"], "name": admin["display_name"]},
            },
        )
        self.storage.audit(
            admin["id"],
            "request.assigned",
            "request",
            request_id,
            {"assignee": assignee["id"]},
        )
        await self.show_admin_request(event, request_id)

    async def show_status_choices(self, event: IncomingEvent, request_id: str) -> None:
        buttons = [
            [Button(label, callback=f"req:status-set:{request_id}:{status}")]
            for status, label in STATUS_LABELS.items()
        ]
        buttons.append([Button("Назад", callback=f"req:view:{request_id}")])
        await self.send(event, OutgoingMessage("<b>Новый статус заявки</b>", buttons))

    async def set_request_status(
        self, event: IncomingEvent, admin: dict[str, Any], request_id: str, status: str
    ) -> None:
        if not self._can_mutate(admin) or status not in STATUS_LABELS:
            raise ValueError("Недостаточно прав или неизвестный статус")
        await self.site.update_request(
            request_id, {"status": status, "actor": {"key": admin["id"], "name": admin["display_name"]}}
        )
        self.storage.audit(admin["id"], "request.status.changed", "request", request_id, {"status": status})
        await self.show_admin_request(event, request_id)

    async def start_admin_text(self, event: IncomingEvent, request_id: str, flow: str) -> None:
        admin = self.storage.admin_for(event.platform, event.user_id)
        if not admin or not self._can_mutate(admin):
            raise ValueError("Недостаточно прав")
        self.storage.set_conversation(event.platform, event.user_id, flow, "text", {"request_id": request_id})
        title = (
            "Введите внутренний комментарий." if flow == "admin_comment" else "Введите сообщение заявителю."
        )
        await self.send(
            event,
            OutgoingMessage(
                f"<b>{title}</b>\n\nДо 4000 символов.", [[Button("Отменить", callback="flow:cancel")]]
            ),
        )

    async def handle_admin_conversation(self, event: IncomingEvent, conversation: dict[str, Any]) -> None:
        admin = self.storage.admin_for(event.platform, event.user_id)
        if not admin:
            self.storage.clear_conversation(event.platform, event.user_id)
            await self.send(event, OutgoingMessage("Доступ администратора не найден."))
            return
        text = (event.text or "").strip()
        if conversation["flow"] == "admin_search":
            if len(text) < 2:
                await self.send(event, OutgoingMessage("Введите не менее двух символов."))
                return
            result = await self.site.requests(q=text[:120], limit=20, offset=0)
            self.storage.clear_conversation(event.platform, event.user_id)
            lines = [f"<b>Результаты поиска</b>\nНайдено: {result['pagination']['total']}", ""]
            buttons = []
            for item in result["items"]:
                lines.append(
                    f"{safe(item['requestNumber'])} · {safe(item['name'])} · {safe(item.get('phone') or '—')}"
                )
                buttons.append([Button(str(item["requestNumber"]), callback=f"req:view:{item['id']}")])
            buttons.append([Button("Панель", callback="admin:home")])
            await self.send(event, OutgoingMessage("\n".join(lines), buttons))
            return
        if not self._can_mutate(admin) or not text or len(text) > 4000:
            await self.send(event, OutgoingMessage("Введите текст длиной от 1 до 4000 символов."))
            return
        request_id = conversation["data"]["request_id"]
        if conversation["flow"] == "admin_comment":
            await self.site.add_activity(
                request_id,
                {
                    "type": "COMMENT",
                    "body": text,
                    "actor": {"key": admin["id"], "name": admin["display_name"]},
                },
            )
            self.storage.audit(admin["id"], "request.comment.added", "request", request_id)
            confirmation = "Комментарий добавлен."
        else:
            item = await self.site.request(request_id)
            recipient = self.storage.request_recipient(request_id)
            if (
                not recipient
                and item.get("externalPlatform") in {"TELEGRAM", "MAX"}
                and item.get("externalUserId")
            ):
                recipient = {
                    "platform": item["externalPlatform"],
                    "user_id": item["externalUserId"],
                    "chat_id": item.get("externalChatId") or item["externalUserId"],
                }
            if not recipient:
                await self.send(
                    event,
                    OutgoingMessage(
                        "Пользователь пришёл с сайта и ещё не привязал мессенджер. Используйте телефон или email."
                    ),
                )
                return
            target_platform = Platform(recipient["platform"])
            target_id = str(
                recipient["chat_id"] if target_platform is Platform.TELEGRAM else recipient["user_id"]
            )
            await self.send_to(
                target_platform,
                target_id,
                OutgoingMessage(
                    f"<b>Сообщение команды проекта</b>\n\n{escape(text)}\n\nОтветьте на это сообщение — ответ попадёт в карточку заявки.",
                    [[Button("Открыть заявку", callback=f"request:view:{request_id}")]],
                    content_key="notifications.admin_message",
                    content_title="Пользователю: сообщение администратора",
                    content_category="notifications",
                ),
            )
            self.storage.set_conversation(
                target_platform, str(recipient["user_id"]), "reply_admin", "text", {"request_id": request_id}
            )
            await self.site.add_activity(
                request_id,
                {
                    "type": "MESSAGE_TO_USER",
                    "body": text,
                    "actor": {"key": admin["id"], "name": admin["display_name"]},
                },
            )
            self.storage.audit(admin["id"], "request.message.sent", "request", request_id)
            confirmation = "Сообщение отправлено заявителю."
        self.storage.clear_conversation(event.platform, event.user_id)
        await self.send(
            event,
            OutgoingMessage(confirmation, [[Button("Открыть заявку", callback=f"req:view:{request_id}")]]),
        )

    async def show_reminder_choices(self, event: IncomingEvent, request_id: str) -> None:
        await self.send(
            event,
            OutgoingMessage(
                "<b>Когда напомнить?</b>",
                [
                    [
                        Button("Через час", callback=f"req:remind-set:{request_id}:1h"),
                        Button("Завтра в 10:00", callback=f"req:remind-set:{request_id}:tomorrow"),
                    ],
                    [
                        Button("Через 3 дня", callback=f"req:remind-set:{request_id}:3d"),
                        Button("Снять напоминание", callback=f"req:remind-set:{request_id}:clear"),
                    ],
                    [Button("Назад", callback=f"req:view:{request_id}")],
                ],
            ),
        )

    async def set_reminder(
        self, event: IncomingEvent, admin: dict[str, Any], request_id: str, preset: str
    ) -> None:
        if not self._can_mutate(admin):
            raise ValueError("Недостаточно прав")
        now = datetime.now(self.settings.timezone)
        values = {
            "1h": now + timedelta(hours=1),
            "tomorrow": (now + timedelta(days=1)).replace(hour=10, minute=0, second=0, microsecond=0),
            "3d": now + timedelta(days=3),
            "clear": None,
        }
        if preset not in values:
            raise ValueError("Неизвестный вариант напоминания")
        value = values[preset]
        await self.site.update_request(
            request_id,
            {
                "nextContactAt": value.astimezone(UTC).isoformat() if value else None,
                "actor": {"key": admin["id"], "name": admin["display_name"]},
            },
        )
        self.storage.audit(
            admin["id"],
            "request.reminder.changed",
            "request",
            request_id,
            {"next": value.isoformat() if value else None},
        )
        await self.show_admin_request(event, request_id)

    async def show_request_history(self, event: IncomingEvent, request_id: str) -> None:
        item = await self.site.request(request_id)
        activities = item.get("activities", [])
        lines = [f"<b>История {safe(item['requestNumber'])}</b>", ""]
        for activity in activities[-30:]:
            description = activity["type"]
            if activity.get("fromStatus") or activity.get("toStatus"):
                description = f"{STATUS_LABELS.get(activity.get('fromStatus'), '—')} → {STATUS_LABELS.get(activity.get('toStatus'), '—')}"
            elif activity.get("body"):
                description = activity["body"]
            lines.append(
                f"{format_datetime(activity.get('createdAt'))} · {safe(activity.get('actorName') or 'Система')}\n{safe(description)}"
            )
        await self.send(
            event,
            OutgoingMessage(
                "\n\n".join(lines)[:3900], [[Button("Назад", callback=f"req:view:{request_id}")]]
            ),
        )

    async def confirm_block_sender(
        self, event: IncomingEvent, admin: dict[str, Any], request_id: str
    ) -> None:
        if not self._can_mutate(admin):
            raise ValueError("Недостаточно прав")
        item = await self.site.request(request_id)
        if item.get("externalPlatform") not in {"TELEGRAM", "MAX"} or not item.get("externalUserId"):
            raise ValueError("У заявки нет связанного пользователя бота")
        await self.send(
            event,
            OutgoingMessage(
                f"Заблокировать отправителя заявки {safe(item['requestNumber'])}? Бот перестанет отвечать этому пользователю.",
                [
                    [Button("Заблокировать", callback=f"req:block:{request_id}")],
                    [Button("Отмена", callback=f"req:view:{request_id}")],
                ],
            ),
        )

    async def block_sender(self, event: IncomingEvent, admin: dict[str, Any], request_id: str) -> None:
        if not self._can_mutate(admin):
            raise ValueError("Недостаточно прав")
        item = await self.site.request(request_id)
        source = item.get("externalPlatform")
        user_id = item.get("externalUserId")
        if source not in {"TELEGRAM", "MAX"} or not user_id:
            raise ValueError("У заявки нет связанного пользователя бота")
        self.storage.set_blocked(Platform(source), str(user_id), True)
        await self.site.add_activity(
            request_id,
            {
                "type": "COMMENT",
                "body": "Отправитель заблокирован в боте.",
                "actor": {"key": admin["id"], "name": admin["display_name"]},
            },
        )
        self.storage.audit(admin["id"], "user.blocked", "request", request_id)
        await self.send(
            event,
            OutgoingMessage(
                "Пользователь заблокирован.",
                [[Button("Открыть заявку", callback=f"req:view:{request_id}")]],
            ),
        )

    async def show_admins(self, event: IncomingEvent, current: dict[str, Any]) -> None:
        admins = self.storage.list_admins()
        lines = ["<b>Администраторы</b>", ""]
        buttons = []
        for item in admins:
            state = "Активен" if item["is_active"] else "Отключён"
            accounts = ", ".join(account["platform"] for account in item["accounts"]) or "Нет аккаунтов"
            lines.append(
                f"{safe(item['display_name'])} · {safe(ROLE_LABELS.get(item['role'], item['role']))}\n{state} · {accounts}"
            )
            buttons.append([Button(item["display_name"][:32], callback=f"adm:view:{item['id']}")])
        buttons.append([Button("Добавить администратора", callback="adm:add:ADMIN")])
        buttons.append([Button("Панель", callback="admin:home")])
        await self.send(event, OutgoingMessage("\n\n".join(lines), buttons))

    async def create_admin_invite(self, event: IncomingEvent, current: dict[str, Any], role: str) -> None:
        if not self._can_mutate(current) or role != AdminRole.ADMIN.value:
            raise ValueError("Недостаточно прав")
        token = self.storage.create_invite(current["id"], AdminRole.ADMIN)
        self.storage.audit(
            current["id"],
            "admin.invite.created",
            "admin_invite",
            None,
            {"role": AdminRole.ADMIN.value},
        )
        links = []
        if self.settings.telegram_username:
            links.append(f"Telegram: https://t.me/{self.settings.telegram_username}?start={token}")
        if self.settings.max_username:
            links.append(f"MAX: https://max.ru/{self.settings.max_username}?start={token}")
        link_text = "\n".join(links)
        await self.send(
            event,
            OutgoingMessage(
                f"<b>Приглашение администратора создано</b>\n\nКод: <code>{token}</code>\n"
                "Срок действия: 30 минут. Код одноразовый.\n\n"
                f"{safe(link_text)}\n\nБудущий администратор должен открыть бота по ссылке или отправить <code>/start {token}</code>.",
                [[Button("К администраторам", callback="admins:list")]],
                disable_preview=False,
            ),
        )

    async def show_admin_detail(self, event: IncomingEvent, current: dict[str, Any], admin_id: str) -> None:
        if not self._can_mutate(current):
            raise ValueError("Недостаточно прав")
        item = next((value for value in self.storage.list_admins() if value["id"] == admin_id), None)
        if not item:
            raise ValueError("Администратор не найден")
        accounts = "\n".join(
            f"{account['platform']}: {safe(account.get('username') or account['user_id'])}"
            for account in item["accounts"]
        )
        buttons = [
            [Button("Отключить" if item["is_active"] else "Включить", callback=f"adm:toggle:{admin_id}")],
            [Button("Подключить MAX бот", callback=f"adm:link:{admin_id}")],
            [Button("Назад", callback="admins:list")],
        ]
        await self.send(
            event,
            OutgoingMessage(
                f"<b>{safe(item['display_name'])}</b>\n\nРоль: {safe(ROLE_LABELS.get(item['role'], item['role']))}\n"
                f"Состояние: {'активен' if item['is_active'] else 'отключён'}\n\n{accounts}",
                buttons,
            ),
        )

    async def create_link_invite(self, event: IncomingEvent, current: dict[str, Any], admin_id: str) -> None:
        if not self._can_mutate(current):
            raise ValueError("Недостаточно прав")
        target = next((item for item in self.storage.list_admins() if item["id"] == admin_id), None)
        if not target:
            raise ValueError("Администратор не найден")
        token = self.storage.create_invite(current["id"], AdminRole.ADMIN, target_admin_id=admin_id)
        self.storage.audit(current["id"], "admin.link.invite.created", "admin", admin_id)
        await self.send(
            event,
            OutgoingMessage(
                f"<b>Код для подключения MAX бота</b>\n\n<code>{token}</code>\n\n"
                "Откройте MAX бот и отправьте ему эту команду:\n"
                f"<code>/start {token}</code>\n\nКод одноразовый и действует 30 минут.",
                [[Button("Назад", callback=f"adm:view:{admin_id}")]],
            ),
        )

    async def toggle_admin(self, event: IncomingEvent, current: dict[str, Any], admin_id: str) -> None:
        if not self._can_mutate(current):
            raise ValueError("Недостаточно прав")
        if current["id"] == admin_id:
            raise ValueError("Нельзя отключить собственную учётную запись")
        item = next((value for value in self.storage.list_admins() if value["id"] == admin_id), None)
        if not item:
            raise ValueError("Администратор не найден")
        self.storage.update_admin(admin_id, active=not bool(item["is_active"]))
        self.storage.audit(
            current["id"], "admin.active.changed", "admin", admin_id, {"active": not bool(item["is_active"])}
        )
        await self.show_admins(event, current)

    async def show_preferences(self, event: IncomingEvent, admin: dict[str, Any]) -> None:
        await self.send(
            event,
            OutgoingMessage(
                "<b>Уведомления</b>\n\n"
                f"Все новые заявки: <b>{'включены' if admin['notify_all'] else 'выключены'}</b>\n"
                f"Тихие часы 22:00–08:00: <b>{'включены' if admin['quiet_enabled'] else 'выключены'}</b>\n\n"
                "Назначенные вам заявки и критические ошибки приходят независимо от этой настройки.",
                [
                    [Button("Переключить новые заявки", callback="admin:pref:notify_all")],
                    [Button("Переключить тихие часы", callback="admin:pref:quiet_enabled")],
                    [Button("Панель", callback="admin:home")],
                ],
            ),
        )

    async def show_system(self, event: IncomingEvent, admin: dict[str, Any]) -> None:
        if not self._can_mutate(admin):
            raise ValueError("Недостаточно прав")
        started = datetime.now(UTC)
        try:
            health = await self.site.health()
            site_state = f"Доступен ({health.get('timestamp', 'ответ получен')})"
        except SiteApiError as exc:
            site_state = f"Ошибка: {exc}"
        queue = self.storage.queue_stats()
        cursor = self.storage.state("site_event_cursor", "0")
        elapsed = (datetime.now(UTC) - started).total_seconds()
        await self.send(
            event,
            OutgoingMessage(
                "<b>Состояние системы</b>\n\n"
                f"API сайта: {safe(site_state)}\n"
                f"Проверка: {elapsed:.2f} с\n"
                f"Последнее событие: {safe(cursor)}\n"
                f"В очереди: {queue.get('PENDING', 0)}\n"
                f"Ошибок доставки: {queue.get('DEAD', 0)}\n"
                f"Telegram: {'подключён' if Platform.TELEGRAM in self.messengers else 'отключён'}\n"
                f"MAX: {'подключён' if Platform.MAX in self.messengers else 'отключён'}",
                [
                    [Button("Журнал действий", callback="admin:audit")],
                    [Button("Заблокированные", callback="admin:blocked")],
                    [Button("Обновить", callback="admin:system"), Button("Панель", callback="admin:home")],
                ],
            ),
        )

    async def export_requests(self, event: IncomingEvent, admin: dict[str, Any]) -> None:
        if not self._can_mutate(admin):
            raise ValueError("Недостаточно прав для выгрузки персональных данных")
        content = await self.site.export_requests()
        caption = self.storage.customize_message(
            OutgoingMessage(
                "Заявки «Маршрут построен»",
                content_key="admin.export.caption",
                content_title="Подпись CSV-выгрузки",
                content_category="admin",
            )
        ).text
        await self.messengers[event.platform].send_document(
            event.chat_id,
            f"shooting-requests-{datetime.now(self.settings.timezone).date().isoformat()}.csv",
            content,
            caption,
        )
        self.storage.audit(admin["id"], "requests.exported", "request")
        await self.send(
            event,
            OutgoingMessage(
                "Выгрузка готова и отправлена выше.",
                [[Button("Панель", callback="admin:home")]],
                content_key="admin.export.completed",
                content_title="CSV-выгрузка отправлена",
                content_category="admin",
            ),
        )

    async def show_blocked_users(self, event: IncomingEvent, admin: dict[str, Any]) -> None:
        if not self._can_mutate(admin):
            raise ValueError("Недостаточно прав")
        rows = self.storage.blocked_users()
        lines = ["<b>Заблокированные пользователи</b>", ""]
        buttons = []
        for row in rows:
            lines.append(f"{safe(row['display_name'])} · {safe(row['platform'])}")
            buttons.append(
                [
                    Button(
                        f"Разблокировать {row['display_name'][:24]}",
                        callback=f"admin:unblock:{row['platform']}:{row['user_id']}",
                    )
                ]
            )
        if not rows:
            lines.append("Список пуст.")
        buttons.append([Button("Система", callback="admin:system")])
        await self.send(event, OutgoingMessage("\n".join(lines), buttons))

    async def unblock_user(
        self,
        event: IncomingEvent,
        admin: dict[str, Any],
        platform: str,
        user_id: str,
    ) -> None:
        if not self._can_mutate(admin):
            raise ValueError("Недостаточно прав")
        self.storage.set_blocked(Platform(platform), user_id, False)
        self.storage.audit(admin["id"], "user.unblocked", "bot_user", f"{platform}:{user_id}")
        await self.show_blocked_users(event, admin)

    async def show_audit(self, event: IncomingEvent, admin: dict[str, Any]) -> None:
        if not self._can_mutate(admin):
            raise ValueError("Недостаточно прав")
        rows = self.storage.recent_audit(20)
        lines = ["<b>Последние действия</b>", ""]
        for row in rows:
            lines.append(
                f"{format_datetime(row['created_at'])} · {safe(row.get('actor_name') or 'Система')}\n{safe(row['action'])} {safe(row.get('entity_id') or '')}"
            )
        await self.send(
            event, OutgoingMessage("\n\n".join(lines)[:3900], [[Button("Система", callback="admin:system")]])
        )

    async def show_broadcast_menu(self, event: IncomingEvent, admin: dict[str, Any]) -> None:
        if not self._can_mutate(admin):
            raise ValueError("Недостаточно прав")
        await self.send(
            event,
            OutgoingMessage(
                "<b>Рассылка публикации</b>\n\n"
                "Сообщение будет отправлено всем активным пользователям бота. "
                "Сначала выберите раздел и материал — перед отправкой будет показан предпросмотр.",
                [
                    [
                        Button("Выпуски", callback="broadcast:list:interviews"),
                        Button("Герои", callback="broadcast:list:entrepreneurs"),
                    ],
                    [
                        Button("Бизнес", callback="broadcast:list:businesses"),
                        Button("Журнал", callback="broadcast:list:articles"),
                    ],
                    [Button("Короткие видео", callback="broadcast:list:reels")],
                    [Button("Панель", callback="admin:home")],
                ],
                content_key="admin.broadcast.menu",
                content_title="Рассылка: выбор раздела",
                content_category="admin",
            ),
        )

    async def show_broadcast_items(self, event: IncomingEvent, admin: dict[str, Any], kind: str) -> None:
        if not self._can_mutate(admin):
            raise ValueError("Недостаточно прав")
        result = await self.site.content(kind, limit=10)
        self.storage.cache_content(result["items"])
        buttons = [
            [Button(item["title"][:48], callback=f"broadcast:preview:{kind}:{item['id']}")]
            for item in result["items"]
        ]
        buttons.append([Button("Назад", callback="broadcast:menu")])
        await self.send(
            event,
            OutgoingMessage(f"<b>{safe(CONTENT_LABELS.get(kind, kind))}</b>\n\nВыберите материал:", buttons),
        )

    async def preview_broadcast(
        self, event: IncomingEvent, admin: dict[str, Any], kind: str, item_id: str
    ) -> None:
        if not self._can_mutate(admin):
            raise ValueError("Недостаточно прав")
        item = self.storage.cached_content(kind, item_id)
        if not item:
            raise ValueError("Материал устарел — откройте список заново")
        await self.send(event, self._publication_message(item))
        await self.send(
            event,
            OutgoingMessage(
                "<b>Предпросмотр показан выше.</b>\n\n"
                f"Получателей: <b>{len(self.storage.active_users())}</b> активных пользователей.",
                [
                    [Button("Подтвердить рассылку", callback=f"broadcast:send:{kind}:{item_id}")],
                    [Button("Отмена", callback="broadcast:menu")],
                ],
                content_key="admin.broadcast.confirm",
                content_title="Рассылка: подтверждение",
                content_category="admin",
            ),
        )

    def _publication_message(self, item: dict[str, Any]) -> OutgoingMessage:
        return OutgoingMessage(
            f"<b>Новая публикация</b>\n\n<b>{safe(item['title'])}</b>\n{safe(item.get('subtitle') or '')}",
            [[Button("Открыть", url=f"{self.settings.site_public_url}{item['path']}")]],
            disable_preview=False,
            content_key="notifications.publication",
            content_title="Пользователю: ручная рассылка публикации",
            content_category="notifications",
        )

    async def send_broadcast(
        self, event: IncomingEvent, admin: dict[str, Any], kind: str, item_id: str
    ) -> None:
        if not self._can_mutate(admin):
            raise ValueError("Недостаточно прав")
        item = self.storage.cached_content(kind, item_id)
        if not item:
            raise ValueError("Материал устарел")
        recipients = self.storage.active_users()
        message = self._publication_message(item)
        queued = 0
        for recipient in recipients:
            key = f"manual:{kind}:{item_id}:{recipient['platform']}:{recipient['user_id']}"
            if self.storage.enqueue(
                recipient["platform"], recipient["chat_id"], outgoing_to_dict(message), dedupe_key=key
            ):
                queued += 1
        self.storage.audit(
            admin["id"], "broadcast.queued", "content", item_id, {"kind": kind, "recipients": queued}
        )
        await self.send(
            event,
            OutgoingMessage(
                f"Рассылка поставлена в очередь. Получателей: <b>{queued}</b>.",
                [[Button("Панель", callback="admin:home")]],
            ),
        )

    async def process_site_events_once(self) -> int:
        cursor = int(self.storage.state("site_event_cursor", "0") or 0)
        response = await self.site.events(cursor, 100)
        events = response.get("events", [])
        for site_event in events:
            await self.process_site_event(site_event)
            cursor = int(site_event["id"])
            self.storage.set_state("site_event_cursor", str(cursor))
        return len(events)

    async def process_site_event(self, site_event: dict[str, Any]) -> None:
        event_type = str(site_event["type"])
        payload = site_event.get("payload") or {}
        event_id = int(site_event["id"])
        if event_type.startswith("request."):
            if payload.get("externalPlatform") in {"TELEGRAM", "MAX"} and payload.get("externalUserId"):
                self.storage.link_request(
                    payload["id"], Platform(payload["externalPlatform"]), str(payload["externalUserId"])
                )
            if event_type == "request.created":
                message = request_card(payload, admin=True)
                message.text = f"<b>Новая заявка</b>\n\n{message.text}"
                message.content_key = "notifications.request_created"
                message.content_title = "Администратору: новая заявка"
                message.content_category = "notifications"
                for recipient in self.storage.admin_recipients():
                    available = self._after_quiet_hours(recipient)
                    self.storage.enqueue(
                        recipient["platform"],
                        recipient["chat_id"],
                        outgoing_to_dict(message),
                        dedupe_key=f"event:{event_id}:admin:{recipient['platform']}:{recipient['user_id']}",
                        available_at=available,
                    )
            elif event_type == "request.status_changed":
                recipient = self.storage.request_recipient(str(payload["id"]))
                if recipient:
                    status = STATUS_LABELS.get(str(payload.get("status")), str(payload.get("status")))
                    message = OutgoingMessage(
                        f"<b>Статус заявки {safe(payload.get('requestNumber'))} изменён</b>\n\nТеперь: <b>{safe(status)}</b>",
                        [[Button("Открыть заявку", callback=f"request:view:{payload['id']}")]],
                        content_key="notifications.request_status",
                        content_title="Пользователю: новый статус заявки",
                        content_category="notifications",
                    )
                    self.storage.enqueue(
                        recipient["platform"],
                        recipient["chat_id"],
                        outgoing_to_dict(message),
                        dedupe_key=f"event:{event_id}:user:{recipient['platform']}:{recipient['user_id']}",
                    )
            elif event_type == "request.user_cancelled":
                message = request_card(payload, admin=True)
                message.text = f"<b>Заявка отменена участником</b>\n\n{message.text}"
                message.content_key = "notifications.request_cancelled"
                message.content_title = "Администратору: заявка отменена"
                message.content_category = "notifications"
                for recipient in self.storage.admin_recipients():
                    self.storage.enqueue(
                        recipient["platform"],
                        recipient["chat_id"],
                        outgoing_to_dict(message),
                        dedupe_key=f"event:{event_id}:cancel:{recipient['platform']}:{recipient['user_id']}",
                    )
            elif event_type == "request.activity.message_from_user":
                message = OutgoingMessage(
                    f"<b>Новое сообщение по заявке {safe(payload.get('requestNumber'))}</b>\n\n{safe(payload.get('name'))}",
                    [[Button("Открыть заявку", callback=f"req:view:{payload['id']}")]],
                    content_key="notifications.user_message",
                    content_title="Администратору: сообщение пользователя",
                    content_category="notifications",
                )
                for recipient in self.storage.admin_recipients(
                    assigned_admin_key=payload.get("assignedAdminKey")
                ):
                    self.storage.enqueue(
                        recipient["platform"],
                        recipient["chat_id"],
                        outgoing_to_dict(message),
                        dedupe_key=f"event:{event_id}:message:{recipient['platform']}:{recipient['user_id']}",
                    )

    def _after_quiet_hours(self, recipient: dict[str, Any]) -> datetime | None:
        if not recipient.get("quiet_enabled"):
            return None
        now = datetime.now(self.settings.timezone)
        start, end = int(recipient["quiet_start"]), int(recipient["quiet_end"])
        in_quiet = now.hour >= start or now.hour < end if start > end else start <= now.hour < end
        if not in_quiet:
            return None
        target = now.replace(hour=end, minute=0, second=0, microsecond=0)
        if now.hour >= start:
            target += timedelta(days=1)
        return target.astimezone(UTC)

    async def process_delivery_once(self) -> bool:
        item = self.storage.claim_delivery()
        if not item:
            return False
        try:
            platform = Platform(item["platform"])
            messenger = self.messengers.get(platform)
            if not messenger:
                raise RuntimeError(f"{platform.value} is not configured")
            message = outgoing_from_dict(item["payload"])
            _ensure_home_navigation(message)
            _ensure_content_identity(message)
            await messenger.send(str(item["recipient_id"]), self.storage.customize_message(message))
            self.storage.complete_delivery(int(item["id"]))
            if platform is Platform.MAX:
                await asyncio.sleep(0.55)
        except Exception as exc:
            logger.exception("Delivery %s failed", item["id"])
            self.storage.fail_delivery(int(item["id"]), int(item["attempts"]), str(exc))
        return True

    async def process_reminders_once(self) -> int:
        result = await self.site.requests(dueBefore=datetime.now(UTC).isoformat(), limit=100, offset=0)
        queued = 0
        for item in result["items"]:
            reminder_key = f"reminder:{item['id']}:{item.get('nextContactAt')}"
            message = OutgoingMessage(
                f"<b>Пора связаться с заявителем</b>\n\n{safe(item['requestNumber'])} · {safe(item['name'])}\n{safe(item.get('phone') or item.get('email') or 'Контакт не указан')}",
                [[Button("Открыть заявку", callback=f"req:view:{item['id']}")]],
                content_key="notifications.reminder",
                content_title="Администратору: напоминание",
                content_category="notifications",
            )
            for recipient in self.storage.admin_recipients(assigned_admin_key=item.get("assignedAdminKey")):
                if self.storage.enqueue(
                    recipient["platform"],
                    recipient["chat_id"],
                    outgoing_to_dict(message),
                    dedupe_key=f"{reminder_key}:{recipient['platform']}:{recipient['user_id']}",
                ):
                    queued += 1
        return queued

    async def process_daily_digest_once(self) -> bool:
        now = datetime.now(self.settings.timezone)
        today = now.date().isoformat()
        if now.hour != self.settings.daily_digest_hour or self.storage.state("last_digest_date") == today:
            return False
        results = await asyncio.gather(
            *(self.site.requests(status=status, limit=1, offset=0) for status in STATUS_LABELS)
        )
        counts = {
            status: result["pagination"]["total"]
            for status, result in zip(STATUS_LABELS, results, strict=True)
        }
        message = OutgoingMessage(
            "<b>Ежедневная сводка</b>\n\n"
            f"Новые: <b>{counts['NEW']}</b>\nВ работе: <b>{counts['IN_PROGRESS']}</b>\n"
            f"Завершены: {counts['COMPLETED']}\nАрхив: {counts['ARCHIVED']}",
            [[Button("Открыть панель", callback="admin:home")]],
            content_key="notifications.daily_digest",
            content_title="Ежедневная сводка",
            content_category="notifications",
        )
        for recipient in self.storage.admin_recipients():
            self.storage.enqueue(
                recipient["platform"],
                recipient["chat_id"],
                outgoing_to_dict(message),
                dedupe_key=f"digest:{today}:{recipient['platform']}:{recipient['user_id']}",
            )
        self.storage.set_state("last_digest_date", today)
        return True
