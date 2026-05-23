"""
Handlers для раздела Парсинг видео (мониторинг блогеров TikTok).
"""
import asyncio

from aiogram import Router, F
from aiogram.types import CallbackQuery, Message, InlineKeyboardMarkup, InlineKeyboardButton
from aiogram.fsm.context import FSMContext
from aiogram.filters import StateFilter
from aiogram.exceptions import TelegramBadRequest
from bot.services.api_client import api_client, APIRequestError
from bot.services.formatting import Emoji, escape_markdown, markdown_handle, markdown_inline_code, markdown_to_plain_text
from bot.services.user_utils import get_user_backend_id
from bot.services.software_mode import is_parsing_mode
from bot.services.template_utils import filter_full_templates
from bot.services.video_display import (
    format_video_markdown_line,
    normalize_video_url,
    video_id_link,
    video_short_id,
    video_time_label,
)
from bot.keyboards.video_parsing import (
    get_video_parsing_menu_keyboard,
    get_creator_keyboard,
    get_creators_list_keyboard,
    get_bulk_actions_keyboard,
    get_bulk_scopes_keyboard,
    get_bulk_template_keyboard,
    get_bulk_interval_keyboard,
    get_bulk_confirm_keyboard,
    get_mode_selection_keyboard,
    get_review_video_keyboard,
    get_review_queue_keyboard,
)
from bot.keyboards.admin import get_admin_menu_keyboard
from bot.state.video_parsing_states import VideoParsingStates
import logging
import re
from datetime import datetime

import time as _time

logger = logging.getLogger("bot.video_parsing")
router = Router()

CREATORS_LIST_PER_PAGE = 8
CREATOR_SEARCH_PER_PAGE = 10
SUGGESTIONS_PER_PAGE = 8
CREATOR_TEMPLATE_PICKER_PER_PAGE = 8
CREATOR_SUGGESTIONS_API_LIMIT = 100
CREATOR_BULK_CREATE_DELAY_SECONDS = 1.15
CREATOR_BULK_CREATE_MAX_ATTEMPTS = 3
ADD_CREATOR_INTERVAL_PRESETS = (5, 15, 30, 60, 120)
MIN_CREATOR_INTERVAL_MINUTES = 5
MAX_CREATOR_INTERVAL_MINUTES = 240
# Per-user cooldown for "Запустить сейчас" — key: (user_id, creator_id), value: last_run_ts
_RUN_NOW_COOLDOWN_SECONDS = 60
_run_now_last: dict[tuple[int, int], float] = {}

BULK_ACTION_LABELS = {
    "auto_tpl": "включить авто-комментинг и назначить шаблон",
    "set_tpl": "назначить шаблон",
    "manual": "перевести в ручную проверку",
    "notify": "перевести в уведомления",
    "clear_tpl": "убрать шаблон авто-коммента",
    "enable": "включить мониторинг",
    "disable": "выключить мониторинг",
    "interval": "изменить интервал проверки",
    "run_now": "проверить сейчас",
}
BULK_SCOPE_LABELS = {
    "all": "все блогеры",
    "enabled": "только включенные",
    "disabled": "только выключенные",
    "auto_comment": "уже в авто-комментинге",
    "manual_review": "ручная проверка",
    "notify_only": "только уведомления",
    "without_default_template": "без шаблона",
    "with_new_videos": "с новыми видео",
}
BULK_TEMPLATE_ACTIONS = {"auto_tpl", "set_tpl"}
BULK_INTERVAL_ACTIONS = {"interval"}
BULK_SKIP_REASON_LABELS = {
    "already_matching": "уже настроены так",
    "auto_comment_requires_default_template": "для авто нужен шаблон",
    "disabled_creator_not_queued": "выключенные не ставились на проверку",
}


def _is_creator_quota_error(error_msg: str) -> bool:
    normalized = (error_msg or "").lower()
    return (
        "creator quota exceeded" in normalized
        or "unique creator quota exceeded" in normalized
        or ("maximum" in normalized and "creators" in normalized and "you have" in normalized)
        or ("maximum" in normalized and "unique creators" in normalized)
        or "лимит блогеров" in normalized
    )

def _is_rate_limit_error(error_msg: str) -> bool:
    normalized = (error_msg or "").lower()
    return (
        "rate limit" in normalized
        or "too many requests" in normalized
        or "429" in normalized
    )


def _format_creator_quota_error(error_msg: str) -> str:
    if "unique creator quota exceeded" in (error_msg or "").lower():
        max_match = re.search(r"maximum\s+(\d+)", error_msg or "", re.IGNORECASE)
        current_match = re.search(r"current unique creators:\s*(\d+)", error_msg or "", re.IGNORECASE)
        maximum = max_match.group(1) if max_match else "50"
        current = current_match.group(1) if current_match else maximum
        return (
            f"❌ Достигнут общий лимит блогеров в системе: {current}/{maximum}.\n\n"
            "Добавление новых блогеров временно недоступно. Обратитесь к администратору."
        )

    maximum = None
    current = None
    max_match = re.search(r"maximum\s+(\d+)", error_msg or "", re.IGNORECASE)
    have_match = re.search(r"you have\s+(\d+)", error_msg or "", re.IGNORECASE)
    if max_match:
        maximum = max_match.group(1)
    if have_match:
        current = have_match.group(1)

    if maximum and current:
        return (
            f"❌ Достигнут лимит блогеров: {current}/{maximum}.\n\n"
            "Удалите лишних блогеров из списка или обратитесь к администратору."
        )

    return (
        "❌ Достигнут лимит блогеров.\n\n"
        "Удалите лишних блогеров из списка или обратитесь к администратору."
    )


