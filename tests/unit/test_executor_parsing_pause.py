import threading
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from types import SimpleNamespace

import pytest

from db.models import AppSettings, CreatorSource, CreatorVideo, MonitorOutbox, PendingEnqueue, Task
from executor import executor
from services.app_settings_service import clear_cache
from services import task_service
from services.task_service import recover_pending_timeout_tasks


class _FakeTaskLock:
    def __init__(self, calls: dict | None = None):
        self.calls = calls

    def release(self):
        if self.calls is not None:
            self.calls.setdefault("lock_released", 0)
            self.calls["lock_released"] += 1


class _FakeRedisQueue:
    def __init__(
        self,
        queued_ids: list[int] | None = None,
        marker_ids: set[int] | None = None,
        fail_lrange: bool = False,
    ):
        self.queued_ids = queued_ids or []
        self.marker_ids = marker_ids or set()
        self.fail_lrange = fail_lrange
        self.deleted_keys = []

    def lrange(self, *_args):
        if self.fail_lrange:
            raise RuntimeError("redis unavailable")
        return [str(task_id) for task_id in self.queued_ids]

    def mget(self, keys):
        values = []
        for key in keys:
            try:
                task_id = int(str(key).rsplit(":", 1)[-1])
            except ValueError:
                task_id = -1
            values.append("1" if task_id in self.marker_ids else None)
        return values

    def lrem(self, _queue_name, _count, value):
        before = len(self.queued_ids)
        value_int = int(str(value))
        self.queued_ids = [task_id for task_id in self.queued_ids if task_id != value_int]
        return before - len(self.queued_ids)

    def delete(self, key):
        self.deleted_keys.append(str(key))
        return 1


class _FakeTaskQuery:
    def __init__(self, task):
        self.task = task

    def filter(self, *_args, **_kwargs):
        return self

    def first(self):
        return self.task


class _FakeWorkerDb:
    def __init__(self, task):
        self.task = task

    def expire_all(self):
        pass

    def query(self, _model):
        return _FakeTaskQuery(self.task)

    def close(self):
        pass


def test_worker_requeues_non_admin_task_while_parsing(monkeypatch):
    calls = {"dequeue": 0, "requeue": []}
    fake_shutdown = threading.Event()
    fake_task = SimpleNamespace(id=123, status="pending", user=SimpleNamespace())
    fake_db = _FakeWorkerDb(fake_task)
    monkeypatch.setattr(executor, "_shutdown_event", fake_shutdown)
    monkeypatch.setattr(executor, "get_event_loop", lambda: SimpleNamespace(is_closed=lambda: True))
    monkeypatch.setattr(executor, "get_sync_db", lambda: iter([fake_db]))
    monkeypatch.setattr(executor, "_acquire_global_task_execution_lock", lambda _worker_id: _FakeTaskLock(calls))
    monkeypatch.setattr(executor, "_should_defer_loaded_task_for_parsing", lambda db, task: True)
    monkeypatch.setattr(executor.settings, "WORKER_POLL_COUNT", 1)
    monkeypatch.setattr(executor.settings, "WORKER_POLL_INTERVAL", 0)

    def fake_sleep(_seconds):
        fake_shutdown.set()

    def fake_dequeue_task():
        calls["dequeue"] += 1
        return 123

    monkeypatch.setattr(executor.time, "sleep", fake_sleep)
    monkeypatch.setattr(executor, "dequeue_task", fake_dequeue_task)
    monkeypatch.setattr(
        executor,
        "requeue_deferred_task",
        lambda task_id, **kwargs: calls["requeue"].append((task_id, kwargs.get("priority_level"))),
    )

    executor.worker(2)

    assert calls["dequeue"] == 1
    assert calls["requeue"] == [(123, "normal")]
    assert calls["lock_released"] == 1


