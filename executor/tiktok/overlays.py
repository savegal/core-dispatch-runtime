"""
Управление оверлеями, баннерами и попапами на TikTok.

Централизует логику закрытия всех типов оверлеев.
"""
import asyncio
import logging
from typing import List
from patchright.async_api import Page
from executor.tiktok_selectors import selectors as tiktok_selectors
from executor.adaptive_delays import get_adaptive_delays

logger = logging.getLogger("executor.tiktok.overlays")
adaptive_delays = get_adaptive_delays()


async def close_all_overlays(
    page: Page,
    max_attempts: int = 3,
    using_proxy: bool = False
) -> int:
    """
    Закрывает все оверлеи, баннеры и попапы на странице.
    
    Args:
        page: Страница Playwright
        max_attempts: Максимальное количество попыток
        using_proxy: Используется ли прокси (для адаптивных задержек)
        
    Returns:
        int: Количество закрытых оверлеев
    """
    closed_count = 0
    
    overlay_selectors = tiktok_selectors.get_overlay_selectors()
    
    for attempt in range(max_attempts):
        attempt_closed = 0
        
        # Закрываем cookie баннеры
        cookie_selectors = [
            'button[data-e2e="accept-button"]',
            'button[aria-label*="accept" i]',
            'button:has-text("Accept")',
            'button:has-text("Принять")',
            'button:has-text("I agree")',
            'button:has-text("Согласен")',
        ]
        for selector in cookie_selectors:
            try:
                elements = await page.query_selector_all(selector)
                for elem in elements:
                    if await elem.is_visible():
                        await elem.click()
                        await asyncio.sleep(adaptive_delays.get_delay('overlay_close', using_proxy=using_proxy) / 1000)
                        attempt_closed += 1
                        logger.debug(f"[OVERLAYS] Closed cookie banner with selector: {selector[:50]}")
            except Exception:
                continue
        
        # Закрываем "Continue watching" попапы
        continue_selectors = [
            'button:has-text("Continue watching")',
            'button:has-text("Продолжить просмотр")',
            'button:has-text("Watch more")',
            '[data-e2e*="continue"]',
        ]
        for selector in continue_selectors:
            try:
                elements = await page.query_selector_all(selector)
                for elem in elements:
                    if await elem.is_visible():
                        await elem.click()
                        await asyncio.sleep(adaptive_delays.get_delay('overlay_close', using_proxy=using_proxy) / 1000)
                        attempt_closed += 1
                        logger.debug(f"[OVERLAYS] Closed continue watching with selector: {selector[:50]}")
            except Exception:
                continue
        
        # Закрываем age restriction попапы
        age_selectors = [
            'button:has-text("I am 18")',
            'button:has-text("Мне 18")',
            'button:has-text("Confirm")',
            '[data-e2e*="age"]',
        ]
        for selector in age_selectors:
            try:
                elements = await page.query_selector_all(selector)
                for elem in elements:
                    if await elem.is_visible():
                        await elem.click()
                        await asyncio.sleep(adaptive_delays.get_delay('overlay_close', using_proxy=using_proxy) / 1000)
                        attempt_closed += 1
                        logger.debug(f"[OVERLAYS] Closed age restriction with selector: {selector[:50]}")
            except Exception:
                continue
        
        # Закрываем модальные окна
        modal_selectors = [
            '.TUXModal-overlay',
            '[class*="Modal-overlay"]',
            '[data-e2e*="modal"]',
            'button[aria-label*="close" i]',
            'button[aria-label*="закрыть" i]',
        ]
        for selector in modal_selectors:
            try:
                elements = await page.query_selector_all(selector)
                for elem in elements:
                    if await elem.is_visible():
                        await elem.click()
                        await asyncio.sleep(adaptive_delays.get_delay('overlay_close', using_proxy=using_proxy) / 1000)
                        attempt_closed += 1
                        logger.debug(f"[OVERLAYS] Closed modal with selector: {selector[:50]}")
            except Exception:
                continue
        
        # Пробуем Escape для закрытия оверлеев
        try:
            await page.keyboard.press("Escape")
            await asyncio.sleep(adaptive_delays.get_delay('overlay_close', using_proxy=using_proxy) / 1000)
            await page.keyboard.press("Escape")  # Двойной Escape для надёжности
            await asyncio.sleep(adaptive_delays.get_delay('overlay_close', using_proxy=using_proxy) / 1000)
        except Exception:
            pass
        
        closed_count += attempt_closed
        
        # Если в этой попытке ничего не закрыли - прекращаем
        if attempt_closed == 0:
            break
        
        # Небольшая задержка перед следующей попыткой
        if attempt < max_attempts - 1:
            await asyncio.sleep(adaptive_delays.get_delay('overlay_close', using_proxy=using_proxy) / 1000)
    
    if closed_count > 0:
        logger.info(f"[OVERLAYS] Closed {closed_count} overlay(s) in {attempt + 1} attempt(s)")
    else:
        logger.debug("[OVERLAYS] No overlays found to close")
    
    return closed_count

