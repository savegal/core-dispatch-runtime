"""
Адаптивные задержки на основе поведения TikTok.

Динамически корректирует задержки в зависимости от:
- Частоты успешных/неуспешных попыток
- Обнаружения rate limiting
- Стабильности работы
"""
import logging
import time
from typing import Dict, Optional
from collections import deque
from datetime import datetime, timedelta

logger = logging.getLogger("executor.adaptive_delays")

# История последних операций (для анализа)
_operation_history: Dict[str, deque] = {}
_rate_limit_detections: deque = deque(maxlen=10)  # Последние 10 обнаружений rate limit


class AdaptiveDelays:
    """Управление адаптивными задержками."""
    
    def __init__(self):
        self.base_delays = {
            'page_load': 2000,
            'overlay_close': 500,
            'comment_button_click': 1500,
            'comment_panel_wait': 2000,
            'input_appear': 1000,
            'typing': 150,
            'submit_click': 2000,
            'verification': 8000,
        }
        
        self.multipliers = {
            'page_load': 1.0,
            'overlay_close': 1.0,
            'comment_button_click': 1.0,
            'comment_panel_wait': 1.0,
            'input_appear': 1.0,
            'typing': 1.0,
            'submit_click': 1.0,
            'verification': 1.0,
        }
        
        self.last_rate_limit = None
        self.rate_limit_multiplier = 1.0
    
    def record_operation(self, operation_type: str, success: bool, duration_ms: Optional[float] = None):
        """
        Записывает результат операции для анализа.
        
        Args:
            operation_type: Тип операции ('page_load', 'comment_button_click', etc.)
            success: Успешна ли операция
            duration_ms: Длительность операции в миллисекундах
        """
        if operation_type not in _operation_history:
            _operation_history[operation_type] = deque(maxlen=20)
        
        _operation_history[operation_type].append({
            'success': success,
            'duration_ms': duration_ms,
            'timestamp': time.time()
        })
        
        # Анализируем и корректируем задержки
        self._analyze_and_adjust(operation_type)
    
    def record_rate_limit(self):
        """Записывает обнаружение rate limiting."""
        _rate_limit_detections.append(time.time())
        self.last_rate_limit = time.time()
        
        # Увеличиваем множитель задержек
        self.rate_limit_multiplier = min(self.rate_limit_multiplier * 1.5, 3.0)
        
        logger.warning(
            f"[ADAPTIVE] Rate limit detected. Increasing delay multiplier to {self.rate_limit_multiplier:.2f}"
        )
    
    def _analyze_and_adjust(self, operation_type: str):
        """Анализирует историю операций и корректирует задержки."""
        if operation_type not in _operation_history:
            return
        
        history = _operation_history[operation_type]
        if len(history) < 5:  # Нужно минимум 5 операций для анализа
            return
        
        # Анализируем последние 10 операций
        recent = list(history)[-10:]
        success_rate = sum(1 for op in recent if op['success']) / len(recent)
        
        # Если успешность низкая - увеличиваем задержки
        if success_rate < 0.5:
            self.multipliers[operation_type] = min(
                self.multipliers[operation_type] * 1.2,
                2.5  # Максимум 2.5x
            )
            logger.info(
                f"[ADAPTIVE] Low success rate ({success_rate:.1%}) for {operation_type}. "
                f"Increasing delay multiplier to {self.multipliers[operation_type]:.2f}"
            )
        # Если успешность высокая и стабильная - уменьшаем задержки
        elif success_rate > 0.9 and len(recent) >= 10:
            self.multipliers[operation_type] = max(
                self.multipliers[operation_type] * 0.95,
                0.7  # Минимум 0.7x
            )
            logger.debug(
                f"[ADAPTIVE] High success rate ({success_rate:.1%}) for {operation_type}. "
                f"Decreasing delay multiplier to {self.multipliers[operation_type]:.2f}"
            )
    
    def get_delay(self, operation_type: str, base_delay_ms: Optional[int] = None, using_proxy: bool = False) -> int:
        """
        Возвращает адаптивную задержку для операции.
        
        Args:
            operation_type: Тип операции
            base_delay_ms: Базовая задержка (если не указана, берется из self.base_delays)
            using_proxy: Используется ли прокси (увеличивает задержку)
            
        Returns:
            int: Задержка в миллисекундах
        """
        if base_delay_ms is None:
            base_delay_ms = self.base_delays.get(operation_type, 1000)
        
        # Базовый множитель
        multiplier = self.multipliers.get(operation_type, 1.0)
        
        # Учитываем rate limiting
        if self.last_rate_limit:
            time_since_rate_limit = time.time() - self.last_rate_limit
            # В течение 5 минут после rate limit увеличиваем задержки
            if time_since_rate_limit < 300:
                rate_limit_factor = 1.0 + (self.rate_limit_multiplier - 1.0) * (1.0 - time_since_rate_limit / 300)
                multiplier *= rate_limit_factor
        
        # Учитываем прокси
        if using_proxy:
            multiplier *= 1.3  # +30% для прокси
        
        # Проверяем частоту rate limits
        if len(_rate_limit_detections) >= 3:
            recent_rate_limits = [t for t in _rate_limit_detections if time.time() - t < 600]  # Последние 10 минут
            if len(recent_rate_limits) >= 3:
                multiplier *= 1.5  # +50% если много rate limits
        
        final_delay = int(base_delay_ms * multiplier)
        
        # Ограничиваем разумными пределами
        min_delay = 200 if not using_proxy else 500
        max_delay = 10000 if not using_proxy else 20000
        
        return max(min_delay, min(final_delay, max_delay))
    
    def reset_rate_limit_multiplier(self):
        """Сбрасывает множитель rate limit (после успешных операций)."""
        if self.rate_limit_multiplier > 1.0:
            # Постепенно уменьшаем
            self.rate_limit_multiplier = max(1.0, self.rate_limit_multiplier * 0.9)
            if self.rate_limit_multiplier <= 1.0:
                logger.info("[ADAPTIVE] Rate limit multiplier reset to 1.0")


# Глобальный экземпляр
_adaptive_delays = None

def get_adaptive_delays() -> AdaptiveDelays:
    """Возвращает глобальный экземпляр адаптивных задержек."""
    global _adaptive_delays
    if _adaptive_delays is None:
        _adaptive_delays = AdaptiveDelays()
    return _adaptive_delays

