from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup


WORK_TABS = (("plan", "🗓 Планируется"), ("run", "▶️ В работе"), ("done", "✅ Обработано"))


def _tab_row(active_tab: str) -> list[InlineKeyboardButton]:
    return [
        InlineKeyboardButton(
            text=f"• {label}" if key == active_tab else label,
            callback_data=f"ap:w:t:{key}",
        )
        for key, label in WORK_TABS
    ]


def _pagination_row(tab: str, page: int, pages: int) -> list[InlineKeyboardButton] | None:
    if pages <= 1:
        return None
    prev_page = max(0, page - 1)
    next_page = min(max(0, pages - 1), page + 1)
    return [
        InlineKeyboardButton(text="◀️", callback_data=f"ap:w:p:{tab}:{prev_page}"),
        InlineKeyboardButton(text=f"{page + 1}/{pages}", callback_data="ap:w:no"),
        InlineKeyboardButton(text="▶️", callback_data=f"ap:w:p:{tab}:{next_page}"),
    ]


def get_work_keyboard(
    *,
    tab: str = "plan",
    page: int = 0,
    pages: int = 1,
    items: list[dict] | None = None,
) -> InlineKeyboardMarkup:
    rows: list[list[InlineKeyboardButton]] = [_tab_row(tab)]
    items = items or []

    if tab == "plan":
        cancel_buttons: list[InlineKeyboardButton] = []
        for index, item in enumerate(items[:10], start=1):
            video_id = item.get("video_id")
            task_id = item.get("task_id")
            if video_id:
                cancel_buttons.append(InlineKeyboardButton(text=f"✖ {index}", callback_data=f"ap:w:cv:{int(video_id)}"))
            elif task_id:
                cancel_buttons.append(InlineKeyboardButton(text=f"✖ {index}", callback_data=f"ap:w:ct:{int(task_id)}"))
        for start in range(0, len(cancel_buttons), 5):
            rows.append(cancel_buttons[start : start + 5])
    elif tab == "run":
        for item in items[:10]:
            task_id = int(item.get("task_id") or 0)
            if task_id > 0:
                rows.append([InlineKeyboardButton(text=f"⏹ Остановить #{task_id}", callback_data=f"ap:w:st:{task_id}")])
    elif tab == "done":
        card_buttons = [
            InlineKeyboardButton(text=f"#{int(item.get('task_id') or 0)}", callback_data=f"ap:card:{int(item.get('task_id') or 0)}")
            for item in items[:10]
            if int(item.get("task_id") or 0) > 0
        ]
        for start in range(0, len(card_buttons), 5):
            rows.append(card_buttons[start : start + 5])

    page_row = _pagination_row(tab, page, pages)
    if page_row:
        rows.append(page_row)

    rows.append(
        [
            InlineKeyboardButton(text="🔄 Обновить", callback_data=f"ap:w:t:{tab}"),
            InlineKeyboardButton(text="◀️ Назад", callback_data="ap:main"),
        ]
    )
    return InlineKeyboardMarkup(inline_keyboard=rows)


def get_work_cancel_video_confirm_keyboard(video_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="✅ Да, убрать видео", callback_data=f"ap:w:cvd:{int(video_id)}")],
            [InlineKeyboardButton(text="❌ Отмена", callback_data="ap:w")],
        ]
    )


def get_work_cancel_task_confirm_keyboard(task_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="✅ Да, убрать заказ", callback_data=f"ap:w:ctd:{int(task_id)}")],
            [InlineKeyboardButton(text="❌ Отмена", callback_data="ap:w")],
        ]
    )


def get_work_stop_task_confirm_keyboard(task_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="✅ Да, остановить", callback_data=f"ap:w:std:{int(task_id)}")],
            [InlineKeyboardButton(text="❌ Отмена", callback_data="ap:w:t:run")],
        ]
    )


def get_current_keyboard(*, has_active: bool = False, queue_count: int = 0) -> InlineKeyboardMarkup:
    rows: list[list[InlineKeyboardButton]] = []
    if has_active:
        rows.append([InlineKeyboardButton(text="⏹ Остановить активный", callback_data="ap:current:stop_active")])
    if queue_count > 0:
        rows.append([InlineKeyboardButton(text="🧹 Отменить всю очередь", callback_data="ap:current:cancel_all")])
        rows.append([InlineKeyboardButton(text="⏭ Пропустить очередь", callback_data="ap:current:skipq")])
    rows.append(
        [
            InlineKeyboardButton(text="🔄 Обновить", callback_data="ap:current"),
            InlineKeyboardButton(text="◀️ Назад", callback_data="ap:main"),
        ]
    )
    return InlineKeyboardMarkup(inline_keyboard=rows)


def get_stop_active_confirm_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="✅ Да, остановить", callback_data="ap:action:stop_active:do")],
            [InlineKeyboardButton(text="❌ Отмена", callback_data="ap:current")],
        ]
    )


def get_cancel_all_confirm_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="✅ Да, отменить", callback_data="ap:action:cancel_all:do")],
            [InlineKeyboardButton(text="❌ Отмена", callback_data="ap:current")],
        ]
    )


def get_skip_queue_confirm_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="✅ Да, пропустить", callback_data="ap:action:skipq:do")],
            [InlineKeyboardButton(text="❌ Отмена", callback_data="ap:current")],
        ]
    )
