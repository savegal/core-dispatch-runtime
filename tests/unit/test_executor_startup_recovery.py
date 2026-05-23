import pytest
import os
from types import SimpleNamespace

from db.models import Task, User


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
