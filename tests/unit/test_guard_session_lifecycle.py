"""Guard imports require real authentication; DOM completion preserves identity."""
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from executor import engagement_engine as engine, user_schema as schema
from executor.http.page_runtime import document_generation, prepared_runtime_profile
from services import cookie_validator as validator
from tests.unit.test_http_warmup_recovery import warmup as warmup, URL

GUARD = "a" * 32


def exported_cookies(*, enriched=False, different_session=False):
    values = {"sid_guard": GUARD + "|1800000000|86400", "msToken": "imported", "tt-target-idc": "alisg"}
    if enriched:
        values.update(sessionid=GUARD, sid_tt=GUARD, sessionid_ss=GUARD, ttwid="web", tt_csrf_token="csrf")
    if different_session:
        values["sessionid"] = "b" * 32
    return json.dumps(values)


@pytest.fixture
def auth_caches(monkeypatch):
    for name in ("_HTTP_PREFLIGHT_VALID_CACHE", "_HTTP_PREFLIGHT_BROWSER_HANDOFFS",
                 "_HTTP_PREFLIGHT_BROWSER_FALLBACKS", "_HTTP_PREFLIGHT_USERNAME_CACHE", "_HTTP_PREFLIGHT_USER_ID_CACHE"):
        monkeypatch.setattr(schema, name, {})
    monkeypatch.setattr(schema._settings, "USE_PROXY", False)
    monkeypatch.setattr(schema, "_http_first_for_correlation", lambda *_: True)
    monkeypatch.setattr(schema, "http_first_enabled", lambda _: True)
    monkeypatch.setattr(schema, "mark_soft_error", Mock())


def test_dom_content_loaded_is_completion_not_document_replacement():
    callbacks = {}
    page = SimpleNamespace(url=URL, main_frame=object(), on=lambda event, cb: callbacks.update({event: cb}))
    generation = document_generation(page)
    snapshot = {"_snapshot_reusable": True, "_captured_at": engine.time.monotonic(),
                "current_url": URL, "_document_generation": generation}
    callbacks["domcontentloaded"]()
    assert document_generation(page) == generation
    assert prepared_runtime_profile(page, snapshot) is not None
    callbacks["request"](SimpleNamespace(frame=page.main_frame, is_navigation_request=lambda: True))
    assert document_generation(page) > generation
    assert prepared_runtime_profile(page, snapshot) is None


@pytest.mark.asyncio
@pytest.mark.parametrize("completion_stage", ["signer", "age_wait"])
async def test_delayed_dom_completion_gets_its_own_maturity_wait(monkeypatch, warmup, completion_stage):
    page, comments, sleeps = warmup
    callbacks = {}
    page.on = lambda event, cb: callbacks.update({event: cb})
    page.remove_listener = Mock()
    page.wait_for_load_state.side_effect = TimeoutError("initial DOM still loading")
    page.evaluate.return_value = {"current_url": URL, "navigation_url": URL, "age_ms": 5000, "dom_ready_ms": 0}
    sleep = engine.asyncio.sleep
    emitted = False
    async def emit_during_wait(seconds):
        nonlocal emitted
        if completion_stage == "age_wait" and not emitted:
            emitted = True
            await sleep(seconds - 0.1)
            callbacks["domcontentloaded"]()
            await sleep(0.1)
        else:
            await sleep(seconds)
    async def ready(*_, **__):
        if completion_stage == "signer":
            await sleep(0.3)
            callbacks["domcontentloaded"]()
        return True
    monkeypatch.setattr(engine, "wait_for_runtime_signer_ready", ready)
    monkeypatch.setattr(engine.asyncio, "sleep", emit_during_wait)
    _, valid, info = await engine.warmup_page_for_http(page, URL, max_retries=1)
    assert valid and info["reason_code"] == "ready"
    assert document_generation(page) == 0
    assert info["load_state"] == "domcontentloaded" and info["target_dom_age_ms"] >= 1500
    assert sum(sleeps) == pytest.approx(1.8 if completion_stage == "signer" else 2.9)
    comments.assert_not_awaited()


@pytest.mark.parametrize("enriched,different,expected", [(False, False, True), (True, False, True), (True, True, False)])
def test_guard_alias_shape_survives_enrichment_without_becoming_auth_evidence(enriched, different, expected):
    raw = exported_cookies(enriched=enriched, different_session=different)
    assert validator.has_guard_session_auth(raw) is expected
    if enriched and not different:
        assert not validator.has_guard_only_auth(raw)


def test_2038_web_token_is_kept_as_session_cookie_without_dropping_domains():
    records = [{"name": "sid_guard", "value": GUARD + "|1800000000|86400", "domain": ".tiktok.com"},
               *[{"name": "msToken", "value": value, "domain": domain, "expirationDate": 2147483647}
                 for domain, value in ((".tiktok.com", "web"), ("ads.tiktok.com", "ads"), ("shop.tiktok.com", "shop"))]]
    original = json.dumps(records)
    result = validator.normalize_cookies_for_browser(records)
    tokens = [cookie for cookie in result if cookie["name"] == "msToken"]
    assert len(tokens) == 1 and tokens[0]["value"] == "web"
    assert "expires" not in tokens[0]
    assert json.dumps(records) == original


@pytest.mark.asyncio
@pytest.mark.parametrize("state,passport,expected", [("unknown", False, False), ("logged_out", False, False),
                                                    ("logged_in", False, True), ("unknown", True, True),
                                                    ("logged_out", True, False), ("timeout", False, False)])
