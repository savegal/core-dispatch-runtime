"""Reproductions for the external report, using isolated SQL and browser adapters."""
import asyncio
import json
import threading
import time
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest
from sqlalchemy import event
from sqlalchemy.orm import Session

from db.models import Account, KeywordReplyTarget, QuantityAction
from tests.unit.test_quantity_redesign_recovery import configure_task


@pytest.mark.parametrize("as_records", [False, True])
def test_real_hex_guard_is_not_classified_as_explicit_browser_auth(as_records):
    from services import account_service as accounts
    from services.cookie_validator import parse_cookies_to_dict, validate_cookies
    values = {"sid_guard": "a" * 32 + "|1700000000|86400", "msToken": "token", "ttwid": "wid"}
    raw = json.dumps([dict(name=key, value=value) for key, value in values.items()] if as_records else values)
    explicit = parse_cookies_to_dict(raw, restore_guard_aliases=False)
    assert "sessionid" not in explicit
    assert parse_cookies_to_dict(raw)["sessionid"] == "a" * 32  # Browser compatibility remains available.
    quality = accounts._read_account_session_quality(Account(id=1, label="guard", cookies=raw), accounts.MAIN_SCOPE)
    assert quality["quality_reason"] == "no_browser_auth_sid_guard_only"
    assert validate_cookies(raw).matched_set == "sid_guard+msToken"


def test_reply_pagination_survives_one_lost_sql_row(db_session, monkeypatch):
    from services import account_service as accounts
    monkeypatch.setattr(accounts.settings, "USE_PROXY", False)
    monkeypatch.setattr(accounts.settings, "MAX_CONCURRENT_BROWSERS", 1)
    raw = json.dumps(dict(sessionid="session", sid_tt="session", msToken="m", ttwid="w",
                          tt_csrf_token="c", verifyFp="v", sid_guard="guard"))
    rows = [Account(label=f"candidate-{i}", cookies=raw, status="active") for i in range(21)]
    db_session.add_all(rows)
    db_session.commit()
    ids = [row.id for row in rows]
    def candidates(*args):
        rows[0].assignment_lease_until = datetime.now(timezone.utc) + timedelta(minutes=5)
        db_session.commit()
        return ids
    monkeypatch.setattr(accounts, "_candidate_ids_by_web_readiness", candidates)
    original = accounts._selection_runtime_state
    def state(account, *args, **kwargs):
        result = original(account, *args, **kwargs)
        if account.id in ids[:20]:
            result["active_cooldown"] = True
        return result
    monkeypatch.setattr(accounts, "_selection_runtime_state", state)
    selected = accounts.get_free_account_for_reply(db_session)
    assert selected is not None and selected.id == ids[-1]


def test_readiness_cache_uses_changed_cookie_content_not_account_identity(monkeypatch):
    from services import account_service as accounts
    classify = Mock(side_effect=[2, 0])
    monkeypatch.setattr(accounts, "classify_account_web_readiness", classify)
    assert accounts._cached_web_readiness("first") == accounts._cached_web_readiness("first") == 2
    assert accounts._cached_web_readiness("changed") == 0
    assert classify.call_count == 2


def test_unknown_submission_is_not_completion_but_still_blocks_blind_retry():
    from services.task_execution_ledger import url_result_counts_as_success, has_submission_evidence
    result = dict(status="submitted_unconfirmed", publication_complete=True)
    assert not url_result_counts_as_success(result) and has_submission_evidence(result)


@pytest.mark.asyncio
async def test_client_signer_gate_does_not_consume_a_post(monkeypatch):
    from executor.http import playwright_http_client as transport
    from executor.http.request_profile import RequestProfile
    gate = AsyncMock(return_value=False)
    monkeypatch.setattr(transport, "wait_for_runtime_signer_ready", gate)
    page = SimpleNamespace(evaluate=AsyncMock())
    request = SimpleNamespace(fetch=AsyncMock())
    client = transport.PlaywrightHttpClient(request, page=page)
    assert not (await client._execute_request(RequestProfile.publish_main_profile(), {"text": "fixture"}))[0]
    assert client.last_request_meta["publication_status"] == "not_sent"
    page.evaluate.assert_not_awaited()
    request.fetch.assert_not_awaited()


