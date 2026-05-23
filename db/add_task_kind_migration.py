"""
Миграция: Добавление колонки kind в таблицу tasks.

Эта миграция добавляет колонку kind для поддержки типов заказов:
- "full" - полный заказ (main comment + replies + likes)
- "engagement_only" - только engagement (replies + likes на существующий комментарий)
"""
import sys
import os

# Add root directory to path
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import sqlite3
from pathlib import Path
from config.settings import get_settings

settings = get_settings()


def migrate():
    """
    Добавляет колонку kind в таблицу tasks если её нет.
    """
    db_url = settings.DB_URL
    
    if not db_url.startswith("sqlite:///"):
        print(f"[WARNING] Migration supports SQLite only. Current DB_URL: {db_url}")
        print("   For PostgreSQL use ALTER TABLE manually or Alembic migrations.")
        return False
    
    # Extract database file path
    db_path = db_url.replace("sqlite:///", "")
    if not Path(db_path).exists():
        print(f"[ERROR] Database file not found: {db_path}")
        return False
    
    try:
        conn = sqlite3.connect(db_path)
        cursor = conn.cursor()
        
        # Check if kind column exists
        cursor.execute("PRAGMA table_info(tasks)")
        columns = [col[1] for col in cursor.fetchall()]
        
        if "kind" in columns:
            print("[OK] Column kind already exists in tasks table")
            conn.close()
            return True
        
        # Add column
        print("[+] Adding kind column to tasks table...")
        cursor.execute("""
            ALTER TABLE tasks 
            ADD COLUMN kind VARCHAR(20) DEFAULT 'full' NOT NULL
        """)
        
        conn.commit()
        conn.close()
        
        print("[OK] Migration completed successfully!")
        print(f"   Added kind column to tasks table")
        return True
        
    except Exception as e:
        print(f"[ERROR] Error during migration: {e}")
        return False


if __name__ == "__main__":
    print("Migration: Adding kind column to tasks table...")
    success = migrate()
    exit(0 if success else 1)
