"""
Executor - обработчик задач комментирования TikTok.

Особенности:
- Retry с другим аккаунтом при ошибках
- Автоматическая замена медленных прокси
- Понятные сообщения об ошибках
- Защита аккаунтов от ложного пометки как dead
"""
# ВАЖНО: Настройка кодировки ПЕРЕД любыми импортами
import sys
import os
os.environ['PYTHONIOENCODING'] = 'utf-8'
if sys.platform == 'win32':
    try:
        import ctypes
        ctypes.windll.kernel32.SetConsoleOutputCP(65001)
        ctypes.windll.kernel32.SetConsoleCP(65001)
    except Exception:
        pass
    try:
        sys.stdout.reconfigure(encoding='utf-8', errors='replace')
        sys.stderr.reconfigure(encoding='utf-8', errors='replace')
    except Exception:
        pass

import logging
import threading
import time
import random
import asyncio
import copy
from types import SimpleNamespace
from typing import Any, Callable
from sqlalchemy.orm import Session
from sqlalchemy.orm.attributes import flag_modified
from db.database import get_sync_db
from db.models import Task, Proxy, User
from services.balance_service import (
    get_pricing,
    refund_balance,
    is_admin as balance_is_admin,
    has_full_functionality_access,
)
from decimal import Decimal
from services.task_service import dequeue_task
from services.task_service import get_text_stats, record_text_result, requeue_deferred_task, task_queue_priority_level
from services.app_settings_service import is_parsing_mode
from services.metrics import increment_counter, record_timing
from services.likes_config import BOOST_LIKES_DEFAULT, coerce_likes_quantity
from services.account_service import (
    get_free_account_for_main_comment,
    get_free_account_for_reply,
    increment_main_comment,
    increment_reply,
    mark_error,
    mark_soft_error,
    mark_dead,
    mark_success
)
from services.proxy_replacement_service import mark_proxy_as_slow_and_replace, reset_proxy_fail_count
from services.proxy_concurrency_service import increment_proxy_fail, should_block_proxy
from services.log_service import log
from executor.tiktok_client import post_comment, post_reply, quick_validate_account, verify_comment_visible, TikTokClientError
from executor.comment_result import CommentResult
from executor.error_handler import (
    classify_error as classify_error_new,
    ErrorCategory,
    is_non_critical_error,
    is_proxy_error,
    is_fatal_for_account,
    should_replace_proxy,
    get_retry_policy,
    should_apply_cooldown
)
# ИСПРАВЛЕНО: Убрана проверка прокси - все прокси считаются рабочими
# from services.proxy_service import quick_check_proxy
# from config.settings import get_settings as get_settings_for_proxy_check
from services.mobile_proxy_service import (
    acquire_proxy_lock,
    release_proxy_lock,
    ensure_proxy_ip_rotated,
    get_proxy_last_ip,
    save_proxy_last_ip,
    ProxyRotationError,
    ProxyLockError
)
from executor.utils import random_delay_sync as random_delay
from executor.text_utils import process_spintax # NEW: Spintax support
from config.settings import get_settings
from config.logging_config import setup_logging
from config.sentry_config import init_sentry
from datetime import datetime, timezone
from services.redis_service import update_executor_heartbeat
from executor.policy import ExecutionPolicy
from decimal import Decimal
from services.metrics import set_gauge

# Настройка логирования (используем единую конфигурацию)
# ИСПРАВЛЕНО: Используем JSON logging если включено в настройках
from config.settings import get_settings
settings_for_logging = get_settings()
_executor_log_file = str(
    getattr(settings_for_logging, "EXECUTOR_LOG_FILE", os.path.join("logs", "executor.log"))
    or os.path.join("logs", "executor.log")
)
_executor_log_max_mb = max(1, int(getattr(settings_for_logging, "LOG_FILE_MAX_MB", 50) or 50))
_executor_log_backup_count = max(
    1,
    int(getattr(settings_for_logging, "LOG_FILE_BACKUP_COUNT", 20) or 20),
)
setup_logging(
    level="INFO",
    log_file=_executor_log_file,
    use_json=settings_for_logging.USE_JSON_LOGGING,
    log_max_bytes=_executor_log_max_mb * 1024 * 1024,
    log_backup_count=_executor_log_backup_count,
)
logger = logging.getLogger("executor")
logger.info(
    "Executor file logging enabled: path=%s max_mb=%s backups=%s",
    _executor_log_file,
    _executor_log_max_mb,
    _executor_log_backup_count,
)

# Инициализируем Sentry для мониторинга ошибок
init_sentry()

settings = get_settings()

# ИСПРАВЛЕНО: Используем значение из settings вместо hardcoded
MAX_RETRY_ACCOUNTS = settings.MAX_RETRY_ACCOUNTS
_last_parsing_pause_log_at = 0.0

# Графtop shutdown:
# Раньше брали `from main import shutdown_event` — это тянуло за собой ВЕСЬ
# FastAPI-граф (роутеры, миграции, lifespan-инициализация) при запуске
# `python -m executor.executor`, что замедляло старт и мешало корректному
# завершению. Теперь executor — самодостаточный модуль, signal handlers
# регистрируются в run_executor() ниже.
_shutdown_event: threading.Event = threading.Event()
_full_url_locks_guard = threading.Lock()
_full_url_locks: dict[str, threading.Lock] = {}
_GLOBAL_TASK_EXECUTION_LOCK_KEY = "executor:single_active_task"
_local_task_execution_lock = threading.Lock()
_executor_activity_lock = threading.Lock()
_executor_activity_payload: dict[str, Any] = {"status": "ok"}
_URL_SUCCESS_STATUSES = {"success", "submitted_unconfirmed"}
_URL_UNCONFIRMED_STATUSES = {"submitted_unconfirmed"}
_REPLY_VISIBILITY_EXHAUSTED_ERRORS = {
    "remaining_main_comments_not_visible_after_four_accounts",
    "remaining_main_comments_not_visible_after_ip_rotation",
    "reply_visibility_rotation_unavailable",
}


def _reply_visibility_exhausted(result: dict | None) -> bool:
    if not isinstance(result, dict):
        return False
    return str(result.get("error") or "") in _REPLY_VISIBILITY_EXHAUSTED_ERRORS
_GLOBAL_TASK_LOCK_WAIT_LOG_INTERVAL = 30.0
_last_global_task_lock_wait_log_at = 0.0


def _mark_task_settings_dirty(task: Task) -> None:
    """Tell SQLAlchemy that the JSON settings payload changed in-place."""
    try:
        flag_modified(task, "settings")
    except Exception as exc:
        logger.debug("Task %s: failed to flag settings as modified: %s", getattr(task, "id", None), exc)


def _refresh_execution_ledger(task_settings: dict[str, Any] | None) -> dict[str, Any]:
    """Attach per-URL ledgers and a task-level aggregate to task settings."""
    settings_dict = task_settings if isinstance(task_settings, dict) else {}
    url_results = settings_dict.get("url_results")
    if not isinstance(url_results, dict):
        return settings_dict
    try:
        from services.task_execution_ledger import (
            attach_url_execution_ledgers,
            build_task_execution_ledger,
        )

        attach_url_execution_ledgers(url_results)
        settings_dict["execution_ledger"] = build_task_execution_ledger(url_results)
    except Exception:
        logger.debug("Failed to refresh task execution ledger", exc_info=True)
    return settings_dict


def _set_executor_activity(status: str = "ok", **extra: Any) -> None:
    with _executor_activity_lock:
        payload = {"status": status}
        payload.update(extra)
        _executor_activity_payload.clear()
        _executor_activity_payload.update(payload)


def _get_executor_activity_payload() -> dict[str, Any]:
    with _executor_activity_lock:
        return dict(_executor_activity_payload)


def _mark_executor_idle() -> None:
    _set_executor_activity("ok")
    update_executor_heartbeat("ok")


def _mark_executor_busy(task_id: int, worker_id: int) -> None:
    busy_since = int(time.time())
    payload = {
        "active_task_id": int(task_id),
        "worker_id": int(worker_id),
        "busy_since": busy_since,
    }
    _set_executor_activity("busy", **payload)
    update_executor_heartbeat("busy", **payload)


def _get_full_url_lock(video_url: str) -> tuple[str, threading.Lock]:
    key = (video_url or "").strip().lower()
    with _full_url_locks_guard:
        lock = _full_url_locks.get(key)
        if lock is None:
            lock = threading.Lock()
            _full_url_locks[key] = lock
        return key, lock


def _global_task_lock_timeout_seconds() -> int:
    processing_timeout = int(getattr(settings, "TASK_PROCESSING_TIMEOUT", 1800) or 1800)
    recovery_interval = int(getattr(settings, "STUCK_TASKS_RECOVERY_INTERVAL", 300) or 300)
    return max(60, processing_timeout + recovery_interval)


def _global_task_lock_redis_key() -> str:
    return f"lock:{_GLOBAL_TASK_EXECUTION_LOCK_KEY}"


def _heartbeat_points_to_live_local_process(heartbeat_info: dict | None) -> bool:
    """Return True only when heartbeat belongs to a currently alive executor process."""
    if not isinstance(heartbeat_info, dict):
        return False
    if str(heartbeat_info.get("status") or "").lower() == "stopped":
        return False
    host = str(heartbeat_info.get("host") or "").strip()
    if host:
        try:
            import socket

            if host.lower() != socket.gethostname().lower():
                return True
        except Exception:
            return True
    try:
        pid = int(heartbeat_info.get("pid") or 0)
    except (TypeError, ValueError):
        pid = 0
    if pid <= 0:
        return False
    if pid == os.getpid():
        return True
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False
    except Exception:
        return True


def _clear_stale_global_task_lock_on_startup(
    *,
    redis_client: Any | None = None,
    heartbeat_reader: Callable[[], tuple[bool, dict]] | None = None,
) -> bool:
    """Clear a leftover serial-executor lock only when no executor heartbeat is alive."""
    try:
        if heartbeat_reader is None:
            from services.redis_service import get_executor_heartbeat as _get_executor_heartbeat

            heartbeat_reader = _get_executor_heartbeat
        heartbeat_alive, heartbeat_info = heartbeat_reader()
        if heartbeat_alive:
            if not _heartbeat_points_to_live_local_process(heartbeat_info):
                logger.warning(
                    "Executor startup: heartbeat is stale, will clear task lock if present: %s",
                    heartbeat_info,
                )
            else:
                logger.info(
                    "Executor startup: keeping existing task lock because executor heartbeat is alive: %s",
                    heartbeat_info,
                )
                return False

        if redis_client is None:
            from services.redis_service import get_redis_client

            redis_client = get_redis_client()

        lock_key = _global_task_lock_redis_key()
        token = redis_client.get(lock_key)
        if not token:
            return False
        ttl = redis_client.ttl(lock_key)
        deleted = bool(redis_client.delete(lock_key))
        if deleted:
            logger.info(
                "Executor startup: cleared stale task execution lock %s "
                "(heartbeat missing/stale, ttl=%s, reason=%s)",
                lock_key,
                ttl,
                heartbeat_info.get("reason") if isinstance(heartbeat_info, dict) else heartbeat_info,
            )
        return deleted
    except Exception as exc:
        logger.warning(
            "Executor startup: failed to clear stale task execution lock: %s",
            str(exc)[:120],
        )
        return False


def _run_executor_startup_queue_cleanup() -> None:
    """Clean stale queue state before workers begin polling Redis."""
    db = None
    try:
        db = next(get_sync_db())
        from services.redis_service import get_executor_heartbeat
        from services.task_service import (
            prune_stale_task_queue_entries,
            recover_pending_enqueues,
            recover_stuck_tasks,
            recover_tasks_lost_in_redis,
        )

        heartbeat_alive, heartbeat_info = get_executor_heartbeat()
        can_recover_stuck = not (
            heartbeat_alive and _heartbeat_points_to_live_local_process(heartbeat_info)
        )
        if can_recover_stuck:
            recovered = recover_stuck_tasks(db)
            if recovered:
                logger.info("Executor startup: recovered %s stale in_progress task(s)", recovered)
        else:
            logger.info(
                "Executor startup: skipped in_progress recovery because a live executor heartbeat exists: %s",
                heartbeat_info,
            )

        pruned = prune_stale_task_queue_entries(db)
        if pruned:
            logger.info("Executor startup: pruned %s stale Redis queue entr%s", pruned, "y" if pruned == 1 else "ies")
        recovered_pending = recover_pending_enqueues(db)
        if recovered_pending:
            logger.info("Executor startup: recovered %s reserved pending enqueue(s)", recovered_pending)
        recovered_lost = recover_tasks_lost_in_redis(db)
        if recovered_lost:
            logger.info("Executor startup: requeued %s pending task(s) missing from Redis", recovered_lost)
    except Exception as exc:
        logger.warning("Executor startup queue cleanup failed: %s", str(exc)[:160], exc_info=True)
    finally:
        if db is not None:
            try:
                db.close()
            except Exception:
                pass


