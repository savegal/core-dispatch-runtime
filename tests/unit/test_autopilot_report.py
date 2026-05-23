import pytest

from api import video_parsing
from api.autopilot import report as autopilot_report_api
from decimal import Decimal
from sqlalchemy import select

from db.models import CreatorSource, CreatorVideo, MonitorOutbox, MonitoredBlogger, Task, User


def test_legacy_autopilot_endpoints_are_marked_deprecated():
    deprecated_paths = {
        route.path
        for route in video_parsing.router.routes
        if getattr(route, "deprecated", False)
    }

    assert any(path.endswith("/autopilot-report") for path in deprecated_paths)
    assert any(path.endswith("/autopilot-control") for path in deprecated_paths)
    assert any(path.endswith("/autopilot-live-queue") for path in deprecated_paths)


@pytest.mark.unit
@pytest.mark.asyncio
async def test_autopilot_report_returns_video_links_and_paginates(async_db_session):
    user = User(
        telegram_id=92001,
        username="autopilot_report_user",
        role="user",
        balance=1000,
        is_blocked=False,
    )
    other_user = User(
        telegram_id=92002,
        username="autopilot_report_other",
        role="user",
        balance=1000,
        is_blocked=False,
    )
    async_db_session.add_all([user, other_user])
    await async_db_session.flush()

    creator = CreatorSource(
        user_id=user.id,
        username="report_creator",
        enabled=True,
        interval_minutes=30,
        mode="auto_comment",
    )
    async_db_session.add(creator)
    await async_db_session.flush()

    old_task = Task(
        user_id=user.id,
        status="pending",
        main_comment="main one",
        video_urls=["https://www.tiktok.com/@report_creator/video/111"],
        settings={
            "origin": "autopilot",
            "creator_username": "report_creator",
            "creator_source_id": creator.id,
            "template_name": "Auto template",
        },
    )
    new_task = Task(
        user_id=user.id,
        status="done",
        main_comment="main two",
        video_urls=["https://www.tiktok.com/@report_creator/video/222"],
        settings={
            "origin": "autopilot",
            "creator_username": "report_creator",
            "creator_source_id": creator.id,
            "template_name": "Auto template",
        },
    )
    manual_task = Task(
        user_id=user.id,
        status="done",
        main_comment="manual",
        video_urls=["https://www.tiktok.com/@report_creator/video/333"],
        settings={"origin": "manual", "creator_username": "report_creator"},
    )
    other_task = Task(
        user_id=other_user.id,
        status="done",
        main_comment="other",
        video_urls=["https://www.tiktok.com/@report_creator/video/444"],
        settings={"origin": "autopilot", "creator_username": "report_creator"},
    )
    async_db_session.add_all([old_task, new_task, manual_task, other_task])
    await async_db_session.flush()

    async_db_session.add_all(
        [
            CreatorVideo(
                creator_id=creator.id,
                video_id="111",
                url="https://www.tiktok.com/@report_creator/video/111",
                status="queued",
                task_id=old_task.id,
            ),
            CreatorVideo(
                creator_id=creator.id,
                video_id="222",
                url="https://www.tiktok.com/@report_creator/video/222",
                status="done",
                task_id=new_task.id,
            ),
        ]
    )
    await async_db_session.commit()

    page_one = await video_parsing.get_autopilot_report(
        user_id=user.id,
        limit=1,
        offset=0,
        db=async_db_session,
        auth_user_id=user.id,
    )

    assert page_one["total"] == 2
    assert page_one["pages"] == 2
    assert page_one["status_counts"] == {"done": 1, "pending": 1}
    assert page_one["items"][0]["task_id"] == new_task.id
    assert page_one["items"][0]["videos"][0]["url"].endswith("/222")
    assert page_one["items"][0]["template_name"] == "Auto template"

    page_two = await video_parsing.get_autopilot_report(
        user_id=user.id,
        limit=1,
        offset=1,
        db=async_db_session,
        auth_user_id=user.id,
    )

    assert page_two["items"][0]["task_id"] == old_task.id
    assert page_two["items"][0]["videos"][0]["video_id"] == "111"
    assert manual_task.id not in {item["task_id"] for item in page_one["items"] + page_two["items"]}


