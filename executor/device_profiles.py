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


def _account_seed_key(account) -> str:
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
        if len(token_val) >= 8:
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
    return hashlib.sha256(_account_seed_key(account).encode("utf-8")).digest()


def desktop_hardware(account) -> dict:
    seed = _digest(account)
    width, height = RESOLUTIONS[int.from_bytes(seed[:4], "big") % len(RESOLUTIONS)]
    gpu = WINDOWS_GPUS[int.from_bytes(seed[12:16], "big") % len(WINDOWS_GPUS)]
    return {"width": width, "height": height,
            "hardwareConcurrency": CPU_COUNTS[int.from_bytes(seed[4:8], "big") % len(CPU_COUNTS)],
            "deviceMemory": MEMORY_GB[int.from_bytes(seed[8:12], "big") % len(MEMORY_GB)],
            "webglVendor": gpu[0], "webglRenderer": gpu[1]}


def build_device_profile(account, devices: dict, browser_version: str, chrome_height: int = 90) -> dict:
    version = chromium_version(browser_version)
    ua = desktop_user_agent(version)
    seed = _digest(account)
    hardware = desktop_hardware(account)
    width, height = hardware["width"], hardware["height"]
    chrome_height = max(0, min(int(chrome_height), 240, height - 480))
    return {
        "options": {"user_agent": ua, "viewport": {"width": width, "height": height - chrome_height},
                    "screen": {"width": width, "height": height}, "device_scale_factor": 1,
                    "has_touch": False, "is_mobile": False},
        "identity": {"family": "windows", "preset": None, "platform": "Win32", "vendor": "Google Inc.",
                     "mobile": False, "ios": False, "hardware": hardware,
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
            await session.send("Emulation.setUserAgentOverride", profile["override"])
            await session.send("Emulation.setHardwareConcurrencyOverride", {
                "hardwareConcurrency": profile["identity"]["hardware"]["hardwareConcurrency"],
            })
        task = asyncio.create_task(prepare())
        page._account_device_ready = task
    await asyncio.shield(task)


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
            if not page.is_closed():
                await page.close()

    context.on("page", on_page)
