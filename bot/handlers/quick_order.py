"""
Упрощенный интерфейс для быстрого создания заказов.
Когда пользователь скидывает ссылку, бот автоматически предлагает выбрать шаблон.
"""
from aiogram import Router, F
from aiogram.types import Message, CallbackQuery
from aiogram.fsm.context import FSMContext
from aiogram.filters import StateFilter
from bot.services.validators import parse_links, validate_video_url
from bot.services.api_client import APIRequestError, api_client
from bot.services.drafts import get_draft, set_links, reset_draft, save_draft
from bot.services.subscription_service import check_user_access
from bot.services.telegram_utils import safe_callback_answer
from bot.services.user_utils import get_user_backend_id
from bot.services.error_handlers import get_task_creation_error_message
from bot.services.formatting import Emoji, markdown_inline_code
from bot.services.likes_ui import get_likes_display_label_for_replies
from bot.services.order_creation_ui import build_success_text
from bot.services.template_utils import (
    filter_full_templates,
    has_meaningful_text,
    is_replies_only_template,
    normalize_text_variants_list,
    normalize_user_text,
    preserve_nonempty_lines,
    replies_dict_to_list,
    template_replies,
    template_text_variants,
)
from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton
from bot.keyboards.templates import get_template_choice_keyboard, get_template_preview_keyboard, format_template_preview
from bot.services.main_visibility_ui import CALLBACK_PREFIX as VISIBILITY_CALLBACK_PREFIX, set_visibility_option, visibility_keyboard, REPLY_CALLBACK_PREFIX, set_reply_visibility_option
from bot.keyboards.common import get_access_denied_keyboard
from bot.state.order_states import OrderStates
from bot.utils.markdown_escape import escape_markdown_simple
from config.settings import get_settings
import logging
settings = get_settings()
MAX_REPLIES = settings.MAX_REPLIES
logger = logging.getLogger('bot.quick_order')
router = Router()


@router.callback_query(F.data.startswith(REPLY_CALLBACK_PREFIX))
async def quick_order_reply_visibility_option(callback: CallbackQuery, state: FSMContext):
    await set_reply_visibility_option(callback, state)


@router.callback_query(F.data.startswith(VISIBILITY_CALLBACK_PREFIX))
async def quick_order_visibility_option(callback: CallbackQuery, state: FSMContext):
    await set_visibility_option(callback, state)

ACCOUNT_POOL_MODE_SHARED = 'shared'
ACCOUNT_POOL_MODE_PERSONAL = 'personal'


def _mono_preview(text: object, limit: int) -> str:
    raw = ("" if text is None else str(text)).strip().replace("\n", " ")
    shortened = raw[:limit]
    if len(raw) > limit:
        shortened += "..."
    return markdown_inline_code(shortened or "—")


async def _template_preview_text(
    template: dict,
    *,
    reply_count: int = 3,
    edited_comment: str | None = None,
    edited_text_variants: list[str] | None = None,
    edited_replies: dict | None = None,
) -> str:
    return format_template_preview(
        template,
        reply_count=reply_count,
        edited_comment=edited_comment,
        edited_text_variants=edited_text_variants,
        edited_replies=edited_replies,
        likes_label=await get_likes_display_label_for_replies(reply_count),
    )


async def _return_to_new_videos_template_picker(
    callback: CallbackQuery,
    state: FSMContext,
    *,
    user_backend_id: int,
    creator_id: int,
    batch_id: int,
) -> bool:
    """Возвращает пользователя в выбор шаблона для пачки новых видео.

    Раньше quick-order создавал FakeCallback и вызывал callback-handler
    напрямую. Теперь используем общий renderer, который принимает message/state
    и не зависит от внутренностей aiogram CallbackQuery.
    """
    if not callback.message:
        return False

    from bot.handlers.video_parsing_new_videos import render_template_picker

    return await render_template_picker(
        callback.message,
        state,
        user_id=int(user_backend_id),
        creator_id=int(creator_id),
        batch_id=int(batch_id),
        notify=callback.answer,
    )
QUICK_POOL_CALLBACK_PREFIX = 'quickpool'


def _normalize_account_pool_mode(mode) -> str:
    mode = str(mode or ACCOUNT_POOL_MODE_SHARED).lower()
    if mode == ACCOUNT_POOL_MODE_PERSONAL:
        return ACCOUNT_POOL_MODE_PERSONAL
    return ACCOUNT_POOL_MODE_SHARED


def _account_pool_label(mode: str) -> str:
    if _normalize_account_pool_mode(mode) == ACCOUNT_POOL_MODE_PERSONAL:
        return '👤 Мои аккаунты'
    return '🌐 Общий пул'


def _personal_pool_usable_count(summary: dict | None) -> int:
    if not isinstance(summary, dict):
        return 0
    if 'available_main_now' in summary:
        return max(0, int(summary.get('available_main_now') or 0))
    return max(0, int(summary.get('active') or 0) + int(summary.get('error') or 0))


def _build_quick_pool_keyboard(continue_callback_data: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(
                text='🌐 Общий пул',
                callback_data=f'{QUICK_POOL_CALLBACK_PREFIX}:{ACCOUNT_POOL_MODE_SHARED}:{continue_callback_data}',
            )
        ],
        [
            InlineKeyboardButton(
                text='👤 Мои аккаунты',
                callback_data=f'{QUICK_POOL_CALLBACK_PREFIX}:{ACCOUNT_POOL_MODE_PERSONAL}:{continue_callback_data}',
            )
        ],
        [InlineKeyboardButton(text=f'{Emoji.CANCEL} Отмена', callback_data='quicktpl:cancel')],
    ])


async def _ensure_quick_order_pool_selected(
    callback: CallbackQuery,
    state: FSMContext,
    user_backend_id: int,
    continue_callback_data: str,
) -> bool:
    """Ask which account pool to use before creating a quick-order task."""
    state_data = await state.get_data()
    if 'account_pool_mode' in state_data:
        normalized = _normalize_account_pool_mode(state_data.get('account_pool_mode'))
        if normalized != state_data.get('account_pool_mode'):
            await state.update_data(account_pool_mode=normalized)
        return True

    try:
        pool_summary = await api_client.get_personal_pool_summary(user_backend_id)
    except Exception as exc:
        logger.warning(f'Quick order: personal pool summary unavailable, fallback to shared: {exc}')
        await state.update_data(account_pool_mode=ACCOUNT_POOL_MODE_SHARED)
        return True

    personal_total = int((pool_summary or {}).get('total', 0) or 0)
    personal_usable = _personal_pool_usable_count(pool_summary)
    if personal_usable <= 0:
        await state.update_data(account_pool_mode=ACCOUNT_POOL_MODE_SHARED)
        return True

    if personal_total == personal_usable:
        personal_line = f'👤 *Личные аккаунты* — ваш приватный пул: *{personal_usable}* шт.'
    else:
        personal_line = (
            f'👤 *Личные аккаунты* — доступно сейчас: *{personal_usable}* '
            f'из *{personal_total}* шт.'
        )

    pool_text = (
        '*Какие аккаунты использовать для заказа?*\n\n'
        '🌐 *Общий пул* — заказ идет на общих аккаунтах сервиса.\n'
        f'{personal_line}\n\n'
        '_Выбор применяется только к этому заказу._'
    )

    if not callback.message:
        await callback.answer('Сообщение не найдено', show_alert=True)
        return False

    await callback.message.edit_text(
        pool_text,
        reply_markup=_build_quick_pool_keyboard(continue_callback_data),
        parse_mode='Markdown',
    )
    await callback.answer()
    return False


class _QuickPoolResumeCallback:
    def __init__(self, original: CallbackQuery, data: str):
        self._original = original
        self.data = data
        self.from_user = original.from_user
        self.message = original.message
        self.id = original.id

    def __getattr__(self, name):
        return getattr(self._original, name)

    async def answer(self, *args, **kwargs):
        return None


def _extract_comment_variant(raw_text: str | None) -> str | None:
    normalized = normalize_user_text(raw_text)
    if not has_meaningful_text(normalized):
        return None
    return normalized

async def ensure_access(message_or_callback) -> tuple[bool, dict | None]:
    """Проверяет доступ пользователя с кэшированием."""
    try:
        user_id = message_or_callback.from_user.id
        username = getattr(message_or_callback.from_user, 'username', None)
        user_data = await api_client.sync_telegram(user_id, username, use_cache=True)
        user_backend_id = user_data.get('id')
        role = user_data.get('role', 'user')
        has_access = await check_user_access(user_id, user_backend_id, role=role, use_cache=True)
        if not has_access:
            return (False, user_data)
        return (True, user_data)
    except Exception as e:
        logger.error(f'Error checking access: {e}')
        return (False, None)

@router.callback_query(F.data == 'quick:show_templates')
async def resume_quick_order_templates(callback: CallbackQuery, state: FSMContext):
    """Возобновляет показ шаблонов быстрого заказа (из меню)."""
    try:
        draft = get_draft(callback.from_user.id)
        if not draft.links:
            await callback.answer('Ссылки не найдены. Сначала отправьте ссылки на видео.', show_alert=True)
            return
        await back_to_template_choice(callback, state)
    except Exception as e:
        logger.error(f'Error resuming quick order templates: {e}', exc_info=True)
        await callback.answer('Не удалось открыть шаблоны', show_alert=True)

@router.callback_query(F.data == 'menu:new_order:back')
async def quick_order_back_legacy(callback: CallbackQuery, state: FSMContext):
    """Legacy alias for old back button in quick_order error screens."""
    await resume_quick_order_templates(callback, state)

