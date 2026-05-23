"""
Миграция: добавляет колонки для системы "личных аккаунтов" работника.

Что добавляется в accounts:
- owner_user_id (Integer, NULLable, FK -> users.id, ON DELETE SET NULL)
    NULL  = аккаунт принадлежит общему пулу (поведение как до фичи).
    int   = "личный" аккаунт конкретного работника. Селекторы выберут его
            только при account_pool_mode="personal" того же работника.
- daily_main_limit (Integer, NULLable)
    NULL  = использовать settings.MAX_ACTIONS_PER_ACCOUNT (как раньше).
    int   = жёсткий per-account лимит на main-комменты в сутки.
- daily_reply_limit (Integer, NULLable)
    NULL  = использовать settings.MAX_ACTIONS_PER_ACCOUNT (как раньше).
    int   = жёсткий per-account лимит на ответы в сутки.

Идемпотентно: повторный запуск не делает ничего.
ВАЖНО: миграция не трогает существующие записи — все уже импортированные
аккаунты остаются в общем пуле (owner_user_id=NULL). Это сохраняет
текущее поведение коментинга для всех заказов, где account_pool_mode не указан
(дефолт "shared").

Запуск:
    python db/add_personal_accounts_migration.py
"""

import sqlite3
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))


def run_migration():
    db_path = Path("dev.db")
    if not db_path.exists():
        db_path = Path(__file__).parent.parent / "dev.db"

    if not db_path.exists():
        print("[ERROR] Database not found. Run: python db/create_all.py")
        return

    conn = sqlite3.connect(str(db_path))
    cursor = conn.cursor()

    try:
        cursor.execute("PRAGMA table_info(accounts)")
        columns = [col[1] for col in cursor.fetchall()]

        applied = 0

        if "owner_user_id" not in columns:
            cursor.execute(
                "ALTER TABLE accounts ADD COLUMN owner_user_id INTEGER NULL"
            )
            print("[+] Added column accounts.owner_user_id")
            applied += 1
        else:
            print("[=] Column accounts.owner_user_id already exists, skipping")

        if "daily_main_limit" not in columns:
            cursor.execute(
                "ALTER TABLE accounts ADD COLUMN daily_main_limit INTEGER NULL"
            )
            print("[+] Added column accounts.daily_main_limit")
            applied += 1
        else:
            print("[=] Column accounts.daily_main_limit already exists, skipping")

        if "daily_reply_limit" not in columns:
            cursor.execute(
                "ALTER TABLE accounts ADD COLUMN daily_reply_limit INTEGER NULL"
            )
            print("[+] Added column accounts.daily_reply_limit")
            applied += 1
        else:
            print("[=] Column accounts.daily_reply_limit already exists, skipping")

        # Индекс по owner_user_id для быстрых выборок личных пулов.
        cursor.execute(
            "SELECT name FROM sqlite_master WHERE type='index' AND name='ix_accounts_owner_user_id'"
        )
        if not cursor.fetchone():
            cursor.execute(
                "CREATE INDEX IF NOT EXISTS ix_accounts_owner_user_id ON accounts(owner_user_id)"
            )
            print("[+] Created index ix_accounts_owner_user_id")
        else:
            print("[=] Index ix_accounts_owner_user_id already exists, skipping")

        conn.commit()

        if applied:
            print(f"\n[OK] Migration completed. Applied {applied} column(s).")
        else:
            print("\n[OK] Nothing to apply.")

    except Exception as e:
        conn.rollback()
        print(f"[ERROR] Migration error: {e}")
        raise
    finally:
        conn.close()


if __name__ == "__main__":
    print("Running migration: personal accounts (owner_user_id + per-account daily limits)...")
    run_migration()
    print("[OK] Done!")
