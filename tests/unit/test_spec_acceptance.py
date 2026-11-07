"""Acceptance regressions for live readiness and all order retry entrypoints."""
import asyncio
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker
from starlette.requests import Request

from db.models import AppSettings, PendingEnqueue, Pricing, Task, Transaction, User
from executor.http.signature_runtime import wait_for_runtime_signer_ready
from services.task_replay import retryable_video_urls
from tests.unit.test_http_warmup_recovery import warmup  # reusable production-warmup fixture
from tests.unit.test_task_create_duplicates import order_db


pytestmark = pytest.mark.unit
URL = "https://www.tiktok.com/@demo/video/1234567890123456789"


def receipt():
    return {"success": True, "network_confirmed": True, "method": "http",
            "reply_id": "7701417414836617985"}


@pytest.mark.parametrize("failure", ["cancel", "driver_error"])
async def test_full_pool_shutdown_stops_driver_even_when_browser_close_fails(failure):
    from executor.browser_pool import BrowserPool
    pool = BrowserPool(max_browsers=1)
    lock = pool._account_lock(99)
    runtime = SimpleNamespace(stop=AsyncMock())
    pool._playwright = runtime
    pool._browsers["direct"] = object()
    entered = asyncio.Event()
    async def fail_close(*args, **kwargs):
        entered.set()
        if failure == "driver_error":
            raise RuntimeError("driver disconnected during shutdown")
        await asyncio.Event().wait()
    pool._close_browser = fail_close
    shutdown = asyncio.create_task(pool.close_all(stop_playwright=True))
    await entered.wait()
    if failure == "cancel":
        shutdown.cancel()
    with pytest.raises(asyncio.CancelledError if failure == "cancel" else RuntimeError):
        await shutdown
    runtime.stop.assert_awaited_once()
    assert pool._playwright is None
    assert not pool._closing and not lock.locked()


async def test_full_shutdown_delegates_process_close_to_its_driver():
    from executor.browser_pool import BrowserPool
    pool = BrowserPool(max_browsers=2)
    runtime = SimpleNamespace(stop=AsyncMock())
    browsers = [SimpleNamespace(close=AsyncMock()) for _ in range(2)]
    pool._playwright = runtime
    pool._browsers = dict(zip(("direct", "shared_proxied"), browsers))
    await pool.close_all(stop_playwright=True)
    runtime.stop.assert_awaited_once()
    assert not pool._browsers and pool._playwright is None
    for browser in browsers:
        browser.close.assert_not_awaited()


async def test_short_polls_share_one_driver_read_and_accept_late_ready_result():
    release = asyncio.Event()
    async def evaluate(*a, **kw):
        await release.wait()
        return {"sdk_present": True, "signed_dispatch_seen": False}
    page = SimpleNamespace(url=URL, evaluate=AsyncMock(side_effect=evaluate))
    for _ in range(3):
        assert not await wait_for_runtime_signer_ready(page, timeout_ms=10)
    assert page.evaluate.await_count == 1
    release.set()
    assert await wait_for_runtime_signer_ready(page, timeout_ms=100)
    assert page.evaluate.await_count == 1


async def test_late_old_document_signer_cannot_authorize_a_new_document():
    release = asyncio.Event()
    async def evaluate(*a, **kw):
        if page.evaluate.await_count == 1:
            await release.wait()
            return {"sdk_present": True, "signed_dispatch_seen": True}
        return {"sdk_present": False, "signed_dispatch_seen": False}
    page = SimpleNamespace(url=URL, evaluate=AsyncMock(side_effect=evaluate))
    assert not await wait_for_runtime_signer_ready(page, timeout_ms=10)
    page._http_document_generation += 1
    release.set()
    assert not await wait_for_runtime_signer_ready(page, timeout_ms=30)
    assert page.evaluate.await_count == 2
    assert not page._http_signer_state["sdk_present"]


async def test_transient_signer_read_error_does_not_poison_the_next_poll():
    release = asyncio.Event()
    async def evaluate(*a, **kw):
        if page.evaluate.await_count == 1:
            raise RuntimeError("execution context changed")
        await release.wait()
        return {"sdk_present": True}
    page = SimpleNamespace(url=URL, evaluate=AsyncMock(side_effect=evaluate))
    assert not await wait_for_runtime_signer_ready(page, timeout_ms=10)
    release.set()
    assert await wait_for_runtime_signer_ready(page, timeout_ms=100)
    assert page.evaluate.await_count == 2