def _clamp_page(page: int, total: int, per_page: int) -> int:
    max_page = max(0, (max(0, total) - 1) // max(1, per_page)) if total else 0
    return max(0, min(page, max_page))


def _short_username(username: object, max_len: int = 28) -> str:
    value = str(username or "unknown").strip().lstrip("@") or "unknown"
    if len(value) <= max_len:
        return value
    return value[: max(1, max_len - 1)] + "…"


def _format_last_check(value: str | None) -> str:
    if not value:
        return "никогда"
    try:
        dt = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        return dt.strftime("%d.%m %H:%M")
    except Exception:
        return "недавно"


def _mode_short(mode: str | None) -> str:
    return {
        "auto_comment": "авто",
        "manual_review": "ручн.",
        "notify_only": "увед.",
    }.get(mode or "", mode or "ручн.")


def _plural_ru(count: int, one: str, few: str, many: str) -> str:
    n = abs(int(count))
    if 11 <= n % 100 <= 14:
        return many
    if n % 10 == 1:
        return one
    if 2 <= n % 10 <= 4:
        return few
    return many


async def _get_video_notifications_enabled(user_id: int) -> bool:
    try:
        settings = await api_client.get_video_notifications(user_id)
        return bool(settings.get("enabled", True))
    except Exception as exc:
        logger.warning("Failed to load video notification settings for user %s: %s", user_id, exc)
        return True


def _build_creators_page_message(
    creators: list[dict],
    page: int = 0,
    adaptive_enabled: bool = False,
    parsing_mode: bool = False,
) -> tuple[str, int]:
    total = len(creators)
    page = _clamp_page(page, total, CREATORS_LIST_PER_PAGE)
    start_idx = page * CREATORS_LIST_PER_PAGE
    end_idx = min(start_idx + CREATORS_LIST_PER_PAGE, total)
    page_creators = creators[start_idx:end_idx]

    enabled_count = sum(1 for creator in creators if creator.get("enabled"))
    new_videos_count = sum(int(creator.get("new_videos_count", 0) or 0) for creator in creators)

    message = "📋 *Ваши блогеры*\n\n"
    if adaptive_enabled:
        message += "_🧠 Бот сам подбирает частоту проверок под каждого блогера._\n\n"
    message += f"Показано *{start_idx + 1}-{end_idx}* из *{total}*"
    message += f" · включено *{enabled_count}*"
    if new_videos_count:
        message += f" · новых видео *{new_videos_count}*"
    message += "\n\n"

    for number, creator in enumerate(page_creators, start=start_idx + 1):
        status = "✅" if creator.get("enabled") else "⏸"
        username = markdown_handle(creator.get("username", "unknown"))
        # In parsing-mode the only meaningful mode is "уведомления" — the DB
        # row may still hold an old auto_comment/manual_review value, but the
        # bot ignores it. Show "увед." so the UI stops contradicting reality.
        mode = "увед." if parsing_mode else _mode_short(creator.get("mode"))
        last_check = _format_last_check(creator.get("last_checked_at"))
        creator_new_count = int(creator.get("new_videos_count", 0) or 0)
        if adaptive_enabled:
            interval_str = "🧠 авто"
        else:
            interval_int = int(creator.get("interval_minutes", 30) or 30)
            interval_str = f"{interval_int}м"

        message += f"{number}. {status} {username} · {interval_str} · `{mode}`"
        if creator_new_count:
            message += f" · 🆕 {creator_new_count}"
        message += f"\n   проверка: {last_check}\n"

    message += "\nКнопки ниже открывают карточку выбранного блогера."
    return message, page


def _filter_creators_by_query(creators: list[dict], query: str) -> list[dict]:
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


def _creator_search_result_keyboard(
    matches: list[dict],
    *,
    page: int = 0,
    per_page: int = CREATOR_SEARCH_PER_PAGE,
    limit: int | None = None,
) -> InlineKeyboardMarkup:
    if limit is not None:
        page = 0
        per_page = max(1, int(limit))
    page = _clamp_page(page, len(matches), per_page)
    start = page * per_page
    end = min(start + per_page, len(matches))
    rows: list[list[InlineKeyboardButton]] = []
    for creator in matches[start:end]:
        status = "✅" if creator.get("enabled") else "⏸"
        username = str(creator.get("username") or "unknown").strip().lstrip("@") or "unknown"
        new_count = int(creator.get("new_videos_count") or 0)
        mode = _mode_short(creator.get("mode"))
        text = f"{status} @{_short_username(username, 24)} · {mode}"
        if new_count:
            text += f" · 🆕 {new_count}"
        rows.append([
            InlineKeyboardButton(
                text=text,
                callback_data=f"vp:creator:{creator['id']}:detail",
            )
        ])

    max_page = max(0, (len(matches) - 1) // max(1, per_page)) if matches else 0
    nav_row: list[InlineKeyboardButton] = []
    if page > 0:
        nav_row.append(InlineKeyboardButton(text="◀️", callback_data=f"vp:list:search:page:{page - 1}"))
    if max_page > 0:
        nav_row.append(InlineKeyboardButton(text=f"{page + 1}/{max_page + 1}", callback_data="vp:list:search:noop"))
    if page < max_page:
        nav_row.append(InlineKeyboardButton(text="▶️", callback_data=f"vp:list:search:page:{page + 1}"))
    if nav_row:
        rows.append(nav_row)

    rows.append([
        InlineKeyboardButton(text="🔍 Искать ещё", callback_data="vp:list:search"),
        InlineKeyboardButton(text="👤 К списку", callback_data="vp:list"),
    ])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def _build_creator_search_result_message(
    matches: list[dict],
    query: str,
    *,
    page: int = 0,
    per_page: int = CREATOR_SEARCH_PER_PAGE,
) -> tuple[str, InlineKeyboardMarkup, int]:
    page = _clamp_page(page, len(matches), per_page)
    start = page * per_page
    end = min(start + per_page, len(matches))
    safe_query = markdown_inline_code(query[:80])
    lines = [
        f"🔍 *Найдено: {len(matches)}* по запросу {safe_query}",
        f"Показано `{start + 1}-{end}` из `{len(matches)}`.",
        "",
        "Нажмите на блогера ниже, чтобы открыть карточку.",
    ]
    return "\n".join(lines), _creator_search_result_keyboard(matches, page=page, per_page=per_page), page


def _get_add_interval_keyboard() -> InlineKeyboardMarkup:
    rows: list[list[InlineKeyboardButton]] = []
    rows.append(
        [
            InlineKeyboardButton(text=f"{minutes} мин", callback_data=f"vp:add_interval:{minutes}")
            for minutes in ADD_CREATOR_INTERVAL_PRESETS[:3]
        ]
    )
    rows.append(
        [
            InlineKeyboardButton(text=f"{minutes} мин", callback_data=f"vp:add_interval:{minutes}")
            for minutes in ADD_CREATOR_INTERVAL_PRESETS[3:]
        ]
    )
    rows.append([InlineKeyboardButton(text="⬅️ Назад", callback_data="vp:add")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


async def _render_creators_list(callback: CallbackQuery, page: int = 0, *, answer: bool = True) -> None:
    if answer:
        try:
            await callback.answer()
        except Exception:
            pass

    if not callback.message:
        await callback.answer("Ошибка: сообщение не найдено", show_alert=True)
        return

    user_id = await get_user_backend_id(callback.from_user.id)
    if not user_id:
        await callback.answer("❌ Ошибка: пользователь не найден", show_alert=True)
        return

    creators = await api_client.list_video_creators(user_id)
    if not creators:
        message = (
            "📋 *Блогеры*\n\n"
            "Список пока пуст.\n"
            "Добавьте первого блогера, чтобы мониторинг начал ловить новые видео."
        )
        await safe_edit_text(
            callback.message,
            message,
            reply_markup=get_video_parsing_menu_keyboard(parsing_mode=await is_parsing_mode(telegram_id=callback.from_user.id)),
            parse_mode="Markdown",
        )
        return

    adaptive_enabled = False
    try:
        adaptive_settings = await api_client.get_adaptive_settings()
        adaptive_enabled = bool(adaptive_settings.get("adaptive_enabled"))
    except Exception:
        adaptive_enabled = False
    parsing_mode_now = await is_parsing_mode(telegram_id=callback.from_user.id)
    message, page = _build_creators_page_message(
        creators,
        page,
        adaptive_enabled=adaptive_enabled,
        parsing_mode=parsing_mode_now,
    )
    await safe_edit_text(
        callback.message,
        message,
        reply_markup=get_creators_list_keyboard(
            creators,
            page=page,
            per_page=CREATORS_LIST_PER_PAGE,
            adaptive_enabled=adaptive_enabled,
            parsing_mode=parsing_mode_now,
        ),
        parse_mode="Markdown",
    )


async def safe_edit_text(message, text: str, **kwargs):
    """Безопасно редактирует сообщение, игнорируя ошибку 'message is not modified'."""
    try:
        await message.edit_text(text, **kwargs)
    except TelegramBadRequest as e:
        if "message is not modified" in str(e):
            logger.debug(f"Message not modified (same content), ignoring")
        else:
            if kwargs.get("parse_mode") == "Markdown":
                plain_kwargs = dict(kwargs)
                plain_kwargs.pop("parse_mode", None)
                try:
                    await message.edit_text(markdown_to_plain_text(text), **plain_kwargs)
                    return
                except TelegramBadRequest:
                    pass
            raise


async def _deny_bulk_actions_in_parsing_mode(callback: CallbackQuery) -> bool:
    """Return True after blocking bulk actions that are hidden in parsing mode."""
    if not await is_parsing_mode(telegram_id=callback.from_user.id):
        return False

    await callback.answer("Массовые действия доступны только в полном режиме", show_alert=True)
    if callback.message:
        try:
            await safe_edit_text(
                callback.message,
                "📥 *Режим парсинга видео*\n\n"
                "Массовые действия скрыты в этом режиме. Можно смотреть входящие видео, "
                "управлять отдельными блогерами и уведомлениями.",
                reply_markup=get_video_parsing_menu_keyboard(parsing_mode=True),
                parse_mode="Markdown",
            )
        except Exception:
            logger.debug("Failed to redraw parsing menu after blocked bulk callback", exc_info=True)
    return True


@router.callback_query(F.data == "menu:video_parsing")
async def show_video_parsing_menu_from_main(callback: CallbackQuery):
    """P1 FIX: Главное меню парсинга видео с актуальной статистикой."""
    try:
        await callback.answer()
        
        # Получаем статистику для меню
        user_id = await get_user_backend_id(callback.from_user.id)
        if not user_id:
            await callback.answer("❌ Ошибка: пользователь не найден", show_alert=True)
            return
        
        # Получаем статистику Inbox
        parsing_mode = await is_parsing_mode(telegram_id=callback.from_user.id)
        notifications_enabled = await _get_video_notifications_enabled(user_id)
        inbox_stats = {} if parsing_mode else await api_client.get_inbox_stats(user_id)
        new_count = inbox_stats.get("new", 0)
        
        # Получаем статистику блогеров
        creators = await api_client.list_video_creators(user_id)
        total_creators = len(creators)
        enabled_creators = sum(1 for c in creators if c.get("enabled"))
        autopilot_count = sum(1 for c in creators if c.get("mode") == "auto_comment" and c.get("enabled"))
        parsed_new_count = sum(int(c.get("new_videos_count", 0) or 0) for c in creators)
        menu_new_count = parsed_new_count if parsing_mode else new_count
        
        # Получаем информацию о последней проверке
        last_check_text = "никогда"
        for creator in creators:
            last_check = creator.get("last_checked_at")
            if last_check:
                try:
                    dt = datetime.fromisoformat(last_check.replace('Z', '+00:00'))
                    from datetime import timezone, timedelta
                    now = datetime.now(timezone.utc)
                    diff = now - dt
                    if diff < timedelta(minutes=1):
                        last_check_text = "только что"
                    elif diff < timedelta(hours=1):
                        last_check_text = f"{int(diff.total_seconds() / 60)} мин назад"
                    else:
                        last_check_text = dt.strftime("%d.%m %H:%M")
                    break
                except Exception:
                    pass
        
        message = "🎬 *Мониторинг видео*\n\n"
        if parsing_mode:
            message += f"📥 *Новые видео:* {parsed_new_count}\n"
        else:
            message += f"📥 *Входящие:* {new_count} новых видео\n"
        message += f"👤 *Блогеров:* {total_creators} (вкл: {enabled_creators})\n"
        if autopilot_count > 0 and not parsing_mode:
            message += f"🤖 *Автопилот:* {autopilot_count} {_plural_ru(autopilot_count, 'блогер', 'блогера', 'блогеров')}\n"
        message += (
            "🔔 *Уведы:* включены\n"
            if notifications_enabled
            else "🔕 *Уведы:* выключены\n"
        )
        message += f"⏱ *Последняя проверка:* {last_check_text}\n\n"
        if parsing_mode:
            message += "🔔 *Режим:* уведомления без заказов комментариев.\n\n"
        message += "Выберите действие ниже."
        
        await safe_edit_text(
            callback.message,
            message,
            reply_markup=get_video_parsing_menu_keyboard(
                new_count=menu_new_count,
                total_creators=total_creators,
                enabled_creators=enabled_creators,
                autopilot_count=autopilot_count,
                parsing_mode=parsing_mode,
                notifications_enabled=notifications_enabled,
            ),
            parse_mode="Markdown"
        )
    except Exception as e:
        logger.error(f"Error showing video parsing menu: {e}", exc_info=True)
        await callback.answer("Ошибка при загрузке меню", show_alert=True)


@router.callback_query(F.data == "admin:video_parsing")
async def show_video_parsing_menu(callback: CallbackQuery):
    """Показывает главное меню раздела Парсинг видео (из админ-меню, для обратной совместимости)."""
    # Перенаправляем на тот же обработчик
    await show_video_parsing_menu_from_main(callback)


@router.callback_query(F.data == "vp:menu")
async def back_to_video_parsing_menu(callback: CallbackQuery):
    """Возврат в главное меню."""
    await show_video_parsing_menu(callback)


@router.callback_query(F.data == "vp:notifications:toggle")
async def toggle_video_notifications(callback: CallbackQuery):
    """Toggles all new-video Telegram notifications for the current user."""
    try:
        user_id = await get_user_backend_id(callback.from_user.id)
        if not user_id:
            await callback.answer("❌ Ошибка: пользователь не найден", show_alert=True)
            return

        current = await api_client.get_video_notifications(user_id)
        new_value = not bool(current.get("enabled", True))
        await api_client.update_video_notifications(user_id, new_value)
        await callback.answer(
            "🔔 Уведы включены" if new_value else "🔕 Уведы выключены",
            show_alert=False,
        )
        await show_video_parsing_menu_from_main(callback)
    except Exception as e:
        logger.error(f"Error toggling video notifications: {e}", exc_info=True)
        await callback.answer("❌ Ошибка при изменении уведомлений", show_alert=True)


def _bulk_changes_for_action(action: str, template_id: int | None = None) -> dict:
    if action == "auto_tpl":
        return {"enabled": True, "mode": "auto_comment", "default_template_id": int(template_id or 0)}
    if action == "set_tpl":
        return {"default_template_id": int(template_id or 0)}
    if action == "manual":
        return {"mode": "manual_review"}
    if action == "notify":
        return {"mode": "notify_only"}
    if action == "clear_tpl":
        return {"default_template_id": 0}
    if action == "enable":
        return {"enabled": True}
    if action == "disable":
        return {"enabled": False}
    if action == "interval":
        return {"interval_minutes": int(template_id or 0)}
    if action == "run_now":
        return {"run_now": True}
    raise ValueError(f"unknown bulk action: {action}")


def _bulk_summary_text(preview: dict, *, action: str, scope: str, template_name: str | None = None) -> str:
    matched = int(preview.get("matched_count") or 0)
    updated = int(preview.get("updated_count") or 0)
    skipped = int(preview.get("skipped_count") or 0)
    lines = [
        "⚙️ *Массовое действие*",
        "",
        f"Действие: *{escape_markdown(BULK_ACTION_LABELS.get(action, action))}*",
        f"Область: *{escape_markdown(BULK_SCOPE_LABELS.get(scope, scope))}*",
    ]
    if template_name:
        value_label = "Интервал" if action == "interval" else "Шаблон"
        lines.append(f"{value_label}: *{escape_markdown(template_name)}*")
    impact_label = "Будет поставлено на проверку" if action == "run_now" else "Будет изменено"
    lines.extend(
        [
            "",
            f"Найдено блогеров: *{matched}*",
            f"{impact_label}: *{updated}*",
            f"Пропущено: *{skipped}*",
        ]
    )

    skipped_by_reason = preview.get("skipped_by_reason") or {}
    if skipped_by_reason:
        lines.append("")
        lines.append("Почему пропущены:")
        for reason, count in list(skipped_by_reason.items())[:5]:
            label = BULK_SKIP_REASON_LABELS.get(str(reason), str(reason))
            lines.append(f"• {escape_markdown(label)}: *{int(count or 0)}*")

    updated_preview = preview.get("updated_preview") or []
    if updated_preview:
        names = ", ".join(markdown_handle(item.get("username")) for item in updated_preview[:5])
        if len(updated_preview) > 5:
            names += ", …"
        lines.append("")
        lines.append(f"Пример: {names}")

    if not preview.get("dry_run", True):
        queued = int(preview.get("run_now_queued_bloggers") or 0)
        locked = int(preview.get("run_now_locked_bloggers") or 0)
        autopilot_videos = int(preview.get("autopilot_existing_video_count") or 0)
        if queued or locked:
            lines.append("")
            lines.append(f"Поставлено на проверку: *{queued}*")
            if locked:
                lines.append(f"Уже проверялись прямо сейчас: *{locked}*")
        if autopilot_videos:
            lines.append("")
            lines.append(f"Найденные видео поставлены в автокомментинг: *{autopilot_videos}*")
        lines.append("")
        lines.append("Готово.")
    elif updated:
        lines.append("")
        lines.append("Проверьте цифры и подтвердите применение.")
    else:
        lines.append("")
        lines.append("Подходящих изменений нет. Можно выбрать другую область или действие.")
    return "\n".join(lines)


async def _bulk_preview(
    callback: CallbackQuery,
    *,
    action: str,
    scope: str,
    template_id: int | None = None,
    template_name: str | None = None,
) -> None:
    user_id = await get_user_backend_id(callback.from_user.id)
    if not user_id:
        await callback.answer("❌ Пользователь не найден", show_alert=True)
        return

    preview = await api_client.bulk_update_video_creators(
        user_id,
        selector={"scope": scope},
        changes=_bulk_changes_for_action(action, template_id),
        dry_run=True,
    )
    updated = int(preview.get("updated_count") or 0)
    if updated:
        keyboard = get_bulk_confirm_keyboard(action, scope, template_id)
    else:
        keyboard = InlineKeyboardMarkup(
            inline_keyboard=[
                [InlineKeyboardButton(text="◀️ Выбрать область", callback_data=f"vp:bulk:a:{action}")],
                [InlineKeyboardButton(text="Отмена", callback_data="vp:bulk")],
            ]
        )
    await safe_edit_text(
        callback.message,
        _bulk_summary_text(preview, action=action, scope=scope, template_name=template_name),
        reply_markup=keyboard,
        parse_mode="Markdown",
    )


@router.callback_query(F.data == "vp:bulk")
async def show_bulk_actions(callback: CallbackQuery):
    try:
        if await _deny_bulk_actions_in_parsing_mode(callback):
            return
        await callback.answer()
        parsing_mode = await is_parsing_mode(telegram_id=callback.from_user.id)
        message = (
            "⚙️ *Массовые действия*\n\n"
            "Здесь можно быстро настроить сразу много блогеров: включить авто-комментинг, "
            "назначить шаблон, сменить режим, изменить интервал, включить или выключить мониторинг.\n\n"
            "Перед применением бот покажет точный предпросмотр: сколько блогеров изменится и кто будет пропущен."
        )
        if parsing_mode:
            message += "\n\nСейчас включен режим уведомлений: действия с комментариями скрыты."
        await safe_edit_text(
            callback.message,
            message,
            reply_markup=get_bulk_actions_keyboard(parsing_mode=parsing_mode),
            parse_mode="Markdown",
        )
    except Exception as e:
        logger.error(f"show_bulk_actions error: {e}", exc_info=True)
        await callback.answer("Ошибка", show_alert=True)


@router.callback_query(F.data.startswith("vp:bulk:a:"))
async def bulk_choose_action(callback: CallbackQuery):
    try:
        if await _deny_bulk_actions_in_parsing_mode(callback):
            return
        await callback.answer()
        action = callback.data.split(":")[-1]
        if action not in BULK_ACTION_LABELS:
            await callback.answer("Неизвестное действие", show_alert=True)
            return
        message = (
            "🎯 *Кого изменить?*\n\n"
            f"Действие: *{escape_markdown(BULK_ACTION_LABELS[action])}*\n\n"
            "Выберите область. На следующем шаге бот покажет предпросмотр перед применением."
        )
        await safe_edit_text(
            callback.message,
            message,
            reply_markup=get_bulk_scopes_keyboard(action),
            parse_mode="Markdown",
        )
    except Exception as e:
        logger.error(f"bulk_choose_action error: {e}", exc_info=True)
        await callback.answer("Ошибка", show_alert=True)


@router.callback_query(F.data.startswith("vp:bulk:s:"))
async def bulk_choose_scope(callback: CallbackQuery):
    try:
        if await _deny_bulk_actions_in_parsing_mode(callback):
            return
        await callback.answer()
        parts = callback.data.split(":")
        action = parts[3]
        scope = parts[4]
        if action in BULK_TEMPLATE_ACTIONS:
            user_id = await get_user_backend_id(callback.from_user.id)
            if not user_id:
                await callback.answer("❌ Пользователь не найден", show_alert=True)
                return
            templates = filter_full_templates(await api_client.list_task_templates(user_id))
            if not templates:
                await callback.answer("Сначала создайте шаблон", show_alert=True)
                return
            message = (
                "⭐ *Выберите шаблон*\n\n"
                f"Действие: *{escape_markdown(BULK_ACTION_LABELS.get(action, action))}*\n"
                f"Область: *{escape_markdown(BULK_SCOPE_LABELS.get(scope, scope))}*"
            )
            await safe_edit_text(
                callback.message,
                message,
                reply_markup=get_bulk_template_keyboard(action, scope, templates),
                parse_mode="Markdown",
            )
            return
        if action in BULK_INTERVAL_ACTIONS:
            message = (
                "⏱ *Выберите интервал*\n\n"
                f"Действие: *{escape_markdown(BULK_ACTION_LABELS.get(action, action))}*\n"
                f"Область: *{escape_markdown(BULK_SCOPE_LABELS.get(scope, scope))}*"
            )
            await safe_edit_text(
                callback.message,
                message,
                reply_markup=get_bulk_interval_keyboard(action, scope),
                parse_mode="Markdown",
            )
            return
        await _bulk_preview(callback, action=action, scope=scope)
    except Exception as e:
        logger.error(f"bulk_choose_scope error: {e}", exc_info=True)
        await callback.answer("Ошибка", show_alert=True)


@router.callback_query(F.data.startswith("vp:bulk:t:"))
async def bulk_template_selected(callback: CallbackQuery):
    try:
        if await _deny_bulk_actions_in_parsing_mode(callback):
            return
        await callback.answer()
        parts = callback.data.split(":")
        action = parts[3]
        scope = parts[4]
        template_id = int(parts[5])
        user_id = await get_user_backend_id(callback.from_user.id)
        if not user_id:
            await callback.answer("❌ Пользователь не найден", show_alert=True)
            return
        templates = filter_full_templates(await api_client.list_task_templates(user_id))
        template = next((item for item in templates if int(item.get("id") or 0) == template_id), None)
        if not template:
            await callback.answer("Этот шаблон подходит только для заказов на ответы", show_alert=True)
            return
        template_name = template.get("name") if template else f"#{template_id}"
        await _bulk_preview(
            callback,
            action=action,
            scope=scope,
            template_id=template_id,
            template_name=template_name,
        )
    except Exception as e:
        logger.error(f"bulk_template_selected error: {e}", exc_info=True)
        await callback.answer("Ошибка", show_alert=True)


@router.callback_query(F.data.startswith("vp:bulk:tp:"))
async def bulk_template_page(callback: CallbackQuery):
    try:
        if await _deny_bulk_actions_in_parsing_mode(callback):
            return
        await callback.answer()
        parts = callback.data.split(":")
        action = parts[3]
        scope = parts[4]
        page = max(0, int(parts[5]))
        user_id = await get_user_backend_id(callback.from_user.id)
        if not user_id:
            await callback.answer("❌ Пользователь не найден", show_alert=True)
            return
        templates = filter_full_templates(await api_client.list_task_templates(user_id))
        if not templates:
            await callback.answer("Сначала создайте шаблон", show_alert=True)
            return
        message = (
            "⭐ *Выберите шаблон*\n\n"
            f"Действие: *{escape_markdown(BULK_ACTION_LABELS.get(action, action))}*\n"
            f"Область: *{escape_markdown(BULK_SCOPE_LABELS.get(scope, scope))}*"
        )
        await safe_edit_text(
            callback.message,
            message,
            reply_markup=get_bulk_template_keyboard(action, scope, templates, page=page),
            parse_mode="Markdown",
        )
    except Exception as e:
        logger.error(f"bulk_template_page error: {e}", exc_info=True)
        await callback.answer("Ошибка", show_alert=True)


@router.callback_query(F.data.startswith("vp:bulk:i:"))
async def bulk_interval_selected(callback: CallbackQuery):
    try:
        if await _deny_bulk_actions_in_parsing_mode(callback):
            return
        await callback.answer()
        parts = callback.data.split(":")
        action = parts[3]
        scope = parts[4]
        interval_minutes = int(parts[5])
        await _bulk_preview(
            callback,
            action=action,
            scope=scope,
            template_id=interval_minutes,
            template_name=f"{interval_minutes} мин",
        )
    except Exception as e:
        logger.error(f"bulk_interval_selected error: {e}", exc_info=True)
        await callback.answer("Ошибка", show_alert=True)


@router.callback_query(F.data.startswith("vp:bulk:c:"))
async def bulk_confirm_apply(callback: CallbackQuery):
    try:
        if await _deny_bulk_actions_in_parsing_mode(callback):
            return
        await callback.answer()
        parts = callback.data.split(":")
        action = parts[3]
        scope = parts[4]
        template_id = int(parts[5]) if len(parts) > 5 and parts[5] else None
        user_id = await get_user_backend_id(callback.from_user.id)
        if not user_id:
            await callback.answer("❌ Пользователь не найден", show_alert=True)
            return
        result = await api_client.bulk_update_video_creators(
            user_id,
            selector={"scope": scope},
            changes=_bulk_changes_for_action(action, template_id),
            dry_run=False,
        )
        template_name = None
        if template_id and action in BULK_INTERVAL_ACTIONS:
            template_name = f"{template_id} мин"
        elif template_id:
            try:
                templates = filter_full_templates(await api_client.list_task_templates(user_id))
                template = next((item for item in templates if int(item.get("id") or 0) == template_id), None)
                template_name = template.get("name") if template else f"#{template_id}"
            except Exception:
                template_name = f"#{template_id}"
        await safe_edit_text(
            callback.message,
            _bulk_summary_text(result, action=action, scope=scope, template_name=template_name),
            reply_markup=InlineKeyboardMarkup(
                inline_keyboard=[
                    [InlineKeyboardButton(text="👤 К списку блогеров", callback_data="vp:list")],
                    [InlineKeyboardButton(text="⚙️ Еще массовое действие", callback_data="vp:bulk")],
                ]
            ),
            parse_mode="Markdown",
        )
    except APIRequestError as e:
        logger.error(f"bulk_confirm_apply API error: {e}", exc_info=True)
        await callback.answer(f"Ошибка: {str(e)[:120]}", show_alert=True)
    except Exception as e:
        logger.error(f"bulk_confirm_apply error: {e}", exc_info=True)
        await callback.answer("Ошибка", show_alert=True)


@router.callback_query(F.data == "vp:add")
async def add_creator_start(callback: CallbackQuery, state: FSMContext):
    """Показывает быстрые подсказки блогеров перед ручным добавлением."""
    try:
        await show_creator_suggestions(callback, state)
    except Exception as e:
        logger.error(f"Error starting add creator: {e}", exc_info=True)
        await callback.answer("Ошибка", show_alert=True)


async def _show_manual_add_prompt(callback: CallbackQuery, state: FSMContext) -> None:
    await state.set_state(VideoParsingStates.INPUT_USERNAME)
    await state.update_data(
        pending_new_creator_username=None,
        pending_new_creator_from_suggestion=False,
        pending_new_creator_suggestions_page=None,
    )

    message = "➕ *Добавление блогера*\n\n"
    message += "Отправьте username блогера без `@`.\n"
    message += "Например: `username` или `@username`\n\n"
    message += "Если блогера уже ведут коллеги, он всё равно добавится вам как отдельное подключение мониторинга."

    await safe_edit_text(
        callback.message,
        message,
        parse_mode="Markdown",
    )


async def _show_new_creator_interval_prompt(
    target: CallbackQuery | Message,
    state: FSMContext,
    username: str,
    *,
    from_suggestion: bool = False,
    suggestions_page: int | None = None,
) -> None:
    # If adaptive scheduling is enabled, skip the interval question entirely:
    # the algorithm will figure out the right cadence per blogger from API history.
    # User can still tweak via per-blogger overrides in the admin panel later.
    adaptive_enabled = False
    try:
        adaptive_settings = await api_client.get_adaptive_settings()
        adaptive_enabled = bool(adaptive_settings.get("adaptive_enabled"))
    except Exception:
        adaptive_enabled = False

    if adaptive_enabled:
        await state.update_data(
            pending_new_creator_username=username,
            pending_new_creator_from_suggestion=from_suggestion,
            pending_new_creator_suggestions_page=suggestions_page,
            creator_id=None,
        )
        msg = target if isinstance(target, Message) else target.message
        is_cb = isinstance(target, CallbackQuery)
        # Show a brief adaptive notice before the API call so the user isn't confused
        # by the missing interval step.
        notice = (
            f"🧠 *Адаптивный режим*\n\n"
            f"Блогер: {markdown_handle(username)}\n\n"
            "Интервал подберётся автоматически по истории постов — выбирать вручную не нужно.\n"
            "_Создаю…_"
        )
        try:
            if is_cb:
                await safe_edit_text(msg, notice, parse_mode="Markdown")
            else:
                await msg.answer(notice, parse_mode="Markdown")
        except Exception:
            pass
        # interval=30 is a flat fallback only until adaptive calibrates on first check
        await _create_pending_creator_with_interval(
            msg,
            state,
            interval=30,
            telegram_user_id=target.from_user.id,
            edit_result=is_cb,
        )
        return

    await state.set_state(VideoParsingStates.INPUT_INTERVAL)
    await state.update_data(
        pending_new_creator_username=username,
        pending_new_creator_from_suggestion=from_suggestion,
        pending_new_creator_suggestions_page=suggestions_page,
        creator_id=None,
    )

    safe_username = markdown_handle(username)
    message = "⏱ *Интервал проверки*\n\n"
    message += f"Блогер: {safe_username}\n\n"
    message += f"Выберите кнопку или отправьте число от {MIN_CREATOR_INTERVAL_MINUTES} до {MAX_CREATOR_INTERVAL_MINUTES} минут."

    if isinstance(target, CallbackQuery):
        await safe_edit_text(
            target.message,
            message,
            reply_markup=_get_add_interval_keyboard(),
            parse_mode="Markdown",
        )
    else:
        await target.answer(
            message,
            reply_markup=_get_add_interval_keyboard(),
            parse_mode="Markdown",
        )


async def _send_or_edit_add_creator_result(
    message: Message,
    text: str,
    *,
    edit_result: bool = False,
    reply_markup: InlineKeyboardMarkup | None = None,
    parse_mode: str | None = None,
) -> None:
    kwargs = {"reply_markup": reply_markup}
    if parse_mode:
        kwargs["parse_mode"] = parse_mode
    if edit_result:
        await safe_edit_text(message, text, **kwargs)
    else:
        await message.answer(text, **kwargs)


async def _create_pending_creator_with_interval(
    message: Message,
    state: FSMContext,
    interval: int,
    *,
    telegram_user_id: int | None = None,
    edit_result: bool = False,
) -> None:
    data = await state.get_data()
    username = (data.get("pending_new_creator_username") or "").strip().lstrip("@")
    if not username:
        await _send_or_edit_add_creator_result(
            message,
            "❌ Не найден username блогера. Начните добавление заново.",
            edit_result=edit_result,
        )
        await state.clear()
        return

    telegram_user_id = telegram_user_id or message.from_user.id
    user_id = await get_user_backend_id(telegram_user_id)
    if not user_id:
        await _send_or_edit_add_creator_result(
            message,
            "❌ Ошибка: пользователь не найден. Попробуйте позже.",
            edit_result=edit_result,
        )
        await state.clear()
        return

    try:
        creator = await api_client.create_video_creator(
            user_id=user_id,
            username=username,
            enabled=True,
            interval_minutes=interval,
            mode="notify_only" if await is_parsing_mode(telegram_id=telegram_user_id) else "manual_review",
            auto_start=False,
        )
    except Exception as e:
        error_msg = str(e)
        error_msg_lower = error_msg.lower()
        if "already exists" in error_msg_lower or "уже" in error_msg_lower:
            await _send_or_edit_add_creator_result(
                message,
                "❌ Этот блогер уже добавлен в ваш список.",
                edit_result=edit_result,
            )
        elif _is_creator_quota_error(error_msg):
            await _send_or_edit_add_creator_result(
                message,
                _format_creator_quota_error(error_msg),
                edit_result=edit_result,
                reply_markup=get_video_parsing_menu_keyboard(parsing_mode=await is_parsing_mode(telegram_id=telegram_user_id)),
                parse_mode="Markdown",
            )
        else:
            await _send_or_edit_add_creator_result(
                message,
                f"❌ Ошибка при создании блогера: {error_msg[:180]}",
                edit_result=edit_result,
            )
        await state.clear()
        return

    await state.clear()

    _mode_pretty = {
        "auto_comment":   "🤖 Авто-комментарий",
        "manual_review":  "👀 Ручная проверка",
        "notify_only":    "🔔 Только уведомления",
    }.get(creator.get("mode") or "", str(creator.get("mode") or "—"))

    response = "✅ *Блогер добавлен*\n\n"
    response += f"{markdown_handle(creator.get('username', username))}\n"
    response += f"Режим: {_mode_pretty}\n"
    # If adaptive is on, surface that fact instead of the placeholder interval.
    try:
        adaptive_settings = await api_client.get_adaptive_settings()
        if adaptive_settings.get("adaptive_enabled"):
            response += "Интервал: 🧠 *адаптивный* (подберётся автоматически)\n\n"
        else:
            response += f"Интервал: `{creator['interval_minutes']}` мин\n\n"
    except Exception:
        response += f"Интервал: `{creator['interval_minutes']}` мин\n\n"
    response += "Настройте параметры в списке блогеров."

    await _send_or_edit_add_creator_result(
        message,
        response,
        edit_result=edit_result,
        reply_markup=get_video_parsing_menu_keyboard(parsing_mode=await is_parsing_mode(telegram_id=telegram_user_id)),
        parse_mode="Markdown",
    )


async def show_creator_suggestions(
    callback: CallbackQuery,
    state: FSMContext,
    header: str | None = None,
    page: int = 0,
) -> None:
    try:
        await callback.answer()
    except Exception:
        pass

    user_id = await get_user_backend_id(callback.from_user.id)
    if not user_id:
        await callback.answer("❌ Ошибка: пользователь не найден", show_alert=True)
        return

    page = max(0, int(page or 0))
    response = await api_client.list_creator_suggestions(
        user_id,
        limit=SUGGESTIONS_PER_PAGE,
        offset=page * SUGGESTIONS_PER_PAGE,
        include_total=True,
    )
    suggestions = response.get("items", []) if isinstance(response, dict) else (response or [])
    total = int(response.get("total", len(suggestions)) or 0) if isinstance(response, dict) else len(suggestions)

    if not suggestions and page > 0:
        page = _clamp_page(page - 1, total, SUGGESTIONS_PER_PAGE)
        response = await api_client.list_creator_suggestions(
            user_id,
            limit=SUGGESTIONS_PER_PAGE,
            offset=page * SUGGESTIONS_PER_PAGE,
            include_total=True,
        )
        suggestions = response.get("items", []) if isinstance(response, dict) else (response or [])
        total = int(response.get("total", total) or 0) if isinstance(response, dict) else len(suggestions)

    if not suggestions:
        await _show_manual_add_prompt(callback, state)
        return

    page = _clamp_page(page, total, SUGGESTIONS_PER_PAGE)
    start_num = page * SUGGESTIONS_PER_PAGE + 1
    end_num = min(start_num + len(suggestions) - 1, total)
    max_page = max(0, (max(0, total) - 1) // SUGGESTIONS_PER_PAGE) if total else 0

    await state.set_state(VideoParsingStates.SUGGESTIONS)
    await state.update_data(suggestions=suggestions, suggestions_page=page)

    text = "✨ *Подсказки блогеров*\n\n"
    if header:
        text += f"{header}\n\n"
    text += f"Показано *{start_num}-{end_num}* из *{total}*.\n"
    text += "Этих блогеров уже ведут коллеги, а у вас их ещё нет:\n\n"

    for idx, item in enumerate(suggestions, start=1):
        username = markdown_handle(item.get("username", "unknown"))
        users_count = int(item.get("users_count", 0) or 0)
        last_check = _format_last_check(item.get("last_checked_at"))
        text += f"{start_num + idx - 1}. {username} · у *{users_count}* · {last_check}\n"

    text += "\nНажмите на кнопку снизу, чтобы добавить блогера в свой список."

    keyboard_rows: list[list[InlineKeyboardButton]] = []
    for idx, item in enumerate(suggestions):
        username = item.get("username")
        users_count = int(item.get("users_count", 0) or 0)
        if not username:
            continue
        keyboard_rows.append(
            [
                InlineKeyboardButton(
                    text=f"➕ @{_short_username(username)} · {users_count}",
                    callback_data=f"vp:suggest:add:{page}:{idx}",
                )
            ]
        )

    # Массовое добавление всех подсказок по всем страницам.
    # Показываем кнопку только если действительно есть что добавлять.
    if total > 0:
        keyboard_rows.append(
            [
                InlineKeyboardButton(
                    text=f"➕ Добавить всех ({total})",
                    callback_data="vp:suggest:add_all",
                )
            ]
        )

    nav_buttons: list[InlineKeyboardButton] = []
    if page > 0:
        nav_buttons.append(InlineKeyboardButton(text="◀️", callback_data=f"vp:suggest:page:{page - 1}"))
    nav_buttons.append(InlineKeyboardButton(text=f"{page + 1}/{max_page + 1}", callback_data="vp:suggest:noop"))
    if page < max_page:
        nav_buttons.append(InlineKeyboardButton(text="▶️", callback_data=f"vp:suggest:page:{page + 1}"))
    keyboard_rows.append(nav_buttons)

    keyboard_rows.append(
        [
            InlineKeyboardButton(text="⌨️ Ввести вручную", callback_data="vp:suggest:manual"),
            InlineKeyboardButton(text="🔄 Обновить", callback_data=f"vp:suggest:refresh:{page}"),
        ]
    )
    keyboard_rows.append([InlineKeyboardButton(text="⬅️ Назад", callback_data="vp:menu")])

    await safe_edit_text(
        callback.message,
        text,
        reply_markup=InlineKeyboardMarkup(inline_keyboard=keyboard_rows),
        parse_mode="Markdown",
    )


async def _load_all_creator_suggestions(user_id: int) -> list[dict]:
    suggestions: list[dict] = []
    offset = 0
    total: int | None = None

    while True:
        response = await api_client.list_creator_suggestions(
            user_id,
            limit=CREATOR_SUGGESTIONS_API_LIMIT,
            offset=offset,
            include_total=True,
        )
        items = response.get("items", []) if isinstance(response, dict) else (response or [])
        if isinstance(response, dict):
            total = int(response.get("total", len(items)) or 0)

        if not items:
            break

        suggestions.extend(items)

        if total is not None and len(suggestions) >= total:
            break
        if len(items) < CREATOR_SUGGESTIONS_API_LIMIT:
            break

        offset += len(items)

    return suggestions


async def _create_video_creator_for_bulk(
    *,
    user_id: int,
    username: str,
    creator_mode: str,
) -> dict:
    for attempt in range(CREATOR_BULK_CREATE_MAX_ATTEMPTS):
        try:
            return await api_client.create_video_creator(
                user_id=user_id,
                username=username,
                enabled=True,
                interval_minutes=30,
                mode=creator_mode,
                auto_start=False,
            )
        except Exception as exc:
            if _is_rate_limit_error(str(exc)) and attempt < CREATOR_BULK_CREATE_MAX_ATTEMPTS - 1:
                await asyncio.sleep(CREATOR_BULK_CREATE_DELAY_SECONDS)
                continue
            raise


@router.callback_query(F.data == "vp:suggest:manual")
async def suggestions_manual_input(callback: CallbackQuery, state: FSMContext):
    """Переключает flow на ручной ввод username."""
    try:
        await callback.answer()
        await _show_manual_add_prompt(callback, state)
    except Exception as e:
        logger.error(f"Error switching to manual add prompt: {e}", exc_info=True)
        await callback.answer("Ошибка", show_alert=True)


@router.callback_query(F.data.startswith("vp:suggest:page:"))
async def suggestions_page(callback: CallbackQuery, state: FSMContext):
    try:
        page = int(callback.data.split(":")[-1])
        await show_creator_suggestions(callback, state, page=page)
    except Exception as e:
        logger.error(f"Error paginating creator suggestions: {e}", exc_info=True)
        await callback.answer("Ошибка", show_alert=True)


@router.callback_query(F.data == "vp:suggest:noop")
async def suggestions_noop(callback: CallbackQuery):
    await callback.answer()


@router.callback_query(F.data.startswith("vp:suggest:refresh:"))
async def suggestions_refresh_page(callback: CallbackQuery, state: FSMContext):
    try:
        page = int(callback.data.split(":")[-1])
    except (TypeError, ValueError):
        page = 0

    try:
        await show_creator_suggestions(callback, state, page=page)
    except Exception as e:
        logger.error(f"Error refreshing creator suggestions page: {e}", exc_info=True)
        await callback.answer("Ошибка", show_alert=True)


@router.callback_query(F.data == "vp:suggest:refresh")
async def suggestions_refresh(callback: CallbackQuery, state: FSMContext):
    """Обновляет список подсказок блогеров."""
    try:
        await show_creator_suggestions(callback, state)
    except Exception as e:
        logger.error(f"Error refreshing creator suggestions: {e}", exc_info=True)
        await callback.answer("Ошибка", show_alert=True)


@router.callback_query(F.data.startswith("vp:suggest:add:"))
async def suggestions_add_creator_page(callback: CallbackQuery, state: FSMContext):
    """Добавляет блогера из новой постраничной клавиатуры подсказок."""
    try:
        data = await state.get_data()
        suggestions = data.get("suggestions") or []
        current_page = int(data.get("suggestions_page", 0) or 0)

        try:
            parts = callback.data.split(":")
            page = int(parts[-2])
            idx = int(parts[-1])
        except (TypeError, ValueError, IndexError):
            await callback.answer("❌ Неверная подсказка", show_alert=True)
            return

        if page != current_page or idx < 0 or idx >= len(suggestions):
            await callback.answer("⚠️ Подсказки устарели, обновляю список", show_alert=False)
            await show_creator_suggestions(callback, state, page=page)
            return

        username = (suggestions[idx].get("username") or "").strip().lstrip("@")
        if not username:
            await callback.answer("❌ Неверный username", show_alert=True)
            return

        await callback.answer(f"➕ Добавляю @{username}…", show_alert=False)
        await _show_new_creator_interval_prompt(
            callback,
            state,
            username,
            from_suggestion=True,
            suggestions_page=page,
        )
    except Exception as e:
        logger.error(f"Error adding creator from paged suggestions: {e}", exc_info=True)
        await callback.answer("Ошибка", show_alert=True)


@router.callback_query(F.data == "vp:suggest:add_all")
async def suggestions_add_all_creators(callback: CallbackQuery, state: FSMContext):
    """Массово добавляет себе всех блогеров из текущих подсказок.

    Логика:
    - Тянем подсказки пачками по API-limit — это покрывает реальный
      потолок в проекте; типовой пользователь увидит 5-30 предложений.
    - Добавляем каждого через тот же ``create_video_creator``, что и
      ручное добавление, с дефолтным интервалом 30 минут (если включён
      адаптивный режим — он всё равно сам подберёт интервал).
    - На каждой ошибке решаем: если это quota — прерываемся, если
      "уже есть" — пропускаем, иначе — продолжаем, копим количество
      провалов и в конце показываем сводный отчёт.
    """
    try:
        await callback.answer("⏳ Добавляю блогеров…", show_alert=False)

        telegram_user_id = callback.from_user.id
        user_id = await get_user_backend_id(telegram_user_id)
        if not user_id:
            await callback.answer("❌ Ошибка: пользователь не найден", show_alert=True)
            return

        try:
            suggestions = await _load_all_creator_suggestions(user_id)
        except Exception as exc:
            logger.error(f"Failed to load suggestions for bulk add: {exc}", exc_info=True)
            await callback.answer("Ошибка загрузки подсказок", show_alert=True)
            return

        if not suggestions:
            await callback.answer("Нет блогеров для добавления", show_alert=True)
            await show_creator_suggestions(callback, state)
            return

        parsing_mode = await is_parsing_mode(telegram_id=telegram_user_id)
        creator_mode = "notify_only" if parsing_mode else "manual_review"

        try:
            await safe_edit_text(
                callback.message,
                (
                    "⏳ *Массовое добавление блогеров*\n\n"
                    f"Найдено: `{len(suggestions)}`. Добавляю по очереди, чтобы не упереться в лимит API."
                ),
                reply_markup=None,
                parse_mode="Markdown",
            )
        except Exception:
            pass

        added: list[str] = []
        skipped_existing: list[str] = []
        failed: list[tuple[str, str]] = []
        quota_blocked: str | None = None
        create_attempts = 0

        for item in suggestions:
            username = (item.get("username") or "").strip().lstrip("@")
            if not username:
                continue
            if create_attempts > 0:
                await asyncio.sleep(CREATOR_BULK_CREATE_DELAY_SECONDS)
            create_attempts += 1
            try:
                await _create_video_creator_for_bulk(
                    user_id=user_id,
                    username=username,
                    creator_mode=creator_mode,
                )
                added.append(username)
            except Exception as exc:
                error_msg = str(exc)
                low = error_msg.lower()
                if "already exists" in low or "уже" in low:
                    skipped_existing.append(username)
                    continue
                if _is_creator_quota_error(error_msg):
                    quota_blocked = error_msg
                    break
                failed.append((username, error_msg[:120]))

        # Чистим состояние, чтобы старый список подсказок в FSM не сбивал
        # последующее ручное добавление.
        try:
            await state.clear()
        except Exception:
            pass

        # Собираем человеко-читаемый отчёт.
        report = "🪄 *Массовое добавление блогеров*\n\n"
        report += f"✅ Добавлено: `{len(added)}`\n"
        if skipped_existing:
            report += f"↪️ Уже были: `{len(skipped_existing)}`\n"
        if failed:
            report += f"⚠️ Не удалось: `{len(failed)}`\n"
        if quota_blocked:
            report += "\n" + _format_creator_quota_error(quota_blocked) + "\n"

        if added:
            preview = ", ".join(markdown_handle(u) for u in added[:10])
            if len(added) > 10:
                preview += f" и ещё {len(added) - 10}"
            report += f"\n*Новые:* {preview}"

        if failed:
            sample = "; ".join(f"{markdown_handle(u)} — {err}" for u, err in failed[:3])
            report += f"\n\n_Часть ошибок:_ {sample}"

        try:
            await safe_edit_text(
                callback.message,
                report,
                reply_markup=get_video_parsing_menu_keyboard(parsing_mode=parsing_mode),
                parse_mode="Markdown",
            )
        except Exception:
            await callback.message.answer(
                report,
                reply_markup=get_video_parsing_menu_keyboard(parsing_mode=parsing_mode),
                parse_mode="Markdown",
            )
    except Exception as e:
        logger.error(f"Error in bulk add suggestions: {e}", exc_info=True)
        try:
            await callback.answer("Ошибка массового добавления", show_alert=True)
        except Exception:
            pass


@router.callback_query(F.data.startswith("vp:suggest:add_idx:"))
async def suggestions_add_creator(callback: CallbackQuery, state: FSMContext):
    """Добавляет блогера из подсказок в один клик."""
    try:
        data = await state.get_data()
        suggestions = data.get("suggestions") or []

        try:
            idx = int(callback.data.split(":")[-1])
        except ValueError:
            await callback.answer("❌ Неверный индекс", show_alert=True)
            return

        if idx < 0 or idx >= len(suggestions):
            await callback.answer("⚠️ Подсказки устарели, обновляю список", show_alert=False)
            await show_creator_suggestions(callback, state)
            return

        username = (suggestions[idx].get("username") or "").strip().lstrip("@")
        if not username:
            await callback.answer("❌ Неверный username", show_alert=True)
            return

        await callback.answer(f"➕ Добавляю @{username}…", show_alert=False)
        await _show_new_creator_interval_prompt(
            callback,
            state,
            username,
            from_suggestion=True,
            suggestions_page=int(data.get("suggestions_page", 0) or 0),
        )
    except Exception as e:
        logger.error(f"Error adding creator from suggestions: {e}", exc_info=True)
        await callback.answer("Ошибка", show_alert=True)


@router.message(StateFilter(VideoParsingStates.INPUT_USERNAME, VideoParsingStates.SUGGESTIONS))
async def add_creator_process(message: Message, state: FSMContext):
    """ИСПРАВЛЕНО: Обрабатывает username и создаёт персонального блогера для пользователя."""
    try:
        username = message.text.strip().lstrip("@")
        
        if not username:
            await message.answer("❌ Username не может быть пустым. Попробуйте ещё раз:")
            return
        
        # ИСПРАВЛЕНО: Получаем user_id для создания персонального блогера
        user_id = await get_user_backend_id(message.from_user.id)
        if not user_id:
            await message.answer("❌ Ошибка: пользователь не найден. Попробуйте позже.")
            await state.clear()
            return
        
        await _show_new_creator_interval_prompt(message, state, username)
    except Exception as e:
        logger.error(f"Error creating creator: {e}", exc_info=True)
        error_msg = str(e)
        error_msg_lower = error_msg.lower()
        if "already exists" in error_msg_lower:
            await message.answer("❌ Этот блогер уже добавлен в ваш список. Попробуйте другой username:")
        elif _is_creator_quota_error(error_msg):
            await state.clear()
            await message.answer(
                _format_creator_quota_error(error_msg),
                reply_markup=get_video_parsing_menu_keyboard(parsing_mode=await is_parsing_mode(telegram_id=message.from_user.id)),
                parse_mode="Markdown",
            )
        else:
            await message.answer(f"❌ Ошибка при создании блогера: {error_msg[:180]}")


@router.callback_query(F.data == "vp:list")
async def list_creators(callback: CallbackQuery, state: FSMContext):
    """Показывает список персональных блогеров пользователя."""
    try:
        await state.clear()
    except Exception:
        pass
    await _render_creators_list(callback, 0)


@router.callback_query(F.data == "vp:list:search")
async def search_creators_prompt(callback: CallbackQuery, state: FSMContext):
    """Запрашивает username для поиска по уже добавленным блогерам."""
    try:
        await callback.answer()
        if not callback.message:
            await callback.answer("Ошибка: сообщение не найдено", show_alert=True)
            return
        await state.set_state(VideoParsingStates.SEARCH_CREATORS)
        await state.update_data(creator_search_from="list")
        await safe_edit_text(
            callback.message,
            "🔍 *Поиск блогера*\n\n"
            "Введите username или часть username.\n"
            "Можно писать с `@` или без.\n\n"
            "Я покажу совпадения и каждая кнопка сразу откроет карточку блогера.",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="◀️ К списку", callback_data="vp:list")]
            ]),
            parse_mode="Markdown",
        )
    except Exception as e:
        logger.error(f"Error opening creator search: {e}", exc_info=True)
        await callback.answer("Ошибка", show_alert=True)


@router.message(StateFilter(VideoParsingStates.SEARCH_CREATORS))
async def search_creators_process(message: Message, state: FSMContext):
    """Ищет блогера в личном списке и даёт кнопки для открытия карточки."""
    raw_query = (message.text or "").strip()
    query = raw_query.lstrip("@").strip()
    if not query:
        await message.answer("Введите username или часть username.")
        return

    user_id = await get_user_backend_id(message.from_user.id)
    if not user_id:
        await state.clear()
        await message.answer("❌ Ошибка: пользователь не найден. Попробуйте позже.")
        return

    try:
        creators = await api_client.list_video_creators(user_id)
        matches = _filter_creators_by_query(creators or [], query)
        await state.update_data(creator_search_query=query)
    except Exception as e:
        logger.error(f"Error searching creators: {e}", exc_info=True)
        await state.clear()
        await message.answer("❌ Не удалось загрузить список блогеров. Попробуйте ещё раз.")
        return

    safe_query = markdown_inline_code(query[:80])
    if not matches:
        await message.answer(
            f"🔍 По запросу {safe_query} ничего не найдено.\n\n"
            f"Всего в вашем списке: `{len(creators or [])}`.",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="🔍 Искать ещё", callback_data="vp:list:search")],
                [InlineKeyboardButton(text="👤 К списку", callback_data="vp:list")],
            ]),
            parse_mode="Markdown",
        )
        return

    text, keyboard, _ = _build_creator_search_result_message(matches, query, page=0)

    await message.answer(
        text,
        reply_markup=keyboard,
        parse_mode="Markdown",
    )


