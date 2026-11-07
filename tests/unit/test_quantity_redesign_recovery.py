"""Cross-component recovery, ownership and metered traffic contracts."""
import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock
import threading

import pytest
from fastapi import FastAPI, HTTPException
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import async_sessionmaker

from db.models import (PublicationAttempt, PublicationCheckJob, QuantityAction, Task,
                       TaskTrafficBucket, User)
from executor.quantity_engine import action_id, claim_action, ensure_actions, seed_resume_actions
from services.publication_journal import utcnow
from services.publication_recheck import request_recheck, process_due_checks
from services.publication_reports import activity_snapshot
from services.quantity_plan import QuantityPlan
from services.task_replay import retryable_video_urls
from tests.unit.test_keyword_billing_review import paid_keyword_worker
from tests.unit.test_task_create_duplicates import order_db


def configure_task(db, task, *, mains=1, replies=0, **limits):
    task.settings = {"publication_plan": QuantityPlan(main_count=mains, replies_per_main=replies,
                                                    **limits).model_dump(), "replies": ["answer"]}
    task.status = "done"
    db.commit()


def receipt(db, task, *, cid="7704740811904828192", state="accepted", **kwargs):
    row = PublicationAttempt(id=cid[-32:], task_id=task.id, video_url=task.video_urls[0], kind="main",
        text="main", text_hash="h", comment_id=cid, status=state, wire_count=1,
        aweme_id="123", started_at=utcnow(), next_check_at=utcnow(), **kwargs)
    db.add(row)
    db.commit()
    return row


def test_stopped_after_parent_confirmation_can_continue_missing_reply_stage(db_session, sample_task):
    configure_task(db_session, sample_task, mains=1, replies=2)
    url = sample_task.video_urls[0]
    sample_task.status = "stopped"
    sample_task.settings = {**sample_task.settings, "url_results": {url: {"quantity_slots": [
        {"id": "parent", "kind": "main", "slot": 1, "state": "visible"}]}}}
    assert retryable_video_urls(sample_task) == [url]
    sample_task.settings["url_results"][url]["quantity_slots"].extend([
        {"kind": "reply", "parent_action_id": "parent", "state": "uncertain"},
        {"kind": "reply", "parent_action_id": "parent", "state": "accepted"}])
    assert retryable_video_urls(sample_task) == []


def test_resume_preserves_parent_receipts_with_reversed_input_and_never_reclaims_unknown(db_session, sample_task):
    configure_task(db_session, sample_task, mains=2, replies=2)
    url = sample_task.video_urls[0]
    old = [dict(id="r", kind="reply", slot=1, text="old reply", state="accepted", comment_id="7704740811904828193",
                parent_action_id="m", parent_id="7704740811904828192", account_id=None),
           dict(id="u", kind="main", slot=2, text="uncertain main", state="dispatching", comment_id=None),
           dict(id="m", kind="main", slot=1, text="old main", state="visible", comment_id="7704740811904828192")]
    sample_task.settings = {**sample_task.settings, "_quantity_resume": {url: old}}
    db_session.commit()
    ensure_actions(db_session, sample_task, url, "main", 2, ["new"])
    seed_resume_actions(db_session, sample_task, url)
    seed_resume_actions(db_session, sample_task, url)
    parent = db_session.get(QuantityAction, action_id(sample_task.id, url, "main", 1))
    child = db_session.query(QuantityAction).filter(QuantityAction.kind == "reply").one()
    unknown = db_session.get(QuantityAction, action_id(sample_task.id, url, "main", 2))
    assert parent.comment_id == child.parent_id == "7704740811904828192"
    assert child.parent_action_id == parent.id and child.comment_id == "7704740811904828193"
    assert unknown.state == "uncertain" and claim_action(db_session, unknown.id) is None
    assert db_session.query(PublicationAttempt).count() == 2
    assert activity_snapshot(db_session, sample_task)["summary"]["dispatched"] == 0


