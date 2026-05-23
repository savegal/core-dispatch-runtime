import inspect

import pytest

from api.video_batch import _as_utc_datetime, BatchCreateRequest, create_batch, get_batch
from bot.handlers.video_inbox import comment_all_videos
from db.models import AppSettings, CreatorSource, User, VideoBatch
from services.app_settings_service import SOFTWARE_MODE_KEY, SOFTWARE_MODE_PARSING
from services.notify_service import notify_pending_review
from datetime import datetime, timedelta, timezone


@pytest.mark.unit
def test_video_batches_live_for_seven_days():
    assert "timedelta(days=7)" in inspect.getsource(create_batch)
    assert "timedelta(days=7)" in inspect.getsource(notify_pending_review)


@pytest.mark.unit
def test_video_batch_datetime_normalizer_accepts_naive_values():
    normalized = _as_utc_datetime(datetime(2026, 5, 18, 12, 0, 0))

    assert normalized is not None
    assert normalized.tzinfo is timezone.utc


@pytest.mark.unit
@pytest.mark.asyncio
async def test_get_video_batch_accepts_naive_expires_at(async_db_session):
    user = User(
        telegram_id=92001,
        username="batch_user",
        role="user",
        balance=100,
        is_blocked=False,
    )
    async_db_session.add(user)
    await async_db_session.flush()

    creator = CreatorSource(
        user_id=user.id,
        username="batch_creator",
        enabled=True,
        interval_minutes=30,
        mode="manual_review",
    )
    async_db_session.add(creator)
    await async_db_session.flush()

    batch = VideoBatch(
        creator_id=creator.id,
        video_ids=["111", "222"],
        expires_at=datetime.now() + timedelta(days=1),
    )
    async_db_session.add(batch)
    await async_db_session.commit()

    result = await get_batch(
        batch_id=batch.id,
        user_id=user.id,
        db=async_db_session,
        auth_user_id=user.id,
    )

    assert result["video_ids"] == ["111", "222"]
    assert result["expires_at"].endswith("+00:00")


@pytest.mark.unit
@pytest.mark.asyncio
async def test_create_video_batch_allowed_for_full_access_in_parsing_mode(async_db_session):
    user = User(
        telegram_id=92002,
        username="batch_full_access_user",
        role="user",
        balance=100,
        is_blocked=False,
        full_access_override=True,
    )
    async_db_session.add_all([
        AppSettings(key=SOFTWARE_MODE_KEY, value=SOFTWARE_MODE_PARSING),
        user,
    ])
    await async_db_session.flush()

    creator = CreatorSource(
        user_id=user.id,
        username="batch_full_access_creator",
        enabled=True,
        interval_minutes=30,
        mode="manual_review",
    )
    async_db_session.add(creator)
    await async_db_session.flush()

    result = await create_batch(
        request=BatchCreateRequest(creator_id=creator.id, video_ids=["111"]),
        user_id=user.id,
        db=async_db_session,
        auth_user_id=user.id,
    )

    assert result["creator_id"] == creator.id
    assert result["video_count"] == 1


@pytest.mark.unit
def test_inbox_comment_all_groups_batches_by_creator():
    source = inspect.getsource(comment_all_videos)

    assert "_start_order_for_videos" in source
    assert "vp:new_videos:select_template:{creator_id}:{batch_id}" in source
    assert "primary_creator_id = creator_ids[0]" not in source
