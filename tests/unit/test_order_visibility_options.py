"""Order previews expose independent visibility choices and submit those choices."""
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest
from aiogram.types import Message

from bot.handlers import engagement_wizard, order_wizard, quick_order, video_parsing_new_videos
from bot.services.main_visibility_ui import CALLBACK_PREFIX, REPLY_CALLBACK_PREFIX


pytestmark = pytest.mark.unit
URL = "https://www.tiktok.com/@creator/video/765000000000000001"


class State:
    def __init__(self, **data):
        self.data = data
        self.cleared = False

    async def get_data(self):
        return dict(self.data)

    async def update_data(self, **data):
        self.data.update(data)

    async def set_state(self, state):
        self.current_state = state

    async def clear(self):
        self.cleared = True
        self.data.clear()


def callback(data):
    return SimpleNamespace(
        data=data, from_user=SimpleNamespace(id=123, username="operator"), answer=AsyncMock(),
        message=SimpleNamespace(edit_text=AsyncMock(), edit_reply_markup=AsyncMock()),
        bot=SimpleNamespace(send_chat_action=AsyncMock()),
    )


def preview_markup(cb):
    return cb.message.edit_text.await_args.kwargs["reply_markup"]


def assert_options(markup, main=False, reply=False, *, replies_only=False):
    callbacks = [button.callback_data for row in markup.inline_keyboard for button in row]
    main_buttons = [value for value in callbacks if value and value.startswith(CALLBACK_PREFIX)]
    reply_buttons = [value for value in callbacks if value and value.startswith(REPLY_CALLBACK_PREFIX)]
    assert main_buttons == ([] if replies_only else [f"{CALLBACK_PREFIX}{int(not main)}"])
    assert reply_buttons == [f"{REPLY_CALLBACK_PREFIX}{int(not reply)}"]


async def choose(cb, state, main, reply):
    cb.message.reply_markup = preview_markup(cb)
    for prefix, value, handler in (
        (CALLBACK_PREFIX, main, quick_order.quick_order_visibility_option),
        (REPLY_CALLBACK_PREFIX, reply, quick_order.quick_order_reply_visibility_option),
    ):
        cb.data = f"{prefix}{int(value)}"
        await handler(cb, state)
        cb.message.reply_markup = cb.message.edit_reply_markup.await_args.kwargs["reply_markup"]
    assert_options(cb.message.reply_markup, main, reply)


@pytest.fixture
def setup_orders(monkeypatch):
    template = {
        "id": 77, "name": "Template", "main_comment": "one", "likes": 0,
        "settings": {"text_variants": ["one", "two", "three"], "reply_count": 1, "replies": ["reply"]},
    }
    api = SimpleNamespace(
        list_task_templates=AsyncMock(return_value=[template]),
        has_active_accounts=AsyncMock(return_value=True),
        create_task=AsyncMock(return_value={"task_id": 321}),
        get_geo_recommendation=AsyncMock(return_value={}),
        get_video_batch=AsyncMock(return_value={"creator_id": 44, "video_ids": [1]}),
        get_videos_by_ids=AsyncMock(return_value=[{
            "id": 1, "url": URL, "creator_id": 44, "creator_username": "creator", "status": "new",
        }]),
        get_pricing=AsyncMock(return_value={"price_success": 5}),
        link_videos_to_tasks=AsyncMock(),
    )
    draft = SimpleNamespace(links=[URL], comment="one", text_variants=["one", "two", "three"])
    for module in (quick_order, order_wizard, video_parsing_new_videos, engagement_wizard):
        monkeypatch.setattr(module, "api_client", api)
        monkeypatch.setattr(module, "get_user_backend_id", AsyncMock(return_value=55))
    for module in (quick_order, order_wizard):
        monkeypatch.setattr(module, "get_draft", lambda user_id: draft)
        monkeypatch.setattr(module, "reset_draft", Mock())
    monkeypatch.setattr(order_wizard, "save_draft", Mock())
    monkeypatch.setattr(quick_order, "ensure_access", AsyncMock(return_value=(True, {"id": 55})))
    monkeypatch.setattr(quick_order, "get_likes_display_label_for_replies", AsyncMock(return_value="выключены"))
    monkeypatch.setattr(order_wizard, "_likes_display_label_for_replies", AsyncMock(return_value="выключены"))
    return api, template


