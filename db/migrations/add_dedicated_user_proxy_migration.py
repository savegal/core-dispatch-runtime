"""Add per-user dedicated proxy routing for shared-account orders."""
from __future__ import annotations

import sqlite3
from pathlib import Path

from config.settings import get_settings


def _sqlite_path() -> Path:
    db_url = str(get_settings().DB_URL or "")
    if not db_url.startswith("sqlite:///"):
        raise FileNotFoundError("dedicated proxy migration currently supports SQLite only")
    raw_path = db_url.replace("sqlite:///", "", 1)
    path = Path(raw_path)
    if not path.is_absolute():
        path = Path(__file__).resolve().parents[2] / path
    if not path.exists():
        raise FileNotFoundError(f"database not found: {path}")
    return path


def migrate(verbose: bool = False) -> bool:
    path = _sqlite_path()
    conn = sqlite3.connect(str(path))
    changed = False
    try:
        cursor = conn.cursor()
        cursor.execute("PRAGMA table_info(proxies)")
        columns = {row[1] for row in cursor.fetchall()}
        if "dedicated_user_id" not in columns:
            cursor.execute(
                "ALTER TABLE proxies ADD COLUMN dedicated_user_id INTEGER "
                "REFERENCES users(id) ON DELETE SET NULL"
            )
            changed = True

        cursor.execute(
            "CREATE UNIQUE INDEX IF NOT EXISTS ix_proxies_dedicated_user_id "
            "ON proxies(dedicated_user_id)"
        )
        conn.commit()
        if verbose and changed:
            print("[OK] Added proxies.dedicated_user_id")
        return changed
    finally:
        conn.close()


if __name__ == "__main__":
    migrate(verbose=True)
