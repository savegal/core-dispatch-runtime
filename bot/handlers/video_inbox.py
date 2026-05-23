"""
Handlers для full-mode Inbox: рабочий центр новых видео.
"""
from __future__ import annotations

import logging
from collections import defaultdict
from typing import Iterable

from aiogram import F, Router
from aiogram.exceptions import TelegramBadRequest
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup

from bot.services.api_client import api_client
from bot.services.bot_ui import block, bullets, kv_rows, screen
from bot.services.formatting import escape_markdown, markdown_handle, markdown_to_plain_text
from bot.services.user_utils import get_user_backend_id
from bot.services.video_display import (
    format_video_markdown_line,
    normalize_video_url,
    video_short_id,
    video_time_label,
)

logger = logging.getLogger("bot.video_inbox")
router = Router()

INBOX_PAGE_SIZE = 5
INBOX_SELECT_PAGE_SIZE = 8
INBOX_GROUP_PAGE_SIZE = 8
INBOX_BULK_LIMIT = 50


def _bulk_action_text(action: str, count: int) -> str:
    capped = min(max(0, count), INBOX_BULK_LIMIT)
    suffix = "все новые" if count <= INBOX_BULK_LIMIT else f"первые {capped}"
    if action == "skip":
        return "🗑 Пропустить все новые"
    return f"🚀 Заказать {suffix}"


async def _edit_markdown(message, text: str, **kwargs) -> None:
    kwargs.setdefault("disable_web_page_preview", True)
    try:
        await message.edit_text(text, parse_mode="Markdown", **kwargs)
    except TelegramBadRequest as e:
        if "message is not modified" in str(e):
            return
        plain_kwargs = dict(kwargs)
        plain_kwargs.pop("parse_mode", None)
        await message.edit_text(markdown_to_plain_text(text), **plain_kwargs)


async def _answer_error(callback: CallbackQuery, text: str = "❌ Ошибка") -> None:
    try:
        await callback.answer(text, show_alert=True)
    except TelegramBadRequest:
        pass


async def _get_user_id(callback: CallbackQuery) -> int | None:
    user_id = await get_user_backend_id(callback.from_user.id)
    if not user_id:
        await _answer_error(callback, "❌ Пользователь не найден")
        return None
    return user_id


def _video_status_icon(display_status: str | None) -> str:
    return {
        "new": "🆕",
        "queued": "⏳",
        "done": "✅",
        "done_warning": "⚠️",
        "failed": "🚨",
        "skipped_manual": "⏭️",
    }.get((display_status or "").strip(), "•")


def _status_name(status: str | None) -> str:
    return {
        "new": "Новые",
        "queued": "В очереди",
        "done": "Выполненные",
        "failed": "Ошибки",
        "skipped": "Пропущенные",
    }.get((status or "").strip(), status or "Видео")


