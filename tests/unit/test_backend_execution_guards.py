"""Failure boundaries between the durable order engine and real HTTP operations."""
import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from db.models import Account, QuantityAction
from executor import quantity_engine as quantity
from services.publication_recheck import apply_observations
from services.quantity_plan import QuantityPlan
from tests.unit.test_quantity_redesign_recovery import configure_task


def test_recheck_jobs_use_fresh_runtimes_across_closed_event_loops(
        db_session, sample_task, monkeypatch):
    from db.models import Task, PublicationCheckJob
    from executor import browser_pool as pool
    from services import publication_recheck as checks, dedicated_proxy_service as dedicated
    from tests.unit.test_quantity_redesign_recovery import receipt
    configure_task(db_session, sample_task)
    other = Task(user_id=sample_task.user_id, status="done", main_comment="fixture",
                 video_urls=sample_task.video_urls, settings=sample_task.settings)
    db_session.add(other)
    db_session.commit()
    receipt(db_session, sample_task)
    receipt(db_session, other, cid="7704740811904828193")
    runtimes = []
    monkeypatch.setattr(pool, "_thread_local", SimpleNamespace(browser_pool=None))
    async def initialize(instance):
        if instance._playwright is None:
            instance._playwright = SimpleNamespace(loop=asyncio.get_running_loop(), stop=AsyncMock())
            runtimes.append(instance._playwright)
        elif instance._playwright.loop is not asyncio.get_running_loop():
            raise RuntimeError("Playwright belongs to the previous job's closed loop")
    async def observe(db, task, rows):
        await pool.get_browser_pool()
        return [dict(observer_id=8, found_ids=[row.comment_id for row in rows])]
    monkeypatch.setattr(pool.BrowserPool, "initialize", initialize)
    monkeypatch.setattr(checks, "observe_video", observe)
    monkeypatch.setattr(dedicated, "activate_dedicated_proxy_for_user", lambda *a: None)
    monkeypatch.setattr(dedicated, "reset_dedicated_proxy", lambda *a: None)
    assert checks.process_due_checks(db_session.get_bind())
    assert checks.process_due_checks(db_session.get_bind())
    db_session.expire_all()
    assert [row.status for row in db_session.query(PublicationCheckJob).order_by(PublicationCheckJob.task_id)] == ["done", "done"]
    assert len(runtimes) == 2 and all(runtime.stop.await_count == 1 for runtime in runtimes)
    assert pool._get_thread_local_pool() is None


@pytest.mark.asyncio
@pytest.mark.parametrize("condition", ["unconfirmed", "cid_changed", "other_order"])
async def test_reply_cannot_dispatch_to_a_stale_or_unconfirmed_parent(
        db_session, sample_task, monkeypatch, condition):
    from services import account_service
    configure_task(db_session, sample_task)
    url = sample_task.video_urls[0]
    quantity.ensure_actions(db_session, sample_task, url, "main", 1, ["parent"])
    parent = db_session.query(QuantityAction).one()
    parent.state, parent.comment_id = "visible", "7704740811904828100"
    db_session.commit()
    quantity.ensure_actions(db_session, sample_task, url, "reply", 1, ["reply"], parent=parent)
    reply = db_session.query(QuantityAction).filter_by(kind="reply").one()
    if condition == "unconfirmed":
        parent.state = "accepted"
    elif condition == "cid_changed":
        parent.comment_id = "7704740811904828101"
    else:
        parent.video_url = url + "?other_order=1"
    db_session.commit()
    selector = Mock(side_effect=AssertionError("Do not reserve an account for an invalid parent"))
    monkeypatch.setattr(account_service, "get_free_account_for_reply", selector)
    assert await quantity.publish_action(db_session.get_bind(), sample_task.id, reply.id,
                                        stop_requested=lambda: False) == "parent_not_public"
    db_session.expire_all()
    assert db_session.get(QuantityAction, reply.id).dispatches == 0
    selector.assert_not_called()


