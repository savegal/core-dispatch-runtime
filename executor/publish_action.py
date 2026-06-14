from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import time
from asyncio import wait_for as _asyncio_wait_for
from dataclasses import dataclass
from typing import Any, Dict, Optional

from patchright.async_api import Page, Response

from executor.dom_actions import (
    DomActionResult,
    atomic_set_comment_text,
    comment_input_contains_expected_text,
    find_comment_input_smart,
    find_post_button_smart,
    find_reply_input_smart,
    find_reply_post_button_smart,
    get_comment_surface_state,
    open_comment_section,
    open_reply_box,
    type_into_comment,
    wait_for_post_button_enabled,
    suppress_keyboard_shortcuts_overlay,
    ensure_click_target_clear,
    dismiss_cookie_banners,
)
from executor.captcha_detector import detect_captcha
from executor.session_bootstrap import get_browser_login_state
from config.settings import get_settings

logger = logging.getLogger("executor.publish_action")

MANUAL_CAPTCHA_WAIT_SECONDS = float(os.getenv("MANUAL_CAPTCHA_WAIT_SECONDS", "15"))
MANUAL_CAPTCHA_POLL_SECONDS = 2.0
_REPLY_INPUT_MIN_SECONDS = 8.0
_PARALLEL_REPLY_INPUT_MIN_SECONDS = float(os.getenv("PARALLEL_REPLY_INPUT_MIN_SECONDS", "4"))
_PARALLEL_POST_RESPONSE_WAIT_SECONDS = float(os.getenv("PARALLEL_POST_RESPONSE_WAIT_SECONDS", "2.0"))
_PARALLEL_POST_RESPONSE_GRACE_SECONDS = float(os.getenv("PARALLEL_POST_RESPONSE_GRACE_SECONDS", "0.35"))
_TRUE_ENV_VALUES = {"1", "true", "yes", "on"}
_TIKTOK_MEDIA_ID_RE = re.compile(r"(?:/(?:video|photo)/|[?&](?:aweme_id|item_id)=)(\d+)", re.IGNORECASE)
_PARALLEL_FAST_PUBLISH_MARKERS = (
    "_parallel_main_",
    "_parallel_reply_",
    "_engagement_reply_",
)
_PARALLEL_MAIN_OPEN_COMMENTS_SECONDS_DEFAULT = 5.5
_PARALLEL_MAIN_INPUT_SEARCH_SECONDS_DEFAULT = 6.5


@dataclass
class PublishResult:
    status: str
    comment_id: Optional[str] = None
    error_reason: Optional[str] = None
    network_captured: bool = False
    network_request_attempted: bool = False
    network_status_code: Optional[int] = None
    network_body_preview: Optional[str] = None
    elapsed_seconds: float = 0.0
    dom_strategy: Optional[str] = None
    network_response_json: Optional[Dict[str, Any]] = None
    # True when the Post button passed wait_for_post_button_enabled before the
    # click happened. False when we went through the synthetic-dispatch path on
    # a disabled button — in that case the click may or may not have actually
    # triggered React's handler, and downstream logic must not treat the
    # publish as locally confirmed without a network response.
    post_was_enabled_at_click: bool = False


def _extract_tiktok_media_id(url: Optional[str]) -> Optional[str]:
    if not url:
        return None
    match = _TIKTOK_MEDIA_ID_RE.search(str(url))
    return match.group(1) if match else None


def _is_parallel_fast_publish(correlation_id: Optional[str]) -> bool:
    text = str(correlation_id or "")
    return any(marker in text for marker in _PARALLEL_FAST_PUBLISH_MARKERS)


def _post_click_fallback_selector(*, reply_scoped: bool) -> str:
    if reply_scoped:
        return '[data-smart-reply-post="1"]'
    return (
        '[data-smart-id="comment-post-ready"], '
        '[data-smart-id="comment-post"], '
        '[data-e2e="comment-post"]'
    )


# One cheap evaluate that reports whether the comment/reply composer is empty
# (placeholder) — the strongest local "submit succeeded" signal, since TikTok
# clears the editor right after a successful publish even when it returns an
# empty response body. For replies we look at the reply-scoped editor first.
_JS_COMPOSER_CLEARED = r"""
([replyScoped]) => {
    const placeholderMarkers = [
        'add comment', 'add a comment', 'say something', 'leave a comment',
        'добавьте комментар', 'напишите комментар', 'комментарий'
    ];
    const pick = () => {
        if (replyScoped) {
            const r = document.querySelector('[data-smart-reply-input="1"]');
            if (r) return r;
        }
        const sels = [
            '[data-smart-id="comment-input"]',
            '[data-e2e="comment-input"] div[contenteditable="true"]',
            '[data-e2e="comment-input"] [contenteditable="true"]',
            '[data-e2e*="comment-input"] [contenteditable="true"]',
            'div[contenteditable="true"]',
            'textarea',
        ];
        for (const s of sels) {
            const el = document.querySelector(s);
            if (el) return el;
        }
        return null;
    };
    const el = pick();
    if (!el) return { exists: false, empty: false, text: '' };
    const raw = (el.value !== undefined && el.value !== null && el.value !== '')
        ? el.value
        : (el.textContent || el.innerText || '');
    const txt = String(raw).trim();
    const lower = txt.toLowerCase();
    const isPlaceholder = placeholderMarkers.some(m => lower.startsWith(m));
    return {
        exists: true,
        empty: txt.length === 0 || isPlaceholder,
        text: txt.slice(0, 160),
    };
}
"""


_JS_COMPOSER_CLEARED_V2 = r"""
([replyScoped]) => {
    const placeholderMarkers = [
        'add comment', 'add a comment', 'say something', 'leave a comment',
        '\u0434\u043e\u0431\u0430\u0432\u044c\u0442\u0435 \u043a\u043e\u043c\u043c\u0435\u043d\u0442\u0430\u0440',
        '\u043d\u0430\u043f\u0438\u0448\u0438\u0442\u0435 \u043a\u043e\u043c\u043c\u0435\u043d\u0442\u0430\u0440',
        '\u0434\u043e\u0434\u0430\u0442\u0438 \u043a\u043e\u043c\u0435\u043d\u0442\u0430\u0440',
        '\u0434\u043e\u0434\u0430\u0442\u0438 \u0432\u0456\u0434\u043f\u043e\u0432\u0456\u0434\u044c'
    ];
    const visible = (el) => {
        if (!el || !el.getBoundingClientRect) return false;
        const r = el.getBoundingClientRect();
        const st = getComputedStyle(el);
        return r.width > 0 && r.height > 0 && st.display !== 'none' && st.visibility !== 'hidden';
    };
    const isRealEditor = (el) => el && (
        el.isContentEditable ||
        (el.getAttribute && el.getAttribute('contenteditable') === 'true') ||
        el.tagName === 'TEXTAREA' ||
        el.tagName === 'INPUT' ||
        (el.getAttribute && el.getAttribute('role') === 'textbox')
    );
    const labelOf = (el) => [
        el && el.getAttribute && (el.getAttribute('data-e2e') || ''),
        el && el.getAttribute && (el.getAttribute('data-smart-id') || ''),
        el && el.getAttribute && (el.getAttribute('aria-label') || ''),
        el && el.getAttribute && (el.getAttribute('placeholder') || ''),
        el && el.getAttribute && (el.getAttribute('data-placeholder') || ''),
        el && String(el.className || ''),
        el && (el.id || ''),
        el && (el.tagName || ''),
    ].join(' ').toLowerCase();
    const rejectScope = (el) => {
        for (let cur = el, depth = 0; cur && cur !== document.body && depth < 9; cur = cur.parentElement, depth++) {
            const label = labelOf(cur);
            if (label.includes('search') || label.includes('captcha') || label.includes('challenge')) return true;
            if (label.includes('login') || label.includes('sign in') || label.includes('signup')) return true;
            if (!replyScoped && (label.includes('data-smart-reply') || label.includes('smart-reply'))) return true;
        }
        return false;
    };
    const addEditor = (editors, raw) => {
        if (!raw || !visible(raw) || rejectScope(raw)) return;
        if (isRealEditor(raw)) {
            editors.push(raw);
            return;
        }
        const inner = raw.querySelector && raw.querySelector(
            'div[contenteditable="true"], [contenteditable="true"], textarea, input, [role="textbox"]'
        );
        if (inner && visible(inner) && isRealEditor(inner) && !rejectScope(inner)) {
            editors.push(inner);
        }
    };
    const editors = [];
    if (replyScoped) {
        addEditor(editors, document.querySelector('[data-smart-reply-input="1"]'));
    }
    for (const selector of [
        '[data-smart-id="comment-input"]',
        '[data-e2e="comment-input"]',
        '[data-e2e*="comment-input"]',
        '[data-e2e*="CommentInput"]'
    ]) {
        for (const raw of document.querySelectorAll(selector)) addEditor(editors, raw);
    }
    for (const raw of document.querySelectorAll(
        'div[contenteditable="true"], [contenteditable="true"], textarea, input, [role="textbox"]'
    )) {
        addEditor(editors, raw);
    }
    const el = editors[0] || null;
    if (!el) return { exists: false, empty: false, text: '' };
    const raw = (el.value !== undefined && el.value !== null && el.value !== '')
        ? el.value
        : (el.textContent || el.innerText || '');
    const txt = String(raw).trim();
    const lower = txt.toLowerCase();
    const isPlaceholder = placeholderMarkers.some(m => lower.startsWith(m));
    return {
        exists: true,
        empty: txt.length === 0 || isPlaceholder,
        text: txt.slice(0, 160),
        tag: el.tagName,
        e2e: el.getAttribute && el.getAttribute('data-e2e'),
        smart: el.getAttribute && el.getAttribute('data-smart-id'),
        contenteditable: el.getAttribute && el.getAttribute('contenteditable'),
    };
}
"""


_JS_CLEAR_MAIN_COMMENT_COMPOSER = r"""
() => {
    const visible = (el) => {
        if (!el || !el.getBoundingClientRect) return false;
        const r = el.getBoundingClientRect();
        const st = getComputedStyle(el);
        return r.width > 0 && r.height > 0 && st.display !== 'none' && st.visibility !== 'hidden';
    };
    const isRealEditor = (el) => el && (
        el.isContentEditable ||
        (el.getAttribute && el.getAttribute('contenteditable') === 'true') ||
        el.tagName === 'TEXTAREA' ||
        el.tagName === 'INPUT' ||
        (el.getAttribute && el.getAttribute('role') === 'textbox')
    );
    const labelOf = (el) => [
        el && el.getAttribute && (el.getAttribute('data-e2e') || ''),
        el && el.getAttribute && (el.getAttribute('data-smart-id') || ''),
        el && el.getAttribute && (el.getAttribute('aria-label') || ''),
        el && el.getAttribute && (el.getAttribute('placeholder') || ''),
        el && el.getAttribute && (el.getAttribute('data-placeholder') || ''),
        el && String(el.className || ''),
        el && (el.id || ''),
        el && (el.tagName || ''),
    ].join(' ').toLowerCase();
    const rejectScope = (el) => {
        for (let cur = el, depth = 0; cur && cur !== document.body && depth < 9; cur = cur.parentElement, depth++) {
            const label = labelOf(cur);
            if (label.includes('search') || label.includes('captcha') || label.includes('challenge')) return true;
            if (label.includes('login') || label.includes('sign in') || label.includes('signup')) return true;
            if (label.includes('data-smart-reply') || label.includes('smart-reply')) return true;
        }
        return false;
    };
    const candidates = [];
    const addEditor = (raw) => {
        if (!raw || !visible(raw) || rejectScope(raw)) return;
        if (isRealEditor(raw)) {
            candidates.push(raw);
            return;
        }
        const inner = raw.querySelector && raw.querySelector(
            'div[contenteditable="true"], [contenteditable="true"], textarea, input, [role="textbox"]'
        );
        if (inner && visible(inner) && isRealEditor(inner) && !rejectScope(inner)) {
            candidates.push(inner);
        }
    };
    for (const selector of [
        '[data-smart-id="comment-input"]',
        '[data-e2e="comment-input"]',
        '[data-e2e*="comment-input"]',
        '[data-e2e*="CommentInput"]'
    ]) {
        for (const raw of document.querySelectorAll(selector)) addEditor(raw);
    }
    for (const raw of document.querySelectorAll(
        'div[contenteditable="true"], [contenteditable="true"], textarea, input, [role="textbox"]'
    )) {
        addEditor(raw);
    }
    const el = candidates[0] || null;
    if (!el) return { ok: false, reason: 'no_editor' };
    try { el.focus(); } catch (_) {}
    try {
        if (el.tagName === 'TEXTAREA' || el.tagName === 'INPUT') {
            const proto = el.tagName === 'TEXTAREA'
                ? window.HTMLTextAreaElement.prototype
                : window.HTMLInputElement.prototype;
            const desc = Object.getOwnPropertyDescriptor(proto, 'value');
            if (desc && desc.set) desc.set.call(el, '');
            else el.value = '';
        } else {
            const sel = window.getSelection && window.getSelection();
            if (sel) {
                const range = document.createRange();
                range.selectNodeContents(el);
                sel.removeAllRanges();
                sel.addRange(range);
                range.deleteContents();
                sel.removeAllRanges();
            }
            el.textContent = '';
        }
        for (const ev of [
            new InputEvent('beforeinput', {bubbles: true, cancelable: true, inputType: 'deleteContentBackward', data: null}),
            new InputEvent('input', {bubbles: true, cancelable: true, inputType: 'deleteContentBackward', data: null}),
            new Event('change', {bubbles: true, cancelable: true}),
            new KeyboardEvent('keyup', {bubbles: true, cancelable: true, key: 'Backspace'})
        ]) {
            try { el.dispatchEvent(ev); } catch (_) {}
        }
    } catch (exc) {
        return { ok: false, reason: 'clear_exception:' + String(exc).slice(0, 80) };
    }
    const txt = (el.value !== undefined && el.value !== null && el.value !== '')
        ? el.value
        : (el.textContent || el.innerText || '');
    return { ok: String(txt || '').trim().length === 0, reason: 'cleared', text: String(txt || '').slice(0, 120) };
}
"""


async def _composer_cleared_state(page: Page, *, reply_scoped: bool) -> Dict[str, Any]:
    """Return {exists, empty, text} for the comment/reply composer."""
    try:
        state = await page.evaluate(_JS_COMPOSER_CLEARED_V2, [bool(reply_scoped)])
        if isinstance(state, dict):
            return state
    except Exception as exc:
        logger.debug("[PUBLISH] composer-cleared probe failed: %s", str(exc)[:120])
    return {"exists": False, "empty": False, "text": ""}


async def _clear_main_comment_composer_best_effort(
    page: Page,
    *,
    reason: str,
    expected_video_url: Optional[str] = None,
    correlation_id: Optional[str] = None,
    timeout_seconds: float = 1.2,
) -> bool:
    """Clear dirty main composer state before retrying another variant/account."""
    deadline = time.monotonic() + max(0.2, timeout_seconds)
    cleared = False
    try:
        target = await _asyncio_wait_for(
            page.evaluate(_JS_MAIN_COMMENT_KEYBOARD_FOCUS_TARGET),
            timeout=max(0.1, min(0.35, deadline - time.monotonic())),
        )
    except Exception:
        target = None

    if isinstance(target, dict) and target.get("found"):
        rect = target.get("rect") if isinstance(target.get("rect"), dict) else {}
        try:
            x = float(rect.get("left") or 0.0) + max(2.0, float(rect.get("width") or 0.0) / 2.0)
            y = float(rect.get("top") or 0.0) + max(2.0, float(rect.get("height") or 0.0) / 2.0)
            await page.mouse.click(x, y)
            await page.keyboard.press("Control+A")
            await page.keyboard.press("Backspace")
            state = await _asyncio_wait_for(
                _composer_cleared_state(page, reply_scoped=False),
                timeout=max(0.1, min(0.35, deadline - time.monotonic())),
            )
            cleared = bool(state.get("exists") and state.get("empty"))
        except Exception:
            cleared = False

    if not cleared and deadline - time.monotonic() > 0.1:
        try:
            result = await _asyncio_wait_for(
                page.evaluate(_JS_CLEAR_MAIN_COMMENT_COMPOSER),
                timeout=max(0.1, min(0.75, deadline - time.monotonic())),
            )
            cleared = bool(isinstance(result, dict) and result.get("ok"))
        except Exception:
            cleared = False

    _log_publish_trace(
        "main_composer_clear_after_failure",
        page=page,
        expected_video_url=expected_video_url,
        correlation_id=correlation_id,
        reason=reason,
        cleared=cleared,
        level=logging.INFO if cleared else logging.WARNING,
    )
    return cleared


async def _enter_submit_fast_path(
    page: Page,
    input_handle: Any,
    *,
    reply_scoped: bool,
) -> bool:
    """Try submitting via a real Enter keystroke after ensuring the editor is focused.

    Returns True only when the composer actually CLEARED (submit accepted). If
    Enter instead inserted a newline (composer not cleared, text grew), the
    newline is removed with Backspace and False is returned so the caller falls
    back to the existing Post-button click path. Usable for both main and reply;
    for replies focus is placed on the reply editor first.
    """
    before = await _composer_cleared_state(page, reply_scoped=reply_scoped)
    if not before.get("exists") or before.get("empty"):
        # Nothing meaningful to submit via Enter; let the button path handle it.
        return False
    before_text = str(before.get("text") or "")

    try:
        if input_handle is not None:
            try:
                await input_handle.focus()
            except Exception:
                try:
                    await input_handle.click(timeout=1500)
                except Exception:
                    pass
        await page.keyboard.press("Enter")
    except Exception as exc:
        logger.debug("[PUBLISH] Enter submit fast-path keystroke failed: %s", str(exc)[:120])
        return False

    await asyncio.sleep(0.2)
    after = await _composer_cleared_state(page, reply_scoped=reply_scoped)
    if after.get("exists") and after.get("empty"):
        logger.info("[PUBLISH] Enter submit fast-path cleared the composer (reply=%s)", reply_scoped)
        return True

    # Enter was treated as a newline (or did nothing). Undo any inserted newline
    # so the text stays clean for the button-click fallback.
    after_text = str(after.get("text") or "")
    if after_text != before_text:
        try:
            await page.keyboard.press("Backspace")
        except Exception:
            pass
    logger.debug("[PUBLISH] Enter submit fast-path did not clear composer; falling back to button click")
    return False


def _looks_like_driver_closed_error(text: Optional[str]) -> bool:
    safe = (text or "").lower()
    return any(
        marker in safe
        for marker in (
            "connection closed while reading from the driver",
            "protocol error",
            "session closed",
            "internal server error, session closed",
            "target page, context or browser has been closed",
            "browser has been closed",
            "context has been closed",
            "page has been closed",
        )
    )


def _is_parallel_main_publish(correlation_id: Optional[str]) -> bool:
    return "_parallel_main_" in str(correlation_id or "")


def _surface_has_publish_input(surface: Any) -> bool:
    if not isinstance(surface, dict):
        return False
    return bool(
        surface.get("comment_input_visible")
        or surface.get("publish_input_candidate_visible")
    )


def _float_setting(name: str, default: float, *, minimum: float, maximum: float) -> float:
    try:
        raw_value = getattr(get_settings(), name, default)
        value = float(raw_value)
    except Exception:
        value = float(default)
    return min(max(value, minimum), maximum)


def _parallel_main_open_comments_seconds() -> float:
    return _float_setting(
        "PARALLEL_MAIN_OPEN_COMMENTS_MAX_SECONDS",
        _PARALLEL_MAIN_OPEN_COMMENTS_SECONDS_DEFAULT,
        minimum=4.0,
        maximum=12.0,
    )


def _parallel_main_input_search_seconds() -> float:
    return _float_setting(
        "PARALLEL_MAIN_INPUT_SEARCH_MAX_SECONDS",
        _PARALLEL_MAIN_INPUT_SEARCH_SECONDS_DEFAULT,
        minimum=3.0,
        maximum=10.0,
    )


def extract_tiktok_media_id(url: Optional[str]) -> Optional[str]:
    return _extract_tiktok_media_id(url)


def _log_publish_trace(
    stage: str,
    *,
    page: Optional[Page] = None,
    expected_video_url: Optional[str] = None,
    correlation_id: Optional[str] = None,
    level: int = logging.INFO,
    **fields: Any,
) -> None:
    current_url = getattr(page, "url", None) if page is not None else None
    payload: Dict[str, Any] = {
        "stage": stage,
        "correlation_id": correlation_id,
        "expected_id": _extract_tiktok_media_id(expected_video_url),
        "current_id": _extract_tiktok_media_id(current_url),
        "current_url": current_url,
    }
    payload.update({k: v for k, v in fields.items() if v is not None})
    logger.log(level, "[PUBLISH_TRACE] %s", json.dumps(payload, ensure_ascii=False)[:2000])


def _publish_deadline_result(
    *,
    phase: str,
    started: float,
    page: Optional[Page],
    expected_video_url: Optional[str],
    correlation_id: Optional[str],
    dom_strategy: Optional[str] = None,
) -> PublishResult:
    elapsed = time.monotonic() - started
    _log_publish_trace(
        "publish_result",
        page=page,
        expected_video_url=expected_video_url,
        correlation_id=correlation_id,
        status="timeout",
        reason=f"publish_deadline_exceeded:{phase}",
        elapsed=round(elapsed, 2),
        level=logging.WARNING,
    )
    return PublishResult(
        status="timeout",
        error_reason=f"publish_deadline_exceeded:{phase}",
        elapsed_seconds=elapsed,
        dom_strategy=dom_strategy,
    )


