"""
Proxy normalization utilities.
Ensures consistent proxy format with explicit scheme.
"""
from __future__ import annotations

from urllib.parse import urlparse, quote, unquote
from dataclasses import dataclass, field
from typing import Optional
import socket

from config.settings import get_settings


SUPPORTED_PROXY_SCHEMES = ("http", "https", "socks5", "socks5h")
DEFAULT_PROXY_SCHEME = "http"


class ProxyRouteError(RuntimeError):
    """A route failed before any TikTok publication could be dispatched."""
    publication_attempted = False
    definitive = False

    def __init__(self, reason: str):
        self.reason = reason
        super().__init__(f"proxy_route:{reason}")


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
    raw = dedicated.proxy if dedicated else getattr(account, "proxy", None)
    route = ProxyRoute.parse(raw)
    country = dedicated.country_code if dedicated else getattr(account, "proxy_country_code", None)
    proxy_id = dedicated.proxy_id if dedicated else None
    if account is not None and dedicated is None:
        from sqlalchemy.orm import object_session
        from sqlalchemy import or_
        from db.models import Account, Proxy
        session = object_session(account) if isinstance(account, Account) else None
        if session is not None:
            proxy = session.query(Proxy).filter(or_(Proxy.proxy == raw, Proxy.proxy == route.url)).first()
            if proxy is not None:
                country, proxy_id = proxy.country_code, proxy.id
    country = str(country or "").strip().upper() or None
    if account is not None:
        account.proxy_country_code = country
    return replace(route, country_code=country, proxy_id=proxy_id)


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
