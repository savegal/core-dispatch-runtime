"""
Engagement Engine - единый движок для HYBRID и ENGAGEMENT_ONLY комментирования.

Реализует:
- HYBRID FULL: главный комментарий через HTTP (DOM только fallback), ответы через HTTP, лайки — через API лайков (настроенное количество)
- ENGAGEMENT ONLY: ответы через HTTP, лайки — через API лайков (настроенное количество)

КРИТИЧЕСКИЕ ПРАВИЛА:
- НИ ОДНО действие не считается успешным, если HTTP ответ не подтвердил успех
- НИ ОДНО действие не считается успешным, если DOM verify (если включен) не подтвердил видимость
- Запрещено логировать "успешно", если success=False
- Запрещено увеличивать счетчики, если действие реально не выполнено
"""
import logging
import asyncio
import re
import random
import time
from typing import Optional, Dict, Any, List, Tuple, Callable
from urllib.parse import urlparse
import httpx
from sqlalchemy.orm import Session
from db.models import Task, Account
from executor.tiktok_client_async import create_page_for_account
from patchright.async_api import Page
from executor.comment_verification import verify_with_retries
from executor.publish_action import publish_comment_hybrid, publish_reply_hybrid
from executor.captcha_detector import detect_captcha
from executor.dom_actions import dismiss_cookie_banners
from executor.session_bootstrap import LOGIN_AUTH_COOKIES, bootstrap_session, get_browser_login_state
from executor.cookie_enricher import (
    needs_enrichment,
    enrich_cookies_via_browser,
    save_enriched_cookies_to_db,
    assess_cookie_strength,
)
from executor.http.retry_policy import HttpErrorType
from executor.http.playwright_http_client import PlaywrightHttpClient
from executor.http.signature_runtime import wait_for_runtime_signer_ready
from services.account_service import (
    MAIN_SCOPE,
    REPLY_SCOPE,
    get_free_account_for_main_comment,
    get_free_account_for_reply,
    get_http_execution_strategy,
    mark_needs_reauth,
    mark_soft_error,
    record_main_comment_success,
    record_reply_success,
    release_main_account_reservation,
)
from services.proxy_health_service import clear_proxy_bad, mark_proxy_bad
from services.log_service import log
from config.settings import get_settings
from services.app_settings_service import get_setting
from services.likes_config import (
    BOOST_LIKES_DEFAULT,
    BOOST_LIKES_ENABLED_KEY,
    BOOST_LIKES_QUANTITY_KEY,
    BOOST_LIKES_TARGET_SINGLE_TOP,
    coerce_bool,
    coerce_likes_quantity,
    provider_available as likes_provider_available,
)
from services.metrics import increment_counter, record_timing

logger = logging.getLogger("executor.engagement_engine")
settings = get_settings()
_TASK_LOG_COMPAT = log  # Keep module-level log attribute for tests/extensions that monkeypatch it.
FAST_VERIFY_TIMEOUT_SECONDS = 18
FAST_VERIFY_INTERVAL_SECONDS = 4
FAST_VERIFY_MAX_SCROLLS = 4
FAST_VERIFY_SCROLL_DELAY_MS = 350
_SHORTLINK_HOST_MARKERS = ("vt.tiktok.com", "vm.tiktok.com")
ENGAGEMENT_ONLY_REPLY_MAX_ATTEMPTS = 6


async def _close_attempt_resources(page, context, *, reason: str) -> None:
    if page is not None and getattr(page, "_managed_by_pool", False):
        try:
            from executor.browser_pool import close_pool_page

            if await close_pool_page(page, reason=reason):
                return
        except Exception:
            logger.debug("Failed to close pooled engagement page via pool", exc_info=True)
        try:
            if not page.is_closed():
                await page.close()
        except Exception:
            pass
        return

    try:
        if page and not page.is_closed():
            await page.close()
    except Exception:
        pass
    try:
        if context:
            await context.close()
    except Exception:
        pass


def _bounded_float_setting(
    name: str,
    default: float,
    *,
    minimum: float = 0.0,
    maximum: Optional[float] = None,
) -> float:
    try:
        value = float(getattr(settings, name, default))
    except Exception:
        value = float(default)
    value = max(float(minimum), value)
    if maximum is not None:
        value = min(float(maximum), value)
    return value


def _main_publish_max_seconds() -> float:
    return _bounded_float_setting(
        "PARALLEL_MAIN_PUBLISH_MAX_SECONDS",
        40.0,
        minimum=15.0,
        maximum=90.0,
    )


def _reply_publish_max_seconds() -> float:
    return _bounded_float_setting(
        "PARALLEL_REPLY_PUBLISH_MAX_SECONDS",
        55.0,
        minimum=20.0,
        maximum=120.0,
    )


def _reply_variants_from_task(task: Task) -> List[str]:
    task_settings = task.settings or {}
    settings_replies = task_settings.get("replies")
    if isinstance(settings_replies, list):
        raw_replies = settings_replies
    else:
        raw_replies = [
            task.reply1,
            task.reply2,
            task.reply3,
            *[
                task_settings.get(f"reply{idx}")
                for idx in range(4, int(settings.MAX_REPLIES) + 1)
            ],
        ]

    replies = [
        str(reply).strip()
        for reply in raw_replies
        if str(reply or "").strip()
    ][: int(settings.MAX_REPLIES)]
    reply_count = task_settings.get("reply_count", 3)
    if isinstance(reply_count, int) and reply_count >= 0:
        replies = replies[:reply_count]
    return replies

HTTP_SKIP_AFTER_FATAL_FAILS = 3
HTTP_SKIP_AFTER_WARMUP_FAILS = 5
HTTP_SKIP_AFTER_HTTP_FAILS = 5
HTTP_SIGNER_COOLDOWN_AFTER_WARMUP_FAILS = 3
HTTP_SIGNER_COOLDOWN_SECONDS = 180.0
UI_NO_PROGRESS_COOLDOWN_AFTER = 3
UI_NO_PROGRESS_UNKNOWN_COOLDOWN_AFTER = 1
AMBIGUOUS_POST_SUBMIT_ERRORS = {
    "stealth_reject_empty_body",
    "non_json_response",
    "no_network_request_after_post_click",
    # Synthetic dispatch on a disabled Post button — click may or may not have
    # actually triggered React's handler. Treat as ambiguous so callers do not
    # accept a bare local DOM match as confirmation.
    "submitted_unverified_disabled_post",
}

from collections import defaultdict as _defaultdict

def _new_http_health_stats() -> Dict[str, int]:
    return {
        "success": 0,
        "warmup_fail": 0,
        "soft_fail": 0,
        "http_fail": 0,
        "fatal": 0,
        "ui_no_progress": 0,
    }

_HTTP_ACCOUNT_HEALTH: Dict[int, Dict[str, int]] = _defaultdict(_new_http_health_stats)
_HTTP_RUNTIME_SIGNER_HEALTH: Dict[str, float] = {
    "warmup_fail": 0.0,
    "cooldown_until": 0.0,
}


def _is_ambiguous_post_submit_result(result: Any) -> bool:
    """Return True when a click may have posted but confirmation was ambiguous."""
    status = str(getattr(result, "status", "") or "")
    reason = str(getattr(result, "error_reason", "") or "")
    if status == "pending_public_check":
        return True
    if status == "failed" and reason in AMBIGUOUS_POST_SUBMIT_ERRORS:
        return True
    if status == "failed" and reason == "api_error_status_code=0":
        return True
    if status == "timeout" and reason == "no_network_request_after_post_click":
        return True
    return False


def _normalize_dom_recovery_result(value: Any) -> Optional[Dict[str, Any]]:
    if not value:
        return None
    if isinstance(value, dict):
        if value.get("found") or value.get("comment_id"):
            return {
                "found": bool(value.get("found") or value.get("comment_id")),
                "comment_id": str(value.get("comment_id")) if value.get("comment_id") else None,
                "selector": value.get("selector"),
                "position": value.get("position"),
                "raw_text": value.get("raw_text"),
                "text_normalized": value.get("text_normalized"),
                "reason": value.get("reason"),
            }
        return None
    return {
        "found": True,
        "comment_id": str(value),
        "selector": None,
        "position": None,
        "raw_text": None,
        "text_normalized": None,
        "reason": None,
    }


async def _recover_main_comment_after_ambiguous_publish(
    page: Page,
    text: str,
    task_id: int,
    account_id: int,
    reason: str,
) -> Optional[Dict[str, Any]]:
    """Best-effort local check before treating an ambiguous DOM post as failed."""
    if not text or not str(text).strip():
        return None
    try:
        from executor.tiktok_client_async import _recover_comment_from_dom

        recovery = _normalize_dom_recovery_result(await _recover_comment_from_dom(
            page=page,
            expected_text=text,
            phase="MAIN_COMMENT_AMBIGUOUS_DOM_PUBLISH",
            max_scrolls=3,
            scroll_delay_ms=300,
        ))
        if recovery and recovery.get("comment_id"):
            logger.warning(
                "Task %s: Account %s ambiguous publish result (%s), but local DOM "
                "shows the comment. Recovered comment_id=%s; treating as posted.",
                task_id,
                account_id,
                reason,
                str(recovery.get("comment_id"))[:20],
            )
            return recovery
        if recovery and recovery.get("found"):
            logger.warning(
                "Task %s: Account %s ambiguous publish result (%s), but local DOM "
                "shows the comment without a stable comment_id; treating as locally posted.",
                task_id,
                account_id,
                reason,
            )
            return recovery
        logger.info(
            "Task %s: Account %s ambiguous publish result (%s); local DOM recovery "
            "did not find the comment.",
            task_id,
            account_id,
            reason,
        )
    except Exception:
        logger.debug(
            "Task %s: Account %s local recovery after ambiguous publish failed",
            task_id,
            account_id,
            exc_info=True,
        )
    return None


async def _recover_reply_comment_after_ambiguous_publish(
    page: Page,
    text: str,
    task_id: int,
    account_id: int,
    reason: str,
) -> Optional[Dict[str, Any]]:
    """Best-effort reply recovery after a DOM submit with ambiguous network confirmation."""
    if not text or not str(text).strip():
        return None
    try:
        from executor.tiktok_client_async import _recover_comment_from_dom

        recovery = _normalize_dom_recovery_result(await _recover_comment_from_dom(
            page=page,
            expected_text=text,
            phase="REPLY_AMBIGUOUS_DOM_PUBLISH",
            max_scrolls=2,
            scroll_delay_ms=300,
        ))
        if recovery and recovery.get("comment_id"):
            logger.warning(
                "Task %s: Account %s ambiguous reply result (%s), but local DOM "
                "shows the reply. Recovered reply_id=%s; treating as posted.",
                task_id,
                account_id,
                reason,
                str(recovery.get("comment_id"))[:20],
            )
            return recovery
        if recovery and recovery.get("found"):
            logger.info(
                "Task %s: Account %s ambiguous reply result (%s) was visible locally "
                "but no stable reply_id was exposed; keeping reply result unconfirmed.",
                task_id,
                account_id,
                reason,
            )
            return recovery
        logger.info(
            "Task %s: Account %s ambiguous reply result (%s); local DOM recovery "
            "did not find the reply.",
            task_id,
            account_id,
            reason,
        )
    except Exception:
        logger.debug(
            "Task %s: Account %s local reply recovery after ambiguous publish failed",
            task_id,
            account_id,
            exc_info=True,
        )
    return None


def extract_video_id(video_url: str) -> Optional[str]:
    """
    Извлекает video_id (aweme_id) из URL TikTok.
    
    Args:
        video_url: URL видео TikTok
        
    Returns:
        str: aweme_id или None если не удалось извлечь
    """
    # Паттерны для различных форматов TikTok URL
    patterns = [
        r'/video/(\d+)',  # Стандартный формат: /video/1234567890
        r'video_id=(\d+)',  # Query параметр: ?video_id=1234567890
        r'aweme_id=(\d+)',  # API формат: ?aweme_id=1234567890
        r'@[\w.]+/video/(\d+)',  # Формат с username: @user/video/1234567890
        r'item_id=(\d+)',  # Share link: ?item_id=1234567890
        r'\b(\d{16,20})\b',  # Fallback: длинный числовой ID в URL
    ]
    
    for pattern in patterns:
        match = re.search(pattern, video_url)
        if match:
            return match.group(1)
    
    if _is_tiktok_shortlink(video_url):
        logger.debug(
            "TikTok shortlink does not expose video_id before browser resolution: %s",
            video_url[:100],
        )
    else:
        logger.warning(f"Could not extract video_id from URL: {video_url[:100]}")
    return None


def _is_tiktok_shortlink(url: Optional[str]) -> bool:
    if not url:
        return False
    normalized = str(url).lower()
    return any(marker in normalized for marker in _SHORTLINK_HOST_MARKERS)


async def _ensure_comment_panel_open(page: Page) -> None:
    selectors_to_try = (
        'button[data-e2e="comments"]',
        '[data-e2e="comments"]',
        'button:has([data-e2e="comment-icon"])',
        '[data-e2e="comment-icon"]',
        '[data-e2e="comment-count"]',
    )
    for selector in selectors_to_try:
        try:
            handle = await page.query_selector(selector)
            if not handle:
                continue
            try:
                await handle.click(timeout=1200)
            except Exception:
                await handle.evaluate("el => el.click()")
            await page.wait_for_timeout(700)
            return
        except Exception:
            continue


async def resolve_video_url(
    url: str,
    timeout: float = 10.0,
    *,
    proxy: Optional[str] = None,
) -> tuple[str, Optional[str]]:
    """Resolves TikTok shortlinks into canonical URLs.

    IP-anonymity:
      - Если передан `proxy` — запрос идёт через него (тогда TikTok видит IP
        прокси, а не сервера-воркера).
      - Если прокси нет, мы НЕ резолвим shortlink через httpx, потому что это
        утечёт реальный IP сервера. Возвращаем (url, None) — далее
        page.goto(shortlink) сам разрешит редирект через прокси аккаунта.
    """
    existing_id = extract_video_id(url)
    if existing_id:
        return url, existing_id

    if not _is_tiktok_shortlink(url):
        return url, None

    if not proxy:
        # Нет прокси — НЕ резолвим shortlink сами, чтобы не отдать TikTok IP
        # сервера. page.goto в аккаунт-контексте разрешит шортлинк через
        # прокси этого аккаунта.
        logger.info(
            "[URL_RESOLVE] Skipping HTTP resolve for %s without proxy "
            "(avoiding server-IP exposure). page.goto will follow redirect.",
            url[:80],
        )
        return url, None

    headers = {
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/143.0.0.0 Safari/537.36"
        )
    }

    # httpx понимает proxy как dict {"http://": "...", "https://": "..."} или
    # как одну строку. Принимаем привычный формат "scheme://host:port" /
    # "host:port" + auth — преобразуем в полный URL.
    proxy_url = proxy.strip()
    if proxy_url and "://" not in proxy_url:
        proxy_url = f"http://{proxy_url}"

    try:
        async with httpx.AsyncClient(
            follow_redirects=True,
            timeout=timeout,
            headers=headers,
            proxy=proxy_url,
        ) as client:
            try:
                response = await client.head(url)
            except Exception:
                response = await client.get(url)

            resolved_url = str(response.url)
            resolved_id = extract_video_id(resolved_url)
            if resolved_id:
                logger.info(
                    f"[URL_RESOLVE] {url[:80]} -> video_id={resolved_id} via {resolved_url[:120]}"
                )
                return resolved_url, resolved_id

            response = await client.get(url)
            resolved_url = str(response.url)
            resolved_id = extract_video_id(resolved_url)
            if resolved_id:
                logger.info(
                    f"[URL_RESOLVE] {url[:80]} -> video_id={resolved_id} via GET {resolved_url[:120]}"
                )
                return resolved_url, resolved_id
            return resolved_url, None
    except Exception as exc:
        logger.warning(f"[URL_RESOLVE] Failed to resolve {url[:80]} via proxy: {exc}")
        return url, None


def log_tokens_safely(cookies_dict: Dict[str, str], correlation_id: Optional[str] = None) -> Dict[str, str]:
    """
    ИСПРАВЛЕНО: Безопасно логирует наличие критичных токенов (без утечки секретов).
    
    Проверяет наличие msToken, verifyFp, ttwid, tt_csrf_token.
    Логирует только факт наличия + последние 4 символа.
    
    Args:
        cookies_dict: Словарь cookies
        correlation_id: ID для логирования
        
    Returns:
        dict: Информация о токенах для логирования
    """
    tokens_info = {}
    # ИСПРАВЛЕНО: X-Bogus и X-Gnarly почти никогда не лежат в cookies, это обычно query-параметры или результат JS-подписи
    # Убираем их из логирования cookies, чтобы не было ложных "missing" сообщений
    critical_tokens = ["msToken", "verifyFp", "ttwid", "tt_csrf_token"]
    
    for token_name in critical_tokens:
        value = cookies_dict.get(token_name) or cookies_dict.get(token_name.lower()) or cookies_dict.get(token_name.replace("-", "_"))
        if value:
            # Маскируем: показываем только последние 4 символа
            masked = f"...{value[-4:]}" if len(value) > 4 else "***"
            tokens_info[token_name] = f"present:{masked}"
        else:
            tokens_info[token_name] = "missing"
    
    if correlation_id:
        logger.info(
            f"[TOKENS] {correlation_id} | "
            f"msToken={tokens_info.get('msToken', 'missing')}, "
            f"verifyFp={tokens_info.get('verifyFp', 'missing')}, "
            f"ttwid={tokens_info.get('ttwid', 'missing')}, "
            f"tt_csrf_token={tokens_info.get('tt_csrf_token', 'missing')}"
        )
    
    return tokens_info


def validate_request_profile(cookies_dict: Dict[str, str]) -> Tuple[bool, List[str], List[str]]:
    """
    ИСПРАВЛЕНО: Хард-валидация профиля запроса - проверяет готовность к HTTP запросу.
    
    Проверяет наличие критичных параметров:
    - msToken (может быть в cookies или query)
    - verifyFp (обязателен)
    - device_id (обязателен)
    - odinId (желателен)
    
    Args:
        cookies_dict: Словарь cookies из browser context
        
    Returns:
        tuple: (is_valid, missing_params) - валиден ли профиль и какие параметры отсутствуют
    """
    missing = []
    
    # Критичные параметры (должны быть)
    critical_params = {
        "sessionid": ["sessionid", "sid_tt", "sid_guard"],
    }
    
    # Желательные параметры (желательно иметь)
    optional_params = {
        "msToken": ["msToken", "ms_token"],
        "verifyFp": ["verifyFp", "s_v_web_id", "verifyFP"],
        "device_id": ["device_id", "device-id"],
        "ttwid": ["ttwid"],
        "odinId": ["odinId", "odin_tt", "odin-tt"],
        "tt_csrf_token": ["tt_csrf_token", "csrftoken", "csrf_token"],
    }
    
    # Проверяем критичные
    for param_name, cookie_keys in critical_params.items():
        found = False
        for key in cookie_keys:
            if cookies_dict.get(key) or cookies_dict.get(key.lower()):
                found = True
                break
        if not found:
            missing.append(param_name)
    
    # Проверяем желательные (предупреждаем если нет)
    missing_optional = []
    for param_name, cookie_keys in optional_params.items():
        found = False
        for key in cookie_keys:
            if cookies_dict.get(key) or cookies_dict.get(key.lower()):
                found = True
                break
        if not found:
            missing_optional.append(param_name)
    
    is_valid = len(missing) == 0
    
    if missing_optional:
        logger.debug(f"[VALIDATE] Missing optional params: {missing_optional}")
    
    return is_valid, missing, missing_optional


