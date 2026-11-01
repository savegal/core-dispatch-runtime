import asyncio
from copy import deepcopy
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from pydantic import ValidationError

from services.mention_policy import (
    find_mentions, current_mention_mode, set_task_mention_mode, reset_task_mention_mode,
)
from bot.services import mention_choice as choice_ui
from bot.handlers import mention_choice as choice_handler
from executor.http import mentions
from executor.http.playwright_http_client import PlaywrightHttpClient
from tests.unit.test_task_create_validators import _base_payload


class State:
    def __init__(self):
        self.data = {}

    async def get_data(self):
        return deepcopy(self.data)

    async def update_data(self, **data):
        self.data.update(deepcopy(data))

    async def clear(self):
        self.data.clear()


def message():
    return SimpleNamespace(message_id=23, chat=SimpleNamespace(id=41), edit_text=AsyncMock())


def callback(msg, data, user_id=7):
    return SimpleNamespace(message=msg, data=data, from_user=SimpleNamespace(id=user_id), answer=AsyncMock())


@pytest.mark.parametrize("text,expected", [
    ("@person", ["person"]), ("🔥 @User. @friend...", ["User", "friend"]),
    ("a@domain.com @@person", []), ("https://www.tiktok.com/@person", []),
    ("https://www.tiktok.com/@person @friend", ["friend"]),
    ("@" + "u" * 24 + ".", ["u" * 24]), ("@" + "u" * 25, []),
])
def test_only_complete_handles_are_mention_candidates(text, expected):
    assert [name for _, _, name in find_mentions(text)] == expected


@pytest.mark.asyncio
async def test_plain_text_does_not_require_a_choice_or_state_read():
    msg = message()
    state = SimpleNamespace(get_data=AsyncMock(side_effect=AssertionError("No extra question")))
    selection = await choice_ui.ensure_mention_choice(msg, state, ["text", "a@domain.com"],
                                                       user_id=7, resume="wizard:confirm", scope=["video"])
    assert selection.mode == "plain" and not selection.request_kwargs
    msg.edit_text.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["plain", "attempt"])
async def test_choice_resumes_exact_order_once_and_reuses_idempotency_key(monkeypatch, mode):
    state, msg = State(), message()
    texts = ["🔥 привет @person"]
    assert await choice_ui.ensure_mention_choice(msg, state, texts, user_id=7, resume="wizard:confirm", scope=["video"]) is None
    buttons = msg.edit_text.await_args.kwargs["reply_markup"].inline_keyboard
    assert [row[0].text for row in buttons[:2]] == ["Попробовать отметить", "Оставить как текст"]
    assert all(len(button.callback_data.encode()) <= 64 for row in buttons for button in row)
    token = state.data[choice_ui.PENDING_KEY]["token"]
    resumed = []

    async def resume(cb, session, route):
        selection = await choice_ui.ensure_mention_choice(msg, session, texts, user_id=7, resume=route, scope=["video"])
        resumed.append(selection)
        await asyncio.sleep(0)

    monkeypatch.setattr(choice_handler, "_resume_order", resume)
    cb = callback(msg, f"{choice_ui.CALLBACK_PREFIX}{mode}:{token}")
    await asyncio.gather(choice_handler.choose_mention_mode(cb, state), choice_handler.choose_mention_mode(cb, state))
    assert len(resumed) == 1 and resumed[0].mode == mode
    again = await choice_ui.ensure_mention_choice(msg, state, texts, user_id=7, resume="wizard:confirm", scope=["video"])
    assert again.request_kwargs == resumed[0].request_kwargs
    assert again.request_kwargs["idempotency_key"].startswith("mentions-")
    # Editing either texts or target videos invalidates the previous choice.
    assert await choice_ui.ensure_mention_choice(msg, state, texts, user_id=7, resume="wizard:confirm", scope=["other-video"]) is None


