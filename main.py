# ИСПРАВЛЕНО: Устанавливаем ProactorEventLoopPolicy ДО всех импортов
# Это критично для Windows, чтобы Playwright мог работать в API endpoints
# На Linux это не выполнится (условная компиляция)
import sys
if sys.platform == 'win32':
    import asyncio
    import warnings
    # Устанавливаем политику ДО создания любого event loop
    asyncio.set_event_loop_policy(asyncio.WindowsProactorEventLoopPolicy())
    
    # ИСПРАВЛЕНО: Подавляем предупреждения о "Task was destroyed but it is pending"
    # Это известная проблема Playwright на Windows с ProactorEventLoopPolicy
    # Фоновые задачи Playwright Connection не успевают завершиться до закрытия event loop
    # Эти ошибки не критичны и не влияют на функциональность
    def suppress_playwright_task_warnings():
        """Подавляет предупреждения о незавершенных задачах Playwright."""
        import logging
        asyncio_logger = logging.getLogger('asyncio')
        # Устанавливаем уровень WARNING вместо ERROR для asyncio
        # Это скроет "Task was destroyed but it is pending" ошибки
        original_error = asyncio_logger.error
        
        def filtered_error(msg, *args, **kwargs):
            # Пропускаем сообщения о незавершенных задачах Playwright
            if isinstance(msg, str) and 'Task was destroyed but it is pending' in msg:
                # Логируем как debug вместо error
                asyncio_logger.debug(msg, *args, **kwargs)
            else:
                original_error(msg, *args, **kwargs)
        
        asyncio_logger.error = filtered_error
    
    suppress_playwright_task_warnings()

import logging
import threading
import asyncio
import sqlite3
from contextlib import asynccontextmanager
from pathlib import Path
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from slowapi import Limiter, _rate_limit_exceeded_handler
from slowapi.util import get_remote_address
from slowapi.errors import RateLimitExceeded
from api import users, accounts, tasks, executor_api, templates, proxies, stats, geo_recommendations, geo_stats, geo_coverage, pricing, app_settings, video_parsing, video_batch, video_inbox, adaptive_monitoring, personal_accounts
from api.autopilot import router as autopilot_router
from config.settings import get_settings
from config.sentry_config import init_sentry
from services.redis_service import close_redis_client

settings = get_settings()
logger = logging.getLogger("main")
_is_production = str(getattr(settings, "ENV", "")).lower() == "production"

# ИСПРАВЛЕНО: Импортируем limiter из отдельного модуля для избежания циклических импортов
from config.limiter import limiter

# Глобальный флаг для graceful shutdown (делится с executor при отдельном запуске)
shutdown_event = threading.Event()

# Внутренний scheduler-поток (дневной reset счётчиков + watchdog зависших задач)
_scheduler_stop_event = threading.Event()
_scheduler_thread: threading.Thread | None = None


def _resolve_sqlite_path_from_url(db_url: str) -> Path | None:
    if not db_url.startswith("sqlite:///"):
        return None

    raw_path = db_url.replace("sqlite:///", "", 1)
    sqlite_path = Path(raw_path)
    if not sqlite_path.is_absolute():
        sqlite_path = Path(__file__).resolve().parent / sqlite_path
    return sqlite_path


def _sqlite_column_names(cursor: sqlite3.Cursor, table_name: str) -> set[str]:
    cursor.execute(f"PRAGMA table_info({table_name})")
    return {row[1] for row in cursor.fetchall()}


