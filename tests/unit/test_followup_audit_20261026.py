import asyncio
import gc
import json
import weakref
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.orm import Session
from sqlalchemy.pool import QueuePool

from executor import device_profiles, user_schema as schema
from executor.http import playwright_http_client as transport
from services import cookie_validator as cookies


@pytest.mark.asyncio
@pytest.mark.parametrize("use_proxy,limit", [(False, 2), (True, 3)])
async def test_weak_preflight_semaphore_limits_real_concurrency_and_survives_waiters(monkeypatch, use_proxy, limit):
    monkeypatch.setattr(schema._settings, "USE_PROXY", use_proxy)
    active = peak = 0
    release = asyncio.Event()
    saturated = asyncio.Event()
    sem_ids = set()

    async def operation():
        nonlocal active, peak
        # Use the exact production async-with expression, without an extra
        # test-owned strong reference holding the semaphore alive.
        async with schema._get_http_preflight_semaphore():
            sem_ids.add(id(schema._get_http_preflight_semaphore()))
            active += 1
            peak = max(peak, active)
            if active == limit:
                saturated.set()
            await release.wait()
            active -= 1

    jobs = [asyncio.create_task(operation()) for _ in range(30)]
    try:
        await asyncio.wait_for(saturated.wait(), 1)
        gc.collect()
        await asyncio.sleep(0)
        assert active == limit and not any(job.done() for job in jobs)
        release.set()
        await asyncio.gather(*jobs)
        assert peak == limit and len(sem_ids) == 1
    finally:
        release.set()
        await asyncio.gather(*jobs, return_exceptions=True)


def test_session_scope_is_distinct_from_connection_scope():
    engine = create_engine("sqlite://", poolclass=QueuePool, pool_size=1, max_overflow=0)
    try:
        with Session(engine) as session:
            assert engine.pool.checkedout() == 0
            assert session.scalar(text("select 1")) == 1
            assert engine.pool.checkedout() == 1
            session.commit()
            # The Session still exists, but has released the connection.
            assert engine.pool.checkedout() == 0
    finally:
        engine.dispose()


@pytest.mark.asyncio
async def test_page_callback_reports_cdp_failure_without_closing_callers_page():
    callbacks = {}
    context = SimpleNamespace(add_init_script=AsyncMock(), on=lambda event, callback: callbacks.setdefault(event, callback))
    profile = device_profiles.build_device_profile(SimpleNamespace(id=96900, device_seed="a" * 64), {}, "146.0.0.0")
    session = SimpleNamespace(send=AsyncMock(side_effect=RuntimeError("CDP fixture failure")), detach=AsyncMock())
    context.new_cdp_session = AsyncMock(return_value=session)
    page = SimpleNamespace(context=context, on=Mock(), close=AsyncMock(), is_closed=lambda: False)
    await device_profiles.install_device_profile(context, profile)
    await callbacks["page"](page)
    with pytest.raises(RuntimeError, match="CDP fixture failure"):
        await device_profiles.prepare_device_page(page)
    page.close.assert_not_awaited()
    context.new_cdp_session.assert_awaited_once()
    await device_profiles.release_device_page(page)
    session.detach.assert_awaited_once()


@pytest.fixture
def identity_cache(monkeypatch):
    monkeypatch.setattr(transport, "_ACCOUNT_WEB_IDENTITY_CACHE", {})
    monkeypatch.setattr(transport, "_ACCOUNT_WEB_IDENTITY_SEEDS", {})
    monkeypatch.setattr(transport, "_ACCOUNT_WEB_IDENTITY_MAX_ENTRIES", 2)


def identity_account(n):
    return SimpleNamespace(id=96900 + n, device_seed=str(n) * 64,
                           cookies=json.dumps({"device_id": str(7600000000000000000 + n), "s_v_web_id": f"verify_saved_{n}"}))


def test_identity_cache_lru_eviction_keeps_context_and_saved_fp(identity_cache):
    first, second, third = [identity_account(n) for n in (1, 2, 3)]
    context = SimpleNamespace()
    transport.seed_web_identity_from_account(first, context)
    transport.seed_web_identity_from_account(second)
    transport.get_cached_web_identity(first.id)
    transport.seed_web_identity_from_account(third)
    assert second.id not in transport._ACCOUNT_WEB_IDENTITY_CACHE
    assert set(transport._ACCOUNT_WEB_IDENTITY_SEEDS) == set(transport._ACCOUNT_WEB_IDENTITY_CACHE)
    transport.seed_web_identity_from_account(second)
    assert first.id not in transport._ACCOUNT_WEB_IDENTITY_CACHE
    assert transport.get_cached_web_identity(None, context)["verify_fp"] == "verify_saved_1"
    assert transport.get_cached_web_identity(first.id, account=first)["verify_fp"] == "verify_saved_1"


