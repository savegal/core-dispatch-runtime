import asyncio
import random
import logging
from typing import Optional
from patchright.async_api import BrowserContext
from config.settings import get_settings

settings = get_settings()
logger = logging.getLogger("executor.utils")


async def random_delay(min_seconds: float = None, max_seconds: float = None) -> None:
    """
    Асинхронная задержка. НЕ БЛОКИРУЕТ работу других ботов.
    
    ВАЖНО: Использует asyncio.sleep вместо time.sleep для работы в async контексте.
    Если min_seconds/max_seconds не указаны, использует настройки из settings.
    
    Args:
        min_seconds: Минимальная задержка в секундах (если None - использует MIN_DELAY_BETWEEN_ACTIONS_MS из settings)
        max_seconds: Максимальная задержка в секундах (если None - использует MAX_DELAY_BETWEEN_ACTIONS_MS из settings)
    """
    if settings.RANDOM_DELAY:
        # Если параметры не указаны, используем настройки из settings
        if min_seconds is None:
            min_seconds = settings.MIN_DELAY_BETWEEN_ACTIONS_MS / 1000.0
        if max_seconds is None:
            max_seconds = settings.MAX_DELAY_BETWEEN_ACTIONS_MS / 1000.0
        
        delay = random.uniform(min_seconds, max_seconds)
        await asyncio.sleep(delay)  # КРИТИЧНО: используем await asyncio.sleep вместо time.sleep


def random_delay_sync(min_seconds: float = 3.0, max_seconds: float = 12.0) -> None:
    """
    Синхронная версия random_delay для использования в синхронном коде (executor.py).
    
    ВАЖНО: Используется только в синхронном контексте (executor.py с threads).
    В async коде используйте async def random_delay().
    
    Args:
        min_seconds: Минимальная задержка в секундах
        max_seconds: Максимальная задержка в секундах
    """
    import time
    if settings.RANDOM_DELAY:
        delay = random.uniform(min_seconds, max_seconds)
        time.sleep(delay)  # OK для синхронного кода в threads


async def save_storage_state_if_needed(context: BrowserContext, cookie_file_path: Optional[str] = None) -> None:
    """
    Сохраняет состояние браузерного контекста (cookies, localStorage) в файл, если указан путь.
    
    Эта функция используется для сохранения обновлённых cookies после работы с аккаунтом,
    чтобы при следующем использовании аккаунта были актуальные cookies.
    
    Args:
        context: BrowserContext из Playwright
        cookie_file_path: Опциональный путь к файлу для сохранения. Если None, функция ничего не делает.
    """
    if not cookie_file_path:
        # Если путь не указан, ничего не делаем
        return
    
    try:
        from pathlib import Path
        path = Path(cookie_file_path)
        # Создаём директорию, если её нет
        path.parent.mkdir(parents=True, exist_ok=True)
        # Сохраняем storage state
        await context.storage_state(path=str(path))
        logger.debug(f"Saved storage state to {cookie_file_path}")
    except Exception as e:
        # Логируем ошибку, но не прерываем выполнение
        logger.warning(f"Failed to save storage state to {cookie_file_path}: {e}")

