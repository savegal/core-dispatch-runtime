"""Observe signature presence on the dispatched request without retaining tokens."""
from contextlib import contextmanager
from urllib.parse import parse_qs, urlparse


SECURITY_KEYS = ("X-Bogus", "X-Gnarly", "a_bogus", "msToken")


def has_request_signature(presence):
    """Read-path readiness signal; presence does not prove cryptographic validity."""
    return isinstance(presence, dict) and any(presence.get(key) for key in ("X-Bogus", "a_bogus"))


def signature_presence(url):
    query = parse_qs(urlparse(str(url or "")).query)
    return {key: bool(query.get(key)) for key in SECURITY_KEYS}


def same_http_endpoint(actual_url, expected_url):
    """Match origin and path independently of signatures and query order."""
    def endpoint(url):
        parsed = urlparse(str(url or ""))
        if parsed.scheme not in {"http", "https"} or not parsed.hostname or parsed.username is not None:
            return None
        default_port = 443 if parsed.scheme == "https" else 80
        return parsed.scheme, parsed.hostname, parsed.port if parsed.port is not None else default_port, parsed.path

    try:
        expected = endpoint(expected_url)
        return expected is not None and endpoint(actual_url) == expected
    except ValueError:
        return False


@contextmanager
def observe_request_security(page, expected_url, method="GET"):
    expected = urlparse(expected_url)
    wanted = parse_qs(expected.query)
    evidence = {}

    def on_request(request):
        try:
            actual = urlparse(str(getattr(request, "url", "")))
            if (str(getattr(request, "method", "")).upper() != method.upper()
                    or not same_http_endpoint(request.url, expected_url)):
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
