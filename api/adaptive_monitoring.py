"""
API endpoints for adaptive monitoring management.
All endpoints require admin authentication.
"""
from __future__ import annotations

import asyncio
import logging
import re
import statistics
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field
from sqlalchemy import delete, desc, exists, func, or_, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from api.auth import verify_admin_header
from db.database import get_db, get_sync_db_context
from db.models import BloggerCheckLog, CreatorSource, CreatorVideo, MonitorOutbox, MonitoredBlogger, VideoBatch
from services.app_settings_service import (
    get_all_adaptive_settings,
    set_adaptive_enabled,
    set_adaptive_min_interval,
    set_adaptive_max_interval,
    set_adaptive_sanity_cap_factor,
    set_adaptive_active_phase_interval,
    set_adaptive_active_phase_hours,
    set_adaptive_found_new_reset_factor,
    set_adaptive_burst_threshold_24h,
    set_adaptive_burst_multiplier,
    set_adaptive_burst_duration_hours,
    set_adaptive_silence_multiplier,
    set_adaptive_silence_max_factor,
    set_adaptive_nighttime_multiplier,
    set_adaptive_empty_streak_threshold,
    set_adaptive_empty_streak_multiplier,
    set_adaptive_gap_divisor,
    set_adaptive_use_p25_for_burst,
    set_adaptive_min_samples,
    set_adaptive_hour_histogram_min_total,
    set_adaptive_active_hour_threshold,
    set_adaptive_dead_hour_threshold,
    set_adaptive_active_hour_multiplier,
    set_adaptive_max_keep,
    set_adaptive_max_history_days,
    set_adaptive_initial_limit,
    set_adaptive_check_log_retention_days,
    set_adaptive_burst_relative_factor,
    set_adaptive_prediction_min_confidence,
    set_adaptive_prediction_buffer_minutes,
    set_adaptive_prediction_high_confidence,
    set_adaptive_recent_gap_weight,
    set_adaptive_feedback_enabled,
    set_adaptive_feedback_target_latency,
    set_adaptive_feedback_ema_alpha,
    set_adaptive_feedback_min_factor,
    set_adaptive_feedback_max_factor,
    set_adaptive_feedback_late_threshold,
)

logger = logging.getLogger("api.adaptive_monitoring")
router = APIRouter(prefix="/api/adaptive", tags=["adaptive_monitoring"])

VALID_PRIORITY_MODES = {"auto", "vip", "lazy", "paused"}
USERNAME_PATTERN = re.compile(r"^[A-Za-z0-9._]{2,64}$")


def _normalize_blogger_username(value: str) -> str:
    raw = (value or "").strip()
    match = re.search(r"tiktok\.com/@([^/?#\s]+)", raw, flags=re.IGNORECASE)
    if match:
        raw = match.group(1)
    raw = raw.lstrip("@").strip().strip("/")
    return raw.lower()


# ── Pydantic schemas ───────────────────────────────────────────────────────────

class AdaptiveSettingsResponse(BaseModel):
    # Main switch
    adaptive_enabled: bool
    # Boundaries
    min_interval_minutes: int
    max_interval_minutes: int
    sanity_cap_factor: float
    # Acceleration
    active_phase_interval_minutes: int
    active_phase_hours: float
    found_new_reset_factor: float
    burst_threshold_24h: int
    burst_multiplier: float
    burst_duration_hours: float
    # Deceleration
    silence_multiplier: float
    silence_max_factor: float
    nighttime_multiplier: float
    empty_streak_threshold: int
    empty_streak_multiplier: float
    # Base calculation
    gap_divisor: float
    use_p25_for_burst: bool
    min_samples: int
    # Hour-of-day
    hour_histogram_min_total: int
    active_hour_threshold: float
    dead_hour_threshold: float
    active_hour_multiplier: float
    # History
    max_keep: int
    max_history_days: int
    initial_limit: int
    check_log_retention_days: int
    # Burst relative + Prediction
    burst_relative_factor: float
    prediction_min_confidence: float
    prediction_buffer_minutes: int
    prediction_high_confidence: float
    recent_gap_weight: float
    # Self-learning feedback loop
    feedback_enabled: bool
    feedback_target_latency_min: int
    feedback_ema_alpha: float
    feedback_min_factor: float
    feedback_max_factor: float
    feedback_late_threshold: float


class AdaptiveSettingsUpdate(BaseModel):
    # Main switch
    adaptive_enabled: Optional[bool] = None
    # Boundaries
    min_interval_minutes: Optional[int] = Field(None, ge=1, le=1440)
    max_interval_minutes: Optional[int] = Field(None, ge=1, le=1440)
    sanity_cap_factor: Optional[float] = Field(None, ge=1.0, le=10.0)
    # Acceleration
    active_phase_interval_minutes: Optional[int] = Field(None, ge=1, le=1440)
    active_phase_hours: Optional[float] = Field(None, ge=0.1, le=72.0)
    found_new_reset_factor: Optional[float] = Field(None, ge=0.05, le=1.0)
    burst_threshold_24h: Optional[int] = Field(None, ge=1, le=50)
    burst_multiplier: Optional[float] = Field(None, ge=0.1, le=1.0)
    burst_duration_hours: Optional[float] = Field(None, ge=1.0, le=168.0)
    # Deceleration
    silence_multiplier: Optional[float] = Field(None, ge=1.0, le=20.0)
    silence_max_factor: Optional[float] = Field(None, ge=1.0, le=10.0)
    nighttime_multiplier: Optional[float] = Field(None, ge=1.0, le=10.0)
    empty_streak_threshold: Optional[int] = Field(None, ge=1, le=100)
    empty_streak_multiplier: Optional[float] = Field(None, ge=1.0, le=5.0)
    # Base calculation
    gap_divisor: Optional[float] = Field(None, ge=1.5, le=100.0)
    use_p25_for_burst: Optional[bool] = None
    min_samples: Optional[int] = Field(None, ge=2, le=100)
    # Hour-of-day
    hour_histogram_min_total: Optional[int] = Field(None, ge=5, le=500)
    active_hour_threshold: Optional[float] = Field(None, ge=0.1, le=1.0)
    dead_hour_threshold: Optional[float] = Field(None, ge=0.0, le=0.5)
    active_hour_multiplier: Optional[float] = Field(None, ge=0.1, le=1.0)
    # History
    max_keep: Optional[int] = Field(None, ge=5, le=200)
    max_history_days: Optional[int] = Field(None, ge=7, le=365)
    initial_limit: Optional[int] = Field(None, ge=6, le=50)
    check_log_retention_days: Optional[int] = Field(None, ge=1, le=180)
    # Burst relative + Prediction
    burst_relative_factor: Optional[float] = Field(None, ge=0.5, le=10.0)
    prediction_min_confidence: Optional[float] = Field(None, ge=0.0, le=1.0)
    prediction_buffer_minutes: Optional[int] = Field(None, ge=1, le=120)
    prediction_high_confidence: Optional[float] = Field(None, ge=0.0, le=1.0)
    recent_gap_weight: Optional[float] = Field(None, ge=0.0, le=1.0)
    # Self-learning feedback loop
    feedback_enabled: Optional[bool] = None
    feedback_target_latency_min: Optional[int] = Field(None, ge=1, le=240)
    feedback_ema_alpha: Optional[float] = Field(None, ge=0.05, le=1.0)
    feedback_min_factor: Optional[float] = Field(None, ge=0.1, le=1.0)
    feedback_max_factor: Optional[float] = Field(None, ge=1.0, le=5.0)
    feedback_late_threshold: Optional[float] = Field(None, ge=1.0, le=10.0)


