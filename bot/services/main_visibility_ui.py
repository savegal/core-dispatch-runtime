"""Per-order visibility option shared by quick orders and the order wizard."""
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

CALLBACK_PREFIX = "order:main_visibility:"
REPLY_CALLBACK_PREFIX = "order:reply_visibility:"


def visibility_keyboard(markup: InlineKeyboardMarkup, enabled: bool = False, reply_enabled=None):
    rows = [[button for button in row if not (button.callback_data or "").startswith(CALLBACK_PREFIX)]
            for row in markup.inline_keyboard]
    rows = [row for row in rows if row]
    rows.insert(1, [InlineKeyboardButton(
        text="👁 Проверка главных: вкл. + замена" if enabled else "👁 Проверка главных: выкл.",
        callback_data=f"{CALLBACK_PREFIX}{0 if enabled else 1}",
    )])
    result = InlineKeyboardMarkup(inline_keyboard=rows)
    return reply_visibility_keyboard(result, reply_enabled) if reply_enabled is not None else result


def reply_visibility_keyboard(markup: InlineKeyboardMarkup, enabled: bool = False):
    rows = [[button for button in row if not (button.callback_data or "").startswith(REPLY_CALLBACK_PREFIX)]
            for row in markup.inline_keyboard]
    rows = [row for row in rows if row]
    has_main = any((b.callback_data or "").startswith(CALLBACK_PREFIX) for row in rows for b in row)
    rows.insert(2 if has_main else 1, [InlineKeyboardButton(
        text="👁 Проверка ответов: вкл. + замена" if enabled else "👁 Проверка ответов: выкл.",
        callback_data=f"{REPLY_CALLBACK_PREFIX}{0 if enabled else 1}",
    )])
    return InlineKeyboardMarkup(inline_keyboard=rows)


async def set_reply_visibility_option(callback, state):
    await _set_option(callback, state, prefix=REPLY_CALLBACK_PREFIX,
                      setting="verify_reply_visibility", build=reply_visibility_keyboard)


async def set_visibility_option(callback, state):
    await _set_option(callback, state, prefix=CALLBACK_PREFIX,
                      setting="verify_main_visibility", build=visibility_keyboard)


async def _set_option(callback, state, *, prefix, setting, build):
    value = (callback.data or "").removeprefix(prefix)
    if value not in {"0", "1"} or callback.message is None:
        await callback.answer("Откройте предпросмотр заказа заново", show_alert=True)
        return
    markup = callback.message.reply_markup
    if markup is None or not any((b.callback_data or "").startswith(prefix)
                                 for row in markup.inline_keyboard for b in row):
        await callback.answer("Откройте предпросмотр заказа заново", show_alert=True)
        return
    enabled = value == "1"
    await state.update_data(**{setting: enabled})
    await callback.message.edit_reply_markup(reply_markup=build(markup, enabled))
    await callback.answer("Проверка видимости включена" if enabled else "Проверка видимости выключена")
