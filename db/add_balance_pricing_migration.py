"""
Миграция: Добавление системы баланса и цен.

Добавляет:
- balance и is_blocked в таблицу users
- Таблицу pricing для хранения цен
- Поля total_cost, refunded_cost, video_attempts в таблицу tasks
- Удаляет поля today_videos, last_video_date, daily_video_limit из users

Запуск:
    python db/add_balance_pricing_migration.py
"""

import sqlite3
import sys
from pathlib import Path
from decimal import Decimal

# Добавляем корневую директорию в путь
sys.path.insert(0, str(Path(__file__).parent.parent))

def run_migration():
    """Добавляет поля для баланса и цен."""
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
        # Проверяем существование колонок в users
        cursor.execute("PRAGMA table_info(users)")
        columns = [col[1] for col in cursor.fetchall()]
        
        migrations = []
        
        # Добавляем balance если нет
        if 'balance' not in columns:
            migrations.append("ALTER TABLE users ADD COLUMN balance NUMERIC(10, 2) DEFAULT 0.00 NOT NULL")
            print("[+] Adding column: balance")
            # Устанавливаем баланс 0 для всех существующих пользователей
            migrations.append("UPDATE users SET balance = 0.00 WHERE balance IS NULL")
        
        # Добавляем is_blocked если нет
        if 'is_blocked' not in columns:
            migrations.append("ALTER TABLE users ADD COLUMN is_blocked BOOLEAN DEFAULT 0 NOT NULL")
            print("[+] Adding column: is_blocked")
            migrations.append("UPDATE users SET is_blocked = 0 WHERE is_blocked IS NULL")
        
        # Удаляем старые колонки лимитов (если они есть)
        if 'today_videos' in columns:
            print("[!] Column today_videos exists - will be kept for backward compatibility (not removed)")
        if 'last_video_date' in columns:
            print("[!] Column last_video_date exists - will be kept for backward compatibility (not removed)")
        if 'daily_video_limit' in columns:
            print("[!] Column daily_video_limit exists - will be kept for backward compatibility (not removed)")
        
        # Выполняем миграции для users
        for migration in migrations:
            cursor.execute(migration)
        
        # Проверяем существование таблицы pricing
        cursor.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='pricing'")
        if not cursor.fetchone():
            cursor.execute("""
                CREATE TABLE pricing (
                    id INTEGER PRIMARY KEY,
                    price_success NUMERIC(10, 2) DEFAULT 5.00 NOT NULL,
                    price_failure NUMERIC(10, 2) DEFAULT 2.00 NOT NULL,
                    updated_at DATETIME DEFAULT CURRENT_TIMESTAMP
                )
            """)
            print("[+] Creating table: pricing")
            # Создаем запись с ценами по умолчанию
            cursor.execute("""
                INSERT INTO pricing (id, price_success, price_failure) 
                VALUES (1, 5.00, 2.00)
            """)
        
        # Проверяем существование колонок в tasks
        cursor.execute("PRAGMA table_info(tasks)")
        task_columns = [col[1] for col in cursor.fetchall()]
        
        task_migrations = []
        
        # Добавляем total_cost если нет
        if 'total_cost' not in task_columns:
            task_migrations.append("ALTER TABLE tasks ADD COLUMN total_cost NUMERIC(10, 2)")
            print("[+] Adding column: total_cost to tasks")
        
        # Добавляем refunded_cost если нет
        if 'refunded_cost' not in task_columns:
            task_migrations.append("ALTER TABLE tasks ADD COLUMN refunded_cost NUMERIC(10, 2)")
            print("[+] Adding column: refunded_cost to tasks")
        
        # Добавляем video_attempts если нет
        if 'video_attempts' not in task_columns:
            task_migrations.append("ALTER TABLE tasks ADD COLUMN video_attempts TEXT")
            print("[+] Adding column: video_attempts to tasks")
        
        # Выполняем миграции для tasks
        for migration in task_migrations:
            cursor.execute(migration)
        
        # Создаем индекс для is_blocked если его нет
        cursor.execute("SELECT name FROM sqlite_master WHERE type='index' AND name='ix_users_is_blocked'")
        if not cursor.fetchone():
            cursor.execute("CREATE INDEX ix_users_is_blocked ON users(is_blocked)")
            print("[+] Creating index: ix_users_is_blocked")
        
        # Проверяем существование таблицы transactions
        cursor.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='transactions'")
        if not cursor.fetchone():
            cursor.execute("""
                CREATE TABLE transactions (
                    id INTEGER PRIMARY KEY,
                    user_id INTEGER NOT NULL,
                    amount NUMERIC(10, 2) NOT NULL,
                    type VARCHAR(20) NOT NULL,
                    description TEXT,
                    task_id INTEGER,
                    created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
                    FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE,
                    FOREIGN KEY (task_id) REFERENCES tasks(id) ON DELETE SET NULL
                )
            """)
            print("[+] Creating table: transactions")
            
            # Создаем индексы для transactions
            cursor.execute("CREATE INDEX IF NOT EXISTS ix_transactions_user_id ON transactions(user_id)")
            cursor.execute("CREATE INDEX IF NOT EXISTS ix_transactions_type ON transactions(type)")
            cursor.execute("CREATE INDEX IF NOT EXISTS ix_transactions_task_id ON transactions(task_id)")
            cursor.execute("CREATE INDEX IF NOT EXISTS ix_transactions_created_at ON transactions(created_at)")
            print("[+] Creating indexes for transactions table")
        
        conn.commit()
        
        total_migrations = len(migrations) + len(task_migrations)
        if total_migrations > 0:
            print(f"\n[OK] Migration completed! Applied {total_migrations} changes.")
        else:
            print("\n[OK] All changes already exist. Migration not needed.")
            
    except Exception as e:
        conn.rollback()
        print(f"[ERROR] Migration error: {e}")
        raise
    finally:
        conn.close()

if __name__ == "__main__":
    print("Running migration: adding balance and pricing system...")
    run_migration()
    print("[OK] Done!")

