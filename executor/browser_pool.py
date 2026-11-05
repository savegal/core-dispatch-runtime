"""
Browser pool для переиспользования браузеров Playwright.
Критично для производительности - создание браузеров очень медленное.
"""
import asyncio
import json
import contextlib
import contextvars
import hashlib
import logging
import re
import sys
import time
import weakref
from types import SimpleNamespace
import threading
from typing import Optional, Dict, Tuple, Any
from datetime import datetime, timezone, timedelta
from patchright.async_api import async_playwright, Playwright, Browser, BrowserContext, Page
from db.models import Account
from config.settings import get_settings
from executor.tiktok_client import TikTokClientError
from executor.browser_errors import is_driver_connection_closed, is_browser_target_closed
from executor.static_asset_cache import is_cacheable_static_asset, static_asset_cache
from executor.browser_reads import await_browser_read
from executor.dom_actions import _JS_SUPPRESS_KEYBOARD_SHORTCUTS_OVERLAY
from executor.socks5_to_http_proxy import (
    cleanup_idle_local_http_proxies,
    get_local_http_proxy_for_socks5,
    release_local_http_proxy_for_socks5,
)
from services.metrics import set_gauge
from services.proxy_normalizer import account_proxy_route, ProxyRouteError
from services.cookie_validator import (
    normalize_cookie_records,
    normalize_cookies_for_browser,
    prepare_cookies_for_context,
)
from executor.http.request_profile import resolve_request_geo
from executor.device_profiles import (
    RESOLUTIONS as _DESKTOP_RESOLUTIONS,
    WINDOWS_GPUS as _DESKTOP_WEBGL_PAIRS,
    desktop_hardware, desktop_geometry, build_device_profile, install_device_profile, prepare_device_page, release_device_page,
)

logger = logging.getLogger("executor.browser_pool")

_MEDIA_EXTENSION_RE = re.compile(r"\.(?:mp4|m4v|webm|mov|m3u8|mpd)(?:[?#]|$)", re.IGNORECASE)
_TIKTOK_VIDEO_URL_MARKERS = (
    "/aweme/v1/play",
    "/video/tos/",
    "mime_type=video",
    "mime=video",
    "content_type=video",
    "video_mp4",
    "video_webm",
    "video_data",
)
_TIKTOK_VIDEO_HOST_MARKERS = (
    "v16-webapp-prime",
    "v19-webapp-prime",
    "v26-webapp-prime",
    "v16m-",
    "v19-",
    "v26-",
)
_TIKTOK_BACKGROUND_API_MARKERS = (
    "/api/recommend/",
    "/api/post/item_list/",
    "/api/im/",
    "/api/fullpage/",
)
_TIKTOK_TELEMETRY_HOST_MARKERS = (
    "mon.tiktokv.com",
    "mcs.tiktokv.com",
    "log.tiktokv.com",
)

_current_task_id: contextvars.ContextVar[int | None] = contextvars.ContextVar(
    "browser_pool_current_task_id",
    default=None,
)


def resolve_accept_language(locale: str | None) -> str | None:
    """Map locale to full Accept-Language header matching native Chromium format."""
    if not locale:
        return None
    accept_language_map = {
        "en-US": "en-US,en;q=0.9",
        "en-GB": "en-GB,en;q=0.9",
        "en-SG": "en-SG,en;q=0.9",
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
        "he-IL": "he-IL,he;q=0.9,en;q=0.8",
        "tr-TR": "tr-TR,tr;q=0.9,en;q=0.8",
        "kk-KZ": "kk-KZ,kk;q=0.9,ru;q=0.8,en;q=0.7",
    }
    if locale in accept_language_map:
        return accept_language_map[locale]
    if "-" in locale:
        primary_lang = locale.split("-")[0].lower()
        return f"{locale},en;q=0.9" if primary_lang == "en" else f"{locale},{primary_lang};q=0.9,en;q=0.8"
    return locale


def _looks_like_blockable_video_url(url: str | None) -> bool:
    safe_url = str(url or "").strip().lower()
    if not safe_url:
        return False
    if _MEDIA_EXTENSION_RE.search(safe_url):
        return True
    if any(marker in safe_url for marker in _TIKTOK_VIDEO_URL_MARKERS):
        return True
    return any(marker in safe_url for marker in _TIKTOK_VIDEO_HOST_MARKERS)


def _looks_like_blockable_background_request(url: str | None) -> bool:
    # Never abort TikTok telemetry (mcs/mon/log.tiktokv.com) or background API
    # endpoints (/api/recommend/, etc.): blocking them prevents webmssdk from
    # completing its handshake/signing and triggers empty_body shadow-bans.
    return False


def _should_abort_resource(resource_type: str | None, url: str | None = None) -> bool:
    """Return True when the current resource-blocking settings should abort a request."""
    resource = str(resource_type or "").strip().lower()
    if resource == "image" and settings.BLOCK_IMAGES:
        return True
    if resource == "media" and settings.BLOCK_MEDIA:
        return True
    if settings.BLOCK_MEDIA and _looks_like_blockable_video_url(url):
        return True
    if resource == "font" and settings.BLOCK_FONTS:
        return True
    if resource == "stylesheet" and settings.BLOCK_STYLESHEETS:
        return True
    return False


@contextlib.contextmanager
def browser_pool_task_scope(task_id: int | None):
    """Tags pages created inside this scope with the current task id."""
    token = _current_task_id.set(int(task_id) if task_id is not None else None)
    try:
        yield
    finally:
        _current_task_id.reset(token)


# Use the same bounded DOM handler at document startup and during publication.
# The previous private copy scanned layout text on every attribute mutation and
# every 250 ms, even after the normal DOM handler had been fixed.
_TIKTOK_SHORTCUTS_SUPPRESS_INIT_SCRIPT = (
    "(" + _JS_SUPPRESS_KEYBOARD_SHORTCUTS_OVERLAY.strip() + ")();"
)

settings = get_settings()


async def _install_resource_blocking_routes(context: BrowserContext, account_id: int | None = None) -> None:
    if (
        settings.BLOCK_IMAGES
        or settings.BLOCK_MEDIA
        or settings.BLOCK_FONTS
        or settings.BLOCK_STYLESHEETS
    ):
        logger.info(
            "[BROWSER_RESOURCE_BLOCK] account=%s images=%s media=%s fonts=%s stylesheets=%s service_workers=%s",
            account_id,
            bool(settings.BLOCK_IMAGES),
            bool(settings.BLOCK_MEDIA),
            bool(settings.BLOCK_FONTS),
            bool(settings.BLOCK_STYLESHEETS),
            "default",
        )

    async def route_handler(route):
        try:
            request = route.request
            resource_type = request.resource_type
            request_url = request.url
            if _should_abort_resource(resource_type, request_url):
                await route.abort()
                return
            if (settings.STATIC_ASSET_CACHE_ENABLED
                    and is_cacheable_static_asset(request.method, resource_type, request_url)):
                cached = static_asset_cache.get(request_url)
                if cached is not None:
                    await route.fulfill(status=cached.status, headers=cached.headers, body=cached.body)
                    return
            try:
                await route.continue_()
            except Exception as continue_error:
                if "target" in str(continue_error).lower() or "closed" in str(continue_error).lower():
                    logger.debug(f"Route handler: page/context closed, ignoring: {continue_error}")
                    return
                raise
        except Exception as route_error:
            if "target" in str(route_error).lower() or "closed" in str(route_error).lower():
                logger.debug(f"Route handler: context closed, ignoring: {route_error}")
                return
            logger.debug(f"Route handler error: {route_error}")

    await context.route("**/*", route_handler)

    tasks = set()
    pending_urls = set()
    context._static_asset_cache_tasks = tasks

    async def populate(response):
        try:
            all_headers = getattr(response, "all_headers", None)
            headers = await await_browser_read(all_headers) if callable(all_headers) else response.headers
            body = await await_browser_read(response.body)
            if settings.STATIC_ASSET_CACHE_ENABLED:
                static_asset_cache.put(response.url, response.status, headers, body)
        except Exception:
            pass  # Closing a tab must never affect other contexts' downloads.
        finally:
            pending_urls.discard(response.url)

    def _on_context_response(response):
        request = response.request
        if (settings.STATIC_ASSET_CACHE_ENABLED and response.status == 200
                and is_cacheable_static_asset(request.method, request.resource_type, response.url)
                and response.url not in pending_urls and not static_asset_cache.has(response.url)):
            pending_urls.add(response.url)
            task = asyncio.create_task(populate(response))
            tasks.add(task)
            task.add_done_callback(tasks.discard)

    def cancel_population(*_):
        for task in list(tasks):
            task.cancel()

    if callable(getattr(context, "on", None)):
        context.on("response", _on_context_response)
        context.on("close", cancel_population)