def build_canonical_video_url(video_url: str, aweme_id: Optional[str] = None) -> str:
    """
    ИСПРАВЛЕНО: Строит канонический URL для видео.
    
    Извлекает aweme_id и строит URL вида:
    https://www.tiktok.com/video/{aweme_id}
    
    Если aweme_id не передан, пытается извлечь из video_url.
    
    Args:
        video_url: Исходный URL видео (может быть коротким, с редиректами и т.д.)
        aweme_id: Опциональный aweme_id (если уже известен)
        
    Returns:
        str: Канонический URL
    """
    if not aweme_id:
        aweme_id = extract_video_id(video_url)
    
    if aweme_id:
        # Строим канонический URL
        return f"https://www.tiktok.com/video/{aweme_id}"
    else:
        # Если не удалось извлечь - возвращаем как есть
        logger.warning(f"[CANONICAL] Could not extract aweme_id from {video_url[:100]}, using as-is")
        return video_url


def _build_warmup_video_url(video_url: str, aweme_id: Optional[str] = None) -> str:
    if aweme_id and not _is_tiktok_shortlink(video_url):
        try:
            parsed = urlparse(str(video_url))
            host = parsed.netloc.lower()
            if (host == "tiktok.com" or host.endswith(".tiktok.com")) and "/video/" in parsed.path:
                return video_url
        except Exception:
            pass

    return build_canonical_video_url(video_url, aweme_id)


def _log_pipeline_event(
    task_id: Optional[int],
    stage: str,
    outcome: str,
    level: str = "info",
    **fields: Any,
) -> None:
    parts = [
        "[PIPELINE]",
        f"task_id={task_id if task_id is not None else 'None'}",
        f"stage={stage}",
        f"outcome={outcome}",
    ]
    for key, value in fields.items():
        if value is None:
            continue
        value_str = str(value).replace("\n", " ").replace("\r", " ").strip()
        if len(value_str) > 120:
            value_str = value_str[:117] + "..."
        parts.append(f"{key}={value_str}")
    try:
        metric_tags = {"stage": stage, "outcome": outcome}
        for metric_key in ("reason", "method", "verify_mode", "error_type"):
            metric_value = fields.get(metric_key)
            if metric_value is not None:
                metric_tags[metric_key] = str(metric_value)[:48]
        increment_counter("tiktok.pipeline", tags=metric_tags)
        latency_ms = fields.get("latency_ms")
        if latency_ms is not None:
            record_timing(
                "tiktok.pipeline_latency_ms",
                float(latency_ms),
                tags={"stage": stage, "outcome": outcome},
            )
    except Exception:
        pass
    getattr(logger, level, logger.info)(" ".join(parts))


def _record_http_account_result(account_id: Optional[int], outcome: str) -> None:
    if outcome == "success":
        _HTTP_RUNTIME_SIGNER_HEALTH["warmup_fail"] = 0.0
        _HTTP_RUNTIME_SIGNER_HEALTH["cooldown_until"] = 0.0
    elif outcome == "warmup_fail":
        failures = float(_HTTP_RUNTIME_SIGNER_HEALTH.get("warmup_fail") or 0.0) + 1.0
        _HTTP_RUNTIME_SIGNER_HEALTH["warmup_fail"] = failures
        if failures >= HTTP_SIGNER_COOLDOWN_AFTER_WARMUP_FAILS:
            _HTTP_RUNTIME_SIGNER_HEALTH["cooldown_until"] = (
                time.monotonic() + HTTP_SIGNER_COOLDOWN_SECONDS
            )
    if account_id is None:
        return
    stats = _HTTP_ACCOUNT_HEALTH[account_id]
    if outcome == "success":
        stats["success"] += 1
        stats["warmup_fail"] = 0
        stats["soft_fail"] = 0
        stats["http_fail"] = 0
        stats["fatal"] = 0
        stats["ui_no_progress"] = 0
    elif outcome == "warmup_fail":
        stats["warmup_fail"] += 1
    elif outcome == "soft_fail":
        stats["soft_fail"] += 1
        stats["http_fail"] += 1
    elif outcome == "fatal":
        stats["fatal"] += 1
        stats["http_fail"] += 1
    else:
        stats["http_fail"] += 1


def _record_ui_no_progress(account_id: Optional[int]) -> int:
    if account_id is None:
        return 0
    stats = _HTTP_ACCOUNT_HEALTH[account_id]
    stats["ui_no_progress"] += 1
    return stats["ui_no_progress"]


def _reset_ui_no_progress(account_id: Optional[int]) -> None:
    if account_id is None:
        return
    stats = _HTTP_ACCOUNT_HEALTH[account_id]
    stats["ui_no_progress"] = 0


def _record_http_account_failure_result(
    account_id: Optional[int],
    error_type: Optional[HttpErrorType],
) -> None:
    if error_type in {HttpErrorType.FORBIDDEN, HttpErrorType.CAPTCHA}:
        _record_http_account_result(account_id, "fatal")
    else:
        _record_http_account_result(account_id, "soft_fail")


def _should_skip_http_for_account(account_id: Optional[int]) -> Optional[str]:
    if account_id is None:
        cooldown_until = float(_HTTP_RUNTIME_SIGNER_HEALTH.get("cooldown_until") or 0.0)
        return "runtime_signer_cooldown" if cooldown_until > time.monotonic() else None
    stats = _HTTP_ACCOUNT_HEALTH.get(account_id)
    if stats:
        if stats["fatal"] >= HTTP_SKIP_AFTER_FATAL_FAILS:
            return "recent_fatal_http_errors"
        if stats["warmup_fail"] >= HTTP_SKIP_AFTER_WARMUP_FAILS:
            return "recent_warmup_failures"
        if stats["http_fail"] >= HTTP_SKIP_AFTER_HTTP_FAILS and stats["success"] == 0:
            return "repeated_http_failures_without_success"
    cooldown_until = float(_HTTP_RUNTIME_SIGNER_HEALTH.get("cooldown_until") or 0.0)
    if cooldown_until > time.monotonic():
        return "runtime_signer_cooldown"
    return None


def _is_degraded_http_profile(warmup_info: Optional[Dict[str, Any]]) -> bool:
    if not warmup_info:
        return False
    if warmup_info.get("signer_ready") is False:
        return True
    if warmup_info.get("status") == "degraded":
        return True
    missing_optional = warmup_info.get("missing_optional") or []
    return "msToken" in missing_optional


def _http_strategy_reason(strategy_info: Dict[str, Any]) -> str:
    parts: List[str] = []
    session_bucket = strategy_info.get("session_bucket")
    state_bucket = strategy_info.get("state_bucket")
    quality_reason = strategy_info.get("quality_reason")
    if session_bucket:
        parts.append(f"session={session_bucket}")
    if state_bucket:
        parts.append(f"state={state_bucket}")
    if quality_reason:
        parts.append(str(quality_reason))
    return ", ".join(parts) if parts else "strategy decision"


def _looks_like_proxy_runtime_issue(error_type: Optional[HttpErrorType], message: Optional[str]) -> bool:
    if error_type in {
        HttpErrorType.NETWORK_ERROR,
        HttpErrorType.TIMEOUT,
    }:
        return True
    text = (message or "").lower()
    non_proxy_dom_markers = (
        "input_not_found",
        "captcha",
        "post_button",
        "typing_failed",
        "stealth_reject",
        "no_network_request_after_post_click",
        "signer_not_ready",
    )
    if any(marker in text for marker in non_proxy_dom_markers):
        return False
    proxy_markers = (
        "proxy",
        "tunnel",
        "net::",
        "econn",
        "socks",
        "connection reset",
        "connection refused",
        "connection timed out",
        "timed out connecting",
    )
    return any(marker in text for marker in proxy_markers)


def _register_runtime_failure(
    db: Session,
    account: Optional[Account],
    reason: str,
    *,
    error_type: Optional[HttpErrorType] = None,
) -> None:
    if not account:
        return
    try:
        if _looks_like_proxy_runtime_issue(error_type, reason) and account.proxy:
            mark_proxy_bad(account.proxy, reason=reason[:180])
        mark_soft_error(db, account.id, reason)
    except Exception:
        logger.debug(
            f"[ACCOUNT_HEALTH] Failed to register runtime failure for account {getattr(account, 'id', None)}",
            exc_info=True,
        )


def _register_runtime_success(
    db: Session,
    account: Optional[Account],
    *,
    scope: str,
) -> None:
    if not account:
        return
    try:
        if scope == "main":
            record_main_comment_success(db, account.id)
        else:
            record_reply_success(db, account.id)
        if account.proxy:
            clear_proxy_bad(account.proxy)
    except Exception:
        logger.debug(
            f"[ACCOUNT_HEALTH] Failed to register runtime success for account {getattr(account, 'id', None)}",
            exc_info=True,
        )


async def warmup_page_for_http(
    page: Page,
    video_url: str,
    correlation_id: Optional[str] = None,
    max_retries: int = 2
) -> Tuple[Dict[str, str], bool, Dict[str, Any]]:
    """
    ?????????? ???????? ????? HTTP-????????? ? ?????????? ????????? ???????,
    ???? ??????? ??? ? ?? ???? ????????? ??? HTTP.
    """
    cookies_dict: Dict[str, str] = {}
    warmup_started = time.monotonic()
    warmup_info: Dict[str, Any] = {
        "status": "unknown",
        "reason_code": "unknown",
        "attempts": 0,
        "current_url": None,
        "missing_critical": [],
        "missing_optional": [],
        "degraded_http": False,
    }

    def _finish(valid: bool, info: Dict[str, Any]) -> Tuple[Dict[str, str], bool, Dict[str, Any]]:
        outcome = "ok" if valid else "fail"
        try:
            increment_counter(
                "tiktok.http.warmup",
                tags={
                    "outcome": outcome,
                    "reason": str(info.get("reason_code") or "unknown")[:48],
                },
            )
            record_timing(
                "tiktok.http.warmup_ms",
                (time.monotonic() - warmup_started) * 1000,
                tags={
                    "outcome": outcome,
                    "reason": str(info.get("reason_code") or "unknown")[:48],
                },
            )
        except Exception:
            pass
        return cookies_dict, valid, info

    try:
        aweme_id = extract_video_id(video_url)
        canonical_url = _build_warmup_video_url(video_url, aweme_id)
        warmup_info["target_url"] = canonical_url

        logger.debug(
            f"[WARMUP] Starting warmup for {correlation_id or 'unknown'} | "
            f"original_url={video_url[:50]}, canonical_url={canonical_url}"
        )

        for attempt in range(max_retries):
            warmup_info["attempts"] = attempt + 1
            await page.goto(canonical_url, wait_until="domcontentloaded", timeout=30000)

            current_url = page.url
            warmup_info["current_url"] = current_url
            resolved_video_id = extract_video_id(current_url)
            if resolved_video_id:
                warmup_info["resolved_video_id"] = resolved_video_id
            if "/video/" not in current_url:
                warmup_info.update(
                    {
                        "status": "redirect",
                        "reason_code": "redirect_not_video",
                        "missing_critical": [],
                        "missing_optional": [],
                    }
                )
                logger.warning(
                    f"[WARMUP] Redirect detected after goto: {current_url} (expected /video/). "
                    f"Attempt {attempt + 1}/{max_retries}"
                )
                if attempt < max_retries - 1:
                    await page.reload(wait_until="domcontentloaded")
                    await asyncio.sleep(random.uniform(0.5, 1.0))
                continue

            await page.wait_for_load_state("domcontentloaded")
            signer_ready = await wait_for_runtime_signer_ready(
                page,
                timeout_ms=1500,
            )
            warmup_info["signer_ready"] = signer_ready
            if not signer_ready:
                logger.debug(
                    f"[WARMUP] Signer not available (stable condition, not retrying) "
                    f"for {correlation_id or 'unknown'}"
                )
            await asyncio.sleep(
                random.uniform(0.15, 0.35) if signer_ready else random.uniform(0.45, 0.75)
            )

            context = page.context
            cookies_dict = {}
            for cookie in await context.cookies():
                cookies_dict[cookie["name"]] = cookie["value"]

            is_valid, missing_critical, missing_optional = validate_request_profile(cookies_dict)
            log_tokens_safely(cookies_dict, f"{correlation_id}_attempt{attempt + 1}" if correlation_id else None)

            if not signer_ready:
                warmup_info.update(
                    {
                        "status": "signer_not_ready",
                        "reason_code": "signer_not_ready",
                        "missing_critical": missing_critical,
                        "missing_optional": missing_optional,
                        "degraded_http": True,
                        "signer_ready": False,
                    }
                )
                if attempt < max_retries - 1:
                    logger.debug(
                        f"[WARMUP] Signer not ready (attempt {attempt + 1}/{max_retries}), will retry"
                    )
                    await page.reload(wait_until="domcontentloaded")
                    await asyncio.sleep(random.uniform(0.5, 1.0))
                    continue

                logger.info(
                    f"[WARMUP] Signer not ready after {max_retries} attempts for "
                    f"{correlation_id or 'unknown'}"
                )
                return _finish(False, dict(warmup_info))

            if is_valid and signer_ready:
                degraded = "msToken" in missing_optional
                warmup_info.update(
                    {
                        "status": "degraded" if degraded else "ok",
                        "reason_code": "missing_msToken" if degraded else "ready",
                        "missing_critical": [],
                        "missing_optional": missing_optional,
                        "degraded_http": degraded,
                        "signer_ready": signer_ready,
                    }
                )
                if degraded:
                    logger.warning(
                        f"[WARMUP] Warmup validated in degraded mode for {correlation_id or 'unknown'}: "
                        f"missing optional params {missing_optional}"
                    )
                else:
                    logger.debug(f"[WARMUP] Warmup completed and validated for {correlation_id or 'unknown'}")
                return _finish(True, dict(warmup_info))

            reason_code = (
                f"missing_{missing_critical[0]}"
                if len(missing_critical) == 1
                else "missing_critical_tokens"
            )
            warmup_info.update(
                {
                    "status": "missing_critical",
                    "reason_code": reason_code,
                    "missing_critical": missing_critical,
                    "missing_optional": missing_optional,
                    "signer_ready": signer_ready,
                }
            )
            logger.warning(
                f"[WARMUP] Missing critical params after attempt {attempt + 1}/{max_retries}: {missing_critical}. "
                f"Retrying warmup..."
            )

            if attempt < max_retries - 1:
                await page.reload(wait_until="domcontentloaded")
                await asyncio.sleep(random.uniform(0.5, 1.0))

        final_reason = warmup_info.get("reason_code") or "unknown"
        if final_reason == "signer_not_ready":
            logger.info(
                f"[WARMUP] Proceeding without signer for {correlation_id or 'unknown'} "
                f"(this is normal for modern TikTok - HTTP will likely stealth-reject)"
            )
        logger.warning(
            f"[WARMUP] Profile not fully valid after {max_retries} attempts for {correlation_id or 'unknown'}. "
            f"Reason={final_reason}, missing={warmup_info.get('missing_critical')}"
        )
        return _finish(False, dict(warmup_info))

    except Exception as e:
        logger.warning(f"[WARMUP] Error during warmup for {correlation_id or 'unknown'}: {e}", exc_info=True)
        try:
            current_url = page.url
            logger.debug(f"[WARMUP] Current URL at error: {current_url}")
            warmup_info["current_url"] = current_url
        except Exception:
            pass
        context = page.context
        cookies_dict = {}
        try:
            for cookie in await context.cookies():
                cookies_dict[cookie["name"]] = cookie["value"]
        except Exception:
            pass
        warmup_info.update(
            {
                "status": "exception",
                "reason_code": "warmup_exception",
                "missing_critical": [],
                "missing_optional": [],
            }
        )
        return _finish(False, dict(warmup_info))


def _coerce_bool(value, default: bool) -> bool:
    return coerce_bool(value, default)


def _coerce_likes_quantity(value, default: int) -> int:
    return coerce_likes_quantity(value, default)


def _get_task_boost_config(db: Session, task: Task) -> tuple[bool, int]:
    provider_default = likes_provider_available(settings.BOOST_PROJECT_API_KEY)
    quantity_default = _coerce_likes_quantity(
        settings.BOOST_PROJECT_LIKES_QUANTITY,
        BOOST_LIKES_DEFAULT,
    )

    enabled_default = _coerce_bool(
        get_setting(db, BOOST_LIKES_ENABLED_KEY, provider_default),
        provider_default,
    )
    quantity_fallback = _coerce_likes_quantity(
        get_setting(db, BOOST_LIKES_QUANTITY_KEY, quantity_default),
        quantity_default,
    )

    task_settings = task.settings if isinstance(task.settings, dict) else {}
    per_task_enabled = _coerce_bool(task_settings.get("boost_likes_enabled"), enabled_default)
    enabled = provider_default and enabled_default and per_task_enabled
    quantity_source = task_settings.get("likes_count")
    if quantity_source is None:
        quantity_source = getattr(task, "likes", None)
    quantity = _coerce_likes_quantity(quantity_source, quantity_fallback)
    return enabled, quantity


