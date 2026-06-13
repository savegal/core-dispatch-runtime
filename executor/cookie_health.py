"""Источник истины «жив / мёртв / неясно» для куки аккаунта TikTok.

Идея централизованного модуля:
  - проверка кук должна быть ОДНОРАЗОВОЙ, БЫСТРОЙ (1-2с) и базироваться
    только на визуальном состоянии TikTok-страницы, а не на «технически
    валидных» cookies в браузере;
  - решение «вывести аккаунт из работы» принимается только этим модулем,
    чтобы старый код и user-schema не давили на статус по-разному;
  - двусторонний «дрожжевой» сценарий (бывает «dead» по proxy-таймауту,
    потом «ready» при следующем заходе) разрешается через persisted-
    счётчик Account.validation_fail_count: только два подряд DEAD
    помечают аккаунт reauth_needed.

Не путать с:
  - executor/session_bootstrap.quick_video_login_check — это «дёшево
    спросить страницу: ты залогинен?». cookie_health использует её
    как сенсор.
  - services/account_service.mark_* — низкоуровневая запись в БД. Мы
    дёргаем mark_needs_reauth/mark_soft_error через высокоуровневый
    register_* API.
"""
from __future__ import annotations

import asyncio
import logging
import os
import time
from dataclasses import dataclass
from enum import Enum
from typing import Optional

from patchright.async_api import Page
from sqlalchemy.orm import Session

from db.models import Account
from config.settings import get_settings
from executor.captcha_detector import detect_captcha
from executor.session_bootstrap import (
    QuickLoginCheck,
    get_browser_login_state,
    is_tiktok_media_page_url,
    quick_video_login_check,
)
from services.account_service import mark_needs_reauth, mark_soft_error

logger = logging.getLogger("executor.cookie_health")
_settings = get_settings()
_MANUAL_CAPTCHA_WAIT_SECONDS = float(os.getenv("MANUAL_CAPTCHA_WAIT_SECONDS", "15"))
_MANUAL_CAPTCHA_POLL_SECONDS = 2.0
_TRUE_ENV_VALUES = {"1", "true", "yes", "on"}


class CookieVerdict(str, Enum):
    """Финальная классификация после одной визуальной проверки.

    Один verdict = одно решение для вызывающего кода. НЕ путать с состоянием
    БД (это решение об ИСПОЛЬЗОВАНИИ аккаунта в текущем заказе).
    """

    READY = "ready"            # можно публиковать.
    DEAD = "dead"              # явно logged_out / редирект / страница не видео.
    CAPTCHA = "captcha"
    BLOCKED = "blocked"
    AMBIGUOUS = "ambiguous"    # ни logged_in, ни logged_out маркеров; пробуем,
                               # но без штрафа аккаунту.


@dataclass
class CookieHealth:
    verdict: CookieVerdict
    reason: str
    current_url: Optional[str]
    on_video: bool

    @property
    def is_ready(self) -> bool:
        return self.verdict == CookieVerdict.READY

    @property
    def is_dead(self) -> bool:
        return self.verdict == CookieVerdict.DEAD

    @property
    def is_captcha(self) -> bool:
        return self.verdict == CookieVerdict.CAPTCHA

    @property
    def is_blocked(self) -> bool:
        return self.verdict == CookieVerdict.BLOCKED


def _env_flag(name: str, default: bool = False) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    return raw.strip().lower() in _TRUE_ENV_VALUES


def _verdict_from_quick_check(check: QuickLoginCheck) -> CookieHealth:
    if check.state == "ready":
        return CookieHealth(CookieVerdict.READY, check.reason or "ready", check.current_url, check.on_video)
    if check.state == "dead":
        return CookieHealth(CookieVerdict.DEAD, check.reason or "dead", check.current_url, check.on_video)
    return CookieHealth(CookieVerdict.AMBIGUOUS, check.reason or "unknown_ui", check.current_url, check.on_video)


def _needs_action_probe_reason(reason: Optional[str]) -> bool:
    safe = reason or ""
    return (
        safe.startswith("on_video_no_login_btn")
        or safe in {"on_video_empty_dom", "on_video_empty_dom_after_settle", "ambiguous_ui"}
    )


