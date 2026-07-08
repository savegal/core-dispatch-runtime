"""
Пошаговый мастер создания заказа - УПРОЩЁННАЯ ВЕРСИЯ.
Режим (A/B/C) определяется автоматически "под капотом".
"""
from aiogram import Router, F
from aiogram.types import Message, CallbackQuery, InlineKeyboardMarkup, InlineKeyboardButton
from aiogram.fsm.context import FSMContext
from aiogram.filters import StateFilter
from bot.services.validators import parse_links
from bot.services.api_client import api_client
from bot.services.drafts import get_draft, set_links, set_comment, reset_draft, save_draft
from bot.services.subscription_service import check_user_access
from bot.services.user_utils import get_user_backend_id
from bot.services.error_handlers import get_task_creation_error_message
from bot.services.template_utils import (
    filter_full_templates,
    is_replies_only_template,
    normalize_text_variants_list,
    normalize_user_text,
    preserve_nonempty_lines,
    replies_dict_to_list,
    template_replies,
    template_text_variants,
)
from bot.services.formatting import Emoji
from bot.services.likes_ui import get_likes_display_label_for_replies
from bot.state.order_states import OrderStates
from bot.config import load_config
from config.settings import get_settings
from bot.keyboards.common import get_access_denied_keyboard
from bot.keyboards.templates import get_template_choice_keyboard
from bot.services.order_creation_ui import (
    build_cancel_keyboard,
    build_links_prompt_text,
    build_preview_text,
    build_success_text,
    build_template_choice_text,
)
from datetime import datetime, timezone, timedelta
from typing import Optional
import logging

logger = logging.getLogger("bot.order_wizard")
router = Router()
config = load_config()
settings = get_settings()


async def _likes_display_label_for_replies(replies_or_count: object) -> str:
    return await get_likes_display_label_for_replies(replies_or_count)


def _wizard_template_choice_keyboard(templates: list[dict], page: int = 0) -> tuple[InlineKeyboardMarkup, int, int]:
    keyboard, current_page, total_pages = get_template_choice_keyboard(templates, page=page)
    for row in keyboard.inline_keyboard:
        for button in row:
            if not button.callback_data:
                continue
            if button.callback_data.startswith("template:select:"):
                button.callback_data = button.callback_data.replace("template:select:", "wizard:template:select:")
            elif button.callback_data.startswith("template:page:"):
                button.callback_data = button.callback_data.replace("template:page:", "wizard:template:page:")
            elif button.callback_data == "template:create_new":
                button.callback_data = "wizard:no_template"
            elif button.callback_data == "template:cancel":
                button.callback_data = "menu:main"
    return keyboard, current_page, total_pages


def _variant_text_from_message(message: Message) -> str:
    return (normalize_user_text(message.text or "") or "").strip()


def _looks_like_bulk_variants(raw_text: str) -> bool:
    return len([line for line in (raw_text or "").splitlines() if line.strip()]) >= 3


def _main_variant_prompt(links_count: int | None = None) -> str:
    prefix = "*Вариант комментария 1 из 3*\n\n"
    if links_count is not None:
        prefix += f"Видео: *{links_count}*\n\n"
    return (
        prefix
        + "Отправьте первый текст отдельным сообщением.\n"
        + "После него я попрошу второй и третий вариант."
    )


def _reply_state_for_index(index: int):
    if index >= 1:
        return OrderStates.INPUT_REPLIES_ALL
    return {
        1: OrderStates.INPUT_REPLY1,
        2: OrderStates.INPUT_REPLY2,
        3: OrderStates.INPUT_REPLY3,
        4: OrderStates.NEW_TEMPLATE_REPLY_4,
        5: OrderStates.NEW_TEMPLATE_REPLY_5,
        6: OrderStates.NEW_TEMPLATE_REPLY_6,
    }.get(index, OrderStates.NEW_TEMPLATE_REPLY_6)


def _personal_pool_usable_count(summary: dict | None) -> int:
    if not isinstance(summary, dict):
        return 0
    if "available_main_now" in summary:
        return max(0, int(summary.get("available_main_now") or 0))
    return max(0, int(summary.get("active") or 0) + int(summary.get("error") or 0))


# ========== ШАГ 0: ВЫБОР ШАБЛОНА ==========

@router.callback_query(F.data.startswith("wizard:template:select:"), StateFilter(OrderStates.CHOOSE_TEMPLATE))
async def wizard_select_template(callback: CallbackQuery, state: FSMContext):
    """Обрабатывает выбор шаблона в wizard."""
    try:
        await callback.answer()
        template_id = int(callback.data.split(":")[-1])
        user_id = callback.from_user.id
        user_backend_id = await get_user_backend_id(user_id)
        
        if not user_backend_id:
            await callback.answer("Ошибка: пользователь не найден", show_alert=True)
            return
        
        # Получаем шаблон
        templates = await api_client.list_task_templates(user_backend_id)
        template = next((t for t in templates if t.get("id") == template_id), None)
        
        if not template:
            await callback.answer("Шаблон не найден", show_alert=True)
            return
        if is_replies_only_template(template):
            await callback.answer("Этот шаблон подходит только для заказов на ответы", show_alert=True)
            return
        
        # ИСПРАВЛЕНО: Ссылки уже введены, просто заполняем draft из шаблона
        draft = get_draft(user_id)
        # Проверяем что ссылки уже есть
        if not draft.links:
            await callback.answer("Сначала отправьте ссылки на видео", show_alert=True)
            return
        
        text_variants = template_text_variants(template)
        if not text_variants:
            await callback.answer("В шаблоне нет 3 вариантов комментария", show_alert=True)
            return

        # ИСПРАВЛЕНО: Сохраняем все изменения draft в Redis, чтобы links не потерялись
        draft.comment = text_variants[0]
        draft.text_variants = text_variants
        draft.likes = 0
        
        template_settings = template.get("settings", {})
        draft.delays = template_settings.get("delays", True)
        draft.shuffle = template_settings.get("shuffle", False)
        draft.rotate_accounts = template_settings.get("rotate_accounts", True)
        draft.reply_mode = template_settings.get("reply_mode", "A")
        
        # Сохраняем весь draft в Redis (важно для сохранения links)
        save_draft(user_id, draft)
        
        # Сохраняем replies и text_variants из шаблона в state.
        # Важно: template.settings.text_variants не должен теряться и превращаться в [main] * 3.
        template_reply_list = template_replies(template)
        try:
            configured_reply_count = int(template_settings.get("reply_count") or len(template_reply_list) or 0)
        except (TypeError, ValueError):
            configured_reply_count = len(template_reply_list)
        reply_count = max(0, min(configured_reply_count, settings.MAX_REPLIES, len(template_reply_list)))
        await state.update_data(
            replies={idx: value for idx, value in enumerate(template_reply_list[:reply_count], start=1)},
            reply_count=reply_count,
            text_variants=text_variants,
            main_comment=text_variants[0],
            template_id=template_id,
            using_template=True,
            template_data=template
        )
        
        # ИСПРАВЛЕНО: Переходим сразу к превью/подтверждению заказа
        # Так как ссылки уже есть, шаблон выбран, комментарий из шаблона - можно подтверждать
        await show_template_preview_and_confirm(callback, state, template_id, reply_count)
        
    except Exception as e:
        logger.error(f"Error selecting template: {e}", exc_info=True)
        await callback.answer("Ошибка применения шаблона", show_alert=True)


