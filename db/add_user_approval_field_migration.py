"""
Миграция: добавляет колонку users.was_approved_once.

Зачем:
- Чтобы отличать "новый работник, ждёт одобрения админа" от
  "был активен и заблокирован за нарушения".
- Бот по этому флагу показывает разные тексты ("Ожидайте одобрения" vs
  "Вы заблокированы администратором").
- API users.unblock после первого успешного одобрения выставляет это в True.

Безопасность:
- Если колонка уже есть — миграция ничего не делает.
- Существующим неблокированным пользователям выставляется True (они уже
  считаются "одобренными"), чтобы их повторная сессия не показывала экран
  ожидания.
- Существующим заблокированным — оставляем False по умолчанию: админ либо
  разблокирует и тем самым одобрит, либо оставит как есть.

Запуск:
    python db/add_user_approval_field_migration.py
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
        cursor.execute("PRAGMA table_info(users)")
        columns = [col[1] for col in cursor.fetchall()]

        applied = 0

        if "was_approved_once" not in columns:
            cursor.execute(
                "ALTER TABLE users ADD COLUMN was_approved_once BOOLEAN DEFAULT 0 NOT NULL"
            )
            print("[+] Added column users.was_approved_once")
            # Все, кто уже работает без блокировки — считаются одобренными.
            # Это особенно важно для текущих admin/owner и активных юзеров,
            # у которых лежат заказы и баланс.
            cursor.execute(
                "UPDATE users SET was_approved_once = 1 WHERE is_blocked = 0"
            )
            print(f"[+] Marked existing non-blocked users as approved: {cursor.rowcount} rows")
            applied += 1
        else:
            print("[=] Column users.was_approved_once already exists, skipping")

        conn.commit()

        if applied:
            print("\n[OK] Migration completed.")
        else:
            print("\n[OK] Nothing to apply.")

    except Exception as e:
        conn.rollback()
        print(f"[ERROR] Migration error: {e}")
        raise
    finally:
        conn.close()


if __name__ == "__main__":
    print("Running migration: users.was_approved_once...")
    run_migration()
    print("[OK] Done!")