@pytest.mark.asyncio
async def test_unknown_visibility_gets_a_second_read_without_a_second_post(db_session, sample_task, monkeypatch):
    from executor import quantity_engine as quantity, engagement_engine
    configure_task(db_session, sample_task, max_dispatches=1)
    url = sample_task.video_urls[0]
    monkeypatch.setattr(quantity, "INCONCLUSIVE_RECHECK_DELAYS", (0,))
    monkeypatch.setattr(engagement_engine, "resolve_order_video_url", AsyncMock(return_value=(url, "123")))
    async def publish(engine, task_id, identity, **kwargs):
        with Session(engine) as work:
            row = work.get(QuantityAction, identity)
            row.state, row.comment_id = "accepted", "7704740811904828192"
            work.commit()
        return "accepted"
    reads = 0
    async def verify(engine, task_id, identities, *args, **kwargs):
        nonlocal reads
        reads += 1
        if reads == 2:
            with Session(engine) as work:
                for identity in identities:
                    work.get(QuantityAction, identity).state = "visible"
                work.commit()
    post = AsyncMock(side_effect=publish)
    monkeypatch.setattr(quantity, "publish_action", post)
    monkeypatch.setattr(quantity, "verify_actions", verify)
    result = await quantity.execute_quantity_order(db_session, sample_task, url, ["main"], sample_task.id)
    assert result.error is None and result.main_visibility["confirmed"] == 1
    assert post.await_count == 1 and reads == 2


def test_legacy_reply_fields_and_empty_libraries_do_not_divide_by_zero(db_session, sample_task):
    from services.quantity_plan import quantity_reply_texts
    from executor.quantity_engine import ensure_actions
    sample_task.settings = {"replies": [], "reply_variants": ["variant"]}
    assert quantity_reply_texts(sample_task) == ["variant"]
    sample_task.settings, sample_task.reply1 = {"replies": []}, "legacy"
    sample_task.reply2 = sample_task.reply3 = None
    assert quantity_reply_texts(sample_task) == ["legacy"]
    with pytest.raises(ValueError, match="quantity_reply_text_missing"):
        ensure_actions(db_session, sample_task, sample_task.video_urls[0], "reply", 1, [])


def test_keyword_checkpoints_keep_all_delivery_receipts(db_session, sample_task):
    from executor import keyword_replies as keyword
    url = sample_task.video_urls[0]
    sample_task.settings = {"url_results": {url: {"aweme_id": "123", "warnings": ["keep"]}}}
    db_session.commit()
    for index in range(1, 4):
        parent = str(7700000000000000000 + index)
        db_session.add(KeywordReplyTarget(task_id=sample_task.id, video_key=keyword.video_key(url),
            comment_id=parent, position=index, parent_text="parent", reply_text="reply", status="success",
            result=dict(success=True, network_confirmed=True, method="http_user_schema",
                        reply_id=str(7700000000000000100 + index), parent_comment_id=parent)))
    db_session.commit()
    keyword._checkpoint(db_session, sample_task, url, "plan", dict(targets=[], scan={}), {})
    result = sample_task.settings["url_results"][url]
    assert len(result["engagement"]["replies"]["items"]) == result["engagement"]["replies"]["ok"] == 3
    assert result["aweme_id"] == "123" and result["warnings"] == ["keep"]


@pytest.mark.asyncio
async def test_partial_readers_do_not_count_as_two_complete_readers_per_cid(monkeypatch):
    from executor import user_schema as schema, engagement_engine, main_visibility, reply_visibility
    from services import account_service
    from services.publication_recheck import observe_video, apply_observations
    url, main_id, reply_id = "https://www.tiktok.com/@test/video/123", "7704740811904828192", "7704740811904828193"
    common = dict(account_id=None, identity_keys=[["username", "publisher"]], video_url=url,
                  aweme_id="123", visible_at=None, check_count=0)
    main = SimpleNamespace(**common, comment_id=main_id, kind="main", parent_id=None)
    reply = SimpleNamespace(**common, comment_id=reply_id, kind="reply", parent_id=main_id)
    readers = [SimpleNamespace(id=i, proxy=None, cookies=f"sessionid=other-{i}; uid_tt={i}",
                               _cached_tiktok_username=f"reader-{i}") for i in (8, 9, 10)]
    monkeypatch.setattr(account_service, "get_free_account_for_reply", Mock(side_effect=[*readers, None]))
    monkeypatch.setattr(schema, "_preflight_cookie_http_gate", AsyncMock(return_value=None))
    monkeypatch.setattr(schema, "create_page_for_account", AsyncMock(return_value=(object(), SimpleNamespace(url=url))))
    monkeypatch.setattr(schema, "_ensure_account_login_ready", AsyncMock(return_value=(True, None)))
    monkeypatch.setattr(engagement_engine, "warmup_page_for_http", AsyncMock(return_value=({}, True, {})))
    missing_main = dict(found_ids=[], absent_ids=[main_id], complete=True)
    missing_reply = dict(found_ids=[], absent_ids=[reply_id], complete=True)
    monkeypatch.setattr(main_visibility, "scan_main_comment_ids", AsyncMock(side_effect=[
        missing_main, dict(found_ids=[], absent_ids=[], complete=False), missing_main]))
    monkeypatch.setattr(reply_visibility, "scan_reply_comment_ids", AsyncMock(side_effect=[
        RuntimeError("read failed"), missing_reply, missing_reply]))
    monkeypatch.setattr(schema, "_safe_close", AsyncMock())
    monkeypatch.setattr(schema, "release_reply_account_reservation", Mock())
    observations = await observe_video(SimpleNamespace(), SimpleNamespace(id=1, settings={}), [main, reply])
    apply_observations([main, reply], observations)
    assert len(observations) == 3 and main.visibility == reply.visibility == "not_found"


