import json
import time
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from db.models import Account
from executor import engagement_engine, tiktok_client, tiktok_client_async, user_schema
from services.account_service import MAIN_SCOPE, REPLY_SCOPE, get_http_execution_strategy
from services.cookie_persistence_service import save_warmed_cookies_for_account
from services.cookie_validator import normalize_cookie_records, normalize_cookies_for_browser, parse_cookies_to_dict


pytestmark = pytest.mark.unit


def session_records():
    timestamp = int(time.time())
    return [
        {"name": "sid_guard", "value": f'{"b" * 32}%7C{timestamp}%7C864000', "domain": ".tiktok.com", "hostOnly": True},
        {"name": "sid_guard", "value": f'{"a" * 32}|{timestamp - 100}|864000', "domain": "tiktok.com"},
        {"name": "msToken", "value": "web", "domain": "www.tiktok.com", "hostOnly": True},
        {"name": "msToken", "value": "ads", "domain": "ads.tiktok.com", "expirationDate": 9999999999},
        {"name": "tracking", "value": "scoped", "domain": "shop.tiktok.com", "path": "/shop", "secure": False, "httpOnly": False},
    ]


def account_with_minimal_cookies():
    return Account(
        label="warmup", status="active", cookies=json.dumps(session_records()),
        proxy="http://proxy", created_at=datetime.now(timezone.utc) - timedelta(hours=3),
        last_validation_ok=datetime.now(timezone.utc),
    )


def test_browser_cookie_normalization_restores_session_and_removes_export_conflicts():
    records = session_records()
    original = json.dumps(records)
    browser = tiktok_client.parse_cookies(original)
    by_name = {c["name"]: c for c in browser}
    for name in ("sessionid", "sid_tt", "sessionid_ss"):
        assert by_name[name]["value"] == "b" * 32
        assert by_name[name]["domain"] == ".tiktok.com"
        assert by_name[name]["httpOnly"] is True
    assert [c["value"] for c in browser if c["name"] == "msToken"] == ["web"]
    assert by_name["tracking"]["domain"] == "shop.tiktok.com"
    assert by_name["tracking"]["path"] == "/shop"
    assert by_name["tracking"]["secure"] is False
    assert not any("hostOnly" in c or "expirationDate" in c for c in browser)
    assert normalize_cookies_for_browser(browser) == browser
    assert json.dumps(records) == original


@pytest.mark.parametrize("raw", [
    json.dumps({"sid_guard": "b" * 32 + "|1700000000|864000", "msToken": "web"}),
    json.dumps({"cookies": session_records()}),
    "sid_guard=" + "b" * 32 + "%7C1700000000%7C864000; msToken=web",
])
def test_all_parser_formats_add_restored_browser_session(raw):
    cookies = {c["name"]: c["value"] for c in tiktok_client.parse_cookies(raw)}
    assert all(cookies[name] == "b" * 32 for name in ("sessionid", "sid_tt", "sessionid_ss"))


def test_browser_session_expiry_minus_one_is_not_treated_as_expired():
    account = Account(id=1, cookies='[{"name":"sessionid","value":"live","expires":-1}]')
    assert tiktok_client_async._fast_cookie_health_precheck(account) == (None, "inconclusive")


@pytest.mark.parametrize("scope", [MAIN_SCOPE, REPLY_SCOPE])
def test_minimal_restored_guard_session_gets_http_first_before_warmup(scope):
    account = account_with_minimal_cookies()
    strategy = get_http_execution_strategy(account, scope)
    assert strategy["strategy"] == "http_first"
    assert "ttwid" not in parse_cookies_to_dict(account.cookies)
    assert "tt_csrf_token" not in parse_cookies_to_dict(account.cookies)


