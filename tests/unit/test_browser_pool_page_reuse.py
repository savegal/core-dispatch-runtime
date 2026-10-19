import asyncio
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from executor import browser_pool
from executor.browser_pool import BrowserPool


@pytest.fixture(autouse=True)
def proxy_mode(monkeypatch):
    monkeypatch.setattr(browser_pool.settings, "USE_PROXY", True)


class DummyPage:
    def __init__(self, url: str = "https://www.tiktok.com/@demo/video/1"):
        self.url = url
        self._closed = False
        self._managed_by_pool = True
        self._pool_account_id = 1
        self._pool_available = False

    def is_closed(self):
        return self._closed

    async def close(self):
        self._closed = True

    async def goto(self, url, wait_until=None, timeout=None):
        self.url = url


class DummyContext:
    def __init__(self, pages=None):
        self.pages = list(pages or [])
        self._closed = False

    async def new_page(self):
        page = DummyPage(url="about:blank")
        page.context = self
        self.pages.append(page)
        return page

    async def close(self):
        self._closed = True


class LaunchProbePage(DummyPage):
    async def evaluate(self, _script):
        return {
            "innerWidth": 1920,
            "innerHeight": 960,
            "outerWidth": 1936,
            "outerHeight": 1048,
            "devicePixelRatio": 1,
            "screenWidth": 1920,
            "screenHeight": 960,
            "docClientWidth": 1920,
            "docScrollWidth": 1920,
        }


class LaunchProbeContext:
    def __init__(self):
        self.pages = []
        self._closed = False
        self.init_scripts = []

    def on(self, *_args):
        pass

    async def new_cdp_session(self, _page):
        from unittest.mock import AsyncMock, Mock
        return SimpleNamespace(send=AsyncMock(), on=Mock())

    def set_default_timeout(self, _timeout):
        return None

    async def add_init_script(self, _script):
        self.init_scripts.append(_script)

    async def set_extra_http_headers(self, _headers):
        return None

    async def add_cookies(self, _cookies):
        return None

    async def route(self, _pattern, _handler):
        return None

    async def new_page(self):
        page = LaunchProbePage(url="about:blank")
        page.context = self
        page.on = lambda *_: None
        self.pages.append(page)
        return page

    async def close(self):
        self._closed = True


class LaunchProbeBrowser:
    def __init__(self):
        self.contexts = []
        self.new_context_options = []
        self.closed = False

    def is_connected(self):
        return not self.closed

    async def new_context(self, **_options):
        self.new_context_options.append(dict(_options))
        context = LaunchProbeContext()
        self.contexts.append(context)
        return context

    async def close(self):
        self.closed = True


class WarmupProbeContext(LaunchProbeContext):
    def __init__(self):
        super().__init__()
        self.events = []
        self.add_cookie_calls = 0

    async def route(self, _pattern, _handler):
        self.events.append("route")

    async def add_cookies(self, _cookies):
        self.add_cookie_calls += 1
        self.events.append("add_cookies")
        if self.add_cookie_calls == 1:
            raise RuntimeError("domain warmup required")

    async def new_page(self):
        self.events.append("new_page")
        return await super().new_page()


class WarmupProbeBrowser(LaunchProbeBrowser):
    async def new_context(self, **_options):
        self.new_context_options.append(dict(_options))
        context = WarmupProbeContext()
        self.contexts.append(context)
        return context


@pytest.fixture
def isolated_acquire(monkeypatch):
    from executor import tiktok_client

    monkeypatch.setattr(tiktok_client, "_get_cookies_for_account", lambda _account: "[]")
    monkeypatch.setattr(tiktok_client, "parse_cookies", lambda _raw: [])
    monkeypatch.setattr(browser_pool.settings, "HUMAN_LIKE_BEHAVIOR", False)
    return SimpleNamespace(id=1, label="driver-test", proxy=None, user_agent=None, country_code="US")


@pytest.mark.asyncio
async def test_direct_mode_ignores_saved_proxy_in_browser_launch(monkeypatch, isolated_acquire):
    from unittest.mock import AsyncMock

    monkeypatch.setattr(browser_pool.settings, "USE_PROXY", False)
    isolated_acquire.proxy = "http://unused.invalid:9999"
    launch = AsyncMock(return_value=LaunchProbeBrowser())
    pool = BrowserPool(max_browsers=1)
    pool._playwright = SimpleNamespace(chromium=SimpleNamespace(launch=launch))

    await pool.get_page_for_account(isolated_acquire)

    assert "proxy" not in launch.await_args.kwargs
    assert "--no-proxy-server" in launch.await_args.kwargs["args"]
    assert pool._account_browser_ids[isolated_acquire.id] == "direct"
    assert isolated_acquire.proxy is None
    await pool.close_all()