@router.callback_query(F.data == "wizard:no_template", StateFilter(OrderStates.CHOOSE_TEMPLATE))
async def wizard_no_template(callback: CallbackQuery, state: FSMContext):
    """Пользователь выбрал создать без шаблона - переходим к вводу комментария."""
    await callback.answer()
    # ИСПРАВЛЕНО: Ссылки уже введены, сразу к комментарию
    draft = get_draft(callback.from_user.id)
    if not draft.links:
        await callback.answer("Сначала отправьте ссылки на видео", show_alert=True)
        return
    
    if not callback.message:
        await callback.answer("Ошибка: сообщение не найдено", show_alert=True)
        return
    
    # ИСПРАВЛЕНО: Убрана недостижимая строка кода после return
    await callback.message.edit_text(
        _main_variant_prompt(len(draft.links)),
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text=f"{Emoji.CANCEL} Отмена", callback_data="menu:main")]
        ]),
        parse_mode="Markdown"
    )
    await state.update_data(using_template=False)
    await state.set_state(OrderStates.INPUT_MAIN_COMMENT)


@router.callback_query(F.data.startswith("wizard:template:page:"), StateFilter(OrderStates.CHOOSE_TEMPLATE))
async def wizard_template_page(callback: CallbackQuery, state: FSMContext):
    """Обрабатывает пагинацию шаблонов в wizard."""
    try:
        page = int(callback.data.split(":")[-1])
        state_data = await state.get_data()
        templates = state_data.get("templates", [])
        links_count = len(state_data.get("saved_links") or get_draft(callback.from_user.id).links or [])
        keyboard, current_page, total_pages = _wizard_template_choice_keyboard(templates, page=page)
        
        await state.update_data(template_page=current_page)
        
        await callback.message.edit_text(
            build_template_choice_text(links_count, page=current_page, total_pages=total_pages),
            reply_markup=keyboard,
            parse_mode="Markdown"
        )
        await callback.answer()
    except Exception as e:
        logger.error(f"Error changing template page: {e}")
        await callback.answer("Ошибка переключения страницы", show_alert=True)


@router.callback_query(F.data == "wizard:change_template")
async def wizard_change_template(callback: CallbackQuery, state: FSMContext):
    """Возвращает к выбору шаблона без потери уже введённых ссылок."""
    try:
        state_data = await state.get_data()
        templates = state_data.get("templates") or []
        if not templates:
            user_backend_id = await get_user_backend_id(callback.from_user.id)
            templates = filter_full_templates(await api_client.list_task_templates(user_backend_id)) if user_backend_id else []
            await state.update_data(templates=templates)
        links_count = len(state_data.get("saved_links") or get_draft(callback.from_user.id).links or [])
        keyboard, current_page, total_pages = _wizard_template_choice_keyboard(templates, page=0)
        await callback.message.edit_text(
            build_template_choice_text(links_count, page=current_page, total_pages=total_pages),
            reply_markup=keyboard,
            parse_mode="Markdown",
        )
        await state.set_state(OrderStates.CHOOSE_TEMPLATE)
        await callback.answer()
    except Exception as e:
        logger.error(f"Error returning to template choice: {e}", exc_info=True)
        await callback.answer("Ошибка возврата к шаблонам", show_alert=True)


# ========== ШАГ 1: ССЫЛКИ ==========

@router.callback_query(F.data == "menu:new_order")
async def start_new_order(callback: CallbackQuery, state: FSMContext):
    """Начинает создание нового заказа с выбора шаблона."""
    try:
        await callback.answer()
        
        # Проверяем доступ
        user_id = callback.from_user.id
        user_data = await api_client.sync_telegram(user_id, callback.from_user.username, use_cache=True)
        role = user_data.get("role", "user")
        has_access = await check_user_access(user_id, user_data.get("id"), role, use_cache=True)
        
        if not has_access:
            await callback.message.edit_text(
                "*Доступ закрыт*\n\n"
                f"Стоимость: {settings.SUBSCRIPTION_PRICE}\n"
                "Для активации напишите владельцу",
                reply_markup=get_access_denied_keyboard(),
                parse_mode="Markdown"
            )
            await callback.answer("Доступ запрещён", show_alert=True)
            return
        
        await state.clear()
        reset_draft(user_id)
        
        # ИСПРАВЛЕНО: Всегда начинаем с ввода ссылок (пользователь должен сначала отправить ссылки)
        await callback.message.edit_text(
            build_links_prompt_text(),
            reply_markup=build_cancel_keyboard(),
            parse_mode="Markdown"
        )
        await state.set_state(OrderStates.INPUT_LINKS)
    except Exception as e:
        logger.error(f"Error in start_new_order: {e}", exc_info=True)
        try:
            await callback.answer("Ошибка при запуске создания заказа", show_alert=True)
        except:
            pass


@router.message(StateFilter(OrderStates.INPUT_LINKS), ~F.document)
async def wizard_process_text_links(message: Message, state: FSMContext):
    """Обрабатывает текстовые ссылки."""
    links = parse_links(message.text or "")
    
    if not links:
        await message.answer(
            "❌ *Ссылки не найдены*\n\n"
            "Не удалось найти ссылки на видео TikTok в вашем сообщении.\n\n"
            "*Проверьте формат:*\n"
            "• `https://www.tiktok.com/@user/video/123`\n"
            "• `vm.tiktok.com/xxxxx`\n\n"
            "Отправьте ссылки по одной на строку.",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text=f"{Emoji.CANCEL} Отмена", callback_data="menu:main")]
            ]),
            parse_mode="Markdown"
        )
        return
    
    user_id = message.from_user.id
    set_links(user_id, links)
    logger.info(f"[wizard_process_text_links] User {user_id}, saved {len(links)} links: {links}")
    
    # ИСПРАВЛЕНО: Сохраняем ссылки также в state для восстановления
    await state.update_data(saved_links=links)
    
    # ИСПРАВЛЕНО: После ввода ссылок всегда показываем выбор шаблона
    user_backend_id = await get_user_backend_id(user_id)
    try:
        templates = filter_full_templates(await api_client.list_task_templates(user_backend_id))
    except Exception as e:
        logger.error(f"Error fetching templates: {e}")
        templates = []
    
    # Сохраняем templates в state для последующего использования
    await state.update_data(templates=templates, template_page=0, links_saved=True)
    
    if not templates:
        # Нет шаблонов - переходим к созданию нового (ввод комментария)
        await message.answer(
            _main_variant_prompt(len(links)),
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text=f"{Emoji.CANCEL} Отмена", callback_data="menu:main")]
            ]),
            parse_mode="Markdown"
        )
        await state.update_data(using_template=False)
        await state.set_state(OrderStates.INPUT_MAIN_COMMENT)
    else:
        # Показываем выбор шаблона
        keyboard, current_page, total_pages = _wizard_template_choice_keyboard(templates, page=0)
        
        await message.answer(
            build_template_choice_text(len(links), page=current_page, total_pages=total_pages),
            reply_markup=keyboard,
            parse_mode="Markdown"
        )
        await state.set_state(OrderStates.CHOOSE_TEMPLATE)


# ========== ШАГ 2: ОСНОВНОЙ КОММЕНТАРИЙ ==========

@router.message(StateFilter(OrderStates.INPUT_MAIN_COMMENT))
async def wizard_process_main_comment(message: Message, state: FSMContext):
    """Сохраняет первый вариант комментария. Каждый вариант приходит отдельным сообщением."""
    if _looks_like_bulk_variants(message.text or ""):
        await message.answer(
            "*Создание заказа*\n\n"
            "❌ Теперь варианты принимаются отдельными сообщениями.\n\n"
            "Отправьте только первый вариант комментария. После него я попрошу второй и третий.",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text=f"{Emoji.CANCEL} Отмена", callback_data="menu:main")]
            ]),
            parse_mode="Markdown",
        )
        return

    await _process_main_comment_variant(message, state, 1)


