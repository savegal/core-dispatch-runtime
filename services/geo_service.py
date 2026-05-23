"""
Сервис для определения географического местоположения аккаунтов и прокси.
"""
import logging
import asyncio
import aiohttp
from typing import Optional, Tuple
from urllib.parse import urlparse

logger = logging.getLogger("services.geo_service")

# Бесплатные API для определения гео (без ключей)
GEO_API_URLS = [
    "https://ipapi.co/{ip}/json/",  # Бесплатный, до 1000 запросов/день
    "https://ip-api.com/json/{ip}?fields=status,message,countryCode",  # Бесплатный, 45 req/min
]


async def detect_proxy_country(proxy_str: str, timeout: float = 10.0) -> Optional[str]:
    """
    Определяет страну прокси, делая запрос через прокси к API определения гео.
    
    Args:
        proxy_str: строка прокси
        timeout: таймаут в секундах
        
    Returns:
        str: код страны (ISO 3166-1 alpha-2, например "US", "UA") или None
    """
    try:
        from services.proxy_service import normalize_proxy_url
        from aiohttp_socks import ProxyConnector
        from aiohttp import ClientTimeout, TCPConnector
        
        proxy_url = normalize_proxy_url(proxy_str)
        parsed = urlparse(proxy_url)
        proxy_type = parsed.scheme.lower()
        
        # Создаём connector
        if proxy_type in ("socks5", "socks4"):
            connector = ProxyConnector.from_url(proxy_url)
            request_kwargs = {}
        else:
            connector = TCPConnector()
            request_kwargs = {"proxy": proxy_url}
        
        timeout_obj = ClientTimeout(total=timeout)
        
        async with aiohttp.ClientSession(connector=connector, timeout=timeout_obj) as session:
            # Пробуем разные API по очереди
            for api_url_template in GEO_API_URLS:
                try:
                    # Используем простой endpoint для получения нашего IP
                    test_url = "https://api.ipify.org?format=json"
                    async with session.get(test_url, **request_kwargs) as response:
                        if response.status == 200:
                            data = await response.json()
                            ip = data.get("ip")
                            if not ip:
                                continue
                            
                            # Теперь определяем гео по IP
                            geo_url = api_url_template.format(ip=ip)
                            
                            # Делаем запрос БЕЗ прокси (определяем гео по IP)
                            async with aiohttp.ClientSession(timeout=timeout_obj) as geo_session:
                                async with geo_session.get(geo_url) as geo_response:
                                    if geo_response.status == 200:
                                        geo_data = await geo_response.json()
                                        
                                        # Парсим ответ в зависимости от API
                                        country_code = None
                                        if "ipapi.co" in api_url_template:
                                            country_code = geo_data.get("country_code")
                                        elif "ip-api.com" in api_url_template:
                                            if geo_data.get("status") == "success":
                                                country_code = geo_data.get("countryCode")
                                        elif "ipwho.is" in api_url_template:
                                            country_code = geo_data.get("country_code")
                                        elif "freeipapi.com" in api_url_template:
                                            country_code = geo_data.get("countryCode")
                                        elif "ipinfo.io" in api_url_template:
                                            country_code = geo_data.get("country")
                                        
                                        if country_code:
                                            country_code = country_code.upper()[:2]  # Безопасность: только 2 символа
                                            logger.info(f"Detected proxy {proxy_str[:30]} country: {country_code}")
                                            return country_code
                except Exception as e:
                    logger.debug(f"Failed to detect country via {api_url_template}: {e}")
                    continue
            
            logger.warning(f"Could not detect country for proxy {proxy_str[:30]}")
            return None
            
    except Exception as e:
        logger.warning(f"Error detecting proxy country for {proxy_str[:30]}: {e}")
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
