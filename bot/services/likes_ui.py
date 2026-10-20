import logging

from bot.services.api_client import api_client
from config.settings import get_settings
from services.likes_config import (
    BOOST_LIKES_DEFAULT,
    coerce_likes_quantity,
    provider_available as likes_provider_available,
)

logger = logging.getLogger("bot.likes_ui")
LIKES_DISABLED_LABEL = "выключены"


def _coerce_quantity(value: object, default: int = BOOST_LIKES_DEFAULT) -> int:
    return coerce_likes_quantity(value, default)


def format_likes_label(*, enabled: bool, provider_available: bool, quantity: int) -> str:
    return str(quantity) if enabled and provider_available and quantity > 0 else LIKES_DISABLED_LABEL


def has_reply_texts(replies_or_count: object) -> bool:
    if isinstance(replies_or_count, int):
        return replies_or_count > 0
    if isinstance(replies_or_count, dict):
        return any(str(value or "").strip() for value in replies_or_count.values())
    if isinstance(replies_or_count, (list, tuple, set)):
        return any(str(value or "").strip() for value in replies_or_count)
    return False


async def get_likes_display_label() -> str:
    """Return the effective user-facing likes state: exact quantity or disabled."""
    settings = get_settings()
    try:
        data = await api_client.get_boost_likes_settings()
        return format_likes_label(
            enabled=bool(data.get("boost_likes_enabled")),
            provider_available=bool(data.get("boost_provider_available", True)),
            quantity=_coerce_quantity(data.get("boost_likes_quantity"), settings.BOOST_PROJECT_LIKES_QUANTITY),
        )
    except Exception as exc:
        logger.debug("Could not load likes settings for UI: %s", exc)
        return format_likes_label(
            enabled=likes_provider_available(settings.BOOST_PROJECT_API_KEY),
            provider_available=likes_provider_available(settings.BOOST_PROJECT_API_KEY),
            quantity=_coerce_quantity(settings.BOOST_PROJECT_LIKES_QUANTITY, BOOST_LIKES_DEFAULT),
        )


async def get_likes_display_label_for_replies(replies_or_count: object) -> str:
    """Return the order-facing likes label; full orders without replies never get likes."""
    if not has_reply_texts(replies_or_count):
        return LIKES_DISABLED_LABEL
    return await get_likes_display_label()


def format_reply_likes_label(target: object = "1", count: object = 0) -> str:
    """Форматирует статус точечных лайков на ответ для UI."""
    target_str = str(target or "1").strip().lower()
    try:
        cnt = int(count or 0)
    except (ValueError, TypeError):
        cnt = 0
    if cnt <= 0 or target_str in {"none", "0", "off", "disabled"}:
        return "выключены"
    if target_str == "all":
        return f"все ответы ({cnt} шт)"
    return f"ответ {target_str} ({cnt} шт)"