def ensure_local_sqlite_monitor_schema() -> None:
    """
    Applies small compatibility migrations automatically for local SQLite setups.

    WHY: the project ships schema-changing code, but local dev.db may lag behind
    and break API routes with `no such column` before the monitor worker runs
    the regular migration pass.
    """
    sqlite_path = _resolve_sqlite_path_from_url(settings.DB_URL)
    if sqlite_path is None or not sqlite_path.exists():
        return

    conn = sqlite3.connect(str(sqlite_path))
    try:
        cursor = conn.cursor()
        cursor.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='users'"
        )
        if cursor.fetchone():
            user_columns = _sqlite_column_names(cursor, "users")
            applied = 0
            if "video_notifications_enabled" not in user_columns:
                cursor.execute(
                    "ALTER TABLE users ADD COLUMN video_notifications_enabled BOOLEAN NOT NULL DEFAULT 1"
                )
                applied += 1
            if "full_access_override" not in user_columns:
                cursor.execute(
                    "ALTER TABLE users ADD COLUMN full_access_override BOOLEAN NOT NULL DEFAULT 0"
                )
                applied += 1
        else:
            applied = 0

        cursor.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='monitored_bloggers'"
        )
        if not cursor.fetchone():
            conn.commit()
            if applied:
                logger.info("Applied local SQLite monitor schema migration automatically")
            return

        columns = _sqlite_column_names(cursor, "monitored_bloggers")
        required_columns = {
            "last_attempt_at":      "ALTER TABLE monitored_bloggers ADD COLUMN last_attempt_at DATETIME",
            "last_success_at":      "ALTER TABLE monitored_bloggers ADD COLUMN last_success_at DATETIME",
            "next_check_at":        "ALTER TABLE monitored_bloggers ADD COLUMN next_check_at DATETIME",
            "lock_owner":           "ALTER TABLE monitored_bloggers ADD COLUMN lock_owner VARCHAR(120)",
            "posting_stats":        "ALTER TABLE monitored_bloggers ADD COLUMN posting_stats JSON",
            "priority_mode":        "ALTER TABLE monitored_bloggers ADD COLUMN priority_mode VARCHAR(20) NOT NULL DEFAULT 'auto'",
            "empty_streak":         "ALTER TABLE monitored_bloggers ADD COLUMN empty_streak INTEGER NOT NULL DEFAULT 0",
            "override_min_interval":"ALTER TABLE monitored_bloggers ADD COLUMN override_min_interval INTEGER",
            "override_max_interval":"ALTER TABLE monitored_bloggers ADD COLUMN override_max_interval INTEGER",
            "notes":                "ALTER TABLE monitored_bloggers ADD COLUMN notes TEXT",
            "latency_ema_minutes":  "ALTER TABLE monitored_bloggers ADD COLUMN latency_ema_minutes REAL",
            "last_detection_latency_min": "ALTER TABLE monitored_bloggers ADD COLUMN last_detection_latency_min INTEGER",
            "total_videos_caught":  "ALTER TABLE monitored_bloggers ADD COLUMN total_videos_caught INTEGER NOT NULL DEFAULT 0",
            "total_late_catches":   "ALTER TABLE monitored_bloggers ADD COLUMN total_late_catches INTEGER NOT NULL DEFAULT 0",
        }

        for column_name, ddl in required_columns.items():
            if column_name not in columns:
                cursor.execute(ddl)
                applied += 1

        if applied:
            cursor.execute(
                "UPDATE monitored_bloggers SET last_attempt_at = last_checked_at "
                "WHERE last_checked_at IS NOT NULL AND last_attempt_at IS NULL"
            )
            cursor.execute(
                "UPDATE monitored_bloggers SET last_success_at = last_checked_at "
                "WHERE last_checked_at IS NOT NULL AND last_success_at IS NULL"
            )
            cursor.execute(
                "UPDATE monitored_bloggers SET next_check_at = last_checked_at "
                "WHERE last_checked_at IS NOT NULL AND next_check_at IS NULL"
            )
            cursor.execute(
                "UPDATE monitored_bloggers SET priority_mode = 'auto' WHERE priority_mode IS NULL"
            )

        cursor.execute(
            "SELECT name FROM sqlite_master WHERE type='index' AND name='idx_monitored_blogger_due_lock'"
        )
        if not cursor.fetchone():
            cursor.execute(
                "CREATE INDEX idx_monitored_blogger_due_lock "
                "ON monitored_bloggers(next_check_at, lock_until)"
            )

        cursor.execute(
            "SELECT name FROM sqlite_master WHERE type='index' AND name='idx_monitored_blogger_success_due'"
        )
        if not cursor.fetchone():
            cursor.execute(
                "CREATE INDEX idx_monitored_blogger_success_due "
                "ON monitored_bloggers(last_success_at, next_check_at)"
            )

        cursor.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='monitor_outbox'"
        )
        if not cursor.fetchone():
            cursor.execute(
                """
                CREATE TABLE monitor_outbox (
                    id INTEGER PRIMARY KEY,
                    event_type VARCHAR(50) NOT NULL,
                    status VARCHAR(20) NOT NULL DEFAULT 'pending',
                    dedupe_key VARCHAR(255) UNIQUE,
                    blogger_id INTEGER,
                    creator_id INTEGER,
                    payload JSON NOT NULL,
                    attempts INTEGER NOT NULL DEFAULT 0,
                    available_at DATETIME,
                    locked_at DATETIME,
                    lock_owner VARCHAR(120),
                    last_error TEXT,
                    created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
                    updated_at DATETIME DEFAULT CURRENT_TIMESTAMP,
                    FOREIGN KEY (blogger_id) REFERENCES monitored_bloggers(id) ON DELETE CASCADE,
                    FOREIGN KEY (creator_id) REFERENCES creator_sources(id) ON DELETE CASCADE
                )
                """
            )

        cursor.execute(
            "SELECT name FROM sqlite_master WHERE type='index' AND name='idx_monitor_outbox_status_available'"
        )
        if not cursor.fetchone():
            cursor.execute(
                "CREATE INDEX idx_monitor_outbox_status_available "
                "ON monitor_outbox(status, available_at)"
            )

        cursor.execute(
            "SELECT name FROM sqlite_master WHERE type='index' AND name='idx_monitor_outbox_type_status'"
        )
        if not cursor.fetchone():
            cursor.execute(
                "CREATE INDEX idx_monitor_outbox_type_status "
                "ON monitor_outbox(event_type, status)"
            )

        # blogger_check_log table
        cursor.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='blogger_check_log'"
        )
        if not cursor.fetchone():
            cursor.execute(
                """
                CREATE TABLE blogger_check_log (
                    id INTEGER PRIMARY KEY,
                    blogger_id INTEGER NOT NULL REFERENCES monitored_bloggers(id) ON DELETE CASCADE,
                    checked_at DATETIME DEFAULT CURRENT_TIMESTAMP NOT NULL,
                    found_new_count INTEGER NOT NULL DEFAULT 0,
                    interval_used_minutes INTEGER,
                    next_interval_minutes INTEGER,
                    status VARCHAR(20),
                    adaptive_status VARCHAR(20),
                    reason TEXT,
                    duration_ms INTEGER,
                    detection_latency_min INTEGER
                )
                """
            )
            cursor.execute(
                "CREATE INDEX idx_check_log_blogger_checked ON blogger_check_log(blogger_id, checked_at)"
            )

        blogger_check_log_columns = _sqlite_column_names(cursor, "blogger_check_log")
        if "detection_latency_min" not in blogger_check_log_columns:
            cursor.execute(
                "ALTER TABLE blogger_check_log ADD COLUMN detection_latency_min INTEGER"
            )

        conn.commit()
        if applied:
            logger.info("Applied local SQLite monitor schema migration automatically")
    finally:
        conn.close()


