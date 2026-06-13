"""
Умный поиск элементов с множественными стратегиями, быстрыми таймаутами и агрессивными retry.
Оптимизировано для скорости и надежности.
"""
import asyncio
import logging
import time
from typing import Optional, Tuple, List, Dict, Any
from patchright.async_api import Page
from executor.comment_utils import (
    log_step,
    check_page_alive,
    safe_wait_for_selector,
    safe_query_selector,
    safe_query_selector_all,
    quick_validate_element
)
from executor.tiktok_selectors import selectors

logger = logging.getLogger("executor.smart_finder")


async def _detect_redirect_after_comment_click(page: Page) -> Optional[str]:
    try:
        current_url = (page.url or "").lower()
    except Exception:
        return None

    redirect_markers = ("/messages", "/inbox", "/login", "/signup", "/verify", "/auth", "/register")
    if any(marker in current_url for marker in redirect_markers):
        return current_url
    return None


def get_timeout(using_proxy: bool, base_timeout: int = 5000) -> int:
    """
    Умные таймауты: минимум ожидания, максимум 10 секунд.
    
    Args:
        using_proxy: True если используется прокси
        base_timeout: Базовый таймаут в миллисекундах
        
    Returns:
        int: Таймаут в миллисекундах
    """
    if using_proxy:
        return min(int(base_timeout * 1.5), 4000)  # Максимум 10 сек для прокси
    return min(base_timeout, 3000)  # Максимум 6 сек для прямого


