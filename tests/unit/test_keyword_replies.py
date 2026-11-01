"""Keyword order acceptance: pagination, delivery fences, API, bot and money."""
import asyncio
from decimal import Decimal
import inspect
import threading
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from pydantic import ValidationError
from sqlalchemy import create_engine, event, select
from sqlalchemy.orm import Session
from starlette.requests import Request

from api import tasks
from db.database import Base
from db.models import AppSettings, KeywordReplyTarget, Task, Transaction, User
from executor import engagement_engine as engagement, keyword_replies as keyword
from services.keyword_replies import matches_keywords, normalize_keywords
from services.order_transport import set_task_order_transport, reset_task_order_transport
from tests.unit.test_task_create_duplicates import order_db


pytestmark = pytest.mark.unit
URL = "https://www.tiktok.com/@fixture/video/1234567890123456789"
MEDIA = "1234567890123456789"
CIDS = [str(7700000000000000000 + i) for i in range(1, 5)]


def comment(index, text="Где купить?", **extra):
    return {"cid": CIDS[index], "text": text, "user": {"unique_id": "fixture"}, **extra}


@pytest.mark.parametrize("text,matched", [
    ("ЦЕНА?", True), ("Где🔥купить?", True), ("Доставка есть?", True),
    ("Нигде купить нельзя", False), ("Обсуждение расценок", False), ("", False),
])
def test_keywords_are_any_whole_word_or_phrase_case_insensitive(text, matched):
    keys = normalize_keywords("цена, где купить\nдоставка; ЦЕНА")
    assert keys == ["цена", "где купить", "доставка"]
    assert matches_keywords(text, keys) is matched


@pytest.mark.parametrize("value", [None, [], "🔥", [None], ["x" * 101], [f"word{i}" for i in range(51)]])
def test_invalid_keywords_never_mean_match_everything(value):
    with pytest.raises(ValueError):
        normalize_keywords(value)


@pytest.mark.parametrize("overrides", [
    {"keywords": []}, {"keywords": ["🔥"]}, {"reply1": None},
    {"engagement_comment_id": CIDS[0]}, {"settings": {"replies": "not-a-list"}},
])
def test_api_rejects_incomplete_or_ambiguous_keyword_orders(overrides):
    payload = dict(user_id=1, video_urls=[URL], kind="keyword_replies", keywords=["цена"], reply1="Ответ")
    payload.update(overrides)
    with pytest.raises(ValidationError):
        tasks.TaskCreate(**payload)


async def test_api_persists_keyword_type_server_mode_and_one_hold_per_video(order_db, monkeypatch):
    monkeypatch.setattr(tasks.settings, "TIKTOK_NETWORK_IMPL_READY", True)
    monkeypatch.setattr(tasks.settings, "DEMO_MODE", False)
    monkeypatch.setattr(tasks.settings, "ADMIN_IDS", [])
    monkeypatch.setattr(tasks, "get_pricing_async", AsyncMock(return_value=SimpleNamespace(price_success=Decimal("2.00"))))
    monkeypatch.setattr(tasks, "get_boost_likes_config_async", AsyncMock(return_value=(True, 100)))
    user = await order_db.get(User, 1)
    user.role, user.balance = "user", Decimal("10.00")
    order_db.add(AppSettings(key="order_transport_mode", value="dom"))
    await order_db.commit()
    request = Request({"type": "http", "headers": [(b'x-idempotency-key', b'keyword-order-request')]})
    payload = tasks.TaskCreate(user_id=1, video_urls=[URL], kind="keyword_replies", reply1="Ответ",
        keywords="ЦЕНА, цена, доставка", settings={"order_transport_mode": "http", "boost_likes_enabled": True,
        "keyword_scans": {"fake": {"complete": True}}, "url_results": {URL: {"status": "success"}}})
    create = inspect.unwrap(tasks.create_task)
    first = await create(request=request, task_data=payload, db=order_db, auth_user_id=1)
    repeated = await create(request=request, task_data=payload, db=order_db, auth_user_id=1)
    assert repeated["task_id"] == first["task_id"]
    task = await order_db.get(Task, first["task_id"])
    assert task.kind == "keyword_replies" and task.settings["keywords"] == ["ЦЕНА", "доставка"]
    assert task.settings["order_transport_mode"] == "http"
    assert not task.likes and not task.settings["boost_likes_enabled"]
    assert "keyword_scans" not in task.settings and "url_results" not in task.settings
    await order_db.refresh(user)
    assert user.balance == Decimal("8.00")
    holds = (await order_db.execute(select(Transaction))).scalars().all()
    assert len(holds) == 1 and holds[0].amount == Decimal("-2.00")


