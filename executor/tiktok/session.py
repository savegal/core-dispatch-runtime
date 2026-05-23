"""
TikTok Session - context manager для безопасного управления страницами и контекстами.

Устраняет дублирование кода и гарантирует правильное закрытие ресурсов.
"""
import asyncio
import logging
from contextlib import asynccontextmanager
from typing import Optional, Tuple
from patchright.async_api import BrowserContext, Page
from db.models import Account
from executor.browser_pool import get_browser_pool

logger = logging.getLogger("executor.tiktok.session")


class TikTokSession:
    """
    Context manager для работы с TikTok через Playwright.
    
    Автоматически управляет жизненным циклом страницы и контекста.
    
    Usage:
        async with TikTokSession(account, task_id) as session:
            await session.page.goto(url)
            # page автоматически закроется при выходе
    """
    
    def __init__(self, account: Account, task_id: Optional[int] = None):
        """
        Args:
            account: Аккаунт из БД
            task_id: Опциональный ID задачи (для логирования)
        """
        self.account = account
        self.task_id = task_id
        self._context: Optional[BrowserContext] = None
        self._page: Optional[Page] = None
        self._browser_pool = None
        
        task_info = f"task_id={task_id}, " if task_id else ""
        self._log_prefix = f"[SESSION] {task_info}account_id={account.id}, label={account.label[:20] if account.label else 'N/A'}"
    
    async def __aenter__(self) -> 'TikTokSession':
        """Вход в context manager - создаёт страницу."""
        try:
            self._browser_pool = await get_browser_pool()
            # Используем browser pool напрямую
            self._context, self._page = await self._browser_pool.get_page_for_account(self.account)
            logger.debug(f"{self._log_prefix} - Session created")
            return self
        except Exception as e:
            logger.error(f"{self._log_prefix} - Failed to create session: {e}")
            raise
    
    async def __aexit__(self, exc_type, exc_val, exc_tb):
        """Выход из context manager - закрывает страницу."""
        if self._page:
            try:
                if not self._page.is_closed():
                    await asyncio.sleep(0.1)
                    await self._page.close()
                    logger.debug(f"{self._log_prefix} - Page closed")
            except Exception as e:
                logger.debug(f"{self._log_prefix} - Error closing page: {e}")
            finally:
                self._page = None
        
        # НЕ закрываем context - он управляется BrowserPool
        self._context = None
    
    @property
    def page(self) -> Page:
        """Возвращает текущую страницу."""
        if self._page is None:
            raise RuntimeError("Session is not active. Use 'async with TikTokSession(...)'")
        return self._page
    
    @property
    def context(self) -> BrowserContext:
        """Возвращает текущий контекст."""
        if self._context is None:
            raise RuntimeError("Session is not active. Use 'async with TikTokSession(...)'")
        return self._context
    
    def log_step(self, step_name: str, status: str = "info", message: str = ""):
        """Логирует шаг операции."""
        log_func = getattr(logger, status, logger.info)
        log_message = f"{self._log_prefix} [{step_name}] {message}"
        log_func(log_message)
    
    async def save_diagnostic(self, error_type: str) -> Tuple[Optional[str], Optional[str]]:
        """
        Сохраняет диагностическую информацию (скриншот + HTML).
        
        Args:
            error_type: Тип ошибки (для имени файла)
            
        Returns:
            tuple: (screenshot_path, html_path) или (None, None) при ошибке
        """
        from executor.tiktok.diagnostics import save_diagnostic
        return await save_diagnostic(self.page, self.account, self.task_id, error_type)

