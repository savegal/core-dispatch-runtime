"""Exercise window.fetch in real Chromium; all traffic is fulfilled locally."""
from unittest.mock import AsyncMock
import asyncio
from urllib.parse import parse_qs, urlparse
from types import SimpleNamespace
import json
import time

import pytest
from patchright.async_api import async_playwright, Error

from executor.http.playwright_http_client import PlaywrightHttpClient, _PAGE_FETCH_JS
from executor.http.request_profile import RequestProfile
from executor.http.retry_policy import RetryPolicy
from executor.tiktok_client import parse_cookies


pytestmark = [pytest.mark.integration, pytest.mark.asyncio]


_COMMENT_INTERCEPTOR_JS = """() => {
    const original=window.fetch;
    window.fetch=function fetch(input, options) {
        const url=new URL(input instanceof Request ? input.url : String(input), location.href);
        if (url.pathname==='/api/comment/list/') {
            url.searchParams.set('X-Bogus','local-bogus');url.searchParams.set('X-Gnarly','local-gnarly');
            return original.call(this,url.href,options);
        }
        return original.apply(this,arguments);
    };
}"""


@pytest.fixture
async def browser_page():
    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch(headless=True)
        try:
            yield await browser.new_page()
        finally:
            await browser.close()


async def test_transport_exception_before_fetch_does_not_consume_publish_attempt(browser_page, monkeypatch):
    posts = []
    async def serve(route):
        if route.request.method == "POST":
            posts.append(route.request.url)
        await route.fulfill(content_type="text/html", body="<html>Local</html>")
    await browser_page.route("**/*", serve)
    await browser_page.goto("https://www.tiktok.com/@fixture/video/7635267329407913230")
    client = await PlaywrightHttpClient.from_page(browser_page, retry_policy=RetryPolicy(max_retries=0))
    original = browser_page.evaluate
    async def evaluate(script, *args, **kwargs):
        if script == _PAGE_FETCH_JS:
            return await original("() => { throw new Error('Before window.fetch'); }", isolated_context=False)
        return await original(script, *args, **kwargs)
    monkeypatch.setattr(browser_page, "evaluate", evaluate)
    success, *_ = await client.publish_main_comment("7635267329407913230", "fixture")
    assert not success and not posts
    assert client.last_request_meta["request_attempted"] is False


async def test_cancel_after_real_post_preserves_dispatch_evidence(browser_page):
    started, release = asyncio.Event(), asyncio.Event()
    async def serve(route):
        if route.request.method == "POST":
            started.set()
            await release.wait()
            await route.fulfill(content_type="application/json", body='{"status_code":0}')
        else:
            await route.fulfill(content_type="text/html", body="<html>Local</html>")
    await browser_page.route("**/*", serve)
    await browser_page.goto("https://www.tiktok.com/@fixture/video/7635267329407913230")
    client = await PlaywrightHttpClient.from_page(browser_page, retry_policy=RetryPolicy(max_retries=0))
    request = asyncio.create_task(client.publish_main_comment("7635267329407913230", "fixture"))
    try:
        await asyncio.wait_for(started.wait(), timeout=5)
        request.cancel()
        with pytest.raises(asyncio.CancelledError):
            await request
        assert client.last_request_meta["request_attempted"] is True
        assert browser_page._publication_attempted is True
    finally:
        release.set()
        if not request.done():
            request.cancel()


async def test_warmup_waits_for_subresource_set_cookie_after_commit(browser_page, monkeypatch):
    from executor import engagement_engine

    target = "https://www.tiktok.com/@demo/video/7635267329407913230"
    requests = []

    async def serve(route):
        path = urlparse(route.request.url).path
        if path == "/linkage-cookie":
            await asyncio.sleep(0.15)
            assert not requests, "Comment GET was dispatched before the navigation's ttwid cookie"
            await route.fulfill(status=200, content_type="text/plain", body="ok", headers={
                "Set-Cookie": "ttwid=fresh-wid; Domain=.tiktok.com; Path=/; Secure; HttpOnly; SameSite=Lax",
            })
        elif path == "/api/comment/list/":
            headers = await route.request.all_headers()
            requests.append(headers)
            body = '{"status_code":0,"comments":[],"has_more":0}' if "ttwid=" in headers.get("cookie", "") else ""
            await route.fulfill(status=200, content_type="application/json", body=body, headers={
                "Set-Cookie": "tt_csrf_token=fresh-csrf; Domain=.tiktok.com; Path=/; Secure; SameSite=Lax",
            })
        else:
            await route.fulfill(status=200, content_type="text/html", body="""<html><body><script>
                fetch('/linkage-cookie');
            </script></body></html>""")

    await browser_page.route("**/*", serve)
    await browser_page.context.add_cookies([{
        "name": "sessionid", "value": "local-session", "domain": ".tiktok.com", "path": "/", "secure": True,
    }])
    await browser_page.context.add_init_script(f"({_COMMENT_INTERCEPTOR_JS})();")
    await browser_page.goto(target, wait_until="commit")
    # A reload here would interrupt the delayed response we're testing.
    monkeypatch.setattr(browser_page, "reload", AsyncMock(side_effect=AssertionError("Unexpected reload")))
    warmed, valid, info = await engagement_engine.warmup_page_for_http(browser_page, target)

    assert valid and info["attempts"] == 1 and info["reused_page"]
    assert info["comment_api_ready"]
    assert warmed["ttwid"] == "fresh-wid" and warmed["tt_csrf_token"] == "fresh-csrf"
    assert len(requests) == 1
    assert "ttwid=fresh-wid" in requests[0]["cookie"]


