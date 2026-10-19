"""
ИСПРАВЛЕНО: Улучшенная логика верификации комментариев (рекомендации ChatGPT).

Модуль содержит функции для:
1. Однократной проверки видимости комментария (verify_comment_visible)
2. Повторных попыток с таймаутом (verify_with_retries)
3. Локальной проверки после публикации (verify_comment_by_author)
4. Публичной проверки комментария (verify_comment_public_visibility)

ИСПРАВЛЕНО: Использует функции из comment_outcome.py для нормализации и сравнения текста.
"""
import asyncio
import logging
import time
from typing import Optional, Dict, Any
from db.models import Account
from config.settings import get_settings
from executor.tiktok_client_async import create_page_for_account, TikTokClientError
from executor.comment_outcome import (
    normalize_text,
    is_comment_match,
    find_comment_in_list
)
from services.metrics import record_timing
from executor.tiktok.network_client import fetch_comment_list
from executor.tiktok.session_artifacts import collect_session_artifacts

logger = logging.getLogger("executor.comment_verification")
settings = get_settings()


def compare_texts(text1: str, text2: str, strict: bool = False) -> bool:
    """
    Сравнивает два текста с учетом нормализации (использует comment_outcome).
    
    Args:
        text1: Первый текст
        text2: Второй текст
        strict: Если True - точное совпадение, если False - допускается обрезка
        
    Returns:
        bool: True если тексты совпадают
    """
    if strict:
        return normalize_text(text1) == normalize_text(text2)
    else:
        # Используем улучшенную функцию с допуском по длине
        return is_comment_match(text1, text2, min_ratio=0.7)


def _author_matches(expected_author: str | None, actual_author: str | None) -> bool:
    """Checks author match allowing @ prefix and case/space variations."""
    if not expected_author:
        return True
    if not actual_author:
        return False

    expected_norm = normalize_text(expected_author).lstrip("@")
    actual_norm = normalize_text(actual_author).lstrip("@")
    if not expected_norm or not actual_norm:
        return False
    return expected_norm == actual_norm