@pytest.mark.unit
@pytest.mark.asyncio
async def test_new_autopilot_report_endpoints_return_task_card(async_db_session):
    user = User(
        telegram_id=92011,
        username="ap_new_endpoint_user",
        role="user",
        balance=1000,
        is_blocked=False,
    )
    async_db_session.add(user)
    await async_db_session.flush()

    task = Task(
        user_id=user.id,
        status="done",
        main_comment="main",
        video_urls=["https://www.tiktok.com/@endpoint/video/1"],
        settings={"origin": "autopilot", "creator_username": "endpoint", "template_name": "Auto endpoint"},
        total_cost=Decimal("3.20"),
    )
    async_db_session.add(task)
    await async_db_session.commit()

    summary = await autopilot_report_api.report_summary(
        user_id=user.id,
        period="24h",
        db=async_db_session,
        auth_user_id=user.id,
    )
    tasks = await autopilot_report_api.report_tasks(
        user_id=user.id,
        period="24h",
        status="done",
        page=0,
        db=async_db_session,
        auth_user_id=user.id,
    )
    card = await autopilot_report_api.report_task(
        task.id,
        user_id=user.id,
        db=async_db_session,
        auth_user_id=user.id,
    )

    assert summary["total"] == 1
    assert tasks["total"] == 1
    assert tasks["items"][0]["task_id"] == task.id
    assert card["template_name"] == "Auto endpoint"


@pytest.mark.unit
@pytest.mark.asyncio
async def test_cancel_autopilot_work_cancels_pending_tasks_outbox_and_releases_videos(monkeypatch, async_db_session):
    monkeypatch.setattr(video_parsing, "remove_tasks_from_queue", lambda task_ids: len(task_ids))

    user = User(
        telegram_id=92101,
        username="autopilot_cancel_user",
        role="user",
        balance=Decimal("100.00"),
        is_blocked=False,
    )
    async_db_session.add(user)
    await async_db_session.flush()

    creator = CreatorSource(
        user_id=user.id,
        username="cancel_creator",
        enabled=True,
        interval_minutes=30,
        mode="auto_comment",
    )
    async_db_session.add(creator)
    await async_db_session.flush()

    task = Task(
        user_id=user.id,
        status="pending",
        main_comment="main",
        video_urls=["https://www.tiktok.com/@cancel_creator/video/555"],
        settings={
            "origin": "autopilot",
            "creator_username": "cancel_creator",
            "creator_source_id": creator.id,
        },
        progress=0,
        attempts=0,
        total_cost=Decimal("7.00"),
        refunded_cost=None,
    )
    other_task = Task(
        user_id=user.id,
        status="pending",
        main_comment="main",
        video_urls=["https://www.tiktok.com/@other/video/666"],
        settings={"origin": "manual", "creator_source_id": creator.id},
        progress=0,
        attempts=0,
        total_cost=Decimal("7.00"),
    )
    async_db_session.add_all([task, other_task])
    await async_db_session.flush()

    video = CreatorVideo(
        creator_id=creator.id,
        video_id="555",
        url="https://www.tiktok.com/@cancel_creator/video/555",
        status="queued",
        task_id=task.id,
    )
    async_db_session.add(video)
    await async_db_session.flush()
    outbox = MonitorOutbox(
        event_type="autopilot_create",
        creator_id=creator.id,
        payload={"creator_id": creator.id, "video_ids": [video.id]},
        status="pending",
        dedupe_key="cancel-autopilot-outbox",
    )
    processing_outbox = MonitorOutbox(
        event_type="autopilot_create",
        creator_id=creator.id,
        payload={"creator_id": creator.id, "video_ids": [video.id]},
        status="processing",
        lock_owner="monitor-worker",
        dedupe_key="cancel-autopilot-processing-outbox",
    )
    processing_notice = MonitorOutbox(
        event_type="notify_auto_orders_created",
        creator_id=creator.id,
        payload={"creator_id": creator.id, "video_ids": [video.id], "task_ids": [task.id]},
        status="processing",
        lock_owner="monitor-worker",
        dedupe_key="cancel-autopilot-processing-notice",
    )
    async_db_session.add_all([outbox, processing_outbox, processing_notice])
    await async_db_session.commit()

    result = await video_parsing.cancel_autopilot_work(
        video_parsing.AutopilotCancelRequest(creator_id=creator.id),
        user_id=user.id,
        db=async_db_session,
        auth_user_id=user.id,
    )

    await async_db_session.refresh(task)
    await async_db_session.refresh(other_task)
    await async_db_session.refresh(video)
    await async_db_session.refresh(outbox)
    await async_db_session.refresh(processing_outbox)
    await async_db_session.refresh(processing_notice)
    await async_db_session.refresh(user)

    assert result["cancelled_pending_tasks"] == 1
    assert result["cancelled_outbox_events"] == 2
    assert result["cancelled_notifications"] == 1
    assert result["released_videos"] == 1
    assert result["removed_from_redis_queue"] == 1
    assert result["refunded_amount"] == 7.0
    assert task.status == "stopped"
    assert task.refunded_cost == Decimal("7.00")
    assert other_task.status == "pending"
    assert video.status == "new"
    assert video.task_id is None
    assert outbox.status == "cancelled"
    assert processing_outbox.status == "cancelled"
    assert processing_outbox.lock_owner is None
    assert processing_notice.status == "cancelled"
    assert processing_notice.lock_owner is None
    assert user.balance == Decimal("107.00")


