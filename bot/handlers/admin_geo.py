"""Обработчики для управления гео-прокси и аккаунтами."""
from aiogram import Router, F
from aiogram.types import CallbackQuery, InlineKeyboardMarkup, InlineKeyboardButton
from bot.services.api_client import api_client
from bot.keyboards.admin import get_geo_menu_keyboard, get_admin_menu_keyboard
from bot.handlers.admin import get_user_data
import logging
import asyncio
from collections import Counter
from typing import Dict, List, Tuple, Optional

logger = logging.getLogger("bot.admin_geo")
router = Router()

# Эмодзи для стран
COUNTRY_EMOJIS = {
    "US": "🇺🇸", "UA": "🇺🇦", "GB": "🇬🇧", "DE": "🇩🇪", "FR": "🇫🇷",
    "IT": "🇮🇹", "ES": "🇪🇸", "PL": "🇵🇱", "RU": "🇷🇺", "CA": "🇨🇦",
    "AU": "🇦🇺", "JP": "🇯🇵", "KR": "🇰🇷", "CN": "🇨🇳", "BR": "🇧🇷",
    "MX": "🇲🇽", "IN": "🇮🇳", "NL": "🇳🇱", "SE": "🇸🇪", "NO": "🇳🇴",
    "DK": "🇩🇰", "FI": "🇫🇮", "TR": "🇹🇷", "SA": "🇸🇦", "AE": "🇦🇪",
    "SG": "🇸🇬", "MY": "🇲🇾", "TH": "🇹🇭", "VN": "🇻🇳", "PH": "🇵🇭",
    "ID": "🇮🇩", "NZ": "🇳🇿", "ZA": "🇿🇦", "EG": "🇪🇬", "AR": "🇦🇷",
    "CL": "🇨🇱", "CO": "🇨🇴", "PE": "🇵🇪", "CH": "🇨🇭", "AT": "🇦🇹",
    "BE": "🇧🇪", "PT": "🇵🇹", "GR": "🇬🇷", "IE": "🇮🇪", "CZ": "🇨🇿",
    "HU": "🇭🇺", "RO": "🇷🇴", "BG": "🇧🇬", "HR": "🇭🇷", "SK": "🇸🇰",
    "SI": "🇸🇮", "LT": "🇱🇹", "LV": "🇱🇻", "EE": "🇪🇪", "KZ": "🇰🇿",
    "BY": "🇧🇾", "UZ": "🇺🇿", "GE": "🇬🇪", "AM": "🇦🇲", "AZ": "🇦🇿"
}


def _create_proxy_lookup(proxies: List[dict]) -> Dict[str, dict]:
    """Создаёт словарь для быстрого поиска прокси по строке (O(1) вместо O(n))."""
    lookup = {}
    for proxy in proxies:
        proxy_str = proxy.get("proxy", "")
        if proxy_str:
            # Нормализуем и добавляем в словарь
            normalized = proxy_str.strip().lower()
            lookup[normalized] = proxy
            # Также добавляем первые 30 символов для частичного совпадения
            if len(normalized) > 30:
                lookup[normalized[:30]] = proxy
    return lookup


def _find_proxy_for_account(proxy_string: str, proxies: List[dict], lookup: Optional[Dict[str, dict]] = None) -> dict | None:
    """Находит прокси в списке по строке (оптимизированный поиск)."""
    if not proxy_string:
        return None
    
    # Используем предварительно созданный lookup если есть
    if lookup is not None:
        normalized = proxy_string.strip().lower()
        return lookup.get(normalized) or lookup.get(normalized[:30])
    
    # Fallback: линейный поиск (если lookup не предоставлен)
    proxy_normalized = proxy_string.strip().lower()
    for proxy in proxies:
        proxy_str = proxy.get("proxy", "")
        if not proxy_str:
            continue
        if proxy_str.lower() == proxy_normalized or proxy_str.lower().startswith(proxy_normalized[:30]):
            return proxy
    
    return None


