"""Regressions for same-video hydration, bootstrap handoff and bounded retries."""
import asyncio
from contextlib import contextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import httpx
import pytest

from executor import engagement_engine as engine, user_schema as schema
from executor.http import playwright_http_client as transport
from executor.http.page_runtime import document_generation, prepared_runtime_profile
from executor.http.request_profile import RequestProfile
from executor.http.retry_policy import RetryPolicy
from tests.unit.test_cookie_route_audit_20261025 import export_records
from tests.unit.test_http_warmup_recovery import warmup as warmup, URL, cookies


@pytest.mark.asyncio
@pytest.mark.parametrize("stage", ["signer", "cookie_sync"])
async def test_canonical_url_change_during_warmup_keeps_same_target(monkeypatch, warmup, stage):
    page, comments, _ = warmup
    listeners = {}
    page.main_frame = object()
    page.on = lambda event, callback: listeners.update({event: callback})
    page.remove_listener = Mock()
    page.url = URL.replace("/@demo", "")

    def canonicalize():
        page.url = URL + "?_r=1&_t=fixture"
        listeners["framenavigated"](page.main_frame)

    if stage == "signer":
        async def ready(*_, **__):
            canonicalize()
            return True
        monkeypatch.setattr(engine, "wait_for_runtime_signer_ready", ready)
    else:
        async def sync(*_):
            canonicalize()
            return {key: key for key in ("sessionid", "ttwid", "tt_csrf_token", "msToken")}, False
        monkeypatch.setattr(engine, "synchronize_browser_cookies", sync)

    _, valid, info = await engine.warmup_page_for_http(page, URL, max_retries=1)
    assert valid and info["current_url"] == page.url and info["reason_code"] == "ready"
    assert info["comment_api_skipped"] and not info["comment_api_ready"]
    comments.assert_not_awaited()


def test_same_video_history_keeps_generation_but_reload_invalidates_snapshot():
    callbacks = {}
    page = SimpleNamespace(url=URL, main_frame=object(), on=lambda event, cb: callbacks.update({event: cb}))
    generation = document_generation(page)
    page.url += "?_r=1"
    callbacks["framenavigated"](page.main_frame)
    assert document_generation(page) == generation
    snapshot = {"_snapshot_reusable": True, "_captured_at": engine.time.monotonic(),
                "current_url": page.url, "_document_generation": generation}
    assert prepared_runtime_profile(page, snapshot)
    callbacks["request"](SimpleNamespace(frame=page.main_frame, is_navigation_request=lambda: True))
    assert prepared_runtime_profile(page, snapshot) is None


@pytest.mark.asyncio
async def test_bootstrap_selection_handoff_survives_orm_reattachment(monkeypatch):
    import json
    from services import cookie_validator
    for name in ("_HTTP_PREFLIGHT_VALID_CACHE", "_HTTP_PREFLIGHT_BROWSER_FALLBACKS", "_HTTP_PREFLIGHT_BROWSER_HANDOFFS"):
        monkeypatch.setattr(schema, name, {})
    account = SimpleNamespace(id=98001, cookies=json.dumps(export_records()), proxy=None, cookie_file=None)
    validate = AsyncMock(return_value={"status": "inconclusive", "definitive": False,
                                      "error": "Passport response did not confirm authentication"})
    monkeypatch.setattr(cookie_validator, "validate_account_http", validate)
    monkeypatch.setattr(schema, "http_first_enabled", lambda _: True)
    soft = Mock()
    monkeypatch.setattr(schema, "mark_soft_error", soft)
    assert await schema._preflight_cookie_http_gate(None, account, correlation_id="task_1244_select_acc98001", phase="reply_selection") is None
    attached = SimpleNamespace(id=account.id, cookies=account.cookies, proxy=None, cookie_file=None)
    assert await schema._preflight_cookie_http_gate(None, attached, correlation_id="task_1244_engagement_reply_v1_a1_acc98001", phase="reply_pre_browser") is None
    assert attached._http_preflight_requires_browser_warmup
    validate.assert_awaited_once()
    soft.assert_not_called()
    assert await schema._preflight_cookie_http_gate(None, attached, correlation_id="task_1244_engagement_reply_v1_a1_acc98001", phase="reply_pre_browser")
    assert validate.await_count == 2


