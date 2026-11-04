"""Read IDs through the live page, reusing a native first-page activation GET."""
from __future__ import annotations

import asyncio
import json
import logging
from executor.browser_reads import await_browser_read
from contextlib import asynccontextmanager
from contextvars import ContextVar
from urllib.parse import urlparse

from services.comment_search import contains_search_phrase
from services.cookie_validator import normalize_cookie_records
from executor.http.request_profile import RequestProfile
from executor.http.playwright_http_client import _extract_runtime_profile
from executor.http.page_runtime import activate_page_input, prepared_runtime_profile
from executor.http.request_evidence import (
    has_request_signature, observe_request_security, resolve_security_evidence, same_http_endpoint,
)


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


async def _read_request_metadata(page, runtime_profile=None):
    batch = _READ_BATCH.get()
    runtime = prepared_runtime_profile(page, runtime_profile)
    if runtime is None:
        runtime = batch["snapshot"] if batch is not None and batch["page"] is page else None
    if runtime is None:
        runtime = await await_browser_read(lambda: _extract_runtime_profile(page), timeout=4.0)
    cookies = normalize_cookie_records(await page.context.cookies())
    if batch is not None and batch["page"] is page:
        batch["snapshot"] = runtime
    return runtime, cookies


_COMMENT_LIST_FETCH_JS = """
async url => {
    let activated = false;
    let initializationRequest = false;
    const allowActivation = true;
    const allowDirectFetch = true;
    try {
        const key = location.pathname;
        const request = new URL(url, location.href);
        initializationRequest = request.searchParams.get('cursor') === '0';
        const button = document.querySelector('[data-e2e="comment-icon"], [data-e2e="comments"]');
        if (allowActivation && request.pathname === '/api/comment/list/' &&
            request.searchParams.get('cursor') === '0' && window.__teletanCommentActivation !== key && button) {
            if (button.getAttribute('aria-expanded') !== 'true') {button.click(); activated = true;}
            window.__teletanCommentActivation = key;
        }
    } catch (error) {}
    if (initializationRequest && (allowDirectFetch || activated)) {
        try { window.scrollBy(0, 120); } catch (error) {}
        await new Promise(resolve => setTimeout(resolve, 250));
    }
    if (activated) return {activation_only: true};
    if (!allowDirectFetch) return {activation_only: false};
    const controller = new AbortController();
    const timer = setTimeout(() => controller.abort(), 5000);
    try {
        const response = await window.fetch(url, {credentials: "include", redirect: "error", signal: controller.signal});
        return {status: response.status, text: await response.text(), url: response.url,
                headers: Object.fromEntries(response.headers.entries())};
    } finally { clearTimeout(timer); }
}
"""

_COMMENT_LIST_RETRY_FETCH_JS = _COMMENT_LIST_FETCH_JS.replace('const allowActivation = true;', 'const allowActivation = false;')
_COMMENT_LIST_MAX_RETRIES = 2
_COMMENT_LIST_RETRY_DELAY_SECONDS = 2.0
_COMMENT_LIST_NATIVE_TIMEOUT_SECONDS = 5.0