def test_uncertain_dispatch_consumes_mutation_budget(db_session, sample_task):
    configure_task(db_session, sample_task, mains=1, max_dispatches=1)
    ensure_actions(db_session, sample_task, sample_task.video_urls[0], "main", 1, ["main"])
    action = db_session.query(QuantityAction).one()
    assert claim_action(db_session, action.id)
    action.state = "absent"
    db_session.commit()
    assert claim_action(db_session, action.id) is None


def test_fast_and_export_snapshots_have_same_goal_counts(db_session, sample_task):
    configure_task(db_session, sample_task)
    ensure_actions(db_session, sample_task, sample_task.video_urls[0], "main", 1, ["main"])
    row = db_session.query(QuantityAction).one()
    row.state, row.comment_id = "visible", "7704740811904828192"
    receipt(db_session, sample_task, visibility="visible")
    fast = activity_snapshot(db_session, sample_task)
    detailed = activity_snapshot(db_session, sample_task, include_texts=True)
    assert fast["videos_success"] == detailed["videos_success"] == 1
    assert fast["summary"] == detailed["summary"]


def test_late_positive_proof_reaches_map_and_live_view_without_rebilling(db_session, sample_task):
    from services.quantity_state import quantity_settings_snapshot
    from bot.services.live_orders import render_live
    configure_task(db_session, sample_task)
    url, cid = sample_task.video_urls[0], "7704740811904828192"
    sample_task.status = "partial"
    sample_task.error_reason = "quantity_time_budget_exhausted"
    sample_task.settings = {**sample_task.settings, "url_results": {url: {"status": "partial", "engagement": {
        "main": {"variants_published": [{"comment_id": cid, "public_confirmed": False}]}}}}}
    ensure_actions(db_session, sample_task, url, "main", 1, ["main"])
    action = db_session.query(QuantityAction).one()
    action.state, action.comment_id = "visible", cid
    receipt(db_session, sample_task, visibility="visible")
    settings = quantity_settings_snapshot(db_session, sample_task)
    assert settings["url_results"][url]["engagement"]["main"]["variants_published"][0]["public_confirmed"]
    assert settings["url_results"][url]["main_visibility"]["status"] == "confirmed"
    assert sample_task.settings["url_results"][url]["engagement"]["main"]["variants_published"][0]["public_confirmed"] is False
    assert sample_task.status == "partial"
    text = render_live(activity_snapshot(db_session, sample_task))
    assert "цель подтверждена" in text.casefold() and "Достигнут лимит времени" not in text


def test_recheck_stops_at_task_traffic_limit(db_session, sample_task):
    configure_task(db_session, sample_task, traffic_limit_mb=1)
    db_session.add(TaskTrafficBucket(task_id=sample_task.id, account_key=0, category="reads", hour=utcnow(),
        received_bytes=1048576, sent_bytes=0, requests=1))
    db_session.commit()
    with pytest.raises(ValueError, match="traffic_budget"):
        request_recheck(db_session, sample_task.id)


def test_automatic_rechecks_do_not_repeat_already_visible_publications(db_session, sample_task, monkeypatch):
    import services.publication_recheck as checks
    import executor.browser_pool as pool
    import services.dedicated_proxy_service as dedicated
    configure_task(db_session, sample_task)
    visible = receipt(db_session, sample_task, visibility="visible", visible_at=utcnow())
    due = receipt(db_session, sample_task, cid="7704740811904828193")
    observed = []
    async def observe(db, task, rows):
        observed.extend(row.comment_id for row in rows)
        return [{"observer_id": 8, "found_ids": [due.comment_id]}]
    monkeypatch.setattr(checks, "observe_video", observe)
    monkeypatch.setattr(pool, "close_browser_pool", AsyncMock())
    monkeypatch.setattr(dedicated, "activate_dedicated_proxy_for_user", lambda *a: None)
    monkeypatch.setattr(dedicated, "reset_dedicated_proxy", lambda *a: None)
    assert process_due_checks(db_session.get_bind())
    assert observed == [due.comment_id] and visible.comment_id not in observed


