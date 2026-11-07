"""Paid keyword orders use real worker/SQLite settlement, with no live services."""
import asyncio
from datetime import datetime, timedelta, timezone
from decimal import Decimal
import inspect
import threading
from unittest.mock import AsyncMock
from types import SimpleNamespace

from fastapi import HTTPException
import pytest
from sqlalchemy import create_engine, event
from sqlalchemy.orm import Session
from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker
from starlette.requests import Request

from api import tasks
from db.database import Base
from db.models import Pricing, Task, Transaction, User
from executor import engagement_engine, executor, keyword_replies, reply_visibility
from services import task_service, task_settlement, task_stop_signal
from tests.unit.test_task_create_duplicates import order_db


pytestmark = pytest.mark.unit
URL = "https://www.tiktok.com/@fixture/video/1234567890123456789"
CID = "7700000000000000001"


@pytest.fixture
def paid_keyword_worker(tmp_path, monkeypatch):
    engine = create_engine(f"sqlite:///{(tmp_path / 'billing.db').as_posix()}",
                           connect_args={"check_same_thread": False})

    @event.listens_for(engine, "connect")
    def pragmas(connection, record):
        connection.execute("PRAGMA journal_mode=WAL")
        connection.execute("PRAGMA foreign_keys=ON")

    Base.metadata.create_all(engine)
    with Session(engine) as db:
        db.add(User(id=1, telegram_id=9991, role="user", balance=Decimal("5.00")))
        db.add(Pricing(id=1, price_success=Decimal("5.00"), price_failure=Decimal("2.00")))
        task = Task(user_id=1, kind="keyword_replies", status="pending", main_comment="keyword",
                    reply1="reply", video_urls=[URL], total_cost=Decimal("5.00"),
                    settings={"keywords": ["price"], "replies": ["reply"], "reply_count": 1,
                              "delays": False})
        db.add(task)
        db.flush()
        task_id = task.id
        db.add(Transaction(user_id=1, task_id=task_id, type="hold", amount=Decimal("-5.00")))
        db.commit()

    monkeypatch.setattr(executor, "get_sync_db", lambda: iter([Session(engine, expire_on_commit=False)]))
    monkeypatch.setattr(executor, "activate_dedicated_proxy_for_user", lambda *args: None)
    monkeypatch.setattr(executor, "get_event_loop", lambda: asyncio.get_event_loop())
    monkeypatch.setattr(executor, "_shutdown_event", threading.Event())
    monkeypatch.setattr(executor.settings, "TIKTOK_NETWORK_IMPL_READY", True)
    monkeypatch.setattr(executor.settings, "ADMIN_IDS", [])
    monkeypatch.setattr(task_stop_signal, "is_task_stop_requested", lambda *args: False)
    monkeypatch.setattr(engagement_engine, "resolve_order_video_url", AsyncMock(
        return_value=(URL, "1234567890123456789")))
    discovery = AsyncMock(return_value=([], {"complete": True, "scanned": 2, "error": None}))
    monkeypatch.setattr(keyword_replies, "discover_keyword_targets", discovery)

    async def send(*args, **kwargs):
        cid = kwargs["comment_id"]
        if cid == CID:
            return [{"success": True, "method": "http", "reply_id": str(int(cid) + 100),
                     "parent_comment_id": cid, "network_confirmed": True}], cid, "fixture"
        return [{"success": False, "error": "comment_not_found", "parent_comment_id": cid}], cid, "fixture"

    sender = AsyncMock(side_effect=send)
    monkeypatch.setattr(engagement_engine, "execute_engagement_only_replies_via_full_flow", sender)

    async def unchanged(db, task, url, result, **kwargs):
        return result

    monkeypatch.setattr(reply_visibility, "verify_order_reply_visibility", unchanged)

    def run():
        with Session(engine) as db:
            db.get(Task, task_id).status = "pending"
            db.commit()
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        try:
            executor.process_task(task_id)
        finally:
            loop.close()
            asyncio.set_event_loop(None)
        with Session(engine) as db:
            return {"balance": db.get(User, 1).balance, "refunded": db.get(Task, task_id).refunded_cost,
                    "status": db.get(Task, task_id).status,
                    "refunds": [row.amount for row in db.query(Transaction).filter_by(type="refund").order_by(Transaction.id)]}

    yield engine, task_id, discovery, sender, run
    engine.dispose()