@pytest.mark.asyncio
async def test_acquire_restarts_dead_driver_on_same_account(monkeypatch, isolated_acquire):
    from unittest.mock import AsyncMock

    pool = BrowserPool(max_browsers=1)
    dead_browser = LaunchProbeBrowser()
    dead_browser.new_context = AsyncMock(side_effect=RuntimeError(
        "Browser.new_context: Connection closed while reading from the driver"
    ))
    old = SimpleNamespace(chromium=SimpleNamespace(launch=AsyncMock(return_value=dead_browser)), stop=AsyncMock())
    healthy_browser = LaunchProbeBrowser()
    fresh = SimpleNamespace(chromium=SimpleNamespace(launch=AsyncMock(return_value=healthy_browser)), stop=AsyncMock())
    starter = SimpleNamespace(start=AsyncMock(return_value=fresh))
    monkeypatch.setattr(browser_pool, "async_playwright", lambda: starter)
    pool._playwright = old
    pool._register_browser("direct", dead_browser, None)

    context, page = await pool.get_page_for_account(isolated_acquire)

    assert page._pool_account_id == isolated_acquire.id
    assert pool._playwright is fresh
    assert pool._browsers == {"direct": healthy_browser}
    assert pool._contexts == {1: context}
    old.stop.assert_awaited_once()
    starter.start.assert_awaited_once()
    dead_browser.new_context.assert_awaited_once()
    assert pool._browser_inflight_acquires == {}


@pytest.mark.asyncio
async def test_acquire_discards_disconnected_browser_with_cached_contexts(monkeypatch, isolated_acquire):
    from unittest.mock import AsyncMock

    pool = BrowserPool(max_browsers=1)
    dead_browser = LaunchProbeBrowser()
    dead_browser.closed = True
    stale_context = LaunchProbeContext()
    dead_browser.contexts.append(stale_context)  # This property does not raise.
    healthy_browser = LaunchProbeBrowser()
    runtime = SimpleNamespace(chromium=SimpleNamespace(launch=AsyncMock(return_value=healthy_browser)), stop=AsyncMock())
    pool._playwright = runtime
    pool._register_browser("direct", dead_browser, None)
    pool._contexts[1] = stale_context
    pool._account_browser_ids[1] = "direct"
    pool._context_last_used[1] = datetime.now(timezone.utc)

    context, _page = await pool.get_page_for_account(isolated_acquire)

    assert context is not stale_context
    assert pool._browsers["direct"] is healthy_browser
    assert pool._contexts[1] is context
    runtime.stop.assert_not_awaited()


@pytest.mark.asyncio
async def test_acquire_driver_recovery_is_bounded(monkeypatch, isolated_acquire):
    from unittest.mock import AsyncMock

    pool = BrowserPool(max_browsers=1)
    runtimes = []

    async def start():
        chromium = SimpleNamespace(launch=AsyncMock(side_effect=RuntimeError(
            "BrowserType.launch: Connection closed while reading from the driver"
        )))
        runtime = SimpleNamespace(chromium=chromium, stop=AsyncMock())
        runtimes.append(runtime)
        return runtime

    monkeypatch.setattr(browser_pool, "async_playwright", lambda: SimpleNamespace(start=start))
    with pytest.raises(browser_pool.TikTokClientError, match="Browser driver unavailable after recovery"):
        await pool.get_page_for_account(isolated_acquire)
    assert len(runtimes) == 4
    assert pool._playwright is None
    assert pool._browsers == {}
    assert pool._browser_inflight_acquires == {}


@pytest.mark.asyncio
async def test_concurrent_runtime_start_and_late_disconnect_preserve_replacement(monkeypatch):
    from unittest.mock import AsyncMock

    pool = BrowserPool(max_browsers=1)
    fresh = SimpleNamespace(chromium=object(), stop=AsyncMock())
    old = SimpleNamespace(chromium=object(), stop=AsyncMock())

    async def start():
        await asyncio.sleep(0.01)
        return fresh

    starter = SimpleNamespace(start=AsyncMock(side_effect=start))
    monkeypatch.setattr(browser_pool, "async_playwright", lambda: starter)
    pool._playwright = old
    await asyncio.gather(pool._reset_disconnected_runtime(old), pool._reset_disconnected_runtime(old))
    await asyncio.gather(pool._ensure_playwright(), pool._ensure_playwright())
    healthy_context = LaunchProbeContext()
    pool._contexts[2] = healthy_context
    await pool._reset_disconnected_runtime(old)

    old.stop.assert_awaited_once()
    fresh.stop.assert_not_awaited()
    starter.start.assert_awaited_once()
    assert pool._playwright is fresh
    assert pool._contexts[2] is healthy_context


@pytest.mark.unit
def test_browser_pool_honors_single_browser_limit():
    pool = BrowserPool(max_browsers=1)

    assert pool.max_browsers == 1
    assert pool._max_pool_size == 2


@pytest.mark.unit
def test_browser_pool_capacity_can_be_raised_for_parallel_mode():
    pool = BrowserPool(max_browsers=1)

    pool.ensure_capacity(3)

    assert pool.max_browsers == 3
    assert pool._max_pool_size == 6


