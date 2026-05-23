from aiogram import Router, F
from aiogram.types import Message, CallbackQuery, InlineKeyboardMarkup, InlineKeyboardButton
from aiogram.fsm.context import FSMContext
from aiogram.filters import StateFilter
from bot.services.drafts import (
    get_draft, set_links, set_comment, get_last_comment,
    get_recent_links, reset_draft, save_draft
)
from bot.services.validators import parse_links
from bot.services.api_client import api_client
from bot.services.subscription_service import check_user_access
from bot.services.user_utils import get_user_backend_id
from bot.services.error_handlers import get_task_creation_error_message
from bot.services.formatting import Emoji
from bot.services.likes_ui import LIKES_DISABLED_LABEL
from bot.services.template_utils import normalize_text_variants_list
from bot.keyboards.composer import get_composer_keyboard, format_composer_summary
from bot.keyboards.common import get_main_menu_keyboard, get_access_denied_keyboard
# ИСПРАВЛЕНО: Используем отдельные состояния для композера, чтобы избежать конфликта с order_wizard
from bot.state.composer_states import ComposerStates
from bot.config import load_config
from config.settings import get_settings
import logging

logger = logging.getLogger("bot.composer")
router = Router()
config = load_config()
settings = get_settings()


async def _format_composer_summary(draft) -> str:
    return format_composer_summary(draft, likes_label=LIKES_DISABLED_LABEL)


def _personal_pool_usable_count(summary: dict | None) -> int:
    if not isinstance(summary, dict):
        return 0
    if "available_main_now" in summary:
        return max(0, int(summary.get("available_main_now") or 0))
    return max(0, int(summary.get("active") or 0) + int(summary.get("error") or 0))


async def ensure_access(callback: CallbackQuery) -> tuple[bool, dict | None]:
    """Проверяет доступ пользователя перед выполнением действия."""
    try:
        if not callback.message:
            await callback.answer("Ошибка: сообщение не найдено", show_alert=True)
            return False, None
        
        user_id = callback.from_user.id
        user_data = await api_client.sync_telegram(user_id, callback.from_user.username)
        user_backend_id = user_data.get("id")
        role = user_data.get("role", "user")
        has_access = await check_user_access(user_id, user_backend_id, role)
        
        if not has_access:
            from bot.services.subscription_service import check_user_blocked
            is_blocked, _ = await check_user_blocked(user_id, user_backend_id)

            from bot.handlers.menu import _build_access_denied_text
            await callback.message.edit_text(
                _build_access_denied_text(blocked=is_blocked),
                reply_markup=get_access_denied_keyboard(),
                parse_mode="Markdown"
            )
            await callback.answer("Доступ запрещён", show_alert=True)
            return False, None

        return True, user_data
    except Exception as e:
        logger.error(f"Error checking access in composer: {e}", exc_info=True)
        try:
            await callback.answer("Ошибка проверки доступа", show_alert=True)
        except Exception:
            pass
        return False, None


# УДАЛЕНО: обработчик menu:new_order перенесён в order_wizard.py
# Весь код удалён, так как функционал реализован в order_wizard.py


@router.callback_query(F.data.startswith("composer:input_links"))
async def input_links_handler(callback: CallbackQuery, state: FSMContext):
    """Запрашивает ввод ссылок."""
    await callback.answer()
    if not callback.message:
        await callback.answer("Ошибка: сообщение не найдено", show_alert=True)
        return
    await callback.message.answer("Отправьте ссылки TikTok (по одной на строку):")
    await state.set_state(ComposerStates.INPUT_LINKS)


# УДАЛЕНО: обработчики загрузки файлов (не используются)


@router.message(StateFilter(ComposerStates.INPUT_LINKS), ~F.document)
async def process_links_input(message: Message, state: FSMContext):
    """Обрабатывает ввод ссылок."""
    user_id = message.from_user.id
    text = message.text or ""

    links = parse_links(text)

    if not links:
        await message.answer(
            "Не удалось распознать ссылки TikTok. Попробуйте ещё раз или используйте кнопку 'Загрузить .txt'."
        )
        return

    set_links(user_id, links)
    draft = get_draft(user_id)

    summary = await _format_composer_summary(draft)
    await message.answer(
        summary,
        reply_markup=get_composer_keyboard(draft),
        parse_mode="Markdown"
    )
    await state.clear()


