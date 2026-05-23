"""
Сервис для работы с мобильными прокси с ротацией IP.
Обеспечивает безопасную ротацию IP и блокировку одновременного использования.
"""
import logging
import asyncio
import time
import uuid
import httpx
import redis
from typing import Optional, Tuple, Any, Iterable
from config.settings import get_settings
from db.models import Proxy
from services.log_sanitizer import sanitize_url
from sqlalchemy.orm import Session
from services.redis_service import get_redis_client

logger = logging.getLogger("services.mobile_proxy")
settings = get_settings()
_held_proxy_lock_tokens: dict[int, str] = {}

# ИСПРАВЛЕНО: Используем централизованный Redis клиент
def get_redis():
    """Получает централизованный Redis клиент."""
    return get_redis_client()


class ProxyRotationError(Exception):
    """Исключение при ошибке ротации IP прокси."""
    pass


class ProxyLockError(Exception):
    """Исключение при ошибке блокировки прокси."""
    pass


def _proxy_lock_key(proxy_id: int) -> str:
    return f"proxy_lock:{proxy_id}"


def _effective_proxy_lock_ttl_seconds() -> int:
    configured_ttl = int(getattr(settings, "MOBILE_PROXY_LOCK_TTL", 120) or 120)
    check_timeout = int(getattr(settings, "BLOGGER_CHECK_TIMEOUT_SECONDS", 180) or 180)
    extra_seconds = int(getattr(settings, "MOBILE_PROXY_LOCK_EXTRA_SECONDS", 90) or 90)
    return max(configured_ttl, check_timeout + extra_seconds)


def _decode_redis_value(value: Any) -> str | None:
    if value is None:
        return None
    if isinstance(value, bytes):
        return value.decode("utf-8", errors="ignore")
    return str(value)


def get_locked_proxy_ids(proxy_ids: Iterable[int]) -> set[int]:
    ids: list[int] = []
    for proxy_id in proxy_ids:
        try:
            ids.append(int(proxy_id))
        except (TypeError, ValueError):
            continue
    if not ids:
        return set()

    try:
        redis_client = get_redis()
        pipeline = redis_client.pipeline()
        for proxy_id in ids:
            pipeline.exists(_proxy_lock_key(proxy_id))
        results = pipeline.execute()
        return {proxy_id for proxy_id, exists in zip(ids, results) if bool(exists)}
    except Exception as exc:
        logger.debug("Failed to read mobile proxy locks; failing open: %s", exc)
        return set()


def clear_proxy_locks(proxy_ids: Iterable[int]) -> int:
    """Force-clear Redis locks for known proxy ids during single-worker startup recovery."""
    ids: list[int] = []
    for proxy_id in proxy_ids:
        try:
            ids.append(int(proxy_id))
        except (TypeError, ValueError):
            continue
    if not ids:
        return 0

    try:
        redis_client = get_redis()
        keys = [_proxy_lock_key(proxy_id) for proxy_id in ids]
        deleted = redis_client.delete(*keys)
        for proxy_id in ids:
            _held_proxy_lock_tokens.pop(proxy_id, None)
        return int(deleted or 0)
    except Exception as exc:
        logger.warning("Failed to clear mobile proxy locks during startup recovery: %s", exc)
        return 0


