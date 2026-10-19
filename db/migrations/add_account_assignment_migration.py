"""Persist fair account scheduling and validation order (idempotent SQLite migration)."""
import sqlite3
from pathlib import Path
from config.settings import get_settings


def migrate(verbose: bool = False, *, database_path: str | None = None) -> bool:
    if database_path is None:
        url = str(get_settings().DB_URL)
        if not url.startswith("sqlite:///"):
            raise FileNotFoundError("Account assignment migration requires SQLite")
        path = Path(url.removeprefix("sqlite:///"))
        if not path.is_absolute():
            path = Path(__file__).resolve().parents[2] / path
    else:
        path = Path(database_path)
    if not path.is_file():
        raise FileNotFoundError(path)
    additions = {
        "last_assigned_at": "DATETIME",
        "assignment_count": "INTEGER NOT NULL DEFAULT 0",
        "assignment_lease_until": "DATETIME",
        "assignment_lease_token": "VARCHAR(64)",
        "last_validation_attempt_at": "DATETIME",
    }
    changed = False
    with sqlite3.connect(str(path), timeout=30) as db:
        columns = {row[1] for row in db.execute("PRAGMA table_info(accounts)")}
        for name, declaration in additions.items():
            if name not in columns:
                db.execute(f"ALTER TABLE accounts ADD COLUMN {name} {declaration}")
                changed = True
        db.execute("CREATE INDEX IF NOT EXISTS ix_accounts_last_assigned_at ON accounts(last_assigned_at)")
        db.execute("CREATE INDEX IF NOT EXISTS ix_accounts_last_validation_attempt_at ON accounts(last_validation_attempt_at)")
    if verbose:
        print("Account assignment schema updated" if changed else "Account assignment schema already current")
    return changed


if __name__ == "__main__":
    migrate(verbose=True)
