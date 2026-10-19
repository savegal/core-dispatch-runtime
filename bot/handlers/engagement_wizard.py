"""
Wizard для создания engagement_only заказов (Только ответы на существующий комментарий).
"""
from aiogram import Router, F
from aiogram.types import Message, CallbackQuery, InlineKeyboardMarkup, InlineKeyboardButton
from aiogram.fsm.context import FSMContext
from aiogram.filters import StateFilter
from bot.services.validators import validate_video_url
from bot.services.api_client import api_client
from bot.services.template_utils import (
    filter_reply_source_templates,
    is_replies_only_template,
    normalize_user_text,
    preserve_nonempty_lines,
    template_replies,
)
from bot.services.user_utils import get_user_backend_id
from bot.services.subscription_service import check_user_access
from bot.state.engagement_states import EngagementStates
from bot.keyboards.common import get_main_menu_keyboard
from bot.config import load_config
from config.settings import get_settings
from bot.keyboards.common import get_access_denied_keyboard
from bot.services.formatting import markdown_inline_code
from bot.utils.markdown_escape import escape_markdown_simple
from services.likes_config import BOOST_LIKES_DEFAULT, coerce_likes_quantity
from services.comment_search import normalize_search_phrase
import logging
import re

from bot.services.main_visibility_ui import REPLY_CALLBACK_PREFIX, reply_visibility_keyboard, set_reply_visibility_option

logger = logging.getLogger("bot.engagement_wizard")
router = Router()
config = load_config()
settings = get_settings()
ENGAGEMENT_TEMPLATES_PER_PAGE = 8


@router.callback_query(F.data.startswith(REPLY_CALLBACK_PREFIX))
async def engagement_reply_visibility_option(callback: CallbackQuery, state: FSMContext):
    await set_reply_visibility_option(callback, state)



def _mono_preview(text: object, limit: int) -> str:
    raw = ("" if text is None else str(text)).strip().replace("\n", " ")
    shortened = raw[:limit]
    if len(raw) > limit:
        shortened += "..."
    return markdown_inline_code(shortened or "—")


def _personal_pool_usable_count(summary: dict | None) -> int:
    if not isinstance(summary, dict):
        return 0
    if "available_reply_now" in summary:
        return max(0, int(summary.get("available_reply_now") or 0))
    if "available_main_now" in summary:
        return max(0, int(summary.get("available_main_now") or 0))
    return max(0, int(summary.get("active") or 0) + int(summary.get("error") or 0))


def _template_display_name(template: dict | None) -> str:
    template = template or {}
    template_id = template.get("id")
    name = str(template.get("name") or f"Шаблон #{template_id}").strip()
    return name or f"Шаблон #{template_id}"


def _template_reply_list(template: dict | None) -> list[str]:
    return template_replies(template)[: settings.MAX_REPLIES]


def _parse_comment_locator(raw_text: str) -> tuple[str | None, str]:
    """Parse optional '@username: comment text' input for engagement-only orders."""
    text = normalize_user_text(raw_text or "") or ""
    match = re.match(r"^\s*@([A-Za-z0-9._]{2,64})\s*[:：]\s*(.+?)\s*$", text, flags=re.S)
    if not match:
        return None, text
    username = match.group(1).strip().lstrip("@")
    comment_text = (match.group(2) or "").strip()
    if not comment_text:
        return None, text
    return username, comment_text


def _templates_with_replies(templates: list | None) -> list[dict]:
    return filter_reply_source_templates([template for template in (templates or []) if isinstance(template, dict)])


async def _boost_likes_ui_config() -> tuple[bool, int]:
    try:
        payload = await api_client.get_boost_likes_settings()
        enabled = bool(
            payload.get("boost_likes_enabled")
            and payload.get("boost_provider_available", True)
        )
        default_quantity = coerce_likes_quantity(
            settings.BOOST_PROJECT_LIKES_QUANTITY,
            BOOST_LIKES_DEFAULT,
        )
        quantity = coerce_likes_quantity(payload.get("boost_likes_quantity"), default_quantity)
        return enabled, quantity if enabled else 0
    except Exception as exc:
        logger.warning("engagement: boost likes config unavailable: %s", str(exc)[:120])
        return False, 0


def _reply_source_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="✍️ Ввести ответы вручную", callback_data="engagement:manual_replies")],
        [InlineKeyboardButton(text="📑 Выбрать шаблон", callback_data="engagement:choose_template")],
        [InlineKeyboardButton(text="❌ Отмена", callback_data="menu:main")],
    ])