async def _fetch_geo_data() -> Tuple[List[dict], List[dict]]:
    """Получает данные прокси и аккаунтов параллельно с обработкой ошибок."""
    try:
        # ИСПРАВЛЕНО: Убрана фильтрация по статусу прокси - все прокси считаются рабочими
        proxies, accounts = await asyncio.gather(
            api_client.list_proxies(limit=1000),
            api_client.list_accounts(status="active", limit=1000),
            return_exceptions=True
        )
        
        if isinstance(proxies, Exception):
            logger.error(f"Error fetching proxies: {proxies}", exc_info=True)
            raise proxies
        if isinstance(accounts, Exception):
            logger.error(f"Error fetching accounts: {accounts}", exc_info=True)
            raise accounts
        
        # Валидация: убеждаемся, что это списки
        if not isinstance(proxies, list):
            proxies = []
        if not isinstance(accounts, list):
            accounts = []
        
        return proxies, accounts
    except Exception as e:
        logger.error(f"Error in _fetch_geo_data: {e}", exc_info=True)
        raise


def _safe_percent(numerator: int, denominator: int) -> int:
    """Безопасное вычисление процента (избегает деления на ноль)."""
    if denominator == 0:
        return 0
    return (numerator * 100) // denominator


@router.callback_query(F.data == "admin:geo")
async def geo_menu(callback: CallbackQuery):
    """Показывает меню гео-аналитики с быстрой статистикой."""
    # ИСПРАВЛЕНО: Убрана дублирующая проверка прав - middleware уже проверяет
    
    try:
        if not callback.message:
            await callback.answer("Ошибка: сообщение не найдено", show_alert=True)
            return
        
        # Быстрая статистика для главного экрана
        try:
            proxies, accounts = await _fetch_geo_data()
            proxy_countries = set(p.get("country_code") for p in proxies if p.get("country_code"))
            account_countries = set(a.get("country_code") for a in accounts if a.get("country_code"))
            matched = len(proxy_countries & account_countries)
            
            text = (
                "🌍 *Гео-аналитика*\n\n"
                f"📊 *Быстрый обзор:*\n"
                f"• Прокси: {len(proxies)} активных ({len(proxy_countries)} стран)\n"
                f"• Аккаунты: {len(accounts)} активных ({len(account_countries)} стран)\n"
                f"• Совпадения: {matched} стран\n\n"
                f"Выберите раздел для детального анализа:"
            )
        except Exception:
            text = "🌍 *Гео-аналитика*\n\nВыберите раздел для просмотра гео-информации."
        
        await callback.message.edit_text(
            text,
            reply_markup=get_geo_menu_keyboard(),
            parse_mode="Markdown"
        )
        await callback.answer()
    except Exception as e:
        logger.error(f"Error showing geo menu: {e}", exc_info=True)
        try:
            await callback.answer("Ошибка", show_alert=True)
        except Exception:
            pass


