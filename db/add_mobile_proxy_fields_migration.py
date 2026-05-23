"""
Миграция для добавления полей rotation_url и is_mobile в таблицу proxies.
"""
import sys
import sqlite3
from pathlib import Path

# Добавляем корневую директорию в путь
sys.path.insert(0, str(Path(__file__).parent.parent))


def run_migration():
    """Добавляет поля rotation_url и is_mobile в таблицу proxies."""
    # Ищем базу данных
    db_path = Path("dev.db")
    if not db_path.exists():
        db_path = Path(__file__).parent.parent / "dev.db"
    
    if not db_path.exists():
        print("[ERROR] Database not found. Run: python db/create_all.py")
        return False
    
    conn = sqlite3.connect(str(db_path))
    cursor = conn.cursor()
    
    print("[*] Starting migration: Add mobile proxy fields to proxies table")
    
    try:
        # Проверяем существующие колонки
        cursor.execute("PRAGMA table_info(proxies)")
        columns = [col[1] for col in cursor.fetchall()]
        
        # Добавляем rotation_url
        if 'rotation_url' not in columns:
            cursor.execute("ALTER TABLE proxies ADD COLUMN rotation_url TEXT")
            print("[+] Adding column: rotation_url")
        else:
            print("[!] Column rotation_url already exists, skipping")
        
        # Добавляем is_mobile
        if 'is_mobile' not in columns:
            # SQLite не поддерживает DEFAULT с NOT NULL в ALTER TABLE, добавляем без NOT NULL
            cursor.execute("ALTER TABLE proxies ADD COLUMN is_mobile VARCHAR(1)")
            # Устанавливаем значение по умолчанию для существующих записей
            cursor.execute("UPDATE proxies SET is_mobile = 'N' WHERE is_mobile IS NULL")
            print("[+] Adding column: is_mobile")
        else:
            print("[!] Column is_mobile already exists, skipping")
        
        # Создаём индекс для is_mobile (если ещё нет)
        cursor.execute("SELECT name FROM sqlite_master WHERE type='index' AND name='idx_proxy_is_mobile'")
        if not cursor.fetchone():
            cursor.execute("CREATE INDEX idx_proxy_is_mobile ON proxies(is_mobile)")
            print("[+] Creating index: idx_proxy_is_mobile")
        else:
            print("[!] Index idx_proxy_is_mobile already exists, skipping")
        
        conn.commit()
        print("[OK] Migration completed! Added rotation_url and is_mobile columns.")
        return True
        
    except Exception as e:
        conn.rollback()
        print(f"[ERROR] Migration failed: {e}")
        import traceback
        traceback.print_exc()
        return False
    finally:
        conn.close()


if __name__ == "__main__":
    success = run_migration()
    sys.exit(0 if success else 1)