async def _fetch_with_native_activation(page, script, url, meta, *, native_only=False):
    """Reuse an activation GET for discovery; fresh reads still fetch independently."""
    expected = urlparse(url)
    from urllib.parse import parse_qs
    query = parse_qs(expected.query)
    if query.get("cursor") == ["0"]:
        await activate_page_input(page)
    try:
        requested_count = int((query.get("count") or ["20"])[0])
    except (TypeError, ValueError):
        requested_count = 20
    needs_native_bootstrap = native_only or getattr(page, "_warmup_signer_ready", None) is False
    already_activated = getattr(page, "_cached_first_comment_page", None) is not None
    first_page = (expected.path == "/api/comment/list/" and query.get("cursor") == ["0"]
                  and 'const allowActivation = true;' in script
                  and (needs_native_bootstrap or (requested_count <= 20 and not already_activated)))

    async def direct_fetch():
        if native_only:
            return {"status": None, "text": "", "unavailable_reason": "native_comment_request_unavailable"}
        # A native UI request can bootstrap the SDK. A manual read must not
        # bypass an explicitly failed readiness check while that happens.
        if getattr(page, "_warmup_signer_ready", None) is False:
            from executor.http.signature_runtime import wait_for_runtime_signer_ready
            ready = await wait_for_runtime_signer_ready(page, timeout_ms=1500)
            meta["runtime_readiness_rechecked"] = ready
            if not ready:
                return {"status": None, "text": "", "unavailable_reason": "signer_not_ready"}
        fetch_script = script.replace('const allowActivation = true;', 'const allowActivation = false;')
        return await await_browser_read(lambda: page.evaluate(fetch_script, url, isolated_context=False), timeout=6.0)

    if not first_page or not callable(getattr(page, "on", None)) or not callable(getattr(page, "remove_listener", None)):
        return await direct_fetch()
    result = asyncio.get_running_loop().create_future()
    tasks = set()
    dispatched = False
    dispatch_event = asyncio.Event()

    def matches(raw_url):
        parsed = urlparse(str(raw_url or ""))
        values = parse_qs(parsed.query)
        return (same_http_endpoint(raw_url, url)
                and values.get("aweme_id") == query.get("aweme_id") and values.get("cursor") == ["0"])

    def on_request(request):
        nonlocal dispatched
        if str(getattr(request, "method", "")).upper() == "GET" and matches(getattr(request, "url", "")):
            dispatched = True
            dispatch_event.set()

    async def read_response(response):
        try:
            text = await await_browser_read(response.text, timeout=4.5)
            if not result.done():
                result.set_result({"status": response.status, "text": text, "url": response.url,
                                   "headers": response.headers})
        except Exception:
            pass

    def on_response(response):
        if matches(getattr(response, "url", "")) and not result.done():
            task = asyncio.create_task(read_response(response))
            tasks.add(task)
            task.add_done_callback(tasks.discard)

    page.on("request", on_request)
    page.on("response", on_response)
    try:
        activate_script = script.replace('const allowDirectFetch = true;', 'const allowDirectFetch = false;')
        response = await await_browser_read(lambda: page.evaluate(activate_script, url, isolated_context=False), timeout=6.0)
        if isinstance(response, dict) and response.get("activation_only"):
            deadline = asyncio.get_running_loop().time() + _COMMENT_LIST_NATIVE_TIMEOUT_SECONDS
            try:
                if not dispatched:
                    await asyncio.wait_for(dispatch_event.wait(), timeout=_COMMENT_LIST_NATIVE_TIMEOUT_SECONDS)
                remaining = max(0.001, deadline - asyncio.get_running_loop().time())
                response = await asyncio.wait_for(asyncio.shield(result), timeout=remaining)
                if 'cache: "no-store"' not in script:
                    meta["activation_response_reused"] = True
                    return response
            except asyncio.TimeoutError:
                if dispatched:
                    if native_only:
                        return {"status": None, "text": "", "unavailable_reason": "native_comment_response_timeout"}
                    meta["native_comment_response_timeout"] = True
                else:
                    meta["activation_dispatch_timeout"] = True
        elif isinstance(response, dict) and "status" in response:
            return response
        return await direct_fetch()
    finally:
        for event, callback in (("request", on_request), ("response", on_response)):
            try:
                page.remove_listener(event, callback)
            except Exception:
                logger.debug("Native comment activation listener cleanup failed", exc_info=True)
        for task in list(tasks):
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        if not result.done():
            result.cancel()


async def fetch_comment_page(page, aweme_id: str, cursor=0, count: int = 20, *, fresh: bool = False,
                             runtime_profile=None):
    return await _fetch_comment_list(page, aweme_id, cursor, count, fresh=fresh, runtime_profile=runtime_profile)


async def activate_comment_list_via_page(page, aweme_id: str, *, count=20, runtime_profile=None):
    """Bootstrap through the page's own GET without an unready manual fetch."""
    return await _fetch_comment_list(page, aweme_id, 0, count, fresh=False,
                                   runtime_profile=runtime_profile, native_only=True)


async def fetch_reply_page(page, aweme_id: str, parent_comment_id: str, cursor=0, count: int = 20):
    """Read a fresh reply branch using the page's initialized signer."""
    return await _fetch_comment_list(page, aweme_id, cursor, count, fresh=True, parent_comment_id=parent_comment_id)


