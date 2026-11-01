from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from executor import engagement_engine as engine, user_schema as schema
from tests.unit.test_http_warmup_recovery import warmup as warmup, URL
from executor.http.page_runtime import activate_page_input, prepared_runtime_profile
from executor.http.playwright_http_client import PlaywrightHttpClient, _extract_runtime_profile


@pytest.mark.asyncio
@pytest.mark.parametrize("reuse", [False, True])
async def test_new_document_keeps_navigation_and_dom_maturity_guards(warmup, reuse):
    page, comments, sleeps = warmup
    if not reuse:
        page.url = "about:blank"
        async def navigate(*args, **kwargs):
            page.url = URL
        page.goto.side_effect = navigate
    page.evaluate.return_value = {"current_url": URL, "navigation_url": URL,
                                  "age_ms": 300, "dom_ready_ms": 200}
    _, valid, info = await engine.warmup_page_for_http(page, URL, max_retries=1)
    assert valid
    assert info["minimum_age_wait_ms"] == 1700
    assert sum(sleeps) == pytest.approx(1.7)
    comments.assert_not_awaited()


def test_preflight_key_changes_with_authentication_but_not_rotating_ms_token():
    account = SimpleNamespace(id=97701, cookies='{"sessionid":"first","msToken":"one"}', proxy=None)
    original = schema._http_preflight_cache_key(account)
    account.cookies = '{"sessionid":"first","msToken":"two"}'
    assert schema._http_preflight_cache_key(account) == original
    account.cookies = '{"sessionid":"second","msToken":"two"}'
    assert schema._http_preflight_cache_key(account) != original


@pytest.mark.asyncio
async def test_positive_preflight_reused_for_five_minutes_then_revalidated(monkeypatch):
    from services import cookie_validator
    clock = [1000.0]
    monkeypatch.setattr(schema.time, "monotonic", lambda: clock[0])
    for name in ("_HTTP_PREFLIGHT_VALID_CACHE", "_HTTP_PREFLIGHT_USERNAME_CACHE", "_HTTP_PREFLIGHT_USER_ID_CACHE"):
        monkeypatch.setattr(schema, name, {})
    validate = AsyncMock(return_value={"valid": True, "status": "valid", "definitive": True})
    monkeypatch.setattr(cookie_validator, "validate_account_http", validate)
    account = SimpleNamespace(id=97702, cookies='{"sessionid":"fixture"}', proxy=None)
    for elapsed in (0, 120, 299):
        clock[0] = 1000.0 + elapsed
        assert await schema._preflight_cookie_http_gate(None, account, correlation_id="fixture", phase="main") is None
    assert validate.await_count == 1
    clock[0] = 1301.0
    assert await schema._preflight_cookie_http_gate(None, account, correlation_id="fixture", phase="main") is None
    assert validate.await_count == 2


@pytest.mark.asyncio
async def test_shortlink_maturity_is_reused_only_after_waiting_once(warmup):
    page, _, sleeps = warmup
    page.evaluate.return_value = {"current_url": URL, "navigation_url": "https://vt.tiktok.com/fixture/",
                                  "age_ms": 300, "dom_ready_ms": 200}
    _, valid, info = await engine.warmup_page_for_http(page, URL, max_retries=1)
    assert valid and info["minimum_age_wait_ms"] == 2000
    _, valid, info = await engine.warmup_page_for_http(page, URL, max_retries=1)
    assert valid and info["minimum_age_wait_ms"] == 0
    assert sum(sleeps) == pytest.approx(2.0)


def test_preflight_key_binds_proxy_and_dedicated_route(monkeypatch):
    from services import dedicated_proxy_service as dedicated
    account = SimpleNamespace(id=97703, cookies='{"sessionid":"fixture"}', proxy="http://proxy-one.invalid:8080")
    monkeypatch.setattr(schema._settings, "USE_PROXY", True)
    original = schema._http_preflight_cache_key(account)
    account.proxy = "http://proxy-two.invalid:8080"
    changed = schema._http_preflight_cache_key(account)
    assert original != changed
    monkeypatch.setattr(dedicated, "get_current_dedicated_proxy", lambda: SimpleNamespace(proxy=account.proxy, proxy_id=123))
    assert schema._http_preflight_cache_key(account) != changed
    monkeypatch.setattr(schema._settings, "USE_PROXY", False)
    # An active dedicated route is still the actual transport with USE_PROXY off.
    assert schema._http_preflight_cache_key(account) != changed


