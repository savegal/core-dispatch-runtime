"""User-schema workflow for HYBRID FULL orders.

Реализация схемы, описанной владельцем продукта:

  Главный аккаунт:
    1. Открыть страницу видео и быстро (≤ 2 c) проверить визуально, что
       зашли как пользователь (есть логин-маркеры, нет кнопки Login, URL
       ведёт на /video/).
       - Куки мёртвые → сразу следующий аккаунт.
    2. Если ок — пишет ВСЕ варианты главного коммента ПОДРЯД на одном и
       том же аккаунте (без перезахода). Между публикациями — небольшая
       пауза.
    3. Если ни одна публикация на этом аккаунте не вышла — следующий main-
       аккаунт пробует то же самое.

  Reply-аккаунты:
    1. Берётся reply-аккаунт, делается та же быстрая визуальная проверка.
    2. Аккаунт ищет в DOM ЛЮБОЙ из опубликованных вариантов главного
       коммента по тексту (без сравнения автора — DOM редко отдаёт его
       стабильно). Найденным комментам отвечает текущим вариантом ответа.
       На каждый найденный коммент — один ответ.
    3. Следующий reply-аккаунт делает то же самое, но уже следующим
       вариантом ответа.

  Если ни один reply-аккаунт не нашёл ни одного главного коммента, и
  при этом из main-фазы тоже ничего не подтвердилось, СЕРВЕР сам не
  знает, опубликован коммент или нет. Включаем fallback:
    - С первого reply-аккаунта (того, что уже залогинен) пишутся все 3
      главных коммента. После этого reply-сценарий завершается без
      ответов: цель fallback — хотя бы донести главные комменты до
      площадки.

  Лайки:
    - Заказываются ТОЛЬКО если reply-аккаунт реально нашёл хоть один
      главный коммент в DOM и известен реальный TikTok username автора.
    - Никогда не используется account.label (это внутренний technical
      id, не TikTok username).

Ответы не верифицируются другими аккаунтами. Главный коммент тоже не
гоняется через 3 наблюдательных аккаунта — найденный в DOM считается
опубликованным.
"""
from __future__ import annotations

import asyncio
import json
import logging
import random
import threading
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional, Tuple

from patchright.async_api import Page
from sqlalchemy.orm import Session

from db.models import Account, Task
from config.settings import get_settings
from executor.tiktok_client_async import create_page_for_account
from executor.session_bootstrap import (
    extract_tiktok_username,
    get_browser_login_state,
)
from executor.publish_action import (
    PublishResult,
    extract_tiktok_media_id,
    is_on_expected_media_url,
    publish_comment_hybrid,
    publish_reply_hybrid,
)
from executor.dom_actions import (
    dismiss_cookie_banners,
    get_comment_surface_state,
    open_comment_section,
    suppress_keyboard_shortcuts_overlay,
)
from executor.comment_outcome import find_comment_in_list, find_comments_in_list_batch, normalize_author
# tt_selectors здесь больше не нужен — handle главного коммента не используется
# в reply-флоу (publish_reply_hybrid сам ищет родителя по тексту/cid).
from executor.cookie_health import (
    CookieHealth,
    CookieVerdict,
    probe_cookie_health,
    register_alive_session,
    register_ambiguous_session,
    register_blocked_session,
    register_captcha_session,
    register_dead_session,
)
from executor.captcha_detector import detect_captcha
from services.account_service import (
    RUNTIME_CATEGORY_COOKIE_ENRICHMENT,
    get_free_account_for_main_comment,
    get_free_account_for_reply,
    mark_runtime_cooldown,
    mark_needs_reauth,
    mark_soft_error,
    record_main_comment_success,
    record_reply_success,
    release_main_account_reservation,
)
from services.proxy_health_service import clear_proxy_bad, mark_proxy_bad
from executor.cookie_enricher import (
    enrich_cookies_via_browser,
    needs_enrichment,
    save_enriched_cookies_to_db,
)
from executor.behavioral_warmup import like_random_comment_before_action
from services.cookie_persistence_service import save_cookies_after_action

logger = logging.getLogger("executor.user_schema")
_settings = get_settings()


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
                for idx in range(4, int(_settings.MAX_REPLIES) + 1)
            ],
        ]

    replies = [
        str(reply).strip()
        for reply in raw_replies
        if str(reply or "").strip()
    ][: int(_settings.MAX_REPLIES)]
    reply_count = task_settings.get("reply_count")
    if isinstance(reply_count, int) and reply_count >= 0:
        replies = replies[:reply_count]
    return replies

# Между двумя reply на одном аккаунте.
_INTER_REPLY_DELAY_RANGE = (3.0, 6.0)

# Между главными вариантами на одном аккаунте.
_INTER_MAIN_VARIANT_DELAY_RANGE = (4.0, 7.0)

# Сколько один reply-аккаунт ищет главные комменты по текстам перед тем,
# как считаем этот reply-вариант не найденным и переходим к следующему аккаунту.
_REPLY_MAIN_SEARCH_TIMEOUT_SECONDS = 60.0
_REPLY_MAIN_SEARCH_PER_TEXT_SECONDS = 8.0
_REPLY_MAIN_SEARCH_SCROLLS = 8
_REPLY_MAIN_SEARCH_DEEP_TIMEOUT_SECONDS = 90.0
_REPLY_MAIN_SEARCH_DEEP_PER_TEXT_SECONDS = 24.0
_REPLY_MAIN_SEARCH_DEEP_SCROLLS = 16
_REPLY_MAIN_SEARCH_SCROLL_DELAY_MS = 500
_REPLY_PARENT_SEARCH_EMPTY_COMPATIBLE_ERRORS = {
    "batch_search_timeout",
    "single_search_timeout",
}
_OBSERVER_MAIN_CONFIRM_ATTEMPTS = 2
_OBSERVER_MAIN_PROPAGATION_DELAY_RANGE = (4.0, 7.0)
_MAIN_PUBLIC_CONFIRM_ROUNDS = 2
_MAIN_PUBLIC_REPAIR_ATTEMPTS_PER_ROUND = 2
_OBSERVER_OPEN_NEGATIVE_OUTCOMES = {
    "observer_open_list_no_target",
    "observer_open_list_empty",
}
_OBSERVER_INPUT_ONLY_EMPTY_OUTCOME = "observer_open_input_only_empty"
_OBSERVER_INPUT_ONLY_EMPTY_CONFIRMATIONS = 2
_OBSERVER_INCONCLUSIVE_OUTCOME_PREFIXES = (
    "observer_comment_list_not_open",
    _OBSERVER_INPUT_ONLY_EMPTY_OUTCOME,
    "observer_redirected_or_dead",
    "observer_wrong_url",
    "observer_no_main_found",
    "page_create_failed:",
    "dead_session_during_enrichment",
    "enrichment_failed:",
    "enrichment_exception:",
    "post_enrichment_return_failed:",
    "dead_session:",
    "login_ambiguous:",
    "captcha_challenge:",
    "captcha_challenge_timeout:",
    "blocked_ui:",
)
_MAX_REPLY_FALLBACK_MAIN_ATTEMPTS = max(
    2,
    int(getattr(_settings, "MAX_RETRY_ACCOUNTS", 30) or 30),
)

# Consecutive reply-accounts allowed to attempt fallback-main without
# producing any confirmed mains before we conclude the Post button is
# persistently disabled for this video. Each such attempt burns one
# reply-account, so we cap the streak instead of cycling all 30.
_MAX_DISABLED_POST_FALLBACK_STREAK = 20
_ENRICHMENT_TIMEOUT_SECONDS = 22.0
_ENRICHMENT_FAILURE_COOLDOWN_MINUTES = 6 * 60
_ACTION_READY_PROBE_SECONDS = 8.0
_CAPTCHA_MANUAL_WAIT_SECONDS = 30.0
_CAPTCHA_MANUAL_POLL_SECONDS = 2.0
_HTTP_PREFLIGHT_VALID_CACHE_TTL_SECONDS = 45.0
_HTTP_PREFLIGHT_VALID_CACHE: Dict[Any, Tuple[float, str]] = {}
_HTTP_PREFLIGHT_VALID_CACHE_LOCK = threading.RLock()

# Пауза между неудачными retry-попытками с разными аккаунтами на одном видео.
# Anti-flood: TikTok видит всплеск активности на одном видео, если 30 разных
# аккаунтов подряд за секунды попробуют опубликовать — рейт-лимит сработает.
_RETRY_ACCOUNT_DELAY_RANGE = (3.0, 10.0)


def _reply_parent_search_error_is_inconclusive(error: Any) -> bool:
    if not error:
        return False
    normalized = str(error)
    return normalized not in _REPLY_PARENT_SEARCH_EMPTY_COMPATIBLE_ERRORS


def _http_preflight_cache_key(account: Account) -> Any:
    account_id = getattr(account, "id", None)
    return account_id if account_id is not None else id(account)


def _has_recent_http_preflight_valid(account: Account) -> bool:
    cache_key = _http_preflight_cache_key(account)
    with _HTTP_PREFLIGHT_VALID_CACHE_LOCK:
        cached = _HTTP_PREFLIGHT_VALID_CACHE.get(cache_key)
        if not cached:
            return False
        checked_at, _cached_phase = cached
        if time.monotonic() - checked_at <= _HTTP_PREFLIGHT_VALID_CACHE_TTL_SECONDS:
            return True
        _HTTP_PREFLIGHT_VALID_CACHE.pop(cache_key, None)
        return False


def _forget_http_preflight_valid(account: Optional[Account]) -> None:
    if not account:
        return
    with _HTTP_PREFLIGHT_VALID_CACHE_LOCK:
        _HTTP_PREFLIGHT_VALID_CACHE.pop(_http_preflight_cache_key(account), None)


def _register_dead_session(db: Session, account: Account, reason: Optional[str]) -> bool:
    _forget_http_preflight_valid(account)
    return register_dead_session(db, account, reason)


def _register_captcha_session(db: Session, account: Account, reason: Optional[str]) -> None:
    _forget_http_preflight_valid(account)
    register_captcha_session(db, account, reason)


def _register_blocked_session(db: Session, account: Account, reason: Optional[str]) -> bool:
    _forget_http_preflight_valid(account)
    return register_blocked_session(db, account, reason)


def _current_media_guard_url(page: Page, fallback_url: str) -> str:
    current_url = getattr(page, "url", "") or ""
    if extract_tiktok_media_id(current_url):
        return current_url
    return fallback_url


def _log_user_trace(
    stage: str,
    *,
    page: Optional[Page] = None,
    expected_video_url: Optional[str] = None,
    correlation_id: Optional[str] = None,
    account_id: Optional[int] = None,
    level: int = logging.INFO,
    **fields: Any,
) -> None:
    current_url = getattr(page, "url", None) if page is not None else None
    payload: Dict[str, Any] = {
        "stage": stage,
        "correlation_id": correlation_id,
        "account_id": account_id,
        "expected_id": extract_tiktok_media_id(expected_video_url),
        "current_id": extract_tiktok_media_id(current_url),
        "current_url": current_url,
    }
    payload.update({k: v for k, v in fields.items() if v is not None})
    logger.log(level, "[USER_SCHEMA_TRACE] %s", json.dumps(payload, ensure_ascii=False)[:2200])


async def _ensure_page_on_target_media(
    page: Page,
    expected_video_url: str,
    fallback_video_url: str,
    *,
    timeout_ms: int,
    correlation_id: str,
    phase: str,
) -> bool:
    if not hasattr(page, "goto"):
        return True
    if is_on_expected_media_url(getattr(page, "url", None), expected_video_url):
        _log_user_trace(
            "url_guard_ok",
            page=page,
            expected_video_url=expected_video_url,
            correlation_id=correlation_id,
            phase=phase,
        )
        return True
    logger.warning(
        "[USER_SCHEMA] %s: URL drift before %s; expected=%s current=%s. Returning to target.",
        correlation_id,
        phase,
        expected_video_url,
        getattr(page, "url", None),
    )
    _log_user_trace(
        "url_guard_drift",
        page=page,
        expected_video_url=expected_video_url,
        correlation_id=correlation_id,
        level=logging.WARNING,
        phase=phase,
        action="return_to_target",
    )
    targets = [expected_video_url]
    if fallback_video_url and fallback_video_url not in targets:
        targets.append(fallback_video_url)
    for target_url in targets:
        try:
            await page.goto(target_url, wait_until="domcontentloaded", timeout=int(timeout_ms))
            await asyncio.sleep(1.0)
        except Exception as exc:
            logger.warning(
                "[USER_SCHEMA] %s: return-to-target failed during %s via %s: %s",
                correlation_id,
                phase,
                target_url,
                str(exc)[:120],
            )
            continue
        if is_on_expected_media_url(getattr(page, "url", None), expected_video_url):
            _log_user_trace(
                "url_guard_recovered",
                page=page,
                expected_video_url=expected_video_url,
                correlation_id=correlation_id,
                phase=phase,
                target_url=target_url,
            )
            return True
    logger.warning(
        "[USER_SCHEMA] %s: URL drift persisted during %s; expected=%s current=%s",
        correlation_id,
        phase,
        expected_video_url,
        getattr(page, "url", None),
    )
    _log_user_trace(
        "url_guard_failed",
        page=page,
        expected_video_url=expected_video_url,
        correlation_id=correlation_id,
        level=logging.WARNING,
        phase=phase,
        result="url_drift_to_other_video",
    )
    return False


async def _open_comment_list_with_safe_retry(
    page: Page,
    expected_video_url: str,
    fallback_video_url: str,
    *,
    timeout_ms: int,
    correlation_id: str,
    phase: str,
    account_id: Optional[int] = None,
    variant_index: Optional[int] = None,
) -> bool:
    try:
        opened = await open_comment_section(
            page,
            max_wait=12.0,
            require_list_with_items=True,
        )
    except Exception as exc:
        logger.debug(
            "[USER_SCHEMA] %s: %s open comments failed: %s",
            correlation_id,
            phase,
            str(exc)[:120],
        )
        opened = False
    if opened:
        return True

    _log_user_trace(
        "comment_list_open_retry_start",
        page=page,
        expected_video_url=expected_video_url,
        correlation_id=correlation_id,
        account_id=account_id,
        variant_index=variant_index,
        phase=phase,
        level=logging.WARNING,
    )
    if not await _ensure_page_on_target_media(
        page,
        expected_video_url,
        fallback_video_url,
        timeout_ms=timeout_ms,
        correlation_id=correlation_id,
        phase=f"{phase}_retry_url_guard",
    ):
        return False

    try:
        await page.goto(expected_video_url, wait_until="domcontentloaded", timeout=int(timeout_ms))
        await asyncio.sleep(1.8)
    except Exception as exc:
        logger.warning(
            "[USER_SCHEMA] %s: %s retry goto target failed: %s",
            correlation_id,
            phase,
            str(exc)[:120],
        )
    if not await _ensure_page_on_target_media(
        page,
        expected_video_url,
        fallback_video_url,
        timeout_ms=timeout_ms,
        correlation_id=correlation_id,
        phase=f"{phase}_after_retry_goto",
    ):
        return False

    try:
        opened = await open_comment_section(
            page,
            max_wait=12.0,
            require_list_with_items=True,
        )
    except Exception as exc:
        logger.debug(
            "[USER_SCHEMA] %s: %s retry open comments failed: %s",
            correlation_id,
            phase,
            str(exc)[:120],
        )
        opened = False

    _log_user_trace(
        "comment_list_open_retry_result",
        page=page,
        expected_video_url=expected_video_url,
        correlation_id=correlation_id,
        account_id=account_id,
        variant_index=variant_index,
        phase=phase,
        opened=opened,
        level=logging.INFO if opened else logging.WARNING,
    )
    return opened


async def _wait_for_searchable_comment_surface(
    page: Page,
    *,
    expected_video_url: str,
    correlation_id: str,
    phase: str,
    timeout_seconds: float = 3.0,
) -> bool:
    """Give TikTok's comment drawer a moment to settle before scoped search."""
    deadline = time.monotonic() + max(0.5, timeout_seconds)
    stable_hits = 0
    last_surface: Optional[Dict[str, Any]] = None
    while time.monotonic() < deadline:
        state = await get_comment_surface_state(page)
        if not state:
            # If the browser-side probe fails, do not claim that the drawer was
            # visibly searchable. Callers use this boolean to decide whether a
            # later comment_list_not_open can be downgraded to "parents absent".
            return False
        list_source = state.get("list_source") or {}
        if not isinstance(list_source, dict):
            list_source = {}
        last_surface = {
            "input": bool(state.get("input_visible")),
            "comment_input": bool(state.get("comment_input_visible")),
            "list": bool(state.get("list_visible")),
            "items": int(state.get("comment_items_visible") or 0),
            "strong_items": int(list_source.get("strong_items") or 0),
            "scrollable": bool(list_source.get("scrollable")),
            "list_source": list_source or None,
        }
        if last_surface["list"] and last_surface["items"] > 0:
            stable_hits += 1
            if stable_hits >= 2:
                _log_user_trace(
                    "comment_surface_search_ready",
                    page=page,
                    expected_video_url=expected_video_url,
                    correlation_id=correlation_id,
                    phase=phase,
                    surface=last_surface,
                )
                return True
        else:
            stable_hits = 0
        await asyncio.sleep(0.35)

    _log_user_trace(
        "comment_surface_search_not_stable",
        page=page,
        expected_video_url=expected_video_url,
        correlation_id=correlation_id,
        phase=phase,
        surface=last_surface,
        level=logging.INFO,
    )
    return False


