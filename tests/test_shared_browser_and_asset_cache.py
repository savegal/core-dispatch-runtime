"""Local regressions for shared Chromium, passive caching, and HTTP warmup."""
import asyncio
import json
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from executor import browser_pool, engagement_engine
from executor.browser_pool import BrowserPool
from executor.http import comment_discovery
from executor.http.page_runtime import document_generation
from executor.static_asset_cache import StaticAssetCache, is_cacheable_static_asset
from services.cookie_validator import classify_account_web_readiness, remember_imported_ms_token


pytestmark = pytest.mark.unit
URL = "https://www.tiktok.com/@fixture/video/7635267329407913230"
ASSET = "https://sf16.tiktokcdn.com/assets/app.js"
HOT = {"sessionid": "session", "ttwid": "wid", "tt_csrf_token": "csrf", "msToken": "token"}


class Events:
    def __init__(self):
        self.listeners = {}

    def on(self, event, callback):
        self.listeners.setdefault(event, []).append(callback)

    def remove_listener(self, event, callback):
        self.listeners[event].remove(callback)

    def emit(self, event, value=None):
        for callback in list(self.listeners.get(event, [])):
            callback(value)


class Page(Events):
    def __init__(self, context, url="about:blank"):
        super().__init__()
        self.context, self.url = context, url
        self.main_frame = object()
        self.closed = False

    def is_closed(self):
        return self.closed

    async def goto(self, url, **kwargs):
        self.emit("request", SimpleNamespace(is_navigation_request=lambda: True, frame=self.main_frame))
        self.url = url
        self.emit("framenavigated", self.main_frame)
        self.emit("domcontentloaded")

    async def close(self):
        self.closed = True
        self.emit("close")


class Context(Events):
    def __init__(self):
        super().__init__()
        self.pages = []
        self.cookies = AsyncMock(return_value=[])
        self.add_cookies = AsyncMock()
        self.route = AsyncMock()
        self.add_init_script = AsyncMock()
        self.set_extra_http_headers = AsyncMock()
        self.set_default_timeout = Mock()
        self.closed = False

    async def new_page(self):
        page = Page(self)
        self.pages.append(page)
        return page

    async def close(self):
        for page in self.pages:
            await page.close()
        self.closed = True
        self.emit("close")


class Browser:
    def __init__(self):
        self.contexts, self.options = [], []
        self.close = AsyncMock()

    def is_connected(self):
        return True

    async def new_context(self, **options):
        context = Context()
        self.contexts.append(context)
        self.options.append(options)
        return context


@pytest.fixture
def shared_pool(monkeypatch):
    from executor import tiktok_client
    from services import proxy_concurrency_service
    monkeypatch.setattr(browser_pool.settings, "BROWSER_POOL_SHARED_PROCESS", True)
    monkeypatch.setattr(browser_pool.settings, "USE_PROXY", True)
    monkeypatch.setattr(browser_pool.settings, "BROWSER_POOL_CAPACITY_WAIT_SECONDS", 0.02)
    monkeypatch.setattr(tiktok_client, "_get_cookies_for_account", lambda _: "[]")
    monkeypatch.setattr(tiktok_client, "parse_cookies", lambda _: [])
    monkeypatch.setattr(browser_pool, "install_device_profile", AsyncMock())
    monkeypatch.setattr(browser_pool, "prepare_device_page", AsyncMock())
    monkeypatch.setattr(browser_pool, "release_device_page", AsyncMock())
    monkeypatch.setattr(browser_pool, "_log_page_window_metrics", AsyncMock())
    monkeypatch.setattr(proxy_concurrency_service, "acquire_proxy_usage", AsyncMock(return_value=None))
    pool = BrowserPool(max_browsers=2)
    pool._persist_context_cookies_before_close = AsyncMock()
    launch = AsyncMock(side_effect=lambda **_: Browser())
    pool._playwright = SimpleNamespace(chromium=SimpleNamespace(launch=launch))
    return pool, launch