@router.callback_query(F.data == "composer:recent_links")
async def show_recent_links(callback: CallbackQuery, state: FSMContext):
    """Показывает недавние наборы ссылок."""
    user_id = callback.from_user.id
    recent = get_recent_links(user_id)

    if not recent:
        await callback.answer("Нет недавних наборов ссылок", show_alert=True)
        return

    # Создаём клавиатуру с недавними наборами
    keyboard = []
    for idx, links_text in enumerate(recent[:5]):  # Максимум 5
        links_preview = links_text[:50] + "..." if len(links_text) > 50 else links_text
        keyboard.append([
            InlineKeyboardButton(
                text=f"Набор {idx+1}: {links_preview}",
                callback_data=f"composer:select_recent:{idx}"
            )
        ])

    keyboard.append([InlineKeyboardButton(text=f"{Emoji.CANCEL} Отмена", callback_data="composer:cancel")])

    await callback.message.edit_text(
        "Выберите недавний набор ссылок:",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=keyboard)
    )
    await callback.answer()


@router.callback_query(F.data.startswith("composer:select_recent:"))
async def select_recent_links(callback: CallbackQuery):
    """Выбирает набор недавних ссылок."""
    user_id = callback.from_user.id
    idx = int(callback.data.split(":")[-1])
    recent = get_recent_links(user_id)

    if idx >= len(recent):
        await callback.answer("Ошибка выбора", show_alert=True)
        return

    # Парсим ссылки из текста
    links = parse_links(recent[idx])
    set_links(user_id, links)
    draft = get_draft(user_id)

    summary = await _format_composer_summary(draft)
    await callback.message.edit_text(
        summary,
        reply_markup=get_composer_keyboard(draft),
        parse_mode="Markdown"
    )
    await callback.answer()


@router.callback_query(F.data == "composer:clear_links")
async def clear_links(callback: CallbackQuery):
    """Очищает ссылки в черновике."""
    user_id = callback.from_user.id
    draft = get_draft(user_id)
    draft.links = []

    summary = await _format_composer_summary(draft)
    await callback.message.edit_text(
        summary,
        reply_markup=get_composer_keyboard(draft),
        parse_mode="Markdown"
    )
    await callback.answer("Ссылки очищены")


@router.callback_query(F.data.startswith("composer:preset:"))
async def apply_preset(callback: CallbackQuery):
    """Применяет пресет."""
    preset_name = callback.data.split(":")[-1]
    user_id = callback.from_user.id
    draft = get_draft(user_id)

    # Пресеты комментариев (настройки лайков и режимов удалены)
    presets = {
        "promo": {
            "comment": "Отличный контент! 🔥"
        },
        "announce": {
            "comment": "Интересно! Буду следить 👀"
        },
        "soft": {
            "comment": "Интересное видео, спасибо за контент"
        }
    }

    if preset_name in presets:
        preset = presets[preset_name]
        draft.comment = preset["comment"]
        # Лайки и режим ответов устанавливаются автоматически (фиксированные значения)
        draft.likes = 0
        draft.reply_mode = "C"  # Без ответов (фиксировано)

    summary = await _format_composer_summary(draft)
    await callback.message.edit_text(
        summary,
        reply_markup=get_composer_keyboard(draft),
        parse_mode="Markdown"
    )
    preset_names = {"promo": "Промо", "announce": "Анонс", "soft": "Мягкий"}
    preset_display = preset_names.get(preset_name, preset_name)
    await callback.answer(f"Пресет '{preset_display}' применён")


