import asyncio
import gc
import json
import threading
import weakref
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock
from urllib.parse import parse_qs, urlparse

import pytest

from executor import engagement_engine as engine, tiktok_client_async as legacy, user_schema as schema
from executor.http import mentions
from executor.http.comment_scanner import scan_comment_ids
from executor.http.tiktok_endpoints import TikTokEndpoints
from executor.http.playwright_http_client import seed_web_identity_from_account
from tests.unit.test_http_warmup_recovery import warmup as warmup, cookies


@pytest.mark.asyncio
@pytest.mark.parametrize("text", ["🔥 Привет @User. Как дела?", "@User. @Other... и @User!", "@User.Name."])
async def test_canonical_mentions_preserve_punctuation_and_utf16_offsets(monkeypatch, text):
    async def resolve(_page, username, **_kwargs):
        return {"unique_id": username.lower(), "user_id": "123456", "sec_uid": "SEC"}
    monkeypatch.setattr(mentions, "resolve_mention_user", resolve)
    normalized, raw, meta = await mentions.prepare_mentions(None, text)
    expected = text.replace("@User.Name", "@user.name").replace("@User", "@user").replace("@Other", "@other")
    assert normalized == expected
    assert meta["unresolved"] == []
    for entity, (start, end, _) in zip(json.loads(raw), mentions.find_mentions(expected)):
        assert entity["start"] == mentions.utf16_offset(expected, start)
        assert entity["end"] == mentions.utf16_offset(expected, end)


def test_mention_cache_thread_safety_and_copy_isolation(monkeypatch, tmp_path):
    monkeypatch.setattr(mentions, "_MAX_CACHE_SIZE", 64)
    path = tmp_path / "threaded.json"
    def worker(n):
        for i in range(150):
            key = f"user_{n}_{i}"
            mentions._cache_put(key, {"user_id": str(i), "sec_uid": "sec"})
            mentions._cache_get(key)
            if i % 50 == 0:
                mentions._save_disk_cache(path)
    with ThreadPoolExecutor(max_workers=8) as threads:
        list(threads.map(worker, range(8)))
    mentions._cache_put("final", {"user_id": "original", "sec_uid": "sec"})
    _, value = mentions._cache_get("final")
    value["user_id"] = "mutated"
    assert mentions._cache_get("final")[1]["user_id"] == "original"
    mentions._save_disk_cache(path)
    payload = json.loads(path.read_text(encoding="utf-8"))
    assert payload["final"]["user_id"] == "original"
    assert len(mentions._MENTION_CACHE) <= 64


@pytest.mark.asyncio
async def test_mention_disk_write_does_not_block_event_loop(monkeypatch):
    entered, release = threading.Event(), threading.Event()
    original = mentions._save_disk_cache
    def save(path=None):
        entered.set()
        release.wait(timeout=2)
        original(path)
    monkeypatch.setattr(mentions, "_save_disk_cache", save)
    page = SimpleNamespace(is_closed=lambda: False, evaluate=AsyncMock(return_value={
        "status": "ok", "user": {"user_id": "123456", "sec_uid": "sec", "unique_id": "disk_user"}}))
    task = asyncio.create_task(mentions.resolve_mention_user(page, "disk_user"))
    try:
        assert await asyncio.to_thread(entered.wait, 1)
        assert not task.done()
    finally:
        release.set()
        result = await task
    assert result["user_id"] == "123456"


@pytest.mark.asyncio
async def test_warmup_fp_matches_seeded_identity_across_contexts(warmup):
    page, _, _ = warmup
    account = SimpleNamespace(id=95101, cookies='{"sessionid":"session","ttwid":"tw"}', device_seed="a"*64)
    first = seed_web_identity_from_account(account, context=page.context)
    page.context.add_cookies = AsyncMock()
    page._http_auth_verified_at = 0.0
    for _ in range(2):
        values, valid, _ = await engine.warmup_page_for_http(page, page.url, max_retries=1, account=account)
        assert valid and values["verifyFp"] == first["verify_fp"]
    assert all(call.args[0][0]["value"] == first["verify_fp"] for call in page.context.add_cookies.await_args_list)


