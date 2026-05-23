from __future__ import annotations

from typing import Any

from config.settings import get_settings


app_settings = get_settings()

TEMPLATE_TYPE_FULL = "full"
TEMPLATE_TYPE_REPLIES_ONLY = "replies_only"
REPLIES_ONLY_MAIN_PLACEHOLDER = "__replies_only_template__"


def template_type(template: dict[str, Any] | None) -> str:
    template = template or {}
    settings_payload = template.get("settings") or {}
    raw_type = (
        settings_payload.get("template_type")
        or settings_payload.get("template_kind")
        or settings_payload.get("scope")
        or TEMPLATE_TYPE_FULL
    )
    value = str(raw_type or "").strip().lower()
    if value in {"reply_only", "reply-only", "replies", "replies-only"}:
        return TEMPLATE_TYPE_REPLIES_ONLY
    if value == TEMPLATE_TYPE_REPLIES_ONLY:
        return TEMPLATE_TYPE_REPLIES_ONLY
    return TEMPLATE_TYPE_FULL


def is_replies_only_template(template: dict[str, Any] | None) -> bool:
    return template_type(template) == TEMPLATE_TYPE_REPLIES_ONLY


def is_full_template(template: dict[str, Any] | None) -> bool:
    return template_type(template) == TEMPLATE_TYPE_FULL


def filter_full_templates(templates: list[dict[str, Any]] | None) -> list[dict[str, Any]]:
    return [template for template in (templates or []) if is_full_template(template)]


def filter_reply_source_templates(templates: list[dict[str, Any]] | None) -> list[dict[str, Any]]:
    return [
        template
        for template in (templates or [])
        if template_replies(template)
    ]


def normalize_user_text(value: str | None) -> str | None:
    """Канонизация текста пользователя для wizard/quick_order/composer.

    - CRLF → LF (Telegram-клиент под Windows присылает CRLF).
    - Trim ведущих/трейлинговых whitespace (включая пустые строки в начале/конце).
    - Эмодзи (включая ZWJ-композиции, skin-tone modifiers), кириллицу,
      внутренние переводы строк, иероглифы и пунктуацию НЕ трогаем —
      это пользовательский контент, идущий в TikTok как есть.
    """
    if value is None:
        return None
    return str(value).replace("\r\n", "\n").replace("\r", "\n").strip()


def has_meaningful_text(value: str | None) -> bool:
    normalized = normalize_user_text(value)
    return bool(normalized and normalized.strip())


def preserve_nonempty_lines(raw_text: str | None) -> list[str]:
    normalized = normalize_user_text(raw_text) or ""
    return [line for line in normalized.split("\n") if line.strip()]


def normalize_text_variants_list(items: list[str] | None) -> list[str]:
    variants: list[str] = []
    for item in items or []:
        normalized = normalize_user_text(item)
        if has_meaningful_text(normalized):
            variants.append(normalized or "")
    return variants


def replies_dict_to_list(replies: dict[int, str] | None) -> list[str]:
    values: list[str] = []
    for idx in sorted((replies or {}).keys()):
        normalized = normalize_user_text((replies or {}).get(idx))
        if has_meaningful_text(normalized):
            values.append(normalized or "")
    return values


def template_text_variants(template: dict[str, Any] | None) -> list[str]:
    template = template or {}
    if is_replies_only_template(template):
        return []

    settings_payload = template.get("settings") or {}

    variants = normalize_text_variants_list(settings_payload.get("text_variants"))
    if variants:
        return variants

    main_comment = normalize_user_text(template.get("main_comment"))
    if not has_meaningful_text(main_comment):
        return []
    return [main_comment or ""]


def template_replies(template: dict[str, Any] | None) -> list[str]:
    template = template or {}
    settings_payload = template.get("settings") or {}

    replies = normalize_text_variants_list(settings_payload.get("replies"))
    if replies:
        return replies[: app_settings.MAX_REPLIES]

    fallback: list[str] = []
    for idx in range(1, app_settings.MAX_REPLIES + 1):
        value = template.get(f"reply{idx}") if idx <= 3 else settings_payload.get(f"reply{idx}")
        normalized = normalize_user_text(value)
        if has_meaningful_text(normalized):
            fallback.append(normalized or "")
    return fallback