def _visible_logged_out_reason(state: dict) -> Optional[str]:
    login_count = int(state.get("loginCount") or 0)
    login_modal_count = int(state.get("loginModalCount") or 0)
    comment_login_cta_count = int(state.get("commentLoginCtaCount") or 0)
    state_is_login_false = bool(state.get("stateIsLoginFalse"))
    state_name = str(state.get("state") or "").lower()
    logged_in_count = int(state.get("loggedInCount") or 0)
    state_is_login_true = bool(state.get("stateIsLoginTrue"))
    has_logged_in_marker = (
        state_name == "logged_in"
        or logged_in_count > 0
        or state_is_login_true
    )
    has_visible_login_ui = (
        (login_count > 0 and not has_logged_in_marker)
        or login_modal_count > 0
        or comment_login_cta_count > 0
        or state_is_login_false
    )
    if state_name != "logged_out" and not has_visible_login_ui:
        return None
    return (
        "visible_login_ui:"
        f"login={login_count}:"
        f"modal={login_modal_count}:"
        f"comment_cta={comment_login_cta_count}:"
        f"state_false={state_is_login_false}"
    )


async def _visible_logged_out_health_if_present(page: Page) -> Optional[CookieHealth]:
    state = await get_browser_login_state(page)
    reason = _visible_logged_out_reason(state)
    if not reason:
        return None
    try:
        current_url = page.url
    except Exception:
        current_url = None
    return CookieHealth(
        CookieVerdict.DEAD,
        reason,
        current_url,
        is_tiktok_media_page_url(current_url),
    )


def _manual_captcha_wait_enabled() -> bool:
    raw = os.getenv("ALLOW_MANUAL_CAPTCHA_SOLVE")
    if raw is None or not _env_flag("ALLOW_MANUAL_CAPTCHA_SOLVE", False):
        return False
    try:
        return not bool(_settings.PLAYWRIGHT_HEADLESS)
    except Exception:
        return False


async def _wait_for_manual_captcha_clear(page: Page, captcha_kind: str) -> bool:
    if not _manual_captcha_wait_enabled():
        return False

    deadline = time.monotonic() + max(10.0, _MANUAL_CAPTCHA_WAIT_SECONDS)
    logger.warning(
        "[COOKIE_HEALTH] TikTok %s challenge detected during login probe; "
        "waiting up to %.0fs for manual solve.",
        captcha_kind,
        _MANUAL_CAPTCHA_WAIT_SECONDS,
    )
    while time.monotonic() < deadline:
        await asyncio.sleep(_MANUAL_CAPTCHA_POLL_SECONDS)
        try:
            has_captcha, _ = await detect_captcha(page, wait_ms=0)
        except Exception as exc:
            logger.debug("[COOKIE_HEALTH] captcha detector failed during manual wait: %s", exc)
            continue
        if not has_captcha:
            await asyncio.sleep(1.0)
            try:
                still_clear, _ = await detect_captcha(page, wait_ms=0)
            except Exception:
                still_clear = False
            if not still_clear:
                logger.info("[COOKIE_HEALTH] TikTok challenge cleared manually; continuing login probe.")
                return True

    logger.warning(
        "[COOKIE_HEALTH] Manual captcha wait timed out after %.0fs; switching account.",
        _MANUAL_CAPTCHA_WAIT_SECONDS,
    )
    return False


async def _captcha_health_if_present(page: Page) -> Optional[CookieHealth]:
    try:
        has_captcha, captcha_type = await detect_captcha(page, wait_ms=0)
    except Exception as exc:
        logger.debug("[COOKIE_HEALTH] captcha detector failed: %s", exc)
        return None

    if not has_captcha:
        return None

    captcha_kind = captcha_type or "unknown"
    if await _wait_for_manual_captcha_clear(page, captcha_kind):
        return None

    try:
        current_url = page.url
    except Exception:
        current_url = None
    return CookieHealth(
        CookieVerdict.CAPTCHA,
        f"captcha_challenge:{captcha_kind}",
        current_url,
        is_tiktok_media_page_url(current_url),
    )


