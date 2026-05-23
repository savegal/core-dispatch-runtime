"""
Phase-by-phase coverage of services.adaptive_scheduler.compute_adaptive_interval.

Pure compute module — no DB/SQLAlchemy here, just AdaptiveSettings() defaults.
Each test pins a synthetic now_ts and crafts video_timestamps so exactly
one phase fires (calibrating / active / post-active ramp / burst /
burst-fade / silence / base-slow), plus the prediction-override path.
"""
from __future__ import annotations

from datetime import datetime, timezone

import pytest

from services.adaptive_scheduler import (
    AdaptiveSettings,
    compute_adaptive_interval,
    merge_timestamps,
)


# ── Fixed reference time (avoids flakiness from datetime.now()) ───────────────
NOW_TS = int(datetime(2024, 1, 15, 12, 0, 0, tzinfo=timezone.utc).timestamp())


def _ts_from_offsets_h(offsets_h: list[float], now_ts: int = NOW_TS) -> list[int]:
    """Build timestamps as `now_ts - offset_hours * 3600` (newest first)."""
    return [int(now_ts - h * 3600) for h in offsets_h]


# ─────────────────────────────────────────────────────────────────────────────
# 1. Calibrating
# ─────────────────────────────────────────────────────────────────────────────

@pytest.mark.unit
def test_calibrating_returns_none_when_below_min_samples():
    """< min_samples (5) timestamps → None (algorithm refuses to commit)."""
    settings = AdaptiveSettings()
    ts = _ts_from_offsets_h([1.0, 2.0, 3.0, 4.0])  # only 4 samples
    result = compute_adaptive_interval(ts, NOW_TS, settings)
    assert result is None


# ─────────────────────────────────────────────────────────────────────────────
# 2. Active phase
# ─────────────────────────────────────────────────────────────────────────────

@pytest.mark.unit
def test_active_phase_returns_active_phase_interval_when_found_new():
    """found_new=True → immediate active_phase_interval (20m), status='active'."""
    settings = AdaptiveSettings()
    ts = _ts_from_offsets_h([0.5, 2.0, 3.5, 5.0, 6.5])
    result = compute_adaptive_interval(ts, NOW_TS, settings, found_new=True)
    assert result is not None
    assert result.status == "active"
    # found_new returns active_phase_interval verbatim (may be lowered by
    # prediction override; the test ensures it isn't pushed slower than 20m).
    assert result.next_interval_minutes <= settings.active_phase_interval_minutes


@pytest.mark.unit
def test_active_phase_curve_grows_with_age():
    """In active window, recency curve makes interval rise as last_post ages."""
    settings = AdaptiveSettings()
    gap_h = 1.9
    intervals: list[int] = []
    for last_post_age_h in (0.5, 1.0, 1.9):
        ts = _ts_from_offsets_h(
            [last_post_age_h + i * gap_h for i in range(5)]
        )
        result = compute_adaptive_interval(ts, NOW_TS, settings)
        assert result is not None, f"None for age {last_post_age_h}h"
        assert result.status == "active"
        intervals.append(result.next_interval_minutes)

    assert intervals[0] < intervals[1] < intervals[2], intervals


# ─────────────────────────────────────────────────────────────────────────────
# 2.5 Post-active ramp
# ─────────────────────────────────────────────────────────────────────────────

@pytest.mark.unit
def test_post_active_ramp_smooth_transition():
    """active_window ≤ last_post_age < 2×active_window → smooth ramp, status='slow'."""
    settings = AdaptiveSettings()
    # median 6h, last_post 3h ago → in [2h, 4h) ramp window, not burst (4 < 6).
    ts = _ts_from_offsets_h([3, 9, 15, 21, 27])
    result = compute_adaptive_interval(ts, NOW_TS, settings)
    assert result is not None
    assert result.status == "slow"
    active_exit = settings.active_phase_interval_minutes * 3  # 60m
    raw_base = max(settings.min_interval_minutes, int(6 * 3600 / settings.gap_divisor / 60))
    assert raw_base <= result.next_interval_minutes <= active_exit


# ─────────────────────────────────────────────────────────────────────────────
# 3. Burst (with two-stage fade-out on empty_streak)
# ─────────────────────────────────────────────────────────────────────────────