def is_on_expected_media_url(current_url: Optional[str], expected_video_url: Optional[str]) -> bool:
    """Return True only when current TikTok media id matches the expected target.

    If expected_video_url is a short-link without a media id, we cannot verify
    it here and intentionally allow the call. Callers should pass the resolved
    /video/<id> URL whenever they have it.
    """
    if not expected_video_url:
        return True
    expected_id = _extract_tiktok_media_id(expected_video_url)
    if not expected_id:
        return True
    current_id = _extract_tiktok_media_id(current_url)
    return bool(current_id and current_id == expected_id)


def _publish_url_guard_result(
    page: Page,
    expected_video_url: Optional[str],
    started: float,
    *,
    dom_strategy: Optional[str] = None,
    phase: str,
    correlation_id: Optional[str],
) -> Optional[PublishResult]:
    if is_on_expected_media_url(getattr(page, "url", None), expected_video_url):
        if expected_video_url:
            _log_publish_trace(
                "url_guard_ok",
                page=page,
                expected_video_url=expected_video_url,
                correlation_id=correlation_id,
                phase=phase,
                dom_strategy=dom_strategy,
            )
        return None
    logger.warning(
        "[PUBLISH] URL drift before %s for %s: expected=%s current=%s",
        phase,
        correlation_id or "unknown",
        expected_video_url,
        getattr(page, "url", None),
    )
    _log_publish_trace(
        "url_guard_failed",
        page=page,
        expected_video_url=expected_video_url,
        correlation_id=correlation_id,
        level=logging.WARNING,
        phase=phase,
        dom_strategy=dom_strategy,
        result="url_drift_to_other_video",
    )
    return PublishResult(
        status="failed",
        error_reason="url_drift_to_other_video",
        elapsed_seconds=time.monotonic() - started,
        dom_strategy=dom_strategy,
    )


def _login_state_logged_out_reason(state: dict, *, phase: str) -> Optional[str]:
    login_count = int(state.get("loginCount") or 0)
    login_modal_count = int(state.get("loginModalCount") or 0)
    comment_login_cta_count = int(state.get("commentLoginCtaCount") or 0)
    state_is_login_false = bool(state.get("stateIsLoginFalse"))
    has_visible_login_ui = (
        login_count > 0
        or login_modal_count > 0
        or comment_login_cta_count > 0
        or state_is_login_false
    )
    if str(state.get("state") or "").lower() != "logged_out" and not has_visible_login_ui:
        return None
    return (
        f"visible_login_ui:{phase}:"
        f"login={login_count}:"
        f"modal={login_modal_count}:"
        f"comment_cta={comment_login_cta_count}:"
        f"state_false={state_is_login_false}"
    )


async def _logged_out_publish_result_if_visible(
    page: Page,
    started: float,
    *,
    phase: str,
    correlation_id: Optional[str],
    expected_video_url: Optional[str],
    timeout_seconds: float = 1.5,
) -> Optional[PublishResult]:
    try:
        state = await _asyncio_wait_for(
            get_browser_login_state(page),
            timeout=max(0.2, float(timeout_seconds or 1.5)),
        )
    except asyncio.TimeoutError:
        logger.debug("[PUBLISH] login-state probe timed out during %s", phase)
        return None
    except Exception as exc:
        logger.debug("[PUBLISH] login-state probe failed during %s: %s", phase, exc)
        return None
    reason = _login_state_logged_out_reason(state, phase=phase)
    if not reason:
        return None
    _log_publish_trace(
        "logged_out_ui_detected",
        page=page,
        expected_video_url=expected_video_url,
        correlation_id=correlation_id,
        phase=phase,
        reason=reason,
        level=logging.WARNING,
    )
    return PublishResult(
        status="dom_error",
        error_reason=reason,
        elapsed_seconds=time.monotonic() - started,
        dom_strategy="auth-dom",
    )


def _extract_comment_id(response_json: Optional[Dict[str, Any]]) -> Optional[str]:
    if not isinstance(response_json, dict):
        return None
    comment_obj = response_json.get("comment")
    if isinstance(comment_obj, dict):
        value = comment_obj.get("cid") or comment_obj.get("id")
        normalized = str(value).strip() if value is not None else ""
        if normalized and normalized != "0":
            return normalized
    data = response_json.get("data")
    if isinstance(data, dict):
        nested = data.get("comment")
        if isinstance(nested, dict):
            value = nested.get("cid") or nested.get("id")
            normalized = str(value).strip() if value is not None else ""
            if normalized and normalized != "0":
                return normalized
    return None


def _extract_reply_parent_id(response_json: Optional[Dict[str, Any]]) -> Optional[str]:
    """Extracts the parent comment id from a TikTok comment publish response.

    A real reply carries a non-zero reply_id/parent id; a top-level (main)
    comment has none (or "0"). Returns None when no parent can be determined.
    """
    if not isinstance(response_json, dict):
        return None
    containers: list[Dict[str, Any]] = [response_json]
    data = response_json.get("data")
    if isinstance(data, dict):
        containers.append(data)
        nested = data.get("comment")
        if isinstance(nested, dict):
            containers.append(nested)
    comment_obj = response_json.get("comment")
    if isinstance(comment_obj, dict):
        containers.append(comment_obj)
    for container in containers:
        for key in ("reply_id", "parent_comment_id", "parent_id"):
            value = container.get(key)
            if value is None:
                continue
            normalized = str(value).strip()
            if normalized and normalized != "0":
                return normalized
    return None


CHALLENGE_TEXT_MARKERS = (
    "drag the slider",
    "fit the puzzle",
    "slide to verify",
    "security verification",
    "complete verification",
    "captcha",
)


def _point_diag_looks_like_challenge(diag: Optional[dict[str, Any]]) -> bool:
    if not isinstance(diag, dict):
        return False
    label = " ".join(
        str(diag.get(key) or "")
        for key in ("text", "aria", "e2e", "smart", "role")
    ).lower()
    return any(marker in label for marker in CHALLENGE_TEXT_MARKERS)


COOKIE_BANNER_TEXT_MARKERS = (
    "cookie",
    "cookies",
    "optional cookies",
    "allow all",
    "decline optional cookies",
    "manage your choices",
    "tiktok uses cookies",
    "essential cookies",
    "consent",
    "gdpr",
    "\u30af\u30c3\u30ad\u30fc",
    "\ucfe0\ud0a4",
    "\u997c\u5e72",
    "\u96b1\u79c1",
    "\u9690\u79c1",
)


def _point_diag_looks_like_cookie_banner(diag: Optional[dict[str, Any]]) -> bool:
    if not isinstance(diag, dict):
        return False
    label = " ".join(
        str(diag.get(key) or "")
        for key in ("text", "aria", "e2e", "smart", "role", "tag")
    ).lower()
    if any(marker in label for marker in COOKIE_BANNER_TEXT_MARKERS):
        return True
    try:
        y = float(diag.get("y") or 0)
        h = float(diag.get("h") or 0)
        w = float(diag.get("w") or 0)
        return w > 1000 and 70 < h < 360 and y > 300 and ("button" in label or "div" in label)
    except Exception:
        return False


def _manual_captcha_wait_enabled() -> bool:
    raw = os.getenv("ALLOW_MANUAL_CAPTCHA_SOLVE")
    if raw is None or raw.strip().lower() not in _TRUE_ENV_VALUES:
        return False
    try:
        return not bool(get_settings().PLAYWRIGHT_HEADLESS)
    except Exception:
        return False


async def _wait_for_manual_captcha_clear(page: Page, captcha_kind: str) -> bool:
    if not _manual_captcha_wait_enabled():
        return False

    deadline = time.monotonic() + max(10.0, MANUAL_CAPTCHA_WAIT_SECONDS)
    logger.warning(
        "[PUBLISH] TikTok %s challenge detected in visible browser; waiting up to %.0fs for manual solve.",
        captcha_kind,
        MANUAL_CAPTCHA_WAIT_SECONDS,
    )
    while time.monotonic() < deadline:
        await asyncio.sleep(MANUAL_CAPTCHA_POLL_SECONDS)
        try:
            has_captcha, _ = await detect_captcha(page, wait_ms=0)
        except Exception as exc:
            logger.debug("[PUBLISH] Captcha detector failed during manual wait: %s", exc)
            continue
        if not has_captcha:
            # Give TikTok a beat to re-render the comment box after the modal disappears.
            await asyncio.sleep(1.0)
            try:
                still_clear, _ = await detect_captcha(page, wait_ms=0)
            except Exception:
                still_clear = False
            if not still_clear:
                logger.info("[PUBLISH] TikTok challenge cleared manually; continuing publish flow.")
                return True

    logger.warning("[PUBLISH] Manual captcha wait timed out after %.0fs.", MANUAL_CAPTCHA_WAIT_SECONDS)
    return False


async def _captcha_result_or_cleared_if_present(
    page: Page,
    started: float,
    *,
    dom_strategy: Optional[str] = None,
) -> tuple[Optional[PublishResult], bool]:
    try:
        has_captcha, captcha_type = await detect_captcha(page, wait_ms=0)
    except Exception as exc:
        logger.debug("[PUBLISH] Captcha detector failed: %s", exc)
        return None, False

    if not has_captcha:
        return None, False

    captcha_kind = captcha_type or "unknown"
    if await _wait_for_manual_captcha_clear(page, captcha_kind):
        return None, True

    logger.warning("[PUBLISH] TikTok challenge detected; stopping publish attempt: %s", captcha_kind)
    return (
        PublishResult(
            status="failed",
            error_reason=f"captcha_challenge:{captcha_kind}",
            elapsed_seconds=time.monotonic() - started,
            dom_strategy=dom_strategy,
        ),
        False,
    )


async def _captcha_result_if_present(
    page: Page,
    started: float,
    *,
    dom_strategy: Optional[str] = None,
) -> Optional[PublishResult]:
    result, _cleared = await _captcha_result_or_cleared_if_present(
        page,
        started,
        dom_strategy=dom_strategy,
    )
    return result


async def _find_input_after_manual_captcha_clear(
    page: Page,
    started: float,
    initial_info: Any,
    *,
    max_total_seconds: float,
) -> tuple[Optional[Any], Any, Optional[PublishResult]]:
    """Give TikTok time to rebuild the comment composer after a manual challenge.

    После 2 фейлов делаем жёсткий reload страницы — это сбрасывает React
    state и часто возвращает composer, который TikTok порвал после captcha.
    """
    last_info = initial_info
    retry_pauses = (1.0, 2.0, 3.0, 4.0)
    reload_attempted = False
    for attempt, pause in enumerate(retry_pauses, start=1):
        await asyncio.sleep(pause)
        captcha_result = await _captcha_result_if_present(page, started)
        if captcha_result:
            return None, last_info, captcha_result

        # После 2 неудачных попыток — reload страницы (один раз).
        if attempt == 3 and not reload_attempted:
            reload_attempted = True
            try:
                logger.info("[PUBLISH] Composer not back after captcha; reloading page.")
                await page.reload(wait_until="domcontentloaded", timeout=20000)
                await asyncio.sleep(2.0)
            except Exception as exc:
                logger.debug("[PUBLISH] Reload after captcha failed: %s", exc)

        try:
            await open_comment_section(page, max_wait=5.0, require_input=True)
        except Exception as exc:
            logger.debug("[PUBLISH] Re-opening comment section after captcha clear failed: %s", exc)

        input_handle, input_info = await find_comment_input_smart(
            page,
            max_total_seconds=min(8.0, max(max_total_seconds * 0.5, 5.0)),
        )
        last_info = input_info
        if input_handle:
            logger.info(
                "[PUBLISH] Comment input recovered after manual captcha clear on retry %d.",
                attempt,
            )
            return input_handle, input_info, None

    return None, last_info, None


async def _close_keyboard_shortcuts_overlay(page: Page) -> int:
    """Hard-close TikTok keyboard shortcuts popup if it covers the Post button.

    This version does two things:
    1) runs the shared persistent DOM killer from dom_actions;
    2) presses Escape and force-removes/click-throughs all marker-containing bounded nodes.
    """
    closed_total = 0
    try:
        closed_total += await suppress_keyboard_shortcuts_overlay(page)

        for _ in range(3):
            try:
                await page.keyboard.press("Escape")
                closed_total += 1
                await asyncio.sleep(0.10)
            except Exception:
                pass

        closed = await page.evaluate(
            r"""
            () => {
                const markers = [
                    'Introducing keyboard shortcuts',
                    'Go to previous video',
                    'Go to next video',
                    'Mute / unmute video',
                    'Play / Pause',
                    'Scroll, use the',
                    'browse your feed',
                    'click the arrows on the right'
                ];

                const hasMarker = (txt) => {
                    txt = String(txt || '');
                    if (txt.includes('Introducing keyboard shortcuts')) return true;
                    const lower = txt.toLowerCase();
                    if (
                        lower.includes('browse your feed') &&
                        (lower.includes('scroll') || lower.includes('arrows on the right') || lower.includes('got it'))
                    ) return true;
                    return txt.includes('Go to previous video')
                        && txt.includes('Go to next video')
                        && txt.includes('Mute / unmute video')
                        && txt.includes('Play / Pause');
                };

                const isVisible = (el) => {
                    if (!el || !el.getBoundingClientRect) return false;
                    const r = el.getBoundingClientRect();
                    const st = getComputedStyle(el);
                    return r.width > 0 && r.height > 0 && st.visibility !== 'hidden' && st.display !== 'none';
                };

                const boundedOverlay = (el) => {
                    if (!el || !el.getBoundingClientRect) return false;
                    const r = el.getBoundingClientRect();
                    if (!(r.width > 120 && r.height > 80)) return false;
                    if (r.width > window.innerWidth * 0.95 || r.height > window.innerHeight * 0.95) return false;
                    if (r.x > window.innerWidth * 0.35 && r.y > window.innerHeight * 0.15) return true;
                    const st = getComputedStyle(el);
                    const z = parseInt(st.zIndex || '0', 10);
                    return st.position === 'fixed' || st.position === 'absolute' || z > 10;
                };
                const safeMarkerNode = (el) => {
                    if (!el || !el.getBoundingClientRect) return false;
                    const r = el.getBoundingClientRect();
                    if (r.width > window.innerWidth * 0.75 || r.height > window.innerHeight * 0.75) return false;
                    return r.width >= 20 && r.height >= 12;
                };

                const kill = (el) => {
                    if (!el || el === document.body || el === document.documentElement) return 0;
                    try {
                        el.setAttribute('data-teletan-killed-shortcuts-final', '1');
                        el.style.setProperty('pointer-events', 'none', 'important');
                        el.style.setProperty('visibility', 'hidden', 'important');
                        el.style.setProperty('opacity', '0', 'important');
                        el.style.setProperty('display', 'none', 'important');
                        el.style.setProperty('z-index', '-1', 'important');
                        try { el.remove(); } catch (_) {}
                        return 1;
                    } catch (_) { return 0; }
                };

                let count = 0;
                try {
                    const keys = [
                        'tiktok_keyboard_shortcuts_seen',
                        'keyboard_shortcuts_seen',
                        'keyboardShortcutsSeen',
                        'keyboard_shortcuts_modal_seen',
                        'video_keyboard_shortcuts_seen',
                        'shortcut_guide_seen',
                        'shortcutGuideSeen',
                        'tiktok_shortcuts_guide_seen',
                        'webapp_keyboard_shortcuts_seen',
                        'tiktok_feed_navigation_guide_seen',
                        'feed_navigation_guide_seen',
                        'webapp_feed_navigation_guide_seen',
                        'tiktok_video_navigation_guide_seen'
                    ];
                    for (const k of keys) {
                        localStorage.setItem(k, '1');
                        sessionStorage.setItem(k, '1');
                    }
                } catch (_) {}

                const nodes = [...document.querySelectorAll('body *')];
                for (const node of nodes) {
                    if (!isVisible(node)) continue;
                    const txt = node.innerText || node.textContent || '';
                    if (!hasMarker(txt)) continue;

                    // Kill the node and bounded ancestors. Previous code killed only a "best" node;
                    // TikTok often reuses a parent wrapper which still intercepts clicks.
                    let cur = node;
                    for (let i = 0; i < 10 && cur && cur !== document.body && cur !== document.documentElement; i++, cur = cur.parentElement) {
                        const curTxt = cur.innerText || cur.textContent || '';
                        if (!hasMarker(curTxt)) continue;
                        if (boundedOverlay(cur)) count += kill(cur);
                        else if (i <= 1 && safeMarkerNode(cur)) {
                            try {
                                cur.style.setProperty('pointer-events', 'none', 'important');
                                cur.style.setProperty('visibility', 'hidden', 'important');
                                cur.style.setProperty('opacity', '0', 'important');
                                cur.setAttribute('data-teletan-clickthrough-shortcuts-final', '1');
                                count++;
                            } catch (_) {}
                        }
                    }
                }

                // Explicitly clear the element currently sitting on the Post click point.
                const points = [
                    [window.innerWidth - 44, window.innerHeight - 49],
                    [window.innerWidth - 36, window.innerHeight - 36],
                    [window.innerWidth * 0.88, window.innerHeight * 0.91],
                    [window.innerWidth * 0.98, window.innerHeight * 0.95],
                ];
                for (const [x, y] of points) {
                    let el = document.elementFromPoint(x, y);
                    for (let i = 0; i < 10 && el && el !== document.body && el !== document.documentElement; i++, el = el.parentElement) {
                        const txt = el.innerText || el.textContent || '';
                        if (hasMarker(txt)) {
                            if (boundedOverlay(el)) count += kill(el);
                            else if (i <= 1 && safeMarkerNode(el)) {
                                try {
                                    el.style.setProperty('pointer-events', 'none', 'important');
                                    el.style.setProperty('visibility', 'hidden', 'important');
                                    el.style.setProperty('opacity', '0', 'important');
                                    count++;
                                } catch (_) {}
                            }
                        }
                    }
                }

                if (window.__teletanKillKeyboardShortcuts) {
                    try { count += Number(window.__teletanKillKeyboardShortcuts() || 0); } catch (_) {}
                }

                return count;
            }
            """
        )
        closed_total += int(closed or 0)
        closed_total += await suppress_keyboard_shortcuts_overlay(page)

        if closed_total:
            await asyncio.sleep(0.55)
        return int(closed_total or 0)
    except Exception:
        return int(closed_total or 0)


async def _close_toast_blocking_point(page: Page, x: float, y: float) -> int:
    """Close small TikTok toast/notification close buttons if they cover the Post button."""
    try:
        closed = await page.evaluate(
            r"""
            ({x, y}) => {
                const before = document.elementFromPoint(x, y);
                const beforeLabel = before
                    ? ((before.getAttribute('aria-label') || '') + ' ' + (before.innerText || before.textContent || '')).toLowerCase()
                    : '';

                // Only act when the exact click point is blocked by a toast/close button.
                if (!beforeLabel.includes('close toast') && !beforeLabel.includes('close')) {
                    return 0;
                }

                let count = 0;
                const nodes = [...document.querySelectorAll('button, [role="button"], div[tabindex="0"], span')];
                for (const el of nodes) {
                    const r = el.getBoundingClientRect();
                    if (!(r.width > 0 && r.height > 0)) continue;

                    const label = ((el.getAttribute('aria-label') || '') + ' ' + (el.innerText || el.textContent || '')).toLowerCase();
                    const nearPoint = x >= r.x - 5 && x <= r.x + r.width + 5 && y >= r.y - 5 && y <= r.y + r.height + 5;

                    if (nearPoint && (label.includes('close toast') || label.includes('close'))) {
                        try {
                            el.click();
                            count++;
                        } catch (_) {}
                    }
                }
                return count;
            }
            """,
            {"x": x, "y": y},
        )
        return int(closed or 0)
    except Exception:
        return 0


async def _element_from_point_diag(page: Page, x: float, y: float) -> Optional[Dict[str, Any]]:
    try:
        return await page.evaluate(
            r"""
            ({x, y}) => {
                const el = document.elementFromPoint(x, y);
                if (!el) return null;
                const r = el.getBoundingClientRect();
                const st = getComputedStyle(el);
                return {
                    tag: el.tagName,
                    text: (el.innerText || el.textContent || '').slice(0, 120),
                    e2e: el.getAttribute('data-e2e'),
                    smart: el.getAttribute('data-smart-id'),
                    aria: el.getAttribute('aria-label'),
                    role: el.getAttribute('role'),
                    x: Math.round(r.x),
                    y: Math.round(r.y),
                    w: Math.round(r.width),
                    h: Math.round(r.height),
                    pointerEvents: st.pointerEvents,
                    opacity: st.opacity,
                };
            }
            """,
            {"x": x, "y": y},
        )
    except Exception:
        return None


