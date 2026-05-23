
"""
Ð’Ð¾Ñ€ÐºÐµÑ€ Ð´Ð»Ñ Ð¼Ð¾Ð½Ð¸Ñ‚Ð¾Ñ€Ð¸Ð½Ð³Ð° TikTok Ð±Ð»Ð¾Ð³ÐµÑ€Ð¾Ð² Ð¸ Ð¿Ð°Ñ€ÑÐ¸Ð½Ð³Ð° Ð½Ð¾Ð²Ñ‹Ñ… Ð²Ð¸Ð´ÐµÐ¾.
Ð Ð°Ð±Ð¾Ñ‚Ð°ÐµÑ‚ ÐºÐ°Ðº Ð¾Ñ‚Ð´ÐµÐ»ÑŒÐ½Ñ‹Ð¹ Ð¿Ñ€Ð¾Ñ†ÐµÑÑ, Ð½Ðµ Ð¼ÐµÑˆÐ°ÐµÑ‚ executor.
"""
import sys
import os

# Ð”Ð¾Ð±Ð°Ð²Ð»ÑÐµÐ¼ ÐºÐ¾Ñ€ÐµÐ½ÑŒ Ð¿Ñ€Ð¾ÐµÐºÑ‚Ð° Ð² PYTHONPATH
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# Windows: ProactorEventLoop Ð´Ð»Ñ Playwright (subprocess)
import _win_fix  # noqa: F401

import asyncio
import dataclasses
import json
import logging
import math
import random
import time
import socket
from datetime import datetime, timezone, timedelta
from typing import List, Tuple, Optional, Iterable, Any
from sqlalchemy.orm import Session, selectinload
from sqlalchemy import or_, and_, func, delete, case
from sqlalchemy.exc import IntegrityError
from db.database import get_sync_db, get_sync_db_context
from db.models import (
    CreatorSource,
    CreatorVideo,
    TaskTemplate,
    Task,
    Account,
    MonitoredBlogger,
    MonitorOutbox,
    BloggerCheckLog,
    AppSettings,
    User,
    Subscription,
)
from services.redis_service import (
    executor_task_activity_snapshot,
    get_redis_client,
    update_monitor_heartbeat,
)
from services.metrics import increment_counter, set_gauge, record_timing
from services.video_parser import fetch_latest_videos_from_profile, PageBlocked, RateLimited, ScanResult
from services.account_service import (
    MONITOR_CATEGORY_CAPTCHA,
    MONITOR_CATEGORY_DETECTION,
    MONITOR_CATEGORY_EMPTY,
    MONITOR_CATEGORY_NETWORK,
    MONITOR_CATEGORY_PROXY,
    MONITOR_CATEGORY_RATE_LIMIT,
    MONITOR_CATEGORY_SUSPICIOUS,
    MONITOR_CATEGORY_UNKNOWN,
    _build_monitor_error_payload,
    _infer_monitor_category,
    _serialize_error_payload,
    get_monitor_account,
    get_monitor_pool_stats,
    mark_monitor_use,
    release_main_account_reservation,
)
from services.proxy_health_service import is_proxy_bad, mark_proxy_bad, clear_proxy_bad
from services.proxy_assignment_service import rebalance_working_account_proxies
from executor.tiktok_client_async import create_page_for_account
from services.notify_service import (
    is_telegram_delivery_permanently_blocked,
    notify_pending_review,
    notify_new_videos,
    notify_auto_orders_created,
    notify_autopilot_paused_by_parsing_mode,
    send_notification_to_owner,
)
from services.monitor_incident_service import (
    INCIDENT_MOBILE_PROXY_PRESSURE,
    INCIDENT_TIKTOK_REGION_DEGRADATION,
    build_admin_alert,
    record_incident,
)
from services.balance_service import has_full_functionality_access
from services.mobile_proxy_service import (
    clear_proxy_locks,
    prepare_mobile_proxy_for_account,
    release_proxy_lock,
    ProxyRotationError,
    ProxyLockError
)
from db.models import Proxy
from services.task_creation_service import create_monitored_tasks_batch_sync
from services.adaptive_scheduler import SCHEMA_VERSION, AdaptiveSettings, compute_adaptive_interval, merge_timestamps
from services.app_settings_service import (
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
    get_all_adaptive_settings,
    get_monitor_max_parallel_browsers,
    is_parsing_mode,
)
from config.settings import get_settings

logger = logging.getLogger("video.monitor")
settings = get_settings()


def _creator_is_restricted_by_parsing_mode(
    db: Session,
    creator: CreatorSource | None,
    parsing_mode: bool | None = None,
) -> bool:
    """True when global parsing mode should restrict this creator's owner."""
    if parsing_mode is None:
        parsing_mode = is_parsing_mode(db)
    if not parsing_mode:
        return False
    if not creator or not creator.user_id:
        return True

    owner_user = db.query(User).filter(User.id == creator.user_id).first()
    return not (owner_user and has_full_functionality_access(owner_user))


USER_NOTIFICATION_EVENTS = {
    "notify_new_videos",
    "notify_pending_review",
}


def _creator_owner_notifications_enabled(db: Session, creator: CreatorSource | None) -> bool:
    if not creator or not creator.user_id:
        return False

    owner_user = db.query(User).filter(User.id == creator.user_id).first()
    if not owner_user:
        return False
    return bool(getattr(owner_user, "video_notifications_enabled", True))


def _creator_owner_delivery_permanently_blocked(db: Session, creator: CreatorSource | None) -> bool:
    if not creator or not creator.user_id:
        return False

    owner_user = db.query(User).filter(User.id == creator.user_id).first()
    if not owner_user:
        return False
    return is_telegram_delivery_permanently_blocked(getattr(owner_user, "telegram_id", None))


def _creator_has_autopilot_template_reference(creator: CreatorSource | None) -> bool:
    if not creator:
        return False
    if creator.default_template_id:
        return True
    template_ids = creator.template_ids
    return isinstance(template_ids, list) and bool(template_ids)


def _read_app_setting_uncached(db: Session, key: str, default: Any = None) -> Any:
    value = db.query(AppSettings.value).filter(AppSettings.key == key).scalar()
    if value is None:
        return default
    if not isinstance(value, str):
        return value
    try:
        return json.loads(value)
    except (TypeError, ValueError, json.JSONDecodeError):
        return value


def _coerce_autopilot_limit(value: Any, default: int, minimum: int, maximum: int) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        parsed = default
    return max(minimum, min(maximum, parsed))


def _get_autopilot_mode_safe(db: Session) -> str:
    try:
        raw = _read_app_setting_uncached(db, AUTOPILOT_CONTROL_MODE_KEY, AUTOPILOT_CONTROL_ACTIVE)
        mode = str(raw or "").strip().lower()
        return mode if mode in AUTOPILOT_CONTROL_MODES else AUTOPILOT_CONTROL_ACTIVE
    except Exception as exc:
        logger.warning("[VIDEO_MONITOR] Failed to read autopilot mode, using active: %s", exc)
        return AUTOPILOT_CONTROL_ACTIVE


def _get_autopilot_limits_safe(db: Session) -> dict[str, int]:
    try:
        return {
            "max_create_per_tick": _coerce_autopilot_limit(
                _read_app_setting_uncached(
                    db,
                    AUTOPILOT_MAX_CREATE_PER_TICK_KEY,
                    AUTOPILOT_DEFAULT_MAX_CREATE_PER_TICK,
                ),
                AUTOPILOT_DEFAULT_MAX_CREATE_PER_TICK,
                0,
                1000,
            ),
            "max_create_per_creator": _coerce_autopilot_limit(
                _read_app_setting_uncached(
                    db,
                    AUTOPILOT_MAX_CREATE_PER_CREATOR_KEY,
                    AUTOPILOT_DEFAULT_MAX_CREATE_PER_CREATOR,
                ),
                AUTOPILOT_DEFAULT_MAX_CREATE_PER_CREATOR,
                0,
                1000,
            ),
            "max_create_per_hour": _coerce_autopilot_limit(
                _read_app_setting_uncached(
                    db,
                    AUTOPILOT_MAX_CREATE_PER_HOUR_KEY,
                    AUTOPILOT_DEFAULT_MAX_CREATE_PER_HOUR,
                ),
                AUTOPILOT_DEFAULT_MAX_CREATE_PER_HOUR,
                0,
                10000,
            ),
        }
    except Exception as exc:
        logger.warning("[VIDEO_MONITOR] Failed to read autopilot limits, using defaults: %s", exc)
        return {
            "max_create_per_tick": AUTOPILOT_DEFAULT_MAX_CREATE_PER_TICK,
            "max_create_per_creator": AUTOPILOT_DEFAULT_MAX_CREATE_PER_CREATOR,
            "max_create_per_hour": AUTOPILOT_DEFAULT_MAX_CREATE_PER_HOUR,
        }


def _autopilot_created_last_hour_count(db: Session) -> int:
    cutoff = datetime.now(timezone.utc) - timedelta(hours=1)
    try:
        return int(
            db.query(func.count(Task.id))
            .filter(
                Task.created_at >= cutoff,
                Task.settings["origin"].as_string() == "autopilot",
            )
            .scalar()
            or 0
        )
    except Exception as exc:
        logger.warning("[VIDEO_MONITOR] Failed to count hourly autopilot tasks: %s", exc)
        return 0


def _limited_autopilot_video_ids(
    db: Session,
    *,
    creator: CreatorSource,
    video_ids: list[int],
) -> tuple[list[int], str | None]:
    limits = _get_autopilot_limits_safe(db)
    cap = len(video_ids)

    for key in ("max_create_per_tick", "max_create_per_creator"):
        value = int(limits.get(key) or 0)
        if value <= 0:
            return ([], f"{key}_disabled")
        cap = min(cap, value)

    max_per_hour = int(limits.get("max_create_per_hour") or 0)
    if max_per_hour <= 0:
        return ([], "max_create_per_hour_disabled")
    remaining_hour = max(0, max_per_hour - _autopilot_created_last_hour_count(db))
    if remaining_hour <= 0:
        return ([], "max_create_per_hour_reached")
    cap = min(cap, remaining_hour)

    if cap <= 0:
        return ([], "autopilot_limit_reached")
    limited = video_ids[:cap]
    if len(limited) < len(video_ids):
        logger.info(
            "[VIDEO_MONITOR] Autopilot event for creator_id=%s limited: %s/%s video(s)",
            creator.id,
            len(limited),
            len(video_ids),
        )
    return (limited, None)


CHECK_TICK = 60  # ÐŸÑ€Ð¾Ð²ÐµÑ€ÐºÐ° ÐºÐ°Ð¶Ð´Ñ‹Ðµ 60 ÑÐµÐºÑƒÐ½Ð´


async def _release_proxy_lock_safe(lock_acquired: bool, proxy_obj, account_id: int | None = None) -> None:
    """
    Ð‘ÐµÐ·Ð¾Ð¿Ð°ÑÐ½Ð¾ Ð¾ÑÐ²Ð¾Ð±Ð¾Ð¶Ð´Ð°ÐµÑ‚ Ð±Ð»Ð¾ÐºÐ¸Ñ€Ð¾Ð²ÐºÑƒ Ð¼Ð¾Ð±Ð¸Ð»ÑŒÐ½Ð¾Ð³Ð¾ Ð¿Ñ€Ð¾ÐºÑÐ¸.
    
    Args:
        lock_acquired: Ð¤Ð»Ð°Ð³, ÑƒÐºÐ°Ð·Ñ‹Ð²Ð°ÑŽÑ‰Ð¸Ð¹, Ð±Ñ‹Ð»Ð° Ð»Ð¸ Ð·Ð°Ñ…Ð²Ð°Ñ‡ÐµÐ½Ð° Ð±Ð»Ð¾ÐºÐ¸Ñ€Ð¾Ð²ÐºÐ°
        proxy_obj: ÐžÐ±ÑŠÐµÐºÑ‚ Proxy (Ð´Ð»Ñ Ð¿Ð¾Ð»ÑƒÑ‡ÐµÐ½Ð¸Ñ ID)
        account_id: ID Ð°ÐºÐºÐ°ÑƒÐ½Ñ‚Ð° (Ð´Ð»Ñ Ð»Ð¾Ð³Ð¸Ñ€Ð¾Ð²Ð°Ð½Ð¸Ñ)
    """
    if lock_acquired and proxy_obj:
        try:
            await release_proxy_lock(proxy_obj.id)
            logger.debug(f"[UnifiedParser] Released lock for mobile proxy {proxy_obj.id}")
        except Exception as lock_err:
            logger.warning(f"[UnifiedParser] Failed to release proxy lock for account {account_id or 'unknown'}: {lock_err}")
VIDEOS_LIMIT = 12  # ÐœÐ°ÐºÑÐ¸Ð¼ÑƒÐ¼ Ð²Ð¸Ð´ÐµÐ¾ Ð´Ð»Ñ Ð¿Ð°Ñ€ÑÐ¸Ð½Ð³Ð° Ð·Ð° Ñ€Ð°Ð·
INITIAL_PARSE_LIMIT = 12  #ÐšÐ¾Ð»Ð¸Ñ‡ÐµÑÑ‚Ð²Ð¾ Ð²Ð¸Ð´ÐµÐ¾ Ð´Ð»Ñ Ð¿Ð°Ñ€ÑÐ¸Ð½Ð³Ð° Ð¿Ñ€Ð¸ ÑÐ¾Ð·Ð´Ð°Ð½Ð¸Ð¸ Ð±Ð»Ð¾Ð³ÐµÑ€Ð°
LEASE_MINUTES = 3  # Ð’Ñ€ÐµÐ¼Ñ lease Ð´Ð»Ñ Ð¿Ñ€ÐµÐ´Ð¾Ñ‚Ð²Ñ€Ð°Ñ‰ÐµÐ½Ð¸Ñ double-check
LEASE_HEARTBEAT_SECONDS = 45
STALE_LOCK_MINUTES = 12  # ÐžÑ‡Ð¸ÑÑ‚ÐºÐ° Ð·Ð°Ð»Ð¸Ð¿ÑˆÐ¸Ñ… lock (Ð² Ð¼Ð¸Ð½ÑƒÑ‚Ð°Ñ…)
MAX_CREATORS_PER_TICK = 5  #ÐœÐ°ÐºÑÐ¸Ð¼ÑƒÐ¼ Ð±Ð»Ð¾Ð³ÐµÑ€Ð¾Ð² Ð´Ð»Ñ Ð¾Ð±Ñ€Ð°Ð±Ð¾Ñ‚ÐºÐ¸ Ð·Ð° Ð¾Ð´Ð¸Ð½ Ñ‚Ð¸Ðº
MONITOR_OUTBOX_RETRY_BASE_SECONDS = 30
MONITOR_OUTBOX_MAX_BACKOFF_SECONDS = 15 * 60
MONITOR_OUTBOX_STALE_LOCK_MINUTES = 10
MONITOR_OUTBOX_MAX_ATTEMPTS = 20          # после N попыток → abandoned (не крутится вечно)
MONITOR_OUTBOX_ENTRY_TIMEOUT_SECONDS = 30 # таймаут на обработку одного outbox-события
SILENT_BLOGGER_ARCHIVE_DAYS = 60          # после N дней без постов → предложить архивировать
SILENT_BLOGGER_ALERT_COOLDOWN_HOURS = 7 * 24  # не чаще раза в неделю на одного блогера

_last_admin_alert: dict[str, datetime] = {}
_blogger_fail_streak: dict[int, int] = {}
_blogger_timeout_streak: dict[int, int] = {}
_timeout_events: list[dict[str, Any]] = []
_navigation_timeout_events: list[dict[str, Any]] = []
_navigation_timeout_storm_until: datetime | None = None
_mobile_proxy_hard_failure_events: dict[int, list[float]] = {}
_local_browser_issue_events: list[datetime] = []
_last_browser_pool_reset_at: datetime | None = None
_global_network_probe_cache: dict[str, Any] = {"checked_at": 0.0, "unavailable": False}
_last_health_summary_log: float = 0.0

# --- Autonomous recovery state ---
# Circuit breaker: open when all checks in a tick fail due to empty pool.
_circuit_open_until: float = 0.0          # monotonic; 0 = closed
_consecutive_fail_ticks: int = 0           # ticks where 0 bloggers succeeded
_last_auto_recovery_at: float = 0.0        # last time auto-recovery ran
_last_proxy_rebalance_at: float = 0.0      # last time proxy/account rebalance ran
_proxy_pressure_guard_until: float = 0.0   # monotonic; temporary concurrency guard

# --- Adaptive proxy capacity profile ---
# Worker recomputes this snapshot after every successful rebalance. Used to cap
# blogger concurrency and to recommend an adaptive rotation cooldown to
# mobile_proxy_service via Redis. See _refresh_proxy_capacity_profile().
_proxy_capacity_profile: dict[str, int] = {
    "total_proxies": 0,
    "mobile_proxies": 0,
    "residential_proxies": 0,
    "concurrency_cap": 0,
    "rotation_cooldown_seconds": 0,
}
_monitor_cpu_times_last: tuple[int, int] | None = None

# Thresholds (not settings — intentionally hardcoded for safety)
_CIRCUIT_BREAKER_FAIL_TICKS = 5           # 5 consecutive empty-pool ticks (~5 min) → open
_CIRCUIT_BREAKER_OPEN_MINUTES = 15        # pause all checks for 15 minutes
_AUTO_RECOVERY_INTERVAL_SECONDS = 1800    # never run auto-recovery more than once per 30 min
_AUTO_RECOVERY_IDLE_MINUTES = 30          # only reset accounts idle for 30+ min
_PROXY_REBALANCE_INTERVAL_SECONDS = 600   # keep working accounts balanced across proxy types
_DYNAMIC_CONCURRENCY_BACKLOG_LOW = 10     # above this: 2× base concurrency
_DYNAMIC_CONCURRENCY_BACKLOG_HIGH = 25    # above this: MAX_CREATORS_PER_TICK


def _monitor_max_bloggers_per_tick(max_parallel_browsers: int) -> int:
    configured = getattr(settings, "MONITOR_MAX_BLOGGERS_PER_TICK", MAX_CREATORS_PER_TICK)
    try:
        batch_limit = int(configured)
    except (TypeError, ValueError):
        batch_limit = MAX_CREATORS_PER_TICK
    batch_limit = max(1, batch_limit)
    # Keep the configured browser pool busy when backlog is large without
    # increasing actual Playwright concurrency.
    return max(batch_limit, max(1, int(max_parallel_browsers)) * 2)


def _monitor_sleep_seconds_after_tick(*, due_backlog: int, processed_count: int) -> int:
    """Adaptive tick pacing.

    - backlog growing → tighten sleep so the worker drains faster
    - normal flow      → CHECK_TICK (60s)
    - completely idle  → extend to MONITOR_IDLE_SLEEP_SECONDS (default 180s)
      so we don't hammer the DB with empty due-queries when adaptive has put
      all 74 bloggers on a future schedule.
    """
    if processed_count > 0 and due_backlog > processed_count:
        try:
            return max(1, int(getattr(settings, "MONITOR_BACKLOG_SLEEP_SECONDS", 5)))
        except (TypeError, ValueError):
            return 5
    if processed_count == 0 and due_backlog == 0:
        try:
            return max(CHECK_TICK, int(getattr(settings, "MONITOR_IDLE_SLEEP_SECONDS", 180)))
        except (TypeError, ValueError):
            return 180
    return CHECK_TICK


def _parse_network_probe_hosts(value: str | None) -> list[tuple[str, int]]:
    hosts: list[tuple[str, int]] = []
    for raw_item in (value or "").split(","):
        item = raw_item.strip()
        if not item:
            continue
        if ":" in item:
            host, port_text = item.rsplit(":", 1)
            try:
                port = int(port_text)
            except ValueError:
                port = 443
        else:
            host = item
            port = 443
        host = host.strip()
        if host:
            hosts.append((host, port))
    return hosts or [("www.tiktok.com", 443), ("api.telegram.org", 443)]


def _should_update_blogger_anchor(newest_video_id: str | None, current_video_id: str | None) -> bool:
    if not newest_video_id:
        return False
    if not current_video_id:
        return True
    newest = str(newest_video_id)
    current = str(current_video_id)
    if newest == current:
        return False
    if newest.isdigit() and current.isdigit():
        return int(newest) > int(current)
    return True


async def _probe_network_endpoint(host: str, port: int, timeout_seconds: float) -> bool:
    try:
        reader, writer = await asyncio.wait_for(
            asyncio.open_connection(host, port),
            timeout=max(0.5, timeout_seconds),
        )
        writer.close()
        await writer.wait_closed()
        return True
    except Exception:
        return False


async def _is_global_network_unavailable(*, force: bool = False) -> bool:
    now_monotonic = time.monotonic()
    cache_seconds = float(getattr(settings, "GLOBAL_NETWORK_PROBE_CACHE_SECONDS", 60))
    if not force and now_monotonic - float(_global_network_probe_cache["checked_at"]) < cache_seconds:
        return bool(_global_network_probe_cache["unavailable"])

    timeout_seconds = float(getattr(settings, "GLOBAL_NETWORK_PROBE_TIMEOUT_SECONDS", 3))
    hosts = _parse_network_probe_hosts(getattr(settings, "GLOBAL_NETWORK_PROBE_HOSTS", None))
    available = False
    for host, port in hosts:
        if await _probe_network_endpoint(host, port, timeout_seconds):
            available = True
            break

    unavailable = not available
    _global_network_probe_cache["checked_at"] = now_monotonic
    _global_network_probe_cache["unavailable"] = unavailable
    if unavailable:
        logger.warning(
            "[VIDEO_MONITOR] Global network probe failed; monitor account/proxy penalties are suppressed"
        )
    return unavailable


def _looks_like_network_error(error_text: str) -> bool:
    lowered = str(error_text or "").lower()
    if _looks_like_local_browser_infrastructure_error(lowered):
        return False
    return any(
        marker in lowered
        for marker in [
            "timeout",
            "timed_out",
            "network",
            "connection",
            "err_internet_disconnected",
            "err_name_not_resolved",
            "err_timed_out",
            "clientconnectorerror",
            "winerror 121",
        ]
    )


def _looks_like_local_browser_infrastructure_error(error_text: str) -> bool:
    lowered = str(error_text or "").lower()
    return any(
        marker in lowered
        for marker in [
            "browsertype.launch",
            "connection closed while reading from the driver",
            "target page, context or browser has been closed",
            "browser has been closed",
            "playwright",
        ]
    )


_HARD_PROXY_FAILURE_MARKERS = (
    "err_proxy_connection_failed",
    "err_tunnel_connection_failed",
    "proxy connection failed",
    "proxy authentication",
    "proxy auth",
    "proxy server refused",
    "socks proxy",
    "tunnel connection failed",
)


def _looks_like_hard_proxy_failure(error_text: str) -> bool:
    lowered = str(error_text or "").lower()
    return any(marker in lowered for marker in _HARD_PROXY_FAILURE_MARKERS)


def _is_mobile_proxy_obj(proxy_obj: Proxy | None) -> bool:
    return bool(proxy_obj and getattr(proxy_obj, "is_mobile", None) == "Y")


def _mobile_proxy_hard_failure_key(proxy_obj: Proxy) -> str:
    return f"videomonitor:mobile_proxy_hard_failure:{getattr(proxy_obj, 'id', 0) or 0}"


def _should_mark_mobile_proxy_bad_after_hard_failure(proxy_obj: Proxy | None, reason: str) -> bool:
    if not _is_mobile_proxy_obj(proxy_obj):
        return True

    threshold = _monitor_setting_int("MONITOR_MOBILE_PROXY_HARD_FAILURE_BAD_THRESHOLD", 3, minimum=1)
    window_seconds = _monitor_setting_int("MONITOR_MOBILE_PROXY_HARD_FAILURE_WINDOW_SECONDS", 600, minimum=60)
    if threshold <= 1:
        return True

    proxy_id = int(getattr(proxy_obj, "id", 0) or 0)
    redis_client = _get_monitor_redis()
    if redis_client and proxy_id:
        try:
            redis_key = _mobile_proxy_hard_failure_key(proxy_obj)
            count = int(redis_client.incr(redis_key))
            redis_client.expire(redis_key, window_seconds)
            if count >= threshold:
                logger.warning(
                    "[VIDEO_MONITOR] Mobile proxy %s hard failures reached %s/%s; marking bad: %s",
                    proxy_id,
                    count,
                    threshold,
                    str(reason)[:180],
                )
                return True
            logger.warning(
                "[VIDEO_MONITOR] Mobile proxy %s transient hard failure %s/%s; keeping proxy usable after rotation: %s",
                proxy_id,
                count,
                threshold,
                str(reason)[:180],
            )
            return False
        except Exception as redis_error:
            logger.debug("[VIDEO_MONITOR] Mobile proxy hard failure counter unavailable: %s", redis_error)

    now_monotonic = time.monotonic()
    events = [
        ts
        for ts in _mobile_proxy_hard_failure_events.get(proxy_id, [])
        if now_monotonic - ts <= window_seconds
    ]
    events.append(now_monotonic)
    _mobile_proxy_hard_failure_events[proxy_id] = events[-threshold:]
    if len(events) >= threshold:
        logger.warning(
            "[VIDEO_MONITOR] Mobile proxy %s hard failures reached %s/%s; marking bad: %s",
            proxy_id or "unknown",
            len(events),
            threshold,
            str(reason)[:180],
        )
        return True

    logger.warning(
        "[VIDEO_MONITOR] Mobile proxy %s transient hard failure %s/%s; keeping proxy usable after rotation: %s",
        proxy_id or "unknown",
        len(events),
        threshold,
        str(reason)[:180],
    )
    return False