def test_identity_cache_concurrent_eviction_keeps_seed_maps_bounded(identity_cache):
    def run(n):
        for i in range(40):
            account = identity_account(n * 100 + i)
            transport.seed_web_identity_from_account(account)
            transport.get_cached_web_identity(account.id, account=account)
    with ThreadPoolExecutor(max_workers=6) as workers:
        list(workers.map(run, range(6)))
    assert len(transport._ACCOUNT_WEB_IDENTITY_CACHE) <= 2
    assert set(transport._ACCOUNT_WEB_IDENTITY_SEEDS) == set(transport._ACCOUNT_WEB_IDENTITY_CACHE)


def test_context_cannot_reuse_evicted_identity_after_account_seed_changes(identity_cache):
    from executor.http.request_profile import generate_fallback_device_id
    account = SimpleNamespace(id=96899, device_seed="a" * 64, cookies="{}")
    context = SimpleNamespace()
    transport.seed_web_identity_from_account(account, context)
    transport.cache_web_identity(account.id, {"device_id": "old_observed_device"}, context=context, account=account)
    for n in (1, 2):
        transport.seed_web_identity_from_account(identity_account(n))
    assert account.id not in transport._ACCOUNT_WEB_IDENTITY_SEEDS
    account.device_seed = "b" * 64
    restored = transport.get_cached_web_identity(account.id, context=context, account=account)
    assert restored["device_id"] == generate_fallback_device_id(account.device_seed)
    assert context._account_web_identity_seed == account.device_seed


@pytest.fixture
def preflight_cache(monkeypatch):
    for name in ("_HTTP_PREFLIGHT_VALID_CACHE", "_HTTP_PREFLIGHT_USERNAME_CACHE", "_HTTP_PREFLIGHT_USER_ID_CACHE"):
        monkeypatch.setattr(schema, name, {})
    monkeypatch.setattr(schema, "_HTTP_PREFLIGHT_CACHE_MAX_ENTRIES", 2)


@pytest.mark.asyncio
async def test_preflight_success_bounds_all_three_cache_maps(preflight_cache, monkeypatch):
    monkeypatch.setattr(cookies, "validate_account_http", AsyncMock(return_value={
        "status": "valid", "valid": True, "definitive": True, "username": "fixture", "user_id": "123456",
    }))
    for n in range(5):
        account = SimpleNamespace(id=96950 + n, cookies='{"sessionid":"fixture"}')
        assert await schema._preflight_cookie_http_gate(None, account, correlation_id="fixture", phase="reply") is None
    assert len(schema._HTTP_PREFLIGHT_VALID_CACHE) == 2
    assert set(schema._HTTP_PREFLIGHT_VALID_CACHE) == set(schema._HTTP_PREFLIGHT_USERNAME_CACHE) == set(schema._HTTP_PREFLIGHT_USER_ID_CACHE)


def test_preflight_expiration_removes_metadata_and_orphans(preflight_cache):
    now = schema._HTTP_PREFLIGHT_VALID_CACHE_TTL_SECONDS + 100.0
    schema._HTTP_PREFLIGHT_VALID_CACHE.update({1: (1.0, "old"), 2: (now - 1.0, "recent")})
    schema._HTTP_PREFLIGHT_USERNAME_CACHE.update({1: "old", 2: "recent", 3: "orphan"})
    schema._HTTP_PREFLIGHT_USER_ID_CACHE.update({1: "1", 2: "2", 3: "3"})
    with schema._HTTP_PREFLIGHT_VALID_CACHE_LOCK:
        schema._prune_http_preflight_cache(now)
    assert set(schema._HTTP_PREFLIGHT_VALID_CACHE) == set(schema._HTTP_PREFLIGHT_USERNAME_CACHE) == set(schema._HTTP_PREFLIGHT_USER_ID_CACHE) == {2}


