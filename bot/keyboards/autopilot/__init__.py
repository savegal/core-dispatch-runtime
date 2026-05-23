"""Inline keyboards for the worker autopilot section."""

from .card import get_card_keyboard, get_retry_confirm_keyboard, get_hide_confirm_keyboard
from .current import (
    get_cancel_all_confirm_keyboard,
    get_current_keyboard,
    get_skip_queue_confirm_keyboard,
    get_stop_active_confirm_keyboard,
    get_work_keyboard,
)
from .limits import get_custom_limits_keyboard, get_limits_keyboard
from .main import get_main_keyboard, get_notify_admin_confirm_keyboard
from .report import get_report_keyboard

__all__ = [
    "get_card_keyboard",
    "get_retry_confirm_keyboard",
    "get_hide_confirm_keyboard",
    "get_current_keyboard",
    "get_cancel_all_confirm_keyboard",
    "get_skip_queue_confirm_keyboard",
    "get_stop_active_confirm_keyboard",
    "get_work_keyboard",
    "get_custom_limits_keyboard",
    "get_limits_keyboard",
    "get_main_keyboard",
    "get_notify_admin_confirm_keyboard",
    "get_report_keyboard",
]
