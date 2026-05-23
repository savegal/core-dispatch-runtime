from __future__ import annotations

import logging

from aiogram import F, Router
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup

from bot.handlers.autopilot.common import backend_user_id, edit_or_answer, get_report_filters, money_fmt
from bot.services.api_client import api_client

logger = logging.getLogger("bot.autopilot.actions")
router = Router()


@router.callback_query(F.data == "ap:action:cancel_all:do")
async def do_cancel_all(callback: CallbackQuery):
    user_id = await backend_user_id(callback)
    if not user_id:
        await callback.answer("Пользователь не найден", show_alert=True)
        return
    result = await api_client.ap_action(user_id, "cancel-all")
    details = result.get("details") or {}
    text = (
        "✅ *Очередь отменена*\n\n"
        f"Отменено заказов: *{int(details.get('cancelled_pending_tasks') or 0)}*\n"
        f"Возврат: *{money_fmt(details.get('refunded_amount'))}*\n"
        f"Видео возвращено в новые: *{int(details.get('released_videos') or 0)}*"
    )
    await edit_or_answer(callback, text, reply_markup=InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🧰 Работа", callback_data="ap:w")],
        [InlineKeyboardButton(text="◀️ На главный", callback_data="ap:main")],
    ]))
    await callback.answer("Готово")


@router.callback_query(F.data == "ap:action:skipq:do")
async def do_skip_queue(callback: CallbackQuery):
    user_id = await backend_user_id(callback)
    if not user_id:
        await callback.answer("Пользователь не найден", show_alert=True)
        return
    result = await api_client.ap_action(user_id, "skip-queue")
    details = result.get("details") or {}
    if not result.get("success"):
        await callback.answer(result.get("message", "Не удалось пропустить очередь"), show_alert=True)
        return
    text = (
        "✅ *Очередь пропущена*\n\n"
        f"Остановлено заказов: *{int(details.get('cancelled_pending_tasks') or 0)}*\n"
        f"Отменено событий создания: *{int(details.get('cancelled_outbox_events') or 0)}*\n"
        f"Видео пропущено: *{int(details.get('skipped_videos') or 0)}*\n"
        f"Возврат: *{money_fmt(details.get('refunded_amount'))}*"
    )
    await edit_or_answer(callback, text, reply_markup=InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🧰 Работа", callback_data="ap:w")],
        [InlineKeyboardButton(text="◀️ На главный", callback_data="ap:main")],
    ]))
    await callback.answer("Готово")


@router.callback_query(F.data == "ap:action:stop_active:do")
async def do_stop_active(callback: CallbackQuery):
    user_id = await backend_user_id(callback)
    if not user_id:
        await callback.answer("Пользователь не найден", show_alert=True)
        return
    result = await api_client.ap_action(user_id, "stop-active")
    await callback.answer(result.get("message") or "Готово", show_alert=not result.get("success"))
    from bot.handlers.autopilot.current import show_ap_work

    await show_ap_work(callback, tab="run")


@router.callback_query(F.data == "ap:action:archive:ask")
async def ask_archive(callback: CallbackQuery, state: FSMContext):
    user_id = await backend_user_id(callback)
    if not user_id:
        await callback.answer("Пользователь не найден", show_alert=True)
        return
    filters = await get_report_filters(state)
    period = str(filters.get("ap_period") or "24h")
    preview = await api_client.ap_archive_preview(user_id, period)
    text = (
        "🗃 *Архивировать готовые?*\n\n"
        f"Будет архивировано *{int(preview.get('count') or 0)}* заказов "
        f"(done/stopped/partial/error) за период *{preview.get('period_label')}*."
    )
    keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="✅ Да, архивировать", callback_data="ap:action:archive:do")],
        [InlineKeyboardButton(text="❌ Отмена", callback_data="ap:rep")],
    ])
    await edit_or_answer(callback, text, reply_markup=keyboard)
    await callback.answer()


