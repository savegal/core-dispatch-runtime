"""
Утилиты для комментирования: логирование, диагностика, валидация.
Объединены в один модуль для скорости и удобства.
"""
import json
import logging
from typing import Dict, Any, Optional
from patchright.async_api import Page

logger = logging.getLogger("executor.comment_utils")


def log_step(step_name: str, details: Optional[Dict[str, Any]] = None, level: int = 1):
    """
    Быстрое логирование критичных шагов.
    
    Args:
        step_name: Название шага
        details: Дополнительные детали (опционально)
        level: Уровень логирования (1=INFO, 2=DEBUG)
    """
    if details:
        # Быстрое форматирование (без json.dumps если не нужно)
        details_str = ", ".join(f"{k}={v}" for k, v in details.items() if v is not None)
        if details_str:
            message = f"[STEP] {step_name}: {details_str}"
        else:
            message = f"[STEP] {step_name}"
    else:
        message = f"[STEP] {step_name}"
    
    if level == 1:
        logger.info(message)
    else:
        logger.debug(message)


async def quick_diagnose(page: Page, stage: str, on_error: bool = True) -> Dict[str, Any]:
    """
    Быстрая диагностика состояния страницы (только при ошибках для экономии времени).
    
    Args:
        page: Страница Playwright
        stage: Этап процесса
        on_error: True если диагностика при ошибке (более детальная)
        
    Returns:
        dict: Диагностическая информация
    """
    if not page or page.is_closed():
        return {
            "stage": stage,
            "error": "Page is closed",
            "url": None,
            "title": None,
        }
    
    try:
        # Базовая диагностика (быстро)
        url = page.url
        try:
            title = await page.title() if not page.is_closed() else "N/A"
        except Exception:
            title = "N/A"
        
        result = {
            "stage": stage,
            "url": url[:100] if url else None,
            "title": title[:50] if title else None,
        }
        
        # Детальная диагностика только при ошибках
        if on_error:
            try:
                # Параллельные проверки (быстро)
                dom_info = await page.evaluate("""
                    () => {
                        return {
                            readyState: document.readyState,
                            textareaCount: document.querySelectorAll('textarea').length,
                            contentEditableCount: document.querySelectorAll('[contenteditable="true"]').length,
                            commentSectionCount: document.querySelectorAll('[data-e2e="comment-list"], [class*="CommentList"]').length,
                            bodyHeight: document.body.scrollHeight,
                            windowHeight: window.innerHeight,
                        };
                    }
                """)
                result.update(dom_info)
            except Exception as e:
                result["dom_error"] = str(e)[:100]
        
        return result
    except Exception as e:
        return {
            "stage": stage,
            "error": f"Diagnosis failed: {str(e)[:100]}",
        }


async def quick_validate_element(element, element_name: str = "element") -> tuple[bool, Optional[str]]:
    """
    Быстрая валидация элемента (только критичные проверки).
    
    Args:
        element: Элемент Playwright
        element_name: Название элемента для логирования
        
    Returns:
        tuple: (is_valid, error_message)
    """
    if not element:
        return False, f"{element_name} is None"
    
    try:
        # Быстрая проверка видимости
        is_visible = await element.is_visible()
        if not is_visible:
            return False, f"{element_name} is not visible"
        
        # Быстрая проверка интерактивности (только для критичных элементов)
        is_disabled = await element.evaluate("el => el.disabled || el.readOnly")
        if is_disabled:
            return False, f"{element_name} is disabled or readonly"
        
        return True, None
    except Exception as e:
        return False, f"{element_name} validation error: {str(e)[:100]}"


async def check_page_alive(page: Page) -> tuple[bool, Optional[str]]:
    """
    Быстрая проверка, что страница еще открыта и жива.
    КРИТИЧНО: Предотвращает TargetClosedError.
    
    Args:
        page: Страница Playwright
        
    Returns:
        tuple: (is_alive, error_message)
    """
    if not page:
        return False, "Page is None"
    
    try:
        # ИСПРАВЛЕНО: Проверяем закрытие страницы через try-except, так как is_closed() может выбросить исключение
        try:
            if page.is_closed():
                return False, "Page is closed"
        except Exception:
            # Если is_closed() выбросил исключение, страница точно закрыта
            return False, "Page is closed (exception on is_closed)"
        
        # ИСПРАВЛЕНО: Проверяем контекст через url, так как evaluate может не работать если контекст закрыт
        try:
            # Пробуем получить URL - это более надежная проверка
            # Используем короткий таймаут, чтобы не блокировать
            url = page.url
            # Дополнительная проверка: если URL пустой или невалидный, страница может быть закрыта
            if not url or url == "about:blank":
                return False, "Page URL is blank or invalid"
            return True, None
        except Exception as url_error:
            # Если не можем получить URL, контекст закрыт
            error_str = str(url_error).lower()
            if "target" in error_str or "closed" in error_str:
                return False, "Page context is dead (target closed)"
            return False, f"Page context is dead: {str(url_error)[:50]}"
    except Exception as e:
        return False, f"Page check failed: {str(e)[:100]}"


async def safe_wait_for_selector(page: Page, selector: str, timeout: int = 5000, state: str = "visible") -> tuple[Optional[Any], Optional[str]]:
    """
    Безопасное ожидание селектора с проверкой состояния страницы.
    Предотвращает TargetClosedError.
    
    Args:
        page: Страница Playwright
        selector: CSS селектор
        timeout: Таймаут в миллисекундах
        state: Состояние элемента (visible, attached, hidden)
        
    Returns:
        tuple: (element, error_message)
    """
    # Проверяем, что страница жива перед ожиданием
    is_alive, error = await check_page_alive(page)
    if not is_alive:
        return None, error
    
    try:
        element = await page.wait_for_selector(selector, timeout=timeout, state=state)
        
        # Проверяем еще раз после получения элемента
        is_alive, error = await check_page_alive(page)
        if not is_alive:
            return None, error
        
        return element, None
    except Exception as e:
        error_msg = str(e)
        if "Target page, context or browser has been closed" in error_msg:
            return None, "Page/context/browser was closed during wait"
        if "timeout" in error_msg.lower():
            return None, f"Timeout waiting for selector: {selector[:50]}"
        return None, f"Error waiting for selector: {error_msg[:100]}"


async def safe_query_selector(page: Page, selector: str) -> tuple[Optional[Any], Optional[str]]:
    """
    Безопасный поиск селектора с проверкой состояния страницы.
    
    Args:
        page: Страница Playwright
        selector: CSS селектор
        
    Returns:
        tuple: (element, error_message)
    """
    is_alive, error = await check_page_alive(page)
    if not is_alive:
        return None, error
    
    try:
        element = await page.query_selector(selector)
        return element, None
    except Exception as e:
        return None, f"Error querying selector: {str(e)[:100]}"


async def safe_query_selector_all(page: Page, selector: str) -> tuple[list, Optional[str]]:
    """
    Безопасный поиск всех элементов по селектору.
    
    Args:
        page: Страница Playwright
        selector: CSS селектор
        
    Returns:
        tuple: (elements_list, error_message)
    """
    is_alive, error = await check_page_alive(page)
    if not is_alive:
        return [], error
    
    try:
        elements = await page.query_selector_all(selector)
        return elements, None
    except Exception as e:
        return [], f"Error querying all selectors: {str(e)[:100]}"
