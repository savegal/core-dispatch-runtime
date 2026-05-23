from contextlib import contextmanager
from decimal import Decimal
from types import SimpleNamespace

import pytest
from sqlalchemy import select

import api.tasks as task_api
import services.video_monitor_worker as monitor_worker
from api.tasks import TaskCreate
from api.video_parsing import LinkVideosToTasksRequest, VideoTaskLink, link_videos_to_tasks
from db.models import CreatorSource, CreatorVideo, MonitorOutbox, MonitoredBlogger, Task, TaskTemplate, User
from services.task_creation_service import create_monitored_tasks_batch_sync


def _task_data(user_id: int) -> TaskCreate:
    return TaskCreate(
        user_id=user_id,
        main_comment="Main one",
        text_variants=["Main one", "Main two", "Main three"],
        video_urls=["https://www.tiktok.com/@demo/video/1234567890123456789"],
        settings={"origin": "test"},
    )


@pytest.mark.unit
@pytest.mark.asyncio
async def test_create_task_reuses_terminal_task_for_same_idempotency_key(async_db_session, monkeypatch):
    user = User(
        telegram_id=880001,
        username="idem_user",
        role="user",
        balance=Decimal("100.00"),
        is_blocked=False,
    )
    async_db_session.add(user)
    await async_db_session.commit()
    await async_db_session.refresh(user)

    async def _noop_finalize(*args, **kwargs):
        return True

    monkeypatch.setattr(task_api.settings, "DEMO_MODE", True, raising=False)
    monkeypatch.setattr(task_api, "claim_dedup_key", lambda *args, **kwargs: True)
    monkeypatch.setattr(task_api, "finalize_reserved_enqueue_async", _noop_finalize)

    request = SimpleNamespace(headers={"X-Idempotency-Key": "same-client-click"})
    create_task = task_api.create_task.__wrapped__

    first = await create_task(
        request=request,
        task_data=_task_data(user.id),
        db=async_db_session,
        auth_user_id=user.id,
    )

    task = (
        await async_db_session.execute(select(Task).filter(Task.id == first["task_id"]))
    ).scalar_one()
    task.status = "done"
    await async_db_session.commit()

    second = await create_task(
        request=request,
        task_data=_task_data(user.id),
        db=async_db_session,
        auth_user_id=user.id,
    )

    assert second["task_id"] == first["task_id"]
    assert second["duplicate"] is True
    assert second["idempotency_reuse"] is True


@pytest.mark.unit
@pytest.mark.asyncio
async def test_engagement_only_dedup_uses_author_when_both_sides_have_one(async_db_session, monkeypatch):
    user = User(
        telegram_id=880005,
        username="engagement_dedup_user",
        role="user",
        balance=Decimal("100.00"),
        is_blocked=False,
    )
    existing = Task(
        user=user,
        status="pending",
        main_comment="Target",
        video_urls=["https://www.tiktok.com/@demo/video/9999999999999999999"],
        settings={
            "engagement_username": "alice",
            "engagement_comment_text": "same text",
        },
        kind="engagement_only",
        total_cost=Decimal("0.00"),
    )
    async_db_session.add_all([user, existing])
    await async_db_session.commit()
    await async_db_session.refresh(user)
    await async_db_session.refresh(existing)

    async def _noop_finalize(*args, **kwargs):
        return True

    monkeypatch.setattr(task_api.settings, "DEMO_MODE", True, raising=False)
    monkeypatch.setattr(task_api, "claim_dedup_key", lambda *args, **kwargs: True)
    monkeypatch.setattr(task_api, "finalize_reserved_enqueue_async", _noop_finalize)

    created = await task_api.create_task.__wrapped__(
        request=SimpleNamespace(headers={}),
        task_data=TaskCreate(
            user_id=user.id,
            main_comment="",
            text_variants=["Target", "Target two", "Target three"],
            video_urls=["https://www.tiktok.com/@demo/video/9999999999999999999"],
            settings={"origin": "test"},
            kind="engagement_only",
            engagement_username="bob",
            engagement_comment_text="same text",
        ),
        db=async_db_session,
        auth_user_id=user.id,
    )

    assert created["task_id"] != existing.id


@pytest.mark.unit
def test_monitored_batch_skips_stale_video_with_existing_task(db_session):
    user = User(
        telegram_id=880002,
        username="stale_video_user",
        role="user",
        balance=Decimal("100.00"),
        is_blocked=False,
    )
    creator = CreatorSource(user=user, username="stale_demo", mode="auto_comment", enabled=True)
    template = TaskTemplate(
        user=user,
        name="template",
        main_comment="Main",
        settings={"text_variants": ["Main", "Second", "Third"]},
    )
    existing_task = Task(
        user=user,
        status="pending",
        main_comment="Old",
        video_urls=["https://www.tiktok.com/@stale_demo/video/old"],
        settings={},
        total_cost=Decimal("0.00"),
    )
    db_session.add_all([user, creator, template, existing_task])
    db_session.flush()
    stale_video = CreatorVideo(
        creator_id=creator.id,
        video_id="stale-1",
        url="https://www.tiktok.com/@stale_demo/video/stale-1",
        status="new",
        task_id=existing_task.id,
    )
    db_session.add(stale_video)
    db_session.commit()

    result = create_monitored_tasks_batch_sync(
        db_session,
        creator,
        video_ids=[stale_video.id],
        template=template,
        origin="autopilot",
        created_from="test",
    )

    assert result.status == "skipped"
    assert result.reason == "no_new_videos"
    assert result.created_video_ids == []