@pytest.mark.parametrize("reply", [False, True])
async def test_publish_fetch_runs_page_interceptor_with_form_body_and_cookies(browser_page, reply):
    captured = []

    async def serve_local_response(route):
        request = route.request
        if request.method == "POST":
            captured.append({"url": request.url, "body": request.post_data, "headers": await request.all_headers()})
            await route.fulfill(status=200, content_type="application/json", body='{"status_code":0,"comment":{"cid":"7641761192486195010"}}')
        else:
            await route.fulfill(status=200, content_type="text/html", body="<html><body>Local test</body></html>")

    await browser_page.route("**/*", serve_local_response)
    await browser_page.goto("https://www.tiktok.com/@demo/video/123")
    await browser_page.context.add_cookies([
        {"name": "sessionid", "value": "local_session", "domain": ".tiktok.com", "path": "/", "secure": True, "httpOnly": True},
        {"name": "msToken", "value": "fresh-sdk-token", "domain": ".tiktok.com", "path": "/", "secure": True},
    ])
    # Install in the page's main world, as TikTok's SDK does.
    await browser_page.evaluate("""() => {
        const originalFetch = window.fetch;
        window.fetch = (url, options) => {
            if (typeof options.body !== 'string') throw new Error('Expected form-encoded string');
            if (options.headers['content-type'] !== 'application/x-www-form-urlencoded; charset=UTF-8') throw new Error('Expected form content type');
            if (options.credentials !== 'include') throw new Error('Expected credentials');
            const signed = new URL(url);
            const msToken = document.cookie.split('; ').find(row => row.startsWith('msToken=')).split('=').slice(1).join('=');
            if (signed.searchParams.get('msToken') !== msToken) throw new Error('Expected current cookie msToken in URL');
            if (Object.keys(options.headers).some(key => key.toLowerCase().startsWith('x-secsdk-'))) throw new Error('Caller supplied SDK headers');
            signed.searchParams.set('X-Bogus', options.body.toString());
            options.headers['x-secsdk-csrf-token'] = 'generated-by-sdk';
            return originalFetch.call(window, signed.toString(), options);
        };
    }""", isolated_context=False)
    context = AsyncMock()
    client = PlaywrightHttpClient(context, retry_policy=RetryPolicy(max_retries=0), page=browser_page)
    text = "Привет + & 😀"
    params = {"aweme_id": "123", "text": text}
    if reply:
        params.update(reply_id="456", reply_to_reply_id="789")
    profile = RequestProfile.publish_reply_profile() if reply else RequestProfile.publish_main_profile()
    profile.static_headers.update({"X-SecSDK-CSRF-Request": "1", "X-SecSDK-CSRF-Version": "stale", "X-SecSDK-CSRF-Token": "wrong-token"})

    success, response, error, _ = await client._execute_request(profile, params, cookies={"tt_csrf_token": "local_csrf", "msToken": "stale-db-token", "X-Bogus": "stale"})

    assert success is True, error
    assert response["comment"]["cid"] == "7641761192486195010"
    context.fetch.assert_not_awaited()
    assert len(captured) == 1
    request = captured[0]
    body = parse_qs(request["body"])
    query = parse_qs(urlparse(request["url"]).query)
    assert body["text"] == [text]
    assert "text" not in query
    assert query["X-Bogus"] == [request["body"]]
    assert "sessionid=local_session" in request["headers"]["cookie"]
    assert query["msToken"] == ["fresh-sdk-token"]
    assert request["headers"]["tt-csrf-token"] == "local_csrf"
    assert request["headers"]["x-secsdk-csrf-token"] == "generated-by-sdk"
    assert "x-secsdk-csrf-request" not in request["headers"]
    assert "x-secsdk-csrf-version" not in request["headers"]
    assert request["headers"]["content-type"].startswith("application/x-www-form-urlencoded")
    if reply:
        assert body["reply_id"] == ["456"]
        assert body["reply_to_reply_id"] == ["789"]


