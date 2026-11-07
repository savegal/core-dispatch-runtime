"""Residential account isolation, bounded probes, and API assignment regressions."""
import hashlib
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest
from pydantic import ValidationError
from sqlalchemy.orm import Session

from api import accounts as account_api, proxies as proxy_api
from db.models import Account, Proxy
from services import proxy_assignment_service as assignment, residential_proxy_service as residential
from services.proxy_normalizer import ProxyRoute, ProxyRouteError
from tests.conftest import async_db_session

pytestmark = pytest.mark.unit
RAW = "http://user-session-{session}:password@gateway.example:8080"


@pytest.mark.parametrize("legacy_flags", [
    {}, {"reuse_existing": True}, {"allow_reuse_within_batch": True}, {"test_shared_residential": True},
])
def test_legacy_flags_cannot_share_a_fixed_residential_route(db_session, legacy_flags):
    db_session.add(Proxy(proxy="http://fixed.example:8080", is_mobile="N"))
    db_session.add_all([Account(label=f"account-{i}", cookies="sessionid=fixture") for i in range(3)])
    db_session.commit()
    result = assignment.assign_proxies_to_accounts(db_session, use_geo_matching=False, **legacy_flags)
    assert result["assigned"] == 1 and result["not_enough_proxies"]
    assert db_session.query(Account).filter(Account.proxy.isnot(None)).count() == 1


def test_gateway_sessions_are_distinct_and_bindings_survive_rebalance(db_session, monkeypatch):
    db_session.add(Proxy(proxy=RAW, is_mobile="N"))
    accounts = [Account(label=f"account-{i}", cookies="sessionid=fixture") for i in range(4)]
    db_session.add_all(accounts)
    db_session.commit()
    result = assignment.assign_proxies_to_accounts(db_session, use_geo_matching=False)
    assert result["assigned"] == 4
    saved = {account.id: account.proxy for account in accounts}
    assert len(set(saved.values())) == 4
    monkeypatch.setattr(assignment, "_is_rebalance_eligible_account", lambda account, now: (True, "healthy", 1))
    monkeypatch.setattr(assignment, "get_bad_proxy_set", lambda _: set())
    # A new unrelated proxy does not redistribute stable identities.
    db_session.add(Proxy(proxy="http://new.example:8080", is_mobile="N"))
    db_session.commit()
    assert assignment.rebalance_working_account_proxies(db_session)["changed"] == 0
    assert saved == {account.id: account.proxy for account in accounts}


def test_two_assignment_sessions_cannot_claim_one_fixed_proxy(db_session):
    db_session.add(Proxy(proxy="http://one.example:8080", is_mobile="N"))
    accounts = [Account(label=f"parallel-{i}") for i in range(2)]
    db_session.add_all(accounts)
    db_session.commit()
    ids = [account.id for account in accounts]

    def assign(account_id):
        with Session(db_session.get_bind()) as db:
            return assignment.assign_proxies_to_accounts(db, account_ids=[account_id], use_geo_matching=False)

    with ThreadPoolExecutor(max_workers=2) as threads:
        results = list(threads.map(assign, ids))
    assert sum(row["assigned"] for row in results) == 1


@pytest.mark.asyncio
async def test_duplicate_exit_ip_is_blocked_before_any_platform_request(monkeypatch):
    probe = AsyncMock(return_value="8.8.8.8")
    monkeypatch.setattr(residential, "_probe_exit_ip", probe)
    first, second = SimpleNamespace(id=1), SimpleNamespace(id=2)
    route = ProxyRoute.parse("http://one.example:8080")
    assert await residential.ensure_residential_exit_ip(first, route) == "8.8.8.8"
    with pytest.raises(ProxyRouteError, match="residential_exit_ip_in_use"):
        await residential.ensure_residential_exit_ip(second, ProxyRoute.parse("http://two.example:8080"))
    assert probe.await_count == 2


