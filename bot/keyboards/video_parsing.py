"""
Клавиатуры для раздела парсинга видео.
"""
from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton
from typing import List, Dict


def _shorten_button_text(value: str, max_len: int = 20) -> str:
    """Truncate to fit inline-keyboard buttons with status/interval/mode suffixes.

    With ~7 chars overhead (status emoji, interval label, mode emoji, separators),
    a 28-char username pushed total length past ~50, causing Telegram to wrap or
    cut buttons unpredictably on smaller screens. 20 keeps the username readable
    while leaving room for suffixes.
    """
    value = str(value or "")
    if len(value) <= max_len:
        return value
    return value[: max(1, max_len - 1)] + "…"


def get_video_parsing_menu_keyboard(
    new_count: int = 0,
    total_creators: int = 0,
    enabled_creators: int = 0,
    autopilot_count: int = 0,
    parsing_mode: bool = False,
    notifications_enabled: bool = True,
) -> InlineKeyboardMarkup:
    notify_button = InlineKeyboardButton(
        text="🔕 Выключить уведы" if notifications_enabled else "🔔 Включить уведы",
        callback_data="vp:notifications:toggle",
    )
    if parsing_mode:
        inbox_text = f"📥 Новые видео ({new_count})" if new_count > 0 else "📥 Новые видео"
        return InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text=inbox_text, callback_data="vp:parsing_inbox")],
            [InlineKeyboardButton(text="👤 Блогеры", callback_data="vp:list")],
            [InlineKeyboardButton(text="🤖 Автокомментинг", callback_data="ap:main")],
            [InlineKeyboardButton(text="➕ Добавить блогера", callback_data="vp:add")],
            [notify_button],
            [InlineKeyboardButton(text="◀️ Назад", callback_data="menu:main")],
        ])
    """Главное меню раздела мониторинга видео."""
    keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(
                text=f"📥 Входящие ({new_count})" if new_count > 0 else "📥 Входящие",
                callback_data="vp:inbox"
            )
        ],
        [
            InlineKeyboardButton(text="👤 Блогеры", callback_data="vp:list")
        ],
        [
            InlineKeyboardButton(text="⚙️ Массовые действия", callback_data="vp:bulk")
        ],
        [
            InlineKeyboardButton(text="🤖 Автокомментинг", callback_data="ap:main")
        ],
        [
            InlineKeyboardButton(text="➕ Добавить блогера", callback_data="vp:add")
        ],
        # УБРАНО: кнопка "📑 Шаблоны" → menu:templates. Этот же раздел доступен
        # из главного меню; дублировать его в парсинге видео незачем — внутри
        # карточки блогера всё равно есть контекстные кнопки "🧩 Список шаблонов"
        # и "⭐ Шаблон авто-ответа", которые управляют шаблонами для конкретного
        # блогера, а не открывают общий список.
        [
            notify_button
        ],
        [
            InlineKeyboardButton(text="◀️ Назад", callback_data="menu:main")
        ]
    ])
    return keyboard


