"""Обработчики для управления прокси и мониторинга ресурсов."""
from aiogram import Router, F
from aiogram.types import CallbackQuery, Message, InlineKeyboardMarkup, InlineKeyboardButton
from aiogram.fsm.context import FSMContext
from aiogram.filters import StateFilter
from bot.services.api_client import api_client
from bot.keyboards.admin import (
    get_proxies_menu_keyboard, get_proxy_import_confirm_keyboard,
    get_resources_keyboard, get_admin_menu_keyboard
)
from bot.state.order_states import OrderStates
from bot.handlers.admin import get_user_data, is_admin_user
from bot.config import load_config
from services.admin_audit_service import log_admin_action
import re
import logging

logger = logging.getLogger("bot.admin_resources")
router = Router()
config = load_config()


def parse_proxy_line(line: str) -> dict | None:
    """
    Парсит одну строку с прокси.
    
    Поддерживаемые форматы:
    - login:pass@ip:port|rotation_url (мобильный прокси)
    - ip:port|rotation_url (мобильный прокси)
    - login:pass@ip:port (обычный прокси)
    - ip:port (обычный прокси)
    
    Returns:
        dict с ключами: proxy, rotation_url, is_mobile
        или None если формат не распознан
    """
    line = line.strip()
    if not line or line.startswith("#"):
        return None
    
    # Проверяем наличие rotation_url (разделитель |)
    rotation_url = None
    proxy_part = line
    
    if "|" in line:
        parts = line.split("|", 1)
        proxy_part = parts[0].strip()
        rotation_url = parts[1].strip() if len(parts) > 1 and parts[1].strip() else None
        
        # Валидация rotation_url (должен быть валидный URL)
        if rotation_url:
            # Простая проверка что это похоже на URL
            if not (rotation_url.startswith("http://") or rotation_url.startswith("https://")):
                # Если не начинается с http/https, возможно это ошибка парсинга
                # Возвращаем None - формат не распознан
                return None
    
    # Базовая проверка формата прокси
    # login:pass@ip:port или ip:port
    proxy_pattern = r'^([^@:]+(:[^@]+)?@)?([\d.]+|[\w.-]+):(\d+)$'
    if re.match(proxy_pattern, proxy_part):
        return {
            "proxy": proxy_part,
            "rotation_url": rotation_url,
            "is_mobile": "Y" if rotation_url else "N"
        }
    
    return None


@router.callback_query(F.data == "admin:proxies")
async def proxies_menu(callback: CallbackQuery):
    """Показывает меню управления прокси."""
    user_data = await get_user_data(callback.from_user.id)
    if not user_data or not user_data[2]:
        await callback.answer("Доступ запрещён", show_alert=True)
        return
    
    try:
        stats = await api_client.get_proxy_stats()
        total = stats.get('total', 0)
        
        # ИСПРАВЛЕНО: Убраны статусы - все прокси считаются рабочими
        stats_text = f"🌐 *Управление прокси*\n"
        stats_text += "━━━━━━━━━━━━━━━━━━━━\n\n"
        
        if total == 0:
            stats_text += "📭 Прокси не добавлены\n"
        else:
            stats_text += f"📦 Всего прокси: `{total}`\n"
            if "active" in stats:
                stats_text += f"• Активные: `{stats.get('active', 0)}`\n"
                stats_text += f"• Медленные: `{stats.get('slow', 0)}`\n"
                stats_text += f"• Плохие: `{stats.get('bad', 0)}`\n"
                stats_text += f"• Unknown: `{stats.get('unknown', 0)}`\n"
            stats_text += "\n"
            
            # Показываем краткую информацию о гео (если есть)
            try:
                proxies_list = await api_client.list_proxies(limit=100)
                proxies_with_geo = sum(1 for p in proxies_list if p.get("country_code"))
                if proxies_with_geo > 0:
                    stats_text += f"🌍 *Гео:* `{proxies_with_geo}/{len(proxies_list)}` с гео"
            except Exception:
                pass  # Не критично, просто не показываем
        
        await callback.message.edit_text(
            stats_text,
            reply_markup=get_proxies_menu_keyboard(),
            parse_mode="Markdown"
        )
        await callback.answer()
    except Exception as e:
        logger.error(f"Error getting proxy stats: {e}")
        try:
            await callback.message.edit_text(
                "❌ Ошибка загрузки статистики",
                reply_markup=get_proxies_menu_keyboard()
            )
            await callback.answer()
        except Exception:
            pass


