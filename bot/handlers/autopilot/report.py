from __future__ import annotations

import logging
from datetime import datetime

from aiogram import F, Router
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery

from bot.handlers.autopilot.common import (
    backend_user_id,
    duration_fmt,
    edit_or_answer,
    first_video_url,
    get_report_filters,
    handle,
    md,
    money_fmt,
    publication_progress,
    set_report_filters,
    video_link,
)
from bot.keyboards.autopilot.report import get_report_keyboard
from bot.services.api_client import api_client

logger = logging.getLogger("bot.autopilot.report")
router = Router()

STATUS_ICONS = {
    "pending": "⏳",
    "in_progress": "🔄",
    "done": "🟢",
    "partial": "🟡",
    "error": "🔴",
    "stopped": "⏹",
}
PERIOD_LABELS = {"1h": "1ч", "24h": "24ч", "7d": "7д", "30d": "30д", "all": "всё"}


def _time(value: str | None) -> str:
    if not value:
        return "—"
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).strftime("%H:%M")
    except Exception:
        return "—"


def _pct(part: int, total: int) -> int:
    return int(round((part / total) * 100)) if total else 0


def build_summary_text(summary: dict) -> str:
    total = int(summary.get("total") or 0)
    counts = summary.get("status_counts") or {}
    lines = [
        "Всего автозаказов: *{}*".format(total),
        f"✅ Готово: {int(counts.get('done') or 0)} ({_pct(int(counts.get('done') or 0), total)}%)",
        f"🟡 Частично: {int(counts.get('partial') or 0)} ({_pct(int(counts.get('partial') or 0), total)}%)",
        f"🔴 Ошибки: {int(counts.get('error') or 0)} ({_pct(int(counts.get('error') or 0), total)}%)",
        f"⏹ Отменено: {int(counts.get('stopped') or 0)} ({_pct(int(counts.get('stopped') or 0), total)}%)",
        "",
    ]
    mains = summary.get("published_mains")
    replies = summary.get("published_replies")
    if mains is None or replies is None:
        lines.append("Опубликовано: —")
    else:
        lines.append(f"Опубликовано: {mains} главных · {replies} ответов")
    lines.append(
        f"Расход: {money_fmt(summary.get('cost'))} · Возврат: {money_fmt(summary.get('refunded'))} · Чистый: {money_fmt(summary.get('net_cost'))}"
    )
    top_creators = summary.get("top_creators") or []
    if top_creators:
        lines.extend(["", "*Топ-блогеров по объёму*"])
        for index, row in enumerate(top_creators[:3], start=1):
            lines.append(
                f"{index}. {handle(row.get('username'))} · {int(row.get('total') or 0)} "
                f"(✅{int(row.get('done') or 0)} 🟡{int(row.get('partial') or 0)} 🔴{int(row.get('error') or 0)})"
            )
    top_errors = summary.get("top_errors") or []
    if top_errors:
        lines.extend(["", "*Топ ошибок*"])
        for row in top_errors[:3]:
            lines.append(f"• {md(row.get('reason'))}: {int(row.get('count') or 0)}")
    return "\n".join(lines)


def build_tasks_text(tasks: dict) -> str:
    items = tasks.get("items") or []
    total = int(tasks.get("total") or 0)
    page = int(tasks.get("page") or 0)
    pages = int(tasks.get("pages") or 1)
    filters = tasks.get("filters") or {}
    creator_id = filters.get("creator_id")
    lines = [f"Стр. *{page + 1}*/{pages} · {len(items)} из {total}"]
    if creator_id:
        lines.append(f"Фильтр: блогер #{creator_id}")
    lines.append("")
    if not items:
        lines.append("Заказов за выбранный период нет.")
        return "\n".join(lines)
    for item in items:
        status = str(item.get("status") or "")
        icon = STATUS_ICONS.get(status, "•")
        link = video_link(first_video_url(item), "видео →")
        first_line = f"{icon} *#{item.get('task_id')}* · {handle(item.get('creator_username'))} · {_time(item.get('created_at'))}"
        if link:
            first_line += f" · {link}"
        lines.append(first_line)
        mains = publication_progress(item)
        replies = publication_progress(item, replies=True)
        if status == "pending":
            lines.append("   В очереди")
        else:
            marker = " · 🟠 проверить" if item.get("needs_review") else ""
            lines.append(f"   Главных: {mains} · Ответов: {replies} · {money_fmt(item.get('cost'))}{marker}")
            if item.get("error_summary"):
                lines.append(f"   {md(item.get('error_summary'))}")
        lines.append("")
    return "\n".join(lines).rstrip()


