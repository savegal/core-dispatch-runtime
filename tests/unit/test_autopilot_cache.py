import asyncio

import pytest

from services.autopilot import cache as autopilot_cache


@pytest.mark.unit
@pytest.mark.asyncio
async def test_cached_with_ttl_reuses_value():
    autopilot_cache._cache.clear()
    autopilot_cache._inflight.clear()
    calls = 0

    @autopilot_cache.cached_with_ttl(60)
    async def load_value(key: str) -> dict:
        nonlocal calls
        calls += 1
        return {"key": key, "calls": calls}

    first = await load_value("a")
    second = await load_value("a")

    assert first == second
    assert calls == 1


@pytest.mark.unit
@pytest.mark.asyncio
async def test_cached_with_ttl_single_flight():
    autopilot_cache._cache.clear()
    autopilot_cache._inflight.clear()
    calls = 0
    started = asyncio.Event()

    @autopilot_cache.cached_with_ttl(60)
    async def load_value(key: str) -> dict:
        nonlocal calls
        calls += 1
        started.set()
        await asyncio.sleep(0.05)
        return {"key": key, "calls": calls}

    first = asyncio.create_task(load_value("same"))
    await started.wait()
    second = asyncio.create_task(load_value("same"))

    assert await first == await second
    assert calls == 1