class EngagementResult:
    """Результат выполнения engagement операций с учетом конфигурации Boost."""
    
    def __init__(self):
        self.main_comment: Dict[str, Any] = {
            "success": False,
            "posted": False,
            "comment_id": None,
            "verified": False,
            "method": None,
            "error": None,
            "text_choice": None,
            "network_confirmed": False,
            "warmup_reason": None,
            "http_error_type": None,
            "verify_mode": None,
            "fallback_used": False,
            "http_attempts": 0,
            "submitted_unconfirmed": False,
            "local_confirmed": False,
            "confirmation_source": None,
        }
        self.replies: List[Dict[str, Any]] = []
        self.comment_found: bool = False
        self.comment_id: Optional[str] = None
        self.comment_author: Optional[str] = None
        # API лайков: вкл/выкл и количество настраиваются в app settings.
        self.boost_order_id: Optional[int] = None
        self.boost_orders: List[Dict[str, Any]] = []
        self.boost_error: Optional[str] = None
        self.boost_enabled: bool = True
        self.boost_quantity: int = _coerce_likes_quantity(
            settings.BOOST_PROJECT_LIKES_QUANTITY,
            BOOST_LIKES_DEFAULT,
        )
        self.boost_target_mode: str = BOOST_LIKES_TARGET_SINGLE_TOP
        self.warnings: List[Dict[str, str]] = []
    
    def to_dict(
        self,
        source: str = "manual",
        mode: str = "full",
        started_at: Optional[str] = None,
        ended_at: Optional[str] = None
    ) -> Dict[str, Any]:
        """
        Преобразует результат в словарь для сохранения в settings.
        Лайки считаются успешными, если отключены в настройках, провайдер недоступен или заказ создан.
        """
        from datetime import datetime, timezone
        
        successful_replies = sum(1 for r in self.replies if r.get("success", False))
        submitted_unconfirmed_replies = sum(
            1
            for r in self.replies
            if r.get("submitted_unconfirmed", False)
            or r.get("visibility_status") == "submitted_unconfirmed"
        )
        replies_ok = len(self.replies) == 0 or successful_replies == len(self.replies)
        replies_terminal_ok = len(self.replies) == 0 or all(
            r.get("success", False)
            or r.get("submitted_unconfirmed", False)
            or r.get("visibility_status") == "submitted_unconfirmed"
            for r in self.replies
        )
        main_success = (
            self.main_comment.get("success", False)
            and self.main_comment.get("verified", False)
            and self.main_comment.get("posted", False)
        )
        main_submitted_unconfirmed = (
            self.main_comment.get("posted", False)
            and (
                self.main_comment.get("network_confirmed", False)
                or self.main_comment.get("local_confirmed", False)
            )
            and not self.main_comment.get("verified", False)
            and (
                bool(self.main_comment.get("comment_id"))
                or bool(self.main_comment.get("local_confirmation_without_id"))
            )
        )
        # Строгое правило: если ответы запланированы, успешными должны быть все.
        replies_ok = len(self.replies) == 0 or successful_replies == len(self.replies)
        
        # Лайки через Boost считаются отдельным вторичным шагом.
        # Основной заказ успешен по факту комментария/ответов, а не по факту Boost-заказа.
        from services.boost_project_client import is_boost_enabled as is_provider_enabled
        provider_enabled = is_provider_enabled()
        boost_ok = (not self.boost_enabled) or (not provider_enabled) or self.boost_order_id is not None
        boost_requested = self.boost_enabled and provider_enabled

        if mode == "engagement_only":
            main_core_confirmed = self.comment_found
        elif not self.main_comment.get("comment_id") and self.comment_id:
            main_core_confirmed = self.comment_found
        else:
            main_core_confirmed = main_success
        core_success = main_core_confirmed and replies_ok
        submitted_unconfirmed = (
            replies_terminal_ok
            and not core_success
            and (
                main_submitted_unconfirmed
                or (main_core_confirmed and submitted_unconfirmed_replies > 0)
            )
        )

        warnings = list(self.warnings)
        if core_success and boost_requested and not boost_ok:
            warnings.append({
                "code": "boost_failed",
                "message": self.boost_error or "Comment likes were not created"
            })
        if submitted_unconfirmed and main_submitted_unconfirmed:
            warnings.append({
                "code": "main_submitted_unconfirmed",
                "message": self.main_comment.get("error") or "Main comment was submitted but independent verification did not confirm it yet",
            })
        if submitted_unconfirmed_replies:
            warnings.append({
                "code": "reply_submitted_unconfirmed",
                "message": "One or more replies were submitted but parent binding or independent visibility was not fully confirmed",
            })
        seen_author_fallbacks: set[tuple[str, str]] = set()
        for reply in self.replies:
            if not reply.get("author_fallback_used"):
                continue
            requested_author = str(reply.get("requested_parent_author") or "").strip()
            matched_author = str(reply.get("matched_parent_author") or "").strip()
            fallback_key = (requested_author, matched_author)
            if fallback_key in seen_author_fallbacks:
                continue
            seen_author_fallbacks.add(fallback_key)
            warnings.append({
                "code": "engagement_parent_author_fallback",
                "message": "Requested parent author was not found; replied to the only exact text match",
                "requested_author": requested_author,
                "matched_author": matched_author,
            })

        error_message = None
        if not core_success and not submitted_unconfirmed:
            error_message = self.main_comment.get("error")
            if not error_message:
                error_message = next(
                    (reply.get("error") for reply in self.replies if reply.get("error")),
                    None,
                )

        duration_ms = None
        if started_at and ended_at:
            try:
                start_dt = datetime.fromisoformat(started_at.replace('Z', '+00:00'))
                end_dt = datetime.fromisoformat(ended_at.replace('Z', '+00:00'))
                duration_ms = int((end_dt - start_dt).total_seconds() * 1000)
            except Exception:
                pass
        if not ended_at:
            ended_at = datetime.now(timezone.utc).isoformat()
        if not started_at:
            started_at = ended_at
        
        result = {
            "status": "success" if core_success else ("submitted_unconfirmed" if submitted_unconfirmed else "fail"),
            "core_status": "success" if core_success else ("submitted_unconfirmed" if submitted_unconfirmed else "fail"),
            "comment_id": self.comment_id or self.main_comment.get("comment_id"),
            "error": error_message,
            "warnings": warnings,
            "has_warnings": bool(warnings),
            "source": source,
            "mode": mode,
            "engagement": {
                "main": self.main_comment,
                "replies": {
                    "ok": successful_replies,
                    "total": len(self.replies),
                    "submitted": successful_replies + submitted_unconfirmed_replies,
                    "submitted_unconfirmed": submitted_unconfirmed_replies,
                    "items": self.replies
                },
                "likes": {
                    "provider": "boost_project",
                    "order_id": self.boost_order_id,
                    "orders": list(self.boost_orders),
                    "quantity": self.boost_quantity,
                    "target_mode": self.boost_target_mode,
                    "error": self.boost_error,
                    "enabled": self.boost_enabled,
                    "provider_available": provider_enabled,
                    "ok": boost_ok,
                    "requested": boost_requested,
                }
            },
            "timing": {
                "started_at": started_at,
                "ended_at": ended_at,
                "duration_ms": duration_ms
            }
        }
        try:
            from services.task_execution_ledger import attach_url_execution_ledger

            attach_url_execution_ledger(result)
        except Exception:
            logger.debug("Failed to attach engagement execution ledger", exc_info=True)
        return result


async def find_comment_for_engagement(
    db: Session,
    video_url: str,
    comment_id: Optional[str] = None,
    username: Optional[str] = None,
    comment_text: Optional[str] = None,
    task_id: Optional[int] = None,
    *,
    pool_owner_user_id: Optional[int] = None,
) -> Tuple[bool, Optional[str], Optional[str]]:
    """
    Находит комментарий для engagement_only заказа.
    
    Сначала пытается найти по comment_id, если не указан - ищет по username + text через DOM.
    
    Args:
        db: Сессия БД
        video_url: URL видео
        comment_id: Опциональный ID комментария (приоритет)
        username: Опциональное имя автора комментария
        comment_text: Опциональный текст комментария (для поиска)
        task_id: Опциональный ID задачи
        
    Returns:
        tuple: (found, comment_id, author)
    """
    video_url, _ = await resolve_video_url(video_url)

    # Если есть comment_id - используем его сразу
    if comment_id:
        normalized_comment_id = str(comment_id).strip()
        logger.info(f"Task {task_id}: Verifying provided comment_id: {normalized_comment_id[:20]}")

        max_attempts = max(1, settings.MAX_RETRY_ACCOUNTS)
        tried_account_ids: list[int] = []

        for attempt in range(max_attempts):
            verify_account = get_free_account_for_reply(
                db,
                exclude_ids=tried_account_ids,
                exclude_degraded_categories={"captcha", "page_blocked"},
                pool_owner_user_id=pool_owner_user_id,
            )
            if not verify_account:
                logger.warning(f"Task {task_id}: No account available for comment_id verification")
                return False, None, None

            tried_account_ids.append(verify_account.id)
            context = None
            page = None
            try:
                context, page = await create_page_for_account(verify_account)
                timeout = settings.PLAYWRIGHT_TIMEOUT * 2 if verify_account.proxy else settings.PLAYWRIGHT_TIMEOUT
                await page.goto(
                    video_url,
                    wait_until="load" if verify_account.proxy else "domcontentloaded",
                    timeout=timeout
                )
                await page.wait_for_timeout(1200)
                await _ensure_comment_panel_open(page)

                from executor.like_finder import find_comment_by_id as dom_find_comment_by_id
                _, find_info = await dom_find_comment_by_id(
                    page=page,
                    comment_id=normalized_comment_id,
                    using_proxy=bool(verify_account.proxy),
                    max_scrolls=10,
                )
                if isinstance(find_info, dict) and find_info.get("found"):
                    dom_author = (find_info.get("author") or "").strip() or None
                    fallback_author = (username or "").strip() or None
                    resolved_author = dom_author or fallback_author
                    if dom_author and fallback_author:
                        dom_norm = dom_author.strip().lower().lstrip("@")
                        fallback_norm = fallback_author.strip().lower().lstrip("@")
                        if dom_norm != fallback_norm:
                            logger.warning(
                                f"Task {task_id}: DOM author mismatch for engagement_only comment_id. "
                                f"provided={fallback_author}, dom={dom_author}. Using DOM author."
                            )
                    logger.info(
                        f"Task {task_id}: Provided comment_id exists in DOM. "
                        f"comment_id={normalized_comment_id[:20]}, account={verify_account.id}"
                    )
                    return True, normalized_comment_id, resolved_author

                logger.warning(
                    f"Task {task_id}: comment_id not found in DOM "
                    f"(attempt {attempt + 1}/{max_attempts}, account {verify_account.id})"
                )
            except Exception as e:
                logger.warning(
                    f"Task {task_id}: Error verifying comment_id on account {verify_account.id}: {e}",
                    exc_info=True
                )
            finally:
                await _close_attempt_resources(page, context, reason="engagement_verify_done")

        return False, None, None

    if comment_text:
        logger.info(
            "Task %s: Searching engagement comment by exact text (author is optional): %s",
            task_id,
            comment_text[:50],
        )

        found, found_comment_id, found_author, verify_mode = await verify_comment_dom(
            db=db,
            video_url=video_url,
            comment_text=comment_text,
            expected_author=None,
            task_id=task_id,
            pool_owner_user_id=pool_owner_user_id,
            text_match_ratio=1.0,
        )

        if found:
            resolved_author = found_author or username
            logger.info(
                f"Task {task_id}: Comment found via DOM search. "
                f"comment_id={found_comment_id[:20] if found_comment_id else 'None'}, "
                f"author={resolved_author}, mode={verify_mode}"
            )
            return True, found_comment_id, resolved_author

        if verify_mode is None:
            logger.warning(f"Task {task_id}: Cannot search comment: no verify account available")
            return False, None, "no_verify_account_available"

        logger.warning(f"Task {task_id}: Comment not found via DOM search")
        return False, None, None

    logger.warning(f"Task {task_id}: No comment_id or comment_text provided for engagement_only")
    return False, None, None


async def verify_comment_dom(
    db: Session,
    video_url: str,
    comment_text: str,
    expected_comment_id: Optional[str] = None,
    expected_author: Optional[str] = None,
    task_id: Optional[int] = None,
    prefer_fast_path: bool = False,
    exclude_account_ids: Optional[List[int]] = None,
    retry_schedule: Optional[List[float]] = None,
    *,
    pool_owner_user_id: Optional[int] = None,
    require_author_match: bool = False,
    text_match_ratio: float = 0.7,
) -> Tuple[bool, Optional[str], Optional[str], Optional[str]]:
    """
    Верифицирует комментарий через DOM (отдельный аккаунт).
    
    Args:
        db: Сессия БД
        video_url: URL видео
        comment_text: Текст комментария для поиска
        expected_comment_id: Опциональный ID комментария
        expected_author: Опциональное имя автора
        task_id: Опциональный ID задачи
        
    Returns:
        tuple: (found, comment_id, author)
    """
    if retry_schedule is None:
        retry_schedule = [0.0] * max(1, settings.MAX_RETRY_ACCOUNTS)

    tried_account_ids: list[int] = list(exclude_account_ids or [])
    last_verify_mode: Optional[str] = None
    total_attempts = len(retry_schedule)

    for attempt_idx, _delay_seconds in enumerate(retry_schedule):
        if _delay_seconds and _delay_seconds > 0:
            await asyncio.sleep(_delay_seconds)

        verify_account = get_free_account_for_reply(
            db,
            exclude_ids=tried_account_ids,
            exclude_degraded_categories={"captcha", "page_blocked"},
            pool_owner_user_id=pool_owner_user_id,
        )
        if not verify_account:
            logger.warning(
                f"Task {task_id}: No account available for DOM verification "
                f"(attempt {attempt_idx + 1}/{total_attempts})"
            )
            return False, None, None, last_verify_mode

        tried_account_ids.append(verify_account.id)
        try:
            if prefer_fast_path and expected_comment_id and attempt_idx == 0:
                found, verify_info = await verify_with_retries(
                    verify_account=verify_account,
                    video_url=video_url,
                    main_comment_text=comment_text,
                    task_id=task_id,
                    expected_author=expected_author,
                    expected_comment_id=expected_comment_id,
                    timeout=min(settings.COMMENT_APPEARANCE_TIMEOUT, FAST_VERIFY_TIMEOUT_SECONDS),
                    interval=min(settings.COMMENT_VERIFY_CHECK_INTERVAL, FAST_VERIFY_INTERVAL_SECONDS),
                    verify_mode="fast_precheck",
                    max_scrolls=FAST_VERIFY_MAX_SCROLLS,
                    scroll_delay_ms=FAST_VERIFY_SCROLL_DELAY_MS,
                    require_author_match=require_author_match,
                    text_match_ratio=text_match_ratio,
                )

                if found:
                    comment_id = verify_info.get("comment_id")
                    author = verify_info.get("author")
                    _log_pipeline_event(
                        task_id,
                        "comment_verify",
                        "success",
                        verify_mode="fast_precheck",
                        account_id=verify_account.id,
                        comment_id=(comment_id[:20] if comment_id else None),
                    )
                    logger.info(
                        f"Task {task_id}: Comment verified via DOM. "
                        f"comment_id={comment_id[:20] if comment_id else 'None'}, "
                        f"author={author}"
                    )
                    return True, comment_id, author, "fast_precheck"

                _log_pipeline_event(
                    task_id,
                    "comment_verify",
                    "escalate",
                    verify_mode="fast_precheck",
                    account_id=verify_account.id,
                    expected_comment_id=(expected_comment_id[:20] if expected_comment_id else None),
                )
                last_verify_mode = "fast_precheck"

            found, verify_info = await verify_with_retries(
                verify_account=verify_account,
                video_url=video_url,
                main_comment_text=comment_text,
                task_id=task_id,
                expected_author=expected_author,
                expected_comment_id=expected_comment_id,
                verify_mode="standard",
                require_author_match=require_author_match,
                text_match_ratio=text_match_ratio,
            )

            if found:
                comment_id = verify_info.get("comment_id")
                author = verify_info.get("author")
                _log_pipeline_event(
                    task_id,
                    "comment_verify",
                    "success",
                    verify_mode="standard",
                    account_id=verify_account.id,
                    comment_id=(comment_id[:20] if comment_id else None),
                )
                logger.info(
                    f"Task {task_id}: Comment verified via DOM. "
                    f"comment_id={comment_id[:20] if comment_id else 'None'}, "
                    f"author={author}"
                )
                return True, comment_id, author, "standard"

            last_verify_mode = "standard"
            logger.warning(
                f"Task {task_id}: Comment not verified via DOM "
                f"(attempt {attempt_idx + 1}/{total_attempts}, account {verify_account.id})"
            )
            _log_pipeline_event(
                task_id,
                "comment_verify",
                "fail",
                level="warning",
                verify_mode="standard",
                account_id=verify_account.id,
                attempt=f"{attempt_idx + 1}/{total_attempts}",
            )
        except Exception as e:
            logger.warning(
                f"Task {task_id}: Error in DOM verification on account {verify_account.id}: {e}",
                exc_info=True
            )
            _log_pipeline_event(
                task_id,
                "comment_verify",
                "error",
                level="warning",
                verify_mode="standard",
                account_id=verify_account.id,
                error=str(e)[:120],
            )

    return False, None, None, last_verify_mode or ("standard" if tried_account_ids else None)


_MAIN_PUBLISH_MODE = "main"
_REPLY_PUBLISH_MODE = "reply"


