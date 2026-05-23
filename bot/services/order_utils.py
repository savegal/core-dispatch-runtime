"""Утилиты для работы с заказами."""
from aiogram.types import CallbackQuery, InlineKeyboardMarkup, InlineKeyboardButton
from bot.services.drafts import get_draft
from bot.services.formatting import Emoji
from services.user_facing_errors import humanize_error_text
import logging
import html # Using HTML escaping for safety if needed, but we output Markdown usually.
# For markdown escaping, better to handle manually or minimal escaping.
import re

logger = logging.getLogger("bot.order_utils")
_RE_REQUIRED = re.compile(r"required:\s*([0-9]+(?:[.,][0-9]+)?)", re.IGNORECASE)
_RE_AVAILABLE = re.compile(r"available:\s*([0-9]+(?:[.,][0-9]+)?)", re.IGNORECASE)


def _extract_balance_snapshot(error_msg: str) -> tuple[str | None, str | None]:
    required_match = _RE_REQUIRED.search(error_msg)
    available_match = _RE_AVAILABLE.search(error_msg)
    required = required_match.group(1) if required_match else None
    available = available_match.group(1) if available_match else None
    return required, available


async def validate_draft_has_links(callback: CallbackQuery, user_id: int) -> bool:
    """
    Проверяет наличие ссылок в draft.
    
    Returns:
        True если ссылки есть, False если нет (и показывает сообщение об ошибке)
    """
    draft = get_draft(user_id)
    
    if not draft.links or len(draft.links) == 0:
        await callback.message.edit_text(
            "❌ *Ссылки не найдены*\n\n"
            "Сначала отправьте ссылки на видео TikTok.\n\n"
            "*Как отправить ссылки:*\n"
            "• Отправьте ссылки боту напрямую\n"
            "• Или используйте меню создания заказа\n\n"
            "*Формат ссылок:*\n"
            "`https://www.tiktok.com/@user/video/123`",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="🆕 Создать заказ", callback_data="menu:new_order")],
                [InlineKeyboardButton(text=f"{Emoji.HOME} Меню", callback_data="menu:main")]
            ]),
            parse_mode="Markdown"
        )
        await callback.answer("Сначала отправьте ссылки на видео", show_alert=True)
        return False
    
    return True


def get_task_creation_error_message(e: Exception) -> tuple[str, InlineKeyboardMarkup | None]:
    """
    Возвращает понятное сообщение об ошибке создания задачи.
    
    Args:
        e: Исключение
        
    Returns:
        (text, reply_markup)
    """
    error_msg = str(e)
    
    # Обработка Pydantic 422 ошибок (в виде списка словарей)
    if "value_error" in error_msg and "text_variants" in error_msg:
        # Пытаемся распарсить детали, но для простоты просто возвращаем понятный текст
        return (
            "❌ *Ошибка валидации текста*\n\n"
            "Нужно отправить **минимум 3 разных варианта** текста комментария отдельными сообщениями.\n\n"
            "Вы отправили слишком мало вариантов или они пустые.",
            None
        )
        
    # Обработка дубликатов
    if "DUPLICATE_ORDER" in error_msg:
        return (
            "⚠️ *Дубликат заказа*\n\n"
            "Вы уже успешно публиковали этот комментарий под это видео.\n"
            "ТикТок может склеить их или скрыть.\n\n"
            "Пожалуйста, измените текст комментария (хотя бы пару слов).",
            None
        )

    if "DUPLICATE_ORDER_DEDUP" in error_msg:
        return (
            "⏳ *Запрос уже отправлен*\n\n"
            "Похоже, кнопка была нажата несколько раз подряд.\n"
            "Подождите пару секунд и проверьте список заказов.",
            InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="📋 Мои заказы", callback_data="menu:orders")],
                [InlineKeyboardButton(text=f"{Emoji.HOME} Меню", callback_data="menu:main")]
            ])
        )
        
    # Обработка нет средств
    lower_error = error_msg.lower()

    if any(
        token in lower_error
        for token in (
            "insufficient balance",
            "insufficient funds",
            "недостаточно средств",
            "недостаточно баланса",
            "insufficient_funds",
        )
    ):
        required, available = _extract_balance_snapshot(error_msg)
        detail_lines = []
        if required:
            detail_lines.append(f"Нужно: *{required} грн*")
        if available:
            detail_lines.append(f"Доступно: *{available} грн*")
        return (
            "💰 *Недостаточно средств*\n\n"
            + ("\n".join(detail_lines) + "\n\n" if detail_lines else "")
            + "Стоимость заказа считается по числу ссылок.\n"
            + "Если баланс уже пополнен, попробуйте снова через пару секунд.",
            InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="💳 Пополнить", callback_data="menu:subscription")],
                [InlineKeyboardButton(text=f"{Emoji.HOME} Меню", callback_data="menu:main")]
            ])
        )

    # Таймаут создания: заказ мог успеть создаться на backend
    if "возможно, заказ уже создан" in error_msg.lower():
        return (
            "⏱️ *Не удалось дождаться ответа от сервера*\n\n"
            "Заказ мог успеть создаться. Проверьте список заказов перед повторной отправкой.",
            InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="📋 Мои заказы", callback_data="menu:orders")],
                [InlineKeyboardButton(text=f"{Emoji.HOME} Меню", callback_data="menu:main")]
            ])
        )

    # Общая ошибка - экранируем спецсимволы Markdown для предотвращения крашей
    # Экранируем: _ * [ ] ( ) ~ > # + - = | { } . !
    # Для простоты просто уберем спецсимволы или используем code block
    
    # Очищаем сообщение от JSON мусора если это список ошибок
    clean_error = error_msg
    if "[{'type':" in error_msg:
        try:
            # Пробуем извлечь msg из JSON строки (грубый парсинг)
            import ast
            # Находим список
            start = error_msg.find("[")
            end = error_msg.rfind("]") + 1
            if start != -1 and end != -1:
                json_str = error_msg[start:end]
                errors = ast.literal_eval(json_str)
                if isinstance(errors, list) and len(errors) > 0:
                    clean_error = errors[0].get("msg", clean_error)
        except Exception:
            pass
            
    # Экранируем MD
    human_error = humanize_error_text(clean_error, short=False) or clean_error
    safe_error = human_error.replace("_", "\\_").replace("*", "\\*").replace("[", "\\[").replace("`", "\\`")
    
    return (
        f"❌ *Ошибка создания заказа*\n\n"
        f"{safe_error}\n\n"
        f"Попробуйте позже или обратитесь в поддержку.",
        None
    )
