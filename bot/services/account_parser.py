"""
Парсер аккаунтов TikTok с валидацией cookies.

Поддерживаемые форматы строк:
1. label;cookies;user_agent;proxy (полный)
2. cookies;user_agent;proxy (без label)
3. cookies;proxy (без label и user_agent)
4. cookies (только cookies)

Поддерживаемые форматы cookies:
1. JSON-объект: {"sid_tt": "xxx", "sessionid": "yyy", ...}
2. JSON-массив (browser export): [{"name": "sid_tt", "value": "xxx"}, ...]
3. Inline: sessionid=xxx; sid_tt=yyy

Валидные комбинации cookies:
- sid_tt + sessionid
- sid_guard + msToken
"""

import re
import logging
from typing import Optional, List, Set
from dataclasses import dataclass

from services.cookie_validator import (
    validate_cookies,
    format_validation_error,
    CookieValidationResult
)

logger = logging.getLogger("bot.account_parser")


@dataclass
class ParsedAccount:
    """Распарсенный аккаунт."""
    label: str
    cookies: str  # Нормализованная JSON-строка
    user_agent: str
    proxy: Optional[str]
    unique_key: str  # Уникальный ключ для проверки дубликатов
    validation_info: str  # Информация о валидации (какой набор cookies)
    cookie_strength: str = "unknown"  # full | enriched | minimal | dead


@dataclass
class ParseResult:
    """Результат парсинга файла с аккаунтами."""
    accounts: List[ParsedAccount]
    errors: List[str]
    duplicates: int
    total_lines: int
    valid_count: int


DEFAULT_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
)


def _detect_cookies_in_parts(parts: List[str]) -> int:
    """
    Определяет, какой элемент в списке является cookies.
    Возвращает индекс элемента с cookies или -1 если не найдено.
    """
    for idx, part in enumerate(parts):
        part = part.strip()
        # JSON-объект или массив
        if part.startswith("{") or part.startswith("["):
            return idx
        # Inline cookies
        if "=" in part and ("sessionid" in part.lower() or "sid_tt" in part.lower() or 
                           "sid_guard" in part.lower() or "mstoken" in part.lower()):
            return idx
    return -1


def _is_user_agent(value: str) -> bool:
    """Проверяет, является ли строка User-Agent."""
    value = value.strip().lower()
    return "mozilla" in value or "chrome" in value or "safari" in value or "webkit" in value


def _is_proxy(value: str) -> bool:
    """Проверяет, является ли строка прокси."""
    value = value.strip()
    # Паттерны прокси
    patterns = [
        r'^https?://',  # http://... или https://...
        r'^socks[45]?://',  # socks5://... 
        r'^\d{1,3}\.\d{1,3}\.\d{1,3}\.\d{1,3}(:\d+)?$',  # 123.123.123.123:8080
        r'^[\w.-]+:\d+$',  # proxy.example.com:8080
        r'^[\w.-]+:[\w.-]+@[\w.-]+:\d+$',  # user:pass@proxy:8080
    ]
    return any(re.match(p, value) for p in patterns)