async def _fetch_comment_list(page, aweme_id, cursor, count, *, fresh, parent_comment_id=None, runtime_profile=None,
                              native_only=False):
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
        runtime, cookies = await _read_request_metadata(page, runtime_profile)
        profile = RequestProfile.reply_list_profile() if parent_comment_id is not None else RequestProfile.comment_list_profile()
        query = ({"item_id": aweme_id, "comment_id": parent_comment_id} if parent_comment_id is not None
                 else {"aweme_id": aweme_id})
        warmup_signer_ready = getattr(page, "_warmup_signer_ready", True)
        effective_max_retries = _COMMENT_LIST_MAX_RETRIES if warmup_signer_ready is not False else 0
        for attempt in range(effective_max_retries + 1):
            meta["attempts"] = attempt + 1
            meta["retry_count"] = attempt
            if attempt:
                cookies = normalize_cookie_records(await page.context.cookies())
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
                script = script.replace('const allowActivation = true;', 'const allowActivation = false;')
            with observe_request_security(page, url) as evidence:
                response = await _fetch_with_native_activation(page, script, url, meta, native_only=native_only)
            if response.get("unavailable_reason"):
                return unavailable(response["unavailable_reason"])
            meta["http_status"] = response.get("status")
            meta["body_len"] = len(response.get("text") or "")
            security = resolve_security_evidence(evidence, response.get("url"))
            meta.update(security)
            meta["security_params"] = dict(security["security_params"])
            meta["unsigned_dispatch_observed"] = (
                security["security_params_source"] == "request_event"
                and not has_request_signature(meta["security_params"])
            )
            if not same_http_endpoint(response.get("url"), url):
                return unavailable("unexpected_response_url")
            headers = {str(key).lower(): value for key, value in (response.get("headers") or {}).items()}
            token = headers.get("x-ms-token")
            if meta["http_status"] == 200 and isinstance(token, str) and token.strip():
                try:
                    records = await page.context.cookies()
                    updates = [{**record, "value": token.strip()} for record in records
                               if record.get("name") == "msToken" and str(record.get("domain") or "").lstrip(".") in {"tiktok.com", "www.tiktok.com"}]
                    if not updates:
                        updates = [{"name": "msToken", "value": token.strip(), "domain": ".tiktok.com", "path": "/", "secure": True}]
                    await page.context.add_cookies(updates)
                    meta["ms_token_refreshed"] = True
                except Exception:
                    meta["ms_token_refreshed"] = False
            if attempt < effective_max_retries and meta["http_status"] == 200 and (
                not meta["body_len"] or not has_request_signature(meta["security_params"])
            ):
                reason = "empty_body" if not meta["body_len"] else "unsigned_response"
                meta["initial_retry_reason"] = reason
                logger.info(
                    "[COMMENT_LIST_HTTP] video=%s cursor=%s initialization_retry=%s/%s reason=%s delay=%.1fs security_params=%s",
                    aweme_id, cursor, attempt + 1, effective_max_retries, reason,
                    _COMMENT_LIST_RETRY_DELAY_SECONDS, meta["security_params"],
                )
                await asyncio.sleep(_COMMENT_LIST_RETRY_DELAY_SECONDS)
                continue
            break
        if response.get("status") != 200:
            return unavailable("http_status")
        if not meta["body_len"]:
            return unavailable("empty_body")
        if not has_request_signature(meta["security_params"]):
            return unavailable("unsigned_response")
        try:
            data = json.loads(response["text"])
        except (ValueError, TypeError):
            return unavailable("invalid_json")
        if not isinstance(data, dict):
            return unavailable("invalid_payload")
        if type(data.get("status_code")) is not int:
            return unavailable("missing_api_status")
        if data["status_code"] != 0:
            meta["api_status"] = data["status_code"]
            return unavailable("api_status")
        payload = data.get("data") if isinstance(data.get("data"), dict) else data
        if "status_code" in payload and (type(payload["status_code"]) is not int or payload["status_code"] != 0):
            return unavailable("api_status")
        explicit_empty = (type(payload.get("total")) is int and payload["total"] == 0
                          and type(payload.get("has_more")) in (bool, int, str)
                          and payload.get("has_more") in (False, 0, "0"))
        if "comments" in payload and payload["comments"] is None and explicit_empty:
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
    from executor.http.comment_scanner import _cursor, _more

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
            if row.get("aweme_id") is not None and str(row["aweme_id"]) != str(aweme_id):
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
        has_more = _more(payload.get("has_more"))
        exhausted = has_more is False
        if len(matches) == len(mains) or has_more is not True:
            break
        next_cursor = _cursor(payload.get("cursor"))
        if next_cursor is None or next_cursor <= cursor:
            break
        cursor = next_cursor
    return matches, {"available": available, "scanned": scanned, "exhausted": exhausted}