@pytest.mark.asyncio
async def test_same_route_is_blocked_without_spending_another_probe(monkeypatch):
    probe = AsyncMock(return_value="8.8.4.4")
    monkeypatch.setattr(residential, "_probe_exit_ip", probe)
    route = ProxyRoute.parse("http://one.example:8080")
    await residential.ensure_residential_exit_ip(SimpleNamespace(id=1), route)
    with pytest.raises(ProxyRouteError, match="residential_route_already_assigned"):
        await residential.ensure_residential_exit_ip(SimpleNamespace(id=2), route)
    probe.assert_awaited_once()


@pytest.mark.asyncio
async def test_cached_ip_is_reused_but_expiry_is_not_extended(monkeypatch, isolated_proxy_leases):
    probe = AsyncMock(return_value="8.8.4.4")
    monkeypatch.setattr(residential, "_probe_exit_ip", probe)
    account, route = SimpleNamespace(id=1), ProxyRoute.parse("http://one.example:8080")
    await residential.ensure_residential_exit_ip(account, route)
    key = f"proxy:residential:checked:1:{hashlib.sha256(route.url.encode()).hexdigest()}"
    isolated_proxy_leases.expire(key, 2)
    await residential.ensure_residential_exit_ip(account, route)
    assert 0 < isolated_proxy_leases.ttl(key) <= 2
    probe.assert_awaited_once()
    isolated_proxy_leases.delete(key)
    await residential.ensure_residential_exit_ip(account, route)
    assert probe.await_count == 2


@pytest.mark.asyncio
async def test_mobile_does_not_use_residential_ip_probe(monkeypatch):
    probe = AsyncMock(side_effect=AssertionError("Mobile rotation owns mobile checks"))
    monkeypatch.setattr(residential, "_probe_exit_ip", probe)
    await residential.ensure_residential_exit_ip(SimpleNamespace(id=1), ProxyRoute.parse("http://mobile.example:8080", is_mobile=True))
    probe.assert_not_awaited()


@pytest.mark.parametrize("item", [
    {"proxy": "http://mobile.example:8080", "is_mobile": "Y"},
    {"proxy": "http://fixed.example:8080", "is_mobile": "N", "rotation_url": "https://rotate.example"},
    {"proxy": "http://user-session-auto:password@gateway.example:8080"},
])
def test_new_import_rejects_ambiguous_proxy_modes(item):
    with pytest.raises(ValidationError):
        proxy_api.ProxyImportItem(**item)


@pytest.mark.asyncio
async def test_account_import_assigns_a_different_session_to_each_account(async_db_session):
    db = async_db_session
    db.add(Proxy(proxy=RAW, is_mobile="N"))
    await db.commit()
    response = await account_api.bulk_import(account_api.BulkImportRequest(accounts=[
        account_api.AccountRecord(label=f"new-{i}", cookies='{"sessionid":"' + str(i) * 32 + '"}')
        for i in range(1, 4)
    ]), db=db)
    assert response.added == 3 and response.assigned_proxies == 3
    from sqlalchemy import select
    rows = (await db.execute(select(Account))).scalars().all()
    assert len({row.proxy for row in rows}) == 3
    assert all("{session}" not in row.proxy for row in rows)
    before = {row.id: row.proxy for row in rows}
    again = await account_api.bulk_import(account_api.BulkImportRequest(accounts=[
        account_api.AccountRecord(label=row.label) for row in rows
    ]), db=db)
    assert again.updated == 3 and again.assigned_proxies == 0
    assert before == {row.id: row.proxy for row in (await db.execute(select(Account))).scalars()}


@pytest.mark.asyncio
async def test_plain_proxy_reimport_preserves_mobile_rotation(async_db_session):
    db = async_db_session
    url = "http://mobile.example:8080"
    db.add(Proxy(proxy=url, is_mobile="Y", rotation_url="https://rotate.example"))
    await db.commit()
    await proxy_api.import_proxies(proxy_api.ProxyImportRequest(proxies=[url]), db=db)
    from sqlalchemy import select
    row = (await db.execute(select(Proxy))).scalar_one()
    assert row.is_mobile == "Y" and row.rotation_url == "https://rotate.example"