async def _deprecated_handle_link_message(message: Message, state: FSMContext):
    """
    Обрабатывает сообщение с ссылкой TikTok.
    Если найдена ссылка, автоматически предлагает выбрать шаблон.
    Работает "из воздуха" - ловит любые сообщения с TikTok-ссылками.
    
    КРИТИЧНО: Проверяет состояние перед обработкой, чтобы не перехватывать сообщения,
    которые должны обрабатываться обработчиками с StateFilter (например, ввод комментария).
    """
    try:
        if message.text and message.text.startswith('/'):
            return
        current_state = await state.get_state()
        if current_state:
            state_name = str(current_state)
            skip_states = ['NEW_TEMPLATE', 'EDIT_TEMPLATE', 'INPUT_MAIN_COMMENT', 'INPUT_REPLY', 'CHOOSE_TEMPLATE', 'INPUT_LINKS', 'CREATING_TEMPLATE', 'EDITING_TEMPLATE', 'INPUT_COMMENT', 'REPLIES_ALL', 'INPUT_TEMPLATE_NAME']
            if any((x in state_name for x in skip_states)):
                logger.debug(f'Skipping handle_link_message for state: {state_name}')
                return
        links = parse_links(message.text or '')
        if not links:
            return
        valid_links = [link for link in links if validate_video_url(link)]
        if len(valid_links) == 0:
            await message.answer(f'❌ *Ссылки не валидны*\n\nПроверьте формат и попробуйте снова.\n\n*Правильный формат:*\n`https://www.tiktok.com/@user/video/123`\n`https://vm.tiktok.com/xxxxx`', parse_mode='Markdown')
            return
        if len(valid_links) != len(links):
            invalid_count = len(links) - len(valid_links)
            await message.answer(f'⚠️ *Внимание*\n\nНайдено {invalid_count} невалидных ссылок.\nОни будут проигнорированы.\n\n✅ Сохранено: {len(valid_links)} ссылок', parse_mode='Markdown')
        links = valid_links
        logger.info(f'Found TikTok links in message from user {message.from_user.id}: {len(links)} links')
        has_access, user_data = await ensure_access(message)
        if not has_access:
            await message.answer(f'*Доступ закрыт*\n\nСтоимость: {settings.SUBSCRIPTION_PRICE}\nДля активации напишите владельцу', reply_markup=get_access_denied_keyboard(), parse_mode='Markdown')
            return
        user_id = message.from_user.id
        user_backend_id = user_data.get('id')
        draft = get_draft(user_id)
        set_links(user_id, links)
        try:
            templates = await api_client.list_task_templates(user_backend_id)
            templates = filter_full_templates(templates)
        except Exception as e:
            logger.error(f'Error fetching templates: {e}')
            await message.answer('Ошибка загрузки шаблонов. Попробуйте позже.')
            return
        if not templates:
            await message.answer(f'🔗 {len(links)} ссылок\n\nНет шаблонов — создайте первый!', reply_markup=InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text='➕ Создать шаблон', callback_data='template:create_new'), InlineKeyboardButton(text=f'{Emoji.CANCEL} Отмена', callback_data='menu:main')]]))
            return
        links_text = '\n'.join(links[:3])
        if len(links) > 3:
            links_text += f'\n... и ещё {len(links) - 3}'
        await state.update_data(templates=templates, template_page=0)
        keyboard, _, _ = get_template_choice_keyboard(templates, page=0)
        await message.answer(f'Найдено ссылок: {len(links)}\n\nВыберите шаблон комментария:', reply_markup=keyboard, parse_mode='Markdown')
    except Exception as e:
        logger.error(f'Error handling link message: {e}', exc_info=True)

@router.callback_query(F.data.startswith('template:select:'))
async def select_template(callback: CallbackQuery, state: FSMContext):
    """Обрабатывает выбор шаблона."""
    try:
        template_id = int(callback.data.split(':')[-1])
        user_id = callback.from_user.id
        has_access, user_data = await ensure_access(callback)
        if not has_access:
            await callback.answer('Доступ запрещён', show_alert=True)
            return
        user_backend_id = user_data.get('id')
        templates = await api_client.list_task_templates(user_backend_id)
        template = next((t for t in templates if t.get('id') == template_id), None)
        if not template:
            await callback.answer('Шаблон не найден', show_alert=True)
            return
        if is_replies_only_template(template):
            await callback.answer('Этот шаблон подходит только для заказов на ответы', show_alert=True)
            return
        template_reply_list = template_replies(template)
        default_reply_count = min(len(template_reply_list), settings.MAX_REPLIES)
        await state.update_data(
            template_id=template_id,
            template_data=template,
            edited_comment=None,
            edited_text_variants=None,
            edited_replies={},
            reply_count=default_reply_count,
        )
        preview_text = await _template_preview_text(template, reply_count=default_reply_count)
        keyboard = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text='🚀 Создать заказ', callback_data=f'quicktpl:confirm:{template_id}:{default_reply_count}')],
            [InlineKeyboardButton(text='Изменить шаблон', callback_data=f'quicktpl:edit:{template_id}')],
            [InlineKeyboardButton(text='◀️ Назад', callback_data='quicktpl:back_to_choice')]
        ])
        state_data = await state.get_data()
        keyboard = visibility_keyboard(
            keyboard,
            bool(state_data.get('verify_main_visibility', False)),
            bool(state_data.get('verify_reply_visibility', False)),
        )
        await callback.message.edit_text(preview_text, reply_markup=keyboard, parse_mode='Markdown')
        await callback.answer()
    except Exception as e:
        logger.error(f'Error selecting template: {e}', exc_info=True)
        await callback.answer('Ошибка выбора шаблона', show_alert=True)

@router.callback_query(F.data.startswith('template:page:'))
async def template_page(callback: CallbackQuery, state: FSMContext):
    """Пагинация шаблонов в quick-order."""
    try:
        page = int(callback.data.split(':')[-1])
        user_id = callback.from_user.id
        has_access, user_data = await ensure_access(callback)
        if not has_access:
            await callback.answer('Доступ запрещён', show_alert=True)
            return
        user_backend_id = user_data.get('id')
        templates = await api_client.list_task_templates(user_backend_id)
        templates = filter_full_templates(templates)
        if not templates:
            await callback.answer('Шаблоны не найдены', show_alert=True)
            return
        keyboard, current_page, total_pages = get_template_choice_keyboard(templates, page=page)
        await state.update_data(templates=templates, template_page=current_page)
        draft = get_draft(user_id)
        links_count = len(draft.links) if draft.links else 0
        page_info = f' (стр. {current_page + 1}/{total_pages})' if total_pages > 1 else ''
        await callback.message.edit_text(f'Найдено ссылок: {links_count}\n\nВыберите шаблон комментария{page_info}:', reply_markup=keyboard, parse_mode='Markdown')
        await callback.answer()
    except Exception as e:
        logger.error(f'Error switching template page: {e}', exc_info=True)
        await callback.answer('Ошибка пагинации', show_alert=True)

@router.callback_query(F.data == 'template:cancel')
async def template_cancel_alias(callback: CallbackQuery, state: FSMContext):
    """Legacy callback alias for template cancel button."""
    await cancel_template_choice(callback, state)

@router.callback_query(F.data.startswith('quicktpl:edit:'))
async def start_edit_existing_template(callback: CallbackQuery, state: FSMContext):
    """Начинает редактирование выбранного шаблона."""
    try:
        template_id = int(callback.data.split(':')[-1])
        user_id = callback.from_user.id
        has_access, user_data = await ensure_access(callback)
        if not has_access:
            await callback.answer('Доступ запрещён', show_alert=True)
            return
        user_backend_id = user_data.get('id')
        templates = await api_client.list_task_templates(user_backend_id)
        template = next((t for t in templates if t.get('id') == template_id), None)
        if not template:
            await callback.answer('Шаблон не найден', show_alert=True)
            return
        if is_replies_only_template(template):
            await callback.answer('Этот шаблон подходит только для заказов на ответы', show_alert=True)
            return
        template_reply_list = template_replies(template)
        reply_count = min(len(template_reply_list), settings.MAX_REPLIES)
        await state.update_data(
            template_id=template_id,
            template_data=template,
            edited_comment=None,
            edited_text_variants=None,
            edited_replies={},
            reply_count=reply_count,
        )
        preview_text = await _template_preview_text(template, reply_count=reply_count)
        await callback.message.edit_text(preview_text, reply_markup=get_template_preview_keyboard(template_id, reply_count=reply_count, verify_main_visibility=bool((await state.get_data()).get("verify_main_visibility", False)), verify_reply_visibility=bool((await state.get_data()).get("verify_reply_visibility", False))), parse_mode='Markdown')
        await callback.answer()
    except Exception as e:
        logger.error(f'Error starting edit template: {e}', exc_info=True)
        await callback.answer('Ошибка', show_alert=True)

@router.callback_query(F.data == 'template:create_new')
async def create_new_template(callback: CallbackQuery, state: FSMContext):
    """Начинает создание нового шаблона."""
    try:
        has_access, user_data = await ensure_access(callback)
        if not has_access:
            await callback.answer('Доступ запрещён', show_alert=True)
            return
        await callback.answer()
        await callback.message.edit_text(
            '*Создание шаблона*\n\n'
            'Какой шаблон создать?',
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text='💬 Полный шаблон', callback_data='template:create_full')],
                [InlineKeyboardButton(text='↩️ Только ответы', callback_data='template:create_replies')],
                [InlineKeyboardButton(text='❌ Отмена', callback_data='quicktpl:cancel')],
            ]),
            parse_mode='Markdown',
        )
    except Exception as e:
        logger.error(f'Error showing template type choice: {e}', exc_info=True)
        await callback.answer('Ошибка', show_alert=True)


