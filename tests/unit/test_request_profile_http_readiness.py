import pytest
from types import SimpleNamespace

from executor.http.request_profile import RequestProfile
from executor.engagement_engine import validate_request_profile, _is_degraded_http_profile
import executor.tiktok_client_async as tiktok_client_async


@pytest.mark.unit
def test_publish_main_profile_has_secsdk_csrf_token():
    profile = RequestProfile.publish_main_profile()

    assert profile.static_headers["x-secsdk-csrf-token"] == "DOWNGRADE"


@pytest.mark.unit
def test_build_headers_promotes_cookie_csrf_to_both_headers():
    profile = RequestProfile.publish_main_profile()

    headers = profile.build_headers(cookies={"tt_csrf_token": "csrf_cookie_value"})

    assert headers["tt-csrf-token"] == "csrf_cookie_value"
    assert headers["x-secsdk-csrf-token"] == "csrf_cookie_value"


@pytest.mark.unit
def test_runtime_profile_overrides_headers_and_query_shape():
    profile = RequestProfile.publish_main_profile()
    runtime_profile = {
        "user_agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/145.0.0.0 Safari/537.36",
        "browser_language": "en-US",
        "languages": ["en-US", "en", "ru-RU"],
        "timezone": "America/New_York",
        "platform": "Win32",
        "sec_ch_ua_platform": "Windows",
        "sec_ch_ua_brands": [
            {"brand": "Google Chrome", "version": "145"},
            {"brand": "Chromium", "version": "145"},
        ],
        "screen_width": 1920,
        "screen_height": 1080,
        "history_length": 4,
        "is_page_visible": True,
        "focus_state": True,
        "browser_online": True,
        "cookie_enabled": True,
        "current_url": "https://www.tiktok.com/@demo/video/1234567890",
    }

    headers = profile.build_headers(
        cookies={"tt_csrf_token": "csrf_cookie_value"},
        runtime_profile=runtime_profile,
    )
    url = profile.build_url(
        {"aweme_id": "1234567890", "text": "hello"},
        cookies={"device_id": "device_1", "sessionid": "session_1"},
        runtime_profile=runtime_profile,
    )

    assert "user-agent" not in headers
    assert headers["accept-language"].startswith("en-US")
    assert headers["sec-ch-ua-mobile"] == "?0"
    assert headers["referer"] == runtime_profile["current_url"]
    assert "browser_language=en-US" in url
    assert "screen_width=1920" in url
    assert "screen_height=1080" in url
    assert "tz_name=America%2FNew_York" in url
    assert "region=US" in url
    assert "priority_region=US" in url


@pytest.mark.unit
def test_build_headers_keeps_sec_ch_headers_consistent_with_explicit_user_agent():
    profile = RequestProfile.publish_main_profile()
    explicit_user_agent = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/146.0.0.0 Safari/537.36"

    headers = profile.build_headers(cookies={}, user_agent=explicit_user_agent)

    assert "user-agent" not in headers
    assert 'v="146"' in headers["sec-ch-ua"]
    assert headers["sec-ch-ua-platform"] == '"Windows"'


@pytest.mark.unit
def test_missing_mstoken_is_degraded_not_invalid():
    is_valid, missing_critical, missing_optional = validate_request_profile(
        {
            "verifyFp": "verify",
            "device_id": "device",
            "sessionid": "session",
        }
    )

    assert is_valid is True
    assert missing_critical == []
    assert "msToken" in missing_optional
    assert _is_degraded_http_profile({"status": "ok", "missing_optional": missing_optional}) is True


@pytest.mark.unit
def test_missing_session_cookie_is_invalid():
    is_valid, missing_critical, missing_optional = validate_request_profile(
        {
            "verifyFp": "verify",
            "device_id": "device",
            "msToken": "ms-token",
        }
    )

    assert is_valid is False
    assert "sessionid" in missing_critical
    assert "msToken" not in missing_optional


@pytest.mark.unit
@pytest.mark.asyncio
async def test_quick_validate_account_short_circuits_without_session_cookie(monkeypatch):
    async def _should_not_create_page(*args, **kwargs):
        raise AssertionError("browser should not start for dead cookies")

    monkeypatch.setattr(tiktok_client_async, "create_page_for_account", _should_not_create_page)

    account = SimpleNamespace(
        id=1,
        label="acc",
        cookies="verifyFp=verify; device_id=device",
        cookie_file=None,
        proxy=None,
    )

    result = await tiktok_client_async.quick_validate_account(account, timeout=1)

    assert result is False


@pytest.mark.unit
@pytest.mark.asyncio
async def test_quick_validate_account_rejects_empty_loaded_dom(monkeypatch):
    class _Page:
        url = "https://www.tiktok.com/"

        async def goto(self, *_args, **_kwargs):
            return None

        async def wait_for_timeout(self, *_args, **_kwargs):
            return None

        async def title(self):
            return "TikTok"

        async def query_selector_all(self, _selector):
            return []

        async def content(self):
            return "<html><body><div></div></body></html>"

        async def evaluate(self, _script):
            return {"textLen": 0, "nodeCount": 1}

        async def close(self):
            return None

    async def _create_page(*_args, **_kwargs):
        return SimpleNamespace(), _Page()

    monkeypatch.setattr(tiktok_client_async, "create_page_for_account", _create_page)
    monkeypatch.setattr(tiktok_client_async.settings, "VALIDATION_WAIT_AFTER_LOAD_MS", 0)

    account = SimpleNamespace(
        id=1,
        label="acc",
        cookies="sid_guard=guard",
        cookie_file=None,
        proxy=None,
    )

    result = await tiktok_client_async.quick_validate_account(account, timeout=1)

    assert result is False


@pytest.mark.unit
@pytest.mark.asyncio
async def test_recover_comment_id_from_dom_rejects_zero_cid(monkeypatch):
    async def fake_recover(*_args, **_kwargs):
        return {"comment_id": "0"}

    monkeypatch.setattr(tiktok_client_async, "_recover_comment_from_dom", fake_recover)

    recovered = await tiktok_client_async._recover_comment_id_from_dom(
        page=SimpleNamespace(),
        expected_text="hello",
        phase="test",
    )

    assert recovered is None
