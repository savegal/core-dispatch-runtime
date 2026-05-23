"""
State machine для задач - валидация переходов статусов.

ИСПРАВЛЕНО: Предотвращает невалидные переходы статусов задач.
"""
from enum import Enum
from typing import Set, Optional
from db.constants import TaskStatus
import logging

logger = logging.getLogger("services.task_state_machine")


class TaskStateTransitionError(Exception):
    """Ошибка при невалидном переходе статуса задачи."""
    pass


# ИСПРАВЛЕНО: Определяем валидные переходы статусов
VALID_TRANSITIONS: dict[str, Set[str]] = {
    TaskStatus.PENDING: {
        TaskStatus.STOPPED,
        TaskStatus.IN_PROGRESS,  # Начало обработки
        TaskStatus.ERROR,        # Ошибка при создании/постановке в очередь
    },
    TaskStatus.IN_PROGRESS: {
        TaskStatus.DONE,         # Успешное завершение
        TaskStatus.ERROR,        # Ошибка при обработке
        TaskStatus.PENDING,      # Восстановление зависшей задачи
        TaskStatus.PARTIAL,      # Частичное выполнение (некоторые видео обработаны)
        TaskStatus.STOPPED,      # Остановлено пользователем
    },
    TaskStatus.DONE: set(),  # DONE - финальный статус, переходы не разрешены
    TaskStatus.ERROR: {
        TaskStatus.PENDING,      # Повторная попытка после ошибки
    },
    TaskStatus.PARTIAL: {
        TaskStatus.DONE,         # Завершение после частичного выполнения
        TaskStatus.ERROR,        # Ошибка при доработке
    },
    TaskStatus.STOPPED: set(),  # STOPPED - финальный статус, переходы не разрешены
}


def validate_status_transition(current_status: str, new_status: str) -> bool:
    """
    ИСПРАВЛЕНО: Валидирует переход статуса задачи.
    
    Args:
        current_status: Текущий статус задачи
        new_status: Новый статус задачи
        
    Returns:
        bool: True если переход валиден, False если нет
        
    Raises:
        TaskStateTransitionError: Если переход невалиден
    """
    # Нормализуем статусы (приводим к нижнему регистру)
    current = current_status.lower() if current_status else None
    new = new_status.lower() if new_status else None
    
    if not current or not new:
        raise TaskStateTransitionError(
            f"Invalid status: current={current_status}, new={new_status}"
        )
    
    # Если статус не изменился - это валидно (idempotent)
    if current == new:
        return True
    
    # Проверяем, есть ли такой переход в валидных
    allowed_next = VALID_TRANSITIONS.get(current, set())
    
    if new not in allowed_next:
        # ИСПРАВЛЕНО: Логируем предупреждение, но не падаем (для обратной совместимости)
        logger.warning(
            f"Invalid status transition: {current} -> {new}. "
            f"Allowed transitions from {current}: {allowed_next}"
        )
        raise TaskStateTransitionError(
            f"Invalid status transition: {current} -> {new}. "
            f"Allowed transitions from {current}: {allowed_next}"
        )
    
    return True


def can_transition_to(current_status: str, new_status: str) -> bool:
    """
    Проверяет, возможен ли переход статуса без выброса исключения.
    
    Args:
        current_status: Текущий статус задачи
        new_status: Новый статус задачи
        
    Returns:
        bool: True если переход валиден, False если нет
    """
    try:
        validate_status_transition(current_status, new_status)
        return True
    except TaskStateTransitionError:
        return False


def get_allowed_transitions(current_status: str) -> Set[str]:
    """
    Возвращает множество валидных следующих статусов для текущего статуса.
    
    Args:
        current_status: Текущий статус задачи
        
    Returns:
        Set[str]: Множество валидных следующих статусов
    """
    current = current_status.lower() if current_status else None
    if not current:
        return set()
    
    return VALID_TRANSITIONS.get(current, set())


def safe_transition(current_status: str, new_status: str) -> Optional[str]:
    """
    ИСПРАВЛЕНО: Безопасный переход статуса с валидацией.
    
    Если переход невалиден, возвращает None и логирует предупреждение.
    Если валиден - возвращает новый статус.
    
    Args:
        current_status: Текущий статус задачи
        new_status: Новый статус задачи
        
    Returns:
        Optional[str]: Новый статус если переход валиден, None если нет
    """
    try:
        normalized_new = new_status.lower() if isinstance(new_status, str) else new_status
        validate_status_transition(current_status, normalized_new)
        return normalized_new
    except TaskStateTransitionError as e:
        logger.warning(f"Status transition blocked: {e}")
        return None
