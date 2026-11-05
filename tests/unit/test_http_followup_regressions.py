"""Regression checks for the final HTTP/pool review."""
import asyncio
from concurrent.futures import ThreadPoolExecutor

import threading
import time
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import create_engine, event
from sqlalchemy.orm import Session

from executor import browser_pool, engagement_engine as engine, user_schema as schema
from executor.http import request_profile
from executor.http.playwright_http_client import PlaywrightHttpClient
from executor.http.request_profile import RequestProfile
from executor.http.retry_policy import HttpErrorType, RetryPolicy
from services.visibility_checkpoint import run_visibility_checkpoint


def test_seeded_fallback_has_creation_time_and_base62_suffix(monkeypatch):
    request_profile._seeded_verify_fp.cache_clear()
    monkeypatch.setattr(request_profile.time, "time", lambda: 1792600000.123)
    first = request_profile.generate_tiktok_verify_fp("fresh-followup-fixture")
    _, stamp, suffix = first.split("_")
    assert int(stamp, 36) == 1792600000123
    assert len(suffix) == 32 and suffix.isalnum()
    assert any(c.isupper() for c in suffix) and any(c.islower() for c in suffix)
    monkeypatch.setattr(request_profile.time, "time", lambda: 1792700000)
    assert request_profile.generate_tiktok_verify_fp("fresh-followup-fixture") == first


def test_unknown_web_creation_time_is_omitted_and_native_value_wins():
    profile = RequestProfile.publish_main_profile()
    assert "WebIdLastTime" not in profile.build_query_params({}, {"device_id": "old-device"})
    query = profile.build_query_params({}, {"WebIdLastTime": "older"}, {"web_id_last_time": "native"})
    assert query["WebIdLastTime"] == "native"
    changed = profile.build_query_params({}, {"device_id": "old", "WebIdLastTime": "old-time"}, {"device_id": "new"})
    assert "WebIdLastTime" not in changed


def test_new_ssr_device_does_not_rehydrate_old_creation_time():
    from executor.http.playwright_http_client import cache_web_identity, get_cached_web_identity, seed_web_identity_from_account
    account = SimpleNamespace(id=-320010, cookies='{"device_id":"old","WebIdLastTime":"old-time"}')
    seed_web_identity_from_account(account)
    cache_web_identity(account.id, {"device_id": "new"}, account=account)
    assert "web_id_last_time" not in get_cached_web_identity(account.id, account=account)
    assert "web_id_last_time" not in seed_web_identity_from_account(account)


@pytest.mark.parametrize("referrer", ["", "https://source.example/entry"])
def test_entry_referrer_is_independent_of_current_media(referrer):
    query = RequestProfile.publish_main_profile().build_query_params({}, runtime_profile={
        "current_url": "https://www.tiktok.com/@fixture/video/7635267329407913230",
        "root_referer": referrer,
    })
    assert query["root_referer"] == referrer


async def test_empty_publish_is_an_unconfirmed_receipt():
    from tests.unit.test_http_final_audit import EventPage
    page = EventPage("response", "")
    client = PlaywrightHttpClient(SimpleNamespace(fetch=AsyncMock()), page=page,
                                  retry_policy=RetryPolicy(max_retries=0))
    success, _, message, error = await client._execute_request(RequestProfile.publish_main_profile(), {"text": "fixture"})
    assert not success and error == HttpErrorType.SUBMITTED_UNCONFIRMED
    assert message == "submitted_unconfirmed:empty_body"
    assert client.last_request_meta["request_attempted"] is True
    assert client.last_request_meta["publication_status"] == "submitted_unconfirmed"


def test_cross_thread_health_increments_and_branch_reads_are_atomic():
    account_id = -320001
    engine._HTTP_ACCOUNT_HEALTH.pop(account_id, None)
    def worker(index):
        for step in range(120):
            engine._record_http_account_result(account_id, "soft_fail")
            parent = f"followup-{index}-{step}"
            schema.record_parent_boost(parent, str(index))
            schema._record_branch_published_reply(parent, 1, str(7641761192486194000 + index * 120 + step))
            schema.can_boost_reply(str(7641761192486194000 + step), str(index))
    with ThreadPoolExecutor(max_workers=8) as pool:
        list(pool.map(worker, range(8)))
    assert engine._HTTP_ACCOUNT_HEALTH[account_id]["soft_fail"] == 960