@router.callback_query(F.data.startswith("vp:list:search:page:"))
async def search_creators_page(callback: CallbackQuery, state: FSMContext):
    """Постранично показывает результаты последнего поиска по личным блогерам."""
    try:
        page = int((callback.data or "").split(":")[-1])
    except (TypeError, ValueError):
        page = 0

    data = await state.get_data()
    query = str(data.get("creator_search_query") or "").strip()
    if not query:
        await callback.answer("Поиск устарел. Введите запрос ещё раз.", show_alert=True)
        await search_creators_prompt(callback, state)
        return

    try:
        await callback.answer()
        if not callback.message:
            await callback.answer("Ошибка: сообщение не найдено", show_alert=True)
            return
        user_id = await get_user_backend_id(callback.from_user.id)
        if not user_id:
            await callback.answer("❌ Ошибка: пользователь не найден", show_alert=True)
            return
        creators = await api_client.list_video_creators(user_id)
        matches = _filter_creators_by_query(creators or [], query)
        if not matches:
            await callback.answer("Совпадений больше нет. Обновляю поиск.", show_alert=False)
            await search_creators_prompt(callback, state)
            return
        text, keyboard, _ = _build_creator_search_result_message(matches, query, page=page)
        await safe_edit_text(callback.message, text, reply_markup=keyboard, parse_mode="Markdown")
    except Exception as e:
        logger.error(f"Error paginating creator search: {e}", exc_info=True)
        await callback.answer("Ошибка", show_alert=True)


