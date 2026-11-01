"""Task 1246 regressions in Chromium; every request is intercepted locally."""
import json
import time
from types import SimpleNamespace
from unittest.mock import AsyncMock
from urllib.parse import parse_qs, urlparse

import pytest

from executor import engagement_engine as engine
from executor.session_bootstrap import get_browser_login_state
from services.cookie_validator import remember_imported_ms_token
from tests.integration.test_http_browser_transport import browser_page as browser_page

pytestmark = [pytest.mark.integration, pytest.mark.asyncio]
VIDEO = "7699894341501832466"
URL = f"https://www.tiktok.com/@fixture/video/{VIDEO}"


@pytest.mark.parametrize("source", ["sigi", "universal", "body"])
async def test_login_probe_skips_large_container_layout_and_unneeded_html_serialization(browser_page, source):
    state = '{"isLogin":true}' if source != "body" else '{"isLogin":false}'
    identifier = "SIGI_STATE" if source == "sigi" else "__UNIVERSAL_DATA_FOR_REHYDRATION__"
    state_html = f'<script id="{identifier}" type="application/json">{state}</script>' if source != "body" else f'<script>window.localState={state};</script>'
    await browser_page.route("**/*", lambda route: route.fulfill(content_type="text/html", body=f'<html><body>{state_html}<div id="large"></div></body></html>'))
    await browser_page.goto(URL)
    await browser_page.evaluate("""() => {
        const large = document.getElementById('large');
        const fragment = document.createDocumentFragment();
        for (let i = 0; i < 1200; i++) {
            const node = document.createElement('p');
            node.textContent = 'Feed text with log in far inside a long paragraph ' + 'x'.repeat(400);
            fragment.append(node);
        }
        large.append(fragment);
        window.probeReads = {largeLayout: 0, bodyHtml: 0};
        Object.defineProperty(large, 'innerText', {get() {
            window.probeReads.largeLayout++; throw new Error('Large container forced layout');
        }});
        const descriptor = Object.getOwnPropertyDescriptor(Element.prototype, 'innerHTML');
        Object.defineProperty(document.body, 'innerHTML', {get() {
            window.probeReads.bodyHtml++;
            return descriptor.get.call(this);
        }});
    }""")
    result = await get_browser_login_state(browser_page)
    assert result["state"] == ("logged_out" if source == "body" else "logged_in")
    assert await browser_page.evaluate("window.probeReads") == {
        "largeLayout": 0, "bodyHtml": int(source == "body"),
    }


@pytest.mark.parametrize("initial_signer_ready", [True, False])
async def test_imported_token_requires_native_read_before_shared_parent_reply_path(browser_page, monkeypatch, initial_signer_ready):
    reads = []
    async def serve(route):
        if urlparse(route.request.url).path == "/api/comment/list/":
            reads.append(route.request.url)
            await route.fulfill(content_type="application/json", headers={"x-ms-token": "fresh-native"},
                                body='{"status_code":0,"comments":[],"has_more":0}')
        else:
            await route.fulfill(content_type="text/html", body="<html><button data-e2e='comment-icon'>comments</button></html>")
    await browser_page.route("**/*", serve)
    records = [{"name": name, "value": "imported" if name == "msToken" else "fixture",
                "domain": ".tiktok.com", "path": "/"}
               for name in ("sessionid", "ttwid", "tt_csrf_token", "msToken")]
    remember_imported_ms_token(browser_page.context, records)
    await browser_page.context.add_cookies(records)
    await browser_page.goto(URL)
    await browser_page.evaluate("""video => {
        const fetch = window.fetch;
        window.fetch = (input, options) => {
            const url = new URL(input, location.href); url.searchParams.set('X-Bogus', 'fixture');
            return fetch(url.href, options);
        };
        document.querySelector('button').onclick = () => {
            window.fetch('/api/comment/list/?aweme_id=' + video + '&cursor=0&count=20&native=1');
        };
    }""", VIDEO, isolated_context=False)
    monkeypatch.setattr(engine, "wait_for_runtime_signer_ready", AsyncMock(return_value=initial_signer_ready))
    browser_page._http_auth_verified_at = time.monotonic()
    account = SimpleNamespace(id=None, cookies=json.dumps(records))
    cookies, ready, info = await engine.warmup_page_for_http(browser_page, URL, account=account, max_retries=1)
    assert ready, info["reason_code"]
    assert cookies["msToken"] == "fresh-native" and info["ms_token_from_import_only"]
    assert info["comment_api_native_only"] is (not initial_signer_ready)
    assert len(reads) == 1 and parse_qs(urlparse(reads[0]).query)["native"] == ["1"]
    if initial_signer_ready:
        browser_page._cached_first_comment_page = None
        _, ready, info = await engine.warmup_page_for_http(browser_page, URL, account=account, max_retries=1)
        assert ready and info["comment_api_skipped"] and len(reads) == 1
