"""
Миграция: добавляет колонку updated_at в таблицу accounts (если её нет).
"""
import sqlite3
import sys
from pathlib import Path

try:
    # Добавляем корневую директорию в путь для импорта
    import os
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    from config.settings import get_settings
    settings = get_settings()
    db_url = settings.DB_URL
    if db_url.startswith("sqlite:///"):
        db_path = db_url.replace("sqlite:///", "")
    else:
        print(f"❌ Unsupported DB_URL format: {db_url}")
        print("Этот скрипт работает только с SQLite")
        sys.exit(1)
except Exception as e:
    print(f"⚠️  Warning: Could not load settings: {e}")
    print("Using default database path: dev.db")
    db_path = Path(__file__).parent.parent / "dev.db"


def add_column_if_not_exists(cursor, table_name, column_name, column_type, default_value=None, on_update=None):
    """Добавляет колонку в таблицу, если её нет."""
    cursor.execute(f"PRAGMA table_info({table_name})")
    columns = [row[1] for row in cursor.fetchall()]
    
    if column_name not in columns:
        print(f"➕ Adding column: {table_name}.{column_name}")
        
        # Формируем SQL для добавления колонки
        sql = f"ALTER TABLE {table_name} ADD COLUMN {column_name} {column_type}"
        
        # SQLite не поддерживает DEFAULT для TIMESTAMP с функцией в ALTER TABLE
        # Поэтому просто добавляем колонку, а затем обновляем существующие записи
        cursor.execute(sql)
        
        # Если есть default_value, обновляем существующие записи
        if default_value:
            try:
                cursor.execute(f"UPDATE {table_name} SET {column_name} = {default_value} WHERE {column_name} IS NULL")
            except Exception as e:
                print(f"⚠️  Warning: Could not set default value: {e}")
        
        return True
    else:
        print(f"✅ Column {table_name}.{column_name} already exists.")
        return False


def main():
    db_file = Path(db_path)
    if not db_file.exists():
        print(f"❌ Database not found: {db_file}")
        print("Create database tables using: python db/create_all.py")
        sys.exit(1)

    print(f"📂 Database: {db_file}")
    print(f"🔧 Adding missing columns...\n")

    conn = sqlite3.connect(str(db_file))
    cursor = conn.cursor()

    try:
        # Добавляем updated_at в accounts (DATETIME, может быть NULL, обновляется при изменении)
        added = add_column_if_not_exists(
            cursor, 
            "accounts", 
            "updated_at", 
            "DATETIME"
        )
        
        # Если колонка была добавлена, обновляем существующие записи
        if added:
            # Устанавливаем updated_at = created_at для существующих записей
            cursor.execute("""
                UPDATE accounts 
                SET updated_at = created_at 
                WHERE updated_at IS NULL AND created_at IS NOT NULL
            """)
            # Если created_at тоже NULL, устанавливаем текущее время
            cursor.execute("""
                UPDATE accounts 
                SET updated_at = CURRENT_TIMESTAMP 
                WHERE updated_at IS NULL
            """)
            print("✅ Updated existing records with current timestamp")
        
        conn.commit()
        print("\n✅ Migration completed successfully!")
        
    except Exception as e:
        print(f"❌ Error during migration: {e}")
        import traceback
        traceback.print_exc()
        conn.rollback()
        sys.exit(1)
    finally:
        conn.close()


if __name__ == "__main__":
    main()

