from contextlib import contextmanager
from decimal import Decimal
from types import SimpleNamespace
from datetime import datetime, timezone, timedelta

import pytest

from db.models import AppSettings, Account, BloggerCheckLog, CreatorSource, CreatorVideo, MonitorOutbox, MonitoredBlogger, Proxy, TaskTemplate, User
from services.app_settings_service import (
    AUTOPILOT_CONTROL_MODE_KEY,
    AUTOPILOT_MAX_CREATE_PER_CREATOR_KEY,
    AUTOPILOT_MAX_CREATE_PER_HOUR_KEY,
    AUTOPILOT_MAX_CREATE_PER_TICK_KEY,
    SOFTWARE_MODE_KEY,
    SOFTWARE_MODE_PARSING,
    clear_cache,
)
from services.notify_service import escape_markdown
from services.task_creation_service import MonitoredTaskBatchResult
from services.video_parser import AccountStatus, PageBlocked, ScanResult, quick_detect_dead_account_for_parsing
import services.account_service as account_service_module
import services.video_monitor_worker as monitor_worker
from services.video_monitor_worker import _load_existing_video_ids, process_blogger


def _subscription(interval_minutes: int, enabled: bool = True):
    return SimpleNamespace(interval_minutes=interval_minutes, enabled=enabled)


@pytest.mark.unit
def test_single_subscription_keeps_requested_monitor_interval(monkeypatch):
    monkeypatch.setattr(monitor_worker.settings, "SHARED_BLOGGER_MIN_SUBS", 2, raising=False)
    monkeypatch.setattr(monitor_worker.settings, "SHARED_BLOGGER_MIN_INTERVAL_MINUTES", 15, raising=False)

    interval = monitor_worker._get_monitor_interval_minutes([_subscription(5)])

    assert interval == 5


@pytest.mark.unit
def test_shared_bloggers_do_not_get_unneeded_acceleration(monkeypatch):
    monkeypatch.setattr(monitor_worker.settings, "SHARED_BLOGGER_MIN_SUBS", 2, raising=False)
    monkeypatch.setattr(monitor_worker.settings, "SHARED_BLOGGER_MIN_INTERVAL_MINUTES", 15, raising=False)

    interval = monitor_worker._get_monitor_interval_minutes([
        _subscription(30),
        _subscription(30),
    ])

    assert interval == 30


@pytest.mark.unit
def test_shared_bloggers_clamp_overly_fast_requested_interval(monkeypatch):
    monkeypatch.setattr(monitor_worker.settings, "SHARED_BLOGGER_MIN_SUBS", 2, raising=False)
    monkeypatch.setattr(monitor_worker.settings, "SHARED_BLOGGER_MIN_INTERVAL_MINUTES", 15, raising=False)

    interval = monitor_worker._get_monitor_interval_minutes([
        _subscription(5),
        _subscription(30),
    ])

    assert interval == 15


@pytest.mark.unit
@pytest.mark.asyncio
async def test_tiktok_generic_error_page_is_suspicious_not_hard_dead():
    class FakePage:
        url = "https://www.tiktok.com/@demo"

        async def title(self):
            return "TikTok - Make Your Day"

        async def evaluate(self, script):
            return {
                "hasError": True,
                "errorText": "Something went wrong Sorry about that! Please try again later. Refresh",
                "hasLoginButton": False,
                "hasUserIndicator": False,
            }

    status, reason = await quick_detect_dead_account_for_parsing(FakePage(), "demo")

    assert status == AccountStatus.SUSPICIOUS
    assert "Temporary page error" in reason


@pytest.mark.unit
def test_monitor_interval_ignores_disabled_subscriptions(monkeypatch):
    monkeypatch.setattr(monitor_worker.settings, "SHARED_BLOGGER_MIN_SUBS", 2, raising=False)
    monkeypatch.setattr(monitor_worker.settings, "SHARED_BLOGGER_MIN_INTERVAL_MINUTES", 15, raising=False)

    interval = monitor_worker._get_monitor_interval_minutes([
        _subscription(5, enabled=False),
        _subscription(30),
    ])

    assert interval == 30


@pytest.mark.unit
def test_anchor_update_prefers_newer_numeric_ids():
    assert monitor_worker._should_update_blogger_anchor("200", "100") is True
    assert monitor_worker._should_update_blogger_anchor("100", "200") is False
    assert monitor_worker._should_update_blogger_anchor("200", "200") is False
    assert monitor_worker._should_update_blogger_anchor("abc", "def") is True


@pytest.mark.unit
def test_profile_access_failure_state_is_separate_from_empty_streak():
    blogger = MonitoredBlogger(
        id=77,
        username="demo_creator",
        empty_streak=12,
        posting_stats={"status": "silent", "next_interval_minutes": 180},
    )

    first = monitor_worker._record_blogger_profile_access_failure_state(
        blogger,
        "profile_not_found",
        now=datetime(2026, 5, 7, tzinfo=timezone.utc),
    )
    second = monitor_worker._record_blogger_profile_access_failure_state(
        blogger,
        "private_profile",
        now=datetime(2026, 5, 7, 0, 10, tzinfo=timezone.utc),
    )

    assert first == 1
    assert second == 2
    assert blogger.empty_streak == 12
    assert blogger.posting_stats["status"] == "silent"
    assert blogger.posting_stats["profile_access_failure"]["reason"] == "private_profile"

    monitor_worker._clear_blogger_profile_access_failure(blogger)

    assert "profile_access_failure" not in blogger.posting_stats
    assert blogger.posting_stats["next_interval_minutes"] == 180


@pytest.mark.unit
def test_adaptive_feedback_uses_planned_check_time_not_actual_check_time(monkeypatch, db_session):
    now = datetime(2026, 5, 23, 12, 0, tzinfo=timezone.utc)
    planned_check = now - timedelta(minutes=20)
    publish_ts = int((planned_check - timedelta(minutes=5)).timestamp())
    older_ts = publish_ts - 24 * 3600
    blogger = MonitoredBlogger(
        username="planned_feedback_demo",
        next_check_at=planned_check,
        posting_stats={
            "next_interval_minutes": 15,
            "video_timestamps": [older_ts],
        },
    )
    db_session.add(blogger)
    db_session.flush()

    adaptive_settings = monitor_worker.AdaptiveSettings(min_samples=2)
    monkeypatch.setattr(
        monitor_worker,
        "_load_adaptive_runtime_settings",
        lambda db: (
            True,
            adaptive_settings,
            {
                "feedback_enabled": True,
                "feedback_ema_alpha": 1.0,
                "feedback_target_latency_min": 10,
                "feedback_late_threshold": 2.0,
            },
        ),
    )
    scan_result = ScanResult(
        status="OK",
        new_videos=[("500", "https://www.tiktok.com/@planned_feedback_demo/video/500")],
        newest_normal_video_id="500",
        anchor_found=True,
        canonical_source="API",
        pages_loaded=1,
        diagnostics={},
        video_create_times=[publish_ts, older_ts],
        video_create_times_map={"500": publish_ts},
    )

    monitor_worker._schedule_next_blogger_check_from_scan(
        db_session,
        blogger,
        active_subscriptions=[_subscription(15)],
        now=now,
        scan_result=scan_result,
        found_new_count=1,
        status="OK",
        videos=scan_result.new_videos,
    )
    db_session.flush()

    check_log = db_session.query(BloggerCheckLog).filter_by(blogger_id=blogger.id).one()
    assert int(blogger.latency_ema_minutes) == 5
    assert blogger.last_detection_latency_min == 5
    assert check_log.detection_latency_min == 5
    assert blogger.posting_stats["last_batch_latency"]["p75"] == 5
    assert blogger.posting_stats["last_batch_latency"]["basis"] == "planned_check_at"
    assert blogger.posting_stats["last_observed_batch_latency"]["p75"] == 25


@pytest.mark.unit
def test_adaptive_feedback_skips_videos_published_after_planned_check(monkeypatch, db_session):
    now = datetime(2026, 5, 23, 12, 0, tzinfo=timezone.utc)
    planned_check = now - timedelta(minutes=20)
    publish_ts = int((now - timedelta(minutes=10)).timestamp())
    older_ts = publish_ts - 24 * 3600
    blogger = MonitoredBlogger(
        username="after_planned_feedback_demo",
        next_check_at=planned_check,
        posting_stats={
            "next_interval_minutes": 15,
            "video_timestamps": [older_ts],
        },
    )
    db_session.add(blogger)
    db_session.flush()

    adaptive_settings = monitor_worker.AdaptiveSettings(min_samples=2)
    monkeypatch.setattr(
        monitor_worker,
        "_load_adaptive_runtime_settings",
        lambda db: (
            True,
            adaptive_settings,
            {
                "feedback_enabled": True,
                "feedback_ema_alpha": 1.0,
                "feedback_target_latency_min": 10,
                "feedback_late_threshold": 2.0,
            },
        ),
    )
    scan_result = ScanResult(
        status="OK",
        new_videos=[("501", "https://www.tiktok.com/@after_planned_feedback_demo/video/501")],
        newest_normal_video_id="501",
        anchor_found=True,
        canonical_source="API",
        pages_loaded=1,
        diagnostics={},
        video_create_times=[publish_ts, older_ts],
        video_create_times_map={"501": publish_ts},
    )

    monitor_worker._schedule_next_blogger_check_from_scan(
        db_session,
        blogger,
        active_subscriptions=[_subscription(15)],
        now=now,
        scan_result=scan_result,
        found_new_count=1,
        status="OK",
        videos=scan_result.new_videos,
    )
    db_session.flush()

    check_log = db_session.query(BloggerCheckLog).filter_by(blogger_id=blogger.id).one()
    assert blogger.latency_ema_minutes is None
    assert blogger.last_detection_latency_min is None
    assert check_log.detection_latency_min is None
    assert "last_batch_latency" not in blogger.posting_stats
    assert blogger.posting_stats["last_observed_batch_latency"]["p75"] == 10
    assert "videos published after planned check" in blogger.posting_stats["reason"]


@pytest.mark.unit
def test_adaptive_feedback_skips_checks_run_before_planned_time(monkeypatch, db_session):
    now = datetime(2026, 5, 23, 12, 0, tzinfo=timezone.utc)
    planned_check = now + timedelta(minutes=30)
    publish_ts = int((now - timedelta(minutes=5)).timestamp())
    older_ts = publish_ts - 24 * 3600
    blogger = MonitoredBlogger(
        username="early_run_feedback_demo",
        next_check_at=planned_check,
        posting_stats={
            "next_interval_minutes": 15,
            "video_timestamps": [older_ts],
        },
    )
    db_session.add(blogger)
    db_session.flush()

    adaptive_settings = monitor_worker.AdaptiveSettings(min_samples=2)
    monkeypatch.setattr(
        monitor_worker,
        "_load_adaptive_runtime_settings",
        lambda db: (
            True,
            adaptive_settings,
            {
                "feedback_enabled": True,
                "feedback_ema_alpha": 1.0,
                "feedback_target_latency_min": 10,
                "feedback_late_threshold": 2.0,
            },
        ),
    )
    scan_result = ScanResult(
        status="OK",
        new_videos=[("502", "https://www.tiktok.com/@early_run_feedback_demo/video/502")],
        newest_normal_video_id="502",
        anchor_found=True,
        canonical_source="API",
        pages_loaded=1,
        diagnostics={},
        video_create_times=[publish_ts, older_ts],
        video_create_times_map={"502": publish_ts},
    )

    monitor_worker._schedule_next_blogger_check_from_scan(
        db_session,
        blogger,
        active_subscriptions=[_subscription(15)],
        now=now,
        scan_result=scan_result,
        found_new_count=1,
        status="OK",
        videos=scan_result.new_videos,
    )
    db_session.flush()

    check_log = db_session.query(BloggerCheckLog).filter_by(blogger_id=blogger.id).one()
    assert blogger.latency_ema_minutes is None
    assert blogger.last_detection_latency_min is None
    assert check_log.detection_latency_min is None
    assert "last_batch_latency" not in blogger.posting_stats
    assert blogger.posting_stats["last_observed_batch_latency"]["p75"] == 5
    assert "checked before planned time" in blogger.posting_stats["reason"]


