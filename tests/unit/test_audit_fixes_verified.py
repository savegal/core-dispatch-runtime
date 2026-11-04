import pytest
from unittest.mock import AsyncMock, patch, MagicMock
from types import SimpleNamespace

from services.proxy_service import check_proxy_health_async, normalize_proxy_url
from services.proxy_concurrency_service import _get_proxy_key, _get_fail_key
from executor.http.request_profile import resolve_request_geo
from executor.device_profiles import build_device_profile, prepare_device_page, install_device_profile


@pytest.mark.asyncio
@pytest.mark.parametrize("payload", [{}, {"ip": "invalid"}, {"ip": 123}, ValueError("HTML gateway")])
async def test_proxy_ip_probes_reject_invalid_success_body(payload):
    from services.mobile_proxy_service import get_current_ip

    with patch("httpx.AsyncClient") as client_cls:
        response = MagicMock(status_code=200)
        if isinstance(payload, Exception):
            response.json.side_effect = payload
        else:
            response.json.return_value = payload
        client_cls.return_value.__aenter__.return_value.get = AsyncMock(return_value=response)
        ok, status, _, _ = await check_proxy_health_async("http://1.2.3.4:8080")
        assert (ok, status) == (False, "bad")
        assert await get_current_ip("http://1.2.3.4:8080") is None


@pytest.mark.asyncio
async def test_proxy_service_httpx_client_receives_proxy():
    with patch("httpx.AsyncClient") as mock_client_cls:
        mock_client = AsyncMock()
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {"ip": "1.2.3.4"}
        mock_client.get.return_value = mock_resp
        mock_client_cls.return_value.__aenter__.return_value = mock_client

        ok, status, err, speed = await check_proxy_health_async("1.2.3.4:8080", measure_speed=False)

        assert ok is True
        assert status == "active"
        # Verify proxy was passed to AsyncClient constructor, NOT to client.get()
        assert mock_client_cls.call_args.kwargs["proxy"] == "http://1.2.3.4:8080"
        assert mock_client_cls.call_args.kwargs["trust_env"] is False
        assert "proxies" not in mock_client.get.call_args.kwargs


def test_proxy_concurrency_key_normalization():
    key1 = _get_proxy_key("http://user:pass@1.2.3.4:8080")
    key2 = _get_proxy_key("user:pass@1.2.3.4:8080")
    assert key1 == key2

    key3 = _get_proxy_key("http://1.2.3.4:8080")
    key4 = _get_proxy_key("1.2.3.4:8080")
    assert key3 == key4


def test_request_geo_synchronization_on_proxy_ssr_region():
    runtime = {
        "proxy_in_use": True,
        "ssr_region": "DE",
        "browser_language": "ru-RU",
        "timezone": "Europe/Kyiv",
    }
    geo = resolve_request_geo(runtime)
    assert geo["region"] == "DE"
    assert geo["browser_language"] == "de-DE"
    assert geo["app_language"] == "de-DE"
    assert geo["tz_name"] == "Europe/Berlin"


@pytest.mark.asyncio
async def test_prepare_device_page_passes_accept_language_to_cdp():
    context = SimpleNamespace(
        new_cdp_session=AsyncMock(),
        _account_locale="de-DE",
    )
    session = AsyncMock()
    context.new_cdp_session.return_value = session

    profile = build_device_profile(SimpleNamespace(id=1, user_agent=None), {}, "145.0.7632.6")
    profile["override"]["acceptLanguage"] = "de-DE"
    context._account_device_profile = profile

    page = SimpleNamespace(context=context)
    await prepare_device_page(page)

    send_args = session.send.call_args_list[0]
    assert send_args[0][0] == "Emulation.setUserAgentOverride"
    assert send_args[0][1]["acceptLanguage"] == "de-DE,de;q=0.9,en;q=0.8"


def test_browser_pool_use_proxy_flag_ignores_db_proxy(monkeypatch):
    from executor.browser_pool import _resolve_account_proxy_for_pool, settings
    
    account = SimpleNamespace(id=1, proxy="http://old-db-proxy:8080")
    
    # 1. When USE_PROXY is False and no dedicated proxy: ignores db proxy
    monkeypatch.setattr(settings, "USE_PROXY", False)
    assert _resolve_account_proxy_for_pool(account) == ""
    
    # 2. When USE_PROXY is True: returns db proxy
    monkeypatch.setattr(settings, "USE_PROXY", True)
    assert _resolve_account_proxy_for_pool(account) == "http://old-db-proxy:8080"

