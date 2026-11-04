"""Task 1246: imported token provenance and a task-wide browser-check budget."""
import asyncio
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from executor import engagement_engine as engine, user_schema as schema
from services import cookie_validator as validator
from tests.unit.test_http_warmup_recovery import warmup as warmup, URL
from executor.http.playwright_http_client import PlaywrightHttpClient
from executor.http.request_profile import RequestProfile
from executor.http.retry_policy import RetryPolicy
from urllib.parse import parse_qs, urlparse


def account(number):
    return SimpleNamespace(id=number, proxy=None, cookie_file=None, cookies=json.dumps({
        "sid_guard": "a" * 32 + "|1800000000|86400", "msToken": "imported",
    }))


@pytest.mark.parametrize("expiry", [1840000000, 2147483647])
def test_imported_token_freshness_is_context_local_and_not_based_on_expiry(expiry):
    first, second = SimpleNamespace(), SimpleNamespace()
    records = [{"name": "msToken", "value": "imported", "domain": ".tiktok.com", "expires": expiry}]
    validator.remember_imported_ms_token(first, records)
    validator.remember_imported_ms_token(second, records)
    assert validator.ms_token_needs_browser_refresh(first, {"msToken": "imported"})
    validator.remember_observed_ms_token(first, {"msToken": "imported"})
    assert not validator.ms_token_needs_browser_refresh(first, {"msToken": "imported"})
    assert validator.ms_token_needs_browser_refresh(second, {"msToken": "imported"})
    assert not validator.ms_token_needs_browser_refresh(second, {"msToken": "server-rotated"})
    assert "imported" not in vars(first).values()


@pytest.mark.asyncio
@pytest.mark.parametrize("read_succeeds", [True, False])
async def test_unchanged_import_cannot_skip_read_warmup_even_with_all_cookies(warmup, read_succeeds):
    page, comments, _ = warmup
    current = {key: key for key in ("sessionid", "ttwid", "tt_csrf_token", "msToken")}
    imported = SimpleNamespace(id=None, cookies=json.dumps(current))
    comments.return_value = {"comments": []} if read_succeeds else None
    _, ready, info = await engine.warmup_page_for_http(page, URL, account=imported, max_retries=1)
    assert ready is read_succeeds
    assert info["ms_token_from_import_only"] and info["ms_token_refresh_pending"] is (not read_succeeds)
    comments.assert_awaited_once()
    if read_succeeds:
        page._cached_first_comment_page = None
        _, ready, info = await engine.warmup_page_for_http(page, URL, account=imported, max_retries=1)
        assert ready and info["comment_api_skipped"]
        comments.assert_awaited_once()
    else:
        assert info["reason_code"] == "missing_msToken_refresh_unconfirmed"


@pytest.mark.asyncio
async def test_unready_signer_uses_native_activation_entry_point(monkeypatch, warmup):
    page, comments, _ = warmup
    monkeypatch.setattr(engine, "wait_for_runtime_signer_ready", AsyncMock(return_value=False))
    native = AsyncMock(return_value={"comments": []})
    monkeypatch.setattr("executor.http.comment_discovery.activate_comment_list_via_page", native)
    _, ready, info = await engine.warmup_page_for_http(page, URL, max_retries=1)
    assert not ready and info["comment_api_native_only"]
    native.assert_awaited_once()
    comments.assert_not_awaited()


@pytest.mark.asyncio
async def test_same_video_reload_rebases_age_and_rechecks_runtime(monkeypatch, warmup):
    page, _, sleeps = warmup
    checks = []
    async def ready(*_, **__):
        checks.append(1)
        if len(checks) == 1:
            page._http_document_generation += 1
        return True
    monkeypatch.setattr(engine, "wait_for_runtime_signer_ready", ready)
    _, valid, info = await engine.warmup_page_for_http(page, URL, max_retries=1)
    assert valid and info["document_rebased"]
    assert len(checks) == 2 and sum(sleeps) >= 3.2
    assert info["target_page_age_ms"] >= 3200


@pytest.mark.asyncio
async def test_parallel_selection_shares_two_browser_permits_per_task(monkeypatch):
    for name in ("_HTTP_PREFLIGHT_VALID_CACHE", "_HTTP_PREFLIGHT_BROWSER_FALLBACKS", "_HTTP_PREFLIGHT_BROWSER_HANDOFFS"):
        monkeypatch.setattr(schema, name, {})
    monkeypatch.setattr(schema._settings, "USE_PROXY", False)
    monkeypatch.setattr(schema, "http_first_enabled", lambda _: True)
    validate = AsyncMock(return_value={"status": "inconclusive", "definitive": False,
                                      "error": "Passport response did not confirm authentication"})
    monkeypatch.setattr(validator, "validate_account_http", validate)
    soft = Mock()
    monkeypatch.setattr(schema, "mark_soft_error", soft)
    candidates = [account(99400 + index) for index in range(5)]
    reasons = await asyncio.gather(*[
        schema._preflight_cookie_http_gate(None, candidate, correlation_id=f"task_1246_select_acc{candidate.id}", phase="reply_selection")
        for candidate in candidates
    ])
    granted = [candidate for candidate, reason in zip(candidates, reasons) if reason is None]
    assert len(granted) == 2 and soft.call_count == 3
    for candidate in granted:
        assert await schema._preflight_cookie_http_gate(None, candidate, correlation_id=f"task_1246_reply_acc{candidate.id}", phase="reply_pre_browser") is None
    assert validate.await_count == 5  # Handoff consumption does not rerun Passport or spend a permit.
    assert await schema._preflight_cookie_http_gate(None, account(99410), correlation_id="task_1247_main", phase="main_pre_browser") is None
    validate.return_value = {"status": "valid", "valid": True}
    assert await schema._preflight_cookie_http_gate(None, account(99411), correlation_id="task_1246_select", phase="reply_selection") is None


