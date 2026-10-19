import pytest
from types import SimpleNamespace
from urllib.parse import parse_qs, urlparse

from executor.http.request_profile import RequestProfile, resolve_request_geo
from executor.http.tiktok_endpoints import TikTokEndpoints
from executor.engagement_engine import validate_request_profile, _is_degraded_http_profile
import executor.tiktok_client_async as tiktok_client_async


@pytest.fixture(autouse=True)
def proxied_geo_defaults(monkeypatch):
    from config.settings import get_settings
    monkeypatch.setattr(get_settings(), "USE_PROXY", True)


@pytest.mark.parametrize("cookie_country", ["useast5", "alisg", "eu"])
def test_direct_geo_ignores_account_proxy_document_and_cookie_hints(monkeypatch, cookie_country):
    from config.settings import get_settings
    from executor.http import request_profile
    monkeypatch.setattr(get_settings(), "USE_PROXY", False)
    monkeypatch.setattr(request_profile, "_executor_language", lambda: "ru-RU")
    runtime = {"timezone": "Asia/Jerusalem", "browser_language": "ru-RU", "region": "US",
               "proxy_country_code": "SG", "account_country_code": "US", "locale": "en-US"}
    geo = resolve_request_geo(runtime, {"tt-target-idc": cookie_country, "store-region": "us"})
    assert geo["tz_name"] == "Asia/Jerusalem"
    assert geo["region"] == geo["priority_region"] == "RU"
    assert geo["browser_language"] == "ru-RU"
    # An explicitly recorded direct route wins even if the global flag changes.
    monkeypatch.setattr(get_settings(), "USE_PROXY", True)
    assert resolve_request_geo({**runtime, "proxy_in_use": False}) == geo


def test_actual_proxy_route_uses_proxy_geo_even_when_global_flag_is_off(monkeypatch):
    from config.settings import get_settings
    monkeypatch.setattr(get_settings(), "USE_PROXY", False)
    geo = resolve_request_geo({"proxy_in_use": True, "proxy_country_code": "PL", "account_country_code": "US"})
    assert geo["region"] == "PL" and geo["tz_name"] == "Europe/Warsaw"


def test_direct_geo_without_browser_uses_system_timezone(monkeypatch):
    from config.settings import get_settings
    from executor.http import request_profile
    import tzlocal
    monkeypatch.setattr(get_settings(), "USE_PROXY", False)
    monkeypatch.setattr(tzlocal, "get_localzone_name", lambda: "Asia/Jerusalem")
    monkeypatch.setattr(request_profile, "_executor_language", lambda: "uk-UA")
    geo = resolve_request_geo(cookies={"tt-target-idc": "useast5"})
    assert geo["tz_name"] == "Asia/Jerusalem" and geo["region"] == "UA"


@pytest.mark.parametrize("os_locale,region", [
    ("ru-RU", "RU"), ("ru-UA", "UA"), ("uk-UA", "UA"), ("en-US", "US"),
    ("en-GB", "GB"), ("en", None), ("he-IL", "IL"),
])
def test_jerusalem_timezone_does_not_force_country_or_hebrew(monkeypatch, os_locale, region):
    from config.settings import get_settings
    from executor.http import request_profile
    monkeypatch.setattr(get_settings(), "USE_PROXY", False)
    monkeypatch.setattr(request_profile, "_executor_language", lambda: os_locale)
    runtime = {"timezone":"Asia/Jerusalem", "locale":"he-IL", "region":"IL"}
    cookies = {"store-region":"il", "locale":"he-IL", "tt-target-idc":"useast5"}
    geo = resolve_request_geo(runtime, cookies)
    assert geo["tz_name"] == "Asia/Jerusalem"
    assert geo.get("region") == geo.get("priority_region") == region
    assert geo["browser_language"] == os_locale
    headers = RequestProfile.publish_main_profile().build_headers(cookies=cookies, runtime_profile=runtime)
    assert headers["accept-language"] == os_locale


