from __future__ import annotations

from datetime import datetime
from typing import Any

from aiogram.types import InlineKeyboardButton

from bot.services.formatting import escape_markdown, markdown_handle, markdown_inline_code
from services.video_time_utils import coerce_unix_timestamp, create_time_from_meta


def get_video_value(video: Any, key: str, default: Any = None) -> Any:
    if isinstance(video, dict):
        return video.get(key, default)
    return getattr(video, key, default)


def normalize_video_url(url: Any) -> str:
    if not url:
        return ""
    value = str(url).strip()
    if value.startswith(("http://", "https://")):
        return value
    if value.startswith("/@"):
        return f"https://www.tiktok.com{value}"
    if "/video/" in value:
        return f"https://www.tiktok.com/{value.lstrip('/')}"
    return value


def video_create_time(video: Any) -> int | None:
    direct = coerce_unix_timestamp(
        get_video_value(video, "create_time")
        or get_video_value(video, "published_at")
        or get_video_value(video, "publish_time")
    )
    if direct is not None:
        return direct
    return create_time_from_meta(get_video_value(video, "meta"))


def _format_any_datetime(value: Any, fmt: str = "%d.%m %H:%M") -> str:
    ts = coerce_unix_timestamp(value)
    if ts is not None:
        return datetime.fromtimestamp(ts).strftime(fmt)
    if not value:
        return ""
    try:
        dt = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        return dt.astimezone().strftime(fmt) if dt.tzinfo else dt.strftime(fmt)
    except Exception:
        return ""


def video_time_label(video: Any) -> str:
    create_time = video_create_time(video)
    if create_time is not None:
        return f"опубликовано: {_format_any_datetime(create_time)}"
    discovered = get_video_value(video, "discovered_at")
    discovered_text = _format_any_datetime(discovered)
    if discovered_text:
        return f"найдено: {discovered_text}"
    return "опубликовано: неизвестно"


def video_short_id(video: Any, max_len: int = 12) -> str:
    raw = str(get_video_value(video, "video_id") or "").strip()
    if raw:
        return raw[:max_len] + ("…" if len(raw) > max_len else "")
    db_id = get_video_value(video, "id")
    return f"#{db_id}" if db_id else "unknown"


def video_number_link(video: Any, index: int) -> str:
    return str(index)


def video_id_link(video: Any, max_len: int = 12) -> str:
    label = video_short_id(video, max_len)
    url = normalize_video_url(get_video_value(video, "url"))
    return f"[{escape_markdown(label)}]({url})" if url else markdown_inline_code(label)


def format_video_markdown_line(
    video: Any,
    index: int,
    *,
    include_username: bool = False,
    status_icon: str | None = None,
    id_max_len: int = 12,
) -> str:
    """Одна строка видео для inbox/list-экранов.

    Типографика выбрана сознательно:
      - индекс — обычный порядковый номер,
      - username — `моноширный`, чтобы Telegram не ломал underscore в никах,
      - video_id — link `[id](url)` (тап → открыть TikTok),
      - время — _italic_ (вторичная информация, не привлекает фокус).

    Это даёт визуальную иерархию: глаз сразу видит «куда жать» (синий id),
    и «кто автор» (моноширный ник), время уходит на второй план.
    """
    parts = [f"{index}."]
    if status_icon:
        parts.append(status_icon)
    if include_username:
        username = get_video_value(video, "creator_username") or get_video_value(video, "username") or "unknown"
        cleaned = str(username or "").strip().lstrip("@") or "unknown"
        parts.append(markdown_handle(cleaned))
    parts.append(video_id_link(video, id_max_len))
    parts.append(f"· _{escape_markdown(video_time_label(video))}_")
    return " ".join(parts)


def format_video_markdown_list(
    videos: list[Any],
    *,
    limit: int = 10,
    include_username: bool = False,
    start_index: int = 1,
) -> list[str]:
    lines = [
        format_video_markdown_line(video, start_index + offset, include_username=include_username)
        for offset, video in enumerate(videos[:limit])
    ]
    if len(videos) > limit:
        lines.append(f"... и ещё {len(videos) - limit}")
    return lines


# ──────────────────────────────────────────────────────────────────────────────
# Inbox-specific helpers (used by software-mode inbox).
#
# Все табы используют общий `format_inbox_row(video, idx, tab)` чтобы строка и
# набор per-video кнопок были консистентны между tab=new/queued/failed/done/
# skipped. Иконки статуса и empty-state тексты вынесены сюда же, чтобы хендлер
# inbox'а оставался плоским.
# ──────────────────────────────────────────────────────────────────────────────

