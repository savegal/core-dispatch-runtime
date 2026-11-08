"""Leased, read-only, exact-CID checks. This module never publishes or likes."""
from __future__ import annotations

import asyncio
from collections import defaultdict
from datetime import timedelta
import hashlib
import logging
import re
import threading
import time
from uuid import uuid4

from sqlalchemy import or_, select, update
from sqlalchemy.orm import Session

from db.models import Account, PublicationAttempt, PublicationCheckJob, QuantityAction, Task
from services.async_ownership import await_owned
from services.publication_journal import _safe_error, recent_publication_preparation_failure, utcnow
from services.proxy_normalizer import ProxyRouteError

logger = logging.getLogger(__name__)
ACTIVE = {"pending", "in_progress", "running", "queued"}
RECHECK_DELAYS = (120, 600, 3600)
OBSERVER_ACCOUNT_TIMEOUT_SECONDS = 35.0
OBSERVER_ROUND_TIMEOUT_SECONDS = 65.0
OBSERVER_COOLDOWN_SECONDS = 120
OBSERVER_NO_PROGRESS_LIMIT = 2


def seed_saved_receipts(db, task):
    from services.publication_reports import publication_rows
    from sqlalchemy.dialects.sqlite import insert as sqlite_insert
    from sqlalchemy.dialects.postgresql import insert as pg_insert
    insert = sqlite_insert if db.get_bind().dialect.name == "sqlite" else pg_insert
    for row in publication_rows(db, task):
        if not row["id"].startswith("receipt:"):
            continue
        identity = hashlib.sha256(f"{task.id}:{row['kind']}:{row['comment_id']}".encode()).hexdigest()[:32]
        if db.get(PublicationAttempt, identity):
            continue
        video_id = row["aweme_id"]
        if not video_id:
            match = re.search(r"/(?:video|photo)/(\d+)", row["video_url"])
            video_id = match.group(1) if match else None
        account_id = row["account_id"] if row["account_id"] and db.get(Account, row["account_id"]) else None
        db.execute(insert(PublicationAttempt).values(id=identity, task_id=task.id, account_id=account_id,
            video_url=row["video_url"], aweme_id=video_id, kind=row["kind"], parent_id=row["parent_id"],
            text=row["text"], text_hash=row["text_hash"], comment_id=row["comment_id"],
            status=row["status"], origin="receipt", wire_count=0, visibility=row["visibility"], started_at=utcnow())
            .on_conflict_do_nothing(index_elements=["id"]))


def request_recheck(db, task_id, *, automatic=False):
    task = db.get(Task, task_id)
    if task is None or task.status in ACTIVE:
        raise ValueError("Task is still sending publications")
    from services.quantity_plan import traffic_budget_exhausted
    if traffic_budget_exhausted(db, task):
        raise ValueError("quantity_traffic_budget_exhausted")
    seed_saved_receipts(db, task)
    db.flush()
    from sqlalchemy.dialects.sqlite import insert as sqlite_insert
    from sqlalchemy.dialects.postgresql import insert as pg_insert
    insert = sqlite_insert if db.get_bind().dialect.name == "sqlite" else pg_insert
    now = utcnow()
    scope = {"scope": "due" if automatic else "all"}
    statement = insert(PublicationCheckJob).values(task_id=task_id, status="pending", available_at=now, updated_at=now, result=scope)
    db.execute(statement.on_conflict_do_update(index_elements=["task_id"],
        set_={"status": "pending", "available_at": now, "updated_at": now, "lease_token": None, "lease_until": None, "result": scope},
        where=(PublicationCheckJob.status.notin_(["pending", "running"]))
            & (PublicationCheckJob.updated_at < now - timedelta(seconds=30))))
    db.flush()
    job = db.get(PublicationCheckJob, task_id, populate_existing=True)
    return {"task_id": task_id, "status": job.status}