PROXIES_PER_PAGE = 15


@router.callback_query(F.data == "admin:proxies:validate")
async def proxies_validate(callback: CallbackQuery):
    """Быстрая проверка прокси и авто-назначение рабочим аккаунтам."""
    await callback.answer()
    try:
        await callback.message.edit_text(
            "⚡ Проверяю прокси... это может занять немного времени",
            reply_markup=get_proxies_menu_keyboard()
        )

        result = await api_client.validate_proxies(
            timeout=8.0,
            concurrency=30,
            reassign_bad_proxies=True,
            assign_after=True
        )

        text = "✅ Проверка прокси завершена\n"
        text += f"• Проверено: `{result.get('checked', 0)}`\n"
        text += f"• Активные: `{result.get('active', 0)}`\n"
        text += f"• Медленные: `{result.get('slow', 0)}`\n"
        text += f"• Плохие: `{result.get('bad', 0)}`\n"
        text += f"• Unknown: `{result.get('unknown', 0)}`\n"
        text += f"• Ошибки: `{result.get('errors', 0)}`\n"
        text += f"• Аккаунты очищены: `{result.get('cleared_accounts', 0)}`\n"
        text += f"• Аккаунты назначены: `{result.get('assigned_accounts', 0)}`\n"
        if result.get("not_enough_proxies"):
            text += "⚠️ Недостаточно рабочих прокси для всех аккаунтов\n"

        await callback.message.edit_text(
            text,
            reply_markup=get_proxies_menu_keyboard(),
            parse_mode="Markdown"
        )
    except Exception as e:
        logger.error(f"Proxy validation failed: {e}", exc_info=True)
        await callback.message.edit_text(
            "❌ Ошибка проверки прокси",
            reply_markup=get_proxies_menu_keyboard()
        )

@router.callback_query(F.data == "admin:proxies:list")
async def proxies_list(callback: CallbackQuery):
    """Показывает список прокси с пагинацией."""
    await show_proxies_page(callback, page=0)


@router.callback_query(F.data.startswith("admin:proxies:page:"))
async def show_proxies_page_handler(callback: CallbackQuery):
    """Обрабатывает переход на страницу прокси."""
    try:
        page = int(callback.data.split(":")[-1])
        await show_proxies_page(callback, page=page)
    except (ValueError, IndexError):
        await callback.answer("Ошибка пагинации", show_alert=True)


