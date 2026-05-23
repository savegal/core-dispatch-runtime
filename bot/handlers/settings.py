"""Обработчики для настроек."""
from aiogram import Router, F
from aiogram.types import CallbackQuery
from bot.ui.callbacks import (
    SETTINGS_TOGGLE_DELAYS, SETTINGS_TOGGLE_SHUFFLE, SETTINGS_TOGGLE_ROTATE,
    SETTINGS_REPLY_MODE,
)
from bot.handlers.menu import show_settings
import logging

logger = logging.getLogger("bot.settings")
router = Router()


@router.callback_query(F.data == SETTINGS_TOGGLE_DELAYS)
async def toggle_delays(callback: CallbackQuery):
    """Старый callback: настройки больше не используются."""
    try:
        await callback.answer("Личные настройки отключены", show_alert=False)
        await show_settings(callback)
    except Exception as e:
        logger.error(f"Error toggling delays: {e}", exc_info=True)
        await callback.answer("Ошибка", show_alert=True)


@router.callback_query(F.data == SETTINGS_TOGGLE_SHUFFLE)
async def toggle_shuffle(callback: CallbackQuery):
    """Старый callback: настройки больше не используются."""
    try:
        await callback.answer("Личные настройки отключены", show_alert=False)
        await show_settings(callback)
    except Exception as e:
        logger.error(f"Error toggling shuffle: {e}", exc_info=True)
        await callback.answer("Ошибка", show_alert=True)


@router.callback_query(F.data == SETTINGS_TOGGLE_ROTATE)
async def toggle_rotate(callback: CallbackQuery):
    """Старый callback: настройки больше не используются."""
    try:
        await callback.answer("Личные настройки отключены", show_alert=False)
        await show_settings(callback)
    except Exception as e:
        logger.error(f"Error toggling rotate: {e}", exc_info=True)
        await callback.answer("Ошибка", show_alert=True)


@router.callback_query(F.data.startswith(f"{SETTINGS_REPLY_MODE}:"))
async def set_reply_mode(callback: CallbackQuery):
    """Старый callback: настройки больше не используются."""
    try:
        await callback.answer("Личные настройки отключены", show_alert=False)
        await show_settings(callback)
    except Exception as e:
        logger.error(f"Error setting reply mode: {e}", exc_info=True)
        await callback.answer("Ошибка", show_alert=True)