@pytest.mark.unit
def test_monitor_pool_alert_ignores_small_dead_session_ratio(monkeypatch):
    monkeypatch.setattr(monitor_worker.settings, "MONITOR_ACCOUNT_LOW_THRESHOLD", 8, raising=False)
    monkeypatch.setattr(monitor_worker.settings, "MONITOR_ACCOUNT_CRITICAL_THRESHOLD", 3, raising=False)
    monkeypatch.setattr(monitor_worker.settings, "MONITOR_ACCOUNT_MIN_ELIGIBLE_PER_DUE", 2.0, raising=False)
    monkeypatch.setattr(monitor_worker.settings, "MONITOR_ACCOUNT_MIN_ELIGIBLE_RATIO", 0.35, raising=False)
    monkeypatch.setattr(monitor_worker.settings, "MONITOR_DEAD_SESSION_ALERT_THRESHOLD", 5, raising=False)
    monkeypatch.setattr(monitor_worker.settings, "MONITOR_DEAD_SESSION_ALERT_RATIO", 0.25, raising=False)
    monkeypatch.setattr(monitor_worker.settings, "MONITOR_OK_WINDOW_MIN_RATIO", 0.35, raising=False)

    pressure = monitor_worker._evaluate_monitor_pool_pressure(
        {
            "bloggers": 38,
            "due": 5,
            "accounts_eligible": 56,
            "accounts_total": 79,
            "accounts_reserved": 2,
            "dead_session": 6,
            "bad_proxies": 0,
            "ok_last_window": 14,
            "oldest_overdue_minutes": 0,
        }
    )

    assert pressure is None


@pytest.mark.unit
def test_monitor_pool_alert_keeps_low_eligible_warning(monkeypatch):
    monkeypatch.setattr(monitor_worker.settings, "MONITOR_ACCOUNT_LOW_THRESHOLD", 8, raising=False)
    monkeypatch.setattr(monitor_worker.settings, "MONITOR_ACCOUNT_CRITICAL_THRESHOLD", 3, raising=False)
    monkeypatch.setattr(monitor_worker.settings, "MONITOR_ACCOUNT_MIN_ELIGIBLE_PER_DUE", 2.0, raising=False)
    monkeypatch.setattr(monitor_worker.settings, "MONITOR_ACCOUNT_MIN_ELIGIBLE_RATIO", 0.35, raising=False)
    monkeypatch.setattr(monitor_worker.settings, "MONITOR_DEAD_SESSION_ALERT_THRESHOLD", 5, raising=False)
    monkeypatch.setattr(monitor_worker.settings, "MONITOR_DEAD_SESSION_ALERT_RATIO", 0.25, raising=False)
    monkeypatch.setattr(monitor_worker.settings, "MONITOR_OK_WINDOW_MIN_RATIO", 0.35, raising=False)

    pressure = monitor_worker._evaluate_monitor_pool_pressure(
        {
            "bloggers": 38,
            "due": 8,
            "accounts_eligible": 6,
            "accounts_total": 79,
            "accounts_reserved": 2,
            "dead_session": 6,
            "bad_proxies": 0,
            "ok_last_window": 14,
            "oldest_overdue_minutes": 0,
        }
    )

    assert pressure is not None
    assert pressure["severity"] == "warning"
    assert pressure["suggest_add_accounts"] is True


@pytest.mark.unit
def test_monitor_pool_backlog_attention_does_not_request_more_accounts(monkeypatch):
    monkeypatch.setattr(monitor_worker.settings, "MONITOR_MAX_BLOGGERS_PER_TICK", 5, raising=False)
    monkeypatch.setattr(monitor_worker.settings, "MONITOR_ACCOUNT_LOW_THRESHOLD", 8, raising=False)
    monkeypatch.setattr(monitor_worker.settings, "MONITOR_ACCOUNT_CRITICAL_THRESHOLD", 3, raising=False)
    monkeypatch.setattr(monitor_worker.settings, "MONITOR_ACCOUNT_MIN_ELIGIBLE_PER_DUE", 2.0, raising=False)
    monkeypatch.setattr(monitor_worker.settings, "MONITOR_ACCOUNT_MIN_ELIGIBLE_RATIO", 0.35, raising=False)
    monkeypatch.setattr(monitor_worker.settings, "MONITOR_DEAD_SESSION_ALERT_THRESHOLD", 5, raising=False)
    monkeypatch.setattr(monitor_worker.settings, "MONITOR_DEAD_SESSION_ALERT_RATIO", 0.25, raising=False)
    monkeypatch.setattr(monitor_worker.settings, "MONITOR_OK_WINDOW_MIN_RATIO", 0.35, raising=False)
    monkeypatch.setattr(monitor_worker.settings, "MONITOR_BACKLOG_ATTENTION_OVERDUE_MINUTES", 30, raising=False)

    snapshot = {
        "bloggers": 38,
        "due": 36,
        "accounts_eligible": 56,
        "accounts_total": 79,
        "accounts_reserved": 1,
        "dead_session": 6,
        "bad_proxies": 0,
        "ok_last_window": 0,
        "oldest_overdue_minutes": 122,
        "window_minutes": 15,
    }

    pressure = monitor_worker._evaluate_monitor_pool_pressure(snapshot)
    assert pressure is not None
    assert pressure["attention_kind"] == "backlog"
    assert pressure["suggest_add_accounts"] is False
    assert pressure["recommended"] == 14

    message = monitor_worker._format_monitor_pool_alert(snapshot, pressure)
    assert "добавить минимум" not in message
    assert "аккаунты пока докупать не нужно" in message


@pytest.mark.unit
def test_local_browser_driver_error_is_not_treated_as_network():
    error_text = "BrowserType.launch: Connection closed while reading from the driver"

    assert monitor_worker._looks_like_local_browser_infrastructure_error(error_text) is True
    assert monitor_worker._looks_like_network_error(error_text) is False


@pytest.mark.unit
def test_timeout_event_tracks_streak_and_recent_window(monkeypatch):
    monitor_worker._blogger_timeout_streak.clear()
    monitor_worker._timeout_events.clear()
    monkeypatch.setattr(monitor_worker.settings, "MONITOR_TIMEOUT_WINDOW_MINUTES", 15, raising=False)
    now = monitor_worker.datetime(2026, 5, 1, 12, 0, tzinfo=monitor_worker.timezone.utc)

    first = monitor_worker._record_blogger_timeout_event(1, "alpha", now=now)
    second = monitor_worker._record_blogger_timeout_event(1, "alpha", now=now + monitor_worker.timedelta(minutes=1))
    third = monitor_worker._record_blogger_timeout_event(2, "beta", now=now + monitor_worker.timedelta(minutes=2))

    assert first["streak"] == 1
    assert second["streak"] == 2
    assert third["streak"] == 1
    assert third["window_count"] == 3
    assert third["recent_usernames"] == ["alpha", "beta"]

    monitor_worker._reset_blogger_timeout_streak(1)
    after_reset = monitor_worker._record_blogger_timeout_event(1, "alpha", now=now + monitor_worker.timedelta(minutes=3))

    assert after_reset["streak"] == 1


@pytest.mark.unit
@pytest.mark.asyncio
async def test_timeout_alert_is_suppressed_until_repeat_or_window(monkeypatch):
    captured = []

    async def fake_send_admin_alert_once(key, message, cooldown_minutes=None):
        captured.append((key, message, cooldown_minutes))

    monkeypatch.setattr(monitor_worker, "_send_admin_alert_once", fake_send_admin_alert_once)
    monkeypatch.setattr(monitor_worker.settings, "MONITOR_TIMEOUT_ALERT_THRESHOLD", 2, raising=False)
    monkeypatch.setattr(monitor_worker.settings, "MONITOR_TIMEOUT_WINDOW_ALERT_THRESHOLD", 3, raising=False)

    blogger = SimpleNamespace(id=10, username="alpha")

    await monitor_worker._maybe_send_blogger_timeout_alert(
        blogger,
        timeout_sec=180,
        timeout_stats={"streak": 1, "window_count": 1, "window_minutes": 15, "recent_usernames": ["alpha"]},
    )
    assert captured == []

    await monitor_worker._maybe_send_blogger_timeout_alert(
        blogger,
        timeout_sec=180,
        timeout_stats={"streak": 2, "window_count": 2, "window_minutes": 15, "recent_usernames": ["alpha"]},
    )
    assert captured[-1][0] == "blogger_timeout:10"

    await monitor_worker._maybe_send_blogger_timeout_alert(
        blogger,
        timeout_sec=180,
        timeout_stats={"streak": 1, "window_count": 3, "window_minutes": 15, "recent_usernames": ["alpha", "beta"]},
    )
    assert captured[-1][0] == "blogger_timeout_window"


@pytest.mark.unit
def test_proxy_health_fail_open_skips_repeated_redis_errors(monkeypatch):
    import services.proxy_health_service as proxy_health

    calls = {"count": 0}

    class BrokenRedis:
        def exists(self, key):
            calls["count"] += 1
            raise TimeoutError("redis timeout")

    monkeypatch.setattr(proxy_health, "_proxy_health_unavailable_until", 0.0)
    monkeypatch.setattr(proxy_health, "_proxy_health_client", BrokenRedis())

    assert proxy_health.is_proxy_bad("http://proxy.example:8080") is False
    assert proxy_health.is_proxy_bad("http://proxy.example:8080") is False
    assert calls["count"] == 1


@pytest.mark.unit
def test_clear_proxy_bad_resets_backoff_count(monkeypatch):
    import services.proxy_health_service as proxy_health

    class FakeRedis:
        def __init__(self):
            self.deleted = []

        def delete(self, *keys):
            self.deleted.extend(keys)
            return len(keys)

    fake_redis = FakeRedis()
    proxy = "http://proxy.example:8080"

    monkeypatch.setattr(proxy_health, "_proxy_health_unavailable_until", 0.0)
    monkeypatch.setattr(proxy_health, "_proxy_health_client", fake_redis)

    proxy_health.clear_proxy_bad(proxy)

    assert proxy_health._key_bad(proxy) in fake_redis.deleted
    assert proxy_health._key_count(proxy) in fake_redis.deleted


@pytest.mark.unit
def test_escape_markdown_escapes_user_text_safely():
    text = r"user_name[demo](x)\path*`tick`"

    escaped = escape_markdown(text)

    assert escaped == r"user\_name\[demo\]\(x\)\\path\*\`tick\`"


@pytest.mark.unit
def test_load_existing_video_ids_returns_only_matching_ids(db_session):
    user = User(
        telegram_id=555001,
        username="monitor_user",
        role="user",
    )
    blogger = MonitoredBlogger(username="creator_demo")
    creator = CreatorSource(
        user=user,
        username="creator_demo",
        monitored_blogger=blogger,
        enabled=True,
        interval_minutes=30,
        mode="manual_review",
    )
    db_session.add_all([user, blogger, creator])
    db_session.flush()

    db_session.add_all(
        [
            CreatorVideo(creator_id=creator.id, video_id="v1", url="https://tiktok.com/1"),
            CreatorVideo(creator_id=creator.id, video_id="v3", url="https://tiktok.com/3"),
        ]
    )
    db_session.commit()

    existing_ids = _load_existing_video_ids(db_session, creator.id, ["v1", "v2", "v3"])

    assert existing_ids == {"v1", "v3"}


@pytest.mark.unit
def test_load_existing_video_ids_returns_empty_set_for_empty_input(db_session):
    user = User(
        telegram_id=555002,
        username="monitor_user_2",
        role="user",
    )
    blogger = MonitoredBlogger(username="creator_demo_2")
    creator = CreatorSource(
        user=user,
        username="creator_demo_2",
        monitored_blogger=blogger,
        enabled=True,
        interval_minutes=15,
        mode="notify_only",
    )
    db_session.add_all([user, blogger, creator])
    db_session.commit()

    assert _load_existing_video_ids(db_session, creator.id, []) == set()


@pytest.mark.unit
def test_disabled_notifications_do_not_enqueue_notify_events(db_session):
    user = User(
        telegram_id=555102,
        username="muted_user",
        role="user",
        is_blocked=False,
        video_notifications_enabled=False,
    )
    blogger = MonitoredBlogger(username="muted_demo")
    creator = CreatorSource(
        user=user,
        username="muted_demo",
        monitored_blogger=blogger,
        enabled=True,
        interval_minutes=15,
        mode="notify_only",
    )
    db_session.add_all([user, blogger, creator])
    db_session.flush()
    video = CreatorVideo(
        creator_id=creator.id,
        video_id="v-muted",
        url="https://www.tiktok.com/@muted_demo/video/v-muted",
        status="new",
    )
    db_session.add(video)
    db_session.flush()

    monitor_worker._enqueue_subscription_outbox_events(
        db_session,
        blogger_id=blogger.id,
        batches=[(creator, [video])],
    )

    assert db_session.query(CreatorVideo).filter(CreatorVideo.creator_id == creator.id).count() == 1
    assert db_session.query(MonitorOutbox).filter(MonitorOutbox.creator_id == creator.id).count() == 0


