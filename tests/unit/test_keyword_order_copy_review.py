"""Exercise real order callbacks/FSM, with only API and Telegram I/O replaced."""
import asyncio
from datetime import datetime, timezone
import time
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest
from aiogram.fsm.context import FSMContext
from aiogram.fsm.storage.base import StorageKey
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import Chat, Message, User

from bot.handlers import engagement_wizard as wizard, orders
from bot.services.api_client import APIRequestError
from bot.services.mention_choice import PENDING_KEY, SELECTED_KEY
from bot.state.engagement_states import EngagementStates


URL = "https://www.tiktok.com/@fixture/video/7700000000000000001"
URL_TWO = "https://www.tiktok.com/@fixture/video/7700000000000000002"


@pytest.fixture
async def state():
    storage = MemoryStorage()
    context = FSMContext(storage, StorageKey(bot_id=1, chat_id=12345, user_id=12345))
    yield context
    await storage.close()


@pytest.fixture
def callback():
    user = SimpleNamespace(id=12345, username="fixture")
    message = SimpleNamespace(chat=SimpleNamespace(id=12345), message_id=77,
                              edit_text=AsyncMock(), edit_reply_markup=AsyncMock(), answer=AsyncMock())
    return SimpleNamespace(data="order:copy:7", from_user=user, message=message, answer=AsyncMock())


def order_fixture(urls=None):
    return {"id": 7, "kind": "keyword_replies", "status": "pending", "main_comment": "Ответы по ключевым словам",
            "video_urls": urls or [URL], "settings": {
                "order_transport_mode": "http", "keywords": ["цена", "где купить"],
                "replies": ["Первый", "Второй"], "reply_count": 2,
                "account_pool_mode": "personal", "verify_reply_visibility": True,
                "template_id": 4, "template_name": "Шаблон <ответов>",
                "delays": False, "shuffle": True, "rotate_accounts": False,
                "keyword_scans": {"old": {"complete": True}}, "url_results": {URL: {"status": "success"}},
                "client_task_fingerprint": "old-request", "mention_mode": "attempt"}}


@pytest.mark.parametrize("urls", [[URL], [URL, URL_TWO]])
async def test_copy_callback_preserves_keyword_kind_and_all_videos_through_real_confirmation(
    state, callback, monkeypatch, urls,
):
    source = order_fixture(urls)
    await state.update_data(order_kind="full", selected_mention_choice={"token": "old"}, keyword_scans={"old": {}},
                            order_creation_request={"key": "old-request"})
    monkeypatch.setattr(orders, "get_user_backend_id", AsyncMock(return_value=1))
    monkeypatch.setattr(wizard, "get_user_backend_id", AsyncMock(return_value=1))
    monkeypatch.setattr(orders.api_client, "get_task", AsyncMock(return_value=source))
    draft = Mock(side_effect=AssertionError("keyword copy must not enter generic Composer"))
    monkeypatch.setattr(orders, "get_draft", draft)
    create = AsyncMock(return_value={"task_id": 8, "order_transport_mode": "http"})
    monkeypatch.setattr(wizard.api_client, "create_task", create)
    monkeypatch.setattr(wizard, "_boost_likes_ui_config", AsyncMock(side_effect=AssertionError("no paid boost")))

    await orders.copy_order(callback, state)
    copied = await state.get_data()
    assert await state.get_state() == EngagementStates.CONFIRM_ORDER.state
    assert copied["order_kind"] == "keyword_replies" and copied["video_urls"] == urls
    assert copied["keywords"] == source["settings"]["keywords"]
    assert copied["replies"] == source["settings"]["replies"]
    assert not {"keyword_scans", "url_results", "client_task_fingerprint", SELECTED_KEY,
                "order_creation_request"}.intersection(copied)
    assert copied["template_id"] == 4 and copied["account_pool_mode"] == "personal"
    assert not draft.called and not create.called
    preview = callback.message.edit_text.await_args.args[0]
    assert "HTTP" in preview and "ответы по ключевым словам" in preview
    if len(urls) > 1:
        assert "Всего видео: 2" in preview

    callback.data = "engagement:confirm"
    await wizard.confirm_engagement_order(callback, state)
    payload = create.await_args.kwargs
    assert payload["kind"] == "keyword_replies" and payload["video_urls"] == urls
    assert payload["replies"] == source["settings"]["replies"]
    assert payload["settings"]["keywords"] == source["settings"]["keywords"]
    assert payload["settings"]["verify_reply_visibility"] is True
    assert payload["settings"]["delays"] is False and payload["settings"]["shuffle"] is True
    assert payload["settings"]["rotate_accounts"] is False and payload["likes"] == 0
    assert payload["settings"]["mention_mode"] == "plain"
    assert not {"keyword_scans", "url_results", "client_task_fingerprint"}.intersection(payload["settings"])
    assert payload["idempotency_key"] != "old-request" and await state.get_data() == {}
    if len(urls) > 1:
        assert "Видео в заказе: 2" in callback.message.edit_text.await_args.args[0]