def signal_handler(signum, frame):
    """
    Совместимый обработчик сигналов (оставлен для обратной совместимости).

    ВНИМАНИЕ: больше НЕ регистрируется через signal.signal() в lifespan.
    Раньше мы перезаписывали обработчик SIGINT после установки uvicorn —
    из-за этого Ctrl+C ставил наш флаг, но не сообщал uvicorn'у о том,
    что нужно завершиться, и сервер «зависал» (uvicorn ждал свой
    Server.should_exit, который никем не выставлялся).

    Теперь полностью полагаемся на штатный механизм uvicorn —
    он сам корректно обработает SIGINT/SIGTERM и вызовет lifespan
    shutdown-секцию, в которой мы выставляем shutdown_event ниже.
    """
    logger.info(f"Received signal {signum} (legacy path), setting shutdown_event")
    shutdown_event.set()


@asynccontextmanager
async def lifespan(app: FastAPI):
    """
    Управление жизненным циклом приложения.
    Executor больше не запускается внутри API-процесса (отдельный сервис).
    """
    # ИСПРАВЛЕНО: Устанавливаем ProactorEventLoopPolicy в lifespan (только для Windows)
    if sys.platform == 'win32':
        try:
            loop = asyncio.get_running_loop()
            if not isinstance(loop, asyncio.ProactorEventLoop):
                asyncio.set_event_loop_policy(asyncio.WindowsProactorEventLoopPolicy())
                logger.info("Windows ProactorEventLoopPolicy установлена в lifespan")
        except RuntimeError:
            asyncio.set_event_loop_policy(asyncio.WindowsProactorEventLoopPolicy())
            logger.info("Windows ProactorEventLoopPolicy установлена в lifespan (до создания loop)")
    
    # ВАЖНО: НЕ регистрируем здесь сигналы.
    # uvicorn сам ставит обработчики SIGINT/SIGTERM до запуска lifespan
    # и корректно отрабатывает Ctrl+C (вызовет нашу shutdown-секцию ниже).
    # Раньше тут было signal.signal(SIGINT, signal_handler) — это перезаписывало
    # uvicorn'овский обработчик и ломало Ctrl+C: сервер «зависал», окно
    # консоли не реагировало.
    logger.info("Lifespan startup: relying on uvicorn signal handlers")

    try:
        ensure_local_sqlite_monitor_schema()
    except Exception as exc:
        logger.error(f"Failed to apply local SQLite monitor migration: {exc}")
        raise
    
    # КРИТИЧНО: В runtime не создаем/не меняем схему БД.
    # Все изменения структуры должны применяться только миграциями до старта приложения.
    logger.info("Skipping runtime schema creation; expecting database migrations to be applied")
    
    # КРИТИЧНО: Сбрасываем legacy-статусы задач при старте (running/queued -> pending)
    # Это предотвращает "зомби-задачи" после рестарта и выравнивает статусы со state machine.
    try:
        from db.database import get_db
        from db.models import Task
        from sqlalchemy import update
        
        # Выполняем сброс задач синхронно через async генератор
        async for db in get_db():
            try:
                # Сбрасываем legacy-статусы в pending
                result = await db.execute(
                    update(Task).where(Task.status.in_(["running", "queued"])).values(status="pending")
                )
                await db.commit()
                reset_count = result.rowcount
                if reset_count > 0:
                    logger.info(f"🔄 Reset {reset_count} legacy task(s) from running/queued to pending")
                else:
                    logger.debug("No stuck tasks found (all tasks are in valid status)")
            except Exception as e:
                logger.warning(f"Could not reset stuck tasks: {e}")
                await db.rollback()
            finally:
                break  # Используем только первую итерацию
    except Exception as e:
        logger.warning(f"Could not reset stuck tasks: {e}")

    # Восстанавливаем задачи, которые не попали в очередь (Redis был недоступен)
    try:
        from services.task_service import recover_pending_enqueues_async
        async for db in get_db():
            try:
                recovered = await recover_pending_enqueues_async(db)
                if recovered:
                    logger.info(f"Recovered {recovered} task(s) from pending_enqueue")
            finally:
                break
    except Exception as e:
        logger.warning(f"Could not recover pending enqueues: {e}")

    # Запускаем scheduler в фоновом потоке.
    # WHY: без него `today_main_comments` / `today_replies` никогда не сбрасываются,
    # и аккаунты, упёршиеся в MAX_ACTIONS_PER_ACCOUNT, навсегда выпадают из выборки.
    # Также он крутит watchdog зависших задач (recover_stuck_tasks / recover_pending_timeout_tasks).
    global _scheduler_thread
    try:
        from executor.scheduler import scheduler_loop

        def _supervised_scheduler_loop(stop_event: threading.Event) -> None:
            """Auto-restart wrapper: если scheduler_loop упал — перезапустим через backoff.

            Без этого один баг внутри scheduler_loop мог навсегда выключать счётчики
            и watchdog, и аккаунты постепенно выпадали из ротации без явных сигналов.
            """
            backoff = 5
            while not stop_event.is_set():
                try:
                    scheduler_loop(stop_event)
                    # Чистый выход — stop_event сработал, выходим окончательно.
                    logger.info("Background scheduler loop exited cleanly")
                    return
                except Exception as crash:
                    logger.error(
                        "Background scheduler crashed (%s) — restarting in %ds",
                        crash, backoff, exc_info=True,
                    )
                    if stop_event.wait(backoff):
                        return
                    # Экспоненциальный backoff с потолком 5 минут
                    backoff = min(backoff * 2, 300)

        _scheduler_stop_event.clear()
        _scheduler_thread = threading.Thread(
            target=_supervised_scheduler_loop,
            args=(_scheduler_stop_event,),
            name="BackendScheduler",
            daemon=True,
        )
        _scheduler_thread.start()
        logger.info("Background scheduler thread started (auto-restart on crash, daily counter reset + stuck task watchdog)")
    except Exception as e:
        logger.error(f"Failed to start background scheduler: {e}", exc_info=True)

    yield  # Тут работает приложение
    
    # ИСПРАВЛЕНО: ПРИ ОСТАНОВКЕ - корректное завершение всех компонентов
    logger.info("🛑 SHUTTING DOWN application gracefully...")
    
    # Устанавливаем флаг shutdown для остановки executor
    shutdown_event.set()

    # Останавливаем фоновый scheduler-поток
    try:
        _scheduler_stop_event.set()
        if _scheduler_thread is not None and _scheduler_thread.is_alive():
            _scheduler_thread.join(timeout=5)
            logger.info("Background scheduler thread stopped")
    except Exception as e:
        logger.warning(f"Error stopping background scheduler: {e}")

    # Закрываем Redis соединения
    try:
        close_redis_client()
        logger.info("Redis connections closed")
    except Exception as e:
        logger.warning(f"Error closing Redis connections: {e}")

    logger.info("✅ Shutdown complete")


