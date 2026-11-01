"""One paid keyword request stays one order through concurrent API sessions."""
import asyncio
from decimal import Decimal
import inspect
from types import SimpleNamespace

from fastapi import HTTPException
import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker
from starlette.requests import Request

from api import tasks
from db.database import Base
from db.models import Task, Transaction, User
from services import task_service
from tests.unit.test_task_create_duplicates import order_db


pytestmark = [pytest.mark.unit, pytest.mark.asyncio]
URL = "https://www.tiktok.com/@fixture/video/1234567890123456789"


@pytest.mark.parametrize("redis_available", [False, True])
async def test_keyword_same_request_during_creation_then_after_lost_ack_holds_once(
        order_db, tmp_path, monkeypatch, redis_available):
    engine = create_async_engine(f"sqlite+aiosqlite:///{(tmp_path / 'requests.db').as_posix()}")
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    first_reserved = asyncio.Event()
    finish_creation = asyncio.Event()
    pending = None

    class RedisFixture:
        def __init__(self):
            self.keys = set()

        def set(self, key, value, *, nx, ex):
            assert nx and ex > 0
            if key in self.keys:
                return False
            self.keys.add(key)
            return True

    def unavailable():
        raise ConnectionError("local fixture: Redis unavailable")

    monkeypatch.setattr(task_service, "_local_dedup_cache", {})
    store = RedisFixture()
    monkeypatch.setattr(task_service, "get_redis", (lambda: store) if redis_available else unavailable)
    monkeypatch.setattr(tasks, "claim_dedup_key", task_service.claim_dedup_key)
    monkeypatch.setattr(tasks.settings, "TIKTOK_NETWORK_IMPL_READY", True)
    monkeypatch.setattr(tasks.settings, "DEMO_MODE", False)
    monkeypatch.setattr(tasks.settings, "ADMIN_IDS", [])

    async def pricing(db):
        first_reserved.set()
        await asyncio.wait_for(finish_creation.wait(), 10)
        return SimpleNamespace(price_success=Decimal("2.00"))

    monkeypatch.setattr(tasks, "get_pricing_async", pricing)

    async def create():
        async with sessions() as db:
            return await inspect.unwrap(tasks.create_task)(
                request=Request({"type": "http", "headers": [(b"x-idempotency-key", b"same-keyword-request")]}),
                task_data=tasks.TaskCreate(user_id=1, kind="keyword_replies", video_urls=[URL],
                                           keywords=["price"], reply1="Reply"),
                db=db, auth_user_id=1)

    try:
        async with engine.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)
        async with sessions() as db:
            db.add(User(id=1, telegram_id=12345, role="user", balance=Decimal("10.00")))
            await db.commit()
        pending = asyncio.create_task(create())
        await asyncio.wait_for(first_reserved.wait(), 10)
        with pytest.raises(HTTPException) as error:
            await create()
        assert error.value.status_code == 409
        assert "ORDER_REQUEST_IN_PROGRESS" in error.value.detail
        finish_creation.set()
        accepted = await pending
        repeated = await create()  # The first response could have been lost in transit.
        assert repeated["task_id"] == accepted["task_id"] and repeated["idempotency_reuse"]
        async with sessions() as db:
            assert (await db.get(User, 1)).balance == Decimal("8.00")
            assert len((await db.execute(select(Task))).scalars().all()) == 1
            receipts = (await db.execute(select(Transaction))).scalars().all()
            assert len(receipts) == 1 and receipts[0].amount == Decimal("-2.00")
    finally:
        finish_creation.set()
        if pending is not None:
            await asyncio.gather(pending, return_exceptions=True)
        await engine.dispose()
