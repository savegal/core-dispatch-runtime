from aiogram import Router, F
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message
from aiogram.fsm.context import FSMContext
from aiogram.filters import StateFilter
from aiogram.exceptions import TelegramBadRequest
from bot.services.api_client import APIRequestError, api_client
from bot.services.user_utils import get_user_backend_id
from bot.services.formatting import Emoji, format_date, format_progress, format_relative_datetime
from bot.services.likes_ui import LIKES_DISABLED_LABEL
from bot.keyboards.common import get_main_menu_keyboard, get_access_denied_keyboard
from bot.config import load_config
from bot.services.drafts import get_draft
from bot.services.subscription_service import check_user_access, check_user_blocked
from bot.services.bot_ui import block, empty_screen, screen
from html import escape as html_escape, unescape as html_unescape
import logging
import re
from bot.state.order_states import OrderStates
from bot.services.template_utils import normalize_user_text, preserve_nonempty_lines
from services.user_facing_errors import (
    build_task_display_reason,
    build_task_execution_summary,
    collect_task_warnings,
    humanize_error_text,
    humanize_warning,
)
from services.task_execution_ledger import LEDGER_VERSION, build_url_execution_ledger

logger = logging.getLogger("bot.orders")
router = Router()
config = load_config()

# ИСПРАВЛЕНО: Используем значение из settings вместо hardcoded
from config.settings import get_settings
settings = get_settings()
ORDERS_PER_PAGE = min(int(settings.ORDERS_PER_PAGE or 5), 5)
ACCOUNT_POOL_MODE_SHARED = "shared"
ACCOUNT_POOL_MODE_PERSONAL = "personal"
ORDERS_FETCH_LIMIT = 100
ORDER_FILTERS = {
    "all": "Все",
    "active": "Активные",
    "running": "В работе",
    "failed": "Ошибки",
    "stopped": "Остановленные",
    "done": "Готовые",
}
ORDER_TABS = {
    "overview": "Заказ",
    "map": "Публикации",
    "videos": "Видео",
    "texts": "Тексты",
    "settings": "Детали",
}
ORDER_DETAIL_SOFT_LIMIT = 3400
ORDER_TEXT_CHUNK_LIMIT = 1500
ORDER_MAP_REPLY_CHUNK_SIZE = 6


def _normalize_account_pool_mode(mode) -> str:
    return ACCOUNT_POOL_MODE_PERSONAL if str(mode or "").lower() == ACCOUNT_POOL_MODE_PERSONAL else ACCOUNT_POOL_MODE_SHARED


def _account_pool_label(mode) -> str:
    return "👤 Мои аккаунты" if _normalize_account_pool_mode(mode) == ACCOUNT_POOL_MODE_PERSONAL else "🌐 Общий пул"


def _normalize_order_filter(value: str | None) -> str:
    value = str(value or "all").lower()
    return value if value in ORDER_FILTERS else "all"


def _normalize_order_tab(value: str | None) -> str:
    value = str(value or "overview").lower()
    return value if value in ORDER_TABS else "overview"


def _detail_callback(
    task_id: int,
    tab: str = "overview",
    page: int = 0,
    filter_name: str = "all",
    content_page: int = 0,
) -> str:
    base = f"order:detail:{task_id}:{_normalize_order_tab(tab)}:{max(0, int(page or 0))}:{_normalize_order_filter(filter_name)}"
    content_page = max(0, int(content_page or 0))
    if content_page:
        return f"{base}:{content_page}"
    return base


def _stop_order_callback(
    action: str,
    task_id: int,
    tab: str = "overview",
    page: int = 0,
    filter_name: str = "all",
    content_page: int = 0,
) -> str:
    return (
        f"order:{action}:{int(task_id)}:{_normalize_order_tab(tab)}:"
        f"{max(0, int(page or 0))}:{_normalize_order_filter(filter_name)}:"
        f"{max(0, int(content_page or 0))}"
    )


def _priority_order_callback(
    task_id: int,
    tab: str = "overview",
    page: int = 0,
    filter_name: str = "all",
    content_page: int = 0,
) -> str:
    return (
        f"order:priority:{int(task_id)}:{_normalize_order_tab(tab)}:"
        f"{max(0, int(page or 0))}:{_normalize_order_filter(filter_name)}:"
        f"{max(0, int(content_page or 0))}"
    )


def _parse_stop_order_callback(data: str | None) -> tuple[int, str, int, str, int]:
    parts = (data or "").split(":")
    task_id = int(parts[2])
    tab = _normalize_order_tab(parts[3] if len(parts) > 3 else "overview")
    page = int(parts[4]) if len(parts) > 4 and str(parts[4]).isdigit() else 0
    filter_name = _normalize_order_filter(parts[5] if len(parts) > 5 else "all")
    content_page = int(parts[6]) if len(parts) > 6 and str(parts[6]).isdigit() else 0
    return task_id, tab, page, filter_name, content_page


def _orders_page_callback(filter_name: str = "all", page: int = 0) -> str:
    return f"orders:page:{_normalize_order_filter(filter_name)}:{max(0, int(page or 0))}"


def _cancel_pending_callback(action: str, filter_name: str = "all", page: int = 0) -> str:
    return f"orders:cancel_pending:{action}:{_normalize_order_filter(filter_name)}:{max(0, int(page or 0))}"


def _parse_cancel_pending_callback(data: str | None) -> tuple[str, str, int]:
    parts = (data or "").split(":")
    action = parts[2] if len(parts) > 2 else "ask"
    filter_name = _normalize_order_filter(parts[3] if len(parts) > 3 else "all")
    page = int(parts[4]) if len(parts) > 4 and str(parts[4]).isdigit() else 0
    return action, filter_name, page


def _parse_orders_page_callback(data: str | None) -> tuple[str, int]:
    parts = (data or "").split(":")
    if len(parts) >= 4 and parts[0] == "orders" and parts[1] == "page":
        return _normalize_order_filter(parts[2]), int(parts[3])
    if len(parts) >= 3 and parts[0] == "orders" and parts[1] == "page":
        return "all", int(parts[2])
    if len(parts) >= 3 and parts[0] == "orders" and parts[1] == "filter":
        return _normalize_order_filter(parts[2]), 0
    return "all", 0


def _parse_order_detail_callback(data: str | None) -> tuple[int, str, int, str, int]:
    parts = (data or "").split(":")
    task_id = int(parts[2])
    tab = _normalize_order_tab(parts[3] if len(parts) > 3 else "overview")
    page = int(parts[4]) if len(parts) > 4 and str(parts[4]).isdigit() else 0
    filter_name = _normalize_order_filter(parts[5] if len(parts) > 5 else "all")
    content_page = int(parts[6]) if len(parts) > 6 and str(parts[6]).isdigit() else 0
    return task_id, tab, page, filter_name, content_page


def _task_display_bucket(task: dict) -> str:
    _, display_status, _, _ = _get_order_display(task)
    if display_status in {"pending", "in_progress"}:
        return "active"
    if display_status in {"failed", "partial"}:
        return "failed"
    if display_status == "stopped":
        return "stopped"
    if display_status in {"done", "done_warning"}:
        return "done"
    status = str(task.get("status") or "").lower()
    if status in {"pending", "in_progress"}:
        return "active"
    if status in {"error", "failed", "partial"}:
        return "failed"
    if status == "stopped":
        return "stopped"
    if status == "done":
        return "done"
    return "all"


def _task_is_running(task: dict) -> bool:
    _, display_status, _, _ = _get_order_display(task)
    return display_status == "in_progress" or str(task.get("status") or "").lower() == "in_progress"


def _task_matches_filter(task: dict, filter_name: str) -> bool:
    filter_name = _normalize_order_filter(filter_name)
    if filter_name == "running":
        return _task_is_running(task)
    return filter_name == "all" or _task_display_bucket(task) == filter_name


def _order_counts(tasks: list[dict]) -> dict[str, int]:
    counts = {"all": len(tasks), "active": 0, "running": 0, "failed": 0, "stopped": 0, "done": 0}
    for task in tasks:
        if _task_is_running(task):
            counts["running"] += 1
        bucket = _task_display_bucket(task)
        if bucket in counts:
            counts[bucket] += 1
    return counts


def _short_text(text: str | None, limit: int = 64) -> str:
    text = str(text or "").replace("\n", " ").strip()
    if len(text) <= limit:
        return text
    return text[: max(0, limit - 3)].rstrip() + "..."


def _build_recreated_order_settings(orig_settings: dict | None, *, origin: str) -> dict:
    """Build clean settings for copy/retry without carrying execution artifacts."""
    source = orig_settings if isinstance(orig_settings, dict) else {}
    clean = {
        "delays": source.get("delays", True),
        "shuffle": source.get("shuffle", False),
        "rotate_accounts": source.get("rotate_accounts", True),
        "reply_mode": source.get("reply_mode", "A"),
        "reply_count": source.get("reply_count", 3),
        "origin": origin,
        "account_pool_mode": _normalize_account_pool_mode(source.get("account_pool_mode")),
    }
    if isinstance(source.get("replies"), list):
        clean["replies"] = [
            str(reply).strip()
            for reply in source.get("replies", [])[: settings.MAX_REPLIES]
            if str(reply or "").strip()
        ]
    for idx in range(4, settings.MAX_REPLIES + 1):
        key = f"reply{idx}"
        if source.get(key):
            clean[key] = source[key]
    for key in ("engagement_comment_id", "engagement_username", "engagement_comment_text"):
        if source.get(key):
            clean[key] = source[key]
    return clean


def _text_variants_for_recreated_order(task: dict) -> list[str]:
    task_settings = task.get("settings", {}) if isinstance(task.get("settings"), dict) else {}
    text_variants = task_settings.get("text_variants") or []
    text_variants = [str(item) for item in text_variants if str(item or "").strip()]
    if len(text_variants) >= 3:
        return text_variants
    return []


def _failed_urls_for_retry(task: dict) -> list[str]:
    task_settings = task.get("settings", {}) if isinstance(task.get("settings"), dict) else {}
    url_results = task_settings.get("url_results") or {}
    if not isinstance(url_results, dict):
        url_results = {}
    non_retry_statuses = {"success", "submitted_unconfirmed"}

    def _stored_url_is_retryable(url: str) -> bool:
        row = url_results.get(str(url))
        if not isinstance(row, dict):
            return True
        return str(row.get("status") or "").lower() not in non_retry_statuses

    failed_urls = [
        str(url)
        for url in (task_settings.get("failed_urls") or [])
        if str(url or "").strip()
        and _stored_url_is_retryable(str(url))
    ]
    if failed_urls:
        return failed_urls

    failed_statuses = {"fail", "failed", "error"}
    return [
        str(url)
        for url, row in url_results.items()
        if isinstance(row, dict) and str(row.get("status") or "").lower() in failed_statuses
    ]


def _order_status(task: dict) -> str:
    return str(task.get("status") or "").lower()


def _order_is_stopped(task: dict) -> bool:
    return _order_status(task) == "stopped"


def _order_is_stoppable(task: dict) -> bool:
    return _order_status(task) == "in_progress"


def _order_is_pending(task: dict) -> bool:
    return _order_status(task) == "pending"


def _order_queue_priority(task: dict) -> str:
    settings_dict = _settings_dict(task)
    priority = str(
        settings_dict.get("queue_priority")
        or settings_dict.get("task_queue_priority")
        or ""
    ).strip().lower()
    if priority == "high":
        return "high"
    return "low" if _order_origin(task).lower() in {"autopilot", "monitor_auto_comment"} else "normal"


def _order_kind(task: dict) -> str:
    return str(task.get("kind") or _settings_dict(task).get("kind") or "full").lower()


def _order_is_engagement_only(task: dict) -> bool:
    return _order_kind(task) == "engagement_only" or _order_origin(task).lower() == "engagement_only"


def _html(value: object) -> str:
    return html_escape("" if value is None else str(value), quote=False)


def _html_attr(value: object) -> str:
    return html_escape("" if value is None else str(value), quote=True)


def _html_inline_code(value: object) -> str:
    return f"<code>{_html(value)}</code>"


def _html_monospace(value: object) -> str:
    text = "" if value is None else str(value).strip()
    return _html_inline_code(text or "—")


def _html_code_block(value: object) -> str:
    return _html_monospace(value)


def _html_block(title: str, lines: list[str] | tuple[str, ...]) -> str:
    body = "\n".join(str(line) for line in lines if line)
    if body:
        return f"<b>{_html(title)}</b>\n{body}"
    return f"<b>{_html(title)}</b>"


def _html_screen(title: str, *blocks_text: str) -> str:
    parts = [f"<b>{_html(title)}</b>"]
    parts.extend(part for part in blocks_text if part)
    return "\n\n".join(parts)


