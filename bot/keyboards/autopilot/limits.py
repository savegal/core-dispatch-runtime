from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup


def get_limits_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="🐢 Спокойный · 5/1/20", callback_data="ap:limits:preset:calm")],
            [InlineKeyboardButton(text="🚶 Средний · 20/3/120", callback_data="ap:limits:preset:normal")],
            [InlineKeyboardButton(text="🏃 Быстрый · 50/5/300", callback_data="ap:limits:preset:fast")],
            [InlineKeyboardButton(text="✏️ Свои числа", callback_data="ap:limits:custom")],
            [InlineKeyboardButton(text="◀️ Назад", callback_data="ap:main")],
        ]
    )


def get_custom_limits_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="❌ Отмена", callback_data="ap:limits")],
        ]
    )
