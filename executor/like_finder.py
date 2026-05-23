"""
Умный поиск элементов для лайков комментариев.
Оптимизировано для скорости и надежности.
"""
import asyncio
import logging
from typing import Optional, Tuple, Dict, Any
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

logger = logging.getLogger("executor.like_finder")


def get_timeout(using_proxy: bool, base_timeout: int = 5000) -> int:
    """Умные таймауты для лайков."""
    if using_proxy:
        return min(base_timeout * 2, 10000)  # Максимум 10 сек для прокси
    return min(base_timeout, 6000)  # Максимум 6 сек для прямого


async def find_comment_by_id(
    page: Page,
    comment_id: str,
    using_proxy: bool = False,
    max_scrolls: int = 10
) -> Tuple[Optional[Any], Dict[str, Any]]:
    """
    Находит комментарий по ID.
    
    Args:
        page: Страница Playwright
        comment_id: ID комментария для поиска
        using_proxy: True если используется прокси
        max_scrolls: Максимальное количество прокруток
        
    Returns:
        tuple: (element, info_dict)
    """
    log_step("LIKE_FINDING_COMMENT_BY_ID", {
        "comment_id": comment_id[:20] if comment_id else None,
        "proxy": using_proxy
    })
    
    info = {
        "found": False,
        "comment_element": None,
        "comment_id": None,
        "author": None,
        "error": None,
    }
    
    # КРИТИЧНО: Проверяем, что страница жива
    is_alive, error = await check_page_alive(page)
    if not is_alive:
        info["error"] = f"Page is dead: {error}"
        return None, info
    
    # ОБНОВЛЕНО: Используем реальную структуру DOM (Jan 2026)
    # Ищем корневые контейнеры комментариев: div[class*="DivCommentObject"]
    comment_root_selector = selectors.COMMENT_ROOT
    
    # Ищем комментарий по ID с прокруткой
    for scroll in range(max_scrolls):
        try:
            # Проверяем, что страница жива
            is_alive, error = await check_page_alive(page)
            if not is_alive:
                info["error"] = f"Page is dead during scroll {scroll + 1}: {error}"
                return None, info
            
            # Проверяем редирект
            try:
                current_url = page.url.lower()
                if '/messages' in current_url or '/inbox' in current_url:
                    info["error"] = "Redirected to messages page"
                    return None, info
            except Exception:
                pass
            
            # Ищем корневые контейнеры комментариев
            try:
                comment_roots, query_error = await safe_query_selector_all(page, comment_root_selector)
                if query_error:
                    logger.debug(f"[LIKE_FINDER] Error querying comment roots: {query_error}")
                    continue

                for comment_root in comment_roots:
                    try:
                        # Ищем span с текстом комментария внутри корневого контейнера
                        text_span = await comment_root.query_selector(selectors.COMMENT_TEXT_SPAN)
                        if not text_span:
                            continue
                        
                        # Пробуем извлечь ID комментария из корневого контейнера
                        comment_id_attr = await comment_root.evaluate("""
                            el => {
                                // Пробуем разные атрибуты для ID
                                return el.getAttribute('data-cid') || 
                                       el.getAttribute('data-id') || 
                                       el.getAttribute('data-comment-id') ||
                                       el.getAttribute('id') ||
                                       null;
                            }
                        """)
                        
                        if comment_id_attr and str(comment_id_attr) == str(comment_id):
                            # Нашли по ID - проверяем видимость
                            is_visible = await comment_root.is_visible()
                            if is_visible:
                                author_text = None
                                try:
                                    author_elem = await comment_root.query_selector(
                                        'a[href*="/@"], [class*="username"], [class*="author"], [data-e2e*="username"]'
                                    )
                                    if author_elem:
                                        author_raw = await author_elem.evaluate(
                                            "el => (el.innerText || el.textContent || '').trim()"
                                        )
                                        author_text = (author_raw or "").strip() or None
                                except Exception:
                                    author_text = None
                                info["found"] = True
                                info["comment_element"] = comment_root  # Возвращаем корневой контейнер
                                info["comment_id"] = str(comment_id_attr)
                                info["author"] = author_text
                                log_step("LIKE_COMMENT_FOUND_BY_ID", {"comment_id": comment_id[:20]})
                                return comment_root, info
                    except Exception:
                        continue
            except Exception:
                continue
            
            # Прокручиваем вниз для загрузки новых комментариев
            if scroll < max_scrolls - 1:
                try:
                    await page.evaluate("window.scrollBy(0, 500)")
                    await asyncio.sleep(0.5 if using_proxy else 0.3)
                except Exception:
                    pass
                    
        except Exception as e:
            logger.debug(f"[LIKE_FINDER] Error during scroll {scroll + 1}: {e}")
            continue
    
    info["error"] = f"Comment with ID {comment_id[:20]} not found after {max_scrolls} scrolls"
    log_step("LIKE_COMMENT_NOT_FOUND", info)
    return None, info


