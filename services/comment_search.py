"""Word/phrase matching for engagement orders, not publication verification."""

import re
import unicodedata


def normalize_search_phrase(text: str) -> str:
    """Keep words, ignore emoji/punctuation, fold case and whitespace.

    Symbols separate words rather than joining them: ``где🔥посмотреть`` is
    two words, while ``нигде`` must never match the query ``где``. Combining
    marks are retained for languages that need them. Zero-width formatting
    characters are not words; keycap emoji must not become searchable digits.
    """
    value = re.sub(r"[0-9#*]\ufe0f?\u20e3", " ", text or "")
    chars = []
    for char in value:
        category = unicodedata.category(char)
        if category == "Cf" or "\ufe00" <= char <= "\ufe0f":
            continue
        if char in {"\u2139", "\u20e3"}:  # Information emoji / keycap component.
            chars.append(" ")
            continue
        chars.append(char if category[0] in {"L", "N", "M"} else " ")
    # Remove symbols before NFKC: otherwise emoji such as ™ or 🈁 can turn
    # into letters and incorrectly become part of the search phrase.
    normalized = " ".join(unicodedata.normalize("NFKC", "".join(chars)).casefold().split())
    return normalized if any(char.isalnum() for char in normalized) else ""


def contains_search_phrase(expected: str, actual: str) -> bool:
    """Match consecutive whole words; an empty/emoji-only query never matches."""
    phrase = normalize_search_phrase(expected)
    return bool(phrase and f" {phrase} " in f" {normalize_search_phrase(actual)} ")
