"""
Уведомления владельца о новых работниках, ждущих одобрения.

Логика:
- При первом /start, который реально создал запись в БД, бэкенд возвращает
  is_new=True. Бот в этот момент шлёт владельцу одно сообщение со сводкой
  и кнопками управления, переиспользуя уже существующие admin-callback'и:
    * "✅ Одобрить" → admin:unblock:do:{user_id}
        (унифицированно с разблокировкой; первый unblock также выставляет
        was_approved_once=True на бэкенде)
    * "🚫 Заблокировать" → admin:block:do:{user_id}
    * "👤 Открыть карточку" → admin:user:{user_id}
- Если REQUIRE_WORKER_APPROVAL отключён, шлём только информационный пинг
  (без кнопок одобрения), потому что юзер уже разблокирован.
- Этот модуль НИЧЕГО не делает с самим пользователем — только уведомление.
"""
from __future__ import annotations

import logging
from typing import Any, Mapping

from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

from bot.services.formatting import markdown_handle
from services.notify_service import send_notification_to_owner

logger = logging.getLogger("bot.worker_approval")


def _format_username(user_data: Mapping[str, Any] | None, telegram_id: int) -> str:
    if not user_data:
        return f"ID {telegram_id}"
    raw = user_data.get("username") or ""
    if not raw:
        return f"ID {telegram_id}"
    try:
        return markdown_handle(str(raw))
    except Exception:
        return str(raw)


async def notify_owner_about_new_worker(
    *,
    user_data: Mapping[str, Any],
    telegram_id: int,
) -> bool:
    """Отправляет владельцу уведомление о новом работнике.

    Возвращает True, если сообщение доставлено. Никаких исключений
    наружу — это диагностическое уведомление, не должно ронять /start.
    """
    try:
        user_backend_id = user_data.get("id")
        if not user_backend_id:
            return False

        is_pending = bool(user_data.get("is_pending_approval"))
        username_md = _format_username(user_data, telegram_id)

        if is_pending:
            text = (
                "*🆕 Новый работник ждёт одобрения*\n\n"
                f"👤 {username_md}\n"
                f"🆔 Telegram ID: `{telegram_id}`\n"
                f"🗂 Внутренний ID: `{user_backend_id}`\n\n"
                "Доступ к боту заблокирован до вашего решения."
            )
            keyboard = InlineKeyboardMarkup(
                inline_keyboard=[
                    [
                        InlineKeyboardButton(
                            text="✅ Одобрить",
                            callback_data=f"admin:unblock:do:{user_backend_id}",
                        ),
                        InlineKeyboardButton(
                            text="🚫 Заблокировать",
                            callback_data=f"admin:block:do:{user_backend_id}",
                        ),
                    ],
                    [
                        InlineKeyboardButton(
                            text="👤 Открыть карточку",
                            callback_data=f"admin:user:{user_backend_id}",
                        ),
                    ],
                ]
            )
        else:
            # Approval отключён глобально — просто информируем.
            text = (
                "*🆕 Новый работник зарегистрирован*\n\n"
                f"👤 {username_md}\n"
                f"🆔 Telegram ID: `{telegram_id}`\n"
                f"🗂 Внутренний ID: `{user_backend_id}`\n\n"
                "Подтверждение не требуется (REQUIRE_WORKER_APPROVAL=False)."
            )
            keyboard = InlineKeyboardMarkup(
                inline_keyboard=[
                    [
                        InlineKeyboardButton(
                            text="👤 Открыть карточку",
                            callback_data=f"admin:user:{user_backend_id}",
                        ),
                    ],
                ]
            )

        return await send_notification_to_owner(text, keyboard=keyboard)
    except Exception as exc:
        logger.warning(
            "Failed to notify owner about new worker telegram_id=%s: %s",
            telegram_id,
            exc,
        )
        return False
