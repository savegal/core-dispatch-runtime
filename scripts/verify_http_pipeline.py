"""Verify the production HTTP path; publication requests are always answered locally."""
import argparse
import asyncio
import json
import logging
import re
import sys
from pathlib import Path
from urllib.parse import parse_qs, urlparse

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from config.settings import get_settings
from db.database import get_sync_db_context
from db.models import Account
from executor.browser_pool import BrowserPool
from executor.engagement_engine import warmup_page_for_http
from executor.http.comment_discovery import fetch_comment_page
from executor.http.playwright_http_client import PlaywrightHttpClient
from executor.http.retry_policy import RetryPolicy
from services.cookie_validator import normalize_cookie_records, parse_cookies_to_dict


LOCAL_STATUS = 990001
LOCAL_RESPONSE = json.dumps({
    "status_code": LOCAL_STATUS, "status_msg": "LOCAL_DIAGNOSTIC_BLOCKED_NO_PUBLICATION",
})
PROBE_TEXT = "LOCAL TRANSPORT CHECK - NOT PUBLISHED"


def expected_comment_ids(task_ids):
    ids = set()
    if not task_ids:
        return ids
    logfile = Path(__file__).resolve().parents[1] / "logs" / "executor.log"
    with logfile.open(encoding="utf-8", errors="replace") as source:
        for line in source:
            if "[USER_SCHEMA_TRACE]" not in line or not any(f"task_{task_id}_" in line for task_id in task_ids):
                continue
            try:
                row = json.loads(line.split("[USER_SCHEMA_TRACE]", 1)[1])
            except ValueError:
                continue
            if row.get("stage") == "main_publish_variant_recorded" and row.get("network_confirmed"):
                cid = str(row.get("comment_id") or "")
                if re.fullmatch(r"\d{10,30}", cid):
                    ids.add(cid)
    return ids


