import logging
from typing import Optional

from patchright.async_api import Page

logger = logging.getLogger("executor.captcha_detector")


CAPTCHA_TEXT_MARKERS = (
    "drag the slider",
    "fit the puzzle",
    "slide to verify",
    "complete verification",
    "security verification",
    "security check",
    "captcha",
    "verify you are",
    "verify your phone",
    "verify to post",
    "verify to comment",
    "phone verification",
    "email verification",
    "verification code",
    "enter the code",
    "two-factor",
    "two factor",
    "2fa",
    "2-step",
    "two-step",
    "confirm your identity",
    "account verification",
    "prove you are",
)


async def detect_captcha(page: Page, wait_ms: int = 1000) -> tuple[bool, Optional[str]]:
    if not page or page.is_closed():
        return False, None

    try:
        if wait_ms > 0:
            await page.wait_for_timeout(wait_ms)

        captcha_info = await page.evaluate(
            """
            () => {
                const textMarkers = [
                    'drag the slider',
                    'fit the puzzle',
                    'slide to verify',
                    'complete verification',
                    'security verification',
                    'security check',
                    'captcha',
                    'verify you are',
                    'verify your phone',
                    'verify to post',
                    'verify to comment',
                    'phone verification',
                    'email verification',
                    'verification code',
                    'enter the code',
                    'two-factor',
                    'two factor',
                    '2fa',
                    '2-step',
                    'two-step',
                    'confirm your identity',
                    'account verification',
                    'prove you are'
                ];

                const visible = (el) => {
                    if (!el || !el.getBoundingClientRect) return false;
                    const r = el.getBoundingClientRect();
                    const st = getComputedStyle(el);
                    const ariaHidden = String(el.getAttribute('aria-hidden') || '').toLowerCase() === 'true';
                    return r.width > 0
                        && r.height > 0
                        && st.display !== 'none'
                        && st.visibility !== 'hidden'
                        && st.opacity !== '0'
                        && st.pointerEvents !== 'none'
                        && !ariaHidden;
                };

                const classifyText = (text) => {
                    const normalized = String(text || '').toLowerCase();
                    if (!normalized) return null;
                    if (normalized.includes('drag the slider') || normalized.includes('fit the puzzle')) return 'slider';
                    if (normalized.includes('slide to verify')) return 'slider';
                    if (
                        normalized.includes('verify your phone') ||
                        normalized.includes('phone verification') ||
                        normalized.includes('email verification') ||
                        normalized.includes('verification code') ||
                        normalized.includes('enter the code') ||
                        normalized.includes('two-factor') ||
                        normalized.includes('two factor') ||
                        normalized.includes('2fa') ||
                        normalized.includes('2-step') ||
                        normalized.includes('two-step') ||
                        normalized.includes('confirm your identity') ||
                        normalized.includes('verify to post') ||
                        normalized.includes('verify to comment') ||
                        normalized.includes('account verification')
                    ) return 'account_verification';
                    if (textMarkers.some(marker => normalized.includes(marker))) return 'challenge';
                    return null;
                };

                const captchaSelectors = [
                    '#captcha-verify-image',
                    '[id*="captcha"]',
                    '[class*="captcha"]',
                    '[data-e2e*="captcha"]',
                    '[data-e2e*="challenge"]',
                    'input[autocomplete="one-time-code"]',
                    'iframe[src*="captcha"]',
                    'iframe[src*="verify"]',
                    'iframe[src*="challenge"]',
                    'iframe[src*="secsdk"]',
                ];

                const selectorHintType = (selector) => {
                    const selectorLower = String(selector || '').toLowerCase();
                    if (selectorLower.includes('one-time-code')) return 'account_verification';
                    if (selectorLower.includes('captcha')) return 'verify';
                    if (selectorLower.includes('challenge') || selectorLower.includes('secsdk')) return 'challenge';
                    return null;
                };

                for (const selector of captchaSelectors) {
                    try {
                        const elements = document.querySelectorAll(selector);
                        for (const el of elements) {
                            if (!visible(el)) continue;
                            const textType = classifyText(el.innerText || el.textContent || '');
                            if (textType) {
                                return { found: true, type: textType, selector };
                            }
                            const hintType = selectorHintType(selector);
                            if (hintType) {
                                return { found: true, type: hintType, selector };
                            }
                        }
                    } catch (e) {
                        continue;
                    }
                }

                const iframes = document.querySelectorAll('iframe');
                for (const iframe of iframes) {
                    try {
                        if (!visible(iframe)) continue;
                        const src = (iframe.getAttribute('src') || '').toLowerCase();
                        if (
                            src.includes('captcha') ||
                            src.includes('verify') ||
                            src.includes('challenge') ||
                            src.includes('secsdk')
                        ) {
                            return { found: true, type: 'challenge', selector: 'iframe' };
                        }
                    } catch (e) {
                        continue;
                    }
                }

                const normalizedText = (el) => String(el && (el.innerText || el.textContent) || '')
                    .toLowerCase()
                    .replace(/\\s+/g, ' ')
                    .trim();
                const visibleDescendants = (root, selector) => {
                    try {
                        return [...root.querySelectorAll(selector)].filter(visible);
                    } catch (_) {
                        return [];
                    }
                };
                const looksLikeTikTokPuzzleChallenge = (el) => {
                    if (!visible(el)) return false;
                    const r = el.getBoundingClientRect();
                    const vw = window.innerWidth || 0;
                    const vh = window.innerHeight || 0;
                    if (r.width < 260 || r.height < 220 || r.width > Math.max(760, vw * 0.75)) return false;
                    const cx = r.left + r.width / 2;
                    const cy = r.top + r.height / 2;
                    if (vw > 0 && (cx < vw * 0.25 || cx > vw * 0.82)) return false;
                    if (vh > 0 && (cy < vh * 0.22 || cy > vh * 0.90)) return false;

                    const text = normalizedText(el);
                    if (classifyText(text) === 'slider') return true;

                    const hasAudioControl =
                        /(^|\\s)audio($|\\s)/.test(text) ||
                        visibleDescendants(el, '[aria-label*="audio" i], button, [role="button"]').some((node) => {
                            const label = `${node.getAttribute('aria-label') || ''} ${node.innerText || node.textContent || ''}`.toLowerCase();
                            return /(^|\\s)audio($|\\s)/.test(label);
                        });
                    if (!hasAudioControl) return false;

                    const mediaLike = visibleDescendants(
                        el,
                        'img, canvas, svg, [style*="background-image"], [class*="Image"], [class*="image"]'
                    ).filter((node) => {
                        try {
                            const br = node.getBoundingClientRect();
                            return br.width >= 48 && br.height >= 48;
                        } catch (_) {
                            return false;
                        }
                    });

                    const controls = visibleDescendants(el, 'button, [role="button"], [tabindex], input[type="range"], [role="slider"]');
                    const hasSliderLikeControl = controls.some((node) => {
                        try {
                            const br = node.getBoundingClientRect();
                            const label = `${node.getAttribute('aria-label') || ''} ${node.innerText || node.textContent || ''}`.toLowerCase();
                            return node.matches('[role="slider"], input[type="range"]') ||
                                label.includes('slide') ||
                                label.includes('drag') ||
                                label.includes('verify') ||
                                (br.width >= 44 && br.height >= 28 && br.left >= r.left && br.right <= r.right && br.top > r.top + r.height * 0.45);
                        } catch (_) {
                            return false;
                        }
                    });

                    const iconLikeButtons = controls.filter((node) => {
                        try {
                            const br = node.getBoundingClientRect();
                            return br.width >= 18 && br.height >= 18 && br.width <= 90 && br.height <= 90;
                        } catch (_) {
                            return false;
                        }
                    }).length;

                    return mediaLike.length > 0 && (hasSliderLikeControl || iconLikeButtons >= 2);
                };

                const modalCandidates = [
                    ...document.querySelectorAll('.TUXModal-overlay, [class*="Modal"], [class*="modal"], [role="dialog"]')
                ];
                for (const el of modalCandidates) {
                    if (!visible(el)) continue;
                    const textType = classifyText(el.innerText || el.textContent || '');
                    if (textType) {
                        return { found: true, type: textType, selector: 'modal_text' };
                    }
                    if (looksLikeTikTokPuzzleChallenge(el)) {
                        return { found: true, type: 'slider', selector: 'visual_puzzle_modal' };
                    }
                }

                // During navigation the document can exist before its body.
                const structuralCandidates = [...document.querySelectorAll('body > div, body > section, body > aside')]
                    .filter(visible)
                    .filter((el) => {
                        try {
                            const st = getComputedStyle(el);
                            const r = el.getBoundingClientRect();
                            return /fixed|absolute|sticky/i.test(st.position || '') &&
                                r.width >= 260 &&
                                r.height >= 220;
                        } catch (_) {
                            return false;
                        }
                    });
                for (const el of structuralCandidates) {
                    if (looksLikeTikTokPuzzleChallenge(el)) {
                        return { found: true, type: 'slider', selector: 'visual_puzzle_overlay' };
                    }
                }

                return { found: false, type: null, selector: null };
            }
            """
        )

        if captcha_info and captcha_info.get("found"):
            captcha_type = captcha_info.get("type", "verify")
            selector = captcha_info.get("selector", "unknown")
            logger.warning("[CAPTCHA] Captcha detected: type=%s, selector=%s", captcha_type, selector)
            return True, captcha_type

        return False, None
    except Exception as exc:
        logger.debug("[CAPTCHA] Error detecting captcha: %s", exc)
        return False, None


async def check_captcha_before_action(page: Page) -> tuple[bool, Optional[str]]:
    return await detect_captcha(page, wait_ms=500)
