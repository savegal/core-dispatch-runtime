"""
Middleware для ограничения частоты запросов (Rate Limiting).

Защищает бота от спама и злоупотреблений.
"""
from typing import Callable, Dict, Any, Awaitable
from aiogram import BaseMiddleware
from aiogram.types import TelegramObject, CallbackQuery, Message
from collections import defaultdict
from datetime import datetime, timedelta
import logging

logger = logging.getLogger("bot.middleware.rate_limit")

# Хранилище для отслеживания запросов (в памяти, для продакшена лучше использовать Redis)
_user_requests: Dict[int, list[datetime]] = defaultdict(list)
_user_blocks: Dict[int, datetime] = {}

# ИСПРАВЛЕНО: Настройки rate limiting вынесены в settings
from config.settings import get_settings
settings = get_settings()

# Настройки rate limiting (можно переопределить в settings в будущем)
MESSAGES_PER_MINUTE = getattr(settings, 'BOT_RATE_LIMIT_MESSAGES_PER_MINUTE', 30)  # Максимум сообщений в минуту
CALLBACKS_PER_MINUTE = getattr(settings, 'BOT_RATE_LIMIT_CALLBACKS_PER_MINUTE', 60)  # Максимум callback queries в минуту
BLOCK_DURATION_MINUTES = getattr(settings, 'BOT_RATE_LIMIT_BLOCK_DURATION_MINUTES', 5)  # Длительность блокировки при превышении


class RateLimitMiddleware(BaseMiddleware):
    """
    Middleware для ограничения частоты запросов.
    
    Ограничивает:
    - Сообщения: 30/минуту
    - Callback queries: 60/минуту
    
    При превышении блокирует пользователя на 5 минут.
    """
    
    async def __call__(
        self,
        handler: Callable[[TelegramObject, Dict[str, Any]], Awaitable[Any]],
        event: TelegramObject,
        data: Dict[str, Any]
    ) -> Any:
        """Проверяет rate limit перед выполнением хендлера."""
        # Получаем user_id из события
        if isinstance(event, CallbackQuery):
            user_id = event.from_user.id
            is_callback = True
            limit = CALLBACKS_PER_MINUTE
        elif isinstance(event, Message):
            user_id = event.from_user.id
            is_callback = False
            limit = MESSAGES_PER_MINUTE
        else:
            # Для других типов событий пропускаем
            return await handler(event, data)
        
        # Проверяем, не заблокирован ли пользователь
        if user_id in _user_blocks:
            block_until = _user_blocks[user_id]
            if datetime.now() < block_until:
                remaining = (block_until - datetime.now()).total_seconds()
                logger.warning(f"User {user_id} is rate limited. Block expires in {remaining:.0f} seconds")
                
                if isinstance(event, CallbackQuery):
                    await event.answer(
                        f"⚠️ Слишком много запросов. Подождите {int(remaining / 60) + 1} минут.",
                        show_alert=True
                    )
                elif isinstance(event, Message):
                    await event.answer(
                        f"⚠️ Слишком много запросов. Подождите {int(remaining / 60) + 1} минут."
                    )
                
                return  # Не выполняем хендлер
        
        # Очищаем старые записи (старше 1 минуты)
        now = datetime.now()
        minute_ago = now - timedelta(minutes=1)
        _user_requests[user_id] = [
            req_time for req_time in _user_requests[user_id]
            if req_time > minute_ago
        ]
        
        # Проверяем лимит
        request_count = len(_user_requests[user_id])
        
        if request_count >= limit:
            # Превышен лимит - блокируем пользователя
            block_until = now + timedelta(minutes=BLOCK_DURATION_MINUTES)
            _user_blocks[user_id] = block_until
            
            logger.warning(
                f"Rate limit exceeded for user {user_id}. "
                f"Blocked until {block_until}. "
                f"Requests: {request_count}/{limit}"
            )
            
            if isinstance(event, CallbackQuery):
                await event.answer(
                    f"⚠️ Слишком много запросов. Заблокировано на {BLOCK_DURATION_MINUTES} минут.",
                    show_alert=True
                )
            elif isinstance(event, Message):
                await event.answer(
                    f"⚠️ Слишком много запросов. Заблокировано на {BLOCK_DURATION_MINUTES} минут."
                )
            
            return  # Не выполняем хендлер
        
        # Добавляем текущий запрос
        _user_requests[user_id].append(now)
        
        # Выполняем хендлер
        return await handler(event, data)