async def find_comment_section_fast(
    page: Page,
    using_proxy: bool = False,
    max_attempts: int = 4
) -> Tuple[Optional[Any], Dict[str, Any]]:
    """
    Быстрый поиск секции комментариев с множественными стратегиями.
    
    Args:
        page: Страница Playwright
        using_proxy: True если используется прокси
        max_attempts: Максимальное количество попыток
        
    Returns:
        tuple: (element, info_dict)
    """
    log_step("Finding comment section", {"attempts": max_attempts, "proxy": using_proxy})
    _start_ts = time.monotonic()
    _MAX_TOTAL_SECONDS = 20
    
    info = {
        "found": False,
        "attempt": 0,
        "strategy": None,
        "error": None,
        "redirect_detected": False,
        "redirect_url": None,
    }

    # ИСПРАВЛЕНО: Сначала пытаемся кликнуть на кнопку комментариев (если она есть)
    # TikTok часто скрывает секцию комментариев до клика
    try:
        comment_button_selectors = [
            'button[data-e2e="comments"]',  # Основная кнопка
            '[data-e2e="comments"]',  # Без указания tag
            'button:has([data-e2e="comment-icon"])',  # Кнопка с иконкой
            '[data-e2e="comment-icon"]',  # Сама иконка (может быть кликабельной)
        ]
        
        for btn_selector in comment_button_selectors:
            try:
                button, _ = await safe_query_selector(page, btn_selector)
                if button:
                    # ИСПРАВЛЕНО: Пробуем несколько способов клика
                    clicked = False
                    
                    # Способ 1: Обычный клик (если видим)
                    try:
                        is_visible = await button.is_visible()
                        if is_visible:
                            await button.click(timeout=2000)
                            clicked = True
                    except Exception:
                        pass
                    
                    # Способ 2: JavaScript клик (работает даже если невидим)
                    if not clicked:
                        try:
                            await button.evaluate("el => el.click()")
                            clicked = True
                        except Exception:
                            pass
                    
                    # Способ 3: Клик по координатам (если элемент есть в DOM)
                    if not clicked:
                        try:
                            box = await button.bounding_box()
                            if box:
                                await page.mouse.click(box["x"] + box["width"] / 2, box["y"] + box["height"] / 2)
                                clicked = True
                        except Exception:
                            pass
                    
                    if clicked:
                        await asyncio.sleep(1.2 if using_proxy else 0.6)
                        redirect_url = await _detect_redirect_after_comment_click(page)
                        if redirect_url:
                            info["error"] = f"Redirected after comment click: {redirect_url[:100]}"
                            info["redirect_detected"] = True
                            info["redirect_url"] = redirect_url
                            log_step("Redirected after comment section click", {"url": redirect_url[:100]})
                            return None, info
                        log_step("Clicked comment button to open section", {"selector": btn_selector[:50]})
                        break  # Кликнули, выходим
            except Exception:
                continue
    except Exception:
        pass  # Игнорируем ошибки клика - продолжаем поиск
    
    # ИСПРАВЛЕНО: Расширенный список селекторов для TikTok 2026
    section_selectors = [
        # Приоритет 1: data-e2e атрибуты (самые надежные)
        '[data-e2e="comment-list"]',
        '[data-e2e="comment-container"]',
        '[data-e2e*="comment"]',
        # Приоритет 2: Классы (могут меняться, но часто работают)
        '[class*="CommentList"]',
        '[class*="CommentContainer"]',
        '[class*="comment-section"]',
        '[class*="Comment"]',
        # Приоритет 3: Общие селекторы
        selectors.COMMENT_SECTION,
        # Приоритет 4: Fallback - ищем любые элементы с "comment" в атрибутах
        '[id*="comment" i]',
        '[aria-label*="comment" i]',
    ]
    
    
    info = {
        "found": False,
        "attempt": 0,
        "strategy": None,
        "error": None,
        "redirect_detected": False,
        "redirect_url": None,
    }
    
    for attempt in range(max_attempts):
        info["attempt"] = attempt + 1
        if time.monotonic() - _start_ts > _MAX_TOTAL_SECONDS:
            info["error"] = f"Comment section search exceeded {_MAX_TOTAL_SECONDS}s ceiling"
            info["timeout_ceiling"] = True
            return None, info

        redirect_url = await _detect_redirect_after_comment_click(page)
        if redirect_url:
            info["error"] = f"Redirected during comment section search: {redirect_url[:100]}"
            info["redirect_detected"] = True
            info["redirect_url"] = redirect_url
            return None, info
        
        # Проверяем, что страница жива
        is_alive, error = await check_page_alive(page)
        if not is_alive:
            info["error"] = error
            log_step(f"Comment section search failed (attempt {attempt + 1})", {"error": error})
            return None, info
        
        # ИСПРАВЛЕНО: Убрана проверка редиректа на /messages из функции поиска
        # Проверка редиректа будет выполнена в основном коде ПОСЛЕ поиска секции комментариев
        # Это позволяет избежать ложных срабатываний и дает странице время на загрузку
        
        # Стратегия 1: Параллельный поиск всех селекторов
        for selector in section_selectors:
            if time.monotonic() - _start_ts > _MAX_TOTAL_SECONDS:
                info["error"] = f"Comment section search exceeded {_MAX_TOTAL_SECONDS}s ceiling"
                info["timeout_ceiling"] = True
                return None, info
            redirect_url = await _detect_redirect_after_comment_click(page)
            if redirect_url:
                info["error"] = f"Redirected during comment section search: {redirect_url[:100]}"
                info["redirect_detected"] = True
                info["redirect_url"] = redirect_url
                return None, info
            try:
                element, error = await safe_wait_for_selector(
                    page,
                    selector,
                    timeout=get_timeout(using_proxy, 1500),
                    state="attached"
                )
                if element:
                    info["found"] = True
                    info["strategy"] = f"wait_for_selector: {selector[:50]}"
                    log_step("Comment section found", {"strategy": info["strategy"], "attempt": attempt + 1})
                    return element, info
            except Exception as e:
                continue
        
        # Стратегия 2: Прокрутка + повторный поиск (если не последняя попытка)
        if attempt < max_attempts - 1:
            try:
                if time.monotonic() - _start_ts > _MAX_TOTAL_SECONDS:
                    info["error"] = f"Comment section search exceeded {_MAX_TOTAL_SECONDS}s ceiling"
                    info["timeout_ceiling"] = True
                    return None, info
                is_alive, _ = await check_page_alive(page)
                if not is_alive:
                    break
                
                # Прокручиваем к секции комментариев
                await page.evaluate("""
                    () => {
                        const section = document.querySelector('[data-e2e="comment-list"], [class*="CommentList"]');
                        if (section) {
                            section.scrollIntoView({ behavior: 'smooth', block: 'center' });
                        } else {
                            window.scrollTo(0, document.body.scrollHeight * 0.7);
                        }
                    }
                """)
                await asyncio.sleep(1.0 if using_proxy else 0.55)
                log_step(f"Scrolled for comment section (attempt {attempt + 1})")
            except Exception:
                pass
        
        # Стратегия 3: Клик на "View comments" (только на последней попытке)
        if attempt == max_attempts - 1:
            try:
                if time.monotonic() - _start_ts > _MAX_TOTAL_SECONDS:
                    info["error"] = f"Comment section search exceeded {_MAX_TOTAL_SECONDS}s ceiling"
                    info["timeout_ceiling"] = True
                    return None, info
                is_alive, _ = await check_page_alive(page)
                if not is_alive:
                    break
                
                view_button_selectors = [
                    selectors.VIEW_COMMENTS_BUTTON,
                    'button:has-text("View comments")',
                    'button:has-text("Показать комментарии")',
                    '[data-e2e*="view-comments"]',
                ]
                
                for btn_selector in view_button_selectors:
                    try:
                        button, _ = await safe_query_selector(page, btn_selector)
                        if button:
                            is_visible = await button.is_visible()
                            if is_visible:
                                await button.click()
                                await asyncio.sleep(1.3 if using_proxy else 0.7)
                                log_step("Clicked 'View comments' button")
                                
                                # Проверяем, появилась ли секция
                                for selector in section_selectors:
                                    element, _ = await safe_query_selector(page, selector)
                                    if element:
                                        info["found"] = True
                                        info["strategy"] = f"view_comments_click: {selector[:50]}"
                                        return element, info
                    except Exception:
                        continue
            except Exception:
                pass
    
    # Если не нашли после всех попыток - запускаем диагностику
    if not info["found"]:
        try:
            from executor.page_diagnostics import log_page_diagnostics
            await log_page_diagnostics(page, "comment_section_not_found")
        except Exception as diag_error:
            logger.debug(f"Diagnostics failed: {diag_error}")
    
    info["error"] = "Comment section not found after all attempts"
    log_step("Comment section not found", {"attempts": max_attempts})
    return None, info


