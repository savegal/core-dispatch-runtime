"""Durable HTTP publication telemetry. No credentials, no network writes or retries."""
from __future__ import annotations

import asyncio
from contextvars import ContextVar
from datetime import datetime, timezone, timedelta
from functools import wraps
import hashlib
import logging
import re
from uuid import uuid4

from sqlalchemy import and_, inspect, or_, update
from sqlalchemy.orm import Session

from db.models import PublicationAttempt, Task, TaskRuntime
from services.async_ownership import await_owned

logger = logging.getLogger(__name__)
_operation = ContextVar("publication_operation", default=None)
quantity_action = ContextVar("quantity_action", default=None)


def utcnow():
    return datetime.now(timezone.utc)


def recent_publication_preparation_failure(now):
    """Temporarily skip failed candidates on the same order and video."""
    return and_(PublicationAttempt.status == "not_sent", PublicationAttempt.wire_count == 0,
        PublicationAttempt.finished_at > now - timedelta(minutes=5),
        or_(PublicationAttempt.error_code.in_(("warmup_runtime_profile_timeout",
                                               "http_preflight:inconclusive:passport_auth_unconfirmed")),
            PublicationAttempt.error_code.startswith("comment_readiness_unconfirmed:"),
            PublicationAttempt.error_code.startswith("proxy_route:")))


def publication_context():
    from services.publication_progress import _scope
    scope = _scope.get()
    if not scope:
        return None
    db, task, url = scope
    if not isinstance(db, Session) or not isinstance(task, Task) or not inspect(task).persistent:
        return None
    return db.get_bind().engine, int(task.id), str(url)


def _write(engine, operation):
    with Session(engine) as db:
        operation(db)
        db.commit()


async def _persist(engine, operation):
    """Own the write to completion, also when the browser task is cancelled."""
    job = asyncio.create_task(asyncio.to_thread(_write, engine, operation))
    await await_owned(job)


def _safe_error(error):
    from services.log_sanitizer import sanitize_string, sanitize_url
    value = str(error or "")
    if re.fullmatch(r"[a-zA-Z0-9_.:-]{1,120}", value) and not re.search(r":\d+:", value):
        return value
    value = re.sub(r"(?:https?|socks5h?)://\S+", lambda match: sanitize_url(match.group(0), max_length=160), value)
    value = re.sub(r"\b[^\s:/]+:\d+:[^\s:]+:[^\s]+", lambda match: sanitize_url(match.group(0), max_length=160), value)
    return sanitize_string(value, max_length=240)[:240] or None


def _start(db, operation_id, task_id, url, account_id, params):
    parent = str(params.get("reply_id") or "0")
    text = str(params.get("text") or "")
    kind = "reply" if parent != "0" else "main"
    db.add(PublicationAttempt(
        id=operation_id, task_id=task_id, account_id=account_id,
        video_url=url, aweme_id=str(params.get("aweme_id") or "") or None,
        parent_id=parent if parent != "0" else None, kind=kind,
        text=text, text_hash=hashlib.sha256(text.encode()).hexdigest(),
        status="preparing", started_at=utcnow(), wire_count=0,
        action_id=quantity_action.get()[0] if quantity_action.get() else None,
    ))
    from sqlalchemy.dialects.sqlite import insert as sqlite_insert
    from sqlalchemy.dialects.postgresql import insert as pg_insert
    insert = sqlite_insert if db.get_bind().dialect.name == "sqlite" else pg_insert
    db.execute(insert(TaskRuntime).values(task_id=task_id, phase=kind, updated_at=utcnow())
        .on_conflict_do_update(index_elements=["task_id"], set_={"phase": kind, "updated_at": utcnow()}))


def note_publication_dispatch(operation=None):
    """Called only by the matching browser request event or explicit transport."""
    operation = operation or _operation.get()
    if operation is None:
        return
    engine, operation_id, pending = operation

    async def save():
        try:
            await _persist(engine, lambda db: db.execute(update(PublicationAttempt)
                .where(PublicationAttempt.id == operation_id)
                .values(status="sending", wire_count=PublicationAttempt.wire_count + 1,
                        dispatched_at=utcnow())))
        except Exception:
            logger.warning("Could not save publication dispatch", exc_info=True)
    pending.append(asyncio.create_task(save()))


def capture_dispatch_callback():
    # Playwright emits callbacks from its connection task, outside our ContextVar.
    operation = _operation.get()
    return lambda: note_publication_dispatch(operation) if operation is not None else None


async def record_phase(phase):
    context = publication_context()
    if context is None:
        return
    engine, task_id, _ = context
    try:
        def save(db):
            from sqlalchemy.dialects.sqlite import insert as sqlite_insert
            from sqlalchemy.dialects.postgresql import insert as pg_insert
            insert = sqlite_insert if db.get_bind().dialect.name == "sqlite" else pg_insert
            db.execute(insert(TaskRuntime).values(task_id=task_id, phase=phase, updated_at=utcnow())
                .on_conflict_do_update(index_elements=["task_id"], set_={"phase": phase, "updated_at": utcnow()}))
        await _persist(engine, save)
    except Exception:
        logger.debug("Could not update task phase", exc_info=True)


