"""
Селекторы и поиск элементов на странице TikTok.

Централизует логику поиска элементов с логированием и статистикой.
"""
import logging
from typing import List, Optional, Tuple
from patchright.async_api import Page, ElementHandle
from executor.tiktok_selectors import selectors as tiktok_selectors

logger = logging.getLogger("executor.tiktok.selectors")


# Статистика использования селекторов (для анализа "здоровья селекторов")
_selector_stats: dict = {}


def _record_selector_usage(selector_name: str, selector: str, found: bool):
    """Записывает статистику использования селектора."""
    key = f"{selector_name}:{selector[:50]}"
    if key not in _selector_stats:
        _selector_stats[key] = {"total": 0, "found": 0}
    _selector_stats[key]["total"] += 1
    if found:
        _selector_stats[key]["found"] += 1


def get_selector_health_report() -> dict:
    """
    Возвращает отчёт о "здоровье" селекторов.
    
    Полезно для обнаружения проблем после обновления TikTok.
    """
    report = {}
    for key, stats in _selector_stats.items():
        success_rate = stats["found"] / stats["total"] if stats["total"] > 0 else 0
        report[key] = {
            "total_attempts": stats["total"],
            "success_count": stats["found"],
            "success_rate": success_rate,
            "health": "good" if success_rate > 0.8 else "warning" if success_rate > 0.5 else "bad"
        }
    return report


async def find_first_visible(
    page: Page,
    selector_list: List[str],
    name: str = "element",
    timeout_ms: int = 3000,
    raise_if_not_found: bool = False
) -> Tuple[Optional[ElementHandle], Optional[str]]:
    """
    Находит первый видимый элемент из списка селекторов.
    
    Логирует, какой селектор сработал, и собирает статистику.
    Это критично для обнаружения проблем после обновления TikTok.
    
    Args:
        page: Страница Playwright
        selector_list: Список селекторов для перебора
        name: Имя элемента (для логирования)
        timeout_ms: Таймаут на поиск в миллисекундах
        raise_if_not_found: Выбрасывать исключение если не найдено
        
    Returns:
        tuple: (ElementHandle или None, имя селектора который сработал или None)
    """
    if not selector_list:
        if raise_if_not_found:
            raise ValueError(f"No selectors provided for {name}")
        return None, None
    
    logger.debug(f"[SELECTORS] Searching for {name} using {len(selector_list)} selectors")
    
    # Пробуем каждый селектор
    for selector in selector_list:
        try:
            # Пробуем найти элемент
            element = await page.query_selector(selector)
            
            if element:
                # Проверяем видимость
                is_visible = await element.is_visible()
                
                if is_visible:
                    # Проверяем, что элемент действительно кликабелен (имеет размеры)
                    box = await element.bounding_box()
                    if box and box['width'] > 0 and box['height'] > 0:
                        _record_selector_usage(name, selector, True)
                        logger.debug(f"[SELECTORS] ✓ Found {name} using selector: {selector[:80]}")
                        return element, selector
            
            _record_selector_usage(name, selector, False)
            
        except Exception as e:
            logger.debug(f"[SELECTORS] Error with selector '{selector[:50]}' for {name}: {e}")
            _record_selector_usage(name, selector, False)
            continue
    
    # Не нашли ни один селектор
    logger.warning(f"[SELECTORS] ✗ {name} not found with any of {len(selector_list)} selectors")
    
    if raise_if_not_found:
        raise ValueError(f"{name} not found with any selector")
    
    return None, None


async def find_comment_input(page: Page, timeout_ms: int = 5000) -> Tuple[Optional[ElementHandle], Optional[str]]:
    """Находит поле ввода комментария."""
    selector_list = tiktok_selectors.get_comment_input_selectors()
    return await find_first_visible(page, selector_list, "comment_input", timeout_ms)


async def find_post_button(page: Page, timeout_ms: int = 3000) -> Tuple[Optional[ElementHandle], Optional[str]]:
    """Находит кнопку отправки комментария."""
    selector_list = [
        tiktok_selectors.POST_COMMENT_BUTTON,
        tiktok_selectors.POST_BUTTON,
        'button:has-text("Post")',
        'button:has-text("Опубликовать")',
    ]
    return await find_first_visible(page, selector_list, "post_button", timeout_ms)


async def find_reply_button(page: Page, timeout_ms: int = 3000) -> Tuple[Optional[ElementHandle], Optional[str]]:
    """Находит кнопку ответа на комментарий."""
    selector_list = [
        tiktok_selectors.REPLY_BUTTON,
        'button:has-text("Reply")',
        'button:has-text("Ответить")',
    ]
    return await find_first_visible(page, selector_list, "reply_button", timeout_ms)


async def find_reply_input(page: Page, timeout_ms: int = 5000) -> Tuple[Optional[ElementHandle], Optional[str]]:
    """Находит поле ввода ответа."""
    selector_list = [
        tiktok_selectors.REPLY_INPUT,
        'textarea[placeholder*="reply" i]',
        'textarea[placeholder*="ответ" i]',
    ]
    return await find_first_visible(page, selector_list, "reply_input", timeout_ms)


async def find_like_button(page: Page, timeout_ms: int = 3000) -> Tuple[Optional[ElementHandle], Optional[str]]:
    """Находит кнопку лайка комментария."""
    selector_list = [
        tiktok_selectors.LIKE_BUTTON,
        '[data-e2e="comment-like"]',
        'button[aria-label*="like" i]',
    ]
    return await find_first_visible(page, selector_list, "like_button", timeout_ms)