async def test_warmup_consumes_late_signer_read_after_cookie_and_auth_checks(warmup, monkeypatch):
    from executor import engagement_engine as engine
    from tests.unit.test_http_warmup_recovery import URL as warmup_url
    page, _, _ = warmup
    async def ready(page, timeout_ms=1500):
        page._http_signer_state = {"sdk_present": True, "signed_dispatch_seen": False}
        return timeout_ms == 1000
    monkeypatch.setattr(engine, "wait_for_runtime_signer_ready", ready)
    _, valid, info = await engine.warmup_page_for_http(page, warmup_url, max_retries=1)
    assert valid
    assert info["signer_recovered_after_reads"] is True
    assert info["required_nav_age_ms"] == 3200


async def test_late_signer_read_does_not_mask_missing_csrf(warmup, monkeypatch):
    from executor import engagement_engine as engine
    from tests.unit.test_http_warmup_recovery import URL as warmup_url
    page, _, _ = warmup
    page.context.cookies.return_value = [cookie for cookie in page.context.cookies.return_value if cookie["name"] != "tt_csrf_token"]
    async def ready(page, timeout_ms=1500):
        page._http_signer_state = {"sdk_present": True, "signed_dispatch_seen": False}
        return timeout_ms == 1000
    monkeypatch.setattr(engine, "wait_for_runtime_signer_ready", ready)
    _, valid, info = await engine.warmup_page_for_http(page, warmup_url, max_retries=1)
    assert not valid
    assert "tt_csrf_token" in info["missing_critical"]


async def test_cold_runtime_profile_grace_reuses_the_same_driver_read(monkeypatch):
    from executor.http import playwright_http_client
    from executor.http.page_runtime import read_warmup_runtime_profile
    release = asyncio.Event()
    async def read(page):
        await release.wait()
        return {"device_id": "fixture"}
    extract = AsyncMock(side_effect=read)
    monkeypatch.setattr(playwright_http_client, "_extract_runtime_profile", extract)
    page = SimpleNamespace(url=URL)
    asyncio.get_running_loop().call_later(.03, release.set)
    profile, extended = await read_warmup_runtime_profile(page, allow_extension=True,
        timeout_seconds=.01, grace_seconds=.2)
    assert extended and profile == {"device_id": "fixture"}
    extract.assert_awaited_once()


async def test_unready_runtime_read_does_not_gain_an_unconditional_grace_window(monkeypatch):
    from executor.http import playwright_http_client
    from executor.http.page_runtime import read_warmup_runtime_profile
    release = asyncio.Event()
    async def read(page):
        await release.wait()
        return {"device_id": "fixture"}
    extract = AsyncMock(side_effect=read)
    monkeypatch.setattr(playwright_http_client, "_extract_runtime_profile", extract)
    page = SimpleNamespace(url=URL)
    try:
        with pytest.raises(asyncio.TimeoutError):
            await read_warmup_runtime_profile(page, timeout_seconds=.01, grace_seconds=.2)
        assert page._http_runtime_profile_probe[1].done() is False
    finally:
        release.set()
        profile, _ = await read_warmup_runtime_profile(page, timeout_seconds=.2)
        assert profile["device_id"] == "fixture"
    extract.assert_awaited_once()


@pytest.mark.parametrize("result", [
    {"status": "fail", "engagement": {"replies": {"items": [receipt()]}}},
    {"status": "partial", "engagement": {"replies": {"items": [{"success": True, "method": "dom"}]}}},
    {"status": "fail", "engagement": {"main": {"posted": True}}},
    {"status": "submitted_unconfirmed"},
])
def test_retry_policy_excludes_all_recorded_submission_forms(result):
    task = {"status": "partial", "video_urls": ["sent", "safe", "unstarted"],
            "settings": {"url_results": {"sent": result, "safe": {"status": "fail"}}}}
    assert retryable_video_urls(task) == ["safe", "unstarted"]


def test_retry_policy_does_not_introduce_foreign_urls_or_repeat_done_legacy_orders():
    assert retryable_video_urls({"status": "error", "video_urls": ["own"],
        "settings": {"failed_urls": ["foreign", "own", "own"]}}) == ["own"]
    assert retryable_video_urls({"status": "done", "video_urls": ["own"]}) == []


