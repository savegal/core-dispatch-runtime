"""Cross-process stop signal for running executor tasks."""

from __future__ import annotations

import logging

from services.redis_service import get_redis_client

logger = logging.getLogger("services.task_stop_signal")

TASK_STOP_SIGNAL_TTL_SECONDS = 24 * 3600
TASK_STOP_CHANNEL = "task:stop"


def task_stop_key(task_id: int) -> str:
    return f"task:stop:{int(task_id)}"


def request_task_stop(task_id: int) -> bool:
    """Persist and publish a stop signal for an in-progress task."""
    try:
        redis_client = get_redis_client()
        task_id_int = int(task_id)
        redis_client.set(task_stop_key(task_id_int), "1", ex=TASK_STOP_SIGNAL_TTL_SECONDS)
        try:
            redis_client.publish(TASK_STOP_CHANNEL, str(task_id_int))
        except Exception as publish_error:
            logger.debug("Failed to publish stop signal for task %s: %s", task_id_int, publish_error)
        return True
    except Exception as exc:
        logger.warning("Failed to persist stop signal for task %s: %s", task_id, exc)
        return False


def is_task_stop_requested(task_id: int) -> bool:
    """Returns True when another process requested this task to stop."""
    try:
        redis_client = get_redis_client()
        return bool(redis_client.exists(task_stop_key(int(task_id))))
    except Exception as exc:
        logger.debug("Failed to read stop signal for task %s: %s", task_id, exc)
        return False


def clear_task_stop(task_id: int) -> None:
    try:
        get_redis_client().delete(task_stop_key(int(task_id)))
    except Exception as exc:
        logger.debug("Failed to clear stop signal for task %s: %s", task_id, exc)
