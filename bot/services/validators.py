import re
from services.tiktok_urls import order_video_key


def normalize_url(url: str) -> str:
    """
    Нормализует URL: добавляет протокол, если отсутствует.
    """
    url = url.strip()
    
    # Если нет протокола, добавляем https://
    if url and not url.lower().startswith(('http://', 'https://')):
        url = 'https://' + url
    
    # Убираем лишние символы в конце
    url = url.rstrip('.,!?;:)]')
    
    return url


def parse_links(text: str, return_invalid_info: bool = False) -> list[str] | tuple[list[str], list[str]]:
    """
    Парсит ссылки из текста с улучшенной валидацией.
    Возвращает список уникальных валидных ссылок.
    
    Args:
        text: Текст для парсинга ссылок
        return_invalid_info: Если True, возвращает кортеж (valid_links, invalid_urls)
    
    Returns:
        list[str] если return_invalid_info=False, иначе tuple[list[str], list[str]]
    """
    if not text:
        return [] if not return_invalid_info else ([], [])
    
    # Паттерн для поиска URL (более гибкий)
    # Находит http(s)://, www., или просто доменные имена
    urls = re.findall(
        r'https?://[^\s<>"\'()]+|(?<!\S)(?:[a-z0-9-]+\.)*tiktok\.com/[^\s<>"\'()]+',
        text, re.IGNORECASE,
    )
    unique_urls = []
    invalid_urls = []
    seen = set()
    for raw in urls:
        url = normalize_url(raw)
        key = order_video_key(url)
        if key is None:
            invalid_urls.append(raw)
        elif key not in seen:
            unique_urls.append(url)
            seen.add(key)
    return (unique_urls, invalid_urls) if return_invalid_info else unique_urls


def validate_video_url(url: str) -> bool:
    """
    Проверяет, является ли ссылка валидной TikTok ссылкой.
    Улучшенная версия с проверкой структуры URL и паттернов.
    
    ИСПРАВЛЕНО: Добавлена проверка паттернов для различных форматов TikTok URL.
    """
    normalized = normalize_url(url) if isinstance(url, str) else ""
    return order_video_key(normalized) is not None
