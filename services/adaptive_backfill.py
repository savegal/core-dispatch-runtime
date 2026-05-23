"""
Adaptive monitoring backfill service.

Reconstructs posting_stats from historical creator_videos.discovered_at for
bloggers that have never been calibrated (posting_stats IS NULL or status='calibrating').

Usage:
    from services.adaptive_backfill import backfill_all_bloggers, backfill_blogger

The function is safe to call repeatedly — it skips bloggers that already have
a non-calibrating adaptive profile.
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone, timedelta
from typing import Optional

import dataclasses
from sqlalchemy import func
from sqlalchemy.orm import Session

from db.models import MonitoredBlogger, CreatorSource, CreatorVideo
from services.adaptive_scheduler import (
    AdaptiveSettings,
    AdaptiveResult,
    SCHEMA_VERSION,
    compute_adaptive_interval,
    merge_timestamps,
)
from services.app_settings_service import (
    get_adaptive_min_samples,
    get_adaptive_max_keep,
    get_adaptive_max_history_days,
    get_adaptive_initial_limit,
    get_adaptive_gap_divisor,
    get_adaptive_min_interval,
    get_adaptive_max_interval,
    get_adaptive_sanity_cap_factor,
    get_adaptive_active_phase_interval,
    get_adaptive_active_phase_hours,
    get_adaptive_found_new_reset_factor,
    get_adaptive_burst_threshold_24h,
    get_adaptive_burst_multiplier,
    get_adaptive_burst_duration_hours,
    get_adaptive_silence_multiplier,
    get_adaptive_silence_max_factor,
    get_adaptive_nighttime_multiplier,
    get_adaptive_empty_streak_threshold,
    get_adaptive_empty_streak_multiplier,
    get_adaptive_use_p25_for_burst,
    get_adaptive_hour_histogram_min_total,
    get_adaptive_active_hour_threshold,
    get_adaptive_dead_hour_threshold,
    get_adaptive_active_hour_multiplier,
    get_adaptive_burst_relative_factor,
    get_adaptive_prediction_min_confidence,
    get_adaptive_prediction_buffer_minutes,
    get_adaptive_prediction_high_confidence,
    get_adaptive_recent_gap_weight,
)

logger = logging.getLogger("services.adaptive_backfill")

BACKFILL_DEFAULT_DAYS = 90


def _is_backfill_target(stats: dict, force: bool) -> bool:
    if force:
        return True
    if not stats:
        return True
    if stats.get("schema_version") != SCHEMA_VERSION:
        return True
    return stats.get("status") in (None, "calibrating")


def _to_utc_timestamp(value) -> Optional[int]:
    if value is None:
        return None
    if isinstance(value, datetime):
        dt = value if value.tzinfo is not None else value.replace(tzinfo=timezone.utc)
        return int(dt.astimezone(timezone.utc).timestamp())
    return int(value)


def _backfill_calibrating_stats(
    *,
    timestamps: list[int],
    settings: AdaptiveSettings,
    reason: str,
    backfill_status: str,
) -> dict:
    return {
        "schema_version": SCHEMA_VERSION,
        "video_timestamps": timestamps,
        "status": "calibrating",
        "post_count_analyzed": len(timestamps),
        "reason": reason,
        "backfill_status": backfill_status,
        "computed_at": datetime.now(timezone.utc).isoformat(),
        "min_samples_required": settings.min_samples,
    }


def _load_settings(db: Session) -> AdaptiveSettings:
    return AdaptiveSettings(
        min_interval_minutes=get_adaptive_min_interval(db),
        max_interval_minutes=get_adaptive_max_interval(db),
        sanity_cap_factor=get_adaptive_sanity_cap_factor(db),
        active_phase_interval_minutes=get_adaptive_active_phase_interval(db),
        active_phase_hours=get_adaptive_active_phase_hours(db),
        found_new_reset_factor=get_adaptive_found_new_reset_factor(db),
        burst_threshold_24h=get_adaptive_burst_threshold_24h(db),
        burst_multiplier=get_adaptive_burst_multiplier(db),
        burst_duration_hours=get_adaptive_burst_duration_hours(db),
        silence_multiplier=get_adaptive_silence_multiplier(db),
        silence_max_factor=get_adaptive_silence_max_factor(db),
        nighttime_multiplier=get_adaptive_nighttime_multiplier(db),
        empty_streak_threshold=get_adaptive_empty_streak_threshold(db),
        empty_streak_multiplier=get_adaptive_empty_streak_multiplier(db),
        gap_divisor=get_adaptive_gap_divisor(db),
        use_p25_for_burst=get_adaptive_use_p25_for_burst(db),
        min_samples=get_adaptive_min_samples(db),
        hour_histogram_min_total=get_adaptive_hour_histogram_min_total(db),
        active_hour_threshold=get_adaptive_active_hour_threshold(db),
        dead_hour_threshold=get_adaptive_dead_hour_threshold(db),
        active_hour_multiplier=get_adaptive_active_hour_multiplier(db),
        max_keep=get_adaptive_max_keep(db),
        max_history_days=get_adaptive_max_history_days(db),
        initial_limit=get_adaptive_initial_limit(db),
        burst_relative_factor=get_adaptive_burst_relative_factor(db),
        prediction_min_confidence=get_adaptive_prediction_min_confidence(db),
        prediction_buffer_minutes=get_adaptive_prediction_buffer_minutes(db),
        prediction_high_confidence=get_adaptive_prediction_high_confidence(db),
        recent_gap_weight=get_adaptive_recent_gap_weight(db),
    )


def _reset_feedback_state(blogger: MonitoredBlogger) -> None:
    """Clear EMA-based feedback state so it does not contradict freshly recomputed posting_stats.

    Historical counters (total_videos_caught / total_late_catches) are preserved on purpose:
    they are used for audit / diagnostics, not for the feedback loop.
    """
    if hasattr(blogger, "latency_ema_minutes"):
        blogger.latency_ema_minutes = None
    if hasattr(blogger, "last_detection_latency_min"):
        blogger.last_detection_latency_min = None


def backfill_blogger(
    db: Session,
    blogger: MonitoredBlogger,
    settings: AdaptiveSettings,
    days: int = BACKFILL_DEFAULT_DAYS,
    force: bool = False,
    reset_feedback: bool = False,
) -> int:
    """
    Reconstruct posting_stats for a single blogger from creator_videos history.

    Args:
        db:             Open sync DB session.
        blogger:        MonitoredBlogger ORM object.
        settings:       AdaptiveSettings (loaded once outside to avoid repeated DB reads).
        days:           How many days of history to look back.
        force:          If True, overwrite even if blogger already has a good profile.
        reset_feedback: If True, also clear latency_ema / last_detection_latency so the feedback
                        loop re-learns alongside the freshly recomputed rhythm. Historical
                        counters (total_videos_caught / total_late_catches) are kept.

    Returns:
        Number of timestamps found (0 if skipped or no data).
    """
    locked_blogger = (
        db.query(MonitoredBlogger)
        .filter(MonitoredBlogger.id == blogger.id)
        .with_for_update()
        .populate_existing()
        .first()
    )
    if not locked_blogger:
        return 0
    blogger = locked_blogger

    existing_stats = blogger.posting_stats or {}
    if not _is_backfill_target(existing_stats, force):
        return 0  # already calibrated — skip

    if reset_feedback:
        _reset_feedback_state(blogger)

    cutoff = datetime.now(timezone.utc) - timedelta(days=days)

    # Collect discovered_at from all creator_sources linked to this blogger
    rows = (
        db.query(CreatorVideo.meta, CreatorVideo.discovered_at)
        .join(CreatorSource, CreatorVideo.creator_id == CreatorSource.id)
        .filter(
            CreatorSource.monitored_blogger_id == blogger.id,
            CreatorVideo.discovered_at >= cutoff,
        )
        .all()
    )

    if not rows:
        # No videos found. Keep the profile in calibration, but do not preserve
        # stale derived fields from an older adaptive run.
        db.refresh(blogger)
        existing_stats = blogger.posting_stats or {}
        if not _is_backfill_target(existing_stats, force):
            return 0
        blogger.posting_stats = _backfill_calibrating_stats(
            timestamps=[],
            settings=settings,
            reason=f"backfill: no historical videos in last {days}d",
            backfill_status="no_history",
        )
        return 0

    # Split rows into "has real TikTok create_time" vs "discovered_at only".
    # Mixing the two is dangerous: discovered_at clusters at the moments WE
    # parsed (which can be days after publication for backlog catch-ups), so
    # mixed series produce fake-burst median/P25 and a wrong calibration.
    #
    # Policy: if at least 50% of rows have a real create_time, USE ONLY those
    # rows; otherwise fall back to discovered_at (legacy data, best effort).
    real_ts: list[int] = []
    fallback_ts: list[int] = []
    for meta, discovered_at in rows:
        meta_dict = meta or {}
        create_time = meta_dict.get("create_time")
        if create_time and isinstance(create_time, (int, float)) and create_time > 0:
            real_ts.append(int(create_time))
        elif discovered_at:
            fallback_ts_value = _to_utc_timestamp(discovered_at)
            if fallback_ts_value is not None:
                fallback_ts.append(fallback_ts_value)

    total_rows = len(rows)
    real_share = len(real_ts) / max(total_rows, 1)
    if real_share >= 0.5:
        timestamps = real_ts
        used_source = "create_time"
    else:
        # Either no real timestamps or too few — old data without meta.create_time.
        # We accept discovered_at as last resort; the blogger will recalibrate
        # naturally as new videos are observed (which now always carry create_time).
        timestamps = real_ts + fallback_ts
        used_source = "discovered_at_fallback"

    now_ts = int(datetime.now(timezone.utc).timestamp())
    merged = merge_timestamps(
        existing_stats.get("video_timestamps", []),
        timestamps,
        max_keep=settings.max_keep,
        max_age_days=settings.max_history_days,
        now_ts=now_ts,
    )

    if not merged:
        db.refresh(blogger)
        latest_stats = blogger.posting_stats or {}
        if _is_backfill_target(latest_stats, force):
            blogger.posting_stats = _backfill_calibrating_stats(
                timestamps=[],
                settings=settings,
                reason=f"backfill: no valid timestamps in last {days}d",
                backfill_status="no_valid_timestamps",
            )
        return 0

    result = compute_adaptive_interval(merged, now_ts, settings)
    db.refresh(blogger)
    latest_stats = blogger.posting_stats or {}
    if not _is_backfill_target(latest_stats, force):
        logger.info(
            "[BACKFILL] @%s skipped because fresh posting_stats appeared during backfill",
            blogger.username,
        )
        return 0
    if latest_stats is not existing_stats:
        merged = merge_timestamps(
            latest_stats.get("video_timestamps", []),
            merged,
            max_keep=settings.max_keep,
            max_age_days=settings.max_history_days,
            now_ts=now_ts,
        )
        result = compute_adaptive_interval(merged, now_ts, settings)

    if result is None:
        blogger.posting_stats = _backfill_calibrating_stats(
            timestamps=merged,
            settings=settings,
            reason=f"calibrating: {len(merged)}/{settings.min_samples} samples from backfill",
            backfill_status="insufficient_data",
        )
    else:
        stats_dict = dataclasses.asdict(result)
        stats_dict["video_timestamps"] = merged
        stats_dict["backfill_status"] = "calibrated"
        blogger.posting_stats = stats_dict

    logger.info(
        "[BACKFILL] @%s: %d timestamps (source=%s, real=%d/%d), status=%s",
        blogger.username,
        len(merged),
        used_source,
        len(real_ts),
        total_rows,
        blogger.posting_stats.get("status"),
    )
    return len(merged)


def backfill_all_bloggers(
    db: Session,
    days: int = BACKFILL_DEFAULT_DAYS,
    force: bool = False,
    progress_callback=None,
    reset_feedback: bool = False,
) -> dict:
    """
    Run backfill for all bloggers that need calibration.

    Args:
        db:                Open sync DB session.
        days:              History window in days.
        force:             Overwrite existing calibrated profiles.
        progress_callback: Optional callable(processed, total, blogger_username).
        reset_feedback:    If True, also wipe per-blogger latency_ema / last_detection_latency.
                           Useful when retraining the rhythm fundamentally so the feedback loop
                           does not steer intervals based on stale latency observations.

    Returns:
        dict with keys: total, processed, calibrated, skipped, total_timestamps
    """
    # Fetch all and filter in Python to avoid SQLite JSON1 extension dependency.
    all_bloggers = db.query(MonitoredBlogger).order_by(MonitoredBlogger.username).all()
    if force:
        bloggers = all_bloggers
    else:
        bloggers = [
            b for b in all_bloggers
            if _is_backfill_target(b.posting_stats or {}, force=False)
        ]

    total = len(bloggers)
    if total == 0:
        return {"total": 0, "processed": 0, "calibrated": 0, "skipped": 0, "total_timestamps": 0, "avg_timestamps": 0.0}

    try:
        settings = _load_settings(db)
    except Exception as e:
        logger.error("[BACKFILL] Failed to load settings: %s", e)
        settings = AdaptiveSettings()

    processed = 0
    calibrated = 0
    skipped = 0
    no_data = 0
    insufficient_data = 0
    errors = 0
    skipped_existing = 0
    total_timestamps = 0

    for blogger in bloggers:
        try:
            count = backfill_blogger(
                db,
                blogger,
                settings,
                days=days,
                force=force,
                reset_feedback=reset_feedback,
            )
            stats = blogger.posting_stats or {}
            status = stats.get("status")
            backfill_status = stats.get("backfill_status")
            if count > 0 and status not in (None, "calibrating"):
                calibrated += 1
                total_timestamps += count
            elif backfill_status == "insufficient_data":
                insufficient_data += 1
                skipped += 1
            elif backfill_status in ("no_history", "no_valid_timestamps"):
                no_data += 1
                skipped += 1
            else:
                skipped_existing += 1
                skipped += 1
            processed += 1

            # Commit every 10 bloggers to avoid one giant transaction
            if processed % 10 == 0:
                db.commit()
                if progress_callback:
                    try:
                        progress_callback(processed, total, blogger.username)
                    except Exception:
                        pass

        except Exception as e:
            logger.warning("[BACKFILL] Error processing @%s: %s", blogger.username, e)
            errors += 1
            skipped += 1
            processed += 1
            try:
                db.rollback()
            except Exception:
                pass

    try:
        db.commit()
    except Exception as e:
        logger.error("[BACKFILL] Final commit failed: %s", e)

    result = {
        "total": total,
        "processed": processed,
        "calibrated": calibrated,
        "skipped": skipped,
        "no_data": no_data,
        "insufficient_data": insufficient_data,
        "errors": errors,
        "skipped_existing": skipped_existing,
        "total_timestamps": total_timestamps,
        "avg_timestamps": round(total_timestamps / max(calibrated, 1), 1),
    }
    logger.info("[BACKFILL] Done: %s", result)
    return result
