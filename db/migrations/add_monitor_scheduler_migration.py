"""
Миграция: DB-side scheduler и retryable side effects для мониторинга.

Добавляет:
- monitored_bloggers.last_attempt_at
- monitored_bloggers.last_success_at
- monitored_bloggers.next_check_at
- monitored_bloggers.lock_owner
- таблицу monitor_outbox

Запуск:
    python db/migrations/add_monitor_scheduler_migration.py
"""
from __future__ import annotations

import sqlite3
import sys
from pathlib import Path


sys.path.insert(0, str(Path(__file__).resolve().parents[2]))


def _resolve_db_path() -> Path:
    candidates = [
        Path("dev.db"),
        Path(__file__).resolve().parents[2] / "dev.db",
    ]
    for candidate in candidates:
        if candidate.exists():
            return candidate
    raise FileNotFoundError("Database not found. Run from project root or create dev.db first.")


def _column_names(cursor: sqlite3.Cursor, table_name: str) -> set[str]:
    cursor.execute(f"PRAGMA table_info({table_name})")
    return {row[1] for row in cursor.fetchall()}


def _index_exists(cursor: sqlite3.Cursor, index_name: str) -> bool:
    cursor.execute(
        "SELECT name FROM sqlite_master WHERE type='index' AND name=?",
        (index_name,),
    )
    return cursor.fetchone() is not None


def migrate() -> None:
    db_path = _resolve_db_path()
    conn = sqlite3.connect(str(db_path))
    cursor = conn.cursor()

    try:
        blogger_columns = _column_names(cursor, "monitored_bloggers")
        blogger_migrations: list[str] = []

        if "last_attempt_at" not in blogger_columns:
            blogger_migrations.append("ALTER TABLE monitored_bloggers ADD COLUMN last_attempt_at DATETIME")
        if "last_success_at" not in blogger_columns:
            blogger_migrations.append("ALTER TABLE monitored_bloggers ADD COLUMN last_success_at DATETIME")
        if "next_check_at" not in blogger_columns:
            blogger_migrations.append("ALTER TABLE monitored_bloggers ADD COLUMN next_check_at DATETIME")
        if "lock_owner" not in blogger_columns:
            blogger_migrations.append("ALTER TABLE monitored_bloggers ADD COLUMN lock_owner VARCHAR(120)")

        for statement in blogger_migrations:
            print(f"[+] {statement}")
            cursor.execute(statement)

        if "last_attempt_at" not in blogger_columns:
            cursor.execute(
                "UPDATE monitored_bloggers SET last_attempt_at = last_checked_at "
                "WHERE last_checked_at IS NOT NULL AND last_attempt_at IS NULL"
            )
        if "last_success_at" not in blogger_columns:
            cursor.execute(
                "UPDATE monitored_bloggers SET last_success_at = last_checked_at "
                "WHERE last_checked_at IS NOT NULL AND last_success_at IS NULL"
            )
        if "next_check_at" not in blogger_columns:
            cursor.execute(
                "UPDATE monitored_bloggers SET next_check_at = last_checked_at "
                "WHERE last_checked_at IS NOT NULL AND next_check_at IS NULL"
            )

        if not _index_exists(cursor, "idx_monitored_blogger_due_lock"):
            cursor.execute(
                "CREATE INDEX idx_monitored_blogger_due_lock "
                "ON monitored_bloggers(next_check_at, lock_until)"
            )
        if not _index_exists(cursor, "idx_monitored_blogger_success_due"):
            cursor.execute(
                "CREATE INDEX idx_monitored_blogger_success_due "
                "ON monitored_bloggers(last_success_at, next_check_at)"
            )

        cursor.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='monitor_outbox'")
        if not cursor.fetchone():
            print("[+] Creating table monitor_outbox")
            cursor.execute(
                """
                CREATE TABLE monitor_outbox (
                    id INTEGER PRIMARY KEY,
                    event_type VARCHAR(50) NOT NULL,
                    status VARCHAR(20) NOT NULL DEFAULT 'pending',
                    dedupe_key VARCHAR(255) UNIQUE,
                    blogger_id INTEGER,
                    creator_id INTEGER,
                    payload JSON NOT NULL,
                    attempts INTEGER NOT NULL DEFAULT 0,
                    available_at DATETIME,
                    locked_at DATETIME,
                    lock_owner VARCHAR(120),
                    last_error TEXT,
                    created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
                    updated_at DATETIME DEFAULT CURRENT_TIMESTAMP,
                    FOREIGN KEY (blogger_id) REFERENCES monitored_bloggers(id) ON DELETE CASCADE,
                    FOREIGN KEY (creator_id) REFERENCES creator_sources(id) ON DELETE CASCADE
                )
                """
            )

        for index_name, ddl in [
            (
                "idx_monitor_outbox_status_available",
                "CREATE INDEX idx_monitor_outbox_status_available "
                "ON monitor_outbox(status, available_at)",
            ),
            (
                "idx_monitor_outbox_type_status",
                "CREATE INDEX idx_monitor_outbox_type_status "
                "ON monitor_outbox(event_type, status)",
            ),
        ]:
            if not _index_exists(cursor, index_name):
                cursor.execute(ddl)

        conn.commit()
        print("[OK] Monitor scheduler migration completed")
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


if __name__ == "__main__":
    migrate()