@pytest.mark.unit
@pytest.mark.asyncio
async def test_autopilot_control_update_persists_mode_and_limits(async_db_session):
    user = User(
        telegram_id=92201,
        username="autopilot_control_user",
        role="user",
        balance=1000,
        is_blocked=False,
    )
    async_db_session.add(user)
    await async_db_session.commit()

    result = await video_parsing.update_autopilot_control(
        video_parsing.AutopilotControlUpdate(
            mode="paused",
            max_create_per_tick=5,
            max_create_per_creator=1,
            max_create_per_hour=20,
        ),
        user_id=user.id,
        db=async_db_session,
        auth_user_id=user.id,
    )

    assert result["mode"] == "paused"
    assert result["limits"] == {
        "max_create_per_tick": 5,
        "max_create_per_creator": 1,
        "max_create_per_hour": 20,
    }

    result = await video_parsing.update_autopilot_control(
        video_parsing.AutopilotControlUpdate(mode="active"),
        user_id=user.id,
        db=async_db_session,
        auth_user_id=user.id,
    )

    assert result["mode"] == "active"
    assert result["limits"]["max_create_per_tick"] == 5


@pytest.mark.unit
@pytest.mark.asyncio
async def test_autopilot_control_active_requeues_existing_new_auto_videos(async_db_session):
    user = User(
        telegram_id=92208,
        username="autopilot_requeue_user",
        role="user",
        balance=1000,
        is_blocked=False,
    )
    blogger = MonitoredBlogger(username="autopilot_requeue_creator")
    async_db_session.add_all([user, blogger])
    await async_db_session.flush()

    creator = CreatorSource(
        user_id=user.id,
        username="autopilot_requeue_creator",
        monitored_blogger_id=blogger.id,
        enabled=True,
        interval_minutes=30,
        mode="auto_comment",
        default_template_id=123,
    )
    manual_creator = CreatorSource(
        user_id=user.id,
        username="autopilot_requeue_manual",
        monitored_blogger_id=blogger.id,
        enabled=True,
        interval_minutes=30,
        mode="manual_review",
        default_template_id=123,
    )
    async_db_session.add_all([creator, manual_creator])
    await async_db_session.flush()

    auto_video = CreatorVideo(creator_id=creator.id, video_id="auto-backlog", url="auto-url", status="new")
    manual_video = CreatorVideo(creator_id=manual_creator.id, video_id="manual-backlog", url="manual-url", status="new")
    async_db_session.add_all([auto_video, manual_video])
    await async_db_session.commit()

    result = await video_parsing.update_autopilot_control(
        video_parsing.AutopilotControlUpdate(mode="active"),
        user_id=user.id,
        db=async_db_session,
        auth_user_id=user.id,
    )

    assert result["mode"] == "active"
    assert result["queued_existing_new_video_events"] == 1
    assert result["queued_existing_new_video_count"] == 1

    outbox_rows = await async_db_session.execute(select(MonitorOutbox).order_by(MonitorOutbox.id.asc()))
    outbox = outbox_rows.scalar_one()
    assert outbox.event_type == "autopilot_create"
    assert outbox.creator_id == creator.id
    assert outbox.payload["video_ids"] == [auto_video.id]