@pytest.mark.asyncio
@pytest.mark.parametrize("status,body", [
    (200, b'{"ip":"8.8.8.8"}'), (407, b"auth required"),
    (302, b"redirect"), (200, b"x" * 2048),
])
async def test_egress_probe_has_a_fixed_route_and_bounded_response(monkeypatch, status, body):
    factory = httpx.AsyncClient
    options = []

    def client(**kwargs):
        options.append(dict(kwargs))
        kwargs.pop("proxy")
        return factory(transport=httpx.MockTransport(
            lambda request: httpx.Response(status, content=body, headers={"Location": "https://direct.invalid"})
        ), **kwargs)

    monkeypatch.setattr(residential.httpx, "AsyncClient", client)
    route = ProxyRoute.parse("http://login:password@gateway.example:8080")
    if status == 200 and len(body) < 1024:
        assert await residential._probe_exit_ip(route) == "8.8.8.8"
    else:
        with pytest.raises((httpx.HTTPError, ProxyRouteError)):
            await residential._probe_exit_ip(route)
    assert len(options) == 1
    assert options[0]["proxy"] == route.url
    assert options[0]["trust_env"] is options[0]["follow_redirects"] is False


@pytest.mark.asyncio
async def test_redis_outage_blocks_residential_traffic_before_the_ip_probe(monkeypatch):
    from services import proxy_concurrency_service
    from unittest.mock import Mock

    probe = AsyncMock()
    monkeypatch.setattr(residential, "_probe_exit_ip", probe)
    monkeypatch.setattr(proxy_concurrency_service, "_lease_client", lambda: SimpleNamespace(
        eval=Mock(side_effect=ConnectionError("Redis unavailable")),
    ))
    with pytest.raises(ProxyRouteError, match="residential_coordination_unavailable"):
        await residential.ensure_residential_exit_ip(
            SimpleNamespace(id=1), ProxyRoute.parse("http://gateway.example:8080"),
        )
    probe.assert_not_awaited()


@pytest.mark.asyncio
async def test_manual_rotation_is_available_only_for_mobile_proxies(async_db_session, monkeypatch):
    from services import mobile_proxy_service

    db = async_db_session
    mobile = Proxy(proxy="http://mobile.example:8080", is_mobile="Y", rotation_url="https://rotate.example")
    fixed = Proxy(proxy="http://fixed.example:8080", is_mobile="N")
    db.add_all([mobile, fixed])
    await db.commit()
    mobile_id, fixed_id = mobile.id, fixed.id
    rotate = AsyncMock(return_value="8.8.4.4")
    monkeypatch.setattr(mobile_proxy_service, "ensure_proxy_ip_rotated", rotate)
    monkeypatch.setattr(mobile_proxy_service, "save_proxy_last_ip", AsyncMock())
    result = await proxy_api.rotate_mobile_proxy_endpoint(mobile_id, db)
    assert result == {"proxy_id": mobile_id, "ip": "8.8.4.4", "rotated": True}
    from fastapi import HTTPException
    with pytest.raises(HTTPException) as error:
        await proxy_api.rotate_mobile_proxy_endpoint(fixed_id, db)
    assert error.value.status_code == 409
    rotate.assert_awaited_once()


@pytest.mark.asyncio
async def test_deleting_a_gateway_clears_its_expanded_account_bindings(async_db_session):
    db = async_db_session
    source = Proxy(proxy=RAW, is_mobile="N")
    db.add(source)
    await db.commit()
    source_id = source.id
    await account_api.bulk_import(account_api.BulkImportRequest(accounts=[
        account_api.AccountRecord(label=f"delete-{i}") for i in range(2)
    ]), db=db)
    response = await proxy_api.delete_proxies(proxy_api.ProxyDeleteRequest(ids=[source_id]), db=db)
    assert response == {"deleted": 1, "cleared_accounts": 2}
    from sqlalchemy import select
    assert all(row.proxy is None for row in (await db.execute(select(Account))).scalars())


