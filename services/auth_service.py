from sqlalchemy.orm import Session
from db.models import User
from config.settings import get_settings
import hmac


def resolve_admin_api_token() -> str | None:
    """Токен для заголовка X-Admin-Token: только ADMIN_API_TOKEN (без fallback на EXECUTOR_API_TOKEN)."""
    cfg = get_settings()
    # Не используем getattr(..., None): у Pydantic v2 отсутствие поля всё равно ломает доступ.
    # AttributeError — старый кэш/класс Settings без поля (после reload или старый процесс).
    try:
        raw = cfg.ADMIN_API_TOKEN
    except AttributeError:
        raw = None
    if raw is not None and str(raw).strip():
        return str(raw).strip()
    return None


def verify_admin_token(token: str) -> bool:
    """Проверяет административный токен (только ADMIN_API_TOKEN, без fallback)."""
    if not token or not str(token).strip():
        return False
    expected = resolve_admin_api_token()
    return bool(expected and hmac.compare_digest(str(token).strip(), expected))


def verify_executor_token(token: str) -> bool:
    """Проверяет токен executor'а."""
    expected = get_settings().EXECUTOR_API_TOKEN
    return bool(token and expected and hmac.compare_digest(str(token), str(expected)))


def is_owner(db: Session, telegram_id: int) -> bool:
    """Проверяет, является ли пользователь владельцем."""
    settings = get_settings()
    if settings.OWNER_TELEGRAM_ID is None:
        return False
    user = db.query(User).filter(User.telegram_id == telegram_id).first()
    if not user:
        return False
    return user.role == "owner" or user.telegram_id == settings.OWNER_TELEGRAM_ID


def is_admin(db: Session, telegram_id: int) -> bool:
    """Проверяет, является ли пользователь админом или владельцем."""
    settings = get_settings()
    user = db.query(User).filter(User.telegram_id == telegram_id).first()
    if not user:
        return False
    return user.role in ("admin", "owner") or (
        settings.OWNER_TELEGRAM_ID and user.telegram_id == settings.OWNER_TELEGRAM_ID
    )