def _template_choice_keyboard(templates: list[dict], page: int = 0) -> tuple[InlineKeyboardMarkup, int, int]:
    total_pages = (len(templates) + ENGAGEMENT_TEMPLATES_PER_PAGE - 1) // ENGAGEMENT_TEMPLATES_PER_PAGE if templates else 1
    current_page = max(0, min(page, total_pages - 1))
    start = current_page * ENGAGEMENT_TEMPLATES_PER_PAGE
    shown = templates[start:start + ENGAGEMENT_TEMPLATES_PER_PAGE]

    rows: list[list[InlineKeyboardButton]] = []
    for template in shown:
        template_id = template.get("id")
        if template_id is None:
            continue
        replies_count = len(_template_reply_list(template))
        name = _template_display_name(template)
        if len(name) > 28:
            name = name[:25] + "..."
        prefix = "↩️ " if is_replies_only_template(template) else ""
        rows.append([
            InlineKeyboardButton(
                text=f"{prefix}{name} · {replies_count} отв.",
                callback_data=f"engagement:template_select:{template_id}",
            )
        ])

    if total_pages > 1:
        nav: list[InlineKeyboardButton] = []
        if current_page > 0:
            nav.append(InlineKeyboardButton(text="◀️", callback_data=f"engagement:template_page:{current_page - 1}"))
        if current_page < total_pages - 1:
            nav.append(InlineKeyboardButton(text="▶️", callback_data=f"engagement:template_page:{current_page + 1}"))
        if nav:
            rows.append(nav)

    rows.append([InlineKeyboardButton(text="✍️ Ввести вручную", callback_data="engagement:manual_replies")])
    rows.append([InlineKeyboardButton(text="❌ Отмена", callback_data="menu:main")])
    return InlineKeyboardMarkup(inline_keyboard=rows), current_page, total_pages


@router.callback_query(F.data == "menu:engagement_only")
async def start_engagement_wizard(callback: CallbackQuery, state: FSMContext):
    """Начинает создание engagement_only заказа."""
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
        
        await callback.message.edit_text(
            "*⚡ Только ответы*\n\n"
            "Этот тип заказа работает с *уже существующим* комментарием: главные комментарии не публикуются.\n"
            "Софт отвечает на первый найденный комментарий со словом или фразой из вашего запроса. "
            "Регистр и смайлики не учитываются. Можно ограничить поиск username автора.\n\n"
            "🔗 *Шаг 1:* Отправьте ссылку на видео TikTok:",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="❌ Отмена", callback_data="menu:main")]
            ]),
            parse_mode="Markdown"
        )
        await state.set_state(EngagementStates.INPUT_VIDEO_URL)
        
    except Exception as e:
        logger.error(f"Error starting engagement wizard: {e}", exc_info=True)
        await callback.answer("Ошибка запуска", show_alert=True)


@router.message(StateFilter(EngagementStates.INPUT_VIDEO_URL))
async def process_video_url(message: Message, state: FSMContext):
    """Обрабатывает URL видео."""
    try:
        video_url = normalize_user_text(message.text or "") or ""
        
        if not validate_video_url(video_url):
            await message.answer(
                "❌ Неверный формат ссылки TikTok.\n\n"
                "Отправьте корректную ссылку на видео TikTok.",
                reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                    [InlineKeyboardButton(text="❌ Отмена", callback_data="menu:main")]
                ])
            )
            return
        
        await state.update_data(video_url=video_url)
        
        await message.answer(
            "*⚡ Только ответы*\n\n"
            "🔍 *Шаг 2:* Отправьте слово или фразу для поиска.\n\n"
            "Например: `где посмотреть` — найдём первый комментарий с этой фразой.\n"
            "Регистр и смайлики игнорируются; слова должны идти подряд.\n"
            "Для поиска у конкретного автора: `@username: где посмотреть`.",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="❌ Отмена", callback_data="menu:main")]
            ]),
            parse_mode="Markdown"
        )
        await state.set_state(EngagementStates.INPUT_COMMENT_TEXT)
        
    except Exception as e:
        logger.error(f"Error processing video URL: {e}", exc_info=True)
        await message.answer("Ошибка обработки URL", reply_markup=get_main_menu_keyboard())


@router.message(StateFilter(EngagementStates.INPUT_COMMENT_TEXT))
async def process_comment_text(message: Message, state: FSMContext):
    """Обрабатывает текст комментария."""
    try:
        username, comment_text = _parse_comment_locator(message.text or "")
        
        if not comment_text:
            await message.answer("❌ Текст комментария не может быть пустым.")
            return
        if not normalize_search_phrase(comment_text):
            await message.answer("❌ Укажите слово или фразу: одних смайликов недостаточно для поиска.")
            return
        
        await state.update_data(
            comment_text=comment_text,
            comment_id=None,
            username=username,
            boost_likes_enabled=True,
        )

        await ask_for_reply_source(message, state)
        
    except Exception as e:
        logger.error(f"Error processing comment_text: {e}", exc_info=True)
        await message.answer("Ошибка обработки текста", reply_markup=get_main_menu_keyboard())


async def ask_for_reply_source(message: Message, state: FSMContext):
    """Asks whether replies should be entered manually or loaded from a template."""
    await message.answer(
        "*⚡ Только ответы*\n\n"
        "💬 *Шаг 3:* Откуда взять ответы?\n\n"
        "Можно ввести ответы вручную или выбрать существующий шаблон. "
        "Из шаблона будут использованы *только ответы* — главные комментарии из шаблона публиковаться не будут.",
        reply_markup=_reply_source_keyboard(),
        parse_mode="Markdown",
    )
    await state.set_state(EngagementStates.CHOOSE_REPLY_SOURCE)