def test_direct_geo_uses_browser_locale_when_os_locale_unavailable(monkeypatch):
    from config.settings import get_settings
    from executor.http import request_profile
    monkeypatch.setattr(get_settings(), "USE_PROXY", False)
    monkeypatch.setattr(request_profile, "_executor_language", lambda: None)
    geo = resolve_request_geo({"timezone":"Asia/Jerusalem", "browser_language":"uk-UA"})
    assert geo["region"] == "UA" and geo["browser_language"] == "uk-UA"


@pytest.mark.parametrize("browser_language,os_language,expected_language,region", [
    ("ru-RU", "ru-UA", "ru-RU", "RU"),
    ("uk_UA", "ru-RU", "uk-UA", "UA"),
    ("en-GB", "he-IL", "en-GB", "GB"),
])
def test_direct_geo_prioritizes_navigator_language_over_os(monkeypatch, browser_language, os_language, expected_language, region):
    from executor.http import request_profile
    monkeypatch.setattr(request_profile, "_executor_language", lambda: os_language)
    runtime = {"proxy_in_use": False, "browser_language": browser_language, "timezone": "Asia/Jerusalem"}
    profile = RequestProfile.comment_list_profile()
    query = profile.build_query_params({"aweme_id": "123"}, runtime_profile=runtime)
    assert query["browser_language"] == query["app_language"] == query["webcast_language"] == expected_language
    assert query["region"] == query["priority_region"] == region
    assert profile.build_headers(runtime_profile=runtime)["accept-language"] == expected_language


@pytest.mark.unit
@pytest.mark.parametrize("factory", [RequestProfile.publish_main_profile, RequestProfile.publish_reply_profile, RequestProfile.like_profile])
def test_publish_profiles_do_not_force_secsdk_handshake_headers(factory):
    profile = factory()

    assert not any(key.startswith("x-secsdk-") for key in profile.static_headers)


@pytest.mark.unit
def test_build_headers_keeps_cookie_csrf_separate_from_secsdk_token():
    profile = RequestProfile.publish_main_profile()

    headers = profile.build_headers(cookies={"tt_csrf_token": "csrf_cookie_value"})

    assert headers["tt-csrf-token"] == "csrf_cookie_value"
    assert not any(key.startswith("x-secsdk-") for key in headers)


@pytest.mark.parametrize("factory", [RequestProfile.publish_main_profile, RequestProfile.publish_reply_profile, RequestProfile.comment_list_profile])
def test_page_query_can_delegate_ms_token_without_losing_other_fresh_identifiers(factory):
    profile = factory()
    params, form = profile.build_request_parts(
        {"aweme_id": "123", "text": "hello", "msToken": "old-dynamic", "ms_token": "old-alias"},
        {"msToken": "old-cookie", "WebIdLastTime": "fresh-time", "odin_tt": "device"},
        include_ms_token=False,
    )
    assert "msToken" not in params and "ms_token" not in params
    assert params["WebIdLastTime"] == "fresh-time"
    assert params["odinId"] == "device"
    if profile.method == "POST":
        assert form["text"] == "hello"
    assert profile.build_query_params({}, {"msToken": "direct-token"})["msToken"] == "direct-token"


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
    assert "browser_version=5.0+" in url
    assert "browser_version=Mozilla" not in url
    assert "screen_width=1920" in url
    assert "screen_height=1080" in url
    assert "tz_name=America%2FNew_York" in url
    assert "region=US" in url
    assert "priority_region=US" in url

    runtime_ids = {
        **runtime_profile,
        "device_id": "7499999999999999999",
        "odin_id": "7488888888888888888",
        "web_id_last_time": "1740000000",
    }
    params = profile.build_query_params({"aweme_id": "1234567890", "text": "hello"}, cookies={"sessionid": "session_1"}, runtime_profile=runtime_ids)
    assert params["device_id"] == "7499999999999999999"
    assert params["odinId"] == "7488888888888888888"
    assert params["WebIdLastTime"] == "1740000000"
    assert not params["browser_version"].startswith("Mozilla/")


