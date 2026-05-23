"""SQLite migrations for TeletanSoft.

Each migration is a standalone module exposing a top-level `migrate()` function
that is idempotent (safe to run multiple times). Migrations can be applied via
`apply_pending_migrations()` on worker startup or run individually as scripts.
"""
from __future__ import annotations

import importlib
import logging
import pkgutil
from pathlib import Path
from typing import List

logger = logging.getLogger("db.migrations")

# Migration modules in this package, in the order they should run.
# Order matters: earlier migrations should be safe prerequisites for later ones.
MIGRATION_MODULES: List[str] = [
    "add_task_kind_migration",
    "add_user_video_notifications_enabled_migration",
    "add_user_full_access_override_migration",
    "add_monitor_scheduler_migration",
    "add_adaptive_monitoring_migration",
]


def _accepts_verbose(fn) -> bool:
    """Check via inspect whether migrate() accepts a `verbose` kwarg.
    Avoids triggering noisy tracebacks from a bad-signature TypeError fallback.
    """
    import inspect
    try:
        sig = inspect.signature(fn)
        return "verbose" in sig.parameters or any(
            p.kind == inspect.Parameter.VAR_KEYWORD for p in sig.parameters.values()
        )
    except (TypeError, ValueError):
        return False


def apply_pending_migrations(verbose: bool = False) -> int:
    """
    Run all migrations in order. Each is idempotent — already-applied
    migrations are no-ops. Returns count of migrations that changed something.

    Errors in any single migration are logged but do not abort the worker.
    """
    applied = 0
    for mod_name in MIGRATION_MODULES:
        full_name = f"db.migrations.{mod_name}"
        try:
            module = importlib.import_module(full_name)
            migrate_fn = getattr(module, "migrate", None)
            if not callable(migrate_fn):
                logger.warning("[MIGRATIONS] %s has no migrate() function, skipping", mod_name)
                continue

            # Inspect signature — avoids TypeError-fallback printing tracebacks.
            if _accepts_verbose(migrate_fn):
                result = migrate_fn(verbose=verbose)
            else:
                result = migrate_fn()

            if result:
                applied += 1
                logger.info("[MIGRATIONS] Applied: %s", mod_name)
            else:
                logger.debug("[MIGRATIONS] Already current: %s", mod_name)
        except FileNotFoundError as e:
            logger.warning("[MIGRATIONS] Skipping %s (DB missing): %s", mod_name, e)
        except Exception as e:
            # Non-fatal: log message only (no stacktrace) — avoids noisy startup
            # for pre-existing broken migrations not under our control.
            logger.warning("[MIGRATIONS] %s skipped (non-fatal): %s", mod_name, e)
    return applied