@pytest.mark.parametrize("partial", [False, True])
def test_recovered_paid_keyword_order_does_not_credit_refund_twice(paid_keyword_worker, partial):
    engine, task_id, discovery, sender, run = paid_keyword_worker
    if partial:
        discovery.return_value = ([{"cid": cid, "text": "price"} for cid in (CID, str(int(CID) + 1))],
                                  {"complete": True, "scanned": 2, "error": None})
    for _ in range(2):
        state = run()
        assert state == {"balance": Decimal("8.00"), "refunded": Decimal("3.00"),
                         "status": "partial" if partial else "error", "refunds": [Decimal("3.00")]}
    assert sender.await_count == (2 if partial else 0)


def test_changed_tariff_cannot_refund_more_than_the_original_hold(paid_keyword_worker):
    engine, task_id, _, _, run = paid_keyword_worker
    with Session(engine) as db:
        db.get(Pricing, 1).price_success = Decimal("50.00")
        db.commit()
    assert run() == {"balance": Decimal("10.00"), "refunded": Decimal("5.00"),
                     "status": "error", "refunds": [Decimal("5.00")]}


@pytest.mark.parametrize("marker,recorded,expected_delta", [
    ("1.00", "1.00", "2.00"), ("3.00", "3.00", "0.00"),
    ("0.00", "3.00", "0.00"), ("4.00", "4.00", "0.00"),
])
def test_settlement_reconciles_existing_refunds_without_erasing_them(
        paid_keyword_worker, marker, recorded, expected_delta):
    engine, task_id, _, _, run = paid_keyword_worker
    with Session(engine) as db:
        db.get(Task, task_id).refunded_cost = Decimal(marker)
        db.get(User, 1).balance += Decimal(recorded)
        db.add(Transaction(user_id=1, task_id=task_id, type="refund", amount=Decimal(recorded)))
        db.commit()
    state = run()
    assert state["balance"] == Decimal("5.00") + Decimal(recorded) + Decimal(expected_delta)
    assert state["refunded"] == max(Decimal(marker), Decimal(recorded)) + Decimal(expected_delta)
    assert state["refunds"] == [Decimal(recorded)] + ([Decimal(expected_delta)] if Decimal(expected_delta) else [])


def test_refund_receipt_and_task_settlement_are_committed_together(paid_keyword_worker):
    engine, task_id, _, _, run = paid_keyword_worker
    observed = []

    def before_commit(session):
        if session.get_bind() is not engine:
            return
        credits = [row for row in session.new if isinstance(row, Transaction) and row.type == "refund"]
        if credits:
            observed.append((sum(row.amount for row in credits), session.get(Task, task_id).refunded_cost))

    event.listen(Session, "before_commit", before_commit)
    try:
        assert run()["balance"] == Decimal("8.00")
    finally:
        event.remove(Session, "before_commit", before_commit)
    assert observed == [(Decimal("3.00"), Decimal("3.00"))]


def test_failed_refund_rollback_does_not_leave_keyword_order_in_progress(paid_keyword_worker, monkeypatch):
    _, _, _, _, run = paid_keyword_worker

    def failed_refund(db, *args, **kwargs):
        # The real balance service rolls back its session when its DB operation
        # fails. Finalization must not silently commit the old running status.
        db.rollback()
        return False

    monkeypatch.setattr(task_settlement, "refund_balance", failed_refund)
    state = run()
    assert state["status"] == "error"
    assert state["balance"] == Decimal("5.00")
    assert not state["refunded"] and not state["refunds"]


async def test_keyword_text_endpoint_rejects_main_variant_edits_without_mutation(order_db):
    task = Task(user_id=1, kind="keyword_replies", status="pending", main_comment="keyword",
                reply1="Original reply", video_urls=[URL],
                settings={"keywords": ["price"], "replies": ["Original reply"]})
    order_db.add(task)
    await order_db.commit()
    with pytest.raises(HTTPException) as exc:
        await inspect.unwrap(tasks.update_task_texts)(
            request=Request({"type": "http", "headers": []}), task_id=task.id, user_id=1,
            payload=tasks.TaskTextsUpdate(text_variants=["Wrong reply"] * 3),
            db=order_db, auth_user_id=1)
    assert exc.value.status_code == 409
    await order_db.refresh(task)
    assert task.main_comment == "keyword" and task.reply1 == "Original reply"
    assert task.settings == {"keywords": ["price"], "replies": ["Original reply"]}