class BloggerStatsItem(BaseModel):
    id: int
    username: str
    priority_mode: str
    status: Optional[str] = None
    next_interval_minutes: Optional[int] = None
    median_gap_hours: Optional[float] = None
    last_post_age_hours: Optional[float] = None
    post_count_analyzed: Optional[int] = None
    is_burst: Optional[bool] = None
    hour_histogram: Optional[Dict[str, int]] = None
    override_min_interval: Optional[int] = None
    override_max_interval: Optional[int] = None
    notes: Optional[str] = None
    empty_streak: Optional[int] = None
    next_check_at: Optional[str] = None
    last_success_at: Optional[str] = None
    reason: Optional[str] = None
    predicted_next_post_ts: Optional[int] = None
    predicted_confidence: Optional[float] = None
    prediction_used: Optional[bool] = None
    # Cadence + recency profile (algorithm internals exposed for transparency)
    cadence_type: Optional[str] = None       # clockwork | regular | sporadic | chaotic
    gap_cv: Optional[float] = None
    recent_gap_hours: Optional[float] = None
    active_days: Optional[List[int]] = None
    dow_histogram: Optional[Dict[str, int]] = None
    # Trend: recent rhythm vs full history
    trend_ratio: Optional[float] = None      # recent_median / older_median
    trend_direction: Optional[str] = None    # stable|speeding_up|accelerating|decelerating|slowing_down
    # Self-learning feedback metrics
    latency_ema_minutes: Optional[float] = None
    last_detection_latency_min: Optional[int] = None
    total_videos_caught: Optional[int] = None
    total_late_catches: Optional[int] = None
    last_batch_latency: Optional[Dict[str, Any]] = None
    profile_access_failure: Optional[Dict[str, Any]] = None


class PriorityModeUpdate(BaseModel):
    priority_mode: str


class BloggerOverrideUpdate(BaseModel):
    override_min_interval: Optional[int] = Field(None, ge=1, le=1440)
    override_max_interval: Optional[int] = Field(None, ge=1, le=1440)
    notes: Optional[str] = Field(None, max_length=500)


class BloggerUsernameUpdate(BaseModel):
    username: str = Field(..., min_length=1, max_length=120)


class CheckLogItem(BaseModel):
    id: int
    checked_at: str
    found_new_count: int
    interval_used_minutes: Optional[int] = None
    next_interval_minutes: Optional[int] = None
    status: Optional[str] = None
    adaptive_status: Optional[str] = None
    reason: Optional[str] = None
    duration_ms: Optional[int] = None
    detection_latency_min: Optional[int] = None


class VideoAnalyticsItem(BaseModel):
    id: int
    creator_id: int
    creator_username: str
    user_id: Optional[int] = None
    video_id: str
    url: str
    status: str
    discovered_at: Optional[str] = None
    create_time: Optional[int] = None
    detected_at: Optional[str] = None
    detection_latency_min: Optional[int] = None
    age_at_detection_hours: Optional[float] = None
    source: Optional[str] = None
    canonical_source: Optional[str] = None
    scan_status: Optional[str] = None
    batch_size: Optional[int] = None
    batch_index: Optional[int] = None


class VideoAnalyticsResponse(BaseModel):
    blogger_id: int
    username: str
    returned: int
    latency_summary: Dict[str, Any]
    videos: List[VideoAnalyticsItem]


class AdaptiveSummaryResponse(BaseModel):
    total_bloggers: int
    by_status: Dict[str, int]
    by_priority: Dict[str, int]
    avg_interval_minutes: Optional[float]
    with_prediction: int
    avg_prediction_confidence: Optional[float]
    burst_active: int
    calibrating: int
    paused: int
    next_checks: List[Dict[str, Any]]   # [{username, next_check_at, interval, status}]


class BackfillResult(BaseModel):
    total: int
    processed: int
    calibrated: int
    skipped: int
    no_data: int = 0
    insufficient_data: int = 0
    errors: int = 0
    skipped_existing: int = 0
    total_timestamps: int
    avg_timestamps: float


class LearningReportResponse(BaseModel):
    # Calibration progress
    total_bloggers: int
    calibrated: int
    calibrating: int
    calibration_pct: float
    recently_calibrated_24h: int

    # Interval efficiency
    avg_interval_minutes: Optional[float]
    flat_baseline_minutes: int
    interval_saved_pct: Optional[float]  # (baseline - avg) / baseline * 100
    status_distribution: Dict[str, int]

    # Detection performance (7-day window, only checks where new videos found)
    avg_latency_7d: Optional[float]
    p50_latency_7d: Optional[float]
    finds_7d: int
    total_checks_7d: int
    find_rate_7d: Optional[float]  # finds / total_checks

    # Self-learning feedback loop
    feedback_enabled: bool
    feedback_target_latency: Optional[int]
    avg_latency_ema: Optional[float]   # avg across bloggers
    bloggers_improving: int            # latency_ema < target_latency
    bloggers_late: int                 # latency_ema >= target_latency * late_threshold

    # Top 5 fastest bloggers (by interval)
    fastest: List[Dict[str, Any]]