@pytest.mark.asyncio
async def test_account_preparation_failure_releases_reserved_mutation_budget(db_session, sample_task, monkeypatch):
    import services.account_service as accounts
    from executor.quantity_engine import publish_action
    configure_task(db_session, sample_task)
    ensure_actions(db_session, sample_task, sample_task.video_urls[0], "main", 1, ["main"])
    row = db_session.query(QuantityAction).one()
    def broken(*args, **kwargs):
        raise RuntimeError("selection failed before acquiring account")
    monkeypatch.setattr(accounts, "get_free_account_for_main_comment", broken)
    assert await publish_action(db_session.get_bind(), sample_task.id, row.id,
                                stop_requested=lambda: False) == "account_preparation_failed"
    db_session.refresh(row)
    assert row.dispatches == 0 and row.state == "not_sent"
    assert not db_session.query(PublicationAttempt).count()


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["main", "reply"])
async def test_publisher_uses_real_selector_signatures_before_any_network(db_session, sample_task, kind):
    from executor.quantity_engine import publish_action
    configure_task(db_session, sample_task)
    ensure_actions(db_session, sample_task, sample_task.video_urls[0], "main", 1, ["main"])
    parent = db_session.query(QuantityAction).one()
    if kind == "reply":
        parent.state, parent.comment_id = "visible", "7704740811904828100"
        db_session.commit()
        ensure_actions(db_session, sample_task, sample_task.video_urls[0], kind, 1, ["reply"], parent=parent)
    row = db_session.query(QuantityAction).filter_by(kind=kind).one()
    result = await publish_action(db_session.get_bind(), sample_task.id, row.id, stop_requested=lambda: False)
    assert result == "no_accounts_available"
    db_session.refresh(row)
    assert row.state == "not_sent" and row.dispatches == 0


@pytest.mark.parametrize("scope", ["main", "main_comment"])
def test_http_main_success_updates_main_cooldown_not_reply_counter(db_session, sample_account, scope):
    from executor import user_schema
    user_schema._register_runtime_success(db_session, sample_account, scope=scope)
    db_session.refresh(sample_account)
    assert sample_account.today_main_comments == 1 and sample_account.today_replies == 0
    assert sample_account.last_main_comment_at is not None


@pytest.mark.asyncio
async def test_stop_during_preparation_prevents_the_http_mutation(monkeypatch):
    from executor import visibility_publisher as publisher, user_schema as schema
    from services.order_transport import set_task_order_transport, reset_task_order_transport
    stopped = False
    page = SimpleNamespace(url="https://www.tiktok.com/@test/video/123")
    session = SimpleNamespace(aweme_id="123", client=SimpleNamespace())
    async def prepare(*args, **kwargs):
        nonlocal stopped
        stopped = True
        return session
    monkeypatch.setattr(schema, "_preflight_cookie_http_gate", AsyncMock(return_value=None))
    monkeypatch.setattr(schema, "create_page_for_account", AsyncMock(return_value=(object(), page)))
    monkeypatch.setattr(schema, "track_publication_requests", lambda *a: lambda: None)
    monkeypatch.setattr(schema, "_ensure_account_login_ready", AsyncMock(return_value=(True, None)))
    monkeypatch.setattr(schema, "_prepare_user_schema_http_session", prepare)
    monkeypatch.setattr(publisher, "wait_for_runtime_signer_ready", AsyncMock(return_value=True))
    publish = AsyncMock(side_effect=AssertionError("POST after stop"))
    monkeypatch.setattr(schema, "_publish_main_http_first_if_eligible", publish)
    monkeypatch.setattr(schema, "_safe_close", AsyncMock())
    token = set_task_order_transport("http", SimpleNamespace(TIKTOK_NETWORK_IMPL_READY=True))
    try:
        value, reason = await publisher.publish_visibility_http(None, SimpleNamespace(id=1, proxy=None), page.url,
            "main", correlation_id="test", stop_requested=lambda: stopped)
        assert value is None and reason == "stopped_by_user"
        publish.assert_not_awaited()
        schema._safe_close.assert_awaited_once()
    finally:
        reset_task_order_transport(token)


