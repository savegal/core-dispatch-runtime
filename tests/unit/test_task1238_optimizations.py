import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

import pytest

from executor import engagement_engine, user_schema
from executor.http.comment_discovery import fetch_comment_page
from executor.http.playwright_http_client import _extract_runtime_profile, PlaywrightHttpClient
from executor.http.request_profile import RequestProfile, resolve_request_geo
from executor.http.signature_runtime import resolve_request_signature, _SIGNER_READY_JS, wait_for_runtime_signer_ready


pytestmark = [pytest.mark.unit, pytest.mark.asyncio]
URL = "https://www.tiktok.com/@demo/video/7635267329407913230"


def cookies(*names):
    return [{"name": name, "value": name, "domain": ".tiktok.com"} for name in names]


async def test_warmup_does_not_force_renavigation_and_populates_device_id(monkeypatch):
    """Warmup does not force a slow second navigation if cookies are valid, and populates device_id into cookies_dict."""
    clock = [0.0]
    sleeps = []

    async def sleep(seconds):
        sleeps.append(seconds)
        clock[0] += seconds

    page = SimpleNamespace(
        url=URL,
        goto=AsyncMock(),
        wait_for_load_state=AsyncMock(),
        evaluate=AsyncMock(return_value={
            "current_url": URL,
            "navigation_url": URL,
            "age_ms": 5000,
            "dom_ready_ms": 500,
            "device_id": "71234567890",
            "odin_id": "odin_val",
            "web_id_last_time": "1710000000",
            "ssr_region": "UA",
        }),
        context=SimpleNamespace(
            cookies=AsyncMock(return_value=cookies("sessionid", "ttwid", "tt_csrf_token", "msToken")),
            add_cookies=AsyncMock(),
        ),
    )

    monkeypatch.setattr(engagement_engine.asyncio, "sleep", sleep)
    monkeypatch.setattr(engagement_engine.time, "monotonic", lambda: clock[0])
    monkeypatch.setattr(engagement_engine, "wait_for_runtime_signer_ready", AsyncMock(return_value=False))
    async def signed_probe(*args, **kwargs):
        page._comment_list_http_meta = {
            "security_params": {"X-Bogus": "native"}, "security_params_source": "request_event",
        }
        return {"comments": []}
    monkeypatch.setattr("executor.http.comment_discovery.fetch_comment_page", signed_probe)
    monkeypatch.setattr("executor.http.comment_discovery.activate_comment_list_via_page", signed_probe)

    cookies_dict, valid, info = await engagement_engine.warmup_page_for_http(
        page,
        URL,
        correlation_id="test_no_forced_renav",
        max_retries=2,
    )

    assert valid is True
    # Does not waste 10s on a forced second navigation attempt
    assert info["attempts"] == 1
    assert page._warmup_signer_ready is True
    assert info["signed_dispatch_seen"] is True and info["sdk_present"] is False
    # device_id and odin_id from timing populated into cookies_dict for validation
    assert cookies_dict.get("device_id") == "71234567890"
    assert cookies_dict.get("odin_id") == "odin_val"
    assert cookies_dict.get("WebIdLastTime") == "1710000000"


async def test_fetch_comment_page_fast_fails_when_warmup_signer_ready_is_false(monkeypatch):
    """An unsuccessful readiness recheck sends no manual API request."""
    page = SimpleNamespace(
        _warmup_signer_ready=False,
        context=SimpleNamespace(
            cookies=AsyncMock(return_value=cookies("sessionid", "ttwid")),
        ),
        evaluate=AsyncMock(return_value={"status": 200, "text": "", "url": "https://www.tiktok.com/api/comment/list/"}),
    )

    sleep_mock = AsyncMock()
    monkeypatch.setattr(asyncio, "sleep", sleep_mock)
    monkeypatch.setattr("executor.http.signature_runtime.wait_for_runtime_signer_ready", AsyncMock(return_value=False))

    result = await fetch_comment_page(page, "7635267329407913230", count=20)

    assert result is None
    meta = getattr(page, "_comment_list_http_meta", {})
    assert meta.get("attempts") == 1
    assert meta.get("reason") == "signer_not_ready"
    assert sleep_mock.call_count == 0


async def test_resolve_request_signature_delegates_digg_to_browser_fetch():
    """resolve_request_signature delegates /api/comment/digg to browser_fetch when page is provided."""
    page = SimpleNamespace(url=URL)
    profile = RequestProfile.like_profile()

    params, url, bundle = await resolve_request_signature(
        page=page,
        profile=profile,
        dynamic_params={"aweme_id": "123", "cid": "456", "digg_type": "1"},
        cookies={"sessionid": "abc"},
        runtime_profile=None,
        allow_cookie_fallback=False,
    )

    assert bundle.mode == "browser_fetch"
    assert bundle.source == "window.fetch"
    assert "signature_delegated_to_page_interceptor" in bundle.notes


async def test_background_task_navigation_exception_suppressed():
    """Consuming task.exception() via done_callback suppresses unretrieved exception error."""
    loop = asyncio.get_running_loop()

    error_logged = False
    original_handler = loop.get_exception_handler()

    def exception_handler(loop, context):
        nonlocal error_logged
        error_logged = True

    loop.set_exception_handler(exception_handler)

    async def nav_failing_coroutine():
        raise RuntimeError("Execution context was destroyed, most likely because of a navigation.")

    task = asyncio.create_task(nav_failing_coroutine())

    def _consume_error(t: asyncio.Task) -> None:
        try:
            t.exception()
        except (asyncio.CancelledError, Exception):
            pass

    task.add_done_callback(_consume_error)

    await asyncio.sleep(0.05)

    assert task.done()
    assert not error_logged

    loop.set_exception_handler(original_handler)


def test_resolve_request_geo_prioritizes_ssr_region_and_timezone_over_ru_locale(monkeypatch):
    """When USE_PROXY=False and browser_language='ru-RU', region must NOT default to RU on Europe/Kyiv."""
    monkeypatch.setattr("executor.http.request_profile.get_settings", lambda: SimpleNamespace(USE_PROXY=False))
    monkeypatch.setattr("services.dedicated_proxy_service.get_current_dedicated_proxy", lambda: None)

    # Case 1: Ukrainian timezone with ru-RU language -> region must be UA, never RU
    geo = resolve_request_geo(
        runtime_profile={
            "browser_language": "ru-RU",
            "timezone": "Europe/Kyiv",
            "proxy_in_use": False,
        }
    )
    assert geo.get("region") == "UA"
    assert geo.get("priority_region") == "UA"

    # Case 2: ssr_region explicitly provided -> ssr_region wins
    geo_ssr = resolve_request_geo(
        runtime_profile={
            "browser_language": "ru-RU",
            "timezone": "Europe/Kyiv",
            "ssr_region": "PL",
            "proxy_in_use": False,
        }
    )
    assert geo_ssr.get("region") == "PL"

    # Case 3: Russian timezone with ru-RU language -> can be RU
    geo_ru = resolve_request_geo(
        runtime_profile={
            "browser_language": "ru-RU",
            "timezone": "Europe/Moscow",
            "proxy_in_use": False,
        }
    )
    assert geo_ru.get("region") == "RU"


def test_engagement_engine_has_top_level_os_import():
    """Verify that os is imported in engagement_engine to avoid NameError."""
    import executor.engagement_engine as ee
    assert hasattr(ee, "os")
    assert ee.os is not None
