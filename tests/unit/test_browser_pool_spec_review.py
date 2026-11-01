"""Independent regression cases added during the specification review."""
import asyncio
from concurrent.futures import ThreadPoolExecutor
from unittest.mock import AsyncMock

import pytest

from executor import browser_pool
from executor.http.playwright_http_client import PlaywrightHttpClient
from executor.http.request_profile import RequestProfile
from executor.http.retry_policy import RetryPolicy
from executor.static_asset_cache import StaticAssetCache, is_cacheable_static_asset
from tests.test_shared_browser_and_asset_cache import ASSET, account, shared_pool as shared_pool
from tests.unit.test_http_signature_runtime import _DummyResponse, _SignaturePage


pytestmark = pytest.mark.unit


@pytest.mark.asyncio
async def test_shared_rotation_evicts_idle_context_before_allocating_replacement(shared_pool):
    pool, launch = shared_pool
    pool.max_browsers = 1
    try:
        original, page = await pool.get_page_for_account(account(1, "http://first.invalid:8080"))
        await pool.return_page_to_pool(page)
        replacement, _ = await pool.get_page_for_account(account(2, "http://second.invalid:8080"))
        assert original.closed, "Idle contexts must not accumulate when laptop concurrency is one"
        assert list(pool._contexts.values()) == [replacement]
        assert launch.await_count == 1
        pool._browsers["shared_proxied"].close.assert_not_awaited()
    finally:
        await pool.close_all()


@pytest.mark.parametrize("marker", ["/api/", "/passport/", "/aweme/", "mcs", "mon.", "log.",
                                    "msToken", "X-Bogus", "X-Gnarly", "ttwid", "sessionid"])
def test_every_forbidden_static_cache_marker_is_rejected(marker):
    assert not is_cacheable_static_asset("GET", "script", ASSET + "?" + marker)


def test_cache_memory_limit_survives_parallel_threads_and_replacements():
    cache = StaticAssetCache(max_total_bytes=4096, max_item_bytes=1024)

    def write_batch(index):
        for step in range(100):
            url = ASSET + f"?file={index}-{step % 5}"
            cache.put(url, 200, {}, b"x" * (64 + step % 800))
            cache.get(url)
            assert 0 <= cache.total_bytes <= 4096

    with ThreadPoolExecutor(max_workers=8) as threads:
        list(threads.map(write_batch, range(8)))
    assert cache.total_bytes == sum(len(entry.body) for entry in cache._entries.values())


@pytest.mark.asyncio
@pytest.mark.parametrize("header", ["x-ms-token", "X-MS-TOKEN", "X-Ms-Token"])
async def test_preserved_response_token_capture_updates_only_web_token(header):
    response = _DummyResponse(200, '{"status_code":0,"comment":{"cid":"7641761192486195002"}}',
                              {"content-type": "application/json", header: "fresh-header-token"})
    page = _SignaturePage(None, response=response)
    page.url = "https://www.tiktok.com/@fixture/video/7635267329407913230"
    page.context.cookies.return_value = [
        {"name": "msToken", "value": "old-web", "domain": ".tiktok.com", "path": "/"},
        {"name": "msToken", "value": "shop-token", "domain": "shop.tiktok.com", "path": "/"},
    ]
    page.context.add_cookies = AsyncMock()
    client = PlaywrightHttpClient(page.context, page=page, retry_policy=RetryPolicy(max_retries=0))
    success, *_ = await client._execute_request(RequestProfile.publish_main_profile(),
                                              {"aweme_id": "7635267329407913230", "text": "local fixture"})
    assert success and client._last_response_ms_token == "fresh-header-token"
    updates = page.context.add_cookies.await_args.args[0]
    assert updates == [{"name": "msToken", "value": "fresh-header-token", "domain": ".tiktok.com", "path": "/"}]


@pytest.mark.asyncio
@pytest.mark.parametrize("published", [False, True])
async def test_preserved_parent_and_reply_likes_follow_successful_publication(monkeypatch, published):
    from executor import user_schema as schema
    parent, previous_reply, new_reply = "7641761192486195000", "7641761192486195001", "7641761192486195002"
    monkeypatch.setattr(schema, "_BRANCH_PUBLISHED_REPLIES", {})
    monkeypatch.setattr(schema, "_BRANCH_PARENT_LIKED_ACCOUNTS", {})
    monkeypatch.setenv("LIKE_PARENT_BEFORE_REPLY", "1")
    monkeypatch.setattr(schema.asyncio, "sleep", AsyncMock())
    schema._record_branch_published_reply(parent, 1, previous_reply)
    events = []

    async def reply(**kwargs):
        events.append("reply_started")
        # An asynchronous publish must finish before any parent/reply like.
        events.append("reply_succeeded" if published else "reply_failed")
        return published, new_reply if published else None, None if published else "rejected", None, {}

    async def like(**kwargs):
        events.append("like:" + kwargs["cid"])
        return True, None, None

    client = AsyncMock()
    client.reply_comment.side_effect = reply
    client.like_comment.side_effect = like
    session = schema._UserSchemaHttpSession(scope=schema.REPLY_SCOPE, account_id=930999, client=client,
        cookies={"sessionid": "fixture"}, aweme_id="7635267329407913230",
        video_url="https://www.tiktok.com/@fixture/video/7635267329407913230")
    await schema._publish_reply_http_first_if_eligible(session, "fixture", parent_comment_id=parent,
        correlation_id="spec_review_reply_order", variant_index=2,
        task_settings={"reply_like_target": "1", "reply_like_count": 1})
    expected = ["reply_started", "reply_succeeded", "like:" + parent, "like:" + previous_reply] if published else [
        "reply_started", "reply_failed"]
    assert events == expected


@pytest.mark.asyncio
async def test_cancelled_capacity_wait_leaves_busy_account_and_process_intact(shared_pool, monkeypatch):
    pool, _ = shared_pool
    pool.max_browsers = 1
    monkeypatch.setattr(browser_pool.settings, "BROWSER_POOL_CAPACITY_WAIT_SECONDS", 2.0)
    try:
        original, page = await pool.get_page_for_account(account(1, "http://first.invalid:8080"))
        waiting = asyncio.create_task(pool.get_page_for_account(account(2, "http://second.invalid:8080")))
        await asyncio.sleep(0)
        waiting.cancel()
        with pytest.raises(asyncio.CancelledError):
            await waiting
        assert not original.closed and not page.is_closed()
        assert not pool._context_reservations
        pool._browsers["shared_proxied"].close.assert_not_awaited()
    finally:
        await pool.close_all()
