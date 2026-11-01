"""Real Chromium reproduction of a cancelled Patchright read and page teardown."""
import asyncio
import gc
import json
from pathlib import Path
from unittest.mock import AsyncMock

import pytest
from patchright.async_api import async_playwright

from executor.browser_reads import await_browser_read, _pending_reads


pytestmark = [pytest.mark.integration, pytest.mark.asyncio]


@pytest.mark.parametrize("protected", [False, True])
async def test_real_driver_read_timeout_then_close(protected):
    loop = asyncio.get_running_loop()
    original_handler = loop.get_exception_handler()
    errors = []
    loop.set_exception_handler(lambda _loop, context: errors.append(context))
    try:
        async with async_playwright() as playwright:
            browser = await playwright.chromium.launch(headless=True)
            try:
                page = await browser.new_page()
                script = "() => {window.reviewReadStarted = true; return new Promise(() => {});}"
                operation = lambda: page.evaluate(script, isolated_context=False)
                with pytest.raises(asyncio.TimeoutError):
                    if protected:
                        await await_browser_read(operation, timeout=.2)
                    else:
                        await asyncio.wait_for(operation(), timeout=.2)
                assert await page.evaluate("() => window.reviewReadStarted", isolated_context=False)
                await page.close()
                for _ in range(5):
                    gc.collect()
                    await asyncio.sleep(.03)
            finally:
                await browser.close()
        for _ in range(5):
            gc.collect()
            await asyncio.sleep(.03)
        orphaned = [error for error in errors if "Future exception was never retrieved" in error.get("message", "")]
        directory = Path("output/task1247_review_20261027")
        directory.mkdir(parents=True, exist_ok=True)
        (directory / f"browser_read_{'protected' if protected else 'baseline'}.json").write_text(
            json.dumps({"protected": protected, "orphaned_driver_futures": len(orphaned),
                        "pending_reads_after_close": len(_pending_reads)}), encoding="utf-8")
        assert len(orphaned) == (0 if protected else 1)
        assert not _pending_reads
    finally:
        loop.set_exception_handler(original_handler)


async def test_login_probe_deadline_then_context_close_drains_driver(monkeypatch):
    from executor import user_schema
    errors = []
    loop = asyncio.get_running_loop()
    previous = loop.get_exception_handler()
    loop.set_exception_handler(lambda _loop, context: errors.append(context))
    try:
        async with async_playwright() as playwright:
            browser = await playwright.chromium.launch(headless=True)
            try:
                page = await browser.new_page()
                monkeypatch.setattr(user_schema, "dismiss_cookie_banners", AsyncMock())
                async def blocked_login_state(page):
                    return await page.evaluate("() => new Promise(() => {})", isolated_context=False)
                monkeypatch.setattr(user_schema, "get_browser_login_state", blocked_login_state)
                from types import SimpleNamespace
                result = await user_schema._deny_if_visible_logged_out(SimpleNamespace(), page,
                    SimpleNamespace(id=1247), correlation_id="local_review", phase="test", timeout_seconds=.2)
                assert result is None
                await page.context.close()
                for _ in range(5):
                    gc.collect()
                    await asyncio.sleep(.03)
            finally:
                await browser.close()
        assert not [error for error in errors if "Future exception was never retrieved" in error.get("message", "")]
        assert not _pending_reads
    finally:
        loop.set_exception_handler(previous)

