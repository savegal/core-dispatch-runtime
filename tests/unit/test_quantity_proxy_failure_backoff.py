"""Quantity-route failures should be bounded without corrupting publication receipts."""
from unittest.mock import AsyncMock, Mock

import httpx
import pytest

from db.models import QuantityAction
from executor.quantity_engine import (
    _finalize_claim, _proxy_preflight_backoff_seconds, _proxy_preflight_failure,
    claim_action, ensure_actions,
)
from services.proxy_normalizer import ProxyRoute, ProxyRouteError
from services import residential_proxy_service as residential
from tests.unit.test_quantity_redesign_recovery import configure_task


pytestmark = pytest.mark.unit


def mock_successful_redis_claims(monkeypatch):
    # These cases exercise HTTP error mapping, not the Redis lease Lua scripts.
    # A real FakeRedis lease is already covered by test_residential_proxy_policy.
    from types import SimpleNamespace
    monkeypatch.setattr(
        "services.proxy_concurrency_service._lease_client",
        lambda: SimpleNamespace(eval=Mock(return_value=1), get=Mock(return_value=None)),
    )


@pytest.mark.parametrize("reason", [
    "residential_ip_probe_timeout",
    "residential_ip_probe_connect_error",
    "residential_ip_probe_rate_limited",
    "proxy_authentication_required",
    "residential_coordination_unavailable",
])
def test_classifies_only_pre_post_proxy_failures(reason):
    assert _proxy_preflight_failure(reason, {"publication_call_started": False})
    assert _proxy_preflight_failure("proxy_route:" + reason, {"publication_call_started": False})
    assert not _proxy_preflight_failure(reason, {"publication_call_started": True})
    assert not _proxy_preflight_failure("submitted_unconfirmed:empty_body",
                                        {"publication_call_started": False})


def test_backoff_is_bounded_and_does_not_delay_healthy_calls():
    assert [_proxy_preflight_backoff_seconds(i) for i in range(8)] == [
        0.0, 0.0, 0.0, 2.0, 4.0, 8.0, 16.0, 30.0
    ]
    assert _proxy_preflight_backoff_seconds(100) == 30.0


def test_pre_post_network_failure_remains_retryable_and_does_not_consume_dispatch(db_session, sample_task):
    configure_task(db_session, sample_task, mains=1)
    ensure_actions(db_session, sample_task, sample_task.video_urls[0], "main", 1, ["main"])
    row = db_session.query(QuantityAction).one()
    token = claim_action(db_session, row.id)
    assert token
    assert _finalize_claim(
        db_session, sample_task.id, row.id, token, None,
        "residential_ip_probe_timeout", entered=True, completed=True,
        progress={"publication_call_started": False}
    ) == "not_sent"
    db_session.refresh(row)
    assert row.state == "not_sent"
    assert row.dispatches == 0 and row.candidate_count == 1
    assert claim_action(db_session, row.id)


@pytest.mark.asyncio
@pytest.mark.parametrize("error,reason", [
    (httpx.ConnectError("connection failed"), "residential_ip_probe_connect_error"),
    (httpx.ConnectTimeout("timed out"), "residential_ip_probe_timeout"),
    (httpx.ProxyError("407 Proxy Authentication Required"), "proxy_authentication_required"),
    (httpx.ReadError("read failed"), "residential_ip_probe_transport_error"),
])
async def test_probe_classifies_network_errors_without_leaking_proxy_url(
    monkeypatch, isolated_proxy_leases, error, reason
):
    mock_successful_redis_claims(monkeypatch)
    probe = AsyncMock(side_effect=error)
    monkeypatch.setattr(residential, "_probe_exit_ip", probe)
    route = ProxyRoute.parse("http://secret-login:secret-password@gateway.example:8080")
    from types import SimpleNamespace
    with pytest.raises(ProxyRouteError) as exc:
        await residential.ensure_residential_exit_ip(SimpleNamespace(id=9001), route)
    assert str(exc.value) == "proxy_route:" + reason
    assert "secret" not in str(exc.value)
    probe.assert_awaited_once()


@pytest.mark.asyncio
@pytest.mark.parametrize("code,reason", [
    (407, "proxy_authentication_required"),
    (429, "residential_ip_probe_rate_limited"),
    (503, "residential_ip_probe_service_error"),
])
async def test_probe_classifies_http_status(monkeypatch, isolated_proxy_leases, code, reason):
    mock_successful_redis_claims(monkeypatch)
    request = httpx.Request("GET", "https://api.ipify.org")
    response = httpx.Response(code, request=request)
    probe = AsyncMock(side_effect=httpx.HTTPStatusError("status", request=request, response=response))
    monkeypatch.setattr(residential, "_probe_exit_ip", probe)
    from types import SimpleNamespace
    with pytest.raises(ProxyRouteError) as exc:
        await residential.ensure_residential_exit_ip(
            SimpleNamespace(id=9001), ProxyRoute.parse("http://gateway.example:8080")
        )
    assert str(exc.value) == "proxy_route:" + reason
