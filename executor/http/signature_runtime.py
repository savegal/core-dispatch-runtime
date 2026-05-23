from __future__ import annotations

import asyncio
from dataclasses import dataclass
import logging
from typing import Any, Dict, Optional, Tuple
from urllib.parse import parse_qsl, parse_qs, urlencode, urlparse, urlunparse

from patchright.async_api import Page

from executor.http.request_profile import RequestProfile

logger = logging.getLogger("executor.http.signature_runtime")


_SIGNATURE_PARAM_KEYS = ("X-Bogus", "X-Gnarly", "a_bogus")
_SIGNER_READY_JS = """
() => {
    if (
        window.byted_acrawler &&
        (
            typeof window.byted_acrawler.sign === "function" ||
            typeof window.byted_acrawler.frontierSign === "function"
        )
    ) {
        return true;
    }
    try {
        const entries = performance.getEntriesByType("resource");
        for (let i = entries.length - 1; i >= Math.max(0, entries.length - 30); i--) {
            const name = String(entries[i].name || "");
            if (name.includes("X-Bogus") || name.includes("a_bogus")) {
                return true;
            }
        }
    } catch (error) {
        // Ignore performance inspection failures.
    }
    return false;
}
"""


def _normalize_signature_value(value: Any) -> Optional[str]:
    if value is None:
        return None
    value_str = str(value).strip()
    return value_str or None


@dataclass(frozen=True)
class SignatureBundle:
    x_bogus: Optional[str] = None
    x_gnarly: Optional[str] = None
    a_bogus: Optional[str] = None
    mode: str = "unsigned"
    source: str = "none"
    signer_available: bool = False
    notes: Optional[str] = None

    def has_any(self) -> bool:
        return bool(self.x_bogus or self.x_gnarly or self.a_bogus)


def split_explicit_signature_params(dynamic_params: Dict[str, Any]) -> Tuple[Dict[str, Any], SignatureBundle]:
    clean_params = dict(dynamic_params)
    explicit_x_bogus = _normalize_signature_value(
        clean_params.pop("X-Bogus", None) or clean_params.pop("x-bogus", None)
    )
    explicit_x_gnarly = _normalize_signature_value(
        clean_params.pop("X-Gnarly", None) or clean_params.pop("x-gnarly", None)
    )
    explicit_a_bogus = _normalize_signature_value(
        clean_params.pop("a_bogus", None)
        or clean_params.pop("A-Bogus", None)
        or clean_params.pop("a-bogus", None)
    )
    explicit_values: Dict[str, Optional[str]] = {
        "X-Bogus": explicit_x_bogus,
        "X-Gnarly": explicit_x_gnarly,
        "a_bogus": explicit_a_bogus,
    }
    if explicit_values["X-Bogus"] or explicit_values["X-Gnarly"] or explicit_values["a_bogus"]:
        return (
            clean_params,
            SignatureBundle(
                x_bogus=explicit_values["X-Bogus"],
                x_gnarly=explicit_values["X-Gnarly"],
                a_bogus=explicit_values["a_bogus"],
                mode="explicit_provided",
                source="dynamic_params",
                signer_available=True,
            ),
        )
    return clean_params, SignatureBundle()


def build_unsigned_request_url(
    profile: RequestProfile,
    dynamic_params: Dict[str, Any],
    cookies: Optional[Dict[str, str]] = None,
    runtime_profile: Optional[Dict[str, Any]] = None,
) -> str:
    params, _ = profile.build_request_parts(
        dynamic_params,
        cookies,
        runtime_profile,
        include_cookie_signatures=False,
    )
    return profile.build_url_from_params(params)


def apply_signature_bundle_to_url(url: str, bundle: SignatureBundle) -> str:
    if not bundle.has_any():
        return url

    parsed = urlparse(url)
    query_pairs = [(key, value) for key, value in parse_qsl(parsed.query, keep_blank_values=True) if key not in _SIGNATURE_PARAM_KEYS]
    if bundle.x_bogus:
        query_pairs.append(("X-Bogus", bundle.x_bogus))
    if bundle.x_gnarly:
        query_pairs.append(("X-Gnarly", bundle.x_gnarly))
    if bundle.a_bogus:
        query_pairs.append(("a_bogus", bundle.a_bogus))
    return urlunparse(parsed._replace(query=urlencode(query_pairs, doseq=True)))