@router.message(StateFilter(OrderStates.INPUT_MAIN_COMMENT_VARIANT_2))
async def wizard_process_main_comment_variant2(message: Message, state: FSMContext):
    """Сохраняет второй вариант комментария."""
    await _process_main_comment_variant(message, state, 2)


@router.message(StateFilter(OrderStates.INPUT_MAIN_COMMENT_VARIANT_3))
async def wizard_process_main_comment_variant3(message: Message, state: FSMContext):
    """Сохраняет третий вариант комментария и переводит к выбору ответов."""
    await _process_main_comment_variant(message, state, 3)


async def _process_main_comment_variant(message: Message, state: FSMContext, variant_index: int) -> None:
    user_id = message.from_user.id
    text = _variant_text_from_message(message)

    if not text:
        await message.answer(
            f"*Создание заказа*\n\n❌ Вариант {variant_index} не может быть пустым.\n\n"
            f"Отправьте вариант {variant_index} отдельным сообщением.",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text=f"{Emoji.CANCEL} Отмена", callback_data="menu:main")]
            ]),
            parse_mode="Markdown",
        )
        return

    if len(text) < 2:
        await message.answer(
            f"*Создание заказа*\n\n⚠️ Вариант {variant_index} слишком короткий.\n\n"
            "Комментарий должен содержать хотя бы 2 символа.",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text=f"{Emoji.CANCEL} Отмена", callback_data="menu:main")]
            ]),
            parse_mode="Markdown",
        )
        return

    state_data = await state.get_data()
    variants = normalize_text_variants_list(state_data.get("text_variants") or [])
    if any(existing.strip().casefold() == text.casefold() for existing in variants):
        await message.answer(
            f"*Создание заказа*\n\n⚠️ Вариант {variant_index} повторяется.\n\n"
            "Отправьте другую формулировку отдельным сообщением.",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text=f"{Emoji.CANCEL} Отмена", callback_data="menu:main")]
            ]),
            parse_mode="Markdown",
        )
        return

    variants.append(text)
    if variant_index == 1:
        set_comment(user_id, text)

    await state.update_data(main_comment=variants[0], text_variants=variants)

    if len(variants) < 3:
        next_index = len(variants) + 1
        next_state = (
            OrderStates.INPUT_MAIN_COMMENT_VARIANT_2
            if next_index == 2
            else OrderStates.INPUT_MAIN_COMMENT_VARIANT_3
        )
        await message.answer(
            f"{Emoji.SUCCESS} Вариант {len(variants)} сохранён.\n\n"
            f"*Вариант комментария {next_index} из 3*\n\n"
            "Отправьте следующий вариант отдельным сообщением.",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text=f"{Emoji.CANCEL} Отмена", callback_data="menu:main")]
            ]),
            parse_mode="Markdown",
        )
        await state.set_state(next_state)
        return

    await message.answer(
        f"{Emoji.SUCCESS} Сохранено 3 варианта комментария.",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text=f"{Emoji.CANCEL} Отмена", callback_data="menu:main")]
        ]),
        parse_mode="Markdown",
    )
    await show_reply_count_selection(message, state)


async def show_reply_count_selection(message_or_callback: Message | CallbackQuery, state: FSMContext):
    """Показывает выбор количества ответов - КОМПАКТНО."""
    max_replies = settings.MAX_REPLIES

    buttons = [
        InlineKeyboardButton(text=str(i), callback_data=f"wizard:reply_count:{i}")
        for i in range(0, max_replies + 1)
    ]
    keyboard_rows = [buttons[i:i + 5] for i in range(0, len(buttons), 5)]
    keyboard_rows.append([InlineKeyboardButton(text="❌ Отмена", callback_data="menu:main")])
    
    text = f"*Создание заказа* (шаг 4/6)\n\n💬 Сколько ответов добавить?\n\n*Максимум:* {max_replies}"
    
    if isinstance(message_or_callback, Message):
        await message_or_callback.answer(
            text,
            reply_markup=InlineKeyboardMarkup(inline_keyboard=keyboard_rows),
            parse_mode="Markdown"
        )
    else:
        await message_or_callback.message.edit_text(
            text,
            reply_markup=InlineKeyboardMarkup(inline_keyboard=keyboard_rows),
            parse_mode="Markdown"
        )
    
    await state.set_state(OrderStates.NEW_TEMPLATE_REPLY_COUNT)


# ========== ШАГ 2: ОТВЕТЫ (УПРОЩЕНО) ==========

@router.callback_query(F.data.startswith("wizard:reply_count:"), StateFilter(OrderStates.NEW_TEMPLATE_REPLY_COUNT))
async def wizard_select_reply_count(callback: CallbackQuery, state: FSMContext):
    """Выбирает количество ответов и включает per-message ввод."""
    try:
        await callback.answer()
        reply_count = int(callback.data.split(":")[-1])
    except Exception:
        await callback.answer("Ошибка выбора количества ответов", show_alert=True)
        return

    reply_count = max(0, min(reply_count, settings.MAX_REPLIES))
    await state.update_data(replies={}, reply_count=reply_count)

    if reply_count <= 0:
        await wizard_show_preview(callback, state)
        return

    await callback.message.edit_text(
        "*Создание заказа* (шаг 4/6)\n\n"
        f"💬 *Ответ 1 из {reply_count}*\n\n"
        "Отправьте первый ответ отдельным сообщением.",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text=f"{Emoji.CANCEL} Отмена", callback_data="menu:main")]
        ]),
        parse_mode="Markdown",
    )
    await state.set_state(_reply_state_for_index(1))

@router.callback_query(F.data == "wizard:skip_replies")
async def wizard_skip_replies(callback: CallbackQuery, state: FSMContext):
    """Пропускает ввод ответов и переходит к превью."""
    try:
        await callback.answer()
        await state.update_data(replies={}, reply_count=0)
        await wizard_show_preview(callback, state)
    except Exception as e:
        logger.error(f"Error skipping replies: {e}", exc_info=True)
        await callback.answer("Ошибка", show_alert=True)


async def wizard_show_preview_from_message(message: Message, state: FSMContext):
    """Показывает превью заказа из сообщения."""
    try:
        # ИСПРАВЛЕНО: Используем единый источник комментария - draft.comment
        state_data = await state.get_data()
        draft = get_draft(message.from_user.id)
        # Синхронизируем комментарий из FSM в draft (если есть в FSM, но нет в draft)
        main_comment_fsm = state_data.get("main_comment", "")
        if main_comment_fsm and not draft.comment:
            set_comment(message.from_user.id, main_comment_fsm)
            draft = get_draft(message.from_user.id)  # Обновляем draft
        
        # ИСПРАВЛЕНО: Используем draft.comment как единый источник правды
        main_comment = draft.comment or main_comment_fsm or ""
        replies = state_data.get("replies", {})
        reply_count = state_data.get("reply_count", 0)
        
        text_variants = normalize_text_variants_list(state_data.get("text_variants") or [])
        if not text_variants and main_comment:
            text_variants = [main_comment]
        preview_text = build_preview_text(
            links=draft.links or [],
            text_variants=text_variants,
            replies=replies,
            likes_label=await _likes_display_label_for_replies(replies),
        )
        
        await message.answer(
            preview_text,
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                [
                    InlineKeyboardButton(text="🚀 Запустить", callback_data="wizard:confirm"),
                    InlineKeyboardButton(text="✏️ Изменить", callback_data="wizard:edit"),
                ],
                [InlineKeyboardButton(text=f"{Emoji.CANCEL} Отмена", callback_data="menu:main")],
            ]),
            parse_mode="Markdown"
        )
        await state.set_state(OrderStates.CONFIRM_ORDER)
        
    except Exception as e:
        logger.error(f"Error showing preview: {e}", exc_info=True)
        await message.answer("Ошибка. Попробуйте снова.")