@router.callback_query(F.data == "admin:geo:stats")
async def geo_stats(callback: CallbackQuery):
    """Показывает компактную статистику по странам в табличном формате."""
    user_data = await get_user_data(callback.from_user.id)
    if not user_data or not user_data[2]:
        await callback.answer("Доступ запрещён", show_alert=True)
        return
    
    try:
        await callback.message.edit_text("⏳ Загрузка...")
        await callback.answer()
        
        proxies, accounts = await _fetch_geo_data()
        
        proxy_countries = Counter(p.get("country_code") for p in proxies if p.get("country_code"))
        account_countries = Counter(a.get("country_code") for a in accounts if a.get("country_code"))
        
        # Все уникальные страны
        all_countries = sorted(set(list(proxy_countries.keys()) + list(account_countries.keys())))
        
        proxies_with_geo = sum(1 for p in proxies if p.get("country_code"))
        accounts_with_geo = sum(1 for a in accounts if a.get("country_code"))
        
        text = "📊 *Гео-статистика*\n\n"
        
        # Показываем ВСЕ страны с количеством аккаунтов и прокси
        if all_countries:
            # Сортируем по количеству аккаунтов (по убыванию)
            sorted_countries = sorted(
                all_countries,
                key=lambda c: account_countries.get(c, 0),
                reverse=True
            )
            
            text += "`Страна │ Прокси │ Аккаунты`\n"
            text += "`───────┼────────┼─────────`\n"
            
            # Показываем все страны (не только топ-15)
            for country in sorted_countries:
                emoji = COUNTRY_EMOJIS.get(country, "🌍")
                proxy_count = proxy_countries.get(country, 0)
                account_count = account_countries.get(country, 0)
                # Убираем эмодзи из моноширинного блока для лучшей читаемости
                text += f"`{country:5} │ {proxy_count:6} │ {account_count:8}` {emoji}\n"
        else:
            text += "❌ Нет данных о гео\n"
            text += "\n💡 *Подсказка:* Гео определяется автоматически из:\n"
            text += "• Прокси: по IP-адресу\n"
            text += "• Аккаунты: из cookies, user-agent или прокси\n"
        
        # Итоги с подробной информацией
        text += f"\n📈 *Итого:*\n"
        text += f"• Прокси: {proxies_with_geo}/{len(proxies)} с гео ({_safe_percent(proxies_with_geo, len(proxies))}%)\n"
        text += f"• Аккаунты: {accounts_with_geo}/{len(accounts)} с гео ({_safe_percent(accounts_with_geo, len(accounts))}%)\n"
        text += f"• Уникальных стран: {len(all_countries)}\n"
        
        # Предупреждение если гео не определено
        if proxies_with_geo < len(proxies) or accounts_with_geo < len(accounts):
            text += f"\n⚠️ *Внимание:*\n"
            if proxies_with_geo < len(proxies):
                text += f"• {len(proxies) - proxies_with_geo} прокси без гео\n"
            if accounts_with_geo < len(accounts):
                text += f"• {len(accounts) - accounts_with_geo} аккаунтов без гео\n"
            text += f"Гео определяется автоматически при импорте.\n"
        
        await callback.message.edit_text(
            text,
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="🔄 Обновить", callback_data="admin:geo:stats")],
                [InlineKeyboardButton(text="◀️ Назад", callback_data="admin:geo")]
            ]),
            parse_mode="Markdown"
        )
    except Exception as e:
        logger.error(f"Error getting geo stats: {e}", exc_info=True)
        try:
            if callback.message:
                await callback.message.edit_text(
                    f"❌ Ошибка: {str(e)[:150]}",
                    reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                        [InlineKeyboardButton(text="◀️ Назад", callback_data="admin:geo")]
                    ])
                )
        except Exception:
            pass
        try:
            await callback.answer("Ошибка", show_alert=True)
        except Exception:
            pass


@router.callback_query(F.data == "admin:geo:coverage")
async def geo_coverage(callback: CallbackQuery):
    """Показывает компактное покрытие стран."""
    user_data = await get_user_data(callback.from_user.id)
    if not user_data or not user_data[2]:
        await callback.answer("Доступ запрещён", show_alert=True)
        return
    
    try:
        await callback.message.edit_text("⏳ Анализ...")
        await callback.answer()
        
        proxies, accounts = await _fetch_geo_data()
        
        proxy_countries = set(p.get("country_code") for p in proxies if p.get("country_code"))
        account_countries = set(a.get("country_code") for a in accounts if a.get("country_code"))
        
        missing_accounts = proxy_countries - account_countries
        missing_proxies = account_countries - proxy_countries
        matched = proxy_countries & account_countries
        
        text = "📈 *Покрытие стран*\n\n"
        
        # Подсчитываем количество для каждой страны
        proxy_countries_count = Counter(p.get("country_code") for p in proxies if p.get("country_code"))
        account_countries_count = Counter(a.get("country_code") for a in accounts if a.get("country_code"))
        
        # Показываем ВСЕ совпадения с количествами
        text += f"✅ *Совпадения ({len(matched)} стран):*\n"
        if matched:
            # Сортируем по количеству аккаунтов (по убыванию)
            matched_list = sorted(
                matched,
                key=lambda c: account_countries_count.get(c, 0),
                reverse=True
            )
            # Показываем ВСЕ совпадения (не только топ-15)
            for country in matched_list:
                emoji = COUNTRY_EMOJIS.get(country, "🌍")
                proxy_count = proxy_countries_count.get(country, 0)
                account_count = account_countries_count.get(country, 0)
                text += f"{emoji} *{country}*: {proxy_count} прокси, {account_count} аккаунтов\n"
        else:
            text += "Нет совпадений\n"
        text += "\n"
        
        if missing_accounts:
            # Сортируем по количеству прокси (по убыванию)
            missing_acc_list = sorted(
                missing_accounts,
                key=lambda c: proxy_countries_count.get(c, 0),
                reverse=True
            )
            text += f"⚠️ *Прокси без аккаунтов ({len(missing_accounts)} стран):*\n"
            # Показываем ВСЕ страны (не только топ-10)
            for country in missing_acc_list:
                emoji = COUNTRY_EMOJIS.get(country, "🌍")
                proxy_count = proxy_countries_count.get(country, 0)
                text += f"{emoji} *{country}*: {proxy_count} прокси\n"
            text += "\n"
        
        if missing_proxies:
            # Сортируем по количеству аккаунтов (по убыванию)
            missing_proxy_list = sorted(
                missing_proxies,
                key=lambda c: account_countries_count.get(c, 0),
                reverse=True
            )
            text += f"❌ *Аккаунты без прокси ({len(missing_proxies)} стран):*\n"
            # Показываем ВСЕ страны (не только топ-10)
            for country in missing_proxy_list:
                emoji = COUNTRY_EMOJIS.get(country, "🌍")
                account_count = account_countries_count.get(country, 0)
                text += f"{emoji} *{country}*: {account_count} аккаунтов\n"
            text += "\n"
        
        # Итоги
        all_unique_countries = len(proxy_countries | account_countries)
        coverage_percent = _safe_percent(len(matched), all_unique_countries) if all_unique_countries > 0 else 0
        
        text += f"📊 *Итого:*\n"
        text += f"• Прокси: {len(proxy_countries)} стран\n"
        text += f"• Аккаунты: {len(account_countries)} стран\n"
        text += f"• Совпадения: {len(matched)} из {all_unique_countries} ({coverage_percent}% покрытие)\n"
        
        await callback.message.edit_text(
            text,
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="🔄 Обновить", callback_data="admin:geo:coverage")],
                [InlineKeyboardButton(text="◀️ Назад", callback_data="admin:geo")]
            ]),
            parse_mode="Markdown"
        )
    except Exception as e:
        logger.error(f"Error getting geo coverage: {e}", exc_info=True)
        try:
            if callback.message:
                await callback.message.edit_text(
                    f"❌ Ошибка: {str(e)[:150]}",
                    reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                        [InlineKeyboardButton(text="◀️ Назад", callback_data="admin:geo")]
                    ])
                )
        except Exception:
            pass
        try:
            await callback.answer("Ошибка", show_alert=True)
        except Exception:
            pass