@pytest.mark.unit
def test_resource_blocking_honors_media_toggle(monkeypatch):
    monkeypatch.setattr(browser_pool.settings, "BLOCK_IMAGES", True, raising=False)
    monkeypatch.setattr(browser_pool.settings, "BLOCK_MEDIA", False, raising=False)
    monkeypatch.setattr(browser_pool.settings, "BLOCK_FONTS", True, raising=False)
    monkeypatch.setattr(browser_pool.settings, "BLOCK_STYLESHEETS", False, raising=False)

    assert browser_pool._should_abort_resource("image") is True
    assert browser_pool._should_abort_resource("font") is True
    assert browser_pool._should_abort_resource("stylesheet") is False
    assert browser_pool._should_abort_resource("media") is False
    assert browser_pool._should_abort_resource(
        "fetch",
        "https://v16-webapp-prime.tiktok.com/video/tos/example?mime_type=video_mp4",
    ) is False

    monkeypatch.setattr(browser_pool.settings, "BLOCK_MEDIA", True, raising=False)

    assert browser_pool._should_abort_resource("media") is True
    assert browser_pool._should_abort_resource(
        "fetch",
        "https://v16-webapp-prime.tiktok.com/video/tos/example?mime_type=video_mp4",
    ) is True
    assert browser_pool._should_abort_resource(
        "xhr",
        "https://www.tiktok.com/api/comment/list/?aweme_id=123",
    ) is False
    assert browser_pool._should_abort_resource(
        "fetch",
        "https://mcs.tiktokv.com/v1/list",
    ) is False
    assert browser_pool._should_abort_resource(
        "fetch",
        "https://mon.tiktokv.com/monitor_browser/collect/batch/",
    ) is False
    assert browser_pool._should_abort_resource(
        "fetch",
        "https://www.tiktok.com/api/recommend/item_list/?aid=1988",
    ) is False


@pytest.mark.unit
@pytest.mark.asyncio
@pytest.mark.parametrize("country_code, idc, proxy_country, locale, timezone_id", [
    ("US", None, None, "en-US", "America/New_York"),
    ("IL", None, None, "he-IL", "Asia/Jerusalem"),
    (None, "alisg", None, "en-SG", "Asia/Singapore"),
    ("US", "useast5", "PL", "pl-PL", "Europe/Warsaw"),
    (None, None, None, "en", "UTC"),
])
async def test_get_page_for_account_blocks_service_workers_when_media_blocked(
    monkeypatch, country_code, idc, proxy_country, locale, timezone_id,
):
    from executor import tiktok_client

    pool = BrowserPool(max_browsers=1)
    launched_browsers = []

    class FakeChromium:
        async def launch(self, **_launch_options):
            browser = LaunchProbeBrowser()
            launched_browsers.append(browser)
            return browser

    async def noop_initialize():
        return None

    monkeypatch.setattr(pool, "initialize", noop_initialize)
    monkeypatch.setattr(pool, "_playwright", SimpleNamespace(chromium=FakeChromium()))
    monkeypatch.setattr(tiktok_client, "_get_cookies_for_account", lambda _account: "[]")
    monkeypatch.setattr(tiktok_client, "parse_cookies", lambda _raw: [
        {"name": "tt-target-idc", "value": idc, "domain": ".tiktok.com", "path": "/"},
    ] if idc else [])
    monkeypatch.setattr(
        tiktok_client,
        "_normalize_proxy_for_playwright",
        lambda proxy: {"server": proxy} if proxy else None,
    )
    monkeypatch.setattr(browser_pool.settings, "BLOCK_MEDIA", True, raising=False)
    monkeypatch.setattr(browser_pool.settings, "HUMAN_LIKE_BEHAVIOR", False, raising=False)

    try:
        await pool.get_page_for_account(
            SimpleNamespace(id=1, label="a1", proxy="http://proxy-a:8000", user_agent=None,
                            country_code=country_code, proxy_country_code=proxy_country)
        )

        assert launched_browsers[0].new_context_options[0]["service_workers"] == "block"
        options = launched_browsers[0].new_context_options[0]
        if country_code or proxy_country or idc:
            assert options["locale"] == locale
            assert options["timezone_id"] == timezone_id
        else:
            assert "locale" not in options and "timezone_id" not in options
    finally:
        await pool.close_all()


