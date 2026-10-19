from datetime import datetime, timezone, timedelta
from sqlalchemy.orm import Session
from db.models import CreatorSource, CreatorVideo, MonitorOutbox, Task, PendingEnqueue
from config.settings import get_settings
from services.redis_service import get_redis_client
import logging
import time
import hashlib
from decimal import Decimal
from services.metrics import set_gauge
import asyncio
import threading
from urllib.parse import parse_qsl, urlencode, urlparse, urlunparse

settings = get_settings()
logger = logging.getLogger("services.task_service")
_local_dedup_lock = threading.Lock()
_local_dedup_cache: dict[str, float] = {}


def _claim_local_dedup_key(key: str, ttl_seconds: int) -> bool:
    """
    Process-local fallback dedup when Redis is unavailable.
    """
    now_ts = time.time()
    ttl = max(1, int(ttl_seconds))
    expires_at = now_ts + ttl

    with _local_dedup_lock:
        stale_keys = [k for k, exp_ts in _local_dedup_cache.items() if exp_ts <= now_ts]
        for stale in stale_keys:
            _local_dedup_cache.pop(stale, None)

        current_expiry = _local_dedup_cache.get(key)
        if current_expiry and current_expiry > now_ts:
            return False

        _local_dedup_cache[key] = expires_at
        return True

# ИСПРАВЛЕНО: Используем централизованный Redis клиент
def get_redis():
    """Получает централизованный Redis клиент."""
    return get_redis_client()

# Максимальное время, которое задача может быть в статусе "in_progress".
# Живой executor останавливает задачу сам; это страховка для recovery после падения.
TASK_TIMEOUT_SECONDS = int(getattr(settings, "TASK_PROCESSING_TIMEOUT", 1800) or 1800)
TASK_QUEUE_MARKER_TTL = 24 * 3600
TASK_QUEUE_PRIORITY_HIGH = "high"
TASK_QUEUE_PRIORITY_NORMAL = "normal"
TASK_QUEUE_PRIORITY_LOW = "low"
AUTOPILOT_QUEUE_ORIGINS = {"autopilot", "monitor_auto_comment"}


def _task_queue_marker_key(task_id: int) -> str:
    return f"task:queued:{task_id}"


def _priority_task_queue_name() -> str:
    return f"{settings.REDIS_TASK_QUEUE}:priority"


def _high_priority_task_queue_name() -> str:
    return f"{settings.REDIS_TASK_QUEUE}:high"


def _task_queue_names() -> tuple[str, str, str]:
    return (_high_priority_task_queue_name(), _priority_task_queue_name(), settings.REDIS_TASK_QUEUE)


def _task_origin(task: Task | None) -> str:
    task_settings = getattr(task, "settings", None)
    if isinstance(task_settings, dict):
        return str(task_settings.get("origin") or task_settings.get("source") or "manual").strip().lower()
    return "manual"


def _normalize_queue_priority(value: object, default: str | None = None) -> str | None:
    raw = str(value or "").strip().lower()
    if raw in {"high", "urgent", "vip", "top", "highest"}:
        return TASK_QUEUE_PRIORITY_HIGH
    if raw in {"normal", "manual", "medium", "priority"}:
        return TASK_QUEUE_PRIORITY_NORMAL
    if raw in {"low", "autopilot", "background"}:
        return TASK_QUEUE_PRIORITY_LOW
    return default


def _task_queue_priority_level(task: Task | None) -> str:
    task_settings = getattr(task, "settings", None)
    if isinstance(task_settings, dict):
        explicit = _normalize_queue_priority(
            task_settings.get("queue_priority")
            or task_settings.get("task_queue_priority")
            or task_settings.get("priority")
        )
        if explicit:
            return explicit

    if _task_origin(task) in AUTOPILOT_QUEUE_ORIGINS:
        return TASK_QUEUE_PRIORITY_LOW
    return TASK_QUEUE_PRIORITY_NORMAL


def _task_uses_priority_queue(task: Task | None) -> bool:
    return _task_queue_priority_level(task) != TASK_QUEUE_PRIORITY_LOW


def task_uses_priority_queue(task: Task | None) -> bool:
    return _task_uses_priority_queue(task)


def task_queue_priority_level(task: Task | None) -> str:
    return _task_queue_priority_level(task)


def _queue_name_for_priority_level(priority_level: str | None) -> str:
    normalized = _normalize_queue_priority(priority_level, TASK_QUEUE_PRIORITY_NORMAL)
    if normalized == TASK_QUEUE_PRIORITY_HIGH:
        return _high_priority_task_queue_name()
    if normalized == TASK_QUEUE_PRIORITY_LOW:
        return settings.REDIS_TASK_QUEUE
    return _priority_task_queue_name()


def _queue_name_for_enqueue(*, priority: bool = False, priority_level: str | None = None) -> str:
    if priority_level is not None:
        return _queue_name_for_priority_level(priority_level)
    return _priority_task_queue_name() if priority else settings.REDIS_TASK_QUEUE


def _queued_length(redis_client) -> int:
    total = 0
    for queue_name in _task_queue_names():
        try:
            total += int(redis_client.llen(queue_name) or 0)
        except Exception:
            continue
    return total


