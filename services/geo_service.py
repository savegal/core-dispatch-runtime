"""
Сервис для определения географического местоположения аккаунтов и прокси.
"""
import logging
import asyncio
import aiohttp
from typing import Optional, Tuple
from urllib.parse import urlparse
from services.log_sanitizer import sanitize_url

logger = logging.getLogger("services.geo_service")

# Бесплатные API для определения гео (без ключей)
GEO_API_URLS = [
    "https://ipapi.co/{ip}/json/",  # Бесплатный, до 1000 запросов/день
]
PROXY_PUBLIC_IP_URL = "https://api.ipify.org?format=json"


async def detect_proxy_country(proxy_str: str, timeout: float = 10.0) -> Optional[str]:
    """
    Определяет страну прокси, делая запрос через прокси к API определения гео.
    
    Args:
        proxy_str: строка прокси
        timeout: таймаут в секундах
        
    Returns:
        str: код страны (ISO 3166-1 alpha-2, например "US", "UA") или None
    """
    from ipaddress import ip_address
    from services.proxy_normalizer import ProxyRoute
    from aiohttp_socks import ProxyConnector

    try:
        route = ProxyRoute.parse(proxy_str)
        if route.scheme == "socks5":
            connector = ProxyConnector.from_url(route.url)
            request_kwargs = {}
        else:
            connector = aiohttp.TCPConnector()
            request_kwargs = {"proxy": route.url}
        async with asyncio.timeout(timeout):
            async with aiohttp.ClientSession(connector=connector, trust_env=False,
                                             timeout=aiohttp.ClientTimeout(total=timeout)) as session:
                async with session.get(PROXY_PUBLIC_IP_URL, **request_kwargs) as response:
                    if response.status != 200:
                        return None
                    ip = str(ip_address((await response.json()).get("ip", "")))
                for template in GEO_API_URLS:
                    try:
                        # Reuse the same routed session for both public IP and
                        # country lookup. No direct fallback is allowed.
                        async with session.get(template.format(ip=ip), **request_kwargs) as response:
                            if response.status != 200:
                                continue
                            data = await response.json()
                        if data.get("status") in {"fail", "error"} or data.get("success") is False:
                            continue
                        country = str(data.get("country_code") or data.get("countryCode") or data.get("country") or "").upper()
                        if len(country) == 2 and country.isascii() and country.isalpha():
                            logger.info("Detected proxy %s country: %s", sanitize_url(proxy_str, max_length=80), country)
                            return country
                    except (aiohttp.ClientError, ValueError, TypeError):
                        continue
    except Exception as exc:
        logger.warning("Proxy geo lookup failed for %s: %s", sanitize_url(str(proxy_str or ""), max_length=80), type(exc).__name__)
    return None


async def detect_account_country_from_cookies(cookies_str: str) -> Optional[str]:
    """
    Пытается определить страну аккаунта из cookies или других источников.
    
    Пока что возвращает None, т.к. TikTok не хранит явную страну в cookies.
    В будущем можно:
    - Парсить locale из cookies
    - Использовать IP при первом входе
    - Определять через API TikTok (если доступно)
    
    Args:
        cookies_str: JSON строка с cookies
        
    Returns:
        str: код страны или None
    """
    # TODO: Реализовать определение страны из cookies или через API
    # Пока возвращаем None - страна должна быть установлена вручную или через определение прокси
    return None


async def detect_account_country_via_proxy(proxy_str: str, timeout: float = 10.0) -> Optional[str]:
    """
    Определяет страну аккаунта через прокси (используя его IP).
    
    Args:
        proxy_str: строка прокси аккаунта
        timeout: таймаут в секундах
        
    Returns:
        str: код страны или None
    """
    if not proxy_str:
        return None
    
    return await detect_proxy_country(proxy_str, timeout)


def normalize_country_code(country_code: Optional[str]) -> Optional[str]:
    """
    Нормализует код страны к формату ISO 3166-1 alpha-2 (2 заглавные буквы).
    
    Args:
        country_code: код страны (может быть в разных форматах)
        
    Returns:
        str: нормализованный код (2 заглавные буквы) или None
    """
    if not country_code:
        return None
    
    # Убираем пробелы и берём первые 2 символа
    code = str(country_code).strip().upper()[:2]
    
    # Проверяем что это буквы
    if len(code) == 2 and code.isalpha():
        return code
    
    return None
