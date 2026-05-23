from datetime import datetime, timezone

import pytest

from api.video_inbox import get_inbox_videos, reset_video_to_new, skip_all_new_videos, unskip_video
from api.video_parsing import get_videos_by_ids, list_videos
from bot.services.video_display import format_video_markdown_line, video_time_label
from db.models import CreatorSource, CreatorVideo, Task, User


@pytest.mark.unit
def test_video_markdown_line_links_video_id_and_shows_publish_time():
    video = {
        "id": 10,
        "video_id": "1234567890123456789",
        "url": "https://www.tiktok.com/@demo/video/1234567890123456789",
        "create_time": 1770000000,
    }

    line = format_video_markdown_line(video, 1)

    assert line.startswith("1. ")
    assert "[123456789012…](https://www.tiktok.com/@demo/video/1234567890123456789)" in line
    assert "[1](https://www.tiktok.com/@demo/video/1234567890123456789)" not in line
    assert "опубликовано:" in line
    assert "найдено:" not in line


@pytest.mark.unit
def test_video_markdown_line_formats_creator_handle_as_code_without_escape_slash():
    video = {
        "id": 11,
        "video_id": "1234567890123456790",
        "url": "https://www.tiktok.com/@eu_manitov2/video/1234567890123456790",
        "creator_username": "eu_manitov2",
    }

    line = format_video_markdown_line(video, 2, include_username=True)

    assert "`@eu_manitov2`" in line
    assert "\\_" not in line


@pytest.mark.unit
def test_video_time_label_falls_back_to_discovered_time():
    video = {"discovered_at": "2026-05-18T10:30:00+00:00"}

    assert video_time_label(video).startswith("найдено:")


@pytest.mark.unit
@pytest.mark.asyncio
async def test_video_apis_return_publish_metadata(async_db_session):
    user = User(
        telegram_id=93001,
        username="video_meta_user",
        role="user",
        is_blocked=False,
    )
    async_db_session.add(user)
    await async_db_session.flush()

    creator = CreatorSource(
        user_id=user.id,
        username="video_meta_creator",
        enabled=True,
        interval_minutes=30,
        mode="manual_review",
    )
    async_db_session.add(creator)
    await async_db_session.flush()

    video = CreatorVideo(
        creator_id=creator.id,
        video_id="9876543210987654321",
        url="https://www.tiktok.com/@video_meta_creator/video/9876543210987654321",
        status="new",
        discovered_at=datetime(2026, 5, 18, 10, 0, tzinfo=timezone.utc),
        meta={"create_time": 1770000000},
    )
    async_db_session.add(video)
    await async_db_session.commit()

    inbox = await get_inbox_videos(
        user_id=user.id,
        status=None,
        limit=20,
        offset=0,
        db=async_db_session,
        auth_user_id=user.id,
    )
    listed = await list_videos(
        user_id=user.id,
        creator_id=None,
        status=None,
        limit=20,
        offset=0,
        db=async_db_session,
        auth_user_id=user.id,
    )
    by_ids = await get_videos_by_ids(
        video_ids=str(video.id),
        user_id=user.id,
        db=async_db_session,
        auth_user_id=user.id,
    )

    assert inbox["videos"][0]["create_time"] == 1770000000
    assert inbox["videos"][0]["published_at"].startswith("2026-02-")
    assert listed[0]["create_time"] == 1770000000
    assert listed[0]["meta"] == {"create_time": 1770000000}
    assert by_ids[0]["create_time"] == 1770000000