async def test_http_discovery_visits_all_pages_deduplicates_and_filters(monkeypatch):
    from executor.http import comment_discovery
    read = AsyncMock(side_effect=[
        {"comments": [comment(0), comment(1, "ЦЕНА"), comment(2, "нигде"), comment(3, reply_id="123")], "cursor": 20, "has_more": 1},
        {"comments": [comment(0), comment(2, "Где купить?")], "cursor": 40, "has_more": 0},
    ])
    monkeypatch.setattr(comment_discovery, "fetch_comment_page", read)
    found, info = await keyword.collect_http_targets(object(), MEDIA, ["где купить", "цена"])
    assert [row["cid"] for row in found] == CIDS[:2]
    assert info == {"complete": True, "scanned": 4, "error": None}
    assert [call.kwargs["cursor"] for call in read.await_args_list] == [0, 20]


@pytest.mark.parametrize("second,reason", [
    (None, "keyword_comment_list_unavailable"),
    ({"comments": [], "cursor": 20, "has_more": 1}, "keyword_pagination_stalled"),
])
async def test_partial_discovery_does_not_claim_complete_coverage(monkeypatch, second, reason):
    from executor.http import comment_discovery
    read = AsyncMock(side_effect=[{"comments": [comment(0)], "cursor": 20, "has_more": 1}, second])
    monkeypatch.setattr(comment_discovery, "fetch_comment_page", read)
    found, info = await keyword.collect_http_targets(object(), MEDIA, ["где купить"])
    assert len(found) == 1 and not info["complete"] and info["error"] == reason


@pytest.fixture
def keyword_db(tmp_path):
    engine = create_engine(f"sqlite:///{(tmp_path / 'keyword.db').as_posix()}", connect_args={"check_same_thread": False})
    @event.listens_for(engine, "connect")
    def pragmas(connection, record):
        connection.execute("PRAGMA journal_mode=WAL")
        connection.execute("PRAGMA foreign_keys=ON")
    Base.metadata.create_all(engine)
    with Session(engine, expire_on_commit=False) as db:
        db.add(User(id=1, telegram_id=9991, role="admin"))
        db.flush()
        task = Task(user_id=1, kind="keyword_replies", status="in_progress", main_comment="keyword",
                    video_urls=[URL], settings={"keywords": ["где купить"], "replies": ["Первый", "Второй"], "reply_count": 2})
        db.add(task)
        db.commit()
        yield db, task, engine
    engine.dispose()


def receipt(cid, *, mode="http"):
    return {"success": True, "method": mode, "reply_id": str(int(cid) + 100),
            "parent_comment_id": cid, "network_confirmed": mode == "http", "parent_confirmed": True}


@pytest.fixture
def execution(monkeypatch):
    monkeypatch.setattr(engagement, "resolve_order_video_url", AsyncMock(return_value=(URL, MEDIA)))
    discovery = AsyncMock(return_value=([{"cid": cid, "text": "Где купить?", "author": "fixture"} for cid in CIDS[:3]],
                                      {"complete": True, "scanned": 10, "error": None}))
    monkeypatch.setattr(keyword, "discover_keyword_targets", discovery)
    async def send(*args, **kwargs):
        assert kwargs["parent_phrase_match"] is False
        return [receipt(kwargs["comment_id"])], kwargs["comment_id"], "fixture"
    sender = AsyncMock(side_effect=send)
    monkeypatch.setattr(engagement, "execute_engagement_only_replies_via_full_flow", sender)
    token = set_task_order_transport("http", SimpleNamespace(TIKTOK_NETWORK_IMPL_READY=True))
    yield discovery, sender
    reset_task_order_transport(token)


