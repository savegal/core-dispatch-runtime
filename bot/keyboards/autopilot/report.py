from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

PERIODS = (("1h", "1ч"), ("24h", "24ч"), ("7d", "7д"), ("30d", "30д"), ("all", "всё"))
TABS = (
    ("summary", "📊 Сводка"),
    ("list", "📝 Заказы"),
    ("creators", "👥 Блогеры"),
    ("finance", "💰 Финансы"),
)
STATUS_FILTERS = (
    ("all", "Все"),
    ("pending", "⏳Очередь"),
    ("in_progress", "🔄В работе"),
    ("done", "✅Готово"),
    ("review", "🟠Проверить"),
    ("partial", "🟡Частично"),
    ("error", "🔴Ошибки"),
    ("stopped", "⏹Отменено"),
)


def _mark(label: str, active: bool) -> str:
    return f"• {label}" if active else label


def get_report_keyboard(
    *,
    period: str = "24h",
    tab: str = "summary",
    status: str = "all",
    page: int = 0,
    pages: int = 1,
    creator_filter: int | None = None,
    creator_rows: list[dict] | None = None,
    task_rows: list[dict] | None = None,
    show_archive: bool = True,
) -> InlineKeyboardMarkup:
    rows: list[list[InlineKeyboardButton]] = [
        [
            InlineKeyboardButton(text=_mark(label, key == period), callback_data=f"ap:rep:period:{key}")
            for key, label in PERIODS
        ],
        [
            InlineKeyboardButton(text=_mark(label, key == tab), callback_data=f"ap:rep:tab:{key}")
            for key, label in TABS
        ],
    ]

    if tab == "list":
        if creator_filter:
            rows.append([InlineKeyboardButton(text="Все блогеры", callback_data="ap:rep:creator:0")])
        rows.append(
            [
                InlineKeyboardButton(text=_mark(label, key == status), callback_data=f"ap:rep:list:f:{key}")
                for key, label in STATUS_FILTERS[:4]
            ]
        )
        rows.append(
            [
                InlineKeyboardButton(text=_mark(label, key == status), callback_data=f"ap:rep:list:f:{key}")
                for key, label in STATUS_FILTERS[4:]
            ]
        )
        if task_rows:
            rows.append(
                [
                    InlineKeyboardButton(text=f"#{int(row.get('task_id') or 0)}", callback_data=f"ap:card:{int(row.get('task_id') or 0)}")
                    for row in task_rows[:5]
                    if int(row.get("task_id") or 0) > 0
                ]
            )
            rows.append([InlineKeyboardButton(text="🔗 Открыть 5 видео", callback_data="ap:rep:open5")])

    if tab == "creators" and creator_rows:
        for row in creator_rows[:5]:
            creator_id = int(row.get("creator_id") or 0)
            username = str(row.get("username") or f"id {creator_id}").lstrip("@")
            rows.append([InlineKeyboardButton(text=f"@{username} · Заказы →", callback_data=f"ap:rep:creator:{creator_id}")])

    if tab in {"list", "creators"} and pages > 1:
        prev_page = max(0, page - 1)
        next_page = min(max(0, pages - 1), page + 1)
        page_prefix = "ap:rep:cre:p" if tab == "creators" else "ap:rep:list:p"
        rows.append(
            [
                InlineKeyboardButton(text="◀️", callback_data=f"{page_prefix}:{prev_page}"),
                InlineKeyboardButton(text=f"{page + 1}/{pages}", callback_data="ap:rep:noop"),
                InlineKeyboardButton(text="▶️", callback_data=f"{page_prefix}:{next_page}"),
            ]
        )

    if show_archive and tab != "finance":
        rows.append([InlineKeyboardButton(text="🗃 Архивировать готовые", callback_data="ap:action:archive:ask")])

    rows.append(
        [
            InlineKeyboardButton(text="🔄 Обновить", callback_data="ap:rep"),
            InlineKeyboardButton(text="◀️ Назад", callback_data="ap:main"),
        ]
    )
    return InlineKeyboardMarkup(inline_keyboard=rows)