@pytest.mark.asyncio
async def test_warmup_failure_budget_counts_calls_and_valid_warmup_resets_only_that_budget(warmup):
    page, _, _ = warmup
    account = SimpleNamespace(id=95102, cookies='{"sessionid":"session"}', device_seed="b"*64)
    page._http_auth_verified_at = 0.0
    page.context.cookies.return_value = cookies("sessionid")
    for expected in range(1, engine.HTTP_SKIP_AFTER_WARMUP_FAILS + 1):
        _, valid, _ = await engine.warmup_page_for_http(page, page.url, max_retries=1, account=account)
        assert not valid
        assert engine._HTTP_ACCOUNT_HEALTH[account.id]["warmup_fail"] == expected
    assert engine._should_skip_http_for_account(account.id) == "recent_warmup_failures"
    page.context.cookies.return_value = cookies("sessionid", "ttwid", "msToken", "tt_csrf_token")
    _, valid, _ = await engine.warmup_page_for_http(page, page.url, max_retries=1, account=account)
    assert valid
    assert engine._HTTP_ACCOUNT_HEALTH[account.id]["warmup_fail"] == 0
    assert engine._HTTP_ACCOUNT_HEALTH[account.id]["success"] == 0


def test_contended_preflight_semaphore_does_not_keep_closed_loop_alive():
    async def contend():
        sem = schema._get_http_preflight_semaphore()
        assert schema._get_http_preflight_semaphore() is sem
        limit = 3 if schema._settings.USE_PROXY else 2
        for _ in range(limit):
            await sem.acquire()
        waiting = asyncio.create_task(sem.acquire())
        await asyncio.sleep(0)
        sem.release()
        await waiting
        for _ in range(limit):
            sem.release()
        return weakref.ref(asyncio.get_running_loop()), weakref.ref(sem)
    with ThreadPoolExecutor(max_workers=1) as threads:
        loop_ref, sem_ref = threads.submit(lambda: asyncio.run(contend())).result()
    gc.collect()
    assert sem_ref() is None and loop_ref() is None


@pytest.mark.parametrize("reply", [False, True])
def test_legacy_publish_keeps_form_out_of_url_and_does_not_invent_creation_time(reply):
    url, form = TikTokEndpoints.comment_publish_parts(
        "123", "secret text", reply_id="7641761192486194454" if reply else None,
        runtime_profile={"device_id":"live", "verify_fp":"native"},
    )
    query = parse_qs(urlparse(url).query)
    assert all(key not in query for key in ("text", "text_extra", "reply_id", "reply_to_reply_id", "WebIdLastTime"))
    assert form["text"] == "secret text" and form["text_extra"] == "[]"
    if reply:
        assert form["reply_id"] == "7641761192486194454"
    assert query["device_id"] == ["live"]


@pytest.mark.parametrize("value,expected", [(0,"2"), (1,"1"), (2,"2")])
def test_legacy_digg_uses_canonical_form_and_unlike_value(value, expected):
    url, form = TikTokEndpoints.comment_digg_parts("123", "7641761192486194454", value)
    query = parse_qs(urlparse(url).query)
    assert not any(key in query for key in ("cid", "aweme_id", "digg_type", "WebIdLastTime"))
    assert form == {"aweme_id":"123", "cid":"7641761192486194454", "digg_type":expected}


@pytest.mark.asyncio
@pytest.mark.parametrize("reply", [False, True])
async def test_invalid_scanner_row_still_stops_before_a_later_matching_page(reply):
    wanted, parent = "7641761192486194454", "7641761192486194455"
    rows = [{"cid":"7641761192486194456", "reply_id": parent if reply else "0"},
            {"cid":"invalid", "reply_id": "0"}]
    read = AsyncMock(side_effect=[{"comments":rows,"cursor":20,"has_more":1},
                                {"comments":[{"cid":wanted,"reply_id":parent if reply else "0"}],"has_more":0}])
    result = await scan_comment_ids(SimpleNamespace(), "123", [wanted], read_page=read,
                                    parent_comment_id=parent if reply else None)
    assert not result["complete"] and result["absent_ids"] == []
    assert result["reason"] == ("invalid_rows_or_parent" if reply else "invalid_rows")
    read.assert_awaited_once()


@pytest.mark.asyncio
async def test_shadow_ban_stub_returns_unknown_without_faking_comment_checks(monkeypatch):
    availability = AsyncMock(return_value={"available":True})
    monkeypatch.setattr(legacy, "check_video_availability", availability)
    account = SimpleNamespace(id=95103,label="stub",cookies="sessionid=abc",proxy=None)
    result = await legacy.check_shadow_ban(account, ["https://www.tiktok.com/photo/123"])
    assert result["status"] == "inconclusive" and result["is_shadow_banned"] is None
    assert result["confidence"] == 0 and result["checked_comments"] == 0
    availability.assert_not_awaited()


