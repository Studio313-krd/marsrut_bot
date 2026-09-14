"""Excel snapshot of request arrivals and observed administrator actions.

Dates used in duration formulas are UTC. Calendar buckets use the configured timezone.
Cached formula values let data-only readers analyse the export without opening Excel.
"""

from __future__ import annotations

import hashlib
import io
import json
from collections import Counter
from datetime import UTC, date, datetime, time, timedelta
from statistics import mean, median
from typing import Any
from zoneinfo import ZoneInfo

import xlsxwriter
from xlsxwriter.utility import xl_col_to_name

ADMIN_TYPES = {"STATUS_CHANGED", "ASSIGNED", "COMMENT", "MESSAGE_TO_USER", "REMINDER_SET"}
UNAMBIGUOUS_ADMIN_TYPES = ADMIN_TYPES - {"COMMENT"}
REQUEST_HEADERS = [
    "ID заявки",
    "Номер заявки",
    "Создана UTC",
    "Создана (местное время)",
    "Источник",
    "Текущий статус",
    "Компания",
    "Кампания",
    "Назначенный администратор ID",
    "Назначенный администратор",
    "Первое действие UTC",
    "Первый администратор ID",
    "Первый администратор",
    "Канал первого действия",
    "До первого действия, мин",
    "В работе с UTC",
    "Первое завершение UTC",
    "Первый завершивший ID",
    "Первый завершивший",
    "Канал первого завершения",
    "До первого завершения, мин",
    "В работе до первого завершения, мин",
    "Последнее завершение UTC",
    "Последний завершивший ID",
    "Последний завершивший",
    "Канал последнего завершения",
    "Обновлена UTC",
    "Есть событие создания (1/0)",
    "Действий администраторов",
    "Ожидание первого действия, мин",
    "Интервал между заявками, мин",
    "Группа первого администратора",
    "Группа первого завершившего",
]
EVENT_HEADERS = [
    "ID заявки",
    "Номер заявки",
    "ID события",
    "Время UTC",
    "Местное время",
    "Тип события",
    "Автор ID",
    "Автор",
    "Тип автора",
    "Канал действия",
    "Основание атрибуции",
    "Из статуса",
    "В статус",
    "Действие администратора (1/0)",
    "После создания, мин",
    "Группа администратора",
]


def parse_time(value: str | datetime) -> datetime:
    result = value if isinstance(value, datetime) else datetime.fromisoformat(value.replace("Z", "+00:00"))
    if result.tzinfo is None:
        raise ValueError("В статистике получена дата без часового пояса")
    return result.astimezone(UTC)


def _minutes(start: datetime | None, end: datetime | None) -> float | str:
    if start is None or end is None or end < start:
        return ""
    return (end - start).total_seconds() / 60


def _group(actor: dict[str, Any] | None) -> str:
    if not actor:
        return ""
    identity = json.dumps(
        [
            actor.get("actorKey") or "",
            "" if actor.get("actorKey") else actor.get("actorName") or "",
            actor["actorChannel"],
        ],
        ensure_ascii=False,
    )
    return hashlib.sha256(identity.encode()).hexdigest()


def _actor(
    activity: dict[str, Any], bot_actors: dict[str, str], web_actors: dict[str, str]
) -> dict[str, Any]:
    item = dict(activity)
    key = item.get("actorKey") or ""
    kind = item.get("actorType") or "UNKNOWN"
    channel = item.get("actorChannel") or "UNKNOWN"
    evidence = "RECORDED" if kind != "UNKNOWN" and channel != "UNKNOWN" else "UNKNOWN"
    if (
        kind == "UNKNOWN"
        and item["type"] in ADMIN_TYPES
        and (key in bot_actors or key in web_actors or item["type"] in UNAMBIGUOUS_ADMIN_TYPES)
    ):
        kind = "ADMIN"
        evidence = "LEGACY_ACTION_TYPE"
    if kind == "ADMIN" and channel == "UNKNOWN":
        if key in bot_actors and key not in web_actors:
            channel, evidence = "BOT", "LEGACY_BOT_ACCOUNT"
        elif key in web_actors and key not in bot_actors:
            channel, evidence = "WEBSITE", "LEGACY_WEB_ACCOUNT"
    item.update(actorType=kind, actorChannel=channel, attribution=evidence)
    item["actorName"] = (
        (item.get("actorName") or bot_actors.get(key) or web_actors.get(key) or "") if kind == "ADMIN" else ""
    )
    if kind != "ADMIN":
        item["actorKey"] = None
    item["time"] = parse_time(item["createdAt"])
    item["adminAction"] = kind == "ADMIN" and item["type"] in ADMIN_TYPES
    if item["type"] == "STATUS_CHANGED" and item.get("fromStatus") == item.get("toStatus"):
        item["adminAction"] = False
    item["group"] = _group(item) if item["adminAction"] else ""
    return item


