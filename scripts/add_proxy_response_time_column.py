"""
Миграция: добавляет колонку response_time_ms в таблицу proxies.
"""
import sqlite3
import sys
from pathlib import Path

try:
    from config.settings import get_settings
    settings = get_settings()
    db_url = settings.DB_URL
    if db_url.startswith("sqlite:///"):
        db_path = db_url.replace("sqlite:///", "")
    else:
        print(f"❌ Unsupported DB_URL format: {db_url}")
        sys.exit(1)
except ImportError:
    print("⚠️ config.settings not found, using default dev.db path.")
    db_path = Path(__file__).parent.parent / "dev.db"
except Exception as e:
    print(f"❌ Error loading settings: {e}")
    db_path = Path(__file__).parent.parent / "dev.db"


def add_column_if_not_exists(cursor, table_name, column_name, column_type, default_value=None):
    cursor.execute(f"PRAGMA table_info({table_name})")
    columns = [row[1] for row in cursor.fetchall()]
    if column_name not in columns:
        print(f"➕ Adding column: {column_name}")
        if default_value is not None:
            cursor.execute(f"ALTER TABLE {table_name} ADD COLUMN {column_name} {column_type} DEFAULT {default_value}")
        else:
            cursor.execute(f"ALTER TABLE {table_name} ADD COLUMN {column_name} {column_type}")
        return True
    else:
        print(f"Column {column_name} already exists.")
        return False


def main():
    db_file = Path(db_path)
    if not db_file.exists():
        print(f"❌ Database not found: {db_file}")
        print("Create database tables using: python db/create_all.py")
        sys.exit(1)

    conn = sqlite3.connect(str(db_file))
    cursor = conn.cursor()

    print(f"📂 Database: {db_file}")
    print("🔧 Adding missing columns...")

    try:
        # Добавляем response_time_ms
        add_column_if_not_exists(cursor, "proxies", "response_time_ms", "INTEGER")

        conn.commit()
        print("✅ Migration completed successfully!")
    except Exception as e:
        print(f"❌ Error during migration: {e}")
        conn.rollback()
        sys.exit(1)
    finally:
        conn.close()


if __name__ == "__main__":
    main()

