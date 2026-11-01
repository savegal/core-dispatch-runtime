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
import math
import time
import threading
import uuid
import weakref
from executor.cache_lock import synchronized
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple
from services.mention_policy import find_mentions, MAX_MENTIONS_PER_COMMENT, order_mention_identities

logger = logging.getLogger("executor.http.mentions")

# TikTok unique_id: latin letters, digits, "_" and "."; 2..24 chars.
# The negative lookbehind keeps e-mails ("a@b.c") and "@@x" out.

_POSITIVE_TTL_SECONDS = 24 * 3600
_NEGATIVE_TTL_SECONDS = 10 * 60
_MAX_CACHE_SIZE = 5000
_DISK_CACHE_PATH = Path("data/mention_cache.json")

_MENTION_CACHE: Dict[str, Tuple[float, Optional[Dict[str, str]]]] = {}
_MENTION_LOCKS = weakref.WeakKeyDictionary()
_LOCK_GLOBAL = threading.RLock()
_DISK_WRITE_LOCK = threading.Lock()



def _validated_user_info(username, data):
    if not isinstance(data, dict):
        return None
    raw_uid, raw_sec = data.get("user_id"), data.get("sec_uid")
    if isinstance(raw_uid, bool) or not isinstance(raw_uid, (str, int)) or not isinstance(raw_sec, str):
        return None
    uid, sec = str(raw_uid).strip(), raw_sec.strip()
    canonical = data.get("unique_id")
    if (not uid.isascii() or not uid.isdigit() or not uid.strip("0") or not sec
            or not isinstance(canonical, str) or canonical.lower() != username.lower()):
        return None
    return {"user_id": uid, "sec_uid": sec, "unique_id": canonical}


def _load_disk_cache() -> None:
    """Load persistent positive mention entries from disk cache on startup."""
    try:
        if _DISK_CACHE_PATH.exists():
            with open(_DISK_CACHE_PATH, "r", encoding="utf-8") as f:
                loaded = json.load(f)
            if not isinstance(loaded, dict):
                return
            now = time.time()
            count = 0
            for u, item in loaded.items():
                if (isinstance(u, str) and isinstance(item, dict) and item.get("user_id") and item.get("sec_uid")
                        and type(item.get("expires_at")) in (int, float)
                        and math.isfinite(item["expires_at"]) and item["expires_at"] > now):
                    with _LOCK_GLOBAL:
                        _MENTION_CACHE[u.lower()] = (item["expires_at"], {k: v for k, v in item.items() if k != "expires_at"})
                        while len(_MENTION_CACHE) > _MAX_CACHE_SIZE:
                            _MENTION_CACHE.pop(next(iter(_MENTION_CACHE)))
                    count += 1
                    if count >= _MAX_CACHE_SIZE:
                        break
            if count > 0:
                logger.info("[MENTION] Loaded %d cached mentions from %s", count, _DISK_CACHE_PATH)
    except Exception as exc:
        logger.warning("[MENTION] Failed to load disk cache: %s", exc)


def _save_disk_cache(path: Optional[Path] = None) -> None:
    """Atomically write positive cached entries to data/mention_cache.json."""
    with _DISK_WRITE_LOCK:
        _write_disk_cache(path or _DISK_CACHE_PATH)


def _write_disk_cache(path: Path) -> None:
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with _LOCK_GLOBAL:
            serializable = {
                k: {**v, "expires_at": exp} for k, (exp, v) in _MENTION_CACHE.items()
                if exp > time.time() and v and v.get("user_id") and v.get("sec_uid")
            }
        temp_file = path.with_suffix(f".{uuid.uuid4().hex}.tmp")
        with open(temp_file, "w", encoding="utf-8") as f:
            json.dump(serializable, f, ensure_ascii=False, indent=2)
        temp_file.replace(path)
    except Exception as exc:
        logger.warning("[MENTION] Failed to save disk cache: %s", exc)


# Load existing entries at import time
_load_disk_cache()


async def _get_username_lock(username: str) -> asyncio.Lock:
    key = username.lower()
    with _LOCK_GLOBAL:
        locks = _MENTION_LOCKS.setdefault(asyncio.get_running_loop(), weakref.WeakValueDictionary())
        lock = locks.get(key)
        if lock is None:
            lock = asyncio.Lock()
            locks[key] = lock
        return lock


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


@synchronized(_LOCK_GLOBAL)
def _cache_get(username: str) -> Tuple[bool, Optional[Dict[str, str]]]:
    item = _MENTION_CACHE.get(username.lower())
    if not item:
        return False, None
    expires_at, value = item
    if time.time() > expires_at:
        _MENTION_CACHE.pop(username.lower(), None)
        return False, None
    return True, dict(value) if value is not None else None


