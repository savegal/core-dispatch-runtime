import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from services.cookie_validator import parse_cookies_to_dict


pytestmark = pytest.mark.unit


@pytest.mark.parametrize("domain", ["www.tiktok.com", ".tiktok.com", "tiktok.com", ".WWW.TIKTOK.COM"])
@pytest.mark.parametrize("reverse", [False, True])
def test_ms_token_uses_web_domain_regardless_of_expiry_or_order(domain, reverse):
    records = [
        {"name": "msToken", "value": "web", "domain": domain, "expires": 1},
        *({"name": "msToken", "value": subdomain, "domain": f".{subdomain}.tiktok.com", "expires": 9999999999}
          for subdomain in ("ads", "shop", "livecenter")),
        {"name": "msToken", "value": "unscoped", "expires": 9999999999},
    ]
    if reverse:
        records.reverse()
    cookies = parse_cookies_to_dict(json.dumps(records))
    assert cookies["msToken"] == "web"
    assert any(c.get("domain") == domain and c["value"] == "web" for c in cookies.records)
    assert {c["value"] for c in cookies.records} >= {"web", "ads", "shop", "livecenter"}


def test_subdomain_tokens_are_not_used_as_web_tokens():
    cookies = parse_cookies_to_dict('[{"name":"msToken","value":"ads","domain":"ads.tiktok.com"}]')
    assert "msToken" not in cookies
    assert len(cookies.records) == 1


def test_duplicate_sid_guard_in_same_scope_keeps_newest_record_for_serialization():
    records = [
        {"name": "sid_guard", "value": "b" * 32 + "|1700000001|3600", "domain": ".tiktok.com", "expires": 1},
        {"name": "sid_guard", "value": "a" * 32 + "|1700000000|3600", "domain": ".tiktok.com", "expires": 9999999999},
    ]
    cookies = parse_cookies_to_dict(json.dumps(records))
    assert cookies.records == [records[0]]
    assert cookies["sessionid"] == "b" * 32


@pytest.mark.parametrize("separator", ["|", "%7C", "%7c"])
@pytest.mark.parametrize("reverse", [False, True])
def test_sid_guard_uses_embedded_timestamp_and_restores_session(separator, reverse):
    new_guard = separator.join(["b" * 32, "1700000001", "3600", "unused"])
    records = [
        {"name": "sid_guard", "value": new_guard, "domain": "www.tiktok.com", "expirationDate": 1},
        {"name": "sid_guard", "value": "a" * 32 + "|1700000000|3600", "expirationDate": 9999999999},
        {"name": "sid_guard", "value": "c" * 32 + "|invalid|3600", "expirationDate": 9999999999},
    ]
    if reverse:
        records.reverse()
    cookies = parse_cookies_to_dict(json.dumps({"cookies": records}))
    assert cookies["sid_guard"] == new_guard
    assert all(cookies[name] == "b" * 32 for name in ("sessionid", "sid_tt", "sessionid_ss"))


@pytest.mark.parametrize("session_name", ["sessionid", "sid_tt", "sessionid_ss"])
def test_explicit_session_prevents_guard_aliases(session_name):
    cookies = parse_cookies_to_dict(json.dumps({"sid_guard": "a" * 32 + "|1700000000|3600", session_name: "explicit"}))
    assert cookies[session_name] == "explicit"
    assert set(cookies) == {"sid_guard", session_name}


@pytest.mark.parametrize("guard", ["a" * 31 + "|1700000000", "g" * 32 + "|1700000000",
                                       "a" * 32, "a" * 32 + "|invalid", "a" * 32 + "|-1"])
def test_malformed_guard_does_not_create_session(guard):
    assert parse_cookies_to_dict(json.dumps({"sid_guard": guard})) == {"sid_guard": guard}


@pytest.mark.parametrize("raw", [json.dumps({"sid_guard": "a" * 32 + "|1700000000|3600"}),
                                 "sid_guard=" + "a" * 32 + "%7C1700000000%7C3600"])
def test_session_restoration_for_other_supported_formats(raw):
    assert parse_cookies_to_dict(raw)["sessionid"] == "a" * 32


def test_other_cookies_keep_expiry_precedence_and_empty_values():
    cookies = parse_cookies_to_dict(json.dumps([
        {"name": "other_cookie", "value": "new", "expires": 100},
        {"name": "other_cookie", "value": "old", "expires": 10},
        {"Name": "empty", "Value": ""},
        None,
    ]))
    assert cookies == {"other_cookie": "new", "empty": ""}


@pytest.mark.parametrize("name", ["msToken", "tt_csrf_token", "ttwid"])
@pytest.mark.parametrize("export_expiry", ["expirationDate", "expires"])
@pytest.mark.parametrize("reverse", [False, True])
def test_real_expiry_beats_synthetic_2038_expiry(name, export_expiry, reverse):
    from services.cookie_validator import normalize_cookie_records
    fresh = {"name": name, "value": "fresh", "domain": ".tiktok.com", "expires": 1_800_000_000}
    records = [{"name": name, "value": "old", "domain": ".tiktok.com", export_expiry: 2_147_483_647}, fresh]
    if reverse:
        records.reverse()
    normalized = normalize_cookie_records(records)
    assert normalized[name] == "fresh"
    assert normalized.records == [fresh]