async def test_page_fetch_timeout_aborts_in_flight_request(browser_page):
    await browser_page.evaluate("""() => {
        window.fetch = (_url, options) => new Promise((_resolve, reject) => {
            options.signal.addEventListener('abort', () => {
                document.documentElement.dataset.aborted = 'true';
                reject(new DOMException('Aborted', 'AbortError'));
            });
        });
    }""", isolated_context=False)
    with pytest.raises(Error, match="Request timeout during window.fetch"):
        await browser_page.evaluate(_PAGE_FETCH_JS, {
            "url": "https://www.tiktok.com/api/comment/publish/", "method": "POST",
            "headers": {}, "form_data": {"text": "test"}, "timeout_ms": 25,
        }, isolated_context=False)
    assert await browser_page.locator("html").get_attribute("data-aborted") == "true"


async def test_browser_cookie_updates_are_used_on_retry_and_next_publish(browser_page, monkeypatch):
    captured = []

    def cookies(version):
        return [{"name": name, "value": f"{name}-{version}", "domain": ".tiktok.com", "path": "/", "secure": True}
                for name in ("msToken", "WebIdLastTime", "tt_csrf_token")]

    async def local_route(route):
        if route.request.method != "POST":
            await route.fulfill(content_type="text/html", body="<html>Local cookie test</html>")
            return
        captured.append({"url": route.request.url, "headers": await route.request.all_headers(), "body": route.request.post_data})
        if len(captured) == 1:
            await browser_page.context.add_cookies(cookies(2)[:2])
            await route.fulfill(content_type="application/json", body="", headers={
                "set-cookie": "tt_csrf_token=tt_csrf_token-2; Domain=.tiktok.com; Path=/; Secure",
            })
        else:
            await route.fulfill(content_type="application/json", body='{"status_code":0,"comment":{"cid":"7641761192486194454"}}')

    await browser_page.route("**/*", local_route)
    await browser_page.goto("https://www.tiktok.com/@demo/video/123")
    await browser_page.context.add_cookies(cookies(1))
    await browser_page.evaluate("""() => {
        const original = window.fetch;
        window.fetch = (url, options) => {
            const signed = new URL(url);
            if (Object.keys(options.headers).some(key => key.toLowerCase().startsWith('x-secsdk-'))) throw new Error('Caller supplied SDK headers');
            const readCookie = name => document.cookie.split('; ').find(row => row.startsWith(name + '=')).split('=').slice(1).join('=');
            if (signed.searchParams.get('msToken') !== readCookie('msToken')) throw new Error('Expected current cookie msToken in URL');
            options.headers['x-secsdk-csrf-token'] = 'SDK-' + readCookie('tt_csrf_token');
            return original.call(window, signed.toString(), options);
        };
    }""", isolated_context=False)
    monkeypatch.setattr("executor.http.playwright_http_client.asyncio.sleep", AsyncMock())
    context = AsyncMock()
    client = PlaywrightHttpClient(context, page=browser_page, retry_policy=RetryPolicy(max_retries=1))
    old = {"msToken": "db-token", "WebIdLastTime": "db-time", "tt_csrf_token": "db-csrf"}

    result = await client.publish_main_comment("123", "first", cookies=old)
    assert result[0], result[2]
    await browser_page.context.add_cookies(cookies(3))
    result = await client.publish_main_comment("123", "second", cookies=old)
    assert result[0], result[2]

    assert len(captured) == 3
    for version, request in enumerate(captured, start=1):
        query = parse_qs(urlparse(request["url"]).query)
        assert query["msToken"] == [f"msToken-{version}"]
        assert query["WebIdLastTime"] == [f"WebIdLastTime-{version}"]
        assert request["headers"]["x-secsdk-csrf-token"] == f"SDK-tt_csrf_token-{version}"
        assert request["headers"]["tt-csrf-token"] == f"tt_csrf_token-{version}"
        assert f"tt_csrf_token=tt_csrf_token-{version}" in request["headers"]["cookie"]
    assert [parse_qs(request["body"])["text"] for request in captured] == [["first"], ["first"], ["second"]]
    context.fetch.assert_not_awaited()


@pytest.mark.parametrize("guard_domain", [".tiktok.com", "tiktok.com", "www.tiktok.com"])
async def test_restored_session_is_sent_by_browser_fetch_without_cookie_header(browser_page, guard_domain):
    captured = []

    async def local_route(route):
        if route.request.method == "POST":
            captured.append(await route.request.all_headers())
            await route.fulfill(content_type="application/json", body='{"status_code":0,"comment":{"cid":"7641761192486195011"}}')
        else:
            await route.fulfill(content_type="text/html", body="<html><body>Local</body></html>")

    records = [
        {"name": "sid_guard", "value": f'{"a" * 32}%7C{int(time.time())}%7C86400', "domain": guard_domain, "path": "/passport", "hostOnly": True},
        {"name": "msToken", "value": "web-token", "domain": "www.tiktok.com", "hostOnly": True},
        {"name": "msToken", "value": "ads-token", "domain": "ads.tiktok.com"},
    ]
    await browser_page.route("**/*", local_route)
    await browser_page.context.add_cookies(parse_cookies(json.dumps(records)))
    await browser_page.goto("https://www.tiktok.com/@demo/video/123")
    client = PlaywrightHttpClient(AsyncMock(), page=browser_page, retry_policy=RetryPolicy(max_retries=0))

    success, _, error, _ = await client._execute_request(RequestProfile.publish_main_profile(), {"aweme_id": "123", "text": "test"})

    assert success, error
    assert len(captured) == 1
    cookie_header = captured[0]["cookie"]
    for name in ("sessionid", "sid_tt", "sessionid_ss"):
        assert f'{name}={"a" * 32}' in cookie_header
    assert "msToken=web-token" in cookie_header
    assert "ads-token" not in cookie_header