async def probe_cookie_health(page: Page) -> CookieHealth:
    """Главная функция: одно визуальное обращение к странице.

    Возвращает CookieHealth с verdict ∈ {READY, DEAD, AMBIGUOUS}. Caller сам
    решает, перепроверять ли AMBIGUOUS reload-ом.

    Особый случай: если первый probe вернул "on_video_empty_dom" (URL на
    видео-странице, но TikTok вообще не отрендерил comment/like/share/video
    иконки — признак ограниченного рендеринга для подозрительной куки), даём
    2с на дозагрузку и проверяем повторно. Если DOM по-прежнему пустой —
    помечаем как AMBIGUOUS, чтобы caller сразу перешёл к следующему аккаунту,
    не тратя 30+ секунд на final-editable-fallback и stealth_reject.
    """
    captcha_health = await _captcha_health_if_present(page)
    if captcha_health is not None:
        logger.info(
            "[COOKIE_HEALTH] probe → %s | reason=%s | url=%s | on_video=%s",
            captcha_health.verdict.value,
            captcha_health.reason,
            captcha_health.current_url,
            captcha_health.on_video,
        )
        return captcha_health

    logged_out_health = await _visible_logged_out_health_if_present(page)
    if logged_out_health is not None:
        logger.info(
            "[COOKIE_HEALTH] probe â†’ %s | reason=%s | url=%s | on_video=%s",
            logged_out_health.verdict.value,
            logged_out_health.reason,
            logged_out_health.current_url,
            logged_out_health.on_video,
        )
        return logged_out_health

    check = await quick_video_login_check(page)
    if check.reason == "on_video_empty_dom":
        await asyncio.sleep(2.0)
        logged_out_health = await _visible_logged_out_health_if_present(page)
        if logged_out_health is not None:
            logger.info(
                "[COOKIE_HEALTH] probe â†’ %s | reason=%s | url=%s | on_video=%s",
                logged_out_health.verdict.value,
                logged_out_health.reason,
                logged_out_health.current_url,
                logged_out_health.on_video,
            )
            return logged_out_health
        recheck = await quick_video_login_check(page)
        if recheck.reason == "on_video_empty_dom":
            health = CookieHealth(
                CookieVerdict.AMBIGUOUS,
                "on_video_empty_dom_after_settle",
                recheck.current_url,
                recheck.on_video,
            )
            logger.info(
                "[COOKIE_HEALTH] probe → %s | reason=%s | url=%s | on_video=%s",
                health.verdict.value,
                health.reason,
                health.current_url,
                health.on_video,
            )
            return health
        check = recheck

    if check.state == "ready" and _needs_action_probe_reason(check.reason) and not check.has_logged_in_marker:
        health = CookieHealth(
            CookieVerdict.AMBIGUOUS,
            check.reason or "on_video_auth_unconfirmed",
            check.current_url,
            check.on_video,
        )
        logger.info(
            "[COOKIE_HEALTH] probe â†’ %s | reason=%s | url=%s | on_video=%s",
            health.verdict.value,
            health.reason,
            health.current_url,
            health.on_video,
        )
        return health

    health = _verdict_from_quick_check(check)
    logger.info(
        "[COOKIE_HEALTH] probe → %s | reason=%s | url=%s | on_video=%s",
        health.verdict.value,
        health.reason,
        health.current_url,
        health.on_video,
    )
    return health


# ---------------------------------------------------------------------------
# Persisted bookkeeping (повторные провалы)
# ---------------------------------------------------------------------------

def _threshold() -> int:
    raw = getattr(_settings, "COOKIE_DEAD_RETIRE_THRESHOLD", 2)
    try:
        return max(1, int(raw))
    except (TypeError, ValueError):
        return 2


def _blocked_threshold() -> int:
    raw = getattr(_settings, "COOKIE_BLOCKED_RETIRE_THRESHOLD", _threshold())
    try:
        return max(1, int(raw))
    except (TypeError, ValueError):
        return _threshold()


