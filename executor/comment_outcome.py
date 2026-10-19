"""
ИСПРАВЛЕНО: Модуль для надёжной диагностики результата комментирования.

Содержит утилиты для:
1. Сетевой проверки отправки комментария (ловим response после клика)
2. Обнаружения тостов/ошибок UI
3. Классификации результата комментирования
4. Надёжного ввода текста в contenteditable

Рекомендации ChatGPT для устранения ложных успехов и улучшения диагностики теневого бана.
"""
import asyncio
import json
import logging
import re
import uuid
from typing import Optional, Dict, Any, Callable, Tuple
from patchright.async_api import Page
from executor.captcha_detector import detect_captcha
from executor.browser_errors import is_browser_target_closed
from services.comment_search import contains_search_phrase, normalize_search_phrase

logger = logging.getLogger("executor.comment_outcome")


def _new_parent_anchor_token() -> str:
    return f"pa-{uuid.uuid4().hex}"


async def _visible_captcha_error(page: Page) -> Optional[str]:
    try:
        has_captcha, captcha_type = await detect_captcha(page, wait_ms=0)
    except Exception as exc:
        logger.debug("[FIND_COMMENT] captcha probe failed: %s", exc)
        return None
    if not has_captcha:
        return None
    return f"captcha_challenge:{captcha_type or 'unknown'}"


def normalize_text(text: str) -> str:
    """
    ИСПРАВЛЕНО: Упрощенная нормализация текста (рекомендации ChatGPT).
    
    Простая и быстрая нормализация без лишней сложности.
    Удаляет zero-width символы, коллапсирует пробелы, приводит к нижнему регистру.
    
    Args:
        text: Исходный текст
        
    Returns:
        str: Нормализованный текст
    """
    if not text:
        return ""
    
    # Простая нормализация: lower + strip + коллапс пробелов
    s = (text or "").lower().strip()
    # Удаляем невидимые zero-width space/non-joiner, BOM и вариационные
    # селекторы эмодзи, но сохраняем U+200D ZERO WIDTH JOINER. ZWJ является
    # смысловой частью emoji-последовательностей: 👨‍👩‍👧 и 👨👩👧 — разные
    # комментарии, и их нельзя считать одним и тем же при подтверждении.
    # (VS1-VS16, U+FE00-U+FE0F). В DOM TikTok у эмодзи часто присутствует
    # лишний U+FE0F, которого нет в сохранённом тексте (или наоборот); без
    # этого строгое (ratio=1.0) сопоставление родителя отклоняет визуально
    # идентичный комментарий, из-за чего сканер прокручивает мимо комментария,
    # который явно виден на экране.
    s = re.sub(r'[\u200b\u200c\ufeff\ufe00-\ufe0f]', '', s)
    # Skin-tone modifiers are also meaningful content. Stripping them would
    # make e.g. 👍🏻 and 👍🏿 exact matches and could confirm the wrong comment.
    # Коллапсируем множественные пробелы/переносы в один пробел
    s = re.sub(r'\s+', ' ', s)
    return s.strip()


def normalize_author(author: Any) -> str:
    """Normalize a TikTok username/author hint for matching."""
    value = normalize_text(str(author or "")).lstrip("@")
    value = re.sub(r"^https?://(?:www\.)?tiktok\.com/@", "", value)
    value = value.split("?", 1)[0].split("/", 1)[0]
    return value.strip().lstrip("@")


def is_comment_match(
    expected: str, actual: str, min_ratio: float = 0.7, *, phrase_match: bool = False,
) -> bool:
    """
    Сравнение текста комментария с допуском по длине (рекомендации ChatGPT).
    
    Полезно, если TikTok обрезает текст или добавляет "...".
    
    Args:
        expected: Ожидаемый текст
        actual: Фактический текст из DOM
        min_ratio: Минимальная доля совпадения (0.7 = 70%)
        
    Returns:
        bool: True если тексты совпадают
    """
    if phrase_match:
        return contains_search_phrase(expected, actual)
    exp = normalize_text(expected)
    act = normalize_text(actual)
    
    if not exp or not act:
        return False
    
    if exp == act:
        return True
    
    # Если один текст содержит другой (TikTok может обрезать)
    if exp in act or act in exp:
        shorter = min(len(exp), len(act))
        longer = max(len(exp), len(act))
        if longer > 0:
            return shorter / longer >= min_ratio
    
    return False


def _comment_row_signature(
    raw_text: Any,
    *,
    cid: Any = None,
    author: Any = None,
) -> Optional[str]:
    """Stable-ish identity for scroll progress diagnostics.

    Matching still uses the stricter per-target logic below. This signature is
    only for deciding whether the visible TikTok list is hydrating new rows
    while we scroll, so it deliberately ignores row position and scrollTop.
    """
    cid_value = str(cid or "").strip()
    if cid_value:
        return f"cid:{cid_value}"
    text_key = normalize_text(str(raw_text or ""))
    if not text_key:
        return None
    author_key = normalize_text(str(author or ""))
    return f"text:{text_key}|author:{author_key}"


async def _nudge_comment_list_scroll(comment_list: Any, *, delta: int = 800) -> Dict[str, Any]:
    """Best-effort wheel nudge for TikTok virtualized comment lists."""
    try:
        result = await comment_list.evaluate(
            """
            (el, delta) => {
                const state = (node) => ({
                    top: Number(node && node.scrollTop) || 0,
                    height: Number(node && node.scrollHeight) || 0,
                    client: Number(node && node.clientHeight) || 0,
                });
                const before = state(el);
                const amount = Math.max(
                    Number(delta) || 0,
                    Math.floor((el && el.clientHeight ? el.clientHeight : 0) * 0.9),
                    480
                );
                const rect = el && el.getBoundingClientRect
                    ? el.getBoundingClientRect()
                    : { left: 0, top: 0, width: 1, height: 1 };
                const eventInit = {
                    bubbles: true,
                    cancelable: true,
                    deltaY: amount,
                    deltaMode: 0,
                    clientX: Math.max(1, rect.left + Math.min(rect.width - 1, rect.width / 2)),
                    clientY: Math.max(1, rect.top + Math.min(rect.height - 1, rect.height / 2)),
                };
                const targets = [];
                for (let cur = el; cur && cur !== document.body && cur !== document.documentElement; cur = cur.parentElement) {
                    targets.push(cur);
                }
                targets.push(document.scrollingElement || document.documentElement);
                let dispatched = 0;
                for (const target of targets) {
                    try {
                        target.dispatchEvent(new WheelEvent('wheel', eventInit));
                        dispatched++;
                    } catch (_) {}
                    try {
                        if (typeof target.scrollTop === 'number') target.scrollTop += amount;
                    } catch (_) {}
                }
                return {
                    nudged: true,
                    dispatched,
                    before,
                    after: state(el),
                    docTop: Number((document.scrollingElement || document.documentElement).scrollTop) || 0,
                };
            }
            """,
            delta,
        )
        return result if isinstance(result, dict) else {"nudged": True}
    except Exception as exc:
        return {"nudged": False, "error": str(exc)[:120]}


def _normalize_identifier(value: Any) -> Optional[str]:
    """Converts identifier value to non-empty string."""
    if value is None:
        return None
    value_str = str(value).strip()
    return value_str or None


def _extract_publish_response_fields(response_json: Any) -> Dict[str, Any]:
    """
    Extracts publish metadata from known TikTok response shapes.

    Supported shapes include:
    - {"status_code", "status_msg", "comment": {...}}
    - {"status_code", "status_msg", "data": {"comment": {...}}}
    - {"status_code", "status_msg", "data": {...}}
    """
    fields: Dict[str, Any] = {
        "comment_id": None,
        "parent_comment_id": None,
        "status_code": None,
        "status_msg": None,
    }
    if not isinstance(response_json, dict):
        return fields

    containers: list[Dict[str, Any]] = [response_json]

    data = response_json.get("data")
    if isinstance(data, dict):
        containers.append(data)
        nested_comment = data.get("comment")
        if isinstance(nested_comment, dict):
            containers.append(nested_comment)

    comment_data = response_json.get("comment")
    if isinstance(comment_data, dict):
        containers.append(comment_data)

    for container in containers:
        if fields["status_code"] is None and container.get("status_code") is not None:
            fields["status_code"] = container.get("status_code")
        if fields["status_msg"] is None and container.get("status_msg") is not None:
            fields["status_msg"] = str(container.get("status_msg"))

        if fields["comment_id"] is None:
            for key in ("cid", "comment_id", "id"):
                comment_id = _normalize_identifier(container.get(key))
                if comment_id:
                    fields["comment_id"] = comment_id
                    break

        if fields["parent_comment_id"] is None:
            for key in ("reply_id", "parent_comment_id", "parent_id"):
                parent_id = _normalize_identifier(container.get(key))
                if parent_id and parent_id != "0":
                    fields["parent_comment_id"] = parent_id
                    break

    return fields


async def set_comment_text_reliable(comment_input, text: str) -> str:
    """
    ИСПРАВЛЕНО: Надёжный ввод текста в contenteditable без f-string (рекомендации ChatGPT).
    
    Использует keyboard.insert_text() вместо JS-подмены, что более стабильно
    и не ломается при спецсимволах, кавычках, переносах строк.
    
    Args:
        comment_input: Элемент поля ввода (Locator или ElementHandle)
        text: Текст для ввода
        
    Returns:
        str: Введённый текст (для проверки)
    """
    page = comment_input.page if hasattr(comment_input, 'page') else None
    if not page:
        raise ValueError("Cannot get page from comment_input")
    
    # Фокус
    await comment_input.scroll_into_view_if_needed()
    await comment_input.click()
    await asyncio.sleep(0.2)  # Небольшая задержка для фокуса
    
    # Очистка (универсально для contenteditable и обычного input)
    await comment_input.evaluate("""
        (el) => {
            if (el.isContentEditable) {
                el.innerText = '';
                el.textContent = '';
            } else if ('value' in el) {
                el.value = '';
            }
            el.dispatchEvent(new Event('input', { bubbles: true }));
            el.dispatchEvent(new Event('change', { bubbles: true }));
        }
    """)
    
    # Вставка текстом через keyboard (более стабильно, чем JS)
    await page.keyboard.insert_text(text)
    
    # Небольшая задержка для применения изменений
    await asyncio.sleep(0.3)
    
    # Проверка введённого текста
    entered = await comment_input.evaluate("""
        (el) => {
            if (el.isContentEditable) {
                return el.innerText || el.textContent || '';
            } else if ('value' in el) {
                return el.value || '';
            }
            return '';
        }
    """)
    
    return entered.strip()


async def get_input_text(comment_input) -> str:
    """Reads current text from contenteditable/input/textarea."""
    try:
        value = await comment_input.evaluate(
            """
            (el) => {
                if (!el) return '';
                if (el.isContentEditable) {
                    return (el.innerText || el.textContent || '').trim();
                }
                if ('value' in el) {
                    return (el.value || '').trim();
                }
                return (el.innerText || el.textContent || '').trim();
            }
            """
        )
        return (value or "").strip()
    except Exception:
        return ""


async def set_comment_text_with_retry(comment_input, text: str, max_attempts: int = 3) -> str:
    """
    Reliable text set with verification.

    Retries only text-input stage when text is lost/empty.
    """
    desired = (text or "").strip()
    if not desired:
        return ""

    last_observed = ""
    attempts = max(1, int(max_attempts))

    for _ in range(attempts):
        try:
            last_observed = await set_comment_text_reliable(comment_input, desired)
        except Exception:
            last_observed = ""

        if is_comment_match(desired, last_observed, min_ratio=0.85):
            return last_observed

        # Fallback: direct fill, then re-check.
        try:
            await comment_input.fill(desired)
        except Exception:
            pass

        await asyncio.sleep(0.2)
        actual = await get_input_text(comment_input)
        last_observed = actual or last_observed
        if is_comment_match(desired, actual, min_ratio=0.85):
            return actual

    return last_observed