def _claim_queue_marker(task_id: int) -> bool:
    redis_client = get_redis()
    return bool(redis_client.set(_task_queue_marker_key(task_id), "1", nx=True, ex=TASK_QUEUE_MARKER_TTL))


def _release_queue_marker(task_id: int) -> None:
    try:
        redis_client = get_redis()
        redis_client.delete(_task_queue_marker_key(task_id))
    except Exception as e:
        logger.debug(f"Failed to release queue marker for task {task_id}: {e}")


def remove_tasks_from_queue(task_ids: list[int] | set[int] | tuple[int, ...]) -> int:
    """Removes pending task ids from Redis queue after user-side cancellation."""
    normalized_ids = sorted({task_id for task_id in (_coerce_task_id(item) for item in task_ids) if task_id is not None})
    if not normalized_ids:
        return 0

    try:
        redis_client = get_redis()
    except Exception as exc:
        logger.warning("Failed to access Redis while removing cancelled tasks from queue: %s", exc)
        return 0

    removed = 0
    for task_id in normalized_ids:
        try:
            for queue_name in _task_queue_names():
                removed += int(redis_client.lrem(queue_name, 0, str(task_id)) or 0)
            redis_client.delete(_task_queue_marker_key(task_id))
        except Exception as exc:
            logger.warning("Failed to remove cancelled task %s from Redis queue: %s", task_id, exc)
    return removed


def prune_stale_task_queue_entries(db: Session) -> int:
    """Remove Redis queue entries whose DB task is no longer pending."""
    try:
        redis_client = get_redis()
        raw_items = []
        for queue_name in _task_queue_names():
            raw_items.extend(redis_client.lrange(queue_name, 0, -1) or [])
    except Exception as exc:
        logger.warning("Failed to inspect task queue for stale entries: %s", exc)
        return 0

    queued_ids = [task_id for task_id in (_coerce_task_id(item) for item in raw_items or []) if task_id is not None]
    if not queued_ids:
        return 0

    unique_ids = sorted(set(queued_ids))
    rows = db.query(Task.id, Task.status).filter(Task.id.in_(unique_ids)).all()
    pending_ids = {int(task_id) for task_id, status in rows if str(status or "").lower() == "pending"}
    existing_ids = {int(task_id) for task_id, _status in rows}
    stale_ids = [task_id for task_id in unique_ids if task_id not in pending_ids or task_id not in existing_ids]
    if not stale_ids:
        return 0

    removed = remove_tasks_from_queue(stale_ids)
    if removed:
        logger.info(
            "Pruned %s stale task queue entr%s for non-pending/missing task ids: %s",
            removed,
            "y" if removed == 1 else "ies",
            stale_ids[:20],
        )
    return removed


def _coerce_task_id(value) -> int | None:
    try:
        if isinstance(value, bytes):
            value = value.decode("utf-8", errors="ignore")
        return int(str(value))
    except (TypeError, ValueError):
        return None


def _read_redis_queued_task_ids() -> set[int] | None:
    """
    Returns ids currently waiting in the Redis queue.

    None means Redis could not be inspected, so callers must avoid destructive
    recovery decisions based on an unknown queue state.
    """
    try:
        redis_client = get_redis()
        raw_items = []
        for queue_name in _task_queue_names():
            raw_items.extend(redis_client.lrange(queue_name, 0, -1) or [])
    except Exception as exc:
        logger.warning(
            "Skipping pending timeout recovery: cannot inspect Redis task queue: %s",
            exc,
        )
        return None

    queued_ids: set[int] = set()
    for item in raw_items or []:
        task_id = _coerce_task_id(item)
        if task_id is not None:
            queued_ids.add(task_id)
    return queued_ids


def _read_queue_marker_task_ids(task_ids: list[int]) -> set[int] | None:
    if not task_ids:
        return set()

    try:
        redis_client = get_redis()
        marker_values = redis_client.mget([_task_queue_marker_key(task_id) for task_id in task_ids])
    except Exception as exc:
        logger.warning(
            "Skipping pending timeout recovery: cannot inspect task queue markers: %s",
            exc,
        )
        return None

    return {
        task_id
        for task_id, marker_value in zip(task_ids, marker_values or [])
        if marker_value
    }


def _enqueue_autopilot_retry_for_reset_videos(
    db: Session,
    *,
    task_id: int,
    video_rows: list[tuple[int, int, int | None, str | None, bool | None]],
    available_at: datetime,
) -> int:
    grouped: dict[int, dict[str, object]] = {}
    for video_id, creator_id, blogger_id, creator_mode, creator_enabled in video_rows:
        if creator_mode != "auto_comment" or not creator_enabled:
            continue
        group = grouped.setdefault(
            int(creator_id),
            {"blogger_id": blogger_id, "video_ids": []},
        )
        group["video_ids"].append(int(video_id))

    created = 0
    for creator_id, group in grouped.items():
        video_ids = sorted(set(group["video_ids"]))
        if not video_ids:
            continue
        digest = hashlib.sha1(",".join(map(str, video_ids)).encode("utf-8")).hexdigest()[:16]
        dedupe_key = f"autopilot_retry_pending_timeout:{task_id}:{creator_id}:{digest}"
        if db.query(MonitorOutbox).filter(MonitorOutbox.dedupe_key == dedupe_key).first():
            continue
        db.add(
            MonitorOutbox(
                event_type="autopilot_create",
                status="pending",
                dedupe_key=dedupe_key,
                blogger_id=group["blogger_id"],
                creator_id=creator_id,
                payload={"creator_id": creator_id, "video_ids": video_ids},
                attempts=0,
                available_at=available_at,
            )
        )
        created += 1
    return created