async def test_browser_pool_init_scripts_preserve_plugins_chrome_and_account_renderer(browser_page, monkeypatch):
    from db.models import Account
    from executor import browser_pool

    read_profile = """() => {
        const gl = document.createElement('canvas').getContext('webgl');
        const ext = gl && gl.getExtension('WEBGL_debug_renderer_info');
        return {
            plugins: navigator.plugins.length,
            mimeTypesNative: navigator.mimeTypes instanceof MimeTypeArray,
            chromeLoadTimes: typeof window.chrome?.loadTimes,
            vendor: ext ? gl.getParameter(ext.UNMASKED_VENDOR_WEBGL) : null,
            renderer: ext ? gl.getParameter(ext.UNMASKED_RENDERER_WEBGL) : null,
        };
    }"""
    browser = browser_page.context.browser
    await browser_page.route("**/*", lambda route: route.fulfill(content_type="text/html", body="<html>Native control</html>"))
    await browser_page.goto("https://hardware.fixture.test/control")
    native_profile = await browser_page.evaluate(read_profile, isolated_context=False)
    pool = browser_pool.BrowserPool(max_browsers=1)
    monkeypatch.setattr(pool, "initialize", AsyncMock())
    pool._playwright = SimpleNamespace(chromium=SimpleNamespace(launch=AsyncMock(return_value=browser)), stop=AsyncMock())
    monkeypatch.setattr(browser_pool.settings, "USE_PROXY", False)
    monkeypatch.setattr(browser_pool.settings, "HUMAN_LIKE_BEHAVIOR", True)
    account = Account(id=975, label="init-local", proxy="", country_code="US", cookies='{"sessionid":"local"}')
    try:
        context, page = await pool.get_page_for_account(account)
        await context.route("**/*", lambda route: route.fulfill(content_type="text/html", body="<html>Local device fixture</html>"))
        await page.goto("https://hardware.fixture.test/")
        profile = await page.evaluate(read_profile, isolated_context=False)
        # Headless-shell legitimately omits PDF plugins and chrome.loadTimes.
        # The pool must preserve the browser's native capabilities, rather
        # than manufacture them. Full Chromium is covered by the device tests.
        assert profile["plugins"] == native_profile["plugins"]
        assert profile["mimeTypesNative"] is True
        assert profile["chromeLoadTimes"] == native_profile["chromeLoadTimes"]
        expected = browser_pool._desktop_hardware_profile_for_account(account)
        assert profile["renderer"] == expected["webglRenderer"]
        assert profile["vendor"] == expected["webglVendor"]
    finally:
        # The browser belongs to browser_page's fixture; close our context only.
        await pool.close_context_for_account(account.id)
        pool._browsers.clear()
        await pool.close_all()


@pytest.mark.parametrize("signal", ["sdk", "delayed_sdk", "fetch_wrapper", "delayed_fetch_wrapper", "ms_token_resource", "signature_resource", "x_bogus_resource", "x_gnarly_resource", "both_signatures_resource", "empty_signatures_resource", "native", "foreign_resource"])
async def test_runtime_readiness_observes_main_world_signals(browser_page, signal):
    from executor.http.signature_runtime import wait_for_runtime_signer_ready

    async def fulfill(route):
        await route.fulfill(status=200, content_type="text/html", body="<html><body>local</body></html>")

    await browser_page.route("**/*", fulfill)
    await browser_page.goto("https://www.tiktok.com/@demo/video/123")
    if signal == "sdk":
        await browser_page.evaluate("window.byted_acrawler = {frontierSign: () => ({})}", isolated_context=False)
    elif signal == "delayed_sdk":
        await browser_page.evaluate("setTimeout(() => {window.byted_acrawler = {sign: () => ({})}}, 150)", isolated_context=False)
    elif signal in {"fetch_wrapper", "delayed_fetch_wrapper"}:
        await browser_page.evaluate("""() => {
            const original = window.fetch;
            window.fetch = function(...args) { return original.apply(this, args); };
        }""" if signal == "fetch_wrapper" else """() => {
            window.byted_acrawler={sign:()=>({})};
            setTimeout(()=>{const original=fetch;window.fetch=function(...args){return original.apply(this,args)}},150);
        }""", isolated_context=False)
    elif signal.endswith("_resource") and signal != "foreign_resource":
        query = {"ms_token_resource":"msToken=local", "signature_resource":"a_bogus=local",
                 "x_bogus_resource":"X-Bogus=local", "x_gnarly_resource":"X-Gnarly=local",
                 "both_signatures_resource":"X-Bogus=local&X-Gnarly=local", "empty_signatures_resource":"X-Bogus=&X-Gnarly="}[signal]
        await browser_page.evaluate("async query => { await (await fetch('/api/comment/list/?' + query)).text(); }", query, isolated_context=False)
    elif signal == "foreign_resource":
        await browser_page.evaluate("performance.getEntriesByType = () => [{name: 'https://example.com/api/?X-Bogus=local&X-Gnarly=local'}]", isolated_context=False)

    ready = await wait_for_runtime_signer_ready(browser_page, timeout_ms=600)
    assert ready is (signal == "both_signatures_resource")