def _html_to_plain_text(text: str) -> str:
    text = re.sub(r"</?(?:b|strong|i|em|u|s|code|pre)[^>]*>", "", text)
    text = re.sub(r"<a\s+[^>]*href=[\"'][^\"']+[\"'][^>]*>(.*?)</a>", r"\1", text, flags=re.IGNORECASE | re.DOTALL)
    text = re.sub(r"<[^>]+>", "", text)
    return html_unescape(text)


def _html_link(url: object) -> str:
    raw = str(url or "").strip()
    if not raw:
        return "—"
    if raw.startswith(("http://", "https://")):
        return f'<a href="{_html_attr(raw)}">{_html(raw)}</a>'
    return _html(raw)


def _settings_dict(task: dict) -> dict:
    return task.get("settings") if isinstance(task.get("settings"), dict) else {}


def _order_origin(task: dict) -> str:
    settings_dict = _settings_dict(task)
    return str(task.get("origin") or settings_dict.get("origin") or "manual")


def _order_creator_username(task: dict) -> str | None:
    username = _settings_dict(task).get("creator_username")
    username = str(username or "").strip().lstrip("@")
    return username or None


def _order_template_label(task: dict) -> str | None:
    settings_dict = _settings_dict(task)
    template_id = settings_dict.get("template_id")
    template_name = str(settings_dict.get("template_name") or "").strip()
    if template_id and template_name:
        return f"{template_name} #{template_id}"
    if template_id:
        return f"#{template_id}"
    if template_name:
        return template_name
    return None


def _order_likes_label(task: dict) -> str:
    summary = _get_execution_summary(task)
    likes_info = summary.get("likes") or {}
    settings_dict = _settings_dict(task)
    settings_enabled = settings_dict.get("boost_likes_enabled")
    if settings_enabled is False:
        return "выключены"
    if likes_info:
        if not likes_info.get("enabled") or not likes_info.get("requested"):
            return "выключены"
        orders_total = _safe_int(likes_info.get("orders_total"))
        if orders_total:
            orders_ok = _safe_int(likes_info.get("orders_ok"))
            orders_failed = _safe_int(likes_info.get("orders_failed"))
            likes_qty = int(likes_info.get("quantity", 0) or 0)
            if orders_ok and orders_failed:
                return f"{likes_qty} частично" if likes_qty > 0 else "частично"
            if orders_ok:
                return str(likes_qty) if likes_qty > 0 else "включены"
            return "ошибка"
        if likes_info.get("failed_urls"):
            return "ошибка"
        likes_qty = int(likes_info.get("quantity", 0) or 0)
        return str(likes_qty) if likes_qty > 0 else "включены"
    likes_qty = int(task.get("likes", 0) or settings_dict.get("likes_count", 0) or 0)
    return str(likes_qty) if likes_qty > 0 else "выключены"


def _safe_int(value: object, default: int = 0) -> int:
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return default


def _likes_target_mode_label(mode: object) -> str:
    mode_text = str(mode or "").strip().lower()
    if mode_text == "each_found":
        return "на каждый найденный комментарий"
    if mode_text == "single_top":
        return "на один комментарий с максимумом ответов"
    return "по настройке заказа"


def _likes_selection_reason_label(reason: object) -> str | None:
    reason_text = str(reason or "").strip().lower()
    if reason_text == "most_replies":
        return "выбран комментарий с максимумом ответов"
    if reason_text == "per_username_most_replies":
        return "выбран лучший комментарий этого username"
    if reason_text == "engagement_parent":
        return "комментарий из заказа только на ответы"
    if reason_text == "single_comment":
        return "единственный выбранный комментарий"
    return None


def _ratio_text(done: int, expected: int) -> str:
    if expected > 0:
        return f"{done}/{expected}"
    return str(done)


def _ru_plural(count: int, one: str, few: str, many: str) -> str:
    count = abs(int(count or 0))
    if count % 10 == 1 and count % 100 != 11:
        return one
    if 2 <= count % 10 <= 4 and not 12 <= count % 100 <= 14:
        return few
    return many


def _ledger_summary_section(summary: dict, section: str) -> dict:
    ledger = summary.get("ledger") if isinstance(summary.get("ledger"), dict) else {}
    ledger_summary = ledger.get("summary") if isinstance(ledger.get("summary"), dict) else {}
    value = ledger_summary.get(section) if isinstance(ledger_summary.get(section), dict) else {}
    return value


def _clean_public_username(value: object) -> str:
    return str(value or "").strip().lstrip("@")


def _order_url_ledgers(task: dict) -> list[tuple[str, dict]]:
    settings_dict = _settings_dict(task)
    url_results = settings_dict.get("url_results") if isinstance(settings_dict.get("url_results"), dict) else {}
    ledgers: list[tuple[str, dict]] = []
    for url, result in url_results.items():
        if not isinstance(result, dict):
            continue
        ledger = result.get("execution_ledger")
        if not isinstance(ledger, dict) or _safe_int(ledger.get("version")) != LEDGER_VERSION:
            ledger = build_url_execution_ledger(result, url=str(url))
        ledgers.append((str(url), ledger))
    return ledgers


def _order_parent_target_detail_lines(task: dict, *, limit: int = 5) -> list[str]:
    summary = _get_execution_summary(task)
    replies = summary.get("replies") if isinstance(summary.get("replies"), dict) else {}
    if not (
        _safe_int(replies.get("requested"))
        or _safe_int(replies.get("submitted"))
        or _safe_int(replies.get("attempted"))
    ):
        return []

    rows: list[dict] = []
    for _url, ledger in _order_url_ledgers(task):
        coverage = ledger.get("coverage") if isinstance(ledger.get("coverage"), dict) else {}
        targets = coverage.get("parent_targets") if isinstance(coverage.get("parent_targets"), list) else []
        for target in targets:
            if not isinstance(target, dict):
                continue
            submitted = _safe_int(target.get("submitted"))
            attempted = _safe_int(target.get("attempted"))
            expected = bool(target.get("expected"))
            if not (submitted or attempted or expected):
                continue
            rows.append(target)

    if not rows:
        return []

    rows.sort(
        key=lambda item: (
            -_safe_int(item.get("submitted")),
            -_safe_int(item.get("attempted")),
            str(item.get("label") or ""),
        )
    )

    lines: list[str] = []
    for target in rows[:limit]:
        label = _short_text(str(target.get("label") or "главный комментарий"), 56)
        submitted = _safe_int(target.get("submitted"))
        attempted = _safe_int(target.get("attempted"))
        confirmed = _safe_int(target.get("confirmed"))
        unconfirmed = _safe_int(target.get("unconfirmed"))
        failed = _safe_int(target.get("failed"))
        requested_author = _clean_public_username(target.get("requested_author"))
        matched_authors_raw = target.get("matched_authors") if isinstance(target.get("matched_authors"), list) else []
        matched_authors = [
            _clean_public_username(author)
            for author in matched_authors_raw
            if _clean_public_username(author)
        ]

        if submitted:
            line = f"{label}: ответов {submitted}"
        elif attempted:
            line = f"{label}: пытались {attempted}, не отправлено"
        else:
            line = f"{label}: ожидается, ответов пока нет"

        details: list[str] = []
        if confirmed:
            details.append(f"подтверждено {confirmed}")
        if unconfirmed:
            details.append(f"без проверки {unconfirmed}")
        if failed:
            details.append(f"ошибок {failed}")
        if attempted and submitted and attempted != submitted:
            details.append(f"попыток {attempted}")

        if requested_author and matched_authors:
            first_matched = matched_authors[0]
            if requested_author.lower() == first_matched.lower():
                details.append(f"автор @{requested_author}")
            elif target.get("author_fallback_used"):
                details.append(f"искали @{requested_author}, ответили @{first_matched}")
            else:
                details.append(f"искали @{requested_author}, найден @{first_matched}")
        elif requested_author:
            details.append(f"искали @{requested_author}")
        elif matched_authors:
            details.append(f"автор @{matched_authors[0]}")
        elif target.get("author_fallback_used"):
            details.append("по совпадению текста")

        if details:
            line += " · " + ", ".join(details)
        lines.append(_html(line))

    if len(rows) > limit:
        lines.append(_html(f"Ещё главных целей: {len(rows) - limit}"))
    return lines


def _order_publication_summary_lines(task: dict) -> list[str]:
    summary = _get_execution_summary(task)
    main = summary.get("main") if isinstance(summary.get("main"), dict) else {}
    replies = summary.get("replies") if isinstance(summary.get("replies"), dict) else {}
    coverage = summary.get("coverage") if isinstance(summary.get("coverage"), dict) else {}
    health = summary.get("health") if isinstance(summary.get("health"), dict) else {}
    ledger_main = _ledger_summary_section(summary, "main")
    ledger_replies = _ledger_summary_section(summary, "replies")
    lines: list[str] = []

    main_expected = _safe_int(main.get("expected"))
    main_submitted = _safe_int(main.get("submitted"))
    main_verified = _safe_int(main.get("verified_count"))
    main_failed = _safe_int(ledger_main.get("failed")) or max(0, main_expected - main_submitted)
    if main_expected or main_submitted:
        line = f"Главные: отправлено {_ratio_text(main_submitted, main_expected)}"
        if main_verified:
            line += f", подтверждено {main_verified}"
        unconfirmed = max(0, main_submitted - main_verified)
        if unconfirmed:
            line += f", без публичной проверки {unconfirmed}"
        if main_failed:
            line += f", не отправлено {main_failed}"
        lines.append(_html(line))

    replies_requested = _safe_int(replies.get("requested"))
    replies_submitted = _safe_int(replies.get("submitted"))
    replies_attempted = _safe_int(replies.get("attempted"))
    replies_failed = _safe_int(replies.get("failed"))
    replies_not_started = _safe_int(replies.get("variants_not_started"))
    replies_confirmed = _safe_int(replies.get("visibility_confirmed")) or _safe_int(replies.get("ok"))
    replies_unconfirmed = _safe_int(replies.get("submitted_unconfirmed"))
    if replies_requested or replies_submitted:
        if replies_requested:
            line = f"Ответы: отправлено {replies_submitted}, ожидалось {replies_requested}"
        else:
            line = f"Ответы: отправлено {replies_submitted}"
        if replies_attempted and replies_attempted != replies_submitted:
            line += f", попыток {replies_attempted}"
        if replies_confirmed:
            line += f", подтверждено {replies_confirmed}"
        if replies_unconfirmed:
            line += f", без публичной проверки {replies_unconfirmed}"
        if replies_failed:
            line += f", не отправлено {replies_failed}"
        if replies_not_started:
            line += f", не начато {replies_not_started}"
        lines.append(_html(line))

        author_exact = _safe_int(ledger_replies.get("author_exact_matches"))
        author_fallbacks = _safe_int(ledger_replies.get("author_fallbacks"))
        text_only = _safe_int(ledger_replies.get("text_only_matches"))
        parent_missing = _safe_int(ledger_replies.get("parent_missing"))
        match_parts: list[str] = []
        if author_exact:
            match_parts.append(f"по тексту и username {author_exact}")
        if author_fallbacks:
            match_parts.append(f"по тексту без username {author_fallbacks}")
        elif text_only:
            match_parts.append(f"только по тексту {text_only}")
        if parent_missing:
            match_parts.append(f"главный не найден {parent_missing}")
        if match_parts:
            lines.append(_html("Поиск родителей: " + ", ".join(match_parts)))

    parent_targets_total = _safe_int(coverage.get("parent_targets_total"))
    parent_targets_submitted = _safe_int(coverage.get("parent_targets_submitted"))
    parent_targets_fallback = _safe_int(coverage.get("parent_targets_fallback"))
    if parent_targets_total:
        line = f"Цели ответов: покрыто {_ratio_text(parent_targets_submitted, parent_targets_total)}"
        if parent_targets_fallback:
            line += f", по тексту без username {parent_targets_fallback}"
        lines.append(_html(line))

    error_categories = health.get("error_categories") if isinstance(health.get("error_categories"), dict) else {}
    if error_categories:
        labels = {
            "comment_surface": "UI комментариев",
            "parent_missing": "родитель не найден",
            "stealth_reject": "скрытый reject",
            "network_or_proxy": "сеть/прокси",
            "login_or_cookie": "куки/логин",
            "captcha": "капча",
            "other_errors": "другое",
        }
        parts: list[str] = []
        for key, count in sorted(error_categories.items(), key=lambda item: (-_safe_int(item[1]), str(item[0]))):
            count_int = _safe_int(count)
            if count_int <= 0:
                continue
            parts.append(f"{labels.get(str(key), str(key))}: {count_int}")
            if len(parts) >= 3:
                break
        if parts:
            accounts_with_errors = _safe_int(health.get("accounts_with_errors"))
            prefix = f"Диагностика: аккаунтов с ошибками {accounts_with_errors}; " if accounts_with_errors else "Диагностика: "
            lines.append(_html(prefix + ", ".join(parts)))

    return lines