@pytest.mark.parametrize("name", ["msToken", "tt_csrf_token"])
@pytest.mark.parametrize("expiry", [1_800_000_000, -1, 0])
def test_rotating_web_tokens_prefer_latest_server_record_over_longer_lifetime(name, expiry):
    from services.cookie_validator import normalize_cookie_records
    normalized = normalize_cookie_records([
        {"name": name, "value": "older", "domain": "www.tiktok.com", "expires": 1_900_000_000},
        {"name": name, "value": "latest", "domain": ".tiktok.com", "expires": expiry},
        {"name": name, "value": "ads", "domain": "ads.tiktok.com", "expires": 1_950_000_000},
    ])
    assert normalized[name] == "latest"


def test_playwright_expiry_overrides_conflicting_export_expiry():
    from services.cookie_validator import normalize_cookies_for_browser
    records = [{"name": "msToken", "value": "live-session", "domain": ".tiktok.com",
                "expires": -1, "expirationDate": 2_147_483_647}]
    assert normalize_cookies_for_browser(records)[0]["value"] == "live-session"


@pytest.mark.parametrize("expiry_key", ["expires", "expirationDate", "expiration"])
def test_startup_cleanup_drops_synthetic_ms_token_and_stale_csrf(expiry_key):
    from services.cookie_validator import normalize_cookies_for_browser
    records = [
        {"name": "sessionid", "value": "session", "domain": ".tiktok.com"},
        {"name": "tt_csrf_token", "value": "stale-csrf", "domain": ".tiktok.com"},
        {"name": "msToken", "value": "stale-token", "domain": ".tiktok.com", expiry_key: 2_147_483_647},
    ]
    assert {row["name"] for row in normalize_cookies_for_browser(records)} == {"sessionid"}
    assert {row["name"] for row in normalize_cookies_for_browser(records, drop_stale_antifraud=False)} == {
        "sessionid", "msToken", "tt_csrf_token",
    }
    records[-1][expiry_key] = 1_800_000_000
    assert {row["name"] for row in normalize_cookies_for_browser(records)} == {"sessionid", "msToken"}


@pytest.mark.parametrize("use_proxy", [True, False])
def test_http_context_uses_normalized_web_token_and_restored_session(monkeypatch, use_proxy):
    from executor.http.http_context import HttpContext
    from config.settings import get_settings
    monkeypatch.setattr(get_settings(), "USE_PROXY", use_proxy)

    account = SimpleNamespace(id=1, user_agent=None, proxy="http://unused.invalid:9999", cookies=json.dumps([
        {"name": "msToken", "value": "web", "domain": ".tiktok.com"},
        {"name": "msToken", "value": "shop", "domain": "shop.tiktok.com"},
        {"name": "sid_guard", "value": "a" * 32 + "|1700000000|3600"},
    ]))
    context = HttpContext.from_account(account)
    assert context.ms_token == "web"
    assert context.cookies["sessionid"] == "a" * 32
    assert context.proxy == (account.proxy if use_proxy else None)


@pytest.mark.asyncio
@pytest.mark.parametrize("use_proxy", [True, False])
@pytest.mark.parametrize("saved_ua", [None, "Mozilla/5.0 Chrome/120.0.0.0", "Mozilla/5.0 iPhone Mobile"])
async def test_http_validation_respects_direct_mode(monkeypatch, use_proxy, saved_ua):
    from config.settings import get_settings
    from services import cookie_validator
    monkeypatch.setattr(get_settings(), "USE_PROXY", use_proxy)
    captured = []

    class Response:
        status = 200
        headers = {}

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            pass

        async def json(self, **kwargs):
            return {"message": "success", "data": {"username": "test-user"}}

    class Session(Response):
        def __init__(self, **kwargs):
            pass

        def get(self, url, **kwargs):
            captured.append(kwargs)
            return Response()

    monkeypatch.setattr(cookie_validator.aiohttp, "ClientSession", Session)
    account = SimpleNamespace(id=1, proxy="http://unused.invalid:9999", user_agent=saved_ua,
                              cookies='{"sessionid":"session"}', cookie_file=None)
    result = await cookie_validator.validate_account_http(account)
    assert result["valid"]
    assert captured[0].get("proxy") == (account.proxy if use_proxy else None)
    from executor.browser_identity import desktop_user_agent, sec_ch_ua
    assert captured[0]["headers"]["User-Agent"] == desktop_user_agent()
    assert captured[0]["headers"]["sec-ch-ua"] == sec_ch_ua()