@router.callback_query(F.data == "vp:list:search:noop")
async def search_creators_noop(callback: CallbackQuery):
    await callback.answer()


@router.callback_query(F.data.startswith("vp:list:page:"))
async def list_creators_page(callback: CallbackQuery):
    """Пагинация списка блогеров."""
    try:
        page = int(callback.data.split(":")[-1])
        await list_creators_with_page(callback, page)
    except Exception as e:
        logger.error(f"Error paginating creators: {e}", exc_info=True)
        await callback.answer("Ошибка", show_alert=True)


@router.callback_query(F.data == "vp:list:noop")
async def list_creators_noop(callback: CallbackQuery):
    await callback.answer()


async def list_creators_with_page(callback: CallbackQuery, page: int = 0):
    """Показывает список блогеров с указанной страницей."""
    await _render_creators_list(callback, page)


# ── Parsing-mode inbox (safe read-only view of new videos) ─────────────────

_PARSING_INBOX_PER_PAGE = 5


async def _review_video_result_line(video_id: int, user_id: int) -> str:
    try:
        videos = await api_client.get_videos_by_ids([video_id], user_id)
    except Exception:
        videos = []
    if videos:
        return format_video_markdown_line(videos[0], 1)
    return f"Видео ID: `{video_id}`"


async def _show_parsing_inbox(callback: CallbackQuery, page: int = 0) -> None:
    """Показывает новые видео с действиями: открыть, прокомментил, пропустить."""
    try:
        await callback.answer()
    except Exception:
        pass
    if not callback.message:
        return

    user_id = await get_user_backend_id(callback.from_user.id)
    if not user_id:
        await callback.answer("❌ Пользователь не найден", show_alert=True)
        return

    try:
        inbox_data = await api_client.get_inbox_videos(
            user_id, status="new", limit=_PARSING_INBOX_PER_PAGE + 1, offset=page * _PARSING_INBOX_PER_PAGE
        )
        videos = inbox_data.get("videos", [])
    except Exception as e:
        logger.error(f"Error loading parsing inbox: {e}", exc_info=True)
        await callback.answer("Ошибка загрузки видео. Попробуйте позже.", show_alert=True)
        return

    has_next = len(videos) > _PARSING_INBOX_PER_PAGE
    page_videos = videos[:_PARSING_INBOX_PER_PAGE]

    try:
        stats = await api_client.get_inbox_stats(user_id)
    except Exception:
        stats = {}
    total_new = stats.get("new", len(page_videos))
    total_done = stats.get("done", 0)
    total_skipped = stats.get("skipped", 0)

    if not page_videos and page == 0:
        text = (
            "📥 *Новые видео*\n\n"
            "🟢 Сейчас всё чисто — новых видео нет.\n"
            "Бот продолжает следить за включёнными блогерами и пришлёт сюда, когда появится.\n\n"
            f"_Всего обработано:_ ✅ {total_done}  ·  🗑 {total_skipped}"
        )
        kb = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="🔄 Обновить", callback_data="vp:parsing_inbox")],
            [InlineKeyboardButton(text="◀️ Назад", callback_data="vp:menu")],
        ])
        await safe_edit_text(callback.message, text, reply_markup=kb, parse_mode="Markdown")
        return

    pages_total = (total_new + _PARSING_INBOX_PER_PAGE - 1) // _PARSING_INBOX_PER_PAGE if total_new else 1
    header = (
        f"📥 *Новые видео* — *{total_new}* в очереди\n"
        f"_Прокомментировано:_ ✅ {total_done}  ·  _Пропущено:_ 🗑 {total_skipped}\n"
        f"Страница {page + 1} из {pages_total}\n"
        "━━━━━━━━━━━━━━━\n"
    )
    lines = [header]
    btn_rows: list[list[InlineKeyboardButton]] = []

    for idx, v in enumerate(page_videos, start=1 + page * _PARSING_INBOX_PER_PAGE):
        url = normalize_video_url(v.get("url") or "")
        db_id = v.get("id")

        lines.append(format_video_markdown_line(v, idx, include_username=True))
        lines.append("")

        if db_id:
            row: list[InlineKeyboardButton] = []
            if url:
                row.append(InlineKeyboardButton(text=f"🎬 #{idx}", url=url))
            row.append(InlineKeyboardButton(
                text=f"✅ Прокомментил",
                callback_data=f"vp:parsing_inbox:done:{db_id}",
            ))
            row.append(InlineKeyboardButton(
                text="🗑 Пропустить",
                callback_data=f"vp:parsing_inbox:skip:{db_id}",
            ))
            btn_rows.append(row)

    if total_new > 0:
        btn_rows.append([
            InlineKeyboardButton(
                text=f"✅ Прокомментил все ({total_new})",
                callback_data="vp:pin:d:ask",
            ),
        ])
        btn_rows.append([
            InlineKeyboardButton(
                text=f"🗑 Пропустить все ({total_new})",
                callback_data="vp:pin:s:ask",
            ),
        ])

    nav: list[InlineKeyboardButton] = []
    if page > 0:
        nav.append(InlineKeyboardButton(text="◀️", callback_data=f"vp:parsing_inbox:page:{page - 1}"))
    nav.append(InlineKeyboardButton(text=f"{page + 1}/{pages_total}", callback_data="vp:parsing_inbox:noop"))
    if has_next:
        nav.append(InlineKeyboardButton(text="▶️", callback_data=f"vp:parsing_inbox:page:{page + 1}"))
    if nav:
        btn_rows.append(nav)

    btn_rows.append([
        InlineKeyboardButton(text="🔄 Обновить", callback_data="vp:parsing_inbox"),
        InlineKeyboardButton(text="◀️ Назад", callback_data="vp:menu"),
    ])

    await safe_edit_text(
        callback.message,
        "\n".join(lines),
        reply_markup=InlineKeyboardMarkup(inline_keyboard=btn_rows),
        parse_mode="Markdown",
        disable_web_page_preview=True,
    )


