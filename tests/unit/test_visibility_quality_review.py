import json
import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock
from unittest.mock import Mock

import pytest

from executor import main_visibility, reply_visibility
from executor.http import comment_discovery as api

pytestmark = [pytest.mark.unit, pytest.mark.asyncio]
VIDEO = "7635267329407913230"
CID = "7697345157284266772"
OTHER = "7697345157284266773"
PARENT = "7697345157284266750"


def page_with_body(monkeypatch, body, *, signed=True):
    monkeypatch.setattr(api, "_extract_runtime_profile", AsyncMock(return_value={}))
    monkeypatch.setattr(api.asyncio, "sleep", AsyncMock())
    query = "?X-Bogus=x&X-Gnarly=y" if signed else ""
    return SimpleNamespace(
        url=f"https://www.tiktok.com/@demo/video/{VIDEO}",
        context=SimpleNamespace(cookies=AsyncMock(return_value=[])),
        evaluate=AsyncMock(return_value={"status": 200, "text": json.dumps(body),
                                        "url": "https://www.tiktok.com/api/comment/list/" + query}),
    )


async def test_unsigned_fresh_main_response_is_not_absence_evidence(monkeypatch):
    page = page_with_body(monkeypatch, {"status_code": 0, "comments": [], "has_more": 0}, signed=False)
    assert await api.fetch_comment_page(page, VIDEO, fresh=True) is None
    assert page._comment_list_http_meta["reason"] == "unsigned_response"


async def test_null_comment_list_is_not_a_complete_empty_list(monkeypatch):
    page = page_with_body(monkeypatch, {"status_code": 0, "comments": None, "has_more": 0})
    assert await api.fetch_comment_page(page, VIDEO, fresh=True) is None


@pytest.mark.parametrize("more", [False, 0, "0"])
async def test_explicit_zero_total_allows_legitimate_null_empty_list(monkeypatch, more):
    page = page_with_body(monkeypatch, {"status_code": 0, "comments": None, "total": 0, "has_more": more})
    report = await main_visibility.scan_main_comment_ids(page, VIDEO, [CID])
    assert report["absent_ids"] == [CID] and report["complete"]


async def test_missing_comment_field_is_not_invented_from_zero_total(monkeypatch):
    page = page_with_body(monkeypatch, {"status_code": 0, "total": 0, "has_more": 0})
    assert await api.fetch_comment_page(page, VIDEO, fresh=True) is None


@pytest.mark.parametrize("candidate, reference, expected", [
    ({("session", "a")}, {("session", "b")}, False),
    ({("uid", "a")}, {("username", "b")}, False),
    ({("username", "a")}, {("username", "")}, False),
    ({("uid", "a")}, {("uid", "b")}, True),
    ({("uid", "a"), ("session", "same")}, {("uid", "b"), ("session", "same")}, False),
])
async def test_independence_requires_comparable_actual_user_identity(candidate, reference, expected):
    from executor.visibility_support import identities_independent
    assert identities_independent(candidate, [reference]) is expected


async def test_nested_api_failure_is_not_a_complete_empty_list(monkeypatch):
    page = page_with_body(monkeypatch, {"status_code": 0, "data": {"status_code": 102, "comments": [], "has_more": 0}})
    assert await api.fetch_comment_page(page, VIDEO, fresh=True) is None
    assert page._comment_list_http_meta["reason"] == "api_status"


async def test_signed_response_from_wrong_endpoint_is_rejected(monkeypatch):
    page = page_with_body(monkeypatch, {"status_code": 0, "comments": [], "has_more": 0})
    page.evaluate.return_value["url"] = "https://www.tiktok.com/api/comment/list/reply/?X-Bogus=x&X-Gnarly=y"
    assert await api.fetch_comment_page(page, VIDEO, fresh=True) is None
    assert page._comment_list_http_meta["reason"] == "unexpected_response_url"


@pytest.mark.parametrize("mode", ["main", "reply"])
@pytest.mark.parametrize("more", [0.0, 1.0, [], {}, "false"])
async def test_malformed_has_more_cannot_prove_absence(monkeypatch, mode, more):
    if mode == "main":
        monkeypatch.setattr(main_visibility, "fetch_comment_page", AsyncMock(return_value={"comments": [], "has_more": more}))
        result = await main_visibility.scan_main_comment_ids(None, VIDEO, [CID])
    else:
        monkeypatch.setattr(reply_visibility, "fetch_reply_page", AsyncMock(return_value={"comments": [], "has_more": more}))
        result = await reply_visibility.scan_reply_comment_ids(None, VIDEO, PARENT, [CID])
    assert result["absent_ids"] == [] and not result["complete"]