def account(account_id, proxy):
    return SimpleNamespace(id=account_id, proxy=proxy, cookies="[]", cookie_file=None,
                           country_code=None, user_agent=None, label="fixture")


@pytest.mark.asyncio
async def test_shared_browser_routes_different_proxies_per_context(shared_pool):
    pool, launch = shared_pool
    try:
        first, _ = await pool.get_page_for_account(account(1, "http://first.invalid:8080"))
        second, _ = await pool.get_page_for_account(account(2, "http://second.invalid:8081"))
        browser = pool._browsers["shared_proxied"]
        assert first is not second and launch.await_count == 1
        assert [options["proxy"]["server"] for options in browser.options] == [
            "http://first.invalid:8080", "http://second.invalid:8081"]
        assert launch.await_args.kwargs["proxy"] == {"server": "http://127.0.0.1:1"}
        assert not any(arg.startswith("--window-size=") for arg in launch.await_args.kwargs["args"])
        assert first._account_proxy_key != second._account_proxy_key
        browser.close.assert_not_awaited()
        assert await pool._cleanup_unused_browsers(reserve=10) == 0
        browser.close.assert_not_awaited()
    finally:
        await pool.close_all()


@pytest.mark.asyncio
async def test_shared_context_capacity_waits_then_reuses_process(shared_pool):
    pool, launch = shared_pool
    pool.max_browsers = 1
    try:
        _, page = await pool.get_page_for_account(account(1, "http://first.invalid:8080"))
        with pytest.raises(browser_pool.BrowserPoolCapacityError):
            await pool.get_page_for_account(account(2, "http://second.invalid:8080"))
        assert not pool._context_reservations
        await pool.return_page_to_pool(page)
        await pool.get_page_for_account(account(2, "http://second.invalid:8080"))
        assert launch.await_count == 1
        pool._browsers["shared_proxied"].close.assert_not_awaited()
    finally:
        await pool.close_all()


@pytest.mark.asyncio
async def test_shared_proxy_reassignment_replaces_only_context(shared_pool):
    pool, launch = shared_pool
    acc = account(1, "http://first.invalid:8080")
    try:
        original, page = await pool.get_page_for_account(acc)
        await pool.return_page_to_pool(page)
        acc.proxy = "http://replacement.invalid:8080"
        replacement, _ = await pool.get_page_for_account(acc)
        assert original.closed and replacement is not original
        assert replacement._account_proxy_key == pool._make_browser_id(acc.proxy)
        assert launch.await_count == 1
        pool._browsers["shared_proxied"].close.assert_not_awaited()
    finally:
        await pool.close_all()


@pytest.mark.asyncio
async def test_failed_new_context_does_not_restart_another_busy_context(shared_pool):
    pool, _ = shared_pool
    try:
        original, page = await pool.get_page_for_account(account(1, "http://first.invalid:8080"))
        browser = pool._browsers["shared_proxied"]
        browser.new_context = AsyncMock(side_effect=RuntimeError("Target page, context or browser has been closed"))
        with pytest.raises(browser_pool.TikTokClientError, match="Too many retries"):
            await pool.get_page_for_account(account(2, "http://second.invalid:8080"))
        browser.close.assert_not_awaited()
        assert not original.closed and not page.is_closed()
        assert pool._contexts[1] is original and not pool._context_reservations
    finally:
        await pool.close_all()


