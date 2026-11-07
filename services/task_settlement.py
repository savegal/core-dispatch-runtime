"""Settle the unused part of an order's original hold under its task lock."""
from decimal import Decimal, ROUND_HALF_UP

from sqlalchemy import func, select, update
from sqlalchemy.orm import Session

from db.models import Task, Transaction, User
from services.balance_service import calculate_refund_amount, get_pricing, is_admin, refund_balance
from services.task_execution_ledger import has_submission_evidence, url_result_counts_as_success

REFUND_PENDING_KEY = "_refund_settlement_pending"

_NO_WORK_ERRORS = {
    "no_accounts_available", "no_auto_parent_accounts", "no_free_accounts",
    "no_free_main_accounts", "no_free_reply_accounts", "no_main_accounts_available",
    "no_reply_accounts_available", "stopped_by_user", "task_timeout",
}


def lock_task_for_settlement(db: Session, task_id: int, *criteria) -> Task | None:
    filters = (Task.id == task_id, *criteria)
    with db.no_autoflush:
        if db.get_bind(mapper=Task).dialect.name == "sqlite":
            # SQLite ignores FOR UPDATE. Acquire its write lock before reading.
            db.execute(update(Task).where(*filters).values(updated_at=Task.updated_at),
                       execution_options={"synchronize_session": False})
        return db.scalar(select(Task).where(*filters).with_for_update()
                         .execution_options(populate_existing=True))


def _unused_url_counts(task: Task, *, legacy_progress: bool) -> tuple[int, int]:
    urls = task.video_urls or []
    results = (task.settings or {}).get("url_results") or {}
    # Pre-ledger orders cannot distinguish successful URLs from their old cursor.
    # Preserve credit for the processed prefix only when it is the sole record.
    if legacy_progress and not results:
        return max(0, len(urls) - max(0, task.progress or 0)), 0

    untouched = failed = 0
    for url in urls:
        row = results.get(url)
        if url_result_counts_as_success(row):
            continue
        if not isinstance(row, dict) or not row:
            untouched += 1
            continue
        reason = str(row.get("error") or "")
        never_started = reason in _NO_WORK_ERRORS or (
            reason == "no_main_published" and row.get("execution_attempts") == []
        )
        if never_started and not has_submission_evidence(row):
            untouched += 1
        else:
            failed += 1
    return untouched, failed


def settle_task_refund(db: Session, task: Task, *, legacy_progress: bool = False) -> None:
    """Credit any outstanding refund. The caller commits it with the task state."""
    task_settings = dict(task.settings or {})
    if REFUND_PENDING_KEY in task_settings:
        task_settings.pop(REFUND_PENDING_KEY)
        task.settings = task_settings

    paid = Decimal(str(task.total_cost or 0))
    url_count = len(task.video_urls or [])
    if paid <= 0 or not url_count:
        return
    untouched, failed = _unused_url_counts(task, legacy_progress=legacy_progress)
    if not (untouched or failed):
        return
    user = db.get(User, task.user_id)
    if user is None or is_admin(user):
        return

    unit_value = paid / url_count
    failure_refund = calculate_refund_amount(failed, get_pricing(db, commit=False)) if failed else Decimal(0)
    target = (unit_value * untouched + min(failure_refund, unit_value * failed)).quantize(
        Decimal("0.01"), rounding=ROUND_HALF_UP,
    )
    recorded = db.scalar(select(func.sum(Transaction.amount)).where(
        Transaction.task_id == task.id, Transaction.type == "refund",
    ))
    already_refunded = max(Decimal(str(task.refunded_cost or 0)), Decimal(str(recorded or 0)))
    outstanding = min(target, paid) - already_refunded
    if outstanding > 0:
        if not refund_balance(db, task.user_id, outstanding, task_id=task.id,
                              description=f"Refund for {untouched} unused and {failed} failed URL(s)",
                              commit=False):
            raise RuntimeError("Refund settlement failed")
        already_refunded += outstanding
    task.refunded_cost = already_refunded