async def test_session_aliases_alone_cannot_mark_an_account_alive(monkeypatch, auth_caches, state, passport, expected):
    account = SimpleNamespace(id=99101, cookies=exported_cookies(), proxy=None)
    page = SimpleNamespace(url=URL, context=SimpleNamespace(cookies=AsyncMock(return_value=[
        {"name": "sessionid", "value": GUARD, "domain": ".tiktok.com"}])) )
    if passport:
        schema._HTTP_PREFLIGHT_VALID_CACHE[schema._http_preflight_cache_key(account)] = (schema.time.monotonic(), "test_passport")
    monkeypatch.setattr(schema, "detect_captcha", AsyncMock(return_value=(False, None)))
    monkeypatch.setattr(schema, "dismiss_cookie_banners", AsyncMock())
    if state == "timeout":
        monkeypatch.setattr(schema, "_deny_if_visible_logged_out", AsyncMock(return_value=None))
    else:
        monkeypatch.setattr(schema, "get_browser_login_state", AsyncMock(return_value={"state": state}))
    alive, dead = Mock(), Mock()
    monkeypatch.setattr(schema, "register_alive_session", alive)
    monkeypatch.setattr(schema, "_register_dead_session", dead)
    ok, reason = await schema._ensure_account_login_ready(None, page, account, URL, timeout_ms=1000, correlation_id="task_1244_reply")
    assert ok is expected
    assert alive.call_count == int(expected)
    if not expected:
        assert reason.startswith("dead_session:" if state == "logged_out" else "http_auth_unconfirmed:")
        assert not getattr(page, "_http_auth_verified_at", None)
    elif not passport:
        assert schema._has_recent_http_preflight_valid(account)
        # Browser enrichment does not change the auth/route key.
        account.cookies = exported_cookies(enriched=True)
        validate = AsyncMock(side_effect=AssertionError("Positive browser proof must survive the next task"))
        monkeypatch.setattr(validator, "validate_account_http", validate)
        assert await schema._preflight_cookie_http_gate(None, account, correlation_id="task_1245_reply", phase="reply_pre_browser") is None
        validate.assert_not_awaited()


@pytest.mark.asyncio
async def test_enriched_guard_session_gets_bounded_verification_after_positive_cache_expires(monkeypatch, auth_caches):
    account = SimpleNamespace(id=99102, cookies=exported_cookies(enriched=True), proxy=None)
    key = schema._http_preflight_cache_key(account)
    schema._HTTP_PREFLIGHT_VALID_CACHE[key] = (schema.time.monotonic() - 301, "browser_auth")
    validate = AsyncMock(return_value={"status": "inconclusive", "valid": False, "definitive": False,
                                      "error": "Passport response did not confirm authentication"})
    monkeypatch.setattr(validator, "validate_account_http", validate)
    soft = Mock()
    monkeypatch.setattr(schema, "mark_soft_error", soft)
    assert await schema._preflight_cookie_http_gate(None, account, correlation_id="task_1246_reply", phase="reply_pre_browser") is None
    assert account._http_preflight_requires_browser_warmup
    soft.assert_not_called()
    assert not schema._has_recent_http_preflight_valid(account)


@pytest.mark.asyncio
@pytest.mark.parametrize("canonicalize", [False, True])
async def test_browser_auth_waits_for_hydration_and_keeps_same_video_proof(monkeypatch, auth_caches, canonicalize):
    callbacks = {}
    account = SimpleNamespace(id=99103, cookies=exported_cookies(), proxy=None)
    page = SimpleNamespace(url=URL, main_frame=object(), on=lambda event, cb: callbacks.update({event: cb}),
                           context=SimpleNamespace(cookies=AsyncMock(return_value=[
                               {"name": "sessionid", "value": GUARD, "domain": ".tiktok.com"}])))
    reads = []
    async def state(*_):
        reads.append(1)
        if canonicalize:
            page.url = URL + "?_r=1&_t=fixture"
            callbacks["framenavigated"](page.main_frame)
            return {"state": "logged_in"}
        return {"state": "logged_in" if len(reads) >= 3 else "unknown"}
    monkeypatch.setattr(schema, "detect_captcha", AsyncMock(return_value=(False, None)))
    monkeypatch.setattr(schema, "dismiss_cookie_banners", AsyncMock())
    monkeypatch.setattr(schema, "get_browser_login_state", state)
    monkeypatch.setattr(schema.asyncio, "sleep", AsyncMock())
    alive = Mock()
    monkeypatch.setattr(schema, "register_alive_session", alive)
    ok, reason = await schema._ensure_account_login_ready(None, page, account, URL, timeout_ms=1000, correlation_id="task_1244_reply")
    assert ok, reason
    alive.assert_called_once()
    assert len(reads) == (1 if canonicalize else 3)


@pytest.mark.asyncio
async def test_auth_probe_between_reload_request_and_commit_cannot_transfer_proof(monkeypatch):
    callbacks = {}
    page = SimpleNamespace(url=URL, main_frame=object(), on=lambda event, cb: callbacks.update({event: cb}))
    document_generation(page)
    callbacks["request"](SimpleNamespace(frame=page.main_frame, is_navigation_request=lambda: True))
    monkeypatch.setattr(schema, "detect_captcha", AsyncMock(return_value=(False, None)))
    monkeypatch.setattr(schema, "dismiss_cookie_banners", AsyncMock())
    monkeypatch.setattr(schema, "get_browser_login_state", AsyncMock(return_value={"state": "logged_in"}))
    await schema._fast_visible_captcha_reason(page, phase="before_reload_commit")
    await schema._visible_logged_out_reason(page, phase="before_reload_commit")
    assert schema._has_recent_http_browser_auth(page)
    callbacks["framenavigated"](page.main_frame)
    assert not schema._has_recent_http_browser_auth(page)
