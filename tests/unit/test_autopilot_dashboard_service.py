import pytest
from sqlalchemy import select

from db.models import CreatorSource, CreatorVideo, MonitorOutbox, MonitoredBlogger, PendingEnqueue, Task, User
from services.autopilot import cache as autopilot_cache
from services.autopilot import dashboard as autopilot_dashboard


@pytest.mark.unit
def test_autopilot_dashboard_monitor_down_executor_alive_is_warning():
    state, label, icon = autopilot_dashboard._state_from_inputs(
        mode="active",
        redis_alive=True,
        monitor_alive=False,
        executor_alive=True,
        incidents=[],
    )

    assert state == "warning"
    assert label == "Мониторинг не отвечает"
    assert icon == "⚠️"


@pytest.mark.unit
def test_autopilot_dashboard_executor_down_is_down():
    state, label, icon = autopilot_dashboard._state_from_inputs(
        mode="active",
        redis_alive=True,
        monitor_alive=True,
        executor_alive=False,
        incidents=[],
    )

    assert state == "down"
    assert label == "Executor не отвечает"
    assert icon == "🔴"


@pytest.mark.unit
@pytest.mark.asyncio
async def test_collect_dashboard_reports_incident_state(monkeypatch, async_db_session):
    autopilot_cache._cache.clear()
    autopilot_cache._inflight.clear()

    async def fake_redis_alive():
        return True

    async def fake_current(db, user_id):
        return {
            "active_task": {"task_id": 4231, "creator_username": "blog"},
            "processing_tasks": [],
            "queue_head": [],
            "outbox_active": [],
            "pending_count": 5,
            "active_task_id": 4231,
        }

    async def fake_summary(db, user_id, period):
        return {"total": 1, "status_counts": {"done": 1}, "net_cost": 3.2}

    monkeypatch.setattr(autopilot_dashboard, "_redis_alive", fake_redis_alive)
    monkeypatch.setattr(autopilot_dashboard, "collect_current", fake_current)
    monkeypatch.setattr(autopilot_dashboard, "collect_summary", fake_summary)
    monkeypatch.setattr(autopilot_dashboard, "get_monitor_heartbeat", lambda: (True, {}))
    monkeypatch.setattr(autopilot_dashboard, "get_executor_heartbeat", lambda: (True, {}))
    monkeypatch.setattr(
        autopilot_dashboard,
        "list_monitor_incidents",
        lambda include_recovered=False: [
            {
                "type": "mobile_proxy_pressure",
                "title": "Proxy pressure",
                "severity": "warning",
                "state": "active",
                "cause": "mobile proxy errors",
                "auto_action": "cooldown",
                "admin_action": "check proxy",
            }
        ],
    )
    monkeypatch.setattr(autopilot_dashboard, "get_automation_state", lambda incidents: {"state": "limited"})
    monkeypatch.setattr(autopilot_dashboard, "build_recommended_actions", lambda incidents: ["check proxy"])
    monkeypatch.setattr(autopilot_dashboard, "_notify_cooldown_minutes", lambda user_id: 0)

    snapshot = await autopilot_dashboard.collect_dashboard(async_db_session, 123)

    assert snapshot["state"] == "warning"
    assert snapshot["queue_count"] == 5
    assert snapshot["incidents"][0]["title"] == "Proxy pressure"
    assert snapshot["current_work"]["active_task"]["task_id"] == 4231


@pytest.mark.unit
@pytest.mark.asyncio
async def test_collect_current_does_not_double_count_pending_enqueue(monkeypatch, async_db_session):
    autopilot_cache._cache.clear()
    autopilot_cache._inflight.clear()

    user = User(telegram_id=95010, username="ap_current_count", role="user", balance=1000, is_blocked=False)
    async_db_session.add(user)
    await async_db_session.flush()

    pending = Task(
        user_id=user.id,
        status="pending",
        main_comment="main",
        video_urls=["https://www.tiktok.com/@ap_current_count/video/1"],
        settings={"origin": "autopilot", "creator_username": "ap_current_count"},
    )
    async_db_session.add(pending)
    await async_db_session.flush()
    async_db_session.add(PendingEnqueue(task_id=pending.id))
    await async_db_session.commit()

    monkeypatch.setattr(autopilot_dashboard, "_redis_queue_ids", lambda: ([], None))
    monkeypatch.setattr(autopilot_dashboard, "get_executor_heartbeat", lambda: (True, {}))
    monkeypatch.setattr(autopilot_dashboard, "executor_task_activity_snapshot", lambda: (False, {}))

    snapshot = await autopilot_dashboard.collect_current(async_db_session, user.id)

    assert snapshot["pending_count"] == 1
    assert [task["task_id"] for task in snapshot["queue_head"]] == [pending.id]


@pytest.mark.unit
@pytest.mark.asyncio
async def test_collect_current_reports_processing_tasks(monkeypatch, async_db_session):
    autopilot_cache._cache.clear()
    autopilot_cache._inflight.clear()

    user = User(telegram_id=95011, username="ap_current_processing", role="user", balance=1000, is_blocked=False)
    async_db_session.add(user)
    await async_db_session.flush()

    running = Task(
        user_id=user.id,
        status="in_progress",
        main_comment="main",
        video_urls=["https://www.tiktok.com/@ap_current_processing/video/1"],
        settings={"origin": "autopilot", "creator_username": "ap_current_processing"},
    )
    async_db_session.add(running)
    await async_db_session.commit()

    monkeypatch.setattr(autopilot_dashboard, "_redis_queue_ids", lambda: ([], None))
    monkeypatch.setattr(autopilot_dashboard, "get_executor_heartbeat", lambda: (False, {}))
    monkeypatch.setattr(autopilot_dashboard, "executor_task_activity_snapshot", lambda: (False, {}))

    snapshot = await autopilot_dashboard.collect_current(async_db_session, user.id)

    assert snapshot["active_task"] is None
    assert snapshot["processing_count"] == 1
    assert snapshot["processing_tasks"][0]["task_id"] == running.id


@pytest.mark.unit
@pytest.mark.asyncio
async def test_update_limits_active_requeues_existing_new_auto_videos(async_db_session):
    user = User(telegram_id=95001, username="ap_limits_requeue", role="user", balance=1000, is_blocked=False)
    blogger = MonitoredBlogger(username="ap_limits_requeue_creator")
    async_db_session.add_all([user, blogger])
    await async_db_session.flush()

    creator = CreatorSource(
        user_id=user.id,
        username="ap_limits_requeue_creator",
        monitored_blogger_id=blogger.id,
        enabled=True,
        interval_minutes=30,
        mode="auto_comment",
        default_template_id=123,
    )
    async_db_session.add(creator)
    await async_db_session.flush()
    video = CreatorVideo(creator_id=creator.id, video_id="new-1", url="url-1", status="new")
    async_db_session.add(video)
    await async_db_session.commit()

    result = await autopilot_dashboard.update_limits(async_db_session, owner_user_id=user.id, mode="active")

    assert result["mode"] == "active"
    assert result["queued_existing_new_video_events"] == 1
    assert result["queued_existing_new_video_count"] == 1
    rows = await async_db_session.execute(select(MonitorOutbox))
    outbox = rows.scalar_one()
    assert outbox.event_type == "autopilot_create"
    assert outbox.creator_id == creator.id
    assert outbox.payload["video_ids"] == [video.id]
