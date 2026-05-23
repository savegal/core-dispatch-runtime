from aiogram import Router, F
from aiogram.types import Message, CallbackQuery, InlineKeyboardMarkup, InlineKeyboardButton
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.exceptions import TelegramForbiddenError
from bot.services.api_client import api_client
from bot.services.subscription_service import check_user_access
from bot.keyboards.common import get_main_menu_keyboard, get_access_denied_keyboard
from bot.config import load_config
from config.settings import get_settings
from bot.services.drafts import get_draft
from bot.state.order_states import OrderStates
from bot.services.formatting import Emoji
from bot.services.likes_ui import get_likes_display_label
from bot.services.template_utils import is_replies_only_template, template_replies
from bot.services.telegram_utils import safe_callback_answer
from bot.services.software_mode import (
    get_software_mode_for_user,
    SOFTWARE_MODE_PARSING,
)
import logging

logger = logging.getLogger("bot.menu")
router = Router()
config = load_config()
settings = get_settings()


def _build_access_denied_text(*, blocked: bool) -> str:
    if blocked:
        return (
            f"*{Emoji.LOCK} Доступ запрещён*\n\n"
            "Вы заблокированы администратором.\n"
            "Для разблокировки обратитесь к администратору."
        )
    return (
        "*Доступ не подтверждён*\n\n"
        "Ваш доступ к боту пока не активен.\n"
        "Проверьте статус позже или обратитесь к администратору."
    )


async def check_access(user_id: int) -> tuple[bool, dict | None]:
    """Проверяет доступ пользователя."""
    try:
        user_data = await api_client.sync_telegram(user_id, None)
        role = user_data.get("role", "user")
        has_access = await check_user_access(user_id, user_data.get("id"), role)
        return has_access, user_data
    except Exception as e:
        logger.error(f"Error checking access: {e}")
        return False, None


async def ensure_access(callback: CallbackQuery) -> tuple[bool, dict | None]:
    """Проверяет доступ пользователя перед выполнением действия."""
    try:
        if not callback.message:
            await safe_callback_answer(callback, "Ошибка: сообщение не найдено", show_alert=True)
            return False, None
        
        user_id = callback.from_user.id
        user_data = await api_client.sync_telegram(user_id, callback.from_user.username)
        user_backend_id = user_data.get("id")
        role = user_data.get("role", "user")
        has_access = await check_user_access(user_id, user_backend_id, role)
        
        if not has_access:
            # Проверяем, заблокирован ли пользователь
            from bot.services.subscription_service import check_user_blocked
            user_id = callback.from_user.id
            is_blocked, block_msg = await check_user_blocked(user_id, user_data.get("id") if user_data else None)
            is_pending = bool(user_data.get("is_pending_approval")) if user_data else False

            if is_pending:
                await callback.message.edit_text(
                    "*⏳ Ожидайте одобрения администратором*\n\n"
                    "Ваша заявка получена и отправлена администратору.\n"
                    "Как только её рассмотрят — бот сразу станет доступен.\n\n"
                    "Нажмите «🔄 Проверить доступ», чтобы обновить статус.",
                    reply_markup=get_access_denied_keyboard(pending_approval=True),
                    parse_mode="Markdown"
                )
            elif is_blocked:
                await callback.message.edit_text(
                    "*🔒 Доступ запрещён*\n\n"
                    "Вы заблокированы администратором.\n"
                    "Для разблокировки обратитесь к администратору.",
                    reply_markup=get_access_denied_keyboard(),
                    parse_mode="Markdown"
                )
            else:
                await callback.message.edit_text(
                    _build_access_denied_text(blocked=False),
                    reply_markup=get_access_denied_keyboard(),
                    parse_mode="Markdown"
                )
            await safe_callback_answer(callback, "Доступ запрещён", show_alert=True)
            return False, None

        return True, user_data
    except Exception as e:
        logger.error(f"Error checking access: {e}", exc_info=True)
        try:
            await safe_callback_answer(callback, "Ошибка проверки доступа", show_alert=True)
        except:
            pass
        return False, None