def get_creator_keyboard(
    creator_id: int,
    enabled: bool,
    new_videos_count: int = 0,
    parsing_mode: bool = False,
    adaptive_enabled: bool = False,
) -> InlineKeyboardMarkup:
    """Клавиатура управления блогером."""
    if parsing_mode:
        action_row = []
        if not adaptive_enabled:
            action_row.append(InlineKeyboardButton(text="⏱ Интервал", callback_data=f"vp:creator:{creator_id}:interval"))
        action_row.append(InlineKeyboardButton(text="▶️ Запустить сейчас", callback_data=f"vp:creator:{creator_id}:runnow"))

        keyboard = [
            [
                InlineKeyboardButton(
                    text="⏸ Выключить" if enabled else "▶️ Включить",
                    callback_data=f"vp:creator:{creator_id}:toggle"
                )
            ],
            action_row,
            [InlineKeyboardButton(text="🗑 Удалить блогера", callback_data=f"vp:creator:{creator_id}:delete")],
            [InlineKeyboardButton(text="◀️ Назад", callback_data="vp:list")],
        ]
        return InlineKeyboardMarkup(inline_keyboard=[row for row in keyboard if row])
    keyboard = [
        [
            InlineKeyboardButton(
                text="⏸ Выключить" if enabled else "▶️ Включить",
                callback_data=f"vp:creator:{creator_id}:toggle"
            ),
            InlineKeyboardButton(
                text=f"🆕 Новые ({new_videos_count})" if new_videos_count > 0 else "🆕 Новые видео",
                callback_data=f"vp:creator:{creator_id}:new_videos"
            )
        ],
        (
            [
                InlineKeyboardButton(text="📊 Режим", callback_data=f"vp:creator:{creator_id}:mode"),
                InlineKeyboardButton(text="⏱ Интервал", callback_data=f"vp:creator:{creator_id}:interval"),
            ]
            if not adaptive_enabled
            else [
                InlineKeyboardButton(text="📊 Режим", callback_data=f"vp:creator:{creator_id}:mode"),
            ]
        ),
        [
            InlineKeyboardButton(text="🧩 Список шаблонов", callback_data=f"vp:creator:{creator_id}:templates"),
            InlineKeyboardButton(text="▶️ Запустить сейчас", callback_data=f"vp:creator:{creator_id}:runnow")
        ],
        [
            InlineKeyboardButton(text="⭐ Шаблон авто-ответа", callback_data=f"vp:creator:{creator_id}:default_template")
        ],
        [
            InlineKeyboardButton(text="🧹 Отменить очередь авто", callback_data=f"vp:c:{creator_id}:apc")
        ],
        [
            InlineKeyboardButton(text="🗑 Удалить блогера", callback_data=f"vp:creator:{creator_id}:delete")
        ],
        [
            InlineKeyboardButton(text="◀️ Назад", callback_data="vp:list")
        ]
    ]
    return InlineKeyboardMarkup(inline_keyboard=keyboard)