@pytest.mark.asyncio
@pytest.mark.parametrize("alter", ["token", "actor", "message", "chat", "expired"])
async def test_stale_or_foreign_choice_cannot_submit(monkeypatch, alter):
    state, msg = State(), message()
    await choice_ui.ensure_mention_choice(msg, state, ["@person"], user_id=7, resume="wizard:confirm")
    pending = state.data[choice_ui.PENDING_KEY]
    cb = callback(msg, f'{choice_ui.CALLBACK_PREFIX}attempt:{pending["token"]}')
    if alter == "token": cb.data += "wrong"
    if alter == "actor": cb.from_user.id = 8
    if alter == "message": cb.message.message_id += 1
    if alter == "chat": cb.message.chat.id += 1
    if alter == "expired": pending["created_at"] -= 3601
    resume = AsyncMock()
    monkeypatch.setattr(choice_handler, "_resume_order", resume)
    await choice_handler.choose_mention_mode(cb, state)
    resume.assert_not_awaited()
    cb.answer.assert_awaited()


@pytest.mark.asyncio
async def test_more_than_three_handles_can_be_sent_as_plain_text(monkeypatch):
    state, msg = State(), message()
    await choice_ui.ensure_mention_choice(msg, state, ["@one @two @three @four"], user_id=7, resume="wizard:confirm")
    token = state.data[choice_ui.PENDING_KEY]["token"]
    resume = AsyncMock()
    monkeypatch.setattr(choice_handler, "_resume_order", resume)
    await choice_handler.choose_mention_mode(callback(msg, f"{choice_ui.CALLBACK_PREFIX}attempt:{token}"), state)
    resume.assert_not_awaited()
    await choice_handler.choose_mention_mode(callback(msg, f"{choice_ui.CALLBACK_PREFIX}plain:{token}"), state)
    resume.assert_awaited_once()


@pytest.mark.asyncio
@pytest.mark.parametrize("operation", ["main", "reply"])
async def test_default_plain_mode_preserves_text_and_never_resolves_or_accepts_entities(monkeypatch, operation):
    page = SimpleNamespace(evaluate=AsyncMock(side_effect=AssertionError("Plain text cannot resolve users")))
    client = PlaywrightHttpClient(SimpleNamespace(), page=page)
    receipt = {"comment": {"cid": "7123456789012345678", "reply_id": "7123456789099999999"}}
    execute = AsyncMock(return_value=(True, receipt, None, None))
    client._execute_request = execute
    prepare = AsyncMock(side_effect=AssertionError("Plain mode cannot prepare mentions"))
    monkeypatch.setattr(mentions, "prepare_mentions", prepare)
    text = "🔥 @Somebody @two @three @four — a@domain.com"
    if operation == "main":
        result = await client.publish_main_comment("123", text, text_extra="invalid-user-entities")
    else:
        result = await client.reply_comment("123", "7123456789099999999", text, text_extra="invalid-user-entities")
    assert result[0]
    params = execute.await_args.kwargs["dynamic_params"]
    assert params["text"] == text and params["text_extra"] == "[]"
    assert "mention_mode" not in params
    prepare.assert_not_awaited()
    page.evaluate.assert_not_awaited()


@pytest.mark.asyncio
async def test_order_policy_is_isolated_between_concurrent_publications(monkeypatch):
    async def prepare(_page, text, **kwargs):
        extras = mentions.build_text_extra(text, {"person": {"user_id": "123", "sec_uid": "sec"}})
        return text, json.dumps(extras), {"requested": ["person"], "resolved": ["person"], "unresolved": []}
    monkeypatch.setattr(mentions, "prepare_mentions", prepare)

    async def send(mode):
        token = set_task_mention_mode({"mention_mode": mode})
        try:
            await asyncio.sleep(0)
            client = PlaywrightHttpClient(SimpleNamespace())
            params = {"text": "🔥 @person"}
            assert await client._prepare_publication_mentions(params["text"], params) is None
            return json.loads(params["text_extra"])
        finally:
            reset_task_mention_mode(token)
    plain, attempt = await asyncio.gather(send("plain"), send("attempt"))
    assert plain == [] and attempt[0]["start"] == 3
    assert current_mention_mode() == "plain"