@pytest.mark.parametrize("signed", [False, True])
async def test_signer_wait_activity_wakes_background_traffic_once_without_false_readiness(browser_page, signed):
    from executor.http.signature_runtime import wait_for_runtime_signer_ready
    requests = []

    async def serve(route):
        if urlparse(route.request.url).path == "/api/background/":
            requests.append(route.request.url)
        await route.fulfill(content_type="text/html", body="<html>Local</html>")

    await browser_page.route("**/*", serve)
    await browser_page.goto("https://www.tiktok.com/@demo/video/123")
    await browser_page.evaluate("""signed => {
        window.wakeStats = {moves: 0, scrolls: []};
        const original = window.scrollBy;
        window.scrollBy = function(...args) {
            window.wakeStats.scrolls.push(args);
            return original.apply(this, args);
        };
        window.addEventListener('mousemove', () => {
            window.wakeStats.moves++;
            const query = signed ? '?X-Bogus=local&X-Gnarly=local' : '';
            fetch('/api/background/' + query).then(response => response.text());
        });
    }""", signed, isolated_context=False)
    assert await wait_for_runtime_signer_ready(browser_page, timeout_ms=600) is signed
    assert len(requests) == 1
    stats = await browser_page.evaluate("window.wakeStats", isolated_context=False)
    assert stats == {"moves": 1, "scrolls": [[0, 80]]}


@pytest.mark.parametrize("masked", [False, True])
async def test_warmup_requires_signed_resource_despite_early_ttwid_sdk_and_fetch_wrapper(browser_page, masked):
    from executor import engagement_engine
    from executor.http.signature_runtime import wait_for_runtime_signer_ready
    if masked:
        from executor.device_profiles import build_device_profile, install_device_profile, prepare_device_page
        profile=build_device_profile(SimpleNamespace(id=1224), {}, browser_page.context.browser.version)
        await install_device_profile(browser_page.context, profile)
        await prepare_device_page(browser_page)
    await browser_page.context.add_init_script(
        "window.byted_acrawler={sign:()=>({})};setTimeout(" + _COMMENT_INTERCEPTOR_JS + ",450);"
    )
    requests=[]

    async def serve(route):
        url=urlparse(route.request.url)
        if url.path=='/api/comment/list/':
            query=parse_qs(url.query)
            signed=all(query.get(key) for key in ('X-Bogus','X-Gnarly'))
            requests.append(signed)
            await route.fulfill(content_type='application/json',body='{"comments":[]}' if signed else '',headers={
                'Set-Cookie':'tt_csrf_token=local; Path=/; Secure; SameSite=Lax',
            })
        else:
            await route.fulfill(content_type='text/html',body='<html>Local</html>',headers={
                'Set-Cookie':'ttwid=early-wid; Path=/; Secure; SameSite=Lax',
            })

    await browser_page.route('**/*',serve)
    await browser_page.context.add_cookies([{
        'name':'sessionid','value':'local-session','domain':'.tiktok.com','path':'/','secure':True,
    }])
    url='https://www.tiktok.com/@demo/video/7635267329407913230'
    await browser_page.goto(url,wait_until='commit')
    browser_page._http_auth_verified_at=time.monotonic()
    cookies,valid,info=await engagement_engine.warmup_page_for_http(browser_page,url,max_retries=1)
    assert valid and info['signer_ready'] is False and info['attempts']==1
    assert cookies['ttwid']=='early-wid'
    assert requests==[True]
    assert browser_page._comment_list_http_meta['attempts']==1
    assert await wait_for_runtime_signer_ready(browser_page, timeout_ms=600)