@router.callback_query(F.data == "engagement:manual_replies", StateFilter(EngagementStates.CHOOSE_REPLY_SOURCE, EngagementStates.CHOOSE_TEMPLATE, EngagementStates.CONFIRM_ORDER))
async def choose_manual_replies(callback: CallbackQuery, state: FSMContext):
    try:
        await callback.answer()
        await state.update_data(template_id=None, template_name=None, template_usage=None, reply_source="manual")
        await ask_for_replies(callback.message, state)
    except Exception as e:
        logger.error(f"Error choosing manual engagement replies: {e}", exc_info=True)
        await callback.answer("Ошибка", show_alert=True)


async def _show_engagement_template_choice(message_or_callback: Message | CallbackQuery, state: FSMContext, page: int = 0):
    user_id = message_or_callback.from_user.id
    user_backend_id = await get_user_backend_id(user_id)
    if not user_backend_id:
        text = "Не удалось найти пользователя. Попробуйте начать заново."
        if isinstance(message_or_callback, Message):
            await message_or_callback.answer(text, reply_markup=get_main_menu_keyboard())
        else:
            await message_or_callback.answer(text, show_alert=True)
        return

    raw_templates = await api_client.list_task_templates(user_backend_id)
    if isinstance(raw_templates, dict):
        raw_templates = raw_templates.get("templates") or raw_templates.get("items") or []
    templates = _templates_with_replies(raw_templates)
    await state.update_data(engagement_reply_templates=templates)
    if not templates:
        text = (
            "*⚡ Только ответы*\n\n"
            "У вас пока нет шаблонов с ответами.\n\n"
            "Можно ввести ответы вручную."
        )
        keyboard = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="✍️ Ввести вручную", callback_data="engagement:manual_replies")],
            [InlineKeyboardButton(text="❌ Отмена", callback_data="menu:main")],
        ])
        if isinstance(message_or_callback, Message):
            await message_or_callback.answer(text, reply_markup=keyboard, parse_mode="Markdown")
        else:
            await message_or_callback.message.edit_text(text, reply_markup=keyboard, parse_mode="Markdown")
        await state.set_state(EngagementStates.CHOOSE_TEMPLATE)
        return

    keyboard, current_page, total_pages = _template_choice_keyboard(templates, page=page)
    text = (
        "*⚡ Только ответы · шаблон*\n\n"
        "Выберите шаблон. В заказ пойдут *только ответы* из выбранного шаблона; "
        "главные комментарии из шаблона будут проигнорированы.\n\n"
        f"Страница {current_page + 1}/{total_pages}"
    )
    if isinstance(message_or_callback, Message):
        await message_or_callback.answer(text, reply_markup=keyboard, parse_mode="Markdown")
    else:
        await message_or_callback.message.edit_text(text, reply_markup=keyboard, parse_mode="Markdown")
    await state.set_state(EngagementStates.CHOOSE_TEMPLATE)


@router.callback_query(F.data == "engagement:choose_template", StateFilter(EngagementStates.CHOOSE_REPLY_SOURCE, EngagementStates.CONFIRM_ORDER))
async def choose_template_replies(callback: CallbackQuery, state: FSMContext):
    try:
        await callback.answer()
        await _show_engagement_template_choice(callback, state, page=0)
    except Exception as e:
        logger.error(f"Error showing engagement templates: {e}", exc_info=True)
        await callback.answer("Ошибка загрузки шаблонов", show_alert=True)


@router.callback_query(F.data.startswith("engagement:template_page:"), StateFilter(EngagementStates.CHOOSE_TEMPLATE))
async def engagement_template_page(callback: CallbackQuery, state: FSMContext):
    try:
        await callback.answer()
        page = int((callback.data or "").split(":")[-1])
        await _show_engagement_template_choice(callback, state, page=page)
    except Exception as e:
        logger.error(f"Error switching engagement template page: {e}", exc_info=True)
        await callback.answer("Ошибка", show_alert=True)


@router.callback_query(F.data.startswith("engagement:template_select:"), StateFilter(EngagementStates.CHOOSE_TEMPLATE))
async def select_engagement_template(callback: CallbackQuery, state: FSMContext):
    try:
        await callback.answer()
        template_id = int((callback.data or "").split(":")[-1])
        user_backend_id = await get_user_backend_id(callback.from_user.id)
        if not user_backend_id:
            await callback.answer("Пользователь не найден", show_alert=True)
            return

        template: dict | None = None
        try:
            loaded = await api_client.get_task_template(template_id, user_backend_id)
            template = loaded if isinstance(loaded, dict) else None
        except Exception:
            data = await state.get_data()
            for item in data.get("engagement_reply_templates") or []:
                if int(item.get("id") or 0) == template_id:
                    template = item
                    break

        replies = _template_reply_list(template)
        if not template or not replies:
            await callback.answer("В этом шаблоне нет ответов", show_alert=True)
            return

        await state.update_data(
            replies=replies,
            reply1=replies[0] if len(replies) >= 1 else None,
            reply2=replies[1] if len(replies) >= 2 else None,
            reply3=replies[2] if len(replies) >= 3 else None,
            template_id=template_id,
            template_name=_template_display_name(template),
            template_usage="replies_only",
            reply_source="template",
        )
        await show_engagement_preview(callback, state)
    except Exception as e:
        logger.error(f"Error selecting engagement template: {e}", exc_info=True)
        await callback.answer("Ошибка выбора шаблона", show_alert=True)