@pytest.mark.unit
@pytest.mark.asyncio
async def test_autopilot_report_cleanup_archives_terminal_rows(async_db_session):
    user = User(
        telegram_id=92202,
        username="autopilot_cleanup_user",
        role="user",
        balance=1000,
        is_blocked=False,
    )
    async_db_session.add(user)
    await async_db_session.flush()

    done_task = Task(
        user_id=user.id,
        status="done",
        main_comment="done",
        video_urls=["https://www.tiktok.com/@cleanup/video/1"],
        settings={"origin": "autopilot", "creator_username": "cleanup"},
    )
    pending_task = Task(
        user_id=user.id,
        status="pending",
        main_comment="pending",
        video_urls=["https://www.tiktok.com/@cleanup/video/2"],
        settings={"origin": "autopilot", "creator_username": "cleanup"},
    )
    async_db_session.add_all([done_task, pending_task])
    await async_db_session.commit()

    result = await video_parsing.cleanup_autopilot_report(
        video_parsing.AutopilotReportCleanupRequest(),
        user_id=user.id,
        db=async_db_session,
        auth_user_id=user.id,
    )

    assert result["archived_tasks"] == 1

    visible = await video_parsing.get_autopilot_report(
        user_id=user.id,
        limit=10,
        offset=0,
        db=async_db_session,
        auth_user_id=user.id,
    )
    assert visible["total"] == 1
    assert visible["items"][0]["task_id"] == pending_task.id

    archived = await video_parsing.get_autopilot_report(
        user_id=user.id,
        limit=10,
        offset=0,
        include_archived=True,
        db=async_db_session,
        auth_user_id=user.id,
    )
    assert archived["total"] == 2
    assert done_task.id in {item["task_id"] for item in archived["items"]}


@pytest.mark.unit
@pytest.mark.asyncio
async def test_skip_autopilot_new_videos_marks_only_unqueued_new_rows(async_db_session):
    user = User(
        telegram_id=92203,
        username="autopilot_skip_user",
        role="user",
        balance=1000,
        is_blocked=False,
    )
    other_user = User(
        telegram_id=92204,
        username="autopilot_skip_other",
        role="user",
        balance=1000,
        is_blocked=False,
    )
    async_db_session.add_all([user, other_user])
    await async_db_session.flush()

    creator = CreatorSource(user_id=user.id, username="skip_creator", enabled=True, mode="auto_comment")
    manual_creator = CreatorSource(user_id=user.id, username="manual_skip_creator", enabled=True, mode="manual_review")
    other_creator = CreatorSource(user_id=other_user.id, username="skip_other", enabled=True, mode="auto_comment")
    async_db_session.add_all([creator, manual_creator, other_creator])
    await async_db_session.flush()

    new_video = CreatorVideo(creator_id=creator.id, video_id="new-1", url="u1", status="new")
    queued_video = CreatorVideo(creator_id=creator.id, video_id="queued-1", url="u2", status="new", task_id=123)
    manual_video = CreatorVideo(creator_id=manual_creator.id, video_id="manual-1", url="u4", status="new")
    other_video = CreatorVideo(creator_id=other_creator.id, video_id="other-1", url="u3", status="new")
    async_db_session.add_all([new_video, queued_video, manual_video, other_video])
    await async_db_session.commit()

    result = await video_parsing.skip_autopilot_new_videos(
        video_parsing.AutopilotSkipNewVideosRequest(),
        user_id=user.id,
        db=async_db_session,
        auth_user_id=user.id,
    )

    await async_db_session.refresh(new_video)
    await async_db_session.refresh(queued_video)
    await async_db_session.refresh(manual_video)
    await async_db_session.refresh(other_video)

    assert result["skipped_videos"] == 1
    assert new_video.status == "skipped"
    assert new_video.meta["autopilot_skipped_by"] == "autopilot_control"
    assert queued_video.status == "new"
    assert manual_video.status == "new"
    assert other_video.status == "new"