@pytest.mark.asyncio
async def test_shared_socks_bridge_released_after_last_context(shared_pool, monkeypatch):
    pool, _ = shared_pool
    acquire, release = Mock(return_value=12000), Mock()
    monkeypatch.setattr(browser_pool, "get_local_http_proxy_for_socks5", acquire)
    monkeypatch.setattr(browser_pool, "release_local_http_proxy_for_socks5", release)
    proxy = "socks5://user:pass@proxy.invalid:1080"
    try:
        first, _ = await pool.get_page_for_account(account(1, proxy))
        second, _ = await pool.get_page_for_account(account(2, proxy))
        assert first._leased_socks5_proxy_source == second._leased_socks5_proxy_source == proxy
        assert pool._browsers["shared_proxied"].options[0]["proxy"] == {"server": "http://127.0.0.1:12000"}
        acquire.assert_called_once_with(proxy)
        await pool.close_context_for_account(1)
        release.assert_not_called()
        await pool.close_context_for_account(2)
        release.assert_called_once_with(proxy)
    finally:
        await pool.close_all()


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", [RuntimeError("context setup failed"), asyncio.CancelledError()])
async def test_shared_socks_bridge_released_when_context_creation_fails(shared_pool, monkeypatch, failure):
    pool, launch = shared_pool
    release = Mock()
    monkeypatch.setattr(browser_pool, "get_local_http_proxy_for_socks5", Mock(return_value=12000))
    monkeypatch.setattr(browser_pool, "release_local_http_proxy_for_socks5", release)
    browser = Browser()
    browser.new_context = AsyncMock(side_effect=failure)
    launch.side_effect = None
    launch.return_value = browser
    proxy = "socks5://user:pass@proxy.invalid:1080"
    try:
        with pytest.raises(type(failure) if isinstance(failure, asyncio.CancelledError) else browser_pool.TikTokClientError):
            await pool.get_page_for_account(account(1, proxy))
        release.assert_called_once_with(proxy)
        assert not pool._context_socks5_leases and not pool._context_reservations
        browser.close.assert_not_awaited()
    finally:
        await pool.close_all()


@pytest.mark.asyncio
async def test_shared_capacity_wakes_on_page_close(shared_pool):
    pool, _ = shared_pool
    pool.max_browsers = 1
    try:
        _, page = await pool.get_page_for_account(account(1, "http://first.invalid:8080"))
        waiting = asyncio.create_task(pool.get_page_for_account(account(2, "http://second.invalid:8080")))
        await asyncio.sleep(0)
        assert not waiting.done()
        await page.close()
        context, _ = await waiting
        assert context._pool_account_id == 2
    finally:
        await pool.close_all()


@pytest.mark.asyncio
async def test_capacity_waiter_does_not_hold_shared_process_lock(shared_pool, monkeypatch):
    pool, _ = shared_pool
    pool.max_browsers = 1
    monkeypatch.setattr(browser_pool.settings, "BROWSER_POOL_CAPACITY_WAIT_SECONDS", 1.0)
    first = account(1, "http://first.invalid:8080")
    waiting = None
    try:
        _, page = await pool.get_page_for_account(first)
        waiting = asyncio.create_task(pool.get_page_for_account(account(2, "http://second.invalid:8080")))
        await asyncio.sleep(0)
        # A second page in the already busy context consumes no extra context
        # slot and must not be stalled behind another account's capacity wait.
        context, second_page = await asyncio.wait_for(pool.get_page_for_account(first), timeout=0.2)
        assert second_page is not page and context._pool_account_id == first.id
        assert not waiting.done()
    finally:
        if waiting is not None:
            waiting.cancel()
            await asyncio.gather(waiting, return_exceptions=True)
        await pool.close_all()


@pytest.mark.asyncio
async def test_closing_busy_context_holds_capacity_until_disposal_finishes(shared_pool, monkeypatch):
    pool, _ = shared_pool
    pool.max_browsers = 1
    monkeypatch.setattr(browser_pool.settings, "BROWSER_POOL_CAPACITY_WAIT_SECONDS", 1.0)
    entered, release = asyncio.Event(), asyncio.Event()
    closing = waiting = None

    async def persist(*_):
        entered.set()
        await release.wait()

    try:
        await pool.get_page_for_account(account(1, "http://first.invalid:8080"))
        pool._persist_context_cookies_before_close.side_effect = persist
        closing = asyncio.create_task(pool.close_context_for_account(1))
        await entered.wait()
        waiting = asyncio.create_task(pool.get_page_for_account(account(2, "http://second.invalid:8080")))
        await asyncio.sleep(0)
        assert pool._disposing_busy_context_ids == {1} and not waiting.done()
        release.set()
        await closing
        context, _ = await waiting
        assert context._pool_account_id == 2 and not pool._disposing_busy_context_ids
    finally:
        release.set()
        for task in (closing, waiting):
            if task is not None:
                await asyncio.gather(task, return_exceptions=True)
        await pool.close_all()