async def ask_for_replies(message: Message, state: FSMContext):
    """Просит ввести replies."""
    data = await state.get_data()
    if data.get("reply_source") != "template":
        await state.update_data(replies=[], reply1=None, reply2=None, reply3=None)
    await message.answer(
        "*⚡ Только ответы*\n\n"
        f"💬 *Шаг 4:* Ответы\n\n"
        f"Отправляйте ответы по одному сообщению. Максимум: *{settings.MAX_REPLIES}*.\n"
        "Когда закончите, нажмите «Готово». Для заказа нужен хотя бы один ответ.",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="✅ Готово", callback_data="engagement:finish_replies")],
            [InlineKeyboardButton(text="❌ Отмена", callback_data="menu:main")]
        ]),
        parse_mode="Markdown"
    )
    await state.set_state(EngagementStates.INPUT_REPLIES)


def _engagement_replies_from_state(data: dict) -> list[str]:
    replies = data.get("replies")
    if isinstance(replies, list):
        return [
            text
            for reply in replies
            for text in [normalize_user_text(reply)]
            if text
        ][: settings.MAX_REPLIES]
    fallback = [data.get("reply1"), data.get("reply2"), data.get("reply3")]
    return [
        text
        for reply in fallback
        for text in [normalize_user_text(reply)]
        if text
    ][: settings.MAX_REPLIES]


