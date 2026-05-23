"""Middleware that hides comment/order features in parsing-only mode."""

from typing import Any, Awaitable, Callable

from aiogram import BaseMiddleware
from aiogram.dispatcher.event.bases import SkipHandler
from aiogram.types import CallbackQuery, Message, TelegramObject

from bot.services.software_mode import get_software_mode, is_parsing_mode, SOFTWARE_MODE_PARSING


class SoftwareModeMiddleware(BaseMiddleware):
    """Blocks worker callbacks that are not available in parsing-only mode.

    respect_admin_bypass:
      - True (default): админ/овнер видят full-режим
        даже когда global=parsing. Это admin-bypass из software_mode.
      - False: жёсткая блокировка для всех, включая админа. Оставлено только
        для сценариев, где сознательно нужен глобальный стоп без исключений.
    """

    def __init__(
        self,
        *,
        allow_video_monitoring: bool = False,
        respect_admin_bypass: bool = True,
    ):
        self.allow_video_monitoring = allow_video_monitoring
        self.respect_admin_bypass = respect_admin_bypass

    async def __call__(
        self,
        handler: Callable[[TelegramObject, dict[str, Any]], Awaitable[Any]],
        event: TelegramObject,
        data: dict[str, Any],
    ) -> Any:
        telegram_id: int | None = None
        if isinstance(event, (CallbackQuery, Message)) and event.from_user:
            telegram_id = event.from_user.id

        if self.respect_admin_bypass:
            in_parsing = await is_parsing_mode(telegram_id=telegram_id)
        else:
            in_parsing = (await get_software_mode()) == SOFTWARE_MODE_PARSING

        if not in_parsing:
            return await handler(event, data)

        if isinstance(event, CallbackQuery):
            callback_data = event.data or ""
            if self._is_allowed_callback(callback_data):
                return await handler(event, data)
            await event.answer("В режиме Парсинг этот раздел скрыт", show_alert=True)
            raise SkipHandler()

        if isinstance(event, Message):
            if self.allow_video_monitoring:
                return await handler(event, data)
            await event.answer(
                "Сейчас включен режим Парсинг: доступен только мониторинг видео."
            )
            raise SkipHandler()

        return await handler(event, data)

    def _is_allowed_callback(self, callback_data: str) -> bool:
        if not self.allow_video_monitoring:
            return False

        if callback_data in {
            "menu:video_parsing",
            "admin:video_parsing",
            "vp:menu",
            "vp:list",
            "vp:list:noop",
            "vp:add",
            "vp:parsing_inbox",
            "vp:parsing_inbox:noop",
            "vp:parsing_inbox:skip_all",
            "vp:parsing_inbox:skip_all_confirm",
            "vp:parsing_inbox:done_all",
            "vp:parsing_inbox:done_all_confirm",
            "vp:notifications:toggle",
            "vp:autopilot_control",
            "vp:autopilot_report",
            "vp:autopilot_report:noop",
            "ap:main",
            "ap:main:refresh",
            "ap:main:notify_admin",
            "ap:limits",
            "ap:current",
            "ap:rep",
        }:
            return True
        if callback_data.startswith("ap:"):
            return True
        if callback_data.startswith(("vp:autoctl:", "vp:autopilot_report:")):
            return True
        if callback_data.startswith(("vp:list:page:", "vp:suggest:", "vp:add_interval:")):
            return True
        if callback_data.startswith(("vp:parsing_inbox:page:", "vp:parsing_inbox:skip:", "vp:parsing_inbox:done:")):
            return True
        if callback_data.startswith("vp:pin:"):
            return True
        if not callback_data.startswith("vp:creator:"):
            return False

        blocked_markers = (
            ":mode",
            ":templates",
            ":default_template",
            ":new_videos",
        )
        if any(marker in callback_data for marker in blocked_markers):
            return False

        allowed_actions = (":detail", ":toggle", ":interval", ":runnow", ":delete", ":delete:confirm")
        return any(callback_data.endswith(action) for action in allowed_actions)
