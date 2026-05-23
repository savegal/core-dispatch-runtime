"""Клавиатуры для общих экранов."""
from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton
from bot.ui.callbacks import (
    MENU_MAIN, MENU_NEW_ORDER, MENU_ORDERS, MENU_TEMPLATES,
    MENU_SETTINGS, MENU_HELP, MENU_ADMIN, MENU_CHECK_ACCESS, MENU_SUBSCRIPTION
)


def get_access_denied_keyboard(*, pending_approval: bool = False) -> InlineKeyboardMarkup:
    """Клавиатура для экрана 'нет доступа'.

    pending_approval=True — пользователь ждёт одобрения владельца:
        показываем только кнопку "Проверить доступ", без главного меню
        (туда всё равно нет доступа) и без "Написать владельцу"
        (по требованию: ждать одобрения молча).

    pending_approval=False (по умолчанию) — обычный заблокированный
        или без баланса: те же две кнопки, что и раньше.
    """
    keyboard = [
        [InlineKeyboardButton(text="🔄 Проверить доступ", callback_data=MENU_CHECK_ACCESS)],
    ]
    if not pending_approval:
        keyboard.append([InlineKeyboardButton(text="🏠 Главное меню", callback_data=MENU_MAIN)])
    return InlineKeyboardMarkup(inline_keyboard=keyboard)


def get_main_menu_keyboard(
    is_admin: bool = False,
    software_mode: str = "full",
) -> InlineKeyboardMarkup:
    """Главное меню.

    software_mode:
      - "full"    — все секции (заказы, шаблоны, личные аккаунты, баланс и т.д.)
      - "parsing" — для обычных пользователей только мониторинг видео.
        Админ/owner всегда видит полный набор кнопок: для него parsing-режим
        админ-панели не должен урезать функции бота.
    """
    mode = (software_mode or "full").strip().lower()
    parsing_only = mode == "parsing" and not is_admin

    if parsing_only:
        keyboard = [
            [InlineKeyboardButton(text="🎬 Парсинг видео", callback_data="menu:video_parsing")],
        ]
        if is_admin:
            keyboard.append([InlineKeyboardButton(text="🔧 Админ", callback_data=MENU_ADMIN)])
        return InlineKeyboardMarkup(inline_keyboard=keyboard)

    keyboard = [
        [
            InlineKeyboardButton(text="➕ Полный заказ", callback_data=MENU_NEW_ORDER),
            InlineKeyboardButton(text="⚡ Только ответы", callback_data="menu:engagement_only")
        ],
        [
            InlineKeyboardButton(text="📋 Заказы", callback_data=MENU_ORDERS),
            InlineKeyboardButton(text="📑 Шаблоны", callback_data=MENU_TEMPLATES)
        ],
        [
            InlineKeyboardButton(text="🎬 Парсинг видео", callback_data="menu:video_parsing")
        ],
        [
            InlineKeyboardButton(text="👥 Мои аккаунты", callback_data="pa:menu")
        ],
        [
            InlineKeyboardButton(text="💰 Баланс", callback_data=MENU_SUBSCRIPTION),
            InlineKeyboardButton(text="❓ Помощь", callback_data=MENU_HELP)
        ]
    ]

    if is_admin:
        keyboard.append([InlineKeyboardButton(text="🔧 Админ", callback_data=MENU_ADMIN)])

    return InlineKeyboardMarkup(inline_keyboard=keyboard)


def get_back_keyboard(callback_data: str = MENU_MAIN) -> InlineKeyboardMarkup:
    """Кнопка 'Назад' - ИСПРАВЛЕНО: стандартизированный текст."""
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="◀️ Назад", callback_data=callback_data)]
    ])


def get_navigation_keyboard(back_callback: str = MENU_MAIN) -> InlineKeyboardMarkup:
    """Кнопки навигации - КОМПАКТНЫЕ."""
    return InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text="◀️ Назад", callback_data=back_callback),
            InlineKeyboardButton(text="🏠 Меню", callback_data=MENU_MAIN)
        ]
    ])


def get_cancel_keyboard() -> InlineKeyboardMarkup:
    """Кнопка 'Отмена'."""
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="❌ Отмена", callback_data=MENU_MAIN)]
    ])