def get_creators_list_keyboard(
    creators: List[Dict],
    page: int = 0,
    per_page: int = 8,
    *,
    adaptive_enabled: bool = False,
    parsing_mode: bool = False,
) -> InlineKeyboardMarkup:
    """Клавиатура со списком блогеров."""
    keyboard = []

    total = len(creators)
    max_page = max(0, (total - 1) // per_page) if total else 0
    page = max(0, min(page, max_page))
    start_idx = page * per_page
    end_idx = start_idx + per_page
    page_creators = creators[start_idx:end_idx]

    for creator in page_creators:
        status_emoji = "✅" if creator.get("enabled") else "⏸"
        username = creator.get("username", "unknown")
        new_count = creator.get("new_videos_count", 0)
        mode = creator.get("mode", "manual_review")

        # In parsing-mode the only meaningful mode is "notify_only" — even if the
        # DB row still has auto_comment/manual_review from before mode-switch,
        # surface 🔔 so the UI doesn't lie about what the bot will actually do.
        if parsing_mode:
            mode_emoji = "🔔"
        else:
            mode_emoji = {
                "auto_comment": "🤖",
                "manual_review": "👀",
                "notify_only": "🔔",
            }.get(mode, "👀")

        button_text = f"{status_emoji} @{_shorten_button_text(username)}"
        if new_count > 0:
            button_text += f" · 🆕 {new_count}"
        interval_label = "авто" if adaptive_enabled else f"{creator.get('interval_minutes', 30)}м"
        button_text += f" · {interval_label} {mode_emoji}"

        keyboard.append([
            InlineKeyboardButton(
                text=button_text,
                callback_data=f"vp:creator:{creator['id']}:detail"
            )
        ])

    nav_buttons = []
    if page > 0:
        nav_buttons.append(InlineKeyboardButton(text="◀️", callback_data=f"vp:list:page:{page - 1}"))
    nav_buttons.append(InlineKeyboardButton(text=f"{page + 1}/{max_page + 1}", callback_data="vp:list:noop"))
    if end_idx < len(creators):
        nav_buttons.append(InlineKeyboardButton(text="▶️", callback_data=f"vp:list:page:{page + 1}"))

    keyboard.append(nav_buttons)

    keyboard.append([InlineKeyboardButton(text="🔍 Найти блогера", callback_data="vp:list:search")])
    if not parsing_mode:
        keyboard.append([InlineKeyboardButton(text="⚙️ Массовые действия", callback_data="vp:bulk")])
    keyboard.append([InlineKeyboardButton(text="◀️ Назад", callback_data="vp:menu")])

    return InlineKeyboardMarkup(inline_keyboard=keyboard)


def get_bulk_actions_keyboard(parsing_mode: bool = False) -> InlineKeyboardMarkup:
    if parsing_mode:
        return InlineKeyboardMarkup(
            inline_keyboard=[
                [InlineKeyboardButton(text="◀️ Назад", callback_data="vp:menu")],
            ]
        )

    rows: list[list[InlineKeyboardButton]] = []
    if not parsing_mode:
        rows.extend(
            [
                [InlineKeyboardButton(text="🤖 Включить авто + шаблон", callback_data="vp:bulk:a:auto_tpl")],
                [InlineKeyboardButton(text="⭐ Назначить шаблон", callback_data="vp:bulk:a:set_tpl")],
                [
                    InlineKeyboardButton(text="👀 Ручная проверка", callback_data="vp:bulk:a:manual"),
                    InlineKeyboardButton(text="🔔 Только уведомления", callback_data="vp:bulk:a:notify"),
                ],
                [InlineKeyboardButton(text="🧹 Убрать шаблон", callback_data="vp:bulk:a:clear_tpl")],
            ]
        )
    rows.extend(
        [
            [
                InlineKeyboardButton(text="▶️ Включить", callback_data="vp:bulk:a:enable"),
                InlineKeyboardButton(text="⏸ Выключить", callback_data="vp:bulk:a:disable"),
            ],
            [InlineKeyboardButton(text="⏱ Интервал проверки", callback_data="vp:bulk:a:interval")],
            [InlineKeyboardButton(text="🔄 Проверить сейчас", callback_data="vp:bulk:a:run_now")],
            [InlineKeyboardButton(text="◀️ Назад", callback_data="vp:menu")],
        ]
    )
    return InlineKeyboardMarkup(inline_keyboard=rows)


def get_bulk_scopes_keyboard(action: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="Все блогеры", callback_data=f"vp:bulk:s:{action}:all")],
            [InlineKeyboardButton(text="Только включенные", callback_data=f"vp:bulk:s:{action}:enabled")],
            [InlineKeyboardButton(text="Только выключенные", callback_data=f"vp:bulk:s:{action}:disabled")],
            [InlineKeyboardButton(text="Без шаблона", callback_data=f"vp:bulk:s:{action}:without_default_template")],
            [InlineKeyboardButton(text="С новыми видео", callback_data=f"vp:bulk:s:{action}:with_new_videos")],
            [
                InlineKeyboardButton(text="🤖 Уже авто", callback_data=f"vp:bulk:s:{action}:auto_comment"),
                InlineKeyboardButton(text="👀 Ручные", callback_data=f"vp:bulk:s:{action}:manual_review"),
            ],
            [InlineKeyboardButton(text="🔔 Только уведомления", callback_data=f"vp:bulk:s:{action}:notify_only")],
            [InlineKeyboardButton(text="◀️ Назад", callback_data="vp:bulk")],
        ]
    )


