"""Persistent round-robin history and token-owned leases across worker processes."""
import logging
import threading
import uuid
from datetime import datetime, timedelta, timezone
from sqlalchemy import func, or_, update
from db.models import Account

logger = logging.getLogger("executor.account_assignment")
_leases_lock = threading.RLock()
_owned_leases = {}
LEASE_SECONDS = 600.0  # Covers the 300s browser attempt plus cleanup.


def assignment_order_sql():
    # Bootstrap from real past usage; monitoring never changes an existing turn.
    return func.coalesce(Account.last_assigned_at, Account.last_used_at).asc().nulls_first()


def assignment_rank(account):
    stamp = account.last_assigned_at or account.last_used_at
    if stamp is not None and stamp.tzinfo is None:
        stamp = stamp.replace(tzinfo=timezone.utc)
    return stamp.timestamp() if stamp is not None else float("-inf")


def available_assignment_filter(now):
    return or_(Account.assignment_lease_until.is_(None), Account.assignment_lease_until <= now)


def claim_assignment(db, account, scope, *, reserve=True):
    """Atomically accept a fresh candidate, persist its turn, optionally hold it.

    The selector is a transaction boundary: callers receive a committed assignment.
    Comparing the observed turn also rejects stale candidates from another session.
    """
    account_id = account.id
    previous = account.last_assigned_at
    now = datetime.now(timezone.utc)
    token = uuid.uuid4().hex if reserve else None
    condition = Account.last_assigned_at.is_(None) if previous is None else Account.last_assigned_at == previous
    result = db.execute(
        update(Account).where(Account.id == account_id, condition, available_assignment_filter(now)).values(
            last_assigned_at=now,
            assignment_count=func.coalesce(Account.assignment_count, 0) + 1,
            assignment_lease_until=now + timedelta(seconds=LEASE_SECONDS) if reserve else None,
            assignment_lease_token=token,
        ).execution_options(synchronize_session=False)
    )
    if result.rowcount != 1:
        db.rollback()
        return False
    try:
        db.commit()
    except Exception:
        db.rollback()
        raise
    if token:
        with _leases_lock:
            _owned_leases[(scope, account_id)] = (db.get_bind(), token)
    db.refresh(account)
    logger.info(
        "[ACCOUNT_ASSIGNMENT] account=%s scope=%s policy=least_recently_assigned assignments=%s reserved=%s",
        account_id, scope, account.assignment_count, reserve,
    )
    return True


def release_assignment(account_id, scope):
    with _leases_lock:
        owned = _owned_leases.get((scope, account_id))
        if owned is None:
            return
        engine, token = owned
        try:
            with engine.begin() as connection:
                connection.execute(update(Account).where(
                    Account.id == account_id, Account.assignment_lease_token == token,
                ).values(assignment_lease_until=None, assignment_lease_token=None))
        except Exception as exc:
            # Keep ownership so a later cleanup can retry. A crashed worker's lease expires.
            logger.warning("Account %s lease cleanup failed: %s", account_id, type(exc).__name__)
            return
        _owned_leases.pop((scope, account_id), None)


def extend_assignment(account_id, scope, ttl_seconds):
    with _leases_lock:
        owned = _owned_leases.get((scope, account_id))
        if owned is None:
            return
        engine, token = owned
        until = datetime.now(timezone.utc) + timedelta(seconds=max(1.0, float(ttl_seconds)))
        with engine.begin() as connection:
            connection.execute(update(Account).where(
                Account.id == account_id, Account.assignment_lease_token == token,
                Account.assignment_lease_until < until,
            ).values(assignment_lease_until=until))