# Shared layout: 5 recent posts at 6h gaps + 7 older posts at 48h gaps.
# - posts_24h = 4   (last 4 within 24h)
# - median  = 48h
# - P25     = 6h    (drives base in burst when use_p25_for_burst=True)
# - last_post_age = 5h   (past post-active ramp end at 4h, inside 24h burst window)
# - is_burst: 4 ≥ max(absolute=3, expected_per_24h*1.5 = 0.5*1.5 = 0.75) → 4 ≥ 3 ✓
_BURST_TS_H = [5, 11, 17, 23, 29, 75, 123, 171, 219, 267, 315, 363]


@pytest.mark.unit
def test_burst_when_high_24h_count():
    """Recent burst over historic baseline → status='burst', P25-driven base."""
    settings = AdaptiveSettings()
    ts = _ts_from_offsets_h(_BURST_TS_H)
    result = compute_adaptive_interval(ts, NOW_TS, settings, empty_streak=0)
    assert result is not None
    assert result.status == "burst"
    assert result.is_burst is True
    # P25 = 6h → base = 6*3600/30/60 = 12 → ×0.5 = 6
    expected_base = int(6 * 3600 / settings.gap_divisor / 60)
    assert result.next_interval_minutes == int(expected_base * settings.burst_multiplier)


@pytest.mark.unit
def test_burst_fade_with_empty_streak():
    """streak == threshold (5) → still burst, but fall back from P25 to median."""
    settings = AdaptiveSettings()
    ts = _ts_from_offsets_h(_BURST_TS_H)

    no_fade = compute_adaptive_interval(ts, NOW_TS, settings, empty_streak=0)
    faded = compute_adaptive_interval(
        ts, NOW_TS, settings, empty_streak=settings.empty_streak_threshold
    )
    assert no_fade is not None and faded is not None
    assert faded.status == "burst"
    assert faded.is_burst is True
    # Stage-1 fade uses median (48h) instead of P25 (6h) → larger interval,
    # but burst_multiplier still applied → strictly greater than no_fade.
    assert faded.next_interval_minutes > no_fade.next_interval_minutes
    expected_base = int(48 * 3600 / settings.gap_divisor / 60)
    assert faded.next_interval_minutes == int(expected_base * settings.burst_multiplier)


@pytest.mark.unit
def test_burst_exit_with_high_streak():
    """streak ≥ 2 × threshold (10) → fully exit burst, fall through to base/slow."""
    settings = AdaptiveSettings()
    ts = _ts_from_offsets_h(_BURST_TS_H)
    result = compute_adaptive_interval(
        ts, NOW_TS, settings, empty_streak=settings.empty_streak_threshold * 2
    )
    assert result is not None
    assert result.status != "burst"
    # is_burst on the result reflects the original detection, but the chosen
    # branch is the slow base path (after burst block bails out).
    assert result.status == "slow"


# ─────────────────────────────────────────────────────────────────────────────
# 4. Silence gradient
# ─────────────────────────────────────────────────────────────────────────────

@pytest.mark.unit
def test_silence_gradient():
    """last_post_age > 4.5×median (and not silence_max-extreme) → status='silent', factor in (1, max)."""
    settings = AdaptiveSettings()
    # median 6h (gap), last_post 50h → silence threshold 27h, ratio ≈ 8.3.
    ts = _ts_from_offsets_h([50, 56, 62, 68, 74])
    result = compute_adaptive_interval(ts, NOW_TS, settings)
    assert result is not None
    assert result.status == "silent"
    base = max(settings.min_interval_minutes, int(6 * 3600 / settings.gap_divisor / 60))
    # factor between 1 (just past threshold) and silence_max_factor (capped)
    assert base <= result.next_interval_minutes <= int(base * settings.silence_max_factor) + 1


@pytest.mark.unit
def test_silence_capped_at_silence_max_factor():
    """Very old last post → factor saturates at silence_max_factor (= 2.0 default)."""
    settings = AdaptiveSettings()
    # median 6h, last_post 1000h → ratio ≈ 167; factor capped at 2.0.
    ts = _ts_from_offsets_h([1000, 1006, 1012, 1018, 1024])
    result = compute_adaptive_interval(ts, NOW_TS, settings)
    assert result is not None
    assert result.status == "silent"
    base = max(settings.min_interval_minutes, int(6 * 3600 / settings.gap_divisor / 60))
    expected = min(
        settings.max_interval_minutes,
        int(base * settings.silence_max_factor),
    )
    assert result.next_interval_minutes == expected