def borrow_signature_bundle_from_cookies(cookies: Optional[Dict[str, str]]) -> SignatureBundle:
    if not cookies:
        return SignatureBundle()

    x_bogus = _normalize_signature_value(cookies.get("X-Bogus") or cookies.get("x-bogus"))
    x_gnarly = _normalize_signature_value(cookies.get("X-Gnarly") or cookies.get("x-gnarly"))
    a_bogus = _normalize_signature_value(cookies.get("a_bogus") or cookies.get("a-bogus"))
    if not (x_bogus or x_gnarly or a_bogus):
        return SignatureBundle()

    return SignatureBundle(
        x_bogus=x_bogus,
        x_gnarly=x_gnarly,
        a_bogus=a_bogus,
        mode="borrowed_cookie",
        source="cookies",
        signer_available=False,
        notes="low_confidence_fallback",
    )


async def wait_for_runtime_signer_ready(page: Optional[Page], timeout_ms: int = 1200) -> bool:
    if page is None:
        return False

    try:
        await page.wait_for_function(_SIGNER_READY_JS, timeout=timeout_ms)
        return True
    except Exception:
        try:
            return bool(await page.evaluate(_SIGNER_READY_JS))
        except Exception:
            return False


async def generate_runtime_signature_bundle(page: Optional[Page], unsigned_url: str) -> SignatureBundle:
    if page is None:
        return SignatureBundle()

    js_code = """
    async (url) => {
        const normalize = (value) => {
            if (value === null || value === undefined) return null;
            const asString = String(value).trim();
            return asString || null;
        };

        const coerceResult = (result, source) => {
            if (!result) return null;
            if (typeof result === "string") {
                const xBogus = normalize(result);
                return xBogus ? {
                    x_bogus: xBogus,
                    x_gnarly: null,
                    signer_available: true,
                    source,
                } : null;
            }

            if (typeof result === "object") {
                const xBogus = normalize(
                    result["X-Bogus"] ??
                    result.xBogus ??
                    result.x_bogus ??
                    result.signature ??
                    result.value
                );
                const xGnarly = normalize(
                    result["X-Gnarly"] ??
                    result.xGnarly ??
                    result.x_gnarly ??
                    result.gnarly
                );
                const aBogus = normalize(
                    result["a_bogus"] ??
                    result["a-bogus"] ??
                    result.a_bogus ??
                    result.aBogus
                );
                if (xBogus || xGnarly || aBogus) {
                    return {
                        x_bogus: xBogus,
                        x_gnarly: xGnarly,
                        a_bogus: aBogus,
                        signer_available: true,
                        source,
                    };
                }
            }
            return null;
        };

        const candidates = [];
        if (window.byted_acrawler) {
            if (typeof window.byted_acrawler.sign === "function") {
                candidates.push(() => window.byted_acrawler.sign({ url }));
                candidates.push(() => window.byted_acrawler.sign(url));
            }
            if (typeof window.byted_acrawler.frontierSign === "function") {
                candidates.push(() => window.byted_acrawler.frontierSign({ url }));
                candidates.push(() => window.byted_acrawler.frontierSign(url));
            }
        }

        for (const candidate of candidates) {
            try {
                const result = await candidate();
                const coerced = coerceResult(result, "window.byted_acrawler");
                if (coerced) {
                    return coerced;
                }
            } catch (error) {
                // Keep trying other variants.
            }
        }

        return {
            x_bogus: null,
            x_gnarly: null,
            a_bogus: null,
            signer_available: !!(
                window.byted_acrawler &&
                (
                    typeof window.byted_acrawler.sign === "function" ||
                    typeof window.byted_acrawler.frontierSign === "function"
                )
            ),
            source: "none",
        };
    }
    """

    try:
        raw_result = await page.evaluate(js_code, unsigned_url)
    except Exception as exc:
        logger.debug("[SIGN] Runtime signer evaluation failed", exc_info=True)
        return SignatureBundle(mode="unsigned", source="signer_error", signer_available=False, notes=str(exc)[:120])

    if not isinstance(raw_result, dict):
        return SignatureBundle()

    x_bogus = _normalize_signature_value(raw_result.get("x_bogus"))
    x_gnarly = _normalize_signature_value(raw_result.get("x_gnarly"))
    a_bogus = _normalize_signature_value(raw_result.get("a_bogus"))
    signer_available = bool(raw_result.get("signer_available"))
    source = _normalize_signature_value(raw_result.get("source")) or "runtime"

    if x_bogus or x_gnarly or a_bogus:
        return SignatureBundle(
            x_bogus=x_bogus,
            x_gnarly=x_gnarly,
            a_bogus=a_bogus,
            mode="runtime_signed",
            source=source,
            signer_available=True,
        )

    return SignatureBundle(
        mode="unsigned",
        source=source,
        signer_available=signer_available,
        notes="runtime_signer_missing_output" if signer_available else "runtime_signer_unavailable",
    )