@pytest.mark.asyncio
async def test_http_6x1_uses_six_residential_accounts_without_mobile_rotation(db_session, monkeypatch):
    from contextlib import contextmanager
    from db import database
    from executor import browser_pool, user_schema as schema
    from unittest.mock import Mock
    from tests.unit.test_parallel_6x1_mode import _configure_parallel_6x1

    _configure_parallel_6x1(monkeypatch, schema)
    monkeypatch.setattr(schema._settings, "TIKTOK_NETWORK_FIRST", True)
    monkeypatch.setattr(schema._settings, "TIKTOK_NETWORK_IMPL_READY", True)
    proxies = [Proxy(proxy=f"http://res-{i}.example:8080", is_mobile="N") for i in range(6)]
    db_session.add_all(proxies)
    db_session.commit()
    accounts = [SimpleNamespace(id=i + 1, proxy=proxy.proxy) for i, proxy in enumerate(proxies)]
    selected = set()

    @contextmanager
    def context():
        with Session(db_session.get_bind()) as db:
            yield db

    def select(db, **kwargs):
        for account in accounts:
            if account.id not in selected and account.proxy not in kwargs.get("exclude_proxies", set()):
                selected.add(account.id)
                return account
        return None

    async def publish(db, account, url, texts, **kwargs):
        return [schema.PublishedMain(
            text=texts[0], comment_id=str(7700000000000000000 + account.id),
            network_confirmed=True, local_confirmed=False, method="http_user_schema",
            account_id=account.id, tiktok_username=f"fixture{account.id}",
        )], None

    rotate = AsyncMock(side_effect=AssertionError("Residential routes never rotate"))
    monkeypatch.setattr(database, "get_sync_db_context", context)
    monkeypatch.setattr(schema, "get_free_account_for_main_comment", select)
    monkeypatch.setattr(schema, "_try_main_account", publish)
    monkeypatch.setattr(schema, "ensure_proxy_ip_rotated", rotate)
    monkeypatch.setattr(schema, "release_main_account_reservation", Mock())
    monkeypatch.setattr(schema, "extend_main_account_reservation", Mock(return_value=True))
    monkeypatch.setattr(browser_pool, "ensure_thread_browser_pool_capacity", Mock())
    monkeypatch.setattr(browser_pool, "close_browser_pool", AsyncMock())
    monkeypatch.setattr(schema.asyncio, "sleep", AsyncMock())

    result = await schema.execute_user_schema_parallel_6x1(
        None, SimpleNamespace(settings={"replies": [], "reply_count": 0}),
        "https://www.tiktok.com/@fixture/video/7700000000000000001",
        ["fixture first", "fixture second", "fixture third"], 101, boost_enabled=False, boost_quantity=0,
    )
    assert len(result.published_mains) == 6
    assert len({main.account_id for main in result.published_mains}) == 6
    assert result.to_dict()["status"] == "success"
    rotate.assert_not_awaited()


def test_legacy_mobile_url_is_recognized_after_account_url_normalization(db_session, monkeypatch):
    from config.settings import get_settings
    from executor.user_schema import _proxy_is_rotatable_mobile_for_main
    from services.proxy_normalizer import account_proxy_route
    monkeypatch.setattr(get_settings(), "USE_PROXY", True)
    source = Proxy(proxy="gateway.example:8080:login:password", is_mobile="Y", rotation_url="https://rotate.example")
    account = Account(label="legacy-mobile", proxy="http://login:password@gateway.example:8080")
    db_session.add_all([source, account])
    db_session.commit()
    assert account_proxy_route(account).is_mobile
    assert _proxy_is_rotatable_mobile_for_main(db_session, account.proxy)


def test_assignment_refreshes_owners_loaded_before_another_session_committed(db_session):
    db_session.add(Proxy(proxy="http://fixed.example:8080", is_mobile="N"))
    first, second = Account(label="stale-first"), Account(label="stale-second")
    db_session.add_all([first, second])
    db_session.commit()
    assert first.proxy is None
    with Session(db_session.get_bind()) as other:
        assert assignment.assign_proxies_to_accounts(other, account_ids=[first.id])["assigned"] == 1
    result = assignment.assign_proxies_to_accounts(db_session, account_ids=[second.id])
    assert result["assigned"] == 0 and result["not_enough_proxies"]
    assert second.proxy is None


