from __future__ import annotations

import asyncio
import threading
import time
from contextlib import contextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest


def _configure_parallel_6x1(monkeypatch, user_schema) -> None:
    monkeypatch.setattr(user_schema._settings, "PARALLEL_COMMENTING_TARGET_BROWSERS", 3, raising=False)
    monkeypatch.setattr(user_schema._settings, "PARALLEL_COMMENTING_MAX_BROWSERS", 3, raising=False)
    monkeypatch.setattr(user_schema._settings, "PARALLEL_6X1_MAX_BROWSERS", 3, raising=False)
    monkeypatch.setattr(user_schema._settings, "PARALLEL_6X1_MAIN_WORKERS", 3, raising=False)
    monkeypatch.setattr(
        user_schema._settings,
        "PARALLEL_6X1_MAX_ACTIVE_BROWSERS_PER_PROXY",
        3,
        raising=False,
    )
    monkeypatch.setattr(
        user_schema._settings,
        "PARALLEL_6X1_MAX_MAIN_ATTEMPTS_PER_SLOT",
        6,
        raising=False,
    )
    monkeypatch.setattr(
        user_schema._settings,
        "PARALLEL_6X1_MAIN_REFILL_POLL_SECONDS",
        0.05,
        raising=False,
    )


@pytest.mark.unit
def test_extend_main_account_reservation_keeps_existing_lease_alive():
    from services import account_service

    account_id = 991_001
    account_service.release_main_account_reservation(account_id)
    try:
        assert account_service.reserve_main_account(account_id, ttl_seconds=1.0) is True
        assert account_service.extend_main_account_reservation(account_id, ttl_seconds=360.0) is True
        assert account_service._MAIN_ACCOUNT_RESERVED_UNTIL[account_id] > time.monotonic() + 300.0
    finally:
        account_service.release_main_account_reservation(account_id)