@router.callback_query(F.data.startswith("composer:comment:input"))
async def input_comment_handler(callback: CallbackQuery, state: FSMContext):
    """Запрашивает ввод комментария."""
    await callback.message.answer(
        "Отправьте текст основного комментария:"
    )
    # КРИТИЧНО: Устанавливаем состояние ПЕРЕД тем как пользователь отправит сообщение
    await state.set_state(ComposerStates.INPUT_MAIN_COMMENT)
    logger.debug(f"State set to INPUT_MAIN_COMMENT for user {callback.from_user.id}")
    await callback.answer()


@router.callback_query(F.data.startswith("composer:comment:templates"))
async def show_comment_templates(callback: CallbackQuery):
    """Показывает шаблоны комментариев."""
    user_id = callback.from_user.id

    try:
        user_backend_id = await get_user_backend_id(user_id)
        if not user_backend_id:
            await callback.answer("Ошибка", show_alert=True)
            return

        templates = await api_client.list_comment_templates(user_backend_id, type="main")

        if not templates:
            await callback.answer("Нет шаблонов комментариев", show_alert=True)
            return

        keyboard = []
        for template in templates[:10]:  # Максимум 10
            name = template.get("name", "Без имени")
            keyboard.append([
                InlineKeyboardButton(
                    text=name,
                    callback_data=f"composer:select_template:{template.get('id')}"
                )
            ])

        keyboard.append([InlineKeyboardButton(text=f"{Emoji.CANCEL} Отмена", callback_data="composer:cancel")])

        await callback.message.edit_text(
            "Выберите шаблон комментария:",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=keyboard)
        )
        await callback.answer()

    except Exception as e:
        logger.error(f"Error loading templates: {e}")
        await callback.answer("Ошибка загрузки шаблонов", show_alert=True)


@router.callback_query(F.data.startswith("composer:select_template:"))
async def select_template(callback: CallbackQuery):
    """Выбирает шаблон комментария."""
    template_id = int(callback.data.split(":")[-1])
    user_id = callback.from_user.id

    try:
        user_backend_id = await get_user_backend_id(user_id)
        if not user_backend_id:
            await callback.answer("Ошибка", show_alert=True)
            return

        templates = await api_client.list_comment_templates(user_backend_id, type="main")
        template = next((t for t in templates if t.get("id") == template_id), None)

        if template:
            set_comment(user_id, template.get("text", ""))
            draft = get_draft(user_id)

            summary = await _format_composer_summary(draft)
            await callback.message.edit_text(
                summary,
                reply_markup=get_composer_keyboard(draft),
                parse_mode="Markdown"
            )
            await callback.answer()

    except Exception as e:
        logger.error(f"Error selecting template: {e}")
        await callback.answer("Ошибка", show_alert=True)


@router.message(StateFilter(ComposerStates.INPUT_MAIN_COMMENT))
async def process_comment_input(message: Message, state: FSMContext):
    """Обрабатывает ввод комментария."""
    # КРИТИЧНО: Проверяем, что состояние установлено правильно
    current_state = await state.get_state()
    logger.debug(f"process_comment_input called for user {message.from_user.id}, state: {current_state}")
    
    user_id = message.from_user.id
    text = message.text or ""

    if not text.strip():
        await message.answer("Комментарий не может быть пустым. Попробуйте ещё раз.")
        return

    set_comment(user_id, text)
    draft = get_draft(user_id)

    summary = await _format_composer_summary(draft)
    await message.answer(
        summary,
        reply_markup=get_composer_keyboard(draft),
        parse_mode="Markdown"
    )
    await state.clear()


@router.callback_query(F.data.startswith("composer:comment:last"))
async def use_last_comment(callback: CallbackQuery):
    """Использует последний комментарий."""
    user_id = callback.from_user.id
    last = get_last_comment(user_id)

    if not last:
        await callback.answer("Нет последнего комментария", show_alert=True)
        return

    set_comment(user_id, last)
    draft = get_draft(user_id)

    summary = await _format_composer_summary(draft)
    await callback.message.edit_text(
        summary,
        reply_markup=get_composer_keyboard(draft),
        parse_mode="Markdown"
    )
    await callback.answer()