@pytest.mark.parametrize("major,expected", [(145, "chrome145"), (144, "chrome142"), (200, "chrome150")])
def test_passport_tls_profile_matches_the_available_browser_generation(major, expected):
    from executor.browser_identity import curl_impersonation_profile
    assert curl_impersonation_profile(f"Chrome/{major}.0.0.0", available=[
        "chrome131", "chrome142", "chrome145", "chrome150", "chrome152_macos", "safari260"]
    ) == expected


@pytest.mark.asyncio
async def test_proxy_authentication_failure_is_not_a_dead_tiktok_session(monkeypatch):
    import httpx
    from services import cookie_validator as validator
    monkeypatch.setenv("VALIDATOR_TEST_CURL_CFFI", "1")
    monkeypatch.setattr(validator.get_settings(), "USE_PROXY", True)
    fallback = Mock(side_effect=AssertionError("Do not retry the same failed proxy"))
    monkeypatch.setattr(validator.aiohttp, "ClientSession", fallback)
    monkeypatch.setattr(validator, "_ORIGINAL_AIOHTTP_CLIENT_SESSION", fallback)
    curl = AsyncMock()
    curl.__aenter__.return_value = curl
    curl.get.side_effect = httpx.ProxyError("CONNECT failed: 407 password=private-value")
    monkeypatch.setattr(validator, "_CurlAsyncSession", Mock(return_value=curl))
    account = SimpleNamespace(id=1, cookies="sessionid=valid", proxy="http://user:password@proxy.invalid:8000")
    result = await validator._validate_account_http_tri_state(account)
    assert result["status"] == "inconclusive" and result["definitive"] is False
    assert result["category"] == "proxy_route" and result["request_attempted"] is False
    assert "407" in result["error"] and "private-value" not in result["error"]
    fallback.assert_not_called()


@pytest.mark.asyncio
async def test_transport_exception_without_a_receipt_cannot_reopen_dispatched_slot(db_session, sample_task, sample_account, monkeypatch):
    from executor import visibility_publisher
    from services import account_service
    configure_task(db_session, sample_task)
    quantity.ensure_actions(db_session, sample_task, sample_task.video_urls[0], "main", 1, ["main"])
    identity = db_session.query(QuantityAction.id).scalar()
    monkeypatch.setattr(account_service, "get_free_account_for_main_comment", lambda db, **_: db.get(Account, sample_account.id))
    monkeypatch.setattr(account_service, "release_main_account_reservation", Mock())
    async def interrupted(*args, progress, **kwargs):
        progress["publication_call_started"] = True
        raise RuntimeError("transport failed before delivery could be recorded")
    monkeypatch.setattr(visibility_publisher, "publish_visibility_http", interrupted)
    with pytest.raises(RuntimeError):
        await quantity.publish_action(db_session.get_bind(), sample_task.id, identity, stop_requested=lambda: False)
    db_session.expire_all()
    action = db_session.get(QuantityAction, identity)
    assert action.state == "uncertain" and action.dispatches == 1
    assert quantity.claim_action(db_session, identity) is None


@pytest.mark.asyncio
async def test_failure_after_account_reservation_always_releases_the_lease(db_session, sample_task, sample_account, monkeypatch):
    from services import account_service
    configure_task(db_session, sample_task)
    quantity.ensure_actions(db_session, sample_task, sample_task.video_urls[0], "main", 1, ["main"])
    identity = db_session.query(QuantityAction.id).scalar()
    def select(db, **kwargs):
        account = db.get(Account, sample_account.id)
        original, first = db.commit, True
        def fail_once():
            nonlocal first
            if first:
                first = False
                raise RuntimeError("account checkpoint failed")
            return original()
        db.commit = fail_once
        return account
    release = Mock()
    monkeypatch.setattr(account_service, "get_free_account_for_main_comment", select)
    monkeypatch.setattr(account_service, "release_main_account_reservation", release)
    with pytest.raises(RuntimeError):
        await quantity.publish_action(db_session.get_bind(), sample_task.id, identity, stop_requested=lambda: False)
    release.assert_called_once_with(sample_account.id)


