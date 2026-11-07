from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from executor import engagement_engine as engine
from executor.http import request_profile, signature_runtime
from executor.http.playwright_http_client import PlaywrightHttpClient
from executor.http.request_evidence import has_request_signature
from executor.http.request_profile import RequestProfile
from executor.http.retry_policy import RetryPolicy
from tests.unit.test_http_signature_runtime import _CaptureRequestContext, _DummyResponse, _SignaturePage
from tests.unit.test_http_warmup_recovery import warmup as warmup, URL


@pytest.mark.parametrize("factory", [RequestProfile.like_profile, RequestProfile.publish_main_profile,
                                    RequestProfile.publish_reply_profile, RequestProfile.comment_list_profile])
@pytest.mark.parametrize("cookies", [{}, {"tt_csrf_token": "fresh-csrf"}])
def test_browser_headers_match_previous_filtered_headers_without_environment_work(monkeypatch, factory, cookies):
    profile = factory()
    runtime = {"proxy_in_use": True, "browser_language": "de-DE", "region": "DE", "timezone": "Europe/Berlin"}
    previous = profile.build_headers(cookies, runtime_profile=runtime)
    previous = {key.lower(): value for key, value in previous.items()
                if key.lower() in {"accept", "tt-csrf-token", "content-type"}}
    for name in ("resolve_request_geo", "_format_sec_ch_ua", "_format_sec_ch_platform"):
        monkeypatch.setattr(request_profile, name, Mock(side_effect=AssertionError("Unnecessary header environment work")))
    assert profile.build_browser_headers(cookies) == previous


@pytest.mark.asyncio
async def test_page_transport_never_builds_discarded_headers(monkeypatch):
    profile = RequestProfile.publish_main_profile()
    full_headers = Mock(side_effect=AssertionError("Browser must use its light header builder"))
    monkeypatch.setattr(profile, "build_headers", full_headers)
    page = _SignaturePage(None)
    page.url = URL
    page.context.cookies.return_value = [{"name": "tt_csrf_token", "value": "live", "domain": ".tiktok.com"}]
    context = SimpleNamespace(fetch=AsyncMock(side_effect=AssertionError("Native fetch required")))
    client = PlaywrightHttpClient(context, page=page, retry_policy=RetryPolicy(max_retries=0))
    success, _, error, _ = await client._execute_request(profile, {"aweme_id": "7635267329407913230", "text": "fixture"})
    assert success, error
    assert page.last_request["headers"]["tt-csrf-token"] == "live"
    assert page.last_request["headers"]["content-type"] == "application/x-www-form-urlencoded; charset=UTF-8"
    full_headers.assert_not_called()


@pytest.mark.asyncio
async def test_request_context_transport_keeps_full_headers(monkeypatch):
    profile = RequestProfile.comment_list_profile()  # Context-only transport remains available for reads.
    monkeypatch.setattr(profile, "build_browser_headers", Mock(side_effect=AssertionError("Context transport needs full headers")))
    context = _CaptureRequestContext(_DummyResponse(200, '{"status_code":0,"comment":{"cid":"7641761192486195000"}}'))
    client = PlaywrightHttpClient(context, retry_policy=RetryPolicy(max_retries=0))
    success, _, error, _ = await client._execute_request(
        profile, {"aweme_id": "7635267329407913230", "text": "fixture"},
        user_agent="Mozilla/5.0 Chrome/146.0.0.0", cookies={"tt_csrf_token": "csrf"},
        runtime_profile_override={"proxy_in_use": True, "region": "DE", "browser_language": "de-DE", "timezone": "Europe/Berlin"},
    )
    assert success, error
    assert context.last_headers["user-agent"] == "Mozilla/5.0 Chrome/146.0.0.0"
    assert context.last_headers["accept-language"].startswith("de-DE")
    assert context.last_headers["tt-csrf-token"] == "csrf"


@pytest.mark.asyncio
@pytest.mark.parametrize("navigation,expected_wait", [
    ("https://vt.tiktok.com/local-fixture/", 2000),
    ("https://vm.tiktok.com/local-fixture/", 2000),
    ("http://vt.tiktok.com/local-fixture/", 2000),
    ("https://vt.tiktok.com.attacker.invalid/local-fixture/", 2000),
    ("https://example.invalid/local-fixture/", 2000),
    (URL.replace("7635267329407913230", "1111111111111111111"), 2000),
])
async def test_unproven_shortlink_document_keeps_maturity_guard(warmup, navigation, expected_wait):
    page, comments, sleeps = warmup
    page.evaluate.return_value = {"current_url": URL, "navigation_url": navigation, "age_ms": 100000, "dom_ready_ms": 500}
    _, valid, info = await engine.warmup_page_for_http(page, URL, max_retries=1)
    assert valid and info["minimum_age_wait_ms"] == expected_wait
    assert round(sum(sleeps) * 1000) == expected_wait
    comments.assert_not_awaited()
    page.goto.assert_not_awaited()


@pytest.mark.asyncio
async def test_unready_dom_does_not_use_shortlink_fast_path(warmup):
    page, _, sleeps = warmup
    page.wait_for_load_state.side_effect = TimeoutError("DOM fixture pending")
    page.evaluate.return_value = {"current_url": URL, "navigation_url": "https://vt.tiktok.com/local-fixture/", "age_ms": 100000}
    _, valid, info = await engine.warmup_page_for_http(page, URL, max_retries=1)
    assert valid and info["minimum_age_wait_ms"] == 2000
    assert round(sum(sleeps) * 1000) == 2000


@pytest.mark.asyncio
@pytest.mark.parametrize("factory", [RequestProfile.like_profile, RequestProfile.publish_main_profile,
                                    RequestProfile.publish_reply_profile, RequestProfile.comment_list_profile,
                                    RequestProfile.reply_list_profile])
async def test_native_profiles_never_invoke_url_signing_or_harvest(monkeypatch, factory):
    sign, harvest = AsyncMock(), AsyncMock()
    monkeypatch.setattr(signature_runtime, "generate_runtime_signature_bundle", sign)
    monkeypatch.setattr(signature_runtime, "harvest_signature_from_traffic", harvest)
    _, _, bundle = await signature_runtime.resolve_request_signature(
        page=object(), profile=factory(), dynamic_params={"aweme_id": "7635267329407913230"},
        cookies={}, runtime_profile={}, allow_cookie_fallback=False,
    )
    assert bundle.mode == "browser_fetch"
    sign.assert_not_awaited()
    harvest.assert_not_awaited()


@pytest.mark.asyncio
async def test_generic_profile_still_supports_diagnostic_url_signing(monkeypatch):
    runtime = AsyncMock(return_value=signature_runtime.SignatureBundle(x_bogus="fixture", mode="runtime"))
    monkeypatch.setattr(signature_runtime, "generate_runtime_signature_bundle", runtime)
    _, url, bundle = await signature_runtime.resolve_request_signature(
        page=object(), profile=RequestProfile(endpoint="/api/custom/", method="GET"), dynamic_params={},
        cookies={}, runtime_profile={}, allow_cookie_fallback=False,
    )
    runtime.assert_awaited_once()
    assert "X-Bogus=fixture" in url and bundle.has_any()


def test_gnarly_alone_is_not_accepted_as_read_path_signature():
    assert not has_request_signature({"X-Gnarly": True})
    assert has_request_signature({"X-Bogus": True})
    assert has_request_signature({"a_bogus": True})