def _pages_total(total: int, page_size: int) -> int:
    return max(1, (max(0, total) + page_size - 1) // page_size)


def _nav_row(prefix: str, page: int, has_next: bool) -> list[InlineKeyboardButton]:
    row: list[InlineKeyboardButton] = []
    if page > 0:
        row.append(InlineKeyboardButton(text="◀️", callback_data=f"{prefix}:{page - 1}"))
    row.append(InlineKeyboardButton(text=f"{page + 1}", callback_data="vp:inbox:noop"))
    if has_next:
        row.append(InlineKeyboardButton(text="▶️", callback_data=f"{prefix}:{page + 1}"))
    return row


def _context_back_callback(context_parts: list[str]) -> str:
    try:
        if len(context_parts) >= 2 and context_parts[0] == "new":
            return f"vp:inbox:new:{max(0, int(context_parts[1]))}"
        if len(context_parts) >= 3 and context_parts[0] == "creator":
            creator_id = int(context_parts[1])
            page = max(0, int(context_parts[2]))
            return f"vp:inbox:creator:{creator_id}:{page}"
        if len(context_parts) >= 3 and context_parts[0] == "status":
            status = str(context_parts[1] or "").strip()
            page = max(0, int(context_parts[2]))
            if status == "failed":
                return f"vp:inbox:errors:{page}"
            if status:
                return f"vp:inbox:filter:{status}:{page}"
    except (TypeError, ValueError):
        return "vp:inbox"
    return "vp:inbox"


def _parse_video_action(callback_data: str | None) -> tuple[int, list[str]]:
    parts = (callback_data or "").split(":")
    video_id = int(parts[3])
    return video_id, parts[4:]


def _video_action_row(video: dict, index: int, *, context: str = "") -> list[InlineKeyboardButton]:
    row: list[InlineKeyboardButton] = []
    url = normalize_video_url(video.get("url"))
    suffix = f":{context}" if context else ""
    if url:
        row.append(InlineKeyboardButton(text=f"🎬 #{index}", url=url))
    row.extend(
        [
            InlineKeyboardButton(text="🚀 Заказ", callback_data=f"vp:inbox:order:{video.get('id')}{suffix}"),
            InlineKeyboardButton(text="✅ Уже", callback_data=f"vp:inbox:done:{video.get('id')}{suffix}"),
            InlineKeyboardButton(text="🗑 Скип", callback_data=f"vp:inbox:skip:{video.get('id')}{suffix}"),
        ]
    )
    return row


def _task_button(video: dict) -> InlineKeyboardButton | None:
    task_id = video.get("task_id")
    if not task_id:
        return None
    return InlineKeyboardButton(text=f"📋 Заказ #{task_id}", callback_data=f"order:detail:{task_id}")


def _group_videos_by_creator(videos: Iterable[dict]) -> dict[int, list[dict]]:
    grouped: dict[int, list[dict]] = defaultdict(list)
    for video in videos:
        creator_id = video.get("creator_id")
        video_id = video.get("id")
        if creator_id and video_id:
            grouped[int(creator_id)].append(video)
    return dict(grouped)


def _available_new_videos(videos: Iterable[dict]) -> list[dict]:
    return [
        video
        for video in videos
        if str(video.get("status") or "").strip().lower() == "new"
        and not video.get("task_id")
        and video.get("id")
    ]


def _build_inbox_overview_text(stats: dict, videos: list[dict]) -> str:
    new_count = int(stats.get("new", 0) or 0)
    queued_count = int(stats.get("queued", 0) or 0)
    done_count = int(stats.get("done", 0) or 0)
    skipped_count = int(stats.get("skipped", 0) or 0)
    failed_count = int(stats.get("failed", 0) or 0)

    # Однострочный summary всех статусов — короткая визитка, читается за секунду.
    counters_line = (
        f"🆕 Новые *{new_count}* · ⏳ Очередь *{queued_count}* · "
        f"⚠️ Ошибки *{failed_count}* · ✅ Готово *{done_count}* · 🗑 Скип *{skipped_count}*"
    )

    attention: list[str] = []
    if failed_count:
        attention.append(f"🚨 Ошибки: *{failed_count}* — стоит открыть отдельно.")
    if new_count:
        attention.append(f"👀 Ждут решения: *{new_count}* новых видео.")
    if queued_count:
        attention.append(f"⏳ Уже в работе: *{queued_count}* видео.")
    if not attention:
        attention.append("_Сейчас ничего срочного нет._")

    preview_lines = [
        format_video_markdown_line(
            video,
            idx,
            include_username=True,
            status_icon=_video_status_icon(video.get("display_status")),
        )
        for idx, video in enumerate(videos[:5], start=1)
    ]

    return screen(
        "📥 Входящие",
        counters_line,
        block("Требует внимания", attention),
        block("Последние новые", preview_lines or ["_Новых видео сейчас нет._"]),
    )


def _build_new_videos_text(stats: dict, videos: list[dict], page: int, has_next: bool) -> str:
    total_new = int(stats.get("new", 0) or 0)
    pages_total = _pages_total(total_new, INBOX_PAGE_SIZE)
    start_index = page * INBOX_PAGE_SIZE + 1
    lines = [
        f"Всего новых: *{total_new}* · Страница *{page + 1}/{pages_total}*",
    ]
    if not videos:
        lines.append("")
        lines.append("_На этой странице новых видео нет._")
    else:
        lines.append("")
        for offset, video in enumerate(videos, start=0):
            lines.append(format_video_markdown_line(video, start_index + offset, include_username=True))
    return screen("🆕 Новые видео", "\n".join(lines))


def _build_status_page_text(status: str, videos: list[dict], page: int, total: int) -> str:
    lines = [
        f"Найдено: *{total}*",
        f"Страница *{page + 1}/{_pages_total(total, INBOX_PAGE_SIZE)}*",
    ]
    if not videos:
        lines.append("")
        lines.append("_Видео не найдены._")
    else:
        lines.append("")
        for offset, video in enumerate(videos, start=1 + page * INBOX_PAGE_SIZE):
            line = format_video_markdown_line(
                video,
                offset,
                include_username=True,
                status_icon=_video_status_icon(video.get("display_status")),
            )
            # Раньше показывали и status_label, и human_reason — это даёт две
            # одинаковые по смыслу строки. Оставляем только наиболее информативную:
            #   - human_reason если есть (для failed → текст ошибки, для done с
            #     warnings → текст warning);
            #   - status_label как fallback (для queued/skipped, где причины нет).
            human_reason = video.get("human_reason")
            status_label = video.get("status_label")
            reason_text = human_reason or status_label
            if reason_text:
                reason_preview = reason_text[:120] + ("…" if len(reason_text) > 120 else "")
                line += f"\n   _{escape_markdown(reason_preview)}_"
            lines.append(line)
    return screen(f"{_status_name(status)}", "\n".join(lines))


def _build_filters_text(stats: dict) -> str:
    return screen(
        "📋 Все входящие",
        block(
            "Статусы",
            kv_rows(
                [
                    ("🆕 Новые", f"*{stats.get('new', 0)}*"),
                    ("⏳ В очереди", f"*{stats.get('queued', 0)}*"),
                    ("✅ Выполнено", f"*{stats.get('done', 0)}*"),
                    ("🚨 Ошибки", f"*{stats.get('failed', 0)}*"),
                    ("⏭ Пропущено", f"*{stats.get('skipped', 0)}*"),
                ]
            ),
        ),
        block(
            "Как пользоваться",
            bullets(
                [
                    "Новые — можно заказать, отметить готовыми или пропустить.",
                    "Ошибки — быстрый вход к проблемным видео и связанным заказам.",
                    "История помогает понять, что уже ушло в работу.",
                ]
            ),
        ),
    )


def _build_selection_text(videos: list[dict], selected_ids: set[int], page: int, total_new: int) -> str:
    lines = [
        f"Выбрано: *{len(selected_ids)}* · Всего новых: *{total_new}*",
        f"Страница *{page + 1}/{_pages_total(total_new, INBOX_SELECT_PAGE_SIZE)}*",
        "",
    ]
    if not videos:
        lines.append("_На этой странице новых видео нет._")
    for offset, video in enumerate(videos, start=1 + page * INBOX_SELECT_PAGE_SIZE):
        marker = "☑️" if int(video.get("id") or 0) in selected_ids else "☐"
        lines.append(
            f"{marker} "
            + format_video_markdown_line(video, offset, include_username=True)
        )
    return screen("🎯 Выбор видео", "\n".join(lines))


async def _open_template_flow(
    callback: CallbackQuery,
    state: FSMContext,
    *,
    user_id: int,
    creator_id: int,
    video_ids: list[int],
    back_callback: str = "vp:inbox",
) -> None:
    """Создаёт batch и открывает экран выбора шаблона для пачки видео.

    Раньше тут был ``FakeCallback`` — обманный объект, имитирующий
    CallbackQuery, чтобы переиспользовать `new_videos_select_template`
    handler. Это было хрупко: aiogram эволюционирует, и любое изменение
    типов (`CallbackQuery.bot`, `.chat_instance`, etc.) ломало хак тихо.
    Сейчас зовём чистую функцию `render_template_picker`, которая
    принимает `message`, `state`, `notify`-callback и ничего не знает про
    CallbackQuery.
    """
    if not video_ids:
        await _answer_error(callback, "Видео не найдены")
        return
    if not callback.message:
        return
    batch = await api_client.create_video_batch(creator_id, [str(video_id) for video_id in video_ids], user_id)
    batch_id = batch.get("batch_id")
    if not batch_id:
        await _answer_error(callback, "Не удалось создать пачку")
        return

    from bot.handlers.video_parsing_new_videos import render_template_picker

    await render_template_picker(
        callback.message,
        state,
        user_id=user_id,
        creator_id=int(creator_id),
        batch_id=int(batch_id),
        cancel_callback=back_callback,
        notify=callback.answer,
    )


async def _show_grouped_order_choice(
    callback: CallbackQuery,
    *,
    grouped: dict[int, list[dict]],
    back_callback: str,
    title: str = "Видео из разных блогеров",
) -> None:
    keyboard_buttons: list[list[InlineKeyboardButton]] = []
    summary_lines: list[str] = []
    for creator_id, creator_videos in grouped.items():
        video_ids = [int(video.get("id")) for video in creator_videos if video.get("id")]
        if not video_ids:
            continue
        username = creator_videos[0].get("creator_username") or f"id {creator_id}"
        summary_lines.append(f"{markdown_handle(username)}: *{len(video_ids)}*")
        keyboard_buttons.append([
            InlineKeyboardButton(
                text=f"@{username} · {len(video_ids)}",
                callback_data=f"vp:inbox:group_order:{creator_id}",
            )
        ])

    keyboard_buttons.append([InlineKeyboardButton(text="⬅️ Назад", callback_data=back_callback)])
    await _edit_markdown(
        callback.message,
        screen(
            title,
            block("Разделено по блогерам", summary_lines),
            "Выберите блогера — дальше откроется выбор шаблона для его видео.",
        ),
        reply_markup=InlineKeyboardMarkup(inline_keyboard=keyboard_buttons),
    )


async def _start_order_for_videos(
    callback: CallbackQuery,
    state: FSMContext,
    *,
    user_id: int,
    videos: list[dict],
    back_callback: str,
    grouped_title: str = "Видео из разных блогеров",
) -> None:
    available_videos = _available_new_videos(videos)
    if not available_videos:
        await _answer_error(callback, "Эти видео уже обработаны или стоят в очереди")
        return

    grouped = _group_videos_by_creator(available_videos)
    if not grouped:
        await _answer_error(callback, "Не найден блогер для выбранных видео")
        return
    if len(grouped) == 1:
        creator_id, creator_videos = next(iter(grouped.items()))
        await _open_template_flow(
            callback,
            state,
            user_id=user_id,
            creator_id=creator_id,
            video_ids=[int(video.get("id")) for video in creator_videos if video.get("id")],
            back_callback=back_callback,
        )
        return

    await state.update_data(
        inbox_grouped_order_video_ids={
            str(creator_id): [int(video.get("id")) for video in creator_videos if video.get("id")]
            for creator_id, creator_videos in grouped.items()
        },
        inbox_grouped_order_back=back_callback,
    )
    await _show_grouped_order_choice(
        callback,
        grouped=grouped,
        back_callback=back_callback,
        title=grouped_title,
    )


async def _show_new_videos_page(callback: CallbackQuery, state: FSMContext, page: int = 0) -> None:
    try:
        await callback.answer()
    except TelegramBadRequest:
        pass
    user_id = await _get_user_id(callback)
    if not user_id or not callback.message:
        return

    inbox_data = await api_client.get_inbox_videos(
        user_id,
        status="new",
        limit=INBOX_PAGE_SIZE + 1,
        offset=max(0, page) * INBOX_PAGE_SIZE,
    )
    loaded = inbox_data.get("videos", [])
    has_next = len(loaded) > INBOX_PAGE_SIZE
    videos = loaded[:INBOX_PAGE_SIZE]
    # ``by_status`` приходит вместе с видео — отдельный stats-запрос лишний.
    stats = inbox_data.get("by_status") or {}

    keyboard_buttons: list[list[InlineKeyboardButton]] = []
    for offset, video in enumerate(videos, start=1 + page * INBOX_PAGE_SIZE):
        keyboard_buttons.append(_video_action_row(video, offset, context=f"new:{max(0, page)}"))

    if videos:
        keyboard_buttons.append([
            InlineKeyboardButton(text="🚀 Заказать страницу", callback_data=f"vp:inbox:order_page:{page}"),
        ])
        keyboard_buttons.append([
            InlineKeyboardButton(text="🎯 Выбрать несколько", callback_data=f"vp:inbox:select:page:{page}"),
            InlineKeyboardButton(text="✅ Страница готова", callback_data=f"vp:inbox:done_page_confirm:{page}"),
        ])
        keyboard_buttons.append([
            InlineKeyboardButton(text="🗑 Пропустить страницу", callback_data=f"vp:inbox:skip_page_confirm:{page}"),
        ])

    nav = _nav_row("vp:inbox:new", page, has_next)
    if nav:
        keyboard_buttons.append(nav)
    keyboard_buttons.append([
        InlineKeyboardButton(text="🔄 Обновить", callback_data=f"vp:inbox:new:{page}"),
        InlineKeyboardButton(text="⬅️ Входящие", callback_data="vp:inbox"),
    ])

    await _edit_markdown(
        callback.message,
        _build_new_videos_text(stats, videos, page, has_next),
        reply_markup=InlineKeyboardMarkup(inline_keyboard=keyboard_buttons),
    )


async def _load_new_page(user_id: int, page: int, page_size: int) -> tuple[list[dict], bool]:
    inbox_data = await api_client.get_inbox_videos(
        user_id,
        status="new",
        limit=page_size + 1,
        offset=max(0, page) * page_size,
    )
    loaded = inbox_data.get("videos", [])
    return loaded[:page_size], len(loaded) > page_size


async def _confirm_page_action(callback: CallbackQuery, *, page: int, action: str) -> None:
    if action == "done":
        verb = "отметить готовыми"
        consequence = "Они переедут в *Выполненные* с пометкой «прокомментировано вручную»."
        confirm_label = "✅ Да, отметить"
    else:
        verb = "пропустить"
        consequence = "Они переедут в *Пропущенные*. Оттуда можно вернуть кнопкой *↩️ Вернуть в новые*."
        confirm_label = "🗑 Да, пропустить"
    do_callback = f"vp:inbox:{action}_page_do:{page}"
    await _edit_markdown(
        callback.message,
        screen(
            "⚠️ Подтверждение",
            f"Вы точно хотите {verb} все новые видео на странице *{page + 1}*?",
            consequence,
        ),
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text=confirm_label, callback_data=do_callback)],
            [InlineKeyboardButton(text="⬅️ Назад", callback_data=f"vp:inbox:new:{page}")],
        ]),
    )


