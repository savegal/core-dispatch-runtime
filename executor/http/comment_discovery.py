"""Read parent IDs through the live page, without opening the comment drawer."""
from __future__ import annotations

import asyncio
import json
import logging
from contextlib import asynccontextmanager
from contextvars import ContextVar
from urllib.parse import urlparse

from services.comment_search import contains_search_phrase
from services.cookie_validator import normalize_cookie_records
from executor.http.request_profile import RequestProfile
from executor.http.playwright_http_client import _extract_runtime_profile
from executor.http.request_evidence import observe_request_security, resolve_security_evidence


logger = logging.getLogger(__name__)
_READ_BATCH = ContextVar("comment_read_batch", default=None)


@asynccontextmanager
async def comment_read_batch(page):
    """Reuse request metadata within one traversal, never across observer rounds."""
    existing = _READ_BATCH.get()
    if existing is not None and existing["page"] is page:
        yield
        return
    token = _READ_BATCH.set({"page": page, "snapshot": None})
    try:
        yield
    finally:
        _READ_BATCH.reset(token)


async def _read_request_metadata(page):
    batch = _READ_BATCH.get()
    if batch is not None and batch["page"] is page and batch["snapshot"] is not None:
        return batch["snapshot"]
    runtime = await asyncio.wait_for(_extract_runtime_profile(page), timeout=4.0)
    cookies = normalize_cookie_records(await page.context.cookies())
    if batch is not None and batch["page"] is page:
        batch["snapshot"] = (runtime, cookies)
    return runtime, cookies


_COMMENT_LIST_FETCH_JS = """
async url => {
    const controller = new AbortController();
    const timer = setTimeout(() => controller.abort(), 5000);
    try {
        const response = await window.fetch(url, {credentials: "include", signal: controller.signal});
        return {status: response.status, text: await response.text(), url: response.url};
    } finally { clearTimeout(timer); }
}
"""

# Run activity only on a retry, in the same main-world evaluation as its GET.
# Event dispatch can wake lazy page listeners; it is not proof of SDK readiness.
_COMMENT_LIST_RETRY_FETCH_JS = _COMMENT_LIST_FETCH_JS.replace(
    "    const controller =", """    try { window.scrollBy(0, 120); } catch (error) {}
    try { window.dispatchEvent(new Event("mousemove")); } catch (error) {}
    const controller =""",
)
_COMMENT_LIST_MAX_RETRIES = 2
_COMMENT_LIST_RETRY_DELAY_SECONDS = 2.0


async def fetch_comment_page(page, aweme_id: str, cursor=0, count: int = 20, *, fresh: bool = False):
    return await _fetch_comment_list(page, aweme_id, cursor, count, fresh=fresh)


async def fetch_reply_page(page, aweme_id: str, parent_comment_id: str, cursor=0, count: int = 20):
    """Read a fresh reply branch using the page's initialized signer."""
    return await _fetch_comment_list(page, aweme_id, cursor, count, fresh=True, parent_comment_id=parent_comment_id)


async def _fetch_comment_list(page, aweme_id, cursor, count, *, fresh, parent_comment_id=None):
    meta = {"reason": "not_started", "http_status": None, "body_len": 0, "attempts": 0, "retry_count": 0}
    page._comment_list_http_meta = meta
    if parent_comment_id is not None:
        page._reply_list_http_meta = meta

    def unavailable(reason):
        meta["reason"] = reason
        logger.warning(
            "[COMMENT_LIST_HTTP] video=%s cursor=%s status=%s api_status=%s body_len=%s reason=%s",
            aweme_id, cursor, meta["http_status"], meta.get("api_status"), meta["body_len"], reason,
        )
        return None

    try:
        runtime, cookies = await _read_request_metadata(page)
        profile = RequestProfile.reply_list_profile() if parent_comment_id is not None else RequestProfile.comment_list_profile()
        query = ({"item_id": aweme_id, "comment_id": parent_comment_id} if parent_comment_id is not None
                 else {"aweme_id": aweme_id})
        for attempt in range(_COMMENT_LIST_MAX_RETRIES + 1):
            meta["attempts"] = attempt + 1
            meta["retry_count"] = attempt
            if attempt:
                cookies = normalize_cookie_records(await page.context.cookies())
                batch = _READ_BATCH.get()
                if batch is not None and batch["page"] is page:
                    batch["snapshot"] = (runtime, cookies)
            # Each fetch receives a clean URL; the page interceptor signs it.
            params = profile.build_query_params(
                {**query, "cursor": cursor, "count": count}, cookies, runtime,
                include_cookie_signatures=False,
                include_ms_token=True,
            )
            url = profile.build_url_from_params(params)
            script = _COMMENT_LIST_RETRY_FETCH_JS if attempt else _COMMENT_LIST_FETCH_JS
            if fresh:
                script = script.replace('credentials: "include",', 'credentials: "include", cache: "no-store",')
            with observe_request_security(page, url) as evidence:
                response = await asyncio.wait_for(page.evaluate(script, url, isolated_context=False), timeout=6.0)
            meta["http_status"] = response.get("status")
            meta["body_len"] = len(response.get("text") or "")
            security = resolve_security_evidence(evidence, response.get("url"))
            meta.update(security)
            meta["security_params"] = {key: security["security_params"][key] for key in ("X-Bogus", "X-Gnarly")}
            if attempt < _COMMENT_LIST_MAX_RETRIES and meta["http_status"] == 200 and (
                not meta["body_len"] or not all(meta["security_params"].values())
            ):
                reason = "empty_body" if not meta["body_len"] else "unsigned_response"
                meta["initial_retry_reason"] = reason
                logger.info(
                    "[COMMENT_LIST_HTTP] video=%s cursor=%s initialization_retry=%s/%s reason=%s delay=%.1fs security_params=%s",
                    aweme_id, cursor, attempt + 1, _COMMENT_LIST_MAX_RETRIES, reason,
                    _COMMENT_LIST_RETRY_DELAY_SECONDS, meta["security_params"],
                )
                await asyncio.sleep(_COMMENT_LIST_RETRY_DELAY_SECONDS)
                continue
            break
        if response.get("status") != 200:
            return unavailable("http_status")
        if not meta["body_len"]:
            return unavailable("empty_body")
        if not all(meta["security_params"].values()):
            return unavailable("unsigned_response")
        if fresh:
            actual = urlparse(str(response.get("url") or ""))
            expected = urlparse(url)
            if actual.scheme != "https" or actual.hostname != expected.hostname or actual.path != expected.path:
                return unavailable("unexpected_response_url")
        try:
            data = json.loads(response["text"])
        except (ValueError, TypeError):
            return unavailable("invalid_json")
        if fresh and (not isinstance(data, dict) or type(data.get("status_code")) is not int):
            return unavailable("missing_api_status")
        if not isinstance(data, dict) or data.get("status_code", 0) != 0:
            meta["api_status"] = data.get("status_code") if isinstance(data, dict) else None
            return unavailable("api_status" if isinstance(data, dict) else "invalid_payload")
        payload = data.get("data") if isinstance(data.get("data"), dict) else data
        if fresh and "status_code" in payload and (type(payload["status_code"]) is not int or payload["status_code"] != 0):
            return unavailable("api_status")
        explicit_empty = (type(payload.get("total")) is int and payload["total"] == 0
                          and type(payload.get("has_more")) in (bool, int, str)
                          and payload.get("has_more") in (False, 0, "0"))
        if fresh and "comments" in payload and payload["comments"] is None and explicit_empty:
            payload["comments"] = []
        if not fresh and "comments" in payload and payload["comments"] is None:
            payload["comments"] = []
        if not isinstance(payload.get("comments"), list):
            return unavailable("missing_comments")
        meta["reason"] = "ok"
        return payload
    except asyncio.TimeoutError:
        return unavailable("timeout")
    except Exception as exc:
        return unavailable(f"request_exception:{type(exc).__name__}")


