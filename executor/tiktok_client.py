"""
Универсальный TikTok клиент - автоматически выбирает между BAS и Playwright.
Используется как единая точка входа для всех TikTok операций.
"""
import logging
from config.settings import get_settings

logger = logging.getLogger("executor.tiktok_client")
settings = get_settings()

# Импортируем функции в зависимости от настройки USE_BAS
if settings.USE_BAS:
    logger.info("Using BAS (Browser Automation Studio) for TikTok automation")
    from executor.bas_client import (
        post_comment,
        post_reply,
        like_comment,
        quick_validate_account,
        verify_comment_visible,
        BASClientError as TikTokClientError
    )
    # parse_cookies доступна из bas_client (импортирует из tiktok_client_async)
    from executor.tiktok_client_async import parse_cookies, _get_cookies_for_account, _normalize_proxy_for_playwright
else:
    logger.info("Using Playwright for TikTok automation")
    from executor.tiktok_client_async import (
        post_comment,
        post_reply,
        like_comment,
        quick_validate_account,
        verify_comment_visible,
        TikTokClientError,
        parse_cookies,
        _get_cookies_for_account,
        _normalize_proxy_for_playwright
    )

# Экспортируем все функции для использования в других модулях
__all__ = [
    'post_comment',
    'post_reply',
    'like_comment',
    'quick_validate_account',
    'verify_comment_visible',
    'TikTokClientError',
    'parse_cookies',
    '_get_cookies_for_account',
    '_normalize_proxy_for_playwright'
]
