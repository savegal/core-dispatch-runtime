"""
Middleware для проверки прав администратора перед обработкой админ-команд.

ИСПРАВЛЕНО: Централизованная проверка прав доступа для всех админ-роутов.
Предотвращает доступ к админ-функциям для неавторизованных пользователей.
"""
from typing import Callable, Dict, Any, Awaitable
from aiogram import BaseMiddleware
from aiogram.types import TelegramObject, CallbackQuery, Message
from bot.services.api_client import api_client
from bot.config import load_config
import logging

logger = logging.getLogger("bot.middleware.admin")
config = load_config()


def is_admin_user(telegram_id: int, role: str) -> bool:
    """Проверяет, является ли пользователь админом."""
    return role in ("admin", "owner") or telegram_id == config.get("OWNER_TELEGRAM_ID")


class AdminMiddleware(BaseMiddleware):
    """
    Middleware для проверки прав администратора.
    
    Применяется к роутерам с админ-функциями.
    Автоматически проверяет права доступа перед выполнением хендлера.
    """
    
    async def __call__(
        self,
        handler: Callable[[TelegramObject, Dict[str, Any]], Awaitable[Any]],
        event: TelegramObject,
        data: Dict[str, Any]
    ) -> Any:
        """
        Проверяет права администратора перед выполнением хендлера.
        
        Если пользователь не является админом - возвращает ранний ответ и не выполняет хендлер.
        """
        # Получаем user_id из события
        if isinstance(event, CallbackQuery):
            user_id = event.from_user.id
        elif isinstance(event, Message):
            user_id = event.from_user.id
        else:
            # Для других типов событий пропускаем (не админ-команды)
            return await handler(event, data)
        
        # Проверяем права доступа
        try:
            user_data = await api_client.sync_telegram(user_id, None)
            role = user_data.get("role", "user")
            is_admin = is_admin_user(user_id, role)
            
            if not is_admin:
                # Логируем попытку несанкционированного доступа
                logger.warning(f"Unauthorized admin access attempt by user {user_id} (role: {role})")
                
                # Отвечаем пользователю
                if isinstance(event, CallbackQuery):
                    await event.answer("Доступ запрещён. Только для администраторов", show_alert=True)
                elif isinstance(event, Message):
                    await event.answer("Доступ запрещён. Только для администраторов")
                
                # Не выполняем хендлер
                return
            
            # Пользователь является админом - выполняем хендлер
            return await handler(event, data)
            
        except Exception as e:
            logger.error(f"Error checking admin access for user {user_id}: {e}", exc_info=True)
            
            # При ошибке проверки - запрещаем доступ (безопасный режим)
            if isinstance(event, CallbackQuery):
                await event.answer("Ошибка проверки доступа", show_alert=True)
            elif isinstance(event, Message):
                await event.answer("Ошибка проверки доступа")
            
            return