def build_creators_text(creators: dict) -> str:
    items = creators.get("items") or []
    page = int(creators.get("page") or 0)
    pages = int(creators.get("pages") or 1)
    lines: list[str] = []
    if not items:
        return "Блогеров с автозаказами за выбранный период нет."
    for row in items:
        active = "✅ active" if row.get("enabled") else "⏸ disabled"
        lines.append(f"{handle(row.get('username'))} [{active}]")
        lines.append(
            f"   {int(row.get('total') or 0)} заказа · ✅{int(row.get('done') or 0)} "
            f"🟡{int(row.get('partial') or 0)} 🔴{int(row.get('error') or 0)}"
        )
        line = f"   Расход: {money_fmt(row.get('cost'))}"
        if float(row.get("refunded") or 0) > 0:
            line += f" (возврат {money_fmt(row.get('refunded'))})"
        lines.append(line)
        lines.append(f"   Последний: {_time(row.get('last_at'))} {STATUS_ICONS.get(str(row.get('last_status') or ''), '')}")
        if int(row.get("fail_streak") or 0) > 0:
            lines.append(f"   ⚠️ серия фейлов: {int(row.get('fail_streak') or 0)}")
        lines.append("")
    lines.append(f"Стр. {page + 1}/{pages}")
    return "\n".join(lines).rstrip()


def build_finance_text(finance: dict, period: str) -> str:
    lines = [
        f"*За {PERIOD_LABELS.get(period, period)}*",
        f"Списано: {money_fmt(finance.get('charged'))}",
        f"Возвращено: {money_fmt(finance.get('refunded'))}",
        f"Чистый расход: {money_fmt(finance.get('cost'))}",
        "",
    ]
    pub = finance.get("publication_count")
    if pub is None:
        lines.append(f"{int(finance.get('task_count') or 0)} заказов · публикации: —")
    else:
        lines.append(f"{int(finance.get('task_count') or 0)} заказов · {pub} публикаций")
        avg = finance.get("avg_publication_cost")
        if avg is not None:
            lines.append(f"Средняя стоимость публикации: {money_fmt(avg)}")
    daily = finance.get("daily") or []
    if daily:
        lines.extend(["", "*По дням*"])
        for row in daily[:7]:
            lines.append(f"{md(row.get('day'))} ↘ {money_fmt(row.get('charged'))} ↗ {money_fmt(row.get('refunded'))} · {int(row.get('publications') or 0)} публ")
    reasons = finance.get("refund_reasons") or {}
    if reasons:
        lines.extend(["", "*Куда уходят возвраты*"])
        for reason, amount in list(reasons.items())[:5]:
            lines.append(f"{md(reason)} — {money_fmt(amount)}")
    return "\n".join(lines)


async def show_report(callback: CallbackQuery, state: FSMContext) -> None:
    user_id = await backend_user_id(callback)
    if not user_id:
        await callback.answer("Пользователь не найден", show_alert=True)
        return
    data = await state.get_data()
    filters = await get_report_filters(state) if data.get("ap_period") else await set_report_filters(state)
    period = str(filters["ap_period"])
    tab = str(filters["ap_view"])
    status = str(filters["ap_status"])
    page = int(filters["ap_page"] or 0)
    creator_page = int(filters.get("ap_creator_page") or 0)
    creator_id = filters.get("ap_creator_id")

    title = "📋 *Журнал автокомментинга*"
    if tab == "list":
        title = f"📋 *Журнал · {PERIOD_LABELS.get(period, period)} · Заказы*"
        payload = await api_client.get_ap_tasks(user_id, period=period, status=status, page=page, creator_id=creator_id)
        body = build_tasks_text(payload)
        keyboard = get_report_keyboard(
            period=period,
            tab=tab,
            status=status,
            page=int(payload.get("page") or 0),
            pages=int(payload.get("pages") or 1),
            creator_filter=creator_id,
            task_rows=payload.get("items") or [],
        )
    elif tab == "creators":
        title = f"📋 *Журнал · {PERIOD_LABELS.get(period, period)} · По блогерам*"
        payload = await api_client.get_ap_creators(user_id, period=period, page=creator_page)
        body = build_creators_text(payload)
        keyboard = get_report_keyboard(
            period=period,
            tab=tab,
            status=status,
            page=int(payload.get("page") or 0),
            pages=int(payload.get("pages") or 1),
            creator_rows=payload.get("items") or [],
        )
    elif tab == "finance":
        title = f"📋 *Журнал · {PERIOD_LABELS.get(period, period)} · Финансы*"
        payload = await api_client.get_ap_finance(user_id, period)
        body = build_finance_text(payload, period)
        keyboard = get_report_keyboard(period=period, tab=tab, status=status, show_archive=False)
    else:
        payload = await api_client.get_ap_summary(user_id, period)
        body = build_summary_text(payload)
        keyboard = get_report_keyboard(period=period, tab="summary", status=status)

    text = "\n".join(
        [
            title,
            "",
            f"Период: *{PERIOD_LABELS.get(period, period)}*",
            f"Вид: *{dict(summary='Сводка', list='Заказы', creators='Блогеры', finance='Финансы').get(tab, tab)}*",
            "",
            "━━━━━━━━━━━━━━━━━━━━",
            "",
            body,
            "",
            "━━━━━━━━━━━━━━━━━━━━",
        ]
    )
    await edit_or_answer(callback, text, reply_markup=keyboard)