def _order_likes_detail_lines(task: dict) -> list[str]:
    summary = _get_execution_summary(task)
    likes = summary.get("likes") if isinstance(summary.get("likes"), dict) else {}
    settings_dict = _settings_dict(task)
    settings_enabled = settings_dict.get("boost_likes_enabled")
    targets = likes.get("targets") if isinstance(likes.get("targets"), list) else []
    orders_total = _safe_int(likes.get("orders_total"))
    orders_ok = _safe_int(likes.get("orders_ok"))
    orders_failed = _safe_int(likes.get("orders_failed"))
    requested = bool(likes.get("requested"))

    if not requested and not orders_total:
        return []
    if settings_enabled is False and not orders_total:
        return []

    lines: list[str] = []
    target_mode = likes.get("target_mode") or settings_dict.get("boost_likes_target_mode")
    if target_mode:
        lines.append(f"Режим: {_html(_likes_target_mode_label(target_mode))}")

    quantity = _safe_int(likes.get("quantity") or task.get("likes") or settings_dict.get("likes_count"))
    if orders_total:
        status = f"Заказы лайков: создано {orders_ok}/{orders_total}"
        if orders_failed:
            status += f", ошибок {orders_failed}"
        lines.append(_html(status))
        for item in targets[:5]:
            if not isinstance(item, dict):
                continue
            username = _clean_public_username(item.get("username"))
            if not username:
                continue
            marker = "✅" if item.get("ok") else "❌"
            target_qty = _safe_int(item.get("quantity")) or quantity
            line = f"{marker} @{username}: {target_qty} лайков"
            reply_count = _safe_int(item.get("reply_count"))
            if reply_count:
                line += f", ответов у выбранного комментария {reply_count}"
            reason_label = _likes_selection_reason_label(item.get("selection_reason"))
            if reason_label:
                line += f", {reason_label}"
            error = str(item.get("error") or "").strip()
            if error and not item.get("ok"):
                line += f" - {error}"
            lines.append(_html(line))
            parent_text = str(item.get("parent_text") or "").strip()
            if parent_text:
                lines.append(f"Комментарий: {_html_inline_code(_short_text(parent_text, 90))}")
        if len(targets) > 5:
            lines.append(_html(f"Ещё целей: {len(targets) - 5}"))
    elif requested:
        line = "Boost: включён, но заказов лайков в результате нет"
        error = str(likes.get("error") or "").strip()
        if error:
            line += f" — {error}"
        lines.append(_html(line))

    return lines


def _publication_text_key(value: object) -> str:
    return re.sub(r"\s+", " ", str(value or "").strip().casefold())


def _publication_submitted(item: dict) -> bool:
    visibility_status = str(item.get("visibility_status") or "").strip().lower()
    return bool(
        item.get("success")
        or item.get("submitted_unconfirmed")
        or item.get("reply_id")
        or visibility_status in {"submitted_unconfirmed", "confirmed_local", "network_confirmed"}
    )


def _main_publication_status(variant: dict) -> str:
    if variant.get("network_confirmed") or variant.get("local_confirmed"):
        return "✅ подтверждено"
    return "☑️ отправлено без публичной проверки"


def _main_publication_aliases(variant: dict) -> set[str]:
    text_key = _publication_text_key(variant.get("text"))
    aliases: set[str] = set()
    comment_id = str(variant.get("comment_id") or "").strip()
    if comment_id:
        aliases.add(comment_id)
    if text_key:
        aliases.add(text_key)
    account_id = _safe_int(variant.get("account_id"))
    if account_id and text_key:
        aliases.add(f"account:{account_id}:text:{text_key}")
    username = _clean_public_username(variant.get("tiktok_username") or variant.get("username"))
    username_key = _publication_text_key(username)
    if username_key and text_key:
        aliases.add(f"author:{username_key}:text:{text_key}")
    return aliases


def _reply_publication_aliases(item: dict) -> tuple[list[str], list[str]]:
    strong: list[str] = []
    weak: list[str] = []
    parent_comment_id = str(item.get("parent_comment_id") or "").strip()
    if parent_comment_id:
        strong.append(parent_comment_id)

    parent_key = str(item.get("parent_key") or "").strip()
    if parent_key:
        target = strong if ":" in parent_key or parent_key.isdigit() else weak
        if parent_key not in target:
            target.append(parent_key)

    matched_text_key = _publication_text_key(item.get("matched_main_text"))
    if matched_text_key and matched_text_key not in weak:
        weak.append(matched_text_key)
    return strong, weak


def _build_publication_map_rows(task: dict) -> tuple[list[dict], list[dict], dict[str, int]]:
    settings_dict = _settings_dict(task)
    url_results = settings_dict.get("url_results") if isinstance(settings_dict.get("url_results"), dict) else {}
    is_engagement_only = _order_is_engagement_only(task)
    rows: list[dict] = []
    unassigned_replies: list[dict] = []
    total_expected = 0
    total_submitted_replies = 0

    url_items = list(url_results.items())
    url_total = len(url_items)
    for url_index, (url, result) in enumerate(url_items, 1):
        if not isinstance(result, dict):
            continue
        engagement = result.get("engagement") if isinstance(result.get("engagement"), dict) else {}
        main = engagement.get("main") if isinstance(engagement.get("main"), dict) else {}
        variants = main.get("variants_published") if isinstance(main.get("variants_published"), list) else []
        if not variants and (main.get("posted") or main.get("success") or main.get("submitted_unconfirmed")):
            variants = [main]

        alias_to_rows: dict[str, list[dict]] = {}
        url_rows: list[dict] = []
        replies = engagement.get("replies") if isinstance(engagement.get("replies"), dict) else {}
        reply_items = replies.get("items") if isinstance(replies.get("items"), list) else []

        if is_engagement_only:
            target_text = (
                _order_engagement_target_text(task)
                or str(main.get("text_choice") or "").strip()
                or next(
                    (
                        str(item.get("matched_main_text") or item.get("parent_key") or "").strip()
                        for item in reply_items
                        if isinstance(item, dict)
                        and str(item.get("matched_main_text") or item.get("parent_key") or "").strip()
                    ),
                    "",
                )
                or "комментарий для ответа"
            )
            target_username = _clean_public_username(
                _order_engagement_username(task)
                or next(
                    (
                        str(item.get("matched_parent_author") or item.get("requested_parent_author") or "").strip()
                        for item in reply_items
                        if isinstance(item, dict)
                        and str(item.get("matched_parent_author") or item.get("requested_parent_author") or "").strip()
                    ),
                    "",
                )
            )
            target_comment_id = str(
                result.get("comment_id")
                or main.get("comment_id")
                or next(
                    (
                        str(item.get("parent_comment_id") or "").strip()
                        for item in reply_items
                        if isinstance(item, dict) and str(item.get("parent_comment_id") or "").strip()
                    ),
                    "",
                )
            ).strip()
            submitted_probe = any(isinstance(item, dict) and _publication_submitted(item) for item in reply_items)
            parent_confirmed = bool(target_comment_id or submitted_probe or target_username)
            row = {
                "index": len(rows) + 1,
                "url_index": url_index,
                "url_total": url_total,
                "url": str(url),
                "username": target_username,
                "text": target_text,
                "status": "✅ найден" if parent_confirmed else "☑️ задан для поиска",
                "target_kind": "engagement_parent",
                "requested_author": _clean_public_username(_order_engagement_username(task)),
                "fallback_used": False,
                "replies": [],
                "likes": [],
                "aliases": set(),
            }
            alias_source = {
                "text": target_text,
                "comment_id": target_comment_id,
                "tiktok_username": target_username,
            }
            row["aliases"] = _main_publication_aliases(alias_source)
            for raw_item in reply_items:
                if not isinstance(raw_item, dict):
                    continue
                strong_aliases, weak_aliases = _reply_publication_aliases(raw_item)
                row["aliases"].update(str(alias).strip() for alias in strong_aliases + weak_aliases if str(alias).strip())
            rows.append(row)
            url_rows.append(row)
            for alias in row["aliases"]:
                alias_to_rows.setdefault(alias, []).append(row)
            total_expected += 1
        else:
            total_expected += _safe_int(main.get("expected")) or len(variants)

        for idx, raw_variant in enumerate(variants, 1):
            if is_engagement_only:
                continue
            if not isinstance(raw_variant, dict):
                continue
            username = _clean_public_username(
                raw_variant.get("tiktok_username")
                or raw_variant.get("username")
                or raw_variant.get("author")
            )
            row = {
                "index": len(rows) + 1,
                "url_index": url_index,
                "url_total": url_total,
                "url": str(url),
                "username": username,
                "text": str(raw_variant.get("text") or main.get("text_choice") or task.get("main_comment") or "").strip(),
                "status": _main_publication_status(raw_variant),
                "target_kind": "published_main",
                "requested_author": None,
                "fallback_used": False,
                "replies": [],
                "likes": [],
                "aliases": set(),
            }
            if not row["text"]:
                row["text"] = f"главный комментарий {idx}"
            alias_source = dict(raw_variant)
            if not str(alias_source.get("text") or "").strip():
                alias_source["text"] = row["text"]
            row["aliases"] = _main_publication_aliases(alias_source)
            rows.append(row)
            url_rows.append(row)
            for alias in row["aliases"]:
                alias_to_rows.setdefault(alias, []).append(row)

        for raw_item in reply_items:
            if not isinstance(raw_item, dict) or not _publication_submitted(raw_item):
                continue
            total_submitted_replies += 1
            strong_aliases, weak_aliases = _reply_publication_aliases(raw_item)
            matched_row: dict | None = None
            for alias in strong_aliases:
                candidates = alias_to_rows.get(alias) or []
                if len(candidates) == 1:
                    matched_row = candidates[0]
                    break
            if matched_row is None:
                for alias in weak_aliases:
                    candidates = alias_to_rows.get(alias) or []
                    if len(candidates) == 1:
                        matched_row = candidates[0]
                        break

            reply_row = {
                "text": str(raw_item.get("text") or "").strip(),
                "status": "✅ подтверждено" if raw_item.get("success") else "☑️ отправлено без публичной проверки",
                "requested_author": _clean_public_username(raw_item.get("requested_parent_author")),
                "matched_author": _clean_public_username(raw_item.get("matched_parent_author")),
                "fallback_used": bool(raw_item.get("author_fallback_used")),
                "parent_preview": _short_text(str(raw_item.get("matched_main_text") or raw_item.get("parent_key") or ""), 70),
            }
            if not reply_row["text"]:
                reply_row["text"] = "ответ без сохранённого текста"

            if matched_row is None:
                unassigned_replies.append(reply_row)
                continue

            if not matched_row["username"] and reply_row["matched_author"]:
                matched_row["username"] = reply_row["matched_author"]
            if reply_row["requested_author"] and not matched_row.get("requested_author"):
                matched_row["requested_author"] = reply_row["requested_author"]
            if reply_row["fallback_used"]:
                matched_row["fallback_used"] = True
            matched_row["replies"].append(reply_row)

        likes = engagement.get("likes") if isinstance(engagement.get("likes"), dict) else {}
        like_orders = likes.get("orders") if isinstance(likes.get("orders"), list) else []
        for order in like_orders:
            if not isinstance(order, dict):
                continue
            username = _clean_public_username(order.get("username"))
            if not username:
                continue
            quantity = _safe_int(order.get("quantity")) or _safe_int(likes.get("quantity"))
            like_text = f"{quantity} лайков" if quantity else "лайки"
            like_text += " заказано" if order.get("ok") else " ошибка"
            reply_count = _safe_int(order.get("reply_count"))
            if reply_count:
                like_text += f" ({reply_count} отв.)"
            parent_text_key = _publication_text_key(order.get("parent_text"))
            username_rows = [
                row
                for row in url_rows
                if row.get("username") and row["username"].lower() == username.lower()
            ]
            target_rows = username_rows
            if parent_text_key:
                exact_rows = [
                    row for row in username_rows
                    if _publication_text_key(row.get("text")) == parent_text_key
                ]
                target_rows = exact_rows
            if target_rows:
                target_rows[0]["likes"].append(like_text)

    if total_expected <= 0:
        total_expected = len(rows)
    summary = {
        "main_expected": total_expected,
        "main_submitted": len(rows),
        "reply_submitted": total_submitted_replies,
        "main_with_replies": sum(1 for row in rows if row.get("replies")),
        "unassigned_replies": len(unassigned_replies),
    }
    return rows, unassigned_replies, summary


