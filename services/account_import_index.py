"""Coherence token for a private, per-import cookie index; no schema changes."""
import re
import threading
from weakref import WeakSet

from sqlalchemy import event, text

_installed = WeakSet()
_lock = threading.Lock()
_account_write = re.compile(
    r'^\s*(?:INSERT(?:\s+OR\s+\w+)?\s+INTO|UPDATE(?:\s+OR\s+\w+)?|DELETE\s+FROM)\s+["`\[]?accounts\b', re.I)


def _track_local_writes(connection, _cursor, statement, _parameters, _context, _many):
    if _account_write.match(statement):
        connection.info["account_import_revision"] = connection.info.get("account_import_revision", 0) + 1


async def account_snapshot(db):
    dialect = db.bind.dialect.name
    if dialect == "sqlite":
        def snapshot(sync):
            engine = sync.get_bind()
            with _lock:
                if engine not in _installed:
                    event.listen(engine, "before_cursor_execute", _track_local_writes)
                    _installed.add(engine)
            connection = sync.connection()
            # data_version sees commits on other connections/processes. The
            # local counter also sees account writes on a reused connection.
            version = connection.exec_driver_sql("PRAGMA data_version").scalar_one()
            return (connection.connection.dbapi_connection, version,
                    connection.info.get("account_import_revision", 0))
        return await db.run_sync(snapshot)
    if dialect == "postgresql":
        # xmin changes even if two account updates share one timestamp. Read
        # only row versions, not thousands of cookie payloads on every batch.
        return (await db.execute(text("SELECT md5(coalesce(string_agg(id::text || ':' || xmin::text, ',' ORDER BY id), '')) FROM accounts"))).scalar_one()
    return None  # Unsupported engines conservatively rebuild the index.
