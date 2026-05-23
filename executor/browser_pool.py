"""
Browser pool для переиспользования браузеров Playwright.
Критично для производительности - создание браузеров очень медленное.
"""
import asyncio
import contextlib
import contextvars
import hashlib
import logging
import threading
from typing import Optional, Dict, Tuple
from datetime import datetime, timezone, timedelta
from patchright.async_api import async_playwright, Playwright, Browser, BrowserContext, Page
from db.models import Account
from config.settings import get_settings
from executor.tiktok_client import TikTokClientError
from executor.socks5_to_http_proxy import (
    cleanup_idle_local_http_proxies,
    get_local_http_proxy_for_socks5,
    release_local_http_proxy_for_socks5,
)
from services.metrics import set_gauge

logger = logging.getLogger("executor.browser_pool")

_current_task_id: contextvars.ContextVar[int | None] = contextvars.ContextVar(
    "browser_pool_current_task_id",
    default=None,
)


@contextlib.contextmanager
def browser_pool_task_scope(task_id: int | None):
    """Tags pages created inside this scope with the current task id."""
    token = _current_task_id.set(int(task_id) if task_id is not None else None)
    try:
        yield
    finally:
        _current_task_id.reset(token)


_TIKTOK_SHORTCUTS_SUPPRESS_INIT_SCRIPT = r"""
(() => {
    const hasMarker = (txt) => {
        txt = String(txt || '');
        if (txt.includes('Introducing keyboard shortcuts')) return true;
        return txt.includes('Go to previous video')
            && txt.includes('Go to next video')
            && txt.includes('Mute / unmute video')
            && txt.includes('Play / Pause');
    };

    const kill = (el) => {
        if (!el || el === document.body || el === document.documentElement) return 0;
        try {
            el.setAttribute('data-teletan-init-killed-shortcuts', '1');
            el.style.setProperty('pointer-events', 'none', 'important');
            el.style.setProperty('visibility', 'hidden', 'important');
            el.style.setProperty('opacity', '0', 'important');
            el.style.setProperty('display', 'none', 'important');
            el.style.setProperty('z-index', '-1', 'important');
            try { el.remove(); } catch (_) {}
            return 1;
        } catch (_) { return 0; }
    };

    const boundedOverlay = (el) => {
        if (!el || !el.getBoundingClientRect) return false;
        const r = el.getBoundingClientRect();
        if (!(r.width > 120 && r.height > 80)) return false;
        if (r.width > window.innerWidth * 0.95 || r.height > window.innerHeight * 0.95) return false;
        if (r.x > window.innerWidth * 0.35 && r.y > window.innerHeight * 0.15) return true;
        const st = getComputedStyle(el);
        const z = parseInt(st.zIndex || '0', 10);
        return st.position === 'fixed' || st.position === 'absolute' || z > 10;
    };

    const setSeen = () => {
        try {
            const keys = [
                'tiktok_keyboard_shortcuts_seen',
                'keyboard_shortcuts_seen',
                'keyboardShortcutsSeen',
                'keyboard_shortcuts_modal_seen',
                'video_keyboard_shortcuts_seen',
                'shortcut_guide_seen',
                'shortcutGuideSeen',
                'tiktok_shortcuts_guide_seen',
                'webapp_keyboard_shortcuts_seen',
            ];
            for (const k of keys) {
                localStorage.setItem(k, '1');
                sessionStorage.setItem(k, '1');
            }
        } catch (_) {}
    };

    const killOnce = () => {
        setSeen();
        let count = 0;
        try {
            for (const node of [...document.querySelectorAll('body *')]) {
                const txt = node.innerText || node.textContent || '';
                if (!hasMarker(txt)) continue;
                let cur = node;
                for (let i = 0; i < 10 && cur && cur !== document.body && cur !== document.documentElement; i++, cur = cur.parentElement) {
                    const curTxt = cur.innerText || cur.textContent || '';
                    if (!hasMarker(curTxt)) continue;
                    if (boundedOverlay(cur)) count += kill(cur);
                    else {
                        try {
                            cur.style.setProperty('pointer-events', 'none', 'important');
                            cur.style.setProperty('visibility', 'hidden', 'important');
                            cur.style.setProperty('opacity', '0', 'important');
                            count++;
                        } catch (_) {}
                    }
                }
            }
        } catch (_) {}
        return count;
    };

    try {
        if (!window.__teletanKeyboardShortcutsKillerInstalled) {
            window.__teletanKeyboardShortcutsKillerInstalled = true;
            window.__teletanKillKeyboardShortcuts = killOnce;
            try {
                const observer = new MutationObserver(() => { try { killOnce(); } catch (_) {} });
                observer.observe(document.documentElement || document.body, {childList: true, subtree: true, attributes: true});
                window.__teletanKeyboardShortcutsObserver = observer;
            } catch (_) {}
            try { setInterval(() => { try { killOnce(); } catch (_) {} }, 250); } catch (_) {}
            try {
                document.addEventListener('keydown', (e) => {
                    if (e && (e.key === '?' || e.key === 'F1')) {
                        e.preventDefault();
                        e.stopPropagation();
                        e.stopImmediatePropagation();
                        setTimeout(killOnce, 0);
                    }
                }, true);
            } catch (_) {}
        }
        killOnce();
    } catch (_) {}
})();
"""

settings = get_settings()


class _RetryPageAcquire(Exception):
    """Internal signal to retry account page acquisition from a clean outer loop."""


