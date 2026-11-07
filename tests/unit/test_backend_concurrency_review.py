"""Adversarial cancellation and stale-worker boundaries of durable publications."""
import asyncio
import threading
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest
from sqlalchemy import update
from sqlalchemy.orm import Session

from db.models import Account, PublicationAttempt, QuantityAction, Task
from executor import quantity_engine as quantity
from tests.unit.test_quantity_redesign_recovery import configure_task, receipt


@pytest.mark.asyncio
@pytest.mark.parametrize("boundary", ["quantity", "journal", "checkpoint"])
@pytest.mark.parametrize("worker_fails", [False, True])
async def test_repeated_cancel_owns_worker_until_it_finishes(
        db_session, sample_task, boundary, worker_fails):
    from services.publication_journal import _persist
    from services.visibility_checkpoint import run_visibility_checkpoint
    entered, release, finished = threading.Event(), threading.Event(), threading.Event()

    def work(*args):
        entered.set()
        try:
            assert release.wait(3)
            if worker_fails:
                raise RuntimeError("worker failed after cancellation")
        finally:
            finished.set()

    if boundary == "quantity":
        operation = quantity._owned_db_call(work)
    elif boundary == "journal":
        operation = _persist(db_session.get_bind(), work)
    else:
        db_session.commit()
        operation = run_visibility_checkpoint(db_session, sample_task, work)
    owner = asyncio.create_task(operation)
    try:
        assert await asyncio.wait_for(asyncio.to_thread(entered.wait, 1), 1.5)
        for _ in range(3):
            owner.cancel()
            await asyncio.sleep(.01)
        assert not owner.done(), "A cancelled owner abandoned its still-running worker"
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(owner, 2)
        assert finished.is_set()
    finally:
        release.set()
        await asyncio.gather(owner, return_exceptions=True)
        await asyncio.to_thread(finished.wait, 2)


@pytest.mark.asyncio
@pytest.mark.parametrize("worker_fails", [False, True])
async def test_repeated_cancel_during_selection_restores_slot_and_releases_account(
        db_session, sample_task, sample_account, monkeypatch, worker_fails):
    from services import account_service
    from services.account_assignment import claim_assignment
    configure_task(db_session, sample_task)
    quantity.ensure_actions(db_session, sample_task, sample_task.video_urls[0], "main", 1, ["main"])
    identity = db_session.query(QuantityAction.id).scalar()
    account_id = sample_account.id
    entered, release = threading.Event(), threading.Event()

    def select(db, **kwargs):
        entered.set()
        assert release.wait(3)
        if worker_fails:
            raise RuntimeError("selection failed after stop")
        account = db.get(Account, account_id)
        assert claim_assignment(db, account, "main_comment", reserve=True)
        return account

    monkeypatch.setattr(account_service, "get_free_account_for_main_comment", select)
    owner = asyncio.create_task(quantity.publish_action(db_session.get_bind(), sample_task.id, identity,
                                                       stop_requested=lambda: False))
    try:
        assert await asyncio.wait_for(asyncio.to_thread(entered.wait, 1), 1.5)
        for _ in range(3):
            owner.cancel()
            await asyncio.sleep(.01)
        assert not owner.done()
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(owner, 2)
    finally:
        release.set()
        await asyncio.gather(owner, return_exceptions=True)
    db_session.expire_all()
    assert db_session.get(Account, account_id).assignment_lease_token is None
    action = db_session.get(QuantityAction, identity)
    assert action.state == "not_sent" and action.dispatches == 0


@pytest.mark.parametrize("stop", ["stopped", "stopped_by_user", "cancelled_by_user"])
def test_claim_checks_stop_after_reloading_task(db_session, sample_task, stop):
    configure_task(db_session, sample_task)
    quantity.ensure_actions(db_session, sample_task, sample_task.video_urls[0], "main", 1, ["main"])
    identity = db_session.query(QuantityAction.id).scalar()
    # Keep a stale ORM task in the claimant's identity map, as in the executor.
    assert sample_task.status == "done"
    with Session(db_session.get_bind()) as stopper:
        task = stopper.get(Task, sample_task.id)
        if stop == "stopped":
            task.status = stop
        else:
            task.settings = {**task.settings, stop: True}
        stopper.commit()
    assert quantity.claim_action(db_session, identity) is None
    db_session.expire_all()
    action = db_session.get(QuantityAction, identity)
    assert action.state == "pending" and action.dispatches == 0