async def show_proxies_page(callback: CallbackQuery, page: int = 0):
    """ИСПРАВЛЕНО: Показывает страницу прокси с серверной пагинацией."""
    try:
        await callback.answer()
        # ИСПРАВЛЕНО: Убрана дублирующая проверка прав - middleware уже проверяет
        
        from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton
        
        # Получаем статистику для отображения
        stats = await api_client.get_proxy_stats()
        total = stats.get("total", 0)
        
        # ИСПРАВЛЕНО: Используем серверную пагинацию вместо загрузки всех записей
        offset = page * PROXIES_PER_PAGE
        page_proxies = await api_client.list_proxies(limit=PROXIES_PER_PAGE, offset=offset)
        
        if not page_proxies and page == 0:
            await callback.message.edit_text(
                "*🌐 Прокси*\n\nПрокси не добавлены",
                reply_markup=get_proxies_menu_keyboard(),
                parse_mode="Markdown"
            )
            return
        
        # Вычисляем общее количество страниц (приблизительно)
        total_pages = (total + PROXIES_PER_PAGE - 1) // PROXIES_PER_PAGE if total > 0 else 1
        
        text = "🌐 *Список прокси*\n"
        text += "━━━━━━━━━━━━━━━━━━━━\n\n"
        text += f"📄 Страница: `{page + 1}/{total_pages}`\n"
        text += f"📦 Всего: `{total}`\n\n"
        
        # Маппинг стран для красивого отображения
        country_emojis = {
            "US": "🇺🇸", "UA": "🇺🇦", "GB": "🇬🇧", "DE": "🇩🇪", "FR": "🇫🇷",
            "IT": "🇮🇹", "ES": "🇪🇸", "PL": "🇵🇱", "RU": "🇷🇺", "CA": "🇨🇦",
            "AU": "🇦🇺", "JP": "🇯🇵", "KR": "🇰🇷", "CN": "🇨🇳", "BR": "🇧🇷"
        }
        
        for proxy in page_proxies:
            proxy_str = proxy.get("proxy", "")
            country_code = proxy.get("country_code")
            is_mobile = proxy.get("is_mobile", "N")
            
            # ИСПРАВЛЕНО: Показываем полный прокси без маскирования
            # Формируем строку с гео-информацией
            geo_info = ""
            if country_code:
                country_emoji = country_emojis.get(country_code, "🌍")
                geo_info = f" {country_emoji}{country_code}"
            
            # Показываем тип прокси
            mobile_info = " 📱" if is_mobile == "Y" else ""
            
            text += f"• `{proxy_str}`{geo_info}{mobile_info}\n"
        
        # Навигация
        keyboard = []
        nav_row = []
        if page > 0:
            nav_row.append(InlineKeyboardButton(text="◀️", callback_data=f"admin:proxies:page:{page - 1}"))
        # ИСПРАВЛЕНО: Показываем кнопку "Вперёд" только если получили полную страницу
        if len(page_proxies) == PROXIES_PER_PAGE and page < total_pages - 1:
            nav_row.append(InlineKeyboardButton(text="▶️", callback_data=f"admin:proxies:page:{page + 1}"))
        
        nav_row.append(InlineKeyboardButton(text="🔄", callback_data="admin:proxies:list"))
        nav_row.append(InlineKeyboardButton(text="◀️ Назад", callback_data="admin:proxies"))
        keyboard.append(nav_row)
        
        await callback.message.edit_text(
            text,
            reply_markup=InlineKeyboardMarkup(inline_keyboard=keyboard),
            parse_mode="Markdown"
        )
        
    except Exception as e:
        logger.error(f"Error listing proxies: {e}", exc_info=True)
        await callback.answer("Ошибка загрузки списка", show_alert=True)


# ИСПРАВЛЕНО: Удалены handlers проверки прокси (validate_proxies_handler, recheck_bad_proxies_handler, check_proxies_only)
# Все прокси считаются рабочими, проверка не требуется


@router.callback_query(F.data == "admin:proxies:import")
async def start_proxy_import(callback: CallbackQuery, state: FSMContext):
    """Запускает процесс импорта прокси."""
    user_data = await get_user_data(callback.from_user.id)
    if not user_data or not user_data[2]:
        await callback.answer("Доступ запрещён", show_alert=True)
        return
    
    text = "📥 *Импорт прокси*\n"
    text += "━━━━━━━━━━━━━━━━━━━━\n\n"
    text += "Отправьте .txt или .csv файл со списком прокси.\n\n"
    text += "*Поддерживаемые форматы:*\n"
    text += "• `login:pass@ip:port` - обычный прокси\n"
    text += "• `ip:port` - обычный прокси\n"
    text += "• `login:pass@ip:port|https://...` - мобильный прокси\n"
    text += "• `ip:port|https://...` - мобильный прокси\n\n"
    text += "_Строки с # в начале — игнорируются._"
    
    await callback.message.answer(
        text,
        parse_mode="Markdown"
    )
    await state.set_state(OrderStates.INPUT_PROXIES_FILE)
    await callback.answer()