def _engagement_replies_keyboard(has_replies: bool = True) -> InlineKeyboardMarkup:
    rows = [[InlineKeyboardButton(text="✅ Готово", callback_data="engagement:finish_replies")]]
    if has_replies:
        rows.append([InlineKeyboardButton(text="🧹 Очистить ответы", callback_data="engagement:clear_replies")])
    rows.append([InlineKeyboardButton(text="❌ Отмена", callback_data="menu:main")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


async def _ask_for_first_reply_again(message_or_callback: Message | CallbackQuery, state: FSMContext):
    await state.update_data(replies=[], reply1=None, reply2=None, reply3=None)
    text = (
        "*⚡ Только ответы*\n\n"
        "Ответы очищены.\n\n"
        f"*Ответ 1 из {settings.MAX_REPLIES}*\n\n"
        "Отправьте первый ответ отдельным сообщением. Для этого заказа нужен хотя бы один ответ."
    )
    keyboard = _engagement_replies_keyboard(has_replies=False)
    if isinstance(message_or_callback, Message):
        await message_or_callback.answer(text, reply_markup=keyboard, parse_mode="Markdown")
    else:
        await message_or_callback.message.edit_text(text, reply_markup=keyboard, parse_mode="Markdown")
    await state.set_state(EngagementStates.INPUT_REPLIES)


async def _finish_replies_or_warn(message_or_callback: Message | CallbackQuery, state: FSMContext):
    data = await state.get_data()
    replies = _engagement_replies_from_state(data)
    if replies:
        if isinstance(message_or_callback, CallbackQuery):
            await message_or_callback.answer()
        await show_engagement_preview(message_or_callback, state)
        return

    text = (
        "Пока нет ни одного ответа.\n\n"
        "Отправьте первый ответ или выберите шаблон с ответами."
    )
    if isinstance(message_or_callback, Message):
        await message_or_callback.answer(
            text,
            reply_markup=_engagement_replies_keyboard(has_replies=False),
            parse_mode="Markdown",
        )
    else:
            await message_or_callback.answer("Добавьте хотя бы один ответ.", show_alert=True)


@router.message(StateFilter(EngagementStates.INPUT_REPLIES))
async def process_engagement_reply(message: Message, state: FSMContext):
    """Собирает ответы для engagement_only по одному сообщению."""
    try:
        raw_text = message.text or ""
        normalized = normalize_user_text(raw_text)
        if not normalized:
            await message.answer(
                "❌ Ответ не может быть пустым. Отправьте текст или нажмите «Готово».",
                reply_markup=_engagement_replies_keyboard(),
            )
            return

        if normalized.casefold() in {"готово", "done", "стоп", "пропустить", "skip", "нет"}:
            await _finish_replies_or_warn(message, state)
            return

        if len(preserve_nonempty_lines(raw_text)) > 1:
            await message.answer(
                "❌ Отправьте один ответ одним сообщением. Следующий ответ я попрошу отдельно.",
                reply_markup=_engagement_replies_keyboard(),
            )
            return

        if len(normalized) > settings.MAX_REPLY_LENGTH:
            await message.answer(
                f"❌ Ответ слишком длинный. Максимум: {settings.MAX_REPLY_LENGTH} символов.",
                reply_markup=_engagement_replies_keyboard(),
            )
            return

        data = await state.get_data()
        replies = _engagement_replies_from_state(data)
        if len(replies) >= settings.MAX_REPLIES:
            await message.answer(f"✅ Лимит {settings.MAX_REPLIES} ответов достигнут.")
            await show_engagement_preview(message, state)
            return

        replies.append(normalized)
        await state.update_data(replies=replies)

        if len(replies) >= settings.MAX_REPLIES:
            await message.answer(f"✅ Сохранено {len(replies)} ответов.")
            await show_engagement_preview(message, state)
            return

        await message.answer(
            f"✅ Ответ {len(replies)} сохранён.\n\n"
            f"*Ответ {len(replies) + 1} из {settings.MAX_REPLIES}*\n\n"
            "Отправьте следующий ответ или нажмите «Готово».",
            reply_markup=_engagement_replies_keyboard(has_replies=True),
            parse_mode="Markdown",
        )
    except Exception as e:
        logger.error(f"Error processing engagement reply: {e}", exc_info=True)
        await message.answer("Ошибка обработки ответа", reply_markup=get_main_menu_keyboard())


@router.callback_query(F.data == "engagement:finish_replies", StateFilter(EngagementStates.INPUT_REPLIES))
async def finish_engagement_replies(callback: CallbackQuery, state: FSMContext):
    """Завершает ввод ответов и показывает превью."""
    try:
        await _finish_replies_or_warn(callback, state)
    except Exception as e:
        logger.error(f"Error finishing engagement replies: {e}", exc_info=True)
        await callback.answer("Ошибка", show_alert=True)


@router.callback_query(F.data == "engagement:clear_replies", StateFilter(EngagementStates.INPUT_REPLIES))
async def clear_engagement_replies(callback: CallbackQuery, state: FSMContext):
    """Очищает собранные ответы и оставляет пользователя на шаге ввода."""
    try:
        await callback.answer("Ответы очищены")
        await _ask_for_first_reply_again(callback, state)
    except Exception as e:
        logger.error(f"Error clearing engagement replies: {e}", exc_info=True)
        await callback.answer("Ошибка", show_alert=True)


@router.callback_query(F.data == "engagement:skip_replies", StateFilter(EngagementStates.INPUT_REPLIES))
async def skip_engagement_replies(callback: CallbackQuery, state: FSMContext):
    """Legacy callback guard: engagement_only must contain replies."""
    try:
        await callback.answer("Для этого заказа нужен хотя бы один ответ.", show_alert=True)
    except Exception as e:
        logger.error(f"Error skipping engagement replies: {e}", exc_info=True)
        await callback.answer("Ошибка", show_alert=True)


@router.message(StateFilter(EngagementStates.INPUT_REPLY1))
async def process_reply1(message: Message, state: FSMContext):
    """Обрабатывает первый ответ."""
    try:
        reply1 = normalize_user_text(message.text or "") or ""
        
        if not reply1:
            await message.answer("❌ Ответ не может быть пустым. Введите текст или нажмите 'Пропустить'.")
            return
        
        await state.update_data(reply1=reply1)
        
        await message.answer(
            "*⚡ Только ответы*\n\n"
            "💬 *Шаг 5:* Введите второй ответ (опционально)\n\n"
            "Отправьте второй ответ или 'пропустить'.",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="⏭ Пропустить", callback_data="engagement:skip_reply:2")],
                [InlineKeyboardButton(text="❌ Отмена", callback_data="menu:main")]
            ]),
            parse_mode="Markdown"
        )
        await state.set_state(EngagementStates.INPUT_REPLY2)
        
    except Exception as e:
        logger.error(f"Error processing reply1: {e}", exc_info=True)
        await message.answer("Ошибка обработки ответа", reply_markup=get_main_menu_keyboard())


@router.callback_query(F.data.startswith("engagement:skip_reply:"), StateFilter(EngagementStates.INPUT_REPLY1, EngagementStates.INPUT_REPLY2, EngagementStates.INPUT_REPLY3))
async def skip_reply(callback: CallbackQuery, state: FSMContext):
    """Пропускает ввод ответа."""
    try:
        await callback.answer()
        reply_num = int(callback.data.split(":")[-1])
        
        if reply_num == 1:
            await state.update_data(reply1=None)
            await callback.message.edit_text(
                "*⚡ Только ответы*\n\n"
                "💬 *Шаг 5:* Введите второй ответ (опционально)\n\n"
                "Отправьте второй ответ или 'пропустить'.",
                reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                    [InlineKeyboardButton(text="⏭ Пропустить", callback_data="engagement:skip_reply:2")],
                    [InlineKeyboardButton(text="❌ Отмена", callback_data="menu:main")]
                ]),
                parse_mode="Markdown"
            )
            await state.set_state(EngagementStates.INPUT_REPLY2)
        elif reply_num == 2:
            await state.update_data(reply2=None)
            await callback.message.edit_text(
                "*⚡ Только ответы*\n\n"
                "💬 *Шаг 6:* Введите третий ответ (опционально)\n\n"
                "Отправьте третий ответ или 'пропустить'.",
                reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                    [InlineKeyboardButton(text="⏭ Пропустить", callback_data="engagement:skip_reply:3")],
                    [InlineKeyboardButton(text="❌ Отмена", callback_data="menu:main")]
                ]),
                parse_mode="Markdown"
            )
            await state.set_state(EngagementStates.INPUT_REPLY3)
        elif reply_num == 3:
            await state.update_data(reply3=None)
            # Переходим к подтверждению
            await show_engagement_preview(callback, state)
        
    except Exception as e:
        logger.error(f"Error skipping reply: {e}", exc_info=True)
        await callback.answer("Ошибка", show_alert=True)


