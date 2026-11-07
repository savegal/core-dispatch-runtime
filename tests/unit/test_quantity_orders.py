from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from pydantic import ValidationError

from api.tasks import TaskCreate
from api.templates import TaskTemplateCreate, TaskTemplateUpdate
from db.models import PublicationAttempt, QuantityAction
from executor.quantity_engine import ensure_actions, claim_action, build_result, execute_quantity_order
from services.quantity_plan import QuantityPlan
from services.publication_journal import utcnow


@pytest.mark.asyncio
async def test_order_preview_can_set_speed_and_submit_it(monkeypatch):
    from aiogram.fsm.context import FSMContext
    from aiogram.fsm.storage.base import StorageKey
    from aiogram.fsm.storage.memory import MemoryStorage
    from bot.handlers import quantity_orders as composer
    from bot.services import drafts, mention_choice
    from config.settings import get_settings
    from services.publication_runtime import default_runtime, runtime_payload

    state = FSMContext(MemoryStorage(), StorageKey(bot_id=1, chat_id=2, user_id=3))
    message = SimpleNamespace(from_user=SimpleNamespace(id=3), text="", edit_text=AsyncMock(), answer=AsyncMock())
    callback = SimpleNamespace(from_user=message.from_user, message=message, answer=AsyncMock(), data="qo:speed")
    await state.set_data(dict(backend_user_id=1, idempotency_key="original", texts=["main"], replies=[],
                              publication_plan=QuantityPlan(main_count=1).model_dump(),
                              links=["https://www.tiktok.com/@fixture/video/7704740811904828100"]))
    monkeypatch.setattr(get_settings(), "MAX_CONCURRENT_BROWSERS", 4)
    defaults = runtime_payload(default_runtime())
    monkeypatch.setattr(composer.api_client, "get_publication_runtime", AsyncMock(return_value=defaults))
    monkeypatch.setattr(composer.api_client, "get_pricing", AsyncMock(return_value={"price_success": 1}))
    monkeypatch.setattr(composer.api_client, "get_user", AsyncMock(return_value={"balance": 100}))
    await composer.show_preview(message, state, edit=True)
    args, kwargs = message.edit_text.await_args
    assert "Лимит параллельности · HTTP-отправки <b>3</b>, проверки <b>1</b>" in args[0]
    assert any(button.callback_data == "qo:speed" for row in kwargs["reply_markup"].inline_keyboard for button in row)
    await composer.edit_speed(callback, state)
    speed_buttons = message.edit_text.await_args.kwargs["reply_markup"].inline_keyboard[0]
    assert [button.callback_data for button in speed_buttons] == ["qo:speed:1", "qo:speed:2", "qo:speed:3"]
    callback.data = "qo:speed:80"
    await composer.choose_speed(callback, state)
    assert callback.answer.await_args.kwargs["show_alert"]
    for invalid in ("81", "4", "3 2 0 0 3", "1 4 0 0 3"):
        message.text = invalid
        await composer.input_speed(message, state)
        assert await state.get_state() == composer.QuantityStates.speed.state
        assert (await state.get_data())["idempotency_key"] == "original"
    message.text = "2 2 0,5 4 7"
    await composer.input_speed(message, state)
    assert await state.get_state() == composer.QuantityStates.preview.state
    assert (await state.get_data())["idempotency_key"] != "original"

    creator = AsyncMock(return_value={"task_id": 12})
    monkeypatch.setattr(composer, "get_user_backend_id", AsyncMock(return_value=1))
    monkeypatch.setattr(mention_choice, "ensure_mention_choice", AsyncMock(return_value=SimpleNamespace(mode="plain")))
    monkeypatch.setattr(drafts, "reset_draft", lambda *_: None)
    monkeypatch.setattr(composer.api_client, "create_task", creator)
    monkeypatch.setattr(composer.api_client, "get_task", AsyncMock(return_value={}))
    monkeypatch.setattr(composer.api_client, "get_publication_activity", AsyncMock(return_value={}))
    await composer.create_order(callback, state)
    assert creator.await_args.kwargs["settings"]["publication_options"] == {
        "send_workers": 2, "check_workers": 2, "send_interval": .5, "check_delay": 4, "live_interval": 7,
    }


def test_quantities_are_independent_from_number_of_texts():
    task = TaskCreate(user_id=1, main_comment="one", text_variants=["one"], video_urls=[
        "https://www.tiktok.com/@test/video/7704037684280446216"],
        settings={"publication_plan": {"main_count": 60, "replies_per_main": 2}, "replies": ["answer"]})
    assert len(task.text_variants) == 1
    assert task.settings["publication_plan"]["main_count"] == 60
    assert task.settings["publication_plan"]["max_dispatches"] == 1800


@pytest.mark.parametrize("plan", [
    {"main_count": True}, {"main_count": 0}, {"main_count": 501},
    {"main_count": 5, "replies_per_main": -1}, {"main_count": 5, "replies_per_main": 21},
    {"main_count": 5, "max_dispatches": 4}, {"main_count": 5, "max_seconds": 999999},
])
def test_plan_rejects_unbounded_or_ambiguous_inputs(plan):
    with pytest.raises(ValidationError):
        QuantityPlan.model_validate(plan)


