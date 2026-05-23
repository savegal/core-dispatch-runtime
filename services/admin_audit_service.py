"""
ИСПРАВЛЕНО: Сервис для логирования критических админских операций.
"""
from datetime import datetime, timezone
from sqlalchemy.orm import Session
from db.models import AdminAction
from bot.services.api_client import api_client
import logging

logger = logging.getLogger("services.admin_audit")


async def log_admin_action(
    admin_telegram_id: int,
    action_type: str,
    target_user_id: int | None = None,
    target_account_id: int | None = None,
    details: dict | None = None,
    description: str | None = None
) -> None:
    """
    Логирует критическую админскую операцию.
    
    Args:
        admin_telegram_id: Telegram ID администратора
        action_type: Тип действия (balance_add, balance_subtract, block, unblock, delete_accounts, delete_proxies, etc.)
        target_user_id: ID пользователя, на которого действовали (если применимо)
        target_account_id: ID аккаунта (если применимо)
        details: Дополнительные детали в виде словаря
        description: Текстовое описание действия
    """
    try:
        # Получаем username админа для удобства
        admin_username = None
        try:
            user_data = await api_client.sync_telegram(admin_telegram_id, None)
            admin_username = user_data.get("username")
        except Exception:
            pass  # Не критично, если не удалось получить username
        
        # Создаём запись через API (используем sync DB для простоты)
        from db.database import get_sync_db
        db: Session = next(get_sync_db())
        try:
            action = AdminAction(
                admin_telegram_id=admin_telegram_id,
                admin_username=admin_username,
                action_type=action_type,
                target_user_id=target_user_id,
                target_account_id=target_account_id,
                details=details,
                description=description
            )
            db.add(action)
            db.commit()
            logger.info(f"Admin action logged: {action_type} by {admin_telegram_id} (target_user={target_user_id})")
        except Exception as e:
            db.rollback()
            logger.error(f"Error logging admin action: {e}", exc_info=True)
        finally:
            db.close()
    except Exception as e:
        logger.error(f"Error in log_admin_action: {e}", exc_info=True)
        # Не прерываем выполнение основной операции, если логирование не удалось




