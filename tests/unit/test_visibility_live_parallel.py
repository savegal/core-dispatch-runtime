"""Visibility evidence, consistent reporting and concurrent order execution."""
import asyncio
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from queue import Empty, Queue
import threading
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import fakeredis
import pytest
from sqlalchemy.orm import Session

from db.models import PublicationAttempt, QuantityAction, Task
from executor import executor, quantity_engine as quantity
from services.publication_journal import utcnow
from services.publication_recheck import apply_observations, observe_video
from services.publication_reports import activity_snapshot, export_report
from services.publication_runtime import update_publication_runtime
from tests.unit.test_quantity_redesign_recovery import configure_task


@pytest.mark.asyncio
@pytest.mark.parametrize("case", ["author_only", "changed_author", "public"])
async def test_visibility_reads_the_original_author_only_after_two_independent_absences(
        db_session, sample_task, sample_account, monkeypatch, case):
    from executor import user_schema as schema, engagement_engine, main_visibility
    from executor.visibility_support import account_identity_keys
    from services import account_service

    configure_task(db_session, sample_task)
    author_id, url = sample_account.id, sample_task.video_urls[0]
    sample_account.cookies = '{"sessionid":"author-session","uid_tt":"author-uid"}'
    db_session.commit()
    cid = "7704740811904828192"
    row = SimpleNamespace(account_id=author_id, identity_keys=list(account_identity_keys(sample_account)),
        video_url=url, aweme_id="7704740811904828100", comment_id=cid, kind="main", parent_id=None,
        visibility="unchecked", visible_at=None, check_count=0)
    if case == "changed_author":
        sample_account.cookies = '{"sessionid":"different-session","uid_tt":"different-uid"}'
        db_session.commit()
    readers = [SimpleNamespace(id=i, proxy=None, cookies=f'{{"sessionid":"s{i}","uid_tt":"u{i}"}}') for i in (101, 102)]
    selector = Mock(side_effect=[*readers, None])
    monkeypatch.setattr(account_service, "get_free_account_for_reply", selector)
    monkeypatch.setattr(schema, "_preflight_cookie_http_gate", AsyncMock(return_value=None))
    async def page(account):
        return object(), SimpleNamespace(url=url, reader_id=account.id)
    monkeypatch.setattr(schema, "create_page_for_account", page)
    monkeypatch.setattr(schema, "_ensure_account_login_ready", AsyncMock(return_value=(True, None)))
    monkeypatch.setattr(engagement_engine, "warmup_page_for_http", AsyncMock(return_value=({}, True, {})))
    scanned = []
    async def scan(page, *_args, **_kwargs):
        scanned.append(page.reader_id)
        visible = page.reader_id == author_id or case == "public"
        return dict(found_ids=[cid] if visible else [], absent_ids=[] if visible else [cid], complete=True)
    monkeypatch.setattr(main_visibility, "scan_main_comment_ids", scan)
    monkeypatch.setattr(schema, "_safe_close", AsyncMock())
    monkeypatch.setattr(schema, "release_reply_account_reservation", Mock())
    apply_observations([row], await observe_video(db_session, sample_task, [row]))
    expected = {"author_only": ("author_only", [101, 102, author_id]),
                "changed_author": ("not_found", [101, 102]), "public": ("visible", [101])}
    assert (row.visibility, scanned) == expected[case]
    db_session.refresh(sample_account)
    assert sample_account.assignment_lease_token is None


def test_partial_and_duplicate_reads_do_not_prove_author_only():
    cid = "7704740811904828192"
    row = SimpleNamespace(account_id=1, comment_id=cid, visibility="unchecked", visible_at=None, check_count=0)
    author = dict(observer_id=1, role="author", found_ids=[cid])
    complete = dict(observer_id=2, absent_ids=[cid], complete_ids=[cid])
    partial = dict(observer_id=3, absent_ids=[cid], complete=False)
    apply_observations([row], [author, complete, complete, partial])
    assert row.visibility == "inconclusive"
    stamp = utcnow() - timedelta(minutes=5)
    row.visibility, row.checked_at = "visible", stamp
    apply_observations([row], [partial])
    assert row.visibility == "visible" and row.checked_at == stamp


