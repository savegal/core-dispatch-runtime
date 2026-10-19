"""Shared boundaries for the page-backed HTTP publication pipeline."""


def http_first_enabled(settings) -> bool:
    def enabled(name):
        value = getattr(settings, name, False)
        return value.strip().lower() in {"1", "true", "yes", "on"} if isinstance(value, str) else bool(value)
    return enabled("TIKTOK_NETWORK_FIRST") and enabled("TIKTOK_NETWORK_IMPL_READY")


def valid_comment_id(value) -> bool:
    text = str(value or "").strip()
    return text.isascii() and text.isdigit() and 10 <= len(text) <= 30 and int(text) > 0