async def _clear_post_click_blockers(
    page: Page,
    post_button: Any,
    *,
    max_rounds: int = 5,
    reselect_enabled: bool = True,
) -> tuple[Any, Optional[dict[str, Any]], bool]:
    """Clear overlays blocking the center of Post button.

    Returns: (fresh_post_button, last_point_diag, is_clear)
    """
    last_diag = None
    for round_index in range(max_rounds):
        if reselect_enabled:
            try:
                fresh_button, fresh_info = await wait_for_post_button_enabled(page, max_wait_seconds=2.0)
                if fresh_button:
                    post_button = fresh_button
                    logger.info(
                        "[PUBLISH] Fresh Post button selected before click/clear via %s",
                        fresh_info.strategy,
                    )
            except Exception as fresh_exc:
                logger.warning("[PUBLISH] Fresh Post button reselect during blocker clear failed: %s", fresh_exc)

        box = await post_button.bounding_box()
        if not box:
            return post_button, last_diag, False

        x = box["x"] + box["width"] / 2
        y = box["y"] + box["height"] / 2
        last_diag = await _element_from_point_diag(page, x, y)
        logger.info(
            "[PUBLISH] elementFromPoint before Post click/clear round %s: %s",
            round_index + 1,
            json.dumps(last_diag, ensure_ascii=False)[:1000],
        )

        if isinstance(last_diag, dict):
            e2e = last_diag.get("e2e")
            smart = last_diag.get("smart")
            text = (last_diag.get("text") or "").lower()
            aria = (last_diag.get("aria") or "").lower()
            label = f"{aria} {text}".strip()

            if e2e == "comment-post" or smart == "comment-post-ready" or text.strip() == "post":
                return post_button, last_diag, True

            if _point_diag_looks_like_challenge(last_diag):
                logger.warning(
                    "[PUBLISH] Post click point is blocked by TikTok challenge; not modifying blocker: %s",
                    json.dumps(last_diag, ensure_ascii=False)[:1000],
                )
                return post_button, last_diag, False

            if _point_diag_looks_like_cookie_banner(last_diag):
                logger.warning("[PUBLISH] Post click point is blocked by cookie banner. Dismissing it.")
                dismissed = await dismiss_cookie_banners(page)
                logger.info("[PUBLISH] Cookie banner cleanup before Post click: %s", dismissed)
                await asyncio.sleep(0.45)
                continue

            if "close toast" in label or ("close" in label and "post" not in label):
                logger.warning("[PUBLISH] Post click point is blocked by toast/close element. Closing it.")
                closed_toasts = await _close_toast_blocking_point(page, x, y)
                logger.info("[PUBLISH] Closed toast elements before Post click: %s", closed_toasts)
                await asyncio.sleep(0.35)
                continue

            if "introducing keyboard shortcuts" in text or "browse your feed" in text:
                logger.warning("[PUBLISH] Post click point is blocked by TikTok onboarding overlay. Hard-closing it.")
                closed_keyboard = await _close_keyboard_shortcuts_overlay(page)
                logger.info("[PUBLISH] TikTok onboarding overlay closed before Post click: %s", closed_keyboard)
                await asyncio.sleep(0.35)
                continue

            # Last resort for bounded overlays covering Post: make exactly that blocker click-through.
            try:
                made_passthrough = await page.evaluate(
                    r"""
                    ({x, y}) => {
                        const el = document.elementFromPoint(x, y);
                        if (!el || el === document.body || el === document.documentElement) return 0;
                        const r = el.getBoundingClientRect();
                        const txt = (el.innerText || el.textContent || '').slice(0, 240);
                        const label = [
                            txt,
                            el.id || '',
                            String(el.className || ''),
                            el.getAttribute('aria-label') || '',
                            el.getAttribute('data-e2e') || '',
                            el.tagName || '',
                        ].join(' ').toLowerCase();
                        const cookieMarkers = [
                            'cookie',
                            'cookies',
                            'optional cookies',
                            'allow all',
                            'decline optional cookies',
                            'manage your choices',
                            'tiktok uses cookies',
                            'essential cookies',
                            'consent',
                            'gdpr',
                            '\u30af\u30c3\u30ad\u30fc',
                            '\ucfe0\ud0a4',
                            '\u997c\u5e72',
                            '\u96b1\u79c1',
                            '\u9690\u79c1',
                        ];
                        // Do not touch the real Post button.
                        if ((el.getAttribute('data-e2e') || '') === 'comment-post') return 0;
                        if ((el.getAttribute('data-smart-id') || '') === 'comment-post-ready') return 0;
                        const isCookieBanner = cookieMarkers.some(marker => label.includes(marker));
                        const isBottomBand = r.width > window.innerWidth * 0.55 && r.height > 70 && r.top > window.innerHeight * 0.55;
                        // Only bounded overlays, cookie bottom bands, or known blockers. Avoid breaking the whole app root.
                        const isTikTokOnboarding =
                            txt.includes('Introducing keyboard shortcuts') ||
                            label.includes('browse your feed');
                        if ((r.width <= 1000 && r.height <= 1000) || isTikTokOnboarding || isCookieBanner || isBottomBand) {
                            el.style.setProperty('pointer-events', 'none', 'important');
                            if (isCookieBanner || isBottomBand) {
                                el.style.setProperty('display', 'none', 'important');
                                el.style.setProperty('visibility', 'hidden', 'important');
                                el.style.setProperty('opacity', '0', 'important');
                                el.style.setProperty('z-index', '-1', 'important');
                            }
                            el.setAttribute('data-teletan-click-through', '1');
                            return 1;
                        }
                        return 0;
                    }
                    """,
                    {"x": x, "y": y},
                )
                if made_passthrough:
                    logger.warning("[PUBLISH] Made blocking element click-through before Post click: %s", made_passthrough)
                    await asyncio.sleep(0.25)
                    continue
            except Exception as pass_exc:
                logger.warning("[PUBLISH] click-through fallback failed: %s", pass_exc)

        await asyncio.sleep(0.2)

    return post_button, last_diag, False


_JS_MAIN_COMMENT_DIRECT_POST_CLICK = r"""
([expected]) => {
    const normalizeText = (value) => String(value || '').replace(/\s+/g, ' ').trim();
    const expectedText = normalizeText(expected);
    const placeholderMarkers = [
        'add comment',
        'add a comment',
        'say something',
        'leave a comment',
        '\u0434\u043e\u0431\u0430\u0432\u044c\u0442\u0435 \u043a\u043e\u043c\u043c\u0435\u043d\u0442\u0430\u0440',
        '\u043d\u0430\u043f\u0438\u0448\u0438\u0442\u0435 \u043a\u043e\u043c\u043c\u0435\u043d\u0442\u0430\u0440',
        '\u0434\u043e\u0434\u0430\u0442\u0438 \u043a\u043e\u043c\u0435\u043d\u0442\u0430\u0440'
    ];

    const visible = (el) => {
        if (!el || !el.getBoundingClientRect) return false;
        const r = el.getBoundingClientRect();
        const st = getComputedStyle(el);
        return r.width > 0 && r.height > 0 && st.display !== 'none' && st.visibility !== 'hidden';
    };
    const disabled = (el) => {
        if (!el) return true;
        const st = getComputedStyle(el);
        return el.hasAttribute('disabled') ||
            el.getAttribute('aria-disabled') === 'true' ||
            el.classList.contains('disabled') ||
            parseFloat(st.opacity || '1') < 0.45;
    };
    const isRealEditor = (el) => el && (
        el.isContentEditable ||
        (el.getAttribute && el.getAttribute('contenteditable') === 'true') ||
        el.tagName === 'TEXTAREA' ||
        el.tagName === 'INPUT' ||
        (el.getAttribute && el.getAttribute('role') === 'textbox')
    );
    const resolveInnerEditor = (raw) => {
        if (!raw) return null;
        if (isRealEditor(raw)) return raw;
        if (!raw.querySelector) return null;
        return raw.querySelector(
            'div[contenteditable="true"], [contenteditable="true"], textarea, input, [role="textbox"]'
        );
    };
    const readEditorText = (editor) => {
        if (!editor) return '';
        const raw = (editor.value !== undefined && editor.value !== null && editor.value !== '')
            ? editor.value
            : (editor.textContent || editor.innerText || '');
        return normalizeText(raw);
    };
    const inputWrappers = [
        document.querySelector('[data-smart-id="comment-input"]'),
        document.querySelector('[data-e2e="comment-input"]'),
        document.querySelector('[data-e2e*="comment-input"]'),
        document.querySelector('[data-e2e*="CommentInput"]'),
    ].filter(Boolean);
    let editor = null;
    for (const raw of inputWrappers) {
        const inner = resolveInnerEditor(raw);
        if (!inner) continue;
        if (!visible(inner) && !visible(raw) && !(inner.parentElement && visible(inner.parentElement))) continue;
        editor = inner;
        break;
    }
    if (!editor) {
        for (const raw of document.querySelectorAll(
            '[role="textbox"], div[contenteditable="true"], [contenteditable="true"], textarea, input'
        )) {
            if (!isRealEditor(raw) || !visible(raw)) continue;
            let scoped = false;
            let bad = false;
            for (let cur = raw, depth = 0; cur && cur !== document.body && depth < 8; cur = cur.parentElement, depth++) {
                const label = [
                    cur.getAttribute && (cur.getAttribute('data-e2e') || ''),
                    cur.getAttribute && (cur.getAttribute('aria-label') || ''),
                    cur.getAttribute && (cur.getAttribute('placeholder') || ''),
                    cur.getAttribute && (cur.getAttribute('data-placeholder') || ''),
                    String(cur.className || ''),
                    cur.id || '',
                ].join(' ').toLowerCase();
                if (label.includes('search') || label.includes('captcha') || label.includes('challenge') || label.includes('login')) {
                    bad = true;
                    break;
                }
                if (label.includes('comment')) scoped = true;
            }
            if (!bad && scoped) {
                editor = raw;
                break;
            }
        }
    }
    const inputText = readEditorText(editor);
    const lowerInput = inputText.toLowerCase();
    const placeholderLeak = placeholderMarkers.some(marker => lowerInput.startsWith(marker));
    if (!inputText || placeholderLeak || inputText !== expectedText) {
        return {
            clicked: false,
            reason: placeholderLeak ? 'input_placeholder_leak' : 'input_text_not_committed',
            inputText: inputText.slice(0, 120),
            expected: expectedText.slice(0, 120),
            editorFound: Boolean(editor),
        };
    }

    const clickTargetFor = (raw) => {
        if (!raw) return null;
        const candidates = [
            raw,
            raw.closest && raw.closest('button'),
            raw.closest && raw.closest('[role="button"]'),
            raw.closest && raw.closest('div[tabindex="0"]'),
            raw.parentElement,
            raw.parentElement && raw.parentElement.parentElement,
        ].filter(Boolean);
        const seen = new Set();
        for (const el of candidates) {
            if (!el || seen.has(el) || !visible(el)) continue;
            seen.add(el);
            const r = el.getBoundingClientRect();
            if (r.width > 360 || r.height > 160) continue;
            if (disabled(el)) continue;
            return el;
        }
        return null;
    };
    const rawButtons = [
        ...document.querySelectorAll('[data-e2e="comment-post"], [data-e2e*="comment-post"], [data-e2e="post-comment-button"]'),
        ...document.querySelectorAll('button, [role="button"], div[tabindex="0"]'),
    ];
    let post = null;
    let rawPost = null;
    for (const raw of rawButtons) {
        const label = [
            raw.getAttribute && (raw.getAttribute('data-e2e') || ''),
            raw.getAttribute && (raw.getAttribute('aria-label') || ''),
            raw.getAttribute && (raw.getAttribute('title') || ''),
            raw.innerText || raw.textContent || '',
        ].join(' ').replace(/\s+/g, ' ').trim().toLowerCase();
        const looksPost =
            label.includes('comment-post') ||
            label.includes('post') ||
            label.includes('send') ||
            label.includes('submit') ||
            label.includes('\u043e\u043f\u0443\u0431\u043b') ||
            label.includes('\u043e\u0442\u043f\u0440\u0430\u0432');
        if (!looksPost) continue;
        const target = clickTargetFor(raw);
        if (!target) continue;
        post = target;
        rawPost = raw;
        break;
    }
    if (!post) {
        const disabledPost = document.querySelector('[data-e2e="comment-post"], [data-e2e*="comment-post"]');
        return {
            clicked: false,
            reason: disabledPost ? 'post_button_disabled_or_blocked' : 'post_button_not_found',
            inputText: inputText.slice(0, 120),
            rawPostTag: disabledPost ? disabledPost.tagName : null,
            rawPostDisabled: disabledPost ? disabled(disabledPost) : null,
        };
    }

    try { post.setAttribute('data-smart-id', 'comment-post-ready'); } catch (_) {}
    try { post.scrollIntoView({block:'center', inline:'center'}); } catch (_) {}
    const r = post.getBoundingClientRect();
    const cx = r.left + r.width / 2;
    const cy = r.top + r.height / 2;
    const opts = {bubbles: true, cancelable: true, clientX: cx, clientY: cy, view: window};
    const prevPointerEvents = post.style.pointerEvents;
    try { post.style.setProperty('pointer-events', 'auto', 'important'); } catch (_) {}
    try { post.dispatchEvent(new PointerEvent('pointerdown', opts)); } catch (_) {}
    try { post.dispatchEvent(new MouseEvent('mousedown', opts)); } catch (_) {}
    try { post.dispatchEvent(new PointerEvent('pointerup', opts)); } catch (_) {}
    try { post.dispatchEvent(new MouseEvent('mouseup', opts)); } catch (_) {}
    try { post.dispatchEvent(new MouseEvent('click', opts)); } catch (_) {}
    try { post.style.pointerEvents = prevPointerEvents; } catch (_) {}

    return {
        clicked: true,
        postWasEnabled: true,
        strategy: 'main-direct-dom-post',
        inputText: inputText.slice(0, 120),
        targetTag: post.tagName,
        rawTag: rawPost ? rawPost.tagName : null,
        x: Math.round(cx),
        y: Math.round(cy),
    };
}
"""


_JS_MAIN_COMMENT_KEYBOARD_FOCUS_TARGET = r"""
() => {
    const visible = (el) => {
        if (!el || !el.getBoundingClientRect) return false;
        const r = el.getBoundingClientRect();
        const st = getComputedStyle(el);
        return r.width > 0 && r.height > 0 && st.display !== 'none' && st.visibility !== 'hidden';
    };
    const rectFor = (el) => {
        if (!el || !el.getBoundingClientRect) return null;
        const r = el.getBoundingClientRect();
        return {
            left: r.left,
            top: r.top,
            width: r.width,
            height: r.height,
            right: r.right,
            bottom: r.bottom,
        };
    };
    const isRealEditor = (el) => el && (
        el.isContentEditable ||
        (el.getAttribute && el.getAttribute('contenteditable') === 'true') ||
        el.tagName === 'TEXTAREA' ||
        el.tagName === 'INPUT' ||
        (el.getAttribute && el.getAttribute('role') === 'textbox')
    );
    const labelOf = (el) => {
        if (!el) return '';
        return [
            el.getAttribute && (el.getAttribute('data-e2e') || ''),
            el.getAttribute && (el.getAttribute('data-smart-id') || ''),
            el.getAttribute && (el.getAttribute('aria-label') || ''),
            el.getAttribute && (el.getAttribute('placeholder') || ''),
            el.getAttribute && (el.getAttribute('data-placeholder') || ''),
            el.id || '',
            String(el.className || ''),
            el.tagName || '',
        ].join(' ').toLowerCase();
    };
    const badMarkers = [
        'search', 'captcha', 'verification', 'security check',
        'challenge', 'login', 'log in', 'sign in', 'signup',
        'sign up', 'phone / email', 'email / username',
    ];
    const badSurface = (el) => {
        const authTokenLike = (value) => /(^|[^a-z])auth([^a-z]|$)/.test(String(value || ''));
        for (let cur = el, depth = 0; cur && cur !== document.body && depth < 8; cur = cur.parentElement, depth++) {
            const label = labelOf(cur);
            const text = (cur.innerText || cur.textContent || '').toLowerCase();
            if (
                label.includes('data-smart-reply') ||
                label.includes('smart-reply') ||
                label.includes('replycommenteditor') ||
                label.includes('reply-comment-editor')
            ) return true;
            if (authTokenLike(label) || authTokenLike(text)) return true;
            if (badMarkers.some(marker => label.includes(marker) || text.includes(marker))) return true;
        }
        return false;
    };
    const commentScoped = (el) => {
        for (let cur = el, depth = 0; cur && cur !== document.body && depth < 9; cur = cur.parentElement, depth++) {
            const label = labelOf(cur);
            if (badMarkers.some(marker => label.includes(marker))) return false;
            if (label.includes('comment')) return true;
        }
        return false;
    };
    const resolveEditor = (raw) => {
        if (!raw) return null;
        if (isRealEditor(raw)) return raw;
        if (!raw.querySelector) return null;
        return raw.querySelector(
            'div[contenteditable="true"], [contenteditable="true"], textarea, input, [role="textbox"]'
        );
    };
    const rawCandidates = [
        ...document.querySelectorAll(
            '[data-smart-id="comment-input"], [data-e2e="comment-input"], ' +
            '[data-e2e*="comment-input"], [data-e2e*="CommentInput"], ' +
            '[class*="comment-panel-input"], [class*="DivTextInputContainer"], ' +
            'div[contenteditable="true"], [contenteditable="true"], [role="textbox"], textarea'
        ),
    ];
    const scored = [];
    for (const raw of rawCandidates) {
        if (!raw || badSurface(raw)) continue;
        const editor = resolveEditor(raw) || raw;
        if (!isRealEditor(editor) || badSurface(editor)) continue;
        if (!commentScoped(raw) && !commentScoped(editor)) continue;

        const rawRect = rectFor(raw);
        const editorRect = rectFor(editor);
        const parent = raw.closest && raw.closest('[data-e2e*="comment" i], [class*="comment" i], [aria-label*="comment" i]');
        const parentRect = rectFor(parent);
        const clickRect = (
            rawRect && rawRect.width > 40 && rawRect.height > 10 ? rawRect :
            parentRect && parentRect.width > 40 && parentRect.height > 10 ? parentRect :
            editorRect
        );
        if (!clickRect || clickRect.width <= 20 || clickRect.height <= 4) continue;
        if (!visible(raw) && !visible(editor) && !(parent && visible(parent))) continue;
        if (clickRect.bottom < 0 || clickRect.top > window.innerHeight || clickRect.right < 0 || clickRect.left > window.innerWidth) continue;

        let score = 0;
        const label = `${labelOf(raw)} ${labelOf(editor)} ${labelOf(parent)}`;
        if (label.includes('data-smart-id comment-input') || label.includes('comment-input')) score += 50;
        if (label.includes('comment-panel-input')) score += 40;
        if (label.includes('drafteditor')) score += 20;
        if (clickRect.top > window.innerHeight * 0.45) score += 15;
        score += Math.round(clickRect.top / 100);
        scored.push({raw, editor, clickRect, score});
    }
    scored.sort((a, b) => b.score - a.score || b.clickRect.top - a.clickRect.top);
    const chosen = scored[0];
    if (!chosen) return {ok: false, reason: 'no_main_comment_input_target'};
    const {editor, clickRect} = chosen;
    try { editor.setAttribute('data-smart-id', 'comment-input'); } catch (_) {}
    try { editor.scrollIntoView({block: 'center', inline: 'nearest'}); } catch (_) {}
    try { editor.focus({preventScroll: true}); } catch (_) { try { editor.focus(); } catch (_) {} }
    try {
        const selection = window.getSelection && window.getSelection();
        if (selection && editor.isContentEditable) {
            const range = document.createRange();
            range.selectNodeContents(editor);
            range.collapse(false);
            selection.removeAllRanges();
            selection.addRange(range);
        }
    } catch (_) {}
    const x = Math.min(Math.max(clickRect.left + Math.max(12, Math.min(32, clickRect.width * 0.25)), 1), window.innerWidth - 2);
    const y = Math.min(Math.max(clickRect.top + clickRect.height / 2, 1), window.innerHeight - 2);
    return {
        ok: true,
        x: Math.round(x),
        y: Math.round(y),
        tag: editor.tagName,
        contenteditable: editor.getAttribute && editor.getAttribute('contenteditable'),
        role: editor.getAttribute && editor.getAttribute('role'),
        score: chosen.score,
        rect: {
            left: Math.round(clickRect.left),
            top: Math.round(clickRect.top),
            width: Math.round(clickRect.width),
            height: Math.round(clickRect.height),
        },
    };
}
"""


_JS_REPLY_SCOPED_TEXT_MATCH = r"""
([expected]) => {
    const normalizeText = (value) => String(value || '').replace(/\s+/g, ' ').trim();
    const expectedText = normalizeText(expected);
    const placeholderMarkers = [
        'add comment',
        'add a comment',
        'say something',
        'leave a comment',
        '\u0434\u043e\u0431\u0430\u0432\u044c\u0442\u0435 \u043a\u043e\u043c\u043c\u0435\u043d\u0442\u0430\u0440',
        '\u0434\u043e\u0434\u0430\u0442\u0438 \u0432\u0456\u0434\u043f\u043e\u0432\u0456\u0434\u044c'
    ];
    const input = document.querySelector('[data-smart-reply-input="1"]');
    if (!input) return { ok: false, reason: 'reply_input_missing', text: '' };
    const raw = (input.value !== undefined && input.value !== null && input.value !== '')
        ? input.value
        : (input.textContent || input.innerText || '');
    const text = normalizeText(raw);
    const lower = text.toLowerCase();
    const placeholderLeak = placeholderMarkers.some(marker => lower.startsWith(marker));
    return {
        ok: Boolean(text && !placeholderLeak && text === expectedText),
        reason: placeholderLeak ? 'placeholder_leak' : 'checked',
        text: text.slice(0, 160),
    };
}
"""