@pytest.mark.asyncio
async def test_saved_plain_choice_cannot_be_overridden_by_publication_kwargs():
    token = set_task_mention_mode({"mention_mode": "plain"})
    try:
        client = PlaywrightHttpClient(SimpleNamespace())
        client._execute_request = AsyncMock()
        result = await client.publish_main_comment("123", "hello @person", mention_mode="attempt")
        assert not result[0] and "saved order choice" in result[2]
        client._execute_request.assert_not_awaited()
    finally:
        reset_task_mention_mode(token)


@pytest.mark.parametrize("mode", ["plain", "attempt"])
def test_api_persists_explicit_mode_and_copy_preserves_it(mode):
    from api.tasks import TaskCreate
    from bot.handlers.orders import _build_recreated_order_settings
    payload = _base_payload()
    payload["settings"] = {"mention_mode": mode}
    model = TaskCreate(**payload)
    assert model.model_dump()["settings"]["mention_mode"] == mode
    assert _build_recreated_order_settings(model.settings, origin="copy")["mention_mode"] == mode


@pytest.mark.parametrize("value", [True, False, "auto", 1, {}, []])
def test_api_rejects_ambiguous_mention_mode(value):
    from api.tasks import TaskCreate
    payload = _base_payload()
    payload["settings"] = {"mention_mode": value}
    with pytest.raises(ValidationError):
        TaskCreate(**payload)


@pytest.mark.parametrize("mode,allowed", [("plain", True), ("attempt", False)])
def test_api_mention_limit_applies_only_to_selected_mention_mode(mode, allowed):
    from api.tasks import TaskCreate
    payload = _base_payload()
    payload["kind"] = "full"
    payload["text_variants"][0] = "@one @two @three @four"
    payload["settings"] = {"mention_mode": mode}
    if allowed:
        assert TaskCreate(**payload)
    else:
        with pytest.raises(ValidationError): TaskCreate(**payload)


@pytest.mark.asyncio
@pytest.mark.parametrize("identity", [None, [], {"user_id": "wrong", "sec_uid": "sec", "unique_id": "person"},
                                        {"user_id": "123", "sec_uid": "sec", "unique_id": "someoneelse"}])
async def test_invalid_resolved_identity_is_never_cached(identity):
    mentions._MENTION_CACHE.pop("person", None)
    page = SimpleNamespace(is_closed=lambda: False, evaluate=AsyncMock(return_value={"status": "ok", "user": identity}))
    assert await mentions.resolve_mention_user(page, "person") is None
    assert not mentions._cache_get("person")[0]


