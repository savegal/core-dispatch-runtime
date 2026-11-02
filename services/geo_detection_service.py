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


def _parse_any_cookies(cookies_str: Any) -> Dict[str, str]:
    if not cookies_str:
        return {}
    if isinstance(cookies_str, dict):
        if isinstance(cookies_str.get("cookies"), list):
            return _parse_any_cookies(cookies_str["cookies"])
        return {str(k): str(v) for k, v in cookies_str.items() if k and v is not None and not isinstance(v, (dict, list))}
    if isinstance(cookies_str, list):
        result: Dict[str, str] = {}
        for cookie in cookies_str:
            if isinstance(cookie, dict):
                name = cookie.get("name") or cookie.get("Name") or cookie.get("key")
                value = cookie.get("value", cookie.get("Value"))
                if name and value is not None:
                    result[str(name)] = str(value)
        return result
    if isinstance(cookies_str, str):
        raw = cookies_str.strip()
        if not raw:
            return {}
        try:
            parsed = json.loads(raw)
            return _parse_any_cookies(parsed)
        except (json.JSONDecodeError, TypeError):
            pass
        result = {}
        for part in raw.replace("; ", ";").split(";"):
            part = part.strip()
            if "=" in part:
                k, _, v = part.partition("=")
                if k.strip():
                    result[k.strip()] = v.strip()
        return result
    return {}


def detect_country_from_cookies(cookies_str: str) -> Optional[str]:
    """
    Определяет страну аккаунта из cookies.

    Проверяет:
    1. Явные региональные cookies: store-region, store_region, priority_region, region, country_code
    2. Языковые cookies: locale, i18next, lang (поддерживает en_US и en-US)
    3. Валютные cookies: currency, tt_currency
    4. Однозначные дата-центры: tt-target-idc (useast/uswest -> US, alisg -> SG)
    """
    if not cookies_str:
        return None

    try:
        cookies_dict = _parse_any_cookies(cookies_str)
        if not cookies_dict:
            return None

        lower_map = {str(k).strip().lower(): str(v).strip() for k, v in cookies_dict.items() if v is not None}

        # 1. Explicit store-region / priority_region
        for region_key in ("store-region", "store_region", "priority_region", "region", "country_code", "tt-country-code"):
            raw_region = lower_map.get(region_key)
            if raw_region:
                code = raw_region.split(",")[0].split("-")[0].split("_")[0].strip().upper()
                if len(code) == 2 and code.isalpha():
                    return code

        # 2. Locale cookie (e.g., "en_US", "en-US", "uk_UA", "de-DE")
        locale = lower_map.get("locale") or lower_map.get("i18next") or lower_map.get("lang")
        if locale:
            locale_parts = re.split(r"[_-]", locale)
            if len(locale_parts) >= 2:
                country_code = locale_parts[-1].upper()
                if len(country_code) == 2 and country_code.isalpha():
                    return country_code

        # 3. Currency cookie
        currency = lower_map.get("currency") or lower_map.get("tt_currency")
        if currency:
            currency_to_country = {
                "USD": "US", "EUR": "DE", "GBP": "GB", "UAH": "UA",
                "RUB": "RU", "PLN": "PL", "CAD": "CA", "AUD": "AU",
            }
            if currency.upper() in currency_to_country:
                return currency_to_country[currency.upper()]

        # Note: tt-target-idc is a datacenter cluster label, not a country code.

    except Exception as e:
        logger.debug(f"Error detecting country from cookies: {e}")
        return None

    return None


def detect_country_from_user_agent(user_agent: Optional[str]) -> Optional[str]:
    """
    Пытается определить страну из User-Agent (менее надежно).
    """
    if not user_agent:
        return None
    return None


def detect_account_country(account: Account) -> Optional[str]:
    """
    Определяет страну аккаунта из доступных данных.

    Приоритет:
    1. country_code в БД (если уже установлен)
    2. cookies (store-region, locale, currency, tt-target-idc)
    3. user_agent (менее надежно)
    """
    if getattr(account, "country_code", None):
        return account.country_code

    cookies_val = getattr(account, "cookies", None)
    if cookies_val:
        country = detect_country_from_cookies(cookies_val)
        if country:
            return country

    user_agent_val = getattr(account, "user_agent", None)
    if user_agent_val:
        country = detect_country_from_user_agent(user_agent_val)
        if country:
            return country

    return None


def update_account_country_if_needed(db: Session, account: Account, force: bool = False) -> bool:
    """
    Обновляет country_code аккаунта, если он не установлен или force=True.
    """
    if not force and account.country_code:
        return False

    try:
        detected_country = detect_account_country(account)
        if detected_country and detected_country != account.country_code:
            account.country_code = detected_country
            db.commit()
            logger.info(f"Updated country_code for account {account.id} ({(account.label or '')[:20]}): {detected_country}")
            return True
    except Exception as e:
        db.rollback()
        logger.error(f"Error updating country_code for account {account.id}: {e}", exc_info=True)
        raise

    return False


def detect_country_from_tiktok_url(url: str) -> Optional[str]:
    """
    Пытается определить страну из URL TikTok.
    """
    if not url:
        return None

    url_lower = url.lower()
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