@router.callback_query(F.data == "admin:geo:recommendation")
async def geo_recommendation(callback: CallbackQuery):
    """Показывает рекомендации по гео-подбору."""
    user_data = await get_user_data(callback.from_user.id)
    if not user_data or not user_data[2]:
        await callback.answer("Доступ запрещён", show_alert=True)
        return
    
    try:
        await callback.message.edit_text("⏳ Анализ рекомендаций...")
        await callback.answer()
        
        # ОПТИМИЗАЦИЯ: Используем общую функцию и создаём lookup для быстрого поиска
        proxies, accounts = await _fetch_geo_data()
        proxy_lookup = _create_proxy_lookup(proxies)
        
        # Аккаунты без прокси
        accounts_without_proxy = [a for a in accounts if not a.get("proxy")]
        # Аккаунты с прокси из другой страны
        accounts_with_mismatch = []
        for acc in accounts:
            acc_country = acc.get("country_code")
            acc_proxy = acc.get("proxy", "")
            if acc_country and acc_proxy:
                # ОПТИМИЗАЦИЯ: Используем lookup для O(1) поиска
                proxy_obj = _find_proxy_for_account(acc_proxy, proxies, proxy_lookup)
                if proxy_obj:
                    proxy_country = proxy_obj.get("country_code")
                    if proxy_country and proxy_country != acc_country:
                        accounts_with_mismatch.append((acc, proxy_country))
        
        # Статистика совпадений
        matched_count = 0
        accounts_with_proxy = [a for a in accounts if a.get("proxy")]
        for acc in accounts_with_proxy:
            acc_country = acc.get("country_code")
            acc_proxy = acc.get("proxy", "")
            if acc_country and acc_proxy:
                proxy_obj = _find_proxy_for_account(acc_proxy, proxies, proxy_lookup)
                if proxy_obj and proxy_obj.get("country_code") == acc_country:
                    matched_count += 1
        
        total_with_proxy = len(accounts_with_proxy)
        match_percent = _safe_percent(matched_count, total_with_proxy)
        
        text = "💡 *Рекомендации*\n\n"
        
        # Компактная статистика
        text += f"📊 *Текущее состояние:*\n"
        text += f"• Аккаунты с прокси: {total_with_proxy}/{len(accounts)}\n"
        if total_with_proxy > 0:
            text += f"• Гео-совпадения: {matched_count}/{total_with_proxy} ({match_percent}%)\n"
        else:
            text += f"• Гео-совпадения: нет данных (нет аккаунтов с прокси)\n"
        text += "\n"
        
        # Проблемы и рекомендации
        issues = []
        if accounts_without_proxy:
            issues.append(f"⚠️ {len(accounts_without_proxy)} аккаунтов без прокси")
        if accounts_with_mismatch:
            issues.append(f"🌍 {len(accounts_with_mismatch)} несовпадений гео")
        if match_percent < 70 and total_with_proxy > 0:
            issues.append(f"📉 Низкий процент совпадений ({match_percent}%)")
        
        if issues:
            text += "*⚠️ Проблемы:*\n"
            for issue in issues:
                text += f"{issue}\n"
            text += "\n*💡 Рекомендации:*\n"
            if accounts_without_proxy:
                text += "• Назначить прокси аккаунтам (кнопка ниже)\n"
            if accounts_with_mismatch or (match_percent < 70 and total_with_proxy > 0):
                text += "• Включить гео-подбор в настройках\n"
        else:
            if total_with_proxy == 0:
                text += "⚠️ *Нет аккаунтов с прокси*\n"
                text += "Рекомендуется назначить прокси аккаунтам.\n"
            else:
                text += "🎉 *Всё отлично!*\nГео-подбор работает корректно."
        
        # Клавиатура с умными кнопками
        keyboard_buttons = []
        if accounts_without_proxy or (total_with_proxy == 0):
            keyboard_buttons.append([InlineKeyboardButton(text="🌐 Назначить прокси", callback_data="admin:accounts:assign_proxies")])
        keyboard_buttons.append([InlineKeyboardButton(text="⚙️ Настройки", callback_data="admin:geo:settings")])
        keyboard_buttons.append([InlineKeyboardButton(text="◀️ Назад", callback_data="admin:geo")])
        
        await callback.message.edit_text(
            text,
            reply_markup=InlineKeyboardMarkup(inline_keyboard=keyboard_buttons),
            parse_mode="Markdown"
        )
    except Exception as e:
        logger.error(f"Error getting geo recommendations: {e}", exc_info=True)
        try:
            if callback.message:
                await callback.message.edit_text(
                    f"❌ Ошибка: {str(e)[:200]}",
                    reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                        [InlineKeyboardButton(text="◀️ Назад", callback_data="admin:geo")]
                    ])
                )
        except Exception:
            pass
        try:
            await callback.answer("Ошибка", show_alert=True)
        except Exception:
            pass


