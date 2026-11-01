import json
import time
from urllib.parse import urlparse

import pytest

from executor import engagement_engine
from executor.http import comment_discovery as discovery
from executor.http.playwright_http_client import PlaywrightHttpClient
from executor.http.signature_runtime import wait_for_runtime_signer_ready
from tests.integration.test_http_browser_transport import browser_page as browser_page

pytestmark = [pytest.mark.integration, pytest.mark.asyncio]
VIDEO = "7635267329407913230"
URL = f"https://www.tiktok.com/@fixture/video/{VIDEO}"
SHORT = "https://vt.tiktok.com/local-fixture/"
CLOCK_TOLERANCE_MS = int(time.get_clock_info("monotonic").resolution * 1000) + 1


@pytest.mark.parametrize("ssr_source", ["universal", "sigi"])
async def test_three_runtime_reads_reuse_parsed_ssr_but_see_same_url_updates_and_reload(browser_page, ssr_source):
    payload = {"__DEFAULT_SCOPE__": {"webapp.app-context": {
        "wid": "7600000000000000001", "wid_ct": "1710000000", "region": "US",
    }}, "padding": "z" * 100000}
    universal = json.dumps(payload) if ssr_source == "universal" else ""
    sigi = json.dumps({**payload, "AppContext": {}}) if ssr_source == "sigi" else '{"AppContext":{}}'
    body = (f'<script id="__UNIVERSAL_DATA_FOR_REHYDRATION__" type="application/json">{universal}</script>'
            f'<script id="SIGI_STATE" type="application/json">{sigi}</script>')
    body += """<script>
        window.byted_acrawler = {sign: () => ({})};
        const nativeFetch = window.fetch;
        window.fetch = (input, options) => {
            const url = new URL(input, location.href);
            url.searchParams.set('X-Bogus', 'fixture');
            return nativeFetch(url.href, options);
        };
    </script>"""
    async def serve(route):
        if urlparse(route.request.url).path == "/api/comment/list/":
            await route.fulfill(content_type="application/json", body='{"status_code":0,"comments":[],"has_more":0}')
        else:
            await route.fulfill(content_type="text/html", body=body)
    await browser_page.route("**/*", serve)
    await browser_page.context.add_init_script("""() => {
        const parse = JSON.parse;
        window.parseCounts = {universal: 0, sigi: 0};
        JSON.parse = function(raw, ...args) {
            if (String(raw).includes('AppContext')) window.parseCounts.sigi++;
            else if (String(raw).includes('webapp.app-context')) window.parseCounts.universal++;
            return parse.call(this, raw, ...args);
        };
    }""".replace("() => {", "(() => {", 1) + ")();")
    await browser_page.context.add_cookies([
        {"name": name, "value": "fixture", "domain": ".tiktok.com", "path": "/"}
        for name in ("sessionid", "ttwid", "tt_csrf_token", "msToken")
    ])
    await browser_page.goto(URL)
    await browser_page.evaluate("""short => {
        const entries = performance.getEntriesByType.bind(performance);
        performance.getEntriesByType = kind => kind === 'navigation'
            ? entries(kind).map(entry => ({name: short, domContentLoadedEventEnd: entry.domContentLoadedEventEnd}))
            : entries(kind);
    }""", SHORT, isolated_context=False)
    browser_page._http_auth_verified_at = time.monotonic()
    _, valid, info = await engagement_engine.warmup_page_for_http(browser_page, SHORT, max_retries=1)
    assert valid and info["target_page_age_ms"] >= 2000 - CLOCK_TOLERANCE_MS and info["comment_api_skipped"]
    assert not getattr(browser_page, "_comment_list_http_meta", None)
    client = await PlaywrightHttpClient.from_page(browser_page, runtime_profile=info["_runtime_profile"])
    assert client.runtime_profile["device_id"] == "7600000000000000001"
    counts = {"universal": 0, "sigi": 0}
    counts[ssr_source] = 1
    assert await browser_page.evaluate("window.parseCounts", isolated_context=False) == counts
    element_id = '__UNIVERSAL_DATA_FOR_REHYDRATION__' if ssr_source == "universal" else 'SIGI_STATE'
    await browser_page.evaluate("""id => {
        const element = document.getElementById(id);
        element.textContent = element.textContent.replace('7600000000000000001', '7600000000000000002');
    }""", element_id, isolated_context=False)
    assert (await PlaywrightHttpClient.from_page(browser_page)).runtime_profile["device_id"] == "7600000000000000002"
    counts[ssr_source] = 2
    assert await browser_page.evaluate("window.parseCounts", isolated_context=False) == counts
    await browser_page.reload()
    assert (await PlaywrightHttpClient.from_page(browser_page)).runtime_profile["device_id"] == "7600000000000000001"


async def test_pagination_fetch_has_no_activation_events_scroll_or_fixed_delay(browser_page):
    async def serve(route):
        if "/api/comment/list" in route.request.url:
            await route.fulfill(content_type="application/json", body='{"status_code":0,"comments":[],"has_more":0}')
        else:
            await route.fulfill(content_type="text/html", body="<html>Local</html>")
    await browser_page.route("**/*", serve)
    await browser_page.goto(URL)
    await browser_page.evaluate("""() => {
        window.activity = {scrolls: 0, moves: [], delays: 0};
        window.scrollBy = () => { window.activity.scrolls++; };
        window.addEventListener('mousemove', event => window.activity.moves.push({
            mouse: event instanceof MouseEvent, x: event.clientX, y: event.clientY,
            bubbles: event.bubbles, trusted: event.isTrusted,
        }));
        const timeout = window.setTimeout;
        window.setTimeout = function(callback, delay, ...args) {
            if (delay === 250) window.activity.delays++;
            return timeout.call(this, callback, delay, ...args);
        };
        const nativeFetch = window.fetch;
        window.fetch = (input, options) => {
            const url = new URL(input, location.href);
            url.searchParams.set('X-Bogus', 'fixture');
            return nativeFetch(url.href, options);
        };
    }""", isolated_context=False)
    assert await discovery.fetch_comment_page(browser_page, VIDEO, cursor=20, fresh=True) is not None
    assert await discovery.fetch_reply_page(browser_page, VIDEO, "7641761192486195000", cursor=20) is not None
    assert await browser_page.evaluate("window.activity", isolated_context=False) == {"scrolls": 0, "moves": [], "delays": 0}
    assert await discovery.fetch_comment_page(browser_page, VIDEO, cursor=0, fresh=True) is not None
    assert await browser_page.evaluate("window.activity", isolated_context=False) == {
        "scrolls": 1, "moves": [{"mouse": True, "x": 120, "y": 160, "bubbles": True, "trusted": True}], "delays": 1,
    }


async def test_signer_wake_uses_browser_mouse_input(browser_page):
    await browser_page.route("**/*", lambda route: route.fulfill(content_type="text/html", body="<html>Local</html>"))
    await browser_page.goto(URL)
    await browser_page.evaluate("""() => {
        window.mouseEvents = [];
        window.addEventListener('mousemove', event => {
            window.mouseEvents.push({mouse: event instanceof MouseEvent, x: event.clientX,
                                     y: event.clientY, bubbles: event.bubbles, trusted: event.isTrusted});
            fetch('/api/background/?X-Bogus=fixture').then(response => response.text());
        });
    }""", isolated_context=False)
    assert await wait_for_runtime_signer_ready(browser_page, timeout_ms=1500)
    assert await browser_page.evaluate("window.mouseEvents", isolated_context=False) == [
        {"mouse": True, "x": 120, "y": 160, "bubbles": True, "trusted": True},
    ]