def _positive_int_setting(name: str, default: int, *, minimum: int = 1, maximum: int = 10000) -> int:
    try:
        raw = getattr(settings, name, default)
        value = default if raw is None else int(raw)
    except (TypeError, ValueError):
        value = default
    return max(minimum, min(maximum, value))


def _desktop_hardware_profile_for_account(account) -> dict:
    return desktop_hardware(account)


def _resolve_account_proxy_for_pool(account) -> str:
    from services.dedicated_proxy_service import get_current_dedicated_proxy
    has_dedicated = get_current_dedicated_proxy() is not None
    if not settings.USE_PROXY and not has_dedicated:
        return ""
    return str(getattr(account, "proxy", "") or "").strip()


def _desktop_viewport_size_for_account(account) -> tuple[int, int]:
    if getattr(account, "id", None) is not None:
        profile = _desktop_hardware_profile_for_account(account)
        return profile["width"], profile["height"]
    has_proxy = bool(_resolve_account_proxy_for_pool(account))
    if has_proxy:
        return (
            _positive_int_setting("PLAYWRIGHT_DESKTOP_VIEWPORT_WIDTH", 1920, minimum=800, maximum=3840),
            _positive_int_setting("PLAYWRIGHT_DESKTOP_VIEWPORT_HEIGHT", 1080, minimum=600, maximum=2160),
        )
    return (
        _positive_int_setting("PLAYWRIGHT_PROXYLESS_VIEWPORT_WIDTH", 1280, minimum=800, maximum=3840),
        _positive_int_setting("PLAYWRIGHT_PROXYLESS_VIEWPORT_HEIGHT", 720, minimum=600, maximum=2160),
    )


def _desktop_window_args_for_account(account) -> list[str]:
    if not bool(getattr(settings, "PLAYWRIGHT_SYNC_WINDOW_WITH_VIEWPORT", True)):
        return []
    width, height = _desktop_viewport_size_for_account(account)
    extra_height = _positive_int_setting("PLAYWRIGHT_WINDOW_EXTRA_HEIGHT", 90, minimum=0, maximum=240)
    if getattr(account, "id", None) is not None:
        geometry = desktop_geometry({"width": width, "height": height})
        height = geometry["outerHeight"]
        extra_height = 0
    return [
        f"--window-size={width},{height + extra_height}",
        "--force-device-scale-factor=1",
        "--high-dpi-support=1",
    ]


async def _log_page_window_metrics(page: Page, account_id: int | None) -> None:
    try:
        try:
            metrics = await page.evaluate(
                """() => ({
                    innerWidth,
                    innerHeight,
                    outerWidth,
                    outerHeight,
                    devicePixelRatio,
                    screenWidth: screen.width,
                    screenHeight: screen.height,
                    docClientWidth: document.documentElement.clientWidth,
                    docScrollWidth: document.documentElement.scrollWidth,
                })""",
                isolated_context=False,
            )
        except TypeError:
            metrics = await page.evaluate(
                """() => ({
                    innerWidth,
                    innerHeight,
                    outerWidth,
                    outerHeight,
                    devicePixelRatio,
                    screenWidth: screen.width,
                    screenHeight: screen.height,
                    docClientWidth: document.documentElement.clientWidth,
                    docScrollWidth: document.documentElement.scrollWidth,
                })"""
            )
    except Exception as exc:
        logger.debug("Browser viewport metric probe failed for account=%s: %s", account_id, exc)
        return

    try:
        width_gap = abs(int(metrics.get("innerWidth") or 0) - int(metrics.get("outerWidth") or 0))
        height_gap = abs(int(metrics.get("innerHeight") or 0) - int(metrics.get("outerHeight") or 0))
    except Exception:
        width_gap = 0
        height_gap = 0
    logger.info(
        "[BROWSER_VIEWPORT] account=%s inner=%sx%s outer=%sx%s dpr=%s screen=%sx%s doc=%sx%s mismatch=%s",
        account_id,
        metrics.get("innerWidth"),
        metrics.get("innerHeight"),
        metrics.get("outerWidth"),
        metrics.get("outerHeight"),
        metrics.get("devicePixelRatio"),
        metrics.get("screenWidth"),
        metrics.get("screenHeight"),
        metrics.get("docClientWidth"),
        metrics.get("docScrollWidth"),
        bool(width_gap > 180 or height_gap > 220),
    )


class _RetryPageAcquire(Exception):
    """Internal signal to retry account page acquisition from a clean outer loop."""