@pytest.mark.parametrize("navigation_delay", [0.0, 1.8])
async def test_warmup_respects_dom_age_after_slow_document_navigation(browser_page, navigation_delay):
    from executor import engagement_engine
    url = "https://www.tiktok.com/@demo/video/7635267329407913230"

    async def serve(route):
        if urlparse(route.request.url).path == "/api/background/":
            await route.fulfill(content_type="application/json", body="{}")
            return
        await asyncio.sleep(navigation_delay)
        await route.fulfill(content_type="text/html", body="""<html><body><script>
            fetch('/api/background/?X-Bogus=local&X-Gnarly=local').then(response => response.text());
        </script></body></html>""")

    await browser_page.route("**/*", serve)
    await browser_page.context.add_cookies([
        {"name": name, "value": "fixture", "domain": ".tiktok.com", "path": "/", "secure": True}
        for name in ("sessionid", "ttwid", "tt_csrf_token", "msToken")
    ])
    await browser_page.goto(url, wait_until="commit")
    browser_page._http_auth_verified_at = time.monotonic()
    _, valid, info = await engagement_engine.warmup_page_for_http(browser_page, url, max_retries=1)
    assert valid and info["signer_ready"] and info["load_state"] == "domcontentloaded"
    assert info["target_page_age_ms"] >= 3190 and info["target_dom_age_ms"] >= 1790
    timing = await browser_page.evaluate("""() => {
        const domReady = performance.getEntriesByType('navigation')[0].domContentLoadedEventEnd;
        return {domReady, elapsedSinceDom: performance.now() - domReady};
    }""", isolated_context=False)
    assert timing["elapsedSinceDom"] >= 1790
    assert timing["domReady"] >= navigation_delay * 1000 - 100


async def test_comment_list_retry_recovers_when_interceptor_arrives_after_empty_response(browser_page):
    from executor.http.comment_discovery import fetch_comment_page
    await browser_page.context.add_init_script('setTimeout(' + _COMMENT_INTERCEPTOR_JS + ',400);')
    requests=[]

    async def serve(route):
        url=urlparse(route.request.url)
        if url.path=='/api/comment/list/':
            query=parse_qs(url.query)
            signed=all(query.get(key) for key in ('X-Bogus','X-Gnarly'))
            requests.append((signed,time.monotonic()))
            await route.fulfill(content_type='application/json',body='{"comments":[]}' if signed else '')
        else:
            await route.fulfill(content_type='text/html',body='<html>Local</html>')

    await browser_page.route('**/*',serve)
    await browser_page.goto('https://www.tiktok.com/@demo/video/123')
    assert await fetch_comment_page(browser_page,'123')=={'comments':[]}
    assert [signed for signed,_ in requests]==[False,True]
    assert requests[1][1]-requests[0][1]>=1.4
    assert browser_page._comment_list_http_meta['retry_count']==1


@pytest.mark.parametrize("reply", [False, True])
async def test_comment_retries_refresh_cookies_and_never_presign_native_fetch(browser_page, monkeypatch, reply):
    from executor.http import comment_discovery as api
    monkeypatch.setattr(api, "_COMMENT_LIST_RETRY_DELAY_SECONDS", 0.02)
    requests = []
    path = "/api/comment/list/reply/" if reply else "/api/comment/list/"

    async def serve(route):
        if urlparse(route.request.url).path != path:
            await route.fulfill(content_type="text/html", body="<html>Local</html>")
            return
        requests.append(parse_qs(urlparse(route.request.url).query))
        index = len(requests)
        headers = {"Set-Cookie": f"msToken=token-{index + 1}; Domain=.tiktok.com; Path=/; Secure; SameSite=Lax"} if index < 3 else {}
        await route.fulfill(content_type="application/json", headers=headers,
                            body="" if index == 2 else '{"status_code":0,"comments":[],"has_more":0}')

    await browser_page.route("**/*", serve)
    await browser_page.goto("https://www.tiktok.com/@demo/video/123")
    await browser_page.context.add_cookies([{
        "name": "msToken", "value": "token-1", "domain": ".tiktok.com", "path": "/", "secure": True,
    }])
    await browser_page.evaluate("""() => {
        window.manualSignCalls = 0;
        window.byted_acrawler = {sign: () => {
            window.manualSignCalls++;
            return {'X-Bogus': 'manual-signature'};
        }};
        const original = window.fetch;
        let calls = 0;
        window.fetch = function(input, options) {
            const url = new URL(input, location.href);
            if (['X-Bogus', 'X-Gnarly', 'a_bogus'].some(key => url.searchParams.has(key)))
                throw new Error('Native fetch received a presigned URL');
            const token = document.cookie.split('; ').find(row => row.startsWith('msToken=')).split('=').slice(1).join('=');
            if (url.searchParams.get('msToken') !== token) throw new Error('Stale msToken in URL');
            if (++calls > 1) {
                url.searchParams.set('X-Bogus', 'native-' + calls);
                url.searchParams.set('X-Gnarly', 'native-' + calls);
            }
            return original.call(this, url.href, options);
        };
    }""", isolated_context=False)
    cookies = AsyncMock(side_effect=browser_page.context.cookies)
    monkeypatch.setattr(browser_page.context, "cookies", cookies)
    async with api.comment_read_batch(browser_page):
        for cursor in (0, 20):
            payload = (await api.fetch_reply_page(browser_page, "123", "456", cursor) if reply
                       else await api.fetch_comment_page(browser_page, "123", cursor, fresh=True))
            assert payload == {"status_code": 0, "comments": [], "has_more": 0}
            if cursor == 0:
                assert browser_page._comment_list_http_meta["attempts"] == 3
    assert len(requests) == 4 and cookies.await_count == 3
    for index, (query, version, cursor) in enumerate(zip(requests, (1, 2, 3, 3), (0, 0, 0, 20)), start=1):
        assert query["msToken"] == [f"token-{version}"] and query["cursor"] == [str(cursor)]
        assert query["count"] == ["20"]
        assert query.get("X-Bogus") == ([f"native-{index}"] if index > 1 else None)
        assert query.get("X-Gnarly") == ([f"native-{index}"] if index > 1 else None)
    assert await browser_page.evaluate("window.manualSignCalls", isolated_context=False) == 0