# ── Helpers ────────────────────────────────────────────────────────────────────

def _blogger_to_item(b: MonitoredBlogger) -> BloggerStatsItem:
    stats: dict[str, Any] = b.posting_stats or {}
    hour_hist = stats.get("hour_histogram")
    if hour_hist and not isinstance(hour_hist, dict):
        hour_hist = None
    return BloggerStatsItem(
        id=b.id,
        username=b.username or "",
        priority_mode=b.priority_mode or "auto",
        status=stats.get("status"),
        next_interval_minutes=stats.get("next_interval_minutes"),
        median_gap_hours=stats.get("median_gap_hours"),
        last_post_age_hours=stats.get("last_post_age_hours"),
        post_count_analyzed=stats.get("post_count_analyzed"),
        is_burst=stats.get("is_burst"),
        hour_histogram=hour_hist,
        override_min_interval=getattr(b, "override_min_interval", None),
        override_max_interval=getattr(b, "override_max_interval", None),
        notes=getattr(b, "notes", None),
        empty_streak=getattr(b, "empty_streak", None),
        next_check_at=b.next_check_at.isoformat() if b.next_check_at else None,
        last_success_at=b.last_success_at.isoformat() if b.last_success_at else None,
        reason=stats.get("reason"),
        predicted_next_post_ts=stats.get("predicted_next_post_ts"),
        predicted_confidence=stats.get("predicted_confidence"),
        prediction_used=stats.get("prediction_used"),
        cadence_type=stats.get("cadence_type"),
        gap_cv=stats.get("gap_cv"),
        recent_gap_hours=stats.get("recent_gap_hours"),
        active_days=stats.get("active_days"),
        dow_histogram=stats.get("dow_histogram") if isinstance(stats.get("dow_histogram"), dict) else None,
        trend_ratio=stats.get("trend_ratio"),
        trend_direction=stats.get("trend_direction"),
        latency_ema_minutes=getattr(b, "latency_ema_minutes", None),
        last_detection_latency_min=getattr(b, "last_detection_latency_min", None),
        total_videos_caught=getattr(b, "total_videos_caught", None),
        total_late_catches=getattr(b, "total_late_catches", None),
        last_batch_latency=stats.get("last_batch_latency") if isinstance(stats.get("last_batch_latency"), dict) else None,
        profile_access_failure=(
            stats.get("profile_access_failure")
            if isinstance(stats.get("profile_access_failure"), dict)
            else None
        ),
    )


def _percentile(values: list[int], pct: float) -> Optional[int]:
    if not values:
        return None
    values = sorted(values)
    if len(values) == 1:
        return values[0]
    idx = (len(values) - 1) * pct / 100.0
    lo_i = int(idx)
    hi_i = min(lo_i + 1, len(values) - 1)
    frac = idx - lo_i
    return int(round(values[lo_i] * (1.0 - frac) + values[hi_i] * frac))


def _latency_summary(values: list[int]) -> dict[str, Any]:
    if not values:
        return {"count": 0}
    values = sorted(values)
    return {
        "count": len(values),
        "min": values[0],
        "avg": round(sum(values) / len(values), 1),
        "p50": _percentile(values, 50),
        "p75": _percentile(values, 75),
        "p90": _percentile(values, 90),
        "max": values[-1],
    }


# ── Settings endpoints ─────────────────────────────────────────────────────────

@router.get("/settings", response_model=AdaptiveSettingsResponse)
async def get_adaptive_settings(
    _: None = Depends(verify_admin_header),
) -> AdaptiveSettingsResponse:
    """Returns all adaptive algorithm parameters."""
    try:
        with get_sync_db_context() as db:
            data = get_all_adaptive_settings(db)
        return AdaptiveSettingsResponse(**data)
    except Exception as e:
        logger.error("Error getting adaptive settings: %s", e, exc_info=True)
        raise HTTPException(status_code=500, detail=str(e)[:200])


@router.post("/settings", response_model=AdaptiveSettingsResponse)
async def update_adaptive_settings(
    body: AdaptiveSettingsUpdate,
    _: None = Depends(verify_admin_header),
) -> AdaptiveSettingsResponse:
    """Partial update of adaptive algorithm parameters."""
    _SETTERS = {
        "adaptive_enabled":              set_adaptive_enabled,
        "min_interval_minutes":          set_adaptive_min_interval,
        "max_interval_minutes":          set_adaptive_max_interval,
        "sanity_cap_factor":             set_adaptive_sanity_cap_factor,
        "active_phase_interval_minutes": set_adaptive_active_phase_interval,
        "active_phase_hours":            set_adaptive_active_phase_hours,
        "found_new_reset_factor":        set_adaptive_found_new_reset_factor,
        "burst_threshold_24h":           set_adaptive_burst_threshold_24h,
        "burst_multiplier":              set_adaptive_burst_multiplier,
        "burst_duration_hours":          set_adaptive_burst_duration_hours,
        "silence_multiplier":            set_adaptive_silence_multiplier,
        "silence_max_factor":            set_adaptive_silence_max_factor,
        "nighttime_multiplier":          set_adaptive_nighttime_multiplier,
        "empty_streak_threshold":        set_adaptive_empty_streak_threshold,
        "empty_streak_multiplier":       set_adaptive_empty_streak_multiplier,
        "gap_divisor":                   set_adaptive_gap_divisor,
        "use_p25_for_burst":             set_adaptive_use_p25_for_burst,
        "min_samples":                   set_adaptive_min_samples,
        "hour_histogram_min_total":      set_adaptive_hour_histogram_min_total,
        "active_hour_threshold":         set_adaptive_active_hour_threshold,
        "dead_hour_threshold":           set_adaptive_dead_hour_threshold,
        "active_hour_multiplier":        set_adaptive_active_hour_multiplier,
        "max_keep":                      set_adaptive_max_keep,
        "max_history_days":              set_adaptive_max_history_days,
        "initial_limit":                 set_adaptive_initial_limit,
        "check_log_retention_days":      set_adaptive_check_log_retention_days,
        "burst_relative_factor":         set_adaptive_burst_relative_factor,
        "prediction_min_confidence":     set_adaptive_prediction_min_confidence,
        "prediction_buffer_minutes":     set_adaptive_prediction_buffer_minutes,
        "prediction_high_confidence":    set_adaptive_prediction_high_confidence,
        "recent_gap_weight":             set_adaptive_recent_gap_weight,
        "feedback_enabled":              set_adaptive_feedback_enabled,
        "feedback_target_latency_min":   set_adaptive_feedback_target_latency,
        "feedback_ema_alpha":            set_adaptive_feedback_ema_alpha,
        "feedback_min_factor":           set_adaptive_feedback_min_factor,
        "feedback_max_factor":           set_adaptive_feedback_max_factor,
        "feedback_late_threshold":       set_adaptive_feedback_late_threshold,
    }
    try:
        with get_sync_db_context() as db:
            current_settings = get_all_adaptive_settings(db)
            final_min = (
                body.min_interval_minutes
                if body.min_interval_minutes is not None
                else current_settings.get("min_interval_minutes")
            )
            final_max = (
                body.max_interval_minutes
                if body.max_interval_minutes is not None
                else current_settings.get("max_interval_minutes")
            )
            if final_min is not None and final_max is not None and int(final_min) > int(final_max):
                raise HTTPException(
                    status_code=400,
                    detail=f"min_interval_minutes ({final_min}) must be <= max_interval_minutes ({final_max})",
                )
            for field_name, setter in _SETTERS.items():
                val = getattr(body, field_name, None)
                if val is not None:
                    setter(db, val)
            data = get_all_adaptive_settings(db)
        return AdaptiveSettingsResponse(**data)
    except HTTPException:
        raise
    except Exception as e:
        logger.error("Error updating adaptive settings: %s", e, exc_info=True)
        raise HTTPException(status_code=500, detail=str(e)[:200])