@pytest.mark.unit
@pytest.mark.asyncio
async def test_resource_routes_install_before_cookie_domain_warmup(monkeypatch):
    from executor import tiktok_client

    pool = BrowserPool(max_browsers=1)
    launched_browsers = []

    class FakeChromium:
        async def launch(self, **_launch_options):
            browser = WarmupProbeBrowser()
            launched_browsers.append(browser)
            return browser

    async def noop_initialize():
        return None

    monkeypatch.setattr(pool, "initialize", noop_initialize)
    monkeypatch.setattr(pool, "_playwright", SimpleNamespace(chromium=FakeChromium()))
    monkeypatch.setattr(tiktok_client, "_get_cookies_for_account", lambda _account: "cookie-json")
    monkeypatch.setattr(
        tiktok_client,
        "parse_cookies",
        lambda _raw: [{"name": "sessionid", "value": "x", "domain": ".tiktok.com", "path": "/"}],
    )
    monkeypatch.setattr(
        tiktok_client,
        "_normalize_proxy_for_playwright",
        lambda proxy: {"server": proxy} if proxy else None,
    )
    monkeypatch.setattr(browser_pool.settings, "BLOCK_MEDIA", True, raising=False)
    monkeypatch.setattr(browser_pool.settings, "HUMAN_LIKE_BEHAVIOR", False, raising=False)

    try:
        await pool.get_page_for_account(
            SimpleNamespace(id=1, label="a1", proxy="http://proxy-a:8000", user_agent=None, country_code="US")
        )

        events = launched_browsers[0].contexts[0].events
        assert events.index("route") < events.index("new_page")
    finally:
        await pool.close_all()


@pytest.mark.unit
def test_desktop_viewport_size_uses_proxy_defaults(monkeypatch):
    monkeypatch.setattr(browser_pool.settings, "PLAYWRIGHT_DESKTOP_VIEWPORT_WIDTH", 1920, raising=False)
    monkeypatch.setattr(browser_pool.settings, "PLAYWRIGHT_DESKTOP_VIEWPORT_HEIGHT", 1080, raising=False)

    width, height = browser_pool._desktop_viewport_size_for_account(
        SimpleNamespace(proxy="http://proxy.example:8000")
    )

    assert (width, height) == (1920, 1080)


@pytest.mark.unit
def test_desktop_viewport_size_uses_proxyless_defaults(monkeypatch):
    monkeypatch.setattr(browser_pool.settings, "PLAYWRIGHT_PROXYLESS_VIEWPORT_WIDTH", 1280, raising=False)
    monkeypatch.setattr(browser_pool.settings, "PLAYWRIGHT_PROXYLESS_VIEWPORT_HEIGHT", 720, raising=False)

    width, height = browser_pool._desktop_viewport_size_for_account(SimpleNamespace(proxy=""))

    assert (width, height) == (1280, 720)


@pytest.mark.unit
def test_desktop_window_args_sync_window_and_viewport(monkeypatch):
    monkeypatch.setattr(browser_pool.settings, "PLAYWRIGHT_SYNC_WINDOW_WITH_VIEWPORT", True, raising=False)
    monkeypatch.setattr(browser_pool.settings, "PLAYWRIGHT_DESKTOP_VIEWPORT_WIDTH", 1920, raising=False)
    monkeypatch.setattr(browser_pool.settings, "PLAYWRIGHT_DESKTOP_VIEWPORT_HEIGHT", 960, raising=False)
    monkeypatch.setattr(browser_pool.settings, "PLAYWRIGHT_WINDOW_EXTRA_HEIGHT", 90, raising=False)

    args = browser_pool._desktop_window_args_for_account(
        SimpleNamespace(proxy="http://proxy.example:8000", user_agent="Mozilla/5.0 Windows")
    )

    assert "--window-size=1920,1050" in args
    assert "--force-device-scale-factor=1" in args


@pytest.mark.unit
def test_legacy_mobile_user_agent_still_gets_desktop_window(monkeypatch):
    monkeypatch.setattr(browser_pool.settings, "PLAYWRIGHT_SYNC_WINDOW_WITH_VIEWPORT", True, raising=False)

    args = browser_pool._desktop_window_args_for_account(
        SimpleNamespace(proxy="http://proxy.example:8000", user_agent="Mozilla/5.0 iPhone Mobile")
    )

    assert any(arg.startswith("--window-size=") for arg in args)
    assert "--force-device-scale-factor=1" in args


@pytest.mark.unit
def test_desktop_hardware_profiles_are_stable_and_vary_by_account():
    profiles = [browser_pool._desktop_hardware_profile_for_account(SimpleNamespace(id=i)) for i in range(1, 51)]
    for account_id, profile in enumerate(profiles, 1):
        assert profile == browser_pool._desktop_hardware_profile_for_account(SimpleNamespace(id=account_id, proxy="changed"))
        assert (profile["width"], profile["height"]) in browser_pool._DESKTOP_RESOLUTIONS
        assert profile["hardwareConcurrency"] in (4, 6, 8, 12, 16)
        assert profile["deviceMemory"] in (4, 8)
        assert (profile["webglVendor"], profile["webglRenderer"]) in browser_pool._DESKTOP_WEBGL_PAIRS
        assert "Direct3D11" in profile["webglRenderer"]
    for field in profiles[0]:
        assert len({profile[field] for profile in profiles}) > 1


@pytest.mark.unit
def test_desktop_hardware_profile_survives_different_python_hash_seeds():
    import json
    import os
    import subprocess
    import sys

    results = []
    for seed in ("1", "99"):
        child = subprocess.run([
            sys.executable, "-c",
            "import json; from types import SimpleNamespace; from executor.browser_pool import _desktop_hardware_profile_for_account; "
            "print(json.dumps(_desktop_hardware_profile_for_account(SimpleNamespace(id=1219))))",
        ], env={**os.environ, "PYTHONHASHSEED": seed}, capture_output=True, text=True, check=True, timeout=20)
        results.append(json.loads(child.stdout))
    assert results[0] == results[1]