async def test_concurrent_keyword_orders_cannot_spend_the_same_balance_twice(order_db, tmp_path, monkeypatch):
    engine = create_async_engine(f"sqlite+aiosqlite:///{(tmp_path / 'api_billing.db').as_posix()}")
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    try:
        async with engine.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)
        async with sessions() as db:
            db.add(User(id=1, telegram_id=12345, role="user", balance=Decimal("10.00")))
            await db.commit()
        monkeypatch.setattr(tasks.settings, "DEMO_MODE", False)
        monkeypatch.setattr(tasks.settings, "ADMIN_IDS", [])
        monkeypatch.setattr(tasks.settings, "TIKTOK_NETWORK_IMPL_READY", True)
        monkeypatch.setattr(tasks, "get_pricing_async", AsyncMock(return_value=SimpleNamespace(price_success=Decimal("7.00"))))
        read_balance = tasks.get_user_balance_async
        both_read = asyncio.Event()
        reads = 0

        async def simultaneous_balance(db, user_id):
            nonlocal reads
            balance = await read_balance(db, user_id)
            reads += 1
            if reads == 2:
                both_read.set()
            await asyncio.wait_for(both_read.wait(), 5)
            return balance

        monkeypatch.setattr(tasks, "get_user_balance_async", simultaneous_balance)

        async def create(key):
            async with sessions() as db:
                return await inspect.unwrap(tasks.create_task)(
                    request=Request({"type": "http", "headers": [(b"x-idempotency-key", key.encode())]}),
                    task_data=tasks.TaskCreate(user_id=1, kind="keyword_replies", video_urls=[URL],
                                               keywords=["price"], reply1="reply"),
                    db=db, auth_user_id=1)

        outcomes = await asyncio.gather(create("order-one"), create("order-two"), return_exceptions=True)
        assert sum(isinstance(outcome, dict) for outcome in outcomes) == 1, outcomes
        errors = [outcome for outcome in outcomes if isinstance(outcome, HTTPException)]
        assert len(errors) == 1 and errors[0].status_code == 402, outcomes
        async with sessions() as db:
            from sqlalchemy import select
            assert (await db.get(User, 1)).balance == Decimal("3.00")
            assert len((await db.execute(select(Task))).scalars().all()) == 1
            assert len((await db.execute(select(Transaction))).scalars().all()) == 1
    finally:
        await engine.dispose()


@pytest.mark.parametrize("exit_path", ["user_stop", "shutdown", "timeout", "critical_error"])
def test_interrupted_worker_refunds_only_the_unused_video(paid_keyword_worker, monkeypatch, exit_path):
    import time

    engine, task_id, _, _, run = paid_keyword_worker
    second = URL[:-1] + "8"
    with Session(engine) as db:
        task = db.get(Task, task_id)
        task.video_urls = [URL, second]
        task.total_cost = Decimal("10.00")
        task.settings = {**task.settings, "url_results": {URL: {"status": "success"}}}
        db.commit()

    if exit_path == "user_stop":
        policy = executor.ExecutionPolicy.from_settings

        def stop_during_setup():
            with Session(engine) as db:
                task = db.get(Task, task_id)
                task.status = "stopped"
                task.error_reason = "stopped_by_user"
                task.settings = {**task.settings, "stopped_by_user": True,
                                 task_settlement.REFUND_PENDING_KEY: True}
                db.commit()
            return policy()

        monkeypatch.setattr(executor.ExecutionPolicy, "from_settings", stop_during_setup)
    elif exit_path == "shutdown":
        executor._shutdown_event.set()
    elif exit_path == "timeout":
        policy = executor.ExecutionPolicy.from_settings

        def slow_setup():
            time.sleep(1.1)
            return policy()

        monkeypatch.setattr(executor.ExecutionPolicy, "from_settings", slow_setup)
        monkeypatch.setattr(executor.settings, "TASK_PROCESSING_TIMEOUT", 1)
        monkeypatch.setattr(executor.settings, "TASK_TIMEOUT_COOLDOWN_SECONDS", 0)
    else:
        def broken_setup(*args):
            raise RuntimeError("fixture worker setup failed")

        monkeypatch.setattr(executor, "activate_dedicated_proxy_for_user", broken_setup)

    state = run()
    assert state["status"] == ("error" if exit_path == "critical_error" else "stopped")
    assert state["refunded"] == Decimal("5.00")
    assert state["refunds"] == [Decimal("5.00")]
    if exit_path != "critical_error":
        with Session(engine) as db:
            assert db.get(Task, task_id).error_reason == {
                "timeout": "task_timeout", "shutdown": "worker_shutdown", "user_stop": "stopped_by_user",
            }[exit_path]