@router.message(StateFilter(EngagementStates.INPUT_REPLY2))
async def process_reply2(message: Message, state: FSMContext):
    """Обрабатывает второй ответ."""
    try:
        reply2 = normalize_user_text(message.text or "") or ""
        
        if not reply2:
            await message.answer("❌ Ответ не может быть пустым. Введите текст или нажмите 'Пропустить'.")
            return
        
        await state.update_data(reply2=reply2)
        
        await message.answer(
            "*⚡ Только ответы*\n\n"
            "💬 *Шаг 6:* Введите третий ответ (опционально)\n\n"
            "Отправьте третий ответ или 'пропустить'.",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="⏭ Пропустить", callback_data="engagement:skip_reply:3")],
                [InlineKeyboardButton(text="❌ Отмена", callback_data="menu:main")]
            ]),
            parse_mode="Markdown"
        )
        await state.set_state(EngagementStates.INPUT_REPLY3)
        
    except Exception as e:
        logger.error(f"Error processing reply2: {e}", exc_info=True)
        await message.answer("Ошибка обработки ответа", reply_markup=get_main_menu_keyboard())


@router.message(StateFilter(EngagementStates.INPUT_REPLY3))
async def process_reply3(message: Message, state: FSMContext):
    """Обрабатывает третий ответ."""
    try:
        reply3 = normalize_user_text(message.text or "") or ""
        
        if not reply3:
            await message.answer("❌ Ответ не может быть пустым. Введите текст или нажмите 'Пропустить'.")
            return
        
        await state.update_data(reply3=reply3)
        
        await show_engagement_preview(message, state)
        
    except Exception as e:
        logger.error(f"Error processing reply3: {e}", exc_info=True)
        await message.answer("Ошибка обработки ответа", reply_markup=get_main_menu_keyboard())


async def show_engagement_preview(message_or_callback: Message | CallbackQuery, state: FSMContext):
    """Показывает превью engagement_only заказа."""
    try:
        data = await state.get_data()
        video_url = data.get("video_url")
        comment_text = data.get("comment_text")
        replies = _engagement_replies_from_state(data)
        global_likes_enabled, likes_quantity = await _boost_likes_ui_config()
        boost_likes_enabled = bool(data.get("boost_likes_enabled", True) and global_likes_enabled)
        
        # Формируем текст превью
        preview_text = "*⚡ Только ответы · подтверждение*\n\n"
        preview_text += f"🔗 *Видео:* {escape_markdown_simple((video_url or '')[:70])}{'...' if video_url and len(video_url) > 70 else ''}\n\n"

        preview_text += "🔍 *Поиск:* первое совпадение слова/фразы, без регистра и смайликов\n"
        preview_text += f"📝 *Текст:* {_mono_preview(comment_text, 120)}\n"
        preview_text += "👤 *Автор:* не проверяем\n\n"

        if data.get("template_id"):
            preview_text += f"📑 *Шаблон:* {escape_markdown_simple(data.get('template_name') or 'Без названия')}\n"
            preview_text += "_Из шаблона используются только ответы._\n\n"
        
        preview_text += f"*Ответы:* {len(replies)}\n"
        for idx, reply in enumerate(replies[:5], start=1):
            preview_text += f"{idx}. {_mono_preview(reply, 80)}\n"
        if len(replies) > 5:
            preview_text += f"... и ещё {len(replies) - 5}\n"
        if not replies:
            preview_text += "Ответы не добавлены\n"
        
        if global_likes_enabled:
            if boost_likes_enabled:
                preview_text += f"\n❤️ *Лайки:* добавить {likes_quantity}\n\n"
            else:
                preview_text += "\n🤍 *Лайки:* не добавлять\n\n"
        else:
            preview_text += "\n🤍 *Лайки:* выключены в настройках сервиса\n\n"
        preview_text += "🚀 *Запустить заказ?*"

        rows = [
            [InlineKeyboardButton(text="✅ Запустить", callback_data="engagement:confirm")],
            [InlineKeyboardButton(text="↩️ Изменить ответы", callback_data="engagement:edit_replies")],
        ]
        if global_likes_enabled:
            rows.append([
                InlineKeyboardButton(
                    text=("💔 Без лайков" if boost_likes_enabled else "❤️ Добавить лайки"),
                    callback_data="engagement:toggle_likes",
                )
            ])
        rows.append([InlineKeyboardButton(text="📑 Выбрать шаблон", callback_data="engagement:choose_template")])
        rows.append(
            [InlineKeyboardButton(text="❌ Отмена", callback_data="menu:main")]
        )
        keyboard = reply_visibility_keyboard(InlineKeyboardMarkup(inline_keyboard=rows), bool(data.get("verify_reply_visibility", False)))
        
        if isinstance(message_or_callback, Message):
            await message_or_callback.answer(preview_text, reply_markup=keyboard, parse_mode="Markdown")
        else:
            await message_or_callback.message.edit_text(preview_text, reply_markup=keyboard, parse_mode="Markdown")
        
        await state.set_state(EngagementStates.CONFIRM_ORDER)
        
    except Exception as e:
        logger.error(f"Error showing preview: {e}", exc_info=True)
        if isinstance(message_or_callback, Message):
            await message_or_callback.answer("Ошибка создания превью", reply_markup=get_main_menu_keyboard())
        else:
            await message_or_callback.answer("Ошибка создания превью", show_alert=True)


