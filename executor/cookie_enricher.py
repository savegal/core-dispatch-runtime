"""
Cookie Enricher для TikTok аккаунтов.

Зачем:
Купленные cookies часто содержат только sid_guard + msToken + tt-target-idc.
Для стабильной работы DOM-публикации лучше дать TikTok самому догенерировать
browser cookies: ttwid, tt_csrf_token, tt_chain_token, odin_tt и т.д.

Как работает:
- Использует уже созданный Playwright page/context аккаунта.
- Открывает https://www.tiktok.com/foryou через тот же proxy/fingerprint.
- Собирает fresh cookies из browser context.
- Мержит их с текущими cookies и сохраняет обратно в Account.cookies.
"""
from __future__ import annotations

import asyncio
import json
import logging
import time
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Set

from patchright.async_api import Page
from sqlalchemy.orm import Session

from db.models import Account
from executor.dom_actions import dismiss_cookie_banners

logger = logging.getLogger("executor.cookie_enricher")


ENRICHMENT_TARGET_COOKIES: Set[str] = {
    "ttwid",
    "tt_csrf_token",
    "tt_chain_token",
    "odin_tt",
    "passport_csrf_token",
    "cmpl_token",
    "s_v_web_id",
    "_ttp",
    # Auth cookies — harvested when patchright/real Chrome triggers session restore
    "sessionid",
    "sessionid_ss",
    "sid_tt",
}

CRITICAL_ENRICHMENT_COOKIES: Set[str] = {"ttwid", "tt_csrf_token"}
FULL_SESSION_COOKIES: Set[str] = {"sessionid", "sessionid_ss", "sid_tt"}
PARTIAL_SESSION_COOKIES: Set[str] = {"sid_guard"}

DEAD_URL_MARKERS = ("/login", "/signup", "/verify", "/messages", "/inbox", "/auth")


def assess_cookie_strength(cookies: Dict[str, str]) -> str:
    """
    Возвращает:
    - full: sessionid/sessionid_ss/sid_tt + ttwid + tt_csrf_token
    - enriched: sid_guard + ttwid + tt_csrf_token
    - minimal: sid_guard без generated cookies
    - dead: нет auth cookies
    """
    has_full_session = any(c in cookies for c in FULL_SESSION_COOKIES)
    has_partial_session = any(c in cookies for c in PARTIAL_SESSION_COOKIES)
    has_ttwid = "ttwid" in cookies
    has_csrf = "tt_csrf_token" in cookies

    if has_full_session and has_ttwid and has_csrf:
        return "full"
    if has_partial_session and has_ttwid and has_csrf:
        return "enriched"
    if has_partial_session:
        return "minimal"
    return "dead"


def needs_enrichment(account: Account) -> bool:
    """
    True если cookies minimal/dead или не парсятся.
    """
    try:
        from services.cookie_validator import get_cookies_for_account, parse_cookies_to_dict

        raw = get_cookies_for_account(account)
        cookies = parse_cookies_to_dict(raw)
        return assess_cookie_strength(cookies) in ("minimal", "dead")
    except Exception:
        return True


@dataclass
class EnrichmentResult:
    success: bool
    strength_before: str = "unknown"
    strength_after: str = "unknown"
    new_cookies: List[str] = field(default_factory=list)
    all_cookies_json: Optional[str] = None
    error: Optional[str] = None
    redirected: bool = False
    elapsed_seconds: float = 0.0

    def log_summary(self, account_id: int, correlation_id: Optional[str] = None) -> None:
        if self.success:
            logger.info(
                "[ENRICH] Account %d enriched in %.1fs | %s -> %s | new=%s | tag=%s",
                account_id,
                self.elapsed_seconds,
                self.strength_before,
                self.strength_after,
                self.new_cookies,
                correlation_id or "-",
            )
        else:
            logger.warning(
                "[ENRICH] Account %d enrichment failed in %.1fs | error=%s | redirected=%s | tag=%s",
                account_id,
                self.elapsed_seconds,
                self.error,
                self.redirected,
                correlation_id or "-",
            )