@pytest.mark.unit
def test_disabled_notifications_keep_autopilot_enqueue(db_session):
    user = User(
        telegram_id=555103,
        username="muted_auto_user",
        role="user",
        is_blocked=False,
        video_notifications_enabled=False,
    )
    blogger = MonitoredBlogger(username="muted_auto_demo")
    creator = CreatorSource(
        user=user,
        username="muted_auto_demo",
        monitored_blogger=blogger,
        enabled=True,
        interval_minutes=15,
        mode="auto_comment",
    )
    db_session.add_all([user, blogger, creator])
    db_session.flush()
    video = CreatorVideo(
        creator_id=creator.id,
        video_id="v-auto-muted",
        url="https://www.tiktok.com/@muted_auto_demo/video/v-auto-muted",
        status="new",
    )
    db_session.add(video)
    db_session.flush()

    monitor_worker._enqueue_subscription_outbox_events(
        db_session,
        blogger_id=blogger.id,
        batches=[(creator, [video])],
    )

    outbox = db_session.query(MonitorOutbox).filter(MonitorOutbox.creator_id == creator.id).one()
    assert outbox.event_type == "autopilot_create"


@pytest.mark.unit
def test_full_access_autopilot_is_not_paused_by_parsing_mode(db_session):
    clear_cache()
    user = User(
        telegram_id=555107,
        username="full_access_auto_user",
        role="user",
        is_blocked=False,
        full_access_override=True,
    )
    blogger = MonitoredBlogger(username="full_access_auto_demo")
    creator = CreatorSource(
        user=user,
        username="full_access_auto_demo",
        monitored_blogger=blogger,
        enabled=True,
        interval_minutes=15,
        mode="auto_comment",
    )
    db_session.add_all([
        AppSettings(key=SOFTWARE_MODE_KEY, value=SOFTWARE_MODE_PARSING),
        user,
        blogger,
        creator,
    ])
    db_session.flush()
    video = CreatorVideo(
        creator_id=creator.id,
        video_id="v-full-access-auto",
        url="https://www.tiktok.com/@full_access_auto_demo/video/v-full-access-auto",
        status="new",
    )
    db_session.add(video)
    db_session.flush()

    try:
        monitor_worker._enqueue_subscription_outbox_events(
            db_session,
            blogger_id=blogger.id,
            batches=[(creator, [video])],
        )
    finally:
        clear_cache()

    outbox = db_session.query(MonitorOutbox).filter(MonitorOutbox.creator_id == creator.id).one()
    assert outbox.event_type == "autopilot_create"


@pytest.mark.unit
@pytest.mark.asyncio
async def test_full_access_manual_review_keeps_order_notification_in_parsing_mode(monkeypatch, db_session):
    clear_cache()
    user = User(
        telegram_id=555108,
        username="full_access_review_user",
        role="user",
        is_blocked=False,
        full_access_override=True,
    )
    blogger = MonitoredBlogger(username="full_access_review_demo")
    creator = CreatorSource(
        user=user,
        username="full_access_review_demo",
        monitored_blogger=blogger,
        enabled=True,
        interval_minutes=15,
        mode="manual_review",
    )
    db_session.add_all([
        AppSettings(key=SOFTWARE_MODE_KEY, value=SOFTWARE_MODE_PARSING),
        user,
        blogger,
        creator,
    ])
    db_session.flush()
    video = CreatorVideo(
        creator_id=creator.id,
        video_id="v-full-access-review",
        url="https://www.tiktok.com/@full_access_review_demo/video/v-full-access-review",
        status="new",
    )
    db_session.add(video)
    db_session.flush()

    monitor_worker._enqueue_subscription_outbox_events(
        db_session,
        blogger_id=blogger.id,
        batches=[(creator, [video])],
    )
    outbox = db_session.query(MonitorOutbox).filter(MonitorOutbox.creator_id == creator.id).one()
    assert outbox.event_type == "notify_pending_review"

    called: dict[str, bool] = {"pending": False, "new": False}

    @contextmanager
    def fake_db_context():
        yield db_session

    async def fake_notify_pending_review(*args, **kwargs):
        called["pending"] = True
        return True

    async def fake_notify_new_videos(*args, **kwargs):
        called["new"] = True
        return True

    monkeypatch.setattr(monitor_worker, "get_sync_db_context", fake_db_context)
    monkeypatch.setattr(monitor_worker, "notify_pending_review", fake_notify_pending_review)
    monkeypatch.setattr(monitor_worker, "notify_new_videos", fake_notify_new_videos)

    try:
        assert await monitor_worker._process_outbox_entry(outbox.id) is True
    finally:
        clear_cache()

    assert called == {"pending": True, "new": False}


@pytest.mark.unit
@pytest.mark.asyncio
async def test_stale_review_outbox_reroutes_to_autopilot_when_creator_is_now_auto(monkeypatch, db_session):
    clear_cache()
    user = User(
        telegram_id=555109,
        username="reroute_auto_user",
        role="user",
        is_blocked=False,
        video_notifications_enabled=True,
    )
    blogger = MonitoredBlogger(username="reroute_auto_demo")
    template = TaskTemplate(
        user=user,
        id=124,
        name="auto-template-reroute",
        main_comment="Main",
        settings={"text_variants": ["Main", "Second", "Third"]},
    )
    creator = CreatorSource(
        user=user,
        username="reroute_auto_demo",
        monitored_blogger=blogger,
        enabled=True,
        interval_minutes=15,
        mode="auto_comment",
        default_template_id=124,
    )
    db_session.add_all([user, blogger, template, creator])
    db_session.flush()
    video = CreatorVideo(
        creator_id=creator.id,
        video_id="v-reroute-auto",
        url="https://www.tiktok.com/@reroute_auto_demo/video/v-reroute-auto",
        status="new",
    )
    db_session.add(video)
    db_session.flush()
    outbox = MonitorOutbox(
        event_type="notify_pending_review",
        status="pending",
        blogger_id=blogger.id,
        creator_id=creator.id,
        payload={"creator_id": creator.id, "video_ids": [video.id]},
        dedupe_key="stale-review-reroute",
    )
    db_session.add(outbox)
    db_session.commit()

    @contextmanager
    def fake_db_context():
        yield db_session

    async def fail_if_not_rerouted(*args, **kwargs):
        raise AssertionError("stale review notification should be rerouted to autopilot")

    monkeypatch.setattr(monitor_worker, "get_sync_db_context", fake_db_context)
    monkeypatch.setattr(monitor_worker, "notify_pending_review", fail_if_not_rerouted)

    try:
        assert await monitor_worker._process_outbox_entry(outbox.id) is True
    finally:
        clear_cache()

    db_session.refresh(outbox)
    assert outbox.status == "done"
    autopilot = (
        db_session.query(MonitorOutbox)
        .filter(
            MonitorOutbox.creator_id == creator.id,
            MonitorOutbox.event_type == "autopilot_create",
        )
        .one()
    )
    assert autopilot.status == "pending"
    assert autopilot.payload["video_ids"] == [video.id]


@pytest.mark.unit
@pytest.mark.asyncio
async def test_stale_review_outbox_without_auto_template_still_notifies(monkeypatch, db_session):
    clear_cache()
    user = User(
        telegram_id=555110,
        username="auto_without_template_user",
        role="user",
        is_blocked=False,
        video_notifications_enabled=True,
    )
    blogger = MonitoredBlogger(username="auto_without_template_demo")
    creator = CreatorSource(
        user=user,
        username="auto_without_template_demo",
        monitored_blogger=blogger,
        enabled=True,
        interval_minutes=15,
        mode="auto_comment",
        default_template_id=None,
    )
    db_session.add_all([user, blogger, creator])
    db_session.flush()
    video = CreatorVideo(
        creator_id=creator.id,
        video_id="v-auto-without-template",
        url="https://www.tiktok.com/@auto_without_template_demo/video/v-auto-without-template",
        status="new",
    )
    db_session.add(video)
    db_session.flush()
    outbox = MonitorOutbox(
        event_type="notify_pending_review",
        status="pending",
        blogger_id=blogger.id,
        creator_id=creator.id,
        payload={"creator_id": creator.id, "video_ids": [video.id]},
        dedupe_key="stale-review-no-template",
    )
    db_session.add(outbox)
    db_session.commit()

    called = {"pending": False}

    @contextmanager
    def fake_db_context():
        yield db_session

    async def fake_notify_pending_review(*args, **kwargs):
        called["pending"] = True
        return True

    monkeypatch.setattr(monitor_worker, "get_sync_db_context", fake_db_context)
    monkeypatch.setattr(monitor_worker, "notify_pending_review", fake_notify_pending_review)

    try:
        assert await monitor_worker._process_outbox_entry(outbox.id) is True
    finally:
        clear_cache()

    assert called["pending"] is True
    assert (
        db_session.query(MonitorOutbox)
        .filter(
            MonitorOutbox.creator_id == creator.id,
            MonitorOutbox.event_type == "autopilot_create",
        )
        .count()
        == 0
    )


@pytest.mark.unit
def test_autopilot_paused_does_not_enqueue_new_create_outbox(db_session):
    clear_cache()
    user = User(
        telegram_id=555120,
        username="paused_autopilot_user",
        role="user",
        is_blocked=False,
        video_notifications_enabled=True,
    )
    blogger = MonitoredBlogger(username="paused_autopilot_demo")
    creator = CreatorSource(
        user=user,
        username="paused_autopilot_demo",
        monitored_blogger=blogger,
        enabled=True,
        interval_minutes=15,
        mode="auto_comment",
        default_template_id=1,
    )
    db_session.add_all([user, blogger, creator])
    db_session.flush()
    video = CreatorVideo(
        creator_id=creator.id,
        video_id="v-paused-auto",
        url="https://www.tiktok.com/@paused_autopilot_demo/video/v-paused-auto",
        status="new",
    )
    db_session.add_all(
        [
            video,
            AppSettings(key=AUTOPILOT_CONTROL_MODE_KEY, value="paused"),
        ]
    )
    db_session.commit()

    try:
        monitor_worker._enqueue_subscription_outbox_events(
            db_session,
            blogger_id=blogger.id,
            batches=[(creator, [video])],
        )
    finally:
        clear_cache()

    assert db_session.query(MonitorOutbox).count() == 0


@pytest.mark.unit
def test_autopilot_worker_reads_control_settings_without_cache_delay(db_session):
    clear_cache()
    setting = AppSettings(key=AUTOPILOT_CONTROL_MODE_KEY, value="active")
    limit = AppSettings(key=AUTOPILOT_MAX_CREATE_PER_TICK_KEY, value="20")
    db_session.add_all([setting, limit])
    db_session.commit()

    assert monitor_worker._get_autopilot_mode_safe(db_session) == "active"
    assert monitor_worker._get_autopilot_limits_safe(db_session)["max_create_per_tick"] == 20

    setting.value = "paused"
    limit.value = "5"
    db_session.commit()

    try:
        assert monitor_worker._get_autopilot_mode_safe(db_session) == "paused"
        assert monitor_worker._get_autopilot_limits_safe(db_session)["max_create_per_tick"] == 5
    finally:
        clear_cache()


@pytest.mark.unit
def test_autopilot_paused_claims_notifications_but_not_create_outbox(db_session):
    clear_cache()
    creator = CreatorSource(username="paused_claim_creator", enabled=True, mode="auto_comment")
    db_session.add(creator)
    db_session.flush()
    autopilot_entry = MonitorOutbox(
        event_type="autopilot_create",
        creator_id=creator.id,
        status="pending",
        payload={"creator_id": creator.id, "video_ids": [1]},
        dedupe_key="paused-claim-auto",
    )
    notify_entry = MonitorOutbox(
        event_type="notify_auto_orders_created",
        creator_id=creator.id,
        status="pending",
        payload={"creator_id": creator.id, "video_ids": [1], "task_ids": [2]},
        dedupe_key="paused-claim-notify",
    )
    db_session.add_all(
        [
            autopilot_entry,
            notify_entry,
            AppSettings(key=AUTOPILOT_CONTROL_MODE_KEY, value="paused"),
        ]
    )
    db_session.commit()

    try:
        claimed = monitor_worker._claim_outbox_batch(db_session, limit=10, lock_owner="test-worker")
    finally:
        clear_cache()

    assert [entry.id for entry in claimed] == [notify_entry.id]
    db_session.refresh(autopilot_entry)
    db_session.refresh(notify_entry)
    assert autopilot_entry.status == "pending"
    assert notify_entry.status == "processing"


