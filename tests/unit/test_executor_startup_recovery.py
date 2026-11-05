import asyncio
import os
import threading
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest
from sqlalchemy.orm import Session

from db.models import PendingEnqueue, Task, User


class _FakeRedis:
    def __init__(self, values=None, ttl=123):
        self.values = dict(values or {})
        self._ttl = ttl
        self.deleted = []

    def get(self, key):
        return self.values.get(key)

    def ttl(self, key):
        return self._ttl if key in self.values else -2

    def delete(self, key):
        self.deleted.append(key)
        return 1 if self.values.pop(key, None) is not None else 0


@pytest.mark.unit
def test_executor_startup_clears_stale_global_task_lock_when_heartbeat_missing():
    from executor import executor

    lock_key = executor._global_task_lock_redis_key()
    fake_redis = _FakeRedis({lock_key: "old-token"}, ttl=777)

    cleared = executor._clear_stale_global_task_lock_on_startup(
        redis_client=fake_redis,
        heartbeat_reader=lambda: (False, {"reason": "missing"}),
    )

    assert cleared is True
    assert fake_redis.get(lock_key) is None
    assert fake_redis.deleted == [lock_key]


@pytest.mark.unit
def test_executor_startup_keeps_global_task_lock_when_heartbeat_alive():
    from executor import executor

    lock_key = executor._global_task_lock_redis_key()
    fake_redis = _FakeRedis({lock_key: "live-token"}, ttl=777)

    cleared = executor._clear_stale_global_task_lock_on_startup(
        redis_client=fake_redis,
        heartbeat_reader=lambda: (True, {"status": "ok", "pid": os.getpid()}),
    )

    assert cleared is False
    assert fake_redis.get(lock_key) == "live-token"
    assert fake_redis.deleted == []


@pytest.mark.unit
def test_executor_activity_payload_tracks_busy_task():
    from executor import executor

    executor._set_executor_activity("busy", active_task_id=123, worker_id=1, busy_since=456)
    assert executor._get_executor_activity_payload() == {
        "status": "busy",
        "active_task_id": 123,
        "worker_id": 1,
        "busy_since": 456,
    }

    executor._set_executor_activity("ok")
    assert executor._get_executor_activity_payload() == {"status": "ok"}


@pytest.mark.unit
def test_executor_startup_clears_lock_when_heartbeat_has_dead_pid():
    from executor import executor

    lock_key = executor._global_task_lock_redis_key()
    fake_redis = _FakeRedis({lock_key: "dead-pid-token"}, ttl=777)

    cleared = executor._clear_stale_global_task_lock_on_startup(
        redis_client=fake_redis,
        heartbeat_reader=lambda: (True, {"status": "ok", "pid": 99999999}),
    )

    assert cleared is True
    assert fake_redis.get(lock_key) is None
    assert fake_redis.deleted == [lock_key]


@pytest.mark.unit
def test_executor_startup_clears_lock_when_heartbeat_has_old_format():
    from executor import executor

    lock_key = executor._global_task_lock_redis_key()
    fake_redis = _FakeRedis({lock_key: "old-format-token"}, ttl=777)

    cleared = executor._clear_stale_global_task_lock_on_startup(
        redis_client=fake_redis,
        heartbeat_reader=lambda: (True, {"status": "ok"}),
    )

    assert cleared is True
    assert fake_redis.get(lock_key) is None
    assert fake_redis.deleted == [lock_key]


@pytest.mark.unit
def test_executor_startup_requeues_pending_tasks_missing_from_redis(monkeypatch):
    from executor import executor
    from services import redis_service, task_service

    fake_db = SimpleNamespace(close=lambda: None)
    calls = []

    monkeypatch.setattr(executor, "get_sync_db", lambda: iter([fake_db]))
    monkeypatch.setattr(redis_service, "get_executor_heartbeat", lambda: (False, {"reason": "missing"}))
    monkeypatch.setattr(task_service, "recover_stuck_tasks", lambda db: calls.append(("stuck", db)) or 0)
    monkeypatch.setattr(task_service, "prune_stale_task_queue_entries", lambda db: calls.append(("prune", db)) or 0)
    monkeypatch.setattr(task_service, "recover_pending_enqueues", lambda db: calls.append(("reserved", db)) or 0)
    monkeypatch.setattr(task_service, "recover_tasks_lost_in_redis", lambda db: calls.append(("lost", db)) or 6)

    executor._run_executor_startup_queue_cleanup()

    assert calls == [
        ("stuck", fake_db),
        ("prune", fake_db),
        ("reserved", fake_db),
        ("lost", fake_db),
    ]