app = FastAPI(
    title="TikTok Comment Service API",
    description="Private TikTok comment service",
    version="1.0.0",
    lifespan=lifespan,  # КРИТИЧНО: Запускает executor при старте
    docs_url=None if _is_production else "/docs",
    redoc_url=None if _is_production else "/redoc",
    openapi_url=None if _is_production else "/openapi.json",
)

# ИСПРАВЛЕНО: Применяем rate limiter к приложению
app.state.limiter = limiter
app.add_exception_handler(RateLimitExceeded, _rate_limit_exceeded_handler)

app.include_router(users.router)
app.include_router(accounts.router)
app.include_router(tasks.router)
app.include_router(executor_api.router)
app.include_router(templates.router)
app.include_router(proxies.router)
app.include_router(stats.router)
app.include_router(geo_recommendations.router)
app.include_router(geo_stats.router)
app.include_router(geo_coverage.router)
app.include_router(pricing.router)
app.include_router(app_settings.router)
app.include_router(autopilot_router)
app.include_router(video_parsing.router)
app.include_router(video_batch.router)  # P0 FIX: Router для batch'ей видео
app.include_router(video_inbox.router)  # УЛУЧШЕНО: Router для Inbox
app.include_router(adaptive_monitoring.router)
app.include_router(personal_accounts.router)  # Личные аккаунты работника


