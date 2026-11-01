"""Merge durable per-publication receipts into existing URL results."""
from collections.abc import Mapping
from contextlib import contextmanager
from contextvars import ContextVar
from copy import deepcopy
from dataclasses import asdict, is_dataclass

_scope = ContextVar("publication_progress", default=None)


def valid_id(value):
    value = str(value or "")
    return value.isascii() and value.isdigit() and 10 <= len(value) <= 30 and int(value) > 0


def delivery_state(row):
    """Positive submission evidence always overrides legacy error inference."""
    if not isinstance(row, Mapping):
        return "unknown"
    if any(valid_id(row.get(k)) for k in ("reply_id", "reply_comment_id", "comment_id")):
        return "accepted"
    if (row.get("submitted_unconfirmed") or row.get("soft_submitted")
            or row.get("visibility_status") == "submitted_unconfirmed"):
        return "unknown"
    if row.get("publication_attempted") is True or row.get("network_request_attempted"):
        return "unknown"
    if row.get("publication_attempted") is False:
        return "not_sent"
    error = str(row.get("error") or row.get("error_reason") or "").lower()
    # These named gates run before publication, even if their GET timed out.
    if error.startswith(("http_preflight:", "dead_session:http_preflight:", "parent_api_unavailable:", "cookies_not_http_ready")):
        return "not_sent"
    # A POST failure / worker exception without stage evidence stays unknown.
    if any(s in error for s in ("empty_body", "submitted_unconfirmed", "timeout", "exception")):
        return "unknown"
    if error in {"no_free_reply_accounts", "no_accounts_available", "target_document_changed", "stopped_by_user"}:
        return "not_sent"
    return "unknown"


def action_key(row, kind="reply"):
    if kind == "main":
        return str(row.get("comment_id") or (row.get("variant_index"), row.get("account_id"), row.get("text")))
    return (str(row.get("parent_comment_id") or row.get("physical_parent_key") or ""),
            int(row.get("variant_index") or 0), str(row.get("text") or ""))


def merge_items(old, new, kind="reply"):
    merged = []
    rank = {"not_sent": 0, "unknown": 1, "accepted": 2}
    def receipt_id(item):
        return str(item.get("reply_id") or item.get("reply_comment_id") or item.get("comment_id") or "")
    for item in [*(old or []), *(new or [])]:
        if not isinstance(item, Mapping):
            continue
        row = deepcopy(dict(item))
        cid = receipt_id(row)
        index = next((i for i, previous in enumerate(merged)
                      if valid_id(cid) and receipt_id(previous) == cid), None)
        if index is None:
            index = next((i for i, previous in enumerate(merged)
                          if action_key(previous, kind) == action_key(row, kind)
                          and not (valid_id(cid) and valid_id(receipt_id(previous))
                                   and cid != receipt_id(previous))), None)
        previous = merged[index] if index is not None else {}
        finishing_intent = (previous.get("_resume_intent") and row.get("_resume_attempt_token")
                            == previous.get("_resume_attempt_token") and not row.get("_resume_intent"))
        if previous and not finishing_intent and rank[delivery_state(previous)] > rank[delivery_state(row)]:
            row = {**row, **previous}
        else:
            row = {**previous, **row}
        if previous.get("variant_index") and not row.get("variant_index"):
            row["variant_index"] = previous["variant_index"]
        if previous.get("public_confirmed"):
            row["public_confirmed"] = True
        if type(row.get("publication_attempted")) is not bool:
            row["publication_attempted"] = delivery_state(row) != "not_sent"
        if index is None:
            merged.append(row)
        else:
            merged[index] = row
    return merged


