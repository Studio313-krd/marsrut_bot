from __future__ import annotations

import re
from datetime import datetime
from html import escape, unescape
from typing import Any

from app.domain import Button, OutgoingMessage

STATUS_LABELS = {
    "NEW": "Новая",
    "IN_PROGRESS": "В работе",
    "COMPLETED": "Завершена",
    "ARCHIVED": "Архив",
}
SOURCE_LABELS = {"WEBSITE": "Сайт", "TELEGRAM": "Telegram", "MAX": "MAX"}
CONTENT_LABELS = {
    "latest": "Новые материалы",
    "entrepreneurs": "Герои",
    "businesses": "Бизнес",
    "articles": "Журнал",
    "interviews": "Выпуски",
    "videos": "Новые выпуски",
    "reels": "Короткие видео",
    "all": "Все публикации",
}
ROLE_LABELS = {"ADMIN": "Администратор"}


def safe(value: Any) -> str:
    return escape(str(value or ""), quote=False)


def plain_text(value: Any) -> str:
    """Convert optional CMS HTML into compact text suitable for a bot message or button."""
    text = unescape(str(value or ""))
    text = re.sub(r"<\s*br\s*/?\s*>", "\n", text, flags=re.IGNORECASE)
    text = re.sub(r"</\s*(?:p|div|li|h[1-6])\s*>", "\n", text, flags=re.IGNORECASE)
    text = re.sub(r"<[^>]+>", "", text)
    text = re.sub(r"[ \t\f\v]+", " ", text)
    text = re.sub(r"\s*\n\s*", "\n", text)
    return text.strip()


def open_content_button(title: Any, index: int) -> str:
    prefix = "Открыть "
    clean_title = plain_text(title) or f"материал {index}"
    available = 64 - len(prefix)
    if len(clean_title) > available:
        clean_title = f"{clean_title[: available - 1].rstrip()}…"
    return f"{prefix}{clean_title}"


def format_datetime(value: str | None) -> str:
    if not value:
        return "—"
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        return parsed.strftime("%d.%m.%Y %H:%M")
    except ValueError:
        return safe(value)


def user_menu(*, is_admin: bool, city_name: str | None = None) -> OutgoingMessage:
    city = city_name or "Все города"
    buttons = [
        [Button("Стать героем", callback="apply:start")],
        [
            Button("Новые выпуски", callback="content:videos:0"),
            Button("Герои", callback="content:entrepreneurs:0"),
        ],
        [Button("Бизнес", callback="content:businesses:0"), Button("Журнал", callback="content:articles:0")],
        [Button("Новое", callback="content:latest:0"), Button("Короткие видео", callback="content:reels:0")],
        [Button("Мои заявки", callback="requests:mine"), Button(f"Город: {city}", callback="city:menu")],
        [Button("О проекте", callback="about")],
    ]
    if is_admin:
        buttons.append([Button("Панель администратора", callback="admin:home")])
    return OutgoingMessage(
        "<b>МАРШРУТ ПОСТРОЕН</b>\n\n"
        "Истории людей, компаний и проектов, которые меняют привычные сферы и города.\n\n"
        "Выберите, что хотите посмотреть:",
        buttons,
        content_key="main.menu",
        content_title="Главное меню",
        content_category="main",
    )


def application_prompt(step: str, data: dict[str, Any]) -> OutgoingMessage:
    definitions = {
        "name": (1, "Как вас зовут?", "Укажите имя и фамилию."),
        "company": (
            2,
            "Как называется ваш проект или компания?",
            "Если проекта пока нет, нажмите «Пропустить».",
        ),
        "position": (3, "Какова ваша роль?", "Например: основатель, руководитель, управляющий партнёр."),
        "phone": (
            4,
            "По какому номеру с вами связаться?",
            "Поделитесь контактом кнопкой или введите номер вручную.",
        ),
        "email": (5, "Укажите email", "Это необязательно — можно пропустить."),
        "message": (
            6,
            "Расскажите о себе или проекте",
            "Коротко: чем вы занимаетесь и почему ваша история может быть интересна.",
        ),
    }
    number, title, hint = definitions[step]
    buttons: list[list[Button]] = []
    if step == "phone":
        buttons.append([Button("Поделиться контактом", kind="request_contact")])
    if step in {"company", "position", "email", "message"}:
        buttons.append([Button("Пропустить", callback=f"apply:skip:{step}")])
    if number > 1:
        buttons.append([Button("Назад", callback="apply:back"), Button("Отменить", callback="flow:cancel")])
    else:
        buttons.append([Button("Отменить", callback="flow:cancel")])
    return OutgoingMessage(
        f"<b>Заявка на участие</b>\nШаг {number} из 6\n\n<b>{title}</b>\n{hint}",
        buttons,
        content_key=f"application.prompt.{step}",
        content_title=f"Заявка: шаг {number} — {title}",
        content_category="application",
    )