@router.callback_query(F.data == 'template:create_full')
async def create_full_template(callback: CallbackQuery, state: FSMContext):
    """Начинает создание полного шаблона."""
    try:
        has_access, user_data = await ensure_access(callback)
        if not has_access:
            await callback.answer('Доступ запрещён', show_alert=True)
            return
        await callback.answer()
        await callback.message.edit_text(
            '*Вариант комментария 1 из 3*\n\nОтправьте первый вариант одним сообщением.',
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text='❌ Отмена', callback_data='quicktpl:back_to_choice')]]),
            parse_mode='Markdown',
        )
        await state.update_data(
            creating_template_via_quick_order=True,
            main_comment=None,
            new_template_text_variants=[],
            new_template_variant_index=1,
            replies={},
            reply_count=0,
            template_type='full',
        )
        await state.set_state(OrderStates.NEW_TEMPLATE_MAIN_COMMENT)
        logger.debug(f'State set to NEW_TEMPLATE_MAIN_COMMENT for user {callback.from_user.id}')
    except Exception as e:
        logger.error(f'Error starting new template creation: {e}', exc_info=True)
        await callback.answer('Ошибка', show_alert=True)


@router.callback_query(F.data == 'template:create_replies')
async def create_replies_only_template(callback: CallbackQuery, state: FSMContext):
    """Начинает создание шаблона только для ответов."""
    try:
        has_access, user_data = await ensure_access(callback)
        if not has_access:
            await callback.answer('Доступ запрещён', show_alert=True)
            return
        await callback.answer()
        await state.update_data(
            creating_template_via_quick_order=True,
            main_comment='',
            new_template_text_variants=[],
            new_template_variant_index=1,
            replies={},
            reply_count=0,
            template_type='replies_only',
        )
        await callback.message.edit_text(
            f'*Шаблон ответов*\n\n'
            f'Отправьте первый ответ отдельным сообщением. Максимум: *{settings.MAX_REPLIES}*.\n'
            'Когда ответов достаточно, нажмите «Готово».',
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text='✅ Готово', callback_data='new_template:finish_replies')],
                [InlineKeyboardButton(text='❌ Отмена', callback_data='quicktpl:cancel')],
            ]),
            parse_mode='Markdown',
        )
        await state.set_state(OrderStates.NEW_TEMPLATE_REPLIES_ALL)
    except Exception as e:
        logger.error(f'Error starting replies-only template creation: {e}', exc_info=True)
        await callback.answer('Ошибка', show_alert=True)

@router.message(StateFilter(OrderStates.NEW_TEMPLATE_MAIN_COMMENT))
async def save_main_comment(message: Message, state: FSMContext):
    """Сохраняет три варианта основного комментария по одному сообщению."""
    try:
        current_state = await state.get_state()
        logger.debug(f'save_main_comment called for user {message.from_user.id}, state: {current_state}')
        state_data = await state.get_data()
        variant_index = int(state_data.get('new_template_variant_index', 1))
        variants = normalize_text_variants_list(state_data.get('new_template_text_variants') or [])
        main_comment = _extract_comment_variant(message.text)
        if main_comment is None:
            await message.answer(
                f'❌ *Вариант {variant_index} не распознан*\n\nОтправьте один вариант отдельным сообщением.',
                reply_markup=InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text=f'{Emoji.CANCEL} Отмена', callback_data='quicktpl:cancel')]]),
                parse_mode='Markdown'
            )
            return
        from config.settings import get_settings
        settings = get_settings()
        if len(main_comment) < 2:
            await message.answer('⚠️ *Комментарий слишком короткий*\n\nКомментарий должен содержать хотя бы 2 символа.', reply_markup=InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text=f'{Emoji.CANCEL} Отмена', callback_data='quicktpl:cancel')]]), parse_mode='Markdown')
            return
        if any(existing.lower() == main_comment.lower() for existing in variants):
            await message.answer(
                f'⚠️ *Вариант {variant_index} повторяется*\n\nОтправьте формулировку, которая реально отличается.',
                reply_markup=InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text=f'{Emoji.CANCEL} Отмена', callback_data='quicktpl:cancel')]]),
                parse_mode='Markdown'
            )
            return
        variants.append(main_comment)
        await state.update_data(
            main_comment=variants[0],
            new_template_text_variants=variants,
        )
        if len(variants) < 3:
            next_variant = len(variants) + 1
            await state.update_data(new_template_variant_index=next_variant)
            await message.answer(
                f'✅ Вариант {len(variants)} сохранён.\n\n*Вариант комментария {next_variant} из 3*\n\nОтправьте следующий вариант одним сообщением.',
                reply_markup=InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text=f'{Emoji.CANCEL} Отмена', callback_data='quicktpl:cancel')]]),
                parse_mode='Markdown'
            )
            return
        max_replies = settings.MAX_REPLIES
        await message.answer(
            f'*Ответ 1 из {max_replies}*\n\nНеобязательно. Отправьте первый ответ отдельным сообщением или пропустите этот шаг.',
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text='⏭ Без ответов', callback_data='new_template:skip_replies')], [InlineKeyboardButton(text=f'{Emoji.CANCEL} Отмена', callback_data='quicktpl:cancel')]]),
            parse_mode='Markdown'
        )
        await state.set_state(OrderStates.NEW_TEMPLATE_REPLIES_ALL)
    except Exception as e:
        logger.error(f'Error saving main comment: {e}', exc_info=True)
        await message.answer('Ошибка сохранения. Попробуйте снова.')

@router.callback_query(F.data == 'new_template:skip_replies')
async def skip_replies_new_template(callback: CallbackQuery, state: FSMContext):
    """Пропускает ввод ответов и переходит к превью."""
    try:
        state_data = await state.get_data()
        if state_data.get('template_type') == 'replies_only':
            await callback.answer('Для шаблона ответов нужен хотя бы один ответ', show_alert=True)
            return
        await callback.answer()
        await state.update_data(replies={}, reply_count=0)
        if not callback.message:
            return
        from aiogram.types import Message as MessageType
        await show_new_template_preview_from_callback(callback, state)
    except Exception as e:
        logger.error(f'Error skipping replies: {e}', exc_info=True)
        await callback.answer('Ошибка', show_alert=True)


@router.callback_query(F.data == 'new_template:finish_replies')
async def finish_replies_new_template(callback: CallbackQuery, state: FSMContext):
    """Завершает per-message ввод ответов без очистки уже собранных replies."""
    try:
        state_data = await state.get_data()
        if state_data.get('template_type') == 'replies_only' and not state_data.get('replies'):
            await callback.answer('Отправьте хотя бы один ответ', show_alert=True)
            return
        await callback.answer()
        await show_new_template_preview_from_callback(callback, state)
    except Exception as e:
        logger.error(f'Error finishing replies: {e}', exc_info=True)
        await callback.answer('Ошибка', show_alert=True)


@router.message(StateFilter(OrderStates.NEW_TEMPLATE_REPLIES_ALL))
async def process_all_replies_new_template(message: Message, state: FSMContext):
    """Собирает ответы по одному сообщению."""
    try:
        text = message.text or ''
        if text.strip() == '0' or text.strip().lower() in ['пропустить', 'skip', 'нет']:
            state_data = await state.get_data()
            existing_replies = state_data.get('replies') or {}
            if state_data.get('template_type') == 'replies_only' and not existing_replies:
                await message.answer(
                    'Для шаблона ответов нужен хотя бы один ответ.\n\nОтправьте ответ отдельным сообщением.',
                    reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                        [InlineKeyboardButton(text=f'{Emoji.CANCEL} Отмена', callback_data='quicktpl:cancel')],
                    ]),
                    parse_mode='Markdown',
                )
                return
            if existing_replies:
                await state.update_data(reply_count=len(existing_replies))
            else:
                await state.update_data(replies={}, reply_count=0)
            await show_new_template_preview(message, state)
            return
        if len(preserve_nonempty_lines(text)) > 1:
            await message.answer(
                '❌ Отправьте только один ответ одним сообщением. Следующий ответ я попрошу отдельно.',
                reply_markup=InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text='✅ Готово', callback_data='new_template:finish_replies')], [InlineKeyboardButton(text=f'{Emoji.CANCEL} Отмена', callback_data='quicktpl:cancel')]]),
                parse_mode='Markdown'
            )
            return
        reply_text = _extract_comment_variant(text)
        if reply_text is None:
            max_replies = settings.MAX_REPLIES
            await message.answer(
                '*Ответы*\n\nНе удалось распознать ответ. Отправьте один ответ отдельным сообщением или завершите ввод.',
                reply_markup=InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text='⏭ Без ответов', callback_data='new_template:skip_replies')], [InlineKeyboardButton(text=f'{Emoji.CANCEL} Отмена', callback_data='quicktpl:cancel')]]),
                parse_mode='Markdown'
            )
            return
        max_replies = settings.MAX_REPLIES
        state_data = await state.get_data()
        replies = dict(state_data.get('replies') or {})
        next_index = len(replies) + 1
        if next_index > max_replies:
            await state.update_data(reply_count=len(replies))
            await show_new_template_preview(message, state)
            return
        replies[next_index] = reply_text
        await state.update_data(replies=replies, reply_count=len(replies))
        if len(replies) >= max_replies:
            await show_new_template_preview(message, state)
            return
        await message.answer(
            f'✅ Ответ {next_index} сохранён.\n\n*Ответ {next_index + 1} из {max_replies}*\n\nОтправьте следующий ответ отдельным сообщением или нажмите «Готово».',
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text='✅ Готово', callback_data='new_template:finish_replies')], [InlineKeyboardButton(text=f'{Emoji.CANCEL} Отмена', callback_data='quicktpl:cancel')]]),
            parse_mode='Markdown'
        )
    except Exception as e:
        logger.error(f'Error processing replies: {e}', exc_info=True)
        await message.answer('❌ Ошибка сохранения ответов.\n\nПопробуйте снова:', reply_markup=InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text=f'{Emoji.CANCEL} Отмена', callback_data='quicktpl:cancel')]]))