@pytest.mark.parametrize("kind", ["keyword_replies", "engagement_only"])
async def test_reply_only_retry_accepts_personal_accounts_available_for_replies(callback, monkeypatch, kind):
    task = order_fixture()
    task.update(kind=kind, status="error")
    task["settings"].update(text_variants=["Один", "Два", "Три"],
                            failed_urls=[URL], url_results={URL: {"status": "fail", "error": "no_accounts"}})
    callback.data = "order:retry_failed:7"
    monkeypatch.setattr(orders, "get_user_backend_id", AsyncMock(return_value=1))
    monkeypatch.setattr(orders.api_client, "get_task", AsyncMock(return_value=task))
    monkeypatch.setattr(orders.api_client, "get_personal_pool_summary", AsyncMock(return_value={
        "available_reply_now": 1, "available_main_now": 0}))
    create = AsyncMock(return_value={"task_id": 8})
    monkeypatch.setattr(orders.api_client, "create_task", create)
    await orders.retry_failed_order(callback)
    assert create.await_count == 1
    assert create.await_args.kwargs["kind"] == kind
    assert create.await_args.kwargs["settings"]["account_pool_mode"] == "personal"
    assert "keyword_scans" not in create.await_args.kwargs["settings"]


async def test_keyword_main_text_editor_is_hidden_and_stale_callback_cannot_report_success(state, callback, monkeypatch):
    task = order_fixture()
    keyboard = orders._build_order_detail_keyboard(task, "overview", 0, "all")
    callbacks = {button.callback_data for row in keyboard.inline_keyboard for button in row}
    assert "order:texts:7" not in callbacks
    callback.data = "order:texts:7"
    monkeypatch.setattr(orders, "get_user_backend_id", AsyncMock(return_value=1))
    monkeypatch.setattr(orders.api_client, "get_task", AsyncMock(return_value=task))
    await orders.order_replace_texts(callback, state)
    assert await state.get_state() is None and await state.get_data() == {}
    assert "копию" in callback.answer.await_args.args[0]
    task["kind"] = "full"
    keyboard = orders._build_order_detail_keyboard(task, "overview", 0, "all")
    assert "order:texts:7" in {button.callback_data for row in keyboard.inline_keyboard for button in row}


async def test_plain_confirmation_concurrent_clicks_and_timeout_retry_keep_one_request_key(state, callback, monkeypatch):
    callback.data = "engagement:confirm"
    await state.set_state(EngagementStates.CONFIRM_ORDER)
    await state.update_data(order_kind="keyword_replies", order_create_token="wizard-nonce", video_url=URL,
                            keywords=["цена"], comment_text="цена", replies=["Ответ"], account_pool_mode="shared")
    monkeypatch.setattr(wizard, "get_user_backend_id", AsyncMock(return_value=1))
    entered = asyncio.Event()
    keys = []
    async def create(**kwargs):
        keys.append(kwargs["idempotency_key"])
        if len(keys) == 2:
            entered.set()
        await asyncio.wait_for(entered.wait(), timeout=2)
        raise APIRequestError("Acknowledgement lost after server accepted request")
    monkeypatch.setattr(wizard.api_client, "create_task", create)
    # Real ensure_mention_choice returns a plain choice without its own key.
    await asyncio.gather(wizard.confirm_engagement_order(callback, state),
                         wizard.confirm_engagement_order(callback, state))
    assert len(keys) == 2 and keys[0] == keys[1]
    await wizard.confirm_engagement_order(callback, state)
    assert keys[2] == keys[0]
    await state.update_data(replies=["Изменённый ответ"])
    await wizard.confirm_engagement_order(callback, state)
    assert keys[3] != keys[0]


async def test_confirmation_retains_real_selected_mention_request_key(state, callback, monkeypatch):
    callback.data = "engagement:confirm"
    await state.set_state(EngagementStates.CONFIRM_ORDER)
    await state.update_data(order_kind="keyword_replies", order_create_token="wizard-nonce", video_url=URL,
                            keywords=["цена"], comment_text="цена", replies=["Спросите @fixture"], account_pool_mode="shared")
    monkeypatch.setattr(wizard, "get_user_backend_id", AsyncMock(return_value=1))
    create = AsyncMock(return_value={"task_id": 9, "order_transport_mode": "http"})
    monkeypatch.setattr(wizard.api_client, "create_task", create)
    await wizard.confirm_engagement_order(callback, state)
    pending = (await state.get_data())[PENDING_KEY]
    assert not create.called
    await state.update_data(**{SELECTED_KEY: {**pending, "mode": "plain", "created_at": time.time()}, PENDING_KEY: None})
    await wizard.confirm_engagement_order(callback, state)
    assert create.await_args.kwargs["idempotency_key"] == "mentions-" + pending["token"]
    assert create.await_args.kwargs["settings"]["mention_mode"] == "plain"


