"""
Константы для моделей БД.
Использование Enum вместо magic strings для безопасности и удобства.
"""
from enum import Enum


class UserRole(str, Enum):
    """Роли пользователей."""
    USER = "user"
    ADMIN = "admin"
    OWNER = "owner"


class AccountStatus(str, Enum):
    """Статусы аккаунтов."""
    ACTIVE = "active"
    DEAD = "dead"
    ERROR = "error"
    LIMITED = "limited"


class TaskStatus(str, Enum):
    """Статусы задач."""
    PENDING = "pending"
    IN_PROGRESS = "in_progress"
    DONE = "done"
    PARTIAL = "partial"
    ERROR = "error"
    STOPPED = "stopped"


class ProxyStatus(str, Enum):
    """Статусы прокси."""
    ACTIVE = "active"
    DEAD = "dead"
    ERROR = "error"