class BrowserPoolCapacityError(TikTokClientError):
    """Local pre-dispatch resource pressure, not an account/proxy failure."""


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
        self._runtime_lock = asyncio.Lock()
        self._capacity_lock = asyncio.Lock()
        self._capacity_changed = asyncio.Event()
        self._browser_reservations: set[str] = set()
        self._context_reservations: set[int] = set()
        self._disposing_busy_context_ids: set[int] = set()
        self._context_socks5_leases: dict[str, tuple[int, int]] = {}
        self._closing_browser_ids: set[str] = set()
        self._account_locks = weakref.WeakValueDictionary()
        self._closing = False
        self._browser_idle_since: Dict[str, float] = {}
        configured_max = max_browsers or settings.MAX_CONCURRENT_BROWSERS
        self.max_browsers = max(1, int(configured_max or 1))
        self._playwright: Optional[Playwright] = None
        self._browsers: Dict[str, Browser] = {}  # {browser_id: Browser}
        self._browser_id_locks = weakref.WeakValueDictionary()
        self._browser_inflight_acquires: Dict[str, int] = {}
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
        self._capacity_changed.set()
        logger.info(
            "Browser pool capacity raised for current thread: max_browsers=%s max_pool_size=%s",
            self.max_browsers,
            self._max_pool_size,
        )
    
    async def _ensure_playwright(self):
        # Acquires for different proxies run outside _lock. Serialize the
        # asynchronous start itself, not just the assignment of its result.
        async with self._runtime_lock:
            await self._ensure_playwright_locked()

    async def _reset_disconnected_runtime(self, failed_playwright: Optional[Playwright]) -> None:
        async with self._runtime_lock:
            # A late error from another acquire must not stop a replacement
            # driver that has already recovered successfully.
            if self._playwright is not failed_playwright or failed_playwright is None:
                return
            self._playwright = None
            contexts = list(self._contexts.items())
            proxy_sources = list(self._browser_proxy_keys.values())
            self._browsers.clear()
            self._browser_idle_since.clear()
            self._browser_proxy_keys.clear()
            self._contexts.clear()
            self._context_last_used.clear()
            self._account_browser_ids.clear()
            self._sync_pool_metrics()
            try:
                # A dead transport may not emit page-close events. Explicit
                # disposal still releases every captured page's proxy lease.
                await asyncio.gather(*(self._dispose_context(context, account_id)
                                       for account_id, context in contexts if context is not None),
                                     return_exceptions=True)
            finally:
                for proxy_source in proxy_sources:
                    try:
                        release_local_http_proxy_for_socks5(proxy_source)
                    except Exception as exc:
                        logger.debug("Disconnected driver proxy cleanup failed: %s", exc)
                try:
                    await asyncio.wait_for(failed_playwright.stop(), timeout=3.0)
                except Exception as exc:
                    logger.debug("Disconnected browser driver cleanup failed: %s", exc)
            logger.warning("Browser driver disconnected; cleared stale pool handles for recovery")

    async def _ensure_playwright_locked(self):
        """Start the thread-local driver while holding the runtime lock."""
        if self._playwright is None:
            try:
                playwright_instance = await async_playwright().start()
                if playwright_instance is None:
                    raise TikTokClientError("Failed to start Playwright (returned None)")
                self._playwright = playwright_instance
                logger.info("Browser pool initialized")
            except Exception as exc:
                self._playwright = None
                raise TikTokClientError(f"Failed to initialize Playwright: {str(exc)[:200]}") from exc

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
        if self._closing:
            raise TikTokClientError("Browser pool is closing")
        await self._ensure_playwright()
    
    def _make_browser_id(self, proxy_value: Optional[str]) -> str:
        normalized = (proxy_value or "").strip()
        if not normalized:
            return "direct"
        digest = hashlib.md5(normalized.encode("utf-8")).hexdigest()[:12]
        return f"proxy:{digest}"

    def _process_browser_id(self, proxy_value: Optional[str]) -> str:
        if settings.BROWSER_POOL_SHARED_PROCESS and proxy_value:
            return "shared_proxied"
        return self._make_browser_id(proxy_value)

    def _acquire_context_proxy(self, proxy_source, proxy_dict):
        server = proxy_dict.get("server", "")
        if (server.startswith(("socks5://", "socks5h://"))
                and ("username" in proxy_dict or "password" in proxy_dict)):
            current = self._context_socks5_leases.get(proxy_source)
            try:
                port = current[0] if current else get_local_http_proxy_for_socks5(proxy_source)
            except Exception as exc:
                raise ProxyRouteError("socks_bridge_unavailable") from exc
            if not port:
                raise ProxyRouteError("socks_bridge_unavailable")
            self._context_socks5_leases[proxy_source] = (port, (current[1] if current else 0) + 1)
            return {"server": f"http://127.0.0.1:{port}"}, proxy_source
        return proxy_dict, None

    def _release_context_proxy(self, proxy_source):
        current = self._context_socks5_leases.get(proxy_source)
        if current is None:
            return
        if current[1] > 1:
            self._context_socks5_leases[proxy_source] = (current[0], current[1] - 1)
        else:
            self._context_socks5_leases.pop(proxy_source, None)
            release_local_http_proxy_for_socks5(proxy_source)

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
        active_values.extend(getattr(context, "_account_proxy_source", "")
                             for context in self._contexts.values()
                             if getattr(context, "_account_proxy_source", ""))
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
        from executor.http.page_runtime import document_generation
        document_generation(page)
        page._managed_by_pool = True
        page._pool_account_id = account_id
        page._pool_available = False
        page._pool_task_id = _current_task_id.get()

    async def return_page_to_pool(self, page: Page | None) -> None:
        if page is None:
            return
        account_id = getattr(page, "_pool_account_id", None)
        if account_id is None:
            await page.close()
            return
        async with self._account_lock(account_id):
            context = self._contexts.get(account_id)
            if context is None or getattr(page, "context", context) is not context:
                await page.close()
                return
            others = [p for p in context.pages if p is not page and not p.is_closed()]
            if any(self._is_page_available(p, account_id) for p in others) or len(others) >= 2:
                await page.close()
            else:
                try:
                    if page.url != "about:blank":
                        await page.goto("about:blank", wait_until="domcontentloaded", timeout=1500)
                    if not page.is_closed():
                        page._pool_available = True
                        page._pool_task_id = None
                except Exception:
                    await page.close()
            await self._release_page_lease(page)
            self._context_last_used[account_id] = datetime.now(timezone.utc)
            self._sync_pool_metrics()
        self._capacity_changed.set()
        await self._cleanup_unused_browsers()

    async def close_pages_for_task(self, task_id: int, *, reason: str | None = None) -> int:
        targets = []
        # No await during the snapshot. Page closing uses the account lock and
        # checks ownership again, so a replacement context cannot be removed.
        for context in list(self._contexts.values()):
            try:
                targets.extend(page for page in context.pages
                               if not page.is_closed() and getattr(page, "_pool_task_id", None) == int(task_id))
            except Exception as exc:
                logger.debug("Could not inspect stopped task context: %s", exc)
        results = await asyncio.gather(*(self.close_page_from_pool(page, reason=reason) for page in targets))
        return sum(bool(result) for result in results)

    async def close_page_from_pool(self, page: Page | None, *, reason: str | None = None) -> bool:
        if page is None:
            return False
        account_id = getattr(page, "_pool_account_id", None)
        if account_id is None:
            if page.is_closed():
                return False
            await page.close()
            return True
        async with self._account_lock(account_id):
            context = self._contexts.get(account_id)
            if not page.is_closed():
                try:
                    await page.close()
                except Exception as exc:
                    logger.debug("Page close failed account=%s: %s", account_id, exc)
                if not page.is_closed():
                    return False
            await self._release_page_lease(page)
            if context is None or getattr(page, "context", context) is not context:
                return True
            if any(not other.is_closed() for other in context.pages):
                self._context_last_used[account_id] = datetime.now(timezone.utc)
                self._sync_pool_metrics()
                return True
            self._detach_context(account_id, context)
            await self._dispose_context(context, account_id)
        await self._cleanup_unused_browsers()
        self._sync_pool_metrics()
        return True

    async def _persist_context_cookies_before_close(
        self,
        context: Any,
        account_id: Optional[int] = None,
    ) -> None:
        """Best-effort flush of rotated cookies and JS web identity to DB before context close."""
        account = getattr(context, "_pool_account_obj", None)
        if account is None or getattr(account, "_anonymous_http_context", False):
            return
        try:
            from services.cookie_persistence_service import synchronize_browser_cookies
            await synchronize_browser_cookies(context, account)
        except Exception as exc:
            logger.debug(
                "Could not flush cookies before closing context account=%s: %s",
                account_id or getattr(account, "id", None),
                exc,
            )

    async def close_context_for_account(self, account_id: int, *, reason: str | None = None) -> bool:
        try:
            account_id = int(account_id)
        except (TypeError, ValueError):
            return False
        async with self._account_lock(account_id):
            context = self._contexts.get(account_id)
            if context is None:
                return False
            self._detach_context(account_id, context)
            await self._dispose_context(context, account_id)
        await self._cleanup_unused_browsers()
        self._sync_pool_metrics()
        return True

    def _account_lock(self, account_id):
        return self._account_locks.setdefault(account_id, asyncio.Lock())

    @staticmethod
    async def _release_page_lease(page):
        lease = getattr(page, "_proxy_usage_lease", None)
        if lease is not None:
            page._proxy_usage_lease = None
            callback = getattr(page, "_proxy_lease_close_callback", None)
            if callback is not None and callable(getattr(page, "remove_listener", None)):
                page.remove_listener("close", callback)
            await lease.release()

    def _detach_context(self, account_id, context):
        if self._contexts.get(account_id) is context:
            if settings.BROWSER_POOL_SHARED_PROCESS and self._context_has_busy_pages(account_id):
                self._disposing_busy_context_ids.add(account_id)
            browser_id = self._account_browser_ids.get(account_id)
            if browser_id:
                context._pool_disposal_browser_id = browser_id
                self._mark_browser_acquire_started(browser_id)
            self._contexts.pop(account_id, None)
            self._context_last_used.pop(account_id, None)
            self._account_browser_ids.pop(account_id, None)

    async def _dispose_context(self, context, account_id):
        if getattr(context, "_pool_disposed", False) is True:
            return
        context._pool_disposed = True
        cache_tasks = list(getattr(context, "_static_asset_cache_tasks", ()))
        for task in cache_tasks:
            task.cancel()
        try:
            pages = list(context.pages)
        except Exception:
            pages = []
        try:
            if cache_tasks:
                await asyncio.gather(*cache_tasks, return_exceptions=True)
            await self._persist_context_cookies_before_close(context, account_id)
        finally:
            try:
                try:
                    await asyncio.gather(*(release_device_page(page) for page in pages), return_exceptions=True)
                finally:
                    await context.close()
            except Exception as exc:
                logger.debug("Context close failed account=%s: %s", account_id, exc)
            finally:
                try:
                    for page in pages:
                        await self._release_page_lease(page)
                finally:
                    proxy_source = getattr(context, "_leased_socks5_proxy_source", None)
                    try:
                        if proxy_source:
                            context._leased_socks5_proxy_source = None
                            self._release_context_proxy(proxy_source)
                    except Exception as exc:
                        logger.debug("Context proxy cleanup failed account=%s: %s", account_id, exc)
                    self._disposing_busy_context_ids.discard(account_id)
                    self._capacity_changed.set()
                    browser_id = getattr(context, "_pool_disposal_browser_id", None)
                    if browser_id:
                        context._pool_disposal_browser_id = None
                        self._mark_browser_acquire_finished(browser_id)

    async def _close_browser(self, browser_id: str, reason: str | None = None, *, close_process: bool = True) -> None:
        if browser_id in self._closing_browser_ids:
            return
        self._closing_browser_ids.add(browser_id)
        browser = self._browsers.pop(browser_id, None)
        self._browser_idle_since.pop(browser_id, None)
        proxy_value = self._browser_proxy_keys.pop(browser_id, None)
        self._capacity_changed.set()

        if reason:
            logger.debug("Closing browser %s: %s", browser_id, reason)

        contexts = []
        for account_id, mapped_browser_id in list(self._account_browser_ids.items()):
            if mapped_browser_id == browser_id:
                context = self._contexts.get(account_id)
                if context is not None:
                    self._detach_context(account_id, context)
                    contexts.append((account_id, context))
                else:
                    self._contexts.pop(account_id, None)
                    self._account_browser_ids.pop(account_id, None)
                    self._context_last_used.pop(account_id, None)

        try:
            await asyncio.gather(*(self._dispose_context(context, aid) for aid, context in contexts), return_exceptions=True)
        finally:
            try:
                if browser and close_process:
                    try:
                        await browser.close()
                    except Exception as exc:
                        logger.debug("Error closing browser %s: %s", browser_id, exc)
            finally:
                try:
                    if proxy_value:
                        release_local_http_proxy_for_socks5(proxy_value)
                except Exception as exc:
                    logger.debug("Error releasing local proxy for browser %s: %s", browser_id, exc)
                finally:
                    self._closing_browser_ids.discard(browser_id)
                    self._capacity_changed.set()

    def _register_browser(self, browser_id: str, browser: Browser, proxy_source: Optional[str]) -> None:
        self._browsers[browser_id] = browser
        self._browser_idle_since.pop(browser_id, None)
        if proxy_source and proxy_source.strip() and browser_id != "shared_proxied":
            self._browser_proxy_keys[browser_id] = proxy_source.strip()
        else:
            self._browser_proxy_keys.pop(browser_id, None)

    async def _cleanup_unused_browsers(self, *, reserve: int = 0) -> int:
        if settings.BROWSER_POOL_SHARED_PROCESS:
            # Keep the two process owners alive. Bound retained idle contexts
            # to the concurrency budget as well: a single-worker laptop must
            # not retain two extra renderers while rotating account proxies.
            # Reservations still protect busy and not-yet-created contexts.
            excess = max(0, len(self._contexts) + reserve - self.max_browsers)
            disposed = 0
            for account_id in sorted(self._contexts, key=lambda aid: self._context_last_used.get(
                    aid, datetime.min.replace(tzinfo=timezone.utc))):
                if disposed >= excess:
                    break
                lock = self._account_lock(account_id)
                if (lock.locked() or account_id in self._context_reservations
                        or self._context_has_busy_pages(account_id)):
                    continue
                async with lock:
                    context = self._contexts.get(account_id)
                    if context is not None:
                        self._detach_context(account_id, context)
                        await self._dispose_context(context, account_id)
                        disposed += 1
            return disposed
        referenced_browser_ids = set(self._account_browser_ids.values())
        referenced_browser_ids.update(
            browser_id
            for browser_id, count in self._browser_inflight_acquires.items()
            if count > 0
        )
        live_context_ids = {id(context) for context in self._contexts.values() if context is not None}
        for browser_id, browser in list(self._browsers.items()):
            try:
                browser_context_ids = {id(context) for context in browser.contexts}
            except Exception:
                browser_context_ids = set()
            if live_context_ids.intersection(browser_context_ids):
                referenced_browser_ids.add(browser_id)
        now = time.monotonic()
        for browser_id in referenced_browser_ids:
            self._browser_idle_since.pop(browser_id, None)
        idle = []
        for browser_id in self._browsers:
            if browser_id not in referenced_browser_ids:
                idle.append((self._browser_idle_since.setdefault(browser_id, now), browser_id))
        idle.sort()
        stale = {browser_id for since, browser_id in idle if now - since >= self._context_ttl_seconds}
        excess = max(0, len(self._browsers) - len(stale) + reserve - self.max_browsers)
        for _, browser_id in idle:
            if excess <= 0:
                break
            if browser_id not in stale:
                stale.add(browser_id)
                excess -= 1
        for browser_id in stale:
            await self._close_browser(browser_id, reason="idle TTL or capacity")
        closed = len(stale)
        # An idle account context is not a reason to keep a whole extra
        # Chromium process alive when another proxy needs its slot.
        candidates = sorted(list(self._browsers), key=lambda bid: min(
            (self._context_last_used.get(aid, datetime.min.replace(tzinfo=timezone.utc))
             for aid, owner in self._account_browser_ids.items() if owner == bid),
            default=datetime.min.replace(tzinfo=timezone.utc)))
        for browser_id in candidates:
            if len(self._browsers) + reserve <= self.max_browsers:
                break
            if self._browser_inflight_acquires.get(browser_id, 0):
                continue
            browser_lock = self._browser_id_lock(browser_id)
            if browser_lock.locked():
                continue
            accounts = [aid for aid, owner in list(self._account_browser_ids.items()) if owner == browser_id]
            if not accounts:
                continue  # Unmapped live contexts are protected above.
            locks = [self._account_lock(aid) for aid in accounts]
            if any(lock.locked() for lock in locks) or any(self._context_has_busy_pages(aid) for aid in accounts):
                continue
            async with browser_lock:
                acquired = []
                try:
                    for lock in locks:
                        await lock.acquire()
                        acquired.append(lock)
                    if (self._browser_inflight_acquires.get(browser_id, 0)
                            or any(self._context_has_busy_pages(aid) for aid in accounts)):
                        continue
                    await self._close_browser(browser_id, reason="idle context LRU capacity")
                    closed += 1
                finally:
                    for lock in reversed(acquired):
                        lock.release()
        return closed

    async def _reserve_browser_capacity(self, browser_id: str, *, account_id: int | None = None) -> None:
        if settings.BROWSER_POOL_SHARED_PROCESS and account_id is None:
            return  # Process creation follows an account-context reservation.
        timeout = max(0.01, min(float(settings.BROWSER_POOL_CAPACITY_WAIT_SECONDS), 120.0))
        deadline = asyncio.get_running_loop().time() + timeout
        while True:
            if self._closing:
                raise TikTokClientError("Browser pool is closing")
            async with self._capacity_lock:
                if settings.BROWSER_POOL_SHARED_PROCESS:
                    self._capacity_changed.clear()
                    busy = {aid for aid in self._contexts if self._context_has_busy_pages(aid)}
                    occupied = busy | self._context_reservations | self._disposing_busy_context_ids
                    if account_id in occupied or len(occupied) < self.max_browsers:
                        self._context_reservations.add(account_id)
                        return
                else:
                    if browser_id in self._browsers or browser_id in self._browser_reservations:
                        return
                    self._capacity_changed.clear()
                    pending = len(self._browser_reservations - set(self._browsers) - self._closing_browser_ids)
                    await self._cleanup_unused_browsers(reserve=pending + 1)
                    if self._closing:
                        raise TikTokClientError("Browser pool is closing")
                    if len(set(self._browsers) | self._browser_reservations | self._closing_browser_ids) < self.max_browsers:
                        self._browser_reservations.add(browser_id)
                        return
            remaining = deadline - asyncio.get_running_loop().time()
            if remaining <= 0:
                raise BrowserPoolCapacityError("browser_pool_capacity_busy")
            try:
                await asyncio.wait_for(self._capacity_changed.wait(), timeout=remaining)
            except asyncio.TimeoutError as exc:
                raise BrowserPoolCapacityError("browser_pool_capacity_busy") from exc


    async def close_all(self, *, stop_playwright: bool = False):
        self._closing = True
        self._capacity_changed.set()
        locks = list(self._account_locks.values())
        acquired = []
        try:
            for lock in locks:
                await lock.acquire()
                acquired.append(lock)
            contexts = list(self._contexts.items())
            self._contexts.clear()
            self._context_last_used.clear()
            self._account_browser_ids.clear()
            # Slow CDP and persistence calls never hold the pool metadata lock.
            await asyncio.gather(*(self._dispose_context(context, account_id)
                                   for account_id, context in contexts))
            runtime_owns_browsers = stop_playwright and self._playwright is not None
            for browser_id in list(self._browsers):
                # Full shutdown closes the driver below, which also terminates
                # its browsers. Avoid redundant per-browser close RPCs after
                # all account contexts have already been flushed and closed.
                await self._close_browser(browser_id, reason="close_all", close_process=not runtime_owns_browsers)
        finally:
            try:
                # A cancelled browser.close() must still stop this pool's
                # driver and its remaining Chromium processes on full shutdown.
                if stop_playwright:
                    async with self._runtime_lock:
                        runtime, self._playwright = self._playwright, None
                        if runtime is not None:
                            await runtime.stop()
            finally:
                try:
                    cleanup_idle_local_http_proxies(idle_seconds=0)
                finally:
                    for lock in reversed(acquired):
                        lock.release()
                    self._closing = False
                    self._browser_reservations.clear()
                    self._context_reservations.clear()
                    self._disposing_busy_context_ids.clear()
                    self._capacity_changed.set()
                    self._sync_pool_metrics()

    def _context_has_busy_pages(self, account_id: int) -> bool:
        """Return True when a context still owns an in-use page."""
        context = self._contexts.get(account_id)
        if context is None:
            return False
        try:
            pages = [page for page in context.pages if not page.is_closed()]
        except Exception:
            return False
        return any(getattr(page, "_pool_available", None) is not True for page in pages)

    async def _cleanup_old_contexts(self):
        """
        Удаляет старые контексты (старше TTL) и контролирует размер пула.
        
        ИСПРАВЛЕНО: Добавлен контроль размера пула для предотвращения утечек памяти.
        """
        now = datetime.now(timezone.utc)
        to_remove = []
        busy_contexts = {
            account_id
            for account_id in list(self._contexts)
            if self._context_has_busy_pages(account_id)
        }
        
        # Очистка по TTL
        for account_id, last_used in list(self._context_last_used.items()):
            age = (now - last_used).total_seconds()
            if age > self._context_ttl_seconds and account_id not in busy_contexts:
                to_remove.append(account_id)
        
        # ИСПРАВЛЕНО: Если пул переполнен - удаляем самые старые контексты (LRU)
        if len(self._contexts) > self._max_pool_size:
            # Сортируем по времени последнего использования
            sorted_contexts = sorted(
                (
                    (account_id, last_used)
                    for account_id, last_used in self._context_last_used.items()
                    if account_id not in busy_contexts
                ),
                key=lambda x: x[1]
            )
            # Удаляем самые старые
            excess_count = len(self._contexts) - self._max_pool_size
            for account_id, _ in sorted_contexts[:excess_count]:
                if account_id not in to_remove:
                    to_remove.append(account_id)
            if to_remove:
                logger.warning(
                    f"Pool size ({len(self._contexts)}) exceeds max ({self._max_pool_size}), "
                    f"removing {len(to_remove)} idle context(s); busy contexts kept={len(busy_contexts)}"
                )
        
        for account_id in to_remove:
            lock = self._account_lock(account_id)
            if lock.locked() or self._context_has_busy_pages(account_id):
                continue
            async with lock:
                context = self._contexts.get(account_id)
                if context is not None:
                    self._detach_context(account_id, context)
                    await self._dispose_context(context, account_id)

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
    
    def _browser_id_lock(self, browser_id: str) -> asyncio.Lock:
        lock = self._browser_id_locks.get(browser_id)
        if lock is None:
            lock = asyncio.Lock()
            self._browser_id_locks[browser_id] = lock
        return lock

    def _mark_browser_acquire_started(self, browser_id: str) -> None:
        self._browser_inflight_acquires[browser_id] = self._browser_inflight_acquires.get(browser_id, 0) + 1

    def _mark_browser_acquire_finished(self, browser_id: str) -> None:
        current = self._browser_inflight_acquires.get(browser_id, 0)
        if current <= 1:
            self._browser_inflight_acquires.pop(browser_id, None)
            self._browser_reservations.discard(browser_id)
            self._capacity_changed.set()
        else:
            self._browser_inflight_acquires[browser_id] = current - 1

    async def resolve_shortlink(self, url: str, *, proxy=None, timeout=10.0, country_code=None) -> str:
        route_key = self._make_browser_id(proxy)
        anonymous_id = -int(hashlib.sha256(route_key.encode()).hexdigest()[:15], 16) - 1
        account = SimpleNamespace(id=anonymous_id, proxy=proxy, country_code=country_code,
                                  proxy_country_code=country_code,
                                  user_agent=None, label="Shortlink resolver",
                                  _anonymous_http_context=True)
        context, page = await self.get_page_for_account(account)
        try:
            await page.goto(url, wait_until="commit", timeout=int(timeout * 1000))
            return str(page.url)
        finally:
            await self.close_page_from_pool(page, reason="shortlink_resolved")

    async def get_page_for_account(
        self,
        account: Account,
        retry_count: int = 0,
        _browser_id_lock_held: bool = False,
        _parallel_shared_owner: bool = False,
    ) -> Tuple[BrowserContext, Page]:
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
        from services.dedicated_proxy_service import apply_dedicated_proxy
        apply_dedicated_proxy(account)
        resolved_route = account_proxy_route(account)
        if not _browser_id_lock_held:
            from services.proxy_concurrency_service import acquire_proxy_usage
            account_proxy_for_lock = _resolve_account_proxy_for_pool(account)
            proxy_source_for_lock = account_proxy_for_lock or None
            browser_id_for_lock = self._process_browser_id(proxy_source_for_lock)
            if self._closing:
                raise TikTokClientError("Browser pool is closing")
            async with self._account_lock(account.id):
                if self._closing:
                    raise TikTokClientError("Browser pool is closing")
                lease = None
                self._mark_browser_acquire_started(browser_id_for_lock)
                try:
                    # Waiting for an account slot or proxy lease must not hold
                    # the shared process lock and stall every other account.
                    lease = await acquire_proxy_usage(resolved_route.url) if resolved_route else None
                    if settings.BROWSER_POOL_SHARED_PROCESS:
                        await self._reserve_browser_capacity(browser_id_for_lock, account_id=account.id)
                    # Reuse of an account-isolated ready page needs only the
                    # account lock already held, not the global process lock.
                    reused = None
                    if settings.BROWSER_POOL_SHARED_PROCESS:
                        existing = self._contexts.get(account.id)
                        owner_id = self._account_browser_ids.get(account.id)
                        owner = self._browsers.get(owner_id)
                        expected_proxy_key = self._make_browser_id(proxy_source_for_lock)
                        if (existing is not None and owner_id == browser_id_for_lock
                                and getattr(existing, "_account_proxy_key", None) == expected_proxy_key
                                and not getattr(existing, "_pool_disposed", False)
                                and owner is not None and owner.is_connected()):
                            available_page = next(
                                (candidate for candidate in list(existing.pages)
                                 if self._is_page_available(candidate, account.id)),
                                None,
                            )
                            if available_page is not None:
                                self._context_last_used[account.id] = datetime.now(timezone.utc)
                                self._mark_page_in_use(available_page, account.id)
                                self._sync_pool_metrics()
                                reused = (existing, available_page)
                    if reused is not None:
                        context, page = reused
                    else:
                        # A live shared process can open independent account
                        # contexts concurrently. The process lock is needed
                        # only when its Chromium owner may need creation or
                        # recovery; keeping it during slow per-proxy setup
                        # serializes otherwise unrelated accounts.
                        shared_owner_ready = False
                        if settings.BROWSER_POOL_SHARED_PROCESS:
                            existing_owner = self._browsers.get(browser_id_for_lock)
                            if existing_owner is not None:
                                try:
                                    shared_owner_ready = bool(existing_owner.is_connected())
                                except Exception:
                                    shared_owner_ready = False
                        if shared_owner_ready:
                            context, page = await self.get_page_for_account(
                                account,
                                retry_count=retry_count,
                                _browser_id_lock_held=True,
                                _parallel_shared_owner=True,
                            )
                        else:
                            async with self._browser_id_lock(browser_id_for_lock):
                                if (not settings.BROWSER_POOL_SHARED_PROCESS
                                        and browser_id_for_lock not in self._browsers and account.id not in self._contexts):
                                    await self._reserve_browser_capacity(browser_id_for_lock)
                                context, page = await self.get_page_for_account(
                                    account,
                                    retry_count=retry_count,
                                    _browser_id_lock_held=True,
                                )
                    if callable(getattr(page, "on", None)) and not getattr(page, "_pool_capacity_callback", None):
                        page._pool_capacity_callback = lambda *_: self._capacity_changed.set()
                        page.on("close", page._pool_capacity_callback)
                    if lease is not None:
                        page._proxy_usage_lease = lease
                        async def release_lease(*_):
                            if getattr(page, "_proxy_usage_lease", None) is lease:
                                page._proxy_usage_lease = None
                                await lease.release()
                        page.on("close", release_lease)
                        page._proxy_lease_close_callback = release_lease
                    return context, page
                except BaseException:
                    if lease is not None:
                        await lease.release()
                    raise
                finally:
                    self._context_reservations.discard(account.id)
                    self._capacity_changed.set()
                    self._mark_browser_acquire_finished(browser_id_for_lock)

        while True:
            # ИСПРАВЛЕНО: Защита от бесконечной рекурсии и ручного release/acquire lock
            if retry_count > 3:
                raise TikTokClientError(f"Too many retries ({retry_count}) for account {account.id}, giving up")
            
            # ИСПРАВЛЕНО: Инициализируем Playwright перед блокировкой
            await self.initialize()
            # Очищаем старые контексты
            await self._cleanup_old_contexts()

            account_id = account.id

            # Проверяем, есть ли уже контекст для этого аккаунта
            if account_id in self._contexts:
                context = self._contexts[account_id]
                requested_proxy = _resolve_account_proxy_for_pool(account)
                requested_browser_id = self._make_browser_id(requested_proxy or None)
                mapped_browser_id = self._account_browser_ids.get(account_id)
                mapped_browser = self._browsers.get(mapped_browser_id)
                if mapped_browser is not None and not mapped_browser.is_connected():
                    await self._close_browser(mapped_browser_id, reason="disconnected context owner")
                    context = None
                if (
                    context is not None
                    and mapped_browser_id
                    and getattr(context, "_account_proxy_key", mapped_browser_id) != requested_browser_id
                ):
                    # A Playwright context cannot change its configured
                    # proxy. Reusing this account context after a DB proxy
                    # reassignment would silently send TikTok traffic through
                    # the old IP. Never interrupt an active page; otherwise
                    # retire the stale context/browser before acquiring the
                    # account on its currently assigned proxy.
                    if self._context_has_busy_pages(account_id):
                        raise TikTokClientError(
                            f"Account {account_id} proxy changed while its browser context is busy"
                        )
                    logger.info(
                        "Account %s proxy assignment changed; replacing stale browser context %s -> %s",
                        account_id,
                        mapped_browser_id,
                        requested_browser_id,
                    )
                    self._detach_context(account_id, context)
                    await self._dispose_context(context, account_id)
                    await self._cleanup_unused_browsers()
                    context = None
                # ИСПРАВЛЕНО: Проверяем, не закрыт ли контекст и не стал ли он None
                if context is None:
                    logger.debug(f"Context for account {account_id} is None, creating new one")
                    self._contexts.pop(account_id, None)
                    self._context_last_used.pop(account_id, None)
                    self._account_browser_ids.pop(account_id, None)
                else:
                    created_page = None
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
                                    if self._is_page_available(old_page, account_id):
                                        await old_page.close()
                                except Exception:
                                    pass

                        # Обновляем время последнего использования
                        self._context_last_used[account_id] = datetime.now(timezone.utc)

                        # Создаём новую страницу
                        page = await context.new_page()
                        created_page = page
                        await prepare_device_page(page)
                        self._mark_page_in_use(page, account_id)
                        self._sync_pool_metrics()
                        return context, page
                    except Exception as e:
                        if created_page is not None:
                            try:
                                await created_page.close()
                            except Exception:
                                pass
                        # Контекст закрыт или стал невалидным, удаляем его
                        logger.debug(f"Context for account {account_id} is closed or invalid, creating new one: {e}")
                        if self._context_has_busy_pages(account_id):
                            logger.warning(
                                "Context for account %s failed during page acquire, but busy page(s) are still active; "
                                "preserving context instead of closing it: %s",
                                account_id,
                                str(e)[:160],
                            )
                            raise TikTokClientError(
                                f"Context for account {account_id} is busy and cannot create a new page: {str(e)[:160]}"
                            )
                        self._detach_context(account_id, context)
                        await self._dispose_context(context, account_id)
                        context = None

            # Нужно создать новый контекст
            if settings.BROWSER_POOL_SHARED_PROCESS:
                await self._cleanup_unused_browsers(reserve=1)
            # Импортируем функцию создания контекста из универсального клиента
            from executor.tiktok_client import _get_cookies_for_account, parse_cookies, _normalize_proxy_for_playwright
            
            playwright_instance = self._playwright
            context = None
            leased_context_proxy_source = None
            context_proxy_options = None
            try:
                # Получаем cookies
                if getattr(account, "_anonymous_http_context", False):
                    cookies_list = []
                else:
                    cookies_str = _get_cookies_for_account(account)
                    cookies_list = prepare_cookies_for_context(account, cookies_str, parse_fn=parse_cookies)
                
                # Shared owners are keyed by proxied/direct mode; account
                # proxy identity remains on the isolated browser context.
                account_proxy = _resolve_account_proxy_for_pool(account)
                proxy_source = account_proxy or None
                browser_id = self._process_browser_id(proxy_source)
                if settings.BROWSER_POOL_SHARED_PROCESS and proxy_source:
                    proxy_dict = _normalize_proxy_for_playwright(proxy_source)
                    if not proxy_dict:
                        raise ProxyRouteError("invalid_proxy_route")
                    context_proxy_options, leased_context_proxy_source = self._acquire_context_proxy(proxy_source, proxy_dict)
                
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
                    self._detach_context(account_id, leaked_context)
                    await self._dispose_context(leaked_context, account_id)
                    context = None
                    self._sync_pool_metrics()

                if browser_id in self._browsers:
                    browser = self._browsers[browser_id]
                    # Проверяем, что браузер ещё подключен
                    try:
                        # contexts is a cached list; only is_connected reports a disconnect.
                        if not browser.is_connected():
                            raise TikTokClientError("Browser is disconnected")
                    except Exception as e:
                        # Браузер закрыт или отключен, удаляем его и создадим новый
                        logger.debug(f"Browser {browser_id} is closed or disconnected: {e}, creating new one")
                        await self._close_browser(browser_id, reason="browser disconnected")
                        browser = None
                        browser = None
                
                if browser is None:
                    # This path deliberately skipped the shared process lock
                    # because an owner was alive at entry. If it disappeared,
                    # do not race another account to launch a replacement.
                    # The next independent acquire will enter via the lock.
                    if _parallel_shared_owner:
                        raise TikTokClientError(
                            "Shared browser owner lost during parallel context setup"
                        )
                    await self._reserve_browser_capacity(browser_id)
                    # Создаём новый браузер.
                    # patchright требует полный Chromium (не headless-shell) — headless-shell
                    # не поддерживает CDP remote debugging pipe, который нужен для stealth-патчей.
                    # Поэтому всегда headless=False + "--headless=new" в args вместо headless=True.
                    launch_options = {
                        "headless": False,
                    }
                    leased_proxy_source = None
                    
                    # Добавляем прокси, если есть
                    if settings.BROWSER_POOL_SHARED_PROCESS and proxy_source:
                        launch_options["proxy"] = {"server": "http://127.0.0.1:1"}
                    elif account_proxy:
                        proxy_dict = _normalize_proxy_for_playwright(account_proxy)
                        if proxy_dict:
                            proxy_server = proxy_dict.get("server", "")
                            
                            # Обработка SOCKS5 с авторизацией
                            # ИСПРАВЛЕНО: Считаем SOCKS5 только когда схема явно socks5:// или socks5h://
                            is_socks5 = proxy_server.startswith(("socks5://", "socks5h://"))
                            has_auth = "username" in proxy_dict or "password" in proxy_dict
                            
                            if is_socks5 and has_auth:
                                try:
                                    local_proxy_port = get_local_http_proxy_for_socks5(proxy_source or account_proxy)
                                    if local_proxy_port:
                                        launch_options["proxy"] = {
                                            "server": f"http://127.0.0.1:{local_proxy_port}"
                                        }
                                        leased_proxy_source = proxy_source or account_proxy
                                        logger.debug(f"Account {account.id}: Using local HTTP proxy (port {local_proxy_port}) for SOCKS5")
                                    else:
                                        raise ProxyRouteError("socks_bridge_unavailable")
                                except Exception as e:
                                    raise ProxyRouteError("socks_bridge_unavailable") from e
                            elif proxy_server.startswith("socks5://"):
                                # SOCKS5 без авторизации (редкий случай)
                                launch_options["proxy"] = proxy_dict
                            else:
                                # HTTP прокси
                                launch_options["proxy"] = proxy_dict
                    
                    # WebRTC args применяются ВСЕГДА, не только при наличии прокси
                    launch_options.setdefault("args", [])
                    if not proxy_source:
                        launch_options["args"].append("--no-proxy-server")
                    # Headless через --headless=new (полный Chromium, а не headless-shell)
                    if settings.PLAYWRIGHT_HEADLESS:
                        launch_options["args"].append("--headless=new")
                    if not settings.BROWSER_POOL_SHARED_PROCESS:
                        launch_options["args"].extend(_desktop_window_args_for_account(account))
                    launch_options["args"].extend([
                        # ── Audio ────────────────────────────────────────────────────────────
                        "--mute-audio",

                        # ── Anti-detect (КРИТИЧНО для TikTok) ──────────────────────────────
                        "--disable-blink-features=AutomationControlled",
                        "--no-first-run",
                        "--no-default-browser-check",
                        "--disable-infobars",
                        "--disable-popup-blocking",

                        # ── WebRTC — предотвращаем утечку IP без удаления RTCPeerConnection ──
                        "--force-webrtc-ip-handling-policy=disable_non_proxied_udp",
                        "--enforce-webrtc-ip-permission-check",

                        # ── Производительность ─────────────────────────────────────────────
                        "--disable-dev-shm-usage",
                    ])
                    if sys.platform != "win32":
                        launch_options["args"].extend(["--no-sandbox", "--disable-gpu-sandbox"])
                    
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
                                playwright_instance = final_check
                                if self._playwright is not playwright_instance:
                                    raise TikTokClientError("Driver connection closed during browser acquisition")
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
                    # contexts remains readable even after a driver disconnect.
                    if not browser.is_connected():
                        raise TikTokClientError("Browser is disconnected")
                except Exception as e:
                    # Браузер мертв, удаляем его и пробуем создать новый
                    logger.warning(f"Browser {browser_id} is dead before context creation: {e}, removing and retrying")
                    await self._close_browser(browser_id, reason="browser dead before context creation")
                    # Рекурсивно вызываем себя для создания нового браузера
                    # ИСПРАВЛЕНО: Передаем счетчик попыток
                    raise _RetryPageAcquire()
                
                geo = resolve_request_geo(
                    runtime_profile={
                        "proxy_in_use": bool(proxy_source),
                        "proxy_country_code": resolved_route.country_code if resolved_route else None,
                        "account_country_code": getattr(account, "country_code", None),
                    },
                    cookies=normalize_cookie_records(cookies_list),
                )
                if proxy_source:
                    # Unknown proxy geography must never inherit the host's
                    # locale/timezone. Country metadata remains unset until known.
                    geo.setdefault("browser_language", "en")
                    geo.setdefault("tz_name", "Etc/UTC")
                locale = geo.get("browser_language")
                
                device = build_device_profile(
                    account, getattr(playwright_instance, "devices", {}),
                    getattr(browser, "version", None),
                    _positive_int_setting("PLAYWRIGHT_WINDOW_EXTRA_HEIGHT", 90, minimum=0, maximum=240),
                )
                context_options = dict(device["options"])
                if context_proxy_options is not None:
                    context_options["proxy"] = context_proxy_options
                accept_language = resolve_accept_language(locale)
                # A direct connection uses Chromium's system timezone/locale.
                # Account country and IDC hints only describe a proxied route.
                if proxy_source:
                    if locale:
                        context_options["locale"] = locale
                        device["override"]["acceptLanguage"] = accept_language
                        device["acceptLanguage"] = accept_language
                        device["locale"] = locale
                    if geo.get("tz_name"):
                        context_options["timezone_id"] = geo["tz_name"]
                logger.debug("Account %s device family=%s preset=%s", account.id,
                             device["identity"]["family"], device["identity"]["preset"])

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
                    if not browser.is_connected():
                        raise TikTokClientError("Browser is disconnected")
                except Exception as e:
                    logger.warning(f"Browser {browser_id} is dead right before context creation: {e}, removing and retrying")
                    await self._close_browser(browser_id, reason="browser died before context creation")
                    raise _RetryPageAcquire()
                
                # КРИТИЧНО: Инициализируем context = None перед try, чтобы избежать UnboundLocalError
                context = None
                try:
                    context = await browser.new_context(**context_options)
                except BaseException as e:
                    if not isinstance(e, Exception):
                        raise
                    error_msg = str(e).lower()
                    # A lost driver is recovered by the outer runtime handler.
                    if is_driver_connection_closed(e):
                        raise
                    if not self._closing and ("nonetype" in error_msg or is_browser_target_closed(e)):
                        other_busy_contexts = any(
                            self._context_has_busy_pages(aid) for aid, owner in self._account_browser_ids.items()
                            if owner == browser_id
                        )
                        if (not settings.BROWSER_POOL_SHARED_PROCESS or not browser.is_connected()
                                or not other_busy_contexts):
                            await self._close_browser(browser_id, reason="context creation transport failure")
                        raise _RetryPageAcquire()
                    raise
                
                # КРИТИЧНО: Проверяем, что context был успешно создан перед использованием
                if context is None:
                    raise TikTokClientError(f"Failed to create browser context for account {account.id}")

                context._leased_socks5_proxy_source = leased_context_proxy_source
                leased_context_proxy_source = None
                context._account_proxy_source = proxy_source
                context._account_proxy_key = self._make_browser_id(proxy_source)
                context._account_proxy_in_use = bool(proxy_source)
                context._shared_browser_process = bool(settings.BROWSER_POOL_SHARED_PROCESS and proxy_source)
                context._account_proxy_route = resolved_route
                context._account_locale = context_options.get("locale")
                context._account_accept_language = accept_language if proxy_source else None
                context._pool_account_id = account.id
                context._pool_account_obj = None if getattr(account, "_anonymous_http_context", False) else account
                try:
                    from executor.http.playwright_http_client import seed_web_identity_from_account
                    seeded_identity = seed_web_identity_from_account(account, context=context)
                    if seeded_identity.get("web_id_last_time"):
                        await context.add_init_script(
                            f"window.__tt_web_id_last_time = {json.dumps(str(seeded_identity['web_id_last_time']))};"
                        )
                except Exception:
                    pass
                await install_device_profile(context, device)

                await _install_resource_blocking_routes(context, account.id)
                context.set_default_timeout(settings.PLAYWRIGHT_TIMEOUT)

                # Install early TikTok UI popup suppression before any real page is created.
                # This prevents the "Introducing keyboard shortcuts" overlay from blocking the Post button.
                try:
                    await context.add_init_script(_TIKTOK_SHORTCUTS_SUPPRESS_INIT_SCRIPT)
                    logger.debug("Teletan TikTok shortcuts suppression init script installed for account %s", account.id)
                except Exception as init_script_error:
                    logger.debug("Failed to install TikTok shortcuts suppression init script: %s", init_script_error)

                # Синхронизируем Accept-Language заголовки с navigator.languages
                if proxy_source and accept_language:
                    await context.set_extra_http_headers({"Accept-Language": accept_language})
                
                # Добавляем cookies
                from services.cookie_validator import remember_imported_ms_token
                remember_imported_ms_token(context, cookies_list or [])
                if cookies_list:
                    domain = cookies_list[0].get("domain", ".tiktok.com")
                    try:
                        await context.add_cookies(cookies_list)
                    except Exception as cookie_error:
                        if not domain.startswith("."):
                            raise TikTokClientError(f"Failed to add cookies: {cookie_error}")

                        temp_page = await context.new_page()
                        await prepare_device_page(temp_page)
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
                            raise TikTokClientError(
                                f"Failed to add cookies after domain warmup: {warmup_error}"
                            )
                        finally:
                            await temp_page.close()

                # Сохраняем контекст
                if self._closing or self._playwright is not playwright_instance:
                    raise TikTokClientError("Driver connection closed during context acquisition")
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
                    await prepare_device_page(page)
                except Exception as new_page_err:
                    logger.warning(
                        "new_page() failed for account %s — closing orphan context: %s",
                        account_id, new_page_err,
                    )
                    self._detach_context(account_id, context)
                    await self._dispose_context(context, account_id)
                    self._sync_pool_metrics()
                    context = None
                    raise
                self._mark_page_in_use(page, account_id)
                await _log_page_window_metrics(page, account_id)
                if self._playwright is not playwright_instance:
                    raise TikTokClientError("Driver connection closed during page acquisition")
                return context, page
                
            except asyncio.CancelledError:
                if context is not None:
                    self._detach_context(account_id, context)
                    await self._dispose_context(context, account_id)
                raise
            except _RetryPageAcquire:
                retry_count += 1
                logger.debug(
                    "Retrying page acquire for account %s after browser/context recovery (retry=%s)",
                    account.id,
                    retry_count,
                )
                continue
            except Exception as e:
                if context is not None:
                    self._detach_context(account_id, context)
                    await self._dispose_context(context, account_id)
                if isinstance(e, ProxyRouteError):
                    raise
                error_msg = str(e).lower()
                full_error = str(e)

                if is_driver_connection_closed(e):
                    await self._reset_disconnected_runtime(playwright_instance)
                    if retry_count < 3:
                        retry_count += 1
                        logger.warning(
                            "Retrying account %s page acquisition after driver disconnect (retry=%s)",
                            account_id, retry_count,
                        )
                        continue
                    raise TikTokClientError(f"Browser driver unavailable after recovery: {full_error[:200]}") from e

                if isinstance(e, TikTokClientError):
                    raise

                # ИСПРАВЛЕНО: Улучшенное логирование с контекстом
                logger.error(
                    f"Error creating browser context for account {account.id} (label={str(getattr(account, 'label', None) or '')[:20]}): "
                    f"{full_error[:200]}"
                )

                # ИСПРАВЛЕНО: Если браузер был создан, но контекст не создался - очищаем браузер
                if browser_id in self._browsers:
                    try:
                        browser = self._browsers[browser_id]
                        # Проверяем, не закрыт ли браузер
                        try:
                            if not browser.is_connected():
                                raise TikTokClientError("Browser is disconnected")
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
            finally:
                if leased_context_proxy_source:
                    self._release_context_proxy(leased_context_proxy_source)


