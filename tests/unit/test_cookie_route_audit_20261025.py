import asyncio
import json
import time
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from executor import browser_pool as pool_module, device_profiles, user_schema as schema
from executor.http import playwright_http_client as transport, request_profile
from services import cookie_validator as validator, cookie_persistence_service as persistence
from tests.unit.test_browser_pool_page_reuse import LaunchProbeBrowser


def export_records():
    return [
        {"name":"sid_guard","value":"a"*32+"%7C1700000000%7C86400","domain":".tiktok.com","expirationDate":2147483647,"hostOnly":True},
        {"name":"sid_guard","value":"b"*32+"%7C1800000000%7C86400","domain":"tiktok.com","expirationDate":2147483647,"hostOnly":True},
        {"name":"msToken","value":"old-export-token","domain":".tiktok.com","expirationDate":2147483647,"hostOnly":True},
        {"name":"tt-target-idc","value":"alisg","domain":".tiktok.com","expirationDate":2147483647},
    ]


def test_imported_root_guard_reaches_web_domain_without_mutating_export():
    records = export_records()
    before = json.dumps(records)
    output = validator.normalize_cookies_for_browser(records)
    guard = next(row for row in output if row["name"] == "sid_guard")
    assert guard["domain"] == ".tiktok.com" and guard["value"].startswith("b"*32)
    assert all(next(row for row in output if row["name"] == key)["value"] == "b"*32 for key in ("sessionid","sid_tt","sessionid_ss"))
    token = next(row for row in output if row["name"] == "msToken")
    assert token["value"] == "old-export-token" and "expires" not in token
    assert json.dumps(records) == before


def test_fresh_browser_tokens_are_not_removed_just_for_export_expiry():
    records = export_records()
    assert any(row["name"] == "msToken" for row in validator.normalize_cookies_for_browser(records, preserve_fresh_tokens=True))


@pytest.mark.parametrize("shape", ["list","map","header","aliases"])
def test_guard_bootstrap_identifies_minimal_import_and_compatibility_aliases(shape):
    values = {"sid_guard":"b"*32+"|1800000000|86400", "msToken":"export"}
    if shape == "list":
        raw = json.dumps(export_records())
    elif shape == "header":
        raw = "; ".join(f"{key}={value}" for key,value in values.items())
    else:
        if shape == "aliases":
            values.update(sessionid="b"*32,sid_tt="b"*32,sessionid_ss="b"*32)
        raw = json.dumps(values)
    assert validator.has_guard_only_auth(raw)
    assert not validator.has_guard_only_auth(json.dumps({**values,"sessionid":"native-different"}))


@pytest.mark.asyncio
@pytest.mark.parametrize("status,category,expected_allow", [("inconclusive",None,True), ("dead",None,False),
                                                          ("inconclusive","proxy_route",False)])
async def test_minimal_passport_bootstrap_is_once_and_definitive_or_route_errors_still_stop(monkeypatch,status,category,expected_allow):
    account = SimpleNamespace(id=96301,cookies=json.dumps(export_records()),cookie_file=None,proxy=None,country_code=None)
    key = schema._http_preflight_cache_key(account)
    for cache in (schema._HTTP_PREFLIGHT_VALID_CACHE,schema._HTTP_PREFLIGHT_BROWSER_FALLBACKS):
        cache.pop(key,None)
    monkeypatch.setattr(schema,"http_first_enabled",lambda _:True)
    result = {"status":status,"definitive":status=="dead","valid":False,
              "error":"proxy_route:missing_proxy" if category else "Passport response did not confirm authentication"}
    if category:
        result["category"] = category
    monkeypatch.setattr(validator,"validate_account_http",AsyncMock(return_value=result))
    soft, dead = Mock(), Mock()
    monkeypatch.setattr(schema,"mark_soft_error",soft)
    monkeypatch.setattr(schema,"_register_dead_session",dead)
    denied = await schema._preflight_cookie_http_gate(Mock(),account,correlation_id="guard-fixture",phase="main_pre_browser")
    assert (denied is None) is expected_allow
    if expected_allow:
        soft.assert_not_called()
        assert account._http_preflight_requires_browser_warmup
        assert await schema._preflight_cookie_http_gate(Mock(),account,correlation_id="guard-fixture",phase="main_pre_browser")
        soft.assert_called_once()
        dead.assert_not_called()
    elif status == "dead":
        dead.assert_called_once()
    else:
        soft.assert_not_called()
        dead.assert_not_called()