class _LocalTaskExecutionLock:
    def __init__(self, lock: threading.Lock):
        self._lock = lock
        self._released = False

    def release(self) -> None:
        if self._released:
            return
        self._released = True
        self._lock.release()


def _acquire_local_task_execution_lock(worker_id: int):
    if not _local_task_execution_lock.acquire(blocking=False):
        logger.debug("Worker %s waiting: local task execution lock is held", worker_id)
        return None
    logger.warning(
        "Worker %s using local task execution lock fallback; cross-process serialization is unavailable",
        worker_id,
    )
    return _LocalTaskExecutionLock(_local_task_execution_lock)


def _redis_available_for_task_lock() -> bool:
    try:
        from services.redis_service import get_redis_client

        get_redis_client().ping()
        return True
    except Exception:
        return False


def _acquire_global_task_execution_lock(worker_id: int):
    """Best-effort task serialization lock shared across executor processes."""
    global _last_global_task_lock_wait_log_at
    try:
        from services.redis_service import RedisLock

        lock = RedisLock(
            _GLOBAL_TASK_EXECUTION_LOCK_KEY,
            timeout=_global_task_lock_timeout_seconds(),
            blocking=False,
        )
        if lock.acquire():
            logger.debug("Worker %s acquired global task execution lock", worker_id)
            return lock
        try:
            from services.redis_service import get_executor_heartbeat, get_redis_client

            heartbeat_alive, heartbeat_info = get_executor_heartbeat()
            if not heartbeat_alive or not _heartbeat_points_to_live_local_process(heartbeat_info):
                cleared = _clear_stale_global_task_lock_on_startup(
                    redis_client=get_redis_client(),
                    heartbeat_reader=lambda: (heartbeat_alive, heartbeat_info),
                )
                if cleared and lock.acquire():
                    logger.info(
                        "Worker %s acquired global task execution lock after clearing stale lock",
                        worker_id,
                    )
                    return lock
        except Exception as stale_lock_exc:
            logger.debug(
                "Worker %s stale task lock check failed: %s",
                worker_id,
                str(stale_lock_exc)[:120],
            )
        if not _redis_available_for_task_lock():
            return _acquire_local_task_execution_lock(worker_id)
        now_ts = time.time()
        if now_ts - _last_global_task_lock_wait_log_at >= _GLOBAL_TASK_LOCK_WAIT_LOG_INTERVAL:
            _last_global_task_lock_wait_log_at = now_ts
            try:
                from services.redis_service import get_redis_client

                ttl = get_redis_client().ttl(_global_task_lock_redis_key())
            except Exception:
                ttl = "unknown"
            logger.info(
                "Worker %s waiting: another executor holds global task execution lock (ttl=%s)",
                worker_id,
                ttl,
            )
        return None
    except Exception as exc:
        logger.warning(
            "Worker %s could not acquire global task execution lock: %s",
            worker_id,
            str(exc)[:120],
        )
        return _acquire_local_task_execution_lock(worker_id)


def _release_global_task_execution_lock(lock, worker_id: int) -> None:
    if lock is None:
        return
    try:
        lock.release()
        logger.debug("Worker %s released global task execution lock", worker_id)
    except Exception as exc:
        logger.warning(
            "Worker %s could not release global task execution lock: %s",
            worker_id,
            str(exc)[:120],
        )


def _format_likes_status(likes_info: dict | None) -> str:
    info = likes_info or {}
    if info.get("enabled") is False or info.get("requested") is False:
        return "Likes: disabled"
    if info.get("attempted") is False:
        return "Likes: skipped"
    if info.get("error") in {"likes_not_started", "likes_target_missing", "tiktok_username_unknown"}:
        return "Likes: skipped"
    if info.get("ok"):
        return f"Likes: {info.get('quantity', BOOST_LIKES_DEFAULT)}"
    if info.get("provider_available") is False:
        return "Likes: skipped"
    return "Likes: failed"


def _coerce_positive_int(value, default: int, *, minimum: int = 1, maximum: int = 3) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        parsed = default
    return max(minimum, min(maximum, parsed))


def _unique_text_variants(text_variants: list[str]) -> list[str]:
    unique: list[str] = []
    seen: set[str] = set()
    for item in text_variants or []:
        text = str(item or "").strip()
        if not text:
            continue
        key = " ".join(text.lower().split())
        if key in seen:
            continue
        seen.add(key)
        unique.append(text)
    return unique


def _base_variant_index(
    *,
    task_settings: dict,
    task_id: int,
    url_index: int,
    video_count: int,
    variant_count: int,
) -> int:
    if variant_count <= 0:
        return 0
    if video_count == 1 and task_settings.get("origin") == "autopilot":
        try:
            value = (
                task_settings.get("text_variant_offset")
                if task_settings.get("text_variant_offset") is not None
                else task_settings.get("creator_video_id")
                if task_settings.get("creator_video_id") is not None
                else task_id
            )
            return int(value) % variant_count
        except (TypeError, ValueError, ZeroDivisionError):
            return 0
    return url_index % variant_count


def _main_variants_for_url(
    *,
    text_variants: list[str],
    task_settings: dict,
    task_id: int,
    url_index: int,
    video_count: int,
) -> list[str]:
    unique = _unique_text_variants(text_variants)
    if not unique:
        return []

    base_index = _base_variant_index(
        task_settings=task_settings,
        task_id=task_id,
        url_index=url_index,
        video_count=video_count,
        variant_count=len(unique),
    )
    rotated = unique[base_index:] + unique[:base_index]

    publish_all = task_settings.get("publish_all_text_variants", True)
    if str(publish_all).strip().lower() in {"0", "false", "no", "off"}:
        return rotated[:1]

    default_count = min(3, len(rotated))
    count = _coerce_positive_int(
        task_settings.get("main_variants_per_url"),
        default_count,
        minimum=1,
        maximum=min(3, len(rotated)),
    )
    return rotated[:count]


def _task_for_variant(task: Task, *, allow_likes: bool) -> SimpleNamespace:
    variant_settings = dict(task.settings or {})
    if not allow_likes:
        variant_settings["boost_likes_enabled"] = False
    return SimpleNamespace(
        reply1=task.reply1,
        reply2=task.reply2,
        reply3=task.reply3,
        settings=variant_settings,
    )


def _merge_full_variant_results(variant_results: list[dict]) -> dict:
    if not variant_results:
        return {
            "status": "fail",
            "core_status": "fail",
            "comment_id": None,
            "error": "no_main_variants_attempted",
            "warnings": [],
            "has_warnings": False,
            "engagement": {"main_variants": []},
        }

    accepted_statuses = {"success", "submitted_unconfirmed"}
    accepted = [item for item in variant_results if item.get("status") in accepted_statuses]
    fatal_reply_visibility_result = next(
        (item for item in variant_results if _reply_visibility_exhausted(item)),
        None,
    )
    primary = fatal_reply_visibility_result or (accepted[0] if accepted else variant_results[0])
    merged = copy.deepcopy(primary)

    summaries = []
    for idx, item in enumerate(variant_results, start=1):
        engagement = item.get("engagement") if isinstance(item.get("engagement"), dict) else {}
        main = engagement.get("main") if isinstance(engagement.get("main"), dict) else {}
        summaries.append(
            {
                "index": idx,
                "text": item.get("text_choice") or main.get("text_choice"),
                "status": item.get("status"),
                "comment_id": item.get("comment_id") or main.get("comment_id"),
                "error": item.get("error") or main.get("error"),
                "verified": bool(main.get("verified")),
                "submitted_unconfirmed": bool(main.get("submitted_unconfirmed")),
                "warnings": item.get("warnings") or [],
                "engagement": engagement,
            }
        )

    total = len(variant_results)
    accepted_count = len(accepted)
    failed_count = total - accepted_count
    all_success = accepted_count == total and all(item.get("status") == "success" for item in accepted)

    if fatal_reply_visibility_result is not None:
        merged["status"] = "fail"
        merged["core_status"] = "fail"
        merged["error"] = fatal_reply_visibility_result.get("error")
    elif accepted_count:
        merged["status"] = "success" if all_success else "submitted_unconfirmed"
        merged["core_status"] = merged["status"]
        if failed_count:
            merged["error"] = "main_variant_partial"
            warnings = list(merged.get("warnings") or [])
            warnings.append(
                {
                    "code": "main_variant_partial",
                    "message": f"{accepted_count}/{total} main comment variants were submitted",
                }
            )
            merged["warnings"] = warnings
            merged["has_warnings"] = True
    else:
        merged["status"] = "fail"
        merged["core_status"] = "fail"
        merged["error"] = merged.get("error") or "all_main_variants_failed"

    merged["main_variants"] = summaries
    merged["main_variants_summary"] = {
        "total": total,
        "submitted": accepted_count,
        "failed": failed_count,
    }
    engagement = merged.setdefault("engagement", {})
    if isinstance(engagement, dict):
        engagement["main_variants"] = summaries
        engagement["main_variants_summary"] = merged["main_variants_summary"]
    return merged


def get_shutdown_event() -> threading.Event:
    """Внешний доступ к флагу остановки (используется тестами и worker-ами)."""
    return _shutdown_event

# Thread-local storage для переиспользования event loop
def _should_pause_executor_for_parsing(db: Session) -> bool:
    try:
        return bool(is_parsing_mode(db))
    except Exception as exc:
        logger.warning(f"Failed to read software_mode for executor pause check: {exc}")
        return False


def _should_defer_task_for_parsing(db: Session, task_id: int) -> bool:
    if not _should_pause_executor_for_parsing(db):
        return False
    task = db.query(Task).filter(Task.id == task_id).first()
    if not task or task.status != "pending":
        return False
    return not has_full_functionality_access(task.user)


def _should_defer_loaded_task_for_parsing(db: Session, task: Task | None) -> bool:
    if not _should_pause_executor_for_parsing(db):
        return False
    if not task or task.status != "pending":
        return False
    return not has_full_functionality_access(task.user)


def _sleep_until_next_worker_poll(shutdown_event: threading.Event) -> None:
    for _ in range(settings.WORKER_POLL_COUNT):
        if shutdown_event.is_set():
            break
        time.sleep(settings.WORKER_POLL_INTERVAL)


_thread_local = threading.local()

def get_event_loop():
    """Получает или создает event loop для текущего потока."""
    if not hasattr(_thread_local, 'loop') or _thread_local.loop.is_closed():
        # ИСПРАВЛЕНО: Для Windows используем ProactorEventLoop, который поддерживает subprocess
        # Политика уже установлена в run_executor, но проверяем на всякий случай
        if sys.platform == 'win32':
            try:
                # Проверяем, что политика установлена (должна быть установлена в run_executor)
                policy = asyncio.get_event_loop_policy()
                if not isinstance(policy, asyncio.WindowsProactorEventLoopPolicy):
                    asyncio.set_event_loop_policy(asyncio.WindowsProactorEventLoopPolicy())
            except Exception:
                # Если что-то пошло не так, пробуем установить политику
                try:
                    asyncio.set_event_loop_policy(asyncio.WindowsProactorEventLoopPolicy())
                except Exception:
                    pass
        _thread_local.loop = asyncio.new_event_loop()
        asyncio.set_event_loop(_thread_local.loop)
    return _thread_local.loop