def test_dom_partial_result_remains_partial_without_an_http_receipt():
    from executor.executor import _completion_status_with_errors
    from services.user_facing_errors import build_task_execution_summary
    result = {"status": "partial", "mode": "engagement_only", "engagement": {"replies": {
        "ok": 1, "total": 2, "items": [{"success": True, "method": "dom"}, {"success": False}]}}}
    assert _completion_status_with_errors(0, {URL: result}) == "partial"
    task = {"status": "error", "kind": "engagement_only", "video_urls": [URL],
            "settings": {"url_results": {URL: result}}}
    assert build_task_execution_summary(task)["display_status"] == "partial"


async def make_autopilot_order(db, *, sent=False, balance="20.00", blocked=False):
    user = User(telegram_id=991234, role="user", balance=Decimal(balance), is_blocked=blocked)
    db.add_all([user, Pricing(price_success=Decimal("5.00"), price_failure=Decimal("2.00"))])
    await db.flush()
    original = Task(user_id=user.id, status="partial", main_comment="main", video_urls=["sent", "safe"],
        settings={"origin": "autopilot", "url_results": {
            "sent": {"status": "fail", "engagement": {"replies": {"items": [receipt()]}}},
            "safe": {"status": "fail", **({"engagement": {"replies": {"items": [receipt()]}}} if sent else {})},
        }})
    db.add(original)
    await db.commit()
    return user, original


async def test_autopilot_retry_filters_receipts_and_reuses_existing_task_without_a_second_hold(async_db_session, monkeypatch):
    from services.autopilot import actions
    monkeypatch.setattr(actions.settings, "DEMO_MODE", False)
    monkeypatch.setattr(actions.settings, "ADMIN_IDS", [])
    finalize = AsyncMock(return_value=True)
    monkeypatch.setattr(actions, "finalize_reserved_enqueue_async", finalize)
    user, original = await make_autopilot_order(async_db_session)
    preview = await actions.retry_task_preview(async_db_session, user.id, original.id)
    assert preview["details"]["cost"] == 5.0 and preview["details"]["video_count"] == 1
    first = await actions.retry_task(async_db_session, user.id, original.id)
    second = await actions.retry_task(async_db_session, user.id, original.id)
    assert first["details"]["task_id"] == second["details"]["task_id"]
    assert second["details"]["reused"] is True
    retry = await async_db_session.get(Task, first["details"]["task_id"])
    assert retry.video_urls == ["safe"]
    await async_db_session.refresh(user)
    assert user.balance == Decimal("15.00")
    assert len((await async_db_session.execute(select(Transaction))).scalars().all()) == 1
    assert len((await async_db_session.execute(select(PendingEnqueue))).scalars().all()) == 1
    finalize.assert_awaited_once()
    retry.status = "done"
    await async_db_session.commit()
    third = await actions.retry_task(async_db_session, user.id, original.id)
    assert third["details"]["task_id"] == retry.id
    assert third["details"]["reused"] is True
    assert len((await async_db_session.execute(select(Transaction))).scalars().all()) == 1


async def test_concurrent_autopilot_retries_share_one_creation(async_db_session, monkeypatch):
    from services.autopilot import actions
    monkeypatch.setattr(actions.settings, "DEMO_MODE", False)
    monkeypatch.setattr(actions.settings, "ADMIN_IDS", [])
    monkeypatch.setattr(actions, "finalize_reserved_enqueue_async", AsyncMock(return_value=True))
    user, original = await make_autopilot_order(async_db_session)
    sessions = async_sessionmaker(async_db_session.bind, expire_on_commit=False)
    async def request():
        async with sessions() as db:
            return await actions.retry_task(db, user.id, original.id)
    first, second = await asyncio.gather(request(), request())
    assert first["success"] and second["success"]
    assert first["details"]["task_id"] == second["details"]["task_id"]
    assert len((await async_db_session.execute(select(Transaction))).scalars().all()) == 1


@pytest.mark.parametrize("reason", ["already_sent", "blocked", "parsing"])
async def test_autopilot_rechecks_publications_and_creation_access(async_db_session, monkeypatch, reason):
    from services.autopilot import actions
    monkeypatch.setattr(actions.settings, "DEMO_MODE", False)
    monkeypatch.setattr(actions.settings, "ADMIN_IDS", [])
    user, original = await make_autopilot_order(async_db_session, sent=reason == "already_sent", blocked=reason == "blocked")
    if reason == "parsing":
        async_db_session.add(AppSettings(key="software_mode", value="parsing"))
        await async_db_session.commit()
    result = await actions.retry_task(async_db_session, user.id, original.id)
    assert not result["success"]
    assert result["code"] == ("no_retryable_videos" if reason == "already_sent" else "access_denied")
    await async_db_session.refresh(user)
    assert user.balance == Decimal("20.00")
    assert not (await async_db_session.execute(select(Transaction))).scalars().all()