@pytest.mark.asyncio
@pytest.mark.parametrize("new_state", ["dispatching", "visible"])
async def test_old_publisher_cannot_overwrite_newer_owner_or_visibility(
        db_session, sample_task, sample_account, monkeypatch, new_state):
    from executor import visibility_publisher
    from services import account_service
    configure_task(db_session, sample_task)
    quantity.ensure_actions(db_session, sample_task, sample_task.video_urls[0], "main", 1, ["main"])
    identity = db_session.query(QuantityAction.id).scalar()
    engine, account_id = db_session.get_bind(), sample_account.id
    release = Mock()
    monkeypatch.setattr(account_service, "get_free_account_for_main_comment", lambda db, **_: db.get(Account, account_id))
    monkeypatch.setattr(account_service, "release_main_account_reservation", release)

    async def stale_receipt(*args, **kwargs):
        with Session(engine) as other:
            values = dict(state=new_state, comment_id="7704740811904828101")
            if new_state == "dispatching":
                values["token"] = "new-owner"
            other.execute(update(QuantityAction).where(QuantityAction.id == identity).values(**values))
            other.commit()
        return SimpleNamespace(comment_id="7704740811904828100"), None

    monkeypatch.setattr(visibility_publisher, "publish_visibility_http", stale_receipt)
    await quantity.publish_action(engine, sample_task.id, identity, stop_requested=lambda: False)
    db_session.expire_all()
    action = db_session.get(QuantityAction, identity)
    assert action.state == new_state and action.comment_id == "7704740811904828101"
    release.assert_called_once_with(account_id)


@pytest.mark.asyncio
async def test_stale_visibility_proof_cannot_confirm_replaced_cid(db_session, sample_task, monkeypatch):
    from services import publication_recheck
    configure_task(db_session, sample_task)
    quantity.ensure_actions(db_session, sample_task, sample_task.video_urls[0], "main", 1, ["main"])
    action = db_session.query(QuantityAction).one()
    action.state, action.comment_id, action.token = "accepted", "7704740811904828192", "original"
    db_session.commit()
    receipt(db_session, sample_task)
    engine, identity = db_session.get_bind(), action.id

    async def observe(db, task, rows):
        with Session(engine) as other:
            other.execute(update(QuantityAction).where(QuantityAction.id == identity).values(
                state="accepted", token="replacement", comment_id="7704740811904828193"))
            other.commit()
        return [dict(observer_id=900, found_ids=["7704740811904828192"])]

    monkeypatch.setattr(publication_recheck, "observe_video", observe)
    await quantity.verify_actions(engine, sample_task.id, [identity], lambda: False)
    db_session.expire_all()
    action = db_session.get(QuantityAction, identity)
    assert action.state == "accepted" and action.comment_id == "7704740811904828193"
    assert db_session.query(PublicationAttempt).one().visibility == "visible"


@pytest.mark.asyncio
@pytest.mark.parametrize("change", ["stop", "new_owner", "parent_changed"])
async def test_late_selector_cannot_authorize_a_stopped_or_replaced_action(
        db_session, sample_task, sample_account, monkeypatch, change):
    from executor import visibility_publisher
    from services import account_service
    configure_task(db_session, sample_task)
    url = sample_task.video_urls[0]
    quantity.ensure_actions(db_session, sample_task, url, "main", 1, ["main"])
    action = db_session.query(QuantityAction).one()
    if change == "parent_changed":
        action.state, action.comment_id = "visible", "7704740811904828100"
        db_session.commit()
        quantity.ensure_actions(db_session, sample_task, url, "reply", 1, ["reply"], parent=action)
        parent_id = action.id
        action = db_session.query(QuantityAction).filter_by(kind="reply").one()
    engine, identity, account_id, task_id = db_session.get_bind(), action.id, sample_account.id, sample_task.id

    def select(db, **kwargs):
        # Selection releases its transaction before another process stops or
        # replaces the action, then returns a candidate based on the old read.
        account = db.get(Account, account_id)
        db.commit()
        with Session(engine) as other:
            if change == "stop":
                other.execute(update(Task).where(Task.id == task_id).values(status="stopped"))
            elif change == "new_owner":
                other.execute(update(QuantityAction).where(QuantityAction.id == identity).values(token="replacement"))
            else:
                other.execute(update(QuantityAction).where(QuantityAction.id == parent_id).values(
                    comment_id="7704740811904828101"))
            other.commit()
        return account

    selector = "get_free_account_for_reply" if change == "parent_changed" else "get_free_account_for_main_comment"
    releaser = "release_reply_account_reservation" if change == "parent_changed" else "release_main_account_reservation"
    release, publish = Mock(), Mock(side_effect=AssertionError("Stale selection cannot publish"))
    monkeypatch.setattr(account_service, selector, select)
    monkeypatch.setattr(account_service, releaser, release)
    monkeypatch.setattr(visibility_publisher, "publish_visibility_http", publish)
    await quantity.publish_action(engine, task_id, identity, stop_requested=lambda: False)
    publish.assert_not_called()
    release.assert_called_once_with(account_id)
    db_session.expire_all()
    action = db_session.get(QuantityAction, identity)
    if change == "new_owner":
        assert action.token == "replacement" and action.state == "dispatching"
    else:
        assert action.state == "not_sent" and action.dispatches == 0


