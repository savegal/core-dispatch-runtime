import asyncio
import copy
import threading
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock
from urllib.parse import parse_qs, urlparse

import pytest

from executor import browser_pool as pool_module
from executor.http import playwright_http_client as transport
from executor.http.request_profile import RequestProfile
from executor.http.retry_policy import HttpErrorType, RetryPolicy
from tests.unit.test_browser_pool_page_reuse import LaunchProbeBrowser, LaunchProbeContext


@pytest.fixture
def account_and_pool(monkeypatch):
    from executor import tiktok_client
    monkeypatch.setattr(pool_module.settings, "USE_PROXY", False)
    monkeypatch.setattr(tiktok_client, "_get_cookies_for_account", lambda _: "[]")
    monkeypatch.setattr(tiktok_client, "parse_cookies", lambda _: [])
    account = SimpleNamespace(id=791, label=None, cookies="[]", proxy=None, country_code="UA")
    pool = pool_module.BrowserPool(max_browsers=1)
    browser = LaunchProbeBrowser()
    runtime = SimpleNamespace(chromium=SimpleNamespace(launch=AsyncMock(return_value=browser)), stop=AsyncMock())
    pool._playwright = runtime
    return account, pool, browser


@pytest.mark.asyncio
async def test_concurrent_pool_initialization_shares_one_driver(monkeypatch):
    monkeypatch.setattr(pool_module, "_thread_local", threading.local())
    runtime = SimpleNamespace(chromium=Mock(), stop=AsyncMock())
    async def start():
        await asyncio.sleep(0.01)
        return runtime
    starter = SimpleNamespace(start=AsyncMock(side_effect=start))
    monkeypatch.setattr(pool_module, "async_playwright", lambda: starter)
    pools = await asyncio.gather(*(pool_module.get_browser_pool() for _ in range(8)))
    assert all(pool is pools[0] for pool in pools)
    starter.start.assert_awaited_once()
    await pool_module.close_browser_pool(stop_playwright=True)
    runtime.stop.assert_awaited_once()


@pytest.mark.asyncio
async def test_failed_initializer_is_reused_without_leaking_a_second_pool(monkeypatch):
    monkeypatch.setattr(pool_module, "_thread_local", threading.local())
    runtime = SimpleNamespace(chromium=Mock(), stop=AsyncMock())
    starter = SimpleNamespace(start=AsyncMock(side_effect=[RuntimeError("startup failed"), runtime]))
    monkeypatch.setattr(pool_module, "async_playwright", lambda: starter)
    with pytest.raises(pool_module.TikTokClientError, match="startup failed"):
        await pool_module.get_browser_pool()
    original = pool_module._get_thread_local_pool()
    assert original is not None and original._playwright is None
    assert await pool_module.get_browser_pool() is original
    assert starter.start.await_count == 2
    await pool_module.close_browser_pool(stop_playwright=True)


@pytest.mark.asyncio
@pytest.mark.parametrize("error", [AttributeError("'NoneType' object has no attribute 'send'"),
                                        RuntimeError("Target page, context or browser has been closed")])
async def test_new_context_transport_failure_recovers_without_restarting_driver(account_and_pool, error):
    account, pool, healthy = account_and_pool
    broken = LaunchProbeBrowser()
    broken.new_context = AsyncMock(side_effect=error)
    pool._register_browser("direct", broken, None)
    try:
        context, page = await pool.get_page_for_account(account)
        assert context in healthy.contexts
        assert page._pool_account_id == account.id
        assert broken.closed
        broken.new_context.assert_awaited_once()
        assert not pool._browser_inflight_acquires
    finally:
        await pool.close_all(stop_playwright=True)