def get_bulk_template_keyboard(action: str, scope: str, templates: list[dict], page: int = 0, per_page: int = 8) -> InlineKeyboardMarkup:
    rows: list[list[InlineKeyboardButton]] = []
    total_pages = max(1, (len(templates) + per_page - 1) // per_page)
    page = max(0, min(page, total_pages - 1))
    start = page * per_page
    for template in templates[start:start + per_page]:
        template_id = template.get("id")
        name = _shorten_button_text(template.get("name") or f"#{template_id}", 28)
        rows.append(
            [
                InlineKeyboardButton(
                    text=f"⭐ {name}",
                    callback_data=f"vp:bulk:t:{action}:{scope}:{template_id}",
                )
            ]
        )
    if total_pages > 1:
        nav: list[InlineKeyboardButton] = []
        if page > 0:
            nav.append(InlineKeyboardButton(text="◀️", callback_data=f"vp:bulk:tp:{action}:{scope}:{page - 1}"))
        if page < total_pages - 1:
            nav.append(InlineKeyboardButton(text="▶️", callback_data=f"vp:bulk:tp:{action}:{scope}:{page + 1}"))
        if nav:
            rows.append(nav)
    rows.append([InlineKeyboardButton(text="◀️ Назад", callback_data=f"vp:bulk:a:{action}")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def get_bulk_interval_keyboard(action: str, scope: str) -> InlineKeyboardMarkup:
    rows: list[list[InlineKeyboardButton]] = [
        [
            InlineKeyboardButton(text="5 мин", callback_data=f"vp:bulk:i:{action}:{scope}:5"),
            InlineKeyboardButton(text="15 мин", callback_data=f"vp:bulk:i:{action}:{scope}:15"),
            InlineKeyboardButton(text="30 мин", callback_data=f"vp:bulk:i:{action}:{scope}:30"),
        ],
        [
            InlineKeyboardButton(text="60 мин", callback_data=f"vp:bulk:i:{action}:{scope}:60"),
            InlineKeyboardButton(text="120 мин", callback_data=f"vp:bulk:i:{action}:{scope}:120"),
            InlineKeyboardButton(text="240 мин", callback_data=f"vp:bulk:i:{action}:{scope}:240"),
        ],
        [InlineKeyboardButton(text="◀️ Назад", callback_data=f"vp:bulk:a:{action}")],
    ]
    return InlineKeyboardMarkup(inline_keyboard=rows)


def get_bulk_confirm_keyboard(action: str, scope: str, template_id: int | None = None) -> InlineKeyboardMarkup:
    template_part = f":{template_id}" if template_id is not None else ""
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="✅ Применить", callback_data=f"vp:bulk:c:{action}:{scope}{template_part}")],
            [InlineKeyboardButton(text="◀️ Назад", callback_data=f"vp:bulk:a:{action}")],
            [InlineKeyboardButton(text="Отмена", callback_data="vp:bulk")],
        ]
    )


def get_mode_selection_keyboard(creator_id: int) -> InlineKeyboardMarkup:
    """Клавиатура выбора режима работы."""
    keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text="🤖 Авто-комментирование", callback_data=f"vp:creator:{creator_id}:mode:set:auto_comment")
        ],
        [
            InlineKeyboardButton(text="👀 Ручная проверка", callback_data=f"vp:creator:{creator_id}:mode:set:manual_review")
        ],
        [
            InlineKeyboardButton(text="🔔 Только уведомления", callback_data=f"vp:creator:{creator_id}:mode:set:notify_only")
        ],
        [
            InlineKeyboardButton(text="◀️ Назад", callback_data=f"vp:creator:{creator_id}:detail")
        ]
    ])
    return keyboard


def get_review_video_keyboard(video_id: int) -> InlineKeyboardMarkup:
    """Клавиатура для видео на проверке."""
    keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text="✅ Комментировать", callback_data=f"vp:review:{video_id}:approve")
        ],
        [
            InlineKeyboardButton(text="❌ Пропустить", callback_data=f"vp:review:{video_id}:reject")
        ],
        [
            InlineKeyboardButton(text="◀️ Назад", callback_data="vp:review:list")
        ]
    ])
    return keyboard


def get_review_queue_keyboard(videos: List[Dict], page: int = 0, per_page: int = 5) -> InlineKeyboardMarkup:
    """Клавиатура очереди на проверку с пагинацией."""
    keyboard = []

    start_idx = page * per_page
    end_idx = start_idx + per_page
    page_videos = videos[start_idx:end_idx]

    for video in page_videos:
        username = video.get("creator_username", "unknown")
        keyboard.append([
            InlineKeyboardButton(
                text=f"@{username} · {video.get('video_id', 'unknown')[:12]}…",
                callback_data=f"vp:review:{video['id']}:detail"
            )
        ])

    nav_buttons = []
    if page > 0:
        nav_buttons.append(InlineKeyboardButton(text="◀️", callback_data=f"vp:review:list:page:{page-1}"))
    if end_idx < len(videos):
        nav_buttons.append(InlineKeyboardButton(text="▶️", callback_data=f"vp:review:list:page:{page+1}"))

    if nav_buttons:
        keyboard.append(nav_buttons)

    keyboard.append([InlineKeyboardButton(text="◀️ Назад", callback_data="vp:menu")])

    return InlineKeyboardMarkup(inline_keyboard=keyboard)