def observe_publication_request(function):
    @wraps(function)
    async def wrapped(self, *args, **kwargs):
        profile = kwargs.get("profile") or (args[0] if args else None)
        params = kwargs.get("dynamic_params") or (args[1] if len(args) > 1 else {})
        if (getattr(profile, "method", "").upper() != "POST"
                or str(getattr(profile, "endpoint", "")).rstrip("/") != "/api/comment/publish"):
            return await function(self, *args, **kwargs)
        context = publication_context()
        if context is None:
            return await function(self, *args, **kwargs)
        engine, task_id, url = context
        operation_id, pending = uuid4().hex, []
        page = getattr(self, "page", None)
        account_id = getattr(page, "_pool_account_id", None)
        try:
            await _persist(engine, lambda db: _start(db, operation_id, task_id, url, account_id, params))
        except Exception:
            logger.warning("Could not start publication telemetry", exc_info=True)
            return await function(self, *args, **kwargs)
        token = _operation.set((engine, operation_id, pending))
        result = None
        try:
            result = await function(self, *args, **kwargs)
        finally:
            _operation.reset(token)
            async def finish():
                if pending:
                    await asyncio.gather(*pending, return_exceptions=True)
                meta = dict(getattr(self, "last_request_meta", {}) or {})
                from executor.http.playwright_http_client import _extract_comment_id
                cid = _extract_comment_id(result[1]) if result and result[0] else None
                action = quantity_action.get()
                def save(db):
                    nonlocal cid, result
                    if cid and action:
                        # The task lock serializes ownership of a CID across
                        # simultaneous sends and author-list recoveries.
                        db.execute(update(Task).where(Task.id == task_id).values(updated_at=Task.updated_at))
                        owner = db.query(PublicationAttempt.id).filter(
                            PublicationAttempt.task_id == task_id, PublicationAttempt.comment_id == cid,
                            PublicationAttempt.action_id != action[0], PublicationAttempt.status == "accepted").first()
                        if owner is not None:
                            from executor.http.retry_policy import HttpErrorType
                            cid = None
                            result = (False, None, "submitted_unconfirmed:cid_already_assigned",
                                      HttpErrorType.SUBMITTED_UNCONFIRMED)
                    row = db.get(PublicationAttempt, operation_id)
                    if row is None:
                        return
                    dispatched = bool(meta.get("request_attempted") or row.wire_count)
                    row.wire_count = max(row.wire_count or 0, int(dispatched))
                    if dispatched and row.dispatched_at is None:
                        row.dispatched_at = utcnow()
                    rejected = (dispatched and row.wire_count == 1 and result is not None
                                and not result[0] and meta.get("publication_status") == "rejected")
                    row.status = "accepted" if cid else (
                        "failed" if rejected else "unknown" if dispatched else "not_sent")
                    row.comment_id, row.finished_at = cid, utcnow()
                    row.error_code = _safe_error(result[2] if result else "operation_interrupted")
                    if cid:
                        from db.models import Account
                        from executor.visibility_support import account_identity_keys
                        account = db.get(Account, row.account_id) if row.account_id else None
                        if account is not None:
                            row.identity_keys = list(account_identity_keys(account, meta.get("comment_author"),
                                user_id=meta.get("comment_author_uid")))
                            if meta.get("receipt_source") == "author_list_recovery":
                                row.observers = [{"account_id": row.account_id, "role": "author", "found": True,
                                                  "source": "empty_post_recovery"}]
                        from services.publication_runtime import load_publication_runtime
                        row.next_check_at = utcnow() + timedelta(seconds=load_publication_runtime(db).late_delays[0])
                    if action:
                        from db.models import QuantityAction
                        db.execute(update(QuantityAction).where(QuantityAction.id == action[0],
                            QuantityAction.token == action[1], QuantityAction.state == "dispatching")
                            .values(comment_id=cid, account_id=row.account_id,
                                    state="accepted" if cid else (
                                        "rejected" if rejected else "uncertain" if dispatched else "not_sent"),
                                    updated_at=utcnow()))
                await _persist(engine, save)
            completion = asyncio.create_task(finish())
            try:
                await await_owned(completion)
            except Exception:
                # Telemetry cannot convert an accepted POST into a retryable failure.
                logger.warning("Could not finish publication telemetry", exc_info=True)
        return result
    return wrapped


def sync_public_evidence(db, task):
    """Project existing independent visibility evidence onto tracked CIDs."""
    results = (task.settings or {}).get("url_results") or {}
    evidence = {}
    for result in results.values():
        if not isinstance(result, dict):
            continue
        engagement = result.get("engagement") or {}
        mains = (engagement.get("main") or {}).get("variants_published") or []
        replies = (engagement.get("replies") or {}).get("items") or []
        for item in [*mains, *replies]:
            if not isinstance(item, dict):
                continue
            cid = item.get("reply_id") or item.get("reply_comment_id") or item.get("comment_id")
            if cid and (item.get("public_confirmed") is True or item.get("visibility_status") == "public_confirmed"):
                evidence[str(cid)] = item
    if not evidence:
        return
    for row in db.query(PublicationAttempt).filter(
            PublicationAttempt.task_id == task.id, PublicationAttempt.comment_id.in_(evidence)):
        if row.checked_at is not None and row.visibility in {"not_found", "author_only", "visible"}:
            continue
        row.visibility = "visible"
        row.visible_at = row.visible_at or utcnow()
        row.checked_at = utcnow()
        row.next_check_at = None
