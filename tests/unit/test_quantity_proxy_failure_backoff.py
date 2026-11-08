"""Quantity-route failures should be bounded without corrupting publication receipts."""
from unittest.mock import AsyncMock, Mock

import httpx
import pytest

from db.models import PublicationAttempt, QuantityAction
from executor.quantity_engine import (
    _finalize_claim, _proxy_preflight_backoff_seconds, _proxy_preflight_outcome,
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
    "residential_ip_probe_payment_required",
    "proxy_payment_required:HTTP402",
    "proxy_authentication_required",
    "proxy_authentication_required:HTTP407",
    "residential_coordination_unavailable",
])
def test_classifies_only_pre_post_proxy_failures(reason):
    expected = "proxy_payment_required" if "payment_required" in reason else "quantity_proxy_preflight_unavailable"
    assert _proxy_preflight_outcome(reason, {"publication_call_started": False}) == expected
    assert _proxy_preflight_outcome("proxy_route:" + reason, {"publication_call_started": False}) == expected
    assert _proxy_preflight_outcome(reason, {}) is None
    assert _proxy_preflight_outcome(reason, {"publication_call_started": True}) is None
    assert _proxy_preflight_outcome("submitted_unconfirmed:empty_body", {"publication_call_started": False}) is None


def test_backoff_is_bounded_and_does_not_delay_healthy_calls():
    assert [_proxy_preflight_backoff_seconds(i) for i in range(8)] == [
        0.0, 0.0, 0.0, 2.0, 4.0, 8.0, 16.0, 30.0
    ]
    assert _proxy_preflight_backoff_seconds(100) == 30.0


def test_pre_post_network_failure_remains_retryable_and_does_not_consume_dispatch(db_session, sample_task):
    configure_task(db_session, sample_task, mains=1)
    ensure_actions(db_session, sample_task, sample_task.video_urls[0], "main", 1, ["main"])
    row = db_session.query(QuantityAction).one()
    row.dispatches = 1  # An earlier dispatch for this slot must still count.
    db_session.commit()
    token = claim_action(db_session, row.id)
    assert token
    for _ in range(2):
        assert _finalize_claim(
            db_session, sample_task.id, row.id, token, None,
            "residential_ip_probe_timeout", entered=True, completed=True,
            progress={"publication_call_started": False}
        ) == "not_sent"
    db_session.refresh(row)
    assert row.state == "not_sent" and row.token is None
    assert row.dispatches == 1 and row.candidate_count == 1
    attempt = db_session.query(PublicationAttempt).one()
    assert attempt.status == "not_sent" and attempt.wire_count == 0
    assert attempt.action_id == row.id and attempt.error_code == "residential_ip_probe_timeout"
    assert claim_action(db_session, row.id)


