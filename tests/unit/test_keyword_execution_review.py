"""Recovery and pool-lifetime regressions found during the keyword-order review."""
import asyncio
from contextlib import contextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest
from sqlalchemy.orm import Session

from executor import engagement_engine as engagement, keyword_replies as keyword
from services.order_transport import reset_task_order_transport, set_task_order_transport
from tests.unit.test_keyword_replies import CIDS, MEDIA, URL, execution, keyword_db, receipt


pytestmark = pytest.mark.unit


async def test_no_account_before_publication_remains_pending_and_recovers(keyword_db, execution):
    db, task, _ = keyword_db
    discovery, sender = execution
    sender.side_effect = None
    sender.return_value = ([{"success": False, "method": None, "reply_id": None,
        "error": "no_free_reply_accounts", "publication_attempted": False}], CIDS[0], None)
    first = (await keyword.execute_keyword_replies(db, task, URL, task_id=task.id)).to_dict()
    rows = keyword._targets(db, task.id, keyword.video_key(URL))
    assert [row.status for row in rows] == ["pending"] * 3
    assert first["error"] == "no_free_reply_accounts"
    assert sender.await_count == 1

    async def available(*args, **kwargs):
        return [receipt(kwargs["comment_id"])], kwargs["comment_id"], "fixture"
    sender.side_effect = available
    recovered = (await keyword.execute_keyword_replies(db, task, URL, task_id=task.id)).to_dict()
    assert recovered["status"] == "success"
    assert sender.await_count == 4 and discovery.await_count == 1


@pytest.mark.parametrize("extra", [
    {"publication_attempted": True}, {"submitted_unconfirmed": True},
    {"network_confirmed": True}, {"reply_id": CIDS[3]}, {"reply_comment_id": CIDS[3]},
])
async def test_no_account_message_never_releases_a_possibly_sent_reply(keyword_db, execution, extra):
    db, task, _ = keyword_db
    discovery, sender = execution
    discovery.return_value = ([{"cid": CIDS[0], "text": "цена"}], {"complete": True})
    sender.side_effect = None
    sender.return_value = ([{"success": False, "method": None, "error": "no_free_reply_accounts",
        "publication_attempted": False, **extra}], CIDS[0], None)
    await keyword.execute_keyword_replies(db, task, URL, task_id=task.id)
    await keyword.execute_keyword_replies(db, task, URL, task_id=task.id)
    assert sender.await_count == 1
    assert keyword._targets(db, task.id, keyword.video_key(URL))[0].status != "pending"


@pytest.mark.parametrize("dynamic", [True, False])
async def test_real_reply_scheduler_keeps_pool_for_the_keyword_batch(keyword_db, monkeypatch, dynamic):
    import db.database as database
    from executor import browser_pool, user_schema as schema
    db, task, sql_engine = keyword_db
    @contextmanager
    def db_context():
        with Session(sql_engine, expire_on_commit=False) as other:
            yield other
    monkeypatch.setattr(database, "get_sync_db_context", db_context)
    monkeypatch.setattr(engagement, "resolve_order_video_url", AsyncMock(return_value=(URL, MEDIA)))
    monkeypatch.setattr(keyword, "discover_keyword_targets", AsyncMock(return_value=(
        [{"cid": cid, "text": "Где купить?"} for cid in CIDS[:3]], {"complete": True})))
    monkeypatch.setattr(engagement, "get_free_account_for_reply", lambda *args, **kwargs: SimpleNamespace(id=900, proxy=None))
    monkeypatch.setattr(schema, "_preflight_cookie_http_gate", AsyncMock(return_value=None))
    monkeypatch.setattr(browser_pool, "ensure_thread_browser_pool_capacity", lambda *args: None)
    close = AsyncMock()
    monkeypatch.setattr(browser_pool, "close_browser_pool", close)
    monkeypatch.setattr(engagement.settings, "PARALLEL_DYNAMIC_WORKER_POOL_ENABLED", dynamic)
    async def publish(_db, account, url, parents, text, *, variant_index, **kwargs):
        parent = parents[0]
        return schema.ReplyAttemptResult(replies=[schema.ReplyOutcome(
            text, variant_index, parent.text, parent.comment_id, True, "http_user_schema", None, account.id,
            attempted=True, publication_attempted=True, counts_as_submitted=True,
            reply_comment_id=str(int(parent.comment_id) + 100), reply_confirmation="network_comment_id",
        )], found_mains=parents)
    monkeypatch.setattr(schema, "_reply_with_variant_on_all_mains", publish)
    token = set_task_order_transport("http", SimpleNamespace(TIKTOK_NETWORK_IMPL_READY=True))
    try:
        result = await keyword.execute_keyword_replies(db, task, URL, task_id=task.id)
    finally:
        reset_task_order_transport(token)
    assert result.to_dict()["engagement"]["replies"]["ok"] == 3
    assert close.await_count == 1


