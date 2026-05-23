from datetime import datetime
from sqlalchemy.orm import Session
from db.models import Log
import logging

logger = logging.getLogger("services.log_service")


def log(db: Session, task_id: int, account_id: int | None, message: str) -> Log:
    """Создаёт запись в логе."""
    try:
        log_entry = Log(
            task_id=task_id,
            account_id=account_id,
            message=message
        )
        db.add(log_entry)
        db.commit()
        db.refresh(log_entry)
        return log_entry
    except Exception as e:
        db.rollback()
        logger.error(f"Error creating log entry for task {task_id}: {e}", exc_info=True)
        # Не пробрасываем исключение - логирование не критично
        # Создаём пустой объект для обратной совместимости
        return Log(id=0, task_id=task_id, account_id=account_id, message=message, timestamp=datetime.now())

