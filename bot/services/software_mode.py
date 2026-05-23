"""Helpers for the bot-wide feature mode.

Resilience policy
-----------------
Cache layout: (mode, fetched_at).
- Fresh hit (TTL ~5s)        -> serve from cache.
- API call succeeds          -> refresh cache, serve fresh value.
- API call fails AND we have a previous value (any age)
                             -> serve last-known-good and log warning, but do
                                NOT refresh `fetched_at` so we keep retrying on
                                every call instead of pinning the stale value.
- API call fails AND we have NO previous value
                             -> serve the FAIL_SAFE mode (default: parsing,
                                i.e. the strictest mode), never silently switch
                                a parsing-only deployment into full.

Rationale: previously a single failed API call caused us to fall back to "full"
and *cache* it for 5 seconds, which turned a transient network blip into a
real security/billing incident on parsing-only installs.

Full-mode bypass
----------------
Admins, owners, and workers with explicit full_access_override get full mode
regardless of the global setting.
is_parsing_mode(telegram_id=...) and get_software_mode_for_user(telegram_id)
enforce this transparently. The global is_parsing_mode() (no telegram_id) is
unchanged and used by the API/worker layer where no user context is available.
"""

import logging
import time
from typing import Any

from bot.services.api_client import api_client

logger = logging.getLogger("bot.software_mode")

SOFTWARE_MODE_FULL = "full"
SOFTWARE_MODE_PARSING = "parsing"
SOFTWARE_MODES = {SOFTWARE_MODE_FULL, SOFTWARE_MODE_PARSING}

# Strictest mode wins on cold-start failure: a parsing-only install must NEVER
# silently flip to full just because /software-mode endpoint is briefly down.
FAIL_SAFE_MODE = SOFTWARE_MODE_PARSING

_mode_cache: tuple[str, float] | None = None
_MODE_CACHE_TTL = 5.0

# Per-user full-mode bypass cache: telegram_id -> (has_full_mode, fetched_at)
_admin_cache: dict[int, tuple[bool, float]] = {}
_ADMIN_CACHE_TTL = 60.0


def normalize_software_mode(value: Any, default: str = SOFTWARE_MODE_FULL) -> str:
    mode = str(value or "").strip().lower()
    if mode in SOFTWARE_MODES:
        return mode
    return default if default in SOFTWARE_MODES else SOFTWARE_MODE_FULL


def is_parsing_mode_value(value: Any) -> bool:
    return normalize_software_mode(value) == SOFTWARE_MODE_PARSING


async def get_software_mode(*, use_cache: bool = True) -> str:
    global _mode_cache
    now = time.time()
    if use_cache and _mode_cache and now - _mode_cache[1] < _MODE_CACHE_TTL:
        return _mode_cache[0]

    try:
        data = await api_client.get_software_mode_settings()
        mode = normalize_software_mode(data.get("software_mode"))
        _mode_cache = (mode, now)
        return mode
    except Exception as e:
        if _mode_cache is not None:
            stale_age = now - _mode_cache[1]
            logger.warning(
                "software_mode API failed, serving last-known-good '%s' "
                "(age=%.1fs): %s",
                _mode_cache[0], stale_age, e,
            )
            # Intentionally do NOT bump fetched_at — we want every subsequent
            # call to keep retrying the API, not pin the stale value for TTL.
            return _mode_cache[0]

        logger.error(
            "software_mode API failed and no cached value, falling back to "
            "fail-safe mode '%s': %s",
            FAIL_SAFE_MODE, e,
        )
        # Cache the fail-safe so we don't spam the failing API for TTL window.
        _mode_cache = (FAIL_SAFE_MODE, now)
        return FAIL_SAFE_MODE


async def _has_full_mode_bypass(telegram_id: int) -> bool:
    """True if telegram_id may use full bot mode. Cached for 60 s.

    Fast path: OWNER_TELEGRAM_ID from config (no network call).
    Slow path: sync_telegram API call to check role/full_access_override.
    Fail-safe: returns False on API error (keeps parsing restrictions).
    """
    now = time.time()
    entry = _admin_cache.get(telegram_id)
    if entry and now - entry[1] < _ADMIN_CACHE_TTL:
        return entry[0]

    # Fast path — config-only, no network call needed for the owner.
    from bot.config import load_config
    cfg = load_config()
    owner_id = cfg.get("OWNER_TELEGRAM_ID")
    try:
        owner_telegram_id = int(owner_id) if owner_id not in (None, "") else None
    except (TypeError, ValueError):
        owner_telegram_id = None
    if owner_telegram_id is not None and telegram_id == owner_telegram_id:
        _admin_cache[telegram_id] = (True, now)
        return True

    # Slow path — fetch role from backend (api_client has its own user cache).
    try:
        data = await api_client.sync_telegram(telegram_id, None)
        has_full_mode = (
            data.get("role", "user") in ("admin", "owner")
            or bool(data.get("full_access"))
            or (
                bool(data.get("full_access_override"))
                and not bool(data.get("is_blocked"))
            )
        )
        _admin_cache[telegram_id] = (has_full_mode, now)
        return has_full_mode
    except Exception as e:
        logger.debug("Admin check for %d failed, treating as non-admin: %s", telegram_id, e)
        # Do NOT cache on failure so the next call retries.
        return False


async def is_parsing_mode(*, use_cache: bool = True, telegram_id: int | None = None) -> bool:
    """Return True when the system is in parsing-only mode.

    If telegram_id is provided and belongs to an admin/owner, always returns
    False (full mode), regardless of the global setting.
    Callers without user context omit telegram_id — behaviour is unchanged.
    """
    global_parsing = (await get_software_mode(use_cache=use_cache)) == SOFTWARE_MODE_PARSING
    if not global_parsing or telegram_id is None:
        return global_parsing
    return not await _has_full_mode_bypass(telegram_id)


async def get_software_mode_for_user(telegram_id: int | None, *, use_cache: bool = True) -> str:
    """Like get_software_mode(), but returns 'full' for per-user bypasses."""
    mode = await get_software_mode(use_cache=use_cache)
    if mode == SOFTWARE_MODE_PARSING and telegram_id is not None:
        if await _has_full_mode_bypass(telegram_id):
            return SOFTWARE_MODE_FULL
    return mode


def clear_software_mode_cache() -> None:
    global _mode_cache
    _mode_cache = None


def clear_admin_mode_cache(telegram_id: int | None = None) -> None:
    """Clear cached full-mode bypass status. Call when role/access changes."""
    if telegram_id is not None:
        _admin_cache.pop(telegram_id, None)
    else:
        _admin_cache.clear()