def _build_new_template_preview_text_and_keyboard(
    text_variants: list,
    replies: dict,
    reply_count: int,
    links: list,
    likes_label: str = "выключены",
    template_type: str = "full",
    verify_main_visibility: bool = False,
    verify_reply_visibility: bool = False,
) -> tuple[str, InlineKeyboardMarkup]:
    """Общий строитель preview-текста и клавиатуры для нового шаблона/заказа."""
    replies_only = template_type == "replies_only"
    has_videos = bool(links) and not replies_only

    if has_videos:
        preview_text = f'*Проверьте заказ*\n\n🔗 Видео: *{len(links)}*\n'
    elif replies_only:
        preview_text = '*Проверьте шаблон ответов*\n\n'
    else:
        preview_text = '*Проверьте шаблон*\n\n'

    if not replies_only:
        preview_text += f'💬 Комментарий: *{len(text_variants) if text_variants else 1} вариант(а)*\n\n*Комментарий*\n'
        for idx, variant in enumerate(text_variants[:3], start=1):
            preview_text += f'{idx}. {_mono_preview(variant, 100)}\n'
        preview_text += '\n'

    if reply_count > 0:
        preview_text += f'↩️ Ответов: *{reply_count}*\n'
        for i in sorted(replies.keys())[:3]:
            preview_text += f'{i}. {_mono_preview(replies[i], 50)}\n'
        if reply_count > 3:
            preview_text += f'... и ещё {reply_count - 3}\n'
    else:
        preview_text += '↩️ Ответов: *0*\n'

    if has_videos:
        preview_text += f'\n❤️ Лайки: *{escape_markdown_simple(likes_label)}*'

    if has_videos:
        keyboard = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text='🚀 Создать заказ', callback_data='new_template:confirm')],
            [InlineKeyboardButton(text='💾 Сохранить шаблон', callback_data='new_template:save_as_template')],
            [InlineKeyboardButton(text='Изменить текст', callback_data='new_template:edit')],
            [InlineKeyboardButton(text=f'{Emoji.CANCEL} Отмена', callback_data='quicktpl:cancel')]
        ])
    else:
        keyboard = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text='💾 Сохранить шаблон', callback_data='new_template:save_as_template')],
            [InlineKeyboardButton(text='Изменить ответы' if replies_only else 'Изменить текст', callback_data='new_template:edit_replies' if replies_only else 'new_template:edit')],
            [InlineKeyboardButton(text=f'{Emoji.CANCEL} Отмена', callback_data='quicktpl:cancel')]
        ])

    if has_videos:
        keyboard = visibility_keyboard(keyboard, verify_main_visibility, verify_reply_visibility)
    return preview_text, keyboard


async def show_new_template_preview_from_callback(callback: CallbackQuery, state: FSMContext):
    """Показывает превью нового шаблона из callback."""
    try:
        state_data = await state.get_data()
        text_variants = normalize_text_variants_list(state_data.get('new_template_text_variants') or [])
        replies = state_data.get('replies', {})
        reply_count = state_data.get('reply_count', 0)
        template_type = state_data.get('template_type') or 'full'
        draft = get_draft(callback.from_user.id)

        if not callback.message:
            await callback.answer('Ошибка: сообщение не найдено', show_alert=True)
            return

        preview_text, keyboard = _build_new_template_preview_text_and_keyboard(
            text_variants,
            replies,
            reply_count,
            draft.links or [],
            likes_label=await get_likes_display_label_for_replies(reply_count),
            template_type=template_type,
            verify_main_visibility=bool(state_data.get('verify_main_visibility', False)),
            verify_reply_visibility=bool(state_data.get('verify_reply_visibility', False)),
        )
        await callback.message.edit_text(preview_text, reply_markup=keyboard, parse_mode='Markdown')
        await state.set_state(None)
    except Exception as e:
        logger.error(f'Error showing preview from callback: {e}', exc_info=True)
        await callback.answer('Ошибка', show_alert=True)


async def show_new_template_preview(message: Message, state: FSMContext):
    """Показывает превью нового шаблона в ответ на сообщение."""
    try:
        state_data = await state.get_data()
        text_variants = normalize_text_variants_list(state_data.get('new_template_text_variants') or [])
        replies = state_data.get('replies', {})
        reply_count = state_data.get('reply_count', 0)
        template_type = state_data.get('template_type') or 'full'
        draft = get_draft(message.from_user.id)

        preview_text, keyboard = _build_new_template_preview_text_and_keyboard(
            text_variants,
            replies,
            reply_count,
            draft.links or [],
            likes_label=await get_likes_display_label_for_replies(reply_count),
            template_type=template_type,
            verify_main_visibility=bool(state_data.get('verify_main_visibility', False)),
            verify_reply_visibility=bool(state_data.get('verify_reply_visibility', False)),
        )
        await message.answer(preview_text, reply_markup=keyboard, parse_mode='Markdown')
        await state.set_state(None)
    except Exception as e:
        logger.error(f'Error showing preview: {e}', exc_info=True)
        await message.answer('Ошибка. Попробуйте снова.')



@router.callback_query(F.data == 'new_template:edit')
async def edit_new_template(callback: CallbackQuery, state: FSMContext):
    """УЛУЧШЕНО: Возвращается к редактированию комментария."""
    try:
        await callback.answer()
        if not callback.message:
            return
        await callback.message.edit_text(
            '*Вариант комментария 1 из 3*\n\nОтправьте первый вариант одним сообщением.',
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text=f'{Emoji.CANCEL} Отмена', callback_data='quicktpl:cancel')]]),
            parse_mode='Markdown'
        )
        await state.update_data(
            new_template_text_variants=[],
            new_template_variant_index=1,
            main_comment=None,
        )
        await state.set_state(OrderStates.NEW_TEMPLATE_MAIN_COMMENT)
    except Exception as e:
        logger.error(f'Error editing template: {e}', exc_info=True)
        await callback.answer('Ошибка', show_alert=True)


@router.callback_query(F.data == 'new_template:edit_replies')
async def edit_replies_only_template(callback: CallbackQuery, state: FSMContext):
    """Возвращается к вводу ответов для шаблона ответов."""
    try:
        await callback.answer()
        await state.update_data(replies={}, reply_count=0, template_type='replies_only')
        await callback.message.edit_text(
            f'*Шаблон ответов*\n\n'
            f'Отправьте первый ответ отдельным сообщением. Максимум: *{settings.MAX_REPLIES}*.\n'
            'Когда ответов достаточно, нажмите «Готово».',
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text='✅ Готово', callback_data='new_template:finish_replies')],
                [InlineKeyboardButton(text='❌ Отмена', callback_data='quicktpl:cancel')],
            ]),
            parse_mode='Markdown',
        )
        await state.set_state(OrderStates.NEW_TEMPLATE_REPLIES_ALL)
    except Exception as e:
        logger.error(f'Error editing replies-only template: {e}', exc_info=True)
        await callback.answer('Ошибка', show_alert=True)

