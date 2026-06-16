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
def test_parallel_commenting_mode_uses_dynamic_reply_worker_pool():
    from executor import user_schema
    src = inspect.getsource(user_schema.execute_user_schema_parallel)

    assert "asyncio.FIRST_COMPLETED" in src
    assert "inflight_reply_variants" in src
    assert "pending_reply_variants.insert(0, (item.variant_index, item.text))" in src
    assert "pending_reply_variants = retry_variants + pending_reply_variants" in src


@pytest.mark.unit
def test_parallel_commenting_uses_phase_specific_worker_and_proxy_limits():
    from executor import user_schema
    src = inspect.getsource(user_schema.execute_user_schema_parallel)

    assert "PARALLEL_MAIN_WORKERS" in src
    assert "PARALLEL_MAIN_MAX_ACTIVE_BROWSERS_PER_PROXY" in src
    assert "PARALLEL_REPLY_MAX_ACTIVE_BROWSERS_PER_PROXY" in src
    assert "max_active_main_browsers_per_proxy" in src
    assert "max_active_reply_browsers_per_proxy" in src
    assert "len(active_main_tasks) < main_worker_target" in src


@pytest.mark.unit
def test_parallel_reply_default_starts_two_variant_workers():
    from config.settings import Settings
    from executor import user_schema
    src = inspect.getsource(user_schema.execute_user_schema_parallel)

    assert Settings().PARALLEL_REPLY_VARIANT_WORKERS == 2
    assert '_positive_int_setting("PARALLEL_REPLY_VARIANT_WORKERS", 2)' in src


@pytest.mark.unit
def test_parallel_commenting_continues_replies_after_partial_mains():
    from executor import user_schema
    src = inspect.getsource(user_schema.execute_user_schema_parallel)

    assert "parallel_main_incomplete_replies_continuing" in src
    assert "parallel_main_incomplete_replies_blocked" not in src
    assert "main_partial_replies_blocked" not in src


@pytest.mark.unit
def test_parallel_main_no_free_account_is_temporary_while_browser_active():
    from executor import user_schema
    src = inspect.getsource(user_schema.execute_user_schema_parallel)

    assert "if candidate is None:" in src
    assert "active_main_proxy_counts" in src
    assert "temporary_main_selection_error" in src


@pytest.mark.unit
def test_parallel_commenting_mode_does_not_abort_on_one_exhausted_reply_variant():
    from executor import user_schema
    src = inspect.getsource(user_schema.execute_user_schema_parallel)

    assert "continuing remaining variants" in src
    assert "fatal_reply_error" in src
    assert "result.error = fatal_reply_error.error" in src
    assert "result.error = terminal_reply_error.error" not in src


@pytest.mark.unit
def test_executor_selects_parallel_user_schema_runner_from_commenting_mode():
    from executor import executor

    src = inspect.getsource(executor.process_task)

    assert "get_commenting_mode" in src
    assert "execute_user_schema_parallel" in src
    assert "if commenting_mode == COMMENTING_MODE_PARALLEL" in src
    assert "else execute_user_schema_full" in src