_JS_REPLY_DIRECT_POST_CLICK = r"""
([expected]) => {
    const normalizeText = (value) => String(value || '').replace(/\s+/g, ' ').trim();
    const expectedText = normalizeText(expected);
    const placeholderMarkers = [
        'add comment',
        'add a comment',
        'say something',
        'leave a comment',
        '\u0434\u043e\u0431\u0430\u0432\u044c\u0442\u0435 \u043a\u043e\u043c\u043c\u0435\u043d\u0442\u0430\u0440',
        '\u0434\u043e\u0434\u0430\u0442\u0438 \u0432\u0456\u0434\u043f\u043e\u0432\u0456\u0434\u044c'
    ];
    const visible = (el) => {
        if (!el || !el.getBoundingClientRect) return false;
        const r = el.getBoundingClientRect();
        const st = getComputedStyle(el);
        return r.width > 0 && r.height > 0 && st.display !== 'none' && st.visibility !== 'hidden';
    };
    const disabled = (el) => {
        if (!el) return true;
        const st = getComputedStyle(el);
        return el.hasAttribute('disabled') ||
            el.getAttribute('aria-disabled') === 'true' ||
            el.classList.contains('disabled') ||
            parseFloat(st.opacity || '1') < 0.45;
    };
    const input = document.querySelector('[data-smart-reply-input="1"]');
    if (!input) return { clicked: false, reason: 'reply_input_missing' };
    const composer = document.querySelector('[data-smart-reply-composer="1"]');
    if (!composer || !composer.contains(input) || !visible(composer)) {
        return { clicked: false, reason: 'reply_composer_missing' };
    }
    const rawText = (input.value !== undefined && input.value !== null && input.value !== '')
        ? input.value
        : (input.textContent || input.innerText || '');
    const inputText = normalizeText(rawText);
    const lowerInput = inputText.toLowerCase();
    const placeholderLeak = placeholderMarkers.some(marker => lowerInput.startsWith(marker));
    if (!inputText || placeholderLeak || inputText !== expectedText) {
        return {
            clicked: false,
            reason: placeholderLeak ? 'reply_input_placeholder_leak' : 'reply_input_text_not_committed',
            inputText: inputText.slice(0, 120),
            expected: expectedText.slice(0, 120),
        };
    }

    const clickTargetFor = (raw) => {
        if (!raw) return null;
        const candidates = [
            raw,
            raw.closest && raw.closest('button'),
            raw.closest && raw.closest('[role="button"]'),
            raw.closest && raw.closest('div[tabindex="0"]'),
            raw.parentElement,
            raw.parentElement && raw.parentElement.parentElement,
        ].filter(Boolean);
        const seen = new Set();
        for (const el of candidates) {
            if (!el || seen.has(el) || !visible(el)) continue;
            seen.add(el);
            if (!composer.contains(el)) continue;
            const r = el.getBoundingClientRect();
            if (r.width > 360 || r.height > 160) continue;
            if (disabled(el)) continue;
            return el;
        }
        return null;
    };
    const rawButtons = [
        ...composer.querySelectorAll('[data-e2e="comment-post"], [data-e2e*="comment-post"], [data-e2e="post-comment-button"]'),
        ...composer.querySelectorAll('button, [role="button"], div[tabindex="0"]'),
    ];
    let post = null;
    let rawPost = null;
    for (const raw of rawButtons) {
        const label = [
            raw.getAttribute && (raw.getAttribute('data-e2e') || ''),
            raw.getAttribute && (raw.getAttribute('aria-label') || ''),
            raw.getAttribute && (raw.getAttribute('title') || ''),
            raw.innerText || raw.textContent || '',
        ].join(' ').replace(/\s+/g, ' ').trim().toLowerCase();
        const looksPost =
            label.includes('comment-post') ||
            label.includes('post') ||
            label.includes('send') ||
            label.includes('submit') ||
            label.includes('\u043e\u043f\u0443\u0431\u043b') ||
            label.includes('\u043e\u0442\u043f\u0440\u0430\u0432');
        if (!looksPost) continue;
        const target = clickTargetFor(raw);
        if (!target) continue;
        post = target;
        rawPost = raw;
        break;
    }
    if (!post) {
        const disabledPost = composer.querySelector('[data-e2e="comment-post"], [data-e2e*="comment-post"]');
        return {
            clicked: false,
            reason: disabledPost ? 'reply_post_button_disabled_or_blocked' : 'reply_post_button_not_found',
            inputText: inputText.slice(0, 120),
            rawPostTag: disabledPost ? disabledPost.tagName : null,
            rawPostDisabled: disabledPost ? disabled(disabledPost) : null,
        };
    }

    try { post.setAttribute('data-smart-reply-post', '1'); } catch (_) {}
    try { post.setAttribute('data-smart-id', 'comment-post-ready'); } catch (_) {}
    try { post.scrollIntoView({block:'center', inline:'center'}); } catch (_) {}
    const r = post.getBoundingClientRect();
    const cx = r.left + r.width / 2;
    const cy = r.top + r.height / 2;
    const opts = {bubbles: true, cancelable: true, clientX: cx, clientY: cy, view: window};
    const prevPointerEvents = post.style.pointerEvents;
    try { post.style.setProperty('pointer-events', 'auto', 'important'); } catch (_) {}
    try { post.dispatchEvent(new PointerEvent('pointerdown', opts)); } catch (_) {}
    try { post.dispatchEvent(new MouseEvent('mousedown', opts)); } catch (_) {}
    try { post.dispatchEvent(new PointerEvent('pointerup', opts)); } catch (_) {}
    try { post.dispatchEvent(new MouseEvent('mouseup', opts)); } catch (_) {}
    try { post.dispatchEvent(new MouseEvent('click', opts)); } catch (_) {}
    try { post.style.pointerEvents = prevPointerEvents; } catch (_) {}

    return {
        clicked: true,
        postWasEnabled: true,
        strategy: 'reply-direct-dom-post',
        inputText: inputText.slice(0, 120),
        targetTag: post.tagName,
        rawTag: rawPost ? rawPost.tagName : null,
        x: Math.round(cx),
        y: Math.round(cy),
    };
}
"""


def _normalize_publish_text(value: str) -> str:
    return " ".join(str(value or "").split()).strip()


async def _reply_scoped_input_contains_expected_text(page: Page, text: str) -> tuple[bool, str]:
    try:
        result = await page.evaluate(_JS_REPLY_SCOPED_TEXT_MATCH, [text])
        if isinstance(result, dict):
            return bool(result.get("ok")), str(result.get("text") or "")
    except Exception:
        pass
    return False, ""


async def _main_comment_input_committed_text_state(
    page: Page,
    expected_text: Optional[str] = None,
) -> Dict[str, Any]:
    """Return visible top-level composer text without requiring a byte-exact match."""
    try:
        result = await page.evaluate(
            r"""
            ([expected]) => {
                const normalizeText = (value) => String(value || '').replace(/\s+/g, ' ').trim();
                const expectedText = normalizeText(expected);
                const placeholderMarkers = [
                    'add comment',
                    'add a comment',
                    'say something',
                    'leave a comment',
                    '\u0434\u043e\u0431\u0430\u0432\u044c\u0442\u0435 \u043a\u043e\u043c\u043c\u0435\u043d\u0442\u0430\u0440',
                    '\u043d\u0430\u043f\u0438\u0448\u0438\u0442\u0435 \u043a\u043e\u043c\u043c\u0435\u043d\u0442\u0430\u0440',
                    '\u0434\u043e\u0434\u0430\u0442\u0438 \u043a\u043e\u043c\u0435\u043d\u0442\u0430\u0440',
                    '\u0434\u043e\u0434\u0430\u0442\u0438 \u0432\u0456\u0434\u043f\u043e\u0432\u0456\u0434\u044c'
                ];
                const visible = (el) => {
                    if (!el || !el.getBoundingClientRect) return false;
                    const r = el.getBoundingClientRect();
                    const st = getComputedStyle(el);
                    return r.width > 0 && r.height > 0 &&
                        st.display !== 'none' && st.visibility !== 'hidden';
                };
                const disabled = (el) => {
                    if (!el) return true;
                    const st = getComputedStyle(el);
                    return el.hasAttribute('disabled') ||
                        el.getAttribute('aria-disabled') === 'true' ||
                        el.classList.contains('disabled') ||
                        getComputedStyle(el).pointerEvents === 'none' ||
                        parseFloat(st.opacity || '1') < 0.45;
                };
                const isRealEditor = (el) => el && (
                    el.isContentEditable ||
                    (el.getAttribute && el.getAttribute('contenteditable') === 'true') ||
                    el.tagName === 'TEXTAREA' ||
                    el.tagName === 'INPUT' ||
                    (el.getAttribute && el.getAttribute('role') === 'textbox')
                );
                const labelOf = (el) => [
                    el && el.getAttribute && (el.getAttribute('data-e2e') || ''),
                    el && el.getAttribute && (el.getAttribute('aria-label') || ''),
                    el && el.getAttribute && (el.getAttribute('placeholder') || ''),
                    el && el.getAttribute && (el.getAttribute('data-placeholder') || ''),
                    el && String(el.className || ''),
                    el && (el.id || ''),
                    el && (el.tagName || ''),
                ].join(' ').toLowerCase();
                const isCommentScoped = (el) => {
                    let cur = el;
                    for (let depth = 0; depth < 8 && cur && cur !== document.body; depth++, cur = cur.parentElement) {
                        const label = labelOf(cur);
                        if (
                            label.includes('search') ||
                            label.includes('captcha') ||
                            label.includes('challenge') ||
                            label.includes('login') ||
                            label.includes('sign in') ||
                            label.includes('signup')
                        ) return false;
                        if (label.includes('comment')) return true;
                    }
                    return false;
                };
                const readEditorText = (editor) => {
                    if (!editor) return '';
                    const raw = (editor.value !== undefined && editor.value !== null && editor.value !== '')
                        ? editor.value
                        : (editor.textContent || editor.innerText || '');
                    return normalizeText(raw);
                };
                const textMatchesExpected = (actual, expectedValue) => {
                    if (!expectedValue) return true;
                    const actualLower = normalizeText(actual).toLowerCase();
                    const expectedLower = normalizeText(expectedValue).toLowerCase();
                    if (!actualLower || !expectedLower) return false;
                    return actualLower === expectedLower;
                };
                const innerEditor = (raw) => {
                    if (!raw) return null;
                    if (isRealEditor(raw)) return raw;
                    if (!raw.querySelector) return null;
                    return raw.querySelector(
                        'div[contenteditable="true"], [contenteditable="true"], textarea, input, [role="textbox"]'
                    );
                };
                const editors = [];
                const wrappers = [
                    ...document.querySelectorAll('[data-smart-id="comment-input"]'),
                    ...document.querySelectorAll('[data-e2e="comment-input"]'),
                    ...document.querySelectorAll('[data-e2e*="comment-input"]'),
                    ...document.querySelectorAll('[data-e2e*="CommentInput"]'),
                ];
                for (const wrapper of wrappers) {
                    const editor = innerEditor(wrapper);
                    if (editor && (visible(editor) || visible(wrapper) || visible(editor.parentElement))) {
                        editors.push(editor);
                    }
                }
                for (const editor of document.querySelectorAll(
                    'div[contenteditable="true"], [contenteditable="true"], [role="textbox"], textarea, input'
                )) {
                    if (!isRealEditor(editor) || !visible(editor) || !isCommentScoped(editor)) continue;
                    editors.push(editor);
                }

                const seen = new Set();
                for (const editor of editors) {
                    if (!editor || seen.has(editor)) continue;
                    seen.add(editor);
                    const text = readEditorText(editor);
                    const lower = text.toLowerCase();
                    const placeholderLeak = placeholderMarkers.some(
                        marker => lower.startsWith(marker) || lower.includes('add comment...')
                    );
                    if (!text || placeholderLeak) continue;
                    if (!textMatchesExpected(text, expectedText)) {
                        return {
                            ok: false,
                            reason: 'text_mismatch',
                            text,
                            textLen: text.length,
                            expected: expectedText.slice(0, 180),
                        };
                    }
                    const post = document.querySelector(
                        '[data-smart-id="comment-post-ready"], [data-smart-id="comment-post"], ' +
                        '[data-e2e="comment-post"], [data-e2e*="comment-post"], [data-e2e="post-comment-button"]'
                    );
                    return {
                        ok: true,
                        text,
                        textLen: text.length,
                        postVisible: visible(post),
                        postDisabled: post ? disabled(post) : null,
                        editorTag: editor.tagName,
                        editorE2e: editor.getAttribute && editor.getAttribute('data-e2e'),
                        editorSmart: editor.getAttribute && editor.getAttribute('data-smart-id'),
                    };
                }
                return { ok: false, reason: editors.length ? 'empty_or_placeholder' : 'editor_not_found' };
            }
            """,
            [expected_text or ""],
        )
        if isinstance(result, dict):
            return result
    except Exception as exc:
        logger.debug("[PUBLISH] main composer text-state probe failed: %s", str(exc)[:120])
    return {"ok": False, "reason": "probe_exception"}


async def _type_main_comment_by_mouse_keyboard(page: Page, text: str, *, fast: bool = True) -> dict[str, Any]:
    """Type a top-level comment through a real focused editor path.

    TikTok's DraftEditor can show DOM text while its React state still thinks
    the composer is empty. For main comments we prefer a mouse click plus
    keyboard insertion before any DOM-setting fallback, so the Post button is
    enabled by the same event path a human uses.
    """
    try:
        target = await page.evaluate(_JS_MAIN_COMMENT_KEYBOARD_FOCUS_TARGET)
    except Exception as exc:
        return {"ok": False, "reason": f"focus_probe_failed:{str(exc)[:80]}"}
    if not isinstance(target, dict) or not target.get("ok"):
        reason = str(target.get("reason") if isinstance(target, dict) else "focus_target_missing")
        return {"ok": False, "reason": reason}

    try:
        await page.mouse.click(float(target.get("x") or 0), float(target.get("y") or 0))
        await asyncio.sleep(0.08 if fast else 0.14)
        await suppress_keyboard_shortcuts_overlay(page)
        await page.keyboard.press("Control+A")
        await page.keyboard.press("Backspace")
        await asyncio.sleep(0.04 if fast else 0.08)
        await page.keyboard.insert_text(text)
        await asyncio.sleep(0.18 if fast else 0.30)
        ok, found_text = await comment_input_contains_expected_text(page, text)
        if ok:
            return {
                "ok": True,
                "strategy": "main-direct-mouse-keyboard",
                "text": found_text,
            }

        # A tiny trusted nudge often makes DraftEditor flush selection/input
        # state when Chromium is under load and the text is already visible.
        try:
            await page.keyboard.press("End")
            await page.keyboard.insert_text(" ")
            await page.keyboard.press("Backspace")
            await asyncio.sleep(0.12 if fast else 0.20)
            ok, found_text = await comment_input_contains_expected_text(page, text)
            if ok:
                return {
                    "ok": True,
                    "strategy": "main-direct-mouse-keyboard-nudged",
                    "text": found_text,
                }
        except Exception as nudge_exc:
            logger.debug("[PUBLISH] Main keyboard nudge failed: %s", str(nudge_exc)[:120])

        return {
            "ok": False,
            "reason": "keyboard_text_not_verified",
            "last_text": found_text[:120],
            "target": {
                "tag": target.get("tag"),
                "role": target.get("role"),
                "contenteditable": target.get("contenteditable"),
                "rect": target.get("rect"),
            },
        }
    except Exception as exc:
        return {"ok": False, "reason": f"keyboard_type_failed:{str(exc)[:100]}"}


async def _type_main_comment_without_handle(page: Page, text: str, *, fast: bool = True) -> dict[str, Any]:
    """Type a top-level comment without first resolving an ElementHandle.

    This is intentionally main-comment only. Prefer a human-like click/focus
    and keyboard insert; fall back to DOM-assisted input only if that cannot
    resolve the visible composer.
    """
    if not text:
        return {"ok": False, "reason": "empty_text"}

    keyboard_result = await _type_main_comment_by_mouse_keyboard(page, text, fast=fast)
    if isinstance(keyboard_result, dict) and keyboard_result.get("ok"):
        return keyboard_result
    keyboard_reason = str(keyboard_result.get("reason") if isinstance(keyboard_result, dict) else "keyboard_failed")
    logger.debug("[PUBLISH] Direct main keyboard typing did not verify: %s", keyboard_reason[:160])

    focus_result = await atomic_set_comment_text(
        page,
        text,
        reply_scoped=False,
        focus_only=True,
    )
    if isinstance(focus_result, dict) and focus_result.get("ok"):
        try:
            await suppress_keyboard_shortcuts_overlay(page)
            await page.keyboard.press("Control+A")
            await page.keyboard.press("Backspace")
            await page.keyboard.insert_text(text)
            await asyncio.sleep(0.12 if fast else 0.22)
            ok, found_text = await comment_input_contains_expected_text(page, text)
            if ok:
                return {
                    "ok": True,
                    "strategy": "main-direct-focus-insert-text",
                    "text": found_text,
                }
        except Exception as exc:
            logger.debug("[PUBLISH] Direct main focus+insert_text failed: %s", str(exc)[:120])

    return {
        "ok": False,
        "reason": str(
            (focus_result or {}).get("reason")
            if isinstance(focus_result, dict)
            else keyboard_reason or "typing_failed"
        ),
        "last_text": str(
            (focus_result or {}).get("text")
            if isinstance(focus_result, dict)
            else keyboard_result.get("last_text", "") if isinstance(keyboard_result, dict) else ""
        )[:120],
    }


async def _type_reply_comment_without_handle(
    page: Page,
    text: str,
    *,
    parent_anchor_token: Optional[str],
    fast: bool = True,
) -> dict[str, Any]:
    """Type a reply without first resolving an ElementHandle.

    Safe only when a parent anchor exists. The embedded atomic selector is
    reply-scoped and refuses to fall back to the global composer.
    """
    if not text:
        return {"ok": False, "reason": "empty_text"}
    if not parent_anchor_token:
        return {"ok": False, "reason": "missing_parent_anchor"}

    prefix = text[:-1]
    tail = text[-1:]
    result = await atomic_set_comment_text(
        page,
        text,
        reply_scoped=True,
        anchor_token=parent_anchor_token,
        insert_text=prefix,
    )
    if isinstance(result, dict) and result.get("reason") in {
        "no_editor",
        "reply_marked_input_missing",
        "reply_input_not_scoped_to_parent",
        "parent_anchor_not_found",
    }:
        return {"ok": False, "reason": str(result.get("reason") or "no_editor")}

    landed = str(result.get("text") or "") if isinstance(result, dict) else ""
    if prefix and len(prefix) <= 120 and _normalize_publish_text(landed) != _normalize_publish_text(prefix):
        logger.debug(
            "[PUBLISH] Direct reply prefix did not land cleanly: reason=%s text='%s'",
            result.get("reason") if isinstance(result, dict) else "non_dict_result",
            landed[:60],
        )
    else:
        try:
            await suppress_keyboard_shortcuts_overlay(page)
            await page.keyboard.insert_text(tail)
            await asyncio.sleep(0.10 if fast else 0.18)
            ok, found_text = await _reply_scoped_input_contains_expected_text(page, text)
            if ok:
                return {
                    "ok": True,
                    "strategy": "reply-direct-atomic-prefix-tail",
                    "text": found_text,
                }
        except Exception as exc:
            logger.debug("[PUBLISH] Direct reply trusted tail failed: %s", str(exc)[:120])

    focus_result = await atomic_set_comment_text(
        page,
        text,
        reply_scoped=True,
        anchor_token=parent_anchor_token,
        focus_only=True,
    )
    if isinstance(focus_result, dict) and focus_result.get("ok"):
        try:
            await suppress_keyboard_shortcuts_overlay(page)
            await page.keyboard.press("Control+A")
            await page.keyboard.press("Backspace")
            await page.keyboard.insert_text(text)
            await asyncio.sleep(0.12 if fast else 0.22)
            ok, found_text = await _reply_scoped_input_contains_expected_text(page, text)
            if ok:
                return {
                    "ok": True,
                    "strategy": "reply-direct-focus-insert-text",
                    "text": found_text,
                }
        except Exception as exc:
            logger.debug("[PUBLISH] Direct reply focus+insert_text failed: %s", str(exc)[:120])

    full_result = await atomic_set_comment_text(
        page,
        text,
        reply_scoped=True,
        anchor_token=parent_anchor_token,
    )
    if isinstance(full_result, dict) and full_result.get("ok"):
        ok, found_text = await _reply_scoped_input_contains_expected_text(page, text)
        if ok:
            return {
                "ok": True,
                "strategy": "reply-direct-atomic-full",
                "text": found_text,
            }
    return {
        "ok": False,
        "reason": str((full_result or {}).get("reason") if isinstance(full_result, dict) else "typing_failed"),
        "last_text": str((full_result or {}).get("text") if isinstance(full_result, dict) else "")[:120],
    }