async def test_every_parent_gets_one_reply_and_variant_rotation_is_durable(keyword_db, execution):
    db, task, _ = keyword_db
    discovery, sender = execution
    result = await keyword.execute_keyword_replies(db, task, URL, task_id=task.id)
    payload = result.to_dict()
    assert payload["status"] == "success" and payload["engagement"]["replies"]["ok"] == 3
    assert [call.kwargs["replies"] for call in sender.await_args_list] == [["Первый"], ["Второй"], ["Первый"]]
    assert [row["parent_comment_id"] for row in payload["engagement"]["replies"]["items"]] == CIDS[:3]
    again = await keyword.execute_keyword_replies(db, task, URL, task_id=task.id)
    assert again.to_dict()["status"] == "success"
    assert sender.await_count == 3 and discovery.await_count == 1
    assert db.query(KeywordReplyTarget).count() == 3


async def test_cancellation_resumes_pending_parents_without_repeating_an_uncertain_send(keyword_db, execution):
    db, task, _ = keyword_db
    discovery, sender = execution
    async def interrupted(*args, **kwargs):
        if kwargs["comment_id"] == CIDS[1]:
            raise asyncio.CancelledError
        return [receipt(kwargs["comment_id"])], kwargs["comment_id"], "fixture"
    sender.side_effect = interrupted
    with pytest.raises(asyncio.CancelledError):
        await keyword.execute_keyword_replies(db, task, URL, task_id=task.id)
    rows = keyword._targets(db, task.id, keyword.video_key(URL))
    assert [row.status for row in rows] == ["success", "dispatching", "pending"]
    result = await keyword.execute_keyword_replies(db, task, URL, task_id=task.id)
    payload = result.to_dict()
    assert sender.await_count == 3 and discovery.await_count == 1
    assert payload["status"] == "submitted_unconfirmed"
    assert payload["engagement"]["replies"]["ok"] == 2
    assert payload["engagement"]["replies"]["items"][1]["submitted_unconfirmed"]


async def test_target_claim_is_atomic_across_independent_sessions(keyword_db):
    db, task, engine = keyword_db
    await keyword.checkpoint(db, task, URL, "plan", targets=[{"cid": CIDS[0], "text": "цена"}],
                             scan={"complete": True}, replies=["Ответ"])
    async def claim():
        with Session(engine, expire_on_commit=False) as other:
            row = other.get(Task, task.id)
            return await keyword.checkpoint(other, row, URL, "claim", cid=CIDS[0])
    assert sorted(await asyncio.gather(claim(), claim())) == [False, True]


async def test_failed_durable_plan_never_dispatches_a_reply(keyword_db, execution, monkeypatch):
    db, task, _ = keyword_db
    _, sender = execution
    monkeypatch.setattr(keyword, "run_visibility_checkpoint", AsyncMock(side_effect=RuntimeError("db unavailable")))
    with pytest.raises(RuntimeError, match="db unavailable"):
        await keyword.execute_keyword_replies(db, task, URL, task_id=task.id)
    sender.assert_not_awaited()


async def test_no_matches_and_incomplete_scan_have_distinct_results(keyword_db, execution):
    db, task, _ = keyword_db
    discovery, sender = execution
    discovery.return_value = ([], {"complete": True, "scanned": 20, "error": None})
    result = await keyword.execute_keyword_replies(db, task, URL, task_id=task.id)
    assert result.to_dict()["status"] == "fail" and result.to_dict()["error"] == "keyword_no_matches"
    sender.assert_not_awaited()


