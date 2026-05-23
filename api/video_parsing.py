"""
API роутер для управления парсингом видео TikTok блогеров.
"""
from fastapi import APIRouter, Depends, HTTPException, Query, Request
from pydantic import BaseModel, Field, field_validator
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, update, func, delete, or_
from db.database import get_db, get_sync_db_context
from db.models import AppSettings, CreatorSource, CreatorVideo, TaskTemplate, Task, User, MonitoredBlogger, MonitorOutbox, PendingEnqueue, Transaction
from config.limiter import limiter
from config.settings import get_settings
from typing import Any, List, Optional
import logging
import asyncio
import json
import re
import aiohttp
from decimal import Decimal
from datetime import datetime, timezone, timedelta
from api.auth import verify_admin_header, verify_user_header, ensure_user_scope
from services.task_creation_service import create_monitored_task_async
from services.app_settings_service import (
    SOFTWARE_MODE_KEY,
    SOFTWARE_MODE_PARSING,
    AUTOPILOT_CONTROL_ACTIVE,
    AUTOPILOT_CONTROL_DRAIN,
    AUTOPILOT_CONTROL_MODE_KEY,
    AUTOPILOT_CONTROL_MODES,
    AUTOPILOT_CONTROL_PAUSED,
    AUTOPILOT_DEFAULT_MAX_CREATE_PER_CREATOR,
    AUTOPILOT_DEFAULT_MAX_CREATE_PER_HOUR,
    AUTOPILOT_DEFAULT_MAX_CREATE_PER_TICK,
    AUTOPILOT_MAX_CREATE_PER_CREATOR_KEY,
    AUTOPILOT_MAX_CREATE_PER_HOUR_KEY,
    AUTOPILOT_MAX_CREATE_PER_TICK_KEY,
    clear_cache as clear_app_settings_cache,
    get_monitor_max_parallel_browsers,
)
from services.balance_service import has_full_functionality_access
from services.task_service import remove_tasks_from_queue
from services.task_stop_signal import request_task_stop
from services.video_time_utils import create_time_from_meta, published_at_iso_from_create_time
from services.redis_service import executor_task_activity_snapshot, get_executor_heartbeat, get_redis_client

logger = logging.getLogger("api.video_parsing")
settings = get_settings()

router = APIRouter(prefix="/api/video-parsing", tags=["video_parsing"])


def _video_time_payload(video: CreatorVideo) -> dict:
    create_time = create_time_from_meta(video.meta)
    return {
        "create_time": create_time,
        "published_at": published_at_iso_from_create_time(create_time),
        "meta": video.meta,
    }


def _task_video_urls(task: Task) -> list[str]:
    raw_urls = task.video_urls or []
    if isinstance(raw_urls, str):
        raw_urls = [raw_urls]
    if not isinstance(raw_urls, list):
        return []
    return [str(url).strip() for url in raw_urls if str(url or "").strip()]


def _extract_video_id_from_url(url: str) -> str | None:
    match = re.search(r"/video/([^/?#]+)", str(url or ""))
    return match.group(1) if match else None


def _iso_or_none(value: datetime | None) -> str | None:
    return value.isoformat() if value else None


async def _is_parsing_mode_enabled(db: AsyncSession) -> bool:
    result = await db.execute(select(AppSettings.value).filter(AppSettings.key == SOFTWARE_MODE_KEY))
    return (result.scalar_one_or_none() or "").strip().lower() == SOFTWARE_MODE_PARSING


def _parse_setting_value(raw_value: Any, default: Any = None) -> Any:
    if raw_value is None:
        return default
    if not isinstance(raw_value, str):
        return raw_value
    try:
        return json.loads(raw_value)
    except (TypeError, ValueError):
        return raw_value


async def _get_app_setting_async(db: AsyncSession, key: str, default: Any = None) -> Any:
    result = await db.execute(select(AppSettings.value).filter(AppSettings.key == key))
    return _parse_setting_value(result.scalar_one_or_none(), default)


async def _set_app_setting_async(
    db: AsyncSession,
    key: str,
    value: Any,
    *,
    description: str | None = None,
    updated_by: int | None = None,
) -> None:
    value_str = value if isinstance(value, str) else json.dumps(value)
    result = await db.execute(select(AppSettings).filter(AppSettings.key == key))
    setting = result.scalar_one_or_none()
    if setting is None:
        db.add(
            AppSettings(
                key=key,
                value=value_str,
                description=description,
                updated_by=updated_by,
            )
        )
        return
    setting.value = value_str
    if description is not None:
        setting.description = description
    if updated_by is not None:
        setting.updated_by = updated_by
    setting.updated_at = datetime.now(timezone.utc)


def _coerce_int_range(value: Any, default: int, min_value: int, max_value: int) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        parsed = default
    return max(min_value, min(max_value, parsed))


def _normalize_autopilot_control_mode(value: Any) -> str:
    mode = str(value or "").strip().lower()
    return mode if mode in AUTOPILOT_CONTROL_MODES else AUTOPILOT_CONTROL_ACTIVE


async def _get_autopilot_control_state(db: AsyncSession) -> dict[str, Any]:
    mode = _normalize_autopilot_control_mode(
        await _get_app_setting_async(db, AUTOPILOT_CONTROL_MODE_KEY, AUTOPILOT_CONTROL_ACTIVE)
    )
    return {
        "mode": mode,
        "limits": {
            "max_create_per_tick": _coerce_int_range(
                await _get_app_setting_async(
                    db,
                    AUTOPILOT_MAX_CREATE_PER_TICK_KEY,
                    AUTOPILOT_DEFAULT_MAX_CREATE_PER_TICK,
                ),
                AUTOPILOT_DEFAULT_MAX_CREATE_PER_TICK,
                0,
                1000,
            ),
            "max_create_per_creator": _coerce_int_range(
                await _get_app_setting_async(
                    db,
                    AUTOPILOT_MAX_CREATE_PER_CREATOR_KEY,
                    AUTOPILOT_DEFAULT_MAX_CREATE_PER_CREATOR,
                ),
                AUTOPILOT_DEFAULT_MAX_CREATE_PER_CREATOR,
                0,
                1000,
            ),
            "max_create_per_hour": _coerce_int_range(
                await _get_app_setting_async(
                    db,
                    AUTOPILOT_MAX_CREATE_PER_HOUR_KEY,
                    AUTOPILOT_DEFAULT_MAX_CREATE_PER_HOUR,
                ),
                AUTOPILOT_DEFAULT_MAX_CREATE_PER_HOUR,
                0,
                10000,
            ),
        },
    }


async def _is_parsing_mode_restricted(db: AsyncSession, user_id: int) -> bool:
    """True only when global mode is parsing AND the user has no full-mode bypass."""
    if not await _is_parsing_mode_enabled(db):
        return False
    user_result = await db.execute(select(User).filter(User.id == user_id))
    user = user_result.scalar_one_or_none()
    return not (user and has_full_functionality_access(user))


def _normalize_creator_username(value: str) -> str:
    return (value or "").strip().lstrip("@").strip().lower()


def _get_max_unique_creators_total() -> int:
    try:
        return max(0, int(getattr(settings, "MAX_UNIQUE_CREATORS_TOTAL", 2000) or 0))
    except (TypeError, ValueError):
        return 2000


async def _load_monitored_blogger_by_username(
    db: AsyncSession,
    username: str,
) -> MonitoredBlogger | None:
    result = await db.execute(
        select(MonitoredBlogger)
        .filter(func.lower(MonitoredBlogger.username) == username)
        .order_by((MonitoredBlogger.username == username).desc(), MonitoredBlogger.id.asc())
        .limit(1)
    )
    return result.scalar_one_or_none()


async def _ensure_unique_creator_capacity(
    db: AsyncSession,
    monitored_blogger: MonitoredBlogger | None,
) -> None:
    if monitored_blogger is not None:
        return

    max_unique = _get_max_unique_creators_total()
    if max_unique <= 0:
        return

    total_result = await db.execute(select(func.count(MonitoredBlogger.id)))
    current_unique = total_result.scalar_one() or 0
    if current_unique >= max_unique:
        raise HTTPException(
            status_code=403,
            detail=(
                f"Unique creator quota exceeded. Maximum {max_unique} unique creators total. "
                f"Current unique creators: {current_unique}. "
                "Existing creators can still be added as duplicate subscriptions."
            ),
        )


def _clamp_monitor_interval_minutes(value: int | None, default: int = 30) -> int:
    try:
        interval = int(value or default)
    except (TypeError, ValueError):
        interval = default
    return max(1, interval)


def _effective_monitor_interval_minutes_from_subscriptions(subscriptions: list[CreatorSource]) -> int:
    enabled_subscriptions = [sub for sub in subscriptions if getattr(sub, "enabled", True)]
    if not enabled_subscriptions:
        return 30

    requested_interval = min(
        _clamp_monitor_interval_minutes(getattr(sub, "interval_minutes", None), 30)
        for sub in enabled_subscriptions
    )
    try:
        shared_min_subs = max(2, int(getattr(settings, "SHARED_BLOGGER_MIN_SUBS", 2) or 2))
    except (TypeError, ValueError):
        shared_min_subs = 2
    try:
        shared_min_interval = max(
            1,
            int(
                getattr(
                    settings,
                    "SHARED_BLOGGER_MIN_INTERVAL_MINUTES",
                    getattr(settings, "SHARED_BLOGGER_INTERVAL_MINUTES", 15),
                )
                or 15
            ),
        )
    except (TypeError, ValueError):
        shared_min_interval = 15

    if len(enabled_subscriptions) >= shared_min_subs:
        return max(requested_interval, shared_min_interval)
    return requested_interval


async def _recalculate_monitored_blogger_schedule(
    db: AsyncSession,
    monitored_blogger_id: int | None,
    *,
    now: datetime | None = None,
    force_due_if_unchecked: bool = False,
) -> None:
    if not monitored_blogger_id:
        return

    now = now or datetime.now(timezone.utc)
    result = await db.execute(
        select(MonitoredBlogger).filter(MonitoredBlogger.id == monitored_blogger_id)
    )
    blogger = result.scalar_one_or_none()
    if not blogger:
        return

    subscriptions_result = await db.execute(
        select(CreatorSource).join(User, CreatorSource.user_id == User.id).filter(
            CreatorSource.monitored_blogger_id == monitored_blogger_id,
            CreatorSource.enabled == True,
            User.is_blocked == False,
        )
    )
    subscriptions = list(subscriptions_result.scalars().all())
    if not subscriptions:
        blogger.next_check_at = None
        blogger.lock_until = None
        blogger.lock_owner = None
        return

    interval_minutes = _effective_monitor_interval_minutes_from_subscriptions(subscriptions)
    target_next_check_at = now + timedelta(minutes=interval_minutes)
    current_next_check_at = blogger.next_check_at
    if current_next_check_at is not None and current_next_check_at.tzinfo is None:
        current_next_check_at = current_next_check_at.replace(tzinfo=timezone.utc)

    if force_due_if_unchecked or blogger.last_success_at is None:
        blogger.next_check_at = None
        return
    if current_next_check_at is None or current_next_check_at > target_next_check_at:
        blogger.next_check_at = target_next_check_at


def _extract_tagged_metric_values(
    metric_map: dict[str, Any] | None,
    prefix: str,
    *,
    tag_name: str = "result",
) -> dict[str, Any]:
    extracted: dict[str, Any] = {}
    for raw_key, value in (metric_map or {}).items():
        if not isinstance(raw_key, str) or not raw_key.startswith(f"{prefix}:"):
            continue
        parts = raw_key.split(":")
        tag_value = None
        for part in parts[1:]:
            if part.startswith(f"{tag_name}="):
                tag_value = part.split("=", 1)[1]
                break
        if tag_value:
            extracted[tag_value] = value
    return extracted


class CreatorCreate(BaseModel):
    """Модель для создания блогера."""
    username: str = Field(..., min_length=1, max_length=64, description="Username блогера (без @)")
    enabled: bool = True
    interval_minutes: int = Field(30, ge=5, le=1440, description="Интервал проверки в минутах (5-1440, т.е. от 5 минут до 24 часов)")
    mode: str = Field("manual_review", description="Режим работы: auto_comment, manual_review, notify_only")
    template_ids: Optional[List[int]] = None
    auto_start: bool = False
    
    @field_validator('username')
    @classmethod
    def validate_username(cls, v: str) -> str:
        """УЛУЧШЕНО: Валидация username - только допустимые символы."""
        if not v or not v.strip():
            raise ValueError("username не может быть пустым")
        
        username = _normalize_creator_username(v)
        # Разрешаем только буквы, цифры, точки и подчеркивания
        if not re.match(r"^[a-zA-Z0-9_.]+$", username):
            raise ValueError(
                "username может содержать только буквы (a-z, A-Z), цифры (0-9), точки (.) и подчеркивания (_)"
            )
        if not re.search(r"[a-zA-Z0-9]", username):
            raise ValueError("username должен содержать хотя бы одну букву или цифру")
        
        return username


class CreatorUpdate(BaseModel):
    """Модель для обновления блогера."""
    enabled: Optional[bool] = None
    interval_minutes: Optional[int] = Field(None, ge=5, le=1440, description="Интервал проверки в минутах (5-1440)")
    mode: Optional[str] = None
    template_ids: Optional[List[int]] = None
    default_template_id: Optional[int] = Field(None, description="ID шаблона по умолчанию для авто-комментирования")
    auto_start: Optional[bool] = None