class BrowserPool:
    """
    Пул браузеров для переиспользования контекстов.
    Каждый аккаунт имеет свой контекст (для cookies), но браузеры переиспользуются.
    """
    
    def __init__(self, max_browsers: int = None):
        """
        Инициализирует Browser Pool.
        
        Args:
            max_browsers: Максимальное количество одновременно открытых браузеров (None = из settings)
        """
        self._lock = asyncio.Lock()
        configured_max = max_browsers or settings.MAX_CONCURRENT_BROWSERS
        self.max_browsers = max(1, int(configured_max or 1))
        self._playwright: Optional[Playwright] = None
        self._browsers: Dict[str, Browser] = {}  # {browser_id: Browser}
        self._browser_proxy_keys: Dict[str, str] = {}  # {browser_id: original proxy string}
        self._account_browser_ids: Dict[int, str] = {}  # {account_id: browser_id}
        self._contexts: Dict[int, BrowserContext] = {}  # {account_id: BrowserContext}
        self._context_last_used: Dict[int, datetime] = {}  # {account_id: datetime}
        self._context_ttl_seconds = 300  # TTL контекста - 5 минут (cookies могут устареть)
        self._max_pool_size = max(1, self.max_browsers * 2)  # ИСПРАВЛЕНО: Максимальный размер пула контекстов (2x от браузеров)
        logger.info(f"Browser pool initialized: max_browsers={self.max_browsers}")

    def ensure_capacity(self, min_browsers: int) -> None:
        """Raise pool capacity for workflows that intentionally run browsers in parallel."""
        target = max(1, int(min_browsers or 1))
        if target <= self.max_browsers:
            return
        self.max_browsers = target
        self._max_pool_size = max(self._max_pool_size, self.max_browsers * 2)
        logger.info(
            "Browser pool capacity raised for current thread: max_browsers=%s max_pool_size=%s",
            self.max_browsers,
            self._max_pool_size,
        )
    
    async def _ensure_playwright(self):
        """
        ИСПРАВЛЕНО: Вспомогательный метод для гарантированной инициализации Playwright.
        Должен вызываться внутри блокировки _lock.
        
        КРИТИЧНО: Этот метод НЕ создает новую блокировку, а использует существующую.
        Все операции с Playwright должны быть атомарными внутри одной блокировки.
        """
        # ИСПРАВЛЕНО: Простая проверка - если Playwright None, создаем новый
        if self._playwright is None:
            try:
                logger.debug("Creating new Playwright instance...")
                
                # ИСПРАВЛЕНО: Используем глобальный синхронный lock для синхронизации создания Playwright
                # Это предотвращает ситуации, когда несколько потоков одновременно создают Playwright
                # и один из них может получить None из-за race condition
                # Сначала проверяем с lock, затем создаем async, затем присваиваем с lock
                should_create = False
                with _playwright_init_lock:
                    if self._playwright is None:
                        should_create = True
                
                if should_create:
                    # Создаем Playwright вне lock (async операция)
                    playwright_instance = await async_playwright().start()
                    if playwright_instance is None:
                        raise TikTokClientError("Failed to start Playwright (returned None)")
                    
                    # Присваиваем с lock для защиты от race condition
                    with _playwright_init_lock:
                        # Двойная проверка - возможно другой поток уже создал
                        if self._playwright is None:
                            self._playwright = playwright_instance
                            logger.info("Browser pool initialized")
                        else:
                            # Другой поток уже создал, используем его и закрываем наш
                            logger.debug("Playwright was initialized by another thread, closing duplicate")
                            try:
                                await playwright_instance.stop()
                            except Exception:
                                pass
            except Exception as e:
                logger.error(f"Failed to initialize Playwright: {e}")
                self._playwright = None
                raise TikTokClientError(f"Failed to initialize Playwright: {str(e)[:200]}")
        
        # ИСПРАВЛЕНО: Проверяем валидность Playwright атомарно
        # Проверяем наличие chromium атрибута
        if not hasattr(self._playwright, 'chromium') or self._playwright.chromium is None:
            logger.warning("Playwright.chromium attribute not found or None, recreating Playwright...")
            try:
                # Сохраняем ссылку на старый экземпляр
                old_playwright = self._playwright
                self._playwright = None  # Сбрасываем ДО создания нового
                
                # Останавливаем старый экземпляр
                if old_playwright:
                    try:
                        await old_playwright.stop()
                    except Exception:
                        pass
            except Exception:
                pass
            
            # Создаем новый экземпляр
            try:
                playwright_instance = await async_playwright().start()
                if playwright_instance is None:
                    raise TikTokClientError("Failed to start Playwright (returned None)")
                # ИСПРАВЛЕНО: Присваиваем только после успешного создания
                self._playwright = playwright_instance
                logger.info("Browser pool reinitialized")
            except Exception as e:
                logger.error(f"Failed to reinitialize Playwright: {e}")
                self._playwright = None
                raise TikTokClientError(f"Failed to reinitialize Playwright: {str(e)[:200]}")
    
    async def initialize(self):
        """Инициализирует Playwright (вызывается один раз)."""
        # ИСПРАВЛЕНО: Защищаем инициализацию блокировкой для предотвращения гонок
        async with self._lock:
            await self._ensure_playwright()
    
    def _make_browser_id(self, proxy_value: Optional[str]) -> str:
        normalized = (proxy_value or "").strip()
        if not normalized:
            return "direct"
        digest = hashlib.md5(normalized.encode("utf-8")).hexdigest()[:12]
        return f"proxy:{digest}"

    def _sync_pool_metrics(self) -> None:
        try:
            live_pages = 0
            reusable_pages = 0
            for account_id, context in list(self._contexts.items()):
                try:
                    for page in context.pages:
                        if page.is_closed():
                            continue
                        live_pages += 1
                        if self._is_page_available(page, account_id):
                            reusable_pages += 1
                except Exception:
                    continue
            set_gauge("browser_pool.contexts", len(self._contexts))
            set_gauge("browser_pool.browsers", len(self._browsers))
            set_gauge("browser_pool.pages", live_pages)
            set_gauge("browser_pool.reusable_pages", reusable_pages)
        except Exception:
            pass

    def _active_proxy_strings(self) -> list[str]:
        active_values: list[str] = []
        for browser_id in self._account_browser_ids.values():
            proxy_value = self._browser_proxy_keys.get(browser_id)
            if proxy_value:
                active_values.append(proxy_value)
        return active_values

    @staticmethod
    def _is_page_available(page: Page, account_id: int | None = None) -> bool:
        try:
            if page.is_closed():
                return False
        except Exception:
            return False
        if not getattr(page, "_managed_by_pool", False):
            return False
        if getattr(page, "_pool_available", False) is not True:
            return False
        if account_id is not None and getattr(page, "_pool_account_id", None) != account_id:
            return False
        return True

    @staticmethod
    def _mark_page_in_use(page: Page, account_id: int) -> None:
        page._managed_by_pool = True
        page._pool_account_id = account_id
        page._pool_available = False
        page._pool_task_id = _current_task_id.get()

    async def return_page_to_pool(self, page: Page | None) -> None:
        """
        Возвращает управляемую страницу обратно в пул.

        WHY: это снижает churn по new_page()/close(), экономит память и даёт
        более ровное время отклика на повторных действиях того же аккаунта.
        """
        if not page:
            return
        try:
            if page.is_closed():
                return
        except Exception:
            return

        account_id = getattr(page, "_pool_account_id", None)
        if account_id is None:
            try:
                await page.close()
            except Exception:
                pass
            return

        async with self._lock:
            context = self._contexts.get(account_id)
            if context is None:
                try:
                    await page.close()
                except Exception:
                    pass
                return

            # Держим в контексте максимум одну "свободную" страницу.
            try:
                live_pages = [p for p in context.pages if not p.is_closed()]
            except Exception:
                live_pages = []

            other_available = [
                p for p in live_pages
                if p is not page and self._is_page_available(p, account_id)
            ]
            if other_available or len(live_pages) > 2:
                try:
                    await page.close()
                except Exception:
                    pass
                self._context_last_used[account_id] = datetime.now(timezone.utc)
                self._sync_pool_metrics()
                return

            try:
                current_url = page.url
            except Exception:
                current_url = ""

            if current_url and current_url != "about:blank":
                try:
                    await page.goto("about:blank", wait_until="domcontentloaded", timeout=1500)
                except Exception:
                    # Если очистка не удалась, лучше закрыть страницу, чем держать её в грязном состоянии.
                    try:
                        await page.close()
                    except Exception:
                        pass
                    self._context_last_used[account_id] = datetime.now(timezone.utc)
                    self._sync_pool_metrics()
                    return

            page._managed_by_pool = True
            page._pool_account_id = account_id
            page._pool_available = True
            page._pool_task_id = None
            self._context_last_used[account_id] = datetime.now(timezone.utc)
            self._sync_pool_metrics()

    async def close_pages_for_task(self, task_id: int, *, reason: str | None = None) -> int:
        """Close live pages in this pool that belong to a stopped task."""
        task_id_int = int(task_id)
        closed_targets = 0
        async with self._lock:
            for account_id, context in list(self._contexts.items()):
                try:
                    pages = [page for page in context.pages if not page.is_closed()]
                except Exception:
                    pages = []

                task_pages = [
                    page for page in pages
                    if getattr(page, "_pool_task_id", None) == task_id_int
                ]
                if not task_pages:
                    continue

                logger.info(
                    "Closing browser pages for stopped task %s account=%s pages=%s reason=%s",
                    task_id_int,
                    account_id,
                    len(task_pages),
                    reason or "task_stop",
                )
                try:
                    await asyncio.gather(
                        *(page.close() for page in task_pages),
                        return_exceptions=True,
                    )
                except Exception as exc:
                    logger.debug(
                        "Error closing pages for stopped task %s account=%s: %s",
                        task_id_int,
                        account_id,
                        exc,
                    )

                closed_targets += len(task_pages)
                try:
                    remaining_pages = [page for page in context.pages if not page.is_closed()]
                except Exception:
                    remaining_pages = []

                if remaining_pages:
                    self._context_last_used[account_id] = datetime.now(timezone.utc)
                else:
                    try:
                        await context.close()
                    except Exception as exc:
                        logger.debug(
                            "Error closing empty context for stopped task %s account=%s: %s",
                            task_id_int,
                            account_id,
                            exc,
                        )
                    self._contexts.pop(account_id, None)
                    self._context_last_used.pop(account_id, None)
                    self._account_browser_ids.pop(account_id, None)

            if closed_targets:
                await self._cleanup_unused_browsers()
                self._sync_pool_metrics()
        return closed_targets

    async def close_context_for_account(self, account_id: int, *, reason: str | None = None) -> bool:
        """Close and forget one account context from the current pool."""
        try:
            account_id_int = int(account_id)
        except (TypeError, ValueError):
            return False

        async with self._lock:
            context = self._contexts.pop(account_id_int, None)
            self._context_last_used.pop(account_id_int, None)
            self._account_browser_ids.pop(account_id_int, None)
            if not context:
                return False

            logger.debug(
                "Closing browser context for account=%s reason=%s",
                account_id_int,
                reason or "account_context_close",
            )
            try:
                pages = [page for page in context.pages if not page.is_closed()]
            except Exception:
                pages = []
            if pages:
                await asyncio.gather(*(page.close() for page in pages), return_exceptions=True)
            try:
                await context.close()
            except Exception as exc:
                logger.debug("Error closing context for account=%s: %s", account_id_int, exc)

            await self._cleanup_unused_browsers()
            self._sync_pool_metrics()
            return True

    async def _close_browser(self, browser_id: str, reason: str | None = None) -> None:
        browser = self._browsers.pop(browser_id, None)
        proxy_value = self._browser_proxy_keys.pop(browser_id, None)

        if reason:
            logger.debug("Closing browser %s: %s", browser_id, reason)

        for account_id, mapped_browser_id in list(self._account_browser_ids.items()):
            if mapped_browser_id == browser_id:
                self._account_browser_ids.pop(account_id, None)

        if browser:
            try:
                await browser.close()
            except Exception as exc:
                logger.debug("Error closing browser %s: %s", browser_id, exc)

        if proxy_value:
            try:
                release_local_http_proxy_for_socks5(proxy_value)
            except Exception as exc:
                logger.debug("Error releasing local proxy for browser %s: %s", browser_id, exc)

    def _register_browser(self, browser_id: str, browser: Browser, proxy_source: Optional[str]) -> None:
        self._browsers[browser_id] = browser
        if proxy_source and proxy_source.strip():
            self._browser_proxy_keys[browser_id] = proxy_source.strip()
        else:
            self._browser_proxy_keys.pop(browser_id, None)

    async def _cleanup_unused_browsers(self) -> int:
        referenced_browser_ids = set(self._account_browser_ids.values())
        stale_browser_ids = [
            browser_id
            for browser_id in list(self._browsers.keys())
            if browser_id not in referenced_browser_ids
        ]
        for browser_id in stale_browser_ids:
            await self._close_browser(browser_id, reason="no active contexts")
        return len(stale_browser_ids)

    async def close_all(self):
        """Закрывает все браузеры и контексты."""
        async with self._lock:
            self._sync_pool_metrics()
            # Закрываем страницы и контексты без искусственных пауз:
            # это заметно ускоряет shutdown и не держит лишнюю память.
            for account_id, context in list(self._contexts.items()):
                try:
                    pages = [page for page in context.pages if not page.is_closed()]
                    if pages:
                        await asyncio.gather(
                            *(page.close() for page in pages),
                            return_exceptions=True,
                        )
                    await context.close()
                except Exception as e:
                    logger.warning(f"Error closing context for account {account_id}: {e}")
            self._contexts.clear()
            self._context_last_used.clear()
            self._account_browser_ids.clear()

            for browser_id in list(self._browsers.keys()):
                await self._close_browser(browser_id, reason="close_all")
            
            # Закрываем Playwright с ожиданием завершения всех connections
            if self._playwright:
                try:
                    await self._playwright.stop()
                except Exception as e:
                    logger.warning(f"Error stopping Playwright: {e}")
                finally:
                    self._playwright = None
            cleanup_idle_local_http_proxies(idle_seconds=0)
            self._sync_pool_metrics()
            logger.info("Browser pool closed")
    
    async def _cleanup_old_contexts(self):
        """
        Удаляет старые контексты (старше TTL) и контролирует размер пула.
        
        ИСПРАВЛЕНО: Добавлен контроль размера пула для предотвращения утечек памяти.
        """
        now = datetime.now(timezone.utc)
        to_remove = []
        
        # Очистка по TTL
        for account_id, last_used in list(self._context_last_used.items()):
            age = (now - last_used).total_seconds()
            if age > self._context_ttl_seconds:
                to_remove.append(account_id)
        
        # ИСПРАВЛЕНО: Если пул переполнен - удаляем самые старые контексты (LRU)
        if len(self._contexts) > self._max_pool_size:
            # Сортируем по времени последнего использования
            sorted_contexts = sorted(
                self._context_last_used.items(),
                key=lambda x: x[1]
            )
            # Удаляем самые старые
            excess_count = len(self._contexts) - self._max_pool_size
            for account_id, _ in sorted_contexts[:excess_count]:
                if account_id not in to_remove:
                    to_remove.append(account_id)
            logger.warning(
                f"Pool size ({len(self._contexts)}) exceeds max ({self._max_pool_size}), "
                f"removing {excess_count} oldest contexts"
            )
        
        # Закрываем и удаляем контексты
        for account_id in to_remove:
            if account_id in self._contexts:
                try:
                    await self._contexts[account_id].close()
                except Exception as e:
                    logger.debug(f"Error closing old context for account {account_id}: {e}")
                del self._contexts[account_id]
            if account_id in self._context_last_used:
                del self._context_last_used[account_id]
            self._account_browser_ids.pop(account_id, None)
        
        if to_remove:
            logger.debug(f"Cleaned up {len(to_remove)} old contexts. Pool size: {len(self._contexts)}")
        cleaned_browsers = await self._cleanup_unused_browsers()
        cleanup_idle_local_http_proxies(
            active_proxy_strings=self._active_proxy_strings(),
            idle_seconds=self._context_ttl_seconds,
        )
        if cleaned_browsers:
            logger.debug(f"Cleaned up {cleaned_browsers} unused browsers after context eviction")
        self._sync_pool_metrics()
    
    async def get_page_for_account(self, account: Account, retry_count: int = 0) -> Tuple[BrowserContext, Page]:
        """
        Получает или создаёт страницу для аккаунта.
        
        Args:
            account: Аккаунт из БД
            retry_count: Счетчик попыток (для защиты от бесконечной рекурсии)
            
        Returns:
            Tuple[BrowserContext, Page]: Контекст и страница
            
        Raises:
            TikTokClientError: При ошибке создания контекста
        """
        while True:
            # ИСПРАВЛЕНО: Защита от бесконечной рекурсии и ручного release/acquire lock
            if retry_count > 3:
                raise TikTokClientError(f"Too many retries ({retry_count}) for account {account.id}, giving up")
            
            # ИСПРАВЛЕНО: Инициализируем Playwright перед блокировкой
            await self.initialize()
            async with self._lock:
                # Очищаем старые контексты
                await self._cleanup_old_contexts()

                account_id = account.id

                # Проверяем, есть ли уже контекст для этого аккаунта
                if account_id in self._contexts:
                    context = self._contexts[account_id]
                    # ИСПРАВЛЕНО: Проверяем, не закрыт ли контекст и не стал ли он None
                    if context is None:
                        logger.debug(f"Context for account {account_id} is None, creating new one")
                        del self._contexts[account_id]
                        if account_id in self._context_last_used:
                            del self._context_last_used[account_id]
                        self._account_browser_ids.pop(account_id, None)
                    else:
                        try:
                            existing_pages = context.pages
                            reusable_page = next(
                                (p for p in existing_pages if self._is_page_available(p, account_id)),
                                None
                            )
                            if reusable_page is not None:
                                self._context_last_used[account_id] = datetime.now(timezone.utc)
                                self._mark_page_in_use(reusable_page, account_id)
                                self._sync_pool_metrics()
                                return context, reusable_page

                            if len(existing_pages) > 2:  # Держим максимум 2 страницы на контекст
                                for old_page in existing_pages[:-1]:
                                    try:
                                        if not old_page.is_closed():
                                            await old_page.close()
                                    except Exception:
                                        pass

                            # Обновляем время последнего использования
                            self._context_last_used[account_id] = datetime.now(timezone.utc)

                            # Создаём новую страницу
                            page = await context.new_page()
                            self._mark_page_in_use(page, account_id)
                            self._sync_pool_metrics()
                            return context, page
                        except Exception as e:
                            # Контекст закрыт или стал невалидным, удаляем его
                            logger.debug(f"Context for account {account_id} is closed or invalid, creating new one: {e}")
                            try:
                                await context.close()
                            except Exception:
                                pass
                            del self._contexts[account_id]
                            if account_id in self._context_last_used:
                                del self._context_last_used[account_id]
                            self._account_browser_ids.pop(account_id, None)
            
            # Нужно создать новый контекст
            # Импортируем функцию создания контекста из универсального клиента
            from executor.tiktok_client import _get_cookies_for_account, parse_cookies, _normalize_proxy_for_playwright
            
            try:
                # Получаем cookies
                cookies_str = _get_cookies_for_account(account)
                cookies_list = parse_cookies(cookies_str)
                
                # Находим или создаём браузер
                # ИСПРАВЛЕНО: Используем отдельный браузер для каждого прокси
                # Это предотвращает проблемы, когда разные прокси требуют разных настроек браузера
                # Используем хеш прокси как ID браузера (или 0 если прокси нет)
                proxy_source = account.proxy.strip() if account.proxy and account.proxy.strip() else None
                browser_id = self._make_browser_id(proxy_source)
                
                # ИСПРАВЛЕНО: Гарантируем, что Playwright инициализирован (внутри блокировки)
                await self._ensure_playwright()
                
                # ИСПРАВЛЕНО: Получаем ссылку на Playwright АТОМАРНО сразу после ensure
                # Это критично - между ensure и использованием не должно быть await, который может освободить блокировку
                playwright_instance = self._playwright
                if playwright_instance is None:
                    logger.error(f"Playwright is None after ensure_playwright for account {account.id}")
                    raise TikTokClientError("Playwright reference is None after ensure")
                
                if not hasattr(playwright_instance, 'chromium'):
                    logger.error(f"Playwright.chromium attribute not found for account {account.id}")
                    raise TikTokClientError("Playwright.chromium attribute not found")
                
                chromium_instance = playwright_instance.chromium
                if chromium_instance is None:
                    logger.error(f"Playwright.chromium is None for account {account.id}")
                    raise TikTokClientError("Playwright.chromium is None")
                
                # Проверяем, существует ли браузер и он ещё подключен
                browser = None
                leaked_context = locals().get("context")
                if leaked_context is not None:
                    try:
                        await leaked_context.close()
                    except Exception as cleanup_error:
                        logger.debug(f"Error closing context after acquire failure: {cleanup_error}")
                    if self._contexts.get(account_id) is leaked_context:
                        self._contexts.pop(account_id, None)
                        self._context_last_used.pop(account_id, None)
                        self._account_browser_ids.pop(account_id, None)
                        self._sync_pool_metrics()

                if browser_id in self._browsers:
                    browser = self._browsers[browser_id]
                    # Проверяем, что браузер ещё подключен
                    try:
                        # Пробуем получить список контекстов - если браузер закрыт, это вызовет ошибку
                        _ = browser.contexts
                    except Exception as e:
                        # Браузер закрыт или отключен, удаляем его и создадим новый
                        logger.debug(f"Browser {browser_id} is closed or disconnected: {e}, creating new one")
                        await self._close_browser(browser_id, reason="browser disconnected")
                        browser = None
                        browser = None
                
                if browser is None:
                    # Создаём новый браузер.
                    # patchright требует полный Chromium (не headless-shell) — headless-shell
                    # не поддерживает CDP remote debugging pipe, который нужен для stealth-патчей.
                    # Поэтому всегда headless=False + "--headless=new" в args вместо headless=True.
                    launch_options = {
                        "headless": False,
                    }
                    leased_proxy_source = None
                    
                    # Добавляем прокси, если есть
                    if account.proxy and account.proxy.strip():
                        proxy_dict = _normalize_proxy_for_playwright(account.proxy)
                        if proxy_dict:
                            proxy_server = proxy_dict.get("server", "")
                            
                            # Обработка SOCKS5 с авторизацией
                            # ИСПРАВЛЕНО: Проверяем SOCKS5 прокси (с префиксом или без) с авторизацией
                            is_socks5 = proxy_server.startswith("socks5://") or (
                                "@" in account.proxy and "://" not in account.proxy
                            )
                            has_auth = "username" in proxy_dict or "password" in proxy_dict
                            
                            if is_socks5 and has_auth:
                                try:
                                    local_proxy_port = get_local_http_proxy_for_socks5(proxy_source or account.proxy)
                                    if local_proxy_port:
                                        launch_options["proxy"] = {
                                            "server": f"http://127.0.0.1:{local_proxy_port}"
                                        }
                                        leased_proxy_source = proxy_source or account.proxy
                                        logger.debug(f"Account {account.id}: Using local HTTP proxy (port {local_proxy_port}) for SOCKS5")
                                    else:
                                        logger.warning(f"Account {account.id}: Failed to create local HTTP proxy for SOCKS5")
                                except Exception as e:
                                    logger.warning(f"Account {account.id}: Error creating local HTTP proxy: {e}")
                            elif proxy_server.startswith("socks5://"):
                                # SOCKS5 без авторизации (редкий случай)
                                launch_options["proxy"] = proxy_dict
                            else:
                                # HTTP прокси
                                launch_options["proxy"] = proxy_dict
                    
                    # WebRTC args применяются ВСЕГДА, не только при наличии прокси
                    launch_options.setdefault("args", [])
                    # Headless через --headless=new (полный Chromium, а не headless-shell)
                    if settings.PLAYWRIGHT_HEADLESS:
                        launch_options["args"].append("--headless=new")
                    launch_options["args"].extend([
                        # ── Audio ────────────────────────────────────────────────────────────
                        "--mute-audio",

                        # ── Anti-detect (КРИТИЧНО для TikTok) ──────────────────────────────
                        "--disable-blink-features=AutomationControlled",
                        "--no-first-run",
                        "--no-default-browser-check",
                        "--disable-infobars",
                        "--disable-popup-blocking",
                        "--ignore-certificate-errors",

                        # ── WebRTC — отключаем утечку IP ───────────────────────────────────
                        # `--disable-webrtc` полностью убирает WebRTC API → не будет
                        # ICE candidates, не будет gathering реального LAN/WAN IP
                        # вокруг прокси.
                        "--disable-webrtc",
                        "--disable-webrtc-hw-encoding",
                        # WebRtcHideLocalIpsWithMdns ставился ранее в `disable-features`,
                        # но имя фичи означает «скрывать local IP» — выключать её = РАСКРЫВАТЬ.
                        # Поскольку весь WebRTC уже выключен, этот флаг не имеет эффекта
                        # и убран как мёртвая (потенциально вводящая в заблуждение) опция.

                        # ── Производительность ─────────────────────────────────────────────
                        "--disable-dev-shm-usage",
                        "--no-sandbox",
                        "--disable-gpu-sandbox",
                    ])
                    
                    # Таймаут только для прокси (чтобы не ждать долго при проблемах с прокси)
                    if "proxy" in launch_options:
                        launch_options["args"].append("--timeout=60000")
                    
                    try:
                        logger.debug(f"Launching browser for account {account.id} with browser_id {browser_id}")
                        # КРИТИЧНО: Получаем СВЕЖУЮ ссылку на chromium ПРЯМО перед вызовом launch()
                        # Это защищает от ситуации, когда Playwright становится None между проверкой и вызовом
                        # Делаем это в цикле с проверкой, чтобы гарантировать валидность ссылки
                        max_retries = 3
                        browser = None
                        for retry in range(max_retries):
                            playwright_final = self._playwright
                            if playwright_final is None:
                                logger.warning(f"Playwright is None before launch (attempt {retry+1}/{max_retries}), reinitializing...")
                                await self._ensure_playwright()
                                playwright_final = self._playwright
                                if playwright_final is None:
                                    if retry < max_retries - 1:
                                        continue
                                    raise TikTokClientError("Playwright is None after reinitialization")
                            
                            if not hasattr(playwright_final, 'chromium'):
                                logger.warning(f"Playwright.chromium attribute not found (attempt {retry+1}/{max_retries})")
                                if retry < max_retries - 1:
                                    self._playwright = None
                                    continue
                                raise TikTokClientError("Playwright.chromium attribute not found")
                            
                            chromium_final = playwright_final.chromium
                            if chromium_final is None:
                                logger.warning(f"Playwright.chromium is None (attempt {retry+1}/{max_retries})")
                                if retry < max_retries - 1:
                                    self._playwright = None
                                    continue
                                raise TikTokClientError("Playwright.chromium is None")
                            
                            # ИСПРАВЛЕНО: Вызываем launch() используя СВЕЖУЮ ссылку, полученную прямо перед вызовом
                            # Дополнительная проверка прямо перед вызовом для максимальной защиты
                            try:
                                # КРИТИЧНО: Последняя проверка прямо перед вызовом launch()
                                # Это защищает от ситуации, когда Playwright становится None в последний момент
                                final_check = self._playwright
                                if final_check is None or not hasattr(final_check, 'chromium') or final_check.chromium is None:
                                    logger.warning(f"Playwright became None right before launch() call (attempt {retry+1}/{max_retries})")
                                    if retry < max_retries - 1:
                                        self._playwright = None
                                        if final_check:
                                            try:
                                                await final_check.stop()
                                            except Exception:
                                                pass
                                        # Небольшая задержка перед следующей попыткой для стабилизации
                                        await asyncio.sleep(0.2)
                                        continue
                                    raise TikTokClientError("Playwright became None right before launch() call")
                                
                                # Получаем СВЕЖУЮ ссылку на chromium прямо перед вызовом
                                final_chromium = final_check.chromium
                                if final_chromium is None:
                                    logger.warning(f"Playwright.chromium became None right before launch() call (attempt {retry+1}/{max_retries})")
                                    if retry < max_retries - 1:
                                        self._playwright = None
                                        if final_check:
                                            try:
                                                await final_check.stop()
                                            except Exception:
                                                pass
                                        await asyncio.sleep(0.2)
                                        continue
                                    raise TikTokClientError("Playwright.chromium became None right before launch() call")
                                
                                # Вызываем launch() с максимально свежей ссылкой
                                browser = await final_chromium.launch(**launch_options)
                                break  # Успешно запустили браузер
                            except (AttributeError, TypeError) as launch_error:
                                error_str = str(launch_error)
                                if "'NoneType' object has no attribute 'send'" in error_str or "NoneType" in error_str:
                                    logger.warning(f"Playwright became None during launch() call (attempt {retry+1}/{max_retries}): {launch_error}")
                                    if retry < max_retries - 1:
                                        # Переинициализируем и пробуем снова
                                        old_playwright = self._playwright
                                        self._playwright = None
                                        if old_playwright:
                                            try:
                                                await old_playwright.stop()
                                            except Exception:
                                                pass
                                        # Задержка для стабилизации перед следующей попыткой
                                        await asyncio.sleep(0.3)
                                        continue
                                    else:
                                        raise
                                else:
                                    raise
                        
                        if browser is None:
                            raise TikTokClientError("Failed to launch browser after all retries")
                        
                        # ИСПРАВЛЕНО: Сохраняем браузер в словарь АТОМАРНО
                        self._register_browser(browser_id, browser, proxy_source)
                        leased_proxy_source = None
                        logger.debug(f"Created new browser {browser_id} for account {account.id}")
                    except (AttributeError, TypeError) as e:
                        error_str = str(e)
                        # ИСПРАВЛЕНО: Обрабатываем ошибки, связанные с NoneType
                        if "'NoneType' object has no attribute 'send'" in error_str or "NoneType" in error_str:
                            logger.error(f"Playwright became None during browser launch for account {account.id}: {e}")
                            # ИСПРАВЛЕНО: Переинициализируем Playwright и пробуем снова (все еще внутри блокировки)
                            try:
                                # Останавливаем старый Playwright если есть
                                old_playwright = self._playwright
                                self._playwright = None  # Сбрасываем ДО создания нового
                                
                                if old_playwright:
                                    try:
                                        await old_playwright.stop()
                                    except Exception:
                                        pass
                                
                                # Используем централизованный метод для переинициализации
                                await self._ensure_playwright()
                                
                                # Получаем свежие ссылки
                                playwright_instance = self._playwright
                                if playwright_instance is None:
                                    raise TikTokClientError("Playwright is None after reinitialization")
                                
                                chromium_instance = playwright_instance.chromium
                                if chromium_instance is None:
                                    raise TikTokClientError("Playwright.chromium is None after reinitialization")
                                
                                # Пробуем запустить браузер снова с новым экземпляром
                                browser = await chromium_instance.launch(**launch_options)
                                if browser is None:
                                    raise TikTokClientError("Browser launch returned None after reinitialization")
                                self._register_browser(browser_id, browser, proxy_source)
                                leased_proxy_source = None
                                logger.debug(f"Created new browser {browser_id} after reinitialization")
                            except Exception as retry_error:
                                logger.error(f"Failed to reinitialize and retry browser launch: {retry_error}")
                                if leased_proxy_source:
                                    release_local_http_proxy_for_socks5(leased_proxy_source)
                                    leased_proxy_source = None
                                raise TikTokClientError(f"Failed to launch browser after reinitialization: {str(retry_error)[:200]}")
                        else:
                            # Другие AttributeError/TypeError - пробрасываем дальше
                            if leased_proxy_source:
                                release_local_http_proxy_for_socks5(leased_proxy_source)
                                leased_proxy_source = None
                            raise
                    except Exception as e:
                        error_msg = str(e).lower()
                        logger.error(f"Failed to launch browser for account {account.id}: {e}")
                        # ИСПРАВЛЕНО: Не пытаемся переинициализировать для всех ошибок
                        # Только для ошибок, явно связанных с NoneType
                        if "nonetype" in error_msg or ("none" in error_msg and "object" in error_msg):
                            logger.error(f"NoneType error during browser launch for account {account.id}: {e}")
                            # Пробуем переинициализировать
                            try:
                                old_playwright = self._playwright
                                self._playwright = None
                                if old_playwright:
                                    try:
                                        await old_playwright.stop()
                                    except Exception:
                                        pass
                                await self._ensure_playwright()
                                playwright_instance = self._playwright
                                if playwright_instance is None:
                                    raise TikTokClientError("Playwright is None after reinitialization")
                                chromium_instance = playwright_instance.chromium
                                if chromium_instance is None:
                                    raise TikTokClientError("Playwright.chromium is None after reinitialization")
                                browser = await chromium_instance.launch(**launch_options)
                                if browser is None:
                                    raise TikTokClientError("Browser launch returned None after reinitialization")
                                self._register_browser(browser_id, browser, proxy_source)
                                leased_proxy_source = None
                                logger.debug(f"Created new browser {browser_id} after NoneType error recovery")
                            except Exception as retry_error:
                                logger.error(f"Failed to recover from NoneType error: {retry_error}")
                                if leased_proxy_source:
                                    release_local_http_proxy_for_socks5(leased_proxy_source)
                                    leased_proxy_source = None
                                raise TikTokClientError(f"Failed to launch browser: {str(e)[:200]}")
                        else:
                            # Для других ошибок просто пробрасываем
                            if leased_proxy_source:
                                release_local_http_proxy_for_socks5(leased_proxy_source)
                            leased_proxy_source = None
                            raise TikTokClientError(f"Failed to launch browser: {str(e)[:200]}")
                
                # ИСПРАВЛЕНО: Финальная проверка, что browser не None
                if browser is None:
                    raise TikTokClientError("Browser is None after initialization")
                
                # ИСПРАВЛЕНО: Дополнительная проверка перед созданием контекста
                # Проверяем, что браузер действительно подключен и работает
                try:
                    # Пробуем получить список контекстов - это проверит, что браузер жив
                    _ = browser.contexts
                except Exception as e:
                    # Браузер мертв, удаляем его и пробуем создать новый
                    logger.warning(f"Browser {browser_id} is dead before context creation: {e}, removing and retrying")
                    await self._close_browser(browser_id, reason="browser dead before context creation")
                    # Рекурсивно вызываем себя для создания нового браузера
                    # ИСПРАВЛЕНО: Передаем счетчик попыток
                    raise _RetryPageAcquire()
                
                # ИСПРАВЛЕНО: Определяем locale и timezone из country_code аккаунта
                # Маппинг country_code -> (locale, timezone)
                country_to_locale_tz = {
                    "US": ("en-US", "America/New_York"),
                    "GB": ("en-GB", "Europe/London"),
                    "DE": ("de-DE", "Europe/Berlin"),
                    "FR": ("fr-FR", "Europe/Paris"),
                    "RU": ("ru-RU", "Europe/Moscow"),
                    "UA": ("uk-UA", "Europe/Kyiv"),
                    "PL": ("pl-PL", "Europe/Warsaw"),
                    "IT": ("it-IT", "Europe/Rome"),
                    "ES": ("es-ES", "Europe/Madrid"),
                    "NL": ("nl-NL", "Europe/Amsterdam"),
                    "CA": ("en-CA", "America/Toronto"),
                    "AU": ("en-AU", "Australia/Sydney"),
                    "JP": ("ja-JP", "Asia/Tokyo"),
                    "KR": ("ko-KR", "Asia/Seoul"),
                    "CN": ("zh-CN", "Asia/Shanghai"),
                    "IN": ("en-IN", "Asia/Kolkata"),
                    "BR": ("pt-BR", "America/Sao_Paulo"),
                    "MX": ("es-MX", "America/Mexico_City"),
                }
                
                # Определяем locale и timezone
                locale = "en-US"
                timezone_id = "America/New_York"
                if account.country_code:
                    country_upper = account.country_code.upper()
                    if country_upper in country_to_locale_tz:
                        locale, timezone_id = country_to_locale_tz[country_upper]
                
                # КРИТИЧНО: Определяем тип устройства для эмуляции (Device Fingerprint)
                # Проверяем user_agent аккаунта на наличие мобильных индикаторов
                user_agent_lower = (account.user_agent or "").lower()
                is_mobile_ua = any(marker in user_agent_lower for marker in ["iphone", "android", "mobile", "ipad", "tablet"])
                
                # КРИТИЧНО: Используем playwright.devices для эмуляции реального устройства
                # Это предотвращает детект через navigator.maxTouchPoints, screen resolution, hardware concurrency
                device_profile = None
                if is_mobile_ua:
                    # Мобильное устройство - используем iPhone 13
                    try:
                        # КРИТИЧНО: devices доступен через статический словарь в модуле playwright
                        # Импортируем devices из playwright.sync_api или используем через async_playwright
                        from patchright.sync_api import devices
                        if "iPhone 13" in devices:
                            device_profile = devices["iPhone 13"]
                            logger.debug(f"Using iPhone 13 device profile for account {account.id} (mobile user_agent detected)")
                        else:
                            # Пробуем другие варианты iPhone
                            for device_name in ["iPhone 13 Pro", "iPhone 12", "iPhone 13 Pro Max"]:
                                if device_name in devices:
                                    device_profile = devices[device_name]
                                    logger.debug(f"Using {device_name} device profile for account {account.id} (mobile user_agent detected)")
                                    break
                    except (AttributeError, KeyError, ImportError) as e:
                        logger.debug(f"Device profile not available ({e}), falling back to custom mobile profile")
                        device_profile = None
                
                # Формируем context_options
                context_options = {
                    "locale": locale,
                    "timezone_id": timezone_id,
                }
                
                # КРИТИЧНО: Если есть device_profile - используем его (он уже содержит user_agent, viewport, deviceScaleFactor, hasTouch)
                if device_profile:
                    context_options.update(device_profile)
                else:
                    # Desktop - используем стандартные настройки
                    import random as _random
                    _chrome_versions = ["131.0.0.0", "132.0.0.0", "133.0.0.0", "134.0.0.0", "135.0.0.0"]
                    _chrome_ver = _random.choice(_chrome_versions)
                    context_options["user_agent"] = (
                        account.user_agent or
                        f"Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                        f"(KHTML, like Gecko) Chrome/{_chrome_ver} Safari/537.36"
                    )
                    # Desktop не имеет touch-событий
                    if account.proxy and account.proxy.strip():
                        context_options["viewport"] = {"width": 1920, "height": 1080}
                    else:
                        context_options["viewport"] = {"width": 1280, "height": 720}
                
                # КРИТИЧНО: Отключаем WebRTC для предотвращения утечки реального IP
                # permissions=[] запрещает камеру/микрофон, что предотвращает WebRTC запросы
                context_options["permissions"] = []  # Отключаем все permissions (включая geolocation для безопасности)
                
                # Если нужна geolocation - можно добавить позже, но WebRTC важнее
                
                # ИСПРАВЛЕНО: Проверяем browser еще раз перед вызовом new_context
                # И получаем его заново из словаря, чтобы убедиться, что он не изменился
                if browser_id not in self._browsers:
                    logger.warning(f"Browser {browser_id} was removed from dict before context creation, creating new one")
                    browser = None
                else:
                    browser = self._browsers[browser_id]
                
                if browser is None:
                    # Браузер стал None - пробуем создать новый
                    logger.warning(f"Browser {browser_id} is None before context creation, will create new one")
                    # Удаляем из словаря если есть
                    if browser_id in self._browsers:
                        await self._close_browser(browser_id, reason="browser missing before context creation")
                    # Рекурсивно вызываем себя для создания нового браузера
                    raise _RetryPageAcquire()
                
                # ИСПРАВЛЕНО: Финальная проверка жизнеспособности браузера
                try:
                    _ = browser.contexts
                except Exception as e:
                    logger.warning(f"Browser {browser_id} is dead right before context creation: {e}, removing and retrying")
                    await self._close_browser(browser_id, reason="browser died before context creation")
                    raise _RetryPageAcquire()
                
                # КРИТИЧНО: Инициализируем context = None перед try, чтобы избежать UnboundLocalError
                context = None
                try:
                    context = await browser.new_context(**context_options)
                    # Anti-detect init script — выполняется ДО любого JS на странице
                    try:
                        await context.add_init_script("""
                            Object.defineProperty(navigator, 'webdriver', { get: () => undefined, configurable: true });
                            if (!window.chrome) {
                                window.chrome = {
                                    runtime: { connect: () => {}, sendMessage: () => {} },
                                    loadTimes: function() { return {}; },
                                    csi: function() { return {}; },
                                    app: { isInstalled: false },
                                };
                            }
                            Object.defineProperty(navigator, 'plugins', {
                                get: () => {
                                    const plugins = [
                                        { name: 'Chrome PDF Plugin', filename: 'internal-pdf-viewer', description: 'Portable Document Format' },
                                        { name: 'Chrome PDF Viewer', filename: 'mhjfbmdgcfjbbpaeojofohoefgiehjai', description: '' },
                                        { name: 'Native Client', filename: 'internal-nacl-plugin', description: '' },
                                    ];
                                    plugins.refresh = () => {};
                                    plugins.item = (i) => plugins[i];
                                    plugins.namedItem = (n) => plugins.find(p => p.name === n);
                                    Object.setPrototypeOf(plugins, PluginArray.prototype);
                                    return plugins;
                                },
                                configurable: true,
                            });
                            Object.defineProperty(navigator, 'languages', { get: () => ['en-US', 'en'], configurable: true });
                            const _oq = window.navigator.permissions ? window.navigator.permissions.query : null;
                            if (_oq) {
                                window.navigator.permissions.query = (p) => (
                                    p.name === 'notifications' ?
                                        Promise.resolve({ state: Notification.permission }) : _oq(p)
                                );
                            }
                            ['cdc_adoQpoasnfa76pfcZLmcfl_Array','cdc_adoQpoasnfa76pfcZLmcfl_Promise',
                             'cdc_adoQpoasnfa76pfcZLmcfl_Symbol','__selenium_evaluate','__webdriver_evaluate',
                             '__driver_evaluate','__webdriver_script_fn','__$webdriverAsyncExecutor',
                             '__lastWatirAlert','__lastWatirConfirm','__lastWatirPrompt'].forEach(k => { try { delete window[k]; } catch(_) {} });
                        """)
                    except Exception as _init_err:
                        logger.debug("Anti-detect init script failed: %s", _init_err)
                except Exception as e:
                    # ИСПРАВЛЕНО: Обрабатываем все ошибки при создании контекста, включая TargetClosedError при shutdown
                    error_msg = str(e).lower()
                    if "target" in error_msg and "closed" in error_msg:
                        # Браузер закрыт (возможно, во время shutdown) - это нормально
                        logger.debug(f"Browser {browser_id} was closed during context creation (likely shutdown): {e}")
                        raise TikTokClientError(f"Browser was closed during context creation (likely shutdown): {e}")
                    # Если context не был создан, не пытаемся его использовать
                    if context is None:
                        raise
                except AttributeError as e:
                    if "'NoneType' object has no attribute 'send'" in str(e) or "NoneType" in str(e):
                        # Браузер стал None во время создания контекста - это проблема синхронизации
                        logger.error(
                            f"Browser became None during context creation for account {account.id}. "
                            f"Browser state: {browser is None}, browser_id in dict: {browser_id in self._browsers}, "
                            f"browser_id value: {browser_id}"
                        )
                        # Удаляем браузер и пробуем создать новый
                        await self._close_browser(browser_id, reason="context creation attribute error")
                        # Пробуем создать новый браузер и контекст
                        raise _RetryPageAcquire()
                    raise
                except Exception as e:
                    # Любая другая ошибка при создании контекста
                    error_msg = str(e).lower()
                    if "nonetype" in error_msg or "none" in error_msg:
                        logger.error(f"Browser-related error during context creation for account {account.id}: {e}")
                        # Удаляем браузер и пробуем создать новый
                        await self._close_browser(browser_id, reason="context creation none-type error")
                        # Пробуем создать новый браузер и контекст
                        raise _RetryPageAcquire()
                    raise
                
                # КРИТИЧНО: Проверяем, что context был успешно создан перед использованием
                if context is None:
                    raise TikTokClientError(f"Failed to create browser context for account {account.id}")
                
                context.set_default_timeout(settings.PLAYWRIGHT_TIMEOUT)

                # Install early TikTok UI popup suppression before any real page is created.
                # This prevents the "Introducing keyboard shortcuts" overlay from blocking the Post button.
                try:
                    await context.add_init_script(_TIKTOK_SHORTCUTS_SUPPRESS_INIT_SCRIPT)
                    logger.debug("Teletan TikTok shortcuts suppression init script installed for account %s", account.id)
                except Exception as init_script_error:
                    logger.debug("Failed to install TikTok shortcuts suppression init script: %s", init_script_error)
                
                # ИСПРАВЛЕНО: Устанавливаем Accept-Language заголовки для синхронизации с navigator.languages
                accept_language_map = {
                    "en-US": "en-US,en;q=0.9",
                    "en-GB": "en-GB,en;q=0.9",
                    "de-DE": "de-DE,de;q=0.9,en;q=0.8",
                    "fr-FR": "fr-FR,fr;q=0.9,en;q=0.8",
                    "ru-RU": "ru-RU,ru;q=0.9,en;q=0.8",
                    "uk-UA": "uk-UA,uk;q=0.9,ru;q=0.8,en;q=0.7",
                    "pl-PL": "pl-PL,pl;q=0.9,en;q=0.8",
                    "it-IT": "it-IT,it;q=0.9,en;q=0.8",
                    "es-ES": "es-ES,es;q=0.9,en;q=0.8",
                    "nl-NL": "nl-NL,nl;q=0.9,en;q=0.8",
                    "en-CA": "en-CA,en;q=0.9",
                    "en-AU": "en-AU,en;q=0.9",
                    "ja-JP": "ja-JP,ja;q=0.9,en;q=0.8",
                    "ko-KR": "ko-KR,ko;q=0.9,en;q=0.8",
                    "zh-CN": "zh-CN,zh;q=0.9,en;q=0.8",
                    "en-IN": "en-IN,en;q=0.9",
                    "pt-BR": "pt-BR,pt;q=0.9,en;q=0.8",
                    "es-MX": "es-MX,es;q=0.9,en;q=0.8",
                }
                accept_language = accept_language_map.get(locale, "en-US,en;q=0.9")
                await context.set_extra_http_headers({
                    "Accept-Language": accept_language
                })
                
                # Добавляем cookies
                if cookies_list:
                    domain = cookies_list[0].get("domain", ".tiktok.com")
                    try:
                        await context.add_cookies(cookies_list)
                    except Exception as cookie_error:
                        if not domain.startswith("."):
                            await context.close()
                            raise TikTokClientError(f"Failed to add cookies: {cookie_error}")

                        temp_page = await context.new_page()
                        try:
                            wait_until_mode = "domcontentloaded"
                            timeout_for_proxy = settings.PLAYWRIGHT_TIMEOUT * 1.5
                            await temp_page.goto(
                                settings.TIKTOK_BASE_URL,
                                wait_until=wait_until_mode,
                                timeout=timeout_for_proxy
                            )
                            await context.add_cookies(cookies_list)
                        except Exception as warmup_error:
                            await context.close()
                            raise TikTokClientError(
                                f"Failed to add cookies after domain warmup: {warmup_error}"
                            )
                        finally:
                            await temp_page.close()
                
                # Применяем блокировку ресурсов
                async def route_handler(route):
                    try:
                        resource_type = route.request.resource_type
                        if settings.BLOCK_IMAGES and resource_type == "image":
                            await route.abort()
                            return
                        if settings.BLOCK_FONTS and resource_type == "font":
                            await route.abort()
                            return
                        if settings.BLOCK_STYLESHEETS and resource_type == "stylesheet":
                            await route.abort()
                            return
                        # ИСПРАВЛЕНО: Обрабатываем TargetClosedError при continue_()
                        try:
                            await route.continue_()
                        except Exception as continue_error:
                            # Игнорируем ошибки если страница/контекст закрыт
                            if "target" in str(continue_error).lower() or "closed" in str(continue_error).lower():
                                logger.debug(f"Route handler: page/context closed, ignoring: {continue_error}")
                                return
                            raise
                    except Exception as route_error:
                        # Игнорируем ошибки маршрутизации если контекст закрыт
                        if "target" in str(route_error).lower() or "closed" in str(route_error).lower():
                            logger.debug(f"Route handler: context closed, ignoring: {route_error}")
                            return
                        # Для других ошибок логируем, но не падаем
                        logger.debug(f"Route handler error: {route_error}")
                
                await context.route("**/*", route_handler)
                
                # ИСПРАВЛЕНО: Добавляем скрипты для маскировки НА КОНТЕКСТ, а не на временную страницу
                # КРИТИЧНО: context.add_init_script применяется ко ВСЕМ страницам контекста
                if settings.HUMAN_LIKE_BEHAVIOR:
                    # УЛУЧШЕННАЯ МАСКИРОВКА (Stealth V2)
                    # Включает:
                    # 1. Скрытие webdriver
                    # 2. Правильная эмуляция plugins (пустой список вместо [1..5])
                    # 3. Шум для Canvas (уникализация отпечатка)
                    # 4. Шум для WebGL (уникализация видеокарты)
                    # 5. Маскировка разрешений и WebRTC
                    await context.add_init_script("""
                        // 1. Hide Webdriver
                        Object.defineProperty(navigator, 'webdriver', { get: () => undefined });
                        
                        // 2. Fix Plugins (Emulate empty list correctly)
                        const mockPlugins = [];
                        Object.defineProperty(navigator, 'plugins', { get: () => mockPlugins });
                        Object.defineProperty(navigator, 'mimeTypes', { get: () => [] });
                        
                        // 3. Fix Languages
                        Object.defineProperty(navigator, 'languages', { 
                            get: () => ['en-US', 'en', 'ru-RU', 'ru'] 
                        });
                        
                        // 4. Chrome Runtime Mock
                        window.chrome = { runtime: {} };
                        
                        // 5. Permissions Mock
                        const originalQuery = window.navigator.permissions.query;
                        window.navigator.permissions.query = (parameters) => (
                            parameters.name === 'notifications' ?
                                Promise.resolve({ state: Notification.permission }) :
                                originalQuery(parameters)
                        );
                        
                        // 6. WebRTC Disable (Logic remains same)
                        const originalRTCPeerConnection = window.RTCPeerConnection;
                        if (originalRTCPeerConnection) {
                            window.RTCPeerConnection = function(...args) {
                                const pc = new originalRTCPeerConnection(...args);
                                const originalCreateDataChannel = pc.createDataChannel;
                                pc.createDataChannel = function() { throw new Error('WebRTC is disabled'); };
                                return pc;
                            };
                            if (window.RTCPeerConnection.prototype) {
                                window.RTCPeerConnection.prototype.getStats = () => Promise.resolve(new Map());
                            }
                        }

                        // === CANVAS NOISE ===
                        // Уникализируем рендеринг канваса, добавляя незаметный шум
                        (function() {
                            const shift = {
                                'r': Math.floor(Math.random() * 10) - 5,
                                'g': Math.floor(Math.random() * 10) - 5,
                                'b': Math.floor(Math.random() * 10) - 5,
                                'a': Math.floor(Math.random() * 10) - 5
                            };
                            // toDataURL noise
                            const originalToDataURL = HTMLCanvasElement.prototype.toDataURL;
                            HTMLCanvasElement.prototype.toDataURL = function(...args) {
                                const ctx = this.getContext('2d');
                                if (ctx) {
                                    // Рисуем невидимый шум перед экспортом
                                    const width = this.width;
                                    const height = this.height;
                                    const imageData = ctx.getImageData(0, 0, width, height);
                                    // Модифицируем всего пару пикселей
                                    for (let i = 0; i < Math.min(10, imageData.data.length); i += 4) {
                                        imageData.data[i] = imageData.data[i] + shift.r;
                                        imageData.data[i+1] = imageData.data[i+1] + shift.g;
                                        imageData.data[i+2] = imageData.data[i+2] + shift.b;
                                    }
                                    ctx.putImageData(imageData, 0, 0);
                                }
                                return originalToDataURL.apply(this, args);
                            };
                            
                            // getImageData noise
                            const originalGetImageData = CanvasRenderingContext2D.prototype.getImageData;
                            CanvasRenderingContext2D.prototype.getImageData = function(...args) {
                                const imageData = originalGetImageData.apply(this, args);
                                for (let i = 0; i < Math.min(100, imageData.data.length); i += 4) {
                                     imageData.data[i] = imageData.data[i] + shift.r; // R
                                     imageData.data[i+1] = imageData.data[i+1] + shift.g; // G
                                     imageData.data[i+2] = imageData.data[i+2] + shift.b; // B
                                }
                                return imageData;
                            };
                        })();

                        // === WEBGL NOISE ===
                        // Уникализируем параметры видеокарты
                        (function() {
                            const config = {
                                random: {
                                    value: function() { return Math.random() }
                                }
                            };
                            
                            const getParameter = WebGLRenderingContext.prototype.getParameter;
                            WebGLRenderingContext.prototype.getParameter = function(parameter) {
                                // Spoof unmasked info to generic values or random
                                if (parameter === 37445) { // UNMASKED_VENDOR_WEBGL
                                    return 'Intel Inc.';
                                }
                                if (parameter === 37446) { // UNMASKED_RENDERER_WEBGL
                                    return 'Intel Iris OpenGL Engine'; 
                                }
                                return getParameter.apply(this, [parameter]);
                            };
                        })();
                    """)
                
                # Сохраняем контекст
                self._contexts[account_id] = context
                self._context_last_used[account_id] = datetime.now(timezone.utc)
                self._account_browser_ids[account_id] = browser_id
                self._sync_pool_metrics()

                # Создаём страницу.
                # WHY: any exception here (e.g. browser died right between context
                # creation and new_page) would leak `context` because we already
                # registered it in self._contexts but it has no live page → no
                # cleanup path picks it up. Catch and close the orphan to keep RAM
                # stable on long worker runs.
                try:
                    page = await context.new_page()
                except Exception as new_page_err:
                    logger.warning(
                        "new_page() failed for account %s — closing orphan context: %s",
                        account_id, new_page_err,
                    )
                    self._contexts.pop(account_id, None)
                    self._context_last_used.pop(account_id, None)
                    self._account_browser_ids.pop(account_id, None)
                    self._sync_pool_metrics()
                    try:
                        await context.close()
                    except Exception:
                        pass
                    raise
                self._mark_page_in_use(page, account_id)
                return context, page
                
            except _RetryPageAcquire:
                retry_count += 1
                logger.debug(
                    "Retrying page acquire for account %s after browser/context recovery (retry=%s)",
                    account.id,
                    retry_count,
                )
                continue
            except TikTokClientError:
                raise
            except Exception as e:
                error_msg = str(e).lower()
                full_error = str(e)

                # Close orphan Playwright context: it was created via new_context()
                # but never registered in self._contexts (failure between init
                # script / headers / route setup and the registration line).
                # Without this, every such failure leaks a context until the
                # entire browser is recycled.
                if (
                    "context" in dir()
                    and context is not None
                    and self._contexts.get(account_id) is not context
                ):
                    try:
                        await context.close()
                        logger.debug(
                            "Closed orphan context for account %s after acquire failure",
                            account_id,
                        )
                    except Exception:
                        pass

                # ИСПРАВЛЕНО: Улучшенное логирование с контекстом
                logger.error(
                    f"Error creating browser context for account {account.id} (label={account.label[:20]}): "
                    f"{full_error[:200]}"
                )

                # ИСПРАВЛЕНО: Если браузер был создан, но контекст не создался - очищаем браузер
                if browser_id in self._browsers:
                    try:
                        browser = self._browsers[browser_id]
                        # Проверяем, не закрыт ли браузер
                        try:
                            _ = browser.contexts
                        except Exception:
                            # Браузер закрыт, удаляем его
                            logger.debug(f"Removing closed browser {browser_id} after context creation error")
                            await self._close_browser(browser_id, reason="context creation error")
                    except Exception as cleanup_error:
                        logger.debug(f"Error during browser cleanup: {cleanup_error}")
                
                if any(x in error_msg for x in ["timeout", "timed out", "exceeded"]):
                    raise TikTokClientError(f"Browser context timeout (proxy may be slow): {full_error[:200]}")
                if "nonetype" in error_msg or "none" in error_msg:
                    raise TikTokClientError(f"Browser or Playwright not initialized properly: {full_error[:200]}")
                raise TikTokClientError(f"Failed to create browser context: {full_error[:200]}")