async def wizard_show_preview(callback: CallbackQuery, state: FSMContext):
    """Показывает превью заказа из callback."""
    try:
        if not callback.message:
            await callback.answer("Ошибка: сообщение не найдено", show_alert=True)
            return
        
        # ИСПРАВЛЕНО: Используем единый источник комментария - draft.comment
        state_data = await state.get_data()
        draft = get_draft(callback.from_user.id)
        # Синхронизируем комментарий из FSM в draft (если есть в FSM, но нет в draft)
        main_comment_fsm = state_data.get("main_comment", "")
        if main_comment_fsm and not draft.comment:
            set_comment(callback.from_user.id, main_comment_fsm)
            draft = get_draft(callback.from_user.id)  # Обновляем draft
        
        # ИСПРАВЛЕНО: Используем draft.comment как единый источник правды
        main_comment = draft.comment or main_comment_fsm or ""
        replies = state_data.get("replies", {})
        reply_count = state_data.get("reply_count", 0)
        
        text_variants = normalize_text_variants_list(state_data.get("text_variants") or [])
        if not text_variants and main_comment:
            text_variants = [main_comment]
        preview_text = build_preview_text(
            links=draft.links or [],
            text_variants=text_variants,
            replies=replies,
            likes_label=await _likes_display_label_for_replies(replies),
        )
        
        await callback.message.edit_text(
            preview_text,
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                [
                    InlineKeyboardButton(text="🚀 Запустить", callback_data="wizard:confirm"),
                    InlineKeyboardButton(text="✏️ Изменить", callback_data="wizard:edit")
                ],
                [InlineKeyboardButton(text=f"{Emoji.CANCEL} Отмена", callback_data="menu:main")]
            ]),
            parse_mode="Markdown"
        )
        await state.set_state(OrderStates.CONFIRM_ORDER)
        
    except Exception as e:
        logger.error(f"Error showing preview: {e}", exc_info=True)
        await callback.answer("Ошибка", show_alert=True)


# ========== ШАГ 4: ВВОД ОТВЕТОВ ==========

async def process_reply_input(message: Message, state: FSMContext, reply_index: int, step_num: int = None):
    """Универсальный обработчик ввода ответа."""
    raw_text = message.text or ""
    reply_text = normalize_user_text(raw_text) or ""
    state_data = await state.get_data()
    reply_count = max(0, min(int(state_data.get("reply_count", 1) or 1), settings.MAX_REPLIES))
    current_step = step_num or (4 + reply_count)
    # ИСПРАВЛЕНО: Валидация длины ответа
    
    if not reply_text:
        await message.answer(
            f"*Создание заказа* (шаг {current_step}/6)\n\n"
            f"❌ Ответ не может быть пустым.\n\n"
            f"Отправьте ответ {reply_index}:",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text=f"{Emoji.CANCEL} Отмена", callback_data="menu:main")]
            ]),
            parse_mode="Markdown"
        )
        return

    if len(preserve_nonempty_lines(raw_text)) > 1:
        await message.answer(
            f"*Создание заказа* (шаг {current_step}/6)\n\n"
            "❌ Отправьте только один ответ одним сообщением.\n\n"
            f"Отправьте ответ {reply_index}:",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text=f"{Emoji.CANCEL} Отмена", callback_data="menu:main")]
            ]),
            parse_mode="Markdown"
        )
        return

    if len(reply_text) > settings.MAX_REPLY_LENGTH:
        await message.answer(
            f"*Создание заказа* (шаг {current_step}/6)\n\n"
            f"❌ Ответ слишком длинный. Максимум: {settings.MAX_REPLY_LENGTH} символов.\n\n"
            f"Отправьте ответ {reply_index}:",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text=f"{Emoji.CANCEL} Отмена", callback_data="menu:main")]
            ]),
            parse_mode="Markdown"
        )
        return
    
    replies = state_data.get("replies", {})
    replies[reply_index] = reply_text
    
    await state.update_data(replies=replies)
    
    # Если есть ещё ответы для ввода
    if reply_index < reply_count:
        next_index = reply_index + 1
        current_step = step_num or (4 + reply_count)
        
        await message.answer(
            f"*Создание заказа* (шаг {current_step}/6)\n\n"
            f"{Emoji.SUCCESS} Ответ {reply_index} добавлен\n\n"
            f"Отправьте ответ {next_index}:",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text=f"{Emoji.CANCEL} Отмена", callback_data="menu:main")]
            ]),
            parse_mode="Markdown"
        )
        await state.set_state(OrderStates.INPUT_REPLIES_ALL)
    else:
        # Все ответы введены - переходим к сохранению шаблона/превью
        await ask_save_template_with_preview(message, state)


@router.message(StateFilter(OrderStates.INPUT_REPLIES_ALL))
async def wizard_process_reply_message(message: Message, state: FSMContext):
    """Обрабатывает ввод ответов по одному сообщению до выбранного лимита."""
    state_data = await state.get_data()
    replies = state_data.get("replies", {}) or {}
    reply_count = max(0, min(int(state_data.get("reply_count", 0) or 0), settings.MAX_REPLIES))
    next_index = len(replies) + 1
    if next_index > reply_count:
        await ask_save_template_with_preview(message, state)
        return
    step_num = 4 + reply_count
    await process_reply_input(message, state, next_index, step_num=step_num)


@router.message(StateFilter(OrderStates.INPUT_REPLY1))
async def wizard_process_reply1(message: Message, state: FSMContext):
    """Обрабатывает ввод первого ответа."""
    state_data = await state.get_data()
    reply_count = state_data.get("reply_count", 1)
    step_num = 4 + reply_count
    await process_reply_input(message, state, 1, step_num=step_num)


@router.message(StateFilter(OrderStates.INPUT_REPLY2))
async def wizard_process_reply2(message: Message, state: FSMContext):
    """Обрабатывает ввод второго ответа."""
    state_data = await state.get_data()
    reply_count = state_data.get("reply_count", 2)
    step_num = 4 + reply_count
    await process_reply_input(message, state, 2, step_num=step_num)


@router.message(StateFilter(OrderStates.INPUT_REPLY3))
async def wizard_process_reply3(message: Message, state: FSMContext):
    """Обрабатывает ввод третьего ответа."""
    state_data = await state.get_data()
    reply_count = state_data.get("reply_count", 3)
    step_num = 4 + reply_count
    await process_reply_input(message, state, 3, step_num=step_num)


@router.message(StateFilter(OrderStates.NEW_TEMPLATE_REPLY_4))
async def wizard_process_reply4(message: Message, state: FSMContext):
    """Обрабатывает ввод четвёртого ответа."""
    state_data = await state.get_data()
    reply_count = state_data.get("reply_count", 4)
    step_num = 4 + reply_count
    await process_reply_input(message, state, 4, step_num=step_num)


@router.message(StateFilter(OrderStates.NEW_TEMPLATE_REPLY_5))
async def wizard_process_reply5(message: Message, state: FSMContext):
    """Обрабатывает ввод пятого ответа."""
    state_data = await state.get_data()
    reply_count = state_data.get("reply_count", 5)
    step_num = 4 + reply_count
    await process_reply_input(message, state, 5, step_num=step_num)


@router.message(StateFilter(OrderStates.NEW_TEMPLATE_REPLY_6))
async def wizard_process_reply6(message: Message, state: FSMContext):
    """Обрабатывает ввод шестого ответа."""
    state_data = await state.get_data()
    reply_count = state_data.get("reply_count", 6)
    step_num = 4 + reply_count
    await process_reply_input(message, state, 6, step_num=step_num)


