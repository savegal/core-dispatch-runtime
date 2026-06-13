"""
Client for the TikTok comment likes API.

Docs: https://boost-project.pro/api-docs
Service #5 accepts link, username and quantity. Local product range is 50-5000.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any, Optional, Tuple

import httpx

from config.settings import get_settings
from services.likes_config import coerce_likes_quantity

logger = logging.getLogger("services.boost_project")

RETRY_STATUS_CODES = {429, 500, 502, 503, 504}


def _base_url() -> str:
    s = get_settings()
    return (s.BOOST_PROJECT_BASE_URL or "https://boost-project.pro/api/v1").rstrip("/")


def _api_key() -> str:
    return (get_settings().BOOST_PROJECT_API_KEY or "").strip()


def is_boost_enabled() -> bool:
    """Return True when the likes API key is configured."""
    return bool(_api_key())


def _coerce_likes_quantity(value: object) -> int:
    return coerce_likes_quantity(
        value,
        get_settings().BOOST_PROJECT_LIKES_QUANTITY,
    )


def _api_error(data: dict[str, Any], raw: str) -> str:
    for key in ("error", "message", "msg", "detail"):
        value = data.get(key)
        if value:
            return str(value)[:300]
    raw = (raw or "").strip()
    if raw and not raw.lower().startswith(("<!doctype", "<html")):
        return raw[:300]
    return ""


def _extract_order_id(data: dict[str, Any]) -> Optional[int]:
    for key in ("order", "order_id", "id"):
        value = data.get(key)
        if value is None:
            continue
        try:
            return int(value)
        except (TypeError, ValueError):
            continue
    return None


async def _request_json_with_retries(
    client: httpx.AsyncClient,
    method: str,
    url: str,
    *,
    params: Optional[dict[str, Any]] = None,
    data: Optional[dict[str, Any]] = None,
    attempts: int = 4,
) -> tuple[int, dict[str, Any], str]:
    last_status = 0
    last_data: dict[str, Any] = {}
    last_raw = ""
    for attempt in range(1, attempts + 1):
        try:
            response = await client.request(method, url, params=params, data=data)
            last_status = response.status_code
            last_raw = response.text or ""
            try:
                parsed = response.json()
                last_data = parsed if isinstance(parsed, dict) else {}
            except ValueError:
                last_data = {}
            if last_status not in RETRY_STATUS_CODES or attempt == attempts:
                return last_status, last_data, last_raw
        except Exception as exc:
            last_raw = repr(exc)
            if attempt == attempts:
                return last_status, last_data, last_raw
        await asyncio.sleep(0.6 * attempt)
    return last_status, last_data, last_raw


async def get_balance() -> Tuple[bool, Optional[float], Optional[str]]:
    """Return (success, balance_rub, error_message)."""
    key = _api_key()
    if not key:
        return False, None, "API key not set"

    try:
        async with httpx.AsyncClient(timeout=15.0) as client:
            status, data, raw = await _request_json_with_retries(
                client,
                "GET",
                _base_url(),
                params={"key": key, "action": "balance"},
            )
        if status == 401:
            return False, None, "Invalid API key"
        if status != 200:
            return False, None, _api_error(data, raw) or f"HTTP {status}"
        balance = float(data.get("balance", 0))
        return True, balance, None
    except Exception as e:
        logger.warning("Likes API balance check failed: %s", e)
        return False, None, str(e)


async def order_tiktok_comment_likes(
    video_url: str,
    comment_author_username: str,
    quantity: Optional[int] = None,
) -> Tuple[bool, Optional[int], Optional[str]]:
    """Create a TikTok comment likes order and return (success, order_id, error)."""
    key = _api_key()
    if not key:
        return False, None, "API key not set"

    settings = get_settings()
    qty = _coerce_likes_quantity(quantity if quantity is not None else settings.BOOST_PROJECT_LIKES_QUANTITY)
    service_id = settings.BOOST_PROJECT_TIKTOK_LIKES_SERVICE_ID

    if not video_url or not video_url.strip():
        return False, None, "video_url is empty"
    if not comment_author_username or not comment_author_username.strip():
        return False, None, "comment_author_username is empty"

    payload = {
        "key": key,
        "action": "add",
        "service": int(service_id),
        "link": video_url.strip(),
        "username": comment_author_username.strip().lstrip("@"),
        "quantity": qty,
    }

    try:
        async with httpx.AsyncClient(timeout=30.0) as client:
            # Boost Project documents API v1 examples as GET query requests,
            # and the previous working bot used that form. Keep POST as a
            # compatibility fallback for panels that accept form submissions.
            status, data, raw = await _request_json_with_retries(
                client,
                "GET",
                _base_url(),
                params=payload,
            )
            if status in {404, 405}:
                status, data, raw = await _request_json_with_retries(
                    client,
                    "POST",
                    _base_url(),
                    data=payload,
                )

        if status == 401:
            return False, None, "Invalid API key"
        if status != 200:
            detail = _api_error(data, raw)
            return False, None, f"HTTP {status}: {detail}" if detail else f"HTTP {status}"

        api_error = _api_error(data, "")
        if api_error:
            return False, None, api_error

        order_id = _extract_order_id(data)
        if order_id is None:
            return False, None, f"Response missing order: {data}"

        logger.info(
            "Comment likes order created: order_id=%s, video_url=%s, username=%s, quantity=%s",
            order_id,
            video_url[:50],
            payload["username"],
            qty,
        )
        return True, order_id, None
    except Exception as e:
        logger.warning("Comment likes order failed: %s", e)
        return False, None, str(e)
