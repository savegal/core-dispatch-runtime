"""Real Chromium against intercepted local fixtures; no TikTok mutations."""
import json
import asyncio
import gc
from urllib.parse import urlparse
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from executor import engagement_engine as engine
from executor.http import playwright_http_client as transport
from executor.http.page_runtime import document_generation, prepared_runtime_profile
from executor.http.request_profile import RequestProfile
from executor.http.retry_policy import RetryPolicy
from tests.integration.test_http_browser_transport import browser_page as browser_page

pytestmark = [pytest.mark.integration, pytest.mark.asyncio]
VIDEO = "7635267329407913230"
URL = f"https://www.tiktok.com/@fixture/video/{VIDEO}"


async def test_native_history_updates_keep_document_but_full_reload_invalidates_it(browser_page):
    await browser_page.route("**/*", lambda route: route.fulfill(content_type="text/html", body="<html>Local fixture</html>"))
    await browser_page.goto(URL)
    profile = await transport._extract_runtime_profile(browser_page)
    generation = document_generation(browser_page)
    for method, url in [("replaceState", URL + "?_r=1"), ("pushState", URL.replace("/@fixture", ""))]:
        await browser_page.evaluate("([method, url]) => history[method]({}, '', url)", [method, url], isolated_context=False)
        assert document_generation(browser_page) == generation
    # The profile's URL changed, so it still needs fresh runtime extraction.
    assert prepared_runtime_profile(browser_page, profile) is None
    profile = await transport._extract_runtime_profile(browser_page)
    await browser_page.reload(wait_until="domcontentloaded")
    assert document_generation(browser_page) > generation
    assert prepared_runtime_profile(browser_page, profile) is None


@pytest.mark.parametrize("factory", [RequestProfile.publish_main_profile, RequestProfile.publish_reply_profile, RequestProfile.like_profile])
async def test_native_fetch_survives_canonicalization_before_dispatch(browser_page, monkeypatch, factory):
    requests = []
    async def serve(route):
        if urlparse(route.request.url).path.startswith("/api/comment/"):
            requests.append(route.request)
            await route.fulfill(content_type="application/json", body=json.dumps({"status_code": 0, "comment": {"cid": "7641761192486194454"}}))
        else:
            await route.fulfill(content_type="text/html", body="<html>Local fixture</html>")
    await browser_page.route("**/*", serve)
    await browser_page.goto(URL.replace("/@fixture", ""))
    original = transport.resolve_request_signature
    async def canonicalize(**kwargs):
        await browser_page.evaluate("url => history.replaceState({}, '', url)", URL + "?_r=1&_t=fixture", isolated_context=False)
        return await original(**kwargs)
    monkeypatch.setattr(transport, "resolve_request_signature", canonicalize)
    client = await transport.PlaywrightHttpClient.from_page(browser_page, retry_policy=RetryPolicy(max_retries=0))
    result = await client._execute_request(factory(), {"aweme_id": VIDEO, "cid": "7641761192486194454", "text": "fixture"})
    assert result[0], result[2]
    assert len(requests) == 1


async def test_native_warmup_allows_history_change_without_comment_probe(browser_page, monkeypatch):
    async def serve(route):
        assert not urlparse(route.request.url).path.startswith("/api/")
        await route.fulfill(content_type="text/html", body="<html>Local fixture</html>")
    await browser_page.route("**/*", serve)
    await browser_page.goto(URL.replace("/@fixture", ""))
    await browser_page.context.add_cookies([
        {"name": name, "value": "fixture", "domain": ".tiktok.com", "path": "/"}
        for name in ("sessionid", "ttwid", "tt_csrf_token", "msToken")
    ])
    async def ready(*_, **__):
        await browser_page.evaluate("url => history.replaceState({}, '', url)", URL + "?_r=1", isolated_context=False)
        return True
    monkeypatch.setattr(engine, "wait_for_runtime_signer_ready", ready)
    monkeypatch.setattr("executor.user_schema._visible_logged_out_reason", lambda *_, **__: _clear())
    monkeypatch.setattr("executor.user_schema._fast_visible_captcha_reason", lambda *_, **__: _clear())
    _, valid, info = await engine.warmup_page_for_http(browser_page, URL, max_retries=1)
    assert valid and info["comment_api_skipped"] and not info["comment_api_ready"]
    assert info["current_url"] == browser_page.url