async def _publish_main_comment_direct_dom(
    page: Page,
    text: str,
    *,
    started: float,
    max_total_seconds: float,
    expected_video_url: Optional[str],
    correlation_id: Optional[str],
    previous_reason: Optional[str],
) -> Optional[PublishResult]:
    """Main-comment fallback for visible composers when handle lookup stalls."""

    def _remaining() -> float:
        return max_total_seconds - (time.monotonic() - started)

    if _remaining() <= 1.0:
        return None

    _log_publish_trace(
        "main_direct_dom_fallback_start",
        page=page,
        expected_video_url=expected_video_url,
        correlation_id=correlation_id,
        previous_reason=previous_reason,
    )

    type_timeout = min(5.0, max(0.8, _remaining() - 1.2))
    try:
        type_result = await _asyncio_wait_for(
            _type_main_comment_without_handle(page, text, fast=True),
            timeout=type_timeout,
        )
    except asyncio.TimeoutError:
        await _clear_main_comment_composer_best_effort(
            page,
            reason="main_direct_typing_timeout",
            expected_video_url=expected_video_url,
            correlation_id=correlation_id,
        )
        _log_publish_trace(
            "main_direct_dom_fallback_typing_failed",
            page=page,
            expected_video_url=expected_video_url,
            correlation_id=correlation_id,
            reason="typing_timeout",
            level=logging.WARNING,
        )
        return None
    if not isinstance(type_result, dict) or not type_result.get("ok"):
        reason = str(type_result.get("reason") if isinstance(type_result, dict) else "typing_failed")
        await _clear_main_comment_composer_best_effort(
            page,
            reason=f"main_direct_typing_failed:{reason}"[:160],
            expected_video_url=expected_video_url,
            correlation_id=correlation_id,
        )
        _log_publish_trace(
            "main_direct_dom_fallback_typing_failed",
            page=page,
            expected_video_url=expected_video_url,
            correlation_id=correlation_id,
            reason=reason,
            level=logging.WARNING,
        )
        return None

    strategy = str(type_result.get("strategy") or "main-direct-dom")
    logger.info(
        "[PUBLISH] Main direct DOM fallback typed via %s for %s",
        strategy,
        correlation_id or "unknown",
    )
    _log_publish_trace(
        "main_direct_dom_fallback_typed",
        page=page,
        expected_video_url=expected_video_url,
        correlation_id=correlation_id,
        strategy=strategy,
        input_text_len=len(str(type_result.get("text") or "")),
    )

    captured: Dict[str, Any] = {}
    event = asyncio.Event()
    listener_tasks = set()

    def _consume_listener_task(task: asyncio.Task) -> None:
        listener_tasks.discard(task)
        try:
            task.exception()
        except asyncio.CancelledError:
            pass
        except Exception:
            pass

    async def _capture_publish_response(response: Response) -> None:
        try:
            url = response.url
        except Exception:
            return
        if "/api/comment/publish/" not in url:
            return
        try:
            captured["url"] = url
            captured["status"] = response.status
            body = await response.text()
            captured["body"] = body
            try:
                captured["json"] = json.loads(body) if body else None
            except Exception:
                captured["json"] = None
        except Exception as exc:
            captured["body_error"] = str(exc)[:100]
        finally:
            event.set()

    def on_response(response: Response):
        try:
            url = response.url
        except Exception:
            return
        if "/api/comment/publish/" not in url:
            return
        task = asyncio.create_task(_capture_publish_response(response))
        listener_tasks.add(task)
        task.add_done_callback(_consume_listener_task)

    def on_request(request):
        try:
            url = request.url
        except Exception:
            return
        if "/api/comment/publish/" not in url:
            return
        captured["request_url"] = url
        try:
            captured["request_method"] = request.method
        except Exception:
            pass
        event.set()

    page.on("request", on_request)
    page.on("response", on_response)
    listener_removed = False

    def _detach_listener() -> None:
        nonlocal listener_removed
        if listener_removed:
            return
        try:
            page.remove_listener("request", on_request)
        except Exception:
            pass
        try:
            page.remove_listener("response", on_response)
        except Exception:
            pass
        for task in tuple(listener_tasks):
            if not task.done():
                task.cancel()
        listener_removed = True

    try:
        if _remaining() <= 0:
            _detach_listener()
            return _publish_deadline_result(
                phase="main_direct_before_post_click",
                started=started,
                page=page,
                expected_video_url=expected_video_url,
                correlation_id=correlation_id,
                dom_strategy=strategy,
            )
        try:
            click_result = await _asyncio_wait_for(
                page.evaluate(_JS_MAIN_COMMENT_DIRECT_POST_CLICK, [text]),
                timeout=min(1.4, max(0.3, _remaining())),
            )
        except asyncio.TimeoutError:
            _detach_listener()
            return _publish_deadline_result(
                phase="main_direct_post_click",
                started=started,
                page=page,
                expected_video_url=expected_video_url,
                correlation_id=correlation_id,
                dom_strategy=strategy,
            )
        if not isinstance(click_result, dict) or not click_result.get("clicked"):
            reason = str(click_result.get("reason") if isinstance(click_result, dict) else "click_failed")
            if any(marker in reason for marker in ("disabled", "input", "empty", "not_found", "not_ready")):
                await _clear_main_comment_composer_best_effort(
                    page,
                    reason=f"main_direct_post_click_failed:{reason}"[:160],
                    expected_video_url=expected_video_url,
                    correlation_id=correlation_id,
                )
            _log_publish_trace(
                "publish_result",
                page=page,
                expected_video_url=expected_video_url,
                correlation_id=correlation_id,
                status="dom_error",
                reason=f"main_direct_post_click_failed:{reason}",
                dom_strategy=strategy,
                elapsed=round(time.monotonic() - started, 2),
                level=logging.WARNING,
            )
            return PublishResult(
                status="dom_error",
                error_reason=f"main_direct_post_click_failed:{reason}",
                elapsed_seconds=time.monotonic() - started,
                dom_strategy=strategy,
                network_request_attempted=False,
                post_was_enabled_at_click=False,
            )

        post_was_enabled = bool(click_result.get("postWasEnabled"))
        click_strategy = str(click_result.get("strategy") or "main-direct-dom-post")
        combined_strategy = f"{strategy}+{click_strategy}"
        logger.info(
            "[PUBLISH] Main direct DOM fallback clicked Post via %s for %s",
            click_strategy,
            correlation_id or "unknown",
        )
        _log_publish_trace(
            "main_direct_dom_fallback_clicked",
            page=page,
            expected_video_url=expected_video_url,
            correlation_id=correlation_id,
            strategy=combined_strategy,
            post_was_enabled=post_was_enabled,
        )

        wait_remaining = _remaining()
        if wait_remaining > 0 and not event.is_set():
            try:
                await asyncio.wait_for(
                    event.wait(),
                    timeout=min(_PARALLEL_POST_RESPONSE_WAIT_SECONDS, max(wait_remaining, 0.3)),
                )
            except asyncio.TimeoutError:
                pass
        if captured.get("request_url") and not captured.get("url"):
            grace = min(_PARALLEL_POST_RESPONSE_GRACE_SECONDS, max(_remaining(), 0.0))
            if grace > 0:
                await asyncio.sleep(grace)

        if captured.get("url"):
            elapsed = time.monotonic() - started
            body_preview = (captured.get("body") or "")[:200].replace("\n", " ")
            response_json = captured.get("json") if isinstance(captured.get("json"), dict) else None
            comment_id = _extract_comment_id(response_json)
            if comment_id:
                _log_publish_trace(
                    "publish_result",
                    page=page,
                    expected_video_url=expected_video_url,
                    correlation_id=correlation_id,
                    status="success",
                    comment_id=comment_id,
                    network_status=captured.get("status"),
                    post_was_enabled=post_was_enabled,
                    elapsed=round(elapsed, 2),
                )
                return PublishResult(
                    status="success",
                    comment_id=comment_id,
                    network_captured=True,
                    network_request_attempted=True,
                    network_status_code=captured.get("status"),
                    network_body_preview=body_preview,
                    network_response_json=response_json,
                    elapsed_seconds=elapsed,
                    dom_strategy=combined_strategy,
                    post_was_enabled_at_click=post_was_enabled,
                )
            return PublishResult(
                status="failed",
                error_reason="main_direct_api_response_without_comment_id",
                network_captured=True,
                network_request_attempted=True,
                network_status_code=captured.get("status"),
                network_body_preview=body_preview,
                network_response_json=response_json,
                elapsed_seconds=elapsed,
                dom_strategy=combined_strategy,
                post_was_enabled_at_click=post_was_enabled,
            )

        cleared: Dict[str, Any] = {}
        if _remaining() > 0:
            try:
                cleared = await _asyncio_wait_for(
                    _composer_cleared_state(page, reply_scoped=False),
                    timeout=min(0.9, max(_remaining(), 0.2)),
                )
            except asyncio.TimeoutError:
                cleared = {}
            except Exception:
                cleared = {}
        request_attempted = bool(captured.get("request_url"))
        elapsed = time.monotonic() - started
        if post_was_enabled and cleared.get("exists") and cleared.get("empty"):
            _log_publish_trace(
                "publish_result",
                page=page,
                expected_video_url=expected_video_url,
                correlation_id=correlation_id,
                status="success",
                reason="main_direct_composer_cleared_after_submit",
                request_attempted=request_attempted,
                post_was_enabled=post_was_enabled,
                elapsed=round(elapsed, 2),
            )
            return PublishResult(
                status="success",
                network_captured=False,
                network_request_attempted=request_attempted,
                elapsed_seconds=elapsed,
                dom_strategy=combined_strategy,
                post_was_enabled_at_click=post_was_enabled,
            )
        error_reason = (
            "no_network_response_after_main_direct_post_click"
            if request_attempted
            else "no_network_request_after_main_direct_post_click"
        )
        if not request_attempted:
            await _clear_main_comment_composer_best_effort(
                page,
                reason=error_reason,
                expected_video_url=expected_video_url,
                correlation_id=correlation_id,
            )
        _log_publish_trace(
            "publish_result",
            page=page,
            expected_video_url=expected_video_url,
            correlation_id=correlation_id,
            status="timeout",
            reason=error_reason,
            request_attempted=request_attempted,
            post_was_enabled=post_was_enabled,
            elapsed=round(elapsed, 2),
            level=logging.WARNING,
        )
        return PublishResult(
            status="timeout",
            error_reason=error_reason,
            network_captured=False,
            network_request_attempted=request_attempted,
            elapsed_seconds=elapsed,
            dom_strategy=combined_strategy,
            post_was_enabled_at_click=post_was_enabled,
        )
    finally:
        _detach_listener()


async def _publish_reply_comment_direct_dom(
    page: Page,
    text: str,
    *,
    started: float,
    max_total_seconds: float,
    expected_video_url: Optional[str],
    correlation_id: Optional[str],
    parent_anchor_token: Optional[str],
    previous_reason: Optional[str],
) -> Optional[PublishResult]:
    """Reply fallback for visible scoped composers when handle lookup stalls."""

    def _remaining() -> float:
        return max_total_seconds - (time.monotonic() - started)

    if not parent_anchor_token or _remaining() <= 1.0:
        return None

    _log_publish_trace(
        "reply_direct_dom_fallback_start",
        page=page,
        expected_video_url=expected_video_url,
        correlation_id=correlation_id,
        previous_reason=previous_reason,
    )

    type_timeout = min(3.2, max(0.5, _remaining() - 1.2))
    try:
        type_result = await _asyncio_wait_for(
            _type_reply_comment_without_handle(
                page,
                text,
                parent_anchor_token=parent_anchor_token,
                fast=True,
            ),
            timeout=type_timeout,
        )
    except asyncio.TimeoutError:
        _log_publish_trace(
            "reply_direct_dom_fallback_typing_failed",
            page=page,
            expected_video_url=expected_video_url,
            correlation_id=correlation_id,
            reason="typing_timeout",
            level=logging.WARNING,
        )
        return None
    if not isinstance(type_result, dict) or not type_result.get("ok"):
        reason = str(type_result.get("reason") if isinstance(type_result, dict) else "typing_failed")
        _log_publish_trace(
            "reply_direct_dom_fallback_typing_failed",
            page=page,
            expected_video_url=expected_video_url,
            correlation_id=correlation_id,
            reason=reason,
            level=logging.WARNING,
        )
        return None

    strategy = str(type_result.get("strategy") or "reply-direct-dom")
    logger.info(
        "[PUBLISH] Reply direct DOM fallback typed via %s for %s",
        strategy,
        correlation_id or "unknown",
    )
    _log_publish_trace(
        "reply_direct_dom_fallback_typed",
        page=page,
        expected_video_url=expected_video_url,
        correlation_id=correlation_id,
        strategy=strategy,
        input_text_len=len(str(type_result.get("text") or "")),
    )

    captured: Dict[str, Any] = {}
    event = asyncio.Event()
    listener_tasks = set()

    def _consume_listener_task(task: asyncio.Task) -> None:
        listener_tasks.discard(task)
        try:
            task.exception()
        except asyncio.CancelledError:
            pass
        except Exception:
            pass

    async def _capture_publish_response(response: Response) -> None:
        try:
            url = response.url
        except Exception:
            return
        if "/api/comment/publish/" not in url:
            return
        try:
            captured["url"] = url
            captured["status"] = response.status
            body = await response.text()
            captured["body"] = body
            try:
                captured["json"] = json.loads(body) if body else None
            except Exception:
                captured["json"] = None
        except Exception as exc:
            captured["body_error"] = str(exc)[:100]
        finally:
            event.set()

    def on_response(response: Response):
        try:
            url = response.url
        except Exception:
            return
        if "/api/comment/publish/" not in url:
            return
        task = asyncio.create_task(_capture_publish_response(response))
        listener_tasks.add(task)
        task.add_done_callback(_consume_listener_task)

    def on_request(request):
        try:
            url = request.url
        except Exception:
            return
        if "/api/comment/publish/" not in url:
            return
        captured["request_url"] = url
        try:
            captured["request_method"] = request.method
        except Exception:
            pass
        event.set()

    page.on("request", on_request)
    page.on("response", on_response)
    listener_removed = False

    def _detach_listener() -> None:
        nonlocal listener_removed
        if listener_removed:
            return
        try:
            page.remove_listener("request", on_request)
        except Exception:
            pass
        try:
            page.remove_listener("response", on_response)
        except Exception:
            pass
        for task in tuple(listener_tasks):
            if not task.done():
                task.cancel()
        listener_removed = True

    try:
        if _remaining() <= 0:
            _detach_listener()
            return _publish_deadline_result(
                phase="reply_direct_before_post_click",
                started=started,
                page=page,
                expected_video_url=expected_video_url,
                correlation_id=correlation_id,
                dom_strategy=strategy,
            )
        try:
            click_result = await _asyncio_wait_for(
                page.evaluate(_JS_REPLY_DIRECT_POST_CLICK, [text]),
                timeout=min(1.4, max(0.3, _remaining())),
            )
        except asyncio.TimeoutError:
            _detach_listener()
            return _publish_deadline_result(
                phase="reply_direct_post_click",
                started=started,
                page=page,
                expected_video_url=expected_video_url,
                correlation_id=correlation_id,
                dom_strategy=strategy,
            )
        if not isinstance(click_result, dict) or not click_result.get("clicked"):
            reason = str(click_result.get("reason") if isinstance(click_result, dict) else "click_failed")
            _log_publish_trace(
                "publish_result",
                page=page,
                expected_video_url=expected_video_url,
                correlation_id=correlation_id,
                status="dom_error",
                reason=f"reply_direct_post_click_failed:{reason}",
                dom_strategy=strategy,
                elapsed=round(time.monotonic() - started, 2),
                level=logging.WARNING,
            )
            return PublishResult(
                status="dom_error",
                error_reason=f"reply_direct_post_click_failed:{reason}",
                elapsed_seconds=time.monotonic() - started,
                dom_strategy=strategy,
                network_request_attempted=False,
                post_was_enabled_at_click=False,
            )

        post_was_enabled = bool(click_result.get("postWasEnabled"))
        click_strategy = str(click_result.get("strategy") or "reply-direct-dom-post")
        combined_strategy = f"{strategy}+{click_strategy}"
        logger.info(
            "[PUBLISH] Reply direct DOM fallback clicked Post via %s for %s",
            click_strategy,
            correlation_id or "unknown",
        )
        _log_publish_trace(
            "reply_direct_dom_fallback_clicked",
            page=page,
            expected_video_url=expected_video_url,
            correlation_id=correlation_id,
            strategy=combined_strategy,
            post_was_enabled=post_was_enabled,
        )

        wait_remaining = _remaining()
        if wait_remaining > 0 and not event.is_set():
            try:
                await asyncio.wait_for(
                    event.wait(),
                    timeout=min(_PARALLEL_POST_RESPONSE_WAIT_SECONDS, max(wait_remaining, 0.3)),
                )
            except asyncio.TimeoutError:
                pass
        if captured.get("request_url") and not captured.get("url"):
            grace = min(_PARALLEL_POST_RESPONSE_GRACE_SECONDS, max(_remaining(), 0.0))
            if grace > 0:
                await asyncio.sleep(grace)

        if captured.get("url"):
            elapsed = time.monotonic() - started
            body_preview = (captured.get("body") or "")[:200].replace("\n", " ")
            response_json = captured.get("json") if isinstance(captured.get("json"), dict) else None
            comment_id = _extract_comment_id(response_json)
            if comment_id:
                _log_publish_trace(
                    "publish_result",
                    page=page,
                    expected_video_url=expected_video_url,
                    correlation_id=correlation_id,
                    status="success",
                    comment_id=comment_id,
                    network_status=captured.get("status"),
                    post_was_enabled=post_was_enabled,
                    elapsed=round(elapsed, 2),
                )
                return PublishResult(
                    status="success",
                    comment_id=comment_id,
                    network_captured=True,
                    network_request_attempted=True,
                    network_status_code=captured.get("status"),
                    network_body_preview=body_preview,
                    network_response_json=response_json,
                    elapsed_seconds=elapsed,
                    dom_strategy=combined_strategy,
                    post_was_enabled_at_click=post_was_enabled,
                )
            return PublishResult(
                status="failed",
                error_reason="reply_direct_api_response_without_comment_id",
                network_captured=True,
                network_request_attempted=True,
                network_status_code=captured.get("status"),
                network_body_preview=body_preview,
                network_response_json=response_json,
                elapsed_seconds=elapsed,
                dom_strategy=combined_strategy,
                post_was_enabled_at_click=post_was_enabled,
            )

        cleared: Dict[str, Any] = {}
        if _remaining() > 0:
            try:
                cleared = await _asyncio_wait_for(
                    _composer_cleared_state(page, reply_scoped=True),
                    timeout=min(0.9, max(_remaining(), 0.2)),
                )
            except asyncio.TimeoutError:
                cleared = {}
            except Exception:
                cleared = {}
        request_attempted = bool(captured.get("request_url"))
        elapsed = time.monotonic() - started
        if post_was_enabled and cleared.get("exists") and cleared.get("empty"):
            _log_publish_trace(
                "publish_result",
                page=page,
                expected_video_url=expected_video_url,
                correlation_id=correlation_id,
                status="success",
                reason="reply_direct_composer_cleared_after_submit",
                request_attempted=request_attempted,
                post_was_enabled=post_was_enabled,
                elapsed=round(elapsed, 2),
            )
            return PublishResult(
                status="success",
                network_captured=False,
                network_request_attempted=request_attempted,
                elapsed_seconds=elapsed,
                dom_strategy=combined_strategy,
                post_was_enabled_at_click=post_was_enabled,
            )
        return PublishResult(
            status="timeout",
            error_reason="no_network_request_after_reply_direct_post_click",
            network_captured=False,
            network_request_attempted=request_attempted,
            elapsed_seconds=elapsed,
            dom_strategy=combined_strategy,
            post_was_enabled_at_click=post_was_enabled,
        )
    finally:
        _detach_listener()


