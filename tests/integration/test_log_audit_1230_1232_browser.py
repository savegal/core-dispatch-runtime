"""Use local Chromium traffic to distinguish request and response signatures."""
import json
from urllib.parse import parse_qs, urlparse

import pytest
from patchright.async_api import async_playwright

from executor import main_visibility, reply_visibility, user_schema as schema
from executor.http.playwright_http_client import PlaywrightHttpClient
from executor.http.retry_policy import RetryPolicy


pytestmark = [pytest.mark.integration, pytest.mark.asyncio]
VIDEO = "7635267329407913230"
URL = f"https://www.tiktok.com/@demo/video/{VIDEO}"
CID = "7697345157284266772"
PARENT = "7697345157284266750"


@pytest.fixture
async def page():
    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch(headless=True)
        try:
            yield await browser.new_page()
        finally:
            await browser.close()


async def fixture(page, mode, *, strip_response_url=False, explicit_sdk=False):
    requests = []

    async def serve(route):
        parsed = urlparse(route.request.url)
        if parsed.path.startswith("/api/comment/"):
            query = parse_qs(parsed.query)
            signed = bool(query.get("X-Bogus") and query.get("X-Gnarly"))
            requests.append((route.request.method, signed))
            comment = {"cid": CID, "reply_id": PARENT if mode == "reply" else "0", "aweme_id": VIDEO,
                       "user": {"uid": "123456789", "unique_id": "api_author"}}
            payload = {"status_code": 0, "comment": comment} if route.request.method == "POST" else {
                "status_code": 0, "comments": [comment], "has_more": 0,
            }
            await route.fulfill(content_type="application/json", body=json.dumps(payload) if signed else "")
        else:
            await route.fulfill(content_type="text/html", body="<html><body>Local</body></html>")

    await page.route("**/*", serve)
    await page.goto(URL)
    if explicit_sdk:
        await page.evaluate("""() => {
            window.byted_acrawler = {sign: () => ({'X-Bogus': 'fixture', 'X-Gnarly': 'fixture'})};
        }""", isolated_context=False)
    else:
        await page.evaluate("""strip => {
            const original = window.fetch;
            window.fetch = async function(input, options) {
                const url = new URL(String(input), location.href);
                url.searchParams.set('X-Bogus', 'fixture');
                url.searchParams.set('X-Gnarly', 'fixture');
                const response = await original.call(this, url.href, options);
                if (strip) Object.defineProperty(response, 'url', {value: url.origin + url.pathname});
                return response;
            };
        }""", strip_response_url, isolated_context=False)
    return requests


@pytest.mark.parametrize("mode", ["main", "reply"])
async def test_stripped_response_url_does_not_invalidate_signed_get(page, mode):
    requests = await fixture(page, mode, strip_response_url=True)
    report = (await main_visibility.scan_main_comment_ids(page, VIDEO, [CID]) if mode == "main"
              else await reply_visibility.scan_reply_comment_ids(page, VIDEO, PARENT, [CID]))
    assert report["found_ids"] == [CID]
    assert requests == [("GET", True)]
    meta = page._comment_list_http_meta
    assert meta["attempts"] == 1 and meta["security_params_source"] == "request_event"
    assert meta["request_security_params"]["X-Bogus"] and not meta["response_security_params"]["X-Bogus"]


@pytest.mark.parametrize("mode", ["main", "reply"])
async def test_sdk_without_native_interceptor_cannot_presign_get_retries(page, mode):
    requests = await fixture(page, mode, explicit_sdk=True)
    report = (await main_visibility.scan_main_comment_ids(page, VIDEO, [CID]) if mode == "main"
              else await reply_visibility.scan_reply_comment_ids(page, VIDEO, PARENT, [CID]))
    assert report["found_ids"] == report["absent_ids"] == [] and not report["complete"]
    assert requests == [("GET", False)] * 3
    assert page._comment_list_http_meta["reason"] == "empty_body"
    assert "runtime_signature_mode" not in page._comment_list_http_meta


@pytest.mark.parametrize("mode", ["main", "reply"])
async def test_publish_retains_wire_signatures_and_author_uid_when_response_url_is_stripped(page, mode, monkeypatch):
    from types import SimpleNamespace
    requests = await fixture(page, mode, strip_response_url=True)
    client = await PlaywrightHttpClient.from_page(page, retry_policy=RetryPolicy(max_retries=0))
    if mode == "main":
        success, cid, error, _ = await client.publish_main_comment(VIDEO, "hello")
        assert success and cid == CID, error
    else:
        monkeypatch.setattr(schema._settings, "TIKTOK_NETWORK_FIRST", True)
        monkeypatch.setattr(schema._settings, "TIKTOK_NETWORK_IMPL_READY", True)
        session = schema._UserSchemaHttpSession(schema.REPLY_SCOPE, 1, client, {}, VIDEO, URL)
        result = await schema._publish_reply_http_first_if_eligible(session, "hello", parent_comment_id=PARENT, correlation_id="fixture")
        assert result.status == "success" and result.comment_id == CID
    assert requests == [("POST", True)]
    assert client.last_request_meta["observed_security_params"]["X-Bogus"]
    assert client.last_request_meta["security_params_source"] == "request_event"
    assert not client.last_request_meta["response_security_params"]["X-Bogus"]
    account = SimpleNamespace(id=1)
    identity = schema._publication_author_identity(account, SimpleNamespace(client=client))
    assert identity == {"author_username": "api_author", "author_uid": "123456789"}
    assert schema._get_cached_http_preflight_user_id(account) == "123456789"