@pytest.mark.asyncio
async def test_quantity_has_no_checked_out_transaction_during_browser_io(db_session, sample_task, sample_account, monkeypatch):
    from executor import quantity_engine as quantity, visibility_publisher
    from services import account_service
    configure_task(db_session, sample_task)
    quantity.ensure_actions(db_session, sample_task, sample_task.video_urls[0], "main", 1, ["main"])
    identity = db_session.query(QuantityAction.id).scalar()
    monkeypatch.setattr(account_service, "get_free_account_for_main_comment", lambda db, **_: db.get(Account, sample_account.id))
    monkeypatch.setattr(account_service, "release_main_account_reservation", Mock())
    async def browser(db, *args, progress, **kwargs):
        assert not db.in_transaction()
        progress["publication_call_started"] = False
        return None, "fixture_gate"
    monkeypatch.setattr(visibility_publisher, "publish_visibility_http", browser)
    assert await quantity.publish_action(db_session.get_bind(), sample_task.id, identity, stop_requested=lambda: False) == "not_sent"


@pytest.mark.asyncio
async def test_cancelled_slow_sql_claim_finishes_without_sending_or_holding_a_slot(db_session, sample_task, monkeypatch):
    from executor import quantity_engine as quantity
    from services import account_service
    configure_task(db_session, sample_task)
    quantity.ensure_actions(db_session, sample_task, sample_task.video_urls[0], "main", 1, ["main"])
    identity = db_session.query(QuantityAction.id).scalar()
    entered, release = threading.Event(), threading.Event()
    original = quantity.claim_action
    def slow_claim(*args):
        entered.set()
        release.wait(2)
        return original(*args)
    monkeypatch.setattr(quantity, "claim_action", slow_claim)
    selector = Mock(side_effect=AssertionError("Cancelled claim must not select an account"))
    monkeypatch.setattr(account_service, "get_free_account_for_main_comment", selector)
    operation = asyncio.create_task(quantity.publish_action(db_session.get_bind(), sample_task.id, identity,
                                                          stop_requested=lambda: False))
    try:
        assert await asyncio.wait_for(asyncio.to_thread(entered.wait, 1), timeout=1.5)
        operation.cancel()
    finally:
        release.set()
    with pytest.raises(asyncio.CancelledError):
        await operation
    db_session.expire_all()
    action = db_session.get(QuantityAction, identity)
    assert action.state == "not_sent" and action.dispatches == 0
    selector.assert_not_called()


@pytest.mark.asyncio
async def test_cancelled_selector_cannot_acquire_a_late_orphan_lease(db_session, sample_task, sample_account, monkeypatch):
    from executor import quantity_engine as quantity
    from services import account_service
    from services.account_assignment import claim_assignment
    configure_task(db_session, sample_task)
    quantity.ensure_actions(db_session, sample_task, sample_task.video_urls[0], "main", 1, ["main"])
    identity = db_session.query(QuantityAction.id).scalar()
    account_id = sample_account.id
    entered, release = threading.Event(), threading.Event()
    def delayed_selector(db, **kwargs):
        entered.set()
        release.wait(2)
        account = db.get(Account, account_id)
        assert claim_assignment(db, account, "main_comment", reserve=True)
        return account
    monkeypatch.setattr(account_service, "get_free_account_for_main_comment", delayed_selector)
    operation = asyncio.create_task(quantity.publish_action(db_session.get_bind(), sample_task.id, identity,
                                                          stop_requested=lambda: False))
    try:
        assert await asyncio.wait_for(asyncio.to_thread(entered.wait, 1), timeout=1.5)
        operation.cancel()
    finally:
        release.set()
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(operation, timeout=2)
    db_session.expire_all()
    assert db_session.get(Account, account_id).assignment_lease_token is None
    action = db_session.get(QuantityAction, identity)
    assert action.state == "not_sent" and action.dispatches == 0


