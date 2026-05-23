from __future__ import annotations

import logging
from datetime import datetime, timezone

from aiogram import F, Router
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery

from bot.handlers.autopilot.common import (
    backend_user_id,
    duration_fmt,
    edit_or_answer,
    first_video_url,
    handle,
    md,
    money_fmt,
    publication_progress,
    video_link,
)
from bot.keyboards.autopilot.current import (
    get_cancel_all_confirm_keyboard,
    get_current_keyboard,
    get_skip_queue_confirm_keyboard,
    get_stop_active_confirm_keyboard,
    get_work_cancel_task_confirm_keyboard,
    get_work_cancel_video_confirm_keyboard,
    get_work_keyboard,
    get_work_stop_task_confirm_keyboard,
)
from bot.services.api_client import api_client

logger = logging.getLogger("bot.autopilot.current")
router = Router()

STATUS_ICONS = {
    "pending": "⏳",
    "in_progress": "🔄",
    "done": "✅",
    "partial": "🟡",
    "error": "🔴",
    "stopped": "⏹",
}


def _elapsed(created_at: str | None) -> str:
    if not created_at:
        return "—"
    try:
        value = datetime.fromisoformat(created_at.replace("Z", "+00:00"))
        if value.tzinfo is None:
            value = value.replace(tzinfo=timezone.utc)
        return duration_fmt((datetime.now(timezone.utc) - value).total_seconds())
    except Exception:
        return "—"


def _short_time(value: str | None) -> str:
    if not value:
        return "—"
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).strftime("%d.%m %H:%M")
    except Exception:
        return "—"


def _work_title(tab: str) -> str:
    return {
        "plan": "🗓 *Работа · Планируется*",
        "run": "▶️ *Работа · В работе*",
        "done": "✅ *Работа · Обработано*",
    }.get(tab, "🧰 *Работа автокомментинга*")


def _video_links(urls: list[str] | None, *, limit: int = 2) -> str:
    result = []
    for index, url in enumerate((urls or [])[:limit], start=1):
        link = video_link(url, "открыть →" if limit == 1 else f"{index} →")
        if link:
            result.append(link)
    return " ".join(result)


def build_work_text(payload: dict, tab: str = "plan") -> str:
    items = payload.get("items") or []
    total = int(payload.get("total") or 0)
    page = int(payload.get("page") or 0)
    pages = int(payload.get("pages") or 1)
    lines = [_work_title(tab), ""]

    if tab == "plan":
        lines.append(f"Ближайшие видео: *{total}* · стр. *{page + 1}*/{pages}")
        lines.append("Кнопки `✖ 1`, `✖ 2` убирают конкретное видео из автокомментинга.")
        lines.append("")
        if not items:
            lines.append("План пуст: сейчас нет видео, которые ждут автокомментинга.")
            return "\n".join(lines)
        for index, item in enumerate(items, start=1):
            link = video_link(item.get("url"), "открыть →")
            task = f" · заказ `#{item.get('task_id')}`" if item.get("task_id") else ""
            label = md(item.get("label") or "Ждёт")
            line = f"*{index}.* {handle(item.get('creator_username'))}{task}"
            if link:
                line += f" · {link}"
            lines.append(line)
            lines.append(f"   {label} · статус: `{md(item.get('status') or 'new')}`")
            if item.get("discovered_at"):
                lines.append(f"   Найдено: `{_short_time(item.get('discovered_at'))}`")
            lines.append("")
        return "\n".join(lines).rstrip()

    if tab == "run":
        lines.append(f"Активных заказов: *{total}*")
        lines.append("")
        if not items:
            lines.append("Сейчас ничего не выполняется. Если есть план, executor возьмёт следующий заказ из очереди.")
            return "\n".join(lines)
        for item in items:
            link = video_link(first_video_url(item), "открыть →")
            task_id = item.get("task_id")
            lines.append(f"🔄 *Заказ #{task_id}* · {handle(item.get('creator_username'))}")
            if link:
                lines.append(f"Видео: {link}")
            if item.get("template_name"):
                lines.append(f"Шаблон: {md(item.get('template_name'))}")
            lines.append(f"Этап: *{md(item.get('stage') or 'В работе')}*")
            lines.append(f"Главных: {publication_progress(item)}")
            lines.append(f"Ответов: {publication_progress(item, replies=True)}")
            if item.get("video_count"):
                lines.append(f"Видео в заказе: *{int(item.get('video_count') or 0)}* · прогресс executor: *{int(item.get('progress_percent') or 0)}*")
            lines.append(f"Идёт: {_elapsed(item.get('updated_at') or item.get('created_at'))}")
            lines.append("")
        return "\n".join(lines).rstrip()

    lines.append(f"Всего обработано: *{total}* · стр. *{page + 1}*/{pages}")
    lines.append("")
    if not items:
        lines.append("Обработанных автозаказов пока нет.")
        return "\n".join(lines)
    for index, item in enumerate(items, start=1):
        status = str(item.get("status") or "")
        icon = STATUS_ICONS.get(status, "•")
        link = video_link(first_video_url(item), "открыть →")
        line = f"{index}. {icon} `#{item.get('task_id')}` · {handle(item.get('creator_username'))} · {_short_time(item.get('updated_at') or item.get('created_at'))}"
        if link:
            line += f" · {link}"
        lines.append(line)
        lines.append(f"   Главных: {publication_progress(item)} · Ответов: {publication_progress(item, replies=True)} · {money_fmt(item.get('net_cost', item.get('cost')))}")
        if item.get("error_summary"):
            lines.append(f"   {md(item.get('error_summary'))}")
        if item.get("needs_review"):
            lines.append("   🟠 Требует ручной проверки")
        lines.append("")
    return "\n".join(lines).rstrip()


