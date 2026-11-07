"""Account replacement before submission does not consume a publish attempt."""


def candidate_scan_limit(settings) -> int:
    """Monotonic candidate cap, independent of the budget for real attempts."""
    try:
        return max(1, int(getattr(settings, "MAX_ACCOUNT_CANDIDATES", 50) or 50))
    except (TypeError, ValueError):
        return 50


def is_pre_publish_account_failure(reason, *, publication_attempted=None) -> bool:
    if publication_attempted:
        return False
    # An explicit submission flag is stronger evidence than an error string.
    # Keep the marker fallback for legacy callers with no submission metadata.
    if publication_attempted is False:
        return True
    value = str(reason or "").lower()
    return any(marker in value for marker in (
        "dead_session", "visible_login_button", "visible_login_ui", "account_login_failed",
        "proxy_edge_access_denied", "goto_failed", "captcha_challenge", "cookies_expired",
        "warmup_exception", "parent_api_unavailable", "http_session_not_ready",
        "http_preflight", "proxy_route:",
    ))


def track_publication_requests(page, state, *, flag="publication_attempted", expected_url=None):
    """Record actual POST dispatch even if the surrounding coroutine times out."""
    from urllib.parse import urlparse
    from executor.http.request_evidence import same_http_endpoint
    from services.publication_journal import capture_dispatch_callback
    dispatched = capture_dispatch_callback()

    def on_request(request):
        if str(getattr(request, "method", "")).upper() != "POST":
            return
        url = str(getattr(request, "url", ""))
        if expected_url is not None and not same_http_endpoint(url, expected_url):
            return
        parsed = urlparse(url)
        host = (parsed.hostname or "").lower()
        publish = parsed.path.rstrip("/") == "/api/comment/publish"
        if (publish or expected_url is not None) and (host == "tiktok.com" or host.endswith(".tiktok.com")):
            state[flag] = True
            if publish:
                dispatched()
                page._publication_attempted = True

    if not callable(getattr(page, "on", None)):
        return lambda: None
    page.on("request", on_request)

    def detach():
        if callable(getattr(page, "remove_listener", None)):
            page.remove_listener("request", on_request)
    return detach


def pre_publish_outcomes(outcomes) -> bool:
    rows = list(outcomes or [])
    return bool(rows) and all(is_pre_publish_account_failure(
        getattr(row, "error", None), publication_attempted=(
            getattr(row, "attempted", None) if getattr(row, "publication_attempted", None) is None
            else row.publication_attempted
        ),
    ) for row in rows)
