from decimal import Decimal

import pytest
from sqlalchemy import select

from db.models import AppSettings, CreatorSource, CreatorVideo, MonitorOutbox, PendingEnqueue, Pricing, Task, User
from services.autopilot import actions as autopilot_actions


@pytest.mark.unit
@pytest.mark.asyncio
async def test_archive_and_hide_mark_tasks_as_report_hidden(async_db_session):
    user = User(telegram_id=94001, username="ap_actions_user", role="user", balance=1000, is_blocked=False)
    async_db_session.add(user)
    await async_db_session.flush()

    done_task = Task(
        user_id=user.id,
        status="done",
        main_comment="done",
        video_urls=["https://www.tiktok.com/@a/video/1"],
        settings={"origin": "autopilot", "creator_username": "a"},
    )
    pending_task = Task(
        user_id=user.id,
        status="pending",
        main_comment="pending",
        video_urls=["https://www.tiktok.com/@a/video/2"],
        settings={"origin": "autopilot", "creator_username": "a"},
    )
    async_db_session.add_all([done_task, pending_task])
    await async_db_session.commit()

    archive = await autopilot_actions.archive_completed(async_db_session, user.id, "24h")
    await async_db_session.refresh(done_task)
    await async_db_session.refresh(pending_task)

    assert archive["details"]["archived_tasks"] == 1
    assert done_task.settings["origin"] == "autopilot_archived"
    assert done_task.settings["autopilot_report_hidden"] is True
    assert pending_task.settings["origin"] == "autopilot"

    hide = await autopilot_actions.hide_task(async_db_session, user.id, pending_task.id)
    await async_db_session.refresh(pending_task)

    assert hide["success"] is False
    assert hide["code"] == "bad_status"
    assert pending_task.settings["origin"] == "autopilot"

    pending_task.status = "error"
    await async_db_session.commit()

    hide = await autopilot_actions.hide_task(async_db_session, user.id, pending_task.id)
    await async_db_session.refresh(pending_task)

    assert hide["success"] is True
    assert pending_task.settings["origin"] == "autopilot_archived"
    assert pending_task.settings["autopilot_report_hidden"] is True


@pytest.mark.unit
@pytest.mark.asyncio
async def test_retry_task_checks_balance_before_creating_copy(monkeypatch, async_db_session):
    monkeypatch.setattr(autopilot_actions.settings, "DEMO_MODE", False, raising=False)
    monkeypatch.setattr(autopilot_actions.settings, "ADMIN_IDS", [], raising=False)

    user = User(telegram_id=94002, username="ap_retry_user", role="user", balance=Decimal("1.00"), is_blocked=False)
    pricing = Pricing(price_success=Decimal("5.00"), price_failure=Decimal("2.00"))
    async_db_session.add_all([user, pricing])
    await async_db_session.flush()

    original = Task(
        user_id=user.id,
        status="error",
        main_comment="main",
        reply1="reply",
        likes=0,
        video_urls=["https://www.tiktok.com/@a/video/3"],
        settings={"origin": "autopilot", "creator_source_id": 123, "creator_username": "a"},
        total_cost=Decimal("5.00"),
    )
    async_db_session.add(original)
    await async_db_session.commit()

    result = await autopilot_actions.retry_task(async_db_session, user.id, original.id)

    assert result["success"] is False
    assert result["code"] == "insufficient_funds"
    rows = await async_db_session.execute(
        select(Task).filter(Task.user_id == user.id, Task.settings["origin"].as_string() == "manual_retry")
    )
    assert rows.scalars().all() == []


@pytest.mark.unit
@pytest.mark.asyncio
async def test_retry_task_preview_uses_current_pricing_not_original_cost(monkeypatch, async_db_session):
    monkeypatch.setattr(autopilot_actions.settings, "DEMO_MODE", False, raising=False)
    monkeypatch.setattr(autopilot_actions.settings, "ADMIN_IDS", [], raising=False)

    user = User(telegram_id=94005, username="ap_retry_preview", role="user", balance=Decimal("20.00"), is_blocked=False)
    pricing = Pricing(price_success=Decimal("7.00"), price_failure=Decimal("2.00"))
    async_db_session.add_all([user, pricing])
    await async_db_session.flush()

    original = Task(
        user_id=user.id,
        status="error",
        main_comment="main",
        video_urls=["https://www.tiktok.com/@a/video/1", "https://www.tiktok.com/@a/video/2"],
        settings={"origin": "autopilot", "creator_username": "a"},
        total_cost=Decimal("3.20"),
    )
    async_db_session.add(original)
    await async_db_session.commit()

    preview = await autopilot_actions.retry_task_preview(async_db_session, user.id, original.id)

    assert preview["success"] is True
    assert preview["details"]["cost"] == 14.0
    assert preview["details"]["balance"] == 20.0