def test_live_text_and_export_use_one_current_visibility_per_cid(db_session, sample_task):
    from bot.services.live_orders import render_live
    configure_task(db_session, sample_task)
    now, url = utcnow(), sample_task.video_urls[0]
    cids = [str(7704740811904828190 + n) for n in range(4)]
    for index, (cid, visibility, stamp) in enumerate([
        (cids[0], "visible", now - timedelta(minutes=5)), (cids[0], "author_only", now),
        (cids[1], "visible", now), (cids[2], "not_found", now), (cids[3], "unchecked", None),
        (None, "unchecked", None),
    ]):
        db_session.add(PublicationAttempt(id=f"attempt-{index}", task_id=sample_task.id, video_url=url,
            aweme_id="7704740811904828100", kind="main", text="same", text_hash="h", comment_id=cid,
            status="accepted" if cid else "unknown", visibility=visibility, wire_count=1,
            started_at=now, checked_at=stamp, visible_at=stamp if visibility == "visible" else None))
    db_session.commit()
    live = activity_snapshot(db_session, sample_task)
    exported = activity_snapshot(db_session, sample_task, include_texts=True)
    assert live["summary"] == exported["summary"]
    summary = live["summary"]
    assert [summary[key] for key in ("dispatched", "accepted", "visible", "author_only", "not_found",
                                    "pending_checks", "uncertain", "ever_visible")] == [6, 4, 1, 1, 1, 1, 1, 2]
    assert "Только автору" in render_live(live)
    links = export_report(db_session, sample_task, "links")
    assert cids[0] not in links and cids[1] in links
    assert "Только автору: 1" in export_report(db_session, sample_task, "txt")


def test_late_visibility_updates_the_same_slot_without_reviving_old_evidence(db_session, sample_task):
    from services.publication_journal import sync_public_evidence
    from services.publication_recheck import sync_visible_actions
    configure_task(db_session, sample_task)
    url, cid, now = sample_task.video_urls[0], "7704740811904828192", utcnow()
    quantity.ensure_actions(db_session, sample_task, url, "main", 1, ["main"])
    action = db_session.query(QuantityAction).one()
    action.state, action.comment_id = "absent", cid
    sample_task.settings = {**sample_task.settings, "url_results": {url: {"engagement": {"main": {
        "variants_published": [{"comment_id": cid, "public_confirmed": True}]}}}}}
    old, current = [PublicationAttempt(id=identity, task_id=sample_task.id, video_url=url,
        kind="main", text="main", text_hash="h", status="accepted", comment_id=cid,
        visibility=visibility, checked_at=stamp, started_at=now) for identity, visibility, stamp in (
        ("old", "visible", now - timedelta(minutes=5)), ("current", "author_only", now))]
    db_session.add_all([old, current])
    db_session.commit()
    sync_public_evidence(db_session, sample_task)
    sync_visible_actions(db_session, sample_task.id)
    db_session.commit()
    db_session.refresh(action)
    assert action.state == "absent" and current.visibility == "author_only"
    apply_observations([current], [dict(observer_id=77, found_ids=[cid])], now=now + timedelta(minutes=1))
    db_session.flush()
    sync_visible_actions(db_session, sample_task.id)
    db_session.commit()
    db_session.refresh(action)
    assert action.state == "visible" and action.comment_id == cid