@router.callback_query(F.data == "ap:action:archive:do")
async def do_archive(callback: CallbackQuery, state: FSMContext):
    user_id = await backend_user_id(callback)
    if not user_id:
        await callback.answer("Пользователь не найден", show_alert=True)
        return
    filters = await get_report_filters(state)
    period = str(filters.get("ap_period") or "24h")
    result = await api_client.ap_action(user_id, "archive", period=period)
    await callback.answer("Готово" if result.get("success") else result.get("message", "Не удалось"), show_alert=not result.get("success"))
    from bot.handlers.autopilot.report import show_report

    await show_report(callback, state)


@router.callback_query(F.data.regexp(r"^(ap:action:retry:\d+:do|ap:a:r:\d+)$"))
async def do_retry(callback: CallbackQuery):
    parts = (callback.data or "").split(":")
    task_id = int(parts[3] if parts[:3] == ["ap", "action", "retry"] else parts[3])
    user_id = await backend_user_id(callback)
    if not user_id:
        await callback.answer("Пользователь не найден", show_alert=True)
        return
    result = await api_client.ap_action(user_id, "retry", task_id=task_id)
    if result.get("code") == "insufficient_funds":
        details = result.get("details") or {}
        text = (
            "💰 *Недостаточно средств на балансе*\n\n"
            f"Текущий баланс: *{money_fmt(details.get('balance'))}*\n"
            f"Нужно: *{money_fmt(details.get('required'))}*"
        )
        await edit_or_answer(callback, text, reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="◀️ К заказу", callback_data=f"ap:card:{task_id}")],
        ]))
        await callback.answer("Недостаточно средств", show_alert=True)
        return
    if not result.get("success"):
        await callback.answer(result.get("message", "Не удалось создать заказ"), show_alert=True)
        return
    new_task_id = (result.get("details") or {}).get("task_id")
    text = f"✅ *Заказ создан*\n\nНовый заказ: `#{new_task_id}`"
    await edit_or_answer(callback, text, reply_markup=InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=f"📄 Открыть #{new_task_id}", callback_data=f"ap:card:{new_task_id}")],
        [InlineKeyboardButton(text="◀️ К Журналу", callback_data="ap:rep:tab:list")],
    ]))
    await callback.answer("Готово")


@router.callback_query(F.data.regexp(r"^(ap:action:hide:\d+:do|ap:a:h:\d+)$"))
async def do_hide(callback: CallbackQuery):
    parts = (callback.data or "").split(":")
    task_id = int(parts[3] if parts[:3] == ["ap", "action", "hide"] else parts[3])
    user_id = await backend_user_id(callback)
    if not user_id:
        await callback.answer("Пользователь не найден", show_alert=True)
        return
    result = await api_client.ap_action(user_id, "hide", task_id=task_id)
    await callback.answer(result.get("message") or "Готово", show_alert=not result.get("success"))
    if not result.get("success"):
        return
    await edit_or_answer(callback, "✅ *Заказ скрыт из отчёта*", reply_markup=InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="◀️ К Журналу", callback_data="ap:rep:tab:list")],
    ]))


@router.callback_query(F.data == "ap:action:notify_admin:do")
async def do_notify_admin(callback: CallbackQuery):
    user_id = await backend_user_id(callback)
    if not user_id:
        await callback.answer("Пользователь не найден", show_alert=True)
        return
    result = await api_client.ap_action(user_id, "notify-admin")
    if result.get("code") == "cooldown":
        minutes = int((result.get("details") or {}).get("remaining_minutes") or 0)
        await callback.answer(f"Можно снова через {minutes} мин", show_alert=True)
    else:
        await callback.answer("Админ уведомлён" if result.get("success") else "Не удалось уведомить", show_alert=not result.get("success"))
    from bot.handlers.autopilot.main import show_ap_main

    await show_ap_main(callback)
