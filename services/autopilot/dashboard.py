from __future__ import annotations

import asyncio
import json
from dataclasses import asdict
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from config.settings import get_settings
from db.models import AppSettings, CreatorSource, CreatorVideo, MonitorOutbox, Task
from services.app_settings_service import (
    AUTOPILOT_CONTROL_ACTIVE,
    AUTOPILOT_CONTROL_MODE_KEY,
    AUTOPILOT_DEFAULT_MAX_CREATE_PER_CREATOR,
    AUTOPILOT_DEFAULT_MAX_CREATE_PER_HOUR,
    AUTOPILOT_DEFAULT_MAX_CREATE_PER_TICK,
    AUTOPILOT_MAX_CREATE_PER_CREATOR_KEY,
    AUTOPILOT_MAX_CREATE_PER_HOUR_KEY,
    AUTOPILOT_MAX_CREATE_PER_TICK_KEY,
)
from services.autopilot.cache import cached_with_ttl, clear_cache as clear_autopilot_cache
from services.autopilot.models import CurrentWorkSnapshot, DashboardSnapshot, IncidentSummary, LimitsSnapshot, OutboxPreview
from services.autopilot.report import VISIBLE_ORIGINS, collect_summary, list_recent_tasks, task_preview
from services.monitor_incident_service import build_recommended_actions, get_automation_state, list_monitor_incidents
from services.redis_service import executor_task_activity_snapshot, get_executor_heartbeat, get_monitor_heartbeat, get_redis_client

settings = get_settings()


def _parse_setting_value(raw_value: Any, default: Any = None) -> Any:
    if raw_value is None:
        return default
    if not isinstance(raw_value, str):
        return raw_value
    try:
        return json.loads(raw_value)
    except (TypeError, ValueError):
        return raw_value


async def get_app_setting(db: AsyncSession, key: str, default: Any = None) -> Any:
    result = await db.execute(select(AppSettings.value).filter(AppSettings.key == key))
    return _parse_setting_value(result.scalar_one_or_none(), default)


async def set_app_setting(db: AsyncSession, key: str, value: Any, *, description: str | None = None, updated_by: int | None = None) -> None:
    value_str = value if isinstance(value, str) else json.dumps(value)
    result = await db.execute(select(AppSettings).filter(AppSettings.key == key))
    row = result.scalar_one_or_none()
    if row is None:
        db.add(AppSettings(key=key, value=value_str, description=description, updated_by=updated_by))
        return
    row.value = value_str
    if description is not None:
        row.description = description
    if updated_by is not None:
        row.updated_by = updated_by


def coerce_int_range(value: Any, default: int, min_value: int, max_value: int) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        parsed = default
    return max(min_value, min(max_value, parsed))


async def get_limits(db: AsyncSession) -> dict[str, Any]:
    mode = str(await get_app_setting(db, AUTOPILOT_CONTROL_MODE_KEY, AUTOPILOT_CONTROL_ACTIVE) or AUTOPILOT_CONTROL_ACTIVE)
    if mode not in {"active", "paused", "drain"}:
        mode = AUTOPILOT_CONTROL_ACTIVE
    snapshot = LimitsSnapshot(
        mode=mode,
        max_create_per_tick=coerce_int_range(
            await get_app_setting(db, AUTOPILOT_MAX_CREATE_PER_TICK_KEY, AUTOPILOT_DEFAULT_MAX_CREATE_PER_TICK),
            AUTOPILOT_DEFAULT_MAX_CREATE_PER_TICK,
            0,
            1000,
        ),
        max_create_per_creator=coerce_int_range(
            await get_app_setting(db, AUTOPILOT_MAX_CREATE_PER_CREATOR_KEY, AUTOPILOT_DEFAULT_MAX_CREATE_PER_CREATOR),
            AUTOPILOT_DEFAULT_MAX_CREATE_PER_CREATOR,
            0,
            1000,
        ),
        max_create_per_hour=coerce_int_range(
            await get_app_setting(db, AUTOPILOT_MAX_CREATE_PER_HOUR_KEY, AUTOPILOT_DEFAULT_MAX_CREATE_PER_HOUR),
            AUTOPILOT_DEFAULT_MAX_CREATE_PER_HOUR,
            0,
            10000,
        ),
    )
    return asdict(snapshot)


