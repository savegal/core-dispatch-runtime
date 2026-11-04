"""Regression checks for HTTP/proxy release blockers, without external requests."""
import asyncio
import json
import time
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from config.settings import get_settings
from db.models import Account, Proxy
from executor.browser_pool import BrowserPool
from executor.device_profiles import build_device_profile
from executor.http import mentions
from executor.http.playwright_http_client import PlaywrightHttpClient
from executor.http.request_profile import RequestProfile
from executor.http.retry_policy import HttpErrorType, RetryPolicy
from services import mobile_proxy_service as mobile, proxy_concurrency_service as concurrency
from services.cookie_validator import validate_account_http, normalize_cookie_records
from services.proxy_normalizer import ProxyRoute, ProxyRouteError, account_proxy_route

URL = "https://www.tiktok.com/@fixture/video/7635267329407913230"
CID = "7641761192486194454"
pytestmark = pytest.mark.unit


@pytest.mark.parametrize("raw", ["http://user:p%40%253A@localhost:8080", "user:p%40%253A@localhost:8080", "localhost:8080:user:p%40%253A"])
def test_proxy_auth_is_decoded_once_in_every_consumer(raw):
    from executor.tiktok_client_async import _normalize_proxy_for_playwright
    from services.cookie_validator import _normalize_proxy_for_aiohttp
    from services.proxy_service import normalize_proxy_url
    route = ProxyRoute.parse(raw)
    assert route.password == "p@%3A"
    assert _normalize_proxy_for_playwright(raw) == route.playwright()
    assert _normalize_proxy_for_aiohttp(raw) == (route.url, None)
    assert normalize_proxy_url(raw) == route.url
    assert mobile._parse_proxy_for_httpx(raw)["https://"] == route.url
    assert ProxyRoute.parse(route.url) == route


@pytest.mark.asyncio
async def test_missing_required_route_never_starts_browser_or_http(monkeypatch):
    monkeypatch.setattr(get_settings(), "USE_PROXY", True)
    account = SimpleNamespace(id=1, proxy=None, cookies='{"sessionid":"fixture"}', cookie_file=None)
    pool = BrowserPool()
    pool.initialize = AsyncMock()
    with pytest.raises(ProxyRouteError, match="missing"):
        await pool.get_page_for_account(account)
    pool.initialize.assert_not_awaited()
    result = await validate_account_http(account)
    assert result["category"] == "proxy_route" and result["request_attempted"] is False
    with pytest.raises(ProxyRouteError):
        await mobile.prepare_mobile_proxy_for_account(account, None)


@pytest.mark.asyncio
@pytest.mark.parametrize("bridge_result", [None, RuntimeError("bridge failed")])
async def test_broken_socks_bridge_cannot_launch_direct_browser(monkeypatch, bridge_result):
    from executor import browser_pool, tiktok_client
    monkeypatch.setattr(get_settings(), "USE_PROXY", True)
    monkeypatch.setattr(tiktok_client, "_get_cookies_for_account", lambda _: '{"sessionid":"fixture"}')
    def bridge(*_):
        if isinstance(bridge_result, Exception):
            raise bridge_result
        return bridge_result
    monkeypatch.setattr(browser_pool, "get_local_http_proxy_for_socks5", bridge)
    launch = AsyncMock()
    pool = BrowserPool()
    pool._playwright = SimpleNamespace(chromium=SimpleNamespace(launch=launch))
    account = SimpleNamespace(id=42, label="bridge-fixture", proxy="socks5://user:secret@fixture.invalid:1080", user_agent=None)
    with pytest.raises(ProxyRouteError, match="socks_bridge_unavailable"):
        await pool.get_page_for_account(account)
    launch.assert_not_awaited()


def test_device_seed_migration_is_idempotent(tmp_path, monkeypatch):
    from sqlalchemy import create_engine, text, inspect
    from db.migrations import add_account_device_seed_migration as migration
    engine = create_engine("sqlite:///" + str(tmp_path / "old.db"))
    try:
        with engine.begin() as connection:
            connection.execute(text("CREATE TABLE accounts (id INTEGER PRIMARY KEY, label TEXT)"))
            connection.execute(text("INSERT INTO accounts VALUES (1, 'fixture')"))
        monkeypatch.setattr(migration, "sync_engine", engine)
        assert migration.migrate() is True
        assert migration.migrate() is False
        assert "device_seed" in {c["name"] for c in inspect(engine).get_columns("accounts")}
        with engine.connect() as connection:
            assert connection.execute(text("SELECT label FROM accounts")).scalar() == "fixture"
    finally:
        engine.dispose()


@pytest.mark.parametrize("raw", ["host:8080:user:secret", "http://user:secret@host:8080", "socks5://user:secret@host:1080"])
def test_proxy_passwords_are_redacted(raw):
    from services.log_sanitizer import sanitize_url
    assert "secret" not in sanitize_url(raw)