@pytest.mark.asyncio
@pytest.mark.parametrize("error,reason", [
    (httpx.ConnectError("connection failed"), "residential_ip_probe_connect_error"),
    (httpx.ConnectTimeout("timed out"), "residential_ip_probe_timeout"),
    (httpx.ProxyError("402 Payment Required"), "residential_ip_probe_payment_required"),
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
    (402, "residential_ip_probe_payment_required"),
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


@pytest.mark.asyncio
@pytest.mark.parametrize("reason,started,attempted,expected_state", [
    ("proxy_route:residential_ip_probe_timeout", False, False, "not_sent"),
    ("submitted_unconfirmed:empty_body", True, True, "uncertain"),
])
async def test_quantity_publisher_maps_proxy_failures_without_replaying_ambiguous_post(
    db_session, sample_task, sample_account, monkeypatch, reason, started, attempted, expected_state
):
    from db.models import Account
    from services import account_service
    from executor import visibility_publisher
    from executor.quantity_engine import publish_action

    configure_task(db_session, sample_task, mains=1)
    ensure_actions(db_session, sample_task, sample_task.video_urls[0], "main", 1, ["main"])
    row = db_session.query(QuantityAction).one()
    account_id = sample_account.id
    monkeypatch.setattr(account_service, "get_free_account_for_main_comment",
                        lambda db, **kwargs: db.get(Account, account_id))

    async def fake_publisher(*args, **kwargs):
        kwargs["progress"]["publication_call_started"] = started
        kwargs["progress"]["publication_attempted"] = attempted
        return None, reason

    monkeypatch.setattr(visibility_publisher, "publish_visibility_http", fake_publisher)
    outcome = await publish_action(db_session.get_bind(), sample_task.id, row.id,
                                   stop_requested=lambda: False)
    assert outcome == ("quantity_proxy_preflight_unavailable" if not started else "uncertain")
    db_session.refresh(row)
    assert row.state == expected_state and row.candidate_count == 1
    assert row.dispatches == (1 if attempted else 0)
    assert (claim_action(db_session, row.id) is not None) is (not attempted)


@pytest.mark.asyncio
async def test_browser_acquire_proxy_failure_reaches_live_report(db_session, sample_task, sample_account, monkeypatch):
    from db.models import Account
    from executor import quantity_engine, user_schema
    from services import account_service
    from services.publication_reports import activity_snapshot, export_report

    configure_task(db_session, sample_task)
    ensure_actions(db_session, sample_task, sample_task.video_urls[0], "main", 1, ["main"])
    row = db_session.query(QuantityAction).one()
    account_id = sample_account.id
    monkeypatch.setattr(account_service, "get_free_account_for_main_comment",
                        lambda db, **kwargs: db.get(Account, account_id))
    monkeypatch.setattr("executor.visibility_publisher.http_first_enabled", lambda settings: True)
    monkeypatch.setattr(user_schema, "_preflight_cookie_http_gate", AsyncMock(return_value=None))
    monkeypatch.setattr(user_schema, "create_page_for_account", AsyncMock(
        side_effect=ProxyRouteError("residential_ip_probe_payment_required")))

    assert await quantity_engine.publish_action(db_session.get_bind(), sample_task.id, row.id,
        stop_requested=lambda: False) == "proxy_payment_required"
    snapshot = activity_snapshot(db_session, sample_task)
    assert snapshot["errors"] == {"proxy_route:residential_ip_probe_payment_required": 1}
    assert snapshot["summary"]["not_sent"] == 1
    assert snapshot["summary"]["dispatched"] == 0
    assert snapshot["summary"]["visible_percent"] is None
    report = export_report(db_session, sample_task, "txt")
    assert "не отправлено: 1" in report
    assert "Причина: proxy_route:residential_ip_probe_payment_required" in report
    csv_report = export_report(db_session, sample_task, "csv")
    assert csv_report.splitlines()[0].endswith(",error_code")
    assert csv_report.splitlines()[1].endswith(",proxy_route:residential_ip_probe_payment_required")


@pytest.mark.asyncio
@pytest.mark.parametrize("first,expected", [
    ("proxy_payment_required", "proxy_payment_required"),
    ("quantity_proxy_preflight_unavailable", "quantity_proxy_preflight_unavailable"),
])
async def test_quantity_stops_selecting_after_consecutive_proxy_failures(
    db_session, sample_task, monkeypatch, first, expected,
):
    from executor import engagement_engine, quantity_engine
    from services.publication_runtime import update_publication_runtime

    configure_task(db_session, sample_task)
    update_publication_runtime(db_session, {"send_workers": 1})
    url = sample_task.video_urls[0]
    monkeypatch.setattr(engagement_engine, "resolve_order_video_url", AsyncMock(
        return_value=(url, "7704740811904828100")))
    monkeypatch.setattr(quantity_engine, "_proxy_preflight_backoff_seconds", lambda streak: 0)
    monkeypatch.setattr(quantity_engine, "PROXY_PREFLIGHT_FAILURE_LIMIT", 3)
    payment = "proxy_payment_required"
    # A result unrelated to the proxy breaks the streak before the second wave.
    publish = AsyncMock(side_effect=[payment, payment, "not_sent", first, payment, payment])
    monkeypatch.setattr(quantity_engine, "publish_action", publish)

    result = await quantity_engine.execute_quantity_order(db_session, sample_task, url, ["main"], sample_task.id)

    assert publish.await_count == 6
    assert result.error == expected
    assert result.to_dict()["quantity_plan"]["complete"] is False


@pytest.mark.asyncio
async def test_quantity_bounds_failed_comment_reads_even_between_passport_refusals(db_session, sample_task, monkeypatch):
    from executor import engagement_engine, quantity_engine
    from services.publication_runtime import update_publication_runtime

    configure_task(db_session, sample_task)
    update_publication_runtime(db_session, {"send_workers": 1})
    url = sample_task.video_urls[0]
    monkeypatch.setattr(engagement_engine, "resolve_order_video_url", AsyncMock(
        return_value=(url, "7704740811904828100")))
    monkeypatch.setattr(quantity_engine, "COMMENT_READINESS_FAILURE_LIMIT", 3)
    unavailable = "quantity_comment_readiness_unavailable"
    publish = AsyncMock(side_effect=[unavailable, "not_sent", unavailable, unavailable])
    monkeypatch.setattr(quantity_engine, "publish_action", publish)

    result = await quantity_engine.execute_quantity_order(db_session, sample_task, url, ["main"], sample_task.id)

    assert publish.await_count == 4
    assert result.error == unavailable


@pytest.mark.asyncio
async def test_quantity_gives_untouched_slots_a_turn_before_retrying_preflight(db_session, sample_task, monkeypatch):
    from sqlalchemy.orm import Session
    from executor import engagement_engine, quantity_engine
    from services.publication_runtime import update_publication_runtime

    configure_task(db_session, sample_task, mains=2)
    update_publication_runtime(db_session, {"send_workers": 1})
    url = sample_task.video_urls[0]
    monkeypatch.setattr(engagement_engine, "resolve_order_video_url", AsyncMock(return_value=(url, "7704740811904828100")))
    monkeypatch.setattr(quantity_engine, "PROXY_PREFLIGHT_FAILURE_LIMIT", 3)
    visited = []

    async def fail_preflight(engine, task_id, identity, **kwargs):
        with Session(engine) as db:
            row = db.get(QuantityAction, identity)
            visited.append(row.slot)
            row.candidate_count += 1
            row.state = "not_sent"
            db.commit()
        return "quantity_proxy_preflight_unavailable"

    monkeypatch.setattr(quantity_engine, "publish_action", fail_preflight)
    result = await quantity_engine.execute_quantity_order(db_session, sample_task, url, ["main"], sample_task.id)
    assert visited == [1, 2, 1]
    assert result.error == "quantity_proxy_preflight_unavailable"


@pytest.mark.asyncio
@pytest.mark.parametrize("reason,age_seconds,excluded", [
    ("http_preflight:inconclusive:passport_auth_unconfirmed", 120, True),
    ("http_auth_unconfirmed:browser_login_state", 120, True),
    ("comment_readiness_unconfirmed:empty_body", 120, True),
    ("proxy_route:residential_ip_probe_timeout", 120, False),
    ("proxy_route:residential_ip_probe_timeout", 30, True),
    ("warmup_runtime_profile_timeout", 30, True),
    ("comment_readiness_unconfirmed:timeout", 30, True),
])
async def test_quantity_skips_rechecking_account_after_stable_refusal(
    db_session, sample_task, sample_account, monkeypatch, reason, age_seconds, excluded,
):
    from datetime import timedelta
    from executor.quantity_engine import publish_action
    from services import account_service
    from services.publication_journal import utcnow

    configure_task(db_session, sample_task, mains=2)
    url = sample_task.video_urls[0]
    ensure_actions(db_session, sample_task, url, "main", 2, ["main"])
    first, second = db_session.query(QuantityAction).order_by(QuantityAction.slot).all()
    db_session.add(PublicationAttempt(id="prior-preflight", task_id=sample_task.id, account_id=sample_account.id,
        video_url=url, kind="main", action_id=first.id, status="not_sent", wire_count=0,
        error_code=reason, text="main", text_hash="fixture", finished_at=utcnow() - timedelta(seconds=age_seconds)))
    db_session.commit()
    selector = Mock(return_value=None)
    monkeypatch.setattr(account_service, "get_free_account_for_main_comment", selector)

    assert await publish_action(db_session.get_bind(), sample_task.id, second.id,
                                stop_requested=lambda: False) == "no_accounts_available"
    assert (sample_account.id in selector.call_args.kwargs["exclude_ids"]) is excluded