def build_current_text(snapshot: dict) -> str:
    lines = ["▶️ *В работе сейчас*", ""]
    active = snapshot.get("active_task") or {}
    processing = snapshot.get("processing_tasks") or []
    queue = snapshot.get("queue_head") or []
    outbox = snapshot.get("outbox_active") or []
    active_task_id = active.get("task_id")
    processing = [task for task in processing if task.get("task_id") != active_task_id]
    if not active and not processing and not queue and not outbox:
        lines.append("Сейчас тихо — нет активных заказов и очереди.")
        return "\n".join(lines)

    if active:
        lines.extend(
            [
                "*Активный заказ*",
                f"`#{active.get('task_id')}` · {handle(active.get('creator_username'))}",
            ]
        )
        if active.get("template_name"):
            lines.append(f"Шаблон: {md(active.get('template_name'))}")
        link = video_link(first_video_url(active), "видео")
        if link:
            lines.append(f"Видео: {link}")
        main_progress = publication_progress(active)
        if main_progress != "—":
            lines.append(f"Главных: {main_progress}")
        reply_progress = publication_progress(active, replies=True)
        if reply_progress != "—":
            lines.append(f"Ответов: {reply_progress}")
        lines.append(f"Идёт: {_elapsed(active.get('updated_at') or active.get('created_at'))}")
        lines.append("")

    if processing:
        total_processing = int(snapshot.get("processing_count") or len(processing))
        lines.append(f"*В работе без активного слота ({total_processing})*")
        for index, task in enumerate(processing[:5], start=1):
            link = video_link(first_video_url(task), "видео")
            progress = publication_progress(task)
            line = (
                f"{index}. `#{task.get('task_id')}` · {handle(task.get('creator_username'))} · "
                f"главных {progress} · идёт {_elapsed(task.get('updated_at') or task.get('created_at'))}"
            )
            if link:
                line += f" · {link}"
            lines.append(line)
        lines.append("")

    lines.append(f"*В очереди ({int(snapshot.get('pending_count') or len(queue))})*")
    if queue:
        for index, task in enumerate(queue[:5], start=1):
            urls = [str(url or "") for url in (task.get("video_urls") or []) if str(url or "").strip()]
            links = " ".join(video_link(url, str(pos)) for pos, url in enumerate(urls[:2], start=1))
            line = f"{index}. `#{task.get('task_id')}` · {handle(task.get('creator_username'))}"
            if links:
                line += f" · {links}"
            lines.append(line)
    else:
        lines.append("Очередь пуста")
    lines.append("")

    lines.append("*Скоро запустится*")
    if outbox:
        for item in outbox[:5]:
            count = int(item.get("video_count") or 0)
            plural = "новое видео" if count == 1 else "новых видео"
            urls = [str(url or "") for url in (item.get("video_urls") or []) if str(url or "").strip()]
            links = " ".join(video_link(url, str(pos)) for pos, url in enumerate(urls[:2], start=1))
            line = f"{handle(item.get('creator_username'))} — {count} {plural}"
            if links:
                line += f" · {links}"
            lines.append(line)
    else:
        lines.append("Пусто")
    return "\n".join(lines)


