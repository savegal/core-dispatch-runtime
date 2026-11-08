"""
Playwright HTTP Client - HTTP-клиент с транспортом через window.fetch страницы.

Использует живые cookies и headers из browser context для выполнения запросов к TikTok API.
Это более надежный подход, чем попытка генерировать подписи вручную.
"""
import threading
from executor.cache_lock import synchronized
import logging
import json
import asyncio
import time
from urllib.parse import parse_qs, urlparse
from types import SimpleNamespace
from typing import Optional, Dict, Any, Tuple
from datetime import datetime, timezone
from patchright.async_api import APIRequestContext, Page, BrowserContext
from executor.http.request_profile import RequestProfile
from executor.http.page_runtime import document_generation, page_document_key, prepared_runtime_profile, target_page_matches
from executor.http.execution_policy import valid_comment_id
from services.execution_attempts import track_publication_requests
from executor.http.signature_runtime import resolve_request_signature, split_explicit_signature_params, wait_for_runtime_signer_ready
from executor.http.request_evidence import observe_request_security, resolve_security_evidence, same_http_endpoint
from services.tiktok_urls import is_tiktok_shortlink, media_id_from_url
from services.cookie_validator import normalize_cookie_records
from executor.http.retry_policy import (
    RetryPolicy, HttpErrorType, classify_http_error,
    STANDARD_POLICY
)

LIKE_FAST_POLICY = RetryPolicy(max_retries=0, base_delay=0.5, max_delay=1.0)

logger = logging.getLogger("executor.http.playwright_client")
_PAGE_FETCH_JS = """
async ({url, method, headers, form_data, timeout_ms, referrer}) => {
    const controller = new AbortController();
    let timedOut = false;
    const timer = setTimeout(() => {
        timedOut = true;
        controller.abort();
    }, timeout_ms);
    try {
        const options = {
            method,
            headers,
            credentials: "include",
            redirect: "error",
            signal: controller.signal,
        };
        if (referrer) {
            try {
                const target = new URL(url, location.href);
                const source = new URL(referrer, location.href);
                if (target.origin === location.origin && source.origin === location.origin) {
                    source.hash = '';
                    options.referrer = source.href;
                }
            } catch (error) {}
        }
        if (method !== "GET" && method !== "HEAD") {
            options.body = new URLSearchParams(form_data).toString();
            options.headers = {...headers, "content-type": "application/x-www-form-urlencoded; charset=UTF-8"};
        }
        // Use the live page function so its interceptor sees the POST body.
        const response = await window.fetch(url, options);
        return {
            status: response.status,
            url: typeof response.url === "string" ? response.url : "",
            headers: Object.fromEntries(response.headers.entries()),
            text: await response.text(),
        };
    } catch (error) {
        if (timedOut) throw new Error("Request timeout during window.fetch");
        throw error;
    } finally {
        clearTimeout(timer);
    }
}
"""


def mask_cookies(cookies: Dict[str, str]) -> Dict[str, str]:
    """
    Маскирует чувствительные cookies для логирования.
    
    Args:
        cookies: Словарь cookies
    
    Returns:
        dict: Маскированные cookies
    """
    masked = {}
    sensitive_keys = ["sid_tt", "sessionid", "sid_guard", "tt_csrf_token"]
    
    for key, value in cookies.items():
        if any(sensitive in key.lower() for sensitive in sensitive_keys):
            if value:
                masked[key] = f"{value[:4]}...{value[-4:]}" if len(value) > 8 else "***"
            else:
                masked[key] = "***"
        else:
            masked[key] = value
    
    return masked


def _normalize_identifier(value: Any) -> Optional[str]:
    """Converts response identifier value to non-empty string."""
    if value is None:
        return None
    value_str = str(value).strip()
    return value_str or None


def _extract_comment_id(response_data: Optional[Dict[str, Any]]) -> Optional[str]:
    """
    Extracts comment ID from known TikTok response shapes.

    Supports:
    - {"comment": {"cid"/"comment_id"/"id": ...}}
    - {"data": {"comment": {...}}}
    - {"data": {"cid"/"comment_id": ...}}
    - {"cid"/"comment_id": ...}

    Generic envelope IDs can identify a request rather than a comment.
    """
    if not isinstance(response_data, dict):
        return None

    containers: list[Dict[str, Any]] = []

    comment_data = response_data.get("comment")
    if isinstance(comment_data, dict):
        containers.append(comment_data)

    data = response_data.get("data")
    if isinstance(data, dict):
        nested_comment = data.get("comment")
        if isinstance(nested_comment, dict):
            containers.append(nested_comment)
        containers.append(data)
    containers.append(response_data)

    for container in containers:
        keys = ("cid", "comment_id", "id") if container is comment_data or container is (data.get("comment") if isinstance(data, dict) else None) else ("cid", "comment_id")
        for key in keys:
            comment_id = _normalize_identifier(container.get(key))
            if valid_comment_id(comment_id):
                return comment_id

    return None


def _extract_comment_author(response_data: Optional[Dict[str, Any]]) -> Optional[str]:
    """Read the published comment's author from either supported API envelope."""
    if not isinstance(response_data, dict):
        return None
    data = response_data.get("data")
    for envelope in (response_data, data):
        if not isinstance(envelope, dict):
            continue
        comment = envelope.get("comment")
        user = comment.get("user") if isinstance(comment, dict) else None
        if not isinstance(user, dict):
            continue
        for name in ("unique_id", "username"):
            value = user.get(name)
            if isinstance(value, str) and value.strip():
                return value.strip()
    return None


def _extract_comment_user_id(response_data):
    if not isinstance(response_data, dict):
        return None
    for envelope in (response_data, response_data.get("data")):
        if not isinstance(envelope, dict):
            continue
        comment = envelope.get("comment")
        user = comment.get("user") if isinstance(comment, dict) else None
        if isinstance(user, dict):
            value = user.get("uid") or user.get("user_id_str") or user.get("user_id")
            if value is not None and str(value).strip():
                return str(value).strip()
    return None


def _inject_comment_id(
    response_data: Optional[Dict[str, Any]],
    comment_id: str,
    *,
    confirmation_source: str,
) -> Dict[str, Any]:
    """
    Enriches a successful publish response with recovered comment_id.

    We preserve the original response shape as much as possible and only add the
    minimum fields needed for the rest of the pipeline to treat the response as
    confirmed.
    """
    base: Dict[str, Any] = dict(response_data) if isinstance(response_data, dict) else {}
    comment_id = str(comment_id)

    data = base.get("data")
    if not isinstance(data, dict):
        data = {}
        base["data"] = data
    else:
        data = dict(data)
        base["data"] = data

    comment = data.get("comment")
    if not isinstance(comment, dict):
        comment = {}
    else:
        comment = dict(comment)

    comment.setdefault("cid", comment_id)
    data["comment"] = comment
    base.setdefault("comment", dict(comment))
    base["__confirmation_source"] = confirmation_source
    return base


async def _recover_main_comment_id_via_dom(page: Optional[Page], text: str) -> Optional[str]:
    """
    Best-effort recovery when TikTok accepted publish but omitted comment_id.

    We only use this for main comments on a live warmed page. Replies keep their
    stricter path because parent binding matters more than raw publish success.
    """
    if not page or not text or not text.strip():
        return None

    try:
        from executor.tiktok_client_async import _recover_comment_id_from_dom

        return await _recover_comment_id_from_dom(
            page=page,
            expected_text=text,
            phase="MAIN_COMMENT_HTTP",
            max_scrolls=3,
            scroll_delay_ms=250,
        )
    except Exception:
        logger.debug("[HTTP] Main comment DOM recovery helper failed", exc_info=True)
        return None


def _extract_reply_network_metadata(response_data: Optional[Dict[str, Any]]) -> Dict[str, Optional[str]]:
    """
    Extracts reply-parent metadata from known TikTok response shapes.

    We keep direct parent, reply-to-reply parent, and root parent separately so
    the caller can distinguish strong network confirmation from weaker hints.
    """
    metadata: Dict[str, Optional[str]] = {
        "direct_parent_id": None,
        "reply_to_reply_id": None,
        "root_comment_id": None,
    }
    if not isinstance(response_data, dict):
        return metadata

    containers: list[Dict[str, Any]] = []

    comment_data = response_data.get("comment")
    if isinstance(comment_data, dict):
        containers.append(comment_data)

    data = response_data.get("data")
    if isinstance(data, dict):
        nested_comment = data.get("comment")
        if isinstance(nested_comment, dict):
            containers.append(nested_comment)
        containers.append(data)
    containers.append(response_data)

    for container in containers:
        if metadata["direct_parent_id"] is None:
            for key in ("reply_id", "parent_comment_id", "parent_id", "reply_to_comment_id"):
                parent_id = _normalize_identifier(container.get(key))
                if parent_id and parent_id != "0":
                    metadata["direct_parent_id"] = parent_id
                    break

        if metadata["reply_to_reply_id"] is None:
            reply_to_reply_id = _normalize_identifier(container.get("reply_to_reply_id"))
            if reply_to_reply_id and reply_to_reply_id != "0":
                metadata["reply_to_reply_id"] = reply_to_reply_id

        if metadata["root_comment_id"] is None:
            for key in ("root_comment_id", "root_comment_cid"):
                root_comment_id = _normalize_identifier(container.get(key))
                if root_comment_id and root_comment_id != "0":
                    metadata["root_comment_id"] = root_comment_id
                    break

    return metadata