@pytest.mark.asyncio
async def test_shared_direct_accounts_use_separate_proxyless_process(shared_pool, monkeypatch):
    pool, launch = shared_pool
    try:
        await pool.get_page_for_account(account(1, "http://first.invalid:8080"))
        monkeypatch.setattr(browser_pool.settings, "USE_PROXY", False)
        await pool.get_page_for_account(account(2, None))
        assert set(pool._browsers) == {"direct", "shared_proxied"}
        assert launch.await_count == 2
        assert "--no-proxy-server" in launch.await_args.kwargs["args"]
        assert "proxy" not in pool._browsers["direct"].options[0]
    finally:
        await pool.close_all()


@pytest.mark.parametrize("url", [
    ASSET.replace("/assets/", "/api/"), ASSET + "?msToken=secret", ASSET + "?X-Bogus=secret",
    ASSET.replace("sf16.", "mon."), ASSET.replace("sf16.", "mcs."),
    ASSET.replace("sf16.", "log."), ASSET.replace("tiktokcdn.com", "eviltiktokcdn.com"),
    ASSET.replace("tiktokcdn.com", "tiktokcdn.com.evil.invalid"),
    ASSET.replace("/assets/", "/passport/"), ASSET.replace("/assets/", "/aweme/"),
    ASSET + "?verifyFp=secret", ASSET + "?%64evice_id=secret",
])
def test_cache_rejects_dynamic_and_untrusted_urls(url):
    assert not is_cacheable_static_asset("GET", "script", url)
    assert not StaticAssetCache().put(url, 200, {}, b"x" * 100)


def test_cache_lru_limits_and_response_headers():
    cache = StaticAssetCache(max_total_bytes=200, max_item_bytes=150)
    assert cache.put(ASSET, 200, {"Content-Encoding": "gzip"}, b"a" * 100)
    second, third = ASSET + "?v=2", ASSET + "?v=3"
    assert cache.put(second, 200, {}, b"b" * 100)
    cache.get(ASSET)
    assert cache.put(third, 200, {}, b"c" * 100)
    assert not cache.has(second) and cache.total_bytes == 200
    entry = cache.get(ASSET)
    assert entry.headers["access-control-allow-origin"] == "*"
    assert entry.headers["access-control-allow-credentials"] == "true"
    assert entry.headers["timing-allow-origin"] == "*"
    assert entry.headers["content-type"] == "application/javascript; charset=utf-8"
    assert "set-cookie" not in entry.headers and "content-encoding" not in entry.headers
    for status, body in [(201, b"x" * 100), (200, b"x" * 63), (200, b"x" * 151)]:
        assert not cache.put(ASSET, status, {}, body)
    assert not is_cacheable_static_asset("POST", "script", ASSET)
    assert not is_cacheable_static_asset("GET", "fetch", ASSET)


@pytest.mark.parametrize("headers", [
    {"Set-Cookie": "secret=1"}, {"Cache-Control": "private, max-age=60"},
    {"Cache-Control": "no-store"}, {"Vary": "Accept-Encoding, Cookie"},
    {"Content-Type": "text/html"},
])
def test_cache_rejects_personalized_or_non_asset_responses(headers):
    assert not StaticAssetCache().put(ASSET, 200, headers, b"x" * 100)