def request_facts(
    item: dict[str, Any], bot_actors: dict[str, str], web_actors: dict[str, str]
) -> dict[str, Any]:
    created = parse_time(item["createdAt"])
    activities = sorted(
        (_actor(a, bot_actors, web_actors) for a in item["activities"]), key=lambda a: (a["time"], a["id"])
    )
    actions = [a for a in activities if a["adminAction"] and a["time"] >= created]
    completed = [a for a in actions if a["type"] == "STATUS_CHANGED" and a.get("toStatus") == "COMPLETED"]
    first_completion = completed[0] if completed else None
    work = next(
        (
            a
            for a in actions
            if a["type"] == "STATUS_CHANGED"
            and a.get("toStatus") == "IN_PROGRESS"
            and (not first_completion or a["time"] <= first_completion["time"])
        ),
        None,
    )
    return {
        "item": item,
        "created": created,
        "activities": activities,
        "actions": actions,
        "first": actions[0] if actions else None,
        "work": work,
        "completed": first_completion,
        "lastCompleted": completed[-1] if completed else None,
        "hasCreation": any(a["type"] == "CREATED" for a in activities),
    }


def _percentile(values: list[float], fraction: float) -> float | str:
    if not values:
        return ""
    ordered = sorted(values)
    position = (len(ordered) - 1) * fraction
    lower = int(position)
    return ordered[lower] + (ordered[min(lower + 1, len(ordered) - 1)] - ordered[lower]) * (position - lower)


