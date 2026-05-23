"""
Outbox retry-backoff jitter regression tests.

When N outbox entries fail at the same moment (e.g. Telegram API outage), they
all become eligible after the same backoff and stampede the downstream service
in a tight burst. We add ±20% jitter to desynchronize the cohort.
"""
from __future__ import annotations

import statistics

import pytest

from services.video_monitor_worker import (
    MONITOR_OUTBOX_MAX_BACKOFF_SECONDS,
    MONITOR_OUTBOX_RETRY_BASE_SECONDS,
    _outbox_backoff_seconds,
)


@pytest.mark.unit
def test_backoff_within_jitter_band():
    """For a fixed attempts value, repeated calls fall in [base*0.8, base*1.2]."""
    attempts = 2
    expected_base = MONITOR_OUTBOX_RETRY_BASE_SECONDS * (2 ** attempts)
    lo = max(1, int(expected_base * 0.8))
    hi = min(MONITOR_OUTBOX_MAX_BACKOFF_SECONDS, int(expected_base * 1.2) + 1)

    samples = [_outbox_backoff_seconds(attempts) for _ in range(200)]
    for s in samples:
        assert lo <= s <= hi, f"sample {s} outside [{lo}, {hi}]"


@pytest.mark.unit
def test_backoff_actually_jitters():
    """Across many calls, results MUST vary — otherwise jitter is broken."""
    samples = [_outbox_backoff_seconds(3) for _ in range(100)]
    assert len(set(samples)) > 5, (
        "Backoff returned identical values across 100 calls — jitter not applied"
    )
    # Stdev should be material (> 5% of median) when jitter is real
    med = statistics.median(samples)
    sd = statistics.stdev(samples)
    assert sd > med * 0.03, f"jitter stdev {sd:.1f} too small vs median {med:.1f}"


@pytest.mark.unit
def test_backoff_floor_and_ceiling():
    """Floor at 1s; ceiling at MONITOR_OUTBOX_MAX_BACKOFF_SECONDS."""
    # attempts=0 → base = MONITOR_OUTBOX_RETRY_BASE_SECONDS, jittered ≥ 1
    for _ in range(50):
        v0 = _outbox_backoff_seconds(0)
        assert v0 >= 1
    # Very high attempts → must still cap at MAX
    for _ in range(50):
        v_huge = _outbox_backoff_seconds(50)
        assert v_huge <= MONITOR_OUTBOX_MAX_BACKOFF_SECONDS


@pytest.mark.unit
def test_backoff_never_negative_or_zero_for_negative_attempts():
    """Defensive: attempts<0 must not produce negative or zero seconds."""
    for attempts in (-5, -1, 0):
        for _ in range(20):
            v = _outbox_backoff_seconds(attempts)
            assert v >= 1
