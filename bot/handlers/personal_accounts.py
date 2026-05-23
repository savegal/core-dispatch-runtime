"""Раздел бота «👥 Мои аккаунты» — личный пул работника.

UX:
- pa:menu — главный экран с короткой сводкой и навигацией.
- pa:list:{page} — постраничный список личных аккаунтов.
- pa:item:{id} — карточка аккаунта (статус, лимиты, ошибка, удалить).
- pa:add — старт импорта (FSM PersonalAccountsImport.WAITING_FILE).
- pa:limits — массовые лимиты для всего пула.
- pa:item_limits:{id} — per-account override лимита.
- pa:clear / pa:clear:do — очистка пула.

Архитектурные принципы:
- Все изменения проходят через API (/api/personal-accounts/*) — то же место,
  где идёт жёсткая проверка scope (verify_user_header). Бот сам ничего в БД
  не пишет.
- При импорте reuse уже существующего парсера parse_accounts_file_smart,
  чтобы поддерживать те же форматы, что и админский импорт.
- Лимиты ограничены константами PERSONAL_POOL_MAX_DAILY_*_LIMIT.
"""
from __future__ import annotations

import logging
from dataclasses import asdict
from typing import Any

from aiogram import F, Router
from aiogram.filters import StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import (
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
)

from bot.keyboards.common import get_back_keyboard
from bot.services.api_client import api_client
from bot.services.formatting import markdown_handle as _md_handle
from config.settings import get_settings
from services.smart_account_parser import parse_accounts_file_smart

logger = logging.getLogger("bot.personal_accounts")
router = Router(name="personal_accounts")

PAGE_SIZE = 8


class PersonalAccountsImport(StatesGroup):
    WAITING_INPUT = State()


class PersonalLimitsState(StatesGroup):
    BULK_MAIN = State()
    BULK_REPLIES = State()
    ITEM_MAIN = State()
    ITEM_REPLIES = State()


# ----------------------- helpers -----------------------

async def _resolve_user(callback: CallbackQuery) -> dict | None:
    """Возвращает user_data {id, role, ...} или None.
    На любую ошибку аккуратно отвечает пользователю и пишет в лог."""
    try:
        user_data = await api_client.sync_telegram(
            callback.from_user.id,
            callback.from_user.username,
            use_cache=True,
        )
        return user_data
    except Exception as exc:
        logger.error(f"Personal accounts: failed to sync user: {exc}", exc_info=True)
        try:
            await callback.answer("Ошибка соединения с сервером", show_alert=True)
        except Exception:
            pass
        return None


async def _resolve_user_from_message(message: Message) -> dict | None:
    try:
        return await api_client.sync_telegram(
            message.from_user.id,
            message.from_user.username,
            use_cache=True,
        )
    except Exception as exc:
        logger.error(f"Personal accounts: failed to sync user (msg): {exc}", exc_info=True)
        return None


