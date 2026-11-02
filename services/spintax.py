"""
Spintax expansion utility for anti-spam comment/reply text variation.
Supports nested {option1|option2|option3} expressions while leaving regular
non-pipe braces untouched.
"""
from __future__ import annotations

import hashlib
import random
import re
from typing import Any

_SPINTAX_PATTERN = re.compile(r"\{([^{}]*\|[^{}]*)\}")


def has_spintax(text: str | None) -> bool:
    """Return True when text contains at least one {opt1|opt2} spintax block."""
    if not text or "{" not in text or "|" not in text or "}" not in text:
        return False
    return bool(_SPINTAX_PATTERN.search(text))


def expand_spintax(text: str | None, *, seed: Any = None) -> str:
    """Expand {opt1|opt2|opt3} blocks (including nested spintax) into one string."""
    if not text:
        return ""
    raw = str(text)
    if not has_spintax(raw):
        return raw

    if seed is not None:
        digest = hashlib.sha256(str(seed).encode("utf-8", errors="ignore")).hexdigest()
        rng: random.Random | Any = random.Random(int(digest[:16], 16))
    else:
        rng = random

    def _replace_match(match: re.Match[str]) -> str:
        options = [opt for opt in match.group(1).split("|")]
        non_empty = [opt for opt in options if opt.strip()]
        pool = non_empty if non_empty else options
        return rng.choice(pool) if pool else ""

    current = raw
    for _ in range(10):
        updated = _SPINTAX_PATTERN.sub(_replace_match, current)
        if updated == current:
            break
        current = updated

    # Normalize accidental double spaces introduced by empty/varied branches
    lines = [" ".join(line.split()) for line in current.splitlines()]
    return "\n".join(lines).strip()
