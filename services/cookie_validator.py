"""
HTTP-based cookie validation for TikTok accounts.
Uses aiohttp instead of Playwright - works in any async context.
"""
import aiohttp
from config.settings import get_settings
import asyncio
import logging
import json
import math
import re
from datetime import datetime, timezone
from typing import Dict, List, Any, Optional, Tuple
from dataclasses import dataclass
from db.models import Account
from executor.browser_identity import desktop_user_agent, sec_ch_ua

logger = logging.getLogger("services.cookie_validator")


class NormalizedCookies(dict):
    """HTTP name/value view with the original domain-scoped browser records."""

    def __init__(self, values=(), *, records=()):
        super().__init__(values)
        self.records = [dict(record) for record in records]


_SYNTHETIC_COOKIE_EXPIRY = 2_000_000_000


def _cookie_expiry(cookie: dict) -> float:
    # Playwright's current attributes take precedence over export metadata.
    for key in ("expires", "expirationDate", "expiration"):
        try:
            expiry = float(cookie[key])
            if math.isfinite(expiry):
                return expiry
        except (KeyError, TypeError, ValueError):
            continue
    return 0.0


def _sid_guard_parts(value: str) -> Tuple[str, int]:
    parts = re.split(r"\||%7c", value, flags=re.IGNORECASE)
    try:
        timestamp = int(parts[1]) if len(parts) > 1 else -1
    except ValueError:
        timestamp = -1
    return parts[0], timestamp


def _restore_session_cookies(cookies: Dict[str, str]) -> Dict[str, str]:
    if any(name.lower() in {"sessionid", "sid_tt", "sessionid_ss"} for name in cookies):
        return cookies
    guard = next((value for name, value in cookies.items() if name.lower() == "sid_guard"), "")
    prefix, timestamp = _sid_guard_parts(guard)
    if timestamp <= 0 or not re.fullmatch(r"[0-9a-fA-F]{32}", prefix):
        return cookies
    for name in ("sessionid", "sid_tt", "sessionid_ss"):
        cookies[name] = prefix
    return cookies


def normalize_cookie_records(records: List[dict]) -> NormalizedCookies:
    """Select web API values without discarding browser domains or attributes."""
    result = NormalizedCookies()
    best_rank = {}
    scoped_records = {}
    scoped_ranks = {}
    for index, cookie in enumerate(records):
        if not isinstance(cookie, dict):
            continue
        name = cookie.get("name") or cookie.get("Name") or cookie.get("key")
        value = cookie.get("value", cookie.get("Value"))
        if not name or value is None:
            continue
        name, value = str(name), str(value)
        record = {**cookie, "name": name, "value": value}
        expiry = _cookie_expiry(cookie)
        expiry_rank = (expiry < _SYNTHETIC_COOKIE_EXPIRY, expiry if 0 < expiry < _SYNTHETIC_COOKIE_EXPIRY else 0)
        rank = (_sid_guard_parts(value)[1], *expiry_rank) if name.lower() == "sid_guard" else expiry_rank
        domain = str(cookie.get("domain") or "").strip().lower().lstrip(".")
        if name.lower() in {"mstoken", "ttwid", "tt_csrf_token"}:
            # Exported 2038 expirations are not freshness evidence. Within the
            # web domain prefer the latest real/session cookie, even if its
            # lifetime is shorter than the previously stored value.
            rank = (domain in {"tiktok.com", "www.tiktok.com"}, expiry_rank[0], index)
        scope = (name, cookie.get("domain", ".tiktok.com"), cookie.get("path", "/"))
        if scope not in scoped_records or rank >= scoped_ranks[scope]:
            # Preserve the selected record's position for subsequent passes.
            # Replacing a dict value alone would retain its old list position.
            scoped_records.pop(scope, None)
            scoped_records[scope] = record
            scoped_ranks[scope] = rank
        if name.lower() == "mstoken":
            # Subdomain cookies (ads/shop/livecenter, etc.) do not apply to www.
            if domain and domain not in {"www.tiktok.com", "tiktok.com"}:
                continue
        if name not in result or rank >= best_rank[name]:
            result[name] = value
            best_rank[name] = rank
    result.records = list(scoped_records.values())
    return _restore_session_cookies(result)


def account_has_fresh_antifraud_cookies(account: Any, *, max_age_minutes: float = 30.0) -> bool:
    """Return True when the account's cookies were validated or used within max_age_minutes."""
    if account is None:
        return False
    now_utc = datetime.now(timezone.utc)
    max_age_seconds = max(0.0, float(max_age_minutes) * 60.0)
    for attr in ("last_validation_ok", "last_used_at", "_cookies_warmed_at"):
        ts = getattr(account, attr, None)
        if not isinstance(ts, datetime):
            continue
        if ts.tzinfo is None:
            ts = ts.replace(tzinfo=timezone.utc)
        age = (now_utc - ts).total_seconds()
        if 0.0 <= age <= max_age_seconds:
            return True
    return False


