"""
Lightweight in-memory idempotency guard for comment/reply sending.

Prevents accidental duplicate sends from the same account within a short window.
"""

from __future__ import annotations

import hashlib
import threading
import time
from dataclasses import dataclass
from typing import Dict, Optional, Tuple


@dataclass(frozen=True)
class _SendKey:
    account_id: int
    action: str
    video_key: str
    text_hash: str
    scope: str


class SendIdempotencyGuard:
    """
    Idempotency guard for recent successful sends.

    Uses in-memory cache for fast local checks and Redis (when available)
    for cross-worker / cross-process protection.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._recent_success: Dict[_SendKey, float] = {}
        self._redis_client = None
        self._redis_disabled_until = 0.0

    @staticmethod
    def _redis_storage_key(key: _SendKey) -> str:
        stable_payload = f"{key.video_key}|{key.text_hash}|{key.scope}"
        stable_hash = hashlib.sha1(stable_payload.encode("utf-8")).hexdigest()
        return f"idempotency:send:{key.account_id}:{key.action}:{stable_hash}"

    @staticmethod
    def _redis_ttl_seconds() -> int:
        try:
            from config.settings import get_settings

            settings = get_settings()
            comment_window = int(getattr(settings, "COMMENT_IDEMPOTENCY_WINDOW_SECONDS", 600))
            reply_window = int(getattr(settings, "REPLY_IDEMPOTENCY_WINDOW_SECONDS", 600))
            max_window = max(comment_window, reply_window)
            return max(60, max_window * 2)
        except Exception:
            return 1800

    def _disable_redis_temporarily(self, cooldown_seconds: int = 30) -> None:
        self._redis_client = None
        self._redis_disabled_until = time.time() + max(1, cooldown_seconds)

    def _get_redis_client(self):
        now_ts = time.time()
        if now_ts < self._redis_disabled_until:
            return None
        if self._redis_client is not None:
            return self._redis_client

        try:
            import redis
            from config.settings import get_settings

            settings = get_settings()
            self._redis_client = redis.from_url(
                settings.REDIS_URL,
                decode_responses=True,
                socket_connect_timeout=0.25,
                socket_timeout=0.25,
                retry_on_timeout=False,
                health_check_interval=0,
                max_connections=10,
            )
            return self._redis_client
        except Exception:
            self._disable_redis_temporarily()
            return None

    @staticmethod
    def _normalize_video_key(video_url: str) -> str:
        raw = (video_url or "").strip().lower()
        # Ignore query/hash to keep same video grouped.
        raw = raw.split("#", 1)[0].split("?", 1)[0]
        return raw

    @staticmethod
    def _normalize_text(text: str) -> str:
        return " ".join((text or "").strip().lower().split())

    @staticmethod
    def _hash_text(text: str) -> str:
        normalized = SendIdempotencyGuard._normalize_text(text)
        return hashlib.sha1(normalized.encode("utf-8")).hexdigest()

    def _build_key(
        self,
        account_id: int,
        action: str,
        video_url: str,
        text: str,
        scope: Optional[str],
    ) -> _SendKey:
        return _SendKey(
            account_id=int(account_id),
            action=(action or "").strip().lower(),
            video_key=self._normalize_video_key(video_url),
            text_hash=self._hash_text(text),
            scope=(scope or "").strip(),
        )

    def _cleanup(self, now_ts: float, max_window_seconds: int) -> None:
        if not self._recent_success:
            return

        max_age = float(max(60, max_window_seconds * 2))
        stale = [k for k, ts in self._recent_success.items() if now_ts - ts > max_age]
        for key in stale:
            self._recent_success.pop(key, None)

    def was_sent_recently(
        self,
        account_id: int,
        action: str,
        video_url: str,
        text: str,
        window_seconds: int,
        scope: Optional[str] = None,
    ) -> Tuple[bool, Optional[float]]:
        """
        Returns (is_duplicate, age_seconds).
        """
        now_ts = time.time()
        window = max(1, int(window_seconds))
        key = self._build_key(account_id, action, video_url, text, scope)

        with self._lock:
            self._cleanup(now_ts, window)
            prev_ts = self._recent_success.get(key)
            if prev_ts is None:
                pass
            else:
                age = now_ts - prev_ts
                if age <= window:
                    return True, age
                return False, age

        redis_client = self._get_redis_client()
        if redis_client is not None:
            redis_key = self._redis_storage_key(key)
            try:
                redis_ts_raw = redis_client.get(redis_key)
                if redis_ts_raw is None:
                    return False, None
                prev_ts = float(redis_ts_raw)
                age = now_ts - prev_ts
                if age <= window:
                    with self._lock:
                        self._recent_success[key] = prev_ts
                    return True, age
                return False, age
            except Exception:
                # Redis недоступен/вернул некорректные данные — fallback на in-memory.
                self._disable_redis_temporarily()
                return False, None
        return False, None

    def mark_success(
        self,
        account_id: int,
        action: str,
        video_url: str,
        text: str,
        scope: Optional[str] = None,
    ) -> None:
        key = self._build_key(account_id, action, video_url, text, scope)
        now_ts = time.time()
        with self._lock:
            self._recent_success[key] = now_ts
            self._cleanup(now_ts, max_window_seconds=900)

        redis_client = self._get_redis_client()
        if redis_client is not None:
            redis_key = self._redis_storage_key(key)
            try:
                redis_client.set(redis_key, f"{now_ts:.6f}", ex=self._redis_ttl_seconds())
            except Exception:
                # Не ломаем рабочий путь при ошибках Redis.
                self._disable_redis_temporarily()


send_idempotency_guard = SendIdempotencyGuard()