async def acquire_proxy_lock(proxy_id: int, timeout: int = 30) -> bool:
    """
    Захватывает блокировку прокси через Redis.
    
    Args:
        proxy_id: ID прокси в БД
        timeout: Максимальное время ожидания блокировки (секунды)
    
    Returns:
        True если блокировка получена, False если timeout
    
    Raises:
        ProxyLockError: при ошибке Redis
    """
    redis_client = get_redis()
    lock_key = _proxy_lock_key(proxy_id)
    lock_ttl = _effective_proxy_lock_ttl_seconds()
    lock_token = uuid.uuid4().hex
    
    start_time = time.time()
    
    max_retries = 3
    retry_delay = 0.5
    
    while True:
        for retry in range(max_retries):
            try:
                # Получаем клиент (с автоматическим переподключением)
                redis_client = get_redis()
                
                # Пробуем установить блокировку (SET NX EX - установить только если не существует, с TTL)
                # Используем синхронный Redis клиент в async контексте (Redis операции быстрые)
                result = redis_client.set(lock_key, lock_token, nx=True, ex=lock_ttl)
                
                if result:
                    _held_proxy_lock_tokens[int(proxy_id)] = lock_token
                    logger.debug(f"Acquired lock for proxy {proxy_id}")
                    return True
                
                # Блокировка занята - выходим из retry цикла и ждём
                break
                
            except Exception as redis_e:
                # Проверяем, это ли ошибка Redis (connection/timeout)
                error_str = str(redis_e).lower()
                if "connection" in error_str or "timeout" in error_str or "redis" in error_str:
                    # Временная ошибка Redis - retry
                    if retry < max_retries - 1:
                        logger.warning(f"Redis error acquiring lock (retry {retry + 1}/{max_retries}): {redis_e}")
                        await asyncio.sleep(retry_delay * (retry + 1))  # Exponential backoff
                        continue
                    else:
                        logger.error(f"Redis unavailable after {max_retries} retries: {redis_e}")
                        raise ProxyLockError(f"Redis unavailable: {redis_e}")
                else:
                    # Другая ошибка - пробрасываем дальше
                    logger.error(f"Error acquiring lock for proxy {proxy_id}: {redis_e}")
                    raise ProxyLockError(f"Failed to acquire lock: {redis_e}")
            except Exception as e:
                logger.error(f"Error acquiring lock for proxy {proxy_id}: {e}")
                raise ProxyLockError(f"Failed to acquire lock: {e}")
        
        # Блокировка занята - ждём перед следующей попыткой
        elapsed = time.time() - start_time
        if elapsed >= timeout:
            logger.warning(f"Timeout waiting for proxy {proxy_id} lock (waited {elapsed:.1f}s)")
            return False
        
        await asyncio.sleep(0.5)  # Ждём 500ms перед следующей попыткой


async def release_proxy_lock(proxy_id: int) -> None:
    """
    Освобождает блокировку прокси.
    Включает retry при временных ошибках Redis.
    
    Args:
        proxy_id: ID прокси в БД
    
    Raises:
        ProxyLockError: при ошибке Redis
    """
    lock_key = _proxy_lock_key(proxy_id)
    lock_token = _held_proxy_lock_tokens.pop(int(proxy_id), None)
    max_retries = 3
    
    for retry in range(max_retries):
        try:
            redis_client = get_redis()
            if lock_token:
                deleted = redis_client.eval(
                    "if redis.call('get', KEYS[1]) == ARGV[1] "
                    "then return redis.call('del', KEYS[1]) else return 0 end",
                    1,
                    lock_key,
                    lock_token,
                )
                if deleted:
                    logger.debug(f"Released lock for proxy {proxy_id}")
                else:
                    logger.debug(f"Proxy {proxy_id} lock is owned by another worker or already expired")
                return

            current_value = _decode_redis_value(redis_client.get(lock_key))
            if current_value == "locked":
                redis_client.delete(lock_key)
                logger.debug(f"Released legacy lock for proxy {proxy_id}")
            else:
                logger.debug(f"No owned lock token for proxy {proxy_id}; leaving lock untouched")
            return
        except (redis.ConnectionError, redis.TimeoutError) as e:
            # Временная ошибка Redis - retry
            if retry < max_retries - 1:
                logger.warning(f"Redis error releasing lock (retry {retry + 1}/{max_retries}): {e}")
                await asyncio.sleep(0.5 * (retry + 1))  # Exponential backoff
                continue
            else:
                logger.error(f"Redis unavailable after {max_retries} retries: {e}")
                # Не критично - блокировка освободится через TTL
                logger.warning(f"Lock {proxy_id} will expire via TTL ({_effective_proxy_lock_ttl_seconds()}s)")
                return  # Не пробрасываем ошибку - TTL защитит
        except Exception as e:
            logger.error(f"Error releasing lock for proxy {proxy_id}: {e}")
            # Не критично - блокировка освободится через TTL
            logger.warning(f"Lock {proxy_id} will expire via TTL ({_effective_proxy_lock_ttl_seconds()}s)")
            return  # Не пробрасываем ошибку - TTL защитит