async def _apply_page_action(callback: CallbackQuery, state: FSMContext, *, page: int, action: str) -> None:
    user_id = await _get_user_id(callback)
    if not user_id:
        return
    videos, _ = await _load_new_page(user_id, page, INBOX_PAGE_SIZE)
    video_ids = [int(video.get("id")) for video in videos if video.get("id")]
    if not video_ids:
        await _answer_error(callback, "На странице нет новых видео")
        return
    if action == "done":
        result = await api_client.mark_videos_commented_batch(video_ids, user_id)
        await callback.answer(f"✅ Отмечено: {result.get('marked', len(video_ids))}", show_alert=False)
    else:
        result = await api_client.skip_videos_batch(video_ids, user_id)
        await callback.answer(f"🗑 Пропущено: {result.get('skipped', len(video_ids))}", show_alert=False)
    await _show_new_videos_page(callback, state, page=max(0, page))


@router.callback_query(F.data == "vp:inbox:noop")
async def inbox_noop(callback: CallbackQuery):
    await callback.answer()


@router.callback_query(F.data == "vp:inbox")
async def show_inbox(callback: CallbackQuery, state: FSMContext):
    try:
        await callback.answer()
    except TelegramBadRequest:
        pass
    user_id = await _get_user_id(callback)
    if not user_id or not callback.message:
        return

    # ``get_inbox_videos`` уже возвращает ``by_status`` — отдельный вызов
    # ``get_inbox_stats`` для overview-экрана был лишним round-trip'ом.
    inbox_data = await api_client.get_inbox_videos(user_id, status="new", limit=5)
    videos = inbox_data.get("videos", [])
    stats = inbox_data.get("by_status") or {}

    keyboard_buttons: list[list[InlineKeyboardButton]] = []
    new_count = int(stats.get("new", 0) or 0)
    failed_count = int(stats.get("failed", 0) or 0)

    if new_count:
        keyboard_buttons.append([InlineKeyboardButton(text=f"📋 Открыть новые ({new_count})", callback_data="vp:inbox:new:0")])
        keyboard_buttons.append([InlineKeyboardButton(text=_bulk_action_text("order", new_count), callback_data="vp:inbox:comment_all")])
        for offset, video in enumerate(videos[:INBOX_PAGE_SIZE], start=1):
            keyboard_buttons.append(_video_action_row(video, offset))
        keyboard_buttons.append([
            InlineKeyboardButton(text="🎯 Выбрать несколько", callback_data="vp:inbox:select"),
            InlineKeyboardButton(text="👤 По блогерам", callback_data="vp:inbox:creators:0"),
        ])
        keyboard_buttons.append([InlineKeyboardButton(text=_bulk_action_text("skip", new_count), callback_data="vp:inbox:skip_all")])
    else:
        keyboard_buttons.append([InlineKeyboardButton(text="👤 По блогерам", callback_data="vp:inbox:creators:0")])

    if failed_count:
        keyboard_buttons.append([InlineKeyboardButton(text=f"🚨 Ошибки ({failed_count})", callback_data="vp:inbox:errors:0")])
    keyboard_buttons.append([
        InlineKeyboardButton(text="📋 Все статусы", callback_data="vp:inbox:all"),
        InlineKeyboardButton(text="🔄 Обновить", callback_data="vp:inbox"),
    ])
    keyboard_buttons.append([InlineKeyboardButton(text="⬅️ Назад", callback_data="vp:menu")])

    await _edit_markdown(
        callback.message,
        _build_inbox_overview_text(stats, videos),
        reply_markup=InlineKeyboardMarkup(inline_keyboard=keyboard_buttons),
    )