def test_http_session_routes_are_not_grouped_by_the_gateway_host(monkeypatch):
    from config.settings import get_settings
    from services.account_service import _parallel_proxy_diversify_by_source_host
    settings = get_settings()
    monkeypatch.setattr(settings, "TIKTOK_NETWORK_FIRST", True)
    monkeypatch.setattr(settings, "TIKTOK_NETWORK_IMPL_READY", True)
    monkeypatch.setattr(settings, "PARALLEL_PROXY_DIVERSIFY_BY_SOURCE_HOST", True)
    assert _parallel_proxy_diversify_by_source_host() is False


@pytest.mark.asyncio
async def test_mobile_rotation_uses_the_bot_api_entry_point(monkeypatch):
    from bot.services.api_client import APIClient
    client = APIClient()
    request = AsyncMock(return_value={"proxy_id": 7, "ip": "8.8.8.8", "rotated": True})
    monkeypatch.setattr(client, "_request", request)
    try:
        result = await client.rotate_mobile_proxy(7)
        assert result["rotated"]
        request.assert_awaited_once_with("POST", "/api/proxies/7/rotate", timeout=240, retry_safe=False)
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_mobile_rotation_button_reports_a_verified_ip(monkeypatch):
    from bot.handlers import admin
    edit = AsyncMock()
    rotate = AsyncMock(return_value={"ip": "8.8.8.8", "rotated": True})
    monkeypatch.setattr(admin, "_safe_edit_callback_text", edit)
    monkeypatch.setattr(admin.api_client, "rotate_mobile_proxy", rotate)
    callback = SimpleNamespace(data="admin:proxies:rotate:7", answer=AsyncMock())
    await admin.rotate_mobile_proxy(callback)
    rotate.assert_awaited_once_with(7)
    assert "8.8.8.8" in edit.await_args_list[-1].args[1]


def test_explicit_binding_refreshes_an_owner_cached_before_another_commit(db_session):
    from services.proxy_assignment_service import bind_account_proxy
    source = Proxy(proxy="http://fixed-owner.example:8080", is_mobile="N")
    first, second = Account(label="explicit-stale-first"), Account(label="explicit-stale-second")
    db_session.add_all([source, first, second])
    db_session.commit()
    assert first.proxy is None
    with Session(db_session.get_bind()) as other:
        bind_account_proxy(other, other.get(Account, first.id), source.proxy)
        other.commit()
    with pytest.raises(ProxyRouteError, match="residential_route_already_assigned"):
        bind_account_proxy(db_session, second, source.proxy)


def test_explicit_binding_refreshes_an_account_lease_before_changing_its_route(db_session):
    from datetime import datetime, timedelta, timezone
    from services.proxy_assignment_service import bind_account_proxy
    source = Proxy(proxy="http://lease-aware.example:8080", is_mobile="N")
    account = Account(label="explicit-active-account")
    db_session.add_all([source, account])
    db_session.commit()
    assert account.assignment_lease_until is None
    with Session(db_session.get_bind()) as other:
        other.get(Account, account.id).assignment_lease_until = datetime.now(timezone.utc) + timedelta(minutes=5)
        other.commit()
    with pytest.raises(ProxyRouteError, match="account_route_in_use"):
        bind_account_proxy(db_session, account, source.proxy)


@pytest.mark.asyncio
async def test_clearing_an_active_account_proxy_obeys_the_assignment_lease(async_db_session):
    import inspect
    from datetime import datetime, timedelta, timezone
    from fastapi import HTTPException
    from starlette.requests import Request
    db = async_db_session
    account = Account(label="active-api-clear", proxy="http://busy.example:8080",
                      assignment_lease_until=datetime.now(timezone.utc) + timedelta(minutes=5))
    db.add(account)
    await db.commit()
    with pytest.raises(HTTPException) as error:
        await inspect.unwrap(account_api.update_account)(
            request=Request({"type": "http", "headers": []}), account_id=account.id,
            update_data=account_api.UpdateAccountRequest(proxy=""), db=db,
        )
    assert error.value.status_code == 409
    await db.refresh(account)
    assert account.proxy == "http://busy.example:8080"