class CreatorBulkSelector(BaseModel):
    """Selection rules for bulk creator actions."""
    scope: str = Field(
        "all",
        description=(
            "all, enabled, disabled, auto_comment, manual_review, notify_only, "
            "without_default_template, with_new_videos, explicit"
        ),
    )
    creator_ids: Optional[List[int]] = None


class CreatorBulkChanges(BaseModel):
    """Fields that can be changed by a bulk creator action."""
    enabled: Optional[bool] = None
    interval_minutes: Optional[int] = Field(None, ge=5, le=1440)
    mode: Optional[str] = None
    default_template_id: Optional[int] = Field(None, ge=0)
    run_now: bool = False


class CreatorBulkUpdate(BaseModel):
    selector: CreatorBulkSelector = Field(default_factory=CreatorBulkSelector)
    changes: CreatorBulkChanges
    dry_run: bool = True


class AutopilotCancelRequest(BaseModel):
    """Controls cancellation of queued autopilot work."""
    creator_id: Optional[int] = Field(None, ge=1)
    include_running: bool = False


class AutopilotControlUpdate(BaseModel):
    """Updates global autopilot controls."""
    mode: Optional[str] = None
    max_create_per_tick: Optional[int] = Field(None, ge=0, le=1000)
    max_create_per_creator: Optional[int] = Field(None, ge=0, le=1000)
    max_create_per_hour: Optional[int] = Field(None, ge=0, le=10000)


class AutopilotReportCleanupRequest(BaseModel):
    """Archives report rows without deleting task history."""
    statuses: Optional[List[str]] = None
    older_than_days: Optional[int] = Field(None, ge=0, le=3650)
    creator_id: Optional[int] = Field(None, ge=1)


class AutopilotSkipNewVideosRequest(BaseModel):
    """Skips currently new videos so they won't be auto-commented later."""
    creator_id: Optional[int] = Field(None, ge=1)


class NotificationSettingsUpdate(BaseModel):
    enabled: bool


def _build_monitor_outbox_dedupe_key(
    event_type: str,
    creator_id: int | None,
    video_ids: list[int] | list[str],
    extra: str | None = None,
) -> str:
    normalized_ids = ",".join(str(item) for item in sorted({int(item) for item in video_ids}))
    suffix = f":{extra}" if extra else ""
    return f"{event_type}:{creator_id or 0}:{normalized_ids}{suffix}"


def _task_settings_dict(task: Task) -> dict[str, Any]:
    return task.settings if isinstance(task.settings, dict) else {}


def _task_creator_source_id(task: Task) -> int | None:
    raw_value = _task_settings_dict(task).get("creator_source_id")
    try:
        return int(raw_value)
    except (TypeError, ValueError):
        return None


def _remaining_task_refund(task: Task) -> Decimal:
    total = Decimal(str(task.total_cost or 0))
    already_refunded = Decimal(str(task.refunded_cost or 0))
    return max(total - already_refunded, Decimal("0.00"))


def _mark_video_released_from_autopilot(video: CreatorVideo, *, task_id: int, now: datetime) -> None:
    meta = dict(video.meta or {}) if isinstance(video.meta, dict) else {}
    meta["autopilot_cancelled_at"] = now.isoformat()
    meta["autopilot_cancelled_task_id"] = task_id
    video.meta = meta
    video.status = "new"
    video.task_id = None


def _creator_has_autopilot_template_reference(creator: CreatorSource) -> bool:
    if creator.default_template_id:
        return True
    template_ids = creator.template_ids
    return isinstance(template_ids, list) and bool(template_ids)


def _is_full_task_template(template: TaskTemplate | None) -> bool:
    if not template:
        return False
    payload = template.settings or {}
    raw_type = str(
        payload.get("template_type")
        or payload.get("template_kind")
        or payload.get("scope")
        or "full"
    ).strip().lower()
    return raw_type not in {"replies_only", "reply_only", "reply-only", "replies", "replies-only"}


async def _enqueue_existing_new_videos_for_autopilot(
    db: AsyncSession,
    creator: CreatorSource,
) -> tuple[int, int]:
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

    control = await _get_autopilot_control_state(db)
    if control["mode"] != AUTOPILOT_CONTROL_ACTIVE:
        return (0, len(video_ids))

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


async def _creator_needs_existing_new_video_autopilot(
    db: AsyncSession,
    creator: CreatorSource,
) -> bool:
    if not creator.id:
        return False

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
        return False

    dedupe_key = _build_monitor_outbox_dedupe_key("autopilot_create", creator.id, video_ids)
    existing = await db.execute(
        select(MonitorOutbox.id)
        .filter(
            MonitorOutbox.dedupe_key == dedupe_key,
            MonitorOutbox.status.in_(["pending", "failed", "processing"]),
        )
        .limit(1)
    )
    return existing.scalar_one_or_none() is None


class ReviewApprove(BaseModel):
    """Модель для подтверждения видео."""
    template_id: Optional[int] = None


class VideoTaskLink(BaseModel):
    """Модель для связи видео с задачей."""
    video_id: int
    task_id: int


class LinkVideosToTasksRequest(BaseModel):
    """Модель для массового связывания видео с задачами."""
    links: List[VideoTaskLink] = Field(..., description="Список связей video_id -> task_id")


@router.get("/monitoring/summary")
async def get_monitoring_summary(
    db: AsyncSession = Depends(get_db),
    _: None = Depends(verify_admin_header),
) -> dict:
    """Admin summary for monitoring state, backlog and monitor accounts."""
    from datetime import datetime, timezone, timedelta
    from db.models import MonitoredBlogger
    from services.account_service import get_monitor_pool_stats
    from services.metrics import get_metrics_summary
    from services.monitor_incident_service import (
        build_recommended_actions,
        get_automation_state,
        list_monitor_incidents,
    )
    from services.redis_service import get_executor_heartbeat, get_monitor_heartbeat, get_redis_client

    now = datetime.now(timezone.utc)
    redis_status = "ok"
    try:
        get_redis_client().ping()
    except Exception as exc:
        logger.warning(f"Monitoring summary redis ping failed: {exc}")
        redis_status = "error"

    monitor_alive, monitor_info = get_monitor_heartbeat()
    executor_alive, executor_info = get_executor_heartbeat()
    metrics = get_metrics_summary()
    gauges = metrics.get("gauges", {}) if isinstance(metrics, dict) else {}
    counters = metrics.get("counters", {}) if isinstance(metrics, dict) else {}
    timings = metrics.get("timings", {}) if isinstance(metrics, dict) else {}

    total_bloggers = (await db.execute(select(func.count(MonitoredBlogger.id)))).scalar_one() or 0
    enabled_subscriptions = (
        await db.execute(select(func.count(CreatorSource.id)).filter(CreatorSource.enabled == True))
    ).scalar_one() or 0
    locked_bloggers = (
        await db.execute(
            select(func.count(MonitoredBlogger.id)).filter(
                MonitoredBlogger.lock_until.isnot(None),
                MonitoredBlogger.lock_until > now,
            )
        )
    ).scalar_one() or 0
    stale_locks = (
        await db.execute(
            select(func.count(MonitoredBlogger.id)).filter(
                MonitoredBlogger.lock_until.isnot(None),
                MonitoredBlogger.lock_until <= now,
            )
        )
    ).scalar_one() or 0
    discovered_last_24h = (
        await db.execute(
            select(func.count(CreatorVideo.id)).filter(
                CreatorVideo.discovered_at >= now - timedelta(hours=24)
            )
        )
    ).scalar_one() or 0
    task_status_rows = (
        await db.execute(
            select(Task.status, func.count(Task.id)).group_by(Task.status)
        )
    ).all()
    task_statuses = {status: count for status, count in task_status_rows}
    failed_tasks_today = (
        await db.execute(
            select(func.count(Task.id)).filter(
                Task.status.in_(["error", "partial"]),
                Task.updated_at >= now - timedelta(hours=24),
            )
        )
    ).scalar_one() or 0
    stuck_in_progress = (
        await db.execute(
            select(func.count(Task.id)).filter(
                Task.status == "in_progress",
                Task.updated_at < now - timedelta(minutes=20),
            )
        )
    ).scalar_one() or 0
    video_status_rows = (
        await db.execute(
            select(CreatorVideo.status, func.count(CreatorVideo.id)).group_by(CreatorVideo.status)
        )
    ).all()
    video_statuses = {status: count for status, count in video_status_rows}
    account_pool = await db.run_sync(lambda sync_db: get_monitor_pool_stats(sync_db))
    runtime = {
        "due_backlog": int(gauges.get("monitor.queue_len", 0) or 0),
        "oldest_overdue_minutes": int(gauges.get("monitor.oldest_overdue_minutes", 0) or 0),
        "unscheduled_due": int(gauges.get("monitor.unscheduled_due", 0) or 0),
        "never_successful_due": int(gauges.get("monitor.never_successful_due", 0) or 0),
        "next_due_in_minutes": (
            None
            if float(gauges.get("monitor.next_due_in_minutes", -1) or -1) < 0
            else int(gauges.get("monitor.next_due_in_minutes", 0) or 0)
        ),
        "outbox_ready": int(gauges.get("monitor.outbox.ready", 0) or 0),
        "outbox_pending": int(gauges.get("monitor.outbox.pending", 0) or 0),
        "outbox_failed": int(gauges.get("monitor.outbox.failed", 0) or 0),
        "outbox_processing": int(gauges.get("monitor.outbox.processing", 0) or 0),
        "oldest_outbox_ready_minutes": int(gauges.get("monitor.outbox.oldest_ready_minutes", 0) or 0),
        "blogger_check_results": _extract_tagged_metric_values(counters, "monitor.blogger_check"),
        "blogger_check_timings": _extract_tagged_metric_values(timings, "monitor.blogger_check"),
    }
    attention: list[str] = []
    if redis_status != "ok":
        attention.append("redis_unavailable")
    if stale_locks:
        attention.append("stale_monitor_locks")
    if runtime["due_backlog"] > 0 and runtime["oldest_overdue_minutes"] >= 10:
        attention.append("monitor_backlog")
    if runtime["outbox_failed"] > 0:
        attention.append("monitor_outbox_failed")
    if stuck_in_progress:
        attention.append("executor_tasks_stuck")
    incidents = list_monitor_incidents(include_recovered=True)
    automation_state = get_automation_state(incidents)
    recommended_actions = build_recommended_actions(incidents)
    for incident in incidents:
        if incident.get("state") == "active":
            incident_type = str(incident.get("type") or "monitor_incident")
            if incident_type not in attention:
                attention.append(incident_type)

    with get_sync_db_context() as sync_db:
        monitor_max_parallel_browsers = get_monitor_max_parallel_browsers(sync_db)

    return {
        "system": {
            "redis": redis_status,
            "monitor_worker": "ok" if monitor_alive else "error",
            "executor": "ok" if executor_alive else "error",
            "monitor_info": monitor_info,
            "executor_info": executor_info,
            "queue_len": int(gauges.get("monitor.queue_len", 0) or 0),
            "browser_pool_browsers": int(gauges.get("browser_pool.browsers", 0) or 0),
            "browser_pool_contexts": int(gauges.get("browser_pool.contexts", 0) or 0),
            "browser_pool_pages": int(gauges.get("browser_pool.pages", 0) or 0),
            "browser_pool_reusable_pages": int(gauges.get("browser_pool.reusable_pages", 0) or 0),
            "monitor_max_parallel_browsers": int(monitor_max_parallel_browsers),
            "attention": attention,
        },
        "automation_state": automation_state,
        "incidents": incidents,
        "recommended_actions": recommended_actions,
        "bloggers": {
            "total": total_bloggers,
            "enabled_subscriptions": enabled_subscriptions,
            "locked": locked_bloggers,
            "stale_locks": stale_locks,
        },
        "runtime": runtime,
        "videos": {
            "discovered_last_24h": discovered_last_24h,
            "new": int(video_statuses.get("new", 0) or 0),
            "queued": int(video_statuses.get("queued", 0) or 0),
            "done": int(video_statuses.get("done", 0) or 0),
            "failed": int(video_statuses.get("failed", 0) or 0),
            "skipped": int(video_statuses.get("skipped", 0) or 0),
        },
        "tasks": {
            "pending": int(task_statuses.get("pending", 0) or 0),
            "in_progress": int(task_statuses.get("in_progress", 0) or 0),
            "done": int(task_statuses.get("done", 0) or 0),
            "error": int(task_statuses.get("error", 0) or 0),
            "partial": int(task_statuses.get("partial", 0) or 0),
            "failed_today": int(failed_tasks_today or 0),
            "stuck_in_progress": int(stuck_in_progress or 0),
        },
        "accounts": account_pool,
    }


@router.get("/notifications")
async def get_video_notifications(
    user_id: int = Query(..., description="ID пользователя"),
    db: AsyncSession = Depends(get_db),
    auth_user_id: int = Depends(verify_user_header),
) -> dict:
    """Returns the global video notification switch for this user."""
    ensure_user_scope(user_id, auth_user_id)
    result = await db.execute(select(User).filter(User.id == user_id))
    user = result.scalar_one_or_none()
    if not user:
        raise HTTPException(status_code=404, detail=f"User {user_id} not found")
    return {"enabled": bool(getattr(user, "video_notifications_enabled", True))}