def normalize_cookies_for_browser(
    records: List[dict],
    *,
    drop_stale_antifraud: bool = True,
    preserve_fresh_tokens: bool = False,
) -> List[dict]:
    """Build startup cookies; pass False when persisting a warmed live context."""
    cookies = normalize_cookie_records(records)
    selected_guard = next((c for c in cookies.records
                           if c["name"] == "sid_guard" and c["value"] == cookies.get("sid_guard")), {})
    source = list(cookies.records)
    names = {c["name"] for c in source}
    for name, value in cookies.items():
        if name not in names:
            scope = dict(selected_guard)
            # New compatibility aliases must reach the web API even when the
            # exported guard is host-only on tiktok.com or on a different path.
            if scope.get("domain") not in {".tiktok.com", "www.tiktok.com", ".www.tiktok.com"}:
                scope["domain"] = ".tiktok.com"
            source.append({**scope, "name": name, "value": value, "path": "/", "secure": True, "httpOnly": True})

    stale_antifraud_names = {
        "_waftokenid",
        "csrf_session_id",
        "x-web-secsdk-uid",
        "perf_feed_cache",
        "tt_session_tlb_tag",
        "device_id",
        "device-id",
        "webidlasttime",
        "web_id_last_time",
        "odinid",
        "odin_id",
    }
    if not preserve_fresh_tokens:
        stale_antifraud_names.add("tt_csrf_token")

    result = []
    for record in source:
        name = record["name"]
        if drop_stale_antifraud and name.lower() in stale_antifraud_names:
            continue
        if drop_stale_antifraud and name.lower() == "mstoken" and _cookie_expiry(record) >= _SYNTHETIC_COOKIE_EXPIRY:
            continue
        if name.lower() in {"mstoken", "sid_guard"} and record["value"] != cookies.get(name):
            continue
        domain = str(record.get("domain") or ".tiktok.com").strip().lower()
        if name.lower() == "mstoken" and domain.lstrip(".") not in {"tiktok.com", "www.tiktok.com"}:
            continue
        # Whitelist browser attributes: hostOnly/session/export fields are not
        # accepted by add_cookies; domain notation already encodes host scope.
        cookie = {
            "name": name, "value": record["value"], "domain": domain,
            "path": record.get("path") or "/",
            "secure": bool(record.get("secure", True)),
            "httpOnly": bool(record.get("httpOnly", name in {"sid_guard", "sessionid", "sid_tt", "sessionid_ss"})),
        }
        same_site = str(record.get("sameSite") or "").lower()
        if same_site in {"strict", "lax", "none", "no_restriction"}:
            cookie["sameSite"] = {"strict": "Strict", "lax": "Lax", "none": "None", "no_restriction": "None"}[same_site]
        if not record.get("session"):
            expiry = record.get("expires", record.get("expirationDate", record.get("expiration")))
            try:
                expiry = float(expiry)
                if math.isfinite(expiry):
                    cookie["expires"] = expiry if expiry > 0 else -1
            except (TypeError, ValueError):
                pass
        result.append(cookie)
    return result


def prepare_cookies_for_context(
    account: Any,
    raw_cookies: Optional[str] = None,
    *,
    parse_fn: Optional[Any] = None,
    max_age_minutes: float = 30.0,
) -> List[dict]:
    """Prepare browser context cookies, retaining fresh ttwid/tt_csrf_token (<30m) and seeding web identity."""
    cookies_source = raw_cookies if raw_cookies is not None else get_cookies_for_account(account)
    try:
        from executor.http.playwright_http_client import cache_web_identity, extract_web_identity_from_cookies
        if isinstance(cookies_source, list):
            raw_map = {
                str(r.get("name")): str(r.get("value"))
                for r in cookies_source
                if isinstance(r, dict) and r.get("name") and r.get("value") is not None
            }
            extracted = extract_web_identity_from_cookies(raw_map)
        else:
            extracted = extract_web_identity_from_cookies(cookies_source)
        if extracted:
            cache_web_identity(getattr(account, "id", None), extracted, account=account)
    except Exception:
        pass
    preserve_fresh = account_has_fresh_antifraud_cookies(account, max_age_minutes=max_age_minutes)
    if isinstance(cookies_source, list):
        records = [dict(r) for r in cookies_source if isinstance(r, dict)]
        return normalize_cookies_for_browser(
            records,
            drop_stale_antifraud=True,
            preserve_fresh_tokens=preserve_fresh,
        )
    cookies_str = str(cookies_source or "")
    records: List[dict] = []
    if parse_fn is not None and cookies_str:
        records = list(parse_fn(cookies_str) or [])
    elif cookies_str:
        parsed_dict = parse_cookies_to_dict(cookies_str)
        records = list(
            getattr(parsed_dict, "records", None)
            or [
                {"name": k, "value": v, "domain": ".tiktok.com", "path": "/"}
                for k, v in parsed_dict.items()
            ]
        )
    if preserve_fresh and cookies_str:
        try:
            raw_data = json.loads(cookies_str)
            raw_records: List[dict] = []
            if isinstance(raw_data, list):
                raw_records = [r for r in raw_data if isinstance(r, dict)]
            elif isinstance(raw_data, dict):
                if isinstance(raw_data.get("cookies"), list):
                    raw_records = [r for r in raw_data["cookies"] if isinstance(r, dict)]
                else:
                    raw_records = [
                        {"name": str(k), "value": str(v), "domain": ".tiktok.com", "path": "/"}
                        for k, v in raw_data.items()
                        if k and v is not None and not isinstance(v, (dict, list))
                    ]
            existing_names = {str(r.get("name") or "").lower() for r in records if isinstance(r, dict)}
            for r in raw_records:
                r_name = str(r.get("name") or "").lower()
                if r_name in {"ttwid", "tt_csrf_token", "odin_tt", "s_v_web_id", "mstoken"} and r_name not in existing_names:
                    records.append(r)
        except Exception:
            pass
    return normalize_cookies_for_browser(
        records,
        drop_stale_antifraud=True,
        preserve_fresh_tokens=preserve_fresh,
    )


