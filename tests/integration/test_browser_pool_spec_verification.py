"""Specification review with real Chromium and loopback HTTP/SOCKS proxies."""
import asyncio
import base64
import ctypes
import json
import os
import statistics
import time
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock
from urllib.parse import urlsplit

import pytest

from executor import browser_pool
from executor.socks5_to_http_proxy import get_local_http_proxy_stats
from executor.static_asset_cache import StaticAssetCache


pytestmark = [pytest.mark.integration, pytest.mark.asyncio]
ARTIFACTS = Path("output/playwright/browser_pool_review_20261027")
ASSET = "http://static.tiktokcdn.com/assets/spec-review.js"
STYLE = "http://static.tiktokcdn.com/assets/spec-review.css"
BUNDLE = b'window.specReviewBundle = "executed";' + b"/* public fixture */" * 27000
CSS = b"body { color: rgb(12, 34, 56); } /* public stylesheet fixture padding */"


class LocalProxy:
    def __init__(self, label, *, socks=False, credentials=None, stall=False):
        self.label, self.socks, self.credentials, self.stall = label, socks, credentials, stall
        self.requests, self.authenticated, self.tasks = [], [], set()
        self.started, self.release = asyncio.Event(), asyncio.Event()
        self.asset_bytes = 0
        self.server = None

    async def start(self):
        self.server = await asyncio.start_server(self.handle, "127.0.0.1", 0)
        self.port = self.server.sockets[0].getsockname()[1]
        return self

    @property
    def url(self):
        auth = f"{self.credentials[0]}:{self.credentials[1]}@" if self.credentials else ""
        return f"{'socks5' if self.socks else 'http'}://{auth}127.0.0.1:{self.port}"

    async def close(self):
        self.release.set()
        self.server.close()
        await self.server.wait_closed()
        pending = list(self.tasks)
        for task in pending:
            task.cancel()
        await asyncio.gather(*pending, return_exceptions=True)

    async def socks_handshake(self, reader, writer):
        version, count = await reader.readexactly(2)
        assert version == 5
        methods = await reader.readexactly(count)
        method = 2 if self.credentials else 0
        assert method in methods
        writer.write(bytes((5, method)))
        await writer.drain()
        if self.credentials:
            version, length = await reader.readexactly(2)
            username = (await reader.readexactly(length)).decode()
            length = (await reader.readexactly(1))[0]
            password = (await reader.readexactly(length)).decode()
            assert version == 1 and (username, password) == self.credentials
            self.authenticated.append(True)
            writer.write(b"\x01\x00")
            await writer.drain()
        version, command, _, address_type = await reader.readexactly(4)
        assert version == 5 and command == 1
        if address_type == 1:
            await reader.readexactly(4)
        elif address_type == 3:
            await reader.readexactly((await reader.readexactly(1))[0])
        else:
            await reader.readexactly(16)
        await reader.readexactly(2)
        # Never open an upstream socket. The fixture itself is the destination.
        writer.write(b"\x05\x00\x00\x01\x7f\x00\x00\x01\x00\x50")
        await writer.drain()

    async def handle(self, reader, writer):
        task = asyncio.current_task()
        self.tasks.add(task)
        try:
            if self.socks:
                await self.socks_handshake(reader, writer)
            raw = await asyncio.wait_for(reader.readuntil(b"\r\n\r\n"), timeout=5)
            lines = raw.decode("latin-1").split("\r\n")
            method, url, _ = lines[0].split(" ", 2)
            headers = dict(line.split(": ", 1) for line in lines[1:] if ": " in line)
            if method != "GET":
                writer.write(b"HTTP/1.1 502 Local fixture only\r\nContent-Length: 0\r\nConnection: close\r\n\r\n")
                return
            if self.credentials and not self.socks:
                expected = "Basic " + base64.b64encode(":".join(self.credentials).encode()).decode()
                if headers.get("Proxy-Authorization") != expected:
                    writer.write(b'HTTP/1.1 407 Proxy Authentication Required\r\nProxy-Authenticate: Basic realm="fixture"\r\nContent-Length: 0\r\nConnection: close\r\n\r\n')
                    return
                self.authenticated.append(True)
            self.requests.append(url)
            path = urlsplit(url).path
            if path.endswith(".js"):
                body, content_type = BUNDLE, "application/javascript"
                self.asset_bytes += len(body)
            elif path.endswith(".css"):
                body, content_type = CSS, "text/css"
                self.asset_bytes += len(body)
            else:
                body = (f'<html><head><link rel="stylesheet" crossorigin="anonymous" href="{STYLE}"></head>'
                        f'<body data-proxy="{self.label}"><script crossorigin="anonymous" src="{ASSET}"></script></body></html>').encode()
                content_type = "text/html"
            writer.write((f"HTTP/1.1 200 OK\r\nContent-Type: {content_type}\r\nContent-Length: {len(body)}\r\n"
                          "Access-Control-Allow-Origin: *\r\nConnection: close\r\n\r\n").encode())
            if self.stall and path.endswith(".js"):
                writer.write(body[:64])
                await writer.drain()
                self.started.set()
                await self.release.wait()
                body = body[64:]
            writer.write(body)
            await writer.drain()
        except (asyncio.IncompleteReadError, ConnectionError, TimeoutError, UnicodeError, ValueError):
            pass
        finally:
            writer.close()
            try:
                await writer.wait_closed()
            except ConnectionError:
                pass
            self.tasks.discard(task)