@pytest.mark.asyncio
async def test_one_failed_session_does_not_clear_every_account_on_the_gateway(async_db_session, monkeypatch):
    db = async_db_session
    source = Proxy(proxy=RAW, is_mobile="N")
    db.add(source)
    await db.commit()
    source_id = source.id
    await account_api.bulk_import(account_api.BulkImportRequest(accounts=[
        account_api.AccountRecord(label=f"session-health-{i}") for i in range(2)
    ]), db=db)
    monkeypatch.setattr(proxy_api, "check_proxy_health_async", AsyncMock(return_value=(False, "bad", "HTTP 407", None)))
    result = await proxy_api.validate_proxies(
        proxy_api.ProxyValidateRequest(ids=[source_id], reassign_bad_proxies=True), db=db,
    )
    assert result.cleared_accounts == 0
    assert result.bad == 0 and result.unknown == 1
    from sqlalchemy import select
    assert all(account.proxy for account in (await db.execute(select(Account))).scalars())


def test_residential_gateway_still_has_capacity_after_assigning_sessions(db_session):
    db_session.add(Proxy(proxy=RAW, is_mobile="N"))
    db_session.add_all([Account(label=f"capacity-{i}") for i in range(3)])
    db_session.commit()
    result = assignment.assign_proxies_to_accounts(db_session, use_geo_matching=False)
    assert result["assigned"] == 3
    assert result["free_proxies_before"] == result["free_proxies_after"] == 1


def test_strict_geo_matching_does_not_assign_a_foreign_proxy(db_session, monkeypatch):
    monkeypatch.setattr(assignment.settings, "PROXY_GEO_FALLBACK", False)
    db_session.add(Proxy(proxy="http://us.example:8080", is_mobile="N", country_code="US"))
    account = Account(label="strict-geo", country_code="DE")
    db_session.add(account)
    db_session.commit()
    result = assignment.assign_proxies_to_accounts(db_session, use_geo_matching=True)
    assert result["assigned"] == 0 and account.proxy is None


@pytest.mark.asyncio
async def test_forced_mobile_rotation_does_not_stop_at_an_earlier_ip_change(monkeypatch):
    from services import mobile_proxy_service as mobile
    monkeypatch.setattr(mobile, "get_current_ip", AsyncMock(return_value="8.8.8.2"))
    rotate = AsyncMock(return_value=(True, "8.8.8.3"))
    monkeypatch.setattr(mobile, "_rotate_proxy_ip", rotate)
    monkeypatch.setattr(mobile, "_mark_proxy_rotated_now", AsyncMock())
    proxy = SimpleNamespace(id=123, proxy="http://mobile.example:8080", is_mobile="Y", rotation_url="https://rotate.example")
    result = await mobile.ensure_proxy_ip_rotated(proxy, None, previous_ip="8.8.8.1", force_rotation=True)
    assert result == "8.8.8.3"
    rotate.assert_awaited_once()


@pytest.mark.asyncio
async def test_active_browser_keeps_its_exit_ip_claim_while_route_lease_is_renewed(monkeypatch, isolated_proxy_leases):
    import asyncio
    from services import proxy_concurrency_service as concurrency
    monkeypatch.setattr(concurrency, "_LEASE_TTL", 2)
    monkeypatch.setattr(residential, "_probe_exit_ip", AsyncMock(return_value="8.8.4.4"))
    route = ProxyRoute.parse("http://long-running.example:8080")
    async with concurrency.proxy_operation(route.url) as lease:
        await residential.ensure_residential_exit_ip(SimpleNamespace(id=1), route, lease=lease)
        isolated_proxy_leases.expire("proxy:residential:ip:8.8.4.4", 2)
        await asyncio.sleep(0.9)
        assert isolated_proxy_leases.ttl("proxy:residential:ip:8.8.4.4") > 2
        with pytest.raises(ProxyRouteError, match="residential_exit_ip_in_use"):
            await residential.ensure_residential_exit_ip(
                SimpleNamespace(id=2), ProxyRoute.parse("http://another-route.example:8080"),
            )


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ["timeout", "server_error"])
async def test_rotation_transport_failure_does_not_send_another_post(failure):
    from bot.services.api_client import APIClient, APIRequestError
    calls = []

    def response(request):
        calls.append(request)
        if failure == "timeout":
            raise httpx.ReadTimeout("Response was lost after the request", request=request)
        return httpx.Response(503, json={"detail": "Rotation result is unavailable"})

    client = APIClient()
    await client._client.aclose()
    client._client = httpx.AsyncClient(transport=httpx.MockTransport(response))
    try:
        with pytest.raises(APIRequestError):
            await client.rotate_mobile_proxy(7)
    finally:
        await client.close()
    assert len(calls) == 1 and calls[0].method == "POST"