@pytest.mark.asyncio
@pytest.mark.parametrize("login,captcha,verified", [
    ("logged_in", False, True), ("unknown", False, False), ("logged_out", False, False),
    ("logged_in", True, False), ("logged_in", RuntimeError("probe failure"), False),
])
async def test_auth_freshness_requires_positive_login_and_successful_clear_captcha(monkeypatch, login, captcha, verified):
    page = SimpleNamespace(url="https://www.tiktok.com/@fixture/video/7635267329407913230")
    detector = AsyncMock(side_effect=captcha) if isinstance(captcha, Exception) else AsyncMock(return_value=(captcha, "fixture"))
    monkeypatch.setattr(schema, "detect_captcha", detector)
    monkeypatch.setattr(schema, "dismiss_cookie_banners", AsyncMock())
    monkeypatch.setattr(schema, "get_browser_login_state", AsyncMock(return_value={"state": login}))
    await schema._fast_visible_captcha_reason(page, phase="http_warmup")
    await schema._visible_logged_out_reason(page, phase="http_warmup")
    assert schema._has_recent_http_browser_auth(page) is verified


@pytest.mark.asyncio
async def test_auth_proof_does_not_transfer_after_navigation(monkeypatch):
    page = SimpleNamespace(url="https://www.tiktok.com/@fixture/video/7635267329407913230")
    monkeypatch.setattr(schema, "detect_captcha", AsyncMock(return_value=(False, None)))
    monkeypatch.setattr(schema, "dismiss_cookie_banners", AsyncMock())
    monkeypatch.setattr(schema, "get_browser_login_state", AsyncMock(return_value={"state": "logged_in"}))
    await schema._fast_visible_captcha_reason(page, phase="fixture")
    page.url = page.url.replace("7635267329407913230", "1111111111111111111")
    await schema._visible_logged_out_reason(page, phase="fixture")
    assert not schema._has_recent_http_browser_auth(page)


@pytest.mark.parametrize("shape", ["records", "dict", "header"])
def test_cookie_names_remain_case_sensitive_without_suppressing_guard_aliases(shape):
    records = [
        {"name": "sid_guard", "value": "b" * 32 + "|1800000000|86400", "domain": ".tiktok.com"},
        {"name": "SESSIONID", "value": "other-cookie", "domain": ".tiktok.com"},
        {"name": "msToken", "value": "web-token", "domain": ".tiktok.com"},
        {"name": "mstoken", "value": "different-cookie", "domain": ".tiktok.com"},
    ]
    raw = json.dumps(records if shape == "records" else {r["name"]: r["value"] for r in records})
    if shape == "header":
        raw = "; ".join(f'{r["name"]}={r["value"]}' for r in records)
    parsed = cookies.parse_cookies_to_dict(raw)
    assert parsed["sessionid"] == "b" * 32 and parsed["SESSIONID"] == "other-cookie"
    assert parsed["msToken"] == "web-token" and parsed["mstoken"] == "different-cookie"
    browser = cookies.normalize_cookies_for_browser(records, drop_stale_antifraud=False)
    assert {r["name"]: r["value"] for r in browser}["sessionid"] == "b" * 32
    assert {r["name"]: r["value"] for r in browser}["mstoken"] == "different-cookie"


def test_uppercase_guard_is_not_relabelled_as_a_real_auth_cookie():
    raw = json.dumps({"SID_GUARD": "a" * 32 + "|1800000000|86400"})
    assert "sessionid" not in cookies.parse_cookies_to_dict(raw)
    assert not cookies.has_guard_only_auth(raw)


@pytest.mark.asyncio
async def test_same_url_document_reload_clears_browser_auth_proof(monkeypatch):
    callbacks = {}
    page = SimpleNamespace(url="https://www.tiktok.com/@fixture/video/7635267329407913230",
                           main_frame=object(), on=lambda event, callback: callbacks.setdefault(event, callback))
    monkeypatch.setattr(schema, "detect_captcha", AsyncMock(return_value=(False, None)))
    monkeypatch.setattr(schema, "dismiss_cookie_banners", AsyncMock())
    monkeypatch.setattr(schema, "get_browser_login_state", AsyncMock(return_value={"state": "logged_in"}))
    await schema._fast_visible_captcha_reason(page, phase="fixture")
    await schema._visible_logged_out_reason(page, phase="fixture")
    assert schema._has_recent_http_browser_auth(page)
    callbacks["framenavigated"](object())  # A child frame does not invalidate the page.
    assert schema._has_recent_http_browser_auth(page)
    callbacks["request"](SimpleNamespace(frame=page.main_frame, is_navigation_request=lambda: True))
    assert not schema._has_recent_http_browser_auth(page)
    await schema._visible_logged_out_reason(page, phase="fixture")
    assert not schema._has_recent_http_browser_auth(page)
