"""
Clickable TikTok @mentions for HTTP comment/reply publication.

TikTok Web renders ``@username`` as a real (clickable, notifying) mention only
when the publish request carries a matching ``text_extra`` entry:

    text       = "видел у него @username"
    text_extra = [{"start": 13, "end": 22, "type": 0, "user_id": "...",
                   "sec_uid": "MS4w...", "hashtag_name": ""}]

``start``/``end`` are offsets in JavaScript string units (UTF-16 code units),
because the web client computes them from the textarea value. Python ``str``
indices differ as soon as an emoji precedes the mention, so offsets are
converted explicitly.

``user_id``/``sec_uid`` are resolved inside the already-warmed page:
1. ``/api/user/detail/?uniqueId=...`` via the page's own ``window.fetch``
   (webmssdk signs it like any other page request);
2. fallback: the profile HTML ``/@username`` SSR blob
   (``__UNIVERSAL_DATA_FOR_REHYDRATION__`` → ``webapp.user-detail``).

Features:
- Structured status handling: distinguishing confirmed ``not_found`` from transient errors (no false negative caching).
- Strict validation: both ``user_id`` and ``sec_uid`` must be non-empty.
- Per-username ``asyncio.Lock``: prevents parallel browser workers from stamping on the same profile.
- Persistent disk cache in ``data/mention_cache.json``.
- Case normalization: normalizes ``@Username`` in text to the canonical TikTok ``@unique_id``.
"""
from __future__ import annotations

import asyncio
import json
import logging
import re
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger("executor.http.mentions")

# TikTok unique_id: latin letters, digits, "_" and "."; 2..24 chars.
# The negative lookbehind keeps e-mails ("a@b.c") and "@@x" out.
_MENTION_RE = re.compile(r"(?<![\w@.])@([A-Za-z0-9_.]{2,24})")

_POSITIVE_TTL_SECONDS = 24 * 3600
_NEGATIVE_TTL_SECONDS = 10 * 60
_MAX_CACHE_SIZE = 5000
_DISK_CACHE_PATH = Path("data/mention_cache.json")

_MENTION_CACHE: Dict[str, Tuple[float, Optional[Dict[str, str]]]] = {}
_MENTION_LOCKS: Dict[str, asyncio.Lock] = {}
_LOCK_GLOBAL = asyncio.Lock()

MAX_MENTIONS_PER_COMMENT = 3


def _load_disk_cache() -> None:
    """Load persistent positive mention entries from disk cache on startup."""
    try:
        if _DISK_CACHE_PATH.exists():
            with open(_DISK_CACHE_PATH, "r", encoding="utf-8") as f:
                loaded = json.load(f)
            now = time.monotonic()
            count = 0
            for u, item in loaded.items():
                if isinstance(item, dict) and item.get("user_id") and item.get("sec_uid"):
                    _MENTION_CACHE[u.lower()] = (now + _POSITIVE_TTL_SECONDS, item)
                    count += 1
            if count > 0:
                logger.info("[MENTION] Loaded %d cached mentions from %s", count, _DISK_CACHE_PATH)
    except Exception as exc:
        logger.warning("[MENTION] Failed to load disk cache: %s", exc)


def _save_disk_cache() -> None:
    """Atomically write positive cached entries to data/mention_cache.json."""
    try:
        _DISK_CACHE_PATH.parent.mkdir(parents=True, exist_ok=True)
        serializable = {
            k: v for k, (exp, v) in _MENTION_CACHE.items()
            if v and v.get("user_id") and v.get("sec_uid")
        }
        temp_file = _DISK_CACHE_PATH.with_suffix(".tmp")
        with open(temp_file, "w", encoding="utf-8") as f:
            json.dump(serializable, f, ensure_ascii=False, indent=2)
        temp_file.replace(_DISK_CACHE_PATH)
    except Exception as exc:
        logger.warning("[MENTION] Failed to save disk cache: %s", exc)


# Load existing entries at import time
_load_disk_cache()


async def _get_username_lock(username: str) -> asyncio.Lock:
    key = username.lower()
    async with _LOCK_GLOBAL:
        if key not in _MENTION_LOCKS:
            _MENTION_LOCKS[key] = asyncio.Lock()
        return _MENTION_LOCKS[key]