@pytest.mark.unit
@pytest.mark.asyncio
async def test_cancelled_autopilot_outbox_is_not_processed(monkeypatch, db_session):
    user = User(
        telegram_id=880006,
        username="cancelled_outbox_user",
        role="user",
        balance=Decimal("100.00"),
        is_blocked=False,
        video_notifications_enabled=True,
    )
    blogger = MonitoredBlogger(username="cancelled_outbox_demo")
    creator = CreatorSource(
        user=user,
        username="cancelled_outbox_demo",
        monitored_blogger=blogger,
        mode="auto_comment",
        enabled=True,
    )
    db_session.add_all([user, blogger, creator])
    db_session.flush()
    video = CreatorVideo(
        creator_id=creator.id,
        video_id="cancelled-outbox-1",
        url="https://www.tiktok.com/@cancelled_outbox_demo/video/cancelled-outbox-1",
        status="new",
    )
    db_session.add(video)
    db_session.flush()
    outbox = MonitorOutbox(
        event_type="autopilot_create",
        status="cancelled",
        lock_owner="monitor-worker",
        blogger_id=blogger.id,
        creator_id=creator.id,
        payload={"creator_id": creator.id, "video_ids": [video.id]},
        dedupe_key="cancelled-autopilot-outbox",
    )
    db_session.add(outbox)
    db_session.commit()

    @contextmanager
    def fake_db_context():
        yield db_session

    def fail_if_called(*args, **kwargs):
        raise AssertionError("cancelled autopilot outbox must not create tasks")

    monkeypatch.setattr(monitor_worker, "get_sync_db_context", fake_db_context)
    monkeypatch.setattr(monitor_worker, "create_monitored_tasks_batch_sync", fail_if_called)

    assert await monitor_worker._process_outbox_entry(outbox.id) is True
    db_session.refresh(outbox)
    db_session.refresh(video)

    assert outbox.status == "cancelled"
    assert outbox.lock_owner is None
    assert video.status == "new"
    assert video.task_id is None


@pytest.mark.unit
@pytest.mark.asyncio
async def test_autopilot_outbox_skips_when_creator_mode_changed(monkeypatch, db_session):
    user = User(
        telegram_id=880003,
        username="mode_changed_user",
        role="user",
        balance=Decimal("100.00"),
        is_blocked=False,
        video_notifications_enabled=True,
    )
    blogger = MonitoredBlogger(username="mode_changed_demo")
    creator = CreatorSource(
        user=user,
        username="mode_changed_demo",
        monitored_blogger=blogger,
        mode="manual_review",
        enabled=True,
    )
    db_session.add_all([user, blogger, creator])
    db_session.flush()
    video = CreatorVideo(
        creator_id=creator.id,
        video_id="mode-change-1",
        url="https://www.tiktok.com/@mode_changed_demo/video/mode-change-1",
        status="new",
    )
    db_session.add(video)
    db_session.flush()
    outbox = MonitorOutbox(
        event_type="autopilot_create",
        status="pending",
        blogger_id=blogger.id,
        creator_id=creator.id,
        payload={"creator_id": creator.id, "video_ids": [video.id]},
        dedupe_key="mode-change-autopilot",
    )
    db_session.add(outbox)
    db_session.commit()

    @contextmanager
    def fake_db_context():
        yield db_session

    def fail_if_called(*args, **kwargs):
        raise AssertionError("autopilot task creation must not run after mode change")

    monkeypatch.setattr(monitor_worker, "get_sync_db_context", fake_db_context)
    monkeypatch.setattr(monitor_worker, "create_monitored_tasks_batch_sync", fail_if_called)

    assert await monitor_worker._process_outbox_entry(outbox.id) is True
    db_session.refresh(outbox)
    db_session.refresh(video)

    assert outbox.status == "done"
    assert video.status == "new"
    review_notice = (
        db_session.query(MonitorOutbox)
        .filter(
            MonitorOutbox.creator_id == creator.id,
            MonitorOutbox.event_type == "notify_pending_review",
        )
        .one()
    )
    assert review_notice.payload["video_ids"] == [video.id]


@pytest.mark.unit
@pytest.mark.asyncio
async def test_link_videos_to_tasks_rejects_relinking_processed_video(async_db_session):
    user = User(
        telegram_id=880004,
        username="link_guard_user",
        role="user",
        balance=Decimal("100.00"),
        is_blocked=False,
    )
    creator = CreatorSource(user=user, username="link_guard_demo", mode="manual_review", enabled=True)
    task_one = Task(
        user=user,
        status="pending",
        main_comment="One",
        video_urls=["https://www.tiktok.com/@link_guard_demo/video/one"],
        settings={},
        total_cost=Decimal("0.00"),
    )
    task_two = Task(
        user=user,
        status="pending",
        main_comment="Two",
        video_urls=["https://www.tiktok.com/@link_guard_demo/video/two"],
        settings={},
        total_cost=Decimal("0.00"),
    )
    async_db_session.add_all([user, creator, task_one, task_two])
    await async_db_session.flush()
    video = CreatorVideo(
        creator_id=creator.id,
        video_id="link-1",
        url="https://www.tiktok.com/@link_guard_demo/video/link-1",
        status="queued",
        task_id=task_one.id,
    )
    async_db_session.add(video)
    await async_db_session.commit()

    request = LinkVideosToTasksRequest(
        links=[VideoTaskLink(video_id=video.id, task_id=task_two.id)]
    )
    with pytest.raises(Exception) as exc_info:
        await link_videos_to_tasks(
            request=request,
            user_id=user.id,
            db=async_db_session,
            auth_user_id=user.id,
        )

    assert getattr(exc_info.value, "status_code", None) == 409
