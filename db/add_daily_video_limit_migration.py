"""
Миграция: Добавление полей для суточного лимита видео в таблицу users.

Запуск:
    python db/add_daily_video_limit_migration.py
"""

import sqlite3
import sys
from pathlib import Path

# Добавляем корневую директорию в путь
sys.path.insert(0, str(Path(__file__).parent.parent))

def run_migration():
    """Добавляет колонки для суточного лимита видео."""
    # Ищем базу данных
    db_path = Path("dev.db")
    if not db_path.exists():
        # Пробуем найти в текущей директории
        db_path = Path(__file__).parent.parent / "dev.db"
    
    if not db_path.exists():
        print("[ERROR] Database not found. Run: python db/create_all.py")
        return
    
    conn = sqlite3.connect(str(db_path))
    cursor = conn.cursor()
    
    try:
        # Проверяем существование колонок
        cursor.execute("PRAGMA table_info(users)")
        columns = [col[1] for col in cursor.fetchall()]
        
        migrations = []
        
        # Добавляем today_videos если нет
        if 'today_videos' not in columns:
            migrations.append("ALTER TABLE users ADD COLUMN today_videos INTEGER DEFAULT 0 NOT NULL")
            print("[+] Adding column: today_videos")
        
        # Добавляем last_video_date если нет
        if 'last_video_date' not in columns:
            migrations.append("ALTER TABLE users ADD COLUMN last_video_date DATETIME")
            print("[+] Adding column: last_video_date")
        
        # Добавляем daily_video_limit если нет
        if 'daily_video_limit' not in columns:
            migrations.append("ALTER TABLE users ADD COLUMN daily_video_limit INTEGER DEFAULT 50 NOT NULL")
            print("[+] Adding column: daily_video_limit")
        
        # Выполняем миграции
        for migration in migrations:
            cursor.execute(migration)
        
        conn.commit()
        
        if migrations:
            print(f"\n[OK] Migration completed! Added {len(migrations)} columns.")
        else:
            print("\n[OK] All columns already exist. Migration not needed.")
            
    except Exception as e:
        conn.rollback()
        print(f"[ERROR] Migration error: {e}")
        raise
    finally:
        conn.close()

if __name__ == "__main__":
    print("Running migration: adding daily video limit fields...")
    run_migration()
    print("[OK] Done!")

