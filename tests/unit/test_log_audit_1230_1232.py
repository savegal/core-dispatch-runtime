"""Reproduce UID-only Passport accounts and misleading response-URL diagnostics."""
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from executor import engagement_engine, main_visibility, reply_visibility, user_schema as schema
from executor.http import comment_discovery as api
from executor.http.request_evidence import observe_request_security, resolve_security_evidence
from executor.visibility_support import account_identity_keys, identities_independent


pytestmark = [pytest.mark.unit, pytest.mark.asyncio]
VIDEO = "7635267329407913230"
URL = f"https://www.tiktok.com/@demo/video/{VIDEO}"
CID = "7697345157284266772"
PARENT = "7697345157284266750"


@pytest.fixture
def identity_cache(monkeypatch):
    for name in ("_HTTP_PREFLIGHT_VALID_CACHE", "_HTTP_PREFLIGHT_USERNAME_CACHE", "_HTTP_PREFLIGHT_USER_ID_CACHE"):
        monkeypatch.setattr(schema, name, {})


async def test_uid_only_passport_is_cached_without_becoming_a_username(monkeypatch, identity_cache):
    from services import cookie_validator
    validate = AsyncMock(return_value={"status": "valid", "valid": True, "user_id": "123456789", "username": None})
    monkeypatch.setattr(cookie_validator, "validate_account_http", validate)
    account = SimpleNamespace(id=1, cookies='{"sessionid":"session"}', cookie_file=None)
    assert await schema._preflight_cookie_http_gate(None, account, correlation_id="fixture", phase="reply_visibility_pre_browser") is None
    assert schema._get_cached_http_preflight_user_id(account) == "123456789"
    assert schema._get_cached_http_preflight_username(account) is None
    other_instance = SimpleNamespace(id=1)
    assert schema._get_cached_http_preflight_user_id(other_instance) == "123456789"
    schema._forget_http_preflight_valid(account)
    assert schema._get_cached_http_preflight_user_id(account) is None
    assert schema._get_cached_http_preflight_user_id(other_instance) is None


@pytest.mark.parametrize("mode", ["main", "reply"])
@pytest.mark.parametrize("duplicate", [None, "same_session", "same_uid", "unknown"])
@pytest.mark.parametrize("saved_uid", [False, True])
async def test_uid_only_visibility_and_duplicate_rejection_happen_before_browser(monkeypatch, identity_cache, mode, duplicate, saved_uid):
    accounts = {i: SimpleNamespace(id=i, proxy=None, cookies=json.dumps({"sessionid": f"session-{i}"}), cookie_file=None)
                for i in (1, 8, 9)}
    if duplicate == "same_session":
        accounts[8].cookies = accounts[1].cookies
    if duplicate == "same_uid":
        accounts[8]._cached_tiktok_uid = "1001"
    selected = iter([accounts[8], accounts[9]])
    monkeypatch.setattr(schema, "get_free_account_for_reply", Mock(side_effect=lambda *args, **kwargs: next(selected, None)))
    events = []

    async def preflight(db, account, **kwargs):
        events.append(("passport", account.id))
        if duplicate != "unknown" or account.id == 1:
            account._cached_tiktok_uid = str(1000 + account.id)

    monkeypatch.setattr(schema, "_preflight_cookie_http_gate", AsyncMock(side_effect=preflight))

    async def create(account):
        events.append(("browser", account.id))
        return object(), SimpleNamespace(url=URL)

    browser = AsyncMock(side_effect=create)
    monkeypatch.setattr(schema, "create_page_for_account", browser)
    monkeypatch.setattr(schema, "_ensure_account_login_ready", AsyncMock(return_value=(True, None)))
    monkeypatch.setattr(schema, "_safe_close", AsyncMock())
    release = Mock()
    monkeypatch.setattr(schema, "release_reply_account_reservation", release)
    monkeypatch.setattr(engagement_engine, "warmup_page_for_http", AsyncMock(return_value=({}, True, {})))
    monkeypatch.setattr(schema._settings, "TIKTOK_NETWORK_FIRST", True)
    monkeypatch.setattr(schema._settings, "TIKTOK_NETWORK_IMPL_READY", True)
    monkeypatch.setattr(schema._settings, "MAX_RETRY_ACCOUNTS", 1)
    for prefix in ("MAIN_VISIBILITY", "REPLY_PUBLICATION_VISIBILITY"):
        monkeypatch.setattr(schema._settings, f"{prefix}_INITIAL_DELAY_SECONDS", 0)
        monkeypatch.setattr(schema._settings, f"{prefix}_RECHECK_DELAY_SECONDS", 0)
    db = SimpleNamespace(get=lambda model, key: accounts[key])
    uid = "1001" if saved_uid else None
    scan = AsyncMock(return_value={"found_ids": [CID], "absent_ids": [], "complete": False, "reason": "all_found"})
    if mode == "main":
        monkeypatch.setattr(main_visibility, "scan_main_comment_ids", scan)
        monkeypatch.setattr(schema, "_save_main_visibility_checkpoint", Mock())
        task = SimpleNamespace(id=1230, settings={"verify_main_visibility": True})
        result = schema.UserSchemaResult(published_mains=[schema.PublishedMain("text", CID, True, False, "http", 1, None, author_uid=uid)])
        await schema._verify_order_main_visibility(db, task, URL, result, task_id=task.id, pool_owner_user_id=None)
        report = result.main_visibility
    else:
        monkeypatch.setattr(reply_visibility, "scan_reply_comment_ids", scan)
        monkeypatch.setattr(reply_visibility, "save_reply_visibility_checkpoint", Mock())
        task = SimpleNamespace(id=1230, settings={"verify_reply_visibility": True}, replies=["text"])
        result = {"status": "success", "engagement": {"main": {}, "replies": {"submitted": 1, "expected": 1, "items": [{
            "text": "text", "reply_comment_id": CID, "parent_comment_id": PARENT, "account_id": 1, "author_uid": uid,
        }]}}}
        await reply_visibility.verify_order_reply_visibility(db, task, URL, result)
        report = result[reply_visibility.REPORT_KEY]
    if duplicate == "unknown":
        assert report["status"] == "unconfirmed"
        assert report["checks"][0]["reason"] == "observer_identity_unavailable"
        browser.assert_not_awaited()
        scan.assert_not_awaited()
    else:
        assert report["status"] == "confirmed" and report["confirmed"] == 1
        assert browser.await_count == scan.await_count == report["observer_attempts"] == 1
        assert report["observer_candidates"] == 1
        expected_account = 9 if duplicate else 8
        assert ("passport", expected_account) in events
        assert events.index(("passport", expected_account)) < events.index(("browser", expected_account))
        if duplicate:
            assert ("browser", 8) not in events and ("passport", 8) not in events
            assert report["observer_identity_skips"] == 1 and release.call_count == 2