def _classify_api_response_error(response_data: Optional[Dict[str, Any]]) -> Tuple[HttpErrorType, str]:
    if not isinstance(response_data, dict):
        return HttpErrorType.API_ERROR, "Unknown API error"

    message_parts: list[str] = []
    for key in ("status_msg", "message", "msg", "error", "description"):
        value = response_data.get(key)
        if isinstance(value, str) and value.strip():
            message_parts.append(value.strip())

    prompts = response_data.get("prompts")
    if isinstance(prompts, list):
        for prompt in prompts:
            if isinstance(prompt, str) and prompt.strip():
                message_parts.append(prompt.strip())

    message = " | ".join(message_parts) or f"API error: status_code={response_data.get('status_code')}"
    message_lower = message.lower()

    if any(marker in message_lower for marker in ("captcha", "verify", "security check", "slide", "robot", "human")):
        return HttpErrorType.CAPTCHA, message
    if any(marker in message_lower for marker in ("too fast", "too many", "rate limit", "frequent", "try again later", "too frequently")):
        return HttpErrorType.RATE_LIMIT, message
    if any(marker in message_lower for marker in ("forbidden", "denied", "blocked", "suspended", "not authorized", "not authorised")):
        return HttpErrorType.FORBIDDEN, message

    return HttpErrorType.API_ERROR, message


_ACCOUNT_WEB_IDENTITY_CACHE: Dict[int, Dict[str, str]] = {}
_ACCOUNT_WEB_IDENTITY_SEEDS: Dict[int, str] = {}
_ACCOUNT_WEB_IDENTITY_LOCK = threading.RLock()
_ACCOUNT_WEB_IDENTITY_MAX_ENTRIES = 4096


def _get_account_web_identity(account_id):
    """Read/touch the bounded process cache; caller holds the identity lock."""
    entry = _ACCOUNT_WEB_IDENTITY_CACHE.pop(account_id, None)
    if entry is not None:
        _ACCOUNT_WEB_IDENTITY_CACHE[account_id] = entry
    return entry


def _store_account_web_identity(account_id, identity):
    _ACCOUNT_WEB_IDENTITY_CACHE.pop(account_id, None)
    _ACCOUNT_WEB_IDENTITY_CACHE[account_id] = dict(identity)
    while len(_ACCOUNT_WEB_IDENTITY_CACHE) > max(1, _ACCOUNT_WEB_IDENTITY_MAX_ENTRIES):
        oldest = next(iter(_ACCOUNT_WEB_IDENTITY_CACHE))
        _ACCOUNT_WEB_IDENTITY_CACHE.pop(oldest, None)
        _ACCOUNT_WEB_IDENTITY_SEEDS.pop(oldest, None)


def extract_web_identity_from_cookies(cookies: Any) -> Dict[str, str]:
    """Extract persisted (device_id, odin_id, web_id_last_time) metadata from Account.cookies."""
    if not cookies:
        return {}
    parsed: Dict[str, Any] = {}
    if isinstance(cookies, dict):
        parsed = cookies
    else:
        try:
            from services.cookie_validator import parse_cookies_to_dict
            parsed = parse_cookies_to_dict(str(cookies))
        except Exception:
            parsed = {}
    if not isinstance(parsed, dict):
        return {}
    out: Dict[str, str] = {}
    device_id = _normalize_identifier(parsed.get("device_id") or parsed.get("device-id"))
    odin_id = _normalize_identifier(
        parsed.get("odinId") or parsed.get("odin_id") or parsed.get("odin_tt")
    )
    wid_time = _normalize_identifier(
        parsed.get("WebIdLastTime") or parsed.get("web_id_last_time")
    )
    vfp = _normalize_identifier(
        parsed.get("s_v_web_id") or parsed.get("verifyFp") or parsed.get("verify_fp") or parsed.get("verifyFP")
    )
    if device_id:
        out["device_id"] = device_id
    if odin_id:
        out["odin_id"] = odin_id
    if wid_time:
        out["web_id_last_time"] = wid_time
    if vfp:
        out["verify_fp"] = vfp
        out["s_v_web_id"] = vfp
        out["verifyFp"] = vfp
    return out


@synchronized(_ACCOUNT_WEB_IDENTITY_LOCK)
def seed_web_identity_from_account(account: Any, context: Any = None) -> Dict[str, str]:
    """Restore persisted web identity from Account.cookies into in-memory cache and context."""
    if account is None:
        return {}
    from executor.device_profiles import _account_seed_key
    device_seed = _account_seed_key(account)
    account_id = getattr(account, "id", None)
    try:
        account_id = int(account_id) if account_id is not None else None
    except (TypeError, ValueError):
        account_id = None
    same_account = True
    if account_id is not None:
        same_account = _ACCOUNT_WEB_IDENTITY_SEEDS.get(int(account_id)) in (None, device_seed)
    if context is not None and getattr(context, "_account_web_identity_seed", None) not in (None, device_seed):
        same_account = False
    raw_cookies = getattr(account, "cookies", None)
    if not raw_cookies and getattr(account, "cookie_file", None):
        try:
            from services.cookie_validator import get_cookies_for_account
            raw_cookies = get_cookies_for_account(account)
        except Exception:
            raw_cookies = None
    seeded = extract_web_identity_from_cookies(raw_cookies)
    merged: Dict[str, str] = dict(seeded) if seeded else {}
    if not merged.get("device_id"):
        from executor.http.request_profile import generate_fallback_device_id
        merged["device_id"] = generate_fallback_device_id(device_seed)
        # A legacy creation time without its device ID cannot identify this
        # newly created fallback. Preserve observed runtime times separately.
        merged.pop("web_id_last_time", None)
    if "verify_fp" not in merged and "s_v_web_id" not in merged:
        try:
            from executor.device_profiles import _account_seed_key
            from executor.http.request_profile import generate_tiktok_verify_fp
            seed = _account_seed_key(account)
            vfp = generate_tiktok_verify_fp(seed)
            merged["verify_fp"] = vfp
            merged["s_v_web_id"] = vfp
            merged["verifyFp"] = vfp
        except Exception:
            pass
    if not merged:
        return {}
    if account_id is not None:
        try:
            acct_cached = _get_account_web_identity(int(account_id))
            if same_account and isinstance(acct_cached, dict):
                if (acct_cached.get("device_id") and merged.get("device_id")
                        and acct_cached["device_id"] != merged["device_id"]):
                    merged = {}
                for k, v in acct_cached.items():
                    if v:
                        merged[k] = str(v)
            _ACCOUNT_WEB_IDENTITY_SEEDS[int(account_id)] = device_seed
            _store_account_web_identity(int(account_id), merged)
        except (TypeError, ValueError):
            pass
    if context is not None:
        ctx_cached = getattr(context, "_account_web_identity", None)
        if same_account and isinstance(ctx_cached, dict):
            for k, v in ctx_cached.items():
                if v:
                    merged[k] = str(v)
        try:
            setattr(context, "_account_web_identity", dict(merged))
            setattr(context, "_account_web_identity_seed", device_seed)
        except Exception:
            pass
    return merged


@synchronized(_ACCOUNT_WEB_IDENTITY_LOCK)
def get_cached_web_identity(
    account_id: Optional[int],
    context: Any = None,
    account: Any = None,
) -> Dict[str, str]:
    """Return cached (device_id, odin_id, web_id_last_time) for an account or browser context."""
    if account is None and context is not None:
        account = getattr(context, "_pool_account_obj", None)
    if account is not None:
        acct_id = account_id if account_id is not None else getattr(account, "id", None)
        cached_entry = None
        if acct_id is not None:
            try:
                cached_entry = _get_account_web_identity(int(acct_id))
            except (TypeError, ValueError):
                cached_entry = None
        if not isinstance(cached_entry, dict) or not cached_entry:
            seed_web_identity_from_account(account, context=context)
        elif acct_id is not None:
            from executor.device_profiles import _account_seed_key
            expected_seed = _account_seed_key(account)
            if (_ACCOUNT_WEB_IDENTITY_SEEDS.get(int(acct_id)) not in (None, expected_seed)
                    or getattr(context, "_account_web_identity_seed", None) not in (None, expected_seed)):
                seed_web_identity_from_account(account, context=context)

    merged: Dict[str, str] = {}
    if account_id is not None:
        try:
            acct_cached = _get_account_web_identity(int(account_id))
            if isinstance(acct_cached, dict):
                merged.update({k: str(v) for k, v in acct_cached.items() if v})
        except (TypeError, ValueError):
            pass
    if context is not None:
        ctx_cached = getattr(context, "_account_web_identity", None)
        if isinstance(ctx_cached, dict):
            merged.update({k: str(v) for k, v in ctx_cached.items() if v})
    return merged


