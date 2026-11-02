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


def _trusted_country_code(value: Any) -> Optional[str]:
    """Only accept an unambiguous ASCII ISO-style two-letter country value."""
    code = str(value or "").strip().upper()
    return code if re.fullmatch(r"[A-Z]{2}", code) else None


def detect_country_from_cookies(cookies_str: Any) -> Optional[str]:
    """Conservative last-resort account country from explicit region cookies.

    Language, currency, and unsigned IDC/datacenter names are not account
    country evidence. In particular tt-target-idc-sign is usually an opaque
    signature: never guess a country from its prefix, alphabet or hash.
    """
    try:
        cookies = _parse_any_cookies(cookies_str)
        values = {str(k).strip().lower(): v for k, v in cookies.items()}
        for key in ("store-region", "store_region"):
            region = _trusted_country_code(values.get(key))
            if region:
                return region
        # A provider may export a literal ISO country in this field. Accept
        # ONLY that explicit value; an opaque signature is not decodable.
        return _trusted_country_code(values.get("tt-target-idc-sign"))
    except Exception as exc:
        logger.debug("Unable to parse trusted account-region cookies: %s", exc)
    return None


def detect_country_from_user_agent(user_agent: Optional[str]) -> Optional[str]:
    """
    Пытается определить страну из User-Agent (менее надежно).
    """
    if not user_agent:
        return None
    return None


def detect_account_country(account: Account) -> Optional[str]:
    """Country evidence precedence: stored DB > authenticated Passport > SSR > cookies.

    Passport/SSR values must be recorded by their respective validated paths
    as transient evidence; a locale, proxy, currency, or header is not enough.
    """
    if account is None:
        return None
    persisted = getattr(account, "country_code", None)
    if persisted:
        return persisted
    passport = _trusted_country_code(getattr(account, "_passport_country_code", None))
    if passport:
        return passport
    ssr = _trusted_country_code(getattr(account, "_account_ssr_region", None))
    if ssr:
        return ssr
    return detect_country_from_cookies(getattr(account, "cookies", None))


def update_account_country_if_needed(db: Session, account: Account, force: bool = False) -> bool:
    """Fill missing country from trusted evidence; never replace a DB value.

    The legacy force argument is retained for API compatibility but may not
    override a manually specified or previously persisted country.
    """
    if getattr(account, "country_code", None):
        return False
    try:
        country = _trusted_country_code(detect_account_country(account))
        if not country:
            return False
        account.country_code = country
        db.commit()
        logger.info("Set trusted country_code for account %s: %s", account.id, country)
        return True
    except Exception:
        db.rollback()
        logger.exception("Error persisting account %s country_code", getattr(account, "id", "?"))
        raise


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