@app.get("/health")
async def health_check():
    """
    ИСПРАВЛЕНО: Расширенный health check для всех компонентов системы.
    Проверяет доступность БД, Redis и других критических сервисов.
    """
    from sqlalchemy import text
    from db.database import engine
    from services.redis_service import (
        get_redis_client,
        get_executor_heartbeat,
        get_monitor_heartbeat,
    )
    
    health_status = {
        "status": "ok",
        "timestamp": asyncio.get_event_loop().time(),
        "checks": {}
    }
    
    # Проверка базы данных
    try:
        async with engine.begin() as conn:
            await conn.execute(text("SELECT 1"))
        health_status["checks"]["database"] = {"status": "ok"}
    except Exception as e:
        logger.warning(f"Health check database error: {e}")
        health_status["checks"]["database"] = {"status": "error"}
        health_status["status"] = "degraded"
    
    # Проверка Redis
    try:
        redis_client = get_redis_client()
        redis_client.ping()
        health_status["checks"]["redis"] = {"status": "ok"}
    except Exception as e:
        logger.warning(f"Health check redis error: {e}")
        health_status["checks"]["redis"] = {"status": "error"}
        health_status["status"] = "degraded"
    
    # Проверка executor heartbeat (отдельный сервис)
    alive, info = get_executor_heartbeat()
    if alive:
        health_status["checks"]["executor"] = {"status": "ok"}
    else:
        health_status["checks"]["executor"] = {"status": "error"}
        health_status["status"] = "degraded"

    # Проверка video monitor heartbeat
    monitor_alive, monitor_info = get_monitor_heartbeat()
    if monitor_alive:
        health_status["checks"]["video_monitor"] = {"status": "ok"}
    else:
        health_status["checks"]["video_monitor"] = {
            "status": "error",
        }
        health_status["status"] = "degraded"
    
    # Если есть ошибки, возвращаем 503
    if health_status["status"] == "degraded":
        return JSONResponse(status_code=503, content=health_status)
    
    return health_status


@app.get("/")
def root():
    return {"message": "TikTok Comment Service API"}