async def show_ap_current(callback: CallbackQuery) -> None:
    user_id = await backend_user_id(callback)
    if not user_id:
        await callback.answer("Пользователь не найден", show_alert=True)
        return
    snapshot = await api_client.get_ap_current(user_id)
    await edit_or_answer(
        callback,
        build_current_text(snapshot),
        reply_markup=get_current_keyboard(
            has_active=bool(snapshot.get("active_task")),
            queue_count=int(snapshot.get("pending_count") or 0) + len(snapshot.get("outbox_active") or []),
        ),
    )


async def show_ap_work(callback: CallbackQuery, *, tab: str = "plan", page: int = 0) -> None:
    user_id = await backend_user_id(callback)
    if not user_id:
        await callback.answer("Пользователь не найден", show_alert=True)
        return
    normalized = tab if tab in {"plan", "run", "done"} else "plan"
    payload = await api_client.get_ap_work(user_id, tab=normalized, page=max(0, int(page or 0)), limit=10)
    await edit_or_answer(
        callback,
        build_work_text(payload, normalized),
        reply_markup=get_work_keyboard(
            tab=normalized,
            page=int(payload.get("page") or 0),
            pages=int(payload.get("pages") or 1),
            items=payload.get("items") or [],
        ),
    )


@router.callback_query(F.data == "ap:current")
async def current_screen(callback: CallbackQuery):
    try:
        await show_ap_work(callback, tab="run", page=0)
        await callback.answer()
    except Exception as exc:
        logger.error("Error showing autopilot current: %s", exc, exc_info=True)
        await callback.answer("Не удалось открыть экран", show_alert=True)


@router.callback_query(F.data.in_({"ap:w", "ap:work"}))
async def work_screen(callback: CallbackQuery):
    try:
        await show_ap_work(callback, tab="plan", page=0)
        await callback.answer()
    except Exception as exc:
        logger.error("Error showing autopilot work: %s", exc, exc_info=True)
        await callback.answer("Не удалось открыть работу", show_alert=True)


@router.callback_query(F.data.startswith("ap:w:t:"))
async def work_tab(callback: CallbackQuery):
    tab = (callback.data or "").split(":")[-1]
    try:
        await show_ap_work(callback, tab=tab, page=0)
        await callback.answer()
    except Exception as exc:
        logger.error("Error switching autopilot work tab: %s", exc, exc_info=True)
        await callback.answer("Не удалось открыть вкладку", show_alert=True)


@router.callback_query(F.data.startswith("ap:w:p:"))
async def work_page(callback: CallbackQuery):
    parts = (callback.data or "").split(":")
    try:
        tab = parts[-2]
        page = int(parts[-1])
    except (ValueError, IndexError):
        tab, page = "plan", 0
    try:
        await show_ap_work(callback, tab=tab, page=page)
        await callback.answer()
    except Exception as exc:
        logger.error("Error paginating autopilot work: %s", exc, exc_info=True)
        await callback.answer("Не удалось открыть страницу", show_alert=True)


@router.callback_query(F.data == "ap:w:no")
async def work_noop(callback: CallbackQuery):
    await callback.answer()


@router.callback_query(F.data.regexp(r"^ap:w:cv:\d+$"))
async def ask_cancel_planned_video(callback: CallbackQuery):
    try:
        video_id = int((callback.data or "").split(":")[-1])
        user_id = await backend_user_id(callback)
        preview = await api_client.get_ap_planned_video(user_id, video_id) if user_id else {}
        target_lines = []
        if preview:
            link = video_link(preview.get("url"), "открыть видео →")
            target = f"{handle(preview.get('creator_username'))}"
            if preview.get("task_id"):
                target += f" · заказ `#{preview.get('task_id')}`"
            if link:
                target += f" · {link}"
            target_lines.append(target)
            if preview.get("template_name"):
                target_lines.append(f"Шаблон: {md(preview.get('template_name'))}")
            if preview.get("refund_preview"):
                target_lines.append(f"Возврат на баланс: ~*{money_fmt(preview.get('refund_preview'))}*")
            elif not preview.get("task_id"):
                target_lines.append("Заказ ещё не создан, денег списывать не нужно.")
        text = (
            "✖ *Убрать видео из плана?*\n\n"
            + ("\n".join(target_lines) + "\n\n" if target_lines else "")
            + "Видео будет помечено как пропущенное и больше не запустится на автокомментинг.\n"
            + "Если под него уже создан ожидающий заказ, заказ остановится, а неиспользованная сумма вернётся на баланс."
        )
        await edit_or_answer(callback, text, reply_markup=get_work_cancel_video_confirm_keyboard(video_id))
        await callback.answer()
    except Exception as exc:
        logger.error("Error asking planned video cancel: %s", exc, exc_info=True)
        await callback.answer("Не удалось открыть подтверждение", show_alert=True)


