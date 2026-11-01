from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from executor import device_profiles, user_schema as schema
from services.cookie_validator import normalize_cookies_for_browser
from tests.integration.test_http_browser_transport import browser_page as browser_page

pytestmark = [pytest.mark.integration, pytest.mark.asyncio]
URL = "https://www.tiktok.com/@fixture/video/7635267329407913230"


@pytest.mark.parametrize("markup,verified", [
    ('<button data-e2e="inbox-icon">Inbox</button>', True),
    ('<div>Local unknown page</div>', False),
    ('<button>Log in</button>', False),
])
async def test_native_dom_proof_refreshes_auth_only_for_logged_in_page(browser_page, markup, verified):
    await browser_page.route("**/*", lambda route: route.fulfill(content_type="text/html", body=markup))
    await browser_page.goto(URL)
    await schema._fast_visible_captcha_reason(browser_page, phase="http_warmup")
    await schema._visible_logged_out_reason(browser_page, phase="http_warmup")
    assert schema._has_recent_http_browser_auth(browser_page) is verified
    if verified:
        await browser_page.reload()
        assert not schema._has_recent_http_browser_auth(browser_page)


async def test_context_page_callback_leaves_cdp_failure_for_pool_cleanup(browser_page, monkeypatch):
    context = await browser_page.context.browser.new_context()
    try:
        profile = device_profiles.build_device_profile(SimpleNamespace(id=97001, device_seed="a" * 64), {}, context.browser.version)
        await device_profiles.install_device_profile(context, profile)
        monkeypatch.setattr(context, "new_cdp_session", AsyncMock(side_effect=RuntimeError("fixture CDP failure")))
        page = await context.new_page()
        with pytest.raises(RuntimeError, match="fixture CDP failure"):
            await device_profiles.prepare_device_page(page)
        assert not page.is_closed()
    finally:
        await context.close()


async def test_browser_keeps_distinct_cookie_name_cases_on_wire(browser_page):
    sent = []
    async def serve(route):
        sent.append(await route.request.header_value("cookie") or "")
        await route.fulfill(content_type="text/html", body="<html>Local</html>")
    await browser_page.route("**/*", serve)
    await browser_page.context.add_cookies(normalize_cookies_for_browser([
        {"name": "sid_guard", "value": "a" * 32 + "|1800000000|86400", "domain": ".tiktok.com"},
        {"name": "SESSIONID", "value": "other", "domain": ".tiktok.com"},
        {"name": "msToken", "value": "native", "domain": ".tiktok.com"},
        {"name": "mstoken", "value": "different", "domain": ".tiktok.com"},
    ], drop_stale_antifraud=False))
    await browser_page.goto(URL)
    values = dict(part.strip().split("=", 1) for part in sent[0].split(";"))
    assert values["SESSIONID"] == "other" and values["sessionid"] == "a" * 32
    assert values["msToken"] == "native" and values["mstoken"] == "different"