def requeue_deferred_task(task_id: int, *, priority: bool = False, priority_level: str | None = None) -> None:
    """Puts a popped task back as newest so older eligible tasks can proceed."""
    marker_claimed = False
    try:
        marker_claimed = _claim_queue_marker(task_id)
        redis_client = get_redis()
        queue_name = _queue_name_for_enqueue(priority=priority, priority_level=priority_level)
        redis_client.lpush(queue_name, str(task_id))
    except Exception:
        if marker_claimed:
            _release_queue_marker(task_id)
        raise


def enqueue_task(
    task_id: int,
    retries: int = 3,
    *,
    priority: bool = False,
    priority_level: str | None = None,
) -> None:
    """
    Добавляет задачу в очередь Redis с retry при ошибках подключения.
    
    ИСПРАВЛЕНО: Добавлена обработка недоступности Redis с автоматическим retry.
    """
    from redis.exceptions import ConnectionError, TimeoutError
    
    redis_client = get_redis()
    queue_name = _queue_name_for_enqueue(priority=priority, priority_level=priority_level)
    for attempt in range(retries):
        try:
            redis_client.lpush(queue_name, str(task_id))
            logger.debug(
                "Task %s enqueued successfully (priority=%s, priority_level=%s)",
                task_id,
                priority,
                priority_level,
            )
            try:
                queue_len = _queued_length(redis_client)
                set_gauge("queue.length", queue_len)
            except Exception:
                pass
            return
        except (ConnectionError, TimeoutError) as e:
            if attempt < retries - 1:
                wait_time = 2 ** attempt  # Exponential backoff: 1s, 2s, 4s
                logger.warning(
                    f"Redis connection failed (attempt {attempt + 1}/{retries}), "
                    f"retrying in {wait_time}s: {e}"
                )
                time.sleep(wait_time)
                continue
            else:
                logger.error(
                    f"Failed to enqueue task {task_id} after {retries} attempts. "
                    f"Redis is unavailable. Caller must use reserve/recovery flow to avoid task loss."
                )
                # Пробрасываем исключение, чтобы вызывающий код мог обработать
                raise Exception(f"Redis unavailable: {e}")
        except Exception as e:
            logger.error(f"Unexpected error enqueueing task {task_id}: {e}")
            raise


def enqueue_task_once(
    task_id: int,
    retries: int = 3,
    *,
    priority: bool = False,
    priority_level: str | None = None,
) -> str:
    """
    Enqueues a task only once across the normal flow and recovery flow.

    Returns:
        "enqueued" if the task was pushed to Redis.
        "already_queued" if another flow already pushed it.
    """
    marker_claimed = False
    try:
        marker_claimed = _claim_queue_marker(task_id)
        if not marker_claimed:
            logger.info(f"Task {task_id} is already queued, skipping duplicate enqueue")
            return "already_queued"
        enqueue_task(task_id, retries=retries, priority=priority, priority_level=priority_level)
        return "enqueued"
    except Exception:
        if marker_claimed:
            _release_queue_marker(task_id)
        raise


def promote_task_to_high_priority(task_id: int) -> dict[str, object]:
    """Moves a pending queued task into the highest-priority Redis queue."""
    task_id_int = int(task_id)
    redis_client = get_redis()
    removed = 0
    try:
        removed += int(redis_client.lrem(_high_priority_task_queue_name(), 0, str(task_id_int)) or 0)
    except Exception as exc:
        logger.warning(
            "Task %s is being promoted, but old high-priority queue copy could not be removed: %s",
            task_id_int,
            exc,
        )
    redis_client.lpush(_high_priority_task_queue_name(), str(task_id_int))
    try:
        redis_client.set(_task_queue_marker_key(task_id_int), "1", ex=TASK_QUEUE_MARKER_TTL)
    except Exception as exc:
        logger.warning("Failed to refresh queue marker while promoting task %s: %s", task_id_int, exc)

    for queue_name in (_priority_task_queue_name(), settings.REDIS_TASK_QUEUE):
        try:
            removed += int(redis_client.lrem(queue_name, 0, str(task_id_int)) or 0)
        except Exception as exc:
            logger.warning(
                "Task %s promoted to high priority, but old queue copy in %s could not be removed: %s",
                task_id_int,
                queue_name,
                exc,
            )
    return {"removed": removed, "enqueue_state": "enqueued"}


def reserve_task_for_enqueue(db: Session, task_id: int) -> None:
    """Adds a task to pending_enqueue without committing the current transaction."""
    existing = db.query(PendingEnqueue).filter(PendingEnqueue.task_id == task_id).first()
    if existing:
        return
    db.add(PendingEnqueue(task_id=task_id))