@synchronized(_ACCOUNT_WEB_IDENTITY_LOCK)
def cache_web_identity(
    account_id: Optional[int],
    identity: Optional[Dict[str, Any]],
    *,
    context: Any = None,
    account: Any = None,
) -> Dict[str, str]:
    """Persist (device_id, odin_id, web_id_last_time) on the context and per-account cache."""
    if account is None and context is not None:
        account = getattr(context, "_pool_account_obj", None)
    if account_id is None and account is not None:
        account_id = getattr(account, "id", None)
    existing = get_cached_web_identity(account_id, context, account=account)
    if not isinstance(identity, dict):
        return existing
    new_device_id = _normalize_identifier(identity.get("device_id"))
    new_odin_id = _normalize_identifier(
        identity.get("odin_id") or identity.get("odinId") or identity.get("odin_tt")
    )
    new_wid_time = _normalize_identifier(
        identity.get("web_id_last_time") or identity.get("WebIdLastTime")
    )
    new_vfp = _normalize_identifier(
        identity.get("verify_fp") or identity.get("verifyFp") or identity.get("s_v_web_id")
    )
    explicit_wid_time = bool(identity.get("_web_id_last_time_explicit"))

    device_changed = bool(
        new_device_id and existing.get("device_id") and new_device_id != existing.get("device_id")
    )
    updated = {} if device_changed else dict(existing)

    if new_device_id:
        updated["device_id"] = new_device_id
    if new_odin_id:
        updated["odin_id"] = new_odin_id
    if new_wid_time:
        if explicit_wid_time or device_changed or "web_id_last_time" not in updated:
            updated["web_id_last_time"] = new_wid_time
    if new_vfp:
        updated["verify_fp"] = new_vfp
        updated["s_v_web_id"] = new_vfp
        updated["verifyFp"] = new_vfp

    if context is not None and updated:
        try:
            setattr(context, "_account_web_identity", dict(updated))
        except Exception:
            pass
    if account_id is not None and updated:
        try:
            _store_account_web_identity(int(account_id), updated)
        except (TypeError, ValueError):
            pass
    return updated


async def _extract_runtime_profile(page: Page) -> Dict[str, Any]:
    generation = document_generation(page)
    current_url = None
    try:
        current_url = page.url
    except Exception:
        current_url = None

    try:
        profile = await page.evaluate(
            """() => {
                const uaData = navigator.userAgentData || null;
                const brands = uaData && Array.isArray(uaData.brands)
                    ? uaData.brands.map(item => ({ brand: item.brand, version: item.version }))
                    : [];
                const metaLocale = document.querySelector('meta[property="og:locale"]')?.getAttribute("content") || null;
                const documentLocale = document.documentElement?.lang || null;
                const locale = documentLocale || metaLocale || null;
                const localeRegion = locale && locale.includes("-")
                    ? locale.split("-").slice(-1)[0].toUpperCase()
                    : null;
                let nextRegion = null;
                let deviceId = null;
                let odinId = null;
                let webIdLastTime = null;
                let webIdLastTimeExplicit = false;
                let verifyFp = null;
                try {
                    try {
                        verifyFp = sessionStorage.getItem('s_v_web_id') || localStorage.getItem('s_v_web_id') || null;
                    } catch (error) {}
                    const initialSsr = window.__UNIVERSAL_DATA_FOR_REHYDRATION__;
                    let universal = initialSsr && typeof initialSsr === "object" && !initialSsr.nodeType ? initialSsr : null;
                    if (!universal) {
                        try {
                            const el = document.getElementById("__UNIVERSAL_DATA_FOR_REHYDRATION__");
                            if (el && el.textContent) {
                                universal = (() => {
                                    const raw = el.textContent;
                                    if (el.__teletanSsrRaw !== raw) {
                                        el.__teletanSsrValue = JSON.parse(raw);
                                        el.__teletanSsrRaw = raw;
                                    }
                                    return el.__teletanSsrValue;
                                })();
                            }
                        } catch (e) {}
                    }
                    if (!universal) {
                        try {
                            const sigi = document.getElementById("SIGI_STATE");
                            if (sigi && sigi.textContent) {
                                universal = (() => {
                                    const raw = sigi.textContent;
                                    if (sigi.__teletanSsrRaw !== raw) {
                                        sigi.__teletanSsrValue = JSON.parse(raw);
                                        sigi.__teletanSsrRaw = raw;
                                    }
                                    return sigi.__teletanSsrValue;
                                })();
                            }
                        } catch (e) {}
                    }
                    const scope = universal?.__DEFAULT_SCOPE__ || null;
                    const appCtx = scope?.["webapp.app-context"] || scope?.["webapp.a-context"] || null;
                    const nextData = window.__NEXT_DATA__ || null;
                    const pageProps = nextData?.props?.pageProps || null;
                    const nextAppCtx = pageProps?.appContext || pageProps?.webappContext || null;
                    nextRegion =
                        appCtx?.region ||
                        nextAppCtx?.region ||
                        pageProps?.region ||
                        null;
                    const rawWid = appCtx?.wid || appCtx?.webId || nextAppCtx?.wid || nextAppCtx?.webId || null;
                    if (rawWid && String(rawWid).trim()) {
                        deviceId = String(rawWid).trim();
                    }
                    const rawOdin = appCtx?.odinId || nextAppCtx?.odinId || null;
                    if (rawOdin && String(rawOdin).trim()) {
                        odinId = String(rawOdin).trim();
                    }
                    const rawWidTime = appCtx?.wid_ct || appCtx?.webIdCreatedTime || nextAppCtx?.wid_ct || nextAppCtx?.webIdCreatedTime || null;
                    if (rawWidTime && String(rawWidTime).trim()) {
                        webIdLastTime = String(rawWidTime).trim();
                        webIdLastTimeExplicit = true;
                    } else if (deviceId || odinId || appCtx || nextAppCtx) {
                        webIdLastTime = window.__tt_web_id_last_time || null;
                    }
                } catch (error) {
                    nextRegion = null;
                }
                const out = {
                    current_url: window.location.href,
                    root_referer: document.referrer || "",
                    user_agent: navigator.userAgent || null,
                    browser_language: navigator.language || null,
                    languages: Array.isArray(navigator.languages) ? navigator.languages : [],
                    locale,
                    region: nextRegion || localeRegion,
                    ssr_region: nextRegion ? String(nextRegion).trim().toUpperCase() : null,
                    timezone: Intl.DateTimeFormat().resolvedOptions().timeZone || null,
                    platform: navigator.platform || null,
                    sec_ch_ua_platform: uaData && uaData.platform ? uaData.platform : null,
                    sec_ch_ua_brands: brands,
                    mobile: !!(uaData && uaData.mobile),
                    screen_width: window.screen && window.screen.width ? window.screen.width : null,
                    screen_height: window.screen && window.screen.height ? window.screen.height : null,
                    viewport_width: window.innerWidth || null,
                    viewport_height: window.innerHeight || null,
                    history_length: window.history && typeof window.history.length === "number" ? window.history.length : null,
                    is_fullscreen: !!document.fullscreenElement,
                    is_page_visible: document.visibilityState === "visible",
                    focus_state: typeof document.hasFocus === "function" ? !!document.hasFocus() : null,
                    cookie_enabled: !!navigator.cookieEnabled,
                    browser_online: typeof navigator.onLine === "boolean" ? navigator.onLine : true
                };
                if (deviceId) out.device_id = deviceId;
                if (odinId) out.odin_id = odinId;
                if (webIdLastTime) out.web_id_last_time = webIdLastTime;
                if (webIdLastTimeExplicit) out._web_id_last_time_explicit = true;
                if (verifyFp) out.verify_fp = verifyFp;
                return out;
            }""",
            isolated_context=False,
        )
    except Exception:
        profile = {}

    return _complete_runtime_profile(page, profile, generation, current_url)


def runtime_profile_from_context(page):
    """Use the actual context settings when the renderer cannot answer."""
    context = getattr(page, "context", None)
    device_profile = getattr(context, "_account_device_profile", None)
    if not isinstance(device_profile, dict) or not (device_profile.get("options") or {}).get("user_agent"):
        return None
    profile = {key: value for key, value in {
        "browser_language": getattr(context, "_account_locale", None),
        "timezone": getattr(context, "_account_timezone", None),
    }.items() if isinstance(value, str) and value}
    return _complete_runtime_profile(page, profile, document_generation(page), page.url, context_fallback=True)


