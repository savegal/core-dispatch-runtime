"""
ИСПРАВЛЕНО: Сервис для сохранения обновленных cookies после успешных действий.

КРИТИЧНО: TikTok постоянно ротирует sessionid и токены безопасности.
Если не сохранять обновленные cookies, при следующем использовании аккаунт
будет использовать старые cookies → Replay Attack → Login Required.
"""
import json
import logging
from typing import List, Dict, Any, Optional
from pathlib import Path
from sqlalchemy.orm import Session, object_session
from db.models import Account
from config.settings import get_settings

logger = logging.getLogger("services.cookie_persistence_service")
settings = get_settings()


def _inject_web_identity_records(
    records: List[Dict[str, Any]],
    *,
    account: Optional[Account] = None,
    context: Any = None,
    previous: Optional[Dict[str, str]] = None,
) -> tuple[List[Dict[str, Any]], Dict[str, str]]:
    """Attach persisted/cached (device_id, WebIdLastTime, odinId) metadata records for DB storage."""
    from executor.http.playwright_http_client import get_cached_web_identity

    account_id = getattr(account, "id", None) or getattr(context, "_pool_account_id", None)
    cached = get_cached_web_identity(account_id, context=context, account=account)
    prev = previous if isinstance(previous, dict) else {}

    device_id = str(cached.get("device_id") or prev.get("device_id") or prev.get("device-id") or "").strip()
    web_id_last_time = str(
        cached.get("web_id_last_time")
        or prev.get("WebIdLastTime")
        or prev.get("web_id_last_time")
        or ""
    ).strip()
    odin_id = str(
        cached.get("odin_id")
        or prev.get("odinId")
        or prev.get("odin_id")
        or prev.get("odin_tt")
        or ""
    ).strip()

    identity_map: Dict[str, str] = {}
    if device_id:
        identity_map["device_id"] = device_id
    if web_id_last_time:
        identity_map["WebIdLastTime"] = web_id_last_time
    if odin_id:
        identity_map["odinId"] = odin_id

    if not identity_map:
        return list(records or []), identity_map

    meta_lower = {k.lower() for k in identity_map}
    filtered = [
        dict(r)
        for r in (records or [])
        if isinstance(r, dict) and str(r.get("name") or "").strip().lower() not in meta_lower
    ]
    for name, value in identity_map.items():
        filtered.append(
            {
                "name": name,
                "value": value,
                "domain": ".tiktok.com",
                "path": "/",
                "secure": True,
                "httpOnly": False,
            }
        )
    return filtered, identity_map


async def synchronize_browser_cookies(
    context: Any, account: Optional[Account] = None, db: Optional[Session] = None,
) -> tuple[Dict[str, str], bool]:
    """Restore missing browser auth aliases and save fresh page cookies + JS web identity."""
    from services.cookie_validator import (
        get_cookies_for_account,
        normalize_cookie_records,
        normalize_cookies_for_browser,
        parse_cookies_to_dict,
    )

    records = await context.cookies()
    names = {c["name"] for c in records}
    restored = [c for c in normalize_cookies_for_browser(records, drop_stale_antifraud=False)
                if c["name"] in {"sessionid", "sid_tt", "sessionid_ss"} and c["name"] not in names]
    if restored:
        await context.add_cookies(restored)
    previous = parse_cookies_to_dict(get_cookies_for_account(account)) if account is not None else {}
    merged_records, identity_map = _inject_web_identity_records(
        [*records, *restored],
        account=account,
        context=context,
        previous=previous,
    )
    cookies = normalize_cookie_records(merged_records)
    for k, v in identity_map.items():
        cookies[k] = v
    try:
        saved = save_warmed_cookies_for_account(account, cookies, db, context=context)
    except Exception:
        logger.warning("Could not persist warmed browser cookies for account %s", getattr(account, "id", None), exc_info=True)
        saved = False
    return cookies, saved


def save_warmed_cookies_for_account(
    account: Optional[Account],
    cookies: Dict[str, str],
    db: Optional[Session] = None,
    *,
    context: Any = None,
) -> bool:
    """Persist generated/rotated browser linkage cookies and JS web identity after page warmup."""
    if account is None:
        return False
    from services.cookie_validator import (
        get_cookies_for_account, parse_cookies_to_dict, normalize_cookies_for_browser,
    )

    previous = parse_cookies_to_dict(get_cookies_for_account(account))
    records = getattr(cookies, "records", None)
    if records is None:
        if cookies:
            records = [{"name": name, "value": value} for name, value in cookies.items()]
        else:
            records = getattr(previous, "records", None) or [
                {"name": name, "value": value} for name, value in previous.items()
            ]
    records, identity_map = _inject_web_identity_records(
        records,
        account=account,
        context=context,
        previous=previous,
    )
    effective_cookies = dict(previous)
    effective_cookies.update(cookies or {})
    effective_cookies.update(identity_map)

    tracked_names = (
        "ttwid",
        "tt_csrf_token",
        "odin_tt",
        "msToken",
        "s_v_web_id",
        "device_id",
        "WebIdLastTime",
        "odinId",
    )
    if not any(
        effective_cookies.get(name) and effective_cookies[name] != previous.get(name)
        for name in tracked_names
    ):
        return False
    if db is None and isinstance(account, Account):
        db = object_session(account)
    if db is None:
        return False
    normalized_records = normalize_cookies_for_browser(records, drop_stale_antifraud=False)
    saved = save_cookies_to_db(db, account.id, normalized_records)
    if getattr(account, "cookie_file", None):
        save_cookies_to_file(account.cookie_file, normalized_records)
    if saved:
        from datetime import datetime, timezone
        now_utc = datetime.now(timezone.utc)
        cookies_str = cookies_to_string(normalized_records)
        if cookies_str and hasattr(account, "cookies"):
            account.cookies = cookies_str
        setattr(account, "_cookies_warmed_at", now_utc)
        if hasattr(account, "last_validation_ok"):
            account.last_validation_ok = now_utc
    return saved