async def find_comment_input_fast(
    page: Page,
    using_proxy: bool = False,
    max_attempts: int = 4
) -> Tuple[Optional[Any], Dict[str, Any]]:
    """
    Быстрый поиск поля ввода комментария с множественными стратегиями.
    
    Args:
        page: Страница Playwright
        using_proxy: True если используется прокси
        max_attempts: Максимальное количество попыток
        
    Returns:
        tuple: (element, info_dict)
    """
    log_step("Finding comment input", {"attempts": max_attempts, "proxy": using_proxy})
    
    info = {
        "found": False,
        "attempt": 0,
        "strategy": None,
        "error": None,
        "redirect_detected": False,
        "redirect_url": None,
    }
    
    # ИСПРАВЛЕНО: Убрана проверка редиректа на /messages перед началом поиска
    # Проверка редиректа будет выполнена в основном коде ПОСЛЕ поиска поля ввода
    # Это позволяет избежать ложных срабатываний и дает странице время на загрузку
    
    # ИСПРАВЛЕНО: Сначала пытаемся кликнуть на кнопку комментариев (если она есть)
    # TikTok часто скрывает поле ввода до клика на кнопку комментариев
    try:
        comment_button_selectors = [
            'button[data-e2e="comments"]',  # Основная кнопка
            '[data-e2e="comments"]',  # Без указания tag
            'button:has([data-e2e="comment-icon"])',  # Кнопка с иконкой
            '[data-e2e="comment-icon"]',  # Сама иконка (может быть кликабельной)
            'button:has-text("Comments")',  # По тексту
            'button:has-text("Комментарии")',  # По тексту (RU)
        ]
        
        for btn_selector in comment_button_selectors:
            try:
                button, _ = await safe_query_selector(page, btn_selector)
                if button:
                    # ИСПРАВЛЕНО: Пробуем несколько способов клика
                    clicked = False
                    
                    # Способ 1: Обычный клик (если видим)
                    try:
                        is_visible = await button.is_visible()
                        if is_visible:
                            await button.click(timeout=2000)
                            clicked = True
                    except Exception:
                        pass
                    
                    # Способ 2: JavaScript клик (работает даже если невидим)
                    if not clicked:
                        try:
                            await button.evaluate("el => el.click()")
                            clicked = True
                        except Exception:
                            pass
                    
                    # Способ 3: Клик по координатам (если элемент есть в DOM)
                    if not clicked:
                        try:
                            box = await button.bounding_box()
                            if box:
                                await page.mouse.click(box["x"] + box["width"] / 2, box["y"] + box["height"] / 2)
                                clicked = True
                        except Exception:
                            pass
                    
                    if clicked:
                        await asyncio.sleep(2.0 if using_proxy else 1.0)
                        redirect_url = await _detect_redirect_after_comment_click(page)
                        if redirect_url:
                            info["error"] = f"Redirected after comment click: {redirect_url[:100]}"
                            info["redirect_detected"] = True
                            info["redirect_url"] = redirect_url
                            log_step("Redirected after comment input click", {"url": redirect_url[:100]})
                            return None, info
                        log_step("Clicked comment button to open input", {"selector": btn_selector[:50]})
                        break  # Кликнули, выходим
            except Exception:
                continue
    except Exception:
        pass  # Игнорируем ошибки клика - продолжаем поиск
    
    input_selectors = selectors.get_comment_input_selectors()
    _start_ts = time.monotonic()
    _MAX_TOTAL_SECONDS = 30
    
    info = {
        "found": False,
        "attempt": 0,
        "strategy": None,
        "error": None,
        "redirect_detected": False,
        "redirect_url": None,
    }
    
    for attempt in range(max_attempts):
        info["attempt"] = attempt + 1
        if time.monotonic() - _start_ts > _MAX_TOTAL_SECONDS:
            info["error"] = f"Comment input search exceeded {_MAX_TOTAL_SECONDS}s ceiling"
            info["timeout_ceiling"] = True
            return None, info

        redirect_url = await _detect_redirect_after_comment_click(page)
        if redirect_url:
            info["error"] = f"Redirected during comment section search: {redirect_url[:100]}"
            info["redirect_detected"] = True
            info["redirect_url"] = redirect_url
            return None, info
        
        # Проверяем, что страница жива
        is_alive, error = await check_page_alive(page)
        if not is_alive:
            info["error"] = error
            log_step(f"Comment input search failed (attempt {attempt + 1})", {"error": error})
            return None, info
        
        # ИСПРАВЛЕНО: Убрана проверка редиректа на /messages во время поиска
        # Проверка редиректа будет выполнена в основном коде ПОСЛЕ поиска поля ввода
        # Это позволяет избежать ложных срабатываний и дает странице время на загрузку
        
        # Стратегия 1: Параллельный поиск всех типов элементов
        for selector in input_selectors:
            if time.monotonic() - _start_ts > _MAX_TOTAL_SECONDS:
                info["error"] = f"Comment input search exceeded {_MAX_TOTAL_SECONDS}s ceiling"
                info["timeout_ceiling"] = True
                return None, info
            redirect_url = await _detect_redirect_after_comment_click(page)
            if redirect_url:
                info["error"] = f"Redirected during comment input search: {redirect_url[:100]}"
                info["redirect_detected"] = True
                info["redirect_url"] = redirect_url
                return None, info
            try:
                element, error = await safe_wait_for_selector(
                    page,
                    selector,
                    timeout=get_timeout(using_proxy, 2000),
                    state="visible"
                )
                if element:
                    # Быстрая валидация
                    is_valid, validation_error = await quick_validate_element(element, "comment_input")
                    if is_valid:
                        info["found"] = True
                        info["strategy"] = f"wait_for_selector: {selector[:50]}"
                        log_step("Comment input found", {"strategy": info["strategy"], "attempt": attempt + 1})
                        return element, info
            except Exception:
                continue
        
        # Стратегия 2: query_selector_all + фильтрация (быстрее чем wait_for_selector)
        try:
            if time.monotonic() - _start_ts > _MAX_TOTAL_SECONDS:
                info["error"] = f"Comment input search exceeded {_MAX_TOTAL_SECONDS}s ceiling"
                info["timeout_ceiling"] = True
                return None, info
            is_alive, _ = await check_page_alive(page)
            if not is_alive:
                break
            redirect_url = await _detect_redirect_after_comment_click(page)
            if redirect_url:
                info["error"] = f"Redirected during comment input search: {redirect_url[:100]}"
                info["redirect_detected"] = True
                info["redirect_url"] = redirect_url
                return None, info
            
            # Параллельный поиск всех типов
            all_elements = []
            for selector in input_selectors[:5]:
                if time.monotonic() - _start_ts > _MAX_TOTAL_SECONDS:
                    info["error"] = f"Comment input search exceeded {_MAX_TOTAL_SECONDS}s ceiling"
                    info["timeout_ceiling"] = True
                    return None, info  # Только первые 5 селекторов для скорости
                elements, _ = await safe_query_selector_all(page, selector)
                all_elements.extend(elements)
            
            # Фильтрация по видимости и интерактивности
            for element in all_elements:
                try:
                    is_valid, _ = await quick_validate_element(element, "comment_input")
                    if is_valid:
                        # Дополнительная проверка интерактивности
                        is_editable = await element.evaluate("""
                            el => {
                                return el.tagName === 'TEXTAREA' || 
                                       el.contentEditable === 'true' || 
                                       !el.disabled;
                            }
                        """)
                        if is_editable:
                            info["found"] = True
                            info["strategy"] = "query_selector_all_filtered"
                            log_step("Comment input found (filtered)", {"attempt": attempt + 1})
                            return element, info
                except Exception:
                    continue
        except Exception:
            pass
        
        # Стратегия 3: Прокрутка + повторный поиск
        if attempt < max_attempts - 1:
            try:
                if time.monotonic() - _start_ts > _MAX_TOTAL_SECONDS:
                    info["error"] = f"Comment input search exceeded {_MAX_TOTAL_SECONDS}s ceiling"
                    info["timeout_ceiling"] = True
                    return None, info
                is_alive, _ = await check_page_alive(page)
                if not is_alive:
                    break
                
                await page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
                await asyncio.sleep(1.5 if using_proxy else 0.8)
            except Exception:
                pass
        
        # Стратегия 4: JavaScript поиск (только на последней попытке)
        if attempt == max_attempts - 1:
            try:
                if time.monotonic() - _start_ts > _MAX_TOTAL_SECONDS:
                    info["error"] = f"Comment input search exceeded {_MAX_TOTAL_SECONDS}s ceiling"
                    info["timeout_ceiling"] = True
                    return None, info
                is_alive, _ = await check_page_alive(page)
                if not is_alive:
                    break
                
                element_handle = await page.evaluate_handle("""
                    () => {
                        const selectors = [
                            '[data-e2e="comment-input"]',
                            'textarea[placeholder*="comment" i]',
                            'div[contenteditable="true"]',
                            'textarea',
                        ];
                        
                        for (const selector of selectors) {
                            const elements = document.querySelectorAll(selector);
                            for (const el of elements) {
                                const rect = el.getBoundingClientRect();
                                const isVisible = rect.width > 0 && rect.height > 0 && 
                                                  window.getComputedStyle(el).display !== 'none';
                                if (isVisible) {
                                    const isEditable = el.tagName === 'TEXTAREA' || 
                                                       el.contentEditable === 'true';
                                    if (isEditable && !el.disabled) {
                                        return el;
                                    }
                                }
                            }
                        }
                        return null;
                    }
                """)
                
                if element_handle and await element_handle.as_element():
                    element = await element_handle.as_element()
                    info["found"] = True
                    info["strategy"] = "javascript_search"
                    log_step("Comment input found (JavaScript)", {"attempt": attempt + 1})
                    return element, info
            except Exception:
                pass
    
    # Если не нашли после всех попыток - запускаем диагностику
    if not info["found"]:
        try:
            from executor.page_diagnostics import log_page_diagnostics
            await log_page_diagnostics(page, "comment_input_not_found")
        except Exception as diag_error:
            logger.debug(f"Diagnostics failed: {diag_error}")
    
    info["error"] = "Comment input not found after all attempts"
    log_step("Comment input not found", {"attempts": max_attempts})
    return None, info


