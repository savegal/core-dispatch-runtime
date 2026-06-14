"""
Умный поиск элементов для ответов на комментарии.
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
from executor.comment_outcome import normalize_text, is_comment_match

logger = logging.getLogger("executor.reply_finder")


def get_timeout(using_proxy: bool, base_timeout: int = 5000) -> int:
    """Умные таймауты для ответов."""
    if using_proxy:
        return min(base_timeout * 2, 10000)  # Максимум 10 сек для прокси
    return min(base_timeout, 6000)  # Максимум 6 сек для прямого


def _normalize_author_name(value: Optional[str]) -> str:
    return normalize_text(value or "").lstrip("@").strip()


async def find_comment_by_text_and_author(
    page: Page,
    comment_text: str,
    expected_author: Optional[str] = None,
    expected_comment_id: Optional[str] = None,
    using_proxy: bool = False,
    max_scrolls: int = 10,
    min_match_ratio: float = 0.7,
    top_level_only: bool = True,
) -> Tuple[Optional[Any], Dict[str, Any]]:
    """
    Находит комментарий по тексту, автору или ID.
    
    Args:
        page: Страница Playwright
        comment_text: Текст комментария для поиска
        expected_author: Опциональное имя автора (для более точного поиска)
        expected_comment_id: Опциональный ID комментария
        using_proxy: True если используется прокси
        max_scrolls: Максимальное количество прокруток
        
    Returns:
        tuple: (element, info_dict)
    """
    log_step("Finding parent comment", {
        "text_len": len(comment_text),
        "author": expected_author,
        "comment_id": expected_comment_id[:20] if expected_comment_id else None,
        "min_match_ratio": min_match_ratio,
        "proxy": using_proxy,
        "top_level_only": top_level_only,
    })
    
    info = {
        "found": False,
        "comment_element": None,
        "comment_id": None,
        "author": None,
        "position": None,
        "error": None,
    }
    
    # КРИТИЧНО: Проверяем, что страница жива
    is_alive, error = await check_page_alive(page)
    if not is_alive:
        info["error"] = f"Page is dead: {error}"
        return None, info
    
    # Нормализуем текст для поиска
    target_text = normalize_text(comment_text)
    target_author = normalize_text(expected_author) if expected_author else None
    strict_id_required = bool(expected_comment_id)
    
    # ОБНОВЛЕНО: Используем реальную структуру DOM (Jan 2026)
    # Корневой контейнер комментария: div[class*="DivCommentObject"]
    comment_root_selector = selectors.COMMENT_ROOT

    # Жёсткий scope: ищем parent ТОЛЬКО внутри видимого comment-list. Без этого
    # глобальные query_selector_all по странице матчат сайдбар "similar videos"
    # (карточки с cid="one-column-item-N"), профильные ссылки, фид — это даёт
    # ложные "родительские комменты" и невозможный для клика Reply button.
    list_selectors = [
        '[data-e2e="comment-list"]',
        '[data-e2e="comment-list-container"]',
        '[class*="CommentList"]',
        '[class*="DivCommentListContainer"]',
        '[class*="CommentContainer"]',
    ]

    comment_list = None
    matched_list_selector = None
    for sel in list_selectors:
        try:
            candidate = await page.query_selector(sel)
            if candidate:
                try:
                    if await candidate.is_visible():
                        comment_list = candidate
                        matched_list_selector = sel
                        break
                except Exception:
                    continue
        except Exception:
            continue

    if comment_list is None:
        info["error"] = "comment_list_not_open"
        logger.warning(
            "[FIND_COMMENT] comment-list container not visible on %s — refusing "
            "page-wide search to avoid matching sidebar feed cards",
            page.url[:120],
        )
        log_step("Parent comment not found", {"reason": "comment_list_not_open"})
        return None, info

    # Если указан comment_id - сначала пробуем найти по ID
    # If expected_comment_id is set, we will check it during each scroll pass.
    async def _looks_like_top_level_parent(handle: Any) -> bool:
        if not top_level_only:
            return True
        try:
            return bool(
                await handle.evaluate(
                    """
                    (el) => {
                        const visible = (node) => {
                            if (!node || !node.getBoundingClientRect) return false;
                            const r = node.getBoundingClientRect();
                            const s = getComputedStyle(node);
                            return r.width > 0 && r.height > 0 && s.display !== 'none' && s.visibility !== 'hidden';
                        };
                        const labelOf = (node) => [
                            node && node.getAttribute && (node.getAttribute('data-e2e') || ''),
                            node && node.getAttribute && (node.getAttribute('aria-label') || ''),
                            node && (node.id || ''),
                            node && String(node.className || ''),
                        ].join(' ').toLowerCase();
                        const isReplyLevel = (node) => /comment-level-[23]/.test(labelOf(node));
                        if (isReplyLevel(el)) return false;
                        const strongSelf = el.querySelector && el.querySelector('[data-e2e="comment-level-1"], [data-e2e*="comment-text"]');
                        const firstLevel = el.querySelector && el.querySelector('[data-e2e*="comment-level"]');
                        if (firstLevel && isReplyLevel(firstLevel) && !strongSelf) return false;
                        for (let cur = el.parentElement, depth = 0; cur && cur !== document.body && depth < 8; cur = cur.parentElement, depth++) {
                            const label = labelOf(cur);
                            if (isReplyLevel(cur)) return false;
                            if (label.includes('reply-container') || label.includes('replyitem') || label.includes('reply-item')) return false;
                            if (/(reply|Reply)/.test(String(cur.className || '')) && !label.includes('comment-reply')) return false;
                        }
                        if (strongSelf && visible(strongSelf)) return true;
                        try {
                            const r = el.getBoundingClientRect();
                            const textNode = el.querySelector('[data-e2e*="comment-level"], [data-e2e*="comment-text"], p, span') || el;
                            const tr = textNode.getBoundingClientRect();
                            if (tr.left - r.left > 96) return false;
                        } catch (_) {}
                        return true;
                    }
                    """
                )
            )
        except Exception:
            return True

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

            # Ищем корневые контейнеры комментариев — ТОЛЬКО внутри
            # обнаруженного comment-list, не глобально.
            try:
                try:
                    comment_roots = await comment_list.query_selector_all(comment_root_selector)
                    query_error = None
                except Exception as exc:
                    comment_roots = []
                    query_error = str(exc)[:120]
                if query_error:
                    logger.debug(f"[FIND_COMMENT] Error querying comment roots: {query_error}")
                    continue

                for idx, comment_root in enumerate(comment_roots):
                    try:
                        comment_id = await comment_root.get_attribute("data-cid") or \
                                    await comment_root.get_attribute("data-id") or \
                                    await comment_root.get_attribute("data-comment-id") or \
                                    await comment_root.get_attribute("id")

                        if expected_comment_id and comment_id and str(comment_id) == str(expected_comment_id):
                            if not await _looks_like_top_level_parent(comment_root):
                                continue
                            is_visible = await comment_root.is_visible()
                            if not is_visible:
                                try:
                                    await comment_root.scroll_into_view_if_needed()
                                    await asyncio.sleep(0.5)
                                    is_visible = await comment_root.is_visible()
                                except Exception:
                                    pass

                            if is_visible:
                                author_text = None
                                try:
                                    author_elem = await comment_root.query_selector('a[href*="/@"], [class*="username"], [class*="author"], [data-e2e*="username"]')
                                    if author_elem:
                                        author_raw = await author_elem.evaluate("el => (el.innerText || el.textContent || '').trim()")
                                        author_text = (author_raw or "").strip() or None
                                except Exception:
                                    author_text = None
                                info["found"] = True
                                info["comment_element"] = comment_root
                                info["comment_id"] = str(comment_id)
                                info["author"] = author_text
                                info["position"] = idx
                                log_step("Parent comment found by ID", {"comment_id": str(comment_id)[:20], "scroll": scroll})
                                return comment_root, info
                        # Ищем span с текстом комментария внутри корневого контейнера
                        if strict_id_required:
                            continue

                        text_span = await comment_root.query_selector(selectors.COMMENT_TEXT_SPAN)
                        if not text_span:
                            continue
                        if not await _looks_like_top_level_parent(text_span):
                            continue
                        
                        # Извлекаем текст из span[data-e2e="comment-level-1"]
                        comment_text_elem = await text_span.evaluate("""
                            el => {
                                return (el.innerText || el.textContent || '').trim();
                            }
                        """)
                        
                        if not comment_text_elem or len(comment_text_elem) < 3:
                            continue
                        
                        comment_text_normalized = normalize_text(comment_text_elem)
                        
                        # Проверяем совпадение текста.
                        # min_ratio выровнен с find_comment_in_list (0.7): TikTok
                        # часто обрезает длинные комменты или добавляет невидимые
                        # символы, и более строгий порог даёт ложно-отрицательные.
                        if not is_comment_match(comment_text, comment_text_elem, min_ratio=min_match_ratio):
                            continue

                        # Извлекаем handle ПРИОРИТЕТНО из href="/@<handle>" — это
                        # настоящий TikTok username. innerText даёт display name
                        # ("Ozymandias", "Ẩn Thiên Nhân"), который не сходится с
                        # ожидаемым handle и приводит к ложно-отрицательному
                        # совпадению. Та же логика используется в
                        # comment_verification.verify_comment_visible.
                        author_text = None
                        author_normalized = ""
                        try:
                            author_text = await comment_root.evaluate(r"""
                                el => {
                                    const selectors = [
                                        'a[data-e2e="comment-username-1"]',
                                        '[data-e2e="comment-username-1"]',
                                        'a[href*="/@"][data-e2e]',
                                        'a[href*="/@"]',
                                        '[class*="UserName"]',
                                        '[class*="username"]',
                                        '[class*="user-name"]',
                                        '[class*="author"]',
                                    ];
                                    for (const sel of selectors) {
                                        const found = el.querySelector(sel);
                                        if (found) {
                                            const href = found.getAttribute('href') || '';
                                            const match = href.match(/\/@([^/?]+)/);
                                            if (match) return match[1];
                                            const text = (found.innerText || found.textContent || '').trim();
                                            if (text) return text.replace(/^@/, '');
                                        }
                                    }
                                    return null;
                                }
                            """)
                            author_text = (author_text or "").strip() or None
                            author_normalized = _normalize_author_name(author_text)
                        except Exception:
                            author_text = None
                            author_normalized = ""

                        # Если ожидаем конкретного автора и сумели прочитать
                        # имя — оно должно совпасть. Если прочитать не удалось
                        # (TikTok скрыл/перерендерил) — принимаем по тексту:
                        # текст уже прошёл совпадение ≥ 0.7. Это поведение
                        # совпадает с verify_with_retries (accepting by text).
                        if target_author and author_normalized:
                            if author_normalized != _normalize_author_name(expected_author):
                                continue

                        if author_text:
                            info["author"] = author_text
                        
                        # Комментарий найден!
                        is_visible = await comment_root.is_visible()
                        if not is_visible:
                            # Скроллим к комментарию
                            try:
                                await comment_root.scroll_into_view_if_needed()
                                await asyncio.sleep(0.5)
                                is_visible = await comment_root.is_visible()
                            except Exception:
                                pass
                        
                        if is_visible:
                            # ОБНОВЛЕНО: Извлекаем cid из корневого контейнера комментария
                            try:
                                # Приоритет 1: data-cid (если TikTok сохраняет cid в DOM)
                                comment_id = await comment_root.get_attribute("data-cid") or \
                                            await comment_root.get_attribute("data-id") or \
                                            await comment_root.get_attribute("data-comment-id") or \
                                            await comment_root.get_attribute("id")
                                
                                if comment_id:
                                    info["comment_id"] = str(comment_id)
                                    log_step("Comment cid extracted", {"cid": comment_id[:20]})
                            except Exception as e:
                                logger.debug(f"[FIND_COMMENT] Error extracting cid: {e}")
                                pass
                            
                            info["found"] = True
                            info["comment_element"] = comment_root  # Возвращаем корневой контейнер
                            info["position"] = idx
                            log_step("Parent comment found", {
                                "position": idx,
                                "comment_id": info["comment_id"][:20] if info["comment_id"] else None,
                                "scroll": scroll
                            })
                            return comment_root, info
                    except Exception as e:
                        logger.debug(f"[FIND_COMMENT] Error processing comment {idx}: {e}")
                        continue
            except Exception as e:
                logger.debug(f"[FIND_COMMENT] Error querying comment roots: {e}")
                continue
            
            # Прокручиваем для загрузки новых комментариев — ВНУТРИ контейнера.
            # window.scrollBy здесь приводит к подгрузке "similar videos", а не
            # самих комментов, поэтому используем только scrollTop контейнера.
            if scroll < max_scrolls - 1:
                try:
                    await comment_list.evaluate("""
                        (el) => {
                            el.scrollTop += el.clientHeight * 0.9;
                        }
                    """)
                    await asyncio.sleep(0.6 if using_proxy else 0.4)
                except Exception:
                    pass
        
        except Exception as e:
            logger.debug(f"[FIND_COMMENT] Error during scroll {scroll + 1}: {e}")
            continue
    
    if strict_id_required:
        info["error"] = f"Comment with expected ID {str(expected_comment_id)[:20]} not found after {max_scrolls} scrolls"
    else:
        info["error"] = f"Comment not found after {max_scrolls} scrolls"
    log_step("Parent comment not found", {"scrolls": max_scrolls})
    return None, info


async def find_reply_button_on_comment(
    page: Page,
    comment_element: Any,
    using_proxy: bool = False,
    max_attempts: int = 3
) -> Tuple[Optional[Any], Dict[str, Any]]:
    """
    Находит кнопку Reply на конкретном комментарии.
    
    Args:
        page: Страница Playwright
        comment_element: Элемент комментария
        using_proxy: True если используется прокси
        max_attempts: Максимальное количество попыток
        
    Returns:
        tuple: (element, info_dict)
    """
    log_step("Finding reply button on comment", {"attempts": max_attempts, "proxy": using_proxy})
    
    info = {
        "found": False,
        "attempt": 0,
        "strategy": None,
        "error": None,
    }
    
    reply_button_selectors = [
        selectors.REPLY_BUTTON,  # Реальный селектор: p[data-e2e="comment-reply-1"]
        selectors.REPLY_BUTTON_FALLBACK,  # Fallback
        'p:has-text("Ответить")',  # По тексту
        'p:has-text("Reply")',  # По тексту (английский)
        '[data-e2e*="reply"]',  # Общий fallback
    ]
    
    for attempt in range(max_attempts):
        info["attempt"] = attempt + 1
        
        # Проверяем, что страница жива
        is_alive, error = await check_page_alive(page)
        if not is_alive:
            info["error"] = error
            return None, info
        
        # Стратегия 1: Поиск кнопки внутри элемента комментария
        try:
            # Ищем кнопку Reply внутри комментария
            for selector in reply_button_selectors:
                try:
                    reply_button = await comment_element.query_selector(selector)
                    if reply_button:
                        is_visible = await reply_button.is_visible()
                        if is_visible:
                            info["found"] = True
                            info["strategy"] = f"inside_comment: {selector[:50]}"
                            log_step("Reply button found", {"strategy": info["strategy"]})
                            return reply_button, info
                except Exception:
                    continue
            
            # Стратегия 2: JavaScript поиск внутри комментария
            try:
                button_handle = await comment_element.evaluate_handle("""
                    el => {
                        // Ищем кнопку Reply разными способами
                        const buttons = el.querySelectorAll('button, [role="button"], a');
                        for (const btn of buttons) {
                            const text = (btn.innerText || btn.textContent || '').toLowerCase();
                            const ariaLabel = (btn.getAttribute('aria-label') || '').toLowerCase();
                            const dataE2e = (btn.getAttribute('data-e2e') || '').toLowerCase();
                            
                            if (text.includes('reply') || text.includes('ответить') ||
                                ariaLabel.includes('reply') || ariaLabel.includes('ответить') ||
                                dataE2e.includes('reply')) {
                                if (btn.offsetParent !== null) {
                                    return btn;
                                }
                            }
                        }
                        return null;
                    }
                """)
                
                if button_handle and await button_handle.as_element():
                    button = await button_handle.as_element()
                    is_visible = await button.is_visible()
                    if is_visible:
                        info["found"] = True
                        info["strategy"] = "javascript_inside_comment"
                        log_step("Reply button found (JavaScript)")
                        return button, info
            except Exception:
                pass
            
            # Стратегия 3: Поиск кнопки рядом с комментарием (может быть вне элемента)
            try:
                # Ищем кнопку в родительском контейнере
                parent = await comment_element.evaluate_handle("el => el.parentElement")
                if parent and await parent.as_element():
                    parent_elem = await parent.as_element()
                    for selector in reply_button_selectors[:4]:  # Только первые 4 для скорости
                        try:
                            buttons = await parent_elem.query_selector_all(selector)
                            for btn in buttons:
                                is_visible = await btn.is_visible()
                                if is_visible:
                                    # Проверяем, что кнопка близко к комментарию
                                    try:
                                        comment_rect = await comment_element.bounding_box()
                                        button_rect = await btn.bounding_box()
                                        
                                        if comment_rect and button_rect:
                                            # Кнопка должна быть в пределах 500px от комментария
                                            distance = abs(button_rect["y"] - comment_rect["y"])
                                            if distance < 500:
                                                info["found"] = True
                                                info["strategy"] = f"near_comment: {selector[:50]}"
                                                log_step("Reply button found near comment")
                                                return btn, info
                                    except Exception:
                                        # Если не можем проверить расстояние - все равно пробуем
                                        info["found"] = True
                                        info["strategy"] = f"near_comment_fallback: {selector[:50]}"
                                        return btn, info
                        except Exception:
                            continue
            except Exception:
                pass
        
        except Exception as e:
            logger.debug(f"[REPLY_BUTTON] Error in attempt {attempt + 1}: {e}")
            continue
        
        # Задержка перед следующей попыткой
        if attempt < max_attempts - 1:
            await asyncio.sleep(0.5 if using_proxy else 0.3)
    
    info["error"] = "Reply button not found after all attempts"
    log_step("Reply button not found", {"attempts": max_attempts})
    return None, info


async def find_reply_input_fast(
    page: Page,
    using_proxy: bool = False,
    max_attempts: int = 4
) -> Tuple[Optional[Any], Dict[str, Any]]:
    """
    Быстрый поиск поля ввода ответа после клика на Reply.
    
    Args:
        page: Страница Playwright
        using_proxy: True если используется прокси
        max_attempts: Максимальное количество попыток
        
    Returns:
        tuple: (element, info_dict)
    """
    log_step("Finding reply input", {"attempts": max_attempts, "proxy": using_proxy})
    
    info = {
        "found": False,
        "attempt": 0,
        "strategy": None,
        "error": None,
    }
    
    reply_input_selectors = [
        selectors.REPLY_INPUT,  # Реальный селектор: div[data-e2e="comment-input"]
        selectors.COMMENT_INPUT,  # Может быть то же поле, что и для основного комментария
        'div[data-e2e="comment-input"][contenteditable="true"]',  # С contenteditable
        'div[contenteditable="true"][data-e2e="comment-input"]',  # Обратный порядок
        '[data-e2e="comment-input"]',  # Без указания tag
    ]
    
    # КРИТИЧНО: Проверяем редирект перед началом поиска
    try:
        is_alive, _ = await check_page_alive(page)
        if is_alive:
            current_url = page.url.lower()
            if '/messages' in current_url or '/inbox' in current_url:
                return None, {
                    "found": False,
                    "attempt": 0,
                    "error": f"Redirected to messages page: {current_url[:100]}"
                }
    except Exception:
        pass
    
    for attempt in range(max_attempts):
        info["attempt"] = attempt + 1
        
        # Проверяем, что страница жива
        is_alive, error = await check_page_alive(page)
        if not is_alive:
            info["error"] = error
            return None, info
        
        # КРИТИЧНО: Проверяем редирект во время поиска
        try:
            current_url = page.url.lower()
            if '/messages' in current_url or '/inbox' in current_url:
                info["error"] = f"Redirected to messages page: {current_url[:100]}"
                return None, info
        except Exception:
            pass
        
        # Стратегия 1: Параллельный поиск всех селекторов
        for selector in reply_input_selectors:
            try:
                element, error = await safe_wait_for_selector(
                    page,
                    selector,
                    timeout=get_timeout(using_proxy, 3000),
                    state="visible"
                )
                if element:
                    is_valid, _ = await quick_validate_element(element, "comment_input")
                    if is_valid:
                        info["found"] = True
                        info["strategy"] = f"wait_for_selector: {selector[:50]}"
                        log_step("Reply input found", {"strategy": info["strategy"]})
                        return element, info
            except Exception:
                continue
        
        # Стратегия 2: query_selector_all + фильтрация
        try:
            all_elements = []
            for selector in reply_input_selectors[:6]:  # Только первые 6 для скорости
                elements, _ = await safe_query_selector_all(page, selector)
                all_elements.extend(elements)
            
            # Фильтрация по видимости и интерактивности
            for element in all_elements:
                try:
                    is_valid, _ = await quick_validate_element(element, "comment_input")
                    if is_valid:
                        is_visible = await element.is_visible()
                        if is_visible:
                            info["found"] = True
                            info["strategy"] = "query_selector_all"
                            log_step("Reply input found (query_all)")
                            return element, info
                except Exception:
                    continue
        except Exception:
            pass
        
        # Стратегия 3: JavaScript поиск
        if attempt == max_attempts - 1:
            try:
                element_handle = await page.evaluate_handle("""
                    () => {
                        const selectors = [
                            '[data-e2e="reply-input"]',
                            'textarea[placeholder*="reply" i]',
                            'div[contenteditable="true"]',
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
                                        // Проверяем, что это поле ответа (не основное)
                                        const placeholder = (el.getAttribute('placeholder') || '').toLowerCase();
                                        if (placeholder.includes('reply') || placeholder.includes('ответ')) {
                                            return el;
                                        }
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
                    log_step("Reply input found (JavaScript)")
                    return element, info
            except Exception:
                pass
        
        # Задержка перед следующей попыткой
        if attempt < max_attempts - 1:
            await asyncio.sleep(0.5 if using_proxy else 0.3)
    
    info["error"] = "Reply input not found after all attempts"
    log_step("Reply input not found", {"attempts": max_attempts})
    return None, info


async def find_reply_submit_button_fast(
    page: Page,
    reply_input: Optional[Any] = None,
    using_proxy: bool = False,
    max_attempts: int = 3
) -> Tuple[Optional[Any], Dict[str, Any]]:
    """
    Быстрый поиск кнопки отправки ответа.
    
    Args:
        page: Страница Playwright
        reply_input: Найденное поле ввода ответа (для поиска рядом)
        using_proxy: True если используется прокси
        max_attempts: Максимальное количество попыток
        
    Returns:
        tuple: (element, info_dict)
    """
    log_step("Finding reply submit button", {"attempts": max_attempts, "proxy": using_proxy})
    
    info = {
        "found": False,
        "attempt": 0,
        "strategy": None,
        "error": None,
    }
    
    submit_button_selectors = [
        selectors.REPLY_POST_BUTTON,  # Реальный селектор: div[data-e2e="comment-post"][role="button"]
        selectors.REPLY_POST_BUTTON_FALLBACK,  # Fallback: div[data-e2e="comment-post"]
        'div[data-e2e="comment-post"]:has-text("Опубликовать")',  # С текстом
        'div[data-e2e="comment-post"]:has-text("Post")',  # С текстом (английский)
        'div[role="button"][data-e2e="comment-post"]',  # Обратный порядок
        selectors.POST_COMMENT_BUTTON,  # Fallback на общий селектор
        'button:has-text("Опубликовать")',  # Общий fallback
    ]
    
    for attempt in range(max_attempts):
        info["attempt"] = attempt + 1
        
        # Проверяем, что страница жива
        is_alive, error = await check_page_alive(page)
        if not is_alive:
            info["error"] = error
            return None, info
        
        # Стратегия 1: Параллельный поиск всех селекторов
        for selector in submit_button_selectors:
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
                        log_step("Reply submit button found", {"strategy": info["strategy"]})
                        return element, info
            except Exception:
                continue
        
        # Стратегия 2: Поиск рядом с полем ввода ответа
        if reply_input and attempt < max_attempts - 1:
            try:
                is_alive, _ = await check_page_alive(page)
                if not is_alive:
                    break
                
                # Ищем кнопку в родительском элементе или рядом
                button = await reply_input.evaluate_handle("""
                    el => {
                        // NB: ':has-text()' is a Playwright-only pseudo-class and is
                        // INVALID inside the browser's native querySelectorAll (it throws,
                        // and the surrounding try/except silently swallowed it, so this
                        // strategy never worked). Use valid CSS to gather candidates, then
                        // filter by type/data-e2e/text in JS, scoped to the reply container.
                        const parent = el.closest('form, div, section, [class*="ReplyContainer"]');
                        if (!parent) return null;
                        const wanted = ['post', 'опубликовать', 'отправить', 'reply', 'ответить'];
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
                        log_step("Reply submit button found (near input)")
                        return element, info
            except Exception:
                pass
        
        # Задержка перед следующей попыткой
        if attempt < max_attempts - 1:
            await asyncio.sleep(0.5 if using_proxy else 0.3)
    
    info["error"] = "Reply submit button not found after all attempts"
    log_step("Reply submit button not found", {"attempts": max_attempts})
    return None, info