@router.callback_query(F.data == "admin:geo:summary")
async def geo_summary(callback: CallbackQuery):
    """Показывает сводку по гео."""
    user_data = await get_user_data(callback.from_user.id)
    if not user_data or not user_data[2]:
        await callback.answer("Доступ запрещён", show_alert=True)
        return
    
    try:
        await callback.message.edit_text("⏳ Формирование сводки...")
        await callback.answer()
        
        # ОПТИМИЗАЦИЯ: Используем общую функцию и создаём lookup
        proxies, accounts = await _fetch_geo_data()
        proxy_lookup = _create_proxy_lookup(proxies)
        
        proxies_with_geo = sum(1 for p in proxies if p.get("country_code"))
        accounts_with_geo = sum(1 for a in accounts if a.get("country_code"))
        accounts_with_proxy_list = [a for a in accounts if a.get("proxy")]
        accounts_with_proxy = len(accounts_with_proxy_list)
        
        # ОПТИМИЗАЦИЯ: Подсчитываем совпадения с lookup для O(1) поиска
        matched = 0
        for acc in accounts_with_proxy_list:
            acc_country = acc.get("country_code")
            acc_proxy = acc.get("proxy", "")
            if acc_country and acc_proxy:
                proxy_obj = _find_proxy_for_account(acc_proxy, proxies, proxy_lookup)
                if proxy_obj and proxy_obj.get("country_code") == acc_country:
                    matched += 1
        
        from config.settings import get_settings
        settings = get_settings()
        geo_matching_status = "✅" if settings.PROXY_GEO_MATCHING else "❌"
        geo_fallback_status = "✅" if settings.PROXY_GEO_FALLBACK else "❌"
        
        match_percent = _safe_percent(matched, accounts_with_proxy) if accounts_with_proxy > 0 else 0
        
        text = "📋 *Гео-сводка*\n\n"
        
        # Улучшенное оформление без кривой таблицы
        text += "🌐 *Прокси:*\n"
        text += f"• Всего: `{len(proxies)}`\n"
        text += f"• С гео: `{proxies_with_geo}` ({_safe_percent(proxies_with_geo, len(proxies))}%)\n"
        if proxies_with_geo < len(proxies):
            text += f"• Без гео: `{len(proxies) - proxies_with_geo}`\n"
        text += "\n"
        
        text += "🔑 *Аккаунты:*\n"
        text += f"• Всего: `{len(accounts)}`\n"
        text += f"• С гео: `{accounts_with_geo}` ({_safe_percent(accounts_with_geo, len(accounts))}%)\n"
        if accounts_with_geo < len(accounts):
            text += f"• Без гео: `{len(accounts) - accounts_with_geo}`\n"
        text += f"• С прокси: `{accounts_with_proxy}`\n"
        text += f"• Совпадения: `{matched}` ({match_percent}%)\n"
        text += "\n"
        
        text += "⚙️ *Настройки:*\n"
        text += f"• Гео-подбор: {geo_matching_status}\n"
        text += f"• Fallback: {geo_fallback_status}\n"
        
        # Краткие рекомендации
        recommendations = []
        if proxies_with_geo < len(proxies) * 0.5:
            recommendations.append("Проверить гео прокси")
        if accounts_with_geo < len(accounts) * 0.5:
            recommendations.append("Проверить гео аккаунтов")
        if matched < accounts_with_proxy * 0.7 and accounts_with_proxy > 0:
            recommendations.append("Включить гео-подбор")
        
        if recommendations:
            text += f"\n💡 *Рекомендации:*\n"
            for rec in recommendations:
                text += f"• {rec}\n"
        else:
            text += f"\n✅ Всё настроено корректно!"
        
        keyboard_buttons = []
        
        # Кнопка "Узнать гео" если есть аккаунты/прокси без гео
        if proxies_with_geo < len(proxies) or accounts_with_geo < len(accounts):
            keyboard_buttons.append([
                InlineKeyboardButton(text="🌍 Узнать гео", callback_data="admin:geo:detect")
            ])
        
        keyboard_buttons.append([
            InlineKeyboardButton(text="🔍 Проверить прокси", callback_data="admin:proxies:validate"),
            InlineKeyboardButton(text="🌐 Назначить прокси", callback_data="admin:accounts:assign_proxies")
        ])
        keyboard_buttons.append([
            InlineKeyboardButton(text="⚙️ Настройки", callback_data="admin:geo:settings")
        ])
        keyboard_buttons.append([
            InlineKeyboardButton(text="◀️ Назад", callback_data="admin:geo")
        ])
        
        await callback.message.edit_text(
            text,
            reply_markup=InlineKeyboardMarkup(inline_keyboard=keyboard_buttons),
            parse_mode="Markdown"
        )
    except Exception as e:
        logger.error(f"Error getting geo summary: {e}", exc_info=True)
        try:
            if callback.message:
                await callback.message.edit_text(
                    f"❌ Ошибка: {str(e)[:200]}",
                    reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                        [InlineKeyboardButton(text="◀️ Назад", callback_data="admin:geo")]
                    ])
                )
        except Exception:
            pass
        try:
            await callback.answer("Ошибка", show_alert=True)
        except Exception:
            pass