def _publication_map_summary_lines(task: dict, *, content_page: int = 0, total_pages: int = 1) -> list[str]:
    rows, unassigned, summary = _build_publication_map_rows(task)
    if _order_is_engagement_only(task):
        lines = [
            f"Комментарий для ответа: {'найден/задан' if rows else 'нет данных'}",
            (
                f"Ответы: отправлено {summary['reply_submitted']} "
                f"{_ru_plural(summary['reply_submitted'], 'ответ', 'ответа', 'ответов')}"
            ),
        ]
        if summary["main_with_replies"]:
            lines.append("Есть ответы под найденным комментарием")
    else:
        lines = [
            f"Главные: {_ratio_text(summary['main_submitted'], summary['main_expected'])}",
            (
                f"Ответы: отправлено {summary['reply_submitted']} "
                f"{_ru_plural(summary['reply_submitted'], 'ответ', 'ответа', 'ответов')}"
            ),
            (
                f"С ответами: {summary['main_with_replies']} "
                f"{_ru_plural(summary['main_with_replies'], 'главный', 'главных', 'главных')}"
            ),
        ]
    if unassigned:
        count = len(unassigned)
        lines.append(f"Без точной привязки: {count} {_ru_plural(count, 'ответ', 'ответа', 'ответов')}")
    if total_pages > 1:
        lines.append(f"Страница: {content_page + 1}/{total_pages}")
    if not rows and not unassigned:
        lines.append("Карта появится после отправки комментариев.")
    return lines


def _publication_map_card(row: dict, *, reply_offset: int = 0, replies: list[dict] | None = None) -> str:
    replies = list(replies if replies is not None else row.get("replies") or [])
    username = _clean_public_username(row.get("username")) or "автор неизвестен"
    author_label = f"@{username}" if username != "автор неизвестен" else username
    if row.get("target_kind") == "engagement_parent":
        title = f"{_safe_int(row.get('index'))}. Комментарий для ответа · {author_label} · {row.get('status') or 'найден'}"
    else:
        title = f"{_safe_int(row.get('index'))}. {author_label} · {row.get('status') or 'отправлено'}"
    lines = [f"<b>{_html(title)}</b>", _html_inline_code(row.get("text") or "главный комментарий")]
    details: list[str] = []
    if _safe_int(row.get("url_total")) > 1:
        details.append(f"видео {_safe_int(row.get('url_index'))}/{_safe_int(row.get('url_total'))}")
    if row.get("fallback_used"):
        if row.get("target_kind") == "engagement_parent":
            details.append("username не нашли, ответили по совпадению текста")
        else:
            details.append("ответили по совпадению текста")
    requested_author = _clean_public_username(row.get("requested_author"))
    if requested_author and requested_author.lower() != username.lower():
        details.append(f"искали @{requested_author}")
    if row.get("likes"):
        details.append("Лайки: " + ", ".join(str(item) for item in row["likes"]))
    if details:
        lines.append(_html(" · ".join(details)))

    total_replies = len(row.get("replies") or [])
    if total_replies:
        start = reply_offset + 1
        end = reply_offset + len(replies)
        header = f"Ответы {start}-{end}/{total_replies}" if total_replies > len(replies) else f"Ответы: {total_replies}"
        lines.append(f"<b>{_html(header)}</b>")
        for idx, reply in enumerate(replies, start=start):
            marker = "✅" if str(reply.get("status") or "").startswith("✅") else "☑️"
            lines.append(f"{idx}. {marker} {_html_inline_code(reply.get('text') or 'ответ')}")
    else:
        lines.append("Ответы: нет")
    return "\n".join(lines)


def _publication_map_unassigned_card(replies: list[dict], *, reply_offset: int = 0, total: int | None = None) -> str:
    total = total if total is not None else len(replies)
    start = reply_offset + 1
    end = reply_offset + len(replies)
    title = "Ответы без точной привязки"
    if total > len(replies):
        title += f" {start}-{end}/{total}"
    lines = [
        f"<b>{_html(title)}</b>",
        "Ответ отправлен, но в сохранённых данных нельзя точно определить главный комментарий.",
    ]
    for idx, reply in enumerate(replies, start=start):
        parent = str(reply.get("parent_preview") or "").strip()
        suffix = f" · родитель: {_short_text(parent, 60)}" if parent else ""
        lines.append(f"{idx}. {_html_inline_code(reply.get('text') or 'ответ')}{_html(suffix)}")
    return "\n".join(lines)


def _publication_map_items(task: dict) -> list[str]:
    rows, unassigned, _summary = _build_publication_map_rows(task)
    items: list[str] = []
    for row in rows:
        replies = list(row.get("replies") or [])
        if not replies:
            items.append(_publication_map_card(row))
            continue
        for offset in range(0, len(replies), ORDER_MAP_REPLY_CHUNK_SIZE):
            chunk = replies[offset: offset + ORDER_MAP_REPLY_CHUNK_SIZE]
            items.append(_publication_map_card(row, reply_offset=offset, replies=chunk))
    for offset in range(0, len(unassigned), ORDER_MAP_REPLY_CHUNK_SIZE):
        chunk = unassigned[offset: offset + ORDER_MAP_REPLY_CHUNK_SIZE]
        items.append(_publication_map_unassigned_card(chunk, reply_offset=offset, total=len(unassigned)))
    return items


def _split_text_for_code(text: str) -> list[str]:
    raw = str(text or "").strip()
    if not raw:
        return ["—"]
    if len(_html(raw)) <= ORDER_TEXT_CHUNK_LIMIT:
        return [raw]

    chunks: list[str] = []
    current: list[str] = []
    current_len = 0
    for char in raw:
        char_len = len(_html(char))
        if current and current_len + char_len > ORDER_TEXT_CHUNK_LIMIT:
            chunks.append("".join(current))
            current = [char]
            current_len = char_len
        else:
            current.append(char)
            current_len += char_len
    if current:
        chunks.append("".join(current))
    return chunks or ["—"]


def _content_item(title: str, text: object) -> str:
    return f"<b>{_html(title)}</b>\n{_html_monospace(text)}"


def _paginate_html_items(items: list[str], *, header_len: int = 350, soft_limit: int = ORDER_DETAIL_SOFT_LIMIT) -> list[list[str]]:
    if not items:
        return [[]]

    pages: list[list[str]] = []
    current: list[str] = []
    current_len = header_len
    for item in items:
        item_len = len(item) + 2
        if current and current_len + item_len > soft_limit:
            pages.append(current)
            current = [item]
            current_len = header_len + item_len
        else:
            current.append(item)
            current_len += item_len
    if current:
        pages.append(current)
    return pages or [[]]


def _build_access_denied_text(*, blocked: bool) -> str:
    if blocked:
        return screen(
            "Доступ закрыт",
            "Вы заблокированы администратором.",
            "Чтобы вернуться к работе, обратитесь к владельцу сервиса.",
        )
    return screen(
        "Недостаточно средств",
        "Для работы с заказами сейчас не хватает баланса.",
        "Пополните баланс и затем повторите действие.",
    )


def _origin_label(origin: str) -> str:
    return {
        "manual": "Ручной заказ",
        "composer": "Из конструктора",
        "wizard": "Через мастер",
        "wizard_template": "Мастер + шаблон",
        "quick_order": "Быстрый заказ",
        "autopilot": "Автопилот (мониторинг)",
        "manual_review": "Ручное подтверждение (мониторинг)",
        "new_videos": "Из новых видео (вручную)",
        "monitor_auto_comment": "Авто-коммент (мониторинг)",
        "monitor_manual_review": "Мониторинг → ручной выбор",
        "notification": "Из уведомления",
        "engagement_only": "Только ответы",
        "retry_failed": "Повтор ошибок",
    }.get(origin, origin or "—")


def _format_cost_inline(task: dict) -> str | None:
    total_cost = task.get("total_cost")
    refunded_cost = task.get("refunded_cost")
    final_cost = task.get("final_cost")
    if total_cost is None:
        return None
    total_val = float(total_cost)
    if total_val <= 0:
        return None
    if refunded_cost and float(refunded_cost) > 0:
        final_val = float(final_cost) if final_cost else total_val
        return f"{final_val:.2f} грн, возврат {float(refunded_cost):.2f}"
    if task.get("status") in ("pending", "in_progress"):
        return f"холд {total_val:.2f} грн"
    final_val = float(final_cost) if final_cost else total_val
    return f"{final_val:.2f} грн"


def _build_orders_empty_text() -> str:
    return empty_screen(
        "Заказы",
        "У вас пока нет заказов.",
        "Создайте первый заказ, и он появится здесь вместе со статусом и деталями выполнения.",
    )


async def _ensure_order_access(callback: CallbackQuery) -> tuple[bool, dict | None]:
    """Проверяет доступ пользователя для действий с заказами."""
    try:
        if not callback.message:
            await callback.answer("Ошибка: сообщение не найдено", show_alert=True)
            return False, None

        user_id = callback.from_user.id
        user_data = await api_client.sync_telegram(user_id, callback.from_user.username, use_cache=True)
        user_backend_id = user_data.get("id")
        role = user_data.get("role", "user")
        has_access = await check_user_access(
            telegram_id=user_id,
            user_backend_id=user_backend_id,
            role=role,
            use_cache=True
        )
        if has_access:
            return True, user_data

        is_blocked, _ = await check_user_blocked(user_id, user_backend_id)
        if is_blocked:
            text = _build_access_denied_text(blocked=True)
        else:
            text = _build_access_denied_text(blocked=False)

        await callback.message.edit_text(
            text,
            reply_markup=get_access_denied_keyboard(),
            parse_mode="Markdown"
        )
        try:
            await callback.answer("Доступ запрещён", show_alert=True)
        except Exception:
            pass
        return False, None
    except Exception as e:
        logger.error(f"Error checking order access: {e}", exc_info=True)
        try:
            await callback.answer("Ошибка проверки доступа", show_alert=True)
        except Exception:
            pass
        return False, None


def _get_execution_summary(task: dict) -> dict:
    return build_task_execution_summary(task)


def _get_order_display(task: dict) -> tuple[dict, str, str, str]:
    summary = _get_execution_summary(task)
    display_status = summary.get("display_status") or task.get("status", "unknown")
    status_label = summary.get("status_label") or task.get("status", "Неизвестно")
    icon = {
        "pending": Emoji.PENDING,
        "in_progress": Emoji.IN_PROGRESS,
        "done": Emoji.DONE,
        "done_warning": Emoji.WARNING,
        "failed": Emoji.ERROR,
        "partial": Emoji.PARTIAL,
        "stopped": Emoji.STOPPED,
    }.get(display_status, "❓")
    return summary, display_status, status_label, icon


def format_order_line(task: dict) -> str:
    """Форматирует строку заказа для компактного списка."""
    task_id = task.get("id")
    summary, display_status, status_label, icon = _get_order_display(task)
    progress = task.get("progress", 0)
    total_urls = len(task.get("video_urls", []))
    created_at = task.get("created_at", "")
    # ИСПРАВЛЕНО: показываем дружелюбную дату ("сегодня 09:30", "вчера 22:15",
    # "12.05 18:00") вместо одного только HH:MM, иначе порядок выглядит
    # хаотично, когда в списке заказы из разных дней.
    time_str = format_relative_datetime(created_at)
    variants = _order_text_variants(task)
    replies = _order_replies(task)
    is_engagement_only = _order_is_engagement_only(task)
    if is_engagement_only and replies:
        preview_source = replies[0]
    elif is_engagement_only:
        preview_source = _order_engagement_target_text(task)
    else:
        preview_source = variants[0] if variants else task.get("main_comment")
    preview = _short_text(preview_source, 80)
    progress_text = format_progress(progress, total_urls) if total_urls > 0 else "—"
    first_line = f"{icon} <b>#{_html(task_id)}</b> · {_html(status_label)} · {_html_inline_code(progress_text)}"
    meta_parts: list[str] = []
    creator_username = _order_creator_username(task)
    if creator_username:
        meta_parts.append(_html_inline_code(f"@{creator_username}"))
    if total_urls:
        meta_parts.append(f"{total_urls} видео")
    if is_engagement_only:
        meta_parts.append(f"только ответы · {len(replies)} отв.")
    else:
        meta_parts.append(f"{len(variants)} комм. · {len(replies)} отв.")
    if time_str:
        meta_parts.append(_html(time_str))
    warnings_count = len(collect_task_warnings(task, short=True))
    if warnings_count:
        meta_parts.append(f"⚠️ {warnings_count}")
    preview_line = f"   {_html_inline_code(preview or 'без текста')}"
    return first_line + "\n" + "   " + " · ".join(meta_parts) + "\n" + preview_line


def _format_orders_header(tasks: list[dict], filtered_tasks: list[dict], filter_name: str, page: int, total_pages: int) -> str:
    counts = _order_counts(tasks)
    filter_label = ORDER_FILTERS[_normalize_order_filter(filter_name)]
    return (
        "<b>Заказы</b>  <i>· новые сверху</i>\n"
        f"Активные: <b>{counts['active']}</b> · В работе: <b>{counts['running']}</b> · Ошибки: <b>{counts['failed']}</b>\n"
        f"Остановлены: <b>{counts['stopped']}</b> · Готовые: <b>{counts['done']}</b>\n"
        f"{_html(filter_label)} · {len(filtered_tasks)} из {len(tasks)} · стр. <b>{page + 1}/{total_pages}</b>"
    )