@pytest.mark.asyncio
async def test_new_context_recovery_remains_bounded(account_and_pool):
    account, pool, browser = account_and_pool
    browsers = [LaunchProbeBrowser() for _ in range(4)]
    for failed in browsers:
        failed.new_context = AsyncMock(side_effect=AttributeError("NoneType transport"))
    pool._playwright.chromium.launch.side_effect = browsers
    with pytest.raises(pool_module.TikTokClientError, match="Too many retries"):
        await pool.get_page_for_account(account)
    assert sum(failed.new_context.await_count for failed in browsers) == 4
    assert not pool._contexts and not pool._browser_inflight_acquires
    await pool.close_all(stop_playwright=True)


@pytest.mark.asyncio
async def test_bad_context_options_are_not_retried_and_none_label_cannot_mask_error(account_and_pool):
    account, pool, browser = account_and_pool
    browser.new_context = AsyncMock(side_effect=ValueError("invalid timezone parameter"))
    with pytest.raises(pool_module.TikTokClientError, match="invalid timezone parameter"):
        await pool.get_page_for_account(account)
    browser.new_context.assert_awaited_once()
    assert not pool._contexts
    await pool.close_all(stop_playwright=True)


@pytest.mark.asyncio
async def test_direct_connection_does_not_inherit_locale_from_account_cookies(account_and_pool):
    account, pool, browser = account_and_pool
    try:
        context, page = await pool.get_page_for_account(account)
        options = browser.new_context_options[0]
        assert "locale" not in options and "timezone_id" not in options
        assert context._account_accept_language is None
        cdp = page._account_device_session
        override = next(call.args[1] for call in cdp.send.await_args_list if call.args[0] == "Emulation.setUserAgentOverride")
        assert "acceptLanguage" not in override
    finally:
        await pool.close_all(stop_playwright=True)


def registered_context(pool, account, browser_id, context):
    from datetime import datetime, timezone
    pool._contexts[account.id] = context
    pool._account_browser_ids[account.id] = browser_id
    pool._context_last_used[account.id] = datetime.now(timezone.utc)


@pytest.mark.asyncio
async def test_proxy_reassignment_flushes_context_and_releases_old_page_lease(account_and_pool, monkeypatch):
    account, pool, browser = account_and_pool
    old = LaunchProbeContext()
    page = await old.new_page()
    page._pool_account_id = account.id
    page._pool_available = True
    lease = SimpleNamespace(release=AsyncMock())
    page._proxy_usage_lease = lease
    pool._register_browser("old-route", LaunchProbeBrowser(), None)
    registered_context(pool, account, "old-route", old)
    events = []
    async def persist(context, account_id):
        assert account_id == account.id
        events.append(("persist", context))
    monkeypatch.setattr(pool, "_persist_context_cookies_before_close", persist)
    try:
        context, _ = await pool.get_page_for_account(account)
        assert context is not old and old._closed
        assert events[0] == ("persist", old)
        lease.release.assert_awaited_once()
        assert page._proxy_usage_lease is None
        assert pool._account_browser_ids[account.id] == "direct"
        assert not pool._browser_inflight_acquires
    finally:
        await pool.close_all(stop_playwright=True)


@pytest.mark.asyncio
async def test_invalid_cached_context_uses_same_disposal_path(account_and_pool, monkeypatch):
    account, pool, browser = account_and_pool
    old = LaunchProbeContext()
    old.new_page = AsyncMock(side_effect=RuntimeError("cached context invalid"))
    pool._register_browser("direct", browser, None)
    registered_context(pool, account, "direct", old)
    persist = AsyncMock()
    monkeypatch.setattr(pool, "_persist_context_cookies_before_close", persist)
    try:
        context, _ = await pool.get_page_for_account(account)
        assert context is not old and old._closed
        assert persist.await_args_list[0].args == (old, account.id)
        assert not pool._browser_inflight_acquires
    finally:
        await pool.close_all(stop_playwright=True)


