from __future__ import annotations

import logging

from aiogram import F, Router
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Message

from bot.handlers.autopilot.common import AutopilotLimitsStates, backend_user_id, edit_or_answer, md
from bot.keyboards.autopilot.limits import get_custom_limits_keyboard, get_limits_keyboard
from bot.services.api_client import api_client
from bot.services.user_utils import get_user_backend_id

logger = logging.getLogger("bot.autopilot.limits")
router = Router()

PRESETS = {
    "calm": (5, 1, 20),
    "normal": (20, 3, 120),
    "fast": (50, 5, 300),
}


def build_limits_text(limits: dict, history: list[dict] | None = None) -> str:
    tick = int(limits.get("max_create_per_tick") or 20)
    creator = int(limits.get("max_create_per_creator") or 3)
    hour = int(limits.get("max_create_per_hour") or 120)
    lines = [
        "⚙️ *Лимиты автокомментинга*",
        "",
        f"Сейчас: *{tick}/{creator}/{hour}*",
        "",
        f"• За одну проверку: *{tick}* заказов",
        f"• На одного блогера: *{creator}*",
        f"• В час всего: *{hour}*",
    ]
    if history:
        lines.extend(["", "*Последние изменения*"])
        for item in history[:3]:
            lines.append(md(item.get("text") or ""))
    return "\n".join(lines)


async def show_limits(callback: CallbackQuery) -> None:
    user_id = await backend_user_id(callback)
    if not user_id:
        await callback.answer("Пользователь не найден", show_alert=True)
        return
    limits = await api_client.get_ap_limits(user_id)
    await edit_or_answer(callback, build_limits_text(limits), reply_markup=get_limits_keyboard())


@router.callback_query(F.data == "ap:limits")
async def limits_screen(callback: CallbackQuery, state: FSMContext):
    await state.set_state(None)
    await show_limits(callback)
    await callback.answer()


@router.callback_query(F.data.startswith("ap:limits:preset:"))
async def apply_preset(callback: CallbackQuery):
    name = (callback.data or "").split(":")[-1]
    preset = PRESETS.get(name)
    if not preset:
        await callback.answer("Неизвестный пресет", show_alert=True)
        return
    user_id = await backend_user_id(callback)
    if not user_id:
        await callback.answer("Пользователь не найден", show_alert=True)
        return
    tick, creator, hour = preset
    await api_client.update_ap_limits(
        user_id,
        max_create_per_tick=tick,
        max_create_per_creator=creator,
        max_create_per_hour=hour,
    )
    await show_limits(callback)
    await callback.answer("Готово")


@router.callback_query(F.data.startswith("ap:limits:mode:"))
async def apply_mode(callback: CallbackQuery):
    mode = (callback.data or "").split(":")[-1]
    user_id = await backend_user_id(callback)
    if not user_id:
        await callback.answer("Пользователь не найден", show_alert=True)
        return
    await api_client.update_ap_limits(user_id, mode=mode)
    from bot.handlers.autopilot.main import show_ap_main

    await show_ap_main(callback)
    await callback.answer("Готово")


@router.callback_query(F.data == "ap:limits:custom")
async def custom_limits_start(callback: CallbackQuery, state: FSMContext):
    await state.set_state(AutopilotLimitsStates.wait_input)
    text = (
        "✏️ *Свои лимиты*\n\n"
        "Введите 3 числа через пробел:\n"
        "• За проверку (1-1000)\n"
        "• На блогера (0-1000)\n"
        "• В час (1-10000)\n\n"
        "Пример: `30 4 200`"
    )
    await edit_or_answer(callback, text, reply_markup=get_custom_limits_keyboard())
    await callback.answer()


@router.message(AutopilotLimitsStates.wait_input)
async def custom_limits_message(message: Message, state: FSMContext):
    raw = (message.text or "").strip()
    parts = raw.split()
    if len(parts) != 3:
        await message.answer("Введите ровно 3 числа через пробел. Пример: `30 4 200`", parse_mode="Markdown", reply_markup=get_custom_limits_keyboard())
        return
    try:
        tick, creator, hour = [int(part) for part in parts]
    except ValueError:
        await message.answer("Лимиты должны быть числами. Пример: `30 4 200`", parse_mode="Markdown", reply_markup=get_custom_limits_keyboard())
        return
    if not (1 <= tick <= 1000 and 0 <= creator <= 1000 and 1 <= hour <= 10000):
        await message.answer("Диапазоны: за проверку 1-1000, на блогера 0-1000, в час 1-10000.", reply_markup=get_custom_limits_keyboard())
        return
    user_id = await get_user_backend_id(message.from_user.id) if message.from_user else None
    if not user_id:
        await message.answer("Пользователь не найден.")
        return
    await api_client.update_ap_limits(
        user_id,
        max_create_per_tick=tick,
        max_create_per_creator=creator,
        max_create_per_hour=hour,
    )
    await state.set_state(None)
    limits = await api_client.get_ap_limits(user_id)
    await message.answer(build_limits_text(limits), parse_mode="Markdown", reply_markup=get_limits_keyboard())
