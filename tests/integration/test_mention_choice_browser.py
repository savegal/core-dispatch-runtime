"""Mention policy on real Chromium input/transport; every URL is served locally."""
import json
from urllib.parse import parse_qs, urlparse

import pytest

from executor.http import mentions
from executor.http.playwright_http_client import PlaywrightHttpClient
from executor.http.retry_policy import RetryPolicy
from services.mention_policy import set_task_mention_mode, reset_task_mention_mode
from tests.integration.test_http_browser_transport import browser_page as browser_page


pytestmark = [pytest.mark.integration, pytest.mark.asyncio]
URL = "https://www.tiktok.com/@fixture/video/7635267329407913230"
CID = "7123456789012345678"
PARENT = "7123456789099999999"
UID = "7123456789012345000"


async def setup(page, *, lookup="api", receipt="echo"):
    requests, posts = [], []
    user = {"id": UID, "secUid": "sec_fixture", "uniqueId": "person"}

    async def serve(route):
        path = urlparse(route.request.url).path
        requests.append(path)
        if path == "/api/user/detail/":
            if lookup == "redirect":
                await route.fulfill(status=302, headers={"location": "/login"})
            elif lookup == "ssr":
                await route.fulfill(status=404, body="private endpoint not available")
            elif lookup == "absent":
                await route.fulfill(content_type="application/json", body='{"statusCode":10202}')
            else:
                await route.fulfill(content_type="application/json", body=json.dumps({"statusCode": 0, "userInfo": {"user": user}}))
        elif path.lower() == "/@person":
            if lookup == "redirect":
                await route.fulfill(status=302, headers={"location": "/login"})
            else:
                data = {"__DEFAULT_SCOPE__": {"webapp.user-detail": {"statusCode": 0, "userInfo": {"user": user}}}}
                await route.fulfill(content_type="text/html", body='<script id="__UNIVERSAL_DATA_FOR_REHYDRATION__">' + json.dumps(data) + '</script>')
        elif path == "/api/comment/publish/":
            values = parse_qs(route.request.post_data or "", keep_blank_values=True)
            posts.append({"url": route.request.url, "values": values})
            comment = {"cid": CID, "reply_id": PARENT, "text": values["text"][0]}
            if receipt != "omitted":
                comment["text_extra"] = json.loads(values["text_extra"][0]) if receipt == "echo" else []
            await route.fulfill(content_type="application/json", body=json.dumps({"status_code": 0, "comment": comment}))
        else:
            await route.fulfill(content_type="text/html", body="<html>Local fixture</html>")

    await page.route("**/*", serve)
    await page.goto(URL)
    await page.evaluate("""() => {
        const original = window.fetch;
        window.fetch = (input, options) => {
            const url = new URL(input, location.href);
            if (url.pathname.startsWith('/api/')) url.searchParams.set('X-Bogus', 'local-fixture');
            return original(url.href, options);
        };
    }""", isolated_context=False)
    await page.context.add_cookies([{"name": "tt_csrf_token", "value": "csrf", "domain": ".tiktok.com", "path": "/"}])
    return requests, posts


@pytest.mark.parametrize("kind", ["main", "reply"])
@pytest.mark.parametrize("mode", ["plain", "attempt"])
async def test_saved_order_choice_controls_actual_post_entities(browser_page, mode, kind):
    requests, posts = await setup(browser_page)
    client = await PlaywrightHttpClient.from_page(browser_page, retry_policy=RetryPolicy(max_retries=0))
    token = set_task_mention_mode({"mention_mode": mode})
    text = "🔥 Привет @Person!"
    try:
        if kind == "main":
            result = await client.publish_main_comment("7635267329407913230", text)
        else:
            result = await client.reply_comment("7635267329407913230", PARENT, text)
    finally:
        reset_task_mention_mode(token)
    assert result[0], result
    assert len(posts) == 1
    payload = posts[0]["values"]
    assert "mention_mode" not in payload and "mention_mode" not in parse_qs(urlparse(posts[0]["url"]).query)
    if mode == "plain":
        assert payload["text"] == [text]
        assert json.loads(payload["text_extra"][0]) == []
        assert "/api/user/detail/" not in requests
    else:
        assert payload["text"] == [text.replace("@Person", "@person")]
        extras = json.loads(payload["text_extra"][0])
        assert extras == [{"start": 10, "end": 17, "type": 0, "user_id": UID,
                           "sec_uid": "sec_fixture", "hashtag_name": ""}]
        assert client.last_request_meta["mentions"]["confirmation"] == "retained"


async def test_private_api_404_falls_back_to_profile_before_negative_cache(browser_page):
    requests, posts = await setup(browser_page, lookup="ssr")
    client = await PlaywrightHttpClient.from_page(browser_page, retry_policy=RetryPolicy(max_retries=0))
    result = await client.publish_main_comment("7635267329407913230", "Привет @person", mention_mode="attempt")
    assert result[0], result
    assert "/api/user/detail/" in requests and "/@person" in [p.lower() for p in requests]
    assert len(posts) == 1
    assert mentions._cache_get("person")[1]["user_id"] == UID


async def test_login_redirect_is_inconclusive_and_cannot_negative_cache_or_post(browser_page):
    _, posts = await setup(browser_page, lookup="redirect")
    client = await PlaywrightHttpClient.from_page(browser_page, retry_policy=RetryPolicy(max_retries=0))
    result = await client.publish_main_comment("7635267329407913230", "Привет @person", mention_mode="attempt")
    assert not result[0] and "unresolved" in result[2]
    assert not posts and mentions._cache_get("person") == (False, None)


@pytest.mark.parametrize("receipt,confirmation", [("omitted", "unknown"), ("empty", "not_retained")])
async def test_missing_mention_receipt_never_retries_a_published_comment(browser_page, receipt, confirmation):
    _, posts = await setup(browser_page, receipt=receipt)
    client = await PlaywrightHttpClient.from_page(browser_page, retry_policy=RetryPolicy(max_retries=2))
    result = await client.publish_main_comment("7635267329407913230", "Привет @person", mention_mode="attempt")
    assert result[0] and len(posts) == 1
    assert client.last_request_meta["mentions"]["confirmation"] == confirmation
