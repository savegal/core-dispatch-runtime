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
from services.cookie_validator import NormalizedCookies, normalize_cookie_records

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
    has_full_session = any(cookies.get(c) for c in FULL_SESSION_COOKIES)
    has_partial_session = any(cookies.get(c) for c in PARTIAL_SESSION_COOKIES)
    has_ttwid = bool(cookies.get("ttwid"))
    has_csrf = bool(cookies.get("tt_csrf_token"))

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
    """Block heavy resources only for the duration of cookie enrichment."""
    async def filter_resources(route):
        if route.request.resource_type in {"image", "media", "font"}:
            await route.abort()
        else:
            await route.fallback()

    await page.route("**/*", filter_resources)
    try:
        return await _enrich_cookies_via_browser(
            page,
            existing_cookies,
            correlation_id=correlation_id,
            timeout_seconds=timeout_seconds,
        )
    finally:
        try:
            await page.unroute("**/*", filter_resources)
        except Exception as exc:
            logger.debug("[ENRICH] Resource filter cleanup failed for %s: %s", correlation_id or "?", exc)


async def _enrich_cookies_via_browser(
    page: Page,
    existing_cookies: Dict[str, str],
    *,
    correlation_id: Optional[str],
    timeout_seconds: float,
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

    deadline = started + timeout_seconds
    best_cookies = NormalizedCookies(existing_cookies, records=getattr(existing_cookies, "records", ()))
    # Browser cookie identity includes domain and path, not just name.
    records = {
        (c["name"], c.get("domain", ".tiktok.com"), c.get("path", "/")): dict(c)
        for c in getattr(existing_cookies, "records", ())
    }
    scrolls = iter([200, 350, 150, 400, 100])

    # Always collect once, even if navigation has already consumed the budget.
    while True:
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
            for cookie in cookies_list:
                if cookie.get("name") and cookie.get("value") is not None:
                    key = (cookie["name"], cookie.get("domain", ".tiktok.com"), cookie.get("path", "/"))
                    records[key] = dict(cookie)
            fresh = normalize_cookie_records(list(records.values()))
            best_cookies = NormalizedCookies({**existing_cookies, **fresh}, records=fresh.records)
        except Exception:
            if time.monotonic() >= deadline:
                break
            await asyncio.sleep(0.4)
            continue

        if all(best_cookies.get(c) for c in CRITICAL_ENRICHMENT_COOKIES):
            break
        if time.monotonic() >= deadline:
            break

        try:
            await dismiss_cookie_banners(page)
        except Exception:
            pass
        scroll_y = next(scrolls, None)
        if scroll_y is not None:
            try:
                await page.evaluate(f"window.scrollBy(0, {scroll_y})")
            except Exception:
                scrolls = iter(())
        await asyncio.sleep(0.5)

    new_cookies = [
        c for c in ENRICHMENT_TARGET_COOKIES
        if c in best_cookies and c not in existing_cookies
    ]
    strength_after = assess_cookie_strength(best_cookies)
    success = strength_after in ("full", "enriched")
    if success:
        error = None
    elif all(best_cookies.get(c) for c in CRITICAL_ENRICHMENT_COOKIES):
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
    result = [dict(c) for c in getattr(cookies_dict, "records", ())]
    recorded_names = {c["name"] for c in result}
    guard = next((c for c in result if c["name"].lower() == "sid_guard"
                  and c["value"] == cookies_dict.get(c["name"])), {})
    for name, value in cookies_dict.items():
        if name in recorded_names:
            continue
        # Derived session aliases inherit the selected sid_guard's scope.
        scope = guard if name in FULL_SESSION_COOKIES and len(value) == 32 and guard.get("value", "")[:32] == value else {}
        result.append({**scope, "name": name, "value": value})
    for cookie in result:
        cookie.setdefault("domain", ".tiktok.com")
        cookie.setdefault("path", "/")
        cookie.setdefault("secure", True)
        cookie.setdefault("httpOnly", cookie["name"].lower() in {"sid_guard", "sessionid", "sessionid_ss", "sid_tt"})
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