@pytest.mark.unit
def test_multi_clip_waves_are_treated_as_one_post_for_gap_math():
    """Several clips uploaded within minutes should not collapse weekly cadence to 5-10m."""
    settings = AdaptiveSettings()
    raw_ts: list[int] = []
    for wave_age_h in [48, 168, 336, 504, 672]:
        wave_base = int(NOW_TS - wave_age_h * 3600)
        raw_ts.extend([wave_base, wave_base - 30, wave_base - 120, wave_base - 240])

    merged = merge_timestamps([], raw_ts, max_keep=30, max_age_days=90, now_ts=NOW_TS)
    assert len(merged) == 5

    result = compute_adaptive_interval(merged, NOW_TS, settings, empty_streak=settings.empty_streak_threshold)
    assert result is not None
    assert result.post_count_analyzed == 5
    assert result.next_interval_minutes >= 120


# ─────────────────────────────────────────────────────────────────────────────
# 5. Base slow + hour-of-day adjustment
# ─────────────────────────────────────────────────────────────────────────────

# Histogram needs ≥ 10 posts and a sparse hour to flag any "dead" hour
# (the algorithm only iterates hours that actually appear in posts). So we
# stack 9 posts at peak hour 11 + 1 outlier at hour 3, all 24h-spaced.
def _peak11_with_dead3_history() -> list[int]:
    ts: list[int] = []
    # 9 posts at 11:00 UTC (2024-01-05 .. 2024-01-13)
    for day in range(5, 14):
        ts.append(int(datetime(2024, 1, day, 11, 0, 0, tzinfo=timezone.utc).timestamp()))
    # 1 outlier at 03:00 UTC on 2024-01-04 (marks hour 3 as a dead-hour data point)
    ts.append(int(datetime(2024, 1, 4, 3, 0, 0, tzinfo=timezone.utc).timestamp()))
    return ts


@pytest.mark.unit
def test_base_slow_active_hour_speedup():
    """In active hour (peak hour 11), base is multiplied by active_hour_multiplier (0.7)."""
    settings = AdaptiveSettings()
    # Last post = 2024-01-13 11:00 → 48.5h before now → past silence threshold
    # check (108h) but well past burst window (24h), so we land in base-slow.
    now_active = int(datetime(2024, 1, 15, 11, 30, 0, tzinfo=timezone.utc).timestamp())
    ts = _peak11_with_dead3_history()
    result = compute_adaptive_interval(ts, now_active, settings)
    assert result is not None
    assert result.status == "slow"
    base = int(24 * 3600 / settings.gap_divisor / 60)  # = 48
    expected = max(
        settings.min_interval_minutes,
        int(base * settings.active_hour_multiplier),
    )
    assert result.next_interval_minutes == expected


@pytest.mark.unit
def test_base_slow_dead_hour_slowdown():
    """In a flagged dead hour, base is multiplied by nighttime_multiplier (2.0)."""
    settings = AdaptiveSettings()
    now_dead = int(datetime(2024, 1, 15, 3, 0, 0, tzinfo=timezone.utc).timestamp())
    ts = _peak11_with_dead3_history()
    result = compute_adaptive_interval(ts, now_dead, settings)
    assert result is not None
    assert result.status == "slow"
    base = int(24 * 3600 / settings.gap_divisor / 60)
    expected = min(
        settings.max_interval_minutes,
        int(base * settings.nighttime_multiplier),
    )
    assert result.next_interval_minutes == expected


