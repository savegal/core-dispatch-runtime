"""
HTTP-based cookie validation for TikTok accounts.
Uses aiohttp instead of Playwright - works in any async context.
"""
import aiohttp
import asyncio
import logging
import json
from typing import Dict, List, Any, Optional, Tuple
from dataclasses import dataclass
from db.models import Account

logger = logging.getLogger("services.cookie_validator")


def parse_cookies_to_dict(raw: str) -> Dict[str, str]:
    """
    Парсит cookies из строки в словарь {name: value}.
    
    Поддерживаемые форматы:
    1. JSON-объект: {"sid_tt": "xxx", "sessionid": "yyy"}
    2. JSON-массив: [{"name": "sid_tt", "value": "xxx"}, ...]
       Поддерживает полный формат браузерных расширений с дополнительными полями.
       При коллизиях имени (например, sid_guard продублирован для ".tiktok.com"
       и "tiktok.com" с разными значениями) выбирается запись с максимальным
       expirationDate/expiration — это всегда самое свежее значение,
       а не "последнее по порядку в файле".
    3. Inline: sessionid=xxx; sid_tt=yyy
    """
    if not raw or not raw.strip():
        return {}
    
    raw = raw.strip()
    result = {}
    
    try:
        data = json.loads(raw)
        
        if isinstance(data, list):
            # Browser export format [{"name": "x", "value": "y"}, ...]
            # При коллизии имени выбираем запись с большим expirationDate.
            best_expiry: Dict[str, float] = {}
            for cookie in data:
                if not isinstance(cookie, dict):
                    continue
                name = cookie.get("name") or cookie.get("Name") or cookie.get("key")
                value = cookie.get("value") or cookie.get("Value")
                # Принимаем даже пустые строки как валидные значения (некоторые cookies могут быть пустыми)
                if not name or value is None:
                    continue
                name_str = str(name)
                try:
                    expiry_raw = (
                        cookie.get("expirationDate")
                        or cookie.get("expiration")
                        or cookie.get("expires")
                        or 0
                    )
                    expiry = float(expiry_raw)
                except (TypeError, ValueError):
                    expiry = 0.0
                # Правило выбора при коллизии:
                # 1. Если у новой записи expirationDate СТРОГО больше — заменяем
                #    (это "явно более свежий" cookie с длинным сроком).
                # 2. Если expirationDate равны (часто оба = 2147483647 ∞) —
                #    тоже заменяем, потому что в TikTok-кук-дампах последний
                #    встретившийся sid_guard почти всегда содержит более новый
                #    embedded created_ts (TikTok пишет ротированную копию ниже
                #    в списке). Старое поведение dict-перезаписывания так же
                #    выбирало последний — мы его сохраняем.
                # 3. Только если новый expiry СТРОГО меньше — игнорим.
                if name_str not in result or expiry >= best_expiry.get(name_str, -1.0):
                    result[name_str] = str(value)
                    best_expiry[name_str] = expiry
        
        elif isinstance(data, dict):
            # Check for nested cookies key
            if "cookies" in data and isinstance(data["cookies"], list):
                return parse_cookies_to_dict(json.dumps(data["cookies"]))
            
            # Simple key-value format
            for key, value in data.items():
                if key and value is not None and not isinstance(value, (dict, list)):
                    result[str(key)] = str(value)
        
        return result
    
    except json.JSONDecodeError:
        pass
    
    # Inline format: key=value; key2=value2
    for part in raw.replace("; ", ";").split(";"):
        part = part.strip()
        if "=" in part:
            key, _, value = part.partition("=")
            key = key.strip()
            value = value.strip()
            if key:
                result[key] = value
    
    return result


def get_cookies_for_account(account: Account) -> str:
    """Получает cookies аккаунта из любого источника."""
    if getattr(account, "cookies", None):
        return account.cookies
    
    if getattr(account, "cookie_file", None):
        try:
            from pathlib import Path
            from config.settings import get_settings

            cookies_dir = Path(get_settings().COOKIES_DIR).resolve()
            cookie_path = Path(account.cookie_file)
            if not cookie_path.is_absolute():
                cookie_path = cookies_dir / cookie_path
            cookie_path = cookie_path.resolve()
            if not cookie_path.is_relative_to(cookies_dir):
                logger.warning("Rejected cookie_file outside COOKIES_DIR for account %s", getattr(account, "id", "?"))
                return ""
            if cookie_path.exists():
                return cookie_path.read_text(encoding="utf-8")
        except Exception:
            pass
    
    return ""