def test_direct_preflight_is_not_reused_when_a_required_proxy_is_missing(monkeypatch):
    account = SimpleNamespace(id=97707, cookies='{"sessionid":"fixture"}', proxy=None)
    monkeypatch.setattr(schema._settings, "USE_PROXY", False)
    direct = schema._http_preflight_cache_key(account)
    monkeypatch.setattr(schema._settings, "USE_PROXY", True)
    assert schema._http_preflight_cache_key(account) != direct


@pytest.mark.asyncio
async def test_prepared_profile_is_copied_once_and_invalidated_on_same_url_reload(monkeypatch):
    listeners = {}
    frame = object()
    page = SimpleNamespace(url=URL, main_frame=frame, on=lambda event, cb: listeners.update({event: cb}),
                           evaluate=AsyncMock(return_value={"current_url": URL, "device_id": "first"}),
                           context=SimpleNamespace(request=object()))
    snapshot = await _extract_runtime_profile(page)
    page.evaluate.reset_mock()
    client = await PlaywrightHttpClient.from_page(page, runtime_profile=snapshot)
    assert client.runtime_profile["device_id"] == "first"
    client.runtime_profile["device_id"] = "local-edit"
    assert snapshot["device_id"] == "first"
    page.evaluate.assert_not_awaited()
    listeners["request"](SimpleNamespace(frame=frame, is_navigation_request=lambda: True))
    listeners["domcontentloaded"]()
    page.evaluate.return_value = {"current_url": URL, "device_id": "second"}
    client = await PlaywrightHttpClient.from_page(page, runtime_profile=snapshot)
    assert client.runtime_profile["device_id"] == "second"
    page.evaluate.assert_awaited_once()


@pytest.mark.asyncio
async def test_failed_or_expired_extraction_is_not_a_reusable_snapshot(monkeypatch):
    page = SimpleNamespace(url=URL, evaluate=AsyncMock(side_effect=RuntimeError("reload")), context=SimpleNamespace())
    assert prepared_runtime_profile(page, await _extract_runtime_profile(page)) is None
    page.evaluate.side_effect = None
    page.evaluate.return_value = {"current_url": URL, "device_id": "fixture"}
    snapshot = await _extract_runtime_profile(page)
    assert prepared_runtime_profile(page, snapshot) is not None
    monkeypatch.setattr("executor.http.page_runtime.time.monotonic", lambda: snapshot["_captured_at"] + 11)
    assert prepared_runtime_profile(page, snapshot) is None


@pytest.mark.asyncio
async def test_mouse_activation_is_once_per_document_and_resets_after_reload():
    callbacks = {}
    frame = object()
    move = AsyncMock()
    page = SimpleNamespace(url=URL, main_frame=frame, mouse=SimpleNamespace(move=move),
                           on=lambda event, cb: callbacks.update({event: cb}))
    assert await activate_page_input(page)
    assert await activate_page_input(page)
    move.assert_awaited_once_with(120, 160)
    callbacks["request"](SimpleNamespace(frame=frame, is_navigation_request=lambda: True))
    callbacks["domcontentloaded"]()
    assert await activate_page_input(page)
    assert move.await_count == 2


def test_geometry_matches_launch_options_and_js_profile():
    from executor import device_profiles as devices, browser_pool
    account = SimpleNamespace(id=97704)
    profile = devices.build_device_profile(account, {}, "145.0.0.0")
    geometry = profile["identity"]["geometry"]
    assert f'--window-size={geometry["outerWidth"]},{geometry["outerHeight"]}' in browser_pool._desktop_window_args_for_account(account)
    assert profile["options"]["viewport"]["height"] == geometry["viewportHeight"]
    assert geometry["viewportHeight"] <= geometry["outerHeight"] <= profile["options"]["screen"]["height"]


