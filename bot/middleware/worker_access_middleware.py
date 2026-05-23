"""
Middleware для проверки доступа worker-пользователей.

Блокирует обход через ручные callback'и и устаревшие кнопки, если пользователь заблокирован.
"""
from typing import Callable, Dict, Any, Awaitable
from aiogram import BaseMiddleware
from aiogram.types import TelegramObject, CallbackQuery, Message
from aiogram.dispatcher.event.bases import SkipHandler
from bot.services.api_client import api_client
from bot.services.subscription_service import check_user_access, check_user_blocked
from bot.keyboards.common import get_access_denied_keyboard
import logging

logger = logging.getLogger("bot.middleware.worker")


class WorkerAccessMiddleware(BaseMiddleware):
    """Проверяет доступ пользователя перед выполнением worker-хендлера."""

    async def __call__(
        self,
        handler: Callable[[TelegramObject, Dict[str, Any]], Awaitable[Any]],
        event: TelegramObject,
        data: Dict[str, Any]
    ) -> Any:
        if isinstance(event, CallbackQuery):
            user_id = event.from_user.id
            username = event.from_user.username
        elif isinstance(event, Message):
            user_id = event.from_user.id
            username = event.from_user.username
        else:
            return await handler(event, data)

        try:
            user_data = await api_client.sync_telegram(user_id, username, use_cache=True)
            user_backend_id = user_data.get("id")
            role = user_data.get("role", "user")
            has_access = await check_user_access(
                telegram_id=user_id,
                user_backend_id=user_backend_id,
                role=role,
                use_cache=True
            )
            if has_access:
                return await handler(event, data)

            is_blocked, _ = await check_user_blocked(user_id, user_backend_id)
            is_pending = bool(user_data.get("is_pending_approval"))
            if is_pending:
                text = (
                    "*⏳ Ожидайте одобрения администратором*\n\n"
                    "Ваша заявка получена и отправлена администратору.\n"
                    "Как только её рассмотрят — бот сразу станет доступен."
                )
            elif is_blocked:
                text = (
                    "*🔒 Доступ запрещён*\n\n"
                    "Вы заблокированы администратором.\n"
                    "Для разблокировки обратитесь к администратору."
                )
            else:
                text = (
                    "*Доступ не подтверждён*\n\n"
                    "Бот не смог подтвердить активный доступ пользователя.\n"
                    "Проверьте доступ ещё раз или обратитесь к администратору."
                )

            keyboard_for_pending = get_access_denied_keyboard(pending_approval=is_pending)

            state = data.get("state")
            if state:
                await state.clear()

            if isinstance(event, CallbackQuery):
                try:
                    await event.answer("Доступ запрещён", show_alert=True)
                except Exception:
                    pass

                if event.message:
                    try:
                        await event.message.edit_text(
                            text,
                            reply_markup=keyboard_for_pending,
                            parse_mode="Markdown"
                        )
                    except Exception:
                        await event.message.answer(
                            text,
                            reply_markup=keyboard_for_pending,
                            parse_mode="Markdown"
                        )
            else:
                await event.answer(
                    text,
                    reply_markup=keyboard_for_pending,
                    parse_mode="Markdown"
                )
            return
        except SkipHandler:
            raise
        except Exception as e:
            logger.error(f"Error checking worker access for user {user_id}: {e}", exc_info=True)
            if isinstance(event, CallbackQuery):
                await event.answer("Ошибка проверки доступа", show_alert=True)
            elif isinstance(event, Message):
                await event.answer("Ошибка проверки доступа")
            return
