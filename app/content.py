from __future__ import annotations

from collections.abc import Iterable

CATEGORY_LABELS: dict[str, str] = {
    "main": "Главное меню",
    "application": "Заявка на участие",
    "catalog": "Материалы и города",
    "requests": "Заявки пользователя",
    "privacy": "О проекте и данные",
    "admin": "Панель администратора",
    "notifications": "Служебные уведомления",
    "system": "Ошибки и подсказки",
    "custom": "Дополнительные страницы",
    "other": "Прочее",
}


CONTENT_CATALOG: tuple[tuple[str, str, str], ...] = (
    ("main.menu", "Главное меню", "main"),
    ("system.rate_limit", "Слишком много сообщений", "system"),
    ("system.action_cancelled", "Действие отменено", "system"),
    ("system.stale_button", "Устаревшая кнопка", "system"),
    ("system.feature_disabled", "Раздел временно отключён", "system"),
    ("admin.access_connected", "Доступ администратора подключён", "admin"),
    ("admin.invite_invalid", "Недействительное приглашение", "admin"),
    ("admin.home", "Главная страница панели администратора", "admin"),
    ("admin.denied", "Нет доступа в панель", "admin"),
    ("admin.load_error", "Ошибка загрузки панели", "admin"),
    ("admin.account_missing", "Учётная запись администратора не найдена", "admin"),
    ("admin.action_error", "Ошибка действия администратора", "admin"),
    ("admin.content.home", "Редактор контента", "admin"),
    ("admin.content.list", "Список редактируемых ответов", "admin"),
    ("admin.content.detail", "Карточка редактируемого ответа", "admin"),
    ("admin.content.buttons", "Список кнопок ответа", "admin"),
    ("admin.content.edit_text", "Подсказка редактирования текста", "admin"),
    ("admin.content.edit_images", "Подсказка добавления изображений", "admin"),
    ("admin.content.edit_button", "Редактор кнопки", "admin"),
    ("admin.content.edit_button_text", "Подсказка переименования кнопки", "admin"),
    ("admin.content.add_button_label", "Новая кнопка: название", "admin"),
    ("admin.content.add_button_body", "Новая кнопка: сообщение", "admin"),
    ("admin.content.updated", "Изменения контента применены", "admin"),
    ("admin.content.features", "Управление возможностями", "admin"),
    ("admin.content.preview_return", "Возврат из предпросмотра", "admin"),
    ("admin.export.caption", "Подпись CSV-выгрузки", "admin"),
    ("admin.broadcast.menu", "Рассылка: выбор раздела", "admin"),
    ("admin.broadcast.confirm", "Рассылка: подтверждение", "admin"),
    ("application.review", "Заявка: проверка перед отправкой", "application"),
    ("application.submitted", "Заявка успешно отправлена", "application"),
    ("application.submit_error", "Ошибка отправки заявки", "application"),
    ("catalog.load_error", "Ошибка загрузки материалов", "catalog"),
    ("cities.menu", "Выбор города", "catalog"),
    ("cities.load_error", "Ошибка загрузки городов", "catalog"),
    ("requests.card", "Карточка заявки пользователя", "requests"),
    ("requests.list", "Мои заявки", "requests"),
    ("requests.empty", "У пользователя нет заявок", "requests"),
    ("privacy.about", "О проекте", "privacy"),
    ("privacy.delete_confirm", "Подтверждение удаления данных", "privacy"),
    ("notifications.request_created", "Администратору: новая заявка", "notifications"),
    ("notifications.request_status", "Пользователю: новый статус заявки", "notifications"),
    ("notifications.request_cancelled", "Администратору: заявка отменена", "notifications"),
    ("notifications.user_message", "Администратору: сообщение пользователя", "notifications"),
    ("notifications.admin_message", "Пользователю: сообщение администратора", "notifications"),
    ("notifications.publication", "Пользователю: ручная рассылка публикации", "notifications"),
    ("notifications.reminder", "Администратору: напоминание", "notifications"),
    ("notifications.daily_digest", "Ежедневная сводка", "notifications"),
)

for _step, _title in (
    ("name", "имя"),
    ("company", "компания"),
    ("position", "роль"),
    ("phone", "телефон"),
    ("email", "email"),
    ("message", "рассказ о проекте"),
):
    CONTENT_CATALOG += ((f"application.prompt.{_step}", f"Заявка: {_title}", "application"),)

for _kind, _title in (
    ("latest", "Новое"),
    ("entrepreneurs", "Герои"),
    ("businesses", "Бизнес"),
    ("articles", "Журнал"),
    ("interviews", "Выпуски"),
    ("reels", "Короткие видео"),
):
    CONTENT_CATALOG += (
        (f"catalog.{_kind}.list", f"Каталог «{_title}»: список", "catalog"),
        (f"catalog.{_kind}.empty", f"Каталог «{_title}»: пустой список", "catalog"),
    )


# Feature keys are deliberately independent from button positions and message text.
# This lets an owner hide a feature and also reject callbacks from old messages.
FEATURE_DEFINITIONS: tuple[tuple[str, str, tuple[str, ...]], ...] = (
    ("application", "Заявка на участие", ("apply:",)),
    ("catalog", "Каталог материалов", ("content:",)),
    ("city", "Выбор города", ("city:",)),
    ("requests", "Заявки пользователя", ("requests:", "request:")),
    ("about", "Раздел «О проекте»", ("about",)),
    ("data_deletion", "Удаление данных", ("privacy:",)),
)


def feature_for_callback(callback: str | None) -> str | None:
    if not callback:
        return None
    for feature_key, _title, prefixes in FEATURE_DEFINITIONS:
        if any(callback == prefix or callback.startswith(prefix) for prefix in prefixes):
            return feature_key
    return None


def feature_rows() -> Iterable[tuple[str, str]]:
    for key, title, _prefixes in FEATURE_DEFINITIONS:
        yield key, title