@router.callback_query(F.data.startswith("vp:inbox:new:"))
async def show_new_videos_page(callback: CallbackQuery, state: FSMContext):
    try:
        page = int((callback.data or "").split(":")[-1])
    except (TypeError, ValueError):
        page = 0
    await _show_new_videos_page(callback, state, page=page)


@router.callback_query(F.data == "vp:inbox:comment_all")
async def comment_all_videos(callback: CallbackQuery, state: FSMContext):
    """Запускает все новые видео, разделяя batch по блогерам."""
    try:
        await callback.answer()
        user_id = await _get_user_id(callback)
        if not user_id:
            return
        inbox_data = await api_client.get_inbox_videos(user_id, status="new", limit=INBOX_BULK_LIMIT)
        videos = _available_new_videos(inbox_data.get("videos", []))
        if not videos:
            await callback.answer("Нет новых видео для комментирования", show_alert=True)
            return

        await _start_order_for_videos(
            callback,
            state,
            user_id=user_id,
            videos=videos,
            back_callback="vp:inbox",
            grouped_title=_bulk_action_text("order", int(inbox_data.get("by_status", {}).get("new", len(videos)) or len(videos))),
        )
        # Callback format intentionally stays compatible:
        # vp:new_videos:select_template:{creator_id}:{batch_id}
    except Exception as e:
        logger.error(f"Error commenting all videos: {e}", exc_info=True)
        await _answer_error(callback)


@router.callback_query(F.data.startswith("vp:inbox:group_order:"))
async def order_grouped_creator_videos(callback: CallbackQuery, state: FSMContext):
    try:
        await callback.answer()
        creator_id = int((callback.data or "").split(":")[-1])
        user_id = await _get_user_id(callback)
        if not user_id:
            return
        data = await state.get_data()
        grouped_ids = data.get("inbox_grouped_order_video_ids") or {}
        back_callback = str(data.get("inbox_grouped_order_back") or "vp:inbox")
        video_ids = [int(video_id) for video_id in grouped_ids.get(str(creator_id), []) if video_id]
        if not video_ids:
            await callback.answer("Выбор устарел. Откройте входящие заново.", show_alert=True)
            return
        await _open_template_flow(
            callback,
            state,
            user_id=user_id,
            creator_id=creator_id,
            video_ids=video_ids,
            back_callback=back_callback,
        )
    except Exception as e:
        logger.error(f"Error ordering grouped inbox videos: {e}", exc_info=True)
        await _answer_error(callback)


@router.callback_query(F.data.startswith("vp:inbox:order_page:"))
async def order_new_page(callback: CallbackQuery, state: FSMContext):
    try:
        await callback.answer()
        page = int((callback.data or "").split(":")[-1])
        user_id = await _get_user_id(callback)
        if not user_id:
            return
        videos, _ = await _load_new_page(user_id, page, INBOX_PAGE_SIZE)
        await _start_order_for_videos(callback, state, user_id=user_id, videos=videos, back_callback=f"vp:inbox:new:{page}")
    except Exception as e:
        logger.error(f"Error ordering inbox page: {e}", exc_info=True)
        await _answer_error(callback)


@router.callback_query(F.data.startswith("vp:inbox:order:"))
async def order_single_video(callback: CallbackQuery, state: FSMContext):
    try:
        await callback.answer()
        video_id, context_parts = _parse_video_action(callback.data)
        user_id = await _get_user_id(callback)
        if not user_id:
            return
        videos = await api_client.get_videos_by_ids([video_id], user_id)
        await _start_order_for_videos(
            callback,
            state,
            user_id=user_id,
            videos=videos,
            back_callback=_context_back_callback(context_parts),
        )
    except Exception as e:
        logger.error(f"Error ordering inbox video: {e}", exc_info=True)
        await _answer_error(callback)


async def _refresh_after_video_action(callback: CallbackQuery, state: FSMContext, context_parts: list[str]) -> None:
    if len(context_parts) >= 2 and context_parts[0] == "new":
        try:
            await _show_new_videos_page(callback, state, page=max(0, int(context_parts[1])))
            return
        except (TypeError, ValueError):
            pass
    if len(context_parts) >= 3 and context_parts[0] == "creator":
        try:
            await _show_creator_detail(callback, creator_id=int(context_parts[1]), page=max(0, int(context_parts[2])))
            return
        except (TypeError, ValueError):
            pass
    if len(context_parts) >= 3 and context_parts[0] == "status":
        try:
            status = str(context_parts[1] or "").strip() or "new"
            page = max(0, int(context_parts[2]))
            if status == "new":
                await _show_new_videos_page(callback, state, page=page)
            else:
                await _show_status_page(callback, status, page)
            return
        except (TypeError, ValueError):
            pass
    await _show_new_videos_page(callback, state, page=0)