def parse_cookies_to_dict(raw: str) -> Dict[str, str]:
    """
    Парсит cookies из строки в словарь {name: value}.
    
    Поддерживаемые форматы:
    1. JSON-объект: {"sid_tt": "xxx", "sessionid": "yyy"}
    2. JSON-массив: [{"name": "sid_tt", "value": "xxx"}, ...]
       Поддерживает полный формат браузерных расширений с дополнительными полями.
       msToken выбирается для веб-домена, sid_guard — по timestamp внутри значения.
       Исходные домены и атрибуты доступны в NormalizedCookies.records.
    3. Inline: sessionid=xxx; sid_tt=yyy
    """
    if not raw or not raw.strip():
        return {}
    
    raw = raw.strip()
    result = {}
    
    try:
        data = json.loads(raw)
        
        if isinstance(data, list):
            return normalize_cookie_records(data)
        
        elif isinstance(data, dict):
            # Check for nested cookies key
            if "cookies" in data and isinstance(data["cookies"], list):
                return parse_cookies_to_dict(json.dumps(data["cookies"]))
            
            # Simple key-value format
            for key, value in data.items():
                if key and value is not None and not isinstance(value, (dict, list)):
                    result[str(key)] = str(value)
        
        return _restore_session_cookies(result)
    
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
    
    return _restore_session_cookies(result)


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
    user_id: Optional[str] = None,
    country_code: Optional[str] = None,
) -> dict:
    result = _result(
        status="valid",
        valid=True,
        username=username,
        country_code=country_code,
        definitive=True,
    )
    result["user_id"] = user_id
    return result


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


