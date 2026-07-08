"""
Сервис управления аккаунтами.

Логика обработки ошибок:
- Временные ошибки (прокси, page_load) увеличивают error_count_recent
- Аккаунт становится "error" только при error_count_recent >= ERROR_THRESHOLD
- Успешные операции сбрасывают error_count_recent
- "dead" ставится только при явных ошибках авторизации

Гео-приоритизация:
- Если PROXY_GEO_MATCHING=True, аккаунты с совпадающим country_code (аккаунт и прокси)
  имеют приоритет при выборе для снижения вероятности капчи.
"""
import logging
import threading
import time
import hashlib
from typing import Any, Iterable
import random # NEW: For jitter
import json  # ИСПРАВЛЕНО: Для проверки cooldown в last_error
from pathlib import Path
from datetime import datetime, timezone, timedelta
from urllib.parse import urlparse
from sqlalchemy import and_, or_, func, case
from sqlalchemy.orm import Session
from db.models import Account, Proxy
from config.settings import get_settings
from services.proxy_filter_utils import get_proxy_type_filter
from services.app_settings_service import get_proxy_prefer_mobile
from services.proxy_health_service import get_bad_proxy_set, is_proxy_bad
from services.mobile_proxy_service import get_locked_proxy_ids
from services.cookie_validator import parse_cookies_to_dict, validate_cookies

logger = logging.getLogger("executor.account_service")
settings = get_settings()

# Используем настройку из settings
ERROR_THRESHOLD = settings.ERROR_THRESHOLD

# Process-local reservation for main-comment accounts.
# SQLite SELECT FOR UPDATE does not protect us from two async workers selecting
# the same account while the first one is still publishing. This reservation
# lives inside the executor process and prevents that race.
_MAIN_ACCOUNT_RESERVATION_LOCK = threading.Lock()
_MAIN_ACCOUNT_RESERVED_UNTIL: dict[int, float] = {}
_MAIN_ACCOUNT_RESERVATION_TTL_SECONDS = 180.0


def _cleanup_main_account_reservations_locked() -> None:
    now_monotonic = time.monotonic()
    expired = [
        account_id
        for account_id, reserved_until in _MAIN_ACCOUNT_RESERVED_UNTIL.items()
        if reserved_until <= now_monotonic
    ]
    for account_id in expired:
        _MAIN_ACCOUNT_RESERVED_UNTIL.pop(account_id, None)


def get_reserved_main_account_ids() -> set[int]:
    with _MAIN_ACCOUNT_RESERVATION_LOCK:
        _cleanup_main_account_reservations_locked()
        return set(_MAIN_ACCOUNT_RESERVED_UNTIL.keys())


def reserve_main_account(account_id: int, ttl_seconds: float = _MAIN_ACCOUNT_RESERVATION_TTL_SECONDS) -> bool:
    with _MAIN_ACCOUNT_RESERVATION_LOCK:
        _cleanup_main_account_reservations_locked()
        if account_id in _MAIN_ACCOUNT_RESERVED_UNTIL:
            return False
        _MAIN_ACCOUNT_RESERVED_UNTIL[account_id] = time.monotonic() + ttl_seconds
        return True


def release_main_account_reservation(account_id: int) -> None:
    with _MAIN_ACCOUNT_RESERVATION_LOCK:
        _MAIN_ACCOUNT_RESERVED_UNTIL.pop(account_id, None)


def extend_main_account_reservation(account_id: int, ttl_seconds: float) -> bool:
    """Extend an existing process-local main-account lease.

    The strict 6x1 coordinator can keep a browser alive for up to its 300s
    worker deadline, which is longer than the default 180s selection lease.
    Only an already-reserved account can be extended.
    """
    with _MAIN_ACCOUNT_RESERVATION_LOCK:
        _cleanup_main_account_reservations_locked()
        if account_id not in _MAIN_ACCOUNT_RESERVED_UNTIL:
            return False
        _MAIN_ACCOUNT_RESERVED_UNTIL[account_id] = (
            time.monotonic() + max(1.0, float(ttl_seconds))
        )
        return True


_REPLY_ACCOUNT_RESERVATION_LOCK = threading.Lock()
_REPLY_ACCOUNT_RESERVED_UNTIL: dict[int, float] = {}
_REPLY_ACCOUNT_RESERVATION_TTL_SECONDS = 180.0


def _cleanup_reply_account_reservations_locked() -> None:
    now_monotonic = time.monotonic()
    expired = [
        account_id
        for account_id, reserved_until in _REPLY_ACCOUNT_RESERVED_UNTIL.items()
        if reserved_until <= now_monotonic
    ]
    for account_id in expired:
        _REPLY_ACCOUNT_RESERVED_UNTIL.pop(account_id, None)


def get_reserved_reply_account_ids() -> set[int]:
    with _REPLY_ACCOUNT_RESERVATION_LOCK:
        _cleanup_reply_account_reservations_locked()
        return set(_REPLY_ACCOUNT_RESERVED_UNTIL.keys())


def reserve_reply_account(account_id: int, ttl_seconds: float = _REPLY_ACCOUNT_RESERVATION_TTL_SECONDS) -> bool:
    with _REPLY_ACCOUNT_RESERVATION_LOCK:
        _cleanup_reply_account_reservations_locked()
        if account_id in _REPLY_ACCOUNT_RESERVED_UNTIL:
            return False
        _REPLY_ACCOUNT_RESERVED_UNTIL[account_id] = time.monotonic() + ttl_seconds
        return True


def release_reply_account_reservation(account_id: int) -> None:
    with _REPLY_ACCOUNT_RESERVATION_LOCK:
        _REPLY_ACCOUNT_RESERVED_UNTIL.pop(account_id, None)


MONITOR_SCOPE = "monitor"
RUNTIME_SCOPE = "runtime"
MAIN_SCOPE = "main_comment"
REPLY_SCOPE = "reply"
MONITOR_CATEGORY_UNKNOWN = "unknown"
MONITOR_CATEGORY_CAPTCHA = "captcha"
MONITOR_CATEGORY_RATE_LIMIT = "rate_limit"
MONITOR_CATEGORY_PROXY = "proxy_issue"
MONITOR_CATEGORY_NETWORK = "network"
MONITOR_CATEGORY_LOGIN = "dead_session"
MONITOR_CATEGORY_SUSPICIOUS = "suspicious"
MONITOR_CATEGORY_EMPTY = "empty_profile"
MONITOR_CATEGORY_DETECTION = "detection_error"
MONITOR_CATEGORY_BLOCKED = "page_blocked"
RUNTIME_CATEGORY_COOKIE_ENRICHMENT = "cookie_enrichment"

MONITOR_CATEGORY_COOLDOWNS_MIN = {
    MONITOR_CATEGORY_CAPTCHA: 25,
    MONITOR_CATEGORY_RATE_LIMIT: 30,
    MONITOR_CATEGORY_PROXY: 15,
    MONITOR_CATEGORY_NETWORK: 10,
    MONITOR_CATEGORY_LOGIN: 120,
    MONITOR_CATEGORY_SUSPICIOUS: 45,
    MONITOR_CATEGORY_EMPTY: 15,
    MONITOR_CATEGORY_DETECTION: 15,
    MONITOR_CATEGORY_BLOCKED: 20,
    MONITOR_CATEGORY_UNKNOWN: 10,
}

RUNTIME_CATEGORY_COOLDOWNS_MIN = {
    MONITOR_CATEGORY_CAPTCHA: 35,
    MONITOR_CATEGORY_RATE_LIMIT: 30,
    MONITOR_CATEGORY_PROXY: 15,
    MONITOR_CATEGORY_NETWORK: 10,
    MONITOR_CATEGORY_LOGIN: 120,
    MONITOR_CATEGORY_SUSPICIOUS: 30,
    MONITOR_CATEGORY_BLOCKED: 45,
    RUNTIME_CATEGORY_COOKIE_ENRICHMENT: 360,
    MONITOR_CATEGORY_UNKNOWN: 10,
}

RUNTIME_BUCKET_ORDER = {
    "healthy": 0,
    "degraded": 1,
    "proxy_issue": 2,
    "cooldown": 3,
    "dead_session": 4,
}

RUNTIME_HEALTH_HEALTHY = "healthy"
RUNTIME_HEALTH_RISKY = "risky"
RUNTIME_HEALTH_CAPTCHA_RECENT = "captcha_recent"
RUNTIME_HEALTH_PAGE_BLOCKED_RECENT = "page_blocked_recent"
RUNTIME_HEALTH_LOGIN_UI_RECENT = "login_ui_recent"
RUNTIME_HEALTH_PROXY_ERROR_RECENT = "proxy_error_recent"
RUNTIME_HEALTH_COOKIE_ENRICHMENT_RECENT = "cookie_enrichment_recent"
RUNTIME_HEALTH_COOLDOWN = "cooldown"
RUNTIME_HEALTH_DEAD_SESSION = "dead_session"
RUNTIME_HEALTH_PROXY_ISSUE = "proxy_issue"

_RUNTIME_HEALTH_SCORE_PENALTIES = {
    RUNTIME_HEALTH_RISKY: 8,
    RUNTIME_HEALTH_CAPTCHA_RECENT: 36,
    RUNTIME_HEALTH_PAGE_BLOCKED_RECENT: 34,
    RUNTIME_HEALTH_LOGIN_UI_RECENT: 48,
    RUNTIME_HEALTH_PROXY_ERROR_RECENT: 30,
    RUNTIME_HEALTH_COOKIE_ENRICHMENT_RECENT: 18,
}

_RUNTIME_HEALTH_LAST_RESORT_GRADES = {
    RUNTIME_HEALTH_CAPTCHA_RECENT,
    RUNTIME_HEALTH_PAGE_BLOCKED_RECENT,
    RUNTIME_HEALTH_LOGIN_UI_RECENT,
    RUNTIME_HEALTH_PROXY_ERROR_RECENT,
}

ACCOUNT_LIFECYCLE_UNKNOWN = "unknown"
ACCOUNT_LIFECYCLE_FRESH = "fresh"
ACCOUNT_LIFECYCLE_BURN = "burn_window"
ACCOUNT_LIFECYCLE_EXPIRING = "expiring"
ACCOUNT_LIFECYCLE_EXPIRED = "expired"

ACCOUNT_LANE_KEEPER = "keeper"
ACCOUNT_LANE_BALANCED = "balanced"
ACCOUNT_LANE_BURNER = "burner"
ACCOUNT_LANE_RETIRE = "retire"

SHORT_LIVED_ACCOUNT_FRESH_HOURS = 24
SHORT_LIVED_ACCOUNT_EXPIRING_HOURS = 60
SHORT_LIVED_ACCOUNT_MAX_HOURS = 72
SHORT_LIVED_ACCOUNT_VALIDATION_GRACE_HOURS = 8

SESSION_BUCKET_STRONG = "http_strong"
SESSION_BUCKET_WORKABLE = "http_workable"
SESSION_BUCKET_DEGRADED = "http_degraded"
SESSION_BUCKET_DOM_ONLY = "dom_only"
SESSION_BUCKET_WEAK = "weak"
SESSION_BUCKET_DEAD = "dead"

_SESSION_BUCKET_SCORE_KEYS = {
    SESSION_BUCKET_STRONG: "session_strong",
    SESSION_BUCKET_WORKABLE: "session_workable",
    SESSION_BUCKET_DEGRADED: "session_degraded",
    SESSION_BUCKET_DOM_ONLY: "session_dom_only",
    SESSION_BUCKET_WEAK: "session_weak",
    SESSION_BUCKET_DEAD: "session_dead",
}

_SESSION_COOKIE_KEYS = {"sessionid", "sessionid_ss", "sid_tt", "sid_guard"}
_SESSION_FINGERPRINT_KEYS = {"verifyfp", "s_v_web_id"}
_SESSION_LINKAGE_KEYS = {"ttwid", "uid_tt", "uid_tt_ss", "odin_tt"}
_SESSION_DEVICE_KEYS = {"device_id", "webid", "webid_v2"}
_SESSION_CSRF_KEYS = {"tt_csrf_token", "passport_csrf_token", "csrf_session_id"}
_LOCAL_BROWSER_INFRA_ERROR_MARKERS = (
    "failed to launch browser",
    "failed to create browser context",
    "connection closed while reading from the driver",
    "browser.new_context",
    "browsertype.launch",
    "target page, context or browser has been closed",
    "playwright driver",
)


def count_usable_accounts_for_scope(db: Session, scope: str = "main_comment") -> int:
    """
    Counts accounts in the pool that are not dead/reauth_needed/banned and have cookies.
    """
    query = db.query(Account).filter(
        Account.status.notin_(["dead", "reauth_needed", "banned"]),
        Account.cookies.isnot(None),
    )
    return query.count()


def _normalize_reason(reason: str | None, max_len: int = 220) -> str | None:
    if reason is None:
        return None
    normalized = " ".join(str(reason).split())
    if not normalized:
        return None
    return normalized[:max_len]


def _ensure_utc(dt: datetime | None) -> datetime | None:
    if dt is None:
        return None
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def _parse_last_error_payload(last_error: str | None) -> dict[str, Any] | None:
    if not last_error:
        return None
    try:
        payload = json.loads(last_error)
    except (json.JSONDecodeError, TypeError):
        return None
    return payload if isinstance(payload, dict) else None


def _has_auth_material(account: Account) -> bool:
    cookies = getattr(account, "cookies", None)
    cookie_file = getattr(account, "cookie_file", None)
    return bool(
        (cookies is not None and str(cookies).strip())
        or (cookie_file is not None and str(cookie_file).strip())
    )


def _account_age_hours(account: Account, now: datetime | None = None) -> float | None:
    created_at = _ensure_utc(getattr(account, "created_at", None))
    if not created_at:
        return None
    now = now or datetime.now(timezone.utc)
    return max(0.0, (now - created_at).total_seconds() / 3600.0)


def _read_account_lifecycle(account: Account, now: datetime | None = None) -> dict[str, Any]:
    now = now or datetime.now(timezone.utc)
    age_hours = _account_age_hours(account, now)
    last_validation_ok = _ensure_utc(getattr(account, "last_validation_ok", None))
    recently_validated = bool(
        last_validation_ok
        and last_validation_ok >= now - timedelta(hours=SHORT_LIVED_ACCOUNT_VALIDATION_GRACE_HOURS)
    )

    if age_hours is None:
        return {
            "stage": ACCOUNT_LIFECYCLE_UNKNOWN,
            "age_hours": None,
            "recently_validated": recently_validated,
            "hard_expired": False,
        }

    # Long-lived pools: never retire from calendar age of the DB row alone, but keep
    # fresh / burn / expiring *stages* for lane prioritization (keeper vs burner).
    if not bool(getattr(settings, "ACCOUNT_SHORT_LIVED_LIFECYCLE_BY_CREATED_AT", False)):
        if age_hours <= SHORT_LIVED_ACCOUNT_FRESH_HOURS:
            stage = ACCOUNT_LIFECYCLE_FRESH
        elif age_hours <= SHORT_LIVED_ACCOUNT_EXPIRING_HOURS:
            stage = ACCOUNT_LIFECYCLE_BURN
        elif age_hours <= SHORT_LIVED_ACCOUNT_MAX_HOURS:
            stage = ACCOUNT_LIFECYCLE_EXPIRING
        else:
            # Past the old 72h "max" window: still usable, same lane tier as expiring.
            stage = ACCOUNT_LIFECYCLE_EXPIRING
        return {
            "stage": stage,
            "age_hours": age_hours,
            "recently_validated": recently_validated,
            "hard_expired": False,
        }

    if age_hours > SHORT_LIVED_ACCOUNT_MAX_HOURS:
        if recently_validated:
            stage = ACCOUNT_LIFECYCLE_EXPIRING
            hard_expired = False
        else:
            stage = ACCOUNT_LIFECYCLE_EXPIRED
            hard_expired = True
    elif age_hours > SHORT_LIVED_ACCOUNT_EXPIRING_HOURS:
        stage = ACCOUNT_LIFECYCLE_EXPIRING
        hard_expired = False
    elif age_hours > SHORT_LIVED_ACCOUNT_FRESH_HOURS:
        stage = ACCOUNT_LIFECYCLE_BURN
        hard_expired = False
    else:
        stage = ACCOUNT_LIFECYCLE_FRESH
        hard_expired = False

    return {
        "stage": stage,
        "age_hours": age_hours,
        "recently_validated": recently_validated,
        "hard_expired": hard_expired,
    }


def _lifecycle_sort_rank(stage: str | None) -> int:
    order = {
        ACCOUNT_LIFECYCLE_FRESH: 0,
        ACCOUNT_LIFECYCLE_BURN: 1,
        ACCOUNT_LIFECYCLE_EXPIRING: 2,
        ACCOUNT_LIFECYCLE_UNKNOWN: 3,
        ACCOUNT_LIFECYCLE_EXPIRED: 4,
    }
    return order.get(stage or ACCOUNT_LIFECYCLE_UNKNOWN, 5)