@router.callback_query(F.data.startswith("vp:inbox:done:"))
async def mark_single_done(callback: CallbackQuery, state: FSMContext):
    try:
        video_id, context_parts = _parse_video_action(callback.data)
        user_id = await _get_user_id(callback)
        if not user_id:
            return
        await api_client.mark_videos_commented_batch([video_id], user_id)
        await callback.answer("✅ Отмечено", show_alert=False)
        await _refresh_after_video_action(callback, state, context_parts)
    except Exception as e:
        logger.error(f"Error marking inbox video done: {e}", exc_info=True)
        await _answer_error(callback)


@router.callback_query(F.data.startswith("vp:inbox:skip:"))
async def skip_single_video(callback: CallbackQuery, state: FSMContext):
    try:
        video_id, context_parts = _parse_video_action(callback.data)
        user_id = await _get_user_id(callback)
        if not user_id:
            return
        await api_client.skip_videos_batch([video_id], user_id)
        await callback.answer("🗑 Пропущено", show_alert=False)
        await _refresh_after_video_action(callback, state, context_parts)
    except Exception as e:
        logger.error(f"Error skipping inbox video: {e}", exc_info=True)
        await _answer_error(callback)


@router.callback_query(F.data.startswith("vp:inbox:retry:"))
async def retry_failed_video(callback: CallbackQuery, state: FSMContext):
    """Перезапуск упавшего видео: reset-to-new → выбор шаблона.

    Сценарий: пользователь сидит в табе «Ошибки», видит «Аккаунт отклонён»
    под видео, хочет повторить с другим шаблоном/аккаунтом. Нажимает 🔁 —
    мы возвращаем видео в new, очищаем task_id и сразу пускаем в обычный
    template-picker, чтобы юзер не делал лишний шаг.
    """
    try:
        video_id, context_parts = _parse_video_action(callback.data)
        user_id = await _get_user_id(callback)
        if not user_id:
            return
        try:
            await api_client.reset_video_to_new(video_id, user_id)
        except Exception as exc:
            err = str(exc)
            # Бэкенд отвечает 409, если видео уже не в failed (race condition).
            if "409" in err or "failed" in err:
                await callback.answer("Видео уже не в статусе «ошибка»", show_alert=True)
                await _refresh_after_video_action(callback, state, context_parts)
                return
            logger.error(f"reset_video_to_new error: {exc}", exc_info=True)
            await _answer_error(callback, "Ошибка повтора")
            return

        await callback.answer()
        videos = await api_client.get_videos_by_ids([video_id], user_id)
        if not videos:
            # Странно — только что сделали reset, а видео исчезло. Откатываемся
            # на refresh, юзер увидит актуальную картину.
            await _refresh_after_video_action(callback, state, context_parts)
            return
        await _start_order_for_videos(
            callback,
            state,
            user_id=user_id,
            videos=videos,
            back_callback=_context_back_callback(context_parts),
        )
    except Exception as e:
        logger.error(f"Error retrying failed video: {e}", exc_info=True)
        await _answer_error(callback)


@router.callback_query(F.data.startswith("vp:inbox:unskip:"))
async def unskip_video_handler(callback: CallbackQuery, state: FSMContext):
    """Возврат пропущенного видео в новые. Перезагружаем тот же status-таб."""
    try:
        video_id, context_parts = _parse_video_action(callback.data)
        user_id = await _get_user_id(callback)
        if not user_id:
            return
        try:
            await api_client.unskip_video(video_id, user_id)
        except Exception as exc:
            err = str(exc)
            if "409" in err or "skipped" in err:
                await callback.answer("Видео уже не в статусе «пропущено»", show_alert=True)
                await _refresh_after_video_action(callback, state, context_parts)
                return
            logger.error(f"unskip_video error: {exc}", exc_info=True)
            await _answer_error(callback, "Ошибка возврата")
            return
        await callback.answer("↩️ Возвращено в новые", show_alert=False)
        await _refresh_after_video_action(callback, state, context_parts)
    except Exception as e:
        logger.error(f"Error unskipping inbox video: {e}", exc_info=True)
        await _answer_error(callback)


@router.callback_query(F.data.startswith("vp:inbox:stop:"))
async def stop_queued_video(callback: CallbackQuery, state: FSMContext):
    """Остановка задачи, в которой выполняется выбранное видео.

    stop_task на бэкенде требует task.status == in_progress. На UI мы
    показываем кнопку только если ``task_status == 'in_progress'``, но за
    время между показом и кликом задача могла перейти в другой статус —
    обрабатываем 409 человеческим alert'ом, не как ошибку.
    """
    try:
        video_id, context_parts = _parse_video_action(callback.data)
        user_id = await _get_user_id(callback)
        if not user_id:
            return
        videos = await api_client.get_videos_by_ids([video_id], user_id)
        task_id = (videos or [{}])[0].get("task_id") if videos else None
        if not task_id:
            await callback.answer("У видео нет связанного заказа", show_alert=True)
            return
        try:
            await api_client.stop_task(int(task_id), user_id)
            await callback.answer("⏹ Заказ остановлен", show_alert=False)
        except Exception as exc:
            err = str(exc)
            if "409" in err or "in_progress" in err:
                await callback.answer("Заказ уже не выполняется", show_alert=True)
            else:
                logger.error(f"stop_task error: {exc}", exc_info=True)
                await _answer_error(callback, "Ошибка остановки")
                return
        await _refresh_after_video_action(callback, state, context_parts)
    except Exception as e:
        logger.error(f"Error stopping queued video: {e}", exc_info=True)
        await _answer_error(callback)


@router.callback_query(F.data.startswith("vp:inbox:done_page_confirm:"))
async def done_page_confirm(callback: CallbackQuery):
    try:
        await callback.answer()
        page = int((callback.data or "").split(":")[-1])
        await _confirm_page_action(callback, page=page, action="done")
    except Exception as e:
        logger.error(f"Error confirming inbox page done: {e}", exc_info=True)
        await _answer_error(callback)


@router.callback_query(F.data.startswith("vp:inbox:skip_page_confirm:"))
async def skip_page_confirm(callback: CallbackQuery):
    try:
        await callback.answer()
        page = int((callback.data or "").split(":")[-1])
        await _confirm_page_action(callback, page=page, action="skip")
    except Exception as e:
        logger.error(f"Error confirming inbox page skip: {e}", exc_info=True)
        await _answer_error(callback)


