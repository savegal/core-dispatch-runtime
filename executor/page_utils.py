"""
Утилиты для работы со страницами Playwright.

ОПТИМИЗИРОВАНО: Умное закрытие страниц - переиспользование через browser_pool.
"""
import logging
from typing import Optional
from patchright.async_api import Page

logger = logging.getLogger("executor.page_utils")


async def safe_close_page(page: Optional[Page]) -> None:
    """
    ИСПРАВЛЕНО: Умное закрытие страницы - переиспользование через browser_pool.
    
    Если страница управляется browser_pool (_managed_by_pool=True), возвращает её в пул
    вместо закрытия. Это позволяет переиспользовать страницы и экономит ресурсы.
    
    Args:
        page: Страница для закрытия/возврата в пул
    """
    if not page:
        return
    
    try:
        # Проверяем, управляется ли страница пулом
        if hasattr(page, '_managed_by_pool') and page._managed_by_pool:
            # Страница из пула - возвращаем в пул вместо закрытия
            from executor.browser_pool import get_browser_pool
            try:
                pool = await get_browser_pool()
                await pool.return_page_to_pool(page)
                logger.debug(f"Returned page to pool (account_id={getattr(page, '_pool_account_id', 'unknown')})")
            except Exception as e:
                logger.debug(f"Error returning page to pool: {e}, closing page instead")
                # Если возврат в пул не удался - закрываем страницу
                try:
                    await page.close()
                except Exception:
                    pass
        else:
            # Страница не из пула - закрываем её
            await page.close()
    except Exception as e:
        logger.debug(f"Error in safe_close_page: {e}")