def _has_recent_validation(
    account: Account,
    now: datetime | None = None,
    *,
    window_hours: int = 12,
) -> bool:
    now = now or datetime.now(timezone.utc)
    last_validation_ok = _ensure_utc(getattr(account, "last_validation_ok", None))
    if not last_validation_ok:
        return False
    return last_validation_ok >= now - timedelta(hours=window_hours)


def _read_account_spend_lane(
    account: Account,
    scope: str,
    now: datetime | None = None,
) -> dict[str, Any]:
    """
    Determines how aggressively the system should spend this account.

    keeper:
        strong/fresh inventory that we prefer to preserve for main comment and monitor.
    balanced:
        normal working pool.
    burner:
        accounts we can spend first on replies or opportunistic work.
    retire:
        accounts that should effectively leave rotation.
    """
    now = now or datetime.now(timezone.utc)
    lifecycle = _read_account_lifecycle(account, now)
    session_quality = _read_account_session_quality(account, scope, now)
    validation_fail_count = int(account.validation_fail_count or 0)
    recent_validation = _has_recent_validation(account, now)
    stage = lifecycle["stage"]
    session_bucket = session_quality["bucket"]

    if (
        account.status == "dead"
        or lifecycle["hard_expired"]
        or session_bucket == SESSION_BUCKET_DEAD
        or validation_fail_count >= 3
    ):
        return {
            "lane": ACCOUNT_LANE_RETIRE,
            "reason": "expired_or_dead",
        }

    if scope == REPLY_SCOPE:
        if (
            stage == ACCOUNT_LIFECYCLE_EXPIRING
            or validation_fail_count > 0
            or session_bucket in {SESSION_BUCKET_DEGRADED, SESSION_BUCKET_DOM_ONLY, SESSION_BUCKET_WEAK}
        ):
            return {
                "lane": ACCOUNT_LANE_BURNER,
                "reason": "reply_burn_lane",
            }
        if (
            stage == ACCOUNT_LIFECYCLE_FRESH
            and recent_validation
            and validation_fail_count == 0
            and session_bucket == SESSION_BUCKET_STRONG
        ):
            return {
                "lane": ACCOUNT_LANE_KEEPER,
                "reason": "preserve_strong_reply_account",
            }
        return {
            "lane": ACCOUNT_LANE_BALANCED,
            "reason": "reply_general_pool",
        }

    if scope == MAIN_SCOPE:
        if (
            stage in {ACCOUNT_LIFECYCLE_EXPIRING, ACCOUNT_LIFECYCLE_UNKNOWN}
            or validation_fail_count > 0
            or session_bucket in {SESSION_BUCKET_DEGRADED, SESSION_BUCKET_DOM_ONLY, SESSION_BUCKET_WEAK}
        ):
            return {
                "lane": ACCOUNT_LANE_BURNER,
                "reason": "protect_main_pool",
            }
        if (
            stage in {ACCOUNT_LIFECYCLE_FRESH, ACCOUNT_LIFECYCLE_BURN}
            and recent_validation
            and validation_fail_count == 0
            and session_bucket in {SESSION_BUCKET_STRONG, SESSION_BUCKET_WORKABLE}
        ):
            return {
                "lane": ACCOUNT_LANE_KEEPER,
                "reason": "main_keeper",
            }
        return {
            "lane": ACCOUNT_LANE_BALANCED,
            "reason": "main_general_pool",
        }

    # MONITOR_SCOPE and any other fallback scopes:
    if (
        stage in {ACCOUNT_LIFECYCLE_EXPIRING, ACCOUNT_LIFECYCLE_UNKNOWN}
        or validation_fail_count > 0
        or session_bucket in {SESSION_BUCKET_DOM_ONLY, SESSION_BUCKET_WEAK}
    ):
        return {
            "lane": ACCOUNT_LANE_BURNER,
            "reason": "monitor_not_stable_enough",
        }
    if (
        stage in {ACCOUNT_LIFECYCLE_FRESH, ACCOUNT_LIFECYCLE_BURN}
        and recent_validation
        and validation_fail_count == 0
        and session_bucket in {SESSION_BUCKET_STRONG, SESSION_BUCKET_WORKABLE}
    ):
        return {
            "lane": ACCOUNT_LANE_KEEPER,
            "reason": "monitor_keeper",
        }
    return {
        "lane": ACCOUNT_LANE_BALANCED,
        "reason": "monitor_general_pool",
    }


def _lane_sort_rank(scope: str, lane: str | None) -> int:
    if scope == REPLY_SCOPE:
        order = {
            ACCOUNT_LANE_BURNER: 0,
            ACCOUNT_LANE_BALANCED: 1,
            ACCOUNT_LANE_KEEPER: 2,
            ACCOUNT_LANE_RETIRE: 3,
        }
    else:
        order = {
            ACCOUNT_LANE_KEEPER: 0,
            ACCOUNT_LANE_BALANCED: 1,
            ACCOUNT_LANE_BURNER: 2,
            ACCOUNT_LANE_RETIRE: 3,
        }
    return order.get(lane or ACCOUNT_LANE_BALANCED, 4)



def _read_account_cookie_source(account: Account) -> str:
    inline_cookies = getattr(account, "cookies", None)
    if inline_cookies and str(inline_cookies).strip():
        return str(inline_cookies)

    cookie_file = getattr(account, "cookie_file", None)
    if not cookie_file:
        return ""

    raw_cookie_file = str(cookie_file).strip()
    if not raw_cookie_file:
        return ""

    try:
        cookies_dir = Path(settings.COOKIES_DIR).resolve()
        candidate_path = Path(raw_cookie_file)
        if candidate_path.is_absolute():
            resolved_path = candidate_path.resolve()
        else:
            if ".." in candidate_path.parts:
                logger.warning(
                    "[ACCOUNT_SERVICE] Rejected unsafe cookie_file for account %s",
                    getattr(account, "id", "?"),
                )
                return ""
            resolved_path = (cookies_dir / candidate_path).resolve()
        if not resolved_path.is_relative_to(cookies_dir):
            logger.warning(
                "[ACCOUNT_SERVICE] Rejected cookie_file outside COOKIES_DIR for account %s",
                getattr(account, "id", "?"),
            )
            return ""
        if resolved_path.exists():
            return resolved_path.read_text(encoding="utf-8")
    except Exception as exc:
        logger.debug(f"[ACCOUNT_SERVICE] Failed to read cookie file {raw_cookie_file}: {exc}")

    return ""


def _safe_proxy_selection_diag(proxy_value: str | None, proxy_obj: Proxy | None = None) -> str:
    raw = str(proxy_value or "").strip()
    if not raw:
        return "none"

    proxy_sig = hashlib.sha1(raw.encode("utf-8", errors="ignore")).hexdigest()[:10]
    host = None
    port = None
    try:
        parsed = urlparse(raw if "://" in raw else f"//{raw}")
        host = parsed.hostname
        port = parsed.port
    except Exception:
        host = None
        port = None

    if not host:
        visible_part = raw.split("@")[-1]
        parts = visible_part.split(":")
        if parts and parts[0]:
            host = parts[0].strip("[]")
        if len(parts) > 1 and parts[1]:
            port = parts[1]

    host_port = f"{host}:{port}" if host and port else (host or "unknown")
    proxy_id = getattr(proxy_obj, "id", None) if proxy_obj is not None else None
    mobile = getattr(proxy_obj, "is_mobile", None) if proxy_obj is not None else None
    country = getattr(proxy_obj, "country_code", None) if proxy_obj is not None else None
    return (
        f"sig={proxy_sig},host={host_port},id={proxy_id or 'n/a'},"
        f"mobile={mobile or 'n/a'},country={country or 'n/a'}"
    )


def _proxy_source_host(proxy_value: str | None) -> str:
    raw = str(proxy_value or "").strip()
    if not raw:
        return ""
    try:
        parsed = urlparse(raw if "://" in raw else f"//{raw}")
        if parsed.hostname:
            return parsed.hostname.strip("[]").lower()
    except Exception:
        pass
    visible_part = raw.split("@")[-1]
    host = visible_part.split(":")[0].strip("[]").strip().lower()
    return host


def _parallel_proxy_diversify_by_source_host() -> bool:
    return bool(getattr(settings, "PARALLEL_PROXY_DIVERSIFY_BY_SOURCE_HOST", False))


def _account_cookie_key_diag(account: Account, max_keys: int = 40) -> list[str]:
    raw_cookies = _read_account_cookie_source(account)
    if not raw_cookies:
        return []
    try:
        cookies_dict = parse_cookies_to_dict(raw_cookies)
    except Exception as exc:
        logger.debug(f"[ACCOUNT_SERVICE] Failed to parse cookie keys for account {account.id}: {exc}")
        return ["<parse_error>"]

    keys = sorted({str(key) for key in cookies_dict.keys() if str(key).strip()})
    if len(keys) <= max_keys:
        return keys
    return [*keys[:max_keys], f"+{len(keys) - max_keys}_more"]


