"""
Утилиты для работы с пользователями.
Централизованные функции для избежания дублирования кода.
"""
from bot.services.api_client import api_client
import logging

logger = logging.getLogger("bot.user_utils")


async def get_user_backend_id(telegram_id: int, use_cache: bool = True) -> int | None:
    """
    Получает backend ID пользователя по Telegram ID.
    
    Args:
        telegram_id: Telegram ID пользователя
        use_cache: Использовать ли кэш (по умолчанию True)
        
    Returns:
        Backend ID пользователя или None если не найден
    """
    try:
        user_data = await api_client.sync_telegram(telegram_id, None, use_cache=use_cache)
        return user_data.get("id")
    except Exception as e:
        logger.error(f"Error getting user backend ID for telegram_id {telegram_id}: {e}")
        return None