async def _clear():
    return None


@pytest.mark.parametrize(("html", "blocked_by"), [
    ('<button aria-label="Open settings menu">Settings</button>', None),
    ('<button data-e2e="login-button">Log in</button>', "dead_session:visible_login_ui:"),
    ('<div id="captcha-verify-image">Drag the slider</div>', "captcha_challenge:slider"),
    ('<button data-e2e="login-button">Log in</button><div id="captcha-verify-image">Drag the slider</div>',
     "captcha_challenge:slider"),
])
async def test_confirmed_http_auth_still_checks_live_browser_ui(browser_page, monkeypatch, html, blocked_by):
    from executor import user_schema as schema

    await browser_page.route("**/*", lambda route: route.fulfill(content_type="text/html", body=html))
    await browser_page.goto(URL)
    await browser_page.context.add_cookies([
        {"name": "sessionid", "value": "fixture", "domain": ".tiktok.com", "path": "/"},
    ])
    monkeypatch.setattr(schema, "_has_recent_http_preflight_valid", lambda account: True)
    monkeypatch.setattr(schema, "_http_first_for_correlation", lambda *args: True)
    monkeypatch.setattr(schema, "_HTTP_PREFLIGHT_VALID_CACHE", {})
    for name in ("register_alive_session", "_register_dead_session", "_register_captcha_session"):
        monkeypatch.setattr(schema, name, Mock())

    ok, reason = await schema._ensure_account_login_ready(
        None, browser_page, SimpleNamespace(id=77, cookies='{"sessionid": "fixture"}', cookie_file=None),
        URL, timeout_ms=1750, correlation_id="fixture",
    )

    assert ok == (blocked_by is None)
    assert reason is None if blocked_by is None else reason.startswith(blocked_by)
    assert schema.register_alive_session.call_count == int(ok)
    assert schema._register_dead_session.call_count == int(bool(blocked_by and blocked_by.startswith("dead_session:")))
    assert schema._register_captcha_session.call_count == int(bool(blocked_by and blocked_by.startswith("captcha_challenge:")))


@pytest.mark.parametrize("confirmed_auth", [False, True])
async def test_browser_close_after_login_probe_timeout_retrieves_late_errors(browser_page, monkeypatch, confirmed_auth):
    from executor import user_schema as schema
    await browser_page.route("**/*", lambda route: route.fulfill(content_type="text/html", body="<html>Local fixture</html>"))
    await browser_page.goto(URL)
    loop = asyncio.get_running_loop()
    errors = []
    previous = loop.get_exception_handler()
    loop.set_exception_handler(lambda _, context: errors.append(context))
    async def slow_probe(page, **_):
        return await page.evaluate("() => new Promise(() => {})", isolated_context=False)
    try:
        if confirmed_auth:
            monkeypatch.setattr(schema, "get_browser_login_state", slow_probe)
            assert await schema._probe_confirmed_http_auth_ui(
                browser_page, correlation_id="fixture", phase="fixture", timeout_seconds=0.2,
            ) == (None, None)
        else:
            monkeypatch.setattr(schema, "_visible_logged_out_reason", slow_probe)
            for _ in range(5):
                assert await schema._deny_if_visible_logged_out(None, browser_page, None, correlation_id="fixture", phase="fixture", timeout_seconds=0.2) is None
        await browser_page.close()
        await asyncio.sleep(0.1)
        gc.collect()
        await asyncio.sleep(0.1)
        assert not errors, [item.get("message") for item in errors]
    finally:
        loop.set_exception_handler(previous)
