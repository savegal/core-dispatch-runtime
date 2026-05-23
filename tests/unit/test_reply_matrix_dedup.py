"""Проверяем dedup в reply matrix — _reply_with_variant_on_all_mains.

При отсутствии parent_comment_id (TikTok не отдаёт cid в DOM) дедуп parent'ов
шёл через normalize_text, который убирает ZWJ. Это значило что два main с
одинаковым «видимым» текстом но разными ZWJ-эмодзи приравнивались, и второй
не получал reply. Фикс — использовать _dedup_key (ZWJ-safe).
"""
import inspect

import pytest


@pytest.mark.unit
def test_reply_matrix_uses_dedup_key_not_normalize_text():
    """В цикле по main_texts dedup_key строится через _dedup_key, не через
    normalize_text — иначе ZWJ-эмодзи объединяются."""
    from executor import user_schema
    src = inspect.getsource(user_schema._reply_with_variant_on_all_mains)
    assert "_dedup_key(anchor_text)" in src, (
        "reply matrix должен использовать _dedup_key для anchor_text-fallback'а"
    )
    assert "normalize_text(anchor_text)" not in src, (
        "normalize_text удаляет ZWJ — разные ZWJ-семьи попадут в один dedup"
    )


@pytest.mark.unit
def test_dedup_key_distinguishes_zwj_families():
    """Sanity-check самой функции _dedup_key."""
    from executor.user_schema import _dedup_key

    family_zwj = "Семья \U0001F468‍\U0001F469‍\U0001F467"
    family_plain = "Семья \U0001F468\U0001F469\U0001F467"
    assert _dedup_key(family_zwj) != _dedup_key(family_plain)


@pytest.mark.unit
def test_parallel_commenting_mode_never_passes_reply_fallback_mains():
    from executor import user_schema
    src = inspect.getsource(user_schema.execute_user_schema_parallel)

    assert "fallback_main_texts=None" in src
    assert "fallback_allowed=False" in src


@pytest.mark.unit
def test_parallel_commenting_mode_batches_replies_by_three():
    from executor import user_schema
    src = inspect.getsource(user_schema.execute_user_schema_parallel)

    assert "batch = pending_reply_variants[:3]" in src
    assert "pending_reply_variants = pending_reply_variants[3:]" in src


@pytest.mark.unit
def test_executor_selects_parallel_user_schema_runner_from_commenting_mode():
    from executor import executor

    src = inspect.getsource(executor.process_task)

    assert "get_commenting_mode" in src
    assert "execute_user_schema_parallel" in src
    assert "if commenting_mode == COMMENTING_MODE_PARALLEL" in src
    assert "else execute_user_schema_full" in src