@pytest.mark.asyncio
async def test_unknown_browser_auth_is_softly_marked_and_cannot_be_reselected(monkeypatch):
    clock = [0.0]
    async def sleep(delay):
        clock[0] += delay
    monkeypatch.setattr(schema.time, "monotonic", lambda: clock[0])
    monkeypatch.setattr(schema.asyncio, "sleep", sleep)
    monkeypatch.setattr(schema, "_has_recent_http_preflight_valid", lambda _: False)
    monkeypatch.setattr(schema, "_http_first_for_correlation", lambda *_: True)
    monkeypatch.setattr(schema, "_fast_visible_captcha_reason", AsyncMock(return_value=None))
    monkeypatch.setattr(schema, "_deny_if_visible_logged_out", AsyncMock(return_value=None))
    soft = Mock()
    monkeypatch.setattr(schema, "mark_soft_error", soft)
    candidate = account(99420)
    key = schema._http_preflight_cache_key(candidate)
    schema._HTTP_PREFLIGHT_BROWSER_HANDOFFS[schema._http_preflight_handoff_key(key, "task_1246_select")] = 0.0
    page = SimpleNamespace(url=URL, context=SimpleNamespace(cookies=AsyncMock(return_value=[
        {"name": "sessionid", "value": "session", "domain": ".tiktok.com"}])))
    ok, reason = await schema._ensure_account_login_ready(None, page, candidate, URL, timeout_ms=1, correlation_id="task_1246_reply")
    assert not ok and reason == "http_auth_unconfirmed:browser_login_state"
    soft.assert_called_once_with(None, candidate.id, reason)
    assert schema._http_preflight_handoff_key(key, "task_1246_select") not in schema._HTTP_PREFLIGHT_BROWSER_HANDOFFS
    validate = AsyncMock(side_effect=AssertionError("Failed account must not be selected again in this task"))
    monkeypatch.setattr(validator, "validate_account_http", validate)
    assert await schema._preflight_cookie_http_gate(None, candidate, correlation_id="task_1246_select", phase="reply_selection") == "http_preflight:browser_auth_failed_this_task"
    validate.assert_not_awaited()


@pytest.mark.asyncio
async def test_empty_publish_keeps_rotated_header_token_without_retry_or_overriding_newer_browser_token():
    jar = {"msToken": "imported"}
    sent = []
    async def cookies():
        return [{"name": key, "value": value, "domain": ".tiktok.com", "path": "/"} for key, value in jar.items()]
    async def add_cookies(records):
        jar.update({item["name"]: item["value"] for item in records})
    async def evaluate(script, request, **_):
        sent.append(parse_qs(urlparse(request["url"]).query).get("msToken", [None])[0])
        return {"status": 200, "headers": {"content-type": "application/json", **({"X-Ms-Token": "server-fresh"} if len(sent) == 1 else {})},
                "url": request["url"], "text": "" if len(sent) == 1 else '{"status_code":0,"comment":{"cid":"7641761192486194454"}}'}
    page = SimpleNamespace(url=URL, context=SimpleNamespace(cookies=cookies, add_cookies=add_cookies), evaluate=evaluate)
    client = PlaywrightHttpClient(SimpleNamespace(), page=page, retry_policy=RetryPolicy(max_retries=3))
    result = await client._execute_request(RequestProfile.publish_main_profile(), {"aweme_id": "7635267329407913230", "text": "fixture"})
    assert result[0] is False
    assert sent == ["imported"] and jar["msToken"] == "server-fresh"
    assert client.last_request_meta["publication_status"] == "submitted_unconfirmed"
    jar["msToken"] = "newer-browser-token"
    result = await client._execute_request(RequestProfile.publish_main_profile(), {"aweme_id": "7635267329407913230", "text": "next"})
    assert result[0] and sent[-1] == "newer-browser-token"


@pytest.mark.asyncio
@pytest.mark.parametrize("success", [True, False])
async def test_parent_auto_like_runs_only_after_successful_reply(monkeypatch, success):
    calls = []
    async def reply_comment(**_):
        calls.append("reply")
        return success, "7641761192486194454" if success else None, None if success else "empty", None, {"parent_confirmed": True}
    async def like_comment(**_):
        calls.append("like")
        return True, None, None
    client = SimpleNamespace(reply_comment=reply_comment, like_comment=like_comment, last_request_meta={"request_attempted": True})
    session = schema._UserSchemaHttpSession(schema.REPLY_SCOPE, 99430, client, {}, "7635267329407913230", URL)
    monkeypatch.setattr(schema, "_like_parent_before_reply_enabled", lambda: True)
    monkeypatch.setattr(schema, "can_boost_parent", lambda *_: True)
    monkeypatch.setattr(schema, "record_parent_boost", Mock())
    monkeypatch.setattr(schema, "_record_branch_published_reply", Mock())
    monkeypatch.setattr(schema.asyncio, "sleep", AsyncMock())
    await schema._publish_reply_http_first_if_eligible(session, "fixture", parent_comment_id="7699899442673468212", correlation_id="task_1246_reply")
    assert calls == (["reply", "like"] if success else ["reply"])