@synchronized(_LOCK_GLOBAL)
def _cache_put(username: str, value: Optional[Dict[str, str]]) -> None:
    # Memory safeguard: evict expired or oldest items if cache grows large
    if len(_MENTION_CACHE) >= _MAX_CACHE_SIZE:
        now = time.time()
        expired = [k for k, (exp, _) in _MENTION_CACHE.items() if exp < now]
        for k in expired:
            _MENTION_CACHE.pop(k, None)
        if len(_MENTION_CACHE) >= _MAX_CACHE_SIZE:
            for k in list(_MENTION_CACHE.keys())[:1000]:
                _MENTION_CACHE.pop(k, None)
    ttl = _POSITIVE_TTL_SECONDS if value else _NEGATIVE_TTL_SECONDS
    _MENTION_CACHE[username.lower()] = (time.time() + ttl, dict(value) if value is not None else None)


def validate_text_extra(text: str, text_extra: Any) -> list[dict]:
    """Every mention occurrence must map to exactly one valid UTF-16 entity."""
    mentions = find_mentions(text)
    if len(mentions) > MAX_MENTIONS_PER_COMMENT:
        raise ValueError(f"Too many mentions: maximum {MAX_MENTIONS_PER_COMMENT}")
    try:
        entries = json.loads(text_extra) if isinstance(text_extra, str) else text_extra
    except (ValueError, TypeError) as exc:
        raise ValueError("Invalid text_extra JSON") from exc
    if not isinstance(entries, list):
        raise ValueError("text_extra must be an array")
    spans = {(utf16_offset(text, start), utf16_offset(text, end)) for start, end, _ in mentions}
    actual = set()
    for entry in entries:
        if not isinstance(entry, dict) or type(entry.get("type")) is not int or entry["type"] != 0:
            raise ValueError("Invalid mention entity type")
        start, end = entry.get("start"), entry.get("end")
        if type(start) is not int or type(end) is not int or (start, end) not in spans or (start, end) in actual:
            raise ValueError("Invalid or duplicate mention offsets")
        uid = str(entry.get("user_id") or "").strip()
        if not uid.isascii() or not uid.isdigit() or int(uid) <= 0 or not str(entry.get("sec_uid") or "").strip():
            raise ValueError("Mention requires user_id and sec_uid")
        actual.add((start, end))
    if actual != spans:
        raise ValueError("text_extra must cover every mention")
    return entries


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
    const sameEndpoint = (response, path) => {
        try {
            const url = new URL(response.url);
            return url.origin === location.origin && url.pathname.toLowerCase() === path.toLowerCase();
        } catch (error) { return false; }
    };
    const controller = new AbortController();
    const abortTimer = setTimeout(() => controller.abort(), timeoutMs);
    const remainingTimeout = () => Math.max(600, deadline - Date.now());

    const withTimeout = (promise, ms) => Promise.race([
        promise,
        new Promise((_, reject) => setTimeout(() => reject(new Error('timeout')), ms)),
    ]);

    try {
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
        if (p.odin_id || p.odinId) qs.set('odinId', String(p.odin_id || p.odinId));
        if (p.web_id_last_time || p.WebIdLastTime) qs.set('WebIdLastTime', String(p.web_id_last_time || p.WebIdLastTime));
        if (p.region) qs.set('region', String(p.region));
        qs.set('tz_name', String(p.timezone || p.tz_name || Intl.DateTimeFormat().resolvedOptions().timeZone));
        if (p.msToken) qs.set('msToken', String(p.msToken));
        if (p.verifyFp || p.s_v_web_id) qs.set('verifyFp', String(p.verifyFp || p.s_v_web_id));

        const resp = await withTimeout(
            fetch('/api/user/detail/?' + qs.toString(), {
                credentials: 'include', headers: {accept: 'application/json'}, signal: controller.signal,
            }),
            remainingTimeout()
        );

        if (!sameEndpoint(resp, '/api/user/detail/')) throw new Error('api_unexpected_response_url');
        // A missing private API endpoint does not prove that the user is gone.
        // Try the public profile before creating a negative cache entry.
        if (resp.status !== 200) throw new Error('api_http_' + resp.status);

        const body = await resp.text();
        if (body) {
            try {
                const data = JSON.parse(body);
                const code = data?.statusCode ?? data?.status_code;
                const found = code == null || Number(code) === 0 ? pick(data?.userInfo?.user) : null;
                if (found) return {status: 'ok', user: {...found, source: 'api_user_detail'}};
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
                    credentials: 'include', headers: {accept: 'text/html'}, signal: controller.signal,
                }),
                remainingTimeout()
            );

            if (!sameEndpoint(resp, '/@' + encodeURIComponent(username))) throw new Error('ssr_unexpected_response_url');
            if (resp.status === 404) {
                return {status: 'not_found', reason: 'ssr_http_404', source: 'profile_ssr'};
            }
            if (resp.status !== 200) throw new Error('ssr_http_' + resp.status);

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
    } finally { clearTimeout(abortTimer); controller.abort(); }
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

    # Resolve once freshly per order: a saved handle may have changed owners
    # since yesterday's disk cache. All publishing accounts share this binding.
    order_cache = order_mention_identities()
    key = username.lower()
    if order_cache is not None and key in order_cache:
        value = order_cache[key]
        return dict(value) if value is not None else None

    # Check cache outside lock
    hit, cached = _cache_get(username) if order_cache is None else (False, None)
    if hit and (cached is None or _validated_user_info(username, cached)):
        return _validated_user_info(username, cached) if cached is not None else None
    if hit:
        with _LOCK_GLOBAL:
            _MENTION_CACHE.pop(username.lower(), None)

    lock = await _get_username_lock(username)
    async with lock:
        if order_cache is not None and key in order_cache:
            value = order_cache[key]
            return dict(value) if value is not None else None
        # Double check cache inside lock
        hit, cached = _cache_get(username) if order_cache is None else (False, None)
        if hit and (cached is None or _validated_user_info(username, cached)):
            return _validated_user_info(username, cached) if cached is not None else None

        if page is None:
            return None
        if getattr(page, "is_closed", None) and page.is_closed() is True:
            return None

        try:
            result = await asyncio.wait_for(page.evaluate(
                _RESOLVE_USER_JS,
                {
                    "username": username,
                    "timeoutMs": int(timeout_ms),
                    "runtimeParams": runtime_params or {},
                },
                isolated_context=False,
            ), timeout=max(0.1, timeout_ms / 1000) + 0.5)
        except Exception as exc:
            logger.warning("[MENTION] resolve failed for @%s: %s", username, str(exc)[:120])
            return None  # transient: do not negative-cache page errors

        if not isinstance(result, dict):
            logger.warning("[MENTION] @%s returned non-dict JS result: %s", username, result)
            return None

        status = result.get("status")

        if status == "ok":
            user_data = result.get("user") or {}
            if _validated_user_info(username, user_data) is None:
                logger.warning("[MENTION] Invalid or mismatched identity for @%s; not cached", username)
                return None
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
                if order_cache is not None:
                    order_cache[key] = dict(info)
                await asyncio.to_thread(_save_disk_cache, _DISK_CACHE_PATH)
                return info

        elif status == "not_found":
            logger.warning(
                "[MENTION] @%s confirmed not found (%s); negative caching for %ds",
                username, result.get("reason"), _NEGATIVE_TTL_SECONDS
            )
            _cache_put(username, None)
            if order_cache is not None:
                order_cache[key] = None
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
    if len(mentions) > MAX_MENTIONS_PER_COMMENT:
        raise ValueError(f"Too many mentions: maximum {MAX_MENTIONS_PER_COMMENT}")
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
    for py_start, py_end, u in reversed(mentions):
        info = resolved.get(u.lower())
        if info and info.get("unique_id"):
            canonical = info["unique_id"]
            if canonical != u:
                normalized_text = normalized_text[:py_start] + "@" + canonical + normalized_text[py_end:]

    entries = build_text_extra(normalized_text, resolved)
    if not meta["unresolved"]:
        validate_text_extra(normalized_text, entries)
    meta["entries"] = len(entries)
    return normalized_text, json.dumps(entries, ensure_ascii=False, separators=(",", ":")), meta