@pytest.mark.asyncio
async def test_warmup_persists_new_linkage_and_restored_session_in_database(db_session, monkeypatch):
    account = account_with_minimal_cookies()
    db_session.add(account)
    db_session.commit()
    records = session_records() + [
        {"name": "ttwid", "value": "new-wid", "domain": ".tiktok.com", "httpOnly": True},
        {"name": "tt_csrf_token", "value": "new-csrf", "domain": "www.tiktok.com", "sameSite": "Lax"},
    ]
    context = SimpleNamespace(cookies=AsyncMock(return_value=records), add_cookies=AsyncMock())
    page = SimpleNamespace(
        url="https://www.tiktok.com/@demo/video/1234567890123456789",
        context=context, goto=AsyncMock(), reload=AsyncMock(), wait_for_load_state=AsyncMock(),
    )
    monkeypatch.setattr(engagement_engine, "wait_for_runtime_signer_ready", AsyncMock(return_value=False))
    monkeypatch.setattr(engagement_engine.asyncio, "sleep", AsyncMock())

    cookies, valid, info = await engagement_engine.warmup_page_for_http(page, page.url, account=account)

    assert valid is True
    assert info["cookies_saved"] is True
    assert info["signer_ready"] is False
    assert info["degraded_http"] is False
    page.reload.assert_not_awaited()
    context.add_cookies.assert_awaited_once()
    db_session.refresh(account)
    saved = parse_cookies_to_dict(account.cookies)
    assert saved["ttwid"] == cookies["ttwid"] == "new-wid"
    assert saved["tt_csrf_token"] == "new-csrf"
    assert all(saved[name] == "b" * 32 for name in ("sessionid", "sid_tt", "sessionid_ss"))
    assert next(c for c in json.loads(account.cookies) if c["name"] == "tt_csrf_token")["domain"] == "www.tiktok.com"


def test_warmup_cookie_persistence_does_not_commit_unchanged_linkage():
    account = SimpleNamespace(id=1, cookies='{"ttwid":"wid","tt_csrf_token":"csrf"}')
    db = Mock()
    assert not save_warmed_cookies_for_account(account, {"ttwid": "wid", "tt_csrf_token": "csrf"}, db)
    db.query.assert_not_called()


def test_warmup_cookie_persistence_saves_rotated_odin_and_mstoken(db_session):
    account = account_with_minimal_cookies()
    db_session.add(account)
    db_session.commit()
    warmed = {
        "sessionid": "b" * 32,
        "ttwid": "wid-existing",
        "odin_tt": "odin-748888",
        "msToken": "fresh-ms-token",
    }
    assert save_warmed_cookies_for_account(account, warmed, db_session) is True
    saved = parse_cookies_to_dict(account.cookies)
    assert saved["odin_tt"] == "odin-748888"
    assert saved["msToken"] == "fresh-ms-token"



@pytest.mark.asyncio
@pytest.mark.parametrize("scope", [MAIN_SCOPE, REPLY_SCOPE])
async def test_user_schema_prepares_minimal_session_without_global_signer(monkeypatch, scope):
    account = account_with_minimal_cookies()
    records = normalize_cookies_for_browser(session_records()) + [{"name": "ttwid", "value": "wid"}]
    page = SimpleNamespace(
        url="https://www.tiktok.com/@demo/video/1234567890123456789",
        context=SimpleNamespace(cookies=AsyncMock(return_value=records)),
        goto=AsyncMock(), reload=AsyncMock(), wait_for_load_state=AsyncMock(),
    )
    monkeypatch.setattr(engagement_engine, "wait_for_runtime_signer_ready", AsyncMock(return_value=False))
    monkeypatch.setattr(engagement_engine.asyncio, "sleep", AsyncMock())
    monkeypatch.setattr(user_schema, "_visible_captcha_reason", AsyncMock(return_value=None))
    monkeypatch.setattr(user_schema, "get_browser_login_state", AsyncMock(return_value={"state": "unknown"}))
    client = SimpleNamespace()
    monkeypatch.setattr(user_schema.PlaywrightHttpClient, "from_page", AsyncMock(return_value=client))
    monkeypatch.setattr(user_schema._settings, "TIKTOK_NETWORK_FIRST", True)
    monkeypatch.setattr(user_schema._settings, "TIKTOK_NETWORK_IMPL_READY", True)

    session = await user_schema._prepare_user_schema_http_session(page, account, page.url, scope=scope, correlation_id="minimal")

    assert session is not None
    assert session.client is client
    assert session.cookies["sessionid"] == "b" * 32
    page.reload.assert_not_awaited()
