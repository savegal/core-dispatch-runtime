"""
Типизированные структуры данных для executor.

Используются для улучшения типобезопасности и читаемости кода.
"""
from dataclasses import dataclass
from typing import Optional, List
from datetime import datetime


@dataclass
class BranchInfo:
    """Информация об успешной ветке комментария."""
    main_account_id: int
    comment_text: str
    comment_id: Optional[str] = None
    verified: bool = False
    author: Optional[str] = None
    position: Optional[int] = None


@dataclass
class TaskProcessingResult:
    """Результат обработки задачи."""
    task_id: int
    successful_urls: int
    total_urls: int
    has_errors: bool
    error_messages: List[str]
    processing_time: float
    
    @property
    def success_rate(self) -> float:
        """Возвращает процент успешных URL."""
        if self.total_urls == 0:
            return 0.0
        return (self.successful_urls / self.total_urls) * 100
    
    def is_success(self) -> bool:
        """Проверяет, была ли задача успешно выполнена."""
        return not self.has_errors and self.successful_urls == self.total_urls


@dataclass
class AccountSelectionCriteria:
    """Критерии для выбора аккаунта."""
    exclude_ids: Optional[List[int]] = None
    require_proxy: bool = False
    max_error_count: Optional[int] = None
    geo_match: bool = False
    
    def to_dict(self) -> dict:
        """Преобразует в словарь."""
        return {
            "exclude_ids": self.exclude_ids or [],
            "require_proxy": self.require_proxy,
            "max_error_count": self.max_error_count,
            "geo_match": self.geo_match,
        }


@dataclass
class VerificationInfo:
    """Информация о верификации комментария."""
    found: bool
    position: Optional[int] = None
    comment_id: Optional[str] = None
    author: Optional[str] = None
    text_normalized: Optional[str] = None
    verification_attempts: int = 0
    moderation_timeout: bool = False
    
    def to_dict(self) -> dict:
        """Преобразует в словарь."""
        return {
            "found": self.found,
            "position": self.position,
            "comment_id": self.comment_id,
            "author": self.author,
            "text_normalized": self.text_normalized,
            "verification_attempts": self.verification_attempts,
            "moderation_timeout": self.moderation_timeout,
        }