async def attach_publish_watcher(page: Page) -> Tuple[asyncio.Future, Dict[str, Any], Callable]:
    """
    УЛУЧШЕНО: Создает watcher для отслеживания сетевого ответа публикации комментария.
    
    КРИТИЧНО: Подписка на response должна быть ДО клика по submit, а не после.
    УЛУЧШЕНО: Возвращает функцию для удаления обработчика (предотвращает утечки памяти).
    
    Args:
        page: Страница Playwright
        
    Returns:
        tuple[Future, dict, Callable]: (Future для ожидания ответа, dict для результата, функция для удаления обработчика)
            dict будет заполнен при получении ответа: {
                'ok': bool,  # True если 200-299
                'status': int | None,  # HTTP статус
                'url': str | None  # URL ответа
            }
    """
    loop = asyncio.get_event_loop()
    done = loop.create_future()
    result: Dict[str, Any] = {
        "ok": False,
        "status": None,
        "url": None,
        "comment_id": None,  # ОБНОВЛЕНО: comment_id из Network response
        "status_code": None,  # status_code из JSON ответа
        "status_msg": None,  # status_msg из JSON ответа
        "parent_comment_id": None  # Для ответов: ID родительского комментария
    }
    
    async def on_response(resp):
        """Обработчик response событий (async для Playwright events)."""
        try:
            req = resp.request
            if req.method != "POST":
                return
            u = resp.url.lower()
            # ОБНОВЛЕНО: Реальные эндпоинты для комментариев, ответов и лайков
            # URL: https://www.tiktok.com/api/comment/publish/ (комментарии и ответы)
            # URL: https://www.tiktok.com/api/comment/digg/ (лайки)
            if re.search(r"/api/comment/publish/?(?:\\?|$)", u):
                result["status"] = resp.status
                result["url"] = resp.url
                result["ok"] = resp.ok  # True для 200-299
                
                # ОБНОВЛЕНО: Извлекаем comment_id из JSON ответа
                try:
                    response_json = await resp.json()
                    extracted = _extract_publish_response_fields(response_json)
                    result["comment_id"] = extracted.get("comment_id")
                    result["status_code"] = extracted.get("status_code")
                    result["status_msg"] = extracted.get("status_msg")
                    result["parent_comment_id"] = extracted.get("parent_comment_id")

                    if result.get("comment_id"):
                        logger.debug(
                            f"[PUBLISH_WATCHER] Extracted comment_id={str(result['comment_id'])[:20]}, "
                            f"status_code={result.get('status_code')}"
                        )
                except Exception as json_error:
                    logger.debug(f"[PUBLISH_WATCHER] Error parsing JSON response: {json_error}")
                    # Продолжаем без comment_id, но с базовой информацией
                
                if not done.done():
                    done.set_result(result.copy())  # Копируем результат для безопасности
        except Exception as e:
            logger.debug(f"[PUBLISH_WATCHER] Error processing response: {e}")
    
    # Подписываемся на события response ДО клика (критично!)
    page.on("response", on_response)
    
    # Функция для удаления обработчика (предотвращает утечки памяти)
    def remove_listener():
        try:
            page.remove_listener("response", on_response)
        except Exception as e:
            logger.debug(f"[PUBLISH_WATCHER] Error removing listener: {e}")
    
    return done, result, remove_listener


async def click_and_wait_comment_response(
    page: Page,
    submit_button,
    timeout_ms: int = 12000
) -> Tuple[bool, Dict[str, Any]]:
    """
    УЛУЧШЕНО: Ловим сетевой результат отправки комментария.
    
    Использует улучшенный подход через attach_publish_watcher.
    УЛУЧШЕНО: Удаляет обработчик событий после использования (предотвращает утечки памяти).
    
    Args:
        page: Страница Playwright
        submit_button: Кнопка отправки комментария
        timeout_ms: Таймаут ожидания ответа в миллисекундах
        
    Returns:
        tuple[bool, dict]: (успех, информация о ответе)
            dict содержит: {
                'status': int | None,  # HTTP статус
                'url': str | None,  # URL ответа
                'ok': bool,  # True если 200-299
                'comment_id': str | None,  # ОБНОВЛЕНО: comment_id из response.comment.cid
                'status_code': int | None,  # status_code из JSON ответа (0 = успех)
                'status_msg': str | None,  # status_msg из JSON ответа
                'parent_comment_id': str | None,  # Для ответов: ID родительского комментария
                'reason': str | None  # Причина отсутствия ответа
            }
    """
    # Используем улучшенный watcher
    done, result, remove_listener = await attach_publish_watcher(page)
    
    try:
        async def _wait_publish(wait_ms: int):
            return await asyncio.wait_for(asyncio.shield(done), timeout=max(1, wait_ms) / 1000)

        async def _is_submit_still_interactable() -> bool:
            try:
                return bool(
                    await submit_button.evaluate(
                        """
                        (el) => {
                            if (!el) return false;
                            const aria = (el.getAttribute('aria-disabled') || '').toLowerCase();
                            const cls = (el.className || '').toString().toLowerCase();
                            const disabled = Boolean(el.disabled) || aria === 'true' || cls.includes('disabled');
                            return !disabled;
                        }
                        """
                    )
                )
            except Exception:
                return True

        primary_wait_ms = max(1000, int(timeout_ms * 0.7))
        secondary_wait_ms = max(1000, timeout_ms - primary_wait_ms)

        # First submit attempt: click
        await submit_button.click()
        try:
            publish_result = await _wait_publish(primary_wait_ms)
            logger.debug(
                f"[NETWORK] Comment submission response: status={publish_result.get('status')}, "
                f"ok={publish_result.get('ok')}, url={publish_result.get('url', '')[:100]}"
            )
            return True, publish_result
        except asyncio.TimeoutError:
            logger.debug(
                f"[NETWORK] No response after click in {primary_wait_ms}ms, "
                "trying fallback path"
            )

        # Fallback: if button still looks interactable, try Enter
        tried_enter = False
        if not done.done() and await _is_submit_still_interactable():
            try:
                try:
                    await submit_button.focus()
                except Exception:
                    pass
                await page.keyboard.press("Enter")
                tried_enter = True
            except Exception as enter_error:
                logger.debug(f"[NETWORK] Enter fallback failed: {enter_error}")

        try:
            publish_result = await _wait_publish(secondary_wait_ms)
            logger.debug(
                f"[NETWORK] Comment submission response (fallback={tried_enter}): "
                f"status={publish_result.get('status')}, ok={publish_result.get('ok')}, "
                f"url={publish_result.get('url', '')[:100]}"
            )
            return True, publish_result
        except asyncio.TimeoutError:
            reason = "no_response_timeout_click_enter" if tried_enter else "no_response_timeout_click_only"
            logger.warning(f"[NETWORK] No comment response received within {timeout_ms}ms ({reason})")
            return False, {
                **result,
                "reason": reason,
                "success": False
            }
             
    except Exception as e:
        logger.warning(f"[NETWORK] Error during comment submission: {e}")
        return False, {
            **result,
            "reason": str(e),
            "success": False
        }
    finally:
        # УЛУЧШЕНО: Удаляем обработчик событий для предотвращения утечек памяти
        remove_listener()


async def detect_error_toast(page: Page, wait_ms: int = 2000) -> Optional[str]:
    """
    ИСПРАВЛЕНО: Отлавливаем тосты/ошибки UI после отправки (рекомендации ChatGPT).
    
    Проверяет наличие toast-сообщений об ошибках после отправки комментария.
    Это важный сигнал, что сервер отклонил комментарий, даже если UI не показал явной ошибки.
    
    Args:
        page: Страница Playwright
        wait_ms: Задержка перед проверкой (тосты появляются не сразу)
        
    Returns:
        str | None: Текст ошибки из тоста, или None если тоста нет
    """
    await asyncio.sleep(wait_ms / 1000)  # Конвертируем в секунды
    
    toast_selectors = [
        '[role="alert"]',
        '[data-e2e="toast"]',
        '[data-e2e="error-toast"]',
        'div[class*="Toast"]',
        'div[class*="toast"]',
        'div[class*="Error"]',
        'div[class*="error"]',
        '[class*="notification"]',
        '[class*="message"]',
    ]
    
    error_keywords = [
        "try again",
        "couldn't",
        "could not",
        "error",
        "не удалось",
        "попробуйте позже",
        "слишком много",
        "too many",
        "rate limit",
        "limit exceeded",
        "failed",
        "failed to post",
        "cannot",
        "unable",
    ]
    
    for selector in toast_selectors:
        try:
            elements = await page.query_selector_all(selector)
            for el in elements:
                if el and await el.is_visible():
                    txt = (await el.inner_text()).strip()
                    if txt:
                        txt_lower = txt.lower()
                        # Проверяем, содержит ли тост признаки ошибки
                        if any(keyword in txt_lower for keyword in error_keywords):
                            logger.warning(f"[TOAST] Error toast detected: {txt[:100]}")
                            return txt
        except Exception as e:
            logger.debug(f"[TOAST] Error checking toast selector {selector}: {e}")
            continue
    
    return None