@router.callback_query(F.data == "vp:parsing_inbox")
async def parsing_inbox_show(callback: CallbackQuery):
    await _show_parsing_inbox(callback, page=0)


@router.callback_query(F.data == "vp:parsing_inbox:noop")
async def parsing_inbox_noop(callback: CallbackQuery):
    await callback.answer()


@router.callback_query(F.data.startswith("vp:parsing_inbox:page:"))
async def parsing_inbox_page(callback: CallbackQuery):
    try:
        page = int(callback.data.split(":")[-1])
    except (ValueError, IndexError):
        page = 0
    await _show_parsing_inbox(callback, page=page)


@router.callback_query(F.data.startswith("vp:parsing_inbox:skip:"))
async def parsing_inbox_skip_one(callback: CallbackQuery):
    """Пропускает одно видео в parsing-mode inbox."""
    try:
        db_id = int(callback.data.split(":")[-1])
    except (ValueError, IndexError):
        await callback.answer("Неверный формат", show_alert=True)
        return
    user_id = await get_user_backend_id(callback.from_user.id)
    if not user_id:
        await callback.answer("❌ Пользователь не найден", show_alert=True)
        return
    try:
        await api_client.skip_video(db_id, user_id)
        await callback.answer("🗑 Видео пропущено", show_alert=False)
        await _show_parsing_inbox(callback, page=0)
    except Exception as e:
        logger.error(f"parsing_inbox_skip_one error: {e}", exc_info=True)
        await callback.answer("Ошибка при пропуске видео", show_alert=True)


@router.callback_query(F.data.startswith("vp:parsing_inbox:done:"))
async def parsing_inbox_done_one(callback: CallbackQuery):
    """Помечает одно видео как откомментированное вручную."""
    try:
        db_id = int(callback.data.split(":")[-1])
    except (ValueError, IndexError):
        await callback.answer("Неверный формат", show_alert=True)
        return
    user_id = await get_user_backend_id(callback.from_user.id)
    if not user_id:
        await callback.answer("❌ Пользователь не найден", show_alert=True)
        return
    try:
        await api_client.mark_video_commented(db_id, user_id)
        await callback.answer("✅ Помечено как прокомментированное", show_alert=False)
        await _show_parsing_inbox(callback, page=0)
    except Exception as e:
        logger.error(f"parsing_inbox_done_one error: {e}", exc_info=True)
        await callback.answer("Ошибка при отметке видео", show_alert=True)


def _confirm_keyboard(action: str, label: str, count: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=f"{label} ({count})", callback_data=f"vp:parsing_inbox:{action}")],
        [InlineKeyboardButton(text="◀️ Отмена", callback_data="vp:parsing_inbox")],
    ])


@router.callback_query(F.data.in_({"vp:parsing_inbox:skip_all_confirm", "vp:pin:s:ask"}))
async def parsing_inbox_skip_all_confirm(callback: CallbackQuery):
    user_id = await get_user_backend_id(callback.from_user.id)
    if not user_id:
        await callback.answer("❌ Пользователь не найден", show_alert=True)
        return
    try:
        stats = await api_client.get_inbox_stats(user_id)
    except Exception:
        stats = {}
    total_new = stats.get("new", 0)
    if not total_new:
        await callback.answer("Нет новых видео", show_alert=True)
        return
    await callback.answer()
    text = (
        f"🗑 *Пропустить все новые видео?*\n\n"
        f"К пропуску: *{total_new}* видео.\n"
        "Они исчезнут из «Новые видео» и переедут в «Пропущенные».\n"
        "Действие нельзя отменить — но пропущенные видео можно посмотреть в общем списке."
    )
    await safe_edit_text(
        callback.message, text,
        reply_markup=_confirm_keyboard("skip_all", "🗑 Да, пропустить", total_new),
        parse_mode="Markdown",
    )


@router.callback_query(F.data.in_({"vp:parsing_inbox:done_all_confirm", "vp:pin:d:ask"}))
async def parsing_inbox_done_all_confirm(callback: CallbackQuery):
    user_id = await get_user_backend_id(callback.from_user.id)
    if not user_id:
        await callback.answer("❌ Пользователь не найден", show_alert=True)
        return
    try:
        stats = await api_client.get_inbox_stats(user_id)
    except Exception:
        stats = {}
    total_new = stats.get("new", 0)
    if not total_new:
        await callback.answer("Нет новых видео", show_alert=True)
        return
    await callback.answer()
    text = (
        f"✅ *Пометить все как прокомментированные?*\n\n"
        f"К отметке: *{total_new}* видео.\n"
        "Используйте, если вы уже оставили комментарии вручную на TikTok."
    )
    await safe_edit_text(
        callback.message, text,
        reply_markup=_confirm_keyboard("done_all", "✅ Да, всё прокомментил", total_new),
        parse_mode="Markdown",
    )


@router.callback_query(F.data == "vp:parsing_inbox:skip_all")
async def parsing_inbox_skip_all(callback: CallbackQuery):
    """Пропускает все новые видео в parsing-mode inbox."""
    user_id = await get_user_backend_id(callback.from_user.id)
    if not user_id:
        await callback.answer("❌ Пользователь не найден", show_alert=True)
        return
    try:
        inbox_data = await api_client.get_inbox_videos(user_id, status="new", limit=500)
        videos = inbox_data.get("videos", [])
        if not videos:
            await callback.answer("Нет новых видео", show_alert=True)
            return
        video_ids = [v["id"] for v in videos if v.get("id")]
        result = await api_client.skip_videos_batch(video_ids, user_id)
        skipped = result.get("skipped", len(video_ids))
        await callback.answer(f"🗑 Пропущено {skipped} видео", show_alert=False)
        await _show_parsing_inbox(callback, page=0)
    except Exception as e:
        logger.error(f"parsing_inbox_skip_all error: {e}", exc_info=True)
        await callback.answer("Ошибка", show_alert=True)


@router.callback_query(F.data == "vp:parsing_inbox:done_all")
async def parsing_inbox_done_all(callback: CallbackQuery):
    """Помечает все новые видео как откомментированные вручную."""
    user_id = await get_user_backend_id(callback.from_user.id)
    if not user_id:
        await callback.answer("❌ Пользователь не найден", show_alert=True)
        return
    try:
        inbox_data = await api_client.get_inbox_videos(user_id, status="new", limit=500)
        videos = inbox_data.get("videos", [])
        if not videos:
            await callback.answer("Нет новых видео", show_alert=True)
            return
        video_ids = [v["id"] for v in videos if v.get("id")]
        result = await api_client.mark_videos_commented_batch(video_ids, user_id)
        marked = result.get("marked", len(video_ids))
        await callback.answer(f"✅ Помечено {marked} видео", show_alert=False)
        await _show_parsing_inbox(callback, page=0)
    except Exception as e:
        logger.error(f"parsing_inbox_done_all error: {e}", exc_info=True)
        await callback.answer("Ошибка", show_alert=True)


@router.callback_query(F.data.regexp(r"^vp:c:\d+:apc(?::do(?::run)?)?$"))
async def handle_creator_autopilot_cancel_short(callback: CallbackQuery):
    try:
        parts = (callback.data or "").split(":")
        creator_id = int(parts[2])
    except (ValueError, IndexError):
        await callback.answer("❌ Неверный ID", show_alert=True)
        return
    if len(parts) > 4 and parts[4] == "do":
        include_running = len(parts) > 5 and parts[5] == "run"
        await do_creator_card_cancel_autopilot(callback, creator_id, include_running=include_running)
    else:
        await ask_creator_card_cancel_autopilot(callback, creator_id)


# ИСПРАВЛЕНО: Перемещён после специфичных обработчиков, чтобы они обрабатывались первыми
@router.callback_query(F.data.startswith("vp:creator:") & ~F.data.contains(":template:toggle:") & ~F.data.contains(":template:page:") & ~F.data.contains(":mode:set:") & ~F.data.contains(":templates:save") & ~F.data.contains(":new_videos") & ~F.data.contains(":default_template:set:") & ~F.data.contains(":default_template:page:") & ~F.data.contains(":delete:confirm") & ~F.data.contains(":auto_disable"))
async def handle_creator_action(callback: CallbackQuery, state: FSMContext):
    """Обрабатывает действия с блогером."""
    try:
        if not callback.message:
            await callback.answer("Ошибка: сообщение не найдено", show_alert=True)
            return
        
        parts = callback.data.split(":")
        creator_id = int(parts[2])
        action = parts[3] if len(parts) > 3 else "detail"
        
        if action == "detail":
            await show_creator_detail(callback, creator_id)
        elif action == "toggle":
            await toggle_creator(callback, creator_id)
        elif action == "mode":
            await show_mode_selection(callback, creator_id)
        elif action == "interval":
            await set_creator_interval_start(callback, creator_id, state)
        elif action == "templates":
            await set_creator_templates_start(callback, creator_id, state)
        elif action == "runnow":
            await run_creator_now(callback, creator_id)
        elif action == "delete":
            await delete_creator_confirm(callback, creator_id, state)
        elif action == "default_template":
            await set_creator_default_template_start(callback, creator_id)
        elif action == "cancel_autopilot":
            if len(parts) > 4 and parts[4] == "do":
                include_running = len(parts) > 5 and parts[5] == "run"
                await do_creator_card_cancel_autopilot(callback, creator_id, include_running=include_running)
            else:
                await ask_creator_card_cancel_autopilot(callback, creator_id)
        else:
            await callback.answer("❌ Неизвестное действие", show_alert=True)
    except ValueError as e:
        logger.error(f"Invalid creator_id in handle_creator_action: {e}", exc_info=True)
        await callback.answer("❌ Неверный ID", show_alert=True)
    except Exception as e:
        logger.error(f"Error handling creator action: {e}", exc_info=True)
        await callback.answer("❌ Ошибка", show_alert=True)


def _build_creator_cancel_result_message(result: dict) -> str:
    lines = ["🧹 *Очередь автокомментинга отменена*", ""]
    creator = str(result.get("creator_username") or "").strip()
    if creator:
        lines.append(f"Блогер: {markdown_handle(creator)}")
    lines.append(f"Отменено заказов в очереди: *{int(result.get('cancelled_pending_tasks') or 0)}*")
    running = int(result.get("stopped_running_tasks") or 0)
    if running:
        lines.append(f"Остановлено активных заказов: *{running}*")
    lines.append(f"Отменено событий создания: *{int(result.get('cancelled_outbox_events') or 0)}*")
    lines.append(f"Видео возвращено в новые: *{int(result.get('released_videos') or 0)}*")
    refunded = float(result.get("refunded_amount") or 0)
    if refunded > 0:
        lines.append(f"Возврат: *{refunded:.2f} грн*")
    removed = int(result.get("removed_from_redis_queue") or 0)
    if removed:
        lines.append(f"Убрано из Redis-очереди: *{removed}*")
    return "\n".join(lines)


async def ask_creator_card_cancel_autopilot(callback: CallbackQuery, creator_id: int):
    text = (
        "⚠️ *Отменить очередь автокомментинга по этому блогеру?*\n\n"
        "Будут отменены ожидающие auto-заказы и события создания заказов только по этому блогеру. "
        "Видео из ещё не начатых заказов вернутся в новые, деньги за них вернутся на баланс.\n\n"
        "Сам блогер останется включён, его режим не изменится."
    )
    if callback.message:
        await safe_edit_text(
            callback.message,
            text,
            reply_markup=InlineKeyboardMarkup(
                inline_keyboard=[
                    [InlineKeyboardButton(text="✅ Отменить очередь", callback_data=f"vp:c:{creator_id}:apc:do")],
                    [InlineKeyboardButton(text="⛔ Очередь + текущий заказ", callback_data=f"vp:c:{creator_id}:apc:do:run")],
                    [InlineKeyboardButton(text="◀️ Назад к блогеру", callback_data=f"vp:creator:{creator_id}:detail")],
                ]
            ),
            parse_mode="Markdown",
        )
    await callback.answer()


async def do_creator_card_cancel_autopilot(
    callback: CallbackQuery,
    creator_id: int,
    *,
    include_running: bool = False,
):
    user_id = await get_user_backend_id(callback.from_user.id)
    if not user_id:
        await callback.answer("❌ Ошибка: пользователь не найден", show_alert=True)
        return

    try:
        result = await api_client.cancel_autopilot_work(
            user_id,
            creator_id=creator_id,
            include_running=include_running,
        )
        if callback.message:
            await safe_edit_text(
                callback.message,
                _build_creator_cancel_result_message(result),
                reply_markup=InlineKeyboardMarkup(
                    inline_keyboard=[
                        [InlineKeyboardButton(text="👤 К блогеру", callback_data=f"vp:creator:{creator_id}:detail")],
                        [InlineKeyboardButton(text="📋 Журнал", callback_data="ap:rep")],
                    ]
                ),
                parse_mode="Markdown",
            )
        await callback.answer("Готово")
    except APIRequestError as e:
        logger.error("Error cancelling creator card autopilot work: %s", e, exc_info=True)
        await callback.answer("❌ Не удалось отменить очередь блогера", show_alert=True)
    except Exception as e:
        logger.error("Unexpected error cancelling creator card autopilot work: %s", e, exc_info=True)
        await callback.answer("❌ Ошибка отмены", show_alert=True)