@pytest.mark.unit
@pytest.mark.asyncio
async def test_unskip_clears_stale_task_link(async_db_session):
    user = User(
        telegram_id=93002,
        username="unskip_user",
        role="user",
        is_blocked=False,
    )
    async_db_session.add(user)
    await async_db_session.flush()

    creator = CreatorSource(
        user_id=user.id,
        username="unskip_creator",
        enabled=True,
        interval_minutes=30,
        mode="manual_review",
    )
    async_db_session.add(creator)
    await async_db_session.flush()

    task = Task(
        user_id=user.id,
        status="error",
        main_comment="Main",
        video_urls=["https://www.tiktok.com/@unskip_creator/video/1"],
        settings={},
    )
    async_db_session.add(task)
    await async_db_session.flush()

    video = CreatorVideo(
        creator_id=creator.id,
        video_id="1",
        url="https://www.tiktok.com/@unskip_creator/video/1",
        status="skipped",
        task_id=task.id,
        meta={"last_error": "old task failed"},
    )
    async_db_session.add(video)
    await async_db_session.commit()

    result = await unskip_video(
        video_id=video.id,
        user_id=user.id,
        db=async_db_session,
        auth_user_id=user.id,
    )
    await async_db_session.refresh(video)

    assert result["previous_task_id"] == task.id
    assert video.status == "new"
    assert video.task_id is None
    assert video.meta["unskipped_from_task_id"] == task.id
    assert video.meta["unskipped_at"]


@pytest.mark.unit
@pytest.mark.asyncio
async def test_skip_all_new_videos_marks_only_current_user_new_videos(async_db_session):
    user = User(
        telegram_id=93004,
        username="skip_all_user",
        role="user",
        is_blocked=False,
    )
    other_user = User(
        telegram_id=93005,
        username="skip_all_other",
        role="user",
        is_blocked=False,
    )
    async_db_session.add_all([user, other_user])
    await async_db_session.flush()

    creator = CreatorSource(
        user_id=user.id,
        username="skip_all_creator",
        enabled=True,
        interval_minutes=30,
        mode="manual_review",
    )
    other_creator = CreatorSource(
        user_id=other_user.id,
        username="skip_all_other_creator",
        enabled=True,
        interval_minutes=30,
        mode="manual_review",
    )
    async_db_session.add_all([creator, other_creator])
    await async_db_session.flush()

    new_video_a = CreatorVideo(creator_id=creator.id, video_id="1", url="u1", status="new")
    new_video_b = CreatorVideo(creator_id=creator.id, video_id="2", url="u2", status="new")
    done_video = CreatorVideo(creator_id=creator.id, video_id="3", url="u3", status="done")
    other_video = CreatorVideo(creator_id=other_creator.id, video_id="4", url="u4", status="new")
    async_db_session.add_all([new_video_a, new_video_b, done_video, other_video])
    await async_db_session.commit()

    result = await skip_all_new_videos(
        user_id=user.id,
        db=async_db_session,
        auth_user_id=user.id,
    )
    await async_db_session.refresh(new_video_a)
    await async_db_session.refresh(new_video_b)
    await async_db_session.refresh(done_video)
    await async_db_session.refresh(other_video)

    assert result == {"ok": True, "skipped": 2}
    assert new_video_a.status == "skipped"
    assert new_video_b.status == "skipped"
    assert done_video.status == "done"
    assert other_video.status == "new"


@pytest.mark.unit
@pytest.mark.asyncio
async def test_reset_failed_video_to_new_clears_task_link(async_db_session):
    user = User(
        telegram_id=93003,
        username="retry_user",
        role="user",
        is_blocked=False,
    )
    async_db_session.add(user)
    await async_db_session.flush()

    creator = CreatorSource(
        user_id=user.id,
        username="retry_creator",
        enabled=True,
        interval_minutes=30,
        mode="manual_review",
    )
    async_db_session.add(creator)
    await async_db_session.flush()

    task = Task(
        user_id=user.id,
        status="error",
        main_comment="Main",
        video_urls=["https://www.tiktok.com/@retry_creator/video/1"],
        settings={},
    )
    async_db_session.add(task)
    await async_db_session.flush()

    video = CreatorVideo(
        creator_id=creator.id,
        video_id="1",
        url="https://www.tiktok.com/@retry_creator/video/1",
        status="failed",
        task_id=task.id,
        meta={"last_error": "failed"},
    )
    async_db_session.add(video)
    await async_db_session.commit()

    result = await reset_video_to_new(
        video_id=video.id,
        user_id=user.id,
        db=async_db_session,
        auth_user_id=user.id,
    )
    await async_db_session.refresh(video)

    assert result["previous_task_id"] == task.id
    assert video.status == "new"
    assert video.task_id is None
    assert video.meta["retry_of_task_id"] == task.id
    assert video.meta["reset_to_new_at"]