async def update_limits(
    db: AsyncSession,
    *,
    owner_user_id: int | None = None,
    mode: str | None = None,
    max_create_per_tick: int | None = None,
    max_create_per_creator: int | None = None,
    max_create_per_hour: int | None = None,
    updated_by: int | None = None,
) -> dict[str, Any]:
    queued_existing_video_events = 0
    queued_existing_video_count = 0
    if mode is not None:
        normalized = str(mode or "").strip().lower()
        if normalized not in {"active", "paused", "drain"}:
            raise ValueError("mode must be one of: active, paused, drain")
        await set_app_setting(
            db,
            AUTOPILOT_CONTROL_MODE_KEY,
            normalized,
            description="Autopilot order creation mode: active, paused, or drain.",
            updated_by=updated_by,
        )
        if normalized == AUTOPILOT_CONTROL_ACTIVE and owner_user_id is not None:
            queued_existing_video_events, queued_existing_video_count = await _queue_existing_new_videos_for_user(
                db,
                int(owner_user_id),
            )
    if max_create_per_tick is not None:
        await set_app_setting(
            db,
            AUTOPILOT_MAX_CREATE_PER_TICK_KEY,
            coerce_int_range(max_create_per_tick, AUTOPILOT_DEFAULT_MAX_CREATE_PER_TICK, 0, 1000),
            description="Maximum autopilot orders created from monitor outbox per monitor pass.",
            updated_by=updated_by,
        )
    if max_create_per_creator is not None:
        await set_app_setting(
            db,
            AUTOPILOT_MAX_CREATE_PER_CREATOR_KEY,
            coerce_int_range(max_create_per_creator, AUTOPILOT_DEFAULT_MAX_CREATE_PER_CREATOR, 0, 1000),
            description="Maximum autopilot orders created for one creator from one outbox event.",
            updated_by=updated_by,
        )
    if max_create_per_hour is not None:
        await set_app_setting(
            db,
            AUTOPILOT_MAX_CREATE_PER_HOUR_KEY,
            coerce_int_range(max_create_per_hour, AUTOPILOT_DEFAULT_MAX_CREATE_PER_HOUR, 0, 10000),
            description="Maximum autopilot orders created globally per rolling hour.",
            updated_by=updated_by,
        )
    await db.commit()
    clear_autopilot_cache()
    try:
        from services.app_settings_service import clear_cache

        clear_cache()
    except Exception:
        pass
    payload = await get_limits(db)
    payload["queued_existing_new_video_events"] = queued_existing_video_events
    payload["queued_existing_new_video_count"] = queued_existing_video_count
    return payload


def _build_monitor_outbox_dedupe_key(
    event_type: str,
    creator_id: int | None,
    video_ids: list[int] | list[str],
    extra: str | None = None,
) -> str:
    normalized_ids = ",".join(str(item) for item in sorted({int(item) for item in video_ids}))
    suffix = f":{extra}" if extra else ""
    return f"{event_type}:{creator_id or 0}:{normalized_ids}{suffix}"


def _creator_has_autopilot_template_reference(creator: CreatorSource) -> bool:
    if creator.default_template_id:
        return True
    template_ids = creator.template_ids
    return isinstance(template_ids, list) and bool(template_ids)