@pytest.mark.unit
@pytest.mark.parametrize("factory", [RequestProfile.publish_main_profile, RequestProfile.publish_reply_profile, RequestProfile.like_profile])
def test_request_profiles_do_not_invent_geo_without_runtime_or_cookie_evidence(factory):
    profile = factory()
    params = profile.build_query_params({"aweme_id": "123"})
    assert not {"region", "priority_region", "tz_name", "app_language", "browser_language", "webcast_language"} & params.keys()
    assert "accept-language" not in profile.build_headers()


@pytest.mark.unit
@pytest.mark.parametrize("cookies, region, language, timezone", [
    ({"tt-target-idc": "useast5"}, "US", "en-US", "America/New_York"),
    ({"tt-target-idc": "alisg"}, "SG", "en-SG", "Asia/Singapore"),
    ({"store-region": "pl", "tt-target-idc": "useast2a"}, "PL", "pl-PL", "Europe/Warsaw"),
    ({"locale": "uk_UA"}, "UA", "uk-UA", "Europe/Kyiv"),
])
def test_cookie_geo_produces_consistent_query_and_language_header(cookies, region, language, timezone):
    profile = RequestProfile.publish_main_profile()
    params = profile.build_query_params({"aweme_id": "123"}, cookies=cookies)
    assert params["region"] == params["priority_region"] == region
    assert params["app_language"] == params["browser_language"] == params["webcast_language"] == language
    assert params["tz_name"] == timezone
    assert profile.build_headers(cookies=cookies)["accept-language"] == language


@pytest.mark.unit
def test_runtime_geo_wins_over_data_centre_hint():
    runtime = {"region": "GB", "browser_language": "en-GB", "timezone": "Europe/London"}
    geo = resolve_request_geo(runtime, cookies={"tt-target-idc": "alisg"})
    assert geo["region"] == "GB"
    assert geo["browser_language"] == "en-GB"
    assert geo["tz_name"] == "Europe/London"


@pytest.mark.unit
def test_proxy_geo_replaces_conflicting_old_browser_defaults_as_one_tuple():
    runtime = {"proxy_country_code": "PL", "region": "US", "browser_language": "ru-RU", "timezone": "Asia/Jerusalem"}
    profile = RequestProfile.publish_main_profile()
    params = profile.build_query_params({}, runtime_profile=runtime)
    assert params["region"] == params["priority_region"] == "PL"
    assert params["browser_language"] == "pl-PL"
    assert params["tz_name"] == "Europe/Warsaw"
    assert profile.build_headers(runtime_profile=runtime)["accept-language"] == "pl-PL"


@pytest.mark.unit
def test_runtime_keeps_actual_timezone_for_country_with_multiple_timezones():
    geo = resolve_request_geo({"proxy_country_code": "US", "region": "US", "browser_language": "en-US", "timezone": "America/Los_Angeles"})
    assert geo["tz_name"] == "America/Los_Angeles"


@pytest.mark.unit
def test_account_country_and_proxy_hint_work_without_runtime_browser_profile():
    profile = RequestProfile.publish_reply_profile()
    params = profile.build_query_params({"account_country_code": "UA", "proxy_country_code": "PL"})
    assert params["region"] == "PL"
    assert params["browser_language"] == "pl-PL"
    assert params["tz_name"] == "Europe/Warsaw"
    assert "account_country_code" not in params and "proxy_country_code" not in params


@pytest.mark.unit
@pytest.mark.parametrize("idc", ["eu", "unknown-datacenter", ""])
def test_ambiguous_data_centre_does_not_invent_a_country(idc):
    assert resolve_request_geo(cookies={"tt-target-idc": idc}) == {}