def _drop_pending_enqueue(db: Session, task_id: int) -> bool:
    row = db.query(PendingEnqueue).filter(PendingEnqueue.task_id == task_id).first()
    if not row:
        return False
    db.delete(row)
    return True


def finalize_reserved_enqueue(db: Session, task_id: int) -> bool:
    """
    Tries to enqueue a reserved task and removes pending_enqueue on success.
    Keeps the reserve row if Redis is unavailable.
    """
    task = db.query(Task).filter(Task.id == task_id).first()
    if task and task.status != "pending":
        try:
            removed = _drop_pending_enqueue(db, task_id)
            if removed:
                db.commit()
            logger.info(f"Removed stale pending reserve for task {task_id} with status={task.status}")
            return True
        except Exception as e:
            db.rollback()
            logger.error(f"Failed to clear stale pending reserve for task {task_id}: {e}", exc_info=True)
            return False

    try:
        enqueue_state = enqueue_task_once(
            task_id,
            retries=1,
            priority_level=_task_queue_priority_level(task),
        )
    except Exception as e:
        logger.warning(f"Failed to finalize reserved enqueue for task {task_id}: {e}")
        return False

    try:
        removed = _drop_pending_enqueue(db, task_id)
        if removed:
            db.commit()
        logger.info(f"Task {task_id} reserve finalized ({enqueue_state})")
        return True
    except Exception as e:
        db.rollback()
        logger.error(f"Failed to remove pending reserve for task {task_id}: {e}", exc_info=True)
        return False


def _save_pending_enqueue(db: Session, task_id: int) -> None:
    """Сохраняет задачу в резерв, если очередь недоступна."""
    try:
        reserve_task_for_enqueue(db, task_id)
        db.commit()
        logger.warning(f"Task {task_id} saved to pending_enqueue (Redis unavailable)")
    except Exception as e:
        db.rollback()
        logger.error(f"Failed to save task {task_id} to pending_enqueue: {e}")


async def reserve_task_for_enqueue_async(db, task_id: int) -> None:
    """Async variant of reserve_task_for_enqueue without committing."""
    from sqlalchemy import select

    result = await db.execute(select(PendingEnqueue).filter(PendingEnqueue.task_id == task_id))
    if result.scalar_one_or_none():
        return
    db.add(PendingEnqueue(task_id=task_id))


async def _drop_pending_enqueue_async(db, task_id: int) -> bool:
    from sqlalchemy import select

    result = await db.execute(select(PendingEnqueue).filter(PendingEnqueue.task_id == task_id))
    row = result.scalar_one_or_none()
    if not row:
        return False
    await db.delete(row)
    return True


async def finalize_reserved_enqueue_async(db, task_id: int) -> bool:
    """Async variant of finalize_reserved_enqueue."""
    from sqlalchemy import select

    result = await db.execute(select(Task).filter(Task.id == task_id))
    task = result.scalar_one_or_none()
    if task and task.status != "pending":
        try:
            removed = await _drop_pending_enqueue_async(db, task_id)
            if removed:
                await db.commit()
            logger.info(f"Removed stale pending reserve for task {task_id} with status={task.status} (async)")
            return True
        except Exception as e:
            await db.rollback()
            logger.error(f"Failed to clear stale pending reserve for task {task_id} (async): {e}", exc_info=True)
            return False

    try:
        enqueue_state = await asyncio.to_thread(
            enqueue_task_once,
            task_id,
            1,
            priority_level=_task_queue_priority_level(task),
        )
    except Exception as e:
        logger.warning(f"Failed to finalize reserved enqueue for task {task_id} (async): {e}")
        return False

    try:
        removed = await _drop_pending_enqueue_async(db, task_id)
        if removed:
            await db.commit()
        logger.info(f"Task {task_id} reserve finalized ({enqueue_state}, async)")
        return True
    except Exception as e:
        await db.rollback()
        logger.error(f"Failed to remove pending reserve for task {task_id} (async): {e}", exc_info=True)
        return False


async def _save_pending_enqueue_async(db, task_id: int) -> None:
    """Async версия сохранения в резерв."""
    try:
        await reserve_task_for_enqueue_async(db, task_id)
        await db.commit()
        logger.warning(f"Task {task_id} saved to pending_enqueue (Redis unavailable, async)")
    except Exception as e:
        await db.rollback()
        logger.error(f"Failed to save task {task_id} to pending_enqueue (async): {e}")


async def enqueue_with_reserve_async(db, task_id: int) -> None:
    """
    Пытается положить задачу в Redis, при ошибке сохраняет в pending_enqueue.
    """
    try:
        await reserve_task_for_enqueue_async(db, task_id)
        await db.commit()
    except Exception as e:
        await db.rollback()
        logger.error(f"Failed to reserve pending enqueue for task {task_id}: {e}", exc_info=True)
        return

    await finalize_reserved_enqueue_async(db, task_id)