async def test_comment_api_discovers_numeric_parent_without_dom(browser_page):
    from executor.http.comment_discovery import find_parent_comments
    from executor.user_schema import PublishedMain
    seen_cursors = []

    async def fulfill(route):
        url = urlparse(route.request.url)
        if url.path == "/api/comment/list/":
            params = parse_qs(url.query)
            seen_cursors.append(params["cursor"][0])
            body = {"status_code": 0, "comments": [], "has_more": 1, "cursor": 50}
            if params["cursor"] == ["50"]:
                body.update(comments=[{"cid": "7641761192486194454", "text": "Where can I find this?", "user": {"unique_id": "target"}}], has_more=0)
            await route.fulfill(status=200, content_type="application/json", body=json.dumps(body))
        else:
            await route.fulfill(status=200, content_type="text/html", body="<html><body>No comment drawer</body></html>")

    await browser_page.route("**/*", fulfill)
    await browser_page.goto("https://www.tiktok.com/@demo/video/123")
    await browser_page.evaluate(_COMMENT_INTERCEPTOR_JS, isolated_context=False)
    matches, info = await find_parent_comments(browser_page, "123", [PublishedMain("find this", None, False, False, "target", 0, "target")], phrase_match=True)
    assert matches[0]["cid"] == "7641761192486194454"
    assert seen_cursors == ["0", "50"]
    assert info["available"]


async def test_warmup_comment_get_refreshes_live_ms_token(browser_page, monkeypatch):
    from executor import engagement_engine
    seen = []

    async def fulfill(route):
        if urlparse(route.request.url).path == "/api/comment/list/":
            seen.append(route.request.url)
            await route.fulfill(status=200, content_type="application/json", body='{"status_code":0,"comments":[],"has_more":0}',
                                headers={"set-cookie": "msToken=fresh; Path=/; Secure; SameSite=None"})
        else:
            await route.fulfill(status=200, content_type="text/html", body="<html><body>Local</body></html>")

    await browser_page.route("**/*", fulfill)
    url = "https://www.tiktok.com/@demo/video/7635267329407913230"
    await browser_page.goto(url)
    await browser_page.evaluate(_COMMENT_INTERCEPTOR_JS, isolated_context=False)
    await browser_page.context.add_cookies([{"name": name, "value": value, "domain": ".tiktok.com", "path": "/", "secure": True}
                                           for name, value in (("sessionid", "session"), ("ttwid", "wid"), ("msToken", "old"))])
    monkeypatch.setattr(engagement_engine, "wait_for_runtime_signer_ready", AsyncMock(return_value=True))
    cookies, valid, info = await engagement_engine.warmup_page_for_http(browser_page, url)
    assert valid and info["comment_api_ready"]
    assert cookies["msToken"] == "fresh"
    assert len(seen) == 1 and "count=20" in seen[0]