async def _attempt_publish_on_account(
    db: Session,
    account: Account,
    video_url: str,
    text: str,
    task_id: int,
    correlation_id: str,
    *,
    mode: str,
    parent_comment_id: Optional[str] = None,
    parent_comment_text: Optional[str] = None,
    outcome_dict: Optional[Dict[str, Any]] = None,
) -> tuple[Optional[str], Optional[str], str]:
    """Shared body for posting either a main comment or a reply on a single account.

    Returns (comment_id, error_reason, stage). On success comment_id is set and
    error_reason is None. On failure comment_id is None, error_reason carries a
    human-readable cause and stage is one of: 'bas', 'strategy', 'http_health_gate',
    'warmup', 'http', 'enrichment', 'bootstrap', 'publish', 'error'.

    The function chooses between three publish backends in this order:
      1. BAS HTTP server (when settings.USE_BAS is enabled) — externalised
         publishing service.
      2. HTTP via the warmed Playwright page (for accounts whose strategy is
         http_first / http_cautious). For replies this branch only runs if a
         parent_comment_id is supplied — TikTok's reply endpoint requires it.
      3. DOM publish via Playwright (fallback / dom_first strategy).
    """
    is_reply = mode == _REPLY_PUBLISH_MODE
    scope_label = mode  # used as 'main' / 'reply' for runtime stats
    http_strategy_scope = REPLY_SCOPE if is_reply else MAIN_SCOPE
    publish_kind_label = "Reply" if is_reply else "Main comment"

    if settings.USE_BAS:
        try:
            if is_reply:
                from executor.bas_client import post_reply_via_bas
                bas_result = await post_reply_via_bas(
                    account_id=account.id,
                    video_url=video_url,
                    reply_text=text,
                    parent_comment_text=parent_comment_text,
                    parent_comment_id=parent_comment_id,
                    proxy=account.proxy or None,
                    cookies_json=account.cookies or "[]",
                    correlation_id=correlation_id,
                    timeout=60.0,
                )
            else:
                from executor.bas_client import post_comment_via_bas
                bas_result = await post_comment_via_bas(
                    account_id=account.id,
                    video_url=video_url,
                    comment_text=text,
                    proxy=account.proxy or None,
                    cookies_json=account.cookies or "[]",
                    correlation_id=correlation_id,
                    timeout=60.0,
                )
        except Exception as bas_exc:
            logger.exception(
                "Task %s: BAS %s exception for account %s: %s",
                task_id, scope_label, account.id, bas_exc,
            )
            _register_runtime_failure(db, account, f"bas_exception: {str(bas_exc)[:80]}")
            return None, f"bas_exception: {str(bas_exc)[:80]}", "bas"

        if bas_result.status == "success":
            _register_runtime_success(db, account, scope=scope_label)
            logger.info(
                "Task %s: BAS %s SUCCESS on account %s | comment_id=%s",
                task_id, scope_label, account.id, bas_result.comment_id,
            )
            if outcome_dict is not None:
                outcome_dict["posted"] = True
                outcome_dict["network_confirmed"] = True
                outcome_dict["method"] = "bas"
            return bas_result.comment_id, None, "bas"
        if bas_result.status == "redirected":
            mark_needs_reauth(db, account.id, reason=bas_result.error_reason or "bas_redirected")
            return None, bas_result.error_reason, "bas"
        if bas_result.error_reason == "bas_not_running":
            logger.error(
                "Task %s: BAS server not running! Start bas_scripts/bas_http_server.py",
                task_id,
            )
            return None, "bas_not_running", "bas"
        _register_runtime_failure(db, account, bas_result.error_reason or "bas_failed")
        return None, bas_result.error_reason, "bas"

    context = None
    page = None
    try:
        context, page = await create_page_for_account(account)

        strategy_info = get_http_execution_strategy(account, http_strategy_scope)
        strategy = (strategy_info or {}).get("strategy")
        if strategy == "skip":
            reason = str((strategy_info or {}).get("reason") or "strategy_skip")
            return None, reason, "strategy"

        eligible_for_http = strategy in {"http_first", "http_cautious"}
        if is_reply:
            eligible_for_http = eligible_for_http and bool(parent_comment_id)

        if eligible_for_http:
            skip_reason = _should_skip_http_for_account(account.id)
            if skip_reason:
                logger.info(
                    "Task %s: Skipping HTTP for account %s (%s), trying DOM fallback",
                    task_id,
                    account.id,
                    skip_reason,
                )
            else:
                cookies_dict, profile_valid, warmup_info = await warmup_page_for_http(
                    page,
                    video_url,
                    correlation_id=correlation_id,
                )
                warmup_current_url = str(warmup_info.get("current_url") or "").strip()
                warmup_video_id = extract_video_id(warmup_current_url) if warmup_current_url else None
                if warmup_video_id:
                    video_url = warmup_current_url

                if not profile_valid:
                    reason = str(warmup_info.get("reason_code") or "warmup_failed")
                    missing_critical = warmup_info.get("missing_critical") or []
                    if reason == "signer_not_ready" and not missing_critical:
                        _record_http_account_result(account.id, "warmup_fail")
                        logger.info(
                            "Task %s: HTTP warmup signer not ready for account %s; "
                            "trying DOM fallback on the same account",
                            task_id,
                            account.id,
                        )
                    else:
                        _record_http_account_result(account.id, "soft_fail")
                        _register_runtime_failure(db, account, reason)
                        return None, reason, "warmup"
                else:
                    aweme_id = extract_video_id(video_url)
                    if not aweme_id:
                        aweme_id = warmup_info.get("resolved_video_id") or warmup_video_id
                        if aweme_id and warmup_current_url:
                            logger.info(
                                "Task %s: using warmup-resolved aweme_id=%s from %s",
                                task_id,
                                str(aweme_id)[:24],
                                warmup_current_url[:120],
                            )
                            video_url = warmup_current_url
                    if not aweme_id:
                        _record_http_account_result(account.id, "soft_fail")
                        _register_runtime_failure(db, account, "missing_aweme_id")
                        return None, "missing_aweme_id", "http"

                    http_client = await PlaywrightHttpClient.from_page(page)
                    if is_reply:
                        success, comment_id, error_msg, error_type, meta = await http_client.reply_comment(
                            aweme_id=aweme_id,
                            parent_comment_id=str(parent_comment_id).strip(),
                            text=text,
                            cookies=cookies_dict,
                            correlation_id=correlation_id,
                            video_url=video_url,
                        )
                        if success and comment_id:
                            _record_http_account_result(account.id, "success")
                            _register_runtime_success(db, account, scope=scope_label)
                            parent_confirmed = bool((meta or {}).get("parent_confirmed"))
                            parent_confirmation_level = (
                                (meta or {}).get("parent_confirmation_level")
                                or "unconfirmed"
                            )
                            logger.info(
                                "Task %s: %s HTTP SUCCESS on account %s | comment_id=%s | parent_level=%s",
                                task_id,
                                publish_kind_label,
                                account.id,
                                comment_id,
                                parent_confirmation_level,
                            )
                            if outcome_dict is not None:
                                outcome_dict["posted"] = True
                                outcome_dict["network_confirmed"] = True
                                outcome_dict["method"] = "http"
                                outcome_dict["parent_confirmed"] = parent_confirmed
                                outcome_dict["parent_confirmation_level"] = parent_confirmation_level
                                outcome_dict["response_parent_id"] = (meta or {}).get("response_parent_id")
                                outcome_dict["response_reply_to_reply_id"] = (meta or {}).get("response_reply_to_reply_id")
                                outcome_dict["response_root_comment_id"] = (meta or {}).get("response_root_comment_id")
                            return comment_id, None, "http"
                    else:
                        success, comment_id, error_msg, error_type = await http_client.publish_main_comment(
                            aweme_id=aweme_id,
                            text=text,
                            cookies=cookies_dict,
                            correlation_id=correlation_id,
                            video_url=video_url,
                        )
                        if success and comment_id:
                            _record_http_account_result(account.id, "success")
                            _register_runtime_success(db, account, scope=scope_label)
                            logger.info(
                                "Task %s: %s HTTP SUCCESS on account %s | comment_id=%s",
                                task_id, publish_kind_label, account.id, comment_id,
                            )
                            if outcome_dict is not None:
                                outcome_dict["posted"] = True
                                outcome_dict["network_confirmed"] = True
                                outcome_dict["method"] = "http"
                            return comment_id, None, "http"

                    reason = error_msg or ("http_reply_failed" if is_reply else "http_failed")
                    request_meta = getattr(http_client, "last_request_meta", None)
                    request_attempted = True
                    if isinstance(request_meta, dict) and "request_attempted" in request_meta:
                        request_attempted = bool(request_meta.get("request_attempted"))

                    if not request_attempted:
                        logger.info(
                            "Task %s: %s HTTP stopped before TikTok request on account %s "
                            "(%s); trying DOM fallback on the same account",
                            task_id,
                            publish_kind_label,
                            account.id,
                            str(reason)[:120],
                        )
                    else:
                        _record_http_account_failure_result(account.id, error_type)
                        _register_runtime_failure(db, account, reason, error_type=error_type)
                        return None, reason, "http"

        # Cookie enrichment runs only for main publishes. We deliberately skip
        # it for replies so we do not slow down the answer (the main pass on a
        # different account already enriched cookies recently).
        if not is_reply and needs_enrichment(account):
            logger.info(
                "Task %s: Enriching cookies for account %s before main task",
                task_id, account.id,
            )
            try:
                from services.cookie_validator import (
                    get_cookies_for_account,
                    parse_cookies_to_dict,
                )

                existing_cookies = parse_cookies_to_dict(get_cookies_for_account(account))
                enrich_result = await enrich_cookies_via_browser(
                    page,
                    existing_cookies,
                    correlation_id=f"enrich_task{task_id}_acc{account.id}",
                    timeout_seconds=12.0,
                )
                enrich_result.log_summary(account.id, correlation_id)

                if enrich_result.redirected:
                    mark_needs_reauth(db, account.id, reason="enrichment_redirect_dead_session")
                    return None, "dead_session_during_enrichment", "enrichment"

                if enrich_result.success and enrich_result.all_cookies_json:
                    save_enriched_cookies_to_db(db, account.id, enrich_result.all_cookies_json)
                    account.cookies = enrich_result.all_cookies_json
                    logger.info(
                        "Task %s: Account %s cookies enriched: %s -> %s | new=%s",
                        task_id,
                        account.id,
                        enrich_result.strength_before,
                        enrich_result.strength_after,
                        enrich_result.new_cookies,
                    )
                else:
                    reason = f"enrichment_failed:{enrich_result.error or 'missing_required_browser_cookies'}"
                    logger.warning(
                        "Task %s: Cookie enrichment failed for account %s: %s. "
                        "Skipping this account instead of spending time on weak cookies.",
                        task_id,
                        account.id,
                        enrich_result.error,
                    )
                    _register_runtime_failure(db, account, reason)
                    return None, reason, "enrichment"

                _BROWSER_AUTH_COOKIES = {"sessionid", "sessionid_ss", "sid_tt"}
                try:
                    from services.cookie_validator import (
                        get_cookies_for_account,
                        parse_cookies_to_dict,
                    )
                    _fresh_cookies = parse_cookies_to_dict(get_cookies_for_account(account))
                    _has_browser_auth = any(k in _fresh_cookies for k in _BROWSER_AUTH_COOKIES)
                    _has_login_auth = any(k in _fresh_cookies for k in LOGIN_AUTH_COOKIES)
                except Exception:
                    _has_browser_auth = False
                    _has_login_auth = False

                if not _has_browser_auth:
                    if _has_login_auth:
                        reason = "enrichment_network_missing_browser_auth"
                        logger.warning(
                            "Task %s: Account %s still has login auth after enrichment but no "
                            "sessionid/sessionid_ss/sid_tt (auth=%s). Applying runtime cooldown "
                            "instead of reauth.",
                            task_id,
                            account.id,
                            enrich_result.new_cookies,
                        )
                        _register_runtime_failure(db, account, reason)
                        return None, reason, "enrichment"

                    logger.warning(
                        "Task %s: Account %s — no sessionid/sid_tt after enrichment "
                        "(auth=%s). Dead session, marking reauth_needed and skipping.",
                        task_id,
                        account.id,
                        enrich_result.new_cookies,
                    )
                    mark_needs_reauth(db, account.id, reason="no_login_auth_after_enrichment")
                    return None, "dead_session: no_login_auth_after_enrichment", "enrichment"
            except Exception as enrich_exc:
                reason = f"enrichment_exception:{str(enrich_exc)[:80]}"
                logger.warning(
                    "Task %s: Cookie enrichment exception for account %s: %s. "
                    "Skipping this account.",
                    task_id,
                    account.id,
                    enrich_exc,
                )
                _register_runtime_failure(db, account, reason)
                return None, reason, "enrichment"
        elif not is_reply:
            try:
                from services.cookie_validator import (
                    get_cookies_for_account,
                    parse_cookies_to_dict,
                )

                cookies = parse_cookies_to_dict(get_cookies_for_account(account))
                strength = assess_cookie_strength(cookies)
                logger.debug(
                    "Task %s: Account %s cookies already %s, skipping enrichment",
                    task_id, account.id, strength,
                )
            except Exception:
                pass

        boot = await bootstrap_session(
            page,
            video_url,
            max_total_seconds=22.0,
            correlation_id=correlation_id,
        )

        if boot.status == "dead_session":
            mark_needs_reauth(db, account.id, reason=boot.dead_reason or "dead_session")
            return None, f"dead_session: {boot.dead_reason}", "bootstrap"

        if boot.status == "navigation_failed":
            _register_runtime_failure(db, account, boot.dead_reason or "nav_fail")
            return None, f"navigation: {boot.dead_reason}", "bootstrap"

        if not boot.can_attempt_publish:
            boot_cookies = getattr(boot, "cookies", {}) or {}
            has_login_auth = any(name in boot_cookies for name in LOGIN_AUTH_COOKIES)
            if boot.status == "timeout" or has_login_auth:
                reason = (
                    "bootstrap_timeout_incomplete_session"
                    if boot.status == "timeout"
                    else "weak_browser_linkage_no_ttwid"
                )
                _register_runtime_failure(db, account, reason)
                logger.warning(
                    "Task %s: Account %s bootstrap produced auth but publish cookies are incomplete "
                    "(status=%s, auth=%s, generated=%s). Applying runtime cooldown instead of reauth.",
                    task_id,
                    account.id,
                    boot.status,
                    getattr(boot, "auth_present", []),
                    getattr(boot, "generated_present", []),
                )
                return None, reason, "bootstrap"

            mark_needs_reauth(db, account.id, reason="weak_session_no_login_auth")
            return None, "weak_session_no_login_auth", "bootstrap"

        try:
            await page.wait_for_selector(
                'video, [data-e2e*="video"], [class*="video-player"]',
                timeout=5000,
                state="visible",
            )
        except Exception:
            pass
        await asyncio.sleep(1.0)

        try:
            from executor.tiktok.overlays import dismiss_all_overlays
            await dismiss_all_overlays(page)
        except ImportError:
            try:
                await page.evaluate(r'''
                    () => {
                        const acceptBtns = document.querySelectorAll(
                            'button[data-e2e="accept-button"]'
                        );
                        for (const btn of acceptBtns) {
                            try { btn.click(); } catch (e) {}
                        }
                        const closeBtns = document.querySelectorAll(
                            '[class*="close" i][role="button"], ' +
                            'button[aria-label*="close" i], ' +
                            'button[aria-label*="закрыть" i]'
                        );
                        for (const btn of closeBtns) {
                            try {
                                const r = btn.getBoundingClientRect();
                                if (r.width > 0 && r.height > 0) btn.click();
                            } catch (e) {}
                        }
                    }
                ''')
            except Exception:
                pass
        except Exception:
            pass
        await asyncio.sleep(0.5)

        if not is_reply:
            session_strength = assess_cookie_strength(boot.cookies)
            logger.info(
                "Task %s: Account %s session strength = %s (auth=%s)",
                task_id,
                account.id,
                session_strength,
                boot.auth_present,
            )

        if is_reply:
            result = await publish_reply_hybrid(
                page,
                text,
                parent_comment_text=parent_comment_text,
                parent_comment_id=parent_comment_id,
                max_total_seconds=_reply_publish_max_seconds(),
                correlation_id=correlation_id,
                expected_video_url=video_url,
            )
        else:
            result = await publish_comment_hybrid(
                page,
                text,
                max_total_seconds=_main_publish_max_seconds(),
                correlation_id=correlation_id,
            )

        if (
            result.status in ("dom_error", "timeout")
            and "input_not_found" in (result.error_reason or "")
        ):
            if "after_captcha_clear" in (result.error_reason or ""):
                _register_runtime_failure(db, account, "captcha_after_clear_input_not_found")
                logger.warning(
                    "Task %s: Account %s still has no comment input after manual captcha clear; "
                    "not marking reauth_needed; applying captcha runtime cooldown.",
                    task_id,
                    account.id,
                )
                return None, result.error_reason, "publish"

            try:
                has_captcha, captcha_type = await detect_captcha(page, wait_ms=0)
            except Exception:
                has_captcha, captcha_type = False, None

            if has_captcha:
                captcha_reason = f"captcha_challenge:{captcha_type or 'unknown'}"
                _register_runtime_failure(db, account, captcha_reason)
                logger.warning(
                    "Task %s: Account %s has TikTok captcha while input is hidden; "
                    "not marking reauth_needed.",
                    task_id,
                    account.id,
                )
                return None, captcha_reason, "publish"

            try:
                await dismiss_cookie_banners(page)
            except Exception:
                pass

            try:
                login_state = await get_browser_login_state(page)
            except Exception:
                login_state = {"state": "unknown"}
            browser_state = str(login_state.get("state") or "unknown")

            if browser_state != "logged_out":
                if not is_reply:
                    try:
                        import os
                        os.makedirs("diagnostics", exist_ok=True)
                        import time
                        ts = int(time.time())
                        path = (
                            f"diagnostics/task_{task_id}_acc_{account.id}_"
                            f"{ts}_input_not_found.png"
                        )
                        await page.screenshot(path=path, full_page=False)
                        logger.info(f"Task {task_id}: Diagnostic screenshot saved: {path}")
                    except Exception:
                        pass
                logger.warning(
                    "Task %s: Account %s input_not_found but browser state is %s; "
                    "not marking reauth_needed. state=%s",
                    task_id,
                    account.id,
                    browser_state,
                    login_state,
                )
                no_progress_count = _record_ui_no_progress(account.id)
                cooldown_after = (
                    UI_NO_PROGRESS_UNKNOWN_COOLDOWN_AFTER
                    if browser_state == "unknown"
                    else UI_NO_PROGRESS_COOLDOWN_AFTER
                )
                if no_progress_count >= cooldown_after:
                    reason = f"unknown_ui_blocked:input_not_found:{browser_state}"
                    _register_runtime_failure(db, account, reason)
                    _reset_ui_no_progress(account.id)
                    logger.warning(
                        "Task %s: Account %s reached %s consecutive input_not_found/no-cause "
                        "attempts; applying runtime cooldown (%s).",
                        task_id,
                        account.id,
                        no_progress_count,
                        reason,
                    )
                else:
                    logger.info(
                        "Task %s: Account %s no-progress count=%s/%s before runtime cooldown.",
                        task_id,
                        account.id,
                        no_progress_count,
                        cooldown_after,
                    )
                return None, result.error_reason, "publish"

            if not is_reply:
                try:
                    import os
                    os.makedirs("diagnostics", exist_ok=True)
                    import time
                    ts = int(time.time())
                    path = (
                        f"diagnostics/task_{task_id}_acc_{account.id}_"
                        f"{ts}_input_not_found.png"
                    )
                    await page.screenshot(path=path, full_page=False)
                    logger.info(f"Task {task_id}: Diagnostic screenshot saved: {path}")
                except Exception:
                    pass
                mark_needs_reauth(db, account.id, reason="input_not_found_logged_out")
                logger.warning(
                    "Task %s: Account %s — input_not_found, session likely dead. "
                    "Marked reauth_needed.",
                    task_id, account.id,
                )
            else:
                mark_needs_reauth(db, account.id, reason="reply_input_not_found_logged_out")
                logger.warning(
                    "Task %s: Reply account %s — input_not_found, session dead. "
                    "Marked reauth_needed.",
                    task_id, account.id,
                )
            return None, result.error_reason, "publish"

        if result.status == "success":
            _record_http_account_result(account.id, "success")
            _register_runtime_success(db, account, scope=scope_label)
            logger.info(
                f"Task {task_id}: {publish_kind_label} SUCCESS on account {account.id} "
                f"via {result.dom_strategy} | comment_id={result.comment_id} "
                f"| elapsed={result.elapsed_seconds:.1f}s"
            )
            if outcome_dict is not None:
                outcome_dict["posted"] = True
                # network_confirmed is True only when the DOM publisher actually
                # observed TikTok's network response confirming the post — that
                # signal is much stronger than seeing the comment locally in DOM.
                outcome_dict["network_confirmed"] = bool(getattr(result, "network_captured", False))
                outcome_dict["local_confirmed"] = False
                outcome_dict["confirmation_source"] = (
                    "network_comment_id"
                    if bool(getattr(result, "network_captured", False))
                    else "dom_publish_success"
                )
                outcome_dict["method"] = "hybrid_dom"
            return result.comment_id, None, "hybrid"

        if result.status == "redirected":
            mark_needs_reauth(db, account.id, reason=result.error_reason or "redirected")
            return None, result.error_reason, "publish"

        if _is_ambiguous_post_submit_result(result):
            ambiguous_reason = result.error_reason or result.status or "ambiguous_publish"
            if is_reply:
                recovery = await _recover_reply_comment_after_ambiguous_publish(
                    page,
                    text,
                    task_id,
                    account.id,
                    ambiguous_reason,
                )
            else:
                recovery = await _recover_main_comment_after_ambiguous_publish(
                    page,
                    text,
                    task_id,
                    account.id,
                    ambiguous_reason,
                )
            recovery = _normalize_dom_recovery_result(recovery)
            recovered_comment_id = recovery.get("comment_id") if recovery else None
            locally_found_without_id = bool(
                recovery and recovery.get("found") and not recovered_comment_id
            )
            if recovered_comment_id or locally_found_without_id:
                _record_http_account_result(account.id, "success")
                _register_runtime_success(db, account, scope=scope_label)
                if outcome_dict is not None:
                    outcome_dict["posted"] = True
                    outcome_dict["network_confirmed"] = False
                    outcome_dict["local_confirmed"] = True
                    outcome_dict["confirmation_source"] = (
                        "local_dom_after_ambiguous_reply_publish"
                        if is_reply
                        else (
                            "local_dom_after_ambiguous_publish"
                            if recovered_comment_id
                            else "local_dom_text_match_no_id"
                        )
                    )
                    if is_reply and locally_found_without_id and not recovered_comment_id:
                        outcome_dict["confirmation_source"] = "local_dom_reply_text_match_no_id"
                    outcome_dict["method"] = "hybrid_dom"
                    outcome_dict["comment_id"] = recovered_comment_id
                    outcome_dict["local_recovery"] = recovery
                    outcome_dict["local_confirmation_without_id"] = bool(
                        locally_found_without_id and not recovered_comment_id
                    )
                    if is_reply:
                        outcome_dict["parent_confirmed"] = True
                        outcome_dict["parent_confirmation_level"] = "ui_selected_parent"
                if recovered_comment_id:
                    return str(recovered_comment_id), None, "hybrid"
                if is_reply:
                    return None, None, "reply_local_no_id"
                return None, None, "hybrid_local_no_id"

        if result.status == "failed" and result.error_reason == "stealth_reject_empty_body":
            # Empty publish bodies are usually a runtime/browser rejection, not
            # proof that the login cookies are dead. A real auth loss is handled
            # by redirected/input_not_found/dead_session branches above.
            _register_runtime_failure(db, account, "suspicious: stealth_reject_empty_body")
            return None, "stealth_reject", "publish"

        _register_runtime_failure(db, account, result.error_reason or "unknown")
        return None, result.error_reason or "unknown", "publish"
    except Exception as e:
        logger.exception(
            f"Task {task_id}: unexpected {scope_label} error on account {account.id}: {e}"
        )
        try:
            db.rollback()
        except Exception:
            pass
        _register_runtime_failure(db, account, f"exception: {str(e)[:100]}")
        return None, f"exception: {str(e)[:100]}", "error"
    finally:
        await _close_attempt_resources(page, context, reason="engagement_publish_done")