async def find_comment_in_list(
    page: Page,
    expected_text: str,
    max_scrolls: int = 10,
    scroll_delay_ms: int = 600,
    allowed_text_selectors: list[str] | None = None,
    ensure_open: bool = True,
    strict_scope: bool = False,
    min_match_ratio: float = 0.7,
    top_level_only: bool = False,
    expected_author: Optional[str] = None,
    require_author_match: bool = False,
    phrase_match: bool = False,
) -> Tuple[bool, Dict[str, Any]]:
    """
    Find a comment by text inside the TikTok comment list.

    Args:
        page: Playwright page.
        expected_text: Expected comment text (compared via is_comment_match).
        max_scrolls: Max scroll iterations inside the comment list.
        scroll_delay_ms: Delay between scrolls.
        allowed_text_selectors: Optional override of text selectors.
        ensure_open: If True, attempts to open the comment section first.
        strict_scope: When True, search ONLY inside the comment-list container
            and refuse to fall back to a page-wide scan. If the container is
            absent the function returns (False, {"error": "comment_list_not_open"})
            without enumerating any nodes. Use for observer / reply flows where
            page-wide selectors like <li>/<article>/<p> match sidebar feed
            cards ("one-column-item-N") and produce false signals.
            Default False preserves legacy publish-flow behaviour.
        top_level_only: When True, reject nested reply rows while searching
            for a parent/main comment. Keep False for reply visibility checks.
        require_author_match: When True and expected_author is known, rows
            without a readable matching author are rejected instead of falling
            back to text-only confirmation.
        phrase_match: Opt-in whole-word phrase search, ignoring case and emoji.
            Never enable for verification of a published comment/reply.

    Returns:
        (found, info_dict). When strict_scope=True and the container is not
        present, info["error"] == "comment_list_not_open" and diag includes
        why we believe so.
    """
    expected = normalize_search_phrase(expected_text) if phrase_match else normalize_text(expected_text)
    if phrase_match and not expected:
        return False, {"error": "empty_search_phrase", "scrolls": 0}

    def _log_parent_search_trace(
        stage: str,
        *,
        level: int = logging.INFO,
        **fields: Any,
    ) -> None:
        payload: Dict[str, Any] = {
            "stage": stage,
            "expected": expected[:120],
            "strict_scope": strict_scope,
            "top_level_only": top_level_only,
            "ensure_open": ensure_open,
            "max_scrolls": max_scrolls,
            "min_match_ratio": min_match_ratio,
            "phrase_match": phrase_match,
            "require_author_match": require_author_match,
            "url": getattr(page, "url", None),
        }
        payload.update({k: v for k, v in fields.items() if v is not None})
        logger.log(level, "[PARENT_SEARCH_TRACE] %s", json.dumps(payload, ensure_ascii=False)[:2200])

    _log_parent_search_trace("start")
    captcha_error = await _visible_captcha_error(page)
    if captcha_error:
        _log_parent_search_trace(
            "captcha_detected",
            level=logging.WARNING,
            error=captcha_error,
        )
        return False, {
            "error": captcha_error,
            "scrolls": 0,
            "text_normalized": expected,
        }
    
    # Селекторы для контейнера списка комментариев
    list_selectors = [
        '[data-smart-comment-surface-opened="1"]',
        '[data-e2e="comment-list"]',
        '[data-e2e="comment-list-container"]',
        '[class*="CommentList"]',
        '[class*="CommentListContainer"]',
        '[class*="DivCommentListContainer"]',
        '[class*="CommentContainer"]',
        '[class*="comment-list"]',
        '[class*="comment-list-container"]',
        '[data-e2e*="comment-container"]',
    ]
    
    # ОБНОВЛЕНО: Используем несколько вариантов DOM TikTok. Desktop web часто
    # меняет className, поэтому нельзя завязываться только на DivCommentObject.
    from executor.tiktok_selectors import selectors
    comment_root_selectors = [
        selectors.COMMENT_ROOT,
        selectors.COMMENT_ITEM,
        '[data-e2e="comment-item"]',
        '[data-e2e*="comment-level"]',
        '[data-cid]:not([data-cid^="one-column-item"])',
        '[data-comment-id]',
        '[class*="CommentItem"]',
        '[class*="DivComment"]',
        'article',
        'li',
    ]
    comment_root_selector = ", ".join(dict.fromkeys(s for s in comment_root_selectors if s))
    # Текст комментария/ответа в разных вариантах разметки
    comment_text_selectors = allowed_text_selectors or [
        selectors.COMMENT_TEXT_SPAN,            # level-1 (main comment)
        '[data-e2e="comment-level-1"]',
        '[data-e2e*="comment-level"]',
        '[data-e2e*="comment-text"]',
        'span[data-e2e="comment-level-2"]',     # level-2 (reply)
        'span[data-e2e="comment-level-3"]',     # deeper threads
        '[data-e2e="comment-level-2"]',
        '[data-e2e="comment-level-3"]',
        'p',
        'span',
    ]

    if ensure_open:
        try:
            from executor.dom_actions import open_comment_section
            await open_comment_section(
                page,
                max_wait=6.0,
                require_input=False,
                require_list_with_items=bool(strict_scope),
            )
            await asyncio.sleep(0.8)
        except Exception:
            pass
    
    # Legacy publish-flow may ask this helper to open the comments itself.
    # Strict observer/reply callers open the drawer before calling us; clicking
    # here can toggle TikTok's drawer closed and then falsely report
    # comment_list_not_open.
    if ensure_open and not strict_scope:
        try:
            comment_button_selectors = [
                'button[data-e2e="comments"]',
                '[data-e2e="comments"]',
                'button:has([data-e2e="comment-icon"])',
                '[data-e2e="comment-icon"]',
            ]

            for btn_selector in comment_button_selectors:
                try:
                    button = await page.query_selector(btn_selector)
                    if button:
                        clicked = False

                        try:
                            is_visible = await button.is_visible()
                            if is_visible:
                                await button.click(timeout=2000)
                                clicked = True
                        except Exception:
                            pass

                        if not clicked:
                            try:
                                await button.evaluate("el => el.click()")
                                clicked = True
                            except Exception:
                                pass

                        if not clicked:
                            try:
                                box = await button.bounding_box()
                                if box:
                                    await page.mouse.click(box["x"] + box["width"] / 2, box["y"] + box["height"] / 2)
                                    clicked = True
                            except Exception:
                                pass

                        if clicked:
                            await asyncio.sleep(1.5)
                            logger.debug(f"[FIND_COMMENT] Clicked comment button: {btn_selector[:50]}")
                            break
                except Exception:
                    continue
        except Exception:
            pass
    
    # Найти контейнер списка комментариев (для прокрутки И как scope поиска)
    comment_list = None
    scroll_target = None
    matched_list_selector: Optional[str] = None
    try:
        selected_list = await page.evaluate(
            """
            ({selectors, strictScope}) => {
                const visible = (el) => {
                    if (!el || !el.getBoundingClientRect) return false;
                    const r = el.getBoundingClientRect();
                    const s = getComputedStyle(el);
                    return r.width > 0 && r.height > 0 && s.display !== 'none' && s.visibility !== 'hidden';
                };
                const visibleCount = (root, selector) => {
                    try {
                        return [...root.querySelectorAll(selector)].filter(visible).length;
                    } catch (_) {
                        return 0;
                    }
                };
                const strongItemSelectors = [
                    '[data-e2e="comment-item"]',
                    '[data-e2e*="comment-level"]',
                    '[data-e2e*="comment-text"]',
                    '[data-e2e*="comment-username"]',
                    '[class*="DivCommentObject"]',
                    '[data-cid]:not([data-cid^="one-column-item"])',
                    '[data-comment-id]',
                ].join(', ');
                const itemSelectors = [strongItemSelectors, 'p', 'li', '[role="listitem"]'].join(', ');
                const entries = [];
                for (const selector of selectors) {
                    let nodes = [];
                    try { nodes = [...document.querySelectorAll(selector)]; } catch (_) { nodes = []; }
                    for (const el of nodes) {
                        if (!visible(el)) continue;
                        const strongItems = visibleCount(el, strongItemSelectors);
                        const items = visibleCount(el, itemSelectors);
                        if (strictScope && strongItems <= 0) continue;
                        let strongList = false;
                        try {
                            strongList = el.matches(
                                '[data-e2e="comment-list"], [data-e2e="comment-list-container"], ' +
                                '[class*="CommentList"], [class*="DivCommentListContainer"]'
                            );
                        } catch (_) {}
                        if (!strongList && strongItems <= 0) continue;
                        const r = el.getBoundingClientRect();
                        const area = Math.max(0, r.width) * Math.max(0, r.height);
                        const score =
                            strongItems * 1000 +
                            items * 10 +
                            (strongList ? 100 : 0) -
                            Math.max(0, area - (window.innerWidth * window.innerHeight * 0.8)) / 10000;
                        entries.push({ el, selector, score, items, strongItems });
                    }
                }
                entries.sort((a, b) => b.score - a.score);
                const chosen = entries[0] || null;
                if (!chosen) return null;
                const badScrollTags = new Set([
                    'IMG', 'SVG', 'PATH', 'USE', 'PICTURE', 'SOURCE', 'CANVAS',
                    'SPAN', 'STRONG', 'P', 'A', 'BUTTON', 'I'
                ]);
                const isRealScrollContainer = (el) => {
                    if (!el || !visible(el) || badScrollTags.has(el.tagName)) return false;
                    const r = el.getBoundingClientRect();
                    if (r.width < 180 || r.height < 120) return false;
                    return el.scrollHeight > el.clientHeight + 20;
                };
                const scrollScore = (el) => {
                    if (!isRealScrollContainer(el)) return -1;
                    const r = el.getBoundingClientRect();
                    const strongItems = visibleCount(el, strongItemSelectors);
                    return strongItems * 1000 + Math.min(el.clientHeight || 0, 1200) + Math.min(r.width || 0, 800);
                };
                const scrollableDescendant = (el) => {
                    try {
                        const nodes = [...el.querySelectorAll('*')].filter(visible);
                        const candidates = nodes
                            .map((node) => ({ node, score: scrollScore(node) }))
                            .filter((item) => item.score >= 0 && visibleCount(item.node, strongItemSelectors) > 0)
                            .sort((a, b) => b.score - a.score);
                        return candidates.length ? candidates[0].node : null;
                    } catch (_) {
                        return null;
                    }
                };
                const scrollableAncestor = (el) => {
                    for (let cur = el; cur && cur !== document.body && cur !== document.documentElement; cur = cur.parentElement) {
                        if (!isRealScrollContainer(cur)) continue;
                        let style = null;
                        try { style = getComputedStyle(cur); } catch (_) { style = null; }
                        const overflow = style ? `${style.overflowY || ''} ${style.overflow || ''}` : '';
                        const overflowLooksScrollable = /(auto|scroll|overlay)/i.test(overflow);
                        if (overflowLooksScrollable || cur !== el) {
                            return cur;
                        }
                    }
                    return el;
                };
                const scopeEl = chosen.el;
                const openedScrollEl = (() => {
                    try {
                        const node = document.querySelector('[data-smart-comment-scroll-opened="1"]');
                        return node && (node === scopeEl || scopeEl.contains(node)) && isRealScrollContainer(node)
                            ? node
                            : null;
                    } catch (_) {
                        return null;
                    }
                })();
                let scrollEl = openedScrollEl || (isRealScrollContainer(chosen.el)
                    ? chosen.el
                    : (scrollableDescendant(chosen.el) || scrollableAncestor(chosen.el)));
                if (!isRealScrollContainer(scrollEl)) {
                    scrollEl = scopeEl;
                }
                try {
                    document
                        .querySelectorAll(
                            '[data-smart-id="comment-list-active-scope"], ' +
                            '[data-smart-id="comment-list-active-scroll"]'
                        )
                        .forEach((el) => el.removeAttribute('data-smart-id'));
                    document
                        .querySelectorAll(
                            '[data-smart-comment-list-active-scope], ' +
                            '[data-smart-comment-list-active-scroll]'
                        )
                        .forEach((el) => {
                            el.removeAttribute('data-smart-comment-list-active-scope');
                            el.removeAttribute('data-smart-comment-list-active-scroll');
                        });
                } catch (_) {}
                scopeEl.setAttribute('data-smart-comment-list-active-scope', '1');
                scrollEl.setAttribute('data-smart-comment-list-active-scroll', '1');
                return {
                    selector: chosen.selector,
                    scroll_container_tag: scrollEl.tagName,
                    scroll_container_class: String(scrollEl.className || '').slice(0, 160),
                    scroll_container_scrollable: isRealScrollContainer(scrollEl),
                    items: chosen.items,
                    strong_items: chosen.strongItems,
                    score: chosen.score,
                };
            }
            """,
            {"selectors": list_selectors, "strictScope": bool(strict_scope)},
        )
        if selected_list:
            candidate = await page.query_selector('[data-smart-comment-list-active-scope="1"]')
            if candidate:
                comment_list = candidate
                scroll_target = (
                    await page.query_selector('[data-smart-comment-list-active-scroll="1"]')
                ) or candidate
                matched_list_selector = str(selected_list.get("selector") or "")
                logger.debug(
                    "[FIND_COMMENT] Selected scoped comment list: %s items=%s strong_items=%s",
                    matched_list_selector,
                    selected_list.get("items"),
                    selected_list.get("strong_items"),
                )
                _log_parent_search_trace(
                    "scope_selected",
                    selector=matched_list_selector,
                    items=selected_list.get("items"),
                    strong_items=selected_list.get("strong_items"),
                    scroll_container_scrollable=selected_list.get("scroll_container_scrollable"),
                    scroll_container_tag=selected_list.get("scroll_container_tag"),
                    score=selected_list.get("score"),
                )
    except Exception as exc:
        logger.debug("[FIND_COMMENT] scored comment-list selection failed: %s", exc)
        if is_browser_target_closed(exc):
            return False, {"error": "browser_target_closed", "scrolls": 0}

    for sel in ([] if comment_list is not None or strict_scope else list_selectors):
        try:
            candidate = await page.query_selector(sel)
            if candidate:
                try:
                    if await candidate.is_visible():
                        comment_list = candidate
                        scroll_target = candidate
                        matched_list_selector = sel
                        logger.debug(f"[FIND_COMMENT] Found comment list container: {sel}")
                        break
                except Exception:
                    # is_visible can throw on detached elements — try the next selector
                    continue
        except Exception:
            continue

    # Если контейнер не нашли:
    # - strict_scope=True: возвращаем явный comment_list_not_open. НЕ ищем по
    #   странице глобально — page-wide селекторы (<li>, <article>, <p>)
    #   матчатся с фид-карточками "similar videos", сайдбар-айтемами и т.д.,
    #   и приводят к ложно-найденным «комментам» (`cid: one-column-item-N`).
    # - strict_scope=False: легаси-поведение — fallback на всю страницу, чтобы
    #   не сломать publish-флоу, где local DOM может быть слегка специфичен.
    if not comment_list:
        if strict_scope:
            logger.warning(
                "[FIND_COMMENT] strict_scope=True but no comment-list container "
                "visible on %s — refusing page-wide fallback",
                page.url[:120],
            )
            return False, {
                "error": "comment_list_not_open",
                "scrolls": 0,
                "text_normalized": expected,
                "list_selector_tried": list_selectors,
            }
        logger.debug("[FIND_COMMENT] Comment list container not found, using page scroll")

    # Search scope: внутри контейнера (если найден) или по всей странице (легаси).
    # В strict_scope мы сюда попадаем ТОЛЬКО когда comment_list существует.
    if scroll_target is None and comment_list is not None:
        scroll_target = comment_list

    async def _query_comment_roots():
        if comment_list is not None:
            return await comment_list.query_selector_all(comment_root_selector)
        return await page.query_selector_all(comment_root_selector)

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

    # Цикл поиска с прокруткой
    if strict_scope and scroll_target is not None:
        try:
            await scroll_target.evaluate("(el) => { el.scrollTop = 0; }")
            await asyncio.sleep(0.2)
            _log_parent_search_trace("scope_scroll_reset")
        except Exception as exc:
            logger.debug("[FIND_COMMENT] failed to reset scoped comment-list scroll: %s", exc)

    stagnant_scrolls = 0
    last_scroll_state: Dict[str, Any] | None = None
    wheel_nudge_attempts = 0
    for scroll_i in range(max_scrolls):
        captcha_error = await _visible_captcha_error(page)
        if captcha_error:
            _log_parent_search_trace(
                "captcha_detected",
                level=logging.WARNING,
                scroll=scroll_i,
                error=captcha_error,
            )
            return False, {
                "error": captcha_error,
                "scrolls": scroll_i + 1,
                "text_normalized": expected,
                "matched_list_selector": matched_list_selector,
            }
        # ОБНОВЛЕНО: Ищем корневые контейнеры комментариев в нужном scope.
        try:
            comment_roots = await _query_comment_roots()
            for idx, comment_root in enumerate(comment_roots):
                try:
                    txt = None
                    matched_selector = None

                    for text_selector in comment_text_selectors:
                        text_span = await comment_root.query_selector(text_selector)
                        if not text_span:
                            continue
                        if not await _looks_like_top_level_parent(text_span):
                            continue

                        candidate_text = await text_span.evaluate("el => el.textContent || el.innerText || ''")
                        if not candidate_text:
                            continue

                        if is_comment_match(expected_text, candidate_text, min_ratio=min_match_ratio, phrase_match=phrase_match):
                            txt = candidate_text
                            matched_selector = text_selector
                            break

                    if txt:
                        anchor_token = _new_parent_anchor_token()
                        try:
                            await comment_root.evaluate(
                                """
                                (el, token) => {
                                    try {
                                        el.setAttribute('data-smart-parent-anchor', token);
                                        return true;
                                    } catch (_) {
                                        return false;
                                    }
                                }
                                """,
                                anchor_token,
                            )
                        except Exception:
                            anchor_token = None
                        comment_id = await comment_root.evaluate(
                            """
                            el => {
                                const selector = '[data-cid], [data-comment-id], [id^="comment-"]';
                                const read = (node) => {
                                    if (!node) return null;
                                    const raw =
                                        node.getAttribute('data-cid') ||
                                        node.getAttribute('data-comment-id') ||
                                        (node.id && node.id.startsWith('comment-') ? node.id : null) ||
                                        null;
                                    return raw ? String(raw).replace(/^comment-/, '') : null;
                                };
                                try {
                                    const host = el.matches(selector) ? el : el.closest(selector);
                                    const fromHost = read(host);
                                    if (fromHost) return fromHost;
                                } catch (_) {}
                                try {
                                    return read(el.querySelector(selector));
                                } catch (_) {
                                    return null;
                                }
                            }
                            """
                        )
                        author = None
                        try:
                            author = await comment_root.evaluate(r"""
                                el => {
                                    const usernameMarker = el.querySelector('[data-e2e="comment-username-1"]');
                                    const preferredLinks = [];
                                    if (usernameMarker) {
                                        if (usernameMarker.matches?.('a[href*="/@"]')) preferredLinks.push(usernameMarker);
                                        const nested = usernameMarker.querySelector?.('a[href*="/@"]');
                                        if (nested) preferredLinks.push(nested);
                                    }
                                    const explicit = el.querySelector('a[data-e2e="comment-username-1"][href*="/@"]');
                                    if (explicit) preferredLinks.push(explicit);
                                    for (const link of el.querySelectorAll('a[href*="/@"]')) {
                                        if (link.closest('[data-e2e*="comment-level"], [data-e2e*="comment-text"]')) continue;
                                        preferredLinks.push(link);
                                    }
                                    for (const link of preferredLinks) {
                                        const href = link.getAttribute('href') || '';
                                        const match = href.match(/\/@([^/?#]+)/);
                                        if (match && match[1]) return match[1];
                                    }
                                    if (usernameMarker) {
                                        const text = (usernameMarker.innerText || usernameMarker.textContent || '').trim();
                                        if (text) return text.replace(/^@/, '');
                                    }
                                    return null;
                                }
                            """)
                            author = (author or "").strip() or None
                        except Exception:
                            author = None
                        # Author-scoped match (parallel safety): when an
                        # expected_author is supplied, identical comment text
                        # posted by a DIFFERENT account must not count. Legacy
                        # callers may still allow text-only fallback when the
                        # row author is unreadable; main local confirmation
                        # sets require_author_match=True and rejects unknown
                        # authors instead of confirming a possibly foreign row.
                        if expected_author:
                            exp_author_norm = normalize_author(expected_author)
                            row_author_norm = normalize_author(author) if author else ""
                            if exp_author_norm and require_author_match and not row_author_norm:
                                _log_parent_search_trace(
                                    "author_missing_skip",
                                    scroll=scroll_i,
                                    position=idx,
                                    requested_author=exp_author_norm,
                                )
                                continue
                            if exp_author_norm and row_author_norm and row_author_norm != exp_author_norm:
                                _log_parent_search_trace(
                                    "author_mismatch_skip",
                                    scroll=scroll_i,
                                    position=idx,
                                    requested_author=exp_author_norm,
                                    row_author=row_author_norm,
                                )
                                continue
                        logger.info(
                            f"[FIND_COMMENT] Comment found: scroll={scroll_i}, position={idx}, "
                            f"text_normalized={normalize_text(txt)[:50]}"
                        )
                        _log_parent_search_trace(
                            "found",
                            scroll=scroll_i,
                            position=idx,
                            comment_id=str(comment_id) if comment_id else None,
                            text_selector=matched_selector,
                            matched_preview=normalize_text(txt)[:120],
                        )
                        return True, {
                            "position": idx,
                            "scroll": scroll_i,
                            "text_normalized": normalize_text(txt),
                            "raw_text": txt,
                            "comment_id": str(comment_id) if comment_id else None,
                            "text_selector": matched_selector,
                            "author": author,
                            "anchor_token": anchor_token,
                        }
                except Exception as e:
                    logger.debug(f"[FIND_COMMENT] Error processing comment root {idx}: {e}")
                    if is_browser_target_closed(e):
                        return False, {"error": "browser_target_closed", "scrolls": scroll_i}
                    continue
        except Exception as e:
            logger.debug(f"[FIND_COMMENT] Error querying comment roots: {e}")
            if is_browser_target_closed(e):
                return False, {"error": "browser_target_closed", "scrolls": scroll_i}
            continue
        
        # Прокрутка контейнера (если найден) или страницы (fallback)
        try:
            before_scroll_state = None
            if scroll_target:
                try:
                    before_scroll_state = await scroll_target.evaluate(
                        "(el) => ({top: el.scrollTop, height: el.scrollHeight, client: el.clientHeight})"
                    )
                except Exception:
                    before_scroll_state = None
                # Прокручиваем контейнер комментариев
                await scroll_target.evaluate("""
                    (el) => {
                        el.scrollTop += el.clientHeight * 0.9;
                    }
                """)
            else:
                # Fallback: прокрутка страницы мышью
                await page.mouse.wheel(0, 800)
            if scroll_target:
                try:
                    after_scroll_state = await scroll_target.evaluate(
                        "(el) => ({top: el.scrollTop, height: el.scrollHeight, client: el.clientHeight})"
                    )
                except Exception:
                    after_scroll_state = None
                if before_scroll_state and after_scroll_state:
                    moved = after_scroll_state.get("top") != before_scroll_state.get("top")
                    grew = (
                        last_scroll_state is not None
                        and after_scroll_state.get("height") != last_scroll_state.get("height")
                    )
                    can_scroll = (after_scroll_state.get("height") or 0) > (after_scroll_state.get("client") or 0) + 5
                    stagnant_scrolls = 0 if (moved or grew) else stagnant_scrolls + 1
                    last_scroll_state = after_scroll_state
                    stagnant_limit = 4 if strict_scope else 2
                    if not can_scroll or stagnant_scrolls >= stagnant_limit:
                        nudge_limit = 3 if not can_scroll else 2
                        if strict_scope and wheel_nudge_attempts < nudge_limit:
                            wheel_nudge_attempts += 1
                            nudge_target = scroll_target if scroll_target is not None else comment_list
                            nudge = await _nudge_comment_list_scroll(
                                nudge_target,
                                delta=max(800, int((after_scroll_state.get("client") or 0) * 1.2)),
                            )
                            _log_parent_search_trace(
                                "scope_wheel_nudge",
                                scroll=scroll_i,
                                reason="no_scroll_progress_before_stop",
                                scroll_state=after_scroll_state,
                                nudge=nudge,
                                nudge_attempt=wheel_nudge_attempts,
                            )
                            await asyncio.sleep(scroll_delay_ms / 1000)
                            continue
                        logger.debug(
                            "[FIND_COMMENT] stopping early: no comment-list scroll progress state=%s",
                            after_scroll_state,
                        )
                        _log_parent_search_trace(
                            "scroll_stopped",
                            scroll=scroll_i,
                            reason="no_comment_list_scroll_progress",
                            scroll_state=after_scroll_state,
                        )
                        break
        except Exception as e:
            logger.debug(f"[FIND_COMMENT] Error scrolling: {e}")
            if is_browser_target_closed(e):
                return False, {"error": "browser_target_closed", "scrolls": scroll_i}
        
        # Задержка между прокрутками для загрузки новых комментариев
        await asyncio.sleep(scroll_delay_ms / 1000)
    
    diag_scope_selector = None
    if comment_list is not None:
        diag_scope_selector = '[data-smart-id="comment-list-diag-scope"]'
        try:
            await comment_list.evaluate(
                "(el) => el.setAttribute('data-smart-id', 'comment-list-diag-scope')"
            )
        except Exception:
            diag_scope_selector = None

    try:
        diag = await page.evaluate(
            """
            ({rootSelector, textSelectors, scopeSelector, matchedListSelector, requireScope}) => {
                const visible = (el) => {
                    if (!el || !el.getBoundingClientRect) return false;
                    const r = el.getBoundingClientRect();
                    const s = getComputedStyle(el);
                    return r.width > 0 && r.height > 0 && s.display !== 'none' && s.visibility !== 'hidden';
                };
                const explicitScope = scopeSelector ? document.querySelector(scopeSelector) : null;
                if (requireScope && !explicitScope) {
                    return {
                        root_count: 0,
                        sample_count: 0,
                        samples: [],
                        scope: 'scope_unavailable',
                        matched_list_selector: matchedListSelector || null,
                        list: null,
                        url: location.href,
                    };
                }
                const scope = explicitScope || document;
                const roots = [...scope.querySelectorAll(rootSelector)].filter(visible);
                const samples = [];
                const seenText = new Set();
                for (const root of roots) {
                    let text = '';
                    let selector = null;
                    for (const sel of textSelectors) {
                        let node = null;
                        try { node = root.matches(sel) ? root : root.querySelector(sel); } catch (e) { node = null; }
                        if (!node) continue;
                        text = (node.innerText || node.textContent || '').trim().replace(/\\s+/g, ' ');
                        if (text) {
                            selector = sel;
                            break;
                        }
                    }
                    if (!text || text.length < 2 || seenText.has(text)) continue;
                    seenText.add(text);
                    const authorEl = root.querySelector(
                        'a[href*="/@"], [data-e2e*="username"], [class*="UserName"], [class*="username"], [class*="author"]'
                    );
                    const href = authorEl ? (authorEl.getAttribute('href') || '') : '';
                    const m = href.match(/\\/@([^/?]+)/);
                    samples.push({
                        text: text.slice(0, 180),
                        selector,
                        author: m ? m[1] : (authorEl ? (authorEl.innerText || authorEl.textContent || '').trim().replace(/^@/, '') : null),
                        cid: root.getAttribute('data-cid') || root.getAttribute('data-comment-id') || (root.id && root.id.startsWith('comment-') ? root.id : null) || null,
                        dom_id: root.getAttribute('data-id') || root.id || null,
                    });
                    if (samples.length >= 12) break;
                }
                const list = explicitScope || document.querySelector('[data-e2e="comment-list"], [class*="CommentList"], [class*="comment-list"]');
                return {
                    root_count: roots.length,
                    sample_count: samples.length,
                    samples,
                    scope: explicitScope ? 'matched_comment_list' : 'document',
                    matched_list_selector: matchedListSelector || null,
                    list: list ? {
                        scrollTop: list.scrollTop,
                        clientHeight: list.clientHeight,
                        scrollHeight: list.scrollHeight,
                    } : null,
                    url: location.href,
                };
            }
            """,
            {
                "rootSelector": comment_root_selector,
                "textSelectors": comment_text_selectors,
                "scopeSelector": diag_scope_selector,
                "matchedListSelector": matched_list_selector,
                "requireScope": bool(strict_scope and comment_list is not None),
            },
        )
    except Exception as exc:
        diag = {"diag_error": str(exc)[:120]}

    logger.warning(
        "[FIND_COMMENT] Comment not found after %s scrolls expected=%r diag=%s",
        max_scrolls,
        expected[:120],
        json.dumps(diag, ensure_ascii=False)[:3000],
    )
    _log_parent_search_trace(
        "not_found",
        level=logging.WARNING,
        diag=diag,
    )
    return False, {
        "scrolls": max_scrolls,
        "text_normalized": expected,
        "diag": diag,
    }