@router.callback_query(F.data == "composer:info")
async def show_info(callback: CallbackQuery):
    """Показывает информацию о фиксированных настройках."""
    await callback.answer(
        f"Настройки фиксированы:\n"
        f"❤️ Лайки: {LIKES_DISABLED_LABEL}\n"
        f"💬 Режим: A (автоматический)",
        show_alert=True
    )


@router.callback_query(F.data.startswith("composer:toggle:"))
async def toggle_setting(callback: CallbackQuery):
    """Переключает настройку."""
    setting = callback.data.split(":")[-1]
    user_id = callback.from_user.id
    draft = get_draft(user_id)

    if setting == "delays":
        draft.delays = not draft.delays
    elif setting == "shuffle":
        draft.shuffle = not draft.shuffle
    elif setting == "rotate":
        draft.rotate_accounts = not draft.rotate_accounts

    save_draft(user_id, draft)
    summary = await _format_composer_summary(draft)
    await callback.message.edit_text(
        summary,
        reply_markup=get_composer_keyboard(draft),
        parse_mode="Markdown"
    )
    await callback.answer()


@router.callback_query(F.data == "composer:toggle_pool")
async def toggle_account_pool(callback: CallbackQuery):
    """Переключает пул аккаунтов в composer с проверкой личного пула."""
    user_id = callback.from_user.id
    draft = get_draft(user_id)

    if draft.account_pool_mode == "personal":
        draft.account_pool_mode = "shared"
        save_draft(user_id, draft)
        answer_text = "Выбран общий пул"
    else:
        user_backend_id = await get_user_backend_id(user_id)
        if not user_backend_id:
            await callback.answer("Пользователь не найден", show_alert=True)
            return
        try:
            summary = await api_client.get_personal_pool_summary(user_backend_id)
        except Exception as exc:
            logger.warning("Composer: failed to check personal pool: %s", exc)
            await callback.answer("Не удалось проверить личные аккаунты", show_alert=True)
            return
        if _personal_pool_usable_count(summary) <= 0:
            await callback.answer("В личном пуле нет доступных аккаунтов", show_alert=True)
            return
        draft.account_pool_mode = "personal"
        save_draft(user_id, draft)
        answer_text = "Выбраны личные аккаунты"

    summary = await _format_composer_summary(draft)
    await callback.message.edit_text(
        summary,
        reply_markup=get_composer_keyboard(draft),
        parse_mode="Markdown"
    )
    await callback.answer(answer_text)


@router.callback_query(F.data == "composer:save_template")
async def save_template_handler(callback: CallbackQuery, state: FSMContext):
    """Запрашивает имя для сохранения шаблона."""
    user_id = callback.from_user.id
    draft = get_draft(user_id)

    if not draft.comment:
        await callback.answer("Добавьте комментарий перед сохранением шаблона", show_alert=True)
        return

    await callback.message.answer(
        "Введите имя для шаблона:"
    )
    await state.set_state(ComposerStates.INPUT_TEMPLATE_NAME)
    await callback.answer()


@router.message(StateFilter(ComposerStates.INPUT_TEMPLATE_NAME))
async def process_template_name(message: Message, state: FSMContext):
    """Сохраняет шаблон задачи."""
    user_id = message.from_user.id
    template_name = message.text or ""

    if not template_name.strip():
        await message.answer("Имя шаблона не может быть пустым.")
        return

    try:
        user_backend_id = await get_user_backend_id(user_id)
        if not user_backend_id:
            await message.answer("Ошибка: пользователь не найден")
            await state.clear()
            return

        draft = get_draft(user_id)

        # Composer не собирает тексты ответов — шаблон создаётся без них
        reply1 = None
        reply2 = None
        reply3 = None

        await api_client.create_task_template(
            user_id=user_backend_id,
            name=template_name,
            main_comment=draft.comment,
            text_variants=draft.text_variants if draft.text_variants else None,
            reply1=reply1,
            reply2=reply2,
            reply3=reply3,
            likes=0,
            settings={
                "delays": draft.delays,
                "shuffle": draft.shuffle,
                "rotate_accounts": draft.rotate_accounts,
                "reply_mode": draft.reply_mode,
                "account_pool_mode": draft.account_pool_mode,
            }
        )

        await message.answer(
            f"Шаблон сохранён: {template_name}"
        )
        await state.clear()

    except Exception as e:
        logger.error(f"Error saving template: {e}")
        await message.answer("Ошибка сохранения шаблона")
        await state.clear()


