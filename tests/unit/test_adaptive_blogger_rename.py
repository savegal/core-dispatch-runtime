from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import func, select

from api.adaptive_monitoring import BloggerUsernameUpdate, delete_blogger, get_adaptive_blogger, rename_blogger_username
from db.models import BloggerCheckLog, CreatorSource, CreatorVideo, MonitorOutbox, MonitoredBlogger, User, VideoBatch


@pytest.mark.unit
@pytest.mark.asyncio
async def test_rename_blogger_updates_linked_sources_and_resets_access_failure(async_db_session):
    user = User(
        telegram_id=123456,
        username="worker",
        role="user",
        balance=0,
        is_blocked=False,
    )
    blogger = MonitoredBlogger(
        username="old_name",
        priority_mode="paused",
        empty_streak=8,
        next_check_at=datetime.now(timezone.utc) + timedelta(hours=2),
        posting_stats={
            "profile_access_failure": {"reason": "profile_not_found", "streak": 6},
            "monitor_failure": {"reason": "old", "streak": 2},
            "next_interval_minutes": 180,
        },
    )
    async_db_session.add_all([user, blogger])
    await async_db_session.flush()
    source = CreatorSource(
        user_id=user.id,
        username="old_name",
        monitored_blogger_id=blogger.id,
        enabled=True,
    )
    async_db_session.add(source)
    await async_db_session.commit()

    item = await rename_blogger_username(
        blogger.id,
        BloggerUsernameUpdate(username="https://www.tiktok.com/@New.Name"),
        _=None,
        db=async_db_session,
    )

    assert item.username == "new.name"
    assert item.priority_mode == "auto"
    assert item.empty_streak == 0
    assert item.profile_access_failure is None

    updated_source = (
        await async_db_session.execute(select(CreatorSource).where(CreatorSource.id == source.id))
    ).scalar_one()
    updated_blogger = (
        await async_db_session.execute(select(MonitoredBlogger).where(MonitoredBlogger.id == blogger.id))
    ).scalar_one()

    assert updated_source.username == "new.name"
    assert updated_blogger.posting_stats["renamed_from"] == "old_name"
    assert "profile_access_failure" not in updated_blogger.posting_stats
    assert "monitor_failure" not in updated_blogger.posting_stats


@pytest.mark.unit
@pytest.mark.asyncio
async def test_get_adaptive_blogger_returns_item_beyond_list_window(async_db_session):
    early = MonitoredBlogger(username="aaa_first")
    target = MonitoredBlogger(
        username="zzz_target",
        priority_mode="vip",
        posting_stats={"status": "active", "next_interval_minutes": 12},
    )
    async_db_session.add_all([early, target])
    await async_db_session.commit()

    item = await get_adaptive_blogger(
        target.id,
        _=None,
        db=async_db_session,
    )

    assert item.id == target.id
    assert item.username == "zzz_target"
    assert item.priority_mode == "vip"
    assert item.next_interval_minutes == 12


@pytest.mark.unit
@pytest.mark.asyncio
async def test_delete_blogger_removes_it_for_all_users(async_db_session):
    user_a = User(telegram_id=123457, username="worker_a", role="user", balance=0, is_blocked=False)
    user_b = User(telegram_id=123458, username="worker_b", role="user", balance=0, is_blocked=False)
    blogger = MonitoredBlogger(username="shared_target")
    other_blogger = MonitoredBlogger(username="other_target")
    async_db_session.add_all([user_a, user_b, blogger, other_blogger])
    await async_db_session.flush()

    source_a = CreatorSource(user_id=user_a.id, username="shared_target", monitored_blogger_id=blogger.id, enabled=True)
    source_b = CreatorSource(user_id=user_b.id, username="SHARED_TARGET", monitored_blogger_id=None, enabled=True)
    other_source = CreatorSource(user_id=user_a.id, username="other_target", monitored_blogger_id=other_blogger.id, enabled=True)
    async_db_session.add_all([source_a, source_b, other_source])
    await async_db_session.flush()

    video_a = CreatorVideo(creator_id=source_a.id, video_id="v1", url="https://tiktok.com/@shared_target/video/1")
    video_b = CreatorVideo(creator_id=source_b.id, video_id="v2", url="https://tiktok.com/@shared_target/video/2")
    other_video = CreatorVideo(creator_id=other_source.id, video_id="v3", url="https://tiktok.com/@other_target/video/3")
    batch = VideoBatch(creator_id=source_a.id, video_ids=["v1"])
    outbox_by_creator = MonitorOutbox(
        event_type="autopilot_create",
        status="pending",
        blogger_id=None,
        creator_id=source_b.id,
        payload={"creator_id": source_b.id, "video_ids": []},
    )
    outbox_by_blogger = MonitorOutbox(
        event_type="notify_pending_review",
        status="pending",
        blogger_id=blogger.id,
        creator_id=None,
        payload={"creator_id": None, "video_ids": []},
    )
    check_log = BloggerCheckLog(blogger_id=blogger.id, status="OK")
    async_db_session.add_all([video_a, video_b, other_video, batch, outbox_by_creator, outbox_by_blogger, check_log])
    await async_db_session.commit()

    result = await delete_blogger(blogger.id, _=None, db=async_db_session)

    assert result["ok"] is True
    assert result["username"] == "shared_target"
    assert result["deleted_sources"] == 2
    assert result["deleted_videos"] == 2
    assert result["deleted_batches"] == 1
    assert result["deleted_outbox"] == 2
    assert result["deleted_check_logs"] == 1

    assert (await async_db_session.execute(select(func.count(MonitoredBlogger.id)).where(MonitoredBlogger.id == blogger.id))).scalar_one() == 0
    assert (await async_db_session.execute(select(func.count(CreatorSource.id)).where(CreatorSource.username.ilike("shared_target")))).scalar_one() == 0
    assert (await async_db_session.execute(select(func.count(CreatorVideo.id)).where(CreatorVideo.id.in_([video_a.id, video_b.id])))).scalar_one() == 0
    assert (await async_db_session.execute(select(func.count(MonitorOutbox.id)).where(MonitorOutbox.blogger_id == blogger.id))).scalar_one() == 0

    preserved_source = (
        await async_db_session.execute(select(CreatorSource).where(CreatorSource.id == other_source.id))
    ).scalar_one()
    preserved_video = (
        await async_db_session.execute(select(CreatorVideo).where(CreatorVideo.id == other_video.id))
    ).scalar_one()
    assert preserved_source.username == "other_target"
    assert preserved_video.video_id == "v3"