@router.patch("/notifications")
async def update_video_notifications(
    payload: NotificationSettingsUpdate,
    user_id: int = Query(..., description="ID пользователя"),
    db: AsyncSession = Depends(get_db),
    auth_user_id: int = Depends(verify_user_header),
) -> dict:
    """Updates the global video notification switch for this user."""
    ensure_user_scope(user_id, auth_user_id)
    result = await db.execute(select(User).filter(User.id == user_id))
    user = result.scalar_one_or_none()
    if not user:
        raise HTTPException(status_code=404, detail=f"User {user_id} not found")

    user.video_notifications_enabled = payload.enabled
    enabled = bool(payload.enabled)
    await db.commit()
    return {"enabled": enabled}


@router.get("/creators")
async def list_creators(
    user_id: int = Query(..., description="ID пользователя для получения его блогеров"),
    db: AsyncSession = Depends(get_db),
    auth_user_id: int = Depends(verify_user_header),
) -> List[dict]:
    """ИСПРАВЛЕНО (P0): Получает список блогеров с данными из MonitoredBlogger (реальный источник истины)."""
    ensure_user_scope(user_id, auth_user_id)
    from db.models import MonitoredBlogger
    
    result = await db.execute(
        select(CreatorSource, MonitoredBlogger)
        .outerjoin(MonitoredBlogger, CreatorSource.monitored_blogger_id == MonitoredBlogger.id)
        .filter(CreatorSource.user_id == user_id)
        .order_by(CreatorSource.id.desc())
    )
    creators_data = result.all()
    
    # P1 FIX: Подсчитываем количество новых видео для каждого блогера
    from db.models import CreatorVideo
    
    creator_ids = [c.id for c, _ in creators_data]
    new_videos_query = select(
        CreatorVideo.creator_id,
        func.count(CreatorVideo.id).label("count")
    ).filter(
        CreatorVideo.creator_id.in_(creator_ids),
        CreatorVideo.status == "new"
    ).group_by(CreatorVideo.creator_id)
    
    new_videos_result = await db.execute(new_videos_query)
    new_videos_counts = {row[0]: row[1] for row in new_videos_result.all()}
    
    return [
        {
            "id": c.id,
            "user_id": c.user_id,
            "username": c.username,
            "enabled": c.enabled,
            "interval_minutes": c.interval_minutes,
            "mode": c.mode,
            "template_ids": c.template_ids,
            "default_template_id": c.default_template_id,  # УЛУЧШЕНО: Добавлен default_template_id
            "auto_start": c.auto_start,
            # P0 FIX: Используем данные из MonitoredBlogger (реальный источник истины)
            "last_checked_at": (m.last_checked_at.isoformat() if m and m.last_checked_at else None),
            "last_seen_video_id": (m.last_seen_video_id if m else None),
            # P1 FIX: Добавлено количество новых видео
            "new_videos_count": new_videos_counts.get(c.id, 0),
            "created_at": c.created_at.isoformat() if c.created_at else None,
            "updated_at": c.updated_at.isoformat() if c.updated_at else None,
        }
        for c, m in creators_data
    ]


@router.post("/creators/bulk-update")
async def bulk_update_creators(
    payload: CreatorBulkUpdate,
    user_id: int = Query(..., description="User id that owns the creators"),
    db: AsyncSession = Depends(get_db),
    auth_user_id: int = Depends(verify_user_header),
) -> dict:
    """Preview or apply a bulk update to the current user's creator subscriptions."""
    ensure_user_scope(user_id, auth_user_id)

    selector = payload.selector or CreatorBulkSelector()
    changes = payload.changes
    scope = (selector.scope or "all").strip().lower()
    allowed_scopes = {
        "all",
        "enabled",
        "disabled",
        "auto_comment",
        "manual_review",
        "notify_only",
        "without_default_template",
        "with_new_videos",
        "explicit",
    }
    if scope not in allowed_scopes:
        raise HTTPException(status_code=400, detail=f"Unsupported bulk scope: {scope}")

    if changes.mode is not None and changes.mode not in ["auto_comment", "manual_review", "notify_only"]:
        raise HTTPException(status_code=400, detail="mode must be one of: auto_comment, manual_review, notify_only")

    has_field_change = any(
        value is not None
        for value in (
            changes.enabled,
            changes.interval_minutes,
            changes.mode,
            changes.default_template_id,
        )
    )
    if not has_field_change and not changes.run_now:
        raise HTTPException(status_code=400, detail="No bulk changes requested")

    if await _is_parsing_mode_restricted(db, user_id):
        raise HTTPException(
            status_code=403,
            detail="Software is in parsing mode: bulk creator actions are disabled",
        )

    if changes.default_template_id and changes.default_template_id > 0:
        template_result = await db.execute(
            select(TaskTemplate).filter(
                TaskTemplate.id == changes.default_template_id,
                TaskTemplate.user_id == user_id,
            )
        )
        template = template_result.scalar_one_or_none()
        if not template:
            raise HTTPException(
                status_code=404,
                detail=f"Default template {changes.default_template_id} not found or does not belong to user",
            )
        if not _is_full_task_template(template):
            raise HTTPException(status_code=400, detail="Default template must be a full comment template")

    stmt = select(CreatorSource).filter(CreatorSource.user_id == user_id)
    explicit_ids = []
    if selector.creator_ids:
        explicit_ids = list(dict.fromkeys(int(item) for item in selector.creator_ids if int(item) > 0))
    if scope == "explicit":
        if not explicit_ids:
            raise HTTPException(status_code=400, detail="creator_ids are required for explicit bulk scope")
        stmt = stmt.filter(CreatorSource.id.in_(explicit_ids))
    elif explicit_ids:
        stmt = stmt.filter(CreatorSource.id.in_(explicit_ids))

    if scope == "enabled":
        stmt = stmt.filter(CreatorSource.enabled == True)
    elif scope == "disabled":
        stmt = stmt.filter(CreatorSource.enabled == False)
    elif scope in {"auto_comment", "manual_review", "notify_only"}:
        stmt = stmt.filter(CreatorSource.mode == scope)
    elif scope == "without_default_template":
        stmt = stmt.filter(CreatorSource.default_template_id.is_(None))
    elif scope == "with_new_videos":
        new_video_creators = select(CreatorVideo.creator_id).filter(CreatorVideo.status == "new")
        stmt = stmt.filter(CreatorSource.id.in_(new_video_creators))

    result = await db.execute(stmt.order_by(CreatorSource.username.asc(), CreatorSource.id.asc()))
    creators = result.scalars().all()

    skipped: list[dict[str, Any]] = []
    eligible: list[CreatorSource] = []

    def _skip(creator: CreatorSource, reason: str) -> None:
        skipped.append({"id": creator.id, "username": creator.username, "reason": reason})

    for creator in creators:
        target_enabled = changes.enabled if changes.enabled is not None else bool(creator.enabled)
        target_mode = changes.mode if changes.mode is not None else creator.mode
        if changes.default_template_id is None:
            target_default_template_id = creator.default_template_id
        elif changes.default_template_id > 0:
            target_default_template_id = changes.default_template_id
        else:
            target_default_template_id = None

        requires_auto_template = (
            target_enabled
            and target_mode == "auto_comment"
            and not target_default_template_id
            and (
                changes.mode == "auto_comment"
                or changes.enabled is True
                or changes.run_now
                or changes.default_template_id == 0
            )
        )
        if requires_auto_template:
            _skip(creator, "auto_comment_requires_default_template")
            continue

        changed = False
        if changes.enabled is not None and bool(creator.enabled) != bool(changes.enabled):
            changed = True
        if changes.interval_minutes is not None and int(creator.interval_minutes or 0) != int(changes.interval_minutes):
            changed = True
        if changes.mode is not None and creator.mode != changes.mode:
            changed = True
        if changes.default_template_id is not None and (creator.default_template_id or 0) != int(changes.default_template_id or 0):
            changed = True
        if changes.run_now and target_enabled:
            changed = True
        elif changes.run_now and not target_enabled:
            _skip(creator, "disabled_creator_not_queued")
            continue

        if (
            not changed
            and target_enabled
            and target_mode == "auto_comment"
            and bool(target_default_template_id)
            and await _creator_needs_existing_new_video_autopilot(db, creator)
        ):
            changed = True

        if not changed:
            _skip(creator, "already_matching")
            continue
        eligible.append(creator)

    skipped_by_reason: dict[str, int] = {}
    for item in skipped:
        reason = str(item.get("reason") or "skipped")
        skipped_by_reason[reason] = skipped_by_reason.get(reason, 0) + 1

    response_base = {
        "ok": True,
        "dry_run": bool(payload.dry_run),
        "scope": scope,
        "matched_count": len(creators),
        "updated_count": len(eligible),
        "skipped_count": len(skipped),
        "skipped_by_reason": skipped_by_reason,
        "updated_preview": [
            {"id": creator.id, "username": creator.username}
            for creator in eligible[:15]
        ],
        "skipped_preview": skipped[:15],
        "changes": changes.model_dump(),
    }
    if payload.dry_run:
        return response_base

    cancel_autopilot_creator_ids: list[int] = []
    schedule_blogger_ids: set[int] = set()
    run_now_blogger_ids: set[int] = set()
    autopilot_existing_video_events_queued = 0
    autopilot_existing_video_count = 0

    for creator in eligible:
        old_mode = creator.mode
        if changes.enabled is not None:
            creator.enabled = bool(changes.enabled)
            if creator.monitored_blogger_id:
                schedule_blogger_ids.add(creator.monitored_blogger_id)
        if changes.interval_minutes is not None:
            creator.interval_minutes = int(changes.interval_minutes)
            if creator.monitored_blogger_id:
                schedule_blogger_ids.add(creator.monitored_blogger_id)
        if changes.mode is not None:
            creator.mode = changes.mode
        if changes.default_template_id is not None:
            creator.default_template_id = changes.default_template_id if changes.default_template_id > 0 else None
        if old_mode == "auto_comment" and (creator.mode != "auto_comment" or not creator.enabled):
            cancel_autopilot_creator_ids.append(creator.id)
        if changes.run_now and creator.monitored_blogger_id and creator.enabled:
            run_now_blogger_ids.add(creator.monitored_blogger_id)

    if cancel_autopilot_creator_ids:
        await db.execute(
            update(MonitorOutbox)
            .where(
                MonitorOutbox.creator_id.in_(cancel_autopilot_creator_ids),
                MonitorOutbox.event_type == "autopilot_create",
                MonitorOutbox.status.in_(["pending", "failed", "processing"]),
            )
            .values(
                status="cancelled",
                lock_owner=None,
                locked_at=None,
                last_error="cancelled_by_bulk_creator_disabled_or_mode_change",
            )
        )

    await db.flush()
    for creator in eligible:
        if (
            creator.enabled
            and creator.mode == "auto_comment"
            and _creator_has_autopilot_template_reference(creator)
        ):
            queued_events, queued_videos = await _enqueue_existing_new_videos_for_autopilot(db, creator)
            autopilot_existing_video_events_queued += queued_events
            autopilot_existing_video_count += queued_videos

    for blogger_id in sorted(schedule_blogger_ids):
        await _recalculate_monitored_blogger_schedule(db, blogger_id)

    run_now_queued = 0
    run_now_locked = 0
    if run_now_blogger_ids:
        monitor_result = await db.execute(
            select(MonitoredBlogger).filter(MonitoredBlogger.id.in_(run_now_blogger_ids))
        )
        now = datetime.now(timezone.utc)
        for monitored in monitor_result.scalars().all():
            lock_until = monitored.lock_until
            if lock_until and lock_until.tzinfo is None:
                lock_until = lock_until.replace(tzinfo=timezone.utc)
            if lock_until and lock_until > now:
                run_now_locked += 1
                continue
            monitored.next_check_at = None
            if lock_until:
                monitored.lock_until = None
                monitored.lock_owner = None
            run_now_queued += 1

    await db.commit()

    response_base.update(
        {
            "dry_run": False,
            "cancelled_autopilot_events_for": len(cancel_autopilot_creator_ids),
            "autopilot_existing_video_events_queued": autopilot_existing_video_events_queued,
            "autopilot_existing_video_count": autopilot_existing_video_count,
            "run_now_queued_bloggers": run_now_queued,
            "run_now_locked_bloggers": run_now_locked,
        }
    )
    logger.info(
        "Bulk creator update applied for user_id=%s scope=%s matched=%s updated=%s skipped=%s",
        user_id,
        scope,
        len(creators),
        len(eligible),
        len(skipped),
    )
    return response_base


