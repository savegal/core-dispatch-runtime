"""
Конфигурация Sentry для мониторинга ошибок.
"""
import logging
import sentry_sdk
from sentry_sdk.integrations.fastapi import FastApiIntegration
from sentry_sdk.integrations.sqlalchemy import SqlalchemyIntegration
from sentry_sdk.integrations.logging import LoggingIntegration
from sentry_sdk.integrations.redis import RedisIntegration
from config.settings import get_settings

logger = logging.getLogger(__name__)
settings = get_settings()


def init_sentry():
    """
    Инициализирует Sentry для мониторинга ошибок.
    Вызывается при старте приложения.
    """
    # Получаем DSN из переменных окружения
    sentry_dsn = getattr(settings, 'SENTRY_DSN', None)
    
    if not sentry_dsn:
        logger.warning("SENTRY_DSN not set, Sentry monitoring disabled")
        return
    
    try:
        sentry_sdk.init(
            dsn=sentry_dsn,
            environment=getattr(settings, 'ENV', 'production'),
            traces_sample_rate=0.1,  # 10% транзакций для performance monitoring
            profiles_sample_rate=0.1,  # 10% профилей для performance profiling
            enable_tracing=True,
            integrations=[
                FastApiIntegration(transaction_style='endpoint'),
                SqlalchemyIntegration(),
                LoggingIntegration(
                    level=logging.INFO,  # Логируем INFO и выше
                    event_level=logging.ERROR  # Отправляем в Sentry только ERROR и выше
                ),
                RedisIntegration(),
            ],
            # Игнорируем некоторые ошибки
            ignore_errors=[
                KeyboardInterrupt,  # Ctrl+C
                SystemExit,  # Выход из приложения
            ],
            # Дополнительные данные
            release=getattr(settings, 'APP_VERSION', '1.0.0'),
            before_send=before_send_handler,
        )
        logger.info("✅ Sentry initialized successfully")
    except Exception as e:
        logger.error(f"❌ Failed to initialize Sentry: {e}", exc_info=True)


def before_send_handler(event, hint):
    """
    Обработчик перед отправкой события в Sentry.
    Позволяет фильтровать или модифицировать события.
    """
    # Можно добавить логику фильтрации
    # Например, не отправлять определённые типы ошибок
    
    # Исключаем некоторые ожидаемые ошибки
    if 'exc_info' in hint:
        exc_type, exc_value, tb = hint['exc_info']
        # Не отправляем ошибки валидации в Sentry (они ожидаемые)
        if 'ValidationError' in str(exc_type):
            return None
        # Не отправляем 404 ошибки
        if 'NotFound' in str(exc_type):
            return None
    
    return event


def capture_exception(error: Exception, **kwargs):
    """Обёртка для отправки исключения в Sentry."""
    try:
        sentry_sdk.capture_exception(error, **kwargs)
    except Exception as e:
        logger.error(f"Failed to send exception to Sentry: {e}")


def capture_message(message: str, level: str = "info", **kwargs):
    """Обёртка для отправки сообщения в Sentry."""
    try:
        sentry_sdk.capture_message(message, level=level, **kwargs)
    except Exception as e:
        logger.error(f"Failed to send message to Sentry: {e}")