@pytest.mark.asyncio
async def test_unknown_author_does_not_block_other_checks_and_positive_read_stops_early(monkeypatch):
    from unittest.mock import Mock
    from services.publication_recheck import observe_video, apply_observations
    from services import account_service
    from executor import user_schema as schema, engagement_engine, main_visibility
    url = "https://www.tiktok.com/@test/video/123"
    known = SimpleNamespace(account_id=None, identity_keys=[["username", "deleted-publisher"]],
        comment_id="7704740811904828192", video_url=url, aweme_id="123", kind="main", parent_id=None,
        visible_at=None, check_count=0)
    unknown = SimpleNamespace(**{**vars(known), "identity_keys": [], "comment_id": "7704740811904828193"})
    observer = SimpleNamespace(id=8, proxy=None, cookies="sessionid=other; uid_tt=888",
                               _cached_tiktok_username="observer")
    selector = Mock(side_effect=[observer, None])
    monkeypatch.setattr(account_service, "get_free_account_for_reply", selector)
    monkeypatch.setattr(schema, "_preflight_cookie_http_gate", AsyncMock(return_value=None))
    monkeypatch.setattr(schema, "create_page_for_account", AsyncMock(return_value=(object(), SimpleNamespace(url=url))))
    monkeypatch.setattr(schema, "_ensure_account_login_ready", AsyncMock(return_value=(True, None)))
    monkeypatch.setattr(engagement_engine, "warmup_page_for_http", AsyncMock(return_value=({}, True, {})))
    scan = AsyncMock(return_value={"found_ids": [known.comment_id], "absent_ids": [], "complete": True})
    monkeypatch.setattr(main_visibility, "scan_main_comment_ids", scan)
    monkeypatch.setattr(schema, "_safe_close", AsyncMock())
    monkeypatch.setattr(schema, "release_reply_account_reservation", Mock())
    observations = await observe_video(SimpleNamespace(), SimpleNamespace(id=1, settings={}), [unknown, known])
    apply_observations([unknown, known], observations)
    assert known.visibility == "visible" and unknown.visibility == "inconclusive"
    assert scan.call_args.args[2] == [known.comment_id]
    selector.assert_called_once()
    schema.release_reply_account_reservation.assert_called_once_with(8)


@pytest.mark.parametrize("interrupt", [False, True])
def test_background_recheck_advances_only_positive_current_slots_and_owns_shutdown(
        db_session, sample_task, monkeypatch, interrupt):
    import services.publication_recheck as checks
    import executor.browser_pool as pool
    import services.dedicated_proxy_service as dedicated
    configure_task(db_session, sample_task)
    ensure_actions(db_session, sample_task, sample_task.video_urls[0], "main", 1, ["main"])
    row = db_session.query(QuantityAction).one()
    row.state, row.comment_id = "accepted", "7704740811904828192"
    recorded = receipt(db_session, sample_task, account_id=None)
    request_recheck(db_session, sample_task.id)
    db_session.commit()
    stop = threading.Event()
    closed = []
    async def observe(*args):
        try:
            if interrupt:
                stop.set()
                await asyncio.Event().wait()
            return [{"observer_id": 2, "found_ids": [recorded.comment_id]}]
        finally:
            closed.append(True)
    monkeypatch.setattr(checks, "observe_video", observe)
    monkeypatch.setattr(pool, "close_browser_pool", AsyncMock())
    monkeypatch.setattr(dedicated, "activate_dedicated_proxy_for_user", lambda *a: None)
    monkeypatch.setattr(dedicated, "reset_dedicated_proxy", lambda *a: None)
    assert process_due_checks(db_session.get_bind(), stop)
    db_session.expire_all()
    job = db_session.get(PublicationCheckJob, sample_task.id)
    assert job.status == ("pending" if interrupt else "done")
    assert db_session.get(QuantityAction, row.id).state == ("accepted" if interrupt else "visible")
    assert closed and job.lease_token is None
    if not interrupt:
        assert db_session.get(PublicationAttempt, recorded.id).next_check_at is None


