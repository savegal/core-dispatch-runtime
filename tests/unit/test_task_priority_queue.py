from types import SimpleNamespace


class FakeRedisQueue:
    def __init__(self):
        self.lists: dict[str, list[str]] = {}
        self.keys: dict[str, str] = {}

    def set(self, key, value, nx=False, ex=None):
        if nx and key in self.keys:
            return False
        self.keys[key] = value
        return True

    def delete(self, *keys):
        removed = 0
        for key in keys:
            if key in self.keys:
                removed += 1
                self.keys.pop(key, None)
        return removed

    def lpush(self, key, value):
        self.lists.setdefault(key, []).insert(0, str(value))
        return len(self.lists[key])

    def rpop(self, key):
        values = self.lists.setdefault(key, [])
        if not values:
            return None
        return values.pop()

    def brpop(self, key, timeout=0):
        keys = key if isinstance(key, list) else [key]
        for queue_name in keys:
            value = self.rpop(queue_name)
            if value is not None:
                return queue_name, value
        return None

    def lrange(self, key, start, end):
        values = list(self.lists.get(key, []))
        if end == -1:
            end = len(values) - 1
        return values[start : end + 1]

    def lrem(self, key, count, value):
        values = self.lists.setdefault(key, [])
        before = len(values)
        target = str(value)
        if count == 0:
            self.lists[key] = [item for item in values if item != target]
        else:
            removed = 0
            kept = []
            for item in values:
                if item == target and removed < abs(count):
                    removed += 1
                    continue
                kept.append(item)
            self.lists[key] = kept
        return before - len(self.lists[key])

    def llen(self, key):
        return len(self.lists.get(key, []))


class FailingLremRedisQueue(FakeRedisQueue):
    def lrem(self, key, count, value):
        raise RuntimeError("redis lrem failed")


def test_task_origin_priority_only_excludes_autopilot():
    from services import task_service

    assert not task_service._task_uses_priority_queue(
        SimpleNamespace(settings={"origin": "autopilot"})
    )
    assert task_service._task_queue_priority_level(
        SimpleNamespace(settings={"origin": "monitor_auto_comment"})
    ) == "low"
    assert task_service._task_uses_priority_queue(
        SimpleNamespace(settings={"origin": "manual"})
    )
    assert task_service._task_queue_priority_level(
        SimpleNamespace(settings={"origin": "autopilot", "queue_priority": "high"})
    ) == "high"
    assert task_service._task_uses_priority_queue(
        SimpleNamespace(settings={"origin": "quick_order"})
    )
    assert task_service._task_uses_priority_queue(SimpleNamespace(settings={}))


def test_priority_queue_runs_manual_fifo_before_autopilot(monkeypatch):
    from services import task_service

    fake_redis = FakeRedisQueue()
    monkeypatch.setattr(task_service, "get_redis", lambda: fake_redis)
    monkeypatch.setattr(task_service.settings, "REDIS_TASK_QUEUE", "tasks", raising=False)

    task_service.enqueue_task_once(101, priority=False)
    task_service.enqueue_task_once(102, priority=False)
    task_service.enqueue_task_once(201, priority=True)
    task_service.enqueue_task_once(202, priority=True)
    task_service.enqueue_task_once(301, priority_level="high")

    assert task_service.dequeue_task(timeout=0) == 301
    assert task_service.dequeue_task(timeout=0) == 201
    assert task_service.dequeue_task(timeout=0) == 202
    assert task_service.dequeue_task(timeout=0) == 101
    assert task_service.dequeue_task(timeout=0) == 102
    assert task_service.dequeue_task(timeout=0) is None


def test_remove_tasks_from_queue_cleans_normal_and_priority(monkeypatch):
    from services import task_service

    fake_redis = FakeRedisQueue()
    monkeypatch.setattr(task_service, "get_redis", lambda: fake_redis)
    monkeypatch.setattr(task_service.settings, "REDIS_TASK_QUEUE", "tasks", raising=False)

    task_service.enqueue_task_once(101, priority=False)
    task_service.enqueue_task_once(201, priority=True)
    task_service.enqueue_task_once(301, priority_level="high")

    removed = task_service.remove_tasks_from_queue([101, 201, 301])

    assert removed == 3
    assert task_service.get_queue_length() == 0


def test_requeue_deferred_preserves_manual_priority(monkeypatch):
    from services import task_service

    fake_redis = FakeRedisQueue()
    monkeypatch.setattr(task_service, "get_redis", lambda: fake_redis)
    monkeypatch.setattr(task_service.settings, "REDIS_TASK_QUEUE", "tasks", raising=False)

    task_service.requeue_deferred_task(301, priority=True)
    task_service.requeue_deferred_task(401, priority_level="high")
    task_service.enqueue_task_once(101, priority=False)

    assert task_service.dequeue_task(timeout=0) == 401
    assert task_service.dequeue_task(timeout=0) == 301
    assert task_service.dequeue_task(timeout=0) == 101


def test_promote_task_to_high_priority_moves_task_above_manual(monkeypatch):
    from services import task_service

    fake_redis = FakeRedisQueue()
    monkeypatch.setattr(task_service, "get_redis", lambda: fake_redis)
    monkeypatch.setattr(task_service.settings, "REDIS_TASK_QUEUE", "tasks", raising=False)

    task_service.enqueue_task_once(101, priority=False)
    task_service.enqueue_task_once(201, priority=True)

    result = task_service.promote_task_to_high_priority(101)

    assert result["enqueue_state"] == "enqueued"
    assert task_service.dequeue_task(timeout=0) == 101
    assert task_service.dequeue_task(timeout=0) == 201


def test_promote_task_to_high_priority_is_idempotent_for_high_queue(monkeypatch):
    from services import task_service

    fake_redis = FakeRedisQueue()
    monkeypatch.setattr(task_service, "get_redis", lambda: fake_redis)
    monkeypatch.setattr(task_service.settings, "REDIS_TASK_QUEUE", "tasks", raising=False)

    task_service.promote_task_to_high_priority(101)
    result = task_service.promote_task_to_high_priority(101)

    assert result["removed"] == 1
    assert task_service.dequeue_task(timeout=0) == 101
    assert task_service.dequeue_task(timeout=0) is None


def test_promote_task_to_high_priority_keeps_high_copy_when_old_cleanup_fails(monkeypatch):
    from services import task_service

    fake_redis = FailingLremRedisQueue()
    monkeypatch.setattr(task_service, "get_redis", lambda: fake_redis)
    monkeypatch.setattr(task_service.settings, "REDIS_TASK_QUEUE", "tasks", raising=False)

    result = task_service.promote_task_to_high_priority(101)

    assert result["enqueue_state"] == "enqueued"
    assert task_service.dequeue_task(timeout=0) == 101
