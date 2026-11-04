import re
from services.tiktok_urls import is_tiktok_shortlink, media_id_from_url


def normalize_url(url: str) -> str:
    """
    Нормализует URL: добавляет протокол, если отсутствует.
    """
    url = url.strip()
    
    # Если нет протокола, добавляем https://
    if url and not url.lower().startswith(('http://', 'https://')):
        url = 'https://' + url
    
    # Убираем лишние символы в конце
    url = url.rstrip('.,!?;:)')
    
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
    url_patterns = [
        r'https?://[^\s<>"\'()]+',  # С протоколом
        r'(?:(?<=\s)|^)(?:www\.)?(?:tiktok\.com|vm\.tiktok\.com|vt\.tiktok\.com|m\.tiktok\.com)/[^\s<>"\'()]+',  # Без протокола, но с доменом TikTok
    ]
    
    urls = []
    for pattern in url_patterns:
        found = re.findall(pattern, text, re.IGNORECASE)
        urls.extend(found)
    
    # Нормализуем и фильтруем
    unique_urls = []
    seen = set()

    for url in urls:
        try:
            # Нормализуем URL
            url = normalize_url(url)
            
            # Проверяем дубликаты
            if url in seen:
                continue
            
            # Парсим URL для валидации
            if media_id_from_url(url) or is_tiktok_shortlink(url):
                unique_urls.append(url)
                seen.add(url)
        except Exception:
            # Пропускаем некорректные URL
            continue
    
    if return_invalid_info:
        # Собираем невалидные URL для информации
        all_found_urls = []
        for pattern in url_patterns:
            found = re.findall(pattern, text, re.IGNORECASE)
            all_found_urls.extend(found)
        
        invalid_urls = [url for url in all_found_urls if normalize_url(url) not in seen]
        return unique_urls, invalid_urls

    return unique_urls


def validate_video_url(url: str) -> bool:
    """
    Проверяет, является ли ссылка валидной TikTok ссылкой.
    Улучшенная версия с проверкой структуры URL и паттернов.
    
    ИСПРАВЛЕНО: Добавлена проверка паттернов для различных форматов TikTok URL.
    """
    normalized = normalize_url(url) if isinstance(url, str) else ""
    return bool(media_id_from_url(normalized) or is_tiktok_shortlink(normalized))