def _bump_validation_fail_count(db: Session, account_id: int) -> Optional[int]:
    """Атомарно увеличивает Account.validation_fail_count.

    Стратегия (по убыванию предпочтительности):
      1) Если у Session есть `begin_nested` (нормальный SQLAlchemy Session) —
         используем SAVEPOINT: инкремент атомарен, но НЕ коммитит
         родительскую транзакцию caller'а. Это самый правильный путь, без
         побочных эффектов на чужой scope.
      2) Если begin_nested недоступен (in-process fake, синхронный sqlite
         без SAVEPOINT) — fallback: flush() и пусть caller сам решает,
         коммитить или откатываться.
      3) Если и flush недоступен — коммитим явно (последний вариант).

    Возвращает новое значение validation_fail_count или None при ошибке.
    """
    def _do_update() -> Optional[int]:
        query = db.query(Account).filter(Account.id == account_id)
        if hasattr(query, "with_for_update"):
            query = query.with_for_update()
        live = query.first()
        if not live:
            return None
        live.validation_fail_count = int(getattr(live, "validation_fail_count", 0) or 0) + 1
        return int(live.validation_fail_count or 0)

    # 1) SAVEPOINT через begin_nested.
    begin_nested = getattr(db, "begin_nested", None)
    if callable(begin_nested):
        try:
            with begin_nested():
                new_count = _do_update()
                if new_count is None:
                    return None
                flush = getattr(db, "flush", None)
                if callable(flush):
                    flush()
                return new_count
        except Exception:
            logger.exception(
                "[COOKIE_HEALTH] nested savepoint failed for account=%s",
                account_id,
            )
            try:
                db.rollback()
            except Exception:
                pass
            return None

    # 2) Fallback: flush() — caller сам коммитит.
    flush = getattr(db, "flush", None)
    if callable(flush):
        try:
            new_count = _do_update()
            if new_count is None:
                return None
            flush()
            return new_count
        except Exception:
            logger.exception(
                "[COOKIE_HEALTH] flush-only bump failed for account=%s",
                account_id,
            )
            try:
                db.rollback()
            except Exception:
                pass
            return None

    # 3) Полный fallback (legacy): своя транзакция.
    try:
        new_count = _do_update()
        if new_count is None:
            return None
        if hasattr(db, "commit"):
            db.commit()
        return new_count
    except Exception:
        logger.exception(
            "[COOKIE_HEALTH] outermost commit bump failed for account=%s",
            account_id,
        )
        try:
            db.rollback()
        except Exception:
            pass
        return None


def register_dead_session(
    db: Session,
    account: Account,
    reason: Optional[str],
) -> bool:
    """Аккаунт показал DEAD при визуальной проверке.

    - Атомарно увеличивает Account.validation_fail_count.
    - Если стало >= COOKIE_DEAD_RETIRE_THRESHOLD (default 1) → помечает
      аккаунт reauth_needed: он больше НЕ попадёт в селекторы
      get_free_account_for_main_comment / _for_reply пока не пройдёт
      перевалидацию.
    - При первом DEAD — soft_error с причиной (короткий cooldown).

    Возвращает True, если аккаунт окончательно выведен из работы
    (reauth_needed). False — если только soft.
    """
    if not account:
        return False
    threshold = _threshold()

    new_count = _bump_validation_fail_count(db, account.id)
    if new_count is None:
        return False
    # синхронизируем in-memory объект (caller может ещё держать ссылку)
    try:
        account.validation_fail_count = new_count
    except Exception:
        pass

    logger.warning(
        "[COOKIE_HEALTH] account=%s dead session #%s/%s (reason=%r)",
        account.id,
        new_count,
        threshold,
        (reason or "")[:120],
    )
    if new_count >= threshold:
        try:
            mark_needs_reauth(db, account.id, reason=f"dead_session_repeat:{reason}"[:200])
        except Exception:
            logger.exception(
                "[COOKIE_HEALTH] mark_needs_reauth failed for account=%s",
                account.id,
            )
            return False
        logger.warning(
            "[COOKIE_HEALTH] account=%s retired from rotation: %s consecutive dead sessions",
            account.id,
            new_count,
        )
        return True

    # Первый dead — мягкий cooldown.
    try:
        mark_soft_error(db, account.id, f"dead_session:{reason}"[:200])
    except Exception:
        logger.debug("[COOKIE_HEALTH] mark_soft_error failed", exc_info=True)
    return False


