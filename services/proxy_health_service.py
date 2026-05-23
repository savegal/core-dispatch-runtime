"""
Proxy health backoff using Redis.
Marks proxies as temporarily bad after network errors to avoid repeated failures.
"""
from __future__ import annotations

import json
import logging
import time
from typing import Iterable

import redis

from config.settings import get_settings
from services.proxy_normalizer import normalize_proxy_string


logger = logging.getLogger("services.proxy_health")

_proxy_health_unavailable_until = 0.0
_proxy_health_fail_open_seconds = 30.0
_proxy_health_client: redis.Redis | None = None


def _proxy_health_is_available() -> bool:
    return time.monotonic() >= _proxy_health_unavailable_until


def _mark_proxy_health_unavailable(error: Exception) -> None:
    global _proxy_health_unavailable_until, _proxy_health_client
    _proxy_health_unavailable_until = time.monotonic() + _proxy_health_fail_open_seconds
    if _proxy_health_client is not None:
        try:
            _proxy_health_client.close()
        except Exception:
            pass
        _proxy_health_client = None
    logger.debug(f"Temporarily disabling proxy health checks after Redis error: {error}")


def _get_proxy_health_client() -> redis.Redis:
    global _proxy_health_client
    if _proxy_health_client is None:
        settings = get_settings()
        _proxy_health_client = redis.from_url(
            settings.REDIS_URL,
            decode_responses=True,
            socket_connect_timeout=0.5,
            socket_timeout=0.5,
            retry_on_timeout=False,
            health_check_interval=0,
            max_connections=8,
        )
    return _proxy_health_client


def _normalize_proxy_key(proxy: str) -> str:
    raw = proxy.strip()
    normalized = normalize_proxy_string(raw, default_scheme=None)
    return normalized or raw


def _key_bad(proxy: str) -> str:
    return f"proxy:bad:{_normalize_proxy_key(proxy)}"


def _key_count(proxy: str) -> str:
    return f"proxy:bad_count:{_normalize_proxy_key(proxy)}"


def is_proxy_bad(proxy: str) -> bool:
    if not proxy or not proxy.strip():
        return False
    if not _proxy_health_is_available():
        return False
    proxy = proxy.strip()
    try:
        redis_client = _get_proxy_health_client()
        return bool(redis_client.exists(_key_bad(proxy)))
    except Exception as e:
        _mark_proxy_health_unavailable(e)
        logger.debug(f"Failed to check proxy bad status: {e}")
        return False


def get_bad_proxy_set(proxies: Iterable[str]) -> set[str]:
    if not _proxy_health_is_available():
        return set()
    if _proxy_health_client is None:
        return set()

    keys_by_proxy: dict[str, str] = {}
    for proxy in proxies:
        if not proxy or not str(proxy).strip():
            continue
        normalized_proxy = str(proxy).strip()
        keys_by_proxy.setdefault(normalized_proxy, _key_bad(normalized_proxy))

    if not keys_by_proxy:
        return set()

    try:
        redis_client = _proxy_health_client
        pipe = redis_client.pipeline(transaction=False)
        for key in keys_by_proxy.values():
            pipe.exists(key)
        results = pipe.execute()
        return {
            proxy
            for proxy, exists in zip(keys_by_proxy.keys(), results)
            if bool(exists)
        }
    except Exception as e:
        _mark_proxy_health_unavailable(e)
        logger.debug(f"Failed to batch-check bad proxy status: {e}")
        return set()


def mark_proxy_bad(proxy: str, reason: str | None = None) -> None:
    if not proxy or not proxy.strip():
        return
    if not _proxy_health_is_available():
        return
    proxy = proxy.strip()
    settings = get_settings()
    base_ttl = getattr(settings, "PROXY_BAD_TTL_SECONDS", 120)
    max_ttl = getattr(settings, "PROXY_BAD_TTL_MAX_SECONDS", 1800)
    window = getattr(settings, "PROXY_BAD_TTL_WINDOW_SECONDS", 3600)
    try:
        redis_client = _get_proxy_health_client()
        count_key = _key_count(proxy)
        bad_key = _key_bad(proxy)
        pipe = redis_client.pipeline()
        pipe.incr(count_key)
        pipe.expire(count_key, window)
        count, _ = pipe.execute()
        safe_count = max(int(count or 1), 1)
        ttl = min(base_ttl * safe_count, max_ttl)
        payload = {"reason": reason or "unknown", "ts": int(time.time())}
        redis_client.set(bad_key, json.dumps(payload, ensure_ascii=False), ex=int(ttl))
    except Exception as e:
        _mark_proxy_health_unavailable(e)
        logger.debug(f"Failed to mark proxy bad: {e}")


def clear_proxy_bad(proxy: str, *, reset_count: bool = True) -> None:
    if not proxy or not proxy.strip():
        return
    if not _proxy_health_is_available():
        return
    proxy = proxy.strip()
    try:
        redis_client = _get_proxy_health_client()
        keys = [_key_bad(proxy)]
        if reset_count:
            keys.append(_key_count(proxy))
        redis_client.delete(*keys)
    except Exception as e:
        _mark_proxy_health_unavailable(e)
        logger.debug(f"Failed to clear proxy bad: {e}")


def clear_all_bad_proxies() -> int:
    """
    Удаляет ВСЕ bad-proxy маркеры из Redis.

    Используется при админском сбросе cooldown'ов — чтобы аккаунты
    с «плохими» прокси снова попали в пул выборки.

    Возвращает количество удалённых ключей.
    """
    if not _proxy_health_is_available():
        return 0
    try:
        redis_client = _get_proxy_health_client()
        bad_keys = list(redis_client.scan_iter(match="proxy:bad:*", count=500))
        count_keys = list(redis_client.scan_iter(match="proxy:bad_count:*", count=500))
        all_keys = bad_keys + count_keys
        if not all_keys:
            return 0
        deleted = redis_client.delete(*all_keys)
        logger.info(f"Cleared {deleted} bad-proxy keys from Redis")
        return int(deleted or 0)
    except Exception as e:
        _mark_proxy_health_unavailable(e)
        logger.warning(f"Failed to clear all bad proxies: {e}")
        return 0
