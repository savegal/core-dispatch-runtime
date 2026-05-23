from aiogram import F, Router
from aiogram.filters import StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message

from bot.keyboards.composer import format_composer_summary, get_composer_keyboard
from bot.services.api_client import api_client
from bot.services.bot_ui import block, kv_rows, screen
from bot.services.drafts import get_draft, save_draft
from bot.services.formatting import markdown_inline_code
from bot.services.likes_ui import LIKES_DISABLED_LABEL
from bot.services.template_utils import (
    has_meaningful_text,
    is_replies_only_template,
    normalize_text_variants_list,
    normalize_user_text,
    preserve_nonempty_lines,
    template_replies,
    template_text_variants,
)
from bot.services.user_utils import get_user_backend_id
from bot.state.order_states import OrderStates
from bot.utils.markdown_escape import escape_markdown_simple
from config.settings import get_settings

import logging

logger = logging.getLogger("bot.templates")
router = Router()
settings = get_settings()


async def _format_template_composer_summary(draft) -> str:
    return format_composer_summary(draft, likes_label=LIKES_DISABLED_LABEL)


def _md(text: str | None) -> str:
    normalized = normalize_user_text(text)
    return escape_markdown_simple("" if normalized is None else normalized)


def _preview(text: str | None, limit: int = 120) -> str:
    normalized = (normalize_user_text(text) or "").replace("\n", " ")
    if len(normalized) > limit:
        normalized = normalized[:limit] + "..."
    return markdown_inline_code(normalized or "—")


def _extract_variant(raw_text: str | None) -> str | None:
    normalized = normalize_user_text(raw_text)
    if not has_meaningful_text(normalized):
        return None
    return normalized


def _template_settings(template: dict | None) -> dict:
    return dict((template or {}).get("settings") or {})


def _template_reply_list(template: dict | None) -> list[str]:
    return template_replies(template)[: settings.MAX_REPLIES]


def _variants_lines(variants: list[str]) -> list[str]:
    if not variants:
        return ["Варианты пока не заданы."]
    return [f"{idx}. {_preview(item, 220)}" for idx, item in enumerate(variants[:3], start=1)]


def _replies_lines(replies: list[str]) -> list[str]:
    if not replies:
        return ["Ответы не добавлены."]
    return [f"{idx}. {_preview(item, 140)}" for idx, item in enumerate(replies, start=1)]


def _template_detail_keyboard(template_id: int, *, replies_only: bool = False) -> InlineKeyboardMarkup:
    use_button = (
        InlineKeyboardButton(text="⚡ Заказ только на ответы", callback_data="menu:engagement_only")
        if replies_only
        else InlineKeyboardButton(text="✅ Использовать", callback_data=f"template:apply:{template_id}")
    )
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [use_button],
            [
                InlineKeyboardButton(text="✏️ Изменить", callback_data=f"template:edit:{template_id}"),
                InlineKeyboardButton(text="🗑 Удалить", callback_data=f"template:delete:{template_id}"),
            ],
            [InlineKeyboardButton(text="◀️ Назад", callback_data="menu:templates")],
        ]
    )


