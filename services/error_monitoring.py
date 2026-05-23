"""
Утилиты для мониторинга ошибок через Sentry.
Централизованные функции для отправки ошибок и событий.
"""
import logging
from typing import Optional, Dict, Any
import sentry_sdk

logger = logging.getLogger(__name__)


def capture_error(
    error: Exception,
    context: Optional[Dict[str, Any]] = None,
    level: str = "error",
    user_id: Optional[int] = None,
    tags: Optional[Dict[str, str]] = None
):
    """
    Отправляет ошибку в Sentry с дополнительным контекстом.
    
    Args:
        error: Исключение для отправки
        context: Дополнительный контекст (dict)
        level: Уровень ошибки (error, warning, info)
        user_id: ID пользователя (если применимо)
        tags: Теги для фильтрации в Sentry
    """
    try:
        with sentry_sdk.push_scope() as scope:
            # Добавляем контекст
            if context:
                for key, value in context.items():
                    scope.set_context(key, value)
            
            # Добавляем теги
            if tags:
                for key, value in tags.items():
                    scope.set_tag(key, value)
            
            # Добавляем пользователя
            if user_id:
                scope.user = {"id": user_id}
            
            # Отправляем ошибку
            sentry_sdk.capture_exception(error)
            
        logger.error(f"Error sent to Sentry: {error}", exc_info=True)
    except Exception as e:
        # Если Sentry не настроен или произошла ошибка, просто логируем
        logger.warning(f"Failed to send error to Sentry: {e}")


def capture_message(
    message: str,
    level: str = "info",
    context: Optional[Dict[str, Any]] = None,
    tags: Optional[Dict[str, str]] = None
):
    """
    Отправляет сообщение в Sentry.
    
    Args:
        message: Текст сообщения
        level: Уровень сообщения (info, warning, error)
        context: Дополнительный контекст
        tags: Теги для фильтрации
    """
    try:
        with sentry_sdk.push_scope() as scope:
            if context:
                for key, value in context.items():
                    scope.set_context(key, value)
            
            if tags:
                for key, value in tags.items():
                    scope.set_tag(key, value)
            
            sentry_sdk.capture_message(message, level=level)
    except Exception as e:
        logger.warning(f"Failed to send message to Sentry: {e}")


def set_user_context(user_id: int, username: Optional[str] = None, email: Optional[str] = None):
    """Устанавливает контекст пользователя для последующих событий."""
    try:
        sentry_sdk.set_user({
            "id": user_id,
            "username": username,
            "email": email
        })
    except Exception:
        pass


def clear_user_context():
    """Очищает контекст пользователя."""
    try:
        sentry_sdk.set_user(None)
    except Exception:
        pass




