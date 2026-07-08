import asyncio
import json

from aiogram import Router, F
from aiogram.types import CallbackQuery, Message, InlineKeyboardMarkup, InlineKeyboardButton
from aiogram.fsm.context import FSMContext
from aiogram.filters import StateFilter
from aiogram.filters.command import Command
from aiogram.exceptions import TelegramBadRequest
from bot.state.order_states import OrderStates
from bot.services.api_client import api_client
from bot.services.admin_ui import format_compact_age, safe_edit_callback_text
from bot.services.formatting import Emoji, markdown_inline_code, markdown_handle
from bot.services.software_mode import clear_admin_mode_cache, clear_software_mode_cache
from bot.utils.markdown_escape import escape_markdown_simple
from services.smart_account_parser import parse_accounts_file_smart
from services.likes_config import (
    BOOST_LIKES_DEFAULT,
    BOOST_LIKES_TARGET_EACH_FOUND,
    BOOST_LIKES_TARGET_SINGLE_TOP,
    BOOST_LIKES_MAX,
    BOOST_LIKES_MIN,
    coerce_likes_target_mode,
    coerce_likes_quantity,
)
from dataclasses import asdict
from bot.keyboards.admin import (
    get_admin_menu_keyboard, get_accounts_menu_keyboard, get_import_confirm_keyboard,
    get_monitoring_menu_keyboard, get_monitor_parallel_settings_keyboard,
    get_adaptive_settings_keyboard,
    get_adaptive_advanced_keyboard,
    get_adaptive_category_keyboard, get_adaptive_bloggers_keyboard,
    get_blogger_detail_keyboard, get_blogger_delete_confirm_keyboard,
    get_proxies_menu_keyboard, get_proxy_import_confirm_keyboard, get_proxy_list_keyboard,
    get_resources_keyboard, get_geo_menu_keyboard,
)
from bot.config import load_config
from services.admin_audit_service import log_admin_action
import logging
import re
import unicodedata

logger = logging.getLogger("bot.admin")
router = Router()
config = load_config()

ADMIN_USERS_PAGE_SIZE = 10
USER_CARD_PAGE_SIZE = 5
USER_CARD_PAGINATED_TABS = {"orders", "monitoring", "templates", "accounts"}
ADMIN_USER_FILTERS: tuple[tuple[str, str], ...] = (
    ("all", "Все"),
    ("pending", "Ждут"),
    ("active", "Активные"),
    ("blocked", "Блок"),
    ("admins", "Админы"),
)
ADMIN_USER_FILTER_LABELS = dict(ADMIN_USER_FILTERS)


def _format_likes_range() -> str:
    return f"{BOOST_LIKES_MIN} до {BOOST_LIKES_MAX:,}".replace(",", " ")


def _format_likes_target_mode(mode: str | None) -> str:
    normalized = coerce_likes_target_mode(mode, BOOST_LIKES_TARGET_SINGLE_TOP)
    if normalized == BOOST_LIKES_TARGET_EACH_FOUND:
        return "каждый найденный автор"
    return "один лучший комментарий"


async def _safe_edit_callback_text(
    callback: CallbackQuery,
    text: str,
    *,
    unchanged_notice: str | None = None,
    **kwargs,
):
    """Edits callback message and suppresses Telegram no-op/parse errors."""
    return await safe_edit_callback_text(
        callback,
        text,
        unchanged_notice=unchanged_notice,
        **kwargs,
    )


def _parse_decimal_input(value: str) -> float:
    """Принимает обычные числа, числа с запятой и текст вроде `7 грн`."""
    raw = (value or "").strip()
    if not raw:
        raise ValueError("empty")

    normalized_chars: list[str] = []
    for char in raw:
        if char.isdigit():
            try:
                normalized_chars.append(str(unicodedata.digit(char)))
            except Exception:
                normalized_chars.append(char)
        else:
            normalized_chars.append(char)

    normalized = "".join(normalized_chars).lower()
    normalized = normalized.replace(",", ".")
    normalized = normalized.replace("грн", "").replace("uah", "").replace("₴", "")
    normalized = normalized.replace(" ", "")
    normalized = re.sub(r"[^0-9.]", "", normalized)

    if not normalized or normalized.count(".") > 1:
        raise ValueError("invalid number")

    return float(normalized)


def is_admin_user(telegram_id: int, role: str) -> bool:
    """Проверяет, является ли пользователь админом."""
    return role in ("admin", "owner") or telegram_id == config.get("OWNER_TELEGRAM_ID")


async def get_user_data(telegram_id: int) -> tuple[int, str, bool] | None:
    """Получает данные пользователя. Возвращает (backend_id, role, is_admin) или None."""
    try:
        user_data = await api_client.sync_telegram(telegram_id, None)
        backend_id = user_data.get("id")
        role = user_data.get("role", "user")
        admin = is_admin_user(telegram_id, role)
        return backend_id, role, admin
    except Exception as e:
        logger.error(f"Error getting user data: {e}")
        return None


@router.message(Command("reauth_account"))
@router.message(Command("reauth"))
async def cmd_reauth_account(message: Message, state: FSMContext):
    user_info = await get_user_data(message.from_user.id)
    if not user_info or not user_info[2]:
        await message.answer("Эта команда доступна только администратору.")
        return

    parts = (message.text or "").strip().split(maxsplit=1)
    if len(parts) != 2 or not parts[1].strip().isdigit():
        await message.answer("Использование: /reauth <account_id>")
        return

    account_id = int(parts[1].strip())
    await state.set_state(OrderStates.ADMIN_REAUTH_ACCOUNT)
    await state.update_data(reauth_account_id=account_id)
    await message.answer(
        f"Reauth для аккаунта #{account_id}\n\n"
        "1. Откройте TikTok в реальном браузере с прокси этого аккаунта\n"
        "2. Войдите вручную\n"
        "3. Экспортируйте cookies\n"
        "4. Отправьте сюда JSON cookies следующим сообщением"
    )


@router.message(Command("list_dead"))
async def cmd_list_dead_accounts(message: Message):
    user_info = await get_user_data(message.from_user.id)
    if not user_info or not user_info[2]:
        await message.answer("Эта команда доступна только администратору.")
        return

    try:
        accounts = await api_client.list_accounts(status="reauth_needed", limit=100, offset=0)
    except Exception as e:
        logger.error(f"Error listing reauth accounts: {e}", exc_info=True)
        await message.answer("Не удалось получить список аккаунтов reauth_needed.")
        return

    if not accounts:
        await message.answer("Аккаунтов со статусом reauth_needed сейчас нет.")
        return

    lines = ["Аккаунты на reauth:"]
    for account in accounts[:30]:
        account_id = account.get("id")
        label = account.get("label") or "no-label"
        reason = account.get("last_error") or "нет причины"
        updated_at = account.get("updated_at") or account.get("last_checked_at") or "n/a"
        lines.append(f"#{account_id} | {label} | {updated_at} | {reason[:80]}")
    if len(accounts) > 30:
        lines.append(f"... ещё {len(accounts) - 30}")
    await message.answer("\n".join(lines))


@router.message(StateFilter(OrderStates.ADMIN_REAUTH_ACCOUNT))
async def process_reauth_account_cookies(message: Message, state: FSMContext):
    user_info = await get_user_data(message.from_user.id)
    if not user_info or not user_info[2]:
        await state.clear()
        await message.answer("Эта команда доступна только администратору.")
        return

    data = await state.get_data()
    account_id = data.get("reauth_account_id")
    cookies = (message.text or "").strip()
    if not account_id or not cookies:
        await message.answer("Не удалось получить account_id или cookies. Запустите /reauth_account ещё раз.")
        await state.clear()
        return

    try:
        parsed = json.loads(cookies)
        if not isinstance(parsed, list):
            raise ValueError("cookies must be a JSON list")
        cookie_names = {str(item.get("name", "")) for item in parsed if isinstance(item, dict)}
        if not ({"sid_guard", "sessionid"} & cookie_names):
            raise ValueError("missing sid_guard/sessionid")
    except Exception:
        await message.answer("Cookies должны быть JSON-массивом и содержать cookie sid_guard или sessionid.")
        return

    try:
        await api_client.update_account(
            int(account_id),
            cookies=cookies,
            status="active",
        )
        await log_admin_action(
            admin_telegram_id=message.from_user.id,
            action="reauth_account",
            target_user_id=None,
            details={"account_id": int(account_id)},
        )
        await message.answer(f"Аккаунт #{account_id} обновлён. Cookies сохранены, статус возвращён в active.")
    except Exception as e:
        logger.error(f"Error reauth account {account_id}: {e}", exc_info=True)
        await message.answer(f"Не удалось обновить аккаунт #{account_id}. Проверьте формат cookies и попробуйте снова.")
    finally:
        await state.clear()


def _build_import_preview_text(
    parsed_accounts: list[dict],
    accounts_without_proxy: list[dict],
    parse_errors: list[dict],
    parse_duplicates: int,
    total_lines: int
) -> str:
    """Собирает текст превью перед импортом аккаунтов."""
    has_without_proxy = len(accounts_without_proxy) > 0
    lines = [
        f"{Emoji.SUCCESS} Найдено {len(parsed_accounts)} аккаунтов",
        f"{Emoji.WARNING} Без прокси: {len(accounts_without_proxy)}" if has_without_proxy else f"{Emoji.SUCCESS} Все аккаунты с прокси",
    ]
    if parse_duplicates > 0:
        lines.append(f"♻️ Дубликатов в файле: {parse_duplicates}")
    if parse_errors:
        lines.append(f"⚠️ Ошибок разбора: {len(parse_errors)}")
    if total_lines:
        lines.append(f"📄 Обработано строк: {total_lines}")
    lines.append("")
    lines.append("Выберите действие:")
    return "\n".join(lines)


def _status_badge(status: str) -> str:
    mapping = {
        "ok": "🟢",
        "error": "🔴",
        "degraded": "🟡",
    }
    return mapping.get((status or "").lower(), "⚪")


def _humanize_monitor_category(category: str) -> str:
    mapping = {
        "captcha": "капча",
        "rate_limit": "лимит TikTok",
        "proxy_issue": "прокси",
        "network": "сеть",
        "dead_session": "битые куки",
        "suspicious": "подозрительный аккаунт",
        "empty_profile": "пустой профиль",
        "detection_error": "ошибка детекта",
        "page_blocked": "блок страницы",
    }
    return mapping.get(category or "", category or "неизвестно")


def _format_heartbeat_info(info: dict | None) -> str:
    if not isinstance(info, dict):
        return "нет данных"
    status = info.get("status") or "unknown"
    ts = info.get("ts")
    if not ts:
        return str(status)
    try:
        from datetime import datetime, timezone

        age_seconds = max(
            0,
            int((datetime.now(timezone.utc) - datetime.fromtimestamp(int(ts), tz=timezone.utc)).total_seconds()),
        )
        if age_seconds < 60:
            age_text = f"{age_seconds}с назад"
        else:
            age_text = f"{age_seconds // 60}м назад"
        return f"{status}, {age_text}"
    except Exception:
        return str(status)


def _format_account_pool_block(title: str, pool: dict | None) -> str:
    pool = pool or {}
    top_categories = pool.get("top_categories", []) or []
    categories_text = "нет явных проблем"
    if top_categories:
        categories_text = ", ".join(
            f"{_humanize_monitor_category(item.get('category'))}: {item.get('count', 0)}"
            for item in top_categories[:3]
        )

    return (
        f"*{title}*\n"
        f"Готовы сейчас: `{pool.get('eligible_now', 0)}` из `{pool.get('total_candidates', 0)}` | reserved `{pool.get('reserved', 0)}`\n"
        f"Здоровые: `{pool.get('healthy', 0)}` | Degraded: `{pool.get('degraded', 0)}` | Cooldown: `{pool.get('cooldown', 0)}`\n"
        f"Прокси: `{pool.get('proxy_issue', 0)}` | Плохих прокси: `{pool.get('bad_proxies', 0)}` | Dead session: `{pool.get('dead_session', 0)}`\n"
        f"Свежая валидация: `{pool.get('recently_validated', 0)}` | Avg score: `{pool.get('avg_score', 0)}` | Best: `{pool.get('best_score', 0)}`\n"
        f"Топ причин: `{categories_text}`"
    )


def _humanize_session_bucket(bucket: str | None) -> str:
    mapping = {
        "http_strong": "strong HTTP",
        "http_workable": "workable HTTP",
        "http_degraded": "weak HTTP",
        "dom_only": "DOM only",
        "weak": "weak",
        "dead": "dead",
    }
    return mapping.get(bucket or "", bucket or "unknown")


def _humanize_session_issue(issue: str | None) -> str:
    mapping = {
        "full_http_ready": "full HTTP ready",
        "http_ready": "HTTP ready",
        "weak_http_context": "weak HTTP context",
        "session_only": "session only",
        "reply_http_strong": "reply HTTP strong",
        "reply_http_ready": "reply HTTP ready",
        "reply_session_only": "reply session only",
        "monitor_stable": "monitor stable",
        "monitor_ready": "monitor ready",
        "monitor_session_only": "monitor session only",
        "missing_cookie_source": "missing cookies",
        "invalid_cookie_format": "broken cookies",
        "missing_auth_session": "missing auth session",
    }
    return mapping.get(issue or "", issue or "unknown")


def _format_runtime_bucket_summary(pool: dict | None) -> str:
    pool = pool or {}
    return (
        f"strong `{pool.get('session_strong', 0)}` | "
        f"workable `{pool.get('session_workable', 0)}` | "
        f"weak `{pool.get('session_degraded', 0)}` | "
        f"DOM `{pool.get('session_dom_only', 0)}` | "
        f"weak-only `{pool.get('session_weak', 0)}` | "
        f"dead `{pool.get('session_dead', 0)}`"
    )


def _format_strategy_summary(pool: dict | None) -> str:
    pool = pool or {}
    return (
        f"HTTP-first `{pool.get('http_first', 0)}` | "
        f"cautious `{pool.get('http_cautious', 0)}` | "
        f"DOM-first `{pool.get('dom_first', 0)}` | "
        f"skip `{pool.get('skip_http', 0)}`"
    )


def _format_lane_summary(pool: dict | None) -> str:
    pool = pool or {}
    return (
        f"keeper `{pool.get('keeper', 0)}` | "
        f"balanced `{pool.get('balanced', 0)}` | "
        f"burner `{pool.get('burner', 0)}` | "
        f"retire `{pool.get('retire', 0)}`"
    )


def _top_session_bucket_text(pool: dict | None) -> str:
    items = (pool or {}).get("top_session_buckets", []) or []
    if not items:
        return "n/a"
    return ", ".join(
        f"{_humanize_session_bucket(item.get('bucket'))}: {item.get('count', 0)}"
        for item in items[:3]
    )


def _top_session_issue_text(pool: dict | None) -> str:
    items = (pool or {}).get("top_session_issues", []) or []
    if not items:
        return "n/a"
    return ", ".join(
        f"{_humanize_session_issue(item.get('issue'))}: {item.get('count', 0)}"
        for item in items[:3]
    )


def _build_attention_lines(
    *,
    health_summary: dict | None,
    monitoring_summary: dict | None,
    runtime_health: dict | None,
    account_stats: dict | None,
    proxy_stats: dict | None,
    pricing: dict | None,
    boost_likes: dict | None,
) -> list[str]:
    lines: list[str] = []
    health_summary = health_summary or {}
    monitoring_summary = monitoring_summary or {}
    runtime_health = runtime_health or {}
    account_stats = account_stats or {}
    proxy_stats = proxy_stats or {}
    pricing = pricing or {}
    boost_likes = boost_likes or {}

    active_incidents = [
        item for item in (monitoring_summary.get("incidents") or [])
        if item.get("state") == "active"
    ]
    for incident in active_incidents[:6]:
        title = escape_markdown_simple(str(incident.get("title") or incident.get("type") or "Инцидент монитора"))
        cause = escape_markdown_simple(str(incident.get("cause") or incident.get("admin_action") or "Нужна проверка"))
        lines.append(f"{title}: {cause}")

    executor_alive = bool((health_summary.get("executor") or {}).get("alive"))
    monitor_alive = bool((health_summary.get("video_monitor") or {}).get("alive"))
    queue_len = int(((health_summary.get("queue") or {}).get("length")) or 0)
    if not executor_alive:
        lines.append("executor heartbeat не виден")
    if not monitor_alive:
        lines.append("monitor loop heartbeat не виден")
    if queue_len >= 25:
        lines.append(f"очередь раздулась: {queue_len}")

    tasks = monitoring_summary.get("tasks", {}) or {}
    bloggers = monitoring_summary.get("bloggers", {}) or {}
    accounts = monitoring_summary.get("accounts", {}) or {}
    if int(tasks.get("stuck_in_progress", 0) or 0) > 0:
        lines.append(f"зависшие задачи: {tasks.get('stuck_in_progress', 0)}")
    if int(bloggers.get("stale_locks", 0) or 0) > 0:
        lines.append(f"просроченные lock у блогеров: {bloggers.get('stale_locks', 0)}")
    if int(accounts.get("bad_proxies", 0) or 0) > 0:
        lines.append(f"плохие monitor-прокси: {accounts.get('bad_proxies', 0)}")

    main_pool = runtime_health.get("main_comment", {}) or {}
    reply_pool = runtime_health.get("reply", {}) or {}
    monitor_pool = runtime_health.get("monitor", {}) or {}
    if int(main_pool.get("eligible_now", 0) or 0) == 0:
        lines.append("нет готовых main-comment аккаунтов")
    if int(reply_pool.get("eligible_now", 0) or 0) == 0:
        lines.append("нет готовых reply аккаунтов")
    if int(monitor_pool.get("eligible_now", 0) or 0) == 0:
        lines.append("нет готовых monitor аккаунтов")

    total_accounts = int(account_stats.get("total", 0) or 0)
    active_accounts = int(account_stats.get("active", 0) or 0)
    if total_accounts and active_accounts / max(total_accounts, 1) < 0.5:
        lines.append(f"активных аккаунтов мало: {active_accounts}/{total_accounts}")

    if int(proxy_stats.get("total", 0) or 0) == 0:
        lines.append("пул прокси пуст")
    elif int(proxy_stats.get("bad", 0) or 0) > 0:
        lines.append(f"обнаружены bad proxy: {proxy_stats.get('bad', 0)}")

    try:
        price_success = float(pricing.get("price_success", 0) or 0)
        price_failure = float(pricing.get("price_failure", 0) or 0)
        if price_failure >= price_success > 0:
            lines.append("цены опасные: failed order стоит слишком много")
    except Exception:
        pass

    if boost_likes.get("boost_likes_enabled") and not boost_likes.get("boost_provider_available"):
        lines.append("лайки включены, но API лайков не настроен")

    return lines[:6]


def _format_monitor_state_label(mode: str | None) -> str:
    return {
        "normal": "обычный режим",
        "network_pause": "пауза из-за TikTok/сети",
        "browser_recovering": "восстановление Playwright/browser",
        "account_pool_low": "мало готовых monitor-аккаунтов",
    }.get(str(mode or "normal"), str(mode or "normal"))


def _format_monitor_incident_lines(incidents: list[dict], limit: int = 4) -> list[str]:
    lines: list[str] = []
    for incident in incidents[:limit]:
        state = str(incident.get("state") or "active")
        severity = str(incident.get("severity") or "warning").upper()
        title = escape_markdown_simple(str(incident.get("title") or incident.get("type") or "Инцидент"))
        cause = escape_markdown_simple(str(incident.get("cause") or "без деталей"))
        prefix = "OK" if state == "recovered" else severity
        lines.append(f"`{prefix}` {title} — {cause}")
    return lines


def _safe_ratio(numerator: int, denominator: int) -> int:
    if denominator <= 0:
        return 0
    return int(round((numerator / denominator) * 100))


def _coerce_float(value, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _format_proxy_mode_label(proxy_type_filter: str | None, prefer_mobile: bool) -> str:
    base = "все прокси"
    if proxy_type_filter == "mobile":
        base = "только mobile"
    elif proxy_type_filter == "residential":
        base = "только residential"
    if prefer_mobile and proxy_type_filter != "mobile":
        return f"{base} + prefer mobile"
    return base


def _format_validation_progress(progress: dict | None) -> str:
    progress = progress or {}
    if not progress.get("running"):
        return "не запущена"
    checked = int(progress.get("checked", 0) or 0)
    total = int(progress.get("total", 0) or 0)
    valid = int(progress.get("valid", 0) or 0)
    invalid = int(progress.get("invalid", 0) or 0)
    return f"идёт {checked}/{total} | ok {valid} | bad {invalid}"


def _format_recheck_progress(progress: dict | None) -> str:
    progress = progress or {}
    if not progress.get("running"):
        return "не запущен"
    checked = int(progress.get("checked", 0) or 0)
    total = int(progress.get("total", 0) or 0)
    revived = int(progress.get("revived", 0) or 0)
    still_dead = int(progress.get("still_dead", 0) or 0)
    return f"идёт {checked}/{total} | revived {revived} | dead {still_dead}"


@router.callback_query(F.data == "menu:admin")
async def admin_menu(callback: CallbackQuery):
    """Показывает центральный админ-дашборд с инцидентами, ресурсами и текущим конфигом."""
    try:
        try:
            await callback.answer()
        except TelegramBadRequest as answer_error:
            error_msg = str(answer_error).lower()
            if "query is too old" not in error_msg and "query id is invalid" not in error_msg:
                logger.warning(f"Callback answer error in admin_menu: {answer_error}")

        (
            account_stats_result,
            proxy_stats_result,
            resources_stats_result,
            monitoring_summary_result,
            runtime_health_result,
            health_summary_result,
            pricing_result,
            proxy_settings_result,
            boost_likes_result,
            software_mode_result,
            commenting_mode_result,
            order_strategy_result,
        ) = await asyncio.gather(
            api_client.get_account_stats(),
            api_client.get_proxy_stats(),
            api_client.get_resources_stats(),
            api_client.get_monitoring_summary(),
            api_client.get_account_runtime_health(),
            api_client.get_health_summary(),
            api_client.get_pricing(),
            api_client.get_proxy_settings(),
            api_client.get_boost_likes_settings(),
            api_client.get_software_mode_settings(),
            api_client.get_commenting_mode_settings(),
            api_client.get_order_execution_strategy_settings(),
            return_exceptions=True,
        )

        load_errors: list[str] = []

        def _normalize(result, label: str):
            if isinstance(result, Exception):
                logger.error(f"Admin dashboard: failed to load {label}: {result}", exc_info=True)
                load_errors.append(label)
                return {}
            return result or {}

        account_stats = _normalize(account_stats_result, "accounts")
        proxy_stats = _normalize(proxy_stats_result, "proxies")
        resources_stats = _normalize(resources_stats_result, "resources")
        monitoring_summary = _normalize(monitoring_summary_result, "monitoring")
        runtime_health = _normalize(runtime_health_result, "runtime")
        health_summary = _normalize(health_summary_result, "health")
        pricing = _normalize(pricing_result, "pricing")
        proxy_settings = _normalize(proxy_settings_result, "proxy settings")
        boost_likes = _normalize(boost_likes_result, "likes settings")
        software_mode_data = _normalize(software_mode_result, "software mode")
        commenting_mode_data = _normalize(commenting_mode_result, "commenting mode")
        order_strategy_data = _normalize(order_strategy_result, "order strategy")

        resources_accounts = resources_stats.get("accounts", {}) or {}
        resources_proxies = resources_stats.get("proxies", {}) or {}
        today = resources_stats.get("today", {}) or {}
        system = monitoring_summary.get("system", {}) or {}
        bloggers = monitoring_summary.get("bloggers", {}) or {}
        videos = monitoring_summary.get("videos", {}) or {}
        tasks = monitoring_summary.get("tasks", {}) or {}
        main_pool = runtime_health.get("main_comment", {}) or {}
        reply_pool = runtime_health.get("reply", {}) or {}
        monitor_pool = runtime_health.get("monitor", {}) or {}
        automation_state = monitoring_summary.get("automation_state", {}) or {}
        incidents = monitoring_summary.get("incidents", []) or []
        recommended_actions = monitoring_summary.get("recommended_actions", []) or []

        active_accounts = int(account_stats.get("active", resources_accounts.get("active", 0)) or 0)
        total_accounts = int(account_stats.get("total", resources_accounts.get("total", 0)) or 0)
        error_accounts = int(account_stats.get("error", 0) or 0)
        dead_accounts = int(account_stats.get("dead", resources_accounts.get("dead", 0)) or 0)
        limited_accounts = int(account_stats.get("limited", 0) or 0)
        bad_proxies = int(proxy_stats.get("bad", 0) or 0)
        proxy_total = int(proxy_stats.get("total", resources_proxies.get("total", 0)) or 0)
        price_success = _coerce_float(pricing.get("price_success"), 0.0)
        price_failure = _coerce_float(pricing.get("price_failure"), 0.0)
        refund_delta = max(0.0, price_success - price_failure)
        likes_enabled = bool(boost_likes.get("boost_likes_enabled"))
        likes_quantity = coerce_likes_quantity(
            boost_likes.get("boost_likes_quantity"),
            BOOST_LIKES_DEFAULT,
        )
        provider_available = bool(boost_likes.get("boost_provider_available"))
        proxy_mode = _format_proxy_mode_label(
            proxy_settings.get("proxy_type_filter"),
            bool(proxy_settings.get("proxy_prefer_mobile", False)),
        )
        current_software_mode = software_mode_data.get("software_mode", "full")
        software_mode_label = "🟢 Полный" if current_software_mode == "full" else "🟡 Парсинг"
        current_commenting_mode = commenting_mode_data.get("commenting_mode", "old")
        order_strategy_label = str(order_strategy_data.get("order_execution_strategy") or "balanced")
        commenting_mode_label = {
            "parallel": "🟢 Новая параллельная",
            "parallel_6x1": "🟣 Параллельная 6×1",
        }.get(current_commenting_mode, "⚪ Старая")

        attention_lines = _build_attention_lines(
            health_summary=health_summary,
            monitoring_summary=monitoring_summary,
            runtime_health=runtime_health,
            account_stats=account_stats,
            proxy_stats=proxy_stats,
            pricing=pricing,
            boost_likes=boost_likes,
        )

        text = (
            "🔧 *Админ-панель*\n"
            "━━━━━━━━━━━━━━━━━━━━\n\n"
            "🩺 *Система*\n"
            f"Redis: `{system.get('redis', 'unknown')}`\n"
            f"Executor: `{_format_heartbeat_info((health_summary.get('executor') or {}).get('info'))}`\n"
            f"Monitor: `{_format_heartbeat_info((health_summary.get('video_monitor') or {}).get('info'))}`\n"
            f"Queue: `{((health_summary.get('queue') or {}).get('length')) or system.get('queue_len', 0) or 0}`\n"
            f"Monitor parallel: `{system.get('monitor_max_parallel_browsers', '—')}` browsers/tick cap\n"
            f"Browser pool: `{system.get('browser_pool_browsers', 0)}` browsers | `{system.get('browser_pool_contexts', 0)}` ctx | `{system.get('browser_pool_pages', 0)}` pages | `{system.get('browser_pool_reusable_pages', 0)}` reusable\n\n"
            "📦 *Ресурсы*\n"
            f"Аккаунты: `{active_accounts}/{total_accounts}` active | error `{error_accounts}` | dead `{dead_accounts}` | limited `{limited_accounts}`\n"
            f"Прокси: `{proxy_total}` total | active `{int(proxy_stats.get('active', 0) or 0)}` | slow `{int(proxy_stats.get('slow', 0) or 0)}` | bad `{bad_proxies}` | unknown `{int(proxy_stats.get('unknown', 0) or 0)}`\n"
            f"Сегодня: comments `{int(today.get('comments', 0) or 0)}` | likes `{int(today.get('likes', 0) or 0)}` | errors `{int(today.get('errors', 0) or 0)}`\n\n"
            "🧠 *Runtime сейчас*\n"
            f"Main: ready `{int(main_pool.get('eligible_now', 0) or 0)}` | avg runtime `{int(main_pool.get('avg_score', 0) or 0)}` | avg session `{int(main_pool.get('avg_session_score', 0) or 0)}`\n"
            f"Reply: ready `{int(reply_pool.get('eligible_now', 0) or 0)}` | avg runtime `{int(reply_pool.get('avg_score', 0) or 0)}` | avg session `{int(reply_pool.get('avg_session_score', 0) or 0)}`\n"
            f"Monitor: ready `{int(monitor_pool.get('eligible_now', 0) or 0)}` | reserved `{int(monitor_pool.get('reserved', 0) or 0)}` | avg runtime `{int(monitor_pool.get('avg_score', 0) or 0)}` | avg session `{int(monitor_pool.get('avg_session_score', 0) or 0)}`\n\n"
            "🛰 *Мониторинг*\n"
            f"Блогеры: `{int(bloggers.get('total', 0) or 0)}` | locks `{int(bloggers.get('locked', 0) or 0)}` | stale `{int(bloggers.get('stale_locks', 0) or 0)}`\n"
            f"Видео 24ч: `{int(videos.get('discovered_last_24h', 0) or 0)}` | new `{int(videos.get('new', 0) or 0)}` | queued `{int(videos.get('queued', 0) or 0)}`\n"
            f"Задачи: pending `{int(tasks.get('pending', 0) or 0)}` | work `{int(tasks.get('in_progress', 0) or 0)}` | stuck `{int(tasks.get('stuck_in_progress', 0) or 0)}` | failed 24ч `{int(tasks.get('failed_today', 0) or 0)}`\n\n"
            "⚙️ *Текущий конфиг*\n"
            f"Режим: `{software_mode_label}`\n"
            f"Комментинг: `{commenting_mode_label}`\n"
            f"Стратегия заказов: `{order_strategy_label}`\n"
            f"Прокси-режим: `{proxy_mode}`\n"
            f"Лайки: `{'вкл' if likes_enabled else 'выкл'}` | qty `{likes_quantity}` | API лайков `{'настроен' if provider_available else 'не настроен'}`\n"
            f"Цены: success `{price_success:.2f}` | failure `{price_failure:.2f}` | refund `{refund_delta:.2f}`\n"
        )

        if load_errors:
            text += "\n⚠️ *Не всё загрузилось:* `" + ", ".join(load_errors[:4]) + "`\n"

        state_mode = _format_monitor_state_label(str(automation_state.get("mode") or "normal"))
        state_reason = escape_markdown_simple(str(automation_state.get("reason") or "Критичных инцидентов нет."))
        pause_until = automation_state.get("pause_until")
        text += (
            "\n\n*Автономия монитора*\n"
            f"Режим: `{escape_markdown_simple(state_mode)}`\n"
            f"Почему: {state_reason}\n"
        )
        if pause_until:
            text += f"Пауза до: `{escape_markdown_simple(str(pause_until))}`\n"

        incident_lines = _format_monitor_incident_lines(incidents)
        if incident_lines:
            text += "\n*Понятные инциденты*\n"
            text += "\n".join(f"• {line}" for line in incident_lines)

        if recommended_actions:
            text += "\n\n*Что делать админу*\n"
            text += "\n".join(
                f"• {escape_markdown_simple(str(action))}"
                for action in recommended_actions[:4]
            )

        if attention_lines:
            text += "\n🚨 *Требует внимания*\n"
            text += "\n".join(f"• {line}" for line in attention_lines)

        await _safe_edit_callback_text(
            callback,
            text,
            reply_markup=get_admin_menu_keyboard(),
            parse_mode="Markdown",
        )
    except Exception as e:
        error_msg = str(e).lower()
        if "query is too old" in error_msg or "query id is invalid" in error_msg:
            logger.debug(f"Callback query expired in admin_menu: {e}")
            return
        logger.error(f"Error in admin_menu: {e}", exc_info=True)
        try:
            await callback.answer("Ошибка загрузки админ-панели", show_alert=True)
        except Exception:
            pass


@router.callback_query(F.data == "admin:main")
async def admin_main_menu(callback: CallbackQuery):
    """Обновляет админ-меню (то же что и menu:admin)."""
    await admin_menu(callback)


@router.callback_query(F.data == "admin:monitoring")
async def monitoring_menu(callback: CallbackQuery):
    """Показывает управленческий экран мониторинга: heartbeat, queue, browser pool и проблемные зоны."""
    try:
        await callback.answer()
        monitoring_result, health_result = await asyncio.gather(
            api_client.get_monitoring_summary(),
            api_client.get_health_summary(),
            return_exceptions=True,
        )

        summary = {} if isinstance(monitoring_result, Exception) else (monitoring_result or {})
        health_summary = {} if isinstance(health_result, Exception) else (health_result or {})
        if isinstance(monitoring_result, Exception):
            logger.error(f"Error loading monitoring summary: {monitoring_result}", exc_info=True)
        if isinstance(health_result, Exception):
            logger.error(f"Error loading health summary: {health_result}", exc_info=True)

        system = summary.get("system", {}) or {}
        bloggers = summary.get("bloggers", {}) or {}
        videos = summary.get("videos", {}) or {}
        tasks = summary.get("tasks", {}) or {}
        accounts = summary.get("accounts", {}) or {}
        automation_state = summary.get("automation_state", {}) or {}
        incidents = summary.get("incidents", []) or []
        recommended_actions = summary.get("recommended_actions", []) or []
        top_categories = accounts.get("top_categories", []) or []

        categories_text = "нет явных проблем"
        if top_categories:
            categories_text = ", ".join(
                f"{_humanize_monitor_category(item.get('category'))}: {item.get('count', 0)}"
                for item in top_categories[:3]
            )

        attention_lines = _build_attention_lines(
            health_summary=health_summary,
            monitoring_summary=summary,
            runtime_health=None,
            account_stats=None,
            proxy_stats=None,
            pricing=None,
            boost_likes=None,
        )

        text = (
            "🛰 *Мониторинг*\n"
            "━━━━━━━━━━━━━━━━━━━━\n\n"
            "🩺 *Контур*\n"
            f"Monitor loop: `{_format_heartbeat_info((health_summary.get('video_monitor') or {}).get('info') or system.get('monitor_info'))}`\n"
            f"Executor: `{_format_heartbeat_info((health_summary.get('executor') or {}).get('info') or system.get('executor_info'))}`\n"
            f"Redis: `{system.get('redis', 'unknown')}`\n"
            f"Очередь к проверке: `{((health_summary.get('queue') or {}).get('length')) or system.get('queue_len', 0) or 0}`\n"
            f"Параллель браузеров (monitor): `{system.get('monitor_max_parallel_browsers', '—')}` "
            f"(лимит одновременных Playwright в video\\_monitor\\_worker)\n"
            f"Browser pool: `{system.get('browser_pool_browsers', 0)}` browsers | `{system.get('browser_pool_contexts', 0)}` ctx | `{system.get('browser_pool_pages', 0)}` pages | `{system.get('browser_pool_reusable_pages', 0)}` reusable\n\n"
            "👀 *Блогеры*\n"
            f"Всего: `{int(bloggers.get('total', 0) or 0)}`\n"
            f"Активных подключений: `{int(bloggers.get('enabled_subscriptions', 0) or 0)}`\n"
            f"Сейчас в lock: `{int(bloggers.get('locked', 0) or 0)}` | stale `{int(bloggers.get('stale_locks', 0) or 0)}`\n\n"
            "🎞 *Видео и задачи*\n"
            f"Найдено за 24ч: `{int(videos.get('discovered_last_24h', 0) or 0)}`\n"
            f"Видео: new `{int(videos.get('new', 0) or 0)}` | queued `{int(videos.get('queued', 0) or 0)}` | done `{int(videos.get('done', 0) or 0)}` | failed `{int(videos.get('failed', 0) or 0)}`\n"
            f"Задачи: pending `{int(tasks.get('pending', 0) or 0)}` | work `{int(tasks.get('in_progress', 0) or 0)}` | stuck `{int(tasks.get('stuck_in_progress', 0) or 0)}` | failed 24ч `{int(tasks.get('failed_today', 0) or 0)}`\n\n"
            "🧩 *Monitor-аккаунты*\n"
            f"Готовы сейчас: `{int(accounts.get('eligible_now', 0) or 0)}` из `{int(accounts.get('total_candidates', 0) or 0)}` | reserved `{int(accounts.get('reserved', 0) or 0)}`\n"
            f"Healthy `{int(accounts.get('healthy', 0) or 0)}` | cooldown `{int(accounts.get('cooldown', 0) or 0)}` | degraded `{int(accounts.get('degraded', 0) or 0)}`\n"
            f"Proxy issue `{int(accounts.get('proxy_issue', 0) or 0)}` | bad proxies `{int(accounts.get('bad_proxies', 0) or 0)}` | dead session `{int(accounts.get('dead_session', 0) or 0)}`\n"
            f"Свежая валидация `{int(accounts.get('recently_validated', 0) or 0)}` | avg score `{int(accounts.get('avg_score', 0) or 0)}`\n"
            f"Топ причин: `{categories_text}`"
        )

        if attention_lines:
            text += "\n\n🚨 *Инциденты*\n"
            text += "\n".join(f"• {line}" for line in attention_lines)

        state_mode = _format_monitor_state_label(str(automation_state.get("mode") or "normal"))
        state_reason = escape_markdown_simple(str(automation_state.get("reason") or "Критичных инцидентов нет."))
        pause_until = automation_state.get("pause_until")
        text += (
            "\n\n*Автономия монитора*\n"
            f"Режим: `{escape_markdown_simple(state_mode)}`\n"
            f"Почему: {state_reason}\n"
        )
        if pause_until:
            text += f"Пауза до: `{escape_markdown_simple(str(pause_until))}`\n"

        incident_lines = _format_monitor_incident_lines(incidents)
        if incident_lines:
            text += "\n*Понятные инциденты*\n"
            text += "\n".join(f"• {line}" for line in incident_lines)

        if recommended_actions:
            text += "\n\n*Что делать админу*\n"
            text += "\n".join(
                f"• {escape_markdown_simple(str(action))}"
                for action in recommended_actions[:4]
            )

        await _safe_edit_callback_text(
            callback,
            text,
            reply_markup=get_monitoring_menu_keyboard(),
            parse_mode="Markdown",
        )
    except Exception as e:
        logger.error(f"Error in monitoring_menu: {e}", exc_info=True)
        try:
            await callback.answer("Не удалось загрузить мониторинг", show_alert=True)
        except Exception:
            pass


@router.callback_query(F.data == "admin:monitoring:parallel")
async def monitoring_parallel_settings(callback: CallbackQuery, skip_answer: bool = False):
    """Лимит одновременных браузеров для `python -m services.video_monitor_worker`."""
    try:
        if not skip_answer:
            await callback.answer()
        data = await api_client.get_monitor_parallel_browsers_settings()
        cur = int(data.get("monitor_max_parallel_browsers") or 3)
        env_def = int(data.get("env_default") or 3)
        lo = int(data.get("allowed_min") or 1)
        hi = int(data.get("allowed_max") or 8)
        text = (
            "🖥 *Параллель браузеров (video monitor)*\n"
            "━━━━━━━━━━━━━━━━━━━━\n\n"
            "Сколько блогеров монитор может проверять *одновременно* "
            "(отдельный Playwright на каждого в пределах семафора).\n\n"
            f"Сейчас: `{cur}` (допустимо `{lo}`–`{hi}`)\n"
            f"Значение из `.env`, если в БД нет записи: `{env_def}` (`MONITOR_MAX_PARALLEL_BROWSERS`)\n\n"
            "Изменение сохраняется в базе и подхватывается воркером без перезапуска "
            "(кэш настроек до ~60 сек).\n\n"
            "Меньше — меньше RAM/CPU и нагрузка на прокси. Больше — быстрее разгребать очередь при большом backlog."
        )
        await _safe_edit_callback_text(
            callback,
            text,
            reply_markup=get_monitor_parallel_settings_keyboard(cur),
            parse_mode="Markdown",
        )
    except Exception as e:
        logger.error("monitoring_parallel_settings error: %s", e, exc_info=True)
        if not skip_answer:
            try:
                await callback.answer("Не удалось загрузить настройку", show_alert=True)
            except Exception:
                pass
        else:
            raise


@router.callback_query(F.data.regexp(r"^admin:monitoring:parallel:set:(\d+)$"))
async def monitoring_parallel_set(callback: CallbackQuery):
    match = re.match(r"^admin:monitoring:parallel:set:(\d+)$", callback.data or "")
    if not match:
        await callback.answer()
        return
    value = int(match.group(1))
    try:
        data = await api_client.update_monitor_parallel_browsers_settings(value)
        cur = int(data.get("monitor_max_parallel_browsers") or value)
        await log_admin_action(
            admin_telegram_id=callback.from_user.id,
            action_type="monitor_parallel_browsers",
            target_user_id=None,
            details={"monitor_max_parallel_browsers": cur},
            description=f"monitor_max_parallel_browsers → {cur}",
        )
        try:
            await monitoring_parallel_settings(callback, skip_answer=True)
        except Exception as refresh_err:
            logger.warning("monitor_parallel UI refresh failed after save: %s", refresh_err, exc_info=True)
        await callback.answer(f"Установлено: {cur}", show_alert=False)
    except Exception as e:
        logger.error("monitoring_parallel_set error: %s", e, exc_info=True)
        try:
            await callback.answer("Ошибка сохранения", show_alert=True)
        except Exception:
            pass


@router.callback_query(F.data == "admin:monitoring:delete_all")
async def ask_delete_all_bloggers(callback: CallbackQuery):
    """Спрашивает подтверждение на удаление всех блогеров."""
    await callback.answer()
    keyboard = InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="🚨 Да, удалить всех блогеров", callback_data="admin:monitoring:delete_all:confirm")],
            [InlineKeyboardButton(text="❌ Отмена", callback_data="admin:monitoring")],
        ]
    )
    
    await _safe_edit_callback_text(
        callback,
        "⚠️ *ВНИМАНИЕ*\n\n"
        "Вы собираетесь удалить **всех** блогеров (и все их видео) из базы данных мониторинга. "
        "Это затронет подключения мониторинга всех пользователей системы.\n\n"
        "Это действие невозможно отменить.\n"
        "Продолжить?",
        reply_markup=keyboard,
        parse_mode="Markdown",
    )


@router.callback_query(F.data == "admin:monitoring:delete_all:confirm")
async def confirm_delete_all_bloggers(callback: CallbackQuery):
    """Подтверждает удаление всех блогеров."""
    try:
        await callback.answer("Удаление блогеров...", show_alert=False)
        result = await api_client.delete_all_bloggers()
        
        await log_admin_action(
            callback.from_user.id,
            callback.from_user.username,
            "delete_all_bloggers",
            description="Удалил всех блогеров из базы данных мониторинга"
        )
        
        await _safe_edit_callback_text(
            callback,
            "✅ *Все блогеры успешно удалены из базы.*\n\n"
            "База мониторинга очищена.",
            reply_markup=InlineKeyboardMarkup(
                inline_keyboard=[[InlineKeyboardButton(text="◀️ Назад в мониторинг", callback_data="admin:monitoring")]]
            ),
            parse_mode="Markdown",
        )
    except Exception as e:
        logger.error(f"Failed to delete all bloggers: {e}", exc_info=True)
        try:
            await callback.answer(f"Ошибка при удалении: {e}", show_alert=True)
        except Exception:
            pass


@router.callback_query(F.data == "admin:accounts")
async def accounts_menu(callback: CallbackQuery):
    """Показывает экран управления аккаунтами с runtime-готовностью и фоновыми задачами."""
    try:
        if not callback.message:
            await callback.answer("Ошибка: сообщение не найдено", show_alert=True)
            return

        stats_result, runtime_result, validation_result, recheck_result = await asyncio.gather(
            api_client.get_account_stats(),
            api_client.get_account_runtime_health(),
            api_client.get_validation_progress(),
            api_client.get_recheck_dead_progress(),
            return_exceptions=True,
        )

        stats = {} if isinstance(stats_result, Exception) else (stats_result or {})
        runtime = {} if isinstance(runtime_result, Exception) else (runtime_result or {})
        validation_progress = {} if isinstance(validation_result, Exception) else (validation_result or {})
        recheck_progress = {} if isinstance(recheck_result, Exception) else (recheck_result or {})

        if isinstance(stats_result, Exception):
            logger.error(f"Error loading account stats: {stats_result}", exc_info=True)
        if isinstance(runtime_result, Exception):
            logger.error(f"Error loading runtime health: {runtime_result}", exc_info=True)
        if isinstance(validation_result, Exception):
            logger.error(f"Error loading validation progress: {validation_result}", exc_info=True)
        if isinstance(recheck_result, Exception):
            logger.error(f"Error loading recheck progress: {recheck_result}", exc_info=True)

        total = int(stats.get("total", 0) or 0)
        active = int(stats.get("active", 0) or 0)
        error = int(stats.get("error", 0) or 0)
        dead = int(stats.get("dead", 0) or 0)
        limited = int(stats.get("limited", 0) or 0)
        active_percent = _safe_ratio(active, total)

        main_pool = runtime.get("main_comment", {}) or {}
        reply_pool = runtime.get("reply", {}) or {}
        monitor_pool = runtime.get("monitor", {}) or {}

        validation_current = escape_markdown_simple(str(validation_progress.get("current_account") or "-"))
        recheck_current = escape_markdown_simple(str(recheck_progress.get("current_account") or "-"))

        text = (
            "🔑 *Аккаунты*\n"
            "━━━━━━━━━━━━━━━━━━━━\n\n"
            "📊 *Статусы*\n"
            f"Активные: `{active}` | error `{error}` | dead `{dead}` | limited `{limited}`\n"
            f"Всего: `{total}` | здоровье пула: `{active_percent}%` active\n\n"
            "⚡ *Готовы сейчас*\n"
            f"Main comment: `{int(main_pool.get('eligible_now', 0) or 0)}` из `{int(main_pool.get('total_candidates', 0) or 0)}`\n"
            f"Reply: `{int(reply_pool.get('eligible_now', 0) or 0)}` из `{int(reply_pool.get('total_candidates', 0) or 0)}`\n"
            f"Monitor: `{int(monitor_pool.get('eligible_now', 0) or 0)}` из `{int(monitor_pool.get('total_candidates', 0) or 0)}`\n\n"
            "🧠 *Session quality*\n"
            f"Main: {_format_runtime_bucket_summary(main_pool)}\n"
            f"Reply: {_format_runtime_bucket_summary(reply_pool)}\n"
            f"Monitor: {_format_runtime_bucket_summary(monitor_pool)}\n\n"
            "⛽ *Расход аккаунтов*\n"
            f"Main: {_format_lane_summary(main_pool)}\n"
            f"Reply: {_format_lane_summary(reply_pool)}\n"
            f"Monitor: {_format_lane_summary(monitor_pool)}\n\n"
            "🛠 *HTTP strategy*\n"
            f"Main: {_format_strategy_summary(main_pool)}\n"
            f"Reply: {_format_strategy_summary(reply_pool)}\n"
            f"Monitor: {_format_strategy_summary(monitor_pool)}\n\n"
            "🔄 *Фоновые задачи*\n"
            f"Валидация: `{_format_validation_progress(validation_progress)}`\n"
            f"Сейчас: `{validation_current}`\n"
            f"Recheck dead: `{_format_recheck_progress(recheck_progress)}`\n"
            f"Сейчас: `{recheck_current}`"
        )

        await _safe_edit_callback_text(
            callback,
            text,
            reply_markup=get_accounts_menu_keyboard(),
            parse_mode="Markdown",
        )
    except Exception as e:
        logger.error(f"Error getting account stats: {e}", exc_info=True)
        try:
            await _safe_edit_callback_text(
                callback,
                "❌ Не удалось загрузить экран аккаунтов",
                reply_markup=get_accounts_menu_keyboard(),
            )
        except Exception:
            pass


@router.callback_query(F.data == "admin:accounts:health")
async def accounts_runtime_health_menu(callback: CallbackQuery):
    """Показывает расширенное runtime-здоровье main/reply/monitor пулов."""
    try:
        await callback.answer()
        runtime = await api_client.get_account_runtime_health()
        main_pool = runtime.get("main_comment", {}) or {}
        reply_pool = runtime.get("reply", {}) or {}
        monitor_pool = runtime.get("monitor", {}) or {}

        def _render_pool(title: str, pool: dict) -> str:
            top_categories = pool.get("top_categories", []) or []
            categories_text = "нет явных runtime-проблем"
            if top_categories:
                categories_text = ", ".join(
                    f"{_humanize_monitor_category(item.get('category'))}: {item.get('count', 0)}"
                    for item in top_categories[:3]
                )
            return (
                f"*{title}*\n"
                f"Ready now: `{int(pool.get('eligible_now', 0) or 0)}` / `{int(pool.get('total_candidates', 0) or 0)}`\n"
                f"Healthy `{int(pool.get('healthy', 0) or 0)}` | degraded `{int(pool.get('degraded', 0) or 0)}` | cooldown `{int(pool.get('cooldown', 0) or 0)}`\n"
                f"Proxy issue `{int(pool.get('proxy_issue', 0) or 0)}` | bad proxies `{int(pool.get('bad_proxies', 0) or 0)}` | dead session `{int(pool.get('dead_session', 0) or 0)}`\n"
                f"Свежая валидация `{int(pool.get('recently_validated', 0) or 0)}` | avg runtime `{int(pool.get('avg_score', 0) or 0)}` | avg session `{int(pool.get('avg_session_score', 0) or 0)}`\n"
                f"Session buckets: {_format_runtime_bucket_summary(pool)}\n"
                f"Spend lanes: {_format_lane_summary(pool)}\n"
                f"HTTP strategy: {_format_strategy_summary(pool)}\n"
                f"Top buckets: {_top_session_bucket_text(pool)}\n"
                f"Top issues: {_top_session_issue_text(pool)}\n"
                f"Top runtime reasons: {categories_text}"
            )

        text = (
            "🧠 *Runtime здоровье аккаунтов*\n"
            "━━━━━━━━━━━━━━━━━━━━\n\n"
            f"{_render_pool('Main comment', main_pool)}\n\n"
            f"{_render_pool('Reply', reply_pool)}\n\n"
            f"{_render_pool('Monitor', monitor_pool)}"
        )

        await _safe_edit_callback_text(
            callback,
            text,
            reply_markup=get_accounts_menu_keyboard(),
            parse_mode="Markdown",
        )
    except Exception as e:
        logger.error(f"Error in accounts_runtime_health_menu: {e}", exc_info=True)
        try:
            await callback.answer("Не удалось загрузить runtime здоровье аккаунтов", show_alert=True)
        except Exception:
            pass


@router.callback_query(F.data == "admin:accounts:import")
async def start_import(callback: CallbackQuery, state: FSMContext):
    """Начинает импорт аккаунтов."""
    try:
        if not callback.message:
            await callback.answer("Ошибка: сообщение не найдено", show_alert=True)
            return
        
        # ИСПРАВЛЕНО: Убрана дублирующая проверка прав - middleware уже проверяет
        
        await callback.message.edit_text(
        "📥 *Импорт аккаунтов*\n\n"
        "Отправьте файл с аккаунтами.\n"
        "Формат: одна строка = один аккаунт\n"
        "Пример: `username:password` или `username:password:email`",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text=f"{Emoji.CANCEL} Отмена", callback_data="admin:accounts")]
        ]),
        parse_mode="Markdown"
    )
        await state.set_state(OrderStates.INPUT_ACCOUNTS_FILE)
        await callback.answer()
    except Exception as e:
        logger.error(f"Error starting import: {e}", exc_info=True)
        try:
            await callback.answer("Ошибка", show_alert=True)
        except Exception:
            pass


@router.message(StateFilter(OrderStates.INPUT_ACCOUNTS_FILE), F.document)
async def process_accounts_file(message: Message, state: FSMContext):
    """Обрабатывает файл с аккаунтами."""
    # ИСПРАВЛЕНО: Убрана дублирующая проверка прав - middleware уже проверяет
    
    try:
        # ИСПРАВЛЕНО: В aiogram 3.x используем download_file вместо file.download()
        file = await message.bot.get_file(message.document.file_id)
        file_bytes = await message.bot.download_file(file.file_path)
        
        # Читаем содержимое из BytesIO
        content = file_bytes.read().decode("utf-8", errors="ignore")
        
        parsed = parse_accounts_file_smart(content)
        
        if not parsed.accounts:
            await message.answer(f"{Emoji.ERROR} Не найдено валидных аккаунтов в файле.")
            await state.clear()
            return
        
        # ИСПРАВЛЕНО: Преобразуем объекты ParsedAccountSmart в словари для JSON-сериализации
        accounts_dicts = [asdict(acc) for acc in parsed.accounts]
        accounts_without_proxy_dicts = [asdict(acc) for acc in (parsed.accounts_without_proxy or [])]
        
        await state.update_data(
            parsed_accounts=accounts_dicts,
            accounts_without_proxy=accounts_without_proxy_dicts,
            parse_errors=parsed.errors or [],
            parse_duplicates=parsed.duplicates or 0,
            parse_total_lines=parsed.total_lines or 0
        )

        text = _build_import_preview_text(
            parsed_accounts=accounts_dicts,
            accounts_without_proxy=accounts_without_proxy_dicts,
            parse_errors=parsed.errors or [],
            parse_duplicates=parsed.duplicates or 0,
            total_lines=parsed.total_lines or 0,
        )

        await message.answer(
            text,
            reply_markup=get_import_confirm_keyboard(len(accounts_without_proxy_dicts) > 0)
        )
        
    except Exception as e:
        logger.error(f"Error processing accounts file: {e}", exc_info=True)
        await message.answer(
            f"{Emoji.ERROR} Не удалось обработать файл.\n"
            "Проверьте формат и попробуйте снова."
        )
        await state.clear()


@router.callback_query(F.data == "admin:import:check")
async def check_import_errors(callback: CallbackQuery, state: FSMContext):
    """Показывает ошибки парсинга перед подтверждением импорта."""
    try:
        if not callback.message:
            await callback.answer("Ошибка: сообщение не найдено", show_alert=True)
            return

        await callback.answer()
        data = await state.get_data()
        errors = data.get("parse_errors", []) or []
        duplicates = int(data.get("parse_duplicates", 0) or 0)
        total_lines = int(data.get("parse_total_lines", 0) or 0)
        parsed_accounts = data.get("parsed_accounts", []) or []
        accounts_without_proxy = data.get("accounts_without_proxy", []) or []

        if not errors and duplicates == 0:
            try:
                await callback.answer("Ошибок разбора не найдено", show_alert=True)
            except Exception:
                pass
            return

        text = "*Результат проверки файла*\n\n"
        text += f"✅ Валидных аккаунтов: {len(parsed_accounts)}\n"
        text += f"⚠️ Ошибок разбора: {len(errors)}\n"
        text += f"♻️ Дубликатов: {duplicates}\n"
        if total_lines:
            text += f"📄 Всего строк: {total_lines}\n"
        if accounts_without_proxy:
            text += f"🌐 Без прокси: {len(accounts_without_proxy)}\n"

        if errors:
            text += "\n*Первые ошибки:*\n"
            for idx, error in enumerate(errors[:8], start=1):
                line_num = error.get("line_number", "?")
                reason = str(error.get("reason", "Неизвестная ошибка")).strip()
                if len(reason) > 100:
                    reason = reason[:97] + "..."
                text += f"{idx}. Строка {line_num}: {reason}\n"
            if len(errors) > 8:
                text += f"... и ещё {len(errors) - 8}\n"

        await callback.message.edit_text(
            text,
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="◀️ К импорту", callback_data="admin:import:back")],
                [InlineKeyboardButton(text="❌ Отмена", callback_data="admin:accounts")]
            ]),
            parse_mode="Markdown"
        )
    except Exception as e:
        logger.error(f"Error showing import check: {e}", exc_info=True)
        await callback.answer("Ошибка проверки", show_alert=True)


@router.callback_query(F.data == "admin:import:back")
async def back_to_import_preview(callback: CallbackQuery, state: FSMContext):
    """Возвращает экран подтверждения импорта после просмотра ошибок."""
    try:
        if not callback.message:
            await callback.answer("Ошибка: сообщение не найдено", show_alert=True)
            return

        await callback.answer()
        data = await state.get_data()
        parsed_accounts = data.get("parsed_accounts", []) or []
        accounts_without_proxy = data.get("accounts_without_proxy", []) or []
        parse_errors = data.get("parse_errors", []) or []
        parse_duplicates = int(data.get("parse_duplicates", 0) or 0)
        parse_total_lines = int(data.get("parse_total_lines", 0) or 0)

        if not parsed_accounts:
            try:
                await callback.answer("Нет данных для импорта", show_alert=True)
            except Exception:
                pass
            return

        text = _build_import_preview_text(
            parsed_accounts=parsed_accounts,
            accounts_without_proxy=accounts_without_proxy,
            parse_errors=parse_errors,
            parse_duplicates=parse_duplicates,
            total_lines=parse_total_lines,
        )
        await callback.message.edit_text(
            text,
            reply_markup=get_import_confirm_keyboard(len(accounts_without_proxy) > 0)
        )
    except Exception as e:
        logger.error(f"Error returning to import preview: {e}", exc_info=True)
        await callback.answer("Ошибка возврата", show_alert=True)


@router.callback_query(F.data == "admin:import:confirm")
async def confirm_import(callback: CallbackQuery, state: FSMContext):
    """Подтверждает импорт аккаунтов."""
    try:
        if not callback.message:
            await callback.answer("Ошибка: сообщение не найдено", show_alert=True)
            return
        
        # ИСПРАВЛЕНО: Убрана дублирующая проверка прав - middleware уже проверяет
        
        data = await state.get_data()
        parsed_accounts = data.get("parsed_accounts", [])
        
        if not parsed_accounts:
            await callback.answer("Нет данных для импорта", show_alert=True)
            return
        
        # ИСПРАВЛЕНО: Сразу отвечаем на callback query, чтобы избежать истечения таймаута
        await callback.answer()
        await callback.message.edit_text("⏳ Импорт аккаунтов...")
        
        # Преобразуем словари (из FSM state) в формат для API
        accounts_data = [
            {
                "label": acc.get("label"),
                "cookies": acc.get("cookies"),
                "cookie_file": acc.get("cookie_file"),
                "user_agent": acc.get("user_agent"),
                "proxy": acc.get("proxy")
            }
            for acc in parsed_accounts
        ]
        
        result = await api_client.bulk_import_accounts(accounts_data)
        imported = result.get("added", 0)  # API возвращает "added", а не "imported"
        updated = result.get("updated", 0)
        skipped = result.get("skipped", 0)
        duplicates_by_cookies = result.get("duplicates_by_cookies", 0)
        assigned_proxies = result.get("assigned_proxies", 0)
        without_proxies = result.get("without_proxies", 0)
        errors = result.get("errors", [])
        
        text = f"{Emoji.SUCCESS} *Импорт завершён*\n"
        text += "━━━━━━━━━━━━━━━━━━━━\n\n"
        text += f"📥 Импортировано: `{imported}`\n"
        text += f"🔄 Обновлено: `{updated}`\n"
        if skipped > 0:
            text += f"⏭️ Пропущено: `{skipped}`\n"
        if duplicates_by_cookies > 0:
            text += f"🔁 Дубликаты (одинаковые cookies): `{duplicates_by_cookies}`\n"
        text += f"📦 Всего обработано: `{imported + updated}`\n"
        if assigned_proxies > 0:
            text += f"\n🔗 Автоназначено прокси: `{assigned_proxies}`\n"
        if without_proxies > 0:
            text += f"{Emoji.WARNING} Без прокси: `{without_proxies}`"
        if errors:
            text += f"\n\n⚠️ Ошибок: `{len(errors)}`"
            if len(errors) <= 5:
                text += "\n\n*Детали ошибок:*\n"
                for err in errors[:5]:
                    text += f"• {err[:50]}\n"
        
        await callback.message.edit_text(
            text,
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="🔄 Проверить аккаунты", callback_data="admin:accounts:validate")],
                [InlineKeyboardButton(text="◀️ Назад", callback_data="admin:accounts")]
            ]),
            parse_mode="Markdown"
        )
        await state.clear()
    except Exception as e:
        logger.error(f"Error importing accounts: {e}", exc_info=True)
        try:
            if callback.message:
                await callback.message.edit_text(
                    "❌ Ошибка импорта.\n\n"
                    "Проверьте данные и попробуйте снова."
                )
        except Exception as e2:
            logger.error(f"Error editing message: {e2}", exc_info=True)


@router.callback_query(F.data == "admin:import:confirm_and_assign")
async def confirm_import_and_assign(callback: CallbackQuery, state: FSMContext):
    """Подтверждает импорт аккаунтов и автоматически назначает прокси."""
    try:
        if not callback.message:
            await callback.answer("Ошибка: сообщение не найдено", show_alert=True)
            return
        
        # ИСПРАВЛЕНО: Убрана дублирующая проверка прав - middleware уже проверяет
        
        data = await state.get_data()
        parsed_accounts = data.get("parsed_accounts", [])
        
        if not parsed_accounts:
            await callback.answer("Нет данных для импорта", show_alert=True)
            return
        
        # ИСПРАВЛЕНО: Сразу отвечаем на callback query, чтобы избежать истечения таймаута
        await callback.answer()
        await callback.message.edit_text("⏳ Импорт аккаунтов и назначение прокси...")
        
        # Преобразуем словари (из FSM state) в формат для API
        accounts_data = [
            {
                "label": acc.get("label"),
                "cookies": acc.get("cookies"),
                "cookie_file": acc.get("cookie_file"),
                "user_agent": acc.get("user_agent"),
                "proxy": acc.get("proxy")
            }
            for acc in parsed_accounts
        ]
        
        # ИСПРАВЛЕНО: Улучшена обработка долгих операций - запускаем в фоне
        import asyncio
        import time
        
        start_time = time.time()
        import_task = asyncio.create_task(api_client.bulk_import_accounts(accounts_data))
        
        # Показываем прогресс каждые 5 секунд
        while not import_task.done():
            await asyncio.sleep(5)
            elapsed = int(time.time() - start_time)
            try:
                if callback.message:
                    await callback.message.edit_text(
                        f"⏳ *Импорт аккаунтов*\n\n"
                        f"📥 Обрабатываю {len(accounts_data)} аккаунтов...\n"
                        f"⏱ Прошло: {elapsed} сек\n\n"
                        f"💡 Это может занять несколько минут",
                        parse_mode="Markdown"
                    )
            except Exception:
                pass  # Игнорируем ошибки обновления
        
        # Импортируем аккаунты
        result = await import_task
        imported = result.get("added", 0)
        updated = result.get("updated", 0)
        skipped = result.get("skipped", 0)
        duplicates_by_cookies = result.get("duplicates_by_cookies", 0)
        assigned_proxies = result.get("assigned_proxies", 0)
        without_proxies = result.get("without_proxies", 0)
        errors = result.get("errors", [])
        
        # Назначаем прокси аккаунтам без прокси
        additional_assigned = 0
        if without_proxies > 0:
            try:
                await callback.message.edit_text("⏳ Назначение прокси аккаунтам без прокси...")
                assign_result = await api_client.assign_proxies(only_accounts_without_proxy=True, reuse_existing=False)
                additional_assigned = assign_result.get("assigned", 0)
            except Exception as assign_err:
                logger.error(f"Error assigning proxies: {assign_err}", exc_info=True)
        
        text = f"{Emoji.SUCCESS} *Импорт и назначение прокси завершены*\n"
        text += "━━━━━━━━━━━━━━━━━━━━\n\n"
        text += f"📥 Добавлено новых: `{imported}`\n"
        text += f"🔄 Обновлено существующих: `{updated}`\n"
        if skipped > 0:
            text += f"⏭️ Пропущено: `{skipped}`\n"
        if duplicates_by_cookies > 0:
            text += f"🔁 Дубликаты (одинаковые cookies): `{duplicates_by_cookies}`\n"
        text += f"📦 Всего обработано: `{imported + updated + skipped + duplicates_by_cookies}`\n"
        if assigned_proxies > 0:
            text += f"\n🔗 Прокси назначено при импорте: `{assigned_proxies}`\n"
        if additional_assigned > 0:
            text += f"🔗 Прокси назначено дополнительно: `{additional_assigned}`\n"
        if without_proxies > additional_assigned:
            remaining = without_proxies - additional_assigned
            text += f"{Emoji.WARNING} Осталось без прокси: `{remaining}`\n"
        if errors:
            text += f"\n\n⚠️ Ошибок: `{len(errors)}`"
            if len(errors) <= 5:
                text += "\n\n*Детали ошибок:*\n"
                for err in errors[:5]:
                    text += f"• {err[:50]}\n"
        
        await callback.message.edit_text(
            text,
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="🔄 Проверить аккаунты", callback_data="admin:accounts:validate")],
                [InlineKeyboardButton(text="◀️ Назад", callback_data="admin:accounts")]
            ]),
            parse_mode="Markdown"
        )
        await state.clear()
    except Exception as e:
        logger.error(f"Error importing accounts and assigning proxies: {e}", exc_info=True)
        try:
            if callback.message:
                await callback.message.edit_text(
                    "❌ Ошибка импорта.\n\n"
                    "Проверьте данные и попробуйте снова."
                )
        except Exception as e2:
            logger.error(f"Error editing message: {e2}", exc_info=True)


@router.callback_query(F.data == "admin:accounts:list")
async def accounts_list_handler(callback: CallbackQuery):
    """Показывает список аккаунтов."""
    await show_accounts_page(callback, page=0)


@router.callback_query(F.data == "admin:accounts:make_all_active")
async def make_all_accounts_active_handler(callback: CallbackQuery):
    """Устанавливает статус 'active' для всех аккаунтов."""
    try:
        if not callback.message:
            await callback.answer("Ошибка: сообщение не найдено", show_alert=True)
            return
        
        # Показываем сообщение о начале операции
        await callback.message.edit_text(
            "⏳ Устанавливаю статус 'active' для всех аккаунтов...",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="◀️ Назад", callback_data="admin:accounts")]
            ])
        )
        await callback.answer()
        
        # Вызываем API
        result = await api_client.make_all_accounts_active()
        
        if result.get("success"):
            total = result.get("total_accounts", 0)
            updated = result.get("updated_count", 0)

            text = f"{Emoji.SUCCESS} *Все аккаунты активированы*\n"
            text += "━━━━━━━━━━━━━━━━━━━━\n\n"
            text += f"📦 Всего аккаунтов: `{total}`\n"
            text += f"🔄 Восстановлено статусов: `{updated}`\n\n"
            text += "Сброшено:\n"
            text += "• счётчик ошибок\n"
            text += "• счётчик неудач валидации\n"
            text += "• текст последней ошибки"
            
            await callback.message.edit_text(
                text,
                reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                    [InlineKeyboardButton(text="◀️ Назад", callback_data="admin:accounts")]
                ]),
                parse_mode="Markdown"
            )
        else:
            await callback.message.edit_text(
                f"{Emoji.ERROR} Ошибка при обновлении аккаунтов",
                reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                    [InlineKeyboardButton(text="◀️ Назад", callback_data="admin:accounts")]
                ])
            )
    except Exception as e:
        logger.error(f"Error making all accounts active: {e}", exc_info=True)
        try:
            if callback.message:
                await callback.message.edit_text(
                    f"{Emoji.ERROR} Ошибка: {str(e)[:200]}",
                    reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                        [InlineKeyboardButton(text="◀️ Назад", callback_data="admin:accounts")]
                    ])
                )
            await callback.answer("Ошибка", show_alert=True)
        except Exception:
            pass


@router.callback_query(F.data == "admin:accounts:reset_cooldowns")
async def reset_cooldowns_handler(callback: CallbackQuery):
    """Сбрасывает runtime cooldown'ы аккаунтов и bad-proxy кеш."""
    try:
        if not callback.message:
            await callback.answer("Ошибка: сообщение не найдено", show_alert=True)
            return

        await callback.message.edit_text(
            "⏳ Сбрасываю cooldown'ы аккаунтов и bad-proxy кеш...",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="◀️ Назад", callback_data="admin:accounts")]
            ]),
        )
        await callback.answer()

        result = await api_client.reset_account_cooldowns()

        if result.get("success"):
            cooldowns = int(result.get("cooldowns_cleared", 0) or 0)
            errors = int(result.get("errors_reset", 0) or 0)
            statuses = int(result.get("statuses_restored", 0) or 0)
            proxies = int(result.get("proxy_keys_cleared", 0) or 0)
            total = int(result.get("total_accounts", 0) or 0)

            text = f"{Emoji.SUCCESS} *Cooldown'ы сброшены*\n"
            text += "━━━━━━━━━━━━━━━━━━━━\n\n"
            text += f"📦 Обработано аккаунтов: `{total}`\n"
            text += f"⏳ Cooldown'ов очищено: `{cooldowns}`\n"
            text += f"🔢 Счётчиков ошибок сброшено: `{errors}`\n"
            text += f"🔄 Статусов error→active: `{statuses}`\n"
            text += f"🌐 Bad-proxy ключей в Redis: `{proxies}`\n\n"
            text += "💡 Dead аккаунты не затронуты.\n"
            text += "Аккаунты снова доступны для выборки."

            await callback.message.edit_text(
                text,
                reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                    [InlineKeyboardButton(text="◀️ Назад", callback_data="admin:accounts")]
                ]),
                parse_mode="Markdown",
            )
        else:
            await callback.message.edit_text(
                f"{Emoji.ERROR} Ошибка при сбросе cooldown'ов",
                reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                    [InlineKeyboardButton(text="◀️ Назад", callback_data="admin:accounts")]
                ]),
            )
    except Exception as e:
        logger.error(f"Error resetting cooldowns: {e}", exc_info=True)
        try:
            if callback.message:
                await callback.message.edit_text(
                    f"{Emoji.ERROR} Ошибка: {str(e)[:200]}",
                    reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                        [InlineKeyboardButton(text="◀️ Назад", callback_data="admin:accounts")]
                    ]),
                )
            await callback.answer("Ошибка", show_alert=True)
        except Exception:
            pass


async def show_accounts_page(callback: CallbackQuery, page: int = 0):
    """ИСПРАВЛЕНО: Показывает страницу со списком аккаунтов с серверной пагинацией."""
    # ИСПРАВЛЕНО: Убрана дублирующая проверка прав - middleware уже проверяет
    
    try:
        # ИСПРАВЛЕНО: Используем серверную пагинацию вместо загрузки всех записей
        ACCOUNTS_PER_PAGE = 10
        offset = page * ACCOUNTS_PER_PAGE
        accounts = await api_client.list_accounts(limit=ACCOUNTS_PER_PAGE, offset=offset)
        
        if not accounts:
            if callback.message:
                await callback.message.edit_text(
                    "Нет аккаунтов",
                    reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                        [InlineKeyboardButton(text="◀️ Назад", callback_data="admin:accounts")]
                    ])
                )
            await callback.answer()
            return
        
        text = "🔑 *Список аккаунтов*\n"
        text += "━━━━━━━━━━━━━━━━━━━━\n\n"
        text += f"📄 Страница: `{page + 1}`\n\n"
        
        if not accounts:
            text += "📭 Нет аккаунтов"
        else:
            for idx, acc in enumerate(accounts[:10], 1):
                # ИСПРАВЛЕНО: Используем константы Emoji
                status_emoji_map = {
                    "active": Emoji.SUCCESS,
                    "error": Emoji.WARNING,
                    "dead": "💀"  # Специальный эмодзи для dead
                }
                status_emoji = status_emoji_map.get(acc.get("status", "unknown"), "❓")
                label = acc.get("label", "N/A")[:25]
                country_code = acc.get("country_code", "")
                country_emoji = f" {country_code}" if country_code else ""
                text += f"`{idx:2}.` {status_emoji} {label}{country_emoji}\n"
        
        keyboard = []
        if page > 0:
            keyboard.append([InlineKeyboardButton(text="◀️ Назад", callback_data=f"admin:accounts:page:{page-1}")])
        if len(accounts) == 10:
            keyboard.append([InlineKeyboardButton(text="Вперёд ▶️", callback_data=f"admin:accounts:page:{page+1}")])
        keyboard.append([InlineKeyboardButton(text="◀️ Меню", callback_data="admin:accounts")])
        
        await _safe_edit_callback_text(
            callback,
            text,
            reply_markup=InlineKeyboardMarkup(inline_keyboard=keyboard),
            parse_mode="Markdown"
        )
        await callback.answer()
    except Exception as e:
        logger.error(f"Error showing accounts list: {e}")
        await callback.answer("Ошибка загрузки списка", show_alert=True)


@router.callback_query(F.data.startswith("admin:accounts:page:"))
async def show_accounts_page_handler(callback: CallbackQuery):
    """Обрабатывает переход по страницам списка аккаунтов."""
    page = int(callback.data.split(":")[-1])
    await show_accounts_page(callback, page)


@router.callback_query(F.data == "admin:accounts:validate")
async def validate_accounts_handler(callback: CallbackQuery):
    """Запускает валидацию всех аккаунтов."""
    # ИСПРАВЛЕНО: Убрана дублирующая проверка прав - middleware уже проверяет
    
    await callback.message.edit_text("⏳ Проверка аккаунтов...")
    await callback.answer()
    
    try:
        result = await api_client.validate_accounts()
        checked = result.get("checked", 0)
        valid = result.get("valid", 0)
        invalid = result.get("invalid", 0)
        message = result.get("message")  # Сообщение, если нет аккаунтов для проверки
        
        # ИСПРАВЛЕНО: Показываем понятное сообщение, если аккаунтов нет
        if checked == 0 and message:
            text = f"{Emoji.WARNING} *Проверка не выполнена*\n"
            text += "━━━━━━━━━━━━━━━━━━━━\n\n"
            text += f"{message}\n\n"
            text += "*Что делать:*\n"
            text += "• Если аккаунтов нет - добавьте их через импорт\n"
            text += "• Если все аккаунты в статусе 'dead' - используйте 'Перепроверить 💀'\n"
            text += "• Проверяются только аккаунты со статусом 'active' или 'error'"
            
            await callback.message.edit_text(
                text,
                reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                    [InlineKeyboardButton(text="🔄 Перепроверить 💀", callback_data="admin:accounts:recheck_dead")],
                    [InlineKeyboardButton(text="➕ Импорт аккаунтов", callback_data="admin:accounts:import")],
                    [InlineKeyboardButton(text="◀️ Назад", callback_data="admin:accounts")]
                ]),
                parse_mode="Markdown"
            )
            return
        
        text = f"{Emoji.SUCCESS} *Проверка завершена*\n"
        text += "━━━━━━━━━━━━━━━━━━━━\n\n"
        text += f"📊 Проверено: `{checked}`\n"
        text += f"{Emoji.SUCCESS} Валидных: `{valid}`\n"
        text += f"{Emoji.ERROR} Невалидных: `{invalid}`\n\n"
        
        if checked > 0:
            valid_percent = int((valid / checked) * 100)
            health_emoji = "🟢" if valid_percent >= 80 else "🟡" if valid_percent >= 50 else "🔴"
            text += f"{health_emoji} *Здоровье:* `{valid_percent}%` валидных"
        
        await callback.message.edit_text(
            text,
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="🔄 Перепроверить 💀", callback_data="admin:accounts:recheck_dead")],
                [InlineKeyboardButton(text="◀️ Назад", callback_data="admin:accounts")]
            ]),
            parse_mode="Markdown"
        )
    except Exception as e:
        logger.error(f"Error validating accounts: {e}")
        await callback.message.edit_text(f"{Emoji.ERROR} Ошибка проверки: {str(e)[:200]}")


@router.callback_query(F.data == "admin:accounts:assign_proxies")
async def assign_proxies_manual(callback: CallbackQuery):
    """Назначает прокси аккаунтам вручную."""
    try:
        if not callback.message:
            await callback.answer("Ошибка: сообщение не найдено", show_alert=True)
            return
        
        # ИСПРАВЛЕНО: Убрана дублирующая проверка прав - middleware уже проверяет
        
        await callback.message.edit_text("⏳ Назначение прокси...")
        await callback.answer()
        
        result = await api_client.assign_proxies(only_accounts_without_proxy=True, reuse_existing=False)
        assigned = result.get("assigned", 0)
        
        text = f"{Emoji.SUCCESS} *Назначение прокси*\n"
        text += "━━━━━━━━━━━━━━━━━━━━\n\n"
        text += f"🌐 Назначено: `{assigned}` аккаунтам"
        
        if assigned > 0:
            text += "\n\n💡 *Рекомендация:* проверьте гео-совпадения в разделе 'Гео'"
        
        await callback.message.edit_text(
            text,
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="🌍 Проверить гео", callback_data="admin:geo")],
                [InlineKeyboardButton(text="◀️ Назад", callback_data="admin:accounts")]
            ]),
            parse_mode="Markdown"
        )
        await callback.answer(f"{Emoji.SUCCESS} Прокси назначены")
    except Exception as e:
        logger.error(f"Error assigning proxies: {e}", exc_info=True)
        try:
            if callback.message:
                await callback.message.edit_text(f"❌ Ошибка: {str(e)[:200]}")
            await callback.answer()
        except Exception:
            pass


@router.callback_query(F.data == "admin:accounts:pool_diag")
async def account_pool_diagnostics(callback: CallbackQuery):
    """Показывает диагностику пула аккаунтов для main-comment."""
    try:
        await callback.answer()
        diag = await api_client.get_account_pool_diagnostics()

        notes = diag.get("notes") or []
        notes_text = "\n".join(f"• {note}" for note in notes[:6]) if notes else "• Критичных заметок нет"

        text = (
            "📊 *Пул аккаунтов для main comment*\n"
            "━━━━━━━━━━━━━━━━━━━━\n\n"
            f"🧪 Тестовый режим: `{'ON' if diag.get('test_mode') else 'OFF'}`\n"
            f"USE_PROXY: `{'ON' if diag.get('use_proxy') else 'OFF'}` | GEO: `{'ON' if diag.get('proxy_geo_matching') else 'OFF'}`\n"
            f"proxy_type_filter: `{diag.get('proxy_type_filter') or 'none'}`\n"
            f"prefer_mobile: `{'да' if diag.get('proxy_prefer_mobile') else 'нет'}`\n\n"
            "👤 *Аккаунты*\n"
            f"Всего: `{diag.get('total_accounts', 0)}` | active: `{diag.get('active_accounts', 0)}` | error-low: `{diag.get('error_accounts_low', 0)}`\n"
            f"С proxy: `{diag.get('with_proxy', 0)}` | без proxy: `{diag.get('without_proxy', 0)}` | auth material: `{diag.get('with_auth_material', 0)}`\n\n"
            "🌐 *Прокси*\n"
            f"Всего рабочих/unknown: `{diag.get('proxy_total', 0)}`\n"
            f"mobile: `{diag.get('proxy_mobile', 0)}` | residential: `{diag.get('proxy_residential', 0)}`\n\n"
            "🎯 *Кандидаты*\n"
            f"До proxy-фильтра: `{diag.get('main_sql_candidates', 0)}`\n"
            f"После proxy-фильтра: `{diag.get('main_sql_candidates_after_proxy', 0)}`\n\n"
            "🧾 *Заметки*\n"
            f"{notes_text}"
        )

        keyboard = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="🧪 Тестовый режим", callback_data="admin:accounts:test_mode")],
            [InlineKeyboardButton(text="🔄 Обновить", callback_data="admin:accounts:pool_diag")],
            [InlineKeyboardButton(text="◀️ Назад", callback_data="admin:accounts")],
        ])
        await _safe_edit_callback_text(callback, text, reply_markup=keyboard, parse_mode="Markdown")
    except Exception as e:
        logger.error(f"Error showing account pool diagnostics: {e}", exc_info=True)
        await callback.answer("Ошибка диагностики пула", show_alert=True)


@router.callback_query(F.data == "admin:accounts:test_mode")
async def account_test_mode_menu(callback: CallbackQuery):
    """Меню тестового режима аккаунтов/прокси."""
    try:
        await callback.answer()
        status = await api_client.get_account_test_mode()
        enabled = bool(status.get("enabled"))

        text = (
            "🧪 *Тестовый режим аккаунтов/прокси*\n"
            "━━━━━━━━━━━━━━━━━━━━\n\n"
            f"Статус: `{'ВКЛЮЧЕН' if enabled else 'ВЫКЛЮЧЕН'}`\n"
            f"proxy_type_filter: `{status.get('proxy_type_filter') or 'none'}`\n"
            f"prefer_mobile: `{'да' if status.get('proxy_prefer_mobile') else 'нет'}`\n\n"
            "Что делает режим ON:\n"
            "• выключает жёсткий proxy_type_filter\n"
            "• выключает prefer_mobile\n"
            "• назначает residential proxy аккаунтам без proxy\n"
            "• временно разрешает один residential proxy на несколько аккаунтов\n\n"
            "Это режим диагностики, не продакшен. После теста лучше выключить."
        )

        keyboard = InlineKeyboardMarkup(inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="🔴 Выключить тестовый режим" if enabled else "🟢 Включить тестовый режим",
                    callback_data=f"admin:accounts:test_mode:{'off' if enabled else 'on'}",
                )
            ],
            [InlineKeyboardButton(text="📊 Пул аккаунтов", callback_data="admin:accounts:pool_diag")],
            [InlineKeyboardButton(text="◀️ Назад", callback_data="admin:accounts")],
        ])
        await _safe_edit_callback_text(callback, text, reply_markup=keyboard, parse_mode="Markdown")
    except Exception as e:
        logger.error(f"Error showing account test mode menu: {e}", exc_info=True)
        await callback.answer("Ошибка загрузки тестового режима", show_alert=True)


@router.callback_query(F.data.startswith("admin:accounts:test_mode:"))
async def toggle_account_test_mode(callback: CallbackQuery):
    """Включает/выключает тестовый режим аккаунтов/прокси."""
    try:
        action = callback.data.split(":")[-1]
        enabled = action == "on"
        await callback.answer("Применяю настройки...")
        result = await api_client.set_account_test_mode(enabled)
        assignment = result.get("assignment") or {}

        if enabled:
            text = (
                "🧪 *Тестовый режим включён*\n"
                "━━━━━━━━━━━━━━━━━━━━\n\n"
                "Настройки поставлены в тестовый режим.\n"
                f"Назначено proxy: `{assignment.get('assigned', 0)}`\n"
                f"Аккаунтов без proxy было: `{assignment.get('total_accounts_without_proxy', 0)}`\n\n"
                "Теперь открой `📊 Пул аккаунтов` и проверь, выросло ли число кандидатов."
            )
        else:
            text = (
                "✅ *Тестовый режим выключен*\n"
                "━━━━━━━━━━━━━━━━━━━━\n\n"
                "Proxy-настройки восстановлены из бэкапа, если он был сохранён."
            )

        keyboard = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="📊 Пул аккаунтов", callback_data="admin:accounts:pool_diag")],
            [InlineKeyboardButton(text="🧪 Меню тестового режима", callback_data="admin:accounts:test_mode")],
            [InlineKeyboardButton(text="◀️ Назад", callback_data="admin:accounts")],
        ])
        await _safe_edit_callback_text(callback, text, reply_markup=keyboard, parse_mode="Markdown")
    except Exception as e:
        logger.error(f"Error toggling account test mode: {e}", exc_info=True)
        await callback.answer("Ошибка переключения тестового режима", show_alert=True)


@router.callback_query(F.data == "admin:accounts:recheck_dead")
async def recheck_dead_accounts_handler(callback: CallbackQuery):
    """Перепроверяет мёртвые аккаунты."""
    try:
        if not callback.message:
            await callback.answer("Ошибка: сообщение не найдено", show_alert=True)
            return
        
        # ИСПРАВЛЕНО: Убрана дублирующая проверка прав - middleware уже проверяет
        
        await callback.message.edit_text("⏳ Перепроверка мёртвых аккаунтов...")
        await callback.answer()
        
        result = await api_client.recheck_dead_accounts()
        checked = result.get("checked", 0)
        revived = result.get("revived", 0)
        
        text = f"{Emoji.SUCCESS} *Перепроверка завершена*\n"
        text += "━━━━━━━━━━━━━━━━━━━━\n\n"
        text += f"📊 Проверено: `{checked}`\n"
        text += f"🔄 Восстановлено: `{revived}`\n"
        
        if checked > 0:
            revived_percent = int((revived / checked) * 100)
            text += f"📈 Процент восстановления: `{revived_percent}%`"
        
        await callback.message.edit_text(
            text,
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="🔄 Проверить все", callback_data="admin:accounts:validate")],
                [InlineKeyboardButton(text="◀️ Назад", callback_data="admin:accounts")]
            ]),
            parse_mode="Markdown"
        )
        # ИСПРАВЛЕНО: Отвечаем на callback сразу, чтобы избежать "query is too old" ошибки
        # Если callback уже истек, игнорируем ошибку
        try:
            await callback.answer(f"{Emoji.SUCCESS} Перепроверка завершена")
        except Exception as e:
            # Игнорируем ошибки истекших callback queries
            if "too old" in str(e).lower() or "timeout" in str(e).lower() or "query id" in str(e).lower():
                logger.debug(f"Callback query expired, ignoring: {e}")
            else:
                logger.warning(f"Error answering callback: {e}")
    except Exception as e:
        logger.error(f"Error rechecking dead accounts: {e}", exc_info=True)
        try:
            if callback.message:
                await callback.message.edit_text(f"❌ Ошибка: {str(e)[:200]}")
            await callback.answer()
        except Exception:
            pass


# ========== НОВЫЕ ДЕЙСТВИЯ НАД АККАУНТАМИ ==========

@router.callback_query(F.data == "admin:accounts:export")
async def export_accounts_handler(callback: CallbackQuery, state: FSMContext):
    """Экспорт аккаунтов."""
    try:
        if not callback.message:
            await callback.answer("Ошибка: сообщение не найдено", show_alert=True)
            return
        
        await callback.message.edit_text(
            "📤 *Экспорт аккаунтов*\n\n"
            "Выберите статус для экспорта:",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="📦 Все аккаунты", callback_data="admin:accounts:export:all")],
                [InlineKeyboardButton(text="✅ Активные", callback_data="admin:accounts:export:active")],
                [InlineKeyboardButton(text="⚠️ С ошибками", callback_data="admin:accounts:export:error")],
                [InlineKeyboardButton(text="💀 Мёртвые", callback_data="admin:accounts:export:dead")],
                [InlineKeyboardButton(text="◀️ Назад", callback_data="admin:accounts")]
            ]),
            parse_mode="Markdown"
        )
        await callback.answer()
    except Exception as e:
        logger.error(f"Error in export_accounts_handler: {e}", exc_info=True)
        await callback.answer("Ошибка", show_alert=True)


@router.callback_query(F.data.startswith("admin:accounts:export:"))
async def process_export(callback: CallbackQuery):
    """Обрабатывает экспорт аккаунтов."""
    try:
        if not callback.message:
            await callback.answer("Ошибка: сообщение не найдено", show_alert=True)
            return
        
        status = callback.data.split(":")[-1]
        status_map = {
            "all": None,
            "active": "active",
            "error": "error",
            "dead": "dead"
        }
        export_status = status_map.get(status)
        
        await callback.message.edit_text("⏳ Экспортирую аккаунты...")
        await callback.answer()
        
        result = await api_client.export_accounts(status=export_status)
        total = result.get("total", 0)
        data = result.get("data", "")
        
        if total == 0:
            await callback.message.edit_text(
                "📭 Нет аккаунтов для экспорта",
                reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                    [InlineKeyboardButton(text="◀️ Назад", callback_data="admin:accounts")]
                ])
            )
            return
        
        # Отправляем файл с экспортом
        from aiogram.types import BufferedInputFile
        file_content = data.encode('utf-8')
        file = BufferedInputFile(file_content, filename=f"accounts_export_{status}.txt")
        
        await callback.message.answer_document(
            file,
            caption=f"📤 Экспортировано аккаунтов: `{total}`",
            parse_mode="Markdown"
        )
        
        await callback.message.edit_text(
            f"✅ *Экспорт завершён*\n\n📦 Экспортировано: `{total}` аккаунтов",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="◀️ Назад", callback_data="admin:accounts")]
            ]),
            parse_mode="Markdown"
        )
    except Exception as e:
        logger.error(f"Error exporting accounts: {e}", exc_info=True)
        try:
            if callback.message:
                await callback.message.edit_text(f"❌ Ошибка экспорта: {str(e)[:200]}")
            await callback.answer()
        except Exception:
            pass


@router.callback_query(F.data == "admin:accounts:delete_by_status")
async def delete_by_status_handler(callback: CallbackQuery):
    """Удаление аккаунтов по статусу."""
    try:
        if not callback.message:
            await callback.answer("Ошибка: сообщение не найдено", show_alert=True)
            return
        
        await callback.message.edit_text(
            "🗑 *Удаление аккаунтов*\n\n"
            "⚠️ ВНИМАНИЕ: Это действие необратимо!\n\n"
            "Выберите статус для удаления:",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="💀 Мёртвые", callback_data="admin:accounts:delete:dead")],
                [InlineKeyboardButton(text="⚠️ С ошибками", callback_data="admin:accounts:delete:error")],
                [InlineKeyboardButton(text="🚫 Ограниченные", callback_data="admin:accounts:delete:limited")],
                [InlineKeyboardButton(text="◀️ Назад", callback_data="admin:accounts")]
            ]),
            parse_mode="Markdown"
        )
        await callback.answer()
    except Exception as e:
        logger.error(f"Error in delete_by_status_handler: {e}", exc_info=True)
        await callback.answer("Ошибка", show_alert=True)


@router.callback_query(F.data.startswith("admin:accounts:delete:"))
async def confirm_delete_by_status(callback: CallbackQuery):
    """Подтверждение удаления аккаунтов по статусу."""
    try:
        if not callback.message:
            await callback.answer("Ошибка: сообщение не найдено", show_alert=True)
            return
        
        status = callback.data.split(":")[-1]
        status_names = {
            "dead": "мёртвые",
            "error": "с ошибками",
            "limited": "ограниченные"
        }
        status_name = status_names.get(status, status)
        
        await callback.message.edit_text(
            f"⚠️ *Подтверждение удаления*\n\n"
            f"Вы уверены, что хотите удалить все аккаунты со статусом '{status_name}'?\n\n"
            f"❌ Это действие необратимо!",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="✅ Да, удалить", callback_data=f"admin:accounts:delete_confirm:{status}")],
                [InlineKeyboardButton(text="❌ Отмена", callback_data="admin:accounts")]
            ]),
            parse_mode="Markdown"
        )
        await callback.answer()
    except Exception as e:
        logger.error(f"Error in confirm_delete_by_status: {e}", exc_info=True)
        await callback.answer("Ошибка", show_alert=True)


@router.callback_query(F.data.startswith("admin:accounts:delete_confirm:"))
async def execute_delete_by_status(callback: CallbackQuery):
    """Выполняет удаление аккаунтов по статусу."""
    try:
        if not callback.message:
            await callback.answer("Ошибка: сообщение не найдено", show_alert=True)
            return
        
        status = callback.data.split(":")[-1]
        
        await callback.message.edit_text("⏳ Удаляю аккаунты...")
        await callback.answer()
        
        result = await api_client.delete_accounts_by_status(status)
        deleted_count = result.get("deleted_count", 0)
        
        await callback.message.edit_text(
            f"✅ *Удаление завершено*\n\n"
            f"🗑 Удалено аккаунтов: `{deleted_count}`",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="◀️ Назад", callback_data="admin:accounts")]
            ]),
            parse_mode="Markdown"
        )
    except Exception as e:
        logger.error(f"Error deleting accounts by status: {e}", exc_info=True)
        try:
            if callback.message:
                await callback.message.edit_text(f"❌ Ошибка удаления: {str(e)[:200]}")
            await callback.answer()
        except Exception:
            pass


@router.callback_query(F.data == "admin:accounts:search")
async def search_accounts_handler(callback: CallbackQuery, state: FSMContext):
    """Поиск аккаунтов."""
    try:
        if not callback.message:
            await callback.answer("Ошибка: сообщение не найдено", show_alert=True)
            return
        
        await callback.message.edit_text(
            "🔍 *Поиск аккаунтов*\n\n"
            "Отправьте текст для поиска (по label):",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="❌ Отмена", callback_data="admin:accounts")]
            ]),
            parse_mode="Markdown"
        )
        await state.set_state(OrderStates.SEARCH_ACCOUNTS)
        await callback.answer()
    except Exception as e:
        logger.error(f"Error in search_accounts_handler: {e}", exc_info=True)
        await callback.answer("Ошибка", show_alert=True)


@router.message(StateFilter(OrderStates.SEARCH_ACCOUNTS))
async def process_search_accounts(message: Message, state: FSMContext):
    """Обрабатывает поиск аккаунтов."""
    try:
        search_query = message.text.strip() if message.text else ""
        
        if not search_query:
            await message.answer("❌ Пустой запрос. Попробуйте снова.")
            return
        
        await message.answer("⏳ Ищу аккаунты...")
        
        results = await api_client.search_accounts(q=search_query, limit=20)
        
        if not results:
            await message.answer(
                f"📭 Аккаунты не найдены по запросу: `{search_query}`",
                parse_mode="Markdown"
            )
            await state.clear()
            return
        
        text = f"🔍 *Результаты поиска:* `{search_query}`\n"
        text += "━━━━━━━━━━━━━━━━━━━━\n\n"
        
        for idx, acc in enumerate(results[:20], 1):
            status_emoji = {
                "active": Emoji.SUCCESS,
                "error": Emoji.WARNING,
                "dead": "💀",
                "limited": "🚫"
            }.get(acc.get("status", "unknown"), "❓")
            
            label = acc.get("label", "N/A")[:30]
            acc_id = acc.get("id", "N/A")
            text += f"`{idx:2}.` {status_emoji} {label} (ID: {acc_id})\n"
        
        if len(results) > 20:
            text += f"\n... и ещё `{len(results) - 20}` аккаунтов"
        
        await message.answer(
            text,
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="◀️ Меню аккаунтов", callback_data="admin:accounts")]
            ]),
            parse_mode="Markdown"
        )
        await state.clear()
    except Exception as e:
        logger.error(f"Error processing search: {e}", exc_info=True)
        await message.answer(f"❌ Ошибка поиска: {str(e)[:200]}")
        await state.clear()


@router.callback_query(F.data == "admin:accounts:more")
async def accounts_more_actions(callback: CallbackQuery):
    """Дополнительные действия над аккаунтами."""
    try:
        if not callback.message:
            await callback.answer("Ошибка: сообщение не найдено", show_alert=True)
            return
        
        await callback.message.edit_text(
            "⚙️ *Дополнительные действия*\n\n"
            "Выберите действие:",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="🔄 Реактивировать error-аккаунты", callback_data="admin:accounts:reset_all_errors")],
                [InlineKeyboardButton(text="🧨 Удалить все аккаунты", callback_data="admin:accounts:delete_all")],
                [InlineKeyboardButton(text="◀️ Назад", callback_data="admin:accounts")]
            ]),
            parse_mode="Markdown"
        )
        await callback.answer()
    except Exception as e:
        logger.error(f"Error in accounts_more_actions: {e}", exc_info=True)
        await callback.answer("Ошибка", show_alert=True)


@router.callback_query(F.data == "admin:accounts:delete_all")
async def confirm_delete_all_accounts(callback: CallbackQuery):
    """Confirms deleting every account from the pool."""
    try:
        if not callback.message:
            await callback.answer("Ошибка: сообщение не найдено", show_alert=True)
            return

        stats = await api_client.get_account_stats()
        total = int(stats.get("total", 0) or 0)
        active = int(stats.get("active", 0) or 0)
        error = int(stats.get("error", 0) or 0)
        dead = int(stats.get("dead", 0) or 0)
        limited = int(stats.get("limited", 0) or 0)

        await callback.message.edit_text(
            "🧨 *Удалить все аккаунты?*\n\n"
            "Это действие нельзя отменить.\n\n"
            f"Всего аккаунтов: `{total}`\n"
            f"Активные: `{active}`\n"
            f"С ошибками: `{error}`\n"
            f"Мёртвые: `{dead}`\n"
            f"Ограниченные: `{limited}`\n\n"
            "После удаления новые заказы не смогут выполняться, пока вы не импортируете аккаунты заново.",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="Да, удалить все аккаунты", callback_data="admin:accounts:delete_all_confirm")],
                [InlineKeyboardButton(text="Отмена", callback_data="admin:accounts")]
            ]),
            parse_mode="Markdown"
        )
        await callback.answer()
    except Exception as e:
        logger.error(f"Error in confirm_delete_all_accounts: {e}", exc_info=True)
        await callback.answer("Ошибка", show_alert=True)


@router.callback_query(F.data == "admin:accounts:delete_all_confirm")
async def execute_delete_all_accounts(callback: CallbackQuery):
    """Deletes every account from the pool after confirmation."""
    try:
        if not callback.message:
            await callback.answer("Ошибка: сообщение не найдено", show_alert=True)
            return

        await callback.message.edit_text("⏳ Удаляю все аккаунты...")
        await callback.answer()

        result = await api_client.delete_all_accounts()
        deleted_count = int(result.get("deleted_count", 0) or 0)
        deleted_by_status = result.get("deleted_by_status", {}) or {}

        await log_admin_action(
            admin_telegram_id=callback.from_user.id,
            action_type="delete_accounts",
            details={
                "scope": "all",
                "deleted_count": deleted_count,
                "deleted_by_status": deleted_by_status,
            },
            description=f"Удаление всех аккаунтов: {deleted_count}"
        )

        breakdown = "\n".join(
            f"{status}: `{count}`"
            for status, count in sorted(deleted_by_status.items())
        )
        if not breakdown:
            breakdown = "Аккаунтов для удаления не было."

        await callback.message.edit_text(
            "✅ *Аккаунты удалены*\n\n"
            f"Удалено всего: `{deleted_count}`\n\n"
            f"{breakdown}",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="📥 Импорт аккаунтов", callback_data="admin:accounts:import")],
                [InlineKeyboardButton(text="◀️ Назад", callback_data="admin:accounts")]
            ]),
            parse_mode="Markdown"
        )
    except Exception as e:
        logger.error(f"Error deleting all accounts: {e}", exc_info=True)
        try:
            if callback.message:
                await callback.message.edit_text(
                    f"❌ Ошибка удаления аккаунтов: {str(e)[:200]}",
                    reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                        [InlineKeyboardButton(text="◀️ Назад", callback_data="admin:accounts")]
                    ])
                )
            await callback.answer()
        except Exception:
            pass


@router.callback_query(F.data == "admin:accounts:reset_all_errors")
async def reset_all_errors_handler(callback: CallbackQuery):
    """Сбрасывает ошибки у всех аккаунтов со статусом 'error'."""
    try:
        if not callback.message:
            await callback.answer("Ошибка: сообщение не найдено", show_alert=True)
            return
        
        await callback.message.edit_text("⏳ Сбрасываю ошибки...")
        await callback.answer()
        
        # Получаем все аккаунты со статусом error
        error_accounts = await api_client.list_accounts(status="error", limit=1000)
        account_ids = [acc["id"] for acc in error_accounts]
        
        if not account_ids:
            await callback.message.edit_text(
                "✅ Нет аккаунтов со статусом 'error'",
                reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                    [InlineKeyboardButton(text="◀️ Назад", callback_data="admin:accounts")]
                ])
            )
            return
        
        # Сбрасываем ошибки через bulk update (меняем статус на active и сбрасываем счетчики)
        result = await api_client.bulk_update_account_status(account_ids, "active")
        updated = result.get("updated_count", 0)
        
        await callback.message.edit_text(
            f"✅ *Сброс ошибок завершён*\n\n"
            f"🔄 Обработано аккаунтов: `{updated}`",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="◀️ Назад", callback_data="admin:accounts")]
            ]),
            parse_mode="Markdown"
        )
    except Exception as e:
        logger.error(f"Error resetting all errors: {e}", exc_info=True)
        try:
            if callback.message:
                await callback.message.edit_text(f"❌ Ошибка: {str(e)[:200]}")
            await callback.answer()
        except Exception:
            pass


def _normalize_admin_user_filter(value: str | None) -> str:
    return value if value in ADMIN_USER_FILTER_LABELS else "all"


def _admin_users_page_callback(page: int, status: str) -> str:
    status = _normalize_admin_user_filter(status)
    return f"admin:users:p:{max(0, page)}:{status}"


def _is_pending_user(user: dict) -> bool:
    return bool(user.get("is_pending_approval")) or (
        bool(user.get("is_blocked"))
        and not bool(user.get("was_approved_once", True))
        and user.get("role") not in ("admin", "owner")
    )


def _admin_user_status_icon(user: dict) -> str:
    if _is_pending_user(user):
        return "⏳"
    if bool(user.get("is_blocked")):
        return "🔒"
    return "✅"


def _admin_user_role_icon(role: str | None) -> str:
    if role == "owner":
        return "👑"
    if role == "admin":
        return "🛡"
    return "👤"


def _admin_user_display_name(user: dict, limit: int = 24) -> str:
    raw = str(user.get("username") or f"ID {user.get('telegram_id') or user.get('id')}")
    raw = raw.strip() or f"ID {user.get('id')}"
    return raw[:limit]


def _build_admin_users_text(page_data: dict) -> str:
    items = list(page_data.get("items") or [])
    counts = page_data.get("counts") or {}
    filters = page_data.get("filters") or {}
    status = _normalize_admin_user_filter(filters.get("status"))
    query_text = str(filters.get("q") or "").strip()
    page = int(page_data.get("page") or 1)
    total_pages = int(page_data.get("total_pages") or 1)
    total = int(page_data.get("total") or 0)
    offset = int(page_data.get("offset") or 0)

    lines = [
        "👥 *Пользователи*",
        "━━━━━━━━━━━━━━━━━━━━",
        "",
        (
            f"Всего: `{int(counts.get('total') or 0)}` | "
            f"активных `{int(counts.get('active') or 0)}` | "
            f"ждут `{int(counts.get('pending') or 0)}` | "
            f"блок `{int(counts.get('blocked') or 0)}`"
        ),
        (
            f"Админы: `{int(counts.get('admins') or 0)}` | "
            f"с балансом `{int(counts.get('positive_balance') or 0)}`"
        ),
        f"Фильтр: *{ADMIN_USER_FILTER_LABELS[status]}* | страница `{page}/{total_pages}` | найдено `{total}`",
        f"Поиск: {markdown_inline_code(query_text)}" if query_text else "Поиск: не задан",
        "",
    ]

    if not items:
        lines.append("По этому фильтру пользователей нет.")
    else:
        for index, user in enumerate(items, start=offset + 1):
            role = str(user.get("role") or "user")
            username = markdown_inline_code(_admin_user_display_name(user))
            balance = _coerce_float(user.get("balance"), 0.0)
            lines.append(
                f"`{index:02d}.` {_admin_user_status_icon(user)} "
                f"{_admin_user_role_icon(role)} {username} | `{balance:.2f}` грн"
            )

    lines.append("")
    lines.append("_Нажмите на пользователя, чтобы открыть карточку._")
    return "\n".join(lines)


def _build_admin_users_keyboard(page_data: dict) -> InlineKeyboardMarkup:
    items = list(page_data.get("items") or [])
    filters = page_data.get("filters") or {}
    status = _normalize_admin_user_filter(filters.get("status"))
    query_text = str(filters.get("q") or "").strip()
    page = max(0, int(page_data.get("page") or 1) - 1)
    total_pages = max(1, int(page_data.get("total_pages") or 1))

    keyboard: list[list[InlineKeyboardButton]] = [
        [
            InlineKeyboardButton(text=("• " if status == "all" else "") + "Все", callback_data=_admin_users_page_callback(0, "all")),
            InlineKeyboardButton(text=("• " if status == "pending" else "") + "Ждут", callback_data=_admin_users_page_callback(0, "pending")),
            InlineKeyboardButton(text=("• " if status == "active" else "") + "Активные", callback_data=_admin_users_page_callback(0, "active")),
        ],
        [
            InlineKeyboardButton(text=("• " if status == "blocked" else "") + "Блок", callback_data=_admin_users_page_callback(0, "blocked")),
            InlineKeyboardButton(text=("• " if status == "admins" else "") + "Админы", callback_data=_admin_users_page_callback(0, "admins")),
        ],
    ]

    search_row = [
        InlineKeyboardButton(text="🔎 Поиск", callback_data=f"admin:users:search:{status}"),
    ]
    if query_text:
        search_row.append(InlineKeyboardButton(text="✖️ Сбросить поиск", callback_data=f"admin:users:clear:{status}"))
    keyboard.append(search_row)

    for user in items:
        user_id = user.get("id")
        if not user_id:
            continue
        role = str(user.get("role") or "user")
        username = _admin_user_display_name(user, limit=20)
        balance = _coerce_float(user.get("balance"), 0.0)
        keyboard.append([
            InlineKeyboardButton(
                text=f"{_admin_user_status_icon(user)} {_admin_user_role_icon(role)} {username} | {balance:.0f} грн",
                callback_data=f"admin:user:{user_id}",
            )
        ])

    if total_pages > 1:
        keyboard.append([
            InlineKeyboardButton(
                text="◀️",
                callback_data=_admin_users_page_callback(page - 1, status) if page > 0 else "admin:users:noop",
            ),
            InlineKeyboardButton(text=f"{page + 1}/{total_pages}", callback_data="admin:users:noop"),
            InlineKeyboardButton(
                text="▶️",
                callback_data=_admin_users_page_callback(page + 1, status) if page + 1 < total_pages else "admin:users:noop",
            ),
        ])

    keyboard.append([
        InlineKeyboardButton(text="🔄 Обновить", callback_data=_admin_users_page_callback(page, status)),
        InlineKeyboardButton(text="◀️ Назад", callback_data="admin:main"),
    ])
    return InlineKeyboardMarkup(inline_keyboard=keyboard)


async def _render_admin_users_page(
    callback: CallbackQuery,
    page: int = 0,
    status: str = "all",
    q: str | None = None,
) -> None:
    if not callback.message:
        await callback.answer("Ошибка: сообщение не найдено", show_alert=True)
        return

    status = _normalize_admin_user_filter(status)
    page_data = await api_client.list_users_page(
        limit=ADMIN_USERS_PAGE_SIZE,
        offset=max(0, page) * ADMIN_USERS_PAGE_SIZE,
        status=status,
        q=q,
    )
    await _safe_edit_callback_text(
        callback,
        _build_admin_users_text(page_data),
        reply_markup=_build_admin_users_keyboard(page_data),
        parse_mode="Markdown",
    )


@router.callback_query(F.data == "admin:users")
async def list_users(callback: CallbackQuery, state: FSMContext):
    """Показывает список пользователей с короткой сводкой по ролям, блокировкам и балансу."""
    try:
        if not callback.message:
            await callback.answer("Ошибка: сообщение не найдено", show_alert=True)
            return
        await state.update_data(admin_users_query="")
        await callback.answer()
        await _render_admin_users_page(callback, page=0, status="all")
    except Exception as e:
        logger.error(f"Error listing users: {e}", exc_info=True)
        await callback.answer("Ошибка загрузки списка", show_alert=True)


@router.callback_query(F.data.startswith("admin:users:p:"))
async def list_users_page(callback: CallbackQuery, state: FSMContext):
    """Shows a requested users page."""
    try:
        await callback.answer()
        parts = (callback.data or "").split(":")
        page = int(parts[3]) if len(parts) > 3 and parts[3].isdigit() else 0
        status = _normalize_admin_user_filter(parts[4] if len(parts) > 4 else "all")
        state_data = await state.get_data()
        query_text = str(state_data.get("admin_users_query") or "").strip() or None
        await _render_admin_users_page(callback, page=page, status=status, q=query_text)
    except Exception as e:
        logger.error(f"Error paginating users: {e}", exc_info=True)
        await callback.answer("Ошибка загрузки страницы", show_alert=True)


@router.callback_query(F.data.startswith("admin:users:search:"))
async def users_search_prompt(callback: CallbackQuery, state: FSMContext):
    try:
        await callback.answer()
        status = _normalize_admin_user_filter((callback.data or "").split(":")[-1])
        await state.set_state(OrderStates.ADMIN_USER_SEARCH)
        await state.update_data(admin_users_status=status)
        if callback.message:
            await _safe_edit_callback_text(
                callback,
                "🔎 *Поиск пользователя*\n\n"
                "Введите username, Telegram ID или внутренний ID.\n"
                "Чтобы отменить поиск, отправьте `-`.",
                reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                    [InlineKeyboardButton(text="◀️ Назад", callback_data=_admin_users_page_callback(0, status))]
                ]),
                parse_mode="Markdown",
            )
    except Exception as e:
        logger.error(f"Error opening users search: {e}", exc_info=True)
        await callback.answer("Ошибка открытия поиска", show_alert=True)


@router.callback_query(F.data.startswith("admin:users:clear:"))
async def users_search_clear(callback: CallbackQuery, state: FSMContext):
    try:
        await callback.answer()
        status = _normalize_admin_user_filter((callback.data or "").split(":")[-1])
        await state.update_data(admin_users_query="", admin_users_status=status)
        await _render_admin_users_page(callback, page=0, status=status)
    except Exception as e:
        logger.error(f"Error clearing users search: {e}", exc_info=True)
        await callback.answer("Ошибка сброса поиска", show_alert=True)


@router.message(StateFilter(OrderStates.ADMIN_USER_SEARCH))
async def process_admin_user_search(message: Message, state: FSMContext):
    try:
        raw_query = (message.text or "").strip()
        state_data = await state.get_data()
        status = _normalize_admin_user_filter(state_data.get("admin_users_status"))
        query_text = "" if raw_query in ("", "-") else raw_query[:80]
        await state.update_data(admin_users_query=query_text, admin_users_status=status)
        await state.set_state(None)

        page_data = await api_client.list_users_page(
            limit=ADMIN_USERS_PAGE_SIZE,
            offset=0,
            status=status,
            q=query_text or None,
        )
        await message.answer(
            _build_admin_users_text(page_data),
            reply_markup=_build_admin_users_keyboard(page_data),
            parse_mode="Markdown",
        )
    except Exception as e:
        logger.error(f"Error processing users search: {e}", exc_info=True)
        await message.answer("Ошибка поиска пользователей. Попробуйте ещё раз.")


@router.callback_query(F.data == "admin:users:noop")
async def users_noop(callback: CallbackQuery):
    await callback.answer()


def _format_user_created_at(raw: str | None) -> str:
    """Форматирует ISO created_at в короткий человеческий вид."""
    if not raw:
        return "—"
    try:
        from datetime import datetime, timezone
        dt = datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.strftime("%Y-%m-%d %H:%M UTC")
    except Exception:
        return str(raw)[:19]


async def _build_user_parsing_summary(user_backend_id: int) -> tuple[int, int, int]:
    """Возвращает (total_creators, enabled_creators, total_new_videos).

    Все ошибки глушит наружу — карточка не должна падать из-за недоступного
    парсингового API.
    """
    try:
        creators = await api_client.list_video_creators(user_backend_id)
        if not isinstance(creators, list):
            return 0, 0, 0
        total = len(creators)
        enabled = sum(1 for c in creators if bool(c.get("enabled", True)))
        new_videos = sum(int(c.get("new_videos_count") or 0) for c in creators)
        return total, enabled, new_videos
    except Exception as exc:
        logger.warning(f"Failed to load parsing summary for user {user_backend_id}: {exc}")
        return 0, 0, 0


def _summary_value(summary: dict, *path, default=None):
    current = summary
    for key in path:
        if not isinstance(current, dict):
            return default
        current = current.get(key)
    return default if current is None else current


def _card_page_data(user_detail: dict, tab: str) -> dict:
    page_data = user_detail.get("_card_page") or {}
    if page_data.get("tab") != tab:
        return {}
    return page_data


def _card_page_items(user_detail: dict, tab: str) -> list:
    page_data = _card_page_data(user_detail, tab)
    items = page_data.get("items") if page_data else None
    return list(items or [])


def _card_page_label(user_detail: dict, tab: str) -> str:
    page_data = _card_page_data(user_detail, tab)
    if not page_data:
        return ""
    page = int(page_data.get("page") or 0)
    per_page = max(1, int(page_data.get("per_page") or USER_CARD_PAGE_SIZE))
    total = max(0, int(page_data.get("total") or 0))
    if total <= per_page:
        return f"Показано: `{total}`"
    start = page * per_page + 1
    end = min(start + per_page - 1, total)
    total_pages = max(1, (total + per_page - 1) // per_page)
    return f"Показано: `{start}-{end}` из `{total}` · страница `{page + 1}/{total_pages}`"


def _status_label(status: str | None) -> str:
    labels = {
        "pending": "ожидает",
        "in_progress": "в работе",
        "done": "готов",
        "completed": "готов",
        "partial": "частично",
        "error": "ошибка",
        "failed": "ошибка",
        "stopped": "остановлен",
        "new": "новое",
        "queued": "в очереди",
        "skipped": "пропуск",
        "active": "активен",
        "dead": "dead",
        "reauth_needed": "reauth",
    }
    return labels.get(str(status or ""), str(status or "—"))


def _format_money(value) -> str:
    return f"{_coerce_float(value, 0.0):.2f}"


def _format_compact_date(raw: str | None) -> str:
    if not raw:
        return "—"
    try:
        from datetime import datetime, timezone
        dt = datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.strftime("%d.%m.%Y %H:%M")
    except Exception:
        return str(raw)[:16]


def _mode_label(mode: str | None) -> str:
    return {
        "auto_comment": "автокоммент",
        "manual_review": "ручной разбор",
        "notify_only": "уведомления",
    }.get(str(mode or ""), str(mode or "—"))


def _transaction_type_label(kind: str | None) -> str:
    return {
        "deposit": "пополнения",
        "withdrawal": "списания",
        "hold": "заказы",
        "refund": "возвраты",
    }.get(str(kind or ""), str(kind or "—"))


def _render_user_overview(user_detail: dict) -> str:
    summary = user_detail.get("summary") or {}
    orders = summary.get("orders") or {}
    monitoring = summary.get("monitoring") or {}
    templates = summary.get("templates") or {}
    personal_accounts = summary.get("personal_accounts") or {}
    subscription = summary.get("subscription") or {}

    username_raw = user_detail.get("username") or f"ID: {user_detail.get('telegram_id')}"
    username = markdown_inline_code(str(username_raw))
    role = str(user_detail.get("role") or "user")
    role_is_admin = role in ("admin", "owner")
    is_blocked = bool(user_detail.get("is_blocked", False))
    is_pending = bool(user_detail.get("is_pending_approval", False))
    bot_access = bool(user_detail.get("bot_access", not is_blocked))
    full_access_override = bool(user_detail.get("full_access_override", False))
    status_text = "ждёт доступа" if is_pending else "заблокирован" if is_blocked else "активен"
    bot_access_text = "открыт" if bot_access else "закрыт"
    if role_is_admin:
        full_mode_text = "по роли"
    elif full_access_override and bot_access:
        full_mode_text = "выдан вручную"
    elif full_access_override:
        full_mode_text = "выдан, но закрыт блокировкой"
    else:
        full_mode_text = "нет"

    lines = [
        "👤 *Карточка работника*",
        "━━━━━━━━━━━━━━━━━━━━",
        "",
        f"*Имя:* {username}",
        f"*Роль:* `{role}` | *статус:* `{status_text}`",
        f"*Доступ к боту:* `{bot_access_text}` | *полный режим:* `{full_mode_text}`",
        f"*Telegram ID:* `{user_detail.get('telegram_id')}` | *ID:* `{user_detail.get('id')}`",
        f"*Регистрация:* `{_format_compact_date(user_detail.get('created_at'))}`",
        "",
        "💰 *Финансы*",
        f"Баланс: `{_format_money(user_detail.get('balance'))}` грн",
        f"Потрачено по заказам: `{_format_money(orders.get('final_cost'))}` грн",
        f"Возвраты: `{_format_money(orders.get('refunded_cost'))}` грн",
        "",
        "📋 *Заказы*",
        f"Всего: `{int(orders.get('total') or 0)}` | активных: `{int(orders.get('active') or 0)}`",
        f"Статусы: {_format_status_counts(orders.get('by_status') or {})}",
        "",
        "📡 *Мониторинг*",
        (
            f"Блогеров: `{int(monitoring.get('creators_total') or 0)}` | "
            f"включено: `{int(monitoring.get('creators_enabled') or 0)}` | "
            f"новых видео: `{int((monitoring.get('videos_by_status') or {}).get('new') or 0)}`"
        ),
        f"Автокомментинг: `{int((monitoring.get('creators_by_mode') or {}).get('auto_comment') or 0)}` блогеров",
        f"Уведомления: `{'включены' if monitoring.get('video_notifications_enabled') else 'выключены'}`",
        "",
        "🧩 *Шаблоны и аккаунты*",
        (
            f"Шаблоны заказов: `{int(templates.get('task_templates') or 0)}` | "
            f"текстовые: `{int(templates.get('comment_templates') or 0)}`"
        ),
        (
            f"Личные аккаунты: `{int(personal_accounts.get('total') or 0)}` | "
            f"main сегодня: `{int(personal_accounts.get('today_main_comments') or 0)}` | "
            f"reply сегодня: `{int(personal_accounts.get('today_replies') or 0)}`"
        ),
    ]

    if subscription.get("tier") or subscription.get("expire_at"):
        lines.extend([
            "",
            "🎟 *Подписка*",
            f"Тариф: `{subscription.get('tier') or '—'}` | до `{_format_compact_date(subscription.get('expire_at'))}`",
        ])

    recent = list(orders.get("recent") or [])[:1]
    if recent:
        task = recent[0]
        lines.extend([
            "",
            "🕘 *Последний заказ*",
            (
                f"`#{task.get('id')}` {_status_label(task.get('status'))} | "
                f"{int(task.get('progress') or 0)}/{int(task.get('video_count') or 0)} видео | "
                f"`{_format_compact_date(task.get('created_at'))}`"
            ),
        ])

    return "\n".join(lines)


def _format_status_counts(counts: dict) -> str:
    if not counts:
        return "`—`"
    ordered = ["pending", "in_progress", "done", "completed", "partial", "error", "failed", "stopped"]
    chunks = []
    seen = set()
    for key in ordered:
        value = int(counts.get(key) or 0)
        if value:
            chunks.append(f"{_status_label(key)} `{value}`")
            seen.add(key)
    for key, value in counts.items():
        if key not in seen and int(value or 0):
            chunks.append(f"{_status_label(key)} `{int(value)}`")
    return ", ".join(chunks) if chunks else "`—`"


def _render_user_finance_tab(user_detail: dict) -> str:
    finance = _summary_value(user_detail, "summary", "finance", default={}) or {}
    orders = _summary_value(user_detail, "summary", "orders", default={}) or {}
    by_type = finance.get("transactions_by_type") or {}
    latest = list(finance.get("latest_transactions") or [])

    lines = [
        "💰 *Финансы работника*",
        "━━━━━━━━━━━━━━━━━━━━",
        "",
        f"Баланс: `{_format_money(user_detail.get('balance'))}` грн",
        f"Заказы списали: `{_format_money((by_type.get('hold') or {}).get('amount'))}` грн",
        f"Ручные списания: `{_format_money((by_type.get('withdrawal') or {}).get('amount'))}` грн",
        f"Пополнения: `+{_format_money((by_type.get('deposit') or {}).get('amount'))}` грн",
        f"Возвраты: `+{_format_money((by_type.get('refund') or {}).get('amount'))}` грн",
        "",
        f"Итого заказов: `{_format_money(orders.get('total_cost'))}` грн",
        f"Итог после возвратов: `{_format_money(orders.get('final_cost'))}` грн",
        "",
        "🧾 *Последние операции*",
    ]
    if not latest:
        lines.append("_Операций пока нет._")
    for transaction in latest[:5]:
        sign = "+" if _coerce_float(transaction.get("amount"), 0.0) > 0 else ""
        lines.append(
            f"`#{transaction.get('id')}` {_transaction_type_label(transaction.get('type'))}: "
            f"`{sign}{_format_money(transaction.get('amount'))}` грн | "
            f"`{_format_compact_date(transaction.get('created_at'))}`"
        )
        description = str(transaction.get("description") or "").strip()
        if description:
            lines.append(f"   {markdown_inline_code(description[:72])}")
    return "\n".join(lines)


def _render_user_orders_tab(user_detail: dict) -> str:
    orders = _summary_value(user_detail, "summary", "orders", default={}) or {}
    recent = _card_page_items(user_detail, "orders") or list(orders.get("recent") or [])
    page_label = _card_page_label(user_detail, "orders")
    lines = [
        "📋 *Заказы работника*",
        "━━━━━━━━━━━━━━━━━━━━",
        "",
        f"Всего: `{int(orders.get('total') or 0)}` | активных: `{int(orders.get('active') or 0)}`",
        f"Статусы: {_format_status_counts(orders.get('by_status') or {})}",
        f"Сумма заказов: `{_format_money(orders.get('total_cost'))}` грн",
        f"Возвраты: `{_format_money(orders.get('refunded_cost'))}` грн",
    ]
    if page_label:
        lines.append(page_label)
    lines.extend(["", "🕘 *Заказы на странице*"])
    if not recent:
        lines.append("_Заказов пока нет._")
    for task in recent[:USER_CARD_PAGE_SIZE]:
        template = task.get("template_name") or task.get("template_id") or "—"
        creator = task.get("creator_username") or "—"
        video_count = int(task.get("video_count") or len(task.get("video_urls") or []) or 0)
        lines.append(
            f"`#{task.get('id')}` {_status_label(task.get('status'))} | "
            f"`{int(task.get('progress') or 0)}/{video_count}` видео | "
            f"`{_format_money(task.get('final_cost'))}` грн"
        )
        lines.append(
            "   источник "
            f"{markdown_inline_code(str(task.get('origin') or 'manual')[:24])} | "
            f"шаблон {markdown_inline_code(str(template)[:28])} | "
            f"блогер {markdown_inline_code(str(creator)[:24])}"
        )
    return "\n".join(lines)


def _render_user_monitoring_tab(user_detail: dict) -> str:
    monitoring = _summary_value(user_detail, "summary", "monitoring", default={}) or {}
    latest = _card_page_items(user_detail, "monitoring") or list(monitoring.get("latest_creators") or [])
    page_label = _card_page_label(user_detail, "monitoring")
    modes = monitoring.get("creators_by_mode") or {}
    videos = monitoring.get("videos_by_status") or {}
    lines = [
        "📡 *Мониторинг и автокомментинг*",
        "━━━━━━━━━━━━━━━━━━━━",
        "",
        f"Блогеров: `{int(monitoring.get('creators_total') or 0)}`",
        f"Включено: `{int(monitoring.get('creators_enabled') or 0)}` | выключено: `{int(monitoring.get('creators_disabled') or 0)}`",
        (
            f"Режимы: авто `{int(modes.get('auto_comment') or 0)}`, "
            f"ручной `{int(modes.get('manual_review') or 0)}`, "
            f"уведомления `{int(modes.get('notify_only') or 0)}`"
        ),
        f"Видео: {_format_status_counts(videos)}",
        f"Лимит видео/день: `{int(monitoring.get('daily_video_limit') or 0)}` | сегодня: `{int(monitoring.get('today_videos') or 0)}`",
        f"Последний день лимита: `{_format_compact_date(monitoring.get('last_video_date'))}`",
    ]
    if page_label:
        lines.append(page_label)
    lines.extend(["", "🧭 *Блогеры на странице*"])
    if not latest:
        lines.append("_Блогеров пока нет._")
    for creator in latest[:USER_CARD_PAGE_SIZE]:
        status_icon = "🟢" if creator.get("enabled") else "⚪"
        lines.append(
            f"{status_icon} {markdown_handle(str(creator.get('username') or 'unknown'))} | "
            f"{_mode_label(creator.get('mode'))} | `{int(creator.get('interval_minutes') or 0)}` мин"
        )
        lines.append(f"   шаблон `{creator.get('default_template_id') or '—'}` | обновлён `{_format_compact_date(creator.get('updated_at'))}`")
    return "\n".join(lines)


def _render_user_templates_tab(user_detail: dict) -> str:
    templates = _summary_value(user_detail, "summary", "templates", default={}) or {}
    page_items = _card_page_items(user_detail, "templates")
    page_label = _card_page_label(user_detail, "templates")
    recent = list(templates.get("recent_task_templates") or [])
    recent_texts = list(templates.get("recent_comment_templates") or [])
    lines = [
        "🧩 *Шаблоны работника*",
        "━━━━━━━━━━━━━━━━━━━━",
        "",
        f"Шаблоны заказов: `{int(templates.get('task_templates') or 0)}`",
        f"Текстовые шаблоны: `{int(templates.get('comment_templates') or 0)}`",
    ]
    if page_label:
        lines.append(page_label)
    if page_items:
        lines.extend(["", "🕘 *Шаблоны на странице*"])
        for template in page_items[:USER_CARD_PAGE_SIZE]:
            kind = "заказ" if template.get("_kind") == "task" else "текст"
            name = markdown_inline_code(str(template.get("name") or "Без названия")[:42])
            lines.append(f"`#{template.get('id')}` {name} | `{kind}` | `{_format_compact_date(template.get('created_at'))}`")
            if template.get("_kind") == "task":
                lines.append(
                    f"   ответы `{int(template.get('reply_count') or 0)}`"
                )
            else:
                lines.append(f"   тип {markdown_inline_code(template.get('type') or '—')}")
        return "\n".join(lines)

    lines.extend(["", "🕘 *Последние шаблоны заказов*"])
    if not recent:
        lines.append("_Шаблонов заказов пока нет._")
    for template in recent:
        lines.append(
            f"`#{template.get('id')}` {markdown_inline_code(str(template.get('name') or 'Без названия')[:42])}"
        )
        lines.append(
            f"   ответы `{int(template.get('reply_count') or 0)}` | `{_format_compact_date(template.get('created_at'))}`"
        )
    lines.extend(["", "💬 *Последние текстовые шаблоны*"])
    if not recent_texts:
        lines.append("_Текстовых шаблонов пока нет._")
    for template in recent_texts:
        lines.append(
            f"`#{template.get('id')}` {markdown_inline_code(str(template.get('name') or 'Без названия')[:42])} | "
            f"{markdown_inline_code(template.get('type') or '—')} | `{_format_compact_date(template.get('created_at'))}`"
        )
    return "\n".join(lines)


def _render_user_accounts_tab(user_detail: dict) -> str:
    accounts = _summary_value(user_detail, "summary", "personal_accounts", default={}) or {}
    by_status = accounts.get("by_status") or {}
    recent = _card_page_items(user_detail, "accounts") or list(accounts.get("recent") or [])
    page_label = _card_page_label(user_detail, "accounts")
    lines = [
        "📱 *Личный пул аккаунтов*",
        "━━━━━━━━━━━━━━━━━━━━",
        "",
        f"Всего аккаунтов: `{int(accounts.get('total') or 0)}`",
        f"Статусы: {_format_status_counts(by_status)}",
        f"Main сегодня: `{int(accounts.get('today_main_comments') or 0)}`",
        f"Replies сегодня: `{int(accounts.get('today_replies') or 0)}`",
    ]
    if page_label:
        lines.append(page_label)
    lines.extend(["", "🕘 *Аккаунты на странице*"])
    if not recent:
        lines.append("_Личных аккаунтов пока нет._")
    for account in recent[:USER_CARD_PAGE_SIZE]:
        label = str(account.get("label") or f"#{account.get('id')}")
        country = str(account.get("country_code") or "—").upper()
        lines.append(
            f"`#{account.get('id')}` {markdown_inline_code(label[:34])} | "
            f"{_status_label(account.get('status'))} | `{country}`"
        )
        lines.append(
            f"   main `{int(account.get('today_main_comments') or 0)}` | "
            f"reply `{int(account.get('today_replies') or 0)}` | "
            f"valid `{_format_compact_date(account.get('last_validation_ok'))}`"
        )
        last_error = str(account.get("last_error") or "").strip()
        if last_error:
            lines.append(f"   ошибка {markdown_inline_code(last_error[:64])}")
    return "\n".join(lines)

def _render_user_card_text(user_detail: dict, tab: str) -> str:
    if tab == "finance":
        return _render_user_finance_tab(user_detail)
    if tab == "orders":
        return _render_user_orders_tab(user_detail)
    if tab == "monitoring":
        return _render_user_monitoring_tab(user_detail)
    if tab == "templates":
        return _render_user_templates_tab(user_detail)
    if tab == "accounts":
        return _render_user_accounts_tab(user_detail)
    return _render_user_overview(user_detail)


def _user_card_tab_callback(user_backend_id: int, tab: str, page: int = 0) -> str:
    if page > 0:
        return f"admin:ucard:{user_backend_id}:{tab}:{page}"
    return f"admin:ucard:{user_backend_id}:{tab}"


def _build_user_card_keyboard(user_detail: dict, tab: str) -> InlineKeyboardMarkup:
    user_backend_id = int(user_detail.get("id"))
    role = user_detail.get("role", "user")
    is_blocked = bool(user_detail.get("is_blocked", False))
    full_access_override = bool(user_detail.get("full_access_override", False))
    page_data = _card_page_data(user_detail, tab)
    current_page = int(page_data.get("page") or 0) if page_data else 0
    telegram_id = user_detail.get("telegram_id")
    username = str(user_detail.get("username") or "").strip().lstrip("@")
    username_url = username if re.fullmatch(r"[A-Za-z0-9_]{5,32}", username or "") else ""

    def tab_button(label: str, value: str) -> InlineKeyboardButton:
        prefix = "• " if tab == value else ""
        return InlineKeyboardButton(text=prefix + label, callback_data=_user_card_tab_callback(user_backend_id, value))

    keyboard: list[list[InlineKeyboardButton]] = [
        [tab_button("Обзор", "overview"), tab_button("Финансы", "finance"), tab_button("Заказы", "orders")],
        [tab_button("Мониторинг", "monitoring"), tab_button("Шаблоны", "templates"), tab_button("Аккаунты", "accounts")],
    ]

    if page_data and tab in USER_CARD_PAGINATED_TABS:
        total = max(0, int(page_data.get("total") or 0))
        per_page = max(1, int(page_data.get("per_page") or USER_CARD_PAGE_SIZE))
        max_page = max(0, (total - 1) // per_page) if total else 0
        nav_row: list[InlineKeyboardButton] = []
        if current_page > 0:
            nav_row.append(InlineKeyboardButton(
                text="◀️",
                callback_data=_user_card_tab_callback(user_backend_id, tab, current_page - 1),
            ))
        if total:
            nav_row.append(InlineKeyboardButton(
                text=f"{current_page + 1}/{max_page + 1}",
                callback_data="admin:ucard:noop",
            ))
        if current_page < max_page:
            nav_row.append(InlineKeyboardButton(
                text="▶️",
                callback_data=_user_card_tab_callback(user_backend_id, tab, current_page + 1),
            ))
        if nav_row:
            keyboard.append(nav_row)

    contact_row: list[InlineKeyboardButton] = []
    if username_url:
        contact_row.append(InlineKeyboardButton(text="✉️ Написать", url=f"https://t.me/{username_url}"))
    elif telegram_id:
        contact_row.append(InlineKeyboardButton(text="✉️ Написать", url=f"tg://user?id={telegram_id}"))
    if username_url:
        contact_row.append(InlineKeyboardButton(text="👁 Профиль", url=f"https://t.me/{username_url}"))
    if contact_row:
        keyboard.append(contact_row)

    if role not in ("admin", "owner"):
        keyboard.append([
            InlineKeyboardButton(text="💰 Пополнить", callback_data=f"admin:balance:add:{user_backend_id}"),
            InlineKeyboardButton(text="💸 Списать", callback_data=f"admin:balance:subtract:{user_backend_id}"),
        ])
        keyboard.append([
            InlineKeyboardButton(
                text="🔒 Убрать полный доступ" if full_access_override else "✅ Дать полный доступ",
                callback_data=(
                    f"admin:access:revoke:confirm:{user_backend_id}"
                    if full_access_override else f"admin:access:grant:confirm:{user_backend_id}"
                ),
            ),
            InlineKeyboardButton(
                text="✅ Разблокировать" if is_blocked else "⛔ Заблокировать",
                callback_data=(
                    f"admin:unblock:confirm:{user_backend_id}"
                    if is_blocked else f"admin:block:confirm:{user_backend_id}"
                ),
            ),
        ])

    keyboard.append([
        InlineKeyboardButton(text="📋 Все заказы", callback_data=f"admin:user:orders:{user_backend_id}:0"),
        InlineKeyboardButton(text="📡 Все блогеры", callback_data=f"admin:wbloggers:{user_backend_id}:0"),
    ])
    keyboard.append([
        InlineKeyboardButton(text="🔄 Обновить", callback_data=_user_card_tab_callback(user_backend_id, tab, current_page)),
        InlineKeyboardButton(text="◀️ К списку", callback_data="admin:users"),
    ])
    return InlineKeyboardMarkup(inline_keyboard=keyboard)


def _slice_user_card_items(items: list, page: int) -> tuple[list, int, int]:
    total = len(items)
    max_page = max(0, (total - 1) // USER_CARD_PAGE_SIZE) if total else 0
    safe_page = max(0, min(page, max_page))
    start = safe_page * USER_CARD_PAGE_SIZE
    return items[start:start + USER_CARD_PAGE_SIZE], safe_page, total


async def _attach_user_card_page_data(user_detail: dict, tab: str, page: int) -> None:
    """Loads live page items for heavy card tabs without bloating the overview."""
    if tab not in USER_CARD_PAGINATED_TABS:
        return

    user_backend_id = int(user_detail.get("id") or 0)
    if not user_backend_id:
        return

    try:
        if tab == "orders":
            items = await api_client.list_tasks(user_id=user_backend_id) or []
            page_items, safe_page, total = _slice_user_card_items(list(items), page)
        elif tab == "monitoring":
            items = await api_client.list_video_creators(user_backend_id) or []
            page_items, safe_page, total = _slice_user_card_items(list(items), page)
        elif tab == "templates":
            task_templates = await api_client.list_task_templates(user_backend_id) or []
            comment_templates = await api_client.list_comment_templates(user_backend_id) or []
            items = [
                {**template, "_kind": "task"}
                for template in task_templates
            ] + [
                {**template, "_kind": "comment"}
                for template in comment_templates
            ]
            items.sort(key=lambda item: str(item.get("created_at") or ""), reverse=True)
            page_items, safe_page, total = _slice_user_card_items(items, page)
        elif tab == "accounts":
            response = await api_client.list_personal_accounts(
                user_backend_id,
                page=max(0, page),
                page_size=USER_CARD_PAGE_SIZE,
            )
            if isinstance(response, dict):
                page_items = list(response.get("items") or response.get("accounts") or [])
                total = int(response.get("total") or len(page_items) or 0)
                safe_page = max(0, int(response.get("page") or page or 0))
                max_page = max(0, (total - 1) // USER_CARD_PAGE_SIZE) if total else 0
                safe_page = min(safe_page, max_page)
            else:
                page_items, safe_page, total = _slice_user_card_items(list(response or []), page)
        else:
            return
    except Exception as exc:
        logger.warning(f"Failed to load user card page data for user {user_backend_id}, tab={tab}: {exc}")
        page_items, safe_page, total = [], max(0, page), 0

    user_detail["_card_page"] = {
        "tab": tab,
        "page": safe_page,
        "per_page": USER_CARD_PAGE_SIZE,
        "total": total,
        "items": page_items,
    }


async def _render_user_card(user_backend_id: int, tab: str = "overview", page: int = 0) -> tuple[str | None, InlineKeyboardMarkup | None]:
    """Builds the rich admin card for a worker."""
    user_detail = await api_client.get_user(user_backend_id)
    if not user_detail:
        return None, None
    safe_tab = tab if tab in {"overview", "finance", "orders", "monitoring", "templates", "accounts"} else "overview"
    await _attach_user_card_page_data(user_detail, safe_tab, max(0, page))
    return _render_user_card_text(user_detail, safe_tab), _build_user_card_keyboard(user_detail, safe_tab)


@router.callback_query(F.data.startswith("admin:ucard:"))
async def show_user_card_tab(callback: CallbackQuery):
    """Переключает вкладки расширенной карточки работника."""
    try:
        if not callback.message:
            await callback.answer("Ошибка: сообщение не найдено", show_alert=True)
            return

        parts = (callback.data or "").split(":")
        if parts == ["admin", "ucard", "noop"]:
            await callback.answer()
            return
        if len(parts) not in (4, 5) or not parts[2].isdigit():
            await callback.answer("Некорректный запрос", show_alert=True)
            return

        await callback.answer()
        user_backend_id = int(parts[2])
        tab = parts[3]
        page = int(parts[4]) if len(parts) == 5 and parts[4].lstrip("-").isdigit() else 0

        text, keyboard = await _render_user_card(user_backend_id, tab=tab, page=page)
        if text is None or keyboard is None:
            await callback.answer("Пользователь не найден", show_alert=True)
            return

        await _safe_edit_callback_text(
            callback,
            text,
            reply_markup=keyboard,
            parse_mode="Markdown",
            unchanged_notice=None,
        )
    except Exception as e:
        logger.error(f"Error switching user card tab: {e}", exc_info=True)
        try:
            await callback.answer("Ошибка загрузки карточки", show_alert=True)
        except Exception:
            pass


@router.callback_query(F.data.regexp(r"^admin:user:\d+$"))
async def show_user_details(callback: CallbackQuery):
    """Показывает детали пользователя с балансом и управлением.

    Принимает только формат ровно "admin:user:{int}", чтобы не перехватывать
    отдельные подкоманды вроде "admin:user:orders:{id}:{page}".
    """
    try:
        if not callback.message:
            await callback.answer("Ошибка: сообщение не найдено", show_alert=True)
            return

        parts = (callback.data or "").split(":")
        await callback.answer()
        user_backend_id = int(parts[2])

        text, keyboard = await _render_user_card(user_backend_id)
        if text is None or keyboard is None:
            try:
                await callback.answer("Пользователь не найден", show_alert=True)
            except Exception:
                pass
            return

        await callback.message.edit_text(
            text,
            reply_markup=keyboard,
            parse_mode="Markdown"
        )
    except Exception as e:
        logger.error(f"Error showing user details: {e}", exc_info=True)
        try:
            await callback.answer("Ошибка проверки доступа", show_alert=True)
        except Exception:
            pass


async def _refresh_user_details(callback: CallbackQuery, user_backend_id: int):
    """Обновляет информацию о пользователе после изменения баланса/блокировки."""
    try:
        if not callback.message:
            return
        text, keyboard = await _render_user_card(user_backend_id)
        if text is None or keyboard is None:
            await callback.answer("Пользователь не найден", show_alert=True)
            return

        await callback.message.edit_text(
            text,
            reply_markup=keyboard,
            parse_mode="Markdown"
        )
    except Exception as e:
        logger.error(f"Error refreshing user details: {e}", exc_info=True)
        try:
            await callback.answer("Ошибка обновления информации", show_alert=True)
        except Exception:
            pass


_WORKER_BLOGGERS_PER_PAGE = 10
_WORKER_BLOGGER_SEARCH_PER_PAGE = 10


def _clamp_page(page: int, total: int, per_page: int) -> int:
    max_page = max(0, (max(0, total) - 1) // max(1, per_page)) if total else 0
    return max(0, min(page, max_page))


def _format_creator_last_check(raw: str | None) -> str:
    if not raw:
        return "—"
    try:
        from datetime import datetime, timezone
        dt = datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.strftime("%m-%d %H:%M")
    except Exception:
        return str(raw)[:16]


def _filter_worker_bloggers(creators: list[dict], query: str) -> list[dict]:
    needle = (query or "").strip().lstrip("@").lower()
    if not needle:
        return []

    def username(creator: dict) -> str:
        return str(creator.get("username") or "").strip().lstrip("@").lower()

    matches = [creator for creator in creators if needle in username(creator)]
    matches.sort(
        key=lambda creator: (
            0 if username(creator) == needle else 1 if username(creator).startswith(needle) else 2,
            username(creator),
        )
    )
    return matches


def _worker_blogger_button_text(creator: dict, index: int | None = None) -> str:
    username = str(creator.get("username") or "unknown").strip().lstrip("@") or "unknown"
    enabled = bool(creator.get("enabled", True))
    mode = creator.get("mode") or "manual_review"
    mode_emoji = {
        "auto_comment": "🤖",
        "manual_review": "👀",
        "notify_only": "🔔",
    }.get(mode, "👀")
    status_icon = "🟢" if enabled else "⚪️"
    prefix = f"{index:02d}. " if index is not None else ""
    new_count = int(creator.get("new_videos_count") or 0)
    text = f"{prefix}{status_icon} @{str(username)[:24]} · {mode_emoji}"
    if new_count:
        text += f" · 🆕 {new_count}"
    return text


def _build_worker_blogger_search_result(
    matches: list[dict],
    *,
    user_backend_id: int,
    owner_label: str,
    query: str,
    page: int = 0,
    per_page: int = _WORKER_BLOGGER_SEARCH_PER_PAGE,
) -> tuple[str, InlineKeyboardMarkup, int]:
    page = _clamp_page(page, len(matches), per_page)
    start = page * per_page
    end = min(start + per_page, len(matches))
    page_items = matches[start:end]
    safe_query = markdown_inline_code(query[:80])

    rows: list[list[InlineKeyboardButton]] = []
    for creator in page_items:
        rows.append([
            InlineKeyboardButton(
                text=_worker_blogger_button_text(creator),
                callback_data=f"admin:wblogger:{user_backend_id}:{creator.get('id')}:0",
            )
        ])

    max_page = max(0, (len(matches) - 1) // max(1, per_page)) if matches else 0
    nav_row: list[InlineKeyboardButton] = []
    if page > 0:
        nav_row.append(InlineKeyboardButton(
            text="◀️",
            callback_data=f"admin:wbloggers:search_page:{user_backend_id}:{page - 1}",
        ))
    if max_page > 0:
        nav_row.append(InlineKeyboardButton(text=f"{page + 1}/{max_page + 1}", callback_data="admin:wbloggers:noop"))
    if page < max_page:
        nav_row.append(InlineKeyboardButton(
            text="▶️",
            callback_data=f"admin:wbloggers:search_page:{user_backend_id}:{page + 1}",
        ))
    if nav_row:
        rows.append(nav_row)

    rows.append([
        InlineKeyboardButton(text="🔍 Искать ещё", callback_data=f"admin:wbloggers:search:{user_backend_id}"),
        InlineKeyboardButton(text="📡 Все блогеры", callback_data=f"admin:wbloggers:{user_backend_id}:0"),
    ])

    text = (
        f"🔍 *Найдено: {len(matches)}* у {owner_label} по запросу {safe_query}.\n"
        f"Показано `{start + 1}-{end}` из `{len(matches)}`.\n\n"
        "Нажмите на блогера, чтобы открыть карточку."
    )
    return text, InlineKeyboardMarkup(inline_keyboard=rows), page


def _format_worker_blogger_card_text(creator: dict, owner_label: str) -> str:
    username = str(creator.get("username") or "unknown").strip().lstrip("@") or "unknown"
    enabled = bool(creator.get("enabled", True))
    mode = creator.get("mode") or "manual_review"
    mode_pretty = {
        "auto_comment": "🤖 Автокомментинг",
        "manual_review": "👀 Ручная проверка",
        "notify_only": "🔔 Только уведомления",
    }.get(mode, str(mode))
    interval = creator.get("interval_minutes")
    interval_part = f"{interval} мин" if interval is not None else "—"
    new_count = int(creator.get("new_videos_count") or 0)
    last_check = _format_creator_last_check(creator.get("last_checked_at"))
    last_seen = str(creator.get("last_seen_video_id") or "").strip()
    template_ids = creator.get("template_ids") or []
    default_template_id = creator.get("default_template_id")
    created_at = _format_compact_date(creator.get("created_at"))
    updated_at = _format_compact_date(creator.get("updated_at"))

    lines = [
        "📡 *Карточка блогера работника*",
        "━━━━━━━━━━━━━━━━━━━━",
        "",
        f"👤 Работник: {owner_label}",
        f"🎬 Блогер: {markdown_handle(username)}",
        "",
        f"{'🟢 Включён' if enabled else '⚪️ Выключен'}",
        f"📊 Режим: *{mode_pretty}*",
        f"⏱ Интервал: `{interval_part}`",
        f"🆕 Новых видео: `{new_count}`",
        f"🕒 Последняя проверка: `{last_check}`",
    ]
    if last_seen:
        lines.append(f"🎥 Последнее видео: `{last_seen[:18]}…`")
    else:
        lines.append("🎥 Последнее видео: `—`")

    lines.extend([
        "",
        "*Автокомментинг:*",
        f"⭐ Шаблон авто-ответа: `{default_template_id or 'не выбран'}`",
        f"🧩 Список шаблонов: `{len(template_ids)}`",
        "",
        "*Служебно:*",
        f"ID подключения: `{creator.get('id')}`",
        f"Создан: `{created_at}`",
        f"Обновлён: `{updated_at}`",
    ])
    return "\n".join(lines)


def _worker_blogger_card_keyboard(user_backend_id: int, creator_id: int, page: int, username: str | None) -> InlineKeyboardMarkup:
    rows: list[list[InlineKeyboardButton]] = []
    clean_username = str(username or "").strip().lstrip("@")
    if clean_username:
        rows.append([InlineKeyboardButton(text="👁 Открыть TikTok", url=f"https://www.tiktok.com/@{clean_username}")])
    rows.append([
        InlineKeyboardButton(text="🔄 Обновить", callback_data=f"admin:wblogger:{user_backend_id}:{creator_id}:{page}"),
        InlineKeyboardButton(text="◀️ К блогерам", callback_data=f"admin:wbloggers:{user_backend_id}:{page}"),
    ])
    rows.append([InlineKeyboardButton(text="👤 К карточке работника", callback_data=f"admin:user:{user_backend_id}")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


@router.callback_query(F.data.regexp(r"^admin:wbloggers:search:(\d+)$"))
async def search_worker_bloggers_prompt(callback: CallbackQuery, state: FSMContext):
    """Запрашивает поиск блогера внутри карточки конкретного работника."""
    match = re.match(r"^admin:wbloggers:search:(\d+)$", callback.data or "")
    if not match:
        await callback.answer("Некорректный запрос", show_alert=True)
        return
    user_backend_id = int(match.group(1))
    try:
        if not callback.message:
            await callback.answer("Ошибка: сообщение не найдено", show_alert=True)
            return
        await callback.answer()
        await state.set_state(OrderStates.ADMIN_WORKER_BLOGGER_SEARCH)
        await state.set_data({"worker_blogger_user_id": user_backend_id})
        await _safe_edit_callback_text(
            callback,
            "🔍 *Поиск блогера работника*\n\n"
            "Введите username или часть username.\n"
            "Результаты откроют карточку конкретного подключения блогера.",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="◀️ К блогерам", callback_data=f"admin:wbloggers:{user_backend_id}:0")]
            ]),
            parse_mode="Markdown",
        )
    except Exception as e:
        logger.error(f"Error opening worker blogger search: {e}", exc_info=True)
        await callback.answer("Ошибка поиска", show_alert=True)


@router.message(StateFilter(OrderStates.ADMIN_WORKER_BLOGGER_SEARCH))
async def search_worker_bloggers_input(message: Message, state: FSMContext):
    """Ищет блогера конкретного работника и даёт открыть карточку."""
    data = await state.get_data()
    user_backend_id = int(data.get("worker_blogger_user_id") or 0)
    query = (message.text or "").strip().lstrip("@")
    if not user_backend_id:
        await state.clear()
        await message.answer("Не удалось понять, у какого работника искать блогера. Откройте карточку заново.")
        return
    if not query:
        await message.answer("Введите username или часть username.")
        return

    try:
        creators = await api_client.list_video_creators(user_backend_id)
        matches = _filter_worker_bloggers(creators or [], query)
        user_detail = await api_client.get_user(user_backend_id)
        owner_label = markdown_inline_code(str(user_detail.get("username") or f"ID: {user_detail.get('telegram_id')}"))
        await state.update_data(
            worker_blogger_user_id=user_backend_id,
            worker_blogger_search_query=query,
        )
    except Exception as e:
        logger.error(f"Error searching worker bloggers: {e}", exc_info=True)
        await state.clear()
        await message.answer("Ошибка загрузки блогеров работника. Попробуйте ещё раз.")
        return

    if not matches:
        await message.answer(
            f"🔍 У {owner_label} ничего не найдено по запросу {safe_query}.\n"
            f"Всего блогеров: `{len(creators or [])}`.",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="🔍 Искать ещё", callback_data=f"admin:wbloggers:search:{user_backend_id}")],
                [InlineKeyboardButton(text="📡 Все блогеры", callback_data=f"admin:wbloggers:{user_backend_id}:0")],
            ]),
            parse_mode="Markdown",
        )
        return

    text, keyboard, _ = _build_worker_blogger_search_result(
        matches,
        user_backend_id=user_backend_id,
        owner_label=owner_label,
        query=query,
        page=0,
    )
    await message.answer(
        text,
        reply_markup=keyboard,
        parse_mode="Markdown",
    )


@router.callback_query(F.data.regexp(r"^admin:wbloggers:search_page:(\d+):(\d+)$"))
async def search_worker_bloggers_page(callback: CallbackQuery, state: FSMContext):
    """Постранично показывает результаты поиска блогеров конкретного работника."""
    match = re.match(r"^admin:wbloggers:search_page:(\d+):(\d+)$", callback.data or "")
    if not match:
        await callback.answer("Некорректный запрос", show_alert=True)
        return
    user_backend_id = int(match.group(1))
    page = int(match.group(2))
    if not callback.message:
        await callback.answer("Ошибка: сообщение не найдено", show_alert=True)
        return
    data = await state.get_data()
    query = str(data.get("worker_blogger_search_query") or "").strip()
    if int(data.get("worker_blogger_user_id") or 0) != user_backend_id or not query:
        await callback.answer("Поиск устарел. Введите запрос ещё раз.", show_alert=True)
        await state.set_state(OrderStates.ADMIN_WORKER_BLOGGER_SEARCH)
        await state.set_data({"worker_blogger_user_id": user_backend_id})
        await _safe_edit_callback_text(
            callback,
            "🔍 *Поиск блогера работника*\n\nВведите username или часть username.",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="◀️ К блогерам", callback_data=f"admin:wbloggers:{user_backend_id}:0")]
            ]),
            parse_mode="Markdown",
        )
        return

    try:
        await callback.answer()
        creators = await api_client.list_video_creators(user_backend_id)
        matches = _filter_worker_bloggers(creators or [], query)
        if not matches:
            await callback.answer("Совпадений больше нет. Обновляю поиск.", show_alert=False)
            await state.set_state(OrderStates.ADMIN_WORKER_BLOGGER_SEARCH)
            await state.set_data({"worker_blogger_user_id": user_backend_id})
            await _safe_edit_callback_text(
                callback,
                "🔍 *Поиск блогера работника*\n\nСовпадения исчезли. Введите запрос ещё раз.",
                reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                    [InlineKeyboardButton(text="◀️ К блогерам", callback_data=f"admin:wbloggers:{user_backend_id}:0")]
                ]),
                parse_mode="Markdown",
            )
            return
        user_detail = await api_client.get_user(user_backend_id)
        owner_label = markdown_inline_code(str(user_detail.get("username") or f"ID: {user_detail.get('telegram_id')}"))
        text, keyboard, _ = _build_worker_blogger_search_result(
            matches,
            user_backend_id=user_backend_id,
            owner_label=owner_label,
            query=query,
            page=page,
        )
        await _safe_edit_callback_text(callback, text, reply_markup=keyboard, parse_mode="Markdown")
    except Exception as e:
        logger.error(f"Error paginating worker blogger search: {e}", exc_info=True)
        await callback.answer("Ошибка загрузки страницы", show_alert=True)


@router.callback_query(F.data.regexp(r"^admin:wblogger:(\d+):(\d+):(\d+)$"))
async def show_worker_blogger_detail(callback: CallbackQuery):
    """Карточка одного блогера из списка работника."""
    match = re.match(r"^admin:wblogger:(\d+):(\d+):(\d+)$", callback.data or "")
    if not match:
        await callback.answer("Некорректный запрос", show_alert=True)
        return
    user_backend_id = int(match.group(1))
    creator_id = int(match.group(2))
    page = max(0, int(match.group(3)))
    try:
        if not callback.message:
            await callback.answer("Ошибка: сообщение не найдено", show_alert=True)
            return
        await callback.answer()
        creators = await api_client.list_video_creators(user_backend_id)
        creator = next((c for c in (creators or []) if int(c.get("id") or 0) == creator_id), None)
        if not creator:
            await callback.answer("Блогер не найден у этого работника", show_alert=True)
            return
        user_detail = await api_client.get_user(user_backend_id)
        owner_label = markdown_inline_code(str(user_detail.get("username") or f"ID: {user_detail.get('telegram_id')}"))
        await _safe_edit_callback_text(
            callback,
            _format_worker_blogger_card_text(creator, owner_label),
            reply_markup=_worker_blogger_card_keyboard(
                user_backend_id,
                creator_id,
                page,
                creator.get("username"),
            ),
            parse_mode="Markdown",
        )
    except Exception as e:
        logger.error(f"Error showing worker blogger detail: {e}", exc_info=True)
        await callback.answer("Ошибка загрузки блогера", show_alert=True)


@router.callback_query(F.data.regexp(r"^admin:wbloggers:(\d+):(\d+)$"))
async def show_worker_bloggers(callback: CallbackQuery, state: FSMContext):
    """Постраничный список блогеров конкретного работника.

    Формат callback: ``admin:wbloggers:{user_id}:{page}``. Префикс не
    пересекается с ``admin:user:`` поэтому коллизий с карточкой нет.
    Используется уже существующий backend-эндпоинт ``list_video_creators``.
    """
    try:
        if not callback.message:
            await callback.answer("Ошибка: сообщение не найдено", show_alert=True)
            return

        parts = (callback.data or "").split(":")
        if len(parts) != 4 or not parts[2].isdigit() or not parts[3].lstrip("-").isdigit():
            await callback.answer("Некорректный запрос", show_alert=True)
            return

        await callback.answer()
        try:
            await state.clear()
        except Exception:
            pass
        user_backend_id = int(parts[2])
        page = max(0, int(parts[3]))

        try:
            creators = await api_client.list_video_creators(user_backend_id)
        except Exception as exc:
            logger.warning(f"Failed to load worker bloggers: {exc}")
            creators = []

        creators = creators or []
        total = len(creators)
        max_page = max(0, (total - 1) // _WORKER_BLOGGERS_PER_PAGE) if total else 0
        page = min(page, max_page)
        start = page * _WORKER_BLOGGERS_PER_PAGE
        end = min(start + _WORKER_BLOGGERS_PER_PAGE, total)
        page_items = creators[start:end]

        # Заголовок: подтянем имя пользователя
        try:
            user_detail = await api_client.get_user(user_backend_id)
            owner_label = markdown_inline_code(
                str(user_detail.get("username") or f"ID: {user_detail.get('telegram_id')}")
            )
        except Exception:
            owner_label = f"`#{user_backend_id}`"

        text = "📡 *Блогеры работника*\n"
        text += "━━━━━━━━━━━━━━━━━━━━\n\n"
        text += f"👤 {owner_label}\n"
        text += f"Всего: `{total}`"
        if total:
            enabled_count = sum(1 for c in creators if bool(c.get("enabled", True)))
            text += f" · Активных: `{enabled_count}`\n"
            text += f"Показано `{start + 1}-{end}` из `{total}`\n\n"
        else:
            text += "\n\n_Работник пока не добавил ни одного блогера._\n"

        for idx, creator in enumerate(page_items, start=start + 1):
            uname = creator.get("username") or "—"
            try:
                uname_md = markdown_handle(str(uname))
            except Exception:
                uname_md = str(uname)
            mode = creator.get("mode") or "—"
            interval = creator.get("interval_minutes")
            enabled = bool(creator.get("enabled", True))
            new_count = int(creator.get("new_videos_count") or 0)
            last_check = _format_creator_last_check(creator.get("last_checked_at"))

            status_icon = "🟢" if enabled else "⚪️"
            text += f"{idx:02d}. {status_icon} {uname_md}\n"
            mode_pretty = {
                "auto_comment": "🤖 авто",
                "manual_review": "👀 ручная",
                "notify_only": "🔔 уведомл.",
            }.get(mode, mode)
            interval_part = f"{interval} мин" if interval is not None else "—"
            text += f"     ⏱ `{interval_part}` · {mode_pretty} · 🆕 `{new_count}` · 🕒 `{last_check}`\n"

        keyboard: list[list[InlineKeyboardButton]] = []
        for idx, creator in enumerate(page_items, start=start + 1):
            creator_id = creator.get("id")
            if creator_id is None:
                continue
            keyboard.append([
                InlineKeyboardButton(
                    text=_worker_blogger_button_text(creator, idx),
                    callback_data=f"admin:wblogger:{user_backend_id}:{creator_id}:{page}",
                )
            ])

        nav_row: list[InlineKeyboardButton] = []
        if page > 0:
            nav_row.append(InlineKeyboardButton(
                text="◀️",
                callback_data=f"admin:wbloggers:{user_backend_id}:{page - 1}"
            ))
        if total:
            nav_row.append(InlineKeyboardButton(
                text=f"{page + 1}/{max_page + 1}",
                callback_data="admin:wbloggers:noop"
            ))
        if page < max_page:
            nav_row.append(InlineKeyboardButton(
                text="▶️",
                callback_data=f"admin:wbloggers:{user_backend_id}:{page + 1}"
            ))
        if nav_row:
            keyboard.append(nav_row)

        keyboard.append([
            InlineKeyboardButton(
                text="🔍 Найти блогера",
                callback_data=f"admin:wbloggers:search:{user_backend_id}",
            )
        ])
        keyboard.append([
            InlineKeyboardButton(
                text="🔄 Обновить",
                callback_data=f"admin:wbloggers:{user_backend_id}:{page}"
            )
        ])
        keyboard.append([
            InlineKeyboardButton(
                text="◀️ К карточке",
                callback_data=f"admin:user:{user_backend_id}"
            )
        ])

        await _safe_edit_callback_text(
            callback,
            text,
            reply_markup=InlineKeyboardMarkup(inline_keyboard=keyboard),
            parse_mode="Markdown",
        )
    except Exception as e:
        logger.error(f"Error showing worker bloggers: {e}", exc_info=True)
        try:
            await callback.answer("Ошибка загрузки списка блогеров", show_alert=True)
        except Exception:
            pass


@router.callback_query(F.data == "admin:wbloggers:noop")
async def show_worker_bloggers_noop(callback: CallbackQuery):
    try:
        await callback.answer()
    except Exception:
        pass


def _clear_worker_access_cache(user_detail: dict) -> None:
    try:
        telegram_id = int(user_detail.get("telegram_id") or 0)
        if not telegram_id:
            return
        from bot.services.subscription_service import clear_access_cache

        clear_access_cache(telegram_id)
        api_client.clear_user_cache(telegram_id)
        clear_admin_mode_cache(telegram_id)
    except Exception:
        pass


@router.callback_query(F.data.startswith("admin:access:grant:confirm:"))
async def grant_full_access_confirm(callback: CallbackQuery):
    """Показывает подтверждение выдачи полного доступа работнику."""
    try:
        if not callback.message:
            await callback.answer("Ошибка: сообщение не найдено", show_alert=True)
            return
        await callback.answer()
        user_backend_id = int((callback.data or "").split(":")[-1])
        user_detail = await api_client.get_user(user_backend_id)
        username = markdown_inline_code(str(user_detail.get("username") or f"ID: {user_detail.get('telegram_id')}"))

        await callback.message.edit_text(
            "*Выдать полный доступ*\n\n"
            f"Выдать работнику {username} персональный доступ к полному режиму?\n\n"
            "Он сможет пользоваться заказами, шаблонами, автокомментингом, балансом "
            "и личными аккаунтами даже когда глобально включён режим парсинга.\n\n"
            "Админ-панель и роль администратора не выдаются.",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="✅ Выдать полный доступ", callback_data=f"admin:access:grant:do:{user_backend_id}")],
                [InlineKeyboardButton(text="◀️ Назад", callback_data=f"admin:user:{user_backend_id}")]
            ]),
            parse_mode="Markdown"
        )
    except Exception as e:
        logger.error(f"Error in grant full access confirm: {e}", exc_info=True)
        await callback.answer("Ошибка подтверждения", show_alert=True)


@router.callback_query(F.data.startswith("admin:access:revoke:confirm:"))
async def revoke_full_access_confirm(callback: CallbackQuery):
    """Показывает подтверждение снятия полного доступа у работника."""
    try:
        if not callback.message:
            await callback.answer("Ошибка: сообщение не найдено", show_alert=True)
            return
        await callback.answer()
        user_backend_id = int((callback.data or "").split(":")[-1])
        user_detail = await api_client.get_user(user_backend_id)
        username = markdown_inline_code(str(user_detail.get("username") or f"ID: {user_detail.get('telegram_id')}"))

        await callback.message.edit_text(
            "*Убрать полный доступ*\n\n"
            f"Убрать у работника {username} персональный полный режим?\n\n"
            "Обычный доступ к боту, баланс, заказы и роль не меняются. "
            "Если глобально включён режим парсинга, работник снова будет видеть только функции парсинга.",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="🔒 Убрать полный доступ", callback_data=f"admin:access:revoke:do:{user_backend_id}")],
                [InlineKeyboardButton(text="◀️ Назад", callback_data=f"admin:user:{user_backend_id}")]
            ]),
            parse_mode="Markdown"
        )
    except Exception as e:
        logger.error(f"Error in revoke full access confirm: {e}", exc_info=True)
        await callback.answer("Ошибка подтверждения", show_alert=True)


@router.callback_query(F.data.startswith("admin:access:grant:do:"))
async def grant_full_access_handler(callback: CallbackQuery):
    """Выдаёт работнику полный доступ к боту."""
    try:
        if not callback.message:
            await callback.answer("Ошибка: сообщение не найдено", show_alert=True)
            return

        user_backend_id = int((callback.data or "").split(":")[-1])
        await callback.answer("Выдаю полный доступ...")
        user_detail = await api_client.get_user(user_backend_id)
        await log_admin_action(
            admin_telegram_id=callback.from_user.id,
            action_type="grant_full_access",
            target_user_id=user_backend_id,
            details={"username": user_detail.get("username"), "telegram_id": user_detail.get("telegram_id")},
            description=f"Выдача полного доступа пользователю {user_detail.get('username') or user_backend_id}"
        )

        await api_client.grant_full_access(user_backend_id)
        _clear_worker_access_cache(user_detail)
        await callback.answer("✅ Полный доступ выдан", show_alert=True)
        await _refresh_user_details(callback, user_backend_id)
    except Exception as e:
        logger.error(f"Error granting full access: {e}", exc_info=True)
        try:
            await callback.answer(f"{Emoji.ERROR} Ошибка выдачи доступа", show_alert=True)
        except Exception:
            pass


@router.callback_query(F.data.startswith("admin:access:revoke:do:"))
async def revoke_full_access_handler(callback: CallbackQuery):
    """Убирает у работника полный доступ к боту."""
    try:
        if not callback.message:
            await callback.answer("Ошибка: сообщение не найдено", show_alert=True)
            return

        user_backend_id = int((callback.data or "").split(":")[-1])
        await callback.answer("Убираю полный доступ...")
        user_detail = await api_client.get_user(user_backend_id)
        await log_admin_action(
            admin_telegram_id=callback.from_user.id,
            action_type="revoke_full_access",
            target_user_id=user_backend_id,
            details={"username": user_detail.get("username"), "telegram_id": user_detail.get("telegram_id")},
            description=f"Снятие полного доступа у пользователя {user_detail.get('username') or user_backend_id}"
        )

        await api_client.revoke_full_access(user_backend_id)
        _clear_worker_access_cache(user_detail)
        await callback.answer("🔒 Полный доступ убран", show_alert=True)
        await _refresh_user_details(callback, user_backend_id)
    except Exception as e:
        logger.error(f"Error revoking full access: {e}", exc_info=True)
        try:
            await callback.answer(f"{Emoji.ERROR} Ошибка снятия доступа", show_alert=True)
        except Exception:
            pass


@router.callback_query(F.data.startswith("admin:block:confirm:"))
async def block_user_confirm(callback: CallbackQuery):
    """Показывает подтверждение блокировки пользователя."""
    try:
        if not callback.message:
            await callback.answer("Ошибка: сообщение не найдено", show_alert=True)
            return
        await callback.answer()
        user_backend_id = int(callback.data.split(":")[-1])
        user_detail = await api_client.get_user(user_backend_id)
        username = user_detail.get("username") or f"ID: {user_detail.get('telegram_id')}"

        await callback.message.edit_text(
            "*Подтверждение блокировки*\n\n"
            f"Заблокировать пользователя *{username}*?\n\n"
            "После блокировки пользователь не сможет работать с ботом.",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="✅ Заблокировать", callback_data=f"admin:block:do:{user_backend_id}")],
                [InlineKeyboardButton(text="◀️ Назад", callback_data=f"admin:user:{user_backend_id}")]
            ]),
            parse_mode="Markdown"
        )
    except Exception as e:
        logger.error(f"Error in block confirm: {e}", exc_info=True)
        await callback.answer("Ошибка подтверждения", show_alert=True)


@router.callback_query(F.data.startswith("admin:unblock:confirm:"))
async def unblock_user_confirm(callback: CallbackQuery):
    """Показывает подтверждение разблокировки пользователя."""
    try:
        if not callback.message:
            await callback.answer("Ошибка: сообщение не найдено", show_alert=True)
            return
        await callback.answer()
        user_backend_id = int(callback.data.split(":")[-1])
        user_detail = await api_client.get_user(user_backend_id)
        username = user_detail.get("username") or f"ID: {user_detail.get('telegram_id')}"

        await callback.message.edit_text(
            "*Подтверждение разблокировки*\n\n"
            f"Разблокировать пользователя *{username}*?",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="✅ Разблокировать", callback_data=f"admin:unblock:do:{user_backend_id}")],
                [InlineKeyboardButton(text="◀️ Назад", callback_data=f"admin:user:{user_backend_id}")]
            ]),
            parse_mode="Markdown"
        )
    except Exception as e:
        logger.error(f"Error in unblock confirm: {e}", exc_info=True)
        await callback.answer("Ошибка подтверждения", show_alert=True)


@router.callback_query(F.data.startswith("admin:block:do:"))
async def block_user_handler(callback: CallbackQuery):
    """Блокирует пользователя."""
    try:
        if not callback.message:
            await callback.answer("Ошибка: сообщение не найдено", show_alert=True)
            return
        # ИСПРАВЛЕНО: Убрана дублирующая проверка прав - middleware уже проверяет

        user_backend_id = int(callback.data.split(":")[-1])
        await callback.answer("Блокировка пользователя...")
        
        # ИСПРАВЛЕНО: Логируем критическую операцию
        user_detail = await api_client.get_user(user_backend_id)
        await log_admin_action(
            admin_telegram_id=callback.from_user.id,
            action_type="block",
            target_user_id=user_backend_id,
            details={"username": user_detail.get("username"), "telegram_id": user_detail.get("telegram_id")},
            description=f"Блокировка пользователя {user_detail.get('username') or user_backend_id}"
        )
        
        await api_client.block_user(user_backend_id)
        _clear_worker_access_cache(user_detail)
        await callback.answer("🔒 Пользователь заблокирован", show_alert=True)
        # Обновляем информацию о пользователе
        await _refresh_user_details(callback, user_backend_id)
    except Exception as e:
        logger.error(f"Error blocking user: {e}", exc_info=True)
        try:
            await callback.answer(f"{Emoji.ERROR} Ошибка блокировки", show_alert=True)
        except Exception:
            pass


@router.callback_query(F.data.startswith("admin:unblock:do:"))
async def unblock_user_handler(callback: CallbackQuery):
    """Разблокирует пользователя."""
    try:
        if not callback.message:
            await callback.answer("Ошибка: сообщение не найдено", show_alert=True)
            return
        # ИСПРАВЛЕНО: Убрана дублирующая проверка прав - middleware уже проверяет

        user_backend_id = int(callback.data.split(":")[-1])
        await callback.answer("Разблокировка пользователя...")
        
        # ИСПРАВЛЕНО: Логируем критическую операцию
        user_detail = await api_client.get_user(user_backend_id)
        await log_admin_action(
            admin_telegram_id=callback.from_user.id,
            action_type="unblock",
            target_user_id=user_backend_id,
            details={"username": user_detail.get("username"), "telegram_id": user_detail.get("telegram_id")},
            description=f"Разблокировка пользователя {user_detail.get('username') or user_backend_id}"
        )
        
        await api_client.unblock_user(user_backend_id)
        _clear_worker_access_cache(user_detail)
        await callback.answer("🔓 Пользователь разблокирован", show_alert=True)
        # Обновляем информацию о пользователе
        await _refresh_user_details(callback, user_backend_id)
    except Exception as e:
        logger.error(f"Error unblocking user: {e}", exc_info=True)
        try:
            await callback.answer(f"{Emoji.ERROR} Ошибка разблокировки", show_alert=True)
        except Exception:
            pass


@router.callback_query(F.data.startswith("admin:balance:add:"))
async def start_add_balance(callback: CallbackQuery, state: FSMContext):
    """Начинает процесс пополнения баланса."""
    # ИСПРАВЛЕНО: Убрана дублирующая проверка прав - middleware уже проверяет
    
    try:
        if not callback.message:
            await callback.answer("Ошибка: сообщение не найдено", show_alert=True)
            return
        await callback.answer()
        user_backend_id = int(callback.data.split(":")[-1])
        user_detail = await api_client.get_user(user_backend_id)
        current_balance = user_detail.get("balance", 0.0)
        
        username = user_detail.get('username') or f"ID: {user_backend_id}"
        await callback.message.edit_text(
                f"💰 *Пополнение баланса*\n"
                f"━━━━━━━━━━━━━━━━━━━━\n\n"
                f"👤 *Пользователь:* {username}\n"
                f"💵 *Текущий баланс:* `{current_balance:.2f}` грн\n\n"
                f"*Введите сумму для пополнения:*\n"
                f"• Минимум: `0.01` грн\n"
                f"• Максимум: `10000` грн",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="❌ Отмена", callback_data=f"admin:user:{user_backend_id}")]
            ]),
            parse_mode="Markdown"
        )
        
        await state.update_data(admin_balance_user_id=user_backend_id)
        await state.set_state(OrderStates.ADMIN_ADD_BALANCE)
    except Exception as e:
        logger.error(f"Error starting add balance: {e}", exc_info=True)
        try:
            await callback.answer("Ошибка", show_alert=True)
        except Exception:
            pass


@router.callback_query(F.data.startswith("admin:balance:subtract:"))
async def start_subtract_balance(callback: CallbackQuery, state: FSMContext):
    """Начинает процесс списания баланса."""
    try:
        if not callback.message:
            await callback.answer("Ошибка: сообщение не найдено", show_alert=True)
            return
        await callback.answer()
        
        # ИСПРАВЛЕНО: Убрана дублирующая проверка прав - middleware уже проверяет
        
        user_backend_id = int(callback.data.split(":")[-1])
        user_detail = await api_client.get_user(user_backend_id)
        current_balance = user_detail.get("balance", 0.0)
        
        username = user_detail.get('username') or f"ID: {user_backend_id}"
        await callback.message.edit_text(
                f"💸 *Списание баланса*\n"
                f"━━━━━━━━━━━━━━━━━━━━\n\n"
                f"👤 *Пользователь:* {username}\n"
                f"💵 *Текущий баланс:* `{current_balance:.2f}` грн\n\n"
                f"*Введите сумму для списания:*\n"
                f"• Минимум: `0.01` грн\n"
                f"• Максимум: `{current_balance:.2f}` грн",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="❌ Отмена", callback_data=f"admin:user:{user_backend_id}")]
            ]),
            parse_mode="Markdown"
        )
        
        await state.update_data(admin_balance_user_id=user_backend_id)
        await state.set_state(OrderStates.ADMIN_SUBTRACT_BALANCE)
    except Exception as e:
        logger.error(f"Error starting subtract balance: {e}", exc_info=True)
        try:
            await callback.answer("Ошибка", show_alert=True)
        except Exception:
            pass


@router.message(StateFilter(OrderStates.ADMIN_ADD_BALANCE))
async def process_add_balance_input(message: Message, state: FSMContext):
    """Обрабатывает ввод суммы для пополнения баланса."""
    # ИСПРАВЛЕНО: Убрана дублирующая проверка прав - middleware уже проверяет
    
    try:
        amount = _parse_decimal_input(message.text or "")
        if amount <= 0 or amount > 10000:
            await message.answer(
                "❌ Сумма должна быть от 0.01 до 10000 грн.\nПопробуйте ещё раз:",
                reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                    [InlineKeyboardButton(text="❌ Отмена", callback_data="menu:admin")]
                ])
            )
            return
        
        data = await state.get_data()
        user_backend_id = data.get("admin_balance_user_id")
        
        if not user_backend_id:
            await message.answer("Ошибка: пользователь не найден")
            await state.clear()
            return
        
        # ИСПРАВЛЕНО: Логируем критическую операцию перед выполнением
        user_detail = await api_client.get_user(user_backend_id)
        old_balance = user_detail.get("balance", 0.0)
        
        # Пополняем баланс
        result = await api_client.add_user_balance(user_backend_id, amount)
        new_balance = result.get("new_balance", amount)
        
        # ИСПРАВЛЕНО: Логируем критическую операцию
        await log_admin_action(
            admin_telegram_id=message.from_user.id,
            action_type="balance_add",
            target_user_id=user_backend_id,
            details={
                "amount": float(amount),
                "old_balance": float(old_balance),
                "new_balance": float(new_balance),
                "username": user_detail.get("username")
            },
            description=f"Пополнение баланса пользователя {user_detail.get('username') or user_backend_id} на {amount:.2f} грн"
        )
        
        await message.answer(
            f"{Emoji.SUCCESS} *Баланс пополнен*\n\n"
            f"💰 Добавлено: `+{amount:.2f}` грн\n"
            f"💵 Новый баланс: `{new_balance:.2f}` грн",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="👤 К пользователю", callback_data=f"admin:user:{user_backend_id}")],
                [InlineKeyboardButton(text="🏠 Админ-меню", callback_data="menu:admin")]
            ]),
            parse_mode="Markdown"
        )
        
        await state.clear()
        
    except ValueError:
        await message.answer(
            "❌ Введите число от 0.01 до 10000.\nПопробуйте ещё раз:",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="❌ Отмена", callback_data="menu:admin")]
            ])
        )
    except Exception as e:
        logger.error(f"Error adding balance: {e}")
        await message.answer(f"{Emoji.ERROR} Ошибка пополнения баланса: {str(e)[:200]}")
        await state.clear()


@router.message(StateFilter(OrderStates.ADMIN_SUBTRACT_BALANCE))
async def process_subtract_balance_input(message: Message, state: FSMContext):
    """Обрабатывает ввод суммы для списания баланса."""
    # ИСПРАВЛЕНО: Убрана дублирующая проверка прав - middleware уже проверяет
    
    try:
        amount = _parse_decimal_input(message.text or "")
        if amount <= 0:
            await message.answer(
                "❌ Сумма должна быть больше 0.\nПопробуйте ещё раз:",
                reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                    [InlineKeyboardButton(text="❌ Отмена", callback_data="menu:admin")]
                ])
            )
            return
        
        data = await state.get_data()
        user_backend_id = data.get("admin_balance_user_id")
        
        if not user_backend_id:
            await message.answer("Ошибка: пользователь не найден")
            await state.clear()
            return
        
        # ИСПРАВЛЕНО: Логируем критическую операцию перед выполнением
        user_detail = await api_client.get_user(user_backend_id)
        old_balance = user_detail.get("balance", 0.0)
        
        # Списываем баланс
        result = await api_client.subtract_user_balance(user_backend_id, amount)
        new_balance = result.get("new_balance", 0.0)
        
        # ИСПРАВЛЕНО: Логируем критическую операцию
        await log_admin_action(
            admin_telegram_id=message.from_user.id,
            action_type="balance_subtract",
            target_user_id=user_backend_id,
            details={
                "amount": float(amount),
                "old_balance": float(old_balance),
                "new_balance": float(new_balance),
                "username": user_detail.get("username")
            },
            description=f"Списание баланса пользователя {user_detail.get('username') or user_backend_id} на {amount:.2f} грн"
        )
        
        await message.answer(
            f"{Emoji.SUCCESS} *Баланс списан*\n\n"
            f"💸 Списано: `-{amount:.2f}` грн\n"
            f"💵 Новый баланс: `{new_balance:.2f}` грн",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="👤 К пользователю", callback_data=f"admin:user:{user_backend_id}")],
                [InlineKeyboardButton(text="🏠 Админ-меню", callback_data="menu:admin")]
            ]),
            parse_mode="Markdown"
        )
        
        await state.clear()
        
    except ValueError:
        await message.answer(
            "❌ Введите число больше 0.\nПопробуйте ещё раз:",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="❌ Отмена", callback_data="menu:admin")]
            ])
        )
    except Exception as e:
        error_msg = str(e)
        if "Insufficient balance" in error_msg or "Недостаточно средств" in error_msg:
            await message.answer(
                f"{Emoji.ERROR} Недостаточно средств на балансе.\n"
                f"Проверьте текущий баланс пользователя.",
                reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                    [InlineKeyboardButton(text="👤 К пользователю", callback_data=f"admin:user:{user_backend_id}")],
                    [InlineKeyboardButton(text="🏠 Админ-меню", callback_data="menu:admin")]
                ])
            )
        else:
            await message.answer(f"{Emoji.ERROR} Ошибка списания баланса: {error_msg[:200]}")
        await state.clear()


@router.callback_query(F.data.startswith("admin:user:orders:"))
async def show_user_orders(callback: CallbackQuery):
    """Показывает список заказов пользователя с пагинацией."""
    # ИСПРАВЛЕНО: Убрана дублирующая проверка прав - middleware уже проверяет
    
    try:
        if not callback.message:
            await callback.answer("Ошибка: сообщение не найдено", show_alert=True)
            return
        await callback.answer()
        parts = callback.data.split(":")
        user_backend_id = int(parts[3])
        page = int(parts[4])
        
        # Получаем список заказов пользователя
        tasks = await api_client.list_tasks(user_id=user_backend_id)
        
        # Получаем информацию о пользователе для заголовка
        user_detail = await api_client.get_user(user_backend_id)
        username = user_detail.get("username") or f"ID: {user_detail.get('telegram_id')}"
        
        if not tasks:
            await _safe_edit_callback_text(
                callback,
                f"📋 *Заказы пользователя*\n"
                f"━━━━━━━━━━━━━━━━━━━━\n\n"
                f"👤 {username}\n\n"
                f"📭 *Заказов пока нет*",
                reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                    [InlineKeyboardButton(text="◀️ Назад", callback_data=f"admin:user:{user_backend_id}")]
                ]),
                parse_mode="Markdown"
            )
            return
        
        # Пагинация
        ORDERS_PER_PAGE = 10
        total_pages = (len(tasks) + ORDERS_PER_PAGE - 1) // ORDERS_PER_PAGE
        page = max(0, min(page, total_pages - 1))
        
        start_idx = page * ORDERS_PER_PAGE
        end_idx = min(start_idx + ORDERS_PER_PAGE, len(tasks))
        page_tasks = tasks[start_idx:end_idx]
        
        text = f"📋 *Заказы пользователя*\n"
        text += f"━━━━━━━━━━━━━━━━━━━━\n"
        text += f"👤 {username}\n\n"
        text += f"📄 *Страница {page + 1}/{total_pages}*\n\n"
        
        for idx, task in enumerate(page_tasks, start=start_idx + 1):
            task_id = task.get("id")
            status = task.get("status", "unknown")
            video_count = len(task.get("video_urls", []))
            progress = task.get("progress", 0)
            created_at = task.get("created_at", "")
            
            # Форматируем дату
            try:
                from datetime import datetime
                if created_at:
                    dt = datetime.fromisoformat(created_at.replace('Z', '+00:00'))
                    date_str = dt.strftime('%d.%m.%Y %H:%M')
                else:
                    date_str = "N/A"
            except Exception:
                date_str = created_at[:16] if created_at else "N/A"
            
            status_emoji = {
                "pending": "⏳",
                "in_progress": "🔄",
                "completed": Emoji.SUCCESS,
                "done": Emoji.SUCCESS,
                "error": Emoji.ERROR,
                "failed": Emoji.ERROR,
                "partial": Emoji.WARNING
            }.get(status, "❓")
            
            text += f"{status_emoji} `#{task_id}`\n"
            text += f"   📊 Статус: `{status}`\n"
            text += f"   🎬 Видео: `{progress}/{video_count}`\n"
            text += f"   📅 Дата: `{date_str}`\n\n"
        
        # Кнопки пагинации
        keyboard = []
        nav_row = []
        if page > 0:
            nav_row.append(InlineKeyboardButton(text="◀️ Назад", callback_data=f"admin:user:orders:{user_backend_id}:{page-1}"))
        if page < total_pages - 1:
            nav_row.append(InlineKeyboardButton(text="Вперёд ▶️", callback_data=f"admin:user:orders:{user_backend_id}:{page+1}"))
        if nav_row:
            keyboard.append(nav_row)
        
        keyboard.append([InlineKeyboardButton(text="◀️ К пользователю", callback_data=f"admin:user:{user_backend_id}")])
        
        await _safe_edit_callback_text(
            callback,
            text,
            reply_markup=InlineKeyboardMarkup(inline_keyboard=keyboard),
            parse_mode="Markdown"
        )
        
    except Exception as e:
        logger.error(f"Error showing user orders: {e}")
        await callback.answer("Ошибка загрузки заказов", show_alert=True)


@router.callback_query(F.data == "admin:queue")
async def show_queue(callback: CallbackQuery):
    """Показывает очередь задач без лишней загрузки всего списка задач."""
    try:
        if not callback.message:
            await callback.answer("Ошибка: сообщение не найдено", show_alert=True)
            return
        await callback.answer()

        monitoring_result, health_result, pending_result, active_result = await asyncio.gather(
            api_client.get_monitoring_summary(),
            api_client.get_health_summary(),
            api_client.list_tasks(user_id=None, status="pending", limit=8),
            api_client.list_tasks(user_id=None, status="in_progress", limit=8),
            return_exceptions=True,
        )

        monitoring_summary = {} if isinstance(monitoring_result, Exception) else (monitoring_result or {})
        health_summary = {} if isinstance(health_result, Exception) else (health_result or {})
        pending_tasks = [] if isinstance(pending_result, Exception) else (pending_result or [])
        active_tasks = [] if isinstance(active_result, Exception) else (active_result or [])

        if isinstance(monitoring_result, Exception):
            logger.error(f"Error loading monitoring summary for queue: {monitoring_result}", exc_info=True)
        if isinstance(health_result, Exception):
            logger.error(f"Error loading health summary for queue: {health_result}", exc_info=True)
        if isinstance(pending_result, Exception):
            logger.error(f"Error loading pending tasks: {pending_result}", exc_info=True)
        if isinstance(active_result, Exception):
            logger.error(f"Error loading active tasks: {active_result}", exc_info=True)

        tasks = monitoring_summary.get("tasks", {}) or {}
        queue_len = int(((health_summary.get("queue") or {}).get("length")) or 0)

        text = (
            "📋 *Очередь задач*\n"
            "━━━━━━━━━━━━━━━━━━━━\n\n"
            "📊 *Сводка*\n"
            f"Redis queue: `{queue_len}`\n"
            f"Pending: `{int(tasks.get('pending', 0) or 0)}`\n"
            f"In progress: `{int(tasks.get('in_progress', 0) or 0)}`\n"
            f"Stuck: `{int(tasks.get('stuck_in_progress', 0) or 0)}`\n"
            f"Failed 24ч: `{int(tasks.get('failed_today', 0) or 0)}`\n\n"
        )

        if pending_tasks:
            text += "⏳ *Ближайшие pending*\n"
            for task in pending_tasks[:6]:
                task_id = task.get("id")
                user_id = task.get("user_id")
                video_count = len(task.get("video_urls", []) or [])
                created_age = format_compact_age(task.get("created_at")) or "n/a"
                text += f"• #`{task_id}` | user `{user_id}` | urls `{video_count}` | age `{created_age}`\n"
            text += "\n"
        else:
            text += "✅ *Pending-пул пуст*\n\n"

        if active_tasks:
            text += "🔄 *Сейчас в работе*\n"
            for task in active_tasks[:6]:
                task_id = task.get("id")
                user_id = task.get("user_id")
                progress = int(task.get("progress", 0) or 0)
                total = len(task.get("video_urls", []) or [])
                updated_age = format_compact_age(task.get("updated_at")) or "n/a"
                text += f"• #`{task_id}` | user `{user_id}` | `{progress}/{total}` | upd `{updated_age}`\n"
        else:
            text += "✅ *Сейчас нет задач в работе*"

        keyboard = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="🛰 Мониторинг", callback_data="admin:monitoring")],
            [InlineKeyboardButton(text="🔄 Обновить", callback_data="admin:queue")],
            [InlineKeyboardButton(text="◀️ Назад", callback_data="admin:main")],
        ])

        await _safe_edit_callback_text(
            callback,
            text,
            reply_markup=keyboard,
            parse_mode="Markdown",
        )
    except Exception as e:
        logger.error(f"Error showing queue: {e}", exc_info=True)
        try:
            await _safe_edit_callback_text(
                callback,
                "❌ Не удалось загрузить очередь задач",
                reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                    [InlineKeyboardButton(text="◀️ Назад", callback_data="admin:main")]
                ]),
            )
        except Exception:
            pass


@router.callback_query(F.data == "admin:proxy_settings")
async def proxy_settings_menu(callback: CallbackQuery, *, skip_answer: bool = False):
    """Показывает настройки прокси вместе с реальным инвентарём и рисками выбранного режима."""
    try:
        if not skip_answer:
            await callback.answer()

        settings_result, stats_result, proxies_result = await asyncio.gather(
            api_client.get_proxy_settings(),
            api_client.get_proxy_stats(),
            api_client.list_proxies(limit=300, offset=0),
            return_exceptions=True,
        )

        settings = {} if isinstance(settings_result, Exception) else (settings_result or {})
        proxy_stats = {} if isinstance(stats_result, Exception) else (stats_result or {})
        proxies = [] if isinstance(proxies_result, Exception) else (proxies_result or [])

        if isinstance(settings_result, Exception):
            logger.error(f"Error loading proxy settings: {settings_result}", exc_info=True)
        if isinstance(stats_result, Exception):
            logger.error(f"Error loading proxy stats: {stats_result}", exc_info=True)
        if isinstance(proxies_result, Exception):
            logger.error(f"Error loading proxy inventory: {proxies_result}", exc_info=True)

        proxy_type_filter = settings.get("proxy_type_filter")
        proxy_prefer_mobile = bool(settings.get("proxy_prefer_mobile", False))
        filter_text = _format_proxy_mode_label(proxy_type_filter, proxy_prefer_mobile)

        mobile_count = sum(1 for proxy in proxies if str(proxy.get("is_mobile", "N")).upper() == "Y")
        residential_count = max(0, len(proxies) - mobile_count)
        rotation_count = sum(1 for proxy in proxies if proxy.get("rotation_url"))
        geo_count = len({(proxy.get("country_code") or "").upper() for proxy in proxies if proxy.get("country_code")})

        warnings: list[str] = []
        if proxy_type_filter == "mobile" and mobile_count == 0:
            warnings.append("mobile only включён, но мобильных прокси не видно")
        if proxy_type_filter == "residential" and residential_count == 0:
            warnings.append("residential only включён, но резидентских прокси не видно")
        if proxy_prefer_mobile and mobile_count == 0:
            warnings.append("prefer mobile включён, но мобильных прокси сейчас нет")
        if int(proxy_stats.get("bad", 0) or 0) > 0:
            warnings.append(f"bad proxy: {int(proxy_stats.get('bad', 0) or 0)}")

        text = (
            "⚙️ *Прокси-режим*\n"
            "━━━━━━━━━━━━━━━━━━━━\n\n"
            "🎛 *Текущая логика выбора*\n"
            f"Режим: `{filter_text}`\n"
            f"Предпочитать мобильные: `{'да' if proxy_prefer_mobile else 'нет'}`\n\n"
            "🌐 *Инвентарь*\n"
            f"Всего: `{int(proxy_stats.get('total', 0) or 0)}`\n"
            f"Active `{int(proxy_stats.get('active', 0) or 0)}` | slow `{int(proxy_stats.get('slow', 0) or 0)}` | bad `{int(proxy_stats.get('bad', 0) or 0)}` | unknown `{int(proxy_stats.get('unknown', 0) or 0)}`\n"
            f"Mobile `{mobile_count}` | residential `{residential_count}` | rotation `{rotation_count}` | geo `{geo_count}`\n\n"
            "💡 *Что это меняет*\n"
            "• mobile only — жёстко режет пул до мобильных прокси\n"
            "• residential only — исключает mobile-пул\n"
            "• prefer mobile — сначала берём mobile, но не блокируем весь пул\n"
            "• validate/list proxies — лучший быстрый путь перед массовой работой"
        )

        if warnings:
            text += "\n\n🚨 *Риски*\n"
            text += "\n".join(f"• {warning}" for warning in warnings[:4])

        keyboard = InlineKeyboardMarkup(inline_keyboard=[
            [
                InlineKeyboardButton(
                    text=f"{'✅ ' if proxy_type_filter is None else ''}Все прокси",
                    callback_data="admin:proxy_settings:type:none",
                )
            ],
            [
                InlineKeyboardButton(
                    text=f"{'✅ ' if proxy_type_filter == 'mobile' else ''}Только мобильные",
                    callback_data="admin:proxy_settings:type:mobile",
                )
            ],
            [
                InlineKeyboardButton(
                    text=f"{'✅ ' if proxy_type_filter == 'residential' else ''}Только резидентские",
                    callback_data="admin:proxy_settings:type:residential",
                )
            ],
            [
                InlineKeyboardButton(
                    text=f"{'✅' if proxy_prefer_mobile else '❌'} Предпочитать мобильные",
                    callback_data="admin:proxy_settings:prefer_mobile:toggle",
                )
            ],
            [
                InlineKeyboardButton(text="🌐 Прокси", callback_data="admin:proxies"),
                InlineKeyboardButton(text="🔄 Обновить", callback_data="admin:proxy_settings"),
            ],
            [InlineKeyboardButton(text="◀️ Назад", callback_data="admin:main")],
        ])

        await _safe_edit_callback_text(
            callback,
            text,
            reply_markup=keyboard,
            parse_mode="Markdown",
        )
    except Exception as e:
        logger.error(f"Error showing proxy settings: {e}", exc_info=True)
        await callback.answer("Ошибка загрузки настроек", show_alert=True)


@router.callback_query(F.data.startswith("admin:proxy_settings:type:"))
async def set_proxy_type_filter(callback: CallbackQuery):
    """Устанавливает фильтр типа прокси."""
    try:
        # Парсим тип из callback_data
        proxy_type = callback.data.split(":")[-1]
        if proxy_type == "none":
            proxy_type = None
        
        # Обновляем настройки
        await api_client.update_proxy_settings(proxy_type_filter=proxy_type)
        
        # Обновляем меню
        await proxy_settings_menu(callback, skip_answer=True)
        
        await callback.answer("✅ Настройка обновлена", show_alert=False)
    except Exception as e:
        logger.error(f"Error setting proxy type filter: {e}", exc_info=True)
        await callback.answer("Ошибка обновления настройки", show_alert=True)


@router.callback_query(F.data == "admin:proxy_settings:prefer_mobile:toggle")
async def toggle_proxy_prefer_mobile(callback: CallbackQuery):
    """Переключает предпочтение мобильных прокси."""
    try:
        # Получаем текущие настройки
        settings = await api_client.get_proxy_settings()
        current_value = settings.get("proxy_prefer_mobile", False)
        
        # Переключаем
        new_value = not current_value
        
        # Обновляем настройки
        await api_client.update_proxy_settings(proxy_prefer_mobile=new_value)
        
        # Обновляем меню
        await proxy_settings_menu(callback, skip_answer=True)
        
        await callback.answer("✅ Настройка обновлена", show_alert=False)
    except Exception as e:
        logger.error(f"Error toggling proxy prefer mobile: {e}", exc_info=True)
        await callback.answer("Ошибка обновления настройки", show_alert=True)


def _build_software_mode_text(mode: str) -> tuple[str, InlineKeyboardMarkup]:
    """Returns (text, keyboard) for the software mode panel."""
    is_full = mode == "full"
    is_parsing = mode == "parsing"

    active_label = "🟢 Полный" if is_full else "🟡 Парсинг"

    text = (
        "🎛 *Режим работы софта*\n"
        "━━━━━━━━━━━━━━━━━━━━\n\n"
        f"Активный режим: *{active_label}*\n\n"
        "*🟢 Полный*\n"
        "Доступен весь функционал: заказы, шаблоны, комментинг, мониторинг видео.\n\n"
        "*🟡 Парсинг*\n"
        "В меню остаётся только мониторинг видео.\n"
        "Режим бесплатный для пользователей: баланс и цены в пользовательском меню скрыты.\n"
        "Комментинг и заказы скрыты — бот работает в режиме наблюдателя.\n\n"
        "_Переключение применяется немедленно — новые сессии пользователей увидят изменение._"
    )
    full_btn = f"{'✅ ' if is_full else ''}🟢 Полный"
    parsing_btn = f"{'✅ ' if is_parsing else ''}🟡 Парсинг"
    keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text=full_btn, callback_data="admin:software_mode:set:full"),
            InlineKeyboardButton(text=parsing_btn, callback_data="admin:software_mode:set:parsing"),
        ],
        [InlineKeyboardButton(text="◀️ Назад", callback_data="admin:main")],
    ])
    return text, keyboard


@router.callback_query(F.data == "admin:software_mode")
async def software_mode_menu(callback: CallbackQuery):
    """Shows the global software mode switch."""
    try:
        await callback.answer()
        settings_data = await api_client.get_software_mode_settings()
        mode = settings_data.get("software_mode", "full")
        text, keyboard = _build_software_mode_text(mode)
        await _safe_edit_callback_text(callback, text, reply_markup=keyboard, parse_mode="Markdown")
    except Exception as e:
        logger.error(f"Error showing software mode menu: {e}", exc_info=True)
        try:
            await callback.answer("Ошибка загрузки режима", show_alert=True)
        except Exception:
            pass


@router.callback_query(F.data.startswith("admin:software_mode:set:"))
async def set_software_mode_handler(callback: CallbackQuery):
    """Updates the global software mode."""
    mode = callback.data.split(":")[-1]
    if mode not in {"full", "parsing"}:
        await callback.answer("Неверный режим", show_alert=True)
        return

    # Answer immediately — API calls below may take a moment
    mode_label = "🟢 Полный" if mode == "full" else "🟡 Парсинг"
    try:
        await callback.answer(f"Режим: {mode_label}", show_alert=False)
    except Exception:
        pass

    try:
        old_data = await api_client.get_software_mode_settings()
        old_mode = old_data.get("software_mode", "full")

        if old_mode == mode:
            # Already set — just refresh the panel, no API write needed
            text, keyboard = _build_software_mode_text(mode)
            await _safe_edit_callback_text(callback, text, reply_markup=keyboard, parse_mode="Markdown")
            return

        await api_client.update_software_mode(mode)
        clear_software_mode_cache()
        await log_admin_action(
            admin_telegram_id=callback.from_user.id,
            action_type="software_mode_update",
            target_user_id=None,
            details={"old_mode": old_mode, "new_mode": mode},
        )
        text, keyboard = _build_software_mode_text(mode)
        await _safe_edit_callback_text(callback, text, reply_markup=keyboard, parse_mode="Markdown")
    except Exception as e:
        logger.error(f"Error updating software mode: {e}", exc_info=True)
        try:
            await callback.answer("Ошибка обновления режима", show_alert=True)
        except Exception:
            pass


def _build_commenting_mode_text(mode: str) -> tuple[str, InlineKeyboardMarkup]:
    """Returns (text, keyboard) for the commenting mode panel."""
    is_old = mode == "old"
    is_parallel = mode == "parallel"
    is_parallel_6x1 = mode == "parallel_6x1"
    active_label = {
        "parallel": "🟢 Новая параллельная",
        "parallel_6x1": "🟣 Параллельная 6×1",
    }.get(mode, "⚪ Старая")

    text = (
        "💬 *Режим комментинга*\n"
        "━━━━━━━━━━━━━━━━━━━━\n\n"
        f"Активный режим: *{active_label}*\n\n"
        "*⚪ Старая логика*\n"
        "Текущий рабочий user-schema сценарий без изменения поведения.\n\n"
        "*🟢 Новая параллельная*\n"
        "Главные комментарии публикуются максимум тремя аккаунтами одновременно. "
        "Ответы идут батчами до трёх аккаунтов, без публикации fallback-главных "
        "со стороны ответчиков.\n\n"
        "*🟣 Параллельная 6×1*\n"
        "Две строгие волны по три аккаунта. Каждый аккаунт публикует только один "
        "из трёх вариантов. Между волнами обязательно меняется IP мобильного "
        "прокси; ответы запускаются только после успешных 6/6 главных комментариев.\n\n"
        "_Переключение применяется к новым заказам._"
    )
    old_btn = f"{'✅ ' if is_old else ''}⚪ Старая"
    parallel_btn = f"{'✅ ' if is_parallel else ''}🟢 Новая"
    parallel_6x1_btn = f"{'✅ ' if is_parallel_6x1 else ''}🟣 6×1"
    keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text=old_btn, callback_data="admin:commenting_mode:set:old"),
            InlineKeyboardButton(text=parallel_btn, callback_data="admin:commenting_mode:set:parallel"),
        ],
        [
            InlineKeyboardButton(
                text=parallel_6x1_btn,
                callback_data="admin:commenting_mode:set:parallel_6x1",
            ),
        ],
        [InlineKeyboardButton(text="◀️ Назад", callback_data="admin:main")],
    ])
    return text, keyboard


@router.callback_query(F.data == "admin:commenting_mode")
async def commenting_mode_menu(callback: CallbackQuery):
    """Shows the commenting workflow mode switch."""
    try:
        await callback.answer()
        settings_data = await api_client.get_commenting_mode_settings()
        mode = settings_data.get("commenting_mode", "old")
        text, keyboard = _build_commenting_mode_text(mode)
        await _safe_edit_callback_text(callback, text, reply_markup=keyboard, parse_mode="Markdown")
    except Exception as e:
        logger.error(f"Error showing commenting mode menu: {e}", exc_info=True)
        try:
            await callback.answer("Ошибка загрузки режима комментинга", show_alert=True)
        except Exception:
            pass


@router.callback_query(F.data.startswith("admin:commenting_mode:set:"))
async def set_commenting_mode_handler(callback: CallbackQuery):
    """Updates the commenting workflow mode."""
    mode = callback.data.split(":")[-1]
    if mode not in {"old", "parallel", "parallel_6x1"}:
        await callback.answer("Неверный режим", show_alert=True)
        return

    mode_label = {
        "parallel": "🟢 Новая",
        "parallel_6x1": "🟣 6×1",
    }.get(mode, "⚪ Старая")
    try:
        await callback.answer(f"Комментинг: {mode_label}", show_alert=False)
    except Exception:
        pass

    try:
        old_data = await api_client.get_commenting_mode_settings()
        old_mode = old_data.get("commenting_mode", "old")

        if old_mode == mode:
            text, keyboard = _build_commenting_mode_text(mode)
            await _safe_edit_callback_text(callback, text, reply_markup=keyboard, parse_mode="Markdown")
            return

        await api_client.update_commenting_mode(mode)
        await log_admin_action(
            admin_telegram_id=callback.from_user.id,
            action_type="commenting_mode_update",
            target_user_id=None,
            details={"old_mode": old_mode, "new_mode": mode},
        )
        text, keyboard = _build_commenting_mode_text(mode)
        await _safe_edit_callback_text(callback, text, reply_markup=keyboard, parse_mode="Markdown")
    except Exception as e:
        logger.error(f"Error updating commenting mode: {e}", exc_info=True)
        try:
            await callback.answer("Ошибка обновления режима комментинга", show_alert=True)
        except Exception:
            pass


def _build_order_strategy_text(strategy: str, available: list[str] | None = None) -> tuple[str, InlineKeyboardMarkup]:
    available_set = set(available or ["balanced", "fast", "careful", "verify"])
    ordered = [item for item in ("balanced", "fast", "careful", "verify") if item in available_set]
    labels = {
        "balanced": "Balanced",
        "fast": "Fast",
        "careful": "Careful",
        "verify": "Verify",
    }
    descriptions = {
        "balanced": "Current production behavior. Safest default.",
        "fast": "For future lower-latency policies.",
        "careful": "For future deeper retries and slower confirmation.",
        "verify": "For future stronger public-confirmation policies.",
    }
    active = strategy if strategy in available_set else "balanced"
    lines = [
        "*Order execution strategy*",
        "",
        f"Active: `{active}`",
        "",
    ]
    for item in ordered:
        marker = "✓" if item == active else "•"
        lines.append(f"{marker} *{labels.get(item, item)}* — {descriptions.get(item, '')}")
    lines.append("")
    lines.append("_Balanced keeps current commenting behavior. Other modes are stored on new orders for controlled rollout._")
    keyboard_rows = [
        [InlineKeyboardButton(text=f"{'✓ ' if item == active else ''}{labels.get(item, item)}", callback_data=f"admin:order_strategy:set:{item}")]
        for item in ordered
    ]
    keyboard_rows.append([InlineKeyboardButton(text="Back", callback_data="admin:main")])
    return "\n".join(lines), InlineKeyboardMarkup(inline_keyboard=keyboard_rows)


@router.callback_query(F.data == "admin:order_strategy")
async def order_strategy_menu(callback: CallbackQuery):
    try:
        await callback.answer()
        settings_data = await api_client.get_order_execution_strategy_settings()
        strategy = str(settings_data.get("order_execution_strategy") or "balanced")
        available = settings_data.get("available_strategies")
        text, keyboard = _build_order_strategy_text(strategy, available if isinstance(available, list) else None)
        await _safe_edit_callback_text(callback, text, reply_markup=keyboard, parse_mode="Markdown")
    except Exception as e:
        logger.error(f"Error showing order strategy menu: {e}", exc_info=True)
        try:
            await callback.answer("Order strategy load error", show_alert=True)
        except Exception:
            pass


@router.callback_query(F.data.startswith("admin:order_strategy:set:"))
async def set_order_strategy_handler(callback: CallbackQuery):
    strategy = callback.data.split(":")[-1]
    if strategy not in {"balanced", "fast", "careful", "verify"}:
        await callback.answer("Invalid strategy", show_alert=True)
        return

    try:
        await callback.answer(f"Strategy: {strategy}", show_alert=False)
    except Exception:
        pass

    try:
        old_data = await api_client.get_order_execution_strategy_settings()
        old_strategy = str(old_data.get("order_execution_strategy") or "balanced")
        if old_strategy != strategy:
            await api_client.update_order_execution_strategy(strategy)
            await log_admin_action(
                admin_telegram_id=callback.from_user.id,
                action_type="order_execution_strategy_update",
                target_user_id=None,
                details={"old_strategy": old_strategy, "new_strategy": strategy},
            )
        text, keyboard = _build_order_strategy_text(strategy, old_data.get("available_strategies"))
        await _safe_edit_callback_text(callback, text, reply_markup=keyboard, parse_mode="Markdown")
    except Exception as e:
        logger.error(f"Error updating order strategy: {e}", exc_info=True)
        try:
            await callback.answer("Order strategy update error", show_alert=True)
        except Exception:
            pass


@router.callback_query(F.data == "admin:boost_likes")
async def boost_likes_menu(callback: CallbackQuery, state: FSMContext):
    """Показывает меню управления лайками на комментарии."""
    try:
        await callback.answer()
        await state.clear()

        settings_data = await api_client.get_boost_likes_settings()
        likes_enabled = bool(settings_data.get("boost_likes_enabled", True))
        provider_available = bool(settings_data.get("boost_provider_available", False))

        likes_quantity = coerce_likes_quantity(
            settings_data.get("boost_likes_quantity"),
            BOOST_LIKES_DEFAULT,
        )
        target_mode = coerce_likes_target_mode(
            settings_data.get("boost_likes_target_mode"),
            BOOST_LIKES_TARGET_SINGLE_TOP,
        )

        provider_text = "✅ Подключен" if provider_available else "⚠️ API ключ не настроен"
        enabled_text = "Включено" if likes_enabled else "Выключено"

        text = "❤️ *Лайки на комментарии*\n"
        text += "━━━━━━━━━━━━━━━━━━━━\n\n"
        text += f"• *Статус:* `{enabled_text}`\n"
        text += f"• *Лайков на Boost-заказ:* `{likes_quantity}`\n"
        text += f"• *Parallel-цель:* `{_format_likes_target_mode(target_mode)}`\n"
        text += f"• *API лайков:* `{provider_text}`\n\n"
        text += "💡 *Как это работает:*\n"
        text += "• При включении после комментария/ответов запускается заказ лайков.\n"
        text += "• Количество применяется к новым заказам.\n"
        text += "• Режим цели применяется только к новому parallel-комментингу.\n"
        text += "• `Один лучший` выбирает автора с максимумом ответов; `каждый найденный` создаёт до 3 Boost-заказов.\n"
        text += "• При выключении лайки полностью пропускаются.\n"

        keyboard = InlineKeyboardMarkup(inline_keyboard=[
            [
                InlineKeyboardButton(
                    text=f"{Emoji.SUCCESS if likes_enabled else Emoji.ERROR} Лайки {'включены' if likes_enabled else 'выключены'}",
                    callback_data="admin:boost_likes:toggle",
                )
            ],
            [
                InlineKeyboardButton(
                    text=f"🔢 Количество: {likes_quantity}",
                    callback_data="admin:boost_likes:edit_quantity",
                )
            ],
            [
                InlineKeyboardButton(
                    text=f"🎯 Цель: {_format_likes_target_mode(target_mode)}",
                    callback_data="admin:boost_likes:toggle_target_mode",
                )
            ],
            [
                InlineKeyboardButton(text="◀️ Назад", callback_data="admin:main")
            ],
        ])

        await _safe_edit_callback_text(
            callback,
            text,
            reply_markup=keyboard,
            parse_mode="Markdown",
        )
    except Exception as e:
        logger.error(f"Error showing boost likes settings: {e}", exc_info=True)
        await callback.answer("Ошибка загрузки настроек лайков", show_alert=True)


@router.callback_query(F.data == "admin:boost_likes:toggle")
async def toggle_boost_likes(callback: CallbackQuery, state: FSMContext):
    """Включает/выключает лайки на комментарии."""
    try:
        await callback.answer()

        settings_data = await api_client.get_boost_likes_settings()
        current_value = bool(settings_data.get("boost_likes_enabled", True))
        new_value = not current_value

        await api_client.update_boost_likes_settings(boost_likes_enabled=new_value)
        await boost_likes_menu(callback, state)
        await callback.answer("✅ Настройка обновлена", show_alert=False)
    except Exception as e:
        logger.error(f"Error toggling boost likes: {e}", exc_info=True)
        await callback.answer("Ошибка обновления настройки", show_alert=True)


@router.callback_query(F.data == "admin:boost_likes:toggle_target_mode")
async def toggle_boost_likes_target_mode(callback: CallbackQuery, state: FSMContext):
    """Переключает режим выбора комментариев для Boost-лайков в parallel-комментинге."""
    try:
        await callback.answer()

        settings_data = await api_client.get_boost_likes_settings()
        current_mode = coerce_likes_target_mode(
            settings_data.get("boost_likes_target_mode"),
            BOOST_LIKES_TARGET_SINGLE_TOP,
        )
        new_mode = (
            BOOST_LIKES_TARGET_EACH_FOUND
            if current_mode == BOOST_LIKES_TARGET_SINGLE_TOP
            else BOOST_LIKES_TARGET_SINGLE_TOP
        )

        await api_client.update_boost_likes_settings(boost_likes_target_mode=new_mode)
        await log_admin_action(
            admin_telegram_id=callback.from_user.id,
            action_type="boost_likes_target_mode_update",
            target_user_id=None,
            details={"old_mode": current_mode, "new_mode": new_mode},
        )
        await boost_likes_menu(callback, state)
        await callback.answer("✅ Режим цели обновлён", show_alert=False)
    except Exception as e:
        logger.error(f"Error toggling boost likes target mode: {e}", exc_info=True)
        await callback.answer("Ошибка обновления режима цели", show_alert=True)


@router.callback_query(F.data == "admin:boost_likes:edit_quantity")
async def start_edit_boost_likes_quantity(callback: CallbackQuery, state: FSMContext):
    """Запрашивает новое количество лайков на заказ."""
    try:
        if not callback.message:
            await callback.answer("Ошибка: сообщение не найдено", show_alert=True)
            return

        settings_data = await api_client.get_boost_likes_settings()
        current_quantity = coerce_likes_quantity(
            settings_data.get("boost_likes_quantity"),
            BOOST_LIKES_DEFAULT,
        )

        await callback.message.edit_text(
            "🔢 *Изменение количества лайков*\n"
            "━━━━━━━━━━━━━━━━━━━━\n\n"
            f"Текущее значение: `{current_quantity}`\n\n"
            "Введите новое количество лайков на один заказ:\n"
            f"• Минимум: `{BOOST_LIKES_MIN}`\n"
            f"• Максимум: `{BOOST_LIKES_MAX}`\n\n"
            "Пример: `150`",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="❌ Отмена", callback_data="admin:boost_likes")]
            ]),
            parse_mode="Markdown",
        )

        await state.update_data(admin_boost_likes_old_quantity=current_quantity)
        await state.set_state(OrderStates.ADMIN_EDIT_BOOST_LIKES)
        await callback.answer()
    except Exception as e:
        logger.error(f"Error starting boost likes quantity edit: {e}", exc_info=True)
        await callback.answer("Ошибка", show_alert=True)


@router.message(StateFilter(OrderStates.ADMIN_EDIT_BOOST_LIKES))
async def process_boost_likes_quantity_input(message: Message, state: FSMContext):
    """Обрабатывает ввод нового количества лайков."""
    try:
        quantity = int((message.text or "").strip())
        if quantity < BOOST_LIKES_MIN or quantity > BOOST_LIKES_MAX:
            await message.answer(
                f"❌ Количество должно быть от {_format_likes_range()}.\nПопробуйте ещё раз:",
                reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                    [InlineKeyboardButton(text="❌ Отмена", callback_data="admin:boost_likes")]
                ]),
            )
            return

        result = await api_client.update_boost_likes_settings(boost_likes_quantity=quantity)
        updated_enabled = bool(result.get("boost_likes_enabled", True))
        provider_available = bool(result.get("boost_provider_available", False))

        old_quantity = (await state.get_data()).get("admin_boost_likes_old_quantity")

        await log_admin_action(
            admin_telegram_id=message.from_user.id,
            action_type="boost_likes_update",
            target_user_id=None,
            details={
                "old_quantity": old_quantity,
                "new_quantity": quantity,
                "enabled": updated_enabled,
                "provider_available": provider_available,
            },
            description=f"Изменение количества лайков: {old_quantity} -> {quantity}",
        )

        await message.answer(
            f"{Emoji.SUCCESS} *Настройка сохранена*\n\n"
            f"❤️ Лайки: `{'включены' if updated_enabled else 'выключены'}`\n"
            f"🔢 Количество: `{quantity}`\n"
            f"🔌 API лайков: `{'подключен' if provider_available else 'не настроен'}`",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="❤️ К настройкам лайков", callback_data="admin:boost_likes")],
                [InlineKeyboardButton(text="🏠 Админ-меню", callback_data="menu:admin")],
            ]),
            parse_mode="Markdown",
        )
        await state.clear()
    except ValueError:
        await message.answer(
            f"❌ Введите целое число от {_format_likes_range()}.\nПопробуйте ещё раз:",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="❌ Отмена", callback_data="admin:boost_likes")]
            ]),
        )
    except Exception as e:
        logger.error(f"Error updating boost likes quantity: {e}", exc_info=True)
        await message.answer(f"{Emoji.ERROR} Ошибка обновления настройки: {str(e)[:200]}")
        await state.clear()


@router.callback_query(F.data == "admin:pricing")
async def pricing_menu(callback: CallbackQuery):
    """Показывает цены с экономическим смыслом: success, failure и refund delta."""
    try:
        await callback.answer()

        pricing = await api_client.get_pricing()
        price_success = _coerce_float(pricing.get("price_success"), 5.0)
        price_failure = _coerce_float(pricing.get("price_failure"), 2.0)
        refund_delta = price_success - price_failure
        risky = price_failure >= price_success > 0

        text = (
            "💰 *Цены*\n"
            "━━━━━━━━━━━━━━━━━━━━\n\n"
            f"✅ Успешный заказ: `{price_success:.2f}` грн\n"
            f"❌ Неуспешный заказ: `{price_failure:.2f}` грн\n"
            f"🔁 Возврат при fail: `{refund_delta:.2f}` грн\n\n"
            "💡 *Как это работает*\n"
            "• Success — сколько удерживаем за полностью успешный заказ\n"
            "• Failure — сколько удерживаем, если заказ не выполнен\n"
            "• Refund — сколько вернётся клиенту при fail\n"
        )

        if risky:
            text += "\n🚨 *Риск*\n• failure >= success — финансовая модель выглядит опасно\n"
        elif refund_delta <= 0:
            text += "\n⚠️ *Внимание*\n• refund <= 0 — при fail клиент почти ничего не получит назад\n"

        text += "\nВыберите, что изменить:"

        keyboard = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="✅ Изменить успешный", callback_data="admin:pricing:edit:success")],
            [InlineKeyboardButton(text="❌ Изменить неуспешный", callback_data="admin:pricing:edit:failure")],
            [InlineKeyboardButton(text="◀️ Назад", callback_data="admin:main")],
        ])

        await _safe_edit_callback_text(
            callback,
            text,
            reply_markup=keyboard,
            parse_mode="Markdown",
        )
    except Exception as e:
        logger.error(f"Error showing pricing menu: {e}", exc_info=True)
        await callback.answer("Ошибка загрузки цен", show_alert=True)


@router.callback_query(F.data.startswith("admin:pricing:edit:"))
async def start_edit_pricing(callback: CallbackQuery, state: FSMContext):
    """Начинает редактирование цены."""
    try:
        if not callback.message:
            await callback.answer("Ошибка: сообщение не найдено", show_alert=True)
            return
        
        price_type = callback.data.split(":")[-1]  # "success" или "failure"
        
        # Получаем текущие цены
        pricing = await api_client.get_pricing()
        # ИСПРАВЛЕНО: Преобразуем в float, обрабатываем Decimal, строки и числа
        current_price_raw = pricing.get(f"price_{price_type}", 5.0 if price_type == "success" else 2.0)
        try:
            current_price = float(current_price_raw) if not isinstance(current_price_raw, float) else current_price_raw
        except (ValueError, TypeError):
            current_price = 5.0 if price_type == "success" else 2.0
        
        price_name = "успешный заказ" if price_type == "success" else "неуспешный заказ"
        
        await callback.message.edit_text(
            f"💰 *Изменение цены*\n"
            f"━━━━━━━━━━━━━━━━━━━━\n\n"
            f"📝 *Тип:* {price_name}\n"
            f"💵 *Текущая цена:* `{current_price:.2f}` грн\n\n"
            f"*Введите новую цену:*\n"
            f"• Минимум: `0.01` грн\n"
            f"• Максимум: `10000` грн\n\n"
            f"*Пример:* `7.00` для 7 грн",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="❌ Отмена", callback_data="admin:pricing")]
            ]),
            parse_mode="Markdown"
        )
        
        await state.update_data(
            admin_pricing_type=price_type,
            old_price=float(current_price)
        )
        await state.set_state(OrderStates.ADMIN_EDIT_PRICING)
        await callback.answer()
    except Exception as e:
        logger.error(f"Error starting edit pricing: {e}", exc_info=True)
        await callback.answer("Ошибка", show_alert=True)


@router.message(StateFilter(OrderStates.ADMIN_EDIT_PRICING))
async def process_pricing_input(message: Message, state: FSMContext):
    """Обрабатывает ввод новой цены."""
    try:
        amount = _parse_decimal_input(message.text or "")
        if amount <= 0 or amount > 10000:
            await message.answer(
                "❌ Цена должна быть от 0.01 до 10000 грн.\nПопробуйте ещё раз:",
                reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                    [InlineKeyboardButton(text="❌ Отмена", callback_data="admin:pricing")]
                ])
            )
            return
        
        data = await state.get_data()
        price_type = data.get("admin_pricing_type")
        
        if not price_type:
            await message.answer("Ошибка: тип цены не найден")
            await state.clear()
            return
        
        # Обновляем цену
        if price_type == "success":
            result = await api_client.update_pricing(price_success=amount)
        else:
            result = await api_client.update_pricing(price_failure=amount)
        
        new_price_success = result.get("price_success", 5.0)
        new_price_failure = result.get("price_failure", 2.0)
        
        price_name = "успешный заказ" if price_type == "success" else "неуспешный заказ"
        
        # Логируем изменение
        await log_admin_action(
            admin_telegram_id=message.from_user.id,
            action_type="pricing_update",
            target_user_id=None,
            details={
                "price_type": price_type,
                "old_price": data.get("old_price"),
                "new_price": float(amount)
            },
            description=f"Изменение цены {price_name} на {amount:.2f} грн"
        )
        
        await message.answer(
            f"{Emoji.SUCCESS} *Цена обновлена*\n\n"
            f"💰 {price_name.capitalize()}: `{amount:.2f}` грн\n\n"
            f"📊 *Текущие цены:*\n"
            f"✅ Успешный: `{new_price_success:.2f}` грн\n"
            f"❌ Неуспешный: `{new_price_failure:.2f}` грн",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="💰 К ценам", callback_data="admin:pricing")],
                [InlineKeyboardButton(text="🏠 Админ-меню", callback_data="menu:admin")]
            ]),
            parse_mode="Markdown"
        )
        
        await state.clear()
        
    except ValueError:
        await message.answer(
            "❌ Введите число от 0.01 до 10000.\nПопробуйте ещё раз:",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="❌ Отмена", callback_data="admin:pricing")]
            ])
        )
    except Exception as e:
        logger.error(f"Error updating pricing: {e}")
        await message.answer(f"{Emoji.ERROR} Ошибка обновления цены: {str(e)[:200]}")
        await state.clear()


# ═══════════════════════════════════════════════════════════════════════════════
# ADAPTIVE MONITORING ADMIN PANEL
# ═══════════════════════════════════════════════════════════════════════════════

BLOGGERS_PAGE_SIZE = 10
ADAPTIVE_BLOGGER_SEARCH_PER_PAGE = 10
ADAPTIVE_BLOGGERS_FETCH_LIMIT = 1000
ADAPTIVE_BLOGGERS_FETCH_MAX = 10000

_ADAPTIVE_SETTING_META: dict[str, dict] = {
    # ── Limits ──
    "min_interval": {
        "label": "Минимальный интервал проверки",
        "api_key": "min_interval_minutes", "type": "int", "min": 1, "max": 1440, "unit": "мин", "cat": "limits",
        "desc": (
            "Как можно чаще проверять любого блогера — даже VIP.\n"
            "Меньше → нагрузка выше, видео замечаем быстрее.\n"
            "Больше → нагрузка меньше, но реакция медленнее.\n"
            "Пример: `5` = проверяем не чаще раз в 5 минут."
        ),
    },
    "max_interval": {
        "label": "Максимальный интервал проверки",
        "api_key": "max_interval_minutes", "type": "int", "min": 1, "max": 1440, "unit": "мин", "cat": "limits",
        "desc": (
            "Как можно реже проверять «тихих» блогеров.\n"
            "Больше → тихари занимают меньше ресурсов, но дольше ждём их видео.\n"
            "Пример: `240` = раз в 4 часа максимум."
        ),
    },
    "sanity_cap_factor": {
        "label": "Максимально допустимое превышение интервала",
        "api_key": "sanity_cap_factor", "type": "float", "min": 1.0, "max": 10.0, "unit": "×", "cat": "limits",
        "desc": (
            "Ограничитель: алгоритм не может поставить интервал больше чем\n"
            "макс\\_интервал × этот коэффициент.\n"
            "Защита от «бот забыл про блогера на 2 дня».\n"
            "Пример: макс 240 мин × 2.0 = не более 8 часов в любом случае."
        ),
    },
    # ── Acceleration ──
    "active_phase_interval": {
        "label": "Интервал сразу после нового видео",
        "api_key": "active_phase_interval_minutes", "type": "int", "min": 1, "max": 1440, "unit": "мин", "cat": "acceleration",
        "desc": (
            "Как только блогер выложил видео, бот переходит в активную фазу\n"
            "и проверяет с этим интервалом — ловим следующее видео быстро.\n"
            "Меньше → быстрее ловим серии. Больше → меньше нагрузка.\n"
            "Пример: `20` = проверяем каждые 20 мин после свежего поста."
        ),
    },
    "active_phase_hours": {
        "label": "Длительность активной фазы",
        "api_key": "active_phase_hours", "type": "float", "min": 0.1, "max": 72.0, "unit": "ч", "cat": "acceleration",
        "desc": (
            "Сколько часов держать учащённые проверки после нового видео.\n"
            "Больше → дольше «дежурим» у активного блогера.\n"
            "Пример: `2.0` = 2 часа после поста проверяем часто, потом — по обычному ритму."
        ),
    },
    "found_new_reset_factor": {
        "label": "Ускорение при находке нового видео",
        "api_key": "found_new_reset_factor", "type": "float", "min": 0.05, "max": 1.0, "unit": "×", "cat": "hidden", "hidden": True,
        "desc": (
            "Когда нашли новое видео, следующий интервал = текущий × этот коэффициент.\n"
            "Меньше → сильнее ускоряем. `1.0` = не ускоряем вовсе.\n"
            "Пример: `0.25` = если интервал был 60 мин, после находки → 15 мин."
        ),
    },
    "burst_threshold_24h": {
        "label": "Сколько видео за сутки считается серией",
        "api_key": "burst_threshold_24h", "type": "int", "min": 1, "max": 50, "unit": "видео", "cat": "acceleration",
        "desc": (
            "Если за последние 24 часа блогер выложил столько видео или больше —\n"
            "бот включает burst-режим (проверяет заметно чаще).\n"
            "Меньше → burst включается легче. Больше → только при реально плотных сериях.\n"
            "Пример: `3` = 3 видео за день = burst."
        ),
    },
    "burst_multiplier": {
        "label": "Насколько чаще проверять во время серии",
        "api_key": "burst_multiplier", "type": "float", "min": 0.1, "max": 1.0, "unit": "×", "cat": "acceleration",
        "desc": (
            "В burst-режиме интервал = рассчитанный × этот коэффициент.\n"
            "Меньше → сильнее ускоряем. `1.0` = без ускорения.\n"
            "Пример: `0.5` = вдвое чаще обычного."
        ),
    },
    "burst_duration_hours": {
        "label": "Сколько часов ждать продолжения серии",
        "api_key": "burst_duration_hours", "type": "float", "min": 1.0, "max": 168.0, "unit": "ч", "cat": "acceleration",
        "desc": (
            "Burst-режим отключается, если новых видео нет дольше этого времени.\n"
            "Больше → дольше ждём продолжения серии. Меньше → быстро остываем.\n"
            "Пример: `24.0` = burst держится сутки без новых постов."
        ),
    },
    "use_p25_for_burst": {
        "label": "В серии ориентироваться на быстрый темп",
        "api_key": "use_p25_for_burst", "type": "bool", "unit": "", "cat": "acceleration",
        "desc": (
            "Включено = в burst-режиме бот ориентируется на самые короткие промежутки\n"
            "между постами (нижние 25%), а не на медианный интервал.\n"
            "Точнее ловит быстрые серии.\n"
            "Введите `да` или `нет`."
        ),
    },
    "burst_relative_factor": {
        "label": "Серия относительно обычного ритма",
        "api_key": "burst_relative_factor", "type": "float", "min": 0.5, "max": 10.0, "unit": "×", "cat": "acceleration",
        "desc": (
            "Burst включается если постов за 24ч >= ожидаемых в сутки × этот коэффициент.\n"
            "Блогер с ритмом 1 пост/сутки: burst при 1.5 = нужно ≥1.5 поста/24ч.\n"
            "Блогер с ритмом 4 поста/сутки: burst при 1.5 = нужно ≥6 постов/24ч.\n"
            "Меньше → burst включается легче. Больше → только при аномальных всплесках.\n"
            "Работает вместе с «порогом burst (видео)» — берётся максимум из двух."
        ),
    },
    "prediction_min_confidence": {
        "label": "Насколько верить прогнозу поста",
        "api_key": "prediction_min_confidence", "type": "float", "min": 0.0, "max": 1.0, "unit": "", "cat": "acceleration",
        "desc": (
            "Если уверенность прогноза следующего поста >= этого числа,\n"
            "бот сдвигает следующую проверку ближе к предсказанному времени.\n"
            "`0.0` = всегда применять (ненадёжно). `1.0` = никогда.\n"
            "Рекомендуем `0.4`–`0.6`."
        ),
    },
    "prediction_buffer_minutes": {
        "label": "Проверить за N минут до прогноза",
        "api_key": "prediction_buffer_minutes", "type": "int", "min": 1, "max": 120, "unit": "мин", "cat": "acceleration",
        "desc": (
            "Бот проверяет блогера за столько минут до предсказанного поста.\n"
            "Например: прогноз в 18:00, буфер 15 мин → проверка в 17:45.\n"
            "Меньше → точнее, но рискуем опоздать. Больше → с запасом, но чуть раньше."
        ),
    },
    # ── Deceleration ──
    "silence_multiplier": {
        "label": "Когда считать блогера тихим",
        "api_key": "silence_multiplier", "type": "float", "min": 1.0, "max": 20.0, "unit": "×", "cat": "deceleration",
        "desc": (
            "Если последний пост был давно (дольше чем обычный ритм × этот коэффициент) —\n"
            "блогер считается «тихим» и переходит на максимальный интервал.\n"
            "Больше → терпеливее, долго не замедляем. Меньше → быстро переводим в slow/silent.\n"
            "Пример: `3.0` = если обычно постит раз в 8 ч, а молчит > 24 ч → тихий."
        ),
    },
    "silence_max_factor": {
        "label": "Максимальное замедление при тишине",
        "api_key": "silence_max_factor", "type": "float", "min": 1.0, "max": 10.0, "unit": "×", "cat": "deceleration",
        "desc": (
            "Ограничивает, насколько сильно замедляется тихий блогер.\n"
            "Интервал не вырастет более чем базовый × этот коэффициент.\n"
            "Больше → сильнее замедляем самых тихих. `1.0` = без замедления.\n"
            "Пример: `3.0` + базовый 60 мин = максимум 180 мин при тишине."
        ),
    },
    "nighttime_multiplier": {
        "label": "Замедление в ночные (нерабочие) часы",
        "api_key": "nighttime_multiplier", "type": "float", "min": 1.0, "max": 10.0, "unit": "×", "cat": "deceleration",
        "desc": (
            "В часы, когда блогер обычно не постит, интервал умножается на этот коэффициент.\n"
            "Больше → сильнее тормозим ночью. `1.0` = без изменений.\n"
            "Пример: `2.0` = в «мёртвые» часы проверяем вдвое реже."
        ),
    },
    "empty_streak_threshold": {
        "label": "После скольких пустых проверок замедлять",
        "api_key": "empty_streak_threshold", "type": "int", "min": 1, "max": 100, "unit": "раз", "cat": "deceleration",
        "desc": (
            "Если подряд прошло столько проверок и ни одного нового видео —\n"
            "интервал начинает расти дополнительно.\n"
            "Больше → дольше терпим прежде чем замедлять. Меньше → быстрее реагируем.\n"
            "Пример: `5` = после 5 пустых подряд — дополнительное замедление."
        ),
    },
    "empty_streak_multiplier": {
        "label": "Насколько замедлять при серии пустых проверок",
        "api_key": "empty_streak_multiplier", "type": "float", "min": 1.0, "max": 5.0, "unit": "×", "cat": "deceleration",
        "desc": (
            "После достижения порога пустых проверок интервал × этот коэффициент.\n"
            "Больше → сильнее замедляем «тихарей». `1.0` = без эффекта.\n"
            "Пример: `1.5` = интервал вырастает на 50%."
        ),
    },
    # ── Base ──
    "gap_divisor": {
        "label": "Как из ритма получить интервал",
        "api_key": "gap_divisor", "type": "float", "min": 1.5, "max": 100.0, "unit": "", "cat": "base",
        "desc": (
            "Базовый интервал = средний промежуток между постами ÷ это число.\n"
            "Больше → проверяем чаще относительно ритма. Меньше → реже.\n"
            "Пример: блогер постит раз в 8 ч, делитель 4 → проверка каждые 2 ч.\n"
            "Делитель 8 → каждый час, делитель 2 → каждые 4 ч."
        ),
    },
    "min_samples": {
        "label": "Сколько видео нужно для запуска алгоритма",
        "api_key": "min_samples", "type": "int", "min": 2, "max": 100, "unit": "видео", "cat": "base",
        "desc": (
            "Пока в истории меньше этого числа видео — алгоритм не работает\n"
            "(блогер в статусе «калибровка», проверяется с базовым интервалом).\n"
            "Меньше → быстрее начинаем адаптировать. Больше → надёжнее, но дольше ждём.\n"
            "Пример: `5` = нужно минимум 5 видео в истории."
        ),
    },
    "initial_limit": {
        "label": "Сколько видео грузить при первом знакомстве",
        "api_key": "initial_limit", "type": "int", "min": 6, "max": 50, "unit": "видео", "cat": "base",
        "desc": (
            "Когда блогера только добавили, бот скачивает последние N видео\n"
            "чтобы сразу же набрать историю для алгоритма.\n"
            "Больше → быстрее калибровка, но первый парсинг дольше.\n"
            "Пример: `30` = берём 30 последних видео сразу."
        ),
    },
    # ── Hours ──
    "hour_histogram_min_total": {
        "label": "Мин. видео для учёта часов активности",
        "api_key": "hour_histogram_min_total", "type": "int", "min": 5, "max": 500, "unit": "видео", "cat": "hours",
        "desc": (
            "Минимальное кол-во видео в истории, при котором бот учитывает часы суток.\n"
            "Если данных мало — ночное замедление не применяется (мало смысла).\n"
            "Пример: `10` = нужно минимум 10 видео чтобы понять «когда блогер активен»."
        ),
    },
    "active_hour_threshold": {
        "label": "Порог признания часа «активным»",
        "api_key": "active_hour_threshold", "type": "float", "min": 0.1, "max": 1.0, "unit": "от пика", "cat": "hours",
        "desc": (
            "Час считается активным, если в нём постов ≥ пик × это число.\n"
            "Меньше → больше часов считаются активными. Больше → только самые горячие часы.\n"
            "Пример: `0.5` = активен час, где постов ≥ половины от самого загруженного часа."
        ),
    },
    "dead_hour_threshold": {
        "label": "Порог признания часа «мёртвым» (ночным)",
        "api_key": "dead_hour_threshold", "type": "float", "min": 0.0, "max": 0.5, "unit": "от пика", "cat": "hours",
        "desc": (
            "Час считается «мёртвым» (применяется ночное замедление), если в нём постов ≤ пик × это число.\n"
            "Меньше → меньше часов замедляем. `0` = только полностью пустые часы.\n"
            "Пример: `0.15` = час с < 15% от пика считается ночным."
        ),
    },
    "active_hour_multiplier": {
        "label": "Ускорение в часы активности блогера",
        "api_key": "active_hour_multiplier", "type": "float", "min": 0.1, "max": 1.0, "unit": "×", "cat": "hours",
        "desc": (
            "В активные часы блогера интервал × этот коэффициент.\n"
            "Меньше `1.0` → проверяем чаще. `1.0` = без изменений.\n"
            "Пример: `0.7` = в активные часы проверяем на 30% чаще обычного."
        ),
    },
    # ── History ──
    "max_keep": {
        "label": "Сколько видео хранить в памяти блогера",
        "api_key": "max_keep", "type": "int", "min": 5, "max": 200, "unit": "видео", "cat": "history",
        "desc": (
            "Алгоритм использует последние N временных меток видео.\n"
            "Больше → точнее, но больше памяти. Меньше → быстрее адаптируется к смене ритма.\n"
            "Пример: `30` = смотрим на 30 последних видео."
        ),
    },
    "max_history_days": {
        "label": "Игнорировать видео старше (дней)",
        "api_key": "max_history_days", "type": "int", "min": 7, "max": 365, "unit": "дней", "cat": "history",
        "desc": (
            "Видео старше этого срока не учитываются при расчёте ритма.\n"
            "Меньше → ритм считается по свежим данным. Больше → учитываем старые паттерны.\n"
            "Пример: `90` = смотрим только на видео за последние 3 месяца."
        ),
    },
    "check_log_retention_days": {
        "label": "Хранить историю проверок (дней)",
        "api_key": "check_log_retention_days", "type": "int", "min": 1, "max": 180, "unit": "дней", "cat": "history",
        "desc": (
            "Как долго хранить лог каждой проверки (кнопка «История» в карточке блогера).\n"
            "Больше → можно смотреть дальше в прошлое. Меньше → меньше занимает в базе.\n"
            "Пример: `30` = история за последний месяц."
        ),
    },
    # ── Prediction (advanced) ──
    "prediction_high_confidence": {
        "label": "Когда прогноз может отложить проверку",
        "api_key": "prediction_high_confidence", "type": "float", "min": 0.0, "max": 1.0, "unit": "", "cat": "acceleration",
        "desc": (
            "При уверенности ≥ этого порога прогноз может также УДЛИНЯТЬ интервал —\n"
            "то есть ждать ровно до предсказанного окна, а не дёргать почём зря.\n"
            "Пример: `0.65` = высокая уверенность."
        ),
    },
    "recent_gap_weight": {
        "label": "Насколько верить последним видео",
        "api_key": "recent_gap_weight", "type": "float", "min": 0.0, "max": 1.0, "unit": "", "cat": "base",
        "desc": (
            "Сколько веса дать медиане ПОСЛЕДНИХ 5 интервалов vs полной истории.\n"
            "Больше → быстрее реагируем на смену темпа блогера.\n"
            "Меньше → опираемся на долгосрочную статистику.\n"
            "Пример: `0.3` = 30% от свежих, 70% от общей истории."
        ),
    },
    # ── Self-learning feedback loop ──
    "feedback_enabled": {
        "label": "Самообучение по задержке",
        "api_key": "feedback_enabled", "type": "bool", "unit": "", "cat": "feedback",
        "desc": (
            "Бот отслеживает задержку обнаружения постов и сам подстраивает интервал:\n"
            "если опаздывает → проверяет чаще; если ловит мгновенно → расслабляется.\n"
            "Введите `да` или `нет`."
        ),
    },
    "feedback_target_latency_min": {
        "label": "Целевая задержка обнаружения",
        "api_key": "feedback_target_latency_min", "type": "int", "min": 1, "max": 240, "unit": "мин", "cat": "feedback",
        "desc": (
            "Сколько максимум допустимо ждать после публикации до обнаружения.\n"
            "Бот старается держать среднюю задержку около этого значения.\n"
            "Меньше → проверки чаще, нагрузка выше.\n"
            "Пример: `10` мин = ловить новые видео в течение 10 минут."
        ),
    },
    "feedback_ema_alpha": {
        "label": "Насколько быстро самообучение реагирует",
        "api_key": "feedback_ema_alpha", "type": "float", "min": 0.05, "max": 1.0, "unit": "", "cat": "feedback",
        "desc": (
            "Насколько свежая задержка влияет на оценку. EMA-сглаживание.\n"
            "Выше (0.5+) → быстро реагирует на изменения, может дёргаться.\n"
            "Ниже (0.1-0.2) → плавнее, надёжнее.\n"
            "Пример: `0.3` = баланс между плавностью и реакцией."
        ),
    },
    "feedback_min_factor": {
        "label": "Самообучение: максимум ускорения",
        "api_key": "feedback_min_factor", "type": "float", "min": 0.1, "max": 1.0, "unit": "×", "cat": "feedback",
        "desc": (
            "Защита от чрезмерного ускорения. Самонастройка не может уменьшить\n"
            "интервал больше чем в `1/this` раз.\n"
            "Пример: `0.5` = не быстрее чем в 2× от расчётного."
        ),
    },
    "feedback_max_factor": {
        "label": "Самообучение: максимум замедления",
        "api_key": "feedback_max_factor", "type": "float", "min": 1.0, "max": 5.0, "unit": "×", "cat": "feedback",
        "desc": (
            "Защита от чрезмерного замедления. Самонастройка не может увеличить\n"
            "интервал больше чем в `this` раз.\n"
            "Пример: `2.0` = не медленнее чем в 2× от расчётного."
        ),
    },
    "feedback_late_threshold": {
        "label": "Порог опоздания",
        "api_key": "feedback_late_threshold", "type": "float", "min": 1.0, "max": 10.0, "unit": "×", "cat": "feedback",
        "desc": (
            "Считаем что опоздали если задержка > target × `this`.\n"
            "Используется только для счётчика опозданий в карточке блогера.\n"
            "Пример: `2.0` = опоздание = 2× от target."
        ),
    },
}

_STATUS_EMOJI: dict[str, str] = {
    "active": "🔥",
    "burst": "💥",
    "slow": "🐢",
    "silent": "😴",
    "calibrating": "⏳",
}
_PRIORITY_EMOJI: dict[str, str] = {
    "auto": "🟢",
    "vip": "🔴",
    "lazy": "🟡",
    "paused": "⏸️",
}


async def _load_all_adaptive_bloggers() -> list[dict]:
    """Load all adaptive bloggers page-by-page for admin list/search screens."""
    bloggers: list[dict] = []
    offset = 0
    while offset < ADAPTIVE_BLOGGERS_FETCH_MAX:
        batch = await api_client.list_adaptive_bloggers(
            limit=ADAPTIVE_BLOGGERS_FETCH_LIMIT,
            offset=offset,
        )
        if not batch:
            break
        bloggers.extend(batch)
        if len(batch) < ADAPTIVE_BLOGGERS_FETCH_LIMIT:
            break
        offset += len(batch)

    if len(bloggers) >= ADAPTIVE_BLOGGERS_FETCH_MAX:
        logger.warning(
            "Adaptive bloggers list reached safety cap (%s); admin list/search may be truncated",
            ADAPTIVE_BLOGGERS_FETCH_MAX,
        )
    return bloggers


async def _get_adaptive_blogger(blogger_id: int) -> dict | None:
    """Load one adaptive blogger by id; fallback to full list for old API deployments."""
    try:
        return await api_client.get_adaptive_blogger(blogger_id)
    except Exception as exc:
        logger.warning("Failed to load adaptive blogger %s directly: %s", blogger_id, exc)

    try:
        all_bloggers = await _load_all_adaptive_bloggers()
        return next((b for b in all_bloggers if int(b.get("id") or 0) == int(blogger_id)), None)
    except Exception as exc:
        logger.warning("Fallback adaptive blogger lookup failed for %s: %s", blogger_id, exc)
        return None


def _fmt_minutes_human(value, *, empty: str = "—") -> str:
    if value is None:
        return empty
    try:
        minutes = int(round(float(value)))
    except (TypeError, ValueError):
        return empty
    if minutes < 60:
        return f"{minutes} мин"
    hours = minutes / 60.0
    if hours < 24:
        return f"{hours:.1f} ч"
    return f"{hours / 24.0:.1f} д"


def _fmt_hours_human(value, *, empty: str = "неизвестно") -> str:
    if value is None:
        return empty
    try:
        hours = float(value)
    except (TypeError, ValueError):
        return empty
    if hours < 1:
        return f"{int(hours * 60)} мин"
    if hours < 24:
        return f"{hours:.1f} ч"
    return f"{hours / 24.0:.1f} д"


def _humanize_adaptive_reason(reason: str | None) -> str | None:
    if not reason:
        return None
    low = str(reason).lower()
    parts: list[str] = []
    if "calibrating" in low:
        parts.append("ещё мало истории, пока учится на последних видео")
    if "found_new" in low or "active:" in low:
        parts.append("недавно нашли видео, поэтому проверяем чаще")
    if "burst" in low:
        parts.append("у блогера серия публикаций, держим повышенную частоту")
    if "silent" in low:
        parts.append("блогер давно молчит, поэтому проверяем реже")
    if "post-active ramp" in low:
        parts.append("активная фаза заканчивается, интервал плавно возвращается к обычному")
    if "median" in low and not parts:
        parts.append("интервал рассчитан по обычному ритму публикаций")
    if "active h" in low:
        parts.append("сейчас его активный час")
    if "dead h" in low:
        parts.append("сейчас час, когда он обычно не постит")
    if "feedback" in low:
        parts.append("самообучение поправило интервал по прошлым задержкам")
    if "priority=vip" in low:
        parts.append("VIP ускоряет проверки")
    if "priority=lazy" in low:
        parts.append("ленивый режим замедляет проверки")
    if "clamped" in low:
        parts.append("сработали ручные/общие ограничения интервала")
    if not parts:
        return escape_markdown_simple(str(reason))
    return "; ".join(dict.fromkeys(parts))


def _format_adaptive_settings_text(s: dict) -> str:
    enabled = s.get("adaptive_enabled")
    enabled_str = "работает" if enabled else "выключен"
    enabled_emoji = "✅" if enabled else "❌"
    mn = s.get("min_interval_minutes")
    mx = s.get("max_interval_minutes")
    phase_m = s.get("active_phase_interval_minutes")
    phase_h = s.get("active_phase_hours")
    burst_n = s.get("burst_threshold_24h")
    burst_rel = s.get("burst_relative_factor")
    silence = s.get("silence_multiplier")
    gap_div = s.get("gap_divisor")
    samples = s.get("min_samples")
    pred_conf = s.get("prediction_min_confidence")
    pred_buf = s.get("prediction_buffer_minutes")
    feedback_enabled = bool(s.get("feedback_enabled"))
    target_latency = s.get("feedback_target_latency_min")

    pred_conf_str = f"`{int((pred_conf or 0) * 100)}%`" if pred_conf is not None else "—"
    feedback_str = (
        f"включено, цель `{target_latency}` мин"
        if feedback_enabled
        else "выключено"
    )

    off_banner = (
        "⚠️ *Адаптив сейчас выключен.* Все настройки ниже сохраняются, "
        "но **не применяются** к проверкам — воркер использует ручной интервал из подписки.\n\n"
        if not enabled
        else ""
    )

    return (
        f"*🧠 Адаптивный режим: {enabled_emoji} {enabled_str}*\n\n"
        f"{off_banner}"
        "Простыми словами: бот смотрит, как часто каждый блогер обычно выкладывает видео, "
        "и сам выбирает следующий интервал проверки.\n\n"
        "*Что сейчас настроено:*\n"
        f"• Проверять не чаще `{mn}` мин и не реже `{mx}` мин\n"
        f"• После нового видео: `{phase_m}` мин в течение `{phase_h}` ч\n"
        f"• Если пошла серия: от `{burst_n}` видео за сутки → чаще\n"
        f"• Если блогер молчит: после `{silence}×` его обычного ритма → реже\n"
        f"• Самообучение по задержке: {feedback_str}\n\n"
        "*Обычно трогают только это:*\n"
        "1. `Блогеры` — посмотреть, кого как часто проверяем\n"
        "2. `Цель реакции` — за сколько минут ловить новые видео\n"
        "3. `Мин/макс интервал` — границы нагрузки\n\n"
        f"Тонкая настройка: калибровка `{samples}` видео, ритм ÷ `{gap_div}`, "
        f"прогноз от {pred_conf_str} с буфером `{pred_buf}` мин."
    )


_STATUS_LABEL = {
    "active":      "часто проверяем — недавно было видео",
    "burst":       "очень часто — пошла серия видео",
    "slow":        "обычно — проверяем по ритму блогера",
    "silent":      "редко — блогер давно молчит",
    "calibrating": "учится — мало истории",
}

_PRIORITY_LABEL = {
    "auto":   "авто — решает алгоритм",
    "vip":    "VIP — чаще обычного",
    "lazy":   "экономный — реже обычного",
    "paused": "пауза — не проверяем",
}


def _format_blogger_detail_text(b: dict) -> str:
    import time as _time
    from services.adaptive_scheduler import build_hour_histogram_bars, format_predicted_ts
    priority = b.get("priority_mode", "auto")
    status = b.get("status") or "calibrating"
    p_emoji = _PRIORITY_EMOJI.get(priority, "")
    s_emoji = _STATUS_EMOJI.get(status, "⏳")
    interval = b.get("next_interval_minutes")
    gap = b.get("median_gap_hours")
    age = b.get("last_post_age_hours")
    posts = b.get("post_count_analyzed")
    next_check = b.get("next_check_at")
    empty_streak = b.get("empty_streak") or 0
    override_min = b.get("override_min_interval")
    override_max = b.get("override_max_interval")
    notes = b.get("notes")
    is_burst = b.get("is_burst")
    reason = b.get("reason")
    predicted_ts = b.get("predicted_next_post_ts")
    predicted_conf = b.get("predicted_confidence")
    prediction_used = b.get("prediction_used")
    cadence_type = b.get("cadence_type")
    gap_cv = b.get("gap_cv")
    recent_gap = b.get("recent_gap_hours")
    active_days = b.get("active_days") or []
    latency_ema = b.get("latency_ema_minutes")
    last_latency = b.get("last_detection_latency_min")
    total_caught = b.get("total_videos_caught") or 0
    total_late = b.get("total_late_catches") or 0
    last_batch_latency = b.get("last_batch_latency") or {}
    profile_access_failure = b.get("profile_access_failure") if isinstance(b.get("profile_access_failure"), dict) else None

    if interval is not None:
        interval_str = f"каждые `{_fmt_minutes_human(interval)}`"
        if is_burst:
            interval_str += " (серия видео)"
    else:
        interval_str = "по обычному ручному интервалу, пока идёт обучение"

    # Next check
    next_check_str = next_check[:16].replace("T", " ") if next_check else "не задана"

    gap_str = _fmt_hours_human(gap)
    age_str = f"{_fmt_hours_human(age)} назад" if age is not None else "неизвестно"
    reason_human = _humanize_adaptive_reason(reason)

    lines = [
        markdown_handle(b.get("username", "")),
        f"{s_emoji} Сейчас: *{_STATUS_LABEL.get(status, status)}*",
        f"{p_emoji} Ручной режим: *{_PRIORITY_LABEL.get(priority, priority)}*",
        "",
        f"*Проверка:* {interval_str}",
        f"*Следующая:* `{next_check_str}`",
        f"*Обычный ритм:* 1 видео примерно раз в `{gap_str}`",
        f"*Последний пост:* `{age_str}`",
        f"*История для расчёта:* `{posts or '—'}` видео",
    ]

    if reason_human:
        lines.append(f"*Почему так:* _{reason_human}_")

    # Cadence + recency block
    cadence_labels = {
        "clockwork": "ровный, почти по расписанию",
        "regular":   "понятный, но не идеально ровный",
        "sporadic":  "неровный",
        "chaotic":   "хаотичный",
    }
    if cadence_type and cadence_type in cadence_labels:
        lines.append(f"*Тип ритма:* {cadence_labels[cadence_type]}")
    if recent_gap is not None and gap is not None:
        # only highlight if it diverges from full-history median
        if abs(recent_gap - gap) / max(gap, 0.1) > 0.2:
            recent_str = f"{recent_gap:.1f} ч" if recent_gap >= 1 else f"{int(recent_gap * 60)} мин"
            full_str = f"{gap:.1f} ч" if gap >= 1 else f"{int(gap * 60)} мин"
            arrow = "стал чаще постить" if recent_gap < gap else "стал реже постить"
            lines.append(f"*Последние видео:* `{recent_str}` между постами _({arrow}, обычно {full_str})_")

    # Trend block
    trend_dir = b.get("trend_direction") or "stable"
    trend_r = b.get("trend_ratio") or 1.0
    if trend_dir not in ("stable", "unknown"):
        _TREND_ICONS = {
            "accelerating": "🚀", "speeding_up":  "📈",
            "decelerating": "📉", "slowing_down": "🐢",
        }
        _TREND_LABELS = {
            "accelerating": "сильно разогнался",
            "speeding_up":  "постит чаще обычного",
            "decelerating": "постит реже обычного",
            "slowing_down": "сильно замедлился",
        }
        icon = _TREND_ICONS.get(trend_dir, "")
        label = _TREND_LABELS.get(trend_dir, trend_dir)
        pct = int(abs(1.0 - float(trend_r)) * 100)
        direction_word = "быстрее" if float(trend_r) < 1.0 else "медленнее"
        lines.append(f"{icon} *Тренд:* {label} — посл. {pct}% {direction_word} чем раньше")

    if active_days:
        _DOW_RU = ["Пн", "Вт", "Ср", "Чт", "Пт", "Сб", "Вс"]
        days_str = " ".join(_DOW_RU[d] for d in active_days if 0 <= d < 7)
        if days_str:
            lines.append(f"*Дни, когда чаще постит:* `{days_str}`")

    # Self-learning latency block
    if latency_ema is not None or last_latency is not None or total_caught > 0:
        lat_parts = []
        if latency_ema is not None:
            lat_parts.append(f"обычно ловим за `{_fmt_minutes_human(latency_ema)}`")
        if last_latency is not None:
            lat_parts.append(f"последняя пачка около `{_fmt_minutes_human(last_latency)}`")
        if isinstance(last_batch_latency, dict) and last_batch_latency.get("count"):
            batch_p75 = last_batch_latency.get("p75")
            batch_max = last_batch_latency.get("max")
            batch_count = last_batch_latency.get("count")
            batch_bits = [f"`{batch_count}` видео в последней находке"]
            if batch_p75 is not None:
                batch_bits.append(f"большинство за `{_fmt_minutes_human(batch_p75)}`")
            if batch_max is not None:
                batch_bits.append(f"самое позднее `{_fmt_minutes_human(batch_max)}`")
            lat_parts.append(" / ".join(batch_bits))
        if total_caught > 0:
            late_pct = int(total_late / total_caught * 100) if total_caught else 0
            lat_parts.append(f"всего найдено `{total_caught}`, поздних `{total_late}` ({late_pct}%)")
        lines.append(f"⏱ *Скорость реакции:* {', '.join(lat_parts)}")

    # Prediction block
    if predicted_ts is not None:
        pred_str = format_predicted_ts(predicted_ts, int(_time.time()))
        if pred_str:
            conf_pct = int((predicted_conf or 0) * 100)
            pred_line = f"🔮 *Ожидаем следующий пост:* `{escape_markdown_simple(pred_str)}`"
            if predicted_conf is not None:
                pred_line += f" _(уверенность {conf_pct}%)_"
            if prediction_used:
                pred_line += " ✅ _уже учтено в следующей проверке_"
            lines.append(pred_line)

    if empty_streak >= 3:
        lines.append(f"Пустых проверок подряд: `{empty_streak}` _(если видео нет, бот постепенно экономит проверки)_")

    if profile_access_failure:
        access_streak = int(profile_access_failure.get("streak") or 0)
        reason_text = escape_markdown_simple(str(profile_access_failure.get("reason_text") or profile_access_failure.get("reason") or ""))
        last_at = str(profile_access_failure.get("last_at") or "")[:16].replace("T", " ")
        lines.append(
            f"⚠️ *Доступ к профилю:* `{access_streak}` подряд"
            f"{f' — {reason_text}' if reason_text else ''}"
            f"{f' _(посл. {last_at})_' if last_at else ''}"
        )

    if override_min is not None or override_max is not None:
        ov_min = f"`{override_min}` мин" if override_min is not None else "нет"
        ov_max = f"`{override_max}` мин" if override_max is not None else "нет"
        lines.append(f"\n*Ручные границы для этого блогера:* мин {ov_min} / макс {ov_max}")

    if notes:
        lines.append(f"Заметки: _{escape_markdown_simple(notes)}_")

    hour_histogram = b.get("hour_histogram")
    if isinstance(hour_histogram, dict) and hour_histogram:
        try:
            bars = build_hour_histogram_bars(hour_histogram)
            lines.append(f"\n⏰ *Когда он обычно постит* _(часы UTC)_:\n```\n{bars}\n```")
        except Exception:
            pass

    return "\n".join(lines) + "\n"


def _format_adaptive_summary_text(s: dict) -> str:
    """Render the monitoring summary dashboard for Telegram."""
    total = s.get("total_bloggers", 0)
    by_status = s.get("by_status", {})
    by_priority = s.get("by_priority", {})
    avg_interval = s.get("avg_interval_minutes")
    with_pred = s.get("with_prediction", 0)
    avg_conf = s.get("avg_prediction_confidence")
    burst = s.get("burst_active", 0)
    calibrating = s.get("calibrating", 0)
    paused = s.get("paused", 0)
    next_checks = s.get("next_checks") or []

    status_lines = []
    for status, count in sorted(by_status.items(), key=lambda x: -x[1]):
        emoji = _STATUS_EMOJI.get(status, "•")
        label = _STATUS_LABEL.get(status, status)
        status_lines.append(f"  {emoji} {label}: `{count}`")

    priority_lines = []
    for prio, count in sorted(by_priority.items(), key=lambda x: -x[1]):
        emoji = _PRIORITY_EMOJI.get(prio, "•")
        label = _PRIORITY_LABEL.get(prio, prio)
        priority_lines.append(f"  {emoji} {label}: `{count}`")

    avg_str = f"`{avg_interval:.0f}` мин" if avg_interval is not None else "_нет данных_"
    conf_str = f"`{int((avg_conf or 0) * 100)}%`" if avg_conf is not None else "_нет_"

    import time as _t
    now_ts = int(_t.time())

    next_lines = []
    for item in next_checks[:8]:
        username = item.get("username", "?")
        nc = item.get("next_check_at", "")
        nc_short = nc[:16].replace("T", " ") if nc else "—"
        interval = item.get("interval")
        status = item.get("status", "?")
        s_emoji = _STATUS_EMOJI.get(status, "•")
        p_emoji = _PRIORITY_EMOJI.get(item.get("priority", "auto"), "")
        interval_str = f"`{interval}`м" if interval else "—"
        next_lines.append(f"  {p_emoji}{s_emoji} {markdown_handle(username)} → {nc_short} ({interval_str})")

    lines = [
        f"*📊 Адаптивный режим — общая картина*\n",
        f"Блогеров под контролем: `{total}`",
        f"Средняя частота проверок: {avg_str}",
        f"Сейчас в серии видео: `{burst}` | На паузе: `{paused}` | Ещё учатся: `{calibrating}`\n",
        "*Что происходит с блогерами:*",
        *status_lines,
        "",
        "*Ручные режимы:*",
        *priority_lines,
        "",
        f"*Прогноз следующего поста:* есть у `{with_pred}` блогеров, средняя уверенность {conf_str}",
    ]

    if next_lines:
        lines += ["", "*Кого бот проверит следующим:*", *next_lines]

    return "\n".join(lines)


# ── 1. Main adaptive panel ──────────────────────────────────────────────────

@router.callback_query(F.data == "admin:adaptive")
async def adaptive_settings_menu(callback: CallbackQuery, state: FSMContext):
    await state.clear()
    try:
        await callback.answer()
        data = await api_client.get_adaptive_settings()
        text = _format_adaptive_settings_text(data)
        kb = get_adaptive_settings_keyboard(bool(data.get("adaptive_enabled")))
        await _safe_edit_callback_text(callback, text, reply_markup=kb, parse_mode="Markdown")
    except Exception as e:
        logger.error("adaptive_settings_menu error: %s", e)
        try:
            await callback.answer(f"Ошибка: {str(e)[:100]}", show_alert=True)
        except Exception:
            pass


# ── 2. Toggle enabled/disabled ─────────────────────────────────────────────

@router.callback_query(F.data == "admin:adaptive:toggle")
async def adaptive_toggle(callback: CallbackQuery):
    try:
        await callback.answer()
        current = await api_client.get_adaptive_settings()
        new_val = not bool(current.get("adaptive_enabled"))
        data = await api_client.update_adaptive_settings(adaptive_enabled=new_val)
        await log_admin_action(
            callback.from_user.id,
            "adaptive_toggle",
            description=f"adaptive_enabled → {new_val}",
        )
        text = _format_adaptive_settings_text(data)
        kb = get_adaptive_settings_keyboard(bool(data.get("adaptive_enabled")))
        await _safe_edit_callback_text(callback, text, reply_markup=kb, parse_mode="Markdown")
    except Exception as e:
        logger.error("adaptive_toggle error: %s", e)
        try:
            await callback.answer(f"Ошибка: {str(e)[:100]}", show_alert=True)
        except Exception:
            pass


# ── 3. Start editing a numeric setting ────────────────────────────────────

@router.callback_query(F.data.startswith("admin:adaptive:edit:"))
async def adaptive_start_edit(callback: CallbackQuery, state: FSMContext):
    slug = callback.data.removeprefix("admin:adaptive:edit:")
    meta = _ADAPTIVE_SETTING_META.get(slug)
    if not meta:
        await callback.answer("Неизвестный параметр", show_alert=True)
        return
    await callback.answer()
    try:
        current = await api_client.get_adaptive_settings()
        old_value = current.get(meta["api_key"])
    except Exception:
        old_value = "?"

    cat = meta.get("cat", "")
    back_cb = f"admin:adaptive:cat:{cat}" if cat else "admin:adaptive"

    unit_str = f" {meta['unit']}" if meta.get("unit") else ""
    desc = meta.get("desc", "")

    if meta["type"] == "bool":
        # Bool: show inline buttons immediately — no FSM text input needed
        on_text = ("✅ Включить" if not old_value else "✅ Включено (сейчас)")
        off_text = ("❌ Выключить" if old_value else "❌ Выключено (сейчас)")
        cur_display = "включено" if old_value else "выключено"
        text = (
            f"*{escape_markdown_simple(meta['label'])}*\n\n"
            f"{escape_markdown_simple(desc)}\n\n"
            f"Сейчас: *{cur_display}*"
        )
        kb = InlineKeyboardMarkup(inline_keyboard=[
            [
                InlineKeyboardButton(text=on_text, callback_data=f"admin:adaptive:boolset:{slug}:1"),
                InlineKeyboardButton(text=off_text, callback_data=f"admin:adaptive:boolset:{slug}:0"),
            ],
            [InlineKeyboardButton(text="◀️ Отмена", callback_data=back_cb)],
        ])
        await _safe_edit_callback_text(callback, text, reply_markup=kb, parse_mode="Markdown")
        return  # No FSM state needed

    await state.set_data({
        "adaptive_setting_slug": slug,
        "adaptive_setting_meta": meta,
        "adaptive_setting_old_value": old_value,
        "adaptive_back_cb": back_cb,
    })
    if meta["type"] == "int":
        await state.set_state(OrderStates.ADMIN_ADAPTIVE_EDIT_INT)
    else:
        await state.set_state(OrderStates.ADMIN_ADAPTIVE_EDIT_FLOAT)

    text = (
        f"*{escape_markdown_simple(meta['label'])}*\n\n"
        f"{escape_markdown_simple(desc)}\n\n"
        f"Сейчас: `{old_value}`{unit_str}\n"
        f"Допустимо: от `{meta['min']}` до `{meta['max']}`{unit_str}\n\n"
        "Введите новое значение:"
    )
    await _safe_edit_callback_text(
        callback,
        text,
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="◀️ Отмена", callback_data=back_cb)]
        ]),
        parse_mode="Markdown",
    )


# ── 4. Process int input ───────────────────────────────────────────────────

def _adaptive_save_keyboard(back_cb: str, cat: str) -> InlineKeyboardMarkup:
    """Keyboard after saving — goes back to the category sub-menu."""
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="◀️ Назад к разделу", callback_data=back_cb)],
        [InlineKeyboardButton(text="🏠 Все настройки", callback_data="admin:adaptive")],
    ])


def _adaptive_error_keyboard(back_cb: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="◀️ Отмена", callback_data=back_cb)],
    ])


@router.message(StateFilter(OrderStates.ADMIN_ADAPTIVE_EDIT_INT))
async def adaptive_process_int_input(message: Message, state: FSMContext):
    state_data = await state.get_data()
    meta = state_data.get("adaptive_setting_meta", {})
    back_cb = state_data.get("adaptive_back_cb", "admin:adaptive")
    cat = meta.get("cat", "")
    lo, hi = meta.get("min", 1), meta.get("max", 9999)
    try:
        value = int(message.text.strip())
        if not (lo <= value <= hi):
            raise ValueError()
        # Cross-setting guard: prevent min_interval > max_interval inversion
        # by comparing the new value against the current value of its sibling.
        api_key = meta.get("api_key")
        if api_key in ("min_interval_minutes", "max_interval_minutes"):
            current_settings = await api_client.get_adaptive_settings()
            sibling_key = "max_interval_minutes" if api_key == "min_interval_minutes" else "min_interval_minutes"
            sibling_val = current_settings.get(sibling_key)
            if sibling_val is not None:
                new_min = value if api_key == "min_interval_minutes" else int(sibling_val)
                new_max = value if api_key == "max_interval_minutes" else int(sibling_val)
                if new_min > new_max:
                    await message.answer(
                        f"❌ min_interval ({new_min}) не может быть больше max_interval ({new_max}).\n"
                        f"Сначала измените противоположный параметр.",
                        reply_markup=_adaptive_error_keyboard(back_cb),
                        parse_mode="Markdown",
                    )
                    return
        await api_client.update_adaptive_settings(**{meta["api_key"]: value})
        await log_admin_action(
            message.from_user.id,
            "adaptive_setting_update",
            description=f"{meta['api_key']} = {value}",
        )
        await state.clear()
        unit_str = f" {meta['unit']}" if meta.get("unit") else ""
        await message.answer(
            f"✅ *{escape_markdown_simple(meta['label'])}*\n"
            f"Новое значение: `{value}`{unit_str}",
            reply_markup=_adaptive_save_keyboard(back_cb, cat),
            parse_mode="Markdown",
        )
    except ValueError:
        await message.answer(
            f"❌ Нужно целое число от `{lo}` до `{hi}`.\nПопробуйте ещё раз:",
            reply_markup=_adaptive_error_keyboard(back_cb),
            parse_mode="Markdown",
        )
    except Exception as e:
        logger.error("adaptive_process_int_input error: %s", e)
        await message.answer(f"Ошибка сохранения: {str(e)[:200]}")
        await state.clear()


# ── 5. Process float input ─────────────────────────────────────────────────

@router.message(StateFilter(OrderStates.ADMIN_ADAPTIVE_EDIT_FLOAT))
async def adaptive_process_float_input(message: Message, state: FSMContext):
    state_data = await state.get_data()
    meta = state_data.get("adaptive_setting_meta", {})
    back_cb = state_data.get("adaptive_back_cb", "admin:adaptive")
    cat = meta.get("cat", "")
    lo, hi = meta.get("min", 0.0), meta.get("max", 9999.0)
    try:
        value = float(message.text.strip().replace(",", "."))
        if not (lo <= value <= hi):
            raise ValueError()
        await api_client.update_adaptive_settings(**{meta["api_key"]: value})
        await log_admin_action(
            message.from_user.id,
            "adaptive_setting_update",
            description=f"{meta['api_key']} = {value}",
        )
        await state.clear()
        unit_str = f" {meta['unit']}" if meta.get("unit") else ""
        await message.answer(
            f"✅ *{escape_markdown_simple(meta['label'])}*\n"
            f"Новое значение: `{value}`{unit_str}",
            reply_markup=_adaptive_save_keyboard(back_cb, cat),
            parse_mode="Markdown",
        )
    except ValueError:
        await message.answer(
            f"❌ Нужно число от `{lo}` до `{hi}` (можно через запятую).\nПопробуйте ещё раз:",
            reply_markup=_adaptive_error_keyboard(back_cb),
            parse_mode="Markdown",
        )
    except Exception as e:
        logger.error("adaptive_process_float_input error: %s", e)
        await message.answer(f"Ошибка сохранения: {str(e)[:200]}")
        await state.clear()


# ── 5b. Process bool input ─────────────────────────────────────────────────

@router.message(StateFilter(OrderStates.ADMIN_ADAPTIVE_EDIT_BOOL))
async def adaptive_process_bool_input(message: Message, state: FSMContext):
    state_data = await state.get_data()
    meta = state_data.get("adaptive_setting_meta", {})
    back_cb = state_data.get("adaptive_back_cb", "admin:adaptive")
    cat = meta.get("cat", "")
    raw = message.text.strip().lower()
    if raw in ("да", "1", "true", "yes", "on"):
        value = True
    elif raw in ("нет", "0", "false", "no", "off"):
        value = False
    else:
        await message.answer(
            "❌ Введите `да` или `нет`:",
            reply_markup=_adaptive_error_keyboard(back_cb),
            parse_mode="Markdown",
        )
        return
    try:
        await api_client.update_adaptive_settings(**{meta["api_key"]: value})
        await log_admin_action(
            message.from_user.id,
            "adaptive_setting_update",
            description=f"{meta['api_key']} = {value}",
        )
        await state.clear()
        display = "✅ включено" if value else "❌ выключено"
        await message.answer(
            f"✅ *{escape_markdown_simple(meta['label'])}*\n{display}",
            reply_markup=_adaptive_save_keyboard(back_cb, cat),
            parse_mode="Markdown",
        )
    except Exception as e:
        logger.error("adaptive_process_bool_input error: %s", e)
        await message.answer(f"Ошибка сохранения: {str(e)[:200]}")
        await state.clear()


# ── 5b-2. Bool setting via inline buttons (no FSM needed) ────────────────────

@router.callback_query(F.data.startswith("admin:adaptive:boolset:"))
async def adaptive_process_bool_button(callback: CallbackQuery):
    parts = callback.data.split(":")
    # format: admin:adaptive:boolset:{slug}:{0|1}
    if len(parts) < 5:
        await callback.answer("Неверный формат", show_alert=True)
        return
    slug = parts[3]
    value = parts[4] == "1"
    meta = _ADAPTIVE_SETTING_META.get(slug)
    if not meta:
        await callback.answer("Неизвестный параметр", show_alert=True)
        return
    cat = meta.get("cat", "")
    back_cb = f"admin:adaptive:cat:{cat}" if cat else "admin:adaptive"
    try:
        await api_client.update_adaptive_settings(**{meta["api_key"]: value})
        await log_admin_action(
            callback.from_user.id,
            "adaptive_setting_update",
            description=f"{meta['api_key']} = {value}",
        )
        display = "✅ включено" if value else "❌ выключено"
        await callback.answer(display, show_alert=False)
        await _safe_edit_callback_text(
            callback,
            f"✅ *{escape_markdown_simple(meta['label'])}*\n{display}",
            reply_markup=_adaptive_save_keyboard(back_cb, cat),
            parse_mode="Markdown",
        )
    except Exception as e:
        logger.error("adaptive_process_bool_button error: %s", e)
        await callback.answer(f"Ошибка: {str(e)[:100]}", show_alert=True)


# ── 5c. Blogger override edit ──────────────────────────────────────────────

@router.callback_query(F.data.regexp(r"^admin:adaptive:blogger:(\d+):override_(min|max):(\d+)$"))
async def adaptive_blogger_override_start(callback: CallbackQuery, state: FSMContext):
    match = re.match(r"^admin:adaptive:blogger:(\d+):override_(min|max):(\d+)$", callback.data)
    if not match:
        await callback.answer("Неверный формат", show_alert=True)
        return
    blogger_id = int(match.group(1))
    field = match.group(2)  # "min" or "max"
    page = int(match.group(3))
    label = "минимальный интервал" if field == "min" else "максимальный интервал"
    back_cb = f"admin:adaptive:blogger:{blogger_id}:page:{page}"
    await callback.answer()
    await state.set_state(OrderStates.ADMIN_BLOGGER_OVERRIDE)
    await state.set_data({
        "blogger_id": blogger_id,
        "override_field": f"override_{field}_interval",
        "override_page": page,
        "adaptive_back_cb": back_cb,
    })
    text = (
        f"*Ручное ограничение: {label}*\n\n"
        "Это правило только для одного блогера и сильнее общей настройки.\n"
        "Введите значение в минутах, например `60`.\n"
        "`0` — снять ограничение и снова доверить алгоритму."
    )
    await _safe_edit_callback_text(
        callback, text,
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="◀️ Отмена", callback_data=back_cb)]
        ]),
        parse_mode="Markdown",
    )


@router.message(StateFilter(OrderStates.ADMIN_BLOGGER_OVERRIDE))
async def adaptive_blogger_override_input(message: Message, state: FSMContext):
    state_data = await state.get_data()
    blogger_id = state_data.get("blogger_id")
    field = state_data.get("override_field")
    page = state_data.get("override_page", 0)
    back_cb = state_data.get("adaptive_back_cb", "admin:adaptive")
    try:
        val = int(message.text.strip())
        if val < 0:
            raise ValueError()
        current = await _get_adaptive_blogger(int(blogger_id))
        current_min = current.get("override_min_interval") if current else None
        current_max = current.get("override_max_interval") if current else None
        new_value = val if val > 0 else None
        final_min = new_value if field == "override_min_interval" else current_min
        final_max = new_value if field == "override_max_interval" else current_max
        if final_min is not None and final_max is not None and int(final_min) > int(final_max):
            raise ValueError(f"min_interval ({final_min}) > max_interval ({final_max})")
        payload = {field: val if val > 0 else None}
        b = await api_client.set_blogger_overrides(blogger_id, **payload)
        await log_admin_action(
            message.from_user.id,
            "blogger_override",
            description=f"blogger {blogger_id} {field}={val if val > 0 else 'cleared'}",
        )
        await state.clear()
        display = f"`{val}` мин" if val > 0 else "снято"
        saved_label = "минимум" if field == "override_min_interval" else "максимум"
        await message.answer(
            f"✅ Ручной {saved_label} для блогера: {display}",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="◀️ К блогеру", callback_data=back_cb)]
            ]),
            parse_mode="Markdown",
        )
    except ValueError:
        await message.answer(
            "❌ Введите целое число ≥ 0 (0 = снять ограничение):",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="◀️ Отмена", callback_data=back_cb)]
            ]),
            parse_mode="Markdown",
        )
    except Exception as e:
        logger.error("adaptive_blogger_override_input error: %s", e)
        await message.answer(f"Ошибка: {str(e)[:200]}")
        await state.clear()


# ── 6. Bloggers list (paginated, inline buttons per blogger) ───────────────

def _filter_adaptive_bloggers(bloggers: list[dict], query: str) -> list[dict]:
    needle = (query or "").strip().lstrip("@").lower()
    if not needle:
        return []

    def username(blogger: dict) -> str:
        return str(blogger.get("username") or "").strip().lstrip("@").lower()

    matches = [blogger for blogger in bloggers if needle in username(blogger)]
    matches.sort(
        key=lambda blogger: (
            0 if username(blogger) == needle else 1 if username(blogger).startswith(needle) else 2,
            username(blogger),
        )
    )
    return matches


def _build_adaptive_blogger_search_result(
    matches: list[dict],
    *,
    query: str,
    page: int = 0,
    per_page: int = ADAPTIVE_BLOGGER_SEARCH_PER_PAGE,
) -> tuple[str, InlineKeyboardMarkup, int]:
    page = _clamp_page(page, len(matches), per_page)
    start = page * per_page
    end = min(start + per_page, len(matches))
    page_items = matches[start:end]
    safe_query = markdown_inline_code(query[:80])

    rows: list[list[InlineKeyboardButton]] = []
    lines = [
        f"*🔍 Найдено: {len(matches)}* по запросу {safe_query}",
        f"Показано `{start + 1}-{end}` из `{len(matches)}`.\n",
        "Значки: 🟢 авто, 🔴 VIP, 🟡 экономия, ⏸ пауза; 🔥 часто, 💥 серия, 😴 тихий.\n",
    ]
    for blogger in page_items:
        priority = blogger.get("priority_mode", "auto")
        status = blogger.get("status") or "calibrating"
        p_e = _PRIORITY_EMOJI.get(priority, "")
        s_e = _STATUS_EMOJI.get(status, "")
        interval = blogger.get("next_interval_minutes")
        interval_text = _fmt_minutes_human(interval, empty="?")
        username = (blogger.get("username") or "")[:22]
        blogger_id = blogger.get("id")
        rows.append([InlineKeyboardButton(
            text=f"{p_e}{s_e} @{username}  {interval_text}",
            callback_data=f"admin:adaptive:blogger:{blogger_id}:page:0",
        )])
        int_str = f"каждые {_fmt_minutes_human(interval)}" if interval else "ещё учится"
        status_label = _STATUS_LABEL.get(status, status).split(" — ", 1)[0]
        lines.append(
            f"{p_e}{s_e} {markdown_handle(blogger.get('username') or '')} — _{int_str}; {status_label}_"
        )

    max_page = max(0, (len(matches) - 1) // max(1, per_page)) if matches else 0
    nav_row: list[InlineKeyboardButton] = []
    if page > 0:
        nav_row.append(InlineKeyboardButton(
            text="◀️",
            callback_data=f"admin:adaptive:bloggers:search_page:{page - 1}",
        ))
    if max_page > 0:
        nav_row.append(InlineKeyboardButton(text=f"{page + 1}/{max_page + 1}", callback_data="admin:adaptive:bloggers:noop"))
    if page < max_page:
        nav_row.append(InlineKeyboardButton(
            text="▶️",
            callback_data=f"admin:adaptive:bloggers:search_page:{page + 1}",
        ))
    if nav_row:
        rows.append(nav_row)

    rows.append([InlineKeyboardButton(text="🔍 Искать ещё", callback_data="admin:adaptive:bloggers:search")])
    rows.append([InlineKeyboardButton(text="👤 К списку", callback_data="admin:adaptive:bloggers")])
    return "\n".join(lines), InlineKeyboardMarkup(inline_keyboard=rows), page


def _build_bloggers_page_keyboard(page_items: list, page: int, has_next: bool) -> InlineKeyboardMarkup:
    """Собирает клавиатуру: одна строка на блогера + навигация."""
    rows = []
    for b in page_items:
        priority = b.get("priority_mode", "auto")
        status = b.get("status") or "calibrating"
        p_e = _PRIORITY_EMOJI.get(priority, "")
        s_e = _STATUS_EMOJI.get(status, "")
        interval = b.get("next_interval_minutes")
        interval_text = _fmt_minutes_human(interval, empty="?")
        username = (b.get("username") or "")[:22]
        blogger_id = b.get("id")
        btn_text = f"{p_e}{s_e} @{username}  {interval_text}"
        rows.append([InlineKeyboardButton(
            text=btn_text,
            callback_data=f"admin:adaptive:blogger:{blogger_id}:page:{page}",
        )])

    nav = []
    if page > 0:
        nav.append(InlineKeyboardButton(text="◀️", callback_data=f"admin:adaptive:bloggers:page:{page - 1}"))
    if has_next:
        nav.append(InlineKeyboardButton(text="▶️", callback_data=f"admin:adaptive:bloggers:page:{page + 1}"))
    if nav:
        rows.append(nav)
    rows.append([InlineKeyboardButton(text="🔍 Найти блогера", callback_data="admin:adaptive:bloggers:search")])
    rows.append([InlineKeyboardButton(text="◀️ К настройкам", callback_data="admin:adaptive")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


async def _show_adaptive_bloggers(callback: CallbackQuery, page: int):
    try:
        await callback.answer()
        # Fetch all at once and sort client-side by interval (fastest first)
        all_bloggers = await _load_all_adaptive_bloggers()
        # Sort: calibrated (have interval) first by interval asc, then calibrating (no interval) at end
        all_bloggers.sort(key=lambda b: (b.get("next_interval_minutes") is None, b.get("next_interval_minutes") or 9999))
        total = len(all_bloggers)
        max_page = max(0, (total - 1) // BLOGGERS_PAGE_SIZE) if total else 0
        page = max(0, min(page, max_page))
        start = page * BLOGGERS_PAGE_SIZE
        end = start + BLOGGERS_PAGE_SIZE
        page_items = all_bloggers[start:end]
        has_next = end < total

        if not page_items and page == 0:
            text = "*👤 Блогеры*\n\nСписок пуст — добавьте блогеров в мониторинг."
            kb = InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="🔍 Найти блогера", callback_data="admin:adaptive:bloggers:search")],
                [InlineKeyboardButton(text="◀️ К настройкам", callback_data="admin:adaptive")]
            ])
        else:
            lines = [f"*👤 Блогеры и интервалы* (стр. {page + 1}{'…' if has_next else ''})\n"]
            lines.append("Значки: 🟢 авто, 🔴 VIP, 🟡 экономия, ⏸ пауза; 🔥 часто, 💥 серия, 😴 тихий.\n")
            for b in page_items:
                p = b.get("priority_mode", "auto")
                s = b.get("status") or "calibrating"
                interval = b.get("next_interval_minutes")
                username = b.get("username") or ""
                # Readable interval
                if interval:
                    int_str = f"каждые {_fmt_minutes_human(interval)}"
                else:
                    int_str = "ещё учится"
                status_label = _STATUS_LABEL.get(s, s).split(" — ", 1)[0]
                lines.append(
                    f"{_PRIORITY_EMOJI.get(p, '')}{_STATUS_EMOJI.get(s, '')} "
                    f"{markdown_handle(username)} — _{int_str}; {status_label}_"
                )
            text = "\n".join(lines)
            kb = _build_bloggers_page_keyboard(page_items, page, has_next)

        await _safe_edit_callback_text(callback, text, reply_markup=kb, parse_mode="Markdown")
    except Exception as e:
        logger.error("_show_adaptive_bloggers error: %s", e)
        try:
            await callback.answer(f"Ошибка: {str(e)[:100]}", show_alert=True)
        except Exception:
            pass


@router.callback_query(F.data == "admin:adaptive:bloggers")
async def adaptive_bloggers_list(callback: CallbackQuery, state: FSMContext):
    try:
        await state.clear()
    except Exception:
        pass
    await _show_adaptive_bloggers(callback, page=0)


@router.callback_query(F.data.startswith("admin:adaptive:bloggers:page:"))
async def adaptive_bloggers_page(callback: CallbackQuery):
    try:
        page = int(callback.data.split(":")[-1])
    except (ValueError, IndexError):
        page = 0
    await _show_adaptive_bloggers(callback, page=page)


# ── 6b. Search blogger by username ─────────────────────────────────────────

@router.callback_query(F.data == "admin:adaptive:bloggers:search")
async def adaptive_bloggers_search_prompt(callback: CallbackQuery, state: FSMContext):
    """Запрашивает username для поиска блогера в адаптивном списке."""
    await callback.answer()
    await state.set_state(OrderStates.ADMIN_BLOGGER_SEARCH)
    text = (
        "*🔍 Поиск блогера*\n\n"
        "Введите username (можно с `@` или без, частичное совпадение тоже работает).\n"
        "Пример: `mark` найдёт `markiplier`, `markrober` и т.п."
    )
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="◀️ Отмена", callback_data="admin:adaptive:bloggers")]
    ])
    await _safe_edit_callback_text(callback, text, reply_markup=kb, parse_mode="Markdown")


@router.message(StateFilter(OrderStates.ADMIN_BLOGGER_SEARCH))
async def adaptive_bloggers_search_input(message: Message, state: FSMContext):
    """Обрабатывает ввод username и показывает совпадения."""
    raw = (message.text or "").strip().lstrip("@").lower()
    if not raw:
        await state.clear()
        await message.answer(
            "❌ Пустой запрос. Откройте список блогеров и попробуйте снова.",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="👤 К списку", callback_data="admin:adaptive:bloggers")]
            ]),
        )
        return
    try:
        all_bloggers = await _load_all_adaptive_bloggers()
    except Exception as e:
        logger.error("adaptive_bloggers_search_input list error: %s", e)
        await state.clear()
        await message.answer(f"Ошибка загрузки блогеров: {str(e)[:120]}")
        return

    matches = _filter_adaptive_bloggers(all_bloggers, raw)
    await state.update_data(adaptive_blogger_search_query=raw)
    safe_raw = markdown_inline_code(raw[:80])

    if not matches:
        await message.answer(
            f"🔍 По запросу {safe_raw} ничего не найдено.\n"
            f"Всего блогеров в мониторинге: {len(all_bloggers)}.",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="🔍 Искать ещё", callback_data="admin:adaptive:bloggers:search")],
                [InlineKeyboardButton(text="👤 К списку", callback_data="admin:adaptive:bloggers")],
            ]),
            parse_mode="Markdown",
        )
        return

    text, keyboard, _ = _build_adaptive_blogger_search_result(matches, query=raw, page=0)

    await message.answer(
        text,
        reply_markup=keyboard,
        parse_mode="Markdown",
    )


@router.callback_query(F.data.startswith("admin:adaptive:bloggers:search_page:"))
async def adaptive_bloggers_search_page(callback: CallbackQuery, state: FSMContext):
    """Постранично показывает результаты поиска в адаптивном списке блогеров."""
    try:
        page = int((callback.data or "").split(":")[-1])
    except (TypeError, ValueError):
        page = 0

    if not callback.message:
        await callback.answer("Ошибка: сообщение не найдено", show_alert=True)
        return
    data = await state.get_data()
    raw = str(data.get("adaptive_blogger_search_query") or "").strip().lstrip("@").lower()
    if not raw:
        await callback.answer("Поиск устарел. Введите запрос ещё раз.", show_alert=True)
        await state.set_state(OrderStates.ADMIN_BLOGGER_SEARCH)
        await _safe_edit_callback_text(
            callback,
            "*🔍 Поиск блогера*\n\nВведите username или часть username.",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="◀️ Отмена", callback_data="admin:adaptive:bloggers")]
            ]),
            parse_mode="Markdown",
        )
        return

    try:
        await callback.answer()
        all_bloggers = await _load_all_adaptive_bloggers()
        matches = _filter_adaptive_bloggers(all_bloggers, raw)
        if not matches:
            await callback.answer("Совпадений больше нет. Обновляю поиск.", show_alert=False)
            await state.set_state(OrderStates.ADMIN_BLOGGER_SEARCH)
            await _safe_edit_callback_text(
                callback,
                "*🔍 Поиск блогера*\n\nСовпадения исчезли. Введите запрос ещё раз.",
                reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                    [InlineKeyboardButton(text="◀️ Отмена", callback_data="admin:adaptive:bloggers")]
                ]),
                parse_mode="Markdown",
            )
            return
        text, keyboard, _ = _build_adaptive_blogger_search_result(matches, query=raw, page=page)
        await _safe_edit_callback_text(callback, text, reply_markup=keyboard, parse_mode="Markdown")
    except Exception as e:
        logger.error("adaptive_bloggers_search_page error: %s", e)
        await callback.answer("Ошибка загрузки страницы", show_alert=True)


@router.callback_query(F.data == "admin:adaptive:bloggers:noop")
async def adaptive_bloggers_noop(callback: CallbackQuery):
    await callback.answer()


# ── 7. Blogger detail card (callback from list button) ──────────────────────

@router.callback_query(F.data.regexp(r"^admin:adaptive:blogger:(\d+):page:(\d+)$"))
async def adaptive_blogger_detail(callback: CallbackQuery):
    m = re.match(r"^admin:adaptive:blogger:(\d+):page:(\d+)$", callback.data)
    if not m:
        await callback.answer("Неверный формат", show_alert=True)
        return
    blogger_id, page = int(m.group(1)), int(m.group(2))
    try:
        await callback.answer()
        b = await _get_adaptive_blogger(blogger_id)
        if not b:
            await callback.answer("Блогер не найден.", show_alert=True)
            return
        text = _format_blogger_detail_text(b)
        kb = get_blogger_detail_keyboard(blogger_id, page, current_mode=b.get("priority_mode", "auto"))
        await _safe_edit_callback_text(callback, text, reply_markup=kb, parse_mode="Markdown")
    except Exception as e:
        logger.error("adaptive_blogger_detail error: %s", e)
        try:
            await callback.answer(f"Ошибка: {str(e)[:100]}", show_alert=True)
        except Exception:
            pass


# ── 7b. Rename blogger ───────────────────────────────────────────────────────

@router.callback_query(F.data.regexp(r"^admin:adaptive:blogger:(\d+):rename_prompt:(\d+)$"))
async def adaptive_blogger_rename_prompt(callback: CallbackQuery, state: FSMContext):
    match = re.match(r"^admin:adaptive:blogger:(\d+):rename_prompt:(\d+)$", callback.data)
    if not match:
        await callback.answer("Неверный формат", show_alert=True)
        return
    blogger_id = int(match.group(1))
    page = int(match.group(2))
    try:
        await callback.answer()
        b = await _get_adaptive_blogger(blogger_id)
        username = b.get("username") if b else f"id={blogger_id}"
        await state.set_state(OrderStates.ADMIN_BLOGGER_RENAME)
        await state.set_data({
            "blogger_id": blogger_id,
            "blogger_rename_page": page,
            "old_username": b.get("username") if b else None,
        })
        text = (
            "*✏️ Новый ник блогера*\n\n"
            f"Текущий: {markdown_handle(username)}\n\n"
            "Отправьте новый username одним сообщением.\n"
            "Можно прислать `new_name`, `@new_name` или ссылку на профиль TikTok."
        )
        await _safe_edit_callback_text(
            callback,
            text,
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[[
                InlineKeyboardButton(
                    text="❌ Отмена",
                    callback_data=f"admin:adaptive:blogger:{blogger_id}:rename_cancel:{page}",
                )
            ]]),
            parse_mode="Markdown",
        )
    except Exception as e:
        logger.error("adaptive_blogger_rename_prompt error: %s", e)
        try:
            await callback.answer(f"Ошибка: {str(e)[:100]}", show_alert=True)
        except Exception:
            pass


@router.callback_query(F.data.regexp(r"^admin:adaptive:blogger:(\d+):rename_cancel:(\d+)$"))
async def adaptive_blogger_rename_cancel(callback: CallbackQuery, state: FSMContext):
    match = re.match(r"^admin:adaptive:blogger:(\d+):rename_cancel:(\d+)$", callback.data)
    if not match:
        await callback.answer("Неверный формат", show_alert=True)
        return
    blogger_id = int(match.group(1))
    page = int(match.group(2))
    await state.clear()
    await callback.answer("Отменено", show_alert=False)
    try:
        b = await _get_adaptive_blogger(blogger_id)
        if not b:
            await callback.answer("Блогер не найден.", show_alert=True)
            return
        text = _format_blogger_detail_text(b)
        kb = get_blogger_detail_keyboard(blogger_id, page, current_mode=b.get("priority_mode", "auto"))
        await _safe_edit_callback_text(callback, text, reply_markup=kb, parse_mode="Markdown")
    except Exception as e:
        logger.error("adaptive_blogger_rename_cancel error: %s", e)


@router.message(StateFilter(OrderStates.ADMIN_BLOGGER_RENAME))
async def adaptive_blogger_rename_input(message: Message, state: FSMContext):
    state_data = await state.get_data()
    blogger_id = int(state_data.get("blogger_id") or 0)
    page = int(state_data.get("blogger_rename_page") or 0)
    old_username = state_data.get("old_username") or str(blogger_id)
    new_username = (message.text or "").strip()

    if not blogger_id:
        await state.clear()
        await message.answer("Не удалось понять, какого блогера переименовывать. Откройте карточку ещё раз.")
        return
    if not new_username:
        await message.answer("Отправьте новый username текстом.")
        return

    try:
        b = await api_client.rename_blogger(blogger_id, new_username)
        await log_admin_action(
            message.from_user.id,
            "rename_blogger",
            description=f"@{old_username} -> @{b.get('username')} (id={blogger_id})",
        )
        await state.clear()
        await message.answer(
            (
                f"✅ Ник обновлён: {markdown_handle(str(old_username))} "
                f"→ {markdown_handle(str(b.get('username') or blogger_id))}.\n\n"
                "Блогер поставлен на ближайшую перепроверку."
            ),
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(
                    text="👤 Открыть карточку",
                    callback_data=f"admin:adaptive:blogger:{blogger_id}:page:{page}",
                )],
                [InlineKeyboardButton(
                    text="🚀 Проверить сейчас",
                    callback_data=f"admin:adaptive:blogger:{blogger_id}:recheck:{page}",
                )],
            ]),
            parse_mode="Markdown",
        )
    except Exception as e:
        logger.error("adaptive_blogger_rename_input error: %s", e)
        await message.answer(
            f"❌ Не удалось обновить ник: {escape_markdown_simple(str(e)[:200])}\n\n"
            "Проверьте, что username написан правильно и не дублирует уже добавленного блогера.",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[[
                InlineKeyboardButton(
                    text="❌ Отмена",
                    callback_data=f"admin:adaptive:blogger:{blogger_id}:rename_cancel:{page}",
                )
            ]]),
            parse_mode="Markdown",
        )


# ── 8. Set blogger priority ───────────────────────────────────────────────

@router.callback_query(F.data.regexp(r"^admin:adaptive:blogger:(\d+):priority:(\w+):(\d+)$"))
async def adaptive_set_blogger_priority(callback: CallbackQuery):
    match = re.match(r"^admin:adaptive:blogger:(\d+):priority:(\w+):(\d+)$", callback.data)
    if not match:
        await callback.answer("Неверный формат", show_alert=True)
        return
    blogger_id = int(match.group(1))
    priority_mode = match.group(2)
    page = int(match.group(3))
    try:
        await callback.answer()
        b = await api_client.set_blogger_priority(blogger_id, priority_mode)
        await log_admin_action(
            callback.from_user.id,
            "set_blogger_priority",
            description=f"@{b.get('username')} priority_mode={priority_mode}",
        )
        text = _format_blogger_detail_text(b)
        kb = get_blogger_detail_keyboard(blogger_id, page, current_mode=b.get("priority_mode", "auto"))
        await _safe_edit_callback_text(callback, text, reply_markup=kb, parse_mode="Markdown")
    except Exception as e:
        logger.error("adaptive_set_blogger_priority error: %s", e)
        try:
            await callback.answer(f"Ошибка: {str(e)[:100]}", show_alert=True)
        except Exception:
            pass


# ── 9. Category sub-menus ─────────────────────────────────────────────────────

_CATEGORY_TITLES = {
    "acceleration": "⚡ Когда проверять чаще",
    "deceleration": "🐌 Когда проверять реже",
    "base":         "📐 Как бот понимает ритм",
    "hours":        "⏰ Активные и тихие часы",
    "limits":       "🛡 Границы нагрузки",
    "history":      "💾 История и память",
    "feedback":     "🎯 Цель реакции",
}

_CATEGORY_INTRO = {
    "acceleration": (
        "Тут решается, когда бот должен «подсесть» на блогера: после свежего видео, "
        "во время серии публикаций или перед ожидаемым следующим постом."
    ),
    "deceleration": (
        "Эти параметры управляют тем, когда бот *замедляется* и проверяет реже: "
        "когда блогер давно молчит, в ночные часы или после серии пустых проверок."
    ),
    "base": (
        "Это основа. Бот смотрит последние видео, понимает обычный ритм блогера "
        "и из него получает базовый интервал проверки."
    ),
    "hours": (
        "Если в истории достаточно видео, бот строит «карту активности» по часам UTC. "
        "В активные часы — проверяет чаще, в мёртвые — реже."
    ),
    "limits": (
        "Абсолютные границы: алгоритм никогда не выйдет за эти пределы, "
        "что бы ни посчитал."
    ),
    "history": (
        "Сколько данных хранить о каждом блогере и как долго вести лог проверок."
    ),
    "feedback": (
        "Самая человеческая настройка: за сколько минут после публикации ты хочешь "
        "обычно узнавать о новом видео. Бот смотрит прошлые задержки и подстраивает частоту."
    ),
}


_CATEGORY_HINTS = {
    "feedback": "Если нужна быстрая реакция — уменьши цель в минутах. Если важнее экономия — увеличь.",
    "limits": "Эти значения сильнее всего влияют на нагрузку: меньше минимум = чаще проверки.",
    "base": "Обычно достаточно менять только «Первый сбор видео» и «Видео для старта».",
    "acceleration": "Это про активных блогеров. Чем меньше интервалы и множители, тем чаще проверки.",
    "deceleration": "Это про экономию. Чем выше множители, тем реже бот трогает тихих блогеров.",
    "hours": "Работает только когда у блогера уже достаточно истории. Время считается в UTC.",
    "history": "Больше истории = стабильнее расчёт, меньше истории = быстрее подстройка под новый ритм.",
}


@router.callback_query(F.data == "admin:adaptive:advanced")
async def adaptive_advanced_menu(callback: CallbackQuery):
    await callback.answer()
    text = (
        "*🔧 Тонкая настройка*\n\n"
        "Продвинутые параметры алгоритма. "
        "Менять только если понимаешь как работает алгоритм."
    )
    await _safe_edit_callback_text(callback, text, reply_markup=get_adaptive_advanced_keyboard(), parse_mode="Markdown")


@router.callback_query(F.data.startswith("admin:adaptive:cat:"))
async def adaptive_category_menu(callback: CallbackQuery):
    category = callback.data.removeprefix("admin:adaptive:cat:")
    title = _CATEGORY_TITLES.get(category, category)
    intro = _CATEGORY_INTRO.get(category, "")
    hint = _CATEGORY_HINTS.get(category, "")
    try:
        await callback.answer()
        settings = await api_client.get_adaptive_settings()
        metas = [m for m in _ADAPTIVE_SETTING_META.values() if m.get("cat") == category and not m.get("hidden")]
        lines = [f"*{title}*\n", intro]
        if hint:
            lines.append(f"\n_{escape_markdown_simple(hint)}_")
        lines.append("")
        for m in metas:
            val = settings.get(m["api_key"])
            unit_str = f" {m['unit']}" if m.get("unit") else ""
            lines.append(f"• *{escape_markdown_simple(m['label'])}*: `{val}`{unit_str}")
        lines.append("\n_Нажмите на параметр, чтобы изменить:_")
        text = "\n".join(lines)
        kb = get_adaptive_category_keyboard(category)
        await _safe_edit_callback_text(callback, text, reply_markup=kb, parse_mode="Markdown")
    except Exception as e:
        logger.error("adaptive_category_menu error: %s", e)
        try:
            await callback.answer(f"Ошибка: {str(e)[:100]}", show_alert=True)
        except Exception:
            pass


# ── 10. Force recheck ─────────────────────────────────────────────────────────

@router.callback_query(F.data.regexp(r"^admin:adaptive:blogger:(\d+):recheck:(\d+)$"))
async def adaptive_force_recheck(callback: CallbackQuery):
    match = re.match(r"^admin:adaptive:blogger:(\d+):recheck:(\d+)$", callback.data)
    if not match:
        await callback.answer("Неверный формат", show_alert=True)
        return
    blogger_id = int(match.group(1))
    page = int(match.group(2))
    try:
        b = await api_client.force_blogger_recheck(blogger_id)
        await log_admin_action(
            callback.from_user.id,
            "force_recheck",
            description=f"@{b.get('username')} forced recheck",
        )
        await callback.answer(f"✅ @{b.get('username', blogger_id)} встанет в очередь на следующем тике", show_alert=False)
        text = _format_blogger_detail_text(b)
        kb = get_blogger_detail_keyboard(blogger_id, page, current_mode=b.get("priority_mode", "auto"))
        await _safe_edit_callback_text(callback, text, reply_markup=kb, parse_mode="Markdown")
    except Exception as e:
        logger.error("adaptive_force_recheck error: %s", e)
        await callback.answer(f"Ошибка: {str(e)[:100]}", show_alert=True)


# ── 11. Check log ─────────────────────────────────────────────────────────────

@router.callback_query(F.data.regexp(r"^admin:adaptive:blogger:(\d+):log:(\d+)$"))
async def adaptive_check_log(callback: CallbackQuery):
    match = re.match(r"^admin:adaptive:blogger:(\d+):log:(\d+)$", callback.data)
    if not match:
        await callback.answer("Неверный формат", show_alert=True)
        return
    blogger_id = int(match.group(1))
    page = int(match.group(2))
    try:
        await callback.answer()
        entries = await api_client.get_blogger_check_log(blogger_id, limit=15)
        if not entries:
            await _safe_edit_callback_text(
                callback,
                "📋 *История проверок*\n\nЗаписей пока нет — блогер ещё не проверялся.",
                reply_markup=InlineKeyboardMarkup(inline_keyboard=[[
                    InlineKeyboardButton(text="◀️ Назад", callback_data=f"admin:adaptive:blogger:{blogger_id}:page:{page}")
                ]]),
                parse_mode="Markdown",
            )
            return
        lines = [f"*📋 История проверок (посл. {len(entries)})*\n"]
        for e in entries:
            checked = (e.get("checked_at") or "")[:16].replace("T", " ")
            status = e.get("status") or "?"
            found = e.get("found_new_count") or 0
            next_int = e.get("next_interval_minutes")
            dur = e.get("duration_ms")
            latency = e.get("detection_latency_min")
            dur_str = f" {dur}ms" if dur else ""
            found_str = f" +{found}" if found else ""
            int_str = f" → {next_int}м" if next_int else ""
            latency_str = f" p75={latency}м" if latency is not None else ""
            lines.append(f"`{checked}` {status}{found_str}{int_str}{dur_str}{latency_str}")
        text = "\n".join(lines)
        kb = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(
                text="◀️ Назад",
                callback_data=f"admin:adaptive:blogger:{blogger_id}:page:{page}",
            )],
        ])
        await _safe_edit_callback_text(callback, text, reply_markup=kb, parse_mode="Markdown")
    except Exception as e:
        logger.error("adaptive_check_log error: %s", e)
        try:
            await callback.answer(f"Ошибка: {str(e)[:100]}", show_alert=True)
        except Exception:
            pass


# ── 12. Delete blogger ────────────────────────────────────────────────────────

@router.callback_query(F.data.regexp(r"^admin:adaptive:blogger:(\d+):delete_confirm:(\d+)$"))
async def adaptive_blogger_delete_confirm(callback: CallbackQuery):
    match = re.match(r"^admin:adaptive:blogger:(\d+):delete_confirm:(\d+)$", callback.data)
    if not match:
        await callback.answer("Неверный формат", show_alert=True)
        return
    blogger_id = int(match.group(1))
    page = int(match.group(2))
    try:
        await callback.answer()
        b = await _get_adaptive_blogger(blogger_id)
        username = b.get("username") if b else f"id={blogger_id}"
        text = (
            f"*🗑 Удаление блогера*\n\n"
            f"Окончательно удалить {markdown_handle(username)} из мониторинга для всех пользователей?\n\n"
            f"Будут удалены подписки пользователей на этого блогера, найденные видео, batch-и, "
            f"outbox-события и логи проверок. Исторические заказы не трогаем."
        )
        kb = get_blogger_delete_confirm_keyboard(blogger_id, page)
        await _safe_edit_callback_text(callback, text, reply_markup=kb, parse_mode="Markdown")
    except Exception as e:
        logger.error("adaptive_blogger_delete_confirm error: %s", e)
        try:
            await callback.answer(f"Ошибка: {str(e)[:100]}", show_alert=True)
        except Exception:
            pass


@router.callback_query(F.data.regexp(r"^admin:adaptive:blogger:(\d+):delete:(\d+)$"))
async def adaptive_blogger_delete(callback: CallbackQuery):
    match = re.match(r"^admin:adaptive:blogger:(\d+):delete:(\d+)$", callback.data)
    if not match:
        await callback.answer("Неверный формат", show_alert=True)
        return
    blogger_id = int(match.group(1))
    page = int(match.group(2))
    try:
        await callback.answer()
        result = await api_client.delete_blogger(blogger_id)
        username = result.get("username", str(blogger_id))
        await log_admin_action(
            callback.from_user.id,
            "delete_blogger",
            description=f"Удалил блогера @{username} (id={blogger_id}) из мониторинга",
        )
        await _safe_edit_callback_text(
            callback,
            (
                f"✅ Блогер {markdown_handle(username)} окончательно удалён.\n\n"
                f"Подписок: `{int(result.get('deleted_sources') or 0)}`\n"
                f"Видео: `{int(result.get('deleted_videos') or 0)}`\n"
                f"Outbox-событий: `{int(result.get('deleted_outbox') or 0)}`\n"
                f"Логов проверок: `{int(result.get('deleted_check_logs') or 0)}`"
            ),
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="◀️ К списку", callback_data=f"admin:adaptive:bloggers:page:{page}")]
            ]),
            parse_mode="Markdown",
        )
    except Exception as e:
        logger.error("adaptive_blogger_delete error: %s", e)
        try:
            await callback.answer(f"Ошибка: {str(e)[:100]}", show_alert=True)
        except Exception:
            pass


def _format_video_ts(value) -> str:
    if value is None:
        return "?"
    try:
        from datetime import datetime, timezone
        return datetime.fromtimestamp(int(value), tz=timezone.utc).strftime("%m-%d %H:%M")
    except Exception:
        return "?"


def _format_latency_short(value) -> str:
    if value is None:
        return "?"
    try:
        raw = float(value)
    except (TypeError, ValueError):
        return "?"
    if raw < 0:
        raw = 0.0
    if raw < 1.0:
        return "<1м"
    minutes = int(round(raw))
    if minutes < 60:
        return f"{minutes}м"
    hours = minutes / 60.0
    if hours < 24:
        return f"{hours:.1f}ч"
    return f"{hours / 24.0:.1f}д"


def _latency_emoji(value) -> str:
    """Visual indicator for detection latency (in minutes)."""
    if value is None:
        return "•"
    try:
        m = float(value)
    except (TypeError, ValueError):
        return "•"
    if m < 5:
        return "⚡"
    if m < 30:
        return "✅"
    if m < 120:
        return "🟡"
    return "🔴"


def _format_clock_short(value) -> str:
    """Format a unix timestamp as 'HH:MM' (UTC)."""
    if value is None:
        return "??:??"
    try:
        from datetime import datetime, timezone
        return datetime.fromtimestamp(int(value), tz=timezone.utc).strftime("%H:%M")
    except Exception:
        return "??:??"


def _format_iso_clock_short(value) -> str:
    """Take an ISO datetime string ('2026-05-08T18:04:35') and return 'HH:MM'."""
    if not value:
        return "??:??"
    try:
        s = str(value)
        # supports both 'YYYY-MM-DDTHH:MM:SS' and 'YYYY-MM-DD HH:MM:SS'
        time_part = s.replace("T", " ").split(" ", 1)[1] if (" " in s.replace("T", " ")) else ""
        return time_part[:5] if len(time_part) >= 5 else "??:??"
    except Exception:
        return "??:??"


def _bucket_video_by_day(item: dict) -> str:
    """Group bucket label for a video row: 'Сегодня' / 'Вчера' / 'N дней назад' / 'без даты'."""
    from datetime import datetime, timezone
    raw = item.get("discovered_at")
    if not raw:
        return "без даты"
    try:
        s = str(raw).replace("Z", "+00:00")
        dt = datetime.fromisoformat(s)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
    except Exception:
        return "без даты"
    today = datetime.now(timezone.utc).date()
    delta_days = (today - dt.date()).days
    if delta_days <= 0:
        return "Сегодня"
    if delta_days == 1:
        return "Вчера"
    if delta_days < 7:
        return f"{delta_days} дн. назад"
    return dt.strftime("%d.%m")


def _format_video_analytics_text(data: dict) -> str:
    """Render the per-blogger detection log.

    Trade-off vs. the previous version: drops the noisy `id` / `status` / `batch`
    fields from the main row and groups by day. Raw fields are still available in
    the underlying API for power users — this view optimises for "is detection
    working well?" at a glance.
    """
    username = str(data.get("username") or "")
    summary = data.get("latency_summary") or {}
    videos = data.get("videos") or []

    lines = [f"*📊 Лог обнаружения* {markdown_handle(username)}", ""]

    count = summary.get("count") or 0
    if count:
        avg = _format_latency_short(summary.get("avg"))
        p75 = _format_latency_short(summary.get("p75"))
        max_latency = _format_latency_short(summary.get("max"))
        lines.append(
            f"_За последние `{count}` видео — обычно ловим за `{avg}`, "
            f"большинство до `{p75}`, худший случай `{max_latency}`._"
        )
        lines.append("")
        lines.append("⚡ <5м · ✅ <30м · 🟡 <2ч · 🔴 ≥2ч")
    else:
        lines.append("_Задержка обнаружения: пока нет сохранённых create\\_time_")

    if not videos:
        lines.extend(["", "_Пока нет сохранённых видео._"])
        return "\n".join(lines)

    # Group by day bucket while preserving the original "newest first" order
    current_bucket: str | None = None
    rendered = 0
    MAX_ROWS = 12
    for item in videos:
        if rendered >= MAX_ROWS:
            break
        bucket = _bucket_video_by_day(item)
        if bucket != current_bucket:
            lines.extend(["", f"*{bucket}*"])
            current_bucket = bucket

        published = _format_clock_short(item.get("create_time"))
        detected = _format_iso_clock_short(item.get("discovered_at"))
        latency_raw = item.get("detection_latency_min")
        latency = _format_latency_short(latency_raw)
        emoji = _latency_emoji(latency_raw)

        # batch markers go on a secondary line only if useful (multi-clip find)
        batch_size = item.get("batch_size")
        batch_index = item.get("batch_index")
        suffix = ""
        if batch_size and batch_index and int(batch_size) > 1:
            suffix = f" _·пачка {batch_index}/{batch_size}_"

        lines.append(
            f"{emoji} `{published}` → `{detected}` _({latency})_{suffix}"
        )
        rendered += 1

    if len(videos) > MAX_ROWS:
        lines.extend(["", f"_Показано {MAX_ROWS} из {len(videos)} последних видео._"])

    return "\n".join(lines)


@router.callback_query(F.data.regexp(r"^admin:adaptive:blogger:(\d+):videos:(\d+)$"))
async def adaptive_video_analytics(callback: CallbackQuery):
    match = re.match(r"^admin:adaptive:blogger:(\d+):videos:(\d+)$", callback.data)
    if not match:
        await callback.answer("Неверный формат", show_alert=True)
        return
    blogger_id = int(match.group(1))
    page = int(match.group(2))
    try:
        await callback.answer()
        data = await api_client.get_blogger_video_analytics(blogger_id, limit=12)
        text = _format_video_analytics_text(data)
        kb = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(
                text="◀️ Назад",
                callback_data=f"admin:adaptive:blogger:{blogger_id}:page:{page}",
            )],
        ])
        await _safe_edit_callback_text(callback, text, reply_markup=kb, parse_mode="Markdown")
    except Exception as e:
        logger.error("adaptive_video_analytics error: %s", e)
        try:
            await callback.answer(f"Ошибка: {str(e)[:100]}", show_alert=True)
        except Exception:
            pass


# ── 12. Summary dashboard ─────────────────────────────────────────────────────

@router.callback_query(F.data == "admin:adaptive:summary")
async def adaptive_summary(callback: CallbackQuery):
    try:
        await callback.answer()
        data = await api_client.get_adaptive_summary()
        text = _format_adaptive_summary_text(data)
        kb = InlineKeyboardMarkup(inline_keyboard=[
            [
                InlineKeyboardButton(text="🔄 Обновить", callback_data="admin:adaptive:summary"),
                InlineKeyboardButton(text="◀️ Назад", callback_data="admin:adaptive"),
            ],
        ])
        await _safe_edit_callback_text(callback, text, reply_markup=kb, parse_mode="Markdown")
    except Exception as e:
        logger.error("adaptive_summary error: %s", e)
        try:
            await callback.answer(f"Ошибка: {str(e)[:100]}", show_alert=True)
        except Exception:
            pass


# ── 13. Learning report ───────────────────────────────────────────────────────

def _format_learning_report_text(data: dict) -> str:
    total = data.get("total_bloggers", 0)
    calibrated = data.get("calibrated", 0)
    calibrating = data.get("calibrating", 0)
    cal_pct = data.get("calibration_pct", 0.0)
    recently = data.get("recently_calibrated_24h", 0)

    avg_int = data.get("avg_interval_minutes")
    baseline = data.get("flat_baseline_minutes", 30)
    saved_pct = data.get("interval_saved_pct")

    avg_lat = data.get("avg_latency_7d")
    p50_lat = data.get("p50_latency_7d")
    finds_7d = data.get("finds_7d", 0)
    total_checks = data.get("total_checks_7d", 0)
    find_rate = data.get("find_rate_7d")

    fb_enabled = data.get("feedback_enabled", False)
    fb_target = data.get("feedback_target_latency")
    avg_ema = data.get("avg_latency_ema")
    improving = data.get("bloggers_improving", 0)
    late = data.get("bloggers_late", 0)

    status_dist = data.get("status_distribution", {})
    fastest = data.get("fastest", [])

    lines = ["📈 *Отчёт самообучения*\n━━━━━━━━━━━━━━━━━━━━\n"]

    # ── Calibration progress ──
    cal_bar = "▓" * int(cal_pct / 10) + "░" * (10 - int(cal_pct / 10))
    lines.append(f"*Прогресс калибровки*")
    lines.append(f"`{cal_bar}` {cal_pct:.0f}%")
    lines.append(f"  Откалиброван: `{calibrated}` / `{total}` блогеров")
    if calibrating:
        lines.append(f"  Учится: `{calibrating}` блогеров")
    if recently:
        lines.append(f"  За последние 24ч откалиброван: `{recently}` 🆕")

    # ── Status distribution ──
    if status_dist:
        lines.append("")
        lines.append("*Режимы работы*")
        status_icons = {"active": "🔥", "slow": "🐢", "silent": "😴", "calibrating": "⏳", "burst": "💥"}
        for s, cnt in sorted(status_dist.items(), key=lambda x: -x[1]):
            icon = status_icons.get(s, "")
            lines.append(f"  {icon} {s}: `{cnt}`")

    # ── Interval efficiency ──
    lines.append("")
    lines.append("*Эффективность интервалов*")
    if avg_int is not None:
        lines.append(f"  Средний адаптивный: `{avg_int:.0f} мин`")
        lines.append(f"  Плоский базовый: `{baseline} мин`")
        if saved_pct is not None and saved_pct > 0:
            lines.append(f"  ✅ Экономия проверок: `{saved_pct:.0f}%`")
        elif avg_int > baseline:
            excess = avg_int - baseline
            lines.append(f"  ⚠️ Бот проверяет реже базового на `{excess:.0f} мин` (норма для тихих блогеров)")
    else:
        lines.append("  _Нет данных — блогеры ещё калибруются_")

    # ── Detection latency (7d) ──
    lines.append("")
    lines.append("*Задержка обнаружения видео (7д)*")
    if avg_lat is not None:
        lines.append(f"  Средняя: `{avg_lat:.0f} мин` | Медиана: `{p50_lat:.0f} мин`")
        lines.append(f"  Найдено видео: `{finds_7d}` из `{total_checks}` проверок")
        if find_rate is not None:
            lines.append(f"  Частота находок: `{find_rate * 100:.1f}%`")
    else:
        lines.append("  _Нет данных за 7 дней_")

    # ── Feedback loop ──
    lines.append("")
    lines.append("*Петля обратной связи*")
    if fb_enabled:
        lines.append(f"  Статус: 🟢 включена")
        if fb_target:
            lines.append(f"  Цель задержки: `{fb_target} мин`")
        if avg_ema is not None:
            lines.append(f"  Средняя EMA задержки: `{avg_ema:.0f} мин`")
            health_icon = "✅" if avg_ema <= (fb_target or 999) else "⚠️"
            lines.append(f"  {health_icon} Укладываются в цель: `{improving}` | Опаздывают: `{late}`")
        else:
            lines.append("  _Данных EMA пока нет_")
    else:
        lines.append("  Статус: 🔴 выключена")

    # ── Fastest bloggers ──
    if fastest:
        lines.append("")
        lines.append("*Топ-5 по скорости реакции*")
        priority_icons = {"auto": "🟢", "vip": "🔴", "lazy": "🟡", "paused": "⏸️"}
        for b in fastest:
            p_icon = priority_icons.get(b.get("priority", "auto"), "")
            lines.append(f"  {p_icon} {markdown_handle(b.get('username', ''))} — `{b.get('interval', '?')} мин`")

    return "\n".join(lines)


@router.callback_query(F.data == "admin:adaptive:learning")
async def adaptive_learning_report(callback: CallbackQuery):
    """Отчёт самообучения адаптивного мониторинга."""
    try:
        await callback.answer()
        data = await api_client.get_adaptive_learning_report()
        text = _format_learning_report_text(data)
        kb = InlineKeyboardMarkup(inline_keyboard=[
            [
                InlineKeyboardButton(text="🔄 Обновить", callback_data="admin:adaptive:learning"),
                InlineKeyboardButton(text="◀️ Назад", callback_data="admin:adaptive"),
            ],
        ])
        await _safe_edit_callback_text(callback, text, reply_markup=kb, parse_mode="Markdown")
    except Exception as e:
        logger.error("adaptive_learning_report error: %s", e)
        try:
            await callback.answer(f"Ошибка: {str(e)[:100]}", show_alert=True)
        except Exception:
            pass


# ── 14. Backfill ──────────────────────────────────────────────────────────────

@router.callback_query(F.data == "admin:adaptive:backfill")
async def adaptive_backfill_confirm(callback: CallbackQuery):
    """Показывает экран подтверждения перед запуском пересчёта."""
    await callback.answer()
    text = (
        "*⚡ Переобучить адаптивный режим?*\n\n"
        "Бот заново посмотрит историю публикаций за 90 дней и обновит понимание ритма "
        "у всех блогеров.\n\n"
        "Полезно после изменения настроек или после массового импорта блогеров.\n"
        "_Занимает несколько секунд._"
    )
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text="✅ Переобучить", callback_data="admin:adaptive:backfill:run"),
            InlineKeyboardButton(text="❌ Отмена", callback_data="admin:adaptive"),
        ],
    ])
    await _safe_edit_callback_text(callback, text, reply_markup=kb, parse_mode="Markdown")


@router.callback_query(F.data == "admin:adaptive:backfill:run")
async def adaptive_backfill_run(callback: CallbackQuery):
    """Выполняет пересчёт интервалов после подтверждения."""
    try:
        await callback.answer("Переобучаю...", show_alert=False)
        # force=True — пересобираем ритм у всех блогеров (а не только у некалиброванных).
        # reset_feedback=True — параллельно стираем latency_ema, чтобы петля обратной
        # связи не тянула свежий ритм под старые наблюдения задержки.
        result = await api_client.run_adaptive_backfill(
            days=90,
            force=True,
            reset_feedback=True,
        )
        total = result.get("total", 0)
        calibrated = result.get("calibrated", 0)
        skipped = result.get("skipped", 0)
        no_data = result.get("no_data", 0)
        insufficient = result.get("insufficient_data", 0)
        errors = result.get("errors", 0)
        skipped_existing = result.get("skipped_existing", 0)
        avg = result.get("avg_timestamps", 0)
        skipped_lines = []
        if no_data:
            skipped_lines.append(f"  - нет истории за 90д: `{no_data}`")
        if insufficient:
            skipped_lines.append(f"  - мало видео для ритма: `{insufficient}`")
        if errors:
            skipped_lines.append(f"  - реальные ошибки: `{errors}`")
        if skipped_existing:
            skipped_lines.append(f"  - пропущено: `{skipped_existing}`")
        skipped_block = "\n".join(skipped_lines) if skipped_lines else "  - нет"
        text = (
            f"*✅ Адаптивный режим переобучен*\n\n"
            f"• Блогеров обработано: `{total}`\n"
            f"• Откалибровано: `{calibrated}`\n"
            f"• Не откалибровано сейчас: `{skipped}`\n"
            f"{skipped_block}\n"
            f"• Среднее видео на блогера: `{avg}`\n\n"
            f"Новые интервалы применятся на следующем цикле воркера."
        )
        await log_admin_action(
            callback.from_user.id,
            "adaptive_backfill",
            description=f"force backfill: total={total} calibrated={calibrated}",
        )
        kb = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="◀️ К настройкам", callback_data="admin:adaptive")],
        ])
        await _safe_edit_callback_text(callback, text, reply_markup=kb, parse_mode="Markdown")
    except Exception as e:
        logger.error("adaptive_backfill_run error: %s", e)
        # 409 от API = переобучение уже идёт (двойной клик / параллельный запуск)
        msg = str(e)
        if "409" in msg or "already running" in msg.lower():
            try:
                await callback.answer(
                    "Переобучение уже идёт. Подождите несколько минут.",
                    show_alert=True,
                )
            except Exception:
                pass
            return
        try:
            await callback.answer(f"Ошибка: {msg[:100]}", show_alert=True)
        except Exception:
            pass


# ══════════════════════════════════════════════════════════════════════════════
# ПРОКСИ
# ══════════════════════════════════════════════════════════════════════════════

def _proxy_status_emoji(status: str) -> str:
    return {"active": "✅", "slow": "🐢", "bad": "❌", "unknown": "❓"}.get(status, "❓")


@router.callback_query(F.data == "admin:proxies")
async def proxies_menu(callback: CallbackQuery, state: FSMContext):
    """Главное меню прокси — статистика + навигация."""
    try:
        await callback.answer()
        await state.clear()
        stats = await api_client.get_proxy_stats()
        total = int(stats.get("total", 0) or 0)
        active = int(stats.get("active", 0) or 0)
        slow = int(stats.get("slow", 0) or 0)
        bad = int(stats.get("bad", 0) or 0)
        unknown = int(stats.get("unknown", 0) or 0)

        text = (
            "🌐 *Прокси*\n"
            "━━━━━━━━━━━━━━━━━━━━\n\n"
            f"📦 Всего: `{total}`\n"
            f"✅ Активные: `{active}`\n"
            f"🐢 Медленные: `{slow}`\n"
            f"❌ Плохие: `{bad}`\n"
            f"❓ Неизвестные: `{unknown}`\n"
        )
        await _safe_edit_callback_text(callback, text, reply_markup=get_proxies_menu_keyboard(), parse_mode="Markdown")
    except Exception as e:
        logger.error("proxies_menu error: %s", e, exc_info=True)
        try:
            await callback.answer(f"Ошибка: {str(e)[:100]}", show_alert=True)
        except Exception:
            pass


@router.callback_query(F.data.in_({"admin:proxies:list", "admin:proxies:list:noop"}))
@router.callback_query(F.data.startswith("admin:proxies:list:page:"))
async def proxies_list(callback: CallbackQuery):
    """Список прокси с пагинацией (20 на страницу)."""
    try:
        await callback.answer()
        page = 0
        if callback.data and callback.data.startswith("admin:proxies:list:page:"):
            try:
                page = int(callback.data.rsplit(":", 1)[-1])
            except ValueError:
                page = 0

        per_page = 20
        proxies = await api_client.list_proxies(limit=per_page + 1, offset=page * per_page)
        has_next = len(proxies) > per_page
        page_items = proxies[:per_page]

        if not page_items and page == 0:
            await _safe_edit_callback_text(
                callback, "🌐 *Прокси*\n\n📭 Прокси не добавлены.",
                reply_markup=get_proxies_menu_keyboard(), parse_mode="Markdown",
            )
            return

        lines = [f"🌐 *Прокси* (стр. {page + 1})\n━━━━━━━━━━━━━━━━━━━━\n"]
        for p in page_items:
            proxy_str = str(p.get("proxy", ""))
            host_part = proxy_str.split("@")[-1] if "@" in proxy_str else proxy_str
            host_part = host_part[:40]
            country = p.get("country_code") or "—"
            mobile = "📱" if p.get("is_mobile") == "Y" else ""
            status = _proxy_status_emoji(p.get("status", "unknown"))
            lines.append(f"{status} `{host_part}` {country} {mobile}".rstrip())

        text = "\n".join(lines)
        kb = get_proxy_list_keyboard(page, has_prev=page > 0, has_next=has_next)
        await _safe_edit_callback_text(callback, text, reply_markup=kb, parse_mode="Markdown")
    except Exception as e:
        logger.error("proxies_list error: %s", e, exc_info=True)
        try:
            await callback.answer(f"Ошибка: {str(e)[:100]}", show_alert=True)
        except Exception:
            pass


@router.callback_query(F.data == "admin:proxies:validate")
async def proxies_validate(callback: CallbackQuery):
    """Проверяет все прокси и обновляет их статусы."""
    try:
        if not callback.message:
            await callback.answer()
            return
        await callback.message.edit_text("⏳ Проверяю прокси...")
        await callback.answer()

        result = await api_client.validate_proxies(reassign_bad_proxies=True)
        checked = int(result.get("checked", 0) or 0)
        active = int(result.get("active", 0) or 0)
        slow = int(result.get("slow", 0) or 0)
        bad = int(result.get("bad", 0) or 0)
        unknown = int(result.get("unknown", 0) or 0)
        errors = int(result.get("errors", 0) or 0)

        text = (
            "🔍 *Проверка прокси завершена*\n"
            "━━━━━━━━━━━━━━━━━━━━\n\n"
            f"📊 Проверено: `{checked}`\n"
            f"✅ Активные: `{active}`\n"
            f"🐢 Медленные: `{slow}`\n"
            f"❌ Плохие: `{bad}`\n"
            f"❓ Неизвестные: `{unknown}`\n"
            f"⚠️ Ошибки: `{errors}`\n"
        )
        kb = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="◀️ К прокси", callback_data="admin:proxies")],
        ])
        await callback.message.edit_text(text, reply_markup=kb, parse_mode="Markdown")
    except Exception as e:
        logger.error("proxies_validate error: %s", e, exc_info=True)
        try:
            if callback.message:
                await callback.message.edit_text(
                    f"❌ Ошибка проверки прокси: {str(e)[:200]}",
                    reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                        [InlineKeyboardButton(text="◀️ К прокси", callback_data="admin:proxies")]
                    ])
                )
        except Exception:
            pass


@router.callback_query(F.data == "admin:proxies:import")
async def proxies_import_start(callback: CallbackQuery, state: FSMContext):
    """Запускает FSM импорта прокси — просит прислать список."""
    try:
        if not callback.message:
            await callback.answer()
            return
        await callback.message.edit_text(
            "📥 *Импорт прокси*\n\n"
            "Отправьте список прокси — по одному на строке.\n\n"
            "Поддерживаемые форматы:\n"
            "`host:port`\n"
            "`host:port:user:pass`\n"
            "`user:pass@host:port`\n"
            "`socks5://host:port:user:pass`\n\n"
            "Мобильный прокси с ротацией:\n"
            "`host:port:user:pass rotation_url=https://...`",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="❌ Отмена", callback_data="admin:proxies")]
            ]),
            parse_mode="Markdown",
        )
        await state.set_state(OrderStates.INPUT_PROXIES_FILE)
        await callback.answer()
    except Exception as e:
        logger.error("proxies_import_start error: %s", e, exc_info=True)
        await callback.answer("Ошибка", show_alert=True)


@router.message(StateFilter(OrderStates.INPUT_PROXIES_FILE))
async def proxies_import_receive(message: Message, state: FSMContext):
    """Получает текст с прокси, парсит строки и показывает превью."""
    try:
        raw = (message.text or message.caption or "").strip()
        if not raw:
            await message.answer("❌ Сообщение пустое. Отправьте список прокси текстом.")
            return

        lines = [ln.strip() for ln in raw.splitlines() if ln.strip() and not ln.strip().startswith("#")]
        if not lines:
            await message.answer("❌ Не найдено ни одной строки с прокси.")
            return

        await state.update_data(proxy_lines=lines)

        # Короткое превью первых 5 строк
        preview = "\n".join(f"`{ln[:60]}`" for ln in lines[:5])
        suffix = f"\n…и ещё {len(lines) - 5}" if len(lines) > 5 else ""
        text = (
            f"📥 *Найдено {len(lines)} прокси*\n\n"
            f"{preview}{suffix}\n\n"
            "Импортировать?"
        )
        await message.answer(text, reply_markup=get_proxy_import_confirm_keyboard(), parse_mode="Markdown")
    except Exception as e:
        logger.error("proxies_import_receive error: %s", e, exc_info=True)
        await message.answer(f"❌ Ошибка обработки: {str(e)[:200]}")
        await state.clear()


@router.callback_query(F.data == "admin:proxies:import:confirm")
async def proxies_import_confirm(callback: CallbackQuery, state: FSMContext):
    """Выполняет импорт прокси из сохранённого в FSM списка."""
    try:
        data = await state.get_data()
        lines: list[str] = data.get("proxy_lines", [])
        await state.clear()

        if not lines:
            await callback.answer("Нет данных для импорта. Начните заново.", show_alert=True)
            return

        if callback.message:
            await callback.message.edit_text(f"⏳ Импортирую {len(lines)} прокси...")
        await callback.answer()

        result = await api_client.import_proxies(lines)
        inserted = int(result.get("inserted", 0) or 0)
        updated = int(result.get("updated", 0) or 0)
        total = int(result.get("total", 0) or 0)

        text = (
            "✅ *Импорт прокси завершён*\n"
            "━━━━━━━━━━━━━━━━━━━━\n\n"
            f"📦 Обработано: `{total}`\n"
            f"➕ Добавлено: `{inserted}`\n"
            f"🔄 Обновлено: `{updated}`\n"
        )
        await log_admin_action(
            admin_telegram_id=callback.from_user.id,
            action_type="import_proxies",
            details={"inserted": inserted, "updated": updated, "total": total},
            description=f"Импорт прокси: {inserted} новых, {updated} обновлено",
        )
        kb = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="◀️ К прокси", callback_data="admin:proxies")],
        ])
        if callback.message:
            await callback.message.edit_text(text, reply_markup=kb, parse_mode="Markdown")
    except Exception as e:
        logger.error("proxies_import_confirm error: %s", e, exc_info=True)
        await state.clear()
        try:
            if callback.message:
                await callback.message.edit_text(
                    f"❌ Ошибка импорта прокси: {str(e)[:200]}",
                    reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                        [InlineKeyboardButton(text="◀️ К прокси", callback_data="admin:proxies")]
                    ])
                )
        except Exception:
            pass


@router.callback_query(F.data == "admin:proxies:delete_all")
async def proxies_delete_all_confirm(callback: CallbackQuery):
    """Экран подтверждения удаления всех прокси."""
    try:
        await callback.answer()
        stats = await api_client.get_proxy_stats()
        total = int(stats.get("total", 0) or 0)
        await _safe_edit_callback_text(
            callback,
            f"🗑 *Удалить все прокси?*\n\n"
            f"Всего прокси: `{total}`\n\n"
            "У аккаунтов, привязанных к этим прокси, прокси будет сброшен.\n"
            "Действие необратимо.",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="✅ Да, удалить все", callback_data="admin:proxies:delete_all:confirm")],
                [InlineKeyboardButton(text="❌ Отмена", callback_data="admin:proxies")],
            ]),
            parse_mode="Markdown",
        )
    except Exception as e:
        logger.error("proxies_delete_all_confirm error: %s", e, exc_info=True)
        try:
            await callback.answer(f"Ошибка: {str(e)[:100]}", show_alert=True)
        except Exception:
            pass


@router.callback_query(F.data == "admin:proxies:delete_all:confirm")
async def proxies_delete_all_execute(callback: CallbackQuery):
    """Удаляет все прокси после подтверждения."""
    try:
        if callback.message:
            await callback.message.edit_text("⏳ Удаляю все прокси...")
        await callback.answer()

        result = await api_client.delete_all_proxies(clear_accounts=True)
        deleted = int(result.get("deleted", 0) or 0)

        await log_admin_action(
            admin_telegram_id=callback.from_user.id,
            action_type="delete_proxies",
            details={"scope": "all", "deleted": deleted},
            description=f"Удалены все прокси: {deleted} шт.",
        )
        kb = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="◀️ К прокси", callback_data="admin:proxies")],
        ])
        if callback.message:
            await callback.message.edit_text(
                f"✅ *Прокси удалены*\n\nУдалено: `{deleted}`",
                reply_markup=kb, parse_mode="Markdown",
            )
    except Exception as e:
        logger.error("proxies_delete_all_execute error: %s", e, exc_info=True)
        try:
            if callback.message:
                await callback.message.edit_text(
                    f"❌ Ошибка удаления прокси: {str(e)[:200]}",
                    reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                        [InlineKeyboardButton(text="◀️ К прокси", callback_data="admin:proxies")]
                    ])
                )
        except Exception:
            pass


# ══════════════════════════════════════════════════════════════════════════════
# РЕСУРСЫ
# ══════════════════════════════════════════════════════════════════════════════

@router.callback_query(F.data == "admin:resources")
async def resources_menu(callback: CallbackQuery):
    """Обзор ресурсов системы: аккаунты, прокси, активность за сегодня."""
    try:
        await callback.answer()
        stats = await api_client.get_resources_stats()
        acc = stats.get("accounts", {}) or {}
        prx = stats.get("proxies", {}) or {}
        today = stats.get("today", {}) or {}

        total = int(acc.get("total", 0) or 0)
        active = int(acc.get("active", 0) or 0)
        dead = int(acc.get("dead", 0) or 0)
        suspect = int(acc.get("suspect", 0) or 0)
        inactive = total - active - dead
        prx_total = int(prx.get("total", 0) or 0)
        comments = int(today.get("comments", 0) or 0)
        likes = int(today.get("likes", 0) or 0)
        errors = int(today.get("errors", 0) or 0)

        text = (
            "📊 *Ресурсы системы*\n"
            "━━━━━━━━━━━━━━━━━━━━\n\n"
            "🔑 *Аккаунты*\n"
            f"  Всего: `{total}` | Активные: `{active}`\n"
            f"  Мёртвые: `{dead}` | Неактивные: `{inactive}`\n"
            f"  Под подозрением: `{suspect}`\n\n"
            "🌐 *Прокси*\n"
            f"  Всего: `{prx_total}`\n\n"
            "📈 *Сегодня*\n"
            f"  Комментарии: `{comments}` | Лайки: `{likes}` | Ошибки: `{errors}`\n"
        )
        await _safe_edit_callback_text(callback, text, reply_markup=get_resources_keyboard(), parse_mode="Markdown")
    except Exception as e:
        logger.error("resources_menu error: %s", e, exc_info=True)
        try:
            await callback.answer(f"Ошибка: {str(e)[:100]}", show_alert=True)
        except Exception:
            pass


@router.callback_query(F.data == "admin:resources:dead:accounts")
async def resources_dead_accounts(callback: CallbackQuery):
    """Список мёртвых аккаунтов с причинами."""
    try:
        await callback.answer()
        dead_list = await api_client.get_dead_accounts()
        if not dead_list:
            await _safe_edit_callback_text(
                callback,
                "💀 *Мёртвые аккаунты*\n\n✅ Мёртвых аккаунтов нет.",
                reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                    [InlineKeyboardButton(text="◀️ К ресурсам", callback_data="admin:resources")],
                ]),
                parse_mode="Markdown",
            )
            return

        lines = [f"💀 *Мёртвые аккаунты* — {len(dead_list)} шт.\n━━━━━━━━━━━━━━━━━━━━\n"]
        for acc in dead_list[:20]:
            username = str(acc.get("username") or acc.get("id") or "?")[:25]
            reason = str(acc.get("last_error") or acc.get("error_reason") or "—")[:50]
            lines.append(f"• `{username}` — {reason}")

        if len(dead_list) > 20:
            lines.append(f"\n…и ещё {len(dead_list) - 20} аккаунтов")

        text = "\n".join(lines)
        kb = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="🗑 Удалить мёртвые", callback_data="admin:resources:clean:accounts")],
            [InlineKeyboardButton(text="◀️ К ресурсам", callback_data="admin:resources")],
        ])
        await _safe_edit_callback_text(callback, text, reply_markup=kb, parse_mode="Markdown")
    except Exception as e:
        logger.error("resources_dead_accounts error: %s", e, exc_info=True)
        try:
            await callback.answer(f"Ошибка: {str(e)[:100]}", show_alert=True)
        except Exception:
            pass


@router.callback_query(F.data == "admin:resources:clean:accounts")
async def resources_clean_accounts_confirm(callback: CallbackQuery):
    """Подтверждение удаления мёртвых аккаунтов."""
    try:
        await callback.answer()
        dead_list = await api_client.get_dead_accounts()
        count = len(dead_list) if isinstance(dead_list, list) else 0
        await _safe_edit_callback_text(
            callback,
            f"🗑 *Удалить мёртвые аккаунты?*\n\n"
            f"Будет удалено: `{count}` аккаунтов\n\n"
            "Действие необратимо.",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="✅ Да, удалить", callback_data="admin:resources:clean:accounts:confirm")],
                [InlineKeyboardButton(text="❌ Отмена", callback_data="admin:resources:dead:accounts")],
            ]),
            parse_mode="Markdown",
        )
    except Exception as e:
        logger.error("resources_clean_accounts_confirm error: %s", e, exc_info=True)
        try:
            await callback.answer(f"Ошибка: {str(e)[:100]}", show_alert=True)
        except Exception:
            pass


@router.callback_query(F.data == "admin:resources:clean:accounts:confirm")
async def resources_clean_accounts_execute(callback: CallbackQuery):
    """Удаляет всех мёртвых аккаунтов."""
    try:
        if callback.message:
            await callback.message.edit_text("⏳ Удаляю мёртвые аккаунты...")
        await callback.answer()

        result = await api_client.delete_dead_accounts()
        deleted = int(result.get("deleted_count", 0) or result.get("deleted", 0) or 0)

        await log_admin_action(
            admin_telegram_id=callback.from_user.id,
            action_type="delete_dead_accounts",
            details={"deleted": deleted},
            description=f"Удалены мёртвые аккаунты: {deleted} шт.",
        )
        kb = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="◀️ К ресурсам", callback_data="admin:resources")],
        ])
        if callback.message:
            await callback.message.edit_text(
                f"✅ *Готово*\n\nУдалено мёртвых аккаунтов: `{deleted}`",
                reply_markup=kb, parse_mode="Markdown",
            )
    except Exception as e:
        logger.error("resources_clean_accounts_execute error: %s", e, exc_info=True)
        try:
            if callback.message:
                await callback.message.edit_text(
                    f"❌ Ошибка: {str(e)[:200]}",
                    reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                        [InlineKeyboardButton(text="◀️ К ресурсам", callback_data="admin:resources")]
                    ])
                )
        except Exception:
            pass


# ══════════════════════════════════════════════════════════════════════════════
# ГЕО
# ══════════════════════════════════════════════════════════════════════════════

@router.callback_query(F.data == "admin:geo")
async def geo_menu(callback: CallbackQuery):
    """Главное меню гео-аналитики."""
    try:
        await callback.answer()
        await _safe_edit_callback_text(
            callback,
            "🌍 *Гео-аналитика*\n"
            "━━━━━━━━━━━━━━━━━━━━\n\n"
            "Статистика по странам, покрытие аккаунтами и прокси, рекомендации.",
            reply_markup=get_geo_menu_keyboard(),
            parse_mode="Markdown",
        )
    except Exception as e:
        logger.error("geo_menu error: %s", e, exc_info=True)
        try:
            await callback.answer(f"Ошибка: {str(e)[:100]}", show_alert=True)
        except Exception:
            pass


@router.callback_query(F.data == "admin:geo:stats")
async def geo_stats(callback: CallbackQuery):
    """Статистика эффективности по странам за 7 дней."""
    try:
        await callback.answer()
        data = await api_client.get_geo_stats(days=7)
        countries = data.get("countries", []) or []
        summary = data.get("summary", {}) or {}

        if not countries:
            await _safe_edit_callback_text(
                callback,
                "📊 *Гео-статистика*\n\nДанных пока нет — нужны выполненные задачи с аккаунтами.",
                reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                    [InlineKeyboardButton(text="◀️ К гео", callback_data="admin:geo")]
                ]),
                parse_mode="Markdown",
            )
            return

        lines = ["📊 *Статистика по странам (7д)*\n━━━━━━━━━━━━━━━━━━━━\n"]
        for c in sorted(countries, key=lambda x: x.get("success_rate", 0), reverse=True)[:15]:
            code = c.get("country_code", "??")
            acc = int(c.get("accounts_count", 0) or 0)
            rate = c.get("success_rate")
            rate_str = f"{rate*100:.0f}%" if rate is not None else "—"
            status_map = {"optimal": "✅", "good": "🟢", "fair": "🟡", "poor": "🔴"}
            status_emoji = status_map.get(c.get("status", ""), "❓")
            lines.append(f"{status_emoji} `{code}` — {acc} акк. | SR: {rate_str}")

        best = summary.get("best_country", "—")
        worst = summary.get("worst_country", "—")
        avg_sr = summary.get("avg_success_rate")
        avg_str = f"{avg_sr*100:.1f}%" if avg_sr is not None else "—"
        lines.append(f"\n🏆 Лучшая: `{best}` | 📉 Худшая: `{worst}` | Средний SR: {avg_str}")

        text = "\n".join(lines)
        kb = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="◀️ К гео", callback_data="admin:geo")],
        ])
        await _safe_edit_callback_text(callback, text, reply_markup=kb, parse_mode="Markdown")
    except Exception as e:
        logger.error("geo_stats error: %s", e, exc_info=True)
        try:
            await callback.answer(f"Ошибка: {str(e)[:100]}", show_alert=True)
        except Exception:
            pass


@router.callback_query(F.data == "admin:geo:coverage")
async def geo_coverage(callback: CallbackQuery):
    """Анализ покрытия гео аккаунтами и прокси."""
    try:
        await callback.answer()
        data = await api_client.get_geo_coverage()
        countries = data.get("countries", []) or data.get("coverage", []) or []
        total_accounts = int(data.get("total_accounts", 0) or 0)
        total_proxies = int(data.get("total_proxies", 0) or 0)

        lines = [
            "📈 *Покрытие по гео*\n━━━━━━━━━━━━━━━━━━━━\n",
            f"🔑 Аккаунтов всего: `{total_accounts}`\n🌐 Прокси всего: `{total_proxies}`\n",
        ]
        if countries:
            lines.append("*По странам:*")
            for c in countries[:15]:
                code = c.get("country_code") or c.get("country", "??")
                acc_c = int(c.get("accounts", 0) or c.get("accounts_count", 0) or 0)
                prx_c = int(c.get("proxies", 0) or c.get("proxies_count", 0) or 0)
                lines.append(f"• `{code}` — {acc_c} акк. / {prx_c} прокси")
        else:
            lines.append("Данные по странам недоступны.")

        text = "\n".join(lines)
        kb = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="◀️ К гео", callback_data="admin:geo")],
        ])
        await _safe_edit_callback_text(callback, text, reply_markup=kb, parse_mode="Markdown")
    except Exception as e:
        logger.error("geo_coverage error: %s", e, exc_info=True)
        try:
            await callback.answer(f"Ошибка: {str(e)[:100]}", show_alert=True)
        except Exception:
            pass


@router.callback_query(F.data == "admin:geo:recommendation")
async def geo_recommendation(callback: CallbackQuery):
    """Рекомендации по географии — чего не хватает для нормальной работы."""
    try:
        await callback.answer()
        data = await api_client.get_geo_recommendation(required_accounts=10)
        recs = data.get("recommendations", []) or data.get("missing", []) or []
        summary_text = data.get("summary") or data.get("message") or ""

        lines = ["💡 *Рекомендации по гео*\n━━━━━━━━━━━━━━━━━━━━\n"]
        if summary_text:
            lines.append(f"_{escape_markdown_simple(str(summary_text)[:300])}_\n")
        if recs:
            for r in recs[:10]:
                if isinstance(r, dict):
                    code = r.get("country_code") or r.get("country", "??")
                    need = r.get("needed", r.get("deficit", r.get("required", "?")))
                    have = r.get("available", r.get("current", "?"))
                    lines.append(f"• `{code}` — нужно ещё: `{need}` (есть: `{have}`)")
                else:
                    lines.append(f"• {str(r)[:80]}")
        elif not summary_text:
            lines.append("Рекомендаций нет — покрытие достаточное.")

        text = "\n".join(lines)
        kb = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="◀️ К гео", callback_data="admin:geo")],
        ])
        await _safe_edit_callback_text(callback, text, reply_markup=kb, parse_mode="Markdown")
    except Exception as e:
        logger.error("geo_recommendation error: %s", e, exc_info=True)
        try:
            await callback.answer(f"Ошибка: {str(e)[:100]}", show_alert=True)
        except Exception:
            pass


@router.callback_query(F.data == "admin:geo:summary")
async def geo_summary(callback: CallbackQuery):
    """Сводка: покрытие + топ стран + быстрый статус."""
    try:
        await callback.answer()
        coverage_data, stats_data = await asyncio.gather(
            api_client.get_geo_coverage(),
            api_client.get_geo_stats(days=7),
            return_exceptions=True,
        )
        if isinstance(coverage_data, Exception):
            logger.warning("geo_summary: coverage fetch failed: %s", coverage_data)
        if isinstance(stats_data, Exception):
            logger.warning("geo_summary: stats fetch failed: %s", stats_data)
        lines = ["📋 *Гео-сводка*\n━━━━━━━━━━━━━━━━━━━━\n"]

        if isinstance(coverage_data, dict):
            total_acc = int(coverage_data.get("total_accounts", 0) or 0)
            total_prx = int(coverage_data.get("total_proxies", 0) or 0)
            lines.append(f"🔑 Аккаунты: `{total_acc}` | 🌐 Прокси: `{total_prx}`")
            countries_cov = coverage_data.get("countries", []) or []
            if countries_cov:
                lines.append(f"🗺 Стран с покрытием: `{len(countries_cov)}`")

        if isinstance(stats_data, dict):
            countries_stat = stats_data.get("countries", []) or []
            good = [c for c in countries_stat if c.get("status") in ("optimal", "good")]
            poor = [c for c in countries_stat if c.get("status") in ("poor",)]
            if countries_stat:
                lines.append(f"\n📊 Эффективность (7д): `{len(countries_stat)}` стран")
                lines.append(f"  ✅ Хорошо: `{len(good)}` | 🔴 Плохо: `{len(poor)}`")

        lines.append("\nДля деталей — используй разделы выше.")
        text = "\n".join(lines)
        kb = InlineKeyboardMarkup(inline_keyboard=[
            [
                InlineKeyboardButton(text="📊 Статистика", callback_data="admin:geo:stats"),
                InlineKeyboardButton(text="📈 Покрытие", callback_data="admin:geo:coverage"),
            ],
            [InlineKeyboardButton(text="◀️ К гео", callback_data="admin:geo")],
        ])
        await _safe_edit_callback_text(callback, text, reply_markup=kb, parse_mode="Markdown")
    except Exception as e:
        logger.error("geo_summary error: %s", e, exc_info=True)
        try:
            await callback.answer(f"Ошибка: {str(e)[:100]}", show_alert=True)
        except Exception:
            pass


@router.callback_query(F.data == "admin:geo:detect")
async def geo_detect(callback: CallbackQuery):
    """Запускает определение гео для всех аккаунтов и прокси без гео."""
    try:
        if callback.message:
            await callback.message.edit_text("⏳ Определяю гео...")
        await callback.answer()

        result = await api_client.detect_all_geo()
        if isinstance(result, dict) and result.get("error"):
            await callback.answer(f"⚠️ {result['error'][:100]}", show_alert=True)
            if callback.message:
                await callback.message.edit_text(
                    f"⚠️ {result['error']}",
                    reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                        [InlineKeyboardButton(text="◀️ К гео", callback_data="admin:geo")]
                    ])
                )
            return

        acc = result.get("accounts", {}) or {}
        prx = result.get("proxies", {}) or {}
        text = (
            "🌍 *Определение гео завершено*\n"
            "━━━━━━━━━━━━━━━━━━━━\n\n"
            "*Аккаунты*\n"
            f"  Всего: `{acc.get('total', 0)}` | Определено: `{acc.get('detected', 0)}` | Ошибки: `{acc.get('failed', 0)}`\n\n"
            "*Прокси*\n"
            f"  Всего: `{prx.get('total', 0)}` | Определено: `{prx.get('detected', 0)}` | Ошибки: `{prx.get('failed', 0)}`\n"
        )
        await log_admin_action(
            admin_telegram_id=callback.from_user.id,
            action_type="geo_detect",
            details={"accounts": acc, "proxies": prx},
            description="Запуск определения гео",
        )
        kb = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="◀️ К гео", callback_data="admin:geo")],
        ])
        if callback.message:
            await callback.message.edit_text(text, reply_markup=kb, parse_mode="Markdown")
    except Exception as e:
        logger.error("geo_detect error: %s", e, exc_info=True)
        try:
            if callback.message:
                await callback.message.edit_text(
                    f"❌ Ошибка: {str(e)[:200]}",
                    reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                        [InlineKeyboardButton(text="◀️ К гео", callback_data="admin:geo")]
                    ])
                )
        except Exception:
            pass


@router.callback_query(F.data == "admin:geo:settings")
async def geo_settings(callback: CallbackQuery):
    """Настройки гео — перенаправляет к настройкам прокси (тип и режим)."""
    try:
        await callback.answer()
        await _safe_edit_callback_text(
            callback,
            "⚙️ *Настройки гео*\n\n"
            "Гео-фильтрация управляется через раздел *Прокси-режим*:\n"
            "• Тип прокси (mobile / residential / none)\n"
            "• Предпочтение мобильных прокси\n\n"
            "Нажмите ниже, чтобы перейти к настройкам.",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="⚙️ Прокси-режим", callback_data="admin:proxy_settings")],
                [InlineKeyboardButton(text="◀️ К гео", callback_data="admin:geo")],
            ]),
            parse_mode="Markdown",
        )
    except Exception as e:
        logger.error("geo_settings error: %s", e, exc_info=True)
        try:
            await callback.answer(f"Ошибка: {str(e)[:100]}", show_alert=True)
        except Exception:
            pass
