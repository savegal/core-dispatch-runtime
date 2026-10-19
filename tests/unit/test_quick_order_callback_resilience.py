from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest
from aiogram.exceptions import TelegramBadRequest
from aiogram.types import CallbackQuery

import bot.handlers.quick_order as quick_order_module


@pytest.mark.asyncio
async def test_back_to_template_choice_continues_after_stale_callback(monkeypatch):
    stale_error = TelegramBadRequest(
        method=None,
        message="query is too old and response timeout expired or query ID is invalid",
    )
    callback = Mock(spec=CallbackQuery)
    callback.from_user = SimpleNamespace(id=123)
    callback.answer = AsyncMock(side_effect=stale_error)
    callback.message = SimpleNamespace(edit_text=AsyncMock())
    state = SimpleNamespace(
        get_data=AsyncMock(return_value={}),
        clear=AsyncMock(),
    )

    monkeypatch.setattr(
        quick_order_module,
        "get_user_backend_id",
        AsyncMock(return_value=7),
    )
    monkeypatch.setattr(
        quick_order_module.api_client,
        "list_task_templates",
        AsyncMock(return_value=[]),
    )

    await quick_order_module.back_to_template_choice(callback, state)

    callback.message.edit_text.assert_awaited_once_with("У вас пока нет шаблонов.")
    state.clear.assert_awaited_once()

