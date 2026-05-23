"""
Кэш селекторов TikTok для ускорения поиска элементов.

Запоминает, какие селекторы работают для конкретных версий TikTok,
и приоритизирует их при следующем использовании.
"""
import json
import logging
from pathlib import Path
from typing import Dict, List, Optional
from datetime import datetime, timedelta
from collections import defaultdict

logger = logging.getLogger("executor.tiktok_selector_cache")

CACHE_FILE = Path("cache/tiktok_selectors.json")
CACHE_TTL_DAYS = 7  # Кэш действителен 7 дней


class SelectorCache:
    """Кэш для хранения успешных селекторов."""
    
    def __init__(self):
        self.cache_file = CACHE_FILE
        self.cache_file.parent.mkdir(exist_ok=True)
        self._cache: Dict[str, Dict] = {}
        self._load_cache()
    
    def _load_cache(self):
        """Загружает кэш из файла."""
        try:
            if self.cache_file.exists():
                with open(self.cache_file, 'r', encoding='utf-8') as f:
                    data = json.load(f)
                    # Фильтруем устаревшие записи
                    now = datetime.now()
                    for key, value in data.items():
                        last_used = datetime.fromisoformat(value.get('last_used', '2000-01-01'))
                        if (now - last_used).days < CACHE_TTL_DAYS:
                            self._cache[key] = value
                    
                    if len(self._cache) < len(data):
                        self._save_cache()  # Сохраняем очищенный кэш
        except Exception as e:
            logger.debug(f"Error loading selector cache: {e}")
            self._cache = {}
    
    def _save_cache(self):
        """Сохраняет кэш в файл."""
        try:
            with open(self.cache_file, 'w', encoding='utf-8') as f:
                json.dump(self._cache, f, indent=2, ensure_ascii=False, default=str)
        except Exception as e:
            logger.debug(f"Error saving selector cache: {e}")
    
    def record_success(self, element_type: str, selector: str, context: Optional[str] = None):
        """
        Записывает успешное использование селектора.
        
        Args:
            element_type: Тип элемента ('comment_button', 'comment_input', 'submit_button')
            selector: CSS селектор, который сработал
            context: Дополнительный контекст (например, версия TikTok)
        """
        key = f"{element_type}:{context or 'default'}"
        
        if key not in self._cache:
            self._cache[key] = {
                'selectors': {},
                'last_used': datetime.now().isoformat()
            }
        
        if selector not in self._cache[key]['selectors']:
            self._cache[key]['selectors'][selector] = {
                'success_count': 0,
                'fail_count': 0,
                'first_used': datetime.now().isoformat(),
                'last_used': datetime.now().isoformat()
            }
        
        self._cache[key]['selectors'][selector]['success_count'] += 1
        self._cache[key]['selectors'][selector]['last_used'] = datetime.now().isoformat()
        self._cache[key]['last_used'] = datetime.now().isoformat()
        
        self._save_cache()
    
    def record_failure(self, element_type: str, selector: str, context: Optional[str] = None):
        """Записывает неудачное использование селектора."""
        key = f"{element_type}:{context or 'default'}"
        
        if key in self._cache and selector in self._cache[key]['selectors']:
            self._cache[key]['selectors'][selector]['fail_count'] += 1
            self._save_cache()
    
    def get_prioritized_selectors(self, element_type: str, all_selectors: List[str], context: Optional[str] = None) -> List[str]:
        """
        Возвращает список селекторов, отсортированный по приоритету (успешные первыми).
        
        Args:
            element_type: Тип элемента
            all_selectors: Все доступные селекторы
            context: Дополнительный контекст
            
        Returns:
            List[str]: Отсортированный список селекторов
        """
        key = f"{element_type}:{context or 'default'}"
        
        if key not in self._cache:
            return all_selectors
        
        cached_selectors = self._cache[key]['selectors']
        
        # Сортируем по успешности (success_count / (success_count + fail_count))
        def get_priority(selector: str) -> float:
            if selector not in cached_selectors:
                return 0.0
            
            stats = cached_selectors[selector]
            total = stats['success_count'] + stats['fail_count']
            if total == 0:
                return 0.0
            
            success_rate = stats['success_count'] / total
            # Учитываем количество успешных использований (больше = надежнее)
            confidence = min(stats['success_count'] / 10.0, 1.0)  # Максимум при 10+ успехах
            
            return success_rate * (0.7 + 0.3 * confidence)  # Комбинированный приоритет
        
        # Сортируем: сначала кэшированные с высоким приоритетом, потом остальные
        prioritized = sorted(
            all_selectors,
            key=lambda s: (s in cached_selectors, get_priority(s)),
            reverse=True
        )
        
        return prioritized


# Глобальный экземпляр кэша
_selector_cache = None

def get_selector_cache() -> SelectorCache:
    """Возвращает глобальный экземпляр кэша."""
    global _selector_cache
    if _selector_cache is None:
        _selector_cache = SelectorCache()
    return _selector_cache

