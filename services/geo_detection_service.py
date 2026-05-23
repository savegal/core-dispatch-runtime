"""
Сервис для определения географии аккаунтов TikTok.
"""
import json
import logging
import re
from typing import Optional, Dict, Any
from sqlalchemy.orm import Session
from db.models import Account

logger = logging.getLogger("services.geo_detection")


def detect_country_from_cookies(cookies_str: str) -> Optional[str]:
    """
    Определяет страну аккаунта из cookies.
    
    В TikTok cookies может быть информация о регионе/языке:
    - tt_chain_token может содержать информацию о регионе
    - locale cookie может указывать на язык/регион
    - домен cookies (.com, .uk, .de и т.д.)
    
    Args:
        cookies_str: JSON строка с cookies или словарь
        
    Returns:
        ISO 3166-1 alpha-2 код страны (например, "US", "UA") или None
    """
    if not cookies_str:
        return None
    
    try:
        # Парсим cookies
        cookies_data = None
        if isinstance(cookies_str, str):
            try:
                cookies_data = json.loads(cookies_str)
            except (json.JSONDecodeError, TypeError):
                # Если не JSON, пробуем как словарь из строки
                return None
        elif isinstance(cookies_str, dict):
            cookies_data = cookies_str
        elif isinstance(cookies_str, list):
            # Массив cookies
            cookies_dict = {}
            for cookie in cookies_str:
                if isinstance(cookie, dict) and "name" in cookie and "value" in cookie:
                    cookies_dict[cookie["name"]] = cookie["value"]
            cookies_data = cookies_dict
        else:
            return None
        
        if not cookies_data:
            return None
        
        # Если это массив cookies, преобразуем в словарь
        if isinstance(cookies_data, list):
            cookies_dict = {}
            for cookie in cookies_data:
                if isinstance(cookie, dict):
                    name = cookie.get("name") or cookie.get("key")
                    value = cookie.get("value")
                    if name and value:
                        cookies_dict[name] = value
            cookies_data = cookies_dict
        
        cookies_dict = cookies_data if isinstance(cookies_data, dict) else {}
        
        # Проверяем locale cookie (например, "en_US", "uk_UA", "de_DE")
        locale = cookies_dict.get("locale") or cookies_dict.get("i18next") or cookies_dict.get("lang")
        if locale:
            # Парсим locale формат (например, "en_US" -> "US", "uk_UA" -> "UA")
            locale_parts = locale.split("_")
            if len(locale_parts) >= 2:
                country_code = locale_parts[-1].upper()
                if len(country_code) == 2 and country_code.isalpha():
                    return country_code
        
        # Проверяем домен cookies (если есть в структуре)
        # TikTok использует разные домены для разных регионов, но cookies обычно на .tiktok.com
        
        # Проверяем другие возможные индикаторы
        # Например, currency cookie может указывать на регион
        currency = cookies_dict.get("currency") or cookies_dict.get("tt_currency")
        if currency:
            # Маппинг валюты к странам (базовый)
            currency_to_country = {
                "USD": "US", "EUR": "DE", "GBP": "GB", "UAH": "UA",
                "RUB": "RU", "PLN": "PL", "CAD": "CA", "AUD": "AU"
            }
            if currency.upper() in currency_to_country:
                return currency_to_country[currency.upper()]
        
    except Exception as e:
        logger.debug(f"Error detecting country from cookies: {e}")
        return None
    
    return None


def detect_country_from_user_agent(user_agent: Optional[str]) -> Optional[str]:
    """
    Пытается определить страну из User-Agent (менее надежно).
    
    Args:
        user_agent: User-Agent строка
        
    Returns:
        ISO 3166-1 alpha-2 код страны или None
    """
    if not user_agent:
        return None
    
    # User-Agent обычно не содержит явной информации о стране
    # Но можно попробовать найти языковые маркеры
    user_agent_lower = user_agent.lower()
    
    # Проверяем языковые маркеры (менее надежно)
    # Обычно не работает, но оставляем для возможных расширений
    
    return None


def detect_account_country(account: Account) -> Optional[str]:
    """
    Определяет страну аккаунта из доступных данных.
    
    Приоритет:
    1. country_code в БД (если уже установлен)
    2. cookies (locale, currency)
    3. user_agent (менее надежно)
    
    Args:
        account: объект Account
        
    Returns:
        ISO 3166-1 alpha-2 код страны или None
    """
    # Если уже есть country_code, используем его
    if account.country_code:
        return account.country_code
    
    # Пробуем определить из cookies
    if account.cookies:
        country = detect_country_from_cookies(account.cookies)
        if country:
            return country
    
    # Пробуем определить из user_agent (менее надежно)
    if account.user_agent:
        country = detect_country_from_user_agent(account.user_agent)
        if country:
            return country
    
    return None


def update_account_country_if_needed(db: Session, account: Account, force: bool = False) -> bool:
    """
    Обновляет country_code аккаунта, если он не установлен или force=True.
    
    Args:
        db: SQLAlchemy сессия
        account: объект Account (должен быть привязан к сессии)
        force: если True, обновляет даже если country_code уже установлен
        
    Returns:
        True если country_code был обновлен, False иначе
    """
    if not force and account.country_code:
        return False
    
    try:
        detected_country = detect_account_country(account)
        if detected_country and detected_country != account.country_code:
            account.country_code = detected_country
            db.commit()
            logger.info(f"Updated country_code for account {account.id} ({account.label[:20]}): {detected_country}")
            return True
    except Exception as e:
        db.rollback()
        logger.error(f"Error updating country_code for account {account.id}: {e}", exc_info=True)
        raise
    
    return False


def detect_country_from_tiktok_url(url: str) -> Optional[str]:
    """
    Пытается определить страну из URL TikTok.
    
    TikTok может использовать поддомены для разных регионов, но обычно все на www.tiktok.com.
    
    Args:
        url: URL страницы TikTok
        
    Returns:
        ISO 3166-1 alpha-2 код страны или None
    """
    if not url:
        return None
    
    # TikTok обычно использует один домен для всех регионов
    # Но можно проверить наличие региональных поддоменов (если они есть)
    url_lower = url.lower()
    
    # Примеры региональных поддоменов (если они существуют)
    region_patterns = {
        r'\.uk\.': 'GB',
        r'\.de\.': 'DE',
        r'\.fr\.': 'FR',
        r'\.jp\.': 'JP',
    }
    
    for pattern, country_code in region_patterns.items():
        if re.search(pattern, url_lower):
            return country_code
    
    return None











