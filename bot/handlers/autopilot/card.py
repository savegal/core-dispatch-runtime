from __future__ import annotations

import logging

from aiogram import F, Router
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup

from bot.handlers.autopilot.common import (
    backend_user_id,
    duration_fmt,
    edit_or_answer,
    handle,
    md,
    money_fmt,
    publication_progress,
)
from bot.keyboards.autopilot.card import get_card_keyboard, get_hide_confirm_keyboard, get_retry_confirm_keyboard
from bot.services.api_client import api_client

logger = logging.getLogger("bot.autopilot.card")
router = Router()

STATUS_ICONS = {"done": "✅", "partial": "🟡", "error": "🔴", "stopped": "⏹", "pending": "⏳", "in_progress": "🔄"}


def _back_to_card_keyboard(task_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[[InlineKeyboardButton(text="◀️ Назад", callback_data=f"ap:card:{task_id}")]]
    )


def _publication_line(index: int, item: dict) -> str:
    icon = "🟠" if item.get("unconfirmed") else "✅"
    account = handle(item.get("account_username")) if item.get("account_username") else "аккаунт"
    comment_id = item.get("comment_id")
    if comment_id:
        tail = f"ID `{md(comment_id)}`"
    elif item.get("unconfirmed"):
        tail = "отправлено, не подтверждено"
    else:
        tail = "без ID"
    return f"  {icon} {index}. {account} · {tail}"


def build_card_text(card: dict) -> str:
    status = str(card.get("status") or "")
    icon = STATUS_ICONS.get(status, "•")
    label = md(card.get("status_label") or status)
    created = str(card.get("created_at") or "")[:16].replace("T", " ")
    duration = duration_fmt(card.get("duration_seconds"))
    url = str(card.get("url") or "")
    video_line = f"Видео: {handle(card.get('creator_username'))}"
    if url:
        video_line += f" · [открыть]({url})"
    lines = [
        f"📄 *Заказ #{card.get('task_id')}*",
        "",
        f"{icon} {label} · {md(created)} ({duration})",
        video_line,
    ]
    if card.get("template_name"):
        lines.append(f"Шаблон: {md(card.get('template_name'))}")
    if card.get("origin_label"):
        lines.append(md(card.get("origin_label")))
    lines.extend(
        [
            "",
            "*Что опубликовано*",
            f"Главных: {publication_progress(card)}",
            f"Ответов: {publication_progress(card, replies=True)}",
        ]
    )
    publications = card.get("publications") or {}
    mains = publications.get("mains") or []
    replies = publications.get("replies") or []
    if mains:
        lines.extend(["", f"*Главные ({publication_progress(card)})*"])
        for index, item in enumerate(mains[:5], start=1):
            lines.append(_publication_line(index, item))
        if len(mains) > 5:
            lines.append(f"  ... ещё {len(mains) - 5}")
    if replies:
        lines.extend(["", f"*Ответы ({publication_progress(card, replies=True)})*"])
        for index, item in enumerate(replies[:5], start=1):
            lines.append(_publication_line(index, item))
        if len(replies) > 5:
            lines.append(f"  ... ещё {len(replies) - 5}")
    if status in {"partial", "error"} and card.get("error_summary"):
        lines.extend(["", "*Что случилось*", md(card.get("error_summary"))])
    lines.extend(["", "*Деньги*", f"Списано: {money_fmt(card.get('cost'))}"])
    if float(card.get("refunded") or 0) > 0:
        lines.append(f"Возврат: {money_fmt(card.get('refunded'))}")
    if float(card.get("net_cost") or 0) != float(card.get("cost") or 0):
        lines.append(f"Чистый: {money_fmt(card.get('net_cost'))}")
    return "\n".join(lines)


async def show_ap_card(callback: CallbackQuery, task_id: int) -> None:
    user_id = await backend_user_id(callback)
    if not user_id:
        await callback.answer("Пользователь не найден", show_alert=True)
        return
    card = await api_client.get_ap_task(user_id, task_id)
    retry_cost = float(card.get("cost") or 0)
    if str(card.get("status") or "") in {"error", "partial"}:
        try:
            preview = await api_client.ap_retry_preview(user_id, task_id)
            details = preview.get("details") or {}
            retry_cost = float(details.get("cost") or details.get("required") or retry_cost)
        except Exception as exc:
            logger.warning("Could not load retry preview for task %s: %s", task_id, exc)
    await edit_or_answer(
        callback,
        build_card_text(card),
        reply_markup=get_card_keyboard(
            int(card.get("task_id") or task_id),
            status=str(card.get("status") or ""),
            cost=retry_cost,
            creator_id=card.get("creator_id"),
        ),
    )


@router.callback_query(F.data.regexp(r"^ap:card:\d+$"))
async def card_screen(callback: CallbackQuery):
    task_id = int((callback.data or "").split(":")[-1])
    await show_ap_card(callback, task_id)
    await callback.answer()


@router.callback_query(F.data.regexp(r"^(ap:card:\d+:retry|ap:c:\d+:r)$"))
async def ask_retry(callback: CallbackQuery):
    parts = (callback.data or "").split(":")
    task_id = int(parts[2])
    user_id = await backend_user_id(callback)
    if not user_id:
        await callback.answer("Пользователь не найден", show_alert=True)
        return
    preview = await api_client.ap_retry_preview(user_id, task_id)
    details = preview.get("details") or {}
    cost = float(details.get("cost") or details.get("required") or 0)
    if not preview.get("success"):
        if preview.get("code") == "insufficient_funds":
            text = (
                "🔁 *Создать заказ заново*\n\n"
                "Недостаточно средств на балансе.\n"
                f"Текущий баланс: *{money_fmt(details.get('balance'))}*, нужно *{money_fmt(details.get('required'))}*."
            )
        else:
            text = md(preview.get("message") or "Повтор сейчас недоступен.")
        await edit_or_answer(callback, text, reply_markup=_back_to_card_keyboard(task_id))
        await callback.answer()
        return
    text = (
        f"🔁 *Создать заказ #{task_id} заново?*\n\n"
        "Будет создан новый заказ-копия на это же видео.\n"
        f"Спишется *{money_fmt(cost)}*."
    )
    await edit_or_answer(callback, text, reply_markup=get_retry_confirm_keyboard(task_id))
    await callback.answer()


@router.callback_query(F.data.regexp(r"^(ap:card:\d+:hide|ap:c:\d+:h)$"))
async def ask_hide(callback: CallbackQuery):
    task_id = int((callback.data or "").split(":")[2])
    text = f"🙈 *Скрыть заказ #{task_id} из отчёта?*\n\nЗапись уйдёт в архив и не будет показываться в журнале."
    await edit_or_answer(callback, text, reply_markup=get_hide_confirm_keyboard(task_id))
    await callback.answer()


@router.callback_query(F.data.regexp(r"^(ap:card:\d+:open_creator|ap:c:\d+:oc)$"))
async def open_creator(callback: CallbackQuery):
    task_id = int((callback.data or "").split(":")[2])
    user_id = await backend_user_id(callback)
    if not user_id:
        await callback.answer("Пользователь не найден", show_alert=True)
        return
    card = await api_client.get_ap_task(user_id, task_id)
    creator_id = card.get("creator_id")
    if not creator_id:
        await callback.answer("Блогер не найден", show_alert=True)
        return
    # Existing video parsing router owns this screen.
    from bot.handlers.video_parsing import show_creator_detail

    await show_creator_detail(callback, int(creator_id))