# ========== ШАГ 5: ПРЕВЬЮ И СОХРАНЕНИЕ ШАБЛОНА ==========

async def show_template_preview_and_confirm(callback: CallbackQuery, state: FSMContext, template_id: int, reply_count: int):
    """Показывает превью заказа с выбранным шаблоном и предлагает подтвердить создание."""
    user_id = callback.from_user.id
    draft = get_draft(user_id)
    state_data = await state.get_data()
    template = state_data.get("template_data", {})
    
    replies = state_data.get("replies", {})
    text_variants = normalize_text_variants_list(state_data.get("text_variants") or draft.text_variants or [])
    text = build_preview_text(
        links=draft.links or [],
        text_variants=text_variants,
        replies=replies,
        likes_label=await _likes_display_label_for_replies(replies),
        template_name=template.get("name") or "Без названия",
    )
    
    keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=f"{Emoji.SUCCESS} Создать заказ", callback_data="wizard:confirm_with_template")],
        [InlineKeyboardButton(text="📑 Другой шаблон", callback_data="wizard:change_template")],
        [InlineKeyboardButton(text=f"{Emoji.CANCEL} Отмена", callback_data="menu:main")],
    ])

    await callback.message.edit_text(text, reply_markup=keyboard, parse_mode="Markdown")
    await callback.answer()


@router.callback_query(F.data == "wizard:confirm_with_template")
async def wizard_confirm_with_template(callback: CallbackQuery, state: FSMContext):
    """Подтверждает создание заказа с выбранным шаблоном."""
    await callback.answer()
    user_id = callback.from_user.id
    draft = get_draft(user_id)
    state_data = await state.get_data()
    template = state_data.get("template_data", {})
    template_settings = template.get("settings", {})
    replies = state_data.get("replies", {})
    reply_count = state_data.get("reply_count", 3)
    # template_id берём из state (выставлен в wizard_select_template) либо из
    # самого template_data — обе точки гарантированно его держат.
    template_id = state_data.get("template_id") or template.get("id")
    
    # Проверяем наличие ссылок
    if not draft.links:
        await callback.message.edit_text(
            "❌ Ссылки не найдены",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="🏠 Меню", callback_data="menu:main")]
            ])
        )
        return
    
    try:
        user_backend_id = await get_user_backend_id(user_id)
        if not user_backend_id:
            await callback.message.edit_text(
                "❌ Пользователь не найден",
                reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                    [InlineKeyboardButton(text=f"{Emoji.HOME} Меню", callback_data="menu:main")]
                ])
            )
            return
        
        # Проверяем наличие активных аккаунтов
        has_accounts = await api_client.has_active_accounts()
        if not has_accounts:
            await callback.message.edit_text(
                "❌ Нет активных аккаунтов",
                reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                    [InlineKeyboardButton(text=f"{Emoji.HOME} Меню", callback_data="menu:main")]
                ])
            )
            return
        
        await callback.message.edit_text(f"{Emoji.PENDING} Создание заказа...")
        
        # Формируем settings
        task_settings = {
            "reply_mode": template_settings.get("reply_mode", "A"),
            "reply_count": reply_count,
            "delays": template_settings.get("delays", True),
            "shuffle": template_settings.get("shuffle", False),
            "rotate_accounts": template_settings.get("rotate_accounts", True),
            "replies": [replies.get(i) for i in range(1, reply_count + 1) if replies.get(i)],
            # Связь с шаблоном — нужна для:
            # - отображения в _render_order_settings,
            # - корректного «сделать копию заказа»,
            # - аналитики по шаблонам.
            "template_id": template_id,
            "template_name": template.get("name") or "Без названия",
            "origin": "wizard_template",
        }
        text_variants = normalize_text_variants_list(state_data.get("text_variants") or [])
        if len(text_variants) < 3:
            await callback.message.edit_text(
                "❌ В шаблоне нет минимум 3 вариантов комментария. Обновите шаблон и попробуйте снова.",
                reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                    [InlineKeyboardButton(text=f"{Emoji.HOME} Меню", callback_data="menu:main")]
                ])
            )
            return
        
        # Создаём заказ (проверка на дубликаты выполняется на сервере)
        task = await api_client.create_task(
            user_id=user_backend_id,
            main_comment=draft.comment,
            text_variants=text_variants,
            video_urls=draft.links,
            reply1=replies.get(1),
            reply2=replies.get(2),
            reply3=replies.get(3),
            replies=replies_dict_to_list(replies)[:reply_count],
            likes=0,
            settings=task_settings
        )
        
        task_id = task.get("task_id")
        reset_draft(user_id)
        await state.clear()
        
        await callback.message.edit_text(
            build_success_text(
                task_id=task_id,
                links_count=len(draft.links),
                reply_count=reply_count,
                variants_count=len(text_variants),
                likes_label=await _likes_display_label_for_replies(reply_count),
                template_name=template.get("name") or "Без названия",
            ),
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="👁 Заказ", callback_data=f"order:detail:{task_id}")],
                [InlineKeyboardButton(text="➕ Ещё", callback_data="menu:new_order")],
                [InlineKeyboardButton(text="🏠 Меню", callback_data="menu:main")]
            ]),
            parse_mode="Markdown"
        )
        
    except Exception as e:
        logger.error(f"Error creating task with template: {e}", exc_info=True)
        error_msg = str(e)
        if len(error_msg) > 150:
            error_msg = error_msg[:150] + "..."
        
        await callback.message.edit_text(
            f"❌ Ошибка\n\n{error_msg}",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text=f"{Emoji.REFRESH} Снова", callback_data="menu:new_order")],
                [InlineKeyboardButton(text="🏠 Меню", callback_data="menu:main")]
            ])
        )


async def ask_save_template_with_preview(message_or_callback: Message | CallbackQuery, state: FSMContext):
    """ИСПРАВЛЕНО: Показывает превью заказа и спрашивает про сохранение шаблона (объединены в один шаг)."""
    if isinstance(message_or_callback, Message):
        user_id = message_or_callback.from_user.id
    else:
        user_id = message_or_callback.from_user.id
    
    # ИСПРАВЛЕНО: Используем единый источник комментария - draft.comment
    draft = get_draft(user_id)
    state_data = await state.get_data()
    # Синхронизируем комментарий из FSM в draft (если есть в FSM, но нет в draft)
    main_comment_fsm = state_data.get("main_comment", "")
    if main_comment_fsm and not draft.comment:
        set_comment(user_id, main_comment_fsm)
        draft = get_draft(user_id)  # Обновляем draft
    
    # ИСПРАВЛЕНО: Используем draft.comment как единый источник правды
    main_comment = draft.comment or main_comment_fsm or "не указан"
    replies = state_data.get("replies", {})
    reply_count = state_data.get("reply_count", 0)
    
    text_variants = normalize_text_variants_list(state_data.get("text_variants") or [main_comment])
    text = build_preview_text(
        links=draft.links or [],
        text_variants=text_variants,
        replies=replies,
        likes_label=await _likes_display_label_for_replies(replies),
        include_template_prompt=True,
    )
    
    # Получаем рекомендации по гео (компактно)
    try:
        estimated_accounts = len(draft.links) * (1 + reply_count)
        recommendation = await api_client.get_geo_recommendation(
            required_accounts=estimated_accounts,
            account_type="main_comment"
        )
        
        if recommendation.get("can_execute"):
            best_countries = recommendation.get("best_countries", [])[:2]
            optimal_countries = [c for c in best_countries if c.get("status") == "optimal"]
            
            if optimal_countries:
                text += "\n*📊 Оптимальные регионы:*\n"
                for country in optimal_countries[:2]:
                    text += f"{Emoji.SUCCESS} {country['country_name']}: {country['available_accounts']} аккаунтов"
                    if country.get("available_proxies", 0) > 0:
                        text += f", {country['available_proxies']} прокси"
                    text += "\n"
            
            recommendations = recommendation.get("recommendations", [])[:1]
            if recommendations:
                text += f"\n*💡 Рекомендация:* {recommendations[0]}\n"
    except Exception as e:
        logger.debug(f"Failed to get geo recommendations: {e}")
    
    text += "\n*💾 Сохранить как шаблон?*"
    
    # ИСПРАВЛЕНО: Стандартизированные кнопки
    keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text="💾 Сохранить шаблон", callback_data="wizard:save_template"),
            InlineKeyboardButton(text="🚀 Создать заказ", callback_data="wizard:create_order")
        ],
        [InlineKeyboardButton(text="❌ Отмена", callback_data="menu:main")]
    ])
    
    if isinstance(message_or_callback, Message):
        await message_or_callback.answer(text, reply_markup=keyboard, parse_mode="Markdown")
    else:
        await message_or_callback.message.edit_text(text, reply_markup=keyboard, parse_mode="Markdown")
    
    await state.set_state(OrderStates.INPUT_TEMPLATE_NAME)


