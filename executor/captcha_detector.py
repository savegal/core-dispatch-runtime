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
    "sent to",
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
                    'sent to',
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
                    return r.width > 0 && r.height > 0 && st.display !== 'none' && st.visibility !== 'hidden';
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
                        normalized.includes('sent to') ||
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
                ];

                for (const selector of captchaSelectors) {
                    try {
                        const elements = document.querySelectorAll(selector);
                        for (const el of elements) {
                            if (!visible(el)) continue;
                            const textType = classifyText(el.innerText || el.textContent || '');
                            if (textType) {
                                return { found: true, type: textType, selector };
                            }
                            const selectorLower = selector.toLowerCase();
                            if (
                                selectorLower.includes('one-time-code')
                            ) {
                                return { found: true, type: 'account_verification', selector };
                            }
                            if (selectorLower.includes('captcha')) {
                                return { found: true, type: 'verify', selector };
                            }
                            if (selectorLower.includes('challenge')) {
                                return { found: true, type: 'challenge', selector };
                            }
                        }
                    } catch (e) {
                        continue;
                    }
                }

                const iframes = document.querySelectorAll('iframe');
                for (const iframe of iframes) {
                    try {
                        const src = (iframe.getAttribute('src') || '').toLowerCase();
                        if (src.includes('captcha') || src.includes('verify') || src.includes('challenge')) {
                            return { found: true, type: 'challenge', selector: 'iframe' };
                        }
                    } catch (e) {
                        continue;
                    }
                }

                const modalCandidates = [
                    ...document.querySelectorAll('.TUXModal-overlay, [class*="Modal"], [class*="modal"], [role="dialog"]')
                ];
                for (const el of modalCandidates) {
                    if (!visible(el)) continue;
                    const textType = classifyText(el.innerText || el.textContent || '');
                    if (textType) {
                        return { found: true, type: textType, selector: 'modal_text' };
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