@pytest.mark.unit
def test_commenting_active_claims_notifications_but_defers_autopilot_outbox(db_session):
    creator = CreatorSource(username="commenting_active_claim_creator", enabled=True, mode="auto_comment")
    db_session.add(creator)
    db_session.flush()
    autopilot_entry = MonitorOutbox(
        event_type="autopilot_create",
        creator_id=creator.id,
        status="pending",
        payload={"creator_id": creator.id, "video_ids": [1]},
        dedupe_key="commenting-active-auto",
    )
    notify_entry = MonitorOutbox(
        event_type="notify_auto_orders_created",
        creator_id=creator.id,
        status="pending",
        payload={"creator_id": creator.id, "video_ids": [1], "task_ids": [2]},
        dedupe_key="commenting-active-notify",
    )
    db_session.add_all([autopilot_entry, notify_entry])
    db_session.commit()

    claimed = monitor_worker._claim_outbox_batch(
        db_session,
        limit=10,
        lock_owner="test-worker",
        include_autopilot_create=False,
    )

    assert [entry.id for entry in claimed] == [notify_entry.id]
    db_session.refresh(autopilot_entry)
    db_session.refresh(notify_entry)
    assert autopilot_entry.status == "pending"
    assert notify_entry.status == "processing"


@pytest.mark.unit
def test_outbox_claim_prioritizes_manual_review_over_old_notify_only(db_session):
    creator = CreatorSource(username="priority_claim_creator", enabled=True, mode="manual_review")
    db_session.add(creator)
    db_session.flush()

    now = datetime.now(timezone.utc)
    old_notify = MonitorOutbox(
        event_type="notify_new_videos",
        creator_id=creator.id,
        status="pending",
        payload={"creator_id": creator.id, "video_ids": [1]},
        dedupe_key="priority-old-notify",
        created_at=now - timedelta(days=1),
        available_at=now - timedelta(days=1),
    )
    review_entry = MonitorOutbox(
        event_type="notify_pending_review",
        creator_id=creator.id,
        status="pending",
        payload={"creator_id": creator.id, "video_ids": [2]},
        dedupe_key="priority-review",
        created_at=now,
        available_at=now,
    )
    db_session.add_all([old_notify, review_entry])
    db_session.commit()

    claimed = monitor_worker._claim_outbox_batch(
        db_session,
        limit=1,
        lock_owner="test-worker",
    )

    assert [entry.id for entry in claimed] == [review_entry.id]
    db_session.refresh(old_notify)
    db_session.refresh(review_entry)
    assert old_notify.status == "pending"
    assert review_entry.status == "processing"


@pytest.mark.unit
def test_autopilot_limits_cap_video_ids(db_session):
    clear_cache()
    creator = CreatorSource(username="limited_autopilot_creator", enabled=True, mode="auto_comment")
    db_session.add_all(
        [
            creator,
            AppSettings(key=AUTOPILOT_MAX_CREATE_PER_TICK_KEY, value="5"),
            AppSettings(key=AUTOPILOT_MAX_CREATE_PER_CREATOR_KEY, value="2"),
            AppSettings(key=AUTOPILOT_MAX_CREATE_PER_HOUR_KEY, value="10"),
        ]
    )
    db_session.commit()

    try:
        limited_ids, reason = monitor_worker._limited_autopilot_video_ids(
            db_session,
            creator=creator,
            video_ids=[1, 2, 3, 4],
        )
    finally:
        clear_cache()

    assert limited_ids == [1, 2]
    assert reason is None


@pytest.mark.unit
@pytest.mark.asyncio
async def test_autopilot_limit_defers_remaining_videos_instead_of_manual_review(monkeypatch, db_session):
    clear_cache()
    user = User(
        telegram_id=555121,
        username="limited_process_user",
        role="user",
        is_blocked=False,
        video_notifications_enabled=True,
    )
    blogger = MonitoredBlogger(username="limited_process_demo")
    template = TaskTemplate(
        user=user,
        name="limited-template",
        main_comment="Main",
        settings={"text_variants": ["Main", "Second", "Third"]},
    )
    creator = CreatorSource(
        user=user,
        username="limited_process_demo",
        monitored_blogger=blogger,
        enabled=True,
        interval_minutes=15,
        mode="auto_comment",
    )
    db_session.add_all([user, blogger, template, creator])
    db_session.flush()
    creator.default_template_id = template.id
    videos = [
        CreatorVideo(
            creator_id=creator.id,
            video_id=f"v-limited-{idx}",
            url=f"https://www.tiktok.com/@limited_process_demo/video/v-limited-{idx}",
            status="new",
        )
        for idx in range(4)
    ]
    db_session.add_all(videos)
    db_session.flush()
    outbox = MonitorOutbox(
        event_type="autopilot_create",
        status="processing",
        blogger_id=blogger.id,
        creator_id=creator.id,
        payload={"creator_id": creator.id, "video_ids": [video.id for video in videos]},
        dedupe_key="limited-process-autopilot",
    )
    db_session.add_all(
        [
            outbox,
            AppSettings(key=AUTOPILOT_MAX_CREATE_PER_TICK_KEY, value="2"),
            AppSettings(key=AUTOPILOT_MAX_CREATE_PER_CREATOR_KEY, value="4"),
            AppSettings(key=AUTOPILOT_MAX_CREATE_PER_HOUR_KEY, value="20"),
        ]
    )
    db_session.commit()

    captured_video_ids: list[list[int]] = []

    @contextmanager
    def fake_db_context():
        yield db_session

    def fake_create_batch(db, _creator, *, video_ids, **_kwargs):
        captured_video_ids.append(list(video_ids))
        for video in db.query(CreatorVideo).filter(CreatorVideo.id.in_(video_ids)).all():
            video.status = "queued"
            video.task_id = 9000 + int(video.id)
        return MonitoredTaskBatchResult(
            status="created",
            created_video_ids=list(video_ids),
            task_ids=[9000 + int(video_id) for video_id in video_ids],
            total_cost=Decimal("5.00"),
        )

    monkeypatch.setattr(monitor_worker, "get_sync_db_context", fake_db_context)
    monkeypatch.setattr(monitor_worker, "create_monitored_tasks_batch_sync", fake_create_batch)

    try:
        assert await monitor_worker._process_outbox_entry(outbox.id) is True
    finally:
        clear_cache()

    db_session.refresh(outbox)
    assert captured_video_ids == [[videos[0].id, videos[1].id]]
    assert outbox.status == "pending"
    assert outbox.payload["video_ids"] == [videos[2].id, videos[3].id]
    assert outbox.payload["autopilot_deferred_by_limit"] is True
    assert outbox.dedupe_key == monitor_worker._build_outbox_dedupe_key(
        "autopilot_create",
        creator.id,
        [videos[2].id, videos[3].id],
    )
    assert outbox.last_error == "autopilot_limit_deferred_remaining"
    assert outbox.lock_owner is None

    assert (
        db_session.query(MonitorOutbox)
        .filter(MonitorOutbox.event_type == "notify_pending_review")
        .count()
        == 0
    )
    created_notice = (
        db_session.query(MonitorOutbox)
        .filter(MonitorOutbox.event_type == "notify_auto_orders_created")
        .one()
    )
    assert created_notice.payload["video_ids"] == [videos[0].id, videos[1].id]


@pytest.mark.unit
@pytest.mark.asyncio
async def test_autopilot_created_orders_notify_even_when_video_notifications_disabled(monkeypatch, db_session):
    user = User(
        telegram_id=555105,
        username="muted_auto_orders_user",
        role="user",
        is_blocked=False,
        video_notifications_enabled=False,
    )
    blogger = MonitoredBlogger(username="muted_auto_orders_demo")
    template = TaskTemplate(
        user=user,
        id=123,
        name="auto-template",
        main_comment="Main",
        settings={"text_variants": ["Main", "Second", "Third"]},
    )
    creator = CreatorSource(
        user=user,
        username="muted_auto_orders_demo",
        monitored_blogger=blogger,
        enabled=True,
        interval_minutes=15,
        mode="auto_comment",
        default_template_id=123,
    )
    db_session.add_all([user, blogger, template, creator])
    db_session.flush()
    video = CreatorVideo(
        creator_id=creator.id,
        video_id="v-auto-created-muted",
        url="https://www.tiktok.com/@muted_auto_orders_demo/video/v-auto-created-muted",
        status="new",
    )
    db_session.add(video)
    db_session.flush()
    outbox = MonitorOutbox(
        event_type="autopilot_create",
        status="pending",
        blogger_id=blogger.id,
        creator_id=creator.id,
        payload={"creator_id": creator.id, "video_ids": [video.id]},
        dedupe_key="autopilot-muted-created",
    )
    db_session.add(outbox)
    db_session.commit()

    @contextmanager
    def fake_db_context():
        yield db_session

    monkeypatch.setattr(monitor_worker, "get_sync_db_context", fake_db_context)
    monkeypatch.setattr(monitor_worker, "_creator_is_restricted_by_parsing_mode", lambda *args, **kwargs: False)
    monkeypatch.setattr(
        monitor_worker,
        "create_monitored_tasks_batch_sync",
        lambda *args, **kwargs: MonitoredTaskBatchResult(
            status="created",
            created_video_ids=[video.id],
            task_ids=[9001],
            total_cost=Decimal("5.00"),
        ),
    )

    assert await monitor_worker._process_outbox_entry(outbox.id) is True
    db_session.refresh(outbox)
    assert outbox.status == "done"
    created_notice = (
        db_session.query(MonitorOutbox)
        .filter(
            MonitorOutbox.creator_id == creator.id,
            MonitorOutbox.event_type == "notify_auto_orders_created",
        )
        .one()
    )
    assert created_notice.payload["task_ids"] == [9001]


@pytest.mark.unit
@pytest.mark.asyncio
async def test_existing_notify_outbox_is_skipped_when_notifications_disabled(monkeypatch, db_session):
    user = User(
        telegram_id=555104,
        username="muted_existing_user",
        role="user",
        is_blocked=False,
        video_notifications_enabled=False,
    )
    blogger = MonitoredBlogger(username="muted_existing_demo")
    creator = CreatorSource(
        user=user,
        username="muted_existing_demo",
        monitored_blogger=blogger,
        enabled=True,
        interval_minutes=15,
        mode="notify_only",
    )
    db_session.add_all([user, blogger, creator])
    db_session.flush()
    video = CreatorVideo(
        creator_id=creator.id,
        video_id="v-existing-muted",
        url="https://www.tiktok.com/@muted_existing_demo/video/v-existing-muted",
        status="new",
    )
    db_session.add(video)
    db_session.flush()
    outbox = MonitorOutbox(
        event_type="notify_new_videos",
        status="pending",
        blogger_id=blogger.id,
        creator_id=creator.id,
        payload={"creator_id": creator.id, "video_ids": [video.id]},
        dedupe_key="notify-muted-existing",
    )
    db_session.add(outbox)
    db_session.commit()

    @contextmanager
    def fake_db_context():
        yield db_session

    async def fail_if_called(*args, **kwargs):
        raise AssertionError("notification should not be sent")

    monkeypatch.setattr(monitor_worker, "get_sync_db_context", fake_db_context)
    monkeypatch.setattr(monitor_worker, "notify_new_videos", fail_if_called)

    assert await monitor_worker._process_outbox_entry(outbox.id) is True
    db_session.refresh(outbox)
    assert outbox.status == "done"


