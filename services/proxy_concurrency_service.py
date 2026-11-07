"""
Service for tracking and limiting concurrent tasks per proxy.
Prevents proxy overloading by checking active task counts in Redis.
"""
import hashlib
import logging
import asyncio
import uuid
from dataclasses import dataclass, field
from contextlib import asynccontextmanager
from typing import Optional
from config.settings import get_settings
from services.redis_service import get_redis_client
from services.log_sanitizer import sanitize_url

logger = logging.getLogger("services.proxy_concurrency_service")
settings = get_settings()

_PROXY_USAGE_PREFIX = "proxy_usage:"
_PROXY_USAGE_TTL = 3600  # 1 hour TTL for usage counters (safety cleanup)
_PROXY_FAIL_PREFIX = "proxy_fail:"
_PROXY_FAIL_TTL = 7200  # 2 hours TTL for fail counters

# Read Redis TIME before the write scripts so Redis 3.0 can replicate them.
# Pending rotation still blocks new users, independent of worker clocks.
_LEASE_TTL = 60
_LEASE_ACQUIRE = """
local now = tonumber(ARGV[5])
redis.call('ZREMRANGEBYSCORE', KEYS[1], '-inf', now)
if ARGV[2] == 'rotate' then
  local owner = redis.call('GET', KEYS[2])
  if owner and owner ~= ARGV[1] then return 0 end
  redis.call('SET', KEYS[2], ARGV[1], 'EX', ARGV[3])
  if redis.call('ZCARD', KEYS[1]) > 0 then return 0 end
else
  if redis.call('EXISTS', KEYS[2]) == 1 then return 0 end
  if redis.call('ZCARD', KEYS[1]) >= tonumber(ARGV[4]) then return 0 end
  redis.call('ZADD', KEYS[1], now + tonumber(ARGV[3]), ARGV[1])
  redis.call('EXPIRE', KEYS[1], tonumber(ARGV[3]) * 2)
end
return 1
"""
_LEASE_RELEASE = """
redis.call('ZREM', KEYS[1], ARGV[1])
if redis.call('GET', KEYS[2]) == ARGV[1] then redis.call('DEL', KEYS[2]) end
return 1
"""
_LEASE_RENEW = """
for i = 3, #KEYS do
  if redis.call('GET', KEYS[i]) ~= ARGV[5] then return 0 end
end
if ARGV[2] == 'rotate' then
  if redis.call('GET', KEYS[2]) ~= ARGV[1] then return 0 end
  redis.call('EXPIRE', KEYS[2], ARGV[3])
else
  local now = tonumber(ARGV[4])
  local expiry = redis.call('ZSCORE', KEYS[1], ARGV[1])
  if not expiry or tonumber(expiry) <= tonumber(now) then return 0 end
  redis.call('ZADD', KEYS[1], now + tonumber(ARGV[3]), ARGV[1])
  redis.call('EXPIRE', KEYS[1], tonumber(ARGV[3]) * 2)
end
for i = 3, #KEYS do
  redis.call('EXPIRE', KEYS[i], ARGV[6])
end
return 1
"""


def _lease_client():
    from services.proxy_health_service import _get_proxy_health_client
    return _get_proxy_health_client()


def _eval_lease_at_server_time(script, keys, *args, tail=()):
    client = _lease_client()
    now, _ = client.time()
    return client.eval(script, len(keys), *keys, *args, now, *tail)


@dataclass
class ProxyUsageLease:
    keys: tuple[str, str]
    token: str = field(repr=False)
    mode: str = "use"
    renewal: asyncio.Task | None = field(default=None, repr=False)
    identity_keys: tuple[str, ...] = field(default=(), repr=False)
    identity_owner: str = field(default="", repr=False)
    identity_ttl: int = 0

    async def release(self):
        renewal, self.renewal = self.renewal, None
        if renewal is not None and renewal is not asyncio.current_task():
            renewal.cancel()
            await asyncio.gather(renewal, return_exceptions=True)
        try:
            await asyncio.to_thread(_lease_client().eval, _LEASE_RELEASE, 2, *self.keys, self.token)
        except Exception:
            logger.warning("Proxy lease release failed; token will expire", exc_info=False)