def _result(
    *,
    status: str = "inconclusive",
    valid: bool = False,
    username: Optional[str] = None,
    error: Optional[str] = None,
    country_code: Optional[str] = None,
    definitive: bool = False,
) -> dict:
    return {
        "valid": valid,
        "status": status,
        "username": username,
        "error": error,
        "country_code": country_code,
        "definitive": definitive,
    }


def _dead_result(error: str, *, country_code: Optional[str] = None) -> dict:
    return _result(status="dead", valid=False, error=error, country_code=country_code, definitive=True)


def _valid_result(
    *,
    username: Optional[str] = None,
    country_code: Optional[str] = None,
) -> dict:
    return _result(
        status="valid",
        valid=True,
        username=username,
        country_code=country_code,
        definitive=True,
    )


def _inconclusive_result(error: str, *, country_code: Optional[str] = None) -> dict:
    return _result(status="inconclusive", valid=False, error=error, country_code=country_code, definitive=False)


def _normalize_proxy_for_aiohttp(proxy_raw: Optional[str]) -> Tuple[Optional[str], Optional[str]]:
    """Return (proxy_url, reason_if_unsupported).

    We only use the HTTP probe as a hard negative gate when it uses the same
    network path as the browser. For SOCKS proxies we require aiohttp_socks;
    without it the caller must treat the HTTP probe as inconclusive.
    """
    proxy = (proxy_raw or "").strip()
    if not proxy:
        return None, None
    lower = proxy.lower()
    if lower.startswith(("socks4://", "socks5://")):
        try:
            import aiohttp_socks  # noqa: F401
        except Exception:
            return None, "socks_proxy_requires_aiohttp_socks"
        return proxy, None
    if lower.startswith(("http://", "https://")):
        return proxy, None
    if "@" in proxy:
        return f"http://{proxy}", None
    parts = proxy.split(":")
    if len(parts) == 4:
        host, port, user, password = parts
        if host and port and user:
            return f"http://{user}:{password}@{host}:{port}", None
    if len(parts) == 2:
        return f"http://{proxy}", None
    return None, "unsupported_proxy_format"


def _html_has_logged_out_marker(text: str) -> Optional[str]:
    lowered = (text or "").lower()
    markers = [
        '"islogin":false',
        '"is_login":false',
        'data-e2e="login-button"',
        'log in to comment',
        'log in to tiktok',
        'login to comment',
    ]
    for marker in markers:
        if marker in lowered:
            return marker
    return None


def _html_has_logged_in_marker(text: str) -> Optional[str]:
    lowered = (text or "").lower()
    markers = [
        '"islogin":true',
        '"is_login":true',
        'data-e2e="inbox-icon"',
        'data-e2e="message-icon"',
        'href="/messages"',
    ]
    for marker in markers:
        if marker in lowered:
            return marker
    return None


