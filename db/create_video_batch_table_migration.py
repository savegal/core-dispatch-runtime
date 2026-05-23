"""
Миграция: Создание таблицы video_batches для хранения batch'ей видео.

P0 FIX: Решает проблему превышения лимита Telegram (64 байта на callback_data).
"""
import logging
from sqlalchemy import text
from db.database import get_sync_db

logger = logging.getLogger(__name__)


def migrate():
    """Выполняет миграцию."""
    db = next(get_sync_db())
    try:
        # Создаем таблицу video_batches
        logger.info("Creating video_batches table...")
        db.execute(text("""
            CREATE TABLE IF NOT EXISTS video_batches (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                creator_id INTEGER NOT NULL,
                video_ids TEXT NOT NULL,  -- JSON массив
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                expires_at TIMESTAMP,
                FOREIGN KEY (creator_id) REFERENCES creator_sources(id) ON DELETE CASCADE
            )
        """))
        
        # Создаем индексы
        logger.info("Creating indexes for video_batches...")
        db.execute(text("""
            CREATE INDEX IF NOT EXISTS idx_video_batch_creator_created 
            ON video_batches(creator_id, created_at)
        """))
        
        db.execute(text("""
            CREATE INDEX IF NOT EXISTS idx_video_batch_expires 
            ON video_batches(expires_at)
        """))
        
        db.commit()
        logger.info("Migration completed successfully")
        
    except Exception as e:
        db.rollback()
        logger.error(f"Migration failed: {e}", exc_info=True)
        raise
    finally:
        db.close()


if __name__ == "__main__":
    migrate()
