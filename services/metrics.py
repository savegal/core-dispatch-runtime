"""
Базовые метрики для мониторинга системы.

ИСПРАВЛЕНО: Добавлены счетчики успехов/ошибок для улучшения наблюдаемости.
"""
import logging
from typing import Dict, Optional, List
from datetime import datetime, timezone
from services.redis_service import get_redis_client

logger = logging.getLogger("services.metrics")

# Префикс для ключей метрик в Redis
METRICS_PREFIX = "metrics:"


def _percentile(values: List[float], percentile: float) -> float:
    """Simple percentile helper (linear interpolation)."""
    if not values:
        return 0.0
    if len(values) == 1:
        return float(values[0])

    rank = max(0.0, min(100.0, percentile)) / 100.0 * (len(values) - 1)
    lo = int(rank)
    hi = min(lo + 1, len(values) - 1)
    frac = rank - lo
    return float(values[lo] + (values[hi] - values[lo]) * frac)


def increment_counter(counter_name: str, value: int = 1, tags: Optional[Dict[str, str]] = None) -> None:
    """
    ИСПРАВЛЕНО: Увеличивает счетчик метрики.
    
    Args:
        counter_name: Имя счетчика (например, "tasks.completed", "comments.posted")
        value: Значение для увеличения (по умолчанию 1)
        tags: Дополнительные теги для метрики (например, {"status": "success"})
    """
    try:
        redis_client = get_redis_client()
        
        # Формируем ключ метрики
        key = f"{METRICS_PREFIX}counter:{counter_name}"
        if tags:
            # Добавляем теги к ключу
            tag_str = ":".join(f"{k}={v}" for k, v in sorted(tags.items()))
            key = f"{key}:{tag_str}"
        
        # Увеличиваем счетчик
        redis_client.incrby(key, value)
        
        # Устанавливаем TTL (24 часа) для автоматической очистки старых метрик
        redis_client.expire(key, 86400)
        
    except Exception as e:
        logger.debug(f"Error incrementing counter {counter_name}: {e}")


def set_gauge(gauge_name: str, value: float, tags: Optional[Dict[str, str]] = None) -> None:
    """
    ИСПРАВЛЕНО: Устанавливает значение gauge метрики.
    
    Args:
        gauge_name: Имя gauge (например, "queue.length", "active.accounts")
        value: Значение для установки
        tags: Дополнительные теги для метрики
    """
    try:
        redis_client = get_redis_client()
        
        # Формируем ключ метрики
        key = f"{METRICS_PREFIX}gauge:{gauge_name}"
        if tags:
            tag_str = ":".join(f"{k}={v}" for k, v in sorted(tags.items()))
            key = f"{key}:{tag_str}"
        
        # Устанавливаем значение
        redis_client.set(key, str(value))
        
        # Устанавливаем TTL (1 час) для gauge метрик
        redis_client.expire(key, 3600)
        
    except Exception as e:
        logger.debug(f"Error setting gauge {gauge_name}: {e}")


def record_timing(timing_name: str, duration_ms: float, tags: Optional[Dict[str, str]] = None) -> None:
    """
    ИСПРАВЛЕНО: Записывает время выполнения операции.
    
    Args:
        timing_name: Имя операции (например, "comment.post", "verification.check")
        duration_ms: Длительность в миллисекундах
        tags: Дополнительные теги для метрики
    """
    try:
        redis_client = get_redis_client()
        
        # Формируем ключ метрики
        key = f"{METRICS_PREFIX}timing:{timing_name}"
        if tags:
            tag_str = ":".join(f"{k}={v}" for k, v in sorted(tags.items()))
            key = f"{key}:{tag_str}"
        
        # Добавляем значение в список (для вычисления среднего/медианы)
        redis_client.lpush(key, str(duration_ms))
        redis_client.ltrim(key, 0, 999)  # Храним последние 1000 значений
        redis_client.expire(key, 3600)  # TTL 1 час
        
    except Exception as e:
        logger.debug(f"Error recording timing {timing_name}: {e}")


