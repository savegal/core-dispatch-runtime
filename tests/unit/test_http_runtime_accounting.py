"""Metered HTTP execution must survive cancellation and respect durable controls."""
import asyncio
from datetime import datetime, timezone
import threading
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest
from sqlalchemy import event, update
from sqlalchemy.orm import Session

from db.models import Account, QuantityAction, Task, TaskTrafficBucket
from executor import quantity_engine as quantity
from services.quantity_plan import quantity_plan
from tests.unit.test_quantity_redesign_recovery import configure_task


def traffic_key(task):
    return task.id, 42, "reads", datetime.now(timezone.utc).replace(minute=0, second=0, microsecond=0)


def spend_budget(db, task):
    task_id, account, category, hour = traffic_key(task)
    db.add(TaskTrafficBucket(task_id=task_id, account_key=account, category=category, hour=hour,
                            received_bytes=1048576, sent_bytes=0, requests=1))
    db.commit()


@pytest.mark.asyncio
@pytest.mark.parametrize("worker_fails", [False, True])
async def test_cancelled_traffic_flush_owns_commit_and_restores_only_failed_batch(
        db_session, sample_task, monkeypatch, worker_fails):
    from services import traffic_meter as traffic
    entered, release = threading.Event(), threading.Event()
    original, fail = traffic.persist_sizes, worker_fails
    meter = traffic.TrafficMeter(db_session.get_bind(), 42)
    key = traffic_key(sample_task)
    meter.buffer[key] = [100, 20, 1]

    def persist(engine, batch):
        entered.set()
        assert release.wait(3)
        if fail:
            raise RuntimeError("simulated database outage")
        original(engine, batch)

    monkeypatch.setattr(traffic, "persist_sizes", persist)
    owner = asyncio.create_task(meter.flush())
    try:
        assert await asyncio.wait_for(asyncio.to_thread(entered.wait, 1), 1.5)
        for _ in range(3):
            owner.cancel()
            await asyncio.sleep(.01)
        assert not owner.done(), "The SQL writer is still running"
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(owner, 2)
    finally:
        release.set()
        await asyncio.gather(owner, return_exceptions=True)
    assert dict(meter.buffer) == ({key: [100, 20, 1]} if worker_fails else {})
    fail = False
    await meter.flush()
    db_session.expire_all()
    row = db_session.query(TaskTrafficBucket).one()
    assert (row.received_bytes, row.sent_bytes, row.requests) == (100, 20, 1)


@pytest.mark.asyncio
async def test_cancelled_meter_close_drains_the_pending_measurement(db_session, sample_task):
    from services.traffic_meter import TrafficMeter
    entered, release = asyncio.Event(), asyncio.Event()
    meter = TrafficMeter(db_session.get_bind(), 42)

    async def sizes():
        entered.set()
        await release.wait()
        return dict(responseBodySize=100, responseHeadersSize=10, requestBodySize=0, requestHeadersSize=20)

    meter.finished(SimpleNamespace(_traffic_task_id=sample_task.id, resource_type="fetch", method="GET", sizes=sizes))
    await entered.wait()
    owner = asyncio.create_task(meter.close())
    try:
        for _ in range(3):
            await asyncio.sleep(.01)
            owner.cancel()
        await asyncio.sleep(.01)
        assert not owner.done()
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(owner, 2)
    finally:
        release.set()
        await asyncio.gather(owner, return_exceptions=True)
        await meter.close()
    row = db_session.query(TaskTrafficBucket).one()
    assert (row.received_bytes, row.sent_bytes, row.requests) == (110, 20, 1)


@pytest.mark.asyncio
async def test_failed_meter_close_can_be_retried_without_duplicate_writes(db_session, sample_task, monkeypatch):
    from services import traffic_meter as traffic
    meter = traffic.TrafficMeter(db_session.get_bind(), 42)
    meter.buffer[traffic_key(sample_task)] = [10, 5, 1]
    original = traffic.persist_sizes
    monkeypatch.setattr(traffic, "persist_sizes", Mock(side_effect=RuntimeError("database unavailable")))
    await meter.close()
    assert meter.buffer
    monkeypatch.setattr(traffic, "persist_sizes", original)
    await meter.close()
    await meter.close()
    row = db_session.query(TaskTrafficBucket).one()
    assert (row.received_bytes, row.sent_bytes, row.requests) == (10, 5, 1)