def build_statistics_workbook(
    payload: dict[str, Any], admins: list[dict[str, Any]], timezone: ZoneInfo
) -> bytes:
    if payload.get("schemaVersion") != 1 or not isinstance(payload.get("items"), list):
        raise ValueError("Сайт вернул неподдерживаемый формат статистики")
    items = payload["items"]
    if payload.get("total") != len(items):
        raise ValueError("Получена неполная выгрузка заявок")
    if sum(len(item.get("activities", [])) for item in items) > 500000:
        raise ValueError("Слишком много событий. Выберите меньший период")
    generated = parse_time(payload["generatedAt"])
    bot_actors = {a["id"]: a["display_name"] for a in admins}
    web_actors = {a["id"]: a["name"] for a in payload.get("webActors", [])}
    facts = sorted(
        (request_facts(item, bot_actors, web_actors) for item in items),
        key=lambda f: (f["created"], f["item"]["id"]),
    )
    first_day = (
        (
            parse_time(payload["period"]["from"])
            if payload["period"].get("from")
            else facts[0]["created"]
            if facts
            else generated
        )
        .astimezone(timezone)
        .date()
    )
    last_day = (parse_time(payload["period"]["to"]) - timedelta(microseconds=1)).astimezone(timezone).date()
    if last_day < first_day or (last_day - first_day).days > 36600:
        raise ValueError("Некорректный или слишком длинный период статистики")
    stream = io.BytesIO()
    workbook = xlsxwriter.Workbook(
        stream, {"in_memory": True, "strings_to_formulas": False, "strings_to_urls": False}
    )
    workbook.set_properties({"title": "Статистика заявок — Маршрут построен", "author": "Маршрут построен"})
    normal = workbook.add_format({"font_name": "Arial", "font_size": 10})
    header = workbook.add_format(
        {
            "font_name": "Arial",
            "bold": True,
            "font_color": "white",
            "bg_color": "#263238",
            "text_wrap": True,
            "valign": "top",
        }
    )
    number = workbook.add_format({"font_name": "Arial", "num_format": "0.00"})
    date_format = workbook.add_format({"font_name": "Arial", "num_format": "yyyy-mm-dd hh:mm:ss"})
    day_format = workbook.add_format({"font_name": "Arial", "num_format": "yyyy-mm-dd"})
    wrap = workbook.add_format({"font_name": "Arial", "text_wrap": True, "valign": "top"})

    def sheet(name: str, headers: list[str]):
        ws = workbook.add_worksheet(name)
        ws.set_default_row(18)
        ws.set_column(0, len(headers) - 1, 24, normal)
        ws.write_row(0, 0, headers, header)
        ws.set_row(0, 48)
        ws.freeze_panes(1, 0)
        return ws

    def write_row(ws, row: int, values: list[Any]):
        for col, value in enumerate(values):
            if isinstance(value, datetime):
                ws.write_datetime(row, col, value.replace(tzinfo=None), date_format)
            elif isinstance(value, date):
                ws.write_datetime(row, col, datetime.combine(value, time.min), day_format)
            elif isinstance(value, str):
                # Always a literal string, even when a company/name begins with '=' or '+'.
                ws.write_string(row, col, value, normal)
            elif value is not None:
                ws.write(row, col, value, number if isinstance(value, float) else normal)

    def formula(ws, row: int, col: int, expression: str, cached: float | int | str):
        ws.write_formula(row, col, expression, number, cached)

    description = sheet("Описание", ["Параметр", "Значение"])
    description.set_column(0, 0, 35, normal)
    description.set_column(1, 1, 110, wrap)
    description.write_datetime(1, 1, generated.replace(tzinfo=None), date_format)
    notes = [
        ("Сформировано UTC", generated),
        ("Часовой пояс календаря", str(timezone)),
        ("Начало выборки (включительно)", payload["period"].get("from") or "Первая существующая заявка"),
        ("Конец выборки (не включительно)", payload["period"]["to"]),
        (
            "Состав",
            "Заявки на участие/съёмку, созданные за выбранный период; вся сохранённая история этих заявок до выгрузки. Обращения по товарному знаку не включены.",
        ),
        (
            "Первое действие",
            "Первое зафиксированное действие администратора: статус, назначение, внутренний комментарий, сообщение заявителю или напоминание. Это скорость реакции, а не доказательство фактического звонка.",
        ),
        (
            "Завершение",
            "Переход в COMPLETED. ARCHIVED и отмена пользователем не считаются завершением. Сохранены первое и последнее завершения, поэтому повторное открытие видно вместе с текущим статусом.",
        ),
        (
            "Длительности",
            "Календарные минуты, включая ночи и выходные. Формулы используют UTC. Пустое значение означает отсутствие необходимого события, а не нулевое время.",
        ),
        (
            "Исполнитель",
            "Автор фактического события; назначенный ответственный показан отдельно и не подменяет исполнителя.",
        ),
        (
            "Каналы",
            "WEBSITE — веб-админка; TELEGRAM/MAX — точный бот; BOT — бот без восстановимого мессенджера; UNKNOWN — недостаточно истории. Источник заявки независим от канала обработки.",
        ),
        (
            "Атрибуция старых событий",
            "RECORDED — записано при действии; LEGACY_BOT_ACCOUNT/LEGACY_WEB_ACCOUNT — восстановлено по ID аккаунта; LEGACY_ACTION_TYPE — роль определена по типу действия; UNKNOWN — неизвестно. Удалённые аккаунты могут не распознаться.",
        ),
        (
            "Ограничения истории",
            "Отсутствие CREATED означает неполную историю с момента создания. Первое наблюдаемое действие не обязательно было первым в действительности. updatedAt не используется как время обработки. Удалённые заявки и действия, не записанные системой, восстановить нельзя.",
        ),
        (
            "Календарные периоды",
            "По дням/неделям/месяцам включены нулевые периоды. Неделя начинается в понедельник. Первый и последний периоды могут быть неполными. Для всего времени наблюдение начинается с первой существующей заявки.",
        ),
        (
            "Администраторы",
            "Средняя реакция относится к заявкам, где администратор сделал первое наблюдаемое действие. Среднее до завершения — к заявкам, которые он первым завершил. Другие участники видны на листе События.",
        ),
        (
            "Персональные данные",
            "Телефоны, email, имя заявителя и тексты переписки не выгружаются. Имена администраторов и компания сохранены для анализа.",
        ),
        (
            "Параметры формул",
            "1440 минут в сутках; P90 использует долю 0.9. Расчётные результаты сохранены в файле, поэтому его можно анализировать без открытия Excel. Исходные данные находятся на листах Заявки и События.",
        ),
        (
            "Источник",
            "PostgreSQL сайта /api/integrations/bot/exports/request-statistics; каталог администраторов бота SQLite. Версия схемы 1.",
        ),
    ]
    for row, (label, value) in enumerate(notes, 1):
        description.write_string(row, 0, label, normal)
        if isinstance(value, datetime):
            description.write_datetime(row, 1, value.replace(tzinfo=None), date_format)
        else:
            description.write_string(row, 1, value, wrap)
        description.set_row(row, 55 if row >= 5 else 24)

    summary = sheet("Сводка", ["Показатель", "Значение", "Пояснение"])
    requests = sheet("Заявки", REQUEST_HEADERS)
    events = sheet("События", EVENT_HEADERS)
    requests.set_column(31, 32, 24, normal, {"hidden": True})
    events.set_column(15, 15, 24, normal, {"hidden": True})
    event_row = 1
    groups: dict[str, dict[str, Any]] = {}
    reactions: list[float] = []
    completions: list[float] = []
    gaps: list[float] = []
    for row, fact in enumerate(facts, 1):
        item, created = fact["item"], fact["created"]
        first, work, completed, last = (
            fact[k] or {} for k in ("first", "work", "completed", "lastCompleted")
        )
        reaction = _minutes(created, first.get("time"))
        completion = _minutes(created, completed.get("time"))
        if isinstance(reaction, float):
            reactions.append(reaction)
        if isinstance(completion, float):
            completions.append(completion)
        values = [
            item["id"],
            item["requestNumber"],
            created,
            created.astimezone(timezone),
            item["source"],
            item["status"],
            item.get("company"),
            item.get("campaign"),
            item.get("assignedAdminKey"),
            item.get("assignedAdminName"),
            first.get("time"),
            first.get("actorKey"),
            first.get("actorName"),
            first.get("actorChannel"),
            None,
            work.get("time"),
            completed.get("time"),
            completed.get("actorKey"),
            completed.get("actorName"),
            completed.get("actorChannel"),
            None,
            None,
            last.get("time"),
            last.get("actorKey"),
            last.get("actorName"),
            last.get("actorChannel"),
            parse_time(item["updatedAt"]),
            int(fact["hasCreation"]),
            None,
            None,
            None,
            _group(first),
            _group(completed),
        ]
        write_row(requests, row, values)
        r = row + 1
        for col, start, end, cached in [
            (14, "C", "K", reaction),
            (20, "C", "Q", completion),
            (21, "P", "Q", _minutes(work.get("time"), completed.get("time"))),
        ]:
            formula(
                requests,
                row,
                col,
                f'=IF(OR({start}{r}="",{end}{r}="",{end}{r}<{start}{r}),"",({end}{r}-{start}{r})*1440)',
                cached,
            )
        waiting = (
            _minutes(created, generated)
            if not first and fact["hasCreation"] and item["status"] in {"NEW", "IN_PROGRESS"}
            else ""
        )
        formula(
            requests,
            row,
            29,
            f'=IF(AND(K{r}="",AB{r}=1,OR(F{r}="NEW",F{r}="IN_PROGRESS")),MAX(0,(\'Описание\'!$B$2-C{r})*1440),"")',
            waiting,
        )
        if row > 1:
            gap = _minutes(facts[row - 2]["created"], created)
            formula(requests, row, 30, f"=(C{r}-C{r - 1})*1440", gap)
            if isinstance(gap, float):
                gaps.append(gap)
        start_event_row = event_row
        for activity in fact["activities"]:
            write_row(
                events,
                event_row,
                [
                    item["id"],
                    item["requestNumber"],
                    activity["id"],
                    activity["time"],
                    activity["time"].astimezone(timezone),
                    activity["type"],
                    activity.get("actorKey"),
                    activity.get("actorName"),
                    activity["actorType"],
                    activity["actorChannel"],
                    activity["attribution"],
                    activity.get("fromStatus"),
                    activity.get("toStatus"),
                    int(activity["adminAction"]),
                    None,
                    activity["group"],
                ],
            )
            formula(
                events,
                event_row,
                14,
                f"=(D{event_row + 1}-'Заявки'!C{r})*1440",
                (activity["time"] - created).total_seconds() / 60,
            )
            if activity["adminAction"]:
                group = groups.setdefault(
                    activity["group"], {"actor": activity, "actions": 0, "reactions": [], "completions": []}
                )
                group["actions"] += 1
            event_row += 1
        formula(
            requests,
            row,
            28,
            f"=SUM('События'!N{start_event_row + 1}:N{event_row})" if event_row > start_event_row else "=0",
            sum(a["adminAction"] for a in fact["activities"]),
        )
        if first:
            groups[first["group"]]["reactions"].append(reaction)
        if completed:
            groups[completed["group"]]["completions"].append(completion)

    last_request = max(2, len(facts) + 1)
    last_event = max(2, event_row)
    requests.autofilter(0, 0, max(1, len(facts)), len(REQUEST_HEADERS) - 1)
    events.autofilter(0, 0, max(1, event_row - 1), len(EVENT_HEADERS) - 1)

    def request_range(column: str) -> str:
        return f"'Заявки'!${column}$2:${column}${last_request}"

    def event_range(column: str) -> str:
        return f"'События'!${column}$2:${column}${last_event}"

    daily_counts = Counter(f["created"].astimezone(timezone).date() for f in facts)
    source_counts = Counter((f["created"].astimezone(timezone).date(), f["item"]["source"]) for f in facts)
    for title, cadence in [("По дням", "day"), ("По неделям", "week"), ("По месяцам", "month")]:
        ws = sheet(
            title, ["Начало периода", "Конец (не включительно)", "Всего заявок", "WEBSITE", "TELEGRAM", "MAX"]
        )
        current = (
            first_day
            if cadence == "day"
            else first_day - timedelta(days=first_day.weekday())
            if cadence == "week"
            else first_day.replace(day=1)
        )
        row = 1
        while current <= last_day:
            following = (
                current + timedelta(days=1 if cadence == "day" else 7)
                if cadence != "month"
                else (current.replace(day=28) + timedelta(days=4)).replace(day=1)
            )
            write_row(ws, row, [current, following])
            days = [current + timedelta(days=i) for i in range((following - current).days)]
            count = sum(daily_counts[d] for d in days)
            r = row + 1
            base = f'{request_range("D")},">="&A{r},{request_range("D")},"<"&B{r}'
            formula(ws, row, 2, f"=COUNTIFS({base})", count)
            for col, source in enumerate(("WEBSITE", "TELEGRAM", "MAX"), 3):
                formula(
                    ws,
                    row,
                    col,
                    f"=COUNTIFS({base},{request_range('E')},{xl_col_to_name(col)}$1)",
                    sum(source_counts[(d, source)] for d in days),
                )
            current, row = following, row + 1
        ws.autofilter(0, 0, row - 1, 5)

    admin_sheet = sheet(
        "Администраторы",
        [
            "ID администратора",
            "Имя администратора",
            "Канал",
            "Действий",
            "Первых реакций",
            "Первых завершений",
            "Средняя реакция, мин",
            "Среднее до завершения, мин",
            "Группа",
        ],
    )
    admin_sheet.set_column(8, 8, 24, normal, {"hidden": True})
    for row, (key, group) in enumerate(sorted(groups.items()), 1):
        actor = group["actor"]
        write_row(
            admin_sheet,
            row,
            [
                actor.get("actorKey"),
                actor.get("actorName") or "Неизвестен",
                actor["actorChannel"],
                None,
                None,
                None,
                None,
                None,
                key,
            ],
        )
        r = row + 1
        formula(admin_sheet, row, 3, f"=COUNTIF({event_range('P')},I{r})", group["actions"])
        for count_col, average_col, match_col, metric_col, values in [
            (4, 6, "AF", "O", group["reactions"]),
            (5, 7, "AG", "U", group["completions"]),
        ]:
            formula(admin_sheet, row, count_col, f"=COUNTIF({request_range(match_col)},I{r})", len(values))
            formula(
                admin_sheet,
                row,
                average_col,
                f'=IFERROR(AVERAGEIF({request_range(match_col)},I{r},{request_range(metric_col)}),"")',
                mean(values) if values else "",
            )
    admin_sheet.autofilter(0, 0, max(1, len(groups)), 8)

    days = (last_day - first_day).days + 1
    metrics = [
        ("Заявок в выборке", f"=COUNTA({request_range('A')})", len(facts), "По дате создания"),
        (
            "Календарных дней наблюдения",
            f"=ROWS('По дням'!A2:A{days + 1})",
            days,
            "Включая дни без заявок; крайние дни могут быть неполными",
        ),
        (
            "В среднем заявок в день",
            "=IFERROR(B2/B3,0)",
            len(facts) / days,
            "За календарный период наблюдения",
        ),
        (
            "Средний интервал между заявками, мин",
            f'=IFERROR(AVERAGE({request_range("AE")}),"")',
            mean(gaps) if gaps else "",
            "Только между соседними заявками внутри выборки",
        ),
        (
            "С зафиксированной реакцией",
            f"=COUNT({request_range('K')})",
            len(reactions),
            "Первое наблюдаемое действие администратора",
        ),
        (
            "Средняя реакция, мин",
            f'=IFERROR(AVERAGE({request_range("O")}),"")',
            mean(reactions) if reactions else "",
            "Только заявки с известным временем действия",
        ),
        (
            "Медиана реакции, мин",
            f'=IFERROR(MEDIAN({request_range("O")}),"")',
            median(reactions) if reactions else "",
            "Неизвестные времена исключены",
        ),
        (
            "P90 реакции, мин",
            f'=IFERROR(PERCENTILE({request_range("O")},0.9),"")',
            _percentile(reactions, 0.9),
            "90-й процентиль с линейной интерполяцией",
        ),
        (
            "С зафиксированным завершением",
            f"=COUNT({request_range('Q')})",
            len(completions),
            "Первое наблюдаемое завершение, включая позже переоткрытые заявки",
        ),
        (
            "Медиана до завершения, мин",
            f'=IFERROR(MEDIAN({request_range("U")}),"")',
            median(completions) if completions else "",
            "От создания до первого COMPLETED",
        ),
        (
            "Без события создания",
            f"=COUNTA({request_range('A')})-SUM({request_range('AB')})",
            sum(not f["hasCreation"] for f in facts),
            "История с создания неполная; метрики могут быть неполными",
        ),
    ]
    for status in ("NEW", "IN_PROGRESS", "COMPLETED", "ARCHIVED"):
        metrics.append(
            (
                f"Текущий статус {status}",
                f'=COUNTIF({request_range("F")},"{status}")',
                sum(f["item"]["status"] == status for f in facts),
                "Состояние на момент выгрузки",
            )
        )
    for row, (label, expression, cached, note) in enumerate(metrics, 1):
        write_row(summary, row, [label, None, note])
        formula(summary, row, 1, expression, cached)
    summary.set_column(0, 0, 43, normal)
    summary.set_column(1, 1, 23, number)
    summary.set_column(2, 2, 80, wrap)
    workbook.close()
    content = stream.getvalue()
    if len(content) > 45 * 1024 * 1024:
        raise ValueError("Файл слишком большой для отправки. Выберите меньший период")
    return content
