"""
Enhanced adaptive scheduler for TikTok blogger monitoring.

Pure compute module — no DB access, no SQLAlchemy, only stdlib.
All thresholds come from AdaptiveSettings (populated from AppSettings).

Algorithm summary:
  1. found_new override  → immediate active_phase_interval (20m)
  2. Active phase        → recency curve (not a step), active_phase_interval..×3
  2.5 Post-active ramp  → smooth transition active→base, prevents jarring jumps
  3. Burst mode          → relative burst detection, P25-based, burst_multiplier
  4. Silence gradient    → gradual slow-down, not a hard jump to max
  5. Base slow           → median/gap_divisor, with hour-of-day and empty-streak tweaks
  6. Prediction          → if confidence >= threshold, pull forward check before predicted post
"""
from __future__ import annotations

import statistics
import math
from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Dict, List, Optional, Tuple

SCHEMA_VERSION = 7
_MIN_REASONABLE_TS = 946684800
_MAX_FUTURE_SKEW_SECONDS = 86400
# 5 minutes: treat TikTok clip batches as one publication for gap math. Burst
# detection still uses raw timestamps, so this does not hide real active days.
_MULTI_CLIP_CLUSTER_WINDOW_SECONDS = 300
_CHAOTIC_GAP_FLOOR_CV = 2.0
# Trend detection: compare last _TREND_RECENT_N gaps to older history.
# Requires at least _TREND_MIN_GAPS total so both sides have ≥3 samples each.
_TREND_RECENT_N = 5
_TREND_MIN_GAPS = 9


# ── Settings ───────────────────────────────────────────────────────────────────

@dataclass
class AdaptiveSettings:
    """All algorithm thresholds. Populated from AppSettings (DB layer)."""

    # Hard boundaries
    min_interval_minutes: int = 5
    max_interval_minutes: int = 300
    sanity_cap_factor: float = 2.0          # interval never > max * this

    # Acceleration
    active_phase_interval_minutes: int = 20
    active_phase_hours: float = 2.0
    # DEPRECATED: kept only so existing AppSettings rows / API contracts keep working.
    # Not consumed by the algorithm anymore (the active-phase override is handled by
    # active_phase_interval_minutes + active_phase_hours). Safe to ignore in any new code.
    found_new_reset_factor: float = 0.25
    burst_threshold_24h: int = 3            # absolute floor: burst if posts_24h >= this
    burst_multiplier: float = 0.5           # multiply base by this in burst mode
    burst_duration_hours: float = 24.0      # burst mode window after last post
    burst_relative_factor: float = 1.5     # burst if posts_24h >= expected_daily * this

    # Deceleration
    silence_multiplier: float = 4.5         # silence if age > median * this
    silence_max_factor: float = 2.0         # cap the gradient at base * this
    nighttime_multiplier: float = 2.0       # multiply in blogger's dead hours
    empty_streak_threshold: int = 5         # consecutive empties before slowdown
    empty_streak_multiplier: float = 1.3    # extra slowdown per empty streak

    # Base calculation
    gap_divisor: float = 30.0              # base = median_gap / this (higher = more frequent checks)
    use_p25_for_burst: bool = True          # use P25 instead of median for burst creators
    min_samples: int = 5                   # minimum timestamps to activate adaptive
    recent_gap_weight: float = 0.3         # weight of last-5 gaps vs full-history median (0=ignore, 1=only recent)

    # Prediction
    prediction_min_confidence: float = 0.4  # pull forward check only if confidence >= this
    prediction_buffer_minutes: int = 15     # schedule check N min before predicted post
    prediction_high_confidence: float = 0.65  # allow prediction to also DELAY check (not just speed up)

    # Hour-of-day
    hour_histogram_min_total: int = 10     # min posts in histogram to enable hour weighting
    active_hour_threshold: float = 0.5     # >= peak * this → active hour
    dead_hour_threshold: float = 0.15      # <= peak * this → dead hour
    active_hour_multiplier: float = 0.7    # multiply in active hours (< 1 = speed up)

    # History
    max_keep: int = 30                     # rolling window of timestamps
    max_history_days: int = 90             # discard timestamps older than this
    initial_limit: int = 30               # videos to fetch on first calibration


# ── Result ─────────────────────────────────────────────────────────────────────