async def create_retry_via_api(db, original_id, *, urls=None):
    from api import tasks
    payload = tasks.TaskCreate(user_id=1, main_comment="a", text_variants=["a", "b", "c"],
        video_urls=urls or [URL], settings={"retry_of_task_id": original_id})
    request = Request({"type": "http", "headers": []})
    # Keep the retry serialization wrapper; bypass only the rate-limit wrapper.
    return await tasks.create_task.__wrapped__(request=request, task_data=payload, db=db, auth_user_id=1)


async def test_task_api_reuses_active_retry_without_implicit_deduping_of_new_orders(order_db):
    original = Task(user_id=1, status="error", main_comment="a", video_urls=[URL], settings={})
    setting = AppSettings(key="order_transport_mode", value="dom")
    order_db.add_all([original, setting])
    await order_db.commit()
    first = await create_retry_via_api(order_db, original.id)
    assert first["order_transport_mode"] == "http"
    # Reusing an existing legacy snapshot must preserve its original transport.
    retry = await order_db.get(Task, first["task_id"])
    retry.settings = {**retry.settings, "order_transport_mode": "dom"}
    setting.value = "http"
    await order_db.commit()
    second = await create_retry_via_api(order_db, original.id)
    assert first["task_id"] == second["task_id"]
    assert second["idempotency_reuse"] is True
    assert second["order_transport_mode"] == "dom"
    assert len((await order_db.execute(select(Task))).scalars().all()) == 2
    retry = await order_db.get(Task, first["task_id"])
    retry.status = "done"
    await order_db.commit()
    third = await create_retry_via_api(order_db, original.id)
    assert third["task_id"] == retry.id and third["status"] == "done"


async def test_task_api_refuses_stale_retry_after_a_receipt_is_recorded(order_db):
    original = Task(user_id=1, status="partial", main_comment="a", video_urls=[URL],
        settings={"url_results": {URL: {"status": "fail", "engagement": {"replies": {"items": [receipt()]}}}}})
    order_db.add(original)
    await order_db.commit()
    with pytest.raises(HTTPException) as error:
        await create_retry_via_api(order_db, original.id)
    assert error.value.status_code == 409
    assert len((await order_db.execute(select(Task))).scalars().all()) == 1


async def test_task_api_does_not_retry_another_users_source_order(order_db):
    other = User(id=2, telegram_id=991235, role="user", is_blocked=False)
    original = Task(user_id=2, status="error", main_comment="a", video_urls=[URL], settings={})
    order_db.add_all([other, original])
    await order_db.commit()
    with pytest.raises(HTTPException) as error:
        await create_retry_via_api(order_db, original.id)
    assert error.value.status_code == 404


async def test_task_api_and_autopilot_concurrent_retry_share_one_order(order_db, monkeypatch):
    from services.autopilot import actions
    monkeypatch.setattr(actions, "reserve_task_for_enqueue_async", AsyncMock())
    monkeypatch.setattr(actions, "finalize_reserved_enqueue_async", AsyncMock(return_value=True))
    original = Task(user_id=1, status="error", main_comment="a", video_urls=[URL],
                    settings={"origin": "autopilot", "text_variants": ["a", "b", "c"]})
    order_db.add(original)
    await order_db.commit()
    sessions = async_sessionmaker(order_db.bind, expire_on_commit=False)
    async def api_request():
        async with sessions() as db:
            return await create_retry_via_api(db, original.id)
    async def autopilot_request():
        async with sessions() as db:
            return await actions.retry_task(db, 1, original.id)
    api_result, autopilot_result = await asyncio.gather(api_request(), autopilot_request())
    assert api_result["task_id"] == autopilot_result["details"]["task_id"]
    assert len((await order_db.execute(select(Task))).scalars().all()) == 2


@pytest.mark.parametrize("bad_source", [True, 1.5, "not-an-id", -1])
async def test_task_api_validates_retry_source(order_db, bad_source):
    with pytest.raises(HTTPException) as error:
        await create_retry_via_api(order_db, bad_source)
    assert error.value.status_code == 422