async def ask_save_template(message_or_callback: Message | CallbackQuery, state: FSMContext):
    """Обратная совместимость - вызывает ask_save_template_with_preview."""
    await ask_save_template_with_preview(message_or_callback, state)


@router.callback_query(F.data == "wizard:save_template")
async def wizard_ask_template_name(callback: CallbackQuery, state: FSMContext):
    """ИСПРАВЛЕНО: Запрашивает название шаблона."""
    await callback.answer()
    await callback.message.edit_text(
        '*Создание заказа* (шаг 6/6)\n\n📝 Введите название шаблона:\n\n*Можно пропустить*',
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="⏭ Пропустить сохранение", callback_data="wizard:create_order")]
        ]),
        parse_mode="Markdown"
    )
    await state.set_state(OrderStates.INPUT_TEMPLATE_NAME)


@router.message(StateFilter(OrderStates.INPUT_TEMPLATE_NAME))
async def wizard_save_template(message: Message, state: FSMContext):
    """Сохраняет шаблон и показывает превью заказа."""
    user_id = message.from_user.id
    template_name = message.text or ""
    
    if not template_name.strip():
        await message.answer(
            "*Создание заказа* (шаг 6/6)\n\n"
            "❌ Название не может быть пустым.\n\n"
            "Введите название шаблона или нажмите 'Пропустить сохранение':",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="⏭ Пропустить сохранение", callback_data="wizard:create_order")]
            ]),
            parse_mode="Markdown"
        )
        return
    
    try:
        user_backend_id = await get_user_backend_id(user_id)
        if not user_backend_id:
            await message.answer(
                "*Ошибка*\n\nПользователь не найден",
                reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                    [InlineKeyboardButton(text=f"{Emoji.HOME} Меню", callback_data="menu:main")]
                ]),
                parse_mode="Markdown"
            )
            await state.clear()
            return
        
        draft = get_draft(user_id)
        state_data = await state.get_data()
        
        replies = state_data.get("replies", {})
        reply_count = state_data.get("reply_count", 0)
        text_variants = normalize_text_variants_list(state_data.get("text_variants") or [])
        if len(text_variants) < 3:
            await message.answer("❌ Нужно минимум 3 варианта комментария. Создайте заказ заново.")
            await state.clear()
            return

        await api_client.create_task_template(
            user_id=user_backend_id,
            name=template_name,
            main_comment=draft.comment,
            replies=replies_dict_to_list(replies),
            likes=0,
            settings={
                "reply_count": reply_count,
                "replies": replies_dict_to_list(replies),
                "text_variants": text_variants
            }
        )
        
        await message.answer(f"{Emoji.SUCCESS} Шаблон '{template_name}' сохранён!")
        
    except Exception as e:
        logger.error(f"Error saving template: {e}")
        await message.answer("⚠️ Не удалось сохранить шаблон, но заказ будет создан.")
    
    # ИСПРАВЛЕНО: После сохранения шаблона сразу создаём заказ (без отдельного шага просмотра)
    await wizard_create_order_from_message(message, state)


@router.callback_query(F.data == "wizard:skip_save")
async def wizard_skip_save_template(callback: CallbackQuery, state: FSMContext):
    """ИСПРАВЛЕНО: Пропускает сохранение шаблона и сразу создаёт заказ."""
    await callback.answer()
    await wizard_create_order_from_callback(callback, state)


# ========== УСТАРЕВШАЯ ФУНКЦИЯ (удалена, превью интегрировано в ask_save_template_with_preview) ==========
# async def show_order_preview - больше не используется, заменена на ask_save_template_with_preview


@router.callback_query(F.data == "wizard:confirm")
async def wizard_confirm_order(callback: CallbackQuery, state: FSMContext):
    """УПРОЩЕНО: Подтверждает и создаёт заказ из превью."""
    await callback.answer()  # ИСПРАВЛЕНО: Добавлен ответ на callback
    await wizard_create_order_from_callback(callback, state)


@router.callback_query(F.data == "wizard:edit")
async def wizard_edit_order(callback: CallbackQuery, state: FSMContext):
    """УПРОЩЕНО: Возвращается к редактированию комментария."""
    await callback.answer()
    if not callback.message:
        await callback.answer("Ошибка: сообщение не найдено", show_alert=True)
        return
    
    draft = get_draft(callback.from_user.id)
    await callback.message.edit_text(
        _main_variant_prompt(len(draft.links) if draft.links else 0),
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text=f"{Emoji.CANCEL} Отмена", callback_data="menu:main")]
        ]),
        parse_mode="Markdown"
    )
    await state.set_state(OrderStates.INPUT_MAIN_COMMENT)


# УДАЛЕНО: wizard_create_order - заменено на wizard:confirm


async def wizard_create_order_from_callback(callback: CallbackQuery, state: FSMContext):
    """Создаёт заказ из callback."""
    await callback.answer()
    user_id = callback.from_user.id
    draft = get_draft(user_id)
    logger.info(f"[wizard_create_order_from_callback] User {user_id}, draft.links count: {len(draft.links) if draft.links else 0}, links: {draft.links}")
    # ИСПРАВЛЕНО: Сохраняем draft перед созданием заказа, чтобы убедиться что ссылки не потерялись
    save_draft(user_id, draft)
    await _wizard_create_order_impl(
        callback.message,
        state,
        user_id=user_id,
        is_message=False,
    )


async def wizard_create_order_from_message(message: Message, state: FSMContext):
    """Создаёт заказ из message."""
    await _wizard_create_order_impl(message, state)