@router.get("/creators/suggestions")
async def list_creator_suggestions(
    user_id: int = Query(..., description="ID пользователя, для которого нужны подсказки"),
    limit: int = Query(8, ge=1, le=100, description="Максимум подсказок"),
    offset: int = Query(0, ge=0, description="Смещение для пагинации"),
    include_total: bool = Query(False, description="Вернуть items/total вместо простого списка"),
    db: AsyncSession = Depends(get_db),
    auth_user_id: int = Depends(verify_user_header),
) -> List[dict] | dict:
    """Возвращает блогеров, которые уже есть у других сотрудников, но ещё не добавлены у пользователя."""
    ensure_user_scope(user_id, auth_user_id)
    from db.models import MonitoredBlogger

    normalized_username = func.lower(CreatorSource.username)
    user_usernames_subq = select(func.lower(CreatorSource.username)).filter(
        CreatorSource.user_id == user_id
    )

    suggestions_base_stmt = (
        select(
            normalized_username.label("username"),
            func.count(func.distinct(CreatorSource.user_id)).label("users_count"),
            func.max(MonitoredBlogger.last_checked_at).label("last_checked_at"),
        )
        .outerjoin(MonitoredBlogger, CreatorSource.monitored_blogger_id == MonitoredBlogger.id)
        .filter(CreatorSource.user_id.isnot(None))
        .filter(CreatorSource.user_id != user_id)
        .filter(CreatorSource.enabled == True)
        .filter(~normalized_username.in_(user_usernames_subq))
        .group_by(normalized_username)
    )

    if include_total:
        total = (
            await db.execute(select(func.count()).select_from(suggestions_base_stmt.subquery()))
        ).scalar_one() or 0
    else:
        total = None

    suggestions_stmt = (
        suggestions_base_stmt
        .order_by(
            func.count(func.distinct(CreatorSource.user_id)).desc(),
            func.max(MonitoredBlogger.last_checked_at).desc(),
            normalized_username.asc(),
        )
        .offset(offset)
        .limit(limit)
    )

    rows = (await db.execute(suggestions_stmt)).all()
    items = [
        {
            "username": username,
            "users_count": int(users_count or 0),
            "last_checked_at": last_checked_at.isoformat() if last_checked_at else None,
        }
        for username, users_count, last_checked_at in rows
    ]
    if include_total:
        return {
            "items": items,
            "total": int(total or 0),
            "limit": limit,
            "offset": offset,
        }
    return items


@router.post("/creators")
@limiter.limit("1/second")  # КРИТИЧНО: Rate limit - не более 1 запроса в секунду для защиты от DoS
async def create_creator(
    request: Request,
    payload: CreatorCreate,
    user_id: int = Query(..., description="ID пользователя, создающего блогера"),
    db: AsyncSession = Depends(get_db),
    auth_user_id: int = Depends(verify_user_header),
) -> dict:
    """ИСПРАВЛЕНО: Создаёт нового блогера для конкретного пользователя (персональный блогер)."""
    ensure_user_scope(user_id, auth_user_id)
    username = _normalize_creator_username(payload.username)
    if not username:
        raise HTTPException(status_code=400, detail="username required")
    
    # Проверяем, что пользователь существует
    user_result = await db.execute(select(User).filter(User.id == user_id))
    user = user_result.scalar_one_or_none()
    if not user:
        raise HTTPException(status_code=404, detail=f"User {user_id} not found")
    
    # КРИТИЧНО: Проверяем quota на количество блогеров (защита от DoS)
    # Админы (role="admin" или role="owner") не имеют ограничений
    is_admin = user.role in ("admin", "owner")
    if not is_admin:
        # Подсчитываем количество активных блогеров у пользователя
        creators_count_result = await db.execute(
            select(func.count(CreatorSource.id)).filter(
                CreatorSource.user_id == user_id
            )
        )
        creators_count = creators_count_result.scalar_one() or 0
        
        if creators_count >= settings.MAX_CREATORS_PER_USER:
            # Keep "creator quota exceeded" + "Maximum N creators ... you have"
            # phrasing so the bot's _is_creator_quota_error parser still matches
            # and shows the friendly Russian message. Remove the env-var name —
            # exposing internal config knobs to end users is a leak.
            raise HTTPException(
                status_code=403,
                detail=(
                    f"Creator quota exceeded. Maximum {settings.MAX_CREATORS_PER_USER} creators per user. "
                    f"You have {creators_count} creators. Delete old creators or contact administrator."
                ),
            )
    
    # ИСПРАВЛЕНО: Проверяем уникальность username в рамках одного user_id (не глобально)
    existing = await db.execute(
        select(CreatorSource).filter(
            CreatorSource.user_id == user_id,
            func.lower(CreatorSource.username) == username
        )
    )
    if existing.scalar_one_or_none():
        raise HTTPException(status_code=400, detail=f"Creator @{username} already exists for this user")

    monitored_blogger = await _load_monitored_blogger_by_username(db, username)
    await _ensure_unique_creator_capacity(db, monitored_blogger)
    
    # КРИТИЧНО: Проверяем существование блогера в TikTok перед сохранением в БД (легковесный HTTP запрос)
    # Это предотвращает засорение БД несуществующими блогерами
    if settings.VALIDATE_CREATOR_PROFILE_ON_ADD:
        profile_url = f"https://www.tiktok.com/@{username}"
        try:
            async with aiohttp.ClientSession() as session:
                async with session.get(profile_url, allow_redirects=False, timeout=aiohttp.ClientTimeout(total=5)) as resp:
                    if resp.status == 404:
                        raise HTTPException(status_code=400, detail=f"TikTok profile @{username} not found (404)")
                    elif resp.status >= 500:
                        logger.warning(f"TikTok server error when checking @{username}: HTTP {resp.status}")
                    elif resp.status not in (200, 301, 302, 307, 308):
                        logger.debug(f"TikTok returned status {resp.status} for @{username}, allowing creation")
        except aiohttp.ClientError as e:
            logger.debug(f"Network error when checking @{username}: {e}, allowing creation")
        except asyncio.TimeoutError:
            logger.debug(f"Timeout when checking @{username}, allowing creation")
    else:
        logger.debug(
            "Creator profile validation on add is disabled; skipping direct TikTok profile request for @%s",
            username,
        )

    
    # Валидация уже выполнена Pydantic, но оставляем для явности
    if payload.mode not in ["auto_comment", "manual_review", "notify_only"]:
        raise HTTPException(status_code=400, detail="mode must be one of: auto_comment, manual_review, notify_only")

    parsing_mode = await _is_parsing_mode_enabled(db) and not has_full_functionality_access(user)
    creator_mode = "notify_only" if parsing_mode else payload.mode
    creator_template_ids = None if parsing_mode else payload.template_ids
    creator_auto_start = False if parsing_mode else payload.auto_start
    
    # Дополнительная валидация interval_minutes (Pydantic уже проверил, но для явности)
    if payload.interval_minutes < 5 or payload.interval_minutes > 1440:
        raise HTTPException(status_code=400, detail="interval_minutes must be between 5 and 1440 minutes")
    
    # --- PROXY OPTIMIZATION ---
    # 1. Проверяем, есть ли уже MonitoredBlogger для этого username (любого пользователя)
    
    # Если нет, создаем нового
    is_new_monitored = False
    if not monitored_blogger:
        monitored_blogger = MonitoredBlogger(
            username=username
        )
        db.add(monitored_blogger)
        await db.flush()  # ÐŸÐ¾Ð»ÑƒÑ‡Ð°ÐµÐ¼ ID
        is_new_monitored = True
    
    # 2. Создаем CreatorSource, привязанный к MonitoredBlogger
    creator = CreatorSource(
        user_id=user_id,
        username=username,
        # Привязываем к единому блогеру
        monitored_blogger_id=monitored_blogger.id,
        enabled=payload.enabled,
        interval_minutes=payload.interval_minutes,
        mode=creator_mode,
        template_ids=creator_template_ids,
        auto_start=creator_auto_start,
    )
    db.add(creator)
    await db.flush()
    force_due = is_new_monitored or monitored_blogger.next_check_at is None
    await _recalculate_monitored_blogger_schedule(
        db,
        monitored_blogger.id,
        force_due_if_unchecked=force_due,
    )
    await db.commit()
    await db.refresh(creator)

    # 3. Воркер мониторинга (отдельный процесс) автоматически подхватит
    # нового блогера, так как у него last_checked_at = None и next_check_at = None.
    # Запускать Playwright (через asyncio.create_task) внутри API процесса на Windows
    # приводит к падению Uvicorn (NotImplementedError: create_subprocess_exec).
    
    return {
        "id": creator.id,
        "user_id": creator.user_id,
        "username": creator.username,
        "monitored_blogger_id": monitored_blogger.id,
        "enabled": creator.enabled,
        "interval_minutes": creator.interval_minutes,
        "mode": creator.mode,
        "template_ids": creator.template_ids,
        "auto_start": creator.auto_start,
        "created_at": creator.created_at.isoformat() if creator.created_at else None,
    }


@router.patch("/creators/{creator_id}")
async def update_creator(
    creator_id: int,
    payload: CreatorUpdate,
    user_id: int = Query(..., description="ID пользователя, владельца блогера"),
    auth_user_id: int = Depends(verify_user_header),
    db: AsyncSession = Depends(get_db)
) -> dict:
    """ИСПРАВЛЕНО: Обновляет настройки блогера. Только владелец может изменять своего блогера."""
    ensure_user_scope(user_id, auth_user_id)

    result = await db.execute(
        select(CreatorSource).filter(CreatorSource.id == creator_id)
    )
    creator = result.scalar_one_or_none()
    
    if not creator:
        raise HTTPException(status_code=404, detail="Creator not found")
    
    # ИСПРАВЛЕНО: Проверяем, что пользователь является владельцем блогера
    if creator.user_id != user_id:
        raise HTTPException(status_code=403, detail="You can only update your own creators")

    parsing_mode = await _is_parsing_mode_restricted(db, user_id)
    if parsing_mode and (
        (payload.mode is not None and payload.mode != "notify_only")
        or payload.template_ids is not None
        or payload.default_template_id is not None
    ):
        raise HTTPException(
            status_code=403,
            detail="Software is in parsing mode: comment modes and templates are disabled",
        )

    monitored_blogger_id = creator.monitored_blogger_id
    schedule_affecting_change = payload.enabled is not None or payload.interval_minutes is not None
    cancel_pending_autopilot = False
    
    # Обновляем поля (валидация уже выполнена Pydantic)
    if payload.enabled is not None:
        if payload.enabled is False and creator.mode == "auto_comment":
            cancel_pending_autopilot = True
        creator.enabled = payload.enabled
    if payload.interval_minutes is not None:
        # Pydantic уже проверил диапазон, но оставляем явную проверку для надёжности
        if payload.interval_minutes < 5 or payload.interval_minutes > 1440:
            raise HTTPException(status_code=400, detail="interval_minutes must be between 5 and 1440 minutes")
        creator.interval_minutes = payload.interval_minutes
    old_mode = creator.mode
    if payload.mode is not None:
        if payload.mode not in ["auto_comment", "manual_review", "notify_only"]:
            raise HTTPException(status_code=400, detail="mode must be one of: auto_comment, manual_review, notify_only")
        creator.mode = payload.mode
        if old_mode == "auto_comment" and payload.mode != "auto_comment":
            cancel_pending_autopilot = True
    if payload.template_ids is not None:
        # ИСПРАВЛЕНО: Валидируем шаблоны и проверяем, что они принадлежат пользователю
        if payload.template_ids:
            for template_id in payload.template_ids:
                template_result = await db.execute(
                    select(TaskTemplate).filter(
                        TaskTemplate.id == template_id,
                        TaskTemplate.user_id == user_id  # Проверяем принадлежность шаблона
                    )
                )
                template = template_result.scalar_one_or_none()
                if not template:
                    raise HTTPException(status_code=404, detail=f"Template {template_id} not found or does not belong to user")
                if not _is_full_task_template(template):
                    raise HTTPException(status_code=400, detail=f"Template {template_id} is replies-only and cannot be used for auto-commenting")
        creator.template_ids = payload.template_ids
    # P1 FIX: Поддержка default_template_id для авто-комментирования
    if payload.default_template_id is not None:
        if payload.default_template_id > 0:
            # Валидируем, что шаблон существует и принадлежит пользователю
            template_result = await db.execute(
                select(TaskTemplate).filter(
                    TaskTemplate.id == payload.default_template_id,
                    TaskTemplate.user_id == user_id
                )
            )
            template = template_result.scalar_one_or_none()
            if not template:
                raise HTTPException(status_code=404, detail=f"Default template {payload.default_template_id} not found or does not belong to user")
            if not _is_full_task_template(template):
                raise HTTPException(status_code=400, detail="Default template must be a full comment template")
            creator.default_template_id = payload.default_template_id
        else:
            creator.default_template_id = None
    if payload.auto_start is not None:
        creator.auto_start = payload.auto_start

    if cancel_pending_autopilot:
        await db.execute(
            update(MonitorOutbox)
            .where(
                MonitorOutbox.creator_id == creator_id,
                MonitorOutbox.event_type == "autopilot_create",
                MonitorOutbox.status.in_(["pending", "failed", "processing"]),
            )
            .values(
                status="cancelled",
                lock_owner=None,
                locked_at=None,
                last_error="cancelled_by_creator_disabled_or_mode_change",
            )
        )

    if schedule_affecting_change:
        await db.flush()
        await _recalculate_monitored_blogger_schedule(db, monitored_blogger_id)
    
    await db.commit()
    await db.refresh(creator)
    
    logger.info(f"Updated creator {creator_id}: @{creator.username} for user {user_id}")
    
    return {
        "id": creator.id,
        "user_id": creator.user_id,
        "username": creator.username,
        "enabled": creator.enabled,
        "interval_minutes": creator.interval_minutes,
        "mode": creator.mode,
        "template_ids": creator.template_ids,
        "default_template_id": creator.default_template_id,  # P1 FIX: Добавляем default_template_id в ответ
        "auto_start": creator.auto_start,
        "updated_at": creator.updated_at.isoformat() if creator.updated_at else None,
    }