def _template_edit_keyboard(template_id: int, *, replies_only: bool = False) -> InlineKeyboardMarkup:
    rows = [[InlineKeyboardButton(text="✏️ Название", callback_data=f"template:edit_name:{template_id}")]]
    if not replies_only:
        rows.append([InlineKeyboardButton(text="💬 Комментарии", callback_data=f"template:edit_comment:{template_id}")])
    rows.append([InlineKeyboardButton(text="↩️ Ответы", callback_data=f"template:edit_replies:{template_id}")])
    rows.append([InlineKeyboardButton(text="◀️ Назад", callback_data=f"template:detail:{template_id}")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def _build_template_detail_text(template: dict) -> str:
    name = _md(template.get("name") or "Без названия")
    variants = template_text_variants(template)
    replies = _template_reply_list(template)
    replies_only = is_replies_only_template(template)
    type_label = "только ответы" if replies_only else "полный"

    parts = []
    if not replies_only:
        parts.append(block("Комментарии", _variants_lines(variants)))
    parts.extend([
        block("Ответы", _replies_lines(replies)),
        block(
            "Параметры",
            kv_rows(
                [
                    ("Тип", f"*{type_label}*"),
                    ("↩️ Ответов", f"*{len(replies)}*"),
                ]
            ),
        ),
    ])
    return screen(f"Шаблон · {name}", *parts)


def _build_template_edit_text(template: dict) -> str:
    name = _md(template.get("name") or "Без названия")
    variants = template_text_variants(template)
    replies = _template_reply_list(template)
    replies_only = is_replies_only_template(template)

    return screen(
        f"Редактирование · {name}",
        block(
            "Сейчас в шаблоне",
            [
                f"Название: *{name}*",
                f"Тип: *{'только ответы' if replies_only else 'полный'}*",
                f"Комментариев: *{len(variants)}*" if not replies_only else "Комментариев: *не используется*",
                f"Ответов: *{len(replies)}*",
            ],
        ),
        "Выберите, что хотите изменить.",
    )


def _build_delete_confirm_text(name: str) -> str:
    return screen(
        f"Удалить шаблон «{_md(name)}»?",
        "Шаблон будет удалён без возможности восстановления.",
    )


def _build_name_prompt_text(current_name: str) -> str:
    return screen(
        "Название шаблона",
        f"Сейчас: *{_md(current_name or 'Без названия')}*",
        "Отправьте новое название одним сообщением.",
    )


def _build_comment_prompt_text(current_variants: list[str], next_variant: int) -> str:
    parts = [
        "Отправьте 3 варианта комментария по одному сообщению.",
        "Бот сохранит каждый вариант отдельно и опубликует его без смешивания.",
    ]
    if current_variants:
        parts.append(block("Уже сохранено", _variants_lines(current_variants)))
    parts.append(f"*Вариант комментария {next_variant} из 3*")
    return screen("Комментарии шаблона", *parts)


def _build_replies_prompt_text(current_replies: list[str], collected_replies: list[str] | None = None) -> str:
    collected_replies = collected_replies or []
    parts = [
        block("Сейчас", _replies_lines(current_replies)),
        "Отправьте ответы по одному сообщению.",
        f"*Ответ {len(collected_replies) + 1} из {settings.MAX_REPLIES}*",
    ]
    if collected_replies:
        parts.insert(1, block("Уже сохранено", _replies_lines(collected_replies)))
        parts.append("Когда ответов достаточно, нажмите «Готово».")
    else:
        parts.append("Если ответы не нужны, отправьте `/skip` или нажмите «Очистить ответы».")
    return screen("Ответы шаблона", *parts)


def _build_likes_prompt_text(current_likes: int) -> str:
    return screen(
        "Лайки",
        "Лайки задаются в общих настройках сервиса и применяются к новым заказам.",
        "В шаблоне хранится только текст комментариев и ответы.",
    )


async def _load_template_for_user(telegram_user_id: int, template_id: int) -> tuple[int | None, dict | None]:
    user_backend_id = await get_user_backend_id(telegram_user_id)
    if not user_backend_id:
        return None, None
    template = await api_client.get_task_template(template_id, user_backend_id)
    return user_backend_id, template


async def _show_template_detail(message, template: dict) -> None:
    await message.edit_text(
        _build_template_detail_text(template),
        reply_markup=_template_detail_keyboard(
            template["id"],
            replies_only=is_replies_only_template(template),
        ),
        parse_mode="Markdown",
    )


@router.callback_query(F.data.startswith("template:detail:"))
async def show_template_detail(callback: CallbackQuery):
    try:
        if not callback.message:
            await callback.answer("Сообщение не найдено", show_alert=True)
            return

        template_id = int(callback.data.split(":")[-1])
        _, template = await _load_template_for_user(callback.from_user.id, template_id)
        if not template:
            await callback.answer("Шаблон не найден", show_alert=True)
            return

        await _show_template_detail(callback.message, template)
        await callback.answer()
    except Exception as e:
        logger.error(f"Error showing template detail: {e}", exc_info=True)
        await callback.answer("Не удалось открыть шаблон", show_alert=True)


@router.callback_query(F.data.startswith("template:apply:"))
async def apply_task_template(callback: CallbackQuery):
    try:
        if not callback.message:
            await callback.answer("Сообщение не найдено", show_alert=True)
            return

        template_id = int(callback.data.split(":")[-1])
        _, template = await _load_template_for_user(callback.from_user.id, template_id)
        if not template:
            await callback.answer("Шаблон не найден", show_alert=True)
            return
        if is_replies_only_template(template):
            await callback.answer("Этот шаблон подходит только для заказов на ответы", show_alert=True)
            return

        draft = get_draft(callback.from_user.id)
        variants = template_text_variants(template)
        template_settings = _template_settings(template)

        # Защита от старых шаблонов: если в шаблоне меньше 3 вариантов
        # текста главного коммента, его нельзя использовать с user-schema
        # workflow (он требует ровно 3 для серии публикаций с одного
        # аккаунта). Подсказываем пользователю как починить.
        if len(variants) < 3:
            await callback.message.edit_text(
                "📑 *Шаблон устарел*\n\n"
                f"В шаблоне сейчас *{len(variants)}* вариант(а) комментария, "
                "а для запуска нужно ровно *3* разных текста.\n\n"
                "Откройте шаблон → «Редактировать» и добавьте недостающие варианты "
                "(или создайте новый заказ через мастер).",
                reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                    [InlineKeyboardButton(text="✏️ Редактировать шаблон", callback_data=f"template:edit:{template_id}")],
                    [InlineKeyboardButton(text="📝 Полный мастер заказа", callback_data="menu:new_order")],
                    [InlineKeyboardButton(text="◀️ К шаблонам", callback_data="menu:templates")],
                ]),
                parse_mode="Markdown",
            )
            await callback.answer("Нужно 3 варианта", show_alert=True)
            return

        draft.comment = variants[0]
        draft.text_variants = variants
        draft.likes = template.get("likes", 0)
        draft.delays = template_settings.get("delays", True)
        draft.shuffle = template_settings.get("shuffle", False)
        draft.rotate_accounts = template_settings.get("rotate_accounts", True)
        draft.reply_mode = template_settings.get("reply_mode", "A")
        save_draft(callback.from_user.id, draft)

        await callback.message.edit_text(
            await _format_template_composer_summary(draft),
            reply_markup=get_composer_keyboard(draft),
            parse_mode="Markdown",
        )
        await callback.answer("Шаблон применён")
    except Exception as e:
        logger.error(f"Error applying template: {e}", exc_info=True)
        await callback.answer("Не удалось применить шаблон", show_alert=True)


