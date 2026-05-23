"""
Утилиты для форматирования сообщений.
Стандартизированное форматирование для консистентного UI.
"""
from datetime import datetime, timezone, timedelta
from typing import Optional
import re


class Emoji:
    """Константы эмодзи для единообразного использования."""

    PENDING = "⏳"
    IN_PROGRESS = "🔄"
    DONE = "✅"
    ERROR = "❌"
    PARTIAL = "⚠️"
    STOPPED = "⏹"

    BACK = "◀️"
    HOME = "🏠"
    CANCEL = "❌"
    CONFIRM = "✅"
    EDIT = "✏️"
    DELETE = "🗑️"
    REFRESH = "🔄"
    ADD = "➕"
    REMOVE = "➖"

    NEXT = "▶️"
    PREV = "◀️"
    PAGE = "📄"

    INFO = "ℹ️"
    WARNING = "⚠️"
    SUCCESS = "✅"
    ERROR_ICON = "❌"

    ACCOUNT = "🔑"
    PROXY = "🌐"
    USER = "👥"
    ORDER = "📋"
    TEMPLATE = "📝"
    GEO = "🌍"
    SETTINGS = "⚙️"

    LINK = "🔗"
    COMMENT = "💬"
    REPLY = "↩️"
    LIKES = "❤️"
    MONEY = "💰"
    LOCK = "🔒"
    UNLOCK = "🔓"


def format_status_emoji(status: str) -> str:
    status_map = {
        "pending": Emoji.PENDING,
        "in_progress": Emoji.IN_PROGRESS,
        "done": Emoji.DONE,
        "error": Emoji.ERROR,
        "partial": Emoji.PARTIAL,
        "stopped": Emoji.STOPPED,
        "active": Emoji.DONE,
        "dead": Emoji.ERROR,
    }
    return status_map.get(status.lower(), "❓")


def format_date(date_str: Optional[str], format_str: str = "%d.%m") -> str:
    if not date_str:
        return ""
    try:
        if "T" in date_str or "+" in date_str or "Z" in date_str:
            date_str = date_str.replace("Z", "+00:00")
            dt = datetime.fromisoformat(date_str)
        else:
            dt = datetime.fromisoformat(date_str)
        return dt.strftime(format_str)
    except Exception:
        return ""


def format_relative_datetime(date_str: Optional[str]) -> str:
    """
    Возвращает дружелюбное представление даты-времени для списков:
    - "сегодня HH:MM"
    - "вчера HH:MM"
    - "DD.MM HH:MM" (если дальше)
    - "DD.MM.YY HH:MM" (если другой год)

    Все времена приводятся к UTC+3 (Москва/Киев), потому что хранятся в БД в UTC,
    а пользователь видит локальное время в этом часовом поясе.
    Это важно для правильного определения "сегодня" / "вчера".
    """
    if not date_str:
        return ""
    try:
        raw = date_str.replace("Z", "+00:00") if isinstance(date_str, str) else date_str
        dt = datetime.fromisoformat(raw)
    except Exception:
        return ""

    # Приводим к UTC+3 (рабочий часовой пояс пользователя).
    user_tz = timezone(timedelta(hours=3))
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    dt_local = dt.astimezone(user_tz)
    now_local = datetime.now(user_tz)

    today_local = now_local.date()
    yesterday_local = today_local - timedelta(days=1)
    target_date = dt_local.date()

    time_part = dt_local.strftime("%H:%M")
    if target_date == today_local:
        return f"сегодня {time_part}"
    if target_date == yesterday_local:
        return f"вчера {time_part}"
    if target_date.year == today_local.year:
        return dt_local.strftime("%d.%m") + f" {time_part}"
    return dt_local.strftime("%d.%m.%y") + f" {time_part}"


def format_progress(current: int, total: int) -> str:
    return f"{current}/{total}"


def format_progress_bar(current: int, total: int, length: int = 10) -> str:
    if total == 0:
        return "░" * length
    filled = int((current / total) * length)
    return "█" * filled + "░" * (length - filled)


def format_balance(amount: float | str) -> str:
    try:
        if isinstance(amount, str):
            amount = float(amount)
        return f"{amount:.2f} грн"
    except Exception:
        return "0.00 грн"


def escape_markdown(text: str) -> str:
    if not text:
        return ""
    return re.sub(r"([\\_*[\]()])", r"\\\1", text)


def markdown_inline_code(text: object) -> str:
    raw = "" if text is None else str(text)
    return "`" + raw.replace("\\", "\\\\").replace("`", "\\`") + "`"


def markdown_handle(username: object) -> str:
    raw = ("" if username is None else str(username)).strip().lstrip("@")
    return markdown_inline_code(f"@{raw or 'unknown'}")


def markdown_to_plain_text(text: str) -> str:
    plain = re.sub(r"\\([_*`\[\]()~>#+\-=|{}.!])", r"\1", text)
    plain = plain.replace("*", "").replace("`", "")
    return plain