async def _validate_account_http_tri_state(account: Account) -> dict:
    country_code = None
    try:
        from services.geo_detection_service import detect_account_country
        country_code = detect_account_country(account)
    except Exception:
        country_code = None

    cookies_str = get_cookies_for_account(account)
    if not cookies_str:
        return _dead_result("No cookies", country_code=country_code)

    cookies_dict = parse_cookies_to_dict(cookies_str)
    if not cookies_dict:
        return _dead_result("Invalid cookies format", country_code=country_code)

    has_session = any(key.lower() in {"sid_tt", "sessionid", "sessionid_ss", "sid_guard"} for key in cookies_dict)
    if not has_session:
        return _dead_result("Missing session cookies", country_code=country_code)

    proxy_url, proxy_error = _normalize_proxy_for_aiohttp(getattr(account, "proxy", None))
    if proxy_error:
        return _inconclusive_result(proxy_error, country_code=country_code)

    cookie_header = "; ".join(f"{k}={v}" for k, v in cookies_dict.items())
    headers = {
        "User-Agent": getattr(account, "user_agent", None)
        or "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
        "Cookie": cookie_header,
        "Accept": "application/json, text/plain, */*",
        "Accept-Language": "en-US,en;q=0.9",
        "Referer": "https://www.tiktok.com/",
        "Origin": "https://www.tiktok.com",
    }

    connector = None
    request_proxy = proxy_url
    if proxy_url and proxy_url.lower().startswith(("socks4://", "socks5://")):
        try:
            from aiohttp_socks import ProxyConnector
            connector = ProxyConnector.from_url(proxy_url)
            request_proxy = None
        except Exception as exc:
            return _inconclusive_result(
                f"socks_proxy_connector_failed:{str(exc)[:80]}",
                country_code=country_code,
            )

    timeout = aiohttp.ClientTimeout(
        total=12 if proxy_url else 8,
        connect=6 if proxy_url else 4,
        sock_read=8 if proxy_url else 5,
    )
    network_errors: List[str] = []

    async def _get(session: aiohttp.ClientSession, url: str, *, allow_redirects: bool = False):
        kwargs = {
            "headers": headers,
            "allow_redirects": allow_redirects,
        }
        if request_proxy:
            kwargs["proxy"] = request_proxy
        return session.get(url, **kwargs)

    try:
        async with aiohttp.ClientSession(timeout=timeout, connector=connector) as session:
            passport_url = "https://www.tiktok.com/passport/web/account/info/"
            try:
                async with await _get(session, passport_url, allow_redirects=False) as resp:
                    location = (resp.headers.get("location") or "").lower()
                    if resp.status in (301, 302, 303, 307, 308) and ("login" in location or "signup" in location):
                        return _dead_result(f"Redirected to login (HTTP {resp.status})", country_code=country_code)
                    if resp.status in (401, 403):
                        return _dead_result(f"Auth failed (HTTP {resp.status})", country_code=country_code)
                    if resp.status == 200:
                        try:
                            data = await resp.json(content_type=None)
                        except Exception:
                            data = None
                        if isinstance(data, dict):
                            message = str(data.get("message") or "").lower()
                            user_data = data.get("data") if isinstance(data.get("data"), dict) else {}
                            username = user_data.get("username") or user_data.get("user_id_str")
                            if message == "success" and (username or user_data.get("user_id")):
                                logger.info("[VALIDATE] Account %s: VALID (passport API)", getattr(account, "id", "?"))
                                return _valid_result(username=username, country_code=country_code)
                            if message and message != "success":
                                return _dead_result(
                                    f"Not authenticated: {data.get('message')}",
                                    country_code=country_code,
                                )
                    elif resp.status in (429, 503):
                        return _inconclusive_result(f"HTTP challenge/rate-limit {resp.status}", country_code=country_code)
            except (aiohttp.ClientError, asyncio.TimeoutError) as exc:
                network_errors.append(f"passport:{str(exc)[:80]}")

            try:
                async with await _get(session, "https://www.tiktok.com/foryou", allow_redirects=True) as resp:
                    if resp.status in (401, 403):
                        return _dead_result(f"Auth failed on page (HTTP {resp.status})", country_code=country_code)
                    if resp.status in (429, 503):
                        return _inconclusive_result(f"HTTP challenge/rate-limit {resp.status}", country_code=country_code)
                    try:
                        final_url = str(resp.url).lower()
                        if "login" in final_url or "signup" in final_url:
                            return _dead_result("Redirected to login", country_code=country_code)
                    except Exception:
                        pass
                    if resp.status == 200:
                        html = await resp.text(errors="ignore")
                        logged_out = _html_has_logged_out_marker(html)
                        if logged_out:
                            return _dead_result(f"Not logged in (page marker: {logged_out})", country_code=country_code)
                        logged_in = _html_has_logged_in_marker(html)
                        if logged_in:
                            logger.info(
                                "[VALIDATE] Account %s: VALID (page marker %s)",
                                getattr(account, "id", "?"),
                                logged_in[:40],
                            )
                            return _valid_result(country_code=country_code)
            except (aiohttp.ClientError, asyncio.TimeoutError) as exc:
                network_errors.append(f"foryou:{str(exc)[:80]}")
    except Exception as exc:
        return _inconclusive_result(f"http_probe_error:{str(exc)[:100]}", country_code=country_code)

    if network_errors:
        return _inconclusive_result("; ".join(network_errors)[:180], country_code=country_code)
    return _inconclusive_result("Could not verify session (no definitive auth markers)", country_code=country_code)


