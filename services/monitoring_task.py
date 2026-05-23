"""
Фоновая задача для мониторинга ресурсов и отправки уведомлений владельцу.
Можно запускать как отдельный скрипт или интегрировать в executor.
"""
import asyncio
import logging
import time
from sqlalchemy.orm import Session
from db.database import get_sync_db  # ИСПРАВЛЕНО: Используем синхронную версию для monitoring_task
from services.monitoring_service import check_resources_status, get_notification_message
from config.settings import get_settings
from bot.config import load_config
from aiogram import Bot

logger = logging.getLogger("services.monitoring_task")
settings = get_settings()
config = load_config()

CHECK_INTERVAL_SECONDS = 300  # Проверка каждые 5 минут


async def send_notification_to_owner(message: str) -> bool:
    """
    Отправляет уведомление владельцу через Telegram бота.
    
    Args:
        message: текст сообщения
    
    Returns:
        True если отправлено успешно
    """
    owner_id = settings.OWNER_TELEGRAM_ID
    bot_token = config.get("BOT_TOKEN")
    
    if not owner_id or not bot_token:
        logger.warning("OWNER_TELEGRAM_ID or BOT_TOKEN not set, cannot send notification")
        return False
    
    bot = None
    try:
        bot = Bot(token=bot_token)
        await bot.send_message(owner_id, message)
        logger.info(f"Notification sent to owner: {message[:50]}...")
        return True
    except Exception as e:
        logger.error(f"Error sending notification to owner: {e}")
        return False
    finally:
        if bot is not None:
            try:
                await bot.session.close()
            except Exception as close_error:
                logger.debug(f"Error closing notification bot session: {close_error}")


async def monitoring_loop():
    """Основной цикл мониторинга."""
    logger.info("Monitoring task started")
    
    while True:
        try:
            # ИСПРАВЛЕНО: Используем синхронную версию БД для monitoring_task
            db: Session = next(get_sync_db())
            try:
                status = check_resources_status(db)
                message = get_notification_message(status)
                
                if message:
                    await send_notification_to_owner(message)
                
            finally:
                db.close()  # get_sync_db() автоматически закрывает в finally, но для явности оставляем
            
            # Ждём перед следующей проверкой
            await asyncio.sleep(CHECK_INTERVAL_SECONDS)
            
        except Exception as e:
            logger.error(f"Error in monitoring loop: {e}", exc_info=True)
            await asyncio.sleep(60)  # При ошибке ждём минуту


if __name__ == "__main__":
    logging.basicConfig(
        level=logging.INFO,
        format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
    )
    asyncio.run(monitoring_loop())