@pytest.mark.asyncio
@pytest.mark.parametrize("cause", ["deadline", "stop"])
async def test_quantity_waits_are_bounded_even_inside_a_network_operation(db_session, sample_task, monkeypatch, cause):
    from executor import engagement_engine
    configure_task(db_session, sample_task)
    plan = QuantityPlan(main_count=1).model_copy(update={"max_seconds": .05 if cause == "deadline" else 10})
    monkeypatch.setattr(quantity, "quantity_plan", lambda _: plan)
    monkeypatch.setattr(engagement_engine, "resolve_order_video_url", AsyncMock(
        return_value=(sample_task.video_urls[0], "123")))
    entered, cancelled, stop = asyncio.Event(), asyncio.Event(), asyncio.Event()
    async def slow(*args, **kwargs):
        entered.set()
        try:
            await asyncio.Event().wait()
        finally:
            cancelled.set()
    monkeypatch.setattr(quantity, "_execute_quantity_publications", slow)
    operation = asyncio.create_task(quantity.execute_quantity_order(db_session, sample_task,
        sample_task.video_urls[0], ["main"], sample_task.id, stop_requested=stop.is_set))
    await asyncio.wait_for(entered.wait(), timeout=2)
    if cause == "stop":
        stop.set()
    try:
        result = await asyncio.wait_for(asyncio.shield(operation), timeout=.7)
        assert result.error == ("stopped_by_user" if cause == "stop" else "quantity_time_budget_exhausted")
        assert cancelled.is_set()
    finally:
        operation.cancel()
        await asyncio.gather(operation, return_exceptions=True)


@pytest.mark.asyncio
async def test_reader_failure_keeps_positive_evidence_from_completed_branches(monkeypatch):
    from executor import user_schema as schema, engagement_engine, main_visibility, reply_visibility
    from services import account_service
    from services.publication_recheck import observe_video
    url = "https://www.tiktok.com/@test/video/123"
    common = dict(account_id=None, identity_keys=[["username", "publisher"]], video_url=url,
                  aweme_id="123", visible_at=None, check_count=0)
    main = SimpleNamespace(**common, comment_id="7704740811904828192", kind="main", parent_id=None)
    reply = SimpleNamespace(**common, comment_id="7704740811904828193", kind="reply", parent_id=main.comment_id)
    readers = [SimpleNamespace(id=i, proxy=None, cookies=f"sessionid=other-{i}; uid_tt={i}",
                               _cached_tiktok_username=f"reader-{i}") for i in (8, 9)]
    monkeypatch.setattr(account_service, "get_free_account_for_reply", Mock(side_effect=[*readers, None]))
    monkeypatch.setattr(schema, "_preflight_cookie_http_gate", AsyncMock(side_effect=[None, "dead_session"]))
    monkeypatch.setattr(schema, "create_page_for_account", AsyncMock(return_value=(object(), SimpleNamespace(url=url))))
    monkeypatch.setattr(schema, "_ensure_account_login_ready", AsyncMock(return_value=(True, None)))
    monkeypatch.setattr(engagement_engine, "warmup_page_for_http", AsyncMock(return_value=({}, True, {})))
    monkeypatch.setattr(main_visibility, "scan_main_comment_ids", AsyncMock(return_value={
        "found_ids": [main.comment_id], "absent_ids": [], "complete": True}))
    monkeypatch.setattr(reply_visibility, "scan_reply_comment_ids", AsyncMock(side_effect=RuntimeError("read failed")))
    monkeypatch.setattr(schema, "_safe_close", AsyncMock())
    release = Mock()
    monkeypatch.setattr(schema, "release_reply_account_reservation", release)
    observations = await observe_video(SimpleNamespace(), SimpleNamespace(id=1, settings={}), [main, reply])
    apply_observations([main, reply], observations)
    assert main.visibility == "visible" and reply.visibility == "inconclusive"
    assert release.call_count == 2