@pytest.mark.unit
@pytest.mark.parametrize("endpoint", ["publish", "digg"])
def test_legacy_endpoints_use_the_same_runtime_geo_resolution(endpoint):
    runtime = {"region": "DE", "browser_language": "de-DE", "timezone": "Europe/Berlin"}
    if endpoint == "publish":
        url = TikTokEndpoints.comment_publish("123", "hello", runtime_profile=runtime)
    else:
        url = TikTokEndpoints.comment_digg("123", "456", runtime_profile=runtime)
    query = parse_qs(urlparse(url).query)
    expected = resolve_request_geo(runtime)
    assert all(query[key] == [value] for key, value in expected.items())
    assert "runtime_profile" not in query and "cookies" not in query


@pytest.mark.unit
@pytest.mark.parametrize("endpoint", ["publish", "digg"])
def test_legacy_endpoints_use_cookie_fallback_without_geo_hardcodes(endpoint):
    kwargs = {"cookies": {"tt-target-idc": "alisg"}}
    url = TikTokEndpoints.comment_publish("123", "hello", **kwargs) if endpoint == "publish" else TikTokEndpoints.comment_digg("123", "456", **kwargs)
    query = parse_qs(urlparse(url).query)
    assert query["region"] == ["SG"]
    assert query["tz_name"] == ["Asia/Singapore"]
    assert query["app_language"] == ["en-SG"]


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


@pytest.mark.unit
@pytest.mark.asyncio
async def test_runtime_profile_caches_web_identity_across_navigations_and_http_context():
    from unittest.mock import AsyncMock
    from executor.http.playwright_http_client import _extract_runtime_profile
    from executor.http.http_context import HttpContext

    context1 = SimpleNamespace(_pool_account_id=987654, _account_proxy_in_use=False)
    page1 = SimpleNamespace(
        url="https://www.tiktok.com/@demo/video/111",
        context=context1,
        _pool_account_id=987654,
        evaluate=AsyncMock(
            return_value={
                "current_url": "https://www.tiktok.com/@demo/video/111",
                "device_id": "7499990001112223334",
                "odin_id": "7488880001112223334",
                "web_id_last_time": "1730000100",
            }
        ),
    )
    profile1 = await _extract_runtime_profile(page1)
    assert profile1["device_id"] == "7499990001112223334"
    assert profile1["odin_id"] == "7488880001112223334"
    assert profile1["web_id_last_time"] == "1730000100"

    # Simulate navigation to another video in a new context for the same account where JS generates a newer timestamp
    context2 = SimpleNamespace(_pool_account_id=987654, _account_proxy_in_use=False)
    page2 = SimpleNamespace(
        url="https://www.tiktok.com/@demo/video/222",
        context=context2,
        _pool_account_id=987654,
        evaluate=AsyncMock(
            return_value={
                "current_url": "https://www.tiktok.com/@demo/video/222",
                "device_id": "7499990001112223334",
                "odin_id": "7488880001112223334",
                "web_id_last_time": "1739999999",
            }
        ),
    )
    profile2 = await _extract_runtime_profile(page2)
    assert profile2["web_id_last_time"] == "1730000100"

    # HttpContext.from_account also reuses the cached web identity when cookies omit device_id/WebIdLastTime
    account = SimpleNamespace(
        id=987654,
        cookies='{"sessionid": "abc12345678901234567890123456789", "ttwid": "1%7Cwid"}',
        cookie_file=None,
        proxy=None,
        country_code="US",
    )
    http_ctx = HttpContext.from_account(account)
    assert http_ctx.device_id == "7499990001112223334"
    assert http_ctx.odin_id == "7488880001112223334"
    assert http_ctx.web_id_last_time == "1730000100"