def is_validation_cached(account) -> bool:
    """
    Проверяет, есть ли кэшированный результат валидации.
    Кэш действителен 2 минуты (короткий кэш для надёжности).
    
    ВАЖНО: Эта функция НЕ используется в get_validated_account() перед задачами,
    т.к. HTTP валидация может показать что куки валидны, но в браузере они могут не работать.
    Всегда проверяем аккаунт непосредственно перед использованием.
    (Кэш используется только для массовой валидации через API бота)
    
    Returns:
        bool: True если есть валидный кэш (можно не проверять)
    """
    if not account.last_validation_ok:
        return False
    
    # ИСПРАВЛЕНО: Используем значение из settings вместо hardcoded
    cache_ttl_minutes = settings.ACCOUNT_VALIDATION_CACHE_TTL_MINUTES
    now = datetime.now(timezone.utc)
    
    if account.last_validation_ok.tzinfo is None:
        # Если нет timezone, считаем что это UTC
        from datetime import timezone as tz
        last_ok = account.last_validation_ok.replace(tzinfo=tz.utc)
    else:
        last_ok = account.last_validation_ok
    
    age_minutes = (now - last_ok).total_seconds() / 60
    return age_minutes < cache_ttl_minutes


def prepare_mobile_proxy_for_account_sync(account, db: Session, proxy_obj: Proxy | None = None) -> tuple[bool, Proxy | None]:
    """
    Синхронная обёртка для prepare_mobile_proxy_for_account (для использования в threads).
    
    Args:
        account: Объект Account из БД
        db: SQLAlchemy сессия
        proxy_obj: Объект Proxy (опционально, для оптимизации)
    
    Returns:
        tuple[bool, Proxy | None]: (lock_acquired, proxy_obj)
    """
    try:
        loop = get_event_loop()
        return loop.run_until_complete(prepare_mobile_proxy_for_account(account, db, proxy_obj))
    except Exception as e:
        logger.warning(f"Error in prepare_mobile_proxy_for_account_sync: {e}")
        raise


async def prepare_mobile_proxy_for_account(account, db: Session, proxy_obj: Proxy | None = None) -> tuple[bool, Proxy | None]:
    """
    Подготавливает мобильный прокси для использования: захватывает блокировку и ротирует IP.
    
    Args:
        account: Объект Account из БД
        db: SQLAlchemy сессия
        proxy_obj: Объект Proxy (опционально, для оптимизации - избежать повторного запроса)
    
    Returns:
        tuple[bool, Proxy | None]: (lock_acquired, proxy_obj)
        - lock_acquired: True если блокировка захвачена и нужно освободить
        - proxy_obj: Объект Proxy (для освобождения блокировки)
    
    Raises:
        ProxyRotationError: если ротация не удалась
        ProxyLockError: если не удалось получить блокировку
    """
    if not account.proxy:
        return False, None  # Нет прокси - ничего не делаем
    
    # Получаем объект Proxy из БД (если не передан)
    if not proxy_obj:
        proxy_obj = db.query(Proxy).filter(Proxy.proxy == account.proxy).first()
        if not proxy_obj:
            logger.debug(f"Proxy object not found for account {account.id}, skipping rotation")
            return False, None
    
    # Проверяем, является ли прокси мобильным
    if not proxy_obj.is_mobile or proxy_obj.is_mobile != "Y" or not proxy_obj.rotation_url:
        logger.debug(f"Proxy {proxy_obj.id} is not mobile, skipping rotation")
        return False, None
    
    logger.info(f"Preparing mobile proxy {proxy_obj.id} for account {account.id}")
    
    # 1. Захватываем блокировку
    lock_acquired = await acquire_proxy_lock(proxy_obj.id, timeout=30)
    if not lock_acquired:
        raise ProxyLockError(f"Failed to acquire lock for proxy {proxy_obj.id} (timeout)")
    
    try:
        # 2. Получаем предыдущий IP
        previous_ip = await get_proxy_last_ip(proxy_obj.id)
        
        # 3. Ротируем IP (с проверкой)
        new_ip = await ensure_proxy_ip_rotated(proxy_obj, db, previous_ip)
        
        # 4. Сохраняем новый IP
        await save_proxy_last_ip(proxy_obj.id, new_ip)
        
        logger.info(f"Mobile proxy {proxy_obj.id} prepared: IP {new_ip}")
        
        # Возвращаем True - блокировка захвачена и нужно освободить
        return True, proxy_obj
        
    except Exception as e:
        # При ошибке освобождаем блокировку и пробрасываем исключение
        try:
            await release_proxy_lock(proxy_obj.id)
        except:
            pass
        raise


def get_validated_account(
    db: Session,
    get_account_func,
    exclude_ids: list[int] | None = None,
    task_id: int | None = None
):
    """
    Получает аккаунт и проверяет его валидность (если включено в настройках).
    Использует кэш (10 минут) и защиту от ложных dead (3 неудачи подряд).
    Также проверяет прокси аккаунта, если USE_PROXY=True.
    
    Args:
        db: сессия БД
        get_account_func: функция для получения аккаунта (get_free_account_for_main_comment/reply)
        exclude_ids: список ID для исключения
        task_id: ID задачи для логирования
        
    Returns:
        Account | None: валидированный аккаунт или None
    """
    if not settings.VALIDATE_BEFORE_TASK:
        # Валидация выключена - просто возвращаем аккаунт
        logger.debug("VALIDATE_BEFORE_TASK is False - skipping validation")
        return get_account_func(db, exclude_ids=exclude_ids)
    
    tried_accounts = list(exclude_ids) if exclude_ids else []
    max_validation_attempts = settings.VALIDATION_RETRY_COUNT + 1
    
    logger.debug(f"Starting account validation (max attempts: {max_validation_attempts})")
    
    for attempt in range(max_validation_attempts):
        account = get_account_func(db, exclude_ids=tried_accounts)
        
        if not account:
            # Нет доступных аккаунтов
            logger.debug(f"No account available (attempt {attempt + 1}/{max_validation_attempts})")
            return None
        
        tried_accounts.append(account.id)
        logger.info(f"Validating account {account.id} ({account.label}) - attempt {attempt + 1}/{max_validation_attempts}")
        
        # ВАЖНО: НЕ используем кэш валидации перед задачами!
        # HTTP валидация может показать что куки валидны, но в браузере они могут не работать.
        # Всегда проверяем аккаунт непосредственно перед использованием.
        # (Кэш используется только для массовой валидации через API бота)
        
        # ИСПРАВЛЕНО: Проверяем наличие прокси (обязательно если USE_PROXY=True)
        if settings.USE_PROXY and not account.proxy:
            logger.warning(f"Account {account.id} has no proxy, but USE_PROXY=True - skipping")
            if task_id:
                log(db, task_id, account.id, "Аккаунт без прокси пропущен (USE_PROXY=True)")
            continue
        
        # Блокируем прокси с превышением fail-лимита (fail-open если Redis недоступен)
        if settings.USE_PROXY and account.proxy and should_block_proxy(account.proxy):
            logger.warning(f"Proxy for account {account.id} is temporarily blocked due to failures - skipping account")
            if task_id:
                log(db, task_id, account.id, "Прокси заблокирован по fail-лимиту, аккаунт пропущен")
            continue
        
        # ИСПРАВЛЕНО: Убрана проверка прокси - все прокси считаются рабочими
        # Пользователь управляет прокси через настройки (какие использовать, какие нет)
        
        # 1. Браузерная проверка куков аккаунта через Playwright (точная, ~10-15 сек)
        # HTTP валидация неточная - TikTok различает API и браузерные сессии
        try:
            # ИСПРАВЛЕНО: Переиспользуем event loop из thread-local storage
            loop = get_event_loop()
            is_valid = loop.run_until_complete(quick_validate_account(account))
            now = datetime.now(timezone.utc)
            
            if is_valid:
                # ИСПРАВЛЕНО: Используем единую функцию для обработки успешной валидации
                from services.account_validation_policy import handle_validation_success
                handle_validation_success(db, account)
                db.commit()
                logger.info(f"Account {account.id} passed browser validation (Playwright), cache updated")
                return account
            else:
                # ИСПРАВЛЕНО: Fail-fast — помечаем аккаунт dead сразу, чтобы не тратить попытки
                from services.account_validation_policy import handle_validation_failure
                handle_validation_failure(db, account, task_id=task_id, log_func=log)
                mark_dead(db, account.id, reason="Validation failed before task (login required or cookies dead)")
                db.commit()
                # Пробуем следующий аккаунт
                continue
                
        except Exception as e:
            # Ошибка валидации - НЕ используем аккаунт (безопасный режим)
            logger.warning(f"Validation error for account {account.id}: {e} - skipping")
            if task_id:
                log(db, task_id, account.id, f"Ошибка валидации: {str(e)[:50]}")
            continue
    
    # Не нашли валидный аккаунт
    logger.warning(f"Could not find valid account after {max_validation_attempts} validation attempts")
    return None


# ИСПРАВЛЕНО: Удалена устаревшая функция classify_error()
# Используйте executor.error_handler.classify_error напрямую


def handle_account_error(
    db: Session, 
    account, 
    error_text: str, 
    task_id: int
) -> str:
    """
    Обрабатывает ошибку аккаунта - определяет тип и принимает решение.
    
    Логика:
    - dead_account: только при явных ошибках авторизации → mark_dead
    - proxy ошибки: заменяем прокси, НЕ меняем статус аккаунта сразу
    - page_load: мягкая ошибка → mark_soft_error
    - blocked: мягкая ошибка → mark_soft_error
    - остальное: мягкая ошибка → mark_soft_error
    
    Returns:
        str: Тип ошибки для логирования
    """
    # Прокси реально используется только если USE_PROXY=True И прокси есть
    using_proxy = settings.USE_PROXY and bool(account.proxy)
    error_classification = classify_error_new(error_text, using_proxy)
    error_type = error_classification.category.value
    description = error_classification.description
    
    if is_fatal_for_account(error_classification.category):
        # Аккаунт точно мёртв (cookies истекли, заблокирован, или требуется логин)
        mark_dead(db, account.id, reason=description)
        log(db, task_id, account.id, f"Аккаунт помечен как dead: {description}")
        logger.warning(f"Account {account.id} marked as dead: {description} (error_type: {error_type})")
    
    elif is_proxy_error(error_classification.category) or should_replace_proxy(error_classification.category):
        # Проблема с прокси - заменяем, но НЕ помечаем аккаунт как error
        if account.proxy:
            try:
                increment_proxy_fail(account.proxy)
                is_timeout = error_classification.category in [ErrorCategory.PROXY_TIMEOUT]
                mark_proxy_as_slow_and_replace(
                    db, 
                    account.id, 
                    account.proxy, 
                    timeout_error=is_timeout
                )
                db.refresh(account)
                logger.info(f"Replaced proxy for account {account.id} due to: {description}")
            except Exception as e:
                logger.error(f"Failed to replace proxy for account {account.id}: {e}")
        # Мягкая ошибка - не сразу меняем статус
        mark_soft_error(db, account.id, description)
        log(db, task_id, account.id, f"Ошибка прокси: {description}")
    
    elif error_classification.category == ErrorCategory.PAGE_LOAD:
        # Страница не загрузилась - пробуем заменить прокси, мягкая ошибка
        if account.proxy:
            try:
                mark_proxy_as_slow_and_replace(db, account.id, account.proxy, timeout_error=True)
                db.refresh(account)
            except Exception:
                pass
        mark_soft_error(db, account.id, description)
        log(db, task_id, account.id, f"Страница не загрузилась: {description}")
    
    elif error_classification.category == ErrorCategory.BLOCKED:
        # ИСПРАВЛЕНО: Блокировка TikTok - помечаем как error, но не dead
        mark_soft_error(db, account.id, description)
        log(db, task_id, account.id, f"Блокировка TikTok: {description}")
        logger.warning(f"Account {account.id} blocked by TikTok - may need manual resolution")
    
    elif is_non_critical_error(error_classification.category):
        # ПЕРЕРАБОТАНО: Не критичные ошибки - НЕ увеличиваем error_count, только логируем
        log(db, task_id, account.id, f"Не критичная ошибка: {description}")
        logger.debug(f"Account {account.id}: {error_type} - not counting as error (may be temporary)")
    
    elif error_classification.category == ErrorCategory.CAPTCHA:
        # ИСПРАВЛЕНО: CAPTCHA - применяем cooldown, не агрессивный retry (рекомендации ChatGPT)
        # Не помечаем как dead, но и не ретраим - нужен cooldown на N минут
        mark_soft_error(db, account.id, description)
        log(db, task_id, account.id, f"CAPTCHA обнаружена: {description} - требуется cooldown")
        logger.warning(f"Account {account.id}: CAPTCHA detected - applying cooldown policy (not retrying immediately)")
    
    elif error_classification.category == ErrorCategory.NETWORK_ERROR:
        # Сетевая ошибка - мягкая ошибка
        mark_soft_error(db, account.id, description)
        log(db, task_id, account.id, f"Ошибка сети: {description}")
    
    else:
        # Остальные ошибки - мягкая ошибка
        mark_soft_error(db, account.id, description)
        log(db, task_id, account.id, f"Ошибка: {description}")
    
    return error_type