@pytest.mark.asyncio
@pytest.mark.parametrize("resource_type,extension", [("script", ".js"), ("stylesheet", ".css")])
async def test_cache_populates_passively_and_continues_native_misses(monkeypatch, resource_type, extension):
    cache = StaticAssetCache()
    monkeypatch.setattr(browser_pool, "static_asset_cache", cache)
    monkeypatch.setattr(browser_pool.settings, "STATIC_ASSET_CACHE_ENABLED", True)
    monkeypatch.setattr(browser_pool.settings, "BLOCK_STYLESHEETS", False)
    context = Context()
    await browser_pool._install_resource_blocking_routes(context)
    handler = context.route.await_args.args[1]
    request = SimpleNamespace(method="GET", resource_type=resource_type, url=ASSET.replace(".js", extension))
    route = SimpleNamespace(request=request, continue_=AsyncMock(), fetch=AsyncMock(), fulfill=AsyncMock())
    await handler(route)
    route.continue_.assert_awaited_once()
    route.fetch.assert_not_awaited()
    response = SimpleNamespace(request=request, url=request.url, status=200, headers={}, body=AsyncMock(return_value=b"x" * 100))
    context.emit("response", response)
    assert not cache.has(request.url)
    await asyncio.gather(*context._static_asset_cache_tasks)
    assert cache.has(request.url)
    await handler(route)
    route.fulfill.assert_awaited_once()
    assert route.fulfill.await_args.kwargs["headers"]["access-control-allow-origin"] == "*"
    response.url = request.url.replace("/assets/", "/api/")
    context.emit("response", response)
    assert not context._static_asset_cache_tasks


@pytest.mark.asyncio
async def test_cache_background_reads_cancel_on_close_and_ignore_body_errors(monkeypatch):
    monkeypatch.setattr(browser_pool, "static_asset_cache", StaticAssetCache())
    monkeypatch.setattr(browser_pool.settings, "STATIC_ASSET_CACHE_ENABLED", True)
    context = Context()
    await browser_pool._install_resource_blocking_routes(context)
    response = SimpleNamespace(request=SimpleNamespace(method="GET", resource_type="script"),
                               url=ASSET, status=200, headers={}, body=AsyncMock(side_effect=RuntimeError("page closed")))
    context.emit("response", response)
    await asyncio.gather(*context._static_asset_cache_tasks)
    assert not browser_pool.static_asset_cache.has(ASSET)
    response.body = AsyncMock(side_effect=asyncio.Event().wait)
    context.emit("response", response)
    tasks = list(context._static_asset_cache_tasks)
    await asyncio.sleep(0)
    await context.close()
    await asyncio.gather(*tasks, return_exceptions=True)
    assert all(task.cancelled() for task in tasks)


@pytest.mark.asyncio
async def test_disabled_cache_leaves_network_and_responses_untouched(monkeypatch):
    cache = StaticAssetCache()
    cache.put(ASSET, 200, {}, b"x" * 100)
    monkeypatch.setattr(browser_pool, "static_asset_cache", cache)
    monkeypatch.setattr(browser_pool.settings, "STATIC_ASSET_CACHE_ENABLED", False)
    context = Context()
    await browser_pool._install_resource_blocking_routes(context)
    handler = context.route.await_args.args[1]
    request = SimpleNamespace(method="GET", resource_type="script", url=ASSET)
    route = SimpleNamespace(request=request, continue_=AsyncMock(), fulfill=AsyncMock())
    await handler(route)
    route.continue_.assert_awaited_once()
    route.fulfill.assert_not_awaited()
    response = SimpleNamespace(request=request, status=200, url=ASSET + "?v=2", body=AsyncMock())
    context.emit("response", response)
    response.body.assert_not_awaited()
    assert not context._static_asset_cache_tasks


@pytest.mark.parametrize("raw,expected", [
    (None, 0), ({}, 0), ("garbage", 0), ({"sessionid": "s"}, 0),
    ({"sessionid": "s", "ttwid": "w"}, 1), (HOT, 2), (json.dumps(HOT), 2),
    ("; ".join(f"{key}={value}" for key, value in HOT.items()), 2),
    ([{"name": key, "value": value} for key, value in HOT.items()], 2),
    ({"cookies": [{"name": key, "value": value} for key, value in HOT.items()]}, 2),
    ({**HOT, "msToken": "", "mstoken": "lowercase"}, 1),
    ({"sessionid_ss": "s", "ttwid": "w", "ctoken": "c", "msToken": "m"}, 2),
    ({"sid_guard": "a" * 32 + "|1700000000|100", "ttwid": "w", "tt_csrf_token": "c", "msToken": "m"}, 1),
    ({"sid_guard": "invalid", "ttwid": "w"}, 0),
])
def test_account_readiness_formats_and_guard_limits(raw, expected):
    assert classify_account_web_readiness(raw) == expected