@router.callback_query(F.data == 'new_template:save_as_template')
async def save_as_template_only(callback: CallbackQuery, state: FSMContext):
    """
    ИСПРАВЛЕНО: Создаёт ТОЛЬКО шаблон (без создания заказа).
    Используется когда пользователь хочет сохранить шаблон для будущего использования.
    """
    try:
        has_access, user_data = await ensure_access(callback)
        if not has_access:
            await callback.answer('Доступ запрещён', show_alert=True)
            return
        user_id = callback.from_user.id
        user_backend_id = user_data.get('id')
        await callback.answer('Сохраняем шаблон...')
        state_data = await state.get_data()
        text_variants = normalize_text_variants_list(state_data.get('new_template_text_variants') or [])
        main_comment = text_variants[0] if text_variants else state_data.get('main_comment', '')
        replies = state_data.get('replies', {})
        reply_count = state_data.get('reply_count', 0)
        template_type = state_data.get('template_type') or 'full'
        replies_list = replies_dict_to_list(replies)
        if template_type == 'replies_only':
            if not replies_list:
                await callback.message.edit_text(
                    '❌ *Ошибка*\n\nДля шаблона ответов нужен хотя бы один ответ.',
                    reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                        [InlineKeyboardButton(text='✏️ Ввести ответы', callback_data='new_template:edit_replies')],
                        [InlineKeyboardButton(text=f'{Emoji.HOME} Меню', callback_data='menu:main')],
                    ]),
                    parse_mode='Markdown',
                )
                return
        elif not main_comment:
            await callback.message.edit_text('❌ *Ошибка*\n\nКомментарий не может быть пустым.', reply_markup=InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text='✏️ Изменить', callback_data='new_template:edit')], [InlineKeyboardButton(text=f'{Emoji.HOME} Меню', callback_data='menu:main')]]), parse_mode='Markdown')
            return
        if reply_count == 0:
            reply_mode = 'C'
        else:
            reply_mode = 'A'
        task_settings = {'reply_mode': reply_mode, 'reply_count': reply_count, 'template_type': template_type}
        task_settings['text_variants'] = [] if template_type == 'replies_only' else text_variants
        task_settings['replies'] = replies_list
        try:
            import datetime
            date_str = datetime.datetime.now().strftime('%d.%m %H:%M')
            template_name = f'Ответы {date_str}' if template_type == 'replies_only' else f'Шаблон {date_str}'
            template = await api_client.create_task_template(
                user_id=user_backend_id,
                name=template_name,
                main_comment=main_comment or '',
                text_variants=[] if template_type == 'replies_only' else text_variants,
                replies=replies_list,
                likes=0,
                settings=task_settings,
                template_type=template_type,
            )
            template_id = template.get('id')
            logger.info(f'[TEMPLATE_CREATE] User {user_id}: Created template #{template_id}')
            await state.clear()
            return_to_new_videos = state_data.get('return_to_new_videos', False)
            new_videos_creator_id = state_data.get('new_videos_creator_id')
            new_videos_batch_id = state_data.get('new_videos_batch_id')
            if return_to_new_videos and new_videos_creator_id and new_videos_batch_id:
                await callback.message.edit_text(
                    f'✅ *Шаблон сохранён*\n\n📑 Название: `{template_name}`\n{"↩️ Тип: *только ответы*" if template_type == "replies_only" else f"💬 Вариантов комментария: *{len(text_variants) if text_variants else 1}*"}\n↩️ Ответов: *{reply_count}*\n\nВозвращаемся к выбору шаблонов...',
                    parse_mode='Markdown'
                )
                await _return_to_new_videos_template_picker(
                    callback,
                    state,
                    user_backend_id=user_backend_id,
                    creator_id=int(new_videos_creator_id),
                    batch_id=int(new_videos_batch_id),
                )
            else:
                await callback.message.edit_text(
                    f'✅ *Шаблон сохранён*\n\n📑 Название: `{template_name}`\n{"↩️ Тип: *только ответы*" if template_type == "replies_only" else f"💬 Вариантов комментария: *{len(text_variants) if text_variants else 1}*"}\n↩️ Ответов: *{reply_count}*\n\nТеперь вы можете использовать этот шаблон при создании заказов.',
                    reply_markup=InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text='📑 Мои шаблоны', callback_data='menu:templates')], [InlineKeyboardButton(text=f'{Emoji.HOME} Меню', callback_data='menu:main')]]),
                    parse_mode='Markdown'
                )
        except Exception as e:
            logger.error(f'Error saving template: {e}', exc_info=True)
            error_msg = str(e)
            if len(error_msg) > 150:
                error_msg = error_msg[:150] + '...'
            await callback.message.edit_text(f'❌ *Ошибка сохранения шаблона*\n\n{error_msg}', reply_markup=InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text='🔄 Попробовать снова', callback_data='new_template:save_as_template')], [InlineKeyboardButton(text=f'{Emoji.HOME} Меню', callback_data='menu:main')]]), parse_mode='Markdown')
    except Exception as e:
        logger.error(f'Error in save_as_template_only: {e}', exc_info=True)
        await callback.answer('Ошибка', show_alert=True)

@router.callback_query(F.data == 'new_template:confirm')
async def confirm_new_template(callback: CallbackQuery, state: FSMContext):
    """Создаёт задачу с новым шаблоном."""
    try:
        has_access, user_data = await ensure_access(callback)
        if not has_access:
            await callback.answer('Доступ запрещён', show_alert=True)
            return
        user_id = callback.from_user.id
        user_backend_id = user_data.get('id')
        from aiogram.enums import ChatAction
        state_data = await state.get_data()
        text_variants = normalize_text_variants_list(state_data.get('new_template_text_variants') or [])
        main_comment = text_variants[0] if text_variants else state_data.get('main_comment', '')
        replies = state_data.get('replies', {})
        reply_count = state_data.get('reply_count', 0)
        if state_data.get('template_type') == 'replies_only':
            await callback.answer('Шаблон ответов нельзя использовать для полного заказа', show_alert=True)
            return
        return_to_new_videos = state_data.get('return_to_new_videos', False)
        draft_links = []
        if not return_to_new_videos:
            from bot.services.order_utils import validate_draft_has_links
            if not await validate_draft_has_links(callback, user_id):
                return
            draft = get_draft(user_id)
            draft_links = draft.links
            has_accounts = await api_client.has_active_accounts()
            if not has_accounts:
                await callback.message.edit_text('❌ Нет активных аккаунтов', reply_markup=InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text=f'{Emoji.HOME} Меню', callback_data='menu:main')]]))
                await callback.answer()
                return
            if not await _ensure_quick_order_pool_selected(
                callback,
                state,
                user_backend_id,
                'new_template:confirm',
            ):
                return
        await callback.answer('Создаём заказ...')
        await callback.bot.send_chat_action(user_id, ChatAction.TYPING)
        logger.info(f'[ORDER_CREATE] User {user_id} creating NEW TEMPLATE order: videos={len(draft_links)}, comment_len={len(main_comment)}, replies={reply_count}')
        replies_list = replies_dict_to_list(replies)
        reply1 = replies_list[0] if len(replies_list) >= 1 else None
        reply2 = replies_list[1] if len(replies_list) >= 2 else None
        reply3 = replies_list[2] if len(replies_list) >= 3 else None
        if reply_count == 0:
            reply_mode = 'C'
        else:
            reply_mode = 'A'
        try:
            account_pool_mode = _normalize_account_pool_mode(
                (await state.get_data()).get('account_pool_mode')
            )
            task_settings = {
                'reply_mode': reply_mode,
                'reply_count': reply_count,
                'replies': replies_list,
                'delays': True,
                'shuffle': False,
                'rotate_accounts': True,
                'origin': 'quick_order'
            }
            task_settings['verify_main_visibility'] = bool(state_data.get('verify_main_visibility', False))
            task_settings['verify_reply_visibility'] = bool(state_data.get('verify_reply_visibility', False))
            task_settings['text_variants'] = text_variants
            if not return_to_new_videos:
                task_settings['account_pool_mode'] = account_pool_mode
            if return_to_new_videos:
                import datetime
                date_str = datetime.datetime.now().strftime('%d.%m %H:%M')
                template_name = f'Шаблон {date_str}'
                task = await api_client.create_task_template(
                    user_id=user_backend_id,
                    name=template_name,
                    main_comment=main_comment,
                    text_variants=text_variants,
                    replies=replies_list,
                    likes=0,
                    settings=task_settings,
                )
                task = {'task_id': task.get('id')}
            else:
                task = await api_client.create_task(user_id=user_backend_id, main_comment=main_comment, video_urls=draft_links, text_variants=text_variants, reply1=reply1, reply2=reply2, reply3=reply3, replies=replies_list, likes=0, settings=task_settings)
        except Exception as e:
            logger.error(f'Error confirming new template: {e}', exc_info=True)
            error_text, error_keyboard = get_task_creation_error_message(e)
            await callback.message.edit_text(error_text, reply_markup=error_keyboard or InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text='◀️ Назад', callback_data='quicktpl:back_to_choice')]]), parse_mode='Markdown')
            return
        return_to_new_videos = state_data.get('return_to_new_videos', False)
        new_videos_creator_id = state_data.get('new_videos_creator_id')
        new_videos_batch_id = state_data.get('new_videos_batch_id')
        # Сохраняем количество ссылок ДО reset_draft, пока draft ещё не очищен
        draft_links_count = len(draft_links) if draft_links else 0
        reset_draft(user_id)
        await state.clear()
        task_id = task.get('task_id')
        if return_to_new_videos and new_videos_creator_id and new_videos_batch_id:
            await callback.message.edit_text(
                f'✅ *Шаблон создан*\n\nID шаблона: `{task_id}`\n\nВозвращаемся к новым видео...',
                parse_mode='Markdown'
            )
            await _return_to_new_videos_template_picker(
                callback,
                state,
                user_backend_id=user_backend_id,
                creator_id=int(new_videos_creator_id),
                batch_id=int(new_videos_batch_id),
            )
        else:
            await callback.message.edit_text(
                build_success_text(
                    task_id=task_id,
                    links_count=draft_links_count,

                    reply_count=reply_count,
                    variants_count=len(text_variants) if text_variants else 1,
                    likes_label=await get_likes_display_label_for_replies(reply_count),
                    pool_label=_account_pool_label(account_pool_mode),
                ),
                reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                    [InlineKeyboardButton(text='👁 Смотреть заказ', callback_data=f'order:detail:{task_id}')],
                    [InlineKeyboardButton(text='➕ Ещё заказ', callback_data='menu:new_order')],
                    [InlineKeyboardButton(text='🏠 Меню', callback_data='menu:main')]
                ]),
                parse_mode='Markdown'
            )
            await callback.answer()
    except Exception as e:
        logger.error(f'Error confirming new template: {e}', exc_info=True)
        error_msg = str(e)
        if 'DUPLICATE_ORDER' in error_msg:
            duplicate_msg = '⚠️ *Дубликат заказа*\n\nВы уже успешно публиковали этот комментарий под это видео.\nТикТок может склеить их или скрыть.\n\nПожалуйста, измените текст комментария (хотя бы пару слов).'
            await callback.message.edit_text(duplicate_msg, reply_markup=InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text='🔄 Изменить комментарий', callback_data='menu:new_order')], [InlineKeyboardButton(text=f'{Emoji.HOME} Меню', callback_data='menu:main')]]), parse_mode='Markdown')
        elif 'Daily video limit exceeded' in error_msg or 'limit exceeded' in error_msg.lower():
            from datetime import datetime, timezone, timedelta
            now = datetime.now(timezone.utc)
            tomorrow = now.replace(hour=0, minute=0, second=0, microsecond=0) + timedelta(days=1)
            hours_until_reset = (tomorrow - now).total_seconds() / 3600
            limit_msg = f'⚠️ *Достигнут суточный лимит видео*\n\n{error_msg[:300]}\n\nЛимит сбросится через {int(hours_until_reset)} часов.\nИли обратитесь к администратору для увеличения лимита.'
            await callback.message.edit_text(limit_msg, reply_markup=InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text=f'{Emoji.HOME} Меню', callback_data='menu:main')]]), parse_mode='Markdown')
        else:
            if len(error_msg) > 150:
                error_msg = error_msg[:150] + '...'
            await callback.message.edit_text(f'❌ Ошибка\n\n{error_msg}', reply_markup=InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text=f'{Emoji.HOME} Меню', callback_data='menu:main')]]))
        await callback.answer()