# ── Blogger list & detail endpoints ───────────────────────────────────────────

@router.get("/bloggers", response_model=list[BloggerStatsItem])
async def list_adaptive_bloggers(
    limit: int = Query(50, ge=1, le=1000),
    offset: int = Query(0, ge=0),
    status_filter: Optional[str] = Query(None, alias="status"),
    priority_filter: Optional[str] = Query(None, alias="priority"),
    _: None = Depends(verify_admin_header),
    db: AsyncSession = Depends(get_db),
) -> list[BloggerStatsItem]:
    """List bloggers with adaptive stats and priority_mode. Supports ?status= and ?priority= filters."""
    try:
        q = select(MonitoredBlogger).order_by(MonitoredBlogger.username.asc())

        # Apply priority_mode filter in SQL — it's a plain indexed column.
        if priority_filter:
            q = q.filter(MonitoredBlogger.priority_mode == priority_filter)

        # status lives inside the JSON posting_stats column — filter in Python after fetching,
        # but fetch enough rows so the Python pass can fill the requested page.
        # Fetch up to 5× limit before applying the status filter, then slice.
        fetch_limit = limit * 5 if status_filter else limit
        q = q.limit(fetch_limit).offset(offset)

        result = await db.execute(q)
        rows = result.scalars().all()

        items: list[BloggerStatsItem] = []
        for b in rows:
            item = _blogger_to_item(b)
            if status_filter and item.status != status_filter:
                continue
            items.append(item)
            if len(items) >= limit:
                break
        return items
    except Exception as e:
        logger.error("Error listing adaptive bloggers: %s", e, exc_info=True)
        raise HTTPException(status_code=500, detail=str(e)[:200])


@router.get("/bloggers/{blogger_id}", response_model=BloggerStatsItem)
async def get_adaptive_blogger(
    blogger_id: int,
    _: None = Depends(verify_admin_header),
    db: AsyncSession = Depends(get_db),
) -> BloggerStatsItem:
    """Return one blogger by id so search/card links are not limited by list pagination."""
    result = await db.execute(select(MonitoredBlogger).filter(MonitoredBlogger.id == blogger_id))
    blogger = result.scalar_one_or_none()
    if not blogger:
        raise HTTPException(status_code=404, detail="Blogger not found")
    return _blogger_to_item(blogger)


@router.patch("/bloggers/{blogger_id}/priority", response_model=BloggerStatsItem)
async def set_blogger_priority(
    blogger_id: int,
    body: PriorityModeUpdate,
    _: None = Depends(verify_admin_header),
    db: AsyncSession = Depends(get_db),
) -> BloggerStatsItem:
    """Set blogger priority_mode. Admin only."""
    if body.priority_mode not in VALID_PRIORITY_MODES:
        raise HTTPException(
            status_code=422,
            detail=f"priority_mode must be one of: {sorted(VALID_PRIORITY_MODES)}",
        )
    try:
        result = await db.execute(select(MonitoredBlogger).filter(MonitoredBlogger.id == blogger_id))
        blogger = result.scalar_one_or_none()
        if not blogger:
            raise HTTPException(status_code=404, detail="Blogger not found")

        old_mode = blogger.priority_mode or "auto"
        blogger.priority_mode = body.priority_mode
        await db.commit()
        await db.refresh(blogger)

        logger.info("[ADAPTIVE] priority_mode @%s: %s → %s", blogger.username, old_mode, body.priority_mode)
        return _blogger_to_item(blogger)
    except HTTPException:
        raise
    except Exception as e:
        logger.error("Error setting blogger priority: %s", e, exc_info=True)
        raise HTTPException(status_code=500, detail=str(e)[:200])


@router.patch("/bloggers/{blogger_id}/overrides", response_model=BloggerStatsItem)
async def set_blogger_overrides(
    blogger_id: int,
    body: BloggerOverrideUpdate,
    _: None = Depends(verify_admin_header),
    db: AsyncSession = Depends(get_db),
) -> BloggerStatsItem:
    """Set per-blogger interval overrides and notes. Admin only."""
    try:
        result = await db.execute(select(MonitoredBlogger).filter(MonitoredBlogger.id == blogger_id))
        blogger = result.scalar_one_or_none()
        if not blogger:
            raise HTTPException(status_code=404, detail="Blogger not found")

        provided_fields = getattr(body, "model_fields_set", getattr(body, "__fields_set__", set()))
        if "override_min_interval" in provided_fields:
            blogger.override_min_interval = body.override_min_interval
        if "override_max_interval" in provided_fields:
            blogger.override_max_interval = body.override_max_interval
        if "notes" in provided_fields:
            blogger.notes = body.notes or None

        # Validate ordering after applying both values
        final_min = blogger.override_min_interval
        final_max = blogger.override_max_interval
        if final_min is not None and final_max is not None and final_min > final_max:
            raise HTTPException(
                status_code=400,
                detail=f"override_min_interval ({final_min}) must be ≤ override_max_interval ({final_max})",
            )

        await db.commit()
        await db.refresh(blogger)

        logger.info("[ADAPTIVE] overrides updated for @%s", blogger.username)
        return _blogger_to_item(blogger)
    except HTTPException:
        raise
    except Exception as e:
        logger.error("Error setting blogger overrides: %s", e, exc_info=True)
        raise HTTPException(status_code=500, detail=str(e)[:200])


