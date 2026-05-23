"""
Централизованная обработка ошибок для TikTok executor.

Модуль содержит функции для классификации и обработки ошибок,
возникающих при работе с TikTok API.
"""
import logging
from typing import Tuple, Optional
from dataclasses import dataclass
from enum import Enum

logger = logging.getLogger("executor.error_handler")


class ErrorCategory(Enum):
    """Категории ошибок."""
    DEAD_ACCOUNT = "dead_account"
    LOGIN_REQUIRED = "login_required"
    PROXY_ERROR = "proxy_error"
    PROXY_TIMEOUT = "proxy_timeout"
    PROXY_DEAD = "proxy_dead"
    PROXY_AUTH = "proxy_auth"
    NETWORK_ERROR = "network_error"
    CAPTCHA = "captcha"
    BLOCKED = "blocked"
    RATE_LIMIT = "rate_limit"
    COMMENT_INPUT_NOT_FOUND = "comment_input_not_found"
    REPLY_BUTTON_NOT_FOUND = "reply_button_not_found"
    REPLY_INPUT_NOT_FOUND = "reply_input_not_found"
    REPLY_PARENT_NOT_FOUND = "reply_parent_not_found"
    COMMENT_BUTTON_NOT_FOUND = "comment_button_not_found"
    PAGE_LOAD = "page_load"
    NAVIGATION_ERROR = "navigation_error"
    LAYOUT_CHANGED = "layout_changed"
    COMMENT_DISABLED = "comment_disabled"
    ACCOUNT_BANNED = "account_banned"
    VIDEO_UNAVAILABLE = "video_unavailable"
    UNKNOWN = "unknown"


@dataclass
class ErrorClassification:
    """Классификация ошибки."""
    category: ErrorCategory
    description: str
    should_retry: bool
    fatal_for_account: bool = False
    fatal_for_proxy: bool = False
    is_non_critical: bool = False
    
    def to_dict(self) -> dict:
        """Преобразует в словарь."""
        return {
            "error_type": self.category.value,
            "description": self.description,
            "should_retry": self.should_retry,
            "fatal_for_account": self.fatal_for_account,
            "fatal_for_proxy": self.fatal_for_proxy,
            "is_non_critical": self.is_non_critical,
        }


# Не критичные ошибки (не увеличивают error_count)
NON_CRITICAL_ERRORS = {
    ErrorCategory.COMMENT_INPUT_NOT_FOUND,
    ErrorCategory.COMMENT_BUTTON_NOT_FOUND,
    ErrorCategory.REPLY_BUTTON_NOT_FOUND,
    ErrorCategory.REPLY_INPUT_NOT_FOUND,
    ErrorCategory.REPLY_PARENT_NOT_FOUND,
    ErrorCategory.LAYOUT_CHANGED,
    ErrorCategory.RATE_LIMIT,
    ErrorCategory.VIDEO_UNAVAILABLE,
}

# Ошибки, требующие замены прокси
PROXY_ERRORS = {
    ErrorCategory.PROXY_TIMEOUT,
    ErrorCategory.PROXY_DEAD,
    ErrorCategory.PROXY_ERROR,
    ErrorCategory.PROXY_AUTH,
}

# Ошибки, приводящие к пометке аккаунта как dead
FATAL_ACCOUNT_ERRORS = {
    ErrorCategory.DEAD_ACCOUNT,
    ErrorCategory.LOGIN_REQUIRED,
    ErrorCategory.ACCOUNT_BANNED,
}