@pytest.mark.asyncio
@pytest.mark.parametrize("human_like,mobile", [(False, False), (True, False), (True, True)])
async def test_account_context_installs_matching_desktop_hardware_only(monkeypatch, isolated_acquire, human_like, mobile):
    from unittest.mock import AsyncMock
    import json

    monkeypatch.setattr(browser_pool.settings, "USE_PROXY", False)
    monkeypatch.setattr(browser_pool.settings, "HUMAN_LIKE_BEHAVIOR", human_like)
    monkeypatch.setattr(browser_pool.settings, "PLAYWRIGHT_SYNC_WINDOW_WITH_VIEWPORT", True)
    monkeypatch.setattr(browser_pool.settings, "PLAYWRIGHT_WINDOW_EXTRA_HEIGHT", 90)
    devices = {}
    if mobile:
        isolated_acquire.user_agent = "Mozilla/5.0 iPhone Mobile"
        devices = {"iPhone 13": {
            "user_agent": isolated_acquire.user_agent,
            "viewport": {"width": 390, "height": 664},
            "screen": {"width": 390, "height": 844},
            "is_mobile": True, "has_touch": True, "device_scale_factor": 3,
            "default_browser_type": "webkit",
        }}
    browser = LaunchProbeBrowser()
    launch = AsyncMock(return_value=browser)
    pool = BrowserPool(max_browsers=1)
    pool._playwright = SimpleNamespace(chromium=SimpleNamespace(launch=launch), stop=AsyncMock(), devices=devices)
    try:
        context, _ = await pool.get_page_for_account(isolated_acquire)
        options = browser.new_context_options[0]
        profile = browser_pool._desktop_hardware_profile_for_account(isolated_acquire)
        hardware_scripts = [script for script in context.init_scripts if json.dumps(profile) in script]
        assert len(hardware_scripts) == 1
        assert not options["is_mobile"] and not options["has_touch"]
        assert "default_browser_type" not in options
        assert "timezone_id" not in options and "locale" not in options
        size = {"width": profile["width"], "height": profile["height"]}
        assert options["screen"] == size
        assert options["viewport"] == {"width": profile["width"], "height": profile["height"] - 90}
        assert f'--window-size={profile["width"]},{profile["height"]}' in launch.call_args.kwargs["args"]
    finally:
        await pool.close_all()


@pytest.mark.asyncio
async def test_hardware_script_installation_failure_closes_context_and_fails_acquire(monkeypatch, isolated_acquire):
    from unittest.mock import AsyncMock

    context = LaunchProbeContext()

    async def init(script):
        if "installAccountIdentity" in script:
            raise RuntimeError("hardware init installation failed")

    context.add_init_script = init
    browser = LaunchProbeBrowser()
    browser.contexts.append(context)
    browser.new_context = AsyncMock(return_value=context)
    pool = BrowserPool(max_browsers=1)
    pool._playwright = SimpleNamespace(chromium=SimpleNamespace(launch=AsyncMock(return_value=browser)), stop=AsyncMock())
    monkeypatch.setattr(browser_pool.settings, "USE_PROXY", False)
    try:
        with pytest.raises(Exception, match="hardware init installation failed"):
            await pool.get_page_for_account(isolated_acquire)
        assert context._closed
        assert isolated_acquire.id not in pool._contexts
        assert not context.pages
    finally:
        await pool.close_all()


@pytest.mark.unit
def test_thread_browser_pool_capacity_helper_creates_parallel_ready_pool(monkeypatch):
    monkeypatch.setattr(browser_pool._thread_local, "browser_pool", None, raising=False)

    pool = browser_pool.ensure_thread_browser_pool_capacity(3)

    assert pool.max_browsers == 3
    assert pool._max_pool_size == 6
    assert browser_pool._get_thread_local_pool() is pool
    monkeypatch.setattr(browser_pool._thread_local, "browser_pool", None, raising=False)


@pytest.mark.unit
@pytest.mark.asyncio
async def test_return_page_to_pool_marks_page_available():
    pool = BrowserPool(max_browsers=1)
    page = DummyPage()
    context = DummyContext([page])
    pool._contexts[1] = context
    pool._context_last_used[1] = datetime.now(timezone.utc)

    await pool.return_page_to_pool(page)

    assert page._pool_available is True
    assert page.url == "about:blank"
    assert page.is_closed() is False


