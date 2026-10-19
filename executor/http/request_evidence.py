"""Observe signature presence on the dispatched request without retaining tokens."""
from contextlib import contextmanager
from urllib.parse import parse_qs, urlparse


SECURITY_KEYS = ("X-Bogus", "X-Gnarly", "a_bogus", "msToken")


def signature_presence(url):
    query = parse_qs(urlparse(str(url or "")).query)
    return {key: bool(query.get(key)) for key in SECURITY_KEYS}


@contextmanager
def observe_request_security(page, expected_url, method="GET"):
    expected = urlparse(expected_url)
    wanted = parse_qs(expected.query)
    evidence = {}

    def on_request(request):
        try:
            actual = urlparse(str(getattr(request, "url", "")))
            if (str(getattr(request, "method", "")).upper() != method.upper()
                    or (actual.scheme, actual.hostname, actual.path) != (expected.scheme, expected.hostname, expected.path)):
                return
            query = parse_qs(actual.query)
            if any(wanted.get(key) != query.get(key) for key in ("aweme_id", "item_id", "comment_id", "cursor") if key in wanted):
                return
            # Preserve the original dispatch when a redirect strips its query.
            evidence.setdefault("request_security_params", signature_presence(request.url))
        except (TypeError, ValueError):
            return

    attached = callable(getattr(page, "on", None))
    if attached:
        page.on("request", on_request)
    try:
        yield evidence
    finally:
        if attached and callable(getattr(page, "remove_listener", None)):
            page.remove_listener("request", on_request)


def resolve_security_evidence(evidence, response_url):
    response = signature_presence(response_url)
    request = evidence.get("request_security_params")
    return {
        "request_security_params": request,
        "response_security_params": response,
        "security_params": request if request is not None else response,
        "security_params_source": "request_event" if request is not None else "response_url",
    }
