"""Keep order configuration separate from server-owned execution evidence."""
from copy import deepcopy

EXECUTION_FIELDS = frozenset({
    "url_results", "execution_ledger", "successful_urls", "failed_urls", "unconfirmed_urls",
    "submitted_unconfirmed_urls", "keyword_scans", "keyword_video_bindings", "quantity_slots",
    "publication_complete", "reply_likes", "execution_attempts", "attempts_summary", "timeout_stopped", "timeout_limit_seconds",
    "stopped_at", "stopped_by_user", "stop_requested", "force_close_browsers",
    "cancelled_by_user", "cancelled_at", "cancel_reason", "finished_at", "started_at", "last_error",
})


def clean_order_configuration(value):
    return {key: deepcopy(item) for key, item in (value or {}).items()
            if key not in EXECUTION_FIELDS and not str(key).startswith("_")}