async def _publish_via_current_input(
    page: Page,
    text: str,
    *,
    max_total_seconds: float,
    correlation_id: Optional[str],
    expected_video_url: Optional[str] = None,
    aggressive: bool = False,
    reply_scoped: bool = False,
    parent_anchor_token: Optional[str] = None,
    comment_surface_ready: bool = False,
) -> PublishResult:
    started = time.monotonic()
    fast_publish = _is_parallel_fast_publish(correlation_id)
    aggressive = bool(aggressive or fast_publish)
    main_comment_aggressive = bool(
        aggressive
        and not reply_scoped
        and _is_parallel_main_publish(correlation_id)
    )

    def _remaining() -> float:
        return max_total_seconds - (time.monotonic() - started)

    def _deadline_result(phase: str, dom_strategy: Optional[str] = None) -> PublishResult:
        return _publish_deadline_result(
            phase=phase,
            started=started,
            page=page,
            expected_video_url=expected_video_url,
            correlation_id=correlation_id,
            dom_strategy=dom_strategy,
        )

    def _typing_phase_timeout(*, retry: bool = False) -> float:
        remaining = _remaining()
        if remaining <= 0:
            return 0.0
        cap = 4.5 if aggressive else (6.0 if fast_publish else 10.0)
        if retry:
            cap = min(cap, 3.5 if aggressive else 5.0)
        reserve_for_post = 4.0 if aggressive else (5.0 if fast_publish else 6.0)
        if remaining > reserve_for_post + 0.75:
            return max(0.5, min(cap, remaining - reserve_for_post))
        return max(0.5, min(cap, remaining * 0.55))

    async def _text_visible_after_typing_timeout(phase: str, dom_strategy: Optional[str]) -> bool:
        remaining = _remaining()
        if remaining <= 0:
            return False
        probe_timeout = min(0.8 if aggressive else 1.2, max(0.2, remaining))
        try:
            ok, visible_text = await _asyncio_wait_for(
                comment_input_contains_expected_text(page, text),
                timeout=probe_timeout,
            )
        except asyncio.TimeoutError:
            ok, visible_text = False, ""
        except Exception:
            ok, visible_text = False, ""
        if main_comment_aggressive:
            state: Dict[str, Any] = {"ok": False, "reason": "not_checked"}
            try:
                state = await _asyncio_wait_for(
                    _main_comment_input_committed_text_state(page, text),
                    timeout=probe_timeout,
                )
            except asyncio.TimeoutError:
                state = {"ok": False, "reason": "timeout"}
            except Exception:
                state = {"ok": False, "reason": "exception"}
            if not ok and isinstance(state, dict) and state.get("ok"):
                visible_text = str(state.get("text") or "")

            exact_text = bool(
                _normalize_publish_text(visible_text) == _normalize_publish_text(text)
            )
            if isinstance(state, dict) and state.get("ok"):
                if _normalize_publish_text(visible_text) != _normalize_publish_text(text):
                    logger.warning(
                        "[PUBLISH] Main typing phase %s saw committed composer text, "
                        "but it is not exactly the current variant; refusing to Post. "
                        "actual='%s' expected='%s'",
                        phase,
                        visible_text[:80],
                        text[:80],
                    )
                    _log_publish_trace(
                        "main_typing_timeout_text_mismatch",
                        page=page,
                        expected_video_url=expected_video_url,
                        correlation_id=correlation_id,
                        phase=phase,
                        dom_strategy=dom_strategy,
                        input_preview=visible_text[:80],
                        expected_preview=text[:80],
                        level=logging.WARNING,
                    )
                    return False
                if state.get("postDisabled") is False:
                    logger.warning(
                        "[PUBLISH] Main typing phase %s timed out/unverified but composer has text "
                        "and Post is enabled; continuing to Post: '%s'",
                        phase,
                        visible_text[:60],
                    )
                    _log_publish_trace(
                        "main_typing_timeout_text_present",
                        page=page,
                        expected_video_url=expected_video_url,
                        correlation_id=correlation_id,
                        phase=phase,
                        dom_strategy=dom_strategy,
                        input_text_len=int(state.get("textLen") or len(visible_text or "")),
                        input_preview=visible_text[:60],
                        post_visible=state.get("postVisible"),
                        post_disabled=state.get("postDisabled"),
                        level=logging.WARNING,
                    )
                    return True

            if exact_text and _remaining() > 0.2:
                try:
                    enabled_button, enabled_info = await wait_for_post_button_enabled(
                        page,
                        max_wait_seconds=min(0.8, max(0.2, _remaining())),
                        reply_scoped=False,
                    )
                except Exception:
                    enabled_button, enabled_info = None, None
                if enabled_button:
                    logger.warning(
                        "[PUBLISH] Main typing phase %s timed out/unverified but exact text "
                        "and enabled Post are visible; continuing to Post: '%s'",
                        phase,
                        visible_text[:60],
                    )
                    _log_publish_trace(
                        "main_typing_timeout_text_ready_for_post",
                        page=page,
                        expected_video_url=expected_video_url,
                        correlation_id=correlation_id,
                        phase=phase,
                        dom_strategy=dom_strategy,
                        input_text_len=len(visible_text or ""),
                        input_preview=visible_text[:60],
                        post_enabled=True,
                        level=logging.WARNING,
                    )
                    return True
                _log_publish_trace(
                    "main_typing_timeout_text_not_ready_for_post",
                    page=page,
                    expected_video_url=expected_video_url,
                    correlation_id=correlation_id,
                    phase=phase,
                    dom_strategy=dom_strategy,
                    input_text_len=len(visible_text or ""),
                    input_preview=visible_text[:60],
                    post_visible=state.get("postVisible") if isinstance(state, dict) else None,
                    post_disabled=state.get("postDisabled") if isinstance(state, dict) else None,
                    enabled_reason=getattr(enabled_info, "reason", None),
                    level=logging.WARNING,
                )
                return False
            if not exact_text and visible_text:
                _log_publish_trace(
                    "main_typing_timeout_text_mismatch",
                    page=page,
                    expected_video_url=expected_video_url,
                    correlation_id=correlation_id,
                    phase=phase,
                    dom_strategy=dom_strategy,
                    input_preview=visible_text[:80],
                    expected_preview=text[:80],
                    level=logging.WARNING,
                )
            return False
        if not ok:
            return False
        logger.info(
            "[PUBLISH] Typing phase %s timed out/unverified but text is visible; continuing to Post: '%s'",
            phase,
            visible_text[:60],
        )
        _log_publish_trace(
            "typing_timeout_text_visible",
            page=page,
            expected_video_url=expected_video_url,
            correlation_id=correlation_id,
            phase=phase,
            dom_strategy=dom_strategy,
            input_text_len=len(visible_text or ""),
            input_preview=(visible_text or "")[:60],
        )
        return True

    _log_publish_trace(
        "publish_current_input_start",
        page=page,
        expected_video_url=expected_video_url,
        correlation_id=correlation_id,
        text_len=len(text or ""),
    )
    await suppress_keyboard_shortcuts_overlay(page)
    captcha_result = await _captcha_result_if_present(page, started)
    if captcha_result:
        return captcha_result
    logged_out_result = await _logged_out_publish_result_if_visible(
        page,
        started,
        phase="input_search",
        correlation_id=correlation_id,
        expected_video_url=expected_video_url,
        timeout_seconds=min(1.5, max(_remaining(), 0.2)),
    )
    if logged_out_result:
        return logged_out_result
    drift_result = _publish_url_guard_result(
        page,
        expected_video_url,
        started,
        phase="input_search",
        correlation_id=correlation_id,
    )
    if drift_result:
        return drift_result

    if _remaining() <= 0:
        return _deadline_result("before_input_search")
    if main_comment_aggressive and comment_surface_ready and _remaining() > 2.5:
        direct_result = await _publish_main_comment_direct_dom(
            page,
            text,
            started=started,
            max_total_seconds=max_total_seconds,
            expected_video_url=expected_video_url,
            correlation_id=correlation_id,
            previous_reason="surface_ready_fast_path",
        )
        if direct_result is not None:
            return direct_result

    if aggressive and _is_parallel_main_publish(correlation_id):
        input_search_budget = _parallel_main_input_search_seconds()
    elif aggressive:
        input_search_budget = 3.5
    else:
        input_search_budget = 6.0 if fast_publish else 12.0
    input_search_timeout = min(input_search_budget, _remaining(), max_total_seconds * 0.7)
    if input_search_timeout <= 0:
        return _deadline_result("before_input_search")
    input_search_outer_timeout = min(max(_remaining(), 0.0), input_search_timeout + 1.0)
    input_search_inner_timeout = max(
        0.1,
        min(input_search_timeout, max(0.1, input_search_outer_timeout - 0.25)),
    )
    try:
        if reply_scoped:
            input_handle, input_info = await _asyncio_wait_for(
                find_reply_input_smart(
                    page,
                    parent_anchor_token=parent_anchor_token,
                    max_total_seconds=input_search_inner_timeout,
                ),
                timeout=input_search_outer_timeout,
            )
        else:
            input_handle, input_info = await _asyncio_wait_for(
                find_comment_input_smart(
                    page,
                    max_total_seconds=input_search_inner_timeout,
                    aggressive=aggressive,
                    skip_initial_open=comment_surface_ready,
                    main_comment_aggressive=main_comment_aggressive,
                ),
                timeout=input_search_outer_timeout,
            )
    except asyncio.TimeoutError:
        if reply_scoped and parent_anchor_token:
            direct_result = await _publish_reply_comment_direct_dom(
                page,
                text,
                started=started,
                max_total_seconds=max_total_seconds,
                expected_video_url=expected_video_url,
                correlation_id=correlation_id,
                parent_anchor_token=parent_anchor_token,
                previous_reason="input_search_timeout",
            )
            if direct_result is not None:
                return direct_result
        if main_comment_aggressive:
            direct_result = await _publish_main_comment_direct_dom(
                page,
                text,
                started=started,
                max_total_seconds=max_total_seconds,
                expected_video_url=expected_video_url,
                correlation_id=correlation_id,
                previous_reason="input_search_timeout",
            )
            if direct_result is not None:
                return direct_result
        return _deadline_result("input_search")
    if _remaining() <= 0:
        return _deadline_result("after_input_search", getattr(input_info, "strategy", None))
    if not input_handle:
        captcha_result, captcha_cleared = await _captcha_result_or_cleared_if_present(page, started)
        if captcha_result:
            return captcha_result
        if captcha_cleared:
            logger.info("[PUBLISH] Retrying comment input search after manual captcha clear.")
            input_handle, input_info, retry_captcha_result = await _find_input_after_manual_captcha_clear(
                page,
                started,
                input_info,
                max_total_seconds=max_total_seconds,
            )
            if retry_captcha_result:
                return retry_captcha_result
            if not input_handle:
                captcha_result = await _captcha_result_if_present(page, started)
                if captcha_result:
                    return captcha_result
                return PublishResult(
                    status="dom_error",
                    error_reason=f"input_not_found_after_captcha_clear: {input_info.reason}",
                    elapsed_seconds=time.monotonic() - started,
                )
        if not input_handle:
            if input_info.reason == "redirected":
                return PublishResult(
                    status="redirected",
                    error_reason=f"redirected_to_{input_info.redirected_to}",
                    elapsed_seconds=time.monotonic() - started,
                )
            if reply_scoped and parent_anchor_token:
                direct_result = await _publish_reply_comment_direct_dom(
                    page,
                    text,
                    started=started,
                    max_total_seconds=max_total_seconds,
                    expected_video_url=expected_video_url,
                    correlation_id=correlation_id,
                    parent_anchor_token=parent_anchor_token,
                    previous_reason=input_info.reason,
                )
                if direct_result is not None:
                    return direct_result
            if main_comment_aggressive:
                direct_result = await _publish_main_comment_direct_dom(
                    page,
                    text,
                    started=started,
                    max_total_seconds=max_total_seconds,
                    expected_video_url=expected_video_url,
                    correlation_id=correlation_id,
                    previous_reason=input_info.reason,
                )
                if direct_result is not None:
                    return direct_result
            return PublishResult(
                status="dom_error",
                error_reason=f"input_not_found: {input_info.reason}",
                elapsed_seconds=time.monotonic() - started,
            )

    logger.info(
        "[PUBLISH] Input found via %s in %.1fs for %s",
        input_info.strategy,
        input_info.elapsed_seconds,
        correlation_id or "unknown",
    )
    _log_publish_trace(
        "input_found",
        page=page,
        expected_video_url=expected_video_url,
        correlation_id=correlation_id,
        strategy=input_info.strategy,
        elapsed=round(input_info.elapsed_seconds, 2),
    )

    captcha_result = await _captcha_result_if_present(
        page,
        started,
        dom_strategy=input_info.strategy,
    )
    if captcha_result:
        return captcha_result

    # dismiss_overlays intentionally removed here: calling it after finding the
    # input steals focus before type_into_comment runs. Overlays are dismissed
    # inside open_comment_section (before input search) and as a last resort
    # inside type_into_comment itself if focus verification fails.

    drift_result = _publish_url_guard_result(
        page,
        expected_video_url,
        started,
        dom_strategy=input_info.strategy,
        phase="before_typing",
        correlation_id=correlation_id,
    )
    if drift_result:
        return drift_result

    if _remaining() <= 0:
        return _deadline_result("before_typing", input_info.strategy)
    if input_handle is not None:
        try:
            await _asyncio_wait_for(
                ensure_click_target_clear(page, input_handle),
                timeout=max(0.1, min(_remaining(), 1.5 if aggressive else 3.0)),
            )
        except asyncio.TimeoutError:
            return _deadline_result("input_clear", input_info.strategy)
    captcha_result = await _captcha_result_if_present(
        page,
        started,
        dom_strategy=input_info.strategy,
    )
    if captcha_result:
        return captcha_result

    if _remaining() <= 0:
        return _deadline_result("before_typing", input_info.strategy)
    typing_timeout = _typing_phase_timeout()
    if typing_timeout <= 0:
        return _deadline_result("before_typing", input_info.strategy)
    try:
        typed_ok = await _asyncio_wait_for(
            type_into_comment(
                page,
                input_handle,
                text,
                deadline=time.monotonic() + typing_timeout,
                fast=aggressive,
                reply_scoped=reply_scoped,
                anchor_token=parent_anchor_token,
            ),
            timeout=typing_timeout,
        )
    except asyncio.TimeoutError:
        if await _text_visible_after_typing_timeout("typing", input_info.strategy):
            typed_ok = True
        else:
            if main_comment_aggressive:
                await _clear_main_comment_composer_best_effort(
                    page,
                    reason="typing_timeout_not_ready",
                    expected_video_url=expected_video_url,
                    correlation_id=correlation_id,
                )
            return _deadline_result("typing", input_info.strategy)

    if not typed_ok:
        if await _text_visible_after_typing_timeout("typing_unverified", input_info.strategy):
            typed_ok = True

    if not typed_ok:
        logger.warning("[PUBLISH] type_into_comment failed on first attempt; retrying with fresh input handle")
        if main_comment_aggressive:
            await _clear_main_comment_composer_best_effort(
                page,
                reason="typing_first_attempt_failed_before_retry",
                expected_video_url=expected_video_url,
                correlation_id=correlation_id,
            )
        await asyncio.sleep(0.2 if aggressive else 0.5)
        if _remaining() <= 0:
            return _deadline_result("after_typing_retry_delay", input_info.strategy)
        retry_input_search_timeout = min(2.5 if aggressive else 5.0, _remaining())
        if retry_input_search_timeout <= 0:
            return _deadline_result("before_typing_retry_input_search", input_info.strategy)
        try:
            retry_handle, _ = await _asyncio_wait_for(
                find_reply_input_smart(
                    page,
                    parent_anchor_token=parent_anchor_token,
                    max_total_seconds=retry_input_search_timeout,
                ) if reply_scoped else find_comment_input_smart(
                    page,
                    max_total_seconds=retry_input_search_timeout,
                    aggressive=aggressive,
                    skip_initial_open=comment_surface_ready,
                    main_comment_aggressive=main_comment_aggressive,
                ),
                timeout=retry_input_search_timeout,
            )
        except asyncio.TimeoutError:
            if main_comment_aggressive:
                await _clear_main_comment_composer_best_effort(
                    page,
                    reason="typing_retry_input_search_timeout",
                    expected_video_url=expected_video_url,
                    correlation_id=correlation_id,
                )
            return _deadline_result("typing_retry_input_search", input_info.strategy)
        if not retry_handle:
            retry_handle = input_handle
        if _remaining() <= 0:
            return _deadline_result("before_typing_retry", input_info.strategy)
        retry_typing_timeout = _typing_phase_timeout(retry=True)
        if retry_typing_timeout <= 0:
            return _deadline_result("before_typing_retry", input_info.strategy)
        try:
            retry_typed_ok = await _asyncio_wait_for(
                type_into_comment(
                    page,
                    retry_handle,
                    text,
                    deadline=time.monotonic() + retry_typing_timeout,
                    fast=True,
                    reply_scoped=reply_scoped,
                    anchor_token=parent_anchor_token,
                ),
                timeout=retry_typing_timeout,
            )
        except asyncio.TimeoutError:
            if await _text_visible_after_typing_timeout("typing_retry", input_info.strategy):
                retry_typed_ok = True
            else:
                if main_comment_aggressive:
                    await _clear_main_comment_composer_best_effort(
                        page,
                        reason="typing_retry_timeout_not_ready",
                        expected_video_url=expected_video_url,
                        correlation_id=correlation_id,
                    )
                return _deadline_result("typing_retry", input_info.strategy)
        if not retry_typed_ok:
            if await _text_visible_after_typing_timeout("typing_retry_unverified", input_info.strategy):
                retry_typed_ok = True
            else:
                if main_comment_aggressive:
                    await _clear_main_comment_composer_best_effort(
                        page,
                        reason="typing_retry_failed",
                        expected_video_url=expected_video_url,
                        correlation_id=correlation_id,
                    )
                return PublishResult(
                    status="dom_error",
                    error_reason="typing_failed",
                    dom_strategy=input_info.strategy,
                    elapsed_seconds=time.monotonic() - started,
                )
        input_handle = retry_handle

    if _remaining() <= 0:
        return _deadline_result("after_typing", input_info.strategy)
    await asyncio.sleep(0.15 if aggressive else (0.25 if fast_publish else 0.8))  # let TikTok render Post after robust input

    # Verify the input actually has text visible to React
    try:
        input_text = await input_handle.evaluate(
            "el => (el.textContent || el.innerText || '').trim()"
        )
        if not input_text:
            logger.warning("[PUBLISH] Input text empty after typing — React may not have registered it")
        else:
            logger.info("[PUBLISH] Input verified with text: '%s'", input_text[:30])
        _log_publish_trace(
            "typing_verified",
            page=page,
            expected_video_url=expected_video_url,
            correlation_id=correlation_id,
            input_text_len=len(input_text or ""),
            input_preview=(input_text or "")[:60],
        )
    except Exception:
        pass

    await suppress_keyboard_shortcuts_overlay(page)
    captcha_result = await _captcha_result_if_present(
        page,
        started,
        dom_strategy=input_info.strategy,
    )
    if captcha_result:
        return captcha_result

    drift_result = _publish_url_guard_result(
        page,
        expected_video_url,
        started,
        dom_strategy=input_info.strategy,
        phase="post_button_search",
        correlation_id=correlation_id,
    )
    if drift_result:
        return drift_result

    remaining = max_total_seconds - (time.monotonic() - started)
    if remaining <= 0:
        return _deadline_result("before_post_button_search", input_info.strategy)
    if reply_scoped:
        post_button, post_info = await find_reply_post_button_smart(
            page,
            max_total_seconds=min(3.0, max(remaining, 0.5)),
        )
    else:
        post_button, post_info = await find_post_button_smart(page, max_total_seconds=min(4.0, max(remaining, 0.5)))
    if not post_button:
        captcha_result = await _captcha_result_if_present(
            page,
            started,
            dom_strategy=input_info.strategy,
        )
        if captcha_result:
            return captcha_result
        if main_comment_aggressive:
            await _clear_main_comment_composer_best_effort(
                page,
                reason="post_button_not_found",
                expected_video_url=expected_video_url,
                correlation_id=correlation_id,
            )
        return PublishResult(
            status="dom_error",
            error_reason="post_button_not_found",
            dom_strategy=input_info.strategy,
            elapsed_seconds=time.monotonic() - started,
        )
    _log_publish_trace(
        "post_button_found",
        page=page,
        expected_video_url=expected_video_url,
        correlation_id=correlation_id,
        strategy=getattr(post_info, "strategy", None),
        elapsed=round(getattr(post_info, "elapsed_seconds", 0.0) or 0.0, 2),
    )

    remaining = max_total_seconds - (time.monotonic() - started)
    if remaining <= 0:
        return _deadline_result("before_post_enabled_gate", input_info.strategy or post_info.strategy)
    post_enabled_wait_seconds = (
        2.8
        if main_comment_aggressive
        else (1.5 if aggressive else (3.0 if fast_publish else 8.0))
    )
    enabled_button, enabled_info = await wait_for_post_button_enabled(
        page,
        max_wait_seconds=min(post_enabled_wait_seconds, max(remaining, 0.5)),
        reply_scoped=reply_scoped,
    )
    # Track whether Post passed the enabled-gate. False means the click below will
    # be via synthetic dispatch on a disabled button; treat such submits as
    # unverified unless we capture a network response.
    post_was_enabled = bool(enabled_button)
    if enabled_button:
        post_button = enabled_button
    else:
        captcha_result = await _captcha_result_if_present(
            page,
            started,
            dom_strategy=input_info.strategy or post_info.strategy,
        )
        if captcha_result:
            return captcha_result
        disabled_reason = str(getattr(enabled_info, "reason", "") or "")
        if disabled_reason.startswith("post_button_input_not_ready:"):
            if main_comment_aggressive:
                state = await _main_comment_input_committed_text_state(page, text)
                if isinstance(state, dict) and state.get("ok"):
                    visible_text = str(state.get("text") or "")
                    if _normalize_publish_text(visible_text) != _normalize_publish_text(text):
                        await _clear_main_comment_composer_best_effort(
                            page,
                            reason="post_enabled_gate_text_mismatch",
                            expected_video_url=expected_video_url,
                            correlation_id=correlation_id,
                        )
                        _log_publish_trace(
                            "main_post_enabled_gate_text_mismatch",
                            page=page,
                            expected_video_url=expected_video_url,
                            correlation_id=correlation_id,
                            reason=disabled_reason,
                            input_preview=visible_text[:80],
                            expected_preview=text[:80],
                            level=logging.WARNING,
                        )
                        return PublishResult(
                            status="dom_error",
                            error_reason="post_button_input_not_ready:text_mismatch",
                            dom_strategy=input_info.strategy or post_info.strategy,
                            elapsed_seconds=time.monotonic() - started,
                            post_was_enabled_at_click=False,
                            network_request_attempted=False,
                        )
                    if state.get("postDisabled") is False:
                        logger.warning(
                            "[PUBLISH] Main Post enabled-gate says %s, but composer state shows "
                            "the current variant and Post is enabled; continuing with aggressive click path.",
                            disabled_reason,
                        )
                        _log_publish_trace(
                            "main_post_enabled_gate_bypassed",
                            page=page,
                            expected_video_url=expected_video_url,
                            correlation_id=correlation_id,
                            reason=disabled_reason,
                            input_text_len=int(state.get("textLen") or len(str(state.get("text") or ""))),
                            input_preview=str(state.get("text") or "")[:60],
                            post_visible=state.get("postVisible"),
                            post_disabled=state.get("postDisabled"),
                            level=logging.WARNING,
                        )
                    else:
                        await _clear_main_comment_composer_best_effort(
                            page,
                            reason=f"post_enabled_gate_not_ready:{disabled_reason}"[:160],
                            expected_video_url=expected_video_url,
                            correlation_id=correlation_id,
                        )
                        _log_publish_trace(
                            "publish_result",
                            page=page,
                            expected_video_url=expected_video_url,
                            correlation_id=correlation_id,
                            status="dom_error",
                            reason=disabled_reason,
                            post_visible=state.get("postVisible"),
                            post_disabled=state.get("postDisabled"),
                            post_was_enabled=False,
                            elapsed=round(time.monotonic() - started, 2),
                            level=logging.WARNING,
                        )
                        return PublishResult(
                            status="dom_error",
                            error_reason=disabled_reason,
                            dom_strategy=input_info.strategy or post_info.strategy,
                            elapsed_seconds=time.monotonic() - started,
                            post_was_enabled_at_click=False,
                            network_request_attempted=False,
                        )
                else:
                    await _clear_main_comment_composer_best_effort(
                        page,
                        reason=f"post_enabled_gate_uncommitted:{disabled_reason}"[:160],
                        expected_video_url=expected_video_url,
                        correlation_id=correlation_id,
                    )
                    _log_publish_trace(
                        "publish_result",
                        page=page,
                        expected_video_url=expected_video_url,
                        correlation_id=correlation_id,
                        status="dom_error",
                        reason=disabled_reason,
                        post_was_enabled=False,
                        elapsed=round(time.monotonic() - started, 2),
                        level=logging.WARNING,
                    )
                    return PublishResult(
                        status="dom_error",
                        error_reason=disabled_reason,
                        dom_strategy=input_info.strategy or post_info.strategy,
                        elapsed_seconds=time.monotonic() - started,
                        post_was_enabled_at_click=False,
                        network_request_attempted=False,
                    )
            else:
                _log_publish_trace(
                    "publish_result",
                    page=page,
                    expected_video_url=expected_video_url,
                    correlation_id=correlation_id,
                    status="dom_error",
                    reason=disabled_reason,
                    post_was_enabled=False,
                    elapsed=round(time.monotonic() - started, 2),
                    level=logging.WARNING,
                )
                return PublishResult(
                    status="dom_error",
                    error_reason=disabled_reason,
                    dom_strategy=input_info.strategy or post_info.strategy,
                    elapsed_seconds=time.monotonic() - started,
                    post_was_enabled_at_click=False,
                    network_request_attempted=False,
                )
        logger.warning(
            "[PUBLISH] Post button not enabled after wait: %s. Trying legacy force-click anyway.",
            enabled_info.reason,
        )
    _log_publish_trace(
        "post_enabled_gate",
        page=page,
        expected_video_url=expected_video_url,
        correlation_id=correlation_id,
        post_was_enabled=post_was_enabled,
        reason=getattr(enabled_info, "reason", None),
        strategy=getattr(enabled_info, "strategy", None),
    )

    captured: Dict[str, Any] = {}
    event = asyncio.Event()
    listener_tasks = set()

    def _consume_listener_task(task: asyncio.Task) -> None:
        listener_tasks.discard(task)
        try:
            task.exception()
        except asyncio.CancelledError:
            pass
        except Exception:
            pass

    async def _capture_publish_response(response: Response) -> None:
        try:
            url = response.url
        except Exception:
            return
        if "/api/comment/publish/" not in url:
            return
        try:
            captured["url"] = url
            captured["status"] = response.status
            body = await response.text()
            captured["body"] = body
            try:
                captured["json"] = json.loads(body) if body else None
            except Exception:
                captured["json"] = None
        except Exception as exc:
            captured["body_error"] = str(exc)[:100]
        finally:
            event.set()

    def on_response(response: Response):
        try:
            url = response.url
        except Exception:
            return
        if "/api/comment/publish/" not in url:
            return
        task = asyncio.create_task(_capture_publish_response(response))
        listener_tasks.add(task)
        task.add_done_callback(_consume_listener_task)

    def on_request(request):
        try:
            url = request.url
        except Exception:
            return
        if "/api/comment/publish/" not in url:
            return
        captured["request_url"] = url
        try:
            captured["request_method"] = request.method
        except Exception:
            pass
        if fast_publish:
            event.set()

    page.on("request", on_request)
    page.on("response", on_response)
    # Every exit below must detach listeners. Reused browser_pool pages would
    # otherwise accumulate /api/comment/publish/ listeners and process one
    # publish response multiple times.
    _listener_removed = False

    def _detach_listener() -> None:
        nonlocal _listener_removed
        if _listener_removed:
            return
        try:
            page.remove_listener("request", on_request)
        except Exception:
            pass
        try:
            page.remove_listener("response", on_response)
        except Exception:
            pass
        for task in tuple(listener_tasks):
            if not task.done():
                task.cancel()
        _listener_removed = True

    # Do NOT call dismiss_overlays() after typing.
    # At this point TikTok/React already has the text and the focused input.
    # A late overlay cleanup can steal focus, change pointer-events or make the
    # previously found Post button stale. This was the likely reason for
    # no_network_request_after_post_click after a verified input text.
    if _remaining() <= 0:
        _detach_listener()
        return _deadline_result("before_click_target_clear", input_info.strategy or post_info.strategy)
    try:
        await _asyncio_wait_for(ensure_click_target_clear(page, post_button), timeout=max(_remaining(), 0.5))
    except asyncio.TimeoutError:
        _detach_listener()
        return _deadline_result("click_target_clear", input_info.strategy or post_info.strategy)
    captcha_result = await _captcha_result_if_present(
        page,
        started,
        dom_strategy=input_info.strategy or post_info.strategy,
    )
    if captcha_result:
        _detach_listener()
        return captcha_result

    try:
        await post_button.scroll_into_view_if_needed(timeout=1500)
    except Exception:
        pass

    # Re-select and clear any overlay blocking the Post button center.
    point_diag: Optional[Dict[str, Any]] = None
    fast_main_click_path = bool(
        aggressive
        and _is_parallel_main_publish(correlation_id)
        and post_was_enabled
        and not reply_scoped
    )
    try:
        if _remaining() <= 0:
            _detach_listener()
            return _deadline_result("before_clear_post_click_blockers", input_info.strategy or post_info.strategy)
        post_button, point_diag, click_point_clear = await _asyncio_wait_for(
            _clear_post_click_blockers(
                page,
                post_button,
                max_rounds=1 if fast_main_click_path else (3 if fast_publish else 6),
                reselect_enabled=not fast_main_click_path,
            ),
            timeout=max(_remaining(), 0.5),
        )
        if not click_point_clear:
            if _point_diag_looks_like_challenge(point_diag):
                _detach_listener()
                return PublishResult(
                    status="failed",
                    error_reason="captcha_challenge:slider",
                    elapsed_seconds=time.monotonic() - started,
                    dom_strategy=input_info.strategy or post_info.strategy,
                )
            captcha_result = await _captcha_result_if_present(
                page,
                started,
                dom_strategy=input_info.strategy or post_info.strategy,
            )
            if captcha_result:
                _detach_listener()
                return captcha_result
            logger.warning(
                "[PUBLISH] Post click point is still not clean after blocker clear; last point: %s",
                json.dumps(point_diag, ensure_ascii=False)[:1000],
            )
            # Last hard sweep before click. If the blocker is the keyboard shortcuts popup,
            # the persistent killer should make it pointer-events:none/remove it before the click.
            await suppress_keyboard_shortcuts_overlay(page)
            await asyncio.sleep(0.15 if fast_publish else 0.35)
            retry_fresh, retry_fresh_info = await wait_for_post_button_enabled(
                page,
                max_wait_seconds=0.8 if fast_publish else 2.0,
                reply_scoped=reply_scoped,
            )
            if retry_fresh:
                post_button = retry_fresh
                post_was_enabled = True
                logger.info("[PUBLISH] Fresh Post button selected after final suppress via %s", retry_fresh_info.strategy)
    except Exception as clear_exc:
        logger.warning("[PUBLISH] Clearing post click blockers failed: %s", clear_exc)

    # Final pre-click check: blocker clearance may have transitioned Post from
    # disabled to enabled. If a probe finds it enabled now, mark post_was_enabled.
    # If it's still disabled and we fall through to synthetic dispatch, the
    # publish result will be classified as unverified (see below).
    if not fast_main_click_path:
        try:
            _gate_button, _gate_info = await wait_for_post_button_enabled(
                page,
                max_wait_seconds=0.5,
                reply_scoped=reply_scoped,
            )
            if _gate_button:
                post_was_enabled = True
                post_button = _gate_button
        except Exception:
            pass
    drift_result = _publish_url_guard_result(
        page,
        expected_video_url,
        started,
        dom_strategy=input_info.strategy or post_info.strategy,
        phase="post_click",
        correlation_id=correlation_id,
    )
    if drift_result:
        _detach_listener()
        return drift_result

    # If elementFromPoint at button center returns BODY, the button itself has pointer-events:none.
    # TikTok sets this when it considers the session not authorized enough. Mouse clicks won't land.
    # Detect this and fire a synthetic MouseEvent directly on the element, bypassing pointer-events.
    _use_synthetic_click = False
    if isinstance(point_diag, dict) and (point_diag.get("tag") or "").upper() == "BODY":
        _use_synthetic_click = True
        logger.warning(
            "[PUBLISH] elementFromPoint=BODY; button has pointer-events:none. Will use synthetic dispatchEvent."
        )

    # Enter-submit fast-path: when the Post button is enabled (text committed
    # and ready) and we are not in the pointer-events:none synthetic case, a
    # real Enter keystroke on the focused editor submits faster and more
    # reliably than chasing the button center. Guarded: only treated as
    # submitted when the composer actually clears (see _enter_submit_fast_path).
    enter_submitted = False
    if post_was_enabled and not _use_synthetic_click:
        enter_submitted = await _enter_submit_fast_path(
            page,
            input_handle,
            reply_scoped=reply_scoped,
        )
        if enter_submitted:
            enter_wait_remaining = max_total_seconds - (time.monotonic() - started)
            if enter_wait_remaining > 0 and not event.is_set():
                try:
                    await asyncio.wait_for(
                        event.wait(),
                        timeout=min(2.0, max(enter_wait_remaining, 0.3)),
                    )
                except asyncio.TimeoutError:
                    pass

    synthetic_dispatch_attempted = False
    try:
        box = None if enter_submitted else await post_button.bounding_box()
        if enter_submitted:
            logger.info("[PUBLISH] Enter submit fast-path used; skipping Post button click")
        elif box:
            x = box["x"] + box["width"] / 2
            y = box["y"] + box["height"] / 2

            if _use_synthetic_click:
                # Temporarily restore pointer-events and fire synthetic click.
                dispatched = await page.evaluate(
                    r"""
                    ({x, y, selector}) => {
                        const candidates = [...document.querySelectorAll(selector)];
                        const btn = candidates.find(el => {
                            const r = el.getBoundingClientRect();
                            return r.width > 0 && r.height > 0;
                        }) || candidates[0];
                        if (!btn) return 'no_button';
                        const prev = btn.style.pointerEvents;
                        btn.style.setProperty('pointer-events', 'auto', 'important');
                        const r = btn.getBoundingClientRect();
                        const cx = r.left + r.width / 2;
                        const cy = r.top + r.height / 2;
                        const opts = {bubbles: true, cancelable: true, clientX: cx, clientY: cy, view: window};
                        btn.dispatchEvent(new MouseEvent('mousedown', opts));
                        btn.dispatchEvent(new MouseEvent('mouseup', opts));
                        btn.dispatchEvent(new MouseEvent('click', opts));
                        btn.style.pointerEvents = prev;
                        return 'dispatched';
                    }
                    """,
                    {
                        "x": x,
                        "y": y,
                        "selector": _post_click_fallback_selector(reply_scoped=reply_scoped),
                    },
                )
                synthetic_dispatch_attempted = dispatched == "dispatched"
                logger.info("[PUBLISH] Synthetic dispatchEvent result: %s, waiting for /api/comment/publish/", dispatched)
            else:
                logger.info(
                    "[PUBLISH] Clicking Post by mouse coordinates: x=%.1f y=%.1f w=%.1f h=%.1f",
                    x,
                    y,
                    box["width"],
                    box["height"],
                )
                await page.mouse.move(x, y)
                await asyncio.sleep(0.15)
                await page.mouse.down()
                await asyncio.sleep(0.08)
                await page.mouse.up()
                logger.info("[PUBLISH] Post button mouse-clicked, waiting for /api/comment/publish/")
        else:
            await post_button.click(timeout=5000, force=True)
            logger.info("[PUBLISH] Post button clicked by Playwright fallback, waiting for /api/comment/publish/")
    except Exception as exc:
        logger.warning("[PUBLISH] Normal/mouse post click failed: %s", exc)
        await suppress_keyboard_shortcuts_overlay(page)
        try:
            # Prefer a fresh DOM locator over a stale ElementHandle. In reply-scoped
            # mode never fall back to TikTok's global comment-post selector; if the
            # scoped marker disappeared, this attempt should fail rather than post a
            # top-level comment by mistake.
            locator_selector = _post_click_fallback_selector(reply_scoped=reply_scoped)
            locator = page.locator(locator_selector).last
            await locator.click(timeout=3000, force=True)
            logger.info("[PUBLISH] Used fresh locator click fallback, waiting for /api/comment/publish/")
        except Exception as locator_exc:
            try:
                await post_button.click(timeout=5000, force=False)
                logger.info("[PUBLISH] Used non-force Playwright click fallback, waiting for /api/comment/publish/")
            except Exception as pw_exc:
                try:
                    await post_button.evaluate("el => el.click()")
                    logger.info("[PUBLISH] Used JS click fallback for post button, waiting for /api/comment/publish/")
                except Exception as js_exc:
                    click_error_text = " ".join(
                        str(item)
                        for item in (exc, locator_exc, pw_exc, js_exc)
                        if item is not None
                    )
                    _detach_listener()
                    if _looks_like_driver_closed_error(click_error_text):
                        return PublishResult(
                            status="dom_error",
                            error_reason="post_click_failed:driver_connection_closed",
                            dom_strategy=input_info.strategy,
                            elapsed_seconds=time.monotonic() - started,
                        )
                    return PublishResult(
                        status="dom_error",
                        error_reason=f"post_click_failed: mouse={str(exc)[:40]} locator={str(locator_exc)[:40]} pw={str(pw_exc)[:40]} js={str(js_exc)[:40]}",
                        dom_strategy=input_info.strategy,
                        elapsed_seconds=time.monotonic() - started,
                    )

    wait_remaining = max_total_seconds - (time.monotonic() - started)
    if wait_remaining <= 0:
        _detach_listener()
        return _deadline_result("after_post_click", input_info.strategy or post_info.strategy)
    try:
        publish_wait = _PARALLEL_POST_RESPONSE_WAIT_SECONDS if fast_publish else 6.0
        await asyncio.wait_for(event.wait(), timeout=min(publish_wait, max(wait_remaining, 0.5)))
    except asyncio.TimeoutError:
        pass

    if fast_publish and captured.get("request_url") and not captured.get("url"):
        grace = min(_PARALLEL_POST_RESPONSE_GRACE_SECONDS, max(_remaining(), 0.0))
        if grace > 0:
            await asyncio.sleep(grace)

    if not captured and synthetic_dispatch_attempted:
        logger.warning(
            "[PUBLISH] Synthetic Post dispatch produced no network confirmation; "
            "skipping immediate second click to avoid duplicate comments. "
            "Caller should verify DOM before retrying another account."
        )
    elif not captured.get("url") and fast_publish and captured.get("request_url"):
        logger.info(
            "[PUBLISH] Fast parallel publish mode: skipping second Post click "
            "after captured publish request to avoid duplicate comments."
        )
    elif not captured and _remaining() <= 0:
        logger.info("[PUBLISH] Skipping second Post click because publish deadline is exhausted.")
    elif not captured:
        # Last clean attempt: if the first mouse click closed a toast or focused the page
        # instead of publishing, reselect the visible Post button and try one normal click.
        try:
            retry_button, retry_info = await wait_for_post_button_enabled(
                page,
                max_wait_seconds=min(2.0, max(_remaining(), 0.5)),
                reply_scoped=reply_scoped,
            )
            if retry_button:
                retry_box = await retry_button.bounding_box()
                if retry_box:
                    rx = retry_box["x"] + retry_box["width"] / 2
                    ry = retry_box["y"] + retry_box["height"] / 2
                    retry_button, retry_diag, retry_clear = await _asyncio_wait_for(
                        _clear_post_click_blockers(
                            page,
                            retry_button,
                            max_rounds=4,
                        ),
                        timeout=max(_remaining(), 0.5),
                    )
                    retry_box = await retry_button.bounding_box()
                    if retry_box:
                        rx = retry_box["x"] + retry_box["width"] / 2
                        ry = retry_box["y"] + retry_box["height"] / 2
                    logger.info(
                        "[PUBLISH] Retry elementFromPoint before Post click: %s",
                        json.dumps(retry_diag, ensure_ascii=False)[:1000],
                    )
                    await suppress_keyboard_shortcuts_overlay(page)
                    _retry_synthetic = isinstance(retry_diag, dict) and (retry_diag.get("tag") or "").upper() == "BODY"
                    if _retry_synthetic:
                        logger.warning("[PUBLISH] Retry: elementFromPoint=BODY; using synthetic dispatchEvent.")
                        await page.evaluate(
                            r"""
                            ([selector]) => {
                                const btn = [...document.querySelectorAll(selector)].find(el => { const r = el.getBoundingClientRect(); return r.width > 0 && r.height > 0; });
                                if (!btn) return;
                                const prev = btn.style.pointerEvents;
                                btn.style.setProperty('pointer-events', 'auto', 'important');
                                const r = btn.getBoundingClientRect();
                                const cx = r.left + r.width / 2, cy = r.top + r.height / 2;
                                const opts = {bubbles: true, cancelable: true, clientX: cx, clientY: cy, view: window};
                                btn.dispatchEvent(new MouseEvent('mousedown', opts));
                                btn.dispatchEvent(new MouseEvent('mouseup', opts));
                                btn.dispatchEvent(new MouseEvent('click', opts));
                                btn.style.pointerEvents = prev;
                            }
                            """,
                            [_post_click_fallback_selector(reply_scoped=reply_scoped)],
                        )
                        logger.info("[PUBLISH] Retry synthetic dispatchEvent fired, waiting for /api/comment/publish/")
                    else:
                        logger.info(
                            "[PUBLISH] Retrying Post click by mouse coordinates: x=%.1f y=%.1f",
                            rx,
                            ry,
                        )
                        await page.mouse.move(rx, ry)
                        await asyncio.sleep(0.12)
                        await page.mouse.down()
                        await asyncio.sleep(0.07)
                        await page.mouse.up()

                    try:
                        retry_wait_remaining = max_total_seconds - (time.monotonic() - started)
                        if retry_wait_remaining > 0:
                            await asyncio.wait_for(event.wait(), timeout=min(4.0, max(retry_wait_remaining, 0.5)))
                    except asyncio.TimeoutError:
                        pass
        except Exception as retry_exc:
            logger.warning("[PUBLISH] Retry Post click failed: %s", retry_exc)

    _detach_listener()

    elapsed = time.monotonic() - started
    request_attempted = bool(captured.get("request_url"))
    if captured.get("url"):
        body_preview = (captured.get("body") or "")[:200].replace("\n", " ")
        response_json = captured.get("json") if isinstance(captured.get("json"), dict) else None
        comment_id = _extract_comment_id(response_json)

        if response_json is not None:
            status_code = response_json.get("status_code")
            # comment_id is the definitive success indicator — present when TikTok accepted the comment.
            # Accept it regardless of whether status_code is 0 to handle A/B response variants
            # where status_code may be absent but the comment was clearly created.
            if comment_id:
                _log_publish_trace(
                    "publish_result",
                    page=page,
                    expected_video_url=expected_video_url,
                    correlation_id=correlation_id,
                    status="success",
                    comment_id=comment_id,
                    network_status=captured.get("status"),
                    post_was_enabled=post_was_enabled,
                    elapsed=round(elapsed, 2),
                )
                if status_code is not None and status_code != 0:
                    logger.warning(
                        "[PUBLISH] comment_id present but status_code=%s; treating as success; full json: %s",
                        status_code,
                        json.dumps(response_json, ensure_ascii=False)[:300],
                    )
                return PublishResult(
                    status="success",
                    comment_id=comment_id,
                    network_captured=True,
                    network_request_attempted=True,
                    network_status_code=captured.get("status"),
                    network_body_preview=body_preview,
                    network_response_json=response_json,
                    elapsed_seconds=elapsed,
                    dom_strategy=input_info.strategy or post_info.strategy,
                    post_was_enabled_at_click=post_was_enabled,
                )
            if not response_json:
                _log_publish_trace(
                    "publish_result",
                    page=page,
                    expected_video_url=expected_video_url,
                    correlation_id=correlation_id,
                    status="failed",
                    reason="stealth_reject_empty_body",
                    network_status=captured.get("status"),
                    post_was_enabled=post_was_enabled,
                    elapsed=round(elapsed, 2),
                )
                return PublishResult(
                    status="failed",
                    error_reason="stealth_reject_empty_body",
                    network_captured=True,
                    network_request_attempted=True,
                    network_status_code=captured.get("status"),
                    network_body_preview=body_preview,
                    network_response_json=response_json,
                    elapsed_seconds=elapsed,
                    dom_strategy=input_info.strategy or post_info.strategy,
                    post_was_enabled_at_click=post_was_enabled,
                )
            _log_publish_trace(
                "publish_result",
                page=page,
                expected_video_url=expected_video_url,
                correlation_id=correlation_id,
                status="failed",
                reason=f"api_error_status_code={status_code}",
                network_status=captured.get("status"),
                post_was_enabled=post_was_enabled,
                elapsed=round(elapsed, 2),
            )
            return PublishResult(
                status="failed",
                error_reason=f"api_error_status_code={status_code}",
                network_captured=True,
                network_request_attempted=True,
                network_status_code=captured.get("status"),
                network_body_preview=body_preview,
                network_response_json=response_json,
                elapsed_seconds=elapsed,
                dom_strategy=input_info.strategy or post_info.strategy,
                post_was_enabled_at_click=post_was_enabled,
            )

        if not (captured.get("body") or "").strip():
            _log_publish_trace(
                "publish_result",
                page=page,
                expected_video_url=expected_video_url,
                correlation_id=correlation_id,
                status="failed",
                reason="stealth_reject_empty_body",
                network_status=captured.get("status"),
                post_was_enabled=post_was_enabled,
                elapsed=round(elapsed, 2),
            )
            return PublishResult(
                status="failed",
                error_reason="stealth_reject_empty_body",
                network_captured=True,
                network_request_attempted=True,
                network_status_code=captured.get("status"),
                network_body_preview=body_preview,
                elapsed_seconds=elapsed,
                dom_strategy=input_info.strategy or post_info.strategy,
                post_was_enabled_at_click=post_was_enabled,
            )
        _log_publish_trace(
            "publish_result",
            page=page,
            expected_video_url=expected_video_url,
            correlation_id=correlation_id,
            status="failed",
            reason="non_json_response",
            network_status=captured.get("status"),
            post_was_enabled=post_was_enabled,
            elapsed=round(elapsed, 2),
        )
        return PublishResult(
            status="failed",
            error_reason="non_json_response",
            network_captured=True,
            network_request_attempted=True,
            network_status_code=captured.get("status"),
            network_body_preview=body_preview,
            elapsed_seconds=elapsed,
            dom_strategy=input_info.strategy or post_info.strategy,
            post_was_enabled_at_click=post_was_enabled,
        )

    try:
        current_url = (page.url or "").lower()
    except Exception:
        current_url = ""
    for marker in ("/messages", "/login", "/verify"):
        if marker in current_url:
            return PublishResult(
                status="redirected",
                error_reason=f"redirected_to_{marker.strip('/')}_after_click",
                elapsed_seconds=elapsed,
                dom_strategy=input_info.strategy or post_info.strategy,
                post_was_enabled_at_click=post_was_enabled,
            )
    captcha_result = await _captcha_result_if_present(
        page,
        started,
        dom_strategy=input_info.strategy or post_info.strategy,
    )
    if captcha_result:
        return captcha_result

    # Confirmation robustness: TikTok very frequently returns an empty/late
    # response body even on a successful publish. The composer clearing is a
    # strong local "submit accepted" signal. Trust it ONLY when submission went
    # through a trusted path (Post button was enabled, or the Enter fast-path
    # cleared the composer) so the disabled synthetic-dispatch path stays
    # classified as unverified.
    if post_was_enabled or enter_submitted:
        cleared = await _composer_cleared_state(page, reply_scoped=reply_scoped)
        if cleared.get("exists") and cleared.get("empty"):
            _log_publish_trace(
                "publish_result",
                page=page,
                expected_video_url=expected_video_url,
                correlation_id=correlation_id,
                status="success",
                reason="composer_cleared_after_submit",
                enter_submitted=enter_submitted,
                post_was_enabled=post_was_enabled,
                request_attempted=request_attempted,
                elapsed=round(elapsed, 2),
            )
            logger.info(
                "[PUBLISH] No network body but composer cleared after submit "
                "(enter=%s, post_enabled=%s); treating as success.",
                enter_submitted,
                post_was_enabled,
            )
            return PublishResult(
                status="success",
                network_captured=False,
                network_request_attempted=request_attempted,
                elapsed_seconds=elapsed,
                dom_strategy=input_info.strategy or post_info.strategy,
                post_was_enabled_at_click=post_was_enabled,
            )

    try:
        diag = await page.evaluate(r"""
        () => {
            const buttons = [...document.querySelectorAll('button, [role="button"], div[tabindex="0"]')]
                .slice(0, 80)
                .map((b, i) => {
                    const r = b.getBoundingClientRect();
                    const st = getComputedStyle(b);
                    return {
                        i,
                        text: ((b.innerText || b.textContent || '') + ' ' + (b.getAttribute('aria-label') || '')).trim().slice(0, 100),
                        e2e: b.getAttribute('data-e2e'),
                        smart: b.getAttribute('data-smart-id'),
                        disabled: b.hasAttribute('disabled') || b.getAttribute('aria-disabled') === 'true',
                        visible: r.width > 0 && r.height > 0,
                        x: Math.round(r.x),
                        y: Math.round(r.y),
                        w: Math.round(r.width),
                        h: Math.round(r.height),
                        opacity: st.opacity,
                        pointerEvents: st.pointerEvents
                    };
                });

            const input = document.querySelector(
                '[data-smart-id="comment-input"], [data-e2e="comment-input"], div[contenteditable="true"], textarea'
            );
            const inputText = input ? (input.textContent || input.value || input.innerText || '').trim() : '';

            const postCandidates = buttons
                .filter(b => {
                    const t = (b.text || '').toLowerCase();
                    const e2e = (b.e2e || '').toLowerCase();
                    const smart = (b.smart || '').toLowerCase();
                    return t.includes('post') || t.includes('send') || e2e.includes('post') || e2e.includes('publish') || smart.includes('post');
                })
                .slice(0, 20);

            return {
                url: location.href,
                activeTag: document.activeElement ? document.activeElement.tagName : null,
                activeE2e: document.activeElement ? document.activeElement.getAttribute('data-e2e') : null,
                inputText,
                postCandidates,
                buttons
            };
        }
        """)
        logger.warning(
            "[PUBLISH_DIAG] no network after post click: %s",
            json.dumps(diag, ensure_ascii=False)[:4000],
        )
    except Exception as diag_err:
        logger.warning("[PUBLISH_DIAG] failed: %s", diag_err)

    # No network response after click. Two distinct cases:
    #   A) Post button was enabled — real timeout. Caller can attempt local DOM check.
    #   B) Post button was disabled and we went through synthetic dispatch — the
    #      click may or may not have triggered React's handler. We classify this
    #      as `submitted_unverified_disabled_post` (treated as ambiguous in
    #      AMBIGUOUS_POST_SUBMIT_ERRORS) so downstream logic does NOT consider
    #      a local DOM match alone as proof of publication.
    if not post_was_enabled and not request_attempted:
        if main_comment_aggressive:
            await _clear_main_comment_composer_best_effort(
                page,
                reason="submitted_unverified_disabled_post",
                expected_video_url=expected_video_url,
                correlation_id=correlation_id,
            )
        _log_publish_trace(
            "publish_result",
            page=page,
            expected_video_url=expected_video_url,
            correlation_id=correlation_id,
            status="failed",
            reason="submitted_unverified_disabled_post",
            synthetic_dispatch=synthetic_dispatch_attempted,
            post_was_enabled=post_was_enabled,
            request_attempted=False,
            elapsed=round(elapsed, 2),
            level=logging.WARNING,
        )
        return PublishResult(
            status="failed",
            error_reason="submitted_unverified_disabled_post",
            elapsed_seconds=elapsed,
            dom_strategy=input_info.strategy or post_info.strategy,
            post_was_enabled_at_click=False,
            network_request_attempted=False,
        )

    if request_attempted:
        _log_publish_trace(
            "publish_result",
            page=page,
            expected_video_url=expected_video_url,
            correlation_id=correlation_id,
            status="timeout",
            reason="no_network_response_after_publish_request",
            synthetic_dispatch=synthetic_dispatch_attempted,
            post_was_enabled=post_was_enabled,
            request_attempted=True,
            elapsed=round(elapsed, 2),
            level=logging.WARNING,
        )
        return PublishResult(
            status="timeout",
            error_reason="no_network_response_after_publish_request",
            elapsed_seconds=elapsed,
            dom_strategy=input_info.strategy or post_info.strategy,
            post_was_enabled_at_click=post_was_enabled,
            network_request_attempted=True,
        )

    if main_comment_aggressive:
        await _clear_main_comment_composer_best_effort(
            page,
            reason="no_network_request_after_post_click",
            expected_video_url=expected_video_url,
            correlation_id=correlation_id,
        )
    _log_publish_trace(
        "publish_result",
        page=page,
        expected_video_url=expected_video_url,
        correlation_id=correlation_id,
        status="timeout",
        reason="no_network_request_after_post_click",
        synthetic_dispatch=synthetic_dispatch_attempted,
        post_was_enabled=post_was_enabled,
        request_attempted=False,
        elapsed=round(elapsed, 2),
        level=logging.WARNING,
    )
    return PublishResult(
        status="timeout",
        error_reason="no_network_request_after_post_click",
        elapsed_seconds=elapsed,
        dom_strategy=input_info.strategy or post_info.strategy,
        post_was_enabled_at_click=post_was_enabled,
        network_request_attempted=False,
    )