@pytest.mark.asyncio
async def test_hung_transfer_size_does_not_block_meter_close(db_session, sample_task, monkeypatch):
    from services import traffic_meter as traffic

    monkeypatch.setattr(traffic, "REQUEST_SIZES_TIMEOUT_SECONDS", 0.02)
    meter = traffic.TrafficMeter(db_session.get_bind(), 42)
    released = asyncio.Event()
    slow = SimpleNamespace(_traffic_task_id=sample_task.id, resource_type="fetch", method="GET", sizes=released.wait)
    meter.finished(slow)
    meter.buffer[traffic_key(sample_task)] = [10, 5, 1]
    try:
        await asyncio.wait_for(meter.close(), timeout=1)
        assert not meter.pending
        assert db_session.query(TaskTrafficBucket).one().received_bytes == 10
    finally:
        released.set()


@pytest.mark.parametrize("lifecycle", ["expired", "closed", "changed"])
def test_quantity_result_is_an_independent_immutable_sql_snapshot(db_session, sample_task, lifecycle):
    configure_task(db_session, sample_task)
    task_id, url, engine = sample_task.id, sample_task.video_urls[0], db_session.get_bind()
    sample_task.settings = {**sample_task.settings, "url_results": {url: {"reply_likes": {"confirmed": 7}}}}
    db_session.commit()
    quantity.ensure_actions(db_session, sample_task, url, "main", 1, ["main"])
    with Session(engine, expire_on_commit=False) as source:
        task = source.get(Task, task_id)
        action = source.query(QuantityAction).one()
        action.state, action.comment_id = "visible", "7704740811904828100"
        source.commit()
        result = quantity.build_result(source, task, url, quantity_plan(task.settings))
        if lifecycle == "changed":
            action.state = "accepted"
            task.settings = {**task.settings, "url_results": {url: {"reply_likes": {"confirmed": 99}}}}
            source.commit()
        else:
            source.expire_all()
            if lifecycle == "closed":
                source.close()
        queries = []
        def query(_connection, _cursor, sql, *_):
            queries.append(sql)
        event.listen(engine, "before_cursor_execute", query)
        try:
            payload = result.to_dict()
            payload["quantity_slots"][0]["state"] = "modified-by-caller"
            payload["reply_likes"]["confirmed"] = 100
            again = result.to_dict()
        finally:
            event.remove(engine, "before_cursor_execute", query)
        assert not queries, "Serialization reopened the source SQL session"
        assert again["quantity_slots"][0]["state"] == "visible"
        assert again["reply_likes"]["confirmed"] == 7
        assert again["publication_complete"] is True


@pytest.mark.asyncio
@pytest.mark.parametrize("gate", ["stopped", "budget"])
async def test_no_initial_navigation_or_recheck_when_order_is_already_blocked(
        db_session, sample_task, monkeypatch, gate):
    from executor import engagement_engine
    from services import publication_recheck
    configure_task(db_session, sample_task, traffic_limit_mb=1)
    if gate == "stopped":
        sample_task.status = "stopped"
        db_session.commit()
    else:
        spend_budget(db_session, sample_task)
    resolver = AsyncMock(side_effect=AssertionError("Blocked order must not navigate"))
    observer = AsyncMock(side_effect=AssertionError("Blocked order must not read comments"))
    monkeypatch.setattr(engagement_engine, "resolve_order_video_url", resolver)
    monkeypatch.setattr(publication_recheck, "observe_video", observer)
    result = await quantity.execute_quantity_order(db_session, sample_task, sample_task.video_urls[0],
        ["main"], sample_task.id, stop_requested=lambda: False)
    assert result.error == ("stopped_by_user" if gate == "stopped" else "quantity_traffic_budget_exhausted")
    resolver.assert_not_awaited()
    observer.assert_not_awaited()


@pytest.mark.asyncio
async def test_live_verification_does_not_spend_an_exhausted_traffic_budget(db_session, sample_task, monkeypatch):
    from services import publication_recheck
    configure_task(db_session, sample_task, traffic_limit_mb=1)
    quantity.ensure_actions(db_session, sample_task, sample_task.video_urls[0], "main", 1, ["main"])
    action = db_session.query(QuantityAction).one()
    action.state, action.comment_id = "accepted", "7704740811904828100"
    db_session.commit()
    spend_budget(db_session, sample_task)
    observer = AsyncMock(side_effect=AssertionError("Budget exhausted"))
    monkeypatch.setattr(publication_recheck, "observe_video", observer)
    await quantity.verify_actions(db_session.get_bind(), sample_task.id, [action.id], lambda: False)
    observer.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("gate", ["stopped", "budget"])