@pytest.mark.unit
def test_dead_hour_includes_hours_without_any_observed_posts():
    """
    Regression: dead_hours must consider the full 0..23 grid, not only hours
    that appear in posting history. A blogger that has never posted at hour H
    should still get nighttime_multiplier when checked at hour H.
    """
    settings = AdaptiveSettings()
    # 11 posts every 24h at 11:00 UTC ONLY — no outlier hour at all.
    base = datetime(2024, 1, 14, 11, 0, 0, tzinfo=timezone.utc)
    ts = [int((base.timestamp() - i * 86400)) for i in range(11)]

    # Now at 03:00 UTC — hour 3 was never posted in. Old algorithm wouldn't
    # mark it dead (it wasn't in hour_counts); new algorithm must.
    now_dead = int(datetime(2024, 1, 15, 3, 0, 0, tzinfo=timezone.utc).timestamp())
    result = compute_adaptive_interval(ts, now_dead, settings)
    assert result is not None
    assert result.status == "slow"
    base_min = int(24 * 3600 / settings.gap_divisor / 60)
    expected = min(
        settings.max_interval_minutes,
        int(base_min * settings.nighttime_multiplier),
    )
    assert result.next_interval_minutes == expected, (
        f"Hour 3 (zero observed posts) must be flagged dead and slowed by "
        f"nighttime_multiplier; got {result.next_interval_minutes}m, expected {expected}m"
    )


# ─────────────────────────────────────────────────────────────────────────────
# 6. Prediction override (now allowed in active phase)
# ─────────────────────────────────────────────────────────────────────────────

@pytest.mark.unit
def test_prediction_override_in_active_phase():
    """
    Active-curve interval is ~57m; prediction at +30m should pull check
    forward to ~15m (30 - prediction_buffer_minutes), prediction_used=True.
    """
    settings = AdaptiveSettings()
    # 8 timestamps with a uniform 144-min (2.4h) gap; last post 1.9h ago.
    last_age_s = int(1.9 * 3600)
    gap_s = int(2.4 * 3600)
    ts = [NOW_TS - (last_age_s + i * gap_s) for i in range(8)]

    result = compute_adaptive_interval(ts, NOW_TS, settings)
    assert result is not None
    assert result.status == "active"
    assert result.prediction_used is True
    # predicted_ts = last_post + median ≈ now + 30m → final ≈ 30 - 15 = 15m.
    expected = max(
        settings.min_interval_minutes,
        30 - settings.prediction_buffer_minutes,
    )
    assert result.next_interval_minutes == expected


@pytest.mark.unit
def test_prediction_no_override_when_pred_later():
    """
    found_new=True clamps interval to active_phase_interval (20m); even with
    a high-confidence prediction, if pred_interval >= clamped, no override.
    """
    settings = AdaptiveSettings()
    # 5 timestamps with uniform 1h gap; last post 10s ago → pred ≈ now+59m.
    gap_s = 3600
    ts = [NOW_TS - (10 + i * gap_s) for i in range(5)]
    result = compute_adaptive_interval(ts, NOW_TS, settings, found_new=True)
    assert result is not None
    assert result.status == "active"
    assert result.next_interval_minutes == settings.active_phase_interval_minutes
    assert result.prediction_used is False


# ─────────────────────────────────────────────────────────────────────────────
# 7. Clamp safety net
# ─────────────────────────────────────────────────────────────────────────────

@pytest.mark.unit
def test_clamp_to_min_interval_in_burst():
    """Burst with tiny P25 → base clamped up to min_interval_minutes (5)."""
    settings = AdaptiveSettings()
    # 3 recent posts at 1h gaps + sparse history → P25 ≈ 1h → base 2 → clamped to 5.
    # last_post_age = 5h → past post-active ramp end (4h), inside burst window.
    ts = _ts_from_offsets_h([5, 6, 7, 50, 75, 100])
    result = compute_adaptive_interval(ts, NOW_TS, settings)
    assert result is not None
    assert result.status == "burst"
    assert result.next_interval_minutes >= settings.min_interval_minutes


@pytest.mark.unit
def test_clamp_to_max_interval_in_silence():
    """Extreme silence (very old last post, huge median) → clamped to max (300)."""
    settings = AdaptiveSettings()
    # gap = 100h, last post 1000h ago. Raw silence interval far exceeds 300m.
    ts = _ts_from_offsets_h([1000, 1100, 1200, 1300, 1400])
    result = compute_adaptive_interval(ts, NOW_TS, settings)
    assert result is not None
    assert result.status == "silent"
    assert result.next_interval_minutes == settings.max_interval_minutes