def test_worker_processes_admin_task_while_parsing(monkeypatch):
    calls = {"processed": []}
    fake_shutdown = threading.Event()
    fake_task = SimpleNamespace(id=456, status="pending", user=SimpleNamespace())
    fake_db = _FakeWorkerDb(fake_task)
    monkeypatch.setattr(executor, "_shutdown_event", fake_shutdown)
    monkeypatch.setattr(executor, "get_event_loop", lambda: SimpleNamespace(is_closed=lambda: True))
    monkeypatch.setattr(executor, "get_sync_db", lambda: iter([fake_db]))
    monkeypatch.setattr(executor, "_acquire_global_task_execution_lock", lambda _worker_id: _FakeTaskLock(calls))
    monkeypatch.setattr(executor, "_should_defer_loaded_task_for_parsing", lambda db, task: False)

    def fake_process_task(task_id):
        calls["processed"].append(task_id)
        fake_shutdown.set()

    monkeypatch.setattr(executor, "dequeue_task", lambda: 456)
    monkeypatch.setattr(executor, "process_task", fake_process_task)

    executor.worker(2)

    assert calls["processed"] == [456]
    assert calls["lock_released"] == 1


def test_worker_waits_when_global_task_lock_is_busy(monkeypatch):
    calls = {"dequeue": 0}
    fake_shutdown = threading.Event()
    monkeypatch.setattr(executor, "_shutdown_event", fake_shutdown)
    monkeypatch.setattr(executor, "get_event_loop", lambda: SimpleNamespace(is_closed=lambda: True))
    monkeypatch.setattr(executor, "_acquire_global_task_execution_lock", lambda _worker_id: None)
    monkeypatch.setattr(executor.settings, "WORKER_POLL_COUNT", 1)
    monkeypatch.setattr(executor.settings, "WORKER_POLL_INTERVAL", 0)

    def fake_sleep(_seconds):
        fake_shutdown.set()

    def fake_dequeue_task():
        calls["dequeue"] += 1
        return 789

    monkeypatch.setattr(executor.time, "sleep", fake_sleep)
    monkeypatch.setattr(executor, "dequeue_task", fake_dequeue_task)

    executor.worker(2)

    assert calls["dequeue"] == 0


def test_worker_releases_global_task_lock_when_queue_is_empty(monkeypatch):
    calls = {"dequeue": 0}
    fake_shutdown = threading.Event()
    monkeypatch.setattr(executor, "_shutdown_event", fake_shutdown)
    monkeypatch.setattr(executor, "get_event_loop", lambda: SimpleNamespace(is_closed=lambda: True))
    monkeypatch.setattr(executor, "_acquire_global_task_execution_lock", lambda _worker_id: _FakeTaskLock(calls))
    monkeypatch.setattr(executor.settings, "WORKER_POLL_COUNT", 1)
    monkeypatch.setattr(executor.settings, "WORKER_POLL_INTERVAL", 0)

    def fake_sleep(_seconds):
        fake_shutdown.set()

    def fake_dequeue_task():
        calls["dequeue"] += 1
        return None

    monkeypatch.setattr(executor.time, "sleep", fake_sleep)
    monkeypatch.setattr(executor, "dequeue_task", fake_dequeue_task)

    executor.worker(2)

    assert calls["dequeue"] == 1
    assert calls["lock_released"] == 1


def test_global_task_lock_timeout_covers_processing_and_recovery(monkeypatch):
    monkeypatch.setattr(executor.settings, "TASK_PROCESSING_TIMEOUT", 120, raising=False)
    monkeypatch.setattr(executor.settings, "STUCK_TASKS_RECOVERY_INTERVAL", 30, raising=False)

    assert executor._global_task_lock_timeout_seconds() == 150


