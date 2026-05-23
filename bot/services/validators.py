import re
from urllib.parse import urlparse, urlunparse


def _is_tiktok_domain(hostname: str | None) -> bool:
    """Strict hostname check for TikTok domains."""
    if not hostname:
        return False

    host = hostname.strip().lower().strip(".")
    if not host:
        return False

    allowed_hosts = {
        "tiktok.com",
        "www.tiktok.com",
        "m.tiktok.com",
        "vm.tiktok.com",
        "vt.tiktok.com",
    }
    return host in allowed_hosts or host.endswith(".tiktok.com")


def normalize_url(url: str) -> str:
    """
    Нормализует URL: добавляет протокол, если отсутствует.
    """
    url = url.strip()
    
    # Если нет протокола, добавляем https://
    if url and not url.startswith(('http://', 'https://')):
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
            parsed = urlparse(url)
            
            # Проверяем, что это TikTok ссылка
            hostname = parsed.hostname
            if not hostname:
                continue
                
            
            if _is_tiktok_domain(hostname) and parsed.path:
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
    if not url:
        return False
    
    try:
        # Нормализуем URL
        url = normalize_url(url)
        parsed = urlparse(url)
        
        # Проверяем домен
        hostname = parsed.hostname
        if not _is_tiktok_domain(hostname):
            return False
        
        # ИСПРАВЛЕНО: Проверяем паттерны для различных форматов TikTok URL
        path = parsed.path.lower()
        
        # Паттерны для валидных TikTok URL:
        # 1. /@username/video/1234567890
        # 2. /video/1234567890
        # 3. /ZMHE6tVbs7qR3-CIn5C/ (короткие ссылки)
        # 4. /t/ZTd... (новый формат коротких ссылок)
        
        valid_patterns = [
            r'/@[\w\.-]+/video/\d+',  # Стандартный формат
            r'/video/\d+',  # Прямая ссылка на видео
            r'/[a-z0-9_-]+',  # Короткие ссылки (vm.tiktok.com, vt.tiktok.com)
            r'/t/[a-z0-9_-]+',  # Новый формат коротких ссылок
        ]
        
        import re
        has_valid_pattern = any(re.match(pattern, path) for pattern in valid_patterns)
        
        # Проверяем, что есть путь (видео)
        has_path = bool(parsed.path and len(parsed.path) > 1)
        
        return has_path and has_valid_pattern
    except Exception:
        return False