def process_task(task_id: int) -> None:
    """
    Обрабатывает одну задачу с retry на других аккаунтах при ошибках.
    
    Args:
        task_id: ID задачи для обработки
    """
    # ИСПРАВЛЕНО: Используем синхронную версию БД для executor (работает в отдельных потоках)
    # TODO: В будущем перейти на AsyncSession для полной асинхронности
    db: Session = next(get_sync_db())
    task_start_time = time.time()
    task_start_monotonic = time.monotonic()
    task_timeout_seconds = max(1, int(getattr(settings, "TASK_PROCESSING_TIMEOUT", 1800) or 1800))
    task_timeout_cooldown_seconds = max(0, int(getattr(settings, "TASK_TIMEOUT_COOLDOWN_SECONDS", 60) or 0))
    task_deadline_at = task_start_monotonic + task_timeout_seconds
    task_timed_out = False
    task_timeout_logged = False
    task_timeout_last_close_at = 0.0
    task_stopped_by_user = False
    loop = get_event_loop()
    task_thread_id = threading.get_ident()
    stop_watcher_stop = threading.Event()

    def mark_task_timed_out(reason: str = "task_timeout") -> bool:
        nonlocal task_timed_out, task_timeout_logged, task_timeout_last_close_at
        task_timed_out = True
        if not task_timeout_logged:
            task_timeout_logged = True
            logger.warning(
                "Task %s: hard processing timeout reached after %ss; closing task browsers",
                task_id,
                task_timeout_seconds,
            )
        now_ts = time.monotonic()
        if reason == "task_timeout_final" or now_ts - task_timeout_last_close_at >= 2.0:
            task_timeout_last_close_at = now_ts
            close_task_browsers(reason)
        return True

    def task_timeout_requested_now() -> bool:
        if time.monotonic() < task_deadline_at:
            return False
        return mark_task_timed_out("task_timeout")

    def close_task_browsers(reason: str) -> None:
        try:
            from executor.browser_pool import close_task_pages

            if loop.is_closed():
                return
            coroutine = close_task_pages(task_id, reason=reason)
            if loop.is_running():
                if threading.get_ident() == task_thread_id:
                    close_future = loop.create_task(coroutine)

                    def _log_close_result(future: asyncio.Future) -> None:
                        try:
                            closed = future.result()
                            if closed:
                                logger.info(
                                    "Task %s: closed %s browser page(s) after %s",
                                    task_id,
                                    closed,
                                    reason,
                                )
                        except asyncio.CancelledError:
                            pass
                        except Exception as close_result_error:
                            logger.debug(
                                "Task %s: async browser close failed after %s: %s",
                                task_id,
                                reason,
                                close_result_error,
                            )

                    close_future.add_done_callback(_log_close_result)
                    return
                future = asyncio.run_coroutine_threadsafe(coroutine, loop)
                try:
                    closed = future.result(timeout=5)
                    if closed:
                        logger.info("Task %s: closed %s browser page(s) after %s", task_id, closed, reason)
                except Exception as close_wait_error:
                    logger.debug("Task %s: async browser close scheduled but not confirmed: %s", task_id, close_wait_error)
                return
            if threading.get_ident() == task_thread_id:
                closed = loop.run_until_complete(coroutine)
                if closed:
                    logger.info("Task %s: closed %s browser page(s) after %s", task_id, closed, reason)
            else:
                coroutine.close()
        except Exception as close_error:
            logger.debug("Task %s: failed to close task browsers after %s: %s", task_id, reason, close_error)

    def watch_task_stop_signal() -> None:
        try:
            from services.task_stop_signal import is_task_stop_requested
        except Exception as import_error:
            logger.debug("Task %s: stop watcher disabled: %s", task_id, import_error)
            is_task_stop_requested = lambda _task_id: False

        logged_stop_signal = False
        while not stop_watcher_stop.wait(0.5):
            try:
                if time.monotonic() >= task_deadline_at:
                    mark_task_timed_out("task_timeout_watcher")
                if is_task_stop_requested(task_id):
                    if not logged_stop_signal:
                        logger.info("Task %s: Redis stop signal detected; closing task browsers", task_id)
                        logged_stop_signal = True
                    close_task_browsers("redis_stop_signal")
            except Exception as watch_error:
                logger.debug("Task %s: stop watcher check failed: %s", task_id, watch_error)

    stop_watcher_thread = threading.Thread(
        target=watch_task_stop_signal,
        name=f"TaskStopWatcher-{task_id}",
        daemon=True,
    )
    stop_watcher_thread.start()
    
    # Используем модульный shutdown_event executor'а (см. _shutdown_event выше).
    shutdown_event = _shutdown_event
    
    try:
        # ИСПРАВЛЕНО: Используем SELECT FOR UPDATE для атомарной блокировки задачи
        # Это предотвращает race condition, когда два воркера пытаются взять одну задачу
        try:
            # SQLite не поддерживает nowait=True, поэтому используем без него
            # Для PostgreSQL можно использовать nowait=True для немедленного возврата
            task = db.query(Task).filter(
                Task.id == task_id,
                Task.status == "pending"  # Фильтруем только pending задачи
            ).with_for_update().first()
        except Exception as lock_error:
            # Если блокировка не поддерживается (например, старый SQLite), используем обычный запрос
            # Это не идеально, но лучше чем падение
            logger.warning(f"SELECT FOR UPDATE not supported, falling back to regular query: {lock_error}")
            task = db.query(Task).filter(
                Task.id == task_id,
                Task.status == "pending"
            ).first()
        
        if not task:
            # Задача либо не найдена, либо уже взята другим воркером, либо не в статусе pending
            logger.debug(f"Task {task_id} not available (not found or already taken by another worker)")
            return

        # Устанавливаем статус in_progress (задача заблокирована, можем безопасно обновить)
        # ИСПРАВЛЕНО: Валидация перехода статуса через state machine
        from services.task_state_machine import validate_status_transition
        try:
            validate_status_transition(task.status, "in_progress")
            task.status = "in_progress"
        except Exception as e:
            logger.warning(f"Task {task_id}: Invalid status transition {task.status} -> in_progress: {e}, continuing anyway")
            task.status = "in_progress"  # Продолжаем для обратной совместимости
        task.attempts += 1
        task.updated_at = datetime.now(timezone.utc)
        db.commit()
        db.refresh(task)

        # Разбираем настройки задачи
        task_settings = task.settings or {}
        delays_enabled = task_settings.get("delays", True)
        shuffle_enabled = task_settings.get("shuffle", False)
        rotate_accounts = task_settings.get("rotate_accounts", True)
        reply_count = task_settings.get("reply_count", 3)
        policy = ExecutionPolicy.from_settings()
        # Изоляция аккаунт-пулов:
        # account_pool_mode == "personal" -> используем личные аккаунты владельца task'а
        # (Account.owner_user_id == task.user_id). Иначе -> общий пул.
        account_pool_mode = (task_settings.get("account_pool_mode") or "shared").lower()
        if account_pool_mode == "personal":
            pool_owner_user_id: int | None = int(task.user_id)
        else:
            pool_owner_user_id = None
        text_variants = task_settings.get("text_variants") or [task.main_comment]
        text_variants = [t for t in text_variants if t]
        if not text_variants:
            logger.error(f"Task {task_id}: no text variants available")
            task.status = "error"
            task.error_reason = "Нет доступных текстов для публикации"
            db.commit()
            return

        logger.info(f"Processing task {task_id}: {len(task.video_urls)} URLs, delays={delays_enabled}, shuffle={shuffle_enabled}, rotate={rotate_accounts}")

        video_urls = list(task.video_urls or [])
        
        # Перемешиваем ссылки, если включено
        if shuffle_enabled:
            random.shuffle(video_urls)
            logger.info(f"Task {task_id}: URLs shuffled")
        
        has_errors = False
        error_messages = []
        shadow_ban_notification_sent = False  # Флаг для отслеживания отправки уведомления о shadow ban
        
        # REFACTOR: Инициализируем структуры для per-video outcomes
        # WHY: Сохраняем результаты по каждому видео для возможности повтора failed
        # IDEMPOTENCY: при resume задачи (например, после crash или таймаута)
        # переиспользуем уже сохранённые url_results, чтобы не публиковать
        # дубликаты комментариев на тех URL, где основа уже отработала.
        # Раньше при повторе цикл стартовал с idx=0 и повторно публиковал
        # main + replies на видео, где основа уже отработала.
        # Теперь resume пропускает только public-confirmed success.
        previous_url_results = (task.settings or {}).get("url_results") or {}
        url_results = dict(previous_url_results)  # carry forward existing rows
        failed_urls = []
        unconfirmed_urls = []
        successful_urls_list = list(
            url
            for url, row in previous_url_results.items()
            if isinstance(row, dict) and row.get("status") in _URL_SUCCESS_STATUSES
        )
        successful_urls = len(successful_urls_list)

        def read_stop_marker() -> tuple[bool, dict]:
            nonlocal task_stopped_by_user
            try:
                with db.no_autoflush:
                    row = db.query(Task.status, Task.settings).filter(Task.id == task_id).first()
            except Exception as stop_check_error:
                logger.warning(f"Task {task_id}: stop check failed: {stop_check_error}")
                return False, {}

            if not row:
                return False, {}
            current_status, current_settings = row
            if str(current_status or "").lower() != "stopped":
                return False, {}

            task_stopped_by_user = True
            close_task_browsers("db_stop_marker")
            if isinstance(current_settings, dict):
                return True, dict(current_settings)
            return True, {}

        def merge_stopped_settings(local_settings: dict | None, stopped_settings: dict | None) -> dict:
            merged = dict(stopped_settings or {})
            local_settings = dict(local_settings or {})
            stopped_url_results = merged.get("url_results") if isinstance(merged.get("url_results"), dict) else {}
            local_url_results = local_settings.get("url_results") if isinstance(local_settings.get("url_results"), dict) else {}
            merged.update(local_settings)
            merged["url_results"] = {**stopped_url_results, **local_url_results}
            merged["stopped_by_user"] = True
            merged.setdefault("stopped_at", datetime.now(timezone.utc).isoformat())
            return merged

        def normalize_url_result_for_timeout(result_row: dict) -> dict:
            if not task_timed_out or not isinstance(result_row, dict):
                return result_row
            normalized = dict(result_row)
            if str(normalized.get("error") or "") in {"", "stopped_by_user"}:
                normalized["error"] = "task_timeout"
            normalized["timeout_stopped"] = True
            timing = normalized.get("timing")
            if isinstance(timing, dict):
                timing = dict(timing)
                timing["timeout_stopped"] = True
                timing["timeout_seconds"] = task_timeout_seconds
                normalized["timing"] = timing
            return normalized

        def task_stop_requested_now() -> bool:
            nonlocal task_stopped_by_user
            if task_timeout_requested_now():
                return True
            if shutdown_event and shutdown_event.is_set():
                task_stopped_by_user = True
                return True
            try:
                with db.no_autoflush:
                    status_row = db.query(Task.status).filter(Task.id == task_id).first()
                if status_row and str(status_row[0] or "").lower() == "stopped":
                    task_stopped_by_user = True
                    return True
            except Exception as stop_check_error:
                logger.debug(
                    "Task %s: lightweight stop check failed: %s",
                    task_id,
                    stop_check_error,
                )
            try:
                from services.task_stop_signal import is_task_stop_requested

                if is_task_stop_requested(task_id):
                    task_stopped_by_user = True
                    return True
            except Exception as stop_signal_error:
                logger.debug(
                    "Task %s: redis stop check failed: %s",
                    task_id,
                    stop_signal_error,
                )
            return False

        def sleep_or_stop(delay_seconds: float) -> bool:
            nonlocal task_stopped_by_user
            sleep_until = time.monotonic() + max(0.0, float(delay_seconds or 0))
            while True:
                if task_timeout_requested_now():
                    return True
                if shutdown_event and shutdown_event.is_set():
                    task_stopped_by_user = True
                    return True
                stopped_now, _ = read_stop_marker()
                if stopped_now:
                    return True
                remaining = sleep_until - time.monotonic()
                if remaining <= 0:
                    return False
                time.sleep(min(0.5, remaining))

        def wait_for_full_url_lock(full_url_lock: threading.Lock, lock_key: str, url_index: int) -> bool:
            if full_url_lock.acquire(blocking=False):
                return True

            logger.info(
                "Task %s, URL %s: another worker is already processing this video; waiting. key=%s",
                task_id,
                url_index + 1,
                lock_key[:80],
            )
            while True:
                if task_timeout_requested_now():
                    logger.info(
                        "Task %s, URL %s: timeout while waiting for video lock. key=%s",
                        task_id,
                        url_index + 1,
                        lock_key[:80],
                    )
                    return False
                if task_stop_requested_now():
                    return False
                if full_url_lock.acquire(timeout=0.5):
                    return True

        def stop_due_to_shadow(text_used: str, url_index: int) -> bool:
            stats = get_text_stats(text_used)
            if stats.get("shadow", 0) >= settings.TEXT_SHADOW_THRESHOLD:
                remaining = len(video_urls) - url_index
                try:
                    from services.balance_service import get_pricing, refund_balance
                    pricing = get_pricing(db)
                    refund_amount = Decimal(pricing.price_success) * Decimal(max(0, remaining))
                    if refund_amount > 0:
                        refund_balance(db, task.user_id, refund_amount, task_id=task.id, description="Shadow ban stop refund")
                        task.refunded_cost = (task.refunded_cost or Decimal("0")) + refund_amount
                except Exception as refund_err:
                    logger.warning(f"Task {task_id}: refund on shadow stop failed: {refund_err}")
                    db.rollback()
                task.status = "error"
                task.error_reason = f"Текст в тени: {stats.get('shadow', 0)} подряд. Остановлено."
                task.updated_at = datetime.now(timezone.utc)
                db.commit()
                log(db, task_id, None, f"Остановлено из-за тени текста. Осталось URL: {remaining}")
                return True
            return False

        # Обрабатываем каждую ссылку
        for idx, video_url in enumerate(video_urls):
            if task_timeout_requested_now():
                logger.info(f"Task {task_id}: timeout detected before URL {idx+1}/{len(video_urls)}")
                break

            # ИСПРАВЛЕНО: Проверяем shutdown перед обработкой каждого URL
            if shutdown_event and shutdown_event.is_set():
                logger.info(f"Task {task_id}: Shutdown detected, stopping URL processing at {idx+1}/{len(video_urls)}")
                break

            stopped_now, _ = read_stop_marker()
            if stopped_now:
                logger.info(f"Task {task_id}: user stop detected before URL {idx+1}/{len(video_urls)}")
                break

            # IDEMPOTENCY: пропускаем URL, где основа уже отработала успешно.
            # Это защищает от двойного коммента при повторном запуске задачи
            # (resume после crash, ручной replay через Telegram, retry воркера).
            existing_row = previous_url_results.get(video_url)
            if (
                isinstance(existing_row, dict)
                and existing_row.get("status") in _URL_SUCCESS_STATUSES
            ):
                logger.info(
                    f"Task {task_id}, URL {idx+1}: skipping — already processed "
                    f"(status={existing_row.get('status')}, "
                    f"comment_id={existing_row.get('comment_id')})"
                )
                # progress отражает позицию в списке, не количество новых публикаций
                task.progress = idx + 1
                db.commit()
                continue

            # ANTI-BOT: рандомная пауза между видео.
            # Без этой паузы три коммента подряд от разных аккаунтов с одного
            # IP/proxy выглядят как явный бот-кластер. ACCOUNT_COOLDOWN_SECONDS
            # умножается на случайный множитель в [URL_PROCESSING_DELAY_MIN,
            # URL_PROCESSING_DELAY_MAX] — типично 30-90с при дефолтах.
            # Логика восстановлена из старой версии; пользователь может
            # выключить её через task.settings.delays=False.
            if idx > 0 and delays_enabled:
                delay_seconds = random.uniform(
                    settings.ACCOUNT_COOLDOWN_SECONDS * settings.URL_PROCESSING_DELAY_MIN,
                    settings.ACCOUNT_COOLDOWN_SECONDS * settings.URL_PROCESSING_DELAY_MAX,
                )
                logger.info(
                    f"Task {task_id}, URL {idx+1}: anti-bot inter-video delay "
                    f"{delay_seconds:.1f}s (cooldown={settings.ACCOUNT_COOLDOWN_SECONDS}s)"
                )
                if sleep_or_stop(delay_seconds):
                    logger.info(f"Task {task_id}: stop/timeout detected after delay before URL {idx+1}/{len(video_urls)}")
                    break
            
            if task_timeout_requested_now():
                logger.info(f"Task {task_id}: timeout detected before processing URL {idx+1}/{len(video_urls)}")
                break
            
            # HYBRID: Проверяем тип заказа
            task_kind = getattr(task, "kind", "full") or "full"
            
            # Для engagement_only используем engagement_engine
            if task_kind == "engagement_only":
                try:
                    logger.info(f"Task {task_id}, URL {idx+1}: Processing ENGAGEMENT_ONLY order")
                    
                    # Получаем параметры поиска комментария из settings
                    task_settings = task.settings or {}
                    engagement_comment_id = task_settings.get("engagement_comment_id")
                    engagement_username = task_settings.get("engagement_username")
                    engagement_comment_text = task_settings.get("engagement_comment_text") or task.main_comment
                    
                    # ИСПРАВЛЕНО: Записываем тайминги для единого формата
                    started_at = datetime.now(timezone.utc).isoformat()
                    
                    # Используем engagement_engine
                    from executor.engagement_engine import execute_engagement_only
                    from executor.browser_pool import browser_pool_task_scope
                    loop = get_event_loop()
                    with browser_pool_task_scope(task_id):
                        engagement_result = loop.run_until_complete(
                            execute_engagement_only(
                                db=db,
                                task=task,
                                video_url=video_url,
                                comment_id=engagement_comment_id,
                                username=engagement_username,
                                comment_text=engagement_comment_text,
                                task_id=task_id,
                                pool_owner_user_id=pool_owner_user_id,
                                stop_requested=task_stop_requested_now,
                            )
                        )
                    
                    # ИСПРАВЛЕНО: Передаем source, mode, timing в to_dict()
                    ended_at = datetime.now(timezone.utc).isoformat()
                    source = task_settings.get("origin", "manual")
                    mode = task_kind  # "engagement_only"
                    result_dict = engagement_result.to_dict(
                        source=source,
                        mode=mode,
                        started_at=started_at,
                        ended_at=ended_at
                    )
                    result_dict = normalize_url_result_for_timeout(result_dict)
                    url_results[video_url] = result_dict
                    
                    # Сохраняем результат в task_settings
                    if not task_settings.get("url_results"):
                        task_settings["url_results"] = {}
                    task_settings["url_results"][video_url] = result_dict
                    task_settings = _refresh_execution_ledger(task_settings)
                    task.settings = task_settings
                    _mark_task_settings_dirty(task)
                    
                    # Проверяем успешность по статусу из унифицированного формата
                    result_status = result_dict.get("status")
                    if result_status in _URL_SUCCESS_STATUSES:
                        successful_urls += 1
                        successful_urls_list.append(video_url)
                        if result_status in _URL_UNCONFIRMED_STATUSES:
                            unconfirmed_urls.append(video_url)
                        engagement = result_dict.get("engagement", {})
                        replies_info = engagement.get("replies", {})
                        likes_info = engagement.get("likes", {})
                        likes_str = _format_likes_status(likes_info)
                        replies_expected = (
                            replies_info.get("expected_submissions")
                            or replies_info.get("expected")
                            or replies_info.get("requested_variants")
                            or replies_info.get("total", 0)
                        )
                        log_level = logger.warning if result_status in _URL_UNCONFIRMED_STATUSES else logger.info
                        log_level(
                            f"Task {task_id}, URL {idx+1}: ENGAGEMENT_ONLY processed. "
                            f"Status: {result_status}, Replies: {replies_info.get('ok', 0)}/{replies_expected}, "
                            f"{likes_str}"
                        )
                    else:
                        failed_urls.append(video_url)
                        has_errors = True
                        engagement = result_dict.get("engagement", {})
                        replies_info = engagement.get("replies", {})
                        likes_info = engagement.get("likes", {})
                        likes_str = _format_likes_status(likes_info)
                        replies_expected = (
                            replies_info.get("expected_submissions")
                            or replies_info.get("expected")
                            or replies_info.get("requested_variants")
                            or replies_info.get("total", 0)
                        )
                        logger.warning(
                            f"Task {task_id}, URL {idx+1}: ENGAGEMENT_ONLY failed. "
                            f"Status: {result_dict.get('status')}, Error: {result_dict.get('error')}, "
                            f"Replies: {replies_info.get('ok', 0)}/{replies_expected}, "
                            f"{likes_str}"
                        )
                    
                    # Обновляем прогресс
                    stopped_now, stopped_settings = read_stop_marker()
                    if stopped_now:
                        task.settings = merge_stopped_settings(task.settings, stopped_settings)
                        _mark_task_settings_dirty(task)
                    task.progress = idx + 1
                    db.commit()
                    if _reply_visibility_exhausted(result_dict):
                        logger.warning(
                            "Task %s: stopping remaining URLs after four reply accounts "
                            "could not find the parent comment",
                            task_id,
                        )
                        break
                    continue

                except Exception as e:
                    logger.error(f"Task {task_id}, URL {idx+1}: ENGAGEMENT_ONLY error: {e}", exc_info=True)
                    failed_urls.append(video_url)
                    # ИСПРАВЛЕНО: Используем унифицированный формат даже для ошибок с source, mode, timing
                    ended_at = datetime.now(timezone.utc).isoformat()
                    source = task_settings.get("origin", "manual")
                    url_results[video_url] = {
                        "status": "fail",
                        "error": str(e)[:200],
                        "comment_id": None,
                        "source": source,
                        "mode": "engagement_only",
                        "engagement": {
                            "main": {"success": False, "error": str(e)[:200]},
                            "replies": {"ok": 0, "total": 0, "items": []},
                            "likes": {"provider": "boost_project", "ok": False, "quantity": 100, "items": []}
                        },
                        "timing": {
                            "started_at": started_at if 'started_at' in locals() else ended_at,
                            "ended_at": ended_at,
                            "duration_ms": None
                        }
                    }
                    has_errors = True
                    continue
            
            # Для full заказов - используем HYBRID FULL (вариант 3)
            # Выбираем текст (ротация по индексу)
            raw_variant_choices = _main_variants_for_url(
                text_variants=text_variants,
                task_settings=task_settings,
                task_id=task_id,
                url_index=idx,
                video_count=len(video_urls),
            )
            if not raw_variant_choices:
                logger.error(f"Task {task_id}: no usable text variants available for URL {idx+1}")
                task.status = "error"
                task.error_reason = "Нет доступных вариантов текста для публикации"
                task.updated_at = datetime.now(timezone.utc)
                db.commit()
                return

            try:
                logger.info(f"Processing URL {idx+1}/{len(video_urls)} for task {task_id} (HYBRID FULL)")
                
                # ИСПРАВЛЕНО: Записываем тайминги для единого формата
                started_at = datetime.now(timezone.utc).isoformat()
                
                # HYBRID FULL: Используем execute_hybrid_full() для full заказов
                from executor.engagement_engine import execute_hybrid_full, resolve_video_url
                loop = get_event_loop()
                full_lock_source = video_url
                try:
                    resolved_lock_url, resolved_lock_video_id = loop.run_until_complete(
                        resolve_video_url(video_url)
                    )
                    full_lock_source = resolved_lock_video_id or resolved_lock_url or video_url
                except Exception as lock_resolve_exc:
                    logger.debug(
                        "Task %s, URL %s: could not resolve video for lock key: %s",
                        task_id,
                        idx + 1,
                        lock_resolve_exc,
                    )
                _full_lock_key, full_url_lock = _get_full_url_lock(full_lock_source)
                if not wait_for_full_url_lock(full_url_lock, _full_lock_key, idx):
                    break
                try:
                    variant_result_dicts = []
                    # USER_SCHEMA_ENABLED → один main-аккаунт пишет ВСЕ варианты
                    # подряд, reply-аккаунты ищут любой из них и отвечают по
                    # матрице. Старый код (variant-per-account) остаётся как
                    # fallback при выключенном флаге.
                    #
                    # Источник истины — таблица app_settings (admin panel).
                    # Дефолт: env / settings.USER_SCHEMA_ENABLED.
                    commenting_mode = "old"
                    try:
                        from services.app_settings_service import (
                            COMMENTING_MODE_PARALLEL,
                            COMMENTING_MODE_PARALLEL_6X1,
                            get_commenting_mode,
                            get_user_schema_enabled,
                        )
                        user_schema_active = get_user_schema_enabled(db)
                        commenting_mode = get_commenting_mode(db)
                    except Exception:
                        user_schema_active = bool(getattr(settings, "USER_SCHEMA_ENABLED", True))
                        COMMENTING_MODE_PARALLEL = "parallel"
                        COMMENTING_MODE_PARALLEL_6X1 = "parallel_6x1"

                    if user_schema_active:
                        from executor.user_schema import (
                            execute_user_schema_full,
                            execute_user_schema_parallel,
                            execute_user_schema_parallel_6x1,
                        )
                        from executor.browser_pool import browser_pool_task_scope

                        # Подготовка текстов: spintax + skip shadowed-вариантов.
                        prepared_variants: list[str] = []
                        shadowed_results: list[dict] = []
                        for variant_num, raw_variant in enumerate(raw_variant_choices, start=1):
                            text_choice = process_spintax(raw_variant)
                            text_stats = get_text_stats(text_choice)
                            if (
                                text_stats.get("shadow", 0) >= settings.TEXT_SHADOW_THRESHOLD
                                and text_stats.get("success", 0) == 0
                            ):
                                logger.warning(
                                    "Task %s, URL %s: skipping text variant %s/%s because it looks shadowed",
                                    task_id,
                                    idx + 1,
                                    variant_num,
                                    len(raw_variant_choices),
                                )
                                shadowed_results.append(
                                    {
                                        "status": "fail",
                                        "core_status": "fail",
                                        "comment_id": None,
                                        "error": "text_variant_shadowed",
                                        "warnings": [],
                                        "has_warnings": False,
                                        "text_choice": text_choice,
                                        "variant_index": variant_num,
                                        "source": task_settings.get("origin", "manual"),
                                        "mode": "full",
                                        "engagement": {
                                            "main": {
                                                "success": False,
                                                "posted": False,
                                                "verified": False,
                                                "text_choice": text_choice,
                                                "error": "text_variant_shadowed",
                                            },
                                            "replies": {"ok": 0, "total": 0, "items": []},
                                            "likes": {"enabled": False, "requested": False, "ok": True},
                                        },
                                    }
                                )
                                continue
                            prepared_variants.append(text_choice)

                        variant_started_at = datetime.now(timezone.utc).isoformat()
                        if prepared_variants:
                            boost_enabled_user, boost_qty_user = (
                                False,
                                coerce_likes_quantity(settings.BOOST_PROJECT_LIKES_QUANTITY),
                            )
                            try:
                                from executor.engagement_engine import _get_task_boost_config
                                boost_enabled_user, boost_qty_user = _get_task_boost_config(db, task)
                            except Exception:
                                logger.debug("user_schema: failed to read boost config", exc_info=True)

                            with browser_pool_task_scope(task_id):
                                def user_schema_stop_requested() -> bool:
                                    nonlocal task_stopped_by_user
                                    if task_timeout_requested_now():
                                        return True
                                    try:
                                        with db.no_autoflush:
                                            status_row = db.query(Task.status).filter(Task.id == task_id).first()
                                        if status_row and str(status_row[0] or "").lower() == "stopped":
                                            task_stopped_by_user = True
                                            return True
                                    except Exception as stop_check_error:
                                        logger.debug(
                                            "Task %s: lightweight stop check failed: %s",
                                            task_id,
                                            stop_check_error,
                                        )
                                    try:
                                        from services.task_stop_signal import is_task_stop_requested

                                        if is_task_stop_requested(task_id):
                                            task_stopped_by_user = True
                                            return True
                                    except Exception as stop_signal_error:
                                        logger.debug(
                                            "Task %s: redis stop check failed in user schema: %s",
                                            task_id,
                                            stop_signal_error,
                                        )
                                    return False

                                if commenting_mode == COMMENTING_MODE_PARALLEL_6X1:
                                    user_schema_runner = execute_user_schema_parallel_6x1
                                elif commenting_mode == COMMENTING_MODE_PARALLEL:
                                    user_schema_runner = execute_user_schema_parallel
                                else:
                                    user_schema_runner = execute_user_schema_full
                                logger.info(
                                    "Task %s, URL %s: user_schema commenting_mode=%s",
                                    task_id,
                                    idx + 1,
                                    commenting_mode,
                                )
                                schema_result = loop.run_until_complete(
                                    user_schema_runner(
                                        db=db,
                                        task=task,
                                        video_url=video_url,
                                        text_variants=prepared_variants,
                                        task_id=task_id,
                                        boost_enabled=boost_enabled_user,
                                        boost_quantity=boost_qty_user,
                                        pool_owner_user_id=pool_owner_user_id,
                                        stop_requested=user_schema_stop_requested,
                                    )
                                )
                            variant_ended_at = datetime.now(timezone.utc).isoformat()
                            schema_dict = schema_result.to_dict(
                                source=task_settings.get("origin", "manual"),
                                mode="full",
                            )
                            schema_dict["timing"]["started_at"] = variant_started_at
                            schema_dict["timing"]["ended_at"] = variant_ended_at
                            schema_dict["text_choice"] = prepared_variants[0] if prepared_variants else None
                            variant_result_dicts.extend(shadowed_results)
                            variant_result_dicts.append(schema_dict)
                        else:
                            variant_result_dicts.extend(shadowed_results)
                    else:
                        for variant_num, raw_variant in enumerate(raw_variant_choices, start=1):
                            text_choice = process_spintax(raw_variant)
                            text_stats = get_text_stats(text_choice)
                            if (
                                text_stats.get("shadow", 0) >= settings.TEXT_SHADOW_THRESHOLD
                                and text_stats.get("success", 0) == 0
                            ):
                                logger.warning(
                                    "Task %s, URL %s: skipping text variant %s/%s because it looks shadowed",
                                    task_id,
                                    idx + 1,
                                    variant_num,
                                    len(raw_variant_choices),
                                )
                                variant_result_dicts.append(
                                    {
                                        "status": "fail",
                                        "core_status": "fail",
                                        "comment_id": None,
                                        "error": "text_variant_shadowed",
                                        "warnings": [],
                                        "has_warnings": False,
                                        "text_choice": text_choice,
                                        "variant_index": variant_num,
                                        "source": task_settings.get("origin", "manual"),
                                        "mode": "full",
                                        "engagement": {
                                            "main": {
                                                "success": False,
                                                "posted": False,
                                                "verified": False,
                                                "text_choice": text_choice,
                                                "error": "text_variant_shadowed",
                                            },
                                            "replies": {"ok": 0, "total": 0, "items": []},
                                            "likes": {"enabled": False, "requested": False, "ok": True},
                                        },
                                    }
                                )
                                continue

                            variant_started_at = datetime.now(timezone.utc).isoformat()
                            variant_task = _task_for_variant(task, allow_likes=(variant_num == 1))
                            from executor.browser_pool import browser_pool_task_scope
                            with browser_pool_task_scope(task_id):
                                hybrid_result = loop.run_until_complete(
                                    execute_hybrid_full(
                                        db=db,
                                        task=variant_task,
                                        video_url=video_url,
                                        text_choice=text_choice,
                                        task_id=task_id,
                                        pool_owner_user_id=pool_owner_user_id,
                                        stop_requested=task_stop_requested_now,
                                    )
                                )
                            variant_ended_at = datetime.now(timezone.utc).isoformat()
                            variant_dict = hybrid_result.to_dict(
                                source=task_settings.get("origin", "manual"),
                                mode="full",
                                started_at=variant_started_at,
                                ended_at=variant_ended_at,
                            )
                            variant_dict["variant_index"] = variant_num
                            variant_dict["text_choice"] = text_choice
                            variant_result_dicts.append(variant_dict)
                finally:
                    full_url_lock.release()

                # ИСПРАВЛЕНО: Передаем source, mode, timing в to_dict()
                ended_at = datetime.now(timezone.utc).isoformat()
                task_settings = task.settings or {}
                source = task_settings.get("origin", "manual")
                mode = "full"  # HYBRID FULL
                result_dict = _merge_full_variant_results(variant_result_dicts)
                result_dict["source"] = source
                result_dict["mode"] = mode
                result_dict["timing"] = result_dict.get("timing") or {}
                result_dict["timing"]["started_at"] = started_at
                result_dict["timing"]["ended_at"] = ended_at
                result_dict = normalize_url_result_for_timeout(result_dict)
                url_results[video_url] = result_dict
                
                # Сохраняем результат в task_settings
                if not task_settings.get("url_results"):
                    task_settings["url_results"] = {}
                task_settings["url_results"][video_url] = result_dict
                task_settings = _refresh_execution_ledger(task_settings)
                task.settings = task_settings
                _mark_task_settings_dirty(task)
                
                # Count submitted_unconfirmed as processed. It stays visible in
                # url_results/warnings, but should not make the whole order fail.
                result_status = result_dict.get("status")
                if result_status in _URL_SUCCESS_STATUSES:
                    successful_urls += 1
                    successful_urls_list.append(video_url)
                    if result_status in _URL_UNCONFIRMED_STATUSES:
                        unconfirmed_urls.append(video_url)
                    engagement = result_dict.get("engagement", {})
                    replies_info = engagement.get("replies", {})
                    likes_info = engagement.get("likes", {})
                    likes_str = _format_likes_status(likes_info)
                    submitted_replies = replies_info.get("submitted")
                    if submitted_replies is None:
                        submitted_replies = (
                            int(replies_info.get("ok") or 0)
                            + int(replies_info.get("submitted_unconfirmed") or 0)
                        )
                    replies_expected = (
                        replies_info.get("expected_submissions")
                        or replies_info.get("expected")
                        or replies_info.get("requested_variants")
                        or replies_info.get("total", 0)
                    )
                    if result_status in _URL_UNCONFIRMED_STATUSES:
                        logger.warning(
                            f"Task {task_id}, URL {idx+1}: HYBRID FULL submitted but awaiting public confirmation. "
                            f"Status: {result_status}, Error: {result_dict.get('error')}, "
                            f"Replies submitted: {submitted_replies}/{replies_expected} "
                            f"(public={replies_info.get('ok', 0)}, "
                            f"unconfirmed={replies_info.get('submitted_unconfirmed', 0)}), "
                            f"{likes_str}"
                        )
                    else:
                        logger.info(
                            f"Task {task_id}, URL {idx+1}: HYBRID FULL completed successfully. "
                            f"Replies: {replies_info.get('ok', 0)}/{replies_expected}, "
                            f"{likes_str}"
                        )
                else:
                    failed_urls.append(video_url)
                    has_errors = True
                    error_messages.append(
                        f"{video_url}: {result_dict.get('status') or 'fail'} "
                        f"({result_dict.get('error') or 'not_publicly_confirmed'})"
                    )
                    engagement = result_dict.get("engagement", {})
                    replies_info = engagement.get("replies", {})
                    likes_info = engagement.get("likes", {})
                    likes_str = _format_likes_status(likes_info)
                    replies_expected = (
                        replies_info.get("expected_submissions")
                        or replies_info.get("expected")
                        or replies_info.get("requested_variants")
                        or replies_info.get("total", 0)
                    )
                    logger.warning(
                        f"Task {task_id}, URL {idx+1}: HYBRID FULL failed. "
                        f"Status: {result_dict.get('status')}, Error: {result_dict.get('error')}, "
                        f"Replies: {replies_info.get('ok', 0)}/{replies_expected}, "
                        f"{likes_str}"
                    )
                
                # Обновляем прогресс
                stopped_now, stopped_settings = read_stop_marker()
                if stopped_now:
                    task.settings = merge_stopped_settings(task.settings, stopped_settings)
                    _mark_task_settings_dirty(task)
                task.progress = idx + 1
                db.commit()
                if _reply_visibility_exhausted(result_dict):
                    logger.warning(
                        "Task %s: stopping remaining URLs after four reply accounts "
                        "could not find the published main comments",
                        task_id,
                    )
                    break
                
            except Exception as e:
                logger.error(f"Task {task_id}, URL {idx+1}: HYBRID FULL error: {e}", exc_info=True)
                failed_urls.append(video_url)
                # ИСПРАВЛЕНО: Используем унифицированный формат даже для ошибок с source, mode, timing
                ended_at = datetime.now(timezone.utc).isoformat()
                task_settings = task.settings or {}
                source = task_settings.get("origin", "manual")
                url_results[video_url] = {
                    "status": "fail",
                    "error": str(e)[:200],
                    "comment_id": None,
                    "source": source,
                    "mode": "full",
                    "engagement": {
                        "main": {"success": False, "error": str(e)[:200]},
                        "replies": {"ok": 0, "total": 0, "items": []},
                        "likes": {"provider": "boost_project", "ok": False, "quantity": 100, "items": []}
                    },
                    "timing": {
                        "started_at": started_at if 'started_at' in locals() else ended_at,
                        "ended_at": ended_at,
                        "duration_ms": None
                    }
                }
                has_errors = True
                continue

        # Финализация задачи
        if task_timed_out:
            close_task_browsers("task_timeout_final")
            task_settings = task.settings if isinstance(task.settings, dict) else {}
            task_settings["url_results"] = {
                **(task_settings.get("url_results") if isinstance(task_settings.get("url_results"), dict) else {}),
                **url_results,
            }
            task_settings["failed_urls"] = failed_urls
            task_settings["unconfirmed_urls"] = unconfirmed_urls
            task_settings["successful_urls"] = successful_urls_list
            task_settings["timeout_stopped"] = True
            task_settings["timeout_seconds"] = task_timeout_seconds
            task_settings["timeout_cooldown_seconds"] = task_timeout_cooldown_seconds
            task_settings["stopped_at"] = datetime.now(timezone.utc).isoformat()
            task_settings.pop("stopped_by_user", None)
            task_settings = _refresh_execution_ledger(task_settings)
            task.settings = task_settings
            _mark_task_settings_dirty(task)
            task.status = "stopped"
            task.error_reason = "task_timeout"
            task.updated_at = datetime.now(timezone.utc)
            db.commit()
            log(db, task_id, None, f"Task stopped by timeout after {task_timeout_seconds}s")
            logger.warning(
                "Task %s: finalized as stopped by timeout (%s/%s URLs processed)",
                task_id,
                successful_urls,
                len(video_urls),
            )
            if task_timeout_cooldown_seconds > 0 and not shutdown_event.is_set():
                logger.info(
                    "Task %s: cooling down for %ss before taking the next task",
                    task_id,
                    task_timeout_cooldown_seconds,
                )
                cooldown_until = time.monotonic() + task_timeout_cooldown_seconds
                while not shutdown_event.is_set():
                    remaining = cooldown_until - time.monotonic()
                    if remaining <= 0:
                        break
                    time.sleep(min(0.5, remaining))
            return

        stopped_now, stopped_settings = read_stop_marker()
        if task_stopped_by_user or stopped_now:
            task_settings = task.settings if isinstance(task.settings, dict) else {}
            task_settings = merge_stopped_settings(task_settings, stopped_settings)
            task_settings["url_results"] = {
                **(task_settings.get("url_results") if isinstance(task_settings.get("url_results"), dict) else {}),
                **url_results,
            }
            task_settings["failed_urls"] = failed_urls
            task_settings["unconfirmed_urls"] = unconfirmed_urls
            task_settings["successful_urls"] = successful_urls_list
            task_settings = _refresh_execution_ledger(task_settings)
            task.settings = task_settings
            _mark_task_settings_dirty(task)
            task.status = "stopped"
            task.error_reason = "stopped_by_user"
            task.updated_at = datetime.now(timezone.utc)
            db.commit()
            log(db, task_id, None, "Заказ остановлен пользователем")
            logger.info(
                f"Task {task_id}: finalized as stopped by user "
                f"({successful_urls}/{len(video_urls)} URLs successful)"
            )
            return

        if has_errors:
            if successful_urls == 0:
                task.status = "error"
            else:
                # ИСПРАВЛЕНО: Валидация перехода статуса
                from services.task_state_machine import safe_transition
                new_status = safe_transition(task.status, "partial")
                if new_status:
                    task.status = new_status
                else:
                    logger.warning(f"Task {task_id}: Invalid status transition {task.status} -> partial, using 'error' instead")
                    task.status = "error"
            
            # Формируем понятное сообщение об ошибке
            error_summary = f"Успешно: {successful_urls}/{len(video_urls)} ссылок.\n"
            if error_messages:
                error_summary += "Ошибки:\n" + "\n".join(error_messages[:5])
                if len(error_messages) > 5:
                    error_summary += f"\n...и ещё {len(error_messages) - 5} ошибок"
            task.error_reason = error_summary
            
            # ИСПРАВЛЕНО: url_results уже заполнен в унифицированном формате, просто сохраняем
            # НО: нужно сохранить в task_settings, так как url_results может быть передан из предыдущих итераций
            task_settings = task.settings or {}
            if not task_settings.get("url_results"):
                task_settings["url_results"] = {}
            # Объединяем с уже существующими результатами (на случай частичной обработки)
            task_settings["url_results"].update(url_results)
            task_settings["failed_urls"] = failed_urls
            task_settings["unconfirmed_urls"] = unconfirmed_urls
            task_settings["successful_urls"] = successful_urls_list
            task_settings = _refresh_execution_ledger(task_settings)
            task.settings = task_settings
            _mark_task_settings_dirty(task)
            
            # ИСПРАВЛЕНО: Возврат средств за неудачные видео (централизованный расчёт)
            user = db.query(User).filter(User.id == task.user_id).first()
            if user and not balance_is_admin(user) and task.total_cost:
                refund_units = len(video_urls) - successful_urls
                if refund_units > 0:
                    try:
                        pricing = get_pricing(db)
                        # ИСПРАВЛЕНО: Используем централизованную функцию расчёта возврата
                        from services.balance_service import calculate_refund_amount
                        total_refund = calculate_refund_amount(refund_units, pricing)
                        refund_reason_parts = []
                        if failed_urls:
                            refund_reason_parts.append(f"{len(failed_urls)} failed")
                        refund_reason = ", ".join(refund_reason_parts) or f"{refund_units} not completed"
                        
                        refund_balance(
                            db,
                            task.user_id,
                            total_refund,
                            task_id=task_id,
                            description=f"Refund for {refund_reason} video(s) in task #{task_id}"
                        )
                        
                        task.refunded_cost = total_refund
                        logger.info(
                            "Task %s: Refunded %s UAH for %s video(s): %s",
                            task_id,
                            total_refund,
                            refund_units,
                            refund_reason,
                        )
                    except Exception as refund_error:
                        logger.error(f"Task {task_id}: Error refunding balance: {refund_error}", exc_info=True)
            
            if unconfirmed_urls:
                logger.warning(
                    f"Task {task_id} completed with errors and unconfirmed submissions: "
                    f"{successful_urls}/{len(video_urls)} URLs processed, "
                    f"{len(failed_urls)} failed, {len(unconfirmed_urls)} awaiting confirmation"
                )
            else:
                logger.warning(f"Task {task_id} completed with errors: {successful_urls}/{len(video_urls)} URLs successful")
        else:
            # ИСПРАВЛЕНО: Валидация перехода статуса
            from services.task_state_machine import safe_transition
            new_status = safe_transition(task.status, "done")
            if new_status:
                task.status = new_status
            else:
                logger.warning(f"Task {task_id}: Invalid status transition {task.status} -> done, keeping current status")
                # Если переход невалиден, но задача успешна - логируем предупреждение
            task.error_reason = None
            
            # REFACTOR: Сохраняем per-video outcomes даже при успехе
            # WHY: Для полноты информации и обратной совместимости
            task_settings = task.settings or {}
            task_settings["url_results"] = url_results
            task_settings["failed_urls"] = failed_urls
            task_settings["unconfirmed_urls"] = unconfirmed_urls
            task_settings["successful_urls"] = successful_urls_list
            task_settings = _refresh_execution_ledger(task_settings)
            task.settings = task_settings
            _mark_task_settings_dirty(task)
            
            if unconfirmed_urls:
                logger.warning(
                    f"Task {task_id} completed with unconfirmed submissions: "
                    f"{successful_urls}/{len(video_urls)} URLs processed, "
                    f"{len(unconfirmed_urls)} awaiting confirmation"
                )
            else:
                logger.info(f"Task {task_id} completed successfully: {successful_urls} URLs processed")
            
            # ИСПРАВЛЕНО: Записываем метрику успешного завершения задачи
            try:
                from services.metrics import increment_counter
                increment_counter("tasks.completed", value=1, tags={"status": "success"})
            except Exception:
                pass  # Не критично, если метрики не работают
        
        task.updated_at = datetime.now(timezone.utc)
        db.commit()
        
        # ИСПРАВЛЕНО: Записываем метрику времени выполнения задачи
        task_duration = time.time() - task_start_time
        try:
            record_timing("task.execution_time", task_duration * 1000, tags={
                "status": "success" if not has_errors else "error",
                "urls_count": str(len(video_urls)),
                "successful_urls": str(successful_urls)
            })
        except Exception:
            pass  # Не критично, если метрики не работают
        
    except Exception as e:
        logger.error(f"Task {task_id}: Critical error: {e}", exc_info=True)
        try:
            # ИСПРАВЛЕНО: Улучшена проверка на None - используем with_for_update для безопасности
            try:
                task = db.query(Task).filter(Task.id == task_id).with_for_update().first()
            except Exception:
                # Если блокировка не поддерживается, используем обычный запрос
                task = db.query(Task).filter(Task.id == task_id).first()
            
            if task is not None:
                try:
                    db.refresh(task)
                except Exception:
                    pass
                if str(task.status or "").lower() == "stopped":
                    logger.info(f"Task {task_id}: critical error ignored because task is stopped by user")
                    return
                task.status = "error"
                task.error_reason = f"Критическая ошибка: {str(e)[:200]}"
                
                # ИСПРАВЛЕНО: Записываем метрику критической ошибки
                try:
                    from services.metrics import increment_counter
                    increment_counter("tasks.completed", value=1, tags={"status": "critical_error"})
                except Exception:
                    pass
                task.updated_at = datetime.now(timezone.utc)
                db.commit()
            else:
                logger.warning(f"Task {task_id} not found when trying to update error status")
        except Exception:
            pass
        
        # ИСПРАВЛЕНО: Записываем метрику времени выполнения задачи даже при критической ошибке
        try:
            task_duration = time.time() - task_start_time
            record_timing("task.execution_time", task_duration * 1000, tags={
                "status": "critical_error"
            })
        except Exception:
            pass
    
    finally:
        stop_watcher_stop.set()
        try:
            stop_watcher_thread.join(timeout=1.0)
        except Exception:
            pass
        # ИСПРАВЛЕНО: Гарантированное закрытие DB сессии даже при любой ошибке
        try:
            if db:
                db.close()
        except Exception as close_err:
            logger.warning(f"Task {task_id}: Error closing DB session: {close_err}")


