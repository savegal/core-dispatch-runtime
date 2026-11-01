"""Stable Windows desktop profiles for the installed Chromium engine."""
from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import re
from pathlib import Path
from urllib.parse import unquote

from executor.browser_identity import chromium_version, chromium_brands, desktop_user_agent, remember_running_version


logger = logging.getLogger(__name__)
RESOLUTIONS = ((1920, 1080), (1680, 1050), (1440, 900), (1600, 900), (1366, 768))
CPU_COUNTS = (4, 6, 8, 12, 16)
MEMORY_GB = (4, 8)
DESKTOP_TASKBAR_HEIGHT = 40
DESKTOP_VIEWPORT_INSET = 90
WINDOWS_GPUS = (
    ("Google Inc. (NVIDIA)", "ANGLE (NVIDIA, NVIDIA GeForce GTX 1660 SUPER Direct3D11 vs_5_0 ps_5_0, D3D11)"),
    ("Google Inc. (NVIDIA)", "ANGLE (NVIDIA, NVIDIA GeForce RTX 3060 Direct3D11 vs_5_0 ps_5_0, D3D11)"),
    ("Google Inc. (Intel)", "ANGLE (Intel, Intel(R) UHD Graphics 630 Direct3D11 vs_5_0 ps_5_0, D3D11)"),
    ("Google Inc. (AMD)", "ANGLE (AMD, AMD Radeon RX 580 Series Direct3D11 vs_5_0 ps_5_0, D3D11)"),
    ("Google Inc. (NVIDIA)", "ANGLE (NVIDIA, NVIDIA GeForce RTX 2060 Direct3D11 vs_5_0 ps_5_0, D3D11)"),
    ("Google Inc. (NVIDIA)", "ANGLE (NVIDIA, NVIDIA GeForce GTX 1060 6GB Direct3D11 vs_5_0 ps_5_0, D3D11)"),
    ("Google Inc. (Intel)", "ANGLE (Intel, Intel(R) Iris(R) Xe Graphics Direct3D11 vs_5_0 ps_5_0, D3D11)"),
    ("Google Inc. (AMD)", "ANGLE (AMD, AMD Radeon RX 6600 Direct3D11 vs_5_0 ps_5_0, D3D11)"),
)
_IDENTITY_SCRIPT = Path(__file__).with_name("device_identity.js").read_text(encoding="utf-8")

_TECHNICAL_LABEL_TOKENS = (
    "test",
    "fixture",
    "sample",
    "mock",
    "dummy",
    "temp",
    "warmup",
    "resolve",
    "shared",
    "private",
    "observer",
    "worker",
    "slot",
    "anon",
    "default",
    "unknown",
    "none",
    "null",
    "local",
    "probe",
)
_TECHNICAL_LABEL_RE = re.compile(
    r"^(?:acc(?:ount)?|user|id|bot|tiktok|tt|main|reply|profile|device|hardware)[\s_\-\d:#.]*$",
    re.IGNORECASE,
)


def device_family(user_agent: str | None) -> str:
    # Legacy account UAs are storage metadata, never an emulation instruction.
    return "windows"


def _parse_account_cookies(account) -> dict[str, str]:
    raw_cookies = getattr(account, "cookies", None)
    if not raw_cookies:
        return {}
    if isinstance(raw_cookies, dict):
        return {str(k): str(v) for k, v in raw_cookies.items() if k and v is not None}
    if isinstance(raw_cookies, str) and raw_cookies.strip():
        try:
            from services.cookie_validator import parse_cookies_to_dict

            parsed = parse_cookies_to_dict(raw_cookies)
            if isinstance(parsed, dict):
                return {str(k): str(v) for k, v in parsed.items() if k and v is not None}
        except Exception:
            pass
    return {}


def _extract_sid_guard_prefix(raw_sid_guard: str | None) -> str | None:
    if not raw_sid_guard or not str(raw_sid_guard).strip():
        return None
    decoded = unquote(str(raw_sid_guard).strip())
    token_part = decoded.split("|", 1)[0].strip()
    if len(token_part) >= 16:
        return token_part[:32].lower()
    return None


def _normalize_non_technical_label(raw_label: str | None) -> str | None:
    if not raw_label or not str(raw_label).strip():
        return None
    norm = re.sub(r"\s+", " ", str(raw_label)).strip().lower().lstrip("@")
    if len(norm) < 3 or norm.isdigit():
        return None
    if _TECHNICAL_LABEL_RE.fullmatch(norm):
        return None
    tokens = [part for part in re.split(r"[\s_\-:./#@]+", norm) if part]
    if any(tok in _TECHNICAL_LABEL_TOKENS for tok in tokens):
        return None
    return norm


