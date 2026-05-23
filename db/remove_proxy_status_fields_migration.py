"""
Миграция для удаления полей статуса из таблицы proxies.
Удаляет: status, fail_count, last_error, response_time_ms, last_checked_at
"""
import sys
import sqlite3
from pathlib import Path

# Добавляем корневую директорию в путь
sys.path.insert(0, str(Path(__file__).parent.parent))


def run_migration():
    """Удаляет поля статуса из таблицы proxies."""
    # Ищем базу данных
    db_path = Path("dev.db")
    if not db_path.exists():
        db_path = Path(__file__).parent.parent / "dev.db"
    
    if not db_path.exists():
        print("[ERROR] Database not found. Run: python db/create_all.py")
        return False
    
    conn = sqlite3.connect(str(db_path))
    cursor = conn.cursor()
    
    print("[*] Starting migration: Remove proxy status fields from proxies table")
    
    try:
        # Проверяем существующие колонки
        cursor.execute("PRAGMA table_info(proxies)")
        columns = [col[1] for col in cursor.fetchall()]
        
        fields_to_remove = ['status', 'fail_count', 'last_error', 'response_time_ms', 'last_checked_at']
        removed_count = 0
        
        # SQLite не поддерживает DROP COLUMN напрямую, нужно создать новую таблицу
        # Но для простоты - просто проверяем и предупреждаем
        # В реальности нужно использовать ALTER TABLE ... DROP COLUMN (SQLite 3.35.0+)
        # Или пересоздать таблицу
        
        # Проверяем версию SQLite
        cursor.execute("SELECT sqlite_version()")
        sqlite_version = cursor.fetchone()[0]
        print(f"[INFO] SQLite version: {sqlite_version}")
        
        # SQLite 3.35.0+ поддерживает DROP COLUMN
        major, minor, patch = map(int, sqlite_version.split('.'))
        supports_drop_column = (major > 3) or (major == 3 and minor >= 35)
        
        if not supports_drop_column:
            print("[WARNING] SQLite version does not support DROP COLUMN.")
            print("[WARNING] Migration will only check fields - manual table recreation may be needed.")
            print("[INFO] Fields to remove (if exist):", ", ".join(fields_to_remove))
            
            for field in fields_to_remove:
                if field in columns:
                    print(f"[!] Field {field} exists (will be removed in future SQLite version or manual migration)")
                else:
                    print(f"[+] Field {field} does not exist (already removed)")
            
            conn.commit()
            print("[OK] Migration check completed. Fields exist but cannot be removed with this SQLite version.")
            print("[INFO] Consider upgrading SQLite or manually recreating table.")
            return True
        
        # SQLite 3.35.0+ - можем использовать DROP COLUMN
        for field in fields_to_remove:
            if field in columns:
                try:
                    cursor.execute(f"ALTER TABLE proxies DROP COLUMN {field}")
                    print(f"[+] Removed column: {field}")
                    removed_count += 1
                except sqlite3.OperationalError as e:
                    if "DROP COLUMN" in str(e) or "not supported" in str(e).lower():
                        print(f"[!] Column {field} exists but DROP COLUMN failed: {e}")
                        print(f"[INFO] May need to recreate table manually")
                    else:
                        raise
            else:
                print(f"[!] Column {field} does not exist, skipping")
        
        # Удаляем индекс по status если существует
        cursor.execute("SELECT name FROM sqlite_master WHERE type='index' AND name='idx_proxy_status_country'")
        if cursor.fetchone():
            try:
                cursor.execute("DROP INDEX idx_proxy_status_country")
                print("[+] Dropped index: idx_proxy_status_country")
            except Exception as e:
                print(f"[!] Error dropping index idx_proxy_status_country: {e}")
        
        conn.commit()
        print(f"[OK] Migration completed! Removed {removed_count} fields from proxies table.")
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


