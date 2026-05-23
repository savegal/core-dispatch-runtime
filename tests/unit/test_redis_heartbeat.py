import json

import pytest


class _FakeRedis:
    def __init__(self):
        self.values = {}
        self.expirations = {}

    def set(self, key, value, ex=None, **_kwargs):
        self.values[key] = value
        self.expirations[key] = ex
        return True

    def get(self, key):
        return self.values.get(key)

    def ttl(self, key):
        return self.expirations.get(key, -2) if key in self.values else -2


@pytest.mark.unit
def test_executor_heartbeat_is_json_serialized(monkeypatch):
    from services import redis_service

    fake_redis = _FakeRedis()
    monkeypatch.setattr(redis_service, "get_redis_client", lambda: fake_redis)

    assert redis_service.update_executor_heartbeat("ok") is True
    alive, payload = redis_service.get_executor_heartbeat()

    assert alive is True
    assert payload["status"] == "ok"
    assert isinstance(payload["pid"], int)
    assert payload["host"]
    assert isinstance(json.loads(fake_redis.get("executor:heartbeat")), dict)


@pytest.mark.unit
def test_monitor_heartbeat_is_json_serialized_with_extra(monkeypatch):
    from services import redis_service

    fake_redis = _FakeRedis()
    monkeypatch.setattr(redis_service, "get_redis_client", lambda: fake_redis)

    assert redis_service.update_monitor_heartbeat("ok", queue_len=12, ignored=None) is True
    alive, payload = redis_service.get_monitor_heartbeat()

    assert alive is True
    assert payload["status"] == "ok"
    assert payload["queue_len"] == 12
    assert "ignored" not in payload


@pytest.mark.unit
def test_executor_task_activity_requires_lock_and_live_heartbeat(monkeypatch):
    from services import redis_service

    fake_redis = _FakeRedis()
    monkeypatch.setattr(redis_service, "get_redis_client", lambda: fake_redis)

    assert redis_service.update_executor_heartbeat("ok") is True
    active, payload = redis_service.executor_task_activity_snapshot()
    assert active is False
    assert payload["lock_present"] is False
    assert payload["heartbeat_alive"] is True

    fake_redis.set("lock:executor:single_active_task", "token", ex=3900)
    active, payload = redis_service.executor_task_activity_snapshot()
    assert active is False
    assert payload["lock_present"] is True
    assert payload["lock_ttl"] == 3900
    assert payload["heartbeat_status"] == "ok"

    assert redis_service.update_executor_heartbeat("busy", active_task_id=123, worker_id=1) is True
    active, payload = redis_service.executor_task_activity_snapshot()
    assert active is True
    assert payload["lock_present"] is True
    assert payload["lock_ttl"] == 3900
    assert payload["heartbeat_status"] == "busy"
    assert payload["heartbeat"]["active_task_id"] == 123

    assert redis_service.update_executor_heartbeat("stopped") is True
    active, payload = redis_service.executor_task_activity_snapshot()
    assert active is False
    assert payload["heartbeat_status"] == "stopped"