@dataclass
class AdaptiveResult:
    """
    Computation result stored verbatim in MonitoredBlogger.posting_stats.
    schema_version lets future code detect stale data.
    """
    schema_version: int
    next_interval_minutes: int
    status: str              # active | burst | slow | silent | calibrating
    reason: str
    median_gap_hours: Optional[float]
    last_post_age_hours: Optional[float]
    active_hours: List[int]
    hour_histogram: Dict[str, int]   # {"14": 5, "18": 3, ...} for bot display
    post_count_analyzed: int
    computed_at: str                 # ISO 8601 UTC
    is_burst: bool
    video_timestamps: List[int] = field(default_factory=list)
    predicted_next_post_ts: Optional[int] = None   # Unix ts of predicted next post
    predicted_confidence: float = 0.0              # 0.0..1.0
    prediction_used: bool = False                  # True if prediction pulled the interval forward
    dow_histogram: Dict[str, int] = field(default_factory=dict)   # "0"=Mon…"6"=Sun
    active_days: List[int] = field(default_factory=list)          # DOW indices (0=Mon) with high posting activity
    recent_gap_hours: Optional[float] = None                      # median of last-5 gaps
    cadence_type: str = "unknown"   # clockwork | regular | sporadic | chaotic (based on gap CV)
    gap_cv: float = 0.0             # coefficient of variation of gaps (0=clockwork, >1=chaotic)
    # Trend: recent rhythm vs full history (recent_N gaps vs older gaps)
    trend_ratio: float = 1.0        # recent_median / older_median; <1=speeding up, >1=slowing down
    trend_direction: str = "stable" # stable | speeding_up | accelerating | decelerating | slowing_down


# ── Helpers ────────────────────────────────────────────────────────────────────

def _clamp(value: int, lo: int, hi: int) -> int:
    return max(lo, min(value, hi))


def _clamp_f(value: float, lo: float, hi: float) -> float:
    return max(lo, min(value, hi))


def _count_raw_posts_in_window(video_timestamps: List[int], now_ts: int, window_sec: int) -> int:
    """Count distinct valid timestamps within [now_ts - window_sec, now_ts], no clustering.

    Used for burst detection so a 10-video batch isn't collapsed to 1 by the cluster window.
    """
    cutoff = now_ts - window_sec
    seen: set[int] = set()
    count = 0
    for raw_ts in video_timestamps or []:
        try:
            ts = int(float(raw_ts))
        except (TypeError, ValueError):
            continue
        if ts < _MIN_REASONABLE_TS or ts > now_ts + _MAX_FUTURE_SKEW_SECONDS:
            continue
        if ts in seen:
            continue
        seen.add(ts)
        if ts >= cutoff:
            count += 1
    return count


def _sanitize_timestamps(
    video_timestamps: List[int],
    now_ts: Optional[int] = None,
) -> List[int]:
    ref_ts = now_ts if now_ts is not None else int(datetime.now(timezone.utc).timestamp())
    sanitized: list[int] = []
    seen: set[int] = set()
    for raw_ts in video_timestamps or []:
        try:
            ts_float = float(raw_ts)
        except (TypeError, ValueError):
            continue
        if not math.isfinite(ts_float):
            continue
        ts = int(ts_float)
        if ts < _MIN_REASONABLE_TS or ts > ref_ts + _MAX_FUTURE_SKEW_SECONDS:
            continue
        if ts in seen:
            continue
        sanitized.append(ts)
        seen.add(ts)
    if len(sanitized) < 2:
        return sanitized

    sanitized.sort(reverse=True)
    clustered: list[int] = []
    newest_kept: int | None = None
    for ts in sanitized:
        if newest_kept is not None and newest_kept - ts <= _MULTI_CLIP_CLUSTER_WINDOW_SECONDS:
            continue
        clustered.append(ts)
        newest_kept = ts
    return clustered


def _percentile(data: List[float], pct: float) -> float:
    """Simple linear-interpolation percentile (0..100), no numpy."""
    if not data:
        return 0.0
    s = sorted(data)
    idx = (len(s) - 1) * pct / 100.0
    lo_i = int(idx)
    hi_i = min(lo_i + 1, len(s) - 1)
    frac = idx - lo_i
    return s[lo_i] * (1 - frac) + s[hi_i] * frac


# ── Prediction engine ──────────────────────────────────────────────────────────