def _complete_runtime_profile(page, profile, generation, current_url, *, context_fallback=False):
    extracted = isinstance(profile, dict) and bool(profile)
    if not isinstance(profile, dict):
        profile = {}
    ssr_identity_observed = bool(profile.get("device_id"))
    context = getattr(page, "context", None)
    device_profile = getattr(context, "_account_device_profile", None) if context is not None else None
    if isinstance(device_profile, dict):
        hw = (device_profile.get("identity") or {}).get("hardware") or {}
        opts = device_profile.get("options") or {}
        vp = opts.get("viewport") or {}
        if not profile.get("screen_width") and hw.get("width"):
            profile["screen_width"] = int(hw["width"])
        if not profile.get("screen_height") and hw.get("height"):
            profile["screen_height"] = int(hw["height"])
        if not profile.get("viewport_width") and vp.get("width"):
            profile["viewport_width"] = int(vp["width"])
        if not profile.get("viewport_height") and vp.get("height"):
            profile["viewport_height"] = int(vp["height"])
        if not profile.get("user_agent") and opts.get("user_agent"):
            profile["user_agent"] = str(opts["user_agent"])
        if not profile.get("platform") and (device_profile.get("identity") or {}).get("platform"):
            profile["platform"] = str(device_profile["identity"]["platform"])
    if context is not None and not profile.get("browser_language"):
        ctx_locale = getattr(context, "_account_locale", None)
        if ctx_locale:
            profile["browser_language"] = str(ctx_locale)
    account_obj = getattr(page, "_pool_account_obj", None) or getattr(context, "_pool_account_obj", None)
    account_id = (
        getattr(page, "_pool_account_id", None)
        or getattr(context, "_pool_account_id", None)
        or getattr(account_obj, "id", None)
    )
    cached_identity = cache_web_identity(account_id, profile, context=context, account=account_obj)
    if not (cached_identity.get("verify_fp") or cached_identity.get("verifyFp") or cached_identity.get("s_v_web_id")):
        # Anonymous/non-pool pages need one context-owned fallback too. Keep
        # this state at extraction, rather than mutating query builder inputs.
        from executor.http.request_profile import generate_tiktok_verify_fp
        from executor.device_profiles import _account_seed_key
        seed = _account_seed_key(account_obj) if account_obj is not None else None
        cached_identity = cache_web_identity(account_id, {"verify_fp": generate_tiktok_verify_fp(seed)},
                                             context=context, account=account_obj)
    # Extraction is read/cache-only. Warmup and pool disposal persist identity;
    # no synchronous database/file commit belongs on every page metadata read.
    profile.pop("_web_id_last_time_explicit", None)
    for key in ("device_id", "odin_id", "web_id_last_time", "verify_fp", "verifyFp", "s_v_web_id"):
        if cached_identity.get(key):
            profile[key] = cached_identity[key]
    if account_id is not None:
        profile["account_id"] = account_id
    if account_obj is not None:
        from executor.device_profiles import _account_seed_key
        profile["device_seed"] = _account_seed_key(account_obj)
    ssr_region = str(profile.get("ssr_region") or "").strip().upper()
    if len(ssr_region) == 2 and ssr_region.isalpha() and ssr_region.isascii():
        profile["ssr_region"] = ssr_region
        if context is not None:
            try:
                setattr(context, "_account_ssr_region", ssr_region)
            except Exception:
                pass
        if account_obj is not None:
            # Keep verified page SSR evidence available to the country resolver.
            # The extraction is read-only for the ORM; persistence is handled
            # by the normal account service rather than a browser callback.
            try:
                setattr(account_obj, "_account_ssr_region", ssr_region)
            except (AttributeError, TypeError):
                pass
    else:
        profile.pop("ssr_region", None)
    proxy_in_use = getattr(context, "_account_proxy_in_use", None)
    if isinstance(proxy_in_use, bool):
        profile["proxy_in_use"] = proxy_in_use
        profile["context_geo_bound"] = True
    route = getattr(context, "_account_proxy_route", None)
    if route is not None:
        profile["proxy_country_code"] = route.country_code
    account = getattr(context, "_pool_account_obj", None)
    if account is not None:
        profile["account_country_code"] = getattr(account, "country_code", None)
    if current_url and not profile.get("current_url"):
        profile["current_url"] = current_url
    # Explicit, bounded handoff: ordinary extractions still inspect the current
    # document, including late hydration at the same URL.
    if (extracted or context_fallback) and current_url and profile.get("current_url") == current_url and page.url == current_url:
        profile["_document_generation"] = generation
        profile["_captured_at"] = time.monotonic()
        profile["_snapshot_reusable"] = ssr_identity_observed or context_fallback
    return profile


from services.publication_journal import observe_publication_request