async def _wizard_create_order_impl(
    message_or_edit_target,
    state: FSMContext,
    *,
    user_id: Optional[int] = None,
    is_message: Optional[bool] = None,
):
    """Общая логика создания заказа."""
    # Получаем user_id
    # Callback messages are authored by the bot, so message.from_user.id is
    # the bot ID, not the operator who pressed the button. Callback callers
    # pass the real actor explicitly; message callers keep this inference path.
    if user_id is None:
        if hasattr(message_or_edit_target, 'from_user'):
            user_id = message_or_edit_target.from_user.id
        else:
            user_id = message_or_edit_target.chat.id
    if is_message is None:
        is_message = hasattr(message_or_edit_target, 'from_user')

    # Шаг выбора аккаунт-пула. Если работник ещё не выбрал пул и у него есть
    # личные аккаунты — спрашиваем, какие аккаунты использовать. Иначе по
    # дефолту работает общий пул (account_pool_mode не выставляется в settings,
    # executor читает None как "shared").
    state_data_for_pool = await state.get_data()
    if "account_pool_mode" not in state_data_for_pool:
        try:
            user_backend_id_for_pool = await get_user_backend_id(user_id)
            if user_backend_id_for_pool:
                pool_summary = await api_client.get_personal_pool_summary(
                    user_backend_id_for_pool
                )
                personal_total = int((pool_summary or {}).get("total", 0) or 0)
                personal_usable = _personal_pool_usable_count(pool_summary)
                if personal_usable > 0:
                    if personal_total == personal_usable:
                        personal_line = f"👤 *Личные аккаунты* — ваш приватный пул: *{personal_usable}* шт."
                    else:
                        personal_line = (
                            f"👤 *Личные аккаунты* — доступно сейчас: *{personal_usable}* "
                            f"из *{personal_total}* шт."
                        )
                    pool_text = (
                        "*Какие аккаунты использовать для заказа?*\n\n"
                        "🌐 *Общий пул* — все ваши заказы идут на общих аккаунтах сервиса.\n"
                        f"{personal_line}\n\n"
                        "_Прокси используются общие в обоих режимах._"
                    )
                    pool_kb = InlineKeyboardMarkup(inline_keyboard=[
                        [InlineKeyboardButton(text="🌐 Общий пул", callback_data="wizard:pool:shared")],
                        [InlineKeyboardButton(text="👤 Мои аккаунты", callback_data="wizard:pool:personal")],
                        [InlineKeyboardButton(text="❌ Отмена", callback_data="menu:main")],
                    ])
                    if is_message:
                        await message_or_edit_target.answer(
                            pool_text, reply_markup=pool_kb, parse_mode="Markdown"
                        )
                    else:
                        await message_or_edit_target.edit_text(
                            pool_text, reply_markup=pool_kb, parse_mode="Markdown"
                        )
                    return
        except Exception as exc:
            # Fail-soft: при ошибке показа выбора — продолжаем как обычно
            # с общим пулом, заказ не должен зависеть от доступности этого опционального API.
            logger.warning(
                f"[_wizard_create_order_impl] pool summary unavailable, fallback to shared: {exc}"
            )

    draft = get_draft(user_id)
    
    # ИСПРАВЛЕНО: Добавлено логирование для диагностики
    logger.info(f"[_wizard_create_order_impl] User {user_id}, draft.links count: {len(draft.links) if draft.links else 0}, draft.comment: {bool(draft.comment)}, links: {draft.links}")
    
    # ИСПРАВЛЕНО: Если ссылки потерялись, пытаемся восстановить из state
    # Это не должно происходить в нормальном flow, но на случай багов - восстанавливаем
    if not draft.links:
        state_data = await state.get_data()
        saved_links = state_data.get("saved_links")
        if saved_links:
            logger.warning(f"[_wizard_create_order_impl] Links lost from draft, restoring from state: {saved_links}")
            set_links(user_id, saved_links)
            save_draft(user_id, get_draft(user_id))  # ИСПРАВЛЕНО: Сохраняем draft после восстановления
            draft = get_draft(user_id)
        else:
            # Пробуем восстановить из недавних ссылок (fallback)
            from bot.services.drafts import get_recent_links
            recent_links_text = get_recent_links(user_id)
            if recent_links_text:
                # Парсим последние ссылки
                recent_links = parse_links(recent_links_text[0] if recent_links_text else "")
                if recent_links:
                    logger.warning(f"[_wizard_create_order_impl] Links lost from draft, restoring from recent: {recent_links}")
                    set_links(user_id, recent_links)
                    save_draft(user_id, get_draft(user_id))  # ИСПРАВЛЕНО: Сохраняем draft после восстановления
                    draft = get_draft(user_id)
    
    # ИСПРАВЛЕНО: Улучшена валидация с более понятными сообщениями
    if not draft.links:
        error_text = (
            "*Ошибка*\n\n"
            f"{Emoji.ERROR_ICON} *Ссылки не найдены*\n\n"
            "Не удалось найти ссылки на видео TikTok.\n\n"
            "Пожалуйста, начните создание заказа заново."
        )
        keyboard = InlineKeyboardMarkup(inline_keyboard=[
            [
                InlineKeyboardButton(text="🆕 Заново", callback_data="menu:new_order"),
                InlineKeyboardButton(text=f"{Emoji.HOME} Меню", callback_data="menu:main")
            ]
        ])
        if is_message:
            await message_or_edit_target.answer(error_text, reply_markup=keyboard, parse_mode="Markdown")
        else:
            await message_or_edit_target.edit_text(error_text, reply_markup=keyboard, parse_mode="Markdown")
        return
    
    # ИСПРАВЛЕНО: Восстанавливаем комментарий из FSM, если он потерялся в draft
    # Симметрично логике восстановления ссылок
    if not draft.comment or not draft.comment.strip():
        state_data = await state.get_data()
        main_comment_fsm = state_data.get("main_comment", "")
        
        if main_comment_fsm and main_comment_fsm.strip():
            logger.warning(f"[_wizard_create_order_impl] Comment lost from draft, restoring from FSM: {main_comment_fsm[:50]}...")
            set_comment(user_id, main_comment_fsm)
            draft = get_draft(user_id)
        else:
            # Пробуем восстановить из последнего комментария (fallback)
            from bot.services.drafts import get_last_comment
            last_comment = get_last_comment(user_id)
            if last_comment and last_comment.strip():
                logger.warning(f"[_wizard_create_order_impl] Comment lost from draft, restoring from last comment: {last_comment[:50]}...")
                set_comment(user_id, last_comment)
                draft = get_draft(user_id)
    
    # ИСПРАВЛЕНО: Улучшена валидация комментария (после попытки восстановления)
    if not draft.comment or not draft.comment.strip():
        error_text = (
            "*Ошибка*\n\n"
            f"{Emoji.ERROR_ICON} *Комментарий не указан*\n\n"
            "Не удалось найти текст комментария.\n\n"
            "Пожалуйста, начните создание заказа заново."
        )
        keyboard = InlineKeyboardMarkup(inline_keyboard=[
            [
                InlineKeyboardButton(text="🆕 Заново", callback_data="menu:new_order"),
                InlineKeyboardButton(text=f"{Emoji.HOME} Меню", callback_data="menu:main")
            ]
        ])
        if is_message:
            await message_or_edit_target.answer(error_text, reply_markup=keyboard, parse_mode="Markdown")
        else:
            await message_or_edit_target.edit_text(error_text, reply_markup=keyboard, parse_mode="Markdown")
        return
    
    try:
        # Показываем индикацию загрузки
        loading_text = f"{Emoji.PENDING} Проверка ресурсов..."
        if is_message:
            loading_msg = await message_or_edit_target.answer(loading_text)
        else:
            await message_or_edit_target.edit_text(loading_text)
            loading_msg = message_or_edit_target
        
        # Проверяем наличие активных аккаунтов
        has_accounts = await api_client.has_active_accounts()
        if not has_accounts:
            error_text = (
                "*Ошибка*\n\n"
                "❌ Нет активных аккаунтов\n\n"
                "Обратитесь к администратору"
            )
            keyboard = InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="🏠 Меню", callback_data="menu:main")]
            ])
            await loading_msg.edit_text(error_text, reply_markup=keyboard, parse_mode="Markdown")
            return
        
        user_backend_id = await get_user_backend_id(user_id)
        if not user_backend_id:
            error_text = "*Ошибка*\n\nПользователь не найден"
            keyboard = InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="🏠 Меню", callback_data="menu:main")]
            ])
            await loading_msg.edit_text(error_text, reply_markup=keyboard, parse_mode="Markdown")
            return
        
        # Получаем reply из state
        state_data = await state.get_data()
        replies = state_data.get("replies", {})
        reply_count = state_data.get("reply_count", 0)
        
        # Определяем reply_mode автоматически
        if reply_count == 0:
            reply_mode = "C"  # Без ответов
        else:
            reply_mode = "A"  # С ответами
        
        # account_pool_mode прокидывается в task.settings и читается executor'ом:
        # - "shared"   -> Account.owner_user_id IS NULL (общий пул, дефолт)
        # - "personal" -> Account.owner_user_id == task.user_id (личные аккаунты)
        # См. services/account_service.py::get_free_account_for_main_comment
        # и executor/executor.py — оба места соблюдают этот контракт.
        account_pool_mode = (state_data.get("account_pool_mode") or "shared").lower()
        if account_pool_mode not in {"shared", "personal"}:
            account_pool_mode = "shared"

        settings_dict = {
            "delays": True,
            "shuffle": False,
            "rotate_accounts": True,
            "reply_mode": reply_mode,
            "reply_count": reply_count,
            "replies": replies_dict_to_list(replies),
            "origin": "wizard",  # REFACTOR: Указываем источник заказа
            "account_pool_mode": account_pool_mode,
        }
        
        await loading_msg.edit_text(f"{Emoji.PENDING} Создание заказа...")
        text_variants = normalize_text_variants_list(state_data.get("text_variants") or [])
        if len(text_variants) < 3:
            await loading_msg.edit_text(
                "❌ Нужно минимум 3 варианта комментария. Запустите создание заказа заново и отправьте каждый вариант отдельным сообщением.",
                reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                    [InlineKeyboardButton(text=f"{Emoji.REFRESH} Снова", callback_data="menu:new_order")],
                    [InlineKeyboardButton(text=f"{Emoji.HOME} Меню", callback_data="menu:main")]
                ])
            )
            return
        
        try:
            # Проверка на дубликаты выполняется на сервере
            result = await api_client.create_task(
                user_id=user_backend_id,
                main_comment=draft.comment,
                text_variants=text_variants,
                video_urls=draft.links,
                reply1=replies.get(1),
                reply2=replies.get(2),
                reply3=replies.get(3),
                replies=replies_dict_to_list(replies),
                likes=0,
                settings=settings_dict
            )
            
            task_id = result.get("task_id")
            
            pool_label = "👤 Мои аккаунты" if account_pool_mode == "personal" else "🌐 Общий пул"
            success_text = build_success_text(
                task_id=task_id,
                links_count=len(draft.links),
                reply_count=reply_count,
                variants_count=len(text_variants),
                likes_label=await _likes_display_label_for_replies(reply_count),
                pool_label=pool_label,
            )
            
            keyboard = InlineKeyboardMarkup(inline_keyboard=[
                [
                    InlineKeyboardButton(text="👁 Заказ", callback_data=f"order:detail:{task_id}"),
                    InlineKeyboardButton(text="➕ Ещё", callback_data="menu:new_order"),
                    InlineKeyboardButton(text="🏠 Меню", callback_data="menu:main")
                ]
            ])
            
            # Определяем, куда отправлять сообщение
            if is_message:
                await message_or_edit_target.answer(success_text, reply_markup=keyboard, parse_mode="Markdown")
            else:
                await loading_msg.edit_text(success_text, reply_markup=keyboard, parse_mode="Markdown")
            
            reset_draft(user_id)
            await state.clear()
            
        except Exception as api_err:
            # ИСПРАВЛЕНО: Используем централизованную обработку ошибок
            error_msg = str(api_err)
            if "Daily video limit exceeded" in error_msg or "limit exceeded" in error_msg.lower():
                # Лимит превышен - показываем понятное сообщение
                now = datetime.now(timezone.utc)
                tomorrow = (now.replace(hour=0, minute=0, second=0, microsecond=0) + timedelta(days=1))
                hours_until_reset = (tomorrow - now).total_seconds() / 3600
                
                limit_msg = (
                    f"{Emoji.WARNING} *Достигнут суточный лимит видео*\n\n"
                    f"{error_msg[:300]}\n\n"
                    f"Лимит сбросится через {int(hours_until_reset)} часов.\n"
                    f"Или обратитесь к администратору для увеличения лимита."
                )
                await loading_msg.edit_text(
                    limit_msg,
                    reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                        [InlineKeyboardButton(text=f"{Emoji.HOME} Меню", callback_data="menu:main")]
                    ]),
                    parse_mode="Markdown"
                )
            else:
                # ИСПРАВЛЕНО: Используем централизованную обработку ошибок
                error_text, error_keyboard = get_task_creation_error_message(api_err)
                await loading_msg.edit_text(
                    error_text,
                    reply_markup=error_keyboard,
                    parse_mode="Markdown"
                )
        
    except Exception as e:
        logger.error(f"Error creating task: {e}", exc_info=True)
        error_msg = str(e)
        if len(error_msg) > 150:
            error_msg = error_msg[:150] + "..."
        
        error_text = (
            "*Ошибка*\n\n"
            f"{error_msg}"
        )
        keyboard = InlineKeyboardMarkup(inline_keyboard=[
            [
                InlineKeyboardButton(text="🔄 Снова", callback_data="menu:new_order"),
                InlineKeyboardButton(text="🏠 Меню", callback_data="menu:main")
            ]
        ])
        
        if is_message:
            await message_or_edit_target.answer(error_text, reply_markup=keyboard, parse_mode="Markdown")
        else:
            await message_or_edit_target.edit_text(error_text, reply_markup=keyboard, parse_mode="Markdown")