def test_country_metadata_and_ssr_identity_win(db_session, monkeypatch):
    monkeypatch.setattr(get_settings(), "USE_PROXY", True)
    proxy = Proxy(proxy="http://fixture.invalid:8080", country_code="PL")
    account = Account(label="route-fixture", proxy=proxy.proxy, cookies="{}")
    db_session.add_all([proxy, account])
    db_session.flush()
    assert account_proxy_route(account).country_code == account.proxy_country_code == "PL"
    params = RequestProfile.comment_list_profile().build_query_params(
        {"aweme_id": "123"},
        cookies={"device_id": "old", "odin_tt": "old", "WebIdLastTime": "old"},
        runtime_profile={"device_id": "live", "odin_id": "live-odin", "web_id_last_time": "12345"},
    )
    assert (params["device_id"], params["odinId"], params["WebIdLastTime"]) == ("live", "live-odin", "12345")


def test_device_seed_survives_uid_discovery_rotation_and_reload(db_session):
    account = Account(label="seed-fixture", cookies='{"sessionid":"original-session"}')
    db_session.add(account)
    db_session.commit()
    initial = build_device_profile(account, {}, "145.0.7632.6")
    saved_seed = account.device_seed
    db_session.commit()
    account._cached_tiktok_uid = "987654321"
    account.cookies = '{"sessionid":"rotated-session","uid_tt":"987654321"}'
    assert build_device_profile(account, {}, "145.0.7632.6") == initial
    db_session.commit()
    db_session.expire(account)
    assert account.device_seed == saved_seed
    assert build_device_profile(account, {}, "145.0.7632.6") == initial


def test_ads_session_cookie_cannot_override_web_session():
    cookies = normalize_cookie_records([
        {"name": "sessionid", "value": "web", "domain": ".tiktok.com"},
        {"name": "sessionid", "value": "ads", "domain": "ads.tiktok.com"},
    ])
    assert cookies["sessionid"] == "web"
    assert len(cookies.records) == 2


@pytest.mark.asyncio
async def test_rotation_drains_users_and_blocks_new_users(monkeypatch):
    monkeypatch.setattr(concurrency.settings, "MAX_TASKS_PER_PROXY", 1)
    proxy = "http://fixture.invalid:8080"
    active = await concurrency.acquire_proxy_usage(proxy)
    rotation_task = asyncio.create_task(concurrency.acquire_proxy_usage(proxy, rotation=True, timeout=2))
    await asyncio.sleep(0.15)
    assert not rotation_task.done()
    with pytest.raises(ProxyRouteError, match="capacity_timeout"):
        await concurrency.acquire_proxy_usage(proxy, timeout=0)
    await active.release()
    rotating = await rotation_task
    try:
        with pytest.raises(ProxyRouteError):
            await concurrency.acquire_proxy_usage(proxy, timeout=0)
    finally:
        await rotating.release()
    next_user = await concurrency.acquire_proxy_usage(proxy, timeout=0)
    await next_user.release()


@pytest.mark.asyncio
async def test_late_usage_release_does_not_delete_new_lease(isolated_proxy_leases):
    first = await concurrency.acquire_proxy_usage("http://fixture.invalid:8080")
    isolated_proxy_leases.zrem(first.keys[0], first.token)  # Simulate expiration/crashed owner.
    second = await concurrency.acquire_proxy_usage("http://fixture.invalid:8080")
    await first.release()
    assert isolated_proxy_leases.zscore(second.keys[0], second.token) is not None
    await second.release()


@pytest.mark.asyncio
async def test_late_mobile_release_preserves_second_handle(monkeypatch, isolated_proxy_leases):
    monkeypatch.setattr(mobile, "get_redis", lambda: isolated_proxy_leases)
    first = await mobile.acquire_proxy_lock(7, timeout=0)
    isolated_proxy_leases.delete("proxy_lock:7")
    second = await mobile.acquire_proxy_lock(7, timeout=0)
    await mobile.release_proxy_lock(first)
    assert isolated_proxy_leases.get("proxy_lock:7") == second.token
    await mobile.release_proxy_lock(second)


@pytest.mark.asyncio
@pytest.mark.parametrize("body", ["", "invalid", '{"status_code":0}'])
@pytest.mark.parametrize("observed_signed", [True, None])
async def test_ambiguous_publish_never_replays(body, observed_signed):
    from tests.unit.test_http_final_audit import EventPage
    page = EventPage("response", body)
    listeners = {}
    page.on = lambda event, callback: listeners.setdefault(event, []).append(callback)
    page.remove_listener = lambda event, callback: listeners[event].remove(callback)
    original = page.evaluate
    async def dispatch(script, args, **kwargs):
        if observed_signed is True:
            args["url"] += "&X-Bogus=fixture&X-Gnarly=fixture"
        # EventPage's basic request URL has no query; replace it with dispatch.
        for callback in list(listeners.get("request", [])):
            if observed_signed is True:
                callback(SimpleNamespace(method="POST", url=args["url"]))
        return await original(script, args, **kwargs)
    page.evaluate = AsyncMock(side_effect=dispatch)
    if observed_signed is None:
        page.on = lambda *_: None  # No dispatch evidence: response URL is not enough.
        page.remove_listener = lambda *_: None
    client = PlaywrightHttpClient(SimpleNamespace(fetch=AsyncMock()), page=page,
                                  retry_policy=RetryPolicy(max_retries=3))
    result = await client._execute_request(RequestProfile.publish_main_profile(), {"aweme_id": "7635267329407913230", "text": "fixture"})
    assert result[3] == HttpErrorType.SUBMITTED_UNCONFIRMED
    page.evaluate.assert_awaited_once()
    from executor import user_schema
    from executor.publish_action import PublishResult
    classified = PublishResult(status="submitted_unconfirmed", network_request_attempted=True)
    assert user_schema._main_publish_submitted_unconfirmed(None, classified)
    assert user_schema._reply_publish_submitted_unconfirmed(classified)