@router.callback_query(F.data.startswith('quicktpl:set_replies:'))
async def set_reply_count(callback: CallbackQuery, state: FSMContext):
    """Изменяет количество ответов для использования."""
    try:
        parts = callback.data.split(':')
        template_id = int(parts[2])
        reply_count = max(0, min(int(parts[3]), settings.MAX_REPLIES))
        user_id = callback.from_user.id
        has_access, user_data = await ensure_access(callback)
        if not has_access:
            await callback.answer('Доступ запрещён', show_alert=True)
            return
        user_backend_id = user_data.get('id')
        templates = await api_client.list_task_templates(user_backend_id)
        template = next((t for t in templates if t.get('id') == template_id), None)
        if not template:
            await callback.answer('Шаблон не найден', show_alert=True)
            return
        state_data = await state.get_data()
        await state.update_data(reply_count=reply_count)
        edited_comment = state_data.get('edited_comment')
        edited_text_variants = state_data.get('edited_text_variants')
        edited_replies = state_data.get('edited_replies') if state_data.get('edited_replies_overridden') else None
        preview_text = await _template_preview_text(
            template,
            reply_count=reply_count,
            edited_comment=edited_comment,
            edited_text_variants=edited_text_variants,
            edited_replies=edited_replies,
        )
        await callback.message.edit_text(preview_text, reply_markup=get_template_preview_keyboard(template_id, reply_count=reply_count, verify_main_visibility=bool((await state.get_data()).get("verify_main_visibility", False)), verify_reply_visibility=bool((await state.get_data()).get("verify_reply_visibility", False))), parse_mode='Markdown')
        await callback.answer()
    except Exception as e:
        logger.error(f'Error setting reply count: {e}', exc_info=True)
        await callback.answer('Ошибка', show_alert=True)

@router.callback_query(F.data.startswith('quicktpl:edit_comment:'))
async def start_edit_comment(callback: CallbackQuery, state: FSMContext):
    """Начинает редактирование основного комментария."""
    try:
        parts = callback.data.split(':')
        template_id = int(parts[2])
        reply_count = max(0, min(int(parts[3]) if len(parts) > 3 else 3, settings.MAX_REPLIES))
        has_access, user_data = await ensure_access(callback)
        if not has_access:
            await callback.answer('Доступ запрещён', show_alert=True)
            return
        state_data = await state.get_data()
        template = state_data.get('template_data', {})
        current_variants = state_data.get('edited_text_variants') or template_text_variants(template)
        current_preview = '\n'.join(
            f'{idx}. {_mono_preview(item, 120)}'
            for idx, item in enumerate(current_variants[:3], start=1)
        )
        await callback.message.edit_text(
            f'*Вариант комментария 1 из 3*\n\nТекущие варианты:\n{current_preview}\n\nОтправьте новый вариант 1 одним сообщением.',
            parse_mode='Markdown'
        )
        await state.update_data(
            template_id=template_id,
            reply_count=reply_count,
            quick_template_variant_index=1,
            edited_text_variants=[],
            edited_comment=None,
        )
        await state.set_state(OrderStates.QUICK_TEMPLATE_EDIT_COMMENT)
        await callback.answer()
    except Exception as e:
        logger.error(f'Error starting edit comment: {e}', exc_info=True)
        await callback.answer('Ошибка', show_alert=True)

@router.message(StateFilter(OrderStates.QUICK_TEMPLATE_EDIT_COMMENT))
async def save_edited_comment(message: Message, state: FSMContext):
    """Сохраняет три варианта комментария для выбранного шаблона."""
    try:
        new_comment = _extract_comment_variant(message.text)
        if new_comment is None:
            await message.answer('Отправьте один вариант отдельным сообщением. Текст внутри сообщения можно оставить в исходном виде.')
            return
        state_data = await state.get_data()
        template_id = state_data.get('template_id')
        reply_count = state_data.get('reply_count', 3)
        template = state_data.get('template_data', {})
        variant_index = int(state_data.get('quick_template_variant_index', 1))
        edited_text_variants = normalize_text_variants_list(state_data.get('edited_text_variants') or [])
        edited_replies = state_data.get('edited_replies') if state_data.get('edited_replies_overridden') else None
        if any(existing.lower() == new_comment.lower() for existing in edited_text_variants):
            await message.answer('Этот вариант уже был. Отправьте действительно другой текст.')
            return
        edited_text_variants.append(new_comment)
        if len(edited_text_variants) < 3:
            next_variant = len(edited_text_variants) + 1
            await state.update_data(
                edited_text_variants=edited_text_variants,
                edited_comment=edited_text_variants[0],
                quick_template_variant_index=next_variant,
            )
            await message.answer(
                f'✅ Вариант {variant_index} сохранён.\n\n*Вариант комментария {next_variant} из 3*\n\nОтправьте следующий вариант одним сообщением.',
                parse_mode='Markdown'
            )
            return
        await state.update_data(
            edited_text_variants=edited_text_variants,
            edited_comment=edited_text_variants[0],
        )
        preview_text = await _template_preview_text(
            template,
            reply_count=reply_count,
            edited_text_variants=edited_text_variants,
            edited_replies=edited_replies,
        )
        await message.answer(preview_text, reply_markup=get_template_preview_keyboard(template_id, reply_count=reply_count, verify_main_visibility=bool((await state.get_data()).get("verify_main_visibility", False)), verify_reply_visibility=bool((await state.get_data()).get("verify_reply_visibility", False))), parse_mode='Markdown')
        await state.set_state(None)
    except Exception as e:
        logger.error(f'Error saving edited comment: {e}', exc_info=True)
        await message.answer('Ошибка сохранения. Попробуйте снова.')

@router.callback_query(F.data.startswith('quicktpl:edit_replies:'))
async def start_edit_replies(callback: CallbackQuery, state: FSMContext):
    """Начинает редактирование ответов."""
    try:
        parts = callback.data.split(':')
        template_id = int(parts[2])
        reply_count = max(0, min(int(parts[3]) if len(parts) > 3 else 3, settings.MAX_REPLIES))
        has_access, user_data = await ensure_access(callback)
        if not has_access:
            await callback.answer('Доступ запрещён', show_alert=True)
            return
        state_data = await state.get_data()
        template = state_data.get('template_data', {})
        edited_replies = state_data.get('edited_replies', {})
        current_replies = [edited_replies[idx] for idx in sorted(edited_replies.keys()) if edited_replies.get(idx)] or template_replies(template)
        text = f'*Ответ 1 из {settings.MAX_REPLIES}*\n\nОтправьте первый ответ отдельным сообщением или очистите ответы.\n\n'
        text += f'Текущие:\n'
        for idx, reply in enumerate(current_replies, start=1):
            text += f'{idx}. {_mono_preview(reply, 120)}\n'
        await callback.message.edit_text(
            text,
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text='🧹 Очистить ответы', callback_data='quicktpl:finish_edit_replies')],
                [InlineKeyboardButton(text=f'{Emoji.CANCEL} Отмена', callback_data=f'quicktpl:edit:{template_id}')]
            ]),
            parse_mode='Markdown'
        )
        await state.update_data(
            template_id=template_id,
            reply_count=reply_count,
            edited_replies={},
            edited_replies_overridden=True,
        )
        await state.set_state(OrderStates.QUICK_TEMPLATE_EDIT_REPLIES)
        await callback.answer()
    except Exception as e:
        logger.error(f'Error starting edit replies: {e}', exc_info=True)
        await callback.answer('Ошибка', show_alert=True)

@router.message(StateFilter(OrderStates.QUICK_TEMPLATE_EDIT_REPLIES))
async def save_replies(message: Message, state: FSMContext):
    """Собирает новые ответы шаблона по одному сообщению."""
    try:
        text = message.text or ''
        state_data = await state.get_data()
        edited_replies = dict(state_data.get('edited_replies') or {})
        stripped = text.strip()
        if stripped == '0' or stripped.lower() in {'пропустить', 'skip', 'нет', 'очистить'}:
            await state.update_data(edited_replies=edited_replies, edited_replies_overridden=True, reply_count=len(edited_replies))
            await finish_editing_replies(message, state)
            return
        if len(preserve_nonempty_lines(text)) > 1:
            await message.answer(
                '❌ Отправьте только один ответ одним сообщением. Следующий ответ я попрошу отдельно.',
                reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                    [InlineKeyboardButton(text='✅ Готово', callback_data='quicktpl:finish_edit_replies')],
                    [InlineKeyboardButton(text=f'{Emoji.CANCEL} Отмена', callback_data='quicktpl:back_to_choice')]
                ]),
                parse_mode='Markdown'
            )
            return
        reply_text = _extract_comment_variant(text)
        if reply_text is None:
            await message.answer(
                '*Ответы*\n\nНе удалось распознать ответ. Отправьте один ответ отдельным сообщением или завершите ввод.',
                reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                    [InlineKeyboardButton(text='✅ Готово', callback_data='quicktpl:finish_edit_replies')],
                    [InlineKeyboardButton(text=f'{Emoji.CANCEL} Отмена', callback_data='quicktpl:back_to_choice')]
                ]),
                parse_mode='Markdown'
            )
            return
        next_index = len(edited_replies) + 1
        if next_index > settings.MAX_REPLIES:
            await state.update_data(edited_replies=edited_replies, edited_replies_overridden=True, reply_count=len(edited_replies))
            await finish_editing_replies(message, state)
            return
        edited_replies[next_index] = reply_text
        await state.update_data(edited_replies=edited_replies, edited_replies_overridden=True, reply_count=len(edited_replies))
        if len(edited_replies) >= settings.MAX_REPLIES:
            await finish_editing_replies(message, state)
            return
        await message.answer(
            f'✅ Ответ {next_index} сохранён.\n\n*Ответ {next_index + 1} из {settings.MAX_REPLIES}*\n\nОтправьте следующий ответ отдельным сообщением или нажмите «Готово».',
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text='✅ Готово', callback_data='quicktpl:finish_edit_replies')],
                [InlineKeyboardButton(text=f'{Emoji.CANCEL} Отмена', callback_data='quicktpl:back_to_choice')]
            ]),
            parse_mode='Markdown'
        )
    except Exception as e:
        logger.error(f'Error saving replies: {e}', exc_info=True)
        await message.answer('Ошибка сохранения. Попробуйте снова.')

