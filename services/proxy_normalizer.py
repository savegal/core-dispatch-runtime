"""
Proxy normalization utilities.
Ensures consistent proxy format with explicit scheme and supports per-account
sticky residential proxy templates (1 account = 1 unique sticky IP).
"""
from __future__ import annotations

from urllib.parse import urlparse, quote, unquote
from dataclasses import dataclass, field
from typing import Any, Optional
import hashlib
import re
import socket

from config.settings import get_settings


SUPPORTED_PROXY_SCHEMES = ("http", "https", "socks5", "socks5h")
DEFAULT_PROXY_SCHEME = "http"

_TEMPLATE_PLACEHOLDER_RE = re.compile(
    r"\{(?:session|session_id|sid|account_id|acc_id)\}",
    re.IGNORECASE,
)
_COUNTRY_PLACEHOLDER_RE = re.compile(r"\{(?:country|cc)\}", re.IGNORECASE)
_AUTO_SESSION_MARKER_RE = re.compile(
    r"(?:(?<=-session-)|(?<=_session-)|(?<=-sessid-)|(?<=_sessid-)|(?<=-sid-)|(?<=_sid-)|(?<=sessionid-))auto\b",
    re.IGNORECASE,
)


class ProxyRouteError(RuntimeError):
    """A route failed before any TikTok publication could be dispatched."""
    publication_attempted = False
    definitive = False

    def __init__(self, reason: str):
        self.reason = reason
        super().__init__(f"proxy_route:{reason}")


def is_sticky_residential_template(raw: str | None) -> bool:
    """Return True when a proxy string contains per-account sticky session placeholders."""
    text = str(raw or "").strip()
    if not text:
        return False
    return bool(_TEMPLATE_PLACEHOLDER_RE.search(text) or _AUTO_SESSION_MARKER_RE.search(text))


def _account_sticky_session_token(account: Any) -> str:
    """Generate a deterministic, alphanumeric sticky session token for one account."""
    if account is None:
        return "acc0"
    acc_id = getattr(account, "id", None)
    if acc_id is not None:
        return f"acc{acc_id}"
    seed = str(getattr(account, "label", "") or getattr(account, "cookies", "") or id(account))
    digest = hashlib.sha1(seed.encode("utf-8", errors="ignore")).hexdigest()[:10]
    return f"acc{digest}"


def _resolve_account_geo_code(account: Any, default_country: str | None = None) -> str:
    """Resolve 2-letter ISO country code for sticky residential proxy routing."""
    country = str(getattr(account, "country_code", None) or "").strip().upper()
    if len(country) == 2 and country.isalpha():
        return country
    if account is not None:
        try:
            from services.geo_detection_service import detect_account_country
            detected = detect_account_country(account)
            if detected and len(detected) == 2 and detected.isalpha() and detected.isascii():
                return detected.upper()
        except Exception:
            pass
    fallback = str(default_country or "").strip().upper()
    if len(fallback) == 2 and fallback.isalpha():
        return fallback
    try:
        settings_cc = str(getattr(get_settings(), "RESIDENTIAL_DEFAULT_COUNTRY", "DE") or "DE").strip().upper()
        if len(settings_cc) == 2 and settings_cc.isalpha():
            return settings_cc
    except Exception:
        pass
    return "DE"


def resolve_account_sticky_proxy(
    raw: str | None,
    account: Any = None,
    *,
    default_country: str | None = None,
) -> str | None:
    """Expand per-account sticky session & geo placeholders in a residential proxy string.

    Supports:
    - Explicit placeholders: {session}, {session_id}, {sid}, {account_id}, {acc_id}, {country}, {cc}, {COUNTRY}, {CC}
    - Provider auto-session markers: -session-auto, _session-auto, -sessid-auto, -sid-auto, sessionid-auto
    Non-template proxy strings are returned unchanged.
    """
    if raw is None:
        return None
    text = str(raw).strip()
    if not text or not (is_sticky_residential_template(text) or _COUNTRY_PLACEHOLDER_RE.search(text)):
        return raw

    session_token = _account_sticky_session_token(account)
    acc_id_str = str(getattr(account, "id", None) or session_token)
    country_upper = _resolve_account_geo_code(account, default_country=default_country)
    country_lower = country_upper.lower()

    replacements = {
        "session": session_token,
        "session_id": session_token,
        "sid": session_token,
        "account_id": acc_id_str,
        "acc_id": acc_id_str,
    }
    # The recognizer is case-insensitive; the expander must accept the very
    # same inputs (including provider exports with mixed-case placeholders).
    resolved = _TEMPLATE_PLACEHOLDER_RE.sub(
        lambda m: replacements[m.group()[1:-1].lower()], text
    )
    resolved = _COUNTRY_PLACEHOLDER_RE.sub(
        lambda m: country_upper if m.group()[1:-1].isupper() else country_lower,
        resolved,
    )
    resolved = _AUTO_SESSION_MARKER_RE.sub(session_token, resolved)
    return resolved