@pytest.mark.unit
@pytest.mark.asyncio
async def test_cleanup_old_contexts_keeps_busy_pages_open():
    pool = BrowserPool(max_browsers=1)
    pool._context_ttl_seconds = 0
    active_page = DummyPage(url="https://www.tiktok.com/@demo/video/1")
    active_page._pool_available = False
    idle_page = DummyPage(url="about:blank")
    idle_page._pool_available = True
    active_context = DummyContext([active_page])
    idle_context = DummyContext([idle_page])
    old_time = datetime.now(timezone.utc) - timedelta(minutes=10)
    pool._contexts[1] = active_context
    pool._contexts[2] = idle_context
    pool._context_last_used[1] = old_time
    pool._context_last_used[2] = old_time

    await pool._cleanup_old_contexts()

    assert pool._contexts.get(1) is active_context
    assert active_context._closed is False
    assert active_page.is_closed() is False
    assert 2 not in pool._contexts
    assert idle_context._closed is True


@pytest.mark.unit
@pytest.mark.asyncio
async def test_close_page_from_pool_does_not_close_other_live_pages():
    pool = BrowserPool(max_browsers=1)
    target_page = DummyPage(url="https://www.tiktok.com/@demo/video/1")
    other_page = DummyPage(url="https://www.tiktok.com/@demo/video/2")
    context = DummyContext([target_page, other_page])
    pool._contexts[1] = context
    pool._context_last_used[1] = datetime.now(timezone.utc)

    closed = await pool.close_page_from_pool(target_page, reason="unit_test")

    assert closed is True
    assert target_page.is_closed() is True
    assert other_page.is_closed() is False
    assert context._closed is False
    assert pool._contexts[1] is context


@pytest.mark.unit
@pytest.mark.asyncio
async def test_close_already_closed_page_from_pool_keeps_other_live_pages():
    pool = BrowserPool(max_browsers=1)
    target_page = DummyPage(url="https://www.tiktok.com/@demo/video/1")
    other_page = DummyPage(url="https://www.tiktok.com/@demo/video/2")
    target_page._closed = True
    context = DummyContext([target_page, other_page])
    pool._contexts[1] = context
    pool._context_last_used[1] = datetime.now(timezone.utc)

    closed = await pool.close_page_from_pool(target_page, reason="unit_test_already_closed")

    assert closed is True
    assert target_page.is_closed() is True
    assert other_page.is_closed() is False
    assert context._closed is False
    assert pool._contexts[1] is context


@pytest.mark.unit
@pytest.mark.asyncio
async def test_close_page_from_pool_removes_empty_context_and_browser():
    pool = BrowserPool(max_browsers=1)
    target_page = DummyPage(url="https://www.tiktok.com/@demo/video/1")
    context = DummyContext([target_page])
    browser = LaunchProbeBrowser()
    pool._contexts[1] = context
    pool._context_last_used[1] = datetime.now(timezone.utc)
    pool._account_browser_ids[1] = "browser-1"
    pool._browsers["browser-1"] = browser

    closed = await pool.close_page_from_pool(target_page, reason="unit_test")

    assert closed is True
    assert target_page.is_closed() is True
    assert context._closed is True
    assert browser.closed is True
    assert 1 not in pool._contexts
    assert 1 not in pool._context_last_used
    assert 1 not in pool._account_browser_ids
    assert "browser-1" not in pool._browsers


@pytest.mark.unit
@pytest.mark.asyncio
async def test_close_page_from_pool_returns_false_when_page_stays_open():
    class CloseFailPage(DummyPage):
        async def close(self):
            raise RuntimeError("close failed")

    pool = BrowserPool(max_browsers=1)
    target_page = CloseFailPage(url="https://www.tiktok.com/@demo/video/1")
    context = DummyContext([target_page])
    pool._contexts[1] = context
    pool._context_last_used[1] = datetime.now(timezone.utc)

    closed = await pool.close_page_from_pool(target_page, reason="unit_test_close_error")

    assert closed is False
    assert target_page.is_closed() is False
    assert context._closed is False
    assert pool._contexts[1] is context


@pytest.mark.unit
@pytest.mark.asyncio
async def test_cleanup_unused_browsers_keeps_browser_with_live_context_even_if_mapping_missing():
    pool = BrowserPool(max_browsers=1)
    page = DummyPage(url="https://www.tiktok.com/@demo/video/1")
    context = DummyContext([page])
    browser = LaunchProbeBrowser()
    browser.contexts.append(context)
    pool._contexts[1] = context
    pool._context_last_used[1] = datetime.now(timezone.utc)
    pool._browsers["browser-1"] = browser

    closed_count = await pool._cleanup_unused_browsers()

    assert closed_count == 0
    assert browser.closed is False
    assert pool._browsers["browser-1"] is browser


@pytest.mark.unit
@pytest.mark.asyncio
async def test_cleanup_unused_browsers_keeps_browser_with_inflight_page_acquire():
    pool = BrowserPool(max_browsers=1)
    browser = LaunchProbeBrowser()
    pool._browsers["browser-1"] = browser
    pool._browser_inflight_acquires["browser-1"] = 1

    closed_count = await pool._cleanup_unused_browsers()

    assert closed_count == 0
    assert browser.closed is False
    assert pool._browsers["browser-1"] is browser


