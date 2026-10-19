"""HTTP confirmation policy for main-only, full and replies-only orders."""
import json
from contextlib import contextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from executor import engagement_engine, user_schema
from executor.http.playwright_http_client import PlaywrightHttpClient, _extract_comment_author
from executor.http.retry_policy import RetryPolicy


CID = "7641761192486194454"
URL = "https://www.tiktok.com/@demo/video/7635267329407913230"
pytestmark = pytest.mark.unit


@pytest.fixture(autouse=True)
def http_mode(monkeypatch):
    monkeypatch.setattr(user_schema._settings, "TIKTOK_NETWORK_FIRST", True)
    monkeypatch.setattr(user_schema._settings, "TIKTOK_NETWORK_IMPL_READY", True)
    monkeypatch.setattr(user_schema._settings, "PARALLEL_COMMENTING_DOM_FIRST", False)


def mains():
    return [user_schema.PublishedMain(
        text=f"main {index}", comment_id=str(int(CID) + index), network_confirmed=True,
        local_confirmed=False, method="http_user_schema", account_id=index + 1,
        tiktok_username="api_author",
    ) for index in range(3)]


def reply(main, *, success=True, method="http_user_schema:network_exact", variant=1):
    return user_schema.ReplyOutcome(
        text=f"reply {variant}", variant_index=variant, matched_main_text=main.text,
        parent_comment_id=main.comment_id, success=success, method=method,
        error=None if success else "http_failed", account_id=20,
        reply_comment_id=str(int(main.comment_id) + variant * 100), reply_confirmation="network_comment_id",
    )


def test_http_main_only_is_success_without_observer():
    result = user_schema.UserSchemaResult(published_mains=mains(), expected_main_publications=3)
    payload = result.to_dict()
    assert result.http_main_complete
    assert not result.main_found_by_observer
    assert payload["status"] == "success"
    assert payload["error"] is None
    assert not payload["engagement"]["main"]["submitted_unconfirmed"]