async def find_submit_button_fast(
    page: Page,
    comment_input: Optional[Any] = None,
    using_proxy: bool = False,
    max_attempts: int = 3
) -> Tuple[Optional[Any], Dict[str, Any]]:
    """
    Быстрый поиск кнопки отправки комментария.
    
    Args:
        page: Страница Playwright
        comment_input: Найденное поле ввода (для поиска рядом)
        using_proxy: True если используется прокси
        max_attempts: Максимальное количество попыток
        
    Returns:
        tuple: (element, info_dict)
    """
    log_step("Finding submit button", {"attempts": max_attempts, "proxy": using_proxy})
    
    button_selectors = [
        # ИСПРАВЛЕНО (Jan 2026): Приоритет div[data-e2e="comment-post"] - TikTok перешёл на div вместо button
        'div[data-e2e="comment-post"][role="button"]',  # Самый актуальный селектор
        'div[data-e2e="comment-post"]',  # Fallback без role
        selectors.POST_COMMENT_BUTTON,
        selectors.POST_BUTTON,
        'button[data-e2e="post-button"]',
        'button[data-e2e="comment-post-button"]',
        'button:has-text("Post")',
        'button:has-text("Опубликовать")',
        'button:has-text("Отправить")',
        'button[type="submit"]',
    ]
    
    info = {
        "found": False,
        "attempt": 0,
        "strategy": None,
        "error": None,
    }
    
    for attempt in range(max_attempts):
        info["attempt"] = attempt + 1
        
        # Проверяем, что страница жива
        is_alive, error = await check_page_alive(page)
        if not is_alive:
            info["error"] = error
            return None, info
        
        # Стратегия 1: Параллельный поиск всех селекторов
        for selector in button_selectors:
            try:
                element, error = await safe_wait_for_selector(
                    page,
                    selector,
                    timeout=get_timeout(using_proxy, 3000),
                    state="visible"
                )
                if element:
                    is_valid, _ = await quick_validate_element(element, "submit_button")
                    if is_valid:
                        info["found"] = True
                        info["strategy"] = f"wait_for_selector: {selector[:50]}"
                        log_step("Submit button found", {"strategy": info["strategy"]})
                        return element, info
            except Exception:
                continue
        
        # Стратегия 2: Поиск рядом с полем ввода
        if comment_input and attempt < max_attempts - 1:
            try:
                is_alive, _ = await check_page_alive(page)
                if not is_alive:
                    break
                
                # Ищем кнопку в родительском элементе или рядом
                button = await comment_input.evaluate_handle("""
                    el => {
                        // NB: ':has-text()' is a Playwright-only pseudo-class and is
                        // INVALID inside the browser's native querySelectorAll (it throws,
                        // and the surrounding try/except silently swallowed it, so this
                        // strategy never worked). Use valid CSS to gather candidates, then
                        // filter by type/data-e2e/text in JS.
                        const parent = el.closest('form, div, section');
                        if (!parent) return null;
                        const wanted = ['post', 'опубликовать', 'отправить'];
                        const candidates = parent.querySelectorAll(
                            'button[type="submit"], button[data-e2e*="post"], div[data-e2e*="post"][role="button"], button, [role="button"]'
                        );
                        for (const btn of candidates) {
                            if (btn.offsetParent === null) continue;
                            const type = (btn.getAttribute('type') || '').toLowerCase();
                            const e2e = (btn.getAttribute('data-e2e') || '').toLowerCase();
                            const txt = (btn.textContent || '').trim().toLowerCase();
                            if (type === 'submit' || e2e.includes('post') || wanted.some(w => txt.includes(w))) {
                                return btn;
                            }
                        }
                        return null;
                    }
                """)
                
                if button and await button.as_element():
                    element = await button.as_element()
                    is_valid, _ = await quick_validate_element(element, "submit_button")
                    if is_valid:
                        info["found"] = True
                        info["strategy"] = "near_input_search"
                        log_step("Submit button found (near input)")
                        return element, info
            except Exception:
                pass
        
        # Небольшая задержка перед следующей попыткой
        if attempt < max_attempts - 1:
            await asyncio.sleep(0.5)
    
    info["error"] = "Submit button not found after all attempts"
    log_step("Submit button not found", {"attempts": max_attempts})
    return None, info