@router.message(Command("start"))
async def cmd_start(message: Message, state: FSMContext):
    """Обработчик команды /start."""
    await state.clear()
    
    user_id = message.from_user.id
    username = message.from_user.username

    try:
        # Синхронизируем с backend.
        # ВАЖНО: use_cache=False, потому что нам нужен честный is_new при первом
        # /start новой учётки. С кэшем sync_telegram мог бы вернуть закэшированный
        # ответ без is_new и мы пропустили бы уведомление владельцу.
        user_data = await api_client.sync_telegram(user_id, username, use_cache=False)
        user_backend_id = user_data.get("id")
        role = user_data.get("role", "user")

        # Если бэкенд только что создал запись — уведомляем владельца ровно один
        # раз. Уведомление синхронное, но в фоне: ошибки не должны ломать /start.
        if user_data.get("is_new"):
            try:
                from bot.services.worker_approval import notify_owner_about_new_worker
                await notify_owner_about_new_worker(user_data=user_data, telegram_id=user_id)
            except Exception as notify_exc:  # noqa: BLE001
                logger.warning(f"Failed to notify owner about new worker {user_id}: {notify_exc}")

        # Проверяем доступ (используем уже полученную роль)
        has_access = await check_user_access(user_id, user_backend_id, role=role, use_cache=True)
        is_admin = role in ("admin", "owner") or user_id == config.get("OWNER_TELEGRAM_ID")

        if not has_access:
            # Проверяем, заблокирован ли пользователь
            from bot.services.subscription_service import check_user_blocked
            is_blocked, block_msg = await check_user_blocked(user_id, user_backend_id)
            is_pending = bool(user_data.get("is_pending_approval"))

            if is_pending:
                await message.answer(
                    f"*{Emoji.PENDING} Ожидайте одобрения администратором*\n\n"
                    "Ваша заявка получена и отправлена администратору.\n"
                    "Как только её рассмотрят — бот сразу станет доступен.\n\n"
                    "Нажмите «🔄 Проверить доступ», чтобы обновить статус.",
                    reply_markup=get_access_denied_keyboard(pending_approval=True),
                    parse_mode="Markdown"
                )
            elif is_blocked:
                await message.answer(
                    f"*{Emoji.LOCK} Доступ запрещён*\n\n"
                    "Вы заблокированы администратором.\n"
                    "Для разблокировки обратитесь к администратору.",
                    reply_markup=get_access_denied_keyboard(),
                    parse_mode="Markdown"
                )
            else:
                await message.answer(
                    _build_access_denied_text(blocked=False),
                    reply_markup=get_access_denied_keyboard(),
                    parse_mode="Markdown"
                )
            return

        # Для admin/owner режим всегда full, даже если глобально включён parsing.
        software_mode = await get_software_mode_for_user(user_id)
        parsing_only = software_mode == SOFTWARE_MODE_PARSING

        # Баланс — только в полном режиме (в parsing коментинг недоступен,
        # биллинг по комментам не идёт).
        balance_text = ""
        if not parsing_only and not is_admin:
            try:
                user_detail = await api_client.get_user(user_backend_id)
                balance = user_detail.get("balance", 0.0)
                balance_text = f"\n\n💰 *Баланс:* `{balance:.2f}` грн"
            except Exception:
                pass

        title = (
            "*Мониторинг видео*"
            if parsing_only
            else "*Приватный сервис комментариев*"
        )
        main_text = title + balance_text + "\n\nВыберите действие:"

        await message.answer(
            main_text,
            reply_markup=get_main_menu_keyboard(
                is_admin=is_admin, software_mode=software_mode
            ),
            parse_mode="Markdown"
        )

    except Exception as e:
        error_msg = str(e)
        logger.error(f"Error in /start: {error_msg}")
        
        # ИСПРАВЛЕНО: Убрано техническое сообщение, улучшено форматирование
        if "подключиться" in error_msg or "недоступен" in error_msg:
            user_msg = (
                "*Ошибка подключения*\n\n"
                "Не удалось подключиться к серверу.\n\n"
                "Попробуйте позже или обратитесь к администратору."
            )
        else:
            user_msg = (
                "*Ошибка*\n\n"
                "Произошла ошибка при запуске.\n\n"
                "Попробуйте позже или обратитесь к администратору."
            )
        
        try:
            await message.answer(user_msg, parse_mode="Markdown")
        except TelegramForbiddenError:
            logger.info("Cannot send /start error message: user %s blocked the bot", user_id)