@pytest.mark.asyncio
@pytest.mark.parametrize("parallel", [False, True])
@pytest.mark.parametrize("slots", [1, 3])
@pytest.mark.parametrize("visibility_mode", ["off", "confirmed", "unavailable"])
async def test_main_only_workflow_returns_confirmed_http_mains(monkeypatch, parallel, visibility_mode, slots):
    from db import database
    from executor import browser_pool

    @contextmanager
    def context():
        yield SimpleNamespace()

    selected = []

    def select(*args, **kwargs):
        account = SimpleNamespace(id=len(selected) + 1, proxy=None)
        selected.append(account)
        return account

    async def publish(db, account, url, texts, **kwargs):
        assert url == URL
        if parallel and slots == 3:
            assert len(texts) == 1
        return [user_schema.PublishedMain(
            text=text, comment_id=str(int(CID) + index + account.id * 10), network_confirmed=True,
            local_confirmed=False, method="http_user_schema", account_id=account.id, tiktok_username="api_author",
        ) for index, text in enumerate(texts)], None

    monkeypatch.setattr(database, "get_sync_db_context", context)
    monkeypatch.setattr(user_schema, "get_free_account_for_main_comment", select)
    monkeypatch.setattr(user_schema, "_try_main_account", publish)
    monkeypatch.setattr(user_schema, "release_main_account_reservation", Mock())
    monkeypatch.setattr(user_schema, "extend_main_account_reservation", Mock(return_value=True))
    monkeypatch.setattr(user_schema, "_confirm_mains_by_observers", AsyncMock(side_effect=AssertionError("Main-only HTTP needs no DOM observer")))
    monkeypatch.setattr(user_schema.asyncio, "sleep", AsyncMock())
    monkeypatch.setattr(browser_pool, "ensure_thread_browser_pool_capacity", Mock())
    monkeypatch.setattr(browser_pool, "close_browser_pool", AsyncMock())
    for name, value in {"USE_PROXY": False, "MAX_RETRY_ACCOUNTS": 4, "PARALLEL_MAIN_ACCOUNT_SLOTS": slots,
                        "PARALLEL_COMMENTING_MAX_MAIN_ATTEMPTS": 1}.items():
        monkeypatch.setattr(user_schema._settings, name, value)
    execute = user_schema.execute_user_schema_parallel if parallel else user_schema.execute_user_schema_full
    shortlink = "https://vt.tiktok.com/local-test/"
    resolve = AsyncMock(return_value=(URL, "7635267329407913230"))
    monkeypatch.setattr(engagement_engine, "resolve_video_url", resolve)
    original_check = user_schema._verify_order_main_visibility
    checks = []

    async def verify(db, task, url, result, **kwargs):
        checks.append((url, len(result.published_mains), task.settings["verify_main_visibility"]))
        if visibility_mode == "off":
            return await original_check(db, task, url, result, **kwargs)
        confirmed = visibility_mode == "confirmed"
        result.main_visibility = {"enabled": True, "status": "confirmed" if confirmed else "unconfirmed"}
        for item in result.published_mains:
            item.public_confirmed = confirmed
        result.main_found_by_observer = confirmed
        if not confirmed:
            result.error = "main_visibility_unconfirmed"

    monkeypatch.setattr(user_schema, "_verify_order_main_visibility", verify)
    monkeypatch.setattr(user_schema, "_reply_with_variant_on_all_mains", AsyncMock(side_effect=AssertionError("no replies before verification")))
    replies = ["reply"] if visibility_mode == "unavailable" else []

    result = await execute(None, SimpleNamespace(settings={"replies": replies, "reply_count": len(replies),
                                                         "verify_main_visibility": visibility_mode != "off"}), shortlink,
                           ["first", "second", "third"], 101, boost_enabled=False, boost_quantity=0)

    assert result.expected_main_publications == len(result.published_mains) == 3
    assert sorted(main.text for main in result.published_mains) == ["first", "second", "third"]
    payload = result.to_dict()
    assert result.main_found_by_observer is (visibility_mode == "confirmed")
    assert payload["status"] == ("partial" if visibility_mode == "unavailable" else "success")
    assert checks == [(URL, 3, visibility_mode != "off")]
    assert payload["engagement"]["main"]["verified"] is (visibility_mode == "confirmed")
    resolve.assert_awaited_once_with(shortlink)


def test_full_http_order_is_success_after_all_replies():
    result = user_schema.UserSchemaResult(published_mains=mains(), expected_main_publications=3, expected_reply_variants=2)
    result.replies = [reply(main, variant=variant) for main in result.published_mains for variant in (1, 2)]
    result.accept_http_main_confirmation()
    assert result.http_main_complete
    assert not result.main_found_by_observer
    assert result.to_dict()["status"] == "success"


def test_one_successful_http_reply_confirms_mains_but_not_missing_replies():
    result = user_schema.UserSchemaResult(published_mains=mains(), expected_main_publications=3, expected_reply_variants=1)
    result.replies = [reply(main, success=index == 0) for index, main in enumerate(result.published_mains)]
    payload = result.to_dict()
    assert result.http_main_complete
    assert not result.main_found_by_observer
    assert payload["status"] == "partial"
    assert payload["error"] != "main_public_confirmation_unconfirmed"


@pytest.mark.parametrize("reply_case", ["pending", "failed", "dom", "other_parent"])
def test_full_mains_wait_for_successful_http_reply_to_their_parent(reply_case):
    result = user_schema.UserSchemaResult(published_mains=mains(), expected_main_publications=3, expected_reply_variants=1)
    if reply_case != "pending":
        outcome = reply(result.published_mains[0], success=reply_case != "failed",
                        method="hybrid_dom" if reply_case == "dom" else "http_user_schema")
        if reply_case == "other_parent":
            outcome.parent_comment_id = "1111111111111111111"
        result.replies = [outcome]
    payload = result.to_dict()
    assert not result.main_found_by_observer
    assert payload["status"] != "success"