def _adaptive_rotation_cooldown_seconds() -> int:
    """Возвращает фактический cooldown ротации IP с учётом авто-подстройки.

    Логика:
    1. Если адаптация включена и воркер записал в Redis ключ
       `monitor:mobile_proxy:rotation_cooldown_seconds` — используем его (вычисляется
       исходя из числа мобильных прокси при ребалансе).
    2. Иначе берём `MOBILE_PROXY_ROTATION_COOLDOWN_SECONDS` из конфигурации.
    3. 0 (или отрицательное) → cooldown отключён (старое поведение «ротируй каждый раз»).
    """
    default_value = max(0, int(getattr(settings, "MOBILE_PROXY_ROTATION_COOLDOWN_SECONDS", 0)))
    if not getattr(settings, "MOBILE_PROXY_ADAPTIVE_COOLDOWN_ENABLED", True):
        return default_value
    try:
        redis_client = get_redis()
        raw = redis_client.get("monitor:mobile_proxy:rotation_cooldown_seconds")
        if raw is None:
            return default_value
        value = int(raw)
        if value < 0:
            return 0
        return value
    except Exception as exc:
        logger.debug(f"Adaptive rotation cooldown lookup failed, falling back to default: {exc}")
        return default_value


async def _get_proxy_last_rotation_age_seconds(proxy_id: int) -> Optional[int]:
    """Возвращает возраст последней успешной ротации IP в секундах (или None)."""
    key = f"proxy_rotated_at:{proxy_id}"
    try:
        redis_client = get_redis()
        raw = redis_client.get(key)
        if raw is None:
            return None
        return max(0, int(time.time()) - int(raw))
    except Exception as exc:
        logger.debug(f"Failed to read rotation timestamp for proxy {proxy_id}: {exc}")
        return None


async def _mark_proxy_rotated_now(proxy_id: int, ttl: int = 3600) -> None:
    """Отмечает в Redis момент успешной ротации IP."""
    key = f"proxy_rotated_at:{proxy_id}"
    try:
        redis_client = get_redis()
        redis_client.set(key, int(time.time()), ex=ttl)
    except Exception as exc:
        logger.debug(f"Failed to write rotation timestamp for proxy {proxy_id}: {exc}")


async def get_current_ip(proxy_string: str) -> Optional[str]:
    """
    Получает текущий IP через прокси.
    
    Args:
        proxy_string: Строка прокси (для использования в httpx)
    
    Returns:
        IP адрес или None при ошибке
    """
    try:
        # Парсим прокси для httpx
        proxy_dict = _parse_proxy_for_httpx(proxy_string)
        
        # ИСПРАВЛЕНО: Используем правильный API httpx - прокси передаём через transport
        if proxy_dict:
            # httpx принимает прокси как строку URL
            proxy_url = proxy_dict.get("http://") or proxy_dict.get("https://")
            if proxy_url:
                # ИСПРАВЛЕНО: Увеличен таймаут для проверки IP через прокси
                # После ротации прокси может быть нестабильным, нужно больше времени
                from httpx import AsyncHTTPTransport
                transport = AsyncHTTPTransport(proxy=proxy_url)
                async with httpx.AsyncClient(
                    transport=transport,
                    timeout=httpx.Timeout(30.0, connect=10.0)
                ) as client:
                    response = await client.get(settings.MOBILE_PROXY_CHECK_IP_URL)
                    response.raise_for_status()
                    data = response.json()
                    return data.get("ip") or data.get("origin")  # Поддержка разных форматов ответа
            else:
                async with httpx.AsyncClient(
                    timeout=httpx.Timeout(30.0, connect=10.0)
                ) as client:
                    response = await client.get(settings.MOBILE_PROXY_CHECK_IP_URL)
                    response.raise_for_status()
                    data = response.json()
                    return data.get("ip") or data.get("origin")
        else:
            async with httpx.AsyncClient(
                timeout=httpx.Timeout(30.0, connect=10.0)
            ) as client:
                response = await client.get(settings.MOBILE_PROXY_CHECK_IP_URL)
                response.raise_for_status()
                data = response.json()
                return data.get("ip") or data.get("origin")  # Поддержка разных форматов ответа
            
    except Exception as e:
        logger.warning(f"Failed to get current IP via proxy {proxy_string[:30]}: {e}")
        return None