@pytest.mark.unit
@pytest.mark.asyncio
async def test_outbox_marks_done_after_permanent_telegram_block(monkeypatch, db_session):
    user = User(
        telegram_id=555106,
        username="blocked_delivery_user",
        role="user",
        is_blocked=False,
    )
    blogger = MonitoredBlogger(username="blocked_delivery_demo")
    creator = CreatorSource(
        user=user,
        username="blocked_delivery_demo",
        monitored_blogger=blogger,
        enabled=True,
        interval_minutes=15,
        mode="notify_only",
    )
    db_session.add_all([user, blogger, creator])
    db_session.flush()
    video = CreatorVideo(
        creator_id=creator.id,
        video_id="v-blocked-delivery",
        url="https://www.tiktok.com/@blocked_delivery_demo/video/v-blocked-delivery",
        status="new",
    )
    db_session.add(video)
    db_session.flush()
    outbox = MonitorOutbox(
        event_type="notify_new_videos",
        status="pending",
        blogger_id=blogger.id,
        creator_id=creator.id,
        payload={"creator_id": creator.id, "video_ids": [video.id]},
        dedupe_key="notify-blocked-delivery",
    )
    db_session.add(outbox)
    db_session.commit()

    @contextmanager
    def fake_db_context():
        yield db_session

    async def fake_notify_new_videos(*args, **kwargs):
        return False

    monkeypatch.setattr(monitor_worker, "get_sync_db_context", fake_db_context)
    monkeypatch.setattr(monitor_worker, "notify_new_videos", fake_notify_new_videos)
    monkeypatch.setattr(
        monitor_worker,
        "is_telegram_delivery_permanently_blocked",
        lambda telegram_id: telegram_id == user.telegram_id,
    )

    assert await monitor_worker._process_outbox_entry(outbox.id) is True
    db_session.refresh(outbox)
    assert outbox.status == "done"
    assert outbox.attempts == 0


@pytest.mark.unit
@pytest.mark.asyncio
async def test_mobile_proxy_lock_defers_without_blogger_failure(monkeypatch, db_session):
    user = User(
        telegram_id=555107,
        username="proxy_busy_user",
        role="user",
        is_blocked=False,
    )
    blogger = MonitoredBlogger(
        username="proxy_busy_demo",
        last_seen_video_id="200",
    )
    creator = CreatorSource(
        user=user,
        username="proxy_busy_demo",
        monitored_blogger=blogger,
        enabled=True,
        interval_minutes=15,
        mode="notify_only",
    )
    proxy = Proxy(
        proxy="http://busy-mobile-proxy",
        is_mobile="Y",
        rotation_url="https://proxy.example/rotate",
    )
    account = Account(
        label="proxy_busy_account",
        cookies='{"sessionid":"sid","ttwid":"link","msToken":"token"}',
        status="active",
        proxy=proxy.proxy,
    )
    db_session.add_all([user, blogger, creator, proxy, account])
    db_session.commit()

    def fake_get_monitor_account(db, exclude_ids=None):
        return account

    async def fake_prepare_mobile_proxy_for_account(*args, **kwargs):
        raise monitor_worker.ProxyLockError("Failed to acquire lock for proxy 1 (timeout)")

    failure_calls = []

    async def fake_record_blogger_failure(*args, **kwargs):
        failure_calls.append((args, kwargs))
        return 1

    monkeypatch.setattr(monitor_worker.settings, "MONITOR_MOBILE_PROXY_BUSY_DEFER_MINUTES", 1, raising=False)
    monkeypatch.setattr(monitor_worker, "get_monitor_account", fake_get_monitor_account)
    monkeypatch.setattr(monitor_worker, "prepare_mobile_proxy_for_account", fake_prepare_mobile_proxy_for_account)
    monkeypatch.setattr(monitor_worker, "_record_blogger_failure", fake_record_blogger_failure)

    result = await process_blogger(db_session, blogger, active_user_ids={user.id})

    db_session.refresh(blogger)
    assert result == "mobile_proxy_deferred"
    assert blogger.next_check_at is not None
    assert failure_calls == []


@pytest.mark.unit
def test_mobile_proxy_pressure_guard_decision_and_cap(monkeypatch):
    monkeypatch.setattr(monitor_worker.settings, "MONITOR_PROXY_PRESSURE_DEFER_THRESHOLD", 3, raising=False)
    monkeypatch.setattr(monitor_worker.settings, "MONITOR_PROXY_PRESSURE_DEFER_RATIO", 0.4, raising=False)

    assert monitor_worker._should_activate_mobile_proxy_pressure_guard(
        {"checked": 2, "deferred_proxy": 3},
        processed_count=10,
    ) is False
    assert monitor_worker._should_activate_mobile_proxy_pressure_guard(
        {"checked": 2, "deferred_proxy": 5},
        processed_count=10,
    ) is True

    monkeypatch.setattr(
        monitor_worker,
        "_proxy_capacity_profile",
        {
            "total_proxies": 4,
            "mobile_proxies": 4,
            "residential_proxies": 0,
            "concurrency_cap": 4,
            "rotation_cooldown_seconds": 90,
        },
    )
    monkeypatch.setattr(monitor_worker, "_mobile_proxy_pressure_guard_active", lambda: True)
    monkeypatch.setattr(monitor_worker.settings, "MONITOR_PROXY_PRESSURE_CONCURRENCY_CAP", 1, raising=False)

    assert monitor_worker._proxy_capacity_concurrency_cap() == 1


@pytest.mark.unit
def test_mobile_proxy_hard_failure_requires_repeated_evidence(monkeypatch):
    proxy = SimpleNamespace(id=7, is_mobile="Y")
    monitor_worker._mobile_proxy_hard_failure_events.clear()
    monkeypatch.setattr(monitor_worker, "_get_monitor_redis", lambda: None)
    monkeypatch.setattr(monitor_worker.settings, "MONITOR_MOBILE_PROXY_HARD_FAILURE_BAD_THRESHOLD", 3, raising=False)
    monkeypatch.setattr(monitor_worker.settings, "MONITOR_MOBILE_PROXY_HARD_FAILURE_WINDOW_SECONDS", 600, raising=False)

    error_text = "Page.goto: net::ERR_PROXY_CONNECTION_FAILED at https://www.tiktok.com/@demo"

    assert monitor_worker._should_mark_proxy_bad_for_monitor(
        error_text,
        error_type="network_error",
        proxy_obj=proxy,
    ) is False
    assert monitor_worker._should_mark_proxy_bad_for_monitor(
        error_text,
        error_type="network_error",
        proxy_obj=proxy,
    ) is False
    assert monitor_worker._should_mark_proxy_bad_for_monitor(
        error_text,
        error_type="network_error",
        proxy_obj=proxy,
    ) is True


@pytest.mark.unit
def test_non_mobile_proxy_hard_failure_is_marked_immediately(monkeypatch):
    proxy = SimpleNamespace(id=8, is_mobile="N")
    monkeypatch.setattr(monitor_worker, "_get_monitor_redis", lambda: None)

    assert monitor_worker._should_mark_proxy_bad_for_monitor(
        "Page.goto: net::ERR_PROXY_CONNECTION_FAILED at https://www.tiktok.com/@demo",
        error_type="network_error",
        proxy_obj=proxy,
    ) is True


@pytest.mark.unit
@pytest.mark.asyncio
async def test_mobile_proxy_pressure_handler_records_single_incident(monkeypatch):
    alerts = []
    incidents = []

    async def fake_send_admin_alert_once(*args, **kwargs):
        alerts.append((args, kwargs))

    monkeypatch.setattr(monitor_worker, "_proxy_pressure_guard_until", 0.0, raising=False)
    monkeypatch.setattr(monitor_worker, "_get_monitor_redis", lambda: None)
    monkeypatch.setattr(monitor_worker.settings, "MONITOR_PROXY_PRESSURE_DEFER_THRESHOLD", 3, raising=False)
    monkeypatch.setattr(monitor_worker.settings, "MONITOR_PROXY_PRESSURE_DEFER_RATIO", 0.4, raising=False)
    monkeypatch.setattr(monitor_worker.settings, "MONITOR_PROXY_PRESSURE_GUARD_SECONDS", 60, raising=False)
    monkeypatch.setattr(monitor_worker, "_send_admin_alert_once", fake_send_admin_alert_once)
    monkeypatch.setattr(monitor_worker, "record_incident", lambda *args, **kwargs: incidents.append((args, kwargs)))

    await monitor_worker._handle_mobile_proxy_pressure(
        tick_summary={"checked": 2, "deferred_proxy": 5, "deferred_storm": 0, "deferred_timeout": 0},
        processed_count=10,
        due_backlog=100,
        runtime_snapshot={"due_preview": ["alpha", "beta"]},
    )

    assert alerts
    assert incidents
    assert incidents[0][0][0] == monitor_worker.INCIDENT_MOBILE_PROXY_PRESSURE
    assert monitor_worker._mobile_proxy_pressure_guard_active() is True


@pytest.mark.unit
@pytest.mark.asyncio
async def test_process_blogger_resync_repairs_anchor_without_notifications(monkeypatch, db_session):
    user = User(
        telegram_id=555003,
        username="resync_user",
        role="user",
        is_blocked=False,
    )
    blogger = MonitoredBlogger(
        username="resync_demo",
        last_seen_video_id="100",
    )
    creator = CreatorSource(
        user=user,
        username="resync_demo",
        monitored_blogger=blogger,
        enabled=True,
        interval_minutes=15,
        mode="notify_only",
    )
    account = Account(
        label="resync_account",
        cookies='{"sessionid":"sid","s_v_web_id":"verify","ttwid":"link","msToken":"token"}',
        status="active",
        today_main_comments=0,
        today_replies=0,
        error_count_recent=0,
        validation_fail_count=0,
    )
    db_session.add_all([user, blogger, creator, account])
    db_session.commit()

    monkeypatch.setattr(monitor_worker, "get_monitor_account", lambda db, exclude_ids=None: account)

    async def fake_create_page_for_account(selected_account):
        return None, None

    async def fake_fetch_latest_videos_from_profile(*args, **kwargs):
        return ScanResult(
            status="RESYNC",
            new_videos=[
                ("150", "https://www.tiktok.com/@resync_demo/video/150"),
                ("140", "https://www.tiktok.com/@resync_demo/video/140"),
            ],
            newest_normal_video_id="150",
            anchor_found=False,
            canonical_source="API",
            pages_loaded=1,
            diagnostics={"reason": "anchor_missing"},
        )

    monkeypatch.setattr(monitor_worker, "create_page_for_account", fake_create_page_for_account)
    monkeypatch.setattr(monitor_worker, "fetch_latest_videos_from_profile", fake_fetch_latest_videos_from_profile)

    result = await process_blogger(db_session, blogger, active_user_ids={user.id})

    db_session.refresh(blogger)
    assert result == "resync_anchor_repaired"
    assert blogger.last_seen_video_id == "150"
    assert db_session.query(CreatorVideo).filter(CreatorVideo.creator_id == creator.id).count() == 0
    assert db_session.query(MonitorOutbox).filter(MonitorOutbox.creator_id == creator.id).count() == 0


@pytest.mark.unit
@pytest.mark.asyncio
async def test_process_blogger_resync_does_not_move_anchor_backwards(monkeypatch, db_session):
    user = User(
        telegram_id=555004,
        username="resync_backward_user",
        role="user",
        is_blocked=False,
    )
    blogger = MonitoredBlogger(
        username="resync_backward_demo",
        last_seen_video_id="200",
    )
    creator = CreatorSource(
        user=user,
        username="resync_backward_demo",
        monitored_blogger=blogger,
        enabled=True,
        interval_minutes=15,
        mode="notify_only",
    )
    account = Account(
        label="resync_backward_account",
        cookies='{"sessionid":"sid","s_v_web_id":"verify","ttwid":"link","msToken":"token"}',
        status="active",
        today_main_comments=0,
        today_replies=0,
        error_count_recent=0,
        validation_fail_count=0,
    )
    db_session.add_all([user, blogger, creator, account])
    db_session.commit()

    monkeypatch.setattr(monitor_worker, "get_monitor_account", lambda db, exclude_ids=None: account)

    async def fake_create_page_for_account(selected_account):
        return None, None

    async def fake_fetch_latest_videos_from_profile(*args, **kwargs):
        return ScanResult(
            status="RESYNC",
            new_videos=[("150", "https://www.tiktok.com/@resync_backward_demo/video/150")],
            newest_normal_video_id="150",
            anchor_found=False,
            canonical_source="API",
            pages_loaded=1,
            diagnostics={"reason": "anchor_missing"},
        )

    monkeypatch.setattr(monitor_worker, "create_page_for_account", fake_create_page_for_account)
    monkeypatch.setattr(monitor_worker, "fetch_latest_videos_from_profile", fake_fetch_latest_videos_from_profile)

    result = await process_blogger(db_session, blogger, active_user_ids={user.id})

    db_session.refresh(blogger)
    assert result == "resync_anchor_repaired"
    assert blogger.last_seen_video_id == "200"
    assert db_session.query(CreatorVideo).filter(CreatorVideo.creator_id == creator.id).count() == 0
    assert db_session.query(MonitorOutbox).filter(MonitorOutbox.creator_id == creator.id).count() == 0