def _initial_account_seed_key(account) -> str:
    cookies = _parse_account_cookies(account)

    # 1. uid_tt or cached TikTok UID
    uid_candidates = (
        cookies.get("uid_tt"),
        cookies.get("uid_tt_ss"),
        getattr(account, "_cached_tiktok_uid", None),
        getattr(account, "_http_preflight_user_id", None),
    )
    for candidate in uid_candidates:
        uid = str(candidate or "").strip()
        if uid and uid != "0":
            return f"desktop-hardware-v1:uid:{uid}"

    # 2. Stable session token (sid_tt / sessionid / sessionid_ss / 32-char prefix of sid_guard)
    for token_key in ("sid_tt", "sessionid", "sessionid_ss"):
        token_val = str(cookies.get(token_key) or "").strip()
        if len(token_val) >= 16 and _normalize_non_technical_label(token_val):
            return f"desktop-hardware-v1:session:{token_val[:32].lower()}"

    sid_guard_prefix = _extract_sid_guard_prefix(cookies.get("sid_guard"))
    if sid_guard_prefix:
        return f"desktop-hardware-v1:session:{sid_guard_prefix}"

    # 3. Normalized label (if non-technical)
    norm_label = _normalize_non_technical_label(getattr(account, "label", None))
    if norm_label:
        return f"desktop-hardware-v1:label:{norm_label}"

    # 4. Account.id fallback
    return f"desktop-hardware-v1:{int(getattr(account, 'id', None) or 0)}"


def _digest(account) -> bytes:
    return bytes.fromhex(_account_seed_key(account))


def _account_seed_key(account) -> str:
    stored = getattr(account, "device_seed", None)
    if isinstance(stored, str) and re.fullmatch(r"[0-9a-f]{64}", stored):
        return stored
    seed = hashlib.sha256(_initial_account_seed_key(account).encode("utf-8")).hexdigest()
    from sqlalchemy.orm import object_session
    from sqlalchemy import inspect, update, select, or_
    from sqlalchemy.orm.attributes import set_committed_value
    from db.models import Account
    if isinstance(account, Account):
        session = object_session(account)
        if session is not None and inspect(account).persistent:
            # Compare-and-set: another worker's first seed wins. The caller owns
            # the transaction; hardware initialization never commits other work.
            with session.no_autoflush:
                session.execute(update(Account).where(
                    Account.id == account.id,
                    or_(Account.device_seed.is_(None), Account.device_seed == ""),
                ).values(device_seed=seed).execution_options(synchronize_session=False))
                persisted_seed = session.scalar(select(Account.device_seed).where(Account.id == account.id))
                if isinstance(persisted_seed, str) and re.fullmatch(r"[0-9a-f]{64}", persisted_seed):
                    seed = persisted_seed
            set_committed_value(account, "device_seed", seed)
            return seed
    account.device_seed = seed
    return seed


def desktop_hardware(account) -> dict:
    seed = _digest(account)
    width, height = RESOLUTIONS[int.from_bytes(seed[:4], "big") % len(RESOLUTIONS)]
    gpu = WINDOWS_GPUS[int.from_bytes(seed[12:16], "big") % len(WINDOWS_GPUS)]
    return {"width": width, "height": height,
            "hardwareConcurrency": CPU_COUNTS[int.from_bytes(seed[4:8], "big") % len(CPU_COUNTS)],
            "deviceMemory": MEMORY_GB[int.from_bytes(seed[8:12], "big") % len(MEMORY_GB)],
            "webglVendor": gpu[0], "webglRenderer": gpu[1]}


def desktop_geometry(hardware, chrome_height=DESKTOP_VIEWPORT_INSET):
    width, height = hardware["width"], hardware["height"]
    inset = max(0, min(int(chrome_height), 240, height - 480))
    available = height - DESKTOP_TASKBAR_HEIGHT
    viewport = height - inset
    return {"outerWidth": width, "outerHeight": max(available, viewport),
            "availWidth": width, "availHeight": available,
            "viewportWidth": width, "viewportHeight": viewport}


def build_device_profile(account, devices: dict, browser_version: str, chrome_height: int = DESKTOP_VIEWPORT_INSET) -> dict:
    version = chromium_version(browser_version)
    ua = desktop_user_agent(version)
    seed = _digest(account)
    hardware = desktop_hardware(account)
    width, height = hardware["width"], hardware["height"]
    geometry = desktop_geometry(hardware, chrome_height)
    return {
        "options": {"user_agent": ua, "viewport": {"width": width, "height": geometry["viewportHeight"]},
                    "screen": {"width": width, "height": height}, "device_scale_factor": 1,
                    "has_touch": False, "is_mobile": False},
        "identity": {"family": "windows", "preset": None, "platform": "Win32", "vendor": "Google Inc.",
                     "mobile": False, "ios": False, "hardware": hardware, "geometry": geometry,
                     "canvasSeed": int.from_bytes(seed[16:20], "big"),
                     "audioSeed": int.from_bytes(seed[20:24], "big")},
        "override": {"userAgent": ua, "platform": "Win32", "userAgentMetadata": {
            "brands": chromium_brands(version), "fullVersionList": chromium_brands(version, full=True),
            "fullVersion": version, "platform": "Windows", "platformVersion": "10.0.0",
            "architecture": "x86", "bitness": "64", "model": "", "mobile": False,
        }},
    }


def identity_script(identity: dict, canvas_noise: bool = True) -> str:
    # Keep the old argument; protection is independent of human-like behaviour.
    return f"({_IDENTITY_SCRIPT})({json.dumps(identity)});"