def apply_observations(rows, observations, *, now=None, delays=RECHECK_DELAYS):
    """An unavailable/partial read never proves that a publication is absent."""
    now = now or utcnow()
    for row in rows:
        found, author_found, absent, observers = False, False, set(), []
        for observation in observations:
            observer_id = observation.get("observer_id")
            if observer_id is None:
                continue
            found_here = str(row.comment_id) in set(map(str, observation.get("found_ids") or []))
            absent_here = (str(row.comment_id) in set(map(str, observation.get("absent_ids") or []))
                           and (observation.get("complete") or str(row.comment_id) in set(map(str, observation.get("complete_ids") or []))))
            if observation.get("role") == "author":
                if observer_id == row.account_id:
                    author_found = author_found or found_here
                    observers.append({"account_id": observer_id, "role": "author", "found": found_here,
                                      "complete_absence": absent_here})
                continue
            if observer_id == row.account_id:
                continue
            found = found or found_here
            if absent_here:
                absent.add(observer_id)
            observers.append({"account_id": observer_id, "found": found_here, "complete_absence": absent_here,
                              "reason": str(observation.get("reason") or "")[:100]})
        if found:
            row.visibility = "visible"
        elif len(absent) >= 2:
            row.visibility = "author_only" if author_found else "not_found"
        elif getattr(row, "visibility", None) not in {"visible", "author_only", "not_found"}:
            row.visibility = "inconclusive"
        if found or len(absent) >= 2 or row.visibility == "inconclusive":
            row.observers, row.checked_at = observers, now
        if found:
            row.visible_at = row.visible_at or now
        row.check_count = int(row.check_count or 0) + 1
        row.next_check_at = (now + timedelta(seconds=delays[row.check_count])
                             if not found and row.check_count < len(delays) else None)


def sync_visible_actions(db, task_id):
    """Advance an unchanged CID using its latest confirmed observation."""
    from services.publication_reports import ranked_publication_rows
    ranked = ranked_publication_rows(db.query(PublicationAttempt).filter(PublicationAttempt.task_id == task_id))
    proven = select(ranked.c.comment_id).where(ranked.c.rank == 1, ranked.c.visibility == "visible")
    db.execute(update(QuantityAction).where(QuantityAction.task_id == task_id,
        QuantityAction.comment_id.in_(proven), QuantityAction.state.in_(["accepted", "absent"]))
        .values(state="visible", updated_at=utcnow()))


def _author_identity_matches(saved, current):
    expected = {tuple(key) for key in saved or []}
    matched = False
    for family in ("user_id", "uid"):
        before = {value for key, value in expected if key == family}
        after = {value for key, value in current if key == family}
        if before and after:
            if not before & after:
                return False
            matched = True
    return matched or any(key[0] == "username" for key in expected & current)


def _independent_evidence(observations):
    found, absent = set(), defaultdict(set)
    for observation in observations:
        observer_id = observation.get("observer_id")
        if observer_id is None or observation.get("role") == "author":
            continue
        found.update(map(str, observation.get("found_ids") or []))
        missing = set(map(str, observation.get("absent_ids") or []))
        if not observation.get("complete"):
            missing &= set(map(str, observation.get("complete_ids") or []))
        for cid in missing:
            absent[cid].add(observer_id)
    return found, absent


