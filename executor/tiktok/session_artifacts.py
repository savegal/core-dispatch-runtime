import logging
import re

logger = logging.getLogger("executor.tiktok.session_artifacts")


def _extract_aweme_id_from_url(url: str) -> str | None:
    """Пытается вытащить aweme_id из любого варианта TikTok ссылки."""
    if not url:
        return None
    patterns = [
        r"aweme_id=(\d+)",
        r"/video/(\d+)",
        r"item_id=(\d+)",
        r"\b(\d{16,20})\b",
    ]
    for pattern in patterns:
        m = re.search(pattern, url)
        if m:
            return m.group(1)
    return None


async def collect_session_artifacts(page):
    """
    Собирает базовые session artifacts: cookies + aweme_id/video_url.
    Используется для network-first запросов.
    """
    try:
        cookies = await page.context.cookies()
    except Exception:
        cookies = []

    try:
        current_url = page.url
    except Exception:
        current_url = ""

    aweme_id = _extract_aweme_id_from_url(current_url)

    artifacts = {
        "cookies": cookies,
        "csrf": None,
        "msToken": None,
        "headers": {},
        "video_url": current_url,
        "aweme_id": aweme_id,
    }

    return artifacts