def worker(worker_id: int) -> None:
    """
    Воркер, который берёт задачи из очереди.
    
    ИСПРАВЛЕНО: Добавлен graceful shutdown с проверкой shutdown_event и закрытием browser pool.
    """
    logger.info(f"Worker {worker_id} started")
    
    # Инициализируем event loop для потока
    get_event_loop()
    
    # Используем модульный shutdown_event executor'а.
    shutdown_event = _shutdown_event
    
    last_recovery_time = time.time()
    # ИСПРАВЛЕНО: Добавляем случайную задержку для восстановления зависших задач
    # Это предотвращает одновременное восстановление всеми воркерами
    recovery_delay = random.uniform(0, settings.STUCK_TASKS_RECOVERY_INTERVAL * 0.2)  # До 20% от интервала
    last_recovery_time -= recovery_delay  # Смещаем время первого восстановления
    db: Session = None
    
    try:
        # ИСПРАВЛЕНО: Проверяем shutdown_event вместо бесконечного цикла
        while not shutdown_event.is_set():
            try:
                # Периодическое восстановление зависших задач
                # ИСПРАВЛЕНО: Только воркер 1 делает восстановление, остальные пропускают
                # Это предотвращает одновременные запросы от всех 20 воркеров
                if worker_id == 1 and time.time() - last_recovery_time >= settings.STUCK_TASKS_RECOVERY_INTERVAL:
                    try:
                        if db is None:
                            db = next(get_sync_db())
                        from services.task_service import recover_stuck_tasks
                        recovered = recover_stuck_tasks(db)
                        if recovered > 0:
                            logger.info(f"Worker {worker_id}: Recovered {recovered} stuck task(s)")
                        last_recovery_time = time.time()
                    except Exception as e:
                        logger.error(f"Worker {worker_id}: Error recovering stuck tasks: {e}")
                        if db:
                            try:
                                db.close()
                            except:
                                pass
                            db = None
                
                task_lock = _acquire_global_task_execution_lock(worker_id)
                if task_lock is None:
                    _sleep_until_next_worker_poll(shutdown_event)
                    continue
                try:
                    task_id = dequeue_task()
                    if task_id:
                        if db is None:
                            db = next(get_sync_db())
                        try:
                            db.expire_all()
                        except Exception:
                            pass
                        task_snapshot = db.query(Task).filter(Task.id == task_id).first()
                        if not task_snapshot:
                            logger.debug("Worker %s discarded queued task %s: task not found", worker_id, task_id)
                            continue
                        if str(task_snapshot.status or "").lower() != "pending":
                            logger.debug(
                                "Worker %s discarded queued task %s: status=%s",
                                worker_id,
                                task_id,
                                task_snapshot.status,
                            )
                            continue
                        if _should_defer_loaded_task_for_parsing(db, task_snapshot):
                            global _last_parsing_pause_log_at
                            requeue_deferred_task(
                                task_id,
                                priority_level=task_queue_priority_level(task_snapshot),
                            )
                            now_ts = time.time()
                            if now_ts - _last_parsing_pause_log_at >= 60:
                                logger.info(
                                    "Executor deferred non-admin task %s: software_mode=parsing",
                                    task_id,
                                )
                                _last_parsing_pause_log_at = now_ts
                            _release_global_task_execution_lock(task_lock, worker_id)
                            task_lock = None
                            _sleep_until_next_worker_poll(shutdown_event)
                            continue
                        logger.info(f"Worker {worker_id} got task {task_id}")
                        _mark_executor_busy(task_id, worker_id)
                        try:
                            process_task(task_id)
                        finally:
                            _mark_executor_idle()
                    else:
                        _release_global_task_execution_lock(task_lock, worker_id)
                        task_lock = None
                        _sleep_until_next_worker_poll(shutdown_event)
                finally:
                    _release_global_task_execution_lock(task_lock, worker_id)
            except KeyboardInterrupt:
                logger.info(f"Worker {worker_id} stopping (KeyboardInterrupt)...")
                break
            except Exception as e:
                logger.error(f"Worker {worker_id} error: {e}", exc_info=True)
                # ИСПРАВЛЕНО: Проверяем shutdown_event даже при ошибках
                if shutdown_event.is_set():
                    break
                time.sleep(settings.WORKER_ERROR_DELAY)
    finally:
        # ИСПРАВЛЕНО: Закрываем browser pool при выходе из worker
        logger.info(f"Worker {worker_id} shutting down, closing browser pool...")
        try:
            from executor.browser_pool import close_browser_pool
            loop = get_event_loop()
            if not loop.is_closed():
                loop.run_until_complete(close_browser_pool())
                logger.info(f"Worker {worker_id}: Browser pool closed")
        except Exception as e:
            logger.warning(f"Worker {worker_id}: Error closing browser pool: {e}")
        
        # Закрываем event loop при выходе из worker
        if hasattr(_thread_local, 'loop') and not _thread_local.loop.is_closed():
            try:
                _thread_local.loop.close()
                logger.info(f"Worker {worker_id}: Event loop closed")
            except Exception as e:
                logger.warning(f"Worker {worker_id}: Error closing event loop: {e}")
        
        logger.info(f"Worker {worker_id} stopped")