@pytest.mark.unit
@pytest.mark.asyncio
async def test_process_blogger_resync_caps_recent_notifications(monkeypatch, db_session):
    user = User(
        telegram_id=555204,
        username="resync_cap_user",
        role="user",
        is_blocked=False,
    )
    blogger = MonitoredBlogger(
        username="resync_cap_demo",
        last_seen_video_id="100",
    )
    creator = CreatorSource(
        user=user,
        username="resync_cap_demo",
        monitored_blogger=blogger,
        enabled=True,
        interval_minutes=15,
        mode="notify_only",
    )
    account = Account(
        label="resync_cap_account",
        cookies='{"sessionid":"sid","s_v_web_id":"verify","ttwid":"link","msToken":"token"}',
        status="active",
        today_main_comments=0,
        today_replies=0,
        error_count_recent=0,
        validation_fail_count=0,
    )
    db_session.add_all([user, blogger, creator, account])
    db_session.commit()

    now_ts = int(datetime.now(timezone.utc).timestamp())
    videos = [
        ("190", "https://www.tiktok.com/@resync_cap_demo/video/190"),
        ("180", "https://www.tiktok.com/@resync_cap_demo/video/180"),
        ("170", "https://www.tiktok.com/@resync_cap_demo/video/170"),
    ]

    monkeypatch.setattr(monitor_worker, "get_monitor_account", lambda db, exclude_ids=None: account)

    async def fake_create_page_for_account(selected_account):
        return None, None

    async def fake_fetch_latest_videos_from_profile(*args, **kwargs):
        return ScanResult(
            status="RESYNC",
            new_videos=videos,
            newest_normal_video_id="190",
            anchor_found=False,
            canonical_source="API",
            pages_loaded=1,
            diagnostics={"reason": "anchor_missing"},
            video_create_times=[now_ts, now_ts - 60, now_ts - 120],
            video_create_times_map={
                "190": now_ts,
                "180": now_ts - 60,
                "170": now_ts - 120,
            },
        )

    monkeypatch.setattr(monitor_worker, "create_page_for_account", fake_create_page_for_account)
    monkeypatch.setattr(monitor_worker, "fetch_latest_videos_from_profile", fake_fetch_latest_videos_from_profile)

    result = await process_blogger(db_session, blogger, active_user_ids={user.id})

    db_session.refresh(blogger)
    stored_videos = (
        db_session.query(CreatorVideo)
        .filter(CreatorVideo.creator_id == creator.id)
        .order_by(CreatorVideo.video_id.desc())
        .all()
    )
    outbox = db_session.query(MonitorOutbox).filter(MonitorOutbox.creator_id == creator.id).one()

    assert result == "resync_anchor_repaired"
    assert blogger.last_seen_video_id == "190"
    assert [video.video_id for video in stored_videos] == ["190", "180"]
    assert outbox.event_type == "notify_new_videos"
    assert len(outbox.payload["video_ids"]) == 2


@pytest.mark.unit
@pytest.mark.asyncio
async def test_process_blogger_releases_monitor_account_reservation(monkeypatch, db_session):
    user = User(
        telegram_id=555209,
        username="reservation_release_user",
        role="user",
        is_blocked=False,
    )
    blogger = MonitoredBlogger(
        username="reservation_release_demo",
        last_seen_video_id="200",
    )
    creator = CreatorSource(
        user=user,
        username="reservation_release_demo",
        monitored_blogger=blogger,
        enabled=True,
        interval_minutes=15,
        mode="notify_only",
    )
    account = Account(
        label="reservation_release_account",
        cookies='{"sessionid":"sid","s_v_web_id":"verify","ttwid":"link","msToken":"token"}',
        status="active",
        today_main_comments=0,
        today_replies=0,
        error_count_recent=0,
        validation_fail_count=0,
    )
    db_session.add_all([user, blogger, creator, account])
    db_session.commit()

    released = []
    monkeypatch.setattr(monitor_worker, "get_monitor_account", lambda db, exclude_ids=None: account)
    monkeypatch.setattr(monitor_worker, "release_main_account_reservation", lambda account_id: released.append(account_id))

    async def fake_create_page_for_account(selected_account):
        return None, None

    async def fake_fetch_latest_videos_from_profile(*args, **kwargs):
        return ScanResult(
            status="OK",
            new_videos=[],
            newest_normal_video_id="200",
            anchor_found=True,
            canonical_source="API",
            pages_loaded=1,
            diagnostics={},
        )

    monkeypatch.setattr(monitor_worker, "create_page_for_account", fake_create_page_for_account)
    monkeypatch.setattr(monitor_worker, "fetch_latest_videos_from_profile", fake_fetch_latest_videos_from_profile)

    result = await process_blogger(db_session, blogger, active_user_ids={user.id})

    assert result == "ok_no_new"
    assert released == [account.id]


@pytest.mark.unit
@pytest.mark.asyncio
async def test_process_blogger_no_account_alert_uses_global_key(monkeypatch, db_session):
    user = User(
        telegram_id=555205,
        username="no_accounts_user",
        role="user",
        is_blocked=False,
    )
    blogger = MonitoredBlogger(
        username="no_accounts_demo",
        last_seen_video_id="200",
    )
    creator = CreatorSource(
        user=user,
        username="no_accounts_demo",
        monitored_blogger=blogger,
        enabled=True,
        interval_minutes=15,
        mode="notify_only",
    )
    db_session.add_all([user, blogger, creator])
    db_session.commit()

    captured_alerts = []

    async def fake_send_admin_alert_once(*args, **kwargs):
        captured_alerts.append(kwargs)

    async def fake_record_blogger_failure(*args, **kwargs):
        return 1

    monkeypatch.setattr(monitor_worker, "get_monitor_account", lambda db, exclude_ids=None: None)
    monkeypatch.setattr(monitor_worker, "_count_eligible_monitor_accounts", lambda db, exclude_ids=None: 0)
    monkeypatch.setattr(monitor_worker, "_send_admin_alert_once", fake_send_admin_alert_once)
    monkeypatch.setattr(monitor_worker, "_record_blogger_failure", fake_record_blogger_failure)

    result = await process_blogger(db_session, blogger, active_user_ids={user.id})

    assert result == "no_accounts_available"
    assert len(captured_alerts) == 1
    assert captured_alerts[0]["key"] == "no_monitor_accounts_available"
    assert captured_alerts[0]["cooldown_minutes"] >= 5


@pytest.mark.unit
@pytest.mark.asyncio
async def test_process_blogger_defers_when_account_slots_are_busy(monkeypatch, db_session):
    user = User(
        telegram_id=555206,
        username="account_busy_user",
        role="user",
        is_blocked=False,
    )
    blogger = MonitoredBlogger(
        username="account_busy_demo",
        last_seen_video_id="200",
    )
    creator = CreatorSource(
        user=user,
        username="account_busy_demo",
        monitored_blogger=blogger,
        enabled=True,
        interval_minutes=15,
        mode="notify_only",
    )
    db_session.add_all([user, blogger, creator])
    db_session.commit()

    async def fake_send_admin_alert_once(*args, **kwargs):
        raise AssertionError("busy account slots must not notify owner")

    async def fake_record_blogger_failure(*args, **kwargs):
        raise AssertionError("busy account slots must not increase blogger failure streak")

    monkeypatch.setattr(monitor_worker, "get_monitor_account", lambda db, exclude_ids=None: None)
    monkeypatch.setattr(monitor_worker, "_count_eligible_monitor_accounts", lambda db, exclude_ids=None: 12)
    monkeypatch.setattr(monitor_worker, "_send_admin_alert_once", fake_send_admin_alert_once)
    monkeypatch.setattr(monitor_worker, "_record_blogger_failure", fake_record_blogger_failure)

    result = await process_blogger(db_session, blogger, active_user_ids={user.id})

    assert result == "mobile_proxy_deferred"


@pytest.mark.unit
@pytest.mark.asyncio
async def test_process_blogger_rate_limit_does_not_mark_proxy_bad(monkeypatch, db_session):
    user = User(
        telegram_id=555207,
        username="rate_limit_user",
        role="user",
        is_blocked=False,
    )
    blogger = MonitoredBlogger(
        username="rate_limit_demo",
        last_seen_video_id="200",
    )
    creator = CreatorSource(
        user=user,
        username="rate_limit_demo",
        monitored_blogger=blogger,
        enabled=True,
        interval_minutes=15,
        mode="notify_only",
    )
    account = Account(
        label="rate_limit_account",
        cookies='{"sessionid":"sid","ttwid":"link","msToken":"token"}',
        proxy="http://proxy.example:10001",
        status="active",
        today_main_comments=0,
        today_replies=0,
        error_count_recent=0,
        validation_fail_count=0,
    )
    db_session.add_all([user, blogger, creator, account])
    db_session.commit()

    calls = {"account": 0}

    def fake_get_monitor_account(db, exclude_ids=None):
        calls["account"] += 1
        return account if calls["account"] == 1 else None

    async def fake_create_page_for_account(selected_account):
        return None, None

    async def fake_fetch_latest_videos_from_profile(*args, **kwargs):
        return ScanResult(
            status="RATE_LIMIT",
            new_videos=[],
            newest_normal_video_id=None,
            anchor_found=False,
            canonical_source="API",
            pages_loaded=0,
            diagnostics={"reason": "rate_limited"},
        )

    async def fake_send_admin_alert_once(*args, **kwargs):
        return None

    async def fake_record_blogger_failure(*args, **kwargs):
        return 1

    def fake_mark_proxy_bad(*args, **kwargs):
        raise AssertionError("TikTok rate limit must not globally mark proxy bad by default")

    monkeypatch.setattr(monitor_worker, "get_monitor_account", fake_get_monitor_account)
    monkeypatch.setattr(monitor_worker, "create_page_for_account", fake_create_page_for_account)
    monkeypatch.setattr(monitor_worker, "fetch_latest_videos_from_profile", fake_fetch_latest_videos_from_profile)
    monkeypatch.setattr(monitor_worker, "_count_eligible_monitor_accounts", lambda db, exclude_ids=None: 0)
    monkeypatch.setattr(monitor_worker, "_send_admin_alert_once", fake_send_admin_alert_once)
    monkeypatch.setattr(monitor_worker, "_record_blogger_failure", fake_record_blogger_failure)
    monkeypatch.setattr(monitor_worker, "mark_proxy_bad", fake_mark_proxy_bad)

    result = await process_blogger(db_session, blogger, active_user_ids={user.id})

    assert result == "no_accounts_available"


@pytest.mark.unit
@pytest.mark.asyncio
async def test_process_blogger_rate_limited_exception_does_not_mark_proxy_bad(monkeypatch, db_session):
    user = User(
        telegram_id=555208,
        username="rate_limited_exception_user",
        role="user",
        is_blocked=False,
    )
    blogger = MonitoredBlogger(
        username="rate_limited_exception_demo",
        last_seen_video_id="200",
    )
    creator = CreatorSource(
        user=user,
        username="rate_limited_exception_demo",
        monitored_blogger=blogger,
        enabled=True,
        interval_minutes=15,
        mode="notify_only",
    )
    account = Account(
        label="rate_limited_exception_account",
        cookies='{"sessionid":"sid","ttwid":"link","msToken":"token"}',
        proxy="http://proxy.example:10002",
        status="active",
        today_main_comments=0,
        today_replies=0,
        error_count_recent=0,
        validation_fail_count=0,
    )
    db_session.add_all([user, blogger, creator, account])
    db_session.commit()

    calls = {"account": 0}

    def fake_get_monitor_account(db, exclude_ids=None):
        calls["account"] += 1
        return account if calls["account"] == 1 else None

    async def fake_create_page_for_account(selected_account):
        return None, None

    async def fake_fetch_latest_videos_from_profile(*args, **kwargs):
        raise RateLimited("HTTP 429")

    async def fake_send_admin_alert_once(*args, **kwargs):
        return None

    async def fake_record_blogger_failure(*args, **kwargs):
        return 1

    def fake_mark_proxy_bad(*args, **kwargs):
        raise AssertionError("RateLimited must not globally mark proxy bad by default")

    monkeypatch.setattr(monitor_worker, "get_monitor_account", fake_get_monitor_account)
    monkeypatch.setattr(monitor_worker, "create_page_for_account", fake_create_page_for_account)
    monkeypatch.setattr(monitor_worker, "fetch_latest_videos_from_profile", fake_fetch_latest_videos_from_profile)
    monkeypatch.setattr(monitor_worker, "_count_eligible_monitor_accounts", lambda db, exclude_ids=None: 0)
    monkeypatch.setattr(monitor_worker, "_send_admin_alert_once", fake_send_admin_alert_once)
    monkeypatch.setattr(monitor_worker, "_record_blogger_failure", fake_record_blogger_failure)
    monkeypatch.setattr(monitor_worker, "mark_proxy_bad", fake_mark_proxy_bad)

    result = await process_blogger(db_session, blogger, active_user_ids={user.id})

    assert result == "no_accounts_available"


