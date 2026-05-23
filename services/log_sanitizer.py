"""
Утилита для санитизации логов - скрывает чувствительные данные.
"""
import re
import json
from typing import Any, Dict, List, Union
from urllib.parse import parse_qsl, urlencode, urlparse, urlunparse


# Ключи, которые содержат чувствительные данные
SENSITIVE_KEYS = {
    'cookies', 'cookie', 'token', 'password', 'pass', 'secret', 'api_key',
    'api_token', 'jwt', 'authorization', 'auth', 'access_token', 'refresh_token',
    'sessionid', 'sid_tt', 'sid_guard', 'csrf', 'csrf_token', 'session',
    'executor_api_token', 'jwt_secret', 'bot_token', 'telegram_token'
}

# Паттерны для поиска чувствительных данных в строках
SENSITIVE_PATTERNS = [
    r'(?i)(password|pass|secret|token|api[_-]?key)\s*[:=]\s*["\']?([^"\'\s]+)["\']?',
    r'(?i)(cookie|sessionid|sid_tt|sid_guard)\s*[:=]\s*["\']?([^"\'\s]+)["\']?',
    r'Bearer\s+([A-Za-z0-9\-_]+)',
    r'Authorization:\s*([^\s]+)',
]

SENSITIVE_QUERY_KEYS = {
    "token",
    "api_key",
    "apikey",
    "api-token",
    "key",
    "secret",
    "password",
    "pass",
    "auth",
    "access_token",
    "refresh_token",
    "session",
}


def sanitize_url(value: str, max_length: int = 160) -> str:
    """Redacts credentials and sensitive query values from a URL for logs."""
    if not value:
        return value

    try:
        parsed = urlparse(value)
    except Exception:
        return sanitize_string(str(value), max_length=max_length)

    netloc = parsed.netloc
    if "@" in netloc:
        _, host = netloc.rsplit("@", 1)
        netloc = f"***REDACTED***@{host}"

    redacted_query = []
    for key, item_value in parse_qsl(parsed.query, keep_blank_values=True):
        key_lower = key.lower()
        if any(sensitive in key_lower for sensitive in SENSITIVE_QUERY_KEYS):
            redacted_query.append((key, "***REDACTED***"))
        else:
            redacted_query.append((key, item_value))

    sanitized = urlunparse(
        (
            parsed.scheme,
            netloc,
            parsed.path,
            parsed.params,
            urlencode(redacted_query, safe="*"),
            parsed.fragment,
        )
    )
    if len(sanitized) > max_length:
        sanitized = sanitized[:max_length] + "...***REDACTED***"
    return sanitized


def sanitize_string(value: str, max_length: int = 50) -> str:
    """
    Санитизирует строку, скрывая чувствительные данные.
    
    Args:
        value: Строка для санитизации
        max_length: Максимальная длина видимой части
        
    Returns:
        str: Санитизированная строка
    """
    if not value:
        return value
    
    # Применяем паттерны для замены
    sanitized = value
    for pattern in SENSITIVE_PATTERNS:
        sanitized = re.sub(pattern, r'\1=***REDACTED***', sanitized)
    
    # Если строка содержит чувствительные данные, обрезаем её
    if any(key in sanitized.lower() for key in SENSITIVE_KEYS):
        if len(sanitized) > max_length:
            sanitized = sanitized[:max_length] + "...***REDACTED***"
        else:
            sanitized = "***REDACTED***"
    
    return sanitized


def sanitize_dict(data: Dict[str, Any], max_depth: int = 5) -> Dict[str, Any]:
    """
    Санитизирует словарь, скрывая чувствительные данные.
    
    Args:
        data: Словарь для санитизации
        max_depth: Максимальная глубина рекурсии
        
    Returns:
        Dict: Санитизированный словарь
    """
    if max_depth <= 0:
        return {"***": "MAX_DEPTH_EXCEEDED"}
    
    sanitized = {}
    
    for key, value in data.items():
        key_lower = str(key).lower()
        
        # Если ключ чувствительный - скрываем значение
        if any(sensitive_key in key_lower for sensitive_key in SENSITIVE_KEYS):
            sanitized[key] = "***REDACTED***"
        elif isinstance(value, dict):
            sanitized[key] = sanitize_dict(value, max_depth - 1)
        elif isinstance(value, list):
            sanitized[key] = sanitize_list(value, max_depth - 1)
        elif isinstance(value, str):
            # Проверяем, содержит ли строка чувствительные данные
            if any(sensitive_key in value.lower() for sensitive_key in SENSITIVE_KEYS):
                sanitized[key] = "***REDACTED***"
            else:
                sanitized[key] = sanitize_string(value)
        else:
            sanitized[key] = value
    
    return sanitized


def sanitize_list(data: List[Any], max_depth: int = 5) -> List[Any]:
    """
    Санитизирует список, скрывая чувствительные данные.
    
    Args:
        data: Список для санитизации
        max_depth: Максимальная глубина рекурсии
        
    Returns:
        List: Санитизированный список
    """
    if max_depth <= 0:
        return ["***MAX_DEPTH_EXCEEDED***"]
    
    sanitized = []
    
    for item in data:
        if isinstance(item, dict):
            sanitized.append(sanitize_dict(item, max_depth - 1))
        elif isinstance(item, list):
            sanitized.append(sanitize_list(item, max_depth - 1))
        elif isinstance(item, str):
            sanitized.append(sanitize_string(item))
        else:
            sanitized.append(item)
    
    return sanitized


def sanitize_for_logging(data: Union[Dict, List, str, Any]) -> Union[Dict, List, str]:
    """
    Универсальная функция для санитизации данных перед логированием.
    
    Args:
        data: Данные для санитизации (dict, list, str или любой другой тип)
        
    Returns:
        Санитизированные данные
    """
    if isinstance(data, dict):
        return sanitize_dict(data)
    elif isinstance(data, list):
        return sanitize_list(data)
    elif isinstance(data, str):
        return sanitize_string(data)
    else:
        # Для других типов пробуем преобразовать в строку
        try:
            str_repr = str(data)
            return sanitize_string(str_repr)
        except Exception:
            return "***UNABLE_TO_SANITIZE***"


def safe_log_dict(logger, level: str, message: str, data: Dict[str, Any]):
    """
    Безопасно логирует словарь с санитизацией.
    
    Args:
        logger: Логгер
        level: Уровень логирования ('info', 'warning', 'error', 'debug')
        message: Сообщение
        data: Данные для логирования
    """
    sanitized_data = sanitize_for_logging(data)
    
    if level == 'info':
        logger.info(f"{message}: {sanitized_data}")
    elif level == 'warning':
        logger.warning(f"{message}: {sanitized_data}")
    elif level == 'error':
        logger.error(f"{message}: {sanitized_data}")
    elif level == 'debug':
        logger.debug(f"{message}: {sanitized_data}")
    else:
        logger.info(f"{message}: {sanitized_data}")