def merge_url_result(old, new):
    """Keep receipts when a finalizer still holds a stale in-memory snapshot."""
    old = old if isinstance(old, Mapping) else {}
    new = new if isinstance(new, Mapping) else {}
    result = {**deepcopy(dict(old)), **deepcopy(dict(new))}
    previous, current = old.get("engagement") or {}, new.get("engagement") or {}
    engagement = {**deepcopy(previous), **deepcopy(current)}
    main = {**deepcopy(previous.get("main") or {}), **deepcopy(current.get("main") or {})}
    variants = merge_items((previous.get("main") or {}).get("variants_published"), main.get("variants_published"), "main")
    if variants:
        main["variants_published"] = variants
        main.update(posted=True, network_confirmed=any(v.get("network_confirmed") for v in variants),
                    publication_attempted=True, comment_id=variants[0].get("comment_id"), submitted=len(variants))
    main.setdefault("publication_attempted", bool(main.get("posted") or main.get("network_confirmed")))
    engagement["main"] = main
    replies = {**deepcopy(previous.get("replies") or {}), **deepcopy(current.get("replies") or {})}
    replies["items"] = merge_items((previous.get("replies") or {}).get("items"), (current.get("replies") or {}).get("items"))
    replies["ok"] = sum(bool(r.get("success")) for r in replies["items"])
    replies["submitted"] = sum(delivery_state(r) == "accepted" or bool(r.get("submitted_unconfirmed")) for r in replies["items"])
    replies["total"] = len(replies["items"])
    engagement["replies"] = replies
    result["engagement"] = engagement
    result.pop("execution_ledger", None)
    return result


@contextmanager
def publication_scope(db, task, url):
    token = _scope.set((db, task, url))
    try:
        yield
    finally:
        _scope.reset(token)


def _save(db, task, url, kind, item):
    from sqlalchemy.orm.attributes import flag_modified
    settings = deepcopy(task.settings or {})
    urls = dict(settings.get("url_results") or {})
    section = {"main": {"variants_published": [item]}} if kind == "main" else {"replies": {"items": [item]}}
    urls[url] = merge_url_result(urls.get(url), {"status": "in_progress", "engagement": section})
    settings["url_results"] = urls
    task.settings = settings
    flag_modified(task, "settings")
    db.commit()


async def record_publication(kind, outcome):
    scope = _scope.get()
    if scope is None:
        return
    item = asdict(outcome) if is_dataclass(outcome) else dict(outcome)
    if kind == "reply":
        item["reply_id"] = item.get("reply_id") or item.get("reply_comment_id")
        item["network_confirmed"] = bool(item.get("network_confirmed") or item.get("reply_confirmation") == "network_comment_id")
        if item.get("soft_submitted") or item.get("counts_as_submitted") and not valid_id(item.get("reply_id")):
            item["submitted_unconfirmed"] = True
    if delivery_state(item) != "accepted" and not item.get("submitted_unconfirmed"):
        return
    item["publication_attempted"] = True
    db, task, url = scope
    from services.visibility_checkpoint import run_visibility_checkpoint
    try:
        await run_visibility_checkpoint(db, task, _save, url, kind, item)
    except Exception:
        task._publication_checkpoint_failed = True
        raise


def explicit_publication_flags(result):
    """New payloads always carry a boolean, with conservative ambiguous failures."""
    engagement = result.get("engagement") or {}
    main = engagement.get("main") or {}
    rows = [main, *(main.get("variants_published") or []),
            *((engagement.get("replies") or {}).get("items") or [])]
    for row in rows:
        if type(row.get("publication_attempted")) is not bool:
            row["publication_attempted"] = (delivery_state(row) != "not_sent"
                                             if row.get("error") or row.get("success") or row.get("posted")
                                             else False)
    return result


def _save_result(db, task, url, result):
    from sqlalchemy.orm.attributes import flag_modified
    settings = deepcopy(task.settings or {})
    urls = dict(settings.get("url_results") or {})
    urls[url] = merge_url_result(urls.get(url), result)
    settings["url_results"] = urls
    task.settings = settings
    flag_modified(task, "settings")
    db.commit()