def _minutes_since(dt: datetime | None, now: datetime) -> int | None:
    normalized = _ensure_utc(dt)
    if normalized is None:
        return None
    return max(0, int((now - normalized).total_seconds() // 60))


def _account_selection_diagnostics(
    account: Account,
    now: datetime,
    *,
    proxy_obj: Proxy | None = None,
) -> str:
    try:
        runtime_health = _runtime_health_grade(_read_runtime_state(account, now))
    except Exception:
        runtime_health = RUNTIME_HEALTH_RISKY
    return (
        f"proxy=({_safe_proxy_selection_diag(getattr(account, 'proxy', None), proxy_obj)}), "
        f"runtime_health={runtime_health}, "
        f"cookie_keys={_account_cookie_key_diag(account)}, "
        f"age_min={_minutes_since(getattr(account, 'created_at', None), now)}, "
        f"last_used_min={_minutes_since(getattr(account, 'last_used_at', None), now)}, "
        f"last_main_min={_minutes_since(getattr(account, 'last_main_comment_at', None), now)}, "
        f"last_valid_min={_minutes_since(getattr(account, 'last_validation_ok', None), now)}, "
        f"today_main={int(getattr(account, 'today_main_comments', 0) or 0)}, "
        f"today_replies={int(getattr(account, 'today_replies', 0) or 0)}, "
        f"errors={int(getattr(account, 'error_count_recent', 0) or 0)}"
    )


def _read_account_session_quality(
    account: Account,
    scope: str,
    now: datetime | None = None,
) -> dict[str, Any]:
    now = now or datetime.now(timezone.utc)
    raw_cookies = _read_account_cookie_source(account)
    if not raw_cookies:
        return {
            "bucket": SESSION_BUCKET_DEAD,
            "score": 0,
            "matched_set": None,
            "quality_reason": "missing_cookie_source",
            "http_ready": False,
            "http_preferred": False,
            "has_auth_session": False,
            "missing_http_critical": ["session"],
            "missing_http_optional": [],
        }

    try:
        cookies_dict = parse_cookies_to_dict(raw_cookies)
    except Exception as exc:
        logger.debug(f"[ACCOUNT_SERVICE] Failed to parse cookies for account {account.id}: {exc}")
        cookies_dict = {}

    if not cookies_dict:
        return {
            "bucket": SESSION_BUCKET_DEAD,
            "score": 0,
            "matched_set": None,
            "quality_reason": "invalid_cookie_format",
            "http_ready": False,
            "http_preferred": False,
            "has_auth_session": False,
            "missing_http_critical": ["session"],
            "missing_http_optional": [],
        }

    cookie_keys = {str(key).lower(): value for key, value in cookies_dict.items()}
    has_sid_tt = "sid_tt" in cookie_keys
    has_sessionid = "sessionid" in cookie_keys or "sessionid_ss" in cookie_keys
    has_sid_guard = "sid_guard" in cookie_keys
    has_ms_token = "mstoken" in cookie_keys
    has_verify_fp = any(key in cookie_keys for key in _SESSION_FINGERPRINT_KEYS)
    has_browser_linkage = any(key in cookie_keys for key in _SESSION_LINKAGE_KEYS)
    has_device_linkage = any(key in cookie_keys for key in _SESSION_DEVICE_KEYS)
    has_csrf = any(key in cookie_keys for key in _SESSION_CSRF_KEYS)
    has_auth_session = any(key in cookie_keys for key in _SESSION_COOKIE_KEYS)
    # sid_guard alone cannot render TikTok's comment input box in the browser.
    # Only sessionid/sid_tt allow the React app to show the comment form.
    has_browser_auth = has_sid_tt or has_sessionid
    has_strong_pair = (has_sid_tt and has_sessionid) or (has_sid_guard and has_ms_token)

    matched_set = None
    try:
        validation = validate_cookies(raw_cookies)
        if validation.is_valid:
            matched_set = validation.matched_set
        elif validation.error:
            matched_set = validation.error
    except Exception as exc:
        logger.debug(f"[ACCOUNT_SERVICE] Cookie validation helper failed for account {account.id}: {exc}")

    supporting_signals = sum(
        1
        for flag in (
            has_verify_fp,
            has_ms_token,
            has_csrf,
            has_browser_linkage,
            has_device_linkage,
        )
        if flag
    )

    missing_http_critical: list[str] = []
    if not has_auth_session:
        missing_http_critical.append("session")
    if not has_verify_fp:
        missing_http_critical.append("verify_fp")
    if not (has_browser_linkage or has_device_linkage):
        missing_http_critical.append("browser_linkage")

    missing_http_optional: list[str] = []
    if not has_ms_token:
        missing_http_optional.append("msToken")
    if not has_csrf:
        missing_http_optional.append("csrf")

    score = 0
    if has_auth_session:
        score += 36
    if has_sessionid:
        score += 10
    if has_sid_tt:
        score += 10
    if has_sid_guard:
        score += 6
    if has_strong_pair:
        score += 12
    if has_verify_fp:
        score += 12
    if has_ms_token:
        score += 10
    if has_csrf:
        score += 8
    if has_browser_linkage:
        score += 8
    if has_device_linkage:
        score += 6
    if matched_set in {"sid_tt only", "sessionid only"}:
        score -= 8

    lifecycle = _read_account_lifecycle(account, now)
    if lifecycle["stage"] == ACCOUNT_LIFECYCLE_EXPIRING:
        score -= 6
    elif lifecycle["hard_expired"]:
        score -= 14

    score = max(0, min(100, score))

    if not has_auth_session:
        bucket = SESSION_BUCKET_DEAD
        quality_reason = "missing_auth_session"
    elif scope == MAIN_SCOPE:
        if not has_browser_auth:
            # No sessionid/sid_tt — browser enrichment may harvest them (especially with patchright).
            # Use DOM_ONLY so the browser still launches and enrichment gets a chance.
            bucket = SESSION_BUCKET_DOM_ONLY
            quality_reason = "no_browser_auth_sid_guard_only"
        elif has_strong_pair and has_verify_fp and (has_browser_linkage or has_device_linkage) and (has_ms_token or has_csrf):
            bucket = SESSION_BUCKET_STRONG
            quality_reason = "full_http_ready"
        elif has_verify_fp and (has_ms_token or has_csrf or has_browser_linkage or has_device_linkage):
            bucket = SESSION_BUCKET_WORKABLE
            quality_reason = "http_ready"
        elif supporting_signals >= 1:
            bucket = SESSION_BUCKET_DEGRADED
            quality_reason = "weak_http_context"
        else:
            bucket = SESSION_BUCKET_DOM_ONLY
            quality_reason = "session_only"
    elif scope == REPLY_SCOPE:
        if not has_browser_auth:
            bucket = SESSION_BUCKET_DOM_ONLY
            quality_reason = "no_browser_auth_sid_guard_only"
        elif has_strong_pair and has_verify_fp and (has_browser_linkage or has_device_linkage):
            bucket = SESSION_BUCKET_STRONG
            quality_reason = "reply_http_strong"
        elif has_verify_fp or has_ms_token or has_csrf or has_browser_linkage:
            bucket = SESSION_BUCKET_WORKABLE
            quality_reason = "reply_http_ready"
        elif has_auth_session:
            bucket = SESSION_BUCKET_DEGRADED
            quality_reason = "reply_session_only"
        else:
            bucket = SESSION_BUCKET_DEAD
            quality_reason = "missing_auth_session"
    else:
        if has_verify_fp and (has_browser_linkage or has_ms_token or has_csrf):
            bucket = SESSION_BUCKET_STRONG
            quality_reason = "monitor_stable"
        elif has_verify_fp or has_browser_linkage or has_ms_token or has_csrf:
            bucket = SESSION_BUCKET_WORKABLE
            quality_reason = "monitor_ready"
        else:
            bucket = SESSION_BUCKET_WEAK
            quality_reason = "monitor_session_only"

    return {
        "bucket": bucket,
        "score": score,
        "matched_set": matched_set,
        "quality_reason": quality_reason,
        "http_ready": bucket in {SESSION_BUCKET_STRONG, SESSION_BUCKET_WORKABLE},
        "http_preferred": bucket == SESSION_BUCKET_STRONG,
        "has_auth_session": has_auth_session,
        "has_browser_auth": has_browser_auth,
        "has_sid_tt": has_sid_tt,
        "has_sessionid": has_sessionid,
        "has_sid_guard": has_sid_guard,
        "has_ms_token": has_ms_token,
        "has_verify_fp": has_verify_fp,
        "has_browser_linkage": has_browser_linkage,
        "has_device_linkage": has_device_linkage,
        "has_csrf": has_csrf,
        "missing_http_critical": missing_http_critical,
        "missing_http_optional": missing_http_optional,
    }


def _is_publish_session_last_resort(session_quality: dict[str, Any]) -> bool:
    """Return true for sessions that can pass passport but are weak in browser UI."""
    return (
        session_quality.get("quality_reason") == "no_browser_auth_sid_guard_only"
        and not bool(session_quality.get("has_browser_auth"))
    )


def _allow_publish_session_last_resort() -> bool:
    """Allow sid_guard-only sessions only after stronger browser-auth sessions are exhausted."""
    return bool(getattr(settings, "ALLOW_SID_GUARD_ONLY_PUBLISH_LAST_RESORT", False))


def _main_account_selection_scan_limit(
    candidate_limit: int,
    total_matching: int | None,
) -> int:
    """Scan the real main pool before falling back to degraded runtime sessions."""
    default_cap = 10000
    try:
        configured_cap = int(getattr(settings, "ACCOUNT_SELECTION_MAX_MAIN_SCAN", default_cap) or default_cap)
    except (TypeError, ValueError):
        configured_cap = default_cap
    configured_cap = max(2000, configured_cap)
    if total_matching and total_matching > 0:
        return max(candidate_limit, min(int(total_matching), configured_cap))
    return max(candidate_limit, configured_cap)


def _infer_monitor_category(reason: str | None) -> str:
    text = (reason or "").lower()
    if not text:
        return MONITOR_CATEGORY_UNKNOWN
    if any(marker in text for marker in _LOCAL_BROWSER_INFRA_ERROR_MARKERS):
        return MONITOR_CATEGORY_UNKNOWN
    if "critical_cookies_not_generated" in text:
        return RUNTIME_CATEGORY_COOKIE_ENRICHMENT
    if "captcha" in text:
        return MONITOR_CATEGORY_CAPTCHA
    if "rate limit" in text or "rate limited" in text or "too many requests" in text:
        return MONITOR_CATEGORY_RATE_LIMIT
    if "login required" in text or "cookies dead" in text or "dead session" in text:
        return MONITOR_CATEGORY_LOGIN
    if "suspicious" in text:
        return MONITOR_CATEGORY_SUSPICIOUS
    if "empty profile" in text or "empty grid" in text:
        return MONITOR_CATEGORY_EMPTY
    if "detection error" in text:
        return MONITOR_CATEGORY_DETECTION
    if "proxy" in text or "econnrefused" in text or "enotfound" in text or "econnreset" in text:
        return MONITOR_CATEGORY_PROXY
    if "network" in text or "timeout" in text or "connection" in text:
        return MONITOR_CATEGORY_NETWORK
    if "page blocked" in text or "blocked" in text:
        return MONITOR_CATEGORY_BLOCKED
    return MONITOR_CATEGORY_UNKNOWN


def _looks_like_monitor_error(reason: str | None) -> bool:
    text = (reason or "").lower()
    if not text:
        return False
    monitor_markers = (
        "video parsing",
        "before parsing",
        "after parsing",
        "empty profile",
        "empty grid",
        "quick recheck",
        "rate limited during",
        "detected during video parsing",
        "login required during video parsing",
    )
    return any(marker in text for marker in monitor_markers)


def _infer_runtime_category(reason: str | None) -> str:
    text = (reason or "").lower()
    if not text:
        return MONITOR_CATEGORY_UNKNOWN
    if any(marker in text for marker in _LOCAL_BROWSER_INFRA_ERROR_MARKERS):
        return MONITOR_CATEGORY_UNKNOWN
    if "captcha" in text:
        return MONITOR_CATEGORY_CAPTCHA
    if "rate limit" in text or "rate limited" in text or "too many requests" in text:
        return MONITOR_CATEGORY_RATE_LIMIT
    if "proxy_edge_access_denied" in text or "err_http_response_code_failure" in text:
        return MONITOR_CATEGORY_PROXY
    if "page_loading:" in text or "tiktok_shell" in text or "page loading" in text:
        return MONITOR_CATEGORY_NETWORK
    if "goto_failed:" in text or "page.goto" in text or "url_stuck_about_blank" in text:
        if "proxy" in text or "tunnel" in text or "econnrefused" in text or "enotfound" in text or "econnreset" in text:
            return MONITOR_CATEGORY_PROXY
        return MONITOR_CATEGORY_NETWORK
    if "login_ambiguous" in text:
        return MONITOR_CATEGORY_BLOCKED
    if "login required" in text or "cookies dead" in text or "dead session" in text or "validation failed" in text:
        return MONITOR_CATEGORY_LOGIN
    if (
        "forbidden" in text
        or "blocked" in text
        or "access denied" in text
        or "on_video_empty_dom_after_settle" in text
        or "comment_input_not_visible" in text
        or "comment_ui_not_found" in text
        or "reply_input_not_activated" in text
        or "input_not_found" in text
    ):
        return MONITOR_CATEGORY_BLOCKED
    if "suspicious" in text:
        return MONITOR_CATEGORY_SUSPICIOUS
    if "proxy" in text or "tunnel" in text or "econnrefused" in text or "enotfound" in text or "econnreset" in text:
        return MONITOR_CATEGORY_PROXY
    if "network" in text or "timeout" in text or "connection" in text or "page load" in text:
        return MONITOR_CATEGORY_NETWORK
    return MONITOR_CATEGORY_UNKNOWN


def _looks_like_runtime_error(reason: str | None) -> bool:
    text = (reason or "").lower()
    if not text:
        return False
    runtime_markers = (
        "captcha",
        "rate limit",
        "rate limited",
        "too many requests",
        "proxy_edge_access_denied",
        "err_http_response_code_failure",
        "http 403",
        "proxy",
        "tunnel",
        "timeout",
        "network",
        "connection",
        "blocked",
        "login_ambiguous",
        "goto_failed",
        "page.goto",
        "url_stuck_about_blank",
        "forbidden",
        "access denied",
        "on_video_empty_dom_after_settle",
        "comment_input_not_visible",
        "comment_ui_not_found",
        "reply_input_not_activated",
        "input_not_found",
        "login required",
        "cookies dead",
        "dead session",
        "validation failed",
        "page load",
        "page_loading",
        "tiktok_shell",
        "critical_cookies_not_generated",
    )
    return any(marker in text for marker in runtime_markers)


def _runtime_category_for_stored_reason(category: str, reason: str | None) -> str:
    normalized_category = category or MONITOR_CATEGORY_UNKNOWN
    inferred = _infer_runtime_category(reason)
    if (
        normalized_category == MONITOR_CATEGORY_BLOCKED
        and inferred in {MONITOR_CATEGORY_NETWORK, MONITOR_CATEGORY_PROXY}
    ):
        return inferred
    return normalized_category


def _build_monitor_error_payload(
    reason: str | None,
    *,
    category: str | None = None,
    cooldown_minutes: int | None = None,
    fatal: bool = False,
) -> dict[str, Any]:
    now = datetime.now(timezone.utc)
    normalized_reason = _normalize_reason(reason)
    normalized_category = category or _infer_monitor_category(normalized_reason)
    cooldown = cooldown_minutes
    if cooldown is None and not fatal:
        cooldown = MONITOR_CATEGORY_COOLDOWNS_MIN.get(normalized_category, MONITOR_CATEGORY_COOLDOWNS_MIN[MONITOR_CATEGORY_UNKNOWN])

    payload: dict[str, Any] = {
        "scope": MONITOR_SCOPE,
        "category": normalized_category,
        "reason": normalized_reason,
        "updated_at": now.isoformat(),
    }
    if fatal:
        payload["fatal"] = True
    elif cooldown and cooldown > 0:
        payload["cooldown_until"] = (now + timedelta(minutes=cooldown)).isoformat()
    return payload


def _build_runtime_error_payload(
    reason: str | None,
    *,
    category: str | None = None,
    cooldown_minutes: int | None = None,
    fatal: bool = False,
    scope: str = RUNTIME_SCOPE,
) -> dict[str, Any]:
    now = datetime.now(timezone.utc)
    normalized_reason = _normalize_reason(reason)
    normalized_category = category or _infer_runtime_category(normalized_reason)
    cooldown = cooldown_minutes
    if cooldown is None and not fatal:
        cooldown = RUNTIME_CATEGORY_COOLDOWNS_MIN.get(
            normalized_category,
            RUNTIME_CATEGORY_COOLDOWNS_MIN[MONITOR_CATEGORY_UNKNOWN],
        )

    payload: dict[str, Any] = {
        "scope": scope,
        "category": normalized_category,
        "reason": normalized_reason,
        "updated_at": now.isoformat(),
    }
    if fatal:
        payload["fatal"] = True
    elif cooldown and cooldown > 0:
        payload["cooldown_until"] = (now + timedelta(minutes=cooldown)).isoformat()
    return payload


def _serialize_error_payload(payload: dict[str, Any]) -> str:
    return json.dumps(payload, ensure_ascii=False, separators=(",", ":"))


def _derive_runtime_cooldown_until(
    account: Account,
    category: str,
    now: datetime | None = None,
) -> datetime | None:
    cooldown_minutes = RUNTIME_CATEGORY_COOLDOWNS_MIN.get(category)
    if not cooldown_minutes:
        return None
    last_checked_at = _ensure_utc(account.last_checked_at)
    if not last_checked_at:
        return None
    candidate = last_checked_at + timedelta(minutes=cooldown_minutes)
    return candidate if candidate > (now or datetime.now(timezone.utc)) else None


def _read_runtime_state(
    account: Account,
    now: datetime | None = None,
    *,
    bad_proxy_set: set[str] | None = None,
) -> dict[str, Any]:
    now = now or datetime.now(timezone.utc)
    payload = _parse_last_error_payload(account.last_error)
    category = MONITOR_CATEGORY_UNKNOWN
    reason = _normalize_reason(account.last_error, max_len=180)
    scope = None
    fatal = False
    cooldown_until = None

    if payload:
        payload_scope = payload.get("scope")
        if payload_scope in {RUNTIME_SCOPE, MAIN_SCOPE, REPLY_SCOPE}:
            scope = payload_scope
            reason = _normalize_reason(payload.get("reason")) or reason
            category = _runtime_category_for_stored_reason(
                payload.get("category") or _infer_runtime_category(reason),
                reason,
            )
            fatal = bool(payload.get("fatal"))
            cooldown_until = _parse_reply_cooldown_until(_serialize_error_payload(payload))
    elif _looks_like_runtime_error(account.last_error):
        category = _infer_runtime_category(account.last_error)
        cooldown_until = _derive_runtime_cooldown_until(account, category, now)

    if account.status == "dead":
        category = MONITOR_CATEGORY_LOGIN
        fatal = True

    active_cooldown = bool(cooldown_until and cooldown_until > now)
    if fatal or account.status == "dead":
        bucket = "dead_session"
    elif active_cooldown:
        bucket = "cooldown"
    elif category == MONITOR_CATEGORY_PROXY:
        # A past proxy failure should not poison the account forever after cooldown ends.
        # Keep hard proxy bans only when the proxy is still marked bad externally.
        bucket = "degraded"
    elif category != MONITOR_CATEGORY_UNKNOWN or account.status == "error" or int(account.error_count_recent or 0) > 0:
        bucket = "degraded"
    else:
        bucket = "healthy"

    if bucket not in {"cooldown", "dead_session"} and _has_bad_proxy(account, bad_proxy_set):
        bucket = "proxy_issue"
        category = MONITOR_CATEGORY_PROXY
        reason = reason or "Proxy marked as unstable by recent failures"

    return {
        "scope": scope,
        "category": category,
        "reason": reason,
        "cooldown_until": cooldown_until,
        "active_cooldown": active_cooldown,
        "fatal": fatal,
        "bucket": bucket,
    }


def _runtime_health_grade(state: dict[str, Any] | None) -> str:
    if not state:
        return RUNTIME_HEALTH_RISKY
    bucket = str(state.get("bucket") or "").strip()
    category = str(state.get("category") or "").strip()
    if bucket == "healthy":
        return RUNTIME_HEALTH_HEALTHY
    if bucket == "cooldown" or state.get("active_cooldown"):
        return RUNTIME_HEALTH_COOLDOWN
    if bucket == "dead_session":
        return RUNTIME_HEALTH_DEAD_SESSION
    if bucket == "proxy_issue":
        return RUNTIME_HEALTH_PROXY_ISSUE
    if category == MONITOR_CATEGORY_CAPTCHA:
        return RUNTIME_HEALTH_CAPTCHA_RECENT
    if category == MONITOR_CATEGORY_BLOCKED:
        return RUNTIME_HEALTH_PAGE_BLOCKED_RECENT
    if category == MONITOR_CATEGORY_LOGIN:
        return RUNTIME_HEALTH_LOGIN_UI_RECENT
    if category == MONITOR_CATEGORY_PROXY:
        return RUNTIME_HEALTH_PROXY_ERROR_RECENT
    if category == RUNTIME_CATEGORY_COOKIE_ENRICHMENT:
        return RUNTIME_HEALTH_COOKIE_ENRICHMENT_RECENT
    return RUNTIME_HEALTH_RISKY


def _runtime_health_is_selection_last_resort(state: dict[str, Any] | None) -> bool:
    return _runtime_health_grade(state) in _RUNTIME_HEALTH_LAST_RESORT_GRADES


def get_http_execution_strategy(
    account: Account,
    scope: str,
    now: datetime | None = None,
) -> dict[str, Any]:
    """
    Returns how aggressive the runtime should be with HTTP for this account.

    Strategies:
    - http_first: use HTTP first
    - http_cautious: HTTP is allowed, but only as a weaker candidate
    - dom_first: skip HTTP and go straight to browser/DOM flow
    - skip: account should not be used right now
    """
    now = now or datetime.now(timezone.utc)
    lifecycle = _read_account_lifecycle(account, now)
    state = _read_monitor_state(account, now) if scope == MONITOR_SCOPE else _read_runtime_state(account, now)
    session_quality = _read_account_session_quality(account, scope, now)
    spend_lane = _read_account_spend_lane(account, scope, now)

    if (
        lifecycle["hard_expired"]
        or state["bucket"] in {"dead_session", "proxy_issue"}
        or session_quality["bucket"] == SESSION_BUCKET_DEAD
        or spend_lane["lane"] == ACCOUNT_LANE_RETIRE
    ):
        strategy = "skip"
    elif state["active_cooldown"]:
        strategy = "skip"
    elif scope == MAIN_SCOPE:
        if session_quality["bucket"] in {SESSION_BUCKET_STRONG, SESSION_BUCKET_WORKABLE}:
            strategy = "http_cautious" if spend_lane["lane"] == ACCOUNT_LANE_BURNER else "http_first"
        elif session_quality["bucket"] == SESSION_BUCKET_DEGRADED:
            strategy = "http_cautious"
        else:
            strategy = "dom_first"
    elif scope == REPLY_SCOPE:
        if session_quality["bucket"] in {SESSION_BUCKET_STRONG, SESSION_BUCKET_WORKABLE}:
            strategy = "http_cautious" if spend_lane["lane"] == ACCOUNT_LANE_KEEPER else "http_first"
        elif session_quality["bucket"] == SESSION_BUCKET_DEGRADED:
            strategy = "http_cautious"
        elif session_quality["bucket"] == SESSION_BUCKET_DOM_ONLY:
            strategy = "dom_first"
        else:
            strategy = "skip"
    else:
        if session_quality["bucket"] in {SESSION_BUCKET_STRONG, SESSION_BUCKET_WORKABLE}:
            strategy = "http_cautious" if spend_lane["lane"] == ACCOUNT_LANE_BURNER else "http_first"
        elif session_quality["bucket"] in {SESSION_BUCKET_DEGRADED, SESSION_BUCKET_WEAK}:
            strategy = "dom_first"
        else:
            strategy = "skip"

    return {
        "strategy": strategy,
        "session_bucket": session_quality["bucket"],
        "state_bucket": state["bucket"],
        "runtime_health": _runtime_health_grade(state),
        "spend_lane": spend_lane["lane"],
        "lane_reason": spend_lane["reason"],
        "quality_reason": session_quality.get("quality_reason"),
        "cooldown_until": state.get("cooldown_until"),
        "hard_expired": lifecycle["hard_expired"],
    }


def get_browser_validation_policy(
    account: Account,
    scope: str,
    now: datetime | None = None,
) -> dict[str, Any]:
    """
    Returns whether browser validation can be skipped for a very short trust window.

    Current policy is intentionally conservative:
    - main_comment and monitor always validate strictly
    - reply can skip re-validation only for recently validated, recently used,
      strong/workable sessions that are not in burner/retire lanes
    """
    now = now or datetime.now(timezone.utc)
    lifecycle = _read_account_lifecycle(account, now)
    http_strategy = get_http_execution_strategy(account, scope, now)
    session_quality = _read_account_session_quality(account, scope, now)
    spend_lane = _read_account_spend_lane(account, scope, now)
    state = _read_monitor_state(account, now) if scope == MONITOR_SCOPE else _read_runtime_state(account, now)

    validation_window_seconds = int(getattr(settings, "REPLY_BROWSER_TRUST_WINDOW_SECONDS", 300))
    activity_window_seconds = int(getattr(settings, "REPLY_BROWSER_ACTIVITY_WINDOW_SECONDS", 900))
    if session_quality["bucket"] == SESSION_BUCKET_WORKABLE:
        validation_window_seconds = min(validation_window_seconds, 180)

    last_validation_ok = _ensure_utc(getattr(account, "last_validation_ok", None))
    last_used_at = _ensure_utc(getattr(account, "last_used_at", None))
    validation_age_seconds = None
    activity_age_seconds = None

    if last_validation_ok:
        validation_age_seconds = max(0.0, (now - last_validation_ok).total_seconds())
    if last_used_at:
        activity_age_seconds = max(0.0, (now - last_used_at).total_seconds())

    can_skip = False
    reason = "strict_scope"

    if scope != REPLY_SCOPE:
        reason = "strict_scope"
    elif account.status != "active":
        reason = "account_not_active"
    elif lifecycle["hard_expired"] or lifecycle["stage"] == ACCOUNT_LIFECYCLE_EXPIRING:
        reason = "session_expiring"
    elif state["bucket"] != "healthy":
        reason = f"runtime_{state['bucket']}"
    elif spend_lane["lane"] in {ACCOUNT_LANE_BURNER, ACCOUNT_LANE_RETIRE}:
        reason = f"lane_{spend_lane['lane']}"
    elif session_quality["bucket"] not in {SESSION_BUCKET_STRONG, SESSION_BUCKET_WORKABLE}:
        reason = f"session_{session_quality['bucket']}"
    elif http_strategy["strategy"] not in {"http_first", "http_cautious"}:
        reason = f"http_{http_strategy['strategy']}"
    elif int(account.validation_fail_count or 0) > 0 or int(account.error_count_recent or 0) > 0:
        reason = "recent_failures"
    elif validation_age_seconds is None or validation_age_seconds > validation_window_seconds:
        reason = "validation_too_old"
    elif activity_age_seconds is None or activity_age_seconds > activity_window_seconds:
        reason = "activity_too_old"
    else:
        can_skip = True
        reason = "recent_trusted_reply_session"

    trust_until = None
    if can_skip and last_validation_ok:
        trust_until = last_validation_ok + timedelta(seconds=validation_window_seconds)

    return {
        "skip_browser_validation": can_skip,
        "reason": reason,
        "scope": scope,
        "trust_until": trust_until,
        "validation_age_seconds": validation_age_seconds,
        "activity_age_seconds": activity_age_seconds,
        "session_bucket": session_quality["bucket"],
        "spend_lane": spend_lane["lane"],
        "http_strategy": http_strategy["strategy"],
    }


def _calculate_scope_quality_score(
    account: Account,
    state: dict[str, Any],
    scope: str,
    now: datetime | None = None,
) -> int:
    now = now or datetime.now(timezone.utc)
    score = 100
    bucket = state.get("bucket")
    runtime_health = _runtime_health_grade(state)

    if bucket == "degraded":
        score -= 15
    elif bucket == "proxy_issue":
        score -= 28
    elif bucket == "cooldown":
        score -= 40
    elif bucket == "dead_session":
        return 0

    score -= _RUNTIME_HEALTH_SCORE_PENALTIES.get(runtime_health, 0)

    if account.status == "error":
        score -= 12
    score -= min(int(account.error_count_recent or 0) * 6, 30)

    last_validation_ok = _ensure_utc(account.last_validation_ok)
    if last_validation_ok:
        age_hours = max(0.0, (now - last_validation_ok).total_seconds() / 3600.0)
        if age_hours <= 6:
            score += 8
        elif age_hours <= 24:
            score += 4
        else:
            score -= 6
    else:
        score -= 5

    lifecycle = _read_account_lifecycle(account, now)
    if lifecycle["hard_expired"]:
        return 0

    spend_lane = _read_account_spend_lane(account, scope, now)
    if spend_lane["lane"] == ACCOUNT_LANE_RETIRE:
        return 0

    stage = lifecycle["stage"]
    if stage == ACCOUNT_LIFECYCLE_FRESH:
        score += 4 if scope == MONITOR_SCOPE else 10
    elif stage == ACCOUNT_LIFECYCLE_BURN:
        score += 2 if scope == MONITOR_SCOPE else 6
    elif stage == ACCOUNT_LIFECYCLE_EXPIRING:
        score -= 12 if scope == MONITOR_SCOPE else 18
    elif stage == ACCOUNT_LIFECYCLE_UNKNOWN:
        score -= 6

    session_quality = _read_account_session_quality(account, scope, now)
    session_bucket = session_quality["bucket"]
    if session_bucket == SESSION_BUCKET_DEAD:
        return 0

    if scope == MAIN_SCOPE:
        session_adjustments = {
            SESSION_BUCKET_STRONG: 18,
            SESSION_BUCKET_WORKABLE: 10,
            SESSION_BUCKET_DEGRADED: -4,
            SESSION_BUCKET_DOM_ONLY: -22,
            SESSION_BUCKET_WEAK: -14,
        }
    elif scope == REPLY_SCOPE:
        session_adjustments = {
            SESSION_BUCKET_STRONG: 14,
            SESSION_BUCKET_WORKABLE: 10,
            SESSION_BUCKET_DEGRADED: 3,
            SESSION_BUCKET_DOM_ONLY: -8,
            SESSION_BUCKET_WEAK: -10,
        }
    else:
        session_adjustments = {
            SESSION_BUCKET_STRONG: 8,
            SESSION_BUCKET_WORKABLE: 6,
            SESSION_BUCKET_DEGRADED: 2,
            SESSION_BUCKET_DOM_ONLY: -4,
            SESSION_BUCKET_WEAK: -8,
        }
    score += session_adjustments.get(session_bucket, 0)
    score += int(round((int(session_quality["score"]) - 55) / 10.0))

    validation_fail_count = int(account.validation_fail_count or 0)
    if validation_fail_count >= 3:
        score -= 18
    elif validation_fail_count > 0:
        score -= min(validation_fail_count * 4, 12)

    # Per-account override: если у аккаунта свой лимит (личные пулы),
    # «нагрузка» считается относительно него — иначе аккаунт с лимитом 5 будет
    # выглядеть «свободным» по сравнению с MAX_ACTIONS_PER_ACCOUNT.
    main_cap = max(1, int(account.daily_main_limit or settings.MAX_ACTIONS_PER_ACCOUNT))
    reply_cap = max(1, int(account.daily_reply_limit or settings.MAX_ACTIONS_PER_ACCOUNT))
    if scope == MAIN_SCOPE:
        load_ratio = min(1.0, float(account.today_main_comments or 0) / main_cap)
    elif scope == REPLY_SCOPE:
        load_ratio = min(1.0, float(account.today_replies or 0) / reply_cap)
    else:
        load_ratio = min(1.0, float(account.today_main_comments or 0) / main_cap)

    if load_ratio >= 0.9:
        score -= 15
    elif load_ratio >= 0.75:
        score -= 9
    elif load_ratio >= 0.5:
        score -= 4

    if settings.USE_PROXY and account.proxy:
        score += 2

    if scope == MAIN_SCOPE:
        lane_adjustments = {
            ACCOUNT_LANE_KEEPER: 12,
            ACCOUNT_LANE_BALANCED: 4,
            ACCOUNT_LANE_BURNER: -14,
            ACCOUNT_LANE_RETIRE: -100,
        }
    elif scope == REPLY_SCOPE:
        lane_adjustments = {
            ACCOUNT_LANE_BURNER: 12,
            ACCOUNT_LANE_BALANCED: 4,
            ACCOUNT_LANE_KEEPER: -10,
            ACCOUNT_LANE_RETIRE: -100,
        }
    else:
        lane_adjustments = {
            ACCOUNT_LANE_KEEPER: 14,
            ACCOUNT_LANE_BALANCED: 4,
            ACCOUNT_LANE_BURNER: -16,
            ACCOUNT_LANE_RETIRE: -100,
        }
    score += lane_adjustments.get(spend_lane["lane"], 0)

    return max(0, min(100, score))


def _has_bad_proxy(account: Account, bad_proxy_set: set[str] | None = None) -> bool:
    if not settings.USE_PROXY:
        return False
    proxy = getattr(account, "proxy", None)
    if not proxy:
        return False
    if bad_proxy_set is not None:
        return str(proxy).strip() in bad_proxy_set
    try:
        return is_proxy_bad(proxy)
    except Exception:
        return False


def _read_monitor_state(
    account: Account,
    now: datetime | None = None,
    *,
    bad_proxy_set: set[str] | None = None,
) -> dict[str, Any]:
    now = now or datetime.now(timezone.utc)
    payload = _parse_last_error_payload(account.last_error)
    category = MONITOR_CATEGORY_UNKNOWN
    reason = _normalize_reason(account.last_error, max_len=180)
    scope = None
    fatal = False
    cooldown_until = None

    if payload:
        scope = payload.get("scope")
        reason = _normalize_reason(payload.get("reason")) or reason
        category = payload.get("category") or _infer_monitor_category(reason)
        fatal = bool(payload.get("fatal"))
        cooldown_until = _parse_reply_cooldown_until(_serialize_error_payload(payload))
    elif _looks_like_monitor_error(account.last_error):
        category = _infer_monitor_category(account.last_error)

    if account.status == "dead":
        category = MONITOR_CATEGORY_LOGIN
        fatal = True

    active_cooldown = bool(cooldown_until and cooldown_until > now)
    if fatal or account.status == "dead":
        bucket = "dead_session"
    elif active_cooldown:
        bucket = "cooldown"
    elif category == MONITOR_CATEGORY_PROXY:
        bucket = "proxy_issue"
    elif category != MONITOR_CATEGORY_UNKNOWN:
        bucket = "degraded"
    else:
        bucket = "healthy"

    if bucket not in {"cooldown", "dead_session"} and _has_bad_proxy(account, bad_proxy_set):
        bucket = "proxy_issue"
        category = MONITOR_CATEGORY_PROXY
        reason = reason or "Proxy marked as unstable by recent failures"

    return {
        "scope": scope,
        "category": category,
        "reason": reason,
        "cooldown_until": cooldown_until,
        "active_cooldown": active_cooldown,
        "fatal": fatal,
        "bucket": bucket,
    }


def _get_monitor_proxy_fail_open_limit() -> int:
    try:
        return max(0, int(getattr(settings, "MONITOR_PROXY_FAIL_OPEN_MAX_UNIQUE_PROXIES", 8) or 0))
    except (TypeError, ValueError):
        return 8


def _get_monitor_bad_proxy_set_for_accounts(accounts: Iterable[Account]) -> set[str]:
    if not settings.USE_PROXY:
        return set()

    unique_proxies = {
        str(account.proxy).strip()
        for account in accounts
        if getattr(account, "proxy", None) and str(account.proxy).strip()
    }
    if not unique_proxies:
        return set()

    bad_proxy_set = get_bad_proxy_set(unique_proxies)
    if not bad_proxy_set:
        return set()

    fail_open_limit = _get_monitor_proxy_fail_open_limit()
    if (
        fail_open_limit > 0
        and len(unique_proxies) <= fail_open_limit
        and unique_proxies.issubset(bad_proxy_set)
    ):
        logger.warning(
            "[ACCOUNT_SERVICE] Monitor proxy fail-open: all %s unique proxies are temporarily marked bad; "
            "ignoring proxy bad-list for this monitor selection cycle",
            len(unique_proxies),
        )
        return set()

    return bad_proxy_set


def get_monitor_pool_stats(db: Session) -> dict[str, Any]:
    now = datetime.now(timezone.utc)
    auth_material = or_(
        and_(Account.cookies.isnot(None), Account.cookies != ""),
        and_(Account.cookie_file.isnot(None), Account.cookie_file != ""),
    )
    query = db.query(Account).filter(
        and_(
            Account.status.in_(["active", "error", "dead"]),
            auth_material,
        )
    )
    if settings.USE_PROXY:
        query = query.filter(Account.proxy.isnot(None), Account.proxy != "")

    accounts = query.all()
    reserved_ids = get_reserved_main_account_ids()
    summary = {
        "total_candidates": len(accounts),
        "eligible_now": 0,
        "eligible_including_reserved": 0,
        "reserved": 0,
        "healthy": 0,
        "cooldown": 0,
        "degraded": 0,
        "proxy_issue": 0,
        "dead_session": 0,
        "fresh": 0,
        "burn_window": 0,
        "expiring": 0,
        "expired": 0,
        "recently_validated": 0,
        "bad_proxies": 0,
        "top_categories": [],
    }
    category_counts: dict[str, int] = {}
    bad_proxy_keys: set[str] = set()
    recent_validation_threshold = now - timedelta(hours=12)

    bad_proxy_set = _get_monitor_bad_proxy_set_for_accounts(accounts)

    for account in accounts:
        state = _read_monitor_state(account, now, bad_proxy_set=bad_proxy_set)
        lifecycle = _read_account_lifecycle(account, now)
        session_quality = _read_account_session_quality(account, MONITOR_SCOPE, now)
        bucket = state["bucket"]
        summary[bucket] = int(summary.get(bucket, 0)) + 1
        summary[lifecycle["stage"]] = int(summary.get(lifecycle["stage"], 0)) + 1
        if (
            not lifecycle["hard_expired"]
            and bucket not in {"cooldown", "dead_session", "proxy_issue"}
            and session_quality["bucket"] != SESSION_BUCKET_DEAD
            and account.status in {"active", "error"}
            and (account.error_count_recent or 0) < ERROR_THRESHOLD
        ):
            summary["eligible_including_reserved"] += 1
            if account.id in reserved_ids:
                summary["reserved"] += 1
            else:
                summary["eligible_now"] += 1
        last_validation_ok = _ensure_utc(account.last_validation_ok)
        if last_validation_ok and last_validation_ok >= recent_validation_threshold:
            summary["recently_validated"] += 1
        category = state["category"]
        if category and category != MONITOR_CATEGORY_UNKNOWN:
            category_counts[category] = category_counts.get(category, 0) + 1
        if state["bucket"] == "proxy_issue" and account.proxy:
            bad_proxy_keys.add(account.proxy)

    summary["bad_proxies"] = len(bad_proxy_keys)
    summary["top_categories"] = [
        {"category": category, "count": count}
        for category, count in sorted(category_counts.items(), key=lambda item: (-item[1], item[0]))[:5]
    ]
    return summary


def get_free_account_for_main_comment(
    db: Session,
    *,
    exclude_ids: list[int] | None = None,
    exclude_proxies: set[str] | list[str] | tuple[str, ...] | None = None,
    exclude_proxy_source_hosts: bool | None = None,
    pool_owner_user_id: int | None = None,
    restrict_to_proxies: set[str] | list[str] | tuple[str, ...] | None = None,
) -> Account | None:
    """
    Выбирает свободный аккаунт для основного комментария.

    ИСПРАВЛЕНО: Использует SELECT FOR UPDATE для предотвращения race conditions.
    Это гарантирует, что только один воркер может выбрать аккаунт одновременно.

    ГЕО-ПРИОРИТИЗАЦИЯ: Если PROXY_GEO_MATCHING=True, приоритет аккаунтам с совпадающим
    country_code аккаунта и прокси для снижения вероятности капчи.

    ИЗОЛЯЦИЯ ПУЛОВ:
    - pool_owner_user_id=None  → общий пул (Account.owner_user_id IS NULL).
    - pool_owner_user_id=<int> → личный пул конкретного работника
      (Account.owner_user_id == pool_owner_user_id). Аккаунты из общего
      пула в этом режиме НЕ выбираются — это гарантия, что заказы на
      "личных" аккаунтах не задействуют чужие.

    PER-ACCOUNT ЛИМИТЫ:
    - today_main_comments < COALESCE(Account.daily_main_limit, MAX_ACTIONS_PER_ACCOUNT).
      Если у аккаунта выставлен персональный лимит (как обычно у личных пулов) —
      используется он. Иначе — глобальный.

    Условия:
    - status = 'active' ИЛИ (status = 'error' И error_count_recent < ERROR_THRESHOLD)
    - today_main_comments < лимит (см. выше)
    - last_main_comment_at либо NULL, либо старше ACCOUNT_COOLDOWN_SECONDS
    - не в exclude_ids (если передан)
    - принадлежность пулу (см. выше)
    """
    now = datetime.now(timezone.utc)
    auth_material = or_(
        and_(Account.cookies.isnot(None), Account.cookies != ""),
        and_(Account.cookie_file.isnot(None), Account.cookie_file != ""),
    )
    # NEW: Добавляем Jitter (случайный разброс) к кулдауну, чтобы избежать паттернов
    jitter = random.uniform(0.9, 1.2) # -10% до +20% разброс
    cooldown_threshold = now - timedelta(seconds=settings.ACCOUNT_COOLDOWN_SECONDS * jitter)

    # Лимит «main за сутки» с per-account override.
    daily_main_limit_expr = func.coalesce(
        Account.daily_main_limit, settings.MAX_ACTIONS_PER_ACCOUNT
    )

    # Жёсткая изоляция пулов на уровне SQL — никаких пересечений.
    if pool_owner_user_id is None:
        pool_filter = Account.owner_user_id.is_(None)
    else:
        pool_filter = Account.owner_user_id == int(pool_owner_user_id)

    query = db.query(Account).filter(
        and_(
            or_(
                Account.status == "active",
                and_(
                    Account.status == "error",
                    Account.error_count_recent < ERROR_THRESHOLD
                )
            ),
            Account.status.notin_(["dead", "reauth_needed", "banned"]),
            auth_material,
            pool_filter,
            Account.today_main_comments < daily_main_limit_expr,
            or_(
                Account.last_main_comment_at.is_(None),
                Account.last_main_comment_at < cooldown_threshold
            )
        )
    )

    reserved_ids = get_reserved_main_account_ids()
    combined_exclude_ids = set(exclude_ids or [])
    combined_exclude_ids.update(reserved_ids)
    if combined_exclude_ids:
        query = query.filter(~Account.id.in_(combined_exclude_ids))

    normalized_exclude_proxies = {
        str(proxy).strip()
        for proxy in (exclude_proxies or [])
        if proxy and str(proxy).strip()
    }
    exclude_same_proxy_source_host = (
        _parallel_proxy_diversify_by_source_host()
        if exclude_proxy_source_hosts is None
        else bool(exclude_proxy_source_hosts)
    )
    normalized_exclude_proxy_hosts = (
        {host for host in (_proxy_source_host(proxy) for proxy in normalized_exclude_proxies) if host}
        if exclude_same_proxy_source_host
        else set()
    )
    if normalized_exclude_proxies:
        query = query.filter(
            or_(
                Account.proxy.is_(None),
                ~Account.proxy.in_(sorted(normalized_exclude_proxies)),
            )
        )

    # Жёсткое ограничение выбора конкретным набором прокси (strict 6x1 wave 2):
    # аккаунт обязан находиться на одном из уже ротированных мобильных прокси.
    normalized_restrict_to_proxies = {
        str(proxy).strip()
        for proxy in (restrict_to_proxies or [])
        if proxy and str(proxy).strip()
    }
    if normalized_restrict_to_proxies:
        query = query.filter(
            Account.proxy.in_(sorted(normalized_restrict_to_proxies))
        )

    if settings.USE_PROXY:
        query = query.filter(
            Account.proxy.isnot(None),
            Account.proxy != "",
        )
    
    # Geo-matching и фильтрация типов прокси через JOIN
    proxy_type_filter = get_proxy_type_filter(db)
    proxy_prefer_mobile = get_proxy_prefer_mobile(db)
    has_proxy_join = False
    
    # Подключаем JOIN если нужна фильтрация по типу прокси
    if proxy_type_filter:
        query = query.outerjoin(Proxy, Account.proxy == Proxy.proxy)
        has_proxy_join = True
        if proxy_type_filter.lower() == "mobile":
            query = query.filter(or_(Proxy.is_mobile == "Y", Proxy.id.is_(None)))
        elif proxy_type_filter.lower() == "residential":
            query = query.filter(or_(Proxy.is_mobile != "Y", Proxy.id.is_(None)))
    elif proxy_prefer_mobile:
        query = query.outerjoin(Proxy, Account.proxy == Proxy.proxy)
        has_proxy_join = True
    
    # Geo-приоритизация: если включено, подключаем JOIN для сортировки
    if settings.PROXY_GEO_MATCHING and not has_proxy_join:
        query = query.outerjoin(Proxy, Account.proxy == Proxy.proxy)
        has_proxy_join = True
    
    # Сортировка: ORDER BY с учетом geo-matching
    order_by_clauses = [
        # active статус первый
        (Account.status != "active").asc(),
        # Меньше ошибок - лучше
        Account.error_count_recent.asc().nulls_first(),
    ]
    
    # Добавляем гео-приоритизацию если включено
    if settings.PROXY_GEO_MATCHING and has_proxy_join:
        # Приоритет: совпадение country_code (1 если совпадает, 0 если нет, NULL last)
        geo_match_priority = case(
            (and_(Account.country_code == Proxy.country_code, Account.country_code.isnot(None), Proxy.country_code.isnot(None)), 1),
            else_=0
        )
        order_by_clauses.insert(1, geo_match_priority.desc())  # После статуса, но до ошибок
    
    if proxy_prefer_mobile and has_proxy_join:
        # Приоритет мобильным прокси
        mobile_priority = case((Proxy.is_mobile == "Y", 1), else_=0)
        order_by_clauses.append(mobile_priority.desc())
    
    order_by_clauses.extend([
        Account.last_main_comment_at.nulls_first(),
        Account.last_used_at.asc().nulls_first(),
        Account.id.asc()  # Для детерминированности при одинаковых значениях
    ])

    total_matching_for_scan: int | None = None

    # Логирование для отладки
    try:
        logger.info(f"[ACCOUNT_SERVICE] proxy_type_filter={proxy_type_filter}, proxy_prefer_mobile={proxy_prefer_mobile}, has_proxy_join={has_proxy_join}, PROXY_GEO_MATCHING={settings.PROXY_GEO_MATCHING}")
        
        # При JOIN используем distinct для правильного подсчета
        if has_proxy_join:
            account_ids_query = query.with_entities(Account.id).distinct()
            total_matching = account_ids_query.count()
        else:
            total_matching = query.count()
        total_matching_for_scan = int(total_matching or 0)
        logger.info(f"[ACCOUNT_SERVICE] Found {total_matching} raw accounts matching query for main comment")
        
        if total_matching == 0:
            # Детальная диагностика
            total_accounts = db.query(Account).count()
            active_accounts = db.query(Account).filter(Account.status == "active").count()
            error_accounts = db.query(Account).filter(
                and_(
                    Account.status == "error",
                    Account.error_count_recent < ERROR_THRESHOLD
                )
            ).count()
            
            accounts_not_in_cooldown = db.query(Account).filter(
                or_(
                    Account.last_main_comment_at.is_(None),
                    Account.last_main_comment_at < cooldown_threshold
                )
            ).count()
            
            accounts_below_limit = db.query(Account).filter(
                Account.today_main_comments < daily_main_limit_expr
            ).count()
            
            excluded_count = len(combined_exclude_ids) if "combined_exclude_ids" in locals() else (len(exclude_ids) if exclude_ids else 0)
            
            no_match_log = logger.info if normalized_exclude_proxies else logger.warning
            no_match_log(
                f"[ACCOUNT_SERVICE] ⚠️ No accounts available for main comment! "
                f"Total accounts: {total_accounts}, Active: {active_accounts}, "
                f"Error (with low error_count): {error_accounts}, "
                f"Excluded: {excluded_count}, "
                f"Excluded proxies: {len(normalized_exclude_proxies)}, "
                f"Excluded proxy hosts: {len(normalized_exclude_proxy_hosts)}, "
                f"Not in cooldown: {accounts_not_in_cooldown}, "
                f"Below limit: {accounts_below_limit}, "
                f"MAX_ACTIONS_PER_ACCOUNT={settings.MAX_ACTIONS_PER_ACCOUNT}, "
                f"ACCOUNT_COOLDOWN_SECONDS={settings.ACCOUNT_COOLDOWN_SECONDS}"
            )
            
            if logger.isEnabledFor(logging.DEBUG):
                all_accounts = db.query(Account.id, Account.status, Account.today_main_comments, 
                                       Account.last_main_comment_at, Account.error_count_recent).all()
                for acc in all_accounts:
                    in_cooldown = acc.last_main_comment_at and acc.last_main_comment_at >= cooldown_threshold
                    at_limit = acc.today_main_comments >= settings.MAX_ACTIONS_PER_ACCOUNT
                    excluded = exclude_ids and acc.id in exclude_ids
                    logger.debug(
                        f"[ACCOUNT_SERVICE] Account {acc.id}: status={acc.status}, "
                        f"today_comments={acc.today_main_comments}, "
                        f"in_cooldown={in_cooldown}, at_limit={at_limit}, excluded={excluded}, "
                        f"error_count={acc.error_count_recent}"
                    )
    except Exception as e:
        logger.debug(f"[ACCOUNT_SERVICE] Error counting accounts: {e}")
    
    bind = getattr(db, "bind", None)
    dialect_name = getattr(getattr(bind, "dialect", None), "name", "") or ""
    supports_skip_locked = dialect_name not in {"", "sqlite"}
    candidate_limit = max(20, min(80, getattr(settings, "MAX_CONCURRENT_BROWSERS", 10) * 4))
    max_candidate_scan = _main_account_selection_scan_limit(
        candidate_limit,
        total_matching_for_scan,
    )

    ranked_candidates: list[tuple[tuple[Any, ...], Account, dict[str, Any], int]] = []
    runtime_last_resort_candidates: list[tuple[tuple[Any, ...], Account, dict[str, Any], int]] = []
    last_resort_candidates: list[tuple[tuple[Any, ...], Account, dict[str, Any], int]] = []
    candidates: list[Account] = []
    try:
        desired_ranked_candidates = int(getattr(settings, "PARALLEL_COMMENTING_TARGET_BROWSERS", 3) or 3)
    except (TypeError, ValueError):
        desired_ranked_candidates = 3
    desired_ranked_candidates = max(1, min(10, desired_ranked_candidates))
    offset = 0
    while offset < max_candidate_scan and len(ranked_candidates) < desired_ranked_candidates:
        candidate_query = (
            query
            .order_by(*order_by_clauses)
            .offset(offset)
            .limit(candidate_limit)
        )

        try:
            if supports_skip_locked:
                candidate_batch = candidate_query.with_for_update(skip_locked=True).all()
            else:
                candidate_batch = candidate_query.with_for_update().all()
        except Exception as lock_error:
            logger.warning(f"SELECT FOR UPDATE not supported, falling back to regular candidate query: {lock_error}")
            candidate_batch = candidate_query.all()

        if not candidate_batch:
            break
        candidates.extend(candidate_batch)
        for batch_index, candidate in enumerate(candidate_batch):
            index = offset + batch_index
            candidate_proxy = str(candidate.proxy or "").strip()
            if candidate_proxy:
                if candidate_proxy in normalized_exclude_proxies:
                    continue
                if exclude_same_proxy_source_host and _proxy_source_host(candidate_proxy) in normalized_exclude_proxy_hosts:
                    continue
            if not _has_auth_material(candidate):
                continue
            state = _read_runtime_state(candidate, now)
            lifecycle = _read_account_lifecycle(candidate, now)
            session_quality = _read_account_session_quality(candidate, MAIN_SCOPE, now)
            spend_lane = _read_account_spend_lane(candidate, MAIN_SCOPE, now)
            if lifecycle["hard_expired"]:
                continue
            if state["active_cooldown"] or state["bucket"] in {"dead_session", "proxy_issue"}:
                continue
            if session_quality["bucket"] == SESSION_BUCKET_DEAD:
                continue
            if spend_lane["lane"] == ACCOUNT_LANE_RETIRE:
                continue
            score = _calculate_scope_quality_score(candidate, state, MAIN_SCOPE, now)
            ranked_item = (
                (
                    RUNTIME_BUCKET_ORDER.get(state["bucket"], 5),
                    0 if candidate.status == "active" else 1,
                    _lane_sort_rank(MAIN_SCOPE, spend_lane["lane"]),
                    _lifecycle_sort_rank(lifecycle["stage"]),
                    -score,
                    int(candidate.error_count_recent or 0),
                    index,
                ),
                candidate,
                state,
                score,
            )
            if _is_publish_session_last_resort(session_quality):
                last_resort_candidates.append(ranked_item)
                continue
            if _runtime_health_is_selection_last_resort(state):
                runtime_last_resort_candidates.append(ranked_item)
                continue
            ranked_candidates.append(ranked_item)
        offset += candidate_limit

    if not ranked_candidates and runtime_last_resort_candidates:
        logger.warning(
            "[ACCOUNT_SERVICE] Only recently degraded main runtime sessions available after scanning %s SQL candidates; "
            "using them as last resort behind browser readiness gates",
            len(candidates),
        )
        ranked_candidates = runtime_last_resort_candidates

    if not ranked_candidates and last_resort_candidates and _allow_publish_session_last_resort():
        logger.warning(
            "[ACCOUNT_SERVICE] Only last-resort main sessions available after scanning %s SQL candidates; "
            "using sid_guard-only sessions behind browser readiness/enrichment gates",
            len(candidates),
        )
        ranked_candidates = last_resort_candidates
    elif not ranked_candidates and last_resort_candidates:
        logger.warning(
            "[ACCOUNT_SERVICE] Skipping %s sid_guard-only main session(s): "
            "last-resort weak browser sessions are disabled by settings",
            len(last_resort_candidates),
        )

    ranked_candidates.sort(key=lambda item: item[0])
    logger.info(f"[ACCOUNT_SERVICE] Eligible ranked candidates for main comment: {len(ranked_candidates)}")
    if not ranked_candidates:
        try:
            diag_log = logger.info if normalized_exclude_proxies else logger.warning
            diag_log(
                "[ACCOUNT_SERVICE_DIAG] No eligible ranked candidates after runtime filters. "
                "Inspecting up to 20 SQL candidates."
            )
            for diag_acc in candidates[:20]:
                reasons: list[str] = []
                try:
                    diag_state = _read_runtime_state(diag_acc, now)
                    diag_lifecycle = _read_account_lifecycle(diag_acc, now)
                    diag_session = _read_account_session_quality(diag_acc, MAIN_SCOPE, now)
                    diag_lane = _read_account_spend_lane(diag_acc, MAIN_SCOPE, now)
                    diag_proxy = str(getattr(diag_acc, "proxy", "") or "").strip()
                    diag_proxy_host = _proxy_source_host(diag_proxy)

                    if not _has_auth_material(diag_acc):
                        reasons.append("no_auth_material")
                    if diag_proxy and diag_proxy in normalized_exclude_proxies:
                        reasons.append("excluded_exact_proxy")
                    if diag_proxy_host and diag_proxy_host in normalized_exclude_proxy_hosts:
                        reasons.append(f"excluded_proxy_source_host={diag_proxy_host}")
                    if diag_lifecycle.get("hard_expired"):
                        reasons.append("hard_expired")
                    if diag_state.get("active_cooldown"):
                        reasons.append("runtime_cooldown")
                    if diag_state.get("bucket") in {"dead_session", "proxy_issue"}:
                        reasons.append(f"runtime_bucket={diag_state.get('bucket')}")
                    if diag_session.get("bucket") == SESSION_BUCKET_DEAD:
                        reasons.append("session_dead")
                    if _is_publish_session_last_resort(diag_session):
                        reasons.append("publish_session_last_resort")
                        if not _allow_publish_session_last_resort():
                            reasons.append("publish_session_last_resort_disabled")
                    if diag_lane.get("lane") == ACCOUNT_LANE_RETIRE:
                        reasons.append("lane_retire")

                    logger.warning(
                        "[ACCOUNT_SERVICE_DIAG] acc=%s status=%s proxy=%s errors=%s "
                        "state=%s lifecycle=%s session=%s lane=%s reasons=%s",
                        getattr(diag_acc, "id", None),
                        getattr(diag_acc, "status", None),
                        bool(getattr(diag_acc, "proxy", None)),
                        getattr(diag_acc, "error_count_recent", None),
                        diag_state,
                        diag_lifecycle,
                        diag_session,
                        diag_lane,
                        reasons or ["passed_sql_but_ranker_filtered_unknown"],
                    )
                except Exception as per_acc_diag_err:
                    logger.warning(
                        "[ACCOUNT_SERVICE_DIAG] failed for account %s: %s",
                        getattr(diag_acc, "id", None),
                        per_acc_diag_err,
                    )
        except Exception as diag_err:
            logger.warning(f"[ACCOUNT_SERVICE_DIAG] failed: {diag_err}")
    account = None
    account_state = None
    account_score = None
    for _, candidate_account, candidate_state, candidate_score in ranked_candidates:
        if reserve_main_account(candidate_account.id):
            account = candidate_account
            account_state = candidate_state
            account_score = candidate_score
            break
        logger.debug(
            "[ACCOUNT_SERVICE] Candidate account %s was already reserved by another worker; skipping",
            candidate_account.id,
        )

    if account and settings.PROXY_GEO_MATCHING and (logger.isEnabledFor(logging.DEBUG) or logger.isEnabledFor(logging.WARNING)):
        try:
            if account.proxy:
                proxy_obj = db.query(Proxy).filter(Proxy.proxy == account.proxy).first()
                if proxy_obj:
                    if account.country_code and proxy_obj.country_code:
                        if account.country_code == proxy_obj.country_code:
                            logger.debug(f"[GEO] Selected account {account.id} for main comment: geo-matched ({account.country_code}-{proxy_obj.country_code})")
                        else:
                            logger.warning(f"[GEO] Selected account {account.id} for main comment: geo-mismatch (account: {account.country_code}, proxy: {proxy_obj.country_code}) - may increase CAPTCHA risk")
                    elif not account.country_code:
                        logger.warning(f"[GEO] Selected account {account.id} for main comment: no country_code set (proxy: {proxy_obj.country_code or 'unknown'}) - recommend setting country_code")
                    elif not proxy_obj.country_code:
                        logger.debug(f"[GEO] Selected account {account.id} for main comment: proxy has no country_code (account: {account.country_code})")
                else:
                    logger.debug(f"[GEO] Selected account {account.id} for main comment: proxy {account.proxy[:30]} not in Proxy table")
        except Exception as log_err:
            logger.debug(f"[GEO] Could not log geo match for account {account.id}: {log_err}")

    if account:
        lifecycle = _read_account_lifecycle(account, now)
        selection_proxy_obj = None
        if account.proxy:
            try:
                selection_proxy_obj = db.query(Proxy).filter(Proxy.proxy == account.proxy).first()
            except Exception as proxy_diag_err:
                logger.debug(
                    "[ACCOUNT_SERVICE] Could not load proxy diagnostics for main account %s: %s",
                    account.id,
                    proxy_diag_err,
                )
        logger.info(
            f"[ACCOUNT_SERVICE] Selected main-comment account: id={account.id}, "
            f"status={account.status}, score={account_score}, "
            f"bucket={account_state['bucket'] if account_state else 'n/a'}, "
            f"category={account_state['category'] if account_state else 'n/a'}, "
            f"session={_read_account_session_quality(account, MAIN_SCOPE, now)['bucket']}, "
            f"lane={_read_account_spend_lane(account, MAIN_SCOPE, now)['lane']}, "
            f"lifecycle={lifecycle['stage']}, "
            f"{_account_selection_diagnostics(account, now, proxy_obj=selection_proxy_obj)}"
        )

    return account


def _parse_reply_cooldown_until(last_error: str | None) -> datetime | None:
    if not last_error:
        return None
    try:
        error_data = json.loads(last_error)
    except (json.JSONDecodeError, TypeError):
        return None

    cooldown_until_str = error_data.get("cooldown_until")
    if not cooldown_until_str:
        return None

    try:
        return datetime.fromisoformat(cooldown_until_str.replace("Z", "+00:00"))
    except (ValueError, TypeError):
        return None


def get_free_account_for_reply(
    db: Session,
    exclude_ids: list[int] | None = None,
    *,
    exclude_proxies: set[str] | list[str] | tuple[str, ...] | None = None,
    exclude_proxy_source_hosts: bool | None = None,
    exclude_degraded_categories: set[str] | list[str] | tuple[str, ...] | None = None,
    pool_owner_user_id: int | None = None,
    reserve: bool = False,
) -> Account | None:
    """
    Picks a reply account in small batches so we do not lock and load the
    whole candidate set at once.

    pool_owner_user_id: то же, что и для main-селектора, см. его docstring.
    Per-account override: today_replies < COALESCE(daily_reply_limit, MAX_ACTIONS_PER_ACCOUNT).
    """
    daily_reply_limit_expr = func.coalesce(
        Account.daily_reply_limit, settings.MAX_ACTIONS_PER_ACCOUNT
    )

    if pool_owner_user_id is None:
        pool_filter = Account.owner_user_id.is_(None)
    else:
        pool_filter = Account.owner_user_id == int(pool_owner_user_id)

    query = db.query(Account).filter(
        and_(
            or_(
                Account.status == "active",
                and_(
                    Account.status == "error",
                    Account.error_count_recent < ERROR_THRESHOLD
                )
            ),
            Account.status.notin_(["dead", "reauth_needed", "banned"]),
            or_(
                and_(Account.cookies.isnot(None), Account.cookies != ""),
                and_(Account.cookie_file.isnot(None), Account.cookie_file != ""),
            ),
            pool_filter,
            Account.today_replies < daily_reply_limit_expr,
        )
    )

    if settings.USE_PROXY:
        query = query.filter(
            and_(
                Account.proxy.isnot(None),
                Account.proxy != ""
            )
        )

    excluded_account_ids: set[int] = set(exclude_ids or [])
    if reserve:
        excluded_account_ids.update(get_reserved_reply_account_ids())
    if excluded_account_ids:
        query = query.filter(~Account.id.in_(list(excluded_account_ids)))

    normalized_exclude_proxies = {
        str(proxy).strip()
        for proxy in (exclude_proxies or [])
        if proxy and str(proxy).strip()
    }
    normalized_exclude_degraded_categories = {
        str(category).strip()
        for category in (exclude_degraded_categories or [])
        if category and str(category).strip()
    }
    exclude_same_proxy_source_host = (
        _parallel_proxy_diversify_by_source_host()
        if exclude_proxy_source_hosts is None
        else bool(exclude_proxy_source_hosts)
    )
    normalized_exclude_proxy_hosts = (
        {host for host in (_proxy_source_host(proxy) for proxy in normalized_exclude_proxies) if host}
        if exclude_same_proxy_source_host
        else set()
    )
    if normalized_exclude_proxies:
        query = query.filter(
            or_(
                Account.proxy.is_(None),
                ~Account.proxy.in_(sorted(normalized_exclude_proxies)),
            )
        )

    now = datetime.now(timezone.utc)
    proxy_type_filter = get_proxy_type_filter(db)
    proxy_prefer_mobile = get_proxy_prefer_mobile(db)
    order_by_clauses = [
        (Account.status != "active").asc(),
        Account.error_count_recent.asc().nulls_first(),
        Account.last_used_at.asc().nulls_first(),
        Account.id.asc(),
    ]

    bind = getattr(db, "bind", None)
    dialect_name = getattr(getattr(bind, "dialect", None), "name", "") or ""
    locking_enabled = True
    supports_skip_locked = dialect_name not in {"", "sqlite"}
    batch_size = max(20, min(100, getattr(settings, "MAX_CONCURRENT_BROWSERS", 10) * 4))
    seen_ids: set[int] = set(excluded_account_ids)
    runtime_last_resort_accounts: list[tuple[Account, dict[str, Any], int]] = []
    last_resort_accounts: list[tuple[Account, dict[str, Any], int]] = []
    scanned_count = 0

    while True:
        batch_query = query
        if seen_ids:
            batch_query = batch_query.filter(~Account.id.in_(list(seen_ids)))
        batch_query = batch_query.order_by(*order_by_clauses).limit(batch_size)

        try:
            if locking_enabled:
                if supports_skip_locked:
                    accounts = batch_query.with_for_update(skip_locked=True).all()
                else:
                    accounts = batch_query.with_for_update().all()
            else:
                accounts = batch_query.all()
        except Exception as lock_error:
            if locking_enabled:
                logger.warning(f"Reply account selection lock degraded to regular query: {lock_error}")
                locking_enabled = False
                continue
            raise

        if not accounts:
            if runtime_last_resort_accounts:
                logger.warning(
                    "[ACCOUNT_SERVICE] Only recently degraded reply runtime sessions available after scanning %s SQL candidates; "
                    "using them as last resort behind browser readiness gates",
                    scanned_count,
                )
                accounts = []
            elif last_resort_accounts and _allow_publish_session_last_resort():
                logger.warning(
                    "[ACCOUNT_SERVICE] Only last-resort reply sessions available after scanning %s SQL candidates; "
                    "using sid_guard-only sessions behind browser readiness/enrichment gates",
                    scanned_count,
                )
                accounts = []
            elif last_resort_accounts:
                logger.warning(
                    "[ACCOUNT_SERVICE] Skipping %s sid_guard-only reply session(s): "
                    "last-resort weak browser sessions are disabled by settings",
                    len(last_resort_accounts),
                )
                return None
            else:
                return None

        batch_start_index = scanned_count
        scanned_count += len(accounts)

        using_reserve_accounts = False
        if not accounts:
            available_accounts = runtime_last_resort_accounts or last_resort_accounts
            using_reserve_accounts = bool(available_accounts)
        else:
            seen_ids.update(acc.id for acc in accounts)

            available_accounts: list[tuple[Account, dict[str, Any], int]] = []
            for index, acc in enumerate(accounts):
                acc_proxy = str(acc.proxy or "").strip()
                if acc_proxy:
                    if acc_proxy in normalized_exclude_proxies:
                        continue
                    if exclude_same_proxy_source_host and _proxy_source_host(acc_proxy) in normalized_exclude_proxy_hosts:
                        continue
                if not _has_auth_material(acc):
                    continue
                state = _read_runtime_state(acc, now)
                lifecycle = _read_account_lifecycle(acc, now)
                session_quality = _read_account_session_quality(acc, REPLY_SCOPE, now)
                spend_lane = _read_account_spend_lane(acc, REPLY_SCOPE, now)
                if lifecycle["hard_expired"]:
                    continue
                if session_quality["bucket"] == SESSION_BUCKET_DEAD:
                    continue
                if spend_lane["lane"] == ACCOUNT_LANE_RETIRE:
                    continue
                if state["active_cooldown"]:
                    cooldown_until = state.get("cooldown_until")
                    if cooldown_until:
                        logger.debug(
                            f"[COOLDOWN] Account {acc.id} in cooldown until {cooldown_until.isoformat()} "
                            f"({(cooldown_until - now).total_seconds() / 60:.1f} minutes remaining)"
                        )
                    continue
                if state["bucket"] in {"dead_session", "proxy_issue"}:
                    continue
                ranked_index = batch_start_index + index
                if (
                    normalized_exclude_degraded_categories
                    and state["bucket"] == "degraded"
                    and str(state.get("category") or "").strip() in normalized_exclude_degraded_categories
                ):
                    if _is_publish_session_last_resort(session_quality):
                        last_resort_accounts.append((acc, state, ranked_index))
                    elif _runtime_health_is_selection_last_resort(state):
                        runtime_last_resort_accounts.append((acc, state, ranked_index))
                    continue
                if _is_publish_session_last_resort(session_quality):
                    last_resort_accounts.append((acc, state, ranked_index))
                    continue
                if _runtime_health_is_selection_last_resort(state):
                    runtime_last_resort_accounts.append((acc, state, ranked_index))
                    continue
                available_accounts.append((acc, state, ranked_index))

        if (
            not available_accounts
            and len(accounts) < batch_size
            and runtime_last_resort_accounts
        ):
            logger.warning(
                "[ACCOUNT_SERVICE] Only recently degraded reply runtime sessions available after scanning %s SQL candidates; "
            "using them as last resort behind browser readiness gates",
                scanned_count,
            )
            available_accounts = runtime_last_resort_accounts
            using_reserve_accounts = True
        elif (
            not available_accounts
            and len(accounts) < batch_size
            and last_resort_accounts
            and _allow_publish_session_last_resort()
        ):
            logger.warning(
                "[ACCOUNT_SERVICE] Only last-resort reply sessions available after scanning %s SQL candidates; "
                "using sid_guard-only sessions behind browser readiness/enrichment gates",
                scanned_count,
            )
            available_accounts = last_resort_accounts
            using_reserve_accounts = True
        elif not available_accounts and len(accounts) < batch_size and last_resort_accounts:
            logger.warning(
                "[ACCOUNT_SERVICE] Skipping %s sid_guard-only reply session(s): "
                "last-resort weak browser sessions are disabled by settings",
                len(last_resort_accounts),
            )

        if not available_accounts and len(accounts) >= batch_size:
            continue

        if not available_accounts:
            return None

        proxy_map = {}
        if available_accounts:
            try:
                proxy_values = list({
                    acc.proxy
                    for pool in (available_accounts, runtime_last_resort_accounts, last_resort_accounts)
                    for acc, _, _ in pool
                    if acc.proxy
                })
                if proxy_values:
                    proxies = db.query(Proxy).filter(Proxy.proxy.in_(proxy_values)).all()
                    proxy_map = {p.proxy: p for p in proxies}
            except Exception as proxy_err:
                logger.debug(f"[REPLY] Proxy map build failed: {proxy_err}")

        def _proxy_matches_type(acc: Account) -> bool:
            if not proxy_type_filter:
                return True
            proxy_obj = proxy_map.get(acc.proxy)
            if not proxy_obj:
                return True
            if proxy_type_filter.lower() == "mobile":
                return proxy_obj.is_mobile == "Y"
            if proxy_type_filter.lower() == "residential":
                return proxy_obj.is_mobile != "Y"
            return True

        def _proxy_is_mobile(acc: Account) -> bool:
            proxy_obj = proxy_map.get(acc.proxy)
            return bool(proxy_obj and proxy_obj.is_mobile == "Y")

        if proxy_type_filter:
            filtered_accounts = [
                item for item in available_accounts
                if _proxy_matches_type(item[0])
            ]
            if not filtered_accounts and using_reserve_accounts:
                reserve_pools = [runtime_last_resort_accounts]
                if _allow_publish_session_last_resort():
                    reserve_pools.append(last_resort_accounts)
                for fallback_accounts in reserve_pools:
                    if not fallback_accounts:
                        continue
                    filtered_accounts = [
                        item for item in fallback_accounts
                        if _proxy_matches_type(item[0])
                    ]
                    if filtered_accounts:
                        if fallback_accounts is not available_accounts:
                            logger.warning(
                                "[ACCOUNT_SERVICE] Reply last-resort proxy-type filter skipped preferred reserve bucket; "
                                "using alternate last-resort bucket with matching proxy type"
                            )
                        break
            available_accounts = filtered_accounts
        elif proxy_prefer_mobile:
            available_accounts.sort(key=lambda item: 1 if _proxy_is_mobile(item[0]) else 0, reverse=True)

        if settings.PROXY_GEO_MATCHING and available_accounts:
            def _geo_score(acc: Account) -> int:
                proxy_obj = proxy_map.get(acc.proxy)
                if not proxy_obj or not acc.country_code or not proxy_obj.country_code:
                    return 0
                return 1 if acc.country_code == proxy_obj.country_code else -1

            available_accounts.sort(key=lambda item: _geo_score(item[0]), reverse=True)

        if available_accounts:
            ranked_available = []
            for acc, state, index in available_accounts:
                lifecycle = _read_account_lifecycle(acc, now)
                spend_lane = _read_account_spend_lane(acc, REPLY_SCOPE, now)
                score = _calculate_scope_quality_score(acc, state, REPLY_SCOPE, now)
                ranked_available.append(
                    (
                        (
                            RUNTIME_BUCKET_ORDER.get(state["bucket"], 5),
                            0 if acc.status == "active" else 1,
                            _lane_sort_rank(REPLY_SCOPE, spend_lane["lane"]),
                            _lifecycle_sort_rank(lifecycle["stage"]),
                            -score,
                            int(acc.error_count_recent or 0),
                            index,
                        ),
                        acc,
                        state,
                        score,
                    )
                )

            ranked_available.sort(key=lambda item: item[0])
            account = None
            account_state = None
            account_score = None
            for _, candidate_account, candidate_state, candidate_score in ranked_available:
                if reserve and not reserve_reply_account(candidate_account.id):
                    seen_ids.add(candidate_account.id)
                    logger.debug(
                        "[ACCOUNT_SERVICE] Candidate reply account %s was already reserved by another worker; skipping",
                        candidate_account.id,
                    )
                    continue
                account = candidate_account
                account_state = candidate_state
                account_score = candidate_score
                break
            if account is None:
                if len(accounts) >= batch_size:
                    continue
                return None
            account_proxy_obj = proxy_map.get(account.proxy) if account.proxy else None
            if settings.PROXY_GEO_MATCHING:
                try:
                    proxy_obj = account_proxy_obj
                    if proxy_obj:
                        if account.country_code and proxy_obj.country_code:
                            if account.country_code == proxy_obj.country_code:
                                logger.debug(f"[GEO] Selected account {account.id} for reply: geo-matched ({account.country_code}-{proxy_obj.country_code})")
                            else:
                                logger.warning(f"[GEO] Selected account {account.id} for reply: geo-mismatch (account: {account.country_code}, proxy: {proxy_obj.country_code}) - may increase CAPTCHA risk")
                        elif not account.country_code:
                            logger.warning(f"[GEO] Selected account {account.id} for reply: no country_code set (proxy: {proxy_obj.country_code or 'unknown'}) - recommend setting country_code")
                        elif not proxy_obj.country_code:
                            logger.debug(f"[GEO] Selected account {account.id} for reply: proxy has no country_code (account: {account.country_code})")
                    elif account.proxy:
                        logger.warning(f"[GEO] Selected account {account.id} for reply: proxy {account.proxy[:30]} not found in Proxy table")
                except Exception as log_err:
                    logger.debug(f"[GEO] Could not log geo match for account {account.id}: {log_err}")
            logger.info(
                f"[ACCOUNT_SERVICE] Selected reply account: id={account.id}, "
                f"status={account.status}, score={account_score}, "
                f"bucket={account_state['bucket']}, category={account_state['category']}, "
                f"session={_read_account_session_quality(account, REPLY_SCOPE, now)['bucket']}, "
                f"lane={_read_account_spend_lane(account, REPLY_SCOPE, now)['lane']}, "
                f"lifecycle={_read_account_lifecycle(account, now)['stage']}, "
                f"{_account_selection_diagnostics(account, now, proxy_obj=account_proxy_obj)}"
            )
            return account

        if len(accounts) < batch_size:
            return None


def mark_soft_error(db: Session, account_id: int, error_message: str = None) -> None:
    """Marks a temporary account error and escalates to status=error only after the threshold."""
    account = db.query(Account).filter(Account.id == account_id).with_for_update().first()
    if account:
        account.error_count_recent = (account.error_count_recent or 0) + 1
        if error_message:
            normalized_reason = _normalize_reason(error_message, max_len=240)
            if _looks_like_monitor_error(normalized_reason):
                account.last_error = _serialize_error_payload(
                    _build_monitor_error_payload(normalized_reason)
                )
            elif _looks_like_runtime_error(normalized_reason):
                account.last_error = _serialize_error_payload(
                    _build_runtime_error_payload(normalized_reason)
                )
            else:
                account.last_error = normalized_reason[:500] if normalized_reason else None
        now = datetime.now(timezone.utc)
        account.last_checked_at = now
        account.last_used_at = now

        if account.error_count_recent >= ERROR_THRESHOLD:
            account.status = "error"
            logger.warning(
                f"Account {account_id} marked as error "
                f"(error_count_recent={account.error_count_recent} >= {ERROR_THRESHOLD})"
            )

        try:
            db.commit()
        except Exception as e:
            db.rollback()
            logger.error(f"Error committing soft error for account {account_id}: {e}", exc_info=True)
            raise


def mark_runtime_cooldown(
    db: Session,
    account_id: int,
    reason: str,
    *,
    cooldown_minutes: int,
    category: str | None = None,
    scope: str = RUNTIME_SCOPE,
) -> None:
    """Apply an explicit runtime cooldown through the structured last_error payload."""
    account = db.query(Account).filter(Account.id == account_id).with_for_update().first()
    if not account:
        return

    account.error_count_recent = (account.error_count_recent or 0) + 1
    normalized_reason = _normalize_reason(reason, max_len=240)
    account.last_error = _serialize_error_payload(
        _build_runtime_error_payload(
            normalized_reason,
            category=category,
            cooldown_minutes=cooldown_minutes,
            scope=scope,
        )
    )
    now = datetime.now(timezone.utc)
    account.last_checked_at = now
    account.last_used_at = now

    if account.error_count_recent >= ERROR_THRESHOLD:
        account.status = "error"
        logger.warning(
            f"Account {account_id} marked as error "
            f"(error_count_recent={account.error_count_recent} >= {ERROR_THRESHOLD})"
        )

    try:
        db.commit()
    except Exception as e:
        db.rollback()
        logger.error(f"Error committing runtime cooldown for account {account_id}: {e}", exc_info=True)
        raise


def mark_error(db: Session, account_id: int) -> None:
    """Backward-compatible alias for mark_soft_error."""
    mark_soft_error(db, account_id)


def mark_needs_reauth(db: Session, account_id: int, reason: str | None = None) -> None:
    """Marks an account as requiring fresh cookies before next use."""
    account = db.query(Account).filter(Account.id == account_id).with_for_update().first()
    if not account:
        return

    account.status = "reauth_needed"
    now = datetime.now(timezone.utc)
    account.last_checked_at = now
    account.last_used_at = now
    normalized_reason = _normalize_reason(reason or "Needs re-authentication")
    if normalized_reason:
        account.last_error = normalized_reason[:500]

    try:
        db.commit()
    except Exception as exc:
        db.rollback()
        logger.error(f"Error marking account {account_id} as reauth_needed: {exc}", exc_info=True)
        raise


def mark_dead(db: Session, account_id: int, reason: str | None = None) -> None:
    """Marks an account as dead and stores a structured reason when possible."""
    account = db.query(Account).filter(Account.id == account_id).with_for_update().first()
    if not account:
        return

    account.status = "dead"
    account.error_count_recent = max(int(account.error_count_recent or 0), ERROR_THRESHOLD)
    account.last_checked_at = datetime.now(timezone.utc)
    normalized_reason = _normalize_reason(reason or "Dead session detected")
    if normalized_reason:
        if _looks_like_monitor_error(normalized_reason) or "validation failed" in normalized_reason.lower():
            account.last_error = _serialize_error_payload(
                _build_monitor_error_payload(
                    normalized_reason,
                    category=MONITOR_CATEGORY_LOGIN,
                    cooldown_minutes=None,
                    fatal=True,
                )
            )
        elif _looks_like_runtime_error(normalized_reason):
            account.last_error = _serialize_error_payload(
                _build_runtime_error_payload(
                    normalized_reason,
                    category=MONITOR_CATEGORY_LOGIN,
                    cooldown_minutes=None,
                    fatal=True,
                )
            )
        else:
            account.last_error = normalized_reason[:500]

    try:
        db.commit()
    except Exception as e:
        db.rollback()
        logger.error(f"Error marking account {account_id} as dead: {e}", exc_info=True)
        raise


def mark_success(db: Session, account_id: int) -> None:
    """Marks account as successfully used and clears transient error state."""
    account = db.query(Account).filter(Account.id == account_id).with_for_update().first()
    if not account:
        return

    account.error_count_recent = 0
    account.validation_fail_count = 0
    account.last_error = None
    account.last_checked_at = datetime.now(timezone.utc)
    account.last_validation_ok = datetime.now(timezone.utc)
    if account.status != "dead":
        account.status = "active"

    try:
        db.commit()
    except Exception as e:
        db.rollback()
        logger.error(f"Error committing success state for account {account_id}: {e}", exc_info=True)
        raise


def count_viable_accounts_for_http(
    db: Session,
    *,
    pool_owner_user_id: int | None = None,
) -> int:
    """Count accounts that are still allowed into the hybrid comment pool.

    pool_owner_user_id=None  → общий пул (Account.owner_user_id IS NULL).
    pool_owner_user_id=<int> → личный пул конкретного работника.
    """
    now = datetime.now(timezone.utc)
    cooldown_threshold = now - timedelta(seconds=settings.ACCOUNT_COOLDOWN_SECONDS)
    auth_material = or_(
        and_(Account.cookies.isnot(None), Account.cookies != ""),
        and_(Account.cookie_file.isnot(None), Account.cookie_file != ""),
    )
    daily_main_limit_expr = func.coalesce(
        Account.daily_main_limit, settings.MAX_ACTIONS_PER_ACCOUNT
    )

    if pool_owner_user_id is None:
        pool_filter = Account.owner_user_id.is_(None)
    else:
        pool_filter = Account.owner_user_id == int(pool_owner_user_id)

    query = db.query(Account).filter(
        or_(
            Account.status == "active",
            and_(Account.status == "error", Account.error_count_recent < ERROR_THRESHOLD),
        ),
        Account.status.notin_(["dead", "reauth_needed", "banned"]),
        auth_material,
        pool_filter,
        Account.today_main_comments < daily_main_limit_expr,
        or_(
            Account.last_main_comment_at.is_(None),
            Account.last_main_comment_at < cooldown_threshold,
        ),
    )
    if settings.USE_PROXY:
        query = query.filter(Account.proxy.isnot(None), Account.proxy != "")

    eligible = 0
    for account in query.all():
        if not _has_auth_material(account):
            continue
        state = _read_runtime_state(account, now)
        lifecycle = _read_account_lifecycle(account, now)
        session_quality = _read_account_session_quality(account, MAIN_SCOPE, now)
        spend_lane = _read_account_spend_lane(account, MAIN_SCOPE, now)
        if lifecycle["hard_expired"]:
            continue
        if state["active_cooldown"] or state["bucket"] in {"dead_session", "proxy_issue"}:
            continue
        if session_quality["bucket"] == SESSION_BUCKET_DEAD:
            continue
        if spend_lane["lane"] == ACCOUNT_LANE_RETIRE:
            continue
        eligible += 1
    return eligible


def increment_main_comment(db: Session, account_id: int) -> None:
    """Increments main-comment counters for a successful main comment."""
    account = db.query(Account).filter(Account.id == account_id).with_for_update().first()
    if not account:
        return

    now = datetime.now(timezone.utc)
    account.validation_fail_count = 0
    account.today_main_comments = int(account.today_main_comments or 0) + 1
    account.last_main_comment_at = now
    account.last_used_at = now
    account.last_checked_at = now

    try:
        db.commit()
    except Exception as e:
        db.rollback()
        logger.error(f"Error incrementing main comment counter for account {account_id}: {e}", exc_info=True)
        raise


def increment_reply(db: Session, account_id: int) -> None:
    """Increments reply counters for a successful reply."""
    account = db.query(Account).filter(Account.id == account_id).with_for_update().first()
    if not account:
        return

    now = datetime.now(timezone.utc)
    account.validation_fail_count = 0
    account.today_replies = int(account.today_replies or 0) + 1
    account.last_used_at = now
    account.last_checked_at = now

    try:
        db.commit()
    except Exception as e:
        db.rollback()
        logger.error(f"Error incrementing reply counter for account {account_id}: {e}", exc_info=True)
        raise


def record_main_comment_success(db: Session, account_id: int) -> None:
    """Clears transient errors and increments main-comment counters in one transaction."""
    account = db.query(Account).filter(Account.id == account_id).with_for_update().first()
    if not account:
        return

    now = datetime.now(timezone.utc)
    account.error_count_recent = 0
    account.validation_fail_count = 0
    account.last_error = None
    account.last_checked_at = now
    account.last_validation_ok = now
    account.last_main_comment_at = now
    account.last_used_at = now
    account.today_main_comments = int(account.today_main_comments or 0) + 1
    if account.status != "dead":
        account.status = "active"

    try:
        db.commit()
    except Exception as e:
        db.rollback()
        logger.error(f"Error recording main comment success for account {account_id}: {e}", exc_info=True)
        raise


def record_main_comment_attempt(db: Session, account_id: int) -> None:
    """Marks a failed main-comment attempt without counting it as a success.

    The main selector uses last_main_comment_at as its short per-account
    cooldown gate. Updating it here prevents a DOM-broken/browser-broken
    account from being selected again immediately, while today_main_comments
    remains unchanged.
    """
    account = db.query(Account).filter(Account.id == account_id).with_for_update().first()
    if not account:
        return

    now = datetime.now(timezone.utc)
    account.last_main_comment_at = now
    account.last_used_at = now
    account.last_checked_at = now

    try:
        db.commit()
    except Exception as e:
        db.rollback()
        logger.error(f"Error recording main comment attempt for account {account_id}: {e}", exc_info=True)
        raise


def record_reply_success(db: Session, account_id: int) -> None:
    """Clears transient errors and increments reply counters in one transaction."""
    account = db.query(Account).filter(Account.id == account_id).with_for_update().first()
    if not account:
        return

    now = datetime.now(timezone.utc)
    account.error_count_recent = 0
    account.validation_fail_count = 0
    account.last_error = None
    account.last_checked_at = now
    account.last_validation_ok = now
    account.last_used_at = now
    account.today_replies = int(account.today_replies or 0) + 1
    if account.status != "dead":
        account.status = "active"

    try:
        db.commit()
    except Exception as e:
        db.rollback()
        logger.error(f"Error recording reply success for account {account_id}: {e}", exc_info=True)
        raise


def record_reply_attempt(db: Session, account_id: int) -> None:
    """Marks a reply submit attempt without counting it as a confirmed reply."""
    account = db.query(Account).filter(Account.id == account_id).with_for_update().first()
    if not account:
        return

    now = datetime.now(timezone.utc)
    account.last_used_at = now
    account.last_checked_at = now

    try:
        db.commit()
    except Exception as e:
        db.rollback()
        logger.error(f"Error recording reply attempt for account {account_id}: {e}", exc_info=True)
        raise


def mark_monitor_use(db: Session, account_id: int) -> None:
    """Records that the account was used for monitor work."""
    account = db.query(Account).filter(Account.id == account_id).with_for_update().first()
    if not account:
        return

    now = datetime.now(timezone.utc)
    account.last_used_at = now
    account.last_checked_at = now

    try:
        db.commit()
    except Exception as e:
        db.rollback()
        logger.error(f"Error marking monitor use for account {account_id}: {e}", exc_info=True)
        raise


def reset_error_counters(db: Session) -> None:
    """
    Сбрасывает error_count_recent у всех аккаунтов.
    
    Полезно при перезапуске системы или после исправления проблем с прокси.
    """
    updated = db.query(Account).filter(
        Account.error_count_recent > 0
    ).update({
        Account.error_count_recent: 0,
        Account.last_error: None
    })
    
    # Возвращаем error аккаунты в active (кроме dead)
    restored = db.query(Account).filter(
        Account.status == "error"
    ).update({
        Account.status: "active"
    })
    
    try:
        db.commit()
        logger.info(f"Reset error counters: {updated} accounts, restored {restored} to active")
    except Exception as e:
        db.rollback()
        logger.error(f"Error resetting error counters: {e}", exc_info=True)
        raise


def reset_daily_counters(db: Session) -> int:
    """
    Обнуляет дневные счётчики действий у всех аккаунтов.

    Сбрасывает `today_main_comments` и `today_replies` к нулю — без этого аккаунт,
    упёршийся в `MAX_ACTIONS_PER_ACCOUNT`, навсегда выпадает из выборки в
    `get_free_account_for_main_comment` / `get_free_account_for_reply`.

    Должна вызываться раз в сутки (по UTC, около 00:00) из scheduler-а.

    Возвращает количество обновлённых аккаунтов.
    """
    try:
        updated = db.query(Account).filter(
            or_(
                Account.today_main_comments > 0,
                Account.today_replies > 0,
            )
        ).update(
            {
                Account.today_main_comments: 0,
                Account.today_replies: 0,
            },
            synchronize_session=False,
        )
        db.commit()
        logger.info(
            f"Reset daily counters: cleared today_main_comments / today_replies on {updated} account(s)"
        )
        return int(updated or 0)
    except Exception as e:
        db.rollback()
        logger.error(f"Error resetting daily counters: {e}", exc_info=True)
        raise


def get_account_stats(db: Session) -> dict:
    """Возвращает статистику аккаунтов."""
    total = db.query(Account).count()
    active = db.query(Account).filter(Account.status == "active").count()
    dead = db.query(Account).filter(Account.status == "dead").count()
    error = db.query(Account).filter(Account.status == "error").count()
    limited = db.query(Account).filter(Account.status == "limited").count()
    
    # Сколько аккаунтов с временными ошибками (но ещё active)
    with_errors = db.query(Account).filter(
        and_(
            Account.status == "active",
            Account.error_count_recent > 0
        )
    ).count()

    return {
        "total": total,
        "active": active,
        "dead": dead,
        "error": error,
        "limited": limited,
        "with_recent_errors": with_errors
    }


def _build_scope_runtime_stats(
    accounts: list[Account],
    *,
    scope: str,
    now: datetime | None = None,
) -> dict[str, Any]:
    now = now or datetime.now(timezone.utc)
    if scope == MAIN_SCOPE:
        reserved_ids = get_reserved_main_account_ids()
    elif scope == REPLY_SCOPE:
        reserved_ids = get_reserved_reply_account_ids()
    else:
        reserved_ids = set()
    summary = {
        "total_candidates": len(accounts),
        "eligible_now": 0,
        "eligible_including_reserved": 0,
        "reserved": 0,
        "healthy": 0,
        "cooldown": 0,
        "degraded": 0,
        "proxy_issue": 0,
        "dead_session": 0,
        RUNTIME_HEALTH_CAPTCHA_RECENT: 0,
        RUNTIME_HEALTH_PAGE_BLOCKED_RECENT: 0,
        RUNTIME_HEALTH_LOGIN_UI_RECENT: 0,
        RUNTIME_HEALTH_PROXY_ERROR_RECENT: 0,
        RUNTIME_HEALTH_COOKIE_ENRICHMENT_RECENT: 0,
        "runtime_risky": 0,
        "fresh": 0,
        "burn_window": 0,
        "expiring": 0,
        "expired": 0,
        "recently_validated": 0,
        "bad_proxies": 0,
        "session_strong": 0,
        "session_workable": 0,
        "session_degraded": 0,
        "session_dom_only": 0,
        "session_weak": 0,
        "session_dead": 0,
        "keeper": 0,
        "balanced": 0,
        "burner": 0,
        "retire": 0,
        "http_first": 0,
        "http_cautious": 0,
        "dom_first": 0,
        "skip_http": 0,
        "avg_score": 0,
        "best_score": 0,
        "avg_session_score": 0,
        "top_categories": [],
        "top_session_buckets": [],
        "top_session_issues": [],
    }
    scores: list[int] = []
    session_scores: list[int] = []
    category_counts: dict[str, int] = {}
    session_bucket_counts: dict[str, int] = {}
    session_issue_counts: dict[str, int] = {}
    bad_proxy_keys: set[str] = set()
    recent_validation_threshold = now - timedelta(hours=12)
    known_bad_proxy_set = (
        _get_monitor_bad_proxy_set_for_accounts(accounts)
        if scope == MONITOR_SCOPE
        else get_bad_proxy_set(account.proxy for account in accounts if account.proxy) if settings.USE_PROXY else set()
    )

    for account in accounts:
        if scope == MONITOR_SCOPE:
            state = _read_monitor_state(account, now, bad_proxy_set=known_bad_proxy_set)
        else:
            state = _read_runtime_state(account, now, bad_proxy_set=known_bad_proxy_set)
        lifecycle = _read_account_lifecycle(account, now)
        session_quality = _read_account_session_quality(account, scope, now)
        strategy_info = get_http_execution_strategy(account, scope, now)
        spend_lane = _read_account_spend_lane(account, scope, now)

        bucket = state["bucket"]
        health_grade = _runtime_health_grade(state) if scope != MONITOR_SCOPE else RUNTIME_HEALTH_HEALTHY
        summary[bucket] = int(summary.get(bucket, 0)) + 1
        if scope != MONITOR_SCOPE:
            if health_grade == RUNTIME_HEALTH_RISKY:
                summary["runtime_risky"] = int(summary.get("runtime_risky", 0)) + 1
            elif health_grade in {
                RUNTIME_HEALTH_CAPTCHA_RECENT,
                RUNTIME_HEALTH_PAGE_BLOCKED_RECENT,
                RUNTIME_HEALTH_LOGIN_UI_RECENT,
                RUNTIME_HEALTH_PROXY_ERROR_RECENT,
                RUNTIME_HEALTH_COOKIE_ENRICHMENT_RECENT,
            }:
                summary[health_grade] = int(summary.get(health_grade, 0)) + 1
        summary[lifecycle["stage"]] = int(summary.get(lifecycle["stage"], 0)) + 1
        session_key = _SESSION_BUCKET_SCORE_KEYS.get(session_quality["bucket"])
        if session_key:
            summary[session_key] = int(summary.get(session_key, 0)) + 1
            session_bucket_counts[session_quality["bucket"]] = session_bucket_counts.get(session_quality["bucket"], 0) + 1
        lane_key = spend_lane["lane"]
        if lane_key in {ACCOUNT_LANE_KEEPER, ACCOUNT_LANE_BALANCED, ACCOUNT_LANE_BURNER, ACCOUNT_LANE_RETIRE}:
            summary[lane_key] = int(summary.get(lane_key, 0)) + 1
        strategy_key = {
            "http_first": "http_first",
            "http_cautious": "http_cautious",
            "dom_first": "dom_first",
            "skip": "skip_http",
        }.get(strategy_info["strategy"])
        if strategy_key:
            summary[strategy_key] = int(summary.get(strategy_key, 0)) + 1

        if account.status in {"active", "error"} and int(account.error_count_recent or 0) < ERROR_THRESHOLD:
            main_cap = int(account.daily_main_limit or settings.MAX_ACTIONS_PER_ACCOUNT)
            reply_cap = int(account.daily_reply_limit or settings.MAX_ACTIONS_PER_ACCOUNT)
            if scope == MAIN_SCOPE and int(account.today_main_comments or 0) >= main_cap:
                pass
            elif scope == REPLY_SCOPE and int(account.today_replies or 0) >= reply_cap:
                pass
            elif (
                not lifecycle["hard_expired"]
                and not state["active_cooldown"]
                and bucket not in {"dead_session", "proxy_issue"}
                and session_quality["bucket"] != SESSION_BUCKET_DEAD
            ):
                summary["eligible_including_reserved"] += 1
                if account.id in reserved_ids:
                    summary["reserved"] += 1
                else:
                    summary["eligible_now"] += 1

        last_validation_ok = _ensure_utc(account.last_validation_ok)
        if last_validation_ok and last_validation_ok >= recent_validation_threshold:
            summary["recently_validated"] += 1

        category = state["category"]
        if category and category != MONITOR_CATEGORY_UNKNOWN:
            category_counts[category] = category_counts.get(category, 0) + 1
        if state["bucket"] == "proxy_issue" and account.proxy:
            bad_proxy_keys.add(account.proxy)

        quality_reason = session_quality.get("quality_reason")
        if quality_reason:
            session_issue_counts[quality_reason] = session_issue_counts.get(quality_reason, 0) + 1

        score = _calculate_scope_quality_score(account, state, scope, now)
        scores.append(score)
        session_scores.append(int(session_quality["score"]))

    if scores:
        summary["avg_score"] = int(round(sum(scores) / len(scores)))
        summary["best_score"] = max(scores)
    if session_scores:
        summary["avg_session_score"] = int(round(sum(session_scores) / len(session_scores)))
    summary["bad_proxies"] = len(bad_proxy_keys)

    summary["top_categories"] = [
        {"category": category, "count": count}
        for category, count in sorted(category_counts.items(), key=lambda item: (-item[1], item[0]))[:5]
    ]
    summary["top_session_buckets"] = [
        {"bucket": bucket_name, "count": count}
        for bucket_name, count in sorted(session_bucket_counts.items(), key=lambda item: (-item[1], item[0]))[:5]
    ]
    summary["top_session_issues"] = [
        {"issue": issue, "count": count}
        for issue, count in sorted(session_issue_counts.items(), key=lambda item: (-item[1], item[0]))[:5]
    ]
    return summary


def get_account_runtime_health(db: Session) -> dict[str, Any]:
    """Returns runtime pool health for monitor/main/reply account selection."""
    auth_material = or_(
        and_(Account.cookies.isnot(None), Account.cookies != ""),
        and_(Account.cookie_file.isnot(None), Account.cookie_file != ""),
    )
    query = db.query(Account).filter(
        and_(
            Account.status.in_(["active", "error", "dead"]),
            auth_material,
        )
    )
    if settings.USE_PROXY:
        query = query.filter(Account.proxy.isnot(None), Account.proxy != "")

    accounts = query.all()
    now = datetime.now(timezone.utc)
    return {
        "generated_at": now.isoformat(),
        "main_comment": _build_scope_runtime_stats(accounts, scope=MAIN_SCOPE, now=now),
        "reply": _build_scope_runtime_stats(accounts, scope=REPLY_SCOPE, now=now),
        "monitor": _build_scope_runtime_stats(accounts, scope=MONITOR_SCOPE, now=now),
    }


def get_monitor_account(
    db: Session,
    exclude_ids: list[int] | None = None,
    exclude_proxies: set[str] | list[str] | tuple[str, ...] | None = None,
) -> Account | None:
    """
    Выбирает аккаунт для мониторинга (парсинга видео).
    
    ИСПРАВЛЕНО: Использует любые доступные аккаунты, если нет специальных для мониторинга.
    ИСПРАВЛЕНО: Добавлен параметр exclude_ids для автосмены аккаунтов.
    
    Стратегия:
    1. Сначала пытается найти аккаунты, которые редко используются для комментирования
    2. Если таких нет - использует любые активные аккаунты с валидными куками
    3. Мониторинг требует только просмотра страниц, поэтому менее требователен к аккаунтам
    
    Args:
        exclude_ids: Список ID аккаунтов для исключения (для автосмены)
    
    Returns:
        Account | None: Подходящий аккаунт или None если нет доступных
    """
    now = datetime.now(timezone.utc)
    auth_material = or_(
        and_(Account.cookies.isnot(None), Account.cookies != ""),
        and_(Account.cookie_file.isnot(None), Account.cookie_file != ""),
    )
    query = db.query(Account).filter(
        and_(
            Account.status.in_(["active", "error"]),
            Account.status.notin_(["dead", "reauth_needed", "banned"]),
            Account.error_count_recent < ERROR_THRESHOLD,
            auth_material,
        )
    )

    if settings.USE_PROXY:
        query = query.filter(
            Account.proxy.isnot(None),
            Account.proxy != ""
        )
    
    # ИСПРАВЛЕНО: Исключаем аккаунты из exclude_ids
    if exclude_ids:
        query = query.filter(~Account.id.in_(exclude_ids))

    normalized_exclude_proxies = {
        str(proxy).strip()
        for proxy in (exclude_proxies or [])
        if proxy and str(proxy).strip()
    }
    if normalized_exclude_proxies:
        query = query.filter(
            or_(
                Account.proxy.is_(None),
                ~Account.proxy.in_(sorted(normalized_exclude_proxies)),
            )
        )

    try:
        candidate_limit = max(50, int(getattr(settings, "MONITOR_ACCOUNT_CANDIDATE_LIMIT", 250)))
    except (TypeError, ValueError):
        candidate_limit = 250
    try:
        candidates = query.order_by(
            (Account.status != "active").asc(),
            Account.error_count_recent.asc(),
            Account.last_validation_ok.desc().nulls_last(),
            Account.today_main_comments.asc(),
            Account.last_used_at.asc().nulls_first(),
            Account.id.asc(),
        ).with_for_update().limit(candidate_limit).all()
    except Exception as lock_error:
        logger.warning(f"SELECT FOR UPDATE not supported in get_monitor_account, falling back to regular query: {lock_error}")
        candidates = query.order_by(
            (Account.status != "active").asc(),
            Account.error_count_recent.asc(),
            Account.last_validation_ok.desc().nulls_last(),
            Account.today_main_comments.asc(),
            Account.last_used_at.asc().nulls_first(),
            Account.id.asc(),
        ).limit(candidate_limit).all()

    locked_mobile_proxy_values: set[str] = set()
    candidate_proxy_values: set[str] = set()
    if candidates:
        candidate_proxy_values = {
            str(candidate.proxy).strip()
            for candidate in candidates
            if getattr(candidate, "proxy", None) and str(candidate.proxy).strip()
        }
        if candidate_proxy_values:
            try:
                mobile_proxies = (
                    db.query(Proxy)
                    .filter(Proxy.proxy.in_(candidate_proxy_values), Proxy.is_mobile == "Y")
                    .all()
                )
                locked_mobile_proxy_ids = get_locked_proxy_ids(proxy.id for proxy in mobile_proxies)
                locked_mobile_proxy_values = {
                    proxy.proxy
                    for proxy in mobile_proxies
                    if proxy.id in locked_mobile_proxy_ids
                }
            except Exception as lock_lookup_error:
                logger.debug(
                    "[ACCOUNT_SERVICE] Failed to filter locked mobile proxies for monitor selection: %s",
                    lock_lookup_error,
                )

    ranked_candidates: list[tuple[tuple[Any, ...], Account, dict[str, Any], int]] = []
    bad_proxy_set = _get_monitor_bad_proxy_set_for_accounts(candidates)
    for candidate in candidates:
        if candidate.proxy and str(candidate.proxy).strip() in normalized_exclude_proxies:
            continue
        if candidate.proxy and str(candidate.proxy).strip() in locked_mobile_proxy_values:
            continue
        if not _has_auth_material(candidate):
            continue
        state = _read_monitor_state(candidate, now, bad_proxy_set=bad_proxy_set)
        lifecycle = _read_account_lifecycle(candidate, now)
        session_quality = _read_account_session_quality(candidate, MONITOR_SCOPE, now)
        spend_lane = _read_account_spend_lane(candidate, MONITOR_SCOPE, now)
        if lifecycle["hard_expired"]:
            continue
        if session_quality["bucket"] == SESSION_BUCKET_DEAD:
            continue
        if spend_lane["lane"] == ACCOUNT_LANE_RETIRE:
            continue
        if state["active_cooldown"] or state["bucket"] == "proxy_issue":
            continue

        last_ok_ts = 0.0
        last_validation_ok = _ensure_utc(candidate.last_validation_ok)
        if last_validation_ok:
            try:
                last_ok_ts = last_validation_ok.timestamp()
            except Exception:
                last_ok_ts = 0.0
        last_used_at = _ensure_utc(candidate.last_used_at) or datetime(1970, 1, 1, tzinfo=timezone.utc)

        health_rank = {
            "healthy": 0,
            "degraded": 1,
            "proxy_issue": 2,
            "cooldown": 3,
            "dead_session": 4,
        }.get(state["bucket"], 5)

        score = _calculate_scope_quality_score(candidate, state, MONITOR_SCOPE, now)
        ranked_candidates.append(
            (
                (
                    health_rank,
                    0 if candidate.status == "active" else 1,
                    _lane_sort_rank(MONITOR_SCOPE, spend_lane["lane"]),
                    _lifecycle_sort_rank(lifecycle["stage"]),
                    -score,
                    int(candidate.error_count_recent or 0),
                    -last_ok_ts,
                    int(candidate.today_main_comments or 0),
                    last_used_at,
                    candidate.id,
                ),
                candidate,
                state,
                score,
            )
        )

    ranked_candidates.sort(key=lambda item: item[0])
    account = None
    account_state = None
    account_score = None
    for _, candidate_account, candidate_state, candidate_score in ranked_candidates:
        if reserve_main_account(candidate_account.id):
            account = candidate_account
            account_state = candidate_state
            account_score = candidate_score
            break
        logger.debug(
            "[ACCOUNT_SERVICE] Candidate account %s was already reserved by another worker; skipping",
            candidate_account.id,
        )
    
    if account:
        lifecycle = _read_account_lifecycle(account, now)
        logger.info(
            f"[ACCOUNT_SERVICE] Selected monitor account: id={account.id}, "
            f"label={account.label[:20] if account.label else 'N/A'}, "
            f"status={account.status}, today_comments={account.today_main_comments}, "
            f"has_proxy={bool(account.proxy)}, bucket={account_state['bucket'] if account_state else 'n/a'}, "
            f"category={account_state['category'] if account_state else 'n/a'}, "
            f"session={_read_account_session_quality(account, MONITOR_SCOPE, now)['bucket']}, "
            f"lane={_read_account_spend_lane(account, MONITOR_SCOPE, now)['lane']}, "
            f"score={account_score}, lifecycle={lifecycle['stage']}"
        )
    else:
        pool_stats = get_monitor_pool_stats(db)
        all_candidate_proxies_locked = bool(
            candidate_proxy_values
            and locked_mobile_proxy_values
            and candidate_proxy_values.issubset(locked_mobile_proxy_values)
        )
        logger.log(
            logging.INFO if (normalized_exclude_proxies or all_candidate_proxies_locked) else logging.WARNING,
            f"[ACCOUNT_SERVICE] No monitor accounts available. "
            f"Diagnostics: total_candidates={pool_stats['total_candidates']}, "
            f"eligible_now={pool_stats['eligible_now']}, cooldown={pool_stats['cooldown']}, "
            f"proxy_issue={pool_stats['proxy_issue']}, bad_proxies={pool_stats['bad_proxies']}, dead_session={pool_stats['dead_session']}, "
            f"locked_mobile_proxies={len(locked_mobile_proxy_values)}, "
            f"excluded_failed_proxies={len(normalized_exclude_proxies)}"
        )
    
    return account