@router.callback_query(F.data.startswith("vp:inbox:done_page_do:"))
async def done_page_do(callback: CallbackQuery, state: FSMContext):
    page = int((callback.data or "").split(":")[-1])
    await _apply_page_action(callback, state, page=page, action="done")


@router.callback_query(F.data.startswith("vp:inbox:skip_page_do:"))
async def skip_page_do(callback: CallbackQuery, state: FSMContext):
    page = int((callback.data or "").split(":")[-1])
    await _apply_page_action(callback, state, page=page, action="skip")


@router.callback_query(F.data == "vp:inbox:skip_all")
async def skip_all_confirm(callback: CallbackQuery):
    try:
        await callback.answer()
        user_id = await _get_user_id(callback)
        if not user_id:
            return
        stats = await api_client.get_inbox_stats(user_id)
        new_count = int(stats.get("new", 0) or 0)
        if not new_count:
            await callback.answer("Новых видео нет", show_alert=True)
            return
        await _edit_markdown(
            callback.message,
            screen(
                "⚠️ Подтверждение",
                f"Пропустить все новые видео? Будет обработано *{new_count}*.",
                "Они переедут в *Пропущенные*. Оттуда можно вернуть кнопкой *↩️ Вернуть в новые*.",
            ),
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="🗑 Да, пропустить", callback_data="vp:inbox:skip_all_do")],
                [InlineKeyboardButton(text="⬅️ Назад", callback_data="vp:inbox")],
            ]),
        )
    except Exception as e:
        logger.error(f"Error confirming skip all inbox videos: {e}", exc_info=True)
        await _answer_error(callback)


@router.callback_query(F.data == "vp:inbox:skip_all_do")
async def skip_all_videos(callback: CallbackQuery, state: FSMContext):
    try:
        user_id = await _get_user_id(callback)
        if not user_id:
            return
        result = await api_client.skip_all_new_inbox_videos(user_id)
        skipped = int(result.get("skipped", 0) or 0)
        if not skipped:
            await callback.answer("Новых видео нет", show_alert=True)
            return
        await callback.answer(f"🗑 Пропущено {skipped}", show_alert=False)
        await show_inbox(callback, state)
    except Exception as e:
        logger.error(f"Error skipping all inbox videos: {e}", exc_info=True)
        await _answer_error(callback)


@router.callback_query(F.data == "vp:inbox:all")
async def show_all_inbox_videos(callback: CallbackQuery, state: FSMContext):
    try:
        await callback.answer()
        user_id = await _get_user_id(callback)
        if not user_id:
            return
        stats = await api_client.get_inbox_stats(user_id)
        keyboard = InlineKeyboardMarkup(inline_keyboard=[
            [
                InlineKeyboardButton(text=f"🆕 Новые ({stats.get('new', 0)})", callback_data="vp:inbox:new:0"),
                InlineKeyboardButton(text=f"⏳ Очередь ({stats.get('queued', 0)})", callback_data="vp:inbox:filter:queued:0"),
            ],
            [
                InlineKeyboardButton(text=f"✅ Готово ({stats.get('done', 0)})", callback_data="vp:inbox:filter:done:0"),
                InlineKeyboardButton(text=f"🚨 Ошибки ({stats.get('failed', 0)})", callback_data="vp:inbox:errors:0"),
            ],
            [
                InlineKeyboardButton(text=f"⏭ Пропущено ({stats.get('skipped', 0)})", callback_data="vp:inbox:filter:skipped:0"),
                InlineKeyboardButton(text="⬅️ Назад", callback_data="vp:inbox"),
            ],
        ])
        await _edit_markdown(callback.message, _build_filters_text(stats), reply_markup=keyboard)
    except Exception as e:
        logger.error(f"Error showing all inbox videos: {e}", exc_info=True)
        await _answer_error(callback)


def _status_action_row(video: dict, index: int, status: str, *, context: str) -> list[InlineKeyboardButton]:
    """Per-video actions для не-new табов.

    Раньше status-страницы показывали только 🎬 и опциональную «📋 Заказ #N».
    Юзер на failed/skipped/queued ничего сделать не мог — приходилось уходить
    в раздел заказов или ждать. Тут поднимаем actions ровно те, которые
    имеют смысл в каждом статусе:

      - ``failed``  → 🔁 Повторить (через reset-to-new) + 📋 Заказ + 🗑 Скип
      - ``skipped`` → ↩️ Вернуть в новые
      - ``queued``  → 📋 Заказ + ⏹ Остановить (если task.status=in_progress)
      - ``done``    → 📋 Заказ (если есть)
    """
    row: list[InlineKeyboardButton] = []
    url = normalize_video_url(video.get("url"))
    video_id = video.get("id")
    suffix = f":{context}" if context else ""

    if url:
        row.append(InlineKeyboardButton(text=f"🎬 #{index}", url=url))

    if status == "failed":
        row.append(InlineKeyboardButton(text="🔁 Повторить", callback_data=f"vp:inbox:retry:{video_id}{suffix}"))
        task_button = _task_button(video)
        if task_button:
            row.append(task_button)
        row.append(InlineKeyboardButton(text="🗑 Скип", callback_data=f"vp:inbox:skip:{video_id}{suffix}"))
    elif status == "skipped":
        row.append(InlineKeyboardButton(text="↩️ Вернуть в новые", callback_data=f"vp:inbox:unskip:{video_id}{suffix}"))
    elif status == "queued":
        task_button = _task_button(video)
        if task_button:
            row.append(task_button)
        # ⏹ Остановить показываем только когда задача реально работает —
        # для pending/stopped/done stop_task всё равно вернёт 409 и засветит
        # юзера лишним alert'ом. Поле task_status пробрасывается из бэкенда.
        if str(video.get("task_status") or "").lower() == "in_progress":
            row.append(InlineKeyboardButton(text="⏹ Остановить", callback_data=f"vp:inbox:stop:{video_id}{suffix}"))
    elif status == "done":
        task_button = _task_button(video)
        if task_button:
            row.append(task_button)

    return row