@pytest.fixture
async def lab(monkeypatch):
    monkeypatch.setattr(browser_pool.settings, "BROWSER_POOL_SHARED_PROCESS", True)
    monkeypatch.setattr(browser_pool.settings, "STATIC_ASSET_CACHE_ENABLED", True)
    monkeypatch.setattr(browser_pool.settings, "PLAYWRIGHT_HEADLESS", True)
    monkeypatch.setattr(browser_pool.settings, "USE_PROXY", True)
    monkeypatch.setattr(browser_pool, "static_asset_cache", StaticAssetCache())
    pool = browser_pool.BrowserPool(max_browsers=3)
    pool._persist_context_cookies_before_close = AsyncMock()
    proxies = []

    async def new_proxy(label, **options):
        proxy = await LocalProxy(label, **options).start()
        proxies.append(proxy)
        return proxy

    try:
        yield pool, new_proxy
    finally:
        try:
            await asyncio.wait_for(pool.close_all(stop_playwright=True), timeout=15)
        finally:
            for proxy in proxies:
                await proxy.close()


def account(index, proxy):
    return SimpleNamespace(id=930000 + index, label="spec-review-fixture", cookies="[]", cookie_file=None,
                           proxy=proxy.url, user_agent=None, country_code=None, _anonymous_http_context=True)


async def check_page(page, label):
    await page.goto(f"http://review-fixture.invalid/{label}", wait_until="load")
    state = await page.evaluate("""() => ({proxy: document.body.dataset.proxy,
        bundle: window.specReviewBundle, color: getComputedStyle(document.body).color})""", isolated_context=False)
    assert state == {"proxy": label, "bundle": "executed", "color": "rgb(12, 34, 56)"}


@pytest.mark.parametrize("socks,authenticated", [(False, True), (True, False), (True, True)])
async def test_real_per_context_http_and_socks_routing(lab, socks, authenticated):
    pool, new_proxy = lab
    before = get_local_http_proxy_stats()
    first = await new_proxy("first", socks=socks, credentials=("first-user", "first-pass") if authenticated else None)
    second = await new_proxy("second", socks=socks, credentials=("second-user", "second-pass") if authenticated else None)
    ctx1, page1 = await pool.get_page_for_account(account(1, first))
    ctx2, page2 = await pool.get_page_for_account(account(2, second))
    await check_page(page1, "first")
    await check_page(page2, "second")
    assert ctx1 is not ctx2 and set(pool._browsers) == {"shared_proxied"}
    # HTTP proxy requests use absolute-form; SOCKS tunnels use origin-form.
    assert any(urlsplit(url).path == "/first" for url in first.requests)
    assert any(urlsplit(url).path == "/second" for url in second.requests)
    if authenticated:
        assert first.authenticated and second.authenticated
    if socks and authenticated:
        assert get_local_http_proxy_stats()["leased_proxies"] == before["leased_proxies"] + 2
        await pool.close_context_for_account(930001)
        assert get_local_http_proxy_stats()["leased_proxies"] == before["leased_proxies"] + 1
        await check_page(page2, "second")
        await pool.close_context_for_account(930002)
        assert get_local_http_proxy_stats()["leased_proxies"] == before["leased_proxies"]


