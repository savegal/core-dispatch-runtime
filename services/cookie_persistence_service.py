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
from sqlalchemy.orm import Session
from db.models import Account
from config.settings import get_settings

logger = logging.getLogger("services.cookie_persistence_service")
settings = get_settings()


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
        
        # Сохраняем в БД (если сессия предоставлена)
        saved_to_db = False
        if db:
            saved_to_db = save_cookies_to_db(db, account.id, cookies)
        
        # Сохраняем в файл (если указан cookie_file)
        saved_to_file = False
        if account.cookie_file:
            saved_to_file = save_cookies_to_file(account.cookie_file, cookies)
        
        if saved_to_db or saved_to_file:
            logger.debug(f"Cookies persisted for account {account.id} (db={saved_to_db}, file={saved_to_file})")
            return True
        
        return False
    except Exception as e:
        logger.warning(f"Error persisting cookies for account {account.id}: {e}")
        return False