@router.callback_query(F.data.startswith("template:delete:"))
async def delete_template(callback: CallbackQuery):
    try:
        if not callback.message:
            await callback.answer("Сообщение не найдено", show_alert=True)
            return

        template_id = int(callback.data.split(":")[-1])
        _, template = await _load_template_for_user(callback.from_user.id, template_id)
        if not template:
            await callback.answer("Шаблон не найден", show_alert=True)
            return

        await callback.message.edit_text(
            _build_delete_confirm_text(template.get("name") or "шаблон"),
            reply_markup=InlineKeyboardMarkup(
                inline_keyboard=[
                    [
                        InlineKeyboardButton(text="🗑 Да, удалить", callback_data=f"template:delete_confirm:{template_id}"),
                        InlineKeyboardButton(text="◀️ Отмена", callback_data=f"template:detail:{template_id}"),
                    ]
                ]
            ),
            parse_mode="Markdown",
        )
        await callback.answer()
    except Exception as e:
        logger.error(f"Error deleting template: {e}", exc_info=True)
        await callback.answer("Не удалось открыть подтверждение", show_alert=True)


@router.callback_query(F.data.startswith("template:delete_confirm:"))
async def delete_template_confirm(callback: CallbackQuery):
    try:
        if not callback.message:
            await callback.answer("Сообщение не найдено", show_alert=True)
            return

        template_id = int(callback.data.split(":")[-1])
        user_backend_id = await get_user_backend_id(callback.from_user.id)
        if not user_backend_id:
            await callback.answer("Пользователь не найден", show_alert=True)
            return

        await api_client.delete_task_template(template_id, user_backend_id)
        await callback.message.edit_text(
            screen(
                "Шаблон удалён",
                "Шаблон удалён и больше не будет доступен для запуска.",
            ),
            reply_markup=InlineKeyboardMarkup(
                inline_keyboard=[[InlineKeyboardButton(text="📑 К шаблонам", callback_data="menu:templates")]]
            ),
            parse_mode="Markdown",
        )
        await callback.answer("Шаблон удалён")
    except Exception as e:
        logger.error(f"Error confirming template deletion: {e}", exc_info=True)
        await callback.answer("Не удалось удалить шаблон", show_alert=True)


