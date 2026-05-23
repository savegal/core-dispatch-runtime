"""
Утилиты для обработки текста.
Включает поддержку Spintax (Spinning Syntax) для генерации уникальных вариаций текста.
"""
import random
import re
import logging

logger = logging.getLogger("executor.text_utils")

def process_spintax(text: str) -> str:
    """
    Обрабатывает Spintax в тексте.
    Поддерживает вложенные структуры любой глубины.
    
    Пример:
    "{Привет|Здравствуй}, {друг|товарищ}!" -> "Привет, друг!" или "Здравствуй, товарищ!"
    "{A|B|{C|D}}" -> "A" или "C"
    """
    if not text:
        return ""
        
    start_time = 0
    # Лимит итераций для защиты от зависания при некорректном синтаксисе
    max_iterations = 100 
    iterations = 0
    
    pattern = re.compile(r'\{([^{}]*)\}')
    
    result = text
    while True:
        # Ищем самые глубокие вложенности (без фигурных скобок внутри)
        match = pattern.search(result)
        if not match:
            break
            
        iterations += 1
        if iterations > max_iterations:
            logger.warning(f"Spintax processing hit recursion limit for text: {text[:50]}...")
            break
            
        full_match = match.group(0) # например {a|b}
        content = match.group(1)    # например a|b
        
        choices = content.split('|')
        choice = random.choice(choices)
        
        # Заменяем первое найденное вхождение
        # Используем replace, но только 1 раз для конкретного match
        # Важно: match.start() и match.end() дают позиции в ТЕКУЩЕМ result
        # Но проще использовать re.sub с count=1, но нужно экранировать
        
        result = result[:match.start()] + choice + result[match.end():]
        
    return result
