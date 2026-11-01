"""Actual Chromium routing and anonymous CORS, using only loopback fixtures."""
import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock
from urllib.parse import urlsplit

import pytest

from executor import browser_pool
from executor.static_asset_cache import StaticAssetCache


pytestmark = [pytest.mark.integration, pytest.mark.asyncio]
ASSET = "http://static.tiktokcdn.com/assets/pool-consolidation-fixture.js"


async def test_shared_chromium_context_proxies_and_cached_crossorigin_script(monkeypatch):
    monkeypatch.setattr(browser_pool.settings, "BROWSER_POOL_SHARED_PROCESS", True)
    monkeypatch.setattr(browser_pool.settings, "STATIC_ASSET_CACHE_ENABLED", True)
    monkeypatch.setattr(browser_pool.settings, "USE_PROXY", True)
    monkeypatch.setattr(browser_pool.settings, "PLAYWRIGHT_HEADLESS", True)
    cache = StaticAssetCache()
    monkeypatch.setattr(browser_pool, "static_asset_cache", cache)
    pool = browser_pool.BrowserPool(max_browsers=2)
    pool._persist_context_cookies_before_close = AsyncMock()
    requests = {"first": [], "second": []}
    servers = []
    handlers = set()

    async def serve(reader, writer, label):
        task = asyncio.current_task()
        handlers.add(task)
        try:
            raw = await asyncio.wait_for(reader.readuntil(b"\r\n\r\n"), timeout=5)
            method, url, _ = raw.split(b"\r\n", 1)[0].decode().split(" ", 2)
            # No upstream sockets or DNS: even browser background requests
            # terminate at this fixture proxy.
            if method != "GET":
                return
            requests[label].append(url)
            if urlsplit(url).path.endswith(".js"):
                body = b'window.fixtureBundleLoaded = "cross-origin bundle executed"; /* public fixture padding */'
                content_type = "application/javascript"
            else:
                body = (f'<html><body data-proxy="{label}"><script crossorigin="anonymous" '
                        f'src="{ASSET}"></script></body></html>').encode()
                content_type = "text/html"
            writer.write((f"HTTP/1.1 200 OK\r\nContent-Type: {content_type}\r\n"
                          f"Content-Length: {len(body)}\r\nAccess-Control-Allow-Origin: *\r\n"
                          "Connection: close\r\n\r\n").encode() + body)
            await writer.drain()
        except (asyncio.IncompleteReadError, ConnectionError, TimeoutError):
            pass
        finally:
            writer.close()
            await writer.wait_closed()
            handlers.discard(task)

    try:
        for label in ("first", "second"):
            server = await asyncio.start_server(lambda reader, writer, label=label: serve(reader, writer, label),
                                                "127.0.0.1", 0)
            servers.append(server)
        accounts = [SimpleNamespace(
            id=910001 + index, label="local-routing-fixture", proxy=f"http://127.0.0.1:{server.sockets[0].getsockname()[1]}",
            cookies="[]", country_code=None, user_agent=None, _anonymous_http_context=True,
        ) for index, server in enumerate(servers)]
        first_context, first_page = await pool.get_page_for_account(accounts[0])
        await first_page.goto("http://routing-fixture.invalid/first", wait_until="load")
        assert await first_page.evaluate("document.body.dataset.proxy", isolated_context=False) == "first"
        assert await first_page.evaluate("window.fixtureBundleLoaded", isolated_context=False) == "cross-origin bundle executed"
        for _ in range(50):
            if cache.has(ASSET):
                break
            await asyncio.sleep(0.02)
        assert cache.has(ASSET)
        second_context, second_page = await pool.get_page_for_account(accounts[1])
        owner = pool._browsers["shared_proxied"]
        assert first_context is not second_context and len(pool._browsers) == 1
        await second_page.goto("http://routing-fixture.invalid/second", wait_until="load")
        assert await second_page.evaluate("document.body.dataset.proxy", isolated_context=False) == "second"
        assert await second_page.evaluate("window.fixtureBundleLoaded", isolated_context=False) == "cross-origin bundle executed"
        assert ASSET in requests["first"] and ASSET not in requests["second"]
        await pool.close_context_for_account(accounts[0].id)
        assert owner.is_connected() and pool._browsers["shared_proxied"] is owner
        assert await second_page.evaluate("document.body.dataset.proxy", isolated_context=False) == "second"
    finally:
        await pool.close_all(stop_playwright=True)
        for server in servers:
            server.close()
            await server.wait_closed()
        for task in list(handlers):
            task.cancel()
        await asyncio.gather(*handlers, return_exceptions=True)