async def test_copied_multi_video_order_keeps_links_when_editing_keys_and_replies(state, callback, monkeypatch):
    monkeypatch.setattr(orders, "get_user_backend_id", AsyncMock(return_value=1))
    monkeypatch.setattr(wizard, "get_user_backend_id", AsyncMock(return_value=1))
    monkeypatch.setattr(orders.api_client, "get_task", AsyncMock(return_value=order_fixture([URL, URL_TWO])))
    create = AsyncMock(return_value={"task_id": 9, "order_transport_mode": "http"})
    monkeypatch.setattr(wizard.api_client, "create_task", create)
    await orders.copy_order(callback, state)
    assert "без повторной отправки" in callback.message.edit_text.await_args.args[0]

    callback.data = "engagement:edit_keywords"
    await wizard.edit_keyword_order_keys(callback, state)
    assert await state.get_state() == EngagementStates.INPUT_COMMENT_TEXT.state
    # Real Message shape exercises the message branch of preview, without Telegram I/O.
    message = Message(message_id=88, date=datetime.now(timezone.utc),
                      chat=Chat(id=12345, type="private"), from_user=User(id=12345, is_bot=False, first_name="Fixture"),
                      text="доставка, цена").as_(AsyncMock())
    await wizard.process_comment_text(message, state)
    assert (await state.get_data())["keywords"] == ["доставка", "цена"]
    assert await state.get_state() == EngagementStates.CONFIRM_ORDER.state

    callback.data = "engagement:edit_replies"
    await wizard.edit_engagement_replies(callback, state)
    message = message.model_copy(update={"text": "Новый ответ"})
    await wizard.process_engagement_reply(message, state)
    callback.data = "engagement:replies_done"
    await wizard.finish_engagement_replies(callback, state)
    callback.data = "engagement:confirm"
    await wizard.confirm_engagement_order(callback, state)
    payload = create.await_args.kwargs
    assert payload["kind"] == "keyword_replies" and payload["video_urls"] == [URL, URL_TWO]
    assert payload["settings"]["keywords"] == ["доставка", "цена"]
    assert "Новый ответ" in payload["replies"]
    assert payload["settings"]["verify_reply_visibility"] is True


@pytest.mark.parametrize("kind,replacement_label", [("keyword_replies", False), ("engagement_only", True)])
async def test_visibility_toggle_describes_actual_replacement_policy(state, callback, monkeypatch, kind, replacement_label):
    from bot.handlers import order_wizard
    await state.update_data(order_kind=kind, video_url=URL, keywords=["цена"], comment_text="цена",
                            replies=["Ответ"], verify_reply_visibility=False)
    monkeypatch.setattr(wizard, "_boost_likes_ui_config", AsyncMock(return_value=(False, 0)))
    monkeypatch.setattr(wizard, "current_order_transport_label", AsyncMock(return_value="HTTP"))
    await wizard.show_engagement_preview(callback, state)
    markup = callback.message.edit_text.await_args.kwargs["reply_markup"]
    callback.message.reply_markup = markup
    callback.data = "order:reply_visibility:1"
    # This router precedes engagement_wizard in bot.app and shares the prefix.
    await order_wizard.wizard_reply_visibility_option(callback, state)
    assert (await state.get_data())["verify_reply_visibility"] is True
    enabled_markup = callback.message.edit_reply_markup.await_args.kwargs["reply_markup"]
    button = next(button for row in enabled_markup.inline_keyboard for button in row
                  if (button.callback_data or "").startswith("order:reply_visibility:"))
    assert ("+ замена" in button.text) is replacement_label
    assert ("без повтора" in button.text) is not replacement_label
    assert button.callback_data == "order:reply_visibility:0"


def test_running_keyword_map_uses_durable_aggregate_without_fabricating_receipt_rows():
    task = order_fixture()
    task["settings"]["url_results"] = {URL: {
        "status": "in_progress", "keyword_replies": {"matched": 35, "replied": 20, "unconfirmed": 1},
        "engagement": {"main": {}, "replies": {"ok": 20, "expected": 35, "items": [{
            "parent_comment_id": "7700000000000000003", "reply_comment_id": "7700000000000000004",
            "matched_main_text": "Какая цена?", "text": "Ответ", "success": True, "method": "http",
            "network_confirmed": True,
        }]}}}}
    lines = orders._publication_map_summary_lines(task)
    assert "Найденных комментариев: 35" in lines
    assert "Ответов подтверждено: 20" in lines
    assert "Отправок без подтверждения: 1" in lines
    assert any("Подробностей в карте: 1 из 35" in line for line in lines)
    rows, _, _ = orders._build_publication_map_rows(task)
    assert len(rows) == 1 and len(rows[0]["replies"]) == 1


def test_keyword_order_details_describe_observation_without_replacement():
    task = order_fixture()
    text = orders._render_order_settings(task)
    assert "Тип: ответы по ключевым словам" in text
    assert "Проверка видимости ответов: вкл, без повторной отправки" in text
    task["kind"] = "engagement_only"
    assert "Проверка видимости ответов: вкл, с заменой" in orders._render_order_settings(task)