def test_order_slots_are_shared_and_renewed_only_by_their_owner(monkeypatch):
    from services import redis_service
    redis = fakeredis.FakeRedis(decode_responses=True)
    monkeypatch.setattr(redis_service, "get_redis_client", lambda: redis)
    first = executor._acquire_parallel_task_slot(1, 2)
    second = executor._acquire_parallel_task_slot(2, 2)
    try:
        assert first and second and first.key != second.key
        assert executor._acquire_parallel_task_slot(3, 2) is None
        redis.delete(first.key)
        replacement = executor._acquire_parallel_task_slot(3, 2)
        try:
            assert replacement and replacement.key == first.key
            assert not first.renew() and second.renew()
            executor._release_global_task_execution_lock(first, 1)
            assert replacement.renew()
        finally:
            executor._release_global_task_execution_lock(replacement, 3)
    finally:
        executor._release_global_task_execution_lock(first, 1)
        executor._release_global_task_execution_lock(second, 2)


def test_two_workers_can_run_distinct_orders_at_the_same_time(db_session, sample_task, monkeypatch):
    from services import redis_service
    redis = fakeredis.FakeRedis(decode_responses=True)
    monkeypatch.setattr(redis_service, "get_redis_client", lambda: redis)
    engine = db_session.get_bind()
    sample_task.status = "pending"
    other = Task(user_id=sample_task.user_id, status="pending", main_comment="main", video_urls=sample_task.video_urls)
    db_session.add(other)
    db_session.commit()
    ids = {sample_task.id, other.id}
    queue = Queue()
    for identity in ids:
        queue.put(identity)
    shutdown, both = threading.Event(), threading.Barrier(2)
    reached = []
    def dequeue():
        try:
            return queue.get_nowait()
        except Empty:
            return None
    def process(task_id):
        with Session(engine) as db:
            if executor._claim_pending_task(db, task_id):
                reached.append(task_id)
                both.wait(timeout=3)
                shutdown.set()
    monkeypatch.setattr(executor, "_shutdown_event", shutdown)
    monkeypatch.setattr(executor, "get_event_loop", lambda: SimpleNamespace(is_closed=lambda: True))
    monkeypatch.setattr(executor, "get_sync_db", lambda: iter([Session(engine)]))
    monkeypatch.setattr(executor, "dequeue_task", dequeue)
    monkeypatch.setattr(executor, "process_task", process)
    monkeypatch.setattr(executor, "_should_defer_loaded_task_for_parsing", lambda *_: False)
    monkeypatch.setattr(executor, "_sleep_until_next_worker_poll", lambda *_: shutdown.wait(.01))
    monkeypatch.setattr(executor, "update_executor_heartbeat", Mock())
    with ThreadPoolExecutor(2) as pool:
        futures = [pool.submit(executor.worker, number, 2) for number in (1, 2)]
        try:
            assert shutdown.wait(4)
        finally:
            shutdown.set()
            for future in futures:
                future.result(timeout=5)
    assert set(reached) == ids
    db_session.expire_all()
    assert all(db_session.get(Task, identity).attempts == 1 for identity in ids)


def test_same_order_has_one_atomic_claim_even_across_sqlite_connections(db_session, sample_task):
    engine, identity = db_session.get_bind(), sample_task.id
    sample_task.status = "pending"
    db_session.commit()
    barrier = threading.Barrier(2)
    def claim():
        with Session(engine) as db:
            barrier.wait(timeout=2)
            return executor._claim_pending_task(db, identity) is not None
    with ThreadPoolExecutor(2) as pool:
        claims = [pool.submit(claim) for _ in range(2)]
        assert sorted(job.result(timeout=4) for job in claims) == [False, True]
    db_session.expire_all()
    assert db_session.get(Task, identity).attempts == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("order_override", [False, True])
