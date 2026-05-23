"""
Миграция: Добавление поля kind в таблицу tasks.

Поле kind определяет тип заказа:
- "full" - полный заказ (главный комментарий + replies + likes)
- "engagement_only" - только replies + likes на существующий комментарий
"""
from sqlalchemy import text
from db.database import sync_engine as engine  # use sync engine for raw migration
import logging

logger = logging.getLogger("migration")


def migrate():
    """Добавляет поле kind в таблицу tasks."""
    try:
        with engine.connect() as conn:
            # Проверяем, существует ли поле
            result = conn.execute(text("""
                SELECT COUNT(*) FROM pragma_table_info('tasks') WHERE name='kind'
            """))
            exists = result.scalar() > 0
            
            if exists:
                logger.info("Field 'kind' already exists in tasks table, skipping migration")
                return False
            
            # Добавляем поле
            conn.execute(text("""
                ALTER TABLE tasks ADD COLUMN kind VARCHAR(20) DEFAULT 'full' NOT NULL
            """))
            conn.commit()
            logger.info("✅ Migration completed: Added 'kind' field to tasks table")
            return True
            
    except Exception as e:
        logger.error(f"❌ Migration failed: {e}", exc_info=True)
        raise


if __name__ == "__main__":
    migrate()