def _author_key(value) -> str:
    return str(value or "").strip().lstrip("@").casefold()


async def find_parent_comments(page, aweme_id: str, mains, *, phrase_match=False, max_pages=10):
    """Return matches indexed by candidate position and bounded search evidence.

    Valid JSON with no match is distinct from an unavailable API. A cursor loop
    or a page limit is inconclusive, rather than proof a parent does not exist.
    """
    matches = {}
    cursor = 0
    seen_cursors = set()
    scanned = 0
    available = False
    exhausted = False
    for _ in range(min(20, max(1, max_pages))):
        if str(cursor) in seen_cursors:
            break
        seen_cursors.add(str(cursor))
        payload = None
        if str(cursor) == "0" and page is not None:
            cached_first = getattr(page, "_cached_first_comment_page", None)
            cached_aweme = getattr(page, "_cached_first_comment_aweme_id", None)
            if isinstance(cached_first, tuple) and len(cached_first) == 2:
                cached_aweme, cached_first = cached_first
            if (
                isinstance(cached_first, dict)
                and isinstance(cached_first.get("comments"), list)
                and (not cached_aweme or str(cached_aweme) == str(aweme_id))
            ):
                payload = cached_first
                page._cached_first_comment_page = None
        if payload is None:
            payload = await fetch_comment_page(page, aweme_id, cursor)
        if payload is None:
            meta = getattr(page, "_comment_list_http_meta", {})
            return matches, {"available": False, "scanned": scanned, "exhausted": False,
                             "reason": meta.get("reason", "unavailable") if isinstance(meta, dict) else "unavailable"}
        available = True
        for row in payload["comments"]:
            if not isinstance(row, dict):
                continue
            scanned += 1
            cid = str(row.get("cid") or "").strip()
            if not cid.isascii() or not cid.isdigit() or not 10 <= len(cid) <= 30:
                continue
            if cid in {hit["cid"] for hit in matches.values()}:
                continue
            # Only top-level parents belong to this endpoint's discovery flow.
            if str(row.get("reply_id") or "0") != "0":
                continue
            text = str(row.get("text") or "")
            user = row.get("user") if isinstance(row.get("user"), dict) else {}
            authors = {_author_key(user.get("unique_id")), _author_key(user.get("nickname"))} - {""}
            for index, main in enumerate(mains):
                if index in matches:
                    continue
                wanted_author = _author_key(main.tiktok_username)
                if wanted_author and wanted_author not in authors:
                    continue
                wanted = " ".join(str(main.text or "").split()).casefold()
                actual = " ".join(text.split()).casefold()
                text_matches = contains_search_phrase(main.text, text) if phrase_match else bool(wanted and wanted == actual)
                if text_matches:
                    matches[index] = {"cid": cid, "text": text, "author": str(user.get("unique_id") or user.get("nickname") or "")}
                    break
        has_more = payload.get("has_more") in (True, 1, "1")
        if not has_more:
            exhausted = True
        if len(matches) == len(mains) or not has_more:
            break
        next_cursor = payload.get("cursor")
        if next_cursor is None or str(next_cursor) == str(cursor):
            break
        cursor = next_cursor
    return matches, {"available": available, "scanned": scanned, "exhausted": exhausted}