async def acquire_proxy_usage(proxy_str: str, *, rotation: bool = False, timeout: float = 30):
    from services.proxy_normalizer import ProxyRoute, ProxyRouteError
    route = ProxyRoute.parse(proxy_str)
    key = hashlib.sha256(route.url.encode()).hexdigest()
    lease = ProxyUsageLease((f"proxy:leases:{key}", f"proxy:rotation:{key}"), uuid.uuid4().hex,
                            "rotate" if rotation else "use")
    deadline = asyncio.get_running_loop().time() + timeout
    try:
        while True:
            granted = await asyncio.to_thread(
                _eval_lease_at_server_time, _LEASE_ACQUIRE, lease.keys,
                lease.token, lease.mode, _LEASE_TTL, max(1, settings.MAX_TASKS_PER_PROXY),
            )
            if granted:
                break
            if asyncio.get_running_loop().time() >= deadline:
                raise ProxyRouteError("proxy_capacity_timeout")
            await asyncio.sleep(0.1)
    except BaseException as exc:
        await lease.release()
        if isinstance(exc, (asyncio.CancelledError, ProxyRouteError)):
            raise
        raise ProxyRouteError("proxy_coordination_unavailable") from exc

    owner = asyncio.current_task()
    async def renew():
        try:
            while True:
                await asyncio.sleep(_LEASE_TTL / 3)
                ok = await asyncio.to_thread(
                    _eval_lease_at_server_time, _LEASE_RENEW,
                    (*lease.keys, *lease.identity_keys), lease.token, lease.mode, _LEASE_TTL,
                    tail=(lease.identity_owner, lease.identity_ttl),
                )
                if not ok:
                    raise ProxyRouteError("proxy_lease_lost")
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.error("Active proxy lease lost; cancelling its operation")
            owner.cancel()
    lease.renewal = asyncio.create_task(renew())
    return lease


@asynccontextmanager
async def proxy_operation(proxy_str: str | None, *, rotation=False):
    lease = await acquire_proxy_usage(proxy_str, rotation=rotation) if proxy_str else None
    try:
        yield lease
    finally:
        if lease is not None:
            await lease.release()

_INCR_LIMIT_LUA = """
local current = redis.call('GET', KEYS[1])
if current and tonumber(current) >= tonumber(ARGV[1]) then
    return 0
end
local val = redis.call('INCR', KEYS[1])
redis.call('EXPIRE', KEYS[1], ARGV[2])
return tonumber(val)
"""


def _normalize_key_input(proxy_str: str) -> str:
    if not proxy_str or not proxy_str.strip():
        return ""
    try:
        from services.proxy_service import normalize_proxy_url
        return normalize_proxy_url(proxy_str.strip())
    except Exception:
        return proxy_str.strip()


def _get_proxy_key(proxy_str: str) -> str:
    """Returns Redis key for proxy usage counter."""
    norm = _normalize_key_input(proxy_str)
    proxy_hash = hashlib.md5(norm.encode()).hexdigest()
    return f"{_PROXY_USAGE_PREFIX}{proxy_hash}"


def _get_fail_key(proxy_str: str) -> str:
    norm = _normalize_key_input(proxy_str)
    proxy_hash = hashlib.md5(norm.encode()).hexdigest()
    return f"{_PROXY_FAIL_PREFIX}{proxy_hash}"


def can_use_proxy(proxy_str: str) -> bool:
    """
    Checks if a proxy can be used for a new task (active_count < limit).
    
    Args:
        proxy_str: valid proxy string (e.g., 'socks5://user:pass@host:port')
        
    Returns:
        True if proxy has available slots, False otherwise.
    """
    if not proxy_str:
        return True # No proxy = no limit
        
    try:
        redis_client = get_redis_client()
        key = _get_proxy_key(proxy_str)
        count = redis_client.get(key)
        
        if count is None:
            return True
            
        return int(count) < settings.MAX_TASKS_PER_PROXY
    except Exception as e:
        logger.error(f"Error checking proxy usage for {sanitize_url(proxy_str, max_length=80)}: {e}")
        return True # Fail open to avoid blocking everything on Redis error