def _empty_comment_surface_summary(state: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    try:
        items = int(state.get("comment_items_visible") or 0)
    except Exception:
        items = 0
    if items > 0:
        return None
    opened_empty_surface = bool(
        state.get("list_visible")
        or state.get("comment_input_visible")
    )
    if not opened_empty_surface:
        return None
    return {
        "input": bool(state.get("input_visible")),
        "comment_input": bool(state.get("comment_input_visible")),
        "list": bool(state.get("list_visible")),
        "items": items,
        "list_source": state.get("list_source"),
    }


def _empty_comment_surface_outcome(summary: Dict[str, Any]) -> str:
    if summary.get("list"):
        return "observer_open_list_empty"
    return _OBSERVER_INPUT_ONLY_EMPTY_OUTCOME


async def _empty_comment_surface_summary_for_page(page: Page) -> Optional[Dict[str, Any]]:
    try:
        state = await get_comment_surface_state(page)
    except Exception:
        return None
    if not isinstance(state, dict):
        return None
    return _empty_comment_surface_summary(state)


@dataclass
class PublishedMain:
    """Что было реально опубликовано в main-фазе."""

    text: str                     # текст варианта, который писали
    comment_id: Optional[str]     # network/dom comment_id, может быть None
    network_confirmed: bool       # подтверждено сетью
    local_confirmed: bool         # видим локально в DOM
    method: str                   # "hybrid_dom" / etc.
    account_id: int
    tiktok_username: Optional[str]  # реальный username автора (для лайков)


@dataclass
class MainAnchor:
    """A main comment found by a reply account.

    `matched_text` is the logical text from our configured main variant.
    `open_text` is the exact text seen in the current DOM and is safer for
    opening TikTok's reply box after a fuzzy search.
    """

    matched_text: Optional[str]
    parent_comment_id: Optional[str]
    open_text: Optional[str] = None
    found_by: str = "none"
    id_verified: bool = False
    raw_text: Optional[str] = None
    scroll: Optional[int] = None
    position: Optional[int] = None
    scroll_top: Optional[float] = None
    error: Optional[str] = None
    source_main: Optional[PublishedMain] = None
    parent_key: Optional[str] = None
    requested_author: Optional[str] = None
    matched_author: Optional[str] = None
    author_fallback_used: bool = False
    author_fallback_reason: Optional[str] = None

    def __iter__(self):
        yield self.matched_text
        yield self.parent_comment_id


@dataclass
class ReplyOutcome:
    text: str
    variant_index: int            # 1..N — номер варианта ответа
    matched_main_text: Optional[str]
    parent_comment_id: Optional[str]
    success: bool
    method: Optional[str]
    error: Optional[str]
    account_id: Optional[int]
    attempted: bool = False
    submitted_unconfirmed: bool = False
    parent_key: Optional[str] = None
    requested_parent_author: Optional[str] = None
    matched_parent_author: Optional[str] = None
    parent_author_match_mode: Optional[str] = None
    author_fallback_used: bool = False
    author_fallback_reason: Optional[str] = None


@dataclass
class ReplyAttemptResult:
    replies: List[ReplyOutcome] = field(default_factory=list)
    fallback_mains: List[PublishedMain] = field(default_factory=list)
    fallback_attempted: bool = False
    fallback_error: Optional[str] = None
    found_mains: List[PublishedMain] = field(default_factory=list)
    attempted_parent_keys: set[str] = field(default_factory=set)
    searched: bool = False
    confident_empty_search: bool = False
    submitted_parent_keys_count: int = 0


@dataclass
class UserSchemaResult:
    main_account_id: Optional[int] = None
    main_tiktok_username: Optional[str] = None
    published_mains: List[PublishedMain] = field(default_factory=list)
    replies: List[ReplyOutcome] = field(default_factory=list)
    expected_main_publications: int = 0
    expected_reply_variants: int = 0
    # Reply-аккаунт нашёл хоть один main → можно заказывать лайки.
    main_found_by_observer: bool = False
    # Сработал fallback: ни один reply-аккаунт не нашёл main → reply-аккаунт
    # сам написал главные комменты.
    fallback_main_via_reply: bool = False
    boost_order_id: Optional[int] = None
    boost_error: Optional[str] = None
    boost_enabled: bool = False
    boost_quantity: int = 0
    warnings: List[Dict[str, str]] = field(default_factory=list)
    error: Optional[str] = None
    elapsed_seconds: float = 0.0

    @property
    def has_any_publish(self) -> bool:
        return any(self.published_mains)

    def to_dict(self, *, source: str = "manual", mode: str = "full") -> Dict[str, Any]:
        """Сериализация в формат, совместимый с executor.py / UI."""
        successful_replies = sum(1 for r in self.replies if r.success)
        submitted_unconfirmed_replies = sum(
            1 for r in self.replies if getattr(r, "submitted_unconfirmed", False)
        )
        submitted_replies = sum(
            1 for r in self.replies if r.success or getattr(r, "submitted_unconfirmed", False)
        )
        total_replies = len(self.replies)
        expected_reply_variants = max(0, int(self.expected_reply_variants or 0))

        main_success = self.has_any_publish or self.fallback_main_via_reply
        replies_success = successful_replies == total_replies and (
            expected_reply_variants == 0 or total_replies >= expected_reply_variants
        )
        replies_submitted = submitted_replies == total_replies and (
            expected_reply_variants == 0 or total_replies >= expected_reply_variants
        )
        main_unconfirmed = bool(main_success and not self.main_found_by_observer)
        reply_unconfirmed = bool(
            main_success
            and self.main_found_by_observer
            and not replies_success
            and replies_submitted
            and submitted_unconfirmed_replies > 0
        )
        result_error = self.error
        warnings = list(self.warnings)
        if main_success and self.main_found_by_observer and replies_success:
            status = "success"
            core_status = "success"
        elif main_success:
            status = "submitted_unconfirmed"
            core_status = "submitted_unconfirmed"
            if not result_error:
                if main_unconfirmed:
                    result_error = "main_public_confirmation_unconfirmed"
                elif reply_unconfirmed:
                    result_error = "reply_publication_unconfirmed"
                else:
                    result_error = "reply_publication_incomplete"
            if main_unconfirmed and not any(
                isinstance(item, dict) and item.get("code") == "main_submitted_unconfirmed"
                for item in warnings
            ):
                warnings.append(
                    {
                        "code": "main_submitted_unconfirmed",
                        "message": "Main comment was submitted but not publicly verified",
                    }
                )
            if submitted_unconfirmed_replies and not any(
                isinstance(item, dict) and item.get("code") == "reply_submitted_unconfirmed"
                for item in warnings
            ):
                warnings.append(
                    {
                        "code": "reply_submitted_unconfirmed",
                        "message": "Reply comment was submitted but not publicly verified",
                    }
                )
        else:
            status = "fail"
            core_status = "fail"

        first = self.published_mains[0] if self.published_mains else None
        main_error = result_error if status != "success" and (not main_success or main_unconfirmed) else None
        return {
            "status": status,
            "core_status": core_status,
            "comment_id": first.comment_id if first else None,
            "error": result_error,
            "warnings": warnings,
            "has_warnings": bool(warnings),
            "source": source,
            "mode": mode,
            "engagement": {
                "main": {
                    "success": main_success,
                    "posted": main_success,
                    "verified": self.main_found_by_observer,
                    "comment_id": first.comment_id if first else None,
                    "text_choice": first.text if first else None,
                    "network_confirmed": bool(first and first.network_confirmed),
                    "local_confirmed": bool(first and first.local_confirmed),
                    "method": first.method if first else None,
                    "submitted_unconfirmed": main_unconfirmed,
                    "expected": max(0, int(self.expected_main_publications or 0)),
                    "visibility_status": (
                        "public_confirmed"
                        if self.main_found_by_observer
                        else ("submitted_unconfirmed" if main_success else "not_posted")
                    ),
                    "error": main_error,
                    "variants_published": [
                        {
                            "text": p.text,
                            "comment_id": p.comment_id,
                            "method": p.method,
                            "account_id": p.account_id,
                            "network_confirmed": p.network_confirmed,
                            "local_confirmed": p.local_confirmed,
                        }
                        for p in self.published_mains
                    ],
                    "fallback_via_reply": self.fallback_main_via_reply,
                },
                "replies": {
                    "ok": successful_replies,
                    "total": total_replies,
                    "requested_variants": expected_reply_variants,
                    "submitted": submitted_replies,
                    "submitted_unconfirmed": submitted_unconfirmed_replies,
                    "items": [
                        {
                            "text": r.text,
                            "variant_index": r.variant_index,
                            "matched_main_text": r.matched_main_text,
                            "parent_comment_id": r.parent_comment_id,
                            "parent_key": getattr(r, "parent_key", None),
                            "success": r.success,
                            "attempted": r.attempted,
                            "submitted_unconfirmed": getattr(r, "submitted_unconfirmed", False),
                            "method": r.method,
                            "error": r.error,
                            "account_id": r.account_id,
                        }
                        for r in self.replies
                    ],
                },
                "likes": {
                    "provider": "boost_project",
                    "order_id": self.boost_order_id,
                    "quantity": self.boost_quantity,
                    "error": self.boost_error,
                    "enabled": self.boost_enabled,
                    "provider_available": True,
                    "ok": self.boost_order_id is not None or not self.boost_enabled,
                    "requested": self.boost_enabled,
                },
            },
            "timing": {"started_at": None, "ended_at": None, "duration_ms": int(self.elapsed_seconds * 1000)},
        }


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

async def _safe_close(context, page) -> None:
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


_PROXY_FAILURE_MARKERS = (
    "proxy", "tunnel", "socks",
    "net::err_tunnel_connection_failed",
    "net::err_proxy_connection_failed",
    "net::err_socks_connection_failed",
    "net::err_timed_out",
    "net::err_connection_reset",
    "net::err_connection_refused",
    "net::err_name_not_resolved",
    # NOTE: net::err_http_response_code_failure исключён намеренно.
    # Это HTTP 4xx/5xx ответ TikTok-сервера, а не сбой прокси.
    # Раньше один такой goto-fail помечал прокси как unstable и каскадно
    # выбивал десятки аккаунтов на этом прокси из пула.
    "winerror 10060", "winerror 10061",
    "ssl: connect_failed",
    "connection refused", "connection reset",
    "connect call failed",
    "tls handshake",
    "chrome-error://",
)


def _looks_like_proxy_failure(reason: str) -> bool:
    if not reason:
        return False
    haystack = reason.lower()
    return any(marker in haystack for marker in _PROXY_FAILURE_MARKERS)


def _is_navigation_ambiguous(reason: str) -> bool:
    haystack = (reason or "").lower()
    return (
        haystack.startswith("goto_failed:")
        or haystack == "url_stuck_about_blank"
        or "chrome-error://" in haystack
    )


def _register_runtime_failure(db: Session, account: Optional[Account], reason: str) -> None:
    """Soft cooldown аккаунту + при явных proxy-ошибках помечаем прокси.

    Делим зоны ответственности:
      - аккаунт всегда получает soft cooldown (mark_soft_error);
      - если в reason явные proxy-маркеры, дополнительно `mark_proxy_bad`,
        чтобы следующая попытка не упала на том же канале.
    """
    if not account:
        return
    truncated = (reason or "")[:240]
    if "critical_cookies_not_generated" in truncated.lower():
        try:
            mark_runtime_cooldown(
                db,
                account.id,
                truncated,
                cooldown_minutes=_ENRICHMENT_FAILURE_COOLDOWN_MINUTES,
                category=RUNTIME_CATEGORY_COOKIE_ENRICHMENT,
            )
        except Exception:
            logger.debug("mark_runtime_cooldown failed", exc_info=True)
        return
    try:
        mark_soft_error(db, account.id, truncated)
    except Exception:
        logger.debug("mark_soft_error failed", exc_info=True)
    try:
        if account.proxy and _looks_like_proxy_failure(reason or ""):
            mark_proxy_bad(account.proxy, reason=truncated)
    except Exception:
        logger.debug("mark_proxy_bad failed", exc_info=True)


def _register_critical_enrichment_failure(
    db: Session,
    account: Optional[Account],
    reason: str,
) -> None:
    if "critical_cookies_not_generated" in (reason or "").lower():
        _register_runtime_failure(db, account, reason)


def _register_runtime_success(db: Session, account: Optional[Account], *, scope: str) -> None:
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
        logger.debug("register_runtime_success failed", exc_info=True)


async def _persist_cookies_after_success(
    db: Session,
    page: Optional[Page],
    account: Optional[Account],
    *,
    scope: str,
    correlation_id: str,
) -> None:
    if not page or not account:
        return
    context = getattr(page, "context", None)
    if context is None:
        return
    try:
        saved = await save_cookies_after_action(context, account, db)
        if saved:
            logger.debug(
                "[USER_SCHEMA] %s: persisted browser cookies after %s success for account=%s",
                correlation_id,
                scope,
                account.id,
            )
    except Exception:
        logger.debug(
            "[USER_SCHEMA] %s: cookie persistence after %s success skipped",
            correlation_id,
            scope,
            exc_info=True,
        )


async def _preflight_cookie_http_gate(
    db: Session,
    account: Account,
    *,
    correlation_id: str,
    phase: str,
) -> Optional[str]:
    """Cheap pre-browser cookie gate.

    Only a definitive logged-out answer skips the browser. Network/proxy
    ambiguity stays inconclusive, because bad proxy noise must not retire a
    usable account.
    """
    has_cookie_attrs = hasattr(account, "cookies") or hasattr(account, "cookie_file")
    if not has_cookie_attrs:
        return None
    if has_cookie_attrs and not getattr(account, "cookies", None) and not getattr(account, "cookie_file", None):
        reason = "http_preflight:no_cookies"
        _register_dead_session(db, account, reason)
        return f"dead_session:{reason}"

    cache_key = _http_preflight_cache_key(account)
    now_monotonic = time.monotonic()
    with _HTTP_PREFLIGHT_VALID_CACHE_LOCK:
        cached = _HTTP_PREFLIGHT_VALID_CACHE.get(cache_key)
        if cached:
            checked_at, cached_phase = cached
            if now_monotonic - checked_at <= _HTTP_PREFLIGHT_VALID_CACHE_TTL_SECONDS:
                logger.info(
                    "[USER_SCHEMA] %s: HTTP cookie preflight valid cache hit in %s (from %s)",
                    correlation_id,
                    phase,
                    cached_phase,
                )
                return None
            _HTTP_PREFLIGHT_VALID_CACHE.pop(cache_key, None)

    try:
        from services.cookie_validator import validate_account_http
        result = await validate_account_http(account)
    except Exception as exc:
        logger.info(
            "[USER_SCHEMA] %s: HTTP cookie preflight inconclusive in %s: %s",
            correlation_id,
            phase,
            str(exc)[:120],
        )
        return None

    status = str(result.get("status") or "").lower()
    error = str(result.get("error") or "")
    if result.get("valid") or status == "valid":
        with _HTTP_PREFLIGHT_VALID_CACHE_LOCK:
            _HTTP_PREFLIGHT_VALID_CACHE[cache_key] = (time.monotonic(), phase)
        logger.info(
            "[USER_SCHEMA] %s: HTTP cookie preflight valid in %s",
            correlation_id,
            phase,
        )
        return None

    if status == "dead" and bool(result.get("definitive", True)):
        with _HTTP_PREFLIGHT_VALID_CACHE_LOCK:
            _HTTP_PREFLIGHT_VALID_CACHE.pop(cache_key, None)
        reason = f"http_preflight:{error or 'dead_session'}"
        hard_dead_markers = (
            "no cookies",
            "invalid cookies format",
            "missing session cookies",
            "missing required cookies",
        )
        if not any(marker in error.lower() for marker in hard_dead_markers):
            logger.info(
                "[USER_SCHEMA] %s: HTTP cookie preflight says dead in %s, "
                "but browser DOM will verify before skipping: %s",
                correlation_id,
                phase,
                reason,
            )
            return None
        _register_dead_session(db, account, reason)
        logger.warning(
            "[USER_SCHEMA] %s: skipping browser after definitive HTTP cookie failure in %s: %s",
            correlation_id,
            phase,
            reason,
        )
        return f"dead_session:{reason}"

    logger.info(
        "[USER_SCHEMA] %s: HTTP cookie preflight inconclusive in %s: status=%s error=%s",
        correlation_id,
        phase,
        status or "unknown",
        error[:120],
    )
    return None


def _manual_captcha_wait_enabled() -> bool:
    try:
        return not bool(_settings.PLAYWRIGHT_HEADLESS)
    except Exception:
        return False


async def _wait_for_visible_captcha_clear(
    page: Page,
    *,
    captcha_kind: str,
    phase: str,
) -> bool:
    if not _manual_captcha_wait_enabled():
        return False

    deadline = time.monotonic() + _CAPTCHA_MANUAL_WAIT_SECONDS
    logger.warning(
        "[USER_SCHEMA] TikTok %s captcha visible during %s; waiting up to %.0fs for manual solve.",
        captcha_kind,
        phase,
        _CAPTCHA_MANUAL_WAIT_SECONDS,
    )
    while time.monotonic() < deadline:
        await asyncio.sleep(_CAPTCHA_MANUAL_POLL_SECONDS)
        try:
            has_captcha, _ = await detect_captcha(page, wait_ms=0)
        except Exception as exc:
            logger.debug("[USER_SCHEMA] captcha detector failed during manual wait: %s", exc)
            continue
        if not has_captcha:
            await asyncio.sleep(1.0)
            try:
                still_clear, _ = await detect_captcha(page, wait_ms=0)
            except Exception:
                still_clear = False
            if not still_clear:
                logger.info(
                    "[USER_SCHEMA] TikTok captcha cleared manually during %s; continuing.",
                    phase,
                )
                return True

    logger.warning(
        "[USER_SCHEMA] Manual captcha wait timed out after %.0fs during %s.",
        _CAPTCHA_MANUAL_WAIT_SECONDS,
        phase,
    )
    return False


async def _visible_captcha_reason(page: Page, *, phase: str) -> Optional[str]:
    try:
        has_captcha, captcha_type = await detect_captcha(page, wait_ms=0)
    except Exception as exc:
        logger.debug("[USER_SCHEMA] captcha preflight failed during %s: %s", phase, exc)
        return None
    if not has_captcha:
        return None
    captcha_kind = captcha_type or "unknown"
    if await _wait_for_visible_captcha_clear(page, captcha_kind=captcha_kind, phase=phase):
        return None
    return f"captcha_challenge:{captcha_kind}"


def _login_state_logged_out_reason(state: dict, *, phase: str) -> Optional[str]:
    if str(state.get("state") or "").lower() != "logged_out":
        return None
    return (
        f"visible_login_ui:{phase}:"
        f"login={int(state.get('loginCount') or 0)}:"
        f"modal={int(state.get('loginModalCount') or 0)}:"
        f"comment_cta={int(state.get('commentLoginCtaCount') or 0)}:"
        f"state_false={bool(state.get('stateIsLoginFalse'))}"
    )


async def _visible_logged_out_reason(page: Page, *, phase: str) -> Optional[str]:
    try:
        await dismiss_cookie_banners(page)
    except Exception:
        pass
    try:
        state = await get_browser_login_state(page)
    except Exception as exc:
        logger.debug("[USER_SCHEMA] login-state probe failed during %s: %s", phase, exc)
        return None
    return _login_state_logged_out_reason(state, phase=phase)


async def _deny_if_visible_logged_out(
    db: Session,
    page: Page,
    account: Account,
    *,
    correlation_id: str,
    phase: str,
) -> Optional[str]:
    reason = await _visible_logged_out_reason(page, phase=phase)
    if not reason:
        return None
    _register_dead_session(db, account, reason)
    _log_user_trace(
        "logged_out_ui_detected",
        page=page,
        expected_video_url=getattr(page, "url", None),
        correlation_id=correlation_id,
        account_id=getattr(account, "id", None),
        phase=phase,
        reason=reason,
        level=logging.WARNING,
    )
    return f"dead_session:{reason}"


async def _wait_for_url_settled(page: Page, *, max_wait_ms: int = 4000) -> str:
    """Ждёт пока URL перестанет быть about:blank.

    После goto Playwright возвращается раньше, чем React успевает обновить
    location. probe_cookie_health на about:blank всегда вернёт DEAD —
    и мы зря накажем аккаунт. Поэтому короткий polling.
    """
    started = time.monotonic()
    deadline = started + max_wait_ms / 1000.0
    while time.monotonic() < deadline:
        try:
            url = (page.url or "").strip()
        except Exception:
            url = ""
        if url and url != "about:blank":
            return url.lower()
        await asyncio.sleep(0.1)
    try:
        return (page.url or "").lower()
    except Exception:
        return ""


async def _open_video_and_check(
    page: Page,
    video_url: str,
    *,
    timeout_ms: int,
    correlation_id: str,
) -> CookieHealth:
    """goto + одна визуальная проверка login через cookie_health.

    Возвращает CookieHealth с verdict ∈ {READY, DEAD, AMBIGUOUS}.
    DEAD означает что куки заведомо не работают на этом видео; AMBIGUOUS —
    UI неоднозначный (например, медленный proxy), даём шанс одной
    повторной попытки.

    Защиты:
      - goto может вернуться, пока URL == about:blank → ждём 4с пока
        Playwright обновит location;
      - после dismiss/keyboard-shortcuts очистки даём React 1 с на
        отрисовку шапки/логин-кнопки.
    """
    try:
        await page.goto(video_url, wait_until="domcontentloaded", timeout=timeout_ms)
    except Exception as exc:
        # Сетевые таймауты при goto чаще всего ― проблема прокси, не куки.
        # Не маркируем как DEAD, чтобы не сжечь счётчик. Caller увидит
        # AMBIGUOUS и при повторной AMBIGUOUS попытке аккаунт уйдёт в soft
        # cooldown, не в reauth_needed.
        logger.warning(
            "[USER_SCHEMA] goto failed %s: %s — treating as ambiguous (proxy-flavour)",
            correlation_id,
            str(exc)[:120],
        )
        return CookieHealth(
            verdict=CookieVerdict.AMBIGUOUS,
            reason=f"goto_failed:{str(exc)[:80]}",
            current_url=None,
            on_video=False,
        )

    # Ждём, пока Playwright обновит page.url (избегаем about:blank race).
    settled_url = await _wait_for_url_settled(page, max_wait_ms=4000)
    if not settled_url or settled_url == "about:blank":
        logger.warning(
            "[USER_SCHEMA] url stayed about:blank for %s — proxy/playwright issue, marking ambiguous",
            correlation_id,
        )
        return CookieHealth(
            verdict=CookieVerdict.AMBIGUOUS,
            reason="url_stuck_about_blank",
            current_url=settled_url or None,
            on_video=False,
        )

    # Небольшая пауза, чтобы React успел отрисовать шапку.
    await asyncio.sleep(1.0)
    try:
        await dismiss_cookie_banners(page)
    except Exception:
        pass
    try:
        await suppress_keyboard_shortcuts_overlay(page)
    except Exception:
        pass

    logged_out_reason = await _visible_logged_out_reason(page, phase="initial_video_probe")
    if logged_out_reason:
        health = CookieHealth(
            verdict=CookieVerdict.DEAD,
            reason=logged_out_reason,
            current_url=getattr(page, "url", None),
            on_video=True,
        )
        logger.info(
            "[USER_SCHEMA] login probe %s | verdict=%s reason=%s url=%s",
            correlation_id,
            health.verdict.value,
            health.reason,
            health.current_url,
        )
        return health

    health = await probe_cookie_health(page)
    logger.info(
        "[USER_SCHEMA] login probe %s | verdict=%s reason=%s url=%s",
        correlation_id,
        health.verdict.value,
        health.reason,
        health.current_url,
    )
    return health


async def _ensure_account_login_ready(
    db: Session,
    page: Page,
    account: Account,
    video_url: str,
    *,
    timeout_ms: int,
    correlation_id: str,
) -> Tuple[bool, Optional[str]]:
    """Подготовка аккаунта: один заход + опциональный retry для AMBIGUOUS.

    Возвращает (ok, dead_reason). При ok=False вызывающий код знает, что
    эта попытка с этим аккаунтом отменена, а само решение «штрафовать /
    выводить аккаунт» уже принято внутри (через cookie_health).
    """
    health = await _open_video_and_check(
        page,
        video_url,
        timeout_ms=timeout_ms,
        correlation_id=correlation_id,
    )
    http_preflight_valid = _has_recent_http_preflight_valid(account)
    if health.is_dead:
        _register_dead_session(db, account, health.reason)
        return False, f"dead_session:{health.reason}"

    if health.is_captcha:
        _register_captcha_session(db, account, health.reason)
        return False, health.reason or "captcha_challenge:unknown"

    if health.is_blocked:
        _register_blocked_session(db, account, health.reason)
        reason = health.reason or "blocked_ui:unknown"
        return False, reason if reason.startswith("blocked_ui:") else f"blocked_ui:{reason}"
    if health.is_ready:
        if _should_try_action_ready_probe(health.reason):
            if http_preflight_valid:
                register_alive_session(db, account)
                logger.info(
                    "[USER_SCHEMA] %s: accepting unconfirmed ready DOM because HTTP passport preflight is valid: %s",
                    correlation_id,
                    health.reason,
                )
                return True, None
            ok, action_deny = await _confirm_comment_action_ready(
                db,
                page,
                account,
                video_url,
                timeout_ms=timeout_ms,
                correlation_id=correlation_id,
                phase="login_probe_ready_unconfirmed",
            )
            if ok:
                return True, None
            return False, action_deny or f"login_ambiguous:{health.reason}"
        register_alive_session(db, account)
        return True, None

    if _should_try_action_ready_probe(health.reason):
        if http_preflight_valid:
            register_alive_session(db, account)
            logger.info(
                "[USER_SCHEMA] %s: accepting unconfirmed video DOM because HTTP passport preflight is valid: %s",
                correlation_id,
                health.reason,
            )
            return True, None
        ok, action_deny = await _confirm_comment_action_ready(
            db,
            page,
            account,
            video_url,
            timeout_ms=timeout_ms,
            correlation_id=correlation_id,
            phase="login_probe",
        )
        if ok:
            return True, None
        if action_deny and not action_deny.startswith("login_ambiguous:"):
            return False, action_deny

    if _is_navigation_ambiguous(health.reason):
        register_ambiguous_session(db, account, health.reason)
        if account.proxy and _looks_like_proxy_failure(health.reason or ""):
            try:
                mark_proxy_bad(account.proxy, reason=f"login_ambiguous:{health.reason or ''}"[:200])
            except Exception:
                logger.debug("mark_proxy_bad failed for navigation ambiguous", exc_info=True)
        return False, f"login_ambiguous:{health.reason}"

    if (health.reason or "").startswith(("captcha_challenge:", "captcha_challenge_timeout:")):
        _register_captcha_session(db, account, health.reason)
        return False, health.reason or "captcha_challenge:unknown"

    # AMBIGUOUS: короткая случайная пауза + один reload + повторный probe.
    # Пауза нужна, чтобы при медленном прокси не уйти в hot-loop reload'ов.
    await asyncio.sleep(random.uniform(1.5, 2.5))
    try:
        await page.reload(wait_until="domcontentloaded", timeout=timeout_ms)
        await asyncio.sleep(1.0)
    except Exception:
        pass
    health2 = await probe_cookie_health(page)
    if health2.is_ready:
        if _should_try_action_ready_probe(health2.reason):
            if http_preflight_valid:
                register_alive_session(db, account)
                logger.info(
                    "[USER_SCHEMA] %s: accepting retry unconfirmed ready DOM because HTTP passport preflight is valid: %s",
                    correlation_id,
                    health2.reason,
                )
                return True, None
            ok, action_deny = await _confirm_comment_action_ready(
                db,
                page,
                account,
                video_url,
                timeout_ms=timeout_ms,
                correlation_id=correlation_id,
                phase="login_probe_retry_ready_unconfirmed",
            )
            if ok:
                return True, None
            return False, action_deny or f"login_ambiguous:{health2.reason}"
        register_alive_session(db, account)
        return True, None
    if health2.is_dead:
        _register_dead_session(db, account, health2.reason)
        return False, f"dead_session:{health2.reason}"
    if health2.is_captcha:
        _register_captcha_session(db, account, health2.reason)
        return False, health2.reason or "captcha_challenge:unknown"
    if health2.is_blocked:
        _register_blocked_session(db, account, health2.reason)
        reason = health2.reason or "blocked_ui:unknown"
        return False, reason if reason.startswith("blocked_ui:") else f"blocked_ui:{reason}"
    if _should_try_action_ready_probe(health2.reason):
        if http_preflight_valid:
            register_alive_session(db, account)
            logger.info(
                "[USER_SCHEMA] %s: accepting retry unconfirmed video DOM because HTTP passport preflight is valid: %s",
                correlation_id,
                health2.reason,
            )
            return True, None
        ok, action_deny = await _confirm_comment_action_ready(
            db,
            page,
            account,
            video_url,
            timeout_ms=timeout_ms,
            correlation_id=correlation_id,
            phase="login_probe_retry",
        )
        if ok:
            return True, None
        if action_deny and not action_deny.startswith("login_ambiguous:"):
            return False, action_deny
    # Всё ещё неясно — не наказываем «вторым DEAD» (могла быть проблема прокси).
    # Дополнительно: если reason пахнет прокси, фиксируем mark_proxy_bad.
    register_ambiguous_session(db, account, health2.reason)
    if account.proxy and _looks_like_proxy_failure(health2.reason or ""):
        try:
            mark_proxy_bad(account.proxy, reason=f"login_ambiguous:{health2.reason or ''}"[:200])
        except Exception:
            logger.debug("mark_proxy_bad failed for ambiguous", exc_info=True)
    return False, f"login_ambiguous:{health2.reason}"


def _record_health_failure(
    db: Session,
    account: Account,
    health: CookieHealth,
) -> Optional[str]:
    if health.is_ready:
        register_alive_session(db, account)
        return None
    if health.is_dead:
        _register_dead_session(db, account, health.reason)
        return f"dead_session:{health.reason}"
    if health.is_captcha or (health.reason or "").startswith(
        ("captcha_challenge:", "captcha_challenge_timeout:")
    ):
        _register_captcha_session(db, account, health.reason)
        return health.reason or "captcha_challenge:unknown"
    if health.is_blocked:
        _register_blocked_session(db, account, health.reason)
        reason = health.reason or "blocked_ui:unknown"
        return reason if reason.startswith("blocked_ui:") else f"blocked_ui:{reason}"

    register_ambiguous_session(db, account, health.reason)
    if account.proxy and _looks_like_proxy_failure(health.reason or ""):
        try:
            mark_proxy_bad(account.proxy, reason=f"login_ambiguous:{health.reason or ''}"[:200])
        except Exception:
            logger.debug("mark_proxy_bad failed for cookie health ambiguous", exc_info=True)
    return f"login_ambiguous:{health.reason}"


def _should_try_action_ready_probe(reason: Optional[str]) -> bool:
    safe = reason or ""
    return (
        safe.startswith("on_video_no_login_btn")
        or safe in {"ambiguous_ui", "probe_no_dict", "on_video_empty_dom_after_settle"}
    )


async def _confirm_comment_action_ready(
    db: Session,
    page: Page,
    account: Account,
    video_url: str,
    *,
    timeout_ms: int,
    correlation_id: str,
    phase: str,
) -> Tuple[bool, Optional[str]]:
    """Open the comment UI and require a real composer before publishing."""
    if not await _ensure_page_on_target_media(
        page,
        video_url,
        video_url,
        timeout_ms=timeout_ms,
        correlation_id=correlation_id,
        phase=f"{phase}_action_ready_url_guard",
    ):
        return False, "url_drift_to_other_video"

    deny = await _deny_if_visible_logged_out(
        db,
        page,
        account,
        correlation_id=correlation_id,
        phase=f"{phase}_before_open_comments",
    )
    if deny:
        return False, deny

    try:
        opened = await open_comment_section(
            page,
            max_wait=_ACTION_READY_PROBE_SECONDS,
            require_input=True,
        )
    except Exception as exc:
        opened = False
        logger.debug(
            "[USER_SCHEMA] %s: action-ready comment opener failed in %s: %s",
            correlation_id,
            phase,
            str(exc)[:120],
        )

    deny = await _deny_if_visible_logged_out(
        db,
        page,
        account,
        correlation_id=correlation_id,
        phase=f"{phase}_after_open_comments",
    )
    if deny:
        return False, deny

    health = await probe_cookie_health(page)
    if health.is_dead or health.is_captcha or health.is_blocked:
        deny = _record_health_failure(db, account, health)
        return False, deny

    if health.is_ready and not _should_try_action_ready_probe(health.reason):
        register_alive_session(db, account)
        logger.info(
            "[USER_SCHEMA] %s: action-ready composer was not visible in %s, "
            "but strong login marker is present (%s); continuing to publish path",
            correlation_id,
            phase,
            health.reason,
        )
        return True, None

    if opened:
        register_alive_session(db, account)
        logger.info(
            "[USER_SCHEMA] %s: action-ready probe passed in %s; comment composer is visible",
            correlation_id,
            phase,
        )
        return True, None

    logger.warning(
        "[USER_SCHEMA] %s: action-ready probe inconclusive in %s; "
        "composer not visible after short probe, but no explicit logged-out/captcha/block UI was detected",
        correlation_id,
        phase,
    )
    register_alive_session(db, account)
    return True, None


async def _post_enrichment_deny_reason(
    db: Session,
    page: Page,
    account: Account,
    *,
    correlation_id: str,
    phase: str,
) -> Optional[str]:
    await _wait_for_url_settled(page, max_wait_ms=2500)
    await asyncio.sleep(0.5)
    try:
        await dismiss_cookie_banners(page)
    except Exception:
        pass
    try:
        await suppress_keyboard_shortcuts_overlay(page)
    except Exception:
        pass

    deny = await _deny_if_visible_logged_out(
        db,
        page,
        account,
        correlation_id=correlation_id,
        phase=phase,
    )
    if deny:
        logger.warning(
            "[USER_SCHEMA] %s: stopping after enrichment; visible logged-out UI: %s",
            correlation_id,
            deny,
        )
        return deny

    health = await probe_cookie_health(page)
    logger.info(
        "[USER_SCHEMA] post-enrich probe %s | phase=%s verdict=%s reason=%s url=%s",
        correlation_id,
        phase,
        health.verdict.value,
        health.reason,
        health.current_url,
    )
    if health.is_ready:
        register_alive_session(db, account)
        return None
    if health.is_dead:
        deny = _record_health_failure(db, account, health)
        if deny:
            logger.warning(
                "[USER_SCHEMA] %s: stopping after enrichment; cookie health not ready: %s",
                correlation_id,
                deny,
            )
        return deny
    if health.is_captcha:
        _register_captcha_session(db, account, health.reason)
        logger.warning(
            "[USER_SCHEMA] %s: captcha visible after enrichment; switching account: %s",
            correlation_id,
            health.reason,
        )
        return health.reason or "captcha_challenge:unknown"
    if health.is_blocked:
        _register_blocked_session(db, account, health.reason)
        reason = health.reason or "blocked_ui:unknown"
        logger.warning(
            "[USER_SCHEMA] %s: blocked/empty DOM after enrichment; switching account: %s",
            correlation_id,
            reason,
        )
        return reason if reason.startswith("blocked_ui:") else f"blocked_ui:{reason}"
    if not (health.is_dead or health.is_captcha or health.is_blocked):
        logger.info(
            "[USER_SCHEMA] %s: post-enrich probe ambiguous; continuing to action-level checks: %s",
            correlation_id,
            health.reason,
        )
        return None
    return None


# ---------------------------------------------------------------------------
# MAIN phase: пишем все варианты подряд на одном аккаунте.
# ---------------------------------------------------------------------------

def _dedup_key(text: str) -> str:
    """Ключ дедупа: case-fold + collapse whitespace, но БЕЗ удаления ZWJ.

    `normalize_text` из comment_outcome убирает zero-width (U+200B..U+200D)
    что критично ломает emoji с join'ами вида '👨‍👩‍👧' (ZWJ-семьи). Поэтому
    для дедупа variants используем щадящую нормализацию: case-fold для
    кириллицы/латиницы, схлопнутые пробелы. Эмодзи (skin-tone modifiers,
    ZWJ-sequences, regional indicators) остаются нетронутыми.
    """
    import re as _re
    s = (text or "").strip().casefold()
    s = _re.sub(r"\s+", " ", s)
    return s


def _dedup_variants(text_variants: List[str]) -> List[str]:
    """Убирает пустые и дубликаты (по нормализованной форме), сохраняя порядок.

    Дубликат определяется через `_dedup_key`: case-insensitive сравнение с
    учётом эмодзи (ZWJ, skin tones, regional indicators).
    """
    seen: set[str] = set()
    result: List[str] = []
    for t in text_variants or []:
        s = (t or "").strip()
        if not s:
            continue
        key = _dedup_key(s)
        if not key or key in seen:
            continue
        seen.add(key)
        result.append(s)
    return result


def _next_inter_publish_delay(idx_published_so_far: int) -> float:
    """Пауза между публикациями main-вариантов на одном аккаунте."""
    return random.uniform(*_INTER_MAIN_VARIANT_DELAY_RANGE)


_CRITICAL_PUBLISH_REASONS = {
    # Эти ошибки означают, что страница в текущем состоянии в принципе
    # не позволяет отправить ничего. На second/third variant они тоже
    # сработают — серия прерывается, аккаунт идёт в фоновый cooldown.
    "input_not_found",
    "post_button_not_found",
    "post_button_stayed_disabled",
    "input_not_found_after_captcha_clear",
    "url_drift_to_other_video",
    "reply_input_not_activated",
    "visible_login_ui",
    "logged_out",
    "login_required",
}


def _is_critical_publish_failure(result: "PublishResult") -> bool:
    reason = (result.error_reason or "")
    if result.status == "redirected":
        return True
    if reason.startswith("captcha_challenge:"):
        return True
    # input_not_found: ...  / input_not_found_after_captcha_clear: ...
    for marker in _CRITICAL_PUBLISH_REASONS:
        if marker in reason:
            return True
    return False


def _is_input_not_found_publish_failure(result: "PublishResult") -> bool:
    reason = (result.error_reason or "")
    return result.status == "dom_error" and reason.startswith("input_not_found:")


def _should_attempt_local_publish_confirmation(result: "PublishResult") -> bool:
    """Only verify local DOM after a submit could actually have happened."""
    if _is_critical_publish_failure(result):
        return False
    status = result.status or ""
    reason = result.error_reason or ""
    if status == "pending_public_check":
        return True
    if status == "timeout" and "no_network_request_after_post_click" in reason:
        return True
    if "stealth_reject_empty_body" in reason:
        return True
    # New: synthetic dispatch on disabled Post button. We still run the local
    # DOM probe so we can log what's there, but commit D gates `local_confirmed`
    # so this case is NOT treated as a successful publish.
    if "submitted_unverified_disabled_post" in reason:
        return True
    return False


async def _recover_page_before_input_retry(page: Page, *, correlation_id: str) -> None:
    logger.warning(
        "[USER_SCHEMA] %s got input_not_found with login-ready page; reloading and reopening composer once",
        correlation_id,
    )
    try:
        await suppress_keyboard_shortcuts_overlay(page)
    except Exception:
        pass
    try:
        await dismiss_cookie_banners(page)
    except Exception:
        pass
    try:
        await page.reload(wait_until="domcontentloaded", timeout=20000)
        await asyncio.sleep(1.0)
    except Exception as exc:
        logger.warning(
            "[USER_SCHEMA] %s reload before input retry failed: %s",
            correlation_id,
            str(exc)[:120],
        )
    try:
        await suppress_keyboard_shortcuts_overlay(page)
    except Exception:
        pass
    try:
        await dismiss_cookie_banners(page)
    except Exception:
        pass


async def _publish_all_variants_on_account(
    db: Session,
    page: Page,
    account: Account,
    text_variants: List[str],
    *,
    correlation_id: str,
    expected_video_url: Optional[str] = None,
) -> Tuple[List[PublishedMain], Optional[str]]:
    """Пишет все variants подряд. Возвращает только успешные публикации.

    На каждой публикации делается фактический submit. Между публикациями —
    экспоненциальная пауза с jitter, чтобы TikTok не считал серию роботом.

    Серия прерывается, если первая публикация упала по причине, которая
    точно не починится повторной попыткой (input не появился, post button
    отключён, redirect, captcha challenge).
    """
    published: List[PublishedMain] = []
    last_failure_reason: Optional[str] = None
    tiktok_username = await extract_tiktok_username(page)

    text_variants = _dedup_variants(text_variants)

    for idx, text in enumerate(text_variants, start=1):
        sub_corr = f"{correlation_id}_v{idx}"
        logger.info(
            "[USER_SCHEMA] main publish %s on account=%s text=%r",
            sub_corr,
            account.id,
            text[:40],
        )
        _log_user_trace(
            "main_publish_variant_start",
            page=page,
            expected_video_url=expected_video_url,
            correlation_id=sub_corr,
            account_id=account.id,
            variant_index=idx,
            text_len=len(text or ""),
            text_preview=text[:80],
        )
        try:
            result: PublishResult = await publish_comment_hybrid(
                page,
                text,
                max_total_seconds=25.0,
                correlation_id=sub_corr,
                expected_video_url=expected_video_url,
            )
        except Exception as exc:
            logger.warning(
                "[USER_SCHEMA] main publish %s raised: %s",
                sub_corr,
                str(exc)[:120],
            )
            last_failure_reason = f"publish_exception:{str(exc)[:80]}"
            # Исключение из браузера часто означает закрытие контекста —
            # дальше с этой страницей делать нечего.
            break

        if _is_input_not_found_publish_failure(result):
            await _recover_page_before_input_retry(page, correlation_id=sub_corr)
            try:
                result = await publish_comment_hybrid(
                    page,
                    text,
                    max_total_seconds=30.0,
                    correlation_id=f"{sub_corr}_input_retry",
                    expected_video_url=expected_video_url,
                )
            except Exception as exc:
                logger.warning(
                    "[USER_SCHEMA] main publish %s input retry raised: %s",
                    sub_corr,
                    str(exc)[:120],
                )

        success = result.status == "success" and bool(result.comment_id)
        network_confirmed = success and bool(getattr(result, "network_captured", False))
        post_was_enabled = bool(getattr(result, "post_was_enabled_at_click", False))
        network_attempted = bool(
            getattr(result, "network_captured", False)
            or getattr(result, "network_request_attempted", False)
        )

        local_confirmed = False
        local_comment_id: Optional[str] = None
        if not success and _should_attempt_local_publish_confirmation(result):
            # publish_action did not return success+comment_id. Run a quick
            # local DOM probe to see what's there, but be strict about when we
            # treat it as local_confirmed.
            #
            # We only accept local_confirmed=True when EITHER:
            #   (a) the Post button was actually enabled at click time
            #       (post_was_enabled), OR
            #   (b) there was a captured network response (even with empty body)
            #       — meaning TikTok at least saw the request.
            #
            # A bare local DOM match WITHOUT either signal is NOT enough.
            # That's exactly the false-positive we hit when JS-fallback writes
            # into the wrapper: text appears in DOM, Post is disabled,
            # synthetic dispatch produces no network event → text is in our
            # composer, not in any actual published comment list.
            try:
                if expected_video_url and not is_on_expected_media_url(getattr(page, "url", None), expected_video_url):
                    logger.warning(
                        "[USER_SCHEMA] main publish %s skipped local confirmation due to URL drift: expected=%s current=%s",
                        sub_corr,
                        expected_video_url,
                        getattr(page, "url", None),
                    )
                    local_ok = False
                else:
                    local_ok, local_info = await find_comment_in_list(
                        page=page,
                        expected_text=text,
                        max_scrolls=2,
                        scroll_delay_ms=300,
                    )
            except Exception:
                local_ok = False
            if local_ok and (post_was_enabled or network_attempted):
                local_confirmed = True
                local_comment_id = str((local_info or {}).get("comment_id") or "").strip() or None
                if local_comment_id and not result.comment_id:
                    logger.info(
                        "[USER_SCHEMA] main publish %s recovered comment_id from local DOM: %s",
                        sub_corr,
                        local_comment_id[:24],
                    )
                logger.info(
                    "[USER_SCHEMA] main publish %s recorded as local-only "
                    "after %s (post_enabled=%s, network_attempted=%s); "
                    "observer will verify publicly before replies",
                    sub_corr,
                    result.error_reason or result.status,
                    post_was_enabled,
                    network_attempted,
                )
            elif local_ok:
                logger.warning(
                    "[USER_SCHEMA] main publish %s found text locally but "
                    "Post was disabled AND no network event — treating as "
                    "false positive (likely wrapper-placeholder leak)",
                    sub_corr,
                )
        if success:
            local_confirmed = True

        if not success and not local_confirmed:
            last_failure_reason = result.error_reason or result.status or "publish_failed"
            logger.warning(
                "[USER_SCHEMA] main publish %s FAILED: status=%s reason=%s",
                sub_corr,
                result.status,
                last_failure_reason,
            )
            _log_user_trace(
                "main_publish_variant_failed",
                page=page,
                expected_video_url=expected_video_url,
                correlation_id=sub_corr,
                account_id=account.id,
                variant_index=idx,
                status=result.status,
                reason=last_failure_reason,
                post_was_enabled=post_was_enabled,
                network_attempted=network_attempted,
                level=logging.WARNING,
            )
            logged_out_after_failure = await _deny_if_visible_logged_out(
                db,
                page,
                account,
                correlation_id=sub_corr,
                phase="main_publish_after_failure",
            )
            if logged_out_after_failure:
                last_failure_reason = logged_out_after_failure
                break
            if _is_critical_publish_failure(result):
                if str(last_failure_reason).startswith(("captcha_challenge:", "captcha_challenge_timeout:")):
                    _register_captcha_session(db, account, last_failure_reason)
                elif published:
                    _register_runtime_failure(
                        db,
                        account,
                        f"publish_failed:{last_failure_reason}"[:200],
                    )
                logger.warning(
                    "[USER_SCHEMA] critical failure (%s), stopping series on account=%s",
                    result.error_reason,
                    account.id,
                )
                break
            # На обычных DOM ошибках идём к следующему варианту без паузы:
            # пауза имеет смысл только между УСПЕШНЫМИ публикациями.
            continue

        published_comment_id = result.comment_id or (
            local_comment_id if local_confirmed else None
        )

        published.append(
            PublishedMain(
                text=text,
                comment_id=published_comment_id,
                network_confirmed=network_confirmed,
                local_confirmed=local_confirmed,
                method=result.dom_strategy or ("hybrid_http" if network_confirmed else "hybrid_dom"),
                account_id=account.id,
                tiktok_username=tiktok_username,
            )
        )
        _log_user_trace(
            "main_publish_variant_recorded",
            page=page,
            expected_video_url=expected_video_url,
            correlation_id=sub_corr,
            account_id=account.id,
            variant_index=idx,
            network_confirmed=network_confirmed,
            local_confirmed=local_confirmed,
            comment_id=published_comment_id,
            method=result.dom_strategy or ("hybrid_http" if network_confirmed else "hybrid_dom"),
        )
        if network_confirmed:
            _register_runtime_success(db, account, scope="main")
        elif local_confirmed:
            logger.info(
                "[USER_SCHEMA] main publish %s is local-only; not resetting account health counters yet",
                sub_corr,
            )

        logged_out_after_variant = await _deny_if_visible_logged_out(
            db,
            page,
            account,
            correlation_id=sub_corr,
            phase="main_publish_after_variant",
        )
        if logged_out_after_variant:
            last_failure_reason = logged_out_after_variant
            break

        # Пауза только если впереди ещё есть варианты для отправки.
        if idx < len(text_variants):
            delay = _next_inter_publish_delay(len(published))
            logger.debug("[USER_SCHEMA] inter-variant delay %.1fs (already published %d)", delay, len(published))
            await asyncio.sleep(delay)

    return published, (None if published else last_failure_reason)


async def _try_main_account(
    db: Session,
    account: Account,
    video_url: str,
    text_variants: List[str],
    *,
    correlation_id: str,
    resolved_video_url_holder: Optional[Dict[str, str]] = None,
) -> Tuple[List[PublishedMain], Optional[str]]:
    """Полный цикл одной попытки на main-аккаунте.

    Возвращает (published, deny_reason). deny_reason != None означает что
    попытка не состоялась; решение «вывести из работы» уже принято внутри
    cookie_health и в БД зафиксировано.
    """
    context = None
    page = None
    timeout_ms = _settings.PLAYWRIGHT_TIMEOUT * (2 if account.proxy else 1)
    preflight_deny = await _preflight_cookie_http_gate(
        db,
        account,
        correlation_id=correlation_id,
        phase="main_pre_browser",
    )
    if preflight_deny:
        return [], preflight_deny
    try:
        context, page = await create_page_for_account(account)
    except Exception as exc:
        logger.warning(
            "[USER_SCHEMA] create_page failed for acc=%s: %s",
            account.id,
            str(exc)[:120],
        )
        _register_runtime_failure(db, account, f"page_create_failed:{str(exc)[:80]}")
        return [], f"page_create_failed:{str(exc)[:80]}"

    try:
        ok, deny = await _ensure_account_login_ready(
            db,
            page,
            account,
            video_url,
            timeout_ms=int(timeout_ms),
            correlation_id=correlation_id,
        )
        if not ok:
            return [], deny
        target_media_url = _current_media_guard_url(page, video_url)
        if resolved_video_url_holder is not None and extract_tiktok_media_id(target_media_url):
            resolved_video_url_holder["video_url"] = target_media_url
        _log_user_trace(
            "main_account_ready",
            page=page,
            expected_video_url=target_media_url,
            correlation_id=correlation_id,
            account_id=account.id,
        )

        # ----- Cookie enrichment -----
        # Купленные cookies часто содержат только sid_guard+msToken, без ttwid/
        # s_v_web_id/tt_csrf_token. Это держит аккаунт в bucket=dom_only и
        # триггерит TikTok рендерить страницу без UI или с pointer-events:none
        # на Post. Перед публикацией заходим на /foryou через тот же page и
        # даём TikTok сгенерировать недостающие browser cookies.
        if needs_enrichment(account):
            enriched_cookies_json = None
            logger.info(
                "[USER_SCHEMA] %s: enriching cookies for account=%s before publish",
                correlation_id, account.id,
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
                    correlation_id=f"enrich_{correlation_id}",
                    timeout_seconds=_ENRICHMENT_TIMEOUT_SECONDS,
                )
                enrich_result.log_summary(account.id, correlation_id)
                if enrich_result.redirected:
                    mark_needs_reauth(db, account.id, reason="enrichment_redirect_dead_session")
                    return [], "dead_session_during_enrichment"
                if not enrich_result.success or not enrich_result.all_cookies_json:
                    reason = f"enrichment_failed:{enrich_result.error or 'missing_required_browser_cookies'}"
                    _register_critical_enrichment_failure(db, account, reason)
                    logger.warning(
                        "[USER_SCHEMA] %s: %s; continuing with current cookies",
                        correlation_id,
                        reason,
                    )
                else:
                    enriched_cookies_json = enrich_result.all_cookies_json
            except Exception as exc:
                reason = f"enrichment_exception:{str(exc)[:80]}"
                logger.warning(
                    "[USER_SCHEMA] %s: %s; continuing with current cookies",
                    correlation_id,
                    reason,
                )
            # После enrichment возвращаем page обратно на видео, потому что
            # enricher заходил на /foryou.
            try:
                await page.goto(target_media_url, wait_until="domcontentloaded", timeout=int(timeout_ms))
                await asyncio.sleep(1.0)
                target_media_url = _current_media_guard_url(page, target_media_url)
                if resolved_video_url_holder is not None and extract_tiktok_media_id(target_media_url):
                    resolved_video_url_holder["video_url"] = target_media_url
                deny_after_enrichment = await _post_enrichment_deny_reason(
                    db,
                    page,
                    account,
                    correlation_id=correlation_id,
                    phase="main_post_enrichment",
                )
                if deny_after_enrichment:
                    return [], deny_after_enrichment
                if enriched_cookies_json:
                    save_enriched_cookies_to_db(db, account.id, enriched_cookies_json)
                    account.cookies = enriched_cookies_json
            except Exception as exc:
                logger.warning(
                    "[USER_SCHEMA] %s: return-to-video after enrichment failed: %s",
                    correlation_id, str(exc)[:120],
                )
                _register_runtime_failure(db, account, f"post_enrichment_return_failed:{str(exc)[:80]}")
                return [], f"post_enrichment_return_failed:{str(exc)[:80]}"

        # ----- Human-like idle delay -----
        # Пользователь не сразу пишет коммент: 2-5 сек смотрит видео, прежде
        # чем кликнуть на иконку комментов. Без этого TikTok видит «зашёл
        # → сразу в комменты» — типичный бот-паттерн.
        idle_seconds = random.uniform(2.5, 5.5)
        logger.debug(
            "[USER_SCHEMA] %s: human-like idle %.1fs before publish",
            correlation_id, idle_seconds,
        )
        await asyncio.sleep(idle_seconds)

        captcha_reason = await _visible_captcha_reason(page, phase="main_pre_warmup")
        if captcha_reason:
            _register_captcha_session(db, account, captcha_reason)
            logger.warning(
                "[USER_SCHEMA] %s: captcha visible before warmup; switching account: %s",
                correlation_id,
                captcha_reason,
            )
            return [], captcha_reason
        logged_out_deny = await _deny_if_visible_logged_out(
            db,
            page,
            account,
            correlation_id=correlation_id,
            phase="main_pre_warmup",
        )
        if logged_out_deny:
            return [], logged_out_deny

        # ----- Behavioral warmup -----
        # Лайкнуть случайный чужой коммент перед публикацией своего: TikTok
        # видит «человеческое» поведение, что повышает trust score сессии.
        # probability=0.5 — половина попыток. Повышено с 0.3 после теста,
        # где enrichment один без warmup не пробил pointer-events:none.
        try:
            await like_random_comment_before_action(
                page,
                account.id,
                using_proxy=bool(account.proxy),
                probability=0.5,
            )
        except Exception as exc:
            logger.debug(
                "[USER_SCHEMA] %s: behavioral warmup failed: %s",
                correlation_id, str(exc)[:120],
            )
        captcha_reason = await _visible_captcha_reason(page, phase="main_pre_publish")
        if captcha_reason:
            _register_captcha_session(db, account, captcha_reason)
            logger.warning(
                "[USER_SCHEMA] %s: captcha visible before main publish; switching account: %s",
                correlation_id,
                captcha_reason,
            )
            return [], captcha_reason
        logged_out_deny = await _deny_if_visible_logged_out(
            db,
            page,
            account,
            correlation_id=correlation_id,
            phase="main_pre_publish",
        )
        if logged_out_deny:
            return [], logged_out_deny
        if not await _ensure_page_on_target_media(
            page,
            target_media_url,
            video_url,
            timeout_ms=int(timeout_ms),
            correlation_id=correlation_id,
            phase="main_pre_publish",
        ):
            return [], "url_drift_to_other_video"
        if resolved_video_url_holder is not None and extract_tiktok_media_id(target_media_url):
            resolved_video_url_holder["video_url"] = target_media_url

        published, publish_reason = await _publish_all_variants_on_account(
            db,
            page,
            account,
            text_variants,
            correlation_id=correlation_id,
            expected_video_url=target_media_url,
        )
        if published:
            await _persist_cookies_after_success(
                db,
                page,
                account,
                scope="main",
                correlation_id=correlation_id,
            )
        return published, publish_reason
    finally:
        await _safe_close(context, page)


# ---------------------------------------------------------------------------
# REPLY phase: один аккаунт — один вариант ответа на все найденные комменты.
# ---------------------------------------------------------------------------

async def _find_main_anchor_by_text_legacy(
    page: Page,
    main_candidates: List[Any],
    *,
    timeout_seconds: float = _REPLY_MAIN_SEARCH_PER_TEXT_SECONDS,
) -> Tuple[Optional[str], Optional[str]]:
    """Ищет ЛЮБОЙ из главных комментов на странице видео.

    Возвращает: (matched_text, parent_comment_id_or_None). Сам DOM-handle
    нам не нужен — `publish_reply_hybrid` сам найдёт reply-кнопку по
    тексту/cid внутри страницы.
    """
    main_texts = _dedup_variants([p.text for p in _normalize_main_candidates(main_candidates)])
    deadline = time.monotonic() + max(1.0, timeout_seconds)
    for text in main_texts:
        if time.monotonic() >= deadline:
            break
        if not (text or "").strip():
            continue
        try:
            found, info = await find_comment_in_list(
                page=page,
                expected_text=text,
                max_scrolls=_REPLY_MAIN_SEARCH_SCROLLS,
                scroll_delay_ms=_REPLY_MAIN_SEARCH_SCROLL_DELAY_MS,
                ensure_open=False,
                strict_scope=True,
            )
        except Exception as exc:
            logger.debug("find_comment_in_list error: %s", exc)
            continue
        if not found:
            # Если контейнера нет — нет смысла перебирать остальные тексты
            # в этой сессии (drawer не открыт глобально). Логируем отдельно,
            # чтобы caller мог отличить от «main точно нет в открытом drawer».
            if (info or {}).get("error") == "comment_list_not_open":
                logger.warning(
                    "[USER_SCHEMA] _find_main_anchor: comment_list_not_open while "
                    "searching for main; aborting anchor scan early"
                )
                break
            continue
        cid = (info or {}).get("comment_id")
        return text, (str(cid) if cid else None)
    return None, None


def _normalize_main_candidates(main_candidates: List[Any]) -> List[PublishedMain]:
    normalized: List[PublishedMain] = []
    for item in main_candidates or []:
        if isinstance(item, PublishedMain):
            if (item.text or "").strip():
                normalized.append(item)
            continue
        text = str(getattr(item, "text", item) or "").strip()
        if not text:
            continue
        normalized.append(
            PublishedMain(
                text=text,
                comment_id=getattr(item, "comment_id", None),
                network_confirmed=bool(getattr(item, "network_confirmed", False)),
                local_confirmed=bool(getattr(item, "local_confirmed", False)),
                method=str(getattr(item, "method", "legacy_text")),
                account_id=int(getattr(item, "account_id", 0) or 0),
                tiktok_username=getattr(item, "tiktok_username", None),
            )
        )
    return normalized


def _reply_parent_search_budget(main_candidates: List[Any]) -> Tuple[float, float, int]:
    """Return total timeout, per-pass timeout, and scroll depth for reply parent search."""
    candidates = _normalize_main_candidates(main_candidates)
    if len(candidates) >= 6:
        return (
            _REPLY_MAIN_SEARCH_DEEP_TIMEOUT_SECONDS,
            _REPLY_MAIN_SEARCH_DEEP_PER_TEXT_SECONDS,
            _REPLY_MAIN_SEARCH_DEEP_SCROLLS,
        )
    return (
        _REPLY_MAIN_SEARCH_TIMEOUT_SECONDS,
        _REPLY_MAIN_SEARCH_PER_TEXT_SECONDS,
        _REPLY_MAIN_SEARCH_SCROLLS,
    )


def _main_parent_key(main: PublishedMain) -> str:
    cid = str(main.comment_id or "").strip()
    if cid:
        return cid
    text_key = _dedup_key(main.text)
    if not text_key:
        return ""
    account_key = str(main.account_id or "").strip()
    if account_key:
        return f"account:{account_key}:text:{text_key}"
    username_key = _dedup_key(main.tiktok_username or "")
    if username_key:
        return f"author:{username_key}:text:{text_key}"
    return text_key


def _main_parent_keys(main: PublishedMain) -> set[str]:
    keys: set[str] = set()
    cid = str(main.comment_id or "").strip()
    if cid:
        keys.add(cid)
    text_key = _dedup_key(main.text)
    if text_key:
        keys.add(text_key)
    primary = _main_parent_key(main)
    if primary:
        keys.add(primary)
    return keys


def _main_submission_keys(main: PublishedMain) -> set[str]:
    """Keys used to decide whether this exact parent already got a reply.

    Text is only a fallback when TikTok did not give us a parent id. In the
    parallel mode the same main text is intentionally posted by several
    accounts, so using text when an id exists would collapse distinct parents.
    """
    cid = str(main.comment_id or "").strip()
    if cid:
        return {cid}
    parent_key = _main_parent_key(main)
    return {parent_key} if parent_key else set()


def _main_exact_identity_keys(main: PublishedMain) -> set[str]:
    """Non-ambiguous aliases for the same expected parent.

    This deliberately excludes bare text because parallel mode posts duplicate
    texts. Account/text and author/text are still exact enough to map a DOM
    anchor back to the logical main even when the known TikTok cid was not used
    during the search.
    """
    keys = set(_main_submission_keys(main))
    primary = _main_parent_key(main)
    if primary:
        keys.add(primary)
    text_key = _dedup_key(main.text)
    if text_key:
        account_key = str(main.account_id or "").strip()
        if account_key:
            keys.add(f"account:{account_key}:text:{text_key}")
        username_key = _dedup_key(main.tiktok_username or "")
        if username_key:
            keys.add(f"author:{username_key}:text:{text_key}")
    return keys


def _main_matches_any_parent_key(main: PublishedMain, keys: set[str]) -> bool:
    if not keys:
        return False
    return (
        not _main_submission_keys(main).isdisjoint(keys)
        or not _main_parent_keys(main).isdisjoint(keys)
    )


def _merge_distinct_main_candidates(candidates: List[PublishedMain]) -> List[PublishedMain]:
    active: List[PublishedMain] = []
    primary_to_index: Dict[str, int] = {}
    compound_to_index: Dict[Tuple[int, str], int] = {}
    for main in _normalize_main_candidates(candidates):
        primary = _main_parent_key(main)
        if not primary:
            continue
        text_key = _dedup_key(main.text)
        compound: Optional[Tuple[int, str]] = None
        if main.account_id is not None and text_key:
            compound = (int(main.account_id), text_key)

        if primary in primary_to_index:
            continue
        if compound is not None and compound in compound_to_index:
            existing_index = compound_to_index[compound]
            existing = active[existing_index]
            if main.comment_id and not existing.comment_id:
                old_primary = _main_parent_key(existing)
                if old_primary:
                    primary_to_index.pop(old_primary, None)
                active[existing_index] = main
                primary_to_index[primary] = existing_index
            continue

        index = len(active)
        active.append(main)
        primary_to_index[primary] = index
        if compound is not None:
            compound_to_index[compound] = index
    return active


def _reply_outcome_parent_key(outcome: ReplyOutcome) -> Optional[str]:
    cid = str(outcome.parent_comment_id or "").strip()
    if cid:
        return cid
    parent_key = str(getattr(outcome, "parent_key", "") or "").strip()
    if parent_key:
        return parent_key
    if outcome.matched_main_text:
        return _dedup_key(outcome.matched_main_text)
    return None


def _looks_like_tiktok_comment_id(value: Any) -> bool:
    text = str(value or "").strip()
    return text.isdigit() and 10 <= len(text) <= 30


def _reply_outcome_parent_keys(outcome: ReplyOutcome) -> set[str]:
    keys: set[str] = set()
    cid = str(outcome.parent_comment_id or "").strip()
    if cid:
        keys.add(cid)
        return keys
    parent_key = str(getattr(outcome, "parent_key", "") or "").strip()
    if parent_key:
        keys.add(parent_key)
        return keys
    if outcome.matched_main_text:
        text_key = _dedup_key(outcome.matched_main_text)
        if text_key:
            keys.add(text_key)
    return keys


def _reply_outcome_submitted(outcome: ReplyOutcome) -> bool:
    return bool(outcome.success or getattr(outcome, "submitted_unconfirmed", False))


def _reply_outcomes_submitted_parent_keys(outcomes: List[ReplyOutcome]) -> set[str]:
    keys: set[str] = set()
    for outcome in outcomes or []:
        if not _reply_outcome_submitted(outcome):
            continue
        keys.update(_reply_outcome_parent_keys(outcome))
    return keys


def _main_is_submitted_for_reply_variant(main: PublishedMain, submitted_keys: set[str]) -> bool:
    """Strict completion check for a specific expected parent.

    `_main_matches_any_parent_key` intentionally accepts broader aliases (for
    search/recovery), including bare text. Parallel mode posts duplicate main
    texts by design, so reply completion must use the exact submission key.
    """
    if not submitted_keys:
        return False
    return not _main_exact_identity_keys(main).isdisjoint(submitted_keys)


def _pending_expected_mains_for_reply_variant(
    main_candidates: List[PublishedMain],
    submitted_keys: set[str],
) -> List[PublishedMain]:
    return [
        main
        for main in _normalize_main_candidates(main_candidates)
        if not _main_is_submitted_for_reply_variant(main, submitted_keys)
    ]


def _main_text_key_counts(main_candidates: List[PublishedMain]) -> Dict[str, int]:
    counts: Dict[str, int] = {}
    for main in _normalize_main_candidates(main_candidates):
        key = _dedup_key(main.text)
        if key:
            counts[key] = counts.get(key, 0) + 1
    return counts


def _find_expected_main_for_reply_outcome(
    outcome: ReplyOutcome,
    main_candidates: List[PublishedMain],
) -> Optional[PublishedMain]:
    candidates = _normalize_main_candidates(main_candidates)
    if not candidates:
        return None

    explicit_keys = {
        str(key).strip()
        for key in (
            outcome.parent_comment_id,
            getattr(outcome, "parent_key", None),
        )
        if str(key or "").strip()
    }
    if explicit_keys:
        for main in candidates:
            if not explicit_keys.isdisjoint(_main_exact_identity_keys(main)):
                return main

    text_key = _dedup_key(outcome.matched_main_text)
    if not text_key:
        return None
    if _main_text_key_counts(candidates).get(text_key, 0) != 1:
        return None
    return next((main for main in candidates if _dedup_key(main.text) == text_key), None)


def _reply_outcomes_submitted_parent_keys_for_expected_mains(
    outcomes: List[ReplyOutcome],
    main_candidates: List[PublishedMain],
) -> set[str]:
    keys: set[str] = set()
    for outcome in outcomes or []:
        if not _reply_outcome_submitted(outcome):
            continue
        main = _find_expected_main_for_reply_outcome(outcome, main_candidates)
        if main is not None:
            keys.update(_main_submission_keys(main))
            continue
        # Do not fall back to bare matched text here. In parallel mode several
        # expected parents intentionally have the same text; a text-only
        # submitted key would make the next retry skip all duplicates.
        for explicit_key in (
            outcome.parent_comment_id,
            getattr(outcome, "parent_key", None),
        ):
            explicit_key = str(explicit_key or "").strip()
            if explicit_key:
                keys.add(explicit_key)
    return keys


def _infer_found_mains_from_reply_outcomes(
    outcomes: List[ReplyOutcome],
    main_candidates: List[PublishedMain],
) -> List[PublishedMain]:
    found: List[PublishedMain] = []
    seen: set[str] = set()
    for outcome in outcomes or []:
        main = _find_expected_main_for_reply_outcome(outcome, main_candidates)
        if main is None:
            continue
        key = _main_parent_key(main)
        if not key or key in seen:
            continue
        found.append(main)
        seen.add(key)
    return found


def _reply_publish_submitted_unconfirmed(result: PublishResult) -> bool:
    if result.status == "success" and bool(result.comment_id):
        return False
    reason = result.error_reason or ""
    if reason not in {
        "stealth_reject_empty_body",
        "no_network_response_after_publish_request",
    }:
        return False
    post_was_enabled = bool(getattr(result, "post_was_enabled_at_click", False))
    network_attempted = bool(
        getattr(result, "network_captured", False)
        or getattr(result, "network_request_attempted", False)
    )
    return post_was_enabled and network_attempted


def _coerce_main_anchor(value: Any, source_main: Optional[PublishedMain] = None) -> MainAnchor:
    if isinstance(value, MainAnchor):
        if value.source_main is None:
            value.source_main = source_main
        if value.parent_key is None and value.source_main is not None:
            value.parent_key = _main_parent_key(value.source_main)
        return value
    if isinstance(value, (tuple, list)):
        matched = value[0] if len(value) > 0 else None
        cid = value[1] if len(value) > 1 else None
        return MainAnchor(
            matched_text=str(matched) if matched else None,
            parent_comment_id=str(cid) if cid else None,
            open_text=str(matched) if matched else None,
            found_by="legacy",
            id_verified=bool(cid),
            source_main=source_main,
            parent_key=_main_parent_key(source_main) if source_main else None,
        )
    return MainAnchor(matched_text=None, parent_comment_id=None, source_main=source_main)


def _main_from_anchor(main: PublishedMain, anchor: MainAnchor) -> PublishedMain:
    text = main.text or anchor.matched_text or ""
    return PublishedMain(
        text=text,
        comment_id=anchor.parent_comment_id or (main.comment_id if anchor.found_by == "id" else None),
        network_confirmed=main.network_confirmed,
        local_confirmed=True,
        method=main.method,
        account_id=main.account_id,
        tiktok_username=anchor.matched_author or main.tiktok_username,
    )


def _build_active_mains(
    text_variants: List[str],
    published_mains: List[PublishedMain],
    *,
    main_account_id: Optional[int] = None,
    main_tiktok_username: Optional[str] = None,
) -> List[PublishedMain]:
    by_key = {
        _dedup_key(p.text): p
        for p in _normalize_main_candidates(published_mains)
        if _dedup_key(p.text)
    }
    active: List[PublishedMain] = []
    used: set[str] = set()
    for text in _dedup_variants(text_variants or []):
        key = _dedup_key(text)
        if not key or key in used:
            continue
        existing = by_key.get(key)
        if existing is not None:
            used.add(key)
            active.append(existing)
    for main in _normalize_main_candidates(published_mains):
        key = _dedup_key(main.text)
        if key and key not in used:
            used.add(key)
            active.append(main)
    return active


def _required_main_texts(text_variants: List[str]) -> List[str]:
    return _dedup_variants([str(text or "").strip() for text in (text_variants or [])])


def _missing_public_main_texts(
    required_texts: List[str],
    confirmed_by_key: Dict[str, PublishedMain],
) -> List[str]:
    missing: List[str] = []
    for text in _required_main_texts(required_texts):
        key = _dedup_key(text)
        if key and key not in confirmed_by_key:
            missing.append(text)
    return missing


def _latest_main_candidates_for_texts(
    published_mains: List[PublishedMain],
    texts: List[str],
) -> List[PublishedMain]:
    wanted_keys = [_dedup_key(text) for text in _required_main_texts(texts)]
    wanted = {key for key in wanted_keys if key}
    latest_by_key: Dict[str, PublishedMain] = {}
    for main in _normalize_main_candidates(published_mains):
        key = _dedup_key(main.text)
        if key and key in wanted:
            latest_by_key[key] = main
    return [latest_by_key[key] for key in wanted_keys if key in latest_by_key]


def _observer_outcomes_are_inconclusive(outcomes: List[str]) -> bool:
    if not outcomes:
        return True
    input_only_empty_count = sum(
        1
        for outcome in outcomes
        if str(outcome or "").strip() == _OBSERVER_INPUT_ONLY_EMPTY_OUTCOME
    )
    if input_only_empty_count >= _OBSERVER_INPUT_ONLY_EMPTY_CONFIRMATIONS:
        return False
    for outcome in outcomes:
        reason = str(outcome or "").strip()
        if reason in _OBSERVER_OPEN_NEGATIVE_OUTCOMES:
            return False
        if reason == "observer_confirmed":
            return False
        if not reason.startswith(_OBSERVER_INCONCLUSIVE_OUTCOME_PREFIXES):
            return False
    return True


def _all_main_candidates_have_publish_evidence(candidates: List[PublishedMain]) -> bool:
    normalized = _normalize_main_candidates(candidates)
    return bool(normalized) and all(
        bool(getattr(main, "comment_id", None)) or bool(getattr(main, "local_confirmed", False))
        for main in normalized
    )


async def _prepare_public_mains_before_replies(
    db: Session,
    video_url: str,
    text_variants: List[str],
    result: UserSchemaResult,
    *,
    task_id: int,
    used_reply_accounts: List[int],
    pool_owner_user_id: Optional[int],
) -> List[PublishedMain]:
    """Collect active main candidates before replies start.

    Network success/comment_id only proves TikTok accepted a request locally.
    Observers are a useful signal, but reply accounts are authoritative for
    this workflow: if a candidate has publish evidence and observers do not
    confirm it, the reply account will search it and publish fallback mains if
    the parent is not visible from that session.
    """
    required_texts = _required_main_texts(text_variants)
    if not required_texts:
        return []

    confirmed_by_key: Dict[str, PublishedMain] = {}
    last_outcomes: List[str] = []

    for round_idx in range(1, max(1, _MAIN_PUBLIC_CONFIRM_ROUNDS) + 1):
        missing_texts = _missing_public_main_texts(required_texts, confirmed_by_key)
        if not missing_texts:
            active = _build_active_mains(
                required_texts,
                list(confirmed_by_key.values()),
                main_account_id=result.main_account_id,
                main_tiktok_username=result.main_tiktok_username,
            )
            result.main_found_by_observer = bool(active)
            return active

        candidates = _latest_main_candidates_for_texts(result.published_mains, missing_texts)
        observer_confirmed: List[PublishedMain] = []
        observer_outcomes: List[str] = []
        if candidates:
            logger.info(
                "Task %s: public main confirmation round %s/%s for %d pending main(s)",
                task_id,
                round_idx,
                _MAIN_PUBLIC_CONFIRM_ROUNDS,
                len(candidates),
            )
            observer_confirmed = await _confirm_mains_by_observers(
                db,
                video_url,
                candidates,
                task_id=task_id,
                used_reply_accounts=used_reply_accounts,
                pool_owner_user_id=pool_owner_user_id,
                reason=f"public_before_reply_r{round_idx}",
                aggregate_outcomes=observer_outcomes,
                require_all=True,
            )
            last_outcomes = list(observer_outcomes)
            for main in observer_confirmed:
                key = _dedup_key(main.text)
                if key:
                    confirmed_by_key[key] = main

        missing_texts = _missing_public_main_texts(required_texts, confirmed_by_key)
        if not missing_texts:
            active = _build_active_mains(
                required_texts,
                list(confirmed_by_key.values()),
                main_account_id=result.main_account_id,
                main_tiktok_username=result.main_tiktok_username,
            )
            result.main_found_by_observer = bool(active)
            return active

        if (
            candidates
            and not observer_confirmed
            and _all_main_candidates_have_publish_evidence(candidates)
        ):
            logger.warning(
                "Task %s: observers did not publicly confirm mains; "
                "skipping pre-reply repair and letting reply accounts decide. outcomes=%s",
                task_id,
                observer_outcomes[:3],
            )
            result.warnings.append(
                {
                    "code": "observer_reply_decides",
                    "message": "Observer did not confirm main comments; reply accounts will search and publish fallback if needed",
                }
            )
            active = _build_active_mains(
                required_texts,
                [*confirmed_by_key.values(), *candidates],
                main_account_id=result.main_account_id,
                main_tiktok_username=result.main_tiktok_username,
            )
            return active

        if candidates and not observer_confirmed and _observer_outcomes_are_inconclusive(observer_outcomes):
            logger.warning(
                "Task %s: public main confirmation inconclusive round %s/%s outcomes=%s; "
                "not treating mains as missing yet",
                task_id,
                round_idx,
                _MAIN_PUBLIC_CONFIRM_ROUNDS,
                observer_outcomes[:3],
            )
            if round_idx < _MAIN_PUBLIC_CONFIRM_ROUNDS:
                await asyncio.sleep(random.uniform(*_OBSERVER_MAIN_PROPAGATION_DELAY_RANGE))
                continue
            result.error = "main_public_confirmation_inconclusive"
            return []

        logger.warning(
            "Task %s: %d/%d required main(s) public-confirmed; repairing missing texts before replies",
            task_id,
            len(required_texts) - len(missing_texts),
            len(required_texts),
        )
        fallback_published = await _run_fallback_main_attempts(
            db,
            video_url,
            missing_texts,
            task_id=task_id,
            used_reply_accounts=used_reply_accounts,
            pool_owner_user_id=pool_owner_user_id,
            max_attempts=_MAIN_PUBLIC_REPAIR_ATTEMPTS_PER_ROUND,
            reason=f"missing_public_main_r{round_idx}",
        )
        if not fallback_published:
            result.error = "main_public_repair_failed"
            return []

        result.fallback_main_via_reply = True
        result.published_mains.extend(fallback_published)
        fallback_username = next((p.tiktok_username for p in fallback_published if p.tiktok_username), None)
        if fallback_username:
            result.main_tiktok_username = fallback_username

        if round_idx < _MAIN_PUBLIC_CONFIRM_ROUNDS:
            await asyncio.sleep(random.uniform(*_OBSERVER_MAIN_PROPAGATION_DELAY_RANGE))

    missing_texts = _missing_public_main_texts(required_texts, confirmed_by_key)
    if missing_texts:
        result.error = (
            "main_public_confirmation_inconclusive"
            if _observer_outcomes_are_inconclusive(last_outcomes)
            else "main_public_confirmation_partial"
        )
        return []

    active = _build_active_mains(
        required_texts,
        list(confirmed_by_key.values()),
        main_account_id=result.main_account_id,
        main_tiktok_username=result.main_tiktok_username,
    )
    result.main_found_by_observer = bool(active)
    return active


async def _find_main_anchor(
    page: Page,
    mains: List[PublishedMain],
    *,
    timeout_seconds: float = _REPLY_MAIN_SEARCH_PER_TEXT_SECONDS,
    max_scrolls: int = _REPLY_MAIN_SEARCH_SCROLLS,
    require_author_match: bool = False,
    allow_author_fallback: bool = False,
) -> MainAnchor:
    """Find one published main comment, preferring known TikTok comment_id."""
    candidates = _normalize_main_candidates(mains)
    deadline = time.monotonic() + max(1.0, timeout_seconds)

    async def _find_visible_comment_list() -> Optional[Any]:
        for selector in [
            '[data-e2e="comment-list"]',
            '[data-e2e="comment-list-container"]',
            '[class*="CommentList"]',
            '[class*="CommentListContainer"]',
            '[class*="DivCommentListContainer"]',
            '[class*="comment-list"]',
            '[class*="comment-list-container"]',
            '[class*="CommentContainer"]',
            'div[class*="CommentContainer"]',
        ]:
            try:
                candidate = await page.query_selector(selector)
                if candidate and await candidate.is_visible():
                    return candidate
            except Exception:
                continue
        return None

    def _attr_value(value: str) -> str:
        return value.replace("\\", "\\\\").replace('"', '\\"')

    comment_list = await _find_visible_comment_list()
    if comment_list is not None:
        for main in candidates:
            if time.monotonic() >= deadline:
                break
            cid = str(main.comment_id or "").strip()
            if not cid:
                continue
            escaped = _attr_value(cid)
            selector = (
                f'[data-cid="{escaped}"], '
                f'[data-comment-id="{escaped}"], '
                f'[data-id="{escaped}"]'
            )
            try:
                root = await comment_list.query_selector(selector)
            except Exception:
                root = None
            if not root:
                continue
            try:
                if not await root.is_visible():
                    try:
                        await root.scroll_into_view_if_needed(timeout=1200)
                        await asyncio.sleep(0.4)
                    except Exception:
                        pass
                if await root.is_visible():
                    logger.info(
                        "[USER_SCHEMA] _find_main_anchor found parent by comment_id=%s",
                        cid[:24],
                    )
                    return MainAnchor(
                        matched_text=main.text,
                        parent_comment_id=cid,
                        open_text=main.text,
                        found_by="id",
                        id_verified=True,
                        source_main=main,
                    )
            except Exception:
                continue

        cid_candidates = [
            main
            for main in candidates
            if str(main.comment_id or "").strip()
        ]
        if cid_candidates and time.monotonic() < deadline:
            # The direct selector above only sees currently-mounted DOM rows.
            # TikTok lazily mounts comment rows while scrolling, so give known
            # CIDs a short scoped scroll pass before falling back to slower text
            # matching.
            cid_scroll_timeout = min(
                max(0.5, deadline - time.monotonic()),
                max(3.0, min(8.0, float(max_scrolls) * 1.25)),
            )
            cid_scrolls = max(1, min(max_scrolls, 6))
            try:
                scrolled_anchors, _scrolled_info = await _find_main_anchors_batch(
                    page,
                    cid_candidates,
                    timeout_seconds=cid_scroll_timeout,
                    max_scrolls=cid_scrolls,
                    require_author_match=require_author_match,
                    allow_author_fallback=allow_author_fallback,
                )
            except Exception as exc:
                logger.debug("[USER_SCHEMA] scrolled cid parent search failed: %s", exc)
                scrolled_anchors = []
            if scrolled_anchors:
                anchor = scrolled_anchors[0]
                logger.info(
                    "[USER_SCHEMA] _find_main_anchor found parent after scrolled %s search: %s",
                    anchor.found_by,
                    str(anchor.parent_comment_id or "")[:24],
                )
                return anchor

    for main in candidates:
        if time.monotonic() >= deadline:
            break
        text = (main.text or "").strip()
        if not text:
            continue
        if main.comment_id:
            logger.info(
                "[USER_SCHEMA] _find_main_anchor cid lookup missed for %s; falling back to text",
                str(main.comment_id)[:24],
            )
        if require_author_match and normalize_author(main.tiktok_username):
            try:
                author_anchors, _author_info = await _find_main_anchors_batch(
                    page,
                    [main],
                    timeout_seconds=max(0.5, deadline - time.monotonic()),
                    max_scrolls=max_scrolls,
                    require_author_match=True,
                    allow_author_fallback=allow_author_fallback,
                )
            except Exception as exc:
                logger.debug("[USER_SCHEMA] author-scoped parent search failed: %s", exc)
                author_anchors = []
            if author_anchors:
                return author_anchors[0]
            continue
        try:
            single_search_timeout = max(0.5, deadline - time.monotonic())
            found, info = await asyncio.wait_for(
                find_comment_in_list(
                    page=page,
                    expected_text=text,
                    max_scrolls=max_scrolls,
                    scroll_delay_ms=_REPLY_MAIN_SEARCH_SCROLL_DELAY_MS,
                    ensure_open=False,
                    strict_scope=True,
                ),
                timeout=single_search_timeout,
            )
        except asyncio.TimeoutError:
            logger.info(
                "[USER_SCHEMA] single parent search timed out after %.1fs; "
                "retrying this reply variant with another account",
                single_search_timeout,
            )
            return MainAnchor(
                matched_text=None,
                parent_comment_id=None,
                error="single_search_timeout",
                source_main=main,
            )
        except Exception as exc:
            logger.debug("find_comment_in_list error: %s", exc)
            continue
        if not found:
            if (info or {}).get("error") == "comment_list_not_open":
                logger.warning(
                    "[USER_SCHEMA] _find_main_anchor: comment_list_not_open while "
                    "searching for main; aborting anchor scan early"
                )
                return MainAnchor(
                    matched_text=None,
                    parent_comment_id=None,
                    error="comment_list_not_open",
                    source_main=main,
                )
            continue
        info = info or {}
        cid = info.get("comment_id")
        raw_text = str(info.get("raw_text") or "").strip() or text
        return MainAnchor(
            matched_text=text,
            parent_comment_id=str(cid) if cid else None,
            open_text=raw_text,
            found_by="text",
            id_verified=False,
            raw_text=raw_text,
            scroll=info.get("scroll"),
            position=info.get("position"),
            source_main=main,
        )
    return MainAnchor(matched_text=None, parent_comment_id=None)


async def _find_main_anchors_batch(
    page: Page,
    mains: List[PublishedMain],
    *,
    timeout_seconds: float = _REPLY_MAIN_SEARCH_PER_TEXT_SECONDS,
    max_scrolls: int = _REPLY_MAIN_SEARCH_SCROLLS,
    require_author_match: bool = False,
    allow_author_fallback: bool = False,
) -> Tuple[List[MainAnchor], Dict[str, Any]]:
    """Find several main comments in one scoped pass through the comment list."""
    candidates = _normalize_main_candidates(mains)
    if not candidates:
        return [], {"skipped": "too_few_candidates"}
    author_match_requested = require_author_match and any(
        normalize_author(main.tiktok_username)
        for main in candidates
    )

    try:
        found_map, info = await asyncio.wait_for(
            find_comments_in_list_batch(
                page=page,
                expected_texts=[m.text for m in candidates],
                expected_comment_ids=[
                    str(m.comment_id).strip() if m.comment_id else None
                    for m in candidates
                ],
                expected_authors=[m.tiktok_username for m in candidates],
                require_author_match=require_author_match,
                collect_author_fallbacks=allow_author_fallback,
                max_scrolls=max_scrolls,
                scroll_delay_ms=_REPLY_MAIN_SEARCH_SCROLL_DELAY_MS,
                ensure_open=False,
                strict_scope=True,
                min_match_ratio=1.0 if author_match_requested else 0.7,
            ),
            timeout=max(1.0, timeout_seconds),
        )
    except asyncio.TimeoutError:
        logger.info(
            "[USER_SCHEMA] batch parent search timed out after %.1fs for %d candidates; "
            "falling back to bounded per-comment search",
            timeout_seconds,
            len(candidates),
        )
        return [], {"error": "batch_search_timeout"}
    except Exception as exc:
        logger.debug("[USER_SCHEMA] batch parent search failed: %s", exc)
        return [], {"error": f"batch_search_exception:{str(exc)[:80]}"}

    anchors: List[MainAnchor] = []
    for idx, hit in sorted(
        (found_map or {}).items(),
        key=lambda item: (
            int((item[1] or {}).get("scroll") or 0),
            int((item[1] or {}).get("position") or 0),
        ),
    ):
        if idx < 0 or idx >= len(candidates):
            continue
        main = candidates[idx]
        raw_text = str((hit or {}).get("raw_text") or "").strip() or main.text
        cid = (hit or {}).get("comment_id")
        matched_by = str((hit or {}).get("matched_by") or "text")
        matched_author = normalize_author((hit or {}).get("author")) or None
        anchors.append(
            MainAnchor(
                matched_text=main.text,
                parent_comment_id=str(cid) if cid else None,
                open_text=raw_text,
                found_by="id" if matched_by == "id" else ("text_author" if matched_by == "text_author" else "text"),
                id_verified=matched_by == "id",
                raw_text=raw_text,
                scroll=(hit or {}).get("scroll"),
                position=(hit or {}).get("position"),
                scroll_top=(hit or {}).get("scroll_top"),
                source_main=main,
                parent_key=_main_parent_key(main),
                requested_author=normalize_author(main.tiktok_username) or None,
                matched_author=matched_author,
            )
        )

    if allow_author_fallback:
        fallback_candidates = (info or {}).get("author_fallback_candidates") or {}
        for idx, items in fallback_candidates.items():
            try:
                target_index = int(idx)
            except (TypeError, ValueError):
                continue
            if target_index < 0 or target_index >= len(candidates):
                continue
            main = candidates[target_index]
            if any(anchor.source_main is main for anchor in anchors):
                continue
            unique: Dict[str, Dict[str, Any]] = {}
            for item in items or []:
                key = str(item.get("comment_id") or "").strip()
                if not key:
                    key = "|".join(
                        str(part or "")
                        for part in (
                            item.get("text_normalized"),
                            normalize_author(item.get("author")),
                            item.get("position"),
                            item.get("scroll_top"),
                        )
                    )
                if key and key not in unique:
                    unique[key] = item
            if len(unique) != 1:
                logger.info(
                    "[USER_SCHEMA] author fallback skipped for parent text=%r: candidates=%d",
                    (main.text or "")[:80],
                    len(unique),
                )
                continue
            item = next(iter(unique.values()))
            raw_text = str(item.get("raw_text") or "").strip() or main.text
            cid = item.get("comment_id")
            anchors.append(
                MainAnchor(
                    matched_text=main.text,
                    parent_comment_id=str(cid) if cid else None,
                    open_text=raw_text,
                    found_by="text_author_fallback",
                    id_verified=False,
                    raw_text=raw_text,
                    scroll=item.get("scroll"),
                    position=item.get("position"),
                    scroll_top=item.get("scroll_top"),
                    source_main=main,
                    parent_key=_main_parent_key(main),
                    requested_author=normalize_author(main.tiktok_username) or None,
                    matched_author=normalize_author(item.get("author")) or None,
                    author_fallback_used=True,
                    author_fallback_reason=str(item.get("fallback_reason") or "requested_author_not_found"),
                )
            )

    if anchors:
        logger.info(
            "[USER_SCHEMA] batch parent search found %d/%d active main(s)",
            len(anchors),
            len(candidates),
        )
    return anchors, info or {}


async def _restore_comment_list_position_for_anchor(
    page: Page,
    anchor: MainAnchor,
    *,
    correlation_id: str,
) -> None:
    """Best-effort repositioning before opening a reply box for a batch hit."""
    if anchor.scroll_top is None:
        return
    try:
        await page.evaluate(
            """
            (targetTop) => {
                const candidates = [
                    ...document.querySelectorAll('[data-smart-id="comment-list-batch-scroll"]'),
                    ...document.querySelectorAll(
                        '[data-smart-id="comment-list-batch-scope"], ' +
                        '[data-e2e="comment-list"], [data-e2e="comment-list-container"], ' +
                        '[class*="CommentList"], [class*="CommentContainer"], [class*="comment-list"]'
                    )
                ];
                const list = candidates.find((el) => {
                    try { return el.scrollHeight > el.clientHeight + 20; } catch (_) { return false; }
                }) || candidates[0];
                if (!list) return false;
                const top = Number(targetTop);
                if (!Number.isFinite(top)) return false;
                list.scrollTop = Math.max(0, top);
                return true;
            }
            """,
            anchor.scroll_top,
        )
        await asyncio.sleep(0.25)
    except Exception as exc:
        logger.debug(
            "[USER_SCHEMA] %s: failed to restore comment-list position for batch anchor: %s",
            correlation_id,
            str(exc)[:120],
        )


async def _verify_reply_visible_near_parent(
    page: Page,
    *,
    reply_text: str,
    parent_text: Optional[str],
    parent_comment_id: Optional[str],
    correlation_id: str,
) -> bool:
    """Best-effort local check for ambiguous reply submits.

    This is intentionally scoped to the already-found parent comment. It is not
    a public observer check; it only prevents burning more accounts when TikTok
    returned a blank/ambiguous response but the reply is visible in the current
    DOM.
    """
    if not (reply_text or "").strip() or not ((parent_text or "").strip() or parent_comment_id):
        return False

    for attempt in range(1, 3):
        await asyncio.sleep(0.8 if attempt == 1 else 1.2)
        try:
            result = await page.evaluate(
                r"""
                ({replyText, parentText, parentId}) => {
                    const normalize = (value) => String(value || '')
                        .replace(/[\u200b-\u200d\ufeff]/g, '')
                        .replace(/\s+/g, ' ')
                        .trim()
                        .toLowerCase();
                    const visible = (el) => {
                        if (!el || !el.getBoundingClientRect) return false;
                        const r = el.getBoundingClientRect();
                        const st = getComputedStyle(el);
                        return r.width > 0 && r.height > 0 && st.display !== 'none' && st.visibility !== 'hidden';
                    };
                    const replyNeedle = normalize(replyText);
                    const parentNeedle = normalize(parentText);
                    const targetId = String(parentId || '').trim();
                    if (!replyNeedle || (!parentNeedle && !targetId)) {
                        return { found: false, reason: 'missing_needles' };
                    }

                    const listSelectors = [
                        '[data-e2e="comment-list"]',
                        '[data-e2e="comment-list-container"]',
                        '[class*="CommentList"]',
                        '[class*="CommentListContainer"]',
                        '[class*="DivCommentListContainer"]',
                        '[class*="comment-list"]',
                        '[class*="comment-list-container"]',
                        'div[class*="CommentContainer"]',
                    ].join(', ');
                    const rootSelector = [
                        '[data-cid]',
                        '[data-comment-id]',
                        '[data-e2e="comment-item"]',
                        '[class*="DivCommentObject"]',
                        '[class*="CommentItem"]',
                        '[class*="DivComment"]',
                        'article',
                        'li',
                    ].join(', ');
                    const mainTextSelector = [
                        'span[data-e2e="comment-level-1"]',
                        '[data-e2e="comment-level-1"]',
                        '[data-e2e*="comment-text"]',
                        'p',
                        'span',
                    ].join(', ');
                    const replyTextSelector = [
                        'span[data-e2e="comment-level-2"]',
                        'span[data-e2e="comment-level-3"]',
                        '[data-e2e="comment-level-2"]',
                        '[data-e2e="comment-level-3"]',
                        '[data-e2e*="reply"] span',
                        '[data-e2e*="reply"] p',
                    ].join(', ');

                    const list = [...document.querySelectorAll(listSelectors)]
                        .filter(visible)
                        .sort((a, b) => b.querySelectorAll(rootSelector).length - a.querySelectorAll(rootSelector).length)[0] || document;
                    const roots = [...list.querySelectorAll(rootSelector)].filter(visible);
                    const textOf = (el) => normalize((el && (el.innerText || el.textContent)) || '');
                    const rootMainText = (root) => {
                        for (const node of [...root.querySelectorAll(mainTextSelector)].filter(visible)) {
                            const text = textOf(node);
                            if (text) return text;
                        }
                        return textOf(root);
                    };
                    const textMatches = (haystack, needle) => {
                        if (!haystack || !needle) return false;
                        if (haystack === needle) return true;
                        if (haystack.length <= Math.max(needle.length * 4, needle.length + 120) && haystack.includes(needle)) return true;
                        return false;
                    };

                    let parentRoot = null;
                    if (targetId) {
                        parentRoot = roots.find(root => {
                            const values = [
                                root.getAttribute('data-cid'),
                                root.getAttribute('data-comment-id'),
                                root.getAttribute('data-id'),
                                root.id,
                            ].filter(Boolean).map(String);
                            return values.includes(targetId) || values.includes(`comment-${targetId}`);
                        }) || null;
                    }
                    if (!parentRoot && parentNeedle) {
                        parentRoot = roots.find(root => textMatches(rootMainText(root), parentNeedle)) || null;
                    }
                    if (!parentRoot) {
                        return { found: false, reason: 'parent_not_found' };
                    }

                    const directReplies = [...parentRoot.querySelectorAll(replyTextSelector)].filter(visible);
                    for (const node of directReplies) {
                        const text = textOf(node);
                        if (textMatches(text, replyNeedle)) {
                            return { found: true, reason: 'reply_inside_parent', text: text.slice(0, 120) };
                        }
                    }

                    const parentTop = parentRoot.getBoundingClientRect().top;
                    let nextMainTop = null;
                    for (const root of roots) {
                        if (root === parentRoot) continue;
                        const r = root.getBoundingClientRect();
                        if (r.top <= parentTop + 4) continue;
                        const text = rootMainText(root);
                        if (!text) continue;
                        nextMainTop = nextMainTop === null ? r.top : Math.min(nextMainTop, r.top);
                    }
                    for (const node of [...list.querySelectorAll(replyTextSelector)].filter(visible)) {
                        const r = node.getBoundingClientRect();
                        if (r.top < parentTop - 4) continue;
                        if (nextMainTop !== null && r.top >= nextMainTop - 2) continue;
                        const text = textOf(node);
                        if (textMatches(text, replyNeedle)) {
                            return { found: true, reason: 'reply_between_parent_and_next_main', text: text.slice(0, 120) };
                        }
                    }
                    return { found: false, reason: 'reply_not_found_under_parent' };
                }
                """,
                {
                    "replyText": reply_text,
                    "parentText": parent_text or "",
                    "parentId": parent_comment_id or "",
                },
            )
        except Exception as exc:
            logger.debug(
                "[USER_SCHEMA] %s reply local verify attempt %s failed: %s",
                correlation_id,
                attempt,
                str(exc)[:120],
            )
            continue

        found = bool(isinstance(result, dict) and result.get("found"))
        _log_user_trace(
            "reply_local_verify_result",
            page=page,
            correlation_id=correlation_id,
            variant_index=None,
            found=found,
            reason=(result or {}).get("reason") if isinstance(result, dict) else None,
            text_preview=(result or {}).get("text") if isinstance(result, dict) else None,
            level=logging.INFO if found else logging.DEBUG,
        )
        if found:
            return True
    return False


async def _observe_main_texts_with_account(
    db: Session,
    account: Account,
    video_url: str,
    main_candidates: List[Any],
    *,
    correlation_id: str,
) -> Tuple[List[str], Optional[str]]:
    """Run the observer probe for one account.

    Returns (found_texts, outcome_reason).
    - found_texts: subset of main_texts that this observer actually saw inside
      a visible comment-list container.
    - outcome_reason is one of:
        * None — success (found_texts non-empty)
        * "observer_open_list_no_target" — drawer открыт с items, наших нет.
          Это самый сильный «реально не опубликовано» сигнал.
        * "observer_comment_list_not_open" — drawer не открылся (UI/proxy/SPA).
          Сигнал слабый: коммент мог быть, observer не справился.
        * "observer_redirected_or_dead" — улетел на /messages/login или page died.
        * "observer_wrong_url" — после navigation не на нужной /video|/photo.
        * "page_create_failed:..." / "dead_session_during_enrichment" — early aborts.
        * "observer_no_main_found" — legacy fallback, не должен срабатывать в норме.
    """
    context = None
    page = None
    unique_candidates: List[PublishedMain] = []
    seen_candidate_keys: set[str] = set()
    for candidate in _normalize_main_candidates(main_candidates):
        key = _dedup_key(candidate.text)
        if not key or key in seen_candidate_keys:
            continue
        seen_candidate_keys.add(key)
        unique_candidates.append(candidate)
    main_texts = [p.text for p in unique_candidates]
    expected_comment_ids = [p.comment_id for p in unique_candidates]
    if not main_texts:
        return [], "observer_no_main_found"
    timeout_ms = _settings.PLAYWRIGHT_TIMEOUT * (2 if account.proxy else 1)
    preflight_deny = await _preflight_cookie_http_gate(
        db,
        account,
        correlation_id=correlation_id,
        phase="observer_pre_browser",
    )
    if preflight_deny:
        return [], preflight_deny
    try:
        context, page = await create_page_for_account(account)
    except Exception as exc:
        _register_runtime_failure(db, account, f"observer_page_create_failed:{str(exc)[:80]}")
        return [], f"page_create_failed:{str(exc)[:80]}"

    try:
        ok, deny = await _ensure_account_login_ready(
            db,
            page,
            account,
            video_url,
            timeout_ms=int(timeout_ms),
            correlation_id=correlation_id,
        )
        if not ok:
            return [], deny

        # ----- Cookie enrichment (то же что и для publisher/reply) -----
        # Без enrichment у observer-аккаунта остаются только sid_guard+msToken.
        # TikTok видит «недо-аутентифицированную» сессию и на /photo/-страницах
        # отдаёт деградированный HTML — comment-секция вообще не рендерится,
        # find_comment_in_list получает root_count=0 (даже <li>/<article> нет).
        # Симметрично подходу в _try_main_account и reply-account flow.
        target_media_url = _current_media_guard_url(page, video_url)

        if needs_enrichment(account):
            enriched_cookies_json = None
            logger.info(
                "[USER_SCHEMA] %s: enriching cookies for observer-account=%s before search",
                correlation_id, account.id,
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
                    correlation_id=f"enrich_observer_{correlation_id}",
                    timeout_seconds=_ENRICHMENT_TIMEOUT_SECONDS,
                )
                enrich_result.log_summary(account.id, correlation_id)
                if enrich_result.redirected:
                    mark_needs_reauth(db, account.id, reason="enrichment_redirect_dead_session")
                    return [], "dead_session_during_enrichment"
                if not enrich_result.success or not enrich_result.all_cookies_json:
                    reason = f"enrichment_failed:{enrich_result.error or 'missing_required_browser_cookies'}"
                    _register_runtime_failure(db, account, reason)
                    return [], reason
                enriched_cookies_json = enrich_result.all_cookies_json
            except Exception as exc:
                reason = f"enrichment_exception:{str(exc)[:80]}"
                logger.warning(
                    "[USER_SCHEMA] %s: observer enrichment raised: %s",
                    correlation_id, str(exc)[:120],
                )
                _register_runtime_failure(db, account, reason)
                return [], reason
            # Enricher уходит на /foryou — возвращаемся на видео.
            try:
                await page.goto(target_media_url, wait_until="domcontentloaded", timeout=int(timeout_ms))
                await asyncio.sleep(1.0)
                target_media_url = _current_media_guard_url(page, target_media_url)
                deny_after_enrichment = await _post_enrichment_deny_reason(
                    db,
                    page,
                    account,
                    correlation_id=correlation_id,
                    phase="observer_post_enrichment",
                )
                if deny_after_enrichment:
                    return [], deny_after_enrichment
                if enriched_cookies_json:
                    save_enriched_cookies_to_db(db, account.id, enriched_cookies_json)
                    account.cookies = enriched_cookies_json
            except Exception as exc:
                logger.warning(
                    "[USER_SCHEMA] %s: observer return-to-video after enrichment failed: %s",
                    correlation_id, str(exc)[:120],
                )
                _register_runtime_failure(db, account, f"observer_post_enrichment_return_failed:{str(exc)[:80]}")
                return [], f"post_enrichment_return_failed:{str(exc)[:80]}"

        await asyncio.sleep(random.uniform(1.0, 2.0))

        logged_out_deny = await _deny_if_visible_logged_out(
            db,
            page,
            account,
            correlation_id=correlation_id,
            phase="observer_pre_search",
        )
        if logged_out_deny:
            return [], logged_out_deny

        # URL-guard перед поиском: redirect на /messages, не на нужной
        # media-странице — отдельные причины, не путаем с "main не виден".
        try:
            current_url_lower = (page.url or "").lower()
        except Exception:
            current_url_lower = ""
        if "/messages" in current_url_lower or "/inbox" in current_url_lower or "/login" in current_url_lower:
            logger.warning(
                "[USER_SCHEMA] %s: observer redirected before search: %s",
                correlation_id, current_url_lower[:120],
            )
            return [], "observer_redirected_or_dead"

        if not is_on_expected_media_url(getattr(page, "url", None), target_media_url):
            logger.warning(
                "[USER_SCHEMA] %s: observer wrong url expected=%s got=%s",
                correlation_id,
                (target_media_url or "")[:100],
                current_url_lower[:120],
            )
            return [], "observer_wrong_url"

        # Жёсткий gate: drawer должен быть открыт И иметь items. Без этого
        # find_comment_in_list матчит сайдбар фид-карточек и выдаёт ложные
        # сигналы (root_count=20 из-за <li>/<article> с cid="one-column-item-N").
        opened = await _open_comment_list_with_safe_retry(
            page,
            target_media_url,
            video_url,
            timeout_ms=int(timeout_ms),
            correlation_id=correlation_id,
            phase="observer_open_comments",
            account_id=account.id,
        )
        _log_user_trace(
            "observer_open_comments_result",
            page=page,
            expected_video_url=target_media_url,
            correlation_id=correlation_id,
            account_id=account.id,
            opened=opened,
        )

        if not opened:
            logged_out_deny = await _deny_if_visible_logged_out(
                db,
                page,
                account,
                correlation_id=correlation_id,
                phase="observer_after_open_failed",
            )
            if logged_out_deny:
                return [], logged_out_deny
            empty_surface = await _empty_comment_surface_summary_for_page(page)
            if empty_surface:
                empty_outcome = _empty_comment_surface_outcome(empty_surface)
                _log_user_trace(
                    empty_outcome,
                    page=page,
                    expected_video_url=target_media_url,
                    correlation_id=correlation_id,
                    account_id=account.id,
                    surface=empty_surface,
                    level=logging.WARNING,
                )
                logger.warning(
                    "[USER_SCHEMA] %s: observer opened an empty comment surface on %s; "
                    "outcome=%s",
                    correlation_id,
                    (page.url or "")[:120],
                    empty_outcome,
                )
                return [], empty_outcome
            logger.warning(
                "[USER_SCHEMA] %s: observer could not open comment list on %s",
                correlation_id,
                (page.url or "")[:120],
            )
            return [], "observer_comment_list_not_open"
        if not is_on_expected_media_url(getattr(page, "url", None), target_media_url):
            logger.warning(
                "[USER_SCHEMA] %s: observer drifted after opening comments expected=%s got=%s",
                correlation_id,
                target_media_url,
                getattr(page, "url", None),
            )
            return [], "observer_wrong_url"

        await _wait_for_searchable_comment_surface(
            page,
            expected_video_url=target_media_url,
            correlation_id=correlation_id,
            phase="observer_parent_search",
            timeout_seconds=3.0,
        )

        found_texts: List[str] = []
        try:
            found_map, batch_info = await find_comments_in_list_batch(
                page=page,
                expected_texts=main_texts,
                expected_comment_ids=expected_comment_ids,
                max_scrolls=_REPLY_MAIN_SEARCH_SCROLLS,
                scroll_delay_ms=_REPLY_MAIN_SEARCH_SCROLL_DELAY_MS,
                ensure_open=False,
                strict_scope=True,
            )
        except Exception as exc:
            logger.debug("[USER_SCHEMA] %s: observer batch find failed: %s", correlation_id, exc)
            found_map, batch_info = {}, {"error": f"observer_batch_find_exception:{str(exc)[:80]}"}

        if (batch_info or {}).get("error") == "comment_list_not_open" and not found_map:
            return [], "observer_comment_list_not_open"

        for idx in sorted(found_map):
            if 0 <= int(idx) < len(main_texts):
                found_texts.append(main_texts[int(idx)])

        if found_texts:
            _log_user_trace(
                "observer_confirmed_mains",
                page=page,
                expected_video_url=target_media_url,
                correlation_id=correlation_id,
                account_id=account.id,
                found_count=len(found_texts),
                expected_count=len(_dedup_variants(main_texts)),
            )
            logger.info(
                "[USER_SCHEMA] %s: observer account=%s confirmed %d/%d main text(s)",
                correlation_id,
                account.id,
                len(found_texts),
                len(_dedup_variants(main_texts)),
            )
            return found_texts, None

        # Drawer был открыт с items, наших текстов в нём нет. Это самый
        # сильный сигнал «реально не опубликовано» — но с поправкой на
        # возможный TikTok account-trust filter (его обработает caller).
        return [], "observer_open_list_no_target"
    finally:
        await _safe_close(context, page)


def _should_bypass_observer_for_replies(
    published_mains: List[PublishedMain],
    observer_outcomes: List[str],
) -> bool:
    """Decide whether reply-фаза должна запуститься, несмотря на то что
    observer не подтвердил main-ы.

    Единственный гейт bypass: у нас есть хотя бы один main с
    `local_confirmed=True` или `comment_id != None`. Без этого нет таргета
    для replies — пытаться бессмысленно.

    Почему не блокируем при `observer_open_list_no_target`:
    эмпирически (см. task 245 + скриншот с телефона) TikTok может показывать
    наши комменты обычным пользователям, но скрывать от других ботов
    (account-trust filter). Observer-бот честно сообщит «список открыт,
    наших нет», но это не доказывает что коммент не существует — это
    доказывает только что данный observer-бот его не видит.

    Reply-аккаунт всё равно делает свой строгий scoped поиск parent в
    [reply_finder.find_comment_by_text_and_author]. Если он не находит
    parent — отдаёт parent_not_found, мы аккуратно выпадаем в fallback.
    Если находит (его trust-tier выше observer-а или фильтр другой) — ответ
    постится. Цена False-bypass-а — пара burn-аккаунтов; выигрыш — катит
    на trust-filter сценарий.
    """
    return any(
        bool(getattr(p, "local_confirmed", False)) or bool(getattr(p, "comment_id", None))
        for p in (published_mains or [])
    )


async def _confirm_mains_by_observers(
    db: Session,
    video_url: str,
    published_mains: List[PublishedMain],
    *,
    task_id: int,
    used_reply_accounts: List[int],
    pool_owner_user_id: Optional[int],
    reason: str,
    max_observers: int = _OBSERVER_MAIN_CONFIRM_ATTEMPTS,
    aggregate_outcomes: Optional[List[str]] = None,
    require_all: bool = False,
) -> List[PublishedMain]:
    """Run up to max_observers observers; return the subset of published_mains
    they actually saw.

    If aggregate_outcomes is provided, the per-observer outcome reason (see
    _observe_main_texts_with_account return contract) is appended to it. This
    lets the caller distinguish between «observer can't open the drawer» and
    «drawer открыт, main реально нет», which require different downstream
    actions (retry observer vs. trigger fallback main vs. trust local-only).
    """
    candidates = [p for p in published_mains if (p.text or "").strip()]
    if not candidates:
        return []

    main_texts = _dedup_variants([p.text for p in candidates])
    confirmed_keys: set[str] = set()
    for attempt in range(1, max(1, max_observers) + 1):
        observer = get_free_account_for_reply(
            db,
            exclude_ids=used_reply_accounts,
            pool_owner_user_id=pool_owner_user_id,
        )
        if not observer:
            logger.warning("Task %s: no free observer accounts for main confirmation", task_id)
            break
        used_reply_accounts.append(observer.id)

        corr = f"task_{task_id}_observer_{reason}_a{attempt}_acc{observer.id}"
        logger.info(
            "Task %s: observer main confirmation %s/%s on account=%s (%s)",
            task_id,
            attempt,
            max_observers,
            observer.id,
            reason,
        )
        found_texts, deny = await _observe_main_texts_with_account(
            db,
            observer,
            video_url,
            candidates,
            correlation_id=corr,
        )
        if aggregate_outcomes is not None:
            aggregate_outcomes.append(deny or "observer_confirmed")
        if found_texts:
            found_keys = {_dedup_key(t) for t in found_texts}
            confirmed_keys.update(key for key in found_keys if key)
            if not require_all or len(confirmed_keys) >= len(main_texts):
                return [p for p in candidates if _dedup_key(p.text) in confirmed_keys]
            logger.info(
                "Task %s: observer account=%s confirmed only %d/%d main text(s); "
                "continuing confirmation",
                task_id,
                observer.id,
                len(confirmed_keys),
                len(main_texts),
            )
            if attempt < max_observers:
                await asyncio.sleep(random.uniform(*_RETRY_ACCOUNT_DELAY_RANGE))
                continue

        observer_outcome = deny or "not_found"
        trace_stage = (
            "observer_open_list_no_target"
            if observer_outcome == "observer_open_list_no_target"
            else "observer_main_not_confirmed"
        )
        _log_user_trace(
            trace_stage,
            expected_video_url=video_url,
            correlation_id=corr,
            account_id=observer.id,
            attempt=attempt,
            max_observers=max_observers,
            reason=reason,
            observer_outcome=observer_outcome,
            expected_count=len(main_texts),
            level=logging.WARNING,
        )
        logger.warning(
            "Task %s: observer account=%s did not see main comments (%s)",
            task_id,
            observer.id,
            observer_outcome,
        )
        if attempt < max_observers:
            await asyncio.sleep(random.uniform(*_RETRY_ACCOUNT_DELAY_RANGE))

    if confirmed_keys:
        return [p for p in candidates if _dedup_key(p.text) in confirmed_keys]
    return []


async def _run_fallback_main_attempts(
    db: Session,
    video_url: str,
    text_variants: List[str],
    *,
    task_id: int,
    used_reply_accounts: List[int],
    pool_owner_user_id: Optional[int],
    max_attempts: int,
    reason: str,
) -> List[PublishedMain]:
    for fallback_attempt in range(1, max(1, max_attempts) + 1):
        fallback_account = get_free_account_for_reply(
            db,
            exclude_ids=used_reply_accounts,
            pool_owner_user_id=pool_owner_user_id,
        )
        if not fallback_account:
            logger.warning(
                "Task %s: no free reply accounts for fallback main (attempt %s)",
                task_id,
                fallback_attempt,
            )
            break
        used_reply_accounts.append(fallback_account.id)
        corr = f"task_{task_id}_fallback_{reason}_a{fallback_attempt}_acc{fallback_account.id}"
        logger.info(
            "Task %s: fallback main attempt %s/%s on reply-account=%s (%s)",
            task_id,
            fallback_attempt,
            max_attempts,
            fallback_account.id,
            reason,
        )
        published, dead_reason = await _try_main_account(
            db,
            fallback_account,
            video_url,
            text_variants,
            correlation_id=corr,
        )
        if published:
            return published
        if not dead_reason:
            _register_runtime_failure(db, fallback_account, "fallback_main_no_publish")
        if fallback_attempt < max_attempts:
            await asyncio.sleep(random.uniform(*_RETRY_ACCOUNT_DELAY_RANGE))

    return []


async def _reply_with_variant_on_all_mains(
    db: Session,
    account: Account,
    video_url: str,
    main_candidates: List[Any],
    reply_text: str,
    *,
    variant_index: int,
    correlation_id: str,
    fallback_main_texts: Optional[List[str]] = None,
    already_replied_parent_keys: Optional[set[str]] = None,
    fallback_allowed: bool = False,
    require_parent_author_match: bool = False,
    allow_parent_author_fallback: bool = False,
) -> ReplyAttemptResult:
    """Один reply-аккаунт пишет один вариант ответа на каждый найденный main."""
    results: List[ReplyOutcome] = []
    found_mains_by_key: Dict[str, PublishedMain] = {}
    attempted_parent_keys: set[str] = set()
    submitted_parent_keys: set[str] = set()
    search_attempted = False
    search_saw_rows = False
    search_inconclusive = False
    main_candidates = _normalize_main_candidates(main_candidates)
    if already_replied_parent_keys is None:
        already_replied_parent_keys = set()
    context = None
    page = None
    timeout_ms = _settings.PLAYWRIGHT_TIMEOUT * (2 if account.proxy else 1)
    preflight_deny = await _preflight_cookie_http_gate(
        db,
        account,
        correlation_id=correlation_id,
        phase="reply_pre_browser",
    )
    if preflight_deny:
        results.append(
            ReplyOutcome(
                text=reply_text,
                variant_index=variant_index,
                matched_main_text=None,
                parent_comment_id=None,
                success=False,
                method=None,
                error=preflight_deny,
                account_id=account.id,
                attempted=False,
            )
        )
        return ReplyAttemptResult(replies=results)
    try:
        context, page = await create_page_for_account(account)
    except Exception as exc:
        _register_runtime_failure(db, account, f"page_create_failed:{str(exc)[:80]}")
        results.append(
            ReplyOutcome(
                text=reply_text,
                variant_index=variant_index,
                matched_main_text=None,
                parent_comment_id=None,
                success=False,
                method=None,
                error=f"page_create_failed:{str(exc)[:80]}",
                account_id=account.id,
            )
        )
        return ReplyAttemptResult(replies=results)

    try:
        ok, deny = await _ensure_account_login_ready(
            db,
            page,
            account,
            video_url,
            timeout_ms=int(timeout_ms),
            correlation_id=correlation_id,
        )
        if not ok:
            results.append(
                ReplyOutcome(
                    text=reply_text,
                    variant_index=variant_index,
                    matched_main_text=None,
                    parent_comment_id=None,
                    success=False,
                    method=None,
                    error=deny,
                    account_id=account.id,
                )
            )
            return ReplyAttemptResult(replies=results)

        # ----- Cookie enrichment (то же что и для main-публикации) -----
        # Reply-аккаунты страдают от тех же проблем: дешёвые куки →
        # bucket=dom_only → TikTok режет UI или ставит pointer-events:none
        # на Reply button. Enrichment поднимает trust score.
        if needs_enrichment(account):
            enriched_cookies_json = None
            logger.info(
                "[USER_SCHEMA] %s: enriching cookies for reply-account=%s",
                correlation_id, account.id,
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
                    correlation_id=f"enrich_reply_{correlation_id}",
                    timeout_seconds=_ENRICHMENT_TIMEOUT_SECONDS,
                )
                enrich_result.log_summary(account.id, correlation_id)
                if enrich_result.redirected:
                    mark_needs_reauth(db, account.id, reason="enrichment_redirect_dead_session")
                    results.append(
                        ReplyOutcome(
                            text=reply_text,
                            variant_index=variant_index,
                            matched_main_text=None,
                            parent_comment_id=None,
                            success=False,
                            method=None,
                            error="dead_session_during_enrichment",
                            account_id=account.id,
                        )
                    )
                    return ReplyAttemptResult(replies=results, searched=True)
                if not enrich_result.success or not enrich_result.all_cookies_json:
                    reason = f"enrichment_failed:{enrich_result.error or 'missing_required_browser_cookies'}"
                    _register_runtime_failure(db, account, reason)
                    results.append(
                        ReplyOutcome(
                            text=reply_text,
                            variant_index=variant_index,
                            matched_main_text=None,
                            parent_comment_id=None,
                            success=False,
                            method=None,
                            error=reason,
                            account_id=account.id,
                            attempted=False,
                        )
                    )
                    return ReplyAttemptResult(replies=results)
                enriched_cookies_json = enrich_result.all_cookies_json
            except Exception as exc:
                reason = f"enrichment_exception:{str(exc)[:80]}"
                logger.warning(
                    "[USER_SCHEMA] %s: reply enrichment raised: %s",
                    correlation_id, str(exc)[:120],
                )
                _register_runtime_failure(db, account, reason)
                results.append(
                    ReplyOutcome(
                        text=reply_text,
                        variant_index=variant_index,
                        matched_main_text=None,
                        parent_comment_id=None,
                        success=False,
                        method=None,
                        error=reason,
                        account_id=account.id,
                        attempted=False,
                    )
                )
                return ReplyAttemptResult(replies=results)
            try:
                await page.goto(video_url, wait_until="domcontentloaded", timeout=int(timeout_ms))
                await asyncio.sleep(1.0)
                deny_after_enrichment = await _post_enrichment_deny_reason(
                    db,
                    page,
                    account,
                    correlation_id=correlation_id,
                    phase="reply_post_enrichment",
                )
                if deny_after_enrichment:
                    results.append(
                        ReplyOutcome(
                            text=reply_text,
                            variant_index=variant_index,
                            matched_main_text=None,
                            parent_comment_id=None,
                            success=False,
                            method=None,
                            error=deny_after_enrichment,
                            account_id=account.id,
                            attempted=False,
                        )
                    )
                    return ReplyAttemptResult(replies=results)
                if enriched_cookies_json:
                    save_enriched_cookies_to_db(db, account.id, enriched_cookies_json)
                    account.cookies = enriched_cookies_json
            except Exception as exc:
                logger.warning(
                    "[USER_SCHEMA] %s: reply return-to-video failed: %s",
                    correlation_id, str(exc)[:120],
                )
                _register_runtime_failure(db, account, f"reply_post_enrichment_return_failed:{str(exc)[:80]}")
                results.append(
                    ReplyOutcome(
                        text=reply_text,
                        variant_index=variant_index,
                        matched_main_text=None,
                        parent_comment_id=None,
                        success=False,
                        method=None,
                        error=f"post_enrichment_return_failed:{str(exc)[:80]}",
                        account_id=account.id,
                        attempted=False,
                    )
                )
                return ReplyAttemptResult(replies=results)

        # ----- Human-like idle delay -----
        target_media_url = _current_media_guard_url(page, video_url)
        _log_user_trace(
            "reply_account_ready",
            page=page,
            expected_video_url=target_media_url,
            correlation_id=correlation_id,
            account_id=account.id,
            variant_index=variant_index,
            main_text_count=len([m for m in main_candidates if (m.text or "").strip()]),
        )
        idle_seconds = random.uniform(2.5, 5.5)
        logger.debug(
            "[USER_SCHEMA] %s: reply human-like idle %.1fs",
            correlation_id, idle_seconds,
        )
        await asyncio.sleep(idle_seconds)

        captcha_reason = await _visible_captcha_reason(page, phase="reply_pre_search")
        if captcha_reason:
            _register_runtime_failure(db, account, captcha_reason)
            results.append(
                ReplyOutcome(
                    text=reply_text,
                    variant_index=variant_index,
                    matched_main_text=None,
                    parent_comment_id=None,
                    success=False,
                    method=None,
                    error=captcha_reason,
                    account_id=account.id,
                    attempted=False,
                )
            )
            return ReplyAttemptResult(replies=results)

        # ----- Behavioral warmup перед reply -----
        logged_out_deny = await _deny_if_visible_logged_out(
            db,
            page,
            account,
            correlation_id=correlation_id,
            phase="reply_pre_search",
        )
        if logged_out_deny:
            results.append(
                ReplyOutcome(
                    text=reply_text,
                    variant_index=variant_index,
                    matched_main_text=None,
                    parent_comment_id=None,
                    success=False,
                    method=None,
                    error=logged_out_deny,
                    account_id=account.id,
                    attempted=False,
                )
            )
            return ReplyAttemptResult(replies=results)

        logger.debug("[USER_SCHEMA] %s: reply behavioral warmup disabled", correlation_id)
        if not await _ensure_page_on_target_media(
            page,
            target_media_url,
            video_url,
            timeout_ms=int(timeout_ms),
            correlation_id=correlation_id,
            phase="reply_pre_search",
        ):
            results.append(
                ReplyOutcome(
                    text=reply_text,
                    variant_index=variant_index,
                    matched_main_text=None,
                    parent_comment_id=None,
                    success=False,
                    method=None,
                    error="url_drift_to_other_video",
                    account_id=account.id,
                )
            )
            return ReplyAttemptResult(replies=results)

        # На каждый из main-текстов пытаемся найти и ответить.
        # Дедуп parent'ов: если TikTok выдал cid → им; если cid нет — нашим
        # ZWJ-safe ключом из _dedup_key. Это важно: эмодзи-семьи с разным
        # ZWJ — разные main-комментарии, мы должны ответить на каждый.
        #
        # Strict open перед parent search: без require_list_with_items=True
        # последующий find_comment_in_list(strict_scope=True) сразу выдаст
        # comment_list_not_open и parent never found. Симметрично observer
        # и tiktok_client_async.post_reply.
        async def _publish_fallback_mains(reason: str) -> ReplyAttemptResult:
            logger.warning(
                "[USER_SCHEMA] %s: reply account=%s did not find any main comment "
                "(%s); using this same account to publish fallback main variants",
                correlation_id,
                account.id,
                reason,
            )
            _log_user_trace(
                "reply_fallback_main_decision",
                page=page,
                expected_video_url=target_media_url,
                correlation_id=correlation_id,
                account_id=account.id,
                variant_index=variant_index,
                reason=reason,
                fallback_text_count=len(fallback_main_texts or []),
                level=logging.WARNING,
            )
            if not await _ensure_page_on_target_media(
                page,
                target_media_url,
                video_url,
                timeout_ms=int(timeout_ms),
                correlation_id=correlation_id,
                phase=f"fallback_main_before_publish:{reason}",
            ):
                return ReplyAttemptResult(
                    replies=results,
                    fallback_mains=[],
                    fallback_attempted=True,
                    fallback_error="url_drift_to_other_video",
                    searched=True,
                )
            fallback_published, fallback_error = await _publish_all_variants_on_account(
                db,
                page,
                account,
                fallback_main_texts or [],
                correlation_id=f"{correlation_id}_fallback_main",
                expected_video_url=target_media_url,
            )
            _log_user_trace(
                "reply_fallback_main_result",
                page=page,
                expected_video_url=target_media_url,
                correlation_id=correlation_id,
                account_id=account.id,
                variant_index=variant_index,
                published_count=len(fallback_published),
                fallback_error=fallback_error,
                level=logging.INFO if fallback_published else logging.WARNING,
            )
            if not results:
                results.append(
                    ReplyOutcome(
                        text=reply_text,
                        variant_index=variant_index,
                        matched_main_text=None,
                        parent_comment_id=None,
                        success=False,
                        method="fallback_main",
                        error=(
                            "no_main_found_fallback_main_published"
                            if fallback_published
                            else f"no_main_found_fallback_failed:{fallback_error or 'unknown'}"
                        ),
                        account_id=account.id,
                    )
                )
            return await _finish_reply_attempt(
                ReplyAttemptResult(
                    replies=results,
                    fallback_mains=fallback_published,
                    fallback_attempted=True,
                    fallback_error=fallback_error,
                    searched=True,
                )
            )

        async def _finish_reply_attempt(result: ReplyAttemptResult) -> ReplyAttemptResult:
            if result.submitted_parent_keys_count <= 0:
                result.submitted_parent_keys_count = sum(
                    1 for outcome in result.replies if _reply_outcome_submitted(outcome)
                )
            if result.fallback_mains or any(_reply_outcome_submitted(outcome) for outcome in result.replies):
                await _persist_cookies_after_success(
                    db,
                    page,
                    account,
                    scope="reply",
                    correlation_id=correlation_id,
                )
            return result

        if not await _ensure_page_on_target_media(
            page,
            target_media_url,
            video_url,
            timeout_ms=int(timeout_ms),
            correlation_id=correlation_id,
            phase="before_open_comments_for_parent_search",
        ):
            results.append(
                ReplyOutcome(
                    text=reply_text,
                    variant_index=variant_index,
                    matched_main_text=None,
                    parent_comment_id=None,
                    success=False,
                    method=None,
                    error="url_drift_to_other_video",
                    account_id=account.id,
                )
            )
            return ReplyAttemptResult(replies=results)

        comments_ready_for_search = await _open_comment_list_with_safe_retry(
            page,
            target_media_url,
            video_url,
            timeout_ms=int(timeout_ms),
            correlation_id=correlation_id,
            phase="reply_open_comments",
            account_id=account.id,
            variant_index=variant_index,
        )
        _log_user_trace(
            "reply_open_comments_result",
            page=page,
            expected_video_url=target_media_url,
            correlation_id=correlation_id,
            account_id=account.id,
            variant_index=variant_index,
            opened=comments_ready_for_search,
        )
        if not comments_ready_for_search:
            logger.warning(
                "[USER_SCHEMA] %s: reply-account=%s comment_list_not_open before "
                "parent search; retrying this reply variant with another account",
                correlation_id, account.id,
            )
            results.append(
                ReplyOutcome(
                    text=reply_text,
                    variant_index=variant_index,
                    matched_main_text=None,
                    parent_comment_id=None,
                    success=False,
                    method=None,
                    error="comment_list_not_open",
                    account_id=account.id,
                    attempted=False,
                )
            )
            return await _finish_reply_attempt(ReplyAttemptResult(replies=results, searched=False))
        if not await _ensure_page_on_target_media(
            page,
            target_media_url,
            video_url,
            timeout_ms=int(timeout_ms),
            correlation_id=correlation_id,
            phase="after_open_comments_for_parent_search",
        ):
            results.append(
                ReplyOutcome(
                    text=reply_text,
                    variant_index=variant_index,
                    matched_main_text=None,
                    parent_comment_id=None,
                    success=False,
                    method=None,
                    error="url_drift_to_other_video",
                    account_id=account.id,
                )
            )
            return await _finish_reply_attempt(ReplyAttemptResult(replies=results, searched=True))
        surface_search_ready = await _wait_for_searchable_comment_surface(
            page,
            expected_video_url=target_media_url,
            correlation_id=correlation_id,
            phase="reply_parent_search",
            timeout_seconds=3.0,
        )
        if surface_search_ready:
            search_saw_rows = True

        sent_for_parents: set[str] = set(already_replied_parent_keys)
        remaining_mains = [
            m
            for m in main_candidates
            if (m.text or "").strip() and not _main_matches_any_parent_key(m, already_replied_parent_keys)
        ]
        skipped_parent_count = len(main_candidates) - len(remaining_mains)
        if skipped_parent_count:
            _log_user_trace(
                "reply_variant_skipped_already_answered_parents",
                page=page,
                expected_video_url=target_media_url,
                correlation_id=correlation_id,
                account_id=account.id,
                variant_index=variant_index,
                skipped_parent_count=skipped_parent_count,
                pending_parent_count=len(remaining_mains),
            )
        search_timeout_seconds, search_pass_seconds, search_scrolls = _reply_parent_search_budget(
            remaining_mains
        )
        search_deadline = time.monotonic() + search_timeout_seconds

        while remaining_mains and time.monotonic() < search_deadline:
            found_this_pass = False
            batch_anchors_by_key: Dict[str, MainAnchor] = {}
            if remaining_mains:
                batch_remaining_seconds = search_deadline - time.monotonic()
                if batch_remaining_seconds > 1.0:
                    search_attempted = True
                    batch_anchors, batch_info = await _find_main_anchors_batch(
                        page,
                        remaining_mains,
                        timeout_seconds=min(
                            search_pass_seconds,
                            max(1.0, batch_remaining_seconds),
                        ),
                        max_scrolls=search_scrolls,
                        require_author_match=require_parent_author_match,
                        allow_author_fallback=allow_parent_author_fallback,
                    )
                    batch_info = batch_info or {}
                    if int(batch_info.get("max_seen_rows") or 0) > 0 or int(batch_info.get("last_row_count") or 0) > 0:
                        search_saw_rows = True
                    if _reply_parent_search_error_is_inconclusive(batch_info.get("error")):
                        search_inconclusive = True
                    if (
                        batch_info.get("error") == "comment_list_not_open"
                        and not batch_anchors
                        and len(remaining_mains) > 1
                    ):
                        if surface_search_ready:
                            _log_user_trace(
                                "reply_batch_list_not_open_after_visible_surface",
                                page=page,
                                expected_video_url=target_media_url,
                                correlation_id=correlation_id,
                                account_id=account.id,
                                variant_index=variant_index,
                                remaining_main_count=len(remaining_mains),
                                level=logging.INFO,
                            )
                        else:
                            results.append(
                                ReplyOutcome(
                                    text=reply_text,
                                    variant_index=variant_index,
                                    matched_main_text=None,
                                    parent_comment_id=None,
                                    success=False,
                                    method=None,
                                    error="comment_list_not_open",
                                    account_id=account.id,
                                    attempted=False,
                                )
                            )
                            return await _finish_reply_attempt(
                                ReplyAttemptResult(
                                    replies=results,
                                    found_mains=list(found_mains_by_key.values()),
                                    attempted_parent_keys=attempted_parent_keys,
                                    searched=bool(found_mains_by_key),
                                )
                            )
                    elif batch_anchors:
                        for batch_anchor in batch_anchors:
                            if batch_anchor.source_main is None:
                                continue
                            batch_key = _main_parent_key(batch_anchor.source_main)
                            if batch_key:
                                batch_anchors_by_key[batch_key] = batch_anchor
            for main in list(remaining_mains):
                main_text = main.text
                remaining_seconds = search_deadline - time.monotonic()
                if remaining_seconds <= 0:
                    break
                if not await _ensure_page_on_target_media(
                    page,
                    target_media_url,
                    video_url,
                    timeout_ms=int(timeout_ms),
                    correlation_id=correlation_id,
                    phase="before_find_main_anchor",
                ):
                    results.append(
                        ReplyOutcome(
                            text=reply_text,
                            variant_index=variant_index,
                            matched_main_text=None,
                            parent_comment_id=None,
                            success=False,
                            method=None,
                            error="url_drift_to_other_video",
                            account_id=account.id,
                        )
                    )
                    return await _finish_reply_attempt(ReplyAttemptResult(replies=results, searched=True))
                anchor = batch_anchors_by_key.pop(_main_parent_key(main), None)
                if anchor is None:
                    search_attempted = True
                    anchor = _coerce_main_anchor(
                        await _find_main_anchor(
                            page,
                            [main],
                            timeout_seconds=min(
                                search_pass_seconds,
                                max(1.0, remaining_seconds),
                            ),
                            max_scrolls=search_scrolls,
                            require_author_match=require_parent_author_match,
                            allow_author_fallback=allow_parent_author_fallback,
                        ),
                        main,
                    )
                anchor_text = anchor.matched_text
                parent_id = anchor.parent_comment_id
                if _reply_parent_search_error_is_inconclusive(anchor.error):
                    search_inconclusive = True
                if anchor.error == "comment_list_not_open":
                    if surface_search_ready:
                        _log_user_trace(
                            "reply_single_list_not_open_after_visible_surface",
                            page=page,
                            expected_video_url=target_media_url,
                            correlation_id=correlation_id,
                            account_id=account.id,
                            variant_index=variant_index,
                            main_text_preview=main_text[:80],
                            level=logging.INFO,
                        )
                        continue
                    results.append(
                        ReplyOutcome(
                            text=reply_text,
                            variant_index=variant_index,
                            matched_main_text=None,
                            parent_comment_id=None,
                            success=False,
                            method=None,
                            error="comment_list_not_open",
                            account_id=account.id,
                            attempted=False,
                        )
                    )
                    return await _finish_reply_attempt(
                        ReplyAttemptResult(
                            replies=results,
                            found_mains=list(found_mains_by_key.values()),
                            attempted_parent_keys=attempted_parent_keys,
                            searched=bool(found_mains_by_key or attempted_parent_keys),
                        )
                    )
                if not anchor_text:
                    _log_user_trace(
                        "reply_parent_not_found_for_text",
                        page=page,
                        expected_video_url=target_media_url,
                        correlation_id=correlation_id,
                        account_id=account.id,
                        variant_index=variant_index,
                        main_text_preview=main_text[:80],
                    )
                    continue
                found_this_pass = True
                _log_user_trace(
                    "reply_parent_found",
                    page=page,
                    expected_video_url=target_media_url,
                    correlation_id=correlation_id,
                    account_id=account.id,
                    variant_index=variant_index,
                    parent_id=parent_id,
                    anchor_preview=anchor_text[:80],
                )
                try:
                    remaining_mains.remove(main)
                except ValueError:
                    pass
                found_main = _main_from_anchor(main, anchor)
                anchor_text_key = _dedup_key(anchor_text)
                found_key = str(
                    found_main.comment_id
                    or parent_id
                    or _main_parent_key(found_main)
                ).strip()
                if found_key:
                    found_mains_by_key[found_key] = found_main
                verified_main_id = main.comment_id if anchor.found_by == "id" else None
                dedup_key = str(
                    parent_id
                    or found_main.comment_id
                    or verified_main_id
                    or anchor_text_key
                    or _main_parent_key(found_main)
                ).strip()
                if dedup_key in sent_for_parents:
                    _log_user_trace(
                        "reply_parent_already_handled",
                        page=page,
                        expected_video_url=target_media_url,
                        correlation_id=correlation_id,
                        account_id=account.id,
                        variant_index=variant_index,
                        parent_key=dedup_key[:80],
                        parent_id=parent_id,
                        anchor_preview=anchor_text[:80],
                    )
                    continue
                sent_for_parents.add(dedup_key)

                sub_corr = f"{correlation_id}_to_{(parent_id or 'text')[:20]}"
                open_parent_text = anchor.open_text or anchor_text
                open_parent_id = parent_id if (
                    anchor.found_by == "id"
                    or (
                        anchor.found_by == "text_author_fallback"
                        and _looks_like_tiktok_comment_id(parent_id)
                    )
                ) else None
                try:
                    if not await _ensure_page_on_target_media(
                        page,
                        target_media_url,
                        video_url,
                        timeout_ms=int(timeout_ms),
                        correlation_id=sub_corr,
                        phase="before_reply_publish",
                    ):
                        results.append(
                            ReplyOutcome(
                                text=reply_text,
                                variant_index=variant_index,
                                matched_main_text=anchor_text,
                                parent_comment_id=parent_id,
                                success=False,
                                method=None,
                                error="url_drift_to_other_video",
                                account_id=account.id,
                                attempted=False,
                            )
                        )
                        continue
                    logged_out_deny = await _deny_if_visible_logged_out(
                        db,
                        page,
                        account,
                        correlation_id=sub_corr,
                        phase="before_reply_publish",
                    )
                    if logged_out_deny:
                        results.append(
                            ReplyOutcome(
                                text=reply_text,
                                variant_index=variant_index,
                                matched_main_text=anchor_text,
                                parent_comment_id=parent_id,
                                success=False,
                                method=None,
                                error=logged_out_deny,
                                account_id=account.id,
                                attempted=False,
                            )
                        )
                        return await _finish_reply_attempt(
                            ReplyAttemptResult(
                                replies=results,
                                found_mains=list(found_mains_by_key.values()),
                                attempted_parent_keys=attempted_parent_keys,
                                searched=True,
                            )
                        )
                    if dedup_key:
                        attempted_parent_keys.add(dedup_key)
                    await _restore_comment_list_position_for_anchor(
                        page,
                        anchor,
                        correlation_id=sub_corr,
                    )
                    publish_result = await publish_reply_hybrid(
                        page,
                        reply_text,
                        parent_comment_text=open_parent_text,
                        parent_comment_id=open_parent_id,
                        max_total_seconds=25.0,
                        correlation_id=sub_corr,
                        expected_video_url=target_media_url,
                    )
                except Exception as exc:
                    results.append(
                        ReplyOutcome(
                            text=reply_text,
                            variant_index=variant_index,
                            matched_main_text=anchor_text,
                            parent_comment_id=parent_id,
                            success=False,
                            method=None,
                            error=f"publish_exception:{str(exc)[:80]}",
                            account_id=account.id,
                            attempted=True,
                        )
                    )
                    continue

                success = publish_result.status == "success" and bool(publish_result.comment_id)
                submitted_unconfirmed = _reply_publish_submitted_unconfirmed(publish_result)
                submitted = success or submitted_unconfirmed
                if submitted:
                    submitted_keys = {
                        str(key).strip()
                        for key in (
                            parent_id,
                            found_main.comment_id,
                            main.comment_id if anchor.found_by == "id" else None,
                        )
                        if str(key or "").strip()
                    }
                    if not submitted_keys:
                        anchor_key = dedup_key or _main_parent_key(found_main)
                        if anchor_key:
                            submitted_keys.add(anchor_key)
                    already_replied_parent_keys.update(submitted_keys)
                    submitted_parent_keys.update(submitted_keys)

                results.append(
                    ReplyOutcome(
                        text=reply_text,
                        variant_index=variant_index,
                        matched_main_text=anchor_text,
                        parent_comment_id=parent_id,
                        success=success,
                        method=publish_result.dom_strategy or "hybrid_dom",
                        error=None if success else publish_result.error_reason,
                        account_id=account.id,
                        attempted=True,
                        submitted_unconfirmed=submitted_unconfirmed,
                        parent_key=dedup_key or None,
                        requested_parent_author=anchor.requested_author,
                        matched_parent_author=anchor.matched_author,
                        parent_author_match_mode=anchor.found_by,
                        author_fallback_used=anchor.author_fallback_used,
                        author_fallback_reason=anchor.author_fallback_reason,
                    )
                )
                _log_user_trace(
                    "reply_publish_result",
                    page=page,
                    expected_video_url=target_media_url,
                    correlation_id=sub_corr,
                    account_id=account.id,
                    variant_index=variant_index,
                    success=success,
                    status=publish_result.status,
                    reason=publish_result.error_reason,
                    parent_id=parent_id,
                    parent_key=dedup_key[:80],
                    anchor_preview=anchor_text[:80],
                    local_confirmed=False,
                    submitted_unconfirmed=submitted_unconfirmed,
                    comment_id=publish_result.comment_id,
                    level=logging.INFO if submitted else logging.WARNING,
                )
                if success:
                    _register_runtime_success(db, account, scope="reply")
                elif submitted_unconfirmed:
                    logger.info(
                        "[USER_SCHEMA] %s reply submitted_unconfirmed after %s; not retrying this parent",
                        sub_corr,
                        publish_result.error_reason or publish_result.status,
                    )
                else:
                    _register_runtime_failure(db, account, f"reply_failed:{publish_result.error_reason or 'unknown'}")
                    if _is_critical_publish_failure(publish_result):
                        logger.warning(
                            "[USER_SCHEMA] %s: critical reply failure (%s), switching account",
                            sub_corr,
                            publish_result.error_reason,
                        )
                        return await _finish_reply_attempt(
                            ReplyAttemptResult(
                                replies=results,
                                found_mains=list(found_mains_by_key.values()),
                                attempted_parent_keys=attempted_parent_keys,
                                searched=True,
                            )
                        )

                await asyncio.sleep(random.uniform(*_INTER_REPLY_DELAY_RANGE))

            if not found_this_pass and remaining_mains:
                break

        if not found_mains_by_key:
            own_confident_empty = bool(
                comments_ready_for_search
                and surface_search_ready
                and search_attempted
                and search_saw_rows
                and not search_inconclusive
            )
            if fallback_main_texts and fallback_allowed and own_confident_empty:
                return await _publish_fallback_mains("parent_not_found")
            return await _finish_reply_attempt(
                ReplyAttemptResult(
                    replies=results,
                    found_mains=[],
                    attempted_parent_keys=attempted_parent_keys,
                    searched=True,
                    confident_empty_search=own_confident_empty,
                )
            )

        return await _finish_reply_attempt(
            ReplyAttemptResult(
                replies=results,
                found_mains=list(found_mains_by_key.values()),
                attempted_parent_keys=attempted_parent_keys,
                searched=True,
            )
        )
    finally:
        await _safe_close(context, page)


# ---------------------------------------------------------------------------
# Boost likes
# ---------------------------------------------------------------------------

async def _maybe_order_likes(
    result: UserSchemaResult,
    *,
    video_url: str,
    task_id: int,
    boost_enabled: bool,
    boost_quantity: int,
) -> None:
    if not boost_enabled:
        return
    if not result.main_found_by_observer:
        logger.info(
            "Task %s: likes skipped — main not confirmed by observer",
            task_id,
        )
        return

    # Prefer the currently active main author. Fallback mains can replace the
    # working parent set after older mains were not found by reply accounts.
    username: Optional[str] = result.main_tiktok_username
    for p in result.published_mains:
        if username:
            break
        if p.tiktok_username:
            username = p.tiktok_username
            break
    if not username:
        # Не дёргаем Boost с account.label — он не TikTok username.
        result.boost_error = "tiktok_username_unknown"
        logger.warning(
            "Task %s: likes not ordered — real TikTok username unknown for the comment author",
            task_id,
        )
        return

    from services.boost_project_client import order_tiktok_comment_likes, is_boost_enabled
    if not is_boost_enabled():
        return

    ok, order_id, err = await order_tiktok_comment_likes(
        video_url,
        username,
        quantity=boost_quantity,
    )
    if ok:
        result.boost_order_id = order_id
        logger.info("Task %s: likes order created id=%s qty=%s", task_id, order_id, boost_quantity)
    else:
        result.boost_error = err or "unknown_boost_error"
        logger.warning("Task %s: likes order failed: %s", task_id, err)


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

@dataclass
class _ParallelMainSlotResult:
    slot_index: int
    text: str
    published: List[PublishedMain] = field(default_factory=list)
    remaining_texts: List[str] = field(default_factory=list)
    resolved_video_url: Optional[str] = None
    attempts: int = 0
    error: Optional[str] = None


@dataclass
class _ParallelReplyVariantResult:
    variant_index: int
    text: str
    replies: List[ReplyOutcome] = field(default_factory=list)
    found_mains: List[PublishedMain] = field(default_factory=list)
    completed: bool = False
    found_any_main: bool = False
    no_main_found: bool = False
    error: Optional[str] = None
    account_id: Optional[int] = None


def _record_main_retry_failure(db: Session, account: Account, dead_reason: Optional[str]) -> None:
    if not dead_reason:
        _register_runtime_failure(db, account, "no_publish_after_login_ok")
        return
    if not dead_reason.startswith((
        "dead_session:",
        "login_ambiguous:",
        "page_create_failed:",
        "captcha_challenge:",
        "captcha_challenge_timeout:",
        "blocked_ui:",
        "on_video_empty_dom_after_settle",
    )):
        _register_runtime_failure(db, account, f"publish_failed:{dead_reason}"[:200])


def _reply_failure_outcome(
    *,
    reply_text: str,
    variant_index: int,
    account_id: Optional[int],
    error: str,
) -> ReplyOutcome:
    return ReplyOutcome(
        text=reply_text,
        variant_index=variant_index,
        matched_main_text=None,
        parent_comment_id=None,
        success=False,
        method=None,
        error=error,
        account_id=account_id,
    )


async def execute_user_schema_parallel(
    db: Session,
    task: Task,
    video_url: str,
    text_variants: List[str],
    task_id: int,
    *,
    boost_enabled: bool,
    boost_quantity: int,
    pool_owner_user_id: Optional[int] = None,
    stop_requested: Optional[Callable[[], bool]] = None,
) -> UserSchemaResult:
    """Parallel commenting mode.

    This orchestrates account/browser concurrency only. The actual main and
    reply publication functions are the same ones used by the old workflow.
    """
    from db.database import get_sync_db_context

    started = time.monotonic()
    result = UserSchemaResult(boost_enabled=boost_enabled, boost_quantity=boost_quantity)
    parallel_main_account_slots = 3

    def _is_stop_requested() -> bool:
        if stop_requested is None:
            return False
        try:
            return bool(stop_requested())
        except Exception as exc:
            logger.debug("Task %s: stop callback failed: %s", task_id, exc)
            return False

    def _mark_stopped() -> UserSchemaResult:
        result.error = "stopped_by_user"
        result.elapsed_seconds = time.monotonic() - started
        return result

    async def _close_parallel_phase_browsers(phase: str) -> None:
        try:
            from executor.browser_pool import close_browser_pool

            await close_browser_pool()
            logger.info("Task %s: parallel %s browser pool closed", task_id, phase)
        except Exception as exc:
            logger.warning(
                "Task %s: failed to close browser pool after parallel %s: %s",
                task_id,
                phase,
                str(exc)[:120],
            )

    def _ensure_parallel_browser_capacity() -> bool:
        try:
            from executor.browser_pool import ensure_thread_browser_pool_capacity

            ensure_thread_browser_pool_capacity(3)
            return True
        except Exception as exc:
            logger.warning(
                "Task %s: failed to raise browser pool capacity for parallel mode: %s",
                task_id,
                str(exc)[:120],
            )
            return False

    if _is_stop_requested():
        logger.info("Task %s: user stop detected before parallel user-schema work", task_id)
        return _mark_stopped()

    text_variants = [v for v in (text_variants or []) if (v or "").strip()]
    main_texts = _required_main_texts(text_variants)[:3]
    if not main_texts:
        result.error = "no_text_variants"
        result.elapsed_seconds = time.monotonic() - started
        return result
    result.expected_main_publications = len(main_texts) * parallel_main_account_slots
    reply_variants: List[str] = _reply_variants_from_task(task)
    result.expected_reply_variants = len(reply_variants)

    def _positive_int_setting(name: str, default: int) -> int:
        try:
            return max(1, int(getattr(_settings, name, default) or default))
        except Exception:
            return max(1, int(default))

    retry_account_limit = _positive_int_setting("MAX_RETRY_ACCOUNTS", 30)
    max_main_attempts = min(
        retry_account_limit,
        _positive_int_setting("PARALLEL_COMMENTING_MAX_MAIN_ATTEMPTS", retry_account_limit),
    )
    max_reply_attempts = min(
        retry_account_limit,
        _positive_int_setting("PARALLEL_COMMENTING_MAX_REPLY_ATTEMPTS", retry_account_limit),
    )
    logger.info(
        "Task %s: parallel commenting limits main_attempts=%s reply_attempts=%s retry_account_limit=%s",
        task_id,
        max_main_attempts,
        max_reply_attempts,
        retry_account_limit,
    )
    main_selection_lock = asyncio.Lock()
    reply_selection_lock = asyncio.Lock()
    tried_main_ids: set[int] = set()
    used_reply_ids: set[int] = set()

    async def _select_main_account(attempt_db: Session) -> Optional[Account]:
        async with main_selection_lock:
            candidate = get_free_account_for_main_comment(
                attempt_db,
                exclude_ids=list(tried_main_ids),
                pool_owner_user_id=pool_owner_user_id,
            )
            if candidate:
                tried_main_ids.add(candidate.id)
            return candidate

    async def _run_main_slot_attempt(
        slot_index: int,
        slot_texts: List[str],
        attempt: int,
    ) -> _ParallelMainSlotResult:
        slot_texts = [text for text in (slot_texts or []) if (text or "").strip()]
        slot_result = _ParallelMainSlotResult(
            slot_index=slot_index,
            text=" | ".join(slot_texts),
            remaining_texts=list(slot_texts),
        )
        if _is_stop_requested():
            slot_result.error = "stopped_by_user"
            return slot_result
        if not slot_texts:
            slot_result.error = "main_slot_no_texts"
            slot_result.remaining_texts = []
            return slot_result
        with get_sync_db_context() as attempt_db:
            candidate = await _select_main_account(attempt_db)
            if not candidate:
                slot_result.error = "no_free_main_accounts"
                return slot_result
            slot_result.attempts = attempt
            corr = f"task_{task_id}_parallel_main_v{slot_index}_a{attempt}_acc{candidate.id}"
            logger.info(
                "Task %s: parallel main account slot %s/3 attempt %s/%s on account=%s texts=%s",
                task_id,
                slot_index,
                attempt,
                max_main_attempts,
                candidate.id,
                len(slot_texts),
            )
            resolved_video_url_holder: Dict[str, str] = {}
            try:
                try:
                    published, dead_reason = await _try_main_account(
                        attempt_db,
                        candidate,
                        video_url,
                        slot_texts,
                        correlation_id=corr,
                        resolved_video_url_holder=resolved_video_url_holder,
                    )
                except Exception as exc:
                    published = []
                    dead_reason = f"main_attempt_exception:{str(exc)[:80]}"
                    logger.warning(
                        "Task %s: parallel main slot %s account=%s raised: %s",
                        task_id,
                        slot_index,
                        candidate.id,
                        str(exc)[:120],
                    )
            finally:
                try:
                    release_main_account_reservation(candidate.id)
                except Exception as exc:
                    logger.warning(
                        "Task %s: failed to release parallel main account reservation for account=%s: %s",
                        task_id,
                        candidate.id,
                        str(exc)[:120],
                    )
            if published:
                slot_result.published = published
                slot_result.resolved_video_url = resolved_video_url_holder.get("video_url")
                published_text_keys = {_dedup_key(item.text) for item in published if (item.text or "").strip()}
                slot_result.remaining_texts = [
                    text
                    for text in slot_texts
                    if _dedup_key(text) not in published_text_keys
                ]
                if not slot_result.remaining_texts:
                    return slot_result
                slot_result.error = dead_reason or "main_slot_partially_published"
                return slot_result

            slot_result.error = dead_reason or "main_not_published"
            try:
                _record_main_retry_failure(attempt_db, candidate, dead_reason)
            except Exception as exc:
                logger.warning(
                    "Task %s: failed to record parallel main retry failure for account=%s: %s",
                    task_id,
                    candidate.id,
                    str(exc)[:120],
                )
        return slot_result

    max_empty_main_batches_before_fail = 2
    pending_main_slots: List[Tuple[int, List[str]]] = [
        (slot_index, list(main_texts))
        for slot_index in range(1, parallel_main_account_slots + 1)
    ]
    main_attempts_by_slot: Dict[int, int] = {}
    main_published_by_slot: Dict[int, List[PublishedMain]] = {}
    main_results_by_slot: Dict[int, _ParallelMainSlotResult] = {}
    main_failures_by_slot: Dict[int, _ParallelMainSlotResult] = {}
    empty_main_batches_without_any_publication = 0

    while pending_main_slots and not _is_stop_requested():
        batch = pending_main_slots[:3]
        pending_main_slots = pending_main_slots[3:]
        for slot_index, _slot_texts in batch:
            main_attempts_by_slot[slot_index] = main_attempts_by_slot.get(slot_index, 0) + 1
        if not _ensure_parallel_browser_capacity():
            result.error = "parallel_browser_capacity_unavailable"
            result.elapsed_seconds = time.monotonic() - started
            return result
        try:
            raw_batch_results = await asyncio.gather(
                *[
                    _run_main_slot_attempt(
                        slot_index,
                        main_text,
                        main_attempts_by_slot[slot_index],
                    )
                    for slot_index, main_text in batch
                ],
                return_exceptions=True,
            )
        finally:
            await _close_parallel_phase_browsers("main_batch")

        batch_results: List[_ParallelMainSlotResult] = []
        for (slot_index, slot_texts), raw_result in zip(batch, raw_batch_results):
            if isinstance(raw_result, BaseException):
                if isinstance(raw_result, (asyncio.CancelledError, KeyboardInterrupt, SystemExit)):
                    raise raw_result
                logger.warning(
                    "Task %s: parallel main slot %s batch task raised outside attempt wrapper: %s",
                    task_id,
                    slot_index,
                    str(raw_result)[:120],
                )
                batch_results.append(
                    _ParallelMainSlotResult(
                        slot_index=slot_index,
                        text=" | ".join(slot_texts),
                        remaining_texts=list(slot_texts),
                        attempts=main_attempts_by_slot.get(slot_index, 0),
                        error=f"parallel_main_exception:{str(raw_result)[:80]}",
                    )
                )
                continue
            batch_results.append(raw_result)

        retry_slots: List[Tuple[int, List[str]]] = []
        for slot_result in batch_results:
            if slot_result.published:
                stored = main_published_by_slot.setdefault(slot_result.slot_index, [])
                stored_keys = {
                    (
                        str(item.comment_id).strip()
                        if item.comment_id
                        else f"{_dedup_key(item.text)}:{item.account_id or ''}"
                    )
                    for item in stored
                }
                for published in slot_result.published:
                    published_key = (
                        str(published.comment_id).strip()
                        if published.comment_id
                        else f"{_dedup_key(published.text)}:{published.account_id or ''}"
                    )
                    if published_key not in stored_keys:
                        stored.append(published)
                        stored_keys.add(published_key)

            if slot_result.published:
                slot_result.published = list(main_published_by_slot.get(slot_result.slot_index, []))
                main_results_by_slot[slot_result.slot_index] = slot_result
                continue
            main_failures_by_slot[slot_result.slot_index] = slot_result
            retryable_error = slot_result.error not in {"no_free_main_accounts", "stopped_by_user"}
            if retryable_error and main_attempts_by_slot.get(slot_result.slot_index, 0) < max_main_attempts:
                retry_texts = slot_result.remaining_texts or list(main_texts)
                retry_slots.append((slot_result.slot_index, retry_texts))

        total_main_publications = sum(len(items) for items in main_published_by_slot.values())
        if total_main_publications <= 0 and not any(slot.published for slot in batch_results):
            empty_main_batches_without_any_publication += 1
            logger.warning(
                "Task %s: parallel main batch %s/%s produced zero comments; "
                "likely video/comment surface problem if it repeats",
                task_id,
                empty_main_batches_without_any_publication,
                max_empty_main_batches_before_fail,
            )
        else:
            empty_main_batches_without_any_publication = 0

        if (
            total_main_publications <= 0
            and empty_main_batches_without_any_publication >= max_empty_main_batches_before_fail
        ):
            result.error = "parallel_main_no_publications_after_two_batches"
            logger.warning(
                "Task %s: parallel main phase stopped after %s empty batch(es); "
                "no main comments were published, skipping further main retries",
                task_id,
                empty_main_batches_without_any_publication,
            )
            pending_main_slots = []
            break

        if retry_slots:
            pending_main_slots = retry_slots + pending_main_slots
            await asyncio.sleep(random.uniform(*_RETRY_ACCOUNT_DELAY_RANGE))

    main_results = [
        main_results_by_slot.get(slot_index)
        or main_failures_by_slot.get(slot_index)
        or _ParallelMainSlotResult(
            slot_index=slot_index,
            text=" | ".join(main_texts),
            published=list(main_published_by_slot.get(slot_index, [])),
            error="stopped_by_user" if _is_stop_requested() else "main_not_attempted",
        )
        for slot_index in range(1, parallel_main_account_slots + 1)
    ]
    if _is_stop_requested():
        return _mark_stopped()

    working_video_url = video_url
    for slot_result in main_results:
        if slot_result.resolved_video_url and extract_tiktok_media_id(slot_result.resolved_video_url):
            working_video_url = slot_result.resolved_video_url
            break
    for slot_result in sorted(main_results, key=lambda item: item.slot_index):
        if slot_result.published:
            result.published_mains.extend(slot_result.published)
            for published in slot_result.published:
                if result.main_account_id is None:
                    result.main_account_id = published.account_id
                if published.tiktok_username and not result.main_tiktok_username:
                    result.main_tiktok_username = published.tiktok_username

    if result.error:
        result.elapsed_seconds = time.monotonic() - started
        return result

    if sum(1 for slot in main_results if slot.published) < parallel_main_account_slots:
        missing_slots = [
            str(slot.slot_index)
            for slot in main_results
            if not slot.published
        ]
        result.error = f"parallel_main_incomplete:slots={','.join(missing_slots)}"
        logger.warning(
            "Task %s: parallel main phase incomplete; successful_slots=%s expected_slots=%s published=%s missing_slots=%s",
            task_id,
            sum(1 for slot in main_results if slot.published),
            parallel_main_account_slots,
            len(result.published_mains),
            missing_slots,
        )
        result.elapsed_seconds = time.monotonic() - started
        return result

    for main in result.published_mains:
        if main.account_id is not None:
            used_reply_ids.add(main.account_id)
    used_reply_ids.update(tried_main_ids)

    async def _select_reply_account(attempt_db: Session) -> Optional[Account]:
        async with reply_selection_lock:
            candidate = get_free_account_for_reply(
                attempt_db,
                exclude_ids=list(used_reply_ids),
                pool_owner_user_id=pool_owner_user_id,
            )
            if candidate:
                used_reply_ids.add(candidate.id)
            return candidate

    def _merge_parallel_active_mains(candidates: List[PublishedMain]) -> List[PublishedMain]:
        return _merge_distinct_main_candidates(candidates)

    reply_submitted_parent_keys_by_variant: Dict[int, set[str]] = {}
    reply_reported_outcomes_by_variant: Dict[int, List[ReplyOutcome]] = {}
    reply_found_mains_by_variant: Dict[int, Dict[str, PublishedMain]] = {}

    async def _run_reply_variant_attempt(
        variant_index: int,
        reply_text: str,
        main_candidates: List[PublishedMain],
        attempt: int,
    ) -> _ParallelReplyVariantResult:
        variant_result = _ParallelReplyVariantResult(
            variant_index=variant_index,
            text=reply_text,
        )
        submitted_parent_keys = reply_submitted_parent_keys_by_variant.setdefault(variant_index, set())
        reported_outcomes = reply_reported_outcomes_by_variant.setdefault(variant_index, [])
        found_mains_by_key = reply_found_mains_by_variant.setdefault(variant_index, {})

        if _is_stop_requested():
            variant_result.error = "stopped_by_user"
            return variant_result
        with get_sync_db_context() as attempt_db:
            reply_account = await _select_reply_account(attempt_db)
            if not reply_account:
                variant_result.error = "no_free_reply_accounts"
                return variant_result
            variant_result.account_id = reply_account.id
            corr = f"task_{task_id}_parallel_reply_v{variant_index}_a{attempt}_acc{reply_account.id}"
            logger.info(
                "Task %s: parallel reply variant %s attempt %s/%s on account=%s over %d main(s)",
                task_id,
                variant_index,
                attempt,
                max_reply_attempts,
                reply_account.id,
                len(main_candidates),
            )
            try:
                attempt_result = await _reply_with_variant_on_all_mains(
                    attempt_db,
                    reply_account,
                    working_video_url,
                    main_candidates,
                    reply_text,
                    variant_index=variant_index,
                    correlation_id=corr,
                    fallback_main_texts=None,
                    already_replied_parent_keys=submitted_parent_keys,
                    fallback_allowed=False,
                )
            except Exception as exc:
                logger.warning(
                    "Task %s: parallel reply variant %s account=%s raised: %s",
                    task_id,
                    variant_index,
                    reply_account.id,
                    str(exc)[:120],
                )
                attempt_result = ReplyAttemptResult(
                    replies=[
                        _reply_failure_outcome(
                            reply_text=reply_text,
                            variant_index=variant_index,
                            account_id=reply_account.id,
                            error=f"reply_attempt_exception:{str(exc)[:80]}",
                        )
                    ]
                )

        outcomes = attempt_result.replies
        submitted_outcomes = [outcome for outcome in outcomes if _reply_outcome_submitted(outcome)]
        if submitted_outcomes:
            reported_outcomes.extend(submitted_outcomes)
            submitted_parent_keys.update(
                _reply_outcomes_submitted_parent_keys_for_expected_mains(
                    submitted_outcomes,
                    main_candidates,
                )
            )

        found_mains = attempt_result.found_mains
        if not found_mains and outcomes:
            found_mains = _infer_found_mains_from_reply_outcomes(outcomes, main_candidates)

        if found_mains:
            variant_result.found_any_main = True
            for found_main in _normalize_main_candidates(found_mains):
                found_key = _main_parent_key(found_main)
                if found_key:
                    found_mains_by_key[found_key] = found_main
            active_for_variant = _normalize_main_candidates(list(found_mains_by_key.values()))
            variant_result.found_mains = active_for_variant
            pending_expected_mains = _pending_expected_mains_for_reply_variant(
                main_candidates,
                submitted_parent_keys,
            )
            if main_candidates and not pending_expected_mains:
                variant_result.completed = True
                variant_result.replies = reported_outcomes or submitted_outcomes or outcomes
                return variant_result
            variant_result.error = "reply_publication_incomplete"
        elif attempt_result.confident_empty_search:
            variant_result.no_main_found = True
            variant_result.error = "no_main_found_by_reply_account"
            variant_result.replies = outcomes or [
                _reply_failure_outcome(
                    reply_text=reply_text,
                    variant_index=variant_index,
                    account_id=reply_account.id,
                    error="no_main_found_by_reply_account",
                )
            ]
            return variant_result
        else:
            variant_result.error = (
                (outcomes[-1].error if outcomes else None)
                or "reply_search_inconclusive"
            )

        variant_result.replies = reported_outcomes or outcomes or [
            _reply_failure_outcome(
                reply_text=reply_text,
                variant_index=variant_index,
                account_id=reply_account.id,
                error=variant_result.error or "reply_publication_incomplete",
            )
        ]
        return variant_result

    active_mains = _normalize_main_candidates(result.published_mains)
    pending_reply_variants: List[Tuple[int, str]] = [
        (variant_index, reply_text)
        for variant_index, reply_text in enumerate(reply_variants, start=1)
    ]
    reply_rounds: Dict[int, int] = {}
    any_reply_account_found_main = False

    while pending_reply_variants and not result.error:
        if _is_stop_requested():
            return _mark_stopped()
        batch = pending_reply_variants[:3]
        pending_reply_variants = pending_reply_variants[3:]
        known_main_before_batch = any_reply_account_found_main
        for variant_index, _reply_text in batch:
            reply_rounds[variant_index] = reply_rounds.get(variant_index, 0) + 1

        if not _ensure_parallel_browser_capacity():
            result.error = "parallel_browser_capacity_unavailable"
            break
        try:
            raw_batch_results = await asyncio.gather(
                *[
                    _run_reply_variant_attempt(
                        variant_index,
                        reply_text,
                        active_mains,
                        reply_rounds[variant_index],
                    )
                    for variant_index, reply_text in batch
                ],
                return_exceptions=True,
            )
        finally:
            await _close_parallel_phase_browsers("reply_batch")
        batch_results: List[_ParallelReplyVariantResult] = []
        for (variant_index, reply_text), raw_result in zip(batch, raw_batch_results):
            if isinstance(raw_result, BaseException):
                if isinstance(raw_result, (asyncio.CancelledError, KeyboardInterrupt, SystemExit)):
                    raise raw_result
                logger.warning(
                    "Task %s: parallel reply variant %s batch task raised outside attempt wrapper: %s",
                    task_id,
                    variant_index,
                    str(raw_result)[:120],
                )
                batch_results.append(
                    _ParallelReplyVariantResult(
                        variant_index=variant_index,
                        text=reply_text,
                        error=f"parallel_reply_exception:{str(raw_result)[:80]}",
                        replies=[
                            _reply_failure_outcome(
                                reply_text=reply_text,
                                variant_index=variant_index,
                                account_id=None,
                                error=f"parallel_reply_exception:{str(raw_result)[:80]}",
                            )
                        ],
                    )
                )
                continue
            batch_results.append(raw_result)
        if _is_stop_requested():
            return _mark_stopped()

        batch_found_any = any(item.found_any_main for item in batch_results)
        if batch_found_any:
            any_reply_account_found_main = True
            result.main_found_by_observer = True
            batch_found_mains: List[PublishedMain] = []
            for item in batch_results:
                batch_found_mains.extend(item.found_mains)
            if batch_found_mains:
                active_mains = _merge_parallel_active_mains([*active_mains, *batch_found_mains])

        batch_all_no_main = bool(batch_results) and all(
            item.no_main_found and not item.found_any_main
            for item in batch_results
        )
        if batch_all_no_main and not known_main_before_batch and not batch_found_any:
            result.error = "reply_mains_not_found"
            for item in batch_results:
                result.replies.extend(item.replies)
            logger.warning(
                "Task %s: parallel reply batch found no main comments with %d account(s); no fallback mains in parallel mode",
                task_id,
                len(batch_results),
            )
            break

        retry_variants: List[Tuple[int, str]] = []
        terminal_reply_error: Optional[_ParallelReplyVariantResult] = None
        for item in sorted(batch_results, key=lambda value: value.variant_index):
            if item.completed:
                result.replies.extend(item.replies)
                continue

            can_retry = (
                item.error not in {"no_free_reply_accounts", "stopped_by_user"}
                and reply_rounds.get(item.variant_index, 0) < max_reply_attempts
            )
            if can_retry:
                retry_variants.append((item.variant_index, item.text))
                logger.info(
                    "Task %s: parallel reply variant %s will retry with another account (reason=%s)",
                    task_id,
                    item.variant_index,
                    item.error,
                )
                continue

            result.replies.extend(item.replies or [
                _reply_failure_outcome(
                    reply_text=item.text,
                    variant_index=item.variant_index,
                    account_id=item.account_id,
                    error=item.error or "reply_publication_incomplete",
                )
            ])
            if terminal_reply_error is None:
                terminal_reply_error = item

        if terminal_reply_error is not None:
            result.error = terminal_reply_error.error or "reply_publication_incomplete"
            logger.warning(
                "Task %s: parallel reply variant %s failed after %s round(s): %s",
                task_id,
                terminal_reply_error.variant_index,
                reply_rounds.get(terminal_reply_error.variant_index, 0),
                result.error,
            )

        if retry_variants and not result.error:
            pending_reply_variants = retry_variants + pending_reply_variants
            await asyncio.sleep(random.uniform(*_RETRY_ACCOUNT_DELAY_RANGE))

    if boost_enabled and not reply_variants:
        result.boost_enabled = False
        result.boost_error = None
        logger.info(
            "Task %s: likes skipped because full order has no replies.",
            task_id,
        )
    elif boost_enabled and not any(outcome.success for outcome in result.replies):
        result.boost_enabled = False
        result.boost_error = None
        logger.info(
            "Task %s: likes skipped because full order published no replies.",
            task_id,
        )
    else:
        await _maybe_order_likes(
            result,
            video_url=working_video_url,
            task_id=task_id,
            boost_enabled=boost_enabled,
            boost_quantity=boost_quantity,
        )

    if not result.published_mains and not result.error:
        result.error = "no_main_published"
    result.elapsed_seconds = time.monotonic() - started
    return result


async def execute_user_schema_full(
    db: Session,
    task: Task,
    video_url: str,
    text_variants: List[str],
    task_id: int,
    *,
    boost_enabled: bool,
    boost_quantity: int,
    pool_owner_user_id: Optional[int] = None,
    stop_requested: Optional[Callable[[], bool]] = None,
) -> UserSchemaResult:
    """Реализация пользовательской схемы для одного видео.

    Args:
        text_variants: до 3 уникальных вариантов главного коммента.
    """
    started = time.monotonic()
    result = UserSchemaResult(boost_enabled=boost_enabled, boost_quantity=boost_quantity)

    def _is_stop_requested() -> bool:
        if stop_requested is None:
            return False
        try:
            return bool(stop_requested())
        except Exception as exc:
            logger.debug("Task %s: stop callback failed: %s", task_id, exc)
            return False

    def _mark_stopped() -> UserSchemaResult:
        result.error = "stopped_by_user"
        result.elapsed_seconds = time.monotonic() - started
        return result

    if _is_stop_requested():
        logger.info("Task %s: user stop detected before user-schema work", task_id)
        return _mark_stopped()

    text_variants = [v for v in (text_variants or []) if (v or "").strip()]
    if not text_variants:
        result.error = "no_text_variants"
        result.elapsed_seconds = time.monotonic() - started
        return result

    # Replies из задачи.
    reply_variants: List[str] = _reply_variants_from_task(task)
    result.expected_main_publications = len(_required_main_texts(text_variants))
    result.expected_reply_variants = len(reply_variants)

    # ----- MAIN -----
    max_main_attempts = max(1, int(_settings.MAX_RETRY_ACCOUNTS))
    tried_main: List[int] = []
    reserved_main: set[int] = set()
    comment_ui_missing_attempts = 0
    publish_blocked_attempts = 0

    def _release_reserved() -> None:
        for acc_id in list(reserved_main):
            try:
                release_main_account_reservation(acc_id)
            finally:
                reserved_main.discard(acc_id)

    def _release_main_candidate(candidate: Account) -> None:
        try:
            release_main_account_reservation(candidate.id)
        finally:
            reserved_main.discard(candidate.id)

    def _record_published_mains(
        candidate: Account,
        published: List[PublishedMain],
        resolved_video_url_holder: Dict[str, str],
        current_working_video_url: str,
    ) -> str:
        resolved_video_url = resolved_video_url_holder.get("video_url")
        if resolved_video_url and resolved_video_url != current_working_video_url:
            logger.info(
                "Task %s: using browser-resolved video URL for observer/reply phases: %s -> %s",
                task_id,
                current_working_video_url[:100],
                resolved_video_url[:140],
            )
            current_working_video_url = resolved_video_url

        if result.main_account_id is None:
            result.main_account_id = candidate.id
        result.published_mains.extend(published)
        for p in published:
            if p.tiktok_username and not result.main_tiktok_username:
                result.main_tiktok_username = p.tiktok_username
                break
        _release_main_candidate(candidate)
        return current_working_video_url

    async def _handle_main_attempt_failure(
        candidate: Account,
        dead_reason: Optional[str],
        attempt_number: int,
    ) -> bool:
        """Return True when main attempts should stop completely."""
        nonlocal comment_ui_missing_attempts, publish_blocked_attempts

        _release_main_candidate(candidate)
        if dead_reason and "comment_ui_not_found" in dead_reason:
            comment_ui_missing_attempts += 1
            logger.warning(
                "Task %s: comment UI not found on ready video page (%s/%s). reason=%s",
                task_id,
                comment_ui_missing_attempts,
                5,
                dead_reason,
            )
            if comment_ui_missing_attempts >= 5:
                result.error = "comment_ui_not_found"
                logger.warning(
                    "Task %s: stopping main attempts because the video page exposes no comment UI on ready accounts",
                    task_id,
                )
                return True
            await asyncio.sleep(random.uniform(*_RETRY_ACCOUNT_DELAY_RANGE))
            return False

        main_block_markers = (
            "blocked_ui",
            "on_video_empty_dom_after_settle",
            "comment_input_not_available_after_cookie_probe",
            "input_not_found",
            "post_button_stayed_disabled",
            "submitted_unverified_disabled_post",
        )
        is_main_blocked = bool(
            dead_reason
            and (
                any(marker in str(dead_reason) for marker in main_block_markers)
                or str(dead_reason).startswith(("captcha_challenge:", "captcha_challenge_timeout:"))
            )
        )
        if is_main_blocked:
            publish_blocked_attempts += 1
            logger.warning(
                "Task %s: publish blocked on account=%s (%s/%s). reason=%s",
                task_id,
                candidate.id,
                publish_blocked_attempts,
                3,
                dead_reason,
            )
            if publish_blocked_attempts >= 3:
                result.error = f"publish_blocked:{dead_reason}"[:200]
                logger.warning(
                    "Task %s: stopping main-account retries after repeated publish blocks.",
                    task_id,
                )
                return True
        else:
            publish_blocked_attempts = 0

        if not dead_reason:
            _register_runtime_failure(db, candidate, "no_publish_after_login_ok")
        elif not dead_reason.startswith((
            "dead_session:",
            "login_ambiguous:",
            "page_create_failed:",
            "captcha_challenge:",
            "captcha_challenge_timeout:",
            "blocked_ui:",
            "on_video_empty_dom_after_settle",
        )):
            _register_runtime_failure(db, candidate, f"publish_failed:{dead_reason}"[:200])

        if attempt_number < max_main_attempts:
            await asyncio.sleep(random.uniform(*_RETRY_ACCOUNT_DELAY_RANGE))
        return False

    try:
        working_video_url = video_url
        main_texts_to_publish = _dedup_variants(text_variants)
        publish_main_variants_separately = not reply_variants

        if publish_main_variants_separately:
            attempt = 0
            stop_main_attempts = False
            for variant_index, main_text in enumerate(main_texts_to_publish, start=1):
                variant_published = False
                while attempt < max_main_attempts:
                    if _is_stop_requested():
                        logger.info(
                            "Task %s: user stop detected before main-only variant %s/%s",
                            task_id,
                            variant_index,
                            len(main_texts_to_publish),
                        )
                        stop_main_attempts = True
                        _mark_stopped()
                        break
                    candidate = get_free_account_for_main_comment(
                        db,
                        exclude_ids=tried_main,
                        pool_owner_user_id=pool_owner_user_id,
                    )
                    if not candidate:
                        logger.warning(
                            "Task %s: no free main accounts before main-only variant %s/%s",
                            task_id,
                            variant_index,
                            len(main_texts_to_publish),
                        )
                        stop_main_attempts = True
                        break
                    attempt += 1
                    tried_main.append(candidate.id)
                    reserved_main.add(candidate.id)

                    corr = f"task_{task_id}_main_v{variant_index}_a{attempt}_acc{candidate.id}"
                    logger.info(
                        "Task %s: main-only variant %s/%s attempt %s/%s on account=%s",
                        task_id,
                        variant_index,
                        len(main_texts_to_publish),
                        attempt,
                        max_main_attempts,
                        candidate.id,
                    )
                    resolved_video_url_holder: Dict[str, str] = {}
                    published, dead_reason = await _try_main_account(
                        db,
                        candidate,
                        working_video_url,
                        [main_text],
                        correlation_id=corr,
                        resolved_video_url_holder=resolved_video_url_holder,
                    )
                    if published:
                        working_video_url = _record_published_mains(
                            candidate,
                            published,
                            resolved_video_url_holder,
                            working_video_url,
                        )
                        variant_published = True
                        break

                    should_stop = await _handle_main_attempt_failure(
                        candidate,
                        dead_reason,
                        attempt,
                    )
                    if should_stop:
                        stop_main_attempts = True
                        break

                if not variant_published:
                    if not result.error:
                        result.error = (
                            f"main_variant_partial:{variant_index}"
                            if result.published_mains
                            else f"main_variant_not_published:{variant_index}"
                        )
                    logger.warning(
                        "Task %s: main-only variant %s/%s was not published; published_so_far=%s",
                        task_id,
                        variant_index,
                        len(main_texts_to_publish),
                        len(result.published_mains),
                    )
                    if stop_main_attempts or attempt >= max_main_attempts:
                        break

        legacy_main_attempts = (
            range(1, max_main_attempts + 1)
            if not publish_main_variants_separately
            else ()
        )
        for attempt in legacy_main_attempts:
            if _is_stop_requested():
                logger.info("Task %s: user stop detected before main attempt %s", task_id, attempt)
                _mark_stopped()
                break
            candidate = get_free_account_for_main_comment(
                db,
                exclude_ids=tried_main,
                pool_owner_user_id=pool_owner_user_id,
            )
            if not candidate:
                logger.warning("Task %s: no free main accounts after attempt %s", task_id, attempt - 1)
                break
            tried_main.append(candidate.id)
            reserved_main.add(candidate.id)

            corr = f"task_{task_id}_main_a{attempt}_acc{candidate.id}"
            logger.info("Task %s: main attempt %s/%s on account=%s", task_id, attempt, max_main_attempts, candidate.id)
            resolved_video_url_holder: Dict[str, str] = {}
            published, dead_reason = await _try_main_account(
                db,
                candidate,
                video_url,
                text_variants,
                correlation_id=corr,
                resolved_video_url_holder=resolved_video_url_holder,
            )
            if published:
                resolved_video_url = resolved_video_url_holder.get("video_url")
                if resolved_video_url and resolved_video_url != working_video_url:
                    logger.info(
                        "Task %s: using browser-resolved video URL for observer/reply phases: %s -> %s",
                        task_id,
                        working_video_url[:100],
                        resolved_video_url[:140],
                    )
                    working_video_url = resolved_video_url
                result.main_account_id = candidate.id
                result.published_mains.extend(published)
                # Берём username из bootstrap если он был извлечён.
                for p in published:
                    if p.tiktok_username:
                        result.main_tiktok_username = p.tiktok_username
                        break
                # Освобождаем reservation НЕМЕДЛЕННО, как только main опубликован.
                # Иначе аккаунт остаётся заблокированным до конца reply-фазы
                # (20 reply-аккаунтов × N комментариев = long reservation time for
                # этого main-аккаунта в чужих параллельных задачах).
                release_main_account_reservation(candidate.id)
                reserved_main.discard(candidate.id)
                break
            # Не получилось — освобождаем и идём к следующему.
            # NB: про dead_session / login_ambiguous уже отчитался cookie_health
            # (он же решил, штрафовать ли validation_fail_count). Здесь только
            # «всё чисто, но публикация не прошла» получает soft cooldown.
            release_main_account_reservation(candidate.id)
            reserved_main.discard(candidate.id)
            if dead_reason and "comment_ui_not_found" in dead_reason:
                comment_ui_missing_attempts += 1
                logger.warning(
                    "Task %s: comment UI not found on ready video page (%s/%s). reason=%s",
                    task_id,
                    comment_ui_missing_attempts,
                    5,
                    dead_reason,
                )
                if comment_ui_missing_attempts >= 5:
                    result.error = "comment_ui_not_found"
                    logger.warning(
                        "Task %s: stopping main attempts because the video page exposes no comment UI on ready accounts",
                        task_id,
                    )
                    break
                await asyncio.sleep(random.uniform(*_RETRY_ACCOUNT_DELAY_RANGE))
                continue
            main_block_markers = (
                "blocked_ui",
                "on_video_empty_dom_after_settle",
                "comment_input_not_available_after_cookie_probe",
                "input_not_found",
                "post_button_stayed_disabled",
                "submitted_unverified_disabled_post",
            )
            is_main_blocked = bool(
                dead_reason
                and (
                    any(marker in str(dead_reason) for marker in main_block_markers)
                    or str(dead_reason).startswith(("captcha_challenge:", "captcha_challenge_timeout:"))
                )
            )
            if is_main_blocked:
                publish_blocked_attempts += 1
                logger.warning(
                    "Task %s: publish blocked on account=%s (%s/%s). reason=%s",
                    task_id,
                    candidate.id,
                    publish_blocked_attempts,
                    3,
                    dead_reason,
                )
                if publish_blocked_attempts >= 3:
                    result.error = f"publish_blocked:{dead_reason}"[:200]
                    logger.warning(
                        "Task %s: stopping main-account retries after repeated publish blocks.",
                        task_id,
                    )
                    break
            else:
                publish_blocked_attempts = 0
            if not dead_reason:
                _register_runtime_failure(db, candidate, "no_publish_after_login_ok")
            elif not dead_reason.startswith((
                "dead_session:",
                "login_ambiguous:",
                "page_create_failed:",
                "captcha_challenge:",
                "captcha_challenge_timeout:",
                "blocked_ui:",
                "on_video_empty_dom_after_settle",
            )):
                _register_runtime_failure(db, candidate, f"publish_failed:{dead_reason}"[:200])
            # Anti-flood: пауза перед следующим retry-аккаунтом.
            if attempt < max_main_attempts:
                await asyncio.sleep(random.uniform(*_RETRY_ACCOUNT_DELAY_RANGE))

        # ----- REPLIES -----
        if result.error == "stopped_by_user" or _is_stop_requested():
            logger.info("Task %s: user stop detected before reply phase", task_id)
            return _mark_stopped()

        used_reply_accounts: List[int] = []
        if result.main_account_id is not None:
            used_reply_accounts.append(result.main_account_id)
        for main in result.published_mains:
            if main.account_id is not None and main.account_id not in used_reply_accounts:
                used_reply_accounts.append(main.account_id)
        active_mains: List[PublishedMain] = []
        if result.published_mains and reply_variants:
            active_mains = await _prepare_public_mains_before_replies(
                db,
                working_video_url,
                text_variants,
                result,
                task_id=task_id,
                used_reply_accounts=used_reply_accounts,
                pool_owner_user_id=pool_owner_user_id,
            )
        if reply_variants and result.error != "comment_ui_not_found":
            max_reply_attempts = max(2, max(1, int(_settings.MAX_RETRY_ACCOUNTS)) + 1)
            fallback_main_attempts = 0
            task_fallback_used = False
            task_any_account_found_main = bool(result.main_found_by_observer)
            task_confident_empty_misses = 0

            for variant_idx, reply_text in enumerate(reply_variants, start=1):
                if not active_mains:
                    logger.warning(
                        "Task %s: no active main candidates before reply variant %s",
                        task_id,
                        variant_idx,
                    )
                    if not result.error and result.main_found_by_observer:
                        result.error = "reply_publication_incomplete"
                    break

                last_outcomes: List[ReplyOutcome] = []
                last_reply_account_id: Optional[int] = None
                variant_completed = False
                variant_submitted_parent_keys: set[str] = set()
                variant_reported_outcomes: List[ReplyOutcome] = []
                variant_found_mains_by_key: Dict[str, PublishedMain] = {}

                for reply_attempt in range(1, max_reply_attempts + 1):
                    if _is_stop_requested():
                        logger.info(
                            "Task %s: user stop detected before reply variant %s attempt %s",
                            task_id,
                            variant_idx,
                            reply_attempt,
                        )
                        return _mark_stopped()
                    reply_account = get_free_account_for_reply(
                        db,
                        exclude_ids=used_reply_accounts,
                        pool_owner_user_id=pool_owner_user_id,
                    )
                    if not reply_account:
                        logger.warning(
                            "Task %s: out of reply accounts on variant %s (attempt %s)",
                            task_id,
                            variant_idx,
                            reply_attempt,
                        )
                        break
                    used_reply_accounts.append(reply_account.id)
                    last_reply_account_id = reply_account.id

                    corr = f"task_{task_id}_reply_v{variant_idx}_a{reply_attempt}_acc{reply_account.id}"
                    logger.info(
                        "Task %s: reply variant %s attempt %s/%s on account=%s over %d active main(s)",
                        task_id,
                        variant_idx,
                        reply_attempt,
                        max_reply_attempts,
                        reply_account.id,
                        len(active_mains),
                    )
                    fallback_allowed = (
                        not task_any_account_found_main
                        and not task_fallback_used
                        and task_confident_empty_misses >= 1
                        and fallback_main_attempts < _MAX_REPLY_FALLBACK_MAIN_ATTEMPTS
                    )
                    attempt_result = await _reply_with_variant_on_all_mains(
                        db,
                        reply_account,
                        working_video_url,
                        active_mains,
                        reply_text,
                        variant_index=variant_idx,
                        correlation_id=corr,
                        fallback_main_texts=(
                            _required_main_texts(text_variants)
                            if (
                                not task_fallback_used
                                and fallback_main_attempts < _MAX_REPLY_FALLBACK_MAIN_ATTEMPTS
                            )
                            else None
                        ),
                        already_replied_parent_keys=variant_submitted_parent_keys,
                        fallback_allowed=fallback_allowed,
                    )
                    if _is_stop_requested():
                        logger.info(
                            "Task %s: user stop detected after reply variant %s attempt %s",
                            task_id,
                            variant_idx,
                            reply_attempt,
                        )
                        return _mark_stopped()
                    outcomes = attempt_result.replies
                    last_outcomes = outcomes
                    submitted_outcomes = [o for o in outcomes if _reply_outcome_submitted(o)]
                    if submitted_outcomes:
                        variant_reported_outcomes.extend(submitted_outcomes)
                        variant_submitted_parent_keys.update(
                            _reply_outcomes_submitted_parent_keys(submitted_outcomes)
                        )
                        task_any_account_found_main = True
                        task_confident_empty_misses = 0

                    if attempt_result.fallback_attempted:
                        fallback_main_attempts += 1
                        task_fallback_used = True
                        if not attempt_result.fallback_mains:
                            logger.warning(
                                "Task %s: reply account=%s found no mains and fallback main produced nothing (%s/%s)",
                                task_id,
                                reply_account.id,
                                fallback_main_attempts,
                                _MAX_REPLY_FALLBACK_MAIN_ATTEMPTS,
                            )

                    if attempt_result.fallback_mains:
                        result.fallback_main_via_reply = True
                        result.published_mains.extend(attempt_result.fallback_mains)
                        fallback_username = next(
                            (p.tiktok_username for p in attempt_result.fallback_mains if p.tiktok_username),
                            None,
                        )
                        if fallback_username:
                            result.main_tiktok_username = fallback_username
                        fallback_owner_id = attempt_result.fallback_mains[0].account_id
                        active_mains = _build_active_mains(
                            text_variants,
                            attempt_result.fallback_mains,
                            main_account_id=fallback_owner_id,
                            main_tiktok_username=result.main_tiktok_username,
                        )
                        logger.info(
                            "Task %s: fallback main refreshed active set to %d candidate(s); "
                            "retrying reply variant %s with next account",
                            task_id,
                            len(active_mains),
                            variant_idx,
                        )
                        if reply_attempt < max_reply_attempts:
                            await asyncio.sleep(random.uniform(*_RETRY_ACCOUNT_DELAY_RANGE))
                            continue
                        break

                    if not attempt_result.searched:
                        task_confident_empty_misses = 0
                        logger.info(
                            "Task %s: reply variant %s account=%s did not get a searchable "
                            "comment list; retrying same active mains with next account",
                            task_id,
                            variant_idx,
                            reply_account.id,
                        )

                    found_mains = attempt_result.found_mains
                    if not found_mains and outcomes:
                        matched_keys = {
                            _dedup_key(o.matched_main_text)
                            for o in outcomes
                            if o.matched_main_text
                        }
                        found_mains = [
                            m
                            for m in active_mains
                            if _dedup_key(m.text) in matched_keys
                        ]

                    if found_mains:
                        for found_main in _normalize_main_candidates(found_mains):
                            found_key = _main_parent_key(found_main)
                            if found_key:
                                variant_found_mains_by_key[found_key] = found_main
                        active_mains = _normalize_main_candidates(list(variant_found_mains_by_key.values()))
                        if active_mains:
                            result.main_found_by_observer = True
                            task_any_account_found_main = True
                            task_confident_empty_misses = 0
                        pending_found_mains = [
                            m
                            for m in active_mains
                            if not _main_matches_any_parent_key(m, variant_submitted_parent_keys)
                        ]
                        if active_mains and not pending_found_mains:
                            result.replies.extend(variant_reported_outcomes or outcomes)
                            variant_completed = True
                            logger.info(
                                "Task %s: reply variant %s finished by account=%s; "
                                "next variants will search %d found main(s)",
                                task_id,
                                variant_idx,
                                reply_account.id,
                                len(active_mains),
                            )
                            break
                        logger.warning(
                            "Task %s: reply variant %s found %d main(s) but %d remain unsubmitted; "
                            "retrying same variant with next account",
                            task_id,
                            variant_idx,
                            len(active_mains),
                            len(pending_found_mains),
                        )
                    elif attempt_result.confident_empty_search:
                        task_confident_empty_misses += 1
                    else:
                        task_confident_empty_misses = 0

                    if reply_attempt < max_reply_attempts:
                        await asyncio.sleep(random.uniform(*_RETRY_ACCOUNT_DELAY_RANGE))

                if not variant_completed:
                    if variant_reported_outcomes:
                        result.replies.extend(variant_reported_outcomes)
                    elif last_outcomes:
                        result.replies.extend(last_outcomes)
                    elif last_reply_account_id is not None:
                        result.replies.append(
                            ReplyOutcome(
                                text=reply_text,
                                variant_index=variant_idx,
                                matched_main_text=None,
                                parent_comment_id=None,
                                success=False,
                                method=None,
                                error="no_main_found_by_reply_account",
                                account_id=last_reply_account_id,
                            )
                        )
                    if not result.error and result.main_found_by_observer:
                        result.error = "reply_publication_incomplete"
                    break

        # ----- LIKES -----
        if boost_enabled and not reply_variants:
            result.boost_enabled = False
            result.boost_error = None
            logger.info(
                "Task %s: likes skipped because full order has no replies.",
                task_id,
            )
        elif boost_enabled and not any(outcome.success for outcome in result.replies):
            result.boost_enabled = False
            result.boost_error = None
            logger.info(
                "Task %s: likes skipped because full order published no replies.",
                task_id,
            )
        else:
            await _maybe_order_likes(
                result,
                video_url=working_video_url,
                task_id=task_id,
                boost_enabled=boost_enabled,
                boost_quantity=boost_quantity,
            )

        if not result.published_mains and not result.error:
            result.error = "no_main_published"
        result.elapsed_seconds = time.monotonic() - started
        return result
    finally:
        _release_reserved()