async def publish_comment_hybrid(
    page: Page,
    text: str,
    *,
    max_total_seconds: float = 15.0,
    correlation_id: Optional[str] = None,
    expected_video_url: Optional[str] = None,
    comments_already_open: bool = False,
) -> PublishResult:
    started = time.monotonic()
    fast_publish = _is_parallel_fast_publish(correlation_id)
    aggressive_publish = fast_publish

    def _remaining() -> float:
        return max_total_seconds - (time.monotonic() - started)

    def _deadline_result(phase: str, dom_strategy: Optional[str] = None) -> PublishResult:
        return _publish_deadline_result(
            phase=phase,
            started=started,
            page=page,
            expected_video_url=expected_video_url,
            correlation_id=correlation_id,
            dom_strategy=dom_strategy,
        )

    drift_result = _publish_url_guard_result(
        page,
        expected_video_url,
        started,
        phase="open_comments",
        correlation_id=correlation_id,
    )
    if drift_result:
        return drift_result
    captcha_result = await _captcha_result_if_present(page, started)
    if captcha_result:
        return captcha_result
    logged_out_result = await _logged_out_publish_result_if_visible(
        page,
        started,
        phase="open_comments",
        correlation_id=correlation_id,
        expected_video_url=expected_video_url,
        timeout_seconds=min(1.5, max(_remaining(), 0.2)),
    )
    if logged_out_result:
        return logged_out_result
    if _remaining() <= 0:
        return _deadline_result("before_open_comments")
    comment_surface_ready = False
    if comments_already_open:
        try:
            surface = await get_comment_surface_state(page)
            comment_surface_ready = _surface_has_publish_input(surface)
            if not comment_surface_ready:
                logger.info(
                    "[PUBLISH] Ignoring comments_already_open for %s: publish input is not visible. surface=%s",
                    correlation_id or "unknown",
                    json.dumps(surface, ensure_ascii=False)[:500] if isinstance(surface, dict) else str(surface)[:200],
                )
        except Exception as exc:
            logger.debug(
                "[PUBLISH] Could not validate already-open composer for %s: %s",
                correlation_id or "unknown",
                str(exc)[:120],
            )
    if comment_surface_ready:
        logger.info(
            "[PUBLISH] Publish input already visible for %s; skipping open_comment_section",
            correlation_id or "unknown",
        )
    else:
        try:
            open_comments_budget = (
                _parallel_main_open_comments_seconds()
                if aggressive_publish and _is_parallel_main_publish(correlation_id)
                else (2.5 if aggressive_publish else 5.0)
            )
            open_comments_outer_timeout = min(
                max(_remaining(), 0.5),
                max(open_comments_budget + 0.75, 0.75),
            )
            comment_surface_ready = bool(await _asyncio_wait_for(
                open_comment_section(
                    page,
                    max_wait=min(open_comments_budget, max(_remaining(), 0.5)),
                    # Publishing is ready only when the composer is visible.
                    # A comment list/drawer alone is not enough and leads to
                    # long input-search loops on already loaded pages.
                    require_input=True,
                    aggressive=aggressive_publish,
                    main_comment_aggressive=aggressive_publish and _is_parallel_main_publish(correlation_id),
                ),
                timeout=open_comments_outer_timeout,
            ))
        except asyncio.TimeoutError:
            try:
                surface = await get_comment_surface_state(page)
                if _surface_has_publish_input(surface):
                    logger.info(
                        "[PUBLISH] open_comment_section timed out, but publish input is visible; continuing. surface=%s",
                        json.dumps(surface, ensure_ascii=False)[:500] if isinstance(surface, dict) else str(surface)[:200],
                    )
                    comment_surface_ready = True
                else:
                    return _deadline_result("open_comments")
            except Exception:
                return _deadline_result("open_comments")
    captcha_result = await _captcha_result_if_present(page, started)
    if captcha_result:
        return captcha_result
    return await _publish_via_current_input(
        page,
        text,
        max_total_seconds=max_total_seconds,
        correlation_id=correlation_id,
        expected_video_url=expected_video_url,
        aggressive=aggressive_publish,
        comment_surface_ready=comment_surface_ready,
    )