def increment_proxy_usage(proxy_str: str) -> bool:
    """
    Increments usage counter for a proxy.
    Checks limit ATOMICALLY.
    
    Returns:
        True if successfully incremented and within limit.
        False if limit reached (counter is decremented back).
    """
    if not proxy_str:
        return True

    try:
        redis_client = get_redis_client()
        key = _get_proxy_key(proxy_str)
        
        try:
            res = redis_client.eval(_INCR_LIMIT_LUA, 1, key, settings.MAX_TASKS_PER_PROXY, _PROXY_USAGE_TTL)
            if res and int(res) > 0:
                logger.debug(f"Proxy {sanitize_url(proxy_str, max_length=80)} usage incremented to {res}")
                return True
            else:
                logger.debug(f"Proxy {sanitize_url(proxy_str, max_length=80)} usage limit reached, rejected atomically.")
                return False
        except Exception:
            # Fallback for environments / mocks where eval is not supported
            new_count = redis_client.incr(key)
            redis_client.expire(key, _PROXY_USAGE_TTL)
            if new_count > settings.MAX_TASKS_PER_PROXY:
                redis_client.decr(key)
                logger.debug(f"Proxy {sanitize_url(proxy_str, max_length=80)} usage limit reached ({new_count-1} -> {new_count}), reverted.")
                return False
            logger.debug(f"Proxy {sanitize_url(proxy_str, max_length=80)} usage incremented to {new_count}")
            return True
    except Exception as e:
        logger.error(f"Error incrementing proxy usage: {e}")
        return True # Fail open


def decrement_proxy_usage(proxy_str: str):
    """Decrements usage counter for a proxy."""
    if not proxy_str:
        return

    try:
        redis_client = get_redis_client()
        key = _get_proxy_key(proxy_str)
        
        new_count = redis_client.decr(key)
        if new_count < 0:
            redis_client.set(key, 0)
            
        logger.debug(f"Proxy {sanitize_url(proxy_str, max_length=80)} usage decremented to {max(0, new_count)}")
    except Exception as e:
        logger.error(f"Error decrementing proxy usage: {e}")


def increment_proxy_fail(proxy_str: str) -> None:
    """Increments fail counter for a proxy (used on proxy/timeouts/auth errors)."""
    if not proxy_str:
        return
    try:
        redis_client = get_redis_client()
        key = _get_fail_key(proxy_str)
        new_count = redis_client.incr(key)
        redis_client.expire(key, _PROXY_FAIL_TTL)
        logger.debug(f"Proxy {sanitize_url(proxy_str, max_length=80)} fail count incremented to {new_count}")
    except Exception as e:
        logger.error(f"Error incrementing proxy fail counter: {e}")


def should_block_proxy(proxy_str: str) -> bool:
    """
    Returns True if proxy exceed fail threshold (settings.PROXY_FAIL_THRESHOLD).
    Fail-open on Redis errors.
    """
    if not proxy_str:
        return False
    try:
        redis_client = get_redis_client()
        key = _get_fail_key(proxy_str)
        count = redis_client.get(key)
        if count is None:
            return False
        return int(count) >= settings.PROXY_FAIL_THRESHOLD
    except Exception as e:
        logger.error(f"Error checking proxy fail counter: {e}")
        return False


def get_active_proxy_count(proxy_str: str) -> int:
    """Returns current active task count for a proxy."""
    if not proxy_str:
        return 0
        
    try:
        redis_client = get_redis_client()
        key = _get_proxy_key(proxy_str)
        count = redis_client.get(key)
        return int(count) if count else 0
    except Exception:
        return 0