def _menu_keyboard(has_accounts: bool) -> InlineKeyboardMarkup:
    rows: list[list[InlineKeyboardButton]] = []
    rows.append([InlineKeyboardButton(text="➕ Добавить аккаунты", callback_data="pa:add")])
    if has_accounts:
        rows.append([
            InlineKeyboardButton(text="📋 Список", callback_data="pa:list:0"),
            InlineKeyboardButton(text="📊 Лимиты пула", callback_data="pa:limits"),
        ])
        rows.append([
            InlineKeyboardButton(text="🗑 Удалить все", callback_data="pa:clear"),
        ])
    rows.append([InlineKeyboardButton(text="🏠 Главное меню", callback_data="menu:main")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def _list_keyboard(items: list[dict[str, Any]], page: int, total: int) -> InlineKeyboardMarkup:
    rows: list[list[InlineKeyboardButton]] = []
    for acc in items:
        label = (acc.get("label") or "").strip() or f"#{acc.get('id')}"
        if len(label) > 30:
            label = label[:27] + "…"
        status = (acc.get("status") or "?").lower()
        emoji = {
            "active": "🟢",
            "error": "🟡",
            "dead": "💀",
            "banned": "🚫",
            "reauth_needed": "🔑",
        }.get(status, "❔")
        rows.append([
            InlineKeyboardButton(
                text=f"{emoji} {label}",
                callback_data=f"pa:item:{int(acc.get('id'))}",
            )
        ])

    nav: list[InlineKeyboardButton] = []
    last_page = max(0, (total - 1) // PAGE_SIZE) if total > 0 else 0
    if page > 0:
        nav.append(InlineKeyboardButton(text="◀️", callback_data=f"pa:list:{page - 1}"))
    nav.append(
        InlineKeyboardButton(
            text=f"{page + 1}/{last_page + 1}",
            callback_data="pa:noop",
        )
    )
    if page < last_page:
        nav.append(InlineKeyboardButton(text="▶️", callback_data=f"pa:list:{page + 1}"))
    if nav:
        rows.append(nav)

    rows.append([InlineKeyboardButton(text="◀️ Назад", callback_data="pa:menu")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def _item_keyboard(account_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(
                text="📊 Лимит main",
                callback_data=f"pa:item_limits:{account_id}:main",
            ),
            InlineKeyboardButton(
                text="📊 Лимит replies",
                callback_data=f"pa:item_limits:{account_id}:reply",
            ),
        ],
        [
            InlineKeyboardButton(
                text="🗑 Удалить",
                callback_data=f"pa:item_delete:{account_id}",
            ),
        ],
        [InlineKeyboardButton(text="◀️ К списку", callback_data="pa:list:0")],
    ])


def _format_summary_text(summary: dict[str, Any]) -> str:
    s = get_settings()
    total = int(summary.get("total", 0) or 0)
    active = int(summary.get("active", 0) or 0)
    err = int(summary.get("error", 0) or 0)
    dead = int(summary.get("dead_or_banned", 0) or 0)
    main_today = int(summary.get("today_main_comments_total", 0) or 0)
    reply_today = int(summary.get("today_replies_total", 0) or 0)
    default_main = int(summary.get("default_daily_main_limit", s.PERSONAL_POOL_DEFAULT_DAILY_MAIN_LIMIT))
    default_reply = int(summary.get("default_daily_reply_limit", s.PERSONAL_POOL_DEFAULT_DAILY_REPLY_LIMIT))
    cap = int(summary.get("max_accounts_per_user", s.PERSONAL_POOL_MAX_ACCOUNTS_PER_USER))

    lines = [
        "*👥 Мои аккаунты*",
        "",
        f"Всего: *{total}* / {cap}",
        f"🟢 Активные: *{active}*   🟡 Ошибки: *{err}*   💀 Мёртвые/бан: *{dead}*",
        "",
        f"📈 Сегодня: главных *{main_today}*, ответов *{reply_today}*",
        f"📊 Лимит при импорте: главных *{default_main}*/день, ответов *{default_reply}*/день",
    ]
    if total == 0:
        lines += [
            "",
            "_Личных аккаунтов пока нет. Добавьте свои аккаунты — и в карточке заказа можно будет выбрать «использовать мои аккаунты»._",
        ]
    return "\n".join(lines)


def _format_account_card(account: dict[str, Any]) -> str:
    s = get_settings()
    label = (account.get("label") or "").strip() or f"#{account.get('id')}"
    status = (account.get("status") or "?").lower()
    main_limit = account.get("daily_main_limit")
    reply_limit = account.get("daily_reply_limit")
    main_today = int(account.get("today_main_comments", 0) or 0)
    reply_today = int(account.get("today_replies", 0) or 0)
    proxy = "✅" if account.get("has_proxy") else "—"
    country = account.get("country_code") or "—"
    last_used = account.get("last_used_at") or "—"
    last_main = account.get("last_main_comment_at") or "—"
    last_error = account.get("last_error")
    err_count = int(account.get("error_count_recent", 0) or 0)

    main_cap = int(main_limit) if main_limit is not None else int(s.MAX_ACTIONS_PER_ACCOUNT)
    reply_cap = int(reply_limit) if reply_limit is not None else int(s.MAX_ACTIONS_PER_ACCOUNT)

    lines = [
        f"*{_md_handle(label)}*",
        f"Статус: `{status}`",
        f"Прокси: {proxy}    Гео: `{country}`",
        "",
        f"📈 Main: *{main_today}/{main_cap}*"
        + ("" if main_limit is not None else " _(глобальный лимит)_"),
        f"📈 Replies: *{reply_today}/{reply_cap}*"
        + ("" if reply_limit is not None else " _(глобальный лимит)_"),
        "",
        f"Последнее использование: `{last_used}`",
        f"Последний main-коммент: `{last_main}`",
    ]
    if last_error:
        cleaned = str(last_error)[:160].replace("`", "'")
        lines += ["", f"⚠️ Последняя ошибка ({err_count}): `{cleaned}`"]
    return "\n".join(lines)


# ----------------------- handlers -----------------------

@router.callback_query(F.data == "pa:noop")
async def noop(callback: CallbackQuery) -> None:
    try:
        await callback.answer()
    except Exception:
        pass


@router.callback_query(F.data == "pa:menu")
async def show_personal_accounts_menu(callback: CallbackQuery, state: FSMContext) -> None:
    """Главный экран раздела «Мои аккаунты»."""
    try:
        if not callback.message:
            await callback.answer("Ошибка: сообщение не найдено", show_alert=True)
            return
        try:
            await callback.answer()
        except Exception:
            pass
        await state.clear()
        user_data = await _resolve_user(callback)
        if not user_data or not user_data.get("id"):
            await callback.message.edit_text(
                "Не удалось определить пользователя. Попробуйте открыть раздел заново.",
                reply_markup=get_back_keyboard("menu:main"),
            )
            return
        user_backend_id = int(user_data["id"])

        try:
            summary = await api_client.get_personal_pool_summary(user_backend_id)
        except Exception as exc:
            logger.error(f"pa:menu summary failed: {exc}", exc_info=True)
            await callback.message.edit_text(
                "Ошибка получения данных по аккаунтам. Попробуйте обновить раздел позже.",
                reply_markup=get_back_keyboard("menu:main"),
            )
            return

        text = _format_summary_text(summary)
        keyboard = _menu_keyboard(int(summary.get("total", 0) or 0) > 0)
        await callback.message.edit_text(text, reply_markup=keyboard, parse_mode="Markdown")
    except Exception as exc:
        logger.error(f"Error in pa:menu: {exc}", exc_info=True)
        try:
            if callback.message:
                await callback.message.edit_text(
                    "Ошибка открытия раздела аккаунтов. Попробуйте ещё раз.",
                    reply_markup=get_back_keyboard("menu:main"),
                )
            else:
                await callback.answer("Ошибка", show_alert=True)
        except Exception:
            pass


@router.callback_query(F.data.startswith("pa:list:"))
async def show_personal_accounts_list(callback: CallbackQuery) -> None:
    try:
        if not callback.message:
            await callback.answer("Ошибка: сообщение не найдено", show_alert=True)
            return
        try:
            page = int((callback.data or "pa:list:0").split(":")[2])
        except (ValueError, IndexError):
            page = 0
        if page < 0:
            page = 0

        user_data = await _resolve_user(callback)
        if not user_data or not user_data.get("id"):
            await callback.answer("Не удалось определить пользователя", show_alert=True)
            return
        user_backend_id = int(user_data["id"])

        try:
            data = await api_client.list_personal_accounts(
                user_backend_id, page=page, page_size=PAGE_SIZE
            )
        except Exception as exc:
            logger.error(f"pa:list api failed: {exc}", exc_info=True)
            await callback.answer("Ошибка загрузки списка", show_alert=True)
            return

        accounts = list(data.get("accounts") or [])
        total = int(data.get("total") or 0)

        if total == 0:
            await callback.message.edit_text(
                "*👥 Мои аккаунты — список*\n\n_Пока пусто. Добавьте аккаунты через «➕ Добавить»._",
                reply_markup=get_back_keyboard("pa:menu"),
                parse_mode="Markdown",
            )
            await callback.answer()
            return

        lines = [
            "*👥 Мои аккаунты — список*",
            "",
            f"Всего: *{total}*. Стр. *{page + 1}*.",
        ]
        await callback.message.edit_text(
            "\n".join(lines),
            reply_markup=_list_keyboard(accounts, page, total),
            parse_mode="Markdown",
        )
        await callback.answer()
    except Exception as exc:
        logger.error(f"Error in pa:list: {exc}", exc_info=True)
        try:
            await callback.answer("Ошибка", show_alert=True)
        except Exception:
            pass


@router.callback_query(F.data.startswith("pa:item:"))
async def show_personal_account_card(callback: CallbackQuery) -> None:
    try:
        if not callback.message:
            await callback.answer("Ошибка: сообщение не найдено", show_alert=True)
            return
        parts = (callback.data or "").split(":")
        if len(parts) != 3 or not parts[2].isdigit():
            await callback.answer()
            return
        account_id = int(parts[2])

        user_data = await _resolve_user(callback)
        if not user_data or not user_data.get("id"):
            await callback.answer("Не удалось определить пользователя", show_alert=True)
            return
        user_backend_id = int(user_data["id"])

        # API не отдаёт one-by-one — забираем сразу страницу 0..N через несколько вызовов.
        # Но для UX дешевле просто пройтись по нашему списку с большим page_size:
        # для большого пула (>200 невозможно — есть жёсткий cap), такая стоимость терпима.
        try:
            data = await api_client.list_personal_accounts(
                user_backend_id, page=0, page_size=200
            )
        except Exception as exc:
            logger.error(f"pa:item list failed: {exc}", exc_info=True)
            await callback.answer("Ошибка загрузки", show_alert=True)
            return

        target = next(
            (a for a in (data.get("accounts") or []) if int(a.get("id") or 0) == account_id),
            None,
        )
        if not target:
            await callback.answer("Аккаунт не найден", show_alert=True)
            return

        await callback.message.edit_text(
            _format_account_card(target),
            reply_markup=_item_keyboard(account_id),
            parse_mode="Markdown",
        )
        await callback.answer()
    except Exception as exc:
        logger.error(f"Error in pa:item: {exc}", exc_info=True)
        try:
            await callback.answer("Ошибка", show_alert=True)
        except Exception:
            pass


@router.callback_query(F.data.startswith("pa:item_delete:"))
async def delete_personal_account_handler(callback: CallbackQuery) -> None:
    try:
        if not callback.message:
            await callback.answer("Ошибка: сообщение не найдено", show_alert=True)
            return
        parts = (callback.data or "").split(":")
        if len(parts) != 3 or not parts[2].isdigit():
            await callback.answer()
            return
        account_id = int(parts[2])

        user_data = await _resolve_user(callback)
        if not user_data or not user_data.get("id"):
            await callback.answer("Не удалось определить пользователя", show_alert=True)
            return
        user_backend_id = int(user_data["id"])

        try:
            await api_client.delete_personal_account(user_backend_id, account_id)
        except Exception as exc:
            logger.error(f"pa:item_delete failed: {exc}", exc_info=True)
            await callback.answer("Не удалось удалить", show_alert=True)
            return

        await callback.answer("Удалено", show_alert=False)
        # Перерисовываем главный экран с обновлённой сводкой.
        try:
            summary = await api_client.get_personal_pool_summary(user_backend_id)
            await callback.message.edit_text(
                _format_summary_text(summary),
                reply_markup=_menu_keyboard(int(summary.get("total", 0) or 0) > 0),
                parse_mode="Markdown",
            )
        except Exception:
            pass
    except Exception as exc:
        logger.error(f"Error in pa:item_delete: {exc}", exc_info=True)
        try:
            await callback.answer("Ошибка", show_alert=True)
        except Exception:
            pass


@router.callback_query(F.data == "pa:clear")
async def clear_pool_confirm(callback: CallbackQuery) -> None:
    try:
        if not callback.message:
            return
        await callback.message.edit_text(
            "*🗑 Удалить ВСЕ личные аккаунты?*\n\n"
            "Действие необратимое. Аккаунты пропадут из вашего пула, но в общий пул не попадут.",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="✅ Да, удалить", callback_data="pa:clear:do")],
                [InlineKeyboardButton(text="◀️ Назад", callback_data="pa:menu")],
            ]),
            parse_mode="Markdown",
        )
        await callback.answer()
    except Exception as exc:
        logger.error(f"Error in pa:clear: {exc}", exc_info=True)
        try:
            await callback.answer("Ошибка", show_alert=True)
        except Exception:
            pass


