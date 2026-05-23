from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup


def get_main_keyboard(
    *,
    mode: str = "active",
    state: str = "ok",
    has_incident: bool = False,
    notify_cooldown_minutes: int = 0,
    can_skip_queue: bool = False,
) -> InlineKeyboardMarkup:
    rows: list[list[InlineKeyboardButton]] = []

    if has_incident or state in {"warning", "down"}:
        if notify_cooldown_minutes > 0:
            text = f"📞 Можно снова через {notify_cooldown_minutes} мин"
            rows.append([InlineKeyboardButton(text=text, callback_data="ap:main:refresh")])
        else:
            rows.append([InlineKeyboardButton(text="📞 Уведомить админа", callback_data="ap:main:notify_admin")])

    if state == "down":
        if can_skip_queue:
            rows.append([
                InlineKeyboardButton(text="⏭ Пропустить", callback_data="ap:main:skipq"),
                InlineKeyboardButton(text="📋 Журнал", callback_data="ap:rep"),
            ])
        else:
            rows.append([InlineKeyboardButton(text="📋 Журнал", callback_data="ap:rep")])
        rows.extend(
            [
                [
                    InlineKeyboardButton(text="🔄 Обновить", callback_data="ap:main:refresh"),
                    InlineKeyboardButton(text="◀️ Назад", callback_data="vp:menu"),
                ],
            ]
        )
        return InlineKeyboardMarkup(inline_keyboard=rows)

    mode_button = InlineKeyboardButton(
        text="▶️ Возобновить" if mode in {"paused", "drain"} else "⏸ Пауза",
        callback_data="ap:limits:mode:active" if mode in {"paused", "drain"} else "ap:limits:mode:paused",
    )
    compact_warning = bool(has_incident or state == "warning")
    current_button = InlineKeyboardButton(text="🧰 Работа", callback_data="ap:w")
    limits_button = InlineKeyboardButton(text="⚙️ Лимиты", callback_data="ap:limits")
    rows.append([mode_button, current_button if compact_warning and can_skip_queue else limits_button])
    if can_skip_queue:
        if not compact_warning:
            rows.append([InlineKeyboardButton(text="🧰 Работа", callback_data="ap:w")])
        rows.append([
            InlineKeyboardButton(text="⏭ Пропустить", callback_data="ap:main:skipq"),
            InlineKeyboardButton(text="📋 Журнал", callback_data="ap:rep"),
        ])
        rows.append([InlineKeyboardButton(text="◀️ Назад", callback_data="vp:menu")])
    else:
        rows.append([
            InlineKeyboardButton(text="🧰 Работа", callback_data="ap:w"),
            InlineKeyboardButton(text="📋 Журнал", callback_data="ap:rep"),
        ])
        if compact_warning:
            rows.append([InlineKeyboardButton(text="◀️ Назад", callback_data="vp:menu")])
        else:
            rows.append([
                InlineKeyboardButton(text="🔄 Обновить", callback_data="ap:main:refresh"),
                InlineKeyboardButton(text="◀️ Назад", callback_data="vp:menu"),
            ])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def get_notify_admin_confirm_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="✅ Да, уведомить", callback_data="ap:action:notify_admin:do")],
            [InlineKeyboardButton(text="❌ Отмена", callback_data="ap:main")],
        ]
    )