@pytest.mark.parametrize("bad", ["missing", "invalid_id", "unicode_id", "duplicate_id", "no_network", "dom", "disabled"])
def test_http_policy_does_not_accept_incomplete_or_unconfirmed_mains(monkeypatch, bad):
    result = user_schema.UserSchemaResult(published_mains=mains(), expected_main_publications=3)
    if bad == "missing":
        result.published_mains.pop()
    elif bad in {"invalid_id", "unicode_id"}:
        result.published_mains[0].comment_id = "cid_fake" if bad == "invalid_id" else "١" * 19
    elif bad == "duplicate_id":
        result.published_mains[0].comment_id = result.published_mains[1].comment_id
    elif bad == "no_network":
        result.published_mains[0].network_confirmed = False
    elif bad == "dom":
        result.published_mains[0].method = "hybrid_dom"
    else:
        monkeypatch.setattr(user_schema._settings, "TIKTOK_NETWORK_FIRST", False)
    result.to_dict()
    assert not result.main_found_by_observer


@pytest.mark.parametrize("nested", [False, True])
@pytest.mark.parametrize("user,expected", [
    ({"unique_id": "api_user", "nickname": "Display Name"}, "api_user"),
    ({"unique_id": "  ", "nickname": " Display Name "}, None),
    ({"nickname": "nickname"}, None),
    ({}, None), (None, None), ([], None),
])
def test_extract_comment_author_supports_both_response_envelopes(nested, user, expected):
    payload = {"comment": {"user": user}}
    assert _extract_comment_author({"data": payload} if nested else payload) == expected


@pytest.mark.asyncio
@pytest.mark.parametrize("page_username", [None, "page_user"])
async def test_main_author_flows_from_api_to_published_main(monkeypatch, page_username):
    response = {"status_code": 0, "data": {"comment": {"cid": CID, "user": {"unique_id": "api_user", "uid": "123456789"}}}}
    page = SimpleNamespace(url=URL, context=SimpleNamespace(cookies=AsyncMock(return_value=[])),
                           evaluate=AsyncMock(return_value={"status": 200, "headers": {"content-type": "application/json"}, "text": json.dumps(response)}))
    client = PlaywrightHttpClient(SimpleNamespace(fetch=AsyncMock()), page=page, retry_policy=RetryPolicy(max_retries=0))
    session = user_schema._UserSchemaHttpSession(user_schema.MAIN_SCOPE, 1, client, {}, "7635267329407913230", URL)
    monkeypatch.setattr(user_schema, "extract_tiktok_username", AsyncMock(return_value=page_username))
    monkeypatch.setattr(user_schema, "_prepare_user_schema_http_session", AsyncMock(return_value=session))
    monkeypatch.setattr(user_schema, "_register_runtime_success", Mock())
    monkeypatch.setattr(user_schema, "_deny_if_visible_logged_out", AsyncMock(return_value=None))
    monkeypatch.setattr(engagement_engine, "_record_http_account_result", Mock())
    dom = AsyncMock(side_effect=AssertionError("HTTP publication must not invoke DOM publisher"))
    monkeypatch.setattr(user_schema, "publish_comment_hybrid", dom)

    published, error = await user_schema._publish_all_variants_on_account(None, page, SimpleNamespace(id=1), ["hello"], correlation_id="http_main", expected_video_url=URL)

    assert error is None
    assert client.last_request_meta["comment_author"] == "api_user"
    assert published[0].tiktok_username == (page_username or "api_user")
    assert published[0].author_uid == "123456789"
    dom.assert_not_awaited()
    from services import boost_project_client
    order_likes = AsyncMock(return_value=(True, 42, None))
    monkeypatch.setattr(boost_project_client, "is_boost_enabled", Mock(return_value=True))
    monkeypatch.setattr(boost_project_client, "order_tiktok_comment_likes", order_likes)
    order_result = user_schema.UserSchemaResult(published_mains=published, expected_main_publications=1)
    await user_schema._maybe_order_likes(order_result, video_url=URL, task_id=101, boost_enabled=True, boost_quantity=100)
    order_likes.assert_awaited_once_with(URL, page_username or "api_user", quantity=100)