@router.callback_query(F.data == "admin:geo:detect")
async def detect_geo(callback: CallbackQuery):
    """Определяет гео для всех аккаунтов и прокси без гео."""
    user_data = await get_user_data(callback.from_user.id)
    if not user_data or not user_data[2]:
        await callback.answer("Доступ запрещён", show_alert=True)
        return
    
    try:
        await callback.message.edit_text("⏳ Определение гео...\n\nЭто может занять несколько минут.")
        await callback.answer()
        
        # Запускаем определение гео
        result = await api_client.detect_all_geo()
        
        if "error" in result:
            if "already in progress" in result.get("error", "").lower():
                await callback.message.edit_text(
                    "⚠️ *Определение гео уже запущено*\n\n"
                    "Дождитесь завершения текущей проверки.",
                    reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                        [InlineKeyboardButton(text="◀️ Назад", callback_data="admin:geo")]
                    ]),
                    parse_mode="Markdown"
                )
            else:
                await callback.message.edit_text(
                    f"❌ Ошибка: {result.get('error', 'Неизвестная ошибка')}",
                    reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                        [InlineKeyboardButton(text="◀️ Назад", callback_data="admin:geo")]
                    ])
                )
            return
        
        accounts = result.get("accounts", {})
        proxies = result.get("proxies", {})
        
        text = "✅ *Определение гео завершено*\n\n"
        
        if accounts.get("total", 0) > 0:
            text += "🔑 *Аккаунты:*\n"
            text += f"• Проверено: `{accounts.get('total', 0)}`\n"
            text += f"• Определено: `{accounts.get('detected', 0)}`\n"
            if accounts.get("failed", 0) > 0:
                text += f"• Не удалось: `{accounts.get('failed', 0)}`\n"
            text += "\n"
        
        if proxies.get("total", 0) > 0:
            text += "🌐 *Прокси:*\n"
            text += f"• Проверено: `{proxies.get('total', 0)}`\n"
            text += f"• Определено: `{proxies.get('detected', 0)}`\n"
            if proxies.get("failed", 0) > 0:
                text += f"• Не удалось: `{proxies.get('failed', 0)}`\n"
            text += "\n"
        
        if accounts.get("total", 0) == 0 and proxies.get("total", 0) == 0:
            text += "ℹ️ Все аккаунты и прокси уже имеют определенное гео.\n"
        
        await callback.message.edit_text(
            text,
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="🔄 Обновить сводку", callback_data="admin:geo:summary")],
                [InlineKeyboardButton(text="◀️ Назад", callback_data="admin:geo")]
            ]),
            parse_mode="Markdown"
        )
    except Exception as e:
        logger.error(f"Error detecting geo: {e}", exc_info=True)
        try:
            if callback.message:
                await callback.message.edit_text(
                    f"❌ Ошибка: {str(e)[:200]}",
                    reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                        [InlineKeyboardButton(text="◀️ Назад", callback_data="admin:geo")]
                    ])
                )
        except Exception:
            pass
        try:
            await callback.answer("Ошибка", show_alert=True)
        except Exception:
            pass