def _clear_mobile_proxy_hard_failures(proxy_obj: Proxy | None) -> None:
    if not _is_mobile_proxy_obj(proxy_obj):
        return
    proxy_id = int(getattr(proxy_obj, "id", 0) or 0)
    if proxy_id:
        _mobile_proxy_hard_failure_events.pop(proxy_id, None)
    redis_client = _get_monitor_redis()
    if redis_client and proxy_id:
        try:
            redis_client.delete(_mobile_proxy_hard_failure_key(proxy_obj))
        except Exception:
            logger.debug("[VIDEO_MONITOR] Failed to clear mobile proxy hard failure counter", exc_info=True)


def _clear_monitor_proxy_success(proxy_value: str | None, proxy_obj: Proxy | None = None) -> None:
    if proxy_value:
        clear_proxy_bad(proxy_value)
    _clear_mobile_proxy_hard_failures(proxy_obj)


def _should_mark_proxy_bad_for_monitor(
    error_text: str,
    *,
    error_type: str | None = None,
    proxy_obj: Proxy | None = None,
) -> bool:
    if _looks_like_hard_proxy_failure(error_text):
        if _is_mobile_proxy_obj(proxy_obj):
            return _should_mark_mobile_proxy_bad_after_hard_failure(proxy_obj, error_text)
        return True
    if str(error_type or "").lower() != "network_error":
        return False
    if _is_mobile_proxy_obj(proxy_obj) and not bool(
        getattr(settings, "MONITOR_MARK_MOBILE_PROXY_BAD_ON_NETWORK", False)
    ):
        return False
    if not bool(getattr(settings, "MONITOR_MARK_PROXY_BAD_ON_TIMEOUT", False)):
        return False
    return _looks_like_network_error(error_text) and not _should_defer_monitor_network_timeout(
        error_text,
        error_type=error_type,
    )


def _should_mark_proxy_bad_for_monitor_rate_limit(proxy_obj: Proxy | None = None) -> bool:
    if not bool(getattr(settings, "MONITOR_MARK_PROXY_BAD_ON_RATE_LIMIT", False)):
        return False
    return not (proxy_obj and getattr(proxy_obj, "is_mobile", None) == "Y")


def _should_defer_monitor_network_timeout(error_text: str, *, error_type: str | None = None) -> bool:
    lowered = str(error_text or "").lower()
    if _looks_like_hard_proxy_failure(lowered):
        return False
    if _looks_like_local_browser_infrastructure_error(lowered):
        return False
    if str(error_type or "").lower() not in {"", "network_error"}:
        return False
    timeout_marker = any(
        marker in lowered
        for marker in ("err_timed_out", "timed_out", "timed out", "timeout")
    )
    navigation_marker = "page.goto" in lowered or "navigating to" in lowered
    tiktok_marker = "tiktok.com" in lowered
    return timeout_marker and (navigation_marker or tiktok_marker)


def _clear_navigation_timeout_storm() -> None:
    global _navigation_timeout_storm_until
    _navigation_timeout_events.clear()
    _navigation_timeout_storm_until = None


def _get_navigation_timeout_storm_until(now: datetime | None = None) -> datetime | None:
    global _navigation_timeout_storm_until
    now = now or datetime.now(timezone.utc)
    if _navigation_timeout_storm_until and _navigation_timeout_storm_until > now:
        return _navigation_timeout_storm_until
    _navigation_timeout_storm_until = None
    return None


def _record_navigation_timeout_event(identifier: str, *, now: datetime | None = None) -> dict[str, Any]:
    global _navigation_timeout_storm_until
    now = now or datetime.now(timezone.utc)
    window_minutes = _monitor_setting_int("MONITOR_NAVIGATION_TIMEOUT_WINDOW_MINUTES", 5, minimum=1)
    threshold = _monitor_setting_int("MONITOR_NAVIGATION_TIMEOUT_STORM_THRESHOLD", 4, minimum=1)
    defer_minutes = _monitor_setting_int("MONITOR_NAVIGATION_TIMEOUT_STORM_DEFER_MINUTES", 5, minimum=1)
    cutoff = now - timedelta(minutes=window_minutes)
    _navigation_timeout_events[:] = [
        event for event in _navigation_timeout_events
        if event.get("at") and event["at"] >= cutoff
    ]
    _navigation_timeout_events.append({"identifier": str(identifier or "unknown"), "at": now})
    if len(_navigation_timeout_events) > 1000:
        _navigation_timeout_events[:] = _navigation_timeout_events[-1000:]

    storm_active = len(_navigation_timeout_events) >= threshold
    if storm_active:
        _navigation_timeout_storm_until = now + timedelta(minutes=defer_minutes)

    return {
        "storm_active": storm_active,
        "window_count": len(_navigation_timeout_events),
        "window_minutes": window_minutes,
        "storm_until": _navigation_timeout_storm_until,
    }


def _record_monitor_navigation_success() -> None:
    _clear_navigation_timeout_storm()


def _summarize_tick_results(results: Iterable[Any]) -> dict[str, int]:
    summary = {
        "checked": 0,
        "deferred_storm": 0,
        "deferred_timeout": 0,
        "deferred_proxy": 0,
        "deferred_browser": 0,
        "no_accounts": 0,
        "errors": 0,
    }
    for result in results:
        if isinstance(result, BaseException) or result is None:
            summary["errors"] += 1
        elif result == "network_storm_deferred":
            summary["deferred_storm"] += 1
        elif result == "network_timeout_deferred":
            summary["deferred_timeout"] += 1
        elif result == "mobile_proxy_deferred":
            summary["deferred_proxy"] += 1
        elif result == "local_browser_unavailable":
            summary["deferred_browser"] += 1
        elif result in {"no_accounts_available", "account_pool_exhausted"}:
            summary["no_accounts"] += 1
        else:
            summary["checked"] += 1
    return summary


async def _maybe_reset_browser_pool_after_local_issue(
    *,
    now: datetime | None = None,
    reason: str = "",
) -> bool:
    global _last_browser_pool_reset_at
    now = now or datetime.now(timezone.utc)
    window_minutes = _monitor_setting_int("MONITOR_BROWSER_INFRA_STORM_WINDOW_MINUTES", 5, minimum=1)
    threshold = _monitor_setting_int("MONITOR_BROWSER_INFRA_STORM_THRESHOLD", 3, minimum=1)
    cooldown_minutes = _monitor_setting_int("MONITOR_BROWSER_INFRA_RESET_COOLDOWN_MINUTES", 10, minimum=1)
    cutoff = now - timedelta(minutes=window_minutes)
    _local_browser_issue_events[:] = [event for event in _local_browser_issue_events if event >= cutoff]
    _local_browser_issue_events.append(now)

    if len(_local_browser_issue_events) < threshold:
        return False
    if _last_browser_pool_reset_at and (now - _last_browser_pool_reset_at) < timedelta(minutes=cooldown_minutes):
        return False

    try:
        from executor.browser_pool import get_browser_pool

        pool = await get_browser_pool()
        await pool.close_all()
        _last_browser_pool_reset_at = now
        _local_browser_issue_events.clear()
        logger.warning("[VIDEO_MONITOR] Browser pool reset after local browser issue storm: %s", reason[:180])
        return True
    except Exception as reset_error:
        logger.warning("[VIDEO_MONITOR] Browser pool reset failed after local browser issue: %s", reset_error)
        return False


def _get_monitor_redis():
    try:
        return get_redis_client()
    except Exception:
        return None


def _can_send_admin_alert(key: str, cooldown_minutes: int | None = None) -> bool:
    cooldown_min = int(
        cooldown_minutes
        if cooldown_minutes is not None
        else getattr(get_settings(), "BLOGGER_ALERT_COOLDOWN_MINUTES", 60)
    )
    redis_client = _get_monitor_redis()
    if redis_client:
        try:
            return bool(
                redis_client.set(
                    f"videomonitor:alert:{key}",
                    str(int(time.time())),
                    nx=True,
                    ex=max(60, cooldown_min * 60),
                )
            )
        except Exception as e:
            logger.debug(f"[VIDEO_MONITOR] Redis alert throttle unavailable for {key}: {e}")
    last_time = _last_admin_alert.get(key)
    if not last_time:
        _last_admin_alert[key] = datetime.now(timezone.utc)
        return True
    if (datetime.now(timezone.utc) - last_time) >= timedelta(minutes=cooldown_min):
        _last_admin_alert[key] = datetime.now(timezone.utc)
        return True
    return False


async def _send_admin_alert_once(
    key: str,
    message: str,
    cooldown_minutes: int | None = None,
    keyboard: Any | None = None,
) -> None:
    if _can_send_admin_alert(key, cooldown_minutes=cooldown_minutes):
        await send_notification_to_owner(message, keyboard=keyboard)


_LATE_BROWSER_CANCEL_MARKERS = (
    "targetclosederror",
    "target page",
    "context or browser has been closed",
    "browser has been closed",
    "net::err_aborted",
    "page.goto: timeout",
    "timeout 30000ms exceeded",
    "frame was detached",
    "maybe frame was detached",
)


def _looks_like_late_browser_cancel(text: str) -> bool:
    lowered = str(text or "").lower()
    return any(marker in lowered for marker in _LATE_BROWSER_CANCEL_MARKERS)


def _install_monitor_asyncio_exception_handler() -> None:
    loop = asyncio.get_running_loop()
    previous_handler = loop.get_exception_handler()

    def _handler(loop_obj: asyncio.AbstractEventLoop, context: dict[str, Any]) -> None:
        exc = context.get("exception")
        text = f"{context.get('message') or ''} {exc or ''}".lower()
        if (
            "future exception was never retrieved" in text
            and _looks_like_late_browser_cancel(text)
        ):
            logger.debug("[VIDEO_MONITOR] Suppressed late page-close exception after timeout/cancel: %s", exc)
            return
        if previous_handler:
            previous_handler(loop_obj, context)
        else:
            loop_obj.default_exception_handler(context)

    loop.set_exception_handler(_handler)


def _record_blogger_timeout_event(
    blogger_id: int,
    username: str,
    *,
    now: datetime | None = None,
) -> dict[str, Any]:
    now = now or datetime.now(timezone.utc)
    window_minutes = _monitor_setting_int("MONITOR_TIMEOUT_WINDOW_MINUTES", 15, minimum=1)
    cutoff = now - timedelta(minutes=window_minutes)
    _timeout_events[:] = [
        event for event in _timeout_events
        if event.get("at") and event["at"] >= cutoff
    ]
    if len(_timeout_events) > 1000:
        _timeout_events[:] = _timeout_events[-1000:]
    _blogger_timeout_streak[blogger_id] = int(_blogger_timeout_streak.get(blogger_id, 0) or 0) + 1
    _timeout_events.append({"blogger_id": blogger_id, "username": username, "at": now})

    recent_usernames: list[str] = []
    for event in _timeout_events:
        event_username = str(event.get("username") or "")
        if event_username and event_username not in recent_usernames:
            recent_usernames.append(event_username)

    return {
        "streak": _blogger_timeout_streak[blogger_id],
        "window_count": len(_timeout_events),
        "window_minutes": window_minutes,
        "recent_usernames": recent_usernames[-8:],
    }


def _reset_blogger_timeout_streak(blogger_id: int) -> None:
    _blogger_timeout_streak.pop(blogger_id, None)


_last_streak_cleanup: float = 0.0

def _maybe_cleanup_streak_dicts() -> None:
    global _last_streak_cleanup
    now_mono = time.monotonic()
    if now_mono - _last_streak_cleanup < 3600:
        return
    _last_streak_cleanup = now_mono
    # Keep only bloggers with active streaks (non-zero), cap at 500 entries each.
    if len(_blogger_fail_streak) > 500:
        to_remove = [k for k, v in list(_blogger_fail_streak.items()) if not v]
        for k in to_remove:
            _blogger_fail_streak.pop(k, None)
        if len(_blogger_fail_streak) > 500:
            excess = list(_blogger_fail_streak.keys())[:-500]
            for k in excess:
                _blogger_fail_streak.pop(k, None)
    if len(_blogger_timeout_streak) > 500:
        to_remove = [k for k, v in list(_blogger_timeout_streak.items()) if not v]
        for k in to_remove:
            _blogger_timeout_streak.pop(k, None)
        if len(_blogger_timeout_streak) > 500:
            excess = list(_blogger_timeout_streak.keys())[:-500]
            for k in excess:
                _blogger_timeout_streak.pop(k, None)


async def _maybe_send_blogger_timeout_alert(
    blogger: MonitoredBlogger,
    *,
    timeout_sec: int,
    timeout_stats: dict[str, Any],
) -> None:
    streak_threshold = _monitor_setting_int("MONITOR_TIMEOUT_ALERT_THRESHOLD", 2, minimum=1)
    window_threshold = _monitor_setting_int("MONITOR_TIMEOUT_WINDOW_ALERT_THRESHOLD", 3, minimum=1)
    streak = int(timeout_stats.get("streak", 0) or 0)
    window_count = int(timeout_stats.get("window_count", 0) or 0)
    window_minutes = int(timeout_stats.get("window_minutes", 15) or 15)
    recent = ", ".join(f"@{name}" for name in (timeout_stats.get("recent_usernames") or [])[:8])

    if streak >= streak_threshold:
        await _send_admin_alert_once(
            key=f"blogger_timeout:{blogger.id}",
            message=build_admin_alert(
                severity="warning",
                title="Проверка блогера упирается в timeout",
                what=f"@{blogger.username} не успел провериться за {timeout_sec} секунд.",
                why=f"Timeout подряд по этому блогеру: {streak}. Это техническая проблема проверки, не смена ника.",
                auto_action="Софт перенесёт следующую проверку и попробует снова с другим monitor-аккаунтом.",
                admin_action="Если повторяется часто — проверь скорость прокси, TikTok-доступ и browser/runtime.",
                affected=[f"@{blogger.username}"],
            ),
        )
        return

    if window_count >= window_threshold:
        await _send_admin_alert_once(
            key="blogger_timeout_window",
            message=build_admin_alert(
                severity="warning",
                title="Несколько timeout в мониторинге",
                what=f"За последние {window_minutes} минут было {window_count} timeout-проверок.",
                why="Timeout появился сразу по нескольким блогерам, значит вероятнее проблема инфраструктуры, а не конкретного ника.",
                auto_action="Софт продолжает проверки с cooldown и не помечает блогеров как сменивших ник.",
                admin_action="Проверь прокси/интернет/Playwright. Начни с последних блогеров из списка.",
                affected=[recent or "unknown"],
            ),
        )


def _monitor_setting_int(name: str, default: int, *, minimum: int | None = None) -> int:
    try:
        value = int(getattr(settings, name, default))
    except (TypeError, ValueError):
        value = default
    if minimum is not None:
        value = max(minimum, value)
    return value


def _monitor_setting_bool(name: str, default: bool) -> bool:
    value = getattr(settings, name, default)
    if isinstance(value, str):
        return value.strip().lower() not in {"0", "false", "no", "off", ""}
    return bool(value)


def _monitor_optional_float(value: Any) -> float | None:
    try:
        if value is None:
            return None
        result = float(value)
        if math.isnan(result) or math.isinf(result):
            return None
        return result
    except (TypeError, ValueError):
        return None


def _monitor_system_load_snapshot() -> dict[str, Any]:
    """Best-effort local load snapshot without extra dependencies."""
    global _monitor_cpu_times_last
    snapshot: dict[str, Any] = {
        "cpu_percent": None,
        "memory_percent": None,
        "available_memory_mb": None,
        "source": "unavailable",
    }
    try:
        import ctypes

        class MEMORYSTATUSEX(ctypes.Structure):
            _fields_ = [
                ("dwLength", ctypes.c_ulong),
                ("dwMemoryLoad", ctypes.c_ulong),
                ("ullTotalPhys", ctypes.c_ulonglong),
                ("ullAvailPhys", ctypes.c_ulonglong),
                ("ullTotalPageFile", ctypes.c_ulonglong),
                ("ullAvailPageFile", ctypes.c_ulonglong),
                ("ullTotalVirtual", ctypes.c_ulonglong),
                ("ullAvailVirtual", ctypes.c_ulonglong),
                ("sullAvailExtendedVirtual", ctypes.c_ulonglong),
            ]

        memory_status = MEMORYSTATUSEX()
        memory_status.dwLength = ctypes.sizeof(MEMORYSTATUSEX)
        if ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(memory_status)):
            snapshot["memory_percent"] = float(memory_status.dwMemoryLoad)
            snapshot["available_memory_mb"] = int(memory_status.ullAvailPhys // (1024 * 1024))
            snapshot["source"] = "windows"

        class FILETIME(ctypes.Structure):
            _fields_ = [
                ("dwLowDateTime", ctypes.c_ulong),
                ("dwHighDateTime", ctypes.c_ulong),
            ]

        def _filetime_to_int(filetime: FILETIME) -> int:
            return (int(filetime.dwHighDateTime) << 32) + int(filetime.dwLowDateTime)

        idle_time = FILETIME()
        kernel_time = FILETIME()
        user_time = FILETIME()
        if ctypes.windll.kernel32.GetSystemTimes(
            ctypes.byref(idle_time),
            ctypes.byref(kernel_time),
            ctypes.byref(user_time),
        ):
            idle = _filetime_to_int(idle_time)
            total = _filetime_to_int(kernel_time) + _filetime_to_int(user_time)
            previous = _monitor_cpu_times_last
            _monitor_cpu_times_last = (idle, total)
            if previous:
                idle_delta = max(0, idle - previous[0])
                total_delta = max(0, total - previous[1])
                if total_delta > 0:
                    busy_delta = max(0, total_delta - idle_delta)
                    snapshot["cpu_percent"] = round((busy_delta / total_delta) * 100.0, 1)
                    snapshot["source"] = "windows"
    except Exception as exc:
        snapshot["error"] = str(exc)[:160]
    return snapshot


def _monitor_commenting_pressure_state(commenting_active: bool) -> dict[str, Any]:
    if not commenting_active:
        return {"mode": "normal", "reason": "commenting_inactive", "load": {}}

    load = _monitor_system_load_snapshot()
    cpu_percent = _monitor_optional_float(load.get("cpu_percent"))
    memory_percent = _monitor_optional_float(load.get("memory_percent"))
    if cpu_percent is None and memory_percent is None:
        return {"mode": "light", "reason": "load_unknown", "load": load}

    pause_enabled = _monitor_setting_bool("MONITOR_COMMENTING_ACTIVE_PAUSE_ON_HIGH_LOAD", True)
    pause_cpu = _monitor_setting_float("MONITOR_COMMENTING_ACTIVE_PAUSE_CPU_PERCENT", 85.0, minimum=1.0)
    pause_memory = _monitor_setting_float("MONITOR_COMMENTING_ACTIVE_PAUSE_MEMORY_PERCENT", 85.0, minimum=1.0)
    light_cpu = _monitor_setting_float("MONITOR_COMMENTING_ACTIVE_LIGHT_CPU_PERCENT", 70.0, minimum=1.0)
    light_memory = _monitor_setting_float("MONITOR_COMMENTING_ACTIVE_LIGHT_MEMORY_PERCENT", 75.0, minimum=1.0)

    high_cpu = cpu_percent is not None and cpu_percent >= pause_cpu
    high_memory = memory_percent is not None and memory_percent >= pause_memory
    if pause_enabled and (high_cpu or high_memory):
        return {"mode": "paused", "reason": "high_load", "load": load}

    busy_cpu = cpu_percent is not None and cpu_percent >= light_cpu
    busy_memory = memory_percent is not None and memory_percent >= light_memory
    if busy_cpu or busy_memory:
        return {"mode": "light", "reason": "moderate_load", "load": load}

    return {"mode": "balanced", "reason": "load_ok", "load": load}


def _monitor_commenting_throttle_state() -> tuple[bool, dict[str, Any]]:
    if not _monitor_setting_bool("MONITOR_THROTTLE_WHILE_COMMENTING", True):
        return False, {"reason": "disabled"}
    return executor_task_activity_snapshot()


def _apply_commenting_active_monitor_limits(
    *,
    batch_limit: int,
    effective_concurrency: int,
    commenting_active: bool,
    commenting_mode: str | None = None,
) -> tuple[int, int]:
    if not commenting_active:
        return max(1, batch_limit), max(1, effective_concurrency)
    mode = (commenting_mode or "light").strip().lower()
    if mode == "paused":
        return 0, 0
    if mode == "balanced":
        throttled_batch = min(
            max(1, batch_limit),
            _monitor_setting_int("MONITOR_COMMENTING_ACTIVE_BALANCED_MAX_BLOGGERS_PER_TICK", 2, minimum=1),
        )
        throttled_concurrency = min(
            max(1, effective_concurrency),
            _monitor_setting_int("MONITOR_COMMENTING_ACTIVE_BALANCED_CONCURRENCY", 1, minimum=1),
        )
        return throttled_batch, throttled_concurrency
    throttled_batch = min(
        max(1, batch_limit),
        _monitor_setting_int("MONITOR_COMMENTING_ACTIVE_MAX_BLOGGERS_PER_TICK", 1, minimum=1),
    )
    throttled_concurrency = min(
        max(1, effective_concurrency),
        _monitor_setting_int("MONITOR_COMMENTING_ACTIVE_CONCURRENCY", 1, minimum=1),
    )
    return throttled_batch, throttled_concurrency


def _monitor_outbox_limit_for_commenting(commenting_active: bool, commenting_mode: str | None = None) -> int | None:
    if not commenting_active:
        return None
    if (commenting_mode or "").strip().lower() == "paused":
        return _monitor_setting_int("MONITOR_COMMENTING_ACTIVE_PAUSE_OUTBOX_BATCH_LIMIT", 1, minimum=1)
    return _monitor_setting_int("MONITOR_COMMENTING_ACTIVE_OUTBOX_BATCH_LIMIT", 2, minimum=1)


def _monitor_defer_autopilot_outbox_for_commenting(commenting_active: bool) -> bool:
    return bool(
        commenting_active
        and _monitor_setting_bool("MONITOR_COMMENTING_ACTIVE_DEFER_AUTOPILOT_OUTBOX", True)
    )


def _monitor_setting_float(name: str, default: float, *, minimum: float | None = None) -> float:
    try:
        value = float(getattr(settings, name, default))
    except (TypeError, ValueError):
        value = default
    if minimum is not None:
        value = max(minimum, value)
    return value


def _monitor_soft_error_category(reason: str | None) -> str:
    normalized = str(reason or "")
    category = _infer_monitor_category(normalized)
    if category != MONITOR_CATEGORY_UNKNOWN:
        return category
    if _looks_like_network_error(normalized):
        return MONITOR_CATEGORY_NETWORK
    return MONITOR_CATEGORY_UNKNOWN


def _is_transient_monitor_account_issue(category: str) -> bool:
    return category in {
        MONITOR_CATEGORY_CAPTCHA,
        MONITOR_CATEGORY_DETECTION,
        MONITOR_CATEGORY_EMPTY,
        MONITOR_CATEGORY_NETWORK,
        MONITOR_CATEGORY_PROXY,
        MONITOR_CATEGORY_RATE_LIMIT,
        MONITOR_CATEGORY_SUSPICIOUS,
        MONITOR_CATEGORY_UNKNOWN,
    }


def _evaluate_monitor_pool_pressure(snapshot: dict[str, Any]) -> dict[str, Any] | None:
    bloggers = int(snapshot.get("bloggers", 0) or 0)
    due = int(snapshot.get("due", 0) or 0)
    eligible = int(snapshot.get("accounts_eligible", 0) or 0)
    total = int(snapshot.get("accounts_total", 0) or 0)
    dead_session = int(snapshot.get("dead_session", 0) or 0)
    bad_proxies = int(snapshot.get("bad_proxies", 0) or 0)
    ok_last_window = int(snapshot.get("ok_last_window", 0) or 0)
    oldest_overdue = int(snapshot.get("oldest_overdue_minutes", 0) or 0)

    if bloggers <= 0:
        return None

    low_threshold = _monitor_setting_int("MONITOR_ACCOUNT_LOW_THRESHOLD", 8, minimum=1)
    critical_threshold = _monitor_setting_int("MONITOR_ACCOUNT_CRITICAL_THRESHOLD", 3, minimum=0)
    min_per_due = _monitor_setting_float("MONITOR_ACCOUNT_MIN_ELIGIBLE_PER_DUE", 2.0, minimum=0.0)
    min_ratio = _monitor_setting_float("MONITOR_ACCOUNT_MIN_ELIGIBLE_RATIO", 0.35, minimum=0.0)
    dead_threshold = _monitor_setting_int("MONITOR_DEAD_SESSION_ALERT_THRESHOLD", 5, minimum=1)
    dead_ratio_limit = _monitor_setting_float("MONITOR_DEAD_SESSION_ALERT_RATIO", 0.25, minimum=0.0)
    ok_ratio_limit = _monitor_setting_float("MONITOR_OK_WINDOW_MIN_RATIO", 0.35, minimum=0.0)

    recommended_by_due = int(math.ceil(max(1, due) * min_per_due))
    recommended_by_bloggers = int(math.ceil(bloggers * min_ratio))
    recommended = max(low_threshold, recommended_by_due, recommended_by_bloggers)
    baseline_recommended = max(low_threshold, recommended_by_bloggers)

    reasons: list[str] = []
    severity = "warning"
    suggest_add_accounts = False
    attention_kind: str | None = None

    batch_limit = _monitor_setting_int("MONITOR_MAX_BLOGGERS_PER_TICK", MAX_CREATORS_PER_TICK, minimum=1)
    backlog_attention_overdue = _monitor_setting_int("MONITOR_BACKLOG_ATTENTION_OVERDUE_MINUTES", 30, minimum=1)
    backlog_attention = due > batch_limit and oldest_overdue >= backlog_attention_overdue

    if eligible <= critical_threshold:
        severity = "critical"
        reasons.append(f"готовых monitor-аккаунтов критически мало: {eligible}")
        suggest_add_accounts = True
    elif eligible <= low_threshold:
        reasons.append(f"готовых monitor-аккаунтов мало: {eligible}")
        suggest_add_accounts = True

    if due > 0 and min_per_due > 0 and eligible < recommended_by_due and not backlog_attention:
        reasons.append(f"очередь due={due}, а готовых аккаунтов только {eligible}")
        suggest_add_accounts = True

    if total > 0 and min_ratio > 0 and eligible / max(total, 1) < min_ratio:
        reasons.append(f"готовый пул просел: {eligible}/{total}")
        suggest_add_accounts = True

    dead_ratio = dead_session / max(total, 1) if total else 0.0
    if dead_session >= dead_threshold and (total <= 0 or dead_ratio_limit <= 0 or dead_ratio >= dead_ratio_limit):
        reasons.append(f"много dead session: {dead_session}/{total}")
        if dead_session >= max(dead_threshold * 2, total // 2 if total else dead_threshold * 2):
            severity = "critical"
            suggest_add_accounts = True

    if bloggers >= 10 and ok_ratio_limit > 0 and ok_last_window / max(bloggers, 1) < ok_ratio_limit and oldest_overdue >= 2:
        if backlog_attention and eligible >= baseline_recommended:
            attention_kind = "backlog"
            recommended = baseline_recommended
            reasons.append(
                f"большая очередь проверок: due={due}, oldest_overdue={oldest_overdue}m"
            )
        else:
            reasons.append(
                f"мало успешных проверок за окно: {ok_last_window}/{bloggers}, oldest_overdue={oldest_overdue}m"
            )

    if bad_proxies > 0 and eligible <= recommended:
        reasons.append(f"есть плохие прокси в monitor-пуле: {bad_proxies}")
        suggest_add_accounts = True

    if not reasons:
        return None

    if eligible <= critical_threshold:
        severity = "critical"

    return {
        "severity": severity,
        "reasons": reasons,
        "recommended": recommended,
        "suggest_add_accounts": suggest_add_accounts,
        "attention_kind": attention_kind,
    }


def _format_monitor_pool_alert(snapshot: dict[str, Any], pressure: dict[str, Any]) -> str:
    severity = pressure.get("severity", "warning")
    attention_kind = pressure.get("attention_kind")
    if attention_kind == "backlog":
        title = "⚠️ Monitor: большая очередь проверок"
    else:
        title = "🚨 Monitor: критически мало аккаунтов" if severity == "critical" else "⚠️ Monitor: пора добавить аккаунты"
    reasons = pressure.get("reasons") or []
    reason_text = "\n".join(f"• {reason}" for reason in reasons[:6])
    recommended = int(pressure.get("recommended", 0) or 0)
    eligible = int(snapshot.get("accounts_eligible", 0) or 0)
    total = int(snapshot.get("accounts_total", 0) or 0)
    reserved = int(snapshot.get("accounts_reserved", 0) or 0)
    bloggers = int(snapshot.get("bloggers", 0) or 0)
    due = int(snapshot.get("due", 0) or 0)

    to_add = max(0, recommended - eligible)
    if attention_kind == "backlog" and not pressure.get("suggest_add_accounts"):
        add_line = "Рекомендация: аккаунты пока докупать не нужно; воркер разгребает backlog текущим пулом."
    else:
        add_line = (
            f"Рекомендация: добавить минимум {to_add} аккаунтов."
            if to_add > 0
            else "Рекомендация: пополнить запас, если планируешь добавлять блогеров."
        )

    return (
        f"{title}\n\n"
        f"{reason_text}\n\n"
        f"Сейчас:\n"
        f"• Готово: {eligible}/{total}\n"
        f"• Зарезервировано в работе: {reserved}\n"
        f"• Блогеров: {bloggers}\n"
        f"• Due backlog: {due}\n"
        f"• Dead session: {int(snapshot.get('dead_session', 0) or 0)}\n"
        f"• Bad proxy: {int(snapshot.get('bad_proxies', 0) or 0)}\n"
        f"• OK за {snapshot.get('window_minutes', 15)}м: {int(snapshot.get('ok_last_window', 0) or 0)}\n\n"
        f"{add_line}\n"
        "Новые аккаунты после импорта автоматически попадут в monitor-пул, если у них есть cookies/session и прокси "
        "соответствуют текущим настройкам."
    )


async def _maybe_send_monitor_pool_alert(snapshot: dict[str, Any]) -> None:
    pressure = _evaluate_monitor_pool_pressure(snapshot)
    if not pressure:
        return
    cooldown = _monitor_setting_int("MONITOR_ACCOUNT_ALERT_COOLDOWN_MINUTES", 120, minimum=5)
    severity = pressure.get("severity", "warning")
    await _send_admin_alert_once(
        key=f"monitor_pool:{severity}",
        message=_format_monitor_pool_alert(snapshot, pressure),
        cooldown_minutes=cooldown,
    )


_PROFILE_ACCESS_FAILURE_REASONS = {"private_profile", "profile_not_found"}


def _profile_access_alert_keyboard(blogger_id: int):
    from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

    return InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(
                text="✏️ Новый ник",
                callback_data=f"admin:adaptive:blogger:{blogger_id}:rename_prompt:0",
            ),
            InlineKeyboardButton(
                text="🗑 Удалить",
                callback_data=f"admin:adaptive:blogger:{blogger_id}:delete_confirm:0",
            ),
        ],
        [
            InlineKeyboardButton(
                text="👤 Карточка",
                callback_data=f"admin:adaptive:blogger:{blogger_id}:page:0",
            ),
            InlineKeyboardButton(
                text="🚀 Проверить",
                callback_data=f"admin:adaptive:blogger:{blogger_id}:recheck:0",
            ),
        ],
    ])