@router.callback_query(F.data == "menu:main")
async def show_main_menu(callback: CallbackQuery):
    """Показывает главное меню."""
    try:
        if not callback.message:
            await safe_callback_answer(callback, "Ошибка: сообщение не найдено", show_alert=True)
            return
        
        await safe_callback_answer(callback)
        has_access, user_data = await ensure_access(callback)
        if not has_access:
            return

        user_id = callback.from_user.id
        role = user_data.get("role", "user")
        is_admin = role in ("admin", "owner") or user_id == config.get("OWNER_TELEGRAM_ID")
        user_backend_id = user_data.get("id")

        software_mode = await get_software_mode_for_user(user_id)
        parsing_only = software_mode == SOFTWARE_MODE_PARSING

        balance_text = ""
        if not parsing_only and not is_admin and user_backend_id:
            try:
                user_detail = await api_client.get_user(user_backend_id)
                balance = user_detail.get("balance", 0.0)
                balance_text = f"\n\n💰 *Баланс:* `{balance:.2f}` грн"
            except Exception:
                pass

        title = (
            "*Мониторинг видео*"
            if parsing_only
            else "*Приватный сервис комментариев*"
        )
        main_text = title + balance_text + "\n\nВыберите действие:"

        await callback.message.edit_text(
            main_text,
            reply_markup=get_main_menu_keyboard(
                is_admin=is_admin, software_mode=software_mode
            ),
            parse_mode="Markdown"
        )
    except Exception as e:
        logger.error(f"Error in show_main_menu: {e}", exc_info=True)
        try:
            await safe_callback_answer(callback, "Ошибка загрузки главного меню", show_alert=True)
        except:
            pass


@router.callback_query(F.data == "menu:help")
async def show_help(callback: CallbackQuery):
    """Показывает справку."""
    try:
        if not callback.message:
            await safe_callback_answer(callback, "Ошибка: сообщение не найдено", show_alert=True)
            return
        
        await safe_callback_answer(callback)
        from bot.keyboards.common import get_back_keyboard
        
        help_text = (
            "*❓ Помощь*\n\n"
            "🆕 *Новый заказ* - создание задачи на комментарии\n"
            "📋 *Мои заказы* - список ваших задач\n"
            "📑 *Шаблоны* - сохранённые шаблоны комментариев\n"
            "⚙️ *Настройки* - настройки по умолчанию\n\n"
            "Для создания заказа выберите шаблон или введите новый комментарий."
        )
        
        await callback.message.edit_text(
            help_text,
            reply_markup=get_back_keyboard(),
            parse_mode="Markdown"
        )
    except Exception as e:
        logger.error(f"Error showing help: {e}", exc_info=True)
        try:
            await safe_callback_answer(callback, "Ошибка загрузки справки", show_alert=True)
        except:
            pass


TEMPLATES_MENU_PAGE_SIZE = 10