async def test_closing_page_mid_asset_download_does_not_block_other_context(lab):
    pool, new_proxy = lab
    slow, fast = await new_proxy("slow", stall=True), await new_proxy("fast")
    ctx1, page1 = await pool.get_page_for_account(account(1, slow))
    navigation = asyncio.create_task(page1.goto("http://review-fixture.invalid/slow", wait_until="load"))
    await asyncio.wait_for(slow.started.wait(), timeout=5)
    assert not browser_pool.static_asset_cache.has(ASSET)
    await page1.close()
    await asyncio.gather(navigation, return_exceptions=True)
    for _ in range(100):
        if not ctx1._static_asset_cache_tasks:
            break
        await asyncio.sleep(0.02)
    assert not ctx1._static_asset_cache_tasks
    _, page2 = await pool.get_page_for_account(account(2, fast))
    await asyncio.wait_for(check_page(page2, "fast"), timeout=5)
    assert pool._browsers["shared_proxied"].is_connected()


async def test_concurrent_chromium_contexts_download_the_same_bundle_once(lab, monkeypatch):
    pool, new_proxy = lab
    first, second = await new_proxy("first", stall=True), await new_proxy("second")
    _, page1 = await pool.get_page_for_account(account(1, first))
    _, page2 = await pool.get_page_for_account(account(2, second))
    waiting = asyncio.Event()
    cache = browser_pool.static_asset_cache
    begin_download = cache.begin_download

    def track_waiter(url):
        pending, owner = begin_download(url)
        if url == ASSET and not owner:
            waiting.set()
        return pending, owner

    monkeypatch.setattr(cache, "begin_download", track_waiter)
    monkeypatch.setattr(browser_pool, "STATIC_ASSET_WAIT_SECONDS", 2)
    first_nav = asyncio.create_task(check_page(page1, "first"))
    second_nav = None
    try:
        await asyncio.wait_for(first.started.wait(), timeout=5)
        assert not cache.has(ASSET)
        second_nav = asyncio.create_task(check_page(page2, "second"))
        await asyncio.wait_for(waiting.wait(), timeout=5)
        first.release.set()
        await asyncio.wait_for(asyncio.gather(first_nav, second_nav), timeout=5)
        assert first.requests.count(ASSET) == 1
        assert ASSET not in second.requests
    finally:
        first.release.set()
        jobs = [task for task in (first_nav, second_nav) if task is not None]
        for task in jobs:
            task.cancel()
        await asyncio.gather(*jobs, return_exceptions=True)


def family_memory():
    """Native Windows process-tree RSS equivalent; shared pages count per process."""
    if os.name != "nt":
        return None
    from ctypes import wintypes

    class Entry(ctypes.Structure):
        _fields_ = [("size", wintypes.DWORD), ("usage", wintypes.DWORD), ("pid", wintypes.DWORD),
                    ("heap", ctypes.c_size_t), ("module", wintypes.DWORD), ("threads", wintypes.DWORD),
                    ("parent", wintypes.DWORD), ("priority", wintypes.LONG), ("flags", wintypes.DWORD),
                    ("exe", wintypes.WCHAR * 260)]

    class Counters(ctypes.Structure):
        _fields_ = [("size", wintypes.DWORD), ("faults", wintypes.DWORD)] + [
            (name, ctypes.c_size_t) for name in ("peak_ws", "ws", "peak_paged", "paged", "peak_nonpaged",
                                               "nonpaged", "pagefile", "peak_pagefile", "private")]

    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.CreateToolhelp32Snapshot.restype = wintypes.HANDLE
    kernel.Process32FirstW.argtypes = [wintypes.HANDLE, ctypes.POINTER(Entry)]
    kernel.Process32NextW.argtypes = [wintypes.HANDLE, ctypes.POINTER(Entry)]
    kernel.OpenProcess.restype = wintypes.HANDLE
    kernel.CloseHandle.argtypes = [wintypes.HANDLE]
    psapi = ctypes.WinDLL("psapi", use_last_error=True)
    psapi.GetProcessMemoryInfo.argtypes = [wintypes.HANDLE, ctypes.POINTER(Counters), wintypes.DWORD]
    snapshot = kernel.CreateToolhelp32Snapshot(2, 0)
    entry, rows = Entry(), []
    entry.size = ctypes.sizeof(entry)
    try:
        ok = kernel.Process32FirstW(snapshot, ctypes.byref(entry))
        while ok:
            rows.append((entry.pid, entry.parent))
            ok = kernel.Process32NextW(snapshot, ctypes.byref(entry))
    finally:
        kernel.CloseHandle(snapshot)
    family = {os.getpid()}
    for _ in range(10):
        children = {pid for pid, parent in rows if parent in family}
        if children <= family:
            break
        family.update(children)
    total = private_commit = process_count = 0
    for pid in family - {os.getpid()}:
        handle = kernel.OpenProcess(0x1000 | 0x0010, False, pid)
        if handle:
            counters = Counters()
            counters.size = ctypes.sizeof(counters)
            try:
                if psapi.GetProcessMemoryInfo(handle, ctypes.byref(counters), counters.size):
                    total += counters.ws
                    private_commit += counters.private
                    process_count += 1
            finally:
                kernel.CloseHandle(handle)
    return {"working_set_bytes": total, "private_commit_bytes": private_commit, "process_count": process_count}


