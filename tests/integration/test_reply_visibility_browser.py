"""Real Chromium reads and writes against local fixtures; no TikTok traffic."""
import json
from types import SimpleNamespace
from urllib.parse import parse_qs, urlparse

import pytest
from patchright.async_api import async_playwright

from executor.reply_visibility import scan_reply_comment_ids
from executor.main_visibility import verify_main_publications
from executor.http.playwright_http_client import PlaywrightHttpClient

pytestmark = [pytest.mark.integration, pytest.mark.asyncio]
VIDEO = "7635267329407913230"
PARENT = "7697345157284266750"
OTHER_PARENT = "7697345157284266751"
CID = "7697345157284266772"
NEW = "7697345157284266773"
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
        path = urlparse(route.request.url).path
        if path == "/api/comment/list/reply/":
            query = parse_qs(urlparse(route.request.url).query)
            assert query["comment_id"] == [PARENT] and query["item_id"] == [VIDEO]
            assert "X-Bogus" in query and "X-Gnarly" in query
            state["reads"] += 1
            state["cursors"].append(query["cursor"][0])
            body = {"status_code": 0, "comments": state["rows"], "has_more": state.get("has_more", 0), "cursor": 20}
        elif path == "/api/comment/publish/":
            assert route.request.method == "POST"
            form = parse_qs(route.request.post_data or "")
            assert form["reply_id"] == [PARENT] and form["text"] == ["hello"]
            assert form["reply_to_reply_id"] == ["0"]
            state["posts"] += 1
            state["rows"] = [{"cid": NEW, "reply_id": PARENT, "text": "hello"}]
            body = {"status_code": 0, "comment": {"cid": NEW, "reply_id": PARENT}}
        else:
            await route.fulfill(content_type="text/html", body="<html>Local fixture</html>")
            return
        await route.fulfill(content_type="application/json", body=json.dumps(body))
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
    }""", isolated_context=False)
    return context, page


async def test_reply_replaced_in_same_branch_only_after_two_missing_reads(browser):
    state = {"rows": [], "reads": 0, "posts": 0, "cursors": []}
    observer_id = 100
    async def observe(slots, *_):
        nonlocal observer_id
        observer_id += 1
        context, page = await open_fixture(browser, state)
        try:
            return {**await scan_reply_comment_ids(page, VIDEO, PARENT, [s.comment_id for s in slots]), "observer_id": observer_id}
        finally:
            await context.close()
    async def replace(slot, _):
        assert state["reads"] == 2
        context, page = await open_fixture(browser, state)
        try:
            client = await PlaywrightHttpClient.from_page(page)
            ok, cid, error, _, _ = await client.reply_comment(VIDEO, PARENT, slot.text, video_url=URL)
            assert ok, error
            return SimpleNamespace(comment_id=cid, account_id=20, text="hello", variant_index=1, public_confirmed=False), None
        finally:
            await context.close()
    slots = [SimpleNamespace(comment_id=CID, account_id=1, text="hello", variant_index=1, public_confirmed=False)]
    report = await verify_main_publications(slots, observe=observe, replace=replace, initial_delay=0, recheck_delay=0,
                                             log_label="REPLY_VISIBILITY")
    assert state["reads"] == 3 and state["posts"] == 1
    assert slots[0].comment_id == NEW and slots[0].public_confirmed
    assert report["status"] == "confirmed" and report["history"][0]["comment_id"] == CID


@pytest.mark.parametrize("case", ["wrong_parent", "page_limit"])
async def test_wrong_branch_or_incomplete_pagination_never_permits_repost(browser, case):
    state = {"rows": [{"cid": CID, "reply_id": OTHER_PARENT}], "reads": 0, "posts": 0, "cursors": []}
    if case == "page_limit":
        state.update(rows=[], has_more=1)
    context, page = await open_fixture(browser, state)
    try:
        evidence = await scan_reply_comment_ids(page, VIDEO, PARENT, [CID], max_pages=1)
        assert not evidence["found_ids"] and not evidence["absent_ids"] and not evidence["complete"]
        assert state["posts"] == 0
    finally:
        await context.close()
