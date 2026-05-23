"""
Умный парсер аккаунтов TikTok с валидацией cookies.

Особенности:
- Автоматическая классификация полей (cookies, proxy, user_agent, label)
- Валидация cookies (проверка наличия sid_tt+sessionid или sid_guard+msToken)
- Проверка дубликатов по уникальному ключу
- Подробные сообщения об ошибках

Поддерживаемые форматы cookies:
1. JSON-объект: {"sid_tt": "xxx", "sessionid": "yyy", ...}
2. JSON-массив (browser export): [{"name": "sid_tt", "value": "xxx"}, ...]
3. Inline: sessionid=xxx; sid_tt=yyy

Валидные комбинации:
- sid_tt + sessionid
- sid_guard + msToken
"""

import re
import json
import logging
from typing import Optional, Set
from dataclasses import dataclass
from pathlib import Path

from services.cookie_validator import (
    validate_cookies,
    format_validation_error,
    CookieValidationResult
)

logger = logging.getLogger("services.smart_account_parser")


@dataclass
class ParsedAccountSmart:
    """Распарсенный аккаунт с деталями валидации."""
    label: str
    cookie_file: Optional[str] = None
    cookies: Optional[str] = None  # Нормализованная JSON-строка с cookies
    user_agent: Optional[str] = None
    proxy: Optional[str] = None
    unique_key: Optional[str] = None  # Для проверки дубликатов
    validation_info: Optional[str] = None  # Информация о валидации


@dataclass
class SmartParseResult:
    """Результат парсинга файла."""
    accounts: list
    errors: list
    duplicates: int
    total_lines: int
    accounts_without_proxy: list = None  # Аккаунты без прокси
    
    def __post_init__(self):
        if self.accounts_without_proxy is None:
            self.accounts_without_proxy = [acc for acc in self.accounts if not acc.proxy]


# Паттерн прокси (более гибкий)
PROXY_PATTERNS = [
    re.compile(r'^https?://'),  # http://... или https://...
    re.compile(r'^socks[45]?://'),  # socks5://...
    re.compile(r'^([^@:]+(:[^@]+)?@)?([\d.]+|[\w.-]+):(\d+)$'),  # user:pass@host:port или host:port
]


def _is_proxy(part: str) -> bool:
    """Проверяет, является ли строка прокси."""
    part = part.strip()
    return any(p.match(part) for p in PROXY_PATTERNS)


def _is_user_agent(part: str) -> bool:
    """Проверяет, является ли строка User-Agent."""
    part = part.strip().lower()
    ua_keywords = ['mozilla', 'chrome', 'safari', 'firefox', 'webkit', 'opera', 'edge']
    return any(kw in part for kw in ua_keywords) and ' ' in part and len(part) > 30


def _is_cookie_file(part: str) -> bool:
    """Проверяет, является ли строка путём к файлу cookies."""
    part = part.strip()
    return part.endswith('.txt') or part.endswith('.cookie') or part.endswith('.json')


def _is_cookies(part: str) -> bool:
    """Проверяет, является ли строка cookies."""
    part = part.strip()
    
    # JSON формат
    if (part.startswith('{') and part.endswith('}')) or \
       (part.startswith('[') and part.endswith(']')):
        try:
            json.loads(part)
            return True
        except json.JSONDecodeError:
            pass
    
    # Inline формат с известными ключами cookies
    cookie_keywords = ['sessionid', 'sid_tt', 'sid_guard', 'mstoken', 'uid_tt', 'odin_tt']
    if '=' in part and len(part) > 20:
        part_lower = part.lower()
        return any(kw in part_lower for kw in cookie_keywords)
    
    return False


def classify_part(part: str) -> str:
    """
    Классифицирует часть строки.
    
    Returns:
        'PROXY', 'USER_AGENT', 'COOKIE_FILE', 'COOKIES', 'LABEL', 'UNKNOWN'
    """
    part = part.strip()
    if not part:
        return 'UNKNOWN'
    
    if _is_proxy(part):
        return 'PROXY'
    
    if _is_user_agent(part):
        return 'USER_AGENT'
    
    if _is_cookie_file(part):
        return 'COOKIE_FILE'
    
    if _is_cookies(part):
        return 'COOKIES'
    
    # Если короткая строка без спецсимволов - label
    if len(part) < 50 and not any(c in part for c in ['=', '{', '[', '@']):
        return 'LABEL'
    
    return 'UNKNOWN'