@router.patch("/bloggers/{blogger_id}/username", response_model=BloggerStatsItem)
async def rename_blogger_username(
    blogger_id: int,
    body: BloggerUsernameUpdate,
    _: None = Depends(verify_admin_header),
    db: AsyncSession = Depends(get_db),
) -> BloggerStatsItem:
    """Rename a monitored blogger and update linked user subscriptions."""
    new_username = _normalize_blogger_username(body.username)
    if not USERNAME_PATTERN.match(new_username):
        raise HTTPException(
            status_code=422,
            detail="Username must be 2-64 chars: letters, digits, dot or underscore.",
        )

    try:
        result = await db.execute(select(MonitoredBlogger).filter(MonitoredBlogger.id == blogger_id))
        blogger = result.scalar_one_or_none()
        if not blogger:
            raise HTTPException(status_code=404, detail="Blogger not found")

        old_username = blogger.username or ""
        if old_username.lower() == new_username:
            return _blogger_to_item(blogger)

        conflict_result = await db.execute(
            select(MonitoredBlogger.id)
            .filter(func.lower(MonitoredBlogger.username) == new_username)
            .filter(MonitoredBlogger.id != blogger_id)
            .limit(1)
        )
        if conflict_result.scalar_one_or_none():
            raise HTTPException(status_code=409, detail=f"Blogger @{new_username} already exists")

        now = datetime.now(timezone.utc)
        stats = blogger.posting_stats if isinstance(blogger.posting_stats, dict) else {}
        stats = dict(stats)
        stats.pop("profile_access_failure", None)
        stats.pop("monitor_failure", None)
        stats["renamed_from"] = old_username
        stats["renamed_at"] = now.isoformat()

        blogger.username = new_username
        blogger.empty_streak = 0
        blogger.posting_stats = stats
        blogger.next_check_at = now
        blogger.lock_until = None
        blogger.lock_owner = None
        if blogger.priority_mode == "paused":
            blogger.priority_mode = "auto"

        await db.execute(
            update(CreatorSource)
            .where(CreatorSource.monitored_blogger_id == blogger_id)
            .values(username=new_username)
        )
        await db.commit()
        await db.refresh(blogger)

        logger.info("[ADAPTIVE] Blogger renamed @%s → @%s (id=%d)", old_username, new_username, blogger_id)
        return _blogger_to_item(blogger)
    except HTTPException:
        raise
    except IntegrityError as e:
        await db.rollback()
        logger.warning("Rename blogger conflict for id=%d username=%s: %s", blogger_id, new_username, e)
        raise HTTPException(
            status_code=409,
            detail=(
                f"Cannot rename to @{new_username}: one of the linked users already "
                "has this username in their monitoring list."
            ),
        )
    except Exception as e:
        await db.rollback()
        logger.error("Error renaming blogger %d: %s", blogger_id, e, exc_info=True)
        raise HTTPException(status_code=500, detail=str(e)[:200])


@router.post("/bloggers/{blogger_id}/recheck", response_model=BloggerStatsItem)
async def force_blogger_recheck(
    blogger_id: int,
    _: None = Depends(verify_admin_header),
    db: AsyncSession = Depends(get_db),
) -> BloggerStatsItem:
    """Force a blogger to be rechecked on the next worker tick by setting next_check_at to now."""
    try:
        result = await db.execute(select(MonitoredBlogger).filter(MonitoredBlogger.id == blogger_id))
        blogger = result.scalar_one_or_none()
        if not blogger:
            raise HTTPException(status_code=404, detail="Blogger not found")

        blogger.next_check_at = datetime.now(timezone.utc)
        blogger.lock_until = None
        blogger.lock_owner = None
        await db.commit()
        await db.refresh(blogger)

        logger.info("[ADAPTIVE] force-recheck scheduled for @%s", blogger.username)
        return _blogger_to_item(blogger)
    except HTTPException:
        raise
    except Exception as e:
        logger.error("Error forcing recheck for blogger %d: %s", blogger_id, e, exc_info=True)
        raise HTTPException(status_code=500, detail=str(e)[:200])