@router.callback_query(F.data == "pa:clear:do")
async def clear_pool_do(callback: CallbackQuery) -> None:
    try:
        if not callback.message:
            return
        user_data = await _resolve_user(callback)
        if not user_data or not user_data.get("id"):
            await callback.answer("Не удалось определить пользователя", show_alert=True)
            return
        user_backend_id = int(user_data["id"])

        try:
            res = await api_client.clear_personal_pool(user_backend_id)
        except Exception as exc:
            logger.error(f"pa:clear:do failed: {exc}", exc_info=True)
            await callback.answer("Ошибка", show_alert=True)
            return

        deleted = int((res or {}).get("deleted", 0) or 0)
        await callback.answer(f"Удалено: {deleted}", show_alert=True)

        # Перерисовываем меню.
        try:
            summary = await api_client.get_personal_pool_summary(user_backend_id)
            await callback.message.edit_text(
                _format_summary_text(summary),
                reply_markup=_menu_keyboard(int(summary.get("total", 0) or 0) > 0),
                parse_mode="Markdown",
            )
        except Exception:
            pass
    except Exception as exc:
        logger.error(f"Error in pa:clear:do: {exc}", exc_info=True)
        try:
            await callback.answer("Ошибка", show_alert=True)
        except Exception:
            pass