@pytest.mark.asyncio
async def test_browser_close_disposes_context_and_lease_exactly_once(account_and_pool, monkeypatch):
    account, pool, browser = account_and_pool
    context = LaunchProbeContext()
    page = await context.new_page()
    lease = SimpleNamespace(release=AsyncMock())
    page._proxy_usage_lease = lease
    pool._register_browser("direct", browser, None)
    registered_context(pool, account, "direct", context)
    persist = AsyncMock()
    monkeypatch.setattr(pool, "_persist_context_cookies_before_close", persist)
    await pool._close_browser("direct", reason="test")
    await pool._dispose_context(context, account.id)
    persist.assert_awaited_once_with(context, account.id)
    lease.release.assert_awaited_once()
    assert not pool._contexts and not pool._account_browser_ids and not pool._browser_inflight_acquires
    await pool.close_all(stop_playwright=True)


@pytest.mark.asyncio
async def test_driver_reset_disposes_leases_without_holding_metadata_lock(account_and_pool, monkeypatch):
    account, pool, browser = account_and_pool
    context = LaunchProbeContext()
    page = await context.new_page()
    lease = SimpleNamespace(release=AsyncMock())
    page._proxy_usage_lease = lease
    pool._register_browser("direct", browser, None)
    registered_context(pool, account, "direct", context)
    async def persist(_context, _account_id):
        assert not pool._lock.locked()
    persist_mock = AsyncMock(side_effect=persist)
    monkeypatch.setattr(pool, "_persist_context_cookies_before_close", persist_mock)
    failed = pool._playwright
    await pool._reset_disconnected_runtime(failed)
    await pool._reset_disconnected_runtime(failed)
    assert context._closed
    lease.release.assert_awaited_once()
    persist_mock.assert_awaited_once_with(context, account.id)
    failed.stop.assert_awaited_once()
    assert pool._playwright is None
    assert not pool._contexts and not pool._account_browser_ids and not pool._browser_idle_since


@pytest.mark.asyncio
async def test_cancelled_context_setup_closes_and_flushes_orphan(account_and_pool, monkeypatch):
    account, pool, browser = account_and_pool
    entered = asyncio.Event()
    async def install(context, profile):
        entered.set()
        await asyncio.Event().wait()
    monkeypatch.setattr(pool_module, "install_device_profile", install)
    persist = AsyncMock()
    monkeypatch.setattr(pool, "_persist_context_cookies_before_close", persist)
    task = asyncio.create_task(pool.get_page_for_account(account))
    await asyncio.wait_for(entered.wait(), timeout=2)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    context = browser.contexts[0]
    assert context._closed
    persist.assert_awaited_once_with(context, account.id)
    assert not pool._contexts and not pool._browser_inflight_acquires
    await pool.close_all(stop_playwright=True)


@pytest.mark.asyncio
@pytest.mark.parametrize("native_device_id", [None, "7382000000000000001"])
async def test_extracted_runtime_keeps_seeded_fp_for_every_endpoint(native_device_id):
    account = SimpleNamespace(id=7901, cookies='{"sessionid":"native-session"}', device_seed="b" * 64)
    context = SimpleNamespace(_pool_account_id=account.id, _pool_account_obj=account)
    seeded = transport.seed_web_identity_from_account(account, context=context)
    extracted = {"current_url": "https://www.tiktok.com/@fixture/video/123"}
    if native_device_id:
        extracted["device_id"] = native_device_id
    page = SimpleNamespace(context=context, url="https://www.tiktok.com/@fixture/video/123",
                           evaluate=AsyncMock(return_value=extracted))
    runtime = await transport._extract_runtime_profile(page)
    assert runtime["account_id"] == account.id
    assert runtime["verify_fp"] == runtime["s_v_web_id"] == seeded["verify_fp"]
    if native_device_id:
        assert runtime["device_id"] == native_device_id
    for factory in (RequestProfile.publish_main_profile, RequestProfile.publish_reply_profile,
                    RequestProfile.like_profile, RequestProfile.comment_list_profile, RequestProfile.reply_list_profile):
        query = factory().build_query_params({}, {"sessionid": "native-session"}, runtime)
        assert query["verifyFp"] == seeded["verify_fp"]