@pytest.mark.unit
@pytest.mark.asyncio
async def test_parallel_6x1_runs_two_strict_waves_and_replaces_failed_account(monkeypatch):
    from db import database as db_database
    from executor import browser_pool, user_schema

    shared_proxy = "http://mobile-6x1:1000"
    proxy_obj = SimpleNamespace(
        id=77,
        proxy=shared_proxy,
        is_mobile="Y",
        rotation_url="http://rotate.local",
    )

    class FakeQuery:
        def filter(self, *_args, **_kwargs):
            return self

        def first(self):
            return proxy_obj

    class FakeDb:
        no_autoflush = SimpleNamespace()

        def query(self, *_args, **_kwargs):
            return FakeQuery()

    @contextmanager
    def fake_db_context():
        yield FakeDb()

    accounts = [SimpleNamespace(id=101 + index, proxy=shared_proxy) for index in range(7)]
    selected_ids: set[int] = set()
    selection_lock = threading.Lock()
    events: list[tuple] = []
    active_browsers = 0
    max_active_browsers = 0

    def fake_get_main(_db, *, exclude_ids=None, exclude_proxies=None, **_kwargs):
        excluded_ids = {int(value) for value in (exclude_ids or [])}
        excluded_proxies = {
            str(value or "").strip()
            for value in (exclude_proxies or [])
            if str(value or "").strip()
        }
        with selection_lock:
            candidate = next(
                (
                    account
                    for account in accounts
                    if account.id not in selected_ids
                    and account.id not in excluded_ids
                    and account.proxy not in excluded_proxies
                ),
                None,
            )
            if candidate is not None:
                selected_ids.add(candidate.id)
            return candidate

    async def fake_try_main(_db, account, _video_url, texts, **_kwargs):
        nonlocal active_browsers, max_active_browsers
        assert len(texts) == 1
        text = texts[0]
        events.append(("start", account.id, text))
        active_browsers += 1
        max_active_browsers = max(max_active_browsers, active_browsers)
        try:
            await asyncio.sleep(0.01)
            if account.id == 101:
                events.append(("fail", account.id, text))
                return [], "account_login_failed"
            events.append(("publish", account.id, text))
            return [
                user_schema.PublishedMain(
                    text=text,
                    comment_id=f"cid-{account.id}-{text}",
                    network_confirmed=True,
                    local_confirmed=True,
                    method="hybrid_dom",
                    account_id=account.id,
                    tiktok_username=f"user_{account.id}",
                )
            ], None
        finally:
            active_browsers -= 1

    async def fake_close_pool():
        events.append(("close_pool",))

    async def fake_acquire_lock(proxy_id, timeout=30):
        events.append(("lock", proxy_id))
        return True

    async def fake_rotate(proxy, _db, previous_ip=None, *, force_rotation=False):
        assert proxy.id == 77
        assert previous_ip == "198.51.100.1"
        assert force_rotation is True
        events.append(("rotate", proxy.id))
        return "198.51.100.2"

    _configure_parallel_6x1(monkeypatch, user_schema)
    monkeypatch.setattr(db_database, "get_sync_db_context", fake_db_context)
    monkeypatch.setattr(user_schema, "get_free_account_for_main_comment", fake_get_main)
    monkeypatch.setattr(user_schema, "_try_main_account", fake_try_main)
    monkeypatch.setattr(user_schema, "_record_main_retry_failure", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(user_schema, "release_main_account_reservation", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(user_schema, "extend_main_account_reservation", lambda *_args, **_kwargs: True)
    monkeypatch.setattr(user_schema, "acquire_proxy_lock", fake_acquire_lock)
    monkeypatch.setattr(user_schema, "release_proxy_lock", AsyncMock(return_value=None))
    monkeypatch.setattr(user_schema, "get_current_ip", AsyncMock(return_value="198.51.100.1"))
    monkeypatch.setattr(user_schema, "ensure_proxy_ip_rotated", fake_rotate)
    monkeypatch.setattr(user_schema, "save_proxy_last_ip", AsyncMock(return_value=None))
    monkeypatch.setattr(user_schema, "_maybe_order_likes", AsyncMock(return_value=None))
    monkeypatch.setattr(browser_pool, "ensure_thread_browser_pool_capacity", lambda _minimum: None)
    monkeypatch.setattr(browser_pool, "close_browser_pool", fake_close_pool)

    result = await asyncio.wait_for(
        user_schema.execute_user_schema_parallel_6x1(
            SimpleNamespace(),
            SimpleNamespace(settings={"replies": [], "reply_count": 0}),
            "https://www.tiktok.com/@demo/video/1234567890123456789",
            ["m1", "m2", "m3"],
            610,
            boost_enabled=False,
            boost_quantity=0,
        ),
        timeout=5.0,
    )

    starts = [event for event in events if event[0] == "start"]
    publishes = [event for event in events if event[0] == "publish"]
    assert result.error is None
    assert result.expected_main_publications == 6
    assert len(result.published_mains) == 6
    assert len({main.account_id for main in result.published_mains}) == 6
    assert max_active_browsers == 3
    assert len(starts) == 7
    assert {event[2] for event in starts[:3]} == {"m1", "m2", "m3"}
    assert ("publish", 104, "m1") in publishes
    assert [main.text for main in result.published_mains].count("m1") == 2
    assert [main.text for main in result.published_mains].count("m2") == 2
    assert [main.text for main in result.published_mains].count("m3") == 2

    rotate_index = events.index(("rotate", 77))
    first_wave_publish_indexes = [
        events.index(event)
        for event in publishes
        if event[1] in {102, 103, 104}
    ]
    second_wave_start_indexes = [events.index(event) for event in starts if event[1] >= 105]
    assert max(first_wave_publish_indexes) < rotate_index
    assert rotate_index < min(second_wave_start_indexes)
    assert events[:rotate_index].count(("close_pool",)) == 1


@pytest.mark.unit
@pytest.mark.asyncio
async def test_parallel_6x1_blocks_second_wave_when_rotation_fails(monkeypatch):
    from db import database as db_database
    from executor import browser_pool, user_schema

    shared_proxy = "http://mobile-6x1-fail:1000"
    proxy_obj = SimpleNamespace(
        id=88,
        proxy=shared_proxy,
        is_mobile="Y",
        rotation_url="http://rotate.local",
    )

    class FakeQuery:
        def filter(self, *_args, **_kwargs):
            return self

        def first(self):
            return proxy_obj

    class FakeDb:
        no_autoflush = SimpleNamespace()

        def query(self, *_args, **_kwargs):
            return FakeQuery()

    @contextmanager
    def fake_db_context():
        yield FakeDb()

    accounts = [SimpleNamespace(id=201 + index, proxy=shared_proxy) for index in range(6)]
    selected_ids: set[int] = set()

    def fake_get_main(_db, *, exclude_ids=None, exclude_proxies=None, **_kwargs):
        excluded_ids = {int(value) for value in (exclude_ids or [])}
        excluded_proxies = set(exclude_proxies or [])
        candidate = next(
            (
                account
                for account in accounts
                if account.id not in selected_ids
                and account.id not in excluded_ids
                and account.proxy not in excluded_proxies
            ),
            None,
        )
        if candidate is not None:
            selected_ids.add(candidate.id)
        return candidate

    started_ids: list[int] = []

    async def fake_try_main(_db, account, _video_url, texts, **_kwargs):
        started_ids.append(account.id)
        text = texts[0]
        return [
            user_schema.PublishedMain(
                text=text,
                comment_id=f"cid-{account.id}",
                network_confirmed=True,
                local_confirmed=True,
                method="hybrid_dom",
                account_id=account.id,
                tiktok_username=f"user_{account.id}",
            )
        ], None

    async def failed_rotate(*_args, **_kwargs):
        raise user_schema.ProxyRotationError("provider failed")

    _configure_parallel_6x1(monkeypatch, user_schema)
    monkeypatch.setattr(db_database, "get_sync_db_context", fake_db_context)
    monkeypatch.setattr(user_schema, "get_free_account_for_main_comment", fake_get_main)
    monkeypatch.setattr(user_schema, "_try_main_account", fake_try_main)
    monkeypatch.setattr(user_schema, "release_main_account_reservation", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(user_schema, "extend_main_account_reservation", lambda *_args, **_kwargs: True)
    monkeypatch.setattr(user_schema, "acquire_proxy_lock", AsyncMock(return_value=True))
    monkeypatch.setattr(user_schema, "release_proxy_lock", AsyncMock(return_value=None))
    monkeypatch.setattr(user_schema, "get_current_ip", AsyncMock(return_value="203.0.113.1"))
    monkeypatch.setattr(user_schema, "ensure_proxy_ip_rotated", failed_rotate)
    monkeypatch.setattr(user_schema, "_maybe_order_likes", AsyncMock(return_value=None))
    monkeypatch.setattr(browser_pool, "ensure_thread_browser_pool_capacity", lambda _minimum: None)
    monkeypatch.setattr(browser_pool, "close_browser_pool", AsyncMock(return_value=None))

    result = await asyncio.wait_for(
        user_schema.execute_user_schema_parallel_6x1(
            SimpleNamespace(),
            SimpleNamespace(settings={"replies": ["reply"], "reply_count": 1}),
            "https://www.tiktok.com/@demo/video/1234567890123456789",
            ["m1", "m2", "m3"],
            611,
            boost_enabled=False,
            boost_quantity=0,
        ),
        timeout=5.0,
    )

    assert result.error == "parallel_6x1_rotation_failed"
    assert started_ids == [201, 202, 203]
    assert len(result.published_mains) == 3
    assert result.replies == []


@pytest.mark.unit
@pytest.mark.asyncio
async def test_parallel_6x1_skips_non_rotatable_proxy_accounts_in_wave_one(monkeypatch):
    """First wave must never run on a proxy that cannot rotate afterwards."""
    from db import database as db_database
    from executor import browser_pool, user_schema

    rotatable_proxy = "http://mobile-6x1-ok:1000"
    residential_proxy = "http://residential-6x1:2000"
    proxies_by_value = {
        rotatable_proxy: SimpleNamespace(
            id=77,
            proxy=rotatable_proxy,
            is_mobile="Y",
            rotation_url="http://rotate.local",
        ),
        residential_proxy: SimpleNamespace(
            id=78,
            proxy=residential_proxy,
            is_mobile="N",
            rotation_url="",
        ),
    }

    class FakeProxyQuery:
        def __init__(self):
            self._value = None

        def filter(self, *args, **_kwargs):
            for arg in args:
                try:
                    self._value = arg.right.value
                except Exception:
                    continue
            return self

        def first(self):
            return proxies_by_value.get(self._value)

    class FakeDb:
        no_autoflush = SimpleNamespace()

        def query(self, *_args, **_kwargs):
            return FakeProxyQuery()

    @contextmanager
    def fake_db_context():
        yield FakeDb()

    # Account 101 sits on a non-rotatable residential proxy and must be skipped.
    accounts = [SimpleNamespace(id=101, proxy=residential_proxy)] + [
        SimpleNamespace(id=102 + index, proxy=rotatable_proxy) for index in range(7)
    ]
    selected_ids: set[int] = set()
    selection_lock = threading.Lock()
    started_ids: list[int] = []

    def fake_get_main(_db, *, exclude_ids=None, exclude_proxies=None, **_kwargs):
        excluded_ids = {int(value) for value in (exclude_ids or [])}
        excluded_proxies = {
            str(value or "").strip()
            for value in (exclude_proxies or [])
            if str(value or "").strip()
        }
        with selection_lock:
            candidate = next(
                (
                    account
                    for account in accounts
                    if account.id not in selected_ids
                    and account.id not in excluded_ids
                    and account.proxy not in excluded_proxies
                ),
                None,
            )
            if candidate is not None:
                selected_ids.add(candidate.id)
            return candidate

    async def fake_try_main(_db, account, _video_url, texts, **_kwargs):
        started_ids.append(account.id)
        text = texts[0]
        await asyncio.sleep(0.01)
        return [
            user_schema.PublishedMain(
                text=text,
                comment_id=f"cid-{account.id}",
                network_confirmed=True,
                local_confirmed=True,
                method="hybrid_dom",
                account_id=account.id,
                tiktok_username=f"user_{account.id}",
            )
        ], None

    async def fake_rotate(proxy, _db, previous_ip=None, *, force_rotation=False):
        assert proxy.id == 77
        return "198.51.100.2"

    _configure_parallel_6x1(monkeypatch, user_schema)
    monkeypatch.setattr(db_database, "get_sync_db_context", fake_db_context)
    monkeypatch.setattr(user_schema, "get_free_account_for_main_comment", fake_get_main)
    monkeypatch.setattr(user_schema, "_try_main_account", fake_try_main)
    monkeypatch.setattr(user_schema, "_record_main_retry_failure", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(user_schema, "release_main_account_reservation", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(user_schema, "extend_main_account_reservation", lambda *_args, **_kwargs: True)
    monkeypatch.setattr(user_schema, "acquire_proxy_lock", AsyncMock(return_value=True))
    monkeypatch.setattr(user_schema, "release_proxy_lock", AsyncMock(return_value=None))
    monkeypatch.setattr(user_schema, "get_current_ip", AsyncMock(return_value="198.51.100.1"))
    monkeypatch.setattr(user_schema, "ensure_proxy_ip_rotated", fake_rotate)
    monkeypatch.setattr(user_schema, "save_proxy_last_ip", AsyncMock(return_value=None))
    monkeypatch.setattr(user_schema, "_maybe_order_likes", AsyncMock(return_value=None))
    monkeypatch.setattr(browser_pool, "ensure_thread_browser_pool_capacity", lambda _minimum: None)
    monkeypatch.setattr(browser_pool, "close_browser_pool", AsyncMock(return_value=None))

    result = await asyncio.wait_for(
        user_schema.execute_user_schema_parallel_6x1(
            SimpleNamespace(),
            SimpleNamespace(settings={"replies": [], "reply_count": 0}),
            "https://www.tiktok.com/@demo/video/1234567890123456789",
            ["m1", "m2", "m3"],
            613,
            boost_enabled=False,
            boost_quantity=0,
        ),
        timeout=5.0,
    )

    assert result.error is None
    assert 101 not in started_ids
    assert len(result.published_mains) == 6
    assert all(main.account_id != 101 for main in result.published_mains)


@pytest.mark.unit
@pytest.mark.asyncio
async def test_parallel_6x1_blocks_rotation_when_browser_close_fails(monkeypatch):
    """A failed browser close must stop the mandatory rotation and second wave."""
    from db import database as db_database
    from executor import browser_pool, user_schema

    shared_proxy = "http://mobile-6x1-close:1000"
    proxy_obj = SimpleNamespace(
        id=99,
        proxy=shared_proxy,
        is_mobile="Y",
        rotation_url="http://rotate.local",
    )

    class FakeQuery:
        def filter(self, *_args, **_kwargs):
            return self

        def first(self):
            return proxy_obj

    class FakeDb:
        no_autoflush = SimpleNamespace()

        def query(self, *_args, **_kwargs):
            return FakeQuery()

    @contextmanager
    def fake_db_context():
        yield FakeDb()

    accounts = [SimpleNamespace(id=301 + index, proxy=shared_proxy) for index in range(6)]
    selected_ids: set[int] = set()
    started_ids: list[int] = []

    def fake_get_main(_db, *, exclude_ids=None, exclude_proxies=None, **_kwargs):
        excluded_ids = {int(value) for value in (exclude_ids or [])}
        excluded_proxies = set(exclude_proxies or [])
        candidate = next(
            (
                account
                for account in accounts
                if account.id not in selected_ids
                and account.id not in excluded_ids
                and account.proxy not in excluded_proxies
            ),
            None,
        )
        if candidate is not None:
            selected_ids.add(candidate.id)
        return candidate

    async def fake_try_main(_db, account, _video_url, texts, **_kwargs):
        started_ids.append(account.id)
        text = texts[0]
        return [
            user_schema.PublishedMain(
                text=text,
                comment_id=f"cid-{account.id}",
                network_confirmed=True,
                local_confirmed=True,
                method="hybrid_dom",
                account_id=account.id,
                tiktok_username=f"user_{account.id}",
            )
        ], None

    async def failing_close():
        raise RuntimeError("cannot close browser pool")

    rotate_mock = AsyncMock(return_value="203.0.113.2")

    _configure_parallel_6x1(monkeypatch, user_schema)
    monkeypatch.setattr(db_database, "get_sync_db_context", fake_db_context)
    monkeypatch.setattr(user_schema, "get_free_account_for_main_comment", fake_get_main)
    monkeypatch.setattr(user_schema, "_try_main_account", fake_try_main)
    monkeypatch.setattr(user_schema, "release_main_account_reservation", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(user_schema, "extend_main_account_reservation", lambda *_args, **_kwargs: True)
    monkeypatch.setattr(user_schema, "acquire_proxy_lock", AsyncMock(return_value=True))
    monkeypatch.setattr(user_schema, "release_proxy_lock", AsyncMock(return_value=None))
    monkeypatch.setattr(user_schema, "get_current_ip", AsyncMock(return_value="203.0.113.1"))
    monkeypatch.setattr(user_schema, "ensure_proxy_ip_rotated", rotate_mock)
    monkeypatch.setattr(user_schema, "save_proxy_last_ip", AsyncMock(return_value=None))
    monkeypatch.setattr(user_schema, "_maybe_order_likes", AsyncMock(return_value=None))
    monkeypatch.setattr(browser_pool, "ensure_thread_browser_pool_capacity", lambda _minimum: None)
    monkeypatch.setattr(browser_pool, "close_browser_pool", failing_close)

    result = await asyncio.wait_for(
        user_schema.execute_user_schema_parallel_6x1(
            SimpleNamespace(),
            SimpleNamespace(settings={"replies": ["reply"], "reply_count": 1}),
            "https://www.tiktok.com/@demo/video/1234567890123456789",
            ["m1", "m2", "m3"],
            614,
            boost_enabled=False,
            boost_quantity=0,
        ),
        timeout=5.0,
    )

    assert result.error == "parallel_6x1_browser_close_failed"
    assert started_ids == [301, 302, 303]
    assert len(result.published_mains) == 3
    assert result.replies == []
    rotate_mock.assert_not_called()


@pytest.mark.unit
@pytest.mark.asyncio
async def test_parallel_6x1_stops_before_rotation_when_user_stops(monkeypatch):
    """A user stop after wave 1 must abort before the mandatory rotation."""
    from db import database as db_database
    from executor import browser_pool, user_schema

    shared_proxy = "http://mobile-6x1-stop:1000"
    proxy_obj = SimpleNamespace(
        id=111,
        proxy=shared_proxy,
        is_mobile="Y",
        rotation_url="http://rotate.local",
    )

    class FakeQuery:
        def filter(self, *_args, **_kwargs):
            return self

        def first(self):
            return proxy_obj

    class FakeDb:
        no_autoflush = SimpleNamespace()

        def query(self, *_args, **_kwargs):
            return FakeQuery()

    @contextmanager
    def fake_db_context():
        yield FakeDb()

    accounts = [SimpleNamespace(id=401 + index, proxy=shared_proxy) for index in range(6)]
    selected_ids: set[int] = set()
    started_ids: list[int] = []
    published_count = [0]

    def fake_get_main(_db, *, exclude_ids=None, exclude_proxies=None, **_kwargs):
        excluded_ids = {int(value) for value in (exclude_ids or [])}
        excluded_proxies = set(exclude_proxies or [])
        candidate = next(
            (
                account
                for account in accounts
                if account.id not in selected_ids
                and account.id not in excluded_ids
                and account.proxy not in excluded_proxies
            ),
            None,
        )
        if candidate is not None:
            selected_ids.add(candidate.id)
        return candidate

    async def fake_try_main(_db, account, _video_url, texts, **_kwargs):
        started_ids.append(account.id)
        text = texts[0]
        await asyncio.sleep(0.01)
        published_count[0] += 1
        return [
            user_schema.PublishedMain(
                text=text,
                comment_id=f"cid-{account.id}",
                network_confirmed=True,
                local_confirmed=True,
                method="hybrid_dom",
                account_id=account.id,
                tiktok_username=f"user_{account.id}",
            )
        ], None

    rotate_mock = AsyncMock(return_value="198.51.100.9")

    def stop_after_wave_one() -> bool:
        return published_count[0] >= 3

    _configure_parallel_6x1(monkeypatch, user_schema)
    monkeypatch.setattr(db_database, "get_sync_db_context", fake_db_context)
    monkeypatch.setattr(user_schema, "get_free_account_for_main_comment", fake_get_main)
    monkeypatch.setattr(user_schema, "_try_main_account", fake_try_main)
    monkeypatch.setattr(user_schema, "release_main_account_reservation", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(user_schema, "extend_main_account_reservation", lambda *_args, **_kwargs: True)
    monkeypatch.setattr(user_schema, "acquire_proxy_lock", AsyncMock(return_value=True))
    monkeypatch.setattr(user_schema, "release_proxy_lock", AsyncMock(return_value=None))
    monkeypatch.setattr(user_schema, "get_current_ip", AsyncMock(return_value="198.51.100.1"))
    monkeypatch.setattr(user_schema, "ensure_proxy_ip_rotated", rotate_mock)
    monkeypatch.setattr(user_schema, "save_proxy_last_ip", AsyncMock(return_value=None))
    monkeypatch.setattr(user_schema, "_maybe_order_likes", AsyncMock(return_value=None))
    monkeypatch.setattr(browser_pool, "ensure_thread_browser_pool_capacity", lambda _minimum: None)
    monkeypatch.setattr(browser_pool, "close_browser_pool", AsyncMock(return_value=None))

    result = await asyncio.wait_for(
        user_schema.execute_user_schema_parallel_6x1(
            SimpleNamespace(),
            SimpleNamespace(settings={"replies": [], "reply_count": 0}),
            "https://www.tiktok.com/@demo/video/1234567890123456789",
            ["m1", "m2", "m3"],
            615,
            boost_enabled=False,
            boost_quantity=0,
            stop_requested=stop_after_wave_one,
        ),
        timeout=5.0,
    )

    assert result.error == "stopped_by_user"
    assert len(started_ids) == 3
    rotate_mock.assert_not_called()


@pytest.mark.unit
@pytest.mark.asyncio
async def test_parallel_6x1_requires_three_distinct_variants():
    from executor import user_schema

    result = await user_schema.execute_user_schema_parallel_6x1(
        SimpleNamespace(),
        SimpleNamespace(settings={"replies": [], "reply_count": 0}),
        "https://www.tiktok.com/@demo/video/1234567890123456789",
        ["same", " same ", "other"],
        612,
        boost_enabled=False,
        boost_quantity=0,
    )

    assert result.error == "parallel_6x1_requires_three_distinct_main_variants"
    assert result.published_mains == []