def find_mentions(text: str) -> List[Tuple[int, int, str]]:
    """Return ``(py_start, py_end, username)`` for each ``@username`` in text."""
    found: List[Tuple[int, int, str]] = []
    for match in _MENTION_RE.finditer(text or ""):
        username = match.group(1)
        # A trailing period usually ends the sentence ("...у @user."), and
        # TikTok usernames cannot end with ".".
        stripped = username.rstrip(".")
        if len(stripped) < 2:
            continue
        start = match.start()
        end = start + 1 + len(stripped)
        found.append((start, end, stripped))
    return found


def utf16_offset(text: str, py_index: int) -> int:
    """Convert a Python string index into a JavaScript (UTF-16) offset."""
    return len(text[:py_index].encode("utf-16-le")) // 2


def build_text_extra(text: str, resolved: Dict[str, Dict[str, str]]) -> List[Dict[str, Any]]:
    """Build TikTok ``text_extra`` entries for resolved mentions in ``text``."""
    entries: List[Dict[str, Any]] = []
    for py_start, py_end, username in find_mentions(text):
        info = resolved.get(username.lower())
        if not info or not info.get("user_id") or not info.get("sec_uid"):
            continue
        entries.append({
            "start": utf16_offset(text, py_start),
            "end": utf16_offset(text, py_end),
            "type": 0,
            "user_id": str(info["user_id"]),
            "sec_uid": str(info["sec_uid"]),
            "hashtag_name": "",
        })
        if len(entries) >= MAX_MENTIONS_PER_COMMENT:
            break
    return entries


def _cache_get(username: str) -> Tuple[bool, Optional[Dict[str, str]]]:
    item = _MENTION_CACHE.get(username.lower())
    if not item:
        return False, None
    expires_at, value = item
    if time.monotonic() > expires_at:
        _MENTION_CACHE.pop(username.lower(), None)
        return False, None
    return True, value


def _cache_put(username: str, value: Optional[Dict[str, str]]) -> None:
    # Memory safeguard: evict expired or oldest items if cache grows large
    if len(_MENTION_CACHE) >= _MAX_CACHE_SIZE:
        now = time.monotonic()
        expired = [k for k, (exp, _) in _MENTION_CACHE.items() if exp < now]
        for k in expired:
            _MENTION_CACHE.pop(k, None)
        if len(_MENTION_CACHE) >= _MAX_CACHE_SIZE:
            for k in list(_MENTION_CACHE.keys())[:1000]:
                _MENTION_CACHE.pop(k, None)
    ttl = _POSITIVE_TTL_SECONDS if value else _NEGATIVE_TTL_SECONDS
    _MENTION_CACHE[username.lower()] = (time.monotonic() + ttl, value)


