"""Lightweight preflight check for the in-house team.

Checks the pieces that most often block real work:
- app import
- /health
- database
- Redis
- executor heartbeat
- video monitor heartbeat
- Playwright launch

Usage:
    python scripts/preflight_check.py
"""

from __future__ import annotations

import asyncio
import logging
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="backslashreplace")
logging.basicConfig(level=logging.CRITICAL)


def _print_result(name: str, ok: bool, details: str = "") -> None:
    marker = "OK" if ok else "FAIL"
    line = f"[{marker}] {name}"
    if details:
        line += f" - {details}"
    print(line)


async def _check_health() -> tuple[bool, dict | str]:
    try:
        import httpx
        from main import app

        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://local") as client:
            response = await client.get("/health")
            return response.status_code < 500 or response.status_code == 503, response.json()
    except Exception as exc:
        return False, str(exc)


async def _check_monitoring_summary() -> tuple[bool, dict | str]:
    try:
        import httpx
        from main import app
        from services.auth_service import resolve_admin_api_token

        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://local") as client:
            response = await client.get(
                "/api/video-parsing/monitoring/summary",
                headers={"X-Admin-Token": resolve_admin_api_token() or ""},
            )
            return response.status_code == 200, response.json()
    except Exception as exc:
        return False, str(exc)


async def _check_account_runtime_health() -> tuple[bool, dict | str]:
    try:
        import httpx
        from main import app
        from services.auth_service import resolve_admin_api_token

        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://local") as client:
            response = await client.get(
                "/api/accounts/runtime_health",
                headers={"X-Admin-Token": resolve_admin_api_token() or ""},
            )
            return response.status_code == 200, response.json()
    except Exception as exc:
        return False, str(exc)


async def _check_playwright() -> tuple[bool, str]:
    try:
        from patchright.async_api import async_playwright

        async with async_playwright() as p:
            browser = await p.chromium.launch(headless=True, timeout=15000)
            page = await browser.new_page()
            await page.goto("about:blank")
            await browser.close()
        return True, "Chromium launched"
    except Exception as exc:
        return False, str(exc)


def _version_string() -> str:
    try:
        import fastapi
        import httpx
        import starlette

        return (
            f"fastapi={fastapi.__version__}, "
            f"starlette={starlette.__version__}, "
            f"httpx={httpx.__version__}"
        )
    except Exception as exc:
        return f"version check failed: {exc}"


async def main() -> int:
    print("TeletanSoft preflight")
    print(f"Project: {PROJECT_ROOT}")
    print(_version_string())
    print("")

    health_ok, health_data = await _check_health()
    if not health_ok:
        _print_result("health", False, str(health_data))
        return 1

    if not isinstance(health_data, dict):
        _print_result("health", False, "unexpected response")
        return 1

    _print_result("health", True, f"status={health_data.get('status', 'unknown')}")

    checks = health_data.get("checks", {})
    blockers = 0

    for key in ("database", "redis", "executor", "video_monitor"):
        item = checks.get(key) or {}
        status = item.get("status", "missing")
        ok = status == "ok"
        details = item.get("error") or item.get("info") or ""
        _print_result(key, ok, str(details))
        if not ok and key in {"database", "redis"}:
            blockers += 1

    playwright_ok, playwright_details = await _check_playwright()
    _print_result("playwright", playwright_ok, playwright_details)
    if not playwright_ok:
        blockers += 1

    monitoring_ok, monitoring_data = await _check_monitoring_summary()
    if monitoring_ok and isinstance(monitoring_data, dict):
        system = monitoring_data.get("system", {})
        accounts = monitoring_data.get("accounts", {})
        tasks = monitoring_data.get("tasks", {})
        monitor_details = (
            f"queue={system.get('queue_len', 0)}, "
            f"eligible_accounts={accounts.get('eligible_now', 0)}, "
            f"stuck_tasks={tasks.get('stuck_in_progress', 0)}"
        )
        _print_result("monitoring_summary", True, monitor_details)
    else:
        _print_result("monitoring_summary", False, str(monitoring_data))

    runtime_ok, runtime_data = await _check_account_runtime_health()
    if runtime_ok and isinstance(runtime_data, dict):
        main_pool = runtime_data.get("main_comment", {})
        reply_pool = runtime_data.get("reply", {})
        runtime_details = (
            f"main_ready={main_pool.get('eligible_now', 0)}, "
            f"reply_ready={reply_pool.get('eligible_now', 0)}"
        )
        _print_result("account_runtime_health", True, runtime_details)
    else:
        _print_result("account_runtime_health", False, str(runtime_data))

    print("")
    if blockers:
        print("Result: system is not ready for full work.")
        return 1

    print("Result: core dependencies are ready. If executor/video monitor are not running, start them separately.")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
