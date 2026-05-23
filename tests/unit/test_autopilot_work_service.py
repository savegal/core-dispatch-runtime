from decimal import Decimal

import pytest
from sqlalchemy import select

from db.models import CreatorSource, CreatorVideo, MonitorOutbox, Task, User
from services.autopilot.actions import cancel_planned_video, stop_work_task
from services.autopilot.work import collect_work, get_planned_task_preview, get_planned_video_preview


@pytest.mark.unit
@pytest.mark.asyncio
async def test_collect_work_sections_show_plan_run_and_done(async_db_session):
    user = User(telegram_id=97001, username="ap_work", role="user", balance=Decimal("10.00"), is_blocked=False)
    async_db_session.add(user)
    await async_db_session.flush()
    creator = CreatorSource(user_id=user.id, username="work_blog", enabled=True, mode="auto_comment")
    async_db_session.add(creator)
    await async_db_session.flush()

    pending_task = Task(
        user_id=user.id,
        status="pending",
        main_comment="main",
        video_urls=["https://www.tiktok.com/@work_blog/video/1"],
        settings={"origin": "autopilot", "creator_username": "work_blog", "creator_source_id": creator.id},
        total_cost=Decimal("3.00"),
    )
    running_task = Task(
        user_id=user.id,
        status="in_progress",
        main_comment="main",
        video_urls=["https://www.tiktok.com/@work_blog/video/2"],
        settings={"origin": "autopilot", "creator_username": "work_blog", "creator_source_id": creator.id},
        total_cost=Decimal("3.00"),
    )
    done_task = Task(
        user_id=user.id,
        status="done",
        main_comment="main",
        video_urls=["https://www.tiktok.com/@work_blog/video/3"],
        settings={
            "origin": "autopilot",
            "creator_username": "work_blog",
            "creator_source_id": creator.id,
            "url_results": {
                "https://www.tiktok.com/@work_blog/video/3": {
                    "engagement": {"main": {"expected": 1, "comment_id": "m1"}, "replies": {"ok": 0, "total": 0}}
                }
            },
        },
        total_cost=Decimal("3.00"),
    )
    async_db_session.add_all([pending_task, running_task, done_task])
    await async_db_session.flush()
    queued_video = CreatorVideo(
        creator_id=creator.id,
        video_id="1",
        url="https://www.tiktok.com/@work_blog/video/1",
        status="queued",
        task_id=pending_task.id,
    )
    new_video = CreatorVideo(
        creator_id=creator.id,
        video_id="4",
        url="https://www.tiktok.com/@work_blog/video/4",
        status="new",
    )
    async_db_session.add_all([queued_video, new_video])
    await async_db_session.flush()
    async_db_session.add(
        MonitorOutbox(
            event_type="autopilot_create",
            status="pending",
            creator_id=creator.id,
            payload={"creator_id": creator.id, "video_ids": [new_video.id]},
        )
    )
    await async_db_session.commit()

    planned = await collect_work(async_db_session, user.id, tab="plan")
    running = await collect_work(async_db_session, user.id, tab="run")
    done = await collect_work(async_db_session, user.id, tab="done")

    assert planned["total"] == 2
    assert planned["items"][0]["task_id"] == pending_task.id
    assert planned["items"][1]["video_id"] == new_video.id
    assert running["total"] == 1
    assert running["items"][0]["stage"]
    assert done["total"] == 1
    assert done["items"][0]["task_id"] == done_task.id

    video_preview = await get_planned_video_preview(async_db_session, user.id, queued_video.id)
    task_preview = await get_planned_task_preview(async_db_session, user.id, pending_task.id)
    assert video_preview["url"].endswith("/1")
    assert video_preview["refund_preview"] == 3.0
    assert task_preview["task_id"] == pending_task.id
    assert task_preview["refund_preview"] == 3.0