async def _enqueue_existing_new_videos_for_autopilot(db: AsyncSession, creator: CreatorSource) -> tuple[int, int]:
    if not creator.id or not creator.monitored_blogger_id:
        return (0, 0)

    result = await db.execute(
        select(CreatorVideo.id)
        .filter(
            CreatorVideo.creator_id == creator.id,
            CreatorVideo.status == "new",
            CreatorVideo.task_id.is_(None),
        )
        .order_by(CreatorVideo.discovered_at.asc(), CreatorVideo.id.asc())
    )
    video_ids = [int(video_id) for video_id in result.scalars().all()]
    if not video_ids:
        return (0, 0)

    dedupe_key = _build_monitor_outbox_dedupe_key("autopilot_create", creator.id, video_ids)
    existing = await db.execute(
        select(MonitorOutbox.id, MonitorOutbox.status)
        .filter(MonitorOutbox.dedupe_key == dedupe_key)
        .limit(1)
    )
    existing_row = existing.first()
    if existing_row:
        if existing_row.status in {"pending", "failed", "processing"}:
            return (0, len(video_ids))
        dedupe_key = _build_monitor_outbox_dedupe_key(
            "autopilot_create",
            creator.id,
            video_ids,
            extra=f"requeue:{existing_row.id}:{datetime.now(timezone.utc).strftime('%Y%m%d%H%M%S%f')}",
        )

    db.add(
        MonitorOutbox(
            event_type="autopilot_create",
            blogger_id=creator.monitored_blogger_id,
            creator_id=creator.id,
            payload={"creator_id": creator.id, "video_ids": video_ids},
            status="pending",
            dedupe_key=dedupe_key,
            available_at=datetime.now(timezone.utc),
        )
    )
    return (1, len(video_ids))


async def _queue_existing_new_videos_for_user(db: AsyncSession, user_id: int) -> tuple[int, int]:
    creators_result = await db.execute(
        select(CreatorSource)
        .filter(
            CreatorSource.user_id == user_id,
            CreatorSource.enabled.is_(True),
            CreatorSource.mode == "auto_comment",
        )
        .order_by(CreatorSource.id.asc())
    )
    queued_events = 0
    queued_videos = 0
    for creator in creators_result.scalars().all():
        if not _creator_has_autopilot_template_reference(creator):
            continue
        events, videos = await _enqueue_existing_new_videos_for_autopilot(db, creator)
        queued_events += events
        queued_videos += videos
    return queued_events, queued_videos


def _coerce_task_id(value: Any) -> int | None:
    try:
        if isinstance(value, bytes):
            value = value.decode("utf-8", errors="ignore")
        return int(str(value))
    except (TypeError, ValueError):
        return None


def _redis_queue_ids() -> tuple[list[int], str | None]:
    try:
        redis_client = get_redis_client()
        raw_ids = redis_client.lrange(settings.REDIS_TASK_QUEUE, 0, 99) or []
        return [task_id for task_id in (_coerce_task_id(item) for item in raw_ids) if task_id is not None], None
    except Exception as exc:
        return [], str(exc)[:160]


async def _redis_alive() -> bool:
    try:
        redis_client = get_redis_client()
        return bool(await asyncio.to_thread(redis_client.ping))
    except Exception:
        return False