def classify_error(error_text: str, using_proxy: bool = False) -> ErrorClassification:
    """
    Классифицирует ошибку и возвращает детальную информацию.
    
    Args:
        error_text: Текст ошибки
        using_proxy: True если прокси используется
        
    Returns:
        ErrorClassification: Классифицированная ошибка
    """
    err = error_text.lower()
    
    # 1. Ошибки авторизации (dead аккаунт) - ЯВНЫЕ признаки
    if any(x in err for x in ["unauthorized", "forbidden", "cookies invalid", "cookies expired", 
                               "redirected to login", "login button visible"]):
        return ErrorClassification(
            category=ErrorCategory.DEAD_ACCOUNT,
            description="Куки истекли - аккаунт нужно перелогинить",
            should_retry=True,
            fatal_for_account=True,
        )
    
    # 2. Страница логина - признак мёртвых куков
    if "requires login" in err or "login page detected" in err or "login_required" in err:
        return ErrorClassification(
            category=ErrorCategory.LOGIN_REQUIRED,
            description="Требуется авторизация (куки невалидны)",
            should_retry=True,
            fatal_for_account=True,
        )
    
    # 3. Комментарий не найден / поле ввода не найдено
    if any(x in err for x in ["comment input", "comment input field not found", "comment_input_not_found"]):
        return ErrorClassification(
            category=ErrorCategory.COMMENT_INPUT_NOT_FOUND,
            description="Поле ввода комментария не найдено (возможно изменение layout)",
            should_retry=False,
            is_non_critical=True,
        )
    
    # 3a. Кнопка ответа не найдена
    if any(x in err for x in ["reply_button_not_found", "reply button not found", "не найдена кнопка ответа"]):
        return ErrorClassification(
            category=ErrorCategory.REPLY_BUTTON_NOT_FOUND,
            description="Кнопка Reply не найдена на комментарии",
            should_retry=False,
            is_non_critical=True,
        )
    
    # 3b. Поле ввода ответа не найдено
    if any(x in err for x in ["reply_input_not_found", "reply input not found", "не найдено поле ответа"]):
        return ErrorClassification(
            category=ErrorCategory.REPLY_INPUT_NOT_FOUND,
            description="Поле ввода ответа не найдено",
            should_retry=False,
            is_non_critical=True,
        )
    
    # 3c. Основной комментарий не найден
    if any(x in err for x in ["reply_parent_not_found", "parent comment not found", "основной комментарий не найден"]):
        return ErrorClassification(
            category=ErrorCategory.REPLY_PARENT_NOT_FOUND,
            description="Основной комментарий не найден для ответа",
            should_retry=False,
            is_non_critical=True,
        )
    
    # 4. Кнопка комментария не найдена
    if "comment_button_not_found" in err or "post button" in err:
        return ErrorClassification(
            category=ErrorCategory.COMMENT_BUTTON_NOT_FOUND,
            description="Кнопка отправки комментария не найдена",
            should_retry=False,
            is_non_critical=True,
        )
    
    # 5. Таймаут
    if any(x in err for x in ["timeout", "timed out", "exceeded", "post comment timeout"]):
        if using_proxy:
            return ErrorClassification(
                category=ErrorCategory.PROXY_TIMEOUT,
                description="Таймаут прокси - слишком медленное соединение или прокси не отвечает",
                should_retry=True,
                fatal_for_proxy=True,
            )
        return ErrorClassification(
            category=ErrorCategory.NETWORK_ERROR,
            description="Таймаут сети",
            should_retry=True,
        )
    
    # 6. Проблемы с прокси
    if "proxy" in err:
        if "connection refused" in err:
            return ErrorClassification(
                category=ErrorCategory.PROXY_DEAD,
                description="Прокси недоступен",
                should_retry=True,
                fatal_for_proxy=True,
            )
        if "authentication" in err:
            return ErrorClassification(
                category=ErrorCategory.PROXY_AUTH,
                description="Ошибка авторизации прокси",
                should_retry=True,
                fatal_for_proxy=True,
            )
        return ErrorClassification(
            category=ErrorCategory.PROXY_ERROR,
            description="Ошибка прокси",
            should_retry=True,
            fatal_for_proxy=True,
        )
    
    # 7. Общий "not found" - мягкая ошибка
    if "not found" in err:
        if using_proxy:
            return ErrorClassification(
                category=ErrorCategory.PROXY_ERROR,
                description="Элемент не найден (возможно медленный прокси)",
                should_retry=True,
                fatal_for_proxy=True,
            )
        return ErrorClassification(
            category=ErrorCategory.PAGE_LOAD,
            description="Элемент не найден (возможно изменение DOM или временный глюк)",
            should_retry=True,
        )
    
    # 8. Блокировка TikTok (капча/верификация)
    # ИСПРАВЛЕНО: CAPTCHA не должна ретраиться агрессивно - нужен cooldown (рекомендации ChatGPT)
    if any(x in err for x in ["captcha", "verification", "challenge detected"]):
        return ErrorClassification(
            category=ErrorCategory.CAPTCHA,
            description="TikTok CAPTCHA обнаружена - требуется cooldown, не ретраить агрессивно",
            should_retry=False,  # ИСПРАВЛЕНО: Не ретраить сразу, нужен cooldown
        )
    
    # 9. Аккаунт забанен
    if "blocked" in err or "banned" in err or "account_banned" in err:
        return ErrorClassification(
            category=ErrorCategory.ACCOUNT_BANNED,
            description="Аккаунт заблокирован или забанен",
            should_retry=False,
            fatal_for_account=True,
        )
    
    # 10. Rate limiting
    # ИСПРАВЛЕНО: Rate limit требует cooldown, не агрессивный retry (рекомендации ChatGPT)
    if any(x in err for x in ["rate limit", "too many requests", "try again later", "429"]):
        return ErrorClassification(
            category=ErrorCategory.RATE_LIMIT,
            description="Превышен лимит запросов - требуется cooldown, не ретраить агрессивно",
            should_retry=False,  # ИСПРАВЛЕНО: Не ретраить сразу, нужен cooldown
            is_non_critical=True,
        )
    
    # 11. Видео недоступно
    if any(x in err for x in ["video unavailable", "video is private", "video has been removed", "video_unavailable"]):
        return ErrorClassification(
            category=ErrorCategory.VIDEO_UNAVAILABLE,
            description="Видео недоступно, приватно или удалено",
            should_retry=False,
            is_non_critical=True,
        )
    
    # 12. Ошибки сети
    if any(x in err for x in ["connection", "network", "dns"]):
        return ErrorClassification(
            category=ErrorCategory.NETWORK_ERROR,
            description="Ошибка сети",
            should_retry=True,
        )
    
    # 13. Ошибки навигации
    if "navigation" in err or "navigation_error" in err:
        return ErrorClassification(
            category=ErrorCategory.NAVIGATION_ERROR,
            description="Ошибка навигации (страница не загрузилась)",
            should_retry=True,
            fatal_for_proxy=using_proxy,
        )
    
    # 14. Изменение layout
    if "layout_changed" in err:
        return ErrorClassification(
            category=ErrorCategory.LAYOUT_CHANGED,
            description="Layout страницы изменился",
            should_retry=False,
            is_non_critical=True,
        )
    
    # 15. Комментарии отключены
    if "comment_disabled" in err or "comments are disabled" in err:
        return ErrorClassification(
            category=ErrorCategory.COMMENT_DISABLED,
            description="Комментарии отключены для этого видео",
            should_retry=False,
            is_non_critical=True,
        )
    
    # 16. Неизвестная ошибка
    return ErrorClassification(
        category=ErrorCategory.UNKNOWN,
        description=f"Неизвестная ошибка: {error_text[:100]}",
        should_retry=True,
    )


