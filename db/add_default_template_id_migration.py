"""
Миграция: Добавление поля default_template_id в таблицу creator_sources.

УЛУЧШЕНО: Позволяет использовать один главный шаблон для авто-комментирования.
"""
import logging
from sqlalchemy import text
from db.database import get_sync_db

logger = logging.getLogger(__name__)


def migrate():
    """Выполняет миграцию."""
    import sys
    print("[MIGRATION] Starting migration for default_template_id...")
    db = next(get_sync_db())
    try:
        # Для SQLite используем другой способ проверки
        try:
            db.execute(text("SELECT default_template_id FROM creator_sources LIMIT 1"))
            print("[MIGRATION] Column default_template_id already exists, skipping migration")
            logger.info("Column default_template_id already exists, skipping migration")
            return
        except Exception as e:
            print(f"[MIGRATION] Column does not exist yet: {e}")
            pass  # Колонка не существует, продолжаем
        
        # Добавляем колонку
        print("[MIGRATION] Adding default_template_id column to creator_sources...")
        logger.info("Adding default_template_id column to creator_sources...")
        # SQLite не поддерживает FOREIGN KEY в ALTER TABLE напрямую, добавляем просто INTEGER
        db.execute(text("""
            ALTER TABLE creator_sources 
            ADD COLUMN default_template_id INTEGER
        """))
        
        # Создаем индекс для производительности
        print("[MIGRATION] Creating index for default_template_id...")
        logger.info("Creating index for default_template_id...")
        db.execute(text("""
            CREATE INDEX IF NOT EXISTS idx_creator_default_template 
            ON creator_sources(default_template_id)
        """))
        
        db.commit()
        print("[MIGRATION] Migration completed successfully")
        logger.info("Migration completed successfully")
        
    except Exception as e:
        db.rollback()
        logger.error(f"Migration failed: {e}", exc_info=True)
        raise
    finally:
        db.close()


if __name__ == "__main__":
    migrate()