def test_readiness_excludes_expired_sessions_and_synthesized_guard_aliases():
    from services.cookie_validator import normalize_cookie_records
    expired = [{"name": key, "value": value, **({"expires": 1} if key == "sessionid" else {})}
               for key, value in HOT.items()]
    assert classify_account_web_readiness(expired) == 0
    guard = {"sid_guard": "a" * 32 + "|1700000000|100", "ttwid": "w", "tt_csrf_token": "c", "msToken": "m"}
    normalized = normalize_cookie_records([{"name": key, "value": value} for key, value in guard.items()])
    assert normalized["sessionid"]
    assert classify_account_web_readiness(normalized) == 1


@pytest.fixture
def warmup_case(monkeypatch):
    from executor import user_schema
    from executor.http import playwright_http_client
    clock, sleeps = [0.0], []

    async def sleep(seconds):
        sleeps.append(seconds)
        clock[0] += seconds

    page = Page(Context(), URL)
    page.context.cookies.return_value = [{"name": name, "value": value} for name, value in HOT.items()]
    page.evaluate = AsyncMock(return_value={"current_url": URL, "navigation_url": URL, "age_ms": 0})
    page.wait_for_load_state = AsyncMock()
    monkeypatch.setattr(engagement_engine.time, "monotonic", lambda: clock[0])
    monkeypatch.setattr(engagement_engine.asyncio, "sleep", sleep)
    monkeypatch.setattr(engagement_engine, "wait_for_runtime_signer_ready", AsyncMock(return_value=True))
    monkeypatch.setattr(playwright_http_client, "_extract_runtime_profile", AsyncMock(return_value={}))
    monkeypatch.setattr(user_schema, "_visible_logged_out_reason", AsyncMock(return_value=None))
    monkeypatch.setattr(user_schema, "_fast_visible_captcha_reason", AsyncMock(return_value=None))
    monkeypatch.setattr(comment_discovery, "fetch_comment_page", AsyncMock(return_value={"comments": []}))
    return page, sleeps


@pytest.mark.asyncio
@pytest.mark.parametrize("hot,evidence,required", [
    (True, "signed", (800, 500)), (True, "cached", (800, 500)),
    (True, "sdk_only", (2000, 1500)), (False, "signed", (3200, 1800)),
    (False, "cached", (3200, 1800)), (False, "sdk_only", (3200, 1800)),
])
async def test_warmup_requires_real_traffic_for_fast_path(warmup_case, hot, evidence, required):
    page, sleeps = warmup_case
    if not hot:
        page.context.cookies.return_value = [c for c in page.context.cookies.return_value if c["name"] != "tt_csrf_token"]
    page._http_signer_state = {"sdk_present": True, "signed_dispatch_seen": evidence == "signed"}
    if evidence == "cached":
        page._cached_first_comment_page = {"comments": []}
        page._cached_first_comment_aweme_id = "7635267329407913230"
    _, _, info = await engagement_engine.warmup_page_for_http(page, URL, max_retries=1)
    assert (info["required_nav_age_ms"], info["required_dom_age_ms"]) == required
    assert info["minimum_age_wait_ms"] == required[0]
    assert sum(sleeps) >= required[0] / 1000


@pytest.mark.asyncio
async def test_imported_token_does_not_qualify_for_fast_path(warmup_case):
    page, _ = warmup_case
    remember_imported_ms_token(page.context, page.context.cookies.return_value)
    page._http_signer_state = {"signed_dispatch_seen": True}
    _, valid, info = await engagement_engine.warmup_page_for_http(page, URL, max_retries=1)
    assert valid
    assert info["required_nav_age_ms"] == 3200