@router.callback_query(F.data == "admin:proxies:delete_all")
async def confirm_delete_all_proxies(callback: CallbackQuery):
    """Подтверждение удаления всех прокси."""
    user_data = await get_user_data(callback.from_user.id)
    if not user_data or not user_data[2]:
        await callback.answer("Доступ запрещён", show_alert=True)
        return

    try:
        stats = await api_client.get_proxy_stats()
        total = stats.get("total", 0)

        if total == 0:
            await callback.answer("Прокси уже нет", show_alert=True)
            return

        text = "⚠️ *Подтверждение удаления*\n"
        text += "━━━━━━━━━━━━━━━━━━━━\n\n"
        text += f"Будет удалено прокси: `{total}`\n"
        text += "Также будет очищено поле proxy у аккаунтов.\n\n"
        text += "*Действие необратимо.*"

        keyboard = InlineKeyboardMarkup(inline_keyboard=[
            [
                InlineKeyboardButton(text="🗑 Удалить", callback_data="admin:proxies:delete_all:confirm"),
                InlineKeyboardButton(text="❌ Отмена", callback_data="admin:proxies")
            ]
        ])

        await callback.message.edit_text(text, parse_mode="Markdown", reply_markup=keyboard)
        await callback.answer()
    except Exception as e:
        logger.error(f"Error preparing delete all proxies: {e}", exc_info=True)
        await callback.answer("Ошибка подготовки удаления", show_alert=True)


@router.callback_query(F.data == "admin:proxies:delete_all:confirm")
async def delete_all_proxies_confirm(callback: CallbackQuery):
    """Удаляет все прокси."""
    user_data = await get_user_data(callback.from_user.id)
    if not user_data or not user_data[2]:
        await callback.answer("Доступ запрещён", show_alert=True)
        return

    try:
        result = await api_client.delete_all_proxies(clear_accounts=True)
        deleted = result.get("deleted", 0)
        cleared_accounts = result.get("cleared_accounts", 0)

        text = "✅ *Удаление завершено*\n"
        text += "━━━━━━━━━━━━━━━━━━━━\n\n"
        text += f"Удалено прокси: `{deleted}`\n"
        text += f"Очищено аккаунтов: `{cleared_accounts}`\n"

        await callback.message.edit_text(
            text,
            parse_mode="Markdown",
            reply_markup=get_proxies_menu_keyboard()
        )
        await callback.answer("Прокси удалены")
    except Exception as e:
        logger.error(f"Error deleting all proxies: {e}", exc_info=True)
        await callback.answer("Ошибка удаления прокси", show_alert=True)