@pytest.fixture
async def publications_api(async_db_session):
    from api import publications
    from api.auth import verify_user_header
    from db.database import get_db
    user = User(telegram_id=501, username="owner", role="user", balance=100)
    other = User(telegram_id=502, username="other", role="user", balance=100)
    async_db_session.add_all([user, other])
    await async_db_session.flush()
    task = Task(user_id=user.id, status="done", main_comment="main", video_urls=["https://www.tiktok.com/@test/video/123"],
        settings={"publication_plan": QuantityPlan(main_count=1).model_dump()})
    async_db_session.add(task)
    await async_db_session.commit()
    app = FastAPI()
    app.include_router(publications.router)
    async def session():
        yield async_db_session
    app.dependency_overrides[get_db] = session
    app.dependency_overrides[verify_user_header] = lambda: user.id
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        yield client, task, other, async_db_session


@pytest.mark.asyncio
async def test_publication_api_scopes_all_read_and_mutation_routes(publications_api):
    client, task, other, db = publications_api
    for suffix in ("activity", "report?format=txt", "report?format=csv", "report?format=links"):
        sep = "&" if "?" in suffix else "?"
        assert (await client.get(f"/api/publications/tasks/{task.id}/{suffix}{sep}user_id={task.user_id}")).status_code == 200
        assert (await client.get(f"/api/publications/tasks/{task.id}/{suffix}{sep}user_id={other.id}")).status_code == 403
        assert (await client.get(f"/api/publications/tasks/99999/{suffix}{sep}user_id={task.user_id}")).status_code == 404
    assert (await client.get(f"/api/publications/statistics?user_id={other.id}")).status_code == 403
    assert (await client.post(f"/api/publications/tasks/{task.id}/recheck?user_id={other.id}")).status_code == 403
    assert (await client.post(f"/api/publications/tasks/{task.id}/recheck?user_id={task.user_id}")).status_code == 200
    task.status = "in_progress"
    await db.commit()
    assert (await client.post(f"/api/publications/tasks/{task.id}/recheck?user_id={task.user_id}")).status_code == 409


@pytest.mark.asyncio
async def test_copy_single_text_quantity_order_opens_new_editor_without_receipts(monkeypatch):
    from bot.handlers import quantity_orders as composer
    from aiogram.fsm.context import FSMContext
    from aiogram.fsm.storage.memory import MemoryStorage
    from aiogram.fsm.storage.base import StorageKey
    state = FSMContext(MemoryStorage(), StorageKey(bot_id=1, chat_id=1, user_id=1))
    message = SimpleNamespace(edit_text=AsyncMock())
    task = {"id": 42, "kind": "full", "main_comment": "one", "video_urls": ["url"], "settings": {
        "publication_plan": QuantityPlan(main_count=60, replies_per_main=2).model_dump(),
        "text_variants": ["one"], "replies": ["answer"], "url_results": {"url": {"receipt": "private"}}}}
    monkeypatch.setattr(composer.api_client, "get_pricing", AsyncMock(return_value={"price_success": 1}))
    monkeypatch.setattr(composer.api_client, "get_publication_runtime", AsyncMock(return_value={"browser_slots": 4}))
    monkeypatch.setattr(composer.api_client, "get_user", AsyncMock(return_value={"balance": 100}))
    await composer.copy_full_order(message, state, task, 1)
    data = await state.get_data()
    assert data["texts"] == ["one"] and data["replies"] == ["answer"]
    assert data["publication_plan"]["main_count"] == 60
    assert "url_results" not in data["template_settings"]
    assert await state.get_state() == composer.QuantityStates.preview.state


def test_editing_counts_preserves_user_traffic_and_time_limits():
    from bot.handlers.quantity_orders import make_plan
    original = QuantityPlan(main_count=2, max_dispatches=80, max_seconds=240, traffic_limit_mb=25).model_dump()
    changed = make_plan(5, 2, original)
    assert changed["max_dispatches"] == 80 and changed["max_seconds"] == 240 and changed["traffic_limit_mb"] == 25


