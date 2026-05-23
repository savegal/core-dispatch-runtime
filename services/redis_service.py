"""
Централизованный сервис для работы с Redis.
Обеспечивает единый connection pool для всех сервисов.
"""
import redis
import logging
import json
import os
import socket
from typing import Any, Optional
from functools import lru_cache
from config.settings import get_settings
import uuid

logger = logging.getLogger("services.redis_service")
settings = get_settings()

# Глобальный Redis клиент с connection pooling
_redis_client: Optional[redis.Redis] = None


# ИСПРАВЛЕНО: Убран @lru_cache() - он конфликтует с глобальной переменной _redis_client
# Добавлено ограничение логирования ошибок (раз в минуту)
_last_redis_error_log = 0
_redis_error_log_interval = 60  # Логируем ошибки Redis не чаще раза в минуту

def get_redis_client() -> redis.Redis:
    """
    Получает или создаёт централизованный Redis клиент с connection pooling.
    
    Переиспользуется всеми сервисами для оптимизации соединений.
    Включает автоматическое переподключение при разрыве соединения.
    
    ИСПРАВЛЕНО: Ограничено логирование ошибок (раз в минуту) для уменьшения спама в логах.
    
    Returns:
        redis.Redis: Redis клиент с настроенным connection pool
    
    Raises:
        redis.ConnectionError: Если Redis недоступен и не удалось подключиться
    """
    global _redis_client, _last_redis_error_log
    
    if _redis_client is None:
        try:
            _redis_client = redis.from_url(
                settings.REDIS_URL,
                decode_responses=True,
                socket_connect_timeout=5,  # ИСПРАВЛЕНО: Уменьшено до 5 секунд для быстрого обнаружения недоступности
                socket_timeout=30,  # ИСПРАВЛЕНО: Увеличено до 30 секунд для brpop операций
                retry_on_timeout=True,
                health_check_interval=30,  # Проверка здоровья соединения каждые 30 секунд
                max_connections=100  # ИСПРАВЛЕНО: Увеличено для поддержки большего количества воркеров
            )
            # Проверяем подключение
            _redis_client.ping()
            logger.info("Redis client initialized with connection pooling")
            _last_redis_error_log = 0  # Сбрасываем счетчик ошибок при успешном подключении
        except Exception as e:
            import time
            current_time = time.time()
            # Логируем ошибку только раз в минуту
            if current_time - _last_redis_error_log > _redis_error_log_interval:
                logger.error(f"Failed to initialize Redis client: {e}")
                logger.error("Redis is required for task queue. Please start Redis server.")
                _last_redis_error_log = current_time
            raise
    
    # Проверяем соединение и переподключаемся при необходимости
    try:
        _redis_client.ping()
    except (redis.ConnectionError, redis.TimeoutError, AttributeError) as e:
        # Соединение разорвано - переподключаемся
        import time
        current_time = time.time()
        # Логируем предупреждение только раз в минуту
        if current_time - _last_redis_error_log > _redis_error_log_interval:
            logger.warning("Redis connection lost, reconnecting...")
            _last_redis_error_log = current_time
        
        try:
            _redis_client = redis.from_url(
                settings.REDIS_URL,
                decode_responses=True,
                socket_connect_timeout=5,  # ИСПРАВЛЕНО: Уменьшено до 5 секунд
                socket_timeout=30,  # ИСПРАВЛЕНО: Увеличено до 30 секунд для brpop операций
                retry_on_timeout=True,
                health_check_interval=30,
                max_connections=100  # ИСПРАВЛЕНО: Увеличено для поддержки большего количества воркеров
            )
            _redis_client.ping()
            logger.info("Redis client reconnected successfully")
            _last_redis_error_log = 0  # Сбрасываем счетчик ошибок при успешном переподключении
        except Exception as reconnect_error:
            current_time = time.time()
            # Логируем ошибку только раз в минуту
            if current_time - _last_redis_error_log > _redis_error_log_interval:
                logger.error(f"Failed to reconnect Redis client: {reconnect_error}")
                _last_redis_error_log = current_time
            raise
    
    return _redis_client


def close_redis_client():
    """
    Закрывает Redis клиент (используется при graceful shutdown).
    """
    global _redis_client
    if _redis_client:
        try:
            _redis_client.close()
            logger.info("Redis client closed")
        except Exception as e:
            logger.warning(f"Error closing Redis client: {e}")
        finally:
            _redis_client = None