@pytest.mark.asyncio
@pytest.mark.parametrize("new_url,recheck_ready,valid", [(URL + "?canonical=1", True, True),
    (URL, False, False), ("https://www.tiktok.com/login", True, False)])
async def test_finish_rebases_same_video_only_after_signer_recheck(warmup_case, monkeypatch, new_url, recheck_ready, valid):
    from executor import user_schema
    page, _ = warmup_case

    async def change_document(*args, **kwargs):
        await page.goto(new_url)
        return None

    monkeypatch.setattr(user_schema, "_visible_logged_out_reason", change_document)
    ready = AsyncMock(side_effect=lambda *args, timeout_ms: recheck_ready if timeout_ms == 1000 else True)
    monkeypatch.setattr(engagement_engine, "wait_for_runtime_signer_ready", ready)
    _, actual, info = await engagement_engine.warmup_page_for_http(page, URL, max_retries=1)
    assert actual is valid
    assert bool(info.get("document_rebased")) is valid
    if new_url.startswith(URL):
        assert any(call.kwargs["timeout_ms"] == 1000 for call in ready.await_args_list)


def test_document_generation_ignores_same_video_history_but_invalidates_reload():
    page = Page(Context(), URL)
    assert document_generation(page) == 0
    page.url = URL + "?canonical=1"
    page.emit("framenavigated", page.main_frame)
    assert document_generation(page) == 0
    page.emit("request", SimpleNamespace(is_navigation_request=lambda: True, frame=page.main_frame))
    assert document_generation(page) == 1
    page._cached_first_comment_page = {"comments": []}
    page.emit("framenavigated", page.main_frame)
    assert document_generation(page) == 2
    assert page._cached_first_comment_page is None


@pytest.mark.asyncio
async def test_initial_comment_response_is_captured_during_canonical_navigation(warmup_case):
    page, _ = warmup_case
    page.url = "about:blank"
    native_url = "https://www.tiktok.com/api/comment/list/?aweme_id=7635267329407913230&cursor=0"

    async def response_text():
        page.url = URL + "?canonical=1"
        page.emit("framenavigated", page.main_frame)
        page.emit("domcontentloaded")
        return '{"status_code":0,"comments":[]}'

    async def navigate(url, **kwargs):
        assert page._http_document_tracking
        assert page.listeners["response"]
        page.emit("request", SimpleNamespace(is_navigation_request=lambda: True, frame=page.main_frame))
        page.url = url
        page.emit("framenavigated", page.main_frame)
        before = asyncio.all_tasks()
        page.emit("response", SimpleNamespace(url=native_url, status=200, text=response_text))
        await asyncio.gather(*(asyncio.all_tasks() - before))

    page.goto = navigate
    _, valid, info = await engagement_engine.warmup_page_for_http(page, URL, max_retries=1)
    assert valid and info["comment_api_intercepted"]
    assert info["required_nav_age_ms"] == 800
    assert page._cached_first_comment_page == {"status_code": 0, "comments": []}


@pytest.mark.asyncio
@pytest.mark.parametrize("native_only", [False, True])
async def test_native_comment_timeout_falls_back_only_with_signer(monkeypatch, native_only):
    page = Page(Context(), URL)
    url = "https://www.tiktok.com/api/comment/list/?aweme_id=7635267329407913230&cursor=0"

    async def evaluate(script, *args, **kwargs):
        if "const allowDirectFetch = false;" in script:
            page.emit("request", SimpleNamespace(method="GET", url=url))
            return {"activation_only": True}
        return {"status": 200, "text": '{"comments": []}'}

    page.evaluate = AsyncMock(side_effect=evaluate)
    monkeypatch.setattr(comment_discovery, "_COMMENT_LIST_NATIVE_TIMEOUT_SECONDS", 0.001)
    meta = {}
    result = await comment_discovery._fetch_with_native_activation(
        page, comment_discovery._COMMENT_LIST_FETCH_JS, url, meta, native_only=native_only)
    if native_only:
        assert result["unavailable_reason"] == "native_comment_response_timeout"
        assert page.evaluate.await_count == 1
    else:
        assert result["status"] == 200 and meta["native_comment_response_timeout"]
        assert page.evaluate.await_count == 2
    assert all(callback.__name__ != "on_request" for callback in page.listeners["request"])
    assert not page.listeners["response"]