def dequeue_task(timeout: int = 5) -> int | None:
    """
    Блокирующая операция получения задачи из очереди.
    Возвращает task_id или None если таймаут.
    
    ИСПРАВЛЕНО: Добавлена обработка ошибок подключения к Redis с уменьшением логирования.
    """
    from redis.exceptions import ConnectionError, TimeoutError
    
    redis_client = get_redis()
    try:
        result = redis_client.brpop(
            [_high_priority_task_queue_name(), _priority_task_queue_name(), settings.REDIS_TASK_QUEUE],
            timeout=timeout,
        )
        if result:
            raw_task_id = result[1]
            task_id = int(raw_task_id.decode("utf-8") if isinstance(raw_task_id, bytes) else raw_task_id)
            _release_queue_marker(task_id)
            return task_id
        return None
    except (ConnectionError, TimeoutError) as e:
        # ИСПРАВЛЕНО: Логируем только раз в минуту, чтобы не засорять логи
        import time
        if not hasattr(dequeue_task, '_last_error_log') or time.time() - dequeue_task._last_error_log > 60:
            logger.warning(f"Redis connection error in dequeue_task (will retry): {e}")
            dequeue_task._last_error_log = time.time()
        # Возвращаем None, воркер попробует снова в следующей итерации
        return None
    except Exception as e:
        logger.error(f"Unexpected error in dequeue_task: {e}")
        return None


def get_queue_length() -> int:
    """Возвращает длину очереди."""
    redis_client = get_redis()
    return _queued_length(redis_client)


def _normalize_text_for_dedup(value: str | None) -> str:
    """Normalizes text for stable dedup keys (case/whitespace insensitive)."""
    return " ".join((value or "").strip().lower().split())


def _normalize_video_url_for_dedup(url: str | None) -> str:
    """Normalizes URL shape for stable dedup keys."""
    raw = (url or "").strip()
    if not raw:
        return ""
    if not raw.startswith(("http://", "https://")):
        raw = f"https://{raw}"
    try:
        parsed = urlparse(raw)
        scheme = (parsed.scheme or "https").lower()
        netloc = (parsed.netloc or "").lower().strip()
        path = (parsed.path or "").rstrip("/")
        query_pairs = parse_qsl(parsed.query, keep_blank_values=True)
        query_pairs.sort(key=lambda item: (item[0], item[1]))
        query = urlencode(query_pairs, doseq=True)
        normalized = urlunparse((scheme, netloc, path, "", query, ""))
        return normalized.rstrip("?")
    except Exception:
        return raw.lower()


def make_dedup_key(user_id: int, main_comment: str, video_urls: list[str], scope: str = "") -> str:
    """
    Формирует ключ дедупликации на основе пользователя, текста и списка видео.
    """
    normalized_text = _normalize_text_for_dedup(main_comment)
    videos_normalized = [
        _normalize_video_url_for_dedup(video_url)
        for video_url in (video_urls or [])
        if (video_url or "").strip()
    ]
    videos_sorted = sorted(set(videos_normalized))
    normalized_scope = (scope or "").strip()
    payload = f"{user_id}|{normalized_text}|{'|'.join(videos_sorted)}|{normalized_scope}"
    digest = hashlib.sha256(payload.encode("utf-8")).hexdigest()
    return f"task_dedup:{digest}"


def claim_dedup_key(
    user_id: int,
    main_comment: str,
    video_urls: list[str],
    ttl_seconds: int = 7200,
    scope: str = "",
) -> bool:
    """
    Регистрирует ключ дедупликации в Redis. Возвращает True если успешно установили (нет дубликата).
    """
    key = make_dedup_key(user_id, main_comment, video_urls, scope=scope)
    try:
        redis_client = get_redis()
        return bool(redis_client.set(key, "1", nx=True, ex=ttl_seconds))
    except Exception as e:
        fallback_result = _claim_local_dedup_key(key, ttl_seconds=ttl_seconds)
        logger.warning(
            f"Dedup claim failed in Redis, using process-local fallback: {e}. "
            f"fallback_result={fallback_result}"
        )
        return fallback_result


# --- Статистика по текстам (shadow-ban detection) ---

def _text_stats_key(text: str) -> str:
    digest = hashlib.sha256((text or "").strip().encode("utf-8")).hexdigest()
    return f"text_stats:{digest}"


def record_text_result(text: str, outcome: str, ttl_hours: int) -> None:
    """
    outcome: success | shadow | error
    """
    try:
        redis_client = get_redis()
        key = _text_stats_key(text)
        field = f"{outcome}_count"
        redis_client.hincrby(key, field, 1)
        redis_client.expire(key, ttl_hours * 3600)
    except Exception as e:
        logger.debug(f"record_text_result failed: {e}")


def get_text_stats(text: str) -> dict:
    try:
        redis_client = get_redis()
        key = _text_stats_key(text)
        data = redis_client.hgetall(key) or {}
        return {
            "success": int(data.get("success_count", 0)),
            "shadow": int(data.get("shadow_count", 0)),
            "error": int(data.get("error_count", 0)),
        }
    except Exception as e:
        logger.debug(f"get_text_stats failed: {e}")
        return {"success": 0, "shadow": 0, "error": 0}


