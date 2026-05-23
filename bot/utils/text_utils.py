"""
Утилиты для работы с текстом в боте.

Включает:
- Экранирование Markdown
- Проверка и обрезка длины сообщений
- Форматирование текста
"""
import re
from typing import Optional

# Максимальная длина сообщения в Telegram
MAX_MESSAGE_LENGTH = 4096


def escape_markdown_v2(text: str) -> str:
    """
    Экранирует специальные символы Markdown V2.
    
    Args:
        text: Текст для экранирования
        
    Returns:
        Экранированный текст
    """
    # Символы, которые нужно экранировать в Markdown V2
    special_chars = r'_*[]()~`>#+-=|{}.!'
    
    # Экранируем каждый специальный символ
    escaped = ""
    for char in text:
        if char in special_chars:
            escaped += "\\" + char
        else:
            escaped += char
    
    return escaped


def escape_markdown(text: str) -> str:
    """
    Экранирует специальные символы Markdown (совместимость).
    
    Использует упрощенное экранирование для основных символов.
    
    Args:
        text: Текст для экранирования
        
    Returns:
        Экранированный текст
    """
    # Основные символы, которые могут вызвать проблемы
    special_chars = {
        '*': '\\*',
        '_': '\\_',
        '`': '\\`',
        '[': '\\[',
        ']': '\\]',
    }
    
    escaped = text
    for char, replacement in special_chars.items():
        escaped = escaped.replace(char, replacement)
    
    return escaped


def truncate_message(text: str, max_length: int = MAX_MESSAGE_LENGTH, suffix: str = "...") -> str:
    """
    Обрезает сообщение до максимальной длины.
    
    Args:
        text: Текст для обрезки
        max_length: Максимальная длина (по умолчанию 4096)
        suffix: Суффикс для обрезанного текста
        
    Returns:
        Обрезанный текст
    """
    if len(text) <= max_length:
        return text
    
    # Обрезаем с учетом суффикса
    truncated = text[:max_length - len(suffix)]
    
    # Пытаемся обрезать по последнему переносу строки
    last_newline = truncated.rfind('\n')
    if last_newline > max_length * 0.8:  # Если перенос строки не слишком далеко от конца
        truncated = truncated[:last_newline]
    
    return truncated + suffix


def split_long_message(text: str, max_length: int = MAX_MESSAGE_LENGTH) -> list[str]:
    """
    Разбивает длинное сообщение на части.
    
    Args:
        text: Текст для разбиения
        max_length: Максимальная длина каждой части
        
    Returns:
        Список частей сообщения
    """
    if len(text) <= max_length:
        return [text]
    
    parts = []
    lines = text.split('\n')
    current_part = ""
    
    for line in lines:
        # Если добавление строки не превысит лимит
        if len(current_part) + len(line) + 1 <= max_length:
            if current_part:
                current_part += "\n" + line
            else:
                current_part = line
        else:
            # Сохраняем текущую часть
            if current_part:
                parts.append(current_part)
            
            # Если сама строка длиннее лимита, обрезаем её
            if len(line) > max_length:
                # Разбиваем длинную строку
                while len(line) > max_length:
                    parts.append(line[:max_length])
                    line = line[max_length:]
                current_part = line
            else:
                current_part = line
    
    # Добавляем последнюю часть
    if current_part:
        parts.append(current_part)
    
    return parts


def format_progress_bar(current: int, total: int, length: int = 10) -> str:
    """
    Создает текстовый прогресс-бар.
    
    Args:
        current: Текущее значение
        total: Максимальное значение
        length: Длина прогресс-бара в символах
        
    Returns:
        Строка с прогресс-баром (например: "████░░░░░░ 40%")
    """
    if total == 0:
        return "░" * length + " 0%"
    
    percentage = min(100, int((current / total) * 100))
    filled = int((current / total) * length)
    filled = min(filled, length)
    
    bar = "█" * filled + "░" * (length - filled)
    return f"{bar} {percentage}%"


def format_file_size(size_bytes: int) -> str:
    """
    Форматирует размер файла в читаемый вид.
    
    Args:
        size_bytes: Размер в байтах
        
    Returns:
        Отформатированная строка (например: "1.5 MB")
    """
    for unit in ['B', 'KB', 'MB', 'GB']:
        if size_bytes < 1024.0:
            return f"{size_bytes:.1f} {unit}"
        size_bytes /= 1024.0
    return f"{size_bytes:.1f} TB"


def format_duration(seconds: int) -> str:
    """
    Форматирует длительность в читаемый вид.
    
    Args:
        seconds: Длительность в секундах
        
    Returns:
        Отформатированная строка (например: "1ч 30м" или "45с")
    """
    if seconds < 60:
        return f"{seconds}с"
    
    minutes = seconds // 60
    remaining_seconds = seconds % 60
    
    if minutes < 60:
        if remaining_seconds > 0:
            return f"{minutes}м {remaining_seconds}с"
        return f"{minutes}м"
    
    hours = minutes // 60
    remaining_minutes = minutes % 60
    
    if remaining_minutes > 0:
        return f"{hours}ч {remaining_minutes}м"
    return f"{hours}ч"

