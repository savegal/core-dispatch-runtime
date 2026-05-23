import logging
import threading
import time
from datetime import datetime, timezone
from sqlalchemy.orm import Session
from db.database import get_sync_db  # ИСПРАВЛЕНО: Используем синхронную версию для scheduler
from services.account_service import reset_daily_counters
from services.task_service import (
    recover_stuck_tasks,
    recover_pending_timeout_tasks,
    recover_tasks_lost_in_redis,
)
from services.disk_cleanup import cleanup_diagnostics  # ИСПРАВЛЕНО: Добавлена очистка diagnostics

logger = logging.getLogger("scheduler")


def reset_counters_job():
    """Обнуляет дневные счётчики аккаунтов."""
    # ИСПРАВЛЕНО: Используем синхронную версию БД для scheduler (работает в отдельном потоке)
    db: Session = next(get_sync_db())
    try:
        logger.info("Resetting daily counters...")
        reset_daily_counters(db)
        logger.info("Daily counters reset completed")
    except Exception as e:
        logger.error(f"Error resetting counters: {e}", exc_info=True)
    finally:
        db.close()


def recover_stuck_tasks_job():
    """
    ИСПРАВЛЕНО: Восстанавливает зависшие задачи.
    Задачи в статусе "in_progress" дольше 1 часа возвращаются в "pending".
    """
    # ИСПРАВЛЕНО: Используем синхронную версию БД для scheduler (работает в отдельном потоке)
    db: Session = next(get_sync_db())
    try:
        logger.info("Checking for stuck tasks...")
        recovered_count = recover_stuck_tasks(db)
        if recovered_count > 0:
            logger.info(f"Recovered {recovered_count} stuck task(s)")
        else:
            logger.debug("No stuck tasks found")
    except Exception as e:
        logger.error(f"Error recovering stuck tasks: {e}", exc_info=True)
    finally:
        db.close()


def recover_lost_tasks_job():
    """
    ИСПРАВЛЕНО: Восстанавливает задачи, которые были созданы в БД, но не попали в очередь Redis.
    
    Это может произойти, если Redis упал во время создания задачи.
    """
    db: Session = next(get_sync_db())
    try:
        logger.info("Checking for tasks lost in Redis...")
        recovered_count = recover_tasks_lost_in_redis(db)
        if recovered_count > 0:
            logger.info(f"Recovered {recovered_count} task(s) lost in Redis")
        else:
            logger.debug("No lost tasks found")
    except Exception as e:
        logger.error(f"Error recovering lost tasks: {e}", exc_info=True)
    finally:
        db.close()


def cleanup_diagnostics_job():
    """
    ИСПРАВЛЕНО: Очищает старые диагностические файлы (PNG/HTML) из директории diagnostics/.
    
    Выполняется периодически для предотвращения переполнения диска.
    """
    try:
        logger.info("Cleaning up old diagnostic files...")
        stats = cleanup_diagnostics(max_age_days=7, max_size_mb=1000)
        if stats["deleted"] > 0:
            logger.info(
                f"Diagnostics cleanup: deleted {stats['deleted']} files, "
                f"freed {stats['freed_mb']} MB, {stats['errors']} errors"
            )
        else:
            logger.debug("No old diagnostic files to clean up")
    except Exception as e:
        logger.error(f"Error cleaning up diagnostics: {e}", exc_info=True)


def recover_pending_timeout_tasks_job():
    """
    ИСПРАВЛЕНО: Watchdog для зависших pending задач.
    
    Задачи в статусе "pending" дольше 20 минут (без прогресса) переводятся в "error" с возвратом средств.
    Это предотвращает ситуации, когда задачи зависают в pending (нет аккаунтов, воркер вырубился и т.п.).
    """
    db: Session = next(get_sync_db())
    try:
        logger.info("Checking for pending timeout tasks...")
        processed_count = recover_pending_timeout_tasks(db, timeout_minutes=20)
        if processed_count > 0:
            logger.info(f"Processed {processed_count} pending timeout task(s)")
        else:
            logger.debug("No pending timeout tasks found")
    except Exception as e:
        logger.error(f"Error recovering pending timeout tasks: {e}", exc_info=True)
    finally:
        db.close()


def scheduler_loop(stop_event: threading.Event | None = None):
    """
    Основной цикл планировщика.

    Если передан `stop_event`, цикл корректно завершится при его установке
    (актуально, когда scheduler запускается потоком внутри backend lifespan).
    """
    logger.info("Scheduler started")

    last_reset_date = None
    last_stuck_check = None
    last_lost_check = None
    last_pending_timeout_check = None
    last_diagnostics_cleanup = None

    def _is_stopped() -> bool:
        return stop_event is not None and stop_event.is_set()

    def _sleep(seconds: int) -> None:
        # Спим короткими интервалами, чтобы быстро реагировать на shutdown
        if stop_event is None:
            time.sleep(seconds)
            return
        for _ in range(seconds):
            if stop_event.is_set():
                return
            time.sleep(1)

    while not _is_stopped():
        try:
            now = datetime.now(timezone.utc)
            current_date = now.date()

            if last_reset_date != current_date and now.hour == 0 and now.minute < 5:
                reset_counters_job()
                last_reset_date = current_date

            if last_stuck_check is None or (now - last_stuck_check).total_seconds() >= 300:
                recover_stuck_tasks_job()
                last_stuck_check = now

            if last_lost_check is None or (now - last_lost_check).total_seconds() >= 600:
                recover_lost_tasks_job()
                last_lost_check = now

            if last_pending_timeout_check is None or (now - last_pending_timeout_check).total_seconds() >= 300:
                recover_pending_timeout_tasks_job()
                last_pending_timeout_check = now

            if last_diagnostics_cleanup is None or (now - last_diagnostics_cleanup).total_seconds() >= 21600:
                cleanup_diagnostics_job()
                last_diagnostics_cleanup = now

            _sleep(60)

        except Exception as e:
            logger.error(f"Scheduler error: {e}", exc_info=True)
            _sleep(60)

    logger.info("Scheduler loop stopped")


if __name__ == "__main__":
    logging.basicConfig(
        level=logging.INFO,
        format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
    )
    scheduler_loop()