def test_acquire_global_task_execution_lock_uses_nonblocking_redis_lock(monkeypatch):
    from services import redis_service

    captured = {}

    class FakeRedisLock:
        def __init__(self, key, timeout, blocking):
            captured["key"] = key
            captured["timeout"] = timeout
            captured["blocking"] = blocking

        def acquire(self):
            return True

    monkeypatch.setattr(executor, "_global_task_lock_timeout_seconds", lambda: 456)
    monkeypatch.setattr(redis_service, "RedisLock", FakeRedisLock)

    lock = executor._acquire_global_task_execution_lock(worker_id=2)

    assert lock is not None
    assert captured == {
        "key": executor._GLOBAL_TASK_EXECUTION_LOCK_KEY,
        "timeout": 456,
        "blocking": False,
    }


def test_acquire_global_task_execution_lock_waits_when_redis_lock_is_busy(monkeypatch):
    from services import redis_service

    class FakeRedisLock:
        def __init__(self, *_args, **_kwargs):
            pass

        def acquire(self):
            return False

    monkeypatch.setattr(redis_service, "RedisLock", FakeRedisLock)
    monkeypatch.setattr(
        redis_service,
        "get_executor_heartbeat",
        lambda: (True, {"status": "busy", "pid": __import__("os").getpid()}),
    )
    monkeypatch.setattr(executor, "_redis_available_for_task_lock", lambda: True)

    assert executor._acquire_global_task_execution_lock(worker_id=2) is None


def test_acquire_global_task_execution_lock_clears_stale_redis_lock(monkeypatch):
    from services import redis_service

    calls = {"acquire": 0, "cleared": 0}

    class FakeRedisLock:
        def __init__(self, *_args, **_kwargs):
            pass

        def acquire(self):
            calls["acquire"] += 1
            return calls["acquire"] >= 2

    monkeypatch.setattr(redis_service, "RedisLock", FakeRedisLock)
    monkeypatch.setattr(redis_service, "get_executor_heartbeat", lambda: (False, {"reason": "missing"}))
    monkeypatch.setattr(redis_service, "get_redis_client", lambda: SimpleNamespace())

    def fake_clear_stale(**_kwargs):
        calls["cleared"] += 1
        return True

    monkeypatch.setattr(executor, "_clear_stale_global_task_lock_on_startup", fake_clear_stale)
    monkeypatch.setattr(executor, "_redis_available_for_task_lock", lambda: True)

    lock = executor._acquire_global_task_execution_lock(worker_id=2)

    assert lock is not None
    assert calls == {"acquire": 2, "cleared": 1}


def test_acquire_global_task_execution_lock_falls_back_to_local_when_redis_unavailable(monkeypatch):
    from services import redis_service

    class FakeRedisLock:
        def __init__(self, *_args, **_kwargs):
            pass

        def acquire(self):
            return False

    monkeypatch.setattr(redis_service, "RedisLock", FakeRedisLock)
    monkeypatch.setattr(
        redis_service,
        "get_executor_heartbeat",
        lambda: (True, {"status": "busy", "pid": __import__("os").getpid()}),
    )
    monkeypatch.setattr(executor, "_redis_available_for_task_lock", lambda: False)

    lock = executor._acquire_global_task_execution_lock(worker_id=2)
    try:
        assert lock is not None
        assert executor._acquire_global_task_execution_lock(worker_id=3) is None
    finally:
        executor._release_global_task_execution_lock(lock, worker_id=2)


def test_acquire_global_task_execution_lock_falls_back_to_local_on_lock_exception(monkeypatch):
    from services import redis_service

    class BrokenRedisLock:
        def __init__(self, *_args, **_kwargs):
            raise RuntimeError("redis down")

    monkeypatch.setattr(redis_service, "RedisLock", BrokenRedisLock)

    lock = executor._acquire_global_task_execution_lock(worker_id=2)
    try:
        assert lock is not None
    finally:
        executor._release_global_task_execution_lock(lock, worker_id=2)