@router.callback_query(F.data == "wizard:submit_order", StateFilter(OrderStates.CONFIRM_ORDER))
async def wizard_submit_order_legacy(callback: CallbackQuery, state: FSMContext):
    """Устаревший обработчик для обратной совместимости."""
    await callback.answer()
    await wizard_create_order_from_callback(callback, state)


@router.callback_query(F.data.in_({"wizard:pool:shared", "wizard:pool:personal"}))
async def wizard_select_pool(callback: CallbackQuery, state: FSMContext):
    """Сохраняет выбор пула и продолжает создание заказа.

    Этот handler срабатывает после промежуточного экрана выбора пула,
    который показывается из _wizard_create_order_impl, если у работника
    есть личные аккаунты.
    """
    try:
        if not callback.message:
            await callback.answer("Ошибка: сообщение не найдено", show_alert=True)
            return
        mode = "personal" if callback.data == "wizard:pool:personal" else "shared"
        if mode == "personal":
            user_backend_id = await get_user_backend_id(callback.from_user.id)
            if not user_backend_id:
                await callback.answer("Пользователь не найден", show_alert=True)
                return
            pool_summary = await api_client.get_personal_pool_summary(user_backend_id)
            if _personal_pool_usable_count(pool_summary) <= 0:
                await callback.answer(
                    "В личном пуле сейчас нет доступных аккаунтов. Используйте общий пул или обновите личные аккаунты.",
                    show_alert=True,
                )
                return
        await state.update_data(account_pool_mode=mode)
        await callback.answer(
            "Выбран пул: " + ("👤 Личный" if mode == "personal" else "🌐 Общий"),
            show_alert=False,
        )
        await _wizard_create_order_impl(callback.message, state)
    except Exception as e:
        logger.error(f"Error in wizard:pool: {e}", exc_info=True)
        try:
            await callback.answer("Ошибка", show_alert=True)
        except Exception:
            pass