def extract_response_mentions(response_data: Any) -> List[str]:
    """Return mentioned user ids that TikTok kept in the published comment."""
    if not isinstance(response_data, dict):
        return []
    nested = response_data.get("data")
    comment = response_data.get("comment") or (nested.get("comment") if isinstance(nested, dict) else None) or {}
    extras = comment.get("text_extra") if isinstance(comment, dict) else None
    ids: List[str] = []
    for item in extras if isinstance(extras, list) else []:
        if isinstance(item, dict) and str(item.get("type", 0)) == "0":
            uid = item.get("user_id") or item.get("uid")
            if uid:
                ids.append(str(uid))
    return ids


def mention_receipt(response_data, expected_uids):
    """Distinguish an omitted entity list from explicit loss of mention entities."""
    if not isinstance(response_data, dict):
        return {"confirmation": "unknown", "retained_uids": []}
    nested = response_data.get("data")
    comment = response_data.get("comment") or (nested.get("comment") if isinstance(nested, dict) else None)
    if not isinstance(comment, dict) or not isinstance(comment.get("text_extra"), list):
        return {"confirmation": "unknown", "retained_uids": []}
    retained = extract_response_mentions(response_data)
    missing = sorted(set(expected_uids) - set(retained))
    return {"confirmation": "partial" if retained and missing else "not_retained" if missing else "retained",
            "retained_uids": retained, "missing_uids": missing,
            "stripped_by_server": bool(missing and not retained)}