@router.delete("/creators/{creator_id}")
async def delete_creator(
    creator_id: int,
    user_id: int = Query(..., description="ID пользователя, владельца блогера"),
    db: AsyncSession = Depends(get_db),
    auth_user_id: int = Depends(verify_user_header),
) -> dict:
    """ИСПРАВЛЕНО: Удаляет блогера. Только владелец может удалить своего блогера."""
    ensure_user_scope(user_id, auth_user_id)
    result = await db.execute(
        select(CreatorSource).filter(CreatorSource.id == creator_id)
    )
    creator = result.scalar_one_or_none()
    
    if not creator:
        raise HTTPException(status_code=404, detail="Creator not found")
    
    # ИСПРАВЛЕНО: Проверяем, что пользователь является владельцем блогера
    if creator.user_id != user_id:
        raise HTTPException(status_code=403, detail="You can only delete your own creators")
    
    username = creator.username
    monitored_blogger_id = creator.monitored_blogger_id
    # ИСПРАВЛЕНО: Используем правильный синтаксис для async SQLAlchemy
    from sqlalchemy import delete
    await db.execute(delete(CreatorSource).filter(CreatorSource.id == creator_id))
    await db.flush()
    await _recalculate_monitored_blogger_schedule(db, monitored_blogger_id)
    await db.commit()
    
    logger.info(f"Deleted creator: @{username} for user {user_id}")
    
    return {"ok": True, "message": f"Creator @{username} deleted"}


@router.delete("/admin/creators/all")
async def delete_all_bloggers_from_db(
    db: AsyncSession = Depends(get_db),
    _: None = Depends(verify_admin_header),
) -> dict:
    """Удаляет всех блогеров (и их видео) из базы данных (только для админов)."""
    from sqlalchemy import delete
    from db.models import CreatorSource, MonitoredBlogger, CreatorVideo, VideoBatch, MonitorOutbox
    
    # В SQLite по умолчанию отключены foreign keys, поэтому при bulk delete
    # каскадное удаление (ON DELETE CASCADE) не сработает автоматически.
    # Явно удаляем зависимые записи:
    await db.execute(delete(CreatorVideo))
    await db.execute(delete(VideoBatch))
    await db.execute(delete(MonitorOutbox))
    
    # Затем удаляем основные записи:
    await db.execute(delete(CreatorSource))
    await db.execute(delete(MonitoredBlogger))
    await db.commit()
    
    logger.info("Admin deleted all bloggers from database")
    return {"ok": True, "message": "All bloggers have been deleted"}


@router.post("/creators/{creator_id}/run-now")
async def run_now(
    creator_id: int,
    user_id: int = Query(..., description="ID пользователя, владельца блогера"),
    db: AsyncSession = Depends(get_db),
    auth_user_id: int = Depends(verify_user_header),
) -> dict:
    """Ставит глобального блогера в очередь на ближайший тик мониторинга."""
    ensure_user_scope(user_id, auth_user_id)
    
    result = await db.execute(
        select(CreatorSource).filter(CreatorSource.id == creator_id)
    )
    creator = result.scalar_one_or_none()
    
    if not creator:
        raise HTTPException(status_code=404, detail="Creator not found")
    
    # ИСПРАВЛЕНО: Проверяем, что пользователь является владельцем блогера
    if creator.user_id != user_id:
        raise HTTPException(status_code=403, detail="You can only run checks for your own creators")
    
    message = f"Creator @{creator.username} will be checked on next cycle"
    if creator.monitored_blogger_id:
        monitored_result = await db.execute(
            select(MonitoredBlogger).filter(MonitoredBlogger.id == creator.monitored_blogger_id)
        )
        monitored = monitored_result.scalar_one_or_none()
        if monitored:
            now = datetime.now(timezone.utc)
            lock_until = monitored.lock_until
            if lock_until and lock_until.tzinfo is None:
                lock_until = lock_until.replace(tzinfo=timezone.utc)

            if lock_until and lock_until > now:
                logger.info(
                    "Run-now ignored for @%s (id=%s): check is already locked until %s",
                    creator.username,
                    monitored.id,
                    lock_until.isoformat(),
                )
                return {"ok": True, "message": f"Creator @{creator.username} is already being checked"}

            cooldown_minutes = max(0, int(getattr(settings, "VIDEO_CREATOR_RUN_NOW_COOLDOWN_MINUTES", 3) or 0))
            last_attempt_at = monitored.last_attempt_at
            if last_attempt_at and last_attempt_at.tzinfo is None:
                last_attempt_at = last_attempt_at.replace(tzinfo=timezone.utc)

            if cooldown_minutes and last_attempt_at and now - last_attempt_at < timedelta(minutes=cooldown_minutes):
                earliest_next_check = last_attempt_at + timedelta(minutes=cooldown_minutes)
                current_next_check_at = monitored.next_check_at
                if current_next_check_at and current_next_check_at.tzinfo is None:
                    current_next_check_at = current_next_check_at.replace(tzinfo=timezone.utc)
                if current_next_check_at is None or current_next_check_at != earliest_next_check:
                    monitored.next_check_at = earliest_next_check
                message = f"Creator @{creator.username} was recently checked and is queued for the next cycle"
                logger.info(
                    "Run-now queued for @%s (id=%s) during cooldown; last_attempt_at=%s",
                    creator.username,
                    monitored.id,
                    last_attempt_at.isoformat(),
                )
            else:
                monitored.next_check_at = None
                if lock_until:
                    monitored.lock_until = None
                    monitored.lock_owner = None
                logger.info(f"Run-now: queued MonitoredBlogger @{creator.username} (id={monitored.id})")
            await db.commit()
        else:
            logger.warning(
                "Run-now: MonitoredBlogger %s not found for creator %s",
                creator.monitored_blogger_id, creator_id,
            )
            raise HTTPException(
                status_code=503,
                detail="Blogger is not connected to the monitoring system. Please contact the administrator.",
            )
    else:
        logger.warning("Run-now: Creator %s has no monitored_blogger_id", creator_id)
        raise HTTPException(
            status_code=503,
            detail="Blogger monitoring is not set up for this creator. Please contact the administrator.",
        )
    
    logger.info(f"Run-now requested for creator: @{creator.username} by user {user_id}")
    
    return {"ok": True, "message": message}


@router.get("/review-queue")
async def review_queue(
    user_id: int = Query(..., description="ID пользователя для получения его видео на проверку"),
    limit: int = Query(20, ge=1, le=100),
    offset: int = Query(0, ge=0),
    db: AsyncSession = Depends(get_db),
    auth_user_id: int = Depends(verify_user_header),
) -> List[dict]:
    """P1 FIX: Получает новые видео (статус new) для пользователя. Устаревшая функция - используйте /api/video-inbox/videos."""
    ensure_user_scope(user_id, auth_user_id)
    # P1 FIX: Возвращаем видео со статусом "new" вместо несуществующего "pending_review"
    result = await db.execute(
        select(CreatorVideo)
        .join(CreatorSource, CreatorVideo.creator_id == CreatorSource.id)
        .filter(
            CreatorVideo.status == "new",  # P1 FIX: Используем "new" вместо "pending_review"
            CreatorSource.user_id == user_id  # Только блогеры пользователя
        )
        .order_by(CreatorVideo.discovered_at.desc())
        .offset(offset)
        .limit(limit)
    )
    videos = result.scalars().all()
    
    # Загружаем связанных блогеров (они уже отфильтрованы по user_id)
    creator_ids = {v.creator_id for v in videos}
    creators_result = await db.execute(
        select(CreatorSource).filter(CreatorSource.id.in_(creator_ids))
    )
    creators = {c.id: c for c in creators_result.scalars().all()}
    
    return [
        {
            "id": v.id,
            "creator_id": v.creator_id,
            "creator_username": creators.get(v.creator_id).username if v.creator_id in creators and creators.get(v.creator_id) else None,
            "video_id": v.video_id,
            "url": v.url,
            "status": v.status,
            "discovered_at": v.discovered_at.isoformat() if v.discovered_at else None,
            **_video_time_payload(v),
        }
        for v in videos
    ]


@router.post("/review/{video_id}/approve")
async def approve_video(
    video_id: int,
    payload: ReviewApprove,
    user_id: int = Query(..., description="Owner user ID for the target video"),
    db: AsyncSession = Depends(get_db),
    auth_user_id: int = Depends(verify_user_header),
) -> dict:
    """Approves a discovered video and creates a task via the shared monitoring flow."""
    ensure_user_scope(user_id, auth_user_id)
    if await _is_parsing_mode_restricted(db, user_id):
        raise HTTPException(
            status_code=403,
            detail="Software is in parsing mode: comment orders are disabled",
        )

    result = await db.execute(
        select(CreatorVideo).filter(CreatorVideo.id == video_id)
    )
    video = result.scalar_one_or_none()

    if not video:
        raise HTTPException(status_code=404, detail="Video not found")

    if video.status != "new":
        raise HTTPException(status_code=400, detail=f"Video is not in 'new' status (current: {video.status})")

    creator_result = await db.execute(
        select(CreatorSource).filter(CreatorSource.id == video.creator_id)
    )
    creator = creator_result.scalar_one_or_none()

    if not creator:
        raise HTTPException(status_code=404, detail="Creator not found")
    if creator.user_id != user_id:
        raise HTTPException(status_code=403, detail="You can only approve videos from your own creators")

    template_id = payload.template_id
    if not template_id:
        if creator.default_template_id:
            template_id = creator.default_template_id
        else:
            template_ids = creator.template_ids or []
            if not template_ids:
                raise HTTPException(status_code=400, detail="No templates configured for creator")
            template_id = template_ids[0]

    template_result = await db.execute(
        select(TaskTemplate).filter(
            TaskTemplate.id == template_id,
            TaskTemplate.user_id == user_id,
        )
    )
    template = template_result.scalar_one_or_none()

    if not template:
        raise HTTPException(status_code=404, detail=f"Template {template_id} not found or does not belong to user")
    if not _is_full_task_template(template):
        raise HTTPException(status_code=400, detail="Template must be a full comment template")

    creation_result = await create_monitored_task_async(
        db,
        creator,
        video_id=video.id,
        template=template,
        origin="manual_review",
        created_from="video_parsing_api",
    )

    if creation_result.status == "insufficient_funds":
        raise HTTPException(
            status_code=402,
            detail=f"Insufficient balance. Required: {creation_result.cost} UAH",
        )
    if creation_result.status in {"skipped", "missing_video"}:
        raise HTTPException(
            status_code=409,
            detail=f"Video can no longer be approved ({creation_result.reason})",
        )
    if creation_result.status != "created" or creation_result.task_id is None:
        raise HTTPException(
            status_code=500,
            detail=f"Failed to create task from review: {creation_result.reason or creation_result.status}",
        )

    logger.info(f"Approved video {video_id} by user {user_id}, created task {creation_result.task_id}")
    return {"ok": True, "task_id": creation_result.task_id}


@router.post("/review/{video_id}/reject")
async def reject_video(
    video_id: int,
    user_id: int = Query(..., description="ID пользователя, владельца видео"),
    db: AsyncSession = Depends(get_db),
    auth_user_id: int = Depends(verify_user_header),
) -> dict:
    """P1 FIX: Пропускает видео (ставит статус skipped). Работает со статусом new из Inbox."""
    ensure_user_scope(user_id, auth_user_id)
    result = await db.execute(
        select(CreatorVideo).filter(CreatorVideo.id == video_id)
    )
    video = result.scalar_one_or_none()
    
    if not video:
        raise HTTPException(status_code=404, detail="Video not found")
    
    # P1 FIX: Проверяем что видео в статусе "new" (можно пропустить)
    if video.status != "new":
        raise HTTPException(status_code=400, detail=f"Can only skip videos with status 'new' (current: {video.status})")
    
    # Получаем блогера для проверки прав
    creator_result = await db.execute(
        select(CreatorSource).filter(CreatorSource.id == video.creator_id)
    )
    creator = creator_result.scalar_one_or_none()
    
    if not creator:
        raise HTTPException(status_code=404, detail="Creator not found")
    
    # ИСПРАВЛЕНО: Проверяем, что пользователь является владельцем блогера
    if creator.user_id != user_id:
        raise HTTPException(status_code=403, detail="You can only skip videos from your own creators")
    
    # P1 FIX: Ставим статус "skipped" (стандартизированный статус)
    video.status = "skipped"
    await db.commit()
    
    logger.info(f"Rejected video {video_id} by user {user_id}")
    
    return {"ok": True}


@router.get("/videos")
async def list_videos(
    user_id: int = Query(..., description="ID пользователя, владельца видео"),
    creator_id: Optional[int] = Query(None),
    status: Optional[str] = Query(None),
    limit: int = Query(20, ge=1, le=100),
    offset: int = Query(0, ge=0),
    db: AsyncSession = Depends(get_db),
    auth_user_id: int = Depends(verify_user_header),
) -> List[dict]:
    """Получает список видео пользователя с фильтрацией."""
    ensure_user_scope(user_id, auth_user_id)
    query = (
        select(CreatorVideo, CreatorSource.username)
        .join(CreatorSource, CreatorVideo.creator_id == CreatorSource.id)
        .filter(CreatorSource.user_id == user_id)
    )
    
    if creator_id:
        query = query.filter(CreatorVideo.creator_id == creator_id)
    if status:
        query = query.filter(CreatorVideo.status == status)
    
    query = query.order_by(CreatorVideo.discovered_at.desc()).offset(offset).limit(limit)
    
    result = await db.execute(query)
    videos_data = result.all()
    
    return [
        {
            "id": v.id,
            "creator_id": v.creator_id,
            "creator_username": username,
            "video_id": v.video_id,
            "url": v.url,
            "status": v.status,
            "task_id": v.task_id,
            "discovered_at": v.discovered_at.isoformat() if v.discovered_at else None,
            **_video_time_payload(v),
        }
        for v, username in videos_data
    ]


