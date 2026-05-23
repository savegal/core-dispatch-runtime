"""
Миграция: добавление недостающих столбцов в таблицу accounts.
Добавляет: last_error, error_count_recent, last_checked_at
"""
import sys
import os

# Добавляем корневую директорию в путь
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from sqlalchemy import create_engine, text
from config.settings import get_settings

settings = get_settings()

def add_missing_columns():
    """Добавляет недостающие столбцы в таблицу accounts."""
    engine = create_engine(
        settings.DB_URL,
        connect_args={"check_same_thread": False} if "sqlite" in settings.DB_URL else {}
    )
    
    with engine.connect() as conn:
        # Проверяем и добавляем столбцы, если их нет
        if "sqlite" in settings.DB_URL:
            # SQLite
            # Проверяем наличие столбцов
            result = conn.execute(text("PRAGMA table_info(accounts)"))
            existing_columns = {row[1] for row in result}
            
            if "last_error" not in existing_columns:
                print("Adding column: last_error")
                conn.execute(text("ALTER TABLE accounts ADD COLUMN last_error TEXT"))
                conn.commit()
            
            if "error_count_recent" not in existing_columns:
                print("Adding column: error_count_recent")
                conn.execute(text("ALTER TABLE accounts ADD COLUMN error_count_recent INTEGER DEFAULT 0"))
                conn.commit()
            
            if "last_checked_at" not in existing_columns:
                print("Adding column: last_checked_at")
                conn.execute(text("ALTER TABLE accounts ADD COLUMN last_checked_at DATETIME"))
                conn.commit()
            
            print("✅ All columns added successfully!")
            
        else:
            # PostgreSQL
            # Проверяем наличие столбцов
            result = conn.execute(text("""
                SELECT column_name 
                FROM information_schema.columns 
                WHERE table_name = 'accounts'
            """))
            existing_columns = {row[0] for row in result}
            
            if "last_error" not in existing_columns:
                print("Adding column: last_error")
                conn.execute(text("ALTER TABLE accounts ADD COLUMN last_error TEXT"))
                conn.commit()
            
            if "error_count_recent" not in existing_columns:
                print("Adding column: error_count_recent")
                conn.execute(text("ALTER TABLE accounts ADD COLUMN error_count_recent INTEGER DEFAULT 0"))
                conn.commit()
            
            if "last_checked_at" not in existing_columns:
                print("Adding column: last_checked_at")
                conn.execute(text("ALTER TABLE accounts ADD COLUMN last_checked_at TIMESTAMP WITH TIME ZONE"))
                conn.commit()
            
            print("✅ All columns added successfully!")


if __name__ == "__main__":
    try:
        add_missing_columns()
    except Exception as e:
        print(f"❌ Error: {e}")
        import traceback
        traceback.print_exc()
        sys.exit(1)

