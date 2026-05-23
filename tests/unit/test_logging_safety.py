import logging
from types import SimpleNamespace

import pytest

from config.logging_config import setup_logging
from services import mobile_proxy_service
from services.log_sanitizer import sanitize_url


def test_sanitize_url_redacts_query_secrets_and_credentials():
    raw = "https://user:pass@example.com/reboot?token=secret-token-123&keep=ok"

    safe = sanitize_url(raw)

    assert "secret-token-123" not in safe
    assert "user:pass" not in safe
    assert "token=***REDACTED***" in safe
    assert "keep=ok" in safe


def test_setup_logging_keeps_httpx_request_logs_below_info():
    setup_logging(level="INFO")

    assert logging.getLogger("httpx").getEffectiveLevel() >= logging.WARNING
    assert logging.getLogger("httpcore").getEffectiveLevel() >= logging.WARNING


@pytest.mark.asyncio
async def test_rotate_proxy_ip_logs_redacted_rotation_url(monkeypatch, caplog):
    raw_url = "https://proxy-seller.com/api/proxy/reboot?token=secret-token-123&keep=ok"
    proxy = SimpleNamespace(id=7, rotation_url=raw_url, proxy="http://proxy.example:8080")

    class FakeResponse:
        def raise_for_status(self):
            return None

    class FakeClient:
        def __init__(self, *args, **kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb):
            return False

        async def get(self, url):
            assert url == raw_url
            return FakeResponse()

    async def fake_sleep(*args, **kwargs):
        return None

    async def fake_get_current_ip(*args, **kwargs):
        return "1.2.3.4"

    monkeypatch.setattr(mobile_proxy_service.httpx, "AsyncClient", FakeClient)
    monkeypatch.setattr(mobile_proxy_service.asyncio, "sleep", fake_sleep)
    monkeypatch.setattr(mobile_proxy_service, "get_current_ip", fake_get_current_ip)
    caplog.set_level(logging.INFO, logger="services.mobile_proxy")

    success, ip = await mobile_proxy_service.rotate_proxy_ip(proxy)

    assert success is True
    assert ip == "1.2.3.4"
    assert "secret-token-123" not in caplog.text
    assert "token=***REDACTED***" in caplog.text
