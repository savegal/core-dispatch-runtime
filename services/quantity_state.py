"""Read durable logical slots even when a worker stopped before its final report."""
from copy import deepcopy
from db.models import QuantityAction


RETRYABLE_QUANTITY_STATES = ("pending", "not_sent", "absent", "rejected")


def serialize_actions(actions):
    return [{key: getattr(action, key) for key in ("id", "kind", "slot", "parent_action_id", "parent_id",
        "text", "state", "comment_id", "account_id", "error_code")} for action in actions]


def quantity_settings_snapshot(db, task):
    settings = deepcopy(task.settings or {})
    if not settings.get("publication_plan"):
        return settings
    rows = db.query(QuantityAction).filter(QuantityAction.task_id == task.id).all()
    urls = settings.setdefault("url_results", {})
    for url in task.video_urls or []:
        actions = [action for action in rows if action.video_url == url]
        if actions:
            row = urls.setdefault(url, {"status": "partial"})
            row["quantity_slots"] = serialize_actions(actions)
            # Late read-only checks live in SQL, not in the sender's old JSON.
            # Project positive proof for the publication map without rebilling
            # or rewriting the historical terminal task status.
            proven = {action.comment_id for action in actions if action.state == "visible" and action.comment_id}
            engagement = row.get("engagement") or {}
            mains = (engagement.get("main") or {}).get("variants_published") or []
            replies = (engagement.get("replies") or {}).get("items") or []
            for item in [*mains, *replies]:
                cid = str(item.get("reply_comment_id") or item.get("reply_id") or item.get("comment_id") or "")
                if cid in proven:
                    item.update(public_confirmed=True, visibility_status="public_confirmed")
            plan = settings["publication_plan"]
            for kind, report_key, expected in (("main", "main_visibility", plan["main_count"]),
                    ("reply", "reply_publication_visibility", plan["main_count"] * plan["replies_per_main"])):
                confirmed = sum(action.state == "visible" and action.kind == kind for action in actions)
                row[report_key] = {**(row.get(report_key) or {}), "enabled": bool(expected), "expected": expected,
                    "confirmed": confirmed, "status": "confirmed" if confirmed >= expected else "unconfirmed"}
    return settings