@pytest.mark.unit
@pytest.mark.asyncio
async def test_process_blogger_suspicious_login_block_is_soft_error(monkeypatch, db_session):
    user = User(
        telegram_id=555005,
        username="suspicious_user",
        role="user",
        is_blocked=False,
    )
    blogger = MonitoredBlogger(
        username="suspicious_demo",
        last_seen_video_id="200",
    )
    creator = CreatorSource(
        user=user,
        username="suspicious_demo",
        monitored_blogger=blogger,
        enabled=True,
        interval_minutes=15,
        mode="notify_only",
    )
    account = Account(
        label="suspicious_account",
        cookies='{"sessionid":"sid","s_v_web_id":"verify","ttwid":"link","msToken":"token"}',
        status="active",
        today_main_comments=0,
        today_replies=0,
        error_count_recent=0,
        validation_fail_count=0,
    )
    db_session.add_all([user, blogger, creator, account])
    db_session.commit()

    def fake_get_monitor_account(db, exclude_ids=None):
        return None if exclude_ids else account

    async def fake_create_page_for_account(selected_account):
        return None, None

    async def fake_fetch_latest_videos_from_profile(*args, **kwargs):
        raise PageBlocked(
            "SUSPICIOUS account detected: SUSPICIOUS: Login button found but user indicators also present"
        )

    async def fake_send_admin_alert_once(*args, **kwargs):
        return False

    def fake_record_incident(*args, **kwargs):
        return None

    monkeypatch.setattr(monitor_worker, "get_monitor_account", fake_get_monitor_account)
    monkeypatch.setattr(monitor_worker, "create_page_for_account", fake_create_page_for_account)
    monkeypatch.setattr(monitor_worker, "fetch_latest_videos_from_profile", fake_fetch_latest_videos_from_profile)
    monkeypatch.setattr(monitor_worker, "_send_admin_alert_once", fake_send_admin_alert_once)
    monkeypatch.setattr(monitor_worker, "record_incident", fake_record_incident)

    result = await process_blogger(db_session, blogger, active_user_ids={user.id})

    db_session.refresh(account)
    assert result == "no_accounts_available"
    assert account.status == "active"
    assert "SUSPICIOUS" in (account.last_error or "")


@pytest.mark.unit
@pytest.mark.asyncio
async def test_process_blogger_temporary_tiktok_error_defers_without_account_penalty(monkeypatch, db_session):
    user = User(
        telegram_id=555105,
        username="tiktok_degraded_user",
        role="user",
        is_blocked=False,
    )
    blogger = MonitoredBlogger(
        username="degraded_demo",
        last_seen_video_id="200",
    )
    creator = CreatorSource(
        user=user,
        username="degraded_demo",
        monitored_blogger=blogger,
        enabled=True,
        interval_minutes=15,
        mode="notify_only",
    )
    account = Account(
        label="degraded_account",
        cookies='{"sessionid":"sid","ttwid":"link","msToken":"token"}',
        status="active",
        today_main_comments=0,
        today_replies=0,
        error_count_recent=0,
        validation_fail_count=0,
    )
    db_session.add_all([user, blogger, creator, account])
    db_session.commit()

    def fake_get_monitor_account(db, exclude_ids=None):
        return account

    async def fake_create_page_for_account(selected_account):
        return None, None

    async def fake_fetch_latest_videos_from_profile(*args, **kwargs):
        raise PageBlocked(
            "SUSPICIOUS account detected: SUSPICIOUS: Temporary page error detected: 'Something went wrong'"
        )

    incidents = []

    async def fake_send_admin_alert_once(*args, **kwargs):
        return False

    monkeypatch.setattr(monitor_worker, "get_monitor_account", fake_get_monitor_account)
    monkeypatch.setattr(monitor_worker, "create_page_for_account", fake_create_page_for_account)
    monkeypatch.setattr(monitor_worker, "fetch_latest_videos_from_profile", fake_fetch_latest_videos_from_profile)
    monkeypatch.setattr(monitor_worker, "record_incident", lambda *args, **kwargs: incidents.append((args, kwargs)))
    monkeypatch.setattr(monitor_worker, "_send_admin_alert_once", fake_send_admin_alert_once)

    result = await process_blogger(db_session, blogger, active_user_ids={user.id})

    db_session.refresh(account)
    assert result == "tiktok_degraded_deferred"
    assert account.status == "active"
    assert account.error_count_recent == 0
    assert account.last_error is None
    assert incidents
    assert incidents[0][0][0] == monitor_worker.INCIDENT_TIKTOK_REGION_DEGRADATION


@pytest.mark.unit
@pytest.mark.asyncio
async def test_process_blogger_repeated_untrusted_empty_defers_without_account_penalty(monkeypatch, db_session):
    user = User(
        telegram_id=555106,
        username="empty_degraded_user",
        role="user",
        is_blocked=False,
    )
    blogger = MonitoredBlogger(
        username="empty_degraded_demo",
        last_seen_video_id="200",
    )
    creator = CreatorSource(
        user=user,
        username="empty_degraded_demo",
        monitored_blogger=blogger,
        enabled=True,
        interval_minutes=15,
        mode="notify_only",
    )
    account_1 = Account(
        label="empty_account_1",
        cookies='{"sessionid":"sid1","ttwid":"link1","msToken":"token1"}',
        status="active",
        today_main_comments=0,
        today_replies=0,
        error_count_recent=0,
        validation_fail_count=0,
    )
    account_2 = Account(
        label="empty_account_2",
        cookies='{"sessionid":"sid2","ttwid":"link2","msToken":"token2"}',
        status="active",
        today_main_comments=0,
        today_replies=0,
        error_count_recent=0,
        validation_fail_count=0,
    )
    db_session.add_all([user, blogger, creator, account_1, account_2])
    db_session.commit()

    accounts = [account_1, account_2]

    def fake_get_monitor_account(db, exclude_ids=None):
        excluded = set(exclude_ids or [])
        for account in accounts:
            if account.id not in excluded:
                return account
        return None

    async def fake_create_page_for_account(selected_account):
        return None, None

    async def fake_fetch_latest_videos_from_profile(*args, **kwargs):
        return ScanResult(
            status="EMPTY",
            new_videos=[],
            newest_normal_video_id=None,
            anchor_found=False,
            canonical_source="DOM",
            pages_loaded=1,
            diagnostics={"reason": "unknown"},
        )

    incidents = []

    async def fake_send_admin_alert_once(*args, **kwargs):
        return False

    monkeypatch.setattr(monitor_worker, "get_monitor_account", fake_get_monitor_account)
    monkeypatch.setattr(monitor_worker, "create_page_for_account", fake_create_page_for_account)
    monkeypatch.setattr(monitor_worker, "fetch_latest_videos_from_profile", fake_fetch_latest_videos_from_profile)
    monkeypatch.setattr(monitor_worker, "record_incident", lambda *args, **kwargs: incidents.append((args, kwargs)))
    monkeypatch.setattr(monitor_worker, "_send_admin_alert_once", fake_send_admin_alert_once)

    result = await process_blogger(db_session, blogger, active_user_ids={user.id})

    db_session.refresh(account_1)
    db_session.refresh(account_2)
    assert result == "tiktok_degraded_deferred"
    assert account_1.last_error is None
    assert account_2.last_error is None
    assert account_1.error_count_recent == 0
    assert account_2.error_count_recent == 0
    assert incidents


@pytest.mark.unit
@pytest.mark.asyncio
async def test_global_network_probe_reports_outage_when_all_hosts_fail(monkeypatch):
    monitor_worker._global_network_probe_cache["checked_at"] = 0.0
    monitor_worker._global_network_probe_cache["unavailable"] = False
    monkeypatch.setattr(
        monitor_worker.settings,
        "GLOBAL_NETWORK_PROBE_HOSTS",
        "first.example:443,second.example:443",
        raising=False,
    )

    attempts = []

    async def fake_probe(host, port, timeout_seconds):
        attempts.append((host, port, timeout_seconds))
        return False

    monkeypatch.setattr(monitor_worker, "_probe_network_endpoint", fake_probe)

    assert await monitor_worker._is_global_network_unavailable(force=True) is True
    assert [host for host, _port, _timeout in attempts] == ["first.example", "second.example"]


@pytest.mark.unit
@pytest.mark.asyncio
async def test_global_network_probe_reports_available_when_any_host_works(monkeypatch):
    monitor_worker._global_network_probe_cache["checked_at"] = 0.0
    monitor_worker._global_network_probe_cache["unavailable"] = True
    monkeypatch.setattr(
        monitor_worker.settings,
        "GLOBAL_NETWORK_PROBE_HOSTS",
        "first.example:443,second.example:443",
        raising=False,
    )

    async def fake_probe(host, port, timeout_seconds):
        return host == "second.example"

    monkeypatch.setattr(monitor_worker, "_probe_network_endpoint", fake_probe)

    assert await monitor_worker._is_global_network_unavailable(force=True) is False


@pytest.mark.unit
def test_monitor_health_summary_is_compact_and_informative():
    summary = monitor_worker._format_monitor_health_summary(
        {
            "window_minutes": 15,
            "bloggers": 3,
            "due": 1,
            "ok_last_window": 2,
            "new_videos": 4,
            "accounts_eligible": 16,
            "accounts_reserved": 2,
            "accounts_total": 19,
            "bad_proxies": 0,
            "dead_session": 3,
            "outbox_pending": 1,
            "outbox_ready": 1,
            "outbox_failed": 0,
            "oldest_overdue_minutes": 5,
        }
    )

    # Snapshot above intentionally omits adaptive_status_counts and
    # priority_counts → format renders an empty adaptive block as "[?]" and
    # avg_interval=0m. We assert structure (key=value tokens), not the exact
    # placeholder character, so the test stays robust to future formatter tweaks.
    expected_tokens = [
        "[MONITOR_HEALTH]",
        "bloggers=3",
        "due=1",
        "ok_last_15m=2",
        "new_videos=4",
        "avg_interval=0m",
        "accounts=16/19",
        "reserved=2",
        "bad_proxy=0",
        "dead_session=3",
        "outbox_pending=1",
        "outbox_ready=1",
        "outbox_failed=0",
        "oldest_overdue=5m",
    ]
    for tok in expected_tokens:
        assert tok in summary, f"missing token {tok!r} in:\n{summary!r}"
    assert "adaptive=[" in summary, f"missing adaptive=[...] block in:\n{summary!r}"


@pytest.mark.unit
def test_monitor_commenting_throttle_uses_executor_task_activity(monkeypatch):
    monkeypatch.setattr(monitor_worker.settings, "MONITOR_THROTTLE_WHILE_COMMENTING", True, raising=False)
    monkeypatch.setattr(
        monitor_worker,
        "executor_task_activity_snapshot",
        lambda: (True, {"lock_ttl": 300}),
    )

    active, payload = monitor_worker._monitor_commenting_throttle_state()

    assert active is True
    assert payload["lock_ttl"] == 300


@pytest.mark.unit
def test_monitor_commenting_throttle_limits_batch_concurrency_and_outbox(monkeypatch):
    monkeypatch.setattr(monitor_worker.settings, "MONITOR_COMMENTING_ACTIVE_MAX_BLOGGERS_PER_TICK", 1, raising=False)
    monkeypatch.setattr(monitor_worker.settings, "MONITOR_COMMENTING_ACTIVE_CONCURRENCY", 1, raising=False)
    monkeypatch.setattr(monitor_worker.settings, "MONITOR_COMMENTING_ACTIVE_BALANCED_MAX_BLOGGERS_PER_TICK", 2, raising=False)
    monkeypatch.setattr(monitor_worker.settings, "MONITOR_COMMENTING_ACTIVE_BALANCED_CONCURRENCY", 1, raising=False)
    monkeypatch.setattr(monitor_worker.settings, "MONITOR_COMMENTING_ACTIVE_OUTBOX_BATCH_LIMIT", 2, raising=False)
    monkeypatch.setattr(monitor_worker.settings, "MONITOR_COMMENTING_ACTIVE_PAUSE_OUTBOX_BATCH_LIMIT", 1, raising=False)

    batch_limit, concurrency = monitor_worker._apply_commenting_active_monitor_limits(
        batch_limit=10,
        effective_concurrency=4,
        commenting_active=True,
    )

    assert batch_limit == 1
    assert concurrency == 1
    batch_limit, concurrency = monitor_worker._apply_commenting_active_monitor_limits(
        batch_limit=10,
        effective_concurrency=4,
        commenting_active=True,
        commenting_mode="balanced",
    )
    assert batch_limit == 2
    assert concurrency == 1
    batch_limit, concurrency = monitor_worker._apply_commenting_active_monitor_limits(
        batch_limit=10,
        effective_concurrency=4,
        commenting_active=True,
        commenting_mode="paused",
    )
    assert batch_limit == 0
    assert concurrency == 0
    assert monitor_worker._monitor_outbox_limit_for_commenting(True) == 2
    assert monitor_worker._monitor_outbox_limit_for_commenting(True, "paused") == 1
    assert monitor_worker._monitor_outbox_limit_for_commenting(False) is None
    assert monitor_worker._monitor_defer_autopilot_outbox_for_commenting(True) is True
    assert monitor_worker._monitor_defer_autopilot_outbox_for_commenting(False) is False


