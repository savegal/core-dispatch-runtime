import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

import pytest
from sqlalchemy.orm import Session

from db.models import Account
from executor import browser_pool, cookie_health, engagement_engine, user_schema
from executor.http.request_profile import RequestProfile, generate_tiktok_verify_fp


pytestmark = [pytest.mark.unit, pytest.mark.asyncio]


async def test_cookie_health_retires_definitive_passport_failure_immediately(monkeypatch):
    """When passport response explicitly fails auth, account is retired immediately on #1/1."""
    db = Mock(spec=Session)
    account = Account(id=9999, validation_fail_count=0)
    
    # Mock bump to return 1
    monkeypatch.setattr(cookie_health, "_bump_validation_fail_count", Mock(return_value=1))
    reauth_mock = Mock()
    monkeypatch.setattr(cookie_health, "mark_needs_reauth", reauth_mock)
    soft_mock = Mock()
    monkeypatch.setattr(cookie_health, "mark_soft_error", soft_mock)

    retired = cookie_health.register_dead_session(
        db,
        account,
        reason="http_preflight:Passport response did not confirm authentication",
        definitive=True,
    )

    assert retired is True
    assert reauth_mock.call_count == 1
    assert soft_mock.call_count == 0


def test_generate_tiktok_verify_fp_format():
    """generate_tiktok_verify_fp returns valid format verify_<base36>_<random>."""
    vfp = generate_tiktok_verify_fp()
    assert vfp.startswith("verify_")
    parts = vfp.split("_")
    assert len(parts) == 3
    assert len(parts[1]) > 0
    assert len(parts[2]) == 32


def test_request_profile_auto_populates_verify_fp():
    """RequestProfile automatically populates verifyFp if missing from params and cookies."""
    profile = RequestProfile.comment_list_profile()
    params = profile.build_query_params({"aweme_id": "123456789"})
    assert "verifyFp" in params
    assert params["verifyFp"].startswith("verify_")


async def test_warmup_generates_verify_fp_and_injects_cookie(monkeypatch):
    """warmup_page_for_http ensures verifyFp is generated and added to cookies."""
    URL = "https://www.tiktok.com/@demo/video/7635267329407913230"
    context = SimpleNamespace(
        cookies=AsyncMock(return_value=[
            {"name": "sessionid", "value": "sid123", "domain": ".tiktok.com"},
            {"name": "ttwid", "value": "wid123", "domain": ".tiktok.com"},
            {"name": "tt_csrf_token", "value": "csrf123", "domain": ".tiktok.com"},
            {"name": "msToken", "value": "native-token", "domain": ".tiktok.com"},
        ]),
        add_cookies=AsyncMock(),
    )
    page = SimpleNamespace(
        url=URL,
        goto=AsyncMock(),
        wait_for_load_state=AsyncMock(),
        evaluate=AsyncMock(return_value={
            "current_url": URL,
            "navigation_url": URL,
            "age_ms": 3500,
            "dom_ready_ms": 500,
            "device_id": "71234567890",
            "odin_id": "odin_val",
            "web_id_last_time": "1710000000",
            "ssr_region": "UA",
            "verify_fp": None,
        }),
        context=context,
        on=Mock(),
        remove_listener=Mock(),
    )

    monkeypatch.setattr(engagement_engine.asyncio, "sleep", AsyncMock())
    monkeypatch.setattr(engagement_engine, "wait_for_runtime_signer_ready", AsyncMock(return_value=True))
    monkeypatch.setattr("executor.http.comment_discovery.fetch_comment_page", AsyncMock(return_value={"comments": []}))

    cookies_dict, valid, info = await engagement_engine.warmup_page_for_http(
        page,
        URL,
        correlation_id="test_verify_fp_injection",
        max_retries=1,
    )

    assert valid is True
    assert "verifyFp" in cookies_dict
    assert cookies_dict["verifyFp"].startswith("verify_")
    assert context.add_cookies.call_count >= 1
    call_args = context.add_cookies.call_args[0][0]
    assert any(c.get("name") == "s_v_web_id" for c in call_args)


async def test_browser_pool_close_all_preserves_playwright_by_default():
    """BrowserPool.close_all preserves self._playwright when stop_playwright=False."""
    pool = browser_pool.BrowserPool()
    fake_playwright = SimpleNamespace(stop=AsyncMock())
    pool._playwright = fake_playwright

    await pool.close_all(stop_playwright=False)

    # Playwright remains alive
    assert pool._playwright is fake_playwright
    assert fake_playwright.stop.call_count == 0

    # Explicit stop_playwright=True stops it
    await pool.close_all(stop_playwright=True)
    assert pool._playwright is None
    assert fake_playwright.stop.call_count == 1
