"""
Context manager для безопасного управления browser resources.
Предотвращает утечки ресурсов даже при исключениях.
"""
from contextlib import asynccontextmanager
from typing import Optional, Tuple
from patchright.async_api import BrowserContext, Page
from db.models import Account
from executor.browser_pool import get_browser_pool
import logging

logger = logging.getLogger("executor.browser_context_manager")


@asynccontextmanager
async def browser_context_manager(account: Account):
    """
    Context manager для безопасного управления browser context и page.
    
    Гарантирует закрытие page и освобождение ресурсов даже при исключениях.
    
    Usage:
        async with browser_context_manager(account) as (context, page):
            # Используем page
            await page.goto(url)
            # page автоматически закроется при выходе из блока
    """
    context: Optional[BrowserContext] = None
    page: Optional[Page] = None
    use_pool = False
    
    try:
        # Получаем context и page через pool
        pool = await get_browser_pool()
        context, page = await pool.get_page_for_account(account)
        use_pool = True
        logger.debug(f"[CONTEXT_MANAGER] Acquired context and page for account {account.id}")
        
        yield context, page
        
    except Exception as e:
        logger.error(f"[CONTEXT_MANAGER] Error in browser context manager for account {account.id}: {e}")
        raise
    finally:
        # Возвращаем страницу в пул, если она управляется BrowserPool.
        if page:
            try:
                from executor.page_utils import safe_close_page
                await safe_close_page(page)
                logger.debug(f"[CONTEXT_MANAGER] Released page for account {account.id}")
            except Exception as e:
                logger.warning(f"[CONTEXT_MANAGER] Error closing page for account {account.id}: {e}")
        
        # Context управляется pool'ом, не закрываем его здесь
        # Pool сам управляет жизненным циклом contexts