@pytest.mark.parametrize("condition", ["available", "empty_api", "captcha"])
async def test_replies_only_uses_comment_api_then_native_post_without_dom(browser_page, monkeypatch, condition):
    from unittest.mock import Mock
    from executor import engagement_engine, user_schema
    from services.cookie_validator import normalize_cookie_records
    media_id, parent_id, reply_id = "7635267329407913230", "7641761192486194454", "7641761192486194458"
    url = f"https://www.tiktok.com/@demo/photo/{media_id}"
    requests = []

    async def local_route(route):
        request = route.request
        path = urlparse(request.url).path
        if path == "/api/comment/list/":
            requests.append(("list", request.url))
            body = "" if condition == "empty_api" else json.dumps({
                "status_code": 0, "comments": [{"cid": parent_id, "text": "target comment", "user": {"unique_id": "author"}}], "has_more": 0,
            })
            await route.fulfill(content_type="application/json", body=body)
        elif path == "/api/comment/publish/":
            requests.append(("publish", request.url))
            body = parse_qs(request.post_data)
            assert body["reply_id"] == [parent_id]
            assert body["text"] == ["my reply"]
            await route.fulfill(content_type="application/json", body=json.dumps({
                "status_code": 0, "comment": {"cid": reply_id, "reply_id": parent_id},
            }))
        else:
            await route.fulfill(content_type="text/html", body="<html>Local API test</html>")

    await browser_page.route("**/*", local_route)
    await browser_page.goto(url)
    await browser_page.evaluate(_COMMENT_INTERCEPTOR_JS, isolated_context=False)
    await browser_page.context.add_cookies([
        {"name": name, "value": name, "domain": ".tiktok.com", "path": "/", "secure": True}
        for name in ("sessionid", "ttwid", "tt_csrf_token", "msToken")
    ])

    async def sync(*args, **kwargs):
        return normalize_cookie_records(await browser_page.context.cookies()), False

    for flag in ("TIKTOK_NETWORK_FIRST", "TIKTOK_NETWORK_IMPL_READY"):
        monkeypatch.setattr(user_schema._settings, flag, True)
    monkeypatch.setattr(user_schema._settings, "PARALLEL_COMMENTING_DOM_FIRST", False)
    for name, value in {
        "_preflight_cookie_http_gate": None, "create_page_for_account": (browser_page.context, browser_page),
        "_ensure_account_login_ready": (True, None), "_fast_visible_captcha_reason": None,
        "_deny_if_visible_logged_out": None, "_ensure_page_on_target_media": True,
        "_persist_cookies_after_success": None, "_safe_close": None,
        "get_browser_login_state": {"state": "unknown"},
        "_visible_captcha_reason": "captcha_challenge:slider" if condition == "captcha" else None,
    }.items():
        monkeypatch.setattr(user_schema, name, AsyncMock(return_value=value))
    monkeypatch.setattr(user_schema, "get_http_execution_strategy", Mock(return_value={"strategy": "http_first"}))
    for name in ("_register_runtime_success", "_register_runtime_failure"):
        monkeypatch.setattr(user_schema, name, Mock())
    monkeypatch.setattr(engagement_engine, "_should_skip_http_for_account", Mock(return_value=None))
    monkeypatch.setattr(engagement_engine, "_record_http_account_result", Mock())
    monkeypatch.setattr(engagement_engine, "wait_for_runtime_signer_ready", AsyncMock(return_value=True))
    monkeypatch.setattr(engagement_engine, "synchronize_browser_cookies", sync)
    monkeypatch.setattr(engagement_engine.asyncio, "sleep", AsyncMock())
    dom = AsyncMock(side_effect=AssertionError("HTTP replies must never enter a DOM publish/search path"))
    for name in ("publish_reply_hybrid", "_open_comment_list_with_safe_retry", "_find_main_anchors_batch", "_enrich_cookies_with_hard_deadline"):
        monkeypatch.setattr(user_schema, name, dom)

    result = await user_schema._reply_with_variant_on_all_mains(
        None, SimpleNamespace(id=7, proxy=None), url,
        [user_schema.PublishedMain("target comment", None, False, False, "target", 0, "author")],
        "my reply", variant_index=1, correlation_id="task_engagement_reply_api",
    )

    assert result.replies[0].success is (condition == "available")
    assert [kind for kind, _url in requests].count("publish") == int(condition == "available")
    if condition == "available":
        assert result.replies[0].reply_comment_id == reply_id
        assert result.found_mains[0].comment_id == parent_id
        assert requests[0][0] == "list"
    elif condition == "captcha":
        assert result.replies[0].error == "captcha_challenge:slider"
    else:
        assert result.replies[0].error == "parent_api_unavailable:empty_body"
    dom.assert_not_awaited()


async def test_browser_pool_explicitly_cleans_startup_antifraud_cookies(browser_page, monkeypatch):
    from db.models import Account
    from executor import browser_pool, tiktok_client_async
    browser = browser_page.context.browser
    pool = browser_pool.BrowserPool(max_browsers=1)
    monkeypatch.setattr(pool, "initialize", AsyncMock())
    pool._playwright = SimpleNamespace(chromium=SimpleNamespace(launch=AsyncMock(return_value=browser)), stop=AsyncMock())
    monkeypatch.setattr(browser_pool.settings, "USE_PROXY", False)
    # Bypass the parser's own cleanup to exercise BrowserPool's boundary.
    monkeypatch.setattr(tiktok_client_async, "parse_cookies", json.loads)
    records = [
        {"name": "sid_guard", "value": f'{"a" * 32}|{int(time.time())}|86400', "domain": ".tiktok.com"},
        {"name": "msToken", "value": "old-db-token", "expirationDate": 2_147_483_647, "domain": ".tiktok.com"},
        {"name": "tt_csrf_token", "value": "old-csrf", "domain": ".tiktok.com"},
        {"name": "_waftokenid", "value": "old-waftoken", "domain": ".tiktok.com"},
    ]
    account = Account(id=978, label="startup-cleanup", proxy="", country_code="US", cookies=json.dumps(records))
    try:
        context, _ = await pool.get_page_for_account(account)
        saved = {row["name"]: row["value"] for row in await context.cookies()}
        assert not {"msToken", "tt_csrf_token", "_waftokenid"} & saved.keys()
        assert all(saved[name] == "a" * 32 for name in ("sessionid", "sid_tt", "sessionid_ss"))
    finally:
        await pool.close_context_for_account(account.id)
        pool._browsers.clear()
        await pool.close_all()