@pytest.mark.unit
def test_mark_task_settings_dirty_persists_in_place_json_update(db_session):
    from executor import executor

    user = User(telegram_id=981001, username="json_dirty", role="user", balance=100, is_blocked=False)
    db_session.add(user)
    db_session.flush()
    task = Task(
        user_id=user.id,
        status="pending",
        main_comment="main",
        video_urls=["https://www.tiktok.com/@demo/video/1"],
        settings={"origin": "autopilot", "url_results": {}},
    )
    db_session.add(task)
    db_session.commit()

    task.settings["url_results"]["https://www.tiktok.com/@demo/video/1"] = {"status": "success"}
    executor._mark_task_settings_dirty(task)
    db_session.commit()
    db_session.expire_all()

    saved = db_session.query(Task).filter(Task.id == task.id).one()
    assert saved.settings["url_results"]["https://www.tiktok.com/@demo/video/1"]["status"] == "success"


@pytest.mark.unit
def test_stale_reply_likes_are_requeued_and_resumed_without_republishing(db_session, monkeypatch):
    from executor import engagement_engine, executor, reply_visibility, user_schema
    from services import task_service, task_stop_signal

    user = User(telegram_id=981002, role="user", balance=100, is_blocked=False)
    db_session.add(user)
    db_session.flush()
    media_id = "1234567890123456789"
    url = f"https://www.tiktok.com/@demo/video/{media_id}"
    parent, cid = "7641761192486194454", "7641761192486194455"
    plan = {
        "state": "pending", "requested_per_reply": 2, "targets": [{"cid": cid, "parent_id": parent}],
        "attempts": {cid: {"8": "confirmed"}}, "exclude_account_ids": [1], "by_comment_id": {cid: 1},
    }
    task = Task(user_id=user.id, kind="full", status="in_progress", main_comment="main", video_urls=[url],
                updated_at=datetime.now(timezone.utc) - timedelta(hours=1), total_cost=10,
                settings={"account_pool_mode": "personal", "url_results": {url: {
                    "status": "success", "publication_complete": True, "reply_likes": plan,
                }}})
    db_session.add(task)
    db_session.commit()
    task_id = task.id

    enqueue = Mock(side_effect=ConnectionError("Redis unavailable"))
    monkeypatch.setattr(task_service, "enqueue_task_once", enqueue)
    assert task_service.recover_stuck_tasks(db_session, timeout_seconds=60) == 1
    db_session.refresh(task)
    assert task.status == "pending" and not task.refunded_cost
    assert db_session.query(PendingEnqueue).filter_by(task_id=task_id).one()
    enqueue.side_effect = None
    assert task_service.recover_pending_enqueues(db_session) == 1

    publish = AsyncMock(side_effect=AssertionError("publication was already completed"))
    verify = AsyncMock(side_effect=AssertionError("visibility was already checked"))
    boost = AsyncMock(return_value={cid: 2})
    monkeypatch.setattr(engagement_engine, "execute_hybrid_full", publish)
    monkeypatch.setattr(user_schema, "execute_user_schema_full", publish)
    monkeypatch.setattr(reply_visibility, "verify_order_reply_visibility", verify)
    monkeypatch.setattr(user_schema, "boost_target_replies_via_http_pool", boost)
    monkeypatch.setattr(task_stop_signal, "is_task_stop_requested", lambda _: False)
    monkeypatch.setattr(executor, "get_sync_db", lambda: iter([Session(db_session.get_bind())]))
    monkeypatch.setattr(executor, "_shutdown_event", threading.Event())
    monkeypatch.setattr(executor.settings, "TIKTOK_NETWORK_FIRST", True)
    monkeypatch.setattr(executor.settings, "TIKTOK_NETWORK_IMPL_READY", True)
    monkeypatch.setenv("AUTO_BOOST_LIKES", "1")
    loop = asyncio.new_event_loop()
    monkeypatch.setattr(executor, "get_event_loop", lambda: loop)
    try:
        executor.process_task(task_id)
    finally:
        loop.close()

    db_session.refresh(task)
    assert task.status == "done"
    assert task.settings["url_results"][url]["reply_likes"]["state"] == "completed"
    assert task.settings["url_results"][url]["reply_likes"]["by_comment_id"] == {cid: 2}
    assert boost.await_args.kwargs["attempts"] == {cid: {"8": "confirmed"}}
    publish.assert_not_awaited()
    verify.assert_not_awaited()
