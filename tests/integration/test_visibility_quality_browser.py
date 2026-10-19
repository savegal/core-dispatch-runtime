"""Full visibility HTTP paths against locally fulfilled Chromium requests."""
import json
import time
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock
from urllib.parse import parse_qs, urlparse

import pytest
from patchright.async_api import async_playwright

from executor.http import comment_discovery as api
from executor.http.playwright_http_client import PlaywrightHttpClient
from executor import main_visibility, reply_visibility, user_schema as schema

pytestmark = [pytest.mark.integration, pytest.mark.asyncio]
VIDEO = "7635267329407913230"
CID = "7697345157284266772"
PARENT = "7697345157284266750"
URL = f"https://www.tiktok.com/@fixture/video/{VIDEO}"


@pytest.fixture
async def browser():
    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch(headless=True)
        try:
            yield browser
        finally:
            await browser.close()


async def open_fixture(browser, state):
    context = await browser.new_context(service_workers="block")
    async def respond(route):
        request = route.request
        path = urlparse(request.url).path
        if path == "/api/comment/publish/":
            assert request.method == "POST"
            state["posts"] += 1
            form = parse_qs(request.post_data or "")
            assert form["text"] == ["hello"]
            if state["mode"] == "reply":
                assert form["reply_id"] == [PARENT]
            else:
                assert "reply_id" not in form
            body = {"status_code": 0, "comment": {"cid": CID, "reply_id": PARENT if state["mode"] == "reply" else "0"}}
            text = "" if state.get("empty_post") else json.dumps(body)
        elif path in {"/api/comment/list/", "/api/comment/list/reply/"}:
            query = parse_qs(urlparse(request.url).query)
            assert "X-Bogus" in query and "X-Gnarly" in query
            assert query["item_id" if state["mode"] == "reply" else "aweme_id"] == [VIDEO]
            cursor = int(query.get("cursor", [0])[0])
            state["gets"] += 1
            rows = [] if cursor == 100 else [{"cid": str(int(CID) + cursor + 1), "aweme_id": VIDEO,
                                              "reply_id": PARENT if state["mode"] == "reply" else "0"}]
            text = json.dumps({"status_code": 0, "comments": rows, "has_more": 0 if cursor == 100 else 1,
                               "cursor": 50 if cursor == 0 else 100})
        else:
            await route.fulfill(content_type="text/html", body="<html>Local fixture; no comment controls</html>")
            return
        await route.fulfill(content_type="application/json", body=text)
    await context.route("**/*", respond)
    page = await context.new_page()
    await page.goto(URL)
    await page.evaluate("""() => {
        const original = window.fetch;
        window.fetch = function(input, options) {
            const url = new URL(String(input), location.href);
            url.searchParams.set('X-Bogus', 'fixture');
            url.searchParams.set('X-Gnarly', 'fixture');
            return original.call(this, url.href, options);
        };
        window.addEventListener('mousemove', () => {
            fetch('/api/background/').then(response => response.text());
        }, {once: true});
    }""", isolated_context=False)
    return context, page


@pytest.mark.parametrize("mode", ["main", "reply"])
async def test_three_page_scan_keeps_three_gets_and_reduces_metadata_reads(browser, monkeypatch, record_property, mode):
    state = {"mode": mode, "gets": 0, "posts": 0}
    context, page = await open_fixture(browser, state)
    runtime = AsyncMock(side_effect=api._extract_runtime_profile)
    cookies = AsyncMock(side_effect=context.cookies)
    monkeypatch.setattr(api, "_extract_runtime_profile", runtime)
    monkeypatch.setattr(context, "cookies", cookies)
    try:
        # Reference: the former per-page metadata work, on the same signed GETs.
        for cursor in (0, 50, 100):
            if mode == "main":
                await api.fetch_comment_page(page, VIDEO, cursor, fresh=True)
            else:
                await api.fetch_reply_page(page, VIDEO, PARENT, cursor)
        assert runtime.await_count == cookies.await_count == state["gets"] == 3
        runtime.reset_mock()
        cookies.reset_mock()
        state["gets"] = 0
        started = time.monotonic()
        if mode == "main":
            result = await main_visibility.scan_main_comment_ids(page, VIDEO, [CID])
        else:
            result = await reply_visibility.scan_reply_comment_ids(page, VIDEO, PARENT, [CID])
        assert result["absent_ids"] == [CID] and result["complete"]
        assert runtime.await_count == cookies.await_count == 1
        assert state["gets"] == 3 and state["posts"] == 0
        record_property("metadata_reads_before", 6)
        record_property("metadata_reads_after", 2)
        record_property("http_gets", state["gets"])
        record_property("scan_seconds_local", round(time.monotonic() - started, 3))
    finally:
        await context.close()


@pytest.mark.parametrize("mode", ["main", "reply"])
@pytest.mark.parametrize("empty_post", [False, True])
async def test_visibility_replacement_uses_one_post_without_composer_or_dom_search(browser, monkeypatch, mode, empty_post):
    state = {"mode": mode, "empty_post": empty_post, "posts": 0, "gets": 0}
    context, page = await open_fixture(browser, state)
    account = SimpleNamespace(id=1, proxy=None)
    monkeypatch.setattr(schema._settings, "TIKTOK_NETWORK_FIRST", True)
    monkeypatch.setattr(schema._settings, "TIKTOK_NETWORK_IMPL_READY", True)
    monkeypatch.setattr(schema, "_preflight_cookie_http_gate", AsyncMock(return_value=None))
    monkeypatch.setattr(schema, "create_page_for_account", AsyncMock(return_value=(context, page)))
    monkeypatch.setattr(schema, "_ensure_account_login_ready", AsyncMock(return_value=(True, None)))
    monkeypatch.setattr(schema, "_get_cached_http_preflight_username", lambda _: "fixture")
    monkeypatch.setattr(schema, "_register_runtime_success", Mock())
    monkeypatch.setattr(schema, "record_main_comment_attempt", Mock())
    monkeypatch.setattr(schema, "record_reply_attempt", Mock())
    monkeypatch.setattr(schema, "_persist_cookies_after_success", AsyncMock())
    for helper in ("publish_comment_hybrid", "find_comment_in_list", "like_random_comment_before_action", "extract_tiktok_username"):
        monkeypatch.setattr(schema, helper, AsyncMock(side_effect=AssertionError(f"Unexpected UI helper: {helper}")))
    async def prepare(page, account, video_url, *, scope, **kwargs):
        client = await PlaywrightHttpClient.from_page(page)
        return schema._UserSchemaHttpSession(scope=scope, account_id=account.id, client=client,
                                            cookies={}, aweme_id=VIDEO, video_url=video_url)
    monkeypatch.setattr(schema, "_prepare_user_schema_http_session", prepare)
    captured = []
    if mode == "main":
        published, reason = await schema._try_main_account(None, account, URL, ["hello"],
            correlation_id="visibility_http_fixture", visibility_http_only=True, on_published=captured.append)
        assert bool(published) is (not empty_post)
    else:
        parent = schema.PublishedMain("parent", PARENT, True, False, "http", 2, "parent_author")
        result = await schema._reply_with_variant_on_all_mains(None, account, URL, [parent], "hello",
            variant_index=1, correlation_id="visibility_http_fixture", visibility_http_only=True,
            on_submitted_outcome=captured.append)
        assert bool(result.replies) is (not empty_post)
    assert state["posts"] == 1 and state["gets"] == 0
    assert len(captured) == (0 if empty_post else 1)
    assert page.is_closed()