@pytest.mark.parametrize("bad", ["missing_receipt", "wrong_parent", "wrong_response_parent", "exception"])
async def test_ambiguous_or_mismatched_publication_is_not_success_or_retried(keyword_db, execution, bad):
    db, task, _ = keyword_db
    discovery, sender = execution
    discovery.return_value = ([{"cid": CIDS[0], "text": "цена"}], {"complete": True})
    if bad == "exception":
        sender.side_effect = RuntimeError("lost acknowledgement")
    else:
        row = receipt(CIDS[1] if bad == "wrong_parent" else CIDS[0])
        if bad == "missing_receipt":
            row["reply_id"] = None
        if bad == "wrong_response_parent":
            row["response_parent_id"] = CIDS[1]
        sender.side_effect = None
        sender.return_value = ([row], CIDS[0], "fixture")
    result = await keyword.execute_keyword_replies(db, task, URL, task_id=task.id)
    assert result.to_dict()["status"] == "submitted_unconfirmed"
    await keyword.execute_keyword_replies(db, task, URL, task_id=task.id)
    assert sender.await_count == 1


def test_migration_is_idempotent_and_preserves_existing_orders(keyword_db, monkeypatch):
    from db.migrations import add_keyword_reply_targets_migration as migration
    db, task, engine = keyword_db
    KeywordReplyTarget.__table__.drop(engine)
    monkeypatch.setattr(migration, "sync_engine", engine)
    assert migration.migrate() is True
    assert migration.migrate() is False
    assert db.get(Task, task.id).settings["keywords"] == ["где купить"]


def test_order_card_and_copy_keep_keywords_but_not_delivery_journal():
    from bot.handlers import orders
    task = {"id": 7, "kind": "keyword_replies", "main_comment": "label", "video_urls": [URL],
            "settings": {"keywords": ["цена", "где купить"], "replies": ["Ответ"],
                         "keyword_scans": {"old": {"complete": True}}}}
    assert orders._order_text_variants(task) == []
    assert "ключевым словам" in " ".join(orders._overview_order_plan_lines(task))
    assert "Ключевые слова" in " ".join(orders._text_content_items(task))
    clean = orders._build_recreated_order_settings(task["settings"], origin="retry_failed")
    assert clean["keywords"] == task["settings"]["keywords"] and "keyword_scans" not in clean


async def test_keyword_order_refuses_dom_execution(keyword_db):
    db, task, _ = keyword_db
    token = set_task_order_transport("dom")
    try:
        with pytest.raises(ValueError, match="keyword_http_only"):
            await keyword.execute_keyword_replies(db, task, URL, task_id=task.id)
    finally:
        reset_task_order_transport(token)


async def test_unavailable_http_rejects_creation_before_holding_money(order_db, monkeypatch):
    monkeypatch.setattr(tasks.settings, "TIKTOK_NETWORK_IMPL_READY", False)
    payload = tasks.TaskCreate(user_id=1, kind="keyword_replies", video_urls=[URL], keywords=["цена"], reply1="Ответ")
    from fastapi import HTTPException
    with pytest.raises(HTTPException) as error:
        await inspect.unwrap(tasks.create_task)(request=Request({"type": "http", "headers": []}),
            task_data=payload, db=order_db, auth_user_id=1)
    assert error.value.status_code == 409
    assert not (await order_db.execute(select(Task))).scalars().all()
    assert not (await order_db.execute(select(Transaction))).scalars().all()


async def test_matching_comment_count_is_not_limited_by_twenty_reply_variants(keyword_db, execution):
    db, task, _ = keyword_db
    discovery, sender = execution
    targets = [{"cid": str(7700000000000001000 + index), "text": "Где купить?"} for index in range(35)]
    discovery.return_value = (targets, {"complete": True, "scanned": 40})
    result = await keyword.execute_keyword_replies(db, task, URL, task_id=task.id)
    assert result.to_dict()["status"] == "success" and sender.await_count == 35
    assert result.to_dict()["engagement"]["replies"]["expected"] == 35