@pytest.mark.asyncio
async def test_http_probe_never_commits_or_mutates_orm_country(monkeypatch,db_session):
    from db.models import Account
    account=Account(label="http-pure-probe",cookies=json.dumps({"sessionid":"a"*32}))
    db_session.add(account); db_session.commit()
    commit=Mock(wraps=db_session.commit)
    monkeypatch.setattr(db_session,"commit",commit)
    monkeypatch.setattr(validator.get_settings(),"USE_PROXY",False)
    class Response:
        status=200
        headers={}
        async def __aenter__(self): return self
        async def __aexit__(self,*_): pass
        async def json(self,**_): return {"message":"success","data":{"user_id_str":"123456","region":"DE"}}
    class Session:
        def __init__(self,**_): pass
        async def __aenter__(self): return self
        async def __aexit__(self,*_): pass
        def get(self,*_,**__): return Response()
    monkeypatch.setattr(validator.aiohttp,"ClientSession",Session)
    result=await validator.validate_account_http(account,strict_passport=True)
    assert result["valid"] and result["country_code"]=="DE"
    assert account.country_code is None and db_session.is_active
    commit.assert_not_called()


def test_selected_fp_survives_generator_cache_reset_and_metadata_save_is_not_authentication(db_session,monkeypatch):
    from db.models import Account
    account=Account(label="persisted-fp",cookies=json.dumps(export_records()))
    db_session.add(account); db_session.commit()
    first=validator.prepare_cookies_for_context(account)
    fp=next(row["value"] for row in first if row["name"]=="s_v_web_id")
    assert persistence.save_warmed_cookies_for_account(account,{},db_session)
    assert account.last_validation_ok is None
    assert not getattr(account,"_cookies_warmed_at",None)
    with transport._ACCOUNT_WEB_IDENTITY_LOCK:
        transport._ACCOUNT_WEB_IDENTITY_CACHE.clear(); transport._ACCOUNT_WEB_IDENTITY_SEEDS.clear()
    request_profile._seeded_verify_fp.cache_clear()
    now=time.time()
    monkeypatch.setattr(request_profile.time,"time",lambda:now+3600)
    second=validator.prepare_cookies_for_context(account,parse_fn=lambda raw:[row for row in json.loads(raw) if row.get("name")!="s_v_web_id"])
    assert next(row["value"] for row in second if row["name"]=="s_v_web_id")==fp


@pytest.mark.asyncio
async def test_ads_fp_does_not_replace_or_block_the_web_identity():
    account = SimpleNamespace(id=96303, device_seed="c" * 64, cookies=json.dumps(export_records()))
    transport.cache_web_identity(account.id, {"verify_fp": "verify_web_fixture"}, account=account)
    ads = {"name": "s_v_web_id", "value": "verify_ads_fixture", "domain": "ads.tiktok.com", "path": "/"}
    context = SimpleNamespace(cookies=AsyncMock(return_value=[ads]), add_cookies=AsyncMock())
    cookies, _ = await persistence.synchronize_browser_cookies(context, account)
    assert cookies["s_v_web_id"] == "verify_web_fixture"
    assert ads in cookies.records
    context.add_cookies.assert_awaited_once_with([{
        "name": "s_v_web_id", "value": "verify_web_fixture", "domain": ".tiktok.com",
        "path": "/", "secure": True, "httpOnly": False,
    }])


@pytest.mark.asyncio
async def test_runtime_extraction_does_not_persist_on_every_read(monkeypatch):
    account=SimpleNamespace(id=96302,device_seed="a"*64,cookies="{}")
    context=SimpleNamespace(_pool_account_id=account.id,_pool_account_obj=account)
    page=SimpleNamespace(url="https://www.tiktok.com/@fixture/video/123",context=context,
                         evaluate=AsyncMock(side_effect=lambda *_a,**_k:{"device_id":"7382000000000000001"}))
    save=Mock(side_effect=AssertionError("hot-path persistence"))
    monkeypatch.setattr(persistence,"save_warmed_cookies_for_account",save)
    runtime=await transport._extract_runtime_profile(page)
    assert runtime["device_seed"]==account.device_seed
    save.assert_not_called()
    query=request_profile.RequestProfile.comment_list_profile().build_query_params({},runtime_profile={"device_seed":account.device_seed})
    assert query["verifyFp"]==request_profile.generate_tiktok_verify_fp(account.device_seed)


