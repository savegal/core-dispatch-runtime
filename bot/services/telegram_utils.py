"""
Утилиты для работы с Telegram API.
Включает валидацию длины сообщений и безопасную отправку.
"""
import logging
from typing import Optional
from aiogram import Bot
from aiogram.exceptions import TelegramBadRequest, TelegramAPIError
from aiogram.types import CallbackQuery, Message

logger = logging.getLogger("bot.telegram_utils")

# Лимиты Telegram API
MAX_MESSAGE_LENGTH = 4096  # Максимальная длина текстового сообщения
MAX_CAPTION_LENGTH = 1024  # Максимальная длина подписи к медиа
MAX_BUTTON_TEXT_LENGTH = 64  # Максимальная длина текста кнопки

STALE_CALLBACK_MARKERS = (
    "query is too old",
    "response timeout expired",
    "query id is invalid",
)


def is_stale_callback_error(exc: Exception) -> bool:
    error_msg = str(exc).lower()
    return any(marker in error_msg for marker in STALE_CALLBACK_MARKERS)


async def safe_callback_answer(
    callback: CallbackQuery,
    text: Optional[str] = None,
    *,
    show_alert: Optional[bool] = None,
    **kwargs,
) -> bool:
    """Answer Telegram callback without treating expired callback ids as errors."""
    payload = dict(kwargs)
    if text is not None:
        payload["text"] = text
    if show_alert is not None:
        payload["show_alert"] = show_alert

    try:
        await callback.answer(**payload)
        return True
    except TelegramBadRequest as exc:
        if is_stale_callback_error(exc):
            logger.debug("Ignoring stale Telegram callback answer: %s", exc)
            return False
        logger.warning("Telegram callback answer failed: %s", exc)
        return False
    except TelegramAPIError as exc:
        logger.warning("Telegram callback answer API error: %s", exc)
        return False


def truncate_message(text: str, max_length: int = MAX_MESSAGE_LENGTH - 100, suffix: str = "...") -> str:
    """
    Обрезает сообщение до максимальной длины.
    
    Args:
        text: Текст сообщения
        max_length: Максимальная длина (по умолчанию 3996 для запаса)
        suffix: Суффикс для обрезанного текста
        
    Returns:
        str: Обрезанный текст
    """
    if len(text) <= max_length:
        return text
    
    # Обрезаем с учетом суффикса
    truncated = text[:max_length - len(suffix)] + suffix
    return truncated


def split_long_message(text: str, max_length: int = MAX_MESSAGE_LENGTH - 100) -> list[str]:
    """
    Разбивает длинное сообщение на несколько частей.
    
    Args:
        text: Текст сообщения
        max_length: Максимальная длина одной части
        
    Returns:
        list[str]: Список частей сообщения
    """
    if len(text) <= max_length:
        return [text]
    
    parts = []
    current_part = ""
    
    # Разбиваем по строкам, чтобы не резать посередине
    lines = text.split('\n')
    
    for line in lines:
        # Если текущая часть + новая строка помещается
        if len(current_part) + len(line) + 1 <= max_length:
            if current_part:
                current_part += '\n' + line
            else:
                current_part = line
        else:
            # Сохраняем текущую часть
            if current_part:
                parts.append(current_part)
            
            # Если строка сама по себе длинная, режем её
            if len(line) > max_length:
                # Режем по словам
                words = line.split(' ')
                current_part = ""
                for word in words:
                    if len(current_part) + len(word) + 1 <= max_length:
                        if current_part:
                            current_part += ' ' + word
                        else:
                            current_part = word
                    else:
                        if current_part:
                            parts.append(current_part)
                        current_part = word
            else:
                current_part = line
    
    # Добавляем последнюю часть
    if current_part:
        parts.append(current_part)
    
    return parts


async def safe_send_message(
    bot: Bot,
    chat_id: int,
    text: str,
    **kwargs
) -> Optional[Message]:
    """
    Безопасно отправляет сообщение с обработкой ошибок.
    
    Args:
        bot: Экземпляр бота
        chat_id: ID чата
        text: Текст сообщения
        **kwargs: Дополнительные параметры для send_message
        
    Returns:
        Message или None при ошибке
    """
    try:
        # Обрезаем сообщение, если оно слишком длинное
        if len(text) > MAX_MESSAGE_LENGTH:
            text = truncate_message(text)
        
        return await bot.send_message(chat_id, text, **kwargs)
    except TelegramBadRequest as e:
        error_msg = str(e).lower()
        if "message is too long" in error_msg:
            # Пробуем разбить на части
            parts = split_long_message(text)
            for i, part in enumerate(parts):
                try:
                    await bot.send_message(chat_id, part, **kwargs)
                except Exception as part_error:
                    logger.error(f"Error sending message part {i+1}/{len(parts)}: {part_error}")
            return None
        elif "chat not found" in error_msg or "bot was blocked" in error_msg:
            logger.warning(f"Chat {chat_id} not found or bot blocked")
            return None
        else:
            logger.error(f"Telegram API error sending message: {e}")
            return None
    except TelegramAPIError as e:
        logger.error(f"Telegram API error: {e}")
        return None
    except Exception as e:
        logger.error(f"Unexpected error sending message: {e}", exc_info=True)
        return None


async def safe_edit_message(
    bot: Bot,
    chat_id: int,
    message_id: int,
    text: str,
    **kwargs
) -> Optional[Message]:
    """
    Безопасно редактирует сообщение с обработкой ошибок.
    
    Args:
        bot: Экземпляр бота
        chat_id: ID чата
        message_id: ID сообщения
        text: Новый текст сообщения
        **kwargs: Дополнительные параметры для edit_message_text
        
    Returns:
        Message или None при ошибке
    """
    try:
        # Обрезаем сообщение, если оно слишком длинное
        if len(text) > MAX_MESSAGE_LENGTH:
            text = truncate_message(text)
        
        return await bot.edit_message_text(chat_id=chat_id, message_id=message_id, text=text, **kwargs)
    except TelegramBadRequest as e:
        error_msg = str(e).lower()
        if "message is too long" in error_msg:
            logger.warning(f"Message too long to edit, sending new message instead")
            # Отправляем новое сообщение вместо редактирования
            return await safe_send_message(bot, chat_id, text, **kwargs)
        elif "message is not modified" in error_msg:
            # Это нормально, игнорируем
            logger.debug(f"Message {message_id} not modified (no changes)")
            return None
        elif "message to edit not found" in error_msg:
            logger.warning(f"Message {message_id} not found, sending new message instead")
            return await safe_send_message(bot, chat_id, text, **kwargs)
        else:
            logger.error(f"Telegram API error editing message: {e}")
            return None
    except TelegramAPIError as e:
        logger.error(f"Telegram API error: {e}")
        return None
    except Exception as e:
        logger.error(f"Unexpected error editing message: {e}", exc_info=True)
        return None