@router.callback_query(F.data == 'quicktpl:finish_edit_replies', StateFilter(OrderStates.QUICK_TEMPLATE_EDIT_REPLIES))
async def finish_edit_replies_callback(callback: CallbackQuery, state: FSMContext):
    """Завершает per-message редактирование ответов."""
    try:
        await callback.answer()
        if not callback.message:
            return
        state_data = await state.get_data()
        edited_replies = dict(state_data.get('edited_replies') or {})
        await state.update_data(edited_replies=edited_replies, edited_replies_overridden=True, reply_count=len(edited_replies))
        await finish_editing_replies(callback.message, state)
    except Exception as e:
        logger.error(f'Error finishing edit replies: {e}', exc_info=True)
        await callback.answer('Ошибка', show_alert=True)


async def finish_editing_replies(message: Message, state: FSMContext):
    """Заканчивает редактирование ответов и показывает предпросмотр."""
    try:
        state_data = await state.get_data()
        template_id = state_data.get('template_id')
        reply_count = state_data.get('reply_count', 3)
        template = state_data.get('template_data', {})
        edited_comment = state_data.get('edited_comment')
        edited_text_variants = state_data.get('edited_text_variants')
        edited_replies = state_data.get('edited_replies', {})
        preview_text = await _template_preview_text(
            template,
            reply_count=reply_count,
            edited_comment=edited_comment,
            edited_text_variants=edited_text_variants,
            edited_replies=edited_replies,
        )
        await message.answer(preview_text, reply_markup=get_template_preview_keyboard(template_id, reply_count=reply_count, verify_main_visibility=bool((await state.get_data()).get("verify_main_visibility", False)), verify_reply_visibility=bool((await state.get_data()).get("verify_reply_visibility", False))), parse_mode='Markdown')
        await state.set_state(None)
    except Exception as e:
        logger.error(f'Error finishing edit replies: {e}', exc_info=True)
        await message.answer('Ошибка. Попробуйте снова.')

@router.callback_query(F.data.startswith('quicktpl:confirm:'))
async def confirm_template(callback: CallbackQuery, state: FSMContext):
    """Подтверждает выбор шаблона и создаёт задачу."""
    try:
        parts = callback.data.split(':')
        template_id = int(parts[2])
        reply_count = max(0, min(int(parts[3]) if len(parts) > 3 else 3, settings.MAX_REPLIES))
        user_id = callback.from_user.id
        has_access, user_data = await ensure_access(callback)
        if not has_access:
            await callback.answer('Доступ запрещён', show_alert=True)
            return
        user_backend_id = user_data.get('id')
        from aiogram.enums import ChatAction
        draft_preview = get_draft(user_id)
        logger.info(f'[ORDER_CREATE] User {user_id} confirming EXISTING TEMPLATE {template_id}: videos={len(draft_preview.links)}')
        state_data = await state.get_data()
        template = state_data.get('template_data', {})
        edited_comment = state_data.get('edited_comment')
        edited_replies = state_data.get('edited_replies', {})
        edited_text_variants = normalize_text_variants_list(state_data.get('edited_text_variants') or [])
        base_text_variants = edited_text_variants or template_text_variants(template)
        main_comment = base_text_variants[0] if base_text_variants else (edited_comment if edited_comment is not None else template.get('main_comment', ''))
        template_settings = template.get('settings', {})
        base_replies = replies_dict_to_list(edited_replies or {}) if state_data.get('edited_replies_overridden') else template_replies(template)
        selected_replies = base_replies[: min(reply_count, settings.MAX_REPLIES)]
        reply1 = selected_replies[0] if len(selected_replies) >= 1 else None
        reply2 = selected_replies[1] if len(selected_replies) >= 2 else None
        reply3 = selected_replies[2] if len(selected_replies) >= 3 else None
        from bot.services.order_utils import validate_draft_has_links
        if not await validate_draft_has_links(callback, user_id):
            return
        draft = get_draft(user_id)
        has_accounts = await api_client.has_active_accounts()
        if not has_accounts:
            await callback.message.edit_text('❌ Нет активных аккаунтов', reply_markup=InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text=f'{Emoji.HOME} Меню', callback_data='menu:main')]]))
            await callback.answer()
            return
        if not await _ensure_quick_order_pool_selected(
            callback,
            state,
            user_backend_id,
            callback.data,
        ):
            return
        await callback.answer('Создаём заказ...')
        await callback.bot.send_chat_action(user_id, ChatAction.TYPING)
        account_pool_mode = _normalize_account_pool_mode(
            (await state.get_data()).get('account_pool_mode')
        )
        task_settings = {
            'reply_mode': template.get('settings', {}).get('reply_mode', 'A'),
            'reply_count': len(selected_replies),
            'replies': selected_replies,
            'delays': template.get('settings', {}).get('delays', True),
            'shuffle': template.get('settings', {}).get('shuffle', False),
            'rotate_accounts': template.get('settings', {}).get('rotate_accounts', True),
            'origin': 'quick_order',
            'account_pool_mode': account_pool_mode,
            'verify_main_visibility': bool(state_data.get('verify_main_visibility', False)),
            'verify_reply_visibility': bool(state_data.get('verify_reply_visibility', False)),
            # Привязка к шаблону — нужна для деталей заказа / повтора / аналитики.
            'template_id': template_id,
            'template_name': template.get('name') or 'Без названия',
        }
        text_variants = normalize_text_variants_list(base_text_variants)
        if len(text_variants) < 3:
            raise APIRequestError("Нужно минимум 3 варианта комментария в шаблоне.")
        try:
            task = await api_client.create_task(user_id=user_backend_id, main_comment=main_comment, video_urls=draft.links, text_variants=text_variants, reply1=reply1, reply2=reply2, reply3=reply3, replies=selected_replies, likes=0, settings=task_settings)
        except Exception as e:
            logger.error(f'Error confirming template: {e}', exc_info=True)
            error_text, error_keyboard = get_task_creation_error_message(e)
            await callback.message.edit_text(error_text, reply_markup=error_keyboard or InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text='◀️ Назад', callback_data=f'quicktpl:edit:{template_id}')]]), parse_mode='Markdown')
            return
        reset_draft(user_id)
        await state.clear()
        task_id = task.get('task_id')
        if task.get('duplicate'):
            logger.info(f'[ORDER_CREATE] User {user_id}: Reused existing task #{task_id} (duplicate/idempotency)')
        else:
            logger.info(f'[ORDER_CREATE] User {user_id}: Created task #{task_id} successfully')
        from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup
        await callback.message.edit_text(
            build_success_text(
                task_id=task_id,
                links_count=len(draft.links),
                reply_count=len(selected_replies),
                variants_count=len(text_variants) if text_variants else 1,
                likes_label=await get_likes_display_label_for_replies(selected_replies),
                template_name=template.get('name', 'Без названия'),
                pool_label=_account_pool_label(account_pool_mode),
            ),
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text='👁 Смотреть заказ', callback_data=f'order:detail:{task_id}')],
                [InlineKeyboardButton(text='➕ Ещё заказ', callback_data='menu:new_order')],
                [InlineKeyboardButton(text='🏠 Меню', callback_data='menu:main')]
            ]),
            parse_mode='Markdown'
        )
        await callback.answer()
    except Exception as e:
        logger.error(f'Error confirming template: {e}', exc_info=True)
        error_text, error_keyboard = get_task_creation_error_message(e)
        from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup
        error_msg = str(e)
        if 'DUPLICATE_ORDER' in error_msg:
            duplicate_msg = '⚠️ *Дубликат заказа*\n\nВы уже успешно публиковали этот комментарий под это видео.\nТикТок может склеить их или скрыть.\n\nПожалуйста, измените текст комментария (хотя бы пару слов).'
            await callback.message.edit_text(duplicate_msg, reply_markup=InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text='🔄 Изменить комментарий', callback_data='menu:new_order')], [InlineKeyboardButton(text=f'{Emoji.HOME} Меню', callback_data='menu:main')]]), parse_mode='Markdown')
        else:
            await callback.message.edit_text(error_text, reply_markup=error_keyboard, parse_mode='Markdown')
        await callback.answer()

