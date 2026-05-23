"""
SQLite migration: global per-user switch for video monitor notifications.

Run:
    python db/migrations/add_user_video_notifications_enabled_migration.py
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


def migrate(verbose: bool = True) -> bool:
    db_path = _resolve_db_path()
    conn = sqlite3.connect(str(db_path))
    cursor = conn.cursor()

    try:
        cursor.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='users'")
        if not cursor.fetchone():
            if verbose:
                print("[SKIP] users table does not exist yet")
            return False

        columns = _column_names(cursor, "users")
        if "video_notifications_enabled" in columns:
            if verbose:
                print("[OK] users.video_notifications_enabled already exists")
            return False

        cursor.execute(
            "ALTER TABLE users ADD COLUMN video_notifications_enabled BOOLEAN NOT NULL DEFAULT 1"
        )
        conn.commit()
        if verbose:
            print("[OK] Added users.video_notifications_enabled")
        return True
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


if __name__ == "__main__":
    migrate()