def test_worker_restart_finishes_pending_slots_and_keeps_paid_success(paid_keyword_worker, monkeypatch):
    from sqlalchemy.orm import Session
    from executor import quantity_engine
    from executor.quantity_engine import build_result
    bind, task_id, _, _, run = paid_keyword_worker
    with Session(bind) as db:
        task = db.get(Task, task_id)
        task.kind = "full"
        task.settings = {"publication_plan": QuantityPlan(main_count=1, replies_per_main=1).model_dump(),
                         "replies": ["answer"], "text_variants": ["main"], "order_transport_mode": "http"}
        ensure_actions(db, task, task.video_urls[0], "main", 1, ["main"])
        parent = db.query(QuantityAction).one()
        parent.state, parent.comment_id = "visible", "7704740811904828192"
        ensure_actions(db, task, task.video_urls[0], "reply", 1, ["answer"], parent=parent)
        task.settings = {**task.settings, "url_results": {task.video_urls[0]:
            build_result(db, task, task.video_urls[0], QuantityPlan(main_count=1, replies_per_main=1)).to_dict()}}
        db.commit()
    async def finish(**kwargs):
        with Session(bind) as db:
            task = db.get(Task, task_id)
            pending = db.query(QuantityAction).filter_by(kind="reply").one()
            pending.state, pending.comment_id = "visible", "7704740811904828193"
            db.commit()
            return build_result(db, task, task.video_urls[0], QuantityPlan(main_count=1, replies_per_main=1))
    runner = AsyncMock(side_effect=finish)
    monkeypatch.setattr(quantity_engine, "execute_quantity_order", runner)
    result = run()
    runner.assert_awaited_once()
    assert result["status"] == "done" and not result["refunds"]


def test_quantity_template_cannot_remove_reply_library_while_replies_requested():
    from api.templates import _normalize_template_reply_payload
    with pytest.raises(HTTPException) as error:
        _normalize_template_reply_payload({"publication_plan": QuantityPlan(main_count=1, replies_per_main=2).model_dump()}, [])
    assert error.value.status_code == 422


def test_existing_like_receipts_survive_a_new_quantity_reply_wave():
    from executor.reply_visibility import ReplySlot, prepare_reply_like_phase
    a, b = "7704740811904828192", "7704740811904828193"
    result = {"reply_likes": {"target": "all", "requested_per_reply": 2, "by_comment_id": {a: 1},
                              "attempts": {a: {"account-1": "confirmed"}}}}
    slots = [ReplySlot({"reply_id": cid, "parent_comment_id": "7704740811904828100"}, index, True)
             for index, cid in enumerate((a, b))]
    prepare_reply_like_phase(result, slots, target="all", count=2, used=set())
    plan = result["reply_likes"]
    assert plan["by_comment_id"] == {a: 1, b: 0}
    assert plan["attempts"] == {a: {"account-1": "confirmed"}} and plan["state"] == "pending"
    plan["by_comment_id"] = {a: 2, b: 2}
    prepare_reply_like_phase(result, slots, target="all", count=2, used=set())
    assert result["reply_likes"]["state"] == "completed"


@pytest.mark.asyncio
async def test_new_order_cannot_import_forged_publication_or_settlement_evidence(order_db):
    import inspect
    from starlette.requests import Request
    from api import tasks
    payload = tasks.TaskCreate(user_id=1, main_comment="one", text_variants=["one"],
        video_urls=["https://www.tiktok.com/@test/video/1234567890123456789"],
        settings={"publication_plan": QuantityPlan(main_count=1).model_dump(),
            "url_results": {"forged": {"status": "success"}}, "execution_ledger": {"visible": 500},
            "_quantity_resume": {"forged": []}, "_refund_settlement_pending": True,
            "keyword_video_bindings": {"other": {}}, "failed_urls": ["foreign"]})
    result = await inspect.unwrap(tasks.create_task)(request=Request({"type": "http", "headers": []}),
        task_data=payload, db=order_db, auth_user_id=1)
    row = await order_db.get(Task, result["task_id"])
    assert row.status == "pending" and row.progress == 0
    for field in ("url_results", "execution_ledger", "_quantity_resume", "_refund_settlement_pending",
                  "keyword_video_bindings", "failed_urls"):
        assert field not in row.settings
    assert row.settings["publication_plan"]["main_count"] == 1