@router.callback_query(F.data.regexp(r"^ap:w:ct:\d+$"))
async def ask_cancel_planned_task(callback: CallbackQuery):
    try:
        task_id = int((callback.data or "").split(":")[-1])
        user_id = await backend_user_id(callback)
        preview = await api_client.get_ap_planned_task(user_id, task_id) if user_id else {}
        target_lines = []
        if preview:
            target_lines.append(f"{handle(preview.get('creator_username'))} · заказ `#{task_id}`")
            links = _video_links([str(url) for url in (preview.get("video_urls") or []) if str(url).strip()])
            if links:
                target_lines.append(f"Видео: {links}")
            if preview.get("template_name"):
                target_lines.append(f"Шаблон: {md(preview.get('template_name'))}")
            target_lines.append(f"Возврат на баланс: ~*{money_fmt(preview.get('refund_preview'))}*")
        text = (
            f"✖ *Убрать заказ #{task_id} из плана?*\n\n"
            + ("\n".join(target_lines) + "\n\n" if target_lines else "")
            + "Ожидающий заказ будет остановлен, связанные видео будут помечены как пропущенные, "
            + "неиспользованная сумма вернётся на баланс."
        )
        await edit_or_answer(callback, text, reply_markup=get_work_cancel_task_confirm_keyboard(task_id))
        await callback.answer()
    except Exception as exc:
        logger.error("Error asking planned task cancel: %s", exc, exc_info=True)
        await callback.answer("Не удалось открыть подтверждение", show_alert=True)


@router.callback_query(F.data.regexp(r"^ap:w:st:\d+$"))
async def ask_stop_work_task(callback: CallbackQuery):
    try:
        task_id = int((callback.data or "").split(":")[-1])
        user_id = await backend_user_id(callback)
        card = await api_client.get_ap_task(user_id, task_id) if user_id else {}
        target_lines = []
        if card:
            link = video_link(first_video_url(card), "открыть видео →")
            target = f"{handle(card.get('creator_username'))} · заказ `#{task_id}`"
            if link:
                target += f" · {link}"
            target_lines.append(target)
            target_lines.append(f"Главных: {publication_progress(card)} · Ответов: {publication_progress(card, replies=True)}")
        text = (
            f"⏹ *Остановить заказ #{task_id}?*\n\n"
            + ("\n".join(target_lines) + "\n\n" if target_lines else "")
            + "Executor получит стоп-сигнал и перейдёт к следующему заказу в очереди. "
            + "Уже опубликованные комментарии не удаляются."
        )
        await edit_or_answer(callback, text, reply_markup=get_work_stop_task_confirm_keyboard(task_id))
        await callback.answer()
    except Exception as exc:
        logger.error("Error asking work task stop: %s", exc, exc_info=True)
        await callback.answer("Не удалось открыть подтверждение", show_alert=True)


@router.callback_query(F.data.regexp(r"^ap:w:cvd:\d+$"))
async def do_cancel_planned_video(callback: CallbackQuery):
    try:
        user_id = await backend_user_id(callback)
        if not user_id:
            await callback.answer("Пользователь не найден", show_alert=True)
            return
        video_id = int((callback.data or "").split(":")[-1])
        result = await api_client.ap_action(user_id, "cancel-planned-video", video_id=video_id)
        if not result.get("success"):
            await callback.answer(result.get("message") or "Не удалось убрать видео", show_alert=True)
            return
        await show_ap_work(callback, tab="plan", page=0)
        await callback.answer("Видео убрано")
    except Exception as exc:
        logger.error("Error cancelling planned video: %s", exc, exc_info=True)
        await callback.answer("Не удалось убрать видео", show_alert=True)