async def test_a_fast_publication_is_checked_while_other_senders_are_busy(db_session, sample_task, monkeypatch, order_override):
    from executor import engagement_engine
    configure_task(db_session, sample_task, mains=3)
    update_publication_runtime(db_session, dict(send_workers=1 if order_override else 2, check_workers=1, check_batch_size=1))
    if order_override:
        sample_task.settings = {**sample_task.settings, "publication_options": {"send_workers": 2}}
        db_session.commit()
    monkeypatch.setattr(engagement_engine, "resolve_order_video_url", AsyncMock(
        return_value=(sample_task.video_urls[0], "7704740811904828100")))
    third_started, first_verified = asyncio.Event(), asyncio.Event()
    active, peak, checked_while_sending = 0, 0, False

    async def publish(engine, task_id, identity, **kwargs):
        nonlocal active, peak
        with Session(engine) as db:
            slot = db.get(QuantityAction, identity).slot
        active += 1
        peak = max(active, peak)
        try:
            if slot == 2:
                await first_verified.wait()
            if slot == 3:
                third_started.set()
            with Session(engine) as db:
                action = db.get(QuantityAction, identity)
                action.state, action.comment_id, action.dispatches = "accepted", str(7704740811904828100 + slot), 1
                db.commit()
            return "accepted"
        finally:
            active -= 1

    async def verify(engine, task_id, identities, *args, **kwargs):
        nonlocal checked_while_sending
        assert len(identities) == 1
        with Session(engine) as db:
            slot = db.get(QuantityAction, identities[0]).slot
        if slot == 1:
            checked_while_sending = active > 0
            await third_started.wait()
        with Session(engine) as db:
            db.get(QuantityAction, identities[0]).state = "visible"
            db.commit()
        if slot == 1:
            first_verified.set()

    monkeypatch.setattr(quantity, "publish_action", publish)
    monkeypatch.setattr(quantity, "verify_actions", verify)
    result = await asyncio.wait_for(quantity.execute_quantity_order(db_session, sample_task,
        sample_task.video_urls[0], ["main"], sample_task.id), 2)
    assert result.to_dict()["publication_complete"] and checked_while_sending and peak == 2


def test_background_workers_claim_distinct_jobs_and_show_each_committed_batch(db_session, sample_task, monkeypatch):
    from db.models import PublicationCheckJob
    from executor import browser_pool
    from services import dedicated_proxy_service, publication_recheck
    from tests.unit.test_quantity_redesign_recovery import receipt

    configure_task(db_session, sample_task)
    other = Task(user_id=sample_task.user_id, status="done", main_comment="main", video_urls=sample_task.video_urls)
    db_session.add(other)
    db_session.commit()
    update_publication_runtime(db_session, dict(check_batch_size=1, late_workers=2))
    first = receipt(db_session, sample_task, cid="7704740811904828192")
    second = receipt(db_session, sample_task, cid="7704740811904828193")
    receipt(db_session, other, cid="7704740811904828194")
    for task in (sample_task, other):
        publication_recheck.request_recheck(db_session, task.id)
    db_session.commit()
    engine, first_id, second_id = db_session.get_bind(), first.id, second.id
    barrier, first_reads = threading.Barrier(2), set()
    saw_committed_batch = []
    async def observe(db, task, rows):
        assert len(rows) == 1
        row = rows[0]
        if task.id not in first_reads:
            first_reads.add(task.id)
            await asyncio.to_thread(barrier.wait, 3)
        else:
            with Session(engine) as reader:
                first_row = reader.get(PublicationAttempt, first_id)
                second_row = reader.get(PublicationAttempt, second_id)
                job = reader.get(PublicationCheckJob, task.id)
                saw_committed_batch.append(job.result["processed"] == 1 and
                    sum(item.visibility == "visible" for item in (first_row, second_row)) == 1)
        return [dict(observer_id=900, found_ids=[row.comment_id])]
    monkeypatch.setattr(publication_recheck, "observe_video", observe)
    monkeypatch.setattr(browser_pool, "close_browser_pool", AsyncMock())
    monkeypatch.setattr(dedicated_proxy_service, "activate_dedicated_proxy_for_user", lambda *_: None)
    monkeypatch.setattr(dedicated_proxy_service, "reset_dedicated_proxy", lambda *_: None)
    with ThreadPoolExecutor(2) as pool:
        jobs = [pool.submit(publication_recheck.process_due_checks, engine) for _ in range(2)]
        assert all(job.result(timeout=6) for job in jobs)
    db_session.expire_all()
    assert len(first_reads) == 2 and saw_committed_batch == [True]
    assert all(job.status == "done" for job in db_session.query(PublicationCheckJob))


