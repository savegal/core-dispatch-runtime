"""Select retry targets without repeating recorded publications."""
from collections.abc import Mapping
import asyncio
from functools import wraps
from inspect import signature
from weakref import WeakValueDictionary

from services.task_execution_ledger import has_submission_evidence
from copy import deepcopy
from services.publication_progress import delivery_state, valid_id


_retry_locks = WeakValueDictionary()


def serialize_retry_creation(function):
    """Serialize retries in one process, including SQLite deployments.

    PostgreSQL additionally locks the source row for cross-process exclusion.
    Weak values release idle locks rather than growing with historical orders.
    """
    call_signature = signature(function)

    @wraps(function)
    async def wrapped(*args, **kwargs):
        values = call_signature.bind_partial(*args, **kwargs).arguments
        payload = values.get("task_data")
        source = ((getattr(payload, "settings", None) or {}).get("retry_of_task_id")
                  if payload is not None else values.get("task_id"))
        user_id = getattr(payload, "user_id", None) if payload is not None else values.get("user_id")
        if isinstance(source, bool) or not isinstance(source, (int, str)):
            return await function(*args, **kwargs)
        try:
            source = int(source)
        except ValueError:
            return await function(*args, **kwargs)
        if source <= 0:
            return await function(*args, **kwargs)
        key = (id(asyncio.get_running_loop()), id(values["db"].get_bind()), user_id, source)
        lock = _retry_locks.get(key)
        if lock is None:
            lock = asyncio.Lock()
            _retry_locks[key] = lock
        async with lock:
            return await function(*args, **kwargs)
    return wrapped


async def find_existing_retry(db, user_id: int, original_id: int):
    from sqlalchemy import select
    from db.models import Task
    result = await db.execute(select(Task).where(
        Task.user_id == user_id,
        Task.settings["retry_of_task_id"].as_integer() == original_id,
    ).order_by(Task.id.desc()).limit(1))
    return result.scalar_one_or_none()


def retryable_video_urls(task) -> list[str]:
    def value(key, default=None):
        return task.get(key, default) if isinstance(task, Mapping) else getattr(task, key, default)

    settings = value("settings")
    settings = settings if isinstance(settings, Mapping) else {}
    results = settings.get("url_results")
    results = results if isinstance(results, Mapping) else {}
    raw_urls = value("video_urls")
    urls = [str(url).strip() for url in raw_urls if str(url or "").strip()] if isinstance(raw_urls, (list, tuple)) else []
    failed = settings.get("failed_urls")
    explicit = isinstance(failed, list) and bool(failed)
    candidates = failed if explicit else (urls or list(results))
    known_urls = set(urls)
    status = str(value("status", "") or "").lower()
    selected = []
    for candidate in candidates:
        url = str(candidate or "").strip()
        if not url or url in selected or (explicit and known_urls and url not in known_urls):
            continue
        row = results.get(url)
        if not isinstance(row, Mapping):
            if explicit or status in {"error", "partial"}:
                selected.append(url)
            continue
        row_status = str(row.get("status") or "").lower()
        if row_status in {"success", "done", "submitted_unconfirmed", "pending", "in_progress"}:
            continue
        engagement = row.get("engagement") or {}
        history = list((engagement.get("replies") or {}).get("items") or [])
        if value("kind") == "full":
            history.append(engagement.get("main") or {})
        ambiguous_failure = any(isinstance(item, Mapping) and item.get("error")
                                and delivery_state(item) == "unknown" for item in history)
        if (has_submission_evidence(row) or ambiguous_failure) and not reply_resume_plan(task, url):
            continue
        if explicit or row_status in {"fail", "failed", "error", "partial"}:
            selected.append(url)
    return selected


def reply_resume_plan(task, url):
    """Only known, unsent reply actions; never reconstruct a POST from silence."""
    def value(key, default=None):
        return task.get(key, default) if isinstance(task, Mapping) else getattr(task, key, default)
    settings = value("settings") or {}
    if value("kind") == "keyword_replies":
        return None  # Its durable target table already owns continuation.
    if settings.get("commenting_mode") == "parallel_6x1":
        return None
    source = (settings.get("url_results") or {}).get(url)
    if not isinstance(source, Mapping):
        return None
    engagement = source.get("engagement") or {}
    replies = engagement.get("replies") or {}
    actions = []
    items = list(replies.get("items") or [])
    mains = (engagement.get("main") or {}).get("variants_published") or []
    if value("kind") == "full":
        expanded = []
        for row in items:
            if isinstance(row, Mapping) and not valid_id(row.get("parent_comment_id")) and delivery_state(row) == "not_sent":
                targets = [m for m in mains if isinstance(m, Mapping) and valid_id(m.get("comment_id"))]
                expanded.extend({**deepcopy(dict(row)), "parent_comment_id": str(m["comment_id"]),
                                 "matched_main_text": m.get("text"),
                                 "requested_parent_author": m.get("tiktok_username")} for m in targets)
                if not targets:
                    expanded.append(row)
            else:
                expanded.append(row)
        items = expanded
    for row in items:
        if not isinstance(row, Mapping) or delivery_state(row) != "not_sent":
            continue
        parent = row.get("parent_comment_id")
        if not valid_id(parent) and value("kind") == "engagement_only":
            parent = settings.get("engagement_comment_id") or source.get("comment_id")
        index = row.get("variant_index")
        text = row.get("text")
        if not valid_id(parent) or type(index) is not int or index < 1 or not text:
            continue
        main_report = source.get("main_visibility") or {}
        if main_report.get("enabled"):
            visible_ids = {str(m.get("comment_id")) for m in (engagement.get("main") or {}).get("variants_published") or []
                           if m.get("public_confirmed") is True}
            if str(parent) not in visible_ids:
                continue
        siblings = [r for r in items if isinstance(r, Mapping)
                    and r.get("variant_index") == index
                    and str(r.get("parent_comment_id") or parent) == str(parent)]
        if any(delivery_state(r) != "not_sent" for r in siblings):
            continue
        action = {**deepcopy(dict(row)), "parent_comment_id": str(parent)}
        if not any((a["parent_comment_id"], a["variant_index"]) == (str(parent), index) for a in actions):
            actions.append(action)
    if not actions:
        return None
    snapshot = deepcopy(dict(source))
    snapshot.setdefault("engagement", {}).setdefault("replies", {})["items"] = items
    return {"source_task_id": value("id"), "source": snapshot, "actions": actions}


def seed_reply_resume(source, settings, urls=None):
    """Build server-side from the owned source order, never trust client receipts."""
    source_settings = source.get("settings", {}) if isinstance(source, Mapping) else source.settings or {}
    plans = {url: plan for url in (urls or source_settings.get("url_results") or {})
             if (plan := reply_resume_plan(source, url))}
    settings.pop("_reply_resume", None)
    if plans:
        settings["_reply_resume"] = plans
    return settings