@router.message(StateFilter(OrderStates.INPUT_PROXIES_FILE), F.document)
async def process_proxies_file(message: Message, state: FSMContext):
    """Обрабатывает файл с прокси."""
    user_data = await get_user_data(message.from_user.id)
    if not user_data or not user_data[2]:
        await message.answer("Доступ запрещён")
        await state.clear()
        return
    
    try:
        # Скачиваем файл
        file = await message.bot.get_file(message.document.file_id)
        file_path = await message.bot.download_file(file.file_path)
        
        # Читаем содержимое
        content = file_path.read().decode("utf-8", errors="ignore")
        
        # Парсим прокси
        lines = content.split("\n")
        proxies = []
        errors = []
        mobile_count = 0
        
        for idx, line in enumerate(lines, start=1):
            proxy_data = parse_proxy_line(line)
            if proxy_data:
                proxies.append(proxy_data)
                if proxy_data.get("is_mobile") == "Y":
                    mobile_count += 1
            elif line.strip() and not line.strip().startswith("#"):
                errors.append(f"Строка {idx}: не распознан формат прокси")
        
        if not proxies and not errors:
            await message.answer("Файл пуст или содержит только комментарии.")
            await state.clear()
            return
        
        # Сохраняем во временное хранилище
        await state.update_data(parsed_proxies=proxies, proxy_errors=errors)
        
        # Показываем результат
        errors_text = ""
        if errors:
            errors_preview = errors[:10]
            errors_text = f"\n\n⚠️ *Ошибки* (первые 10):\n" + "\n".join(errors_preview)
            if len(errors) > 10:
                errors_text += f"\n... и ещё {len(errors) - 10}"
        
        text = "✅ *Разбор файла завершён*\n"
        text += "━━━━━━━━━━━━━━━━━━━━\n\n"
        text += f"📊 Строк всего: `{len(lines)}`\n"
        text += f"✅ Прокси распознано: `{len(proxies)}`\n"
        if mobile_count > 0:
            text += f"📱 Мобильных: `{mobile_count}`\n"
        if len(errors) > 0:
            text += f"❌ Ошибок: `{len(errors)}`"
        text += errors_text
        
        await message.answer(
            text,
            reply_markup=get_proxy_import_confirm_keyboard(),
            parse_mode="Markdown"
        )
    
    except Exception as e:
        logger.error(f"Error processing proxies file: {e}")
        error_msg = str(e)[:200] if len(str(e)) > 200 else str(e)
        await message.answer(
            f"*Ошибка обработки файла*\n\n{error_msg}",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="◀️ Назад", callback_data="admin:proxies")]
            ]),
            parse_mode="Markdown"
        )
        await state.clear()


@router.callback_query(F.data == "admin:proxies:import:confirm")
async def confirm_proxy_import(callback: CallbackQuery, state: FSMContext):
    """Подтверждает и выполняет импорт прокси."""
    user_data = await get_user_data(callback.from_user.id)
    if not user_data or not user_data[2]:
        await callback.answer("Доступ запрещён", show_alert=True)
        return
    
    data = await state.get_data()
    proxies = data.get("parsed_proxies", [])
    
    if not proxies:
        await callback.answer("Нет прокси для импорта", show_alert=True)
        return
    
    # ИСПРАВЛЕНО: Сразу отвечаем на callback query, чтобы избежать истечения таймаута
    await callback.answer()
    
    try:
        await callback.message.edit_text("Импорт и проверка прокси... Это может занять время.")
        
        result = await api_client.import_proxies(proxies)
        
        total = result.get('total', 0)
        inserted = result.get('inserted', 0)
        updated = result.get('updated', 0)
        
        # ИСПРАВЛЕНО: Убраны статусы - все прокси считаются рабочими
        text = "✅ *Импорт прокси завершён*\n"
        text += "━━━━━━━━━━━━━━━━━━━━\n\n"
        text += f"📦 Всего: `{total}`\n"
        text += f"📥 Новых: `{inserted}`\n"
        text += f"🔄 Обновлено: `{updated}`\n"
        
        keyboard = [
            [InlineKeyboardButton(text="◀️ Назад", callback_data="admin:proxies")]
        ]
        
        await callback.message.edit_text(
            text,
            reply_markup=InlineKeyboardMarkup(inline_keyboard=keyboard),
            parse_mode="Markdown"
        )
        await state.clear()
    except Exception as e:
        logger.error(f"Error importing proxies: {e}")
        error_msg = str(e)[:150] if len(str(e)) > 150 else str(e)
        await callback.answer(f"Ошибка импорта: {error_msg}", show_alert=True)