# --- Executor heartbeat ---
_EXECUTOR_HEARTBEAT_KEY = "executor:heartbeat"
_EXECUTOR_TASK_LOCK_KEY = "lock:executor:single_active_task"
_EXECUTOR_HEARTBEAT_TTL = 90  # секунд: heartbeat раз в ~10s, TTL с запасом
_MONITOR_HEARTBEAT_KEY = "videomonitor:heartbeat"
_MONITOR_HEARTBEAT_TTL = 120


def update_executor_heartbeat(status: str = "ok", **extra: Any) -> bool:
    """
    Обновляет heartbeat executor в Redis.

    Args:
        status: строка статуса (по умолчанию 'ok')
    Returns:
        bool: True при успешной записи, False при ошибке
    """
    try:
        redis_client = get_redis_client()
        # Пишем timestamp и статус в одном значении
        payload = {
            "status": status,
            "ts": int(__import__("time").time()),
            "pid": os.getpid(),
            "host": socket.gethostname(),
        }
        for key, val in extra.items():
            if val is None:
                continue
            if isinstance(val, (str, int, float, bool)):
                payload[key] = val
        redis_client.set(_EXECUTOR_HEARTBEAT_KEY, json.dumps(payload), ex=_EXECUTOR_HEARTBEAT_TTL)
        return True
    except Exception as e:
        logger.debug(f"Failed to update executor heartbeat: {e}")
        return False


def get_executor_heartbeat() -> tuple[bool, dict]:
    """
    Читает heartbeat executor.

    Returns:
        (alive: bool, info: dict)
        alive=True если ключ существует и не просрочен.
    """
    try:
        redis_client = get_redis_client()
        data = redis_client.get(_EXECUTOR_HEARTBEAT_KEY)
        if not data:
            return False, {"reason": "missing"}
        # redis from_url with decode_responses=True -> data уже dict? set записал dict -> будет строка repr
        if isinstance(data, str):
            try:
                parsed_json = json.loads(data)
                return True, parsed_json if isinstance(parsed_json, dict) else {"raw": data}
            except Exception:
                pass
            try:
                import ast
                parsed = ast.literal_eval(data)
                return True, parsed if isinstance(parsed, dict) else {"raw": data}
            except Exception:
                return True, {"raw": data}
        if isinstance(data, dict):
            return True, data
        return True, {"raw": data}
    except Exception as e:
        logger.debug(f"Failed to read executor heartbeat: {e}")
        return False, {"reason": str(e)}


def executor_task_activity_snapshot() -> tuple[bool, dict]:
    """Return whether an executor task is actively running according to Redis."""
    try:
        redis_client = get_redis_client()
        lock_value = redis_client.get(_EXECUTOR_TASK_LOCK_KEY)
        try:
            lock_ttl = int(redis_client.ttl(_EXECUTOR_TASK_LOCK_KEY))
        except Exception:
            lock_ttl = None
        heartbeat_alive, heartbeat_info = get_executor_heartbeat()
        heartbeat_status = ""
        if isinstance(heartbeat_info, dict):
            heartbeat_status = str(heartbeat_info.get("status") or "").lower()
        lock_present = bool(lock_value)
        active = bool(lock_present and heartbeat_alive and heartbeat_status == "busy")
        return active, {
            "lock_present": lock_present,
            "lock_ttl": lock_ttl,
            "heartbeat_alive": heartbeat_alive,
            "heartbeat_status": heartbeat_status or None,
            "heartbeat": heartbeat_info,
        }
    except Exception as e:
        logger.debug(f"Failed to read executor task activity: {e}")
        return False, {"reason": str(e)}


def update_monitor_heartbeat(status: str = "ok", **extra: Any) -> bool:
    """Heartbeat для воркера видео-мониторинга. Доп. поля (int/str/bool/float) попадают в payload для диагностики."""
    try:
        redis_client = get_redis_client()
        payload: dict[str, Any] = {"status": status, "ts": int(__import__("time").time())}
        for key, val in extra.items():
            if val is None:
                continue
            if isinstance(val, (str, int, float, bool)):
                payload[key] = val
        redis_client.set(_MONITOR_HEARTBEAT_KEY, json.dumps(payload), ex=_MONITOR_HEARTBEAT_TTL)
        return True
    except Exception as e:
        logger.debug(f"Failed to update monitor heartbeat: {e}")
        return False