@router.delete("/bloggers/{blogger_id}", status_code=200)
async def delete_blogger(
    blogger_id: int,
    _: None = Depends(verify_admin_header),
    db: AsyncSession = Depends(get_db),
) -> dict:
    """Permanently remove one blogger from monitoring for all users."""
    try:
        result = await db.execute(select(MonitoredBlogger).filter(MonitoredBlogger.id == blogger_id))
        blogger = result.scalar_one_or_none()
        if not blogger:
            raise HTTPException(status_code=404, detail="Blogger not found")

        username = str(blogger.username or "")
        normalized_username = username.lower()
        creator_rows = await db.execute(
            select(CreatorSource.id).where(
                or_(
                    CreatorSource.monitored_blogger_id == blogger_id,
                    func.lower(CreatorSource.username) == normalized_username,
                )
            )
        )
        creator_ids = [int(row_id) for row_id in creator_rows.scalars().all()]

        deleted_outbox = deleted_videos = deleted_batches = deleted_sources = 0
        outbox_filter = MonitorOutbox.blogger_id == blogger_id
        if creator_ids:
            outbox_filter = or_(outbox_filter, MonitorOutbox.creator_id.in_(creator_ids))

            outbox_result = await db.execute(delete(MonitorOutbox).where(outbox_filter))
            deleted_outbox = int(outbox_result.rowcount or 0)

            video_result = await db.execute(delete(CreatorVideo).where(CreatorVideo.creator_id.in_(creator_ids)))
            deleted_videos = int(video_result.rowcount or 0)

            batch_result = await db.execute(delete(VideoBatch).where(VideoBatch.creator_id.in_(creator_ids)))
            deleted_batches = int(batch_result.rowcount or 0)

            source_result = await db.execute(delete(CreatorSource).where(CreatorSource.id.in_(creator_ids)))
            deleted_sources = int(source_result.rowcount or 0)
        else:
            outbox_result = await db.execute(delete(MonitorOutbox).where(outbox_filter))
            deleted_outbox = int(outbox_result.rowcount or 0)

        check_log_result = await db.execute(delete(BloggerCheckLog).where(BloggerCheckLog.blogger_id == blogger_id))
        deleted_check_logs = int(check_log_result.rowcount or 0)

        blogger_result = await db.execute(delete(MonitoredBlogger).where(MonitoredBlogger.id == blogger_id))
        deleted_bloggers = int(blogger_result.rowcount or 0)
        await db.commit()

        logger.info(
            "[ADAPTIVE] Blogger @%s (id=%d) permanently deleted by admin: sources=%d videos=%d batches=%d outbox=%d logs=%d",
            username,
            blogger_id,
            deleted_sources,
            deleted_videos,
            deleted_batches,
            deleted_outbox,
            deleted_check_logs,
        )
        return {
            "ok": True,
            "username": username,
            "deleted_id": blogger_id,
            "deleted_bloggers": deleted_bloggers,
            "deleted_sources": deleted_sources,
            "deleted_videos": deleted_videos,
            "deleted_batches": deleted_batches,
            "deleted_outbox": deleted_outbox,
            "deleted_check_logs": deleted_check_logs,
        }
    except HTTPException:
        raise
    except Exception as e:
        await db.rollback()
        logger.error("Error deleting blogger %d: %s", blogger_id, e, exc_info=True)
        raise HTTPException(status_code=500, detail=str(e)[:200])


# ── Check log endpoint ─────────────────────────────────────────────────────────

@router.get("/bloggers/{blogger_id}/log", response_model=list[CheckLogItem])
async def get_blogger_check_log(
    blogger_id: int,
    limit: int = Query(50, ge=1, le=200),
    _: None = Depends(verify_admin_header),
    db: AsyncSession = Depends(get_db),
) -> list[CheckLogItem]:
    """Recent check log entries for a specific blogger."""
    try:
        result = await db.execute(
            select(BloggerCheckLog)
            .filter(BloggerCheckLog.blogger_id == blogger_id)
            .order_by(desc(BloggerCheckLog.checked_at))
            .limit(limit)
        )
        rows = result.scalars().all()
        return [
            CheckLogItem(
                id=row.id,
                checked_at=row.checked_at.isoformat() if row.checked_at else "",
                found_new_count=row.found_new_count or 0,
                interval_used_minutes=row.interval_used_minutes,
                next_interval_minutes=row.next_interval_minutes,
                status=row.status,
                adaptive_status=row.adaptive_status,
                reason=row.reason,
                duration_ms=row.duration_ms,
                detection_latency_min=row.detection_latency_min,
            )
            for row in rows
        ]
    except Exception as e:
        logger.error("Error fetching check log for blogger %d: %s", blogger_id, e, exc_info=True)
        raise HTTPException(status_code=500, detail=str(e)[:200])


@router.get("/bloggers/{blogger_id}/videos", response_model=VideoAnalyticsResponse)
async def get_blogger_video_analytics(
    blogger_id: int,
    limit: int = Query(25, ge=1, le=100),
    _: None = Depends(verify_admin_header),
    db: AsyncSession = Depends(get_db),
) -> VideoAnalyticsResponse:
    """Recent discovered videos for a blogger with per-video detection analytics."""
    try:
        blogger_result = await db.execute(select(MonitoredBlogger).filter(MonitoredBlogger.id == blogger_id))
        blogger = blogger_result.scalar_one_or_none()
        if not blogger:
            raise HTTPException(status_code=404, detail="Blogger not found")

        # One row per unique video_id (many subscribers create duplicate rows for the same video)
        dedup_subq = (
            select(func.min(CreatorVideo.id).label("min_id"))
            .join(CreatorSource, CreatorVideo.creator_id == CreatorSource.id)
            .filter(CreatorSource.monitored_blogger_id == blogger_id)
            .group_by(CreatorVideo.video_id)
        )
        rows_result = await db.execute(
            select(CreatorVideo, CreatorSource)
            .join(CreatorSource, CreatorVideo.creator_id == CreatorSource.id)
            .filter(CreatorVideo.id.in_(dedup_subq))
            .order_by(desc(CreatorVideo.discovered_at), desc(CreatorVideo.id))
            .limit(limit)
        )
        rows = rows_result.all()

        items: list[VideoAnalyticsItem] = []
        latencies: list[int] = []
        for video, creator in rows:
            meta = video.meta or {}
            if not isinstance(meta, dict):
                meta = {}
            latency = meta.get("detection_latency_min")
            try:
                latency_int = int(latency) if latency is not None else None
            except (TypeError, ValueError):
                latency_int = None
            if latency_int is not None:
                latencies.append(latency_int)

            create_time = meta.get("create_time")
            try:
                create_time_int = int(create_time) if create_time is not None else None
            except (TypeError, ValueError):
                create_time_int = None

            items.append(
                VideoAnalyticsItem(
                    id=video.id,
                    creator_id=creator.id,
                    creator_username=creator.username or "",
                    user_id=creator.user_id,
                    video_id=video.video_id,
                    url=video.url,
                    status=video.status,
                    discovered_at=video.discovered_at.isoformat() if video.discovered_at else None,
                    create_time=create_time_int,
                    detected_at=meta.get("detected_at"),
                    detection_latency_min=latency_int,
                    age_at_detection_hours=meta.get("age_at_detection_hours"),
                    source=meta.get("source"),
                    canonical_source=meta.get("canonical_source"),
                    scan_status=meta.get("scan_status"),
                    batch_size=meta.get("batch_size"),
                    batch_index=meta.get("batch_index"),
                )
            )

        return VideoAnalyticsResponse(
            blogger_id=blogger.id,
            username=blogger.username or "",
            returned=len(items),
            latency_summary=_latency_summary(latencies),
            videos=items,
        )
    except HTTPException:
        raise
    except Exception as e:
        logger.error("Error fetching video analytics for blogger %d: %s", blogger_id, e, exc_info=True)
        raise HTTPException(status_code=500, detail=str(e)[:200])