@router.callback_query(F.data == "admin:resources")
async def resources_menu(callback: CallbackQuery):
    """Показывает мониторинг ресурсов."""
    user_data = await get_user_data(callback.from_user.id)
    if not user_data or not user_data[2]:
        await callback.answer("Доступ запрещён", show_alert=True)
        return
    
    try:
        stats = await api_client.get_resources_stats()
        
        accounts = stats.get("accounts", {})
        proxies = stats.get("proxies", {})
        today = stats.get("today", {})
        
        text = "📊 *Мониторинг ресурсов*\n"
        text += "━━━━━━━━━━━━━━━━━━━━\n\n"
        
        # Аккаунты с процентом здоровья
        accounts_total = accounts.get('total', 0)
        accounts_active = accounts.get('active', 0)
        accounts_dead = accounts.get('dead', 0)
        
        text += "🔑 *Аккаунты:*\n"
        text += f"✅ Активные: `{accounts_active}`\n"
        text += f"💀 Мёртвые: `{accounts_dead}`\n"
        text += f"📦 Всего: `{accounts_total}`\n"
        if accounts_total > 0:
            health_percent = int((accounts_active / accounts_total) * 100)
            health_emoji = "🟢" if health_percent >= 80 else "🟡" if health_percent >= 50 else "🔴"
            text += f"{health_emoji} Здоровье: `{health_percent}%`\n"
        text += "\n"
        
        # Прокси
        # ИСПРАВЛЕНО: Убраны статусы - все прокси считаются рабочими
        proxies_total = proxies.get('total', 0)
        
        text += "🌐 *Прокси:*\n"
        text += f"📦 Всего: `{proxies_total}`\n"
        text += "\n"
        
        # Активность сегодня
        comments_today = today.get('comments', 0)
        likes_today = today.get('likes', 0)
        errors_today = today.get('errors', 0)
        
        text += "📅 *Активность сегодня:*\n"
        text += f"💬 Комментарии: `{comments_today}`\n"
        text += f"❤️ Лайки: `{likes_today}`\n"
        text += f"⚠️ Ошибки: `{errors_today}`"
        
        # Предупреждения
        warnings = []
        if accounts_dead > accounts_total * 0.2 and accounts_total > 0:
            warnings.append(f"⚠️ Много мёртвых аккаунтов ({accounts_dead})")
        # ИСПРАВЛЕНО: Убрана проверка плохих прокси - все прокси считаются рабочими
        if errors_today > 10:
            warnings.append(f"⚠️ Много ошибок сегодня ({errors_today})")
        
        if warnings:
            text += "\n\n*⚠️ Предупреждения:*\n"
            for warning in warnings:
                text += f"{warning}\n"
        
        await callback.message.edit_text(
            text,
            reply_markup=get_resources_keyboard(),
            parse_mode="Markdown"
        )
        await callback.answer()
    except Exception as e:
        logger.error(f"Error getting resources stats: {e}")
        await callback.answer("Ошибка загрузки статистики", show_alert=True)


@router.callback_query(F.data == "admin:resources:clean:accounts")
async def clean_dead_accounts(callback: CallbackQuery):
    """Очищает мёртвые аккаунты."""
    user_data = await get_user_data(callback.from_user.id)
    if not user_data or not user_data[2]:
        await callback.answer("Доступ запрещён", show_alert=True)
        return
    
    from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton
    
    try:
        dead_accounts = await api_client.list_accounts(status="dead")
        dead_count = len(dead_accounts)
        
        if dead_count == 0:
            await callback.answer("Нет мёртвых аккаунтов для удаления", show_alert=True)
            return
        
        text = "⚠️ *Подтверждение удаления*\n"
        text += "━━━━━━━━━━━━━━━━━━━━\n\n"
        text += f"💀 Будет удалено: `{dead_count}` мёртвых аккаунтов\n\n"
        text += "⚠️ *Это действие необратимо!*"
        
        await callback.message.edit_text(
            text,
            parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                [
                    InlineKeyboardButton(text="✅ Удалить", callback_data="admin:resources:clean:accounts:confirm"),
                    InlineKeyboardButton(text="❌ Отмена", callback_data="admin:resources")
                ]
            ])
        )
        await callback.answer()
    except Exception as e:
        logger.error(f"Error checking dead accounts: {e}")
        await callback.answer("Ошибка проверки аккаунтов", show_alert=True)