@pytest.mark.unit
def test_monitor_commenting_pressure_pauses_only_on_high_load(monkeypatch):
    monkeypatch.setattr(monitor_worker.settings, "MONITOR_COMMENTING_ACTIVE_PAUSE_ON_HIGH_LOAD", True, raising=False)
    monkeypatch.setattr(monitor_worker.settings, "MONITOR_COMMENTING_ACTIVE_LIGHT_CPU_PERCENT", 70.0, raising=False)
    monkeypatch.setattr(monitor_worker.settings, "MONITOR_COMMENTING_ACTIVE_LIGHT_MEMORY_PERCENT", 75.0, raising=False)
    monkeypatch.setattr(monitor_worker.settings, "MONITOR_COMMENTING_ACTIVE_PAUSE_CPU_PERCENT", 85.0, raising=False)
    monkeypatch.setattr(monitor_worker.settings, "MONITOR_COMMENTING_ACTIVE_PAUSE_MEMORY_PERCENT", 85.0, raising=False)

    monkeypatch.setattr(
        monitor_worker,
        "_monitor_system_load_snapshot",
        lambda: {"cpu_percent": 35.0, "memory_percent": 55.0},
    )
    state = monitor_worker._monitor_commenting_pressure_state(True)
    assert state["mode"] == "balanced"

    monkeypatch.setattr(
        monitor_worker,
        "_monitor_system_load_snapshot",
        lambda: {"cpu_percent": 72.0, "memory_percent": 60.0},
    )
    state = monitor_worker._monitor_commenting_pressure_state(True)
    assert state["mode"] == "light"

    monkeypatch.setattr(
        monitor_worker,
        "_monitor_system_load_snapshot",
        lambda: {"cpu_percent": 88.0, "memory_percent": 70.0},
    )
    state = monitor_worker._monitor_commenting_pressure_state(True)
    assert state["mode"] == "paused"


@pytest.mark.unit
@pytest.mark.asyncio
async def test_close_monitor_resources_closes_pool_context_for_account(monkeypatch):
    class FakePage:
        _managed_by_pool = True

        def __init__(self):
            self.returned = False
            self.closed = False
            self._pool_account_id = 661

        async def close(self):
            self.closed = True

    class FakeContext:
        def __init__(self):
            self.closed = False

        async def close(self):
            self.closed = True

    pool_closed_pages = []

    async def fake_close_pool_page(page, *, reason=None):
        page.returned = True
        pool_closed_pages.append((page._pool_account_id, reason))
        return True

    import executor.browser_pool as browser_pool

    monkeypatch.setattr(browser_pool, "close_pool_page", fake_close_pool_page)
    page = FakePage()
    context = FakeContext()

    await monitor_worker._close_monitor_resources(page, context)

    assert page.returned is True
    assert page.closed is False
    assert context.closed is False
    assert pool_closed_pages == [(661, "monitor_check_done")]


@pytest.mark.unit
@pytest.mark.asyncio
async def test_close_monitor_resources_falls_back_when_pool_declines_page(monkeypatch):
    class FakePage:
        _managed_by_pool = True

        def __init__(self):
            self.closed = False
            self._pool_account_id = 662

        def is_closed(self):
            return self.closed

        async def close(self):
            self.closed = True

    class FakeContext:
        def __init__(self):
            self.closed = False

        async def close(self):
            self.closed = True

    async def fake_close_pool_page(_page, *, reason=None):
        return False

    import executor.browser_pool as browser_pool

    monkeypatch.setattr(browser_pool, "close_pool_page", fake_close_pool_page)
    page = FakePage()
    context = FakeContext()

    await monitor_worker._close_monitor_resources(page, context)

    assert page.closed is True
    assert context.closed is False


@pytest.mark.unit
@pytest.mark.asyncio
async def test_close_monitor_resources_closes_non_pool_context(monkeypatch):
    class FakePage:
        _managed_by_pool = False

        def __init__(self):
            self.closed_by_safe_close = False

        async def close(self):
            self.closed_by_safe_close = True

    class FakeContext:
        def __init__(self):
            self.closed = False

        async def close(self):
            self.closed = True

    async def fake_safe_close_page(page):
        page.closed_by_safe_close = True

    import executor.page_utils as page_utils

    monkeypatch.setattr(page_utils, "safe_close_page", fake_safe_close_page)
    page = FakePage()
    context = FakeContext()

    await monitor_worker._close_monitor_resources(page, context)

    assert page.closed_by_safe_close is True
    assert context.closed is True


@pytest.mark.unit
def test_late_browser_cancel_filter_catches_frame_detach_noise():
    assert monitor_worker._looks_like_late_browser_cancel(
        'Future exception was never retrieved net::ERR_ABORTED; maybe frame was detached?'
    )
    assert monitor_worker._looks_like_late_browser_cancel(
        "Future exception was never retrieved Page.goto: Timeout 30000ms exceeded"
    )
    assert not monitor_worker._looks_like_late_browser_cancel("database write failed")


@pytest.mark.unit
@pytest.mark.asyncio
async def test_process_blogger_rotation_error_excludes_failed_proxy(monkeypatch, db_session):
    monkeypatch.setattr(monitor_worker.settings, "USE_PROXY", True, raising=False)
    monkeypatch.setattr(account_service_module.settings, "USE_PROXY", True, raising=False)
    monkeypatch.setattr(account_service_module, "get_bad_proxy_set", lambda proxies: set())
    monkeypatch.setattr(account_service_module, "is_proxy_bad", lambda proxy: False)
    monkeypatch.setattr(account_service_module, "get_locked_proxy_ids", lambda proxy_ids: set())
    monkeypatch.setattr(monitor_worker, "is_proxy_bad", lambda proxy: False)
    monkeypatch.setattr(monitor_worker, "_should_mark_mobile_proxy_bad_after_hard_failure", lambda *args, **kwargs: False)
    monkeypatch.setattr(monitor_worker, "_clear_monitor_proxy_success", lambda *args, **kwargs: None)

    user = User(telegram_id=555301, username="rotation_switch_user", role="user", is_blocked=False)
    blogger = MonitoredBlogger(username="rotation_switch_demo", last_seen_video_id="200")
    creator = CreatorSource(
        user=user,
        username="rotation_switch_demo",
        monitored_blogger=blogger,
        enabled=True,
        interval_minutes=15,
        mode="notify_only",
    )
    proxy_a = Proxy(proxy="http://mobile-a", is_mobile="Y", rotation_url="https://proxy.example/a")
    proxy_b = Proxy(proxy="http://mobile-b", is_mobile="Y", rotation_url="https://proxy.example/b")
    account_a1 = Account(label="rotation_a1", cookies='{"sessionid":"sid1"}', status="active", proxy=proxy_a.proxy)
    account_a2 = Account(label="rotation_a2", cookies='{"sessionid":"sid2"}', status="active", proxy=proxy_a.proxy)
    account_b = Account(label="rotation_b", cookies='{"sessionid":"sid3"}', status="active", proxy=proxy_b.proxy)
    db_session.add_all([user, blogger, creator, proxy_a, proxy_b, account_a1, account_a2, account_b])
    db_session.commit()

    prepared_proxies = []

    async def fake_prepare_mobile_proxy_for_account(account, db, proxy_obj):
        prepared_proxies.append(proxy_obj.proxy)
        if proxy_obj.proxy == proxy_a.proxy:
            raise monitor_worker.ProxyRotationError("HTTP 502 when rotating proxy")
        return False, proxy_obj

    async def fake_create_page_for_account(selected_account):
        assert selected_account.proxy == proxy_b.proxy
        return None, None

    async def fake_fetch_latest_videos_from_profile(*args, **kwargs):
        return ScanResult(
            status="OK",
            new_videos=[],
            newest_normal_video_id="200",
            anchor_found=True,
            canonical_source="API",
            pages_loaded=1,
            diagnostics={},
        )

    monkeypatch.setattr(monitor_worker, "prepare_mobile_proxy_for_account", fake_prepare_mobile_proxy_for_account)
    monkeypatch.setattr(monitor_worker, "create_page_for_account", fake_create_page_for_account)
    monkeypatch.setattr(monitor_worker, "fetch_latest_videos_from_profile", fake_fetch_latest_videos_from_profile)

    result = await process_blogger(db_session, blogger, active_user_ids={user.id})

    assert result == "ok_no_new"
    assert prepared_proxies == [proxy_a.proxy, proxy_b.proxy]


@pytest.mark.unit
@pytest.mark.asyncio
async def test_process_blogger_rotation_error_defers_after_all_failed_proxies(monkeypatch, db_session):
    monkeypatch.setattr(monitor_worker.settings, "USE_PROXY", True, raising=False)
    monkeypatch.setattr(account_service_module.settings, "USE_PROXY", True, raising=False)
    monkeypatch.setattr(account_service_module, "get_bad_proxy_set", lambda proxies: set())
    monkeypatch.setattr(account_service_module, "is_proxy_bad", lambda proxy: False)
    monkeypatch.setattr(account_service_module, "get_locked_proxy_ids", lambda proxy_ids: set())
    monkeypatch.setattr(monitor_worker, "is_proxy_bad", lambda proxy: False)
    monkeypatch.setattr(monitor_worker, "_should_mark_mobile_proxy_bad_after_hard_failure", lambda *args, **kwargs: False)

    user = User(telegram_id=555302, username="rotation_defer_user", role="user", is_blocked=False)
    blogger = MonitoredBlogger(username="rotation_defer_demo", last_seen_video_id="200")
    creator = CreatorSource(
        user=user,
        username="rotation_defer_demo",
        monitored_blogger=blogger,
        enabled=True,
        interval_minutes=15,
        mode="notify_only",
    )
    proxy_a = Proxy(proxy="http://mobile-a-defer", is_mobile="Y", rotation_url="https://proxy.example/a")
    proxy_b = Proxy(proxy="http://mobile-b-defer", is_mobile="Y", rotation_url="https://proxy.example/b")
    account_a = Account(label="rotation_defer_a", cookies='{"sessionid":"sid1"}', status="active", proxy=proxy_a.proxy)
    account_b = Account(label="rotation_defer_b", cookies='{"sessionid":"sid2"}', status="active", proxy=proxy_b.proxy)
    db_session.add_all([user, blogger, creator, proxy_a, proxy_b, account_a, account_b])
    db_session.commit()

    prepared_proxies = []

    async def fake_prepare_mobile_proxy_for_account(account, db, proxy_obj):
        prepared_proxies.append(proxy_obj.proxy)
        raise monitor_worker.ProxyRotationError("HTTP 502 when rotating proxy")

    async def fake_send_admin_alert_once(*args, **kwargs):
        raise AssertionError("Rotation exhaustion should defer, not send no-account admin alert")

    async def fake_create_page_for_account(*args, **kwargs):
        raise AssertionError("No page should be opened when every mobile proxy failed rotation")

    monkeypatch.setattr(monitor_worker, "prepare_mobile_proxy_for_account", fake_prepare_mobile_proxy_for_account)
    monkeypatch.setattr(monitor_worker, "_send_admin_alert_once", fake_send_admin_alert_once)
    monkeypatch.setattr(monitor_worker, "create_page_for_account", fake_create_page_for_account)

    result = await process_blogger(db_session, blogger, active_user_ids={user.id})

    assert result == "mobile_proxy_deferred"
    assert prepared_proxies == [proxy_a.proxy, proxy_b.proxy]