def _parse_proxy_for_httpx(proxy_string: str) -> Optional[dict]:
    """
    Парсит строку прокси для httpx.
    
    Args:
        proxy_string: Строка прокси (например: "socks5://user:pass@ip:port")
    
    Returns:
        Dict с настройками прокси для httpx или None
    """
    if not proxy_string or not proxy_string.strip():
        return None
    
    proxy_string = proxy_string.strip()
    
    # Если уже полный URL - используем как есть
    if "://" in proxy_string:
        return {"http://": proxy_string, "https://": proxy_string}
    
    # Если нет протокола - добавляем socks5 (по умолчанию для мобильных прокси)
    if "@" in proxy_string:
        return {"http://": f"socks5://{proxy_string}", "https://": f"socks5://{proxy_string}"}
    
    # Просто ip:port - предполагаем http
    if ":" in proxy_string:
        return {"http://": f"http://{proxy_string}", "https://": f"http://{proxy_string}"}
    
    return None


async def rotate_proxy_ip(proxy: Proxy) -> Tuple[bool, Optional[str]]:
    """
    Выполняет ротацию IP для мобильного прокси.
    
    Args:
        proxy: Объект Proxy из БД
    
    Returns:
        Tuple[bool, Optional[str]]: (успех, новый IP или None)
    
    Raises:
        ProxyRotationError: при критической ошибке ротации
    """
    if not proxy.rotation_url:
        raise ProxyRotationError(f"Proxy {proxy.id} has no rotation_url")
    
    safe_rotation_url = sanitize_url(proxy.rotation_url)
    logger.info(f"Rotating IP for proxy {proxy.id} (rotation_url: {safe_rotation_url})")
    
    try:
        # ИСПРАВЛЕНО: Увеличен таймаут до 60 секунд для мобильных прокси
        # Мобильные прокси могут перезагружаться 10-30 секунд, нужно дать им время
        # 1. Отправляем запрос на ротацию IP
        async with httpx.AsyncClient(timeout=httpx.Timeout(60.0, connect=10.0)) as client:
            response = await client.get(proxy.rotation_url)
            response.raise_for_status()
        
        logger.debug(f"Rotation request sent for proxy {proxy.id}, waiting {settings.MOBILE_PROXY_ROTATION_DELAY}s")
        
        # 2. Ждём задержку (провайдеру нужно время на смену IP)
        await asyncio.sleep(settings.MOBILE_PROXY_ROTATION_DELAY)
        
        # 3. Получаем новый IP
        new_ip = await get_current_ip(proxy.proxy)
        
        if new_ip:
            logger.info(f"Proxy {proxy.id} new IP: {new_ip}")
            return True, new_ip
        else:
            logger.warning(f"Failed to get new IP for proxy {proxy.id} after rotation")
            return False, None
            
    except httpx.HTTPStatusError as e:
        logger.error(f"HTTP error rotating proxy {proxy.id}: {e.response.status_code}")
        raise ProxyRotationError(f"HTTP {e.response.status_code} when rotating proxy")
    except httpx.RequestError as e:
        logger.error(
            f"Request error rotating proxy {proxy.id} "
            f"({safe_rotation_url}): {e!r}"
        )
        raise ProxyRotationError(f"Request error: {e!r}")
    except Exception as e:
        logger.error(f"Unexpected error rotating proxy {proxy.id}: {e}")
        raise ProxyRotationError(f"Unexpected error: {e}")


