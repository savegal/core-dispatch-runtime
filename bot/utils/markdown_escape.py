"""
Утилиты для экранирования Markdown в Telegram.

ИСПРАВЛЕНО: Добавлена функция escape_markdown для безопасного отображения
пользовательского текста в сообщениях с parse_mode="Markdown".
"""
import re


def escape_markdown(text: str) -> str:
    """
    Экранирует специальные символы Markdown для безопасного отображения в Telegram.
    
    Экранирует:
    - _ (подчеркивание)
    - * (звездочка)
    - ` (обратная кавычка)
    - [ (квадратная скобка открывающая)
    - ] (квадратная скобка закрывающая)
    - ( (круглая скобка открывающая)
    - ) (круглая скобка закрывающая)
    - ~ (тильда)
    - | (вертикальная черта)
    - # (решетка)
    - + (плюс)
    - - (минус)
    - = (равно)
    - . (точка в начале строки)
    - ! (восклицательный знак)
    
    Args:
        text: Текст для экранирования
        
    Returns:
        str: Экранированный текст
    """
    if not text:
        return ""
    
    # Символы, которые нужно экранировать в Markdown V2
    # Для Markdown (обычного) достаточно: _ * ` [ ]
    special_chars = r'_*`\[\]()~>#+\-=|{}.!'
    
    # Экранируем каждый специальный символ
    escaped = ""
    for char in text:
        if char in special_chars:
            escaped += "\\" + char
        else:
            escaped += char
    
    return escaped


def escape_markdown_v2(text: str) -> str:
    """
    Экранирует специальные символы Markdown V2 для безопасного отображения.
    
    Используется для parse_mode="MarkdownV2".
    """
    return escape_markdown(text)  # Пока используем ту же логику


def escape_markdown_simple(text: str) -> str:
    """
    Упрощенное экранирование для обычного Markdown (не V2).
    
    Экранирует только основные символы: _ * ` [ ]
    """
    if not text:
        return ""
    
    special_chars = r'_*`\[\]'
    escaped = ""
    for char in text:
        if char in special_chars:
            escaped += "\\" + char
        else:
            escaped += char
    
    return escaped


