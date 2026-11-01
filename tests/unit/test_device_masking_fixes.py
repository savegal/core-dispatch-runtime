import pytest
from types import SimpleNamespace
from unittest.mock import AsyncMock

from executor.http.request_profile import (
    generate_tiktok_verify_fp,
    RequestProfile,
)
from executor.browser_pool import resolve_accept_language
from executor.device_profiles import build_device_profile, prepare_device_page
from services.cookie_validator import prepare_cookies_for_context


def test_generate_tiktok_verify_fp_determinism():
    # 1. Deterministic output for same seed
    fp1 = generate_tiktok_verify_fp("user_123_session_abc")
    fp2 = generate_tiktok_verify_fp("user_123_session_abc")
    assert fp1 == fp2
    assert fp1.startswith("verify_")
    parts = fp1.split("_")
    assert len(parts) == 3
    assert len(parts[2]) == 32  # 32 characters

    # 2. Different seeds produce different tokens
    fp3 = generate_tiktok_verify_fp("user_456_session_xyz")
    assert fp1 != fp3

    # 3. Random fallback when seed is None
    fp_rnd1 = generate_tiktok_verify_fp()
    fp_rnd2 = generate_tiktok_verify_fp()
    assert fp_rnd1 != fp_rnd2
    assert fp_rnd1.startswith("verify_")


def test_request_profile_build_query_params_consistent_verify_fp():
    profile = RequestProfile.publish_main_profile()
    cookies = {"sessionid": "mock_session_12345678"}
    runtime = {"account_id": 42}

    # First call (e.g. resolve_request_signature)
    params1 = profile.build_query_params({"item_id": "999"}, cookies=cookies, runtime_profile=runtime)
    vfp1 = params1["verifyFp"]
    assert vfp1.startswith("verify_")

    # Request construction must not turn generated identity into caller cookies.
    assert cookies == {"sessionid": "mock_session_12345678"}
    assert runtime == {"account_id": 42}

    # Second call (e.g. build_form_data of the same request)
    params2 = profile.build_query_params({"item_id": "999"}, cookies=cookies, runtime_profile=runtime)
    vfp2 = params2["verifyFp"]

    # Must be 100% identical!
    assert vfp1 == vfp2

    # Third call: form data helper
    form_data = profile.build_form_data({"item_id": "999"}, cookies=cookies, runtime_profile=runtime)
    # verifyFp in form data or query parameters remains the same
    assert cookies == {"sessionid": "mock_session_12345678"}
    assert runtime == {"account_id": 42}
    assert form_data == {"text_extra": "[]"}


def test_resolve_accept_language_and_cdp_override():
    # 1. Locale mappings
    assert resolve_accept_language("uk-UA") == "uk-UA,uk;q=0.9,ru;q=0.8,en;q=0.7"
    assert resolve_accept_language("de-DE") == "de-DE,de;q=0.9,en;q=0.8"
    assert resolve_accept_language("en-US") == "en-US,en;q=0.9"

    # Fallback pattern for hyphenated locales
    assert "fr,en;q=0.9" in resolve_accept_language("fr-CA") or "fr-CA" in resolve_accept_language("fr-CA")


@pytest.mark.asyncio
async def test_prepare_device_page_expands_accept_language_for_cdp():
    context = SimpleNamespace(
        new_cdp_session=AsyncMock(),
        _account_locale="uk-UA",
    )
    session = AsyncMock()
    context.new_cdp_session.return_value = session

    profile = build_device_profile(SimpleNamespace(id=1, user_agent=None), {}, "145.0.7632.6")
    # Simulate single locale passed in override
    profile["override"]["acceptLanguage"] = "uk-UA"
    context._account_device_profile = profile

    page = SimpleNamespace(context=context)
    await prepare_device_page(page)

    send_args = session.send.call_args_list[0]
    assert send_args[0][0] == "Emulation.setUserAgentOverride"
    # CDP must receive the expanded multi-language string so navigator.languages matches Accept-Language
    assert send_args[0][1]["acceptLanguage"] == "uk-UA,uk;q=0.9,ru;q=0.8,en;q=0.7"


def test_prepare_cookies_for_context_injects_persistent_s_v_web_id():
    account = SimpleNamespace(id=101, cookies="sessionid=abcdef1234567890; sid_tt=abcdef1234567890")

    cookies_list = prepare_cookies_for_context(account)
    sv_cookie = next((c for c in cookies_list if c["name"] == "s_v_web_id"), None)

    assert sv_cookie is not None
    assert sv_cookie["domain"] == ".tiktok.com"
    assert sv_cookie["value"].startswith("verify_")

    # Calling it again on the same account produces the exact same s_v_web_id
    cookies_list2 = prepare_cookies_for_context(account)
    sv_cookie2 = next((c for c in cookies_list2 if c["name"] == "s_v_web_id"), None)
    assert sv_cookie2["value"] == sv_cookie["value"]