@router.callback_query((F.data == "menu:templates") | F.data.startswith("menu:templates:p:"))
async def show_templates(callback: CallbackQuery):
    """Показывает шаблоны."""
    try:
        if not callback.message:
            await safe_callback_answer(callback, "Ошибка: сообщение не найдено", show_alert=True)
            return
        
        await safe_callback_answer(callback)
        has_access, user_data = await ensure_access(callback)
        if not has_access:
            return

        user_backend_id = user_data.get("id")
        templates = await api_client.list_task_templates(user_backend_id)
        page = 0
        if callback.data and callback.data.startswith("menu:templates:p:"):
            try:
                page = max(0, int(callback.data.rsplit(":", 1)[-1]))
            except (TypeError, ValueError):
                page = 0
        
        keyboard = []
        
        if not templates:
            await callback.message.edit_text(
                "*📑 Шаблоны комментариев*\n\n"
                "У вас пока нет сохранённых шаблонов.",
                reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                    [InlineKeyboardButton(text="➕ Новый шаблон", callback_data="template:create_new")],
                    [InlineKeyboardButton(text="◀️ Назад", callback_data="menu:main")]
                ]),
                parse_mode="Markdown"
            )
            return
        
        total_pages = max(1, (len(templates) + TEMPLATES_MENU_PAGE_SIZE - 1) // TEMPLATES_MENU_PAGE_SIZE)
        page = min(page, total_pages - 1)
        start = page * TEMPLATES_MENU_PAGE_SIZE
        shown_templates = templates[start:start + TEMPLATES_MENU_PAGE_SIZE]

        # Показываем список шаблонов
        for template in shown_templates:
            name = template.get("name", "Без имени")
            template_id = template.get("id")
            kind_icon = "↩️" if is_replies_only_template(template) else "💬"
            replies_count = len(template_replies(template))
            if len(name) > 28:
                name = name[:25] + "..."
            keyboard.append([
                InlineKeyboardButton(
                    text=f"{kind_icon} {name} · {replies_count} отв.",
                    callback_data=f"template:detail:{template_id}"
                )
            ])
        
        text = (
            "*📑 Шаблоны*\n\n"
            "💬 полный шаблон · ↩️ только ответы\n\n"
            f"Страница *{page + 1}*/{total_pages} · всего: *{len(templates)}*"
        )

        if total_pages > 1:
            nav = []
            if page > 0:
                nav.append(InlineKeyboardButton(text="◀️", callback_data=f"menu:templates:p:{page - 1}"))
            if page < total_pages - 1:
                nav.append(InlineKeyboardButton(text="▶️", callback_data=f"menu:templates:p:{page + 1}"))
            if nav:
                keyboard.append(nav)
        
        keyboard.append([
            InlineKeyboardButton(text="➕ Создать шаблон", callback_data="template:create_new")
        ])
        keyboard.append([
            InlineKeyboardButton(text="Назад", callback_data="menu:main")
        ])
        
        await callback.message.edit_text(
            text,
            reply_markup=InlineKeyboardMarkup(inline_keyboard=keyboard),
            parse_mode="Markdown"
        )
        
    except Exception as e:
        logger.error(f"Error loading templates: {e}", exc_info=True)
        try:
            await safe_callback_answer(callback, "Ошибка загрузки шаблонов", show_alert=True)
        except:
            pass


@router.callback_query(F.data == "menu:subscription")
async def show_subscription(callback: CallbackQuery):
    """Показывает информацию о балансе."""
    try:
        if not callback.message:
            await safe_callback_answer(callback, "Ошибка: сообщение не найдено", show_alert=True)
            return
        
        await safe_callback_answer(callback)
        has_access, user_data = await ensure_access(callback)
        if not has_access:
            return
        
        user_backend_id = user_data.get("id")
        role = user_data.get("role", "user")
        is_admin = role in ("admin", "owner") or callback.from_user.id == config.get("OWNER_TELEGRAM_ID")
        
        if is_admin:
            text = "*💰 Баланс*\n"
            text += "━━━━━━━━━━━━━━━━━━━━\n\n"
            text += "👑 *Администратор*\n"
            text += "♾️ Неограниченный баланс"
        else:
            user_detail = await api_client.get_user(user_backend_id)
            balance = user_detail.get("balance", 0.0)
            
            # Получаем информацию о ценах
            try:
                pricing = await api_client.get_pricing()
                price_success = pricing.get("price_success", 5.0)
                price_failure = pricing.get("price_failure", 2.0)
                refund_amount = price_success - price_failure
            except Exception:
                price_success = 5.0
                price_failure = 2.0
                refund_amount = 3.0
            
            text = "*💰 Баланс*\n"
            text += "━━━━━━━━━━━━━━━━━━━━\n\n"
            text += f"💵 *Текущий баланс:* `{balance:.2f}` грн\n\n"
            text += "*💳 Тарифы:*\n"
            text += f"• ✅ Успешное комментирование: `{price_success:.2f}` грн\n"
            text += f"• ❌ Неуспешное (расходники): `{price_failure:.2f}` грн\n"
            text += f"• 🔄 Возврат при неудаче: `{refund_amount:.2f}` грн\n\n"
            text += "💬 *Для пополнения баланса обратитесь к администратору.*"
        
        await callback.message.edit_text(
            text,
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="◀️ Назад", callback_data="menu:main")]
            ]),
            parse_mode="Markdown"
        )
    except Exception as e:
        logger.error(f"Error showing balance: {e}", exc_info=True)
        try:
            await safe_callback_answer(callback, "Ошибка загрузки баланса", show_alert=True)
        except:
            pass