def _humanize_profile_access_reason(reason: str) -> str:
    return {
        "private_profile": "профиль стал закрытым/private",
        "profile_not_found": "TikTok возвращает, что профиль не найден",
    }.get(str(reason or ""), str(reason or "неизвестно"))


def _record_blogger_profile_access_failure_state(
    blogger: MonitoredBlogger,
    reason: str,
    *,
    now: datetime,
) -> int:
    stats = _posting_stats_dict(blogger)
    previous = stats.get("profile_access_failure")
    previous_payload = previous if isinstance(previous, dict) else {}
    streak = int(previous_payload.get("streak") or 0) + 1
    stats["profile_access_failure"] = {
        "reason": reason,
        "reason_text": _humanize_profile_access_reason(reason),
        "streak": streak,
        "first_at": previous_payload.get("first_at") or now.isoformat(),
        "last_at": now.isoformat(),
    }
    blogger.posting_stats = stats
    return streak


def _clear_blogger_profile_access_failure(blogger: MonitoredBlogger) -> None:
    stats = _posting_stats_dict(blogger)
    if "profile_access_failure" not in stats:
        return
    stats.pop("profile_access_failure", None)
    blogger.posting_stats = stats


async def _maybe_send_blogger_profile_access_alert(
    blogger: MonitoredBlogger,
    *,
    reason: str,
    streak: int,
) -> None:
    alert_after = _monitor_setting_int("BLOGGER_PROFILE_ACCESS_ALERT_AFTER", 5, minimum=1)
    if int(streak or 0) <= alert_after:
        return

    cooldown = _monitor_setting_int(
        "BLOGGER_PROFILE_ACCESS_ALERT_COOLDOWN_MINUTES",
        24 * 60,
        minimum=30,
    )
    next_check = blogger.next_check_at.isoformat() if blogger.next_check_at else "ещё не запланирована"
    reason_text = _humanize_profile_access_reason(reason)
    message = build_admin_alert(
        severity="warning",
        title="Блогер недоступен несколько проверок подряд",
        what=(
            f"Монитор {streak} раз подряд не смог нормально открыть @{blogger.username}.\n"
            f"Причина последней проверки: {reason_text}."
        ),
        why=(
            f"Уведомление отправляется только когда подряд больше {alert_after} проверок "
            "дают уверенный статус private/not found. Ошибки сети, прокси и нехватка "
            "monitor-аккаунтов сюда не считаются."
        ),
        auto_action=(
            "Софт оставил блогера в мониторинге и увеличил интервал через обычный "
            "адаптивный механизм. Автоматически блогер не удаляется."
        ),
        admin_action=(
            "Проверь ник. Если блогер сменил username — нажми «Новый ник». "
            "Если он больше не нужен — нажми «Удалить»."
        ),
        next_check=next_check,
        affected=[f"@{blogger.username}"],
    )
    record_incident(
        f"blogger_profile_access:{blogger.id}",
        severity="warning",
        title=f"Блогер @{blogger.username} недоступен",
        cause=f"{reason_text}; streak={streak}",
        evidence=[
            f"reason={reason}",
            f"streak={streak}",
            f"next_check_at={next_check}",
        ],
        auto_action="Софт продолжает наблюдать и ждёт решения администратора.",
        admin_action="Удалить блогера или указать новый актуальный ник.",
        affected=[f"@{blogger.username}"],
        ttl_minutes=cooldown,
    )
    await _send_admin_alert_once(
        key=f"profile_access:{blogger.id}",
        message=message,
        cooldown_minutes=cooldown,
        keyboard=_profile_access_alert_keyboard(blogger.id),
    )


def _reset_blogger_fail_streak(blogger_id: int) -> None:
    redis_client = _get_monitor_redis()
    if redis_client:
        try:
            redis_client.delete(f"videomonitor:blogger_fail:{blogger_id}")
        except Exception as e:
            logger.debug(f"[VIDEO_MONITOR] Failed to clear fail streak in Redis for blogger {blogger_id}: {e}")
    _blogger_fail_streak.pop(blogger_id, None)
    _reset_blogger_timeout_streak(blogger_id)


async def _record_blogger_failure(db: Session, blogger: MonitoredBlogger, reason: str) -> int:
    threshold = getattr(settings, "BLOGGER_FAILURE_ALERT_THRESHOLD", 3)
    redis_client = _get_monitor_redis()
    current = 0
    if redis_client:
        try:
            fail_key = f"videomonitor:blogger_fail:{blogger.id}"
            current = int(redis_client.incr(fail_key))
            redis_client.expire(fail_key, max(3600, threshold * 3600))
        except Exception as e:
            logger.debug(f"[VIDEO_MONITOR] Failed to increment fail streak in Redis for blogger {blogger.id}: {e}")
            current = 0
    if current <= 0:
        current = _blogger_fail_streak.get(blogger.id, 0) + 1
        _blogger_fail_streak[blogger.id] = current
    if current >= threshold:
        await _send_admin_alert_once(
            key=f"blogger_fail:{blogger.id}",
            message=build_admin_alert(
                severity="warning",
                title="Блогер регулярно не проверяется",
                what=f"@{blogger.username} получил {current} технических ошибок подряд.",
                why=(
                    "Это не считается сменой ника: сюда попадают технические причины "
                    "вроде account pool/proxy/network/parser failure."
                ),
                auto_action="Софт поставил блогера на повторную проверку с увеличенным cooldown.",
                admin_action="Если таких ошибок много по разным блогерам — проверь monitor-аккаунты и прокси.",
                affected=[f"@{blogger.username}", f"reason={reason}"],
            )
        )
    return current


def _blogger_failure_cooldown_minutes(reason: str, streak: int) -> int:
    reason_l = (reason or "").lower()
    default_cooldown = int(getattr(settings, "NO_ACCOUNTS_COOLDOWN_MINUTES", 5) or 5)
    if any(marker in reason_l for marker in ("no_accounts", "global_network")):
        return max(1, default_cooldown)

    threshold = int(getattr(settings, "BLOGGER_FAILURE_QUARANTINE_THRESHOLD", 3) or 3)
    if int(streak or 0) < threshold:
        return max(1, default_cooldown)

    base = int(getattr(settings, "BLOGGER_FAILURE_COOLDOWN_MINUTES", 45) or 45)
    if any(marker in reason_l for marker in ("timeout", "page_not_loaded", "no_video_links", "targetclosed")):
        base = max(base, 60)
    max_cooldown = int(getattr(settings, "BLOGGER_FAILURE_MAX_COOLDOWN_MINUTES", 4 * 60) or (4 * 60))
    exponent = min(4, max(0, int(streak or 0) - threshold))
    return max(1, min(max_cooldown, int(base * (2 ** exponent))))


def _schedule_failed_blogger_check(
    blogger: MonitoredBlogger,
    *,
    active_subscriptions: Iterable[CreatorSource] | None,
    now: datetime,
    reason: str,
    streak: int,
) -> datetime:
    cooldown = _blogger_failure_cooldown_minutes(reason, streak)
    next_check_at = _schedule_next_blogger_check(
        blogger,
        active_subscriptions=active_subscriptions,
        now=now,
        cooldown_minutes=cooldown,
    )
    stats = _posting_stats_dict(blogger)
    stats["monitor_failure"] = {
        "reason": reason,
        "streak": int(streak or 0),
        "cooldown_minutes": cooldown,
        "next_retry_at": next_check_at.isoformat(),
        "updated_at": now.isoformat(),
    }
    blogger.posting_stats = stats
    if int(streak or 0) >= int(getattr(settings, "BLOGGER_FAILURE_QUARANTINE_THRESHOLD", 3) or 3):
        logger.warning(
            "[VIDEO_MONITOR] Quarantined @%s for %dm after repeated monitor failures (streak=%d, reason=%s)",
            blogger.username,
            cooldown,
            int(streak or 0),
            reason,
        )
    return next_check_at


def _count_eligible_monitor_accounts(
    db: Session,
    exclude_ids: list[int] | None = None,
    exclude_proxies: set[str] | list[str] | tuple[str, ...] | None = None,
) -> int:
    auth_material = or_(
        and_(Account.cookies.isnot(None), Account.cookies != ""),
        and_(Account.cookie_file.isnot(None), Account.cookie_file != ""),
    )
    query = db.query(Account).filter(
        Account.status.in_(["active", "error"]),
        Account.error_count_recent < settings.ERROR_THRESHOLD,
        auth_material,
    )
    if settings.USE_PROXY:
        query = query.filter(
            Account.proxy.isnot(None),
            Account.proxy != ""
        )
    if exclude_ids:
        query = query.filter(~Account.id.in_(exclude_ids))
    normalized_exclude_proxies = {
        str(proxy).strip()
        for proxy in (exclude_proxies or [])
        if proxy and str(proxy).strip()
    }
    if normalized_exclude_proxies:
        query = query.filter(~Account.proxy.in_(sorted(normalized_exclude_proxies)))
    if not settings.USE_PROXY:
        return query.count()
    try:
        candidates = query.with_entities(Account.proxy).all()
        return sum(
            1
            for (proxy,) in candidates
            if proxy
            and str(proxy).strip() not in normalized_exclude_proxies
            and not is_proxy_bad(proxy)
        )
    except Exception:
        return query.count()


def _load_existing_video_ids(
    db: Session,
    creator_id: int,
    video_ids: list[str],
) -> set[str]:
    """
    Ð—Ð°Ð³Ñ€ÑƒÐ¶Ð°ÐµÑ‚ ÑƒÐ¶Ðµ ÑÑƒÑ‰ÐµÑÑ‚Ð²ÑƒÑŽÑ‰Ð¸Ðµ video_id Ð´Ð»Ñ ÐºÐ¾Ð½ÐºÑ€ÐµÑ‚Ð½Ð¾Ð¹ Ð¿Ð¾Ð´Ð¿Ð¸ÑÐºÐ¸ Ð¾Ð´Ð½Ð¸Ð¼ Ð·Ð°Ð¿Ñ€Ð¾ÑÐ¾Ð¼.

    WHY: Ð¿Ñ€ÐµÐ¶Ð½Ð¸Ð¹ ÐºÐ¾Ð´ Ð´ÐµÐ»Ð°Ð» SELECT Ð½Ð° ÐºÐ°Ð¶Ð´Ð¾Ðµ Ð²Ð¸Ð´ÐµÐ¾ Ð¾Ñ‚Ð´ÐµÐ»ÑŒÐ½Ð¾. ÐÐ° Ð´Ð»Ð¸Ð½Ð½Ñ‹Ñ… Ð¿Ð°Ñ‡ÐºÐ°Ñ…
    Ð½Ð¾Ð²Ñ‹Ñ… Ñ€Ð¾Ð»Ð¸ÐºÐ¾Ð² Ð¸ Ð½ÐµÑÐºÐ¾Ð»ÑŒÐºÐ¸Ñ… Ð¿Ð¾Ð´Ð¿Ð¸ÑÐºÐ°Ñ… ÑÑ‚Ð¾ Ñ€Ð°Ð·Ð´ÑƒÐ²Ð°Ð»Ð¾ Ñ‡Ð¸ÑÐ»Ð¾ Ð·Ð°Ð¿Ñ€Ð¾ÑÐ¾Ð² Ð±ÐµÐ· Ð¿Ð¾Ð»ÑŒÐ·Ñ‹.
    """
    if not video_ids:
        return set()

    rows = (
        db.query(CreatorVideo.video_id)
        .filter(
            CreatorVideo.creator_id == creator_id,
            CreatorVideo.video_id.in_(video_ids),
        )
        .all()
    )
    return {row[0] for row in rows if row and row[0]}


def _make_lock_owner(prefix: str, entity_id: int) -> str:
    return f"{prefix}:{socket.gethostname()}:{os.getpid()}:{entity_id}:{int(time.time() * 1000)}"


def _get_active_user_ids(db: Session, now: datetime) -> set[int]:
    active_user_ids: set[int] = set()

    free_rows = db.query(User.id).filter(User.is_blocked == False).all()
    active_user_ids.update([row[0] for row in free_rows])

    if settings.OWNER_TELEGRAM_ID:
        owner_row = db.query(User.id).filter(User.telegram_id == settings.OWNER_TELEGRAM_ID).first()
        if owner_row:
            active_user_ids.add(owner_row[0])

    subs_rows = (
        db.query(Subscription.user_id)
        .join(User, User.id == Subscription.user_id)
        .filter(Subscription.expire_at > now, User.is_blocked == False)
        .all()
    )
    active_user_ids.update([row[0] for row in subs_rows])
    return active_user_ids


def _get_active_subscriptions_for_blogger(
    blogger: MonitoredBlogger,
    active_user_ids: set[int] | None,
) -> list[CreatorSource]:
    subscriptions = list(blogger.subscriptions or [])
    if settings.DEMO_MODE:
        return [sub for sub in subscriptions if sub.enabled]

    if not active_user_ids:
        return []

    return [
        sub
        for sub in subscriptions
        if sub.enabled and sub.user_id in active_user_ids
    ]


def _get_monitor_interval_minutes(active_subscriptions: Iterable[CreatorSource]) -> int:
    subscriptions = [sub for sub in active_subscriptions if getattr(sub, "enabled", True)]
    if not subscriptions:
        return 30

    shared_min_subs = getattr(settings, "SHARED_BLOGGER_MIN_SUBS", 2)
    shared_min_interval = getattr(
        settings,
        "SHARED_BLOGGER_MIN_INTERVAL_MINUTES",
        getattr(settings, "SHARED_BLOGGER_INTERVAL_MINUTES", 15),
    )
    requested_interval = min(max(1, int(sub.interval_minutes or 30)) for sub in subscriptions)
    if len(subscriptions) >= shared_min_subs:
        return max(requested_interval, max(1, int(shared_min_interval or 15)))
    return requested_interval


def _coerce_unix_ts(value: Any) -> int | None:
    try:
        ts = int(value)
    except (TypeError, ValueError):
        return None
    return ts if ts > 0 else None


def _clamp_minutes(value: int, lo: int, hi: int) -> int:
    lo = max(1, int(lo))
    hi = max(lo, int(hi))
    return max(lo, min(int(value), hi))


def _load_adaptive_runtime_settings(db: Session) -> tuple[bool, AdaptiveSettings, dict[str, Any]]:
    raw = get_all_adaptive_settings(db)
    adaptive_fields = {field.name for field in dataclasses.fields(AdaptiveSettings)}
    adaptive_kwargs = {key: value for key, value in raw.items() if key in adaptive_fields}
    return bool(raw.get("adaptive_enabled")), AdaptiveSettings(**adaptive_kwargs), raw


def _posting_stats_dict(blogger: MonitoredBlogger) -> dict[str, Any]:
    stats = blogger.posting_stats or {}
    return dict(stats) if isinstance(stats, dict) else {}


def _existing_video_timestamps(blogger: MonitoredBlogger) -> list[int]:
    values = _posting_stats_dict(blogger).get("video_timestamps") or []
    timestamps: list[int] = []
    for value in values:
        ts = _coerce_unix_ts(value)
        if ts is not None:
            timestamps.append(ts)
    return timestamps


def _scan_video_timestamps(scan_result: ScanResult | None) -> list[int]:
    if not scan_result:
        return []

    timestamps: list[int] = []
    for value in getattr(scan_result, "video_create_times", None) or []:
        ts = _coerce_unix_ts(value)
        if ts is not None:
            timestamps.append(ts)

    # Fallback for callers that only have the id -> create_time map populated.
    if not timestamps:
        for value in (getattr(scan_result, "video_create_times_map", None) or {}).values():
            ts = _coerce_unix_ts(value)
            if ts is not None:
                timestamps.append(ts)

    return timestamps


def _video_create_time(scan_result: ScanResult | None, video_id: str) -> int | None:
    if not scan_result:
        return None
    create_times_map = getattr(scan_result, "video_create_times_map", None) or {}
    return _coerce_unix_ts(create_times_map.get(video_id))


def _percentile_int(values: list[int], pct: float) -> int | None:
    if not values:
        return None
    sorted_values = sorted(values)
    if len(sorted_values) == 1:
        return sorted_values[0]
    idx = (len(sorted_values) - 1) * pct / 100.0
    lo_i = int(idx)
    hi_i = min(lo_i + 1, len(sorted_values) - 1)
    frac = idx - lo_i
    return int(round(sorted_values[lo_i] * (1.0 - frac) + sorted_values[hi_i] * frac))


def _latency_stats_from_values(latencies: list[int]) -> dict[str, Any]:
    if not latencies:
        return {"count": 0}

    latencies = sorted(int(v) for v in latencies)
    return {
        "count": len(latencies),
        "samples": latencies,
        "min": latencies[0],
        "avg": round(sum(latencies) / len(latencies), 1),
        "p50": _percentile_int(latencies, 50),
        "p75": _percentile_int(latencies, 75),
        "p90": _percentile_int(latencies, 90),
        "max": latencies[-1],
    }