def _orders_filter_keyboard(filter_name: str) -> list[list[InlineKeyboardButton]]:
    filter_name = _normalize_order_filter(filter_name)
    icons = {"all": "⏳", "active": "🔵", "running": "🔄", "failed": "🔴", "stopped": "⏹", "done": "🟢"}
    buttons = [
        InlineKeyboardButton(
            text=f"{'• ' if key == filter_name else ''}{icons[key]} {label}",
            callback_data=_orders_page_callback(key, 0),
        )
        for key, label in ORDER_FILTERS.items()
    ]
    return [buttons[:2], buttons[2:4], buttons[4:]]


def _build_orders_keyboard(
    page_tasks: list[dict],
    page: int,
    total_pages: int,
    filter_name: str,
    all_tasks: list[dict] | None = None,
    has_pending_tasks: bool | None = None,
) -> InlineKeyboardMarkup:
    keyboard = _orders_filter_keyboard(filter_name)
    order_buttons: list[InlineKeyboardButton] = []
    for task in page_tasks:
        task_id = int(task.get("id"))
        _, _, _, icon = _get_order_display(task)
        order_buttons.append(InlineKeyboardButton(
            text=f"{icon} #{task_id}",
            callback_data=_detail_callback(task_id, "overview", page, filter_name),
        ))
    for idx in range(0, len(order_buttons), 2):
        keyboard.append(order_buttons[idx:idx + 2])

    if total_pages > 1:
        page_row = []
        if page > 0:
            page_row.append(InlineKeyboardButton(text="◀️", callback_data=_orders_page_callback(filter_name, page - 1)))
        page_row.append(InlineKeyboardButton(text=f"{page + 1}/{total_pages}", callback_data="orders:noop"))
        if page < total_pages - 1:
            page_row.append(InlineKeyboardButton(text="▶️", callback_data=_orders_page_callback(filter_name, page + 1)))
        keyboard.append(page_row)

    keyboard.append([
        InlineKeyboardButton(text="🔄 Обновить", callback_data=_orders_page_callback(filter_name, page)),
        InlineKeyboardButton(text="➕ Новый заказ", callback_data="menu:new_order"),
    ])
    if has_pending_tasks is None:
        has_pending_tasks = any(_order_is_pending(task) for task in (all_tasks or page_tasks))
    if has_pending_tasks:
        keyboard.append([
            InlineKeyboardButton(
                text="🧹 Отменить очередь",
                callback_data=_cancel_pending_callback("ask", filter_name, page),
            )
        ])
    keyboard.append([InlineKeyboardButton(text="🏠 Меню", callback_data="menu:main")])
    return InlineKeyboardMarkup(inline_keyboard=keyboard)


def _render_orders_list_text(tasks: list[dict], filtered_tasks: list[dict], page_tasks: list[dict], filter_name: str, page: int, total_pages: int) -> str:
    text = _format_orders_header(tasks, filtered_tasks, filter_name, page, total_pages) + "\n\n"
    if not page_tasks:
        return text + _html_block("Список", ["В этом фильтре заказов нет."])
    for idx, task in enumerate(page_tasks):
        if idx:
            text += "\n\n"
        text += format_order_line(task) + "\n"
    return text.strip()


def _format_cost_lines(task: dict) -> list[str]:
    total_cost = task.get("total_cost")
    refunded_cost = task.get("refunded_cost")
    final_cost = task.get("final_cost")
    if total_cost is None:
        return ["Нет данных по стоимости."]
    total_val = float(total_cost)
    if total_val <= 0:
        return ["Бесплатно или демо-заказ."]
    lines = [f"Списано/холд: {total_val:.2f} грн"]
    if refunded_cost and float(refunded_cost) > 0:
        lines.append(f"Возврат: {float(refunded_cost):.2f} грн")
    final_val = float(final_cost) if final_cost else total_val
    lines.append(f"Итого: {final_val:.2f} грн")
    return lines


def _order_eta_line(task: dict) -> str | None:
    status = task.get("status")
    progress = int(task.get("progress") or 0)
    total_urls = len(task.get("video_urls", []))
    if status != "in_progress" or progress <= 0 or total_urls <= 0 or progress >= total_urls:
        return None
    remaining = total_urls - progress
    estimated_minutes = max(1, int(remaining * 2.5))
    if estimated_minutes < 60:
        return f"Осталось примерно: {estimated_minutes} мин"
    return f"Осталось примерно: {estimated_minutes // 60} ч {estimated_minutes % 60} мин"


def _order_replies(task: dict) -> list[str]:
    settings_dict = task.get("settings") if isinstance(task.get("settings"), dict) else {}
    if isinstance(settings_dict.get("replies"), list):
        replies = settings_dict.get("replies", [])
    else:
        replies = [task.get("reply1"), task.get("reply2"), task.get("reply3")]
        replies.extend(settings_dict.get(f"reply{idx}") for idx in range(4, settings.MAX_REPLIES + 1))
    normalized = [str(reply) for reply in replies if str(reply or "").strip()]
    reply_count = settings_dict.get("reply_count")
    if isinstance(reply_count, int) and reply_count >= 0:
        normalized = normalized[:reply_count]
    return normalized[: settings.MAX_REPLIES]


def _order_engagement_target_text(task: dict) -> str | None:
    settings_dict = _settings_dict(task)
    target = normalize_user_text(
        settings_dict.get("engagement_comment_text")
        or task.get("engagement_comment_text")
        or task.get("main_comment")
    )
    return target or None


def _order_engagement_username(task: dict) -> str | None:
    settings_dict = _settings_dict(task)
    username = normalize_user_text(
        settings_dict.get("engagement_username")
        or task.get("engagement_username")
    )
    return username.strip().lstrip("@") if username else None


def _order_text_variants(task: dict) -> list[str]:
    if _order_is_engagement_only(task):
        return []
    settings_dict = _settings_dict(task)
    variants = [
        str(item)
        for item in (settings_dict.get("text_variants") or [])
        if str(item or "").strip()
    ]
    if variants:
        return variants
    main_comment = normalize_user_text(task.get("main_comment"))
    return [main_comment] if main_comment else []


def _overview_status_lines(task: dict, *, content_page: int = 0, total_pages: int = 1) -> list[str]:
    summary, _display_status, status_text, icon = _get_order_display(task)
    video_info = summary.get("videos") or {}
    progress = int(task.get("progress") or 0)
    total_urls = len(task.get("video_urls", []))
    created_at = task.get("created_at", "")
    created_line = format_date(created_at, "%d.%m %H:%M") if created_at else "—"
    video_line = (
        f"Видео: {video_info.get('success', 0)} ок / {video_info.get('failed', 0)} ошибок"
        + (f" / {video_info.get('with_warnings', 0)} предупреждений" if video_info.get("with_warnings", 0) else "")
    )
    lines = [
        f"{icon} {_html(status_text)}",
        _html(video_line),
        f"Прогресс: {_html_inline_code(format_progress(progress, total_urls) if total_urls else '—')} · создан {_html(created_line)}",
    ]
    if _order_is_pending(task) and _order_queue_priority(task) == "high":
        lines.append("Приоритет: высокий")
    eta = _order_eta_line(task)
    if eta:
        lines.append(_html(eta))
    if total_pages > 1:
        lines.append(f"Страница: {content_page + 1}/{total_pages}")
    return lines


def _overview_context_lines(task: dict) -> list[str]:
    lines = [f"Источник: {_html(_origin_label(_order_origin(task)))}"]
    creator_username = _order_creator_username(task)
    if creator_username:
        lines.append(f"Блогер: {_html_inline_code('@' + creator_username)}")
    template_label = _order_template_label(task)
    if template_label:
        lines.append(f"Шаблон: {_html(template_label)}")
    target_username = _order_engagement_username(task)
    if _order_is_engagement_only(task) and target_username:
        lines.append(f"Автор поиска: {_html_inline_code('@' + target_username)}")
    return lines


def _overview_order_plan_lines(task: dict) -> list[str]:
    summary = _get_execution_summary(task)
    variants = _order_text_variants(task)
    replies = _order_replies(task)
    is_engagement_only = _order_is_engagement_only(task)
    main = summary.get("main") if isinstance(summary.get("main"), dict) else {}
    replies_summary = summary.get("replies") if isinstance(summary.get("replies"), dict) else {}
    lines: list[str] = []

    if is_engagement_only:
        lines.append("Тип: только ответы")
        reply_line = f"Ответы: {len(replies)} {_ru_plural(len(replies), 'текст', 'текста', 'текстов')}"
        replies_requested = _safe_int(replies_summary.get("requested"))
        replies_submitted = _safe_int(replies_summary.get("submitted"))
        if replies_requested:
            reply_line += f" · план отправок {replies_requested}"
        if replies_requested or replies_submitted:
            reply_line += f" · отправлено {_ratio_text(replies_submitted, replies_requested)}"
        lines.append(reply_line)
        lines.append(f"Лайки: {_html(_order_likes_label(task))}")
    else:
        main_expected = _safe_int(main.get("expected"))
        main_submitted = _safe_int(main.get("submitted"))
        main_label = "Тексты главных" if main_expected > len(variants) else "Главные комментарии"
        main_line = f"{main_label}: {len(variants)}"
        if main_expected:
            main_line += f" · план {main_expected}"
        if main_expected or main_submitted:
            main_line += f" · опубликовано {_ratio_text(main_submitted, main_expected)}"
        reply_line = f"Ответы: {len(replies)} {_ru_plural(len(replies), 'текст', 'текста', 'текстов')}"
        replies_requested = _safe_int(replies_summary.get("requested"))
        replies_submitted = _safe_int(replies_summary.get("submitted"))
        if replies_requested:
            reply_line += f" · план отправок {replies_requested}"
        if replies_requested or replies_submitted:
            reply_line += f" · отправлено {_ratio_text(replies_submitted, replies_requested)}"
        lines.append(main_line)
        lines.append(reply_line)
        lines.append(f"Лайки: {_html(_order_likes_label(task))}")
    return lines


def _overview_notice_lines(task: dict) -> list[str]:
    _summary, display_status, _status_text, _icon = _get_order_display(task)
    warnings = collect_task_warnings(task, short=True)
    display_reason = build_task_display_reason(task, short=True)
    warning_line = warnings[0] if warnings else None
    show_reason = display_status in {"failed", "partial", "stopped"} and display_reason
    if show_reason and warning_line and display_reason == warning_line:
        show_reason = False

    lines: list[str] = []
    if show_reason:
        lines.append(f"Причина: {_html(display_reason)}")
    if warning_line:
        lines.append(f"Важно: {_html(warning_line)}")
    return lines


def _overview_header_lines(task: dict, *, content_page: int = 0, total_pages: int = 1) -> list[str]:
    lines = _overview_status_lines(task, content_page=content_page, total_pages=total_pages)
    lines.extend(_overview_order_plan_lines(task))
    lines.extend(_overview_context_lines(task))
    lines.extend(_overview_notice_lines(task))
    return lines


def _overview_header_blocks(task: dict, *, content_page: int = 0, total_pages: int = 1) -> list[str]:
    context_lines = _overview_context_lines(task)
    blocks = [
        _html_block("Статус", _overview_status_lines(task, content_page=content_page, total_pages=total_pages)),
        _html_block("Состав заказа", _overview_order_plan_lines(task)),
    ]
    if context_lines:
        blocks.append(_html_block("Контекст", context_lines))
    notice_lines = _overview_notice_lines(task)
    if notice_lines:
        blocks.append(_html_block("Требует внимания", notice_lines))
    return blocks


def _overview_content_items(task: dict) -> list[str]:
    variants = _order_text_variants(task)
    replies = _order_replies(task)
    is_engagement_only = _order_is_engagement_only(task)
    items: list[str] = []

    publication_lines = _order_publication_summary_lines(task)
    if publication_lines:
        items.append(_html_block("Итоги публикации", publication_lines))

    target_lines = _order_parent_target_detail_lines(task)
    if target_lines:
        items.append(_html_block("Главные для ответов", target_lines))

    likes_lines = _order_likes_detail_lines(task)
    if likes_lines:
        items.append(_html_block("Лайки Boost", likes_lines))

    first_video = (task.get("video_urls") or [None])[0]
    if first_video:
        items.append(_html_block("Видео", [f"Первая ссылка: {_html_link(first_video)}"]))

    first_comment = _order_engagement_target_text(task) if is_engagement_only else (variants[0] if variants else task.get("main_comment"))
    if first_comment:
        comment_chunks = _split_text_for_code(first_comment)
        for chunk_idx, chunk in enumerate(comment_chunks, 1):
            suffix = f" · часть {chunk_idx}/{len(comment_chunks)}" if len(comment_chunks) > 1 else ""
            title = "Комментарий для поиска" if is_engagement_only else "Первый комментарий"
            items.append(_content_item(f"{title}{suffix}", chunk))
    if replies:
        reply_chunks = _split_text_for_code(replies[0])
        for chunk_idx, chunk in enumerate(reply_chunks, 1):
            suffix = f" · часть {chunk_idx}/{len(reply_chunks)}" if len(reply_chunks) > 1 else ""
            items.append(_content_item(f"Первый ответ{suffix}", chunk))

    cost_lines = _format_cost_lines(task)
    if task.get("total_cost") is not None and float(task.get("total_cost") or 0) > 0:
        items.append(_html_block("Финансы", [_html(line) for line in cost_lines]))
    return items


