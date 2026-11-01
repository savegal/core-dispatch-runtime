"""Real browser checks for readiness reads slower than an individual poll."""
import asyncio
import ctypes
import gc
import os

import pytest
from patchright.async_api import async_playwright

from executor.browser_reads import _pending_reads
from executor.browser_pool import BrowserPool
from executor.http import signature_runtime
from executor.http.page_runtime import document_generation


pytestmark = [pytest.mark.integration, pytest.mark.asyncio]


@pytest.fixture
async def local_page():
    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch(headless=True)
        context = await browser.new_context()
        async def route(request):
            sdk = "<script>window.byted_acrawler={sign:()=>''};</script>" if '/video/1' in request.request.url else ''
            await request.fulfill(status=200, content_type="text/html", body="<html><body>fixture"+sdk+"</body></html>")
        await context.route("**/*", route)
        page = await context.new_page()
        await page.goto("https://www.tiktok.com/@fixture/video/1111111111111111111")
        try:
            yield page
        finally:
            await context.close()
            await browser.close()
            for _ in range(5):
                gc.collect()
                await asyncio.sleep(.02)


async def test_slow_real_driver_read_survives_several_short_polls(local_page, monkeypatch):
    page = local_page
    evaluate = page.evaluate
    evaluations = []
    async def slow(script, *args, **kwargs):
        if script == signature_runtime._SIGNER_READY_JS:
            evaluations.append(1)
            return await evaluate("""async () => {
                await new Promise(resolve => setTimeout(resolve, 500));
                return {sdk_present: typeof window.byted_acrawler?.sign === 'function', signed_dispatch_seen: false};
            }""", isolated_context=False)
        return await evaluate(script, *args, **kwargs)
    monkeypatch.setattr(page, "evaluate", slow)
    for _ in range(3):
        assert not await signature_runtime.wait_for_runtime_signer_ready(page, timeout_ms=50)
    assert await signature_runtime.wait_for_runtime_signer_ready(page, timeout_ms=2000)
    assert len(evaluations) == 1
    assert not _pending_reads


async def test_real_navigation_discards_the_old_document_probe(local_page, monkeypatch):
    page = local_page
    evaluate = page.evaluate
    async def slow(script, *args, **kwargs):
        if script == signature_runtime._SIGNER_READY_JS:
            return await evaluate("""async () => {
                await new Promise(resolve => setTimeout(resolve, 500));
                return {sdk_present: typeof window.byted_acrawler?.sign === 'function', signed_dispatch_seen: false};
            }""", isolated_context=False)
        return await evaluate(script, *args, **kwargs)
    monkeypatch.setattr(page, "evaluate", slow)
    assert not await signature_runtime.wait_for_runtime_signer_ready(page, timeout_ms=50)
    initial_generation = document_generation(page)
    await page.goto("https://www.tiktok.com/@fixture/video/2222222222222222222")
    assert document_generation(page) != initial_generation
    assert not await signature_runtime.wait_for_runtime_signer_ready(page, timeout_ms=800)
    assert not (getattr(page, "_http_signer_state", {}) or {}).get("sdk_present")


def process_is_running(pid):
    if os.name == "nt":
        from ctypes import wintypes
        kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
        kernel.OpenProcess.restype = wintypes.HANDLE
        kernel.GetExitCodeProcess.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD)]
        kernel.CloseHandle.argtypes = [wintypes.HANDLE]
        handle = kernel.OpenProcess(0x1000, False, pid)
        if not handle:
            return False
        try:
            code = wintypes.DWORD()
            assert kernel.GetExitCodeProcess(handle, ctypes.byref(code))
            return code.value == 259  # STILL_ACTIVE
        finally:
            kernel.CloseHandle(handle)
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False


async def test_cancelled_full_shutdown_terminates_the_real_browser_process(monkeypatch):
    pool = BrowserPool(max_browsers=1)
    await pool.initialize()
    browser = await pool._playwright.chromium.launch(headless=True)
    pool._register_browser("direct", browser, None)
    session = await browser.new_browser_cdp_session()
    processes = (await session.send("SystemInfo.getProcessInfo"))["processInfo"]
    browser_pids = [int(row["id"]) for row in processes if row["type"] == "browser"]
    assert browser_pids and all(process_is_running(pid) for pid in browser_pids)
    await session.detach()
    entered = asyncio.Event()
    async def stalled_close(*args, **kwargs):
        entered.set()
        await asyncio.Event().wait()
    context = await browser.new_context()
    pool._contexts[99] = context
    monkeypatch.setattr(pool, "_dispose_context", stalled_close)
    shutdown = asyncio.create_task(pool.close_all(stop_playwright=True))
    try:
        await asyncio.wait_for(entered.wait(), timeout=5)
        shutdown.cancel()
        with pytest.raises(asyncio.CancelledError):
            await shutdown
        assert pool._playwright is None
        # is_connected() keeps its old flag after driver.stop(); verify the
        # actual Chromium OS process rather than that cached Python property.
        for _ in range(100):
            if not any(process_is_running(pid) for pid in browser_pids):
                break
            await asyncio.sleep(.02)
        assert not any(process_is_running(pid) for pid in browser_pids)
    finally:
        if not shutdown.done():
            shutdown.cancel()
            await asyncio.gather(shutdown, return_exceptions=True)
        if pool._playwright is not None:
            await pool._playwright.stop()


async def test_full_shutdown_terminates_real_browser_without_individual_close_rpc(monkeypatch):
    from unittest.mock import AsyncMock
    pool = BrowserPool(max_browsers=1)
    await pool.initialize()
    browser = await pool._playwright.chromium.launch(headless=True)
    pool._register_browser("direct", browser, None)
    session = await browser.new_browser_cdp_session()
    processes = (await session.send("SystemInfo.getProcessInfo"))["processInfo"]
    pids = [int(row["id"]) for row in processes if row["type"] == "browser"]
    assert pids and all(process_is_running(pid) for pid in pids)
    await session.detach()
    individual_close = AsyncMock(side_effect=RuntimeError("redundant individual close"))
    monkeypatch.setattr(browser, "close", individual_close)
    try:
        await asyncio.wait_for(pool.close_all(stop_playwright=True), timeout=15)
        individual_close.assert_not_awaited()
        assert pool._playwright is None and not pool._browsers
        for _ in range(100):
            if not any(process_is_running(pid) for pid in pids):
                break
            await asyncio.sleep(.02)
        assert not any(process_is_running(pid) for pid in pids)
    finally:
        if pool._playwright is not None:
            await pool._playwright.stop()