async def test_sql_controls_interrupt_inflight_work_even_when_redis_signal_is_missing(db_session, sample_task, monkeypatch, gate):
    configure_task(db_session, sample_task, traffic_limit_mb=1)
    entered, cleaned = asyncio.Event(), asyncio.Event()
    engine, task_id, url = db_session.get_bind(), sample_task.id, sample_task.video_urls[0]
    async def stalled(*args, **kwargs):
        entered.set()
        try:
            await asyncio.Event().wait()
        finally:
            cleaned.set()
    monkeypatch.setattr(quantity, "_execute_quantity_publications", stalled)
    monkeypatch.setattr(quantity, "TASK_CONTROL_POLL_SECONDS", .05, raising=False)
    owner = asyncio.create_task(quantity.execute_quantity_order(db_session, sample_task, url, ["main"], task_id,
                                                               stop_requested=lambda: False))
    try:
        await asyncio.wait_for(entered.wait(), 1)
        with Session(engine) as stopper:
            if gate == "stopped":
                stopper.execute(update(Task).where(Task.id == task_id).values(status="stopped"))
                stopper.commit()
            else:
                spend_budget(stopper, stopper.get(Task, task_id))
        result = await asyncio.wait_for(asyncio.shield(owner), .8)
        assert result.error == ("stopped_by_user" if gate == "stopped" else "quantity_traffic_budget_exhausted")
        assert cleaned.is_set()
    finally:
        owner.cancel()
        await asyncio.gather(owner, return_exceptions=True)


@pytest.mark.asyncio
@pytest.mark.parametrize("proof", ["partial", "positive_before_reply"])
async def test_observer_stops_before_next_reader_when_traffic_runs_out(db_session, sample_task, monkeypatch, proof):
    from executor import user_schema as schema, engagement_engine, main_visibility, reply_visibility
    from services import account_service
    from services.publication_recheck import observe_video
    configure_task(db_session, sample_task, traffic_limit_mb=1)
    url = sample_task.video_urls[0]
    publisher = SimpleNamespace(account_id=None, identity_keys=[["username", "publisher"]],
        video_url=url, aweme_id="7704740811904828100", comment_id="7704740811904828192", kind="main", parent_id=None)
    reader = SimpleNamespace(id=8, proxy=None, cookies="sessionid=reader; uid_tt=8", _cached_tiktok_username="reader")
    selector = Mock(side_effect=[reader, None])
    monkeypatch.setattr(account_service, "get_free_account_for_reply", selector)
    monkeypatch.setattr(schema, "_preflight_cookie_http_gate", AsyncMock(return_value=None))
    monkeypatch.setattr(schema, "create_page_for_account", AsyncMock(return_value=(object(), SimpleNamespace(url=url))))
    monkeypatch.setattr(schema, "_ensure_account_login_ready", AsyncMock(return_value=(True, None)))
    monkeypatch.setattr(engagement_engine, "warmup_page_for_http", AsyncMock(return_value=({}, True, {})))
    async def scan(*args, **kwargs):
        spend_budget(db_session, sample_task)
        return dict(found_ids=[publisher.comment_id] if proof == "positive_before_reply" else [],
                    absent_ids=[], complete=proof == "positive_before_reply")
    reply_scan = AsyncMock(side_effect=AssertionError("Budget exhausted before next branch"))
    monkeypatch.setattr(main_visibility, "scan_main_comment_ids", scan)
    monkeypatch.setattr(reply_visibility, "scan_reply_comment_ids", reply_scan)
    monkeypatch.setattr(schema, "_safe_close", AsyncMock())
    monkeypatch.setattr(schema, "release_reply_account_reservation", Mock())
    rows = [publisher]
    if proof == "positive_before_reply":
        rows.append(SimpleNamespace(**{**vars(publisher), "kind": "reply", "parent_id": publisher.comment_id,
                                      "comment_id": "7704740811904828193"}))
    observations = await observe_video(db_session, sample_task, rows)
    assert selector.call_count == 1
    reply_scan.assert_not_awaited()
    assert any(item.get("reason") == "quantity_traffic_budget_exhausted" for item in observations)
    if proof == "positive_before_reply":
        assert publisher.comment_id in observations[0]["found_ids"]


