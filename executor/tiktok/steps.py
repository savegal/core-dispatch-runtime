"""
Step-обёртка для унификации обработки шагов операции.

Фиксирует длительность, метрики, делает ретраи и автоматически сохраняет диагностику при ошибках.
"""
import asyncio
import time
import logging
from typing import Callable, Optional, Any, Dict
from functools import wraps
from patchright.async_api import Page
from executor.comment_result import CommentResult
from executor.error_handler import classify_error, ErrorCategory
from executor.tiktok.diagnostics import DiagnosticCollector, save_diagnostic
from executor.adaptive_delays import get_adaptive_delays
from db.models import Account

logger = logging.getLogger("executor.tiktok.steps")
adaptive_delays = get_adaptive_delays()


async def step(
    step_name: str,
    operation: Callable,
    *args,
    account: Optional[Account] = None,
    task_id: Optional[int] = None,
    page: Optional[Page] = None,
    diagnostic_collector: Optional[DiagnosticCollector] = None,
    max_retries: int = 1,
    timeout_ms: Optional[int] = None,
    using_proxy: bool = False,
    **kwargs
) -> Any:
    """
    Универсальная обёртка для выполнения шагов операции.
    
    Автоматически:
    - Фиксирует длительность шага
    - Делает ретраи при ошибках
    - Сохраняет диагностику при ошибках
    - Записывает метрики для adaptive_delays
    - Возвращает CommentResult.error() по карте ошибок
    
    Args:
        step_name: Название шага (для логирования и метрик)
        operation: Асинхронная функция для выполнения
        *args, **kwargs: Аргументы для operation
        account: Аккаунт (для диагностики)
        task_id: ID задачи (для диагностики)
        page: Страница (для диагностики)
        diagnostic_collector: Коллектор диагностики (для таймлайна)
        max_retries: Максимальное количество ретраев
        timeout_ms: Таймаут операции в миллисекундах
        using_proxy: Используется ли прокси (для адаптивных задержек)
        
    Returns:
        Результат выполнения operation или CommentResult.error() при ошибке
    """
    task_info = f"task_id={task_id}, " if task_id else ""
    log_prefix = f"[STEP] {task_info}{step_name}"
    
    last_error = None
    
    for attempt in range(max_retries):
        try:
            start_time = time.time()
            
            logger.debug(f"{log_prefix} - Starting (attempt {attempt + 1}/{max_retries})")
            
            # Выполняем операцию с таймаутом если указан
            if timeout_ms:
                result = await asyncio.wait_for(
                    operation(*args, **kwargs),
                    timeout=timeout_ms / 1000.0
                )
            else:
                result = await operation(*args, **kwargs)
            
            duration_ms = (time.time() - start_time) * 1000
            
            # Записываем успешный шаг
            if diagnostic_collector:
                diagnostic_collector.record_step(step_name, "ok", duration_ms)
            
            # Записываем метрики для adaptive_delays
            adaptive_delays.record_operation(step_name, True, duration_ms)
            
            logger.debug(f"{log_prefix} - Completed successfully in {duration_ms:.1f}ms")
            
            return result
            
        except asyncio.TimeoutError:
            duration_ms = (time.time() - start_time) * 1000 if 'start_time' in locals() else timeout_ms or 0
            error_msg = f"Timeout after {duration_ms:.1f}ms"
            last_error = error_msg
            
            if diagnostic_collector:
                diagnostic_collector.record_step(step_name, "error", duration_ms)
            
            adaptive_delays.record_operation(step_name, False, duration_ms)
            
            logger.warning(f"{log_prefix} - {error_msg} (attempt {attempt + 1}/{max_retries})")
            
            if attempt < max_retries - 1:
                # Задержка перед ретраем
                retry_delay = adaptive_delays.get_delay(f"{step_name}_retry", using_proxy=using_proxy)
                await asyncio.sleep(retry_delay / 1000)
                continue
            else:
                # Сохраняем диагностику при финальной ошибке
                if page and account:
                    await save_diagnostic(page, account, task_id, f"{step_name}_timeout")
                
                # Классифицируем ошибку
                error_classification = classify_error(error_msg, using_proxy)
                return CommentResult.error(
                    error_type=error_classification.category.value,
                    details=f"{step_name} timeout: {error_msg}",
                    fatal_for_account=error_classification.fatal_for_account,
                    fatal_for_proxy=error_classification.fatal_for_proxy
                )
            
        except Exception as e:
            duration_ms = (time.time() - start_time) * 1000 if 'start_time' in locals() else 0
            error_msg = str(e)
            last_error = error_msg
            
            if diagnostic_collector:
                diagnostic_collector.record_step(step_name, "error", duration_ms)
            
            adaptive_delays.record_operation(step_name, False, duration_ms)
            
            logger.warning(f"{log_prefix} - Error: {error_msg[:200]} (attempt {attempt + 1}/{max_retries})")
            
            # Если это TikTokClientError - не ретраим
            from executor.tiktok_client_async import TikTokClientError
            if isinstance(e, TikTokClientError):
                # Сохраняем диагностику
                if page and account:
                    await save_diagnostic(page, account, task_id, f"{step_name}_client_error")
                
                error_classification = classify_error(error_msg, using_proxy)
                return CommentResult.error(
                    error_type=error_classification.category.value,
                    details=f"{step_name} error: {error_msg[:200]}",
                    fatal_for_account=error_classification.fatal_for_account,
                    fatal_for_proxy=error_classification.fatal_for_proxy
                )
            
            if attempt < max_retries - 1:
                # Задержка перед ретраем
                retry_delay = adaptive_delays.get_delay(f"{step_name}_retry", using_proxy=using_proxy)
                await asyncio.sleep(retry_delay / 1000)
                continue
            else:
                # Сохраняем диагностику при финальной ошибке
                if page and account:
                    await save_diagnostic(page, account, task_id, f"{step_name}_error")
                
                # Классифицируем ошибку
                error_classification = classify_error(error_msg, using_proxy)
                return CommentResult.error(
                    error_type=error_classification.category.value,
                    details=f"{step_name} error: {error_msg[:200]}",
                    fatal_for_account=error_classification.fatal_for_account,
                    fatal_for_proxy=error_classification.fatal_for_proxy
                )
    
    # Должно быть недостижимо, но на всякий случай
    if page and account:
        await save_diagnostic(page, account, task_id, f"{step_name}_failed")
    
    error_classification = classify_error(str(last_error) if last_error else "Unknown error", using_proxy)
    return CommentResult.error(
        error_type=error_classification.category.value,
        details=f"{step_name} failed after {max_retries} attempts",
        fatal_for_account=error_classification.fatal_for_account,
        fatal_for_proxy=error_classification.fatal_for_proxy
    )


def step_decorator(
    step_name: str,
    max_retries: int = 1,
    timeout_ms: Optional[int] = None
):
    """
    Декоратор для автоматической обёртки функции в step().
    
    Usage:
        @step_decorator("navigate_to_video", max_retries=2, timeout_ms=10000)
        async def navigate_to_video(page, url):
            await page.goto(url)
    """
    def decorator(func: Callable):
        @wraps(func)
        async def wrapper(*args, **kwargs):
            # Извлекаем account, task_id, page из kwargs или args
            account = kwargs.get('account') or (args[0] if args and hasattr(args[0], 'id') else None)
            task_id = kwargs.get('task_id')
            page = kwargs.get('page') or (args[0] if args and hasattr(args[0], 'goto') else None)
            using_proxy = kwargs.get('using_proxy', False)
            
            return await step(
                step_name,
                func,
                *args,
                account=account,
                task_id=task_id,
                page=page,
                max_retries=max_retries,
                timeout_ms=timeout_ms,
                using_proxy=using_proxy,
                **kwargs
            )
        return wrapper
    return decorator