# ИСПРАВЛЕНО: Используем thread-local storage для browser pool
# Это критично, т.к. executor работает в нескольких потоках, и каждый поток должен иметь свой Playwright экземпляр
import threading
_thread_local = threading.local()

# Глобальный синхронный lock для синхронизации инициализации Playwright между потоками
# Это дополнительная защита от race conditions при создании Playwright экземпляров
# Используем threading.Lock() т.к. он работает между разными потоками и event loops
_playwright_init_lock = threading.Lock()

def _get_thread_local_pool() -> Optional[BrowserPool]:
    """Получает thread-local browser pool."""
    if not hasattr(_thread_local, 'browser_pool'):
        _thread_local.browser_pool = None
    return _thread_local.browser_pool

def _set_thread_local_pool(pool: BrowserPool):
    """Устанавливает thread-local browser pool."""
    _thread_local.browser_pool = pool


def ensure_thread_browser_pool_capacity(min_browsers: int) -> BrowserPool:
    """Ensure the current worker thread pool can keep this many browsers alive."""
    target = max(1, int(min_browsers or 1))
    pool = _get_thread_local_pool()
    if pool is None:
        pool = BrowserPool(max_browsers=target)
        _set_thread_local_pool(pool)
    else:
        pool.ensure_capacity(target)
    return pool