@pytest.mark.unit
@pytest.mark.asyncio
async def test_retry_task_copies_settings_and_uses_current_commenting_mode(monkeypatch, async_db_session):
    monkeypatch.setattr(autopilot_actions.settings, "DEMO_MODE", False, raising=False)
    monkeypatch.setattr(autopilot_actions.settings, "ADMIN_IDS", [], raising=False)

    async def fake_finalize(_db, _task_id: int) -> bool:
        return True

    monkeypatch.setattr(autopilot_actions, "finalize_reserved_enqueue_async", fake_finalize)

    user = User(telegram_id=94003, username="ap_retry_success", role="user", balance=Decimal("20.00"), is_blocked=False)
    pricing = Pricing(price_success=Decimal("5.00"), price_failure=Decimal("2.00"))
    async_db_session.add_all([user, pricing, AppSettings(key="commenting_mode", value="parallel")])
    await async_db_session.flush()

    original = Task(
        user_id=user.id,
        status="partial",
        main_comment="main",
        reply1="reply",
        likes=1,
        video_urls=["https://www.tiktok.com/@a/video/4"],
        settings={
            "origin": "autopilot",
            "creator_source_id": 456,
            "creator_username": "a",
            "template_name": "Auto #2",
            "url_results": {"old": {"runtime": True}},
        },
        total_cost=Decimal("5.00"),
    )
    async_db_session.add(original)
    await async_db_session.commit()

    result = await autopilot_actions.retry_task(async_db_session, user.id, original.id)

    assert result["success"] is True
    rows = await async_db_session.execute(
        select(Task).filter(Task.user_id == user.id, Task.settings["origin"].as_string() == "manual_retry")
    )
    retry = rows.scalar_one()
    assert retry.settings["retry_of_task_id"] == original.id
    assert retry.settings["creator_source_id"] == 456
    assert retry.settings["creator_username"] == "a"
    assert retry.settings["commenting_mode"] == "parallel"
    assert "url_results" not in retry.settings


@pytest.mark.unit
@pytest.mark.asyncio
async def test_skip_autopilot_queue_marks_videos_skipped_and_refunds(monkeypatch, async_db_session):
    monkeypatch.setattr(autopilot_actions, "remove_tasks_from_queue", lambda task_ids: len(task_ids))

    user = User(telegram_id=94004, username="ap_skip_queue", role="user", balance=Decimal("10.00"), is_blocked=False)
    async_db_session.add(user)
    await async_db_session.flush()
    creator = CreatorSource(user_id=user.id, username="skip_creator", enabled=True, mode="auto_comment")
    async_db_session.add(creator)
    await async_db_session.flush()

    task = Task(
        user_id=user.id,
        status="pending",
        main_comment="main",
        video_urls=["https://www.tiktok.com/@skip_creator/video/1"],
        settings={"origin": "autopilot", "creator_source_id": creator.id, "creator_username": "skip_creator"},
        total_cost=Decimal("7.00"),
    )
    async_db_session.add(task)
    await async_db_session.flush()
    queued_video = CreatorVideo(
        creator_id=creator.id,
        video_id="queued-1",
        url="https://www.tiktok.com/@skip_creator/video/1",
        status="queued",
        task_id=task.id,
    )
    new_video = CreatorVideo(
        creator_id=creator.id,
        video_id="new-1",
        url="https://www.tiktok.com/@skip_creator/video/2",
        status="new",
    )
    async_db_session.add_all([queued_video, new_video])
    await async_db_session.flush()
    outbox = MonitorOutbox(
        event_type="autopilot_create",
        creator_id=creator.id,
        payload={"creator_id": creator.id, "video_ids": [new_video.id]},
        status="pending",
        dedupe_key="skip-queue-outbox",
    )
    notice = MonitorOutbox(
        event_type="notify_auto_orders_created",
        creator_id=creator.id,
        payload={"creator_id": creator.id, "task_ids": [task.id]},
        status="processing",
        dedupe_key="skip-queue-notice",
    )
    async_db_session.add_all([outbox, notice, PendingEnqueue(task_id=task.id)])
    await async_db_session.commit()

    preview = await autopilot_actions.skip_queue_preview(async_db_session, user.id)
    result = await autopilot_actions.skip_autopilot_queue(async_db_session, user.id)

    await async_db_session.refresh(user)
    await async_db_session.refresh(task)
    await async_db_session.refresh(queued_video)
    await async_db_session.refresh(new_video)
    await async_db_session.refresh(outbox)
    await async_db_session.refresh(notice)

    assert preview["pending_count"] == 1
    assert preview["outbox_count"] == 1
    assert preview["video_count"] == 2
    assert result["success"] is True
    assert result["details"]["cancelled_pending_tasks"] == 1
    assert result["details"]["cancelled_outbox_events"] == 1
    assert result["details"]["cancelled_notifications"] == 1
    assert result["details"]["skipped_videos"] == 2
    assert result["details"]["removed_from_redis_queue"] == 1
    assert result["details"]["refunded_amount"] == 7.0
    assert user.balance == Decimal("17.00")
    assert task.status == "stopped"
    assert task.refunded_cost == Decimal("7.00")
    assert queued_video.status == "skipped"
    assert queued_video.task_id is None
    assert new_video.status == "skipped"
    assert outbox.status == "cancelled"
    assert notice.status == "cancelled"