class PlaywrightHttpClient:
    """
    HTTP-клиент для TikTok API на основе Playwright APIRequestContext.
    
    Использует живые cookies и headers из browser context.
    """
    
    def __init__(
        self,
        request_context: APIRequestContext,
        retry_policy: Optional[RetryPolicy] = None,
        runtime_profile: Optional[Dict[str, Any]] = None,
        page: Optional[Page] = None,
    ):
        """
        Args:
            request_context: Playwright APIRequestContext (с живыми cookies)
            retry_policy: Политика повторных попыток (по умолчанию STANDARD_POLICY)
        """
        self.request_context = request_context
        self.retry_policy = retry_policy or STANDARD_POLICY
        self.runtime_profile = runtime_profile or {}
        self.page = page
        self.last_request_meta: Dict[str, Any] = {}

    @staticmethod
    def _canonical_referer_url(video_url: Optional[str], runtime_profile: Optional[Dict[str, Any]]) -> Optional[str]:
        if not video_url:
            return None
        if is_tiktok_shortlink(video_url):
            current_url = str((runtime_profile or {}).get("current_url") or "").strip()
            if media_id_from_url(current_url):
                return current_url
        return video_url

    def _runtime_profile_for_request(self, proxy_country_code: Optional[str] = None) -> Dict[str, Any]:
        runtime_profile = dict(self.runtime_profile or {})
        if proxy_country_code:
            runtime_profile["proxy_country_code"] = str(proxy_country_code).upper()
        return runtime_profile

    @staticmethod
    def _page_url_hint(page: Optional[Page]) -> Optional[str]:
        if page is None:
            return None
        try:
            current_url = str(page.url or "").strip()
        except Exception:
            return None
        return current_url or None

    @staticmethod
    def _should_abort_local_retries(error_type: Optional[HttpErrorType], error_msg: Optional[str]) -> bool:
        if error_type == HttpErrorType.TIMEOUT:
            return True
        if error_type != HttpErrorType.NETWORK_ERROR:
            return False

        haystack = (error_msg or "").lower()
        proxy_markers = (
            "proxy",
            "tunnel",
            "socks",
            "timed out",
            "timeout",
            "connection refused",
            "connect call failed",
            "network is unreachable",
            "connection reset",
            "tls handshake",
            "winerror 10060",
            "winerror 10061",
            "err_proxy_connection_failed",
            "err_timed_out",
            "err_name_not_resolved",
            "err_connection_reset",
            "err_network_changed",
        )
        if any(marker in haystack for marker in proxy_markers):
            return True

        suspicious_response_markers = (
            "empty json response",
            "response missing status_code field (empty body)",
            "response missing status_code field (empty json)",
            "response missing status_code field (redirected page state)",
        )
        return any(marker in haystack for marker in suspicious_response_markers)

    @classmethod
    async def from_page(cls, page: Page, retry_policy: Optional[RetryPolicy] = None, *,
                        runtime_profile: Optional[Dict[str, Any]] = None) -> "PlaywrightHttpClient":
        """
        Создает клиент из Playwright Page.
        
        Args:
            page: Playwright Page (из которой будут извлечены cookies)
            retry_policy: Политика повторных попыток
        
        Returns:
            PlaywrightHttpClient: Экземпляр клиента
        """
        # Получаем request context из page context
        context = page.context
        request_context = context.request
        runtime_profile = prepared_runtime_profile(page, runtime_profile) or await _extract_runtime_profile(page)
        return cls(request_context, retry_policy, runtime_profile=runtime_profile, page=page)
    
    @classmethod
    async def from_context(
        cls,
        browser_context: BrowserContext,
        retry_policy: Optional[RetryPolicy] = None,
        page: Optional[Page] = None,
    ) -> "PlaywrightHttpClient":
        """
        Создает клиент из Playwright BrowserContext.
        
        Args:
            browser_context: Playwright BrowserContext
            retry_policy: Политика повторных попыток
        
        Returns:
            PlaywrightHttpClient: Экземпляр клиента
        """
        request_context = browser_context.request
        if page is not None:
            runtime_profile = await _extract_runtime_profile(page)
        else:
            dummy_page = SimpleNamespace(
                url=None,
                context=browser_context,
                _pool_account_id=getattr(browser_context, "_pool_account_id", None),
                _pool_account_obj=getattr(browser_context, "_pool_account_obj", None),
                evaluate=None,
            )
            runtime_profile = await _extract_runtime_profile(dummy_page)
        return cls(request_context, retry_policy, runtime_profile=runtime_profile, page=page)
    
    @observe_publication_request
    async def _execute_request(
        self,
        profile: RequestProfile,
        dynamic_params: Dict[str, Any],
        cookies: Optional[Dict[str, str]] = None,
        user_agent: Optional[str] = None,
        correlation_id: Optional[str] = None,
        runtime_profile_override: Optional[Dict[str, Any]] = None,
        retry_policy_override: Optional[RetryPolicy] = None,
    ) -> Tuple[bool, Optional[Dict[str, Any]], Optional[str], Optional[HttpErrorType]]:
        """
        Выполняет запрос через window.fetch страницы, либо APIRequestContext без page.
        
        Args:
            profile: RequestProfile с шаблоном запроса
            dynamic_params: Динамические параметры (aweme_id, cid, text и т.д.)
            cookies: Дополнительные cookies (если не указаны, используются из context)
            user_agent: User-Agent (если не указан, используется из context)
            correlation_id: ID для логирования (task_id, account_id и т.д.)
            retry_policy_override: Опциональное переопределение политики повторов
        
        Returns:
            tuple: (success, response_data, error_message, error_type)
        """
        prepared_mentions = (self.last_request_meta.get("mentions")
                             if urlparse(profile.endpoint).path.rstrip("/") == "/api/comment/publish" else None)
        self.last_request_meta = {
            "endpoint": profile.endpoint,
            "method": profile.method,
            "request_attempted": False,
            "attempt_count": 0,
            "transport": "window.fetch" if self.page is not None else "request_context.fetch",
        }
        if prepared_mentions is not None:
            self.last_request_meta["mentions"] = prepared_mentions
        is_publish = profile.method.upper() == "POST" and profile.endpoint.rstrip("/") == "/api/comment/publish"
        if is_publish and self.page is None:
            self.last_request_meta["publication_status"] = "not_sent"
            return False, None, "publish_requires_live_page", HttpErrorType.API_ERROR
        if is_publish:
            from executor.http.comment_discovery import comment_token_handshake_ready
            handshake = (getattr(self, "allow_comment_token_handshake", False)
                         and comment_token_handshake_ready(self.page, dynamic_params.get("aweme_id")))
            if not handshake and not await wait_for_runtime_signer_ready(
                    self.page, timeout_ms=1200, require_signed_dispatch=True):
                self.last_request_meta["publication_status"] = "not_sent"
                return False, None, "runtime_signer_not_ready", HttpErrorType.API_ERROR
        if is_publish and isinstance(dynamic_params.get("text"), str):
            self.last_request_meta["published_text"] = dynamic_params["text"]

        def unconfirmed(reason):
            self.last_request_meta["publication_status"] = "submitted_unconfirmed"
            self.last_request_meta["unconfirmed_reason"] = reason
            return False, None, f"submitted_unconfirmed:{reason}", HttpErrorType.SUBMITTED_UNCONFIRMED

        def rejected(error_type, reason):
            self.last_request_meta["publication_status"] = "rejected"
            return False, None, reason, error_type
        effective_runtime_profile = runtime_profile_override or self.runtime_profile
        effective_retry_policy = retry_policy_override or self.retry_policy
        base_dynamic_params = dict(dynamic_params)
        request_signature_meta = {}
        recovery_uid = getattr(self, "empty_post_recovery_uid", None) if is_publish else None
        parent_id = str(base_dynamic_params.get("reply_id") or "0")

        def _attach_request_meta(payload: Optional[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
            if not isinstance(payload, dict):
                return payload
            enriched = dict(payload)
            enriched["__request_signature"] = dict(request_signature_meta)
            enriched["__request_meta"] = dict(self.last_request_meta)
            return enriched
        
        for attempt in range(effective_retry_policy.max_retries + 1):
            try:
                expected_aweme_id = base_dynamic_params.get("aweme_id")
                if self.page is not None and expected_aweme_id:
                    if not target_page_matches(self.page, expected_aweme_id):
                        return False, None, "target_document_changed", HttpErrorType.API_ERROR
                    expected_generation = document_generation(self.page)
                    if (effective_runtime_profile.get("_snapshot_reusable") is True
                            and prepared_runtime_profile(self.page, effective_runtime_profile) is None):
                        effective_runtime_profile = await _extract_runtime_profile(self.page)
                attempt_cookies = dict(cookies or {})
                if self.page is not None:
                    live_cookies = normalize_cookie_records(await self.page.context.cookies())
                    attempt_cookies.update(live_cookies)
                rotated_ms_token = getattr(self, "_last_response_ms_token", None)
                token_baseline = getattr(self, "_last_response_ms_token_cookie_baseline", None)
                if rotated_ms_token and attempt_cookies.get("msToken") in {None, token_baseline, rotated_ms_token}:
                    attempt_cookies["msToken"] = rotated_ms_token
                clean_params, _ = split_explicit_signature_params(base_dynamic_params)
                request_parts = profile.build_request_parts(
                    clean_params, attempt_cookies, runtime_profile=effective_runtime_profile,
                    include_cookie_signatures=False,
                )
                _, form_data = request_parts
                attempt_params, url, signature_bundle = await resolve_request_signature(
                    page=self.page,
                    profile=profile,
                    dynamic_params=dict(base_dynamic_params),
                    cookies=attempt_cookies,
                    runtime_profile=effective_runtime_profile,
                    allow_cookie_fallback=self.page is None,
                    request_parts=request_parts,
                )
                headers = (profile.build_browser_headers(attempt_cookies) if self.page is not None else
                           profile.build_headers(attempt_cookies, user_agent, runtime_profile=effective_runtime_profile))
                request_signature_meta = {
                    "mode": signature_bundle.mode,
                    "source": signature_bundle.source,
                    "has_x_bogus": bool(signature_bundle.x_bogus),
                    "has_x_gnarly": bool(signature_bundle.x_gnarly),
                    "has_a_bogus": bool(signature_bundle.a_bogus),
                    "has_form_body": bool(form_data),
                    "signer_available": signature_bundle.signer_available,
                    "notes": signature_bundle.notes,
                }
                self.last_request_meta["signature"] = dict(request_signature_meta)
                logger.debug(
                    "[HTTP] %s %s | correlation_id=%s attempt=%s dynamic_params=%s form_keys=%s "
                    "cookies_keys=%s signature_mode=%s signature_source=%s",
                    profile.method, profile.endpoint, correlation_id, attempt + 1,
                    list(attempt_params), list(form_data), list(mask_cookies(attempt_cookies)),
                    signature_bundle.mode, signature_bundle.source,
                )
                self.last_request_meta["attempt_count"] = int(
                    self.last_request_meta.get("attempt_count") or 0
                ) + 1
                # ИСПРАВЛЕНО: Разные timeout для разных попыток и эндпоинтов
                # 5 сек для лайков (быстрый non-blocking вызов), 15 сек для обычных, 30 сек для retry
                is_like = profile.endpoint == "/api/comment/digg/"
                base_timeout = 5000 if is_like else 15000
                retry_timeout = 8000 if is_like else 30000
                timeout_to_use = base_timeout if attempt == 0 else retry_timeout
                
                self.last_request_meta["timeout_ms"] = timeout_to_use
                if self.page is not None:
                    if expected_aweme_id and (
                        not target_page_matches(self.page, expected_aweme_id)
                        or document_generation(self.page) != expected_generation
                    ):
                        return False, None, "target_document_changed", HttpErrorType.API_ERROR
                    if is_publish and handshake and not comment_token_handshake_ready(self.page, expected_aweme_id):
                        return False, None, "runtime_signer_not_ready", HttpErrorType.API_ERROR
                    # Browser-managed headers and cookies come from the live page.
                    browser_headers = {
                        key.lower(): value for key, value in headers.items()
                        if key.lower() in {"accept", "tt-csrf-token", "content-type"}
                    }
                    if profile.method.upper() not in {"GET", "HEAD"}:
                        browser_headers["content-type"] = "application/x-www-form-urlencoded; charset=UTF-8"
                    detach = track_publication_requests(
                        self.page, self.last_request_meta, flag="request_attempted", expected_url=url,
                    )
                    try:
                        with observe_request_security(self.page, url, profile.method) as wire_evidence:
                            self.last_request_meta["transport_invoked"] = True
                            if recovery_uid:
                                from executor.http.comment_discovery import signed_first_comment_page
                                recovery_document = page_document_key(self.page)
                                previous = (signed_first_comment_page(self.page, expected_aweme_id)
                                            if parent_id == "0" else None)
                                dispatched_at = time.time()
                            fetch_started = time.monotonic()
                            browser_response = await self.page.evaluate(
                                _PAGE_FETCH_JS,
                                {
                                    "url": url,
                                    "method": profile.method.upper(),
                                    "headers": browser_headers,
                                    "form_data": form_data,
                                    "timeout_ms": timeout_to_use,
                                    "referrer": attempt_params.get("referer") or headers.get("referer"),
                                },
                                isolated_context=False,
                            )
                            self.last_request_meta["transport_ms"] = round((time.monotonic() - fetch_started) * 1000)
                        # A returned HTTP response also proves a dispatch for
                        # minimal page adapters without request event support.
                        self.last_request_meta["request_attempted"] = True
                    finally:
                        detach()
                    response_headers = browser_response["headers"]
                    status_code = browser_response["status"]
                    response_url = browser_response.get("url")
                    security = resolve_security_evidence(wire_evidence, response_url)
                    self.last_request_meta.update(security)
                    self.last_request_meta["observed_security_params"] = security["security_params"]
                else:
                    self.last_request_meta["request_attempted"] = True
                    if is_publish:
                        from services.publication_journal import note_publication_dispatch
                        note_publication_dispatch()
                    response = await self.request_context.fetch(
                        url,
                        method=profile.method,
                        headers=headers,
                        form=form_data or None,
                        timeout=timeout_to_use,
                        max_redirects=0,
                    )
                    response_headers = response.headers
                    status_code = response.status
                    response_url = getattr(response, "url", None)
                if response_url is not None and not same_http_endpoint(response_url, url):
                    if is_publish:
                        return unconfirmed("unexpected_response_url")
                    return False, None, "unexpected_response_url", HttpErrorType.API_ERROR
                
                # ИСПРАВЛЕНО: Проверяем Content-Type ответа перед парсингом
                # Если TikTok вернул HTML (капча/WAF/редирект) - сразу классифицируем как FORBIDDEN/CAPTCHA
                if isinstance(response_headers, dict):
                    for hdr_k, hdr_v in response_headers.items():
                        if str(hdr_k or "").lower() == "x-ms-token" and str(hdr_v or "").strip():
                            self._last_response_ms_token = str(hdr_v).strip()
                            self._last_response_ms_token_cookie_baseline = attempt_cookies.get("msToken")
                            if self.page is not None:
                                try:
                                    records = await self.page.context.cookies()
                                    updates = [{**record, "value": self._last_response_ms_token} for record in records
                                               if record.get("name") == "msToken"
                                               and str(record.get("domain") or "").lstrip(".") in {"tiktok.com", "www.tiktok.com"}]
                                    if not updates:
                                        updates = [{"name": "msToken", "value": self._last_response_ms_token,
                                                    "domain": ".tiktok.com", "path": "/", "secure": True}]
                                    await self.page.context.add_cookies(updates)
                                    from services.cookie_validator import remember_observed_ms_token
                                    remember_observed_ms_token(self.page.context, {"msToken": self._last_response_ms_token})
                                except Exception:
                                    logger.debug("Could not persist response msToken in the active browser context", exc_info=True)
                            break
                content_type = response_headers.get("content-type", "").lower()
                self.last_request_meta["response_status"] = status_code
                self.last_request_meta["response_content_type"] = content_type
                
                # Получаем тело ответа
                try:
                    response_body = browser_response["text"] if self.page is not None else await response.text()
                    self.last_request_meta["response_body_len"] = len(response_body or "")
                    logger.info(
                        "[HTTP_RESPONSE] correlation_id=%s transport=%s attempt=%s status=%s body_len=%s security_params=%s security_params_source=%s transport_ms=%s",
                        correlation_id, self.last_request_meta["transport"], attempt + 1, status_code,
                        len(response_body or ""), self.last_request_meta.get("observed_security_params"),
                        self.last_request_meta.get("security_params_source", "unavailable"),
                        self.last_request_meta.get("transport_ms"),
                    )
                    
                    if is_publish and status_code == 200 and not (response_body or "").strip():
                        if recovery_uid:
                            from executor.http.comment_discovery import recover_empty_publication
                            recovered = await recover_empty_publication(
                                self.page, expected_aweme_id, parent_id=parent_id, author_uid=recovery_uid,
                                text=form_data.get("text"), started_at=dispatched_at,
                                document=recovery_document, previous=previous,
                            )
                            if recovered is not None:
                                payload = {"status_code": 0, "comment": recovered}
                                self.last_request_meta.update(receipt_source="author_list_recovery",
                                    comment_author=_extract_comment_author(payload), comment_author_uid=recovery_uid)
                                logger.info("[HTTP] CID recovered after empty POST | correlation_id=%s", correlation_id)
                                return True, _attach_request_meta(payload), None, None
                        return unconfirmed("empty_body")
                    if is_publish and 300 <= status_code < 400:
                        return unconfirmed("upstream_redirect")
                    if is_publish and 500 <= status_code < 600:
                        return unconfirmed("upstream_5xx")
                    if is_publish and status_code in {401, 403, 429}:
                        try:
                            error_payload = json.loads(response_body or "{}")
                        except (TypeError, ValueError):
                            error_payload = None
                        if _extract_comment_id(error_payload):
                            return unconfirmed("contradictory_receipt")
                        return rejected(*classify_http_error(status_code))

                    # ИСПРАВЛЕНО: Если Content-Type не JSON - это капча/WAF/редирект
                    if "application/json" not in content_type and status_code == 200:
                        # HTML ответ при 200 - это обычно капча или WAF
                        if "text/html" in content_type or not content_type:
                            error_type = HttpErrorType.CAPTCHA
                            error_msg = f"Received HTML instead of JSON (Content-Type: {content_type})"
                            logger.warning(
                                f"[HTTP] Non-JSON response | "
                                f"correlation_id={correlation_id}, "
                                f"content_type={content_type}, "
                                f"status={status_code}"
                            )
                            if self._should_abort_local_retries(error_type, error_msg):
                                return False, None, error_msg, error_type
                            if not effective_retry_policy.should_retry(error_type, attempt):
                                return False, None, error_msg, error_type
                            continue
                    
                    # Парсим JSON только если Content-Type правильный
                    if "application/json" in content_type or status_code != 200:
                        response_data = json.loads(response_body) if response_body else {}
                    else:
                        # Не JSON и не 200 - это ошибка
                        response_data = {"raw": response_body[:500] if response_body else ""}
                except (json.JSONDecodeError, Exception) as parse_error:
                    if is_publish:
                        return unconfirmed("invalid_json")
                    # ИСПРАВЛЕНО: Если не удалось распарсить JSON - проверяем Content-Type
                    if "application/json" in content_type:
                        # Ожидали JSON, но не смогли распарсить - это ошибка
                        error_type = HttpErrorType.API_ERROR
                        error_msg = f"Failed to parse JSON response: {str(parse_error)[:100]}"
                        logger.warning(
                            f"[HTTP] JSON parse error | "
                            f"correlation_id={correlation_id}, "
                            f"content_type={content_type}, "
                            f"error={error_msg}"
                        )
                        if self._should_abort_local_retries(error_type, error_msg):
                            return False, None, error_msg, error_type
                        if not effective_retry_policy.should_retry(error_type, attempt):
                            return False, None, error_msg, error_type
                        continue
                    else:
                        # Не JSON ответ - это капча/WAF
                        response_data = {"raw": response_body[:500] if response_body else ""}
                        error_type = HttpErrorType.CAPTCHA
                        error_msg = f"Non-JSON response (Content-Type: {content_type})"
                        logger.warning(
                            f"[HTTP] Non-JSON response | "
                            f"correlation_id={correlation_id}, "
                            f"content_type={content_type}"
                        )
                        if self._should_abort_local_retries(error_type, error_msg):
                            return False, None, error_msg, error_type
                        if not effective_retry_policy.should_retry(error_type, attempt):
                            return False, None, error_msg, error_type
                        continue
                
                # Классифицируем ошибку
                error_type, error_msg = classify_http_error(status_code)
                
                # ИСПРАВЛЕНО: Жесткая проверка ответа HTTP
                # Успех только если: HTTP статус 200 И JSON status_code == 0
                if status_code == 200 and isinstance(response_data, dict):
                    status_code_in_body = response_data.get("status_code")
                    
                    # КРИТИЧНО: Проверяем status_code в теле ответа
                    if status_code_in_body is None:
                        if is_publish:
                            # Even {} is not evidence that a dispatched POST
                            # failed before being stored. Never blindly repeat it.
                            return unconfirmed("missing_api_status")
                        # Нет status_code в ответе - считаем ошибкой
                        current_page_url = self._page_url_hint(self.page)
                        response_keys = list(response_data.keys())[:10]
                        body_preview = (response_body[:160] if response_body else "").replace("\n", " ").replace("\r", " ")
                        page_redirect_markers = ("/messages", "/login", "/signup", "/verify", "/auth", "/inbox")
                        redirected_page_state = bool(
                            current_page_url and any(marker in current_page_url.lower() for marker in page_redirect_markers)
                        )
                        logger.warning(
                            f"[HTTP] Missing status_code in response | "
                            f"correlation_id={correlation_id}, "
                            f"response_keys={response_keys}, "
                            f"content_type={content_type}, "
                            f"body_len={len(response_body) if response_body else 0}, "
                            f"page_url={current_page_url}, "
                            f"body_preview={body_preview!r}"
                        )
                        if not response_body.strip() or not response_keys:
                            error_type = HttpErrorType.FORBIDDEN
                            error_msg = (
                                "TikTok stealth-reject: empty JSON response "
                                f"(body_len={len(response_body) if response_body else 0}, "
                                f"content_type={content_type}). "
                                "Publication was not confirmed."
                            )
                            logger.warning(
                                f"[HTTP] Stealth-reject detected | "
                                f"correlation_id={correlation_id}, "
                                f"page_url={current_page_url}, "
                                f"redirected_page_state={redirected_page_state}"
                            )
                            return False, None, error_msg, error_type
                        if redirected_page_state:
                            error_type = HttpErrorType.FORBIDDEN
                            error_msg = "Response missing status_code field (redirected page state)"
                        else:
                            error_type = HttpErrorType.API_ERROR
                            error_msg = "Response missing status_code field"
                        if self._should_abort_local_retries(error_type, error_msg):
                            return False, None, error_msg, error_type
                        if not effective_retry_policy.should_retry(error_type, attempt):
                            return False, None, error_msg, error_type
                        continue
                    elif type(status_code_in_body) is not int or status_code_in_body != 0:
                        if is_publish and type(status_code_in_body) is not int:
                            return unconfirmed("invalid_api_status")
                        error_type, error_msg = _classify_api_response_error(response_data)
                        if is_publish:
                            if _extract_comment_id(response_data):
                                return unconfirmed("contradictory_receipt")
                            return rejected(error_type, error_msg)
                        logger.warning(
                            f"[HTTP] API error in response | "
                            f"correlation_id={correlation_id}, "
                            f"status_code={status_code_in_body}, "
                            f"error_type={error_type.value}, "
                            f"msg={error_msg}"
                        )
                        
                        # Check whether retry is allowed for this classified API error.
                        if self._should_abort_local_retries(error_type, error_msg):
                            return False, None, error_msg, error_type
                        if not effective_retry_policy.should_retry(error_type, attempt):
                            return False, None, error_msg, error_type
                        continue
                    
                    # status_code == 0, проверяем наличие подтверждающих данных
                    # Для like: проверяем что ответ не пустой или есть подтверждение
                    # Для reply/publish: желательно есть comment_id
                    has_confirmation = False
                    if profile.endpoint.rstrip("/") == "/api/comment/digg":
                        # Для лайка: если status_code==0, это уже подтверждение
                        has_confirmation = True
                    elif is_publish:
                        # For publish/reply success we require real comment_id.
                        has_confirmation = _extract_comment_id(response_data) is not None
                    else:
                        has_confirmation = True
                    
                    if has_confirmation:
                        if is_publish:
                            self.last_request_meta["comment_author"] = _extract_comment_author(response_data)
                            self.last_request_meta["comment_author_uid"] = _extract_comment_user_id(response_data)
                        response_data = _attach_request_meta(response_data)
                        logger.info(
                            f"[HTTP] Success (API receipt) | "
                            f"correlation_id={correlation_id}, "
                            f"method={profile.method}, "
                            f"endpoint={profile.endpoint}, "
                            f"status_code={status_code_in_body}, "
                            f"signature_mode={signature_bundle.mode}"
                        )
                        return True, response_data, None, None
                    else:
                        # status_code==0, но нет подтверждающих данных - подозрительно
                        if is_publish:
                            return unconfirmed("missing_comment_id")
                        error_type = HttpErrorType.API_ERROR
                        error_msg = "Response OK but missing confirmation data (comment_id)"
                        logger.warning(
                            f"[HTTP] Missing confirmation in response | "
                            f"correlation_id={correlation_id}, "
                            f"response_keys={list(response_data.keys())[:10]}, "
                            f"signature_mode={signature_bundle.mode}"
                        )
                        if self._should_abort_local_retries(error_type, error_msg):
                            return False, None, error_msg, error_type
                        if not effective_retry_policy.should_retry(error_type, attempt):
                            return False, None, error_msg, error_type
                        continue
                elif error_type == HttpErrorType.SUCCESS and status_code == 200:
                    if is_publish:
                        return unconfirmed("invalid_response_format")
                    # HTTP 200, но response_data не dict - ошибка парсинга
                    error_type = HttpErrorType.API_ERROR
                    error_msg = "Invalid response format"
                    logger.warning(
                        f"[HTTP] Invalid response format | "
                        f"correlation_id={correlation_id}, "
                        f"response_type={type(response_data).__name__}"
                    )
                    if self._should_abort_local_retries(error_type, error_msg):
                        return False, None, error_msg, error_type
                    if not effective_retry_policy.should_retry(error_type, attempt):
                        return False, None, error_msg, error_type
                    continue
                else:
                    logger.warning(
                        f"[HTTP] Error | "
                        f"correlation_id={correlation_id}, "
                        f"status={status_code}, "
                        f"error_type={error_type.value}, "
                        f"attempt={attempt+1}/{effective_retry_policy.max_retries+1}"
                    )
                    
                    # Проверяем, нужно ли повторять
                    if self._should_abort_local_retries(error_type, error_msg):
                        return False, None, error_msg, error_type
                    if not effective_retry_policy.should_retry(error_type, attempt):
                        return False, None, error_msg, error_type
                    
                    # ИСПРАВЛЕНО: Задержка перед повтором с экспоненциальным backoff+jitter
                    if attempt < effective_retry_policy.max_retries:
                        delay = effective_retry_policy.get_delay(attempt, error_type)
                        logger.debug(
                            f"[HTTP] Retry after {delay:.2f}s | "
                            f"correlation_id={correlation_id}, "
                            f"attempt={attempt+1}/{effective_retry_policy.max_retries+1}, "
                            f"error_type={error_type.value}"
                        )
                        await asyncio.sleep(delay)
                    
            except Exception as e:
                if is_publish and self.last_request_meta.get("request_attempted"):
                    return unconfirmed("transport_after_dispatch")
                error_type, classified_error_msg = classify_http_error(None, e)
                error_msg = classified_error_msg[:200]
                
                logger.error(
                    f"[HTTP] Exception | "
                    f"correlation_id={correlation_id}, "
                    f"error={error_msg}, "
                    f"attempt={attempt+1}/{effective_retry_policy.max_retries+1}",
                    exc_info=True
                )
                
                # Проверяем, нужно ли повторять
                if self._should_abort_local_retries(error_type, error_msg):
                    return False, None, error_msg, error_type
                if not effective_retry_policy.should_retry(error_type, attempt):
                    return False, None, error_msg, error_type
                
                # ИСПРАВЛЕНО: Задержка перед повтором с error_type для корректного backoff
                if attempt < effective_retry_policy.max_retries:
                    delay = effective_retry_policy.get_delay(attempt, error_type)
                    logger.debug(
                        f"[HTTP] Retry after exception {delay:.2f}s | "
                        f"correlation_id={correlation_id}, "
                        f"attempt={attempt+1}/{effective_retry_policy.max_retries+1}"
                    )
                    await asyncio.sleep(delay)
        
        return False, None, error_msg or "Max retries exceeded", error_type
    
    def _record_mention_receipt(self, response_data):
        from executor.http.mentions import mention_receipt
        meta = self.last_request_meta.get("mentions")
        if isinstance(meta, dict):
            meta.update(mention_receipt(response_data, meta.get("expected_uids", [])))
            if meta.get("missing_uids"):
                logger.warning("[MENTION] Published comment receipt did not retain all requested mentions: %s", meta["confirmation"])

    async def _prepare_publication_mentions(self, text, dynamic_params):
        from executor.http.mentions import find_mentions, prepare_mentions, validate_text_extra
        from services.mention_policy import current_mention_mode, normalize_mention_mode, order_mention_identities, PLAIN_TEXT
        self.last_request_meta = {"request_attempted": False}
        try:
            mode = normalize_mention_mode(dynamic_params.pop("mention_mode", current_mention_mode()))
            if order_mention_identities() is not None and mode != current_mention_mode():
                raise ValueError("Mention mode conflicts with the saved order choice")
            if mode == PLAIN_TEXT:
                dynamic_params["text_extra"] = "[]"
                self.last_request_meta["mentions"] = {"mode": mode, "requested": [], "resolved": [], "unresolved": []}
                return None
            if "text_extra" not in dynamic_params and find_mentions(text):
                mention_runtime = dict(self.runtime_profile or {})
                if self.page is not None:
                    mention_runtime.update(prepared_runtime_profile(self.page, self.runtime_profile) or await _extract_runtime_profile(self.page))
                    mention_cookies = normalize_cookie_records(await self.page.context.cookies())
                    mention_runtime.update({key: value for key, value in mention_cookies.items()
                                            if key in {"msToken", "s_v_web_id"}})
                normalized, extras, meta = await prepare_mentions(self.page, text, runtime_params=mention_runtime)
                self.last_request_meta["mentions"] = meta
                meta["mode"] = mode
                if meta.get("unresolved"):
                    raise ValueError(f"Mentions unresolved: {meta['unresolved']}")
                dynamic_params.update(text=normalized, text_extra=extras)
            entries = validate_text_extra(dynamic_params.get("text", text), dynamic_params.get("text_extra", "[]"))
            if entries:
                self.last_request_meta.setdefault("mentions", {"requested": [u for _, _, u in find_mentions(text)],
                                                               "resolved": [str(e["user_id"]) for e in entries]})
                self.last_request_meta["mentions"]["mode"] = mode
                self.last_request_meta["mentions"]["expected_uids"] = sorted({str(e["user_id"]) for e in entries})
                dynamic_params["text_extra"] = json.dumps(entries, ensure_ascii=False, separators=(",", ":"))
        except Exception as exc:
            return f"Mention preparation failed: {exc}"
        return None

    async def like_comment(
        self,
        aweme_id: str,
        cid: str,
        cookies: Optional[Dict[str, str]] = None,
        user_agent: Optional[str] = None,
        correlation_id: Optional[str] = None,
        digg_type: int = 1,
        video_url: Optional[str] = None,
        proxy_country_code: Optional[str] = None,
        retry_policy: Optional[RetryPolicy] = None,
    ) -> Tuple[bool, Optional[str], Optional[HttpErrorType]]:
        """
        Лайкает комментарий через HTTP.
        
        Args:
            aweme_id: ID видео
            cid: ID комментария
            cookies: Дополнительные cookies
            user_agent: User-Agent
            correlation_id: ID для логирования
            digg_type: Тип действия (1 = like, 2 = unlike в веб-API TikTok)
            video_url: URL видео для установки referer
            proxy_country_code: Код страны прокси
            retry_policy: Политика повторов (по умолчанию LIKE_FAST_POLICY с 0 повторов)
        
        Returns:
            tuple: (success, error_message, error_type)
        """
        profile = RequestProfile.like_profile()
        dynamic_params = {
            "aweme_id": str(aweme_id).strip(),
            "cid": str(cid).strip(),
            "digg_type": str(digg_type)
        }
        
        if video_url:
            referer_url = self._canonical_referer_url(video_url, self.runtime_profile)
            dynamic_params["referer"] = referer_url

        effective_policy = retry_policy or LIKE_FAST_POLICY
        request_runtime_profile = self._runtime_profile_for_request(proxy_country_code)
        success, response_data, error_msg, error_type = await self._execute_request(
            profile=profile,
            dynamic_params=dynamic_params,
            cookies=cookies,
            user_agent=user_agent,
            correlation_id=correlation_id,
            runtime_profile_override=request_runtime_profile,
            retry_policy_override=effective_policy,
        )
        
        if success:
            return True, None, None
        else:
            return False, error_msg, error_type
    
    async def reply_comment(
        self,
        aweme_id: str,
        parent_comment_id: str,
        text: str,
        cookies: Optional[Dict[str, str]] = None,
        user_agent: Optional[str] = None,
        correlation_id: Optional[str] = None,
        video_url: Optional[str] = None,
        **extra_params
    ) -> Tuple[bool, Optional[str], Optional[str], Optional[HttpErrorType], Dict[str, Any]]:
        """
        Отправляет ответ на комментарий через HTTP.
        
        Args:
            aweme_id: ID видео
            parent_comment_id: ID родительского комментария (reply_id)
            text: Текст ответа
            cookies: Дополнительные cookies
            user_agent: User-Agent
            correlation_id: ID для логирования
            video_url: URL видео (для referer)
        
        Returns:
            tuple: (success, comment_id, error_message, error_type, meta)
        """
        from services.spintax import expand_spintax, has_spintax
        if has_spintax(text):
            text = expand_spintax(text)
        profile = RequestProfile.publish_reply_profile()
        extra_params = dict(extra_params or {})
        proxy_country_code = extra_params.pop("proxy_country_code", None)
        dynamic_params = {
            "aweme_id": aweme_id,
            "reply_id": str(parent_comment_id),
            "text": text
        }
        
        # Дополнительные query параметры (например X-Bogus)
        if extra_params:
            dynamic_params.update(extra_params)
        if "reply_to_reply_id" in dynamic_params and dynamic_params["reply_to_reply_id"] is not None:
            dynamic_params["reply_to_reply_id"] = str(dynamic_params["reply_to_reply_id"])
        
        # ИСПРАВЛЕНО: Избегаем мутации profile.static_params (копируем если нужно изменить)
        # TikTok API использует query-параметры, поэтому referer можно установить через dynamic_params
        if video_url:
            # Устанавливаем referer через dynamic_params (не мутируем profile)
            referer_url = self._canonical_referer_url(video_url, self.runtime_profile)
            dynamic_params["referer"] = referer_url

        mention_error = await self._prepare_publication_mentions(text, dynamic_params)
        if mention_error:
            return False, None, mention_error, HttpErrorType.API_ERROR, {"parent_confirmed": False, "request_attempted": False, "request_meta": dict(self.last_request_meta)}

        request_runtime_profile = self._runtime_profile_for_request(proxy_country_code)
        success, response_data, error_msg, error_type = await self._execute_request(
            profile=profile,
            dynamic_params=dynamic_params,
            cookies=cookies,
            user_agent=user_agent,
            correlation_id=correlation_id,
            runtime_profile_override=request_runtime_profile
        )
        
        if success:
            comment_id = _extract_comment_id(response_data)
            request_signature = response_data.get("__request_signature") if isinstance(response_data, dict) else None
            if not comment_id:
                return False, None, "Response OK but missing comment_id", HttpErrorType.API_ERROR, {
                    "parent_confirmed": False,
                    "response_parent_id": None,
                    "response_reply_to_reply_id": None,
                    "response_root_comment_id": None,
                    "parent_confirmation_level": "unconfirmed",
                    "request_signature": request_signature,
                    "request_meta": dict(self.last_request_meta),
                }

            expected_parent_id = _normalize_identifier(parent_comment_id)
            response_parent_id = None
            response_reply_to_reply_id = None
            response_root_comment_id = None
            parent_confirmed = not bool(expected_parent_id)
            parent_confirmation_level = "not_applicable" if not expected_parent_id else "unconfirmed"
            if expected_parent_id:
                reply_network_meta = _extract_reply_network_metadata(response_data)
                response_parent_id = reply_network_meta.get("direct_parent_id")
                response_reply_to_reply_id = reply_network_meta.get("reply_to_reply_id")
                response_root_comment_id = reply_network_meta.get("root_comment_id")

                direct_parent_candidates = {
                    value
                    for value in (response_parent_id, response_reply_to_reply_id)
                    if value
                }

                if direct_parent_candidates and expected_parent_id not in direct_parent_candidates:
                    mismatch_reason = (
                        "Reply parent mismatch: "
                        f"expected={expected_parent_id}, "
                        f"got_direct={response_parent_id}, "
                        f"got_reply_to_reply={response_reply_to_reply_id}"
                    )
                    logger.warning(
                        "[HTTP] Reply response returned a comment_id but parent mismatch was detected | "
                        "correlation_id=%s, comment_id=%s, %s. Treating as submitted-unconfirmed "
                        "to avoid duplicate DOM retry.",
                        correlation_id,
                        comment_id[:40],
                        mismatch_reason,
                    )
                    return True, comment_id, None, None, {
                        "parent_confirmed": False,
                        "response_parent_id": response_parent_id,
                        "response_reply_to_reply_id": response_reply_to_reply_id,
                        "response_root_comment_id": response_root_comment_id,
                        "parent_confirmation_level": "network_parent_mismatch",
                        "parent_mismatch": True,
                        "parent_mismatch_reason": mismatch_reason,
                        "request_signature": request_signature,
                        "request_meta": dict(self.last_request_meta),
                    }

                if expected_parent_id in direct_parent_candidates:
                    parent_confirmed = True
                    parent_confirmation_level = (
                        "network_reply_to_reply"
                        if response_reply_to_reply_id == expected_parent_id and response_parent_id != expected_parent_id
                        else "network_exact"
                    )
                elif (
                    response_root_comment_id == expected_parent_id
                    and not direct_parent_candidates
                ):
                    parent_confirmed = True
                    parent_confirmation_level = "network_root_match"

                if not parent_confirmed:
                    logger.warning(
                        f"[HTTP] Reply response missing strong parent confirmation | "
                        f"correlation_id={correlation_id}, "
                        f"expected_parent_id={expected_parent_id[:40]}, "
                        f"comment_id={comment_id[:40]}. "
                        f"direct_parent_id={str(response_parent_id)[:40] if response_parent_id else 'None'}, "
                        f"reply_to_reply_id={str(response_reply_to_reply_id)[:40] if response_reply_to_reply_id else 'None'}, "
                        f"root_comment_id={str(response_root_comment_id)[:40] if response_root_comment_id else 'None'}. "
                        f"Publication returned an ID; parent confirmation remains unconfirmed."
                    )

            self._record_mention_receipt(response_data)

            return True, comment_id, None, None, {
                "parent_confirmed": parent_confirmed,
                "response_parent_id": response_parent_id,
                "response_reply_to_reply_id": response_reply_to_reply_id,
                "response_root_comment_id": response_root_comment_id,
                "parent_confirmation_level": parent_confirmation_level,
                "request_signature": request_signature,
                "request_meta": dict(self.last_request_meta),
            }
        else:
            return False, None, error_msg, error_type, {
                "parent_confirmed": False,
                "response_parent_id": None,
                "response_reply_to_reply_id": None,
                "response_root_comment_id": None,
                "parent_confirmation_level": "unconfirmed",
                "request_signature": self.last_request_meta.get("signature"),
                "request_meta": dict(self.last_request_meta),
            }
    
    async def publish_main_comment(
        self,
        aweme_id: str,
        text: str,
        cookies: Optional[Dict[str, str]] = None,
        user_agent: Optional[str] = None,
        correlation_id: Optional[str] = None,
        video_url: Optional[str] = None,
        **extra_params
    ) -> Tuple[bool, Optional[str], Optional[str], Optional[HttpErrorType]]:
        """
        Публикует главный комментарий через HTTP в контексте текущей страницы.
        
        Args:
            aweme_id: ID видео
            text: Текст комментария
            cookies: Дополнительные cookies
            user_agent: User-Agent
            correlation_id: ID для логирования
            video_url: URL видео (для referer)
            **extra_params: Дополнительные query параметры (X-Bogus и др.)
        
        Returns:
            tuple: (success, comment_id, error_message, error_type)
        """
        from services.spintax import expand_spintax, has_spintax
        if has_spintax(text):
            text = expand_spintax(text)
        profile = RequestProfile.publish_main_profile()
        extra_params = dict(extra_params or {})
        proxy_country_code = extra_params.pop("proxy_country_code", None)
        dynamic_params = {
            "aweme_id": aweme_id,
            "text": text
        }
        
        # Добавляем любые extra_params (например, X-Bogus)
        dynamic_params.update(extra_params)
        
        # ИСПРАВЛЕНО: Избегаем мутации profile.static_params (копируем если нужно изменить)
        # TikTok API использует query-параметры, поэтому referer можно установить через dynamic_params
        if video_url:
            # Устанавливаем referer через dynamic_params (не мутируем profile)
            referer_url = self._canonical_referer_url(video_url, self.runtime_profile)
            dynamic_params["referer"] = referer_url

        mention_error = await self._prepare_publication_mentions(text, dynamic_params)
        if mention_error:
            return False, None, mention_error, HttpErrorType.API_ERROR

        request_runtime_profile = self._runtime_profile_for_request(proxy_country_code)
        success, response_data, error_msg, error_type = await self._execute_request(
            profile=profile,
            dynamic_params=dynamic_params,
            cookies=cookies,
            user_agent=user_agent,
            correlation_id=correlation_id,
            runtime_profile_override=request_runtime_profile
        )
        
        if success:
            comment_id = _extract_comment_id(response_data)
            if not comment_id:
                return False, None, "Response OK but missing comment_id", HttpErrorType.API_ERROR
            self.last_request_meta["comment_author"] = _extract_comment_author(response_data)
            self.last_request_meta["comment_author_uid"] = _extract_comment_user_id(response_data)
            self._record_mention_receipt(response_data)
            return True, comment_id, None, None
        else:
            return False, None, error_msg, error_type
