from bot.config import load_config
from bot.services.api_client import api_client
import logging
import time
from typing import Optional

logger = logging.getLogger("bot.subscription_service")

config = load_config()

# Кэш для проверки доступа (TTL 30 секунд)
_access_cache: dict[int, tuple[bool, float]] = {}
_access_cache_ttl = 30.0


async def check_user_access(telegram_id: int, user_backend_id: int | None = None, role: str | None = None, use_cache: bool = True) -> bool:
    """
    Проверяет доступ пользователя с кэшированием.
    
    Owner и admin всегда имеют доступ (неограниченный баланс).
    Если DEMO_MODE=True - всегда возвращает True.
    Иначе проверяет баланс и блокировку через API.
    """
    current_time = time.time()
    
    # Проверяем кэш
    if use_cache and telegram_id in _access_cache:
        cached_access, cached_time = _access_cache[telegram_id]
        if current_time - cached_time < _access_cache_ttl:
            return cached_access
    
    # Owner всегда имеет доступ
    owner_id = config.get("OWNER_TELEGRAM_ID")
    if owner_id and telegram_id == owner_id:
        result = True
        if use_cache:
            _access_cache[telegram_id] = (result, current_time)
        logger.debug(f"User {telegram_id} granted access as owner")
        return result
    
    # Admin всегда имеет доступ (неограниченный баланс)
    is_admin = role in ("admin", "owner")
    if is_admin:
        result = True
        if use_cache:
            _access_cache[telegram_id] = (result, current_time)
        logger.debug(f"User {telegram_id} granted access as admin/owner")
        return result
    
    # DEMO_MODE дает доступ всем
    if config.get("DEMO_MODE"):
        result = True
        if use_cache:
            _access_cache[telegram_id] = (result, current_time)
        logger.debug(f"User {telegram_id} granted access in demo mode")
        return result
    
    # Проверяем блокировку и баланс через API
    try:
        if user_backend_id:
            user_data = await api_client.get_user(user_backend_id)
        else:
            # Получаем user_backend_id из sync_telegram
            user_data = await api_client.sync_telegram(telegram_id, None)
            user_backend_id = user_data.get("id")
            if user_backend_id:
                user_data = await api_client.get_user(user_backend_id)
        
        if not user_data:
            logger.warning(f"User {telegram_id} not found")
            return False
        
        # Проверяем блокировку
        is_blocked = user_data.get("is_blocked", False)
        if is_blocked:
            logger.debug(f"User {telegram_id} is blocked")
            result = False
            if use_cache:
                _access_cache[telegram_id] = (result, current_time)
            return result
        
        # Для обычных пользователей доступ определяется балансом (проверяется при создании заказа)
        # Здесь просто проверяем, что пользователь не заблокирован
        result = True
        if use_cache:
            _access_cache[telegram_id] = (result, current_time)
        
        logger.debug(f"User {telegram_id} access check: {result} (not blocked)")
        return result
    except Exception as e:
        logger.error(f"Error checking user access for {telegram_id}: {e}")
        # В случае ошибки не кэшируем
        return False


async def check_user_blocked(telegram_id: int, user_backend_id: int | None = None) -> tuple[bool, str | None]:
    """
    Проверяет, заблокирован ли пользователь.
    Возвращает (is_blocked, error_message).
    """
    try:
        if user_backend_id:
            user_data = await api_client.get_user(user_backend_id)
        else:
            user_data = await api_client.sync_telegram(telegram_id, None)
            user_backend_id = user_data.get("id")
            if user_backend_id:
                user_data = await api_client.get_user(user_backend_id)
        
        if not user_data:
            return False, None
        
        is_blocked = user_data.get("is_blocked", False)
        if is_blocked:
            return True, "Вы заблокированы. Обратитесь к администратору."
        
        return False, None
    except Exception as e:
        logger.error(f"Error checking user block status for {telegram_id}: {e}")
        return False, None


def clear_access_cache(telegram_id: Optional[int] = None):
    """Очищает кэш доступа."""
    if telegram_id:
        _access_cache.pop(telegram_id, None)
    else:
        _access_cache.clear()