# ИСПРАВЛЕНО: Используем thread-local storage для browser pool
# Это критично, т.к. executor работает в нескольких потоках, и каждый поток должен иметь свой Playwright экземпляр
import threading
_thread_local = threading.local()



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
        _set_thread_local_pool(pool)
        logger.debug(f"Created new browser pool for thread {threading.current_thread().ident}")
    
    # Publish the pool before yielding; initialize serializes driver startup
    # under its own runtime lock for every concurrent caller.
    await pool.initialize()
    
    return pool


async def close_browser_pool(*, stop_playwright: bool = False):
    """
    Закрывает thread-local пул браузеров для текущего потока.
    
    Сохраняет playwright запущенным для мгновенного выполнения следующих задач,
    если stop_playwright не установлен в True.
    """
    pool = _get_thread_local_pool()
    if pool:
        await pool.close_all(stop_playwright=stop_playwright)
        if stop_playwright:
            _set_thread_local_pool(None)
        logger.debug(f"Closed browser pool for thread {threading.current_thread().ident} (playwright preserved: {not stop_playwright})")


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


async def close_pool_page(page: Page | None, *, reason: str | None = None) -> bool:
    """Close one managed page in the current worker thread's browser pool."""
    pool = _get_thread_local_pool()
    if not pool:
        return False
    return await pool.close_page_from_pool(page, reason=reason)