async def find_comments_in_list_batch(
    page: Page,
    expected_texts: list[str],
    *,
    expected_comment_ids: list[Optional[str]] | None = None,
    expected_authors: list[Optional[str]] | None = None,
    require_author_match: bool = False,
    collect_author_fallbacks: bool = False,
    max_scrolls: int = 10,
    scroll_delay_ms: int = 600,
    allowed_text_selectors: list[str] | None = None,
    ensure_open: bool = True,
    strict_scope: bool = False,
    min_match_ratio: float = 0.7,
    return_partial_on_found: bool = False,
    partial_found_sink: Optional[Dict[int, Dict[str, Any]]] = None,
    progress_sink: Optional[Dict[str, Any]] = None,
    top_level_only: bool = False,
    _stabilize_retry: bool = False,
    phrase_match: bool = False,
    continue_from_current_position: bool = False,
) -> Tuple[Dict[int, Dict[str, Any]], Dict[str, Any]]:
    """Find multiple target comments in one scoped pass through the comment list.

    Returns a mapping keyed by the original expected_texts index. Each value has
    the same shape as find_comment_in_list(), plus matched_by and scroll_top.
    phrase_match opts into engagement keyword search; rows are considered in
    DOM order and each target retains only its first matching row.
    continue_from_current_position resumes a partial pass after its visible
    hits were handled; callers retain the bounded fallback for the prefix.
    """
    targets: list[Dict[str, Any]] = []
    ids = expected_comment_ids or []
    authors = expected_authors or []
    for idx, text in enumerate(expected_texts or []):
        normalized = normalize_search_phrase(text) if phrase_match else normalize_text(text)
        if not normalized:
            continue
        cid = str(ids[idx]).strip() if idx < len(ids) and ids[idx] else None
        author = normalize_author(authors[idx]) if idx < len(authors) and authors[idx] else None
        targets.append({"index": idx, "text": text, "normalized": normalized, "cid": cid, "author": author})

    def _log_batch_trace(stage: str, *, level: int = logging.INFO, **fields: Any) -> None:
        payload: Dict[str, Any] = {
            "stage": stage,
            "target_count": len(targets),
            "strict_scope": strict_scope,
            "ensure_open": ensure_open,
            "max_scrolls": max_scrolls,
            "min_match_ratio": min_match_ratio,
            "phrase_match": phrase_match,
            "require_author_match": require_author_match,
            "top_level_only": top_level_only,
            "return_partial_on_found": return_partial_on_found,
            "partial_sink": partial_found_sink is not None,
            "progress_sink": progress_sink is not None,
            "continue_from_current_position": continue_from_current_position,
            "url": getattr(page, "url", None),
        }
        payload.update({k: v for k, v in fields.items() if v is not None})
        logger.log(level, "[PARENT_BATCH_SEARCH_TRACE] %s", json.dumps(payload, ensure_ascii=False)[:2400])

    def _empty_result_info(**extra: Any) -> Dict[str, Any]:
        payload: Dict[str, Any] = {
            "found_count": 0,
            "missing_count": len(targets),
            "target_count": len(targets),
            "matched_indexes": [],
            "missing_indexes": [int(target["index"]) for target in targets],
            "missing_previews": [
                {
                    "index": int(target["index"]),
                    "text_preview": str(target.get("text") or "")[:120],
                    "author": target.get("author"),
                    "comment_id": target.get("cid"),
                }
                for target in targets[:12]
            ],
        }
        payload.update(extra)
        return payload

    _log_batch_trace("start")
    if not targets:
        return {}, {"target_count": 0, "scrolls": 0}
    captcha_error = await _visible_captcha_error(page)
    if captcha_error:
        _log_batch_trace(
            "captcha_detected",
            level=logging.WARNING,
            error=captcha_error,
        )
        return {}, _empty_result_info(
            error=captcha_error,
            scrolls=0,
        )

    list_selectors = [
        '[data-smart-comment-surface-opened="1"]',
        '[data-e2e="comment-list"]',
        '[data-e2e="comment-list-container"]',
        '[class*="CommentList"]',
        '[class*="CommentListContainer"]',
        '[class*="DivCommentListContainer"]',
        '[class*="CommentContainer"]',
        '[class*="comment-list"]',
        '[class*="comment-list-container"]',
        '[data-e2e*="comment-container"]',
    ]

    from executor.tiktok_selectors import selectors
    comment_root_selectors = [
        selectors.COMMENT_ROOT,
        selectors.COMMENT_ITEM,
        '[data-e2e="comment-item"]',
        '[data-e2e*="comment-level"]',
        '[data-cid]:not([data-cid^="one-column-item"])',
        '[data-comment-id]',
        '[class*="CommentItem"]',
        '[class*="DivComment"]',
        'article',
        'li',
    ]
    comment_root_selector = ", ".join(dict.fromkeys(s for s in comment_root_selectors if s))
    comment_text_selectors = allowed_text_selectors or [
        selectors.COMMENT_TEXT_SPAN,
        '[data-e2e="comment-level-1"]',
        '[data-e2e*="comment-level"]',
        '[data-e2e*="comment-text"]',
        'span[data-e2e="comment-level-2"]',
        'span[data-e2e="comment-level-3"]',
        '[data-e2e="comment-level-2"]',
        '[data-e2e="comment-level-3"]',
        'p',
        'span',
    ]

    if ensure_open:
        try:
            from executor.dom_actions import open_comment_section
            await open_comment_section(
                page,
                max_wait=6.0,
                require_input=False,
                require_list_with_items=bool(strict_scope),
            )
            await asyncio.sleep(0.8)
        except Exception:
            pass

    comment_list = None
    scroll_target = None
    matched_list_selector: Optional[str] = None
    try:
        selected_list = await page.evaluate(
            """
            ({selectors, strictScope}) => {
                const visible = (el) => {
                    if (!el || !el.getBoundingClientRect) return false;
                    const r = el.getBoundingClientRect();
                    const s = getComputedStyle(el);
                    return r.width > 0 && r.height > 0 && s.display !== 'none' && s.visibility !== 'hidden';
                };
                const visibleCount = (root, selector) => {
                    try {
                        return [...root.querySelectorAll(selector)].filter(visible).length;
                    } catch (_) {
                        return 0;
                    }
                };
                const strongItemSelectors = [
                    '[data-e2e="comment-item"]',
                    '[data-e2e*="comment-level"]',
                    '[data-e2e*="comment-text"]',
                    '[data-e2e*="comment-username"]',
                    '[class*="DivCommentObject"]',
                    '[data-cid]:not([data-cid^="one-column-item"])',
                    '[data-comment-id]',
                ].join(', ');
                const itemSelectors = [strongItemSelectors, 'p', 'li', '[role="listitem"]'].join(', ');
                const entries = [];
                for (const selector of selectors) {
                    let nodes = [];
                    try { nodes = [...document.querySelectorAll(selector)]; } catch (_) { nodes = []; }
                    for (const el of nodes) {
                        if (!visible(el)) continue;
                        const strongItems = visibleCount(el, strongItemSelectors);
                        const items = visibleCount(el, itemSelectors);
                        if (strictScope && strongItems <= 0) continue;
                        let strongList = false;
                        try {
                            strongList = el.matches(
                                '[data-e2e="comment-list"], [data-e2e="comment-list-container"], ' +
                                '[class*="CommentList"], [class*="DivCommentListContainer"]'
                            );
                        } catch (_) {}
                        if (!strongList && strongItems <= 0) continue;
                        const r = el.getBoundingClientRect();
                        const area = Math.max(0, r.width) * Math.max(0, r.height);
                        const score =
                            strongItems * 1000 +
                            items * 10 +
                            (strongList ? 100 : 0) -
                            Math.max(0, area - (window.innerWidth * window.innerHeight * 0.8)) / 10000;
                        entries.push({ el, selector, score, items, strongItems });
                    }
                }
                entries.sort((a, b) => b.score - a.score);
                const chosen = entries[0] || null;
                if (!chosen) return null;
                const badScrollTags = new Set([
                    'IMG', 'SVG', 'PATH', 'USE', 'PICTURE', 'SOURCE', 'CANVAS',
                    'SPAN', 'STRONG', 'P', 'A', 'BUTTON', 'I'
                ]);
                const isRealScrollContainer = (el) => {
                    if (!el || !visible(el) || badScrollTags.has(el.tagName)) return false;
                    const r = el.getBoundingClientRect();
                    if (r.width < 180 || r.height < 120) return false;
                    return el.scrollHeight > el.clientHeight + 20;
                };
                const scrollScore = (el) => {
                    if (!isRealScrollContainer(el)) return -1;
                    const r = el.getBoundingClientRect();
                    const strongItems = visibleCount(el, strongItemSelectors);
                    return strongItems * 1000 + Math.min(el.clientHeight || 0, 1200) + Math.min(r.width || 0, 800);
                };
                const scrollableDescendant = (el) => {
                    try {
                        const nodes = [...el.querySelectorAll('*')].filter(visible);
                        const candidates = nodes
                            .map((node) => ({ node, score: scrollScore(node) }))
                            .filter((item) => item.score >= 0 && visibleCount(item.node, strongItemSelectors) > 0)
                            .sort((a, b) => b.score - a.score);
                        return candidates.length ? candidates[0].node : null;
                    } catch (_) {
                        return null;
                    }
                };
                const scrollableAncestor = (el) => {
                    for (let cur = el; cur && cur !== document.body && cur !== document.documentElement; cur = cur.parentElement) {
                        if (!isRealScrollContainer(cur)) continue;
                        let style = null;
                        try { style = getComputedStyle(cur); } catch (_) { style = null; }
                        const overflow = style ? `${style.overflowY || ''} ${style.overflow || ''}` : '';
                        const overflowLooksScrollable = /(auto|scroll|overlay)/i.test(overflow);
                        if (overflowLooksScrollable || cur !== el) {
                            return cur;
                        }
                    }
                    return el;
                };
                const scopeEl = chosen.el;
                const openedScrollEl = (() => {
                    try {
                        const node = document.querySelector('[data-smart-comment-scroll-opened="1"]');
                        return node && (node === scopeEl || scopeEl.contains(node)) && isRealScrollContainer(node)
                            ? node
                            : null;
                    } catch (_) {
                        return null;
                    }
                })();
                let scrollEl = openedScrollEl || (isRealScrollContainer(chosen.el)
                    ? chosen.el
                    : (scrollableDescendant(chosen.el) || scrollableAncestor(chosen.el)));
                if (!isRealScrollContainer(scrollEl)) {
                    scrollEl = scopeEl;
                }
                try {
                    document
                        .querySelectorAll(
                            '[data-smart-id="comment-list-batch-scope"], ' +
                            '[data-smart-id="comment-list-batch-scroll"]'
                        )
                        .forEach((el) => el.removeAttribute('data-smart-id'));
                    document
                        .querySelectorAll(
                            '[data-smart-comment-list-batch-scope], ' +
                            '[data-smart-comment-list-batch-scroll]'
                        )
                        .forEach((el) => {
                            el.removeAttribute('data-smart-comment-list-batch-scope');
                            el.removeAttribute('data-smart-comment-list-batch-scroll');
                        });
                } catch (_) {}
                scopeEl.setAttribute('data-smart-comment-list-batch-scope', '1');
                scrollEl.setAttribute('data-smart-comment-list-batch-scroll', '1');
                return {
                    selector: chosen.selector,
                    scroll_container_tag: scrollEl.tagName,
                    scroll_container_class: String(scrollEl.className || '').slice(0, 160),
                    scroll_container_scrollable: isRealScrollContainer(scrollEl),
                    items: chosen.items,
                    strong_items: chosen.strongItems,
                    score: chosen.score,
                };
            }
            """,
            {"selectors": list_selectors, "strictScope": bool(strict_scope)},
        )
        if selected_list:
            candidate = await page.query_selector('[data-smart-comment-list-batch-scope="1"]')
            if candidate:
                comment_list = candidate
                scroll_target = (
                    await page.query_selector('[data-smart-comment-list-batch-scroll="1"]')
                ) or candidate
                matched_list_selector = str(selected_list.get("selector") or "")
                selected_scroll_container_scrollable = bool(
                    selected_list.get("scroll_container_scrollable")
                )
                selected_scroll_container_tag = str(
                    selected_list.get("scroll_container_tag") or ""
                ) or None
                try:
                    selected_scope_strong_items = int(selected_list.get("strong_items") or 0)
                except (TypeError, ValueError):
                    selected_scope_strong_items = 0
                try:
                    selected_scope_items = int(selected_list.get("items") or 0)
                except (TypeError, ValueError):
                    selected_scope_items = 0
                _log_batch_trace(
                    "scope_selected",
                    selector=matched_list_selector,
                    items=selected_list.get("items"),
                    strong_items=selected_list.get("strong_items"),
                    scroll_container_scrollable=selected_list.get("scroll_container_scrollable"),
                    scroll_container_tag=selected_list.get("scroll_container_tag"),
                    score=selected_list.get("score"),
                )
    except Exception as exc:
        logger.debug("[FIND_COMMENT_BATCH] scored comment-list selection failed: %s", exc)
        if is_browser_target_closed(exc):
            return {}, _empty_result_info(error="browser_target_closed", scrolls=0)

    for sel in ([] if comment_list is not None or strict_scope else list_selectors):
        try:
            candidate = await page.query_selector(sel)
            if candidate and await candidate.is_visible():
                comment_list = candidate
                scroll_target = candidate
                matched_list_selector = sel
                break
        except Exception:
            continue

    if not comment_list:
        if strict_scope:
            if not _stabilize_retry:
                try:
                    from executor.dom_actions import get_comment_surface_state, open_comment_section

                    surface_state = await get_comment_surface_state(page)
                    _log_batch_trace(
                        "comment_list_not_open_retry",
                        level=logging.INFO,
                        surface={
                            "input": bool(surface_state.get("input_visible")) if surface_state else False,
                            "comment_input": bool(surface_state.get("comment_input_visible")) if surface_state else False,
                            "list": bool(surface_state.get("list_visible")) if surface_state else False,
                            "items": int(surface_state.get("comment_items_visible") or 0) if surface_state else 0,
                            "list_source": surface_state.get("list_source") if surface_state else None,
                        },
                    )
                    await open_comment_section(
                        page,
                        max_wait=5.0,
                        require_input=False,
                        require_list_with_items=True,
                    )
                    await asyncio.sleep(max(1.0, scroll_delay_ms / 1000))
                    return await find_comments_in_list_batch(
                        page,
                        expected_texts,
                        expected_comment_ids=expected_comment_ids,
                        expected_authors=expected_authors,
                        require_author_match=require_author_match,
                        collect_author_fallbacks=collect_author_fallbacks,
                        max_scrolls=max_scrolls,
                        scroll_delay_ms=scroll_delay_ms,
                        allowed_text_selectors=allowed_text_selectors,
                        ensure_open=False,
                        strict_scope=strict_scope,
                        min_match_ratio=min_match_ratio,
                        return_partial_on_found=return_partial_on_found,
                        partial_found_sink=partial_found_sink,
                        progress_sink=progress_sink,
                        top_level_only=top_level_only,
                        _stabilize_retry=True,
                        phrase_match=phrase_match,
                        continue_from_current_position=continue_from_current_position,
                    )
                except Exception as exc:
                    _log_batch_trace(
                        "comment_list_not_open_retry_failed",
                        level=logging.INFO,
                        error=str(exc)[:120],
                    )
            _log_batch_trace("comment_list_not_open", level=logging.WARNING)
            return {}, _empty_result_info(
                error="comment_list_not_open",
                scrolls=0,
                list_selector_tried=list_selectors,
            )
        logger.debug("[FIND_COMMENT_BATCH] Comment list container not found, using document scope")

    scope_selector = None
    if scroll_target is None and comment_list is not None:
        scroll_target = comment_list
    defer_initial_scroll_reset = bool(return_partial_on_found and comment_list is not None)
    deferred_scroll_reset_pending = defer_initial_scroll_reset and not continue_from_current_position
    if comment_list is not None:
        scope_selector = '[data-smart-comment-list-batch-scope="1"]'
        if not defer_initial_scroll_reset and not continue_from_current_position:
            try:
                if scroll_target is not None:
                    await scroll_target.evaluate("(el) => { el.scrollTop = 0; }")
                await asyncio.sleep(0.2)
                _log_batch_trace("scope_scroll_reset")
            except Exception as exc:
                logger.debug("[FIND_COMMENT_BATCH] failed to reset scoped comment-list scroll: %s", exc)

    found: Dict[int, Dict[str, Any]] = {}
    used_comment_keys: set[str] = set()
    stagnant_scrolls = 0
    last_scroll_state: Dict[str, Any] | None = None
    completed_scrolls = 0
    max_seen_rows = 0
    last_row_count = 0
    last_row_samples: list[Dict[str, Any]] = []
    wheel_nudge_attempts = 0
    scan_failures = 0
    consecutive_scan_failures = 0
    seen_row_signatures: set[str] = set()
    last_new_row_count = 0
    stagnant_row_steps = 0
    scroll_exhausted = False
    scroll_stop_reason: Optional[str] = None
    partial_returned = False
    last_scroll_top: Optional[float] = None
    author_fallback_candidates: Dict[int, list[Dict[str, Any]]] = {}
    author_fallback_keys: Dict[int, set[str]] = {}
    hydration_retried_positions: set[Optional[float]] = set()
    rejected_match_keys: set[tuple[int, str, str]] = set()

    def _missing_target_indexes() -> list[int]:
        return [
            int(target["index"])
            for target in targets
            if int(target["index"]) not in found
        ]

    def _missing_target_previews(limit: int = 12) -> list[Dict[str, Any]]:
        previews: list[Dict[str, Any]] = []
        for target in targets:
            target_index = int(target["index"])
            if target_index in found:
                continue
            previews.append(
                {
                    "index": target_index,
                    "text_preview": str(target.get("text") or "")[:120],
                    "author": target.get("author"),
                    "comment_id": target.get("cid"),
                }
            )
            if len(previews) >= limit:
                break
        return previews

    def _result_info(**extra: Any) -> Dict[str, Any]:
        payload: Dict[str, Any] = {
            "found_count": len(found),
            "continued_from_current_position": continue_from_current_position,
            "missing_count": max(0, len(targets) - len(found)),
            "target_count": len(targets),
            "matched_indexes": sorted(int(index) for index in found.keys()),
            "missing_indexes": _missing_target_indexes(),
            "missing_previews": _missing_target_previews(),
            "scrolls": completed_scrolls,
            "max_seen_rows": max_seen_rows,
            "last_row_count": last_row_count,
            "unique_rows_seen": len(seen_row_signatures),
            "resume_scroll_top": last_scroll_top,
        }
        if selected_scroll_container_scrollable is not None:
            payload["scroll_container_scrollable"] = selected_scroll_container_scrollable
        if selected_scroll_container_tag:
            payload["scroll_container_tag"] = selected_scroll_container_tag
        if scroll_exhausted:
            payload["search_exhausted"] = True
        elif completed_scrolls >= max_scrolls and max_scrolls > 0 and not partial_returned:
            # Reaching the configured safety cap is not proof that the
            # virtualized TikTok list reached its end.  In live traces the
            # final capped scan was still hydrating several new rows, so
            # treating the cap as exhaustion suppressed the bounded fallback
            # and skipped valid parents farther down the list.
            payload["scroll_limit_reached"] = True
        if scroll_stop_reason:
            payload["scroll_stop_reason"] = scroll_stop_reason
        if partial_returned or (
            return_partial_on_found and bool(found) and len(found) < len(targets)
        ):
            payload["partial_returned"] = True
        payload.update(extra)
        if progress_sink is not None:
            progress_sink.clear()
            progress_sink.update(payload)
        return payload

    for scroll_i in range(max_scrolls):
        completed_scrolls = scroll_i + 1
        captcha_error = await _visible_captcha_error(page)
        if captcha_error:
            _log_batch_trace(
                "captcha_detected",
                level=logging.WARNING,
                scroll=scroll_i,
                error=captcha_error,
                found_count=len(found),
                target_count=len(targets),
                selector=matched_list_selector,
                max_seen_rows=max_seen_rows,
                unique_rows_seen=len(seen_row_signatures),
            )
            return found, _result_info(
                error=captcha_error,
                scrolls=completed_scrolls,
                matched_list_selector=matched_list_selector,
                max_seen_rows=max_seen_rows,
                last_row_count=last_row_count,
                unique_rows_seen=len(seen_row_signatures),
                author_fallback_candidates=author_fallback_candidates,
            )
        try:
            scan_token = _new_parent_anchor_token()
            scan = await page.evaluate(
                """
                ({rootSelector, textSelectors, scopeSelector, scrollSelector, scanToken, topLevelOnly}) => {
                    const visible = (el) => {
                        if (!el || !el.getBoundingClientRect) return false;
                        const r = el.getBoundingClientRect();
                        const s = getComputedStyle(el);
                        return r.width > 0 && r.height > 0 && s.display !== 'none' && s.visibility !== 'hidden';
                    };
                    const scope = scopeSelector ? document.querySelector(scopeSelector) : document;
                    if (!scope) return {rows: [], scrollTop: null, scopeMissing: true};
                    const scrollNode = scrollSelector ? document.querySelector(scrollSelector) : scope;
                    const scopeRect = scope.getBoundingClientRect ? scope.getBoundingClientRect() : null;
                    const scrollRect = scrollNode && scrollNode.getBoundingClientRect
                        ? scrollNode.getBoundingClientRect() : scopeRect;
                    const intersectsScope = (el) => {
                        if (!visible(el)) return false;
                        if (!scopeRect) return true;
                        const r = el.getBoundingClientRect();
                        const buffer = 80;
                        return r.bottom >= Math.max(0, scopeRect.top, scrollRect.top) - buffer &&
                            r.top <= Math.min(window.innerHeight, scopeRect.bottom, scrollRect.bottom) + buffer &&
                            r.right >= scopeRect.left &&
                            r.left <= scopeRect.right;
                    };
                    const isStrongCommentTextSelector = (sel) => {
                        const value = String(sel || '').toLowerCase();
                        return value.includes('data-e2e') &&
                            (value.includes('comment-level') || value.includes('comment-text'));
                    };
                    const labelOf = (el) => [
                        el && el.getAttribute && (el.getAttribute('data-e2e') || ''),
                        el && el.getAttribute && (el.getAttribute('aria-label') || ''),
                        el && (el.id || ''),
                        el && String(el.className || ''),
                    ].join(' ').toLowerCase();
                    const isReplyLevel = (el) => /comment-level-[23]/.test(labelOf(el));
                    const containerSelector = [
                        '[data-cid]:not([data-cid^="one-column-item"])',
                        '[data-comment-id]',
                        '[data-id]',
                        '[data-e2e="comment-item"]',
                        '[class*="DivCommentObject"]',
                        '[class*="CommentItem"]',
                        'article',
                        'li'
                    ].join(', ');
                    const cidSelector = '[data-cid], [data-comment-id], [id^="comment-"], [data-id]';
                    const isTopLevelCommentText = (container, node) => {
                        if (!topLevelOnly) return true;
                        if (!container || !node) return true;
                        if (isReplyLevel(container) || isReplyLevel(node)) return false;
                        for (let cur = container.parentElement; cur && cur !== scope && cur !== document.body; cur = cur.parentElement) {
                            const label = labelOf(cur);
                            if (isReplyLevel(cur) || label.includes('reply-container') ||
                                label.includes('replyitem') || label.includes('reply-item')) return false;
                        }
                        for (let cur = node.parentElement, depth = 0; cur && cur !== container && depth < 10; cur = cur.parentElement, depth++) {
                            const label = labelOf(cur);
                            if (isReplyLevel(cur)) return false;
                            if (label.includes('reply-container') || label.includes('replyitem') || label.includes('reply-item')) return false;
                            if (/(reply|Reply)/.test(String(cur.className || '')) && !label.includes('comment-reply')) return false;
                        }
                        const strongSelf = container.querySelector('[data-e2e="comment-level-1"], [data-e2e*="comment-text"]');
                        const firstLevel = container.querySelector('[data-e2e*="comment-level"]');
                        if (firstLevel && isReplyLevel(firstLevel) && !strongSelf) return false;
                        if (strongSelf && (strongSelf === node || strongSelf.contains(node) || node.contains(strongSelf))) return true;
                        try {
                            const cr = container.getBoundingClientRect();
                            const nr = node.getBoundingClientRect();
                            if (nr.left - cr.left > 96) return false;
                        } catch (_) {}
                        try {
                            for (let cur = container.parentElement, depth = 0; cur && cur !== scope && cur !== document.body && depth < 6; cur = cur.parentElement, depth++) {
                                if (cur.matches && cur.matches(containerSelector)) return false;
                            }
                        } catch (_) {}
                        return true;
                    };
                    const readCid = (el) => {
                        if (!el) return null;
                        const reliableRaw =
                            el.getAttribute('data-cid') ||
                            el.getAttribute('data-comment-id') ||
                            (el.id && el.id.startsWith('comment-') ? el.id : null) ||
                            null;
                        const dataId = el.getAttribute('data-id');
                        const raw = reliableRaw || (/^\\d{10,30}$/.test(String(dataId || '')) ? dataId : null);
                        if (!raw) return null;
                        return String(raw).replace(/^comment-/, '') || null;
                    };
                    const primaryRootSelector = [
                        '[data-cid]:not([data-cid^="one-column-item"])',
                        '[data-comment-id]',
                        '[data-e2e="comment-item"]',
                        '[class*="DivCommentObject"]',
                        '[class*="CommentItem"]',
                        '[role="listitem"]',
                        'article',
                        'li'
                    ].join(', ');
                    // Mixed TikTok layouts can contain both canonical rows and
                    // text-only wrappers. Limit the current viewport, not the
                    // first 250 loaded rows (which remain mounted above it).
                    const roots = [...scope.querySelectorAll(primaryRootSelector + ', ' + rootSelector)]
                        .filter(intersectsScope).slice(0, 250);
                    const rows = [];
                    const seenContainers = new Set();
                    for (let i = 0; i < roots.length; i++) {
                        const root = roots[i];
                        const closest = root.closest ? root.closest(containerSelector) : null;
                        const container = closest && scope.contains(closest) ? closest : root;
                        if (!intersectsScope(container)) continue;
                        if (seenContainers.has(container)) continue;
                        // TikTok exposes several nested wrappers for one visible
                        // top-level comment. The broad root selector can return
                        // both the outer row and an inner CommentItem wrapper.
                        // Treating both as separate rows maps one physical
                        // comment to several duplicate-text targets. Document
                        // order puts the outer wrapper first, so retain it and
                        // reject later nested aliases during parent-only scans.
                        const nestedContainerAlreadySeen = topLevelOnly && [...seenContainers].some(
                            (existing) => existing.contains(container) || container.contains(existing)
                        );
                        if (nestedContainerAlreadySeen) continue;
                        // A generic CommentItem/list wrapper can contain several
                        // sibling parents. It must not claim the whole subtree as
                        // one row and hide every parent after the first.
                        const ownParentTexts = [...container.querySelectorAll('[data-e2e="comment-level-1"]')]
                            .filter(node => visible(node) && isTopLevelCommentText(container, node) &&
                                !node.querySelector('[data-e2e="comment-level-1"]'));
                        if (topLevelOnly && ownParentTexts.length > 1) continue;
                        const structurallyCommentLike = (() => {
                            try {
                                return container.matches(containerSelector) ||
                                    !!container.querySelector('[data-e2e*="comment-level"], [data-e2e*="comment-text"], [data-cid]:not([data-cid^="one-column-item"]), [data-comment-id]');
                            } catch (_) {
                                return false;
                            }
                        })();
                        let text = '';
                        let textNode = null;
                        let selector = null;
                        for (const sel of textSelectors) {
                            let nodes = [];
                            try { nodes = container.matches(sel) ? [container] : [...container.querySelectorAll(sel)]; } catch (_) {}
                            for (const node of nodes) {
                                if (!intersectsScope(node)) continue;
                                try {
                                    if (
                                        !isStrongCommentTextSelector(sel) &&
                                        node.closest('button, [role="button"], a[href*="/@"]')
                                    ) continue;
                                } catch (_) {}
                                if (!isTopLevelCommentText(container, node)) continue;
                                if ((sel === 'p' || sel === 'span') && !structurallyCommentLike) continue;
                                text = (node.innerText || node.textContent || '').trim().replace(/\\s+/g, ' ');
                                if (text) {
                                    textNode = node;
                                    selector = sel;
                                    break;
                                }
                            }
                            if (text) break;
                        }
                        if (!text) continue;
                        seenContainers.add(container);
                        const authorCandidates = [];
                        const pushAuthor = (value) => {
                            const raw = String(value || '').trim().replace(/^@/, '');
                            if (!raw) return;
                            const key = raw.toLowerCase();
                            if (!authorCandidates.some((item) => String(item || '').toLowerCase() === key)) {
                                authorCandidates.push(raw);
                            }
                        };
                        let author = null;
                        const usernameMarker = (() => {
                            try { return container.querySelector('[data-e2e="comment-username-1"]'); } catch (_) { return null; }
                        })();
                        const preferredAuthorLinks = [];
                        if (usernameMarker) {
                            try {
                                if (usernameMarker.matches('a[href*="/@"]')) preferredAuthorLinks.push(usernameMarker);
                                const nested = usernameMarker.querySelector('a[href*="/@"]');
                                if (nested) preferredAuthorLinks.push(nested);
                            } catch (_) {}
                        }
                        try {
                            const explicit = container.querySelector('a[data-e2e="comment-username-1"][href*="/@"]');
                            if (explicit) preferredAuthorLinks.push(explicit);
                            for (const link of container.querySelectorAll('a[href*="/@"]')) {
                                if (!visible(link)) continue;
                                if (textNode && (textNode === link || textNode.contains(link))) continue;
                                if (link.closest('[data-e2e*="comment-level"], [data-e2e*="comment-text"]')) continue;
                                preferredAuthorLinks.push(link);
                            }
                        } catch (_) {}
                        for (const authorLink of preferredAuthorLinks) {
                            const href = authorLink.getAttribute ? (authorLink.getAttribute('href') || '') : '';
                            const m = href.match(/\\/@([^/?#]+)/);
                            if (!m || !m[1]) continue;
                            author = m[1];
                            pushAuthor(author);
                            break;
                        }
                        if (usernameMarker) {
                            pushAuthor((usernameMarker.innerText || usernameMarker.textContent || '').trim());
                        }
                        if (!author && authorCandidates.length) author = authorCandidates[0];
                        const idHost = (() => {
                            try {
                                const selfOrAncestor = container.matches(cidSelector) ? container : container.closest(cidSelector);
                                if (selfOrAncestor && scope.contains(selfOrAncestor)) return selfOrAncestor;
                            } catch (_) {}
                            // A reply may be the only descendant carrying a cid.
                            // Only accept a descendant id on the parent's text
                            // ancestry, never an arbitrary nested reply's id.
                            try {
                                const textHost = textNode && textNode.closest(cidSelector);
                                return textHost && container.contains(textHost) ? textHost : null;
                            } catch (_) { return null; }
                        })();
                        let anchorToken = null;
                        try { anchorToken = container.getAttribute('data-smart-parent-anchor') || null; } catch (_) { anchorToken = null; }
                        if (!anchorToken && scanToken) {
                            anchorToken = `${scanToken}-${rows.length}`;
                        }
                        if (anchorToken) {
                            try { container.setAttribute('data-smart-parent-anchor', anchorToken); } catch (_) {}
                        }
                        rows.push({
                            position: i,
                            text,
                            selector,
                            cid: readCid(idHost),
                            author,
                            authorCandidates,
                            scrollTop: scrollNode && typeof scrollNode.scrollTop === 'number' ? scrollNode.scrollTop : null,
                            anchorToken,
                        });
                    }
                    return {
                        rows,
                        scrollTop: scrollNode && typeof scrollNode.scrollTop === 'number' ? scrollNode.scrollTop : null,
                        scopeMissing: false,
                    };
                }
                """,
                {
                    "rootSelector": comment_root_selector,
                    "textSelectors": comment_text_selectors,
                    "scopeSelector": scope_selector,
                    "scrollSelector": (
                        '[data-smart-comment-list-batch-scroll="1"]'
                        if scroll_target is not None
                        else None
                    ),
                    "scanToken": scan_token,
                    "topLevelOnly": bool(top_level_only),
                },
            )
        except Exception as exc:
            logger.debug("[FIND_COMMENT_BATCH] scan failed: %s", exc)
            scan_failures += 1
            consecutive_scan_failures += 1
            # A lost driver/target cannot produce another DOM snapshot. Do not
            # count failed reads as scrolls or claim the comment is absent.
            if is_browser_target_closed(exc) or consecutive_scan_failures >= 3:
                error = "browser_target_closed" if is_browser_target_closed(exc) else "scan_failed"
                _log_batch_trace(
                    "scan_failed", level=logging.WARNING,
                    error=error, scan_failures=scan_failures,
                    detail=str(exc)[:160],
                )
                return found, _result_info(
                    error=error, scan_failures=scan_failures,
                    scrolls=scroll_i, max_seen_rows=max_seen_rows,
                    last_row_count=last_row_count,
                    unique_rows_seen=len(seen_row_signatures),
                    search_exhausted=False, scroll_limit_reached=False,
                )
            await asyncio.sleep(0.1)
            continue

        consecutive_scan_failures = 0
        if (scan or {}).get("scopeMissing") and strict_scope:
            _log_batch_trace("comment_list_not_open_during_scan", level=logging.WARNING, scroll=scroll_i)
            return found, _result_info(
                error="comment_list_not_open",
                scrolls=completed_scrolls,
            )

        rows = (scan or {}).get("rows") or []
        raw_scroll_top = (scan or {}).get("scrollTop")
        try:
            last_scroll_top = float(raw_scroll_top) if raw_scroll_top is not None else last_scroll_top
        except (TypeError, ValueError):
            pass
        last_row_count = len(rows)
        max_seen_rows = max(max_seen_rows, last_row_count)
        current_row_signatures: set[str] = set()
        for row in rows:
            signature = _comment_row_signature(
                row.get("text"),
                cid=row.get("cid"),
                author=row.get("author"),
            )
            if signature:
                current_row_signatures.add(signature)
        new_row_signatures = current_row_signatures - seen_row_signatures
        last_new_row_count = len(new_row_signatures)
        if current_row_signatures:
            if new_row_signatures:
                stagnant_row_steps = 0
            else:
                stagnant_row_steps += 1
            seen_row_signatures.update(current_row_signatures)
        # ``asyncio.wait_for`` cancels this coroutine at its wall-clock limit.
        # Keep a mutable snapshot after every completed DOM scan so the wrapper
        # can still account for a real search (and resume it) after cancellation.
        _result_info()
        last_row_samples = [
            {
                "text": normalize_text(str(row.get("text") or ""))[:90],
                "selector": row.get("selector"),
                "cid": row.get("cid"),
                "author": row.get("author"),
                "author_candidates": row.get("authorCandidates") or row.get("author_candidates"),
            }
            for row in rows[:5]
        ]
        matching_author_not_ready = False

        def _record_rejected_match(target: Dict[str, Any], row: Dict[str, Any], reason: str) -> None:
            key = (int(target["index"]), str(row.get("cid") or row.get("anchorToken") or row.get("text")), reason)
            if key in rejected_match_keys or len(rejected_match_keys) >= 12:
                return
            rejected_match_keys.add(key)
            _log_batch_trace(
                "text_match_rejected", reason=reason, matched_index=target["index"],
                text_preview=str(row.get("text") or "")[:100],
                expected_author=target.get("author"), observed_author=row.get("author"),
                expected_comment_id=target.get("cid"), observed_comment_id=row.get("cid"),
            )

        for row in rows:
            raw_text = str(row.get("text") or "")
            if not raw_text:
                continue
            row_cid = str(row.get("cid") or "").strip() or None
            row_key = row_cid or (
                f"{normalize_text(raw_text)}|{str(row.get('author') or '').casefold()}|"
                f"pos:{row.get('position')}|top:{row.get('scrollTop')}"
            )
            if row_key in used_comment_keys:
                continue

            matched_target: Optional[Dict[str, Any]] = None
            matched_by = "text"
            if row_cid:
                for target in targets:
                    if target["index"] in found:
                        continue
                    if target.get("cid") and str(target["cid"]) == row_cid:
                        matched_target = target
                        matched_by = "id"
                        break
            if matched_target is None:
                for target in targets:
                    if target["index"] in found:
                        continue
                    if not is_comment_match(str(target["text"]), raw_text, min_ratio=min_match_ratio, phrase_match=phrase_match):
                        continue
                    if target.get("cid") and row_cid and str(target["cid"]) != row_cid:
                        _record_rejected_match(target, row, "comment_id_mismatch")
                        continue
                    expected_author = str(target.get("author") or "").strip()
                    row_author = normalize_author(row.get("author"))
                    if require_author_match and expected_author:
                        if expected_author == row_author:
                            matched_target = target
                            matched_by = "text_author"
                            break
                        matching_author_not_ready = matching_author_not_ready or not row_author
                        _record_rejected_match(target, row, "author_mismatch" if row_author else "author_missing")
                        if collect_author_fallbacks and normalize_text(str(target["text"])) == normalize_text(raw_text):
                            target_index = int(target["index"])
                            seen_keys = author_fallback_keys.setdefault(target_index, set())
                            if row_key not in seen_keys:
                                seen_keys.add(row_key)
                                author_fallback_candidates.setdefault(target_index, []).append({
                                    "position": row.get("position"),
                                    "scroll": scroll_i,
                                    "scroll_top": row.get("scrollTop", (scan or {}).get("scrollTop")),
                                    "text_normalized": normalize_text(raw_text),
                                    "raw_text": raw_text,
                                    "comment_id": row_cid,
                                    "text_selector": row.get("selector"),
                                    "author": row.get("author"),
                                    "author_candidates": row.get("authorCandidates") or row.get("author_candidates"),
                                    "anchor_token": row.get("anchorToken"),
                                    "matched_by": "text_author_fallback",
                                    "fallback_reason": "author_missing" if not row_author else "author_mismatch",
                                })
                        continue
                    matched_target = target
                    matched_by = "text"
                    break
            if matched_target is None:
                continue

            target_index = int(matched_target["index"])
            used_comment_keys.add(row_key)
            found[target_index] = {
                "position": row.get("position"),
                "scroll": scroll_i,
                "scroll_top": row.get("scrollTop", (scan or {}).get("scrollTop")),
                "text_normalized": normalize_text(raw_text),
                "raw_text": raw_text,
                "comment_id": row_cid,
                "text_selector": row.get("selector"),
                "author": row.get("author"),
                "author_candidates": row.get("authorCandidates") or row.get("author_candidates"),
                "anchor_token": row.get("anchorToken"),
                "matched_by": matched_by,
            }
            if partial_found_sink is not None:
                partial_found_sink[target_index] = dict(found[target_index])
            _log_batch_trace(
                "found",
                scroll=scroll_i,
                position=row.get("position"),
                matched_index=target_index,
                matched_by=matched_by,
                comment_id=row_cid,
                matched_preview=normalize_text(raw_text)[:120],
            )
            if len(found) >= len(targets):
                break

        if len(found) >= len(targets):
            break
        if return_partial_on_found and found:
            partial_returned = True
            _log_batch_trace(
                "partial_found_return",
                scroll=scroll_i,
                found_count=len(found),
                target_count=len(targets),
                missing_indexes=_missing_target_indexes(),
            )
            break
        viewport_top = (scan or {}).get("scrollTop")
        if (
            strict_scope and scroll_i + 1 < max_scrolls
            and (not rows or not last_new_row_count or matching_author_not_ready)
            and viewport_top not in hydration_retried_positions
        ):
            # Virtualized rows and author links can mount after the scroll
            # settles. Re-read this viewport once before moving past it.
            hydration_retried_positions.add(viewport_top)
            _log_batch_trace("viewport_hydration_retry", scroll=scroll_i, scroll_top=viewport_top)
            await asyncio.sleep(min(0.8, max(0.5, scroll_delay_ms / 1000)))
            continue
        if (
            deferred_scroll_reset_pending and not found and scroll_target is not None
        ):
            try:
                await scroll_target.evaluate("(el) => { el.scrollTop = 0; }")
                deferred_scroll_reset_pending = False
                await asyncio.sleep(0.2)
                _log_batch_trace("scope_scroll_reset_after_current_viewport_miss")
                continue
            except Exception as exc:
                logger.debug("[FIND_COMMENT_BATCH] deferred scoped reset failed: %s", exc)

        try:
            if scroll_target:
                before_state = await scroll_target.evaluate(
                    "(el) => ({top: el.scrollTop, height: el.scrollHeight, client: el.clientHeight})"
                )
                await scroll_target.evaluate("(el) => { el.scrollTop += el.clientHeight * 0.9; }")
                await asyncio.sleep(min(0.25, max(0.05, scroll_delay_ms / 2000)))
                after_state = await scroll_target.evaluate(
                    "(el) => ({top: el.scrollTop, height: el.scrollHeight, client: el.clientHeight})"
                )
                moved = after_state.get("top") != before_state.get("top")
                grew = (
                    last_scroll_state is not None
                    and after_state.get("height") != last_scroll_state.get("height")
                )
                can_scroll = (after_state.get("height") or 0) > (after_state.get("client") or 0) + 5
                row_progress = bool(last_new_row_count > 0 and scroll_i > 0)
                stagnant_scrolls = 0 if (moved or grew or row_progress) else stagnant_scrolls + 1
                last_scroll_state = after_state
                stagnant_limit = 4 if strict_scope else 2
                scroll_stalled = (not can_scroll and not row_progress) or stagnant_scrolls >= stagnant_limit
                if scroll_stalled:
                    nudge_limit = 3 if not can_scroll else 2
                    if strict_scope and wheel_nudge_attempts < nudge_limit:
                        wheel_nudge_attempts += 1
                        nudge_target = scroll_target if scroll_target is not None else comment_list
                        nudge = await _nudge_comment_list_scroll(
                            nudge_target,
                            delta=max(800, int((after_state.get("client") or 0) * 1.2)),
                        )
                        _log_batch_trace(
                            "scope_wheel_nudge",
                            scroll=scroll_i,
                            reason="no_scroll_progress_before_stop",
                            scroll_state=after_state,
                            nudge=nudge,
                            nudge_attempt=wheel_nudge_attempts,
                            unique_rows_seen=len(seen_row_signatures),
                            new_rows_last_scan=last_new_row_count,
                            stagnant_row_steps=stagnant_row_steps,
                        )
                        await asyncio.sleep(scroll_delay_ms / 1000)
                        continue
                    scroll_exhausted = True
                    scroll_stop_reason = "no_comment_list_scroll_progress"
                    _log_batch_trace(
                        "scroll_stopped",
                        scroll=scroll_i,
                        reason="no_comment_list_scroll_progress",
                        scroll_state=after_state,
                        unique_rows_seen=len(seen_row_signatures),
                        new_rows_last_scan=last_new_row_count,
                        stagnant_row_steps=stagnant_row_steps,
                    )
                    break
            else:
                await page.mouse.wheel(0, 800)
        except Exception as exc:
            logger.debug("[FIND_COMMENT_BATCH] scroll failed: %s", exc)
            if is_browser_target_closed(exc):
                return found, _result_info(
                    error="browser_target_closed", scrolls=completed_scrolls,
                    max_seen_rows=max_seen_rows, last_row_count=last_row_count,
                    unique_rows_seen=len(seen_row_signatures),
                    search_exhausted=False, scroll_limit_reached=False,
                )

        await asyncio.sleep(scroll_delay_ms / 1000)

    level = logging.INFO if found else logging.WARNING
    if (
        strict_scope
        and not found
        and max_seen_rows == 0
        and selected_scope_strong_items > 0
        and scan_failures == 0
    ):
        _log_batch_trace(
            "surface_parse_empty",
            level=logging.WARNING,
            found_count=0,
            target_count=len(targets),
            scrolls=completed_scrolls,
            selector=matched_list_selector,
            selected_items=selected_scope_items,
            selected_strong_items=selected_scope_strong_items,
            last_row_count=last_row_count,
            unique_rows_seen=len(seen_row_signatures),
            stagnant_row_steps=stagnant_row_steps,
        )
        return found, _result_info(
            error="comment_surface_parse_empty",
            scrolls=completed_scrolls,
            matched_list_selector=matched_list_selector,
            selected_items=selected_scope_items,
            selected_strong_items=selected_scope_strong_items,
            max_seen_rows=max_seen_rows,
            last_row_count=last_row_count,
            unique_rows_seen=len(seen_row_signatures),
            author_fallback_candidates=author_fallback_candidates,
        )
    if not found and max_seen_rows == 0 and scan_failures > 0:
        _log_batch_trace(
            "scan_failed",
            level=logging.WARNING,
            scan_failures=scan_failures,
            scrolls=completed_scrolls,
            selector=matched_list_selector,
        )
        return found, _result_info(
            error="scan_failed",
            scrolls=completed_scrolls,
            matched_list_selector=matched_list_selector,
            scan_failures=scan_failures,
            author_fallback_candidates=author_fallback_candidates,
        )
    scroll_limit_reached = bool(
        completed_scrolls >= max_scrolls
        and max_scrolls > 0
        and not scroll_exhausted
        and not partial_returned
        and len(found) < len(targets)
    )
    _log_batch_trace(
        "complete",
        level=level,
        found_count=len(found),
        missing_count=max(0, len(targets) - len(found)),
        scrolls=completed_scrolls,
        selector=matched_list_selector,
        max_seen_rows=max_seen_rows,
        last_row_count=last_row_count,
        unique_rows_seen=len(seen_row_signatures),
        new_rows_last_scan=last_new_row_count,
        stagnant_row_steps=stagnant_row_steps,
        scroll_limit_reached=scroll_limit_reached or None,
        last_row_samples=last_row_samples if not found else None,
        scan_failures=scan_failures if scan_failures else None,
        author_fallback_count=sum(len(items) for items in author_fallback_candidates.values()) or None,
    )
    return found, _result_info(
        error="scroll_limit_reached" if scroll_limit_reached else None,
        scrolls=completed_scrolls,
        matched_list_selector=matched_list_selector,
        max_seen_rows=max_seen_rows,
        last_row_count=last_row_count,
        unique_rows_seen=len(seen_row_signatures),
        new_rows_last_scan=last_new_row_count,
        stagnant_row_steps=stagnant_row_steps,
        last_row_samples=last_row_samples,
        author_fallback_candidates=author_fallback_candidates,
    )