def _claim_resume(db, task, url, action, intent, box):
    from services.task_replay import reply_resume_plan
    if task.status == "stopped" or (task.settings or {}).get("stopped_by_user"):
        db.commit()
        return
    plan = reply_resume_plan(task, url)
    box["claimed"] = bool(plan and any(action_key(a) == action_key(action) for a in plan["actions"]))
    if box["claimed"]:
        _save(db, task, url, "reply", intent)
    else:
        db.commit()


async def execute_reply_resume(db, task, url, *, pool_owner_user_id=None, stop_requested=None):
    """Reuse the normal reply executor without republishing existing parents."""
    from services.visibility_checkpoint import run_visibility_checkpoint
    from services.task_replay import reply_resume_plan
    from executor.engagement_engine import execute_engagement_only_replies_via_full_flow
    from uuid import uuid4
    plan = task.settings["_reply_resume"][url]
    # Seed receipts before any new work so a crash retains the source order.
    await run_visibility_checkpoint(db, task, _save_result, url, plan["source"])
    for action in plan["actions"]:
        if stop_requested and stop_requested():
            break
        current = ((task.settings or {}).get("url_results") or {}).get(url) or {}
        # Re-plan from the latest durable state; a restarted unknown intent stays blocked.
        safe = reply_resume_plan({"id": task.id, "kind": task.kind,
                                  "settings": {**task.settings, "url_results": {url: current}}}, url)
        if not safe or not any(action_key(a) == action_key(action) for a in safe["actions"]):
            continue
        token = uuid4().hex
        intent = {**action, "publication_attempted": True, "submitted_unconfirmed": True,
                  "success": False, "_resume_intent": True, "_resume_attempt_token": token,
                  "error": "resume_delivery_unconfirmed"}
        claim = {}
        await run_visibility_checkpoint(db, task, _claim_resume, url, action, intent, claim)
        if not claim.get("claimed"):
            continue
        outcomes, _, _ = await execute_engagement_only_replies_via_full_flow(
            db, current.get("resolved_video_url") or url, task_id=task.id,
            comment_text=action.get("matched_main_text") or "", comment_id=action["parent_comment_id"],
            comment_author=action.get("matched_parent_author") or action.get("requested_parent_author"),
            replies=[action["text"]], variant_indices=[action["variant_index"]],
            pool_owner_user_id=pool_owner_user_id, stop_requested=stop_requested)
        if len(outcomes) != 1:
            continue  # Retain intent; never guess after an interrupted/malformed result.
        outcome = {**outcomes[0], "parent_comment_id": action["parent_comment_id"],
                   "variant_index": action["variant_index"], "_resume_intent": False,
                   "_resume_attempt_token": token}
        outcome.setdefault("submitted_unconfirmed", False)
        outcome["attempts"] = int(action.get("attempts") or 0) + int(outcome.get("attempts") or 0)
        observed_parent = outcomes[0].get("parent_comment_id")
        if valid_id(observed_parent) and str(observed_parent) != action["parent_comment_id"]:
            outcome.update(success=False, submitted_unconfirmed=True, error="resume_parent_mismatch",
                           observed_parent_comment_id=str(observed_parent))
        await run_visibility_checkpoint(db, task, _save, url, "reply", outcome)
    result = deepcopy(task.settings["url_results"][url])
    replies = result["engagement"]["replies"]
    expected = int(replies.get("expected_submissions") or replies.get("expected") or len(replies["items"]))
    complete = len({action_key(r) for r in replies["items"] if r.get("success")}) >= expected and all(r.get("success") for r in replies["items"])
    replies["complete"] = complete
    result["publication_complete"] = complete and (result["engagement"].get("main") or {}).get("complete", True)
    result["status"] = result["core_status"] = "success" if result["publication_complete"] else "partial"
    result["error"] = None if complete else "reply_publication_incomplete"
    result.pop("execution_ledger", None)
    return result


class ResumedReplyResult:
    def __init__(self, result):
        self.result = result

    def to_dict(self, **kwargs):
        return deepcopy(self.result)