# ИСПРАВЛЕНО: Функция auto_validation_worker отключена - автоматическая валидация аккаунтов убрана
# def auto_validation_worker():
#     """Отдельный воркер для автоматической валидации аккаунтов раз в полчаса."""
#     from services.auto_account_validator import auto_validate_accounts
#     import time
#     
#     logger.info("Auto account validation worker started")
#     
#     while True:
#         try:
#             auto_validate_accounts()
#         except Exception as e:
#             logger.error(f"Error in auto validation worker: {e}", exc_info=True)
#         
#         # Проверяем каждые 5 минут (внутри функции проверяется 30-минутный интервал)
#         time.sleep(300)  # 5 минут


def run_executor():
    """
    Запускает executor с несколькими воркерами.
    
    ИСПРАВЛЕНО: Добавлена поддержка graceful shutdown через shutdown_event из main.py.
    """
    # ИСПРАВЛЕНО: Устанавливаем политику event loop для Windows ДО создания потоков
    # Это необходимо для поддержки subprocess в Playwright
    if sys.platform == 'win32':
        try:
            asyncio.set_event_loop_policy(asyncio.WindowsProactorEventLoopPolicy())
            logger.info("Windows ProactorEventLoopPolicy установлена для поддержки subprocess")
        except Exception as e:
            logger.warning(f"Не удалось установить WindowsProactorEventLoopPolicy: {e}")
    
    # Используем модульный shutdown_event executor'а.
    shutdown_event = _shutdown_event

    # Регистрируем signal handlers, чтобы Ctrl+C и kill завершали executor
    # быстро и без зависаний. На Windows доп. SIGBREAK (Ctrl+Pause/Break).
    import signal as _signal

    def _on_signal(signum, frame):
        if shutdown_event.is_set():
            # Двойной сигнал — пользователь хочет резкого выхода.
            logger.warning(
                f"Second signal {signum} received, forcing immediate exit"
            )
            os._exit(1)
        logger.info(f"Signal {signum} received, initiating graceful shutdown...")
        shutdown_event.set()

    try:
        _signal.signal(_signal.SIGINT, _on_signal)
    except Exception as exc:
        logger.warning(f"Could not set SIGINT handler: {exc}")
    if sys.platform == "win32":
        try:
            _signal.signal(_signal.SIGBREAK, _on_signal)  # type: ignore[attr-defined]
        except Exception as exc:
            logger.warning(f"Could not set SIGBREAK handler: {exc}")
    else:
        try:
            _signal.signal(_signal.SIGTERM, _on_signal)
        except Exception as exc:
            logger.warning(f"Could not set SIGTERM handler: {exc}")
    
    configured_threads = int(getattr(settings, "MAX_THREADS", 1) or 1)
    num_threads = 1
    if configured_threads != num_threads:
        logger.info(
            "Serial task execution enabled: overriding MAX_THREADS=%s to %s worker",
            configured_threads,
            num_threads,
        )
    
    # ИСПРАВЛЕНО: Убрана автоматическая валидация аккаунтов - пользователь управляет проверками вручную
    # validation_thread = threading.Thread(target=auto_validation_worker, daemon=True, name="AutoValidationThread")
    # validation_thread.start()
    # logger.info("✅ Auto account validation worker started (checks every 30 minutes)")
    
    logger.info(f"Starting executor with {num_threads} threads")
    logger.info(f"Max actions per account: {settings.MAX_ACTIONS_PER_ACCOUNT}")
    logger.info(f"Account cooldown: {settings.ACCOUNT_COOLDOWN_SECONDS}s")
    logger.info(f"Max retry accounts: {MAX_RETRY_ACCOUNTS}")
    logger.info(f"USE_PROXY: {settings.USE_PROXY}")
    logger.info(f"PLAYWRIGHT_HEADLESS: {settings.PLAYWRIGHT_HEADLESS}")
    logger.info(f"VALIDATE_BEFORE_TASK: {settings.VALIDATE_BEFORE_TASK}")
    # ИСПРАВЛЕНО: Убрана проверка прокси - все прокси считаются рабочими
    # logger.info(f"VALIDATE_PROXY_BEFORE_TASK: {settings.VALIDATE_PROXY_BEFORE_TASK}")
    logger.info(f"VALIDATION_RETRY_COUNT: {settings.VALIDATION_RETRY_COUNT}")

    _clear_stale_global_task_lock_on_startup()
    _run_executor_startup_queue_cleanup()
    
    # Heartbeat в Redis для health API
    _set_executor_activity("ok")
    heartbeat_stop = threading.Event()
    def _heartbeat_loop():
        while not heartbeat_stop.is_set():
            payload = _get_executor_activity_payload()
            status = str(payload.pop("status", "ok") or "ok")
            update_executor_heartbeat(status, **payload)
            time.sleep(10)
        # финальный статус
        update_executor_heartbeat("stopped")

    heartbeat_thread = threading.Thread(target=_heartbeat_loop, name="ExecutorHeartbeat", daemon=True)
    heartbeat_thread.start()

    # Метрика по активным воркерам
    try:
        set_gauge("executor.workers", num_threads)
    except Exception:
        pass

    threads = []
    for i in range(num_threads):
        t = threading.Thread(target=worker, args=(i + 1,), daemon=False, name=f"Worker-{i+1}")
        t.start()
        threads.append(t)
    
    logger.info("Executor started, waiting for tasks...")
    
    try:
        # Event.wait() — блокирующий, но прерывается signal handler'ом
        # (в отличие от time.sleep, который на Windows может игнорировать SIGINT).
        shutdown_event.wait()
        logger.info("Shutdown signal received, stopping executor...")
    except KeyboardInterrupt:
        # Подстраховка на случай если signal handler не успел зарегистрироваться.
        logger.info("Keyboard interrupt received, stopping executor...")
        shutdown_event.set()
    finally:
        # Ждём завершения воркеров с жёстким timeout'ом — иначе Playwright
        # subprocess'ы могут «удерживать» поток и процесс никогда не выйдет.
        logger.info("Waiting for workers to finish (max 15s)...")
        worker_join_timeout = 15
        deadline = time.time() + worker_join_timeout
        for t in threads:
            remaining = max(0.5, deadline - time.time())
            t.join(timeout=remaining)
        alive = [t.name for t in threads if t.is_alive()]
        if alive:
            logger.warning(
                f"{len(alive)} worker(s) still alive after {worker_join_timeout}s: {alive}. "
                "Forcing process exit — OS will terminate any leftover Playwright subprocesses."
            )

        heartbeat_stop.set()
        heartbeat_thread.join(timeout=5)
        logger.info("Executor stopped")

        # Воркеры стартуют с daemon=False (чтобы не быть прибитыми посреди
        # обработки задачи). Из-за этого если кто-то зависнет — процесс
        # не выйдет сам. os._exit гарантирует выход из cmd-окна по Ctrl+C
        # без необходимости лезть в Диспетчер задач.
        if alive:
            os._exit(0)


if __name__ == "__main__":
    run_executor()