# ----------------------- import flow -----------------------

@router.callback_query(F.data == "pa:add")
async def start_personal_import(callback: CallbackQuery, state: FSMContext) -> None:
    try:
        if not callback.message:
            return
        s = get_settings()
        await state.set_state(PersonalAccountsImport.WAITING_INPUT)
        await callback.message.edit_text(
            "*➕ Добавить личные аккаунты*\n\n"
            "Пришлите *файл .txt* с аккаунтами или *вставьте текст в чат*.\n\n"
            "Поддерживаются те же форматы, что и в админском импорте:\n"
            "• `cookies-json | proxy | user-agent`\n"
            "• `cookies-inline (sid_tt=...; sessionid=...)`\n"
            "• одна строка = один аккаунт\n\n"
            f"📊 Дефолт-лимит при импорте: *{s.PERSONAL_POOL_DEFAULT_DAILY_MAIN_LIMIT}* main и "
            f"*{s.PERSONAL_POOL_DEFAULT_DAILY_REPLY_LIMIT}* replies в сутки на каждый аккаунт.\n"
            f"Лимиты можно изменить позже в разделе «📊 Лимиты пула».",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="❌ Отмена", callback_data="pa:menu")],
            ]),
            parse_mode="Markdown",
        )
        await callback.answer()
    except Exception as exc:
        logger.error(f"Error in pa:add: {exc}", exc_info=True)
        try:
            await callback.answer("Ошибка", show_alert=True)
        except Exception:
            pass


