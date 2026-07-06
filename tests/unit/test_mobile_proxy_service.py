from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

import services.mobile_proxy_service as mobile_proxy_service


class FakeRedis:
    def __init__(self):
        self.store = {}
        self.expires = {}

    def set(self, key, value, nx=False, ex=None):
        if nx and key in self.store:
            return False
        self.store[key] = value
        self.expires[key] = ex
        return True

    def get(self, key):
        return self.store.get(key)

    def delete(self, *keys):
        deleted = 0
        for key in keys:
            if key in self.store:
                deleted += 1
                self.store.pop(key, None)
                self.expires.pop(key, None)
        return deleted

    def eval(self, _script, _numkeys, key, token):
        if self.store.get(key) == token:
            return self.delete(key)
        return 0


@pytest.mark.unit
@pytest.mark.asyncio
async def test_mobile_proxy_lock_uses_monitor_timeout_ttl(monkeypatch):
    fake_redis = FakeRedis()
    mobile_proxy_service._held_proxy_lock_tokens.clear()
    monkeypatch.setattr(mobile_proxy_service, "get_redis", lambda: fake_redis)
    monkeypatch.setattr(mobile_proxy_service.settings, "MOBILE_PROXY_LOCK_TTL", 120, raising=False)
    monkeypatch.setattr(mobile_proxy_service.settings, "BLOGGER_CHECK_TIMEOUT_SECONDS", 180, raising=False)
    monkeypatch.setattr(mobile_proxy_service.settings, "MOBILE_PROXY_LOCK_EXTRA_SECONDS", 90, raising=False)

    assert await mobile_proxy_service.acquire_proxy_lock(7, timeout=0.1) is True

    assert fake_redis.expires["proxy_lock:7"] == 270
    await mobile_proxy_service.release_proxy_lock(7)
    assert "proxy_lock:7" not in fake_redis.store


@pytest.mark.unit
@pytest.mark.asyncio
async def test_mobile_proxy_lock_release_does_not_delete_new_owner(monkeypatch):
    fake_redis = FakeRedis()
    mobile_proxy_service._held_proxy_lock_tokens.clear()
    monkeypatch.setattr(mobile_proxy_service, "get_redis", lambda: fake_redis)

    assert await mobile_proxy_service.acquire_proxy_lock(8, timeout=0.1) is True
    fake_redis.store["proxy_lock:8"] = "new-owner-token"

    await mobile_proxy_service.release_proxy_lock(8)

    assert fake_redis.store["proxy_lock:8"] == "new-owner-token"


@pytest.mark.unit
def test_clear_proxy_locks_removes_startup_stale_locks(monkeypatch):
    fake_redis = FakeRedis()
    mobile_proxy_service._held_proxy_lock_tokens[6] = "old-token"
    fake_redis.store["proxy_lock:6"] = "old-token"
    fake_redis.store["proxy_lock:7"] = "other-token"
    monkeypatch.setattr(mobile_proxy_service, "get_redis", lambda: fake_redis)

    deleted = mobile_proxy_service.clear_proxy_locks([6, 7, "bad-id"])

    assert deleted == 2
    assert "proxy_lock:6" not in fake_redis.store
    assert "proxy_lock:7" not in fake_redis.store
    assert 6 not in mobile_proxy_service._held_proxy_lock_tokens


@pytest.mark.unit
@pytest.mark.asyncio
async def test_recent_mobile_rotation_reuses_previous_ip_when_probe_fails(monkeypatch):
    proxy = SimpleNamespace(id=6, is_mobile="Y", proxy="http://proxy.example:8080")

    async def fake_get_current_ip(_proxy):
        return None

    async def fake_rotation_age(_proxy_id):
        return 20

    async def fail_rotate(_proxy):
        raise AssertionError("rotation should be skipped inside cooldown")

    monkeypatch.setattr(mobile_proxy_service, "get_current_ip", fake_get_current_ip)
    monkeypatch.setattr(mobile_proxy_service, "_get_proxy_last_rotation_age_seconds", fake_rotation_age)
    monkeypatch.setattr(mobile_proxy_service, "_adaptive_rotation_cooldown_seconds", lambda: 90)
    monkeypatch.setattr(mobile_proxy_service, "rotate_proxy_ip", fail_rotate)

    result = await mobile_proxy_service.ensure_proxy_ip_rotated(
        proxy,
        db=None,
        previous_ip="37.47.100.10",
    )

    assert result == "37.47.100.10"


@pytest.mark.unit
@pytest.mark.asyncio
async def test_forced_mobile_rotation_bypasses_recent_rotation_cooldown(monkeypatch):
    proxy = SimpleNamespace(
        id=9,
        is_mobile="Y",
        proxy="http://proxy.example:8080",
        rotation_url="https://rotate.example",
    )
    rotate_calls = []

    async def fake_get_current_ip(_proxy):
        return "37.47.100.10"

    async def fake_rotation_age(_proxy_id):
        return 20

    async def fake_rotate(_proxy):
        rotate_calls.append(_proxy.id)
        return True, "37.47.100.11"

    monkeypatch.setattr(mobile_proxy_service, "get_current_ip", fake_get_current_ip)
    monkeypatch.setattr(mobile_proxy_service, "_get_proxy_last_rotation_age_seconds", fake_rotation_age)
    monkeypatch.setattr(mobile_proxy_service, "_adaptive_rotation_cooldown_seconds", lambda: 90)
    monkeypatch.setattr(mobile_proxy_service, "rotate_proxy_ip", fake_rotate)
    monkeypatch.setattr(
        mobile_proxy_service,
        "_mark_proxy_rotated_now",
        AsyncMock(return_value=None),
    )

    result = await mobile_proxy_service.ensure_proxy_ip_rotated(
        proxy,
        db=None,
        previous_ip="37.47.100.10",
        force_rotation=True,
    )

    assert result == "37.47.100.11"
    assert rotate_calls == [9]
