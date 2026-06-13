from datetime import datetime, timezone
from types import SimpleNamespace

import pytest

from executor import browser_pool
from executor.browser_pool import BrowserPool


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
        self.pages.append(page)
        return page

    async def close(self):
        self._closed = True


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
def test_mobile_window_args_do_not_force_desktop_window(monkeypatch):
    monkeypatch.setattr(browser_pool.settings, "PLAYWRIGHT_SYNC_WINDOW_WITH_VIEWPORT", True, raising=False)

    args = browser_pool._desktop_window_args_for_account(
        SimpleNamespace(proxy="http://proxy.example:8000", user_agent="Mozilla/5.0 iPhone Mobile")
    )

    assert args == []


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