def register_alive_session(db: Session, account: Account) -> None:
    """Аккаунт визуально жив (READY). Сам счётчик dead-провалов трогать
    нельзя пока публикация не подтверждена: иначе мы будем сбрасывать
    штраф просто за «дошёл до страницы». Здесь только лог.

    Полное обнуление validation_fail_count происходит только в
    services.account_service.mark_success (когда был реальный публикованный
    комментарий) — это правильный момент очистить штраф.
    """
    if not account:
        return
    logger.debug(
        "[COOKIE_HEALTH] account=%s alive probe (count=%s)",
        account.id,
        getattr(account, "validation_fail_count", None),
    )


def register_ambiguous_session(db: Session, account: Account, reason: Optional[str]) -> None:
    """AMBIGUOUS не наказывает аккаунт — это лог + лёгкий soft error.

    Идея: если страница не отдала ни logged_in, ни logged_out маркера, скорее
    всего проблема в proxy/UI таймаутах, не в куки. Не тратим лимит «двух
    провалов».
    """
    if not account:
        return
    safe_reason = (reason or "ambiguous_ui")[:200]
    try:
        mark_soft_error(db, account.id, f"login_ambiguous:{safe_reason}")
    except Exception:
        logger.debug("[COOKIE_HEALTH] ambiguous soft_error skipped", exc_info=True)
    logger.info(
        "[COOKIE_HEALTH] account=%s ambiguous probe (reason=%r); soft cooldown applied without retire counter bump",
        account.id,
        safe_reason[:120],
    )


def register_captcha_session(db: Session, account: Account, reason: Optional[str]) -> None:
    """Visible challenge: cooldown account, but do not count it as dead cookies."""
    if not account:
        return
    safe_reason = (reason or "captcha_challenge:unknown")[:200]
    try:
        mark_soft_error(db, account.id, safe_reason)
    except Exception:
        logger.debug("[COOKIE_HEALTH] captcha soft_error skipped", exc_info=True)
    logger.warning(
        "[COOKIE_HEALTH] account=%s captcha probe (reason=%r); runtime captcha cooldown applied",
        account.id,
        safe_reason[:120],
    )


def register_blocked_session(db: Session, account: Account, reason: Optional[str]) -> bool:
    """Usable video UI did not render after settle; quarantine and retire after repeats."""
    if not account:
        return False
    threshold = _blocked_threshold()
    safe_reason = (reason or "blocked_ui:unknown")[:200]
    if not safe_reason.startswith("blocked_ui:"):
        safe_reason = f"blocked_ui:{safe_reason}"[:200]

    new_count = _bump_validation_fail_count(db, account.id)
    if new_count is None:
        return False
    try:
        account.validation_fail_count = new_count
    except Exception:
        pass

    logger.warning(
        "[COOKIE_HEALTH] account=%s blocked UI #%s/%s (reason=%r)",
        account.id,
        new_count,
        threshold,
        safe_reason[:120],
    )
    if new_count >= threshold:
        try:
            mark_needs_reauth(db, account.id, reason=f"blocked_ui_repeat:{safe_reason}"[:200])
        except Exception:
            logger.exception(
                "[COOKIE_HEALTH] mark_needs_reauth failed for blocked account=%s",
                account.id,
            )
            return False
        logger.warning(
            "[COOKIE_HEALTH] account=%s retired from rotation after repeated blocked UI probes",
            account.id,
        )
        return True

    try:
        mark_soft_error(db, account.id, safe_reason)
    except Exception:
        logger.debug("[COOKIE_HEALTH] blocked soft_error skipped", exc_info=True)
    return False
