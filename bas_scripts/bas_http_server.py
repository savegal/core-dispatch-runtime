"""
BAS HTTP Server — промежуточный сервер между TeletanSoft executor и браузером.

Запуск: python bas_scripts/bas_http_server.py

Слушает на 127.0.0.1:8765, принимает JSON-задачи:
  POST /post_comment  — написать комментарий
  POST /post_reply    — написать ответ
  POST /setup_account — создать профиль браузера (для аккаунтов с sessionid)
  GET  /health        — проверка что сервер жив

Два режима работы:
  1. С профилем (PROFILES_DIR/<account_id>/) — если профиль есть, использует его.
     Профиль содержит sessionid полученный при реальном логине.
  2. Без профиля — загружает куки из запроса. Для аккаунтов без sessionid работает
     ровно так же как текущий Playwright-код, но изолированно.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import sys
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

sys.path.insert(0, str(Path(__file__).parent.parent))

try:
    from aiohttp import web
except ImportError:
    print("ERROR: aiohttp not installed. Run: pip install aiohttp")
    sys.exit(1)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
)
logger = logging.getLogger("bas_http_server")

# --- Конфигурация ---
PROFILES_DIR = Path(os.environ.get(
    "BAS_PROFILES_DIR",
    r"C:\Users\daboo\AppData\Roaming\BAS\Profiles",
))
PORT = int(os.environ.get("BAS_SERVER_PORT", "8765"))
MAX_PARALLEL_BROWSERS = int(os.environ.get("BAS_MAX_BROWSERS", "3"))

# Семафор — не больше N браузеров одновременно
_browser_sem: asyncio.Semaphore


# ---------------------------------------------------------------------------
# Вспомогательные функции
# ---------------------------------------------------------------------------

def _parse_proxy(proxy_str: str) -> Optional[Dict[str, str]]:
    if not proxy_str or not proxy_str.strip():
        return None
    s = proxy_str.strip()
    if "://" not in s:
        s = "http://" + s
    return {"server": s}


def _has_session(cookies: List[Dict]) -> bool:
    """Есть ли sessionid/sid_tt в куках."""
    auth = {"sessionid", "sessionid_ss", "sid_tt"}
    return any(c.get("name") in auth for c in cookies)


def _profile_path(account_id: int) -> Path:
    return PROFILES_DIR / str(account_id)


async def _open_context(playwright, proxy_str: str, account_id: int, cookies: List[Dict]):
    """
    Открывает браузерный контекст.
    Если есть сохранённый профиль — использует его (там будет sessionid от реального логина).
    Иначе — создаёт чистый контекст и грузит куки.
    """
    launch_args = [
        "--disable-blink-features=AutomationControlled",
        "--no-sandbox",
        "--disable-dev-shm-usage",
        "--disable-extensions",
    ]

    proxy = _parse_proxy(proxy_str)
    profile = _profile_path(account_id)

    if profile.exists():
        logger.info("[CTX] account=%d using saved profile: %s", account_id, profile)
        context = await playwright.chromium.launch_persistent_context(
            user_data_dir=str(profile),
            headless=False,
            args=["--headless=new"] + launch_args,
            proxy=proxy,
            viewport={"width": 1280, "height": 720},
        )
        page = await context.new_page()
        return context, page, "profile"
    else:
        logger.info("[CTX] account=%d no profile, loading cookies directly", account_id)
        browser = await playwright.chromium.launch(
            headless=False,
            args=["--headless=new"] + launch_args,
        )
        context = await browser.new_context(
            proxy=proxy,
            viewport={"width": 1280, "height": 720},
            user_agent=(
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/124.0.0.0 Safari/537.36"
            ),
        )
        if cookies:
            try:
                await context.add_cookies(cookies)
            except Exception as e:
                logger.warning("[CTX] account=%d cookie load error: %s", account_id, e)
        page = await context.new_page()
        return context, page, "cookies"


async def _close_context(context, mode: str):
    try:
        if mode == "profile":
            await context.close()
        else:
            browser = context.browser
            await context.close()
            if browser:
                await browser.close()
    except Exception:
        pass


# ---------------------------------------------------------------------------
# Обработчики
# ---------------------------------------------------------------------------

async def handle_health(request: web.Request) -> web.Response:
    profile_count = len(list(PROFILES_DIR.glob("*"))) if PROFILES_DIR.exists() else 0
    return web.json_response({
        "status": "ok",
        "port": PORT,
        "profiles_dir": str(PROFILES_DIR),
        "profiles_count": profile_count,
        "max_parallel_browsers": MAX_PARALLEL_BROWSERS,
    })


async def handle_post_comment(request: web.Request) -> web.Response:
    try:
        data: Dict[str, Any] = await request.json()
    except Exception:
        return web.json_response({"status": "error", "error": "invalid_json"}, status=400)

    account_id = data.get("account_id")
    video_url = data.get("video_url", "").strip()
    comment_text = data.get("comment_text", "").strip()
    proxy = data.get("proxy", "")
    cookies_json = data.get("cookies_json", "[]")
    correlation_id = data.get("correlation_id", f"acc_{account_id}")

    if not all([account_id, video_url, comment_text]):
        return web.json_response({"status": "error", "error": "missing_fields"}, status=400)

    try:
        cookies: List[Dict] = json.loads(cookies_json) if isinstance(cookies_json, str) else cookies_json
    except Exception:
        cookies = []

    logger.info(
        "[REQ] post_comment account=%s text='%s' has_session=%s tag=%s",
        account_id, comment_text[:30], _has_session(cookies), correlation_id,
    )

    async with _browser_sem:
        result = await _do_post_comment(
            account_id=int(account_id),
            video_url=video_url,
            comment_text=comment_text,
            proxy=proxy,
            cookies=cookies,
            correlation_id=correlation_id,
        )

    return web.json_response(result)


async def handle_post_reply(request: web.Request) -> web.Response:
    try:
        data: Dict[str, Any] = await request.json()
    except Exception:
        return web.json_response({"status": "error", "error": "invalid_json"}, status=400)

    account_id = data.get("account_id")
    video_url = data.get("video_url", "").strip()
    reply_text = data.get("reply_text", "").strip()
    parent_comment_text = data.get("parent_comment_text", "")
    parent_comment_id = data.get("parent_comment_id", "")
    proxy = data.get("proxy", "")
    cookies_json = data.get("cookies_json", "[]")
    correlation_id = data.get("correlation_id", f"acc_{account_id}")

    if not all([account_id, video_url, reply_text]):
        return web.json_response({"status": "error", "error": "missing_fields"}, status=400)

    try:
        cookies: List[Dict] = json.loads(cookies_json) if isinstance(cookies_json, str) else cookies_json
    except Exception:
        cookies = []

    logger.info(
        "[REQ] post_reply account=%s parent_id=%s parent_text='%s' reply='%s' tag=%s",
        account_id, parent_comment_id or "none", (parent_comment_text or "")[:30], reply_text[:30], correlation_id,
    )

    async with _browser_sem:
        result = await _do_post_reply(
            account_id=int(account_id),
            video_url=video_url,
            reply_text=reply_text,
            parent_comment_text=parent_comment_text or None,
            parent_comment_id=parent_comment_id or None,
            proxy=proxy,
            cookies=cookies,
            correlation_id=correlation_id,
        )

    return web.json_response(result)


async def handle_setup_account(request: web.Request) -> web.Response:
    """
    Создаёт/обновляет профиль браузера для аккаунта.
    Открывает TikTok с куками — если аккаунт живой, браузер получит sessionid.
    Профиль сохраняется в PROFILES_DIR/<account_id>/.
    """
    try:
        data: Dict[str, Any] = await request.json()
    except Exception:
        return web.json_response({"status": "error", "error": "invalid_json"}, status=400)

    account_id = data.get("account_id")
    cookies_json = data.get("cookies_json", "[]")
    proxy = data.get("proxy", "")

    if not account_id:
        return web.json_response({"status": "error", "error": "missing account_id"}, status=400)

    try:
        cookies: List[Dict] = json.loads(cookies_json) if isinstance(cookies_json, str) else cookies_json
    except Exception:
        cookies = []

    logger.info("[REQ] setup_account account=%s proxy=%s cookies=%d", account_id, bool(proxy), len(cookies))

    async with _browser_sem:
        result = await _do_setup_account(int(account_id), cookies, proxy)

    return web.json_response(result)


# ---------------------------------------------------------------------------
# Логика браузера
# ---------------------------------------------------------------------------

async def _do_post_comment(
    account_id: int,
    video_url: str,
    comment_text: str,
    proxy: str,
    cookies: List[Dict],
    correlation_id: str,
) -> Dict[str, Any]:
    started = time.monotonic()

    try:
        from patchright.async_api import async_playwright
    except ImportError:
        return {"status": "error", "error": "patchright_not_installed", "elapsed": 0}

    try:
        async with async_playwright() as p:
            context, page, mode = await _open_context(p, proxy, account_id, cookies)
            try:
                from executor.session_bootstrap import bootstrap_session
                from executor.publish_action import publish_comment_hybrid

                boot = await bootstrap_session(
                    page,
                    video_url,
                    max_total_seconds=25.0,
                    correlation_id=correlation_id,
                )

                if boot.status == "dead_session":
                    return {
                        "status": "redirected",
                        "error": f"dead_session: {boot.dead_reason}",
                        "elapsed": time.monotonic() - started,
                    }

                if boot.status == "navigation_failed":
                    return {
                        "status": "failed",
                        "error": f"navigation: {boot.dead_reason}",
                        "elapsed": time.monotonic() - started,
                    }

                result = await publish_comment_hybrid(
                    page,
                    comment_text,
                    max_total_seconds=35.0,
                    correlation_id=correlation_id,
                )

                elapsed = time.monotonic() - started

                if result.status == "success":
                    logger.info(
                        "[BAS] comment OK account=%d comment_id=%s elapsed=%.1fs mode=%s",
                        account_id, result.comment_id, elapsed, mode,
                    )
                    return {
                        "status": "success",
                        "comment_id": result.comment_id or f"{account_id}-{int(time.time())}",
                        "elapsed": elapsed,
                        "mode": mode,
                    }

                if result.status == "redirected":
                    return {"status": "redirected", "error": result.error_reason, "elapsed": elapsed}

                logger.warning(
                    "[BAS] comment FAIL account=%d reason=%s elapsed=%.1fs",
                    account_id, result.error_reason, elapsed,
                )
                return {"status": "failed", "error": result.error_reason or "unknown", "elapsed": elapsed}

            finally:
                try:
                    await page.close()
                except Exception:
                    pass
                await _close_context(context, mode)

    except Exception as exc:
        elapsed = time.monotonic() - started
        logger.exception("[BAS] post_comment exception account=%d: %s", account_id, exc)
        return {"status": "error", "error": f"exception: {str(exc)[:150]}", "elapsed": elapsed}


async def _do_post_reply(
    account_id: int,
    video_url: str,
    reply_text: str,
    parent_comment_text: Optional[str],
    proxy: str,
    cookies: List[Dict],
    correlation_id: str,
    parent_comment_id: Optional[str] = None,
) -> Dict[str, Any]:
    started = time.monotonic()

    try:
        from patchright.async_api import async_playwright
    except ImportError:
        return {"status": "error", "error": "patchright_not_installed", "elapsed": 0}

    try:
        async with async_playwright() as p:
            context, page, mode = await _open_context(p, proxy, account_id, cookies)
            try:
                from executor.session_bootstrap import bootstrap_session
                from executor.publish_action import publish_reply_hybrid

                boot = await bootstrap_session(
                    page,
                    video_url,
                    max_total_seconds=25.0,
                    correlation_id=correlation_id,
                )

                if boot.status == "dead_session":
                    return {
                        "status": "redirected",
                        "error": f"dead_session: {boot.dead_reason}",
                        "elapsed": time.monotonic() - started,
                    }

                result = await publish_reply_hybrid(
                    page,
                    reply_text,
                    parent_comment_text=parent_comment_text,
                    parent_comment_id=parent_comment_id,
                    max_total_seconds=35.0,
                    correlation_id=correlation_id,
                    expected_video_url=video_url,
                )

                elapsed = time.monotonic() - started

                if result.status == "success":
                    logger.info(
                        "[BAS] reply OK account=%d comment_id=%s elapsed=%.1fs",
                        account_id, result.comment_id, elapsed,
                    )
                    return {
                        "status": "success",
                        "comment_id": result.comment_id or f"{account_id}-reply-{int(time.time())}",
                        "elapsed": elapsed,
                        "mode": mode,
                    }

                if result.status == "redirected":
                    return {"status": "redirected", "error": result.error_reason, "elapsed": elapsed}

                return {"status": "failed", "error": result.error_reason or "unknown", "elapsed": elapsed}

            finally:
                try:
                    await page.close()
                except Exception:
                    pass
                await _close_context(context, mode)

    except Exception as exc:
        elapsed = time.monotonic() - started
        logger.exception("[BAS] post_reply exception account=%d: %s", account_id, exc)
        return {"status": "error", "error": f"exception: {str(exc)[:150]}", "elapsed": elapsed}


async def _do_setup_account(
    account_id: int,
    cookies: List[Dict],
    proxy: str,
) -> Dict[str, Any]:
    """
    Создаёт persistentContext для аккаунта.
    После открытия TikTok браузер может получить sessionid от сервера.
    """
    profile = _profile_path(account_id)
    profile.mkdir(parents=True, exist_ok=True)

    try:
        from patchright.async_api import async_playwright
    except ImportError:
        return {"status": "error", "error": "patchright_not_installed"}

    try:
        async with async_playwright() as p:
            prx = _parse_proxy(proxy)
            context = await p.chromium.launch_persistent_context(
                user_data_dir=str(profile),
                headless=False,
                args=[
                    "--headless=new",
                    "--disable-blink-features=AutomationControlled",
                    "--no-sandbox",
                ],
                proxy=prx,
                viewport={"width": 1280, "height": 720},
            )
            page = await context.new_page()

            # Заливаем куки
            if cookies:
                try:
                    await context.add_cookies(cookies)
                except Exception as e:
                    logger.warning("[SETUP] account=%d cookie error: %s", account_id, e)

            # Открываем TikTok — браузер сохранит sessionid если аккаунт живой
            try:
                await page.goto(
                    "https://www.tiktok.com/foryou",
                    wait_until="domcontentloaded",
                    timeout=20000,
                )
                await asyncio.sleep(4.0)
            except Exception as e:
                logger.warning("[SETUP] account=%d navigation error: %s", account_id, e)

            # Читаем что получилось
            all_cookies = await context.cookies()
            has_session = any(c.get("name") in {"sessionid", "sessionid_ss", "sid_tt"} for c in all_cookies)
            current_url = page.url or ""

            logger.info(
                "[SETUP] account=%d profile saved | cookies=%d has_session=%s url=%s",
                account_id, len(all_cookies), has_session, current_url[:60],
            )

            await page.close()
            await context.close()

            return {
                "status": "success",
                "account_id": account_id,
                "profile_path": str(profile),
                "cookies_count": len(all_cookies),
                "has_session": has_session,
                "current_url": current_url[:100],
            }

    except Exception as exc:
        logger.exception("[SETUP] account=%d exception: %s", account_id, exc)
        return {"status": "error", "error": str(exc)[:150], "account_id": account_id}


# ---------------------------------------------------------------------------
# Запуск
# ---------------------------------------------------------------------------

async def on_startup(app: web.Application):
    global _browser_sem
    _browser_sem = asyncio.Semaphore(MAX_PARALLEL_BROWSERS)
    logger.info("BAS HTTP server started on port %s", PORT)
    logger.info("Profiles dir: %s (exists=%s)", PROFILES_DIR, PROFILES_DIR.exists())
    logger.info("Max parallel browsers: %s", MAX_PARALLEL_BROWSERS)


def make_app() -> web.Application:
    app = web.Application()
    app.on_startup.append(on_startup)
    app.router.add_get("/health", handle_health)
    app.router.add_post("/post_comment", handle_post_comment)
    app.router.add_post("/post_reply", handle_post_reply)
    app.router.add_post("/setup_account", handle_setup_account)
    return app


if __name__ == "__main__":
    app = make_app()
    web.run_app(app, host="127.0.0.1", port=PORT, access_log=None)