def test_early_failure_charges_failure_rate_only_for_the_visited_video(paid_keyword_worker, monkeypatch):
    engine, task_id, discovery, _, run = paid_keyword_worker
    urls = [URL[:-1] + str(index) for index in range(10)]
    with Session(engine) as db:
        task = db.get(Task, task_id)
        task.video_urls = urls
        task.total_cost = Decimal("50.00")
        db.commit()

    monkeypatch.setattr(executor, "_reply_visibility_exhausted", lambda row: True)
    assert run()["refunds"] == [Decimal("48.00")]
    assert discovery.await_count == 1


async def test_api_stop_is_recoverable_if_worker_exits_and_refund_is_idempotent(paid_keyword_worker, monkeypatch):
    engine, task_id, _, _, _ = paid_keyword_worker
    second = URL[:-1] + "8"
    with Session(engine) as db:
        task = db.get(Task, task_id)
        task.status = "in_progress"
        task.video_urls = [URL, second]
        task.total_cost = Decimal("10.00")
        task.settings = {**task.settings, "url_results": {URL: {"status": "success"}}}
        db.commit()

    async_engine = create_async_engine(f"sqlite+aiosqlite:///{engine.url.database}")
    monkeypatch.setattr(tasks, "request_task_stop", lambda _: True)
    try:
        async with async_sessionmaker(async_engine, expire_on_commit=False)() as db:
            response = await inspect.unwrap(tasks.stop_task)(
                request=Request({"type": "http", "headers": []}),
                task_id=task_id, user_id=1, db=db, auth_user_id=1,
            )
            assert response["status"] == "stopped"
    finally:
        await async_engine.dispose()

    with Session(engine) as db:
        task = db.get(Task, task_id)
        assert task.settings[task_settlement.REFUND_PENDING_KEY]
        task.updated_at = datetime.now(timezone.utc) - timedelta(minutes=2)
        db.commit()
        with monkeypatch.context() as patch:
            patch.setattr(task_settlement, "refund_balance", lambda *args, **kwargs: False)
            assert task_service.recover_stuck_tasks(db, timeout_seconds=60) == 0
            assert task.settings[task_settlement.REFUND_PENDING_KEY]
        assert task_service.recover_stuck_tasks(db, timeout_seconds=60) == 1
        assert task_service.recover_stuck_tasks(db, timeout_seconds=60) == 0
        assert task.status == "stopped"
        assert task_settlement.REFUND_PENDING_KEY not in task.settings
        assert task.refunded_cost == Decimal("5.00")
        assert db.get(User, 1).balance == Decimal("10.00")


def test_account_shortage_without_a_submission_refunds_the_whole_video(paid_keyword_worker, monkeypatch):
    _, _, _, _, run = paid_keyword_worker
    outcome = SimpleNamespace(to_dict=lambda **kwargs: {"status": "fail", "error": "no_free_reply_accounts"})
    monkeypatch.setattr(keyword_replies, "execute_keyword_replies", AsyncMock(return_value=outcome))
    assert run()["refunds"] == [Decimal("5.00")]


def test_stale_task_uses_saved_outcomes_instead_of_treating_cursor_as_success(paid_keyword_worker):
    engine, task_id, _, _, _ = paid_keyword_worker
    urls = [URL[:-1] + str(index) for index in range(3)]
    with Session(engine) as db:
        task = db.get(Task, task_id)
        task.status = "in_progress"
        task.updated_at = datetime.now(timezone.utc) - timedelta(minutes=2)
        task.video_urls = urls
        task.total_cost = Decimal("15.00")
        task.progress = 2
        task.settings = {**task.settings, "url_results": {
            urls[0]: {"status": "success"}, urls[1]: {"status": "fail", "error": "keyword_no_matches"},
        }}
        db.commit()

        assert task_service.recover_stuck_tasks(db, timeout_seconds=60) == 1
        assert task.status == "partial"
        assert task.refunded_cost == Decimal("8.00")