def account_sticky_proxy_matches_template(raw: str | None, account: Any, resolved: str | None) -> bool:
    """Match an existing account-bound route even if its country segment changed.

    Never interpret country-only templates as sticky. Session markers MUST match
    this account's deterministic token/id; only the ISO country field is flexible.
    This lets rebalance preserve a previously resolved route when new geographic
    evidence changes the account's inferred country.
    """
    template = str(raw or "").strip()
    current = str(resolved or "").strip()
    if not template or not current or not is_sticky_residential_template(template):
        return False

    session = _account_sticky_session_token(account)
    acc_id = str(getattr(account, "id", None) or session)
    tokens = {
        "session": session,
        "session_id": session,
        "sid": session,
        "account_id": acc_id,
        "acc_id": acc_id,
    }
    parts = []
    start = 0
    marker = re.compile(
        _TEMPLATE_PLACEHOLDER_RE.pattern
        + "|" + _COUNTRY_PLACEHOLDER_RE.pattern
        + "|" + _AUTO_SESSION_MARKER_RE.pattern,
        re.IGNORECASE,
    )
    for match in marker.finditer(template):
        parts.append(re.escape(template[start:match.start()]))
        key = match.group()
        if key.startswith("{"):
            key = key[1:-1].lower()
            parts.append(r"[A-Za-z]{2}" if key in {"cc", "country"}
                         else re.escape(tokens[key]))
        else:
            parts.append(re.escape(session))
        start = match.end()
    parts.append(re.escape(template[start:]))
    return re.fullmatch("".join(parts), current) is not None


@dataclass(frozen=True)
class ProxyRoute:
    scheme: str
    host: str
    port: int
    username: str | None = field(default=None, repr=False)
    password: str | None = field(default=None, repr=False)
    country_code: str | None = None
    proxy_id: int | None = None

    @classmethod
    def parse(cls, raw: str, *, default_scheme: str | None = None, **metadata) -> "ProxyRoute":
        raw = str(raw or "").strip()
        if not raw:
            raise ProxyRouteError("missing")
        if "://" not in raw:
            # Legacy provider export: host:port:user:pass (password may contain ':').
            pieces = raw.split(":", 3)
            if len(pieces) == 4 and pieces[1].isdigit() and "@" not in pieces[2]:
                host, port, user, password = pieces
                user, password = unquote(user), unquote(password)
                raw = f"{quote(user, safe='')}:{quote(password, safe='')}@{host}:{port}"
            raw = f"{_resolve_default_scheme(default_scheme)}://{raw}"
        try:
            # Split credentials before urlparse: raw '#'/'?'/'/' in a provider
            # password belong to userinfo, not to a fragment/query/path.
            scheme_part, authority = raw.split("://", 1)
            if "@" in authority:
                userinfo, address = authority.rsplit("@", 1)
                user, separator, password = userinfo.partition(":")
                encoded = quote(unquote(user), safe="")
                if separator:
                    encoded += ":" + quote(unquote(password), safe="")
                raw = f"{scheme_part}://{encoded}@{address}"
            parsed = urlparse(raw)
            scheme = parsed.scheme.lower()
            if scheme == "socks5h":
                scheme = "socks5"
            if scheme not in SUPPORTED_PROXY_SCHEMES or not parsed.hostname or not parsed.port:
                raise ValueError()
            if parsed.path not in ("", "/") or parsed.query or parsed.fragment:
                raise ValueError()
            return cls(scheme, parsed.hostname.lower(), parsed.port,
                       unquote(parsed.username) if parsed.username is not None else None,
                       unquote(parsed.password) if parsed.password is not None else None, **metadata)
        except (TypeError, ValueError) as exc:
            raise ProxyRouteError("invalid_format") from exc

    @property
    def server(self) -> str:
        host = f"[{self.host}]" if ":" in self.host else self.host
        return f"{self.scheme}://{host}:{self.port}"

    @property
    def url(self) -> str:
        if self.username is None and self.password is None:
            return self.server
        auth = f"{quote(self.username or '', safe='')}:{quote(self.password or '', safe='')}@"
        return self.server.replace("://", f"://{auth}", 1)

    def playwright(self) -> dict:
        result = {"server": self.server}
        if self.username is not None or self.password is not None:
            result.update(username=self.username or "", password=self.password or "")
        return result