async def _show_status_page(callback: CallbackQuery, status: str, page: int) -> None:
    user_id = await _get_user_id(callback)
    if not user_id:
        return
    inbox_data = await api_client.get_inbox_videos(
        user_id,
        status=status,
        limit=INBOX_PAGE_SIZE + 1,
        offset=max(0, page) * INBOX_PAGE_SIZE,
    )
    loaded = inbox_data.get("videos", [])
    has_next = len(loaded) > INBOX_PAGE_SIZE
    videos = loaded[:INBOX_PAGE_SIZE]
    total = int(inbox_data.get("by_status", {}).get(status, inbox_data.get("total", len(videos))) or 0)

    # Контекст для callback'ов: куда вернуться после действия. У filter и errors
    # разные prefix'ы, поэтому context хранит status+page и позволяет _refresh
    # точно вернуть юзера на ту же страницу.
    context = f"status:{status}:{max(0, page)}"

    keyboard_buttons: list[list[InlineKeyboardButton]] = []
    for offset, video in enumerate(videos, start=1 + page * INBOX_PAGE_SIZE):
        row = _status_action_row(video, offset, status, context=context)
        if row:
            keyboard_buttons.append(row)

    prefix = "vp:inbox:errors" if status == "failed" else f"vp:inbox:filter:{status}"
    nav = _nav_row(prefix, page, has_next)
    if nav:
        keyboard_buttons.append(nav)
    keyboard_buttons.append([InlineKeyboardButton(text="⬅️ Все статусы", callback_data="vp:inbox:all")])

    await _edit_markdown(
        callback.message,
        _build_status_page_text(status, videos, page, total),
        reply_markup=InlineKeyboardMarkup(inline_keyboard=keyboard_buttons),
    )


@router.callback_query(F.data.startswith("vp:inbox:filter:"))
async def filter_inbox_videos(callback: CallbackQuery, state: FSMContext):
    try:
        await callback.answer()
        parts = (callback.data or "").split(":")
        status = parts[3] if len(parts) > 3 else None
        page = int(parts[4]) if len(parts) > 4 else 0
        if status == "new":
            await _show_new_videos_page(callback, state, page=page)
            return
        if not status:
            await _answer_error(callback, "❌ Неверный фильтр")
            return
        await _show_status_page(callback, status, page)
    except Exception as e:
        logger.error(f"Error filtering inbox videos: {e}", exc_info=True)
        await _answer_error(callback)


@router.callback_query(F.data.startswith("vp:inbox:errors:"))
async def show_inbox_errors(callback: CallbackQuery):
    try:
        await callback.answer()
        page = int((callback.data or "").split(":")[-1])
        await _show_status_page(callback, "failed", page)
    except Exception as e:
        logger.error(f"Error showing inbox errors: {e}", exc_info=True)
        await _answer_error(callback)


async def _show_selection_page(callback: CallbackQuery, state: FSMContext, page: int = 0) -> None:
    try:
        await callback.answer()
    except TelegramBadRequest:
        pass
    user_id = await _get_user_id(callback)
    if not user_id:
        return
    stats = await api_client.get_inbox_stats(user_id)
    videos, has_next = await _load_new_page(user_id, page, INBOX_SELECT_PAGE_SIZE)
    data = await state.get_data()
    selected_ids = {int(item) for item in data.get("inbox_selected_video_ids", []) if item}
    await state.update_data(inbox_selection_page=max(0, page))

    keyboard_buttons: list[list[InlineKeyboardButton]] = []
    for offset, video in enumerate(videos, start=1 + page * INBOX_SELECT_PAGE_SIZE):
        video_id = int(video.get("id") or 0)
        marker = "☑️" if video_id in selected_ids else "☐"
        username = str(video.get("creator_username") or "unknown").strip().lstrip("@")
        keyboard_buttons.append([
            InlineKeyboardButton(
                text=f"{marker} #{offset} @{username} · {video_short_id(video, 10)}",
                callback_data=f"vp:inbox:select:toggle:{page}:{video_id}",
            )
        ])

    if videos:
        keyboard_buttons.append([
            InlineKeyboardButton(text="☑️ Выбрать страницу", callback_data=f"vp:inbox:select:page_all:{page}"),
            InlineKeyboardButton(text="🧹 Снять выбор", callback_data=f"vp:inbox:select:clear:{page}"),
        ])
    keyboard_buttons.append([
        InlineKeyboardButton(text=f"🚀 Продолжить ({len(selected_ids)})", callback_data="vp:inbox:select:continue")
    ])
    nav = _nav_row("vp:inbox:select:page", page, has_next)
    if nav:
        keyboard_buttons.append(nav)
    keyboard_buttons.append([InlineKeyboardButton(text="⬅️ Новые видео", callback_data=f"vp:inbox:new:{page}")])

    await _edit_markdown(
        callback.message,
        _build_selection_text(videos, selected_ids, page, int(stats.get("new", 0) or 0)),
        reply_markup=InlineKeyboardMarkup(inline_keyboard=keyboard_buttons),
    )


@router.callback_query(F.data == "vp:inbox:select")
async def select_inbox_videos(callback: CallbackQuery, state: FSMContext):
    await state.update_data(inbox_selected_video_ids=[])
    await _show_selection_page(callback, state, page=0)


@router.callback_query(F.data.startswith("vp:inbox:select:page:"))
async def select_inbox_page(callback: CallbackQuery, state: FSMContext):
    page = int((callback.data or "").split(":")[-1])
    await _show_selection_page(callback, state, page=page)


@router.callback_query(F.data.startswith("vp:inbox:select:toggle:"))
async def toggle_inbox_selection(callback: CallbackQuery, state: FSMContext):
    try:
        parts = (callback.data or "").split(":")
        page = int(parts[-2])
        video_id = int(parts[-1])
        data = await state.get_data()
        selected = {int(item) for item in data.get("inbox_selected_video_ids", []) if item}
        if video_id in selected:
            selected.remove(video_id)
        else:
            selected.add(video_id)
        await state.update_data(inbox_selected_video_ids=sorted(selected))
        await _show_selection_page(callback, state, page=page)
    except Exception as e:
        logger.error(f"Error toggling inbox selection: {e}", exc_info=True)
        await _answer_error(callback)


@router.callback_query(F.data.startswith("vp:inbox:select:page_all:"))
async def select_inbox_page_all(callback: CallbackQuery, state: FSMContext):
    try:
        page = int((callback.data or "").split(":")[-1])
        user_id = await _get_user_id(callback)
        if not user_id:
            return
        videos, _ = await _load_new_page(user_id, page, INBOX_SELECT_PAGE_SIZE)
        data = await state.get_data()
        selected = {int(item) for item in data.get("inbox_selected_video_ids", []) if item}
        selected.update(int(video.get("id")) for video in videos if video.get("id"))
        await state.update_data(inbox_selected_video_ids=sorted(selected))
        await _show_selection_page(callback, state, page=page)
    except Exception as e:
        logger.error(f"Error selecting inbox page: {e}", exc_info=True)
        await _answer_error(callback)


@router.callback_query(F.data.startswith("vp:inbox:select:clear:"))
async def clear_inbox_selection(callback: CallbackQuery, state: FSMContext):
    page = int((callback.data or "").split(":")[-1])
    await state.update_data(inbox_selected_video_ids=[])
    await _show_selection_page(callback, state, page=page)