INBOX_TABS: tuple[tuple[str, str, str], ...] = (
    # (status, label_without_icon, emoji)
    ("new", "Новые", "🆕"),
    ("queued", "Очередь", "⏳"),
    ("failed", "Ошибки", "⚠️"),
    ("done", "Готово", "✅"),
    ("skipped", "Скип", "🗑"),
)

# Шестой "псевдо-таб" — группировка по блогерам. Хранится отдельно, чтобы не
# попадать под status-фильтр API.
INBOX_CREATORS_TAB = ("creators", "Блогеры", "📊")

ACTIVE_TAB_MARKER = "🔹"

INBOX_EMPTY_STATES: dict[str, str] = {
    "new": "🟢 Сейчас всё чисто — новых видео нет.\nБот продолжает следить за блогерами и пришлёт, когда появится.",
    "queued": "⏳ В очереди ничего нет. Все запущенные заказы завершены.",
    "failed": "✅ Ошибок нет — все заказы прошли успешно.",
    "done": "Выполненных видео ещё нет.",
    "skipped": "🗑 Пропущенных видео нет.",
    "creators": "👤 Блогеров пока нет — добавьте в разделе «Блогеры».",
}


def inbox_status_icon(display_status: str | None) -> str:
    """Иконка для строки видео в inbox (отдельно от иконки таба)."""
    return {
        "new": "🆕",
        "queued": "⏳",
        "in_progress": "⏳",
        "done": "✅",
        "done_warning": "⚠️",
        "failed": "🚨",
        "skipped_manual": "🗑",
    }.get((display_status or "").strip(), "•")


def inbox_counters_line(by_status: dict[str, int] | None) -> str:
    """Однострочный summary всех статусов: ``🆕 8 · ⏳ 3 · ⚠️ 2 · ✅ 41 · 🗑 5``.

    Принимает структуру вида {"new": N, "queued": N, ...} (то, что возвращает
    backend в `by_status` и `get_inbox_stats`). Пропуски заменяются нулями.
    """
    counts = by_status or {}
    parts = [
        f"🆕 {int(counts.get('new', 0) or 0)}",
        f"⏳ {int(counts.get('queued', 0) or 0)}",
        f"⚠️ {int(counts.get('failed', 0) or 0)}",
        f"✅ {int(counts.get('done', 0) or 0)}",
        f"🗑 {int(counts.get('skipped', 0) or 0)}",
    ]
    return " · ".join(parts)


def inbox_tab_buttons(
    by_status: dict[str, int] | None,
    active_tab: str,
    *,
    callback_for: callable,
) -> list[list[InlineKeyboardButton]]:
    """Двухрядная клавиатура табов с маркером активного.

    ``callback_for(status)`` должен вернуть строку callback_data — позволяет
    разным контекстам (главный список / drilldown блогера) генерировать свой
    namespace без переписывания хелпера.
    """
    counts = by_status or {}
    buttons: list[InlineKeyboardButton] = []
    for status, label, emoji in INBOX_TABS:
        count = int(counts.get(status, 0) or 0)
        marker = ACTIVE_TAB_MARKER if status == active_tab else emoji
        buttons.append(
            InlineKeyboardButton(
                text=f"{marker} {label} {count}",
                callback_data=callback_for(status),
            )
        )
    creators_status, creators_label, creators_emoji = INBOX_CREATORS_TAB
    marker = ACTIVE_TAB_MARKER if creators_status == active_tab else creators_emoji
    buttons.append(
        InlineKeyboardButton(
            text=f"{marker} {creators_label}",
            callback_data=callback_for(creators_status),
        )
    )
    # Раскладка 3+3.
    return [buttons[:3], buttons[3:]]


def _truncate_reason(text: str, limit: int = 90) -> str:
    raw = str(text or "").strip()
    if len(raw) <= limit:
        return raw
    return raw[: max(0, limit - 1)] + "…"


