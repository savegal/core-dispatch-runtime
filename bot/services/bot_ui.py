from __future__ import annotations

from typing import Iterable, Sequence

from bot.utils.markdown_escape import escape_markdown_simple


def md(text: object) -> str:
    return escape_markdown_simple("" if text is None else str(text))


def short(text: object, limit: int = 80) -> str:
    raw = "" if text is None else str(text).strip()
    if len(raw) > limit:
        raw = raw[: max(0, limit - 1)] + "…"
    return md(raw)


def block(title: str, lines: Sequence[str] | None = None) -> str:
    body = "\n".join(line for line in (lines or []) if line)
    if body:
        return f"*{title}*\n{body}"
    return f"*{title}*"


def bullets(items: Iterable[str], bullet: str = "•") -> list[str]:
    return [f"{bullet} {item}" for item in items if item]


def kv_rows(rows: Sequence[tuple[str, object]]) -> list[str]:
    rendered: list[str] = []
    for label, value in rows:
        if value is None or value == "":
            continue
        rendered.append(f"{label}: {value}")
    return rendered


def divider() -> str:
    """Backward-compatible no-op divider for legacy callers."""
    return ""


def status_line(icon: str, label: str, value: object) -> str:
    if value is None or value == "":
        return ""
    return f"{icon} {label}: {value}"


def screen(title: str, *blocks_text: str) -> str:
    parts = [f"*{title}*"]
    parts.extend(part for part in blocks_text if part)
    return "\n\n".join(parts)


def empty_screen(title: str, body: str, hint: str | None = None) -> str:
    parts = [body]
    if hint:
        parts.append(hint)
    return screen(title, *parts)