async def _validate_account_http_tri_state(account: Account, *, strict_passport: bool = False) -> dict:
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

    has_session = any(key.lower() in {"sid_tt", "sessionid", "sessionid_ss", "sid_guard"} and str(value).strip() for key, value in cookies_dict.items())
    if not has_session:
        return _dead_result("Missing session cookies", country_code=country_code)

    from services.dedicated_proxy_service import get_current_dedicated_proxy
    dedicated = get_current_dedicated_proxy()
    proxy_url, proxy_error = _normalize_proxy_for_aiohttp(
        dedicated.proxy if dedicated else getattr(account, "proxy", None) if get_settings().USE_PROXY else None
    )
    if proxy_error:
        return _inconclusive_result(proxy_error, country_code=country_code)

    from executor.http.request_profile import _build_accept_language, resolve_request_geo
    geo = resolve_request_geo(
        runtime_profile={
            "proxy_in_use": bool(proxy_url),
            "proxy_country_code": getattr(account, "proxy_country_code", None),
            "account_country_code": getattr(account, "country_code", None) or country_code,
        },
        cookies=cookies_dict,
    )
    primary_lang = geo.get("browser_language")
    accept_language = (
        _build_accept_language(
            primary_lang,
            [primary_lang.split("-")[0], "en"] if primary_lang and "-" in primary_lang and not primary_lang.lower().startswith("en-") else (["en"] if primary_lang else None),
        )
        or "en-US,en;q=0.9"
    )

    cookie_header = "; ".join(f"{k}={v}" for k, v in cookies_dict.items())
    headers = {
        "User-Agent": desktop_user_agent(),
        "sec-ch-ua": sec_ch_ua(),
        "sec-ch-ua-mobile": "?0",
        "sec-ch-ua-platform": '"Windows"',
        "Cookie": cookie_header,
        "Accept": "application/json, text/plain, */*",
        "Accept-Language": accept_language,
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
                    if resp.status == 401:
                        return _dead_result(f"Auth failed (HTTP {resp.status})", country_code=country_code)
                    if resp.status == 403:
                        return _inconclusive_result("HTTP access denied/challenge 403", country_code=country_code)
                    if resp.status == 200:
                        try:
                            data = await resp.json(content_type=None)
                        except Exception:
                            data = None
                        if isinstance(data, dict):
                            message = str(data.get("message") or "").lower()
                            user_data = data.get("data") if isinstance(data.get("data"), dict) else {}
                            username = str(user_data.get("username") or user_data.get("unique_id") or "").strip() or None
                            user_id = str(user_data.get("user_id_str") or user_data.get("user_id") or user_data.get("uid") or "").strip() or None
                            for region_key in ("region", "country_code", "priority_region", "store_region", "iso_country_code"):
                                raw_region = user_data.get(region_key) or data.get(region_key)
                                if raw_region:
                                    norm_region = str(raw_region).strip().upper()
                                    if re.fullmatch(r"[A-Z]{2}", norm_region):
                                        country_code = norm_region
                                        break
                            if country_code and account is not None and not getattr(account, "country_code", None):
                                try:
                                    account.country_code = country_code
                                    from sqlalchemy.orm import object_session
                                    sess = object_session(account)
                                    if sess is not None:
                                        sess.add(account)
                                        sess.commit()
                                except Exception:
                                    pass
                            if message == "success" and (username or user_id):
                                logger.info("[VALIDATE] Account %s: VALID (passport API)", getattr(account, "id", "?"))
                                return _valid_result(username=username, user_id=user_id, country_code=country_code)
                            if message and message != "success":
                                # TikTok also returns generic `error` for challenges and
                                # incomplete HTTP context; in strict HTTP-first mode,
                                # unconfirmed passport responses immediately skip browser launch.
                                if message in {"not logged in", "login required", "session expired", "invalid session", "not authenticated"}:
                                    return _dead_result(f"Not authenticated: {message}", country_code=country_code)
                                if strict_passport:
                                    return _dead_result("Passport response did not confirm authentication", country_code=country_code)
                                return _inconclusive_result("Passport response did not confirm authentication", country_code=country_code)
                            if strict_passport:
                                return _dead_result("Passport response did not confirm authentication", country_code=country_code)
                    elif resp.status in (429, 503):
                        return _inconclusive_result(f"HTTP challenge/rate-limit {resp.status}", country_code=country_code)
            except (aiohttp.ClientError, asyncio.TimeoutError) as exc:
                network_errors.append(f"passport:{str(exc)[:80]}")

    except Exception as exc:
        return _inconclusive_result(f"http_probe_error:{str(exc)[:100]}", country_code=country_code)

    if network_errors:
        return _inconclusive_result("; ".join(network_errors)[:180], country_code=country_code)
    return _inconclusive_result("Could not verify session (no definitive auth markers)", country_code=country_code)


def get_http_preflight_concurrency_limit(use_proxy: Optional[bool] = None) -> int:
    """Return bounded concurrency limit for HTTP preflight checks (3 with proxy, 2 without proxy)."""
    if use_proxy is None:
        try:
            use_proxy = bool(get_settings().USE_PROXY)
        except Exception:
            use_proxy = False
    return 3 if use_proxy else 2


async def validate_account_http(account: Account, *, strict_passport: bool = False) -> dict:
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
    res = await _validate_account_http_tri_state(account, strict_passport=strict_passport)
    detected_cc = res.get("country_code")
    if detected_cc and account is not None and not getattr(account, "country_code", None):
        try:
            account.country_code = str(detected_cc).strip().upper()
        except Exception:
            pass
    return res


async def validate_accounts_batch(accounts: List[Account], max_concurrent: Optional[int] = None) -> List[dict]:
    """
    Валидирует несколько аккаунтов параллельно.
    
    Args:
        accounts: список аккаунтов
        max_concurrent: максимум параллельных проверок (по умолчанию 3 с прокси, 2 без прокси)
        
    Returns:
        list: результаты валидации для каждого аккаунта
    """
    import asyncio
    
    default_limit = get_http_preflight_concurrency_limit()
    effective_limit = default_limit if max_concurrent is None else max(1, min(int(max_concurrent), default_limit))
    semaphore = asyncio.Semaphore(effective_limit)
    
    async def validate_with_semaphore(account):
        async with semaphore:
            try:
                result = await validate_account_http(account)
            except Exception as exc:
                result = _inconclusive_result(f"validation_exception:{type(exc).__name__}")
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