async def test_shared_idle_browser_survives_ttl_and_new_account_ignores_slow_disposal(monkeypatch):
    from tests.unit.test_browser_pool_page_reuse import LaunchProbeBrowser
    from executor import tiktok_client
    monkeypatch.setattr(browser_pool.settings, "USE_PROXY", False)
    monkeypatch.setattr(tiktok_client, "_get_cookies_for_account", lambda _: "[]")
    monkeypatch.setattr(tiktok_client, "parse_cookies", lambda _: [])
    browser = LaunchProbeBrowser()
    launch = AsyncMock(return_value=browser)
    pool = browser_pool.BrowserPool(max_browsers=1)
    pool._playwright = SimpleNamespace(chromium=SimpleNamespace(launch=launch), devices={}, stop=AsyncMock())
    started, release = asyncio.Event(), asyncio.Event()
    async def slow_persist(context, account_id):
        if account_id == 101:
            started.set()
            await release.wait()
    monkeypatch.setattr(pool, "_persist_context_cookies_before_close", slow_persist)
    account = lambda ident: SimpleNamespace(id=ident, label="fixture", proxy=None, country_code=None, user_agent=None)
    _, page = await pool.get_page_for_account(account(101))
    closing = asyncio.create_task(pool.close_page_from_pool(page))
    await asyncio.wait_for(started.wait(), 1)
    try:
        _, second = await asyncio.wait_for(pool.get_page_for_account(account(102)), 1)
        assert not pool._lock.locked() and not browser.closed
        release.set()
        assert await closing
        await pool.close_page_from_pool(second)
        assert not browser.closed and launch.await_count == 1
        pool._browser_idle_since["direct"] = time.monotonic() - 301
        assert await pool._cleanup_unused_browsers() == 0
        assert not browser.closed
    finally:
        release.set()
        await closing
        await pool.close_all(stop_playwright=True)


async def test_cancelled_persistence_still_closes_context_and_releases_lease(monkeypatch):
    from tests.unit.test_browser_pool_page_reuse import DummyPage, DummyContext, LaunchProbeBrowser
    pool = browser_pool.BrowserPool(max_browsers=1)
    page = DummyPage()
    page._proxy_usage_lease = SimpleNamespace(release=AsyncMock())
    lease = page._proxy_usage_lease
    context = DummyContext([page])
    pool._contexts[1] = context
    pool._account_browser_ids[1] = "direct"
    pool._browsers["direct"] = LaunchProbeBrowser()
    entered = asyncio.Event()
    async def interrupted(*args):
        entered.set()
        await asyncio.Event().wait()
    monkeypatch.setattr(pool, "_persist_context_cookies_before_close", interrupted)
    closing = asyncio.create_task(pool.close_context_for_account(1))
    await entered.wait()
    closing.cancel()
    with pytest.raises(asyncio.CancelledError):
        await closing
    assert context._closed and 1 not in pool._contexts
    lease.release.assert_awaited_once()
    assert not pool._browser_inflight_acquires
    await pool.close_all(stop_playwright=True)


def test_pool_lock_registry_does_not_keep_completed_accounts_alive():
    import gc
    pool = browser_pool.BrowserPool()
    lock = pool._account_lock(1)
    assert pool._account_lock(1) is lock
    del lock
    gc.collect()
    assert len(pool._account_locks) == 0


async def test_checkpoint_uses_owned_session_and_keeps_loop_responsive(tmp_path):
    from db.models import Base, Task
    db_engine = create_engine(f"sqlite:///{tmp_path / 'checkpoint.db'}")
    Base.metadata.create_all(db_engine)
    loop_thread = threading.get_ident()
    threads = []
    @event.listens_for(db_engine, "before_cursor_execute")
    def track_thread(connection, cursor, statement, parameters, context, many):
        if statement.lstrip().upper().startswith("UPDATE TASKS"):
            threads.append(threading.get_ident())
            time.sleep(0.08)
    try:
        with Session(db_engine, expire_on_commit=False) as db:
            task = Task(user_id=1, main_comment="fixture", video_urls=[], settings={"url_results": {"original": {}}})
            db.add(task)
            db.commit()
            ticks = []
            async def heartbeat():
                for _ in range(12):
                    await asyncio.sleep(0.01)
                    ticks.append(time.monotonic())
            payload = {"mode": "fixture", "engagement": {"replies": {"items": []}}}
            await asyncio.gather(heartbeat(), run_visibility_checkpoint(
                db, task, __import__('executor.reply_visibility', fromlist=['save_reply_visibility_checkpoint']).save_reply_visibility_checkpoint,
                "shortlink-key", payload))
            assert len(ticks) == 12 and threads and all(value != loop_thread for value in threads)
            assert "shortlink-key" in task.settings["url_results"]
            with Session(db_engine) as observer:
                assert set(observer.get(Task, task.id).settings["url_results"]) == {"original", "shortlink-key"}
    finally:
        db_engine.dispose()


async def test_checkpoint_finishes_commit_before_cancellation_cleanup(tmp_path):
    from db.models import Base, Task
    db_engine = create_engine(f"sqlite:///{tmp_path / 'cancel.db'}")
    Base.metadata.create_all(db_engine)
    entered, release, durable = threading.Event(), threading.Event(), threading.Event()
    def checkpoint(db, task, url, result):
        entered.set()
        assert release.wait(2)
        task.settings = {"receipt": result}
        db.commit()
        durable.set()
    try:
        with Session(db_engine, expire_on_commit=False) as db:
            task = Task(user_id=1, main_comment="fixture", video_urls=[], settings={})
            db.add(task); db.commit()
            operation = asyncio.create_task(run_visibility_checkpoint(db, task, checkpoint, "url", "cid"))
            while not entered.is_set():
                await asyncio.sleep(0.005)
            operation.cancel()
            await asyncio.sleep(0.01)
            assert not operation.done()
            release.set()
            with pytest.raises(asyncio.CancelledError):
                await operation
            assert durable.is_set()
    finally:
        release.set()
        db_engine.dispose()