@router.callback_query(F.data.startswith("template:edit:"))
async def edit_template(callback: CallbackQuery, state: FSMContext):
    try:
        if not callback.message:
            await callback.answer("Сообщение не найдено", show_alert=True)
            return

        template_id = int(callback.data.split(":")[-1])
        user_backend_id, template = await _load_template_for_user(callback.from_user.id, template_id)
        if not user_backend_id or not template:
            await callback.answer("Шаблон не найден", show_alert=True)
            return

        await state.update_data(editing_template_id=template_id, editing_template=template)
        await callback.message.edit_text(
            _build_template_edit_text(template),
            reply_markup=_template_edit_keyboard(
                template_id,
                replies_only=is_replies_only_template(template),
            ),
            parse_mode="Markdown",
        )
        await callback.answer()
    except Exception as e:
        logger.error(f"Error editing template: {e}", exc_info=True)
        await callback.answer("Не удалось открыть редактирование", show_alert=True)


async def _get_or_load_editing_template(user_id: int, state: FSMContext, template_id: int) -> dict | None:
    data = await state.get_data()
    template = data.get("editing_template")
    if template and template.get("id") == template_id:
        return template
    _, loaded = await _load_template_for_user(user_id, template_id)
    if loaded:
        await state.update_data(editing_template=loaded)
    return loaded


@router.callback_query(F.data.startswith("template:edit_name:"))
async def edit_template_name_start(callback: CallbackQuery, state: FSMContext):
    try:
        if not callback.message:
            await callback.answer("Сообщение не найдено", show_alert=True)
            return

        template_id = int(callback.data.split(":")[-1])
        template = await _get_or_load_editing_template(callback.from_user.id, state, template_id)
        if not template:
            await callback.answer("Шаблон не найден", show_alert=True)
            return
        await state.update_data(editing_template_id=template_id)
        await state.set_state(OrderStates.EDIT_TEMPLATE_NAME)
        await callback.message.edit_text(
            _build_name_prompt_text(template.get("name") or "Без названия"),
            reply_markup=InlineKeyboardMarkup(
                inline_keyboard=[[InlineKeyboardButton(text="❌ Отмена", callback_data=f"template:edit:{template_id}")]]
            ),
            parse_mode="Markdown",
        )
        await callback.answer()
    except Exception as e:
        logger.error(f"Error starting template name edit: {e}", exc_info=True)
        await callback.answer("Не удалось изменить название", show_alert=True)


@router.message(StateFilter(OrderStates.EDIT_TEMPLATE_NAME))
async def edit_template_name_handler(message: Message, state: FSMContext):
    data = await state.get_data()
    template_id = data.get("editing_template_id")
    new_name = (message.text or "").strip()

    if not template_id:
        await message.answer("Шаблон не найден. Попробуйте открыть его заново.")
        await state.clear()
        return
    if not new_name:
        await message.answer("Название не может быть пустым. Отправьте новое название ещё раз.")
        return

    try:
        user_backend_id = await get_user_backend_id(message.from_user.id)
        if not user_backend_id:
            await message.answer("Пользователь не найден.")
            await state.clear()
            return

        await api_client.update_task_template(template_id, user_backend_id, name=new_name)
        await state.clear()
        await message.answer(
            screen("Шаблон обновлён", f"Новое название: *{_md(new_name)}*"),
            reply_markup=InlineKeyboardMarkup(
                inline_keyboard=[[InlineKeyboardButton(text="◀️ К шаблону", callback_data=f"template:detail:{template_id}")]]
            ),
            parse_mode="Markdown",
        )
    except Exception as e:
        logger.error(f"Error updating template name: {e}", exc_info=True)
        await message.answer(f"Не удалось обновить название: {str(e)[:200]}")