@pytest.mark.unit
@pytest.mark.asyncio
async def test_resolve_order_video_url_ttl_cache_and_result_payload(monkeypatch):
    from unittest.mock import AsyncMock
    import executor.engagement_engine as ee
    from executor.user_schema import UserSchemaResult

    mock_resolve = AsyncMock(return_value=("https://www.tiktok.com/@u/video/7123456789012345678", "7123456789012345678"))
    monkeypatch.setattr(ee, "resolve_video_url", mock_resolve)
    monkeypatch.setattr(ee.settings, "USE_PROXY", False)

    url1, id1 = await ee.resolve_order_video_url(None, "https://vt.tiktok.com/ZSY123456/")
    url2, id2 = await ee.resolve_order_video_url(None, "https://vt.tiktok.com/ZSY123456/")
    assert url1 == url2 == "https://www.tiktok.com/@u/video/7123456789012345678"
    assert id1 == id2 == "7123456789012345678"
    assert mock_resolve.await_count == 1

    res = UserSchemaResult(resolved_video_url=url1, aweme_id=id1)
    payload = res.to_dict()
    assert payload["resolved_video_url"] == url1
    assert payload["aweme_id"] == id1


@pytest.mark.unit
@pytest.mark.asyncio
async def test_http_preflight_concurrency_limit_and_country_code_autobind(monkeypatch):
    import asyncio
    from services.cookie_validator import get_http_preflight_concurrency_limit, validate_accounts_batch
    import executor.user_schema as us

    assert get_http_preflight_concurrency_limit(use_proxy=True) == 3
    assert get_http_preflight_concurrency_limit(use_proxy=False) == 2

    in_flight = 0
    max_in_flight = 0

    async def fake_validate(account, *, strict_passport=False):
        nonlocal in_flight, max_in_flight
        in_flight += 1
        max_in_flight = max(max_in_flight, in_flight)
        await asyncio.sleep(0.02)
        in_flight -= 1
        if not getattr(account, "country_code", None):
            account.country_code = "DE"
        return {"valid": True, "status": "valid", "username": f"u_{account.id}", "user_id": str(account.id), "country_code": "DE"}

    monkeypatch.setattr("services.cookie_validator.validate_account_http", fake_validate)
    monkeypatch.setattr("services.cookie_validator.get_http_preflight_concurrency_limit", lambda use_proxy=None: 2)

    accounts = [SimpleNamespace(id=i, label=f"acc_{i}", country_code=None) for i in range(1, 6)]
    results = await validate_accounts_batch(accounts)
    assert len(results) == 5
    assert max_in_flight <= 2
    assert all(acc.country_code == "DE" for acc in accounts)

    acc_ssr = SimpleNamespace(id=99, country_code=None)
    assert us._bind_account_country_code(None, acc_ssr, "pl") is True
    assert acc_ssr.country_code == "PL"
    assert us._bind_account_country_code(None, acc_ssr, "US") is False
    assert acc_ssr.country_code == "PL"


