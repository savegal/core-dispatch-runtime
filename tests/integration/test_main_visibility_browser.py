"""Visibility and replacement through real Chromium; all traffic stays local."""
import json
from types import SimpleNamespace
from urllib.parse import parse_qs, urlparse

import pytest
from patchright.async_api import async_playwright

from executor.main_visibility import scan_main_comment_ids, verify_main_publications
from executor.http.playwright_http_client import PlaywrightHttpClient

pytestmark = [pytest.mark.integration, pytest.mark.asyncio]
VIDEO = "7635267329407913230"
CID = "7697345157284266772"
NEW = "7697345157284266773"
URL = f"https://www.tiktok.com/@fixture/video/{VIDEO}"


@pytest.fixture
async def local_browser():
    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch(headless=True)
        try:
            yield browser
        finally:
            await browser.close()


async def fixture_page(browser, state):
    context = await browser.new_context(service_workers="block")
    async def route_request(route):
        path = urlparse(route.request.url).path
        if path == "/api/comment/publish/":
            assert route.request.method == "POST"
            form = parse_qs(route.request.post_data or "")
            assert form["text"] == ["hello"]
            state["posts"] += 1
            state["comments"] = [{"cid": NEW, "text": "hello", "reply_id": "0"}]
            body = {"status_code": 0, "comment": {"cid": NEW}}
        elif path == "/api/comment/list/":
            state["reads"] += 1
            query = parse_qs(urlparse(route.request.url).query)
            assert query["aweme_id"] == [VIDEO]
            body = {"status_code": 0, "comments": state["comments"], "has_more": state.get("has_more", 0), "cursor": 50}
        else:
            await route.fulfill(content_type="text/html", body="<html>Local fixture</html>")
            return
        await route.fulfill(content_type="application/json", body=json.dumps(body))
    await context.route("**/*", route_request)
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
    }""", isolated_context=False)
    return context, page


async def test_two_missing_reads_then_one_replacement_is_publicly_confirmed(local_browser):
    state = {"comments": [], "reads": 0, "posts": 0}
    observer_id = 100
    async def observe(mains, *_):
        nonlocal observer_id
        observer_id += 1
        context, page = await fixture_page(local_browser, state)
        try:
            result = await scan_main_comment_ids(page, VIDEO, [m.comment_id for m in mains])
            return {**result, "observer_id": observer_id}
        finally:
            await context.close()
    async def replace(main, _):
        # The write starts only after both complete independent reads.
        assert state["reads"] == 2
        context, page = await fixture_page(local_browser, state)
        try:
            client = await PlaywrightHttpClient.from_page(page)
            ok, cid, error, _ = await client.publish_main_comment(VIDEO, main.text, video_url=URL)
            assert ok, error
            return SimpleNamespace(comment_id=cid, text=main.text, account_id=20,
                                   variant_index=1, public_confirmed=False), None
        finally:
            await context.close()
    mains = [SimpleNamespace(comment_id=CID, text="hello", account_id=1, variant_index=1, public_confirmed=False)]
    report = await verify_main_publications(mains, observe=observe, replace=replace,
                                             initial_delay=0, recheck_delay=0)
    assert state["posts"] == 1 and state["reads"] == 3
    assert report["confirmed"] == 1 and report["replacements"] == 1
    assert mains[0].comment_id == NEW and mains[0].public_confirmed
    assert report["history"][0]["comment_id"] == CID


async def test_paginated_incomplete_list_never_authorizes_replacement(local_browser):
    state = {"comments": [{"cid": NEW, "text": "hello"}], "has_more": 1, "reads": 0, "posts": 0}
    context, page = await fixture_page(local_browser, state)
    try:
        result = await scan_main_comment_ids(page, VIDEO, [CID], max_pages=1)
        assert result["reason"] == "page_limit" and result["absent_ids"] == []
        assert state["posts"] == 0
    finally:
        await context.close()
