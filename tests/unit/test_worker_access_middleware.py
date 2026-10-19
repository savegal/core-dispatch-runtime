from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest
from aiogram.types import Message

import bot.middleware.worker_access_middleware as worker_middleware_module
from bot.middleware.worker_access_middleware import WorkerAccessMiddleware


@pytest.mark.asyncio
async def test_worker_handler_error_is_not_reclassified_as_access_error(monkeypatch):
    event = Mock(spec=Message)
    event.from_user = SimpleNamespace(id=123, username="worker")
    event.answer = AsyncMock()
    handler = AsyncMock(side_effect=RuntimeError("handler failed"))

    monkeypatch.setattr(
        worker_middleware_module.api_client,
        "sync_telegram",
        AsyncMock(return_value={"id": 7, "role": "user"}),
    )
    monkeypatch.setattr(
        worker_middleware_module,
        "check_user_access",
        AsyncMock(return_value=True),
    )

    with pytest.raises(RuntimeError, match="handler failed"):
        await WorkerAccessMiddleware()(handler, event, {})

    event.answer.assert_not_awaited()