@router.callback_query(F.data.startswith("template:edit_comment:"))
async def edit_template_comment_start(callback: CallbackQuery, state: FSMContext):
    try:
        if not callback.message:
            await callback.answer("Сообщение не найдено", show_alert=True)
            return

        template_id = int(callback.data.split(":")[-1])
        template = await _get_or_load_editing_template(callback.from_user.id, state, template_id)
        if not template:
            await callback.answer("Шаблон не найден", show_alert=True)
            return
        if is_replies_only_template(template):
            await callback.answer("В этом шаблоне редактируются только ответы", show_alert=True)
            return
        current_variants = template_text_variants(template)

        await state.update_data(
            editing_template_id=template_id,
            template_edit_variant_index=1,
            template_edit_variants=[],
        )
        await state.set_state(OrderStates.EDIT_TEMPLATE_MAIN_COMMENT)
        await callback.message.edit_text(
            _build_comment_prompt_text(current_variants, 1),
            reply_markup=InlineKeyboardMarkup(
                inline_keyboard=[[InlineKeyboardButton(text="❌ Отмена", callback_data=f"template:edit:{template_id}")]]
            ),
            parse_mode="Markdown",
        )
        await callback.answer()
    except Exception as e:
        logger.error(f"Error starting template comment edit: {e}", exc_info=True)
        await callback.answer("Не удалось изменить комментарии", show_alert=True)


@router.message(StateFilter(OrderStates.EDIT_TEMPLATE_MAIN_COMMENT))
async def edit_template_comment_handler(message: Message, state: FSMContext):
    data = await state.get_data()
    template_id = data.get("editing_template_id")
    template = data.get("editing_template") or {}
    current_variants = normalize_text_variants_list(data.get("template_edit_variants") or [])
    new_variant = _extract_variant(message.text)

    if not template_id:
        await message.answer("Шаблон не найден. Откройте его заново.")
        await state.clear()
        return
    if new_variant is None:
        await message.answer("Не удалось распознать вариант. Отправьте его отдельным сообщением.")
        return
    if any(existing.lower() == new_variant.lower() for existing in current_variants):
        await message.answer("Этот вариант уже был. Отправьте действительно другой текст.")
        return

    current_variants.append(new_variant)
    if len(current_variants) < 3:
        next_variant = len(current_variants) + 1
        await state.update_data(
            template_edit_variants=current_variants,
            template_edit_variant_index=next_variant,
        )
        await message.answer(
            _build_comment_prompt_text(current_variants, next_variant),
            reply_markup=InlineKeyboardMarkup(
                inline_keyboard=[[InlineKeyboardButton(text="❌ Отмена", callback_data=f"template:edit:{template_id}")]]
            ),
            parse_mode="Markdown",
        )
        return

    try:
        user_backend_id = await get_user_backend_id(message.from_user.id)
        if not user_backend_id:
            await message.answer("Пользователь не найден.")
            await state.clear()
            return

        await api_client.update_task_template(
            template_id,
            user_backend_id,
            main_comment=current_variants[0],
            text_variants=current_variants,
            settings=_template_settings(template),
        )
        await state.clear()
        await message.answer(
            screen(
                "Комментарии обновлены",
                "Шаблон сохранён с тремя отдельными вариантами комментария.",
            ),
            reply_markup=InlineKeyboardMarkup(
                inline_keyboard=[[InlineKeyboardButton(text="◀️ К шаблону", callback_data=f"template:detail:{template_id}")]]
            ),
            parse_mode="Markdown",
        )
    except Exception as e:
        logger.error(f"Error updating template comment: {e}", exc_info=True)
        await message.answer(f"Не удалось обновить комментарии: {str(e)[:200]}")