async def verify(account_id, url, parent_id=None, check_ids=None):
    settings = get_settings()
    if settings.USE_PROXY or not settings.BLOCK_MEDIA:
        raise ValueError("This diagnostic requires USE_PROXY=False and BLOCK_MEDIA=True")
    media_id = urlparse(url).path.rstrip("/").split("/")[-1]
    if urlparse(url).hostname != "www.tiktok.com" or not media_id.isdigit():
        raise ValueError("Expected a canonical www.tiktok.com video/photo URL")
    with get_sync_db_context() as db:
        account = db.get(Account, account_id)
        if account is None:
            raise ValueError("Account not found")
        stored_cookies = account.cookies
        stored_ms_token = parse_cookies_to_dict(stored_cookies).get("msToken")
        db.expunge(account)

    pool = BrowserPool(max_browsers=1)
    blocked_posts = []
    head_responses = []
    context = None
    live_cookies = {}

    async def block_publication(route):
        request = route.request
        if urlparse(request.url).path.rstrip("/") != "/api/comment/publish":
            await route.fallback()
            return
        if request.method == "HEAD":
            # The page SDK performs a read-only CSRF handshake before POST.
            await route.fallback()
            return
        # Never continue or fetch this route, including if metadata collection fails.
        try:
            query = parse_qs(urlparse(request.url).query)
            body = parse_qs(request.post_data or "")
            headers = await request.all_headers()
            current_cookies = normalize_cookie_records(await context.cookies())
            csrf = headers.get("x-secsdk-csrf-token")
            cookie_names = {item.split("=", 1)[0].strip() for item in headers.get("cookie", "").split(";")}
            blocked_posts.append({
                "method": request.method,
                "security_params": {key: bool(query.get(key)) for key in ("msToken", "X-Bogus", "X-Gnarly", "a_bogus")},
                "ms_token_matches_stored_cookie": bool(stored_ms_token) and query.get("msToken") == [stored_ms_token],
                "body_keys": sorted(body),
                "probe_text_intact": body.get("text") == [PROBE_TEXT],
                "parent_id_intact": body.get("reply_id") == [str(parent_id)] if "reply_id" in body else None,
                "csrf_header_present": bool(csrf),
                "csrf_request_header_present": "x-secsdk-csrf-request" in headers,
                "csrf_version_header_present": "x-secsdk-csrf-version" in headers,
                "csrf_is_downgrade": csrf == "DOWNGRADE",
                "csrf_matches_context": bool(current_cookies.get("tt_csrf_token")) and csrf == current_cookies.get("tt_csrf_token"),
                "csrf_cookie_match_names": sorted(name for name in ("tt_csrf_token", "passport_csrf_token", "passport_csrf_token_default")
                                                 if csrf and csrf == current_cookies.get(name)),
                "session_cookies_sent": sorted(cookie_names & {"sessionid", "sid_tt", "sessionid_ss"}),
                "form_content_type": headers.get("content-type", "").split(";", 1)[0],
                "forwarded_to_server": False,
            })
        finally:
            await route.fulfill(status=200, content_type="application/json", body=LOCAL_RESPONSE)

    async def observe_head(response):
        if response.request.method == "HEAD" and urlparse(response.url).path.rstrip("/") == "/api/comment/publish":
            headers = await response.all_headers()
            head_responses.append({"status": response.status, "csrf_header_names": sorted(
                key for key in headers if "csrf" in key.lower()
            )})

    try:
        context, page = await pool.get_page_for_account(account)
        # BLOCK_MEDIA also blocks service workers in BrowserPool, so publication
        # fetches cannot bypass this context route through a worker.
        await context.route("**/*", block_publication)
        page.on("response", observe_head)
        live_cookies, valid, warmup = await warmup_page_for_http(
            page, url, max_retries=1, correlation_id="readonly_production_probe",
        )
        comments = await fetch_comment_page(page, media_id)
        first_page_meta = dict(getattr(page, "_comment_list_http_meta", {}))
        visible_ids = set()
        cursor = 0
        seen_cursors = set()
        scan_complete = False
        scanned_pages = 0
        payload = comments
        for _ in range(10 if check_ids else 1):
            if payload is None:
                break
            scanned_pages += 1
            visible_ids.update(str(row.get("cid")) for row in payload["comments"] if isinstance(row, dict))
            if payload.get("has_more") not in (True, 1, "1"):
                scan_complete = True
                break
            if scanned_pages >= (10 if check_ids else 1):
                break
            next_cursor = payload.get("cursor")
            if next_cursor is None or str(next_cursor) in seen_cursors or str(next_cursor) == str(cursor):
                break
            seen_cursors.add(str(cursor))
            cursor = next_cursor
            payload = await fetch_comment_page(page, media_id, cursor=cursor)
        result = {
            "account_id": account_id,
            "production_browser_pool": True,
            "use_proxy": settings.USE_PROXY,
            "headless": settings.PLAYWRIGHT_HEADLESS,
            "warmup_valid": valid,
            "warmup_reason": warmup.get("reason_code"),
            "signer_ready": warmup.get("signer_ready"),
            "comment_api_available": comments is not None,
            "comment_count": len(comments["comments"]) if comments else None,
            "parent_in_first_page": any(str(row.get("cid")) == str(parent_id) for row in comments["comments"])
                if comments is not None and parent_id else None,
            "comment_api_meta": first_page_meta,
            "visibility_check": {
                "checked_id_count": len(check_ids or []),
                "seen_checked_ids": sorted(visible_ids & (check_ids or set())),
                "unique_ids_scanned": len(visible_ids), "pages": scanned_pages,
                "api_exhausted": scan_complete,
                "last_page_meta": dict(getattr(page, "_comment_list_http_meta", {})),
                "parent_found_in_scan": str(parent_id) in visible_ids if parent_id else None,
            },
            "cookie_presence": {name: bool(live_cookies.get(name)) for name in
                                ("sessionid", "sid_tt", "sessionid_ss", "ttwid", "tt_csrf_token", "msToken")},
            "dry_run_results": [],
        }
        if valid:
            client = await PlaywrightHttpClient.from_page(page, RetryPolicy(max_retries=0))
            for mode in (["main", "reply"] if parent_id else ["main"]):
                live_cookies = normalize_cookie_records(await context.cookies())
                common = dict(aweme_id=media_id, text=PROBE_TEXT, cookies=live_cookies,
                              correlation_id=f"readonly_intercepted_{mode}", video_url=url)
                if mode == "reply":
                    response = await client.reply_comment(parent_comment_id=str(parent_id), **common)
                else:
                    response = await client.publish_main_comment(**common)
                result["dry_run_results"].append({
                    "mode": mode,
                    "success": response[0],
                    "comment_id_returned": bool(response[1]),
                    "local_block_response_received": "LOCAL_DIAGNOSTIC_BLOCKED_NO_PUBLICATION" in str(response[2]),
                    "transport": client.last_request_meta.get("transport"),
                })
        result["intercepted_posts"] = blocked_posts
        result["csrf_head_responses"] = head_responses
        with get_sync_db_context() as db:
            result["database_cookies_unchanged"] = db.get(Account, account_id).cookies == stored_cookies
        return result
    finally:
        await pool.close_all()


async def main(args):
    check_ids = expected_comment_ids(args.check_task)
    for account_id in args.account_id:
        try:
            print(json.dumps(await verify(account_id, args.url, args.parent_id, check_ids), ensure_ascii=False), flush=True)
        except Exception as error:
            print(json.dumps({"account_id": account_id, "error_type": type(error).__name__}), flush=True)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--account-id", type=int, action="append", required=True)
    parser.add_argument("--url", required=True)
    parser.add_argument("--parent-id")
    parser.add_argument("--check-task", type=int, action="append", default=[])
    args = parser.parse_args()
    logging.disable(logging.CRITICAL)
    asyncio.run(main(args))