async def find_like_button_on_comment(
    page: Page,
    comment_element: Any,
    using_proxy: bool = False,
    max_attempts: int = 3
) -> Tuple[Optional[Any], Dict[str, Any]]:
    """
    Находит кнопку лайка на комментарии.
    
    Args:
        page: Страница Playwright
        comment_element: Элемент комментария
        using_proxy: True если используется прокси
        max_attempts: Максимальное количество попыток
        
    Returns:
        tuple: (like_button_element, info_dict)
    """
    log_step("LIKE_FINDING_BUTTON", {"proxy": using_proxy})
    
    info = {
        "found": False,
        "like_button": None,
        "strategy": None,
        "error": None,
    }
    
    # Проверяем валидность элемента комментария
    is_valid, validation_error = await quick_validate_element(comment_element, "comment_element")
    if not is_valid:
        info["error"] = validation_error or "Comment element is invalid"
        return None, info
    
    # ОБНОВЛЕНО: Используем реальные селекторы из DOM (Jan 2026)
    # Кнопка лайка: div[role="button"][class*="DivLikeContainer"]
    like_selectors = [
        selectors.LIKE_BUTTON,  # div[role="button"][class*="DivLikeContainer"]
        selectors.LIKE_BUTTON_FALLBACK,  # div[role="button"][aria-label*="лайк" i]
    ]
    
    for attempt in range(max_attempts):
        try:
            # Проверяем, что страница жива
            is_alive, error = await check_page_alive(page)
            if not is_alive:
                info["error"] = f"Page is dead: {error}"
                return None, info
            
            # Пробуем найти кнопку лайка внутри корневого контейнера комментария
            for selector in like_selectors:
                try:
                    # Ищем внутри элемента комментария (корневого контейнера)
                    like_button = await comment_element.query_selector(selector)
                    
                    if like_button:
                        is_visible = await like_button.is_visible()
                        if is_visible:
                            info["found"] = True
                            info["like_button"] = like_button
                            info["strategy"] = f"query_selector({selector})"
                            log_step("LIKE_BUTTON_FOUND", {"strategy": info["strategy"]})
                            return like_button, info
                except Exception:
                    continue
            
            # Если не нашли через селекторы - пробуем через JavaScript
            try:
                like_button = await comment_element.evaluate_handle("""
                    el => {
                        // ОБНОВЛЕНО: Реальные селекторы из DOM (Jan 2026)
                        const selectors = [
                            'div[role="button"][class*="DivLikeContainer"]',
                            'div[role="button"][aria-label*="лайк" i]',
                            'div[role="button"][aria-label*="like" i]'
                        ];
                        
                        for (const sel of selectors) {
                            const btn = el.querySelector(sel);
                            if (btn && btn.offsetParent !== null) {  // Проверка видимости
                                return btn;
                            }
                        }
                        
                        return null;
                    }
                """)
                
                if like_button:
                    # Проверяем, что элемент видим
                    try:
                        is_visible = await like_button.is_visible()
                        if is_visible:
                            info["found"] = True
                            info["like_button"] = like_button
                            info["strategy"] = "JavaScript evaluation"
                            log_step("LIKE_BUTTON_FOUND", {"strategy": info["strategy"]})
                            return like_button, info
                    except Exception:
                        # Если не можем проверить видимость - все равно возвращаем элемент
                        info["found"] = True
                        info["like_button"] = like_button
                        info["strategy"] = "JavaScript evaluation (visibility check failed)"
                        log_step("LIKE_BUTTON_FOUND", {"strategy": info["strategy"]})
                        return like_button, info
            except Exception as e:
                logger.debug(f"[LIKE_FINDER] JavaScript search failed: {e}")
                continue
            
            # Небольшая задержка перед следующей попыткой
            if attempt < max_attempts - 1:
                await asyncio.sleep(0.5 if using_proxy else 0.3)
                
        except Exception as e:
            logger.debug(f"[LIKE_FINDER] Error in attempt {attempt + 1}: {e}")
            continue
    
    info["error"] = f"Like button not found after {max_attempts} attempts"
    log_step("LIKE_BUTTON_NOT_FOUND", info)
    return None, info