@router.callback_query(F.data == "composer:submit")
async def submit_order(callback: CallbackQuery, state: FSMContext):
    """Отправляет заказ."""
    user_id = callback.from_user.id
    draft = get_draft(user_id)

    # Валидация
    if not draft.links:
        await callback.answer("Добавьте ссылки", show_alert=True)
        return

    if not draft.comment:
        await callback.answer("Добавьте комментарий", show_alert=True)
        return

    try:
        # Проверяем наличие активных аккаунтов
        await callback.message.edit_text("Проверка ресурсов...")
        has_accounts = await api_client.has_active_accounts()
        if not has_accounts:
            await callback.message.edit_text(
                "Нет активных аккаунтов для комментирования.\n\n"
                "Обратитесь к администратору для добавления аккаунтов.",
                reply_markup=get_main_menu_keyboard()
            )
            return

        user_backend_id = await get_user_backend_id(user_id)
        if not user_backend_id:
            await callback.message.edit_text("Ошибка: пользователь не найден")
            return

        if draft.account_pool_mode == "personal":
            try:
                pool_summary = await api_client.get_personal_pool_summary(user_backend_id)
            except Exception as exc:
                logger.warning("Composer: failed to validate personal pool before submit: %s", exc)
                await callback.message.edit_text(
                    "Не удалось проверить личные аккаунты. Попробуйте ещё раз.",
                    reply_markup=get_composer_keyboard(draft)
                )
                return
            if _personal_pool_usable_count(pool_summary) <= 0:
                await callback.message.edit_text(
                    "В личном пуле сейчас нет доступных аккаунтов. Выберите общий пул или обновите личные аккаунты.",
                    reply_markup=get_composer_keyboard(draft)
                )
                return

        # Composer не собирает тексты ответов отдельно.
        # Ответы подставляются только через шаблоны (quick_order / order_wizard).
        reply1 = None
        reply2 = None
        reply3 = None
        text_variants = normalize_text_variants_list(draft.text_variants)
        if len(text_variants) < 3:
            # Composer не собирает 3 варианта main-комментария сам — это делается
            # в order_wizard. Предлагаем пользователю явный путь, а не глухую
            # ошибку. Сохраняем уже введённые ссылки/текст в draft, чтобы wizard
            # их подхватил.
            await callback.message.edit_text(
                "💬 *Нужно 3 варианта комментария*\n\n"
                "Для надёжной публикации требуется ровно 3 разных текста — каждый "
                "будет опубликован с одного аккаунта.\n\n"
                "Откройте полный мастер заказа: я попрошу ввести три варианта по одному.",
                reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                    [InlineKeyboardButton(text="📝 Полный мастер заказа", callback_data="menu:new_order")],
                    [InlineKeyboardButton(text="📑 Выбрать шаблон", callback_data="menu:templates")],
                    [InlineKeyboardButton(text=f"{Emoji.CANCEL} Отмена", callback_data="composer:cancel")],
                ]),
                parse_mode="Markdown",
            )
            return

        # Создаём задачу
        settings = {
            "delays": draft.delays,
            "shuffle": draft.shuffle,
            "rotate_accounts": draft.rotate_accounts,
            "reply_mode": draft.reply_mode,
            "account_pool_mode": draft.account_pool_mode,
            "origin": "composer"  # REFACTOR: Указываем источник заказа
        }

        await callback.message.edit_text("Создание заказа...")

        result = await api_client.create_task(
            user_id=user_backend_id,
            main_comment=draft.comment,
            video_urls=draft.links,
            text_variants=text_variants,
            reply1=reply1,
            reply2=reply2,
            reply3=reply3,
            likes=0,
            settings=settings
        )

        task_id = result.get("task_id")
        reset_draft(user_id)

        from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup
        
        if not callback.message:
            await callback.answer("Ошибка: сообщение не найдено", show_alert=True)
            return
        
        await callback.message.edit_text(
            f"✅ *Заказ создан*\n\n"
            f"📋 ID: `{task_id}`\n"
            f"📊 Статус: в очереди\n\n"
            f"Заказ добавлен в очередь.",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="👁 Посмотреть заказ", callback_data=f"order:detail:{task_id}")],
                [InlineKeyboardButton(text="➕ Создать ещё", callback_data="menu:new_order")],
                [InlineKeyboardButton(text="🏠 Главное меню", callback_data="menu:main")]
            ]),
            parse_mode="Markdown"
        )
        await callback.answer(f"{Emoji.SUCCESS} Заказ создан")

    except Exception as e:
        logger.error(f"Error creating task: {e}", exc_info=True)
        try:
            await callback.answer()
        except Exception:
            pass
        error_msg = str(e)

        # Обработка дубликатов
        if "DUPLICATE_ORDER" in error_msg:
            duplicate_msg = (
                "⚠️ *Дубликат заказа*\n\n"
                "Вы уже успешно публиковали этот комментарий под это видео.\n"
                "ТикТок может склеить их или скрыть.\n\n"
                "Пожалуйста, измените текст комментария (хотя бы пару слов)."
            )
            if not callback.message:
                await callback.answer("Ошибка: сообщение не найдено", show_alert=True)
                return
            await callback.message.edit_text(
                duplicate_msg,
                reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                    [InlineKeyboardButton(text=f"{Emoji.REFRESH} Изменить комментарий", callback_data="menu:new_order")],
                    [InlineKeyboardButton(text=f"{Emoji.HOME} Меню", callback_data="menu:main")]
                ]),
                parse_mode="Markdown"
            )
        # Обработка ошибки лимита видео
        elif "Daily video limit exceeded" in error_msg or "limit exceeded" in error_msg.lower():
            from datetime import datetime, timezone, timedelta
            now = datetime.now(timezone.utc)
            tomorrow = (now.replace(hour=0, minute=0, second=0, microsecond=0) + timedelta(days=1))
            hours_until_reset = (tomorrow - now).total_seconds() / 3600
            
            limit_msg = (
                f"{Emoji.WARNING} *Достигнут суточный лимит видео*\n\n"
                f"{error_msg[:300]}\n\n"
                f"Лимит сбросится через {int(hours_until_reset)} часов.\n"
                f"Или обратитесь к администратору для увеличения лимита."
            )
            if not callback.message:
                await callback.answer("Ошибка: сообщение не найдено", show_alert=True)
                return
            await callback.message.edit_text(
                limit_msg,
                reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                    [InlineKeyboardButton(text=f"{Emoji.HOME} Меню", callback_data="menu:main")]
                ]),
                parse_mode="Markdown"
            )
        else:
            # ИСПРАВЛЕНО: Используем централизованную обработку ошибок
            error_text, error_keyboard = get_task_creation_error_message(e)
            if not callback.message:
                await callback.answer("Ошибка: сообщение не найдено", show_alert=True)
                return
            await callback.message.edit_text(
                error_text,
                reply_markup=error_keyboard,
                parse_mode="Markdown"
            )


@router.callback_query(F.data == "composer:cancel")
async def cancel_composer(callback: CallbackQuery, state: FSMContext):
    """Отменяет композер."""
    await state.clear()
    user_id = callback.from_user.id
    reset_draft(user_id)

    user_data = await api_client.sync_telegram(user_id, None)
    role = user_data.get("role", "user")
    is_admin = role in ("admin", "owner") or user_id == config.get("OWNER_TELEGRAM_ID")

    from bot.handlers.menu import _build_main_menu_text
    if not callback.message:
        await callback.answer("Ошибка: сообщение не найдено", show_alert=True)
        return
    await callback.message.edit_text(
        _build_main_menu_text(is_admin=is_admin),
        reply_markup=get_main_menu_keyboard(is_admin=is_admin),
        parse_mode="Markdown"
    )
    await callback.answer()
