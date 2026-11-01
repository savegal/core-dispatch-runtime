import json
import time
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from executor import device_profiles as devices, engagement_engine as engine
from executor.http import playwright_http_client as transport, comment_discovery as discovery
from tests.integration.test_http_browser_transport import browser_page as browser_page


pytestmark = [pytest.mark.integration, pytest.mark.asyncio]
VIDEO = "7635267329407913230"
URL = f"https://www.tiktok.com/@fixture/video/{VIDEO}"
CLOCK_TOLERANCE_MS = int(time.get_clock_info("monotonic").resolution * 1000) + 1


async def test_initialization_uses_one_full_profile_read_and_never_reuses_after_reload(browser_page, monkeypatch):
    reads = AsyncMock(wraps=transport._extract_runtime_profile)
    monkeypatch.setattr(transport, "_extract_runtime_profile", reads)
    monkeypatch.setattr(discovery, "_extract_runtime_profile", reads)
    payload = json.dumps({"__DEFAULT_SCOPE__": {"webapp.app-context": {
        "wid": "7600000000000000001", "region": "US"}}})
    body = f'<script id="__UNIVERSAL_DATA_FOR_REHYDRATION__" type="application/json">{payload}</script>'
    body += """<script>
        window.byted_acrawler = {sign: () => ({})};
        const original = window.fetch;
        window.fetch = (input, options) => {
            const url = new URL(input, location.href);
            url.searchParams.set('X-Bogus', 'local-fixture');
            return original(url.href, options);
        };
    </script>"""
    async def serve(route):
        if "/api/comment/list/" in route.request.url:
            await route.fulfill(content_type="application/json", body='{"status_code":0,"comments":[],"has_more":0}')
        else:
            await route.fulfill(content_type="text/html", body=body)
    await browser_page.route("**/*", serve)
    await browser_page.context.add_cookies([
        {"name": name, "value": "fixture", "domain": ".tiktok.com", "path": "/"}
        for name in ("sessionid", "ttwid", "tt_csrf_token", "msToken")
    ])
    await browser_page.goto(URL)
    browser_page._http_auth_verified_at = time.monotonic()
    _, valid, info = await engine.warmup_page_for_http(browser_page, URL, max_retries=1)
    assert valid
    assert info["target_page_age_ms"] >= 2000 - CLOCK_TOLERANCE_MS
    assert info["target_dom_age_ms"] >= 1500 - CLOCK_TOLERANCE_MS
    client = await transport.PlaywrightHttpClient.from_page(browser_page, runtime_profile=info["_runtime_profile"])
    assert client.runtime_profile["device_id"] == "7600000000000000001"
    assert reads.await_count == 1
    await browser_page.reload()
    await transport.PlaywrightHttpClient.from_page(browser_page, runtime_profile=info["_runtime_profile"])
    assert reads.await_count == 2


async def test_native_window_bounds_match_identity_and_viewport_after_page_creation(browser_page):
    profile = devices.build_device_profile(SimpleNamespace(id=97704), {}, browser_page.context.browser.version)
    await devices.install_device_profile(browser_page.context, profile)
    await devices.prepare_device_page(browser_page)
    await browser_page.set_viewport_size(profile["options"]["viewport"])
    # Match the real pool order: viewport is configured before CDP preparation.
    await devices.release_device_page(browser_page)
    browser_page._account_device_ready = None
    await devices.prepare_device_page(browser_page)
    await browser_page.route("**/*", lambda route: route.fulfill(content_type="text/html", body="<html>Local</html>"))
    await browser_page.goto(URL)
    geometry = profile["identity"]["geometry"]
    bounds = (await browser_page._account_device_session.send("Browser.getWindowForTarget"))["bounds"]
    assert (bounds["width"], bounds["height"]) == (geometry["outerWidth"], geometry["outerHeight"])
    actual = await browser_page.evaluate("() => ({outerWidth,outerHeight,innerHeight})", isolated_context=False)
    assert actual == {"outerWidth": geometry["outerWidth"], "outerHeight": geometry["outerHeight"],
                      "innerHeight": geometry["viewportHeight"]}
    worker_hw = await browser_page.evaluate("""() => new Promise((resolve, reject) => {
        const code = `
            const canvas = new OffscreenCanvas(4, 4);
            const gl = canvas.getContext('webgl2');
            const ext = gl.getExtension('WEBGL_debug_renderer_info');
            postMessage({
                cpu: navigator.hardwareConcurrency,
                memory: navigator.deviceMemory,
                vendor: gl.getParameter(ext.UNMASKED_VENDOR_WEBGL),
                renderer: gl.getParameter(ext.UNMASKED_RENDERER_WEBGL),
            });
        `;
        const url = URL.createObjectURL(new Blob([code], {type: 'text/javascript'}));
        const worker = new Worker(url);
        worker.onerror = err => { worker.terminate(); URL.revokeObjectURL(url); reject(new Error(err.message)); };
        worker.onmessage = ev => { worker.terminate(); URL.revokeObjectURL(url); resolve(ev.data); };
    })""", isolated_context=False)
    expected_hw = profile["identity"]["hardware"]
    assert worker_hw == {
        "cpu": expected_hw["hardwareConcurrency"],
        "memory": expected_hw["deviceMemory"],
        "vendor": expected_hw["webglVendor"],
        "renderer": expected_hw["webglRenderer"],
    }