def _overview_pages(task: dict) -> list[list[str]]:
    title_len = len(str(task.get("id") or "")) + 40
    preliminary_header = "\n\n".join(_overview_header_blocks(task))
    return _paginate_html_items(
        _overview_content_items(task),
        header_len=title_len + len(preliminary_header) + 4,
    )


def _render_order_overview(task: dict, content_page: int = 0) -> str:
    task_id = task.get("id")
    pages = _overview_pages(task)
    content_page = max(0, min(int(content_page or 0), len(pages) - 1))
    header = "\n\n".join(_overview_header_blocks(task, content_page=content_page, total_pages=len(pages)))
    page_items = pages[content_page]

    return _html_screen(
        f"Заказ #{task_id}",
        header,
        "\n\n".join(page_items),
    )


def _video_content_items(task: dict) -> list[str]:
    settings_dict = _settings_dict(task)
    url_results = settings_dict.get("url_results") or {}
    video_urls = task.get("video_urls") or []
    items: list[str] = []
    progress = int(task.get("progress") or 0)
    for idx, url in enumerate(video_urls, 1):
        result = url_results.get(url, {}) if isinstance(url_results, dict) else {}
        result_status = str(result.get("status") or "").lower()
        if result_status == "success":
            icon = "✅"
            label = "опубликовано"
        elif result_status in {"submitted_unconfirmed"}:
            icon = "☑️"
            label = "отправлено, проверка не подтверждена"
        elif result_status in {"fail", "failed", "error"}:
            icon = "❌"
            label = humanize_error_text(result.get("error"), short=True) or "ошибка"
        elif idx <= progress:
            icon = "⚠️"
            label = "обработано без деталей"
        else:
            icon = "⏳"
            label = "ждёт"

        lines = [f"{icon} <b>{idx}. {_html(label)}</b>", _html_link(url)]
        if result.get("warnings"):
            warning = humanize_warning((result.get("warnings") or [None])[0], short=True)
            if warning:
                lines.append(f"Важно: {_html(warning)}")
        engagement = result.get("engagement") if isinstance(result, dict) else None
        if isinstance(engagement, dict):
            main = engagement.get("main") if isinstance(engagement.get("main"), dict) else {}
            variants_published = main.get("variants_published") if isinstance(main.get("variants_published"), list) else []
            main_submitted = len(variants_published) or (1 if main.get("posted") else 0)
            main_expected = _safe_int(main.get("expected"))
            if main_submitted or main_expected:
                lines.append(f"Главные: {_html(_ratio_text(main_submitted, main_expected))}")
            replies_info = engagement.get("replies") if isinstance(engagement.get("replies"), dict) else {}
            if replies_info:
                submitted = _safe_int(replies_info.get("submitted")) or _safe_int(replies_info.get("ok"))
                requested = (
                    _safe_int(replies_info.get("expected_submissions"))
                    or _safe_int(replies_info.get("expected"))
                    or _safe_int(replies_info.get("requested_variants"))
                    or _safe_int(replies_info.get("total"))
                )
                not_started = _safe_int(replies_info.get("variants_not_started"))
                if submitted or requested:
                    reply_line = f"Ответы: {_ratio_text(submitted, requested)}"
                    if not_started:
                        reply_line += f" · не начато {not_started}"
                    lines.append(_html(reply_line))
            likes_info = engagement.get("likes") if isinstance(engagement.get("likes"), dict) else {}
            like_orders = likes_info.get("orders") if isinstance(likes_info.get("orders"), list) else []
            if like_orders:
                target_parts: list[str] = []
                ok_count = sum(1 for order in like_orders if isinstance(order, dict) and order.get("ok"))
                for order in like_orders:
                    if not isinstance(order, dict):
                        continue
                    username = _clean_public_username(order.get("username"))
                    if not username:
                        continue
                    qty = _safe_int(order.get("quantity")) or _safe_int(likes_info.get("quantity"))
                    replies_count = _safe_int(order.get("reply_count"))
                    part = f"@{_html(username)}"
                    if qty:
                        part += f" {qty}"
                    if replies_count:
                        part += f" ({replies_count} отв.)"
                    target_parts.append(part)
                if target_parts:
                    visible_targets = ", ".join(target_parts[:3])
                    suffix = f" +{len(target_parts) - 3}" if len(target_parts) > 3 else ""
                    lines.append(f"Лайки: {ok_count}/{len(like_orders)} · {visible_targets}{_html(suffix)}")
        items.append("\n".join(lines))
    return items


def _render_order_videos(task: dict, content_page: int = 0) -> str:
    task_id = task.get("id")
    video_urls = task.get("video_urls") or []
    items = _video_content_items(task)
    pages = _paginate_html_items(items)
    content_page = max(0, min(int(content_page or 0), len(pages) - 1))
    summary = [f"Всего видео: {len(video_urls)}"]
    if len(pages) > 1:
        summary.append(f"Страница: {content_page + 1}/{len(pages)}")
    body = "\n\n".join(pages[content_page]) if items else "Видео не указаны."
    return _html_screen(
        f"Заказ #{task_id} · Видео",
        _html_block("Сводка", summary),
        body,
    )


def _text_content_items(task: dict) -> list[str]:
    variants = _order_text_variants(task)
    replies = _order_replies(task)
    is_engagement_only = _order_is_engagement_only(task)
    items: list[str] = []

    if is_engagement_only:
        target_text = _order_engagement_target_text(task)
        if target_text:
            chunks = _split_text_for_code(target_text)
            for chunk_idx, chunk in enumerate(chunks, 1):
                suffix = f" · часть {chunk_idx}/{len(chunks)}" if len(chunks) > 1 else ""
                items.append(_content_item(f"Комментарий для поиска{suffix}", chunk))

    for idx, text in enumerate(variants, 1):
        chunks = _split_text_for_code(text)
        for chunk_idx, chunk in enumerate(chunks, 1):
            suffix = f" · часть {chunk_idx}/{len(chunks)}" if len(chunks) > 1 else ""
            items.append(_content_item(f"Комментарий {idx}{suffix}", chunk))

    for idx, text in enumerate(replies, 1):
        chunks = _split_text_for_code(text)
        for chunk_idx, chunk in enumerate(chunks, 1):
            suffix = f" · часть {chunk_idx}/{len(chunks)}" if len(chunks) > 1 else ""
            items.append(_content_item(f"Ответ {idx}{suffix}", chunk))

    return items


def _render_order_texts(task: dict, content_page: int = 0) -> str:
    task_id = task.get("id")
    variants = _order_text_variants(task)
    replies = _order_replies(task)
    items = _text_content_items(task)
    pages = _paginate_html_items(items)
    content_page = max(0, min(int(content_page or 0), len(pages) - 1))

    if _order_is_engagement_only(task):
        summary = ["Тип: только ответы", f"Ответы: {len(replies)}"]
    else:
        summary = [f"Тексты главных: {len(variants)}", f"Ответы: {len(replies)}"]
    if len(pages) > 1:
        summary.append(f"Страница: {content_page + 1}/{len(pages)}")
    body = "\n\n".join(pages[content_page]) if items else "Нет сохранённых комментариев и ответов."
    return _html_screen(
        f"Заказ #{task_id} · Тексты",
        _html_block("Сводка", summary),
        body,
    )


def _render_order_map(task: dict, content_page: int = 0) -> str:
    task_id = task.get("id")
    items = _publication_map_items(task)
    pages = _paginate_html_items(items)
    content_page = max(0, min(int(content_page or 0), len(pages) - 1))
    summary = _publication_map_summary_lines(task, content_page=content_page, total_pages=len(pages))
    body = "\n\n".join(pages[content_page]) if items else "Публикаций для карты пока нет."
    return _html_screen(
        f"Заказ #{task_id} · Публикации",
        _html_block("Сводка", summary),
        body,
    )


def _render_order_settings(task: dict) -> str:
    task_id = task.get("id")
    settings_dict = _settings_dict(task)
    origin = _order_origin(task)
    account_pool_mode = _normalize_account_pool_mode(settings_dict.get("account_pool_mode"))

    lines = [
        f"Источник: {_html(_origin_label(origin))}",
        f"Пул: {_html(_account_pool_label(account_pool_mode))}",
    ]
    if _order_is_pending(task):
        priority_label = {
            "high": "высокий",
            "normal": "обычный",
            "low": "низкий",
        }.get(_order_queue_priority(task), "обычный")
        lines.append(f"Приоритет очереди: {_html(priority_label)}")
    if _order_is_engagement_only(task):
        lines.append("Тип: только ответы")

    # Привязка к шаблону: показываем только если заказ реально создан из
    # шаблона (template_id != None). template_name заполняется при создании
    # из шаблона в wizard / quick_order / video_parsing_new_videos / monitor.
    template_label = _order_template_label(task)
    if template_label:
        lines.append(f"Шаблон: {_html(template_label)}")

    # Для monitor auto-comment показываем блогера-источника видео.
    creator_username = _order_creator_username(task)
    if creator_username:
        lines.append(f"Блогер: {_html_inline_code('@' + creator_username)}")

    lines.extend([
        f"Задержки: {'вкл' if settings_dict.get('delays', True) else 'выкл'}",
        f"Перемешивание: {'вкл' if settings_dict.get('shuffle', False) else 'выкл'}",
        f"Ротация аккаунтов: {'вкл' if settings_dict.get('rotate_accounts', True) else 'выкл'}",
        f"ID заказа: {_html_inline_code(task_id)}",
    ])
    return _html_screen(f"Заказ #{task_id} · Детали", _html_block("Параметры", lines))


def _render_order_detail(task: dict, tab: str, content_page: int = 0) -> str:
    tab = _normalize_order_tab(tab)
    if tab == "videos":
        return _render_order_videos(task, content_page)
    if tab == "map":
        return _render_order_map(task, content_page)
    if tab == "texts":
        return _render_order_texts(task, content_page)
    if tab == "settings":
        return _render_order_settings(task)
    return _render_order_overview(task, content_page)


def _order_detail_page_count(task: dict, tab: str) -> int:
    tab = _normalize_order_tab(tab)
    if tab == "overview":
        return len(_overview_pages(task))
    if tab == "videos":
        return len(_paginate_html_items(_video_content_items(task)))
    if tab == "map":
        return len(_paginate_html_items(_publication_map_items(task)))
    if tab == "texts":
        return len(_paginate_html_items(_text_content_items(task)))
    return 1


def _build_order_detail_keyboard(
    task: dict,
    tab: str,
    page: int,
    filter_name: str,
    content_page: int = 0,
) -> InlineKeyboardMarkup:
    task_id = int(task.get("id"))
    tab = _normalize_order_tab(tab)
    tab_buttons = [
        InlineKeyboardButton(text=("• " if tab == key else "") + label, callback_data=_detail_callback(task_id, key, page, filter_name))
        for key, label in ORDER_TABS.items()
    ]
    keyboard = [tab_buttons[:2], tab_buttons[2:]]
    total_content_pages = _order_detail_page_count(task, tab)
    content_page = max(0, min(int(content_page or 0), total_content_pages - 1))
    if total_content_pages > 1:
        page_row: list[InlineKeyboardButton] = []
        if content_page > 0:
            page_row.append(
                InlineKeyboardButton(
                    text="◀️",
                    callback_data=_detail_callback(task_id, tab, page, filter_name, content_page - 1),
                )
            )
        page_row.append(InlineKeyboardButton(text=f"{content_page + 1}/{total_content_pages}", callback_data="orders:noop"))
        if content_page < total_content_pages - 1:
            page_row.append(
                InlineKeyboardButton(
                    text="▶️",
                    callback_data=_detail_callback(task_id, tab, page, filter_name, content_page + 1),
                )
            )
        keyboard.append(page_row)

    is_stopped = _order_is_stopped(task)
    if _order_is_stoppable(task):
        keyboard.append([
            InlineKeyboardButton(
                text="⏹ Остановить заказ",
                callback_data=_stop_order_callback("stop_prompt", task_id, tab, page, filter_name, content_page),
            )
        ])

    failed_urls = _failed_urls_for_retry(task)
    if failed_urls and not is_stopped:
        keyboard.append([InlineKeyboardButton(text=f"🔄 Повторить ошибки ({len(failed_urls)})", callback_data=f"order:retry_failed:{task_id}")])
    if _order_is_pending(task):
        pending_row: list[InlineKeyboardButton] = []
        if _order_queue_priority(task) != "high":
            pending_row.append(
                InlineKeyboardButton(
                    text="⚡ Приоритет",
                    callback_data=_priority_order_callback(task_id, tab, page, filter_name, content_page),
                )
            )
        pending_row.append(InlineKeyboardButton(text="📝 Заменить тексты", callback_data=f"order:texts:{task_id}"))
        keyboard.append(pending_row)
    if is_stopped:
        keyboard.append([InlineKeyboardButton(text="📋 Сделать копию", callback_data=f"order:copy:{task_id}")])
    else:
        keyboard.append([
            InlineKeyboardButton(text="📋 Сделать копию", callback_data=f"order:copy:{task_id}"),
            InlineKeyboardButton(text="🔁 Новый заказ", callback_data="menu:new_order"),
        ])
    if _order_origin(task) in {"new_videos", "monitor_auto_comment", "monitor_manual_review"}:
        keyboard.append([InlineKeyboardButton(text="📥 Входящие", callback_data="vp:inbox")])
    keyboard.append([
        InlineKeyboardButton(text="◀️ К списку", callback_data=_orders_page_callback(filter_name, page)),
        InlineKeyboardButton(text="🏠 Меню", callback_data="menu:main"),
    ])
    return InlineKeyboardMarkup(inline_keyboard=keyboard)


