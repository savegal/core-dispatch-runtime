"""Bounded, process-local cache of public CDN scripts and stylesheets."""
from collections import OrderedDict
from dataclasses import dataclass
from threading import RLock
from urllib.parse import urlsplit

from config.settings import get_settings


_CDN_DOMAINS = (
    "tiktokcdn.com", "tiktokcdn-us.com", "tiktokcdn-eu.com", "ibytedtos.com",
    "ttwstatic.com", "byteoversea.com", "muscdn.com",
)
_DYNAMIC_MARKERS = (
    "/api/", "/passport/", "/aweme/", "mcs", "mon.", "log.", "mstoken",
    "x-bogus", "x-gnarly", "ttwid", "sessionid",
)


def is_cacheable_static_asset(method: str, resource_type: str, url: str) -> bool:
    if method.upper() != "GET" or resource_type not in ("script", "stylesheet"):
        return False
    try:
        parsed = urlsplit(url)
        host = (parsed.hostname or "").lower()
        return (
            parsed.scheme in ("http", "https")
            and any(host == domain or host.endswith("." + domain) for domain in _CDN_DOMAINS)
            and parsed.path.lower().endswith((".js", ".css"))
            and not any(marker in url.lower() for marker in _DYNAMIC_MARKERS)
        )
    except ValueError:
        return False


@dataclass(frozen=True)
class StaticAsset:
    status: int
    headers: dict[str, str]
    body: bytes


class StaticAssetCache:
    def __init__(self, max_total_bytes=None, max_item_bytes=8 * 1024 * 1024):
        self.max_total_bytes = max(0, int(
            get_settings().STATIC_ASSET_CACHE_MAX_MB * 1024 * 1024
            if max_total_bytes is None else max_total_bytes
        ))
        self.max_item_bytes = max_item_bytes
        self._entries = OrderedDict()
        self._total_bytes = 0
        self._lock = RLock()

    @property
    def total_bytes(self):
        with self._lock:
            return self._total_bytes

    def has(self, url):
        with self._lock:
            return url in self._entries

    def get(self, url):
        with self._lock:
            entry = self._entries.get(url)
            if entry is not None:
                self._entries.move_to_end(url)
            return entry

    def put(self, url, status, headers, body):
        try:
            resource_type = "stylesheet" if urlsplit(url).path.lower().endswith(".css") else "script"
        except ValueError:
            return False
        if (status != 200 or not is_cacheable_static_asset("GET", resource_type, url)
                or not 64 <= len(body) <= min(self.max_item_bytes, self.max_total_bytes)):
            return False
        normalized = {str(key).lower(): str(value) for key, value in headers.items()}
        # response.body() is decoded. Never replay compression, lengths, or
        # account cookies from the original network response into another context.
        for key in ("content-encoding", "content-length", "transfer-encoding", "set-cookie", "connection"):
            normalized.pop(key, None)
        normalized.setdefault("content-type", "text/css; charset=utf-8" if resource_type == "stylesheet"
                              else "application/javascript; charset=utf-8")
        normalized.update({
            "access-control-allow-origin": "*",
            "access-control-allow-credentials": "true",
            "timing-allow-origin": "*",
            "cache-control": "public, max-age=31536000, immutable",
        })
        entry = StaticAsset(status, normalized, bytes(body))
        with self._lock:
            previous = self._entries.pop(url, None)
            if previous is not None:
                self._total_bytes -= len(previous.body)
            while self._entries and self._total_bytes + len(body) > self.max_total_bytes:
                _, evicted = self._entries.popitem(last=False)
                self._total_bytes -= len(evicted.body)
            self._entries[url] = entry
            self._total_bytes += len(body)
        return True


static_asset_cache = StaticAssetCache()
