"""
Retry Policy для HTTP-запросов.

Определяет политику повторных попыток для различных типов ошибок.

ИСПРАВЛЕНО: Добавлен cooldown для 429 и улучшен backoff+jitter.
"""
from enum import Enum
from typing import Optional, Tuple
import time
import random


class HttpErrorType(Enum):
    """Типы HTTP-ошибок."""
    SUCCESS = "success"
    TIMEOUT = "timeout"
    RATE_LIMIT = "rate_limit"  # 429
    FORBIDDEN = "forbidden"  # 403
    CAPTCHA = "captcha"
    NETWORK_ERROR = "network_error"
    SERVER_ERROR = "server_error"  # 5xx
    CLIENT_ERROR = "client_error"  # 4xx (кроме 403, 429)
    API_ERROR = "api_error"  # ИСПРАВЛЕНО: TikTok вернул 200, но status_code != 0 или другие проблемы с ответом API


class RetryPolicy:
    """Политика повторных попыток."""
    
    def __init__(
        self,
        max_retries: int = 3,
        base_delay: float = 1.0,
        max_delay: float = 30.0,
        exponential_base: float = 2.0,
        jitter: bool = True
    ):
        """
        Args:
            max_retries: Максимальное количество повторных попыток
            base_delay: Базовая задержка в секундах
            max_delay: Максимальная задержка в секундах
            exponential_base: Основание для экспоненциального backoff
            jitter: Добавлять ли случайную задержку (jitter)
        """
        self.max_retries = max_retries
        self.base_delay = base_delay
        self.max_delay = max_delay
        self.exponential_base = exponential_base
        self.jitter = jitter
    
    def should_retry(self, error_type: HttpErrorType, attempt: int) -> bool:
        """
        Определяет, нужно ли повторять попытку.
        
        ИСПРАВЛЕНО: При 403/капче - не долбить повторно HTTP, сразу fallback/остановка.
        
        Args:
            error_type: Тип ошибки
            attempt: Номер текущей попытки (начиная с 0)
            
        Returns:
            bool: True если нужно повторить
        """
        if attempt >= self.max_retries:
            return False
        
        # Фатальные ошибки - не ретраим (403, капча)
        # ИСПРАВЛЕНО: При 403/капче не повторяем HTTP, сразу fallback
        if error_type in (HttpErrorType.FORBIDDEN, HttpErrorType.CAPTCHA):
            return False
        
        # 429 (rate limit) - можно ретраить, но с cooldown на аккаунт
        # Решение о retry принимается выше, здесь просто разрешаем
        if error_type == HttpErrorType.RATE_LIMIT:
            return True
        
        # API_ERROR (TikTok вернул 200 но status_code != 0) - можно ретраить, но осторожно
        # Обычно это временные проблемы или проблемы с подписями
        if error_type == HttpErrorType.API_ERROR:
            return True  # Разрешаем retry (но не более max_retries раз)
        
        # Остальные ошибки - ретраим
        return True
    
    def get_cooldown_minutes(self, error_type: HttpErrorType) -> Optional[float]:
        """
        Возвращает время cooldown в минутах для аккаунта при ошибке.
        
        ИСПРАВЛЕНО: При 429 ставим аккаунт в cooldown на 10-30 минут.
        
        Args:
            error_type: Тип ошибки
            
        Returns:
            float: Минуты cooldown или None если cooldown не нужен
        """
        if error_type == HttpErrorType.RATE_LIMIT:
            # Cooldown 10-30 минут (случайный разброс)
            return random.uniform(10.0, 30.0)
        elif error_type in (HttpErrorType.FORBIDDEN, HttpErrorType.CAPTCHA):
            # Фатальные ошибки - длительный cooldown (или нужно ручное вмешательство)
            return None  # Не используем cooldown, аккаунт помечается как error/dead
        return None
    
    def get_delay(self, attempt: int, error_type: Optional[HttpErrorType] = None) -> float:
        """
        Вычисляет задержку перед следующей попыткой.
        
        ИСПРАВЛЕНО: Экспоненциальный backoff с jitter (0.5 → 1.2 → 2.5 сек).
        Для 429 использует более длительные задержки.
        
        Args:
            attempt: Номер попытки (начиная с 0)
            error_type: Тип ошибки (для 429 используются более длительные задержки)
            
        Returns:
            float: Задержка в секундах
        """
        # Для 429 используем более мягкий backoff: 0.5 → 1.2 → 2.5 сек
        if error_type == HttpErrorType.RATE_LIMIT:
            base_delays = [0.5, 1.2, 2.5, 5.0, 10.0]
            if attempt < len(base_delays):
                delay = base_delays[attempt]
            else:
                delay = min(10.0 + (attempt - len(base_delays)) * 2.0, self.max_delay)
        else:
            # Для остальных ошибок: экспоненциальный backoff
            delay = self.base_delay * (self.exponential_base ** attempt)
            delay = min(delay, self.max_delay)
        
        # Добавляем jitter (случайная задержка ±15%)
        if self.jitter:
            jitter_amount = delay * 0.15
            delay += random.uniform(-jitter_amount, jitter_amount)
            delay = max(0.1, delay)  # Минимум 0.1 секунды
        
        return delay
    
    def is_fatal_for_account(self, error_type: HttpErrorType) -> bool:
        """
        Определяет, является ли ошибка фатальной для аккаунта.
        
        Args:
            error_type: Тип ошибки
            
        Returns:
            bool: True если ошибка фатальная
        """
        return error_type in (HttpErrorType.FORBIDDEN, HttpErrorType.CAPTCHA)
    
    def is_fatal_for_proxy(self, error_type: HttpErrorType) -> bool:
        """
        Определяет, является ли ошибка фатальной для прокси.
        
        Args:
            error_type: Тип ошибки
            
        Returns:
            bool: True если ошибка фатальная
        """
        return error_type == HttpErrorType.TIMEOUT


def classify_http_error(status_code: Optional[int], error: Optional[Exception] = None) -> Tuple[HttpErrorType, str]:
    """
    Классифицирует HTTP-ошибку.
    
    Args:
        status_code: HTTP статус код
        error: Исключение (если есть)
        
    Returns:
        tuple: (HttpErrorType, описание ошибки)
    """
    if status_code is None:
        if error:
            error_str = str(error).lower()
            if "timeout" in error_str or "timed out" in error_str:
                return HttpErrorType.TIMEOUT, "Request timeout"
            return HttpErrorType.NETWORK_ERROR, f"Network error: {error}"
        return HttpErrorType.NETWORK_ERROR, "Unknown network error"
    
    if status_code == 200:
        return HttpErrorType.SUCCESS, "Success"
    elif status_code == 403:
        return HttpErrorType.FORBIDDEN, "Forbidden (403) - account may be blocked"
    elif status_code == 429:
        return HttpErrorType.RATE_LIMIT, "Rate limit exceeded (429)"
    elif status_code >= 500:
        return HttpErrorType.SERVER_ERROR, f"Server error ({status_code})"
    elif status_code >= 400:
        return HttpErrorType.CLIENT_ERROR, f"Client error ({status_code})"
    else:
        return HttpErrorType.NETWORK_ERROR, f"Unexpected status code: {status_code}"


# Стандартные политики
STANDARD_POLICY = RetryPolicy(max_retries=3, base_delay=1.0, max_delay=10.0)
AGGRESSIVE_POLICY = RetryPolicy(max_retries=5, base_delay=0.5, max_delay=5.0)
CONSERVATIVE_POLICY = RetryPolicy(max_retries=2, base_delay=2.0, max_delay=30.0)