@pytest.mark.parametrize("idc",["alisg","eu-ttp2","useast5","useast8","us-eastred"])
def test_data_centre_labels_do_not_invent_proxy_geography(idc):
    assert request_profile.resolve_request_geo({"proxy_in_use":True},{"tt-target-idc":idc})=={}
    geo=request_profile.resolve_request_geo({"proxy_in_use":True,"proxy_country_code":"DE"},{"tt-target-idc":idc})
    assert (geo["region"],geo["tz_name"])==("DE","Europe/Berlin")


def test_bound_context_keeps_actual_timezone_after_ssr_region_arrives():
    geo=request_profile.resolve_request_geo({"proxy_in_use":True,"context_geo_bound":True,"ssr_region":"DE",
                                             "browser_language":"en","timezone":"Etc/UTC"})
    assert geo["region"]=="DE" and geo["tz_name"]=="Etc/UTC" and geo["browser_language"]=="en"


def test_explicit_user_agent_is_retained_for_request_context_transport():
    ua="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/146.0.0.0 Safari/537.36"
    assert request_profile.RequestProfile.like_profile().build_headers(user_agent=ua)["user-agent"]==ua


@pytest.fixture
def route_pool(monkeypatch):
    from executor import tiktok_client
    from services import proxy_concurrency_service
    monkeypatch.setattr(pool_module.settings,"USE_PROXY",True)
    monkeypatch.setattr(pool_module.settings,"BROWSER_POOL_SHARED_PROCESS",False)
    monkeypatch.setattr(pool_module.settings,"BROWSER_POOL_CAPACITY_WAIT_SECONDS",0.2)
    monkeypatch.setattr(tiktok_client,"_get_cookies_for_account",lambda _:"[]")
    monkeypatch.setattr(tiktok_client,"parse_cookies",lambda _:[])
    monkeypatch.setattr(proxy_concurrency_service,"acquire_proxy_usage",AsyncMock(side_effect=lambda *_a,**_k:SimpleNamespace(release=AsyncMock())))
    pool=pool_module.BrowserPool(max_browsers=1)
    created=[]
    async def launch(**_):
        browser=LaunchProbeBrowser(); created.append(browser); return browser
    async def stop():
        # The real driver owns and terminates every browser it launched.
        for browser in created:
            await browser.close()
    pool._playwright=SimpleNamespace(chromium=SimpleNamespace(launch=AsyncMock(side_effect=launch)),stop=AsyncMock(side_effect=stop))
    return pool,created


def routed_account(n):
    return SimpleNamespace(id=96400+n,label=f"route-{n}",cookies="[]",proxy=f"http://proxy-{n}.invalid:8080",country_code="US")


@pytest.mark.asyncio
async def test_idle_proxy_contexts_are_evicted_without_exceeding_process_budget(route_pool):
    pool,created=route_pool
    try:
        for n in range(6):
            context,page=await pool.get_page_for_account(routed_account(n))
            assert len(pool._browsers)<=1
            assert sum(not item.closed for item in created)<=1
            await pool.return_page_to_pool(page)
        assert len(created)==6
    finally:
        await pool.close_all(stop_playwright=True)


@pytest.mark.asyncio
async def test_parallel_new_proxy_waits_for_idle_slot_and_never_closes_busy_page(route_pool):
    pool,created=route_pool
    context,page=await pool.get_page_for_account(routed_account(1))
    task=asyncio.create_task(pool.get_page_for_account(routed_account(2)))
    await asyncio.sleep(0.02)
    assert not task.done() and not context._closed and len(created)==1
    await pool.return_page_to_pool(page)
    try:
        new_context,_=await asyncio.wait_for(task,1)
        assert context._closed and new_context is not context and len(created)==2
        assert sum(not item.closed for item in created)==1
    finally:
        await pool.close_all(stop_playwright=True)


@pytest.mark.asyncio
async def test_capacity_timeout_does_not_start_extra_browser_or_penalize_account(route_pool,monkeypatch):
    from services.account_service import mark_soft_error
    pool,created=route_pool
    monkeypatch.setattr(pool_module.settings,"BROWSER_POOL_CAPACITY_WAIT_SECONDS",0.02)
    context,_=await pool.get_page_for_account(routed_account(3))
    try:
        with pytest.raises(pool_module.BrowserPoolCapacityError,match="browser_pool_capacity_busy"):
            await pool.get_page_for_account(routed_account(4))
        assert not context._closed and len(created)==1
        assert not pool._browser_reservations and not pool._browser_inflight_acquires
        db=Mock()
        mark_soft_error(db,1,"runtime:browser_pool_capacity_busy")
        db.query.assert_not_called()
    finally:
        await pool.close_all(stop_playwright=True)