async def test_keyword_batch_closes_pool_on_cancellation_after_durable_claim(keyword_db, execution, monkeypatch):
    from executor import browser_pool
    db, task, _ = keyword_db
    _, sender = execution
    sender.side_effect = asyncio.CancelledError
    close = AsyncMock()
    monkeypatch.setattr(browser_pool, "close_browser_pool", close)
    with pytest.raises(asyncio.CancelledError):
        await keyword.execute_keyword_replies(db, task, URL, task_id=task.id)
    assert close.await_count == 1
    assert keyword._targets(db, task.id, keyword.video_key(URL))[0].status == "dispatching"


@pytest.mark.parametrize("final_url,expected_calls", [
    (URL.replace("@fixture", "@canonical") + "?lang=en", 1),
    (URL.replace(MEDIA, "1234567890123456790"), 0),
])
async def test_discovery_binds_comments_to_the_final_warmed_target(monkeypatch, final_url, expected_calls):
    from executor import user_schema as schema
    account = SimpleNamespace(id=900, proxy=None)
    page = SimpleNamespace(url=URL.replace(MEDIA, "1234567890123456799"))
    context = object()
    monkeypatch.setattr(engagement, "get_free_account_for_reply", Mock(side_effect=[account, None]))
    monkeypatch.setattr(schema, "_preflight_cookie_http_gate", AsyncMock(return_value=None))
    monkeypatch.setattr(engagement, "create_page_for_account", AsyncMock(return_value=(context, page)))
    monkeypatch.setattr(schema, "_ensure_account_login_ready", AsyncMock(return_value=(True, None)))
    async def warmup(*args, **kwargs):
        page.url = final_url
        return {}, True, {}
    monkeypatch.setattr(engagement, "warmup_page_for_http", warmup)
    collect = AsyncMock(return_value=([], {"complete": True, "scanned": 0}))
    monkeypatch.setattr(keyword, "collect_http_targets", collect)
    close, release = AsyncMock(), Mock()
    monkeypatch.setattr(schema, "_safe_close", close)
    monkeypatch.setattr(schema, "release_reply_account_reservation", release)
    token = set_task_order_transport("http", SimpleNamespace(TIKTOK_NETWORK_IMPL_READY=True))
    try:
        _, scan = await keyword.discover_keyword_targets(None, URL, ["цена"], task_id=1)
    finally:
        reset_task_order_transport(token)
    assert collect.await_count == expected_calls
    assert scan["complete"] is bool(expected_calls)
    if expected_calls:
        assert collect.await_args.args[1] == MEDIA
    close.assert_awaited_once()
    release.assert_called_once_with(account.id)


async def test_discovery_cancellation_releases_reserved_account_and_context(monkeypatch):
    from executor import user_schema as schema
    account = SimpleNamespace(id=900, proxy=None)
    page, context = SimpleNamespace(url=URL), object()
    monkeypatch.setattr(engagement, "get_free_account_for_reply", lambda *args, **kwargs: account)
    monkeypatch.setattr(schema, "_preflight_cookie_http_gate", AsyncMock(return_value=None))
    monkeypatch.setattr(engagement, "create_page_for_account", AsyncMock(return_value=(context, page)))
    monkeypatch.setattr(schema, "_ensure_account_login_ready", AsyncMock(return_value=(True, None)))
    monkeypatch.setattr(engagement, "warmup_page_for_http", AsyncMock(side_effect=asyncio.CancelledError))
    close, release = AsyncMock(), Mock()
    monkeypatch.setattr(schema, "_safe_close", close)
    monkeypatch.setattr(schema, "release_reply_account_reservation", release)
    token = set_task_order_transport("http", SimpleNamespace(TIKTOK_NETWORK_IMPL_READY=True))
    try:
        with pytest.raises(asyncio.CancelledError):
            await keyword.discover_keyword_targets(None, URL, ["цена"], task_id=1)
    finally:
        reset_task_order_transport(token)
    close.assert_awaited_once()
    release.assert_called_once_with(account.id)


@pytest.mark.parametrize("malformed", [None, {"cid": "invalid", "text": "цена"},
                                     {"cid": CIDS[1], "text": {"unexpected": "цена"}}])
async def test_malformed_comment_rows_never_prove_complete_keyword_coverage(monkeypatch, malformed):
    from executor.http import comment_discovery
    fetch = AsyncMock(return_value={"comments": [{"cid": CIDS[0], "text": "Какая цена?"}, malformed],
                                   "has_more": 0, "cursor": 50})
    monkeypatch.setattr(comment_discovery, "fetch_comment_page", fetch)
    targets, scan = await keyword.collect_http_targets(object(), MEDIA, ["цена"])
    assert len(targets) == 1 and targets[0]["cid"] == CIDS[0]
    assert scan["complete"] is False and scan["error"] == "keyword_comment_list_unavailable"