@router.get("/videos/by-ids")
async def get_videos_by_ids(
    video_ids: str = Query(..., description="Список ID видео через запятую"),
    user_id: int = Query(..., description="ID пользователя, владельца видео"),
    db: AsyncSession = Depends(get_db),
    auth_user_id: int = Depends(verify_user_header),
) -> List[dict]:
    """Получает видео пользователя по списку ID."""
    ensure_user_scope(user_id, auth_user_id)
    try:
        video_id_list = [int(id.strip()) for id in video_ids.split(",") if id.strip()]
    except ValueError:
        raise HTTPException(status_code=400, detail="Invalid video_ids format")
    
    if not video_id_list:
        raise HTTPException(status_code=400, detail="video_ids cannot be empty")
    
    if len(video_id_list) > 100:
        raise HTTPException(status_code=400, detail="Too many video_ids (max 100)")
    
    result = await db.execute(
        select(CreatorVideo, CreatorSource.username)
        .join(CreatorSource, CreatorVideo.creator_id == CreatorSource.id)
        .filter(
            CreatorVideo.id.in_(video_id_list),
            CreatorSource.user_id == user_id
        )
    )
    videos_data = result.all()
    
    return [
        {
            "id": v.id,
            "creator_id": v.creator_id,
            "creator_username": username,
            "video_id": v.video_id,
            "url": v.url,
            "status": v.status,
            "task_id": v.task_id,
            "discovered_at": v.discovered_at.isoformat() if v.discovered_at else None,
            **_video_time_payload(v),
        }
        for v, username in videos_data
    ]