async def _start_bulk_reply_edit(callback: CallbackQuery, state: FSMContext) -> None:
    template_id = int(callback.data.split(":")[-1])
    template = await _get_or_load_editing_template(callback.from_user.id, state, template_id)
    if not template:
        await callback.answer("Шаблон не найден", show_alert=True)
        return
    current_replies = _template_reply_list(template)

    await state.update_data(editing_template_id=template_id, editing_template_replies=[])
    await state.set_state(OrderStates.EDIT_TEMPLATE_REPLIES_ALL)
    await callback.message.edit_text(
        _build_replies_prompt_text(current_replies, []),
        reply_markup=InlineKeyboardMarkup(
            inline_keyboard=[
                [InlineKeyboardButton(text="🧹 Очистить ответы", callback_data=f"template:finish_replies:{template_id}")],
                [InlineKeyboardButton(text="❌ Отмена", callback_data=f"template:edit:{template_id}")]
            ]
        ),
        parse_mode="Markdown",
    )


@router.callback_query(F.data.startswith("template:edit_replies:"))
async def edit_template_replies_start(callback: CallbackQuery, state: FSMContext):
    try:
        if not callback.message:
            await callback.answer("Сообщение не найдено", show_alert=True)
            return
        await _start_bulk_reply_edit(callback, state)
        await callback.answer()
    except Exception as e:
        logger.error(f"Error starting template replies edit: {e}", exc_info=True)
        await callback.answer("Не удалось изменить ответы", show_alert=True)


@router.callback_query(F.data.startswith("template:edit_reply1:"))
@router.callback_query(F.data.startswith("template:edit_reply2:"))
@router.callback_query(F.data.startswith("template:edit_reply3:"))
async def redirect_legacy_reply_edit(callback: CallbackQuery, state: FSMContext):
    try:
        if not callback.message:
            await callback.answer("Сообщение не найдено", show_alert=True)
            return
        template_id = int(callback.data.split(":")[-1])
        callback.data = f"template:edit_replies:{template_id}"
        await _start_bulk_reply_edit(callback, state)
        await callback.answer()
    except Exception as e:
        logger.error(f"Error redirecting legacy reply edit: {e}", exc_info=True)
        await callback.answer("Не удалось открыть ответы", show_alert=True)


@router.message(StateFilter(OrderStates.EDIT_TEMPLATE_REPLIES_ALL))
@router.message(StateFilter(OrderStates.EDIT_TEMPLATE_REPLY1))
@router.message(StateFilter(OrderStates.EDIT_TEMPLATE_REPLY2))
@router.message(StateFilter(OrderStates.EDIT_TEMPLATE_REPLY3))
async def edit_template_replies_handler(message: Message, state: FSMContext):
    data = await state.get_data()
    template_id = data.get("editing_template_id")
    template = data.get("editing_template") or {}
    raw_text = normalize_user_text(message.text or "") or ""

    if not template_id:
        await message.answer("Шаблон не найден. Откройте его заново.")
        await state.clear()
        return

    collected = normalize_text_variants_list(data.get("editing_template_replies") or [])
    if raw_text.strip().lower() in {"/skip", "skip", "0", "нет", "очистить"}:
        await _finish_template_replies_edit(message, state, collected)
        return
    if len(preserve_nonempty_lines(raw_text)) > 1:
        await message.answer(
            "Отправьте только один ответ одним сообщением. Следующий ответ я попрошу отдельно.",
            reply_markup=InlineKeyboardMarkup(
                inline_keyboard=[
                    [InlineKeyboardButton(text="✅ Готово", callback_data=f"template:finish_replies:{template_id}")],
                    [InlineKeyboardButton(text="❌ Отмена", callback_data=f"template:edit:{template_id}")],
                ]
            ),
        )
        return
    reply_text = _extract_variant(raw_text)
    if reply_text is None:
        await message.answer(
            "Не удалось распознать ответ. Отправьте один ответ отдельным сообщением или нажмите «Готово».",
            reply_markup=InlineKeyboardMarkup(
                inline_keyboard=[
                    [InlineKeyboardButton(text="✅ Готово", callback_data=f"template:finish_replies:{template_id}")],
                    [InlineKeyboardButton(text="❌ Отмена", callback_data=f"template:edit:{template_id}")],
                ]
            ),
        )
        return
    if len(collected) >= settings.MAX_REPLIES:
        await _finish_template_replies_edit(message, state, collected)
        return
    collected.append(reply_text)
    await state.update_data(editing_template_replies=collected)
    if len(collected) >= settings.MAX_REPLIES:
        await _finish_template_replies_edit(message, state, collected)
        return
    current_replies = _template_reply_list(template)
    await message.answer(
        _build_replies_prompt_text(current_replies, collected),
        reply_markup=InlineKeyboardMarkup(
            inline_keyboard=[
                [InlineKeyboardButton(text="✅ Готово", callback_data=f"template:finish_replies:{template_id}")],
                [InlineKeyboardButton(text="❌ Отмена", callback_data=f"template:edit:{template_id}")],
            ]
        ),
        parse_mode="Markdown",
    )