@pytest.mark.asyncio
async def test_recheck_excludes_other_batch_authors_and_recent_readers_and_stops_after_two_empty_reads(
    db_session, sample_task, sample_account, monkeypatch,
):
    from executor import user_schema as schema, engagement_engine, main_visibility
    from services import account_service
    from services.publication_recheck import OBSERVER_COOLDOWN_SECONDS, observe_video
    from services.publication_journal import utcnow
    from db.models import PublicationAttempt

    configure_task(db_session, sample_task)
    url = sample_task.video_urls[0]
    video = schema.extract_tiktok_media_id(url)
    db_session.add(PublicationAttempt(id="other-batch", task_id=sample_task.id, account_id=sample_account.id,
        video_url=url, aweme_id=video, kind="main", text="fixture", text_hash="fixture", status="accepted",
        wire_count=1, comment_id="7704740811904828193", checked_at=utcnow(), observers=[{"account_id": 42}]))
    db_session.commit()
    readers = [SimpleNamespace(id=i, proxy=None, cookies=f"sessionid=reader{i}; uid_tt={i}",
                               _cached_tiktok_username=f"reader{i}") for i in (71, 72, 73)]
    selector = Mock(side_effect=readers)
    monkeypatch.setattr(account_service, "get_free_account_for_reply", selector)
    monkeypatch.setattr(schema, "_preflight_cookie_http_gate", AsyncMock(return_value=None))
    monkeypatch.setattr(schema, "create_page_for_account", AsyncMock(side_effect=lambda _: (object(), SimpleNamespace(url=url))))
    monkeypatch.setattr(schema, "_ensure_account_login_ready", AsyncMock(return_value=(True, None)))
    warmup = AsyncMock(side_effect=AssertionError("Observer must not fetch comments during warmup"))
    monkeypatch.setattr(engagement_engine, "warmup_page_for_http", warmup)
    scan = AsyncMock(return_value={"found_ids": [], "absent_ids": [], "complete": False, "reason": "empty_body"})
    monkeypatch.setattr(main_visibility, "scan_main_comment_ids", scan)
    monkeypatch.setattr(schema, "_safe_close", AsyncMock())
    observer_cooldowns = {}
    loop = asyncio.get_running_loop()
    def release(account_id):
        remaining = observer_cooldowns[account_id] - loop.time()
        assert 0 < remaining <= OBSERVER_COOLDOWN_SECONDS
    release_reservation = Mock(side_effect=release)
    monkeypatch.setattr(schema, "release_reply_account_reservation", release_reservation)
    projection = SimpleNamespace(account_id=None, identity_keys=[["username", "publisher"]],
        video_url=url, aweme_id=video, comment_id="7704740811904828192", kind="main", parent_id=None)

    observations = await observe_video(db_session, sample_task, [projection], observer_cooldowns=observer_cooldowns)
    assert {sample_account.id, 42} <= set(selector.call_args_list[0].kwargs["exclude_ids"])
    assert selector.call_count == scan.await_count == 2
    assert [item["reason"] for item in observations] == ["empty_body", "empty_body"]
    warmup.assert_not_awaited()

    # The first batch has released its leases and has yet to persist its observations.
    # Model a selector with a stale snapshot returning reader 71 again.
    selector.reset_mock(side_effect=True)
    selector.side_effect = [readers[0], readers[2]]
    scan.reset_mock()
    scan.return_value = {"found_ids": [projection.comment_id], "absent_ids": [], "complete": True}
    release_reservation.reset_mock()
    observations = await observe_video(db_session, sample_task, [projection], observer_cooldowns=observer_cooldowns)
    assert {71, 72} <= set(selector.call_args_list[0].kwargs["exclude_ids"])
    assert [item["observer_id"] for item in observations] == [73]
    assert [call.args[0] for call in release_reservation.call_args_list] == [71, 73]
    scan.assert_awaited_once()


@pytest.mark.asyncio
async def test_late_account_selection_cannot_publish_after_traffic_budget_is_spent(
        db_session, sample_task, sample_account, monkeypatch):
    from services import account_service
    from executor import visibility_publisher
    configure_task(db_session, sample_task, traffic_limit_mb=1)
    quantity.ensure_actions(db_session, sample_task, sample_task.video_urls[0], "main", 1, ["main"])
    identity, task_id, account_id = db_session.query(QuantityAction.id).scalar(), sample_task.id, sample_account.id
    def select(db, **kwargs):
        spend_budget(db, db.get(Task, task_id))
        return db.get(Account, account_id)
    publish, release = AsyncMock(side_effect=AssertionError("Budget exhausted during selection")), Mock()
    monkeypatch.setattr(account_service, "get_free_account_for_main_comment", select)
    monkeypatch.setattr(account_service, "release_main_account_reservation", release)
    monkeypatch.setattr(visibility_publisher, "publish_visibility_http", publish)
    result = await quantity.publish_action(db_session.get_bind(), task_id, identity, stop_requested=lambda: False)
    assert result == "quantity_traffic_budget_exhausted"
    publish.assert_not_awaited()
    release.assert_called_once_with(account_id)
    db_session.expire_all()
    action = db_session.get(QuantityAction, identity)
    assert action.state == "not_sent" and action.dispatches == 0


