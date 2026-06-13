import asyncio
import copy
import hashlib
import httpx
import json as jsonlib
import logging
import re
import time
import uuid
from collections import OrderedDict
from typing import Any, Optional

from bot.config import load_config
from bot.services.template_utils import REPLIES_ONLY_MAIN_PLACEHOLDER
from config.settings import get_settings
from services.user_scope_signature import sign_user_scope
from tenacity import retry, retry_if_exception_type, stop_after_attempt, wait_exponential

logger = logging.getLogger("bot.api_client")
config = load_config()
settings = get_settings()
MAX_REPLIES = settings.MAX_REPLIES
_UNSET = object()


class RetryableRequestError(Exception):
    """Ð’Ñ€ÐµÐ¼ÐµÐ½Ð½Ð°Ñ Ð¾ÑˆÐ¸Ð±ÐºÐ° Ð·Ð°Ð¿Ñ€Ð¾ÑÐ°: Ð¼Ð¾Ð¶Ð½Ð¾ Ð±ÐµÐ·Ð¾Ð¿Ð°ÑÐ½Ð¾ Ð¿Ð¾Ð²Ñ‚Ð¾Ñ€Ð¸Ñ‚ÑŒ."""


class APIRequestError(Exception):
    """ÐžÑˆÐ¸Ð±ÐºÐ° API, ÐºÐ¾Ñ‚Ð¾Ñ€ÑƒÑŽ Ð½ÑƒÐ¶Ð½Ð¾ Ð¿Ð¾ÐºÐ°Ð·Ð°Ñ‚ÑŒ Ð¿Ð¾Ð»ÑŒÐ·Ð¾Ð²Ð°Ñ‚ÐµÐ»ÑŽ Ð±ÐµÐ· Ð°Ð²Ñ‚Ð¾Ð¼Ð°Ñ‚Ð¸Ñ‡ÐµÑÐºÐ¾Ð³Ð¾ retry."""


