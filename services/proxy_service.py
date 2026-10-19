"""Сервис для проверки работоспособности прокси через httpx (лучшая поддержка SOCKS5)."""
import asyncio
import logging
import time
from typing import Tuple, Optional
from urllib.parse import urlparse
import httpx
from services.log_sanitizer import sanitize_url

logger = logging.getLogger("services.proxy_service")


def normalize_proxy_url(proxy_str: str) -> str:
    """
    Нормализует строку прокси в полный URL.
    
    НЕ ИЗМЕНЯЕТ строку прокси, только добавляет протокол если его нет.
    Поддерживает:
    - socks5://login:pass@ip:port
    - http://login:pass@ip:port
    - login:pass@ip:port (добавит http://; для SOCKS укажите socks5:// явно)
    - ip:port (добавит http://)
    
    Args:
        proxy_str: строка прокси (может быть с протоколом или без)
        
    Returns:
        str: полный URL прокси
    """
    if not proxy_str or not proxy_str.strip():
        raise ValueError("Proxy string cannot be empty")
    
    proxy_str = proxy_str.strip()
    
    # Если уже полный URL - возвращаем как есть
    if "://" in proxy_str:
        return proxy_str
    
    # Auth without a scheme is most often an HTTP proxy. If the provider gives
    # SOCKS, the import line must include socks5:// explicitly.
    if "@" in proxy_str:
        return f"http://{proxy_str}"
    
    # Просто ip:port - добавляем http://
    return f"http://{proxy_str}"