async def show_creator_detail(callback: CallbackQuery, creator_id: int):
    """ИСПРАВЛЕНО: Показывает детали блогера с улучшенным оформлением."""
    try:
        if not callback.message:
            await callback.answer("Ошибка: сообщение не найдено", show_alert=True)
            return
        
        await callback.answer()
        
        # ИСПРАВЛЕНО: Получаем user_id для получения персонального блогера
        user_id = await get_user_backend_id(callback.from_user.id)
        if not user_id:
            await callback.answer("❌ Ошибка: пользователь не найден", show_alert=True)
            return
        
        creators = await api_client.list_video_creators(user_id)  # ИСПРАВЛЕНО: Передаём user_id
        creator = next((c for c in creators if c["id"] == creator_id), None)
        
        if not creator:
            await callback.answer("❌ Блогер не найден", show_alert=True)
            return
        
        # ИСПРАВЛЕНО: Улучшенное оформление карточки блогера
        username = markdown_handle(creator.get("username", "unknown"))
        enabled = creator.get('enabled', False)
        mode = creator.get('mode', 'manual_review')
        interval = creator.get('interval_minutes', 30)

        # Check if adaptive is enabled — show "🧠 авто" instead of useless flat interval
        adaptive_enabled = False
        try:
            adaptive_settings = await api_client.get_adaptive_settings()
            adaptive_enabled = bool(adaptive_settings.get("adaptive_enabled"))
        except Exception:
            adaptive_enabled = False
        parsing_mode = await is_parsing_mode(telegram_id=callback.from_user.id)

        # Форматируем режим
        mode_names = {
            'auto_comment': '🤖 Авто-комментарий',
            'manual_review': '👀 Ручная проверка',
            'notify_only': '🔔 Только уведомления'
        }
        mode_display = mode_names.get(mode, mode)
        if parsing_mode:
            mode_display = "🔔 Только уведомления"

        message = f"🎬 *Блогер*\n{username}\n\n"
        message += f"{'✅ Включён' if enabled else '⏸ Выключен'}\n"
        message += f"📊 *Режим:* {mode_display}\n"
        if adaptive_enabled:
            message += "⏱ *Интервал:* 🧠 _адаптивный_\n\n"
        else:
            message += f"⏱ *Интервал:* {interval} мин\n\n"
        
        # Последняя проверка
        last_check = creator.get("last_checked_at")
        last_check_str = _format_last_check(last_check)
        
        message += f"🕐 *Последняя проверка:* {last_check_str}\n"
        
        # Последнее видео
        last_video_id = creator.get('last_seen_video_id')
        if last_video_id:
            message += f"🎥 *Последнее видео:* `{last_video_id[:12]}…`\n"
        else:
            message += f"🎥 *Последнее видео:* нет\n"
        
        # P1 FIX: Количество новых видео
        new_videos_count = creator.get("new_videos_count", 0)
        if new_videos_count > 0:
            message += f"\n🆕 *Новых видео:* {new_videos_count}\n"
        else:
            message += f"\n🆕 *Новых видео:* нет\n"
        
        if not parsing_mode:
            # Шаблоны
            default_template_id = creator.get("default_template_id")
            template_ids = creator.get("template_ids") or []
            if default_template_id:
                message += f"\n⭐ *Авто-ответ:* шаблон настроен\n"
                if template_ids:
                    message += f"🧩 *Шаблоны:* {len(template_ids)} шт.\n"
            elif template_ids:
                message += f"\n🧩 *Шаблоны:* {len(template_ids)} шт. (авто-ответ не выбран)\n"
            else:
                message += f"\n🧩 *Шаблоны:* не настроены\n"
        
        await safe_edit_text(
            callback.message,
            message,
            reply_markup=get_creator_keyboard(
                creator_id,
                enabled,
                new_videos_count,
                parsing_mode=parsing_mode,
                adaptive_enabled=adaptive_enabled,
            ),
            parse_mode="Markdown"
        )
    except Exception as e:
        logger.error(f"Error showing creator detail: {e}", exc_info=True)
        await callback.answer("❌ Ошибка", show_alert=True)


async def toggle_creator(callback: CallbackQuery, creator_id: int):
    """ИСПРАВЛЕНО: Включает/выключает блогера. Только владелец может изменять своего блогера."""
    try:
        if not callback.message:
            await callback.answer("Ошибка: сообщение не найдено", show_alert=True)
            return
        
        # ИСПРАВЛЕНО: Получаем user_id для получения персонального блогера
        user_id = await get_user_backend_id(callback.from_user.id)
        if not user_id:
            await callback.answer("❌ Ошибка: пользователь не найден", show_alert=True)
            return
        
        creators = await api_client.list_video_creators(user_id)  # ИСПРАВЛЕНО: Передаём user_id
        creator = next((c for c in creators if c["id"] == creator_id), None)
        
        if not creator:
            await callback.answer("❌ Блогер не найден", show_alert=True)
            return
        
        new_enabled = not creator.get("enabled", False)
        
        await api_client.update_video_creator(creator_id, user_id, enabled=new_enabled)  # ИСПРАВЛЕНО: Передаём user_id
        await callback.answer(f"✅ Блогер {'включён' if new_enabled else 'выключен'}", show_alert=False)
        
        # Обновляем детали
        await show_creator_detail(callback, creator_id)
    except Exception as e:
        logger.error(f"Error toggling creator: {e}", exc_info=True)
        await callback.answer("❌ Ошибка при изменении статуса", show_alert=True)


async def show_mode_selection(callback: CallbackQuery, creator_id: int):
    """Показывает выбор режима работы."""
    try:
        await callback.answer()
        await safe_edit_text(
            callback.message,
            "✏️ *Выбор режима работы*\n\n"
            "• 🤖 Авто-комментарий: автоматически создаёт задачи при новых видео\n"
            "• 👀 Ручная проверка: отправляет видео на подтверждение\n"
            "• 🔔 Только уведомления: только уведомляет о новых видео",
            reply_markup=get_mode_selection_keyboard(creator_id),
            parse_mode="Markdown"
        )
    except Exception as e:
        logger.error(f"Error showing mode selection: {e}", exc_info=True)
        await callback.answer("Ошибка", show_alert=True)


@router.callback_query(F.data.startswith("vp:creator:") & F.data.contains(":mode:set:"))
async def set_creator_mode(callback: CallbackQuery):
    """ИСПРАВЛЕНО: Устанавливает режим работы блогера. Только владелец может изменить режим."""
    try:
        if not callback.message:
            await callback.answer("Ошибка: сообщение не найдено", show_alert=True)
            return
        
        # ИСПРАВЛЕНО: Получаем user_id для проверки прав
        user_id = await get_user_backend_id(callback.from_user.id)
        if not user_id:
            await callback.answer("❌ Ошибка: пользователь не найден", show_alert=True)
            return
        
        parts = callback.data.split(":")
        creator_id = int(parts[2])
        mode = parts[-1]  # auto_comment, manual_review, notify_only
        
        # ИСПРАВЛЕНО: Валидация режима
        if mode not in ["auto_comment", "manual_review", "notify_only"]:
            await callback.answer("❌ Неверный режим", show_alert=True)
            return
        
        await api_client.update_video_creator(creator_id, user_id, mode=mode)  # ИСПРАВЛЕНО: Передаём user_id
        await callback.answer(f"✅ Режим установлен", show_alert=False)
        
        # Возвращаемся к деталям
        await show_creator_detail(callback, creator_id)
    except ValueError as e:
        logger.error(f"Invalid creator_id or mode in set_creator_mode: {e}", exc_info=True)
        await callback.answer("❌ Неверный ID или режим", show_alert=True)
    except Exception as e:
        logger.error(f"Error setting creator mode: {e}", exc_info=True)
        await callback.answer("❌ Ошибка при установке режима", show_alert=True)


async def run_creator_now(callback: CallbackQuery, creator_id: int):
    """Запускает проверку блогера немедленно с ограничением частоты вызовов."""
    try:
        if not callback.message:
            await callback.answer("Ошибка: сообщение не найдено", show_alert=True)
            return

        user_id = await get_user_backend_id(callback.from_user.id)
        if not user_id:
            await callback.answer("❌ Ошибка: пользователь не найден", show_alert=True)
            return

        # Rate limit: не чаще раза в минуту на пользователя × блогера
        cooldown_key = (callback.from_user.id, creator_id)
        now_ts = _time.monotonic()
        last_run = _run_now_last.get(cooldown_key, 0.0)
        remaining = _RUN_NOW_COOLDOWN_SECONDS - (now_ts - last_run)
        if remaining > 0:
            await callback.answer(
                f"⏳ Подождите ещё {int(remaining)} сек. перед повторным запуском",
                show_alert=True,
            )
            return

        result = await api_client.run_video_creator_now(creator_id, user_id)
        _run_now_last[cooldown_key] = now_ts

        api_message = (result or {}).get("message", "")
        if "already being checked" in api_message:
            answer_text = "⏳ Проверка уже идёт"
        elif "recently checked" in api_message:
            answer_text = "⏳ Недавно проверяли, запустится после паузы"
        else:
            answer_text = "✅ Проверка поставлена в ближайший цикл"
        await callback.answer(answer_text, show_alert=False)

        await show_creator_detail(callback, creator_id)
    except Exception as e:
        logger.error(f"Error running creator now: {e}", exc_info=True)
        await callback.answer("❌ Ошибка при запуске проверки", show_alert=True)


async def set_creator_interval_start(callback: CallbackQuery, creator_id: int, state: FSMContext):
    """ИСПРАВЛЕНО: Начинает процесс настройки интервала."""
    try:
        if not callback.message:
            await callback.answer("Ошибка: сообщение не найдено", show_alert=True)
            return

        adaptive_enabled = False
        try:
            adaptive_settings = await api_client.get_adaptive_settings()
            adaptive_enabled = bool(adaptive_settings.get("adaptive_enabled"))
        except Exception:
            pass

        if adaptive_enabled and await is_parsing_mode(telegram_id=callback.from_user.id):
            await state.clear()
            await callback.answer("Интервал подбирается автоматически", show_alert=True)
            return

        await callback.answer()
        await state.set_state(VideoParsingStates.INPUT_INTERVAL)
        await state.update_data(
            creator_id=creator_id,
            pending_new_creator_username=None,
            pending_new_creator_from_suggestion=False,
            pending_new_creator_suggestions_page=None,
        )

        current_interval = await get_creator_interval(callback.from_user.id, creator_id)

        message = "⏱ *Настройка интервала*\n\n"
        message += f"Текущий интервал: `{current_interval}` мин\n\n"
        if adaptive_enabled:
            message += (
                "⚠️ _Адаптивный режим включён — бот подбирает интервал автоматически._\n"
                "_Значение ниже используется как резервный fallback, если адаптивный алгоритм ещё не откалиброван._\n\n"
            )
        message += f"Введите число от {MIN_CREATOR_INTERVAL_MINUTES} до {MAX_CREATOR_INTERVAL_MINUTES} минут:"

        kb = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="◀️ Отмена", callback_data=f"vp:creator:{creator_id}:detail")]
        ])
        await safe_edit_text(callback.message, message, reply_markup=kb, parse_mode="Markdown")
    except Exception as e:
        logger.error(f"Error starting interval setup: {e}", exc_info=True)
        await callback.answer("❌ Ошибка", show_alert=True)


async def get_creator_interval(telegram_user_id: int, creator_id: int) -> int:
    """ИСПРАВЛЕНО: Получает текущий интервал блогера для конкретного пользователя."""
    try:
        user_id = await get_user_backend_id(telegram_user_id)
        if not user_id:
            return 30
        
        creators = await api_client.list_video_creators(user_id)  # ИСПРАВЛЕНО: Передаём user_id
        creator = next((c for c in creators if c["id"] == creator_id), None)
        return creator.get("interval_minutes", 30) if creator else 30
    except Exception as e:
        logger.error(f"Error getting creator interval: {e}", exc_info=True)
        return 30


@router.callback_query(F.data.startswith("vp:add_interval:"))
async def add_creator_interval_preset(callback: CallbackQuery, state: FSMContext):
    """Создаёт нового блогера с интервалом, выбранным кнопкой."""
    try:
        if not callback.message:
            await callback.answer("Ошибка: сообщение не найдено", show_alert=True)
            return

        try:
            interval = int(callback.data.split(":")[-1])
        except (TypeError, ValueError):
            await callback.answer("❌ Неверный интервал", show_alert=True)
            return

        if interval < MIN_CREATOR_INTERVAL_MINUTES or interval > MAX_CREATOR_INTERVAL_MINUTES:
            await callback.answer(
                f"❌ Интервал должен быть от {MIN_CREATOR_INTERVAL_MINUTES} до {MAX_CREATOR_INTERVAL_MINUTES} минут",
                show_alert=True,
            )
            return

        data = await state.get_data()
        if not data.get("pending_new_creator_username"):
            await callback.answer("❌ Не найден username блогера", show_alert=True)
            await state.clear()
            return

        await callback.answer()
        await _create_pending_creator_with_interval(
            callback.message,
            state,
            interval,
            telegram_user_id=callback.from_user.id,
            edit_result=True,
        )
    except Exception as e:
        logger.error(f"Error creating creator from interval preset: {e}", exc_info=True)
        await callback.answer("Ошибка", show_alert=True)


@router.message(StateFilter(VideoParsingStates.INPUT_INTERVAL))
async def set_creator_interval_process(message: Message, state: FSMContext):
    """ИСПРАВЛЕНО: Обрабатывает ввод интервала. Только владелец может изменить интервал."""
    try:
        data = await state.get_data()
        creator_id = data.get("creator_id")

        try:
            interval = int(message.text.strip())
            if interval < MIN_CREATOR_INTERVAL_MINUTES or interval > MAX_CREATOR_INTERVAL_MINUTES:
                await message.answer(
                    f"❌ Интервал должен быть от {MIN_CREATOR_INTERVAL_MINUTES} до {MAX_CREATOR_INTERVAL_MINUTES} минут. Попробуйте ещё раз:"
                )
                return
        except ValueError:
            await message.answer(
                f"❌ Введите число от {MIN_CREATOR_INTERVAL_MINUTES} до {MAX_CREATOR_INTERVAL_MINUTES}. Попробуйте ещё раз:"
            )
            return

        if data.get("pending_new_creator_username"):
            await _create_pending_creator_with_interval(message, state, interval)
            return

        if not creator_id:
            await message.answer("❌ Ошибка: не найден ID блогера. Попробуйте снова.")
            await state.clear()
            return

        # Server-side FSM guard: if adaptive is enabled, changing interval for an existing
        # creator is a no-op (the algorithm ignores it). Block here regardless of how the
        # FSM was entered (could be from a stale old Telegram message).
        try:
            _adaptive_cfg = await api_client.get_adaptive_settings()
            if _adaptive_cfg.get("adaptive_enabled"):
                await state.clear()
                await message.answer(
                    "⚠️ Адаптивный режим включён — интервал подбирается автоматически.\n"
                    "Ручное изменение для существующего блогера не применяется."
                )
                return
        except Exception:
            pass  # If settings unavailable, allow the update (best-effort)

        # Получаем user_id для проверки прав
        user_id = await get_user_backend_id(message.from_user.id)
        if not user_id:
            await message.answer("❌ Ошибка: пользователь не найден.")
            await state.clear()
            return
        
        await api_client.update_video_creator(creator_id, user_id, interval_minutes=interval)  # ИСПРАВЛЕНО: Передаём user_id
        await state.clear()
        
        await message.answer(
            f"✅ Интервал обновлён: {interval} минут",
            reply_markup=get_video_parsing_menu_keyboard(parsing_mode=await is_parsing_mode(telegram_id=message.from_user.id))
        )
    except Exception as e:
        logger.error(f"Error setting interval: {e}", exc_info=True)
        await message.answer(f"❌ Ошибка при установке интервала: {str(e)[:100]}")
        await state.clear()