async def harvest_signature_from_traffic(
    page: Optional[Page],
    timeout_ms: int = 5000,
) -> SignatureBundle:
    """
    Watches TikTok's own page traffic and captures any recently generated
    X-Bogus / a_bogus values as a best-effort fallback.
    """
    if page is None:
        return SignatureBundle()

    result: Dict[str, Optional[str]] = {
        "x_bogus": None,
        "x_gnarly": None,
        "a_bogus": None,
        "source_url": None,
    }
    event = asyncio.Event()

    def on_request(request: Any) -> None:
        try:
            url = str(request.url)
        except Exception:
            return
        if "tiktok.com/api/" not in url:
            return
        parsed = urlparse(url)
        qs = parse_qs(parsed.query)
        x_bogus = _normalize_signature_value((qs.get("X-Bogus") or [None])[0])
        x_gnarly = _normalize_signature_value((qs.get("X-Gnarly") or [None])[0])
        a_bogus = _normalize_signature_value((qs.get("a_bogus") or [None])[0])
        if x_bogus or x_gnarly or a_bogus:
            result["x_bogus"] = x_bogus
            result["x_gnarly"] = x_gnarly
            result["a_bogus"] = a_bogus
            result["source_url"] = url[:160]
            event.set()

    add_request_listener = getattr(page, "on", None)
    if not callable(add_request_listener):
        return SignatureBundle()

    add_request_listener("request", on_request)
    try:
        try:
            await page.evaluate(
                """() => {
                    window.scrollBy(0, 260);
                    window.dispatchEvent(new Event("scroll"));
                    return true;
                }"""
            )
        except Exception:
            pass

        try:
            await asyncio.wait_for(event.wait(), timeout=timeout_ms / 1000)
        except asyncio.TimeoutError:
            pass
    finally:
        try:
            remove_request_listener = getattr(page, "remove_listener", None)
            if callable(remove_request_listener):
                remove_request_listener("request", on_request)
        except Exception:
            pass

    if result["x_bogus"] or result["x_gnarly"] or result["a_bogus"]:
        logger.info(
            f"[SIGN] Harvested signature from TikTok traffic: {result.get('source_url') or 'unknown'}"
        )
        return SignatureBundle(
            x_bogus=result["x_bogus"],
            x_gnarly=result["x_gnarly"],
            a_bogus=result["a_bogus"],
            mode="harvested_traffic",
            source="page_request_intercept",
            signer_available=True,
            notes="borrowed_from_recent_page_request",
        )

    return SignatureBundle()


async def resolve_request_signature(
    *,
    page: Optional[Page],
    profile: RequestProfile,
    dynamic_params: Dict[str, Any],
    cookies: Optional[Dict[str, str]],
    runtime_profile: Optional[Dict[str, Any]],
    allow_cookie_fallback: bool,
) -> Tuple[Dict[str, Any], str, SignatureBundle]:
    clean_dynamic_params, explicit_bundle = split_explicit_signature_params(dynamic_params)
    unsigned_url = build_unsigned_request_url(
        profile,
        clean_dynamic_params,
        cookies=cookies,
        runtime_profile=runtime_profile,
    )

    if explicit_bundle.has_any():
        return clean_dynamic_params, apply_signature_bundle_to_url(unsigned_url, explicit_bundle), explicit_bundle

    runtime_bundle = await generate_runtime_signature_bundle(page, unsigned_url)
    if runtime_bundle.has_any():
        return clean_dynamic_params, apply_signature_bundle_to_url(unsigned_url, runtime_bundle), runtime_bundle

    harvest_bundle = await harvest_signature_from_traffic(page)
    if harvest_bundle.has_any():
        return clean_dynamic_params, apply_signature_bundle_to_url(unsigned_url, harvest_bundle), harvest_bundle

    if allow_cookie_fallback:
        cookie_bundle = borrow_signature_bundle_from_cookies(cookies)
        if cookie_bundle.has_any():
            return clean_dynamic_params, apply_signature_bundle_to_url(unsigned_url, cookie_bundle), cookie_bundle

    return clean_dynamic_params, unsigned_url, runtime_bundle
