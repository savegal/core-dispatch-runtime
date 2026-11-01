"""Persist the account's hardware seed (idempotent)."""
from sqlalchemy import inspect, text
from db.database import sync_engine
import hashlib
from types import SimpleNamespace


def migrate(verbose: bool = False) -> bool:
    with sync_engine.begin() as connection:
        columns = {c["name"] for c in inspect(connection).get_columns("accounts")}
        changed = "device_seed" not in columns
        if changed:
            connection.execute(text("ALTER TABLE accounts ADD COLUMN device_seed VARCHAR(64)"))
        # Freeze existing accounts before a worker can discover a new UID or
        # rotate cookies. Preserve their original deterministic hardware hash.
        cookies_column = "cookies" if "cookies" in columns else "NULL AS cookies"
        rows = connection.execute(text(
            f"SELECT id, label, {cookies_column} FROM accounts WHERE device_seed IS NULL OR device_seed = ''"
        )).mappings().all()
        if rows:
            from executor.device_profiles import _initial_account_seed_key
            for row in rows:
                seed = hashlib.sha256(_initial_account_seed_key(SimpleNamespace(**row)).encode()).hexdigest()
                connection.execute(text("UPDATE accounts SET device_seed = :seed WHERE id = :id"), {"seed": seed, "id": row["id"]})
            changed = True
    return changed
