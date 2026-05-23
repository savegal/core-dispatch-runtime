import hashlib
import hmac
import time
from typing import Optional


DEFAULT_USER_SCOPE_TTL_SECONDS = 300


def build_user_scope_payload(user_id: int | str, timestamp: int | str) -> str:
    return f"{int(user_id)}:{int(timestamp)}"


def sign_user_scope(
    *,
    user_id: int | str,
    timestamp: int | str,
    secret: str,
) -> str:
    payload = build_user_scope_payload(user_id, timestamp)
    return hmac.new(
        secret.encode("utf-8"),
        payload.encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()


def verify_user_scope_signature(
    *,
    user_id: int | str,
    timestamp: int | str,
    signature: Optional[str],
    secret: str,
    max_age_seconds: int = DEFAULT_USER_SCOPE_TTL_SECONDS,
    now_ts: Optional[int] = None,
) -> bool:
    if not signature:
        return False

    try:
        ts = int(timestamp)
        current_ts = int(now_ts if now_ts is not None else time.time())
    except (TypeError, ValueError):
        return False

    if abs(current_ts - ts) > max(1, int(max_age_seconds)):
        return False

    expected = sign_user_scope(user_id=user_id, timestamp=ts, secret=secret)
    return hmac.compare_digest(signature, expected)
