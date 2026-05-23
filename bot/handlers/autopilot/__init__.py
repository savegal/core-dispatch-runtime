from __future__ import annotations

import logging

from aiogram import F, Router
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery

from bot.handlers.autopilot import actions, card, current, limits, main, report
from bot.handlers.autopilot.common import backend_user_id, set_report_filters
from bot.services.api_client import api_client

logger = logging.getLogger("bot.autopilot")

router = Router()
router.include_router(main.router)
router.include_router(limits.router)
router.include_router(current.router)
router.include_router(report.router)
router.include_router(card.router)
router.include_router(actions.router)


# TODO: remove old vp:* autopilot aliases after 1-2 months.
@router.callback_query(F.data == "vp:autopilot_control")
async def alias_autopilot_control(callback: CallbackQuery):
    await main.show_ap_main(callback)
    await callback.answer()


@router.callback_query(F.data == "vp:autopilot_report")
async def alias_autopilot_report(callback: CallbackQuery, state: FSMContext):
    await set_report_filters(state, ap_view="summary")
    await report.show_report(callback, state)
    await callback.answer()


@router.callback_query(F.data == "vp:autopilot_report:noop")
async def alias_report_noop(callback: CallbackQuery):
    await callback.answer()


@router.callback_query(F.data.in_({"vp:autoctl:current", "vp:autoctl:queue"}))
async def alias_current(callback: CallbackQuery):
    await current.show_ap_work(callback, tab="run")
    await callback.answer()


@router.callback_query(F.data.startswith("vp:autoctl:mode:"))
async def alias_mode(callback: CallbackQuery):
    mode = (callback.data or "").split(":")[-1]
    user_id = await backend_user_id(callback)
    if not user_id:
        await callback.answer("Пользователь не найден", show_alert=True)
        return
    await api_client.update_ap_limits(user_id, mode=mode)
    await main.show_ap_main(callback)
    await callback.answer("Готово")


@router.callback_query(F.data.startswith("vp:autoctl:limits:"))
async def alias_limits(callback: CallbackQuery):
    parts = (callback.data or "").split(":")
    user_id = await backend_user_id(callback)
    if not user_id:
        await callback.answer("Пользователь не найден", show_alert=True)
        return
    try:
        tick, creator, hour = int(parts[-3]), int(parts[-2]), int(parts[-1])
    except (ValueError, IndexError):
        await callback.answer("Некорректные лимиты", show_alert=True)
        return
    await api_client.update_ap_limits(
        user_id,
        max_create_per_tick=tick,
        max_create_per_creator=creator,
        max_create_per_hour=hour,
    )
    await limits.show_limits(callback)
    await callback.answer("Готово")


@router.callback_query(F.data == "vp:autoctl:cleanup:ask")
async def alias_archive_ask(callback: CallbackQuery, state: FSMContext):
    await actions.ask_archive(callback, state)


@router.callback_query(F.data == "vp:autoctl:cleanup:do")
async def alias_archive_do(callback: CallbackQuery, state: FSMContext):
    await actions.do_archive(callback, state)


@router.callback_query(F.data.startswith("vp:autopilot_report:cancel_all:ask"))
async def alias_cancel_all_ask(callback: CallbackQuery, state: FSMContext):
    await current.ask_cancel_all(callback, state)


@router.callback_query(F.data.startswith("vp:autopilot_report:cancel_all:do"))
async def alias_cancel_all_do(callback: CallbackQuery):
    await actions.do_cancel_all(callback)


@router.callback_query(F.data.startswith("vp:autopilot_report:cancel_creator:ask:"))
async def alias_cancel_creator_ask(callback: CallbackQuery, state: FSMContext):
    parts = (callback.data or "").split(":")
    try:
        creator_id = int(parts[-2])
    except (ValueError, IndexError):
        creator_id = None
    if creator_id:
        await set_report_filters(state, ap_view="list", ap_creator_id=creator_id, ap_page=0)
    await report.show_report(callback, state)
    await callback.answer("Старый экран заменён новым журналом", show_alert=True)


@router.callback_query(F.data.startswith("vp:autopilot_report:cancel_creator:do:"))
async def alias_cancel_creator_do(callback: CallbackQuery, state: FSMContext):
    parts = (callback.data or "").split(":")
    include_running = parts[-1] == "run"
    try:
        creator_id = int(parts[-3] if include_running else parts[-2])
    except (ValueError, IndexError):
        await callback.answer("Блогер не найден", show_alert=True)
        return
    user_id = await backend_user_id(callback)
    if not user_id:
        await callback.answer("Пользователь не найден", show_alert=True)
        return
    await api_client.cancel_autopilot_work(user_id, creator_id=creator_id, include_running=include_running)
    await set_report_filters(state, ap_view="list", ap_creator_id=creator_id, ap_page=0)
    await report.show_report(callback, state)
    await callback.answer("Готово")


@router.callback_query(F.data.startswith("vp:autopilot_report:hide_errors:"))
async def alias_hide_errors(callback: CallbackQuery, state: FSMContext):
    await set_report_filters(state, ap_view="list", ap_status="error", ap_page=0)
    await report.show_report(callback, state)
    await callback.answer("Старый экран заменён новым журналом", show_alert=True)


@router.callback_query(F.data.startswith("vp:autopilot_report:f:"))
async def alias_report_filter(callback: CallbackQuery, state: FSMContext):
    parts = (callback.data or "").split(":")
    status = parts[3] if len(parts) > 3 else "all"
    await set_report_filters(state, ap_view="list", ap_status=status, ap_page=0)
    await report.show_report(callback, state)
    await callback.answer()


@router.callback_query(F.data.startswith("vp:autopilot_report:page:"))
async def alias_report_page(callback: CallbackQuery, state: FSMContext):
    try:
        page = int((callback.data or "").split(":")[-1])
    except ValueError:
        page = 0
    await set_report_filters(state, ap_view="list", ap_page=page)
    await report.show_report(callback, state)
    await callback.answer()


@router.callback_query(F.data.startswith("vp:autopilot_report:c:"))
async def alias_report_creator(callback: CallbackQuery, state: FSMContext):
    parts = (callback.data or "").split(":")
    try:
        creator_id = int(parts[3])
    except (ValueError, IndexError):
        creator_id = None
    await set_report_filters(state, ap_view="list", ap_creator_id=creator_id, ap_page=0)
    await report.show_report(callback, state)
    await callback.answer()


@router.callback_query(F.data.in_({"vp:autoctl:safefix:do", "vp:autoctl:stale:do"}))
async def alias_safe_fix(callback: CallbackQuery):
    user_id = await backend_user_id(callback)
    if user_id:
        try:
            await api_client.cleanup_autopilot_stale_queue(user_id)
        except Exception:
            logger.debug("Legacy stale cleanup alias failed", exc_info=True)
    await current.show_ap_work(callback, tab="run")
    await callback.answer("Готово")


@router.callback_query(F.data.in_({"vp:autoctl:skipnew:ask", "vp:autoctl:skipnew:do"}))
async def alias_skip_new(callback: CallbackQuery):
    user_id = await backend_user_id(callback)
    if not user_id:
        await callback.answer("Пользователь не найден", show_alert=True)
        return
    if callback.data == "vp:autoctl:skipnew:do":
        await api_client.ap_action(user_id, "skip-queue")
        await callback.answer("Готово")
    await main.show_ap_main(callback)