@router.get("/autopilot-report", deprecated=True)
async def get_autopilot_report(
    user_id: int = Query(..., description="Owner user ID"),
    limit: int = Query(5, ge=1, le=20),
    offset: int = Query(0, ge=0),
    status: Optional[str] = Query(None, description="Optional task status filter"),
    creator_id: Optional[int] = Query(None, ge=1),
    include_archived: bool = Query(False),
    db: AsyncSession = Depends(get_db),
    auth_user_id: int = Depends(verify_user_header),
) -> dict:
    """Returns autopilot-created comment orders with video links and pagination."""
    ensure_user_scope(user_id, auth_user_id)
    if not isinstance(limit, int):
        limit = 5
    if not isinstance(offset, int):
        offset = 0
    if not isinstance(status, str):
        status = None
    if not isinstance(creator_id, int):
        creator_id = None
    if not isinstance(include_archived, bool):
        include_archived = False
    origin_expr = Task.settings["origin"].as_string()
    origin_filter = (
        origin_expr.in_(["autopilot", "autopilot_archived"])
        if include_archived
        else origin_expr == "autopilot"
    )
    base_filters = [Task.user_id == user_id, origin_filter]
    if status and status != "all":
        base_filters.append(Task.status == status)
    if creator_id:
        base_filters.append(
            or_(
                Task.settings["creator_source_id"].as_integer() == int(creator_id),
                Task.settings["creator_source_id"].as_string() == str(creator_id),
            )
        )

    total_result = await db.execute(select(func.count(Task.id)).filter(*base_filters))
    total = int(total_result.scalar_one() or 0)

    status_result = await db.execute(
        select(Task.status, func.count(Task.id))
        .filter(*base_filters)
        .group_by(Task.status)
    )
    status_counts = {
        str(status or "unknown"): int(count or 0)
        for status, count in status_result.all()
    }

    creator_ids_result = await db.execute(select(CreatorSource.id).filter(CreatorSource.user_id == user_id))
    owner_creator_ids = [creator_id for (creator_id,) in creator_ids_result.all()]
    outbox_counts: dict[str, int] = {}
    if owner_creator_ids:
        outbox_result = await db.execute(
            select(MonitorOutbox.status, func.count(MonitorOutbox.id))
            .filter(
                MonitorOutbox.creator_id.in_(owner_creator_ids),
                MonitorOutbox.event_type == "autopilot_create",
            )
            .group_by(MonitorOutbox.status)
        )
        outbox_counts = {
            str(status or "unknown"): int(count or 0)
            for status, count in outbox_result.all()
        }

    tasks_result = await db.execute(
        select(Task)
        .filter(*base_filters)
        .order_by(Task.created_at.desc(), Task.id.desc())
        .offset(offset)
        .limit(limit)
    )
    tasks = list(tasks_result.scalars().all())
    task_ids = [task.id for task in tasks]

    videos_by_task: dict[int, list[dict]] = {}
    if task_ids:
        videos_result = await db.execute(
            select(CreatorVideo, CreatorSource.username)
            .join(CreatorSource, CreatorVideo.creator_id == CreatorSource.id)
            .filter(
                CreatorVideo.task_id.in_(task_ids),
                CreatorSource.user_id == user_id,
            )
            .order_by(CreatorVideo.discovered_at.desc(), CreatorVideo.id.desc())
        )
        for video, username in videos_result.all():
            if not video.task_id:
                continue
            videos_by_task.setdefault(video.task_id, []).append(
                {
                    "id": video.id,
                    "creator_id": video.creator_id,
                    "creator_username": username,
                    "video_id": video.video_id,
                    "url": video.url,
                    "status": video.status,
                    "discovered_at": _iso_or_none(video.discovered_at),
                    **_video_time_payload(video),
                }
            )

    items: list[dict] = []
    for task in tasks:
        task_settings = task.settings if isinstance(task.settings, dict) else {}
        task_urls = _task_video_urls(task)
        linked_videos = list(videos_by_task.get(task.id, []))
        linked_urls = {str(video.get("url") or "").strip() for video in linked_videos}

        for url in task_urls:
            if url in linked_urls:
                continue
            linked_videos.append(
                {
                    "id": None,
                    "creator_id": task_settings.get("creator_source_id"),
                    "creator_username": task_settings.get("creator_username"),
                    "video_id": _extract_video_id_from_url(url),
                    "url": url,
                    "status": None,
                    "discovered_at": None,
                    "create_time": None,
                    "published_at": None,
                    "meta": None,
                }
            )

        creator_username = task_settings.get("creator_username")
        if not creator_username and linked_videos:
            creator_username = linked_videos[0].get("creator_username")

        items.append(
            {
                "task_id": task.id,
                "status": task.status,
                "progress": task.progress,
                "error_reason": task.error_reason,
                "created_at": _iso_or_none(task.created_at),
                "updated_at": _iso_or_none(task.updated_at),
                "creator_username": creator_username,
                "creator_source_id": task_settings.get("creator_source_id"),
                "creator_video_id": task_settings.get("creator_video_id"),
                "template_id": task_settings.get("template_id"),
                "template_name": task_settings.get("template_name"),
                "created_from": task_settings.get("created_from"),
                "video_urls": task_urls,
                "videos": linked_videos,
            }
        )

    pages = max(1, (total + limit - 1) // limit) if limit else 1
    return {
        "items": items,
        "total": total,
        "limit": limit,
        "offset": offset,
        "page": (offset // limit) + 1 if limit else 1,
        "pages": pages,
        "status_counts": status_counts,
        "outbox_counts": outbox_counts,
        "filters": {
            "status": status or "all",
            "creator_id": creator_id,
            "include_archived": include_archived,
        },
    }


def _coerce_task_id(value: Any) -> int | None:
    try:
        if isinstance(value, bytes):
            value = value.decode("utf-8", errors="ignore")
        return int(str(value))
    except (TypeError, ValueError):
        return None


def _task_preview_payload(task: Task) -> dict[str, Any]:
    task_settings = task.settings if isinstance(task.settings, dict) else {}
    return {
        "task_id": task.id,
        "status": task.status,
        "progress": task.progress,
        "creator_username": task_settings.get("creator_username"),
        "creator_source_id": task_settings.get("creator_source_id"),
        "created_at": _iso_or_none(task.created_at),
        "updated_at": _iso_or_none(task.updated_at),
        "video_urls": _task_video_urls(task)[:3],
    }


async def _autopilot_current_work_payload(
    db: AsyncSession,
    *,
    user_id: int,
    redis_task_ids: list[int] | None = None,
) -> dict[str, Any]:
    origin_expr = Task.settings["origin"].as_string()
    active_origin_filter = origin_expr == "autopilot"
    heartbeat_alive, heartbeat_info = get_executor_heartbeat()
    executor_active, executor_activity = executor_task_activity_snapshot()
    active_task_id = None
    if isinstance(heartbeat_info, dict):
        active_task_id = _coerce_task_id(heartbeat_info.get("active_task_id"))

    active_task_payload = None
    if active_task_id:
        active_task_result = await db.execute(
            select(Task).filter(
                Task.id == active_task_id,
                Task.user_id == user_id,
                active_origin_filter,
            )
        )
        active_task = active_task_result.scalar_one_or_none()
        if active_task:
            active_task_payload = _task_preview_payload(active_task)

    processing_result = await db.execute(
        select(Task)
        .filter(
            Task.user_id == user_id,
            active_origin_filter,
            Task.status == "in_progress",
        )
        .order_by(Task.updated_at.desc(), Task.created_at.asc(), Task.id.asc())
        .limit(8)
    )
    processing_tasks = [_task_preview_payload(task) for task in processing_result.scalars().all()]

    queue_head_payload: list[dict[str, Any]] = []
    if redis_task_ids:
        queue_head_result = await db.execute(
            select(Task)
            .filter(
                Task.id.in_(redis_task_ids[:10]),
                Task.user_id == user_id,
                active_origin_filter,
                Task.status == "pending",
            )
        )
        queue_head_map = {task.id: _task_preview_payload(task) for task in queue_head_result.scalars().all()}
        queue_head_payload = [queue_head_map[task_id] for task_id in redis_task_ids[:10] if task_id in queue_head_map]

    owner_creator_ids_result = await db.execute(select(CreatorSource.id).filter(CreatorSource.user_id == user_id))
    owner_creator_ids = [creator_id for (creator_id,) in owner_creator_ids_result.all()]
    active_outbox_preview: list[dict[str, Any]] = []
    if owner_creator_ids:
        outbox_rows = await db.execute(
            select(MonitorOutbox, CreatorSource.username)
            .join(CreatorSource, CreatorSource.id == MonitorOutbox.creator_id)
            .filter(
                MonitorOutbox.creator_id.in_(owner_creator_ids),
                MonitorOutbox.event_type.in_(["autopilot_create", "notify_auto_orders_created"]),
                MonitorOutbox.status.in_(["processing", "pending", "failed"]),
            )
            .order_by(
                (MonitorOutbox.status == "processing").desc(),
                MonitorOutbox.created_at.asc(),
                MonitorOutbox.id.asc(),
            )
            .limit(8)
        )
        for entry, username in outbox_rows.all():
            payload = entry.payload if isinstance(entry.payload, dict) else {}
            active_outbox_preview.append(
                {
                    "id": entry.id,
                    "event_type": entry.event_type,
                    "status": entry.status,
                    "creator_id": entry.creator_id,
                    "creator_username": username,
                    "video_count": len(payload.get("video_ids") or []),
                    "attempts": entry.attempts,
                    "last_error": entry.last_error,
                    "created_at": _iso_or_none(entry.created_at),
                    "available_at": _iso_or_none(entry.available_at),
                    "locked_at": _iso_or_none(entry.locked_at),
                }
            )

    return {
        "executor": {
            "active": bool(executor_active),
            "heartbeat_alive": bool(heartbeat_alive),
            "heartbeat": heartbeat_info if isinstance(heartbeat_info, dict) else {},
            "activity": executor_activity if isinstance(executor_activity, dict) else {},
            "active_task_id": active_task_id,
            "active_task": active_task_payload,
        },
        "processing_tasks": processing_tasks,
        "queue_head": queue_head_payload,
        "outbox_active": active_outbox_preview,
    }


async def _read_autopilot_queue_state(db: AsyncSession, user_id: int) -> dict[str, Any]:
    origin_expr = Task.settings["origin"].as_string()
    active_origin_filter = origin_expr == "autopilot"

    redis_queue_len = 0
    redis_task_ids: list[int] = []
    redis_error = None
    try:
        redis_client = get_redis_client()
        redis_queue_len = int(redis_client.llen(settings.REDIS_TASK_QUEUE) or 0)
        raw_ids = redis_client.lrange(settings.REDIS_TASK_QUEUE, 0, 99) or []
        redis_task_ids = [task_id for task_id in (_coerce_task_id(item) for item in raw_ids) if task_id is not None]
    except Exception as exc:
        redis_error = str(exc)[:160]

    task_status_rows = await db.execute(
        select(Task.status, func.count(Task.id))
        .filter(Task.user_id == user_id, active_origin_filter)
        .group_by(Task.status)
    )
    task_status_counts = {
        str(status or "unknown"): int(count or 0)
        for status, count in task_status_rows.all()
    }

    previews_result = await db.execute(
        select(Task)
        .filter(
            Task.user_id == user_id,
            active_origin_filter,
            Task.status.in_(["pending", "in_progress"]),
        )
        .order_by(Task.created_at.asc(), Task.id.asc())
        .limit(12)
    )
    task_preview = [_task_preview_payload(task) for task in previews_result.scalars().all()]

    pending_reserve_count = 0
    pending_reserve_result = await db.execute(
        select(func.count(PendingEnqueue.task_id))
        .join(Task, Task.id == PendingEnqueue.task_id)
        .filter(Task.user_id == user_id, active_origin_filter)
    )
    pending_reserve_count = int(pending_reserve_result.scalar_one() or 0)

    creator_ids_result = await db.execute(select(CreatorSource.id).filter(CreatorSource.user_id == user_id))
    owner_creator_ids = [creator_id for (creator_id,) in creator_ids_result.all()]
    auto_creator_ids_result = await db.execute(
        select(CreatorSource.id).filter(
            CreatorSource.user_id == user_id,
            CreatorSource.mode == "auto_comment",
        )
    )
    auto_creator_ids = [creator_id for (creator_id,) in auto_creator_ids_result.all()]

    outbox_counts: dict[str, int] = {}
    outbox_preview: list[dict[str, Any]] = []
    new_video_count = 0
    queued_video_count = 0
    if owner_creator_ids:
        outbox_rows = await db.execute(
            select(MonitorOutbox.status, func.count(MonitorOutbox.id))
            .filter(
                MonitorOutbox.creator_id.in_(owner_creator_ids),
                MonitorOutbox.event_type.in_(["autopilot_create", "notify_auto_orders_created"]),
                MonitorOutbox.status.in_(["pending", "failed", "processing"]),
            )
            .group_by(MonitorOutbox.status)
        )
        outbox_counts = {
            str(status or "unknown"): int(count or 0)
            for status, count in outbox_rows.all()
        }
        outbox_preview_rows = await db.execute(
            select(MonitorOutbox, CreatorSource.username)
            .join(CreatorSource, CreatorSource.id == MonitorOutbox.creator_id)
            .filter(
                MonitorOutbox.creator_id.in_(owner_creator_ids),
                MonitorOutbox.event_type.in_(["autopilot_create", "notify_auto_orders_created"]),
                MonitorOutbox.status.in_(["pending", "failed", "processing"]),
            )
            .order_by(MonitorOutbox.created_at.asc(), MonitorOutbox.id.asc())
            .limit(10)
        )
        for entry, username in outbox_preview_rows.all():
            payload = entry.payload if isinstance(entry.payload, dict) else {}
            outbox_preview.append(
                {
                    "id": entry.id,
                    "event_type": entry.event_type,
                    "status": entry.status,
                    "creator_id": entry.creator_id,
                    "creator_username": username,
                    "video_count": len(payload.get("video_ids") or []),
                    "attempts": entry.attempts,
                    "last_error": entry.last_error,
                    "created_at": _iso_or_none(entry.created_at),
                }
            )
    if auto_creator_ids:
        video_counts = await db.execute(
            select(CreatorVideo.status, func.count(CreatorVideo.id))
            .filter(CreatorVideo.creator_id.in_(auto_creator_ids))
            .group_by(CreatorVideo.status)
        )
        video_status_counts = {
            str(status or "unknown"): int(count or 0)
            for status, count in video_counts.all()
        }
        new_video_count = int(video_status_counts.get("new", 0) or 0)
        queued_video_count = int(video_status_counts.get("queued", 0) or 0)

    stale_redis_task_ids: list[int] = []
    if redis_task_ids:
        task_rows = await db.execute(
            select(Task.id, Task.user_id, Task.status, Task.settings)
            .filter(Task.id.in_(redis_task_ids))
        )
        task_map = {row.id: row for row in task_rows.all()}
        for task_id in redis_task_ids:
            row = task_map.get(task_id)
            settings_dict = row.settings if row and isinstance(row.settings, dict) else {}
            if (
                not row
                or row.user_id != user_id
                or row.status != "pending"
                or settings_dict.get("origin") != "autopilot"
            ):
                stale_redis_task_ids.append(task_id)

    current_work = await _autopilot_current_work_payload(
        db,
        user_id=user_id,
        redis_task_ids=redis_task_ids,
    )

    return {
        "control": await _get_autopilot_control_state(db),
        "current": current_work,
        "redis": {
            "queue_len": redis_queue_len,
            "preview_task_ids": redis_task_ids[:20],
            "stale_task_ids": stale_redis_task_ids[:50],
            "stale_count": len(stale_redis_task_ids),
            "error": redis_error,
        },
        "tasks": {
            "status_counts": task_status_counts,
            "pending_reserve_count": pending_reserve_count,
            "preview": task_preview,
        },
        "outbox": {
            "status_counts": outbox_counts,
            "preview": outbox_preview,
        },
        "videos": {
            "new": new_video_count,
            "queued": queued_video_count,
        },
    }


@router.get("/autopilot-control", deprecated=True)
async def get_autopilot_control(
    user_id: int = Query(..., description="Owner user ID"),
    db: AsyncSession = Depends(get_db),
    auth_user_id: int = Depends(verify_user_header),
) -> dict:
    ensure_user_scope(user_id, auth_user_id)
    return await _get_autopilot_control_state(db)


@router.post("/autopilot-control", deprecated=True)
async def update_autopilot_control(
    payload: AutopilotControlUpdate,
    user_id: int = Query(..., description="Owner user ID"),
    db: AsyncSession = Depends(get_db),
    auth_user_id: int = Depends(verify_user_header),
) -> dict:
    ensure_user_scope(user_id, auth_user_id)
    queued_existing_video_events = 0
    queued_existing_video_count = 0
    if payload.mode is not None:
        mode = _normalize_autopilot_control_mode(payload.mode)
        if mode != str(payload.mode or "").strip().lower():
            raise HTTPException(status_code=400, detail="mode must be one of: active, paused, drain")
        await _set_app_setting_async(
            db,
            AUTOPILOT_CONTROL_MODE_KEY,
            mode,
            description="Autopilot order creation mode: active, paused, or drain.",
            updated_by=auth_user_id,
        )
        if mode == AUTOPILOT_CONTROL_ACTIVE:
            creators_result = await db.execute(
                select(CreatorSource)
                .filter(
                    CreatorSource.user_id == user_id,
                    CreatorSource.enabled.is_(True),
                    CreatorSource.mode == "auto_comment",
                )
                .order_by(CreatorSource.id.asc())
            )
            for creator in creators_result.scalars().all():
                if not _creator_has_autopilot_template_reference(creator):
                    continue
                queued_events, queued_videos = await _enqueue_existing_new_videos_for_autopilot(db, creator)
                queued_existing_video_events += queued_events
                queued_existing_video_count += queued_videos
    if payload.max_create_per_tick is not None:
        await _set_app_setting_async(
            db,
            AUTOPILOT_MAX_CREATE_PER_TICK_KEY,
            _coerce_int_range(payload.max_create_per_tick, AUTOPILOT_DEFAULT_MAX_CREATE_PER_TICK, 0, 1000),
            description="Maximum autopilot orders created from monitor outbox per monitor pass.",
            updated_by=auth_user_id,
        )
    if payload.max_create_per_creator is not None:
        await _set_app_setting_async(
            db,
            AUTOPILOT_MAX_CREATE_PER_CREATOR_KEY,
            _coerce_int_range(payload.max_create_per_creator, AUTOPILOT_DEFAULT_MAX_CREATE_PER_CREATOR, 0, 1000),
            description="Maximum autopilot orders created for one creator from one outbox event.",
            updated_by=auth_user_id,
        )
    if payload.max_create_per_hour is not None:
        await _set_app_setting_async(
            db,
            AUTOPILOT_MAX_CREATE_PER_HOUR_KEY,
            _coerce_int_range(payload.max_create_per_hour, AUTOPILOT_DEFAULT_MAX_CREATE_PER_HOUR, 0, 10000),
            description="Maximum autopilot orders created globally per rolling hour.",
            updated_by=auth_user_id,
        )
    await db.commit()
    clear_app_settings_cache()
    state = await _get_autopilot_control_state(db)
    state["queued_existing_new_video_events"] = queued_existing_video_events
    state["queued_existing_new_video_count"] = queued_existing_video_count
    return state


@router.get("/autopilot-live-queue", deprecated=True)
async def get_autopilot_live_queue(
    user_id: int = Query(..., description="Owner user ID"),
    db: AsyncSession = Depends(get_db),
    auth_user_id: int = Depends(verify_user_header),
) -> dict:
    ensure_user_scope(user_id, auth_user_id)
    return await _read_autopilot_queue_state(db, user_id)


@router.post("/autopilot-live-queue/cleanup-stale", deprecated=True)
async def cleanup_autopilot_stale_queue(
    user_id: int = Query(..., description="Owner user ID"),
    db: AsyncSession = Depends(get_db),
    auth_user_id: int = Depends(verify_user_header),
) -> dict:
    ensure_user_scope(user_id, auth_user_id)
    state = await _read_autopilot_queue_state(db, user_id)
    stale_ids = [int(task_id) for task_id in state.get("redis", {}).get("stale_task_ids", [])]
    removed = await asyncio.to_thread(remove_tasks_from_queue, stale_ids) if stale_ids else 0
    return {"ok": True, "stale_task_ids": stale_ids, "removed_from_redis_queue": removed}


@router.post("/autopilot-report/cleanup", deprecated=True)
async def cleanup_autopilot_report(
    payload: AutopilotReportCleanupRequest,
    user_id: int = Query(..., description="Owner user ID"),
    db: AsyncSession = Depends(get_db),
    auth_user_id: int = Depends(verify_user_header),
) -> dict:
    ensure_user_scope(user_id, auth_user_id)
    statuses = [str(status).strip().lower() for status in (payload.statuses or ["done", "stopped", "error", "partial"])]
    allowed_statuses = {"done", "stopped", "error", "partial"}
    statuses = [status for status in statuses if status in allowed_statuses]
    if not statuses:
        raise HTTPException(status_code=400, detail="No cleanup statuses selected")

    filters = [
        Task.user_id == user_id,
        Task.settings["origin"].as_string() == "autopilot",
        Task.status.in_(statuses),
    ]
    if payload.creator_id:
        filters.append(
            or_(
                Task.settings["creator_source_id"].as_integer() == int(payload.creator_id),
                Task.settings["creator_source_id"].as_string() == str(payload.creator_id),
            )
        )
    if payload.older_than_days is not None and payload.older_than_days > 0:
        cutoff = datetime.now(timezone.utc) - timedelta(days=payload.older_than_days)
        filters.append(Task.created_at <= cutoff)

    result = await db.execute(select(Task).filter(*filters).with_for_update())
    tasks = list(result.scalars().all())
    now = datetime.now(timezone.utc).isoformat()
    for task in tasks:
        task_settings = _task_settings_dict(task).copy()
        task_settings["origin"] = "autopilot_archived"
        task_settings["autopilot_report_hidden"] = True
        task_settings["autopilot_report_archived_at"] = now
        task.settings = task_settings
        task.updated_at = datetime.now(timezone.utc)

    await db.commit()
    return {
        "ok": True,
        "archived_tasks": len(tasks),
        "statuses": statuses,
        "creator_id": payload.creator_id,
    }


@router.post("/autopilot-report/skip-new", deprecated=True)
async def skip_autopilot_new_videos(
    payload: AutopilotSkipNewVideosRequest,
    user_id: int = Query(..., description="Owner user ID"),
    db: AsyncSession = Depends(get_db),
    auth_user_id: int = Depends(verify_user_header),
) -> dict:
    ensure_user_scope(user_id, auth_user_id)
    creator_query = select(CreatorSource.id).filter(
        CreatorSource.user_id == user_id,
        CreatorSource.mode == "auto_comment",
    )
    if payload.creator_id:
        creator_query = creator_query.filter(CreatorSource.id == payload.creator_id)
    creator_ids_result = await db.execute(creator_query)
    creator_ids = [creator_id for (creator_id,) in creator_ids_result.all()]
    if payload.creator_id and not creator_ids:
        raise HTTPException(status_code=404, detail="Creator not found")
    if not creator_ids:
        return {"ok": True, "skipped_videos": 0, "creator_id": payload.creator_id}

    result = await db.execute(
        select(CreatorVideo)
        .filter(
            CreatorVideo.creator_id.in_(creator_ids),
            CreatorVideo.status == "new",
            CreatorVideo.task_id.is_(None),
        )
        .with_for_update()
    )
    videos = list(result.scalars().all())
    now = datetime.now(timezone.utc)
    for video in videos:
        meta = video.meta.copy() if isinstance(video.meta, dict) else {}
        meta["autopilot_skipped_at"] = now.isoformat()
        meta["autopilot_skipped_by"] = "autopilot_control"
        video.meta = meta
        video.status = "skipped"
    await db.commit()
    return {"ok": True, "skipped_videos": len(videos), "creator_id": payload.creator_id}


@router.post("/autopilot-report/cancel", deprecated=True)
async def cancel_autopilot_work(
    payload: AutopilotCancelRequest,
    user_id: int = Query(..., description="Owner user ID"),
    db: AsyncSession = Depends(get_db),
    auth_user_id: int = Depends(verify_user_header),
) -> dict:
    """Cancels queued autopilot work for all creators or one creator."""
    ensure_user_scope(user_id, auth_user_id)
    now = datetime.now(timezone.utc)

    owner_result = await db.execute(select(User).filter(User.id == user_id).with_for_update())
    owner = owner_result.scalar_one_or_none()
    if not owner:
        raise HTTPException(status_code=404, detail="User not found")

    creator_query = select(CreatorSource).filter(CreatorSource.user_id == user_id)
    if payload.creator_id:
        creator_query = creator_query.filter(CreatorSource.id == payload.creator_id)
    creator_result = await db.execute(creator_query)
    creators = list(creator_result.scalars().all())
    if payload.creator_id and not creators:
        raise HTTPException(status_code=404, detail="Creator not found")

    creator_ids = [creator.id for creator in creators]
    creator_username = creators[0].username if payload.creator_id and creators else None
    if not creator_ids:
        return {
            "ok": True,
            "scope": "creator" if payload.creator_id else "all",
            "creator_id": payload.creator_id,
            "creator_username": creator_username,
            "cancelled_outbox_events": 0,
            "cancelled_notifications": 0,
            "cancelled_pending_tasks": 0,
            "stopped_running_tasks": 0,
            "released_videos": 0,
            "refunded_amount": 0.0,
            "removed_from_redis_queue": 0,
        }

    outbox_result = await db.execute(
        select(MonitorOutbox).filter(
            MonitorOutbox.creator_id.in_(creator_ids),
            MonitorOutbox.event_type.in_(["autopilot_create", "notify_auto_orders_created"]),
            MonitorOutbox.status.in_(["pending", "failed", "processing"]),
        )
    )
    outbox_entries = list(outbox_result.scalars().all())
    cancelled_outbox_events = 0
    cancelled_notifications = 0
    for entry in outbox_entries:
        entry.status = "cancelled"
        entry.lock_owner = None
        entry.locked_at = None
        entry.last_error = "cancelled_by_autopilot_control"
        if entry.event_type == "autopilot_create":
            cancelled_outbox_events += 1
        elif entry.event_type == "notify_auto_orders_created":
            cancelled_notifications += 1

    target_statuses = ["pending"]
    if payload.include_running:
        target_statuses.append("in_progress")

    origin_filter = Task.settings["origin"].as_string() == "autopilot"
    tasks_result = await db.execute(
        select(Task)
        .filter(
            Task.user_id == user_id,
            origin_filter,
            Task.status.in_(target_statuses),
        )
        .order_by(Task.created_at.asc(), Task.id.asc())
        .with_for_update()
    )
    candidate_tasks = list(tasks_result.scalars().all())
    tasks = [
        task
        for task in candidate_tasks
        if not payload.creator_id or _task_creator_source_id(task) == payload.creator_id
    ]

    pending_tasks = [task for task in tasks if str(task.status or "").lower() == "pending"]
    running_tasks = [task for task in tasks if str(task.status or "").lower() == "in_progress"]
    pending_task_ids = [task.id for task in pending_tasks]
    running_task_ids = [task.id for task in running_tasks]

    videos_by_task: dict[int, list[CreatorVideo]] = {}
    if pending_task_ids:
        videos_result = await db.execute(
            select(CreatorVideo)
            .join(CreatorSource, CreatorVideo.creator_id == CreatorSource.id)
            .filter(
                CreatorVideo.task_id.in_(pending_task_ids),
                CreatorSource.user_id == user_id,
                CreatorVideo.status == "queued",
            )
        )
        for video in videos_result.scalars().all():
            if video.task_id:
                videos_by_task.setdefault(int(video.task_id), []).append(video)

    refunded_amount = Decimal("0.00")
    released_videos = 0
    for task in pending_tasks:
        task_settings = _task_settings_dict(task).copy()
        task_settings["cancelled_by_user"] = True
        task_settings["cancelled_at"] = now.isoformat()
        task_settings["cancel_reason"] = "autopilot_control"
        task.status = "stopped"
        task.error_reason = "cancelled_by_autopilot_control"
        task.settings = task_settings
        task.updated_at = now

        remaining_refund = _remaining_task_refund(task)
        if remaining_refund > 0:
            owner.balance = (owner.balance or Decimal("0.00")) + remaining_refund
            task.refunded_cost = Decimal(str(task.refunded_cost or 0)) + remaining_refund
            db.add(
                Transaction(
                    user_id=user_id,
                    task_id=task.id,
                    amount=remaining_refund,
                    type="refund",
                    description=f"Возврат за отмену автокомментинга #{task.id}",
                )
            )
            refunded_amount += remaining_refund

        for video in videos_by_task.get(task.id, []):
            _mark_video_released_from_autopilot(video, task_id=task.id, now=now)
            released_videos += 1

    for task in running_tasks:
        task_settings = _task_settings_dict(task).copy()
        task_settings["stopped_by_user"] = True
        task_settings["stopped_at"] = now.isoformat()
        task_settings["force_close_browsers"] = True
        task_settings["cancel_reason"] = "autopilot_control"
        task.status = "stopped"
        task.error_reason = "stopped_by_autopilot_control"
        task.settings = task_settings
        task.updated_at = now

    if pending_task_ids:
        await db.execute(delete(PendingEnqueue).where(PendingEnqueue.task_id.in_(pending_task_ids)))

    await db.commit()

    removed_from_redis_queue = 0
    if pending_task_ids:
        removed_from_redis_queue = await asyncio.to_thread(remove_tasks_from_queue, pending_task_ids)

    stop_signal_count = 0
    for task_id in running_task_ids:
        if await asyncio.to_thread(request_task_stop, task_id):
            stop_signal_count += 1

    logger.info(
        "Autopilot cancellation user_id=%s creator_id=%s pending_tasks=%s running_tasks=%s outbox=%s released_videos=%s refunded=%s redis_removed=%s",
        user_id,
        payload.creator_id,
        len(pending_task_ids),
        len(running_task_ids),
        cancelled_outbox_events,
        released_videos,
        refunded_amount,
        removed_from_redis_queue,
    )
    return {
        "ok": True,
        "scope": "creator" if payload.creator_id else "all",
        "creator_id": payload.creator_id,
        "creator_username": creator_username,
        "include_running": bool(payload.include_running),
        "cancelled_outbox_events": cancelled_outbox_events,
        "cancelled_notifications": cancelled_notifications,
        "cancelled_pending_tasks": len(pending_task_ids),
        "stopped_running_tasks": len(running_task_ids),
        "stop_signal_count": stop_signal_count,
        "released_videos": released_videos,
        "refunded_amount": float(refunded_amount),
        "removed_from_redis_queue": removed_from_redis_queue,
    }


@router.get("/auto-orders")
async def get_auto_orders(
    user_id: int = Query(..., description="ID пользователя для получения его автоматических заказов"),
    limit: int = Query(20, ge=1, le=100),
    offset: int = Query(0, ge=0),
    db: AsyncSession = Depends(get_db),
    auth_user_id: int = Depends(verify_user_header),
) -> List[dict]:
    """Получает список автоматических заказов (задач, созданных из видео парсинга)."""
    ensure_user_scope(user_id, auth_user_id)
    from sqlalchemy import distinct
    
    # ÐŸÐ¾Ð»ÑƒÑ‡Ð°ÐµÐ¼ task_id Ð¸Ð· Ð²Ð¸Ð´ÐµÐ¾, ÐºÐ¾Ñ‚Ð¾Ñ€Ñ‹Ðµ Ð¿Ñ€Ð¸Ð½Ð°Ð´Ð»ÐµÐ¶Ð°Ñ‚ Ð¿Ð¾Ð»ÑŒÐ·Ð¾Ð²Ð°Ñ‚ÐµÐ»ÑŽ
    task_ids_subquery = (
        select(distinct(CreatorVideo.task_id).label("task_id"))
        .join(CreatorSource, CreatorVideo.creator_id == CreatorSource.id)
        .filter(
            CreatorSource.user_id == user_id,
            CreatorVideo.task_id.isnot(None)
        )
    ).subquery()
    
    # Получаем задачи пользователя, которые есть в списке task_id из видео
    # Пагинируем сразу по JOIN, чтобы не раздувать тяжелый IN (...)
    # legacy task_ids_list materialization removed
    
    # JOIN pagination below handles the empty-set case naturally
    
    # ÐŸÐ¾Ð»ÑƒÑ‡Ð°ÐµÐ¼ Ð·Ð°Ð´Ð°Ñ‡Ð¸
    result = await db.execute(
        select(Task)
        .join(task_ids_subquery, task_ids_subquery.c.task_id == Task.id)
        .filter(Task.user_id == user_id)
        .order_by(Task.created_at.desc())
        .offset(offset)
        .limit(limit)
    )
    tasks = result.scalars().all()
    
    if not tasks:
        return []
    
    # Загружаем связанные видео для каждой задачи
    task_ids = [t.id for t in tasks]
    videos_result = await db.execute(
        select(CreatorVideo)
        .filter(CreatorVideo.task_id.in_(task_ids))
        .join(CreatorSource, CreatorVideo.creator_id == CreatorSource.id)
        .filter(CreatorSource.user_id == user_id)
    )
    videos = videos_result.scalars().all()
    
    # Ð“Ñ€ÑƒÐ¿Ð¿Ð¸Ñ€ÑƒÐµÐ¼ Ð²Ð¸Ð´ÐµÐ¾ Ð¿Ð¾ task_id
    videos_by_task = {}
    for video in videos:
        if video.task_id:
            if video.task_id not in videos_by_task:
                videos_by_task[video.task_id] = []
            videos_by_task[video.task_id].append(video)
    
    # Загружаем связанных блогеров
    creator_ids = {v.creator_id for v in videos}
    if creator_ids:
        creators_result = await db.execute(
            select(CreatorSource).filter(CreatorSource.id.in_(creator_ids))
        )
        creators = {c.id: c for c in creators_result.scalars().all()}
    else:
        creators = {}
    
    return [
        {
            "task_id": task.id,
            "status": task.status,
            "progress": task.progress,
            "total_cost": float(task.total_cost) if task.total_cost else 0.0,
            "created_at": task.created_at.isoformat() if task.created_at else None,
            "video_count": len(videos_by_task.get(task.id, [])),
            "creator_usernames": list(set(
                creators.get(v.creator_id).username 
                for v in videos_by_task.get(task.id, [])
                if v.creator_id in creators and creators.get(v.creator_id) and creators.get(v.creator_id).username
            )),
        }
        for task in tasks
    ]


@router.post("/videos/link-tasks")
async def link_videos_to_tasks(
    request: LinkVideosToTasksRequest,
    user_id: int = Query(..., description="ID пользователя, владельца видео"),
    db: AsyncSession = Depends(get_db),
    auth_user_id: int = Depends(verify_user_header),
) -> dict:
    """Связывает видео с задачами, обновляя status='queued' и task_id для каждого видео."""
    ensure_user_scope(user_id, auth_user_id)
    if await _is_parsing_mode_restricted(db, user_id):
        raise HTTPException(
            status_code=403,
            detail="Software is in parsing mode: task linking is disabled",
        )
    if not request.links:
        raise HTTPException(status_code=400, detail="links cannot be empty")
    
    if len(request.links) > 100:
        raise HTTPException(status_code=400, detail="Too many links (max 100)")
    
    video_ids = [link.video_id for link in request.links]
    task_ids = [link.task_id for link in request.links]
    if len(set(video_ids)) != len(video_ids):
        raise HTTPException(status_code=400, detail="Duplicate video links are not allowed")
    
    # Получаем все видео и проверяем права доступа
    result = await db.execute(
        select(CreatorVideo)
        .filter(CreatorVideo.id.in_(video_ids))
        .join(CreatorSource, CreatorVideo.creator_id == CreatorSource.id)
        .filter(CreatorSource.user_id == user_id)
        .with_for_update()
    )
    videos = result.scalars().all()
    
    if len(videos) != len(video_ids):
        raise HTTPException(status_code=404, detail="Some videos not found or access denied")
    
    # Проверяем, что все задачи принадлежат пользователю
    tasks_result = await db.execute(
        select(Task)
        .filter(Task.id.in_(task_ids), Task.user_id == user_id)
    )
    tasks = tasks_result.scalars().all()
    
    if len(tasks) != len(set(task_ids)):
        raise HTTPException(status_code=404, detail="Some tasks not found or access denied")
    
    # Создаем словарь video_id -> task_id из запроса
    link_map = {link.video_id: link.task_id for link in request.links}
    conflicts = []
    for video in videos:
        target_task_id = link_map.get(video.id)
        if target_task_id is None:
            continue
        if video.task_id is not None and video.task_id != target_task_id:
            conflicts.append(f"video_id={video.id} already linked to task_id={video.task_id}")
            continue
        if video.status != "new" and not (video.status == "queued" and video.task_id == target_task_id):
            conflicts.append(f"video_id={video.id} cannot be linked from status={video.status}")

    if conflicts:
        raise HTTPException(
            status_code=409,
            detail="Some videos are already queued or processed: " + "; ".join(conflicts[:5]),
        )
    
    # Обновляем видео
    updated_count = 0
    unchanged_count = 0
    for video in videos:
        if video.id in link_map:
            target_task_id = link_map[video.id]
            if video.task_id == target_task_id and video.status == "queued":
                unchanged_count += 1
                continue
            video.task_id = target_task_id
            video.status = "queued"
            updated_count += 1
    
    try:
        await db.commit()
        logger.info(f"Linked {updated_count} videos to tasks for user {user_id}")
        return {"ok": True, "updated_count": updated_count, "unchanged_count": unchanged_count}
    except Exception as e:
        await db.rollback()
        logger.error(f"Error linking videos to tasks: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=f"Database error: {str(e)[:200]}")
