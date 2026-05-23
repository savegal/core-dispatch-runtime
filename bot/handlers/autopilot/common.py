from __future__ import annotations

import inspect
from dataclasses import dataclass
from typing import Any, Awaitable, Callable
from urllib.parse import quote

from aiogram import F, Router
from aiogram.exceptions import TelegramBadRequest
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, InlineKeyboardMarkup

from bot.services.user_utils import get_user_backend_id
from bot.utils.markdown_escape import escape_markdown_simple


class AutopilotLimitsStates(StatesGroup):
    wait_input = State()


class ReportFiltersStates(StatesGroup):
    viewing = State()


DEFAULT_REPORT_FILTERS = {
    "ap_period": "24h",
    "ap_view": "summary",
    "ap_status": "all",
    "ap_page": 0,
    "ap_creator_page": 0,
    "ap_creator_id": None,
}


async def backend_user_id(callback: CallbackQuery) -> int | None:
    if not callback.from_user:
        return None
    return await get_user_backend_id(callback.from_user.id)


async def edit_or_answer(callback: CallbackQuery, text: str, *, reply_markup: InlineKeyboardMarkup | None = None) -> None:
    if not callback.message:
        await callback.answer("Сообщение не найдено", show_alert=True)
        return
    try:
        await callback.message.edit_text(text, reply_markup=reply_markup, parse_mode="Markdown")
    except TelegramBadRequest as exc:
        if "message is not modified" not in str(exc).lower():
            raise
        await callback.answer()


def md(value: Any) -> str:
    return escape_markdown_simple(str(value or ""))


def handle(value: Any) -> str:
    username = str(value or "unknown").strip().lstrip("@")
    return f"@{md(username)}"


def money_fmt(value: Any) -> str:
    try:
        amount = float(value or 0)
    except (TypeError, ValueError):
        amount = 0.0
    if amount == int(amount):
        return f"{int(amount)}₴"
    return f"{amount:.2f}₴"


def duration_fmt(seconds: int | float | None) -> str:
    if seconds is None:
        return "—"
    seconds = max(0, int(seconds))
    minutes, sec = divmod(seconds, 60)
    if minutes < 60:
        return f"{minutes}м {sec:02d}с"
    hours, minutes = divmod(minutes, 60)
    return f"{hours}ч {minutes:02d}м"


def count_fmt(value: Any) -> str:
    return "—" if value is None else str(value)


def video_link(url: Any, label: str = "видео") -> str:
    value = str(url or "").strip()
    if not value:
        return ""
    safe_url = quote(value, safe=":/?&=#%-._~+@,;")
    return f"[{md(label)}]({safe_url})"


def first_video_url(item: dict[str, Any] | None) -> str:
    if not isinstance(item, dict):
        return ""
    if item.get("url"):
        return str(item.get("url") or "")
    urls = item.get("video_urls")
    if isinstance(urls, list) and urls:
        return str(urls[0] or "")
    return ""


def publication_progress(item: dict[str, Any], *, replies: bool = False) -> str:
    prefix = "replies" if replies else "mains"
    published = item.get(f"{prefix}_published")
    expected = item.get(f"{prefix}_expected")
    if published is None:
        return "—"
    base = f"{count_fmt(published)} из {count_fmt(expected)}"
    confirmed = int(item.get(f"{prefix}_confirmed") or 0)
    unconfirmed = int(item.get(f"{prefix}_unconfirmed") or 0)
    if confirmed or unconfirmed:
        parts = []
        if confirmed:
            parts.append(f"{confirmed} ✅")
        if unconfirmed:
            parts.append(f"{unconfirmed} 🟠")
        base += f" ({', '.join(parts)})"
    return base


async def get_report_filters(state: FSMContext) -> dict[str, Any]:
    data = await state.get_data()
    filters = dict(DEFAULT_REPORT_FILTERS)
    filters.update({key: data.get(key, default) for key, default in DEFAULT_REPORT_FILTERS.items()})
    return filters


async def set_report_filters(state: FSMContext, **updates: Any) -> dict[str, Any]:
    await state.set_state(ReportFiltersStates.viewing)
    current = await get_report_filters(state)
    current.update(updates)
    await state.update_data(**current)
    return current


@dataclass
class ConfirmableAction:
    name: str
    build_confirm: Callable[[CallbackQuery, FSMContext], Awaitable[tuple[str, InlineKeyboardMarkup]] | tuple[str, InlineKeyboardMarkup]]
    build_success: Callable[[dict[str, Any]], str]
    do_handler: Callable[[CallbackQuery, FSMContext], Awaitable[dict[str, Any]]]
    return_callback: str

    def register(self, router: Router) -> None:
        @router.callback_query(F.data == f"ap:action:{self.name}:ask")
        async def ask(callback: CallbackQuery, state: FSMContext) -> None:
            built = self.build_confirm(callback, state)
            if inspect.isawaitable(built):
                text, keyboard = await built
            else:
                text, keyboard = built
            await edit_or_answer(callback, text, reply_markup=keyboard)
            await callback.answer()

        @router.callback_query(F.data == f"ap:action:{self.name}:do")
        async def do(callback: CallbackQuery, state: FSMContext) -> None:
            result = await self.do_handler(callback, state)
            await callback.answer("Готово" if result.get("success") else result.get("message", "Не удалось"), show_alert=not result.get("success"))
            text = self.build_success(result)
            await edit_or_answer(callback, text)