async def validate_account_http(account: Account) -> dict:
    """
    Проверяет валидность куков аккаунта через HTTP запрос.
    Также определяет country_code аккаунта из cookies.
    
    Делает запрос к TikTok API с куками и проверяет ответ.
    Быстро (~1-2 сек) и работает в любом async контексте.
    
    Returns:
        dict: {
            "valid": bool,
            "username": str | None,
            "error": str | None,
            "country_code": str | None  # Определенный код страны
        }
    """
    return await _validate_account_http_tri_state(account)



async def validate_accounts_batch(accounts: List[Account], max_concurrent: int = 10) -> List[dict]:
    """
    Валидирует несколько аккаунтов параллельно.
    
    Args:
        accounts: список аккаунтов
        max_concurrent: максимум параллельных проверок
        
    Returns:
        list: результаты валидации для каждого аккаунта
    """
    import asyncio
    
    semaphore = asyncio.Semaphore(max_concurrent)
    
    async def validate_with_semaphore(account):
        async with semaphore:
            result = await validate_account_http(account)
            return {
                "account_id": account.id,
                "label": account.label,
                **result
            }
    
    tasks = [validate_with_semaphore(acc) for acc in accounts]
    return await asyncio.gather(*tasks)


def validate_account_http_sync(account: Account) -> dict:
    """Synchronous tri-state wrapper for non-async maintenance jobs."""
    try:
        running_loop = asyncio.get_running_loop()
    except RuntimeError:
        running_loop = None
    if running_loop and running_loop.is_running():
        return _inconclusive_result("sync_http_validation_called_from_running_loop")

    try:
        return asyncio.run(validate_account_http(account))
    except Exception as exc:
        logger.warning(
            "[VALIDATE] Account %s: sync HTTP validation inconclusive - %s",
            getattr(account, "id", "?"),
            str(exc)[:120],
        )
        return _inconclusive_result(f"sync_http_validation_error:{str(exc)[:100]}")


def quick_validate_account_sync(account: Account) -> bool:
    """
    Синхронная обёртка для быстрой проверки аккаунта.
    Используется в executor.
    
    Args:
        account: аккаунт для проверки
        
    Returns:
        bool: True если аккаунт валидный
    """
    result = validate_account_http_sync(account)
    is_valid = bool(result.get("valid") or result.get("status") == "valid")
    status = str(result.get("status") or "unknown")

    if is_valid:
        logger.info(f"[QUICK_VALIDATE] Account {account.id} ({account.label}): VALID")
    elif status == "inconclusive":
        error = result.get("error", "Unknown")
        logger.info(f"[QUICK_VALIDATE] Account {account.id} ({account.label}): INCONCLUSIVE - {error}")
    else:
        error = result.get("error", "Unknown")
        logger.info(f"[QUICK_VALIDATE] Account {account.id} ({account.label}): INVALID - {error}")

    return is_valid


# ============================================================================
# Функции для парсера аккаунтов (используются в bot/services/account_parser.py)
# ============================================================================


@dataclass
class CookieValidationResult:
    """Результат валидации cookies строки."""
    is_valid: bool
    unique_key: str  # Уникальный ключ для проверки дубликатов
    normalized_cookies: str  # Нормализованная JSON-строка cookies
    matched_set: str  # Какой набор cookies найден (sid_tt+sessionid, sid_guard+msToken, etc.)
    error: Optional[str] = None  # Сообщение об ошибке если is_valid=False
    strength: str = "unknown"  # full | enriched | minimal | dead — нужно ли догенерация


def _assess_cookie_strength_quick(cookies: Dict[str, str]) -> str:
    """Локальная копия assess_cookie_strength.

    Дублируем чтобы не создавать circular import: cookie_enricher уже
    импортирует parse_cookies_to_dict отсюда, а сама эта функция нужна на
    стадии импорта аккаунтов, до того как Playwright вообще запускается.
    """
    keys_lower = {k.lower() for k in cookies.keys()}
    has_full_session = any(k in keys_lower for k in ("sessionid", "sessionid_ss", "sid_tt"))
    has_partial_session = "sid_guard" in keys_lower
    has_ttwid = "ttwid" in keys_lower
    has_csrf = "tt_csrf_token" in keys_lower
    if has_full_session and has_ttwid and has_csrf:
        return "full"
    if has_partial_session and has_ttwid and has_csrf:
        return "enriched"
    if has_partial_session or has_full_session:
        return "minimal"
    return "dead"