def account_proxy_route(account, *, required: bool | None = None) -> ProxyRoute | None:
    """Resolve metadata once, before configuring either HTTP or browser transport."""
    from dataclasses import replace
    from services.dedicated_proxy_service import get_current_dedicated_proxy
    dedicated = get_current_dedicated_proxy()
    required = bool(get_settings().USE_PROXY or dedicated) if required is None else required
    if not required:
        return None
    raw_original = dedicated.proxy if dedicated else getattr(account, "proxy", None)
    default_cc = dedicated.country_code if dedicated else getattr(account, "proxy_country_code", None)
    raw = resolve_account_sticky_proxy(raw_original, account, default_country=default_cc)
    route = ProxyRoute.parse(raw)
    country = dedicated.country_code if dedicated else getattr(account, "proxy_country_code", None)
    proxy_id = dedicated.proxy_id if dedicated else None
    if account is not None and dedicated is None:
        from sqlalchemy.orm import object_session
        from db.models import Account
        session = object_session(account) if isinstance(account, Account) else None
        if session is not None:
            proxy = find_account_proxy_record(session, account)
            if proxy is not None:
                country, proxy_id = proxy.country_code, proxy.id
    if not country and is_sticky_residential_template(raw_original):
        country = _resolve_account_geo_code(account, default_country=default_cc)
    country = str(country or "").strip().upper() or None
    if account is not None:
        account.proxy_country_code = country
    return replace(route, country_code=country, proxy_id=proxy_id)


def find_account_proxy_record(db, account):
    """Find the Proxy row even when Account.proxy contains expanded placeholders.

    Prefer exact addresses and only scan provider templates when an exact row is
    absent, so direct proxies retain their original matching semantics.
    """
    from sqlalchemy import func, or_
    from db.models import Proxy

    current = str(getattr(account, "proxy", "") or "").strip()
    if not current:
        return None
    direct = db.query(Proxy).filter(Proxy.proxy == current).first()
    if direct is not None:
        return direct
    candidates = db.query(Proxy).filter(or_(
        Proxy.proxy.contains("{"),
        func.lower(Proxy.proxy).contains("session-auto"),
        func.lower(Proxy.proxy).contains("sessid-auto"),
        func.lower(Proxy.proxy).contains("sid-auto"),
    )).all()
    for proxy in candidates:
        if (resolve_account_sticky_proxy(proxy.proxy, account, default_country=proxy.country_code) == current
                or account_sticky_proxy_matches_template(proxy.proxy, account, current)):
            return proxy
    return None


def _resolve_default_scheme(default_scheme: str | None) -> str:
    if default_scheme:
        return default_scheme
    try:
        settings = get_settings()
        return getattr(settings, "DEFAULT_PROXY_SCHEME", DEFAULT_PROXY_SCHEME)
    except Exception:
        return DEFAULT_PROXY_SCHEME


def _resolve_detect_timeout(timeout: float | None) -> float:
    if timeout is not None:
        return timeout
    try:
        settings = get_settings()
        return float(getattr(settings, "PROXY_SCHEME_DETECT_TIMEOUT", 1.5))
    except Exception:
        return 1.5


def normalize_proxy_string(proxy: str, default_scheme: str | None = None) -> Optional[str]:
    """
    Normalizes proxy string to explicit scheme://[user:pass@]host:port format.

    - If scheme is missing, defaults to http://
    - socks5h is normalized to socks5 for compatibility
    - Returns None if format is invalid
    """
    if not proxy or not proxy.strip():
        return None

    try:
        return ProxyRoute.parse(proxy, default_scheme=default_scheme).url
    except ProxyRouteError:
        return None


def detect_proxy_scheme_sync(proxy: str, timeout: float | None = None) -> Optional[str]:
    """
    Best-effort detection of proxy scheme (http vs socks5) for raw strings without scheme.
    Returns "socks5", "http", or None if undetermined.
    """
    if not proxy or not proxy.strip():
        return None

    raw = proxy.strip()
    if "://" in raw:
        parsed = urlparse(raw)
        scheme = (parsed.scheme or "").lower()
        if scheme == "socks5h":
            scheme = "socks5"
        return scheme if scheme in SUPPORTED_PROXY_SCHEMES else None

    timeout = _resolve_detect_timeout(timeout)
    normalized = normalize_proxy_string(raw, default_scheme=_resolve_default_scheme(None))
    if not normalized:
        return None

    parsed = urlparse(normalized)
    host = parsed.hostname
    port = parsed.port
    if not host or not port:
        return None

    sock = None
    try:
        sock = socket.create_connection((host, port), timeout=timeout)
        sock.settimeout(timeout)
        username = parsed.username or ""
        password = parsed.password or ""
        has_auth = bool(username or password)
        methods = b"\x00\x02" if has_auth else b"\x00"
        sock.sendall(b"\x05" + bytes([len(methods)]) + methods)
        resp = sock.recv(2)
        if len(resp) >= 2 and resp[0] == 0x05:
            return "socks5"
        return "http"
    except Exception:
        return None
    finally:
        if sock:
            try:
                sock.close()
            except Exception:
                pass