async def _do_personal_import(message: Message, state: FSMContext, content: str) -> None:
    """Общий путь: парсим текст, отправляем в API, сообщаем результат."""
    try:
        parsed = parse_accounts_file_smart(content)
    except Exception as exc:
        logger.error(f"pa import parse failed: {exc}", exc_info=True)
        await message.answer("❌ Не удалось разобрать файл. Проверьте формат.")
        await state.clear()
        return

    if not parsed.accounts:
        await message.answer(
            "❌ В файле не найдено валидных аккаунтов.\n"
            f"Ошибок разбора: {len(parsed.errors or [])}, дублей: {parsed.duplicates}."
        )
        await state.clear()
        return

    user_data = await _resolve_user_from_message(message)
    if not user_data or not user_data.get("id"):
        await message.answer("❌ Не удалось определить пользователя. Попробуйте /start.")
        await state.clear()
        return
    user_backend_id = int(user_data["id"])

    payload_accounts: list[dict[str, Any]] = []
    for acc in parsed.accounts:
        d = asdict(acc)
        # API ждёт {label, cookies, cookie_file, user_agent, proxy} —
        # ParsedAccountSmart содержит ровно их + метаданные validation_*; чистим.
        payload_accounts.append({
            "label": d.get("label"),
            "cookies": d.get("cookies"),
            "cookie_file": d.get("cookie_file"),
            "user_agent": d.get("user_agent"),
            "proxy": d.get("proxy"),
        })

    try:
        result = await api_client.import_personal_accounts(
            user_backend_id, payload_accounts
        )
    except Exception as exc:
        logger.error(f"pa import api failed: {exc}", exc_info=True)
        await message.answer("❌ Ошибка отправки на сервер. Попробуйте позже.")
        await state.clear()
        return

    added = int((result or {}).get("added", 0) or 0)
    updated = int((result or {}).get("updated", 0) or 0)
    skipped = int((result or {}).get("skipped", 0) or 0)
    total_after = int((result or {}).get("total_owned_after", 0) or 0)
    reasons = list((result or {}).get("skipped_reasons") or [])

    text_lines = [
        "*✅ Импорт завершён*",
        "",
        f"➕ Добавлено: *{added}*",
        f"🔄 Обновлено: *{updated}*",
        f"⛔ Пропущено: *{skipped}*",
        f"📦 Всего в личном пуле: *{total_after}*",
    ]
    if reasons:
        text_lines += ["", "_Причины пропусков (первые 8):_"]
        for r in reasons[:8]:
            text_lines.append(f"• `{r[:80]}`")

    await message.answer(
        "\n".join(text_lines),
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="👥 Мои аккаунты", callback_data="pa:menu")],
        ]),
        parse_mode="Markdown",
    )
    await state.clear()