@pytest.mark.asyncio
@pytest.mark.parametrize("change", ["task", "route", "auth", "expired", "revoked"])
async def test_bootstrap_handoff_cannot_bypass_changed_or_expired_context(monkeypatch, change):
    account = SimpleNamespace(id=98002, cookies='{"sessionid":"first"}', proxy="http://one.invalid:80")
    for name in ("_HTTP_PREFLIGHT_VALID_CACHE", "_HTTP_PREFLIGHT_BROWSER_FALLBACKS", "_HTTP_PREFLIGHT_BROWSER_HANDOFFS"):
        monkeypatch.setattr(schema, name, {})
    monkeypatch.setattr(schema._settings, "USE_PROXY", True)
    monkeypatch.setattr(schema, "http_first_enabled", lambda _: True)
    key = schema._http_preflight_handoff_key(schema._http_preflight_cache_key(account), "task_1244_select_acc98002")
    schema._HTTP_PREFLIGHT_BROWSER_HANDOFFS[key] = schema.time.monotonic() - (46 if change == "expired" else 0)
    if change == "route":
        account.proxy = "http://two.invalid:80"
    elif change == "auth":
        account.cookies = '{"sessionid":"second"}'
    elif change == "revoked":
        schema._forget_http_preflight_valid(account)
    validate = AsyncMock(return_value={"status": "dead", "definitive": True, "error": "fixture logged out"})
    monkeypatch.setattr("services.cookie_validator.validate_account_http", validate)
    monkeypatch.setattr(schema, "_register_dead_session", Mock())
    reason = await schema._preflight_cookie_http_gate(None, account, correlation_id=f"task_{9999 if change == 'task' else 1244}_reply", phase="reply_pre_browser")
    assert reason.startswith("dead_session:")
    validate.assert_awaited_once()


@pytest.mark.asyncio
@pytest.mark.parametrize("proxy_enabled,proxy", [(False, None), (False, "http://ignored.invalid:80"), (True, "user:pass@proxy.invalid:80"), (True, "socks5://proxy.invalid:1080")])
async def test_shortlink_http_success_never_starts_browser(monkeypatch, proxy_enabled, proxy):
    from executor import browser_pool
    monkeypatch.setattr(engine.settings, "USE_PROXY", proxy_enabled)
    captured = {}
    client = AsyncMock()
    client.get.return_value = httpx.Response(200, request=httpx.Request("GET", URL))
    client.__aenter__.return_value = client
    def factory(**kwargs):
        captured.update(kwargs)
        return client
    monkeypatch.setattr(httpx, "AsyncClient", factory)
    pool = AsyncMock(side_effect=AssertionError("HTTP redirect must not launch Chromium"))
    monkeypatch.setattr(browser_pool, "get_browser_pool", pool)
    assert await engine.resolve_video_url("https://vt.tiktok.com/fixture/", proxy=proxy) == (URL, "7635267329407913230")
    assert captured["trust_env"] is False and captured["follow_redirects"] is True
    assert captured["proxy"] == (("http://" + proxy if "://" not in proxy else proxy) if proxy_enabled else None)
    pool.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("resolved,status", [("https://www.tiktok.com/login", 200),
                                              ("https://example.invalid/video/7635267329407913230", 200),
                                              ("https://www.tiktok.com/@/video/0", 200), (URL, 503)])
async def test_shortlink_http_refusal_uses_same_route_browser_fallback(monkeypatch, resolved, status):
    from executor import browser_pool
    monkeypatch.setattr(engine.settings, "USE_PROXY", True)
    client = AsyncMock()
    client.__aenter__.return_value = client
    client.get.return_value = httpx.Response(status, request=httpx.Request("GET", resolved))
    monkeypatch.setattr(httpx, "AsyncClient", lambda **_: client)
    resolve = AsyncMock(return_value=URL)
    monkeypatch.setattr(browser_pool, "get_browser_pool", AsyncMock(return_value=SimpleNamespace(resolve_shortlink=resolve)))
    assert await engine.resolve_video_url("https://vt.tiktok.com/fixture/", proxy="http://proxy.invalid:80") == (URL, "7635267329407913230")
    assert resolve.call_args.kwargs["proxy"] == "http://proxy.invalid:80"


