"""Read-only comment-list diagnostic; credentials stay in memory, POST publish is blocked."""
import argparse
import asyncio
import json
import logging
import sys
from pathlib import Path
from urllib.parse import urlencode

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from db.database import get_sync_db_context
from db.models import Account
from executor.http.comment_discovery import fetch_comment_page, _COMMENT_LIST_FETCH_JS
from executor.http.signature_runtime import wait_for_runtime_signer_ready
from executor.tiktok_client import parse_cookies
from patchright.async_api import async_playwright
from services.cookie_validator import get_cookies_for_account


async def probe(account_id, url, compare_minimal=False, target_parent_id=None):
    with get_sync_db_context() as db:
        account = db.get(Account, account_id)
        if account is None:
            raise ValueError("Account not found")
        records = parse_cookies(get_cookies_for_account(account))
    media_id = url.split("?")[0].rstrip("/").split("/")[-1]
    observations = []
    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch(headless=True)
        try:
            context = await browser.new_context()
            await context.add_cookies(records)
            page = await context.new_page()

            async def route_request(route):
                if "/api/comment/publish" in route.request.url or route.request.resource_type in {"image", "font", "media"}:
                    await route.abort()
                else:
                    await route.continue_()

            async def observe(response):
                if "/api/comment/list/" not in response.url:
                    return
                item = {"status": response.status}
                try:
                    body = await response.text()
                    item["body_len"] = len(body)
                    payload = json.loads(body)
                    item["api_status"] = payload.get("status_code") if isinstance(payload, dict) else None
                    item["comments"] = len(payload.get("comments") or []) if isinstance(payload, dict) else None
                except Exception as exc:
                    item["error_type"] = type(exc).__name__
                observations.append(item)

            await context.route("**/*", route_request)
            page.on("response", observe)
            await page.goto(url, wait_until="domcontentloaded", timeout=45000)
            try:
                await page.wait_for_load_state("load", timeout=8000)
            except Exception:
                pass
            ready = await wait_for_runtime_signer_ready(page, timeout_ms=8000)
            minimal_result = None
            if compare_minimal:
                minimal_url = "/api/comment/list/?" + urlencode({
                    "aid": 1988, "app_name": "tiktok_web", "device_platform": "web_pc",
                    "aweme_id": media_id, "cursor": 0, "count": 50,
                })
                try:
                    raw = await page.evaluate(_COMMENT_LIST_FETCH_JS, minimal_url, isolated_context=False)
                    minimal_result = {"status": raw["status"], "body_len": len(raw["text"])}
                    parsed = json.loads(raw["text"])
                    minimal_result["api_status"] = parsed.get("status_code")
                    minimal_result["comments"] = len(parsed.get("comments") or [])
                except Exception as exc:
                    minimal_result = {**(minimal_result or {}), "error_type": type(exc).__name__}
            payload = await fetch_comment_page(page, media_id)
            print(json.dumps({
                "account_id": account_id, "signer_ready": ready,
                "on_target": media_id in page.url and "/login" not in page.url,
                "available": payload is not None,
                "comments": len(payload["comments"]) if payload else None,
                "has_more": payload.get("has_more") if payload else None,
                "target_parent_in_page": (
                    any(str(row.get("cid")) == target_parent_id for row in payload["comments"])
                    if payload and target_parent_id else None
                ),
                "minimal_request": minimal_result,
                "diagnostic": getattr(page, "_comment_list_http_meta", {}),
                "observed_comment_responses": observations,
            }))
        finally:
            await browser.close()


if __name__ == "__main__":
    logging.basicConfig(level=logging.ERROR)
    parser = argparse.ArgumentParser()
    parser.add_argument("--account-id", type=int, required=True)
    parser.add_argument("--url", required=True)
    parser.add_argument("--compare-minimal", action="store_true")
    parser.add_argument("--target-parent-id")
    args = parser.parse_args()
    asyncio.run(probe(args.account_id, args.url, args.compare_minimal, args.target_parent_id))