@router.callback_query(F.data == "menu:orders")
async def list_orders(callback: CallbackQuery, state: FSMContext):
    """Показывает список заказов пользователя (первая страница)."""
    await show_orders_page(callback, state, page=0, filter_name="all")


@router.callback_query(F.data.startswith("orders:page:"))
@router.callback_query(F.data.startswith("orders:filter:"))
async def show_orders_page_handler(callback: CallbackQuery, state: FSMContext):
    """Обрабатывает переход на страницу заказов."""
    try:
        filter_name, page = _parse_orders_page_callback(callback.data)
        await show_orders_page(callback, state, page=page, filter_name=filter_name)
    except (ValueError, IndexError):
        await callback.answer("Ошибка пагинации", show_alert=True)


async def show_orders_page(
    callback: CallbackQuery,
    state: FSMContext,
    page: int = 0,
    filter_name: str = "all",
    *,
    answer_callback: bool = True,
):
    """Показывает страницу заказов с пагинацией."""
    try:
        if not callback.message:
            await callback.answer("Ошибка: сообщение не найдено", show_alert=True)
            return
        
        if answer_callback:
            await callback.answer()
        user_id = callback.from_user.id
        
        user_backend_id = await get_user_backend_id(user_id)
        if not user_backend_id:
            logger.error(f"[ORDERS] User backend ID not found for telegram_id={user_id}")
            await callback.message.edit_text(
                "Пользователь не найден. Попробуйте перезапустить бота командой /start.",
                reply_markup=InlineKeyboardMarkup(inline_keyboard=[[
                    InlineKeyboardButton(text="🏠 Меню", callback_data="menu:main")
                ]])
            )
            return

        logger.debug(f"[ORDERS] Fetching tasks for user_backend_id={user_backend_id}, telegram_id={user_id}")
        tasks = await api_client.list_tasks(user_id=user_backend_id, limit=ORDERS_FETCH_LIMIT, offset=0)
        # ИСПРАВЛЕНО: защитный sort на стороне бота (новые сверху). Если в БД
        # вдруг окажутся задачи с равным created_at, без вторичного критерия
        # они придут в недетерминированном порядке. Сортируем по (created_at, id)
        # убыванию: created_at — основной ключ, id — для разрыва ничьих.
        tasks = sorted(
            tasks or [],
            key=lambda t: (str(t.get("created_at") or ""), int(t.get("id") or 0)),
            reverse=True,
        )

        logger.debug(f"[ORDERS] Received {len(tasks) if tasks else 0} tasks for user_backend_id={user_backend_id}")
        has_pending_tasks = any(_order_is_pending(task) for task in (tasks or []))
        if not has_pending_tasks:
            pending_probe = await api_client.list_tasks(
                user_id=user_backend_id,
                status="pending",
                limit=1,
                offset=0,
            )
            has_pending_tasks = bool(pending_probe)

        if not tasks:
            try:
                await callback.message.edit_text(
                    _build_orders_empty_text(),
                    reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                        [
                            InlineKeyboardButton(text="🆕 Создать заказ", callback_data="menu:new_order"),
                            InlineKeyboardButton(text=f"{Emoji.HOME} Меню", callback_data="menu:main")
                        ]
                    ]),
                    parse_mode="Markdown"
                )
            except TelegramBadRequest as e:
                if "message is not modified" in str(e).lower():
                    pass
                else:
                    raise
            return

        filter_name = _normalize_order_filter(filter_name)
        filtered_tasks = [task for task in tasks if _task_matches_filter(task, filter_name)]
        total_tasks = len(filtered_tasks)
        total_pages = max(1, (total_tasks + ORDERS_PER_PAGE - 1) // ORDERS_PER_PAGE)
        page = max(0, min(page, total_pages - 1))  # Ограничиваем страницу
        
        start_idx = page * ORDERS_PER_PAGE
        end_idx = min(start_idx + ORDERS_PER_PAGE, total_tasks)
        page_tasks = filtered_tasks[start_idx:end_idx]
        text = _render_orders_list_text(tasks, filtered_tasks, page_tasks, filter_name, page, total_pages)
        keyboard = _build_orders_keyboard(
            page_tasks,
            page,
            total_pages,
            filter_name,
            tasks,
            has_pending_tasks=has_pending_tasks,
        )

        try:
            await callback.message.edit_text(
                text,
                reply_markup=keyboard,
                parse_mode="HTML"
            )
        except TelegramBadRequest as e:
            # Сообщение не изменилось - это нормально, игнорируем
            if "message is not modified" in str(e).lower():
                await callback.answer("Заказы без изменений", show_alert=False)
            else:
                raise

    except Exception as e:
        logger.error(f"Error listing orders: {e}", exc_info=True)
        await callback.answer("Ошибка загрузки заказов", show_alert=True)


@router.callback_query(F.data == "orders:noop")
async def orders_noop(callback: CallbackQuery):
    """Заглушка для кнопки номера страницы."""
    await callback.answer()


@router.callback_query(F.data.startswith("orders:cancel_pending:"))
async def cancel_pending_orders(callback: CallbackQuery, state: FSMContext):
    """Подтверждает или выполняет отмену всех pending-заказов пользователя."""
    try:
        if not callback.message:
            await callback.answer("Ошибка: сообщение не найдено", show_alert=True)
            return

        action, filter_name, page = _parse_cancel_pending_callback(callback.data)
        user_backend_id = await get_user_backend_id(callback.from_user.id)
        if not user_backend_id:
            await callback.answer("Пользователь не найден", show_alert=True)
            return

        if action != "do":
            pending_probe = await api_client.list_tasks(
                user_id=user_backend_id,
                status="pending",
                limit=1,
                offset=0,
            )
            if not pending_probe:
                await callback.answer("В очереди нет заказов", show_alert=True)
                await show_orders_page(callback, state, page=page, filter_name=filter_name, answer_callback=False)
                return

            text = _html_screen(
                "Отменить очередь",
                _html_block("Подтверждение", [
                    "Будут отменены все заказы, которые сейчас стоят в очереди.",
                    "Выполняющийся сейчас заказ не трогаем: он доработает по текущей логике.",
                    "Остаток холда по отменённым заказам вернётся на баланс.",
                ]),
            )
            keyboard = InlineKeyboardMarkup(inline_keyboard=[
                [
                    InlineKeyboardButton(
                        text="🧹 Да, отменить очередь",
                        callback_data=_cancel_pending_callback("do", filter_name, page),
                    )
                ],
                [InlineKeyboardButton(text="◀️ Назад", callback_data=_orders_page_callback(filter_name, page))],
            ])
            await callback.message.edit_text(text, reply_markup=keyboard, parse_mode="HTML")
            await callback.answer()
            return

        result = await api_client.cancel_pending_tasks(user_backend_id)
        cancelled = int((result or {}).get("cancelled") or 0)
        refunded = float((result or {}).get("refunded_amount") or 0)
        if cancelled:
            refund_text = f", возврат {refunded:.2f} грн" if refunded > 0 else ""
            await callback.answer(f"Отменено заказов: {cancelled}{refund_text}", show_alert=True)
        else:
            await callback.answer("В очереди уже нет заказов", show_alert=True)
        await show_orders_page(callback, state, page=page, filter_name=filter_name, answer_callback=False)
    except Exception as e:
        logger.error(f"Error cancelling pending orders: {e}", exc_info=True)
        await callback.answer("Не удалось отменить очередь", show_alert=True)


@router.callback_query(F.data.startswith("order:detail:"))
async def show_order_detail(callback: CallbackQuery):
    """Показывает детали заказа."""
    try:
        if not callback.message:
            await callback.answer("Ошибка: сообщение не найдено", show_alert=True)
            return
        
        await callback.answer()
        task_id, tab, back_page, back_filter, content_page = _parse_order_detail_callback(callback.data)
        user_id = callback.from_user.id

        user_backend_id = await get_user_backend_id(user_id)
        if not user_backend_id:
            await callback.answer("Пользователь не найден", show_alert=True)
            return
        
        task = await api_client.get_task(task_id, user_backend_id)
        text = _render_order_detail(task, tab, content_page)
        keyboard = _build_order_detail_keyboard(task, tab, back_page, back_filter, content_page)
        try:
            await callback.message.edit_text(
                text,
                reply_markup=keyboard,
                parse_mode="HTML"
            )
        except TelegramBadRequest as e:
            error_text = str(e).lower()
            if "message is not modified" in error_text:
                pass
            elif "can't parse entities" in error_text:
                logger.warning("Order detail HTML fallback for task_id=%s: %s", task_id, e)
                await callback.message.edit_text(
                    _html_to_plain_text(text),
                    reply_markup=keyboard,
                )
            else:
                raise
        return

    except Exception as e:
        logger.error(f"Error showing order detail: {e}")
        await callback.answer("Ошибка загрузки заказа", show_alert=True)


@router.callback_query(F.data.startswith("order:priority:"))
async def prioritize_order(callback: CallbackQuery):
    """Ставит pending-заказ в верхний приоритет очереди."""
    try:
        if not callback.message:
            await callback.answer("Ошибка: сообщение не найдено", show_alert=True)
            return

        task_id, tab, back_page, back_filter, content_page = _parse_stop_order_callback(callback.data)
        user_backend_id = await get_user_backend_id(callback.from_user.id)
        if not user_backend_id:
            await callback.answer("Пользователь не найден", show_alert=True)
            return

        try:
            await api_client.prioritize_task(task_id, user_backend_id)
            await callback.answer("Заказ поставлен в высокий приоритет", show_alert=True)
        except APIRequestError as exc:
            logger.warning("Prioritize order rejected for task_id=%s: %s", task_id, exc)
            await callback.answer("Приоритет можно поставить только заказу в очереди", show_alert=True)

        task = await api_client.get_task(task_id, user_backend_id)
        text = _render_order_detail(task, tab, content_page)
        keyboard = _build_order_detail_keyboard(task, tab, back_page, back_filter, content_page)
        try:
            await callback.message.edit_text(text, reply_markup=keyboard, parse_mode="HTML")
        except TelegramBadRequest as e:
            if "message is not modified" not in str(e).lower():
                raise
    except Exception as e:
        logger.error(f"Error prioritizing order: {e}", exc_info=True)
        await callback.answer("Не удалось поставить приоритет", show_alert=True)


@router.callback_query(F.data.startswith("order:stop_prompt:"))
async def prompt_stop_order(callback: CallbackQuery):
    """Показывает подтверждение остановки заказа."""
    try:
        if not callback.message:
            await callback.answer("Ошибка: сообщение не найдено", show_alert=True)
            return

        task_id, tab, back_page, back_filter, content_page = _parse_stop_order_callback(callback.data)
        user_backend_id = await get_user_backend_id(callback.from_user.id)
        if not user_backend_id:
            await callback.answer("Пользователь не найден", show_alert=True)
            return

        task = await api_client.get_task(task_id, user_backend_id)
        if not _order_is_stoppable(task):
            text = _render_order_detail(task, tab, content_page)
            keyboard = _build_order_detail_keyboard(task, tab, back_page, back_filter, content_page)
            try:
                await callback.message.edit_text(text, reply_markup=keyboard, parse_mode="HTML")
            except TelegramBadRequest as e:
                if "message is not modified" not in str(e).lower():
                    raise
            await callback.answer("Остановить можно только выполняющийся заказ", show_alert=True)
            return

        text = _html_screen(
            f"Остановить заказ #{task_id}",
            _html_block("Подтверждение", [
                "Заказ будет переведён в статус «Остановлено».",
                "Этот заказ нельзя будет запустить снова. Можно будет только сделать копию.",
                "Если исполнитель уже начал текущее видео, он закончит текущую попытку и не возьмёт следующие.",
            ]),
        )
        keyboard = InlineKeyboardMarkup(inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="⏹ Да, остановить",
                    callback_data=_stop_order_callback("stop_confirm", task_id, tab, back_page, back_filter, content_page),
                )
            ],
            [
                InlineKeyboardButton(
                    text="◀️ Назад",
                    callback_data=_detail_callback(task_id, tab, back_page, back_filter, content_page),
                )
            ],
        ])
        await callback.message.edit_text(text, reply_markup=keyboard, parse_mode="HTML")
        await callback.answer()
    except TelegramBadRequest as e:
        if "message is not modified" in str(e).lower():
            await callback.answer()
        else:
            raise
    except Exception as e:
        logger.error(f"Error showing stop order prompt: {e}", exc_info=True)
        await callback.answer("Ошибка остановки заказа", show_alert=True)


