"""Real Chromium, local responses only: page maturity and two GET retries."""
import json
import time
from urllib.parse import parse_qs, urlparse

import pytest
from patchright.async_api import async_playwright

from executor import engagement_engine, main_visibility, reply_visibility


pytestmark = [pytest.mark.integration, pytest.mark.asyncio]
VIDEO = "7635267329407913230"
CID = "7697345157284266772"
PARENT = "7697345157284266750"
SIGNER = """() => {
    const original = window.fetch;
    window.fetch = function(input, options) {
        const url = new URL(String(input), location.href);
        url.searchParams.set('X-Bogus', 'fixture');
        url.searchParams.set('X-Gnarly', 'fixture');
        return original.call(this, url.href, options);
    };
}"""


@pytest.fixture
async def page():
    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch(headless=True)
        try:
            yield await browser.new_page()
        finally:
            await browser.close()


@pytest.mark.parametrize("path,signer_delay", [("video", 0), ("photo", 0), ("photo", 2600)])
async def test_warmup_never_fetches_from_fresh_page_even_with_early_ready_signal(page, path, signer_delay):
    await page.context.add_init_script(
        "window.byted_acrawler={sign:()=>({})};setTimeout(" + SIGNER + f",{signer_delay});"
    )
    requests = []

    async def fulfill(route):
        url = urlparse(route.request.url)
        if url.path == "/api/comment/list/":
            query = parse_qs(url.query)
            assert query.get("X-Bogus") and query.get("X-Gnarly")
            requests.append(time.monotonic())
            await route.fulfill(content_type="application/json", body='{"status_code":0,"comments":[],"has_more":0}')
        else:
            await route.fulfill(content_type="text/html", body="<html><body>Local video</body></html>", headers={
                "Set-Cookie": "ttwid=early; Path=/; Secure; SameSite=Lax",
            })

    await page.route("**/*", fulfill)
    await page.context.add_cookies([{"name": "sessionid", "value": "fixture", "domain": ".tiktok.com", "path": "/", "secure": True}])
    url = f"https://www.tiktok.com/@demo/{path}/{VIDEO}"
    started = time.monotonic()
    await page.goto(url, wait_until="commit")
    page._http_auth_verified_at = time.monotonic()
    _, valid, info = await engagement_engine.warmup_page_for_http(page, url, max_retries=1)
    assert valid and info["signer_ready"] and info["load_state"] == "domcontentloaded"
    assert info["target_page_age_ms"] >= 2190
    assert len(requests) == 1 and requests[0] - started >= 2.15
    assert page._comment_list_http_meta["retry_count"] == 0


@pytest.mark.parametrize("mode", ["main", "reply"])
@pytest.mark.parametrize("recover", [False, True])
async def test_scan_gets_three_attempts_and_real_page_activity_without_false_absence(page, mode, recover):
    await page.context.add_init_script("(" + SIGNER + ")();")
    await page.context.add_init_script("""window.fixtureActivity = 0;
        window.addEventListener('mousemove', () => { window.fixtureActivity += 1; });""")
    requests = []

    async def fulfill(route):
        url = urlparse(route.request.url)
        if url.path in {"/api/comment/list/", "/api/comment/list/reply/"}:
            assert route.request.method == "GET"
            query = parse_qs(url.query)
            assert query.get("X-Bogus") and query.get("X-Gnarly")
            if mode == "reply":
                assert query["comment_id"] == [PARENT]
            requests.append(time.monotonic())
            body = ""
            if recover and len(requests) == 3:
                body = json.dumps({"status_code": 0, "comments": [{
                    "cid": CID, "reply_id": PARENT if mode == "reply" else "0", "aweme_id": VIDEO,
                }], "has_more": 0})
            await route.fulfill(content_type="application/json", body=body)
        else:
            await route.fulfill(content_type="text/html", body='<html><body style="height:5000px">Local photo</body></html>')

    await page.route("**/*", fulfill)
    await page.goto(f"https://www.tiktok.com/@demo/photo/{VIDEO}")
    if mode == "main":
        report = await main_visibility.scan_main_comment_ids(page, VIDEO, [CID])
    else:
        report = await reply_visibility.scan_reply_comment_ids(page, VIDEO, PARENT, [CID])
    assert len(requests) == 3
    assert requests[1] - requests[0] >= 1.9 and requests[2] - requests[1] >= 1.9
    activity = await page.evaluate("() => ({moves: window.fixtureActivity, scroll: window.scrollY})", isolated_context=False)
    assert activity["moves"] == 2 and activity["scroll"] == 240
    assert page._comment_list_http_meta["attempts"] == 3
    assert page._comment_list_http_meta["retry_count"] == 2
    assert report["found_ids"] == ([CID] if recover else [])
    assert report["absent_ids"] == []
    assert report["reason"] == ("all_found" if recover else "empty_body")