@router.callback_query(F.data == "vp:inbox:select:continue")
async def continue_inbox_selection(callback: CallbackQuery, state: FSMContext):
    try:
        await callback.answer()
        user_id = await _get_user_id(callback)
        if not user_id:
            return
        data = await state.get_data()
        selected_ids = [int(item) for item in data.get("inbox_selected_video_ids", []) if item]
        if not selected_ids:
            await callback.answer("Выберите хотя бы одно видео", show_alert=True)
            return
        videos = await api_client.get_videos_by_ids(selected_ids, user_id)
        selection_page = max(0, int(data.get("inbox_selection_page", 0) or 0))
        await _start_order_for_videos(
            callback,
            state,
            user_id=user_id,
            videos=videos,
            back_callback=f"vp:inbox:select:page:{selection_page}",
        )
    except Exception as e:
        logger.error(f"Error continuing inbox selection: {e}", exc_info=True)
        await _answer_error(callback)


@router.callback_query(F.data.startswith("vp:inbox:creators:"))
async def show_inbox_creator_groups(callback: CallbackQuery):
    try:
        await callback.answer()
        page = int((callback.data or "").split(":")[-1])
        user_id = await _get_user_id(callback)
        if not user_id:
            return
        inbox_data = await api_client.get_inbox_videos(user_id, status="new", limit=INBOX_BULK_LIMIT)
        videos = _available_new_videos(inbox_data.get("videos", []))
        new_total = int(inbox_data.get("by_status", {}).get("new", len(videos)) or 0)
        grouped = _group_videos_by_creator(videos)
        groups = sorted(grouped.items(), key=lambda item: (-len(item[1]), str(item[1][0].get("creator_username") or "")))

        start = max(0, page) * INBOX_GROUP_PAGE_SIZE
        page_groups = groups[start:start + INBOX_GROUP_PAGE_SIZE]
        has_next = start + INBOX_GROUP_PAGE_SIZE < len(groups)

        lines = [f"Блогеров в списке: *{len(groups)}*"]
        if new_total > INBOX_BULK_LIMIT:
            lines.append(f"_Показаны свежие {INBOX_BULK_LIMIT} из {new_total} новых видео._")
        lines.append("")
        if not page_groups:
            lines.append("_Сейчас нет блогеров с новыми видео._")
        for idx, (_creator_id, creator_videos) in enumerate(page_groups, start=start + 1):
            first = creator_videos[0]
            username = (first.get("creator_username") or "unknown").strip().lstrip("@")
            lines.append(
                f"{idx}. {markdown_handle(username)} · новых: *{len(creator_videos)}*"
                f"\n   _последнее {escape_markdown(video_time_label(first))}_"
            )

        keyboard_buttons = [
            [
                InlineKeyboardButton(
                    text=f"@{videos_for_creator[0].get('creator_username') or creator_id} · {len(videos_for_creator)}",
                    callback_data=f"vp:inbox:creator:{creator_id}:0",
                )
            ]
            for creator_id, videos_for_creator in page_groups
        ]
        nav = _nav_row("vp:inbox:creators", page, has_next)
        if nav:
            keyboard_buttons.append(nav)
        keyboard_buttons.append([InlineKeyboardButton(text="⬅️ Входящие", callback_data="vp:inbox")])

        await _edit_markdown(
            callback.message,
            screen("👤 По блогерам", "\n".join(lines)),
            reply_markup=InlineKeyboardMarkup(inline_keyboard=keyboard_buttons),
        )
    except Exception as e:
        logger.error(f"Error showing inbox creator groups: {e}", exc_info=True)
        await _answer_error(callback)


@router.callback_query(F.data.startswith("vp:inbox:creator_order:"))
async def order_creator_new_videos(callback: CallbackQuery, state: FSMContext):
    try:
        await callback.answer()
        creator_id = int((callback.data or "").split(":")[-1])
        user_id = await _get_user_id(callback)
        if not user_id:
            return
        videos = await api_client.list_video_videos(user_id=user_id, creator_id=creator_id, status="new", limit=INBOX_BULK_LIMIT)
        await _start_order_for_videos(callback, state, user_id=user_id, videos=videos, back_callback=f"vp:inbox:creator:{creator_id}:0")
    except Exception as e:
        logger.error(f"Error ordering creator inbox videos: {e}", exc_info=True)
        await _answer_error(callback)


async def _show_creator_detail(callback: CallbackQuery, *, creator_id: int, page: int) -> None:
    user_id = await _get_user_id(callback)
    if not user_id:
        return
    page = max(0, page)
    loaded = await api_client.list_video_videos(
        user_id=user_id,
        creator_id=creator_id,
        status="new",
        limit=INBOX_PAGE_SIZE + 1,
        offset=page * INBOX_PAGE_SIZE,
    )
    has_next = len(loaded) > INBOX_PAGE_SIZE
    videos = loaded[:INBOX_PAGE_SIZE]
    username_raw = (videos[0].get("creator_username") if videos else f"id {creator_id}") or "unknown"
    username = str(username_raw).strip().lstrip("@") or "unknown"

    lines = [f"Блогер: {markdown_handle(username)} · Страница *{page + 1}*"]
    if not videos:
        lines.append("")
        lines.append("_Новых видео у этого блогера нет._")
    else:
        lines.append("")
        for offset, video in enumerate(videos, start=1 + page * INBOX_PAGE_SIZE):
            lines.append(format_video_markdown_line(video, offset, include_username=False))

    keyboard_buttons: list[list[InlineKeyboardButton]] = []
    if videos:
        keyboard_buttons.append([InlineKeyboardButton(text="🚀 Заказать все новые", callback_data=f"vp:inbox:creator_order:{creator_id}")])
        for offset, video in enumerate(videos, start=1 + page * INBOX_PAGE_SIZE):
            keyboard_buttons.append(_video_action_row(video, offset, context=f"creator:{creator_id}:{page}"))
    nav = _nav_row(f"vp:inbox:creator:{creator_id}", page, has_next)
    if nav:
        keyboard_buttons.append(nav)
    keyboard_buttons.append([InlineKeyboardButton(text="⬅️ По блогерам", callback_data="vp:inbox:creators:0")])

    await _edit_markdown(
        callback.message,
        screen("👤 Входящие блогера", "\n".join(lines)),
        reply_markup=InlineKeyboardMarkup(inline_keyboard=keyboard_buttons),
    )


@router.callback_query(F.data.startswith("vp:inbox:creator:"))
async def show_inbox_creator_detail(callback: CallbackQuery):
    try:
        await callback.answer()
        parts = (callback.data or "").split(":")
        creator_id = int(parts[-2])
        page = int(parts[-1])
        await _show_creator_detail(callback, creator_id=creator_id, page=page)
    except Exception as e:
        logger.error(f"Error showing inbox creator detail: {e}", exc_info=True)
        await _answer_error(callback)