@pytest.mark.unit
@pytest.mark.asyncio
async def test_web_identity_persistence_and_restart_recovery():
    import json
    from types import SimpleNamespace
    from unittest.mock import AsyncMock
    from executor.http.playwright_http_client import (
        _ACCOUNT_WEB_IDENTITY_CACHE,
        cache_web_identity,
        get_cached_web_identity,
        extract_web_identity_from_cookies,
    )
    from services.cookie_persistence_service import (
        synchronize_browser_cookies,
        save_cookies_after_action,
    )
    from services.cookie_validator import (
        prepare_cookies_for_context,
        parse_cookies_to_dict,
    )

    # 1. Start with an account with base session cookies and no stored web identity
    account = SimpleNamespace(
        id=777,
        label="acc_777",
        cookies=json.dumps([
            {"name": "sessionid", "value": "test_session_123", "domain": ".tiktok.com", "path": "/"},
            {"name": "sid_tt", "value": "test_session_123", "domain": ".tiktok.com", "path": "/"},
        ]),
        cookie_file=None,
    )

    # Clean in-memory cache
    _ACCOUNT_WEB_IDENTITY_CACHE.pop(777, None)

    # 2. Simulate context with warmup finding device_id & web_id_last_time
    mock_context = SimpleNamespace(
        _pool_account_id=777,
        _pool_account_obj=account,
        cookies=AsyncMock(return_value=[
            {"name": "sessionid", "value": "test_session_123", "domain": ".tiktok.com", "path": "/"},
            {"name": "ttwid", "value": "test_ttwid_abc", "domain": ".tiktok.com", "path": "/"},
            {"name": "odin_tt", "value": "test_odin_xyz", "domain": ".tiktok.com", "path": "/"},
        ]),
        add_cookies=AsyncMock(),
    )

    # Page warmup discovers web identity
    cache_web_identity(
        777,
        {"device_id": "7199999999999999999", "odin_id": "test_odin_xyz", "web_id_last_time": "1710000000"},
        context=mock_context,
        account=account,
    )

    # Dummy DB session
    class DummyDB:
        def query(self, *args, **kwargs):
            return self
        def filter(self, *args, **kwargs):
            return self
        def with_for_update(self):
            return self
        def first(self):
            return account
        def commit(self):
            pass
        def rollback(self):
            pass

    dummy_db = DummyDB()

    # 3. synchronize_browser_cookies persists device_id & WebIdLastTime to account.cookies
    cookies, saved = await synchronize_browser_cookies(mock_context, account, db=dummy_db)
    assert saved is True
    assert "device_id" in cookies
    assert cookies["device_id"] == "7199999999999999999"
    assert cookies["WebIdLastTime"] == "1710000000"

    # Verify account.cookies string in DB format has the identity records
    saved_parsed = parse_cookies_to_dict(account.cookies)
    assert saved_parsed["device_id"] == "7199999999999999999"
    assert saved_parsed["WebIdLastTime"] == "1710000000"
    assert saved_parsed["odinId"] == "test_odin_xyz"

    # 4. Simulate process restart: completely wipe RAM cache!
    _ACCOUNT_WEB_IDENTITY_CACHE.clear()
    assert 777 not in _ACCOUNT_WEB_IDENTITY_CACHE

    # 5. Across restart, restoring from account.cookies works seamlessly:
    restored = extract_web_identity_from_cookies(account.cookies)
    assert restored["device_id"] == "7199999999999999999"
    assert restored["web_id_last_time"] == "1710000000"
    assert restored["odin_id"] == "test_odin_xyz"

    # 6. prepare_cookies_for_context seeds cache AND strips synthetic records from browser add_cookies
    browser_records = prepare_cookies_for_context(account)
    browser_names = {r["name"] for r in browser_records}
    assert "device_id" not in browser_names
    assert "WebIdLastTime" not in browser_names
    assert "odinId" not in browser_names
    assert "sessionid" in browser_names
    assert "ttwid" in browser_names

    # The in-memory cache was re-seeded from account.cookies!
    cached_after_restart = get_cached_web_identity(777, account=account)
    assert cached_after_restart["device_id"] == "7199999999999999999"
    assert cached_after_restart["web_id_last_time"] == "1710000000"

    # 7. save_cookies_after_action preserves the persisted identity rather than wiping it
    mock_action_context = SimpleNamespace(
        _pool_account_id=777,
        _pool_account_obj=account,
        cookies=AsyncMock(return_value=[
            {"name": "sessionid", "value": "test_session_rotated", "domain": ".tiktok.com", "path": "/"},
            {"name": "ttwid", "value": "test_ttwid_abc", "domain": ".tiktok.com", "path": "/"},
        ]),
    )
    res_saved = await save_cookies_after_action(mock_action_context, account, db=dummy_db)
    assert res_saved is True
    post_action_parsed = parse_cookies_to_dict(account.cookies)
    assert post_action_parsed["device_id"] == "7199999999999999999"
    assert post_action_parsed["WebIdLastTime"] == "1710000000"
    assert post_action_parsed["sessionid"] == "test_session_rotated"

    # 8. HttpContext.from_account restores the exact persisted identity
    from executor.http.http_context import HttpContext
    _ACCOUNT_WEB_IDENTITY_CACHE.clear()
    http_ctx = HttpContext.from_account(account)
    assert http_ctx.device_id == "7199999999999999999"
    assert http_ctx.web_id_last_time == "1710000000"
    assert http_ctx.odin_id == "test_odin_xyz"