@pytest.mark.parametrize("mode", ["main", "reply"])
async def test_wrong_media_rows_cannot_prove_presence_or_absence(monkeypatch, mode):
    row = {"cid": CID, "aweme_id": "1111111111111111111", "reply_id": PARENT if mode == "reply" else "0"}
    fetch = AsyncMock(return_value={"comments": [row], "has_more": 0})
    if mode == "main":
        monkeypatch.setattr(main_visibility, "fetch_comment_page", fetch)
        result = await main_visibility.scan_main_comment_ids(None, VIDEO, [CID])
    else:
        monkeypatch.setattr(reply_visibility, "fetch_reply_page", fetch)
        result = await reply_visibility.scan_reply_comment_ids(None, VIDEO, PARENT, [CID])
    assert not result["found_ids"] and not result["absent_ids"]


@pytest.mark.parametrize("mode", ["main", "reply"])
async def test_duplicate_page_with_new_cursor_stops_early(monkeypatch, mode):
    row = {"cid": OTHER, "reply_id": PARENT if mode == "reply" else "0"}
    fetch = AsyncMock(side_effect=[{"comments": [row], "has_more": 1, "cursor": n} for n in (20, 40, 60)])
    if mode == "main":
        monkeypatch.setattr(main_visibility, "fetch_comment_page", fetch)
        result = await main_visibility.scan_main_comment_ids(None, VIDEO, [CID], max_pages=3)
    else:
        monkeypatch.setattr(reply_visibility, "fetch_reply_page", fetch)
        result = await reply_visibility.scan_reply_comment_ids(None, VIDEO, PARENT, [CID], max_pages=3)
    assert fetch.await_count == 2 and not result["absent_ids"]


async def test_three_pages_read_runtime_and_cookies_only_once(monkeypatch):
    page = page_with_body(monkeypatch, {})
    page.evaluate.side_effect = [
        {"status": 200, "text": json.dumps({"status_code": 0, "comments": [], "has_more": more, "cursor": cursor}),
         "url": "https://www.tiktok.com/api/comment/list/?X-Bogus=x&X-Gnarly=y"}
        for more, cursor in [(1, 20), (1, 40), (0, 60)]
    ]
    result = await main_visibility.scan_main_comment_ids(page, VIDEO, [CID])
    assert result["absent_ids"] == [CID]
    assert page.context.cookies.await_count == 1
    assert api._extract_runtime_profile.await_count == 1
    assert page.evaluate.await_count == 3


async def test_duplicate_ids_with_whitespace_do_not_authorize_writes():
    mains = [SimpleNamespace(comment_id=cid, account_id=i, public_confirmed=False, text="x", variant_index=i)
             for i, cid in enumerate([CID, " " + CID + " "], 1)]
    observe, replace = AsyncMock(), AsyncMock()
    report = await main_visibility.verify_main_publications(mains, observe=observe, replace=replace, initial_delay=0, recheck_delay=0)
    assert report["reason"] == "duplicate_comment_ids"
    observe.assert_not_awaited()
    replace.assert_not_awaited()


@pytest.mark.parametrize("mode", ["main", "reply"])
async def test_http_disabled_never_falls_back_to_ui(monkeypatch, mode):
    from executor import user_schema as schema
    monkeypatch.setattr(schema._settings, "TIKTOK_NETWORK_FIRST", False)
    monkeypatch.setattr(schema._settings, "MAIN_VISIBILITY_INITIAL_DELAY_SECONDS", 0)
    monkeypatch.setattr(schema._settings, "REPLY_PUBLICATION_VISIBILITY_INITIAL_DELAY_SECONDS", 0)
    select = Mock(side_effect=AssertionError("HTTP-disabled visibility must not allocate a browser account"))
    monkeypatch.setattr(schema, "get_free_account_for_reply", select)
    if mode == "main":
        result = schema.UserSchemaResult(published_mains=[schema.PublishedMain("x", CID, True, False, "http", 1, "author")])
        await schema._verify_order_main_visibility(None, SimpleNamespace(settings={"verify_main_visibility": True}),
            f"https://www.tiktok.com/@demo/video/{VIDEO}", result, task_id=1, pool_owner_user_id=None)
        report = result.main_visibility
    else:
        task = SimpleNamespace(id=1, settings={"verify_reply_visibility": True}, reply1="hello", reply2=None, reply3=None)
        result = {"status": "success", "engagement": {"replies": {"expected": 1, "submitted": 1,
                  "items": [{"reply_id": CID, "parent_comment_id": PARENT, "account_id": 1, "text": "hello"}]}}}
        await reply_visibility.verify_order_reply_visibility(None, task, "video", result)
        report = result[reply_visibility.REPORT_KEY]
    assert report["reason"] == "http_disabled" and report["status"] == "unconfirmed"
    select.assert_not_called()


