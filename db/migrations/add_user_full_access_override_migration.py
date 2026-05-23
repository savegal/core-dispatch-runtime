"""Migration: add users.full_access_override.

This flag is a per-worker bypass for the global parsing-only mode. It does not
grant admin rights and is separate from is_blocked.
"""
from sqlalchemy import text
from db.database import sync_engine as engine
import logging

logger = logging.getLogger("migration")


def migrate():
    """Adds users.full_access_override if it is missing."""
    try:
        with engine.connect() as conn:
            result = conn.execute(text("""
                SELECT COUNT(*) FROM pragma_table_info('users') WHERE name='full_access_override'
            """))
            exists = result.scalar() > 0
            if exists:
                logger.info("Field 'full_access_override' already exists in users table, skipping migration")
                return False

            conn.execute(text("""
                ALTER TABLE users ADD COLUMN full_access_override BOOLEAN DEFAULT 0 NOT NULL
            """))
            conn.commit()
            logger.info("Migration completed: Added users.full_access_override")
            return True
    except Exception as e:
        logger.error(f"Migration failed: {e}", exc_info=True)
        raise


if __name__ == "__main__":
    migrate()
