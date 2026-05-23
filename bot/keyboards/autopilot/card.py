from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup


def get_card_keyboard(
    task_id: int,
    *,
    status: str,
    cost: float = 0.0,
    creator_id: int | None = None,
) -> InlineKeyboardMarkup:
    rows: list[list[InlineKeyboardButton]] = []
    if status in {"error", "partial"}:
        rows.append([InlineKeyboardButton(text=f"🔁 Создать заказ заново ({cost:.2f}₴)", callback_data=f"ap:c:{task_id}:r")])
    if status in {"done", "partial", "error", "stopped"}:
        rows.append([InlineKeyboardButton(text="🙈 Скрыть из отчёта", callback_data=f"ap:c:{task_id}:h")])
    if creator_id:
        rows.append([InlineKeyboardButton(text="Открыть блогера →", callback_data=f"ap:c:{task_id}:oc")])
    rows.append([InlineKeyboardButton(text="◀️ К Журналу", callback_data="ap:rep:tab:list")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def get_retry_confirm_keyboard(task_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="✅ Да, создать", callback_data=f"ap:a:r:{task_id}")],
            [InlineKeyboardButton(text="❌ Отмена", callback_data=f"ap:card:{task_id}")],
        ]
    )


def get_hide_confirm_keyboard(task_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="✅ Да, скрыть", callback_data=f"ap:a:h:{task_id}")],
            [InlineKeyboardButton(text="❌ Отмена", callback_data=f"ap:card:{task_id}")],
        ]
    )