def application_review(data: dict[str, Any], privacy_url: str) -> OutgoingMessage:
    text = (
        "<b>Проверьте заявку</b>\n\n"
        f"Имя: <b>{safe(data.get('name'))}</b>\n"
        f"Компания: {safe(data.get('company') or '—')}\n"
        f"Роль: {safe(data.get('position') or '—')}\n"
        f"Телефон: {safe(data.get('phone'))}\n"
        f"Email: {safe(data.get('email') or '—')}\n"
        f"О себе: {safe(data.get('message') or '—')}\n\n"
        "Перед отправкой подтвердите согласие на обработку персональных данных."
    )
    return OutgoingMessage(
        text,
        [
            [Button("Политика конфиденциальности", url=privacy_url)],
            [Button("Согласен и отправить", callback="apply:submit")],
            [Button("Изменить", callback="apply:edit"), Button("Отменить", callback="flow:cancel")],
        ],
        content_key="application.review",
        content_title="Заявка: проверка перед отправкой",
        content_category="application",
    )


def request_card(item: dict[str, Any], *, admin: bool = False) -> OutgoingMessage:
    request_id = str(item["id"])
    status = STATUS_LABELS.get(str(item.get("status")), str(item.get("status")))
    text = (
        f"<b>Заявка {safe(item.get('requestNumber'))}</b>\n"
        f"Статус: <b>{safe(status)}</b>\n"
        f"Источник: {safe(SOURCE_LABELS.get(str(item.get('source')), item.get('source')))}\n"
        f"Кампания: {safe(item.get('campaign') or '—')}\n"
        f"Создана: {format_datetime(item.get('createdAt'))}\n\n"
        f"Имя: <b>{safe(item.get('name'))}</b>\n"
        f"Компания: {safe(item.get('company') or '—')}\n"
        f"Роль: {safe(item.get('position') or '—')}\n"
        f"Телефон: {safe(item.get('phone') or '—')}\n"
        f"Email: {safe(item.get('email') or '—')}\n"
        f"Сообщение: {safe(item.get('message') or '—')}"
    )
    if admin:
        text += (
            f"\n\nОтветственный: <b>{safe(item.get('assignedAdminName') or 'Не назначен')}</b>"
            f"\nСледующий контакт: {format_datetime(item.get('nextContactAt'))}"
        )
        buttons = [
            [
                Button("Взять в работу", callback=f"req:take:{request_id}"),
                Button("Назначить", callback=f"req:assign:{request_id}"),
            ],
            [Button("Изменить статус", callback=f"req:status:{request_id}")],
            [
                Button("Написать заявителю", callback=f"req:message:{request_id}"),
                Button("Комментарий", callback=f"req:comment:{request_id}"),
            ],
            [
                Button("Напомнить", callback=f"req:remind:{request_id}"),
                Button("История", callback=f"req:history:{request_id}"),
            ],
            *(
                [[Button("Заблокировать отправителя", callback=f"req:block-confirm:{request_id}")]]
                if item.get("externalPlatform") in {"TELEGRAM", "MAX"}
                else []
            ),
            [Button("Назад к заявкам", callback="admin:requests:NEW:0")],
        ]
    else:
        buttons = []
        if item.get("status") == "NEW":
            buttons.append([Button("Дополнить заявку", callback=f"request:add:{request_id}")])
            buttons.append([Button("Отменить заявку", callback=f"request:cancel-confirm:{request_id}")])
        buttons.append([Button("К моим заявкам", callback="requests:mine")])
    return OutgoingMessage(
        text,
        buttons,
        content_key="admin.request.card" if admin else "requests.card",
        content_title="Карточка заявки для администратора" if admin else "Карточка заявки пользователя",
        content_category="admin" if admin else "requests",
    )