async def set_creator_templates_start(
    callback: CallbackQuery,
    creator_id: int,
    state: FSMContext,
    page: int = 0,
    *,
    answer: bool = True,
):
    """Начинает процесс настройки шаблонов."""
    try:
        if answer:
            await callback.answer()
        
        # Получаем список шаблонов пользователя
        user_id = await get_user_backend_id(callback.from_user.id)
        
        if not user_id:
            await callback.answer("❌ Пользователь не найден", show_alert=True)
            return
        
        templates = filter_full_templates(await api_client.list_task_templates(user_id))
        
        if not templates:
            await callback.answer("❌ У вас нет шаблонов. Создайте шаблоны сначала.", show_alert=True)
            return
        
        # ИСПРАВЛЕНО: Получаем информацию о блогере один раз (используя user_id)
        creators = await api_client.list_video_creators(user_id)  # ИСПРАВЛЕНО: Передаём user_id
        creator = next((c for c in creators if c["id"] == creator_id), None)
        if not creator:
            await callback.answer("❌ Блогер не найден", show_alert=True)
            return
        
        selected_template_ids = set(creator.get("template_ids") or [])
        total_pages = max(1, (len(templates) + CREATOR_TEMPLATE_PICKER_PER_PAGE - 1) // CREATOR_TEMPLATE_PICKER_PER_PAGE)
        page = max(0, min(page, total_pages - 1))
        await state.update_data(creator_template_page=page)
        start = page * CREATOR_TEMPLATE_PICKER_PER_PAGE
        page_templates = templates[start:start + CREATOR_TEMPLATE_PICKER_PER_PAGE]
        
        # ИСПРАВЛЕНО: Улучшенное оформление списка шаблонов
        from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton
        keyboard = []
        
        message = f"🧩 *Настройка шаблонов*\n\n"
        safe_username = markdown_handle(creator.get("username", "unknown"))
        message += f"Выберите шаблоны для блогера {safe_username}:\n\n"
        
        if total_pages > 1:
            message += f"Страница *{page + 1}*/{total_pages}\n\n"

        for template in page_templates:
            template_id = template["id"]
            template_name = template.get("name", f"Template {template_id}")
            safe_template_name = escape_markdown(template_name)
            selected = template_id in selected_template_ids
            emoji = "✅" if selected else "⚪"
            
            keyboard.append([
                InlineKeyboardButton(
                    text=f"{emoji} {template_name}",
                    callback_data=f"vp:creator:{creator_id}:template:toggle:{template_id}"
                )
            ])
            message += f"{emoji} {safe_template_name}\n"
        
        if total_pages > 1:
            nav = []
            if page > 0:
                nav.append(InlineKeyboardButton(text="◀️", callback_data=f"vp:creator:{creator_id}:template:page:{page - 1}"))
            if page < total_pages - 1:
                nav.append(InlineKeyboardButton(text="▶️", callback_data=f"vp:creator:{creator_id}:template:page:{page + 1}"))
            if nav:
                keyboard.append(nav)
        keyboard.append([InlineKeyboardButton(text="✅ Готово", callback_data=f"vp:creator:{creator_id}:templates:save")])
        keyboard.append([InlineKeyboardButton(text="⬅️ Назад", callback_data=f"vp:creator:{creator_id}:detail")])
        
        await safe_edit_text(
            callback.message,
            message,
            reply_markup=InlineKeyboardMarkup(inline_keyboard=keyboard),
            parse_mode="Markdown"
        )
    except Exception as e:
        logger.error(f"Error starting templates setup: {e}", exc_info=True)
        await callback.answer("Ошибка", show_alert=True)


@router.callback_query(F.data.startswith("vp:creator:") & F.data.contains(":template:page:"))
async def creator_template_page(callback: CallbackQuery, state: FSMContext):
    try:
        parts = callback.data.split(":")
        creator_id = int(parts[2])
        page = max(0, int(parts[5]))
        await set_creator_templates_start(callback, creator_id, state, page=page)
    except Exception as e:
        logger.error(f"Error switching creator template page: {e}", exc_info=True)
        await callback.answer("Ошибка", show_alert=True)


@router.callback_query(F.data.startswith("vp:creator:") & F.data.contains(":template:toggle:"))
async def toggle_template_for_creator(callback: CallbackQuery, state: FSMContext):
    """ИСПРАВЛЕНО: Переключает шаблон для блогера. Только владелец может изменить шаблоны."""
    try:
        if not callback.message:
            await callback.answer("Ошибка: сообщение не найдено", show_alert=True)
            return
        
        await callback.answer()  # Убираем задержку для пользователя
        
        # ИСПРАВЛЕНО: Получаем user_id для проверки прав
        user_id = await get_user_backend_id(callback.from_user.id)
        if not user_id:
            await callback.answer("❌ Ошибка: пользователь не найден", show_alert=True)
            return
        
        parts = callback.data.split(":")
        creator_id = int(parts[2])
        template_id = int(parts[5])
        templates = filter_full_templates(await api_client.list_task_templates(user_id))
        if not any(int(item.get("id") or 0) == template_id for item in templates):
            await callback.answer("Этот шаблон подходит только для заказов на ответы", show_alert=True)
            return
        
        # ИСПРАВЛЕНО: Получаем текущие шаблоны (один запрос) используя user_id
        creators = await api_client.list_video_creators(user_id)  # ИСПРАВЛЕНО: Передаём user_id
        creator = next((c for c in creators if c["id"] == creator_id), None)
        
        if not creator:
            await callback.answer("❌ Блогер не найден", show_alert=True)
            return
        
        current_templates = list(creator.get("template_ids") or [])
        
        # Переключаем
        if template_id in current_templates:
            current_templates.remove(template_id)
            action_msg = "удалён"
        else:
            current_templates.append(template_id)
            action_msg = "добавлен"
        
        # ИСПРАВЛЕНО: Обновляем с передачей user_id
        await api_client.update_video_creator(creator_id, user_id, template_ids=current_templates)  # ИСПРАВЛЕНО: Передаём user_id
        await callback.answer(f"✅ Шаблон {action_msg}", show_alert=False)  # Не показываем alert для быстрой работы
        
        # Обновляем список на той же странице.
        state_data = await state.get_data()
        page = int(state_data.get("creator_template_page") or 0)
        await set_creator_templates_start(callback, creator_id, state, page=page, answer=False)
    except ValueError as e:
        logger.error(f"Invalid creator_id or template_id in toggle: {e}", exc_info=True)
        await callback.answer("❌ Неверный ID", show_alert=True)
    except Exception as e:
        logger.error(f"Error toggling template: {e}", exc_info=True)
        await callback.answer("❌ Ошибка", show_alert=True)


@router.callback_query(F.data.startswith("vp:creator:") & F.data.contains(":templates:save"))
async def save_creator_templates(callback: CallbackQuery):
    """Сохраняет шаблоны и возвращается к деталям блогера."""
    try:
        creator_id = int(callback.data.split(":")[2])
        await callback.answer("✅ Шаблоны сохранены", show_alert=True)
        await show_creator_detail(callback, creator_id)
    except Exception as e:
        logger.error(f"Error saving templates: {e}", exc_info=True)
        await callback.answer("Ошибка", show_alert=True)


async def set_creator_default_template_start(
    callback: CallbackQuery,
    creator_id: int,
    page: int = 0,
    *,
    answer: bool = True,
):
    """P1 FIX: Показывает список шаблонов для выбора шаблона по умолчанию."""
    try:
        if answer:
            await callback.answer()
        
        # Получаем список шаблонов пользователя
        user_id = await get_user_backend_id(callback.from_user.id)
        
        if not user_id:
            await callback.answer("❌ Пользователь не найден", show_alert=True)
            return
        
        templates = filter_full_templates(await api_client.list_task_templates(user_id))
        
        if not templates:
            await callback.answer("❌ У вас нет шаблонов. Создайте шаблоны сначала.", show_alert=True)
            return
        
        # Получаем информацию о блогере
        creators = await api_client.list_video_creators(user_id)
        creator = next((c for c in creators if c["id"] == creator_id), None)
        if not creator:
            await callback.answer("❌ Блогер не найден", show_alert=True)
            return
        
        current_default_template_id = creator.get("default_template_id")
        total_pages = max(1, (len(templates) + CREATOR_TEMPLATE_PICKER_PER_PAGE - 1) // CREATOR_TEMPLATE_PICKER_PER_PAGE)
        page = max(0, min(page, total_pages - 1))
        start = page * CREATOR_TEMPLATE_PICKER_PER_PAGE
        page_templates = templates[start:start + CREATOR_TEMPLATE_PICKER_PER_PAGE]
        
        # Формируем клавиатуру
        from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton
        keyboard = []
        
        message = f"📑 *Шаблон по умолчанию*\n\n"
        safe_username = markdown_handle(creator.get("username", "unknown"))
        message += f"Выберите шаблон для блогера {safe_username}:\n\n"
        
        # Добавляем опцию "Убрать шаблон по умолчанию"
        if current_default_template_id:
            keyboard.append([
                InlineKeyboardButton(
                    text="❌ Убрать шаблон по умолчанию",
                    callback_data=f"vp:creator:{creator_id}:default_template:set:0"
                )
            ])
            message += "❌ Убрать шаблон по умолчанию\n\n"
        
        if total_pages > 1:
            message += f"Страница *{page + 1}*/{total_pages}\n\n"

        for template in page_templates:
            template_id = template["id"]
            template_name = template.get("name", f"Template {template_id}")
            safe_template_name = escape_markdown(template_name)
            selected = template_id == current_default_template_id
            emoji = "✅" if selected else "⚪"
            
            keyboard.append([
                InlineKeyboardButton(
                    text=f"{emoji} {template_name}",
                    callback_data=f"vp:creator:{creator_id}:default_template:set:{template_id}"
                )
            ])
            message += f"{emoji} {safe_template_name}\n"
        
        if total_pages > 1:
            nav = []
            if page > 0:
                nav.append(InlineKeyboardButton(text="◀️", callback_data=f"vp:creator:{creator_id}:default_template:page:{page - 1}"))
            if page < total_pages - 1:
                nav.append(InlineKeyboardButton(text="▶️", callback_data=f"vp:creator:{creator_id}:default_template:page:{page + 1}"))
            if nav:
                keyboard.append(nav)
        keyboard.append([InlineKeyboardButton(text="⬅️ Назад", callback_data=f"vp:creator:{creator_id}:detail")])
        
        await safe_edit_text(
            callback.message,
            message,
            reply_markup=InlineKeyboardMarkup(inline_keyboard=keyboard),
            parse_mode="Markdown"
        )
    except Exception as e:
        logger.error(f"Error starting default template setup: {e}", exc_info=True)
        await callback.answer("Ошибка", show_alert=True)


@router.callback_query(F.data.startswith("vp:creator:") & F.data.contains(":default_template:page:"))
async def creator_default_template_page(callback: CallbackQuery):
    try:
        parts = callback.data.split(":")
        creator_id = int(parts[2])
        page = max(0, int(parts[5]))
        await set_creator_default_template_start(callback, creator_id, page=page)
    except Exception as e:
        logger.error(f"Error switching creator default template page: {e}", exc_info=True)
        await callback.answer("Ошибка", show_alert=True)


@router.callback_query(F.data.startswith("vp:creator:") & F.data.contains(":default_template:set:"))
async def set_creator_default_template(callback: CallbackQuery):
    """P1 FIX: Устанавливает шаблон по умолчанию для блогера."""
    try:
        if not callback.message:
            await callback.answer("Ошибка: сообщение не найдено", show_alert=True)
            return
        
        await callback.answer()
        
        # Получаем user_id для проверки прав
        user_id = await get_user_backend_id(callback.from_user.id)
        if not user_id:
            await callback.answer("❌ Ошибка: пользователь не найден", show_alert=True)
            return
        
        parts = callback.data.split(":")
        creator_id = int(parts[2])
        template_id = int(parts[5])
        if template_id > 0:
            templates = filter_full_templates(await api_client.list_task_templates(user_id))
            if not any(int(item.get("id") or 0) == template_id for item in templates):
                await callback.answer("Этот шаблон подходит только для заказов на ответы", show_alert=True)
                return
        
        # Обновляем default_template_id (0 означает удаление)
        await api_client.update_video_creator(creator_id, user_id, default_template_id=template_id)
        
        if template_id > 0:
            await callback.answer("✅ Шаблон по умолчанию установлен", show_alert=False)
        else:
            await callback.answer("✅ Шаблон по умолчанию убран", show_alert=False)
        
        # Возвращаемся к деталям блогера
        await show_creator_detail(callback, creator_id)
    except ValueError as e:
        logger.error(f"Invalid creator_id or template_id in set default template: {e}", exc_info=True)
        await callback.answer("❌ Неверный ID", show_alert=True)
    except Exception as e:
        logger.error(f"Error setting default template: {e}", exc_info=True)
        await callback.answer("❌ Ошибка", show_alert=True)


async def delete_creator_confirm(callback: CallbackQuery, creator_id: int, state: FSMContext):
    """ИСПРАВЛЕНО: Подтверждает удаление блогера. Только владелец может удалить своего блогера."""
    try:
        if not callback.message:
            await callback.answer("Ошибка: сообщение не найдено", show_alert=True)
            return
        
        await callback.answer()
        await state.set_state(VideoParsingStates.DELETE_CONFIRM)
        await state.update_data(creator_id=creator_id)
        
        # ИСПРАВЛЕНО: Получаем user_id для получения персонального блогера
        user_id = await get_user_backend_id(callback.from_user.id)
        if not user_id:
            await callback.answer("❌ Ошибка: пользователь не найден", show_alert=True)
            return
        
        creators = await api_client.list_video_creators(user_id)  # ИСПРАВЛЕНО: Передаём user_id
        creator = next((c for c in creators if c["id"] == creator_id), None)
        
        if not creator:
            await callback.answer("❌ Блогер не найден", show_alert=True)
            return
        
        from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton
        # Buttons on separate rows so a thumb-tap on mobile doesn't accidentally
        # confirm deletion. Use ✅ on the safe (cancel) action and 🗑 on the
        # destructive one — the previous mapping had ✅ on Cancel which trains
        # users to associate the green check with "ok proceed", a real footgun.
        keyboard = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="◀️ Отмена", callback_data=f"vp:creator:{creator_id}:detail")],
            [InlineKeyboardButton(text="🗑 Да, удалить", callback_data=f"vp:creator:{creator_id}:delete:confirm")],
        ])

        message = "🗑 *Удаление блогера*\n\n"
        safe_username = markdown_handle(creator.get("username", "unknown"))
        new_videos_count = int(creator.get("new_videos_count", 0) or 0)
        message += f"Вы уверены, что хотите удалить {safe_username}?\n\n"
        if new_videos_count:
            message += f"⚠️ У этого блогера *{new_videos_count}* непросмотренных видео — они тоже удалятся.\n\n"
        message += "Это действие нельзя отменить."
        
        await safe_edit_text(callback.message, message, reply_markup=keyboard, parse_mode="Markdown")
    except Exception as e:
        logger.error(f"Error confirming delete: {e}", exc_info=True)
        await callback.answer("Ошибка", show_alert=True)


@router.callback_query(F.data.startswith("vp:creator:") & F.data.contains(":delete:confirm"))
async def delete_creator_execute(callback: CallbackQuery, state: FSMContext):
    """ИСПРАВЛЕНО: Выполняет удаление блогера. Только владелец может удалить своего блогера."""
    try:
        if not callback.message:
            await callback.answer("Ошибка: сообщение не найдено", show_alert=True)
            return
        
        creator_id = int(callback.data.split(":")[2])
        
        # ИСПРАВЛЕНО: Получаем user_id для проверки прав
        user_id = await get_user_backend_id(callback.from_user.id)
        if not user_id:
            await callback.answer("❌ Ошибка: пользователь не найден", show_alert=True)
            await state.clear()
            return
        
        # Получаем username для сообщения (используя user_id)
        creators = await api_client.list_video_creators(user_id)  # ИСПРАВЛЕНО: Передаём user_id
        creator = next((c for c in creators if c["id"] == creator_id), None)
        username = creator.get("username", "unknown") if creator else "unknown"
        
        await api_client.delete_video_creator(creator_id, user_id)  # ИСПРАВЛЕНО: Передаём user_id
        await state.clear()
        
        await callback.answer(f"✅ Блогер @{username} удалён", show_alert=False)
        
        # Возвращаемся к списку
        await list_creators(callback)
    except ValueError as e:
        logger.error(f"Invalid creator_id in delete_creator_execute: {e}", exc_info=True)
        await callback.answer("❌ Неверный ID", show_alert=True)
        await state.clear()
    except Exception as e:
        logger.error(f"Error deleting creator: {e}", exc_info=True)
        await callback.answer(f"❌ Ошибка при удалении: {str(e)[:50]}", show_alert=True)
        await state.clear()