async def test_main_observer_with_duplicate_cookies_is_not_independent(monkeypatch):
    from executor import user_schema as schema
    original = schema.PublishedMain("x", CID, True, False, "http", 1, None)
    result = schema.UserSchemaResult(published_mains=[original])
    cookies = json.dumps({"uid_tt": "12345", "sessionid": "the-same-session"})
    publisher, observer = [SimpleNamespace(id=i, cookies=cookies, proxy=None) for i in (1, 8)]
    db = SimpleNamespace(get=lambda *args: publisher)
    monkeypatch.setattr(schema._settings, "MAIN_VISIBILITY_INITIAL_DELAY_SECONDS", 0)
    monkeypatch.setattr(schema, "get_free_account_for_reply", Mock(side_effect=[observer, None]))
    monkeypatch.setattr(schema, "_get_cached_http_preflight_username", lambda account: None)
    monkeypatch.setattr(schema, "_preflight_cookie_http_gate", AsyncMock(return_value=None))
    monkeypatch.setattr(schema, "create_page_for_account", AsyncMock(return_value=(None, SimpleNamespace(url="video"))))
    monkeypatch.setattr(schema, "_ensure_account_login_ready", AsyncMock(return_value=(True, None)))
    monkeypatch.setattr(schema, "_safe_close", AsyncMock())
    monkeypatch.setattr(schema, "release_reply_account_reservation", Mock())
    scan = AsyncMock()
    monkeypatch.setattr(main_visibility, "scan_main_comment_ids", scan)
    await schema._verify_order_main_visibility(db, SimpleNamespace(settings={"verify_main_visibility": True}),
                                             "video", result, task_id=1, pool_owner_user_id=None)
    scan.assert_not_awaited()
    assert not result.main_found_by_observer


async def test_cancellation_during_slow_browser_cleanup_cannot_defeat_deadline():
    cleanup_started, finish_cleanup, cleanup_done = asyncio.Event(), asyncio.Event(), asyncio.Event()
    async def observe(*_):
        try:
            await asyncio.sleep(30)
        finally:
            cleanup_started.set()
            await finish_cleanup.wait()
            cleanup_done.set()
    mains = [SimpleNamespace(comment_id=CID, account_id=1, public_confirmed=False, text="x", variant_index=1)]
    try:
        report = await asyncio.wait_for(main_visibility.verify_main_publications(
            mains, observe=observe, replace=AsyncMock(), initial_delay=0, recheck_delay=0, max_seconds=1), timeout=2)
        assert report["reason"] == "visibility_timeout" and report["cleanup_pending"]
        assert cleanup_started.is_set() and not cleanup_done.is_set()
    finally:
        finish_cleanup.set()
        await asyncio.wait_for(cleanup_done.wait(), timeout=1)


async def test_batch_profile_does_not_leak_to_a_later_observer_read(monkeypatch):
    page = page_with_body(monkeypatch, {"status_code": 0, "comments": [], "has_more": 0})
    await main_visibility.scan_main_comment_ids(page, VIDEO, [CID])
    await main_visibility.scan_main_comment_ids(page, VIDEO, [CID])
    assert api._extract_runtime_profile.await_count == page.context.cookies.await_count == 2


@pytest.mark.parametrize("reason", ["runtime_signer_not_ready", "http_session_media_mismatch", "url_drift_to_other_video"])
async def test_replacement_cannot_post_with_cold_signer_or_wrong_media(monkeypatch, reason):
    from executor import visibility_publisher as publisher, user_schema as schema
    url = f"https://www.tiktok.com/@fixture/video/{VIDEO}"
    page = SimpleNamespace(url=url if reason != "url_drift_to_other_video" else "https://www.tiktok.com/@fixture/video/1111111111111111111")
    account = SimpleNamespace(id=1, proxy=None)
    session = SimpleNamespace(aweme_id=VIDEO if reason != "http_session_media_mismatch" else "1111111111111111111")
    monkeypatch.setattr(schema, "_preflight_cookie_http_gate", AsyncMock(return_value=None))
    monkeypatch.setattr(schema, "create_page_for_account", AsyncMock(return_value=(None, page)))
    monkeypatch.setattr(schema, "track_publication_requests", Mock(return_value=Mock()))
    monkeypatch.setattr(schema, "_ensure_account_login_ready", AsyncMock(return_value=(True, None)))
    monkeypatch.setattr(schema, "_prepare_user_schema_http_session", AsyncMock(return_value=session))
    monkeypatch.setattr(schema, "_safe_close", AsyncMock())
    post = AsyncMock(side_effect=AssertionError("No POST is authorized"))
    monkeypatch.setattr(schema, "_publish_main_http_first_if_eligible", post)
    monkeypatch.setattr(publisher, "wait_for_runtime_signer_ready", AsyncMock(return_value=reason != "runtime_signer_not_ready"))
    receipt, refusal = await publisher.publish_visibility_http(None, account, url, "hello", correlation_id="fixture")
    assert receipt is None and refusal == reason
    post.assert_not_awaited()
    schema._safe_close.assert_awaited_once()
