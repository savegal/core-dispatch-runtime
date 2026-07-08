from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest


class _LegacyPipeline:
    def __init__(self, redis):
        self.redis = redis
        self.commands = []

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return False

    def hset(self, name, key, value):
        self.commands.append(("hset", name, key, value))
        return self

    def expire(self, name, ttl):
        self.commands.append(("expire", name, ttl))
        return self

    def execute(self):
        for command in self.commands:
            if command[0] == "hset":
                _, name, key, value = command
                self.redis.hashes.setdefault(name, {})[key] = value
            elif command[0] == "expire":
                _, name, ttl = command
                self.redis.ttls[name] = ttl
        self.redis.executed_batches.append(list(self.commands))
        return [1] * len(self.commands)


class _LegacyRedis:
    """Redis 3-style fake: HSET accepts exactly one field/value pair."""

    def __init__(self):
        self.hashes = {}
        self.ttls = {}
        self.strings = {}
        self.executed_batches = []

    def pipeline(self, *, transaction):
        assert transaction is True
        return _LegacyPipeline(self)

    def hgetall(self, name):
        return dict(self.hashes.get(name, {}))

    def set(self, name, value, **kwargs):
        self.strings[name] = value
        return True


@pytest.mark.unit
def test_draft_storage_uses_redis3_compatible_hset_and_preserves_full_draft(monkeypatch):
    from bot.services import drafts

    redis = _LegacyRedis()
    monkeypatch.setattr(drafts, "redis_client", redis)
    monkeypatch.setattr(drafts, "redis_available", True)
    drafts._drafts_fallback.clear()
    drafts._last_comment_fallback.clear()

    user_id = 7322846895
    original = drafts.Draft(
        links=["https://vt.tiktok.com/example/"],
        comment="old comment",
        text_variants=["one", "two", "three"],
    )
    drafts.save_draft(user_id, original)

    redis_key = drafts._get_redis_key("data", user_id)
    assert redis.ttls[redis_key] == 86400
    assert all(command[0] != "hset" or len(command) == 4 for command in redis.executed_batches[0])
    assert drafts.get_draft(user_id).links == original.links

    drafts.set_comment(user_id, "new comment")
    restored = drafts.get_draft(user_id)

    assert restored.comment == "new comment"
    assert restored.text_variants == []
    assert restored.links == original.links
    assert redis.strings[drafts._get_redis_key("last_comment", user_id)] == "new comment"


@pytest.mark.unit
@pytest.mark.asyncio
async def test_order_callback_passes_actor_id_instead_of_bot_message_author(monkeypatch):
    from bot.handlers import order_wizard
    from bot.services.drafts import Draft

    actor_id = 7322846895
    bot_message = SimpleNamespace(from_user=SimpleNamespace(id=8398035563))
    callback = SimpleNamespace(
        from_user=SimpleNamespace(id=actor_id),
        message=bot_message,
        answer=AsyncMock(),
    )
    captured = {}

    monkeypatch.setattr(
        order_wizard,
        "get_draft",
        lambda user_id: Draft(links=["https://vt.tiktok.com/example/"])
    )
    monkeypatch.setattr(order_wizard, "save_draft", lambda *_args, **_kwargs: None)

    async def _fake_impl(target, state, **kwargs):
        captured.update({"target": target, "state": state, **kwargs})

    monkeypatch.setattr(order_wizard, "_wizard_create_order_impl", _fake_impl)
    state = object()

    await order_wizard.wizard_create_order_from_callback(callback, state)

    assert captured["target"] is bot_message
    assert captured["state"] is state
    assert captured["user_id"] == actor_id
    assert captured["is_message"] is False