async def attempt_main_comment_on_account(
    db: Session,
    account: Account,
    video_url: str,
    text_choice: str,
    task_id: int,
    correlation_id: str,
    outcome_dict: Optional[Dict[str, Any]] = None,
) -> tuple[Optional[str], Optional[str], str]:
    return await _attempt_publish_on_account(
        db=db,
        account=account,
        video_url=video_url,
        text=text_choice,
        task_id=task_id,
        correlation_id=correlation_id,
        mode=_MAIN_PUBLISH_MODE,
        outcome_dict=outcome_dict,
    )


async def attempt_reply_on_account(
    db: Session,
    account: Account,
    video_url: str,
    reply_text: str,
    parent_comment_text: Optional[str],
    task_id: int,
    correlation_id: str,
    parent_comment_id: Optional[str] = None,
    outcome_dict: Optional[Dict[str, Any]] = None,
) -> tuple[Optional[str], Optional[str], str]:
    return await _attempt_publish_on_account(
        db=db,
        account=account,
        video_url=video_url,
        text=reply_text,
        task_id=task_id,
        correlation_id=correlation_id,
        mode=_REPLY_PUBLISH_MODE,
        parent_comment_id=parent_comment_id,
        parent_comment_text=parent_comment_text,
        outcome_dict=outcome_dict,
    )



def _is_parent_not_found_error(error_msg: Optional[str]) -> bool:
    """Heuristic: did TikTok's reply API explicitly tell us the parent comment is gone?

    We only treat very specific phrases as a parent invalidation signal — these are
    the ones TikTok actually returns in `status_msg` when a parent comment is
    deleted, hidden by the author, or never existed. Generic API errors
    (CAPTCHA, rate limit, network) MUST NOT be classified as parent_not_found,
    otherwise a single bad request would falsely abort the task.
    """
    if not error_msg:
        return False
    lowered = error_msg.lower()
    markers = (
        "comment doesn't exist",
        "comment does not exist",
        "comment_not_exist",
        "parent doesn't exist",
        "parent does not exist",
        "parent comment not exist",
        "no such comment",
        "comment not found",
        "comment has been deleted",
        "comment was deleted",
        "comment is deleted",
        "target comment is deleted",
        "target comment does not exist",
        "комментарий не существует",
        "комментарий не найден",
        "комментарий удален",
        "комментарий удалён",
        "родительский комментарий не найден",
        "comentario no existe",
        "comentario eliminado",
        "comentário não existe",
        "comentário removido",
        "commentaire n'existe pas",
        "commentaire supprimé",
        "kommentar existiert nicht",
        "kommentar wurde gelöscht",
        "评论不存在",
        "评论已删除",
        "留言不存在",
        "留言已删除",
        "評論不存在",
        "評論已刪除",
        "コメントが存在しません",
        "コメントは存在しません",
        "コメントが削除されました",
        "댓글이 존재하지 않습니다",
        "댓글을 찾을 수 없습니다",
        "댓글이 삭제되었습니다",
    )
    return any(marker in lowered for marker in markers)


async def execute_replies_http(
    db: Session,
    video_url: str,
    parent_comment_id: str,
    replies: List[str],
    task_id: Optional[int] = None,
    main_comment_text: Optional[str] = None,
    parent_unconfirmed: bool = False,
    *,
    parent_author: Optional[str] = None,
    pool_owner_user_id: Optional[int] = None,
) -> List[Dict[str, Any]]:
    results: List[Dict[str, Any]] = []
    video_url, _ = await resolve_video_url(video_url)

    norm_parent_id = str(parent_comment_id).strip() if parent_comment_id else ""
    parent_author = (parent_author or "").strip() or None
    allow_dom_text_parent = bool(not norm_parent_id and main_comment_text)
    if not norm_parent_id and not allow_dom_text_parent:
        for reply_text in replies:
            if not reply_text:
                continue
            results.append({
                "text": reply_text,
                "success": False,
                "method": None,
                "error": "parent_comment_id is empty",
                "reply_id": None,
                "http_error_type": None,
                "parent_confirmed": False,
                "parent_confirmation_level": "unconfirmed",
                "visibility_status": "not_checked",
                "local_visible": None,
                "verify_mode": None,
                "attempts": 0,
                "fallback_used": False,
                "warmup_reason": None,
            })
        return results

    if not norm_parent_id:
        logger.warning(
            "Task %s: parent_comment_id is missing; replies will use DOM parent "
            "selection by exact main comment text%s.",
            task_id,
            f" and author={parent_author}" if parent_author else "",
        )

    if not main_comment_text:
        logger.warning(
            "Task %s: execute_replies_http called without main_comment_text. "
            "Reply will locate parent comment by ID attribute only (less reliable if TikTok omits it).",
            task_id,
        )

    parent_invalidated = False

    for reply_idx, reply_text in enumerate(replies):
        if not reply_text:
            continue

        if parent_invalidated:
            # Smart check on the first reply already determined the parent
            # comment doesn't exist on TikTok's side. Don't waste accounts on
            # subsequent replies — they would all 404 with the same signal.
            results.append({
                "text": reply_text,
                "success": False,
                "method": None,
                "error": "skipped_parent_invalidated",
                "reply_id": None,
                "http_error_type": "parent_not_found",
                "parent_confirmed": False,
                "parent_confirmation_level": "unconfirmed",
                "visibility_status": "skipped",
                "local_visible": None,
                "verify_mode": None,
                "attempts": 0,
                "fallback_used": False,
                "warmup_reason": None,
            })
            continue

        if reply_idx > 0:
            import random
            inter_delay = random.uniform(3.0, 7.0)
            logger.debug(
                f"Task {task_id}: Inter-reply delay {inter_delay:.1f}s before reply {reply_idx + 1}"
            )
            await asyncio.sleep(inter_delay)

        used_account_ids: List[int] = []
        last_error: Optional[str] = None
        reply_id: Optional[str] = None
        reply_completed = False

        for attempt_num in range(1, max(1, settings.MAX_RETRY_ACCOUNTS) + 1):
            reply_account = get_free_account_for_reply(
                db,
                exclude_ids=used_account_ids if used_account_ids else None,
                exclude_degraded_categories={"captcha", "page_blocked"},
                pool_owner_user_id=pool_owner_user_id,
            )
            if not reply_account:
                last_error = "out_of_accounts"
                break

            used_account_ids.append(reply_account.id)
            strategy_info = get_http_execution_strategy(reply_account, REPLY_SCOPE)
            strategy = (strategy_info or {}).get("strategy")
            if strategy == "skip":
                last_error = str((strategy_info or {}).get("reason") or "strategy_skip")
                continue

            force_dom_text_parent = not norm_parent_id
            if force_dom_text_parent or strategy == "dom_first":
                try:
                    post_reply_func = globals().get("post_reply")
                    if post_reply_func is None:
                        from executor.tiktok_client_async import post_reply as post_reply_func

                    dom_result = await post_reply_func(
                        account=reply_account,
                        video_url=video_url,
                        main_comment_text=main_comment_text or "",
                        reply_text=reply_text,
                        task_id=task_id,
                        expected_author=parent_author,
                        expected_comment_id=norm_parent_id or None,
                        parent_comment_id=norm_parent_id or None,
                        strict_text_match=bool(force_dom_text_parent and not parent_author),
                    )
                except Exception as dom_exc:
                    last_error = f"dom_fallback_exception: {str(dom_exc)[:100]}"
                    _register_runtime_failure(db, reply_account, last_error)
                    continue

                if getattr(dom_result, "success", False):
                    reply_id = getattr(dom_result, "comment_id", None)
                    raw_parent_level = getattr(dom_result, "parent_confirmation_level", None)
                    if force_dom_text_parent and raw_parent_level in (None, "not_applicable"):
                        parent_level = "ui_text_exact" if not parent_author else "ui_author_only"
                    else:
                        parent_level = raw_parent_level or "unconfirmed"
                    visibility_status = getattr(dom_result, "visibility_status", None) or "not_checked"
                    local_visible = getattr(dom_result, "local_visible", None)
                    _register_runtime_success(db, reply_account, scope="reply")
                    results.append({
                        "text": reply_text,
                        "success": True,
                        "method": "dom_text_parent" if force_dom_text_parent else "dom_fallback",
                        "error": None,
                        "reply_id": reply_id,
                        "http_error_type": None,
                        "parent_confirmed": parent_level != "unconfirmed",
                        "parent_confirmation_level": parent_level,
                        "visibility_status": visibility_status,
                        "local_visible": local_visible,
                        "verify_mode": (
                            "dom_text_author_parent"
                            if force_dom_text_parent
                            else "dom_fallback"
                        ),
                        "attempts": len(used_account_ids),
                        "fallback_used": True,
                        "warmup_reason": None,
                    })
                    reply_completed = True
                    break

                last_error = (
                    getattr(dom_result, "details", None)
                    or getattr(dom_result, "error_type", None)
                    or "dom_fallback_failed"
                )
                _register_runtime_failure(db, reply_account, str(last_error))
                continue

            if settings.USE_PROXY and not reply_account.proxy:
                last_error = "account_without_proxy"
                continue

            logger.info(
                f"Task {task_id}: Reply attempt {attempt_num}/{settings.MAX_RETRY_ACCOUNTS} "
                f"on account {reply_account.id}"
            )

            reply_outcome: Dict[str, Any] = {}
            reply_id, err, stage = await attempt_reply_on_account(
                db=db,
                account=reply_account,
                video_url=video_url,
                reply_text=reply_text,
                parent_comment_text=main_comment_text,
                parent_comment_id=norm_parent_id,
                task_id=task_id or 0,
                correlation_id=f"task_{task_id}_reply_{reply_idx + 1}_a{attempt_num}",
                outcome_dict=reply_outcome,
            )

            reply_local_no_id_accepted = (
                stage == "reply_local_no_id"
                and err is None
                and bool(reply_outcome.get("posted"))
                and bool(reply_outcome.get("local_confirmed"))
                and bool(reply_outcome.get("local_confirmation_without_id"))
            )

            if reply_id or reply_local_no_id_accepted:
                result_method = "http" if stage == "http" else "hybrid"
                verify_mode = "http" if stage == "http" else "hybrid"
                parent_level = (
                    reply_outcome.get("parent_confirmation_level")
                    or ("ui_selected_parent" if stage != "http" else "unconfirmed")
                )
                parent_confirmed = (
                    bool(reply_outcome.get("parent_confirmed"))
                    if "parent_confirmed" in reply_outcome
                    else stage != "http"
                )
                parent_unconfirmed_after_submit = (
                    stage == "http"
                    and bool(norm_parent_id)
                    and not parent_confirmed
                )
                reply_submitted_unconfirmed = (
                    reply_local_no_id_accepted
                    or parent_unconfirmed_after_submit
                )
                if reply_local_no_id_accepted:
                    verify_mode = "local_dom_text_match_no_id"
                elif parent_unconfirmed_after_submit:
                    verify_mode = "http_parent_unconfirmed"
                results.append({
                    "text": reply_text,
                    "success": not reply_submitted_unconfirmed,
                    "method": result_method,
                    "error": (
                        None
                        if not reply_submitted_unconfirmed
                        else (
                            "reply_submitted_local_dom_text_match_no_id"
                            if reply_local_no_id_accepted
                            else "reply_parent_unconfirmed_after_submit"
                        )
                    ),
                    "reply_id": reply_id,
                    "http_error_type": None,
                    "parent_confirmed": parent_confirmed,
                    "parent_confirmation_level": parent_level,
                    "visibility_status": (
                        "submitted_unconfirmed"
                        if reply_submitted_unconfirmed
                        else "network_confirmed"
                    ),
                    "local_visible": True if reply_local_no_id_accepted else None,
                    "verify_mode": verify_mode,
                    "attempts": len(used_account_ids),
                    "fallback_used": False,
                    "warmup_reason": None,
                    "submitted_unconfirmed": reply_submitted_unconfirmed,
                    "local_confirmed": bool(reply_outcome.get("local_confirmed")),
                    "local_confirmation_without_id": bool(
                        reply_outcome.get("local_confirmation_without_id")
                    ),
                    "confirmation_source": reply_outcome.get("confirmation_source"),
                    "local_recovery": reply_outcome.get("local_recovery"),
                    "response_parent_id": reply_outcome.get("response_parent_id"),
                    "response_reply_to_reply_id": reply_outcome.get("response_reply_to_reply_id"),
                    "response_root_comment_id": reply_outcome.get("response_root_comment_id"),
                })
                reply_completed = True
                break

            last_error = err or "unknown"
            logger.warning(
                f"Task {task_id}: Reply {reply_idx + 1} attempt {attempt_num} failed "
                f"on account {reply_account.id} at stage={stage}: {last_error}"
            )

        if not reply_completed:
            # Smart-check: when the main comment was published but DOM could
            # not visually verify it, the FIRST reply doubles as a strong
            # signal of whether the parent really exists. If TikTok's HTTP
            # API explicitly told us the parent comment is gone we propagate
            # http_error_type='parent_not_found' so the orchestrator can
            # abort the task instead of attempting further replies.
            classified_http_error = None
            if (
                parent_unconfirmed
                and reply_idx == 0
                and _is_parent_not_found_error(last_error)
            ):
                classified_http_error = "parent_not_found"
                parent_invalidated = True
                logger.warning(
                    f"Task {task_id}: Smart-check via first reply confirmed "
                    f"parent comment is invalid on TikTok side: {last_error!r}. "
                    f"Aborting remaining replies."
                )

            results.append({
                "text": reply_text,
                "success": False,
                "method": None,
                "error": last_error or "reply_failed",
                "reply_id": None,
                "http_error_type": classified_http_error,
                "parent_confirmed": False,
                "parent_confirmation_level": "unconfirmed",
                "visibility_status": "failed",
                "local_visible": None,
                "verify_mode": None,
                "attempts": len(used_account_ids),
                "fallback_used": False,
                "warmup_reason": None,
            })

    return results