def validate_cookies(cookies_raw: str) -> CookieValidationResult:
    """
    Валидирует строку cookies и возвращает результат.
    Используется в парсере аккаунтов для проверки формата cookies.
    
    Args:
        cookies_raw: сырая строка с cookies
        
    Returns:
        CookieValidationResult с результатами валидации
    """
    if not cookies_raw or not cookies_raw.strip():
        return CookieValidationResult(
            is_valid=False,
            unique_key="",
            normalized_cookies="",
            matched_set="",
            error="Cookies string is empty"
        )
    
    try:
        cookies_dict = parse_cookies_to_dict(cookies_raw)
        
        if not cookies_dict:
            return CookieValidationResult(
                is_valid=False,
                unique_key="",
                normalized_cookies="",
                matched_set="",
                error="Invalid cookies format"
            )
        
        # Проверяем наличие ключевых cookies для авторизации
        cookie_names_lower = {k.lower(): k for k in cookies_dict.keys()}
        
        # Валидные комбинации:
        # 1. sid_tt + sessionid (рекомендуется; работает для комментирования)
        # 2. sid_guard + msToken (только для мониторинга; комментирование НЕ работает без sessionid/sid_tt)
        has_sid_tt = "sid_tt" in cookie_names_lower
        has_sessionid = "sessionid" in cookie_names_lower
        has_sid_guard = "sid_guard" in cookie_names_lower
        has_mstoken = "mstoken" in cookie_names_lower
        
        matched_set = ""
        is_valid = False
        
        if has_sid_tt and has_sessionid:
            matched_set = "sid_tt+sessionid"
            is_valid = True
        elif has_sid_guard and has_mstoken:
            matched_set = "sid_guard+msToken"
            is_valid = True
        elif has_sid_tt:
            # Только sid_tt - может работать, но менее надёжно
            matched_set = "sid_tt only"
            is_valid = True
        elif has_sessionid:
            # Только sessionid - может работать
            matched_set = "sessionid only"
            is_valid = True
        
        if not is_valid:
            return CookieValidationResult(
                is_valid=False,
                unique_key="",
                normalized_cookies="",
                matched_set="",
                error="Missing required cookies (need sid_tt+sessionid or sid_guard+msToken)"
            )
        
        # Создаём нормализованную JSON-строку
        normalized_cookies = json.dumps(cookies_dict, ensure_ascii=False, separators=(',', ':'))
        
        # Создаём уникальный ключ на основе ключевых cookies
        key_parts = []
        if has_sid_tt:
            key_parts.append(f"sid_tt:{cookies_dict[cookie_names_lower['sid_tt']][:20]}")
        if has_sessionid:
            key_parts.append(f"sessionid:{cookies_dict[cookie_names_lower['sessionid']][:20]}")
        if has_sid_guard:
            key_parts.append(f"sid_guard:{cookies_dict[cookie_names_lower['sid_guard']][:20]}")
        if has_mstoken:
            key_parts.append(f"msToken:{cookies_dict[cookie_names_lower['mstoken']][:20]}")
        
        unique_key = "|".join(key_parts) if key_parts else ""
        
        return CookieValidationResult(
            is_valid=True,
            unique_key=unique_key,
            normalized_cookies=normalized_cookies,
            matched_set=matched_set,
            strength=_assess_cookie_strength_quick(cookies_dict),
        )
        
    except Exception as e:
        return CookieValidationResult(
            is_valid=False,
            unique_key="",
            normalized_cookies="",
            matched_set="",
            error=f"Validation error: {str(e)[:100]}"
        )


def format_validation_error(validation: CookieValidationResult, line_num: int) -> str:
    """
    Форматирует сообщение об ошибке валидации для отображения пользователю.
    
    Args:
        validation: результат валидации
        line_num: номер строки
        
    Returns:
        str: отформатированное сообщение об ошибке
    """
    error_msg = validation.error or "Unknown error"
    return f"Строка {line_num}: {error_msg}"