@router.message(StateFilter(PersonalAccountsImport.WAITING_INPUT), F.document)
async def receive_personal_import_file(message: Message, state: FSMContext) -> None:
    try:
        if not message.document:
            return
        file = await message.bot.get_file(message.document.file_id)
        file_bytes = await message.bot.download_file(file.file_path)
        content = file_bytes.read().decode("utf-8", errors="ignore")
    except Exception as exc:
        logger.error(f"pa import download failed: {exc}", exc_info=True)
        await message.answer("❌ Не удалось скачать файл.")
        await state.clear()
        return
    await _do_personal_import(message, state, content)


@router.message(StateFilter(PersonalAccountsImport.WAITING_INPUT), F.text)
async def receive_personal_import_text(message: Message, state: FSMContext) -> None:
    text = (message.text or "").strip()
    if not text:
        await message.answer("Пришлите файл или текст с аккаунтами.")
        return
    await _do_personal_import(message, state, text)


# ----------------------- limits flow -----------------------

@router.callback_query(F.data == "pa:limits")
async def show_pool_limits(callback: CallbackQuery) -> None:
    try:
        if not callback.message:
            return
        s = get_settings()
        await callback.message.edit_text(
            "*📊 Лимиты пула*\n\n"
            f"Настраивают, сколько каждый аккаунт может сделать в сутки.\n\n"
            f"Допустимые границы:\n"
            f"• main: *0 .. {s.PERSONAL_POOL_MAX_DAILY_MAIN_LIMIT}* в сутки\n"
            f"• replies: *0 .. {s.PERSONAL_POOL_MAX_DAILY_REPLY_LIMIT}* в сутки\n\n"
            f"_При изменении применяется ко всем уже импортированным аккаунтам сразу._",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="✏️ Лимит main", callback_data="pa:limits:set:main")],
                [InlineKeyboardButton(text="✏️ Лимит replies", callback_data="pa:limits:set:reply")],
                [InlineKeyboardButton(text="◀️ Назад", callback_data="pa:menu")],
            ]),
            parse_mode="Markdown",
        )
        await callback.answer()
    except Exception as exc:
        logger.error(f"Error in pa:limits: {exc}", exc_info=True)
        try:
            await callback.answer("Ошибка", show_alert=True)
        except Exception:
            pass