async def execute_engagement_only_replies_via_full_flow(
    db: Session,
    video_url: str,
    *,
    comment_text: str,
    comment_id: Optional[str],
    comment_author: Optional[str],
    replies: List[str],
    task_id: Optional[int] = None,
    pool_owner_user_id: Optional[int] = None,
    stop_requested: Optional[Callable[[], bool]] = None,
) -> tuple[List[Dict[str, Any]], Optional[str], Optional[str]]:
    """Run engagement-only replies through the same DOM flow as full orders.

    The old engagement-only path first burned viewer accounts for a separate
    DOM verification and then used a different reply implementation. Full
    orders already have the safer logic: login probe, cookie enrichment, URL
    guard, strict comment drawer opening, scoped parent search, and
    publish_reply_hybrid. Reuse that path here without touching the full order
    orchestration.
    """
    if not replies:
        return [], (str(comment_id).strip() if comment_id else None), comment_author

    anchor_text = (comment_text or "").strip()
    anchor_id = str(comment_id or "").strip() or None
    if not anchor_text and not anchor_id:
        return [
            {
                "text": reply_text,
                "success": False,
                "method": None,
                "error": "engagement_reply_anchor_missing",
                "reply_id": None,
                "http_error_type": None,
                "parent_confirmed": False,
                "parent_confirmation_level": "unconfirmed",
                "visibility_status": "not_checked",
                "local_visible": None,
                "verify_mode": "full_flow_dom_parent",
                "attempts": 0,
                "fallback_used": False,
                "warmup_reason": None,
            }
            for reply_text in replies
            if str(reply_text or "").strip()
        ], anchor_id, comment_author

    # Imported lazily to avoid coupling normal HYBRID paths to user_schema at
    # module import time. These are the exact helpers used by the full order
    # reply phase.
    from executor.user_schema import (
        PublishedMain,
        _main_parent_keys,
        _reply_outcome_parent_key,
        _reply_outcome_submitted,
        _reply_with_variant_on_all_mains,
    )

    results: List[Dict[str, Any]] = []
    used_reply_accounts: List[int] = []
    used_reply_proxy_values: set[str] = set()
    used_reply_proxy_counts: Dict[str, int] = {}
    resolved_parent_id = anchor_id
    resolved_author = (comment_author or "").strip() or None
    max_attempts = max(1, min(int(settings.MAX_RETRY_ACCOUNTS), ENGAGEMENT_ONLY_REPLY_MAX_ATTEMPTS))
    dynamic_worker_pool_enabled = bool(getattr(settings, "PARALLEL_DYNAMIC_WORKER_POOL_ENABLED", True))
    def _positive_int_setting(name: str, default: int) -> int:
        try:
            return max(1, int(getattr(settings, name, default) or default))
        except Exception:
            return max(1, int(default))

    requested_parallel_worker_target = _positive_int_setting("PARALLEL_COMMENTING_TARGET_BROWSERS", 3)
    parallel_browser_capacity = _positive_int_setting(
        "PARALLEL_COMMENTING_MAX_BROWSERS",
        max(3, requested_parallel_worker_target),
    )
    parallel_worker_target = min(requested_parallel_worker_target, parallel_browser_capacity)
    max_active_browsers_per_proxy = min(
        2,
        _positive_int_setting("PARALLEL_MAX_ACTIVE_BROWSERS_PER_PROXY", 2),
    )
    parallel_capacity_request = (
        parallel_browser_capacity
        if dynamic_worker_pool_enabled
        else 3
    )
    reply_selection_lock = asyncio.Lock()
    submitted_parent_keys_by_variant: Dict[int, set[str]] = {}
    last_outcomes_by_variant: Dict[int, List[Any]] = {}
    last_reply_account_by_variant: Dict[int, Optional[int]] = {}
    active_reply_proxy_counts: Dict[str, int] = {}
    temporary_reply_selection_error = "reply_account_temporarily_unavailable"
    try:
        reply_worker_attempt_timeout_seconds = max(
            1.0,
            float(getattr(settings, "PARALLEL_REPLY_WORKER_ATTEMPT_TIMEOUT_SECONDS", 240.0) or 240.0),
        )
    except Exception:
        reply_worker_attempt_timeout_seconds = 240.0

    def _current_main_candidate() -> PublishedMain:
        return PublishedMain(
            text=anchor_text or resolved_parent_id or "",
            comment_id=resolved_parent_id,
            network_confirmed=bool(resolved_parent_id),
            local_confirmed=True,
            method="engagement_only_anchor",
            account_id=0,
            tiktok_username=(resolved_author or None),
        )

    def _account_proxy_value(account: Optional[Account]) -> Optional[str]:
        proxy = str(getattr(account, "proxy", "") or "").strip()
        return proxy or None

    def _inc_proxy_count(counts: Dict[str, int], proxy: Optional[str]) -> None:
        if proxy:
            counts[proxy] = counts.get(proxy, 0) + 1

    def _dec_proxy_count(counts: Dict[str, int], proxy: Optional[str]) -> None:
        if not proxy:
            return
        current = counts.get(proxy, 0)
        if current <= 1:
            counts.pop(proxy, None)
        else:
            counts[proxy] = current - 1

    def _overloaded_active_proxies(counts: Dict[str, int]) -> set[str]:
        if not counts:
            return set()
        min_uses = min(counts.values())
        return {proxy for proxy, count in counts.items() if count > min_uses}

    async def _select_reply_account(attempt_db: Session) -> Tuple[Optional[Account], Optional[str]]:
        async with reply_selection_lock:
            strict_degraded_categories = {"captcha", "page_blocked"}
            capacity_blocked_proxies: set[str] = set()
            for _ in range(max(1, parallel_browser_capacity + 2)):
                exclude_proxies = (
                    set(active_reply_proxy_counts) | set(used_reply_proxy_values)
                    if dynamic_worker_pool_enabled
                    else set(used_reply_proxy_values)
                )
                exclude_proxies |= capacity_blocked_proxies
                candidate = get_free_account_for_reply(
                    attempt_db,
                    exclude_ids=list(used_reply_accounts),
                    exclude_proxies=exclude_proxies,
                    exclude_degraded_categories=strict_degraded_categories,
                    pool_owner_user_id=pool_owner_user_id,
                )
                if candidate is None and exclude_proxies:
                    balanced_exclude_proxies = (
                        _overloaded_active_proxies(active_reply_proxy_counts)
                        if dynamic_worker_pool_enabled
                        else set()
                    )
                    if not balanced_exclude_proxies and used_reply_proxy_counts:
                        min_uses = min(used_reply_proxy_counts.values())
                        balanced_exclude_proxies = {
                            proxy
                            for proxy, count in used_reply_proxy_counts.items()
                            if count > min_uses
                        }
                    if balanced_exclude_proxies:
                        balanced_exclude_proxies |= capacity_blocked_proxies
                        logger.info(
                            "Task %s: engagement_only no completely unused reply proxy; "
                            "trying least-used proxy selection",
                            task_id,
                        )
                        candidate = get_free_account_for_reply(
                            attempt_db,
                            exclude_ids=list(used_reply_accounts),
                            exclude_proxies=balanced_exclude_proxies,
                            exclude_proxy_source_hosts=False,
                            exclude_degraded_categories=strict_degraded_categories,
                            pool_owner_user_id=pool_owner_user_id,
                        )
                    if candidate is None:
                        logger.info(
                            "Task %s: engagement_only no reply account on less-used proxy; "
                            "falling back to account-only selection",
                            task_id,
                        )
                        candidate = get_free_account_for_reply(
                            attempt_db,
                            exclude_ids=list(used_reply_accounts),
                            exclude_proxies=capacity_blocked_proxies,
                            exclude_degraded_categories=strict_degraded_categories,
                            pool_owner_user_id=pool_owner_user_id,
                        )
                if not candidate:
                    if dynamic_worker_pool_enabled and (
                        active_reply_proxy_counts
                        or capacity_blocked_proxies
                    ):
                        logger.info(
                            "Task %s: engagement_only reply account selection deferred because proxy capacity is active "
                            "(active=%s capacity_blocked=%s)",
                            task_id,
                            dict(active_reply_proxy_counts),
                            list(capacity_blocked_proxies),
                        )
                        return None, temporary_reply_selection_error
                    return None, None
                proxy_value = _account_proxy_value(candidate)
                if (
                    dynamic_worker_pool_enabled
                    and proxy_value
                    and active_reply_proxy_counts.get(proxy_value, 0) >= max_active_browsers_per_proxy
                ):
                    capacity_blocked_proxies.add(proxy_value)
                    logger.info(
                        "Task %s: engagement_only reply account %s skipped because proxy already has %s/%s active browser(s)",
                        task_id,
                        candidate.id,
                        active_reply_proxy_counts.get(proxy_value, 0),
                        max_active_browsers_per_proxy,
                    )
                    continue
                used_reply_accounts.append(candidate.id)
                if proxy_value:
                    used_reply_proxy_values.add(proxy_value)
                    used_reply_proxy_counts[proxy_value] = used_reply_proxy_counts.get(proxy_value, 0) + 1
                    if dynamic_worker_pool_enabled:
                        _inc_proxy_count(active_reply_proxy_counts, proxy_value)
                return candidate, None
            if dynamic_worker_pool_enabled and (
                active_reply_proxy_counts
                or capacity_blocked_proxies
            ):
                return None, temporary_reply_selection_error
            return None, None

    def _ensure_parallel_browser_capacity() -> bool:
        try:
            from executor.browser_pool import ensure_thread_browser_pool_capacity

            ensure_thread_browser_pool_capacity(parallel_capacity_request)
            return True
        except Exception as exc:
            logger.warning(
                "Task %s: failed to raise engagement_only browser pool capacity: %s",
                task_id,
                str(exc)[:120],
            )
            return False

    async def _close_parallel_phase_browsers(phase: str) -> None:
        try:
            from executor.browser_pool import close_browser_pool

            await close_browser_pool()
            logger.info("Task %s: engagement_only parallel %s browser pool closed", task_id, phase)
        except Exception as exc:
            logger.warning(
                "Task %s: failed to close engagement_only browser pool after %s: %s",
                task_id,
                phase,
                str(exc)[:120],
            )

    def _is_stop_requested() -> bool:
        if stop_requested is None:
            return False
        try:
            return bool(stop_requested())
        except Exception as stop_error:
            logger.debug("Task %s: engagement_only stop check failed: %s", task_id, stop_error)
            return False

    def _stopped_reply_row(
        reply_text: str,
        variant_index: int,
        *,
        attempts: int,
        account_id: Optional[int],
    ) -> Dict[str, Any]:
        return {
            "text": reply_text,
            "success": False,
            "method": None,
            "error": "stopped_by_user",
            "reply_id": None,
            "http_error_type": None,
            "parent_confirmed": False,
            "parent_confirmation_level": "unconfirmed",
            "visibility_status": "not_checked",
            "local_visible": None,
            "verify_mode": "full_flow_dom_parent",
            "attempts": attempts,
            "fallback_used": False,
            "warmup_reason": None,
            "parent_comment_id": resolved_parent_id,
            "matched_main_text": None,
            "account_id": account_id,
            "variant_index": variant_index,
        }

    def _failure_reply_row(
        reply_text: str,
        variant_index: int,
        *,
        attempts: int,
        account_id: Optional[int],
        error: str,
    ) -> Dict[str, Any]:
        return {
            "text": reply_text,
            "success": False,
            "method": None,
            "error": error,
            "reply_id": None,
            "http_error_type": None,
            "parent_confirmed": False,
            "parent_confirmation_level": "unconfirmed",
            "visibility_status": "not_checked",
            "local_visible": None,
            "verify_mode": "full_flow_dom_parent",
            "attempts": attempts,
            "fallback_used": False,
            "warmup_reason": None,
            "parent_comment_id": resolved_parent_id,
            "matched_main_text": None,
            "account_id": account_id,
            "variant_index": variant_index,
        }

    async def _sleep_or_stop(seconds: float) -> bool:
        deadline = time.monotonic() + max(0.0, seconds)
        while True:
            if _is_stop_requested():
                return True
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return False
            await asyncio.sleep(min(0.5, remaining))

    async def _run_reply_variant_attempt(
        variant_idx: int,
        reply_text: str,
        attempt_num: int,
    ) -> Dict[str, Any]:
        attempt_info: Dict[str, Any] = {
            "variant_index": variant_idx,
            "text": reply_text,
            "completed": False,
            "stopped": False,
            "error": None,
            "account_id": None,
            "outcomes": [],
            "replies": [],
            "resolved_parent_id": None,
            "resolved_author": None,
            "matched_text": None,
            "proxy_value": None,
        }

        if _is_stop_requested():
            attempt_info["stopped"] = True
            attempt_info["error"] = "stopped_by_user"
            return attempt_info

        submitted_parent_keys = submitted_parent_keys_by_variant.setdefault(variant_idx, set())
        main_candidate = _current_main_candidate()
        pending_parent = (
            [main_candidate]
            if _main_parent_keys(main_candidate).isdisjoint(submitted_parent_keys)
            else []
        )
        if not pending_parent:
            attempt_info["completed"] = True
            return attempt_info

        from db.database import get_sync_db_context

        with get_sync_db_context() as attempt_db:
            reply_account, selection_error = await _select_reply_account(attempt_db)
            if not reply_account:
                if selection_error == temporary_reply_selection_error:
                    logger.info(
                        "Task %s: engagement_only reply variant %s waiting for a free proxy/browser slot",
                        task_id,
                        variant_idx,
                    )
                else:
                    logger.warning(
                        "Task %s: engagement_only out of reply accounts on variant %s",
                        task_id,
                        variant_idx,
                    )
                attempt_info["error"] = selection_error or "no_free_reply_accounts"
                return attempt_info

            attempt_info["account_id"] = reply_account.id
            attempt_info["proxy_value"] = _account_proxy_value(reply_account)
            last_reply_account_by_variant[variant_idx] = reply_account.id
            correlation_id = (
                f"task_{task_id}_engagement_reply_v{variant_idx}"
                f"_a{attempt_num}_acc{reply_account.id}"
            )
            logger.info(
                "Task %s: engagement_only parallel reply variant %s attempt %s/%s on account=%s",
                task_id,
                variant_idx,
                attempt_num,
                max_attempts,
                reply_account.id,
            )
            author_required = bool((main_candidate.tiktok_username or "").strip())
            allow_author_fallback = author_required and attempt_num == max_attempts

            try:
                attempt_result = await asyncio.wait_for(
                    _reply_with_variant_on_all_mains(
                        attempt_db,
                        reply_account,
                        video_url,
                        pending_parent,
                        reply_text,
                        variant_index=variant_idx,
                        correlation_id=correlation_id,
                        fallback_main_texts=None,
                        already_replied_parent_keys=submitted_parent_keys,
                        require_parent_author_match=author_required,
                        allow_parent_author_fallback=allow_author_fallback,
                    ),
                    timeout=reply_worker_attempt_timeout_seconds,
                )
            except asyncio.TimeoutError:
                logger.warning(
                    "Task %s: engagement_only parallel reply variant %s account=%s timed out after %.1fs",
                    task_id,
                    variant_idx,
                    reply_account.id,
                    reply_worker_attempt_timeout_seconds,
                )
                attempt_info["error"] = f"parallel_reply_attempt_timeout:{reply_worker_attempt_timeout_seconds:.1f}s"
                return attempt_info
            except Exception as exc:
                logger.warning(
                    "Task %s: engagement_only parallel reply variant %s account=%s raised: %s",
                    task_id,
                    variant_idx,
                    reply_account.id,
                    str(exc)[:120],
                )
                attempt_info["error"] = f"reply_attempt_exception:{str(exc)[:80]}"
                return attempt_info
            finally:
                if dynamic_worker_pool_enabled:
                    async with reply_selection_lock:
                        _dec_proxy_count(active_reply_proxy_counts, attempt_info.get("proxy_value"))

        outcomes = list(attempt_result.replies or [])
        attempt_info["outcomes"] = outcomes
        if outcomes:
            last_outcomes_by_variant[variant_idx] = outcomes

        for outcome in outcomes:
            if outcome.parent_comment_id:
                attempt_info["resolved_parent_id"] = str(outcome.parent_comment_id)
            if outcome.matched_main_text:
                attempt_info["matched_text"] = outcome.matched_main_text
            matched_author = str(getattr(outcome, "matched_parent_author", "") or "").strip()
            if matched_author:
                attempt_info["resolved_author"] = matched_author

        submitted_outcomes = [
            outcome
            for outcome in outcomes
            if _reply_outcome_submitted(outcome)
        ]
        if submitted_outcomes:
            for outcome in submitted_outcomes:
                parent_key = _reply_outcome_parent_key(outcome)
                if parent_key:
                    submitted_parent_keys.add(parent_key)
                if outcome.parent_comment_id:
                    submitted_parent_keys.add(str(outcome.parent_comment_id))
            attempt_info["replies"] = [
                _engagement_reply_outcome_to_dict(outcome, attempts=attempt_num)
                for outcome in submitted_outcomes
            ]
            attempt_info["completed"] = True
            return attempt_info

        attempt_info["error"] = (
            (outcomes[-1].error if outcomes else None)
            or "engagement_reply_parent_not_found_or_comment_list_not_open"
        )
        if _is_stop_requested():
            attempt_info["stopped"] = True
            attempt_info["error"] = "stopped_by_user"
        return attempt_info

    pending_reply_variants: List[Tuple[int, str]] = [
        (variant_idx, str(reply_text or "").strip())
        for variant_idx, reply_text in enumerate(replies, start=1)
        if str(reply_text or "").strip()
    ]
    reply_rounds: Dict[int, int] = {}
    consecutive_surface_unavailable_batches = 0
    max_surface_unavailable_batches = 2

    if dynamic_worker_pool_enabled and pending_reply_variants:
        if not _ensure_parallel_browser_capacity():
            for variant_idx, reply_text in pending_reply_variants:
                results.append(
                    _failure_reply_row(
                        reply_text,
                        variant_idx,
                        attempts=reply_rounds.get(variant_idx, 0),
                        account_id=last_reply_account_by_variant.get(variant_idx),
                        error="parallel_browser_capacity_unavailable",
                    )
                )
            pending_reply_variants = []
        else:
            active_reply_tasks: Dict[asyncio.Task, Tuple[int, str]] = {}
            inflight_reply_variants: set[int] = set()
            stop_after_dynamic = False
            consecutive_surface_unavailable_attempts = 0
            max_surface_unavailable_attempts = (
                max_surface_unavailable_batches * max(1, parallel_worker_target)
            )
            recent_surface_failures: List[Tuple[int, str, Dict[str, Any]]] = []

            async def _cancel_active_dynamic_reply_tasks() -> None:
                if not active_reply_tasks:
                    return
                for pending_task in list(active_reply_tasks):
                    pending_task.cancel()
                await asyncio.gather(*active_reply_tasks.keys(), return_exceptions=True)
                active_reply_tasks.clear()
                inflight_reply_variants.clear()

            def _append_unresolved_dynamic_reply_rows(error: str) -> None:
                reported_variants = {
                    int(row.get("variant_index") or 0)
                    for row in results
                    if isinstance(row, dict)
                }
                unresolved_items = [*active_reply_tasks.values(), *pending_reply_variants]
                for variant_idx, reply_text in unresolved_items:
                    if variant_idx in reported_variants:
                        continue
                    results.append(
                        _failure_reply_row(
                            reply_text,
                            variant_idx,
                            attempts=reply_rounds.get(variant_idx, 0),
                            account_id=last_reply_account_by_variant.get(variant_idx),
                            error=error,
                        )
                    )
                    reported_variants.add(variant_idx)

            try:
                while (
                    ((pending_reply_variants and not stop_after_dynamic) or active_reply_tasks)
                    and not _is_stop_requested()
                ):
                    while (
                        pending_reply_variants
                        and not stop_after_dynamic
                        and len(active_reply_tasks) < parallel_worker_target
                        and not _is_stop_requested()
                    ):
                        variant_idx, reply_text = pending_reply_variants.pop(0)
                        if variant_idx in inflight_reply_variants:
                            pending_reply_variants.append((variant_idx, reply_text))
                            break
                        reply_rounds[variant_idx] = reply_rounds.get(variant_idx, 0) + 1
                        task_obj = asyncio.create_task(
                            _run_reply_variant_attempt(
                                variant_idx,
                                reply_text,
                                reply_rounds[variant_idx],
                            )
                        )
                        active_reply_tasks[task_obj] = (variant_idx, reply_text)
                        inflight_reply_variants.add(variant_idx)
                        logger.debug(
                            "Task %s: engagement_only dynamic reply worker started variant=%s attempt=%s active=%s pending=%s",
                            task_id,
                            variant_idx,
                            reply_rounds[variant_idx],
                            len(active_reply_tasks),
                            len(pending_reply_variants),
                        )

                    if not active_reply_tasks:
                        break

                    done, _pending = await asyncio.wait(
                        list(active_reply_tasks.keys()),
                        timeout=0.5,
                        return_when=asyncio.FIRST_COMPLETED,
                    )
                    if not done:
                        continue

                    completed_reply_results: List[Tuple[int, str, Dict[str, Any]]] = []
                    for task_obj in done:
                        task_meta = active_reply_tasks.pop(task_obj, None)
                        if task_meta is None:
                            continue
                        variant_idx, reply_text = task_meta
                        inflight_reply_variants.discard(variant_idx)
                        try:
                            raw_result = task_obj.result()
                        except BaseException as exc:
                            if isinstance(exc, (asyncio.CancelledError, KeyboardInterrupt, SystemExit)):
                                if _is_stop_requested():
                                    continue
                                raise
                            logger.warning(
                                "Task %s: engagement_only dynamic reply variant %s task raised: %s",
                                task_id,
                                variant_idx,
                                str(exc)[:120],
                            )
                            raw_result = {
                                "variant_index": variant_idx,
                                "text": reply_text,
                                "completed": False,
                                "stopped": False,
                                "error": f"parallel_reply_exception:{str(exc)[:80]}",
                                "account_id": last_reply_account_by_variant.get(variant_idx),
                                "outcomes": [],
                                "replies": [],
                                "resolved_parent_id": None,
                                "resolved_author": None,
                                "matched_text": None,
                            }
                        completed_reply_results.append((variant_idx, reply_text, raw_result))

                    completed_reply_results.sort(
                        key=lambda row: 0 if row[2].get("completed") else 1
                    )
                    for variant_idx, reply_text, raw_result in completed_reply_results:
                        if raw_result.get("error") == temporary_reply_selection_error:
                            reply_rounds[variant_idx] = max(0, reply_rounds.get(variant_idx, 0) - 1)
                            pending_reply_variants.append((variant_idx, reply_text))
                            logger.info(
                                "Task %s: engagement_only dynamic reply variant %s deferred until a proxy/browser slot frees up",
                                task_id,
                                variant_idx,
                            )
                            if await _sleep_or_stop(0.5):
                                results.append(
                                    _stopped_reply_row(
                                        reply_text,
                                        variant_idx,
                                        attempts=reply_rounds.get(variant_idx, 0),
                                        account_id=last_reply_account_by_variant.get(variant_idx),
                                    )
                                )
                                return results, resolved_parent_id, resolved_author
                            continue

                        surface_unavailable = (
                            raw_result.get("error") == "comment_list_not_open"
                            and not raw_result.get("completed")
                            and not raw_result.get("stopped")
                        )
                        if surface_unavailable:
                            consecutive_surface_unavailable_attempts += 1
                            recent_surface_failures.append((variant_idx, reply_text, raw_result))
                        else:
                            consecutive_surface_unavailable_attempts = 0
                            recent_surface_failures = []

                        if raw_result.get("resolved_parent_id"):
                            resolved_parent_id = str(raw_result["resolved_parent_id"])
                        if raw_result.get("resolved_author"):
                            resolved_author = str(raw_result["resolved_author"])
                        if raw_result.get("matched_text"):
                            anchor_text = str(raw_result["matched_text"])

                        if raw_result.get("completed"):
                            results.extend(raw_result.get("replies") or [])
                            continue

                        if raw_result.get("stopped"):
                            results.append(
                                _stopped_reply_row(
                                    reply_text,
                                    variant_idx,
                                    attempts=max(1, reply_rounds.get(variant_idx, 0)),
                                    account_id=raw_result.get("account_id")
                                    or last_reply_account_by_variant.get(variant_idx),
                                )
                            )
                            stop_after_dynamic = True
                            pending_reply_variants = []
                            await _cancel_active_dynamic_reply_tasks()
                            continue

                        if consecutive_surface_unavailable_attempts >= max_surface_unavailable_attempts:
                            logger.warning(
                                "Task %s: stopping engagement_only reply phase after %s consecutive "
                                "dynamic attempts where no account could open the comment list",
                                task_id,
                                consecutive_surface_unavailable_attempts,
                            )
                            for failed_variant_idx, failed_reply_text, failed_result in recent_surface_failures:
                                last_outcomes = last_outcomes_by_variant.get(failed_variant_idx) or []
                                if last_outcomes:
                                    results.extend(
                                        _engagement_reply_outcome_to_dict(
                                            outcome,
                                            attempts=reply_rounds.get(failed_variant_idx, max_attempts),
                                        )
                                        for outcome in last_outcomes
                                    )
                                else:
                                    results.append(
                                        _failure_reply_row(
                                            failed_reply_text,
                                            failed_variant_idx,
                                            attempts=reply_rounds.get(failed_variant_idx, max_attempts),
                                            account_id=failed_result.get("account_id")
                                            or last_reply_account_by_variant.get(failed_variant_idx),
                                            error="comment_list_not_open",
                                        )
                                    )
                            _append_unresolved_dynamic_reply_rows("comment_list_not_open")
                            pending_reply_variants = []
                            stop_after_dynamic = True
                            await _cancel_active_dynamic_reply_tasks()
                            continue

                        can_retry = (
                            raw_result.get("error") not in {"no_free_reply_accounts", "stopped_by_user"}
                            and reply_rounds.get(variant_idx, 0) < max_attempts
                        )
                        if can_retry:
                            pending_reply_variants.append((variant_idx, reply_text))
                            continue

                        last_outcomes = last_outcomes_by_variant.get(variant_idx) or []
                        if last_outcomes:
                            results.extend(
                                _engagement_reply_outcome_to_dict(
                                    outcome,
                                    attempts=reply_rounds.get(variant_idx, max_attempts),
                                )
                                for outcome in last_outcomes
                            )
                        else:
                            results.append(
                                _failure_reply_row(
                                    reply_text,
                                    variant_idx,
                                    attempts=reply_rounds.get(variant_idx, max_attempts),
                                    account_id=raw_result.get("account_id")
                                    or last_reply_account_by_variant.get(variant_idx),
                                    error=raw_result.get("error")
                                    or "engagement_reply_parent_not_found_or_comment_list_not_open",
                                )
                            )
            finally:
                stopped_now = _is_stop_requested()
                active_when_stopped = list(active_reply_tasks.values())
                if active_reply_tasks:
                    for task_obj in active_reply_tasks:
                        task_obj.cancel()
                    await asyncio.gather(*active_reply_tasks.keys(), return_exceptions=True)
                if stopped_now:
                    for variant_idx, reply_text in [*active_when_stopped, *pending_reply_variants]:
                        results.append(
                            _stopped_reply_row(
                                reply_text,
                                variant_idx,
                                attempts=reply_rounds.get(variant_idx, 0),
                                account_id=last_reply_account_by_variant.get(variant_idx),
                            )
                        )
                await _close_parallel_phase_browsers("reply_dynamic_phase")
            pending_reply_variants = []
            if _is_stop_requested():
                return results, resolved_parent_id, resolved_author

    while pending_reply_variants:
        if _is_stop_requested():
            variant_idx, reply_text = pending_reply_variants[0]
            logger.info(
                "Task %s: engagement_only stop requested before reply variant %s",
                task_id,
                variant_idx,
            )
            results.append(
                _stopped_reply_row(
                    reply_text,
                    variant_idx,
                    attempts=reply_rounds.get(variant_idx, 0),
                    account_id=last_reply_account_by_variant.get(variant_idx),
                )
            )
            break

        batch = pending_reply_variants[:3]
        pending_reply_variants = pending_reply_variants[3:]
        for variant_idx, _reply_text in batch:
            reply_rounds[variant_idx] = reply_rounds.get(variant_idx, 0) + 1

        if not _ensure_parallel_browser_capacity():
            for variant_idx, reply_text in batch + pending_reply_variants:
                results.append(
                    _failure_reply_row(
                        reply_text,
                        variant_idx,
                        attempts=reply_rounds.get(variant_idx, 0),
                        account_id=last_reply_account_by_variant.get(variant_idx),
                        error="parallel_browser_capacity_unavailable",
                    )
                )
            break

        try:
            raw_batch_results = await asyncio.gather(
                *[
                    _run_reply_variant_attempt(
                        variant_idx,
                        reply_text,
                        reply_rounds[variant_idx],
                    )
                    for variant_idx, reply_text in batch
                ],
                return_exceptions=True,
            )
        finally:
            await _close_parallel_phase_browsers("reply_batch")

        retry_variants: List[Tuple[int, str]] = []
        stop_after_batch = False
        batch_surface_unavailable_flags: List[bool] = []
        for (variant_idx, reply_text), raw_result in zip(batch, raw_batch_results):
            if isinstance(raw_result, BaseException):
                if isinstance(raw_result, (asyncio.CancelledError, KeyboardInterrupt, SystemExit)):
                    raise raw_result
                logger.warning(
                    "Task %s: engagement_only parallel reply variant %s batch task raised: %s",
                    task_id,
                    variant_idx,
                    str(raw_result)[:120],
                )
                raw_result = {
                    "variant_index": variant_idx,
                    "text": reply_text,
                    "completed": False,
                    "stopped": False,
                    "error": f"parallel_reply_exception:{str(raw_result)[:80]}",
                    "account_id": last_reply_account_by_variant.get(variant_idx),
                    "outcomes": [],
                    "replies": [],
                    "resolved_parent_id": None,
                    "resolved_author": None,
                    "matched_text": None,
                }

            batch_surface_unavailable_flags.append(
                raw_result.get("error") == "comment_list_not_open"
                and not raw_result.get("completed")
                and not raw_result.get("stopped")
            )

            if raw_result.get("resolved_parent_id"):
                resolved_parent_id = str(raw_result["resolved_parent_id"])
            if raw_result.get("resolved_author"):
                resolved_author = str(raw_result["resolved_author"])
            if raw_result.get("matched_text"):
                anchor_text = str(raw_result["matched_text"])

            if raw_result.get("completed"):
                results.extend(raw_result.get("replies") or [])
                continue

            if raw_result.get("error") == temporary_reply_selection_error:
                reply_rounds[variant_idx] = max(0, reply_rounds.get(variant_idx, 0) - 1)
                retry_variants.append((variant_idx, reply_text))
                logger.info(
                    "Task %s: engagement_only reply variant %s deferred until a proxy/browser slot frees up",
                    task_id,
                    variant_idx,
                )
                continue

            if raw_result.get("stopped"):
                results.append(
                    _stopped_reply_row(
                        reply_text,
                        variant_idx,
                        attempts=max(1, reply_rounds.get(variant_idx, 0)),
                        account_id=raw_result.get("account_id")
                        or last_reply_account_by_variant.get(variant_idx),
                    )
                )
                stop_after_batch = True
                continue

            can_retry = (
                raw_result.get("error") not in {"no_free_reply_accounts", "stopped_by_user"}
                and reply_rounds.get(variant_idx, 0) < max_attempts
            )
            if can_retry:
                retry_variants.append((variant_idx, reply_text))
                continue

            last_outcomes = last_outcomes_by_variant.get(variant_idx) or []
            if last_outcomes:
                results.extend(
                    _engagement_reply_outcome_to_dict(
                        outcome,
                        attempts=reply_rounds.get(variant_idx, max_attempts),
                    )
                    for outcome in last_outcomes
                )
            else:
                results.append(
                    _failure_reply_row(
                        reply_text,
                        variant_idx,
                        attempts=reply_rounds.get(variant_idx, max_attempts),
                        account_id=raw_result.get("account_id")
                        or last_reply_account_by_variant.get(variant_idx),
                        error=raw_result.get("error")
                        or "engagement_reply_parent_not_found_or_comment_list_not_open",
                    )
                )

        if batch_surface_unavailable_flags and all(batch_surface_unavailable_flags):
            consecutive_surface_unavailable_batches += 1
        else:
            consecutive_surface_unavailable_batches = 0

        if consecutive_surface_unavailable_batches >= max_surface_unavailable_batches:
            logger.warning(
                "Task %s: stopping engagement_only reply phase after %s consecutive "
                "parallel batches where no account could open the comment list",
                task_id,
                consecutive_surface_unavailable_batches,
            )
            for variant_idx, reply_text in retry_variants:
                last_outcomes = last_outcomes_by_variant.get(variant_idx) or []
                if last_outcomes:
                    results.extend(
                        _engagement_reply_outcome_to_dict(
                            outcome,
                            attempts=reply_rounds.get(variant_idx, max_attempts),
                        )
                        for outcome in last_outcomes
                    )
                else:
                    results.append(
                        _failure_reply_row(
                            reply_text,
                            variant_idx,
                            attempts=reply_rounds.get(variant_idx, max_attempts),
                            account_id=last_reply_account_by_variant.get(variant_idx),
                            error="comment_list_not_open",
                        )
                    )
            retry_variants = []
            pending_reply_variants = []
            break

        if stop_after_batch:
            break

        if retry_variants:
            pending_reply_variants = retry_variants + pending_reply_variants
            if await _sleep_or_stop(random.uniform(3.0, 7.0)):
                variant_idx, reply_text = retry_variants[0]
                results.append(
                    _stopped_reply_row(
                        reply_text,
                        variant_idx,
                        attempts=max(1, reply_rounds.get(variant_idx, 0)),
                        account_id=last_reply_account_by_variant.get(variant_idx),
                    )
                )
                break

    results.sort(key=lambda item: int(item.get("variant_index") or 0))

    return results, resolved_parent_id, resolved_author