def content_list(
    kind: str, items: list[dict[str, Any]], site_url: str, offset: int, has_more: bool
) -> OutgoingMessage:
    label = CONTENT_LABELS.get(kind, "Материалы")
    if not items:
        return OutgoingMessage(
            f"<b>{safe(label)}</b>\n\nПока здесь нет опубликованных материалов для выбранного города.",
            [
                [Button("Выбрать другой город", callback="city:menu")],
                [Button("Главное меню", callback="menu")],
            ],
            content_key=f"catalog.{kind}.empty",
            content_title=f"Каталог «{label}»: пустой список",
            content_category="catalog",
        )
    lines = [f"<b>{safe(label)}</b>", ""]
    buttons: list[list[Button]] = []
    for index, item in enumerate(items, 1):
        title = plain_text(item.get("title")) or "Без названия"
        subtitle = plain_text(item.get("subtitle"))
        lines.append(f"<b>{index}. {safe(title)}</b>")
        if kind != "reels" and subtitle:
            lines.append(safe(subtitle[:220]))
        lines.append("")
        url = f"{site_url}{item['path']}"
        buttons.append([Button(open_content_button(title, index), url=url)])
    navigation: list[Button] = []
    if offset > 0:
        navigation.append(Button("Назад", callback=f"content:{kind}:{max(0, offset - len(items))}"))
    if has_more:
        navigation.append(Button("Далее", callback=f"content:{kind}:{offset + len(items)}"))
    if navigation:
        buttons.append(navigation)
    buttons.append([Button("Главное меню", callback="menu")])
    return OutgoingMessage(
        "\n".join(lines).strip(),
        buttons,
        content_key=f"catalog.{kind}.list",
        content_title=f"Каталог «{label}»: список",
        content_category="catalog",
    )


def admin_menu(counts: dict[str, int]) -> OutgoingMessage:
    return OutgoingMessage(
        "<b>Панель администратора</b>\n\n"
        f"Новые: <b>{counts.get('NEW', 0)}</b>\n"
        f"В работе: <b>{counts.get('IN_PROGRESS', 0)}</b>\n"
        f"Завершены: {counts.get('COMPLETED', 0)}\n"
        f"В архиве: {counts.get('ARCHIVED', 0)}",
        [
            [
                Button("Новые заявки", callback="admin:requests:NEW:0"),
                Button("В работе", callback="admin:requests:IN_PROGRESS:0"),
            ],
            [
                Button("Поиск", callback="admin:search"),
                Button("Мои заявки", callback="admin:requests:mine:0"),
            ],
            [Button("Администраторы", callback="admins:list"), Button("Рассылки", callback="broadcast:menu")],
            [Button("Тексты, кнопки и изображения", callback="cms:home")],
            [Button("Выгрузить заявки CSV", callback="admin:export")],
            [
                Button("Настройки уведомлений", callback="admin:preferences"),
                Button("Система", callback="admin:system"),
            ],
            [Button("Главное меню", callback="menu")],
        ],
        content_key="admin.home",
        content_title="Главная страница панели администратора",
        content_category="admin",
    )


def editable_default_messages(privacy_url: str, site_url: str) -> list[OutgoingMessage]:
    """Build safe examples so the editor is useful before users traverse every screen."""
    sample_application = {
        "name": "Иван Иванов",
        "company": "Название проекта",
        "position": "Основатель",
        "phone": "+7 999 123-45-67",
        "email": "mail@example.ru",
        "message": "Краткий рассказ о проекте",
    }
    sample_request = {
        "id": "example-request",
        "requestNumber": "MP-EXAMPLE",
        "status": "NEW",
        "source": "TELEGRAM",
        "name": "Иван Иванов",
        "company": "Название проекта",
        "position": "Основатель",
        "phone": "+7 999 123-45-67",
        "email": "mail@example.ru",
        "message": "Краткий рассказ о проекте",
        "externalPlatform": "TELEGRAM",
    }
    messages = [
        user_menu(is_admin=True),
        *(
            application_prompt(step, {})
            for step in ("name", "company", "position", "phone", "email", "message")
        ),
        application_review(sample_application, privacy_url),
        request_card(sample_request),
        request_card(sample_request, admin=True),
        admin_menu({status: 0 for status in STATUS_LABELS}),
    ]
    for kind in ("latest", "entrepreneurs", "businesses", "articles", "interviews", "videos", "reels"):
        messages.append(content_list(kind, [], site_url, 0, False))
        messages.append(
            content_list(
                kind,
                [
                    {
                        "id": "example-content",
                        "kind": kind,
                        "title": "Пример материала",
                        "subtitle": "Краткое описание",
                        "path": "/example",
                    }
                ],
                site_url,
                0,
                False,
            )
        )
    return messages