@router.callback_query(F.data == "ap:rep")
async def report_screen(callback: CallbackQuery, state: FSMContext):
    await show_report(callback, state)
    await callback.answer()


@router.callback_query(F.data.startswith("ap:rep:tab:"))
async def report_tab(callback: CallbackQuery, state: FSMContext):
    tab = (callback.data or "").split(":")[-1]
    if tab not in {"summary", "list", "creators", "finance"}:
        await callback.answer("Неизвестный раздел", show_alert=True)
        return
    await set_report_filters(state, ap_view=tab)
    await show_report(callback, state)
    await callback.answer()


@router.callback_query(F.data.startswith("ap:rep:period:"))
async def report_period(callback: CallbackQuery, state: FSMContext):
    period = (callback.data or "").split(":")[-1]
    await set_report_filters(state, ap_period=period, ap_page=0, ap_creator_page=0)
    await show_report(callback, state)
    await callback.answer()


@router.callback_query(F.data.startswith("ap:rep:list:f:"))
async def report_status(callback: CallbackQuery, state: FSMContext):
    status = (callback.data or "").split(":")[-1]
    await set_report_filters(state, ap_view="list", ap_status=status, ap_page=0)
    await show_report(callback, state)
    await callback.answer()


@router.callback_query(F.data.startswith("ap:rep:list:p:"))
async def report_page(callback: CallbackQuery, state: FSMContext):
    try:
        page = int((callback.data or "").split(":")[-1])
    except ValueError:
        page = 0
    filters = await get_report_filters(state)
    if filters.get("ap_view") == "creators":
        await set_report_filters(state, ap_creator_page=page)
    else:
        await set_report_filters(state, ap_view="list", ap_page=page)
    await show_report(callback, state)
    await callback.answer()


@router.callback_query(F.data.startswith("ap:rep:cre:p:"))
async def report_creator_page(callback: CallbackQuery, state: FSMContext):
    try:
        page = int((callback.data or "").split(":")[-1])
    except ValueError:
        page = 0
    await set_report_filters(state, ap_view="creators", ap_creator_page=page)
    await show_report(callback, state)
    await callback.answer()


@router.callback_query(F.data.startswith("ap:rep:creator:"))
async def report_creator(callback: CallbackQuery, state: FSMContext):
    try:
        creator_id = int((callback.data or "").split(":")[-1])
    except ValueError:
        await callback.answer("Блогер не найден", show_alert=True)
        return
    if creator_id <= 0:
        await set_report_filters(state, ap_view="list", ap_creator_id=None, ap_page=0)
        await show_report(callback, state)
        await callback.answer()
        return
    await set_report_filters(state, ap_view="list", ap_creator_id=creator_id, ap_page=0)
    await show_report(callback, state)
    await callback.answer()


@router.callback_query(F.data == "ap:rep:noop")
async def report_noop(callback: CallbackQuery):
    await callback.answer()


@router.callback_query(F.data == "ap:rep:open5")
async def report_open_latest_videos(callback: CallbackQuery, state: FSMContext):
    try:
        user_id = await backend_user_id(callback)
        if not user_id:
            await callback.answer("Пользователь не найден", show_alert=True)
            return
        filters = await get_report_filters(state)
        period = str(filters["ap_period"])
        status = str(filters["ap_status"])
        page = int(filters.get("ap_page") or 0)
        creator_id = filters.get("ap_creator_id")
        payload = await api_client.get_ap_tasks(user_id, period=period, status=status, page=page, limit=5, creator_id=creator_id)
        urls = [first_video_url(item) for item in (payload.get("items") or [])]
        urls = [url for url in urls if url]
        if not urls or not callback.message:
            await callback.answer("Видимых ссылок на этой странице нет", show_alert=True)
            return
        for url in urls[:5]:
            await callback.message.answer(url, disable_web_page_preview=False)
        await callback.answer(f"Открыл ссылок: {len(urls[:5])}")
    except Exception as exc:
        logger.error("Error opening autopilot video links: %s", exc, exc_info=True)
        await callback.answer("Не удалось открыть ссылки", show_alert=True)
