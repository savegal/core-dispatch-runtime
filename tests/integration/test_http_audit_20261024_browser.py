import json
import time
from types import SimpleNamespace
from urllib.parse import parse_qs, urlparse

import pytest

from executor import engagement_engine
from executor.http import comment_discovery as discovery
from executor.http.playwright_http_client import PlaywrightHttpClient
from executor.http.request_profile import RequestProfile
from executor.http.retry_policy import HttpErrorType, RetryPolicy
from executor.http.signature_runtime import wait_for_runtime_signer_ready
from tests.integration.test_http_browser_transport import browser_page as browser_page

pytestmark = [pytest.mark.integration, pytest.mark.asyncio]
VIDEO = "7635267329407913230"
URL = f"https://www.tiktok.com/@fixture/video/{VIDEO}"


async def test_first_get_activates_comments_and_header_token_reaches_next_cursor(browser_page):
    requests = []
    async def serve(route):
        if urlparse(route.request.url).path == "/api/comment/list/":
            requests.append(parse_qs(urlparse(route.request.url).query))
            await route.fulfill(content_type="application/json", body='{"status_code":0,"comments":[],"has_more":0}',
                                headers={"x-ms-token": "header-token"})
        else:
            await route.fulfill(content_type="text/html", body="<button data-e2e='comment-icon'>comments</button>")
    await browser_page.route("**/*", serve)
    browser_page.context._pool_account_obj = SimpleNamespace(
        id=-241024, device_seed="f" * 64, cookies=json.dumps({"sessionid": "fixture", "s_v_web_id": "verify_native_fixture"}),
        proxy=None, country_code=None,
    )
    browser_page.context._account_proxy_in_use = False
    await browser_page.context.add_cookies([
        {"name": "msToken", "value": "old-token", "domain": ".tiktok.com", "path": "/"},
        {"name": "s_v_web_id", "value": "verify_native_fixture", "domain": ".tiktok.com", "path": "/"},
    ])
    await browser_page.goto(URL)
    await browser_page.evaluate("""() => {
        window.byted_acrawler = {sign: () => ({})};
        window.commentReady = false; window.clicks = 0;
        document.querySelector('button').onclick = () => {
            window.clicks++; setTimeout(() => { window.commentReady = true; }, 30);
        };
        const nativeFetch = fetch;
        window.fetch = function(input, options) {
            const url = new URL(input, location.href);
            url.searchParams.set('X-Bogus', 'fixture');
            url.searchParams.set('activationReady', String(window.commentReady));
            return nativeFetch.call(this, url.href, options);
        };
    }""", isolated_context=False)
    assert await wait_for_runtime_signer_ready(browser_page, timeout_ms=6000)
    assert browser_page._http_signer_state == {"sdk_present": True, "signed_dispatch_seen": False}
    async with discovery.comment_read_batch(browser_page):
        assert await discovery.fetch_comment_page(browser_page, VIDEO) is not None
        assert await discovery.fetch_comment_page(browser_page, VIDEO, cursor=20, fresh=True) is not None
    assert len(requests) == 2
    assert [query["msToken"] for query in requests] == [["old-token"], ["header-token"]]
    assert all(query["activationReady"] == ["true"] and "X-Gnarly" not in query for query in requests)
    assert all(query["device_id"][0].isdigit() and len(query["device_id"][0]) == 19 for query in requests)
    assert all(query["verifyFp"] == ["verify_native_fixture"] for query in requests)
    assert await browser_page.evaluate("window.clicks", isolated_context=False) == 1


@pytest.mark.parametrize("signature", ["X-Bogus", "a_bogus"])
async def test_empty_post_with_one_signature_is_never_duplicated(browser_page, signature):
    posts = []
    async def serve(route):
        if route.request.method == "POST":
            posts.append(route.request.url)
            await route.fulfill(content_type="application/json", body="")
        else:
            await route.fulfill(content_type="text/html", body="<html>local</html>")
    await browser_page.route("**/*", serve)
    await browser_page.goto(URL)
    await browser_page.evaluate("""key => {
        const nativeFetch=fetch;
        window.fetch=function(input,options) {
            const url=new URL(input,location.href); url.searchParams.set(key,'fixture');
            return nativeFetch.call(this,url.href,options);
        };
    }""", signature, isolated_context=False)
    client = PlaywrightHttpClient(browser_page.context.request, page=browser_page, retry_policy=RetryPolicy(max_retries=3))
    success, _, _, error = await client._execute_request(RequestProfile.publish_main_profile(), {"aweme_id": VIDEO, "text": "fixture"})
    assert not success and error == HttpErrorType.FORBIDDEN and len(posts) == 1


async def test_dom_and_callable_sdk_skip_mandatory_warmup_age(browser_page):
    async def serve(route):
        if urlparse(route.request.url).path == "/api/comment/list/":
            await route.fulfill(content_type="application/json", body='{"status_code":0,"comments":[]}')
        else:
            await route.fulfill(content_type="text/html", body="""<html><script>
                window.byted_acrawler = {frontierSign: () => ({})};
                const nativeFetch = window.fetch;
                window.fetch = (input, options) => {
                    const url = new URL(input, location.href);
                    url.searchParams.set('X-Bogus', 'fixture');
                    return nativeFetch(url.href, options);
                };
            </script></html>""")
    await browser_page.route("**/*", serve)
    await browser_page.context.add_cookies([
        {"name": key, "value": "fixture", "domain": ".tiktok.com", "path": "/"}
        for key in ("sessionid", "ttwid", "tt_csrf_token", "msToken")
    ])
    await browser_page.goto(URL)
    browser_page._http_auth_verified_at = time.monotonic()
    start = time.monotonic()
    _, valid, info = await engagement_engine.warmup_page_for_http(browser_page, URL, max_retries=1)
    assert valid and info["signer_ready"] and info["sdk_present"]
    assert info["signed_dispatch_seen"] and info["minimum_age_wait_ms"] == 0
    assert time.monotonic() - start < 2.0
