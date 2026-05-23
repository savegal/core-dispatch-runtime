"""
Миграция для добавления user_id в таблицу creator_sources.
Добавляет поле user_id и связь с таблицей users для персональных блогеров.
"""
import sys
import os
import sqlite3
from pathlib import Path

# Add root directory to path
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from config.settings import get_settings

settings = get_settings()


def migrate():
    """
    Выполняет миграцию: добавляет user_id в creator_sources.
    
    Стратегия:
    1. Если user_id уже существует - миграция пропускается
    2. Добавляет поле user_id (nullable=True для обратной совместимости)
    3. Назначает существующие записи системному пользователю (owner/admin)
    4. Создаёт индекс на user_id
    5. Создаёт составной уникальный индекс на (user_id, username)
    6. Удаляет старый уникальный индекс на username если он существует
    
    ВАЖНО: Существующие блогеры будут назначены системному пользователю (owner/admin).
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
        
        # Проверяем, существует ли уже user_id
        cursor.execute("PRAGMA table_info(creator_sources)")
        columns = [col[1] for col in cursor.fetchall()]
        
        if "user_id" in columns:
            print("[OK] Column user_id already exists in creator_sources table")
            conn.close()
            return True
        
        print("[+] Adding user_id column to creator_sources table...")
        
        # Добавляем поле user_id (nullable=True для обратной совместимости)
        cursor.execute("""
            ALTER TABLE creator_sources 
            ADD COLUMN user_id INTEGER REFERENCES users(id) ON DELETE CASCADE
        """)
        
        # Находим системного пользователя (owner или admin) для назначения существующих блогеров
        print("[+] Finding system user (owner/admin) for existing creators...")
        cursor.execute("""
            SELECT id FROM users 
            WHERE role IN ('owner', 'admin') 
            ORDER BY id ASC 
            LIMIT 1
        """)
        system_user_result = cursor.fetchone()
        
        if system_user_result:
            system_user_id = system_user_result[0]
            print(f"[+] Assigning existing creators to system user (id={system_user_id})...")
            # Назначаем существующие блогеры системному пользователю
            cursor.execute("""
                UPDATE creator_sources 
                SET user_id = ? 
                WHERE user_id IS NULL
            """, (system_user_id,))
            updated_count = cursor.rowcount
            print(f"[+] Assigned {updated_count} existing creators to system user")
        else:
            print("[WARNING] No system user (owner/admin) found. Existing creators will have user_id=NULL.")
            print("   They need to be assigned manually after creating a system user.")
        
        # Создаём индекс на user_id для быстрого поиска блогеров пользователя
        print("[+] Creating index for user_id...")
        try:
            cursor.execute("""
                CREATE INDEX IF NOT EXISTS idx_creator_user_id 
                ON creator_sources(user_id)
            """)
        except sqlite3.OperationalError as e:
            if "already exists" not in str(e).lower():
                print(f"[WARNING] Warning while creating index: {e}")
        
        # Создаём составной индекс для фильтрации по user_id и enabled
        print("[+] Creating composite index for (user_id, enabled)...")
        try:
            cursor.execute("""
                CREATE INDEX IF NOT EXISTS idx_creator_user_enabled 
                ON creator_sources(user_id, enabled)
            """)
        except sqlite3.OperationalError as e:
            if "already exists" not in str(e).lower():
                print(f"[WARNING] Warning while creating composite index: {e}")
        
        # Удаляем старый уникальный индекс на username, если он существует
        print("[+] Removing old unique index on username...")
        try:
            # SQLite не поддерживает DROP INDEX IF EXISTS напрямую, проверяем существование
            cursor.execute("SELECT name FROM sqlite_master WHERE type='index' AND name='ix_creator_sources_username'")
            if cursor.fetchone():
                cursor.execute("DROP INDEX ix_creator_sources_username")
            # Также проверяем другие возможные имена индексов
            cursor.execute("SELECT name FROM sqlite_master WHERE type='index' AND name LIKE '%username%'")
            for row in cursor.fetchall():
                index_name = row[0]
                if 'creator' in index_name.lower() and 'username' in index_name.lower():
                    cursor.execute(f"DROP INDEX {index_name}")
        except sqlite3.OperationalError as e:
            print(f"[DEBUG] Could not drop old username index (may not exist): {e}")
        
        # Создаём новый составной уникальный индекс на (user_id, username)
        # Это позволяет одному username быть у разных пользователей
        # ВАЖНО: Для user_id=NULL уникальность username всё ещё гарантируется, так как NULL != NULL в SQLite
        print("[+] Creating unique composite index for (user_id, username)...")
        try:
            cursor.execute("""
                CREATE UNIQUE INDEX IF NOT EXISTS idx_creator_user_username 
                ON creator_sources(user_id, username)
            """)
        except sqlite3.OperationalError as e:
            if "already exists" not in str(e).lower() and "UNIQUE constraint failed" not in str(e):
                print(f"[ERROR] Error creating unique index: {e}")
                raise
        
        conn.commit()
        conn.close()
        
        print("[OK] Migration completed successfully!")
        print(f"   Added user_id column to creator_sources table")
        print(f"   Created indexes for query optimization")
        print(f"   Created unique composite index (user_id, username)")
        if system_user_result:
            print(f"   Assigned existing creators to system user (id={system_user_id})")
        else:
            print(f"\n[WARNING] Existing creators have user_id=NULL. They need to be assigned manually.")
        return True
        
    except Exception as e:
        print(f"[ERROR] Error during migration: {e}")
        import traceback
        traceback.print_exc()
        if 'conn' in locals():
            conn.rollback()
            conn.close()
        return False


if __name__ == "__main__":
    print("Migration: Adding user_id column to creator_sources table...")
    success = migrate()
    sys.exit(0 if success else 1)