async def verify_comment_visible(
    account: Account, 
    video_url: str, 
    comment_text: str, 
    task_id: int | None = None,
    expected_author: str | None = None,
    attempt: int = 1
) -> tuple[bool, dict]:
    """
    ИСПРАВЛЕНО: Упрощенная версия проверки видимости комментария (рекомендации ChatGPT).
    
    Убрана лишняя сложность, код более читаемый и поддерживаемый.
    
    Args:
        account: Аккаунт из БД
        video_url: URL видео
        comment_text: Текст комментария для поиска
        task_id: Опциональный ID задачи (для логирования)
        expected_author: Опциональное имя автора комментария (для более точного поиска)
        attempt: Номер попытки (для логирования)
        
    Returns:
        tuple[bool, dict]: (найден ли комментарий, информация о комментарии)
            dict содержит: {
                'found': bool,
                'position': int | None,
                'comment_id': str | None,
                'author': str | None,
                'text_normalized': str
            }
    """
    task_info = f"task_id={task_id}, " if task_id else ""
    logger.debug(
        f"[VERIFY] Attempt {attempt}: {task_info}account_id={account.id}, "
        f"label={account.label[:20]}, video_url={video_url[:50]}"
    )
    
    info: dict = {
        'found': False,
        'position': None,
        'comment_id': None,
        'author': None,
        'text_normalized': normalize_text(comment_text)
    }
    
    page = None
    verify_start = time.monotonic()
    
    try:
        context, page = await create_page_for_account(account)
        timeout = settings.PLAYWRIGHT_TIMEOUT * 2 if account.proxy else settings.PLAYWRIGHT_TIMEOUT
        await page.goto(
            video_url,
            wait_until="load" if account.proxy else "domcontentloaded",
            timeout=timeout
        )
        await page.wait_for_timeout(1500)
        
        # HYBRID: network-first verify (stub, needs endpoints)
        if settings.TIKTOK_NETWORK_FIRST and settings.TIKTOK_NETWORK_IMPL_READY:
            try:
                artifacts = await collect_session_artifacts(page)
                net_result = await fetch_comment_list(page, video_url, artifacts)
                if isinstance(net_result, dict):
                    # TODO: map comment_id/author/text when endpoints known
                    pass
            except Exception:
                pass

        target = normalize_text(comment_text)
        author_target = normalize_text(expected_author) if expected_author else None
        
        # ИСПРАВЛЕНО: Добавлена диагностика для engagement_only заказов
        try:
            from executor.page_diagnostics import log_page_diagnostics
            await log_page_diagnostics(page, "before_comment_search")
        except Exception as diag_error:
            logger.debug(f"Pre-search diagnostics failed: {diag_error}")
        
        # ИСПРАВЛЕНО: Используем улучшенную функцию find_comment_in_list
        found, find_info = await find_comment_in_list(
            page=page,
            expected_text=comment_text,
            expected_author=expected_author,
            require_author_match=bool(expected_author),
            max_scrolls=10,
            scroll_delay_ms=600
        )
        
        # ИСПРАВЛЕНО: Если комментарий не найден - логируем диагностику
        if not found:
            try:
                from executor.page_diagnostics import log_page_diagnostics
                await log_page_diagnostics(page, "comment_not_found")
            except Exception as diag_error:
                logger.debug(f"Post-search diagnostics failed: {diag_error}")
        
        if found:
            info['found'] = True
            info['position'] = find_info.get('position')
            info['comment_id'] = find_info.get('comment_id')
            info['author'] = find_info.get('author')
            info['text_normalized'] = find_info.get('text_normalized', target)
            
            # ОБНОВЛЕНО: Извлекаем ID и автора из найденного комментария (Jan 2026)
            if find_info.get('position') is not None:
                try:
                    from executor.tiktok_selectors import selectors
                    # Используем корневые контейнеры комментариев
                    comment_roots = await page.query_selector_all(selectors.COMMENT_ROOT)
                    position = find_info.get('position', 0)
                    
                    if 0 <= position < len(comment_roots):
                        comment_root = comment_roots[position]
                        
                        # Извлекаем ID комментария из корневого контейнера
                        if not info.get('comment_id'):
                            comment_id = await comment_root.get_attribute("data-cid") or \
                                        await comment_root.get_attribute("data-id") or \
                                        await comment_root.get_attribute("data-comment-id") or \
                                        await comment_root.get_attribute("id")
                            if comment_id:
                                info['comment_id'] = str(comment_id)
                        
                        # Извлекаем автора (если нужен)
                        if (author_target or expected_author) and not info.get('author'):
                            try:
                                info['author'] = await comment_root.evaluate(r"""
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
                                """) or None
                            except Exception:
                                pass
                except Exception as e:
                    logger.debug(f"[VERIFY] Error extracting metadata: {e}")

            if expected_author and info.get('author') and not _author_matches(expected_author, info.get('author')):
                info['found'] = False
                info['author_mismatch'] = True
                logger.warning(
                    f"[VERIFY] Author mismatch: expected={expected_author}, "
                    f"actual={info.get('author')}, task_id={task_id}, account_id={account.id}"
                )
                return False, info
            if expected_author and not info.get('author'):
                info['found'] = False
                info['author_missing'] = True
                logger.warning(
                    f"[VERIFY] Author not readable (DOM extraction failed), rejecting text-only match: "
                    f"expected={expected_author}, task_id={task_id}, account_id={account.id}"
                )
                return False, info
             
            logger.debug(
                f"[VERIFY] Comment found: {task_info}account_id={account.id}, "
                f"position={info.get('position')}, comment_id={info.get('comment_id')}"
            )
        
        logger.info(
            f"[VERIFY] Result (attempt {attempt}): {task_info}account_id={account.id}, "
            f"found={info['found']}, position={info['position']}"
        )
        
        return info['found'], info
        
    except Exception as e:
        error_msg = str(e)
        logger.warning(
            f"[VERIFY] Error (attempt {attempt}): {task_info}account_id={account.id}, "
            f"error={error_msg[:200]}"
        )
        return False, info
        
    finally:
        try:
            duration_ms = (time.monotonic() - verify_start) * 1000
            record_timing("tiktok.verify_ms", duration_ms, tags={"proxy": str(bool(account.proxy))})
        except Exception:
            pass
        # ОПТИМИЗИРОВАНО: Используем умное закрытие страницы - переиспользование через browser_pool
        if page:
            try:
                await asyncio.sleep(0.1)
                from executor.page_utils import safe_close_page
                await safe_close_page(page)
            except Exception as e:
                logger.debug(f"[VERIFY] Error closing page: {e}")