async def check_proxy_health_async(
    proxy_str: str, 
    test_url: str = "https://api.ipify.org?format=json",
    timeout: float = 10.0,
    measure_speed: bool = True
) -> Tuple[bool, str, Optional[str], Optional[int]]:
    """
    ИСПРАВЛЕНО: Асинхронно проверяет работоспособность прокси через httpx.
    
    httpx имеет лучшую поддержку SOCKS5 прокси с авторизацией, чем aiohttp-socks.
    НЕ ИЗМЕНЯЕТ строку прокси, использует её как есть.
    
    Args:
        proxy_str: строка прокси (может быть с протоколом или без)
        test_url: URL для проверки (по умолчанию api.ipify.org)
        timeout: таймаут в секундах
        
    Returns:
        Tuple[bool, str, Optional[str], Optional[int]]:
        - is_active: True если прокси работает
        - status: "active" | "bad" | "slow" | "unknown"
        - error_message: сообщение об ошибке или None
        - response_time_ms: время ответа в миллисекундах или None
        
    Статусы:
        - "active": успешно подключился и получил IP
        - "bad": timeout, auth failed, connection refused
        - "unknown": DNS error, parsing error, client error (это ошибка клиента, не прокси)
    """
    if not proxy_str or not proxy_str.strip():
        return False, "unknown", "Proxy string is empty", None
    
    proxy_str = proxy_str.strip()
    
    safe_proxy = sanitize_url(str(proxy_str or ""), max_length=80)
    try:
        # Нормализуем URL (добавляем протокол если нет, НО НЕ ИЗМЕНЯЕМ саму строку)
        proxy_url = normalize_proxy_url(proxy_str)
        
        # Парсим URL для определения типа прокси
        parsed = urlparse(proxy_url)
        proxy_type = parsed.scheme.lower()
        
        # Проверяем что протокол поддерживается
        if proxy_type not in ("http", "https", "socks5", "socks4"):
            return False, "unknown", f"Unsupported proxy protocol: {proxy_type}", None
        
        # ИСПРАВЛЕНО: Используем httpx для проверки прокси (лучшая поддержка SOCKS5)
        start_time = time.time() if measure_speed else None
        
        try:
            # ИСПРАВЛЕНО: В httpx прокси передаются в каждый запрос, а не в конструктор
            # Создаём клиент без прокси
            async with httpx.AsyncClient(
                timeout=timeout,
                follow_redirects=True
            ) as client:
                # Прокси передаём в запрос
                response = await client.get(test_url, proxies=proxy_url)
                
                response_time_ms = None
                if measure_speed and start_time:
                    elapsed = time.time() - start_time
                    response_time_ms = int(elapsed * 1000)
                
                if response.status_code == 200:
                    # Пробуем получить IP из ответа
                    try:
                        data = response.json()
                        ip = data.get("ip", "unknown")
                        
                        # ИСПРАВЛЕНО: Определяем статус на основе скорости
                        # Если прокси медленный (>60 секунд), помечаем как "slow", но не как "bad"
                        # Медленные прокси всё равно работают, просто медленнее
                        status = "active"
                        if response_time_ms and response_time_ms > 60000:  # Увеличено с 30 до 60 секунд
                            status = "slow"
                            logger.info(f"Proxy {safe_proxy} is slow but working: {response_time_ms}ms")
                        
                        logger.debug(f"Proxy {safe_proxy} is {status}, IP: {ip}, time: {response_time_ms}ms")
                        return True, status, None, response_time_ms
                    except Exception:
                        # Если не JSON, просто проверяем статус
                        if response.status_code == 200:
                            status = "active"
                            if response_time_ms and response_time_ms > 60000:  # Увеличено с 30 до 60 секунд
                                status = "slow"
                            logger.debug(f"Proxy {safe_proxy} is {status} (status 200), time: {response_time_ms}ms")
                            return True, status, None, response_time_ms
                        else:
                            return False, "bad", f"HTTP {response.status_code}", response_time_ms
                else:
                    return False, "bad", f"HTTP {response.status_code}", response_time_ms
                    
        except httpx.TimeoutException:
            # Timeout - прокси не работает (медленный или не отвечает)
            logger.warning(f"Proxy {safe_proxy} timeout")
            # Таймаут обычно >30 секунд, помечаем как медленный
            return False, "slow", "Timeout connecting to proxy (>30s)", None
            
        except httpx.ProxyError as e:
            # Ошибка прокси - может быть auth failed, connection refused
            error_msg = str(e)
            
            if "authentication" in error_msg.lower() or "auth" in error_msg.lower():
                # Ошибка авторизации - прокси bad
                logger.warning(f"Proxy {safe_proxy} auth failed: {error_msg[:100]}")
                return False, "bad", f"Authentication failed: {error_msg[:100]}", None
            elif "connection refused" in error_msg.lower() or "refused" in error_msg.lower():
                # Connection refused - прокси bad
                logger.warning(f"Proxy {safe_proxy} connection refused: {error_msg[:100]}")
                return False, "bad", f"Connection refused: {error_msg[:100]}", None
            else:
                # Другие ошибки прокси - обычно это bad proxy
                logger.warning(f"Proxy {safe_proxy} proxy error: {error_msg[:100]}")
                return False, "bad", f"Proxy error: {error_msg[:100]}", None
                
        except httpx.ConnectError as e:
            # Ошибка подключения - может быть DNS, connection refused
            error_msg = str(e)
            
            if "getaddrinfo" in error_msg.lower() or "11001" in error_msg or "name resolution" in error_msg.lower():
                # DNS ошибка - это ошибка клиента (неправильный домен), не прокси
                logger.debug(f"Proxy {safe_proxy} DNS error (client error): {error_msg[:100]}")
                return False, "unknown", f"DNS resolution failed (client error): {error_msg[:100]}", None
            else:
                # Другие ошибки подключения - обычно это bad proxy
                logger.warning(f"Proxy {safe_proxy} connection error: {error_msg[:100]}")
                return False, "bad", f"Connection error: {error_msg[:100]}", None
                
        except httpx.HTTPError as e:
            # Другие ошибки HTTP
            error_msg = str(e)[:200]
            logger.warning(f"Proxy {safe_proxy} HTTP error: {error_msg}")
            return False, "bad", f"HTTP error: {error_msg}", None
                
    except ValueError as e:
        # Ошибка валидации/парсинга - это ошибка клиента
        logger.debug(f"Proxy {safe_proxy} validation error: {e}")
        return False, "unknown", f"Invalid proxy format: {str(e)[:100]}", None
        
    except Exception as e:
        # Неожиданная ошибка - логируем и помечаем как unknown (ошибка клиента)
        error_msg = str(e)[:200]
        logger.error(f"Proxy {safe_proxy} unexpected error: {error_msg}")
        return False, "unknown", f"Unexpected error: {error_msg}", None