def classify_outcome(
    network_ok: bool,
    local_ok: Optional[bool],
    public_ok: Optional[bool],
    toast_text: Optional[str],
    response_status: Optional[int] = None
) -> str:
    """
    Классифицирует исход комментирования (рекомендации ChatGPT).
    
    Args:
        network_ok: Сетевой запрос успешен (200-299)
        local_ok: Комментарий виден автору
        public_ok: Комментарий виден другому аккаунту
        toast_text: Текст ошибки из UI (если есть)
        response_status: HTTP статус ответа сервера
        
    Returns:
        str: Классификация результата:
            - SUCCESS_PUBLIC: виден наблюдателю
            - SUCCESS_LOCAL_ONLY: виден только автору (фильтрация/модерация)
            - SUBMITTED_UNCONFIRMED: сетевой успех есть, но не найден (задержка)
            - FAILED_TO_POST: сетевой отказ/тост/ошибка
            - FAILED_UI_TOAST: ошибка в UI (тост)
            - UNKNOWN: неизвестный исход
    """
    # Приоритет 1: Тосты с ошибками - явная ошибка
    if toast_text:
        return "FAILED_UI_TOAST"
    
    # Приоритет 2: Сетевой отказ
    if not network_ok:
        if response_status and 400 <= response_status < 500:
            return "FAILED_TO_POST"  # Клиентская ошибка
        elif response_status and 500 <= response_status < 600:
            return "FAILED_TO_POST"  # Серверная ошибка
        return "FAILED_TO_POST"
    
    # Приоритет 3: Публичная видимость - самый надёжный индикатор
    if public_ok is True:
        return "SUCCESS_PUBLIC"
    
    # Приоритет 4: Локальная видимость без публичной - фильтрация/модерация
    if local_ok is True and public_ok is False:
        return "SUCCESS_LOCAL_ONLY"
    
    # Приоритет 5: Сетевой успех, но не виден - возможна задержка модерации
    if network_ok and local_ok is False:
        return "SUBMITTED_UNCONFIRMED"
    
    # Приоритет 6: Сетевой успех, но проверка не проводилась
    if network_ok and local_ok is None:
        return "SUBMITTED_UNCONFIRMED"
    
    # Приоритет 7: Локальная видимость, но публичная проверка не проводилась
    if local_ok is True and public_ok is None:
        return "SUBMITTED_UNCONFIRMED"  # Нельзя точно сказать
    
    return "UNKNOWN"
