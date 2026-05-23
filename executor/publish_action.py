from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import time
from dataclasses import dataclass
from typing import Any, Dict, Optional

from patchright.async_api import Page, Response

from executor.dom_actions import (
    find_comment_input_smart,
    find_post_button_smart,
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

MANUAL_CAPTCHA_WAIT_SECONDS = float(os.getenv("MANUAL_CAPTCHA_WAIT_SECONDS", "30"))
MANUAL_CAPTCHA_POLL_SECONDS = 2.0
_TRUE_ENV_VALUES = {"1", "true", "yes", "on"}
_TIKTOK_MEDIA_ID_RE = re.compile(r"(?:/(?:video|photo)/|[?&](?:aweme_id|item_id)=)(\d+)", re.IGNORECASE)


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
    if str(state.get("state") or "").lower() != "logged_out":
        return None
    return (
        f"visible_login_ui:{phase}:"
        f"login={int(state.get('loginCount') or 0)}:"
        f"modal={int(state.get('loginModalCount') or 0)}:"
        f"comment_cta={int(state.get('commentLoginCtaCount') or 0)}:"
        f"state_false={bool(state.get('stateIsLoginFalse'))}"
    )


async def _logged_out_publish_result_if_visible(
    page: Page,
    started: float,
    *,
    phase: str,
    correlation_id: Optional[str],
    expected_video_url: Optional[str],
) -> Optional[PublishResult]:
    try:
        state = await get_browser_login_state(page)
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
    if raw is not None and raw.strip().lower() not in _TRUE_ENV_VALUES:
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
                    'Play / Pause'
                ];

                const hasMarker = (txt) => {
                    txt = String(txt || '');
                    if (txt.includes('Introducing keyboard shortcuts')) return true;
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
                        'webapp_keyboard_shortcuts_seen'
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
                        else {
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
                            else {
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


async def _clear_post_click_blockers(page: Page, post_button: Any, *, max_rounds: int = 5) -> tuple[Any, Optional[dict[str, Any]], bool]:
    """Clear overlays blocking the center of Post button.

    Returns: (fresh_post_button, last_point_diag, is_clear)
    """
    last_diag = None
    for round_index in range(max_rounds):
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

            if "introducing keyboard shortcuts" in text:
                logger.warning("[PUBLISH] Post click point is blocked by keyboard shortcuts overlay. Hard-closing it.")
                closed_keyboard = await _close_keyboard_shortcuts_overlay(page)
                logger.info("[PUBLISH] Keyboard shortcuts overlay closed before Post click: %s", closed_keyboard)
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
                        if ((r.width <= 1000 && r.height <= 1000) || txt.includes('Introducing keyboard shortcuts') || isCookieBanner || isBottomBand) {
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


async def _publish_via_current_input(
    page: Page,
    text: str,
    *,
    max_total_seconds: float,
    correlation_id: Optional[str],
    expected_video_url: Optional[str] = None,
) -> PublishResult:
    started = time.monotonic()
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

    input_handle, input_info = await find_comment_input_smart(
        page,
        max_total_seconds=min(12.0, max_total_seconds * 0.7),
    )
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

    # dismiss_overlays intentionally removed here — calling it after finding the
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

    await ensure_click_target_clear(page, input_handle)
    captcha_result = await _captcha_result_if_present(
        page,
        started,
        dom_strategy=input_info.strategy,
    )
    if captcha_result:
        return captcha_result

    if not await type_into_comment(page, input_handle, text):
        logger.warning("[PUBLISH] type_into_comment failed on first attempt — retrying with fresh input handle")
        await asyncio.sleep(0.5)
        retry_handle, _ = await find_comment_input_smart(page, max_total_seconds=5.0)
        if not retry_handle:
            retry_handle = input_handle
        if not await type_into_comment(page, retry_handle, text):
            return PublishResult(
                status="dom_error",
                error_reason="typing_failed",
                dom_strategy=input_info.strategy,
                elapsed_seconds=time.monotonic() - started,
            )
        input_handle = retry_handle

    await asyncio.sleep(0.8)  # let TikTok render Post after robust input

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
    post_button, post_info = await find_post_button_smart(page, max_total_seconds=min(4.0, max(remaining, 1.0)))
    if not post_button:
        captcha_result = await _captcha_result_if_present(
            page,
            started,
            dom_strategy=input_info.strategy,
        )
        if captcha_result:
            return captcha_result
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
    enabled_button, enabled_info = await wait_for_post_button_enabled(
        page,
        max_wait_seconds=min(8.0, max(remaining, 3.0)),
    )
    # Track whether Post passed the enabled-gate. False → the click below will
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

    async def on_response(response: Response):
        try:
            url = response.url
        except Exception:
            return
        if "/api/comment/publish/" not in url:
            return
        captured["url"] = url
        captured["status"] = response.status
        try:
            body = await response.text()
            captured["body"] = body
            try:
                captured["json"] = json.loads(body) if body else None
            except Exception:
                captured["json"] = None
        except Exception as exc:
            captured["body_error"] = str(exc)[:100]
        event.set()

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

    page.on("request", on_request)
    page.on("response", on_response)
    # Любая нештатная ситуация ниже (capture click, captcha probe, retry click,
    # post-click diag) должна снимать listener — иначе при переиспользовании
    # page через browser_pool слушатели накапливаются и каждое новое
    # на /api/comment/publish/ выполняется N раз. _detach_listener идём
    # дёргать перед каждым return-выходом из этого блока.
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
        _listener_removed = True

    # Do NOT call dismiss_overlays() after typing.
    # At this point TikTok/React already has the text and the focused input.
    # A late overlay cleanup can steal focus, change pointer-events or make the
    # previously found Post button stale. This was the likely reason for
    # no_network_request_after_post_click after a verified input text.
    await ensure_click_target_clear(page, post_button)
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
    try:
        post_button, point_diag, click_point_clear = await _clear_post_click_blockers(
            page,
            post_button,
            max_rounds=6,
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
            await asyncio.sleep(0.35)
            retry_fresh, retry_fresh_info = await wait_for_post_button_enabled(page, max_wait_seconds=2.0)
            if retry_fresh:
                post_button = retry_fresh
                post_was_enabled = True
                logger.info("[PUBLISH] Fresh Post button selected after final suppress via %s", retry_fresh_info.strategy)
    except Exception as clear_exc:
        logger.warning("[PUBLISH] Clearing post click blockers failed: %s", clear_exc)

    # Final pre-click check: blocker clearance may have transitioned Post from
    # disabled → enabled. If a probe finds it enabled now, mark post_was_enabled.
    # If it's still disabled and we fall through to synthetic dispatch, the
    # publish result will be classified as unverified (see below).
    try:
        _gate_button, _gate_info = await wait_for_post_button_enabled(page, max_wait_seconds=0.5)
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
            "[PUBLISH] elementFromPoint=BODY → button has pointer-events:none. Will use synthetic dispatchEvent."
        )

    synthetic_dispatch_attempted = False
    try:
        box = await post_button.bounding_box()
        if box:
            x = box["x"] + box["width"] / 2
            y = box["y"] + box["height"] / 2

            if _use_synthetic_click:
                # Temporarily restore pointer-events and fire synthetic click.
                dispatched = await page.evaluate(
                    r"""
                    ({x, y}) => {
                        const candidates = [
                            ...document.querySelectorAll(
                                '[data-smart-id="comment-post-ready"], [data-smart-id="comment-post"], [data-e2e="comment-post"]'
                            )
                        ];
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
                    {"x": x, "y": y},
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
            # Prefer a fresh DOM locator over a stale ElementHandle.
            locator = page.locator('[data-smart-id="comment-post-ready"], [data-e2e="comment-post"]').last
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
                    _detach_listener()
                    return PublishResult(
                        status="dom_error",
                        error_reason=f"post_click_failed: mouse={str(exc)[:40]} locator={str(locator_exc)[:40]} pw={str(pw_exc)[:40]} js={str(js_exc)[:40]}",
                        dom_strategy=input_info.strategy,
                        elapsed_seconds=time.monotonic() - started,
                    )

    wait_remaining = max_total_seconds - (time.monotonic() - started)
    try:
        await asyncio.wait_for(event.wait(), timeout=min(6.0, max(wait_remaining, 2.0)))
    except asyncio.TimeoutError:
        pass

    if not captured and synthetic_dispatch_attempted:
        logger.warning(
            "[PUBLISH] Synthetic Post dispatch produced no network confirmation; "
            "skipping immediate second click to avoid duplicate comments. "
            "Caller should verify DOM before retrying another account."
        )
    elif not captured:
        # Last clean attempt: if the first mouse click closed a toast or focused the page
        # instead of publishing, reselect the visible Post button and try one normal click.
        try:
            retry_button, retry_info = await wait_for_post_button_enabled(
                page,
                max_wait_seconds=2.0,
            )
            if retry_button:
                retry_box = await retry_button.bounding_box()
                if retry_box:
                    rx = retry_box["x"] + retry_box["width"] / 2
                    ry = retry_box["y"] + retry_box["height"] / 2
                    retry_button, retry_diag, retry_clear = await _clear_post_click_blockers(
                        page,
                        retry_button,
                        max_rounds=4,
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
                        logger.warning("[PUBLISH] Retry: elementFromPoint=BODY → using synthetic dispatchEvent.")
                        await page.evaluate(
                            r"""
                            () => {
                                const btn = [...document.querySelectorAll(
                                    '[data-smart-id="comment-post-ready"], [data-smart-id="comment-post"], [data-e2e="comment-post"]'
                                )].find(el => { const r = el.getBoundingClientRect(); return r.width > 0 && r.height > 0; });
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
                            """
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
                        await asyncio.wait_for(event.wait(), timeout=4.0)
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
                        "[PUBLISH] comment_id present but status_code=%s — treating as success; full json: %s",
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
) -> PublishResult:
    started = time.monotonic()
    drift_result = _publish_url_guard_result(
        page,
        expected_video_url,
        started,
        phase="open_comments",
        correlation_id=correlation_id,
    )
    if drift_result:
        return drift_result
    logged_out_result = await _logged_out_publish_result_if_visible(
        page,
        started,
        phase="open_comments",
        correlation_id=correlation_id,
        expected_video_url=expected_video_url,
    )
    if logged_out_result:
        return logged_out_result
    await open_comment_section(page, max_wait=5.0, require_input=True)
    return await _publish_via_current_input(
        page,
        text,
        max_total_seconds=max_total_seconds,
        correlation_id=correlation_id,
        expected_video_url=expected_video_url,
    )


async def publish_reply_hybrid(
    page: Page,
    text: str,
    *,
    parent_comment_text: Optional[str] = None,
    parent_comment_id: Optional[str] = None,
    max_total_seconds: float = 15.0,
    correlation_id: Optional[str] = None,
    expected_video_url: Optional[str] = None,
) -> PublishResult:
    started = time.monotonic()
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
    )
    if logged_out_result:
        return logged_out_result
    await open_comment_section(page)
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
    )
    if logged_out_result:
        return logged_out_result
    reply_open = await open_reply_box(page, parent_comment_text, parent_comment_id)
    if not reply_open.success:
        logged_out_result = await _logged_out_publish_result_if_visible(
            page,
            started,
            phase="reply_box_failed",
            correlation_id=correlation_id,
            expected_video_url=expected_video_url,
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
            error_reason=reply_open.reason or "reply_button_not_found",
            elapsed_seconds=time.monotonic() - started,
        )
    remaining = max_total_seconds - (time.monotonic() - started)
    result = await _publish_via_current_input(
        page,
        text,
        max_total_seconds=max(remaining, 8.0),
        correlation_id=correlation_id,
        expected_video_url=expected_video_url,
    )
    if result.dom_strategy is None:
        result.dom_strategy = reply_open.strategy
    return result