def _predict_next_post_window(
    hour_counts: Counter,
    video_timestamps: List[int],
    now_ts: int,
    median_gap_sec: float,
    active_hours: List[int],
    gaps: List[float],
    dow_counts: Optional[Counter] = None,
) -> Tuple[Optional[int], float]:
    """
    Predict the Unix timestamp of the next likely post and a confidence score (0..1).

    Strategy:
      1. Base prediction: last_post + median_gap
      2. Hour snapping: if active hours known, shift base to the nearest active
         hour at or after the base time (wraps to next day if needed)
      3. Confidence from four independent signals:
         - gap_consistency    (low CV = blogger is clockwork-regular)      × 0.40
         - hour_concentration (posts cluster in known active hours)         × 0.25
         - data_factor        (more samples = higher confidence)            × 0.15
         - dow_signal         (predicted post lands on an active DOW)       × 0.20
      4. Sanity: if the snapped time drifts more than one median gap from the base,
         fall back to the unsnapped prediction.

    Returns (predicted_ts, confidence). predicted_ts can be in the past if the
    blogger is overdue — callers check `predicted_ts > now_ts` before using.
    """
    if not video_timestamps or not gaps:
        return None, 0.0

    last_post_ts = video_timestamps[0]
    base_ts = int(last_post_ts + median_gap_sec)

    # ── Signal 1: Gap consistency (coefficient of variation, lower = better) ──
    if len(gaps) >= 3:
        try:
            stdev = statistics.stdev(gaps)
            cv = stdev / max(median_gap_sec, 1.0)
            gap_consistency = max(0.0, 1.0 - min(cv, 1.5) / 1.5)
        except statistics.StatisticsError:
            gap_consistency = 0.25
    elif len(gaps) >= 1:
        gap_consistency = 0.25
    else:
        return None, 0.0

    # ── Signal 2: Hour concentration in known active hours ────────────────────
    if active_hours and hour_counts:
        total = sum(hour_counts.values())
        active_total = sum(hour_counts.get(h, 0) for h in active_hours)
        hour_concentration = active_total / max(total, 1)
    else:
        hour_concentration = 0.25  # no histogram data → neutral

    # ── Signal 3: Data volume (saturates at 15 gaps) ──────────────────────────
    data_factor = min(len(gaps) / 15.0, 1.0)

    # ── Overall confidence (preliminary, before DOW snap) ────────────────────
    # DOW signal computed after hour-snapping so we know the predicted day.
    confidence_base = (
        gap_consistency    * 0.40
        + hour_concentration * 0.25
        + data_factor        * 0.15
    )

    # ── Hour snapping: find nearest active hour at/after base_ts ─────────────
    if active_hours:
        base_dt = datetime.fromtimestamp(base_ts, tz=timezone.utc)
        base_hour = base_dt.hour
        candidate_hours = sorted(active_hours)

        snap_hour = None
        for h in candidate_hours:
            if h >= base_hour:
                snap_hour = h
                break

        if snap_hour is None:
            snap_hour = candidate_hours[0]  # wrap to tomorrow

        if snap_hour >= base_hour:
            snapped_dt = base_dt.replace(hour=snap_hour, minute=0, second=0, microsecond=0)
        else:
            snapped_dt = (base_dt + timedelta(days=1)).replace(
                hour=snap_hour, minute=0, second=0, microsecond=0
            )

        predicted_ts = int(snapped_dt.timestamp())

        # Sanity check: don't wander more than one median gap from the base
        if abs(predicted_ts - base_ts) > median_gap_sec:
            predicted_ts = base_ts
    else:
        predicted_ts = base_ts

    # ── Signal 4: Day-of-week (does predicted day match blogger's active DOW?) ─
    # Only meaningful when we have ≥2 weeks of data in dow_counts.
    if dow_counts and sum(dow_counts.values()) >= 14:
        predicted_dow = datetime.fromtimestamp(predicted_ts, tz=timezone.utc).weekday()
        dow_total = sum(dow_counts.values())
        avg_per_day = dow_total / 7.0
        predicted_day_count = dow_counts.get(predicted_dow, 0)
        # Relative activity on the predicted day vs average; saturates at 3×
        dow_signal = _clamp_f(predicted_day_count / max(avg_per_day, 0.1) / 3.0, 0.0, 1.0)
    else:
        dow_signal = 0.25  # not enough history → neutral

    confidence = _clamp_f(confidence_base + dow_signal * 0.20, 0.0, 1.0)
    if len(gaps) < 7:
        confidence *= 0.5

    return predicted_ts, confidence


# ── Core algorithm ─────────────────────────────────────────────────────────────

