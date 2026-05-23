"""
Миграция: Исправление unique constraint на CreatorVideo.video_id

P0 FIX: Убираем global unique, добавляем composite unique (creator_id, video_id)
Это позволяет разным подпискам иметь одно и то же видео.
"""
import logging
from sqlalchemy import text
from db.database import get_sync_db

logger = logging.getLogger(__name__)


def migrate():
    """Выполняет миграцию."""
    db = next(get_sync_db())
    try:
        # 1. Удаляем старый unique constraint на video_id
        logger.info("Dropping old unique constraint on video_id...")
        dialect = db.bind.dialect.name if db.bind is not None else ""
        if dialect == "sqlite":
            index_rows = db.execute(text("PRAGMA index_list(creator_videos)")).fetchall()
            for row in index_rows:
                index_name = row[1]
                is_unique = bool(row[2])
                if not is_unique or str(index_name).startswith("sqlite_autoindex"):
                    continue
                columns = [
                    info_row[2]
                    for info_row in db.execute(text(f'PRAGMA index_info("{index_name}")')).fetchall()
                ]
                if columns == ["video_id"]:
                    logger.info("Dropping old unique index %s on creator_videos.video_id", index_name)
                    db.execute(text(f'DROP INDEX "{index_name}"'))
        else:
            for index_name in ("creator_videos_video_id_key", "ix_creator_videos_video_id"):
                try:
                    db.execute(text(f"DROP INDEX IF EXISTS {index_name}"))
                except Exception as e:
                    logger.warning(f"Could not drop old index {index_name} (might not exist): {e}")

        # Оставляем обычный индекс на video_id для быстрых поисков без глобальной уникальности.
        db.execute(text("""
            CREATE INDEX IF NOT EXISTS ix_creator_videos_video_id
            ON creator_videos(video_id)
        """))
        
        # 2. Создаем composite unique index
        logger.info("Creating composite unique index on (creator_id, video_id)...")
        db.execute(text("""
            CREATE UNIQUE INDEX IF NOT EXISTS idx_creator_video_unique 
            ON creator_videos(creator_id, video_id)
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