@pytest.mark.asyncio
@pytest.mark.parametrize("reply_count,successful", [(0, 0), (1, 1), (1, 0), (2, 1)])
async def test_engagement_full_applies_http_confirmation_and_keeps_reply_completion(monkeypatch, reply_count, successful):
    account = SimpleNamespace(id=1, label="account-label")
    replies = ["reply one", "reply two"][:reply_count]
    task = SimpleNamespace(reply1=replies[0] if replies else None, reply2=replies[1] if len(replies) > 1 else None,
                           reply3=None, settings={"reply_count": reply_count})

    async def publish(*args, outcome_dict, **kwargs):
        outcome_dict.update(posted=True, network_confirmed=True, method="http", comment_author="api_author")
        return CID, None, "http"

    verify = AsyncMock(return_value=(False, None, None, "not_found"))
    monkeypatch.setattr(engagement_engine, "resolve_video_url", AsyncMock(return_value=(URL, "7635267329407913230")))
    monkeypatch.setattr(engagement_engine, "get_free_account_for_main_comment", Mock(return_value=account))
    monkeypatch.setattr(engagement_engine, "release_main_account_reservation", Mock())
    monkeypatch.setattr(engagement_engine, "attempt_main_comment_on_account", publish)
    monkeypatch.setattr(engagement_engine, "verify_comment_dom", verify)
    monkeypatch.setattr(engagement_engine, "_get_task_boost_config", Mock(return_value=(False, 100)))
    monkeypatch.setattr(engagement_engine, "_order_boost_likes", AsyncMock(return_value=(None, None)))
    monkeypatch.setattr(engagement_engine, "execute_replies_http", AsyncMock(return_value=[
        {"text": text, "success": index < successful, "method": "http", "error": None if index < successful else "http_failed", "reply_id": str(int(CID) + index + 100), "network_confirmed": index < successful}
        for index, text in enumerate(replies)
    ]))

    result = await engagement_engine.execute_hybrid_full(None, task, URL, "main", 101)
    confirmed = True
    assert result.main_comment["verified"] is confirmed
    assert result.main_comment["submitted_unconfirmed"] is not confirmed
    assert result.comment_author == "api_author"
    assert (result.to_dict()["status"] == "success") is (successful == reply_count)
    verify.assert_not_awaited()


def test_replies_only_status_does_not_require_a_main_publication():
    result = engagement_engine.EngagementResult()
    result.boost_enabled = False
    result.comment_found = True
    result.replies = [{"success": True, "method": "http", "text": "reply", "reply_id": CID, "network_confirmed": True}]
    assert result.to_dict(mode="engagement_only")["status"] == "success"
    assert not result.main_comment["verified"]


def test_engagement_missing_reply_result_is_not_success():
    result = engagement_engine.EngagementResult()
    result.boost_enabled = False
    result.expected_reply_variants = 2
    result.main_comment.update(success=True, posted=True, network_confirmed=True, method="http", comment_id=CID)
    result.replies = [{"success": True, "method": "http", "text": "first"}]
    assert result.to_dict()["status"] != "success"


def test_http_reply_metadata_keeps_server_id_and_does_not_claim_dom_verification():
    outcome = reply(mains()[0])
    outcome.reply_comment_id = "7641761192486194458"
    result = engagement_engine._engagement_reply_outcome_to_dict(outcome, attempts=1)
    assert result["reply_id"] == outcome.reply_comment_id
    assert result["verify_mode"] == "http_parent_id"
    assert result["parent_confirmation_level"] == "network_exact"
    assert result["local_visible"] is None

    api_failure = user_schema.ReplyOutcome(
        text="reply", variant_index=1, matched_main_text=None, parent_comment_id=None,
        success=False, method="parent_http", error="parent_api_unavailable:empty_body",
        account_id=7, attempted=False, publication_attempted=False,
    )
    failed = engagement_engine._engagement_reply_outcome_to_dict(api_failure, attempts=1)
    assert failed["verify_mode"] == "http_comment_list"
    assert failed["parent_confirmation_level"] == "unconfirmed"
    assert failed["local_visible"] is None
    assert failed["reply_id"] is None