@router.callback_query(F.data == "pa:limits:set:main")
async def ask_bulk_main(callback: CallbackQuery, state: FSMContext) -> None:
    if not callback.message:
        return
    s = get_settings()
    await state.set_state(PersonalLimitsState.BULK_MAIN)
    await callback.message.edit_text(
        f"Введите новый лимит *main-комментов* в сутки (0..{s.PERSONAL_POOL_MAX_DAILY_MAIN_LIMIT}).",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="❌ Отмена", callback_data="pa:limits")],
        ]),
        parse_mode="Markdown",
    )
    await callback.answer()


@router.callback_query(F.data == "pa:limits:set:reply")
async def ask_bulk_reply(callback: CallbackQuery, state: FSMContext) -> None:
    if not callback.message:
        return
    s = get_settings()
    await state.set_state(PersonalLimitsState.BULK_REPLIES)
    await callback.message.edit_text(
        f"Введите новый лимит *replies* в сутки (0..{s.PERSONAL_POOL_MAX_DAILY_REPLY_LIMIT}).",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="❌ Отмена", callback_data="pa:limits")],
        ]),
        parse_mode="Markdown",
    )
    await callback.answer()


async def _bulk_apply_limit(
    message: Message,
    state: FSMContext,
    raw_value: str,
    *,
    is_main: bool,
) -> None:
    s = get_settings()
    try:
        value = int(raw_value.strip())
    except (TypeError, ValueError):
        await message.answer("Введите целое число.")
        return
    cap = s.PERSONAL_POOL_MAX_DAILY_MAIN_LIMIT if is_main else s.PERSONAL_POOL_MAX_DAILY_REPLY_LIMIT
    if value < 0 or value > cap:
        await message.answer(f"Допустимый диапазон: 0..{cap}.")
        return

    user_data = await _resolve_user_from_message(message)
    if not user_data or not user_data.get("id"):
        await message.answer("❌ Не удалось определить пользователя.")
        await state.clear()
        return
    user_backend_id = int(user_data["id"])

    try:
        await api_client.patch_personal_pool_limits_bulk(
            user_backend_id,
            daily_main_limit=value if is_main else None,
            daily_reply_limit=None if is_main else value,
            apply_to_existing=True,
        )
    except Exception as exc:
        logger.error(f"pa bulk limits failed: {exc}", exc_info=True)
        await message.answer("❌ Не удалось обновить лимиты. Попробуйте позже.")
        await state.clear()
        return

    await state.clear()
    await message.answer(
        f"✅ Лимит обновлён: *{('main' if is_main else 'replies')}* = *{value}*/день для всего пула.",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="👥 Мои аккаунты", callback_data="pa:menu")],
        ]),
        parse_mode="Markdown",
    )


@router.message(StateFilter(PersonalLimitsState.BULK_MAIN), F.text)
async def process_bulk_main(message: Message, state: FSMContext) -> None:
    await _bulk_apply_limit(message, state, message.text or "", is_main=True)