_RESOLVE_USER_JS = """async ({username, timeoutMs, runtimeParams}) => {
    const pick = (user) => {
        if (!user) return null;
        const id = user.id || user.uid || user.userId;
        const sec = user.secUid || user.sec_uid || '';
        if (!id || !sec) return null;
        return {
            user_id: String(id),
            sec_uid: String(sec),
            unique_id: String(user.uniqueId || user.unique_id || ''),
        };
    };
    const deadline = Date.now() + timeoutMs;
    const remainingTimeout = () => Math.max(600, deadline - Date.now());

    const withTimeout = (promise, ms) => Promise.race([
        promise,
        new Promise((_, reject) => setTimeout(() => reject(new Error('timeout')), ms)),
    ]);

    let lastError = null;

    // Phase 1: Native webmssdk-signed fetch to user detail API with full params
    try {
        const p = runtimeParams || {};
        const qs = new URLSearchParams({
            aid: '1988',
            app_name: 'tiktok_web',
            device_platform: 'web_pc',
            browser_name: 'Mozilla',
            browser_platform: navigator.platform || 'Win32',
            browser_language: navigator.language || 'en-US',
            channel: 'tiktok_web',
            uniqueId: username,
            secUid: '',
        });
        if (p.device_id) qs.set('device_id', String(p.device_id));
        if (p.odin_id) qs.set('odinId', String(p.odin_id));
        if (p.region) qs.set('region', String(p.region));
        if (p.tz_name) qs.set('tz_name', String(p.tz_name));
        if (p.msToken) qs.set('msToken', String(p.msToken));
        if (p.verifyFp) qs.set('verifyFp', String(p.verifyFp));

        const resp = await withTimeout(
            fetch('/api/user/detail/?' + qs.toString(), {
                credentials: 'include', headers: {accept: 'application/json'},
            }),
            remainingTimeout()
        );

        if (resp.status === 404) {
            return {status: 'not_found', reason: 'http_404', source: 'api_user_detail'};
        }

        const body = await resp.text();
        if (body) {
            try {
                const data = JSON.parse(body);
                const found = pick(data?.userInfo?.user);
                if (found) return {status: 'ok', user: {...found, source: 'api_user_detail'}};
                const code = data?.statusCode ?? data?.status_code;
                if ([10202, 10221, 10222].includes(Number(code))) {
                    return {status: 'not_found', reason: 'api_code_' + code, source: 'api_user_detail'};
                }
                lastError = 'api_missing_user_code_' + code;
            } catch (jsonErr) {
                lastError = 'api_json_parse_error';
            }
        } else {
            lastError = 'api_empty_body';
        }
    } catch (e) {
        lastError = e && e.message ? e.message : 'api_fetch_failed';
    }

    // Phase 2: SSR extraction fallback from profile HTML
    if (Date.now() < deadline) {
        try {
            const resp = await withTimeout(
                fetch('/@' + encodeURIComponent(username), {
                    credentials: 'include', headers: {accept: 'text/html'},
                }),
                remainingTimeout()
            );

            if (resp.status === 404) {
                return {status: 'not_found', reason: 'ssr_http_404', source: 'profile_ssr'};
            }

            const html = await resp.text();
            if (html) {
                const m = html.match(/<script[^>]+id="__UNIVERSAL_DATA_FOR_REHYDRATION__"[^>]*>([\\s\\S]*?)<\\/script>/);
                if (m) {
                    const data = JSON.parse(m[1]);
                    const detail = data?.__DEFAULT_SCOPE__?.['webapp.user-detail'];
                    const found = pick(detail?.userInfo?.user);
                    if (found) return {status: 'ok', user: {...found, source: 'profile_ssr'}};
                    const code = detail?.statusCode ?? detail?.status_code;
                    if ([10202, 10221, 10222].includes(Number(code))) {
                        return {status: 'not_found', reason: 'ssr_code_' + code, source: 'profile_ssr'};
                    }
                }
                if (html.includes("Couldn't find this account") || html.includes('User not found')) {
                    return {status: 'not_found', reason: 'ssr_not_found_marker', source: 'profile_ssr'};
                }
                lastError = 'ssr_missing_user';
            } else {
                lastError = 'ssr_empty_body';
            }
        } catch (e) {
            lastError = e && e.message ? e.message : 'ssr_fetch_failed';
        }
    }

    return {status: 'transient_error', reason: lastError || 'all_phases_failed'};
}"""


