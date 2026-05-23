"""
Сервис для отправки уведомлений пользователям через Telegram бота.
"""
import asyncio
import logging
from sqlalchemy.orm import Session
from db.models import User
from config.settings import get_settings
from aiogram import Bot

logger = logging.getLogger("services.notification")
settings = get_settings()


def _get_bot_token() -> str | None:
    """Получает токен бота из настроек."""
    try:
        # Импортируем локально, чтобы избежать циклических зависимостей
        from bot.config import load_config
        config = load_config()
        return config.get("BOT_TOKEN")
    except Exception as e:
        logger.error(f"Error loading bot config: {e}")
        return None


async def send_notification_to_user(db: Session, user_id: int, message: str) -> bool:
    """
    Отправляет уведомление пользователю через Telegram бота.
    
    Args:
        db: сессия БД
        user_id: ID пользователя в БД
        message: текст сообщения
    
    Returns:
        True если отправлено успешно
    """
    bot_token = _get_bot_token()
    if not bot_token:
        logger.warning("BOT_TOKEN not set, cannot send notification")
        return False
    
    try:
        # Получаем пользователя из БД
        user = db.query(User).filter(User.id == user_id).first()
        if not user or not user.telegram_id:
            logger.warning(f"User {user_id} not found or has no telegram_id")
            return False
        
        bot = Bot(token=bot_token)
        try:
            await bot.send_message(user.telegram_id, message, parse_mode="Markdown")
            logger.info(f"Notification sent to user {user_id} (telegram_id: {user.telegram_id})")
            return True
        finally:
            await bot.session.close()
    except Exception as e:
        logger.error(f"Error sending notification to user {user_id}: {e}")
        return False


def send_notification_to_user_sync(db: Session, user_id: int, message: str) -> bool:
    """
    Синхронная обёртка для send_notification_to_user (для использования в threads).
    
    Args:
        db: сессия БД
        user_id: ID пользователя в БД
        message: текст сообщения
    
    Returns:
        True если отправлено успешно
    """
    try:
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        try:
            return loop.run_until_complete(send_notification_to_user(db, user_id, message))
        finally:
            loop.close()
    except Exception as e:
        logger.error(f"Error in send_notification_to_user_sync: {e}")
        return False