def compute_adaptive_interval(
    video_timestamps: List[int],
    now_ts: int,
    settings: AdaptiveSettings,
    found_new: bool = False,
    empty_streak: int = 0,
) -> Optional[AdaptiveResult]:
    """
    Compute the adaptive check interval for a blogger.

    Args:
        video_timestamps: Unix timestamps (seconds) of posts, any order.
                          Should be non-pinned, non-None.
        now_ts:           Current wall time (Unix seconds, UTC).
        settings:         Algorithm parameters from AppSettings.
        found_new:        True if a new video was found in the last check.
        empty_streak:     Consecutive checks with no new videos.

    Returns:
        AdaptiveResult if data is sufficient (>= min_samples), else None.
    """
    ts = _sanitize_timestamps(video_timestamps, now_ts)
    if not ts or len(ts) < settings.min_samples:
        return None

    ts = sorted(ts, reverse=True)  # newest first

    # Inter-post gaps in seconds (positive values only)
    gaps = [float(ts[i] - ts[i + 1]) for i in range(len(ts) - 1) if ts[i] > ts[i + 1]]
    if not gaps:
        return None

    # --- Core statistics ---
    median_gap_sec = statistics.median(gaps)
    if median_gap_sec <= 0:
        median_gap_sec = max(gaps)

    last_post_age_sec = now_ts - ts[0]
    median_gap_hours = median_gap_sec / 3600.0
    last_post_age_hours = last_post_age_sec / 3600.0
    computed_at = datetime.fromtimestamp(now_ts, tz=timezone.utc).isoformat()

    # --- Burst detection: relative (expected daily posts) + absolute floor ---
    # Count raw (pre-cluster) posts so batch uploads (10 videos in 2 min) are each
    # counted — the cluster window would otherwise collapse them to 1 or 2.
    cutoff_24h = now_ts - 86400
    posts_last_24h = _count_raw_posts_in_window(video_timestamps, now_ts, 86400)
    expected_per_24h = 86400.0 / max(median_gap_sec, 1.0)
    relative_burst_thresh = expected_per_24h * settings.burst_relative_factor
    burst_threshold = max(float(settings.burst_threshold_24h), relative_burst_thresh)
    is_burst = posts_last_24h >= burst_threshold

    # Representative gap: P25 for burst creators, median otherwise
    rep_gap_sec = (
        _percentile(gaps, 25) if (is_burst and settings.use_p25_for_burst)
        else median_gap_sec
    )
    if rep_gap_sec <= 0:
        rep_gap_sec = median_gap_sec

    # --- Recency-blended gap ---
    # Blend full-history rep_gap with the median of the last 5 gaps.
    # When a blogger changes their cadence, this reacts faster than the full median.
    _recent_n = min(5, len(gaps))
    _recent_median_sec = statistics.median(gaps[:_recent_n])
    
    # P1: Игнорируем recent_gap_weight во время burst (batch upload),
    # чтобы короткие интервалы заливки пачки видео не сломали базовый интервал навсегда.
    actual_recent_gap_weight = 0.0 if is_burst else settings.recent_gap_weight
    
    blended_gap_sec = (
        rep_gap_sec * (1.0 - actual_recent_gap_weight)
        + _recent_median_sec * actual_recent_gap_weight
    )
    if blended_gap_sec <= 0:
        blended_gap_sec = rep_gap_sec

    # --- Volatility profile (gap CV → cadence labels and silence sensitivity) ---
    # CV = stdev/mean: 0 = clockwork, 1+ = chaotic.
    # Drives derived values:
    #   cadence_type             — shown in admin/analytics.
    #   effective_silence_mult   — clockwork → silence detected sooner (any deviation is unusual);
    #                              chaotic   → patience required (long gaps are normal)
    if len(gaps) >= 3:
        try:
            _gap_stdev = statistics.stdev(gaps)
            gap_cv = _gap_stdev / max(median_gap_sec, 1.0)
            gap_consistency = max(0.0, 1.0 - min(gap_cv, 1.5) / 1.5)
        except statistics.StatisticsError:
            gap_cv, gap_consistency = 1.0, 0.25
    else:
        gap_cv, gap_consistency = 1.0, 0.25

    if gap_cv < 0.3:
        cadence_type = "clockwork"
    elif gap_cv < 0.8:
        cadence_type = "regular"
    elif gap_cv < 1.5:
        cadence_type = "sporadic"
    else:
        cadence_type = "chaotic"

    chaotic_gap_floor_sec = 0.0
    if gap_cv >= _CHAOTIC_GAP_FLOOR_CV and len(gaps) >= settings.min_samples:
        chaotic_gap_floor_sec = _percentile(gaps, 75)
        if chaotic_gap_floor_sec > blended_gap_sec:
            rep_gap_sec = max(rep_gap_sec, chaotic_gap_floor_sec)
            blended_gap_sec = chaotic_gap_floor_sec

    # --- Trend detection: is blogger posting faster or slower recently? ---
    # Compare the last _TREND_RECENT_N gaps (most recent behaviour) to the older
    # part of the history.  gaps[] is newest-first, so gaps[:N] = most recent.
    # Requires enough data on both sides; ignores chaotic bloggers with P75 floor
    # since their gap variance already dominates.
    trend_ratio = 1.0
    trend_direction = "stable"
    _t_n = min(_TREND_RECENT_N, len(gaps) // 2)
    if len(gaps) >= _TREND_MIN_GAPS and _t_n >= 3:
        _t_recent = gaps[:_t_n]
        _t_older  = gaps[_t_n:]
        if len(_t_older) >= 3:
            try:
                _t_rm = statistics.median(_t_recent)
                _t_om = statistics.median(_t_older)
                if _t_om > 0:
                    trend_ratio = round(_t_rm / _t_om, 3)
                    if trend_ratio < 0.60:
                        trend_direction = "accelerating"   # 40 %+ faster than history
                    elif trend_ratio < 0.82:
                        trend_direction = "speeding_up"    # 18–40 % faster
                    elif trend_ratio > 1.60:
                        trend_direction = "slowing_down"   # 60 %+ slower than history
                    elif trend_ratio > 1.22:
                        trend_direction = "decelerating"   # 22–60 % slower
            except statistics.StatisticsError:
                pass

    # Keep interval math literal: admin settings like gap_divisor and silence_max_factor
    # should be easy to reason about. Cadence still affects the silence threshold below.
    try:
        effective_divisor = max(float(settings.gap_divisor), 0.001)
    except (TypeError, ValueError):
        effective_divisor = 0.001

    # effective_silence_mult: silence_multiplier × (1.5 - consistency)
    #   clockwork (consistency≈1) → × 0.5 → tighter: silence detected sooner  ✓
    #   regular   (consistency≈0.5)→ × 1.0 → normal                           ✓
    #   chaotic   (consistency≈0) → × 1.5 → looser: long gaps are normal      ✓
    try:
        silence_multiplier = max(float(settings.silence_multiplier), 0.001)
    except (TypeError, ValueError):
        silence_multiplier = 0.001
    effective_silence_mult = silence_multiplier * (1.5 - gap_consistency)

    # --- Hour-of-day histogram ---
    hour_counts: Counter = Counter(
        datetime.fromtimestamp(t, tz=timezone.utc).hour for t in ts
    )
    hour_histogram: Dict[str, int] = {str(h): c for h, c in sorted(hour_counts.items())}

    active_hours: List[int] = []
    dead_hours: List[int] = []
    total_posts_in_histogram = sum(hour_counts.values())
    if total_posts_in_histogram >= settings.hour_histogram_min_total and hour_counts:
        peak = max(hour_counts.values())
        # Iterate over the full 0..23 grid so hours with zero observed posts can
        # be flagged as dead — otherwise a blogger whose history happens to never
        # touch hour H would never see the nighttime_multiplier applied at H.
        all_hours = {h: hour_counts.get(h, 0) for h in range(24)}
        active_hours = sorted(
            h for h, c in all_hours.items()
            if c >= peak * settings.active_hour_threshold
        )
        dead_hours = sorted(
            h for h, c in all_hours.items()
            if c <= peak * settings.dead_hour_threshold
        )

    current_hour = datetime.fromtimestamp(now_ts, tz=timezone.utc).hour

    # --- Day-of-week histogram ---
    dow_counts: Counter = Counter(
        datetime.fromtimestamp(t, tz=timezone.utc).weekday() for t in ts
    )
    dow_histogram: Dict[str, int] = {str(d): c for d, c in sorted(dow_counts.items())}
    active_days: List[int] = []
    _dow_dead_days: List[int] = []
    dow_total = sum(dow_counts.values())
    # Day-of-week is a slower signal than hour-of-day: with a short or nearly
    # uniform history every day looks "active", which then pollutes the interval
    # multiplier. Enable it only when one day clearly stands out.
    if dow_total >= max(14, settings.hour_histogram_min_total) and dow_counts:
        _dow_peak = max(dow_counts.values())
        _dow_avg = dow_total / 7.0
        if _dow_peak >= max(2.0, _dow_avg * 1.5):
            _all_dows = {d: dow_counts.get(d, 0) for d in range(7)}
            active_days = sorted(
                d for d, c in _all_dows.items()
                if c >= _dow_peak * settings.active_hour_threshold
            )
            _dow_dead_days = sorted(
                d for d, c in _all_dows.items()
                if c <= _dow_peak * settings.dead_hour_threshold
            )
    current_dow = datetime.fromtimestamp(now_ts, tz=timezone.utc).weekday()

    # --- Post time prediction ---
    predicted_ts, predicted_confidence = _predict_next_post_window(
        hour_counts=hour_counts,
        video_timestamps=ts,
        now_ts=now_ts,
        median_gap_sec=median_gap_sec,
        active_hours=active_hours,
        gaps=gaps,
        dow_counts=dow_counts,
    )

    def _make_result(interval: int, status: str, reason: str) -> AdaptiveResult:
        clamped = _clamp(interval, settings.min_interval_minutes, settings.max_interval_minutes)

        # Prediction override: pull the check forward if we have a confident
        # prediction and the predicted post is coming up soon enough to matter.
        # Allowed in any status (including active) — pred_interval < clamped
        # ensures we never slow down, only catch a sooner predicted post.
        prediction_used = False
        final_interval = clamped
        if (
            predicted_ts is not None
            and predicted_ts > now_ts
            and predicted_confidence >= settings.prediction_min_confidence
        ):
            time_to_pred_sec = predicted_ts - now_ts
            time_to_pred_min = int(time_to_pred_sec / 60)
            # Use the configured buffer literally. The previous dynamic 5% buffer
            # looked clever, but made the admin setting feel random.
            pred_interval = max(
                settings.min_interval_minutes,
                time_to_pred_min - settings.prediction_buffer_minutes,
            )

            if pred_interval < clamped:
                # Prediction is sooner → speed up (always)
                final_interval = pred_interval
                prediction_used = True
            elif (
                predicted_confidence >= settings.prediction_high_confidence
                and pred_interval <= settings.max_interval_minutes
            ):
                # High confidence and prediction is further than base: let the check
                # sleep until the prediction window instead of polling repeatedly.
                # Still respect max_interval in the pure scheduler result.
                final_interval = pred_interval
                prediction_used = True

        return AdaptiveResult(
            schema_version=SCHEMA_VERSION,
            next_interval_minutes=final_interval,
            status=status,
            reason=reason,
            median_gap_hours=round(median_gap_hours, 2),
            last_post_age_hours=round(last_post_age_hours, 2),
            active_hours=active_hours,
            hour_histogram=hour_histogram,
            post_count_analyzed=len(ts),
            computed_at=computed_at,
            is_burst=is_burst,
            predicted_next_post_ts=predicted_ts,
            predicted_confidence=round(predicted_confidence, 3),
            prediction_used=prediction_used,
            dow_histogram=dow_histogram,
            active_days=active_days,
            recent_gap_hours=round(_recent_median_sec / 3600.0, 2),
            cadence_type=cadence_type,
            gap_cv=round(gap_cv, 3),
            trend_ratio=trend_ratio,
            trend_direction=trend_direction,
        )

    # ── 1. found_new override ─────────────────────────────────────────────────
    # New video found → enter active phase immediately at the base active interval.
    if found_new:
        return _make_result(
            settings.active_phase_interval_minutes,
            "active",
            f"found_new → active phase ({settings.active_phase_interval_minutes}m)",
        )

    # ── 2. Active phase (recency curve, not a step) ───────────────────────────
    active_window_sec = settings.active_phase_hours * 3600.0
    if 0 <= last_post_age_sec < active_window_sec:
        progress = last_post_age_sec / active_window_sec          # 0..1
        curve = 1.0 + 2.0 * (progress ** 1.5)                    # 1×..3× (convex)
        interval = int(settings.active_phase_interval_minutes * curve)
        return _make_result(
            interval,
            "active",
            f"active: {last_post_age_hours:.1f}h since post, ×{curve:.2f}",
        )

    # ── 2.5. Post-active ramp (smooth transition active→base) ────────────────
    # Prevents a jarring 4× jump when exiting the active window.
    post_active_end_sec = active_window_sec * 2.0
    if active_window_sec <= last_post_age_sec < post_active_end_sec:
        progress = (last_post_age_sec - active_window_sec) / active_window_sec  # 0..1
        active_exit = int(settings.active_phase_interval_minutes * 3.0)
        raw_base = _clamp(
            int(rep_gap_sec / effective_divisor / 60),
            settings.min_interval_minutes,
            settings.max_interval_minutes,
        )
        interval = int(active_exit + (raw_base - active_exit) * (progress ** 0.5))
        return _make_result(
            interval,
            "slow",
            f"post-active ramp {int(progress*100)}%: {active_exit}→{raw_base}m",
        )

    # ── 3. Burst mode (inside burst window, outside active phase) ────────────
    # Two-stage fade-out on empty_streak: at threshold, fall back from P25 to
    # median (less aggressive but still in burst); only fully exit burst once
    # streak ≥ 2× threshold, so a brief pause inside a real burst doesn't
    # collapse the speedup.
    burst_window_sec = settings.burst_duration_hours * 3600.0
    if is_burst and 0 <= last_post_age_sec < burst_window_sec:
        if empty_streak >= settings.empty_streak_threshold * 2:
            is_burst = False  # full exit → fall through to base/silence logic
        else:
            burst_gap_sec = rep_gap_sec
            fade_note = ""
            if empty_streak >= settings.empty_streak_threshold:
                # Stage 1 fade: drop the P25 speedup, keep burst_multiplier
                burst_gap_sec = median_gap_sec
                fade_note = f", fade(streak={empty_streak})"
            base = int(burst_gap_sec / effective_divisor / 60)
            interval = int(base * settings.burst_multiplier)
            gap_str = f"{int(burst_gap_sec/60)}м" if burst_gap_sec < 3600 else f"{burst_gap_sec/3600:.1f}ч"
            return _make_result(
                interval,
                "burst",
                (
                    f"burst: {posts_last_24h}/{burst_threshold:.1f} posts/24h, "
                    f"gap={gap_str} ÷{effective_divisor:g} ×{settings.burst_multiplier}{fade_note}"
                ),
            )

    # ── 4. Silence gradient (smooth, not a step to max) ──────────────────────
    # effective_silence_mult is volatility-aware: clockwork bloggers enter silence
    # sooner (any deviation is unusual), chaotic ones need more patience.
    silence_threshold_sec = effective_silence_mult * blended_gap_sec
    if last_post_age_sec >= silence_threshold_sec:
        silence_ratio = last_post_age_sec / max(blended_gap_sec, 1.0)
        factor = _clamp_f(
            silence_ratio / effective_silence_mult,
            1.0,
            settings.silence_max_factor,
        )
        raw_base = _clamp(
            int(blended_gap_sec / effective_divisor / 60),
            settings.min_interval_minutes,
            settings.max_interval_minutes,
        )
        interval = int(raw_base * factor)
        floor_note = (
            f", p75 floor={chaotic_gap_floor_sec/3600:.1f}h"
            if chaotic_gap_floor_sec > 0
            else ""
        )
        return _make_result(
            interval,
            "silent",
            (
                f"silent {last_post_age_hours:.1f}h "
                f"(ratio={silence_ratio:.1f}×, thresh={effective_silence_mult:.1f}×[{cadence_type}], "
                f"factor={factor:.2f}{floor_note} → {raw_base}…{int(raw_base * settings.silence_max_factor)}m)"
            ),
        )

    # ── 5. Base interval (volatility-adaptive divisor + recency blend) ───────
    base = int(blended_gap_sec / effective_divisor / 60)
    base = _clamp(base, settings.min_interval_minutes, settings.max_interval_minutes)

    _DOW_NAMES = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]
    _recent_note = (
        f" [recent {_recent_median_sec/3600:.1f}h]"
        if abs(_recent_median_sec - rep_gap_sec) / max(rep_gap_sec, 1) > 0.2
        else ""
    )
    if chaotic_gap_floor_sec > 0:
        _recent_note = f"{_recent_note} [p75 floor {chaotic_gap_floor_sec/3600:.1f}h]"
    reason_parts = [
        f"median {median_gap_hours:.1f}h ÷ {effective_divisor:.0f}({cadence_type}){_recent_note}"
    ]

    # ── 6. Time-of-week adjustment (hour + DOW via geometric mean) ───────────
    # Applying both multipliers independently stacks them (e.g. dead_hour × dead_day = ×4).
    # Instead combine via geometric mean so neither signal dominates unfairly.
    _hour_mult = 1.0
    _hour_label = ""
    if active_hours:
        if current_hour in dead_hours:
            _hour_mult = settings.nighttime_multiplier
            _hour_label = f"dead h{current_hour}"
        elif current_hour in active_hours:
            _hour_mult = settings.active_hour_multiplier
            _hour_label = f"active h{current_hour}"

    _dow_mult = 1.0
    _dow_label = ""
    if active_days:
        if current_dow in _dow_dead_days:
            _dow_mult = settings.nighttime_multiplier
            _dow_label = f"dead {_DOW_NAMES[current_dow]}"
        elif current_dow in active_days:
            _dow_mult = settings.active_hour_multiplier
            _dow_label = f"active {_DOW_NAMES[current_dow]}"

    if _hour_mult != 1.0 or _dow_mult != 1.0:
        if _hour_mult != 1.0 and _dow_mult != 1.0:
            combined_mult = (_hour_mult * _dow_mult) ** 0.5  # geometric mean when both signals exist
        elif _hour_mult != 1.0:
            combined_mult = _hour_mult
        else:
            combined_mult = _dow_mult
        base = _clamp(
            int(base * combined_mult),
            settings.min_interval_minutes,
            settings.max_interval_minutes,
        )
        _labels = " + ".join(l for l in [_hour_label, _dow_label] if l)
        reason_parts.append(f"{_labels} ×{combined_mult:.2f}")

    # ── 7. Trend adjustment ───────────────────────────────────────────────────
    # Applied after hour/DOW so the time-of-day signal still operates on the
    # already trend-adjusted base — trend is a slower, structural signal.
    _trend_mult = 1.0
    _trend_label = ""
    if trend_direction in ("accelerating", "speeding_up"):
        # Blogger genuinely posting faster lately → shorten interval proportionally.
        # Floor at 0.60 so we don't go sub-min due to noisy short histories.
        _trend_mult = max(0.60, trend_ratio)
        _trend_label = f"trend↑×{_trend_mult:.2f}"
    elif trend_direction in ("slowing_down", "decelerating"):
        # Blogger slowing down → modest increase, dampen signal (0.5 factor) so
        # a brief quiet spell doesn't immediately halve check frequency.
        _trend_mult = min(1.25, 1.0 + (trend_ratio - 1.0) * 0.5)
        _trend_label = f"trend↓×{_trend_mult:.2f}"
    if _trend_mult != 1.0:
        base = _clamp(int(base * _trend_mult), settings.min_interval_minutes, settings.max_interval_minutes)
        reason_parts.append(_trend_label)

    # ── 8. Empty-streak slowdown ──────────────────────────────────────────────
    if empty_streak >= settings.empty_streak_threshold:
        base = _clamp(
            int(base * settings.empty_streak_multiplier),
            settings.min_interval_minutes,
            settings.max_interval_minutes,
        )
        reason_parts.append(f"streak={empty_streak} ×{settings.empty_streak_multiplier}")

    return _make_result(base, "slow", " + ".join(reason_parts))