@router.callback_query(F.data.startswith("template:finish_replies:"), StateFilter(OrderStates.EDIT_TEMPLATE_REPLIES_ALL))
async def edit_template_replies_finish(callback: CallbackQuery, state: FSMContext):
    try:
        if not callback.message:
            await callback.answer("Сообщение не найдено", show_alert=True)
            return
        data = await state.get_data()
        collected = normalize_text_variants_list(data.get("editing_template_replies") or [])
        await callback.answer()
        await _finish_template_replies_edit(callback.message, state, collected, telegram_user_id=callback.from_user.id)
    except Exception as e:
        logger.error(f"Error finishing template replies edit: {e}", exc_info=True)
        await callback.answer("Не удалось сохранить ответы", show_alert=True)


async def _finish_template_replies_edit(message: Message, state: FSMContext, replies: list[str], telegram_user_id: int | None = None) -> None:
    data = await state.get_data()
    template_id = data.get("editing_template_id")
    template = data.get("editing_template") or {}
    if not template_id:
        await message.answer("Шаблон не найден. Откройте его заново.")
        await state.clear()
        return
    try:
        user_backend_id = await get_user_backend_id(telegram_user_id or message.from_user.id)
        if not user_backend_id:
            await message.answer("Пользователь не найден.")
            await state.clear()
            return

        await api_client.update_task_template(
            template_id,
            user_backend_id,
            replies=replies,
            settings=_template_settings(template),
        )
        await state.clear()
        summary_line = "Ответы очищены." if not replies else f"Сохранено ответов: *{len(replies)}*"
        await message.answer(
            screen("Ответы обновлены", summary_line),
            reply_markup=InlineKeyboardMarkup(
                inline_keyboard=[[InlineKeyboardButton(text="◀️ К шаблону", callback_data=f"template:detail:{template_id}")]]
            ),
            parse_mode="Markdown",
        )
    except Exception as e:
        logger.error(f"Error updating template replies: {e}", exc_info=True)
        await message.answer(f"Не удалось обновить ответы: {str(e)[:200]}")


@router.callback_query(F.data.startswith("template:edit_likes:"))
async def edit_template_likes_start(callback: CallbackQuery, state: FSMContext):
    try:
        template_id = int(callback.data.split(":")[-1])
        await callback.answer(
            "Лайки задаются в общих настройках и больше не редактируются в шаблоне.",
            show_alert=True,
        )
        if callback.message:
            await callback.message.edit_text(
                _build_likes_prompt_text(0),
                reply_markup=InlineKeyboardMarkup(
                    inline_keyboard=[[InlineKeyboardButton(text="◀️ К шаблону", callback_data=f"template:edit:{template_id}")]]
                ),
                parse_mode="Markdown",
            )
    except Exception as e:
        logger.error(f"Error starting template likes edit: {e}", exc_info=True)
        await callback.answer("Не удалось изменить лайки", show_alert=True)


@router.message(StateFilter(OrderStates.EDIT_TEMPLATE_LIKES))
async def edit_template_likes_handler(message: Message, state: FSMContext):
    data = await state.get_data()
    template_id = data.get("editing_template_id")
    await state.clear()
    keyboard = None
    if template_id:
        keyboard = InlineKeyboardMarkup(
            inline_keyboard=[[InlineKeyboardButton(text="◀️ К шаблону", callback_data=f"template:detail:{template_id}")]]
        )
    await message.answer(
        screen(
            "Лайки не изменены",
            "Лайки задаются в общих настройках сервиса и не хранятся в шаблоне.",
        ),
        reply_markup=keyboard,
        parse_mode="Markdown",
    )
