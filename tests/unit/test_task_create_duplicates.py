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


@pytest.mark.asyncio
async def test_partial_retry_api_transfers_owned_receipts_and_reuses_one_child(order_db):
    first = await _create(order_db, 'engagement_only')
    original = await order_db.get(Task, first['task_id'])
    url = original.video_urls[0]
    parent = '7641761192486194454'
    original.status = 'error'
    original.settings = {**original.settings, 'url_results': {url: {
        'status': 'fail', 'comment_id': parent, 'engagement': {'replies': {'expected': 3, 'items': [
            {'variant_index': 1, 'text': 'one', 'reply_id': '7641761192486194501',
             'parent_comment_id': parent, 'success': True, 'network_confirmed': True},
            {'variant_index': 2, 'text': 'two', 'parent_comment_id': parent,
             'publication_attempted': False, 'error': 'http_preflight:inconclusive'},
            {'variant_index': 3, 'text': 'three', 'reply_id': '7641761192486194503',
             'parent_comment_id': parent, 'success': True, 'network_confirmed': True}]}}}}}
    await order_db.commit()
    payload = tasks.TaskCreate(user_id=1, main_comment='same comment',
        text_variants=['same comment'] * 3, video_urls=[url], kind='engagement_only',
        reply1='one', reply2='two', reply3='three', engagement_comment_text='anchor',
        settings={'retry_of_task_id': original.id, '_reply_resume': {'forged': True}})
    request = Request({'type': 'http', 'headers': []})
    create = inspect.unwrap(tasks.create_task)
    result = await create(request=request, task_data=payload, db=order_db, auth_user_id=1)
    child = await order_db.get(Task, result['task_id'])
    plan = child.settings['_reply_resume'][url]
    assert [item['variant_index'] for item in plan['actions']] == [2]
    assert plan['source_task_id'] == original.id
    assert 'forged' not in child.settings['_reply_resume']
    again = await create(request=request, task_data=payload, db=order_db, auth_user_id=1)
    assert again['task_id'] == child.id
