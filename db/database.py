"""
Database configuration with async SQLAlchemy.

ИСПРАВЛЕНО: Мигрировано на async SQLAlchemy для предотвращения блокировки event loop.
FastAPI и aiogram используют async, поэтому БД тоже должна быть async.
"""
import logging
from sqlalchemy import event
from sqlalchemy.ext.asyncio import create_async_engine, AsyncSession, async_sessionmaker
from sqlalchemy.orm import declarative_base
from config.settings import get_settings

logger = logging.getLogger("db.database")
settings = get_settings()


def _apply_sqlite_pragmas(dbapi_connection, _connection_record) -> None:
    """Per-connection SQLite tuning. Applied on every new connection.

    WAL mode lets the worker write check_log/posting_stats while the bot reads
    the same file without the writer blocking readers. synchronous=NORMAL
    drops fsync from every commit to "at WAL checkpoint" — about 3-5× faster
    on the laptop's SSD for the worker's hot path (~5 commits per blogger
    check × 5 bloggers/tick = 25 fsyncs/tick → 1 group fsync per checkpoint).

    busy_timeout protects against `database is locked` when the bot and worker
    happen to commit at exactly the same moment.
    """
    try:
        cursor = dbapi_connection.cursor()
        cursor.execute("PRAGMA journal_mode=WAL")
        cursor.execute("PRAGMA synchronous=NORMAL")
        cursor.execute("PRAGMA cache_size=-64000")     # 64 MB in-memory page cache
        cursor.execute("PRAGMA temp_store=MEMORY")
        cursor.execute("PRAGMA busy_timeout=5000")     # wait up to 5s before "database is locked"
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.close()
    except Exception as exc:
        logger.warning("Failed to apply SQLite PRAGMAs (non-fatal): %s", exc)

# ИСПРАВЛЕНО: Используем async engine
# Для SQLite нужен aiosqlite драйвер
db_url = settings.DB_URL
if db_url.startswith("sqlite:///"):
    # Заменяем sqlite:/// на sqlite+aiosqlite:///
    db_url = db_url.replace("sqlite:///", "sqlite+aiosqlite:///")
elif db_url.startswith("postgresql://"):
    # Для PostgreSQL используем asyncpg
    db_url = db_url.replace("postgresql://", "postgresql+asyncpg://")
elif db_url.startswith("postgresql+psycopg2://"):
    db_url = db_url.replace("postgresql+psycopg2://", "postgresql+asyncpg://")

engine = create_async_engine(
    db_url,
    echo=settings.DEBUG,
    # Для SQLite нужно отключить check_same_thread, но в async версии это не требуется
    # connect_args={"check_same_thread": False} if "sqlite" in db_url else {}
)

# ИСПРАВЛЕНО: Используем async_sessionmaker вместо sessionmaker
AsyncSessionLocal = async_sessionmaker(
    engine,
    class_=AsyncSession,
    expire_on_commit=False,
    autocommit=False,
    autoflush=False
)

Base = declarative_base()


async def get_db():
    """
    Dependency для получения async сессии БД.
    
    Используется в FastAPI endpoints:
    @router.get("/endpoint")
    async def endpoint(db: AsyncSession = Depends(get_db)):
        ...
    """
    async with AsyncSessionLocal() as session:
        try:
            yield session
        finally:
            # Не коммитим автоматически: транзакционные границы задаются явными commit/rollback в бизнес-слое.
            if session.in_transaction():
                await session.rollback()
            await session.close()


# Для обратной совместимости с executor (он работает в отдельных потоках)
# Создаем синхронную версию для executor и scheduler
from sqlalchemy import create_engine as create_sync_engine
from sqlalchemy.orm import sessionmaker as create_sessionmaker

# Pool sizing notes (laptop, single worker process):
# - MAX_THREADS=2, MAX_CONCURRENT_BROWSERS=1, MONITOR_BLOGGER_CONCURRENCY=2
# - peak realistic in-flight queries: ~6 (worker tick + bot handler + outbox)
# - pool_size=8 + overflow=4 covers spikes with margin while saving ~30 MB RAM
#   that idle pool_size=20 was holding for no reason on this deployment.
sync_engine = create_sync_engine(
    settings.DB_URL,
    connect_args={"check_same_thread": False} if "sqlite" in settings.DB_URL else {},
    echo=settings.DEBUG,
    pool_size=8,
    max_overflow=4,
    pool_timeout=30,
    pool_pre_ping=True,  # Проверка соединений перед использованием
    pool_recycle=3600  # Пересоздание соединений каждые 3600 секунд (1 час)
)

# Apply SQLite PRAGMAs on every new connection in both engines.
# WAL is per-database file, so applying once is enough — but pragmas like
# busy_timeout, cache_size, temp_store are per-connection and need this hook.
if "sqlite" in settings.DB_URL:
    event.listen(sync_engine, "connect", _apply_sqlite_pragmas)
    # async engine uses the underlying sync_engine of the connection pool;
    # listen on its sync counterpart so aiosqlite connections also pick up pragmas.
    event.listen(engine.sync_engine, "connect", _apply_sqlite_pragmas)

SessionLocal = create_sessionmaker(autocommit=False, autoflush=False, bind=sync_engine)


def get_sync_db():
    """
    Синхронная версия get_db для executor и scheduler.
    
    Executor работает в отдельных потоках, поэтому может использовать синхронную БД.
    """
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


# ИСПРАВЛЕНО: Context manager для безопасного использования sync_db в API
from contextlib import contextmanager

@contextmanager
def get_sync_db_context():
    """
    Context manager для безопасного использования sync_db в API endpoints.
    
    Гарантирует закрытие соединения даже при исключениях.
    Используется вместо next(get_sync_db()) для улучшения безопасности.
    
    Пример использования:
        with get_sync_db_context() as db:
            result = sync_function(db)
    """
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()