@pytest.mark.asyncio
async def test_reload_to_same_url_during_probe_rechecks_signer_before_confirming(warmup, monkeypatch):
    page, comments, _ = warmup
    page.context.cookies.return_value = [{"name": key, "value": "fixture", "domain": ".tiktok.com"}
                                         for key in ("sessionid", "ttwid", "msToken")]
    async def reload_during_probe(*args, **kwargs):
        page._http_document_generation += 1
        page.context.cookies.return_value.append({"name": "tt_csrf_token", "value": "fresh", "domain": ".tiktok.com"})
        return {"comments": []}
    comments.side_effect = reload_during_probe
    ready = AsyncMock(return_value=True)
    monkeypatch.setattr(engine, "wait_for_runtime_signer_ready", ready)
    _, valid, info = await engine.warmup_page_for_http(page, URL, max_retries=1)
    assert valid and info["document_rebased"]
    assert ready.await_args.kwargs["timeout_ms"] == 1000


@pytest.mark.asyncio
async def test_fresh_cookie_timestamp_alone_does_not_skip_authentication(monkeypatch):
    from datetime import datetime, timezone
    from services import cookie_validator
    account = SimpleNamespace(id=97705, cookies='{"sessionid":"fixture"}', proxy=None,
                              last_used_at=datetime.now(timezone.utc))
    validate = AsyncMock(return_value={"valid": True, "status": "valid"})
    monkeypatch.setattr(cookie_validator, "validate_account_http", validate)
    assert await schema._preflight_cookie_http_gate(None, account, correlation_id="fixture", phase="main") is None
    validate.assert_awaited_once()


def test_dead_session_invalidation_removes_all_positive_states_for_account(monkeypatch):
    account = SimpleNamespace(id=97706, cookies='{"sessionid":"fixture"}', proxy=None)
    key = schema._http_preflight_cache_key(account)
    previous = (key[0], "previous-session-fingerprint")
    for name in ("_HTTP_PREFLIGHT_VALID_CACHE", "_HTTP_PREFLIGHT_USERNAME_CACHE", "_HTTP_PREFLIGHT_USER_ID_CACHE"):
        monkeypatch.setattr(schema, name, {key: (0.0, "fixture"), previous: (0.0, "fixture")})
    schema._forget_http_preflight_valid(account)
    assert not schema._HTTP_PREFLIGHT_VALID_CACHE
    assert not schema._HTTP_PREFLIGHT_USERNAME_CACHE
    assert not schema._HTTP_PREFLIGHT_USER_ID_CACHE


def test_exported_guard_only_cookie_records_keep_web_token_with_session_lifetime():
    import json
    from bot.services.account_parser import parse_account_line
    from services.cookie_validator import has_guard_only_auth, prepare_cookies_for_context

    raw_line = json.dumps([
        {"domain": ".tiktok.com", "expirationDate": 2147483647, "hostOnly": True, "httpOnly": False,
         "name": "sid_guard", "path": "/", "secure": True,
         "value": "4235cfb0df6a9fdab70c6e71d8304fe2%7C1773520285%7C15552000%7CThu"},
        {"domain": ".tiktok.com", "expirationDate": 2147483647, "hostOnly": True, "httpOnly": False,
         "name": "tt-target-idc", "path": "/", "secure": False, "value": "alisg"},
        {"domain": "tiktok.com", "expirationDate": 2147483647, "hostOnly": True, "httpOnly": True,
         "name": "sid_guard", "path": "/", "secure": True,
         "value": "4235cfb0df6a9fdab70c6e71d8304fe2%7C1780899785%7C14572454%7CMon"},
        {"domain": "tiktok.com", "expirationDate": 2147483647, "hostOnly": True, "httpOnly": False,
         "name": "msToken", "path": "/", "secure": True, "value": "synthetic-export-token"},
    ])
    parsed, err, is_dup = parse_account_line(raw_line, 1, set())
    assert err is None and not is_dup and parsed is not None
    assert has_guard_only_auth(parsed.cookies)
    ctx_cookies = prepare_cookies_for_context(SimpleNamespace(id=97707, cookies=parsed.cookies))
    names = {c["name"]: c for c in ctx_cookies}
    assert names["msToken"]["value"] == "synthetic-export-token"
    assert "expires" not in names["msToken"]
    assert names["sid_guard"]["domain"] == ".tiktok.com"
    assert "1780899785" in names["sid_guard"]["value"]
    assert names["sessionid"]["value"] == "4235cfb0df6a9fdab70c6e71d8304fe2"
    assert names["tt-target-idc"]["value"] == "alisg"

