"""Small presentation primitives shared by Telegram order screens."""
from datetime import datetime, timezone
from html import escape
import math
from aiogram.types import LinkPreviewOptions
from services.publication_links import comment_link

NO_LINK_PREVIEWS = LinkPreviewOptions(is_disabled=True)


def count(value):
    try:
        return max(0, int(value or 0))
    except (TypeError, ValueError, OverflowError):
        return 0


def number(value):
    return f"{count(value):,}".replace(",", " ")


def excerpt(value, limit=90, *, encoded_limit=240):
    """Clip before escaping, while keeping entities and Telegram message size intact."""
    value = " ".join(str(value or "").split())
    parts, size = [], 0
    for char in value[:limit]:
        encoded = escape(char)
        if size + len(encoded) > encoded_limit:
            break
        parts.append(encoded)
        size += len(encoded)
    return "".join(parts) + ("…" if len(parts) < len(value) else "")


def utc_time(value):
    """Show a supplied timestamp; rendering never invents an event or refresh time."""
    try:
        stamp = value if isinstance(value, datetime) else datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        if stamp.tzinfo is None:
            stamp = stamp.replace(tzinfo=timezone.utc)
        return stamp.astimezone(timezone.utc).strftime("%d.%m · %H:%M:%S UTC")
    except (TypeError, ValueError):
        return None


def progress_bar(done, total, width=10):
    done, total = count(done), count(total)
    ratio = min(done, total) / total if total else 0
    # Do not round an incomplete target into a full bar.
    filled = math.floor(ratio * width)
    return "▰" * filled + "▱" * (width - filled)


def percent(value):
    try:
        rate = float(value)
        return f"{rate:.2f}%" if math.isfinite(rate) else "—"
    except (TypeError, ValueError):
        return "—"


def publication_links(row):
    """A known CID is linkable independently of its public visibility state."""
    cid = row.get("comment_id") or row.get("reply_comment_id") or row.get("reply_id")
    url = row.get("video_url") or row.get("url") or ""
    link = comment_link(row.get("aweme_id"), cid, url)
    if not link:
        return ""
    label = "Открыть ответ" if row.get("kind") == "reply" else "Открыть комментарий"
    anchors = [f'<a href="{escape(link, quote=True)}">{label}</a>']
    if row.get("kind") == "reply":
        parent = comment_link(row.get("aweme_id"), row.get("parent_id") or row.get("parent_comment_id"), url)
        if parent:
            anchors.append(f'<a href="{escape(parent, quote=True)}">Родитель</a>')
    return " · ".join(anchors)


def main_menu_text(parsing_only, balance_text=""):
    """Markdown to match the existing main-menu transport."""
    if parsing_only:
        return "*Мониторинг видео*\n\nНовые ролики, отбор и история наблюдений.\n\nВыберите действие:"
    return ("*TeletanSoft*\nРабочее пространство" + balance_text + "\n\n"
            "*Создать* — количество комментариев и ответов.\n"
            "*Заказы* — ход работы, публикации и отчёты.\n"
            "*Шаблоны* — сохранённые тексты и параметры.\n\n"
            "Выберите действие:")