def recover_tasks_lost_in_redis(db: Session) -> int:
    """
    ИСПРАВЛЕНО: Восстанавливает задачи, которые были созданы в БД, но не попали в очередь Redis.
    
    Это может произойти, если Redis упал во время создания задачи.
    Задачи в статусе "pending" без попыток (attempts=0) и старше 5 минут считаются потерянными.
    
    Returns:
        int: Количество восстановленных задач
    """
    from datetime import datetime, timezone, timedelta
    
    # Задачи в pending без попыток, созданные более 5 минут назад
    threshold = datetime.now(timezone.utc) - timedelta(minutes=5)
    
    lost_tasks = db.query(Task).filter(
        Task.status == "pending",
        Task.attempts == 0,
        Task.created_at < threshold
    ).all()
    
    if not lost_tasks:
        return 0
    
    recovered = 0
    for task in lost_tasks:
        try:
            # Пытаемся добавить в очередь
            enqueue_task_once(task.id, retries=1, priority_level=_task_queue_priority_level(task))
            recovered += 1
            logger.info(f"Recovered task {task.id} that was lost in Redis")
        except Exception as e:
            logger.warning(f"Failed to recover task {task.id}: {e}")
    
    return recovered


def recover_stuck_tasks(db: Session, timeout_seconds: int = TASK_TIMEOUT_SECONDS) -> int:
    """
    ИСПРАВЛЕНО: Восстанавливает зависшие задачи (в статусе "in_progress" дольше timeout_seconds).
    
    Если задача зависла без прогресса (progress=0) - переводит в error с refund.
    Если есть прогресс - переводит в partial/error с refund за неуспешные видео.
    
    Args:
        db: SQLAlchemy сессия
        timeout_seconds: Максимальное время в секундах для задачи в статусе "in_progress"
        
    Returns:
        Количество восстановленных задач
    """
    from db.models import User
    from services.balance_service import get_pricing, calculate_refund_amount, refund_balance, balance_is_admin
    
    now = datetime.now(timezone.utc)
    timeout_threshold = now - timedelta(seconds=timeout_seconds)
    
    # Находим задачи, которые зависли в статусе "in_progress"
    stuck_tasks = db.query(Task).filter(
        Task.status == "in_progress",
        Task.updated_at < timeout_threshold
    ).all()
    
    recovered_count = 0
    for task in stuck_tasks:
        # Сохраняем старое значение updated_at для сообщения об ошибке
        old_updated_at = task.updated_at
        progress = task.progress or 0
        total_urls = len(task.video_urls or [])
        
        logger.warning(
            f"Recovering stuck task {task.id}: "
            f"in_progress since {old_updated_at}, "
            f"progress={progress}/{total_urls}, "
            f"timeout={timeout_seconds}s"
        )
        
        try:
            # Определяем финальный статус
            if progress == 0:
                # Нет прогресса - полный fail с refund
                task.status = "error"
                task.error_reason = (
                    f"Задача зависла в статусе 'in_progress' более {timeout_seconds // 60} минут "
                    f"(с {old_updated_at}). Не завершена обработка ни одной ссылки; отдельные действия могли быть выполнены. "
                    f"Возможные причины: нет активных аккаунтов, "
                    f"воркер остановлен, ошибки подключения."
                )
                
                # Возвращаем средства полностью (если были списаны)
                user = db.query(User).filter(User.id == task.user_id).first()
                if user and not balance_is_admin(user) and task.total_cost and task.total_cost > 0:
                    try:
                        already_refunded = Decimal(str(task.refunded_cost or 0))
                        remaining_refund = max(Decimal(str(task.total_cost)) - already_refunded, Decimal("0.00"))
                        if remaining_refund <= 0:
                            logger.info(f"Task {task.id}: refund already applied earlier, skipping duplicate full refund")
                        else:
                            refund_success = refund_balance(
                                db=db,
                                user_id=task.user_id,
                                amount=remaining_refund,
                                task_id=task.id,
                                description=f"Возврат за зависшую задачу #{task.id} (in_progress timeout, no progress)",
                                commit=False,
                            )
                            
                            if refund_success:
                                task.refunded_cost = already_refunded + remaining_refund
                                logger.info(
                                    f"Task {task.id}: Full refund {remaining_refund} грн for stuck timeout "
                                    f"(no progress, user {task.user_id})"
                                )
                            else:
                                logger.error(f"Task {task.id}: Failed to refund balance for stuck timeout")
                    except Exception as refund_error:
                        logger.error(
                            f"Task {task.id}: Error refunding balance for stuck timeout: {refund_error}",
                            exc_info=True
                        )
            else:
                # Есть прогресс - partial с refund за неуспешные
                from services.task_state_machine import safe_transition
                new_status = safe_transition(task.status, "partial")
                if new_status:
                    task.status = new_status
                else:
                    task.status = "error"
                
                task.error_reason = (
                    f"Задача зависла в статусе 'in_progress' более {timeout_seconds // 60} минут "
                    f"(с {old_updated_at}). Обработано {progress}/{total_urls} видео."
                )
                
                # Возвращаем средства за неуспешные видео
                user = db.query(User).filter(User.id == task.user_id).first()
                if user and not balance_is_admin(user) and task.total_cost and task.total_cost > 0:
                    failed_videos = total_urls - progress
                    if failed_videos > 0:
                        try:
                            pricing = get_pricing(db)
                            target_refund = calculate_refund_amount(failed_videos, pricing)
                            already_refunded = Decimal(str(task.refunded_cost or 0))
                            remaining_refund = max(Decimal(str(task.total_cost)) - already_refunded, Decimal("0.00"))
                            refund_amount = min(Decimal(str(target_refund)), remaining_refund)
                            if refund_amount <= 0:
                                logger.info(f"Task {task.id}: refund already applied earlier, skipping duplicate partial refund")
                            else:
                                refund_success = refund_balance(
                                    db=db,
                                    user_id=task.user_id,
                                    amount=refund_amount,
                                    task_id=task.id,
                                    description=f"Возврат за {failed_videos} неуспешных видео в зависшей задаче #{task.id}",
                                    commit=False,
                                )
                                
                                if refund_success:
                                    task.refunded_cost = already_refunded + refund_amount
                                    logger.info(
                                        f"Task {task.id}: Refunded {refund_amount} грн for {failed_videos} failed videos "
                                        f"(stuck timeout, user {task.user_id})"
                                    )
                                else:
                                    logger.error(f"Task {task.id}: Failed to refund balance for stuck timeout")
                        except Exception as refund_error:
                            logger.error(
                                f"Task {task.id}: Error refunding balance for stuck timeout: {refund_error}",
                                exc_info=True
                            )
            
            task.updated_at = now
            
            try:
                db.commit()
                recovered_count += 1
                logger.info(f"Task {task.id} recovered from stuck in_progress (status: {task.status})")
            except Exception as e:
                db.rollback()
                logger.error(f"Error committing recovery for task {task.id}: {e}", exc_info=True)
                continue  # Продолжаем обработку следующих задач
            
        except Exception as e:
            db.rollback()
            logger.error(f"Error recovering stuck task {task.id}: {e}", exc_info=True)
            # Продолжаем обработку следующих задач даже если одна не удалась
    
    if recovered_count > 0:
        logger.info(f"Recovered {recovered_count} stuck in_progress task(s)")
    
    return recovered_count


