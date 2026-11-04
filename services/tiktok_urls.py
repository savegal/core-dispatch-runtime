"""Shared URL boundaries for orders and TikTok browser navigation."""

import re
from urllib.parse import urlsplit


_MEDIA_PATH = re.compile(r"/(?:@[\w.-]+/)?(?:video|photo)/([0-9]+)/?", re.ASCII | re.IGNORECASE)
_SHORT_PATH = re.compile(r"/[A-Za-z0-9_-]+/?")
_SHARE_PATH = re.compile(r"/(?:t/[A-Za-z0-9_-]+|Z[A-Za-z0-9_-]+)/?")


def parse_tiktok_url(url: str | None):
    if not isinstance(url, str) or not url or "\\" in url or any(ord(char) <= 32 or ord(char) == 127 for char in url):
        return None
    try:
        parsed = urlsplit(url)
        host = parsed.hostname or ""
        if parsed.scheme not in {"http", "https"} or parsed.username is not None:
            return None
        if parsed.port not in {None, 443 if parsed.scheme == "https" else 80}:
            return None
        if re.fullmatch(r"(?:[a-z0-9-]+\.)*tiktok\.com", host):
            return parsed
    except ValueError:
        pass
    return None


def media_id_from_url(url: str | None, *, allow_relative: bool = False) -> str | None:
    if allow_relative and isinstance(url, str) and url.startswith("/") and not url.startswith("//"):
        url = "https://www.tiktok.com" + url
    parsed = parse_tiktok_url(url)
    match = _MEDIA_PATH.fullmatch(parsed.path) if parsed else None
    return match[1] if match and match[1].strip("0") else None


def is_tiktok_shortlink(url: str | None) -> bool:
    parsed = parse_tiktok_url(url)
    if parsed is None:
        return False
    return bool(
        _SHARE_PATH.fullmatch(parsed.path)
        or (parsed.hostname in {"vt.tiktok.com", "vm.tiktok.com"} and _SHORT_PATH.fullmatch(parsed.path))
    )