@pytest.mark.parametrize("scope", ["main", "reply"])
def test_account_selection_prioritizes_hot_candidates_across_batches(db_session, monkeypatch, scope):
    from db.models import Account
    from services import account_service
    from executor import user_schema
    monkeypatch.setattr(account_service.settings, "USE_PROXY", False)
    account_service._MAIN_ACCOUNT_RESERVED_UNTIL.clear()
    account_service._REPLY_ACCOUNT_RESERVED_UNTIL.clear()
    try:
        cold = [Account(label=f"warm-{i}", status="active", cookies=json.dumps({"sessionid": "s", "ttwid": "w"}),
                        last_validation_ok=datetime.now(timezone.utc), error_count_recent=0) for i in range(25)]
        hot = Account(label="hot", status="active", cookies=json.dumps(HOT), error_count_recent=1)
        db_session.add_all([*cold, hot])
        db_session.commit()
        selector = user_schema.get_free_account_for_main_comment if scope == "main" else user_schema.get_free_account_for_reply
        selected = selector(db_session)
        assert selected.id == hot.id
    finally:
        account_service._MAIN_ACCOUNT_RESERVED_UNTIL.clear()
        account_service._REPLY_ACCOUNT_RESERVED_UNTIL.clear()


@pytest.mark.asyncio
async def test_existing_shared_page_reuse_bypasses_slow_unrelated_context_setup(shared_pool):
    pool, _ = shared_pool
    first = account(1, "http://first.invalid:8080")
    blocker_started = asyncio.Event()
    unblock = asyncio.Event()
    second_task = None
    original_cleanup = pool._cleanup_old_contexts

    try:
        first_context, first_page = await pool.get_page_for_account(first)
        # Simulate an already returned, idle page.
        first_page._pool_available = True

        async def stalled_cleanup():
            if asyncio.current_task() is second_task:
                blocker_started.set()
                await unblock.wait()
            await original_cleanup()

        pool._cleanup_old_contexts = stalled_cleanup
        second_task = asyncio.create_task(pool.get_page_for_account(
            account(2, "http://second.invalid:8080")
        ))
        await asyncio.wait_for(blocker_started.wait(), timeout=2.0)
        assert pool._browser_id_lock("shared_proxied").locked()
        context, page = await asyncio.wait_for(pool.get_page_for_account(first), timeout=0.2)
        assert context is first_context and page is first_page
        assert not page._pool_available
        assert not second_task.done()
    finally:
        unblock.set()
        if second_task is not None:
            await asyncio.gather(second_task, return_exceptions=True)
        await pool.close_all()


@pytest.mark.asyncio
async def test_shared_existing_process_allows_parallel_new_account_contexts(shared_pool):
    pool, launch = shared_pool
    pool.max_browsers = 3
    slow_task = None
    entered = asyncio.Event()
    release = asyncio.Event()
    original_cleanup = pool._cleanup_old_contexts

    try:
        await pool.get_page_for_account(account(1, "http://owner.invalid:8080"))

        async def slow_cleanup():
            if asyncio.current_task() is slow_task:
                entered.set()
                await release.wait()
            await original_cleanup()

        pool._cleanup_old_contexts = slow_cleanup
        slow_task = asyncio.create_task(pool.get_page_for_account(
            account(2, "http://slow.invalid:8080")
        ))
        await asyncio.wait_for(entered.wait(), timeout=2.0)
        context, page = await asyncio.wait_for(pool.get_page_for_account(
            account(3, "http://fast.invalid:8080")
        ), timeout=0.5)
        assert context._pool_account_id == 3 and page is not None
        assert launch.await_count == 1
        assert not slow_task.done()
    finally:
        release.set()
        if slow_task is not None:
            await asyncio.gather(slow_task, return_exceptions=True)
        await pool.close_all()
