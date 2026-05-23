from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest

from db.models import CreatorSource, CreatorVideo, MonitorOutbox, MonitoredBlogger, Pricing, Task, TaskTemplate, User
from services.task_creation_service import create_monitored_tasks_batch_sync
from services.video_monitor_worker import (
    _build_due_bloggers_query,
    _get_active_user_ids,
    get_monitor_runtime_snapshot,
)


@pytest.mark.unit
def test_active_monitor_users_are_free_non_blocked_users(db_session):
    now = datetime.now(timezone.utc)
    active_user = User(
        telegram_id=770000,
        username="free_worker",
        role="user",
        is_blocked=False,
    )
    blocked_user = User(
        telegram_id=770099,
        username="blocked_worker",
        role="user",
        is_blocked=True,
    )
    db_session.add_all([active_user, blocked_user])
    db_session.commit()

    active_user_ids = _get_active_user_ids(db_session, now)

    assert active_user.id in active_user_ids
    assert blocked_user.id not in active_user_ids


@pytest.mark.unit
def test_due_bloggers_query_uses_next_check_and_active_users(db_session):
    now = datetime.now(timezone.utc)
    user = User(
        telegram_id=770001,
        username="monitor_owner",
        role="admin",
        balance=Decimal("50.00"),
    )
    db_session.add(user)
    db_session.flush()

    active_sub = CreatorSource(
        user_id=user.id,
        username="due_creator",
        enabled=True,
        interval_minutes=15,
        mode="manual_review",
        monitored_blogger=MonitoredBlogger(
            username="due_blogger",
            next_check_at=now - timedelta(minutes=2),
            last_success_at=now - timedelta(hours=2),
        ),
    )
    future_sub = CreatorSource(
        user_id=user.id,
        username="future_creator",
        enabled=True,
        interval_minutes=15,
        mode="manual_review",
        monitored_blogger=MonitoredBlogger(
            username="future_blogger",
            next_check_at=now + timedelta(minutes=20),
            last_success_at=now - timedelta(hours=1),
        ),
    )
    db_session.add_all([active_sub, future_sub])
    db_session.commit()

    active_user_ids = _get_active_user_ids(db_session, now)
    due_bloggers = _build_due_bloggers_query(db_session, now=now, active_user_ids=active_user_ids).all()

    assert [blogger.username for blogger in due_bloggers] == ["due_blogger"]
    assert due_bloggers[0].subscriptions


@pytest.mark.unit
def test_due_bloggers_query_prefers_oldest_attempt_with_same_due_time(db_session):
    now = datetime.now(timezone.utc)
    user = User(
        telegram_id=770010,
        username="fairness_owner",
        role="admin",
        balance=Decimal("50.00"),
    )
    db_session.add(user)
    db_session.flush()

    same_due_at = now - timedelta(minutes=10)
    older_attempt = CreatorSource(
        user_id=user.id,
        username="older_attempt_creator",
        enabled=True,
        interval_minutes=15,
        mode="manual_review",
        monitored_blogger=MonitoredBlogger(
            username="older_attempt",
            next_check_at=same_due_at,
            last_attempt_at=now - timedelta(hours=3),
            last_success_at=now - timedelta(days=1),
        ),
    )
    newer_attempt = CreatorSource(
        user_id=user.id,
        username="newer_attempt_creator",
        enabled=True,
        interval_minutes=15,
        mode="manual_review",
        monitored_blogger=MonitoredBlogger(
            username="newer_attempt",
            next_check_at=same_due_at,
            last_attempt_at=now - timedelta(minutes=20),
            last_success_at=now - timedelta(days=1),
        ),
    )
    db_session.add_all([older_attempt, newer_attempt])
    db_session.commit()

    active_user_ids = _get_active_user_ids(db_session, now)
    due_bloggers = _build_due_bloggers_query(db_session, now=now, active_user_ids=active_user_ids).all()

    assert [blogger.username for blogger in due_bloggers] == ["older_attempt", "newer_attempt"]