async def get_browser_pool() -> BrowserPool:
    """
    Получает thread-local пул браузеров.
    
    ИСПРАВЛЕНО: Каждый поток executor'а получает свой собственный browser pool.
    Это предотвращает проблемы с thread-safety Playwright.
    """
    pool = _get_thread_local_pool()
    
    if pool is None:
        # Создаём новый pool для этого потока
        pool = BrowserPool()
        await pool.initialize()
        _set_thread_local_pool(pool)
        logger.debug(f"Created new browser pool for thread {threading.current_thread().ident}")
    
    # ИСПРАВЛЕНО: Проверяем инициализацию внутри блокировки pool
    async with pool._lock:
        if pool._playwright is None:
            logger.warning(f"Browser pool playwright is None in thread {threading.current_thread().ident}, reinitializing...")
            await pool._ensure_playwright()
    
    return pool


async def close_browser_pool():
    """
    Закрывает thread-local пул браузеров для текущего потока.
    
    ИСПРАВЛЕНО: Закрываем только pool текущего потока.
    """
    pool = _get_thread_local_pool()
    if pool:
        await pool.close_all()
        _set_thread_local_pool(None)
        logger.debug(f"Closed browser pool for thread {threading.current_thread().ident}")


async def close_task_pages(task_id: int, *, reason: str | None = None) -> int:
    """Close pages for a task in the current worker thread's browser pool."""
    pool = _get_thread_local_pool()
    if not pool:
        return 0
    return await pool.close_pages_for_task(task_id, reason=reason)


async def close_account_context(account_id: int, *, reason: str | None = None) -> bool:
    """Close one account context in the current worker thread's browser pool."""
    pool = _get_thread_local_pool()
    if not pool:
        return False
    return await pool.close_context_for_account(account_id, reason=reason)