def parse_account_line_smart(
    line: str, 
    line_num: int,
    existing_keys: Set[str]
) -> tuple[Optional[ParsedAccountSmart], Optional[dict], bool]:
    """
    Умный парсер строки аккаунта с валидацией cookies.
    
    Args:
        line: строка для парсинга
        line_num: номер строки
        existing_keys: множество уже существующих ключей для проверки дубликатов
    
    Returns:
        Tuple[ParsedAccountSmart | None, error_dict | None, is_duplicate]
    """
    line = line.strip()
    
    # Игнорируем пустые строки и комментарии
    if not line or line.startswith("#"):
        return None, None, False
    
    # Определяем разделитель
    parts = None
    for delimiter in [";", "|", "\t"]:
        if delimiter in line:
            parts = [p.strip() for p in line.split(delimiter) if p.strip()]
            if len(parts) >= 1:
                break
    
    # Формируем объект аккаунта
    label = None
    cookie_file = None
    cookies = None
    cookies_raw = None
    user_agent = None
    proxy = None
    validation_result: Optional[CookieValidationResult] = None
    
    # Специальная обработка: если строка целиком - валидный JSON-массив, обрабатываем как cookies
    line_stripped = line.strip()
    if (line_stripped.startswith('[') and line_stripped.endswith(']')):
        try:
            # Пробуем распарсить как JSON
            json.loads(line_stripped)
            # Если это валидный JSON-массив, пробуем валидировать как cookies
            test_result = validate_cookies(line_stripped)
            if test_result.is_valid:
                # Это валидные cookies, обрабатываем строку как содержащую только cookies
                cookies_raw = line_stripped
                # Пропускаем дальнейшую обработку частей
                parts = []
                classified = {}
        except (json.JSONDecodeError, ValueError):
            # Не валидный JSON, обрабатываем как обычно
            pass
    
    # Если cookies еще не найдены, разбиваем строку на части
    if not cookies_raw:
        # Определяем разделитель
        parts = None
        for delimiter in [";", "|", "\t"]:
            if delimiter in line:
                parts = [p.strip() for p in line.split(delimiter) if p.strip()]
                if len(parts) >= 1:
                    break
        
        if not parts:
            # Пробуем множественные пробелы (но только если нет JSON)
            if not ('{' in line or '[' in line):
                parts = [p.strip() for p in re.split(r'\s+', line) if p.strip()]
            else:
                parts = [line.strip()]
        
        # Классифицируем каждую часть
        classified = {}
        for part in parts:
            part = part.strip()
            if not part:
                continue
            
            part_type = classify_part(part)
            if part_type not in classified:
                classified[part_type] = []
            classified[part_type].append(part)
        
        # Cookies (приоритет inline над file)
        if 'COOKIES' in classified:
            cookies_raw = classified['COOKIES'][0]
        elif 'UNKNOWN' in classified:
            # Пробуем неклассифицированные части как cookies
            unknown_parts = classified['UNKNOWN']
            # Если только один элемент и он похож на JSON - это cookies
            if len(unknown_parts) == 1:
                unk = unknown_parts[0]
                # Если это JSON (массив или объект), пробуем как cookies
                if (unk.startswith('[') and unk.endswith(']')) or (unk.startswith('{') and unk.endswith('}')):
                    # Пробуем валидировать - если валидно, это cookies
                    test_result = validate_cookies(unk)
                    if test_result.is_valid:
                        cookies_raw = unk
            # Пробуем длинные строки как cookies (на случай, если JSON не распознан)
            if not cookies_raw:
                for unk in unknown_parts:
                    if len(unk) > 50:  # Длинные строки могут быть cookies
                        test_result = validate_cookies(unk)
                        if test_result.is_valid:
                            cookies_raw = unk
                            break
    
    # Cookie file
    if 'COOKIE_FILE' in classified:
        cookie_file = classified['COOKIE_FILE'][0]
    
    # Если нет ни cookies, ни cookie_file
    if not cookies_raw and not cookie_file:
        return None, {
            "line_number": line_num,
            "line_text": line[:100],
            "reason": "Не найдены cookies. Ожидается JSON или inline формат (sid_tt=xxx;sessionid=yyy)"
        }, False
    
    # Валидируем cookies если есть
    if cookies_raw:
        validation_result = validate_cookies(cookies_raw)
        
        if not validation_result.is_valid:
            return None, {
                "line_number": line_num,
                "line_text": line[:100],
                "reason": validation_result.error or "Невалидные cookies"
            }, False
        
        cookies = validation_result.normalized_cookies
        
        # Проверяем дубликаты
        if validation_result.unique_key in existing_keys:
            return None, {
                "line_number": line_num,
                "line_text": line[:100],
                "reason": f"Дубликат аккаунта (ключ: {validation_result.unique_key})"
            }, True
    
    # Label
    if 'LABEL' in classified and len(classified['LABEL']) == 1:
        label = classified['LABEL'][0]
    elif cookie_file:
        # ИСПРАВЛЕНО: Добавляем line_num к имени файла для уникальности
        label = f"{Path(cookie_file).stem}_{line_num}"
    else:
        # ИСПРАВЛЕНО: Добавляем timestamp для уникальности label
        from datetime import datetime, timezone
        timestamp = int(datetime.now(timezone.utc).timestamp())
        label = f"acc_{line_num}_{timestamp}"
    
    # User agent
    if 'USER_AGENT' in classified:
        user_agent = classified['USER_AGENT'][0]
    
    # Proxy
    if 'PROXY' in classified:
        proxy = classified['PROXY'][0]
    
    # Формируем validation_info
    validation_info = None
    if validation_result:
        validation_info = f"Cookies: {validation_result.matched_set}"
    
    return ParsedAccountSmart(
        label=label,
        cookie_file=cookie_file,
        cookies=cookies,
        user_agent=user_agent,
        proxy=proxy,
        unique_key=validation_result.unique_key if validation_result else None,
        validation_info=validation_info
    ), None, False