def cookies_to_string(cookies: List[Dict[str, Any]]) -> str:
    """
    Конвертирует список cookies из Playwright в строку для сохранения в БД.
    
    Args:
        cookies: Список cookies в формате Playwright
        
    Returns:
        str: JSON строка с cookies
    """
    if not cookies:
        return ""
    
    try:
        # Конвертируем в JSON массив
        return json.dumps(cookies, ensure_ascii=False)
    except Exception as e:
        logger.warning(f"Error converting cookies to string: {e}")
        return ""


def save_cookies_to_db(db: Session, account_id: int, cookies: List[Dict[str, Any]]) -> bool:
    """
    ИСПРАВЛЕНО: Сохраняет обновленные cookies в БД для аккаунта.
    
    КРИТИЧНО: Использует SELECT FOR UPDATE для предотвращения race conditions.
    
    Args:
        db: SQLAlchemy сессия
        account_id: ID аккаунта
        cookies: Список cookies в формате Playwright
        
    Returns:
        bool: True если сохранение успешно
    """
    try:
        # ИСПРАВЛЕНО: Используем SELECT FOR UPDATE для атомарной операции
        account = db.query(Account).filter(Account.id == account_id).with_for_update().first()
        if not account:
            logger.warning(f"Account {account_id} not found for cookie persistence")
            return False
        
        # Конвертируем cookies в строку
        cookies_str = cookies_to_string(cookies)
        if not cookies_str:
            logger.debug(f"No cookies to save for account {account_id}")
            return False
        
        # Сохраняем в БД
        account.cookies = cookies_str
        db.commit()
        
        logger.debug(f"Saved updated cookies for account {account_id}")
        return True
    except Exception as e:
        logger.error(f"Error saving cookies for account {account_id}: {e}", exc_info=True)
        db.rollback()
        return False


def save_cookies_to_file(cookie_file_path: Optional[str], cookies: List[Dict[str, Any]]) -> bool:
    """
    ИСПРАВЛЕНО: Сохраняет обновленные cookies в файл.
    
    Args:
        cookie_file_path: Путь к файлу с cookies
        cookies: Список cookies в формате Playwright
        
    Returns:
        bool: True если сохранение успешно
    """
    if not cookie_file_path:
        return False
    
    try:
        # Only relative paths inside COOKIES_DIR are accepted.  Use resolved
        # paths instead of string-prefix checks so Windows drive paths and
        # sibling directories cannot escape the cookie directory.
        cookie_file_str = cookie_file_path.strip()
        candidate = Path(cookie_file_str)
        if (
            not cookie_file_str
            or candidate.is_absolute()
            or ".." in candidate.parts
        ):
            logger.warning(f"Unsafe cookie file path: {cookie_file_str}")
            return False

        cookies_dir = Path(settings.COOKIES_DIR).resolve()
        cookie_file = (cookies_dir / candidate).resolve()
        if not cookie_file.is_relative_to(cookies_dir):
            logger.warning(f"Cookie file path outside cookies directory: {cookie_file_str}")
            return False

        # Сохраняем в файл
        cookie_file.parent.mkdir(parents=True, exist_ok=True)
        
        cookies_str = cookies_to_string(cookies)
        if cookies_str:
            cookie_file.write_text(cookies_str, encoding='utf-8')
            logger.debug(f"Saved updated cookies to file: {cookie_file}")
            return True
        return False
    except Exception as e:
        logger.warning(f"Error saving cookies to file {cookie_file_path}: {e}")
        return False


async def save_cookies_after_action(
    context,
    account: Account,
    db: Optional[Session] = None
) -> bool:
    """
    ИСПРАВЛЕНО: Сохраняет обновленные cookies после успешного действия.
    
    КРИТИЧНО: Вызывать после каждого успешного действия (post_comment, post_reply, like_comment).
    
    Args:
        context: BrowserContext из Playwright
        account: Аккаунт из БД
        db: SQLAlchemy сессия (опционально, если None - сохраняет только в файл)
        
    Returns:
        bool: True если сохранение успешно
    """
    try:
        # Получаем актуальные cookies из контекста
        cookies = await context.cookies()
        if not cookies:
            logger.debug(f"No cookies to save for account {account.id}")
            return False

        try:
            from services.cookie_validator import get_cookies_for_account, parse_cookies_to_dict
            previous = parse_cookies_to_dict(get_cookies_for_account(account))
        except Exception:
            previous = {}
        cookies, _ = _inject_web_identity_records(
            cookies,
            account=account,
            context=context,
            previous=previous,
        )

        if db is None and isinstance(account, Account):
            db = object_session(account)

        # Сохраняем в БД (если сессия предоставлена)
        saved_to_db = False
        if db:
            saved_to_db = save_cookies_to_db(db, account.id, cookies)
        
        # Сохраняем в файл (если указан cookie_file)
        saved_to_file = False
        if account.cookie_file:
            saved_to_file = save_cookies_to_file(account.cookie_file, cookies)
        
        if saved_to_db or saved_to_file:
            cookies_str = cookies_to_string(cookies)
            if cookies_str and hasattr(account, "cookies"):
                account.cookies = cookies_str
            logger.debug(f"Cookies persisted for account {account.id} (db={saved_to_db}, file={saved_to_file})")
            return True
        
        return False
    except Exception as e:
        logger.warning(f"Error persisting cookies for account {account.id}: {e}")
        return False
