"""Local Chromium checks: no requests reach TikTok."""
import json
from types import SimpleNamespace


import pytest

from executor import browser_pool, engagement_engine, tiktok_client
from executor.http.comment_discovery import _COMMENT_LIST_RETRY_FETCH_JS, _fetch_with_native_activation
from executor.http.playwright_http_client import PlaywrightHttpClient
from tests.integration.test_http_browser_transport import browser_page as browser_page

pytestmark = [pytest.mark.integration, pytest.mark.asyncio]
URL = "https://www.tiktok.com/@fixture/video/7635267329407913230"
SHORT = "https://vt.tiktok.com/local-fixture/"


async def test_retry_fetch_yields_to_async_event_handlers(browser_page):
    await browser_page.route("**/*", lambda route: route.fulfill(content_type="text/html", body="<button data-e2e='comment-icon'>comments</button>"))
    await browser_page.goto(URL)
    await browser_page.evaluate("""() => {
        window.handlerReady = false;
        window.addEventListener('mousemove', () => setTimeout(() => { window.handlerReady = true; }, 50));
        window.fetch = async url => ({status: 200, url, headers: new Headers(), text: async () => JSON.stringify({ready: window.handlerReady})});
    }""", isolated_context=False)
    endpoint = "https://www.tiktok.com/api/comment/list/?aweme_id=7635267329407913230&cursor=0&count=20"
    response = await _fetch_with_native_activation(browser_page, _COMMENT_LIST_RETRY_FETCH_JS, endpoint, {})
    assert json.loads(response["text"]) == {"ready": True}


async def test_anonymous_redirect_and_next_account_share_chromium(monkeypatch):
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
    import asyncio
    import threading
    monkeypatch.setattr(browser_pool.settings, "USE_PROXY", False)
    monkeypatch.setattr(tiktok_client, "_get_cookies_for_account", lambda _: '{"sessionid":"local-only"}')
    requests = []
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_):
            pass
        def do_GET(self):
            requests.append((self.path, self.headers.get("Cookie", "")))
            if self.path == "/short":
                self.send_response(302)
                self.send_header("Location", "/@fixture/video/7635267329407913230")
                self.end_headers()
            else:
                self.send_response(200)
                self.end_headers()
                self.wfile.write(b"<html>local</html>")
    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    worker = threading.Thread(target=server.serve_forever, daemon=True)
    worker.start()
    origin = f"http://127.0.0.1:{server.server_port}"
    pool = browser_pool.BrowserPool(max_browsers=1)
    try:
        assert await pool.resolve_shortlink(origin + "/short") == origin + "/@fixture/video/7635267329407913230"
        browser = pool._browsers["direct"]
        assert [path for path, _ in requests[:2]] == ["/short", "/@fixture/video/7635267329407913230"]
        assert all("sessionid" not in cookie for _, cookie in requests)
        assert not pool._contexts
        context, page = await pool.get_page_for_account(SimpleNamespace(
            id=1241, label="local fixture", proxy=None, country_code=None, user_agent=None,
        ))
        assert context.browser is browser and len(pool._browsers) == 1
        metrics = await page.evaluate("() => ({innerHeight,outerHeight})", isolated_context=False)
        assert metrics["outerHeight"] > metrics["innerHeight"]
        await pool.close_page_from_pool(page)
        assert browser.is_connected()
    finally:
        await pool.close_all(stop_playwright=True)
        await asyncio.to_thread(server.shutdown)
        server.server_close()
        worker.join(timeout=2)


async def test_warmup_reuses_resolved_shortlink_and_parsed_ssr(browser_page):
    payload = {"__DEFAULT_SCOPE__": {"webapp.app-context": {"wid": "native-device", "wid_ct": "1710000000", "region": "US"}}}
    async def serve(route):
        if "/api/comment/list/" in route.request.url:
            await route.fulfill(content_type="application/json", body='{"status_code":0,"comments":[],"has_more":0}')
        else:
            await route.fulfill(content_type="text/html", body=f'<html><script id="__UNIVERSAL_DATA_FOR_REHYDRATION__" type="application/json">{json.dumps(payload)}</script></html>')
    await browser_page.route("**/*", serve)
    await browser_page.context.add_cookies([
        {"name": name, "value": "fixture-token", "domain": ".tiktok.com", "path": "/"}
        for name in ("sessionid", "ttwid", "tt_csrf_token", "msToken")
    ])
    await browser_page.goto(URL)
    await browser_page.evaluate("""async () => {
        const nativeParse = JSON.parse;
        window.ssrParseCount = 0;
        JSON.parse = function(value, ...args) {
            if (String(value).includes('webapp.app-context')) window.ssrParseCount++;
            return nativeParse.call(this, value, ...args);
        };
        await fetch('/api/comment/list/?aweme_id=7635267329407913230&cursor=0&X-Bogus=local&X-Gnarly=local').then(r => r.text());
    }""", isolated_context=False)
    browser_page._cached_first_comment_page = {"status_code": 0, "comments": [], "has_more": 0}
    browser_page._cached_first_comment_aweme_id = "7635267329407913230"
    before = await browser_page.evaluate("performance.getEntriesByType('navigation')[0].startTime", isolated_context=False)
    _, valid, info = await engagement_engine.warmup_page_for_http(browser_page, SHORT, max_retries=1)
    assert valid and info["reused_page"]
    assert info["target_page_age_ms"] >= info["required_nav_age_ms"]
    assert info["target_dom_age_ms"] >= info["required_dom_age_ms"]
    assert await browser_page.evaluate("performance.getEntriesByType('navigation')[0].startTime", isolated_context=False) == before
    client = await PlaywrightHttpClient.from_page(browser_page)
    assert client.runtime_profile["device_id"] == "native-device"
    assert client.runtime_profile["root_referer"] == ""
    assert await browser_page.evaluate("window.ssrParseCount", isolated_context=False) == 1
    await browser_page.evaluate("document.querySelector('#__UNIVERSAL_DATA_FOR_REHYDRATION__').textContent += ' '", isolated_context=False)
    await PlaywrightHttpClient.from_page(browser_page)
    assert await browser_page.evaluate("window.ssrParseCount", isolated_context=False) == 2
