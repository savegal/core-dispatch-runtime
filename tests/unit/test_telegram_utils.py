import pytest
from aiogram.exceptions import TelegramBadRequest

from bot.services.telegram_utils import (
    is_stale_callback_error,
    safe_callback_answer,
)


class _FakeCallback:
    def __init__(self, exc=None):
        self.exc = exc
        self.payload = None

    async def answer(self, **kwargs):
        self.payload = kwargs
        if self.exc:
            raise self.exc


@pytest.mark.asyncio
async def test_safe_callback_answer_passes_payload():
    callback = _FakeCallback()

    assert await safe_callback_answer(callback, "ok", show_alert=True)
    assert callback.payload == {"text": "ok", "show_alert": True}


@pytest.mark.asyncio
async def test_safe_callback_answer_ignores_stale_query():
    exc = TelegramBadRequest(
        method=None,
        message="query is too old and response timeout expired or query ID is invalid",
    )
    callback = _FakeCallback(exc=exc)

    assert not await safe_callback_answer(callback)


def test_is_stale_callback_error_matches_telegram_message():
    exc = TelegramBadRequest(
        method=None,
        message="query is too old and response timeout expired or query ID is invalid",
    )

    assert is_stale_callback_error(exc)
