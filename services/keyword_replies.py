"""Configuration and matching shared by keyword orders, API and bot."""
import re

from services.comment_search import contains_search_phrase, normalize_search_phrase

KEYWORD_ORDER_KIND = "keyword_replies"
MAX_KEYWORDS = 50
MAX_KEYWORD_LENGTH = 100


def replies_per_keyword_target(value) -> int:
    try:
        return max(1, min(6, int(value or 1)))
    except (TypeError, ValueError, OverflowError):
        return 1


def normalize_keywords(value) -> list[str]:
    if isinstance(value, str):
        value = re.split(r"[,;\n\r]+", value)
    if not isinstance(value, list):
        raise ValueError("Укажите ключевые слова списком, через запятую или с новой строки")
    result, seen = [], set()
    for item in value:
        if not isinstance(item, str):
            raise ValueError("Каждый ключ должен быть словом или фразой")
        text = item.strip()
        if not text:
            continue
        if len(text) > MAX_KEYWORD_LENGTH:
            raise ValueError(f"Ключевая фраза слишком длинная: максимум {MAX_KEYWORD_LENGTH} символов")
        normalized = normalize_search_phrase(text)
        if not normalized:
            raise ValueError("Ключ должен содержать слова: одних смайликов недостаточно")
        if normalized not in seen:
            result.append(text)
            seen.add(normalized)
    if not result or len(result) > MAX_KEYWORDS:
        raise ValueError(f"Нужно от 1 до {MAX_KEYWORDS} ключевых слов или фраз")
    return result


def matches_keywords(text: str, keywords: list[str]) -> bool:
    return any(contains_search_phrase(keyword, text) for keyword in keywords)