# ── Summary endpoint ──────────────────────────────────────────────────────────

@router.get("/summary", response_model=AdaptiveSummaryResponse)
async def get_adaptive_summary(
    _: None = Depends(verify_admin_header),
    db: AsyncSession = Depends(get_db),
) -> AdaptiveSummaryResponse:
    """
    Dashboard-level summary: counts by status/priority, avg interval,
    prediction stats, and the next 10 upcoming checks.
    """
    try:
        active_sub_subq = (
            select(CreatorSource.id)
            .where(CreatorSource.monitored_blogger_id == MonitoredBlogger.id)
            .where(CreatorSource.enabled == True)
            .exists()
        )
        result = await db.execute(
            select(MonitoredBlogger)
            .where(active_sub_subq)
            .order_by(MonitoredBlogger.next_check_at.asc().nulls_last())
        )
        bloggers = result.scalars().all()

        by_status: Dict[str, int] = {}
        by_priority: Dict[str, int] = {}
        intervals: list[int] = []
        pred_confidences: list[float] = []
        with_prediction = 0
        burst_active = 0
        calibrating = 0
        paused = 0

        for b in bloggers:
            stats = b.posting_stats or {}
            priority = b.priority_mode or "auto"
            status = stats.get("status") or "calibrating"

            by_status[status] = by_status.get(status, 0) + 1
            by_priority[priority] = by_priority.get(priority, 0) + 1

            if priority == "paused":
                paused += 1

            if status == "calibrating":
                calibrating += 1

            if stats.get("is_burst"):
                burst_active += 1

            interval = stats.get("next_interval_minutes")
            if interval is not None:
                intervals.append(interval)

            conf = stats.get("predicted_confidence")
            pred_ts = stats.get("predicted_next_post_ts")
            if conf is not None and pred_ts is not None:
                with_prediction += 1
                pred_confidences.append(float(conf))

        now = datetime.now(timezone.utc)
        next_checks = []
        for b in bloggers:
            if len(next_checks) >= 10:
                break
            if (b.priority_mode or "auto") == "paused":
                continue
            if b.next_check_at is None:
                continue
            stats = b.posting_stats or {}
            next_checks.append({
                "id": b.id,
                "username": b.username or "",
                "next_check_at": b.next_check_at.isoformat(),
                "interval": stats.get("next_interval_minutes"),
                "status": stats.get("status") or "calibrating",
                "priority": b.priority_mode or "auto",
            })

        return AdaptiveSummaryResponse(
            total_bloggers=len(bloggers),
            by_status=by_status,
            by_priority=by_priority,
            avg_interval_minutes=round(sum(intervals) / len(intervals), 1) if intervals else None,
            with_prediction=with_prediction,
            avg_prediction_confidence=round(sum(pred_confidences) / len(pred_confidences), 3) if pred_confidences else None,
            burst_active=burst_active,
            calibrating=calibrating,
            paused=paused,
            next_checks=next_checks,
        )
    except Exception as e:
        logger.error("Error computing adaptive summary: %s", e, exc_info=True)
        raise HTTPException(status_code=500, detail=str(e)[:200])


# ── Backfill endpoints ─────────────────────────────────────────────────────────

_BACKFILL_LOCK_KEY = "monitor:adaptive:backfill:running"
_BACKFILL_LOCK_TTL_SEC = 600  # 10 minutes — generous upper bound for very large datasets


def _acquire_backfill_lock() -> tuple[bool, Optional[str]]:
    """Try to acquire a Redis-backed singleton lock for backfill.

    Returns (acquired, owner_token). owner_token is the value stored in Redis;
    pass it back to _release_backfill_lock so we don't release someone else's lock
    if our run was abnormally long and the TTL expired.
    """
    try:
        from services.redis_service import get_redis
        import secrets

        token = secrets.token_hex(8)
        redis_client = get_redis()
        ok = redis_client.set(_BACKFILL_LOCK_KEY, token, nx=True, ex=_BACKFILL_LOCK_TTL_SEC)
        if ok:
            return True, token
        return False, None
    except Exception as exc:
        # If Redis is down we degrade to "no lock" — better than blocking the feature outright.
        logger.warning("[BACKFILL] Could not acquire Redis lock, proceeding unguarded: %s", exc)
        return True, None


def _release_backfill_lock(token: Optional[str]) -> None:
    if not token:
        return
    try:
        from services.redis_service import get_redis

        redis_client = get_redis()
        # Compare-and-delete so a stale owner does not release a fresh lock.
        current = redis_client.get(_BACKFILL_LOCK_KEY)
        if isinstance(current, bytes):
            current = current.decode("utf-8", errors="ignore")
        if current == token:
            redis_client.delete(_BACKFILL_LOCK_KEY)
    except Exception as exc:
        logger.debug("[BACKFILL] Lock release failed (non-fatal): %s", exc)


@router.post("/backfill", response_model=BackfillResult)
async def run_backfill(
    days: int = Query(90, ge=7, le=365),
    force: bool = Query(False),
    reset_feedback: bool = Query(False),
    _: None = Depends(verify_admin_header),
) -> BackfillResult:
    """
    Reconstruct posting_stats for all uncalibrated bloggers from historical video data.
    Runs synchronously; may take several seconds for large datasets.

    A Redis-backed singleton lock prevents two concurrent backfills (e.g. impatient
    double-click) from doubling DB load.
    """
    from services.adaptive_backfill import backfill_all_bloggers

    acquired, lock_token = _acquire_backfill_lock()
    if not acquired:
        raise HTTPException(
            status_code=409,
            detail="Backfill is already running. Try again in a few minutes.",
        )

    try:
        def _run() -> dict:
            with get_sync_db_context() as db:
                return backfill_all_bloggers(
                    db,
                    days=days,
                    force=force,
                    reset_feedback=reset_feedback,
                )

        result = await asyncio.to_thread(_run)
        logger.info(
            "[BACKFILL] API-triggered backfill complete (force=%s reset_feedback=%s): %s",
            force,
            reset_feedback,
            result,
        )
        return BackfillResult(**result)
    except Exception as e:
        logger.error("Error running backfill: %s", e, exc_info=True)
        raise HTTPException(status_code=500, detail=str(e)[:200])
    finally:
        _release_backfill_lock(lock_token)