@router.callback_query(F.data == "vp:auto_orders:list")
async def show_auto_orders_list(callback: CallbackQuery):
    """ПЕРЕДЕЛАНО: Показывает список автоматически созданных заказов."""
    try:
        if not callback.message:
            await callback.answer("Ошибка: сообщение не найдено", show_alert=True)
            return
        
        await callback.answer()
        
        # Получаем user_id
        user_id = await get_user_backend_id(callback.from_user.id)
        if not user_id:
            await callback.answer("❌ Ошибка: пользователь не найден", show_alert=True)
            return
        
        # Получаем автоматические заказы
        orders = await api_client.get_auto_orders(user_id, limit=50)
        
        if not orders:
            message = "📋 *Автоматические заказы*\n\n"
            message += "Нет автоматических заказов.\n\n"
            message += "Заказы будут появляться здесь после автоматического создания из видео парсинга."
            
            await safe_edit_text(
                callback.message,
                message,
                reply_markup=get_video_parsing_menu_keyboard(parsing_mode=await is_parsing_mode(telegram_id=callback.from_user.id)),
                parse_mode="Markdown"
            )
            return

        # Форматируем список заказов
        message = f"📋 *Автоматические заказы*\n\n"
        message += f"Всего заказов: {len(orders)}\n\n"
        
        for i, order in enumerate(orders[:10], 1):
            task_id = order.get("task_id", 0)
            status = order.get("status", "unknown")
            video_count = order.get("video_count", 0)
            total_cost = order.get("total_cost", 0.0)
            creator_usernames = order.get("creator_usernames", [])
            
            status_emoji = "✅" if status == "done" else "⏳" if status == "in_progress" else "❌"
            creators_str = ", ".join(markdown_handle(u) for u in creator_usernames[:2])
            if len(creator_usernames) > 2:
                creators_str += f" и ещё {len(creator_usernames) - 2}"
            
            message += f"{i}. {status_emoji} *Заказ #{task_id}*\n"
            message += f"   Видео: {video_count}, Стоимость: {total_cost:.2f} грн\n"
            if creators_str:
                message += f"   Блогеры: {creators_str}\n"
            message += "\n"
        
        if len(orders) > 10:
            message += f"… и ещё {len(orders) - 10} заказов\n"
        
        # Создаем клавиатуру с кнопками для каждого заказа
        keyboard = []
        for order in orders[:10]:
            task_id = order.get("task_id", 0)
            keyboard.append([
                InlineKeyboardButton(
                    text=f"📋 Заказ #{task_id}",
                    callback_data=f"order:detail:{task_id}"
                )
            ])
        
        keyboard.append([InlineKeyboardButton(text="⬅️ Назад", callback_data="menu:video_parsing")])
        
        await safe_edit_text(
            callback.message,
            message,
            reply_markup=InlineKeyboardMarkup(inline_keyboard=keyboard),
            parse_mode="Markdown"
        )
    except Exception as e:
        logger.error(f"Error showing auto orders list: {e}", exc_info=True)
        await callback.answer("❌ Ошибка при загрузке списка", show_alert=True)


@router.callback_query(F.data.startswith("vp:review:") & F.data.contains(":approve"))
async def approve_video(callback: CallbackQuery):
    """ИСПРАВЛЕНО: Подтверждает видео и создаёт задачу. Только владелец блогера может подтвердить видео."""
    try:
        if not callback.message:
            await callback.answer("Ошибка: сообщение не найдено", show_alert=True)
            return
        
        # ИСПРАВЛЕНО: Получаем user_id для проверки прав
        user_id = await get_user_backend_id(callback.from_user.id)
        if not user_id:
            await callback.answer("❌ Ошибка: пользователь не найден", show_alert=True)
            return
        
        # Парсим video_id из callback_data: vp:review:{video_id}:approve
        parts = callback.data.split(":")
        video_id = int(parts[2])
        
        # ИСПРАВЛЕНО: Создаём задачу с передачей user_id для проверки прав
        video_line = await _review_video_result_line(video_id, user_id)
        result = await api_client.approve_video(video_id, user_id, template_id=None)  # ИСПРАВЛЕНО: Передаём user_id
        task_id = result.get("task_id")
        
        await callback.answer("✅ Видео подтверждено. Задача создана.", show_alert=False)
        
        # Если callback пришёл из уведомления, обновляем уведомление, иначе показываем очередь
        if callback.message:
            # Проверяем, это уведомление (содержит "Новые видео на проверку") или сообщение из бота
            message_text = callback.message.text or callback.message.caption or ""
            if "Новые видео на проверку" in message_text or "Новое видео на проверку" in message_text:
                # Обновляем уведомление - показываем результат
                await safe_edit_text(
                    callback.message,
                    f"✅ *Видео обработано*\n\n"
                    f"{video_line}\n"
                    f"Задача создана: `#{task_id}`\n\n"
                    f"Перейдите во входящие для обработки других видео.",
                    reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                        [InlineKeyboardButton(text="📥 Перейти во входящие", callback_data="vp:review:list")]
                    ]),
                    parse_mode="Markdown",
                    disable_web_page_preview=True,
                )
            else:
                # P0 FIX: Упростили - просто показываем сообщение об успехе
                # Review queue функционал устарел, используем новый flow через уведомления
                await safe_edit_text(
                    callback.message,
                    f"✅ *Видео обработано*\n\n"
                    f"{video_line}\n"
                    f"Задача создана: `#{task_id}`",
                    parse_mode="Markdown",
                    disable_web_page_preview=True,
                )
        else:
            await callback.answer("✅ Видео подтверждено", show_alert=True)
            
    except Exception as e:
        logger.error(f"Error approving video: {e}", exc_info=True)
        error_msg = str(e)
        if "template" in error_msg.lower():
            await callback.answer("❌ Не настроены шаблоны для блогера", show_alert=True)
        else:
            await callback.answer(f"Ошибка: {error_msg[:50]}", show_alert=True)


@router.callback_query(F.data.startswith("vp:review:") & F.data.contains(":reject"))
async def reject_video(callback: CallbackQuery):
    """ИСПРАВЛЕНО: Отклоняет видео. Только владелец блогера может отклонить видео."""
    try:
        if not callback.message:
            await callback.answer("Ошибка: сообщение не найдено", show_alert=True)
            return
        
        # ИСПРАВЛЕНО: Получаем user_id для проверки прав
        user_id = await get_user_backend_id(callback.from_user.id)
        if not user_id:
            await callback.answer("❌ Ошибка: пользователь не найден", show_alert=True)
            return
        
        # Парсим video_id из callback_data: vp:review:{video_id}:reject
        parts = callback.data.split(":")
        video_id = int(parts[2])
        
        # ИСПРАВЛЕНО: Отклоняем видео с передачей user_id для проверки прав
        video_line = await _review_video_result_line(video_id, user_id)
        await api_client.reject_video(video_id, user_id)  # ИСПРАВЛЕНО: Передаём user_id
        
        await callback.answer("❌ Видео отклонено", show_alert=False)
        
        # Если callback пришёл из уведомления, обновляем уведомление, иначе показываем очередь
        if callback.message:
            # Проверяем, это уведомление (содержит "Новые видео на проверку") или сообщение из бота
            message_text = callback.message.text or callback.message.caption or ""
            if "Новые видео на проверку" in message_text or "Новое видео на проверку" in message_text:
                # Обновляем уведомление - показываем результат
                await safe_edit_text(
                    callback.message,
                    f"❌ *Видео отклонено*\n\n"
                    f"{video_line}\n\n"
                    f"Перейдите во входящие для обработки других видео.",
                    reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                        [InlineKeyboardButton(text="📥 Перейти во входящие", callback_data="vp:review:list")]
                    ]),
                    parse_mode="Markdown",
                    disable_web_page_preview=True,
                )
            else:
                # P0 FIX: Упростили - просто показываем сообщение об успехе
                # Review queue функционал устарел, используем новый flow через уведомления
                await safe_edit_text(
                    callback.message,
                    f"❌ *Видео отклонено*\n\n"
                    f"{video_line}\n\n"
                    f"Видео помечено как отклоненное.",
                    parse_mode="Markdown",
                    disable_web_page_preview=True,
                )
        else:
            await callback.answer("❌ Видео отклонено", show_alert=True)
            
    except Exception as e:
        logger.error(f"Error rejecting video: {e}", exc_info=True)
        await callback.answer("Ошибка при отклонении видео", show_alert=True)


@router.callback_query(F.data.startswith("vp:creator:") & F.data.contains(":new_videos"))
async def show_creator_new_videos(callback: CallbackQuery):
    """P1 FIX: Показывает новые видео конкретного блогера."""
    try:
        await callback.answer()
        
        if not callback.message:
            await callback.answer("Ошибка: сообщение не найдено", show_alert=True)
            return
        
        # P0 FIX: Парсим creator_id из callback_data: vp:creator:{creator_id}:new_videos
        parts = callback.data.split(":")
        if len(parts) < 3:
            await callback.answer("❌ Неверный формат callback_data", show_alert=True)
            return
        creator_id = int(parts[2])
        
        # Получаем user_id
        user_id = await get_user_backend_id(callback.from_user.id)
        if not user_id:
            await callback.answer("❌ Ошибка: пользователь не найден", show_alert=True)
            return
        
        # Получаем новые видео этого блогера
        videos = await api_client.list_video_videos(
            user_id=user_id,
            creator_id=creator_id,
            status="new",
            limit=100
        )
        
        if not videos:
            message = f"🆕 *Новые видео*\n\n"
            message += f"Нет новых видео для этого блогера."
            
            await safe_edit_text(
                callback.message,
                message,
                reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                    [InlineKeyboardButton(text="⬅️ Назад", callback_data=f"vp:creator:{creator_id}:detail")]
                ]),
                parse_mode="Markdown"
            )
            return
        
        # Создаем batch для этих видео
        video_ids = [str(v.get("id")) for v in videos]
        batch_data = await api_client.create_video_batch(creator_id, video_ids, user_id)
        batch_id = batch_data.get("batch_id")
        
        # Получаем информацию о блогере
        creators = await api_client.list_video_creators(user_id)
        creator = next((c for c in creators if c["id"] == creator_id), None)
        
        if not creator:
            await callback.answer("❌ Блогер не найден", show_alert=True)
            return
        
        username = markdown_handle(creator.get("username", "unknown"))
        default_template_id = creator.get("default_template_id")
        
        # Получаем цены
        pricing = await api_client.get_pricing()
        price_per_video = float(pricing.get("price_success", 5.0))
        total_cost = len(videos) * price_per_video
        
        message = f"🆕 *Новые видео*\n\n"
        message += f"Блогер: {username}\n"
        message += f"Видео: {len(videos)}\n"
        message += f"💰 Стоимость: {total_cost:.2f} грн\n\n"
        
        # Показываем первые 5 видео
        for i, video in enumerate(videos[:5], 1):
            message += format_video_markdown_line(video, i) + "\n"
        
        if len(videos) > 5:
            message += f"\n… и ещё {len(videos) - 5} видео\n"
        
        # Формируем кнопки
        keyboard_buttons = []
        
        if default_template_id:
            # Если есть шаблон по умолчанию - можно сразу комментить
                keyboard_buttons.append([
                InlineKeyboardButton(
                    text=f"✅ Комментировать все (шаблон)",
                    callback_data=f"vp:new_videos:template_selected:{creator_id}:{batch_id}:{default_template_id}"
                )
            ])
        
        keyboard_buttons.append([
            InlineKeyboardButton(
                text="📑 Выбрать шаблон",
                callback_data=f"vp:new_videos:select_template:{creator_id}:{batch_id}"
            )
        ])
        
        keyboard_buttons.append([
            InlineKeyboardButton(text="⬅️ Назад", callback_data=f"vp:creator:{creator_id}:detail")
        ])
        
        await safe_edit_text(
            callback.message,
            message,
            reply_markup=InlineKeyboardMarkup(inline_keyboard=keyboard_buttons),
            parse_mode="Markdown",
            disable_web_page_preview=True,
        )
    except ValueError as e:
        logger.error(f"Invalid creator_id in show_creator_new_videos: {e}", exc_info=True)
        await callback.answer("❌ Неверный ID", show_alert=True)
    except Exception as e:
        logger.error(f"Error showing creator new videos: {e}", exc_info=True)
        await callback.answer("❌ Ошибка", show_alert=True)


@router.callback_query(F.data == "vp:review:list")
async def show_review_queue(callback: CallbackQuery, state: FSMContext):
    """P1 FIX: Перенаправляет на Inbox (единая очередь новых видео)."""
    # P1 FIX: Объединяем "Очередь новых видео" с Inbox - это одно и то же
    # Просто вызываем Inbox handler напрямую
    try:
        from bot.handlers.video_inbox import show_inbox
        await show_inbox(callback, state)
    except Exception as e:
        logger.error(f"Error showing review queue: {e}", exc_info=True)
        await callback.answer("❌ Ошибка при загрузке очереди", show_alert=True)


@router.callback_query(F.data.startswith("vp:review:") & F.data.contains(":detail"))
async def show_review_video_detail(callback: CallbackQuery):
    """P1 FIX: Показывает детали одного видео из Inbox (статус new)."""
    try:
        await callback.answer()
        
        if not callback.message:
            await callback.answer("Ошибка: сообщение не найдено", show_alert=True)
            return
        
        # Парсим video_id из callback_data: vp:review:{video_id}:detail
        parts = callback.data.split(":")
        video_id = int(parts[2])
        
        # Получаем user_id
        user_id = await get_user_backend_id(callback.from_user.id)
        if not user_id:
            await callback.answer("❌ Ошибка: пользователь не найден", show_alert=True)
            return
        
        # P1 FIX: Используем Inbox API (статус new) вместо старой review-очереди
        inbox_data = await api_client.get_inbox_videos(user_id, status="new", limit=1000)
        videos = inbox_data.get("videos", [])
        video = next((v for v in videos if v.get("id") == video_id), None)
        
        if not video:
            await callback.answer("❌ Видео не найдено", show_alert=True)
            return
        
        creator_username = video.get("creator_username", "unknown")
        video_id_str = video.get("video_id", "unknown")
        video_url = normalize_video_url(video.get("url", ""))
        
        message = f"🎥 *Видео на проверке*\n\n"
        message += f"Блогер: {markdown_handle(creator_username)}\n"
        video_label = f"[{video_short_id(video, 18)}]({video_url})" if video_url else markdown_inline_code(video_id_str)
        message += f"Видео: {video_label}\n"
        message += f"{escape_markdown(video_time_label(video))}\n"

        if video_url:
            message += f"\n🔗 [Открыть видео]({video_url})\n"
        
        await safe_edit_text(
            callback.message,
            message,
            reply_markup=get_review_video_keyboard(video_id),
            parse_mode="Markdown",
            disable_web_page_preview=True,
        )
    except ValueError as e:
        logger.error(f"Invalid video_id in show_review_video_detail: {e}", exc_info=True)
        await callback.answer("❌ Неверный ID", show_alert=True)
    except Exception as e:
        logger.error(f"Error showing review video detail: {e}", exc_info=True)
        await callback.answer("❌ Ошибка", show_alert=True)


@router.callback_query(F.data.startswith("vp:review:list:page:"))
async def show_review_queue_page(callback: CallbackQuery):
    """Обработчик пагинации очереди на проверку."""
    try:
        page = int(callback.data.split(":")[-1])
        await show_review_queue_with_page(callback, page)
    except Exception as e:
        logger.error(f"Error paginating review queue: {e}", exc_info=True)
        await callback.answer("Ошибка", show_alert=True)


async def show_review_queue_with_page(callback: CallbackQuery, page: int = 0):
    """Показывает очередь на проверку с указанной страницей."""
    if not callback.message:
        await callback.answer("Ошибка: сообщение не найдено", show_alert=True)
        return
    
    # Получаем user_id
    user_id = await get_user_backend_id(callback.from_user.id)
    if not user_id:
        await callback.answer("❌ Ошибка: пользователь не найден", show_alert=True)
        return
    
    videos = await api_client.get_review_queue(user_id, limit=1000)
    await callback.message.edit_reply_markup(
        reply_markup=get_review_queue_keyboard(videos, page=page)
    )
    await callback.answer()


# Импортируем обработчики для нового функционала
from bot.handlers.video_parsing_new_videos import router as new_videos_router
router.include_router(new_videos_router)