@pytest.mark.asyncio
async def test_legacy_finalizer_keeps_quantity_proof_without_spending_new_reads(db_session, sample_task, monkeypatch):
    from executor.quantity_engine import build_result
    from executor import reply_visibility as visibility
    from services.task_execution_ledger import url_result_counts_as_success
    configure_task(db_session, sample_task)
    sample_task.settings = {**sample_task.settings, "verify_reply_visibility": True, "reply_like_count": 0}
    ensure_actions(db_session, sample_task, sample_task.video_urls[0], "main", 1, ["main"])
    action = db_session.query(QuantityAction).one()
    action.state, action.comment_id = "visible", "7704740811904828192"
    db_session.commit()
    result = build_result(db_session, sample_task, sample_task.video_urls[0], QuantityPlan(main_count=1)).to_dict()
    monkeypatch.setattr(visibility, "scan_reply_comment_ids", AsyncMock(side_effect=AssertionError("extra paid read")))
    finalized = await visibility.verify_order_reply_visibility(db_session, sample_task, sample_task.video_urls[0], result)
    assert url_result_counts_as_success(finalized)
    visibility.scan_reply_comment_ids.assert_not_awaited()


@pytest.mark.asyncio
async def test_old_template_confirm_callback_uses_template_id_instead_of_reply_count(monkeypatch):
    from bot.handlers import quantity_orders as composer
    from aiogram.fsm.context import FSMContext
    from aiogram.fsm.storage.memory import MemoryStorage
    from aiogram.fsm.storage.base import StorageKey
    state = FSMContext(MemoryStorage(), StorageKey(bot_id=1, chat_id=1, user_id=1))
    await state.update_data(links=["url"])
    template = {"id": 42, "main_comment": "one", "settings": {"text_variants": ["one"],
                "publication_plan": QuantityPlan(main_count=5).model_dump()}}
    get = AsyncMock(return_value=template)
    monkeypatch.setattr(composer.api_client, "get_task_template", get)
    monkeypatch.setattr(composer, "get_user_backend_id", AsyncMock(return_value=1))
    monkeypatch.setattr(composer, "show_preview", AsyncMock())
    callback = SimpleNamespace(data="quicktpl:confirm:42:3", from_user=SimpleNamespace(id=1),
                               message=SimpleNamespace(), answer=AsyncMock())
    await composer.use_template(callback, state)
    assert get.call_args.args == (42, 1)


@pytest.mark.asyncio
async def test_account_import_deduplicates_rotated_non_session_cookies_and_rejects_garbage(
        async_db_session, isolated_proxy_leases, monkeypatch):
    from services import account_import_jobs as jobs
    from db.models import Account
    from sqlalchemy import select
    monkeypatch.setattr(jobs, "get_redis_client", lambda: isolated_proxy_leases)
    job = await jobs.create_import_job(async_db_session, [
        {"label": "first", "cookies": "sessionid=stable; msToken=old"},
        {"label": "second", "cookies": "sessionid=stable; msToken=new"},
        {"label": "broken", "cookies": "nonsense"}])
    assert await jobs.process_import_job(async_sessionmaker(async_db_session.bind, expire_on_commit=False))
    await async_db_session.refresh(job)
    assert job.processed == 3 and job.counters["added"] == 1 and job.counters["updated"] == 1
    assert job.counters["skipped"] == 1 and job.counters["duplicates_by_cookies"] == 1
    assert len((await async_db_session.execute(select(Account))).scalars().all()) == 1
    assert job.results[-1]["error"] == "invalid_cookie_format"
