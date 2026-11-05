from urllib.parse import parse_qs, urlparse

import pytest

from executor.http.comment_discovery import fetch_comment_page
from executor.http.playwright_http_client import PlaywrightHttpClient
from executor.http.request_profile import RequestProfile
from executor.http.retry_policy import HttpErrorType, RetryPolicy
from tests.integration.test_http_browser_transport import browser_page as browser_page
from tests.integration.test_cross_file_audit_20261025_browser import test_fetch_sets_only_same_origin_referrer as check_referrer

pytestmark = [pytest.mark.integration, pytest.mark.asyncio]
VIDEO = "7635267329407913230"
URL = f"https://www.tiktok.com/@fixture/video/{VIDEO}"


@pytest.mark.parametrize("fresh", [False, True])
async def test_panel_activation_reuses_native_get_but_fresh_observer_reads_independently(browser_page, fresh):
    requests = []
    html = f"""<button data-e2e='comment-icon'>comments</button><script>
        window.clicks=0;
        const original=window.fetch;
        window.fetch=function(input,options){{const url=new URL(input,location.href);url.searchParams.set('X-Bogus','fixture');return original.call(this,url.href,options)}};
        document.querySelector('button').onclick=()=>{{window.clicks++;window.fetch('/api/comment/list/?aweme_id={VIDEO}&cursor=0&count=20')}};
    </script>"""
    async def serve(route):
        if urlparse(route.request.url).path == "/api/comment/list/":
            requests.append(route.request.url)
            await route.fulfill(content_type="application/json", body='{"status_code":0,"comments":[],"has_more":0}',
                                headers={"x-ms-token":"native-header-token"})
        else:
            await route.fulfill(content_type="text/html", body=html)
    await browser_page.route("**/*", serve)
    await browser_page.goto(URL)
    if fresh:
        assert await fetch_comment_page(browser_page, VIDEO) is not None
    payload = await fetch_comment_page(browser_page, VIDEO, fresh=fresh)
    assert payload["comments"] == []
    assert len(requests) == (2 if fresh else 1)
    assert await browser_page.evaluate("window.clicks", isolated_context=False) == 1
    assert browser_page._comment_list_http_meta.get("activation_response_reused", False) is (not fresh)
    assert next(item["value"] for item in await browser_page.context.cookies() if item["name"] == "msToken") == "native-header-token"


@pytest.mark.parametrize("source", ["https://vt.tiktok.com/abc", "https://www.google.com/", "https://www.tiktok.com.attacker.test/path"])
async def test_referrer_requires_exact_origin_not_a_string_prefix(browser_page, source):
    await check_referrer(browser_page, source)


@pytest.mark.parametrize("body,expected", [("",HttpErrorType.SUBMITTED_UNCONFIRMED), ("{}",HttpErrorType.SUBMITTED_UNCONFIRMED),
                                           ('{"data":{}}',HttpErrorType.SUBMITTED_UNCONFIRMED),
                                           (" ",HttpErrorType.SUBMITTED_UNCONFIRMED)])
async def test_empty_vs_nonempty_publish_receipts_do_not_gain_an_extra_post(browser_page, body, expected):
    dispatched = []
    async def serve(route):
        if urlparse(route.request.url).path == "/api/comment/publish/":
            dispatched.append(route.request.url)
            await route.fulfill(content_type="application/json",body=body)
        else:
            await route.fulfill(content_type="text/html",body="<html>Local publish fixture</html>")
    await browser_page.route("**/*", serve)
    await browser_page.goto(URL)
    await browser_page.evaluate("""() => {const original=window.fetch;window.fetch=(input,options)=>{
        const url=new URL(input,location.href);url.searchParams.set('X-Bogus','fixture');return original(url.href,options)}}""", isolated_context=False)
    client = await PlaywrightHttpClient.from_page(browser_page, retry_policy=RetryPolicy(max_retries=3))
    result = await client._execute_request(RequestProfile.publish_main_profile(), {"text":"fixture"})
    assert result[0] is False and result[3] is expected
    assert len(dispatched) == 1
    assert parse_qs(urlparse(dispatched[0]).query)["X-Bogus"] == ["fixture"]