@pytest.mark.asyncio
@pytest.mark.parametrize("text,extra", [
    ("@one @two @three @four", None),
    ("@one", "not-json"),
    ("@one", '[{"type":0,"start":0,"end":4,"user_id":"123","sec_uid":""}]'),
    ("@one", "[]"),
])
async def test_bad_mentions_stop_before_post(text, extra):
    client = PlaywrightHttpClient(SimpleNamespace(fetch=AsyncMock()))
    client._execute_request = AsyncMock()
    result = await client.publish_main_comment("123", text, mention_mode="attempt", **({"text_extra": extra} if extra is not None else {}))
    assert result[0] is False
    assert client.last_request_meta["request_attempted"] is False
    client._execute_request.assert_not_awaited()


def test_disk_mention_cache_preserves_expiry_across_restart(tmp_path, monkeypatch):
    path = tmp_path / "cache.json"
    monkeypatch.setattr(mentions, "_DISK_CACHE_PATH", path)
    monkeypatch.setattr(mentions, "_MENTION_CACHE", {})
    expiry = time.time() + 10
    info = {"user_id": "123", "sec_uid": "sec", "unique_id": "fixture"}
    mentions._MENTION_CACHE["fixture"] = (expiry, info)
    mentions._save_disk_cache()
    assert json.loads(path.read_text())["fixture"]["expires_at"] == expiry
    mentions._MENTION_CACHE.clear()
    mentions._load_disk_cache()
    assert mentions._MENTION_CACHE["fixture"][0] == expiry
    monkeypatch.setattr(mentions.time, "time", lambda: expiry + 1)
    mentions._MENTION_CACHE.clear()
    mentions._load_disk_cache()
    assert not mentions._MENTION_CACHE


@pytest.mark.asyncio
@pytest.mark.parametrize("dispatched,expected", [(True, 6), (False, 9)])
async def test_legacy_reply_loop_has_separate_candidate_and_publication_budgets(monkeypatch, dispatched, expected):
    from executor import engagement_engine as engine
    monkeypatch.setattr(engine.settings, "USE_PROXY", False)
    monkeypatch.setattr(engine.settings, "MAX_RETRY_ACCOUNTS", 9)
    monkeypatch.setattr(engine.settings, "MAX_ACCOUNT_CANDIDATES", 9)
    selected = []
    def select(*_, **kwargs):
        selected.append(len(selected) + 1)
        return SimpleNamespace(id=selected[-1], proxy=None, label="fixture")
    async def attempt(*_, outcome_dict, **kwargs):
        outcome_dict["publication_attempted"] = dispatched
        return None, "fixture_failure", "http"
    monkeypatch.setattr(engine, "get_free_account_for_reply", select)
    monkeypatch.setattr(engine, "get_http_execution_strategy", lambda *_: {"strategy": "http_first"})
    monkeypatch.setattr(engine, "_should_skip_http_for_account", lambda *_: None)
    monkeypatch.setattr(engine, "attempt_reply_on_account", attempt)
    rows = await engine.execute_replies_http(None, URL, CID, ["fixture"], main_comment_text="fixture")
    assert len(selected) == expected and len(rows) == 1


@pytest.mark.asyncio
async def test_legacy_reply_unknown_result_is_terminal_once(monkeypatch):
    from executor import engagement_engine as engine
    monkeypatch.setattr(engine.settings, "USE_PROXY", False)
    account = SimpleNamespace(id=1, proxy=None, label="fixture")
    monkeypatch.setattr(engine, "get_free_account_for_reply", lambda *_, **kwargs: account)
    monkeypatch.setattr(engine, "get_http_execution_strategy", lambda *_: {"strategy": "http_first"})
    monkeypatch.setattr(engine, "_should_skip_http_for_account", lambda *_: None)
    async def attempt(*_, outcome_dict, **kwargs):
        outcome_dict["publication_attempted"] = True
        return None, "submitted_unconfirmed:empty_body", "submitted_unconfirmed"
    mock = AsyncMock(side_effect=attempt)
    monkeypatch.setattr(engine, "attempt_reply_on_account", mock)
    rows = await engine.execute_replies_http(None, URL, CID, ["fixture"], main_comment_text="fixture")
    assert len(rows) == 1 and rows[0]["submitted_unconfirmed"]
    mock.assert_awaited_once()
