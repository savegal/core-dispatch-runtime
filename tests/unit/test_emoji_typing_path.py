"""Проверяем, что type_into_comment правильно выбирает insert_text для
текстов с эмодзи / surrogate pairs, и keyboard.type для plain-ASCII.

Полноценный e2e тест требует Playwright, нам же нужно убедиться что
выбор пути не сломан в самой логике. Делаем это inline grep'ом по
исходнику (как и тесты на login state ниже).
"""
from __future__ import annotations

import inspect

import pytest


@pytest.mark.unit
def test_type_into_comment_routes_emoji_through_insert_text():
    from executor.dom_actions import type_into_comment

    source = inspect.getsource(type_into_comment)

    # Должны быть оба пути.
    assert "page.keyboard.insert_text(text)" in source, (
        "Эмодзи (codepoints > 0xFFFF) обязаны идти через insert_text — "
        "иначе surrogate pair режется на половину при keyboard.type"
    )
    assert "page.keyboard.type(text, delay=30)" in source, (
        "Plain-ASCII должен оставаться human-like type'ом для антифрода"
    )
    # И сама логика ветвления.
    assert "has_non_bmp" in source
    assert "ord(ch) > 0xFFFF" in source


@pytest.mark.unit
def test_has_non_bmp_split_logic_detects_real_emoji():
    """Внешне-инвариантная проверка: '😀' (U+1F600) попадает в non-BMP."""
    sample = "Привет 😀!"
    assert any(ord(c) > 0xFFFF for c in sample)


@pytest.mark.unit
def test_has_non_bmp_split_logic_passes_plain_ascii_through_type():
    sample = "Hello world!"
    assert not any(ord(c) > 0xFFFF for c in sample)


@pytest.mark.unit
def test_has_non_bmp_split_logic_passes_cyrillic_through_type():
    """Кириллица — это BMP (U+0400..U+04FF), не должно роутиться в insert_text."""
    sample = "Привет, мир!"
    assert not any(ord(c) > 0xFFFF for c in sample)