@pytest.mark.parametrize("main,reply", [(False, False), (True, False), (False, True), (True, True)])
async def test_quick_template_preview_to_order_keeps_both_choices(setup_orders, monkeypatch, main, reply):
    api, template = setup_orders
    state = State(account_pool_mode="shared")
    cb = callback("template:select:77")
    await quick_order.select_template(cb, state)
    assert_options(preview_markup(cb))
    await choose(cb, state, main, reply)
    # Editing/reopening the selected template must retain the per-order choice.
    cb.data = "template:select:77"
    await quick_order.select_template(cb, state)
    assert_options(preview_markup(cb), main, reply)
    import bot.services.order_utils
    monkeypatch.setattr(bot.services.order_utils, "validate_draft_has_links", AsyncMock(return_value=True))
    cb.data = "quicktpl:confirm:77:1"
    await quick_order.confirm_template(cb, state)
    api.create_task.assert_awaited_once()
    settings = api.create_task.await_args.kwargs["settings"]
    assert settings["verify_main_visibility"] is main
    assert settings["verify_reply_visibility"] is reply
    assert "verify_main_visibility" not in template["settings"]
    assert state.cleared


@pytest.mark.parametrize("main,reply", [(False, False), (True, False), (False, True), (True, True)])
async def test_manual_final_preview_to_order_keeps_both_choices(setup_orders, main, reply):
    api, _ = setup_orders
    state = State(text_variants=["one", "two", "three"], replies={1: "reply"}, reply_count=1, account_pool_mode="shared")
    cb = callback("wizard:create_order")
    await order_wizard.ask_save_template_with_preview(cb, state)
    assert_options(preview_markup(cb))
    await choose(cb, state, main, reply)
    await order_wizard.wizard_create_order_from_callback(cb, state)
    api.create_task.assert_awaited_once()
    settings = api.create_task.await_args.kwargs["settings"]
    assert settings["verify_main_visibility"] is main
    assert settings["verify_reply_visibility"] is reply
    assert state.cleared


async def test_manual_message_preview_shows_existing_choices(setup_orders):
    state = State(text_variants=["one", "two", "three"], replies={}, verify_main_visibility=True, verify_reply_visibility=True)
    message = Mock(spec=Message)
    message.from_user = SimpleNamespace(id=123)
    message.answer = AsyncMock()
    await order_wizard.ask_save_template_with_preview(message, state)
    message.answer.assert_awaited_once()
    assert_options(message.answer.await_args.kwargs["reply_markup"], True, True)


@pytest.mark.parametrize("main,reply", [(False, False), (True, False), (False, True), (True, True)])
async def test_new_video_card_to_order_keeps_both_choices(setup_orders, main, reply):
    api, _ = setup_orders
    state = State()
    cb = callback("vp:new_videos:template_selected:44:901:77")
    await video_parsing_new_videos.new_videos_template_selected(cb, state)
    assert_options(preview_markup(cb))
    await choose(cb, state, main, reply)
    cb.data = "vp:new_videos:template_selected:44:901:77"
    await video_parsing_new_videos.new_videos_template_selected(cb, state)
    assert_options(preview_markup(cb), main, reply)
    cb.data = "vp:new_videos:confirm_order:44:901:77"
    await video_parsing_new_videos.new_videos_confirm_order(cb, state)
    api.create_task.assert_awaited_once()
    settings = api.create_task.await_args.kwargs["settings"]
    assert settings["verify_main_visibility"] is main
    assert settings["verify_reply_visibility"] is reply
    assert state.cleared


@pytest.mark.parametrize("enabled", [False, True])
async def test_reply_only_preview_offers_only_reply_choice(setup_orders, monkeypatch, enabled):
    api, _ = setup_orders
    monkeypatch.setattr(engagement_wizard, "_boost_likes_ui_config", AsyncMock(return_value=(False, 0)))
    state = State(video_url=URL, comment_text="parent", replies=["reply"], account_pool_mode="shared")
    cb = callback("engagement:confirm")
    await engagement_wizard.show_engagement_preview(cb, state)
    cb.message.reply_markup = preview_markup(cb)
    assert_options(cb.message.reply_markup, replies_only=True)
    cb.data = f"{REPLY_CALLBACK_PREFIX}{int(enabled)}"
    await engagement_wizard.engagement_reply_visibility_option(cb, state)
    assert_options(cb.message.edit_reply_markup.await_args.kwargs["reply_markup"], reply=enabled, replies_only=True)
    cb.data = "engagement:confirm"
    await engagement_wizard.confirm_engagement_order(cb, state)
    api.create_task.assert_awaited_once()
    request = api.create_task.await_args.kwargs
    assert request["settings"]["verify_reply_visibility"] is enabled
    assert "verify_main_visibility" not in request["settings"]
    assert request["kind"] == "engagement_only"
