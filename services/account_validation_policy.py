"""
Единая политика валидации аккаунтов.

ИСПРАВЛЕНО: Вынесена единая логика обработки валидационных неудач,
чтобы избежать дублирования кода в executor и auto_account_validator.
"""
import logging
from datetime import datetime, timezone
from sqlalchemy.orm import Session
from db.models import Account
from services.account_service import mark_dead

logger = logging.getLogger("services.account_validation_policy")

# Порог для пометки аккаунта как dead после валидационных неудач
VALIDATION_FAIL_THRESHOLD = 3


def handle_validation_failure(
    db: Session,
    account: Account,
    task_id: int | None = None,
    log_func=None
) -> bool:
    """
    Обрабатывает неудачную валидацию аккаунта.
    
    Единая политика:
    - Увеличивает validation_fail_count
    - Если validation_fail_count >= VALIDATION_FAIL_THRESHOLD (3) → mark_dead
    - Иначе просто логирует предупреждение
    
    Args:
        db: SQLAlchemy сессия
        account: Аккаунт из БД (должен быть уже загружен с SELECT FOR UPDATE если нужно)
        task_id: Опциональный ID задачи для логирования
        log_func: Опциональная функция для логирования (например, log из log_service)
        
    Returns:
        bool: True если аккаунт помечен как dead, False если просто увеличено validation_fail_count
    """
    now = datetime.now(timezone.utc)
    
    # Увеличиваем счётчик неудач
    account.validation_fail_count = (account.validation_fail_count or 0) + 1
    account.last_checked_at = now
    
    # Проверяем порог
    if account.validation_fail_count >= VALIDATION_FAIL_THRESHOLD:
        # Помечаем как dead после 3 неудачных проверок подряд
        logger.warning(
            f"Account {account.id} ({account.label[:20] if account.label else 'N/A'}) "
            f"failed validation {account.validation_fail_count} times - marking as dead"
        )
        mark_dead(db, account.id)
        
        if task_id and log_func:
            log_func(db, task_id, account.id, "Аккаунт помечен как dead (3 неудачные проверки)")
        
        return True
    else:
        # Еще не достигли порога - просто логируем
        logger.warning(
            f"Account {account.id} ({account.label[:20] if account.label else 'N/A'}) "
            f"failed validation ({account.validation_fail_count}/{VALIDATION_FAIL_THRESHOLD}) - will retry"
        )
        
        if task_id and log_func:
            log_func(db, task_id, account.id, f"Проверка неудачна ({account.validation_fail_count}/{VALIDATION_FAIL_THRESHOLD})")
        
        return False


def handle_validation_success(
    db: Session,
    account: Account
) -> None:
    """
    Обрабатывает успешную валидацию аккаунта.
    
    Единая политика:
    - Сбрасывает validation_fail_count в 0
    - Обновляет last_validation_ok и last_checked_at
    
    Args:
        db: SQLAlchemy сессия
        account: Аккаунт из БД (должен быть уже загружен с SELECT FOR UPDATE если нужно)
    """
    now = datetime.now(timezone.utc)
    
    account.validation_fail_count = 0
    account.last_validation_ok = now
    account.last_checked_at = now
    
    logger.debug(
        f"Account {account.id} ({account.label[:20] if account.label else 'N/A'}) "
        f"passed validation, cache updated"
    )