async def verify_comment_by_author(
    account: Account,
    video_url: str,
    comment_text: str,
    task_id: int | None = None
) -> tuple[bool, dict]:
    """
    ИСПРАВЛЕНО: Локальная проверка - автор видит свой комментарий (рекомендации ChatGPT).
    
    Вызывается сразу после post_comment тем же аккаунтом.
    Ждёт 2-5 секунд и ищет комментарий на странице.
    
    ИСПРАВЛЕНО: Использует улучшенную функцию find_comment_in_list,
    которая скроллит контейнер комментариев, а не страницу.
    
    Args:
        account: Аккаунт-автор комментария
        video_url: URL видео
        comment_text: Текст комментария
        task_id: Опциональный ID задачи
        
    Returns:
        tuple[bool, dict]: (найден ли комментарий, информация о комментарии)
            dict содержит дополнительные поля:
                - verification_attempts: количество попыток проверки
                - moderation_timeout: True если проверка не удалась из-за таймаута
    """
    # Ждём появления комментария (2-5 секунд) - рекомендации ChatGPT
    wait_time = settings.COMMENT_LOCAL_VERIFY_DELAY
    await asyncio.sleep(wait_time)
    
    # Делаем однократную проверку с улучшенной логикой
    found, info = await verify_comment_visible(
        account=account,
        video_url=video_url,
        comment_text=comment_text,
        task_id=task_id,
        attempt=0  # Локальная проверка
    )
    
    # Добавляем метаданные для анализа
    if not found:
        info['verification_attempts'] = 1
        info['moderation_timeout'] = False
    else:
        info['verification_attempts'] = 1
        info['moderation_timeout'] = False
    
    return found, info


async def verify_comment_public_visibility(
    observer_account: Account,
    video_url: str,
    comment_text: str,
    expected_author: str | None = None,
    task_id: int | None = None,
    timeout: int | None = None,
    interval: int | None = None
) -> tuple[bool, dict]:
    """
    ИСПРАВЛЕНО: Публичная проверка видимости комментария (рекомендации ChatGPT).
    
    Проверяет видимость комментария с точки зрения другого аккаунта (наблюдателя).
    Это критично для диагностики теневого бана: если автор видит, а наблюдатель нет -
    это признак фильтрации/модерации.
    
    ВАЖНО: Вызывайте эту функцию только если local_verify = True (иначе бессмысленно).
    
    Args:
        observer_account: Аккаунт-наблюдатель (другой аккаунт, не автор)
        video_url: URL видео
        comment_text: Текст комментария для поиска
        expected_author: Опциональное имя автора комментария (для более точного поиска)
        task_id: Опциональный ID задачи
        timeout: Максимальное время ожидания (секунды, по умолчанию из settings)
        interval: Интервал между проверками (секунды, по умолчанию из settings)
        
    Returns:
        tuple[bool, dict]: (виден ли комментарий наблюдателю, информация о комментарии)
    """
    task_info = f"task_id={task_id}, " if task_id else ""
    logger.info(
        f"[VERIFY_PUBLIC] Starting public visibility check: {task_info}observer_account_id={observer_account.id}, "
        f"video_url={video_url[:50]}, expected_author={expected_author or 'any'}"
    )
    
    # Используем verify_with_retries для публичной проверки
    found, info = await verify_with_retries(
        verify_account=observer_account,
        video_url=video_url,
        main_comment_text=comment_text,
        task_id=task_id,
        timeout=timeout,
        interval=interval,
        expected_author=expected_author
    )
    
    if found:
        logger.info(
            f"[VERIFY_PUBLIC] Comment is PUBLICLY VISIBLE: {task_info}observer_account_id={observer_account.id}, "
            f"position={info.get('position')}, comment_id={info.get('comment_id')}"
        )
    else:
        logger.warning(
            f"[VERIFY_PUBLIC] Comment is NOT publicly visible (may indicate shadow ban/filtering): "
            f"{task_info}observer_account_id={observer_account.id}, "
            f"verification_attempts={info.get('verification_attempts', 'unknown')}"
        )
    
    return found, info