def format_inbox_row(video: Any, idx: int, tab: str) -> list[str]:
    """Markdown-строки одного видео для inbox (1-2 строки в зависимости от таба).

    В строке всегда: clickable-индекс с ссылкой на TikTok, ник блогера, короткий
    video_id и временная метка (опубликовано/найдено). В failed/done/queued
    добавляется вторая строка со статусом или человеческой причиной — чтобы
    юзер видел контекст без открытия карточки.
    """
    base = format_video_markdown_line(video, idx, include_username=True)
    lines = [base]

    human_reason = get_video_value(video, "human_reason")
    status_label = get_video_value(video, "status_label")
    task_id = get_video_value(video, "task_id")
    task_status = get_video_value(video, "task_status")
    meta = get_video_value(video, "meta") or {}
    commented_manually = bool(meta.get("commented_manually")) if isinstance(meta, dict) else False

    if tab == "queued":
        # Показываем фактическое состояние задачи: in_progress / pending /
        # stopped — это помогает понять, можно ли её остановить.
        parts = []
        if task_status:
            label_map = {
                "in_progress": "в работе",
                "pending": "ожидает",
                "stopped": "остановлена",
                "done": "завершена",
                "error": "ошибка",
                "partial": "частично",
            }
            parts.append(label_map.get(str(task_status), str(task_status)))
        if task_id:
            parts.append(f"#{task_id}")
        if parts:
            lines.append(f"   {escape_markdown(' · '.join(parts))}")
    elif tab == "failed":
        reason = _truncate_reason(human_reason or "Ошибка обработки")
        lines.append(f"   ⚠️ {escape_markdown(reason)}")
    elif tab == "done":
        if commented_manually:
            lines.append(f"   ✅ {escape_markdown('Прокомментировано вручную')}")
        elif video_has_warnings(video):
            reason = _truncate_reason(human_reason or "Готово с предупреждением")
            lines.append(f"   ⚠️ {escape_markdown(reason)}")
        elif human_reason:
            lines.append(f"   ✅ {escape_markdown(_truncate_reason(human_reason))}")

    return lines


def video_has_warnings(video: Any) -> bool:
    flag = get_video_value(video, "has_warnings")
    if flag:
        return True
    warnings = get_video_value(video, "warnings")
    return bool(warnings)


def inbox_row_action_buttons(
    video: Any,
    tab: str,
    *,
    bulk_mode: bool = False,
    bulk_selected: bool = False,
) -> list[InlineKeyboardButton]:
    """Ряд per-video кнопок согласно текущему табу.

    Если ``bulk_mode=True`` — заменяет всё на чекбокс выбора плюс url-кнопку.
    Иначе формирует тонко настроенный набор: для new — запустить/уже/скип,
    для queued — открыть заказ и (опционально) остановить, для failed —
    повторить/заказ/скип, для done — открыть заказ, для skipped — вернуть.
    """
    db_id = get_video_value(video, "id")
    url = normalize_video_url(get_video_value(video, "url"))
    task_id = get_video_value(video, "task_id")
    task_status = get_video_value(video, "task_status")

    if bulk_mode:
        check_label = "☑ Отмечено" if bulk_selected else "⬜ Выбрать"
        row: list[InlineKeyboardButton] = []
        if url:
            row.append(InlineKeyboardButton(text="🎬", url=url))
        row.append(
            InlineKeyboardButton(
                text=check_label,
                callback_data=f"vp:inbox:bulk:toggle:{db_id}",
            )
        )
        return row

    row: list[InlineKeyboardButton] = []
    if url:
        row.append(InlineKeyboardButton(text="🎬", url=url))

    if tab == "new":
        row.append(InlineKeyboardButton(text="🚀 Заказ", callback_data=f"vp:inbox:act:run:{db_id}"))
        row.append(InlineKeyboardButton(text="✅ Уже", callback_data=f"vp:inbox:act:done:{db_id}"))
        row.append(InlineKeyboardButton(text="🗑 Скип", callback_data=f"vp:inbox:act:skip:{db_id}"))
    elif tab == "queued":
        if task_id:
            row.append(
                InlineKeyboardButton(
                    text=f"📋 Заказ #{task_id}",
                    callback_data=f"vp:inbox:act:order:{db_id}",
                )
            )
        if str(task_status or "").lower() == "in_progress":
            row.append(
                InlineKeyboardButton(
                    text="⏹ Остановить",
                    callback_data=f"vp:inbox:act:stop:{db_id}",
                )
            )
    elif tab == "failed":
        row.append(InlineKeyboardButton(text="🔁 Повторить", callback_data=f"vp:inbox:act:retry:{db_id}"))
        if task_id:
            row.append(
                InlineKeyboardButton(
                    text=f"📋 Заказ #{task_id}",
                    callback_data=f"vp:inbox:act:order:{db_id}",
                )
            )
        row.append(InlineKeyboardButton(text="🗑 Скип", callback_data=f"vp:inbox:act:skip:{db_id}"))
    elif tab == "done":
        if task_id:
            row.append(
                InlineKeyboardButton(
                    text=f"📋 Заказ #{task_id}",
                    callback_data=f"vp:inbox:act:order:{db_id}",
                )
            )
    elif tab == "skipped":
        row.append(
            InlineKeyboardButton(
                text="↩️ Вернуть в новые",
                callback_data=f"vp:inbox:act:unskip:{db_id}",
            )
        )

    return row


def inbox_empty_state_text(tab: str) -> str:
    return INBOX_EMPTY_STATES.get(tab, INBOX_EMPTY_STATES["new"])