async def resolve_mention_user(
    page: Any,
    username: str,
    *,
    timeout_ms: int = 6000,
    runtime_params: Optional[Dict[str, Any]] = None,
) -> Optional[Dict[str, str]]:
    """Resolve ``username`` → ``{user_id, sec_uid, unique_id}`` using the live page."""
    username = str(username or "").strip().lstrip("@")
    if not username:
        return None

    # Check cache outside lock
    hit, cached = _cache_get(username)
    if hit:
        return cached

    lock = await _get_username_lock(username)
    async with lock:
        # Double check cache inside lock
        hit, cached = _cache_get(username)
        if hit:
            return cached

        if page is None:
            return None
        if getattr(page, "is_closed", None) and page.is_closed() is True:
            return None

        try:
            result = await page.evaluate(
                _RESOLVE_USER_JS,
                {
                    "username": username,
                    "timeoutMs": int(timeout_ms),
                    "runtimeParams": runtime_params or {},
                },
                isolated_context=False,
            )
        except Exception as exc:
            logger.warning("[MENTION] resolve failed for @%s: %s", username, str(exc)[:120])
            return None  # transient: do not negative-cache page errors

        if not isinstance(result, dict):
            logger.warning("[MENTION] @%s returned non-dict JS result: %s", username, result)
            return None

        status = result.get("status")

        if status == "ok":
            user_data = result.get("user") or {}
            user_id = str(user_data.get("user_id") or "").strip()
            sec_uid = str(user_data.get("sec_uid") or "").strip()
            unique_id = str(user_data.get("unique_id") or "").strip() or username

            if user_id and sec_uid:
                if unique_id and unique_id.lower() != username.lower():
                    logger.warning(
                        "[MENTION] @%s resolved to different unique_id=@%s; skipping",
                        username, unique_id
                    )
                    return None

                info = {
                    "user_id": user_id,
                    "sec_uid": sec_uid,
                    "unique_id": unique_id,
                }
                logger.info(
                    "[MENTION] @%s resolved user_id=%s, sec_uid=%s... via %s",
                    username, info["user_id"], info["sec_uid"][:12], user_data.get("source")
                )
                _cache_put(username, info)
                _save_disk_cache()
                return info

        elif status == "not_found":
            logger.warning(
                "[MENTION] @%s confirmed not found (%s); negative caching for %ds",
                username, result.get("reason"), _NEGATIVE_TTL_SECONDS
            )
            _cache_put(username, None)
            return None

        else:
            # transient_error: DO NOT put into negative cache!
            logger.warning(
                "[MENTION] @%s transient error during resolve: %s; NOT negative caching",
                username, result.get("reason")
            )
            return None


async def prepare_mentions(
    page: Any,
    text: str,
    runtime_params: Optional[Dict[str, Any]] = None,
) -> Tuple[str, str, Dict[str, Any]]:
    """Return ``(normalized_text, text_extra_json, meta)`` for ``text``.

    ``meta`` = {"requested": [...], "resolved": [...], "unresolved": [...]}.
    Without mentions this returns ``(text, "[]", meta)`` so callers can always pass it.
    """
    mentions = find_mentions(text)
    meta: Dict[str, Any] = {"requested": [], "resolved": [], "unresolved": []}
    if not mentions:
        return text, "[]", meta

    distinct_usernames: List[str] = []
    for _, _, u in mentions:
        if u.lower() not in [x.lower() for x in distinct_usernames]:
            distinct_usernames.append(u)
        if len(distinct_usernames) >= MAX_MENTIONS_PER_COMMENT:
            break

    async def _resolve_one(u: str) -> Tuple[str, Optional[Dict[str, str]]]:
        return u, await resolve_mention_user(page, u, runtime_params=runtime_params)

    results = await asyncio.gather(*[_resolve_one(u) for u in distinct_usernames])
    resolved: Dict[str, Dict[str, str]] = {}
    for u, info in results:
        meta["requested"].append(u)
        if info and info.get("user_id") and info.get("sec_uid"):
            resolved[u.lower()] = info
            meta["resolved"].append(u)
        else:
            meta["unresolved"].append(u)

    # Normalize casing in text according to canonical TikTok unique_id
    normalized_text = text
    for _, _, u in mentions:
        info = resolved.get(u.lower())
        if info and info.get("unique_id"):
            canonical = info["unique_id"]
            if canonical != u:
                normalized_text = re.sub(
                    rf"(?<![\w@.])@{re.escape(u)}(?![\w.])",
                    f"@{canonical}",
                    normalized_text
                )

    entries = build_text_extra(normalized_text, resolved)
    meta["entries"] = len(entries)
    return normalized_text, json.dumps(entries, ensure_ascii=False, separators=(",", ":")), meta


def extract_response_mentions(response_data: Any) -> List[str]:
    """Return mentioned user ids that TikTok kept in the published comment."""
    if not isinstance(response_data, dict):
        return []
    comment = response_data.get("comment") or (response_data.get("data") or {}).get("comment") or {}
    extras = comment.get("text_extra") if isinstance(comment, dict) else None
    ids: List[str] = []
    for item in extras or []:
        if isinstance(item, dict) and str(item.get("type", 0)) == "0":
            uid = item.get("user_id") or item.get("uid")
            if uid:
                ids.append(str(uid))
    return ids
