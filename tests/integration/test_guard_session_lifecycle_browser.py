"""Browser lifecycle and guard import checks against intercepted local pages."""
import asyncio
import json
import time
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from executor import engagement_engine as engine, user_schema as schema
from executor.http.page_runtime import document_generation
from services import cookie_validator as validator
from tests.integration.test_http_browser_transport import browser_page as browser_page

pytestmark = [pytest.mark.integration, pytest.mark.asyncio]
URL = "https://www.tiktok.com/@fixture/video/7635267329407913230"
GUARD = "a" * 32


async def test_dom_finishing_after_five_second_timeout_keeps_same_document(browser_page, monkeypatch):
    release_script = asyncio.Event()
    async def serve(route):
        if route.request.url.endswith("/slow.js"):
            await release_script.wait()
            await route.fulfill(content_type="application/javascript", body="window.localFixtureLoaded = true;")
        else:
            await route.fulfill(content_type="text/html", body='<html><script defer src="/slow.js"></script><body>Local</body></html>')
    await browser_page.route("**/*", serve)
    await browser_page.goto(URL, wait_until="commit")
    original_generation = document_generation(browser_page)
    await browser_page.context.add_cookies([
        {"name": name, "value": "fixture", "domain": ".tiktok.com", "path": "/"}
        for name in ("sessionid", "ttwid", "tt_csrf_token", "msToken")
    ])
    async def ready(*_, **__):
        release_script.set()
        await browser_page.wait_for_load_state("domcontentloaded")
        return True
    monkeypatch.setattr(engine, "wait_for_runtime_signer_ready", ready)
    monkeypatch.setattr(schema, "_visible_logged_out_reason", AsyncMock(return_value=None))
    monkeypatch.setattr(schema, "_fast_visible_captcha_reason", AsyncMock(return_value=None))
    started = time.monotonic()
    try:
        _, valid, info = await engine.warmup_page_for_http(browser_page, URL, max_retries=1)
        assert valid, info["reason_code"]
        assert time.monotonic() - started >= 5
        assert document_generation(browser_page) == original_generation
        assert info["target_dom_age_ms"] >= 1500 - 32
        assert info["load_state"] == "domcontentloaded"
    finally:
        release_script.set()


@pytest.mark.parametrize("state,expected", [("logged_in", True), ("logged_out", False), ("unknown", False), ("hydrating", True)])
async def test_guard_import_requires_real_browser_auth_markers(browser_page, monkeypatch, state, expected):
    proof = ('<script id="SIGI_STATE" type="application/json">{"isLogin":true}</script>' if state == "logged_in"
             else '<script id="SIGI_STATE" type="application/json">{"isLogin":false}</script>' if state == "logged_out" else "")
    if state == "hydrating":
        proof = """<script>setTimeout(() => {
            const state = document.createElement('script'); state.id = 'SIGI_STATE';
            state.type = 'application/json'; state.textContent = '{"isLogin":true}';
            document.body.append(state);
            history.replaceState({}, '', location.pathname + '?_r=1&_t=fixture');
        }, 800);</script>"""
    await browser_page.route("**/*", lambda route: route.fulfill(content_type="text/html", body=f"<html><body>{proof}Local page</body></html>"))
    await browser_page.goto(URL, wait_until="commit" if state == "hydrating" else "load")
    records = [{"name": "sid_guard", "value": GUARD + "|1800000000|86400", "domain": ".tiktok.com"},
               {"name": "tt-target-idc", "value": "alisg", "domain": ".tiktok.com"},
               *[{"name": "msToken", "value": domain, "domain": domain, "expirationDate": 2147483647}
                 for domain in (".tiktok.com", "ads.tiktok.com", "shop.tiktok.com")]]
    account = SimpleNamespace(id=99201, cookies=json.dumps(records), proxy=None)
    await browser_page.context.add_cookies(validator.normalize_cookies_for_browser(records))
    for name in ("_HTTP_PREFLIGHT_VALID_CACHE", "_HTTP_PREFLIGHT_USERNAME_CACHE", "_HTTP_PREFLIGHT_USER_ID_CACHE"):
        monkeypatch.setattr(schema, name, {})
    monkeypatch.setattr(schema._settings, "USE_PROXY", False)
    monkeypatch.setattr(schema, "_http_first_for_correlation", lambda *_: True)
    monkeypatch.setattr(schema, "http_first_enabled", lambda _: True)
    alive = Mock()
    monkeypatch.setattr(schema, "register_alive_session", alive)
    monkeypatch.setattr(schema, "_register_dead_session", Mock())
    monkeypatch.setattr(schema, "mark_soft_error", Mock())
    ok, reason = await schema._ensure_account_login_ready(None, browser_page, account, URL, timeout_ms=5000 if state == "hydrating" else 1000, correlation_id="task_1244_reply")
    assert ok is expected, reason
    assert alive.call_count == int(expected)
    live = await browser_page.context.cookies()
    token = next(cookie for cookie in live if cookie["name"] == "msToken")
    assert token["value"] == ".tiktok.com" and token["expires"] == -1
    if expected:
        account.cookies = json.dumps({"sid_guard": GUARD + "|1800000000|86400", "sessionid": GUARD,
                                      "sid_tt": GUARD, "sessionid_ss": GUARD, "ttwid": "web", "tt_csrf_token": "csrf"})
        validate = AsyncMock(side_effect=AssertionError("Fresh positive browser auth must survive enrichment"))
        monkeypatch.setattr(validator, "validate_account_http", validate)
        assert await schema._preflight_cookie_http_gate(None, account, correlation_id="task_1245_reply", phase="reply_pre_browser") is None
        validate.assert_not_awaited()
        if state == "logged_in":
            await browser_page.reload(wait_until="domcontentloaded")
            assert not schema._has_recent_http_browser_auth(browser_page)
            await schema._visible_logged_out_reason(browser_page, phase="after_reload")
            assert not schema._has_recent_http_browser_auth(browser_page)
    else:
        assert not schema._has_recent_http_browser_auth(browser_page)
