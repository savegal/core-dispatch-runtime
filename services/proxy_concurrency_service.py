"""
Service for tracking and limiting concurrent tasks per proxy.
Prevents proxy overloading by checking active task counts in Redis.
"""
import logging
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

def _get_proxy_key(proxy_str: str) -> str:
    """Returns Redis key for proxy usage counter."""
    # Normalize proxy string (remove protocol for consistent keys)
    # Using hash might be safer if proxies are long/complex, but raw string is easier to debug
    import hashlib
    proxy_hash = hashlib.md5(proxy_str.strip().encode()).hexdigest()
    return f"{_PROXY_USAGE_PREFIX}{proxy_hash}"

def _get_fail_key(proxy_str: str) -> str:
    import hashlib
    proxy_hash = hashlib.md5(proxy_str.strip().encode()).hexdigest()
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
        
        # INCR is atomic
        new_count = redis_client.incr(key)
        redis_client.expire(key, _PROXY_USAGE_TTL) # Refresh TTL
        
        if new_count > settings.MAX_TASKS_PER_PROXY:
            # Over limit, revert
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
        
        # DECR is atomic
        # Use lua script to prevent negative values? Redis DECR allows negative.
        # Simple DECR is fine, maybe check if < 0 and reset.
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