@pytest.mark.asyncio
async def test_admin_runtime_api_persists_changes_and_rejects_invalid_ranges(db_session, monkeypatch):
    from contextlib import contextmanager
    from fastapi import FastAPI, Header, HTTPException
    from httpx import ASGITransport, AsyncClient
    from api import app_settings
    from api.auth import verify_admin_header
    from config.settings import get_settings
    from services.publication_runtime import load_publication_runtime

    engine = db_session.get_bind()
    @contextmanager
    def get_db():
        with Session(engine) as db:
            yield db
    def admin(x_admin_token: str | None = Header(default=None)):
        if x_admin_token != "fixture":
            raise HTTPException(403)
    monkeypatch.setattr(app_settings, "get_sync_db_context", get_db)
    app = FastAPI()
    app.include_router(app_settings.router)
    app.dependency_overrides[verify_admin_header] = admin
    route = "/api/settings/publication_runtime"
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        assert (await client.get(route)).status_code == 403
        headers = {"X-Admin-Token": "fixture"}
        settings = get_settings()
        monkeypatch.setattr(settings, "MAX_CONCURRENT_BROWSERS", 4)
        response = await client.post(route, headers=headers, json={"live_interval": 4})
        assert response.status_code == 200
        assert response.json()["send_workers"] == 3
        monkeypatch.setattr(settings, "MAX_CONCURRENT_BROWSERS", 8)
        assert (await client.get(route, headers=headers)).json()["send_workers"] == 7
        monkeypatch.setattr(settings, "MAX_CONCURRENT_BROWSERS", 1)
        single_slot = (await client.get(route, headers=headers)).json()
        assert single_slot["send_workers"] == single_slot["effective_readers"] == 1
        monkeypatch.setattr(settings, "MAX_CONCURRENT_BROWSERS", 4)
        response = await client.post(route, headers=headers, json={"send_workers": 3, "check_workers": 2,
            "late_delays": [10, 90], "live_interval": 4})
        assert response.status_code == 200
        assert response.json()["effective_senders"] + response.json()["effective_readers"] <= max(
            2, response.json()["browser_slots"])
        assert (await client.post(route, headers=headers, json={"check_workers": 0})).status_code == 422
        assert (await client.post(route, headers=headers, json={"late_delays": [90, 10]})).status_code == 422
        assert (await client.get(route, headers=headers)).json()["check_workers"] == 2
    with Session(engine) as worker:
        runtime = load_publication_runtime(worker)
    assert runtime.late_delays == (10, 90) and runtime.live_interval == 4


@pytest.mark.asyncio
async def test_telegram_admin_can_save_runtime_changes(monkeypatch):
    from bot.handlers import admin
    from bot.keyboards.admin import get_admin_menu_keyboard
    from services.publication_runtime import PublicationRuntime, runtime_payload

    settings = PublicationRuntime(send_workers=3, check_workers=2, live_interval=4)
    saver = AsyncMock(return_value=runtime_payload(settings))
    monkeypatch.setattr(admin, "get_user_data", AsyncMock(return_value=(1, "admin", True)))
    monkeypatch.setattr(admin.api_client, "update_publication_runtime", saver)
    message = SimpleNamespace(from_user=SimpleNamespace(id=5), text="send_workers=3\ncheck_workers=2\nlive_interval=4",
                              answer=AsyncMock())
    state = SimpleNamespace(clear=AsyncMock())
    await admin.publication_runtime_save(message, state)
    saver.assert_awaited_once_with({"send_workers": 3, "check_workers": 2, "live_interval": 4})
    state.clear.assert_awaited_once()
    assert "Обновление карточки · каждые 4 с" in message.answer.await_args.args[0]
    assert any(button.callback_data == "admin:publication_runtime"
               for row in get_admin_menu_keyboard().inline_keyboard for button in row)