@router.callback_query(F.data == "engagement:toggle_likes", StateFilter(EngagementStates.CONFIRM_ORDER))
async def toggle_engagement_likes(callback: CallbackQuery, state: FSMContext):
    """Toggles optional boost likes for engagement_only order."""
    try:
        data = await state.get_data()
        enabled, _quantity = await _boost_likes_ui_config()
        if not enabled:
            await state.update_data(boost_likes_enabled=False)
            await callback.answer("Лайки выключены в настройках сервиса", show_alert=True)
            return
        new_value = not bool(data.get("boost_likes_enabled", True))
        await state.update_data(boost_likes_enabled=new_value)
        await callback.answer("Лайки добавим" if new_value else "Без лайков")
        await show_engagement_preview(callback, state)
    except Exception as e:
        logger.error(f"Error toggling engagement likes: {e}", exc_info=True)
        await callback.answer("Ошибка", show_alert=True)


@router.callback_query(F.data == "engagement:edit_replies", StateFilter(EngagementStates.CONFIRM_ORDER))
async def edit_engagement_replies(callback: CallbackQuery, state: FSMContext):
    """Возвращает пользователя из превью к редактированию списка ответов."""
    try:
        await callback.answer()
        data = await state.get_data()
        replies = _engagement_replies_from_state(data)
        if not replies:
            await _ask_for_first_reply_again(callback, state)
            return

        await state.update_data(replies=replies)
        prompt_tail = (
            f"Отправьте следующий ответ до *{settings.MAX_REPLIES}*, нажмите «Готово» или очистите список."
            if len(replies) < settings.MAX_REPLIES
            else "Лимит ответов достигнут. Нажмите «Готово» или очистите список."
        )
        await callback.message.edit_text(
            "*⚡ Только ответы*\n\n"
            f"Сейчас сохранено ответов: *{len(replies)}*.\n\n"
            f"{prompt_tail}",
            reply_markup=_engagement_replies_keyboard(has_replies=True),
            parse_mode="Markdown",
        )
        await state.set_state(EngagementStates.INPUT_REPLIES)
    except Exception as e:
        logger.error(f"Error editing engagement replies: {e}", exc_info=True)
        await callback.answer("Ошибка", show_alert=True)