@pytest.mark.parametrize("shared,cache_enabled", [(False, False), (True, False), (True, True)])
async def test_real_sequential_account_rotation_measurements(lab, monkeypatch, shared, cache_enabled):
    pool, new_proxy = lab
    pool.max_browsers, pool._max_pool_size = 1, 2
    monkeypatch.setattr(browser_pool.settings, "BROWSER_POOL_SHARED_PROCESS", shared)
    monkeypatch.setattr(browser_pool.settings, "STATIC_ASSET_CACHE_ENABLED", cache_enabled)
    await pool.initialize()
    chromium = pool._playwright.chromium
    real_launch, launches = chromium.launch, []

    async def launch(**options):
        browser = await real_launch(**options)
        launches.append(browser)
        return browser

    monkeypatch.setattr(chromium, "launch", launch)
    proxies = [await new_proxy(f"rotation-{index}") for index in range(4)]
    acquire_ms, peak_ws, peak_private = [], 0, 0
    started = time.perf_counter()
    for index, proxy in enumerate(proxies):
        acquire_started = time.perf_counter()
        _, page = await pool.get_page_for_account(account(index + 1, proxy))
        acquire_ms.append(round((time.perf_counter() - acquire_started) * 1000, 2))
        await check_page(page, proxy.label)
        for _ in range(100):
            if not getattr(page.context, "_static_asset_cache_tasks", ()):
                break
            await asyncio.sleep(0.01)
        memory = family_memory() or {}
        peak_ws = max(peak_ws, memory.get("working_set_bytes", 0))
        peak_private = max(peak_private, memory.get("private_commit_bytes", 0))
        await pool.return_page_to_pool(page)
    elapsed_ms = round((time.perf_counter() - started) * 1000, 2)
    assert len(launches) == (1 if shared else 4)
    downloads = sum(sum(urlsplit(url).path.endswith(".js") for url in proxy.requests) for proxy in proxies)
    assert downloads == (1 if cache_enabled else 4)
    measurements = {"shared": shared, "cache_enabled": cache_enabled, "accounts": 4,
                    "browser_launches": len(launches), "bundle_downloads": downloads,
                    "asset_bytes": sum(proxy.asset_bytes for proxy in proxies), "elapsed_ms": elapsed_ms,
                    "acquire_ms": acquire_ms, "median_acquire_ms": statistics.median(acquire_ms),
                    "peak_sampled_family_working_set_bytes": peak_ws,
                    "peak_sampled_family_private_commit_bytes": peak_private,
                    "working_set_scope": "Patchright driver and Chromium descendants; excludes test runner and cache"}
    ARTIFACTS.mkdir(parents=True, exist_ok=True)
    (ARTIFACTS / f"rotation_shared-{shared}_cache-{cache_enabled}.json").write_text(json.dumps(measurements, indent=2))


@pytest.mark.parametrize("shared", [False, True])
async def test_real_three_busy_context_memory_measurements(lab, monkeypatch, shared):
    pool, new_proxy = lab
    monkeypatch.setattr(browser_pool.settings, "BROWSER_POOL_SHARED_PROCESS", shared)
    proxies = [await new_proxy(f"parallel-{index}") for index in range(3)]
    pages = await asyncio.wait_for(asyncio.gather(
        *(pool.get_page_for_account(account(index + 1, proxy)) for index, proxy in enumerate(proxies))), timeout=20)
    await asyncio.wait_for(asyncio.gather(
        *(check_page(page, proxy.label) for (_, page), proxy in zip(pages, proxies))), timeout=20)
    await asyncio.sleep(0.1)
    memory = family_memory() or {}
    measurements = {"shared": shared, "busy_contexts": 3, "browser_owners": len(pool._browsers),
                    "family_working_set_bytes": memory.get("working_set_bytes"),
                    "family_private_commit_bytes": memory.get("private_commit_bytes"),
                    "family_process_count": memory.get("process_count"),
                    "working_set_scope": "Patchright driver and Chromium descendants; excludes test runner and cache"}
    assert len(pool._browsers) == (1 if shared else 3)
    ARTIFACTS.mkdir(parents=True, exist_ok=True)
    (ARTIFACTS / f"parallel_shared-{shared}.json").write_text(json.dumps(measurements, indent=2))
