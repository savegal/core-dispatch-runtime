"""
Proxy normalization utilities.
Ensures consistent proxy format with explicit scheme.
"""
from __future__ import annotations

from urllib.parse import urlparse
from typing import Optional
import socket

from config.settings import get_settings


SUPPORTED_PROXY_SCHEMES = ("http", "https", "socks5", "socks5h")
DEFAULT_PROXY_SCHEME = "socks5"


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

    proxy = proxy.strip()
    default_scheme = _resolve_default_scheme(default_scheme)
    if "://" not in proxy:
        proxy = f"{default_scheme}://{proxy}"

    parsed = urlparse(proxy)
    scheme = (parsed.scheme or "").lower()
    if scheme not in SUPPORTED_PROXY_SCHEMES:
        return None

    if scheme == "socks5h":
        scheme = "socks5"

    host = parsed.hostname
    port = parsed.port
    if not host or not port:
        return None

    host = host.lower()
    if ":" in host and not host.startswith("["):
        host = f"[{host}]"

    username = parsed.username or ""
    password = parsed.password or ""
    auth = f"{username}:{password}@" if (username or password) else ""

    return f"{scheme}://{auth}{host}:{port}"


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