@router.callback_query(F.data == "engagement:confirm", StateFilter(EngagementStates.CONFIRM_ORDER))
async def confirm_engagement_order(callback: CallbackQuery, state: FSMContext):
    """Подтверждает и создает engagement_only заказ."""
    try:
        await callback.answer()
        
        user_id = callback.from_user.id
        user_backend_id = await get_user_backend_id(user_id)
        
        if not user_backend_id:
            await callback.answer("Ошибка: пользователь не найден", show_alert=True)
            return
        
        data = await state.get_data()
        video_url = data.get("video_url")
        comment_text = normalize_user_text(data.get("comment_text") or "") or ""
        replies = _engagement_replies_from_state(data)
        reply1 = replies[0] if len(replies) >= 1 else None
        reply2 = replies[1] if len(replies) >= 2 else None
        reply3 = replies[2] if len(replies) >= 3 else None

        # Валидация: обязательные поля должны быть заполнены
        if not video_url or not validate_video_url(video_url):
            await callback.answer("Ссылка на видео потеряна или некорректна. Начните заново.", show_alert=True)
            await state.clear()
            return

        if not normalize_search_phrase(comment_text):
            await callback.answer("Укажите слово или фразу для поиска, а не только смайлики. Начните заново.", show_alert=True)
            await state.clear()
            return

        if not replies:
            await callback.answer("Добавьте хотя бы один ответ.", show_alert=True)
            return

        # Если работник ещё не выбрал пул и у него есть личные аккаунты — спрашиваем.
        # Если личных нет или вызов API упал — продолжаем с общим пулом (fail-soft).
        if "account_pool_mode" not in data:
            try:
                pool_summary = await api_client.get_personal_pool_summary(user_backend_id)
                personal_total = int((pool_summary or {}).get("total", 0) or 0)
                personal_usable = _personal_pool_usable_count(pool_summary)
                if personal_usable > 0:
                    if personal_total == personal_usable:
                        personal_line = f"👤 *Личные аккаунты* — у вас сейчас *{personal_usable}* шт."
                    else:
                        personal_line = (
                            f"👤 *Личные аккаунты* — доступно сейчас: *{personal_usable}* "
                            f"из *{personal_total}* шт."
                        )
                    await callback.message.edit_text(
                        "*Какие аккаунты использовать?*\n\n"
                        "🌐 *Общий пул* — заказ идёт на общих аккаунтах сервиса.\n"
                        f"{personal_line}\n\n"
                        "_Прокси используются общие в обоих режимах._",
                        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                            [InlineKeyboardButton(text="🌐 Общий пул", callback_data="engagement:pool:shared")],
                            [InlineKeyboardButton(text="👤 Мои аккаунты", callback_data="engagement:pool:personal")],
                            [InlineKeyboardButton(text="❌ Отмена", callback_data="menu:main")],
                        ]),
                        parse_mode="Markdown",
                    )
                    return
            except Exception as exc:
                logger.warning(f"engagement: pool summary unavailable, fallback to shared: {exc}")

        safe_main_comment = comment_text.strip()
        safe_text_variants = [safe_main_comment] * 3

        # Формируем settings
        account_pool_mode = (data.get("account_pool_mode") or "shared").lower()
        if account_pool_mode not in {"shared", "personal"}:
            account_pool_mode = "shared"
        global_likes_enabled, _likes_quantity = await _boost_likes_ui_config()
        effective_likes_enabled = bool(data.get("boost_likes_enabled", True) and global_likes_enabled)

        task_settings = {
            "origin": "engagement_only",
            "verify_reply_visibility": bool(data.get("verify_reply_visibility", False)),
            "reply_count": len(replies),
            "replies": replies,
            "delays": True,
            "shuffle": False,
            "rotate_accounts": True,
            "account_pool_mode": account_pool_mode,
            "engagement_comment_text": comment_text,
            "comment_text_match": "phrase",
            "boost_likes_enabled": effective_likes_enabled,
        }
        if data.get("username"):
            task_settings["engagement_username"] = data.get("username")
        if data.get("template_id"):
            task_settings["template_id"] = data.get("template_id")
            task_settings["template_name"] = data.get("template_name")
            task_settings["template_usage"] = "replies_only"
        
        # Создаем заказ через API
        task_result = await api_client.create_task(
            user_id=user_backend_id,
            main_comment=safe_main_comment,
            video_urls=[video_url],
            text_variants=safe_text_variants,  # Минимум 3 варианта
            reply1=reply1,
            reply2=reply2,
            reply3=reply3,
            replies=replies,
            likes=0,
            settings=task_settings,
            kind="engagement_only",  # HYBRID: Тип заказа
            engagement_comment_id=None,
            engagement_username=data.get("username"),
            engagement_comment_text=comment_text
        )
        
        task_id = task_result.get("task_id")
        
        if not task_id:
            await callback.answer("Ошибка создания заказа", show_alert=True)
            return
        
        await callback.message.edit_text(
            f"✅ *Заказ создан!*\n\n"
            f"📋 Заказ #{task_id}\n"
            f"⚡ Тип: Только ответы\n"
            f"↩️ Ответов: {len(replies)}\n"
            f"❤️ Лайки: {'добавим' if effective_likes_enabled else 'без лайков'}\n"
            f"🔗 Видео: {escape_markdown_simple(video_url[:50])}...\n\n"
            f"Заказ добавлен в очередь обработки.",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="📋 Мои заказы", callback_data="menu:orders")],
                [InlineKeyboardButton(text="🏠 Главное меню", callback_data="menu:main")]
            ]),
            parse_mode="Markdown"
        )
        
        await state.clear()
        
    except Exception as e:
        logger.error(f"Error confirming engagement order: {e}", exc_info=True)
        await callback.answer(f"Ошибка создания заказа: {str(e)[:100]}", show_alert=True)


@router.callback_query(F.data.in_({"engagement:pool:shared", "engagement:pool:personal"}), StateFilter(EngagementStates.CONFIRM_ORDER))
async def engagement_select_pool(callback: CallbackQuery, state: FSMContext):
    """Сохраняет выбор пула и продолжает создание engagement-заказа."""
    try:
        if not callback.message:
            await callback.answer("Ошибка: сообщение не найдено", show_alert=True)
            return
        mode = "personal" if callback.data == "engagement:pool:personal" else "shared"
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
        # Повторно вызываем confirm — теперь account_pool_mode уже в state.
        await confirm_engagement_order(callback, state)
    except Exception as e:
        logger.error(f"Error in engagement:pool: {e}", exc_info=True)
        try:
            await callback.answer("Ошибка", show_alert=True)
        except Exception:
            pass
