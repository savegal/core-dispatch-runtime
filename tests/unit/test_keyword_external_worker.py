"""Validate external audit claims at the real worker/SQLite boundary."""
import asyncio
from decimal import Decimal
import threading
from unittest.mock import AsyncMock

import pytest
from sqlalchemy.orm import Session

from db.models import Pricing, Transaction, User
from executor import engagement_engine as engine, executor
from services import task_stop_signal
from tests.unit.test_keyword_replies import keyword_db, execution, URL, CIDS, receipt


pytestmark = [pytest.mark.unit, pytest.mark.asyncio]


@pytest.fixture
def run_keyword_worker(keyword_db, monkeypatch):
    from services import metrics
    db, task, sql_engine = keyword_db
    monkeypatch.setattr(executor, "get_sync_db", lambda: iter([Session(sql_engine, expire_on_commit=False)]))
    monkeypatch.setattr(executor, "activate_dedicated_proxy_for_user", lambda *args: None)
    monkeypatch.setattr(executor, "get_event_loop", lambda: asyncio.get_event_loop())
    monkeypatch.setattr(executor, "_shutdown_event", threading.Event())
    monkeypatch.setattr(executor.settings, "TIKTOK_NETWORK_IMPL_READY", True)
    monkeypatch.setattr(executor.settings, "ADMIN_IDS", [])
    monkeypatch.setattr(task_stop_signal, "is_task_stop_requested", lambda *args: False)
    monkeypatch.setattr(executor, "record_timing", lambda *args, **kwargs: None)
    monkeypatch.setattr(metrics, "increment_counter", lambda *args, **kwargs: None)

    async def run():
        task.status = "pending"
        task.settings = {**task.settings, "delays": False, "order_transport_mode": "http"}
        db.commit()

        def worker():
            loop = asyncio.new_event_loop()
            asyncio.set_event_loop(loop)
            try:
                executor.process_task(task.id)
            finally:
                loop.close()
                asyncio.set_event_loop(None)

        await asyncio.to_thread(worker)
        db.refresh(task)
        return task

    return run


async def test_keyword_worker_does_not_require_unused_main_text(keyword_db, execution, run_keyword_worker):
    _, task, _ = keyword_db
    _, send = execution
    task.main_comment = ""
    task.settings = {key: value for key, value in task.settings.items() if key != "text_variants"}
    result = await run_keyword_worker()
    assert result.status == "done", result.error_reason
    assert send.await_count == 3


async def test_existing_reply_only_worker_uses_reply_texts_without_main_variants(
        keyword_db, run_keyword_worker, monkeypatch):
    _, task, _ = keyword_db
    task.kind, task.main_comment = "engagement_only", ""
    task.settings = {**task.settings, "engagement_comment_id": CIDS[0]}
    result = engine.EngagementResult()
    result.comment_found = True
    result.expected_reply_variants = 1
    result.replies = [{**receipt(CIDS[0]), "variant_index": 1}]
    execute = AsyncMock(return_value=result)
    monkeypatch.setattr(engine, "execute_engagement_only", execute)
    completed = await run_keyword_worker()
    assert completed.status == "done", completed.error_reason
    execute.assert_awaited_once()


@pytest.mark.parametrize("kind", ["full", "keyword_replies"])
async def test_worker_keeps_required_publication_text_validation(
        keyword_db, execution, run_keyword_worker, kind):
    _, task, _ = keyword_db
    _, send = execution
    task.kind, task.main_comment = kind, ""
    task.settings = {**task.settings, "replies": [], "reply_count": 0}
    result = await run_keyword_worker()
    assert result.status == "error"
    assert "Нет доступных" in result.error_reason
    send.assert_not_awaited()


async def test_zero_matches_is_not_an_unconfirmed_publication_and_refund_follows_tariff(
        keyword_db, execution, run_keyword_worker):
    db, task, _ = keyword_db
    discover, send = execution
    discover.return_value = ([], {"complete": True, "scanned": 200, "error": None})
    task.settings = {**task.settings, "verify_reply_visibility": True}
    task.total_cost = Decimal("5.00")
    user = db.get(User, task.user_id)
    user.role, user.balance = "user", Decimal("5.00")
    db.add(Pricing(id=1, price_success=Decimal("5.00"), price_failure=Decimal("2.00")))
    db.add(Transaction(user_id=user.id, task_id=task.id, type="hold", amount=Decimal("-5.00")))
    completed = await run_keyword_worker()
    row = completed.settings["url_results"][URL]
    assert row["error"] == "keyword_no_matches" and completed.status == "error"
    assert row["reply_publication_visibility"]["status"] == "not_requested"
    assert completed.settings["unconfirmed_urls"] == []
    assert completed.settings["failed_urls"] == [URL]
    db.refresh(user)
    assert completed.refunded_cost == Decimal("3.00") and user.balance == Decimal("8.00")
    send.assert_not_awaited()


def test_incomplete_scan_is_partial_even_when_all_discovered_replies_succeeded():
    from executor.keyword_replies import KeywordReplyResult
    result = KeywordReplyResult({"complete": False, "scanned": 50, "error": "keyword_scan_limit"})
    result.comment_found, result.expected_reply_variants = True, 1
    result.replies = [{**receipt(CIDS[0]), "variant_index": 1}]
    row = result.to_dict()
    assert row["status"] == "partial" and row["error"] == "keyword_scan_limit"
    assert row["publication_complete"] is False
    assert not executor._url_result_counts_as_success(row)


def test_new_or_retried_keyword_order_cannot_inherit_a_runtime_video_binding():
    from api.tasks import TaskCreate
    from bot.handlers.orders import _build_recreated_order_settings
    from services.autopilot.actions import _retry_settings
    runtime = {URL: {"storage_key": "forged", "aweme_id": "9999999999999999999"}}
    source = {"keywords": ["price"], "replies": ["reply"], "keyword_video_bindings": runtime}
    created = TaskCreate(user_id=1, kind="keyword_replies", video_urls=[URL], settings=source)
    assert "keyword_video_bindings" not in created.settings
    assert "keyword_video_bindings" not in _retry_settings(source, 1)
    assert "keyword_video_bindings" not in _build_recreated_order_settings(
        {"kind": "keyword_replies", "settings": source}, origin="copy")