@pytest.mark.unit
def test_executor_does_not_defer_full_access_worker_in_parsing_mode(db_session, sample_user):
    clear_cache()
    sample_user.full_access_override = True
    db_session.add(AppSettings(key="software_mode", value="parsing"))
    task = Task(
        user_id=sample_user.id,
        status="pending",
        main_comment="main",
        video_urls=["https://www.tiktok.com/@demo/video/1234567890123456789"],
        progress=0,
        attempts=0,
        total_cost=Decimal("10.00"),
    )
    db_session.add(task)
    db_session.commit()

    try:
        assert executor._should_defer_task_for_parsing(db_session, task.id) is False
    finally:
        clear_cache()


@pytest.mark.unit
def test_executor_defers_restricted_worker_in_parsing_mode(db_session, sample_user):
    clear_cache()
    db_session.add(AppSettings(key="software_mode", value="parsing"))
    task = Task(
        user_id=sample_user.id,
        status="pending",
        main_comment="main",
        video_urls=["https://www.tiktok.com/@demo/video/1234567890123456789"],
        progress=0,
        attempts=0,
        total_cost=Decimal("10.00"),
    )
    db_session.add(task)
    db_session.commit()

    try:
        assert executor._should_defer_task_for_parsing(db_session, task.id) is True
    finally:
        clear_cache()


@pytest.mark.unit
def test_pending_timeout_recovery_skips_tasks_in_parsing_mode(db_session, sample_user):
    clear_cache()
    db_session.add(AppSettings(key="software_mode", value="parsing"))
    task = Task(
        user_id=sample_user.id,
        status="pending",
        main_comment="main",
        video_urls=["https://www.tiktok.com/@demo/video/1234567890123456789"],
        progress=0,
        attempts=0,
        total_cost=Decimal("10.00"),
        created_at=datetime.now(timezone.utc) - timedelta(hours=2),
    )
    db_session.add(task)
    db_session.commit()

    try:
        processed = recover_pending_timeout_tasks(db_session, timeout_minutes=20)
    finally:
        clear_cache()

    db_session.refresh(task)
    assert processed == 0
    assert task.status == "pending"
    assert task.error_reason is None


@pytest.mark.unit
def test_pending_timeout_recovery_still_handles_admin_tasks_in_parsing_mode(monkeypatch, db_session, sample_admin):
    clear_cache()
    monkeypatch.setattr(task_service, "get_redis", lambda: _FakeRedisQueue())
    db_session.add(AppSettings(key="software_mode", value="parsing"))
    task = Task(
        user_id=sample_admin.id,
        status="pending",
        main_comment="main",
        video_urls=["https://www.tiktok.com/@demo/video/1234567890123456789"],
        progress=0,
        attempts=0,
        total_cost=Decimal("0.00"),
        created_at=datetime.now(timezone.utc) - timedelta(hours=2),
    )
    db_session.add(task)
    db_session.commit()

    try:
        processed = recover_pending_timeout_tasks(db_session, timeout_minutes=20)
    finally:
        clear_cache()

    db_session.refresh(task)
    assert processed == 1
    assert task.status == "error"


@pytest.mark.unit
def test_pending_timeout_recovery_resets_queued_monitor_video(monkeypatch, db_session, sample_admin):
    monkeypatch.setattr(task_service, "get_redis", lambda: _FakeRedisQueue())
    creator = CreatorSource(
        user_id=sample_admin.id,
        username="queued_demo",
        mode="auto_comment",
        enabled=True,
    )
    task = Task(
        user_id=sample_admin.id,
        status="pending",
        main_comment="main",
        video_urls=["https://www.tiktok.com/@demo/video/1234567890123456789"],
        progress=0,
        attempts=0,
        total_cost=Decimal("0.00"),
        created_at=datetime.now(timezone.utc) - timedelta(hours=2),
        settings={"origin": "autopilot"},
    )
    db_session.add_all([creator, task])
    db_session.flush()
    video = CreatorVideo(
        creator_id=creator.id,
        video_id="1234567890123456789",
        url="https://www.tiktok.com/@demo/video/1234567890123456789",
        status="queued",
        task_id=task.id,
    )
    db_session.add(video)
    db_session.commit()

    processed = recover_pending_timeout_tasks(db_session, timeout_minutes=20)

    db_session.refresh(task)
    db_session.refresh(video)
    outbox = db_session.query(MonitorOutbox).filter(MonitorOutbox.event_type == "autopilot_create").one()
    assert processed == 1
    assert task.status == "error"
    assert video.status == "new"
    assert video.task_id is None
    assert outbox.status == "pending"
    assert outbox.payload == {"creator_id": creator.id, "video_ids": [video.id]}


