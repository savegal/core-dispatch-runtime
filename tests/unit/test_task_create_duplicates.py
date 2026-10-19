"""Creation uses isolated SQLite and never enqueues live orders."""

import inspect
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from starlette.requests import Request

from api import tasks
from db.database import Base
from db.models import Task, User


@pytest.fixture
async def order_db(monkeypatch):
    engine = create_async_engine('sqlite+aiosqlite:///:memory:')
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    monkeypatch.setattr(tasks, 'get_boost_likes_config_async', AsyncMock(return_value=(False, 0)))
    monkeypatch.setattr(tasks, 'get_boost_likes_target_mode_async', AsyncMock(return_value='single_top'))
    monkeypatch.setattr(tasks, 'get_order_execution_strategy_async', AsyncMock(return_value='parallel'))
    monkeypatch.setattr(tasks, 'reserve_task_for_enqueue_async', AsyncMock())
    monkeypatch.setattr(tasks, 'finalize_reserved_enqueue_async', AsyncMock())
    monkeypatch.setattr(tasks, 'claim_dedup_key', lambda *args, **kwargs: True)
    try:
        async with async_sessionmaker(engine, expire_on_commit=False)() as session:
            session.add(User(id=1, telegram_id=12345, role='admin', is_blocked=False))
            await session.commit()
            yield session
    finally:
        await engine.dispose()


async def _create(db, kind, key=None):
    request = Request({'type': 'http', 'headers': [(b'x-idempotency-key', key.encode())] if key else []})
    payload = tasks.TaskCreate(
        user_id=1, main_comment='same comment', text_variants=['same comment'] * 3,
        video_urls=['https://www.tiktok.com/@demo/video/1234567890123456789'],
        kind=kind, reply1='same reply', engagement_comment_text='same target',
    )
    return await inspect.unwrap(tasks.create_task)(request=request, task_data=payload, db=db, auth_user_id=1)


@pytest.mark.unit
@pytest.mark.asyncio
@pytest.mark.parametrize('kind', ['full', 'engagement_only'])
@pytest.mark.parametrize('previous_status', ['pending', 'in_progress', 'done', 'partial'])
async def test_identical_orders_create_separate_rows(order_db, kind, previous_status):
    first = await _create(order_db, kind)
    previous = await order_db.get(Task, first['task_id'])
    previous.status = previous_status
    await order_db.commit()
    second = await _create(order_db, kind, key='new-order')
    third = await _create(order_db, kind)
    assert len({first['task_id'], second['task_id'], third['task_id']}) == 3
    assert second['status'] == third['status'] == 'pending'
    assert len((await order_db.execute(select(Task))).scalars().all()) == 3


@pytest.mark.unit
@pytest.mark.asyncio
@pytest.mark.parametrize('kind', ['full', 'engagement_only'])
async def test_same_request_key_still_reuses_one_order(order_db, kind):
    first = await _create(order_db, kind, key='one-request')
    repeat = await _create(order_db, kind, key='one-request')
    assert repeat['task_id'] == first['task_id']
    assert repeat['idempotency_reuse'] is True
    assert len((await order_db.execute(select(Task))).scalars().all()) == 1
