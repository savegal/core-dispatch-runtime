"""
Централизованная обработка ошибок для бота.
Стандартизированные сообщения об ошибках для пользователей.
"""
from __future__ import annotations

import logging
import re

from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton

from services.user_facing_errors import humanize_error_text

logger = logging.getLogger("bot.error_handlers")

_RE_REQUIRED = re.compile(r"required:\s*([0-9]+(?:[.,][0-9]+)?)", re.IGNORECASE)
_RE_AVAILABLE = re.compile(r"available:\s*([0-9]+(?:[.,][0-9]+)?)", re.IGNORECASE)


def _extract_balance_snapshot(error_text: str) -> tuple[str | None, str | None]:
    required_match = _RE_REQUIRED.search(error_text)
    available_match = _RE_AVAILABLE.search(error_text)
    required = required_match.group(1) if required_match else None
    available = available_match.group(1) if available_match else None
    return required, available


def get_error_message(error: Exception, context: str = "") -> tuple[str, InlineKeyboardMarkup]:
    """
    Форматирует ошибку в понятное сообщение для пользователя.
    
    Args:
        error: Исключение
        context: Контекст ошибки (например, "создание заказа")
        
    Returns:
        Tuple (текст сообщения, клавиатура)
    """
    error_text = str(error).lower()
    
    # Ошибки API
    if "timeout" in error_text or "превышен таймаут" in error_text:
        text = (
            "*⏱ Ошибка таймаута*\n\n"
            "Сервер не ответил вовремя.\n"
            "Попробуйте позже или обратитесь к администратору."
        )
    elif "connection" in error_text or "подключиться" in error_text:
        text = (
            "*🔌 Ошибка подключения*\n\n"
            "Не удалось подключиться к серверу.\n"
            "Проверьте интернет-соединение и попробуйте позже."
        )
    elif "duplicate" in error_text or "дубликат" in error_text.lower():
        text = (
            "*⚠️ Дубликат заказа*\n\n"
            "Похожий заказ уже существует.\n\n"
            "Измените текст комментария и попробуйте снова."
        )
    elif any(
        token in error_text
        for token in (
            "insufficient balance",
            "insufficient funds",
            "недостаточно средств",
            "недостаточно баланса",
        )
    ):
        required, available = _extract_balance_snapshot(error_text)
        text = (
            "*💰 Недостаточно средств*\n\n"
            "Пополните баланс для создания заказа."
        )
        if required:
            text += f"\nНужно: *{required} грн*"
        if available:
            text += f"\nСейчас доступно: *{available} грн*"
        text += "\n\nЕсли сумма уже пополнена, попробуйте создать заказ ещё раз через пару секунд."
    elif any(
        token in error_text
        for token in (
            "insufficient accounts",
            "insufficient proxies",
            "нет активных аккаунтов",
            "no active accounts",
            "no accounts available",
        )
    ):
        text = (
            "*⚠️ Недостаточно ресурсов*\n\n"
            "Сейчас не хватает готовых аккаунтов или прокси для запуска заказа.\n"
            "Попробуйте позже или обратитесь к администратору."
        )
    elif "blocked" in error_text:
        text = (
            "*🔒 Доступ запрещён*\n\n"
            "Вы заблокированы администратором.\n"
            "Для разблокировки обратитесь к администратору."
        )
    elif "not found" in error_text or "не найден" in error_text.lower():
        text = (
            "*❌ Не найдено*\n\n"
            "Запрашиваемый ресурс не найден.\n"
            "Попробуйте обновить страницу."
        )
    elif "validation" in error_text or "валидация" in error_text.lower():
        text = (
            "*❌ Ошибка валидации*\n\n"
            "Проверьте правильность введённых данных."
        )
    else:
        # Общая ошибка
        text = (
            "*❌ Ошибка*\n\n"
            f"Произошла ошибка при {context if context else 'выполнении операции'}.\n\n"
            "Попробуйте ещё раз чуть позже."
        )
    
    keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text="🔄 Попробовать снова", callback_data="menu:new_order"),
            InlineKeyboardButton(text="🏠 Меню", callback_data="menu:main")
        ]
    ])
    
    return text, keyboard


def get_task_creation_error_message(error: Exception) -> tuple[str, InlineKeyboardMarkup]:
    """
    Специализированное сообщение об ошибке создания заказа.
    
    Args:
        error: Исключение
        
    Returns:
        Tuple (текст сообщения, клавиатура)
    """
    error_text = str(error)
    lower = error_text.lower()

    if "duplicate_order_dedup" in lower:
        return (
            "*⏳ Запрос уже отправлен*\n\n"
            "Похоже, кнопка была нажата несколько раз подряд.\n"
            "Подождите пару секунд и проверьте список заказов.",
            InlineKeyboardMarkup(
                inline_keyboard=[
                    [InlineKeyboardButton(text="📋 Мои заказы", callback_data="menu:orders")],
                    [InlineKeyboardButton(text="🏠 Меню", callback_data="menu:main")],
                ]
            ),
        )

    if any(
        token in lower
        for token in (
            "insufficient balance",
            "insufficient funds",
            "недостаточно средств",
            "недостаточно баланса",
        )
    ):
        required, available = _extract_balance_snapshot(error_text)
        text = "*💰 Недостаточно средств*\n\n"
        if required or available:
            if required:
                text += f"Нужно: *{required} грн*\n"
            if available:
                text += f"Доступно: *{available} грн*\n"
            text += "\n"
        text += (
            "Стоимость заказа считается по числу ссылок.\n"
            "Если баланс уже пополнили, попробуйте отправить заказ ещё раз через пару секунд."
        )
        return text, InlineKeyboardMarkup(
            inline_keyboard=[
                [InlineKeyboardButton(text="💳 Баланс", callback_data="menu:subscription")],
                [InlineKeyboardButton(text="🏠 Меню", callback_data="menu:main")],
            ]
        )

    humanized = humanize_error_text(error_text, short=False)
    if humanized:
        return (
            f"*❌ Ошибка создания заказа*\n\n{humanized}",
            InlineKeyboardMarkup(
                inline_keyboard=[
                    [InlineKeyboardButton(text="🔄 Попробовать снова", callback_data="menu:new_order")],
                    [InlineKeyboardButton(text="🏠 Меню", callback_data="menu:main")],
                ]
            ),
        )

    return get_error_message(error, "создании заказа")