async def quick_check_proxy(proxy_str: str, timeout: float = 30.0, max_response_time_ms: int = 60000) -> tuple[bool, int | None]:
    """
    ИСПРАВЛЕНО: Быстрая проверка прокси с увеличенными таймаутами для медленных прокси.
    Используется перед выполнением задачи.
    
    Args:
        proxy_str: строка прокси
        timeout: таймаут в секундах (по умолчанию 30 для медленных прокси)
        max_response_time_ms: максимальное время ответа в миллисекундах (по умолчанию 60 секунд)
                              Прокси медленнее этого порога считается слишком медленным
                              ИСПРАВЛЕНО: Увеличено с 15 до 60 секунд для медленных прокси
        
    Returns:
        tuple[bool, int | None]: (is_ok, response_time_ms)
        - is_ok: True если прокси работает (даже если медленный)
        - response_time_ms: время ответа в миллисекундах или None
    """
    try:
        is_active, status, _, response_time_ms = await check_proxy_health_async(
            proxy_str, 
            "https://api.ipify.org?format=json",  # Быстрый endpoint
            timeout=timeout,
            measure_speed=True  # ИСПРАВЛЕНО: Измеряем скорость для проверки
        )
        
        if not is_active:
            return False, response_time_ms
        
        # ИСПРАВЛЕНО: Не помечаем прокси как негодный только из-за медленности
        # Если прокси работает (is_active=True), считаем его годным, даже если медленный
        # Медленные прокси будут помечены как "slow" в статусе, но не как "bad"
        if status == "bad":
            # Только если статус "bad" (не работает), возвращаем False
            return False, response_time_ms
        
        # Прокси работает (может быть медленным, но работает)
        return True, response_time_ms
    except Exception as e:
        logger.warning(
            "Quick proxy check failed for %s: %s",
            sanitize_url(str(proxy_str or ""), max_length=80),
            e,
        )
        return False, None


def check_proxy_health(proxy_str: str, test_url: str, timeout: float = 10.0) -> Tuple[bool, str | None]:
    """
    Синхронная обёртка для check_proxy_health_async.
    
    Используется в синхронном контексте (например, в FastAPI endpoint).
    Создаёт новый event loop если его нет.
    
    Args:
        proxy_str: строка прокси
        test_url: URL для проверки
        timeout: таймаут в секундах
        
    Returns:
        Tuple[bool, str | None]:
        - is_active: True если прокси работает
        - error_message: сообщение об ошибке или None
    """
    try:
        # Пробуем получить существующий event loop
        loop = asyncio.get_event_loop()
        if loop.is_running():
            # Если loop уже запущен, создаём новый в отдельном потоке
            import concurrent.futures
            with concurrent.futures.ThreadPoolExecutor() as executor:
                future = executor.submit(
                    lambda: asyncio.run(check_proxy_health_async(proxy_str, test_url, timeout))
                )
                is_active, status, error_msg, _ = future.result()
        else:
            # Loop существует но не запущен
            is_active, status, error_msg, _ = loop.run_until_complete(
                check_proxy_health_async(proxy_str, test_url, timeout)
            )
    except RuntimeError:
        # Нет event loop, создаём новый
        is_active, status, error_msg, _ = asyncio.run(
            check_proxy_health_async(proxy_str, test_url, timeout)
        )
    
    # Возвращаем только is_active и error_message для обратной совместимости
    return is_active, error_msg