async def ensure_proxy_ip_rotated(
    proxy: Proxy,
    db: Session,
    previous_ip: Optional[str] = None
) -> str:
    """
    Обеспечивает ротацию IP прокси с проверкой (Double Check).
    
    Алгоритм:
    1. Получить текущий IP
    2. Если IP не изменился (или это первая проверка) - выполнить ротацию
    3. Проверить что IP изменился (до MAX_RETRIES попыток)
    4. Вернуть новый IP
    
    Args:
        proxy: Объект Proxy из БД
        db: SQLAlchemy сессия (для обновления данных если нужно)
        previous_ip: Предыдущий IP (из Redis или None)
    
    Returns:
        str: Новый IP адрес
    
    Raises:
        ProxyRotationError: если ротация не удалась после MAX_RETRIES попыток
    """
    if not proxy.is_mobile or proxy.is_mobile != "Y":
        # Не мобильный прокси - возвращаем текущий IP (если есть)
        if previous_ip:
            return previous_ip
        # Пробуем получить IP
        current_ip = await get_current_ip(proxy.proxy)
        if current_ip:
            return current_ip
        raise ProxyRotationError(f"Proxy {proxy.id} is not mobile and IP check failed")
    
    # ОПТИМИЗАЦИЯ: Если previous_ip есть и мы знаем что IP уже изменился - пропускаем ротацию
    # Но сначала проверяем текущий IP для уверенности
    current_ip_before = await get_current_ip(proxy.proxy)

    # Если IP не изменился с предыдущего использования - нужно ротировать
    needs_rotation = (previous_ip is None) or (current_ip_before == previous_ip)

    if not needs_rotation and current_ip_before:
        logger.debug(f"Proxy {proxy.id} IP already changed ({current_ip_before}), no rotation needed")
        return current_ip_before

    # Cooldown переиспользования: если совсем недавно (< cooldown) уже крутили IP — повторять
    # ротацию нет смысла, она лишь даст 25–30 секунд простоя для других воркеров. Особенно
    # критично, когда мобильный прокси один и concurrency > 1.
    cooldown_seconds = _adaptive_rotation_cooldown_seconds()
    if cooldown_seconds > 0:
        rotation_age = await _get_proxy_last_rotation_age_seconds(proxy.id)
        if rotation_age is not None and rotation_age < cooldown_seconds:
            reusable_ip = current_ip_before or previous_ip
            if not reusable_ip:
                logger.info(
                    f"Proxy {proxy.id} rotated {rotation_age}s ago "
                    f"(<{cooldown_seconds}s cooldown), but no cached/current IP is available; rotating"
                )
            else:
                logger.info(
                    f"Proxy {proxy.id} rotated {rotation_age}s ago "
                    f"(<{cooldown_seconds}s cooldown), reusing IP {reusable_ip}"
                )
                return reusable_ip
    
    # Выполняем ротацию с retry
    for attempt in range(settings.MOBILE_PROXY_MAX_RETRIES):
        try:
            success, new_ip = await rotate_proxy_ip(proxy)
            
            if not success or not new_ip:
                logger.warning(f"Proxy {proxy.id} rotation attempt {attempt + 1} failed to get new IP")
                if attempt < settings.MOBILE_PROXY_MAX_RETRIES - 1:
                    await asyncio.sleep(2)  # Короткая задержка перед retry
                continue
            
            # Проверяем что IP действительно изменился
            if current_ip_before and new_ip == current_ip_before:
                logger.warning(
                    f"Proxy {proxy.id} IP did not change after rotation "
                    f"(attempt {attempt + 1}/{settings.MOBILE_PROXY_MAX_RETRIES}): {new_ip}"
                )
                if attempt < settings.MOBILE_PROXY_MAX_RETRIES - 1:
                    await asyncio.sleep(2)  # Короткая задержка перед retry
                continue
            
            # IP изменился - успех!
            logger.info(f"Proxy {proxy.id} IP successfully rotated: {current_ip_before} -> {new_ip}")
            # Запоминаем момент успешной ротации, чтобы cooldown в следующих вызовах работал
            await _mark_proxy_rotated_now(proxy.id)
            return new_ip
            
        except ProxyRotationError:
            # Критическая ошибка - пробрасываем дальше
            raise
        except Exception as e:
            logger.error(f"Unexpected error in rotation attempt {attempt + 1} for proxy {proxy.id}: {e}")
            if attempt < settings.MOBILE_PROXY_MAX_RETRIES - 1:
                await asyncio.sleep(2)
            continue
    
    # Все попытки исчерпаны
    raise ProxyRotationError(
        f"Failed to rotate IP for proxy {proxy.id} after {settings.MOBILE_PROXY_MAX_RETRIES} attempts. "
        f"IP did not change or rotation failed."
    )


async def get_proxy_last_ip(proxy_id: int) -> Optional[str]:
    """
    Получает последний использованный IP прокси из Redis.
    Включает retry при временных ошибках Redis.
    
    Args:
        proxy_id: ID прокси в БД
    
    Returns:
        IP адрес или None (при ошибке)
    """
    ip_key = f"proxy_last_ip:{proxy_id}"
    max_retries = 2
    
    for retry in range(max_retries):
        try:
            redis_client = get_redis()
            return redis_client.get(ip_key)
        except (redis.ConnectionError, redis.TimeoutError) as e:
            if retry < max_retries - 1:
                logger.debug(f"Redis error getting last IP (retry {retry + 1}/{max_retries}): {e}")
                await asyncio.sleep(0.3)
                continue
            else:
                logger.warning(f"Failed to get last IP for proxy {proxy_id} after {max_retries} retries: {e}")
                return None
        except Exception as e:
            logger.warning(f"Failed to get last IP for proxy {proxy_id}: {e}")
            return None