def parse_account_line(
    line: str, 
    line_num: int, 
    existing_keys: Set[str],
    default_label_prefix: str = "acc_"
) -> tuple[Optional[ParsedAccount], Optional[str], bool]:
    """
    Парсит одну строку с аккаунтом.
    
    Args:
        line: строка для парсинга
        line_num: номер строки (для сообщений об ошибках)
        existing_keys: множество уже существующих уникальных ключей (для проверки дубликатов)
        default_label_prefix: префикс для автоматической генерации label
    
    Returns:
        Tuple[ParsedAccount | None, error_message | None, is_duplicate]
    """
    line = line.strip()

    # Игнорируем пустые строки и комментарии
    if not line or line.startswith("#"):
        return None, None, False

    # Определяем разделитель
    delimiter = None
    for d in [";", "|", "\t"]:
        if d in line:
            delimiter = d
            break

    if delimiter:
        parts = [p.strip() for p in line.split(delimiter)]
    else:
        # Только один элемент (cookies)
        parts = [line]

    parts = [p for p in parts if p]  # Убираем пустые

    if not parts:
        return None, f"Строка {line_num}: пустая после парсинга", False

    # Определяем, где cookies
    cookies_idx = _detect_cookies_in_parts(parts)
    
    if cookies_idx == -1:
        # Предполагаем, что первый элемент - cookies
        cookies_idx = 0

    cookies_raw = parts[cookies_idx]
    
    # Валидируем cookies
    validation = validate_cookies(cookies_raw)
    
    if not validation.is_valid:
        error_msg = format_validation_error(validation, line_num)
        return None, error_msg, False

    # Проверяем дубликаты
    if validation.unique_key in existing_keys:
        return None, f"Строка {line_num}: дубликат аккаунта (ключ: {validation.unique_key})", True

    # Определяем остальные поля
    label = None
    user_agent = None
    proxy = None

    # Убираем cookies из списка и анализируем остальное
    remaining = [p for i, p in enumerate(parts) if i != cookies_idx]

    for part in remaining:
        if _is_proxy(part):
            proxy = part
        elif _is_user_agent(part):
            user_agent = part
        elif not label:
            # Первый неопознанный элемент - label
            label = part

    # Дефолтные значения
    if not label:
        label = f"{default_label_prefix}{line_num}"
    if not user_agent:
        user_agent = DEFAULT_USER_AGENT

    # Валидация proxy (базовая нормализация)
    if proxy:
        proxy = proxy.strip()
        # Если нет протокола - проверяем и пропускаем если некорректный
        if not any(proxy.startswith(p) for p in ["http://", "https://", "socks4://", "socks5://"]):
            # Простая проверка формата ip:port или host:port
            if not re.match(r'^[\w.-]+(:\d+)?$', proxy) and not re.match(r'^[\w.-]+:[\w.-]+@[\w.-]+:\d+$', proxy):
                logger.warning(f"Line {line_num}: Invalid proxy format: {proxy[:50]}")
                proxy = None

    return ParsedAccount(
        label=label,
        cookies=validation.normalized_cookies,
        user_agent=user_agent,
        proxy=proxy,
        unique_key=validation.unique_key,
        validation_info=f"Cookies: {validation.matched_set}",
        cookie_strength=validation.strength,
    ), None, False


def parse_accounts_file(content: str) -> ParseResult:
    """
    Парсит файл с аккаунтами.
    
    Args:
        content: содержимое файла
        
    Returns:
        ParseResult с результатами парсинга
    """
    lines = content.split("\n")
    accounts: List[ParsedAccount] = []
    errors: List[str] = []
    existing_keys: Set[str] = set()
    duplicates = 0
    valid_count = 0

    for idx, line in enumerate(lines, start=1):
        account, error, is_duplicate = parse_account_line(line, idx, existing_keys)
        
        if is_duplicate:
            duplicates += 1
            errors.append(error)
        elif error:
            errors.append(error)
        elif account:
            accounts.append(account)
            existing_keys.add(account.unique_key)
            valid_count += 1

    return ParseResult(
        accounts=accounts,
        errors=errors,
        duplicates=duplicates,
        total_lines=len(lines),
        valid_count=valid_count
    )


def accounts_to_api_format(accounts: List[ParsedAccount]) -> List[dict]:
    """Конвертирует список ParsedAccount в формат для API."""
    return [
        {
            "label": acc.label,
            "cookies": acc.cookies,
            "user_agent": acc.user_agent,
            "proxy": acc.proxy
        }
        for acc in accounts
    ]


def format_parse_report(result: ParseResult) -> str:
    """Форматирует отчёт о парсинге для отображения пользователю."""
    lines = [
        f"Обработано строк: {result.total_lines}",
        f"Валидных аккаунтов: {result.valid_count}",
    ]

    # Раскладка по "силе" cookies. Это важно когда юзер импортирует купленные
    # купоны с урезанным набором (только sid_guard+msToken): в логе сразу видно
    # что N штук попадут в auto-enrichment перед первым коммент-таском.
    if result.accounts:
        strength_counts: dict[str, int] = {}
        for acc in result.accounts:
            key = getattr(acc, "cookie_strength", "unknown") or "unknown"
            strength_counts[key] = strength_counts.get(key, 0) + 1
        if strength_counts:
            order = ["full", "enriched", "minimal", "dead", "unknown"]
            parts = [
                f"{name}: {strength_counts[name]}"
                for name in order
                if strength_counts.get(name)
            ]
            if parts:
                lines.append("Сила cookies — " + ", ".join(parts))
            if strength_counts.get("minimal"):
                lines.append(
                    f"  ↳ {strength_counts['minimal']} аккаунт(ов) пройдут "
                    f"авто-обогащение через TikTok при первом коммент-таске "
                    f"(дособерут ttwid/tt_csrf_token/sessionid)."
                )

    if result.duplicates > 0:
        lines.append(f"Пропущено дубликатов: {result.duplicates}")

    if result.errors:
        lines.append(f"Ошибок: {len(result.errors)}")
        # Показываем первые 5 ошибок
        for error in result.errors[:5]:
            lines.append(f"  • {error}")
        if len(result.errors) > 5:
            lines.append(f"  ... и ещё {len(result.errors) - 5} ошибок")

    return "\n".join(lines)
