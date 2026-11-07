"""Exercise discovered CIDs through the production scheduler and HTTP publisher."""
from contextlib import contextmanager
import json
from unittest.mock import AsyncMock, Mock

import pytest
from sqlalchemy.orm import Session

from db.models import Account
from executor import engagement_engine as engine, keyword_replies as keyword, user_schema as schema
from executor.http import comment_discovery
from executor.http.retry_policy import HttpErrorType
from services.order_transport import reset_task_order_transport, set_task_order_transport
from tests.unit.test_http_first_navigation import reply_env
from tests.unit.test_keyword_replies import keyword_db, URL, MEDIA


pytestmark = [pytest.mark.unit, pytest.mark.asyncio]
_REAL_HTTP_PUBLISH = schema._publish_reply_http_first_if_eligible
_REAL_HTTP_PREPARE = schema._prepare_user_schema_http_session
FIRST = "7700000000000099001"
DEEP = "7700000000000099002"


def connect_wire_fixture(reply_env, monkeypatch):
    reply_env.page.url = URL
    reply_env.session.aweme_id = MEDIA
    reply_env.session.video_url = URL

    async def reply(**kwargs):
        parent = kwargs["parent_comment_id"]
        return True, str(int(parent) + 1000000), None, None, {
            "parent_confirmed": True, "parent_confirmation_level": "network_exact",
            "response_parent_id": parent,
        }

    wire = AsyncMock(side_effect=reply)
    reply_env.session.client.reply_comment = wire
    monkeypatch.setattr(schema, "_publish_reply_http_first_if_eligible", _REAL_HTTP_PUBLISH)
    monkeypatch.setattr(schema, "_like_parent_before_reply_enabled", lambda: False)
    monkeypatch.setattr(engine, "_record_http_account_result", Mock())
    no_search = AsyncMock(side_effect=AssertionError("known CID must not trigger parent discovery"))
    monkeypatch.setattr(comment_discovery, "find_parent_comments", no_search)
    return wire, no_search


async def test_sparse_deep_identical_text_targets_publish_exact_cids_without_rescanning(
        keyword_db, reply_env, monkeypatch):
    import db.database as database
    from executor import browser_pool

    db, task, sql_engine = keyword_db
    wire, no_search = connect_wire_fixture(reply_env, monkeypatch)
    pages = {}
    for index in range(6):
        comments = [{"cid": str(7700000000000100000 + index * 50 + offset),
                     "text": "unrelated", "reply_id": "0"} for offset in range(50)]
        if index in {0, 5}:
            comments[0] = {"cid": FIRST if index == 0 else DEEP, "text": "Какая цена?",
                           "reply_id": "0", "user": {"nickname": "Display name only"}}
        pages[index * 50] = {"comments": comments, "cursor": (index + 1) * 50,
                             "has_more": int(index < 5)}

    fetch = AsyncMock(side_effect=lambda page, media, cursor=0, **kwargs: pages[cursor])
    monkeypatch.setattr(comment_discovery, "fetch_comment_page", fetch)

    async def discovery(db, url, keys, **kwargs):
        return await keyword.collect_http_targets(reply_env.page, MEDIA, keys)

    @contextmanager
    def session_context():
        with Session(sql_engine, expire_on_commit=False) as other:
            yield other

    monkeypatch.setattr(database, "get_sync_db_context", session_context)
    monkeypatch.setattr(keyword, "discover_keyword_targets", discovery)
    monkeypatch.setattr(engine, "resolve_order_video_url", AsyncMock(return_value=(URL, MEDIA)))
    monkeypatch.setattr(engine, "get_free_account_for_reply", lambda *args, **kwargs: reply_env.account)
    monkeypatch.setattr(browser_pool, "ensure_thread_browser_pool_capacity", Mock())
    close = AsyncMock()
    monkeypatch.setattr(browser_pool, "close_browser_pool", close)
    task.settings = {**task.settings, "keywords": ["цена"]}
    db.commit()
    token = set_task_order_transport("http")
    try:
        result = await keyword.execute_keyword_replies(db, task, URL, task_id=task.id)
        repeated = await keyword.execute_keyword_replies(db, task, URL, task_id=task.id)
    finally:
        reset_task_order_transport(token)

    assert result.to_dict()["status"] == repeated.to_dict()["status"] == "success"
    assert result.scan["complete"] and result.scan["scanned"] == 300
    assert [call.kwargs["parent_comment_id"] for call in wire.await_args_list] == [FIRST, DEEP]
    assert [call.kwargs["text"] for call in wire.await_args_list] == ["Первый", "Второй"]
    assert [call.kwargs["cursor"] for call in fetch.await_args_list] == [0, 50, 100, 150, 200, 250]
    no_search.assert_not_awaited()
    for name in ("_open_comment_list_with_safe_retry", "_find_main_anchors_batch", "publish_reply_hybrid"):
        reply_env.mocks[name].assert_not_awaited()
    assert close.await_count == 0  # Executor keeps ownership through verification and later URLs.


