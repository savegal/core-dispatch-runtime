"""
Backfill correctness tests.

Critical invariant: when reconstructing posting_stats from CreatorVideo history,
prefer the real TikTok publication timestamp (meta.create_time) over our
discovered_at, because discovered_at clusters at parse moments and produces
fake-burst calibration.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from db.models import CreatorSource, CreatorVideo, MonitoredBlogger
from services.adaptive_backfill import backfill_all_bloggers, backfill_blogger
from services.adaptive_scheduler import AdaptiveSettings


def _make_blogger(db_session, username: str = "@victim") -> MonitoredBlogger:
    blogger = MonitoredBlogger(username=username, posting_stats=None)
    db_session.add(blogger)
    db_session.commit()
    db_session.refresh(blogger)
    return blogger


def _make_creator(db_session, blogger: MonitoredBlogger) -> CreatorSource:
    creator = CreatorSource(
        user_id=None,
        username=blogger.username,
        monitored_blogger_id=blogger.id,
        enabled=True,
        interval_minutes=30,
    )
    db_session.add(creator)
    db_session.commit()
    db_session.refresh(creator)
    return creator


def _make_video(
    db_session,
    creator: CreatorSource,
    *,
    video_id: str,
    discovered_at: datetime,
    create_time_ts: int | None,
) -> CreatorVideo:
    """Insert a CreatorVideo. create_time_ts goes into meta.create_time."""
    video = CreatorVideo(
        creator_id=creator.id,
        video_id=video_id,
        url=f"https://www.tiktok.com/@x/video/{video_id}",
        status="new",
        discovered_at=discovered_at,
        meta={"create_time": create_time_ts} if create_time_ts is not None else None,
    )
    db_session.add(video)
    db_session.commit()
    db_session.refresh(video)
    return video


@pytest.mark.unit
def test_backfill_uses_real_create_time_when_majority_have_it(db_session):
    """
    Given 6 videos with real create_time spread evenly over 6 days,
    discovered_at all clustered to a single backlog catch-up burst:
    backfill must NOT collapse into burst — it must use create_time and
    yield median≈24h.
    """
    blogger = _make_blogger(db_session)
    creator = _make_creator(db_session, blogger)
    settings = AdaptiveSettings()

    # All 6 videos discovered at the same moment (catch-up parse) — but their
    # real publication times are 1..6 days ago, evenly spaced.
    catch_up_moment = datetime.now(timezone.utc) - timedelta(hours=2)
    for i in range(6):
        publish_ts = int((datetime.now(timezone.utc) - timedelta(days=i + 1)).timestamp())
        _make_video(
            db_session, creator,
            video_id=f"vid{i}",
            discovered_at=catch_up_moment + timedelta(seconds=i),
            create_time_ts=publish_ts,
        )

    n = backfill_blogger(db_session, blogger, settings, days=90, force=True)
    db_session.commit()
    assert n == 6
    db_session.refresh(blogger)
    stats = blogger.posting_stats
    assert stats is not None
    # Real publication cadence: 1 post per 24h → median_gap_hours ≈ 24.
    median_h = stats.get("median_gap_hours")
    assert median_h is not None
    assert 22 <= median_h <= 26, f"expected median ~24h, got {median_h}h"
    # Should NOT be flagged as burst — they're 24h apart, not a burst.
    assert stats.get("is_burst") is False, "must not enter burst on real cadence"


@pytest.mark.unit
def test_backfill_ignores_legacy_videos_without_create_time(db_session):
    """
    A small minority of legacy videos without create_time should NOT pollute
    the calibration — when ≥50% have real create_time, those alone are used.
    """
    blogger = _make_blogger(db_session)
    creator = _make_creator(db_session, blogger)
    settings = AdaptiveSettings()

    # 6 modern videos with real create_time at 24h cadence
    for i in range(6):
        publish_ts = int((datetime.now(timezone.utc) - timedelta(days=i + 1)).timestamp())
        _make_video(
            db_session, creator,
            video_id=f"new{i}",
            discovered_at=datetime.now(timezone.utc) - timedelta(hours=1),
            create_time_ts=publish_ts,
        )
    # 2 legacy videos: discovered_at clustered, no create_time.
    legacy_disc = datetime.now(timezone.utc) - timedelta(hours=1, minutes=5)
    for i in range(2):
        _make_video(
            db_session, creator,
            video_id=f"legacy{i}",
            discovered_at=legacy_disc + timedelta(minutes=i),
            create_time_ts=None,
        )

    backfill_blogger(db_session, blogger, settings, days=90, force=True)
    db_session.commit()
    db_session.refresh(blogger)
    stats = blogger.posting_stats
    # Should not see the legacy cluster contaminating gaps; median stays ~24h.
    median_h = stats.get("median_gap_hours")
    assert 22 <= median_h <= 26, f"expected ~24h despite legacy cluster, got {median_h}h"
    assert stats.get("post_count_analyzed") == 6  # only real-create_time rows used


@pytest.mark.unit
def test_backfill_falls_back_to_discovered_at_when_no_create_time(db_session):
    """If NO videos have create_time (pure legacy), fall back to discovered_at
    so the blogger isn't stuck in calibrating forever."""
    blogger = _make_blogger(db_session)
    creator = _make_creator(db_session, blogger)
    settings = AdaptiveSettings()

    for i in range(6):
        _make_video(
            db_session, creator,
            video_id=f"old{i}",
            discovered_at=datetime.now(timezone.utc) - timedelta(days=i + 1),
            create_time_ts=None,
        )

    n = backfill_blogger(db_session, blogger, settings, days=90, force=True)
    db_session.commit()
    assert n == 6
    db_session.refresh(blogger)
    stats = blogger.posting_stats
    # Status must NOT remain calibrating — fallback should produce a real result.
    assert stats.get("status") not in (None, "calibrating")


@pytest.mark.unit
def test_backfill_no_history_clears_stale_derived_stats(db_session):
    blogger = _make_blogger(db_session, username="@empty")
    blogger.posting_stats = {
        "schema_version": 999,
        "video_timestamps": [],
        "status": "slow",
        "median_gap_hours": 24.0,
        "reason": "stale old reason",
    }
    db_session.commit()

    n = backfill_blogger(db_session, blogger, AdaptiveSettings(), days=90, force=True)
    db_session.commit()
    db_session.refresh(blogger)

    assert n == 0
    stats = blogger.posting_stats
    assert stats["status"] == "calibrating"
    assert stats["backfill_status"] == "no_history"
    assert stats["video_timestamps"] == []
    assert "median_gap_hours" not in stats
    assert "stale old reason" not in stats.get("reason", "")


@pytest.mark.unit
def test_backfill_all_counts_insufficient_data_separately(db_session):
    blogger = _make_blogger(db_session, username="@short")
    creator = _make_creator(db_session, blogger)

    for i in range(4):
        publish_ts = int((datetime.now(timezone.utc) - timedelta(days=i + 1)).timestamp())
        _make_video(
            db_session,
            creator,
            video_id=f"short{i}",
            discovered_at=datetime.now(timezone.utc) - timedelta(hours=1),
            create_time_ts=publish_ts,
        )

    result = backfill_all_bloggers(db_session, days=90, force=True)
    db_session.commit()
    db_session.refresh(blogger)

    assert result["processed"] == 1
    assert result["calibrated"] == 0
    assert result["insufficient_data"] == 1
    assert result["no_data"] == 0
    assert result["errors"] == 0
    assert blogger.posting_stats["status"] == "calibrating"
    assert blogger.posting_stats["backfill_status"] == "insufficient_data"