async def verify_with_retries(
    verify_account: Account,
    video_url: str,
    main_comment_text: str,
    task_id: int | None = None,
    timeout: int | None = None,
    interval: int | None = None,
    expected_author: str | None = None,
    expected_comment_id: str | None = None,
    verify_mode: str = "standard",
    max_scrolls: int = 10,
    scroll_delay_ms: int = 600,
    require_author_match: bool = False,
    text_match_ratio: float = 0.7,
) -> tuple[bool, dict]:
    """
    ОПТИМИЗИРОВАНО: Повторные попытки верификации комментария с таймаутом.
    
    ИСПРАВЛЕНО: Создает страницу один раз и переиспользует её для всех попыток,
    вместо создания новой страницы при каждом retry. Это значительно ускоряет верификацию.
    
    Проверяет видимость комментария каждые N секунд до истечения таймаута.
    Это учитывает модерацию TikTok - комментарий может появиться с задержкой.
    
    Args:
        verify_account: Аккаунт для проверки (другой аккаунт, не автор)
        video_url: URL видео
        main_comment_text: Текст комментария
        task_id: Опциональный ID задачи
        timeout: Максимальное время ожидания (секунды, по умолчанию из settings)
        interval: Интервал между проверками (секунды, по умолчанию из settings)
        expected_author: Опциональное имя автора комментария
        
    Returns:
        tuple[bool, dict]: (найден ли комментарий, информация о комментарии)
            Если комментарий не найден за время таймаута, возвращает False и пустой dict
    """
    timeout_seconds = timeout or settings.COMMENT_APPEARANCE_TIMEOUT
    check_interval = interval or settings.COMMENT_VERIFY_CHECK_INTERVAL
    
    deadline = time.monotonic() + timeout_seconds
    attempt = 1
    
    task_info = f"task_id={task_id}, " if task_id else ""
    logger.info(
        f"[VERIFY_RETRIES] Starting: {task_info}account_id={verify_account.id}, "
        f"timeout={timeout_seconds}s, interval={check_interval}s, "
        f"mode={verify_mode}, max_scrolls={max_scrolls}, scroll_delay_ms={scroll_delay_ms}"
    )
    
    # ОПТИМИЗИРОВАНО: Создаем страницу один раз, переиспользуем для всех попыток
    page = None
    context = None
    
    try:
        context, page = await create_page_for_account(verify_account)
        timeout_ms = settings.PLAYWRIGHT_TIMEOUT * 2 if verify_account.proxy else settings.PLAYWRIGHT_TIMEOUT
        
        # Навигация к видео (один раз)
        await page.goto(
            video_url,
            wait_until="load" if verify_account.proxy else "domcontentloaded",
            timeout=timeout_ms
        )
        await page.wait_for_timeout(1500)
        
        target = normalize_text(main_comment_text)
        author_target = normalize_text(expected_author) if expected_author else None
        expected_comment_id_norm = str(expected_comment_id).strip() if expected_comment_id else None
        
        # Цикл проверок на одной странице
        while time.monotonic() < deadline:
            # ИСПРАВЛЕНО: Используем улучшенную функцию find_comment_in_list
            found, find_info = await find_comment_in_list(
                page=page,
                expected_text=main_comment_text,
                expected_author=expected_author,
                require_author_match=bool(require_author_match and expected_author),
                max_scrolls=max_scrolls,
                scroll_delay_ms=scroll_delay_ms,
                min_match_ratio=text_match_ratio,
            )
            
            info: dict = {
                'found': found,
                'position': find_info.get('position') if found else None,
                'comment_id': find_info.get('comment_id') if found else None,
                'author': find_info.get('author') if found else None,
                'text_normalized': target
            }
            
            if found:
                # Извлекаем ID и автора из найденного комментария
                if find_info.get('position') is not None:
                    try:
                        from executor.tiktok_selectors import selectors
                        comment_roots = await page.query_selector_all(selectors.COMMENT_ROOT)
                        position = find_info.get('position', 0)

                        if 0 <= position < len(comment_roots):
                            comment_root = comment_roots[position]

                            # Извлекаем ID комментария (если не пришел из find_info)
                            if not info.get("comment_id"):
                                comment_id = await comment_root.get_attribute("data-cid") or \
                                            await comment_root.get_attribute("data-id") or \
                                            await comment_root.get_attribute("data-comment-id") or \
                                            await comment_root.get_attribute("id")
                                if comment_id:
                                    info['comment_id'] = str(comment_id)

                            # Извлекаем автора (если нужен)
                            if not info.get("author"):
                                try:
                                    author_elem = await comment_root.query_selector(
                                        'a[href*="/@"], [class*="username"], [class*="author"], [data-e2e*="username"]'
                                    )
                                    if author_elem:
                                        author_text = await author_elem.evaluate("el => (el.innerText || el.textContent || '')")
                                        info['author'] = author_text.strip() or None
                                except Exception:
                                    pass
                    except Exception as e:
                        logger.debug(f"[VERIFY_RETRIES] Error extracting metadata: {e}")

                # Строгая проверка comment_id: если ожидаемый ID задан, успешная верификация
                # возможна только при точном совпадении.
                if expected_comment_id_norm:
                    actual_comment_id = str(info.get("comment_id") or "").strip()
                    if not actual_comment_id:
                        info["found"] = False
                        info["comment_id_missing"] = True
                        logger.warning(
                            f"[VERIFY_RETRIES] comment_id missing for strict verification: "
                            f"expected={expected_comment_id_norm[:40]}, task_id={task_id}, account_id={verify_account.id}"
                        )
                    elif actual_comment_id != expected_comment_id_norm:
                        info["found"] = False
                        info["comment_id_mismatch"] = True
                        logger.warning(
                            f"[VERIFY_RETRIES] comment_id mismatch: expected={expected_comment_id_norm[:40]}, "
                            f"actual={actual_comment_id[:40]}, task_id={task_id}, account_id={verify_account.id}"
                        )
                
                if info.get("found") and expected_author and info.get('author') and not _author_matches(expected_author, info.get('author')):
                    info['found'] = False
                    info['author_mismatch'] = True
                    logger.warning(
                        f"[VERIFY_RETRIES] Author mismatch: expected={expected_author}, "
                        f"actual={info.get('author')}, task_id={task_id}, account_id={verify_account.id}"
                    )
                elif info.get("found") and expected_author and not info.get('author'):
                    if require_author_match:
                        info['found'] = False
                        info['author_missing'] = True
                        logger.warning(
                            f"[VERIFY_RETRIES] Author not readable (DOM), rejecting text-only match: "
                            f"expected={expected_author}, task_id={task_id}, account_id={verify_account.id}"
                        )
                    else:
                        logger.warning(
                            f"[VERIFY_RETRIES] Author not readable (DOM), accepting by text: "
                            f"expected={expected_author}, task_id={task_id}, account_id={verify_account.id}"
                        )

                if info.get("found"):
                    logger.info(
                        f"[VERIFY_RETRIES] Success (attempt {attempt}): {task_info}account_id={verify_account.id}, "
                        f"mode={verify_mode}, position={info.get('position')}, "
                        f"comment_id={info.get('comment_id')}"
                    )
                    return True, info
            
            # Проверяем, не истёк ли таймаут
            if time.monotonic() >= deadline:
                break
            
            # Ждём перед следующей попыткой
            remaining_time = deadline - time.monotonic()
            sleep_time = min(check_interval, remaining_time)
            
            if sleep_time > 0:
                logger.debug(
                    f"[VERIFY_RETRIES] Attempt {attempt} failed, waiting {sleep_time:.1f}s before next check"
                )
                await asyncio.sleep(sleep_time)
            
            # ОПТИМИЗИРОВАНО: Перезагружаем страницу для получения свежих комментариев
            # (вместо создания новой страницы)
            try:
                await page.reload(wait_until="domcontentloaded", timeout=timeout_ms)
                await page.wait_for_timeout(1000)  # Небольшая задержка для загрузки комментариев
            except Exception as reload_error:
                logger.debug(f"[VERIFY_RETRIES] Reload error (attempt {attempt}): {reload_error}")
                # Если reload не удался, продолжаем на той же странице
            
            attempt += 1
        
        # Таймаут истёк, комментарий не найден
        logger.warning(
            f"[VERIFY_RETRIES] Timeout: {task_info}account_id={verify_account.id}, "
            f"mode={verify_mode}, comment not found after {attempt} attempts ({timeout_seconds}s)"
        )
        
        return False, {
            'found': False,
            'position': None,
            'comment_id': None,
            'author': None,
            'text_normalized': target,
            'verification_attempts': attempt,
            'moderation_timeout': True
        }
    
    except Exception as e:
        error_msg = str(e)
        logger.warning(
            f"[VERIFY_RETRIES] Error: {task_info}account_id={verify_account.id}, "
            f"mode={verify_mode}, error={error_msg[:200]}"
        )
        return False, {
            'found': False,
            'position': None,
            'comment_id': None,
            'author': None,
            'text_normalized': normalize_text(main_comment_text),
            'verification_attempts': attempt,
            'moderation_timeout': False,
            'error': error_msg
        }
    
    finally:
        try:
            duration_ms = (timeout_seconds - max(0, deadline - time.monotonic())) * 1000
            record_timing(
                "tiktok.verify_retries_ms",
                duration_ms,
                tags={
                    "proxy": str(bool(verify_account.proxy)),
                    "mode": verify_mode,
                    "strict_comment_id": str(bool(expected_comment_id)),
                }
            )
        except Exception:
            pass
        # ОПТИМИЗИРОВАНО: Закрываем страницу один раз в конце
        if page:
            try:
                await asyncio.sleep(0.1)
                await page.close()
            except Exception as e:
                logger.debug(f"[VERIFY_RETRIES] Error closing page: {e}")