async def prepare_device_page(page):
    profile = getattr(page.context, "_account_device_profile", None)
    if profile is None:
        return
    task = getattr(page, "_account_device_ready", None)
    if task is None:
        async def prepare():
            session = await page.context.new_cdp_session(page)
            page._account_device_session = session
            if callable(getattr(page, "on", None)):
                async def on_close(*_):
                    await release_device_page(page)
                page.on("close", on_close)
            override = dict(profile["override"])
            accept_language = (
                override.get("acceptLanguage")
                or profile.get("acceptLanguage")
                or getattr(page.context, "_account_accept_language", None)
                or profile.get("locale")
                or getattr(page.context, "_account_locale", None)
                or profile.get("options", {}).get("locale")
            )
            if accept_language and "," not in str(accept_language):
                try:
                    from executor.browser_pool import resolve_accept_language
                    resolved = resolve_accept_language(str(accept_language))
                    if resolved:
                        accept_language = resolved
                except Exception:
                    pass
            if accept_language:
                override["acceptLanguage"] = str(accept_language)
            await session.send("Emulation.setUserAgentOverride", override)
            concurrency_override = {
                "hardwareConcurrency": profile["identity"]["hardware"]["hardwareConcurrency"],
            }
            await session.send("Emulation.setHardwareConcurrencyOverride", concurrency_override)
            on_fn = getattr(session, "on", None)
            if callable(on_fn) and not asyncio.iscoroutinefunction(on_fn):
                msg_seq = 0

                async def _send_to_worker(target_session_id: str, method: str, params: dict | None = None):
                    nonlocal msg_seq
                    msg_seq += 1
                    await session.send("Target.sendMessageToTarget", {
                        "sessionId": target_session_id,
                        "message": json.dumps({
                            "id": msg_seq,
                            "method": method,
                            "params": params or {},
                        }),
                    })

                async def _configure_attached_target(event):
                    if not isinstance(event, dict):
                        return
                    target_session_id = event.get("sessionId")
                    if not target_session_id:
                        return
                    target_info = event.get("targetInfo") or {}
                    target_type = target_info.get("type")
                    try:
                        if target_type in {"worker", "shared_worker", "service_worker"}:
                            await _send_to_worker(target_session_id, "Emulation.setUserAgentOverride", override)
                            await _send_to_worker(
                                target_session_id,
                                "Emulation.setHardwareConcurrencyOverride",
                                concurrency_override,
                            )
                    except Exception:
                        logger.debug("Worker CDP identity setup failed", exc_info=True)
                    finally:
                        if event.get("waitingForDebugger"):
                            try:
                                await _send_to_worker(target_session_id, "Runtime.runIfWaitingForDebugger")
                            except Exception:
                                pass

                on_fn(
                    "Target.attachedToTarget",
                    lambda event: asyncio.create_task(_configure_attached_target(event)),
                )
                await session.send(
                    "Target.setAutoAttach",
                    {"autoAttach": True, "waitForDebuggerOnStart": True, "flatten": False},
                )
            # new_context/new_page can resize the native window after launch.
            # Apply the same geometry used by the viewport and identity script.
            geometry = profile["identity"].get("geometry")
            from config.settings import get_settings
            if geometry and get_settings().PLAYWRIGHT_SYNC_WINDOW_WITH_VIEWPORT:
                window = await session.send("Browser.getWindowForTarget")
                if isinstance(window, dict) and type(window.get("windowId")) is int:
                    await session.send("Browser.setWindowBounds", {
                        "windowId": window["windowId"],
                        "bounds": {"width": geometry["outerWidth"], "height": geometry["outerHeight"],
                                   "windowState": "normal"},
                    })
        task = asyncio.create_task(prepare())
        page._account_device_ready = task
        # A cancelled waiter uses shield(), so consume a later failure even
        # when no caller remains to await the shared preparation task.
        task.add_done_callback(lambda done: None if done.cancelled() else done.exception())
    await asyncio.shield(task)


async def release_device_page(page):
    """Detach only on actual close; a returned reusable page still needs CDP."""
    task = getattr(page, "_account_device_ready", None)
    if isinstance(task, asyncio.Task) and not task.done() and task is not asyncio.current_task():
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
    session = getattr(page, "_account_device_session", None)
    if session is None:
        return
    page._account_device_session = None
    if callable(getattr(session, "detach", None)):
        try:
            await asyncio.wait_for(session.detach(), timeout=1.0)
        except Exception:
            logger.debug("Device CDP session already closed or detach failed", exc_info=True)


async def install_device_profile(context, profile, canvas_noise=True):
    remember_running_version(profile["override"]["userAgentMetadata"]["fullVersion"])
    await context.add_init_script(identity_script(profile["identity"]))
    context._account_device_profile = profile
    context._account_canvas_noise = True

    async def on_page(page):
        try:
            await prepare_device_page(page)
        except Exception:
            logger.warning("Device page setup failed", exc_info=True)
            # The shared preparation task still fails for its explicit waiter.
            # Pool/caller owns cleanup; this event callback must not close a
            # page concurrently with the caller's error/recovery handling.

    context.on("page", on_page)