@router.callback_query(F.data == "admin:geo:settings")
async def geo_settings(callback: CallbackQuery):
    """Показывает настройки гео-подбора."""
    user_data = await get_user_data(callback.from_user.id)
    if not user_data or not user_data[2]:
        await callback.answer("Доступ запрещён", show_alert=True)
        return
    
    try:
        from config.settings import get_settings
        settings = get_settings()
        
        geo_matching_status = "✅ Включен" if settings.PROXY_GEO_MATCHING else "❌ Выключен"
        geo_fallback_status = "✅ Включен" if settings.PROXY_GEO_FALLBACK else "❌ Выключен"
        
        text = (
            "⚙️ *Настройки гео-подбора*\n\n"
            f"`Параметр      │ Статус`\n"
            f"`──────────────┼────────`\n"
            f"`🌍 Гео-подбор  │ {geo_matching_status:6}`\n"
            f"`🔄 Fallback    │ {geo_fallback_status:6}`\n\n"
            f"*Описание:*\n"
            f"• Гео-подбор: подбор прокси из той же страны\n"
            f"• Fallback: использовать любой прокси, если нет совпадения\n\n"
            f"💡 *Изменить:* отредактируйте `.env`:\n"
            f"`PROXY_GEO_MATCHING=True/False`\n"
            f"`PROXY_GEO_FALLBACK=True/False`\n\n"
            f"_После изменения перезапустите бота и executor._"
        )
        
        await callback.message.edit_text(
            text,
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="◀️ Назад", callback_data="admin:geo")]
            ]),
            parse_mode="Markdown"
        )
        await callback.answer()
    except Exception as e:
        logger.error(f"Error showing geo settings: {e}", exc_info=True)
        try:
            if callback.message:
                await callback.message.edit_text(
                    f"❌ Ошибка: {str(e)[:200]}",
                    reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                        [InlineKeyboardButton(text="◀️ Назад", callback_data="admin:geo")]
                    ])
                )
        except Exception:
            pass
        try:
            await callback.answer("Ошибка", show_alert=True)
        except Exception:
            pass