def parse_accounts_file_smart(content: str) -> SmartParseResult:
    """
    Парсит файл с аккаунтами с валидацией cookies и проверкой дубликатов.
    
    Returns:
        SmartParseResult с результатами парсинга
    """
    # ИСПРАВЛЕНО: Проверяем, является ли весь файл одним JSON-массивом куков
    content_stripped = content.strip()
    logger.debug(f"Parsing file: starts with '[' = {content_stripped.startswith('[')}, ends with ']' = {content_stripped.endswith(']')}")
    
    if content_stripped.startswith('[') and content_stripped.endswith(']'):
        try:
            # Пробуем распарсить весь файл как JSON-массив куков
            parsed_json = json.loads(content_stripped)
            logger.debug(f"Successfully parsed as JSON array, length: {len(parsed_json) if isinstance(parsed_json, list) else 'N/A'}")
            
            # Если это валидный JSON-массив, обрабатываем как один аккаунт
            validation = validate_cookies(content_stripped)
            logger.debug(f"Validation result: is_valid={validation.is_valid}, error={validation.error}, matched_set={validation.matched_set}")
            
            if validation.is_valid:
                logger.info("File is a single JSON array of cookies, treating as one account")
                # ИСПРАВЛЕНО: Генерируем уникальный label для каждого импорта
                from datetime import datetime, timezone
                timestamp = int(datetime.now(timezone.utc).timestamp())
                account = ParsedAccountSmart(
                    label=f"acc_imported_{timestamp}",
                    cookies=validation.normalized_cookies,
                    unique_key=validation.unique_key,
                    validation_info=validation.matched_set
                )
                logger.info(f"Created account: label={account.label}, has_cookies={bool(account.cookies)}, unique_key={account.unique_key[:50] if account.unique_key else None}")
                return SmartParseResult(
                    accounts=[account],
                    errors=[],
                    duplicates=0,
                    total_lines=1,
                    accounts_without_proxy=[account] if not account.proxy else []
                )
            else:
                logger.warning(f"JSON array is not valid cookies: {validation.error}")
        except json.JSONDecodeError as e:
            logger.debug(f"File is not a valid JSON array, parsing line by line: {e}")
        except Exception as e:
            logger.error(f"Error processing single JSON array: {e}", exc_info=True)
    
    # Обычная обработка построчно
    lines = content.split("\n")
    accounts: list[ParsedAccountSmart] = []
    errors: list[dict] = []
    existing_keys: Set[str] = set()
    duplicates = 0
    
    for idx, line in enumerate(lines, start=1):
        account, error, is_duplicate = parse_account_line_smart(line, idx, existing_keys)
        
        if is_duplicate:
            duplicates += 1
            if error:
                errors.append(error)
        elif error:
            errors.append(error)
        elif account:
            accounts.append(account)
            if account.unique_key:
                existing_keys.add(account.unique_key)
    
    logger.info(
        f"Smart parser: {len(accounts)} valid accounts, "
        f"{len(errors)} errors, {duplicates} duplicates"
    )
    
    return SmartParseResult(
        accounts=accounts,
        errors=errors,
        duplicates=duplicates,
        total_lines=len(lines)
    )


def format_smart_parse_report(
    accounts: list[ParsedAccountSmart], 
    errors: list[dict],
    duplicates: int = 0
) -> str:
    """Форматирует отчёт о парсинге."""
    lines = [
        f"<b>Результат парсинга:</b>\n",
        f"✅ Валидных аккаунтов: {len(accounts)}",
    ]
    
    if duplicates > 0:
        lines.append(f"⚠️ Пропущено дубликатов: {duplicates}")
    
    if errors:
        lines.append(f"❌ Ошибок: {len(errors)}")
        lines.append("")
        lines.append("<b>Первые 5 ошибок:</b>")
        for error in errors[:5]:
            line_num = error.get('line_number', '?')
            reason = error.get('reason', 'Неизвестная ошибка')
            cookies_found = error.get('cookies_found', [])
            cookies_missing = error.get('cookies_missing', [])
            
            lines.append(f"• Строка {line_num}: {reason}")
            if cookies_found:
                lines.append(f"  Найдено: {', '.join(cookies_found)}")
            if cookies_missing:
                lines.append(f"  Не хватает: {', '.join(cookies_missing)}")
        
        if len(errors) > 5:
            lines.append(f"... и ещё {len(errors) - 5} ошибок")
    
    # Статистика по cookies
    if accounts:
        cookies_stats = {}
        for acc in accounts:
            if acc.validation_info:
                key = acc.validation_info
                cookies_stats[key] = cookies_stats.get(key, 0) + 1
        
        if cookies_stats:
            lines.append("")
            lines.append("<b>Типы cookies:</b>")
            for key, count in sorted(cookies_stats.items(), key=lambda x: -x[1]):
                lines.append(f"• {key}: {count}")
    
    return "\n".join(lines)