@pytest.mark.asyncio
async def test_new_order_revalidates_old_handle_cache_but_accounts_share_one_identity():
    mentions._cache_put("person", {"user_id": "111", "sec_uid": "old", "unique_id": "person"})
    page = SimpleNamespace(is_closed=lambda: False, evaluate=AsyncMock(return_value={"status": "ok", "user": {
        "user_id": "222", "sec_uid": "fresh", "unique_id": "person"}}))
    token = set_task_mention_mode({"mention_mode": "attempt"})
    try:
        first, second = await asyncio.gather(mentions.resolve_mention_user(page, "person"),
                                             mentions.resolve_mention_user(page, "person"))
        assert first["user_id"] == second["user_id"] == "222"
        page.evaluate.assert_awaited_once()
    finally:
        reset_task_mention_mode(token)
    page.evaluate.return_value["user"]["user_id"] = "333"
    token = set_task_mention_mode({"mention_mode": "attempt"})
    try:
        assert (await mentions.resolve_mention_user(page, "person"))["user_id"] == "333"
        assert page.evaluate.await_count == 2
    finally:
        reset_task_mention_mode(token)


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["plain", "attempt"])
async def test_real_quick_order_handler_waits_for_choice_then_sends_saved_mode(monkeypatch, mode):
    from bot.handlers import quick_order
    from bot.services import order_utils
    from bot.services.drafts import Draft
    state, msg = State(), message()
    state.data.update(new_template_text_variants=["first @person", "second", "third"], main_comment="first @person",
                      reply_count=0, replies={}, account_pool_mode="shared")
    draft = Draft(links=["https://www.tiktok.com/@demo/video/7635267329407913230"], comment="first @person")
    monkeypatch.setattr(quick_order, "ensure_access", AsyncMock(return_value=(True, {"id": 100})))
    monkeypatch.setattr(quick_order, "get_draft", lambda _: draft)
    monkeypatch.setattr(order_utils, "get_draft", lambda _: draft)
    monkeypatch.setattr(quick_order, "reset_draft", lambda _: None)
    monkeypatch.setattr(quick_order, "get_likes_display_label_for_replies", AsyncMock(return_value="выключены"))
    monkeypatch.setattr(quick_order.api_client, "has_active_accounts", AsyncMock(return_value=True))
    create = AsyncMock(return_value={"task_id": 456})
    monkeypatch.setattr(quick_order.api_client, "create_task", create)
    cb = callback(msg, "new_template:confirm")
    cb.bot = SimpleNamespace(send_chat_action=AsyncMock())
    await quick_order.confirm_new_template(cb, state)
    create.assert_not_awaited()
    token = state.data[choice_ui.PENDING_KEY]["token"]
    cb.data = f"{choice_ui.CALLBACK_PREFIX}{mode}:{token}"
    await choice_handler.choose_mention_mode(cb, state)
    create.assert_awaited_once()
    assert create.await_args.kwargs["settings"]["mention_mode"] == mode
    assert create.await_args.kwargs["idempotency_key"] == "mentions-" + token
    assert not state.data


@pytest.mark.asyncio
async def test_engagement_search_handle_is_not_a_published_mention(monkeypatch):
    from bot.handlers import engagement_wizard as wizard
    state, msg = State(), message()
    state.data.update(video_url="https://www.tiktok.com/@demo/video/7635267329407913230",
                      comment_text="find @person", replies=["plain reply"], account_pool_mode="shared")
    monkeypatch.setattr(wizard, "get_user_backend_id", AsyncMock(return_value=100))
    monkeypatch.setattr(wizard, "_boost_likes_ui_config", AsyncMock(return_value=(False, 0)))
    create = AsyncMock(return_value={"task_id": 789})
    monkeypatch.setattr(wizard.api_client, "create_task", create)
    await wizard.confirm_engagement_order(callback(msg, "engagement:confirm"), state)
    create.assert_awaited_once()
    assert create.await_args.kwargs["settings"]["mention_mode"] == "plain"
    assert not any("Попробовать" in str(call) for call in msg.edit_text.await_args_list)


@pytest.mark.asyncio
async def test_pool_selection_preserves_human_actor_for_mention_question(monkeypatch):
    from bot.handlers import order_wizard as wizard
    state, msg = State(), message()
    cb = callback(msg, "wizard:pool:shared", user_id=7)
    cb.message.from_user = SimpleNamespace(id=999999, is_bot=True)
    monkeypatch.setattr(wizard, "get_user_backend_id", AsyncMock(return_value=100))
    create = AsyncMock()
    monkeypatch.setattr(wizard, "_wizard_create_order_impl", create)
    await wizard.wizard_select_pool(cb, state)
    create.assert_awaited_once_with(msg, state, user_id=7, is_message=False)


@pytest.mark.parametrize("response,expected", [
    ({"comment": {}}, "unknown"), ({"data": []}, "unknown"),
    ({"comment": {"text_extra": []}}, "not_retained"),
    ({"comment": {"text_extra": [{"type": 0, "user_id": "1"}]}}, "partial"),
    ({"comment": {"text_extra": [{"type": 0, "user_id": "1"}, {"type": 0, "user_id": "2"}]}}, "retained"),
])
def test_receipt_distinguishes_missing_partial_and_omitted_mentions(response, expected):
    assert mentions.mention_receipt(response, ["1", "2"])["confirmation"] == expected
