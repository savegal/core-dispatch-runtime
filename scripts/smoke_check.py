"""Operational smoke-check for the in-house team.

This script does not touch TikTok or create real tasks.
It verifies that the critical local runtime wiring is alive:
- app import
- critical runtime imports (executor, monitor, TikTok client)
- health endpoint
- admin health summary
- monitoring summary
- presence of key routes/buttons

Usage:
    python scripts/smoke_check.py
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


async def _call_json(path: str, headers: dict[str, str] | None = None) -> tuple[bool, dict | str]:
    try:
        import httpx
        from main import app

        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://local") as client:
            response = await client.get(path, headers=headers)
            if response.status_code != 200:
                return False, f"HTTP {response.status_code}: {response.text[:200]}"
            return True, response.json()
    except Exception as exc:
        return False, str(exc)


def _route_exists(path: str) -> bool:
    from main import app

    return any(route.path == path for route in app.routes)


def _admin_button_exists() -> bool:
    from bot.keyboards.admin import get_admin_menu_keyboard

    keyboard = get_admin_menu_keyboard()
    return any(btn.callback_data == "admin:monitoring" for row in keyboard.inline_keyboard for btn in row)


def _accounts_health_button_exists() -> bool:
    from bot.keyboards.admin import get_accounts_menu_keyboard

    keyboard = get_accounts_menu_keyboard()
    return any(btn.callback_data == "admin:accounts:health" for row in keyboard.inline_keyboard for btn in row)


async def main() -> int:
    print("TeletanSoft smoke-check")
    print(f"Project: {PROJECT_ROOT}")
    print("")

    failures = 0

    try:
        import executor.engagement_engine  # noqa: F401
        import executor.tiktok_client_async  # noqa: F401
        import services.video_monitor_worker  # noqa: F401
        import services.account_service  # noqa: F401

        _print_result("runtime_imports", True, "executor/monitor stack imports")
    except Exception as exc:
        _print_result("runtime_imports", False, str(exc))
        return 1

    from services.auth_service import resolve_admin_api_token

    admin_headers = {"X-Admin-Token": resolve_admin_api_token() or ""}

    health_ok, health_data = await _call_json("/health")
    _print_result("health", health_ok, str(health_data)[:180] if not health_ok else f"status={health_data.get('status', 'unknown')}")
    if not health_ok:
        failures += 1

    summary_ok, summary_data = await _call_json("/api/stats/health/summary", headers=admin_headers)
    if summary_ok and isinstance(summary_data, dict):
        queue_len = ((summary_data.get("queue") or {}).get("length", 0))
        _print_result("health_summary", True, f"queue={queue_len}")
    else:
        _print_result("health_summary", False, str(summary_data))
        failures += 1

    monitor_ok, monitor_data = await _call_json("/api/video-parsing/monitoring/summary", headers=admin_headers)
    if monitor_ok and isinstance(monitor_data, dict):
        system = monitor_data.get("system", {})
        accounts = monitor_data.get("accounts", {})
        tasks = monitor_data.get("tasks", {})
        _print_result(
            "monitoring_summary",
            True,
            f"queue={system.get('queue_len', 0)}, eligible_accounts={accounts.get('eligible_now', 0)}, stuck_tasks={tasks.get('stuck_in_progress', 0)}",
        )
    else:
        _print_result("monitoring_summary", False, str(monitor_data))
        failures += 1

    runtime_ok, runtime_data = await _call_json("/api/accounts/runtime_health", headers=admin_headers)
    if runtime_ok and isinstance(runtime_data, dict):
        main_pool = runtime_data.get("main_comment", {})
        reply_pool = runtime_data.get("reply", {})
        _print_result(
            "account_runtime_health",
            True,
            f"main_ready={main_pool.get('eligible_now', 0)}, reply_ready={reply_pool.get('eligible_now', 0)}",
        )
    else:
        _print_result("account_runtime_health", False, str(runtime_data))
        failures += 1

    routes_ok = (
        _route_exists("/api/video-parsing/monitoring/summary")
        and _route_exists("/api/stats/health/summary")
        and _route_exists("/api/accounts/runtime_health")
    )
    _print_result("critical_routes", routes_ok, "monitoring+health+account runtime routes")
    if not routes_ok:
        failures += 1

    button_ok = _admin_button_exists()
    _print_result("admin_monitor_button", button_ok, "admin:monitoring callback")
    if not button_ok:
        failures += 1

    accounts_button_ok = _accounts_health_button_exists()
    _print_result("accounts_runtime_button", accounts_button_ok, "admin:accounts:health callback")
    if not accounts_button_ok:
        failures += 1

    print("")
    if failures:
        print("Result: smoke-check found problems in the operational contour.")
        return 1

    print("Result: operational contour is wired correctly.")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