async def test_successful_replies_do_not_hide_an_incomplete_comment_scan(keyword_db, execution):
    db, task, _ = keyword_db
    discovery, _ = execution
    discovery.return_value = ([{"cid": CIDS[0], "text": "цена"}],
                             {"complete": False, "scanned": 50, "error": "keyword_scan_limit"})
    result = (await keyword.execute_keyword_replies(db, task, URL, task_id=task.id)).to_dict()
    assert result["status"] == "partial" and result["error"] == "keyword_scan_limit"
    assert result["engagement"]["replies"]["ok"] == 1


async def test_same_video_under_another_username_does_not_repeat_replies(keyword_db, execution):
    db, task, _ = keyword_db
    _, sender = execution
    await keyword.execute_keyword_replies(db, task, URL, task_id=task.id)
    await keyword.execute_keyword_replies(db, task, URL.replace('@fixture', '@canonical'), task_id=task.id)
    assert sender.await_count == 3


async def test_wizard_creates_a_distinct_http_keyword_order(monkeypatch):
    from bot.handlers import engagement_wizard as wizard
    from bot.keyboards.common import get_main_menu_keyboard
    class State:
        def __init__(self): self.data = {}; self.current = None
        async def get_data(self): return dict(self.data)
        async def update_data(self, **kwargs): self.data.update(kwargs)
        async def clear(self): self.data.clear()
        async def set_state(self, value): self.current = value
    state = State()
    user = SimpleNamespace(id=12345, username="fixture")
    message = SimpleNamespace(from_user=user, answer=AsyncMock(), edit_text=AsyncMock())
    callback = SimpleNamespace(data="menu:keyword_replies", from_user=user, message=message, answer=AsyncMock())
    monkeypatch.setattr(wizard.api_client, "sync_telegram", AsyncMock(return_value={"id": 1, "role": "admin"}))
    monkeypatch.setattr(wizard, "check_user_access", AsyncMock(return_value=True))
    monkeypatch.setattr(wizard, "get_user_backend_id", AsyncMock(return_value=1))
    monkeypatch.setattr(wizard, "_boost_likes_ui_config", AsyncMock(side_effect=AssertionError("keyword order has no boost phase")))
    monkeypatch.setattr(wizard, "ensure_mention_choice", AsyncMock(return_value=SimpleNamespace(
        mode="plain", request_kwargs={"idempotency_key": "fixture-keyword"})))
    create = AsyncMock(return_value={"task_id": 44, "order_transport_mode": "http"})
    monkeypatch.setattr(wizard.api_client, "create_task", create)
    await wizard.start_engagement_wizard(callback, state)
    assert state.data["order_kind"] == "keyword_replies"
    message.text = URL
    await wizard.process_video_url(message, state)
    message.text = "цена, где купить"
    await wizard.process_comment_text(message, state)
    await state.update_data(replies=["Подробности в профиле"], account_pool_mode="shared")
    await wizard.show_engagement_preview(callback, state)
    preview = message.edit_text.await_args.args[0]
    assert "HTTP" in preview and "каждый найденный комментарий" in preview
    assert "по действующему тарифу за видео" in preview
    await wizard.confirm_engagement_order(callback, state)
    payload = create.await_args.kwargs
    assert payload["kind"] == "keyword_replies"
    assert payload["settings"]["keywords"] == ["цена", "где купить"]
    assert payload["engagement_comment_text"] is None and not payload["settings"]["boost_likes_enabled"]
    assert payload["idempotency_key"] == "fixture-keyword"
    callbacks = {button.callback_data for row in get_main_menu_keyboard().inline_keyboard for button in row}
    assert "menu:keyword_replies" in callbacks


def test_publication_map_keeps_different_parent_ids_separate_even_with_identical_text():
    from bot.handlers import orders
    items = [{**receipt(cid), "matched_main_text": "Где купить?", "text": "Ответ"} for cid in CIDS[:2]]
    task = {"id": 1, "kind": "keyword_replies", "settings": {"keywords": ["где купить"],
        "url_results": {URL: {"engagement": {"replies": {"items": items}}}}}}
    rows, unassigned, summary = orders._build_publication_map_rows(task)
    assert len(rows) == 2 and not unassigned and summary["reply_submitted"] == 2
    assert all(len(row["replies"]) == 1 for row in rows)


