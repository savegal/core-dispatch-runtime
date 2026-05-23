from __future__ import annotations

import logging

from aiogram import F, Router
from aiogram.types import CallbackQuery

from bot.handlers.autopilot.common import (
    backend_user_id,
    edit_or_answer,
    first_video_url,
    handle,
    md,
    money_fmt,
    publication_progress,
    video_link,
)
from bot.keyboards.autopilot.main import get_main_keyboard, get_notify_admin_confirm_keyboard
from bot.services.api_client import api_client

logger = logging.getLogger("bot.autopilot.main")
router = Router()


def _orders_line(summary: dict) -> str:
    counts = summary.get("status_counts") or {}
    return (
        f"Заказов: *{int(summary.get('total') or 0)}* "
        f"(✅{int(counts.get('done') or 0)} 🟡{int(counts.get('partial') or 0)} "
        f"🔴{int(counts.get('error') or 0)} ⏹{int(counts.get('stopped') or 0)})"
    )


def _status_icon(status: str) -> str:
    return {"done": "✅", "partial": "🟡", "error": "🔴", "stopped": "⏹"}.get(str(status or ""), "•")


def build_main_text(snapshot: dict) -> str:
    control = snapshot.get("control") or {}
    current = snapshot.get("current_work") or {}
    summary = snapshot.get("summary") or {}
    incidents = snapshot.get("incidents") or []
    mode = str(control.get("mode") or "active")
    mode_label = {"active": "▶️ Активен", "paused": "⏸ Пауза", "drain": "🚰 Дренаж"}.get(mode, mode)
    state = str(snapshot.get("state") or "ok")

    lines = ["🤖 *Автокомментинг*", ""]
    if state == "down":
        state_label = md(snapshot.get("state_label") or "Софт не работает")
        lines.extend(
            [
                f"🔴 *{state_label}*",
                "Один из сервисов автокомментинга не отвечает > 2 мин.",
                "Проверьте executor/monitor и Redis.",
                "",
                f"В очереди: *{int(snapshot.get('queue_count') or 0)}* заказов (ждут запуска)",
            ]
        )
        return "\n".join(lines)

    if incidents:
        incident = incidents[0]
        title = md(incident.get("title") or "инцидент")
        cause = md(incident.get("cause") or "Софт заметил проблему и держит защитный режим.")
        auto_action = md(incident.get("auto_action") or "Софт сам попробует восстановиться.")
        admin_action = md(incident.get("admin_action") or "Если повторится — обратитесь к админу.")
        lines.extend(
            [
                f"⚠️ *Проблема: {title}*",
                cause,
                auto_action,
                "",
                f"Что делать: {admin_action}",
                "",
            ]
        )
    elif mode == "paused":
        lines.extend(
            [
                "⏸ *На паузе*",
                "Новые автозаказы не создаются.",
                "Уже созданные в очереди — продолжат выполняться.",
                "",
            ]
        )
    elif mode == "drain":
        lines.extend(
            [
                "🚰 *Дренаж*",
                "Новые автозаказы не создаются.",
                "Очередь спокойно дорабатывает уже созданные заказы.",
                "",
            ]
        )
    elif state == "warning":
        lines.extend(
            [
                f"⚠️ *{md(snapshot.get('state_label') or 'Есть проблема')}*",
                "Автокомментинг частично работает, но один из служебных компонентов не отвечает.",
                "Если очередь не двигается — откройте «Работа» или уведомите админа.",
                "",
            ]
        )

    lines.append(f"{snapshot.get('state_icon', '✅')} {md(snapshot.get('state_label') or 'Работает')} · Режим: {mode_label}")
    lines.append("")
    lines.append(f"В очереди: *{int(snapshot.get('queue_count') or 0)}* заказов")
    outbox_items = current.get("outbox_active") or []
    if outbox_items:
        outbox_videos = sum(int(item.get("video_count") or 0) for item in outbox_items)
        suffix = f" · {outbox_videos} видео" if outbox_videos else ""
        lines.append(f"Ждут создания заказа: *{len(outbox_items)}* событий{suffix}")
    active = current.get("active_task") or {}
    if active:
        progress = publication_progress(active)
        active_line = f"Сейчас выполняется: `#{active.get('task_id')}` {handle(active.get('creator_username'))}"
        if progress != "—":
            active_line += f" · главных {progress}"
        lines.append(active_line)
        link = video_link(first_video_url(active), "открыть видео")
        if link:
            lines.append(f"   🔗 {link}")
    recent_tasks = snapshot.get("recent_tasks") or []
    if recent_tasks:
        lines.extend(["", "🔗 *Последние 3 заказа*"])
        for item in recent_tasks[:3]:
            progress = publication_progress(item)
            suffix = " · стоит проверить" if item.get("needs_review") else ""
            link = video_link(first_video_url(item), "видео →")
            line = (
                f"{_status_icon(str(item.get('status') or ''))} `#{item.get('task_id')}` "
                f"{handle(item.get('creator_username'))} · главных {progress}{suffix}"
            )
            if link:
                line += f" · {link}"
            lines.append(line)
    lines.extend(["", "📊 *За 24 часа*", _orders_line(summary)])
    mains = summary.get("published_mains")
    replies = summary.get("published_replies")
    if mains is None or replies is None:
        lines.append("Опубликовано: —")
    else:
        lines.append(f"Опубликовано: *{mains}* главных + *{replies}* ответов")
    lines.append(f"Расход: *{money_fmt(summary.get('net_cost'))}*")
    return "\n".join(lines)


async def show_ap_main(callback: CallbackQuery) -> None:
    user_id = await backend_user_id(callback)
    if not user_id:
        await callback.answer("Пользователь не найден", show_alert=True)
        return
    snapshot = await api_client.get_ap_dashboard(user_id)
    state = str(snapshot.get("state") or "ok")
    control = snapshot.get("control") or {}
    text = build_main_text(snapshot)
    keyboard = get_main_keyboard(
        mode=str(control.get("mode") or "active"),
        state=state,
        has_incident=bool(snapshot.get("incidents")),
        notify_cooldown_minutes=int(snapshot.get("notify_admin_cooldown_minutes") or 0),
        can_skip_queue=(
            int(snapshot.get("queue_count") or 0)
            + len((snapshot.get("current_work") or {}).get("outbox_active") or [])
        ) > 0,
    )
    await edit_or_answer(callback, text, reply_markup=keyboard)


@router.callback_query(F.data.in_({"ap:main", "ap:main:refresh"}))
async def show_main(callback: CallbackQuery):
    try:
        await show_ap_main(callback)
        await callback.answer()
    except Exception as exc:
        logger.error("Error showing autopilot main: %s", exc, exc_info=True)
        await callback.answer("Не удалось открыть автокомментинг", show_alert=True)


@router.callback_query(F.data == "ap:main:notify_admin")
async def ask_notify_admin(callback: CallbackQuery):
    text = (
        "📞 *Уведомить админа?*\n\n"
        "Сейчас всем админам уйдёт сообщение с состоянием автокомментинга. Подтвердить?"
    )
    await edit_or_answer(callback, text, reply_markup=get_notify_admin_confirm_keyboard())
    await callback.answer()