class APIClient:
    def __init__(self):
        self.base_url = config["API_BASE"]
        self.token = config.get("ADMIN_API_TOKEN")
        if not self.token:
            logger.error("ADMIN_API_TOKEN is not configured; bot API admin requests will be rejected")
        self.timeout = 10.0  # Ð£Ð¼ÐµÐ½ÑŒÑˆÐµÐ½ Ñ 30 Ð´Ð¾ 10 ÑÐµÐºÑƒÐ½Ð´
        
        if not self.base_url:
            logger.warning("API_BASE not configured, using default http://localhost:8000")
            self.base_url = "http://localhost:8000"
        
        # Ð˜ÑÐ¿Ð¾Ð»ÑŒÐ·ÑƒÐµÐ¼ Ð¾Ð´Ð¸Ð½ Ð¾Ð±Ñ‰Ð¸Ð¹ HTTP ÐºÐ»Ð¸ÐµÐ½Ñ‚ Ñ connection pooling Ð´Ð»Ñ Ð¿Ñ€Ð¾Ð¸Ð·Ð²Ð¾Ð´Ð¸Ñ‚ÐµÐ»ÑŒÐ½Ð¾ÑÑ‚Ð¸
        self._client = httpx.AsyncClient(
            timeout=httpx.Timeout(10.0, connect=5.0),
            limits=httpx.Limits(max_keepalive_connections=20, max_connections=100)
        )
        
        # ÐšÑÑˆ Ð´Ð»Ñ Ð¿Ð¾Ð»ÑŒÐ·Ð¾Ð²Ð°Ñ‚ÐµÐ»ÐµÐ¹ (TTL 60 ÑÐµÐºÑƒÐ½Ð´)
        self._user_cache: dict[int, tuple[dict, float]] = {}
        self._user_cache_ttl = 60.0
        
        # ÐšÑÑˆ Ð´Ð»Ñ Ð¿Ñ€Ð¾Ð²ÐµÑ€ÐºÐ¸ Ð´Ð¾ÑÑ‚ÑƒÐ¿Ð° (TTL 30 ÑÐµÐºÑƒÐ½Ð´)
        self._access_cache: dict[int, tuple[bool, float]] = {}
        self._access_cache_ttl = 30.0
        self._user_cache_max_size = 256
        self._access_cache_max_size = 256

        # Ð—Ð°Ñ‰Ð¸Ñ‚Ð° Ð¾Ñ‚ Ð´Ð²Ð¾Ð¹Ð½Ð¾Ð¹ Ð¾Ñ‚Ð¿Ñ€Ð°Ð²ÐºÐ¸ Ð¾Ð´Ð½Ð¾Ð³Ð¾ Ð¸ Ñ‚Ð¾Ð³Ð¾ Ð¶Ðµ Ð·Ð°ÐºÐ°Ð·Ð°
        self._create_task_inflight: dict[str, asyncio.Task] = {}
        self._create_task_recent: dict[str, tuple[dict, float]] = {}
        self._create_task_dedupe_ttl = 45.0

        # ÐšÐ¾Ñ€Ð¾Ñ‚ÐºÐ¾Ð¶Ð¸Ð²ÑƒÑ‰Ð¸Ð¹ read-cache Ð´Ð»Ñ ÑÐ½Ð¸Ð¶ÐµÐ½Ð¸Ñ API-ÑˆÑ‚Ð¾Ñ€Ð¼Ð° Ð² Ð±Ð¾Ñ‚Ðµ.
        # Ð¥Ñ€Ð°Ð½Ð¸Ð¼ Ð¼Ð°Ð»Ð¾ Ð¸ Ð½ÐµÐ´Ð¾Ð»Ð³Ð¾, Ñ‡Ñ‚Ð¾Ð±Ñ‹ Ð½Ðµ Ð¿Ð¾ÐºÐ°Ð·Ñ‹Ð²Ð°Ñ‚ÑŒ Ð¿Ð¾Ð»ÑŒÐ·Ð¾Ð²Ð°Ñ‚ÐµÐ»ÑŽ ÑƒÑÑ‚Ð°Ñ€ÐµÐ²ÑˆÐ¸Ðµ Ð´Ð°Ð½Ð½Ñ‹Ðµ.
        self._response_cache: OrderedDict[str, tuple[Any, float]] = OrderedDict()
        self._response_cache_max_size = 512
        self._inflight_get_requests: dict[str, asyncio.Future] = {}
        self._inflight_get_waiters: dict[str, int] = {}

    @staticmethod
    def _cache_key(method: str, endpoint: str, headers: Optional[dict] = None) -> str:
        scoped_headers = headers or {}
        user_scope = scoped_headers.get("X-User-Id") or ""
        return f"{method.upper()}|{endpoint}|{user_scope}"

    @staticmethod
    def _extract_user_scope(headers: dict[str, Any]) -> Optional[int]:
        raw_user_id = headers.get("X-User-Id")
        if raw_user_id is None:
            return None
        try:
            return int(raw_user_id)
        except (TypeError, ValueError):
            return None

    def _ensure_user_scope_headers(self, request_headers: dict[str, Any]) -> None:
        user_id = self._extract_user_scope(request_headers)
        if user_id is None:
            return

        ts = int(time.time())
        request_headers["X-User-Ts"] = str(ts)
        request_headers["X-User-Signature"] = sign_user_scope(
            user_id=user_id,
            timestamp=ts,
            secret=settings.JWT_SECRET,
        )

    @staticmethod
    def _cache_ttl_for_endpoint(method: str, endpoint: str) -> float:
        if method.upper() != "GET":
            return 0.0

        endpoint = endpoint or ""
        if re.match(r"^/api/users/\d+$", endpoint):
            return 15.0
        if endpoint.startswith("/api/video-inbox/stats"):
            return 6.0
        if endpoint.startswith("/api/video-parsing/notifications"):
            return 5.0
        if endpoint.startswith("/api/video-parsing/creators/suggestions"):
            return 10.0
        if endpoint.startswith("/api/video-parsing/creators"):
            return 6.0
        if endpoint.startswith("/api/templates/task"):
            return 20.0
        if endpoint.startswith("/api/settings/boost_likes"):
            return 20.0
        if endpoint.startswith("/api/settings/software_mode"):
            return 5.0
        if endpoint.startswith("/api/settings/commenting_mode"):
            return 5.0
        if endpoint.startswith("/api/settings/order_execution_strategy"):
            return 5.0
        if endpoint.startswith("/api/settings/monitor_parallel_browsers"):
            return 3.0
        if endpoint.startswith("/api/settings/proxy"):
            return 15.0
        if endpoint == "/api/pricing":
            return 30.0
        if endpoint in {
            "/api/accounts/stats",
            "/api/proxies/stats",
            "/api/stats/resources",
            "/api/stats/health/summary",
            "/api/accounts/runtime_health",
            "/api/video-parsing/monitoring/summary",
        }:
            return 5.0
        if endpoint.startswith("/api/users?"):
            return 10.0
        if endpoint.startswith("/api/accounts?") or endpoint.startswith("/api/proxies?"):
            return 8.0
        return 0.0

    def _prune_timed_cache(self, cache: dict, ttl: float, max_size: int, now_ts: float) -> None:
        stale_keys = [
            key for key, (_, created_ts) in cache.items()
            if now_ts - created_ts > ttl
        ]
        for key in stale_keys:
            cache.pop(key, None)
        while len(cache) > max_size:
            first_key = next(iter(cache), None)
            if first_key is None:
                break
            cache.pop(first_key, None)

    def _prune_response_cache(self, now_ts: float) -> None:
        stale_keys = [
            key for key, (_, created_ts) in self._response_cache.items()
            if now_ts - created_ts > 60.0
        ]
        for key in stale_keys:
            self._response_cache.pop(key, None)
        while len(self._response_cache) > self._response_cache_max_size:
            self._response_cache.popitem(last=False)

    def _clear_response_cache(self) -> None:
        if self._response_cache:
            self._response_cache.clear()

    def _fail_inflight_request(self, cache_key: Optional[str], inflight_future: Optional[asyncio.Future], exc: Exception) -> None:
        if not cache_key or inflight_future is None or inflight_future.done():
            return
        waiter_count = self._inflight_get_waiters.get(cache_key, 0)
        if waiter_count > 0:
            inflight_future.set_exception(exc)
        else:
            inflight_future.cancel()

    @staticmethod
    def _cache_invalidation_prefixes_for_endpoint(endpoint: str) -> tuple[str, ...]:
        endpoint = endpoint or ""
        if endpoint.startswith("/api/tasks"):
            return (
                "/api/tasks",
                "/api/users/",
                "/api/users?",
                "/api/video-inbox",
                "/api/video-inbox/stats",
            )
        if endpoint.startswith("/api/templates/task"):
            return ("/api/templates/task",)
        if endpoint.startswith("/api/video-parsing/creators"):
            return (
                "/api/video-parsing/creators",
                "/api/video-parsing/monitoring/summary",
                "/api/video-inbox",
                "/api/video-inbox/stats",
            )
        if endpoint.startswith("/api/video-parsing/notifications"):
            return ("/api/video-parsing/notifications",)
        if endpoint.startswith("/api/video-inbox"):
            return (
                "/api/video-inbox",
                "/api/video-inbox/stats",
                "/api/video-parsing/monitoring/summary",
            )
        if endpoint.startswith("/api/accounts"):
            return (
                "/api/accounts",
                "/api/accounts/stats",
                "/api/accounts/runtime_health",
                "/api/stats/resources",
                "/api/stats/health/summary",
            )
        if endpoint.startswith("/api/proxies"):
            return (
                "/api/proxies",
                "/api/proxies/stats",
                "/api/stats/resources",
                "/api/stats/health/summary",
            )
        if endpoint.startswith("/api/settings/proxy"):
            return (
                "/api/settings/proxy",
                "/api/proxies",
                "/api/proxies/stats",
                "/api/stats/resources",
                "/api/stats/health/summary",
            )
        if endpoint.startswith("/api/settings/boost_likes"):
            return ("/api/settings/boost_likes",)
        if endpoint.startswith("/api/settings/software_mode"):
            return ("/api/settings/software_mode",)
        if endpoint.startswith("/api/settings/commenting_mode"):
            return ("/api/settings/commenting_mode",)
        if endpoint.startswith("/api/settings/order_execution_strategy"):
            return ("/api/settings/order_execution_strategy",)
        if endpoint.startswith("/api/pricing"):
            return ("/api/pricing",)
        if endpoint.startswith("/api/users/") or endpoint.startswith("/api/users?"):
            return ("/api/users/", "/api/users?")
        return ()

    def _invalidate_response_cache_by_prefixes(self, prefixes: tuple[str, ...]) -> None:
        if not prefixes or not self._response_cache:
            return

        keys_to_remove: list[str] = []
        for cache_key in list(self._response_cache.keys()):
            try:
                _, cached_endpoint, _ = cache_key.split("|", 2)
            except ValueError:
                keys_to_remove.append(cache_key)
                continue
            if any(cached_endpoint.startswith(prefix) for prefix in prefixes):
                keys_to_remove.append(cache_key)

        for cache_key in keys_to_remove:
            self._response_cache.pop(cache_key, None)

    def _invalidate_response_cache_for_request(
        self,
        method: str,
        endpoint: str,
        request_headers: Optional[dict[str, Any]] = None,
    ) -> None:
        if method.upper() == "GET":
            return
        # sync_telegram Ð²Ñ‹Ð·Ñ‹Ð²Ð°ÐµÑ‚ÑÑ Ð¿Ð¾Ñ‡Ñ‚Ð¸ Ð½Ð° ÐºÐ°Ð¶Ð´Ñ‹Ð¹ Ð°Ð¿Ð´ÐµÐ¹Ñ‚, Ð½Ð¾ Ð½Ðµ Ð´Ð¾Ð»Ð¶ÐµÐ½ Ð¾Ð±Ð½ÑƒÐ»ÑÑ‚ÑŒ Ð²ÐµÑÑŒ read-cache.
        if endpoint == "/api/users/sync_telegram":
            return

        user_scope = self._extract_user_scope(request_headers or {})
        if user_scope is not None:
            self._user_cache.pop(user_scope, None)
            self._access_cache.pop(user_scope, None)

        prefixes = self._cache_invalidation_prefixes_for_endpoint(endpoint)
        if prefixes:
            self._invalidate_response_cache_by_prefixes(prefixes)
            return
        self._clear_response_cache()

    @staticmethod
    def _trim_text(value: Optional[str]) -> Optional[str]:
        if value is None:
            return None
        text = str(value).strip()
        return text if text else None

    @staticmethod
    def _preserve_user_text(value: Optional[str]) -> Optional[str]:
        """Нормализация пользовательского текста перед отправкой на API.

        - CRLF → LF (на случай клиента под Windows).
        - Trim ведущих/трейлинговых whitespace (пустых строк), чтобы не
          улетал «пустой коммент с переносом строки» в TikTok.
        - Внутренняя структура (multi-line, эмодзи, спецсимволы) сохраняется.
        """
        if value is None:
            return None
        return str(value).replace("\r\n", "\n").replace("\r", "\n").strip()

    @classmethod
    def _meaningful_user_text(cls, value: Optional[str]) -> Optional[str]:
        text = cls._preserve_user_text(value)
        if text is None or not text.strip():
            return None
        return text

    @classmethod
    def _normalize_text_variants(cls, main_comment: str, text_variants: Optional[list[str]]) -> list[str]:
        cleaned: list[str] = []
        if text_variants:
            for variant in text_variants:
                text = cls._meaningful_user_text(variant)
                if text:
                    cleaned.append(text)
        if not cleaned:
            cleaned = [main_comment]
        if len(cleaned) < 3:
            raise APIRequestError("Нужно минимум 3 варианта комментария.")
        return cleaned[:10]

    @staticmethod
    def _dedupe_video_urls(video_urls: list[str]) -> list[str]:
        seen: set[str] = set()
        result: list[str] = []
        for raw_url in video_urls or []:
            url = str(raw_url).strip()
            if not url or url in seen:
                continue
            seen.add(url)
            result.append(url)
        return result

    @staticmethod
    def _extract_error_detail(response: httpx.Response) -> str:
        status_code = response.status_code
        response_text = response.text[:500]
        try:
            data = response.json()
        except ValueError:
            return f"Ошибка сервера: {status_code} ({response_text[:120]})"

        detail = data.get("detail", data)
        if isinstance(detail, str):
            return detail
        if isinstance(detail, list) and detail:
            first = detail[0]
            if isinstance(first, dict):
                message = first.get("msg") or first.get("message")
                if message:
                    location = first.get("loc")
                    if isinstance(location, (list, tuple)) and location:
                        loc_str = ".".join(str(item) for item in location)
                        return f"{message} ({loc_str})"
                    return str(message)
            return str(first)
        if isinstance(detail, dict):
            return str(detail.get("message") or detail.get("error") or detail)
        return f"Ошибка сервера: {status_code}"

    @staticmethod
    def _build_task_fingerprint(payload: dict) -> str:
        digest_payload = {
            "user_id": payload.get("user_id"),
            "kind": payload.get("kind", "full"),
            "main_comment": payload.get("main_comment"),
            "likes": payload.get("likes", 0),
            "video_urls": payload.get("video_urls", []),
            "text_variants": payload.get("text_variants", []),
            "reply1": payload.get("reply1"),
            "reply2": payload.get("reply2"),
            "reply3": payload.get("reply3"),
            "settings": payload.get("settings", {}),
            "engagement_comment_id": payload.get("engagement_comment_id"),
            "engagement_username": payload.get("engagement_username"),
            "engagement_comment_text": payload.get("engagement_comment_text"),
        }
        packed = jsonlib.dumps(digest_payload, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
        return hashlib.sha256(packed.encode("utf-8")).hexdigest()

    def _prune_recent_create_task_cache(self, now_ts: float):
        stale_keys = [
            key for key, (_, created_ts) in self._create_task_recent.items()
            if now_ts - created_ts > self._create_task_dedupe_ttl
        ]
        for key in stale_keys:
            self._create_task_recent.pop(key, None)

    @retry(
        stop=stop_after_attempt(3),
        wait=wait_exponential(multiplier=1, min=2, max=10),
        retry=retry_if_exception_type(RetryableRequestError),
        reraise=True
    )
    async def _request(
        self,
        method: str,
        endpoint: str,
        json: Optional[dict] = None,
        headers: Optional[dict] = None,
        timeout: Optional[float] = None,
        retry_safe: bool = True
    ) -> Any:
        """
        Ð’Ñ‹Ð¿Ð¾Ð»Ð½ÑÐµÑ‚ HTTP Ð·Ð°Ð¿Ñ€Ð¾Ñ Ñ Ð°Ð²Ñ‚Ð¾Ð¼Ð°Ñ‚Ð¸Ñ‡ÐµÑÐºÐ¸Ð¼ retry Ð¿Ñ€Ð¸ Ð¾ÑˆÐ¸Ð±ÐºÐ°Ñ… ÑÐµÑ‚Ð¸.
        
        Ð˜Ð¡ÐŸÐ ÐÐ’Ð›Ð•ÐÐž: Ð”Ð¾Ð±Ð°Ð²Ð»ÐµÐ½ retry Ð´Ð»Ñ Ð¾Ð±Ñ€Ð°Ð±Ð¾Ñ‚ÐºÐ¸ Ð²Ñ€ÐµÐ¼ÐµÐ½Ð½Ñ‹Ñ… Ð¾ÑˆÐ¸Ð±Ð¾Ðº ÑÐµÑ‚Ð¸ Ð¸ ÑÐµÑ€Ð²ÐµÑ€Ð°.
        Retry Ð¿Ñ€Ð¸Ð¼ÐµÐ½ÑÐµÑ‚ÑÑ Ñ‚Ð¾Ð»ÑŒÐºÐ¾ Ð´Ð»Ñ ÑÐµÑ‚ÐµÐ²Ñ‹Ñ… Ð¾ÑˆÐ¸Ð±Ð¾Ðº Ð¸ ÑÐµÑ€Ð²ÐµÑ€Ð½Ñ‹Ñ… Ð¾ÑˆÐ¸Ð±Ð¾Ðº (5xx).
        """
        url = f"{self.base_url}{endpoint}"
        request_headers = dict(headers or {})
        request_headers.setdefault("X-Request-ID", uuid.uuid4().hex)
        if self.token and "X-Admin-Token" not in request_headers:
            request_headers["X-Admin-Token"] = self.token
        if "X-User-Id" not in request_headers and isinstance(json, dict):
            request_user_id = json.get("user_id")
            if request_user_id is not None:
                request_headers["X-User-Id"] = str(request_user_id)
        if "X-User-Id" not in request_headers and "user_id=" in endpoint:
            try:
                from urllib.parse import parse_qs, urlparse
                parsed = urlparse(endpoint)
                query = parse_qs(parsed.query)
                user_id_values = query.get("user_id")
                if user_id_values and user_id_values[0]:
                    request_headers["X-User-Id"] = str(user_id_values[0])
            except Exception:
                pass
        if "X-RateLimit-Key" not in request_headers and isinstance(json, dict):
            rate_limit_user_id = json.get("user_id") or json.get("telegram_id")
            if rate_limit_user_id is not None:
                request_headers["X-RateLimit-Key"] = f"user:{rate_limit_user_id}"
        if "X-RateLimit-Key" not in request_headers and "user_id=" in endpoint:
            try:
                from urllib.parse import parse_qs, urlparse
                parsed = urlparse(endpoint)
                query = parse_qs(parsed.query)
                user_id_values = query.get("user_id")
                if user_id_values and user_id_values[0]:
                    request_headers["X-RateLimit-Key"] = f"user:{user_id_values[0]}"
            except Exception:
                # Ð•ÑÐ»Ð¸ Ð½Ðµ ÑÐ¼Ð¾Ð³Ð»Ð¸ Ñ€Ð°ÑÐ¿Ð°Ñ€ÑÐ¸Ñ‚ÑŒ query - Ð¿Ñ€Ð¾ÑÑ‚Ð¾ Ð¸ÑÐ¿Ð¾Ð»ÑŒÐ·ÑƒÐµÐ¼ fallback Ð½Ð° API
                pass
        self._ensure_user_scope_headers(request_headers)
        request_timeout = httpx.Timeout(timeout, connect=5.0) if timeout is not None else None
        started = time.monotonic()
        now_ts = time.time()
        self._prune_response_cache(now_ts)
        cache_ttl = self._cache_ttl_for_endpoint(method, endpoint)
        cache_key = None
        inflight_future: asyncio.Future | None = None
        if cache_ttl > 0 and json is None:
            cache_key = self._cache_key(method, endpoint, request_headers)
            cached_item = self._response_cache.get(cache_key)
            if cached_item and (now_ts - cached_item[1]) <= cache_ttl:
                self._response_cache.move_to_end(cache_key)
                return copy.deepcopy(cached_item[0])
            inflight_existing = self._inflight_get_requests.get(cache_key)
            if inflight_existing is not None:
                self._inflight_get_waiters[cache_key] = self._inflight_get_waiters.get(cache_key, 0) + 1
                try:
                    return copy.deepcopy(await inflight_existing)
                finally:
                    remaining_waiters = self._inflight_get_waiters.get(cache_key, 0) - 1
                    if remaining_waiters > 0:
                        self._inflight_get_waiters[cache_key] = remaining_waiters
                    else:
                        self._inflight_get_waiters.pop(cache_key, None)
            inflight_future = asyncio.get_running_loop().create_future()
            self._inflight_get_requests[cache_key] = inflight_future

        try:
            response = await self._client.request(
                method,
                url,
                json=json,
                headers=request_headers,
                timeout=request_timeout,
            )
            response.raise_for_status()
            elapsed = time.monotonic() - started
            if elapsed > 5:
                logger.warning(f"Slow API request ({elapsed:.2f}s): {method} {endpoint}")

            if not response.content:
                result: Any = {}
            else:
                try:
                    result = response.json()
                except ValueError:
                    logger.warning(f"Non-JSON success response: {method} {endpoint}")
                    result = {"raw_response": response.text}
            if cache_key:
                self._response_cache[cache_key] = (copy.deepcopy(result), time.time())
                self._response_cache.move_to_end(cache_key)
                if inflight_future and not inflight_future.done():
                    inflight_future.set_result(copy.deepcopy(result))
            else:
                self._invalidate_response_cache_for_request(method, endpoint, request_headers)
            return result
        except httpx.TimeoutException as exc:
            self._fail_inflight_request(cache_key, inflight_future, exc)
            timeout_used = timeout if timeout is not None else self.timeout
            logger.error(f"Request timeout (timeout={timeout_used}s): {method} {endpoint} to {url}")
            if "/proxies/import" in endpoint:
                raise APIRequestError(
                    f"Превышен таймаут ({timeout_used}s). Проверка прокси занимает много времени. "
                    "Импорт продолжается на сервере, но ответ не получен. "
                    "Попробуйте проверить статистику прокси позже."
                ) from exc
            if "/accounts/bulk_import" in endpoint:
                raise APIRequestError(
                    f"Превышен таймаут ({timeout_used}s). Импорт аккаунтов занимает много времени "
                    "(проверка cookies, определение геолокации). Импорт может продолжаться на сервере. "
                    "Попробуйте проверить список аккаунтов позже."
                ) from exc
            if endpoint == "/api/tasks" and method.upper() == "POST":
                raise APIRequestError(
                    f"Превышен таймаут ожидания ({timeout_used}s) при создании заказа. "
                    "Возможно, заказ уже создан. Проверьте список заказов перед повторной отправкой."
                ) from exc
            message = f"Превышен таймаут ожидания ({timeout_used}s). Сервер временно недоступен, попробуйте позже."
            if retry_safe:
                raise RetryableRequestError(message) from exc
            raise APIRequestError(message) from exc
        except httpx.ConnectError as e:
            self._fail_inflight_request(cache_key, inflight_future, e)
            logger.error(f"Connection error: {method} {endpoint} to {url}: {str(e)}")
            message = (
                f"Не удалось подключиться к серверу API ({self.base_url}). "
                "Убедитесь, что backend запущен."
            )
            if retry_safe:
                raise RetryableRequestError(message) from e
            raise APIRequestError(message) from e
        except httpx.HTTPStatusError as e:
            self._fail_inflight_request(cache_key, inflight_future, e)
            status_code = e.response.status_code
            response_text = e.response.text[:500]
            logger.error(f"HTTP error {status_code}: {response_text}")

            error_detail = self._extract_error_detail(e.response)

            # 429 is not always transient. In this project it is also used for hard
            # business limits, for example: "Creator quota exceeded". Retrying that
            # only creates log spam and makes the bot look broken. Retry 429 only
            # when the server explicitly says when to retry.
            if status_code == 429:
                retry_after = e.response.headers.get("Retry-After")
                if retry_after and retry_safe:
                    logger.warning(f"Rate limited HTTP 429, retrying after server hint: {endpoint}")
                    raise RetryableRequestError(
                        f"Сервер временно ограничил запросы (429), повторяем позже."
                    ) from e
                raise APIRequestError(error_detail) from e

            transient_statuses = {408, 425}
            if status_code >= 500 or status_code in transient_statuses:
                if retry_safe:
                    logger.warning(f"Transient HTTP {status_code}, retrying: {endpoint}")
                    raise RetryableRequestError(f"Временная ошибка сервера ({status_code}), выполняется повтор.") from e
                raise APIRequestError(error_detail) from e

            raise APIRequestError(error_detail) from e
        except (RetryableRequestError, APIRequestError):
            raise
        except Exception as e:
            self._fail_inflight_request(cache_key, inflight_future, e)
            logger.error(f"Request error: {method} {endpoint} to {url}: {e}")
            if retry_safe:
                raise RetryableRequestError("Сервер временно недоступен, попробуйте позже.") from e
            raise APIRequestError("Сервер временно недоступен, попробуйте позже.") from e

        finally:
            if cache_key and inflight_future is not None:
                existing_future = self._inflight_get_requests.get(cache_key)
                if existing_future is inflight_future:
                    self._inflight_get_requests.pop(cache_key, None)

    async def sync_telegram(self, telegram_id: int, username: Optional[str] = None, use_cache: bool = True) -> dict:
        """Ð¡Ð¸Ð½Ñ…Ñ€Ð¾Ð½Ð¸Ð·Ð¸Ñ€ÑƒÐµÑ‚ Ð¿Ð¾Ð»ÑŒÐ·Ð¾Ð²Ð°Ñ‚ÐµÐ»Ñ Ñ backend. Ð˜ÑÐ¿Ð¾Ð»ÑŒÐ·ÑƒÐµÑ‚ ÐºÑÑˆ Ð´Ð»Ñ ÑƒÑÐºÐ¾Ñ€ÐµÐ½Ð¸Ñ."""
        current_time = time.time()
        self._prune_timed_cache(self._user_cache, self._user_cache_ttl, self._user_cache_max_size, current_time)
        
        # ÐŸÑ€Ð¾Ð²ÐµÑ€ÑÐµÐ¼ ÐºÑÑˆ
        if use_cache and telegram_id in self._user_cache:
            cached_data, cached_time = self._user_cache[telegram_id]
            if current_time - cached_time < self._user_cache_ttl:
                return cached_data
        
        # Ð”ÐµÐ»Ð°ÐµÐ¼ Ð·Ð°Ð¿Ñ€Ð¾Ñ
        user_data = await self._request(
            "POST",
            "/api/users/sync_telegram",
            json={"telegram_id": telegram_id, "username": username}
        )
        
        # Ð¡Ð¾Ñ…Ñ€Ð°Ð½ÑÐµÐ¼ Ð² ÐºÑÑˆ
        if use_cache:
            self._user_cache[telegram_id] = (user_data, current_time)
            self._prune_timed_cache(self._user_cache, self._user_cache_ttl, self._user_cache_max_size, current_time)
        
        return user_data
    
    def clear_user_cache(self, telegram_id: Optional[int] = None):
        """ÐžÑ‡Ð¸Ñ‰Ð°ÐµÑ‚ ÐºÑÑˆ Ð¿Ð¾Ð»ÑŒÐ·Ð¾Ð²Ð°Ñ‚ÐµÐ»Ñ. Ð•ÑÐ»Ð¸ telegram_id Ð½Ðµ ÑƒÐºÐ°Ð·Ð°Ð½ - Ð¾Ñ‡Ð¸Ñ‰Ð°ÐµÑ‚ Ð²ÐµÑÑŒ ÐºÑÑˆ."""
        if telegram_id:
            self._user_cache.pop(telegram_id, None)
            self._access_cache.pop(telegram_id, None)
            self._invalidate_response_cache_by_prefixes(("/api/users/", "/api/users?"))
        else:
            self._user_cache.clear()
            self._access_cache.clear()
            self._clear_response_cache()

    async def create_task(
        self,
        user_id: int,
        main_comment: str,
        video_urls: list[str],
        text_variants: Optional[list[str]] = None,
        reply1: Optional[str] = None,
        reply2: Optional[str] = None,
        reply3: Optional[str] = None,
        reply4: Optional[str] = None,
        reply5: Optional[str] = None,
        reply6: Optional[str] = None,
        likes: int = 0,
        settings: Optional[dict] = None,
        kind: str = "full",  # HYBRID: Ð¢Ð¸Ð¿ Ð·Ð°ÐºÐ°Ð·Ð° (full/engagement_only)
        engagement_comment_id: Optional[str] = None,  # HYBRID: ID ÐºÐ¾Ð¼Ð¼ÐµÐ½Ñ‚Ð°Ñ€Ð¸Ñ Ð´Ð»Ñ engagement_only
        engagement_username: Optional[str] = None,  # HYBRID: Username Ð°Ð²Ñ‚Ð¾Ñ€Ð° ÐºÐ¾Ð¼Ð¼ÐµÐ½Ñ‚Ð°Ñ€Ð¸Ñ
        engagement_comment_text: Optional[str] = None,  # HYBRID: Ð¢ÐµÐºÑÑ‚ ÐºÐ¾Ð¼Ð¼ÐµÐ½Ñ‚Ð°Ñ€Ð¸Ñ Ð´Ð»Ñ Ð¿Ð¾Ð¸ÑÐºÐ°
        replies: Optional[list[str]] = None,
    ) -> dict:
        """Ð¡Ð¾Ð·Ð´Ð°Ñ‘Ñ‚ Ð·Ð°Ð´Ð°Ñ‡Ñƒ."""
        normalized_main_comment = self._meaningful_user_text(main_comment)
        if not normalized_main_comment:
            if kind == "engagement_only":
                # engagement_only does not publish main_comment, but payload must stay valid.
                normalized_main_comment = (
                    self._meaningful_user_text(engagement_comment_text)
                    or self._meaningful_user_text(reply1)
                    or "engagement_only"
                )
            else:
                raise APIRequestError("Main comment cannot be empty.")

        normalized_video_urls = self._dedupe_video_urls(video_urls)
        if not normalized_video_urls:
            raise APIRequestError("Не переданы валидные ссылки на видео.")

        text_variants_input = text_variants
        if kind == "engagement_only" and not text_variants_input:
            text_variants_input = [normalized_main_comment] * 3
        normalized_text_variants = self._normalize_text_variants(normalized_main_comment, text_variants_input)
        normalized_settings = dict(settings or {})

        settings_replies = normalized_settings.get("replies")
        if replies is not None:
            raw_replies = replies
        elif isinstance(settings_replies, list) and settings_replies:
            raw_replies = settings_replies
        else:
            raw_replies = [
                reply1,
                reply2,
                reply3,
                reply4 if reply4 is not None else normalized_settings.get("reply4"),
                reply5 if reply5 is not None else normalized_settings.get("reply5"),
                reply6 if reply6 is not None else normalized_settings.get("reply6"),
                *[
                    normalized_settings.get(f"reply{idx}")
                    for idx in range(7, MAX_REPLIES + 1)
                ],
            ]
        normalized_replies = [
            text
            for reply in (raw_replies or [])
            for text in [self._meaningful_user_text(reply)]
            if text
        ][:MAX_REPLIES]
        normalized_reply1 = normalized_replies[0] if len(normalized_replies) >= 1 else None
        normalized_reply2 = normalized_replies[1] if len(normalized_replies) >= 2 else None
        normalized_reply3 = normalized_replies[2] if len(normalized_replies) >= 3 else None
        for idx in range(4, MAX_REPLIES + 1):
            normalized_settings.pop(f"reply{idx}", None)
        normalized_settings["replies"] = normalized_replies
        requested_reply_count = normalized_settings.get("reply_count")
        if replies is None and isinstance(requested_reply_count, int) and requested_reply_count >= 0:
            normalized_settings["reply_count"] = min(requested_reply_count, len(normalized_replies), MAX_REPLIES)
        else:
            normalized_settings["reply_count"] = len(normalized_replies)
        for idx, reply_value in enumerate(normalized_replies[3:], start=4):
            normalized_settings[f"reply{idx}"] = reply_value

        json_data = {
            "user_id": user_id,
            "main_comment": normalized_main_comment,
            "likes": likes,
            "video_urls": normalized_video_urls,
            "text_variants": normalized_text_variants,
            "settings": normalized_settings,
            "kind": kind  # HYBRID: Ð¢Ð¸Ð¿ Ð·Ð°ÐºÐ°Ð·Ð°
        }
        
        # HYBRID: Ð”Ð¾Ð±Ð°Ð²Ð»ÑÐµÐ¼ Ð¿Ð°Ñ€Ð°Ð¼ÐµÑ‚Ñ€Ñ‹ Ð´Ð»Ñ engagement_only
        if kind == "engagement_only":
            if engagement_comment_id:
                json_data["engagement_comment_id"] = engagement_comment_id
            if engagement_username:
                json_data["engagement_username"] = engagement_username
            if engagement_comment_text:
                json_data["engagement_comment_text"] = engagement_comment_text
        if normalized_reply1:
            json_data["reply1"] = normalized_reply1
        if normalized_reply2:
            json_data["reply2"] = normalized_reply2
        if normalized_reply3:
            json_data["reply3"] = normalized_reply3
        
        task_fingerprint = self._build_task_fingerprint(json_data)
        now_ts = time.time()
        self._prune_recent_create_task_cache(now_ts)
        cached_result = self._create_task_recent.get(task_fingerprint)
        if cached_result and (now_ts - cached_result[1]) <= self._create_task_dedupe_ttl:
            logger.warning(f"Deduplicated create_task from cache: user_id={user_id}, fingerprint={task_fingerprint[:12]}")
            return cached_result[0]

        in_flight_task = self._create_task_inflight.get(task_fingerprint)
        if in_flight_task:
            logger.warning(f"Joined in-flight create_task: user_id={user_id}, fingerprint={task_fingerprint[:12]}")
            result = await in_flight_task
            if isinstance(result, dict):
                return result
            raise APIRequestError("Некорректный ответ API при создании заказа.")

        if "client_task_fingerprint" not in json_data["settings"]:
            json_data["settings"]["client_task_fingerprint"] = task_fingerprint

        request_headers = {
            "X-Idempotency-Key": task_fingerprint
        }

        request_task = asyncio.create_task(
            self._request(
                "POST",
                "/api/tasks",
                json=json_data,
                headers=request_headers,
                timeout=25.0,
                retry_safe=False,  # create_task Ð½Ðµ Ð¿Ð¾Ð²Ñ‚Ð¾Ñ€ÑÐµÐ¼ Ð°Ð²Ñ‚Ð¾Ð¼Ð°Ñ‚Ð¸Ñ‡ÐµÑÐºÐ¸, Ñ‡Ñ‚Ð¾Ð±Ñ‹ Ð¸ÑÐºÐ»ÑŽÑ‡Ð¸Ñ‚ÑŒ Ð´Ð²Ð¾Ð¹Ð½ÑƒÑŽ Ð¾Ñ‚Ð¿Ñ€Ð°Ð²ÐºÑƒ
            )
        )
        self._create_task_inflight[task_fingerprint] = request_task
        try:
            result = await request_task
            if not isinstance(result, dict):
                raise APIRequestError("Некорректный формат ответа API при создании заказа.")
            self._create_task_recent[task_fingerprint] = (result, time.time())
            return result
        finally:
            self._create_task_inflight.pop(task_fingerprint, None)

    async def update_task_texts(self, task_id: int, text_variants: list[str], user_id: int) -> dict:
        """ÐžÐ±Ð½Ð¾Ð²Ð»ÑÐµÑ‚ Ð²Ð°Ñ€Ð¸Ð°Ð½Ñ‚Ñ‹ Ñ‚ÐµÐºÑÑ‚Ð° Ð´Ð»Ñ pending-Ð·Ð°ÐºÐ°Ð·Ð°."""
        from urllib.parse import urlencode
        params = {"user_id": user_id}
        url = f"/api/tasks/{task_id}/texts?{urlencode(params)}"
        return await self._request(
            "POST",
            url,
            json={"text_variants": text_variants}
        )

    async def stop_task(self, task_id: int, user_id: int) -> dict:
        """Останавливает выполняющийся заказ."""
        from urllib.parse import urlencode
        params = {"user_id": user_id}
        url = f"/api/tasks/{task_id}/stop?{urlencode(params)}"
        return await self._request("POST", url, json={})

    async def cancel_pending_tasks(self, user_id: int) -> dict:
        """Отменяет все pending-заказы пользователя в очереди."""
        from urllib.parse import urlencode
        params = {"user_id": user_id}
        url = f"/api/tasks/cancel_pending?{urlencode(params)}"
        return await self._request("POST", url, json={})

    async def prioritize_task(self, task_id: int, user_id: int) -> dict:
        """Повышает pending-заказ до верхнего приоритета очереди."""
        from urllib.parse import urlencode
        params = {"user_id": user_id}
        url = f"/api/tasks/{task_id}/priority?{urlencode(params)}"
        return await self._request("POST", url, json={})

    async def list_tasks(
        self,
        user_id: Optional[int] = None,
        status: Optional[str] = None,
        limit: Optional[int] = None,
        offset: Optional[int] = None,
    ) -> list:
        """ÐŸÐ¾Ð»ÑƒÑ‡Ð°ÐµÑ‚ ÑÐ¿Ð¸ÑÐ¾Ðº Ð·Ð°Ð´Ð°Ñ‡ Ð¿Ð¾Ð»ÑŒÐ·Ð¾Ð²Ð°Ñ‚ÐµÐ»Ñ."""
        from urllib.parse import urlencode
        params = {}
        if user_id is not None:
            params["user_id"] = user_id
        if status:
            params["status"] = status
        if limit is not None:
            params["limit"] = limit
        if offset is not None:
            params["offset"] = offset
        url = "/api/tasks?" + urlencode(params) if params else "/api/tasks"
        return await self._request("GET", url)

    async def get_task(self, task_id: int, user_id: int) -> dict:
        """ÐŸÐ¾Ð»ÑƒÑ‡Ð°ÐµÑ‚ Ð·Ð°Ð´Ð°Ñ‡Ñƒ Ð¿Ð¾ ID."""
        from urllib.parse import urlencode
        params = {"user_id": user_id}
        url = f"/api/tasks/{task_id}?{urlencode(params)}"
        return await self._request("GET", url)
    
    async def get_geo_recommendation(
        self,
        required_accounts: int,
        account_type: str = "main_comment"
    ) -> dict:
        """ÐŸÐ¾Ð»ÑƒÑ‡Ð°ÐµÑ‚ Ñ€ÐµÐºÐ¾Ð¼ÐµÐ½Ð´Ð°Ñ†Ð¸Ð¸ Ð¿Ð¾ Ð³ÐµÐ¾Ð³Ñ€Ð°Ñ„Ð¸Ð¸ Ð´Ð»Ñ Ð·Ð°Ð´Ð°Ñ‡Ð¸."""
        from urllib.parse import urlencode
        params = {
            "required_accounts": required_accounts,
            "account_type": account_type
        }
        url = "/api/geo/recommendation?" + urlencode(params)
        return await self._request("GET", url)
    
    async def get_geo_coverage(self, required_accounts: int | None = None) -> dict:
        """ÐŸÐ¾Ð»ÑƒÑ‡Ð°ÐµÑ‚ Ð°Ð½Ð°Ð»Ð¸Ð· Ð¿Ð¾ÐºÑ€Ñ‹Ñ‚Ð¸Ñ Ð³ÐµÐ¾Ð³Ñ€Ð°Ñ„Ð¸Ð¸."""
        from urllib.parse import urlencode
        params = {}
        if required_accounts is not None:
            params["required_accounts"] = required_accounts
        url = "/api/geo/coverage" + ("?" + urlencode(params) if params else "")
        return await self._request("GET", url)

    async def get_geo_stats(self, days: int = 7) -> dict:
        return await self._request("GET", f"/api/geo/stats?days={days}")

    async def bulk_import_accounts(self, accounts: list[dict]) -> dict:
        """
        Ð˜Ð¼Ð¿Ð¾Ñ€Ñ‚Ð¸Ñ€ÑƒÐµÑ‚ Ð°ÐºÐºÐ°ÑƒÐ½Ñ‚Ñ‹.
        Ð˜ÑÐ¿Ð¾Ð»ÑŒÐ·ÑƒÐµÑ‚ ÑƒÐ²ÐµÐ»Ð¸Ñ‡ÐµÐ½Ð½Ñ‹Ð¹ Ñ‚Ð°Ð¹Ð¼Ð°ÑƒÑ‚, Ñ‚.Ðº. Ð¸Ð¼Ð¿Ð¾Ñ€Ñ‚ Ð¼Ð¾Ð¶ÐµÑ‚ Ð·Ð°Ð½Ð¸Ð¼Ð°Ñ‚ÑŒ Ð¼Ð½Ð¾Ð³Ð¾ Ð²Ñ€ÐµÐ¼ÐµÐ½Ð¸
        (Ð¿Ñ€Ð¾Ð²ÐµÑ€ÐºÐ° cookies, Ð¾Ð¿Ñ€ÐµÐ´ÐµÐ»ÐµÐ½Ð¸Ðµ country_code Ð¸Ð· Ð¿Ñ€Ð¾ÐºÑÐ¸ Ð¸ Ñ‚.Ð´.).
        """
        # Ð£Ð²ÐµÐ»Ð¸Ñ‡Ð¸Ð²Ð°ÐµÐ¼ Ñ‚Ð°Ð¹Ð¼Ð°ÑƒÑ‚: 3 ÑÐµÐºÑƒÐ½Ð´Ñ‹ Ð½Ð° Ð°ÐºÐºÐ°ÑƒÐ½Ñ‚ + 60 ÑÐµÐºÑƒÐ½Ð´ Ð±Ð°Ð·Ð¾Ð²Ñ‹Ð¹
        # ÐœÐ¸Ð½Ð¸Ð¼ÑƒÐ¼ 120 ÑÐµÐºÑƒÐ½Ð´, Ð¼Ð°ÐºÑÐ¸Ð¼ÑƒÐ¼ 15 Ð¼Ð¸Ð½ÑƒÑ‚ (Ð´Ð»Ñ Ð±Ð¾Ð»ÑŒÑˆÐ¸Ñ… Ð¸Ð¼Ð¿Ð¾Ñ€Ñ‚Ð¾Ð²)
        timeout = max(120.0, 60.0 + (len(accounts) * 3.0))
        timeout = min(timeout, 900.0)  # ÐœÐ°ÐºÑÐ¸Ð¼ÑƒÐ¼ 15 Ð¼Ð¸Ð½ÑƒÑ‚
        
        return await self._request(
            "POST",
            "/api/accounts/bulk_import",
            json={"accounts": accounts},
            timeout=timeout
        )
    
    async def get_proxy_stats(self) -> dict:
        """ÐŸÐ¾Ð»ÑƒÑ‡Ð°ÐµÑ‚ ÑÑ‚Ð°Ñ‚Ð¸ÑÑ‚Ð¸ÐºÑƒ Ð¿Ñ€Ð¾ÐºÑÐ¸."""
        return await self._request("GET", "/api/proxies/stats")
    
    async def import_proxies(self, proxies: list[str] | list[dict]) -> dict:
        """
        Ð˜Ð¼Ð¿Ð¾Ñ€Ñ‚Ð¸Ñ€ÑƒÐµÑ‚ Ð¿Ñ€Ð¾ÐºÑÐ¸.
        ÐŸÐ¾Ð´Ð´ÐµÑ€Ð¶Ð¸Ð²Ð°ÐµÑ‚ ÐºÐ°Ðº ÑÑ‚Ð°Ñ€Ñ‹Ð¹ Ñ„Ð¾Ñ€Ð¼Ð°Ñ‚ (ÑÐ¿Ð¸ÑÐ¾Ðº ÑÑ‚Ñ€Ð¾Ðº), Ñ‚Ð°Ðº Ð¸ Ð½Ð¾Ð²Ñ‹Ð¹ (ÑÐ¿Ð¸ÑÐ¾Ðº ÑÐ»Ð¾Ð²Ð°Ñ€ÐµÐ¹ Ñ rotation_url).
        Ð˜ÑÐ¿Ð¾Ð»ÑŒÐ·ÑƒÐµÑ‚ ÑƒÐ²ÐµÐ»Ð¸Ñ‡ÐµÐ½Ð½Ñ‹Ð¹ Ñ‚Ð°Ð¹Ð¼Ð°ÑƒÑ‚, Ñ‚.Ðº. Ð¿Ñ€Ð¾Ð²ÐµÑ€ÐºÐ° Ð¿Ñ€Ð¾ÐºÑÐ¸ Ð¼Ð¾Ð¶ÐµÑ‚ Ð·Ð°Ð½Ð¸Ð¼Ð°Ñ‚ÑŒ Ð¼Ð½Ð¾Ð³Ð¾ Ð²Ñ€ÐµÐ¼ÐµÐ½Ð¸
        (Ð¿Ñ€Ð¸Ð¼ÐµÑ€Ð½Ð¾ 2 ÑÐµÐºÑƒÐ½Ð´Ñ‹ Ð½Ð° Ð¿Ñ€Ð¾ÐºÑÐ¸).
        """
        # Ð£Ð²ÐµÐ»Ð¸Ñ‡Ð¸Ð²Ð°ÐµÐ¼ Ñ‚Ð°Ð¹Ð¼Ð°ÑƒÑ‚: 2 ÑÐµÐºÑƒÐ½Ð´Ñ‹ Ð½Ð° Ð¿Ñ€Ð¾ÐºÑÐ¸ + 10 ÑÐµÐºÑƒÐ½Ð´ Ð±Ð°Ð·Ð¾Ð²Ñ‹Ð¹
        timeout = 10.0 + (len(proxies) * 2.0)
        timeout = min(timeout, 120.0)  # ÐœÐ°ÐºÑÐ¸Ð¼ÑƒÐ¼ 2 Ð¼Ð¸Ð½ÑƒÑ‚Ñ‹
        
        return await self._request(
            "POST",
            "/api/proxies/import",
            json={"proxies": proxies},
            timeout=timeout
        )

    async def delete_proxies(
        self,
        ids: list[int] | None = None,
        proxies: list[str] | None = None,
        clear_accounts: bool = True
    ) -> dict:
        """Ð£Ð´Ð°Ð»ÑÐµÑ‚ Ð¿Ñ€Ð¾ÐºÑÐ¸ Ð¿Ð¾ ID Ð¸/Ð¸Ð»Ð¸ ÑÑ‚Ñ€Ð¾ÐºÐ°Ð¼ proxy."""
        payload: dict = {"clear_accounts": clear_accounts}
        if ids is not None:
            payload["ids"] = ids
        if proxies is not None:
            payload["proxies"] = proxies
        return await self._request("POST", "/api/proxies/delete", json=payload)

    async def delete_all_proxies(self, clear_accounts: bool = True) -> dict:
        """Ð£Ð´Ð°Ð»ÑÐµÑ‚ Ð²ÑÐµ Ð¿Ñ€Ð¾ÐºÑÐ¸."""
        return await self._request(
            "POST",
            "/api/proxies/delete_all",
            json={"clear_accounts": clear_accounts}
        )

    async def validate_proxies(
        self,
        ids: list[int] | None = None,
        proxies: list[str] | None = None,
        timeout: float = 8.0,
        concurrency: int = 30,
        limit: int | None = None,
        reassign_bad_proxies: bool = False,
        assign_after: bool = False
    ) -> dict:
        """Ð‘Ñ‹ÑÑ‚Ñ€Ð°Ñ Ð¿Ñ€Ð¾Ð²ÐµÑ€ÐºÐ° Ð¿Ñ€Ð¾ÐºÑÐ¸ Ñ Ð¾Ð±Ð½Ð¾Ð²Ð»ÐµÐ½Ð¸ÐµÐ¼ ÑÑ‚Ð°Ñ‚ÑƒÑÐ¾Ð²."""
        payload = {
            "ids": ids,
            "proxies": proxies,
            "timeout": timeout,
            "concurrency": concurrency,
            "limit": limit,
            "reassign_bad_proxies": reassign_bad_proxies,
            "assign_after": assign_after
        }
        return await self._request("POST", "/api/proxies/validate", json=payload)
    

    async def list_proxies(self, limit: int = 50, offset: int = 0) -> list:
        """Ð˜Ð¡ÐŸÐ ÐÐ’Ð›Ð•ÐÐž: ÐŸÐ¾Ð»ÑƒÑ‡Ð°ÐµÑ‚ ÑÐ¿Ð¸ÑÐ¾Ðº Ð¿Ñ€Ð¾ÐºÑÐ¸ Ñ ÑÐµÑ€Ð²ÐµÑ€Ð½Ð¾Ð¹ Ð¿Ð°Ð³Ð¸Ð½Ð°Ñ†Ð¸ÐµÐ¹. Ð£Ð±Ñ€Ð°Ð½Ð° Ñ„Ð¸Ð»ÑŒÑ‚Ñ€Ð°Ñ†Ð¸Ñ Ð¿Ð¾ ÑÑ‚Ð°Ñ‚ÑƒÑÑƒ."""
        from urllib.parse import urlencode
        params = {"limit": limit, "offset": offset}
        endpoint = f"/api/proxies?{urlencode(params)}"
        return await self._request("GET", endpoint)
    
    async def get_dead_accounts(self) -> list:
        """ÐŸÐ¾Ð»ÑƒÑ‡Ð°ÐµÑ‚ Ñ Ð¿Ð¸Ñ Ð¾Ðº Ð¼Ñ‘Ñ€Ñ‚Ð²Ñ‹Ñ… Ð°ÐºÐºÐ°ÑƒÐ½Ñ‚Ð¾Ð²."""
        return await self._request("GET", "/api/stats/accounts/dead")

    async def get_account_stats(self) -> dict:
        """ÐŸÐ¾Ð»ÑƒÑ‡Ð°ÐµÑ‚ Ñ Ñ‚Ð°Ñ‚Ð¸Ñ Ñ‚Ð¸ÐºÑƒ Ð°ÐºÐºÐ°ÑƒÐ½Ñ‚Ð¾Ð²."""
        return await self._request("GET", "/api/accounts/stats")

    async def make_all_accounts_active(self) -> dict:
        """Ð£Ñ Ñ‚Ð°Ð½Ð°Ð²Ð»Ð¸Ð²Ð°ÐµÑ‚ Ñ Ñ‚Ð°Ñ‚ÑƒÑ  'active' Ð´Ð»Ñ  Ð²Ñ ÐµÑ… Ð°ÐºÐºÐ°ÑƒÐ½Ñ‚Ð¾Ð² Ð² Ð±Ð°Ð·Ðµ Ð´Ð°Ð½Ð½Ñ‹Ñ…."""
        return await self._request("POST", "/api/accounts/make_all_active", timeout=60)
    
    async def reset_account_cooldowns(self) -> dict:
        """Сбрасывает runtime cooldown'ы и bad-proxy кеш (мягкий сброс, не трогает dead)."""
        return await self._request("POST", "/api/accounts/reset_cooldowns", timeout=60)

    async def get_account_runtime_health(self) -> dict:
        """ÐŸÐ¾Ð»ÑƒÑ‡Ð°ÐµÑ‚ runtime-Ð·Ð´Ð¾Ñ€Ð¾Ð²ÑŒÐµ Ð¿ÑƒÐ»Ð¾Ð² Ð°ÐºÐºÐ°ÑƒÐ½Ñ‚Ð¾Ð² Ð´Ð»Ñ  monitor/main/reply."""
        return await self._request("GET", "/api/accounts/runtime_health")
    
    async def get_video_batch(self, batch_id: int, user_id: int) -> dict:
        """P0 FIX: ÐŸÐ¾Ð»ÑƒÑ‡Ð°ÐµÑ‚ batch Ð¿Ð¾ ID Ð¸ Ð²Ð¾Ð·Ð²Ñ€Ð°Ñ‰Ð°ÐµÑ‚ Ñ Ð¿Ð¸Ñ Ð¾Ðº video_ids."""
        from urllib.parse import urlencode
        params = {"user_id": user_id}
        url = f"/api/video-batch/{batch_id}?{urlencode(params)}"
        return await self._request("GET", url)
    
    async def create_video_batch(self, creator_id: int, video_ids: list[str], user_id: int) -> dict:
        """P0 FIX: Ð¡Ð¾Ð·Ð´Ð°ÐµÑ‚ batch Ð´Ð»Ñ Ñ…Ñ€Ð°Ð½ÐµÐ½Ð¸Ñ ÑÐ¿Ð¸ÑÐºÐ° video_ids."""
        from urllib.parse import urlencode
        params = {"user_id": user_id}
        url = f"/api/video-batch/create?{urlencode(params)}"
        return await self._request("POST", url, json={
            "creator_id": creator_id,
            "video_ids": video_ids
        })
    
    async def has_active_accounts(self) -> bool:
        """ÐŸÑ€Ð¾Ð²ÐµÑ€ÑÐµÑ‚, ÐµÑÑ‚ÑŒ Ð»Ð¸ Ð°ÐºÑ‚Ð¸Ð²Ð½Ñ‹Ðµ Ð°ÐºÐºÐ°ÑƒÐ½Ñ‚Ñ‹."""
        try:
            stats = await self.get_account_stats()
            active_count = stats.get("active", 0)
            return active_count > 0
        except Exception as e:
            logger.error(f"Error checking active accounts: {e}")
            # Ð’ ÑÐ»ÑƒÑ‡Ð°Ðµ Ð¾ÑˆÐ¸Ð±ÐºÐ¸ Ð²Ð¾Ð·Ð²Ñ€Ð°Ñ‰Ð°ÐµÐ¼ False, Ñ‡Ñ‚Ð¾Ð±Ñ‹ Ð½Ðµ ÑÐ¾Ð·Ð´Ð°Ð²Ð°Ñ‚ÑŒ Ð·Ð°ÐºÐ°Ð·Ñ‹ Ð±ÐµÐ· Ð¿Ñ€Ð¾Ð²ÐµÑ€ÐºÐ¸
            return False
    
    async def assign_proxies(
        self,
        only_accounts_without_proxy: bool = True,
        reuse_existing: bool = False,
        allow_reuse_within_batch: bool = False,
        test_shared_residential: bool = False,
        force_proxy_type_filter: str | None = None,
        use_geo_matching: bool | None = None,
    ) -> dict:
        """Распределяет прокси из пула активных прокси по аккаунтам."""
        payload = {
            "only_accounts_without_proxy": only_accounts_without_proxy,
            "reuse_existing": reuse_existing,
            "allow_reuse_within_batch": allow_reuse_within_batch,
            "test_shared_residential": test_shared_residential,
        }
        if force_proxy_type_filter is not None:
            payload["force_proxy_type_filter"] = force_proxy_type_filter
        if use_geo_matching is not None:
            payload["use_geo_matching"] = use_geo_matching
        return await self._request("POST", "/api/accounts/assign_proxies", json=payload)

    async def get_account_pool_diagnostics(self) -> dict:
        """Возвращает диагностику пула аккаунтов для main-comment."""
        return await self._request("GET", "/api/accounts/pool_diagnostics")

    async def get_account_test_mode(self) -> dict:
        """Возвращает статус тестового режима аккаунтов/прокси."""
        return await self._request("GET", "/api/accounts/test_mode")

    async def set_account_test_mode(self, enabled: bool) -> dict:
        """Включает/выключает тестовый режим аккаунтов/прокси."""
        return await self._request("POST", "/api/accounts/test_mode", json={"enabled": enabled})

    async def replace_proxy(
        self,
        account_id: int | None = None,
        new_proxy_id: int | None = None,
        replace_slow: bool = True,
        replace_bad: bool = True
    ) -> dict:
        """Ð—Ð°Ð¼ÐµÐ½ÑÐµÑ‚ Ð¿Ñ€Ð¾ÐºÑÐ¸ Ñƒ Ð°ÐºÐºÐ°ÑƒÐ½Ñ‚Ð° Ð¸Ð»Ð¸ Ð³Ñ€ÑƒÐ¿Ð¿Ñ‹ Ð°ÐºÐºÐ°ÑƒÐ½Ñ‚Ð¾Ð²."""
        return await self._request(
            "POST",
            "/api/accounts/replace_proxy",
            json={
                "account_id": account_id,
                "new_proxy_id": new_proxy_id,
                "replace_slow": replace_slow,
                "replace_bad": replace_bad
            }
        )
    
    async def get_resources_stats(self) -> dict:
        """ÐŸÐ¾Ð»ÑƒÑ‡Ð°ÐµÑ‚ Ð°Ð³Ñ€ÐµÐ³Ð¸Ñ€Ð¾Ð²Ð°Ð½Ð½ÑƒÑŽ ÑÑ‚Ð°Ñ‚Ð¸ÑÑ‚Ð¸ÐºÑƒ Ð¿Ð¾ Ñ€ÐµÑÑƒÑ€ÑÐ°Ð¼."""
        return await self._request("GET", "/api/stats/resources")

    async def get_health_summary(self) -> dict:
        """Gets admin health summary for executor, monitor and queue state."""
        return await self._request("GET", "/api/stats/health/summary")
    

    async def validate_accounts(self) -> dict:
        """ÐŸÑ€Ð¾Ð²ÐµÑ€ÑÐµÑ‚ Ð²Ð°Ð»Ð¸Ð´Ð½Ð¾ÑÑ‚ÑŒ ÐºÑƒÐºÐ¾Ð² Ð²ÑÐµÑ… Ð°ÐºÑ‚Ð¸Ð²Ð½Ñ‹Ñ… Ð°ÐºÐºÐ°ÑƒÐ½Ñ‚Ð¾Ð² (Ð¿Ð°Ñ€Ð°Ð»Ð»ÐµÐ»ÑŒÐ½Ð¾)."""
        return await self._request("POST", "/api/accounts/validate", timeout=900)  # 15 Ð¼Ð¸Ð½ÑƒÑ‚ Ñ‚Ð°Ð¹Ð¼Ð°ÑƒÑ‚
    
    async def make_all_accounts_active(self) -> dict:
        """Ð£ÑÑ‚Ð°Ð½Ð°Ð²Ð»Ð¸Ð²Ð°ÐµÑ‚ ÑÑ‚Ð°Ñ‚ÑƒÑ 'active' Ð´Ð»Ñ Ð²ÑÐµÑ… Ð°ÐºÐºÐ°ÑƒÐ½Ñ‚Ð¾Ð² Ð² Ð±Ð°Ð·Ðµ Ð´Ð°Ð½Ð½Ñ‹Ñ…."""
        return await self._request("POST", "/api/accounts/make_all_active", timeout=60)
    
    async def reset_account_cooldowns(self) -> dict:
        """Сбрасывает runtime cooldown'ы и bad-proxy кеш (мягкий сброс, не трогает dead)."""
        return await self._request("POST", "/api/accounts/reset_cooldowns", timeout=60)
    
    async def get_validation_progress(self) -> dict:
        """ÐŸÐ¾Ð»ÑƒÑ‡Ð°ÐµÑ‚ Ñ‚ÐµÐºÑƒÑ‰Ð¸Ð¹ Ð¿Ñ€Ð¾Ð³Ñ€ÐµÑÑ Ð²Ð°Ð»Ð¸Ð´Ð°Ñ†Ð¸Ð¸ Ð°ÐºÐºÐ°ÑƒÐ½Ñ‚Ð¾Ð²."""
        return await self._request("GET", "/api/accounts/validate/progress", timeout=10)
    
    # Ð˜Ð¡ÐŸÐ ÐÐ’Ð›Ð•ÐÐž: Ð£Ð´Ð°Ð»ÐµÐ½Ñ‹ Ð¼ÐµÑ‚Ð¾Ð´Ñ‹ validate_proxies Ð¸ get_proxy_validation_progress - Ð²ÑÐµ Ð¿Ñ€Ð¾ÐºÑÐ¸ ÑÑ‡Ð¸Ñ‚Ð°ÑŽÑ‚ÑÑ Ñ€Ð°Ð±Ð¾Ñ‡Ð¸Ð¼Ð¸
    
    async def recheck_dead_accounts(self) -> dict:
        """ÐŸÐµÑ€ÐµÐ¿Ñ€Ð¾Ð²ÐµÑ€ÑÐµÑ‚ Ð²ÑÐµ Ð¼Ñ‘Ñ€Ñ‚Ð²Ñ‹Ðµ Ð°ÐºÐºÐ°ÑƒÐ½Ñ‚Ñ‹."""
        return await self._request("POST", "/api/accounts/recheck_dead", timeout=900)
    
    async def get_recheck_dead_progress(self) -> dict:
        """ÐŸÐ¾Ð»ÑƒÑ‡Ð°ÐµÑ‚ Ð¿Ñ€Ð¾Ð³Ñ€ÐµÑÑ Ð¿ÐµÑ€ÐµÐ¿Ñ€Ð¾Ð²ÐµÑ€ÐºÐ¸ Ð¼Ñ‘Ñ€Ñ‚Ð²Ñ‹Ñ… Ð°ÐºÐºÐ°ÑƒÐ½Ñ‚Ð¾Ð²."""
        return await self._request("GET", "/api/accounts/recheck_dead/progress", timeout=10)
    
    async def validate_single_account(self, account_id: int) -> dict:
        """ÐŸÑ€Ð¾Ð²ÐµÑ€ÑÐµÑ‚ Ð²Ð°Ð»Ð¸Ð´Ð½Ð¾ÑÑ‚ÑŒ ÐºÑƒÐºÐ¾Ð² Ð¾Ð´Ð½Ð¾Ð³Ð¾ Ð°ÐºÐºÐ°ÑƒÐ½Ñ‚Ð°."""
        return await self._request("POST", f"/api/accounts/validate/{account_id}", timeout=60)
    
    async def close(self):
        """Ð—Ð°ÐºÑ€Ñ‹Ð²Ð°ÐµÑ‚ HTTP ÐºÐ»Ð¸ÐµÐ½Ñ‚. Ð’Ñ‹Ð·Ñ‹Ð²Ð°ÐµÑ‚ÑÑ Ð¿Ñ€Ð¸ Ð·Ð°Ð²ÐµÑ€ÑˆÐµÐ½Ð¸Ð¸ Ñ€Ð°Ð±Ð¾Ñ‚Ñ‹ Ð±Ð¾Ñ‚Ð°."""
        await self._client.aclose()

    async def list_task_templates(self, user_id: int) -> list:
        """ÐŸÐ¾Ð»ÑƒÑ‡Ð°ÐµÑ‚ ÑÐ¿Ð¸ÑÐ¾Ðº ÑˆÐ°Ð±Ð»Ð¾Ð½Ð¾Ð² Ð·Ð°Ð´Ð°Ñ‡."""
        return await self._request("GET", f"/api/templates/task?user_id={user_id}")

    async def create_task_template(
        self,
        user_id: int,
        name: str,
        main_comment: str,
        text_variants: Optional[list[str]] = None,
        replies: Optional[list[str]] = None,
        reply1: Optional[str] = None,
        reply2: Optional[str] = None,
        reply3: Optional[str] = None,
        likes: int = 0,
        settings: Optional[dict] = None,
        template_type: str = "full",
    ) -> dict:
        """Создаёт шаблон задачи."""
        normalized_name = self._trim_text(name)
        normalized_main_comment = self._meaningful_user_text(main_comment)
        if not normalized_name:
            raise APIRequestError("Название шаблона не может быть пустым.")

        normalized_settings = dict(settings or {})
        normalized_template_type = str(
            normalized_settings.get("template_type")
            or normalized_settings.get("template_kind")
            or template_type
            or "full"
        ).strip().lower()
        if normalized_template_type in {"reply_only", "reply-only", "replies", "replies-only"}:
            normalized_template_type = "replies_only"
        if normalized_template_type not in {"full", "replies_only"}:
            raise APIRequestError("Тип шаблона должен быть full или replies_only.")
        normalized_settings["template_type"] = normalized_template_type

        normalized_text_variants = [
            text
            for variant in (text_variants or [])
            for text in [self._meaningful_user_text(variant)]
            if text
        ]

        normalized_replies = [
            text
            for reply in (replies or [])
            for text in [self._meaningful_user_text(reply)]
            if text
        ][:MAX_REPLIES]
        normalized_reply1 = normalized_replies[0] if len(normalized_replies) >= 1 else self._meaningful_user_text(reply1)
        normalized_reply2 = normalized_replies[1] if len(normalized_replies) >= 2 else self._meaningful_user_text(reply2)
        normalized_reply3 = normalized_replies[2] if len(normalized_replies) >= 3 else self._meaningful_user_text(reply3)

        if normalized_template_type == "replies_only":
            normalized_main_comment = normalized_main_comment or REPLIES_ONLY_MAIN_PLACEHOLDER
            normalized_text_variants = []
            if not normalized_replies and not any((normalized_reply1, normalized_reply2, normalized_reply3)):
                raise APIRequestError("Для шаблона ответов нужен хотя бы один ответ.")
        else:
            if normalized_text_variants:
                normalized_main_comment = normalized_text_variants[0]
            if not normalized_main_comment:
                raise APIRequestError("Основной комментарий шаблона не может быть пустым.")
            if len(normalized_text_variants) < 3:
                raise APIRequestError("Для шаблона нужно минимум 3 варианта комментария.")

        normalized_settings["text_variants"] = normalized_text_variants
        normalized_settings["replies"] = normalized_replies or [
            reply_value
            for reply_value in (normalized_reply1, normalized_reply2, normalized_reply3)
            if reply_value
        ]
        normalized_settings["reply_count"] = len(normalized_settings["replies"])
        for idx in range(4, MAX_REPLIES + 1):
            normalized_settings.pop(f"reply{idx}", None)
        for idx, reply_value in enumerate(normalized_settings["replies"][3:], start=4):
            normalized_settings[f"reply{idx}"] = reply_value

        return await self._request(
            "POST",
            "/api/templates/task",
            json={
                "user_id": user_id,
                "name": normalized_name,
                "main_comment": normalized_main_comment,
                "reply1": normalized_reply1,
                "reply2": normalized_reply2,
                "reply3": normalized_reply3,
                "likes": likes,
                "settings": normalized_settings
            },
            timeout=20.0,
            retry_safe=False,
        )

    async def list_comment_templates(self, user_id: int, type: Optional[str] = None) -> list:
        """ÐŸÐ¾Ð»ÑƒÑ‡Ð°ÐµÑ‚ ÑÐ¿Ð¸ÑÐ¾Ðº ÑˆÐ°Ð±Ð»Ð¾Ð½Ð¾Ð² ÐºÐ¾Ð¼Ð¼ÐµÐ½Ñ‚Ð°Ñ€Ð¸ÐµÐ²."""
        url = f"/api/templates/comment?user_id={user_id}"
        if type:
            url += f"&type={type}"
        return await self._request("GET", url)
    
    async def get_task_template(self, template_id: int, user_id: int) -> dict:
        """ÐŸÐ¾Ð»ÑƒÑ‡Ð°ÐµÑ‚ ÑˆÐ°Ð±Ð»Ð¾Ð½ Ð·Ð°Ð´Ð°Ñ‡Ð¸ Ð¿Ð¾ ID."""
        return await self._request("GET", f"/api/templates/task/{template_id}?user_id={user_id}")
    
    async def update_task_template(
        self,
        template_id: int,
        user_id: int,
        name: Optional[str] | object = _UNSET,
        main_comment: Optional[str] | object = _UNSET,
        text_variants: Optional[list[str]] | object = _UNSET,
        replies: Optional[list[str]] | object = _UNSET,
        reply1: Optional[str] | object = _UNSET,
        reply2: Optional[str] | object = _UNSET,
        reply3: Optional[str] | object = _UNSET,
        likes: Optional[int] | object = _UNSET,
        settings: Optional[dict] | object = _UNSET
    ) -> dict:
        """Обновляет шаблон задачи."""
        data = {}
        if name is not _UNSET:
            data["name"] = name
        merged_settings = None if settings is _UNSET else dict(settings or {})
        if main_comment is not _UNSET:
            data["main_comment"] = self._preserve_user_text(main_comment)
        if text_variants is not _UNSET:
            variants_payload = [
                text
                for variant in (text_variants or [])
                for text in [self._meaningful_user_text(variant)]
                if text
            ]
            merged_settings = dict(merged_settings or {})
            merged_settings["text_variants"] = variants_payload
            if variants_payload and "main_comment" not in data:
                data["main_comment"] = variants_payload[0]
        if replies is not _UNSET:
            replies_payload = [
                text
                for reply in (replies or [])
                for text in [self._meaningful_user_text(reply)]
                if text
            ][:MAX_REPLIES]
            merged_settings = dict(merged_settings or {})
            merged_settings["replies"] = replies_payload
            merged_settings["reply_count"] = len(replies_payload)
            for idx in range(4, MAX_REPLIES + 1):
                merged_settings.pop(f"reply{idx}", None)
            for idx, reply_text in enumerate(replies_payload[3:], start=4):
                merged_settings[f"reply{idx}"] = reply_text
            if reply1 is _UNSET:
                data["reply1"] = replies_payload[0] if len(replies_payload) >= 1 else None
            if reply2 is _UNSET:
                data["reply2"] = replies_payload[1] if len(replies_payload) >= 2 else None
            if reply3 is _UNSET:
                data["reply3"] = replies_payload[2] if len(replies_payload) >= 3 else None
        if reply1 is not _UNSET:
            data["reply1"] = self._preserve_user_text(reply1)
        if reply2 is not _UNSET:
            data["reply2"] = self._preserve_user_text(reply2)
        if reply3 is not _UNSET:
            data["reply3"] = self._preserve_user_text(reply3)
        if likes is not _UNSET:
            data["likes"] = likes
        if merged_settings is not None:
            data["settings"] = merged_settings

        return await self._request("PUT", f"/api/templates/task/{template_id}?user_id={user_id}", json=data)

    async def delete_task_template(self, template_id: int, user_id: int) -> dict:
        """Ð£Ð´Ð°Ð»ÑÐµÑ‚ ÑˆÐ°Ð±Ð»Ð¾Ð½ Ð·Ð°Ð´Ð°Ñ‡Ð¸."""
        return await self._request("DELETE", f"/api/templates/task/{template_id}?user_id={user_id}")

    async def list_users_page(
        self,
        limit: int = 20,
        offset: int = 0,
        status: str = "all",
        q: str | None = None,
    ) -> dict:
        """Returns a paginated admin users page."""
        from urllib.parse import urlencode
        params = {"limit": limit, "offset": offset, "status": status}
        if q:
            params["q"] = q
        url = "/api/users?" + urlencode(params)
        data = await self._request("GET", url)
        if isinstance(data, list):
            return {
                "items": data,
                "total": len(data),
                "limit": limit,
                "offset": offset,
                "page": (offset // limit) + 1 if limit else 1,
                "total_pages": 1,
                "filters": {"status": status, "q": q or ""},
                "counts": {"total": len(data)},
            }
        return data

    async def list_users(
        self,
        limit: int = 50,
        offset: int = 0,
        status: str = "all",
        q: str | None = None,
    ) -> list:
        """ÐŸÐ¾Ð»ÑƒÑ‡Ð°ÐµÑ‚ ÑÐ¿Ð¸ÑÐ¾Ðº Ð¿Ð¾Ð»ÑŒÐ·Ð¾Ð²Ð°Ñ‚ÐµÐ»ÐµÐ¹ (Ñ‚Ð¾Ð»ÑŒÐºÐ¾ Ð´Ð»Ñ Ð°Ð´Ð¼Ð¸Ð½Ð¾Ð²)."""
        page = await self.list_users_page(limit=limit, offset=offset, status=status, q=q)
        return list(page.get("items") or [])
    
    async def get_user(self, user_id: int) -> dict:
        """ÐŸÐ¾Ð»ÑƒÑ‡Ð°ÐµÑ‚ Ð¸Ð½Ñ„Ð¾Ñ€Ð¼Ð°Ñ†Ð¸ÑŽ Ð¾ Ð¿Ð¾Ð»ÑŒÐ·Ð¾Ð²Ð°Ñ‚ÐµÐ»Ðµ, Ð²ÐºÐ»ÑŽÑ‡Ð°Ñ Ð±Ð°Ð»Ð°Ð½Ñ."""
        return await self._request("GET", f"/api/users/{user_id}")
    
    
    async def add_user_balance(self, user_id: int, amount: float) -> dict:
        """Ð”Ð¾Ð±Ð°Ð²Ð»ÑÐµÑ‚ Ð±Ð°Ð»Ð°Ð½Ñ Ð¿Ð¾Ð»ÑŒÐ·Ð¾Ð²Ð°Ñ‚ÐµÐ»ÑŽ (Ñ‚Ð¾Ð»ÑŒÐºÐ¾ Ð´Ð»Ñ Ð°Ð´Ð¼Ð¸Ð½Ð¾Ð²)."""
        return await self._request("POST", f"/api/users/{user_id}/balance/add", json={"amount": amount})
    
    async def subtract_user_balance(self, user_id: int, amount: float) -> dict:
        """Ð’Ñ‹Ñ‡Ð¸Ñ‚Ð°ÐµÑ‚ Ð±Ð°Ð»Ð°Ð½Ñ Ñƒ Ð¿Ð¾Ð»ÑŒÐ·Ð¾Ð²Ð°Ñ‚ÐµÐ»Ñ (Ñ‚Ð¾Ð»ÑŒÐºÐ¾ Ð´Ð»Ñ Ð°Ð´Ð¼Ð¸Ð½Ð¾Ð²)."""
        return await self._request("POST", f"/api/users/{user_id}/balance/subtract", json={"amount": amount})
    
    async def block_user(self, user_id: int) -> dict:
        """Ð‘Ð»Ð¾ÐºÐ¸Ñ€ÑƒÐµÑ‚ Ð¿Ð¾Ð»ÑŒÐ·Ð¾Ð²Ð°Ñ‚ÐµÐ»Ñ (Ñ‚Ð¾Ð»ÑŒÐºÐ¾ Ð´Ð»Ñ Ð°Ð´Ð¼Ð¸Ð½Ð¾Ð²)."""
        return await self._request("POST", f"/api/users/{user_id}/block")
    
    async def unblock_user(self, user_id: int) -> dict:
        """Ð Ð°Ð·Ð±Ð»Ð¾ÐºÐ¸Ñ€ÑƒÐµÑ‚ Ð¿Ð¾Ð»ÑŒÐ·Ð¾Ð²Ð°Ñ‚ÐµÐ»Ñ (Ñ‚Ð¾Ð»ÑŒÐºÐ¾ Ð´Ð»Ñ Ð°Ð´Ð¼Ð¸Ð½Ð¾Ð²)."""
        return await self._request("POST", f"/api/users/{user_id}/unblock")

    async def grant_full_access(self, user_id: int) -> dict:
        """Grant bot access to a worker without changing their role."""
        result = await self._request("POST", f"/api/users/{user_id}/full-access/grant")
        telegram_id = result.get("telegram_id") if isinstance(result, dict) else None
        if telegram_id:
            self.clear_user_cache(int(telegram_id))
        else:
            self.clear_user_cache()
        return result

    async def revoke_full_access(self, user_id: int) -> dict:
        """Revoke bot access from a worker without changing their role."""
        result = await self._request("POST", f"/api/users/{user_id}/full-access/revoke")
        telegram_id = result.get("telegram_id") if isinstance(result, dict) else None
        if telegram_id:
            self.clear_user_cache(int(telegram_id))
        else:
            self.clear_user_cache()
        return result
    
    async def get_pricing(self) -> dict:
        """ÐŸÐ¾Ð»ÑƒÑ‡Ð°ÐµÑ‚ Ñ‚ÐµÐºÑƒÑ‰Ð¸Ðµ Ñ†ÐµÐ½Ñ‹."""
        return await self._request("GET", "/api/pricing")
    
    async def update_pricing(self, price_success: Optional[float] = None, price_failure: Optional[float] = None) -> dict:
        """ÐžÐ±Ð½Ð¾Ð²Ð»ÑÐµÑ‚ Ñ†ÐµÐ½Ñ‹ (Ñ‚Ð¾Ð»ÑŒÐºÐ¾ Ð´Ð»Ñ Ð°Ð´Ð¼Ð¸Ð½Ð¾Ð²)."""
        data = {}
        if price_success is not None:
            data["price_success"] = price_success
        if price_failure is not None:
            data["price_failure"] = price_failure
        # Ð˜ÑÐ¿Ð¾Ð»ÑŒÐ·ÑƒÐµÐ¼ Ð°Ð´Ð¼Ð¸Ð½ Ñ‚Ð¾ÐºÐµÐ½ Ð¸Ð· ÐºÐ¾Ð½Ñ„Ð¸Ð³Ð°
        headers = {"X-Admin-Token": self.token or ""}
        return await self._request("PUT", "/api/pricing", json=data, headers=headers)

    async def list_accounts(self, status: str | None = None, limit: int = 50, offset: int = 0) -> list:
        """Ð˜Ð¡ÐŸÐ ÐÐ’Ð›Ð•ÐÐž: ÐŸÐ¾Ð»ÑƒÑ‡Ð°ÐµÑ‚ ÑÐ¿Ð¸ÑÐ¾Ðº Ð°ÐºÐºÐ°ÑƒÐ½Ñ‚Ð¾Ð² Ñ ÑÐµÑ€Ð²ÐµÑ€Ð½Ð¾Ð¹ Ð¿Ð°Ð³Ð¸Ð½Ð°Ñ†Ð¸ÐµÐ¹."""
        from urllib.parse import urlencode
        params = {"limit": limit, "offset": offset}
        if status:
            params["status"] = status
        url = f"/api/accounts?{urlencode(params)}"
        return await self._request("GET", url)
    
    # ========== ÐÐžÐ’Ð«Ð• Ð”Ð•Ð™Ð¡Ð¢Ð’Ð˜Ð¯ ÐÐÐ” ÐÐšÐšÐÐ£ÐÐ¢ÐÐœÐ˜ ==========
    
    async def export_accounts(self, status: str | None = None) -> dict:
        """Ð­ÐºÑÐ¿Ð¾Ñ€Ñ‚Ð¸Ñ€ÑƒÐµÑ‚ Ð°ÐºÐºÐ°ÑƒÐ½Ñ‚Ñ‹ Ð² Ñ„Ð¾Ñ€Ð¼Ð°Ñ‚Ðµ Ð´Ð»Ñ Ð¸Ð¼Ð¿Ð¾Ñ€Ñ‚Ð°."""
        from urllib.parse import urlencode
        params = {}
        if status:
            params["status"] = status
        endpoint = f"/api/accounts/export" + ("?" + urlencode(params) if params else "")
        return await self._request("GET", endpoint)
    
    async def delete_accounts_by_status(self, status: str) -> dict:
        """Ð£Ð´Ð°Ð»ÑÐµÑ‚ Ð°ÐºÐºÐ°ÑƒÐ½Ñ‚Ñ‹ Ð¿Ð¾ ÑÑ‚Ð°Ñ‚ÑƒÑÑƒ."""
        return await self._request("DELETE", f"/api/accounts/by_status/{status}")

    async def delete_all_accounts(self) -> dict:
        """Deletes every account from the account pool."""
        return await self._request("DELETE", "/api/accounts/all")
    
    async def get_account_details(self, account_id: int) -> dict:
        """ÐŸÐ¾Ð»ÑƒÑ‡Ð°ÐµÑ‚ Ð´ÐµÑ‚Ð°Ð»ÑŒÐ½ÑƒÑŽ Ð¸Ð½Ñ„Ð¾Ñ€Ð¼Ð°Ñ†Ð¸ÑŽ Ð¾Ð± Ð°ÐºÐºÐ°ÑƒÐ½Ñ‚Ðµ."""
        return await self._request("GET", f"/api/accounts/{account_id}")
    
    async def update_account(
        self,
        account_id: int,
        label: str | None = None,
        cookies: str | None = None,
        cookie_file: str | None = None,
        user_agent: str | None = None,
        proxy: str | None = None,
        status: str | None = None
    ) -> dict:
        """ÐžÐ±Ð½Ð¾Ð²Ð»ÑÐµÑ‚ Ð´Ð°Ð½Ð½Ñ‹Ðµ Ð°ÐºÐºÐ°ÑƒÐ½Ñ‚Ð°."""
        update_data = {}
        if label is not None:
            update_data["label"] = label
        if cookies is not None:
            update_data["cookies"] = cookies
        if cookie_file is not None:
            update_data["cookie_file"] = cookie_file
        if user_agent is not None:
            update_data["user_agent"] = user_agent
        if proxy is not None:
            update_data["proxy"] = proxy
        if status is not None:
            update_data["status"] = status
        
        return await self._request("PUT", f"/api/accounts/{account_id}", json=update_data)
    
    async def reset_account_errors(self, account_id: int) -> dict:
        """Ð¡Ð±Ñ€Ð°ÑÑ‹Ð²Ð°ÐµÑ‚ ÑÑ‡ÐµÑ‚Ñ‡Ð¸ÐºÐ¸ Ð¾ÑˆÐ¸Ð±Ð¾Ðº Ð°ÐºÐºÐ°ÑƒÐ½Ñ‚Ð°."""
        return await self._request("POST", f"/api/accounts/{account_id}/reset_errors")
    
    async def bulk_update_account_status(self, account_ids: list[int], new_status: str) -> dict:
        """ÐœÐ°ÑÑÐ¾Ð²Ð¾ Ð¸Ð·Ð¼ÐµÐ½ÑÐµÑ‚ ÑÑ‚Ð°Ñ‚ÑƒÑ Ð°ÐºÐºÐ°ÑƒÐ½Ñ‚Ð¾Ð²."""
        return await self._request(
            "POST",
            "/api/accounts/bulk_update_status",
            json={"account_ids": account_ids, "new_status": new_status}
        )
    
    async def search_accounts(
        self,
        q: str | None = None,
        status: str | None = None,
        limit: int = 50,
        offset: int = 0
    ) -> list:
        """ÐŸÐ¾Ð¸ÑÐº Ð°ÐºÐºÐ°ÑƒÐ½Ñ‚Ð¾Ð² Ð¿Ð¾ label Ð¸Ð»Ð¸ Ð´Ñ€ÑƒÐ³Ð¸Ð¼ Ð¿Ð°Ñ€Ð°Ð¼ÐµÑ‚Ñ€Ð°Ð¼."""
        from urllib.parse import urlencode
        params = {"limit": limit, "offset": offset}
        if q:
            params["q"] = q
        if status:
            params["status"] = status
        endpoint = f"/api/accounts/search?{urlencode(params)}"
        return await self._request("GET", endpoint)
    
    async def detect_all_geo(self) -> dict:
        """ÐžÐ¿Ñ€ÐµÐ´ÐµÐ»ÑÐµÑ‚ Ð³ÐµÐ¾ Ð´Ð»Ñ Ð²ÑÐµÑ… Ð°ÐºÐºÐ°ÑƒÐ½Ñ‚Ð¾Ð² Ð¸ Ð¿Ñ€Ð¾ÐºÑÐ¸ Ð±ÐµÐ· Ð³ÐµÐ¾."""
        return await self._request("POST", "/api/geo/detect", timeout=300)  # 5 Ð¼Ð¸Ð½ÑƒÑ‚ Ñ‚Ð°Ð¹Ð¼Ð°ÑƒÑ‚
    
    async def get_geo_detection_progress(self) -> dict:
        """ÐŸÐ¾Ð»ÑƒÑ‡Ð°ÐµÑ‚ Ñ‚ÐµÐºÑƒÑ‰Ð¸Ð¹ Ð¿Ñ€Ð¾Ð³Ñ€ÐµÑÑ Ð¾Ð¿Ñ€ÐµÐ´ÐµÐ»ÐµÐ½Ð¸Ñ Ð³ÐµÐ¾."""
        return await self._request("GET", "/api/geo/detect/progress", timeout=10)

    async def delete_dead_accounts(self) -> dict:
        """Ð£Ð´Ð°Ð»ÑÐµÑ‚ Ð²ÑÐµ Ð°ÐºÐºÐ°ÑƒÐ½Ñ‚Ñ‹ ÑÐ¾ ÑÑ‚Ð°Ñ‚ÑƒÑÐ¾Ð¼ 'dead'."""
        return await self._request("DELETE", "/api/accounts/dead")
    
    async def get_proxy_settings(self) -> dict:
        """ÐŸÐ¾Ð»ÑƒÑ‡Ð°ÐµÑ‚ Ð½Ð°ÑÑ‚Ñ€Ð¾Ð¹ÐºÐ¸ Ñ„Ð¸Ð»ÑŒÑ‚Ñ€Ð°Ñ†Ð¸Ð¸ Ð¿Ñ€Ð¾ÐºÑÐ¸."""
        return await self._request("GET", "/api/settings/proxy")
    
    _UNSET = object()

    async def update_proxy_settings(
        self,
        proxy_type_filter: str | None | object = _UNSET,
        proxy_prefer_mobile: bool | None = None,
    ) -> dict:
        """ÐžÐ±Ð½Ð¾Ð²Ð»ÑÐµÑ‚ Ð½Ð°ÑÑ‚Ñ€Ð¾Ð¹ÐºÐ¸ Ñ„Ð¸Ð»ÑŒÑ‚Ñ€Ð°Ñ†Ð¸Ð¸ Ð¿Ñ€Ð¾ÐºÑÐ¸."""
        payload = {}
        if proxy_type_filter is not self._UNSET:
            payload["proxy_type_filter"] = proxy_type_filter
        if proxy_prefer_mobile is not None:
            payload["proxy_prefer_mobile"] = proxy_prefer_mobile
        return await self._request("POST", "/api/settings/proxy", json=payload)

    async def get_boost_likes_settings(self) -> dict:
        """Get likes settings."""
        return await self._request("GET", "/api/settings/boost_likes")

    async def update_boost_likes_settings(
        self,
        boost_likes_enabled: bool | None = None,
        boost_likes_quantity: int | None = None,
        boost_likes_target_mode: str | None = None,
    ) -> dict:
        """Update likes settings."""
        payload = {}
        if boost_likes_enabled is not None:
            payload["boost_likes_enabled"] = boost_likes_enabled
        if boost_likes_quantity is not None:
            payload["boost_likes_quantity"] = boost_likes_quantity
        if boost_likes_target_mode is not None:
            payload["boost_likes_target_mode"] = boost_likes_target_mode
        return await self._request("POST", "/api/settings/boost_likes", json=payload)

    async def get_software_mode_settings(self) -> dict:
        """Returns the current global software mode."""
        return await self._request("GET", "/api/settings/software_mode")

    async def update_software_mode(self, software_mode: str) -> dict:
        """Updates the global software mode."""
        return await self._request(
            "POST",
            "/api/settings/software_mode",
            json={"software_mode": software_mode},
        )

    async def get_commenting_mode_settings(self) -> dict:
        """Returns the current commenting workflow mode."""
        return await self._request("GET", "/api/settings/commenting_mode")

    async def update_commenting_mode(self, commenting_mode: str) -> dict:
        """Updates the commenting workflow mode."""
        return await self._request(
            "POST",
            "/api/settings/commenting_mode",
            json={"commenting_mode": commenting_mode},
        )

    async def get_order_execution_strategy_settings(self) -> dict:
        """Returns the current order execution strategy."""
        return await self._request("GET", "/api/settings/order_execution_strategy")

    async def update_order_execution_strategy(self, order_execution_strategy: str) -> dict:
        """Updates the current order execution strategy."""
        return await self._request(
            "POST",
            "/api/settings/order_execution_strategy",
            json={"order_execution_strategy": order_execution_strategy},
        )

    async def get_monitor_parallel_browsers_settings(self) -> dict:
        """Лимит одновременных Playwright в video_monitor_worker."""
        return await self._request("GET", "/api/settings/monitor_parallel_browsers")

    async def update_monitor_parallel_browsers_settings(self, monitor_max_parallel_browsers: int) -> dict:
        return await self._request(
            "POST",
            "/api/settings/monitor_parallel_browsers",
            json={"monitor_max_parallel_browsers": monitor_max_parallel_browsers},
        )
    
    # ========== VIDEO PARSING API ==========

    async def get_monitoring_summary(self) -> dict:
        """Gets global monitoring summary for admin/debug screens."""
        return await self._request(
            "GET",
            "/api/video-parsing/monitoring/summary",
            retry_safe=False,
        )

    async def list_video_creators(self, user_id: int) -> list:
        """Ð˜Ð¡ÐŸÐ ÐÐ’Ð›Ð•ÐÐž: ÐŸÐ¾Ð»ÑƒÑ‡Ð°ÐµÑ‚ ÑÐ¿Ð¸ÑÐ¾Ðº Ð±Ð»Ð¾Ð³ÐµÑ€Ð¾Ð² Ð´Ð»Ñ Ð¼Ð¾Ð½Ð¸Ñ‚Ð¾Ñ€Ð¸Ð½Ð³Ð° Ð´Ð»Ñ ÐºÐ¾Ð½ÐºÑ€ÐµÑ‚Ð½Ð¾Ð³Ð¾ Ð¿Ð¾Ð»ÑŒÐ·Ð¾Ð²Ð°Ñ‚ÐµÐ»Ñ."""
        from urllib.parse import urlencode
        params = {"user_id": user_id}
        url = f"/api/video-parsing/creators?{urlencode(params)}"
        return await self._request("GET", url)

    async def list_creator_suggestions(
        self,
        user_id: int,
        limit: int = 8,
        offset: int = 0,
        include_total: bool = False,
    ) -> list | dict:
        """Ð’Ð¾Ð·Ð²Ñ€Ð°Ñ‰Ð°ÐµÑ‚ Ð±Ð»Ð¾Ð³ÐµÑ€Ð¾Ð², ÐºÐ¾Ñ‚Ð¾Ñ€Ñ‹Ðµ ÑƒÐ¶Ðµ ÐµÑÑ‚ÑŒ Ñƒ Ð´Ñ€ÑƒÐ³Ð¸Ñ… ÑÐ¾Ñ‚Ñ€ÑƒÐ´Ð½Ð¸ÐºÐ¾Ð², Ð½Ð¾ ÐµÑ‰Ñ‘ Ð½Ðµ Ð´Ð¾Ð±Ð°Ð²Ð»ÐµÐ½Ñ‹ Ñƒ Ð¿Ð¾Ð»ÑŒÐ·Ð¾Ð²Ð°Ñ‚ÐµÐ»Ñ."""
        from urllib.parse import urlencode
        params = {
            "user_id": user_id,
            "limit": limit,
            "offset": offset,
            "include_total": str(include_total).lower(),
        }
        url = f"/api/video-parsing/creators/suggestions?{urlencode(params)}"
        return await self._request("GET", url)

    async def get_video_notifications(self, user_id: int) -> dict:
        """Gets the user's global video notification switch."""
        from urllib.parse import urlencode
        params = {"user_id": user_id}
        url = f"/api/video-parsing/notifications?{urlencode(params)}"
        return await self._request("GET", url)

    async def update_video_notifications(self, user_id: int, enabled: bool) -> dict:
        """Updates the user's global video notification switch."""
        from urllib.parse import urlencode
        params = {"user_id": user_id}
        url = f"/api/video-parsing/notifications?{urlencode(params)}"
        return await self._request("PATCH", url, json={"enabled": enabled})
    
    async def create_video_creator(
        self,
        user_id: int,  # Ð˜Ð¡ÐŸÐ ÐÐ’Ð›Ð•ÐÐž: Ð”Ð¾Ð±Ð°Ð²Ð»ÐµÐ½ user_id
        username: str,
        enabled: bool = True,
        interval_minutes: int = 30,
        mode: str = "manual_review",
        template_ids: list[int] | None = None,
        auto_start: bool = False,
    ) -> dict:
        """Ð˜Ð¡ÐŸÐ ÐÐ’Ð›Ð•ÐÐž: Ð¡Ð¾Ð·Ð´Ð°Ñ‘Ñ‚ Ð½Ð¾Ð²Ð¾Ð³Ð¾ Ð±Ð»Ð¾Ð³ÐµÑ€Ð° Ð´Ð»Ñ Ð¼Ð¾Ð½Ð¸Ñ‚Ð¾Ñ€Ð¸Ð½Ð³Ð° Ð´Ð»Ñ ÐºÐ¾Ð½ÐºÑ€ÐµÑ‚Ð½Ð¾Ð³Ð¾ Ð¿Ð¾Ð»ÑŒÐ·Ð¾Ð²Ð°Ñ‚ÐµÐ»Ñ."""
        from urllib.parse import urlencode
        params = {"user_id": user_id}
        url = f"/api/video-parsing/creators?{urlencode(params)}"
        return await self._request(
            "POST",
            url,
            json={
                "username": username,
                "enabled": enabled,
                "interval_minutes": interval_minutes,
                "mode": mode,
                "template_ids": template_ids,
                "auto_start": auto_start,
            }
        )
    
    async def update_video_creator(
        self,
        creator_id: int,
        user_id: int,  # Ð˜Ð¡ÐŸÐ ÐÐ’Ð›Ð•ÐÐž: Ð”Ð¾Ð±Ð°Ð²Ð»ÐµÐ½ user_id
        enabled: bool | None = None,
        interval_minutes: int | None = None,
        mode: str | None = None,
        template_ids: list[int] | None = None,
        default_template_id: int | None = None,  # P1 FIX: Ð”Ð¾Ð±Ð°Ð²Ð»ÐµÐ½Ð° Ð¿Ð¾Ð´Ð´ÐµÑ€Ð¶ÐºÐ° default_template_id
        auto_start: bool | None = None,
    ) -> dict:
        """Ð˜Ð¡ÐŸÐ ÐÐ’Ð›Ð•ÐÐž: ÐžÐ±Ð½Ð¾Ð²Ð»ÑÐµÑ‚ Ð½Ð°ÑÑ‚Ñ€Ð¾Ð¹ÐºÐ¸ Ð±Ð»Ð¾Ð³ÐµÑ€Ð°. Ð¢Ñ€ÐµÐ±ÑƒÐµÑ‚ user_id Ð´Ð»Ñ Ð¿Ñ€Ð¾Ð²ÐµÑ€ÐºÐ¸ Ð¿Ñ€Ð°Ð²."""
        from urllib.parse import urlencode
        params = {"user_id": user_id}
        url = f"/api/video-parsing/creators/{creator_id}?{urlencode(params)}"
        payload = {}
        if enabled is not None:
            payload["enabled"] = enabled
        if interval_minutes is not None:
            payload["interval_minutes"] = interval_minutes
        if mode is not None:
            payload["mode"] = mode
        if template_ids is not None:
            payload["template_ids"] = template_ids
        if default_template_id is not None:  # P1 FIX: Ð”Ð¾Ð±Ð°Ð²Ð»ÐµÐ½Ð° Ð¿Ð¾Ð´Ð´ÐµÑ€Ð¶ÐºÐ° default_template_id
            payload["default_template_id"] = default_template_id
        if auto_start is not None:
            payload["auto_start"] = auto_start
        
        return await self._request("PATCH", url, json=payload)

    async def bulk_update_video_creators(
        self,
        user_id: int,
        *,
        selector: dict,
        changes: dict,
        dry_run: bool = True,
    ) -> dict:
        """Preview or apply a bulk update for the user's video creators."""
        from urllib.parse import urlencode
        params = {"user_id": user_id}
        url = f"/api/video-parsing/creators/bulk-update?{urlencode(params)}"
        return await self._request(
            "POST",
            url,
            json={
                "selector": selector,
                "changes": changes,
                "dry_run": dry_run,
            },
        )

    async def delete_video_creator(self, creator_id: int, user_id: int) -> dict:
        """Ð˜Ð¡ÐŸÐ ÐÐ’Ð›Ð•ÐÐž: Ð£Ð´Ð°Ð»ÑÐµÑ‚ Ð±Ð»Ð¾Ð³ÐµÑ€Ð°. Ð¢Ñ€ÐµÐ±ÑƒÐµÑ‚ user_id Ð´Ð»Ñ Ð¿Ñ€Ð¾Ð²ÐµÑ€ÐºÐ¸ Ð¿Ñ€Ð°Ð²."""
        from urllib.parse import urlencode
        params = {"user_id": user_id}
        url = f"/api/video-parsing/creators/{creator_id}?{urlencode(params)}"
        return await self._request("DELETE", url)
    
    async def delete_all_bloggers(self) -> dict:
        """Удаляет всех блогеров из базы (только для админов)."""
        return await self._request("DELETE", "/api/video-parsing/admin/creators/all")
    
    async def run_video_creator_now(self, creator_id: int, user_id: int) -> dict:
        """Ð˜Ð¡ÐŸÐ ÐÐ’Ð›Ð•ÐÐž: Ð—Ð°Ð¿ÑƒÑÐºÐ°ÐµÑ‚ Ð¿Ñ€Ð¾Ð²ÐµÑ€ÐºÑƒ Ð±Ð»Ð¾Ð³ÐµÑ€Ð° Ð½ÐµÐ¼ÐµÐ´Ð»ÐµÐ½Ð½Ð¾. Ð¢Ñ€ÐµÐ±ÑƒÐµÑ‚ user_id Ð´Ð»Ñ Ð¿Ñ€Ð¾Ð²ÐµÑ€ÐºÐ¸ Ð¿Ñ€Ð°Ð²."""
        from urllib.parse import urlencode
        params = {"user_id": user_id}
        url = f"/api/video-parsing/creators/{creator_id}/run-now?{urlencode(params)}"
        return await self._request("POST", url)
    
    async def get_review_queue(self, user_id: int, limit: int = 20, offset: int = 0) -> list:
        """Ð˜Ð¡ÐŸÐ ÐÐ’Ð›Ð•ÐÐž: ÐŸÐ¾Ð»ÑƒÑ‡Ð°ÐµÑ‚ Ð¾Ñ‡ÐµÑ€ÐµÐ´ÑŒ Ð²Ð¸Ð´ÐµÐ¾ Ð½Ð° Ð¿Ñ€Ð¾Ð²ÐµÑ€ÐºÑƒ Ð´Ð»Ñ ÐºÐ¾Ð½ÐºÑ€ÐµÑ‚Ð½Ð¾Ð³Ð¾ Ð¿Ð¾Ð»ÑŒÐ·Ð¾Ð²Ð°Ñ‚ÐµÐ»Ñ."""
        from urllib.parse import urlencode
        params = {"user_id": user_id, "limit": limit, "offset": offset}
        url = f"/api/video-parsing/review-queue?{urlencode(params)}"
        return await self._request("GET", url)
    
    async def approve_video(self, video_id: int, user_id: int, template_id: int | None = None) -> dict:
        """Ð˜Ð¡ÐŸÐ ÐÐ’Ð›Ð•ÐÐž: ÐŸÐ¾Ð´Ñ‚Ð²ÐµÑ€Ð¶Ð´Ð°ÐµÑ‚ Ð²Ð¸Ð´ÐµÐ¾ Ð¸ ÑÐ¾Ð·Ð´Ð°Ñ‘Ñ‚ Ð·Ð°Ð´Ð°Ñ‡Ñƒ. Ð¢Ñ€ÐµÐ±ÑƒÐµÑ‚ user_id Ð´Ð»Ñ Ð¿Ñ€Ð¾Ð²ÐµÑ€ÐºÐ¸ Ð¿Ñ€Ð°Ð²."""
        from urllib.parse import urlencode
        params = {"user_id": user_id}
        url = f"/api/video-parsing/review/{video_id}/approve?{urlencode(params)}"
        payload = {}
        if template_id is not None:
            payload["template_id"] = template_id
        return await self._request("POST", url, json=payload)
    
    async def reject_video(self, video_id: int, user_id: int) -> dict:
        """Ð˜Ð¡ÐŸÐ ÐÐ’Ð›Ð•ÐÐž: ÐžÑ‚ÐºÐ»Ð¾Ð½ÑÐµÑ‚ Ð²Ð¸Ð´ÐµÐ¾. Ð¢Ñ€ÐµÐ±ÑƒÐµÑ‚ user_id Ð´Ð»Ñ Ð¿Ñ€Ð¾Ð²ÐµÑ€ÐºÐ¸ Ð¿Ñ€Ð°Ð²."""
        from urllib.parse import urlencode
        params = {"user_id": user_id}
        url = f"/api/video-parsing/review/{video_id}/reject?{urlencode(params)}"
        return await self._request("POST", url)
    
    async def list_video_videos(
        self,
        user_id: int,
        creator_id: int | None = None,
        status: str | None = None,
        limit: int = 20,
        offset: int = 0
    ) -> list:
        """ÐŸÐ¾Ð»ÑƒÑ‡Ð°ÐµÑ‚ ÑÐ¿Ð¸ÑÐ¾Ðº Ð²Ð¸Ð´ÐµÐ¾ Ñ Ñ„Ð¸Ð»ÑŒÑ‚Ñ€Ð°Ñ†Ð¸ÐµÐ¹."""
        from urllib.parse import urlencode
        params = {"user_id": user_id, "limit": limit, "offset": offset}
        if creator_id is not None:
            params["creator_id"] = creator_id
        if status:
            params["status"] = status
        url = f"/api/video-parsing/videos?{urlencode(params)}"
        return await self._request("GET", url)
    
    async def get_videos_by_ids(self, video_ids: list[int], user_id: int) -> list:
        """ÐŸÐ¾Ð»ÑƒÑ‡Ð°ÐµÑ‚ Ð²Ð¸Ð´ÐµÐ¾ Ð¿Ð¾ ÑÐ¿Ð¸ÑÐºÑƒ ID."""
        from urllib.parse import urlencode
        video_ids_str = ",".join(str(vid) for vid in video_ids)
        params = {"video_ids": video_ids_str, "user_id": user_id}
        url = f"/api/video-parsing/videos/by-ids?{urlencode(params)}"
        return await self._request("GET", url)
    
    async def get_auto_orders(self, user_id: int, limit: int = 20, offset: int = 0) -> list:
        """ÐŸÐ¾Ð»ÑƒÑ‡Ð°ÐµÑ‚ ÑÐ¿Ð¸ÑÐ¾Ðº Ð°Ð²Ñ‚Ð¾Ð¼Ð°Ñ‚Ð¸Ñ‡ÐµÑÐºÐ¸Ñ… Ð·Ð°ÐºÐ°Ð·Ð¾Ð² (Ð·Ð°Ð´Ð°Ñ‡, ÑÐ¾Ð·Ð´Ð°Ð½Ð½Ñ‹Ñ… Ð¸Ð· Ð²Ð¸Ð´ÐµÐ¾ Ð¿Ð°Ñ€ÑÐ¸Ð½Ð³Ð°)."""
        from urllib.parse import urlencode
        params = {"user_id": user_id, "limit": limit, "offset": offset}
        url = f"/api/video-parsing/auto-orders?{urlencode(params)}"
        return await self._request("GET", url)

    async def get_autopilot_report(
        self,
        user_id: int,
        limit: int = 5,
        offset: int = 0,
        *,
        status: str | None = None,
        creator_id: int | None = None,
        include_archived: bool = False,
    ) -> dict:
        """Gets autopilot-created comment orders with video links and pagination."""
        from urllib.parse import urlencode
        params = {"user_id": user_id, "limit": limit, "offset": offset}
        if status and status != "all":
            params["status"] = status
        if creator_id is not None:
            params["creator_id"] = int(creator_id)
        if include_archived:
            params["include_archived"] = "true"
        url = f"/api/video-parsing/autopilot-report?{urlencode(params)}"
        return await self._request("GET", url)

    async def get_autopilot_control(self, user_id: int) -> dict:
        """Gets global autopilot control mode and limits."""
        from urllib.parse import urlencode
        url = f"/api/video-parsing/autopilot-control?{urlencode({'user_id': user_id})}"
        return await self._request("GET", url)

    async def update_autopilot_control(
        self,
        user_id: int,
        *,
        mode: str | None = None,
        max_create_per_tick: int | None = None,
        max_create_per_creator: int | None = None,
        max_create_per_hour: int | None = None,
    ) -> dict:
        """Updates autopilot control mode and limits."""
        from urllib.parse import urlencode
        payload = {}
        if mode is not None:
            payload["mode"] = mode
        if max_create_per_tick is not None:
            payload["max_create_per_tick"] = int(max_create_per_tick)
        if max_create_per_creator is not None:
            payload["max_create_per_creator"] = int(max_create_per_creator)
        if max_create_per_hour is not None:
            payload["max_create_per_hour"] = int(max_create_per_hour)
        url = f"/api/video-parsing/autopilot-control?{urlencode({'user_id': user_id})}"
        return await self._request("POST", url, json=payload)

    async def get_autopilot_live_queue(self, user_id: int) -> dict:
        """Gets current autopilot queue/outbox state."""
        from urllib.parse import urlencode
        url = f"/api/video-parsing/autopilot-live-queue?{urlencode({'user_id': user_id})}"
        return await self._request("GET", url)

    async def cleanup_autopilot_stale_queue(self, user_id: int) -> dict:
        """Removes stale autopilot ids from the Redis task queue."""
        from urllib.parse import urlencode
        url = f"/api/video-parsing/autopilot-live-queue/cleanup-stale?{urlencode({'user_id': user_id})}"
        return await self._request("POST", url, json={})

    async def cleanup_autopilot_report(
        self,
        user_id: int,
        *,
        statuses: list[str] | None = None,
        older_than_days: int | None = None,
        creator_id: int | None = None,
    ) -> dict:
        """Archives autopilot report rows without deleting task history."""
        from urllib.parse import urlencode
        payload = {}
        if statuses is not None:
            payload["statuses"] = statuses
        if older_than_days is not None:
            payload["older_than_days"] = int(older_than_days)
        if creator_id is not None:
            payload["creator_id"] = int(creator_id)
        url = f"/api/video-parsing/autopilot-report/cleanup?{urlencode({'user_id': user_id})}"
        return await self._request("POST", url, json=payload)

    async def skip_autopilot_new_videos(self, user_id: int, creator_id: int | None = None) -> dict:
        """Marks current new videos as skipped for all creators or one creator."""
        from urllib.parse import urlencode
        payload = {}
        if creator_id is not None:
            payload["creator_id"] = int(creator_id)
        url = f"/api/video-parsing/autopilot-report/skip-new?{urlencode({'user_id': user_id})}"
        return await self._request("POST", url, json=payload)

    async def cancel_autopilot_work(
        self,
        user_id: int,
        *,
        creator_id: int | None = None,
        include_running: bool = False,
    ) -> dict:
        """Cancels queued autopilot orders/events for all creators or one creator."""
        from urllib.parse import urlencode
        params = {"user_id": user_id}
        payload = {"include_running": bool(include_running)}
        if creator_id is not None:
            payload["creator_id"] = int(creator_id)
        url = f"/api/video-parsing/autopilot-report/cancel?{urlencode(params)}"
        return await self._request("POST", url, json=payload)

    async def get_ap_dashboard(self, user_id: int) -> dict:
        from urllib.parse import urlencode
        return await self._request("GET", f"/api/autopilot/dashboard?{urlencode({'user_id': user_id})}")

    async def get_ap_limits(self, user_id: int) -> dict:
        from urllib.parse import urlencode
        return await self._request("GET", f"/api/autopilot/limits?{urlencode({'user_id': user_id})}")

    async def update_ap_limits(
        self,
        user_id: int,
        *,
        mode: str | None = None,
        max_create_per_tick: int | None = None,
        max_create_per_creator: int | None = None,
        max_create_per_hour: int | None = None,
    ) -> dict:
        from urllib.parse import urlencode
        payload = {}
        if mode is not None:
            payload["mode"] = mode
        if max_create_per_tick is not None:
            payload["max_create_per_tick"] = int(max_create_per_tick)
        if max_create_per_creator is not None:
            payload["max_create_per_creator"] = int(max_create_per_creator)
        if max_create_per_hour is not None:
            payload["max_create_per_hour"] = int(max_create_per_hour)
        return await self._request("POST", f"/api/autopilot/limits?{urlencode({'user_id': user_id})}", json=payload)

    async def get_ap_current(self, user_id: int) -> dict:
        from urllib.parse import urlencode
        return await self._request("GET", f"/api/autopilot/current?{urlencode({'user_id': user_id})}")

    async def get_ap_work(self, user_id: int, *, tab: str = "plan", page: int = 0, limit: int = 10) -> dict:
        from urllib.parse import urlencode
        params = {"user_id": user_id, "tab": tab, "page": page, "limit": limit}
        return await self._request("GET", f"/api/autopilot/work?{urlencode(params)}")

    async def get_ap_planned_video(self, user_id: int, video_id: int) -> dict:
        from urllib.parse import urlencode
        return await self._request("GET", f"/api/autopilot/work/planned-video/{int(video_id)}?{urlencode({'user_id': user_id})}")

    async def get_ap_planned_task(self, user_id: int, task_id: int) -> dict:
        from urllib.parse import urlencode
        return await self._request("GET", f"/api/autopilot/work/planned-task/{int(task_id)}?{urlencode({'user_id': user_id})}")

    async def get_ap_summary(self, user_id: int, period: str = "24h") -> dict:
        from urllib.parse import urlencode
        return await self._request("GET", f"/api/autopilot/report/summary?{urlencode({'user_id': user_id, 'period': period})}")

    async def get_ap_tasks(
        self,
        user_id: int,
        *,
        period: str = "24h",
        status: str = "all",
        page: int = 0,
        limit: int = 5,
        creator_id: int | None = None,
    ) -> dict:
        from urllib.parse import urlencode
        params = {"user_id": user_id, "period": period, "status": status, "page": page, "limit": limit}
        if creator_id is not None:
            params["creator_id"] = int(creator_id)
        return await self._request("GET", f"/api/autopilot/report/tasks?{urlencode(params)}")

    async def get_ap_task(self, user_id: int, task_id: int) -> dict:
        from urllib.parse import urlencode
        return await self._request("GET", f"/api/autopilot/report/task/{int(task_id)}?{urlencode({'user_id': user_id})}")

    async def get_ap_creators(self, user_id: int, *, period: str = "24h", page: int = 0, limit: int = 5) -> dict:
        from urllib.parse import urlencode
        params = {"user_id": user_id, "period": period, "page": page, "limit": limit}
        return await self._request("GET", f"/api/autopilot/report/creators?{urlencode(params)}")

    async def get_ap_finance(self, user_id: int, period: str = "24h") -> dict:
        from urllib.parse import urlencode
        return await self._request("GET", f"/api/autopilot/report/finance?{urlencode({'user_id': user_id, 'period': period})}")

    async def ap_cancel_all_preview(self, user_id: int) -> dict:
        from urllib.parse import urlencode
        return await self._request("GET", f"/api/autopilot/actions/cancel-all/preview?{urlencode({'user_id': user_id})}")

    async def ap_skip_queue_preview(self, user_id: int) -> dict:
        from urllib.parse import urlencode
        return await self._request("GET", f"/api/autopilot/actions/skip-queue/preview?{urlencode({'user_id': user_id})}")

    async def ap_archive_preview(self, user_id: int, period: str = "24h") -> dict:
        from urllib.parse import urlencode
        return await self._request("GET", f"/api/autopilot/actions/archive/preview?{urlencode({'user_id': user_id, 'period': period})}")

    async def ap_retry_preview(self, user_id: int, task_id: int) -> dict:
        from urllib.parse import urlencode
        return await self._request(
            "GET",
            f"/api/autopilot/actions/retry/preview?{urlencode({'user_id': user_id, 'task_id': int(task_id)})}",
        )

    async def ap_action(
        self,
        user_id: int,
        action: str,
        *,
        task_id: int | None = None,
        video_id: int | None = None,
        period: str = "24h",
        include_running: bool = False,
    ) -> dict:
        from urllib.parse import urlencode
        payload = {"period": period, "include_running": bool(include_running)}
        if task_id is not None:
            payload["task_id"] = int(task_id)
        if video_id is not None:
            payload["video_id"] = int(video_id)
        return await self._request(
            "POST",
            f"/api/autopilot/actions/{action}?{urlencode({'user_id': user_id})}",
            json=payload,
            retry_safe=False,
        )
    
    async def link_videos_to_tasks(self, user_id: int, video_task_pairs: list[tuple[int, int]]) -> dict:
        """Ð¡Ð²ÑÐ·Ñ‹Ð²Ð°ÐµÑ‚ Ð²Ð¸Ð´ÐµÐ¾ Ñ Ð·Ð°Ð´Ð°Ñ‡Ð°Ð¼Ð¸, Ð¾Ð±Ð½Ð¾Ð²Ð»ÑÑ status='queued' Ð¸ task_id."""
        from urllib.parse import urlencode
        links = [{"video_id": video_id, "task_id": task_id} for video_id, task_id in video_task_pairs]
        params = {"user_id": user_id}
        url = f"/api/video-parsing/videos/link-tasks?{urlencode(params)}"
        payload = {"links": links}
        return await self._request("POST", url, json=payload)
    
    # Ð£Ð›Ð£Ð§Ð¨Ð•ÐÐž: ÐœÐµÑ‚Ð¾Ð´Ñ‹ Ð´Ð»Ñ Ñ€Ð°Ð±Ð¾Ñ‚Ñ‹ Ñ Inbox
    async def get_inbox_videos(self, user_id: int, status: str | None = None, limit: int = 20, offset: int = 0) -> dict:
        """ÐŸÐ¾Ð»ÑƒÑ‡Ð°ÐµÑ‚ Ð²Ð¸Ð´ÐµÐ¾ Ð´Ð»Ñ Inbox."""
        from urllib.parse import urlencode

        page_limit = 50

        async def _fetch_page(page_size: int, page_offset: int) -> dict:
            params = {"user_id": user_id, "limit": page_size, "offset": page_offset}
            if status:
                params["status"] = status
            url = f"/api/video-inbox/videos?{urlencode(params)}"
            return await self._request("GET", url)

        safe_limit = max(0, int(limit or 0))
        safe_offset = max(0, int(offset or 0))
        if safe_limit <= page_limit:
            return await _fetch_page(safe_limit or 1, safe_offset)

        collected: list[dict] = []
        first_page: dict | None = None
        remaining = safe_limit
        current_offset = safe_offset
        while remaining > 0:
            current_limit = min(page_limit, remaining)
            page = await _fetch_page(current_limit, current_offset)
            if first_page is None:
                first_page = dict(page)
            videos = list(page.get("videos") or [])
            collected.extend(videos)
            if len(videos) < current_limit:
                break
            current_offset += len(videos)
            remaining -= len(videos)

        result = first_page or {"total": 0, "by_status": {}, "videos": []}
        result["videos"] = collected
        return result
    
    async def get_inbox_stats(self, user_id: int) -> dict:
        """ÐŸÐ¾Ð»ÑƒÑ‡Ð°ÐµÑ‚ ÑÑ‚Ð°Ñ‚Ð¸ÑÑ‚Ð¸ÐºÑƒ Inbox."""
        from urllib.parse import urlencode
        params = {"user_id": user_id}
        url = f"/api/video-inbox/stats?{urlencode(params)}"
        return await self._request("GET", url)
    
    async def skip_video(self, video_id: int, user_id: int) -> dict:
        """ÐŸÑ€Ð¾Ð¿ÑƒÑÐºÐ°ÐµÑ‚ Ð²Ð¸Ð´ÐµÐ¾."""
        from urllib.parse import urlencode
        params = {"user_id": user_id}
        url = f"/api/video-inbox/videos/{video_id}/skip?{urlencode(params)}"
        return await self._request("POST", url)
    
    async def skip_videos_batch(self, video_ids: list[int], user_id: int) -> dict:
        """ÐŸÑ€Ð¾Ð¿ÑƒÑÐºÐ°ÐµÑ‚ Ð½ÐµÑÐºÐ¾Ð»ÑŒÐºÐ¾ Ð²Ð¸Ð´ÐµÐ¾ Ð¿Ð°Ñ‡ÐºÐ¾Ð¹."""
        from urllib.parse import urlencode
        params = {"user_id": user_id}
        url = f"/api/video-inbox/videos/batch/skip?{urlencode(params)}"
        return await self._request("POST", url, json={"video_ids": video_ids})

    async def skip_all_new_inbox_videos(self, user_id: int) -> dict:
        """Пропускает все новые видео во входящих без клиентского лимита страницы."""
        from urllib.parse import urlencode
        params = {"user_id": user_id}
        url = f"/api/video-inbox/videos/batch/skip-new?{urlencode(params)}"
        return await self._request("POST", url)

    async def mark_video_commented(self, video_id: int, user_id: int) -> dict:
        """Помечает видео как откомментированное вручную (status=done)."""
        from urllib.parse import urlencode
        params = {"user_id": user_id}
        url = f"/api/video-inbox/videos/{video_id}/mark-commented?{urlencode(params)}"
        return await self._request("POST", url)

    async def mark_videos_commented_batch(self, video_ids: list[int], user_id: int) -> dict:
        """Помечает несколько видео как откомментированные вручную пачкой."""
        from urllib.parse import urlencode
        params = {"user_id": user_id}
        url = f"/api/video-inbox/videos/batch/mark-commented?{urlencode(params)}"
        return await self._request("POST", url, json={"video_ids": video_ids})

    async def unskip_video(self, video_id: int, user_id: int) -> dict:
        """Возвращает пропущенное видео обратно в статус ``new``.

        Бэкенд отвечает 409, если видео не в скип-статусе — caller обязан это
        отобразить пользователю (UI меняет состояние оптимистично только при ok).
        """
        from urllib.parse import urlencode
        params = {"user_id": user_id}
        url = f"/api/video-inbox/videos/{video_id}/unskip?{urlencode(params)}"
        return await self._request("POST", url)

    async def reset_video_to_new(self, video_id: int, user_id: int) -> dict:
        """Сбрасывает упавшее видео в статус ``new`` (для повторного запуска).

        Бэкенд отвечает 409, если видео не в failed-статусе.
        """
        from urllib.parse import urlencode
        params = {"user_id": user_id}
        url = f"/api/video-inbox/videos/{video_id}/reset-to-new?{urlencode(params)}"
        return await self._request("POST", url)

    # ── Adaptive monitoring (admin only) ──────────────────────────────────────

    async def get_adaptive_settings(self) -> dict:
        """Возвращает параметры адаптивного алгоритма."""
        return await self._request("GET", "/api/adaptive/settings")

    async def update_adaptive_settings(self, **kwargs) -> dict:
        """Частичное обновление параметров адаптивного алгоритма."""
        return await self._request("POST", "/api/adaptive/settings", json=kwargs)

    async def list_adaptive_bloggers(self, limit: int = 50, offset: int = 0) -> list:
        """Список блогеров с адаптивной статистикой."""
        from urllib.parse import urlencode
        url = f"/api/adaptive/bloggers?{urlencode({'limit': limit, 'offset': offset})}"
        return await self._request("GET", url)

    async def get_adaptive_blogger(self, blogger_id: int) -> dict:
        """Карточка одного блогера с адаптивной статистикой."""
        return await self._request("GET", f"/api/adaptive/bloggers/{blogger_id}")

    async def set_blogger_priority(self, blogger_id: int, priority_mode: str) -> dict:
        """Устанавливает priority_mode блогера."""
        return await self._request(
            "PATCH",
            f"/api/adaptive/bloggers/{blogger_id}/priority",
            json={"priority_mode": priority_mode},
        )

    async def force_blogger_recheck(self, blogger_id: int) -> dict:
        """Ставит next_check_at = now, чтобы воркер взял блогера на следующем тике."""
        return await self._request("POST", f"/api/adaptive/bloggers/{blogger_id}/recheck")

    async def get_blogger_check_log(self, blogger_id: int, limit: int = 20) -> list:
        """Последние записи проверок блогера."""
        from urllib.parse import urlencode
        url = f"/api/adaptive/bloggers/{blogger_id}/log?{urlencode({'limit': limit})}"
        return await self._request("GET", url)

    async def get_blogger_video_analytics(self, blogger_id: int, limit: int = 25) -> dict:
        """Per-video adaptive analytics for a monitored blogger."""
        from urllib.parse import urlencode
        url = f"/api/adaptive/bloggers/{blogger_id}/videos?{urlencode({'limit': limit})}"
        return await self._request("GET", url)

    async def get_adaptive_summary(self) -> dict:
        """Сводная статистика мониторинга: счётчики, прогнозы, ближайшие проверки."""
        return await self._request("GET", "/api/adaptive/summary")

    async def get_adaptive_learning_report(self) -> dict:
        """Отчёт самообучения: прогресс калибровки, эффективность интервалов, задержка обнаружения."""
        return await self._request("GET", "/api/adaptive/learning-report")

    async def run_adaptive_backfill(
        self,
        days: int = 90,
        force: bool = False,
        reset_feedback: bool = False,
    ) -> dict:
        """Запускает backfill posting_stats из исторических данных.

        reset_feedback=True дополнительно сбрасывает latency_ema у блогеров — нужно
        при принудительном переобучении ритма, чтобы петля обратной связи не тянула
        интервалы под устаревшие наблюдения задержки.
        """
        from urllib.parse import urlencode
        params = {
            "days": days,
            "force": str(force).lower(),
            "reset_feedback": str(reset_feedback).lower(),
        }
        url = f"/api/adaptive/backfill?{urlencode(params)}"
        return await self._request("POST", url)

    async def delete_blogger(self, blogger_id: int) -> dict:
        """Окончательно удаляет блогера из адаптивного мониторинга для всех пользователей."""
        return await self._request("DELETE", f"/api/adaptive/bloggers/{blogger_id}")

    async def rename_blogger(self, blogger_id: int, username: str) -> dict:
        """Меняет username блогера и связанных подписок пользователей."""
        return await self._request(
            "PATCH",
            f"/api/adaptive/bloggers/{blogger_id}/username",
            json={"username": username},
        )

    async def set_blogger_overrides(
        self,
        blogger_id: int,
        override_min_interval: int | None | object = _UNSET,
        override_max_interval: int | None | object = _UNSET,
        notes: str | None | object = _UNSET,
    ) -> dict:
        """Устанавливает ручные ограничения интервала или заметку для блогера."""
        payload: dict = {}
        if override_min_interval is not _UNSET:
            payload["override_min_interval"] = (
                override_min_interval
                if override_min_interval is not None and override_min_interval > 0
                else None
            )
        if override_max_interval is not _UNSET:
            payload["override_max_interval"] = (
                override_max_interval
                if override_max_interval is not None and override_max_interval > 0
                else None
            )
        if notes is not _UNSET:
            payload["notes"] = notes
        return await self._request(
            "PATCH",
            f"/api/adaptive/bloggers/{blogger_id}/overrides",
            json=payload,
        )

    # ============================================================
    # Personal accounts (личный пул работника)
    # Все методы автоматически подписываются user-scope сигнатурой
    # через _ensure_user_scope_headers (срабатывает при наличии user_id
    # в query или body).
    # ============================================================

    async def list_personal_accounts(
        self,
        user_id: int,
        page: int = 0,
        page_size: int = 20,
    ) -> dict:
        return await self._request(
            "GET",
            f"/api/personal-accounts/list?user_id={int(user_id)}&page={int(page)}&page_size={int(page_size)}",
        )

    async def get_personal_pool_summary(self, user_id: int) -> dict:
        return await self._request(
            "GET",
            f"/api/personal-accounts/summary?user_id={int(user_id)}",
        )

    async def import_personal_accounts(
        self,
        user_id: int,
        accounts: list[dict],
        daily_main_limit: int | None = None,
        daily_reply_limit: int | None = None,
    ) -> dict:
        payload: dict = {
            "user_id": int(user_id),
            "accounts": accounts,
        }
        if daily_main_limit is not None:
            payload["daily_main_limit"] = int(daily_main_limit)
        if daily_reply_limit is not None:
            payload["daily_reply_limit"] = int(daily_reply_limit)
        return await self._request(
            "POST",
            "/api/personal-accounts/import",
            json=payload,
        )

    async def delete_personal_account(self, user_id: int, account_id: int) -> dict:
        return await self._request(
            "DELETE",
            f"/api/personal-accounts/{int(account_id)}?user_id={int(user_id)}",
        )

    async def clear_personal_pool(self, user_id: int) -> dict:
        return await self._request(
            "DELETE",
            f"/api/personal-accounts/all/clear?user_id={int(user_id)}",
        )

    async def patch_personal_account_limits(
        self,
        user_id: int,
        account_id: int,
        daily_main_limit: int | None = None,
        daily_reply_limit: int | None = None,
    ) -> dict:
        payload: dict = {"user_id": int(user_id)}
        if daily_main_limit is not None:
            payload["daily_main_limit"] = int(daily_main_limit)
        if daily_reply_limit is not None:
            payload["daily_reply_limit"] = int(daily_reply_limit)
        return await self._request(
            "PATCH",
            f"/api/personal-accounts/{int(account_id)}/limits",
            json=payload,
        )

    async def patch_personal_pool_limits_bulk(
        self,
        user_id: int,
        daily_main_limit: int | None = None,
        daily_reply_limit: int | None = None,
        apply_to_existing: bool = True,
    ) -> dict:
        payload: dict = {
            "user_id": int(user_id),
            "apply_to_existing": bool(apply_to_existing),
        }
        if daily_main_limit is not None:
            payload["daily_main_limit"] = int(daily_main_limit)
        if daily_reply_limit is not None:
            payload["daily_reply_limit"] = int(daily_reply_limit)
        return await self._request(
            "PATCH",
            "/api/personal-accounts/limits/bulk",
            json=payload,
        )


api_client = APIClient()