# ── Timestamp utilities ────────────────────────────────────────────────────────

def merge_timestamps(
    existing: List[int],
    new_timestamps: List[int],
    max_keep: int = 30,
    max_age_days: int = 90,
    now_ts: Optional[int] = None,
) -> List[int]:
    """
    Merge two timestamp lists: dedup, age-cutoff, sort desc, keep top N.

    Args:
        existing:       Previous timestamps from posting_stats.
        new_timestamps: Fresh timestamps from ScanResult.video_create_times.
        max_keep:       Rolling window size.
        max_age_days:   Drop timestamps older than this many days (0 = no cutoff).
        now_ts:         Reference "now" for age cutoff (Unix seconds).

    Returns:
        Up to max_keep timestamps, newest first, deduplicated.
    """
    ref = now_ts if now_ts is not None else int(datetime.now(timezone.utc).timestamp())
    combined = _sanitize_timestamps(existing + new_timestamps, ref)

    if max_age_days > 0:
        cutoff = ref - max_age_days * 86400
        combined = [t for t in combined if t >= cutoff]

    combined.sort(reverse=True)
    return combined[:max_keep]


def build_hour_histogram_bars(hour_histogram: Dict[str, int], width: int = 8) -> str:
    """
    Render hour_histogram as a compact bar chart string for Telegram.
    Returns empty string if no data.
    """
    if not hour_histogram:
        return ""
    counts = {int(h): c for h, c in hour_histogram.items()}
    peak = max(1, max(counts.values(), default=0))
    blocks = " ▏▎▍▌▋▊▉█"

    lines = []
    for hour in sorted(counts):
        count = counts[hour]
        filled = int(count / peak * width)
        frac = (count / peak * width) - filled
        frac_idx = int(frac * (len(blocks) - 1))
        bar = "█" * filled + (blocks[frac_idx] if filled < width else "")
        lines.append(f"  {hour:02d} {bar}")

    return "\n".join(lines)


def format_predicted_ts(predicted_ts: Optional[int], now_ts: int) -> Optional[str]:
    """
    Human-readable prediction string for bot display.
    Returns None if no prediction or prediction is in the past.
    """
    if predicted_ts is None or predicted_ts <= now_ts:
        return None
    dt = datetime.fromtimestamp(predicted_ts, tz=timezone.utc)
    delta_sec = predicted_ts - now_ts
    if delta_sec < 3600:
        delta_str = f"через {int(delta_sec / 60)} мин"
    elif delta_sec < 86400:
        delta_str = f"через {delta_sec / 3600:.1f} ч"
    else:
        delta_str = f"через {delta_sec / 86400:.1f} дн"
    time_str = dt.strftime("%d.%m %H:%M UTC")
    return f"{time_str} ({delta_str})"