@router.callback_query(F.data == "menu:check_access")
async def check_access_handler(callback: CallbackQuery):
    """Проверяет доступ пользователя."""
    try:
        if not callback.message:
            await safe_callback_answer(callback, "Ошибка: сообщение не найдено", show_alert=True)
            return
        
        await safe_callback_answer(callback)
        user_id = callback.from_user.id
        
        # Проверяем доступ
        has_access = await check_user_access(user_id, None, None)
        
        if has_access:
            # Если доступ есть, показываем главное меню
            user_data = await api_client.sync_telegram(user_id, None)
            role = user_data.get("role", "user")
            is_admin = role in ("admin", "owner") or user_id == config.get("OWNER_TELEGRAM_ID")

            software_mode = await get_software_mode_for_user(user_id)
            parsing_only = software_mode == SOFTWARE_MODE_PARSING

            balance_text = ""
            if not parsing_only and not is_admin:
                try:
                    user_detail = await api_client.get_user(user_data.get("id"))
                    balance = user_detail.get("balance", 0.0)
                    balance_text = f"\n\n💰 *Баланс:* `{balance:.2f}` грн"
                except Exception:
                    pass

            title = (
                "*Мониторинг видео*"
                if parsing_only
                else "*Приватный сервис комментариев*"
            )
            main_text = title + balance_text + "\n\nВыберите действие:"

            await callback.message.edit_text(
                main_text,
                reply_markup=get_main_menu_keyboard(
                    is_admin=is_admin, software_mode=software_mode
                ),
                parse_mode="Markdown"
            )
        else:
            # Если доступа нет, показываем экран "нет доступа".
            # Тянем актуальный sync_telegram без кэша — иначе после
            # одобрения админом флаг is_pending_approval не успеет обновиться.
            from bot.services.subscription_service import check_user_blocked
            try:
                user_data = await api_client.sync_telegram(user_id, callback.from_user.username, use_cache=False)
            except Exception:
                user_data = {}
            is_blocked, block_msg = await check_user_blocked(user_id, user_data.get("id") if user_data else None)
            is_pending = bool(user_data.get("is_pending_approval")) if user_data else False

            if is_pending:
                await callback.message.edit_text(
                    "*⏳ Ожидайте одобрения администратором*\n\n"
                    "Ваша заявка получена и отправлена администратору.\n"
                    "Как только её рассмотрят — бот сразу станет доступен.",
                    reply_markup=get_access_denied_keyboard(pending_approval=True),
                    parse_mode="Markdown"
                )
            elif is_blocked:
                await callback.message.edit_text(
                    "*🔒 Доступ запрещён*\n\n"
                    "Вы заблокированы администратором.\n"
                    "Для разблокировки обратитесь к администратору.",
                    reply_markup=get_access_denied_keyboard(),
                    parse_mode="Markdown"
                )
            else:
                await callback.message.edit_text(
                    _build_access_denied_text(blocked=False),
                    reply_markup=get_access_denied_keyboard(),
                    parse_mode="Markdown"
                )
    except Exception as e:
        logger.error(f"Error checking access: {e}", exc_info=True)
        try:
            await safe_callback_answer(callback, "Ошибка проверки доступа", show_alert=True)
        except:
            pass