def _engagement_reply_outcome_to_dict(outcome: Any, *, attempts: int) -> Dict[str, Any]:
    parent_confirmed = bool(outcome.matched_main_text or outcome.parent_comment_id)
    submitted_unconfirmed = bool(getattr(outcome, "submitted_unconfirmed", False))
    return {
        "text": outcome.text,
        "success": bool(outcome.success),
        "method": outcome.method,
        "error": outcome.error,
        "reply_id": None,
        "http_error_type": None,
        "parent_confirmed": parent_confirmed,
        "parent_confirmation_level": "ui_text_exact" if parent_confirmed else "unconfirmed",
        "visibility_status": (
            "network_confirmed"
            if outcome.success
            else ("submitted_unconfirmed" if submitted_unconfirmed else "not_checked")
        ),
        "local_visible": True if outcome.success else None,
        "verify_mode": "full_flow_dom_parent",
        "attempts": attempts,
        "fallback_used": False,
        "warmup_reason": None,
        "submitted_unconfirmed": submitted_unconfirmed,
        "parent_comment_id": outcome.parent_comment_id,
        "matched_main_text": outcome.matched_main_text,
        "requested_parent_author": getattr(outcome, "requested_parent_author", None),
        "matched_parent_author": getattr(outcome, "matched_parent_author", None),
        "parent_author_match_mode": getattr(outcome, "parent_author_match_mode", None),
        "author_fallback_used": bool(getattr(outcome, "author_fallback_used", False)),
        "author_fallback_reason": getattr(outcome, "author_fallback_reason", None),
        "account_id": outcome.account_id,
        "variant_index": outcome.variant_index,
    }


async def _order_boost_likes(
    video_url: str,
    comment_author_username: Optional[str],
    task_id: Optional[int] = None,
    likes_enabled: bool = True,
    likes_quantity: Optional[int] = None,
) -> Tuple[Optional[int], Optional[str]]:
    from services.boost_project_client import order_tiktok_comment_likes, is_boost_enabled
    if not likes_enabled:
        logger.info(f"Task {task_id}: Likes disabled in settings, skipping likes order")
        return None, None
    if not is_boost_enabled():
        logger.debug("Likes provider disabled (no API key), skipping likes order")
        return None, None
    if not comment_author_username or not str(comment_author_username).strip():
        err = "comment_author_username empty, cannot order likes"
        logger.warning(f"Task {task_id}: {err}")
        return None, err
    canonical = build_canonical_video_url(video_url)
    ok, order_id, err = await order_tiktok_comment_likes(
        canonical,
        str(comment_author_username).strip(),
        quantity=likes_quantity,
    )
    if ok:
        logger.info(
            f"Task {task_id}: Likes order created: "
            f"order_id={order_id}, quantity={likes_quantity or coerce_likes_quantity(settings.BOOST_PROJECT_LIKES_QUANTITY)}"
        )
        return order_id, None
    logger.warning(f"Task {task_id}: Likes order failed: {err}")
    return None, err or "Unknown error"


def _record_single_boost_order(
    result: EngagementResult,
    *,
    username: Optional[str],
    order_id: Optional[Any],
    error: Optional[str],
    quantity: int,
    parent_text: Optional[str] = None,
    parent_key: Optional[str] = None,
    selection_reason: str = "single_comment",
) -> None:
    clean_username = str(username or "").strip().lstrip("@")
    if not clean_username:
        return
    result.boost_orders = [
        {
            "order_id": order_id,
            "username": clean_username,
            "quantity": quantity,
            "ok": order_id is not None,
            "error": error,
            "reply_count": sum(1 for reply in result.replies if reply.get("success") or reply.get("submitted_unconfirmed")),
            "parent_key": str(parent_key or "").strip() or None,
            "parent_text": str(parent_text or "").strip() or None,
            "selection_reason": selection_reason,
        }
    ]


def _engagement_only_replies_confirm_parent(replies: List[Dict[str, Any]]) -> bool:
    """Return True when engagement-only replies proved the parent exists.

    A successful reply is enough. A failed reply can also be useful evidence if
    the DOM parent was found but the publish step failed later.
    """
    for reply in replies or []:
        if not isinstance(reply, dict):
            continue
        if reply.get("success"):
            return True
        if reply.get("parent_confirmed"):
            return True
    return False