@cached_with_ttl(5)
async def collect_current(db: AsyncSession, user_id: int) -> dict[str, Any]:
    redis_task_ids, redis_error = await asyncio.to_thread(_redis_queue_ids)
    heartbeat_alive, heartbeat_info = get_executor_heartbeat()
    executor_active, executor_activity = executor_task_activity_snapshot()
    active_task_id = _coerce_task_id(heartbeat_info.get("active_task_id") if isinstance(heartbeat_info, dict) else None)

    active_task = None
    if active_task_id:
        active_result = await db.execute(
            select(Task).filter(
                Task.id == active_task_id,
                Task.user_id == user_id,
                Task.settings["origin"].as_string().in_(sorted(VISIBLE_ORIGINS)),
            )
        )
        row = active_result.scalar_one_or_none()
        if row:
            active_task = task_preview(row)

    pending_count_result = await db.execute(
        select(func.count(Task.id)).filter(
            Task.user_id == user_id,
            Task.settings["origin"].as_string().in_(sorted(VISIBLE_ORIGINS)),
            Task.status == "pending",
        )
    )
    pending_count = int(pending_count_result.scalar_one() or 0)

    processing_count_result = await db.execute(
        select(func.count(Task.id)).filter(
            Task.user_id == user_id,
            Task.settings["origin"].as_string().in_(sorted(VISIBLE_ORIGINS)),
            Task.status == "in_progress",
        )
    )
    processing_count = int(processing_count_result.scalar_one() or 0)

    processing_result = await db.execute(
        select(Task)
        .filter(
            Task.user_id == user_id,
            Task.settings["origin"].as_string().in_(sorted(VISIBLE_ORIGINS)),
            Task.status == "in_progress",
        )
        .order_by(Task.updated_at.desc(), Task.created_at.asc(), Task.id.asc())
        .limit(5)
    )
    processing_tasks = [task_preview(task) for task in processing_result.scalars().all()]

    queue_head: list[dict[str, Any]] = []
    if redis_task_ids:
        queue_rows = await db.execute(
            select(Task).filter(
                Task.id.in_(redis_task_ids[:20]),
                Task.user_id == user_id,
                Task.settings["origin"].as_string().in_(sorted(VISIBLE_ORIGINS)),
                Task.status == "pending",
            )
        )
        queue_map = {int(task.id): task_preview(task) for task in queue_rows.scalars().all()}
        queue_head = [queue_map[task_id] for task_id in redis_task_ids if task_id in queue_map][:5]
    if not queue_head:
        queue_rows = await db.execute(
            select(Task)
            .filter(
                Task.user_id == user_id,
                Task.settings["origin"].as_string().in_(sorted(VISIBLE_ORIGINS)),
                Task.status == "pending",
            )
            .order_by(Task.created_at.asc(), Task.id.asc())
            .limit(5)
        )
        queue_head = [task_preview(task) for task in queue_rows.scalars().all()]

    creator_ids_result = await db.execute(select(CreatorSource.id).filter(CreatorSource.user_id == user_id))
    creator_ids = [creator_id for (creator_id,) in creator_ids_result.all()]
    outbox_active: list[dict[str, Any]] = []
    if creator_ids:
        outbox_rows = await db.execute(
            select(MonitorOutbox, CreatorSource.username)
            .join(CreatorSource, CreatorSource.id == MonitorOutbox.creator_id)
            .filter(
                MonitorOutbox.creator_id.in_(creator_ids),
                MonitorOutbox.event_type == "autopilot_create",
                MonitorOutbox.status.in_(["pending", "processing", "failed"]),
            )
            .order_by(MonitorOutbox.created_at.asc(), MonitorOutbox.id.asc())
            .limit(5)
        )
        for entry, username in outbox_rows.all():
            payload = entry.payload if isinstance(entry.payload, dict) else {}
            video_ids = []
            for raw_id in payload.get("video_ids") or []:
                try:
                    video_ids.append(int(raw_id))
                except (TypeError, ValueError):
                    continue
            video_urls: list[str] = []
            if video_ids:
                video_rows = await db.execute(
                    select(CreatorVideo.url)
                    .filter(CreatorVideo.id.in_(video_ids))
                    .order_by(CreatorVideo.discovered_at.asc(), CreatorVideo.id.asc())
                    .limit(3)
                )
                video_urls = [str(url or "") for (url,) in video_rows.all() if str(url or "").strip()]
            outbox_active.append(
                asdict(
                    OutboxPreview(
                        id=int(entry.id),
                        creator_id=entry.creator_id,
                        creator_username=str(username or ""),
                        status=str(entry.status or ""),
                        video_count=len(payload.get("video_ids") or []),
                        video_urls=video_urls,
                        created_at=entry.created_at,
                    )
                )
            )

    snapshot = CurrentWorkSnapshot(
        active_task=active_task,
        processing_tasks=processing_tasks,
        queue_head=queue_head,
        outbox_active=outbox_active,
        pending_count=pending_count,
        processing_count=processing_count,
        active_task_id=active_task_id,
    )
    payload = asdict(snapshot)
    payload["executor"] = {
        "active": bool(executor_active),
        "heartbeat_alive": bool(heartbeat_alive),
        "heartbeat": heartbeat_info if isinstance(heartbeat_info, dict) else {},
        "activity": executor_activity if isinstance(executor_activity, dict) else {},
    }
    payload["redis"] = {"preview_task_ids": redis_task_ids[:20], "error": redis_error}
    return payload