def _video_detection_latency_stats(
    scan_result: ScanResult | None,
    videos: Iterable[tuple[str, str]] | None,
    now_ts: int,
) -> dict[str, Any]:
    latencies: list[int] = []
    for video_id, _url in videos or []:
        create_time = _video_create_time(scan_result, video_id)
        if create_time is None:
            continue
        latencies.append(max(0, int((now_ts - create_time) // 60)))
    return _latency_stats_from_values(latencies)


def _video_planned_detection_latency_stats(
    scan_result: ScanResult | None,
    videos: Iterable[tuple[str, str]] | None,
    planned_check_at: datetime | None,
) -> dict[str, Any]:
    planned_dt = _ensure_utc_datetime(planned_check_at)
    if planned_dt is None:
        return {"count": 0, "basis": "planned_check_at", "skipped_after_planned": 0}

    planned_ts = int(planned_dt.timestamp())
    latencies: list[int] = []
    skipped_after_planned = 0
    for video_id, _url in videos or []:
        create_time = _video_create_time(scan_result, video_id)
        if create_time is None:
            continue
        if create_time > planned_ts:
            skipped_after_planned += 1
            continue
        latencies.append(max(0, int((planned_ts - create_time) // 60)))

    stats = _latency_stats_from_values(latencies)
    stats["basis"] = "planned_check_at"
    stats["planned_check_at"] = planned_dt.isoformat()
    stats["skipped_after_planned"] = skipped_after_planned
    return stats


def _latency_stats_public_summary(stats: dict[str, Any]) -> dict[str, Any]:
    return {key: value for key, value in stats.items() if key != "samples"}


def _latency_p75(stats: dict[str, Any]) -> int | None:
    return stats.get("p75") if stats.get("count") else None


def _detection_latency_minutes(
    scan_result: ScanResult | None,
    videos: Iterable[tuple[str, str]] | None,
    now_ts: int,
) -> int | None:
    stats = _video_detection_latency_stats(scan_result, videos, now_ts)
    return stats.get("p75") if stats.get("count") else None


def _build_video_analytics_meta(
    *,
    blogger: MonitoredBlogger,
    scan_result: ScanResult | None,
    video_id: str,
    detected_at: datetime,
    batch_size: int,
    batch_index: int,
) -> dict[str, Any]:
    detected_ts = int(detected_at.timestamp())
    create_time = _video_create_time(scan_result, video_id)
    meta: dict[str, Any] = {
        "source": "unified_parser",
        "blogger_id": blogger.id,
        "detected_at": detected_at.isoformat(),
        "batch_size": batch_size,
        "batch_index": batch_index,
    }
    if scan_result is not None:
        meta["scan_status"] = scan_result.status
        meta["canonical_source"] = scan_result.canonical_source
        meta["pages_loaded"] = scan_result.pages_loaded
    if create_time is not None:
        latency_min = max(0, int((detected_ts - create_time) // 60))
        meta["create_time"] = create_time
        meta["detection_latency_min"] = latency_min
        meta["age_at_detection_hours"] = round(latency_min / 60.0, 2)
    return meta


def _update_latency_feedback(
    blogger: MonitoredBlogger,
    *,
    found_new_count: int,
    detection_latency_min: int | None,
    latency_stats: dict[str, Any] | None,
    adaptive_config: dict[str, Any],
    feedback_reliable: bool,
) -> None:
    if found_new_count > 0:
        blogger.total_videos_caught = int(blogger.total_videos_caught or 0) + int(found_new_count)

    if detection_latency_min is None:
        return

    if not feedback_reliable:
        return

    blogger.last_detection_latency_min = int(detection_latency_min)
    alpha = float(adaptive_config.get("feedback_ema_alpha", 0.3) or 0.3)
    alpha = max(0.05, min(alpha, 1.0))
    
    # P2: Bounded Penalty for EMA.
    # Предотвращаем вечную порчу EMA при огромных спайках (например, сервис не работал 3 дня).
    # Ограничиваем штраф на уровне max_interval * 2.
    max_penalty_minutes = int(adaptive_config.get("max_interval_minutes", 720) or 720) * 2
    clamped_latency = float(min(detection_latency_min, max_penalty_minutes))
    
    previous_ema = blogger.latency_ema_minutes
    if previous_ema is None:
        blogger.latency_ema_minutes = clamped_latency
    else:
        blogger.latency_ema_minutes = float(previous_ema) * (1.0 - alpha) + clamped_latency * alpha

    target = max(1, int(adaptive_config.get("feedback_target_latency_min", 10) or 10))
    late_threshold = max(1.0, float(adaptive_config.get("feedback_late_threshold", 2.0) or 2.0))
    late_border = target * late_threshold
    if latency_stats and latency_stats.get("count"):
        # Count late videos, not just late checks. If only aggregate stats are
        # available, max/p75 still prevents a single fresh video from hiding a
        # batch that was mostly discovered late.
        samples = [int(v) for v in latency_stats.get("samples") or []]
        late_count = sum(1 for value in samples if value > late_border)
        if late_count <= 0 and (latency_stats.get("p75") or 0) > late_border:
            late_count = max(1, int(round((latency_stats.get("count") or 1) * 0.75)))
        blogger.total_late_catches = int(blogger.total_late_catches or 0) + late_count
    elif detection_latency_min > late_border:
        blogger.total_late_catches = int(blogger.total_late_catches or 0) + 1


def _adjust_adaptive_interval(
    interval_minutes: int,
    *,
    blogger: MonitoredBlogger,
    adaptive_settings: AdaptiveSettings,
    adaptive_config: dict[str, Any],
    apply_feedback: bool,
    allow_sanity_cap: bool = False,
) -> tuple[int, list[str]]:
    interval = max(1, int(interval_minutes))
    notes: list[str] = []

    priority = (blogger.priority_mode or "auto").strip().lower()
    if priority == "vip":
        interval = max(1, int(round(interval * 0.5)))
        notes.append("priority=vip x0.50")
    elif priority == "lazy":
        interval = max(1, int(round(interval * 2.0)))
        notes.append("priority=lazy x2.00")

    if apply_feedback and adaptive_config.get("feedback_enabled", True):
        latency_ema = blogger.latency_ema_minutes
        if latency_ema is not None and latency_ema > 0:
            target = max(1.0, float(adaptive_config.get("feedback_target_latency_min", 10) or 10))
            min_factor = max(0.1, float(adaptive_config.get("feedback_min_factor", 0.5) or 0.5))
            max_factor = max(min_factor, float(adaptive_config.get("feedback_max_factor", 2.0) or 2.0))
            factor = max(min_factor, min(max_factor, target / max(float(latency_ema), 1.0)))
            if abs(factor - 1.0) >= 0.05:
                interval = max(1, int(round(interval * factor)))
                notes.append(f"feedback x{factor:.2f} (latency_ema={latency_ema:.0f}m)")

    lo = adaptive_settings.min_interval_minutes
    hi = (
        int(adaptive_settings.max_interval_minutes * adaptive_settings.sanity_cap_factor)
        if allow_sanity_cap
        else adaptive_settings.max_interval_minutes
    )
    if blogger.override_min_interval is not None:
        lo = int(blogger.override_min_interval)
    if blogger.override_max_interval is not None:
        hi = int(blogger.override_max_interval)
    clamped = _clamp_minutes(interval, lo, hi)
    if clamped != interval:
        notes.append(f"clamped {max(1, int(lo))}-{max(max(1, int(lo)), int(hi))}m")
    return clamped, notes


def _feedback_reliability_for_check(
    blogger: MonitoredBlogger,
    *,
    now: datetime,
) -> tuple[bool, str | None]:
    previous_next_check_at = _ensure_utc_datetime(blogger.next_check_at)
    if previous_next_check_at is None:
        return False, "feedback skipped: first check"
    if previous_next_check_at > now + timedelta(minutes=1):
        return False, "feedback skipped: checked before planned time"

    return True, None


def _adaptive_schedule_ceiling_minutes(blogger: MonitoredBlogger, adaptive_settings: AdaptiveSettings) -> int:
    configured_max = adaptive_settings.max_interval_minutes
    if blogger.override_max_interval is not None:
        configured_max = int(blogger.override_max_interval)
    return max(1, int(configured_max * 1.5))


def _repair_far_future_blogger_checks(
    db: Session,
    *,
    now: datetime,
    adaptive_settings: AdaptiveSettings,
) -> int:
    ceiling_minutes = max(1, int(adaptive_settings.max_interval_minutes * 1.5))
    cutoff = now + timedelta(minutes=ceiling_minutes)
    repaired_to = now + timedelta(minutes=max(1, int(adaptive_settings.max_interval_minutes)))
    repaired = (
        db.query(MonitoredBlogger)
        .filter(MonitoredBlogger.next_check_at.isnot(None))
        .filter(MonitoredBlogger.next_check_at > cutoff)
        .filter(or_(MonitoredBlogger.priority_mode.is_(None), MonitoredBlogger.priority_mode != "paused"))
        .update(
            {MonitoredBlogger.next_check_at: repaired_to},
            synchronize_session=False,
        )
    )
    return int(repaired or 0)


_DERIVED_POSTING_STATS_KEYS = {
    "median_gap_hours",
    "recent_gap_hours",
    "last_post_age_hours",
    "active_hours",
    "hour_histogram",
    "dow_histogram",
    "active_days",
    "is_burst",
    "predicted_next_post_ts",
    "predicted_confidence",
    "prediction_used",
    "cadence_type",
    "gap_cv",
}


def _repair_orphan_adaptive_stats(
    db: Session,
    *,
    now: datetime,
    adaptive_settings: AdaptiveSettings,
) -> int:
    repaired = 0
    bloggers = (
        db.query(MonitoredBlogger)
        .options(selectinload(MonitoredBlogger.subscriptions))
        .filter(MonitoredBlogger.posting_stats.isnot(None))
        .all()
    )
    for blogger in bloggers:
        stats = blogger.posting_stats if isinstance(blogger.posting_stats, dict) else {}
        timestamps = stats.get("video_timestamps") or []
        if timestamps or not any(key in stats for key in _DERIVED_POSTING_STATS_KEYS):
            continue
        manual_interval = _get_monitor_interval_minutes(blogger.subscriptions)
        blogger.posting_stats = {
            "schema_version": SCHEMA_VERSION,
            "video_timestamps": [],
            "status": "calibrating",
            "post_count_analyzed": 0,
            "next_interval_minutes": manual_interval,
            "reason": f"calibrating: 0/{adaptive_settings.min_samples} samples, cleared stale derived stats",
            "computed_at": now.isoformat(),
        }
        repaired += 1
    return repaired


def _recompute_stale_adaptive_stats(
    db: Session,
    *,
    now: datetime,
    adaptive_settings: AdaptiveSettings,
) -> int:
    now_ts = int(now.timestamp())
    recomputed = 0
    bloggers = (
        db.query(MonitoredBlogger)
        .filter(MonitoredBlogger.posting_stats.isnot(None))
        .all()
    )
    for blogger in bloggers:
        stats = blogger.posting_stats if isinstance(blogger.posting_stats, dict) else {}
        reason = str(stats.get("reason") or "")
        needs_recompute = stats.get("schema_version") != SCHEMA_VERSION or "feedback " in reason
        if not needs_recompute:
            continue
        timestamps = merge_timestamps(
            [],
            stats.get("video_timestamps") or [],
            max_keep=adaptive_settings.max_keep,
            max_age_days=adaptive_settings.max_history_days,
            now_ts=now_ts,
        )
        if not timestamps:
            continue
        result = compute_adaptive_interval(
            timestamps,
            now_ts,
            adaptive_settings,
            found_new=False,
            empty_streak=int(blogger.empty_streak or 0),
        )
        if result is None:
            continue
        updated_stats = dataclasses.asdict(result)
        updated_stats["video_timestamps"] = timestamps
        blogger.posting_stats = updated_stats
        recomputed += 1
    return recomputed


def _record_blogger_check_log(
    db: Session,
    *,
    blogger: MonitoredBlogger,
    checked_at: datetime,
    found_new_count: int,
    interval_used_minutes: int | None,
    next_interval_minutes: int | None,
    status: str | None,
    adaptive_status: str | None,
    reason: str | None,
    duration_ms: int | None,
    detection_latency_min: int | None,
) -> None:
    db.add(
        BloggerCheckLog(
            blogger_id=blogger.id,
            checked_at=checked_at,
            found_new_count=max(0, int(found_new_count or 0)),
            interval_used_minutes=interval_used_minutes,
            next_interval_minutes=next_interval_minutes,
            status=status,
            adaptive_status=adaptive_status,
            reason=reason,
            duration_ms=duration_ms,
            detection_latency_min=detection_latency_min,
        )
    )


def _cleanup_old_blogger_check_logs(db: Session, *, now: datetime, retention_days: int | None) -> None:
    if not retention_days:
        return
    try:
        days = max(1, int(retention_days))
    except (TypeError, ValueError):
        return
    cutoff = now - timedelta(days=days)
    db.execute(delete(BloggerCheckLog).where(BloggerCheckLog.checked_at < cutoff))


def _schedule_next_blogger_check_from_scan(
    db: Session,
    blogger: MonitoredBlogger,
    *,
    active_subscriptions: Iterable[CreatorSource] | None,
    now: datetime,
    scan_result: ScanResult | None,
    found_new_count: int,
    status: str | None,
    duration_ms: int | None = None,
    videos: Iterable[tuple[str, str]] | None = None,
    found_new_for_adaptive: bool | None = None,
    count_empty_streak: bool = True,
) -> datetime:
    subscriptions = list(active_subscriptions or [])
    video_items = list(videos or [])
    manual_interval = _get_monitor_interval_minutes(subscriptions)
    previous_stats = _posting_stats_dict(blogger)
    planned_check_at = _ensure_utc_datetime(blogger.next_check_at)
    interval_used = previous_stats.get("next_interval_minutes") or manual_interval
    try:
        interval_used_int = int(interval_used)
    except (TypeError, ValueError):
        interval_used_int = int(manual_interval)

    if found_new_count > 0:
        blogger.empty_streak = 0
    elif count_empty_streak:
        blogger.empty_streak = int(blogger.empty_streak or 0) + 1

    now_ts = int(now.timestamp())
    observed_latency_stats = _video_detection_latency_stats(scan_result, video_items, now_ts)
    planned_latency_stats = _video_planned_detection_latency_stats(
        scan_result,
        video_items,
        planned_check_at,
    )
    detection_latency_min = _latency_p75(planned_latency_stats)

    try:
        adaptive_enabled, adaptive_settings, adaptive_config = _load_adaptive_runtime_settings(db)
    except Exception as exc:
        adaptive_enabled = False
        adaptive_settings = AdaptiveSettings()
        adaptive_config = {}
        logger.warning("[ADAPTIVE] failed to load settings, using manual interval for @%s: %s", blogger.username, exc)

    feedback_reliable, feedback_skip_note = _feedback_reliability_for_check(
        blogger,
        now=now,
    )
    if (
        feedback_reliable
        and observed_latency_stats.get("count")
        and not planned_latency_stats.get("count")
    ):
        feedback_reliable = False
        feedback_skip_note = "feedback skipped: videos published after planned check"

    _update_latency_feedback(
        blogger,
        found_new_count=found_new_count,
        detection_latency_min=detection_latency_min,
        latency_stats=planned_latency_stats,
        adaptive_config=adaptive_config,
        feedback_reliable=feedback_reliable,
    )

    merged_timestamps = merge_timestamps(
        _existing_video_timestamps(blogger),
        _scan_video_timestamps(scan_result),
        max_keep=adaptive_settings.max_keep,
        max_age_days=adaptive_settings.max_history_days,
        now_ts=now_ts,
    )

    interval_minutes = manual_interval
    adaptive_status: str | None = None
    reason: str | None = None
    stats_to_store: dict[str, Any] | None = None

    if adaptive_enabled:
        adaptive_found_new = bool(found_new_count > 0) if found_new_for_adaptive is None else bool(found_new_for_adaptive)
        result = compute_adaptive_interval(
            merged_timestamps,
            now_ts,
            adaptive_settings,
            found_new=adaptive_found_new,
            empty_streak=int(blogger.empty_streak or 0),
        )
        if result is None:
            adaptive_status = "calibrating"
            reason = f"calibrating: {len(merged_timestamps)}/{adaptive_settings.min_samples} samples, fallback {manual_interval}m"
            stats_to_store = {
                "schema_version": SCHEMA_VERSION,
                "video_timestamps": merged_timestamps,
                "status": adaptive_status,
                "post_count_analyzed": len(merged_timestamps),
                "next_interval_minutes": manual_interval,
                "reason": reason,
                "computed_at": now.isoformat(),
            }
        else:
            adaptive_status = result.status
            reason = result.reason
            interval_minutes = int(result.next_interval_minutes)
            stats_to_store = dataclasses.asdict(result)
            stats_to_store["video_timestamps"] = merged_timestamps

        interval_minutes, notes = _adjust_adaptive_interval(
            interval_minutes,
            blogger=blogger,
            adaptive_settings=adaptive_settings,
            adaptive_config=adaptive_config,
            apply_feedback=result is not None and feedback_reliable and bool(planned_latency_stats.get("count")),
            allow_sanity_cap=(
                result is not None
                and bool(getattr(result, "prediction_used", False))
                and interval_minutes > adaptive_settings.max_interval_minutes
            ),
        )
        if feedback_skip_note and result is not None and observed_latency_stats.get("count"):
            notes.append(feedback_skip_note)
        if notes:
            reason = f"{reason} + {' + '.join(notes)}" if reason else " + ".join(notes)
        schedule_ceiling = _adaptive_schedule_ceiling_minutes(blogger, adaptive_settings)
        if interval_minutes > schedule_ceiling:
            interval_minutes = schedule_ceiling
            reason = f"{reason} + schedule_ceiling={schedule_ceiling}m" if reason else f"schedule_ceiling={schedule_ceiling}m"
        if stats_to_store is not None:
            stats_to_store["next_interval_minutes"] = interval_minutes
            stats_to_store["reason"] = reason
            if feedback_reliable and planned_latency_stats.get("count"):
                stats_to_store["last_batch_latency"] = _latency_stats_public_summary(planned_latency_stats)
            if observed_latency_stats.get("count"):
                stats_to_store["last_observed_batch_latency"] = _latency_stats_public_summary(observed_latency_stats)
            blogger.posting_stats = stats_to_store
    elif merged_timestamps:
        blogger.posting_stats = {
            **previous_stats,
            "video_timestamps": merged_timestamps,
        }

    next_check_at = now + timedelta(minutes=max(1, int(interval_minutes)))
    blogger.next_check_at = next_check_at
    _record_blogger_check_log(
        db,
        blogger=blogger,
        checked_at=now,
        found_new_count=found_new_count,
        interval_used_minutes=interval_used_int,
        next_interval_minutes=int(interval_minutes),
        status=status,
        adaptive_status=adaptive_status,
        reason=reason,
        duration_ms=duration_ms,
        detection_latency_min=detection_latency_min if feedback_reliable else None,
    )
    _cleanup_old_blogger_check_logs(
        db,
        now=now,
        retention_days=adaptive_config.get("check_log_retention_days"),
    )

    # Auto-archive suggestion: блогер давно ничего не постил → подсказать админу архивировать.
    # Алерт идёт через outbox с ключом-cooldown, не дёргает админа чаще раза в неделю.
    try:
        last_post_age_hours = None
        ps_check = blogger.posting_stats or {}
        if isinstance(ps_check, dict):
            last_post_age_hours = ps_check.get("last_post_age_hours")
        threshold_hours = SILENT_BLOGGER_ARCHIVE_DAYS * 24
        if (
            isinstance(last_post_age_hours, (int, float))
            and last_post_age_hours >= threshold_hours
            and any(getattr(sub, "enabled", False) for sub in (active_subscriptions or []))
        ):
            asyncio.create_task(_send_admin_alert_once(
                key=f"silent_archive:{blogger.id}",
                message=(
                    f"🗄 Блогер @{blogger.username} молчит уже {int(last_post_age_hours / 24)} дн. "
                    f"Возможно стоит архивировать (priority=lazy/paused) — освободит ресурсы для активных."
                ),
                cooldown_minutes=SILENT_BLOGGER_ALERT_COOLDOWN_HOURS * 60,
            ))
    except Exception:
        pass

    return next_check_at


def _apply_blogger_scope_filters(query, *, active_user_ids: set[int] | None):
    if settings.DEMO_MODE:
        return query.filter(
            MonitoredBlogger.subscriptions.any(CreatorSource.enabled == True)
        )

    if active_user_ids:
        return query.filter(
            MonitoredBlogger.subscriptions.any(
                and_(
                    CreatorSource.enabled == True,
                    CreatorSource.user_id.in_(active_user_ids),
                )
            )
        )

    return query.filter(False)


def _schedule_next_blogger_check(
    blogger: MonitoredBlogger,
    *,
    active_subscriptions: Iterable[CreatorSource] | None,
    now: datetime,
    cooldown_minutes: int | None = None,
) -> datetime:
    interval_minutes = cooldown_minutes or _get_monitor_interval_minutes(active_subscriptions or [])
    next_check_at = now + timedelta(minutes=max(1, int(interval_minutes)))
    blogger.next_check_at = next_check_at
    return next_check_at


def _ensure_utc_datetime(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _build_due_bloggers_query(
    db: Session,
    *,
    now: datetime,
    active_user_ids: set[int] | None,
):
    query = db.query(MonitoredBlogger).options(selectinload(MonitoredBlogger.subscriptions))
    query = _apply_blogger_scope_filters(query, active_user_ids=active_user_ids)

    query = query.filter(
        or_(MonitoredBlogger.next_check_at.is_(None), MonitoredBlogger.next_check_at <= now)
    ).filter(
        or_(MonitoredBlogger.priority_mode.is_(None), MonitoredBlogger.priority_mode != "paused")
    ).order_by(
        MonitoredBlogger.next_check_at.asc().nulls_first(),
        MonitoredBlogger.last_attempt_at.asc().nulls_first(),
        MonitoredBlogger.last_success_at.asc().nulls_first(),
        MonitoredBlogger.updated_at.asc(),
        MonitoredBlogger.id.asc(),
    )
    return query


def get_monitor_runtime_snapshot(
    db: Session,
    *,
    now: datetime,
    active_user_ids: set[int] | None,
) -> dict[str, Any]:
    base_query = _apply_blogger_scope_filters(
        db.query(MonitoredBlogger),
        active_user_ids=active_user_ids,
    )
    due_filter = or_(MonitoredBlogger.next_check_at.is_(None), MonitoredBlogger.next_check_at <= now)
    due_query = base_query.filter(due_filter)

    due_backlog = due_query.count()
    unscheduled_due = base_query.filter(MonitoredBlogger.next_check_at.is_(None)).count()
    never_successful_due = due_query.filter(MonitoredBlogger.last_success_at.is_(None)).count()
    oldest_due_at = _ensure_utc_datetime(
        base_query.with_entities(func.min(MonitoredBlogger.next_check_at))
        .filter(
            MonitoredBlogger.next_check_at.isnot(None),
            MonitoredBlogger.next_check_at <= now,
        )
        .scalar()
    )
    next_due_at = _ensure_utc_datetime(
        base_query.with_entities(func.min(MonitoredBlogger.next_check_at))
        .filter(MonitoredBlogger.next_check_at.isnot(None), MonitoredBlogger.next_check_at > now)
        .scalar()
    )
    oldest_overdue_minutes = (
        max(0, int((now - oldest_due_at).total_seconds() // 60))
        if oldest_due_at
        else 0
    )
    next_due_in_minutes = (
        max(0, int((next_due_at - now).total_seconds() // 60))
        if next_due_at
        else None
    )
    preview_rows = (
        _build_due_bloggers_query(db, now=now, active_user_ids=active_user_ids)
        .with_entities(MonitoredBlogger.username)
        .limit(3)
        .all()
    )
    outbox_rows = (
        db.query(MonitorOutbox.status, func.count(MonitorOutbox.id))
        .group_by(MonitorOutbox.status)
        .all()
    )
    outbox_statuses = {status: int(count or 0) for status, count in outbox_rows}
    outbox_ready_filter = and_(
        MonitorOutbox.status.in_(["pending", "failed"]),
        or_(MonitorOutbox.available_at.is_(None), MonitorOutbox.available_at <= now),
    )
    outbox_ready = db.query(MonitorOutbox.id).filter(outbox_ready_filter).count()
    oldest_outbox_ready_at = _ensure_utc_datetime(
        db.query(func.min(MonitorOutbox.available_at))
        .filter(outbox_ready_filter, MonitorOutbox.available_at.isnot(None))
        .scalar()
    )
    oldest_outbox_ready_minutes = (
        max(0, int((now - oldest_outbox_ready_at).total_seconds() // 60))
        if oldest_outbox_ready_at
        else 0
    )

    return {
        "due_backlog": int(due_backlog or 0),
        "unscheduled_due": int(unscheduled_due or 0),
        "never_successful_due": int(never_successful_due or 0),
        "oldest_overdue_minutes": int(oldest_overdue_minutes or 0),
        "next_due_in_minutes": next_due_in_minutes,
        "due_preview": [row[0] for row in preview_rows],
        "outbox_ready": int(outbox_ready or 0),
        "outbox_pending": int(outbox_statuses.get("pending", 0) or 0),
        "outbox_failed": int(outbox_statuses.get("failed", 0) or 0),
        "outbox_processing": int(outbox_statuses.get("processing", 0) or 0),
        "oldest_outbox_ready_minutes": int(oldest_outbox_ready_minutes or 0),
    }


def get_monitor_health_snapshot(
    db: Session,
    *,
    now: datetime,
    active_user_ids: set[int] | None,
    runtime_snapshot: dict[str, Any],
) -> dict[str, Any]:
    window_minutes = int(getattr(settings, "MONITOR_HEALTH_WINDOW_MINUTES", 15))
    window_start = now - timedelta(minutes=max(1, window_minutes))
    base_query = _apply_blogger_scope_filters(
        db.query(MonitoredBlogger),
        active_user_ids=active_user_ids,
    )
    pool_stats = get_monitor_pool_stats(db)
    active_bloggers = base_query.count()
    ok_last_window = base_query.filter(MonitoredBlogger.last_success_at >= window_start).count()
    new_videos_last_window = (
        db.query(CreatorVideo.id)
        .filter(CreatorVideo.discovered_at >= window_start)
        .count()
    )
    adaptive_status_counts: dict[str, int] = {}
    adaptive_intervals: list[int] = []
    for b in base_query.all():
        stats = b.posting_stats or {}
        status = stats.get("status") or "calibrating"
        adaptive_status_counts[status] = adaptive_status_counts.get(status, 0) + 1
        interval = stats.get("next_interval_minutes")
        if interval is not None:
            adaptive_intervals.append(int(interval))
    avg_interval_minutes = round(sum(adaptive_intervals) / len(adaptive_intervals)) if adaptive_intervals else 0
    return {
        "window_minutes": window_minutes,
        "bloggers": int(active_bloggers or 0),
        "due": int(runtime_snapshot.get("due_backlog", 0) or 0),
        "ok_last_window": int(ok_last_window or 0),
        "new_videos": int(new_videos_last_window or 0),
        "accounts_eligible": int(pool_stats.get("eligible_now", 0) or 0),
        "accounts_eligible_including_reserved": int(pool_stats.get("eligible_including_reserved", 0) or 0),
        "accounts_reserved": int(pool_stats.get("reserved", 0) or 0),
        "accounts_total": int(pool_stats.get("total_candidates", 0) or 0),
        "bad_proxies": int(pool_stats.get("bad_proxies", 0) or 0),
        "dead_session": int(pool_stats.get("dead_session", 0) or 0),
        "outbox_pending": int(runtime_snapshot.get("outbox_pending", 0) or 0),
        "outbox_ready": int(runtime_snapshot.get("outbox_ready", 0) or 0),
        "outbox_failed": int(runtime_snapshot.get("outbox_failed", 0) or 0),
        "oldest_overdue_minutes": int(runtime_snapshot.get("oldest_overdue_minutes", 0) or 0),
        "avg_interval_minutes": avg_interval_minutes,
        "adaptive_status_counts": adaptive_status_counts,
    }


def _format_monitor_health_summary(snapshot: dict[str, Any]) -> str:
    avg_interval = int(snapshot.get("avg_interval_minutes", 0) or snapshot.get("avg_interval", 0) or 0)
    adaptive_counts = snapshot.get("adaptive_status_counts") or snapshot.get("by_adaptive_status") or {}
    if isinstance(adaptive_counts, dict) and adaptive_counts:
        adaptive_str = ",".join(f"{key}:{value}" for key, value in sorted(adaptive_counts.items()))
    else:
        adaptive_str = "?"
    return (
        "[MONITOR_HEALTH] "
        f"bloggers={snapshot['bloggers']} "
        f"due={snapshot['due']} "
        f"ok_last_{snapshot['window_minutes']}m={snapshot['ok_last_window']} "
        f"new_videos={snapshot['new_videos']} "
        f"avg_interval={avg_interval}m "
        f"adaptive=[{adaptive_str}] "
        f"accounts={snapshot['accounts_eligible']}/{snapshot['accounts_total']} "
        f"reserved={snapshot.get('accounts_reserved', 0)} "
        f"bad_proxy={snapshot['bad_proxies']} "
        f"dead_session={snapshot['dead_session']} "
        f"outbox_pending={snapshot['outbox_pending']} "
        f"outbox_ready={snapshot['outbox_ready']} "
        f"outbox_failed={snapshot['outbox_failed']} "
        f"oldest_overdue={snapshot['oldest_overdue_minutes']}m"
    )


def _should_log_monitor_health_summary(now_monotonic: float) -> bool:
    global _last_health_summary_log
    interval_seconds = int(getattr(settings, "MONITOR_HEALTH_LOG_INTERVAL_SECONDS", 300))
    if interval_seconds <= 0:
        return False
    if now_monotonic - _last_health_summary_log < interval_seconds:
        return False
    _last_health_summary_log = now_monotonic
    return True


async def _lease_heartbeat_loop(blogger_id: int, lock_owner: str) -> None:
    while True:
        await asyncio.sleep(LEASE_HEARTBEAT_SECONDS)
        try:
            with get_sync_db_context() as db_hb:
                blogger = (
                    db_hb.query(MonitoredBlogger)
                    .filter(MonitoredBlogger.id == blogger_id)
                    .first()
                )
                if not blogger or blogger.lock_owner != lock_owner:
                    return
                blogger.lock_until = datetime.now(timezone.utc) + timedelta(minutes=LEASE_MINUTES)
                db_hb.commit()
        except asyncio.CancelledError:
            raise
        except Exception as heartbeat_error:
            logger.warning(
                f"[VIDEO_MONITOR] Lease heartbeat failed for blogger {blogger_id}: {heartbeat_error}"
            )


def _outbox_backoff_seconds(attempts: int) -> int:
    """Exponential backoff with ±25% jitter to prevent thundering herd.

    Without jitter, when N events fail at once they all retry at the same moment,
    overload the worker, fail again, retry again — endless storm. Jitter spreads
    retries across a window so the system can drain naturally.
    """
    exponent = max(0, int(attempts or 0))
    base = min(
        MONITOR_OUTBOX_MAX_BACKOFF_SECONDS,
        MONITOR_OUTBOX_RETRY_BASE_SECONDS * (2 ** exponent),
    )
    # ±25% jitter, never less than 1 second
    jitter_factor = random.uniform(0.8, 1.2)
    return min(MONITOR_OUTBOX_MAX_BACKOFF_SECONDS, max(1, int(base * jitter_factor)))


def _build_outbox_dedupe_key(
    event_type: str,
    creator_id: int | None,
    video_ids: list[int] | list[str],
    extra: str | None = None,
) -> str:
    normalized_ids = ",".join(str(item) for item in sorted({int(item) for item in video_ids}))
    suffix = f":{extra}" if extra else ""
    return f"{event_type}:{creator_id or 0}:{normalized_ids}{suffix}"


def _enqueue_monitor_outbox(
    db: Session,
    *,
    event_type: str,
    blogger_id: int | None,
    creator_id: int | None,
    payload: dict[str, Any],
    dedupe_key: str,
) -> None:
    existing = db.query(MonitorOutbox).filter(MonitorOutbox.dedupe_key == dedupe_key).first()
    if existing:
        return
    try:
        with db.begin_nested():
            db.add(
                MonitorOutbox(
                    event_type=event_type,
                    blogger_id=blogger_id,
                    creator_id=creator_id,
                    payload=payload,
                    status="pending",
                    dedupe_key=dedupe_key,
                    available_at=datetime.now(timezone.utc),
                )
            )
            db.flush()
    except IntegrityError:
        logger.info(
            "[VIDEO_MONITOR] Outbox event already enqueued (dedupe_key=%s)",
            dedupe_key,
        )


def _enqueue_subscription_outbox_events(
    db: Session,
    *,
    blogger_id: int,
    batches: list[tuple[CreatorSource, list[CreatorVideo]]],
) -> None:
    global_parsing_mode = is_parsing_mode(db)
    autopilot_mode = _get_autopilot_mode_safe(db)
    for creator, videos in batches:
        if not videos:
            continue
        video_ids = [video.id for video in videos if video.id is not None]
        if not video_ids:
            continue
        notifications_enabled = _creator_owner_notifications_enabled(db, creator)
        if _creator_is_restricted_by_parsing_mode(db, creator, global_parsing_mode):
            if not notifications_enabled:
                continue
            if creator.mode == "auto_comment":
                _enqueue_monitor_outbox(
                    db,
                    event_type="notify_autopilot_paused_by_parsing_mode",
                    blogger_id=blogger_id,
                    creator_id=creator.id,
                    payload={"creator_id": creator.id, "video_ids": video_ids},
                    dedupe_key=_build_outbox_dedupe_key(
                        "notify_autopilot_paused_by_parsing_mode",
                        creator.id,
                        video_ids,
                    ),
                )
                continue
            _enqueue_monitor_outbox(
                db,
                event_type="notify_new_videos",
                blogger_id=blogger_id,
                creator_id=creator.id,
                payload={"creator_id": creator.id, "video_ids": video_ids},
                dedupe_key=_build_outbox_dedupe_key("notify_new_videos", creator.id, video_ids),
            )
        elif creator.mode == "auto_comment":
            if autopilot_mode != AUTOPILOT_CONTROL_ACTIVE:
                logger.info(
                    "[VIDEO_MONITOR] Autopilot enqueue skipped for creator_id=%s: mode=%s",
                    creator.id,
                    autopilot_mode,
                )
                continue
            _enqueue_monitor_outbox(
                db,
                event_type="autopilot_create",
                blogger_id=blogger_id,
                creator_id=creator.id,
                payload={"creator_id": creator.id, "video_ids": video_ids},
                dedupe_key=_build_outbox_dedupe_key("autopilot_create", creator.id, video_ids),
            )
        elif creator.mode == "manual_review":
            if not notifications_enabled:
                continue
            _enqueue_monitor_outbox(
                db,
                event_type="notify_pending_review",
                blogger_id=blogger_id,
                creator_id=creator.id,
                payload={"creator_id": creator.id, "video_ids": video_ids},
                dedupe_key=_build_outbox_dedupe_key("notify_pending_review", creator.id, video_ids),
            )
        else:
            if not notifications_enabled:
                continue
            _enqueue_monitor_outbox(
                db,
                event_type="notify_new_videos",
                blogger_id=blogger_id,
                creator_id=creator.id,
                payload={"creator_id": creator.id, "video_ids": video_ids},
                dedupe_key=_build_outbox_dedupe_key("notify_new_videos", creator.id, video_ids),
            )


def _claim_outbox_batch(
    db: Session,
    limit: int,
    lock_owner: str,
    *,
    include_autopilot_create: bool = True,
) -> list[MonitorOutbox]:
    now = datetime.now(timezone.utc)
    stale_before = now - timedelta(minutes=MONITOR_OUTBOX_STALE_LOCK_MINUTES)
    autopilot_mode = _get_autopilot_mode_safe(db)
    mode_filters = []
    if autopilot_mode == AUTOPILOT_CONTROL_PAUSED:
        mode_filters.append(MonitorOutbox.event_type != "autopilot_create")
    if not include_autopilot_create:
        mode_filters.append(MonitorOutbox.event_type != "autopilot_create")
    priority = case(
        (MonitorOutbox.event_type == "notify_pending_review", 0),
        (MonitorOutbox.event_type == "autopilot_create", 1),
        (MonitorOutbox.event_type == "notify_auto_orders_created", 2),
        (MonitorOutbox.event_type == "notify_autopilot_paused_by_parsing_mode", 3),
        else_=4,
    )
    rows = (
        db.query(MonitorOutbox)
        .filter(
            *mode_filters,
            or_(
                and_(
                    MonitorOutbox.status.in_(["pending", "failed"]),
                    or_(MonitorOutbox.available_at.is_(None), MonitorOutbox.available_at <= now),
                    or_(MonitorOutbox.locked_at.is_(None), MonitorOutbox.locked_at < stale_before),
                ),
                and_(
                    MonitorOutbox.status == "processing",
                    or_(MonitorOutbox.locked_at.is_(None), MonitorOutbox.locked_at < stale_before),
                ),
            ),
        )
        .order_by(
            priority,
            MonitorOutbox.available_at.asc().nulls_first(),
            MonitorOutbox.created_at.asc(),
        )
        .limit(limit)
        .with_for_update()
        .all()
    )
    for row in rows:
        row.status = "processing"
        row.locked_at = now
        row.lock_owner = lock_owner
    if rows:
        db.commit()
    return rows


def _mark_outbox_entry_failed(entry_id: int, error: BaseException | str) -> None:
    with get_sync_db_context() as db:
        retry_db = db.query(MonitorOutbox).filter(MonitorOutbox.id == entry_id).first()
        if not retry_db:
            return
        new_attempts = int(retry_db.attempts or 0) + 1
        retry_db.attempts = new_attempts
        retry_db.lock_owner = None
        retry_db.locked_at = None
        retry_db.last_error = str(error)[:400]
        if new_attempts >= MONITOR_OUTBOX_MAX_ATTEMPTS:
            retry_db.status = "abandoned"
            logger.error(
                "[VIDEO_MONITOR] Outbox event %d abandoned after %d attempts (type=%s): %s",
                entry_id, new_attempts, retry_db.event_type, error,
            )
        else:
            retry_db.status = "failed"
            retry_db.available_at = datetime.now(timezone.utc) + timedelta(
                seconds=_outbox_backoff_seconds(new_attempts)
            )
        db.commit()


async def _process_outbox_entry(entry_id: int) -> bool:
    started_monotonic = time.perf_counter()
    with get_sync_db_context() as db:
        entry = db.query(MonitorOutbox).filter(MonitorOutbox.id == entry_id).first()
        if not entry:
            return True
        if entry.status == "cancelled":
            entry.lock_owner = None
            entry.locked_at = None
            db.commit()
            logger.info("[VIDEO_MONITOR] Outbox entry %d skipped: cancelled", entry_id)
            return True

        payload = dict(entry.payload or {})
        creator_id = payload.get("creator_id") or entry.creator_id
        video_ids = [int(video_id) for video_id in (payload.get("video_ids") or [])]
        creator = db.query(CreatorSource).filter(CreatorSource.id == creator_id).first() if creator_id else None
        videos = []
        if creator and video_ids:
            video_map = {
                video.id: video
                for video in db.query(CreatorVideo).filter(CreatorVideo.id.in_(video_ids)).all()
            }
            videos = [video_map[video_id] for video_id in video_ids if video_id in video_map]

        try:
            if entry.event_type == "autopilot_create":
                autopilot_mode = _get_autopilot_mode_safe(db)
                if autopilot_mode == AUTOPILOT_CONTROL_PAUSED:
                    entry.status = "pending"
                    entry.lock_owner = None
                    entry.locked_at = None
                    entry.available_at = datetime.now(timezone.utc) + timedelta(minutes=5)
                    entry.last_error = "autopilot_paused"
                    db.commit()
                    logger.info("[VIDEO_MONITOR] Outbox entry %d deferred: autopilot paused", entry_id)
                    return True
                if not creator:
                    raise RuntimeError("creator_not_found")
                if creator.mode != "auto_comment":
                    notification_event = (
                        "notify_pending_review"
                        if creator.mode == "manual_review"
                        else "notify_new_videos"
                    )
                    logger.info(
                        "[VIDEO_MONITOR] Outbox entry %d skipped: creator_id=%s mode changed to %s",
                        entry_id,
                        creator.id,
                        creator.mode,
                    )
                    if video_ids and _creator_owner_notifications_enabled(db, creator):
                        _enqueue_monitor_outbox(
                            db,
                            event_type=notification_event,
                            blogger_id=entry.blogger_id,
                            creator_id=creator.id,
                            payload={"creator_id": creator.id, "video_ids": video_ids},
                            dedupe_key=_build_outbox_dedupe_key(notification_event, creator.id, video_ids),
                        )
                    entry.status = "done"
                    entry.lock_owner = None
                    entry.locked_at = None
                    entry.last_error = None
                    db.commit()
                    return True
                if _creator_is_restricted_by_parsing_mode(db, creator):
                    if video_ids and _creator_owner_notifications_enabled(db, creator):
                        _enqueue_monitor_outbox(
                            db,
                            event_type="notify_new_videos",
                            blogger_id=entry.blogger_id,
                            creator_id=creator.id,
                            payload={"creator_id": creator.id, "video_ids": video_ids},
                            dedupe_key=_build_outbox_dedupe_key("notify_new_videos", creator.id, video_ids),
                        )
                    entry.status = "done"
                    entry.lock_owner = None
                    entry.locked_at = None
                    entry.last_error = None
                    db.commit()
                    return True

                template_id = creator.default_template_id
                explicit_default_template_id = template_id
                if not template_id and creator.template_ids:
                    template_id = creator.template_ids[0] if isinstance(creator.template_ids, list) and creator.template_ids else None
                if not template_id:
                    if video_ids and _creator_owner_notifications_enabled(db, creator):
                        _enqueue_monitor_outbox(
                            db,
                            event_type="notify_pending_review",
                            blogger_id=entry.blogger_id,
                            creator_id=creator.id,
                            payload={"creator_id": creator.id, "video_ids": video_ids},
                            dedupe_key=_build_outbox_dedupe_key("notify_pending_review", creator.id, video_ids),
                        )
                    entry.status = "done"
                    entry.lock_owner = None
                    entry.locked_at = None
                    db.commit()
                    return True

                template = db.query(TaskTemplate).filter(
                    TaskTemplate.id == template_id,
                    TaskTemplate.user_id == creator.user_id,
                ).first()
                if not template and explicit_default_template_id:
                    logger.warning(
                        "[VIDEO_MONITOR] Creator %s (@%s) default template %s is missing; disabling autopilot fallback",
                        creator.id,
                        creator.username,
                        explicit_default_template_id,
                    )
                    creator.mode = "manual_review"
                    creator.default_template_id = None
                    if video_ids and _creator_owner_notifications_enabled(db, creator):
                        _enqueue_monitor_outbox(
                            db,
                            event_type="notify_pending_review",
                            blogger_id=entry.blogger_id,
                            creator_id=creator.id,
                            payload={"creator_id": creator.id, "video_ids": video_ids},
                            dedupe_key=_build_outbox_dedupe_key("notify_pending_review", creator.id, video_ids),
                        )
                    entry.status = "done"
                    entry.lock_owner = None
                    entry.locked_at = None
                    entry.last_error = "default_template_missing"
                    db.commit()
                    return True
                if not template:
                    template = db.query(TaskTemplate).filter(TaskTemplate.user_id == creator.user_id).first()
                if not template:
                    raise RuntimeError("template_not_found")

                limited_video_ids, limit_reason = _limited_autopilot_video_ids(
                    db,
                    creator=creator,
                    video_ids=video_ids,
                )
                if not limited_video_ids:
                    entry.status = "pending"
                    entry.lock_owner = None
                    entry.locked_at = None
                    entry.available_at = datetime.now(timezone.utc) + timedelta(minutes=10)
                    entry.last_error = limit_reason or "autopilot_limit_reached"
                    db.commit()
                    logger.info(
                        "[VIDEO_MONITOR] Outbox entry %d deferred by autopilot limit: %s",
                        entry_id,
                        entry.last_error,
                    )
                    return True

                limited_video_id_set = set(limited_video_ids)
                deferred_video_ids = [video_id for video_id in video_ids if video_id not in limited_video_id_set]

                batch_result = create_monitored_tasks_batch_sync(
                    db,
                    creator,
                    video_ids=limited_video_ids,
                    template=template,
                    origin="autopilot",
                    created_from="monitor_worker",
                )
                notifications_enabled = _creator_owner_notifications_enabled(db, creator)
                if batch_result.created_video_ids:
                    _enqueue_monitor_outbox(
                        db,
                        event_type="notify_auto_orders_created",
                        blogger_id=entry.blogger_id,
                        creator_id=creator.id,
                        payload={
                            "creator_id": creator.id,
                            "video_ids": batch_result.created_video_ids,
                            "task_ids": batch_result.task_ids,
                            "total_cost": float(batch_result.total_cost),
                        },
                        dedupe_key=_build_outbox_dedupe_key(
                            "notify_auto_orders_created",
                            creator.id,
                            batch_result.created_video_ids,
                            extra=",".join(str(task_id) for task_id in batch_result.task_ids),
                        ),
                    )

                remaining_videos = (
                    db.query(CreatorVideo)
                    .filter(
                        CreatorVideo.id.in_(limited_video_ids),
                        CreatorVideo.status == "new",
                        CreatorVideo.task_id.is_(None),
                    )
                    .all()
                )
                if remaining_videos and notifications_enabled:
                    remaining_ids = [video.id for video in remaining_videos]
                    _enqueue_monitor_outbox(
                        db,
                        event_type="notify_pending_review",
                        blogger_id=entry.blogger_id,
                        creator_id=creator.id,
                        payload={"creator_id": creator.id, "video_ids": remaining_ids},
                        dedupe_key=_build_outbox_dedupe_key("notify_pending_review", creator.id, remaining_ids),
                    )

                if deferred_video_ids:
                    deferred_payload = dict(payload)
                    deferred_payload["video_ids"] = deferred_video_ids
                    deferred_payload["autopilot_deferred_by_limit"] = True
                    entry.payload = deferred_payload
                    entry.dedupe_key = _build_outbox_dedupe_key("autopilot_create", creator.id, deferred_video_ids)
                    entry.status = "pending"
                    entry.lock_owner = None
                    entry.locked_at = None
                    entry.available_at = datetime.now(timezone.utc) + timedelta(minutes=10)
                    entry.last_error = "autopilot_limit_deferred_remaining"
                    db.commit()
                    logger.info(
                        "[VIDEO_MONITOR] Outbox entry %d kept pending with %d deferred video(s) after autopilot limit",
                        entry_id,
                        len(deferred_video_ids),
                    )
                    return True

                entry.status = "done"
                entry.lock_owner = None
                entry.locked_at = None
                entry.last_error = None
                db.commit()
                increment_counter(
                    "monitor.outbox",
                    tags={"event": entry.event_type, "result": "done"},
                )
                record_timing(
                    "monitor.outbox",
                    max(0.0, (time.perf_counter() - started_monotonic) * 1000.0),
                    tags={"event": entry.event_type, "result": "done"},
                )
                return True

            if not creator:
                logger.warning(
                    "[VIDEO_MONITOR] Outbox entry %d skipped: creator_id=%s not found (deleted?), event=%s",
                    entry_id, creator_id, entry.event_type,
                )
                raise RuntimeError("creator_not_found")

            if (
                entry.event_type in USER_NOTIFICATION_EVENTS
                and creator.mode == "auto_comment"
                and video_ids
                and not _creator_is_restricted_by_parsing_mode(db, creator)
                and _creator_has_autopilot_template_reference(creator)
            ):
                _enqueue_monitor_outbox(
                    db,
                    event_type="autopilot_create",
                    blogger_id=entry.blogger_id,
                    creator_id=creator.id,
                    payload={"creator_id": creator.id, "video_ids": video_ids},
                    dedupe_key=_build_outbox_dedupe_key("autopilot_create", creator.id, video_ids),
                )
                logger.info(
                    "[VIDEO_MONITOR] Outbox entry %d rerouted from %s to autopilot_create because creator_id=%s is now auto_comment",
                    entry_id,
                    entry.event_type,
                    creator.id,
                )
                entry.status = "done"
                entry.lock_owner = None
                entry.locked_at = None
                entry.last_error = None
                db.commit()
                return True

            if entry.event_type in USER_NOTIFICATION_EVENTS and not _creator_owner_notifications_enabled(db, creator):
                logger.info(
                    "[VIDEO_MONITOR] Outbox entry %d skipped: notifications disabled for user_id=%s creator_id=%s",
                    entry_id,
                    creator.user_id,
                    creator.id,
                )
                sent = True
            elif (
                _creator_is_restricted_by_parsing_mode(db, creator)
                and entry.event_type == "notify_pending_review"
            ):
                sent = await notify_new_videos(creator, videos)
            elif entry.event_type == "notify_pending_review":
                sent = await notify_pending_review(creator, videos)
            elif entry.event_type == "notify_new_videos":
                sent = await notify_new_videos(creator, videos)
            elif entry.event_type == "notify_auto_orders_created":
                sent = await notify_auto_orders_created(
                    creator,
                    videos,
                    float(payload.get("total_cost") or 0.0),
                    [int(task_id) for task_id in (payload.get("task_ids") or [])],
                )
            elif entry.event_type == "notify_autopilot_paused_by_parsing_mode":
                sent = await notify_autopilot_paused_by_parsing_mode(creator, videos)
            else:
                logger.warning(f"[VIDEO_MONITOR] Unknown outbox event type {entry.event_type}, marking done")
                sent = True

            if not sent:
                if _creator_owner_delivery_permanently_blocked(db, creator):
                    logger.info(
                        "[VIDEO_MONITOR] Outbox entry %d marked done: Telegram permanently blocked delivery for user_id=%s creator_id=%s",
                        entry_id,
                        creator.user_id if creator else None,
                        creator.id if creator else None,
                    )
                    sent = True
                else:
                    raise RuntimeError("notification_not_delivered")

            entry.status = "done"
            entry.lock_owner = None
            entry.locked_at = None
            entry.last_error = None
            db.commit()
            increment_counter(
                "monitor.outbox",
                tags={"event": entry.event_type, "result": "done"},
            )
            record_timing(
                "monitor.outbox",
                max(0.0, (time.perf_counter() - started_monotonic) * 1000.0),
                tags={"event": entry.event_type, "result": "done"},
            )
            return True
        except Exception as e:
            db.rollback()
            _mark_outbox_entry_failed(entry_id, e)
            increment_counter(
                "monitor.outbox",
                tags={"event": entry.event_type, "result": "failed"},
            )
            record_timing(
                "monitor.outbox",
                max(0.0, (time.perf_counter() - started_monotonic) * 1000.0),
                tags={"event": entry.event_type, "result": "failed"},
            )
            logger.warning(f"[VIDEO_MONITOR] Outbox event {entry_id} failed: {e}")
            return False


async def _process_monitor_outbox(
    limit: int | None = None,
    *,
    include_autopilot_create: bool = True,
) -> int:
    limit = limit or int(getattr(settings, "MONITOR_OUTBOX_BATCH_LIMIT", 20))
    lock_owner = _make_lock_owner("outbox", 0)
    with get_sync_db_context() as db:
        claimed = _claim_outbox_batch(
            db,
            limit,
            lock_owner,
            include_autopilot_create=include_autopilot_create,
        )
        claimed_ids = [row.id for row in claimed]

    processed = 0
    for entry_id in claimed_ids:
        try:
            await asyncio.wait_for(
                _process_outbox_entry(entry_id),
                timeout=MONITOR_OUTBOX_ENTRY_TIMEOUT_SECONDS,
            )
        except asyncio.TimeoutError:
            logger.warning("[VIDEO_MONITOR] Outbox entry %d timed out after %ds", entry_id, MONITOR_OUTBOX_ENTRY_TIMEOUT_SECONDS)
            _mark_outbox_entry_failed(entry_id, "outbox_entry_timeout")
        except asyncio.CancelledError as cancel_err:
            logger.warning("[VIDEO_MONITOR] Outbox entry %d cancelled while processing", entry_id)
            _mark_outbox_entry_failed(entry_id, cancel_err)
            raise
        except Exception as outbox_err:
            logger.warning("[VIDEO_MONITOR] Outbox entry %d unexpected error: %s", entry_id, outbox_err)
            _mark_outbox_entry_failed(entry_id, outbox_err)
        processed += 1
    return processed


def _apply_account_monitor_use(account: Account, *, now: datetime) -> None:
    account.last_used_at = now
    account.last_checked_at = now


def _apply_account_success_state(account: Account, *, now: datetime) -> None:
    account.error_count_recent = 0
    account.validation_fail_count = 0
    account.last_error = None
    account.last_checked_at = now
    account.last_validation_ok = now
    account.last_used_at = now
    if account.status != "dead":
        account.status = "active"


def _apply_account_soft_error_state(account: Account, reason: str, *, now: datetime) -> None:
    normalized_reason = str(reason or "Monitor parsing error")[:500]
    category = _monitor_soft_error_category(normalized_reason)
    transient_issue = _is_transient_monitor_account_issue(category)

    current_errors = int(account.error_count_recent or 0)
    should_increment = True

    if transient_issue:
        # Monitor errors are often caused by a temporary page/proxy/network condition.
        # Do not burn an account on repeated transient signals during a short cooldown window.
        last_checked_at = _ensure_utc_datetime(account.last_checked_at)
        grace_minutes = _monitor_setting_int(
            "MONITOR_TRANSIENT_ERROR_INCREMENT_COOLDOWN_MINUTES",
            20,
            minimum=1,
        )
        if last_checked_at and (now - last_checked_at) < timedelta(minutes=grace_minutes):
            should_increment = False

    if should_increment:
        next_errors = current_errors + 1
        if transient_issue:
            transient_cap = _monitor_setting_int(
                "MONITOR_TRANSIENT_ERROR_MAX_COUNT",
                max(0, int(settings.ERROR_THRESHOLD) - 1),
                minimum=0,
            )
            next_errors = min(next_errors, max(0, transient_cap))
        account.error_count_recent = next_errors

    account.last_checked_at = now
    account.last_error = _serialize_error_payload(
        _build_monitor_error_payload(
            normalized_reason,
            category=category,
            fatal=False,
        )
    )

    if not transient_issue and int(account.error_count_recent or 0) >= settings.ERROR_THRESHOLD:
        account.status = "error"
    elif account.status == "error" and int(account.error_count_recent or 0) < settings.ERROR_THRESHOLD:
        # If a fresh imported/previously degraded account only got a soft monitor cooldown,
        # let it return to the pool after cooldown instead of keeping status=error forever.
        account.status = "active"


def _apply_account_dead_state(account: Account, reason: str, *, now: datetime) -> None:
    account.status = "dead"
    account.error_count_recent = max(int(account.error_count_recent or 0), settings.ERROR_THRESHOLD)
    account.last_checked_at = now
    account.last_error = _serialize_error_payload(
        _build_monitor_error_payload(
            str(reason or "Dead session detected")[:500],
            category="dead_session",
            cooldown_minutes=None,
            fatal=True,
        )
    )


async def _defer_blogger_for_global_network_outage(
    db: Session,
    blogger: MonitoredBlogger,
    *,
    active_subscriptions: list[CreatorSource],
    now: datetime,
    reason: str,
) -> None:
    logger.warning(
        f"[VIDEO_MONITOR] Global network outage suspected while checking @{blogger.username}: {reason[:180]}"
    )
    await _record_blogger_failure(db, blogger, "global_network_unavailable")
    _schedule_next_blogger_check(
        blogger,
        active_subscriptions=active_subscriptions,
        now=now,
        cooldown_minutes=getattr(settings, "NO_ACCOUNTS_COOLDOWN_MINUTES", 5),
    )
    db.commit()


def _defer_blogger_for_mobile_proxy_busy(
    db: Session,
    blogger: MonitoredBlogger,
    *,
    active_subscriptions: list[CreatorSource],
    now: datetime,
    reason: str,
) -> str:
    cooldown_minutes = _monitor_setting_int(
        "MONITOR_MOBILE_PROXY_BUSY_DEFER_MINUTES",
        getattr(settings, "NO_ACCOUNTS_COOLDOWN_MINUTES", 5),
        minimum=1,
    )
    _schedule_next_blogger_check(
        blogger,
        active_subscriptions=active_subscriptions,
        now=now,
        cooldown_minutes=cooldown_minutes,
    )
    logger.warning(
        "[VIDEO_MONITOR] Deferred @%s because mobile proxy is busy; retry in %dm: %s",
        blogger.username,
        cooldown_minutes,
        str(reason)[:180],
    )
    db.commit()
    return "mobile_proxy_deferred"


def _looks_like_temporary_tiktok_page_error(reason: str) -> bool:
    lowered = str(reason or "").lower()
    return any(
        marker in lowered
        for marker in (
            "temporary page error",
            "something went wrong",
            "sorry about that",
            "please try again later",
            "unable to load",
            "failed to load",
            "error loading",
        )
    )


async def _defer_blogger_for_tiktok_degradation(
    db: Session,
    blogger: MonitoredBlogger,
    *,
    active_subscriptions: list[CreatorSource],
    now: datetime,
    reason: str,
) -> str:
    cooldown_minutes = _monitor_setting_int(
        "MONITOR_TIKTOK_DEGRADATION_DEFER_MINUTES",
        getattr(settings, "NO_ACCOUNTS_COOLDOWN_MINUTES", 5),
        minimum=1,
    )
    _schedule_next_blogger_check(
        blogger,
        active_subscriptions=active_subscriptions,
        now=now,
        cooldown_minutes=cooldown_minutes,
    )
    record_incident(
        INCIDENT_TIKTOK_REGION_DEGRADATION,
        severity="warning",
        title="TikTok profile pages degraded",
        cause=str(reason or "temporary TikTok profile parse degradation")[:500],
        auto_action=f"Deferred @{blogger.username} for {cooldown_minutes} minute(s) without penalizing monitor accounts.",
        admin_action="Watch monitor health. If this persists, rotate/replace monitor proxies or lower monitor concurrency.",
        affected=[blogger.username],
        cooldown_until=now + timedelta(minutes=cooldown_minutes),
        ttl_minutes=max(cooldown_minutes, 10),
    )
    streak = await _record_blogger_failure(db, blogger, f"tiktok_degraded: {str(reason)[:180]}")
    _schedule_failed_blogger_check(
        blogger,
        active_subscriptions=active_subscriptions,
        now=now,
        reason=f"tiktok_degraded: {str(reason)[:180]}",
        streak=streak,
    )
    logger.warning(
        "[VIDEO_MONITOR] Deferred @%s due to temporary TikTok degradation without account penalty: %s",
        blogger.username,
        str(reason)[:180],
    )
    db.commit()
    return "tiktok_degraded_deferred"


def _defer_blogger_for_navigation_timeout(
    db: Session,
    blogger: MonitoredBlogger,
    *,
    active_subscriptions: list[CreatorSource],
    now: datetime,
    reason: str,
) -> str:
    timeout_stats = _record_navigation_timeout_event(blogger.username or str(blogger.id), now=now)
    storm_active = bool(timeout_stats.get("storm_active"))
    cooldown_setting = (
        "MONITOR_NAVIGATION_TIMEOUT_STORM_DEFER_MINUTES"
        if storm_active
        else "MONITOR_NAVIGATION_TIMEOUT_DEFER_MINUTES"
    )
    cooldown_minutes = _monitor_setting_int(
        cooldown_setting,
        getattr(settings, "NO_ACCOUNTS_COOLDOWN_MINUTES", 5),
        minimum=1,
    )
    _schedule_next_blogger_check(
        blogger,
        active_subscriptions=active_subscriptions,
        now=now,
        cooldown_minutes=cooldown_minutes,
    )
    logger.warning(
        "[VIDEO_MONITOR] Deferred @%s after TikTok navigation timeout "
        "(storm=%s, window_count=%s): %s",
        blogger.username,
        storm_active,
        timeout_stats.get("window_count"),
        reason[:180],
    )
    db.commit()
    return "network_storm_deferred" if storm_active else "network_timeout_deferred"


async def _close_monitor_resources(page, context) -> None:
    async def _do_close():
        page_managed_by_pool = bool(getattr(page, '_managed_by_pool', False)) if page else False
        managed_account_id = getattr(page, "_pool_account_id", None) if page_managed_by_pool else None
        if page:
            try:
                from executor.page_utils import safe_close_page
                await safe_close_page(page)
            except Exception:
                try:
                    await page.close()
                except Exception:
                    pass
        if page_managed_by_pool and managed_account_id is not None:
            try:
                from executor.browser_pool import close_account_context

                await close_account_context(managed_account_id, reason="monitor_check_done")
            except Exception:
                if context:
                    try:
                        await context.close()
                    except Exception:
                        pass
            return
        if context and (not page_managed_by_pool):
            try:
                await context.close()
            except Exception:
                pass
                
    try:
        import asyncio
        await asyncio.wait_for(_do_close(), timeout=5.0)
    except Exception as e:
        import logging
        logging.getLogger(__name__).warning(f"[VIDEO_MONITOR] Hard timeout closing browser resources (zombie process prevented): {e}")



async def initial_parse_blogger(blogger_id: int) -> bool:
    """
    Performs the first-time parse of a MonitoredBlogger.

    DB session is intentionally NOT held open during browser automation to avoid
    connection pool exhaustion (browser operations can take up to 180 seconds).
    Phase 1 reads blogger + account, closes session.
    Phase 2 runs the browser (no open DB conn).
    Phase 3 opens a fresh session to persist results.
    """
    from services.account_service import mark_success

    # Phase 1: load data, mark account use — close session immediately.
    blogger_username: str = ""
    account_id: int | None = None
    account_proxy: str | None = None
    account_obj = None
    initial_parse_limit = INITIAL_PARSE_LIMIT

    try:
        with get_sync_db_context() as db:
            blogger = db.query(MonitoredBlogger).filter(MonitoredBlogger.id == blogger_id).first()
            if not blogger:
                return False
            blogger_username = blogger.username
            logger.info(f"[INITIAL_PARSE] Starting initial parse for @{blogger_username}")
            account_obj = get_monitor_account(db)
            if not account_obj:
                return False
            account_id = account_obj.id
            account_proxy = account_obj.proxy
            try:
                adaptive_enabled, adaptive_settings, _adaptive_config = _load_adaptive_runtime_settings(db)
                if adaptive_enabled:
                    initial_parse_limit = max(INITIAL_PARSE_LIMIT, int(adaptive_settings.initial_limit))
            except Exception as settings_error:
                logger.warning(
                    "[INITIAL_PARSE] Failed to load adaptive initial_limit for @%s, using %s: %s",
                    blogger_username,
                    initial_parse_limit,
                    settings_error,
                )
            mark_monitor_use(db, account_obj.id)
            db.commit()
    except Exception as e:
        logger.error(f"[INITIAL_PARSE] Phase 1 error for blogger {blogger_id}: {e}", exc_info=True)
        return False

    # Phase 2: browser automation — no DB session held.
    page = None
    context = None
    scan_result: ScanResult | None = None
    try:
        initial_parse_timeout = getattr(settings, "INITIAL_PARSE_TIMEOUT_SECONDS", 180)
        context, page = await create_page_for_account(account_obj)
        scan_result = await asyncio.wait_for(
            fetch_latest_videos_from_profile(
                page,
                blogger_username,
                limit=initial_parse_limit,
                stop_at_video_id=None,
                account=account_obj,
            ),
            timeout=initial_parse_timeout,
        )
    except Exception as e:
        logger.error(f"[INITIAL_PARSE] Phase 2 (browser) error for @{blogger_username}: {e}", exc_info=True)
        return False
    finally:
        await _close_monitor_resources(page, context)

    # Phase 3: persist results — fresh DB session.
    try:
        with get_sync_db_context() as db:
            blogger = db.query(MonitoredBlogger).filter(MonitoredBlogger.id == blogger_id).first()
            if not blogger:
                return False
            now = datetime.now(timezone.utc)
            videos = scan_result.new_videos if scan_result else []
            active_user_ids = None if settings.DEMO_MODE else _get_active_user_ids(db, now)
            blogger.last_checked_at = now
            blogger.last_attempt_at = now
            blogger.last_success_at = now
            if videos:
                blogger.last_seen_video_id = scan_result.newest_normal_video_id or videos[0][0]
            _schedule_next_blogger_check_from_scan(
                db,
                blogger,
                active_subscriptions=_get_active_subscriptions_for_blogger(blogger, active_user_ids),
                now=now,
                scan_result=scan_result,
                found_new_count=0,
                status=scan_result.status if scan_result else "INITIAL",
                videos=videos,
                found_new_for_adaptive=False,
                count_empty_streak=False,
            )
            if account_id:
                mark_success(db, account_id)
            if account_proxy:
                clear_proxy_bad(account_proxy)
            db.commit()
            return True
    except Exception as e:
        logger.error(f"[INITIAL_PARSE] Phase 3 (persist) error for @{blogger_username}: {e}", exc_info=True)
        return False


async def auto_create_tasks(db: Session, creator: CreatorSource, video_rows: List[CreatorVideo]) -> Tuple[List[CreatorVideo], float, List[int]]:
    """
    Automatically creates tasks for videos in auto_comment mode.
    Uses the same transactional flow as the main task creation path.
    """
    if not creator.user_id:
        return ([], 0.0, [])

    template_id = creator.default_template_id
    if not template_id and creator.template_ids:
        template_id = creator.template_ids[0] if isinstance(creator.template_ids, list) and creator.template_ids else None

    if not template_id:
        logger.info(f"[AUTOPILOT] Creator {creator.id} (@{creator.username}) has no default_template_id, videos will remain in Inbox")
        return ([], 0.0, [])

    max_videos = getattr(settings, "AUTOPILOT_MAX_VIDEOS_PER_TICK", 10)
    if len(video_rows) > max_videos:
        logger.info(f"[AUTOPILOT] Limiting videos from {len(video_rows)} to {max_videos} (max_videos_per_tick)")
        video_rows = video_rows[:max_videos]

    template = db.query(TaskTemplate).filter(
        TaskTemplate.id == template_id,
        TaskTemplate.user_id == creator.user_id,
    ).first()
    if not template:
        template = db.query(TaskTemplate).filter(TaskTemplate.user_id == creator.user_id).first()
        if not template:
            return ([], 0.0, [])

    batch_result = create_monitored_tasks_batch_sync(
        db,
        creator,
        video_ids=[video.id for video in video_rows],
        template=template,
        origin="autopilot",
        created_from="monitor_worker",
    )

    if batch_result.status == "insufficient_funds":
        logger.warning(
            f"[AUTOPILOT] User {creator.user_id} has insufficient funds; videos stay in Inbox"
        )
        return ([], 0.0, [])

    successful_ids = set(batch_result.created_video_ids)
    successful_videos = [video for video in video_rows if video.id in successful_ids]
    return (successful_videos, float(batch_result.total_cost), list(batch_result.task_ids))


async def process_blogger(
    db: Session,
    blogger: MonitoredBlogger,
    *,
    active_user_ids: set[int] | None = None,
) -> str | None:
    """
    Processes a single blogger using DB-side scheduling and retryable side effects.
    """
    now = datetime.now(timezone.utc)
    started_monotonic = time.perf_counter()

    def elapsed_ms() -> int:
        return max(0, int((time.perf_counter() - started_monotonic) * 1000.0))

    lock_owner = _make_lock_owner("blogger", blogger.id)
    heartbeat_task: asyncio.Task | None = None

    try:
        locked_blogger = (
            db.query(MonitoredBlogger)
            .options(selectinload(MonitoredBlogger.subscriptions))
            .filter(
                MonitoredBlogger.id == blogger.id,
                or_(MonitoredBlogger.lock_until.is_(None), MonitoredBlogger.lock_until <= now),
            )
            .with_for_update()
            .first()
        )
        if not locked_blogger:
            return "lease_busy"

        locked_blogger.lock_until = now + timedelta(minutes=LEASE_MINUTES)
        locked_blogger.lock_owner = lock_owner
        locked_blogger.last_attempt_at = now
        locked_blogger.last_checked_at = now
        db.commit()
        blogger = locked_blogger
        heartbeat_task = asyncio.create_task(_lease_heartbeat_loop(blogger.id, lock_owner))

        active_subscriptions = _get_active_subscriptions_for_blogger(blogger, active_user_ids)
        if not active_subscriptions:
            _schedule_next_blogger_check(blogger, active_subscriptions=[], now=now)
            db.commit()
            return "no_active_subscriptions"

        max_account_switches = 3
        excluded_account_ids: list[int] = []
        excluded_proxy_values: set[str] = set()
        empty_profile_count = 0
        last_failure_reason = "account_pool_exhausted"

        for attempt in range(max_account_switches + 1):
            if excluded_proxy_values:
                account = get_monitor_account(
                    db,
                    exclude_ids=excluded_account_ids or None,
                    exclude_proxies=excluded_proxy_values,
                )
            else:
                account = get_monitor_account(db, exclude_ids=excluded_account_ids or None)
            if not account:
                if excluded_proxy_values:
                    total_eligible = _count_eligible_monitor_accounts(
                        db,
                        exclude_ids=excluded_account_ids,
                        exclude_proxies=excluded_proxy_values,
                    )
                else:
                    total_eligible = _count_eligible_monitor_accounts(db, exclude_ids=excluded_account_ids)
                if total_eligible > 0 or excluded_proxy_values:
                    return _defer_blogger_for_mobile_proxy_busy(
                        db,
                        blogger,
                        active_subscriptions=active_subscriptions,
                        now=now,
                        reason=(
                            "monitor_account_selection_busy: "
                            f"eligible_accounts={total_eligible}, "
                            f"excluded_accounts={len(excluded_account_ids)}, "
                            f"excluded_failed_proxies={len(excluded_proxy_values)}"
                        ),
                    )
                await _send_admin_alert_once(
                    key="no_monitor_accounts_available",
                    message=build_admin_alert(
                        severity="warning",
                        title="Нет доступных monitor-аккаунтов",
                        what=f"Проверка @{blogger.username} отложена: сейчас нет аккаунта, которым можно открыть TikTok.",
                        why=(
                            f"Готовых monitor-аккаунтов: {total_eligible}. "
                            f"Исключено в этой попытке: {len(excluded_account_ids)}."
                        ),
                        auto_action="Софт перенёс проверку блогера на ближайший cooldown и продолжит сам.",
                        admin_action="Проверь раздел Аккаунты/Runtime: нужны живые cookies/session и рабочие прокси.",
                        affected=[f"@{blogger.username}"],
                    ),
                    cooldown_minutes=_monitor_setting_int(
                        "MONITOR_NO_ACCOUNTS_ALERT_COOLDOWN_MINUTES",
                        getattr(settings, "MONITOR_ACCOUNT_ALERT_COOLDOWN_MINUTES", 120),
                        minimum=5,
                    ),
                )
                failure_streak = await _record_blogger_failure(db, blogger, "no_accounts_available")
                _schedule_failed_blogger_check(
                    blogger,
                    active_subscriptions=active_subscriptions,
                    now=now,
                    reason="no_accounts_available",
                    streak=failure_streak,
                )
                db.commit()
                return "no_accounts_available"
            context = None
            page = None
            lock_acquired = False
            proxy_obj = None

            try:
                logger.info(
                    f"[UnifiedParser] Checking @{blogger.username} "
                    f"(attempt {attempt + 1}/{max_account_switches + 1}, account_id={account.id}, last_seen={blogger.last_seen_video_id})"
                )

                if account.proxy:
                    proxy_obj = db.query(Proxy).filter(Proxy.proxy == account.proxy).first()
                    if proxy_obj and proxy_obj.is_mobile == "Y" and proxy_obj.rotation_url:
                        try:
                            lock_acquired, proxy_obj = await prepare_mobile_proxy_for_account(account, db, proxy_obj)
                        except ProxyLockError as proxy_error:
                            logger.warning(
                                f"[UnifiedParser] Failed to prepare mobile proxy for account {account.id}: {proxy_error}"
                            )
                            return _defer_blogger_for_mobile_proxy_busy(
                                db,
                                blogger,
                                active_subscriptions=active_subscriptions,
                                now=now,
                                reason=str(proxy_error),
                            )
                        except ProxyRotationError as proxy_error:
                            logger.warning(
                                f"[UnifiedParser] Failed to rotate mobile proxy for account {account.id}: {proxy_error}"
                            )
                            if account.proxy and str(account.proxy).strip():
                                excluded_proxy_values.add(str(account.proxy).strip())
                            excluded_account_ids.append(account.id)
                            if account.proxy and _should_mark_mobile_proxy_bad_after_hard_failure(
                                proxy_obj,
                                str(proxy_error),
                            ):
                                mark_proxy_bad(account.proxy, reason=f"monitor_mobile_proxy_rotation: {str(proxy_error)[:120]}")
                            _apply_account_soft_error_state(
                                account,
                                f"Monitor mobile proxy rotation failed: {str(proxy_error)[:180]}",
                                now=now,
                            )
                            db.commit()
                            last_failure_reason = f"mobile_proxy_rotation_failed: {str(proxy_error)[:120]}"
                            continue

                context, page = await create_page_for_account(account)
                scan_result = await asyncio.wait_for(
                    fetch_latest_videos_from_profile(
                        page,
                        blogger.username,
                        limit=VIDEOS_LIMIT,
                        stop_at_video_id=blogger.last_seen_video_id,
                        account=account,
                        min_video_create_time=int(now.timestamp()) - 14 * 86400,
                    ),
                    timeout=getattr(settings, "BLOGGER_CHECK_TIMEOUT_SECONDS", 180),
                )
                videos = scan_result.new_videos

                if scan_result.status == "RATE_LIMIT":
                    excluded_account_ids.append(account.id)
                    if account.proxy and _should_mark_proxy_bad_for_monitor_rate_limit(proxy_obj):
                        mark_proxy_bad(account.proxy, reason="monitor_rate_limited")
                    _apply_account_soft_error_state(account, "Monitor rate limited", now=now)
                    db.commit()
                    continue

                if scan_result.status == "FAIL":
                    excluded_account_ids.append(account.id)
                    error_text = scan_result.diagnostics.get("error", "unknown monitor failure")
                    error_type = scan_result.diagnostics.get("error_type", "unknown")
                    last_failure_reason = f"{error_type}: {error_text[:120]}"
                    if _should_defer_monitor_network_timeout(error_text, error_type=error_type):
                        return _defer_blogger_for_navigation_timeout(
                            db,
                            blogger,
                            active_subscriptions=active_subscriptions,
                            now=now,
                            reason=error_text,
                        )
                    if error_type == "network_error" and await _is_global_network_unavailable():
                        await _defer_blogger_for_global_network_outage(
                            db,
                            blogger,
                            active_subscriptions=active_subscriptions,
                            now=now,
                            reason=error_text,
                        )
                        return "global_network_unavailable"
                    if account.proxy and _should_mark_proxy_bad_for_monitor(
                        error_text,
                        error_type=error_type,
                        proxy_obj=proxy_obj,
                    ):
                        mark_proxy_bad(account.proxy, reason=f"monitor_network_error: {error_text[:120]}")
                    _apply_account_soft_error_state(account, f"Monitor parsing failed: {error_text[:180]}", now=now)
                    db.commit()
                    continue

                if scan_result.status == "EMPTY":
                    empty_profile_count += 1
                    reason = scan_result.diagnostics.get("reason", "unknown")
                    valid_empty = reason in {"private_profile", "empty_profile", "profile_not_found"}
                    if valid_empty:
                        _apply_account_success_state(account, now=now)
                        _clear_monitor_proxy_success(account.proxy, proxy_obj)
                        blogger.last_success_at = now
                        _schedule_next_blogger_check_from_scan(
                            db,
                            blogger,
                            active_subscriptions=active_subscriptions,
                            now=now,
                            scan_result=scan_result,
                            found_new_count=0,
                            status=scan_result.status,
                            duration_ms=elapsed_ms(),
                        )
                        if reason in _PROFILE_ACCESS_FAILURE_REASONS:
                            profile_streak = _record_blogger_profile_access_failure_state(
                                blogger,
                                reason,
                                now=now,
                            )
                            await _maybe_send_blogger_profile_access_alert(
                                blogger,
                                reason=reason,
                                streak=profile_streak,
                            )
                        else:
                            _clear_blogger_profile_access_failure(blogger)
                        db.commit()
                        _reset_blogger_fail_streak(blogger.id)
                        return "empty_profile_valid"

                    excluded_account_ids.append(account.id)
                    if empty_profile_count >= 2:
                        last_failure_reason = f"empty_profile_untrusted: {reason}"
                        return await _defer_blogger_for_tiktok_degradation(
                            db,
                            blogger,
                            active_subscriptions=active_subscriptions,
                            now=now,
                            reason=last_failure_reason,
                        )
                    continue

                if scan_result.status == "NO_NORMAL_VIDEOS":
                    _apply_account_success_state(account, now=now)
                    _clear_monitor_proxy_success(account.proxy, proxy_obj)
                    blogger.last_success_at = now
                    _schedule_next_blogger_check_from_scan(
                        db,
                        blogger,
                        active_subscriptions=active_subscriptions,
                        now=now,
                        scan_result=scan_result,
                        found_new_count=0,
                        status=scan_result.status,
                        duration_ms=elapsed_ms(),
                    )
                    _clear_blogger_profile_access_failure(blogger)
                    db.commit()
                    _reset_blogger_fail_streak(blogger.id)
                    return "no_normal_videos"

                if scan_result.status == "RESYNC":
                    repaired_anchor = scan_result.newest_normal_video_id or (videos[0][0] if videos else None)
                    if _should_update_blogger_anchor(repaired_anchor, blogger.last_seen_video_id):
                        blogger.last_seen_video_id = repaired_anchor
                    _apply_account_success_state(account, now=now)
                    _clear_monitor_proxy_success(account.proxy, proxy_obj)
                    blogger.last_success_at = now

                    # Notify about recent videos discovered during RESYNC (max 2, each < 48h old)
                    resync_notify_batches: list[tuple[CreatorSource, list[CreatorVideo]]] = []
                    resync_videos: list[tuple[str, str]] = []
                    if videos and scan_result.video_create_times_map:
                        now_ts = int(now.timestamp())
                        max_age_s = 48 * 3600  # 48 hours per-video filter
                        for _rv_id, _rv_url in videos:
                            _rv_ctime = scan_result.video_create_times_map.get(_rv_id)
                            if _rv_ctime is not None and (now_ts - _rv_ctime) <= max_age_s:
                                resync_videos.append((_rv_id, _rv_url))
                    elif videos and scan_result.video_create_times:
                        # Fallback: no per-video map, check only newest
                        now_ts = int(now.timestamp())
                        if now_ts - scan_result.video_create_times[0] <= 48 * 3600:
                            resync_videos = videos
                    resync_videos = resync_videos[:2]
                    if resync_videos:
                        shared_video_ids = [vid_id for vid_id, _ in resync_videos]
                        for sub in active_subscriptions:
                            existing_video_ids = _load_existing_video_ids(db, creator_id=sub.id, video_ids=shared_video_ids)
                            sub_new_videos: list[CreatorVideo] = []
                            for batch_index, (video_id, video_url) in enumerate(resync_videos, start=1):
                                if video_id in existing_video_ids:
                                    continue
                                video_meta = _build_video_analytics_meta(
                                    blogger=blogger,
                                    scan_result=scan_result,
                                    video_id=video_id,
                                    detected_at=now,
                                    batch_size=len(resync_videos),
                                    batch_index=batch_index,
                                )
                                video_row = CreatorVideo(
                                    creator_id=sub.id,
                                    video_id=video_id,
                                    url=video_url,
                                    status="new",
                                    meta=video_meta,
                                )
                                db.add(video_row)
                                sub_new_videos.append(video_row)
                            if sub_new_videos:
                                resync_notify_batches.append((sub, sub_new_videos))

                    resync_found_new = sum(len(b[1]) for b in resync_notify_batches)
                    _schedule_next_blogger_check_from_scan(
                        db,
                        blogger,
                        active_subscriptions=active_subscriptions,
                        now=now,
                        scan_result=scan_result,
                        found_new_count=resync_found_new,
                        status=scan_result.status,
                        duration_ms=elapsed_ms(),
                        videos=videos,
                        found_new_for_adaptive=resync_found_new > 0,
                    )
                    _clear_blogger_profile_access_failure(blogger)
                    if resync_notify_batches:
                        db.flush()
                        try:
                            _enqueue_subscription_outbox_events(db, blogger_id=blogger.id, batches=resync_notify_batches)
                        except Exception as _enqueue_err:
                            logger.error("[VIDEO_MONITOR] resync enqueue failed, rolling back: %s", _enqueue_err)
                            db.rollback()
                            raise
                    db.commit()
                    _reset_blogger_fail_streak(blogger.id)
                    return "resync_anchor_repaired"

                if scan_result.status == "OK" and not videos:
                    _apply_account_success_state(account, now=now)
                    _clear_monitor_proxy_success(account.proxy, proxy_obj)
                    blogger.last_success_at = now
                    _schedule_next_blogger_check_from_scan(
                        db,
                        blogger,
                        active_subscriptions=active_subscriptions,
                        now=now,
                        scan_result=scan_result,
                        found_new_count=0,
                        status=scan_result.status,
                        duration_ms=elapsed_ms(),
                    )
                    _clear_blogger_profile_access_failure(blogger)
                    db.commit()
                    _reset_blogger_fail_streak(blogger.id)
                    return "ok_no_new"

                if not videos:
                    excluded_account_ids.append(account.id)
                    _apply_account_soft_error_state(
                        account,
                        "No videos found and no valid anchor signal during monitor parsing",
                        now=now,
                    )
                    db.commit()
                    continue

                newest_video_id = scan_result.newest_normal_video_id or videos[0][0]
                if blogger.last_seen_video_id is None and blogger.last_success_at is None:
                    blogger.last_seen_video_id = newest_video_id
                    blogger.last_success_at = now
                    _apply_account_success_state(account, now=now)
                    _clear_monitor_proxy_success(account.proxy, proxy_obj)
                    _schedule_next_blogger_check_from_scan(
                        db,
                        blogger,
                        active_subscriptions=active_subscriptions,
                        now=now,
                        scan_result=scan_result,
                        found_new_count=0,
                        status=scan_result.status,
                        duration_ms=elapsed_ms(),
                        videos=videos,
                        found_new_for_adaptive=False,
                        count_empty_streak=False,
                    )
                    _clear_blogger_profile_access_failure(blogger)
                    db.commit()
                    _reset_blogger_fail_streak(blogger.id)
                    logger.info(
                        f"[UnifiedParser] Initial sync for @{blogger.username}: anchor set to {newest_video_id}, notifications skipped"
                    )
                    return "initial_sync"

                logger.info(
                    f"[UnifiedParser] Found {len(videos)} new videos for @{blogger.username} "
                    f"(source: {scan_result.canonical_source})"
                )
                notify_batches: list[tuple[CreatorSource, list[CreatorVideo]]] = []
                shared_video_ids = [video_id for video_id, _ in videos]
                for sub in active_subscriptions:
                    existing_video_ids = _load_existing_video_ids(db, creator_id=sub.id, video_ids=shared_video_ids)
                    sub_new_videos: list[CreatorVideo] = []
                    for batch_index, (video_id, video_url) in enumerate(videos, start=1):
                        if video_id in existing_video_ids:
                            continue
                        video_meta = _build_video_analytics_meta(
                            blogger=blogger,
                            scan_result=scan_result,
                            video_id=video_id,
                            detected_at=now,
                            batch_size=len(videos),
                            batch_index=batch_index,
                        )
                        video_row = CreatorVideo(
                            creator_id=sub.id,
                            video_id=video_id,
                            url=video_url,
                            status="new",
                            meta=video_meta,
                        )
                        db.add(video_row)
                        sub_new_videos.append(video_row)
                    if sub_new_videos:
                        notify_batches.append((sub, sub_new_videos))

                if newest_video_id and newest_video_id != blogger.last_seen_video_id:
                    blogger.last_seen_video_id = newest_video_id

                blogger.last_success_at = now
                _schedule_next_blogger_check_from_scan(
                    db,
                    blogger,
                    active_subscriptions=active_subscriptions,
                    now=now,
                    scan_result=scan_result,
                    found_new_count=len(videos),
                    status=scan_result.status,
                    duration_ms=elapsed_ms(),
                    videos=videos,
                    found_new_for_adaptive=True,
                )
                _clear_blogger_profile_access_failure(blogger)
                _apply_account_success_state(account, now=now)
                _clear_monitor_proxy_success(account.proxy, proxy_obj)
                db.flush()
                try:
                    _enqueue_subscription_outbox_events(db, blogger_id=blogger.id, batches=notify_batches)
                except Exception as _enqueue_err:
                    logger.error("[VIDEO_MONITOR] ok enqueue failed, rolling back: %s", _enqueue_err)
                    db.rollback()
                    raise
                db.commit()
                _reset_blogger_fail_streak(blogger.id)
                return "new_videos"

            except PageBlocked as blocked_error:
                excluded_account_ids.append(account.id)
                blocked_text = str(blocked_error)
                last_failure_reason = f"page_blocked: {blocked_text[:120]}"
                lowered = blocked_text.lower()
                if "suspicious" in lowered and _looks_like_temporary_tiktok_page_error(blocked_text):
                    return await _defer_blogger_for_tiktok_degradation(
                        db,
                        blogger,
                        active_subscriptions=active_subscriptions,
                        now=now,
                        reason=blocked_text,
                    )
                if "suspicious" in lowered:
                    _apply_account_soft_error_state(account, blocked_text, now=now)
                elif any(marker in lowered for marker in ["hard_dead", "login", "redirect to login"]):
                    _apply_account_dead_state(account, blocked_text, now=now)
                else:
                    _apply_account_soft_error_state(account, blocked_text, now=now)
                db.commit()
            except RateLimited as rate_limited_error:
                excluded_account_ids.append(account.id)
                last_failure_reason = f"rate_limited: {str(rate_limited_error)[:120]}"
                if account.proxy and _should_mark_proxy_bad_for_monitor_rate_limit(proxy_obj):
                    mark_proxy_bad(account.proxy, reason=f"monitor_rate_limited: {rate_limited_error}")
                _apply_account_soft_error_state(account, str(rate_limited_error), now=now)
                db.commit()
            except Exception as e:
                excluded_account_ids.append(account.id)
                error_text = str(e)
                last_failure_reason = f"exception: {error_text[:120]}"
                if _should_defer_monitor_network_timeout(error_text, error_type="network_error"):
                    return _defer_blogger_for_navigation_timeout(
                        db,
                        blogger,
                        active_subscriptions=active_subscriptions,
                        now=now,
                        reason=error_text,
                    )
                if _looks_like_network_error(error_text) and await _is_global_network_unavailable():
                    await _defer_blogger_for_global_network_outage(
                        db,
                        blogger,
                        active_subscriptions=active_subscriptions,
                        now=now,
                        reason=error_text,
                    )
                    return "global_network_unavailable"
                if account.proxy and _should_mark_proxy_bad_for_monitor(
                    error_text,
                    error_type="network_error",
                    proxy_obj=proxy_obj,
                ):
                    mark_proxy_bad(account.proxy, reason=f"monitor_error: {error_text[:120]}")
                _apply_account_soft_error_state(account, f"Monitor parsing error: {error_text[:180]}", now=now)
                db.commit()
            finally:
                await _release_proxy_lock_safe(lock_acquired, proxy_obj, account.id if account else None)
                if account and getattr(account, "id", None):
                    release_main_account_reservation(account.id)
                await _close_monitor_resources(page, context)

        failure_streak = await _record_blogger_failure(db, blogger, last_failure_reason)
        _schedule_failed_blogger_check(
            blogger,
            active_subscriptions=active_subscriptions,
            now=now,
            reason=last_failure_reason,
            streak=failure_streak,
        )
        db.commit()
        return "account_pool_exhausted"
    finally:
        if heartbeat_task:
            heartbeat_task.cancel()
            try:
                await heartbeat_task
            except asyncio.CancelledError:
                pass
            except Exception:
                logger.debug("[VIDEO_MONITOR] Lease heartbeat shutdown failed", exc_info=True)

        try:
            release_now = datetime.now(timezone.utc)
            lease_row = (
                db.query(MonitoredBlogger)
                .filter(MonitoredBlogger.id == blogger.id)
                .with_for_update()
                .first()
            )
            if lease_row and lease_row.lock_owner == lock_owner:
                lease_row.lock_until = None
                lease_row.lock_owner = None
                if lease_row.next_check_at is None:
                    _schedule_next_blogger_check(
                        lease_row,
                        active_subscriptions=_get_active_subscriptions_for_blogger(lease_row, active_user_ids),
                        now=release_now,
                    )
                db.commit()
        except Exception:
            db.rollback()
            logger.debug("[VIDEO_MONITOR] Failed to release blogger lease cleanly", exc_info=True)


async def _auto_recover_error_accounts() -> int:
    """
    Reset error_count_recent for accounts that have been stuck in error/soft-error
    state without being used for a while.  Called automatically when the account
    pool is empty for several consecutive ticks so the worker can self-heal without
    manual intervention.

    Returns the number of accounts reset.
    """
    global _last_auto_recovery_at
    now = time.monotonic()
    if now - _last_auto_recovery_at < _AUTO_RECOVERY_INTERVAL_SECONDS:
        return 0

    _last_auto_recovery_at = now
    now_dt = datetime.now(timezone.utc)
    idle_cutoff = now_dt - timedelta(minutes=_AUTO_RECOVERY_IDLE_MINUTES)

    auth_material = or_(
        and_(Account.cookies.isnot(None), Account.cookies != ""),
        and_(Account.cookie_file.isnot(None), Account.cookie_file != ""),
    )

    try:
        with get_sync_db_context() as db:
            # Target: accounts in 'error' or 'active' state with elevated error counts
            # that haven't been used recently.  Exclude 'dead' — those need the
            # validator or manual review.
            candidates = (
                db.query(Account)
                .filter(
                    Account.status.in_(["error", "active"]),
                    Account.error_count_recent > 0,
                    auth_material,
                    or_(
                        Account.last_used_at.is_(None),
                        Account.last_used_at < idle_cutoff,
                    ),
                )
                .all()
            )
            if not candidates:
                logger.info("[AUTO_RECOVERY] No recoverable accounts found")
                return 0

            reset_count = 0
            for acc in candidates:
                acc.error_count_recent = 0
                acc.validation_fail_count = 0
                acc.last_error = None
                if acc.status == "error":
                    acc.status = "active"
                reset_count += 1

            db.commit()
            logger.warning(
                "[AUTO_RECOVERY] Reset error_count for %d idle-error accounts "
                "(idle >%dm) — pool was empty",
                reset_count,
                _AUTO_RECOVERY_IDLE_MINUTES,
            )
            await _send_admin_alert_once(
                key="auto_recovery_triggered",
                message=build_admin_alert(
                    severity="warning",
                    title="Monitor auto-recovery сбросил ошибки аккаунтов",
                    what=f"Сброшены счётчики ошибок у {reset_count} idle-error аккаунтов.",
                    why=(
                        f"Пул monitor-аккаунтов был пуст {_CIRCUIT_BREAKER_FAIL_TICKS} тиков подряд, "
                        "поэтому софт попробовал вернуть в работу мягко ошибочные аккаунты."
                    ),
                    auto_action="Аккаунты будут заново пробоваться на следующих тиках мониторинга.",
                    admin_action="Если инцидент повторится — проверь cookies, dead session и прокси в админке.",
                ),
                cooldown_minutes=60,
            )
            return reset_count
    except Exception as e:
        logger.error("[AUTO_RECOVERY] Failed: %s", e, exc_info=True)
        return 0


def _auto_proxy_rebalance_interval_seconds() -> int:
    raw_value = getattr(
        settings,
        "PROXY_AUTO_REBALANCE_INTERVAL_SECONDS",
        _PROXY_REBALANCE_INTERVAL_SECONDS,
    )
    try:
        return max(0, int(raw_value or 0))
    except (TypeError, ValueError):
        return _PROXY_REBALANCE_INTERVAL_SECONDS


def _compute_recommended_rotation_cooldown_seconds(mobile_proxies: int) -> int:
    """Recommend rotation cooldown based on number of mobile proxies.

    Single mobile proxy is the worst case: every rotation (~25-30s) blocks
    all parallel monitor workers. The more mobile proxies we have, the less
    we need to keep the same IP, so cooldown shrinks. Result is clamped to
    MOBILE_PROXY_ROTATION_COOLDOWN_MIN_SECONDS / MAX_SECONDS.
    """
    if not getattr(settings, "MOBILE_PROXY_ADAPTIVE_COOLDOWN_ENABLED", True):
        return max(0, int(getattr(settings, "MOBILE_PROXY_ROTATION_COOLDOWN_SECONDS", 0)))
    floor = max(0, int(getattr(settings, "MOBILE_PROXY_ROTATION_COOLDOWN_MIN_SECONDS", 60)))
    ceil = max(floor, int(getattr(settings, "MOBILE_PROXY_ROTATION_COOLDOWN_MAX_SECONDS", 240)))
    if mobile_proxies <= 0:
        return ceil
    base_default = max(0, int(getattr(settings, "MOBILE_PROXY_ROTATION_COOLDOWN_SECONDS", 180)))
    if base_default <= 0:
        return 0
    # Inverse scaling: more proxies => smaller cooldown.
    raw = base_default // mobile_proxies
    return max(floor, min(ceil, raw))


def _refresh_proxy_capacity_profile(rebalance_result: dict[str, Any] | None) -> None:
    """Refresh in-memory capacity snapshot and publish rotation cooldown to Redis.

    Called after every successful rebalance. Cheap and idempotent.
    Failures (e.g. Redis unavailable) are non-critical: mobile_proxy_service falls
    back to the default cooldown from settings.
    """
    global _proxy_capacity_profile
    if not rebalance_result:
        return
    mobile = max(0, int(rebalance_result.get("mobile_proxies", 0) or 0))
    residential = max(0, int(rebalance_result.get("residential_proxies", 0) or 0))
    total = max(0, int(rebalance_result.get("active_proxies", 0) or 0)) or (mobile + residential)
    # Each mobile proxy can comfortably handle 1 worker (locks serialize anyway).
    # Each residential proxy has no per-proxy lock and can take 2 workers without contention.
    proxy_concurrency_cap = max(1, mobile + residential * 2) if (mobile + residential) > 0 else 1
    cooldown_seconds = _compute_recommended_rotation_cooldown_seconds(mobile)
    previous = dict(_proxy_capacity_profile)
    _proxy_capacity_profile = {
        "total_proxies": total,
        "mobile_proxies": mobile,
        "residential_proxies": residential,
        "concurrency_cap": proxy_concurrency_cap,
        "rotation_cooldown_seconds": cooldown_seconds,
    }
    try:
        from services.redis_service import get_redis_client
        redis_client = get_redis_client()
        redis_client.set("monitor:mobile_proxy:rotation_cooldown_seconds", cooldown_seconds, ex=3600)
    except Exception as redis_err:
        logger.debug("Failed to publish rotation cooldown to Redis (non-critical): %s", redis_err)
    if previous != _proxy_capacity_profile:
        logger.info(
            "[VIDEO_MONITOR] Proxy capacity profile: total=%s mobile=%s residential=%s "
            "-> concurrency_cap=%s rotation_cooldown=%ss",
            total,
            mobile,
            residential,
            proxy_concurrency_cap,
            cooldown_seconds,
        )


def _proxy_capacity_concurrency_cap() -> int:
    """Read latest concurrency cap from the cached profile (defaults to 1)."""
    cap = int(_proxy_capacity_profile.get("concurrency_cap", 0) or 0)
    cap = cap if cap > 0 else 1
    if _mobile_proxy_pressure_guard_active():
        guard_cap = _monitor_setting_int(
            "MONITOR_PROXY_PRESSURE_CONCURRENCY_CAP",
            1,
            minimum=1,
        )
        return max(1, min(cap, guard_cap))
    return cap


def _mobile_proxy_pressure_guard_key() -> str:
    return "monitor:mobile_proxy_pressure_guard_until"


def _mobile_proxy_pressure_guard_active() -> bool:
    now_monotonic = time.monotonic()
    if _proxy_pressure_guard_until > now_monotonic:
        return True
    redis_client = _get_monitor_redis()
    if not redis_client:
        return False
    try:
        raw = redis_client.get(_mobile_proxy_pressure_guard_key())
        if raw is None:
            return False
        guard_until_epoch = float(raw)
        return guard_until_epoch > time.time()
    except Exception as exc:
        logger.debug("[VIDEO_MONITOR] Failed to read proxy pressure guard: %s", exc)
        return False


def _activate_mobile_proxy_pressure_guard(duration_seconds: int | None = None) -> int:
    global _proxy_pressure_guard_until
    seconds = duration_seconds
    if seconds is None:
        seconds = _monitor_setting_int("MONITOR_PROXY_PRESSURE_GUARD_SECONDS", 600, minimum=60)
    seconds = max(60, int(seconds or 60))
    _proxy_pressure_guard_until = time.monotonic() + seconds
    redis_client = _get_monitor_redis()
    if redis_client:
        try:
            redis_client.set(
                _mobile_proxy_pressure_guard_key(),
                time.time() + seconds,
                ex=seconds,
            )
        except Exception as exc:
            logger.debug("[VIDEO_MONITOR] Failed to publish proxy pressure guard: %s", exc)
    return seconds


def _clear_mobile_proxy_pressure_guard() -> int:
    global _proxy_pressure_guard_until
    _proxy_pressure_guard_until = 0.0
    redis_client = _get_monitor_redis()
    if redis_client:
        try:
            return int(redis_client.delete(_mobile_proxy_pressure_guard_key()) or 0)
        except Exception as exc:
            logger.debug("[VIDEO_MONITOR] Failed to clear proxy pressure guard: %s", exc)
    return 0


def _should_activate_mobile_proxy_pressure_guard(tick_summary: dict[str, int], processed_count: int) -> bool:
    deferred_proxy = int(tick_summary.get("deferred_proxy", 0) or 0)
    threshold = _monitor_setting_int("MONITOR_PROXY_PRESSURE_DEFER_THRESHOLD", 3, minimum=1)
    if deferred_proxy < threshold:
        return False
    ratio_threshold = _monitor_setting_float("MONITOR_PROXY_PRESSURE_DEFER_RATIO", 0.4, minimum=0.0)
    processed = max(1, int(processed_count or 0))
    return (deferred_proxy / processed) >= ratio_threshold


async def _handle_mobile_proxy_pressure(
    *,
    tick_summary: dict[str, int],
    processed_count: int,
    due_backlog: int,
    runtime_snapshot: dict[str, Any],
) -> None:
    if not _should_activate_mobile_proxy_pressure_guard(tick_summary, processed_count):
        return

    guard_seconds = _activate_mobile_proxy_pressure_guard()
    deferred_proxy = int(tick_summary.get("deferred_proxy", 0) or 0)
    checked = int(tick_summary.get("checked", 0) or 0)
    proxy_cap = _proxy_capacity_profile.get("concurrency_cap", 0)
    mobile = _proxy_capacity_profile.get("mobile_proxies", 0)
    residential = _proxy_capacity_profile.get("residential_proxies", 0)
    evidence = [
        f"processed={processed_count}",
        f"checked={checked}",
        f"deferred_proxy={deferred_proxy}",
        f"due_backlog={due_backlog}",
        f"proxy_cap={proxy_cap}",
        f"mobile={mobile}",
        f"residential={residential}",
    ]
    record_incident(
        INCIDENT_MOBILE_PROXY_PRESSURE,
        severity="warning",
        title="Monitor mobile proxy pressure",
        cause="Too many blogger checks were deferred because mobile proxies were busy.",
        evidence=evidence,
        auto_action=f"Temporarily capped monitor concurrency for {guard_seconds}s and rescheduled proxy-busy checks.",
        admin_action="Watch the next health tick. If pressure repeats, add mobile proxies or lower monitor concurrency.",
        affected=runtime_snapshot.get("due_preview") or [],
        ttl_minutes=max(10, int(math.ceil(guard_seconds / 60))),
    )
    await _send_admin_alert_once(
        key="mobile_proxy_pressure",
        message=build_admin_alert(
            severity="warning",
            title="Monitor mobile proxy pressure",
            what=(
                f"{deferred_proxy}/{processed_count} checks were deferred because mobile proxies were busy. "
                f"Only {checked} completed in this tick."
            ),
            why=(
                f"Current proxy capacity profile: mobile={mobile}, residential={residential}, "
                f"cap={proxy_cap}, due_backlog={due_backlog}."
            ),
            auto_action=f"Soft capped monitor concurrency for {guard_seconds}s and will retry deferred bloggers without fail-streak.",
            admin_action="If this alert repeats, add mobile proxies or reduce configured monitor concurrency.",
            affected=runtime_snapshot.get("due_preview") or [],
        ),
        cooldown_minutes=_monitor_setting_int(
            "MONITOR_PROXY_PRESSURE_ALERT_COOLDOWN_MINUTES",
            30,
            minimum=1,
        ),
    )


def _maybe_rebalance_working_account_proxies(db: Session) -> dict[str, Any] | None:
    """Periodically keep only currently working accounts balanced across proxies."""
    global _last_proxy_rebalance_at

    interval_seconds = _auto_proxy_rebalance_interval_seconds()
    if interval_seconds <= 0:
        return None

    now_monotonic = time.monotonic()
    if now_monotonic - _last_proxy_rebalance_at < interval_seconds:
        return None

    _last_proxy_rebalance_at = now_monotonic
    try:
        result = rebalance_working_account_proxies(
            db,
            mobile_weight=getattr(settings, "PROXY_REBALANCE_MOBILE_WEIGHT", 5),
        )
        try:
            _refresh_proxy_capacity_profile(result)
        except Exception as profile_err:
            logger.debug("_refresh_proxy_capacity_profile failed (non-critical): %s", profile_err)
        if result.get("eligible_accounts") or result.get("changed"):
            logger.info(
                "[VIDEO_MONITOR] Auto proxy rebalance: working=%s changed=%s "
                "mobile=%s residential=%s targets=%s",
                result.get("eligible_accounts", 0),
                result.get("changed", 0),
                result.get("mobile_proxies", 0),
                result.get("residential_proxies", 0),
                result.get("targets_by_proxy_id", {}),
            )
        return result
    except Exception as rebalance_error:
        try:
            db.rollback()
        except Exception:
            pass
        logger.warning("[VIDEO_MONITOR] Auto proxy rebalance failed: %s", rebalance_error, exc_info=True)
        return None


async def run_monitor_loop() -> None:
    """
    Main monitoring loop for shared TikTok blogger checks.
    """
    global _consecutive_fail_ticks, _circuit_open_until
    _install_monitor_asyncio_exception_handler()
    logger.info("[VIDEO_MONITOR] Starting Unified Video Monitor (v2)...")
    set_gauge("monitor.queue_len", 0)
    update_monitor_heartbeat(
        "starting",
        max_parallel_browsers=max(1, int(settings.MONITOR_MAX_PARALLEL_BROWSERS)),
    )

    # Auto-apply pending DB migrations: на свежей БД без ручного запуска
    # `python -m db.migrations.add_adaptive_monitoring_migration` — упадёт ORM
    # на первой же query из-за отсутствия колонок posting_stats / latency_ema_minutes.
    try:
        from db.migrations import apply_pending_migrations
        applied = apply_pending_migrations(verbose=False)
        if applied:
            logger.info("[VIDEO_MONITOR] Startup: applied %d pending DB migration(s)", applied)
    except Exception as _migrate_err:
        logger.error("[VIDEO_MONITOR] Migration step failed (continuing): %s", _migrate_err, exc_info=True)

    # Aggressive startup cleanup: освобождаем все блогер-locks от предыдущих запусков.
    # На laptop один воркер → ничьи locks нам не мешают, их можно сбрасывать.
    # Без этого после краша воркера блогеры висят залоченными до STALE_LOCK_MINUTES.
    try:
        with get_sync_db_context() as _db_init:
            _released = (
                _db_init.query(MonitoredBlogger)
                .filter(MonitoredBlogger.lock_until.isnot(None))
                .update(
                    {MonitoredBlogger.lock_until: None, MonitoredBlogger.lock_owner: None},
                    synchronize_session=False,
                )
            )
            _repaired = 0
            _stats_repaired = 0
            _stats_recomputed = 0
            _released_proxy_locks = 0
            _cleared_proxy_pressure_guard = 0
            try:
                _adaptive_enabled, _adaptive_settings, _ = _load_adaptive_runtime_settings(_db_init)
                _repaired = _repair_far_future_blogger_checks(
                    _db_init,
                    now=datetime.now(timezone.utc),
                    adaptive_settings=_adaptive_settings,
                )
                _stats_repaired = _repair_orphan_adaptive_stats(
                    _db_init,
                    now=datetime.now(timezone.utc),
                    adaptive_settings=_adaptive_settings,
                )
                if _adaptive_enabled:
                    _stats_recomputed = _recompute_stale_adaptive_stats(
                        _db_init,
                        now=datetime.now(timezone.utc),
                        adaptive_settings=_adaptive_settings,
                    )
            except Exception as _repair_err:
                logger.warning("[VIDEO_MONITOR] Startup next_check_at repair failed (non-critical): %s", _repair_err)
            try:
                mobile_proxy_ids = [
                    proxy_id
                    for (proxy_id,) in (
                        _db_init.query(Proxy.id)
                        .filter(
                            Proxy.is_mobile == "Y",
                            Proxy.rotation_url.isnot(None),
                            Proxy.rotation_url != "",
                        )
                        .all()
                    )
                ]
                _released_proxy_locks = clear_proxy_locks(mobile_proxy_ids)
                _cleared_proxy_pressure_guard = _clear_mobile_proxy_pressure_guard()
            except Exception as _proxy_lock_cleanup_err:
                logger.warning(
                    "[VIDEO_MONITOR] Startup mobile proxy lock cleanup failed (non-critical): %s",
                    _proxy_lock_cleanup_err,
                )
            _db_init.commit()
            if _released:
                logger.info("[VIDEO_MONITOR] Startup: released %d stale blogger lock(s) from previous run", _released)
            if _released_proxy_locks:
                logger.info("[VIDEO_MONITOR] Startup: released %d stale mobile proxy lock(s) from previous run", _released_proxy_locks)
            if _cleared_proxy_pressure_guard:
                logger.info("[VIDEO_MONITOR] Startup: cleared stale mobile proxy pressure guard from previous run")
            if _repaired:
                logger.warning("[VIDEO_MONITOR] Startup: repaired %d far-future blogger check(s)", _repaired)
            if _stats_repaired:
                logger.warning("[VIDEO_MONITOR] Startup: cleared %d orphan adaptive stat record(s)", _stats_repaired)
            if _stats_recomputed:
                logger.warning("[VIDEO_MONITOR] Startup: recomputed %d stale/feedback adaptive stat record(s)", _stats_recomputed)
    except Exception as _startup_err:
        logger.warning("[VIDEO_MONITOR] Startup lock cleanup failed (non-critical): %s", _startup_err)

    while True:
        try:
            # --- Circuit breaker check ---
            if time.monotonic() < _circuit_open_until:
                remaining = int(_circuit_open_until - time.monotonic())
                commenting_active, _commenting_info = _monitor_commenting_throttle_state()
                commenting_pressure = _monitor_commenting_pressure_state(commenting_active)
                commenting_monitor_mode = str(commenting_pressure.get("mode") or "normal")
                commenting_outbox_limit = _monitor_outbox_limit_for_commenting(commenting_active, commenting_monitor_mode)
                commenting_defer_autopilot_outbox = _monitor_defer_autopilot_outbox_for_commenting(commenting_active)
                logger.warning(
                    "[VIDEO_MONITOR] Circuit breaker OPEN — skipping checks for %ds "
                    "(pool was empty %d consecutive ticks)",
                    remaining,
                    _consecutive_fail_ticks,
                )
                update_monitor_heartbeat(
                    "circuit_open",
                    max_parallel_browsers=max(1, int(settings.MONITOR_MAX_PARALLEL_BROWSERS)),
                    executor_active=commenting_active,
                    monitor_mode=commenting_monitor_mode,
                )
                await _process_monitor_outbox(
                    limit=commenting_outbox_limit,
                    include_autopilot_create=not commenting_defer_autopilot_outbox,
                )
                await asyncio.sleep(CHECK_TICK)
                continue

            now = datetime.now(timezone.utc)
            active_user_ids: set[int] = set()
            due_bloggers: list[MonitoredBlogger] = []
            due_backlog = 0
            batch_limit = MAX_CREATORS_PER_TICK
            runtime_snapshot: dict[str, Any] = {}
            health_snapshot: dict[str, Any] = {}
            max_parallel_browsers = max(1, int(settings.MONITOR_MAX_PARALLEL_BROWSERS))
            commenting_active, commenting_info = _monitor_commenting_throttle_state()
            commenting_pressure = _monitor_commenting_pressure_state(commenting_active)
            commenting_monitor_mode = str(commenting_pressure.get("mode") or "normal")
            commenting_load = commenting_pressure.get("load") if isinstance(commenting_pressure, dict) else {}
            if not isinstance(commenting_load, dict):
                commenting_load = {}
            commenting_outbox_limit = _monitor_outbox_limit_for_commenting(commenting_active, commenting_monitor_mode)
            commenting_defer_autopilot_outbox = _monitor_defer_autopilot_outbox_for_commenting(commenting_active)

            _maybe_cleanup_streak_dicts()

            with get_sync_db_context() as db:
                max_parallel_browsers = get_monitor_max_parallel_browsers(db)
                stale_threshold = now - timedelta(minutes=STALE_LOCK_MINUTES)
                stale_locks = (
                    db.query(MonitoredBlogger)
                    .filter(
                        MonitoredBlogger.lock_until.isnot(None),
                        MonitoredBlogger.lock_until < stale_threshold,
                    )
                    .all()
                )
                if stale_locks:
                    for stale_blogger in stale_locks:
                        logger.warning(
                            f"[VIDEO_MONITOR] Clearing stale lock for @{stale_blogger.username} "
                            f"(lock_until={stale_blogger.lock_until}, lock_owner={stale_blogger.lock_owner})"
                        )
                        stale_blogger.lock_until = None
                        stale_blogger.lock_owner = None
                    db.commit()

                _maybe_rebalance_working_account_proxies(db)

                if not settings.DEMO_MODE:
                    active_user_ids = _get_active_user_ids(db, now)

                runtime_snapshot = get_monitor_runtime_snapshot(
                    db,
                    now=now,
                    active_user_ids=None if settings.DEMO_MODE else active_user_ids,
                )
                health_snapshot = get_monitor_health_snapshot(
                    db,
                    now=now,
                    active_user_ids=None if settings.DEMO_MODE else active_user_ids,
                    runtime_snapshot=runtime_snapshot,
                )
                due_query = _build_due_bloggers_query(
                    db,
                    now=now,
                    active_user_ids=None if settings.DEMO_MODE else active_user_ids,
                )
                due_backlog = runtime_snapshot["due_backlog"]
                batch_limit = _monitor_max_bloggers_per_tick(max_parallel_browsers)
                batch_limit, _ = _apply_commenting_active_monitor_limits(
                    batch_limit=batch_limit,
                    effective_concurrency=max(1, int(settings.MONITOR_BLOGGER_CONCURRENCY)),
                    commenting_active=commenting_active,
                    commenting_mode=commenting_monitor_mode,
                )
                due_bloggers = [] if batch_limit <= 0 else due_query.limit(batch_limit).all()

            set_gauge("monitor.queue_len", due_backlog)
            set_gauge("monitor.oldest_overdue_minutes", runtime_snapshot.get("oldest_overdue_minutes", 0))
            set_gauge("monitor.unscheduled_due", runtime_snapshot.get("unscheduled_due", 0))
            set_gauge("monitor.never_successful_due", runtime_snapshot.get("never_successful_due", 0))
            set_gauge(
                "monitor.next_due_in_minutes",
                runtime_snapshot.get("next_due_in_minutes")
                if runtime_snapshot.get("next_due_in_minutes") is not None
                else -1,
            )
            set_gauge("monitor.outbox.ready", runtime_snapshot.get("outbox_ready", 0))
            set_gauge("monitor.outbox.pending", runtime_snapshot.get("outbox_pending", 0))
            set_gauge("monitor.outbox.failed", runtime_snapshot.get("outbox_failed", 0))
            set_gauge("monitor.outbox.processing", runtime_snapshot.get("outbox_processing", 0))
            set_gauge(
                "monitor.outbox.oldest_ready_minutes",
                runtime_snapshot.get("oldest_outbox_ready_minutes", 0),
            )
            set_gauge("monitor.executor_active", 1 if commenting_active else 0)
            set_gauge("monitor.commenting_throttle_paused", 1 if commenting_monitor_mode == "paused" else 0)
            load_cpu = _monitor_optional_float(commenting_load.get("cpu_percent"))
            load_memory = _monitor_optional_float(commenting_load.get("memory_percent"))
            if load_cpu is not None:
                set_gauge("monitor.local_cpu_percent", load_cpu)
            if load_memory is not None:
                set_gauge("monitor.local_memory_percent", load_memory)
            if health_snapshot and _should_log_monitor_health_summary(time.monotonic()):
                logger.info(_format_monitor_health_summary(health_snapshot))
            if health_snapshot:
                try:
                    await _maybe_send_monitor_pool_alert(health_snapshot)
                except Exception as _alert_err:
                    logger.warning("[VIDEO_MONITOR] Pool alert failed (non-critical): %s", _alert_err)
            update_monitor_heartbeat(
                "ok",
                max_parallel_browsers=max_parallel_browsers,
                executor_active=commenting_active,
                monitor_mode=commenting_monitor_mode,
                local_cpu_percent=load_cpu,
                local_memory_percent=load_memory,
            )

            if due_bloggers:
                if due_backlog > batch_limit:
                    logger.warning(
                        f"[VIDEO_MONITOR] Due backlog {due_backlog} exceeds tick limit {batch_limit}; "
                        f"processing earliest due bloggers first "
                        f"(oldest_overdue={runtime_snapshot.get('oldest_overdue_minutes', 0)}m, "
                        f"unscheduled={runtime_snapshot.get('unscheduled_due', 0)}, "
                        f"preview={runtime_snapshot.get('due_preview', [])})"
                    )

                # Dynamic concurrency: scale up when backlog is building (cap = max_parallel_browsers из БД / .env).
                parallel_cap = max(1, int(max_parallel_browsers))
                base_concurrency = max(1, min(parallel_cap, int(settings.MONITOR_BLOGGER_CONCURRENCY)))
                if due_backlog >= _DYNAMIC_CONCURRENCY_BACKLOG_HIGH:
                    effective_concurrency = parallel_cap
                elif due_backlog >= _DYNAMIC_CONCURRENCY_BACKLOG_LOW:
                    effective_concurrency = min(parallel_cap, base_concurrency * 2)
                else:
                    effective_concurrency = base_concurrency
                if commenting_active:
                    before_concurrency = effective_concurrency
                    batch_limit, effective_concurrency = _apply_commenting_active_monitor_limits(
                        batch_limit=batch_limit,
                        effective_concurrency=effective_concurrency,
                        commenting_active=True,
                        commenting_mode=commenting_monitor_mode,
                    )
                    logger.info(
                        "[VIDEO_MONITOR] Commenting active: monitor %s mode "
                        "(batch_limit=%s, concurrency=%s->%s, outbox_limit=%s, defer_autopilot=%s, cpu=%s, memory=%s, reason=%s, executor_lock_ttl=%s)",
                        commenting_monitor_mode,
                        batch_limit,
                        before_concurrency,
                        effective_concurrency,
                        commenting_outbox_limit,
                        commenting_defer_autopilot_outbox,
                        commenting_load.get("cpu_percent"),
                        commenting_load.get("memory_percent"),
                        commenting_pressure.get("reason") if isinstance(commenting_pressure, dict) else None,
                        commenting_info.get("lock_ttl") if isinstance(commenting_info, dict) else None,
                    )
                # Hard cap by available proxies. With a single mobile proxy, running 2
                # workers in parallel just makes one of them wait for the proxy lock for 30s.
                if getattr(settings, "MONITOR_ADAPTIVE_CONCURRENCY_BY_PROXIES", True):
                    proxy_cap = _proxy_capacity_concurrency_cap()
                    if proxy_cap > 0 and proxy_cap < effective_concurrency:
                        logger.debug(
                            "[VIDEO_MONITOR] Capping concurrency by proxy capacity: "
                            "%s -> %s (mobile=%s residential=%s)",
                            effective_concurrency,
                            proxy_cap,
                            _proxy_capacity_profile.get("mobile_proxies", 0),
                            _proxy_capacity_profile.get("residential_proxies", 0),
                        )
                        effective_concurrency = proxy_cap
                concurrency = max(1, min(len(due_bloggers), effective_concurrency))
                logger.info(
                    f"[VIDEO_MONITOR] Processing {len(due_bloggers)} bloggers due for check "
                    f"(backlog={due_backlog}, concurrency={concurrency}, "
                    f"proxy_cap={_proxy_capacity_concurrency_cap()})"
                )
                semaphore = asyncio.Semaphore(concurrency)

                async def process_with_semaphore(blogger_to_process: MonitoredBlogger):
                    async with semaphore:
                        timeout_sec = getattr(settings, "BLOGGER_CHECK_TIMEOUT_SECONDS", 180)
                        started_monotonic = time.perf_counter()
                        try:
                            with get_sync_db_context() as db_local:
                                local_blogger = (
                                    db_local.query(MonitoredBlogger)
                                    .options(selectinload(MonitoredBlogger.subscriptions))
                                    .filter(MonitoredBlogger.id == blogger_to_process.id)
                                    .first()
                                )
                                if not local_blogger:
                                    logger.warning(
                                        f"[VIDEO_MONITOR] Blogger id={blogger_to_process.id} disappeared before processing"
                                    )
                                    return None
                                result_tag = await asyncio.wait_for(
                                    process_blogger(db_local, local_blogger, active_user_ids=active_user_ids),
                                    timeout=timeout_sec,
                                )
                                normalized_result = result_tag or "completed"
                                duration_ms = max(0.0, (time.perf_counter() - started_monotonic) * 1000.0)
                                increment_counter(
                                    "monitor.blogger_check",
                                    tags={"result": normalized_result},
                                )
                                record_timing(
                                    "monitor.blogger_check",
                                    duration_ms,
                                    tags={"result": normalized_result},
                                )
                                if normalized_result in {
                                    "empty_profile_valid",
                                    "no_normal_videos",
                                    "resync_anchor_repaired",
                                    "ok_no_new",
                                    "initial_sync",
                                    "new_videos",
                                }:
                                    _record_monitor_navigation_success()
                                return normalized_result
                        except asyncio.TimeoutError:
                            logger.error(
                                f"[VIDEO_MONITOR] Timeout checking @{blogger_to_process.username} after {timeout_sec}s"
                            )
                            timeout_stats = _record_blogger_timeout_event(
                                blogger_to_process.id,
                                blogger_to_process.username,
                                now=datetime.now(timezone.utc),
                            )
                            increment_counter("monitor.blogger_check", tags={"result": "timeout"})
                            record_timing(
                                "monitor.blogger_check",
                                float(timeout_sec) * 1000.0,
                                tags={"result": "timeout"},
                            )
                            await _maybe_send_blogger_timeout_alert(
                                blogger_to_process,
                                timeout_sec=timeout_sec,
                                timeout_stats=timeout_stats,
                            )
                            try:
                                with get_sync_db_context() as db_timeout:
                                    timeout_blogger = (
                                        db_timeout.query(MonitoredBlogger)
                                        .options(selectinload(MonitoredBlogger.subscriptions))
                                        .filter(MonitoredBlogger.id == blogger_to_process.id)
                                        .first()
                                    )
                                    if timeout_blogger:
                                        failure_streak = await _record_blogger_failure(db_timeout, timeout_blogger, "timeout")
                                        timeout_blogger.last_attempt_at = datetime.now(timezone.utc)
                                        timeout_blogger.last_checked_at = timeout_blogger.last_attempt_at
                                        timeout_blogger.lock_until = None
                                        timeout_blogger.lock_owner = None
                                        _schedule_failed_blogger_check(
                                            timeout_blogger,
                                            active_subscriptions=_get_active_subscriptions_for_blogger(
                                                timeout_blogger,
                                                active_user_ids,
                                            ),
                                            now=timeout_blogger.last_attempt_at,
                                            reason="timeout",
                                            streak=failure_streak,
                                        )
                                        db_timeout.commit()
                            except Exception as timeout_mark_error:
                                logger.warning(
                                    f"[VIDEO_MONITOR] Failed to mark timeout failure for blogger {blogger_to_process.id}: "
                                    f"{timeout_mark_error}"
                            )
                            return None
                        except Exception as process_error:
                            error_text = str(process_error)
                            if _looks_like_local_browser_infrastructure_error(error_text):
                                await _maybe_reset_browser_pool_after_local_issue(
                                    now=datetime.now(timezone.utc),
                                    reason=error_text,
                                )
                                increment_counter("monitor.blogger_check", tags={"result": "local_browser_unavailable"})
                                return "local_browser_unavailable"
                            duration_ms = max(0.0, (time.perf_counter() - started_monotonic) * 1000.0)
                            increment_counter("monitor.blogger_check", tags={"result": "error"})
                            record_timing(
                                "monitor.blogger_check",
                                duration_ms,
                                tags={"result": "error"},
                            )
                            logger.error(
                                f"[VIDEO_MONITOR] Unexpected error checking @{blogger_to_process.username}: {process_error}",
                                exc_info=True,
                            )
                            return None

                tick_results = await asyncio.gather(
                    *(process_with_semaphore(blogger) for blogger in due_bloggers),
                    return_exceptions=True,
                )
                tick_summary = _summarize_tick_results(tick_results)
                logger.info(
                    "[VIDEO_MONITOR] Processed %d bloggers | checked=%d deferred_storm=%d "
                    "deferred_timeout=%d deferred_proxy=%d deferred_browser=%d no_accounts=%d errors=%d",
                    len(due_bloggers),
                    tick_summary["checked"],
                    tick_summary["deferred_storm"],
                    tick_summary["deferred_timeout"],
                    tick_summary["deferred_proxy"],
                    tick_summary["deferred_browser"],
                    tick_summary["no_accounts"],
                    tick_summary["errors"],
                )
                await _handle_mobile_proxy_pressure(
                    tick_summary=tick_summary,
                    processed_count=len(due_bloggers),
                    due_backlog=due_backlog,
                    runtime_snapshot=runtime_snapshot,
                )

                # --- Circuit breaker & auto-recovery ---
                _pool_fail_results = {"no_accounts_available", "account_pool_exhausted"}
                pool_fail_count = sum(
                    1 for r in tick_results
                    if isinstance(r, str) and r in _pool_fail_results
                )
                any_success = any(
                    isinstance(r, str) and r not in _pool_fail_results
                    for r in tick_results
                    if r is not None
                )
                if pool_fail_count == len(due_bloggers) and not any_success:
                    _consecutive_fail_ticks += 1
                    logger.warning(
                        "[VIDEO_MONITOR] All %d blogger checks failed (pool empty) — "
                        "consecutive empty ticks: %d/%d",
                        len(due_bloggers),
                        _consecutive_fail_ticks,
                        _CIRCUIT_BREAKER_FAIL_TICKS,
                    )
                    if _consecutive_fail_ticks >= _CIRCUIT_BREAKER_FAIL_TICKS:
                        _circuit_open_until = (
                            time.monotonic() + _CIRCUIT_BREAKER_OPEN_MINUTES * 60
                        )
                        _consecutive_fail_ticks = 0
                        logger.error(
                            "[VIDEO_MONITOR] Circuit breaker OPENED for %d min — "
                            "triggering account auto-recovery",
                            _CIRCUIT_BREAKER_OPEN_MINUTES,
                        )
                        await _auto_recover_error_accounts()
                else:
                    _consecutive_fail_ticks = 0
            else:
                if commenting_active and commenting_monitor_mode == "paused" and due_backlog > 0:
                    logger.warning(
                        "[VIDEO_MONITOR] Commenting load is high: paused monitor browser checks for this tick "
                        "(due_backlog=%s, cpu=%s, memory=%s, reason=%s)",
                        due_backlog,
                        commenting_load.get("cpu_percent"),
                        commenting_load.get("memory_percent"),
                        commenting_pressure.get("reason") if isinstance(commenting_pressure, dict) else None,
                    )
                else:
                    logger.info("[VIDEO_MONITOR] No bloggers due for check")

            processed_outbox = await _process_monitor_outbox(
                limit=commenting_outbox_limit,
                include_autopilot_create=not commenting_defer_autopilot_outbox,
            )
            if processed_outbox:
                logger.info(f"[VIDEO_MONITOR] Processed {processed_outbox} outbox events")

            update_monitor_heartbeat(
                "ok",
                max_parallel_browsers=max_parallel_browsers,
                executor_active=commenting_active,
                monitor_mode=commenting_monitor_mode,
                local_cpu_percent=load_cpu,
                local_memory_percent=load_memory,
            )
        except Exception as e:
            logger.error(f"[VIDEO_MONITOR] Loop error: {e}", exc_info=True)
            update_monitor_heartbeat("error")
            await asyncio.sleep(CHECK_TICK)
            continue

        sleep_seconds = _monitor_sleep_seconds_after_tick(
            due_backlog=due_backlog,
            processed_count=len(due_bloggers),
        )
        if commenting_active and commenting_monitor_mode == "paused":
            sleep_seconds = max(
                sleep_seconds,
                _monitor_setting_int("MONITOR_COMMENTING_ACTIVE_PAUSE_SLEEP_SECONDS", 90, minimum=CHECK_TICK),
            )
        await asyncio.sleep(sleep_seconds)

def main():
    import sys
    from logging.handlers import RotatingFileHandler
    from pathlib import Path

    log_format = '%(asctime)s - %(name)s - %(levelname)s - %(message)s'
    formatter = logging.Formatter(log_format)

    # Stream handler — keep stdout output for `python -m services.video_monitor_worker`
    stream_handler = logging.StreamHandler(sys.stdout)
    stream_handler.setFormatter(formatter)

    # Rotating file handler: 10 MB / file, 3 backups (~40 MB ceiling).
    # Prevents "0 bytes free disk" recurrence by capping log growth.
    handlers: list[logging.Handler] = [stream_handler]
    try:
        log_dir = Path("logs")
        log_dir.mkdir(exist_ok=True)
        file_handler = RotatingFileHandler(
            log_dir / "video_monitor.log",
            maxBytes=10 * 1024 * 1024,
            backupCount=3,
            encoding="utf-8",
        )
        file_handler.setFormatter(formatter)
        handlers.append(file_handler)
    except Exception as log_setup_err:
        sys.stderr.write("[boot] log file setup failed (stdout only): " + str(log_setup_err) + "\n")

    logging.basicConfig(level=logging.INFO, format=log_format, handlers=handlers)
    logging.getLogger("video.monitor").setLevel(logging.DEBUG)
    try:
        asyncio.run(run_monitor_loop())
    except KeyboardInterrupt:
        pass

if __name__ == "__main__":
    main()