@router.callback_query(F.data.regexp(r"^ap:w:ctd:\d+$"))
async def do_cancel_planned_task(callback: CallbackQuery):
    try:
        user_id = await backend_user_id(callback)
        if not user_id:
            await callback.answer("Пользователь не найден", show_alert=True)
            return
        task_id = int((callback.data or "").split(":")[-1])
        result = await api_client.ap_action(user_id, "cancel-planned-task", task_id=task_id)
        if not result.get("success"):
            await callback.answer(result.get("message") or "Не удалось убрать заказ", show_alert=True)
            return
        await show_ap_work(callback, tab="plan", page=0)
        await callback.answer("Заказ убран")
    except Exception as exc:
        logger.error("Error cancelling planned task: %s", exc, exc_info=True)
        await callback.answer("Не удалось убрать заказ", show_alert=True)


@router.callback_query(F.data.regexp(r"^ap:w:std:\d+$"))
async def do_stop_work_task(callback: CallbackQuery):
    try:
        user_id = await backend_user_id(callback)
        if not user_id:
            await callback.answer("Пользователь не найден", show_alert=True)
            return
        task_id = int((callback.data or "").split(":")[-1])
        result = await api_client.ap_action(user_id, "stop-task", task_id=task_id)
        if not result.get("success"):
            await callback.answer(result.get("message") or "Не удалось остановить заказ", show_alert=True)
            return
        await show_ap_work(callback, tab="run", page=0)
        await callback.answer("Заказ остановлен")
    except Exception as exc:
        logger.error("Error stopping work task: %s", exc, exc_info=True)
        await callback.answer("Не удалось остановить заказ", show_alert=True)


@router.callback_query(F.data == "ap:current:stop_active")
async def ask_stop_active(callback: CallbackQuery):
    text = (
        "⏹ *Остановить активный заказ?*\n\n"
        "Активный заказ будет помечен как остановленный. Опубликованные комментарии не удаляются, "
        "оплата за публикации остаётся. Подтвердить?"
    )
    await edit_or_answer(callback, text, reply_markup=get_stop_active_confirm_keyboard())
    await callback.answer()


@router.callback_query(F.data == "ap:current:cancel_all")
async def ask_cancel_all(callback: CallbackQuery, state: FSMContext):
    user_id = await backend_user_id(callback)
    if not user_id:
        await callback.answer("Пользователь не найден", show_alert=True)
        return
    preview = await api_client.ap_cancel_all_preview(user_id)
    text = (
        "🧹 *Отменить всю очередь?*\n\n"
        f"Будут отменены *{int(preview.get('pending_count') or 0)}* ожидающих заказов.\n"
        f"Видео вернутся в новые, ~*{money_fmt(preview.get('refund'))}* вернётся на баланс.\n"
    )
    if preview.get("active_task_id"):
        text += f"Активный заказ `#{preview.get('active_task_id')}` не трогаем.\n\n"
    text += "Режимы блогеров не меняются."
    await edit_or_answer(callback, text, reply_markup=get_cancel_all_confirm_keyboard())
    await callback.answer()


@router.callback_query(F.data.in_({"ap:current:skipq", "ap:main:skipq"}))
async def ask_skip_queue(callback: CallbackQuery, state: FSMContext):
    user_id = await backend_user_id(callback)
    if not user_id:
        await callback.answer("Пользователь не найден", show_alert=True)
        return
    preview = await api_client.ap_skip_queue_preview(user_id)
    text = (
        "⏭ *Пропустить очередь автокомментинга?*\n\n"
        f"Будут остановлены *{int(preview.get('pending_count') or 0)}* ожидающих заказов "
        f"и отменены *{int(preview.get('outbox_count') or 0)}* событий создания.\n"
        f"Видео уйдут в пропущенные: *{int(preview.get('video_count') or 0)}*.\n"
        f"Возврат на баланс: ~*{money_fmt(preview.get('refund'))}*.\n"
    )
    if preview.get("active_task_id"):
        text += f"Активный заказ `#{preview.get('active_task_id')}` не трогаем.\n\n"
    text += "Эти видео больше не запустятся автоматически."
    await edit_or_answer(callback, text, reply_markup=get_skip_queue_confirm_keyboard())
    await callback.answer()