@pytest.mark.asyncio
async def test_quick_validation_returns_page_through_pool_cleanup(monkeypatch):
    from executor import page_utils
    account = SimpleNamespace(id=95105, label="validator", cookies="sessionid=abc", proxy=None)
    page = SimpleNamespace(url="https://www.tiktok.com/login", goto=AsyncMock(), close=AsyncMock())
    monkeypatch.setattr(legacy, "_fast_cookie_health_precheck", lambda _: (None,"inconclusive"))
    monkeypatch.setattr(legacy, "create_page_for_account", AsyncMock(return_value=(SimpleNamespace(),page)))
    monkeypatch.setattr(legacy.asyncio, "sleep", AsyncMock())
    cleanup = AsyncMock()
    monkeypatch.setattr(page_utils, "safe_close_page", cleanup)
    assert not await legacy.quick_validate_account(account, timeout=1)
    cleanup.assert_awaited_once_with(page)
    page.close.assert_not_awaited()


@pytest.mark.asyncio
async def test_photo_without_username_passes_post_comment_media_guard(monkeypatch):
    from executor import comment_utils, navigation_utils, page_utils
    account = SimpleNamespace(id=95106, label="photo", cookies="sessionid=abc", proxy=None)
    page = SimpleNamespace(url="https://www.tiktok.com/photo/7635267329407913230", title=AsyncMock(return_value="TikTok"),
                           wait_for_timeout=AsyncMock(side_effect=[None, RuntimeError("after media guard")]))
    monkeypatch.setattr(legacy, "create_page_for_account", AsyncMock(return_value=(SimpleNamespace(),page)))
    monkeypatch.setattr(navigation_utils, "smart_navigate", AsyncMock())
    monkeypatch.setattr(comment_utils, "check_page_alive", AsyncMock(return_value=(True,None)))
    monkeypatch.setattr(legacy, "_save_diagnostic", AsyncMock(return_value=(None,None)))
    monkeypatch.setattr(legacy, "check_page_alive", AsyncMock(return_value=(True,None)))
    monkeypatch.setattr(legacy, "quick_diagnose", AsyncMock(return_value={}))
    monkeypatch.setattr(page_utils, "safe_close_page", AsyncMock())
    await legacy.post_comment(account, page.url, "local fixture")
    assert page.wait_for_timeout.await_count == 2


@pytest.mark.asyncio
@pytest.mark.parametrize("states,expected", [(["true"],True), (["false","true"],True),
                                            (["false","false"],False), ([RuntimeError("closed")],False)])
async def test_dom_like_needs_confirmation_and_never_clicks_twice(monkeypatch, states, expected):
    from executor import like_finder, comment_utils, navigation_utils, page_utils
    account = SimpleNamespace(id=95104,label="like",cookies="sessionid=abc",proxy=None)
    page = SimpleNamespace(url="https://www.tiktok.com/@fixture/video/123")
    button = SimpleNamespace(evaluate=AsyncMock(return_value=False), click=AsyncMock(),
                             get_attribute=AsyncMock(side_effect=states))
    monkeypatch.setattr(legacy, "create_page_for_account", AsyncMock(return_value=(SimpleNamespace(), page)))
    monkeypatch.setattr(navigation_utils, "smart_navigate", AsyncMock())
    monkeypatch.setattr(comment_utils, "check_page_alive", AsyncMock(return_value=(True,None)))
    monkeypatch.setattr(like_finder, "find_comment_by_id", AsyncMock(return_value=(SimpleNamespace(),{})))
    monkeypatch.setattr(like_finder, "find_like_button_on_comment", AsyncMock(return_value=(button,{})))
    monkeypatch.setattr(legacy.settings, "HUMAN_LIKE_BEHAVIOR", False)
    monkeypatch.setattr(legacy.asyncio, "sleep", AsyncMock())
    close = AsyncMock()
    monkeypatch.setattr(page_utils, "safe_close_page", close)
    assert await legacy.like_comment(account, "7641761192486194454", video_url=page.url) is expected
    button.click.assert_awaited_once()
    close.assert_awaited_once_with(page)


@pytest.mark.asyncio
async def test_reply_boost_returns_updated_count_without_mutating_previous_snapshot():
    parent, cid = "7641761192486194457", "7641761192486194458"
    schema._record_branch_published_reply(parent, 1, cid)
    copied = schema._get_branch_published_replies(parent)[0]
    assert schema.record_reply_boost(cid, "account-a", parent) == 1
    assert schema.record_reply_boost(cid, "account-b", parent) == 2
    assert schema.record_reply_boost(cid, "account-b", parent) == 2
    assert copied["likes_given"] == 0