@pytest.mark.asyncio
async def test_verification_does_not_read_another_orders_slots(db_session, sample_task, monkeypatch):
    from services import publication_recheck
    configure_task(db_session, sample_task)
    quantity.ensure_actions(db_session, sample_task, sample_task.video_urls[0], "main", 1, ["main"])
    action = db_session.query(QuantityAction).one()
    action.state, action.comment_id = "accepted", "7704740811904828192"
    other = Task(user_id=sample_task.user_id, status="done", main_comment="fixture",
                 video_urls=sample_task.video_urls, settings=sample_task.settings)
    db_session.add(other)
    db_session.commit()
    observe = Mock(side_effect=AssertionError("Another order owns this slot"))
    monkeypatch.setattr(publication_recheck, "observe_video", observe)
    await quantity.verify_actions(db_session.get_bind(), other.id, [action.id], lambda: False)
    observe.assert_not_called()


def test_unstarted_release_refunds_only_its_own_dispatch_reservation(db_session, sample_task):
    configure_task(db_session, sample_task, max_dispatches=20)
    quantity.ensure_actions(db_session, sample_task, sample_task.video_urls[0], "main", 1, ["main"])
    action = db_session.query(QuantityAction).one()
    action.state, action.dispatches = "absent", 7
    db_session.commit()
    identity = action.id
    token = quantity.claim_action(db_session, identity)
    assert token
    quantity._release_unstarted_claim(db_session, identity, token, "stopped")
    quantity._release_unstarted_claim(db_session, identity, token, "stopped")
    quantity._finalize_claim(db_session, sample_task.id, identity, token, None, None,
                             entered=False, completed=False, progress={})
    db_session.expire_all()
    assert db_session.get(QuantityAction, identity).dispatches == 7


@pytest.mark.asyncio
async def test_cancelling_receipt_finalization_still_saves_the_accepted_cid(
        db_session, sample_task, sample_account, monkeypatch):
    from services import publication_journal as journal
    from services.publication_progress import publication_scope
    configure_task(db_session, sample_task)
    db_session.commit()
    entered, release = threading.Event(), threading.Event()
    original_write, calls = journal._write, 0

    def delayed_write(engine, operation):
        nonlocal calls
        calls += 1
        if calls == 2:
            entered.set()
            assert release.wait(3)
        return original_write(engine, operation)

    monkeypatch.setattr(journal, "_write", delayed_write)

    @journal.observe_publication_request
    async def publish(self, **kwargs):
        return True, {"comment": {"cid": "7704740811904828192"}}, None

    client = SimpleNamespace(page=SimpleNamespace(_pool_account_id=sample_account.id),
                             last_request_meta={"request_attempted": True})
    with publication_scope(db_session, sample_task, sample_task.video_urls[0]):
        owner = asyncio.create_task(publish(client, profile=SimpleNamespace(method="POST", endpoint="/api/comment/publish/"),
                                           dynamic_params={"aweme_id": "123", "text": "main"}))
    try:
        assert await asyncio.wait_for(asyncio.to_thread(entered.wait, 1), 1.5)
        for _ in range(3):
            owner.cancel()
            await asyncio.sleep(.01)
        assert not owner.done()
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(owner, 2)
    finally:
        release.set()
        await asyncio.gather(owner, return_exceptions=True)
    db_session.expire_all()
    row = db_session.query(PublicationAttempt).one()
    assert row.status == "accepted" and row.comment_id == "7704740811904828192" and row.wire_count == 1


@pytest.mark.asyncio
async def test_sql_stop_ends_scheduler_even_without_a_redis_stop_signal(db_session, sample_task, monkeypatch):
    from executor import engagement_engine
    configure_task(db_session, sample_task)
    engine, task_id, url = db_session.get_bind(), sample_task.id, sample_task.video_urls[0]

    async def resolve(*args):
        with Session(engine) as stopper:
            stopper.execute(update(Task).where(Task.id == task_id).values(status="stopped"))
            stopper.commit()
        return url, "7704740811904828100"

    publish = AsyncMock(return_value="account_preparation_failed")
    monkeypatch.setattr(engagement_engine, "resolve_order_video_url", resolve)
    monkeypatch.setattr(quantity, "publish_action", publish)
    result = await quantity._execute_quantity_publications(db_session, sample_task, url, ["main"], task_id,
                                                          stop_requested=lambda: False)
    assert result.error == "stopped_by_user"
    publish.assert_not_awaited()