async def observe_video(db, task, rows, *, observer_cooldowns: dict[int, float] | None = None):
    from services.publication_journal import record_phase
    from services.publication_runtime import load_publication_runtime
    from services.quantity_plan import traffic_control_reason
    from services.traffic_meter import flush_task_traffic
    await record_phase("checking")
    from executor import user_schema as schema
    from executor.main_visibility import scan_main_comment_ids
    from executor.reply_visibility import scan_reply_comment_ids
    from executor.http.comment_discovery import comment_read_batch
    from executor.http.signature_runtime import wait_for_runtime_signer_ready
    from executor.visibility_support import account_identity_keys, identity_is_known, identity_rejection_reason
    from services.account_service import get_free_account_for_reply
    from config.settings import get_settings
    settings = get_settings()
    publishers = {row.account_id for row in rows if row.account_id is not None}
    references, safe_rows = [], []
    for row in rows:
        account = db.get(Account, row.account_id) if row.account_id is not None else None
        keys = account_identity_keys(account, schema._get_cached_http_preflight_username(account),
                                     user_id=schema._get_cached_http_preflight_user_id(account))
        keys.update(tuple(key) for key in (row.identity_keys or []))
        if not identity_is_known(keys):
            continue
        safe_rows.append(row)
        if keys not in references:
            references.append(keys)
    # One missing author must not block checks of unrelated known publishers.
    # Saved identity keys still prove authorship after a pool record is deleted.
    if not safe_rows:
        return [{"reason": "publisher_identity_unavailable"}]
    url, aweme_id = safe_rows[0].video_url, safe_rows[0].aweme_id
    if not aweme_id:
        return [{"reason": "video_id_unavailable"}]
    used, observations = set(publishers), []
    if isinstance(db, Session):
        order_urls = task.video_urls or []
        aliases = {url, *(value for value in order_urls if schema.extract_tiktok_media_id(value) == str(aweme_id))}
        if len(order_urls) == 1:
            aliases.add(order_urls[0])
        same_video = or_(PublicationAttempt.aweme_id == str(aweme_id), PublicationAttempt.video_url.in_(aliases))
        history = db.query(PublicationAttempt).filter(PublicationAttempt.task_id == task.id, same_video)
        for account_id, identity_keys in history.filter(or_(PublicationAttempt.wire_count > 0,
                PublicationAttempt.status == "accepted")).with_entities(PublicationAttempt.account_id, PublicationAttempt.identity_keys):
            if account_id is not None:
                used.add(account_id)
            if identity_keys:
                keys = {tuple(key) for key in identity_keys if isinstance(key, (list, tuple)) and len(key) == 2
                        and all(isinstance(value, str) for value in key)}
                if identity_is_known(keys) and keys not in references:
                    references.append(keys)
        now = utcnow()
        used.update(account_id for (account_id,) in history.filter(PublicationAttempt.account_id.isnot(None),
            recent_publication_preparation_failure(now)).with_entities(PublicationAttempt.account_id).distinct())
        cutoff = now - timedelta(seconds=OBSERVER_COOLDOWN_SECONDS)
        for (readers,) in history.filter(PublicationAttempt.checked_at > cutoff).with_entities(PublicationAttempt.observers):
            used.update(item["account_id"] for item in readers or [] if isinstance(item, dict)
                        and item.get("role") != "author" and isinstance(item.get("account_id"), int))
    # Guard navigation with the resolved media ID even when the order used vt/vm.
    if not schema.extract_tiktok_media_id(url):
        url = f"https://www.tiktok.com/@_/video/{aweme_id}"
    wanted = {str(row.comment_id) for row in safe_rows}
    loop = asyncio.get_running_loop()
    round_deadline = loop.time() + OBSERVER_ROUND_TIMEOUT_SECONDS
    async def budget_reason():
        if loop.time() >= round_deadline:
            return "observer_round_timeout"
        checkpointed = (await flush_task_traffic(db.get_bind().engine, task.id)
                        if isinstance(db, Session) else True)
        return traffic_control_reason(db, task, checkpointed=checkpointed)
    async def read_account(observer, candidates, *, author=False):
        context = page = keys = None
        observer_id = observer.id
        corr = f"task_{task.id}_recheck_acc{observer_id}"
        result = {"observer_id": observer_id, "role": "author" if author else "observer",
                  "found_ids": [], "absent_ids": [], "complete_ids": []}
        deadline = min(round_deadline, loop.time() + OBSERVER_ACCOUNT_TIMEOUT_SECONDS)
        try:
            async with asyncio.timeout_at(deadline):
                deny = await schema._preflight_cookie_http_gate(db, observer, correlation_id=corr, phase="publication_recheck")
                if deny:
                    return {**result, "reason": deny, "preflight_failed": True}, None
                keys = account_identity_keys(observer, schema._get_cached_http_preflight_username(observer),
                                             user_id=schema._get_cached_http_preflight_user_id(observer))
                if author:
                    candidates = [row for row in candidates if _author_identity_matches(row.identity_keys, keys)]
                    if not candidates:
                        return None, None
                elif identity_rejection_reason(keys, references):
                    return None, None
                if isinstance(db, Session) and db.in_transaction():
                    db.commit()
                warmup_started_at = time.monotonic()
                context, page = await schema.create_page_for_account(observer)
                ready, reason = await schema._ensure_account_login_ready(db, page, observer, url,
                    timeout_ms=int(settings.PLAYWRIGHT_TIMEOUT * (2 if observer.proxy else 1)), correlation_id=corr)
                if not ready:
                    return {**result, "reason": reason or "observer_auth_unconfirmed"}, None
                if not schema.is_on_expected_media_url(getattr(page, "url", None), url):
                    return {**result, "reason": "observer_target_changed"}, None
                page._warmup_signer_ready = await wait_for_runtime_signer_ready(page, timeout_ms=5500)
                result["signer_ready"] = page._warmup_signer_ready
                if not schema.is_on_expected_media_url(getattr(page, "url", None), url):
                    return {**result, "reason": "observer_target_changed"}, None
                groups = defaultdict(list)
                for row in candidates:
                    groups[(row.kind, row.parent_id)].append(str(row.comment_id))
                async with comment_read_batch(page):
                    for (kind, parent), ids in groups.items():
                        if reason := await budget_reason():
                            result["reason"] = reason
                            break
                        evidence = (await scan_main_comment_ids(page, aweme_id, ids, max_pages=runtime.main_pages,
                                                               warmup_started_at=warmup_started_at, allow_page_fallback=True)
                            if kind == "main" else await scan_reply_comment_ids(page, aweme_id, parent, ids,
                                max_pages=runtime.reply_pages))
                        result["found_ids"].extend(evidence.get("found_ids") or [])
                        result["absent_ids"].extend(evidence.get("absent_ids") or [])
                        if evidence.get("complete"):
                            result["complete_ids"].extend(ids)
                        if evidence.get("reason") and not (evidence.get("found_ids") or evidence.get("complete")):
                            result.setdefault("reason", evidence["reason"])
        except TimeoutError:
            result["reason"] = "observer_account_timeout"
        except ProxyRouteError as exc:
            result["reason"] = str(exc)
            result["preflight_failed"] = context is None
        except Exception as exc:
            result["reason"] = "observer_read_error:" + type(exc).__name__
            logger.warning("Publication reader failed task=%s account=%s (%s)", task.id, observer_id,
                           type(exc).__name__)
        finally:
            try:
                await schema._safe_close(context, page, reason="publication_rechecked", correlation_id=corr)
            except Exception:
                logger.warning("Could not close publication reader %s", observer_id, exc_info=True)
        return result, keys

    runtime = load_publication_runtime(db)
    no_progress, read_attempts = 0, 0
    for _ in range(runtime.observer_attempts * 3):
        if read_attempts >= runtime.observer_attempts:
            break
        if reason := await budget_reason():
            observations.append({"reason": reason})
            break
        found_ids, absences = _independent_evidence(observations)
        if all(cid in found_ids or len(absences[cid]) >= 2 for cid in wanted):
            break
        excluded = used
        if observer_cooldowns is not None:
            now = loop.time()
            excluded = used | {account_id for account_id, until in observer_cooldowns.items() if until > now}
        selection = asyncio.create_task(asyncio.to_thread(
            get_free_account_for_reply, db, exclude_ids=list(excluded), reserve=True,
            pool_owner_user_id=task.user_id if (task.settings or {}).get("account_pool_mode") == "personal" else None,
            exclude_degraded_categories={"auth_unconfirmed", "captcha", "page_blocked"}))
        try:
            observer = await await_owned(selection)
        except asyncio.CancelledError:
            if not selection.cancelled() and selection.exception() is None:
                observer = selection.result()
                if observer is not None:
                    schema.release_reply_account_reservation(observer.id)
            raise
        if observer is None:
            break
        observer_id = observer.id
        used.add(observer_id)
        if observer_cooldowns is not None:
            # A parallel batch can finish reading while this selection is in flight.
            if observer_cooldowns.get(observer_id, 0) > loop.time():
                schema.release_reply_account_reservation(observer_id)
                continue
            observer_cooldowns[observer_id] = float("inf")
        try:
            candidates = [row for row in safe_rows if str(row.comment_id) not in found_ids]
            started = loop.time()
            result, keys = await read_account(observer, candidates)
            if result is None:
                continue
            preflight_failed = result.pop("preflight_failed", False)
            logger.info("[PUBLICATION_OBSERVER] task=%s account=%s elapsed_ms=%s preflight_failed=%s "
                        "signer_ready=%s found=%s complete=%s reason=%s", task.id, observer_id,
                        round((loop.time() - started) * 1000), preflight_failed, result.get("signer_ready"),
                        len(result["found_ids"]), len(result["complete_ids"]), _safe_error(result.get("reason")))
            if preflight_failed:
                observations.append(result)
                continue
            read_attempts += 1
            if result["found_ids"] or result["complete_ids"] or result.get("reason"):
                observations.append(result)
                if result["found_ids"] or result["complete_ids"]:
                    references.append(keys)
                    no_progress = 0
                else:
                    no_progress += 1
        finally:
            if observer_cooldowns is not None:
                observer_cooldowns[observer_id] = loop.time() + OBSERVER_COOLDOWN_SECONDS
            schema.release_reply_account_reservation(observer_id)
        if no_progress >= OBSERVER_NO_PROGRESS_LIMIT:
            break

    # The author read is a separate, read-only check. A POST receipt alone
    # provides no evidence that the author can see the comment in the list.
    from services.account_assignment import claim_assignment, release_assignment
    found_ids, absences = _independent_evidence(observations)
    authors = defaultdict(list)
    for row in safe_rows:
        cid = str(row.comment_id)
        if row.account_id is not None and row.identity_keys and cid not in found_ids and len(absences[cid]) >= 2:
            authors[row.account_id].append(row)
    ordered_authors = sorted(authors.items(), key=lambda pair: all(
        getattr(row, "visibility", None) == "author_only" for row in pair[1]))
    for author_id, candidates in ordered_authors[:runtime.author_checks]:
        if reason := await budget_reason():
            observations.append({"reason": reason})
            break
        account = db.get(Account, author_id)
        owner = task.user_id if (task.settings or {}).get("account_pool_mode") == "personal" else None
        if account is None or account.status != "active" or account.owner_user_id != owner:
            continue
        if not claim_assignment(db, account, "publication_author_read", reserve=True):
            continue
        try:
            result, _ = await read_account(account, candidates, author=True)
            if result is not None:
                observations.append(result)
        finally:
            release_assignment(author_id, "publication_author_read")
    return observations