def is_non_critical_error(error_category: ErrorCategory) -> bool:
    """Проверяет, является ли ошибка не критичной."""
    return error_category in NON_CRITICAL_ERRORS


def is_proxy_error(error_category: ErrorCategory) -> bool:
    """Проверяет, связана ли ошибка с прокси."""
    return error_category in PROXY_ERRORS


def is_fatal_for_account(error_category: ErrorCategory) -> bool:
    """Проверяет, является ли ошибка фатальной для аккаунта."""
    return error_category in FATAL_ACCOUNT_ERRORS


def should_replace_proxy(error_category: ErrorCategory) -> bool:
    """Проверяет, нужно ли заменить прокси."""
    return error_category in PROXY_ERRORS or error_category == ErrorCategory.PROXY_TIMEOUT


# ИСПРАВЛЕНО: Таблица политики ретраев (рекомендации ChatGPT)
RETRYABLE_ERRORS = {
    ErrorCategory.NAVIGATION_ERROR,
    ErrorCategory.LAYOUT_CHANGED,
    ErrorCategory.NETWORK_ERROR,
    ErrorCategory.PROXY_TIMEOUT,
    ErrorCategory.PROXY_ERROR,
}

NON_RETRYABLE_ERRORS = {
    ErrorCategory.LOGIN_REQUIRED,
    ErrorCategory.ACCOUNT_BANNED,
    ErrorCategory.COMMENT_DISABLED,
    ErrorCategory.VIDEO_UNAVAILABLE,
    ErrorCategory.DEAD_ACCOUNT,
}

COOLDOWN_ERRORS = {
    ErrorCategory.CAPTCHA,
    ErrorCategory.RATE_LIMIT,
}


def get_retry_policy(error_category: ErrorCategory) -> str:
    """
    ИСПРАВЛЕНО: Возвращает политику ретраев для ошибки (рекомендации ChatGPT).
    
    Returns:
        str: "retryable", "non-retryable", или "cooldown"
    """
    if error_category in RETRYABLE_ERRORS:
        return "retryable"
    elif error_category in NON_RETRYABLE_ERRORS:
        return "non-retryable"
    elif error_category in COOLDOWN_ERRORS:
        return "cooldown"
    else:
        # По умолчанию - можно попробовать ретрай
        return "retryable"


def should_apply_cooldown(error_category: ErrorCategory) -> bool:
    """
    ИСПРАВЛЕНО: Проверяет, нужно ли применить cooldown (рекомендации ChatGPT).
    
    Для CAPTCHA и RATE_LIMIT нужно заморозить аккаунт/прокси на N минут,
    а не агрессивно ретраить.
    """
    return error_category in COOLDOWN_ERRORS