@pytest.mark.asyncio
async def test_proxy_change_on_same_idle_account_does_not_deadlock_capacity(route_pool):
    pool,_=route_pool
    account=routed_account(5)
    old,page=await pool.get_page_for_account(account)
    await pool.return_page_to_pool(page)
    account.proxy="http://changed.invalid:8080"
    try:
        context,_=await asyncio.wait_for(pool.get_page_for_account(account),1)
        assert old._closed and context is not old
    finally:
        await pool.close_all(stop_playwright=True)


@pytest.mark.asyncio
async def test_closing_browser_keeps_its_process_slot_until_close_finishes(route_pool, monkeypatch):
    pool, created = route_pool
    context, page = await pool.get_page_for_account(routed_account(71))
    await pool.return_page_to_pool(page)
    started, release = asyncio.Event(), asyncio.Event()
    async def persist(closing_context, _):
        if closing_context is context:
            started.set()
            await release.wait()
    monkeypatch.setattr(pool, "_persist_context_cookies_before_close", persist)
    closing = asyncio.create_task(pool._close_browser(next(iter(pool._browsers)), "capacity test"))
    await asyncio.wait_for(started.wait(), 1)
    waiter = asyncio.create_task(pool.get_page_for_account(routed_account(72)))
    try:
        await asyncio.sleep(0.02)
        assert not waiter.done() and not created[0].closed and len(created) == 1
        release.set()
        await closing
        await asyncio.wait_for(waiter, 1)
        assert sum(not browser.closed for browser in created) == 1
        assert not pool._closing_browser_ids
    finally:
        release.set()
        await asyncio.gather(closing, return_exceptions=True)
        if not waiter.done():
            waiter.cancel()
        await asyncio.gather(waiter, return_exceptions=True)
        await pool.close_all(stop_playwright=True)


@pytest.mark.asyncio
@pytest.mark.parametrize("shutdown", [False, True])
async def test_waiting_for_capacity_is_cancellable_and_shutdown_wakes_waiter(route_pool, shutdown):
    pool, created = route_pool
    _, page = await pool.get_page_for_account(routed_account(61))
    waiter = asyncio.create_task(pool.get_page_for_account(routed_account(62)))
    await asyncio.sleep(0.01)
    assert not waiter.done()
    try:
        if shutdown:
            await asyncio.wait_for(pool.close_all(stop_playwright=True), 1)
            with pytest.raises(pool_module.TikTokClientError, match="closing"):
                await waiter
            assert created[0].closed
        else:
            waiter.cancel()
            with pytest.raises(asyncio.CancelledError):
                await waiter
            assert not page.is_closed()
        assert not pool._browser_reservations and not pool._browser_inflight_acquires
        assert all(not lock.locked() for lock in pool._account_locks.values())
    finally:
        if not waiter.done():
            waiter.cancel()
            await asyncio.gather(waiter, return_exceptions=True)
        await pool.close_all(stop_playwright=True)


def test_socks5h_without_auth_is_normalized_before_browser_launch():
    from services.proxy_normalizer import ProxyRoute
    route = ProxyRoute.parse("socks5h://proxy.invalid:1080")
    assert route.scheme == "socks5" and route.playwright() == {"server": "socks5://proxy.invalid:1080"}


@pytest.mark.asyncio
async def test_device_preparation_is_single_and_detaches_only_when_released():
    session=SimpleNamespace(send=AsyncMock(),detach=AsyncMock())
    profile=device_profiles.build_device_profile(SimpleNamespace(id=96500,device_seed="b"*64),{},"146.0.0.0")
    context=SimpleNamespace(_account_device_profile=profile,new_cdp_session=AsyncMock(return_value=session))
    page=SimpleNamespace(context=context,on=Mock())
    await asyncio.gather(device_profiles.prepare_device_page(page),device_profiles.prepare_device_page(page))
    context.new_cdp_session.assert_awaited_once()
    session.detach.assert_not_awaited()
    await device_profiles.release_device_page(page)
    await device_profiles.release_device_page(page)
    session.detach.assert_awaited_once()
