"""Canonical TikTok links for known publication IDs; no URL supplied as markup."""
import base64
import re
from urllib.parse import urlsplit


def _numeric_id(value, *, minimum=1):
    value = str(value or "").strip()
    return value if value.isascii() and value.isdigit() and minimum <= len(value) <= 30 and int(value) > 0 else None


def comment_link(video_id, cid, fallback=""):
    """Match TikTok's share links, including receipts that only saved a media URL."""
    cid = _numeric_id(cid, minimum=10)
    if not cid:
        return ""
    media_id = _numeric_id(video_id)
    if not media_id:
        try:
            parsed = urlsplit(str(fallback or ""))
            host = (parsed.hostname or "").lower()
            match = re.search(r"/(?:video|photo)/(\d{1,30})(?:/|$)", parsed.path)
            if parsed.scheme == "https" and (host == "tiktok.com" or host.endswith(".tiktok.com")) and match:
                media_id = _numeric_id(match[1])
        except ValueError:
            pass
    if not media_id:
        return ""
    encoded = base64.urlsafe_b64encode(cid.encode("ascii")).decode("ascii").rstrip("=")
    return f"https://www.tiktok.com/share/video/{media_id}?cid={encoded}"