def _incident_payload(raw: dict[str, Any]) -> dict[str, Any]:
    return asdict(
        IncidentSummary(
            type=str(raw.get("type") or ""),
            title=str(raw.get("title") or raw.get("type") or ""),
            severity=str(raw.get("severity") or ""),
            state=str(raw.get("state") or ""),
            cause=str(raw.get("cause") or ""),
            auto_action=str(raw.get("auto_action") or ""),
            admin_action=str(raw.get("admin_action") or ""),
            cooldown_until=raw.get("cooldown_until"),
        )
    )


def _state_from_inputs(*, mode: str, redis_alive: bool, monitor_alive: bool, executor_alive: bool, incidents: list[dict[str, Any]]) -> tuple[str, str, str]:
    active_incidents = [item for item in incidents if item.get("state") == "active"]
    critical = [item for item in active_incidents if item.get("severity") == "critical"]
    if not redis_alive:
        return "down", "Redis не отвечает", "🔴"
    if not monitor_alive and not executor_alive:
        return "down", "Мониторинг и executor не отвечают", "🔴"
    if not executor_alive:
        return "down", "Executor не отвечает", "🔴"
    if not monitor_alive:
        return "warning", "Мониторинг не отвечает", "⚠️"
    if critical or active_incidents:
        return "warning", "Проблема", "⚠️"
    if mode == "paused":
        return "paused", "На паузе", "⏸"
    if mode == "drain":
        return "drain", "Дренаж", "🚰"
    return "ok", "Работает", "✅"


def _notify_cooldown_minutes(user_id: int) -> int:
    try:
        ttl = get_redis_client().ttl(f"notify_admin:cooldown:{user_id}")
        ttl = max(0, int(ttl or 0))
        return (ttl + 59) // 60 if ttl else 0
    except Exception:
        return 0


@cached_with_ttl(10)
async def collect_dashboard(db: AsyncSession, user_id: int) -> dict[str, Any]:
    control = await get_limits(db)
    current = await collect_current(db, user_id)
    summary = await collect_summary(db, user_id, "24h")
    recent_tasks = await list_recent_tasks(db, user_id, limit=3)
    incidents_raw = list_monitor_incidents(include_recovered=False)
    incidents = [_incident_payload(item) for item in incidents_raw if item.get("state") == "active"]
    redis_alive = await _redis_alive()
    monitor_alive, monitor_info = get_monitor_heartbeat()
    executor_alive, executor_info = get_executor_heartbeat()
    state, state_label, state_icon = _state_from_inputs(
        mode=str(control.get("mode") or "active"),
        redis_alive=redis_alive,
        monitor_alive=monitor_alive,
        executor_alive=executor_alive,
        incidents=incidents,
    )
    automation_state = get_automation_state(incidents_raw)
    snapshot = DashboardSnapshot(
        state=state,
        state_label=state_label,
        state_icon=state_icon,
        control=LimitsSnapshot(
            mode=str(control.get("mode") or "active"),
            max_create_per_tick=int(control.get("max_create_per_tick") or 20),
            max_create_per_creator=int(control.get("max_create_per_creator") or 3),
            max_create_per_hour=int(control.get("max_create_per_hour") or 120),
        ),
        incidents=[IncidentSummary(**item) for item in incidents],
        current_work=CurrentWorkSnapshot(
            active_task=current.get("active_task"),
            processing_tasks=current.get("processing_tasks") or [],
            queue_head=current.get("queue_head") or [],
            outbox_active=current.get("outbox_active") or [],
            pending_count=int(current.get("pending_count") or 0),
            processing_count=int(current.get("processing_count") or 0),
            active_task_id=current.get("active_task_id"),
        ),
        summary=summary,
        queue_count=int(current.get("pending_count") or 0),
        redis_alive=redis_alive,
        monitor_alive=monitor_alive,
        executor_alive=executor_alive,
        metadata={
            "monitor": monitor_info if isinstance(monitor_info, dict) else {},
            "executor": executor_info if isinstance(executor_info, dict) else {},
            "automation_state": automation_state,
            "recommended_actions": build_recommended_actions(incidents_raw),
        },
    )
    payload = asdict(snapshot)
    payload["notify_admin_cooldown_minutes"] = _notify_cooldown_minutes(user_id)
    payload["recent_tasks"] = recent_tasks
    return payload
