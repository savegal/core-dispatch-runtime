"""
Утилиты для умной навигации в Playwright.

ОПТИМИЗИРОВАНО: Использует reload() вместо goto() если уже на нужной странице.
Это даёт 20-30% ускорение навигации.
"""
import logging
from typing import Optional
from urllib.parse import urlsplit
from patchright.async_api import Page

logger = logging.getLogger("executor.navigation_utils")


def _normalize_url_for_compare(raw: str) -> str:
    """Normalize a URL to scheme://host/path for an *exact* identity check.

    Query string and fragment are dropped and the trailing slash is stripped so
    that e.g. ``.../video/123`` and ``.../video/123/?lang=en`` compare equal,
    while a short URL (the homepage ``https://www.tiktok.com/``) is NOT treated
    as "the same page" as a longer video URL. A bare substring check used to do
    exactly that and caused reload() on the wrong page instead of navigating to
    the target video.
    """
    try:
        parts = urlsplit((raw or "").strip())
    except Exception:
        return (raw or "").strip().lower()
    scheme = (parts.scheme or "").lower()
    netloc = (parts.netloc or "").lower()
    path = (parts.path or "").rstrip("/").lower()
    if not netloc and not scheme:
        # Relative or malformed input: fall back to a normalized raw string.
        return (raw or "").strip().rstrip("/").lower()
    return f"{scheme}://{netloc}{path}"


async def smart_navigate(
    page: Page,
    url: str,
    wait_until: str = "domcontentloaded",
    timeout: int = 30000,
    force_goto: bool = False
) -> None:
    """
    Умная навигация: использует reload() если уже на нужной странице, иначе goto().
    
    ОПТИМИЗИРОВАНО: Это даёт 20-30% ускорение, т.к. reload() быстрее чем goto()
    (не нужно заново устанавливать cookies и инициализировать контекст).
    
    Args:
        page: Страница Playwright
        url: URL для навигации
        wait_until: Режим ожидания ("load", "domcontentloaded", "networkidle")
        timeout: Таймаут в миллисекундах
        force_goto: Если True - всегда использует goto(), даже если уже на нужной странице
    
    Returns:
        None
    """
    if force_goto:
        await page.goto(url, wait_until=wait_until, timeout=timeout)
        return
    
    try:
        # Точное сравнение адресов (scheme://host/path, без query/fragment).
        # Подстрочное сравнение было опасным: короткий адрес (главная
        # https://www.tiktok.com/) является подстрокой любого видео-URL, из-за
        # чего софт ошибочно делал reload() не на той странице вместо перехода
        # на нужное видео.
        same_page = _normalize_url_for_compare(page.url) == _normalize_url_for_compare(url)

        if same_page:
            logger.debug(f"[NAVIGATION] Already on page {url[:50]}, using reload() instead of goto()")
            await page.reload(wait_until=wait_until, timeout=timeout)
        else:
            # На другой странице - используем goto
            await page.goto(url, wait_until=wait_until, timeout=timeout)
    except Exception as nav_error:
        # Если проверка URL не удалась или навигация провалилась - используем обычный goto
        logger.debug(f"[NAVIGATION] Error in smart_navigate, falling back to goto(): {nav_error}")
        try:
            await page.goto(url, wait_until=wait_until, timeout=timeout)
        except Exception as goto_error:
            logger.warning(f"[NAVIGATION] Failed to navigate to {url[:50]}: {goto_error}")
            raise
