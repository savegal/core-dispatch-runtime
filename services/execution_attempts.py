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


# Only connection-setup errors proving that the origin never received a POST.
# Timeout, reset, abort and generic failed fetch remain ambiguous.
_PRE_DISPATCH_NETWORK_FAILURES = frozenset({
    "ERR_PROXY_CONNECTION_FAILED",
    "ERR_SOCKS_CONNECTION_FAILED",
    "ERR_TUNNEL_CONNECTION_FAILED",
    "ERR_NO_SUPPORTED_PROXIES",
    "ERR_PROXY_AUTH_UNSUPPORTED",
    "ERR_CONNECTION_REFUSED",
    "ERR_NAME_NOT_RESOLVED",
})


def track_publication_requests(
    page, state, *, flag="publication_attempted", expected_url=None,
    track_failures=False,
):
    """Track matching POSTs and optionally identify proven pre-connect failures.

    The request event means Chromium initiated a request, not that TikTok
    received it. A generic requestfailed is also not proof of non-delivery.
    Only narrowly identified connection-setup failures cancel dispatch evidence.
    """
    from urllib.parse import urlparse
    from executor.http.request_evidence import same_http_endpoint

    tracked = {}
    safe_failed = set()
    responded = set()
    page_was_attempted = bool(getattr(page, "_publication_attempted", False))

    def match(request):
        if str(getattr(request, "method", "")).upper() != "POST":
            return False
        url = str(getattr(request, "url", ""))
        if expected_url is not None and not same_http_endpoint(url, expected_url):
            return False
        parsed = urlparse(url)
        host = (parsed.hostname or "").lower()
        publish = parsed.path.rstrip("/") == "/api/comment/publish"
        return (publish or expected_url is not None) and (host == "tiktok.com" or host.endswith(".tiktok.com"))

    def on_request(request):
        if not match(request):
            return
        tracked[id(request)] = request
        state[flag] = True
        state.pop("request_pre_dispatch_failed", None)
        if urlparse(str(getattr(request, "url", ""))).path.rstrip("/") == "/api/comment/publish":
            page._publication_attempted = True

    def on_response(response):
        request = getattr(response, "request", None)
        if id(request) in tracked:
            responded.add(id(request))

    def on_requestfailed(request):
        key = id(request)
        if key not in tracked:
            return
        failure = getattr(request, "failure", None)
        if callable(failure):
            try:
                failure = failure()
            except Exception:
                failure = None
        failure_text = str(failure or "")
        state["request_failure"] = failure_text[:200]
        code = failure_text.upper()
        proven_pre_dispatch = (
            key not in responded
            and any(marker in code for marker in _PRE_DISPATCH_NETWORK_FAILURES)
        )
        if proven_pre_dispatch:
            safe_failed.add(key)
        else:
            safe_failed.discard(key)
        # Never clear evidence from a response or another uncertain POST.
        if tracked and len(safe_failed) == len(tracked) and not responded:
            state[flag] = False
            state["request_pre_dispatch_failed"] = True
            if not page_was_attempted:
                page._publication_attempted = False

    if not callable(getattr(page, "on", None)):
        return lambda: None
    page.on("request", on_request)
    if track_failures:
        page.on("response", on_response)
        page.on("requestfailed", on_requestfailed)

    def detach():
        if callable(getattr(page, "remove_listener", None)):
            page.remove_listener("request", on_request)
            if track_failures:
                page.remove_listener("response", on_response)
                page.remove_listener("requestfailed", on_requestfailed)
    return detach


def pre_publish_outcomes(outcomes) -> bool:
    rows = list(outcomes or [])
    return bool(rows) and all(is_pre_publish_account_failure(
        getattr(row, "error", None), publication_attempted=(
            getattr(row, "attempted", None) if getattr(row, "publication_attempted", None) is None
            else row.publication_attempted
        ),
    ) for row in rows)