# ── Learning report ────────────────────────────────────────────────────────────

@router.get("/learning-report", response_model=LearningReportResponse)
async def get_learning_report(
    _: None = Depends(verify_admin_header),
    db: AsyncSession = Depends(get_db),
) -> LearningReportResponse:
    """
    Self-learning effectiveness report: calibration progress, interval efficiency,
    detection latency stats, and feedback loop health over the last 7 days.
    """
    try:
        from services.app_settings_service import (
            get_adaptive_enabled,
            get_adaptive_min_interval,
            get_adaptive_max_interval,
        )
        from db.database import get_sync_db_context as _sync_ctx

        FLAT_BASELINE = 30  # minutes — reference flat interval

        # ── 1. Fetch all bloggers with active subscriptions ────────────────────
        active_sub_subq = (
            select(CreatorSource.id)
            .where(CreatorSource.monitored_blogger_id == MonitoredBlogger.id)
            .where(CreatorSource.enabled == True)
            .exists()
        )
        result = await db.execute(
            select(MonitoredBlogger).where(active_sub_subq)
        )
        bloggers = result.scalars().all()

        # ── 2. Calibration stats ───────────────────────────────────────────────
        total = len(bloggers)
        calibrated_count = 0
        calibrating_count = 0
        recently_calibrated = 0
        status_dist: Dict[str, int] = {}
        intervals: list[int] = []
        latency_emas: list[float] = []
        fastest_items: list[dict] = []

        now = datetime.now(timezone.utc)
        cutoff_24h = now - timedelta(hours=24)

        from services.app_settings_service import get_adaptive_feedback_enabled, get_adaptive_feedback_target_latency, get_adaptive_feedback_late_threshold

        def _get_sync_settings():
            with _sync_ctx() as sdb:
                return {
                    "feedback_enabled": get_adaptive_feedback_enabled(sdb),
                    "feedback_target": get_adaptive_feedback_target_latency(sdb),
                    "feedback_late_threshold": get_adaptive_feedback_late_threshold(sdb),
                }

        sync_settings = await asyncio.to_thread(_get_sync_settings)
        feedback_enabled: bool = sync_settings["feedback_enabled"]
        feedback_target: int = sync_settings["feedback_target"]
        feedback_late_threshold: float = sync_settings["feedback_late_threshold"]

        bloggers_improving = 0
        bloggers_late = 0

        for b in bloggers:
            stats = b.posting_stats or {}
            status = stats.get("status") or "calibrating"
            status_dist[status] = status_dist.get(status, 0) + 1

            if status == "calibrating":
                calibrating_count += 1
            else:
                calibrated_count += 1

            computed_at_raw = stats.get("computed_at")
            if computed_at_raw:
                try:
                    computed_dt = datetime.fromisoformat(computed_at_raw.replace("Z", "+00:00"))
                    if computed_dt.tzinfo is None:
                        computed_dt = computed_dt.replace(tzinfo=timezone.utc)
                    if computed_dt >= cutoff_24h:
                        recently_calibrated += 1
                except Exception:
                    pass

            interval = stats.get("next_interval_minutes")
            if interval is not None:
                intervals.append(int(interval))
                fastest_items.append({
                    "id": b.id,
                    "username": b.username or "",
                    "interval": int(interval),
                    "status": status,
                    "priority": b.priority_mode or "auto",
                })

            # Feedback loop metrics
            ema = getattr(b, "latency_ema_minutes", None)
            if ema is not None:
                latency_emas.append(float(ema))
                if float(ema) < feedback_target:
                    bloggers_improving += 1
                elif float(ema) >= feedback_target * feedback_late_threshold:
                    bloggers_late += 1

        avg_interval = round(statistics.mean(intervals), 1) if intervals else None
        interval_saved_pct = (
            round((FLAT_BASELINE - avg_interval) / FLAT_BASELINE * 100, 1)
            if avg_interval is not None and avg_interval < FLAT_BASELINE
            else None
        )
        calibration_pct = round(calibrated_count / total * 100, 1) if total else 0.0
        avg_latency_ema = round(statistics.mean(latency_emas), 1) if latency_emas else None

        # Top 5 fastest (by interval ascending)
        fastest_items.sort(key=lambda x: x["interval"])
        fastest_top5 = fastest_items[:5]

        # ── 3. Detection latency from BloggerCheckLog (7-day window) ──────────
        cutoff_7d = now - timedelta(days=7)
        log_result = await db.execute(
            select(BloggerCheckLog)
            .where(BloggerCheckLog.checked_at >= cutoff_7d)
            .where(BloggerCheckLog.status == "OK")
        )
        log_rows = log_result.scalars().all()

        total_checks_7d = len(log_rows)
        find_rows = [r for r in log_rows if (r.found_new_count or 0) > 0]
        finds_7d = len(find_rows)
        latencies = [r.detection_latency_min for r in find_rows if r.detection_latency_min is not None]

        avg_latency_7d = round(statistics.mean(latencies), 1) if latencies else None
        p50_latency_7d = round(statistics.median(latencies), 1) if latencies else None
        find_rate_7d = round(finds_7d / total_checks_7d, 3) if total_checks_7d else None

        return LearningReportResponse(
            total_bloggers=total,
            calibrated=calibrated_count,
            calibrating=calibrating_count,
            calibration_pct=calibration_pct,
            recently_calibrated_24h=recently_calibrated,
            avg_interval_minutes=avg_interval,
            flat_baseline_minutes=FLAT_BASELINE,
            interval_saved_pct=interval_saved_pct,
            status_distribution=status_dist,
            avg_latency_7d=avg_latency_7d,
            p50_latency_7d=p50_latency_7d,
            finds_7d=finds_7d,
            total_checks_7d=total_checks_7d,
            find_rate_7d=find_rate_7d,
            feedback_enabled=feedback_enabled,
            feedback_target_latency=feedback_target,
            avg_latency_ema=avg_latency_ema,
            bloggers_improving=bloggers_improving,
            bloggers_late=bloggers_late,
            fastest=fastest_top5,
        )
    except Exception as e:
        logger.error("Error computing learning report: %s", e, exc_info=True)
        raise HTTPException(status_code=500, detail=str(e)[:200])
