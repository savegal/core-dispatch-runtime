"""Shared configuration helpers for TikTok comment likes."""

from __future__ import annotations

from typing import Any

BOOST_LIKES_ENABLED_KEY = "boost_likes_enabled"
BOOST_LIKES_QUANTITY_KEY = "boost_likes_quantity"
BOOST_LIKES_DEFAULT = 100
BOOST_LIKES_MIN = 50
BOOST_LIKES_MAX = 5000


def coerce_bool(value: Any, default: bool) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        normalized = value.strip().lower()
        if normalized in {"1", "true", "yes", "on", "y"}:
            return True
        if normalized in {"0", "false", "no", "off", "n"}:
            return False
    if isinstance(value, (int, float)):
        return bool(value)
    return default


def coerce_likes_quantity(value: Any, default: int = BOOST_LIKES_DEFAULT) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        try:
            parsed = int(default)
        except (TypeError, ValueError):
            parsed = BOOST_LIKES_DEFAULT
    return max(BOOST_LIKES_MIN, min(BOOST_LIKES_MAX, parsed))


def provider_available(api_key: object) -> bool:
    return bool(str(api_key or "").strip())