@router.message(StateFilter(PersonalLimitsState.BULK_REPLIES), F.text)
async def process_bulk_reply(message: Message, state: FSMContext) -> None:
    await _bulk_apply_limit(message, state, message.text or "", is_main=False)


# ----------------------- per-account limit flow -----------------------

@router.callback_query(F.data.startswith("pa:item_limits:"))
async def ask_item_limit(callback: CallbackQuery, state: FSMContext) -> None:
    try:
        if not callback.message:
            return
        parts = (callback.data or "").split(":")
        # pa:item_limits:{id}:{main|reply}
        if len(parts) != 4 or not parts[2].isdigit():
            await callback.answer()
            return
        account_id = int(parts[2])
        kind = parts[3]
        s = get_settings()

        if kind == "main":
            await state.set_state(PersonalLimitsState.ITEM_MAIN)
            cap = s.PERSONAL_POOL_MAX_DAILY_MAIN_LIMIT
            label = "main-комменты"
        elif kind == "reply":
            await state.set_state(PersonalLimitsState.ITEM_REPLIES)
            cap = s.PERSONAL_POOL_MAX_DAILY_REPLY_LIMIT
            label = "replies"
        else:
            await callback.answer()
            return
        await state.update_data(item_account_id=account_id)
        await callback.message.edit_text(
            f"Введите *новый лимит {label}* в сутки для этого аккаунта (0..{cap}).\n"
            "Введите `0`, чтобы отключить аккаунт от этого типа активности.",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="❌ Отмена", callback_data=f"pa:item:{account_id}")],
            ]),
            parse_mode="Markdown",
        )
        await callback.answer()
    except Exception as exc:
        logger.error(f"Error in pa:item_limits: {exc}", exc_info=True)
        try:
            await callback.answer("Ошибка", show_alert=True)
        except Exception:
            pass


async def _apply_item_limit(message: Message, state: FSMContext, *, is_main: bool) -> None:
    s = get_settings()
    data = await state.get_data()
    account_id = int(data.get("item_account_id") or 0)
    if not account_id:
        await message.answer("Сессия потеряна. Откройте карточку аккаунта снова.")
        await state.clear()
        return
    try:
        value = int((message.text or "").strip())
    except (TypeError, ValueError):
        await message.answer("Введите целое число.")
        return
    cap = s.PERSONAL_POOL_MAX_DAILY_MAIN_LIMIT if is_main else s.PERSONAL_POOL_MAX_DAILY_REPLY_LIMIT
    if value < 0 or value > cap:
        await message.answer(f"Допустимый диапазон: 0..{cap}.")
        return

    user_data = await _resolve_user_from_message(message)
    if not user_data or not user_data.get("id"):
        await message.answer("❌ Не удалось определить пользователя.")
        await state.clear()
        return
    user_backend_id = int(user_data["id"])

    try:
        await api_client.patch_personal_account_limits(
            user_backend_id,
            account_id,
            daily_main_limit=value if is_main else None,
            daily_reply_limit=None if is_main else value,
        )
    except Exception as exc:
        logger.error(f"pa item limit failed: {exc}", exc_info=True)
        await message.answer("❌ Не удалось обновить лимит. Попробуйте позже.")
        await state.clear()
        return

    await state.clear()
    await message.answer(
        f"✅ Лимит обновлён: *{('main' if is_main else 'replies')}* = *{value}*/день",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="◀️ К аккаунту", callback_data=f"pa:item:{account_id}")],
            [InlineKeyboardButton(text="👥 Мои аккаунты", callback_data="pa:menu")],
        ]),
        parse_mode="Markdown",
    )


@router.message(StateFilter(PersonalLimitsState.ITEM_MAIN), F.text)
async def process_item_main(message: Message, state: FSMContext) -> None:
    await _apply_item_limit(message, state, is_main=True)


@router.message(StateFilter(PersonalLimitsState.ITEM_REPLIES), F.text)
async def process_item_reply(message: Message, state: FSMContext) -> None:
    await _apply_item_limit(message, state, is_main=False)