@pytest.mark.asyncio
@pytest.mark.parametrize("factory", [RequestProfile.publish_main_profile, RequestProfile.publish_reply_profile, RequestProfile.like_profile])
@pytest.mark.parametrize("url", [URL + "?_r=1", "https://www.tiktok.com/login", URL.replace("7635267329407913230", "1111111111111111111")])
async def test_request_guard_checks_media_identity_after_signature_resolution(monkeypatch, factory, url):
    page = SimpleNamespace(url=URL, context=SimpleNamespace(cookies=AsyncMock(return_value=[])), evaluate=AsyncMock())
    profile = factory()
    async def sign(**kwargs):
        page.url = url
        return {}, "https://www.tiktok.com" + profile.endpoint, SimpleNamespace(mode="browser_fetch", source="fixture", x_bogus=None, x_gnarly=None, a_bogus=None, signer_available=True, notes="")
    monkeypatch.setattr(transport, "resolve_request_signature", sign)
    page.evaluate.return_value = {"headers": {}, "status": 200, "url": "https://www.tiktok.com" + profile.endpoint,
                                  "text": '{"status_code":0,"comment":{"cid":"7641761192486194454"}}'}
    client = transport.PlaywrightHttpClient(SimpleNamespace(), page=page, retry_policy=RetryPolicy(max_retries=0))
    result = await client._execute_request(profile, {"aweme_id": "7635267329407913230", "text": "fixture", "cid": "7641761192486194454"})
    if url == URL + "?_r=1":
        page.evaluate.assert_awaited_once()
    else:
        assert not result[0] and result[2] == "target_document_changed"
        assert client.last_request_meta["request_attempted"] is False
        page.evaluate.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("reason", ["target_document_changed", "http_preflight:inconclusive", "mixed"])
async def test_dynamic_main_stops_mass_preparation_failures_with_candidates_still_available(monkeypatch, reason):
    from db import database
    from executor import browser_pool
    @contextmanager
    def context():
        yield SimpleNamespace()
    selected = []
    def select(*_, **__):
        account = SimpleNamespace(id=10000 + len(selected), proxy=None)
        selected.append(account)
        return account
    async def fail(*_, **__):
        return [], ("goto_failed:fixture" if len(selected) % 2 else "target_document_changed") if reason == "mixed" else reason
    monkeypatch.setattr(database, "get_sync_db_context", context)
    monkeypatch.setattr(schema, "get_free_account_for_main_comment", select)
    monkeypatch.setattr(schema, "_try_main_account", fail)
    for name in ("release_main_account_reservation", "_record_main_retry_failure"):
        monkeypatch.setattr(schema, name, Mock())
    monkeypatch.setattr(schema, "extend_main_account_reservation", Mock(return_value=True))
    monkeypatch.setattr(browser_pool, "ensure_thread_browser_pool_capacity", Mock())
    monkeypatch.setattr(browser_pool, "close_browser_pool", AsyncMock())
    monkeypatch.setattr(schema.asyncio, "sleep", AsyncMock())
    for name, value in {"USE_PROXY": False, "MAX_ACCOUNT_CANDIDATES": 3000,
                        "PARALLEL_MAIN_ACCOUNT_SLOTS": 3, "PARALLEL_COMMENTING_MAX_MAIN_ATTEMPTS": 6,
                        "PARALLEL_DYNAMIC_WORKER_POOL_ENABLED": True}.items():
        monkeypatch.setattr(schema._settings, name, value)
    result = await schema.execute_user_schema_parallel(None, SimpleNamespace(settings={"replies": []}), URL, ["main"], 1245, boost_enabled=False, boost_quantity=0)
    assert 6 <= len(selected) <= 8
    assert result.error == "parallel_main_no_publications_after_two_batches"
    assert not result.published_mains


@pytest.mark.asyncio
async def test_response_listener_detaches_and_driver_read_drains_on_close(monkeypatch, warmup):
    page, _, _ = warmup
    callbacks = {}
    page.on = lambda event, cb: callbacks.update({event: cb})
    page.remove_listener = Mock()
    started, closed, drained = asyncio.Event(), asyncio.Event(), asyncio.Event()
    async def stalled_body():
        started.set()
        try:
            await closed.wait()
        finally:
            drained.set()
    async def ready(*_, **__):
        callbacks["response"](SimpleNamespace(url="https://www.tiktok.com/api/comment/list/?aweme_id=7635267329407913230&cursor=0", status=200, text=stalled_body))
        await started.wait()
        return True
    monkeypatch.setattr(engine, "wait_for_runtime_signer_ready", ready)
    _, valid, _ = await engine.warmup_page_for_http(page, URL, max_retries=1)
    try:
        assert valid and not drained.is_set()
        page.remove_listener.assert_called_once_with("response", callbacks["response"])
    finally:
        # A driver read must keep its protocol waiter until page teardown;
        # cancellation here recreates the orphaned TargetClosedError.
        closed.set()
        await asyncio.wait_for(drained.wait(), timeout=1)