def get_monitor_heartbeat() -> tuple[bool, dict]:
    """Читает heartbeat видео-монитора."""
    try:
        redis_client = get_redis_client()
        data = redis_client.get(_MONITOR_HEARTBEAT_KEY)
        if not data:
            return False, {"reason": "missing"}
        if isinstance(data, str):
            try:
                parsed_json = json.loads(data)
                return True, parsed_json if isinstance(parsed_json, dict) else {"raw": data}
            except Exception:
                pass
            try:
                import ast
                parsed = ast.literal_eval(data)
                return True, parsed if isinstance(parsed, dict) else {"raw": data}
            except Exception:
                return True, {"raw": data}
        if isinstance(data, dict):
            return True, data
        return True, {"raw": data}
    except Exception as e:
        logger.debug(f"Failed to read monitor heartbeat: {e}")
        return False, {"reason": str(e)}


class RedisLock:
    """
    ИСПРАВЛЕНО: Redis lock для атомарных операций между процессами/серверами.
    
    Используется для дополнительной защиты от race conditions при выборе аккаунтов,
    когда SELECT FOR UPDATE недостаточно (например, между разными серверами).
    
    Пример использования:
        lock = RedisLock(f"account_select:{account_id}", timeout=5)
        if lock.acquire():
            try:
                # Критическая операция
                account = get_account()
            finally:
                lock.release()
    """
    
    def __init__(self, key: str, timeout: int = 5, blocking: bool = False):
        """
        Args:
            key: Ключ блокировки в Redis
            timeout: Время жизни блокировки в секундах (TTL)
            blocking: Если True - блокирующее ожидание, если False - немедленный возврат
        """
        self.key = f"lock:{key}"
        self.timeout = timeout
        self.blocking = blocking
        self._token = uuid.uuid4().hex
        self._acquired = False
    
    def acquire(self) -> bool:
        """
        Пытается захватить блокировку.
        
        Returns:
            bool: True если блокировка захвачена, False если уже занята
        """
        redis_client = get_redis_client()
        
        try:
            # Используем SET NX EX для атомарной установки блокировки с TTL
            # NX = только если ключ не существует
            # EX = установить TTL в секундах
            result = redis_client.set(self.key, self._token, nx=True, ex=self.timeout)
            
            if result:
                self._acquired = True
                logger.debug(f"Redis lock acquired: {self.key}")
                return True
            else:
                if self.blocking:
                    # Блокирующее ожидание (опционально, не используется сейчас)
                    import time
                    wait_time = 0.1
                    max_wait = self.timeout
                    waited = 0
                    while waited < max_wait:
                        time.sleep(wait_time)
                        result = redis_client.set(self.key, self._token, nx=True, ex=self.timeout)
                        if result:
                            self._acquired = True
                            logger.debug(f"Redis lock acquired after {waited:.1f}s: {self.key}")
                            return True
                        waited += wait_time
                
                logger.debug(f"Redis lock not acquired (already locked): {self.key}")
                return False
                
        except Exception as e:
            logger.warning(f"Error acquiring Redis lock {self.key}: {e}")
            # При ошибке Redis считаем, что блокировка не захвачена (безопасный режим)
            return False
    
    def release(self):
        """Освобождает блокировку."""
        if not self._acquired:
            return
        
        redis_client = get_redis_client()
        try:
            # Освобождаем lock только если он принадлежит текущему владельцу.
            release_script = """
if redis.call("GET", KEYS[1]) == ARGV[1] then
  return redis.call("DEL", KEYS[1])
end
return 0
"""
            redis_client.eval(release_script, 1, self.key, self._token)
            self._acquired = False
            logger.debug(f"Redis lock released: {self.key}")
        except Exception as e:
            logger.warning(f"Error releasing Redis lock {self.key}: {e}")
    
    def __enter__(self):
        """Context manager support."""
        self.acquire()
        return self
    
    def __exit__(self, exc_type, exc_val, exc_tb):
        """Context manager support."""
        self.release()
    
    def __del__(self):
        """Освобождаем блокировку при удалении объекта (на всякий случай)."""
        if self._acquired:
            try:
                self.release()
            except Exception:
                pass