@pytest.mark.parametrize("resume", [False, True])
async def test_real_worker_dispatches_keyword_kind_and_forces_http_without_dom_fallback(keyword_db, execution, monkeypatch, resume):
    from executor import executor
    from executor import reply_visibility
    from services import task_stop_signal
    from services.order_transport import current_order_transport
    db, task, engine = keyword_db
    discovery, sender = execution
    if resume:
        async def interrupted(*args, **kwargs):
            if kwargs["comment_id"] == CIDS[1]:
                raise asyncio.CancelledError
            return [receipt(kwargs["comment_id"])], kwargs["comment_id"], "fixture"
        sender.side_effect = interrupted
        with pytest.raises(asyncio.CancelledError):
            await keyword.execute_keyword_replies(db, task, URL, task_id=task.id)
    task.status = "pending"
    task.settings = {**task.settings, "order_transport_mode": "dom", "delays": False}
    db.commit()
    monkeypatch.setattr(executor, "get_sync_db", lambda: iter([Session(engine, expire_on_commit=False)]))
    monkeypatch.setattr(executor, "activate_dedicated_proxy_for_user", lambda *args: None)
    monkeypatch.setattr(executor, "get_event_loop", lambda: asyncio.get_event_loop())
    monkeypatch.setattr(executor, "_shutdown_event", threading.Event())
    monkeypatch.setattr(executor.settings, "TIKTOK_NETWORK_IMPL_READY", True)
    monkeypatch.setattr(task_stop_signal, "is_task_stop_requested", lambda *args: False)
    async def unchanged(db, task, video_url, result, **kwargs):
        return result
    monkeypatch.setattr(reply_visibility, "verify_order_reply_visibility", unchanged)
    def run():
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        try:
            executor.process_task(task.id)
        finally:
            loop.close()
            asyncio.set_event_loop(None)
    token = set_task_order_transport("dom")
    try:
        await asyncio.to_thread(run)
        assert current_order_transport() == "dom"
    finally:
        reset_task_order_transport(token)
    db.refresh(task)
    assert task.status == ("partial" if resume else "done"), task.error_reason
    assert task.settings["url_results"][URL]["mode"] == "keyword_replies"
    assert task.settings["url_results"][URL]["engagement"]["replies"]["ok"] == (2 if resume else 3)
    assert sender.await_count == 3 and discovery.await_count == 1


async def test_scan_budget_is_bounded_without_silently_claiming_all_comments(monkeypatch):
    from executor.http import comment_discovery
    read = AsyncMock(return_value={"comments": [comment(0)], "cursor": 20, "has_more": 1})
    monkeypatch.setattr(comment_discovery, "fetch_comment_page", read)
    monkeypatch.setattr(keyword.settings, "KEYWORD_REPLY_MAX_SCAN_PAGES", 1)
    rows, info = await keyword.collect_http_targets(object(), MEDIA, ["где купить"])
    assert len(rows) == 1 and info["error"] == "keyword_scan_limit" and not info["complete"]
    assert read.await_count == 1


async def test_stopped_order_cannot_claim_another_comment(keyword_db):
    db, task, _ = keyword_db
    await keyword.checkpoint(db, task, URL, "plan", targets=[{"cid": CIDS[0], "text": "цена"}],
                             scan={"complete": True}, replies=["Ответ"])
    task.status = "stopped"
    db.commit()
    assert not await keyword.checkpoint(db, task, URL, "claim", cid=CIDS[0])
    assert keyword._targets(db, task.id, keyword.video_key(URL))[0].status == "pending"


async def test_deleting_an_order_also_deletes_its_delivery_journal(keyword_db):
    db, task, _ = keyword_db
    await keyword.checkpoint(db, task, URL, "plan", targets=[{"cid": CIDS[0], "text": "цена"}],
                             scan={"complete": True}, replies=["Ответ"])
    db.delete(task)
    db.commit()
    assert db.query(KeywordReplyTarget).count() == 0