@router.callback_query(F.data.startswith("order:stop_confirm:"))
async def confirm_stop_order(callback: CallbackQuery):
    """Останавливает выполняющийся заказ."""
    try:
        if not callback.message:
            await callback.answer("Ошибка: сообщение не найдено", show_alert=True)
            return

        task_id, tab, back_page, back_filter, content_page = _parse_stop_order_callback(callback.data)
        user_backend_id = await get_user_backend_id(callback.from_user.id)
        if not user_backend_id:
            await callback.answer("Пользователь не найден", show_alert=True)
            return

        try:
            await api_client.stop_task(task_id, user_backend_id)
            await callback.answer("Заказ остановлен", show_alert=True)
        except APIRequestError as exc:
            logger.warning("Stop order rejected for task_id=%s: %s", task_id, exc)
            await callback.answer("Остановить можно только выполняющийся заказ", show_alert=True)

        task = await api_client.get_task(task_id, user_backend_id)
        text = _render_order_detail(task, tab, content_page)
        keyboard = _build_order_detail_keyboard(task, tab, back_page, back_filter, content_page)
        try:
            await callback.message.edit_text(text, reply_markup=keyboard, parse_mode="HTML")
        except TelegramBadRequest as e:
            if "message is not modified" not in str(e).lower():
                raise
    except Exception as e:
        logger.error(f"Error stopping order: {e}", exc_info=True)
        await callback.answer("Не удалось остановить заказ", show_alert=True)


@router.callback_query(F.data.startswith("order:retry_failed:"))
async def retry_failed_order(callback: CallbackQuery):
    """Создаёт новый заказ только с failed_urls из текущего заказа."""
    if not callback.message:
        await callback.answer("Ошибка: сообщение не найдено", show_alert=True)
        return

    task_id = int(callback.data.split(":")[-1])
    user_id = callback.from_user.id

    try:
        user_backend_id = await get_user_backend_id(user_id)
        if not user_backend_id:
            await callback.answer("Пользователь не найден", show_alert=True)
            return

        task = await api_client.get_task(task_id, user_backend_id)
        orig_settings = task.get("settings", {})
        failed_urls = _failed_urls_for_retry(task)

        if _order_is_stopped(task):
            await callback.answer("Остановленный заказ нельзя повторить. Можно сделать копию.", show_alert=True)
            return

        if not failed_urls:
            await callback.answer("Нет видео с ошибкой для повтора", show_alert=True)
            return

        text_variants = _text_variants_for_recreated_order(task)
        if len(text_variants) < 3:
            await callback.answer(
                "В старом заказе нет 3 вариантов текста. Сначала замените тексты.",
                show_alert=True,
            )
            return
        new_settings = _build_recreated_order_settings(orig_settings, origin="retry_failed")
        if new_settings.get("account_pool_mode") == ACCOUNT_POOL_MODE_PERSONAL:
            try:
                pool_summary = await api_client.get_personal_pool_summary(user_backend_id)
            except Exception as exc:
                logger.warning("Retry failed order: failed to validate personal pool: %s", exc)
                await callback.answer("Не удалось проверить личные аккаунты", show_alert=True)
                return
            available_main = int((pool_summary or {}).get("available_main_now") or 0)
            if available_main <= 0:
                await callback.answer("В личном пуле сейчас нет доступных аккаунтов", show_alert=True)
                return

        result = await api_client.create_task(
            user_id=user_backend_id,
            main_comment=text_variants[0],
            text_variants=text_variants,
            video_urls=failed_urls,
            reply1=task.get("reply1"),
            reply2=task.get("reply2"),
            reply3=task.get("reply3"),
            replies=_order_replies(task),
            likes=task.get("likes", 0),
            settings=new_settings,
            kind=task.get("kind", "full"),
            engagement_comment_id=orig_settings.get("engagement_comment_id"),
            engagement_username=orig_settings.get("engagement_username"),
            engagement_comment_text=orig_settings.get("engagement_comment_text"),
        )
        new_task_id = result.get("task_id")

        await callback.answer(f"Создан заказ #{new_task_id} ({len(failed_urls)} видео)", show_alert=True)
        await callback.message.edit_reply_markup(
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text=f"📋 Открыть заказ #{new_task_id}", callback_data=_detail_callback(int(new_task_id or 0)))],
                [InlineKeyboardButton(text="◀️ Назад к заказам", callback_data=_orders_page_callback())],
            ])
        )
    except Exception as e:
        logger.error(f"Error in retry_failed_order: {e}", exc_info=True)
        await callback.answer("Не удалось создать заказ. Попробуйте позже.", show_alert=True)


@router.callback_query(F.data.startswith("order:copy:"))
async def copy_order(callback: CallbackQuery):
    """Создаёт копию заказа в композере."""
    try:
        if not callback.message:
            await callback.answer("Ошибка: сообщение не найдено", show_alert=True)
            return
        
        await callback.answer()
        task_id = int(callback.data.split(":")[-1])
        user_id = callback.from_user.id

        user_backend_id = await get_user_backend_id(user_id)
        if not user_backend_id:
            await callback.answer("Пользователь не найден", show_alert=True)
            return
        
        task = await api_client.get_task(task_id, user_backend_id)

        # REFACTOR: Заполняем draft с text_variants (минимум 3 строки)
        # WHY: Нужно для создания нового заказа с теми же текстами
        draft = get_draft(user_id)
        draft.links = task.get("video_urls", [])
        draft.comment = task.get("main_comment")
        draft.likes = task.get("likes", 0)

        settings = task.get("settings", {})
        draft.delays = settings.get("delays", True)
        draft.shuffle = settings.get("shuffle", False)
        draft.rotate_accounts = settings.get("rotate_accounts", True)
        draft.account_pool_mode = _normalize_account_pool_mode(settings.get("account_pool_mode"))
        # При копировании в композер ответы сбрасываются, поэтому режим принудительно "C"
        draft.reply_mode = "C"
        
        draft.text_variants = _text_variants_for_recreated_order(task)

        from bot.services.drafts import save_draft
        save_draft(user_id, draft)

        await callback.answer("Заказ скопирован в композер")

        # Показываем композер
        from bot.keyboards.composer import get_composer_keyboard, format_composer_summary
        summary = format_composer_summary(draft, likes_label=LIKES_DISABLED_LABEL)
        try:
            await callback.message.edit_text(
                summary,
                reply_markup=get_composer_keyboard(draft),
                parse_mode="Markdown"
            )
        except TelegramBadRequest as e:
            if "message is not modified" in str(e).lower():
                await callback.answer("Композер без изменений", show_alert=False)
            else:
                raise

    except Exception as e:
        logger.error(f"Error copying order: {e}")
        await callback.answer("Ошибка копирования заказа", show_alert=True)


@router.callback_query(F.data.startswith("order:texts:"))
async def order_replace_texts(callback: CallbackQuery, state: FSMContext):
    """Запрашивает новые тексты для оставшихся URL заказа."""
    try:
        if not callback.message:
            await callback.answer("Ошибка: сообщение не найдено", show_alert=True)
            return
        await callback.answer()
        task_id = int(callback.data.split(":")[-1])
        user_backend_id = await get_user_backend_id(callback.from_user.id)
        if not user_backend_id:
            await callback.answer("Пользователь не найден", show_alert=True)
            return
        task = await api_client.get_task(task_id, user_backend_id)
        if str(task.get("status") or "").lower() != "pending":
            await callback.answer("Тексты можно менять только у pending-заказа", show_alert=True)
            return
        await state.update_data(replace_task_id=task_id, replace_text_variants=[])
        await callback.message.edit_text(
            "*Замена текстов*\n\n"
            "Отправьте минимум 3 варианта текста по одному сообщению.\n"
            "Сейчас нужен вариант 1 из 3. Применится к заказу, если он в статусе pending.",
            parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="❌ Отмена", callback_data="menu:orders")]
            ])
        )
        await state.set_state(OrderStates.REPLACE_TEXTS)
    except Exception as e:
        logger.error(f"Error starting replace texts: {e}", exc_info=True)
        await callback.answer("Ошибка", show_alert=True)


@router.message(StateFilter(OrderStates.REPLACE_TEXTS))
async def handle_replace_texts(message: Message, state: FSMContext):
    """Собирает новые тексты заказа по одному сообщению."""
    try:
        user_id = message.from_user.id
        state_data = await state.get_data()
        task_id = state_data.get("replace_task_id")
        if not task_id:
            await message.answer("Нет ID заказа в состоянии, попробуйте снова.")
            await state.clear()
            return

        variants = list(state_data.get("replace_text_variants") or [])
        raw_text = message.text or ""
        stripped = raw_text.strip()
        if stripped.lower() in {"готово", "done", "save", "сохранить"}:
            if len(variants) < 3:
                await message.answer("Нужно минимум 3 варианта текста. Отправьте следующий вариант отдельным сообщением.")
                return
            await _finish_replace_texts(message, state, user_id, task_id, variants)
            return
        if len(preserve_nonempty_lines(raw_text)) > 1:
            await message.answer("Отправьте только один вариант одним сообщением. Следующий вариант я попрошу отдельно.")
            return
        variant = normalize_user_text(raw_text)
        if not variant:
            await message.answer("Текст не распознан. Отправьте вариант отдельным сообщением.")
            return
        if any(existing.casefold() == variant.casefold() for existing in variants):
            await message.answer("Этот вариант уже есть. Отправьте другую формулировку.")
            return
        variants.append(variant)
        await state.update_data(replace_text_variants=variants)
        if len(variants) < 3:
            await message.answer(
                f"✅ Вариант {len(variants)} сохранён.\n\nОтправьте вариант {len(variants) + 1} из 3 отдельным сообщением."
            )
            return
        if len(variants) >= 10:
            await _finish_replace_texts(message, state, user_id, task_id, variants)
            return
        await message.answer(
            f"✅ Вариант {len(variants)} сохранён.\n\nМожно отправить ещё вариант или нажать «Сохранить».",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="✅ Сохранить", callback_data="order:texts_finish")],
                [InlineKeyboardButton(text="❌ Отмена", callback_data="menu:orders")]
            ])
        )
    except Exception as e:
        logger.error(f"Error handling replace texts: {e}", exc_info=True)
        await message.answer("Ошибка обработки. Попробуйте снова.")
        await state.clear()


@router.callback_query(F.data == "order:texts_finish", StateFilter(OrderStates.REPLACE_TEXTS))
async def finish_replace_texts_callback(callback: CallbackQuery, state: FSMContext):
    try:
        await callback.answer()
        data = await state.get_data()
        task_id = data.get("replace_task_id")
        variants = list(data.get("replace_text_variants") or [])
        if not callback.message or not task_id:
            await callback.answer("Заказ не найден", show_alert=True)
            await state.clear()
            return
        if len(variants) < 3:
            await callback.answer("Нужно минимум 3 варианта", show_alert=True)
            return
        await _finish_replace_texts(callback.message, state, callback.from_user.id, task_id, variants)
    except Exception as e:
        logger.error(f"Error finishing replace texts: {e}", exc_info=True)
        await callback.answer("Ошибка", show_alert=True)
        await state.clear()


async def _finish_replace_texts(message: Message, state: FSMContext, user_id: int, task_id: int, variants: list[str]) -> None:
    try:
        user_backend_id = await get_user_backend_id(user_id)
        if not user_backend_id:
            await message.answer("Пользователь не найден. Попробуйте позже.")
            await state.clear()
            return
        await api_client.update_task_texts(task_id, variants, user_backend_id)
        await message.answer(
            f"{Emoji.SUCCESS} Тексты обновлены для заказа #{task_id} (pending).",
            reply_markup=get_main_menu_keyboard()
        )
    except Exception as e:
        logger.error(f"Error updating order texts: {e}", exc_info=True)
        await message.answer("Не удалось обновить тексты. Попробуйте позже.")
    await state.clear()