@pytest.mark.unit
def test_pending_timeout_recovery_skips_tasks_still_in_redis_queue(monkeypatch, db_session, sample_admin):
    monkeypatch.setattr(task_service, "get_redis", lambda: _FakeRedisQueue(queued_ids=[901]))
    task = Task(
        id=901,
        user_id=sample_admin.id,
        status="pending",
        main_comment="main",
        video_urls=["https://www.tiktok.com/@demo/video/1234567890123456789"],
        progress=0,
        attempts=0,
        total_cost=Decimal("0.00"),
        created_at=datetime.now(timezone.utc) - timedelta(hours=2),
    )
    db_session.add(task)
    db_session.commit()

    processed = recover_pending_timeout_tasks(db_session, timeout_minutes=20)

    db_session.refresh(task)
    assert processed == 0
    assert task.status == "pending"
    assert task.error_reason is None


@pytest.mark.unit
def test_pending_timeout_recovery_skips_tasks_with_queue_marker(monkeypatch, db_session, sample_admin):
    monkeypatch.setattr(task_service, "get_redis", lambda: _FakeRedisQueue(marker_ids={902}))
    task = Task(
        id=902,
        user_id=sample_admin.id,
        status="pending",
        main_comment="main",
        video_urls=["https://www.tiktok.com/@demo/video/1234567890123456789"],
        progress=0,
        attempts=0,
        total_cost=Decimal("0.00"),
        created_at=datetime.now(timezone.utc) - timedelta(hours=2),
    )
    db_session.add(task)
    db_session.commit()

    processed = recover_pending_timeout_tasks(db_session, timeout_minutes=20)

    db_session.refresh(task)
    assert processed == 0
    assert task.status == "pending"
    assert task.error_reason is None


@pytest.mark.unit
def test_pending_timeout_recovery_skips_pending_enqueue_reserve(monkeypatch, db_session, sample_admin):
    monkeypatch.setattr(task_service, "get_redis", lambda: _FakeRedisQueue())
    task = Task(
        id=903,
        user_id=sample_admin.id,
        status="pending",
        main_comment="main",
        video_urls=["https://www.tiktok.com/@demo/video/1234567890123456789"],
        progress=0,
        attempts=0,
        total_cost=Decimal("0.00"),
        created_at=datetime.now(timezone.utc) - timedelta(hours=2),
    )
    db_session.add(task)
    db_session.add(PendingEnqueue(task_id=903))
    db_session.commit()

    processed = recover_pending_timeout_tasks(db_session, timeout_minutes=20)

    db_session.refresh(task)
    assert processed == 0
    assert task.status == "pending"
    assert task.error_reason is None


@pytest.mark.unit
def test_pending_timeout_recovery_skips_all_when_redis_queue_state_unknown(monkeypatch, db_session, sample_admin):
    monkeypatch.setattr(task_service, "get_redis", lambda: _FakeRedisQueue(fail_lrange=True))
    task = Task(
        id=904,
        user_id=sample_admin.id,
        status="pending",
        main_comment="main",
        video_urls=["https://www.tiktok.com/@demo/video/1234567890123456789"],
        progress=0,
        attempts=0,
        total_cost=Decimal("0.00"),
        created_at=datetime.now(timezone.utc) - timedelta(hours=2),
    )
    db_session.add(task)
    db_session.commit()

    processed = recover_pending_timeout_tasks(db_session, timeout_minutes=20)

    db_session.refresh(task)
    assert processed == 0
    assert task.status == "pending"
    assert task.error_reason is None
