"""Проверяем единообразную обработку текста на пути создания заказа.

Покрываем:
- эмодзи (включая ZWJ-композиции и skin-tone modifiers) проходят без потерь;
- _preserve_text / _preserve_user_text / normalize_user_text trim'ят
  ведущие/трейлинговые whitespace, не ломая внутренний multi-line;
- _dedup_variants корректно различает ZWJ-семьи (👨‍👩‍👧 vs 👨👩👧)
  и одинаково сворачивает кириллицу разного регистра.
"""
import pytest


# ─── ЭМОДЗИ ПРОХОДЯТ ───────────────────────────────────────────────────────

@pytest.mark.unit
def test_template_utils_preserves_emoji():
    from bot.services.template_utils import normalize_user_text
    sample = "Огонь видео 🔥👍 спасибо ✨"
    out = normalize_user_text(sample)
    assert out is not None
    assert "🔥" in out
    assert "👍" in out
    assert "✨" in out


@pytest.mark.unit
def test_api_preserve_text_keeps_emoji_and_punctuation():
    from api.tasks import _preserve_text
    sample = "Это супер!!! 🚀 Реально ★ круто... 👍🏿"  # spec chars + skin tone
    out = _preserve_text(sample)
    assert out == sample.strip()
    assert "🚀" in out
    assert "👍🏿" in out
    assert "★" in out


@pytest.mark.unit
def test_api_client_preserve_user_text_keeps_emoji():
    from bot.services.api_client import APIClient

    sample = "Привет мир 👋 как дела? 🤔"
    out = APIClient._preserve_user_text(sample)
    assert "👋" in out
    assert "🤔" in out


# ─── TRIM WHITESPACE ───────────────────────────────────────────────────────

@pytest.mark.unit
def test_normalize_user_text_strips_leading_trailing_whitespace():
    from bot.services.template_utils import normalize_user_text
    assert normalize_user_text("  hello  ") == "hello"
    assert normalize_user_text("\n\n  hi  \n\n") == "hi"


@pytest.mark.unit
def test_normalize_user_text_preserves_internal_newlines():
    from bot.services.template_utils import normalize_user_text
    assert normalize_user_text("first\nsecond\nthird") == "first\nsecond\nthird"


@pytest.mark.unit
def test_normalize_user_text_normalizes_crlf_to_lf():
    from bot.services.template_utils import normalize_user_text
    assert normalize_user_text("a\r\nb\r\nc") == "a\nb\nc"
    assert normalize_user_text("a\rb\rc") == "a\nb\nc"


@pytest.mark.unit
def test_api_preserve_text_strips_but_keeps_inner_newlines():
    from api.tasks import _preserve_text
    assert _preserve_text("  \n  hello\nworld  \n  ") == "hello\nworld"


@pytest.mark.unit
def test_api_client_preserve_user_text_strips_whitespace():
    from bot.services.api_client import APIClient
    assert APIClient._preserve_user_text("\t  привет  \n") == "привет"
    assert APIClient._preserve_user_text("") is None or APIClient._preserve_user_text("") == ""


# ─── DEDUP С УЧЁТОМ ЭМОДЗИ ─────────────────────────────────────────────────

@pytest.mark.unit
def test_dedup_variants_keeps_zwj_emoji_distinct():
    """👨‍👩‍👧 (с ZWJ) ≠ 👨👩👧 (без ZWJ) — это два разных эмодзи."""
    from executor.user_schema import _dedup_variants

    family_zwj = "\U0001F468‍\U0001F469‍\U0001F467"   # ZWJ
    family_plain = "\U0001F468\U0001F469\U0001F467"             # no ZWJ
    assert family_zwj != family_plain

    out = _dedup_variants([family_zwj, family_plain, family_zwj])
    # Два разных варианта остались, дубликат zwj свёрнут.
    assert len(out) == 2
    assert family_zwj in out
    assert family_plain in out


@pytest.mark.unit
def test_dedup_variants_keeps_skin_tone_modifiers_distinct():
    """👍🏿 (тёмный) ≠ 👍🏼 (светлый)."""
    from executor.user_schema import _dedup_variants

    dark = "\U0001F44D\U0001F3FF"
    light = "\U0001F44D\U0001F3FC"
    plain = "\U0001F44D"
    out = _dedup_variants([dark, light, plain])
    assert len(out) == 3


@pytest.mark.unit
def test_dedup_variants_case_insensitive_cyrillic():
    from executor.user_schema import _dedup_variants
    assert _dedup_variants(["Привет", "ПРИВЕТ", "пРиВеТ"]) == ["Привет"]


@pytest.mark.unit
def test_dedup_variants_normalizes_internal_whitespace():
    from executor.user_schema import _dedup_variants
    # Разница только в количестве пробелов внутри — должны считаться одинаковыми.
    out = _dedup_variants(["hello   world", "hello world"])
    assert len(out) == 1


# ─── ТЕКСТ В ШАБЛОНАХ ───────────────────────────────────────────────────────

@pytest.mark.unit
def test_template_text_variants_falls_back_to_main_comment():
    from bot.services.template_utils import template_text_variants

    # Старый шаблон без settings.text_variants — должен дать [main_comment].
    t = {"main_comment": "Привет 👋", "settings": {}}
    assert template_text_variants(t) == ["Привет 👋"]


@pytest.mark.unit
def test_template_text_variants_uses_settings_when_present():
    from bot.services.template_utils import template_text_variants

    t = {
        "main_comment": "x",
        "settings": {"text_variants": ["вариант 1 🔥", "вариант 2 ✨", "вариант 3 🎉"]},
    }
    out = template_text_variants(t)
    assert out == ["вариант 1 🔥", "вариант 2 ✨", "вариант 3 🎉"]


@pytest.mark.unit
def test_replies_only_template_has_no_main_variants_and_stays_reply_source():
    from bot.services.template_utils import (
        filter_full_templates,
        filter_reply_source_templates,
        is_replies_only_template,
        template_text_variants,
    )

    full = {
        "id": 1,
        "main_comment": "main",
        "reply1": "reply",
        "settings": {"template_type": "full", "text_variants": ["a", "b", "c"]},
    }
    replies_only = {
        "id": 2,
        "main_comment": "__replies_only_template__",
        "reply1": "reply only",
        "settings": {"template_type": "replies_only", "replies": ["reply only"]},
    }

    assert is_replies_only_template(replies_only)
    assert template_text_variants(replies_only) == []
    assert [template["id"] for template in filter_full_templates([full, replies_only])] == [1]
    assert [template["id"] for template in filter_reply_source_templates([full, replies_only])] == [1, 2]