def recover_pending_enqueues(db: Session) -> int:
    """
    Отправляет в Redis задачи из резервной таблицы pending_enqueue.
    """
    pending = db.query(PendingEnqueue).all()
    recovered = 0
    for item in pending:
        try:
            if finalize_reserved_enqueue(db, item.task_id):
                recovered += 1
                logger.info(f"Recovered pending_enqueue task {item.task_id}")
        except Exception as e:
            db.rollback()
            logger.warning(f"Failed to enqueue pending task {item.task_id}: {e}")
    return recovered


async def recover_pending_enqueues_async(db) -> int:
    """
    Async вариант восстановления pending_enqueue для API/фоновых задач.
    """
    recovered = 0
    try:
        from sqlalchemy import select
        result = await db.execute(select(PendingEnqueue))
        rows = result.scalars().all()
    except Exception as e:
        logger.error(f"Failed to read pending_enqueue async: {e}")
        return 0

    for item in rows:
        try:
            if await finalize_reserved_enqueue_async(db, item.task_id):
                recovered += 1
                logger.info(f"Recovered pending_enqueue task {item.task_id} (async)")
        except Exception as e:
            await db.rollback()
            logger.warning(f"Failed to enqueue pending task {item.task_id} (async): {e}")
    return recovered


def recover_pending_timeout_tasks(db: Session, timeout_minutes: int = 20) -> int:
    """
    ИСПРАВЛЕНО: Восстанавливает зависшие pending задачи (автоматический watchdog).
    
    Задачи в статусе "pending" дольше timeout_minutes переводятся в "error" с возвратом средств.
    Это предотвращает ситуации, когда задачи зависают в pending (нет аккаунтов, воркер вырубился и т.п.).
    
    Args:
        db: SQLAlchemy сессия
        timeout_minutes: Максимальное время в минутах для задачи в статусе "pending" (по умолчанию 20 минут)
        
    Returns:
        Количество обработанных задач
    """
    from db.models import User
    from services.balance_service import refund_balance, balance_is_admin, has_full_functionality_access
    from services.app_settings_service import is_parsing_mode

    try:
        parsing_mode = is_parsing_mode(db)
    except Exception as exc:
        logger.warning(f"Failed to read software_mode before pending timeout recovery: {exc}")
        parsing_mode = False
    
    now = datetime.now(timezone.utc)
    timeout_threshold = now - timedelta(minutes=timeout_minutes)
    
    # Находим задачи, которые зависли в статусе "pending"
    stuck_pending_tasks = db.query(Task).filter(
        Task.status == "pending",
        Task.created_at < timeout_threshold,
        Task.progress == 0  # Только задачи, которые не начали обработку
    ).all()
    
    if parsing_mode:
        before_count = len(stuck_pending_tasks)
        stuck_pending_tasks = [
            task for task in stuck_pending_tasks
            if task.user and has_full_functionality_access(task.user)
        ]
        if before_count and not stuck_pending_tasks:
            logger.info("Skipping restricted pending timeout recovery while software_mode=parsing")

    if stuck_pending_tasks:
        queued_task_ids = _read_redis_queued_task_ids()
        if queued_task_ids is None:
            return 0

        candidate_ids = [task.id for task in stuck_pending_tasks]
        marker_task_ids = _read_queue_marker_task_ids(candidate_ids)
        if marker_task_ids is None:
            return 0

        reserved_task_ids = {
            task_id
            for (task_id,) in db.query(PendingEnqueue.task_id).filter(
                PendingEnqueue.task_id.in_(candidate_ids)
            ).all()
        }
        protected_task_ids = queued_task_ids | marker_task_ids | reserved_task_ids
        if protected_task_ids:
            before_count = len(stuck_pending_tasks)
            stuck_pending_tasks = [
                task for task in stuck_pending_tasks
                if task.id not in protected_task_ids
            ]
            skipped_count = before_count - len(stuck_pending_tasks)
            if skipped_count:
                logger.info(
                    "Skipping %s pending timeout task(s) still queued/reserved for executor",
                    skipped_count,
                )

    processed_count = 0
    for task in stuck_pending_tasks:
        logger.warning(
            f"Recovering pending timeout task {task.id}: "
            f"pending since {task.created_at}, "
            f"timeout={timeout_minutes} minutes"
        )
        
        try:
            # Переводим в error
            task.status = "error"
            task.error_reason = (
                f"Задача зависла в статусе 'pending' более {timeout_minutes} минут "
                f"(создана {task.created_at}). Возможные причины: нет активных аккаунтов, "
                f"воркер остановлен, ошибки подключения."
            )
            task.updated_at = now
            
            # Возвращаем средства, если были списаны
            user = db.query(User).filter(User.id == task.user_id).first()
            if user and not balance_is_admin(user) and task.total_cost and task.total_cost > 0:
                try:
                    # Возвращаем только остаток, чтобы избежать double-refund
                    already_refunded = Decimal(str(task.refunded_cost or 0))
                    remaining_refund = max(Decimal(str(task.total_cost)) - already_refunded, Decimal("0.00"))
                    if remaining_refund <= 0:
                        logger.info(f"Task {task.id}: refund already applied earlier, skipping duplicate pending-timeout refund")
                    else:
                        refund_success = refund_balance(
                            db=db,
                            user_id=task.user_id,
                            amount=remaining_refund,
                            task_id=task.id,
                            description=f"Возврат за зависшую задачу #{task.id} (pending timeout)",
                            commit=False,
                        )
                        
                        if refund_success:
                            task.refunded_cost = already_refunded + remaining_refund
                            logger.info(
                                f"Task {task.id}: Refunded {remaining_refund} грн for pending timeout "
                                f"(user {task.user_id})"
                            )
                        else:
                            logger.error(f"Task {task.id}: Failed to refund balance for pending timeout")
                except Exception as refund_error:
                    logger.error(
                        f"Task {task.id}: Error refunding balance for pending timeout: {refund_error}",
                        exc_info=True
                    )

            queued_video_rows = [
                (
                    int(video_id),
                    int(creator_id),
                    int(blogger_id) if blogger_id is not None else None,
                    creator_mode,
                    bool(creator_enabled),
                )
                for video_id, creator_id, blogger_id, creator_mode, creator_enabled in db.query(
                    CreatorVideo.id,
                    CreatorVideo.creator_id,
                    CreatorSource.monitored_blogger_id,
                    CreatorSource.mode,
                    CreatorSource.enabled,
                )
                .join(CreatorSource, CreatorVideo.creator_id == CreatorSource.id)
                .filter(
                    CreatorVideo.task_id == task.id,
                    CreatorVideo.status == "queued",
                )
                .all()
            ]

            reset_videos = db.query(CreatorVideo).filter(
                CreatorVideo.task_id == task.id,
                CreatorVideo.status == "queued",
            ).update(
                {
                    CreatorVideo.status: "new",
                    CreatorVideo.task_id: None,
                },
                synchronize_session=False,
            )
            if reset_videos:
                logger.info(
                    "Task %s: reset %s queued monitor video(s) after pending timeout",
                    task.id,
                    reset_videos,
                )
                retry_events = _enqueue_autopilot_retry_for_reset_videos(
                    db,
                    task_id=task.id,
                    video_rows=queued_video_rows,
                    available_at=now,
                )
                if retry_events:
                    logger.info(
                        "Task %s: enqueued %s autopilot retry outbox event(s) after pending timeout",
                        task.id,
                        retry_events,
                    )
            
            try:
                db.commit()
                processed_count += 1
                logger.info(f"Task {task.id} marked as error due to pending timeout")
            except Exception as e:
                db.rollback()
                logger.error(f"Error committing pending timeout recovery for task {task.id}: {e}", exc_info=True)
                continue
                
        except Exception as e:
            db.rollback()
            logger.error(f"Error recovering pending timeout task {task.id}: {e}", exc_info=True)
            # Продолжаем обработку следующих задач даже если одна не удалась
    
    if processed_count > 0:
        logger.info(f"Recovered {processed_count} pending timeout task(s)")
    
    return processed_count