@pytest.mark.unit
@pytest.mark.asyncio
async def test_get_page_for_account_does_not_close_busy_pages_when_context_has_many_pages(monkeypatch):
    pool = BrowserPool(max_browsers=1)
    busy_pages = [
        DummyPage(url=f"https://www.tiktok.com/@demo/video/{index}")
        for index in range(3)
    ]
    for page in busy_pages:
        page._pool_available = False
    context = DummyContext(busy_pages)
    pool._contexts[1] = context
    pool._context_last_used[1] = datetime.now(timezone.utc)

    async def noop_initialize():
        return None

    monkeypatch.setattr(pool, "initialize", noop_initialize)

    reused_context, new_page = await pool.get_page_for_account(SimpleNamespace(id=1))

    assert reused_context is context
    assert new_page in context.pages
    assert new_page._pool_available is False
    assert all(page.is_closed() is False for page in busy_pages)


@pytest.mark.unit
@pytest.mark.asyncio
async def test_get_page_for_account_preserves_busy_context_when_new_page_fails(monkeypatch):
    class BrokenNewPageContext(DummyContext):
        async def new_page(self):
            raise RuntimeError("new page failed")

    pool = BrowserPool(max_browsers=1)
    busy_page = DummyPage(url="https://www.tiktok.com/@demo/video/1")
    busy_page._pool_available = False
    context = BrokenNewPageContext([busy_page])
    pool._contexts[1] = context
    pool._context_last_used[1] = datetime.now(timezone.utc)

    async def noop_initialize():
        return None

    monkeypatch.setattr(pool, "initialize", noop_initialize)

    with pytest.raises(browser_pool.TikTokClientError, match="busy"):
        await pool.get_page_for_account(SimpleNamespace(id=1))

    assert context._closed is False
    assert busy_page.is_closed() is False
    assert pool._contexts[1] is context


@pytest.mark.unit
@pytest.mark.asyncio
async def test_get_page_for_account_reuses_available_page(monkeypatch):
    pool = BrowserPool(max_browsers=1)
    page = DummyPage(url="about:blank")
    page._pool_available = True
    context = DummyContext([page])
    pool._contexts[1] = context
    pool._context_last_used[1] = datetime.now(timezone.utc)

    async def noop_initialize():
        return None

    monkeypatch.setattr(pool, "initialize", noop_initialize)

    reused_context, reused_page = await pool.get_page_for_account(SimpleNamespace(id=1))

    assert reused_context is context
    assert reused_page is page
    assert reused_page._pool_available is False


@pytest.mark.unit
@pytest.mark.asyncio
async def test_get_page_for_account_replaces_idle_context_after_proxy_change(monkeypatch):
    from executor import tiktok_client

    pool = BrowserPool(max_browsers=2)
    old_proxy = "http://old-proxy:8000"
    new_proxy = "http://new-proxy:9000"
    old_browser_id = pool._make_browser_id(old_proxy)
    old_page = DummyPage(url="about:blank")
    old_page._pool_available = True
    old_context = DummyContext([old_page])
    old_browser = LaunchProbeBrowser()
    old_browser.contexts.append(old_context)
    pool._contexts[1] = old_context
    pool._context_last_used[1] = datetime.now(timezone.utc)
    pool._account_browser_ids[1] = old_browser_id
    pool._browsers[old_browser_id] = old_browser
    pool._browser_proxy_keys[old_browser_id] = old_proxy
    launched_options = []

    class FakeChromium:
        async def launch(self, **options):
            launched_options.append(dict(options))
            return LaunchProbeBrowser()

    async def noop_initialize():
        return None

    monkeypatch.setattr(pool, "initialize", noop_initialize)
    monkeypatch.setattr(pool, "_playwright", SimpleNamespace(chromium=FakeChromium()))
    monkeypatch.setattr(tiktok_client, "_get_cookies_for_account", lambda _account: "[]")
    monkeypatch.setattr(tiktok_client, "parse_cookies", lambda _raw: [])
    monkeypatch.setattr(
        tiktok_client,
        "_normalize_proxy_for_playwright",
        lambda proxy: {"server": proxy} if proxy else None,
    )
    monkeypatch.setattr(browser_pool.settings, "HUMAN_LIKE_BEHAVIOR", False, raising=False)

    new_context, new_page = await pool.get_page_for_account(
        SimpleNamespace(
            id=1,
            label="a1",
            proxy=new_proxy,
            user_agent=None,
            country_code="US",
        )
    )

    assert old_context._closed is True
    assert old_browser.closed is True
    assert new_context is not old_context
    assert new_page not in old_context.pages
    assert launched_options[0]["proxy"]["server"] == new_proxy
    assert pool._account_browser_ids[1] == pool._make_browser_id(new_proxy)