@pytest.mark.parametrize("text", ["", "Какая цена?"])
async def test_known_cid_http_publisher_does_not_search_by_empty_or_duplicate_text(
        keyword_db, reply_env, monkeypatch, text):
    import db.database as database
    from executor import browser_pool

    db, task, sql_engine = keyword_db
    wire, no_search = connect_wire_fixture(reply_env, monkeypatch)
    @contextmanager
    def session_context():
        with Session(sql_engine, expire_on_commit=False) as other:
            yield other
    monkeypatch.setattr(database, "get_sync_db_context", session_context)
    monkeypatch.setattr(engine, "get_free_account_for_reply", lambda *args, **kwargs: reply_env.account)
    monkeypatch.setattr(browser_pool, "ensure_thread_browser_pool_capacity", Mock())
    token = set_task_order_transport("http")
    try:
        rows, parent, _ = await engine.execute_engagement_only_replies_via_full_flow(
            db, URL, comment_text=text, comment_id=DEEP, comment_author=None, replies=["reply"],
            task_id=task.id, parent_phrase_match=False, close_pool_after_phase=False)
    finally:
        reset_task_order_transport(token)
    assert rows[0]["success"] and rows[0]["parent_comment_id"] == DEEP and parent == DEEP
    assert wire.await_args.kwargs["parent_comment_id"] == DEEP
    no_search.assert_not_awaited()
    reply_env.mocks["_find_main_anchors_batch"].assert_not_awaited()


async def test_pre_publish_warmup_failure_is_not_reopened_for_each_keyword_target(
        keyword_db, reply_env, monkeypatch):
    """Real SQL selection and session preparation retain only failed exclusions."""
    import db.database as database
    from executor import browser_pool
    from services import account_service

    db, task, sql_engine = keyword_db
    wire, _ = connect_wire_fixture(reply_env, monkeypatch)
    cookies = {"sessionid": "a" * 32, "ttwid": "wid", "tt_csrf_token": "csrf", "msToken": "web"}
    bad = Account(label="warmup-fails", cookies=json.dumps(cookies), country_code="US")
    good = Account(label="warmup-works", cookies=json.dumps(cookies), country_code="US")
    db.add_all([bad, good])
    db.commit()
    bad_id, good_id = bad.id, good.id

    @contextmanager
    def session_context():
        with Session(sql_engine, expire_on_commit=False) as other:
            yield other

    warmed = []
    async def warmup(page, url, *, account, **kwargs):
        warmed.append(account.id)
        ready = account.id == good_id
        return cookies, ready, {"current_url": URL, "reason_code": "ok" if ready else "signer_not_ready"}

    monkeypatch.setattr(account_service.settings, "USE_PROXY", False)
    monkeypatch.setattr(account_service, "get_current_dedicated_proxy", lambda: None)
    monkeypatch.setattr(database, "get_sync_db_context", session_context)
    monkeypatch.setattr(schema, "_prepare_user_schema_http_session", _REAL_HTTP_PREPARE)
    monkeypatch.setattr(schema, "get_http_execution_strategy", lambda *args: {"strategy": "http_first"})
    monkeypatch.setattr(schema, "_has_recent_http_browser_auth", lambda page: True)
    monkeypatch.setattr(schema.PlaywrightHttpClient, "from_page", AsyncMock(return_value=reply_env.session.client))
    monkeypatch.setattr(engine, "warmup_page_for_http", warmup)
    monkeypatch.setattr(engine, "_should_skip_http_for_account", lambda account_id: None)
    monkeypatch.setattr(engine, "get_free_account_for_reply", account_service.get_free_account_for_reply)
    monkeypatch.setattr(engine, "resolve_order_video_url", AsyncMock(return_value=(URL, MEDIA)))
    monkeypatch.setattr(keyword, "discover_keyword_targets", AsyncMock(return_value=(
        [{"cid": cid, "text": "Какая цена?"} for cid in (FIRST, DEEP)], {"complete": True, "scanned": 300})))
    monkeypatch.setattr(browser_pool, "ensure_thread_browser_pool_capacity", Mock())
    monkeypatch.setattr(browser_pool, "close_browser_pool", AsyncMock())
    token = set_task_order_transport("http")
    try:
        result = await keyword.execute_keyword_replies(db, task, URL, task_id=task.id)
    finally:
        reset_task_order_transport(token)
    assert result.to_dict()["status"] == "success"
    assert [call.kwargs["parent_comment_id"] for call in wire.await_args_list] == [FIRST, DEEP]
    db.refresh(bad)
    assert bad.status == "active" and bad.error_count_recent == 0 and bad.last_error is None
    assert warmed.count(bad_id) == 1
    assert warmed.count(good_id) == 2


async def test_unknown_post_result_is_not_recorded_as_a_prepublication_account_failure(
        keyword_db, reply_env, monkeypatch):
    import db.database as database
    from executor import browser_pool

    db, task, sql_engine = keyword_db
    wire, no_search = connect_wire_fixture(reply_env, monkeypatch)
    wire.side_effect = None
    wire.return_value = (False, None, "response_lost_after_dispatch", HttpErrorType.SUBMITTED_UNCONFIRMED, {})
    failed = set()
    @contextmanager
    def session_context():
        with Session(sql_engine, expire_on_commit=False) as other:
            yield other
    monkeypatch.setattr(database, "get_sync_db_context", session_context)
    monkeypatch.setattr(engine, "get_free_account_for_reply", lambda *args, **kwargs: reply_env.account)
    monkeypatch.setattr(browser_pool, "ensure_thread_browser_pool_capacity", Mock())
    token = set_task_order_transport("http")
    try:
        rows, _, _ = await engine.execute_engagement_only_replies_via_full_flow(
            db, URL, comment_text="Какая цена?", comment_id=DEEP, comment_author=None, replies=["reply"],
            task_id=task.id, parent_phrase_match=False, close_pool_after_phase=False,
            prepublish_failed_account_ids=failed)
    finally:
        reset_task_order_transport(token)
    assert not rows[0]["success"] and rows[0]["submitted_unconfirmed"]
    assert failed == set()
    wire.assert_awaited_once()
    no_search.assert_not_awaited()
