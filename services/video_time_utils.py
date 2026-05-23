from __future__ import annotations

from datetime import datetime, timezone
from typing import Any


def coerce_unix_timestamp(value: Any) -> int | None:
    if value is None or value is False:
        return None
    if isinstance(value, datetime):
        dt = value if value.tzinfo else value.replace(tzinfo=timezone.utc)
        return int(dt.timestamp())
    try:
        if isinstance(value, str):
            raw = value.strip()
            if not raw:
                return None
            if any(marker in raw for marker in ("T", "-", ":")):
                dt = datetime.fromisoformat(raw.replace("Z", "+00:00"))
                if dt.tzinfo is None:
                    dt = dt.replace(tzinfo=timezone.utc)
                return int(dt.timestamp())
            numeric = float(raw)
        else:
            numeric = float(value)
    except (TypeError, ValueError):
        return None

    if numeric <= 0:
        return None
    if numeric > 10_000_000_000_000:
        numeric = numeric / 1_000_000
    elif numeric > 10_000_000_000:
        numeric = numeric / 1_000
    return int(numeric)


def create_time_from_meta(meta: Any) -> int | None:
    if not isinstance(meta, dict):
        return None
    for key in ("create_time", "createTime", "createdAt", "created_at", "publish_time", "published_at"):
        ts = coerce_unix_timestamp(meta.get(key))
        if ts is not None:
            return ts
    return None


def published_at_iso_from_create_time(create_time: Any) -> str | None:
    ts = coerce_unix_timestamp(create_time)
    if ts is None:
        return None
    return datetime.fromtimestamp(ts, tz=timezone.utc).isoformat()