@router.callback_query(F.data == "admin:resources:clean:accounts:confirm")
async def confirm_clean_dead_accounts(callback: CallbackQuery):
    """Подтверждает удаление мёртвых аккаунтов."""
    user_data = await get_user_data(callback.from_user.id)
    if not user_data or not user_data[2]:
        await callback.answer("Доступ запрещён", show_alert=True)
        return
    
    try:
        # ИСПРАВЛЕНО: Логируем критическую операцию перед выполнением
        dead_accounts = await api_client.list_accounts(status="dead", limit=1000)
        dead_count = len(dead_accounts)
        
        result = await api_client.delete_dead_accounts()
        deleted = result.get("deleted_count", 0)
        
        # ИСПРАВЛЕНО: Логируем критическую операцию
        await log_admin_action(
            admin_telegram_id=callback.from_user.id,
            action_type="delete_accounts",
            details={"deleted_count": deleted, "total_dead": dead_count},
            description=f"Удаление {deleted} мёртвых аккаунтов"
        )
        
        text = "✅ *Удаление завершено*\n"
        text += "━━━━━━━━━━━━━━━━━━━━\n\n"
        text += f"💀 Удалено: `{deleted}` мёртвых аккаунтов"
        
        await callback.message.edit_text(
            text,
            parse_mode="Markdown",
            reply_markup=get_resources_keyboard()
        )
        await callback.answer("Мёртвые аккаунты удалены")
    except Exception as e:
        logger.error(f"Error deleting dead accounts: {e}")
        await callback.answer("Ошибка удаления аккаунтов", show_alert=True)


# ИСПРАВЛЕНО: Удалены handlers clean_bad_proxies и confirm_clean_bad_proxies - все прокси считаются рабочими

@router.callback_query(F.data == "admin:resources:dead:accounts")
async def list_dead_accounts(callback: CallbackQuery):
    """Показывает список мёртвых аккаунтов."""
    try:
        await callback.answer()
        user_data = await get_user_data(callback.from_user.id)
        if not user_data or not user_data[2]:
            await callback.answer("Доступ запрещён", show_alert=True)
            return
        
        from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton
        
        dead_accounts = await api_client.list_accounts(status="dead")
        
        if not dead_accounts:
            await callback.message.edit_text(
                "✅ *Мёртвых аккаунтов нет!*\n\n"
                "Все аккаунты в порядке.",
                reply_markup=get_resources_keyboard(),
                parse_mode="Markdown"
            )
            return
        
        text = "💀 *Мёртвые аккаунты*\n"
        text += "━━━━━━━━━━━━━━━━━━━━\n\n"
        text += f"📊 Всего: `{len(dead_accounts)}`\n\n"
        
        if dead_accounts:
            for idx, acc in enumerate(dead_accounts[:25], 1):
                label = acc.get("label", f"#{acc.get('id')}")[:25]
                text += f"`{idx:2}.` {label}\n"
            
            if len(dead_accounts) > 25:
                text += f"\n_... и ещё {len(dead_accounts) - 25} аккаунтов_"
        
        keyboard = InlineKeyboardMarkup(inline_keyboard=[
            [
                InlineKeyboardButton(text="🔄 Проверить", callback_data="admin:accounts:recheck_dead"),
                InlineKeyboardButton(text="🗑 Удалить", callback_data="admin:resources:clean:accounts"),
                InlineKeyboardButton(text="◀️", callback_data="admin:resources")
            ]
        ])
        
        await callback.message.edit_text(text, reply_markup=keyboard, parse_mode="Markdown")
        
    except Exception as e:
        logger.error(f"Error listing dead accounts: {e}", exc_info=True)
        await callback.answer("Ошибка загрузки списка", show_alert=True)


# ИСПРАВЛЕНО: Удалены handlers list_bad_proxies и replace_slow_proxies_handler - все прокси считаются рабочими