@pytest.mark.asyncio
@pytest.mark.parametrize("status,payload,error,expected", [
    (200, {"message": "success", "data": {"username": "test-user"}}, None, "valid"),
    (200, {"message": "success", "data": {"user_id_str": "12345678901234"}}, None, "valid"),
    (200, {"message": "success", "data": {"username": "test-user", "user_id": 12345678901234}}, None, "valid"),
    (200, {"message": "session expired"}, None, "dead"),
    (401, None, None, "dead"),
    (403, None, None, "inconclusive"),
    (429, None, None, "inconclusive"),
    (500, None, None, "inconclusive"),
    (200, None, None, "inconclusive"),
    (200, {}, None, "inconclusive"),
    (200, {"message": "success", "data": {}}, None, "inconclusive"),
    (200, {"message": "error"}, None, "inconclusive"),
    (200, None, ValueError("HTML instead of JSON"), "inconclusive"),
    (None, None, TimeoutError("passport timed out"), "inconclusive"),
])
async def test_http_preflight_uses_only_one_passport_request(monkeypatch, status, payload, error, expected):
    from services import cookie_validator

    requested = []

    class Response:
        headers = {}

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            pass

        async def json(self, **kwargs):
            if error:
                raise error
            return payload

        async def text(self, **kwargs):
            raise AssertionError("Preflight must not download an HTML page")

    response = Response()
    response.status = status

    class Session(Response):
        def __init__(self, **kwargs):
            pass

        def get(self, url, **kwargs):
            requested.append(url)
            if status is None:
                raise error
            return response

    monkeypatch.setattr(cookie_validator.aiohttp, "ClientSession", Session)
    account = SimpleNamespace(id=1, proxy=None, user_agent=None, cookies='{"sessionid":"session"}', cookie_file=None)
    result = await cookie_validator.validate_account_http(account)
    assert result["status"] == expected
    assert requested == ["https://www.tiktok.com/passport/web/account/info/"]
    assert result["definitive"] is (expected != "inconclusive")
    if expected == "valid":
        user = payload["data"]
        expected_uid = user.get("user_id_str") or user.get("user_id")
        assert result["user_id"] == (str(expected_uid) if expected_uid else None)
        assert result["username"] == user.get("username")


@pytest.mark.asyncio
@pytest.mark.parametrize("runtime", ["bootstrap", "bootstrap_timeout", "warmup"])
async def test_browser_runtime_uses_same_cookie_normalization(monkeypatch, runtime):
    import executor.session_bootstrap as bootstrap
    import executor.engagement_engine as engine

    records = [
        {"name": "msToken", "value": "web", "domain": "www.tiktok.com"},
        {"name": "msToken", "value": "ads", "domain": "ads.tiktok.com"},
        {"name": "sid_guard", "value": "b" * 32 + "|1700000001|3600", "expires": 1},
        {"name": "sid_guard", "value": "a" * 32 + "%7C1700000000%7C3600", "expires": 9999999999},
        *({"name": name, "value": name} for name in ("ttwid", "tt_csrf_token", "verifyFp", "device_id")),
    ]
    url = "https://www.tiktok.com/@demo/video/1234567890123456789"
    page = SimpleNamespace(
        url=url, goto=AsyncMock(), evaluate=AsyncMock(return_value=0),
        context=SimpleNamespace(cookies=AsyncMock(return_value=records), add_cookies=AsyncMock()),
        wait_for_load_state=AsyncMock(),
    )
    monkeypatch.setattr(bootstrap.asyncio, "sleep", AsyncMock())
    if runtime.startswith("bootstrap"):
        monkeypatch.setattr(bootstrap, "_apply_browser_login_state", AsyncMock(return_value=True))
        monkeypatch.setattr(bootstrap, "extract_tiktok_username", AsyncMock(return_value=None))
        result = await bootstrap.bootstrap_session(page, url, max_total_seconds=0 if runtime.endswith("timeout") else 22)
        cookies = result.cookies
    else:
        monkeypatch.setattr(engine, "dismiss_cookie_banners", AsyncMock())
        monkeypatch.setattr(engine, "wait_for_runtime_signer_ready", AsyncMock(return_value=True))
        cookies, valid, _ = await engine.warmup_page_for_http(page, url, max_retries=1)
        assert valid is True
    assert cookies["msToken"] == "web"
    assert cookies["sessionid"] == "b" * 32


@pytest.mark.parametrize("name", ["msToken", "tt_csrf_token"])
def test_repeated_normalization_preserves_latest_token_across_web_domains(name):
    from services.cookie_validator import normalize_cookie_records
    records = [
        {"name": name, "value": "www-old", "domain": "www.tiktok.com", "expires": 1_800_000_000},
        {"name": name, "value": "root-old", "domain": ".tiktok.com", "expires": 1_800_000_000},
        {"name": name, "value": "www-fresh", "domain": "www.tiktok.com", "expires": 1_800_000_000},
    ]
    normalized = normalize_cookie_records(records)
    assert normalized[name] == "www-fresh"
    assert normalize_cookie_records(normalized.records)[name] == "www-fresh"