async def enrich_cookies_via_browser(
    page: Page,
    existing_cookies: Dict[str, str],
    *,
    correlation_id: Optional[str] = None,
    timeout_seconds: float = 25.0,
) -> EnrichmentResult:
    """
    Обогащает cookies через реальный browser visit.
    """
    started = time.monotonic()
    strength_before = assess_cookie_strength(existing_cookies)

    try:
        await page.goto(
            "https://www.tiktok.com/foryou",
            wait_until="domcontentloaded",
            timeout=12000,
        )
    except Exception as exc:
        exc_str = str(exc).lower()
        if "timeout" not in exc_str and "net::" not in exc_str:
            return EnrichmentResult(
                success=False,
                strength_before=strength_before,
                error=f"navigation_failed: {str(exc)[:80]}",
                elapsed_seconds=time.monotonic() - started,
            )
        logger.debug("[ENRICH] Navigation timeout for %s; continuing", correlation_id or "?")

    await asyncio.sleep(1.5)
    try:
        await dismiss_cookie_banners(page)
    except Exception as exc:
        logger.debug("[ENRICH] Cookie banner cleanup failed for %s: %s", correlation_id or "?", exc)

    try:
        current_url = (page.url or "").lower()
        for marker in DEAD_URL_MARKERS:
            if marker in current_url:
                return EnrichmentResult(
                    success=False,
                    strength_before=strength_before,
                    error=f"redirected_to_{marker.strip('/')}",
                    redirected=True,
                    elapsed_seconds=time.monotonic() - started,
                )
    except Exception:
        pass

    # Чуть-чуть поведения, чтобы страница догенерировала browser cookies.
    for i, scroll_y in enumerate([200, 350, 150, 400, 100]):
        if i in (0, 2):
            try:
                await dismiss_cookie_banners(page)
            except Exception:
                pass
        try:
            await page.evaluate(f"window.scrollBy(0, {scroll_y})")
        except Exception:
            break
        await asyncio.sleep(0.7 + i * 0.25)

    deadline = started + timeout_seconds
    best_cookies: Dict[str, str] = dict(existing_cookies)

    while time.monotonic() < deadline:
        try:
            current_url = (page.url or "").lower()
            for marker in DEAD_URL_MARKERS:
                if marker in current_url:
                    return EnrichmentResult(
                        success=False,
                        strength_before=strength_before,
                        error=f"redirected_to_{marker.strip('/')}_during_enrichment",
                        redirected=True,
                        elapsed_seconds=time.monotonic() - started,
                    )
        except Exception:
            pass

        try:
            cookies_list = await page.context.cookies()
            fresh = {
                c.get("name"): c.get("value")
                for c in cookies_list
                if c.get("name") and c.get("value") is not None
            }
            best_cookies = {**existing_cookies, **fresh}
        except Exception:
            await asyncio.sleep(0.4)
            continue

        if all(c in best_cookies for c in CRITICAL_ENRICHMENT_COOKIES):
            break

        try:
            await dismiss_cookie_banners(page)
        except Exception:
            pass
        await asyncio.sleep(0.5)

    new_cookies = [
        c for c in ENRICHMENT_TARGET_COOKIES
        if c in best_cookies and c not in existing_cookies
    ]
    strength_after = assess_cookie_strength(best_cookies)
    success = strength_after in ("full", "enriched")
    if success:
        error = None
    elif all(c in best_cookies for c in CRITICAL_ENRICHMENT_COOKIES):
        error = "auth_cookies_not_restored"
    else:
        error = "critical_cookies_not_generated"

    return EnrichmentResult(
        success=success,
        strength_before=strength_before,
        strength_after=strength_after,
        new_cookies=new_cookies,
        all_cookies_json=_serialize_cookies_to_json(best_cookies),
        error=error,
        elapsed_seconds=time.monotonic() - started,
    )


def _serialize_cookies_to_json(cookies_dict: Dict[str, str]) -> str:
    """
    Сериализует cookies в JSON-array формат, который уже понимает проект.
    """
    result = []
    for name, value in cookies_dict.items():
        result.append({
            "name": name,
            "value": value,
            "domain": ".tiktok.com",
            "path": "/",
            "secure": True,
            "httpOnly": name in {"sid_guard", "sessionid", "sessionid_ss", "sid_tt"},
        })
    return json.dumps(result, ensure_ascii=False)


def save_enriched_cookies_to_db(
    db: Session,
    account_id: int,
    cookies_json: str,
) -> bool:
    """
    Сохраняет enriched cookies в БД.
    """
    from datetime import datetime, timezone

    try:
        account = db.query(Account).filter(Account.id == account_id).first()
        if not account:
            logger.error("[ENRICH] Account %d not found in DB", account_id)
            return False

        account.cookies = cookies_json

        if hasattr(account, "cookie_enriched_at"):
            account.cookie_enriched_at = datetime.now(timezone.utc)

        db.commit()
        logger.info("[ENRICH] Saved enriched cookies for account %d (%d chars)", account_id, len(cookies_json))
        return True

    except Exception as exc:
        db.rollback()
        logger.error("[ENRICH] Failed to save cookies for account %d: %s", account_id, exc)
        return False