@pytest.mark.unit
@pytest.mark.asyncio
async def test_get_page_for_account_refuses_proxy_change_while_context_busy(monkeypatch):
    pool = BrowserPool(max_browsers=2)
    old_proxy = "http://old-proxy:8000"
    busy_page = DummyPage(url="https://www.tiktok.com/@demo/video/1")
    busy_page._pool_available = False
    context = DummyContext([busy_page])
    pool._contexts[1] = context
    pool._context_last_used[1] = datetime.now(timezone.utc)
    pool._account_browser_ids[1] = pool._make_browser_id(old_proxy)

    async def noop_initialize():
        return None

    monkeypatch.setattr(pool, "initialize", noop_initialize)

    with pytest.raises(browser_pool.TikTokClientError, match="proxy changed.*busy"):
        await pool.get_page_for_account(
            SimpleNamespace(id=1, proxy="http://new-proxy:9000")
        )

    assert context._closed is False
    assert busy_page.is_closed() is False
    assert pool._contexts[1] is context


@pytest.mark.unit
@pytest.mark.asyncio
async def test_get_page_for_account_launches_distinct_browsers_concurrently(monkeypatch):
    from executor import tiktok_client

    pool = BrowserPool(max_browsers=2)
    active_launches = 0
    max_active_launches = 0

    class FakeChromium:
        async def launch(self, **_launch_options):
            nonlocal active_launches, max_active_launches
            active_launches += 1
            max_active_launches = max(max_active_launches, active_launches)
            await asyncio.sleep(0.02)
            active_launches -= 1
            return LaunchProbeBrowser()

    async def noop_initialize():
        return None

    monkeypatch.setattr(pool, "initialize", noop_initialize)
    monkeypatch.setattr(pool, "_playwright", SimpleNamespace(chromium=FakeChromium()))
    monkeypatch.setattr(tiktok_client, "_get_cookies_for_account", lambda _account: "[]")
    monkeypatch.setattr(tiktok_client, "parse_cookies", lambda _raw: [])
    monkeypatch.setattr(
        tiktok_client,
        "_normalize_proxy_for_playwright",
        lambda proxy: {"server": proxy} if proxy else None,
    )
    monkeypatch.setattr(browser_pool.settings, "HUMAN_LIKE_BEHAVIOR", False, raising=False)

    await asyncio.gather(
        pool.get_page_for_account(
            SimpleNamespace(id=1, label="a1", proxy="http://proxy-a:8000", user_agent=None, country_code="US")
        ),
        pool.get_page_for_account(
            SimpleNamespace(id=2, label="a2", proxy="http://proxy-b:8000", user_agent=None, country_code="US")
        ),
    )

    assert max_active_launches == 2


@pytest.mark.unit
@pytest.mark.asyncio
async def test_get_page_for_account_serializes_same_proxy_browser_launch(monkeypatch):
    from executor import tiktok_client

    pool = BrowserPool(max_browsers=2)
    active_launches = 0
    max_active_launches = 0
    launch_count = 0

    class FakeChromium:
        async def launch(self, **_launch_options):
            nonlocal active_launches, max_active_launches, launch_count
            active_launches += 1
            max_active_launches = max(max_active_launches, active_launches)
            await asyncio.sleep(0.02)
            active_launches -= 1
            launch_count += 1
            return LaunchProbeBrowser()

    async def noop_initialize():
        return None

    monkeypatch.setattr(pool, "initialize", noop_initialize)
    monkeypatch.setattr(pool, "_playwright", SimpleNamespace(chromium=FakeChromium()))
    monkeypatch.setattr(tiktok_client, "_get_cookies_for_account", lambda _account: "[]")
    monkeypatch.setattr(tiktok_client, "parse_cookies", lambda _raw: [])
    monkeypatch.setattr(
        tiktok_client,
        "_normalize_proxy_for_playwright",
        lambda proxy: {"server": proxy} if proxy else None,
    )
    monkeypatch.setattr(browser_pool.settings, "HUMAN_LIKE_BEHAVIOR", False, raising=False)

    await asyncio.gather(
        pool.get_page_for_account(
            SimpleNamespace(id=1, label="a1", proxy="http://proxy-a:8000", user_agent=None, country_code="US")
        ),
        pool.get_page_for_account(
            SimpleNamespace(id=2, label="a2", proxy="http://proxy-a:8000", user_agent=None, country_code="US")
        ),
    )

    assert max_active_launches == 1
    assert launch_count == 1
    assert len(pool._browsers) == 1


@pytest.mark.unit
@pytest.mark.asyncio
async def test_close_pages_for_task_does_not_close_other_task_pages():
    pool = BrowserPool(max_browsers=1)
    target_page = DummyPage(url="https://www.tiktok.com/@demo/video/1")
    other_page = DummyPage(url="https://www.tiktok.com/@demo/video/2")
    target_page._pool_task_id = 101
    other_page._pool_task_id = 202
    context = DummyContext([target_page, other_page])
    pool._contexts[1] = context
    pool._context_last_used[1] = datetime.now(timezone.utc)

    closed = await pool.close_pages_for_task(101, reason="unit_test")

    assert closed == 1
    assert target_page.is_closed() is True
    assert other_page.is_closed() is False
    assert context._closed is False
    assert pool._contexts[1] is context