def get_counter_value(counter_name: str, tags: Optional[Dict[str, str]] = None) -> int:
    """
    Получает текущее значение счетчика.
    
    Args:
        counter_name: Имя счетчика
        tags: Теги для метрики
        
    Returns:
        int: Текущее значение счетчика или 0 если не найден
    """
    try:
        redis_client = get_redis_client()
        
        key = f"{METRICS_PREFIX}counter:{counter_name}"
        if tags:
            tag_str = ":".join(f"{k}={v}" for k, v in sorted(tags.items()))
            key = f"{key}:{tag_str}"
        
        value = redis_client.get(key)
        return int(value) if value else 0
        
    except Exception as e:
        logger.debug(f"Error getting counter {counter_name}: {e}")
        return 0


def get_metrics_summary() -> Dict[str, any]:
    """
    ИСПРАВЛЕНО: Возвращает сводку метрик для мониторинга.
    
    Returns:
        dict: Словарь с метриками
    """
    try:
        redis_client = get_redis_client()

        # Use SCAN (cursor-based, non-blocking) instead of KEYS, which is O(N)
        # and will block the entire Redis server on large keyspaces. Bounded
        # to MAX_METRICS_KEYS_PER_TYPE per category to keep summary endpoints
        # responsive even under metric explosions (e.g. unbounded tag values).
        MAX_METRICS_KEYS_PER_TYPE = 5000

        def _scan_bounded(pattern: str) -> List[str]:
            collected: List[str] = []
            try:
                for raw in redis_client.scan_iter(match=pattern, count=500):
                    collected.append(raw if isinstance(raw, str) else raw.decode("utf-8", "ignore"))
                    if len(collected) >= MAX_METRICS_KEYS_PER_TYPE:
                        logger.warning(
                            "metrics summary: scan for %s capped at %d keys",
                            pattern, MAX_METRICS_KEYS_PER_TYPE,
                        )
                        break
            except Exception as scan_err:
                logger.debug("scan_iter failed for %s: %s", pattern, scan_err)
            return collected

        counter_keys = _scan_bounded(f"{METRICS_PREFIX}counter:*")
        gauge_keys = _scan_bounded(f"{METRICS_PREFIX}gauge:*")
        timing_keys = _scan_bounded(f"{METRICS_PREFIX}timing:*")
        
        metrics = {
            "counters": {},
            "gauges": {},
            "timings": {},
            "timestamp": datetime.now(timezone.utc).isoformat()
        }
        
        # Собираем счетчики
        for key in counter_keys:
            name = key.replace(f"{METRICS_PREFIX}counter:", "")
            value = redis_client.get(key)
            if value:
                metrics["counters"][name] = int(value)
        
        # Собираем gauges
        for key in gauge_keys:
            name = key.replace(f"{METRICS_PREFIX}gauge:", "")
            value = redis_client.get(key)
            if value:
                metrics["gauges"][name] = float(value)

        # Собираем timings (последние значения + p50/p95)
        for key in timing_keys:
            name = key.replace(f"{METRICS_PREFIX}timing:", "")
            raw_values = redis_client.lrange(key, 0, 999) or []
            parsed: List[float] = []
            for item in raw_values:
                try:
                    parsed.append(float(item))
                except Exception:
                    continue
            if not parsed:
                continue

            parsed.sort()
            count = len(parsed)
            metrics["timings"][name] = {
                "count": count,
                "avg_ms": round(sum(parsed) / count, 2),
                "p50_ms": round(_percentile(parsed, 50), 2),
                "p95_ms": round(_percentile(parsed, 95), 2),
                "min_ms": round(parsed[0], 2),
                "max_ms": round(parsed[-1], 2),
            }
        
        return metrics
        
    except Exception as e:
        logger.error(f"Error getting metrics summary: {e}")
        return {
            "error": str(e),
            "timestamp": datetime.now(timezone.utc).isoformat()
        }
