"""Resume an order only after its owner explicitly chooses mention handling."""
import asyncio
from copy import copy
import time
import weakref

from aiogram import Router, F
from services.mention_policy import PLAIN_TEXT, ATTEMPT_MENTIONS
from bot.services.mention_choice import CALLBACK_PREFIX, PENDING_KEY, SELECTED_KEY, choice_is_fresh


router = Router()
_LOCKS = weakref.WeakValueDictionary()


async def _resume_order(callback, state, resume):
    if not isinstance(resume, str):
        await callback.answer("Откройте заказ заново", show_alert=True)
        return
    if resume == "wizard:confirm":
        from bot.handlers.order_wizard import wizard_confirm_order
        handler = wizard_confirm_order
    elif resume == "wizard:confirm_with_template":
        from bot.handlers.order_wizard import wizard_confirm_with_template
        handler = wizard_confirm_with_template
    elif resume == "new_template:confirm":
        from bot.handlers.quick_order import confirm_new_template
        handler = confirm_new_template
    elif resume.startswith("quicktpl:confirm:"):
        from bot.handlers.quick_order import confirm_template
        handler = confirm_template
    elif resume == "engagement:confirm":
        from bot.handlers.engagement_wizard import confirm_engagement_order
        handler = confirm_engagement_order
    elif resume == "composer:submit":
        from bot.handlers.composer import submit_order
        handler = submit_order
    else:
        await callback.answer("Откройте заказ заново", show_alert=True)
        return
    forwarded = callback.model_copy(update={"data": resume}) if hasattr(callback, "model_copy") else copy(callback)
    if not hasattr(callback, "model_copy"):
        forwarded.data = resume
    await handler(forwarded, state)


@router.callback_query(F.data.startswith(CALLBACK_PREFIX))
async def choose_mention_mode(callback, state):
    parts = (callback.data or "").removeprefix(CALLBACK_PREFIX).split(":")
    if len(parts) != 2 or parts[0] not in (PLAIN_TEXT, ATTEMPT_MENTIONS) or callback.message is None:
        await callback.answer("Откройте заказ заново", show_alert=True)
        return
    key = (asyncio.get_running_loop(), callback.from_user.id, callback.message.chat.id)
    lock = _LOCKS.get(key)
    if lock is None:
        lock = asyncio.Lock()
        _LOCKS[key] = lock
    async with lock:
        data = await state.get_data()
        pending = data.get(PENDING_KEY) if isinstance(data, dict) else None
        if (not choice_is_fresh(pending) or pending.get("token") != parts[1] or pending.get("user_id") != callback.from_user.id
                or pending.get("chat_id") != callback.message.chat.id
                or pending.get("message_id") != callback.message.message_id
                ):
            await callback.answer("Этот выбор уже недействителен. Откройте заказ заново", show_alert=True)
            return
        if parts[0] == ATTEMPT_MENTIONS and pending.get("over_limit"):
            await callback.answer("Можно до 3 отметок в одном комментарии. Измените текст или выберите обычный текст", show_alert=True)
            return
        await state.update_data(**{PENDING_KEY: None, SELECTED_KEY: {**pending, "mode": parts[0], "created_at": time.time()}})
        await _resume_order(callback, state, pending["resume"])