@pytest.mark.asyncio
async def test_anonymous_page_runtime_reuses_context_fp_without_mutating_query_inputs():
    context = SimpleNamespace()
    page = SimpleNamespace(context=context, url="https://www.tiktok.com/@fixture/video/123",
                           evaluate=AsyncMock(side_effect=lambda *_args, **_kwargs: {}))
    first = await transport._extract_runtime_profile(page)
    second = await transport._extract_runtime_profile(page)
    assert first["verify_fp"] == second["verify_fp"]
    assert first["verify_fp"] == context._account_web_identity["verify_fp"]
    cookies = {}
    before = dict(first)
    profile = RequestProfile.comment_list_profile()
    assert profile.build_query_params({}, cookies, first) == profile.build_query_params({}, cookies, first)
    assert cookies == {} and first == before


@pytest.mark.asyncio
@pytest.mark.parametrize("suffix", ["/", ""])
async def test_empty_object_publish_is_unknown_and_never_retried(suffix):
    page = SimpleNamespace(context=SimpleNamespace(cookies=AsyncMock(return_value=[])),
                           evaluate=AsyncMock(return_value={"status": 200, "headers": {"content-type": "application/json"}, "text": "{}"}))
    profile = RequestProfile.publish_main_profile()
    profile.endpoint = "/api/comment/publish" + suffix
    client = transport.PlaywrightHttpClient(SimpleNamespace(fetch=AsyncMock()), page=page,
                                            retry_policy=RetryPolicy(max_retries=3))
    success, payload, error, error_type = await client._execute_request(profile, {"text": "fixture"})
    assert not success and payload is None
    assert error_type is HttpErrorType.SUBMITTED_UNCONFIRMED
    assert error == "submitted_unconfirmed:missing_api_status"
    assert client.last_request_meta["publication_status"] == "submitted_unconfirmed"
    page.evaluate.assert_awaited_once()


@pytest.mark.asyncio
async def test_no_trailing_slash_publish_keeps_author_metadata_and_builds_parts_once(monkeypatch):
    page = SimpleNamespace(context=SimpleNamespace(cookies=AsyncMock(return_value=[])),
                           evaluate=AsyncMock(return_value={"status": 200, "headers": {"content-type": "application/json"},
                               "text": '{"status_code":0,"comment":{"cid":"7641761192486194454","user":{"unique_id":"fixture","uid":"123456"}}}'}))
    profile = RequestProfile.publish_main_profile()
    profile.endpoint = "/api/comment/publish"
    spy = Mock(wraps=profile.build_query_params)
    monkeypatch.setattr(profile, "build_query_params", spy)
    cookies = {"sessionid": "fixture"}
    runtime = {"account_id": 123, "verify_fp": "native-fp"}
    originals = copy.deepcopy((cookies, runtime))
    client = transport.PlaywrightHttpClient(SimpleNamespace(fetch=AsyncMock()), page=page, runtime_profile=runtime)
    success, _, _, _ = await client._execute_request(profile, {"text": "hello"}, cookies=cookies)
    assert success
    spy.assert_called_once()
    assert (cookies, runtime) == originals
    assert client.last_request_meta["comment_author"] == "fixture"
    assert client.last_request_meta["comment_author_uid"] == "123456"
    args = page.evaluate.await_args.args[1]
    assert args["form_data"] == {"text": "hello", "text_extra": "[]"}
    assert "text=" not in urlparse(args["url"]).query
    assert parse_qs(urlparse(args["url"]).query)["verifyFp"] == ["native-fp"]


def test_deleted_account_row_cannot_turn_seed_into_none(db_session):
    from sqlalchemy import delete, update
    from db.models import Account
    from executor.device_profiles import _account_seed_key
    account = Account(label="deleted-seed-fixture", cookies="{}")
    db_session.add(account)
    db_session.commit()
    db_session.execute(update(Account).where(Account.id == account.id).values(device_seed=None))
    db_session.commit()
    assert account.device_seed is None
    db_session.execute(delete(Account).where(Account.id == account.id).execution_options(synchronize_session=False))
    seed = _account_seed_key(account)
    assert isinstance(seed, str) and len(bytes.fromhex(seed)) == 32
