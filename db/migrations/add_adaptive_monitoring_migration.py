"""
Миграция: Адаптивный мониторинг для MonitoredBlogger.

Добавляет в monitored_bloggers:
  - posting_stats              (JSON, nullable)         — рассчитанный профиль
  - priority_mode              (VARCHAR, default 'auto') — auto|vip|lazy|paused
  - empty_streak               (INTEGER, default 0)      — счётчик подряд пустых проверок
  - override_min_interval      (INTEGER, nullable)       — per-blogger минимум (мин)
  - override_max_interval      (INTEGER, nullable)       — per-blogger максимум (мин)
  - notes                      (TEXT, nullable)          — заметка админа
  - latency_ema_minutes        (FLOAT, nullable)         — EMA задержки обнаружения постов (мин)
  - last_detection_latency_min (INTEGER, nullable)       — задержка обнаружения последнего поста
  - total_videos_caught        (INTEGER, default 0)      — счётчик пойманных видео (всё время)
  - total_late_catches         (INTEGER, default 0)      — счётчик опозданий

Создаёт/обновляет таблицу blogger_check_log с индексом (blogger_id, checked_at).
В check_log добавляет detection_latency_min для трекинга feedback loop.

Запуск:
    python db/migrations/add_adaptive_monitoring_migration.py

Идемпотентная: безопасно запускать несколько раз.
Также вызывается автоматически на старте video_monitor_worker.
"""
from __future__ import annotations

import sqlite3
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))


def _resolve_db_path() -> Path:
    """Находит dev.db в корне проекта или текущем каталоге."""
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


def _table_exists(cursor: sqlite3.Cursor, table_name: str) -> bool:
    cursor.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name=?",
        (table_name,),
    )
    return cursor.fetchone() is not None


def migrate(verbose: bool = True) -> bool:
    """
    Применяет миграцию. Возвращает True если что-то изменилось,
    False если всё уже актуально.
    """
    db_path = _resolve_db_path()
    conn = sqlite3.connect(str(db_path))
    cursor = conn.cursor()
    changed = False

    def log(msg: str) -> None:
        if verbose:
            print(msg)

    try:
        if not _table_exists(cursor, "monitored_bloggers"):
            log("[SKIP] Table monitored_bloggers does not exist yet — nothing to migrate.")
            return False

        # ── monitored_bloggers: добавляем 6 колонок ─────────────────────────────
        existing = _column_names(cursor, "monitored_bloggers")

        column_specs: list[tuple[str, str, str | None]] = [
            ("posting_stats",              "JSON",                                              None),
            ("priority_mode",              "VARCHAR(20) NOT NULL DEFAULT 'auto'",               "auto"),
            ("empty_streak",               "INTEGER NOT NULL DEFAULT 0",                        "0"),
            ("override_min_interval",      "INTEGER",                                           None),
            ("override_max_interval",      "INTEGER",                                           None),
            ("notes",                      "TEXT",                                              None),
            ("latency_ema_minutes",        "REAL",                                              None),
            ("last_detection_latency_min", "INTEGER",                                           None),
            ("total_videos_caught",        "INTEGER NOT NULL DEFAULT 0",                        "0"),
            ("total_late_catches",         "INTEGER NOT NULL DEFAULT 0",                        "0"),
        ]

        for col_name, col_ddl, fill_value in column_specs:
            if col_name in existing:
                continue
            stmt = f"ALTER TABLE monitored_bloggers ADD COLUMN {col_name} {col_ddl}"
            log(f"[+] {stmt}")
            cursor.execute(stmt)
            changed = True
            if fill_value is not None:
                cursor.execute(
                    f"UPDATE monitored_bloggers SET {col_name} = ? WHERE {col_name} IS NULL",
                    (fill_value,),
                )

        # ── blogger_check_log: создаём таблицу + индекс ─────────────────────────
        if not _table_exists(cursor, "blogger_check_log"):
            log("[+] CREATE TABLE blogger_check_log")
            cursor.execute("""
                CREATE TABLE blogger_check_log (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    blogger_id INTEGER NOT NULL,
                    checked_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    found_new_count INTEGER NOT NULL DEFAULT 0,
                    interval_used_minutes INTEGER,
                    next_interval_minutes INTEGER,
                    status VARCHAR(20),
                    adaptive_status VARCHAR(20),
                    reason TEXT,
                    duration_ms INTEGER,
                    detection_latency_min INTEGER,
                    FOREIGN KEY (blogger_id) REFERENCES monitored_bloggers(id) ON DELETE CASCADE
                )
            """)
            cursor.execute(
                "CREATE INDEX idx_check_log_blogger_checked "
                "ON blogger_check_log (blogger_id, checked_at)"
            )
            changed = True
        else:
            check_log_cols = _column_names(cursor, "blogger_check_log")
            if "detection_latency_min" not in check_log_cols:
                log("[+] ALTER TABLE blogger_check_log ADD COLUMN detection_latency_min INTEGER")
                cursor.execute(
                    "ALTER TABLE blogger_check_log ADD COLUMN detection_latency_min INTEGER"
                )
                changed = True

        conn.commit()
        if changed:
            log("[OK] Adaptive monitoring migration applied.")
        else:
            log("[OK] Already up-to-date, nothing applied.")
        return changed

    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


if __name__ == "__main__":
    migrate()