async def publish_reply_hybrid(
    page: Page,
    text: str,
    *,
    parent_comment_text: Optional[str] = None,
    parent_comment_id: Optional[str] = None,
    parent_anchor_token: Optional[str] = None,
    max_total_seconds: float = 15.0,
    correlation_id: Optional[str] = None,
    expected_video_url: Optional[str] = None,
    comments_already_open: bool = False,
) -> PublishResult:
    started = time.monotonic()
    fast_publish = _is_parallel_fast_publish(correlation_id)

    def _remaining() -> float:
        return max_total_seconds - (time.monotonic() - started)

    def _deadline_result(phase: str, dom_strategy: Optional[str] = None) -> PublishResult:
        return _publish_deadline_result(
            phase=phase,
            started=started,
            page=page,
            expected_video_url=expected_video_url,
            correlation_id=correlation_id,
            dom_strategy=dom_strategy,
        )

    async def _recover_reply_open_timeout(phase: str) -> Optional[DomActionResult]:
        remaining = _remaining()
        if remaining <= 0:
            return None
        probe_timeout = min(1.5 if fast_publish else 2.0, max(0.4, remaining))
        try:
            input_handle, input_info = await _asyncio_wait_for(
                find_reply_input_smart(
                    page,
                    parent_anchor_token=parent_anchor_token,
                    max_total_seconds=probe_timeout,
                ),
                timeout=probe_timeout,
            )
        except asyncio.TimeoutError:
            return None
        except Exception:
            return None
        if not input_handle:
            return None
        strategy = getattr(input_info, "strategy", None) or "reply-input-visible"
        logger.info(
            "[PUBLISH] Reply box %s timed out, but scoped reply input is visible via %s; continuing",
            phase,
            strategy,
        )
        _log_publish_trace(
            "reply_open_timeout_recovered_input_visible",
            page=page,
            expected_video_url=expected_video_url,
            correlation_id=correlation_id,
            phase=phase,
            strategy=strategy,
        )
        return DomActionResult(
            True,
            strategy=f"{phase}+{strategy}",
            anchor_token=parent_anchor_token,
            elapsed_seconds=time.monotonic() - started,
        )

    drift_result = _publish_url_guard_result(
        page,
        expected_video_url,
        started,
        phase="open_reply_comments",
        correlation_id=correlation_id,
    )
    if drift_result:
        return drift_result
    captcha_result = await _captcha_result_if_present(page, started)
    if captcha_result:
        return captcha_result
    logged_out_result = await _logged_out_publish_result_if_visible(
        page,
        started,
        phase="open_reply_comments",
        correlation_id=correlation_id,
        expected_video_url=expected_video_url,
        timeout_seconds=min(1.5, max(_remaining(), 0.2)),
    )
    if logged_out_result:
        return logged_out_result
    reply_open = None
    reply_box_attempts = 2 if fast_publish else None
    reply_box_timeout = 6.0 if fast_publish else 10.0
    if comments_already_open:
        if _remaining() <= 0:
            return _deadline_result("before_open_reply_box")
        try:
            reply_open = await _asyncio_wait_for(
                open_reply_box(
                    page,
                    parent_comment_text,
                    parent_comment_id,
                    parent_anchor_token,
                    max_attempts=reply_box_attempts,
                ),
                timeout=max(0.5, min(_remaining(), reply_box_timeout)),
            )
        except asyncio.TimeoutError:
            recovered = await _recover_reply_open_timeout("reply_fast_open_timeout")
            if recovered is not None:
                reply_open = recovered
            else:
                _log_publish_trace(
                    "reply_fast_open_timeout",
                    page=page,
                    expected_video_url=expected_video_url,
                    correlation_id=correlation_id,
                    level=logging.WARNING,
                    remaining=round(_remaining(), 2),
                )
                return PublishResult(
                    status="timeout",
                    error_reason="reply_box_open_timeout",
                    elapsed_seconds=time.monotonic() - started,
                )
        if reply_open is not None and not reply_open.success:
            if reply_open.reason == "redirected":
                return PublishResult(
                    status="redirected",
                    error_reason=f"redirected_to_{reply_open.redirected_to}",
                    elapsed_seconds=time.monotonic() - started,
                )
            _log_publish_trace(
                "reply_fast_open_failed",
                page=page,
                expected_video_url=expected_video_url,
                correlation_id=correlation_id,
                reason=reply_open.reason,
                level=logging.INFO,
            )
            return PublishResult(
                status="dom_error",
                error_reason=f"reply_box_not_open:{reply_open.reason or 'unknown'}",
                elapsed_seconds=time.monotonic() - started,
            )
    elif reply_open is not None and not reply_open.success:
        if reply_open.reason == "redirected":
            return PublishResult(
                status="redirected",
                error_reason=f"redirected_to_{reply_open.redirected_to}",
                elapsed_seconds=time.monotonic() - started,
            )
        _log_publish_trace(
            "reply_fast_open_failed",
            page=page,
            expected_video_url=expected_video_url,
            correlation_id=correlation_id,
            reason=reply_open.reason,
            level=logging.INFO,
        )
        return PublishResult(
            status="dom_error",
            error_reason=f"reply_box_not_open:{reply_open.reason or 'unknown'}",
            elapsed_seconds=time.monotonic() - started,
        )

    if reply_open is None:
        if _remaining() <= 0:
            return _deadline_result("before_open_reply_comments")
        try:
            await _asyncio_wait_for(
                open_comment_section(page),
                timeout=max(0.5, min(_remaining(), 8.0)),
            )
        except asyncio.TimeoutError:
            return _deadline_result("open_reply_comments")
        captcha_result = await _captcha_result_if_present(page, started)
        if captcha_result:
            return captcha_result
        drift_result = _publish_url_guard_result(
            page,
            expected_video_url,
            started,
            phase="open_reply_box",
            correlation_id=correlation_id,
        )
        if drift_result:
            return drift_result
        logged_out_result = await _logged_out_publish_result_if_visible(
            page,
            started,
            phase="open_reply_box",
            correlation_id=correlation_id,
            expected_video_url=expected_video_url,
            timeout_seconds=min(1.5, max(_remaining(), 0.2)),
        )
        if logged_out_result:
            return logged_out_result
        captcha_result = await _captcha_result_if_present(page, started)
        if captcha_result:
            return captcha_result
        if _remaining() <= 0:
            return _deadline_result("before_open_reply_box")
        try:
            reply_open = await _asyncio_wait_for(
                open_reply_box(
                    page,
                    parent_comment_text,
                    parent_comment_id,
                    parent_anchor_token,
                    max_attempts=reply_box_attempts,
                ),
                timeout=max(0.5, min(_remaining(), 6.0 if fast_publish else 8.0)),
            )
        except asyncio.TimeoutError:
            recovered = await _recover_reply_open_timeout("open_reply_box")
            if recovered is not None:
                reply_open = recovered
            else:
                return _deadline_result("open_reply_box")
    if not reply_open.success:
        captcha_result = await _captcha_result_if_present(page, started)
        if captcha_result:
            return captcha_result
        logged_out_result = await _logged_out_publish_result_if_visible(
            page,
            started,
            phase="reply_box_failed",
            correlation_id=correlation_id,
            expected_video_url=expected_video_url,
            timeout_seconds=min(1.5, max(_remaining(), 0.2)),
        )
        if logged_out_result:
            return logged_out_result
        if reply_open.reason == "redirected":
            return PublishResult(
                status="redirected",
                error_reason=f"redirected_to_{reply_open.redirected_to}",
                elapsed_seconds=time.monotonic() - started,
            )
        return PublishResult(
            status="dom_error",
            error_reason=f"reply_box_not_open:{reply_open.reason or 'reply_button_not_found'}",
            elapsed_seconds=time.monotonic() - started,
        )
    resolved_parent_anchor_token = (
        getattr(reply_open, "anchor_token", None)
        or parent_anchor_token
        or None
    )
    remaining = max_total_seconds - (time.monotonic() - started)
    if remaining <= 0:
        return _deadline_result("before_reply_input_publish", reply_open.strategy)
    input_budget = remaining
    if max_total_seconds >= _REPLY_INPUT_MIN_SECONDS:
        min_input_budget = (
            max(1.0, _PARALLEL_REPLY_INPUT_MIN_SECONDS)
            if fast_publish
            else _REPLY_INPUT_MIN_SECONDS
        )
        input_budget = max(remaining, min_input_budget)
    result = await _publish_via_current_input(
        page,
        text,
        max_total_seconds=input_budget,
        correlation_id=correlation_id,
        expected_video_url=expected_video_url,
        aggressive=fast_publish,
        reply_scoped=True,
        parent_anchor_token=resolved_parent_anchor_token,
    )
    if result.dom_strategy is None:
        result.dom_strategy = reply_open.strategy

    # Network-level safety backstop against a reply landing under the wrong
    # comment. Acts ONLY on unambiguous positive evidence: the parent cid is
    # known AND TikTok's response reports a DIFFERENT non-zero parent. We never
    # fail on a missing parent in the response body, because a legitimate reply
    # response does not always carry a parent id; failing on absence would break
    # valid replies. The DOM-level scoping (reply button + reply input) is the
    # primary defense; this only catches a clearly cross-posted reply.
    expected_parent = str(parent_comment_id or "").strip()
    if result.status == "success" and expected_parent:
        observed_parent = _extract_reply_parent_id(getattr(result, "network_response_json", None))
        if observed_parent and observed_parent != expected_parent:
            logger.warning(
                "[REPLY] network parent mismatch: expected_parent=%s observed_parent=%s "
                "comment_id=%s corr=%s; rejecting so it is not counted as a reply",
                expected_parent[:24],
                observed_parent[:24],
                (result.comment_id or "")[:24] or None,
                correlation_id,
            )
            result.status = "failed"
            result.error_reason = "reply_parent_mismatch"
            result.comment_id = None
    return result