async def save_proxy_last_ip(proxy_id: int, ip: str, ttl: int = 3600) -> None:
    """
    Сохраняет последний использованный IP прокси в Redis.
    Включает retry при временных ошибках Redis.
    
    Args:
        proxy_id: ID прокси в БД
        ip: IP адрес
        ttl: TTL в секундах (по умолчанию 1 час)
    """
    ip_key = f"proxy_last_ip:{proxy_id}"
    max_retries = 2
    
    for retry in range(max_retries):
        try:
            redis_client = get_redis()
            redis_client.set(ip_key, ip, ex=ttl)
            logger.debug(f"Saved last IP {ip} for proxy {proxy_id}")
            return
        except Exception as redis_e:
            # Проверяем, это ли ошибка Redis
            error_str = str(redis_e).lower()
            if "connection" in error_str or "timeout" in error_str or "redis" in error_str:
                if retry < max_retries - 1:
                    logger.debug(f"Redis error saving last IP (retry {retry + 1}/{max_retries}): {redis_e}")
                    await asyncio.sleep(0.3)
                    continue
                else:
                    logger.warning(f"Failed to save last IP for proxy {proxy_id} after {max_retries} retries: {redis_e}")
                    # Не критично - можно продолжить без сохранения IP
                    return
            else:
                logger.warning(f"Failed to save last IP for proxy {proxy_id}: {redis_e}")
                # Не критично - можно продолжить без сохранения IP
                return
        except Exception as e:
            logger.warning(f"Failed to save last IP for proxy {proxy_id}: {e}")
            # Не критично - можно продолжить без сохранения IP
            return


async def prepare_mobile_proxy_for_account(
    account: Any,  # Account model (избегаем циклического импорта)
    db: Session,
    proxy: Proxy | None = None
) -> Tuple[bool, Optional[Proxy]]:
    """
    Подготавливает мобильный прокси для использования аккаунтом.
    Захватывает блокировку прокси, и только после этого ротирует IP,
    чтобы избежать отключения других активных воркеров.
    
    Args:
        account: Объект Account
        db: SQLAlchemy сессия
        proxy: Объект Proxy из БД (опционально, если None - загрузит из БД)
    
    Returns:
        Tuple[bool, Optional[Proxy]]: (lock_acquired, proxy_obj)
        - lock_acquired: True если блокировка получена успешно и нужно её освободить
        - proxy_obj: Объект Proxy (для освобождения блокировки)
    
    Raises:
        ProxyRotationError: при ошибке ротации IP
        ProxyLockError: при ошибке блокировки
    """
    if not account.proxy:
        return False, None

    if not proxy:
        proxy = db.query(Proxy).filter(Proxy.proxy == account.proxy).first()
        if not proxy:
            return False, None

    if proxy.is_mobile != "Y" or not proxy.rotation_url:
        return False, proxy
    
    logger.info(f"Preparing mobile proxy {proxy.id} for account {account.id}")
    
    # 1. СНАЧАЛА захватываем блокировку, чтобы другой воркер не использовал этот прокси
    lock_acquired = await acquire_proxy_lock(proxy.id, timeout=30)
    if not lock_acquired:
        raise ProxyLockError(f"Failed to acquire lock for proxy {proxy.id} (timeout)")
    
    try:
        # 2. Получаем предыдущий IP из Redis
        previous_ip = await get_proxy_last_ip(proxy.id)
        
        # 3. Ротируем IP (с проверкой)
        new_ip = await ensure_proxy_ip_rotated(proxy, db, previous_ip)
        
        # 4. Сохраняем новый IP в Redis
        if new_ip:
            await save_proxy_last_ip(proxy.id, new_ip)
            
        logger.info(f"Mobile proxy {proxy.id} prepared for account {account.id}: IP {new_ip}")
        return True, proxy
        
    except Exception as e:
        # При ошибке освобождаем блокировку и пробрасываем исключение
        try:
            await release_proxy_lock(proxy.id)
        except Exception:
            pass
        raise