@pytest.mark.asyncio
async def test_deleting_a_gateway_with_an_active_account_rolls_back_the_whole_change(async_db_session):
    from datetime import datetime, timedelta, timezone
    from fastapi import HTTPException
    from sqlalchemy import select
    db = async_db_session
    source = Proxy(proxy=RAW, is_mobile="N")
    db.add(source)
    await db.commit()
    source_id = source.id
    await account_api.bulk_import(account_api.BulkImportRequest(accounts=[
        account_api.AccountRecord(label="idle-before-active"),
        account_api.AccountRecord(label="busy-after-idle"),
    ]), db=db)
    rows = (await db.execute(select(Account).order_by(Account.id))).scalars().all()
    rows[1].assignment_lease_until = datetime.now(timezone.utc) + timedelta(minutes=5)
    await db.commit()
    with pytest.raises(HTTPException) as error:
        await proxy_api.delete_proxies(proxy_api.ProxyDeleteRequest(ids=[source_id]), db=db)
    assert error.value.status_code == 409
    assert (await db.execute(select(Proxy.id))).scalar_one() == source_id
    assert all(row.proxy for row in (await db.execute(select(Account))).scalars())


@pytest.mark.asyncio
async def test_delete_all_keeps_busy_account_routes_and_proxy_rows(async_db_session):
    from datetime import datetime, timedelta, timezone
    from fastapi import HTTPException
    from sqlalchemy import select
    db = async_db_session
    source = Proxy(proxy="http://delete-all-busy.example:8080", is_mobile="N")
    db.add(source)
    db.add(Account(label="delete-all-busy", proxy=source.proxy,
                   assignment_lease_until=datetime.now(timezone.utc) + timedelta(minutes=5)))
    await db.commit()
    with pytest.raises(HTTPException) as error:
        await proxy_api.delete_all_proxies(proxy_api.ProxyDeleteAllRequest(), db=db)
    assert error.value.status_code == 409
    assert len((await db.execute(select(Proxy))).scalars().all()) == 1
    assert (await db.execute(select(Account.proxy))).scalar_one() is not None


@pytest.mark.asyncio
async def test_lost_exit_ip_ownership_cancels_the_active_operation(monkeypatch, isolated_proxy_leases):
    import asyncio
    from services import proxy_concurrency_service as concurrency
    monkeypatch.setattr(concurrency, "_LEASE_TTL", 2)
    monkeypatch.setattr(residential, "_probe_exit_ip", AsyncMock(return_value="8.8.4.4"))
    ready = asyncio.Event()

    async def operation():
        route = ProxyRoute.parse("http://claim-loss.example:8080")
        async with concurrency.proxy_operation(route.url) as lease:
            await residential.ensure_residential_exit_ip(SimpleNamespace(id=1), route, lease=lease)
            ready.set()
            await asyncio.Event().wait()

    task = asyncio.create_task(operation())
    try:
        await asyncio.wait_for(ready.wait(), timeout=2)
        isolated_proxy_leases.set("proxy:residential:ip:8.8.4.4", "2", ex=600)
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(task, timeout=3)
        assert isolated_proxy_leases.get("proxy:residential:ip:8.8.4.4") == "2"
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)


@pytest.mark.asyncio
async def test_account_details_can_be_opened_for_proxy_configuration(async_db_session):
    import inspect
    from starlette.requests import Request
    db = async_db_session
    account = Account(label="details-proxy-account", proxy="http://details.example:8080")
    db.add(account)
    await db.commit()
    response = await inspect.unwrap(account_api.get_account_details)(
        request=Request({"type": "http", "headers": []}), account_id=account.id, db=db,
    )
    assert response["proxy"] == account.proxy
    assert response["last_reply_at"] is None