@pytest.mark.unit
@pytest.mark.asyncio
async def test_collect_processed_work_paginates_all_rows_without_hidden_cap(async_db_session):
    user = User(telegram_id=97004, username="ap_done_many", role="user", balance=Decimal("1.00"), is_blocked=False)
    async_db_session.add(user)
    await async_db_session.flush()
    tasks = [
        Task(
            user_id=user.id,
            status="done",
            main_comment="main",
            video_urls=[f"https://www.tiktok.com/@many/video/{index}"],
            settings={"origin": "autopilot", "creator_username": "many"},
            total_cost=Decimal("1.00"),
        )
        for index in range(505)
    ]
    async_db_session.add_all(tasks)
    await async_db_session.commit()

    first_page = await collect_work(async_db_session, user.id, tab="done", page=0, limit=10)
    last_page = await collect_work(async_db_session, user.id, tab="done", page=50, limit=10)

    assert first_page["total"] == 505
    assert first_page["pages"] == 51
    assert len(first_page["items"]) == 10
    assert last_page["page"] == 50
    assert len(last_page["items"]) == 5


@pytest.mark.unit
@pytest.mark.asyncio
async def test_cancel_planned_video_stops_pending_task_and_skips_video(async_db_session, monkeypatch):
    monkeypatch.setattr("services.autopilot.actions.remove_tasks_from_queue", lambda task_ids: len(task_ids))
    user = User(telegram_id=97002, username="ap_cancel_video", role="user", balance=Decimal("1.00"), is_blocked=False)
    async_db_session.add(user)
    await async_db_session.flush()
    creator = CreatorSource(user_id=user.id, username="cancel_blog", enabled=True, mode="auto_comment")
    async_db_session.add(creator)
    await async_db_session.flush()
    task = Task(
        user_id=user.id,
        status="pending",
        main_comment="main",
        video_urls=["https://www.tiktok.com/@cancel_blog/video/1"],
        settings={"origin": "autopilot", "creator_username": "cancel_blog", "creator_source_id": creator.id},
        total_cost=Decimal("4.00"),
    )
    async_db_session.add(task)
    await async_db_session.flush()
    video = CreatorVideo(
        creator_id=creator.id,
        video_id="1",
        url="https://www.tiktok.com/@cancel_blog/video/1",
        status="queued",
        task_id=task.id,
    )
    async_db_session.add(video)
    await async_db_session.commit()

    result = await cancel_planned_video(async_db_session, user.id, video.id)

    assert result["success"] is True
    assert result["details"]["refunded_amount"] == 4.0
    updated_task = (await async_db_session.execute(select(Task).where(Task.id == task.id))).scalar_one()
    updated_video = (await async_db_session.execute(select(CreatorVideo).where(CreatorVideo.id == video.id))).scalar_one()
    updated_user = (await async_db_session.execute(select(User).where(User.id == user.id))).scalar_one()
    assert updated_task.status == "stopped"
    assert updated_video.status == "skipped"
    assert updated_video.task_id is None
    assert updated_user.balance == Decimal("5.00")


@pytest.mark.unit
@pytest.mark.asyncio
async def test_stop_work_task_marks_running_task_stopped(async_db_session, monkeypatch):
    monkeypatch.setattr("services.autopilot.actions.request_task_stop", lambda task_id: True)
    user = User(telegram_id=97003, username="ap_stop_work", role="user", balance=Decimal("1.00"), is_blocked=False)
    async_db_session.add(user)
    await async_db_session.flush()
    task = Task(
        user_id=user.id,
        status="in_progress",
        main_comment="main",
        video_urls=["https://www.tiktok.com/@stop_blog/video/1"],
        settings={"origin": "autopilot", "creator_username": "stop_blog"},
        total_cost=Decimal("4.00"),
    )
    async_db_session.add(task)
    await async_db_session.commit()

    result = await stop_work_task(async_db_session, user.id, task.id)

    assert result["success"] is True
    updated = (await async_db_session.execute(select(Task).where(Task.id == task.id))).scalar_one()
    assert updated.status == "stopped"
    assert updated.settings["force_close_browsers"] is True