@pytest.mark.unit
def test_monitor_runtime_snapshot_reports_backlog_and_outbox(db_session):
    now = datetime.now(timezone.utc)
    user = User(
        telegram_id=770011,
        username="snapshot_owner",
        role="admin",
        balance=Decimal("50.00"),
    )
    db_session.add(user)
    db_session.flush()

    creator_due = CreatorSource(
        user_id=user.id,
        username="snapshot_due_creator",
        enabled=True,
        interval_minutes=15,
        mode="manual_review",
        monitored_blogger=MonitoredBlogger(
            username="snapshot_due",
            next_check_at=now - timedelta(minutes=25),
            last_attempt_at=now - timedelta(hours=2),
        ),
    )
    creator_unscheduled = CreatorSource(
        user_id=user.id,
        username="snapshot_unscheduled_creator",
        enabled=True,
        interval_minutes=15,
        mode="manual_review",
        monitored_blogger=MonitoredBlogger(
            username="snapshot_unscheduled",
            next_check_at=None,
        ),
    )
    creator_future = CreatorSource(
        user_id=user.id,
        username="snapshot_future_creator",
        enabled=True,
        interval_minutes=15,
        mode="manual_review",
        monitored_blogger=MonitoredBlogger(
            username="snapshot_future",
            next_check_at=now + timedelta(minutes=12),
            last_success_at=now - timedelta(minutes=5),
        ),
    )
    db_session.add_all([creator_due, creator_unscheduled, creator_future])
    db_session.flush()
    db_session.add_all(
        [
            MonitorOutbox(
                event_type="notify_new_videos",
                blogger_id=creator_due.monitored_blogger.id,
                creator_id=creator_due.id,
                payload={"creator_id": creator_due.id, "video_ids": [1]},
                status="pending",
                dedupe_key="snapshot:pending",
                available_at=now - timedelta(minutes=7),
            ),
            MonitorOutbox(
                event_type="notify_pending_review",
                blogger_id=creator_unscheduled.monitored_blogger.id,
                creator_id=creator_unscheduled.id,
                payload={"creator_id": creator_unscheduled.id, "video_ids": [2]},
                status="failed",
                dedupe_key="snapshot:failed",
                available_at=now - timedelta(minutes=3),
            ),
            MonitorOutbox(
                event_type="autopilot_create",
                blogger_id=creator_future.monitored_blogger.id,
                creator_id=creator_future.id,
                payload={"creator_id": creator_future.id, "video_ids": [3]},
                status="processing",
                dedupe_key="snapshot:processing",
                available_at=now + timedelta(minutes=1),
            ),
        ]
    )
    db_session.commit()

    active_user_ids = _get_active_user_ids(db_session, now)
    snapshot = get_monitor_runtime_snapshot(
        db_session,
        now=now,
        active_user_ids=active_user_ids,
    )

    assert snapshot["due_backlog"] == 2
    assert snapshot["unscheduled_due"] == 1
    assert snapshot["never_successful_due"] == 2
    assert snapshot["oldest_overdue_minutes"] >= 25
    assert snapshot["next_due_in_minutes"] == 12
    assert snapshot["outbox_ready"] == 2
    assert snapshot["outbox_pending"] == 1
    assert snapshot["outbox_failed"] == 1
    assert snapshot["outbox_processing"] == 1
    assert snapshot["oldest_outbox_ready_minutes"] >= 7
    assert snapshot["due_preview"]


@pytest.mark.unit
def test_create_monitored_tasks_batch_sync_limits_by_balance(db_session):
    user = User(
        telegram_id=770002,
        username="autopilot_owner",
        role="user",
        balance=Decimal("11.00"),
    )
    pricing = Pricing(price_success=Decimal("5.00"), price_failure=Decimal("2.00"))
    blogger = MonitoredBlogger(username="batch_blogger")
    creator = CreatorSource(
        user=user,
        username="batch_creator",
        enabled=True,
        interval_minutes=15,
        mode="auto_comment",
        monitored_blogger=blogger,
    )
    template = TaskTemplate(
        user=user,
        name="auto",
        main_comment="Main text",
        reply1="r1",
        reply2="r2",
        reply3="r3",
        settings={"text_variants": ["Main text", "Second text", "Third text"]},
    )
    db_session.add_all([user, pricing, blogger, creator, template])
    db_session.flush()

    videos = [
        CreatorVideo(creator=creator, video_id="v1", url="https://tiktok.com/1", status="new"),
        CreatorVideo(creator=creator, video_id="v2", url="https://tiktok.com/2", status="new"),
        CreatorVideo(creator=creator, video_id="v3", url="https://tiktok.com/3", status="new"),
    ]
    db_session.add_all(videos)
    db_session.commit()

    result = create_monitored_tasks_batch_sync(
        db_session,
        creator,
        video_ids=[video.id for video in videos],
        template=template,
        origin="autopilot",
        created_from="monitor_worker",
    )

    db_session.refresh(user)
    db_session.refresh(videos[0])
    db_session.refresh(videos[1])
    db_session.refresh(videos[2])

    assert result.status == "created"
    assert len(result.created_video_ids) == 2
    assert result.total_cost == Decimal("10.00")
    assert result.reason == "partial_insufficient_funds"
    assert result.skipped_video_ids == [videos[2].id]
    assert user.balance == Decimal("1.00")
    assert videos[0].status == "queued"
    assert videos[1].status == "queued"
    assert videos[2].status == "new"
    tasks = db_session.query(Task).filter(Task.id.in_(result.task_ids)).order_by(Task.id).all()
    assert [task.main_comment for task in tasks] == ["Main text", "Second text"]
    assert [task.settings.get("text_variant_offset") for task in tasks] == [0, 1]
