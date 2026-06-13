import inspect

import pytest

from executor.captcha_detector import CAPTCHA_TEXT_MARKERS, detect_captcha


@pytest.mark.unit
def test_captcha_detector_knows_account_verification_markers():
    source = inspect.getsource(detect_captcha)

    assert "verification code" in CAPTCHA_TEXT_MARKERS
    assert "verify your phone" in CAPTCHA_TEXT_MARKERS
    assert "two-factor" in CAPTCHA_TEXT_MARKERS
    assert "account_verification" in source
    assert 'input[autocomplete="one-time-code"]' in source


@pytest.mark.unit
def test_captcha_detector_avoids_broad_user_text_and_verified_badge_markers():
    source = inspect.getsource(detect_captcha)

    assert "robot" not in CAPTCHA_TEXT_MARKERS
    assert "human" not in CAPTCHA_TEXT_MARKERS
    assert "'robot'" not in source
    assert "'human'" not in source
    assert '[class*="verify"]' not in source
    assert "selectorLower.includes('captcha') || selectorLower.includes('verify')" not in source
    assert "selectorLower.includes('verification')" not in source
    assert "'[role=\"dialog\"]'" not in source[source.find("const captchaSelectors"):source.find("const iframes")]
    assert '[class*="verify"]' not in source


@pytest.mark.unit
@pytest.mark.asyncio
async def test_captcha_detector_returns_account_verification_type():
    class _Page:
        def is_closed(self):
            return False

        async def evaluate(self, _script):
            return {
                "found": True,
                "type": "account_verification",
                "selector": "modal_text",
            }

    found, captcha_type = await detect_captcha(_Page(), wait_ms=0)

    assert found is True
    assert captcha_type == "account_verification"


@pytest.mark.unit
def test_captcha_detector_has_visual_puzzle_modal_heuristic_without_broad_verify_class():
    source = inspect.getsource(detect_captcha)

    assert "looksLikeTikTokPuzzleChallenge" in source
    assert "visual_puzzle_modal" in source
    assert "visual_puzzle_overlay" in source
    assert "audio" in source
    assert '[class*="verify"]' not in source


@pytest.mark.unit
def test_captcha_detector_requires_visible_iframe_and_avoids_broad_sent_to_marker():
    source = inspect.getsource(detect_captcha)

    assert "if (!visible(iframe)) continue;" in source
    assert "st.opacity !== '0'" in source
    assert "st.pointerEvents !== 'none'" in source
    assert "sent to" not in CAPTCHA_TEXT_MARKERS
    assert "'sent to'" not in source
    assert "'[id*=\"secsdk\"]'" not in source
    assert "'[class*=\"secsdk\"]'" not in source
    assert "'iframe[src*=\"secsdk\"]'" in source
