from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from aiogram.exceptions import TelegramBadRequest
from aiogram.types import CallbackQuery, Message


def format_compact_age(iso_value: str | None) -> str | None:
    """Formats an ISO timestamp into a short age string like 5m / 2h / 1d."""
    if not iso_value:
        return None

    try:
        normalized = str(iso_value).replace("Z", "+00:00")
        dt = datetime.fromisoformat(normalized)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        now = datetime.now(timezone.utc)
        delta = max(0, int((now - dt.astimezone(timezone.utc)).total_seconds()))
    except Exception:
        return None

    if delta < 60:
        return f"{delta}s"
    if delta < 3600:
        return f"{delta // 60}m"
    if delta < 86400:
        return f"{delta // 3600}h"
    return f"{delta // 86400}d"


async def safe_edit_message_text(
    message: Message | None,
    text: str,
    *,
    unchanged_notice: str | None = None,
    **kwargs: Any,
) -> str | None:
    """Edits message text safely, suppressing no-op edits and retrying without parse mode."""
    if message is None:
        return None

    try:
        await message.edit_text(text, **kwargs)
        return None
    except TelegramBadRequest as exc:
        error_text = str(exc).lower()
        if "message is not modified" in error_text:
            return unchanged_notice
        if kwargs.get("parse_mode") and "can't parse entities" in error_text:
            fallback_kwargs = dict(kwargs)
            fallback_kwargs.pop("parse_mode", None)
            await message.edit_text(text, **fallback_kwargs)
            return "plain_text_fallback"
        raise


async def safe_edit_callback_text(
    callback: CallbackQuery,
    text: str,
    *,
    unchanged_notice: str | None = None,
    **kwargs: Any,
) -> str | None:
    """Convenience wrapper around safe_edit_message_text for callback handlers."""
    return await safe_edit_message_text(
        callback.message,
        text,
        unchanged_notice=unchanged_notice,
        **kwargs,
    )