async def test_different_sessions_without_user_identity_are_not_positive_independence():
    left = SimpleNamespace(id=1, cookies='{"sessionid":"a"}')
    right = SimpleNamespace(id=2, cookies='{"sessionid":"b"}')
    assert not identities_independent(account_identity_keys(left), [account_identity_keys(right)])


@pytest.mark.parametrize("separator", ["|", "%7C"])
async def test_sid_guard_prefix_detects_duplicate_imports_without_sessionid(separator):
    token = "ab" * 16
    author = SimpleNamespace(id=1, cookies=json.dumps({"sessionid": token}))
    observer = SimpleNamespace(id=2, cookies=json.dumps({"sid_guard": token + separator + "1700000000"}))
    common = account_identity_keys(author) & account_identity_keys(observer)
    assert any(family == "session" for family, value in common)


@pytest.mark.parametrize("mode", ["main", "reply"])
async def test_get_retries_use_clean_urls_live_cookies_and_refresh_scan_batch(monkeypatch, mode):
    runtime = AsyncMock(return_value={})
    monkeypatch.setattr(api, "_extract_runtime_profile", runtime)
    monkeypatch.setattr(api.asyncio, "sleep", AsyncMock())
    urls = []
    manual_sign_calls = []

    async def evaluate(script, url, **kwargs):
        assert kwargs["isolated_context"] is False
        if "byted_acrawler" in script:
            manual_sign_calls.append(url)
            return {"x_bogus": "manual-bogus", "signer_available": True}
        urls.append(url)
        index = len(urls)
        response_url = url if index == 1 else url + f"&X-Bogus=native-{index}&X-Gnarly=native-{index}"
        body = {"status_code": 0, "comments": [{"cid": CID if index == 4 else str(int(CID) + 1),
                "reply_id": PARENT if mode == "reply" else "0"}], "has_more": int(index < 4), "cursor": 20}
        return {"status": 200, "url": response_url, "text": "" if index == 2 else json.dumps(body)}

    cookies = AsyncMock(side_effect=[[
        {"name": "msToken", "value": f"token-{version}", "domain": ".tiktok.com"},
        {"name": "WebIdLastTime", "value": f"time-{version}", "domain": ".tiktok.com"},
        {"name": "X-Bogus", "value": "stale-cookie-signature", "domain": ".tiktok.com"},
    ] for version in (1, 2, 3)])
    page = SimpleNamespace(url=URL, context=SimpleNamespace(cookies=cookies), evaluate=AsyncMock(side_effect=evaluate))
    report = (await main_visibility.scan_main_comment_ids(page, VIDEO, [CID]) if mode == "main"
              else await reply_visibility.scan_reply_comment_ids(page, VIDEO, PARENT, [CID]))
    assert report["found_ids"] == [CID]
    from urllib.parse import parse_qs, urlparse
    queries = [parse_qs(urlparse(url).query) for url in urls]
    assert len(queries) == 4 and not manual_sign_calls
    for query, version, cursor in zip(queries, (1, 2, 3, 3), (0, 0, 0, 20)):
        assert query["msToken"] == [f"token-{version}"]
        assert query["WebIdLastTime"] == [f"time-{version}"]
        assert query["cursor"] == [str(cursor)]
        assert not {"X-Bogus", "X-Gnarly", "a_bogus"} & query.keys()
    assert cookies.await_count == 3 and runtime.await_count == 1


async def test_wire_evidence_survives_stripped_response_url_and_ignores_other_requests():
    listeners = {}
    page = SimpleNamespace(on=lambda name, callback: listeners.update({name: callback}), remove_listener=lambda name, callback: listeners.pop(name))
    endpoint = f"https://www.tiktok.com/api/comment/list/?aweme_id={VIDEO}&cursor=0"
    with observe_request_security(page, endpoint) as evidence:
        listeners["request"](SimpleNamespace(method="POST", url=endpoint))
        listeners["request"](SimpleNamespace(method="GET", url=endpoint.replace(VIDEO, "other") + "&X-Bogus=secret"))
        assert evidence == {}
        listeners["request"](SimpleNamespace(method="GET", url=endpoint + "&X-Bogus=secret&X-Gnarly=secret"))
        listeners["request"](SimpleNamespace(method="GET", url=endpoint))  # Redirect without signatures.
    assert listeners == {}
    result = resolve_security_evidence(evidence, endpoint)
    assert result["security_params_source"] == "request_event"
    assert result["security_params"]["X-Bogus"] and not result["response_security_params"]["X-Bogus"]
    assert "secret" not in json.dumps(result)