@pytest.mark.asyncio
async def test_concurrent_meter_closes_share_one_commit(db_session, sample_task, monkeypatch):
    from services import traffic_meter as traffic
    meter, entered, release = traffic.TrafficMeter(db_session.get_bind(), 42), threading.Event(), threading.Event()
    meter.buffer[traffic_key(sample_task)] = [100, 20, 1]
    original, calls = traffic.persist_sizes, []
    def persist(engine, batch):
        calls.append(1)
        entered.set()
        assert release.wait(3)
        original(engine, batch)
    monkeypatch.setattr(traffic, "persist_sizes", persist)
    owners = [asyncio.create_task(meter.close()), asyncio.create_task(meter.close())]
    try:
        assert await asyncio.wait_for(asyncio.to_thread(entered.wait, 1), 1.5)
        owners[0].cancel()
        await asyncio.sleep(.02)
        assert all(not owner.done() for owner in owners)
        release.set()
        results = await asyncio.wait_for(asyncio.gather(*owners, return_exceptions=True), 2)
        assert isinstance(results[0], asyncio.CancelledError) and results[1] is None
    finally:
        release.set()
        await asyncio.gather(*owners, return_exceptions=True)
    assert len(calls) == 1
    assert db_session.query(TaskTrafficBucket).one().requests == 1


@pytest.mark.asyncio
async def test_measured_but_unflushed_traffic_blocks_initial_navigation(db_session, sample_task, monkeypatch):
    from services.traffic_meter import TrafficMeter
    from executor import engagement_engine
    configure_task(db_session, sample_task, traffic_limit_mb=1)
    meter = TrafficMeter(db_session.get_bind(), 42)
    meter.buffer[traffic_key(sample_task)] = [1048576, 0, 1]
    resolver = AsyncMock(side_effect=AssertionError("Measured traffic already exhausted the budget"))
    monkeypatch.setattr(engagement_engine, "resolve_order_video_url", resolver)
    try:
        result = await quantity.execute_quantity_order(db_session, sample_task, sample_task.video_urls[0],
            ["main"], sample_task.id, stop_requested=lambda: False)
        assert result.error == "quantity_traffic_budget_exhausted"
        resolver.assert_not_awaited()
        assert not meter.buffer
    finally:
        await meter.close()


@pytest.mark.asyncio
async def test_limited_order_stops_when_measured_traffic_cannot_be_saved(db_session, sample_task, monkeypatch):
    from services import traffic_meter as traffic
    from executor import engagement_engine
    configure_task(db_session, sample_task, traffic_limit_mb=1)
    meter = traffic.TrafficMeter(db_session.get_bind(), 42)
    meter.buffer[traffic_key(sample_task)] = [100, 20, 1]
    resolver = AsyncMock(side_effect=AssertionError("Quota cannot be checked reliably"))
    monkeypatch.setattr(engagement_engine, "resolve_order_video_url", resolver)
    original = traffic.persist_sizes
    monkeypatch.setattr(traffic, "persist_sizes", Mock(side_effect=RuntimeError("database unavailable")))
    try:
        result = await quantity.execute_quantity_order(db_session, sample_task, sample_task.video_urls[0],
            ["main"], sample_task.id, stop_requested=lambda: False)
        assert result.error == "quantity_traffic_accounting_unavailable"
        assert meter.buffer
        resolver.assert_not_awaited()
    finally:
        monkeypatch.setattr(traffic, "persist_sizes", original)
        await meter.close()


@pytest.mark.asyncio
async def test_traffic_checkpoint_never_uses_a_meter_from_another_event_loop(db_session, sample_task):
    from services.traffic_meter import TrafficMeter, flush_task_traffic
    engine, key = db_session.get_bind(), traffic_key(sample_task)
    async def make_foreign():
        meter = TrafficMeter(engine, 42)
        meter.buffer[key] = [100, 20, 1]
        return meter
    foreign = await asyncio.to_thread(lambda: asyncio.run(make_foreign()))
    assert await flush_task_traffic(engine, sample_task.id)
    assert foreign.buffer[key] == [100, 20, 1]
    assert db_session.query(TaskTrafficBucket).count() == 0