@router.callback_query(F.data == "menu:new_order")
async def menu_new_order(callback: CallbackQuery, state: FSMContext):
    """Начинает создание заказа через меню."""
    try:
        await safe_callback_answer(callback)
        has_access, user_data = await ensure_access(callback)
        if not has_access:
            return
        
        user_id = callback.from_user.id
        
        # КРИТИЧНО: Проверяем наличие ссылок в draft
        draft = get_draft(user_id)
        
        if not draft.links or len(draft.links) == 0:
            # Просим отправить ссылки
            await callback.message.edit_text(
                "📹 *Создание заказа*\n\n"
                "Отправьте ссылки на TikTok видео (одну или несколько)\n\n"
                "*Формат:*\n"
                "`https://www.tiktok.com/@user/video/123`\n\n"
                "*Или используйте короткие ссылки:*\n"
                "`https://vm.tiktok.com/...`",
                reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                    [InlineKeyboardButton(text="❌ Отмена", callback_data="menu:main")]
                ]),
                parse_mode="Markdown"
            )
            # Устанавливаем состояние ожидания ссылок
            await state.set_state(OrderStates.INPUT_LINKS)
            return
        
        # Если ссылки есть - перенаправляем на quick_order flow
        # (НО у нас нет прямого способа вызвать show_template_selection)
        # Вместо этого показываем сообщение с подтверждением
        # ИСПРАВЛЕНО: Предлагаем начать заново если есть ссылки
        await callback.message.edit_text(
            f"📹 *Создание заказа*\n\n"
            f"✅ *Найден незавершенный заказ*\n"
            f"Сохраненных ссылок: `{len(draft.links)}`\n\n"
            f"Вы хотите продолжить оформление или начать заново?",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="✅ Продолжить", callback_data="quick:show_templates")],
                [InlineKeyboardButton(text="🗑 Начать заново", callback_data="menu:new_order:clear")],
                [InlineKeyboardButton(text="🏠 Меню", callback_data="menu:main")]
            ]),
            parse_mode="Markdown"
        )
        
    except Exception as e:
        logger.error(f"Error in menu_new_order: {e}", exc_info=True)
        try:
            await safe_callback_answer(callback, "Ошибка", show_alert=True)
        except:
            pass


@router.callback_query(F.data == "menu:new_order:clear")
async def new_order_clear(callback: CallbackQuery, state: FSMContext):
    """Очищает черновик и начинает новый заказ."""
    try:
        from bot.services.drafts import reset_draft
        user_id = callback.from_user.id
        reset_draft(user_id)
        await state.clear()
        
        await callback.message.edit_text(
            "🗑 *Черновик очищен*\n\n"
            "📹 *Создание заказа*\n\n"
            "Отправьте ссылки на TikTok видео (одну или несколько)\n\n"
            "*Формат:*\n"
            "`https://www.tiktok.com/@user/video/123`",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="❌ Отмена", callback_data="menu:main")]
            ]),
            parse_mode="Markdown"
        )
        await state.set_state(OrderStates.INPUT_LINKS)
        await safe_callback_answer(callback, "Черновик очищен")
    except Exception as e:
        logger.error(f"Error in new_order_clear: {e}", exc_info=True)
        await safe_callback_answer(callback, "Ошибка очистки", show_alert=True)


@router.callback_query(F.data == "menu:settings")
async def show_settings(callback: CallbackQuery):
    """Показывает информацию о настройках (упрощённая версия)."""
    try:
        if not callback.message:
            await safe_callback_answer(callback, "Ошибка: сообщение не найдено", show_alert=True)
            return
        
        await safe_callback_answer(callback)
        
        likes_label = await get_likes_display_label()
        text = (
            "*⚙️ Информация*\n\n"
            "Все настройки применяются автоматически:\n\n"
            f"• ❤️ Лайки: {likes_label}\n"
            "• 💬 Режим ответов: автоматический\n"
            "• ⏱ Задержки: включены\n"
            "• 🔄 Ротация аккаунтов: включена\n\n"
            "Для изменения настроек обратитесь к администратору."
        )
        
        keyboard = [
            [InlineKeyboardButton(text="◀️ Назад", callback_data="menu:main")]
        ]
        
        await callback.message.edit_text(
            text,
            reply_markup=InlineKeyboardMarkup(inline_keyboard=keyboard),
            parse_mode="Markdown"
        )
    except Exception as e:
        logger.error(f"Error showing settings: {e}", exc_info=True)
        try:
            await safe_callback_answer(callback, "Ошибка загрузки настроек", show_alert=True)
        except:
            pass


# Fallback обработчик удален - он мешал обработке callback'ов в других роутерах
# Если callback не обработан ни одним роутером, aiogram просто не отвечает на него
# Это нормальное поведение - пользователь увидит, что кнопка не работает