class RecheckInterrupted(Exception):
    pass


def process_due_checks(engine, stop_event=None):
    from services.publication_progress import publication_scope
    from executor.browser_pool import browser_pool_task_scope, close_browser_pool
    from services.dedicated_proxy_service import activate_dedicated_proxy_for_user, reset_dedicated_proxy
    from services.publication_runtime import load_publication_runtime
    if stop_event is not None and stop_event.is_set():
        return False
    now, token = utcnow(), uuid4().hex
    with Session(engine, expire_on_commit=False) as db:
        # Schedule a bounded number of newly accepted publications per tick.
        due = db.query(PublicationAttempt.task_id).join(Task).filter(
            PublicationAttempt.next_check_at <= now, PublicationAttempt.visibility != "visible",
            Task.status.notin_(ACTIVE)).distinct().limit(10).all()
        for (task_id,) in due:
            try:
                request_recheck(db, task_id, automatic=True)
            except ValueError as exc:
                if str(exc) != "quantity_traffic_budget_exhausted":
                    raise
                db.execute(update(PublicationAttempt).where(PublicationAttempt.task_id == task_id)
                    .values(next_check_at=None))
        db.commit()
        now = utcnow()
        if engine.dialect.name == "sqlite":
            db.connection().exec_driver_sql("BEGIN IMMEDIATE")
        job = db.query(PublicationCheckJob).join(Task).filter(Task.status.notin_(ACTIVE),
            or_((PublicationCheckJob.status == "pending") & (PublicationCheckJob.available_at <= now),
                (PublicationCheckJob.status == "running") & (PublicationCheckJob.lease_until < now)))
        candidate = job.order_by(PublicationCheckJob.available_at, PublicationCheckJob.task_id).with_for_update(
            skip_locked=True, of=PublicationCheckJob).first()
        if candidate is None:
            return False
        task_id = candidate.task_id
        due_only = (candidate.result or {}).get("scope") == "due"
        # SQLite returns a naive lease timestamp. Compare it only in SQL.
        claimed = db.execute(update(PublicationCheckJob).where(PublicationCheckJob.task_id == task_id,
            or_(PublicationCheckJob.status == "pending", PublicationCheckJob.lease_until < now))
            .values(status="running", lease_token=token, lease_until=now + timedelta(minutes=12), updated_at=now),
            execution_options={"synchronize_session": False}).rowcount
        db.commit()
        if not claimed:
            return False
        task = db.get(Task, task_id)
        runtime = load_publication_runtime(db)
        # Copy plain values before observers commit their own account reservations.
        selected_rows = db.query(PublicationAttempt).filter(
            PublicationAttempt.task_id == task_id, PublicationAttempt.comment_id.isnot(None))
        if due_only:
            selected_rows = selected_rows.filter(PublicationAttempt.next_check_at <= now,
                                                  PublicationAttempt.visibility != "visible")
        row_ids = [row.id for row in selected_rows]
        db.rollback()
        dedicated_token = None
        progress = dict(scope="due" if due_only else "all", processed=0, checked=0, visible=0, author_only=0)
        committed = set()

        async def run():
            groups = defaultdict(list)
            for row_id in row_ids:
                row = db.get(PublicationAttempt, row_id)
                groups[(row.video_url, row.aweme_id)].append(row)
            try:
                with browser_pool_task_scope(task_id), publication_scope(db, task, (task.video_urls or [""])[0]):
                    for rows in groups.values():
                        for offset in range(0, len(rows), runtime.check_batch_size):
                            if stop_event is not None and stop_event.is_set():
                                raise RecheckInterrupted()
                            batch = rows[offset:offset + runtime.check_batch_size]
                            from services.quantity_plan import traffic_budget_exhausted
                            if traffic_budget_exhausted(db, task):
                                observations = [{"reason": "quantity_traffic_budget_exhausted"}]
                            else:
                                observations = await observe_video(db, task, batch)
                            if stop_event is not None and stop_event.is_set():
                                raise RecheckInterrupted()
                            owned = db.execute(update(PublicationCheckJob).where(
                                PublicationCheckJob.task_id == task_id, PublicationCheckJob.lease_token == token)
                                .values(updated_at=utcnow())).rowcount
                            if not owned:
                                db.rollback()
                                raise RecheckInterrupted()
                            apply_observations(batch, observations, delays=runtime.late_delays)
                            if any(o.get("reason") == "quantity_traffic_budget_exhausted" for o in observations):
                                for row in batch:
                                    row.next_check_at = None
                            found, absent = _independent_evidence(observations)
                            confirmed = [row for row in batch if row.comment_id in found or len(absent[row.comment_id]) >= 2]
                            progress["processed"] += len(batch)
                            progress["checked"] += len(confirmed)
                            progress["visible"] += sum(row.visibility == "visible" for row in confirmed)
                            progress["author_only"] += sum(row.visibility == "author_only" for row in confirmed)
                            db.execute(update(PublicationCheckJob).where(PublicationCheckJob.task_id == task_id)
                                       .values(result=dict(progress)))
                            db.flush()
                            sync_visible_actions(db, task_id)
                            db.commit()
                            committed.update(row.id for row in batch)
                return dict(progress)
            finally:
                try:
                    # asyncio.run closes this loop after the job. A retained
                    # thread-local driver would still belong to that dead loop.
                    await asyncio.wait_for(close_browser_pool(stop_playwright=True), timeout=10)
                except asyncio.TimeoutError:
                    logger.warning("Publication recheck browser cleanup timed out")
        async def run_owned():
            operation = asyncio.create_task(run())
            try:
                while not operation.done():
                    await asyncio.wait({operation}, timeout=.25)
                    if stop_event is not None and stop_event.is_set():
                        operation.cancel()
                        await asyncio.gather(operation, return_exceptions=True)
                        raise RecheckInterrupted()
                return operation.result()
            finally:
                if not operation.done():
                    operation.cancel()
                    await asyncio.gather(operation, return_exceptions=True)
        try:
            if (task.settings or {}).get("account_pool_mode") != "personal":
                dedicated_token = activate_dedicated_proxy_for_user(db, task.user_id)
            result = asyncio.run(asyncio.wait_for(run_owned(), timeout=600))
            state = "done"
        except RecheckInterrupted:
            db.rollback()
            result, state = dict(progress, reason="service_stopping"), "pending"
        except Exception as exc:
            db.rollback()
            for row_id in set(row_ids) - committed:
                row = db.get(PublicationAttempt, row_id)
                if row is not None:
                    apply_observations([row], [], delays=runtime.late_delays)
            result, state = dict(progress, error=type(exc).__name__), "error"
            logger.warning("Publication recheck failed for task %s", task_id, exc_info=True)
        finally:
            reset_dedicated_proxy(dedicated_token)
        owned = db.execute(update(PublicationCheckJob).where(
            PublicationCheckJob.task_id == task_id, PublicationCheckJob.lease_token == token)
            .values(status=state, result=result, lease_token=None, lease_until=None, updated_at=utcnow())).rowcount
        if owned:
            db.commit()
        else:
            db.rollback()
        return True


async def recheck_loop():
    from db.database import sync_engine
    from services.publication_runtime import load_publication_runtime, default_runtime
    stop_event = threading.Event()
    runtime = default_runtime()

    def read_runtime():
        with Session(sync_engine) as db:
            return load_publication_runtime(db)

    try:
        while True:
            try:
                runtime = await asyncio.to_thread(read_runtime)
                batch = asyncio.gather(*(asyncio.to_thread(process_due_checks, sync_engine, stop_event)
                    for _ in range(runtime.late_workers)), return_exceptions=True)
                try:
                    results = await asyncio.shield(batch)
                except asyncio.CancelledError:
                    stop_event.set()
                    await await_owned(batch)
                    raise
                for result in results:
                    if isinstance(result, Exception):
                        logger.warning("Publication recheck worker failed (%s)", type(result).__name__, exc_info=result)
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.warning("Publication recheck tick failed", exc_info=True)
            await asyncio.sleep(runtime.late_poll)
    finally:
        stop_event.set()
