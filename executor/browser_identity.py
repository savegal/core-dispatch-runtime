"""One Windows Chromium identity for browser contexts and HTTP preflights."""
from __future__ import annotations

from functools import lru_cache
import json
from pathlib import Path
import re


_running_version: str | None = None


def chromium_version(version: str | None = None) -> str:
    if version is not None:
        match = re.search(r"\b\d+\.\d+\.\d+\.\d+\b", str(version))
        if not match:
            raise ValueError(f"Invalid Chromium engine version: {version!r}")
        return match.group()
    return _running_version or installed_chromium_version()


def remember_running_version(version: str) -> None:
    global _running_version
    _running_version = chromium_version(version)


@lru_cache(maxsize=1)
def installed_chromium_version() -> str:
    # BrowserPool launches Patchright's bundled chromium without channel/path
    # overrides. Its manifest identifies that exact executable, before launch.
    import patchright
    manifest = Path(patchright.__file__).parent / "driver" / "package" / "browsers.json"
    browsers = json.loads(manifest.read_text(encoding="utf-8"))["browsers"]
    return next(item["browserVersion"] for item in browsers if item["name"] == "chromium")


def desktop_user_agent(version: str | None = None) -> str:
    major = chromium_version(version).split(".")[0]
    return ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
            f"(KHTML, like Gecko) Chrome/{major}.0.0.0 Safari/537.36")


def chromium_brands(version: str | None = None, *, full: bool = False) -> list[dict[str, str]]:
    """Chromium's GetGreasedUserAgentBrandVersion + ShuffleBrandList."""
    # https://chromium.googlesource.com/chromium/src/+/main/components/embedder_support/user_agent_utils.cc
    version = chromium_version(version)
    major = int(version.split(".")[0])
    chars = " (:-./);=?_"
    grease_version = ("8", "99", "24")[major % 3]
    brands = [
        {"brand": f"Not{chars[major % len(chars)]}A{chars[(major + 1) % len(chars)]}Brand",
         "version": grease_version + (".0.0.0" if full else "")},
        {"brand": "Chromium", "version": version if full else str(major)},
        {"brand": "Google Chrome", "version": version if full else str(major)},
    ]
    order = ((0, 1, 2), (0, 2, 1), (1, 0, 2), (1, 2, 0), (2, 0, 1), (2, 1, 0))[major % 6]
    shuffled = [None] * 3
    for index, destination in enumerate(order):
        shuffled[destination] = brands[index]
    return shuffled


def sec_ch_ua(version: str | None = None) -> str:
    return ", ".join(f'"{item["brand"]}";v="{item["version"]}"' for item in chromium_brands(version))