@pytest.mark.asyncio
async def test_windows_validation_does_not_block_the_api_loop():
    from api.accounts import _run_browser_validation
    entered, release = threading.Event(), threading.Event()
    def blocking():
        entered.set()
        return release.wait(2)
    operation = asyncio.create_task(_run_browser_validation(blocking, timeout=3))
    try:
        await asyncio.wait_for(asyncio.to_thread(entered.wait, 1), timeout=1.5)
        tick = time.monotonic()
        await asyncio.sleep(.01)
        assert time.monotonic() - tick < .3 and not operation.done()
    finally:
        release.set()
        assert await operation


@pytest.mark.asyncio
async def test_sdk_symbol_alone_cannot_authorize_publication():
    from executor.http.signature_runtime import wait_for_runtime_signer_ready
    page = SimpleNamespace(evaluate=AsyncMock(return_value=dict(sdk_present=True, signed_dispatch_seen=False)))
    assert not await wait_for_runtime_signer_ready(page, timeout_ms=10, require_signed_dispatch=True)
    page = SimpleNamespace(evaluate=AsyncMock(return_value=dict(sdk_present=True, signed_dispatch_seen=True)))
    assert await wait_for_runtime_signer_ready(page, timeout_ms=100, require_signed_dispatch=True)


@pytest.mark.asyncio
async def test_returning_a_foreign_or_dead_page_always_releases_proxy_lease():
    from executor.browser_pool import BrowserPool
    lease = SimpleNamespace(release=AsyncMock())
    page = SimpleNamespace(_pool_account_id=10, context=object(), _proxy_usage_lease=lease,
                           close=AsyncMock(side_effect=RuntimeError("driver disconnected")))
    with pytest.raises(RuntimeError):
        await BrowserPool(1).return_page_to_pool(page)
    lease.release.assert_awaited_once()


@pytest.mark.asyncio
async def test_task_stop_interrupts_pending_acquisition_without_account_lock_deadlock(monkeypatch):
    from executor.browser_pool import BrowserPool, browser_pool_task_scope
    from executor.tiktok_client_async import TikTokClientError
    pool, entered, cleaned = BrowserPool(1), asyncio.Event(), asyncio.Event()
    async def blocked(account, *args):
        async with pool._account_lock(account.id):
            entered.set()
            try:
                await asyncio.Event().wait()
            finally:
                cleaned.set()
    monkeypatch.setattr(pool, "_get_page_for_account", blocked)
    with browser_pool_task_scope(5):
        operation = asyncio.create_task(pool.get_page_for_account(SimpleNamespace(id=1)))
    await entered.wait()
    await asyncio.wait_for(pool.close_pages_for_task(5), timeout=.5)
    with pytest.raises(TikTokClientError, match="interrupted"):
        await operation
    assert cleaned.is_set() and not pool._pending_task_acquisitions


@pytest.mark.asyncio
async def test_import_batches_reuse_index_and_invalidate_after_other_import(async_db_session):
    from api.accounts import import_account_records, BulkImportRequest
    from executor.browser_identity import desktop_user_agent
    from tests.unit.test_account_import_jobs import records
    queries = []
    def query(_conn, _cursor, sql, _params, _context, _many):
        if "SELECT accounts.label, accounts.cookies" in sql:
            queries.append(sql)
    event.listen(async_db_session.bind.sync_engine, "before_cursor_execute", query)
    try:
        cache = {}
        await import_account_records(BulkImportRequest(accounts=records(1)), async_db_session, cookie_index=cache)
        await import_account_records(BulkImportRequest(accounts=[dict(label="second", cookies="sessionid=second")]), async_db_session, cookie_index=cache)
        assert len(queries) == 1
        await import_account_records(BulkImportRequest(accounts=[dict(label="other", cookies="sessionid=other")]), async_db_session)
        result = await import_account_records(BulkImportRequest(accounts=[dict(label="duplicate-other", cookies="sessionid=other")]), async_db_session, cookie_index=cache)
        assert result.updated == 1 and result.added == 0 and len(queries) == 3
        assert (await async_db_session.get(Account, 1)).user_agent == desktop_user_agent()
        account = await async_db_session.get(Account, 1)
        account.cookies = "sessionid=reauthenticated"
        await async_db_session.commit()
        result = await import_account_records(BulkImportRequest(accounts=[dict(label="same-reauth", cookies=account.cookies)]), async_db_session, cookie_index=cache)
        assert result.updated == 1 and result.added == 0 and len(queries) == 4
    finally:
        event.remove(async_db_session.bind.sync_engine, "before_cursor_execute", query)