async def execute_hybrid_full(
    db: Session,
    task: Task,
    video_url: str,
    text_choice: str,
    task_id: int,
    *,
    pool_owner_user_id: Optional[int] = None,
) -> EngagementResult:
    result = EngagementResult()
    boost_enabled, boost_quantity = _get_task_boost_config(db, task)
    result.boost_enabled = boost_enabled
    result.boost_quantity = boost_quantity
    
    logger.info(f"Task {task_id}: Starting HYBRID FULL for {video_url[:50]}")
    resolved_video_url, resolved_video_id = await resolve_video_url(video_url)
    if resolved_video_url != video_url:
        logger.info(
            "Task %s: resolved video URL for HYBRID FULL: %s -> %s",
            task_id,
            video_url[:80],
            resolved_video_url[:120],
        )
        video_url = resolved_video_url
    elif resolved_video_id:
        video_url = resolved_video_url
    
    max_attempts = max(1, settings.MAX_RETRY_ACCOUNTS)
    tried_account_ids: List[int] = []
    reserved_main_account_ids: set[int] = set()
    
    main_comment_id = None
    last_error = None

    replies_to_post: List[str] = _reply_variants_from_task(task)

    def _release_reserved_main_accounts() -> None:
        for account_id in list(reserved_main_account_ids):
            try:
                release_main_account_reservation(account_id)
            finally:
                reserved_main_account_ids.discard(account_id)
    
    for attempt_num in range(1, max_attempts + 1):
        candidate = get_free_account_for_main_comment(
            db,
            exclude_ids=tried_account_ids,
            pool_owner_user_id=pool_owner_user_id,
        )
        if not candidate:
            if not last_error:
                last_error = "out_of_accounts"
            logger.warning(
                f"Task {task_id}: No free accounts available for comment "
                f"(last_error={last_error})"
            )
            break
            
        tried_account_ids.append(candidate.id)
        reserved_main_account_ids.add(candidate.id)
        
        logger.info(f"Task {task_id}: Attempt {attempt_num}/{max_attempts} on account {candidate.id}")
        
        # Pre-record which text variant we sent. Doing it BEFORE the call
        # ensures task.settings.url_results carries this even for failures
        # (helpful when diagnosing "did we use a shadowed text?").
        result.main_comment["text_choice"] = text_choice
        comment_id, err, stage = await attempt_main_comment_on_account(
            db,
            candidate,
            video_url,
            text_choice,
            task_id,
            correlation_id=f"task_{task_id}_main_a{attempt_num}",
            outcome_dict=result.main_comment,
        )
        
        local_no_id_accepted = (
            stage == "hybrid_local_no_id"
            and err is None
            and bool(result.main_comment.get("posted"))
            and bool(result.main_comment.get("local_confirmed"))
        )

        if comment_id:
            if not replies_to_post:
                main_comment_id = comment_id
                result.main_comment["comment_id"] = main_comment_id
                result.main_comment["verify_mode"] = "skipped_no_replies"
                result.comment_id = main_comment_id
                result.comment_author = candidate.label
                result.comment_found = True
                result.main_comment["success"] = True
                result.main_comment["verified"] = False
                result.main_comment["submitted_unconfirmed"] = True
                result.main_comment["error"] = "main_submitted_unconfirmed_manual_check"
                logger.info(
                    "Task %s: Main comment accepted without public DOM verification "
                    "(no replies planned). Marking as submitted_unconfirmed for manual check.",
                    task_id,
                )
                break

            # Single verify_comment_dom call; the function internally cycles
            # through up to len(retry_schedule) viewer accounts with growing
            # pauses (fast precheck on the first attempt, scrolling verify on
            # the rest). Replaces the previous 3-stage ladder which could
            # spend up to MAX_RETRY_ACCOUNTS x 3 viewer accounts on a single
            # main comment.
            verified, verified_id, verified_author, verify_mode = await verify_comment_dom(
                db=db,
                video_url=video_url,
                comment_text=text_choice,
                expected_comment_id=comment_id,
                expected_author=candidate.label,
                task_id=task_id,
                prefer_fast_path=True,
                exclude_account_ids=[candidate.id],
                retry_schedule=[0.0, 5.0, 15.0, 30.0],
                pool_owner_user_id=pool_owner_user_id,
            )
            final_verify_mode = verify_mode or "unknown"

            main_comment_id = verified_id or comment_id
            result.main_comment["comment_id"] = main_comment_id
            result.main_comment["verify_mode"] = final_verify_mode
            result.comment_id = main_comment_id
            result.comment_author = verified_author or candidate.label
            result.comment_found = True
            result.main_comment["success"] = True

            if verified:
                result.main_comment["verified"] = True
                result.main_comment["submitted_unconfirmed"] = False
            else:
                # Publisher returned a comment_id but the DOM viewer failed to
                # observe it within ~50 seconds. Common reasons:
                #   - TikTok renders our own comment lazily for other viewers
                #   - viewer accounts hit shadowban / rate-limit and see a
                #     stale list
                #   - the publish path was actually rejected (rare when we
                #     have a real comment_id from the network response)
                # We do NOT abort here: the first reply call below acts as a
                # smart-check. The HTTP reply API responds with a precise
                # "comment doesn't exist" status when the parent is missing,
                # which is the only signal that *truly* invalidates the main
                # comment. If the reply succeeds, we know the parent was
                # really posted and DOM was just slow.
                result.main_comment["verified"] = False
                result.main_comment["submitted_unconfirmed"] = True
                logger.warning(
                    f"Task {task_id}: Main comment optimistically accepted "
                    f"(comment_id={main_comment_id[:20]}, verify_mode={final_verify_mode}). "
                    f"DOM viewer could not see it; first reply will smart-check parent existence."
                )

            break

        if local_no_id_accepted:
            if not replies_to_post:
                result.main_comment["success"] = True
                result.main_comment["verified"] = False
                result.main_comment["submitted_unconfirmed"] = True
                result.main_comment["error"] = "main_submitted_unconfirmed_manual_check"
                result.main_comment["verify_mode"] = "skipped_no_replies_local_no_id"
                result.comment_author = candidate.label
                result.comment_found = True
                logger.info(
                    "Task %s: Local no-id main comment accepted without public DOM "
                    "verification (no replies planned). Marking as submitted_unconfirmed "
                    "for manual check.",
                    task_id,
                )
                break

            logger.warning(
                "Task %s: Main comment was found locally after an ambiguous publish, "
                "but TikTok did not expose comment_id in DOM. Running strict "
                "public verification before accepting it.",
                task_id,
            )
            verified, verified_id, verified_author, verify_mode = await verify_comment_dom(
                db=db,
                video_url=video_url,
                comment_text=text_choice,
                expected_comment_id=None,
                expected_author=candidate.label,
                task_id=task_id,
                prefer_fast_path=False,
                exclude_account_ids=[candidate.id],
                retry_schedule=[5.0, 15.0, 30.0],
                pool_owner_user_id=pool_owner_user_id,
                require_author_match=True,
            )
            result.main_comment["verify_mode"] = verify_mode or "local_no_id_public_verify"
            if verified:
                result.main_comment["success"] = True
                result.main_comment["verified"] = True
                result.main_comment["submitted_unconfirmed"] = False
                result.comment_author = verified_author or candidate.label
                result.comment_found = True
                if verified_id:
                    main_comment_id = verified_id
                    result.main_comment["comment_id"] = verified_id
                    result.main_comment["local_confirmation_without_id"] = False
                    result.comment_id = verified_id
                logger.info(
                    "Task %s: Local no-id main comment was verified publicly "
                    "(comment_id=%s, author=%s).",
                    task_id,
                    str(verified_id)[:20] if verified_id else "None",
                    result.comment_author,
                )
            else:
                result.main_comment["success"] = False
                result.main_comment["posted"] = False
                result.main_comment["verified"] = False
                result.main_comment["submitted_unconfirmed"] = False
                result.main_comment["error"] = "local_dom_only_not_publicly_verified"
                result.comment_author = candidate.label
                result.comment_found = False
                logger.warning(
                    "Task %s: Local no-id main comment was not visible from "
                    "independent verification accounts; treating as failed "
                    "instead of successful submitted_unconfirmed.",
                    task_id,
                )
            break

        last_error = err or "unknown"
        logger.warning(
            f"Task {task_id}: Attempt {attempt_num} failed on account {candidate.id} "
            f"at stage={stage}: {last_error}"
        )
        release_main_account_reservation(candidate.id)
        reserved_main_account_ids.discard(candidate.id)

        if str(last_error).startswith("captcha_challenge:") or "after_captcha_clear" in str(last_error):
            logger.warning(
                "Task %s: Stopping main-comment retries after captcha-related result "
                "(%s) to avoid burning more accounts.",
                task_id,
                last_error,
            )
            break

    main_local_confirmed_without_id = bool(
        result.main_comment.get("posted")
        and result.main_comment.get("local_confirmed")
        and result.main_comment.get("local_confirmation_without_id")
    )

    main_accepted_without_id = (
        main_local_confirmed_without_id
        and bool(result.main_comment.get("success"))
    )

    if not main_comment_id and not main_accepted_without_id:
        result.main_comment["error"] = (
            result.main_comment.get("error")
            or last_error
            or "Main comment failed"
        )
        logger.warning(
            f"Task {task_id}: Main comment FAILED after {len(tried_account_ids)} accounts: "
            f"{result.main_comment['error']}"
        )
        _release_reserved_main_accounts()
        return result

    can_reply_by_dom_parent = bool(
        replies_to_post
        and main_local_confirmed_without_id
        and not result.comment_id
        and result.main_comment.get("verified")
        and result.comment_author
        and text_choice
    )

    if (
        replies_to_post
        and main_local_confirmed_without_id
        and not result.comment_id
        and not can_reply_by_dom_parent
    ):
        logger.warning(
            "Task %s: Main comment is locally confirmed but comment_id is missing; "
            "cannot safely attach replies. Marking replies as skipped failures.",
            task_id,
        )
        result.replies = [
            {
                "success": False,
                "text": reply_text,
                "error": "main_comment_id_missing_after_local_recovery",
                "method": "skipped",
                "visibility_status": "skipped",
            }
            for reply_text in replies_to_post
        ]
        result.main_comment["error"] = "main_comment_id_missing_after_local_recovery"
        _release_reserved_main_accounts()
        return result

    if replies_to_post and (result.comment_id or can_reply_by_dom_parent):
        if can_reply_by_dom_parent:
            logger.warning(
                "Task %s: Main comment is publicly verified but comment_id is missing; "
                "posting replies via DOM parent selection by text and author=%s.",
                task_id,
                result.comment_author,
            )
        # parent_unconfirmed=True is set when DOM could not visually observe
        # the main comment. execute_replies_http will treat the FIRST reply
        # as a smart-check: if the HTTP reply API rejects with a parent-not-
        # found signal, we mark the whole task as a real failure rather
        # than spam unrelated comments.
        result.replies = await execute_replies_http(
            db=db,
            video_url=video_url,
            parent_comment_id=result.comment_id or "",
            replies=replies_to_post,
            task_id=task_id,
            main_comment_text=text_choice,
            parent_unconfirmed=bool(result.main_comment.get("submitted_unconfirmed")),
            parent_author=result.comment_author,
            pool_owner_user_id=pool_owner_user_id,
        )

        # If the smart check resolved to "parent doesn't exist", the first
        # reply will have its error set to a parent-not-found marker. In
        # that case rewrite the main comment outcome so downstream logic
        # treats this task as a failure (no boost, no successful counter).
        first_reply = next(iter(result.replies), None) if result.replies else None
        if (
            first_reply
            and not first_reply.get("success")
            and first_reply.get("http_error_type") == "parent_not_found"
        ):
            logger.warning(
                f"Task {task_id}: Smart-check determined main comment is missing "
                f"(comment_id={result.comment_id[:20] if result.comment_id else 'None'}). "
                f"Aborting boost and marking task as failed."
            )
            result.main_comment["success"] = False
            result.main_comment["posted"] = False
            result.main_comment["verified"] = False
            result.main_comment["network_confirmed"] = False
            result.main_comment["local_confirmed"] = False
            result.main_comment["submitted_unconfirmed"] = False
            result.main_comment["error"] = "main_invalidated_by_smart_check"
            result.comment_found = False
            _release_reserved_main_accounts()
            return result

    if boost_enabled and not replies_to_post:
        result.boost_enabled = False
        result.boost_error = None
        logger.info(
            "Task %s: Likes skipped because this full order has no replies.",
            task_id,
        )
    elif boost_enabled and not result.main_comment.get("verified"):
        result.boost_enabled = False
        result.boost_error = None
        logger.info(
            "Task %s: Likes skipped because main comment is not publicly verified.",
            task_id,
        )
    elif boost_enabled and replies_to_post and not any(reply.get("success") for reply in result.replies):
        result.boost_enabled = False
        result.boost_error = None
        logger.info(
            "Task %s: Likes skipped because full order published no replies.",
            task_id,
        )
    else:
        result.boost_order_id, result.boost_error = await _order_boost_likes(
            video_url=video_url,
            comment_author_username=result.comment_author,
            task_id=task_id,
            likes_enabled=boost_enabled,
            likes_quantity=boost_quantity,
        )
        _record_single_boost_order(
            result,
            username=result.comment_author,
            order_id=result.boost_order_id,
            error=result.boost_error,
            quantity=boost_quantity,
            parent_text=result.main_comment.get("text_choice"),
            parent_key=result.main_comment.get("comment_id"),
            selection_reason="single_comment",
        )

    _release_reserved_main_accounts()
    return result


async def execute_engagement_only(
    db: Session,
    task: Task,
    video_url: str,
    comment_id: Optional[str] = None,
    username: Optional[str] = None,
    comment_text: Optional[str] = None,
    task_id: int = 0,
    *,
    pool_owner_user_id: Optional[int] = None,
    stop_requested: Optional[Callable[[], bool]] = None,
) -> EngagementResult:
    """
    Выполняет ENGAGEMENT ONLY:
    - Ответы на существующий комментарий через HTTP
    - Лайки через API лайков (количество из настроек), после ответов
    
    Args:
        db: Сессия БД
        task: Задача
        video_url: URL видео
        comment_id: Опциональный ID комментария
        username: Опциональное имя автора комментария
        comment_text: Опциональный текст комментария
        task_id: ID задачи
        
    Returns:
        EngagementResult: Результат выполнения
    """
    result = EngagementResult()
    boost_enabled, boost_quantity = _get_task_boost_config(db, task)
    result.boost_enabled = boost_enabled
    result.boost_quantity = boost_quantity
    
    logger.info(f"Task {task_id}: Starting ENGAGEMENT ONLY for {video_url[:50]}")

    # Safety: comment_text is the anchor for finding the Reply button in the DOM.
    # Missing text → browser falls back to comment ID attribute only (less reliable).
    if not comment_text:
        logger.warning(
            "Task %s: execute_engagement_only — comment_text is empty. "
            "Browser will rely solely on comment ID attribute to locate Reply button. "
            "Set engagement_comment_text in task settings or task.main_comment for better reliability.",
            task_id,
        )

    # ========== REPLIES (HTTP) ==========
    replies_to_post = _reply_variants_from_task(task)
    likes_ordered_upfront = False

    initial_comment_id = str(comment_id or "").strip()
    initial_author = (username or "").strip() or None
    if replies_to_post and not initial_comment_id and not (comment_text or "").strip():
        result.main_comment["error"] = "comment_found_but_reply_anchor_incomplete"
        logger.error(
            "Task %s: Comment was found for engagement_only, but replies cannot "
            "be anchored because both comment_id and comment_text are missing. "
            "has_text=%s, author=%s",
            task_id,
            bool(comment_text),
            initial_author,
        )
        return result

    # For engagement-only we do not publish a main comment. Treat the provided
    # text/ID as the parent anchor, then let the full-order reply flow prove it
    # by opening the comments drawer and matching the parent before each reply.
    result.comment_found = bool((comment_text or "").strip() or initial_comment_id)
    result.comment_id = initial_comment_id or None
    result.comment_author = initial_author

    if replies_to_post:
        try:
            stop_before_replies = bool(stop_requested and stop_requested())
        except Exception as stop_error:
            logger.debug("Task %s: engagement_only stop check failed: %s", task_id, stop_error)
            stop_before_replies = False
        if stop_before_replies:
            result.replies.append({
                "text": replies_to_post[0],
                "success": False,
                "method": None,
                "error": "stopped_by_user",
                "reply_id": None,
                "http_error_type": None,
                "parent_confirmed": False,
                "parent_confirmation_level": "unconfirmed",
                "visibility_status": "not_checked",
                "local_visible": None,
                "verify_mode": "full_flow_dom_parent",
                "attempts": 0,
                "fallback_used": False,
                "warmup_reason": None,
                "parent_comment_id": result.comment_id,
                "matched_main_text": None,
                "account_id": None,
                "variant_index": 1,
            })
            result.boost_enabled = False
            return result

        if boost_enabled and initial_author:
            # Likes-first: автор целевого комментария известен заранее, поэтому
            # заказываем лайки ДО фазы ответов — комментарий поднимается выше в
            # списке и reply-аккаунты находят его быстрее.
            result.boost_order_id, result.boost_error = await _order_boost_likes(
                video_url=video_url,
                comment_author_username=initial_author,
                task_id=task_id,
                likes_enabled=boost_enabled,
                likes_quantity=boost_quantity,
            )
            _record_single_boost_order(
                result,
                username=initial_author,
                order_id=result.boost_order_id,
                error=result.boost_error,
                quantity=boost_quantity,
                parent_text=comment_text,
                parent_key=initial_comment_id or None,
                selection_reason="engagement_parent",
            )
            likes_ordered_upfront = True

        logger.info(
            "Task %s: Executing %s engagement_only replies via full-order hybrid flow",
            task_id,
            len(replies_to_post),
        )
        result.replies, resolved_comment_id, resolved_author = await execute_engagement_only_replies_via_full_flow(
            db=db,
            video_url=video_url,
            comment_text=comment_text or "",
            comment_id=initial_comment_id or None,
            comment_author=initial_author,
            replies=replies_to_post,
            task_id=task_id,
            pool_owner_user_id=pool_owner_user_id,
            stop_requested=stop_requested,
        )
        if resolved_comment_id:
            result.comment_id = resolved_comment_id
        if resolved_author:
            result.comment_author = resolved_author
    else:
        logger.info(f"Task {task_id}: No replies to post")
    
    # ========== LIKES через API лайков ==========
    if likes_ordered_upfront:
        # Лайки уже заказаны до фазы ответов (автор был известен заранее).
        pass
    elif boost_enabled and not replies_to_post:
        result.boost_enabled = False
        result.boost_error = None
        logger.info(
            "Task %s: Likes skipped because engagement_only order has no replies.",
            task_id,
        )
    elif (
        boost_enabled
        and replies_to_post
        and not _engagement_only_replies_confirm_parent(result.replies)
    ):
        result.boost_enabled = False
        result.boost_error = None
        result.warnings.append(
            {
                "code": "likes_skipped_parent_not_confirmed",
                "message": "Comment likes were skipped because reply accounts did not confirm the parent comment",
            }
        )
        logger.info(
            "Task %s: Likes skipped because engagement_only replies did not confirm the parent comment.",
            task_id,
        )
    else:
        result.boost_order_id, result.boost_error = await _order_boost_likes(
            video_url=video_url,
            comment_author_username=result.comment_author,
            task_id=task_id,
            likes_enabled=boost_enabled,
            likes_quantity=boost_quantity,
        )
        _record_single_boost_order(
            result,
            username=result.comment_author,
            order_id=result.boost_order_id,
            error=result.boost_error,
            quantity=boost_quantity,
            parent_text=comment_text,
            parent_key=result.comment_id,
            selection_reason="engagement_parent",
        )

    return result