@router.callback_query(F.data.startswith(f'{QUICK_POOL_CALLBACK_PREFIX}:'))
async def quick_order_select_pool(callback: CallbackQuery, state: FSMContext):
    """Stores account-pool choice and resumes the interrupted quick-order submit."""
    try:
        if not callback.message:
            await callback.answer('Ошибка: сообщение не найдено', show_alert=True)
            return

        parts = (callback.data or '').split(':', 2)
        if len(parts) != 3:
            await callback.answer('Ошибка выбора пула', show_alert=True)
            return

        mode = _normalize_account_pool_mode(parts[1])
        continue_callback_data = parts[2]

        if mode == ACCOUNT_POOL_MODE_PERSONAL:
            user_backend_id = await get_user_backend_id(callback.from_user.id)
            if not user_backend_id:
                await callback.answer('Пользователь не найден', show_alert=True)
                return
            try:
                pool_summary = await api_client.get_personal_pool_summary(user_backend_id)
            except Exception as exc:
                logger.warning(f'Quick order: failed to validate personal pool selection: {exc}')
                await callback.answer('Не удалось проверить личные аккаунты', show_alert=True)
                return
            if _personal_pool_usable_count(pool_summary) <= 0:
                await callback.answer('В личном пуле нет доступных аккаунтов', show_alert=True)
                return

        await state.update_data(account_pool_mode=mode)
        await callback.answer(f'Выбран пул: {_account_pool_label(mode)}', show_alert=False)

        resume_callback = _QuickPoolResumeCallback(callback, continue_callback_data)
        if continue_callback_data == 'new_template:confirm':
            await confirm_new_template(resume_callback, state)
            return
        if continue_callback_data.startswith('quicktpl:confirm:'):
            await confirm_template(resume_callback, state)
            return

        logger.warning(f'Quick order: unknown pool continuation {continue_callback_data!r}')
        await callback.answer('Не удалось продолжить создание заказа', show_alert=True)
    except Exception as e:
        logger.error(f'Error selecting quick-order account pool: {e}', exc_info=True)
        try:
            await callback.answer('Ошибка', show_alert=True)
        except Exception:
            pass

@router.callback_query(F.data == 'quicktpl:noop')
async def noop_handler(callback: CallbackQuery):
    """Заглушка для кнопок, которые не должны ничего делать."""
    await callback.answer()

@router.callback_query(F.data == 'quicktpl:back_to_choice')
async def back_to_template_choice(callback: CallbackQuery, state: FSMContext):
    """Возврат к выбору шаблона с сохранением прогресса."""
    # Stop Telegram's loading spinner before backend/template requests. If the
    # callback already expired, navigation can still continue via message edit.
    await safe_callback_answer(callback)
    try:
        user_id = callback.from_user.id
        user_backend_id = await get_user_backend_id(user_id)
        if not user_backend_id:
            await safe_callback_answer(callback, 'Ошибка загрузки данных', show_alert=True)
            return
        state_data = await state.get_data()
        current_comment = state_data.get('main_comment')
        current_variants = normalize_text_variants_list(state_data.get('new_template_text_variants') or [])
        current_page = state_data.get('template_page', 0)
        if current_variants:
            draft = get_draft(user_id)
            draft.text_variants = current_variants
            draft.comment = current_variants[0]
            save_draft(user_id, draft)
            logger.info(f'Saved draft text variants for user {user_id} before going back')
        elif current_comment:
            draft = get_draft(user_id)
            draft.comment = current_comment
            save_draft(user_id, draft)
            logger.info(f'Saved draft comment for user {user_id} before going back')
        await state.clear()
        templates = await api_client.list_task_templates(user_backend_id)
        templates = filter_full_templates(templates)
        if not templates:
            await callback.message.edit_text('У вас пока нет шаблонов.')
            return
        draft = get_draft(user_id)
        links_count = len(draft.links) if draft.links else 0
        await state.update_data(templates=templates, template_page=current_page)
        keyboard, page, total_pages = get_template_choice_keyboard(templates, page=current_page)
        page_info = f' (стр. {page + 1}/{total_pages})' if total_pages > 1 else ''
        await callback.message.edit_text(f'Найдено ссылок: {links_count}\n\nВыберите шаблон комментария{page_info}:', reply_markup=keyboard, parse_mode='Markdown')
    except Exception as e:
        logger.error(f'Error going back to template choice: {e}', exc_info=True)
        await safe_callback_answer(callback, 'Ошибка', show_alert=True)

@router.callback_query(F.data == 'quicktpl:cancel')
async def cancel_template_choice(callback: CallbackQuery, state: FSMContext):
    """Отменяет выбор/создание шаблона."""
    try:
        user_id = callback.from_user.id
        state_data = await state.get_data()
        return_to_new_videos = state_data.get('return_to_new_videos', False)
        new_videos_creator_id = state_data.get('new_videos_creator_id')
        new_videos_batch_id = state_data.get('new_videos_batch_id')
        reset_draft(user_id)
        await state.clear()
        if return_to_new_videos and new_videos_creator_id and new_videos_batch_id:
            user_backend_id = await get_user_backend_id(callback.from_user.id)
            if not user_backend_id:
                await callback.answer('Пользователь не найден', show_alert=True)
                return
            await _return_to_new_videos_template_picker(
                callback,
                state,
                user_backend_id=user_backend_id,
                creator_id=int(new_videos_creator_id),
                batch_id=int(new_videos_batch_id),
            )
        else:
            await callback.message.edit_text('❌ *Отменено*', reply_markup=InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text=f'{Emoji.HOME} Главное меню', callback_data='menu:main')]]), parse_mode='Markdown')
            await callback.answer()
    except Exception as e:
        logger.error(f'Error cancelling template choice: {e}', exc_info=True)
        await callback.answer('Ошибка', show_alert=True)

@router.message(F.text, ~StateFilter(OrderStates.NEW_TEMPLATE_MAIN_COMMENT), ~StateFilter(OrderStates.NEW_TEMPLATE_REPLIES_ALL), ~StateFilter(OrderStates.QUICK_TEMPLATE_EDIT_COMMENT), ~StateFilter(OrderStates.QUICK_TEMPLATE_EDIT_REPLIES), ~StateFilter(OrderStates.INPUT_MAIN_COMMENT), ~StateFilter(OrderStates.INPUT_LINKS), ~StateFilter(OrderStates.CHOOSE_TEMPLATE))
async def handle_link_message(message: Message, state: FSMContext):
    """
    Обрабатывает сообщение с ссылкой TikTok.
    Если найдена ссылка, автоматически предлагает выбрать шаблон.
    Работает "из воздуха" - ловит любые сообщения с TikTok-ссылками.
    """
    try:
        if message.text and message.text.startswith('/'):
            return
        if any(
            getattr(message, attr, None)
            for attr in ("forward_origin", "forward_from", "forward_from_chat", "forward_sender_name", "forward_date")
        ):
            logger.debug("Skipping quick link auto-flow for forwarded message")
            return
        current_state = await state.get_state()
        if current_state:
            state_name = str(current_state)
            skip_states = ['NEW_TEMPLATE', 'EDIT_TEMPLATE', 'INPUT_MAIN_COMMENT', 'INPUT_REPLY', 'CHOOSE_TEMPLATE', 'INPUT_LINKS', 'CREATING_TEMPLATE', 'EDITING_TEMPLATE', 'INPUT_COMMENT', 'REPLIES_ALL', 'INPUT_TEMPLATE_NAME']
            if any((x in state_name for x in skip_states)):
                logger.debug(f'Skipping handle_link_message for state: {state_name}')
                return
        links = parse_links(message.text or '')
        if not links:
            return
        valid_links = [link for link in links if validate_video_url(link)]
        if len(valid_links) == 0:
            await message.answer(f'❌ *Ссылки не валидны*\n\nПроверьте формат и попробуйте снова.\n\n*Правильный формат:*\n`https://www.tiktok.com/@user/video/123`\n`https://vm.tiktok.com/xxxxx`', parse_mode='Markdown')
            return
        if len(valid_links) != len(links):
            invalid_count = len(links) - len(valid_links)
            await message.answer(f'⚠️ *Внимание*\n\nНайдено {invalid_count} невалидных ссылок.\nОни будут проигнорированы.\n\n✅ Сохранено: {len(valid_links)} ссылок', parse_mode='Markdown')
        links = valid_links
        logger.info(f'Found TikTok links in message from user {message.from_user.id}: {len(links)} links')
        has_access, user_data = await ensure_access(message)
        if not has_access:
            await message.answer(f'*Доступ закрыт*\n\nСтоимость: {settings.SUBSCRIPTION_PRICE}\nДля активации напишите владельцу', reply_markup=get_access_denied_keyboard(), parse_mode='Markdown')
            return
        user_id = message.from_user.id
        user_backend_id = user_data.get('id')
        draft = get_draft(user_id)
        set_links(user_id, links)
        try:
            templates = await api_client.list_task_templates(user_backend_id)
            templates = filter_full_templates(templates)
        except Exception as e:
            logger.error(f'Error fetching templates: {e}')
            await message.answer('Ошибка загрузки шаблонов. Попробуйте позже.')
            return
        if not templates:
            await message.answer(f'🔗 {len(links)} ссылок\n\nНет шаблонов — создайте первый!', reply_markup=InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text='➕ Создать шаблон', callback_data='template:create_new'), InlineKeyboardButton(text=f'{Emoji.CANCEL} Отмена', callback_data='menu:main')]]))
            return
        await state.update_data(templates=templates, template_page=0)
        keyboard, _, _ = get_template_choice_keyboard(templates, page=0)
        await message.answer(f'Найдено ссылок: {len(links)}\n\nВыберите шаблон комментария:', reply_markup=keyboard, parse_mode='Markdown')
    except Exception as e:
        logger.error(f'Error handling link message: {e}', exc_info=True)