def test_templates_validate_and_keep_quantities():
    payload = {"publication_plan": {"main_count": 60, "replies_per_main": 2}}
    template = TaskTemplateCreate(user_id=1, name="test", main_comment="main", settings=payload)
    updated = TaskTemplateUpdate(settings=payload)
    assert template.settings["publication_plan"] == updated.settings["publication_plan"]
    with pytest.raises(ValidationError):
        TaskTemplateUpdate(settings={"publication_plan": {"main_count": -1}})


def test_slot_creation_and_claim_are_idempotent(db_session, sample_task):
    url = sample_task.video_urls[0]
    ensure_actions(db_session, sample_task, url, "main", 5, ["a", "b"])
    ensure_actions(db_session, sample_task, url, "main", 5, ["a", "b"])
    rows = db_session.query(QuantityAction).order_by(QuantityAction.slot).all()
    assert len(rows) == 5
    assert [r.text for r in rows] == ["a", "b", "a", "b", "a"]
    assert claim_action(db_session, rows[0].id)
    assert claim_action(db_session, rows[0].id) is None


def test_uncertain_delivery_slot_is_never_reclaimed(db_session, sample_task):
    ensure_actions(db_session, sample_task, sample_task.video_urls[0], "main", 1, ["a"])
    row = db_session.query(QuantityAction).one()
    row.state = "uncertain"
    db_session.commit()
    assert claim_action(db_session, row.id) is None


def test_reply_slots_bind_to_physical_parent(db_session, sample_task):
    url = sample_task.video_urls[0]
    ensure_actions(db_session, sample_task, url, "main", 2, ["same text"])
    mains = db_session.query(QuantityAction).order_by(QuantityAction.slot).all()
    for index, main in enumerate(mains):
        main.comment_id, main.state = str(7704740811904828192 + index), "visible"
        ensure_actions(db_session, sample_task, url, "reply", 2, ["answer"], parent=main)
    rows = db_session.query(QuantityAction).filter(QuantityAction.kind == "reply").all()
    assert len(rows) == 4
    assert len({r.parent_id for r in rows}) == 2
    assert len({r.id for r in rows}) == 4


def test_result_requires_all_requested_visible_publications(db_session, sample_task):
    url, plan = sample_task.video_urls[0], QuantityPlan(main_count=2, replies_per_main=2)
    ensure_actions(db_session, sample_task, url, "main", 2, ["same"])
    parents = db_session.query(QuantityAction).all()
    for index, parent in enumerate(parents):
        parent.comment_id, parent.state = str(7704740811904828192 + index), "visible"
        ensure_actions(db_session, sample_task, url, "reply", 2, ["answer"], parent=parent)
    replies = db_session.query(QuantityAction).filter(QuantityAction.kind == "reply").all()
    for index, reply in enumerate(replies):
        reply.comment_id, reply.state = str(7704740811904828292 + index), "visible"
    db_session.commit()
    result = build_result(db_session, sample_task, url, plan).to_dict()
    assert result["status"] == "success"
    from services.task_execution_ledger import url_result_counts_as_success
    assert url_result_counts_as_success(result)
    assert result["quantity_plan"]["visible_main"] == 2
    assert result["quantity_plan"]["visible_replies"] == 4
    replies[0].state = "accepted"
    db_session.commit()
    result = build_result(db_session, sample_task, url, plan).to_dict()
    assert result["status"] == "partial"
    assert not result["publication_complete"]


@pytest.mark.asyncio
async def test_engine_sends_requested_count_and_replies_only_after_visible_parent(db_session, sample_task, monkeypatch):
    import executor.quantity_engine as engine
    import executor.engagement_engine as engagement
    from sqlalchemy.orm import Session
    url = "https://www.tiktok.com/@test/video/7704037684280446216"
    sample_task.video_urls = [url]
    sample_task.status = "in_progress"
    sample_task.settings = {"publication_plan": {"main_count": 5, "replies_per_main": 2}, "replies": ["answer"]}
    db_session.commit()
    monkeypatch.setattr(engagement, "resolve_order_video_url", AsyncMock(return_value=(url, "7704037684280446216")))
    sends = []
    async def publish(bind, task_id, identity, **kwargs):
        with Session(bind) as db:
            action = db.get(QuantityAction, identity)
            if action.kind == "reply":
                assert db.get(QuantityAction, action.parent_action_id).state == "visible"
            sends.append(action.kind)
            action.comment_id = str(7704740811904828192 + len(sends))
            action.state = "accepted"
            db.add(PublicationAttempt(id=identity, task_id=task_id, video_url=url, kind=action.kind,
                text=action.text, text_hash="h", status="accepted", wire_count=1,
                comment_id=action.comment_id, started_at=utcnow()))
            db.commit()
    async def verify(bind, task_id, identities, stop, **kwargs):
        with Session(bind) as db:
            for identity in identities:
                db.get(QuantityAction, identity).state = "visible"
            db.commit()
    monkeypatch.setattr(engine, "publish_action", publish)
    monkeypatch.setattr(engine, "verify_actions", verify)
    result = await execute_quantity_order(db_session, sample_task, url, ["one", "two"], sample_task.id)
    assert sends.count("main") == 5
    assert sends.count("reply") == 10
    assert result.to_dict()["quantity_plan"]["complete"]


def test_preview_separates_text_library_from_publication_counts():
    from bot.handlers.quantity_orders import preview_text
    text = preview_text(dict(publication_plan={"main_count": 60, "replies_per_main": 2},
        texts=["one", "two", "three"], replies=["answer"], links=["x"]))
    assert "<b>60</b>" in text and "<b>120</b>" in text
    assert "Вариантов главного текста: 3" in text