@pytest.mark.unit
@pytest.mark.asyncio
async def test_autopilot_live_queue_reports_stale_redis_ids(monkeypatch, async_db_session):
    user = User(
        telegram_id=92205,
        username="autopilot_queue_user",
        role="user",
        balance=1000,
        is_blocked=False,
    )
    other_user = User(
        telegram_id=92206,
        username="autopilot_queue_other",
        role="user",
        balance=1000,
        is_blocked=False,
    )
    async_db_session.add_all([user, other_user])
    await async_db_session.flush()

    pending_task = Task(
        user_id=user.id,
        status="pending",
        main_comment="p",
        video_urls=["https://www.tiktok.com/@queue/video/1"],
        settings={"origin": "autopilot"},
    )
    done_task = Task(
        user_id=user.id,
        status="done",
        main_comment="d",
        video_urls=["https://www.tiktok.com/@queue/video/2"],
        settings={"origin": "autopilot"},
    )
    other_task = Task(
        user_id=other_user.id,
        status="pending",
        main_comment="o",
        video_urls=["https://www.tiktok.com/@queue/video/3"],
        settings={"origin": "autopilot"},
    )
    async_db_session.add_all([pending_task, done_task, other_task])
    await async_db_session.commit()

    class FakeRedis:
        def llen(self, _key):
            return 4

        def lrange(self, _key, _start, _end):
            return [
                str(pending_task.id).encode(),
                str(done_task.id).encode(),
                str(other_task.id).encode(),
                b"999999",
            ]

    monkeypatch.setattr(video_parsing, "get_redis_client", lambda: FakeRedis())
    monkeypatch.setattr(video_parsing, "get_executor_heartbeat", lambda: (True, {"status": "busy", "active_task_id": pending_task.id}))
    monkeypatch.setattr(
        video_parsing,
        "executor_task_activity_snapshot",
        lambda: (True, {"lock_present": True, "heartbeat_status": "busy"}),
    )

    state = await video_parsing.get_autopilot_live_queue(
        user_id=user.id,
        db=async_db_session,
        auth_user_id=user.id,
    )

    assert state["redis"]["queue_len"] == 4
    assert state["current"]["executor"]["active"] is True
    assert state["current"]["executor"]["active_task"]["task_id"] == pending_task.id
    assert [item["task_id"] for item in state["current"]["queue_head"]] == [pending_task.id]
    assert pending_task.id not in state["redis"]["stale_task_ids"]
    assert done_task.id in state["redis"]["stale_task_ids"]
    assert other_task.id in state["redis"]["stale_task_ids"]
    assert 999999 in state["redis"]["stale_task_ids"]


@pytest.mark.unit
@pytest.mark.asyncio
async def test_autopilot_live_queue_ignores_archived_report_rows(monkeypatch, async_db_session):
    user = User(
        telegram_id=92207,
        username="autopilot_queue_archived_user",
        role="user",
        balance=1000,
        is_blocked=False,
    )
    async_db_session.add(user)
    await async_db_session.flush()

    creator = CreatorSource(user_id=user.id, username="queue_archived", enabled=True, mode="auto_comment")
    manual_creator = CreatorSource(user_id=user.id, username="queue_manual", enabled=True, mode="manual_review")
    async_db_session.add_all([creator, manual_creator])
    await async_db_session.flush()

    active_task = Task(
        user_id=user.id,
        status="pending",
        main_comment="active",
        video_urls=["https://www.tiktok.com/@queue/video/active"],
        settings={"origin": "autopilot", "creator_username": "queue"},
    )
    archived_task = Task(
        user_id=user.id,
        status="pending",
        main_comment="archived",
        video_urls=["https://www.tiktok.com/@queue/video/archived"],
        settings={"origin": "autopilot_archived", "creator_username": "queue"},
    )
    pending_outbox = MonitorOutbox(
        event_type="autopilot_create",
        creator_id=creator.id,
        status="pending",
        payload={"creator_id": creator.id, "video_ids": [1]},
        dedupe_key="live-queue-pending-outbox",
    )
    done_outbox = MonitorOutbox(
        event_type="autopilot_create",
        creator_id=creator.id,
        status="done",
        payload={"creator_id": creator.id, "video_ids": [2]},
        dedupe_key="live-queue-done-outbox",
    )
    auto_video = CreatorVideo(creator_id=creator.id, video_id="auto-new", url="auto-url", status="new")
    manual_video = CreatorVideo(creator_id=manual_creator.id, video_id="manual-new", url="manual-url", status="new")
    async_db_session.add_all([active_task, archived_task, pending_outbox, done_outbox, auto_video, manual_video])
    await async_db_session.commit()

    class FakeRedis:
        def llen(self, _key):
            return 2

        def lrange(self, _key, _start, _end):
            return [str(active_task.id).encode(), str(archived_task.id).encode()]

    monkeypatch.setattr(video_parsing, "get_redis_client", lambda: FakeRedis())
    monkeypatch.setattr(video_parsing, "get_executor_heartbeat", lambda: (False, {"reason": "missing"}))
    monkeypatch.setattr(
        video_parsing,
        "executor_task_activity_snapshot",
        lambda: (False, {"lock_present": False, "heartbeat_alive": False}),
    )

    state = await video_parsing.get_autopilot_live_queue(
        user_id=user.id,
        db=async_db_session,
        auth_user_id=user.id,
    )

    assert state["tasks"]["status_counts"] == {"pending": 1}
    assert state["outbox"]["status_counts"] == {"pending": 1}
    assert state["videos"]["new"] == 1
    assert [item["task_id"] for item in state["tasks"]["preview"]] == [active_task.id]
    assert active_task.id not in state["redis"]["stale_task_ids"]
    assert archived_task.id in state["redis"]["stale_task_ids"]
