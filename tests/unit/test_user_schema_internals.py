"""Tests for the internal helpers added during the self-review pass.

Здесь покрываем:
- _dedup_variants — стабильный порядок + case-insensitive dedup.
- _next_inter_publish_delay — экспоненциальный рост с jitter.
- _is_critical_publish_failure — early-stop логика серии.
- _looks_like_proxy_failure — детекция «это прокси-проблема, а не сессия».
- _wait_for_url_settled — about:blank race protection.
"""
from __future__ import annotations

import asyncio
import inspect
import time
from contextlib import contextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest


# ---------------------------------------------------------------------------
# dedup
# ---------------------------------------------------------------------------

@pytest.mark.unit
def test_dedup_variants_preserves_order_and_removes_case_dupes():
    from executor.user_schema import _dedup_variants
    assert _dedup_variants(["Hello", "HELLO", "", "world", "Hello"]) == ["Hello", "world"]


@pytest.mark.unit
def test_dedup_variants_handles_unicode():
    from executor.user_schema import _dedup_variants
    # Эмодзи + кириллица не должны конфликтовать с дедуп-нормализацией.
    out = _dedup_variants(["привет 👋", "ПРИВЕТ 👋", "пока 👋"])
    assert out == ["привет 👋", "пока 👋"]


# ---------------------------------------------------------------------------
# exponential delay
# ---------------------------------------------------------------------------

@pytest.mark.unit
def test_next_inter_publish_delay_grows_per_publication():
    """Main-варианты идут с короткой стабильной паузой 4-7 секунд."""
    from executor.user_schema import _next_inter_publish_delay

    for _ in range(30):
        assert 4.0 <= _next_inter_publish_delay(0) <= 7.0
        assert 4.0 <= _next_inter_publish_delay(1) <= 7.0
        assert 4.0 <= _next_inter_publish_delay(2) <= 7.0


# ---------------------------------------------------------------------------
# critical-failure detection
# ---------------------------------------------------------------------------

@pytest.mark.unit
@pytest.mark.parametrize(
    "status,reason,expected",
    [
        ("redirected", None, True),
        ("dom_error", "captcha_challenge:slider", True),
        ("dom_error", "input_not_found: timeout", True),
        ("dom_error", "input_not_found_after_captcha_clear: timeout", True),
        ("dom_error", "post_button_not_found", True),
        ("dom_error", "post_button_stayed_disabled", True),
        ("failed", "stealth_reject_empty_body", False),
        ("timeout", "no_network_request_after_post_click", False),
        ("dom_error", "typing_failed", False),
    ],
)
def test_is_critical_publish_failure(status, reason, expected):
    from executor.user_schema import _is_critical_publish_failure

    result = SimpleNamespace(status=status, error_reason=reason)
    assert _is_critical_publish_failure(result) is expected


@pytest.mark.unit
@pytest.mark.asyncio
async def test_find_main_anchors_batch_uses_single_author_fallback(monkeypatch):
    from executor import user_schema

    async def _fake_batch(**_kwargs):
        return {}, {
            "author_fallback_candidates": {
                0: [
                    {
                        "raw_text": "target comment",
                        "comment_id": None,
                        "author": "found_user",
                        "scroll": 2,
                        "position": 4,
                        "scroll_top": 1200,
                        "fallback_reason": "author_mismatch",
                    }
                ]
            }
        }

    monkeypatch.setattr(user_schema, "find_comments_in_list_batch", _fake_batch)

    anchors, _info = await user_schema._find_main_anchors_batch(
        page=object(),
        mains=[
            user_schema.PublishedMain(
                text="target comment",
                comment_id=None,
                network_confirmed=False,
                local_confirmed=True,
                method="engagement_only_anchor",
                account_id=0,
                tiktok_username="target_user",
            )
        ],
        require_author_match=True,
        allow_author_fallback=True,
    )

    assert len(anchors) == 1
    assert anchors[0].found_by == "text_author_fallback"
    assert anchors[0].requested_author == "target_user"
    assert anchors[0].matched_author == "found_user"
    assert anchors[0].author_fallback_used is True


@pytest.mark.unit
@pytest.mark.asyncio
async def test_find_main_anchors_batch_requires_exact_text_when_author_matching(monkeypatch):
    from executor import user_schema

    calls = []

    async def _fake_batch(**kwargs):
        calls.append(kwargs)
        return {}, {}

    monkeypatch.setattr(user_schema, "find_comments_in_list_batch", _fake_batch)

    await user_schema._find_main_anchors_batch(
        page=object(),
        mains=[
            user_schema.PublishedMain(
                text="target comment",
                comment_id=None,
                network_confirmed=False,
                local_confirmed=True,
                method="engagement_only_anchor",
                account_id=0,
                tiktok_username="target_user",
            )
        ],
        require_author_match=True,
        allow_author_fallback=False,
    )

    assert calls
    assert calls[0]["min_match_ratio"] == 1.0


@pytest.mark.unit
@pytest.mark.asyncio
async def test_find_main_anchors_batch_rejects_ambiguous_author_fallback(monkeypatch):
    from executor import user_schema

    async def _fake_batch(**_kwargs):
        return {}, {
            "author_fallback_candidates": {
                0: [
                    {"raw_text": "target comment", "author": "one", "position": 1, "scroll_top": 10},
                    {"raw_text": "target comment", "author": "two", "position": 2, "scroll_top": 20},
                ]
            }
        }

    monkeypatch.setattr(user_schema, "find_comments_in_list_batch", _fake_batch)

    anchors, _info = await user_schema._find_main_anchors_batch(
        page=object(),
        mains=[
            user_schema.PublishedMain(
                text="target comment",
                comment_id=None,
                network_confirmed=False,
                local_confirmed=True,
                method="engagement_only_anchor",
                account_id=0,
                tiktok_username="target_user",
            )
        ],
        require_author_match=True,
        allow_author_fallback=True,
    )

    assert anchors == []


@pytest.mark.unit
def test_reply_publish_no_network_response_after_request_counts_as_submitted_unconfirmed():
    from executor.publish_action import PublishResult
    from executor.user_schema import _reply_publish_submitted_unconfirmed

    result = PublishResult(
        status="timeout",
        error_reason="no_network_response_after_publish_request",
        post_was_enabled_at_click=True,
        network_request_attempted=True,
    )

    assert _reply_publish_submitted_unconfirmed(result) is True


@pytest.mark.unit
def test_looks_like_tiktok_comment_id_rejects_dom_wrapper_ids():
    from executor.user_schema import _looks_like_tiktok_comment_id

    assert _looks_like_tiktok_comment_id("7641761192486194454") is True
    assert _looks_like_tiktok_comment_id('wrapper-"bad"') is False
    assert _looks_like_tiktok_comment_id("comment-7641761192486194454") is False
    assert _looks_like_tiktok_comment_id("") is False


@pytest.mark.unit
def test_reply_publish_no_network_request_after_click_does_not_count_as_submitted():
    from executor.publish_action import PublishResult
    from executor.user_schema import _reply_publish_submitted_unconfirmed

    result = PublishResult(
        status="timeout",
        error_reason="no_network_request_after_post_click",
        post_was_enabled_at_click=True,
        network_request_attempted=False,
    )

    assert _reply_publish_submitted_unconfirmed(result) is False


@pytest.mark.unit
@pytest.mark.asyncio
async def test_execute_user_schema_full_stops_before_main_attempt(monkeypatch):
    from executor import user_schema

    def fail_if_called(*_args, **_kwargs):
        raise AssertionError("main account selection should not run after stop")

    monkeypatch.setattr(user_schema, "get_free_account_for_main_comment", fail_if_called)

    result = await user_schema.execute_user_schema_full(
        SimpleNamespace(),
        SimpleNamespace(settings={}, reply1=None, reply2=None, reply3=None),
        "https://www.tiktok.com/@demo/video/1234567890123456789",
        ["main one"],
        299,
        boost_enabled=False,
        boost_quantity=0,
        stop_requested=lambda: True,
    )

    assert result.error == "stopped_by_user"
    assert result.published_mains == []


@pytest.mark.unit
def test_single_parent_search_has_timeout_guard():
    from executor import user_schema

    source = inspect.getsource(user_schema._find_main_anchor)

    assert "asyncio.wait_for" in source
    assert "single parent search timed out" in source


@pytest.mark.unit
@pytest.mark.asyncio
async def test_wait_for_searchable_comment_surface_requires_two_stable_samples(monkeypatch):
    from executor import user_schema

    stable_surface = {
        "input_visible": True,
        "comment_input_visible": True,
        "list_visible": True,
        "comment_items_visible": 15,
        "list_source": {"items": 15},
    }
    states = [
        {
            "input_visible": False,
            "comment_input_visible": False,
            "list_visible": False,
            "comment_items_visible": 0,
            "list_source": None,
        },
        stable_surface,
        stable_surface,
    ]
    sleeps = []
    traces = []

    async def fake_get_surface(_page):
        return states.pop(0) if states else stable_surface

    async def fake_sleep(seconds):
        sleeps.append(seconds)

    def fake_trace(stage, **kwargs):
        traces.append((stage, kwargs))

    monkeypatch.setattr(user_schema, "get_comment_surface_state", fake_get_surface)
    monkeypatch.setattr(user_schema.asyncio, "sleep", fake_sleep)
    monkeypatch.setattr(user_schema, "_log_user_trace", fake_trace)

    ok = await user_schema._wait_for_searchable_comment_surface(
        SimpleNamespace(url="https://www.tiktok.com/@u/video/1"),
        expected_video_url="https://www.tiktok.com/@u/video/1",
        correlation_id="test_reply",
        phase="reply_parent_search",
        timeout_seconds=3.0,
    )

    assert ok is True
    assert [stage for stage, _ in traces] == ["comment_surface_search_ready"]
    assert len(sleeps) == 2


@pytest.mark.unit
@pytest.mark.asyncio
async def test_publish_all_variants_retries_input_not_found_on_same_account(monkeypatch):
    from executor import user_schema
    from executor.publish_action import PublishResult

    calls = []
    recovered = {"count": 0}

    async def fake_publish(_page, text, **kwargs):
        calls.append((text, kwargs.get("correlation_id")))
        if len(calls) == 1:
            return PublishResult(status="dom_error", error_reason="input_not_found: timeout")
        return PublishResult(
            status="success",
            comment_id="cid-1",
            network_captured=True,
            dom_strategy="data-e2e",
        )

    async def fake_recover(_page, *, correlation_id):
        recovered["count"] += 1
        assert correlation_id == "task_1_v1"

    async def fake_username(_page):
        return "main_user"

    monkeypatch.setattr(user_schema, "publish_comment_hybrid", fake_publish)
    monkeypatch.setattr(user_schema, "_recover_page_before_input_retry", fake_recover)
    monkeypatch.setattr(user_schema, "extract_tiktok_username", fake_username)
    monkeypatch.setattr(user_schema, "_register_runtime_success", lambda *_args, **_kwargs: None)

    account = SimpleNamespace(id=10)
    published, failure_reason = await user_schema._publish_all_variants_on_account(
        SimpleNamespace(),
        SimpleNamespace(),
        account,
        ["hello"],
        correlation_id="task_1",
    )

    assert recovered["count"] == 1
    assert calls == [("hello", "task_1_v1"), ("hello", "task_1_v1_input_retry")]
    assert failure_reason is None
    assert len(published) == 1
    assert published[0].text == "hello"
    assert published[0].comment_id == "cid-1"


@pytest.mark.unit
@pytest.mark.asyncio
async def test_publish_all_variants_returns_comment_ui_failure_reason(monkeypatch):
    from executor import user_schema
    from executor.publish_action import PublishResult

    async def fake_publish(_page, _text, **_kwargs):
        return PublishResult(status="dom_error", error_reason="input_not_found: comment_ui_not_found")

    async def fake_recover(_page, *, correlation_id):
        assert correlation_id == "task_2_v1"

    async def fake_username(_page):
        return "main_user"

    async def fake_find_comment(*_args, **_kwargs):
        return False, {}

    monkeypatch.setattr(user_schema, "publish_comment_hybrid", fake_publish)
    monkeypatch.setattr(user_schema, "_recover_page_before_input_retry", fake_recover)
    monkeypatch.setattr(user_schema, "extract_tiktok_username", fake_username)
    monkeypatch.setattr(user_schema, "find_comment_in_list", fake_find_comment)

    published, failure_reason = await user_schema._publish_all_variants_on_account(
        SimpleNamespace(),
        SimpleNamespace(),
        SimpleNamespace(id=10),
        ["hello", "world"],
        correlation_id="task_2",
    )

    assert published == []
    assert failure_reason == "input_not_found: comment_ui_not_found"


@pytest.mark.unit
@pytest.mark.asyncio
async def test_publish_all_variants_records_local_only_and_continues(monkeypatch):
    """stealth_reject_empty_body + network event captured + local DOM ⇒
    вариант записывается как local_only, серия продолжается на следующий
    variant. Публичность проверит observer-фаза дальше по флоу.

    NB (commit D): local_confirmed теперь требует хотя бы один из:
    post_was_enabled_at_click=True ИЛИ network_captured=True. Реальный
    stealth_reject_empty_body ловит network event с пустым телом, поэтому
    network_captured=True в этом сценарии — это правильный мок."""
    from executor import user_schema
    from executor.publish_action import PublishResult

    async def fake_publish(_page, _text, **_kwargs):
        return PublishResult(
            status="failed",
            error_reason="stealth_reject_empty_body",
            network_captured=True,  # real stealth reject = we saw the response
        )

    async def fake_username(_page):
        return "main_user"

    async def fake_find_comment(*_args, **_kwargs):
        return True, {"text_normalized": "hello"}

    async def fake_sleep(_seconds):
        return None

    runtime_success_calls = []

    def fake_register_success(_db, account, *, scope):
        runtime_success_calls.append((account.id, scope))

    monkeypatch.setattr(user_schema, "publish_comment_hybrid", fake_publish)
    monkeypatch.setattr(user_schema, "extract_tiktok_username", fake_username)
    monkeypatch.setattr(user_schema, "find_comment_in_list", fake_find_comment)
    monkeypatch.setattr(user_schema.asyncio, "sleep", fake_sleep)
    monkeypatch.setattr(user_schema, "_register_runtime_success", fake_register_success)

    published, failure_reason = await user_schema._publish_all_variants_on_account(
        SimpleNamespace(),
        SimpleNamespace(),
        SimpleNamespace(id=10),
        ["hello", "world"],
        correlation_id="task_3",
    )

    assert len(published) == 2
    assert [p.text for p in published] == ["hello", "world"]
    assert all(not p.network_confirmed for p in published)
    assert all(p.local_confirmed for p in published)
    assert runtime_success_calls == []
    # published непустой ⇒ failure_reason должен быть None.
    assert failure_reason is None


@pytest.mark.unit
@pytest.mark.asyncio
async def test_publish_all_variants_recovers_dom_comment_id_after_local_confirm(monkeypatch):
    from executor import user_schema
    from executor.publish_action import PublishResult

    async def fake_publish(_page, _text, **_kwargs):
        return PublishResult(
            status="failed",
            error_reason="stealth_reject_empty_body",
            network_captured=True,
            comment_id=None,
        )

    async def fake_username(_page):
        return "main_user"

    async def fake_find_comment(*_args, **_kwargs):
        return True, {"comment_id": "dom-cid-777", "text_normalized": "hello"}

    monkeypatch.setattr(user_schema, "publish_comment_hybrid", fake_publish)
    monkeypatch.setattr(user_schema, "extract_tiktok_username", fake_username)
    monkeypatch.setattr(user_schema, "find_comment_in_list", fake_find_comment)

    published, failure_reason = await user_schema._publish_all_variants_on_account(
        SimpleNamespace(),
        SimpleNamespace(),
        SimpleNamespace(id=10),
        ["hello"],
        correlation_id="task_local_cid",
    )

    assert failure_reason is None
    assert len(published) == 1
    assert published[0].local_confirmed is True
    assert published[0].comment_id == "dom-cid-777"


@pytest.mark.unit
@pytest.mark.asyncio
async def test_publish_all_variants_cooldowns_account_after_partial_success_then_captcha(monkeypatch):
    from executor import user_schema
    from executor.publish_action import PublishResult

    calls = {"publish": 0}
    failures = []
    captchas = []

    async def fake_publish(_page, _text, **_kwargs):
        calls["publish"] += 1
        if calls["publish"] == 1:
            return PublishResult(
                status="success",
                comment_id="cid-1",
                network_captured=True,
                dom_strategy="data-e2e",
            )
        return PublishResult(status="failed", error_reason="captcha_challenge:slider")

    async def fake_username(_page):
        return "main_user"

    async def fake_sleep(_seconds):
        return None

    def fake_register_success(*_args, **_kwargs):
        return None

    def fake_register_failure(_db, account, reason):
        failures.append((account.id, reason))

    def fake_register_captcha(_db, account, reason):
        captchas.append((account.id, reason))

    monkeypatch.setattr(user_schema, "publish_comment_hybrid", fake_publish)
    monkeypatch.setattr(user_schema, "extract_tiktok_username", fake_username)
    monkeypatch.setattr(user_schema.asyncio, "sleep", fake_sleep)
    monkeypatch.setattr(user_schema, "_register_runtime_success", fake_register_success)
    monkeypatch.setattr(user_schema, "_register_runtime_failure", fake_register_failure)
    monkeypatch.setattr(user_schema, "_register_captcha_session", fake_register_captcha)

    account = SimpleNamespace(id=19)
    published, failure_reason = await user_schema._publish_all_variants_on_account(
        SimpleNamespace(),
        SimpleNamespace(),
        account,
        ["hello", "world", "again"],
        correlation_id="task_partial_captcha",
    )

    assert len(published) == 1
    assert calls["publish"] == 2
    assert failure_reason is None
    assert failures == []
    assert captchas == [(19, "captcha_challenge:slider")]


@pytest.mark.unit
@pytest.mark.asyncio
async def test_publish_all_variants_cooldowns_account_when_first_publish_hits_captcha(monkeypatch):
    from executor import user_schema
    from executor.publish_action import PublishResult

    captchas = []

    async def fake_publish(_page, _text, **_kwargs):
        return PublishResult(status="failed", error_reason="captcha_challenge:slider")

    async def fake_username(_page):
        return "main_user"

    def fake_register_captcha(_db, account, reason):
        captchas.append((account.id, reason))

    monkeypatch.setattr(user_schema, "publish_comment_hybrid", fake_publish)
    monkeypatch.setattr(user_schema, "extract_tiktok_username", fake_username)
    monkeypatch.setattr(user_schema, "_register_captcha_session", fake_register_captcha)

    published, failure_reason = await user_schema._publish_all_variants_on_account(
        SimpleNamespace(),
        SimpleNamespace(),
        SimpleNamespace(id=21),
        ["hello", "world"],
        correlation_id="task_first_captcha",
    )

    assert published == []
    assert failure_reason == "captcha_challenge:slider"
    assert captchas == [(21, "captcha_challenge:slider")]


@pytest.mark.unit
@pytest.mark.asyncio
async def test_publish_all_variants_rejects_local_only_when_post_disabled_and_no_network(monkeypatch):
    """Commit D strict gate: synthetic-dispatch on disabled Post + no network
    + local DOM match ⇒ NOT local_confirmed. The text in DOM is most likely
    sitting in the composer/wrapper, not in any published comment list.
    Series continues to the next variant without recording anything."""
    from executor import user_schema
    from executor.publish_action import PublishResult

    async def fake_publish(_page, _text, **_kwargs):
        # Synthetic dispatch on disabled Post, no network response.
        return PublishResult(
            status="failed",
            error_reason="submitted_unverified_disabled_post",
            network_captured=False,
            post_was_enabled_at_click=False,
        )

    async def fake_username(_page):
        return "main_user"

    async def fake_find_comment(*_args, **_kwargs):
        # Local DOM probe finds the text — but it's a false positive (composer
        # wrapper still holds it; React never committed).
        return True, {"text_normalized": "hello"}

    async def fake_sleep(_seconds):
        return None

    monkeypatch.setattr(user_schema, "publish_comment_hybrid", fake_publish)
    monkeypatch.setattr(user_schema, "extract_tiktok_username", fake_username)
    monkeypatch.setattr(user_schema, "find_comment_in_list", fake_find_comment)
    monkeypatch.setattr(user_schema.asyncio, "sleep", fake_sleep)

    published, failure_reason = await user_schema._publish_all_variants_on_account(
        SimpleNamespace(),
        SimpleNamespace(),
        SimpleNamespace(id=11),
        ["hello", "world"],
        correlation_id="task_4",
    )

    # Strict gate: nothing should be recorded.
    assert published == []
    # Failure reason from the last variant should reflect the unverified state.
    assert failure_reason == "submitted_unverified_disabled_post"


@pytest.mark.unit
@pytest.mark.asyncio
async def test_publish_all_variants_does_not_local_confirm_without_submit(monkeypatch):
    from executor import user_schema
    from executor.publish_action import PublishResult

    async def fake_publish(_page, _text, **_kwargs):
        return PublishResult(status="dom_error", error_reason="post_button_stayed_disabled")

    async def fake_username(_page):
        return "main_user"

    async def fake_find_comment(*_args, **_kwargs):
        raise AssertionError("local DOM verification must not run when Post was disabled")

    monkeypatch.setattr(user_schema, "publish_comment_hybrid", fake_publish)
    monkeypatch.setattr(user_schema, "extract_tiktok_username", fake_username)
    monkeypatch.setattr(user_schema, "find_comment_in_list", fake_find_comment)

    published, failure_reason = await user_schema._publish_all_variants_on_account(
        SimpleNamespace(),
        SimpleNamespace(),
        SimpleNamespace(id=10),
        ["hello", "world"],
        correlation_id="task_disabled",
    )

    assert published == []
    assert failure_reason == "post_button_stayed_disabled"


@pytest.mark.unit
@pytest.mark.asyncio
async def test_reply_account_does_not_publish_fallback_on_first_anchor_miss(monkeypatch):
    from executor import user_schema

    async def fake_create_page_for_account(_account):
        return SimpleNamespace(), SimpleNamespace()

    async def fake_login_ready(*_args, **_kwargs):
        return True, None

    async def fake_find_main_anchor(*_args, **_kwargs):
        return user_schema.MainAnchor(matched_text=None, parent_comment_id=None)

    async def fake_batch(*_args, **_kwargs):
        return [], {"found_count": 0, "max_seen_rows": 1, "last_row_count": 1}

    async def fake_publish_all(_db, _page, account, text_variants, **_kwargs):
        return [
            user_schema.PublishedMain(
                text=text,
                comment_id=f"cid-{idx}",
                network_confirmed=True,
                local_confirmed=True,
                method="hybrid_dom",
                account_id=account.id,
                tiktok_username="fallback_user",
            )
            for idx, text in enumerate(text_variants, start=1)
        ], None

    async def fake_sleep(_seconds):
        return None

    monkeypatch.setattr(user_schema, "create_page_for_account", fake_create_page_for_account)
    monkeypatch.setattr(user_schema, "_ensure_account_login_ready", fake_login_ready)
    monkeypatch.setattr(user_schema, "needs_enrichment", lambda _account: False)
    monkeypatch.setattr(user_schema, "_ensure_page_on_target_media", AsyncMock(return_value=True))
    monkeypatch.setattr(user_schema, "_open_comment_list_with_safe_retry", AsyncMock(return_value=True))
    monkeypatch.setattr(user_schema, "_wait_for_searchable_comment_surface", AsyncMock(return_value=True))
    monkeypatch.setattr(user_schema, "_find_main_anchors_batch", fake_batch)
    monkeypatch.setattr(user_schema, "_find_main_anchor", fake_find_main_anchor)
    monkeypatch.setattr(user_schema, "_publish_all_variants_on_account", fake_publish_all)
    monkeypatch.setattr(user_schema.asyncio, "sleep", fake_sleep)

    result = await user_schema._reply_with_variant_on_all_mains(
        SimpleNamespace(),
        SimpleNamespace(id=77, proxy=None),
        "https://www.tiktok.com/@demo/video/1234567890123456789",
        ["old local main"],
        "reply text",
        variant_index=1,
        correlation_id="task_1_reply",
        fallback_main_texts=["main one", "main two", "main three"],
    )

    assert result.searched is True
    assert result.confident_empty_search is True
    assert result.fallback_attempted is False
    assert result.fallback_mains == []


@pytest.mark.unit
@pytest.mark.asyncio
async def test_reply_visible_comment_list_without_mains_publishes_fallback_when_allowed(monkeypatch):
    from executor import user_schema

    class FakePage:
        url = "https://www.tiktok.com/@demo/video/1234567890123456789"

        def is_closed(self):
            return False

        async def close(self):
            return None

    class FakeContext:
        async def close(self):
            return None

    async def fake_create_page_for_account(_account):
        return FakeContext(), FakePage()

    async def fake_login_ready(*_args, **_kwargs):
        return True, None

    async def fake_batch(*_args, **_kwargs):
        return [], {"found_count": 0, "max_seen_rows": 3}

    async def fake_find_main_anchor(*_args, **_kwargs):
        return user_schema.MainAnchor(matched_text=None, parent_comment_id=None)

    async def fake_publish_all(_db, _page, account, text_variants, **_kwargs):
        return [
            user_schema.PublishedMain(
                text=text,
                comment_id=f"cid-{idx}",
                network_confirmed=True,
                local_confirmed=True,
                method="hybrid_dom",
                account_id=account.id,
                tiktok_username="fallback_user",
            )
            for idx, text in enumerate(text_variants, start=1)
        ], None

    async def fake_sleep(_seconds):
        return None

    monkeypatch.setattr(user_schema, "create_page_for_account", fake_create_page_for_account)
    monkeypatch.setattr(user_schema, "_ensure_account_login_ready", fake_login_ready)
    monkeypatch.setattr(user_schema, "needs_enrichment", lambda _account: False)
    monkeypatch.setattr(user_schema, "_ensure_page_on_target_media", AsyncMock(return_value=True))
    monkeypatch.setattr(user_schema, "_open_comment_list_with_safe_retry", AsyncMock(return_value=True))
    monkeypatch.setattr(user_schema, "_wait_for_searchable_comment_surface", AsyncMock(return_value=True))
    monkeypatch.setattr(user_schema, "_find_main_anchors_batch", fake_batch)
    monkeypatch.setattr(user_schema, "_find_main_anchor", fake_find_main_anchor)
    monkeypatch.setattr(user_schema, "_publish_all_variants_on_account", fake_publish_all)
    monkeypatch.setattr(user_schema.asyncio, "sleep", fake_sleep)

    result = await user_schema._reply_with_variant_on_all_mains(
        SimpleNamespace(),
        SimpleNamespace(id=77, proxy=None),
        "https://www.tiktok.com/@demo/video/1234567890123456789",
        [
            user_schema.PublishedMain(
                text="main one",
                comment_id="cid-main-1",
                network_confirmed=True,
                local_confirmed=True,
                method="hybrid_dom",
                account_id=10,
                tiktok_username=None,
            ),
            user_schema.PublishedMain(
                text="main two",
                comment_id="cid-main-2",
                network_confirmed=True,
                local_confirmed=True,
                method="hybrid_dom",
                account_id=10,
                tiktok_username=None,
            ),
        ],
        "reply text",
        variant_index=1,
        correlation_id="task_visible_no_mains",
        fallback_main_texts=["main one", "main two", "main three"],
        fallback_allowed=True,
    )

    assert result.searched is True
    assert result.fallback_attempted is True
    assert [p.text for p in result.fallback_mains] == ["main one", "main two", "main three"]
    assert result.replies[0].method == "fallback_main"


@pytest.mark.unit
@pytest.mark.asyncio
async def test_reply_visible_comment_list_search_timeouts_count_as_confident_empty(monkeypatch):
    from executor import user_schema

    class FakePage:
        url = "https://www.tiktok.com/@demo/video/1234567890123456789"

        def is_closed(self):
            return False

        async def close(self):
            return None

    class FakeContext:
        async def close(self):
            return None

    async def fake_create_page_for_account(_account):
        return FakeContext(), FakePage()

    async def fake_login_ready(*_args, **_kwargs):
        return True, None

    async def fake_batch(*_args, **_kwargs):
        return [], {"error": "batch_search_timeout"}

    async def fake_find_main_anchor(*_args, **_kwargs):
        return user_schema.MainAnchor(
            matched_text=None,
            parent_comment_id=None,
            error="single_search_timeout",
        )

    async def fail_publish_all(*_args, **_kwargs):
        raise AssertionError("first confident miss must not publish fallback main")

    async def fake_sleep(_seconds):
        return None

    monkeypatch.setattr(user_schema, "create_page_for_account", fake_create_page_for_account)
    monkeypatch.setattr(user_schema, "_ensure_account_login_ready", fake_login_ready)
    monkeypatch.setattr(user_schema, "needs_enrichment", lambda _account: False)
    monkeypatch.setattr(user_schema, "_ensure_page_on_target_media", AsyncMock(return_value=True))
    monkeypatch.setattr(user_schema, "_open_comment_list_with_safe_retry", AsyncMock(return_value=True))
    monkeypatch.setattr(user_schema, "_wait_for_searchable_comment_surface", AsyncMock(return_value=True))
    monkeypatch.setattr(user_schema, "_find_main_anchors_batch", fake_batch)
    monkeypatch.setattr(user_schema, "_find_main_anchor", fake_find_main_anchor)
    monkeypatch.setattr(user_schema, "_publish_all_variants_on_account", fail_publish_all)
    monkeypatch.setattr(user_schema.asyncio, "sleep", fake_sleep)

    result = await user_schema._reply_with_variant_on_all_mains(
        SimpleNamespace(),
        SimpleNamespace(id=77, proxy=None),
        "https://www.tiktok.com/@demo/video/1234567890123456789",
        [
            user_schema.PublishedMain(
                text="main one",
                comment_id="cid-main-1",
                network_confirmed=True,
                local_confirmed=True,
                method="hybrid_dom",
                account_id=10,
                tiktok_username=None,
            ),
            user_schema.PublishedMain(
                text="main two",
                comment_id="cid-main-2",
                network_confirmed=True,
                local_confirmed=True,
                method="hybrid_dom",
                account_id=10,
                tiktok_username=None,
            ),
        ],
        "reply text",
        variant_index=1,
        correlation_id="task_visible_timeout_no_mains",
        fallback_main_texts=["main one", "main two", "main three"],
    )

    assert result.searched is True
    assert result.confident_empty_search is True
    assert result.fallback_attempted is False
    assert result.fallback_mains == []


@pytest.mark.unit
@pytest.mark.asyncio
async def test_reply_visible_comment_list_search_timeouts_publish_fallback_when_allowed(monkeypatch):
    from executor import user_schema

    class FakePage:
        url = "https://www.tiktok.com/@demo/video/1234567890123456789"

        def is_closed(self):
            return False

        async def close(self):
            return None

    class FakeContext:
        async def close(self):
            return None

    async def fake_create_page_for_account(_account):
        return FakeContext(), FakePage()

    async def fake_login_ready(*_args, **_kwargs):
        return True, None

    async def fake_batch(*_args, **_kwargs):
        return [], {"error": "batch_search_timeout"}

    async def fake_find_main_anchor(*_args, **_kwargs):
        return user_schema.MainAnchor(
            matched_text=None,
            parent_comment_id=None,
            error="single_search_timeout",
        )

    async def fake_publish_all(_db, _page, account, text_variants, **_kwargs):
        return [
            user_schema.PublishedMain(
                text=text,
                comment_id=f"cid-{idx}",
                network_confirmed=True,
                local_confirmed=True,
                method="hybrid_dom",
                account_id=account.id,
                tiktok_username="fallback_user",
            )
            for idx, text in enumerate(text_variants, start=1)
        ], None

    async def fake_sleep(_seconds):
        return None

    monkeypatch.setattr(user_schema, "create_page_for_account", fake_create_page_for_account)
    monkeypatch.setattr(user_schema, "_ensure_account_login_ready", fake_login_ready)
    monkeypatch.setattr(user_schema, "needs_enrichment", lambda _account: False)
    monkeypatch.setattr(user_schema, "_ensure_page_on_target_media", AsyncMock(return_value=True))
    monkeypatch.setattr(user_schema, "_open_comment_list_with_safe_retry", AsyncMock(return_value=True))
    monkeypatch.setattr(user_schema, "_wait_for_searchable_comment_surface", AsyncMock(return_value=True))
    monkeypatch.setattr(user_schema, "_find_main_anchors_batch", fake_batch)
    monkeypatch.setattr(user_schema, "_find_main_anchor", fake_find_main_anchor)
    monkeypatch.setattr(user_schema, "_publish_all_variants_on_account", fake_publish_all)
    monkeypatch.setattr(user_schema.asyncio, "sleep", fake_sleep)

    result = await user_schema._reply_with_variant_on_all_mains(
        SimpleNamespace(),
        SimpleNamespace(id=77, proxy=None),
        "https://www.tiktok.com/@demo/video/1234567890123456789",
        [
            user_schema.PublishedMain(
                text="main one",
                comment_id="cid-main-1",
                network_confirmed=True,
                local_confirmed=True,
                method="hybrid_dom",
                account_id=10,
                tiktok_username=None,
            ),
            user_schema.PublishedMain(
                text="main two",
                comment_id="cid-main-2",
                network_confirmed=True,
                local_confirmed=True,
                method="hybrid_dom",
                account_id=10,
                tiktok_username=None,
            ),
        ],
        "reply text",
        variant_index=1,
        correlation_id="task_visible_timeout_fallback",
        fallback_main_texts=["main one", "main two", "main three"],
        fallback_allowed=True,
    )

    assert result.searched is True
    assert result.fallback_attempted is True
    assert [p.text for p in result.fallback_mains] == ["main one", "main two", "main three"]
    assert result.replies[0].method == "fallback_main"


@pytest.mark.unit
@pytest.mark.asyncio
async def test_reply_visible_surface_not_open_does_not_count_as_confident_fallback(monkeypatch):
    from executor import user_schema

    class FakePage:
        url = "https://www.tiktok.com/@demo/video/1234567890123456789"

        def is_closed(self):
            return False

        async def close(self):
            return None

    class FakeContext:
        async def close(self):
            return None

    async def fake_create_page_for_account(_account):
        return FakeContext(), FakePage()

    async def fake_login_ready(*_args, **_kwargs):
        return True, None

    async def fake_batch(*_args, **_kwargs):
        return [], {"error": "comment_list_not_open"}

    async def fake_find_main_anchor(*_args, **_kwargs):
        return user_schema.MainAnchor(
            matched_text=None,
            parent_comment_id=None,
            error="comment_list_not_open",
        )

    async def fake_publish_all(_db, _page, account, text_variants, **_kwargs):
        return [
            user_schema.PublishedMain(
                text=text,
                comment_id=f"cid-{idx}",
                network_confirmed=True,
                local_confirmed=True,
                method="hybrid_dom",
                account_id=account.id,
                tiktok_username="fallback_user",
            )
            for idx, text in enumerate(text_variants, start=1)
        ], None

    async def fake_sleep(_seconds):
        return None

    monkeypatch.setattr(user_schema, "create_page_for_account", fake_create_page_for_account)
    monkeypatch.setattr(user_schema, "_ensure_account_login_ready", fake_login_ready)
    monkeypatch.setattr(user_schema, "needs_enrichment", lambda _account: False)
    monkeypatch.setattr(user_schema, "_ensure_page_on_target_media", AsyncMock(return_value=True))
    monkeypatch.setattr(user_schema, "_open_comment_list_with_safe_retry", AsyncMock(return_value=True))
    monkeypatch.setattr(user_schema, "_wait_for_searchable_comment_surface", AsyncMock(return_value=True))
    monkeypatch.setattr(user_schema, "_find_main_anchors_batch", fake_batch)
    monkeypatch.setattr(user_schema, "_find_main_anchor", fake_find_main_anchor)
    monkeypatch.setattr(user_schema, "_publish_all_variants_on_account", fake_publish_all)
    monkeypatch.setattr(user_schema.asyncio, "sleep", fake_sleep)

    result = await user_schema._reply_with_variant_on_all_mains(
        SimpleNamespace(),
        SimpleNamespace(id=77, proxy=None),
        "https://www.tiktok.com/@demo/video/1234567890123456789",
        [
            user_schema.PublishedMain(
                text="main one",
                comment_id="cid-main-1",
                network_confirmed=True,
                local_confirmed=True,
                method="hybrid_dom",
                account_id=10,
                tiktok_username=None,
            ),
            user_schema.PublishedMain(
                text="main two",
                comment_id="cid-main-2",
                network_confirmed=True,
                local_confirmed=True,
                method="hybrid_dom",
                account_id=10,
                tiktok_username=None,
            ),
        ],
        "reply text",
        variant_index=1,
        correlation_id="task_visible_not_open",
        fallback_main_texts=["main one", "main two", "main three"],
        fallback_allowed=True,
    )

    assert result.searched is True
    assert result.confident_empty_search is False
    assert result.fallback_attempted is False
    assert result.fallback_mains == []


@pytest.mark.unit
@pytest.mark.asyncio
async def test_reply_account_does_not_publish_fallback_when_comment_list_unavailable(monkeypatch):
    from executor import user_schema

    class FakePage:
        url = "https://www.tiktok.com/@demo/video/1234567890123456789"

        def is_closed(self):
            return False

        async def close(self):
            return None

    class FakeContext:
        async def close(self):
            return None

    async def fake_create_page_for_account(_account):
        return FakeContext(), FakePage()

    async def fake_login_ready(*_args, **_kwargs):
        return True, None

    async def fail_find_main_anchor(*_args, **_kwargs):
        raise AssertionError("parent search must not run when the comment list is unavailable")

    async def fail_publish_all(*_args, **_kwargs):
        raise AssertionError("fallback main must not publish when the comment list never opened")

    async def fake_sleep(_seconds):
        return None

    monkeypatch.setattr(user_schema, "create_page_for_account", fake_create_page_for_account)
    monkeypatch.setattr(user_schema, "_ensure_account_login_ready", fake_login_ready)
    monkeypatch.setattr(user_schema, "needs_enrichment", lambda _account: False)
    monkeypatch.setattr(user_schema, "_ensure_page_on_target_media", AsyncMock(return_value=True))
    monkeypatch.setattr(user_schema, "_open_comment_list_with_safe_retry", AsyncMock(return_value=False))
    monkeypatch.setattr(user_schema, "_find_main_anchor", fail_find_main_anchor)
    monkeypatch.setattr(user_schema, "_publish_all_variants_on_account", fail_publish_all)
    monkeypatch.setattr(user_schema.asyncio, "sleep", fake_sleep)

    result = await user_schema._reply_with_variant_on_all_mains(
        SimpleNamespace(),
        SimpleNamespace(id=77, proxy=None),
        "https://www.tiktok.com/@demo/video/1234567890123456789",
        ["old local main"],
        "reply text",
        variant_index=1,
        correlation_id="task_1_reply",
        fallback_main_texts=["main one", "main two", "main three"],
    )

    assert result.searched is False
    assert result.fallback_attempted is False
    assert result.fallback_mains == []
    assert len(result.replies) == 1
    assert result.replies[0].error == "comment_list_not_open"
    assert result.replies[0].attempted is False


@pytest.mark.unit
@pytest.mark.asyncio
async def test_reply_single_search_comment_list_loss_does_not_publish_fallback(monkeypatch):
    from executor import user_schema

    class FakePage:
        url = "https://www.tiktok.com/@demo/video/1234567890123456789"

        def is_closed(self):
            return False

        async def close(self):
            return None

    class FakeContext:
        async def close(self):
            return None

    async def fake_create_page_for_account(_account):
        return FakeContext(), FakePage()

    async def fake_login_ready(*_args, **_kwargs):
        return True, None

    async def fake_find_main_anchor(*_args, **_kwargs):
        return user_schema.MainAnchor(
            matched_text=None,
            parent_comment_id=None,
            error="comment_list_not_open",
        )

    async def fail_publish_all(*_args, **_kwargs):
        raise AssertionError("fallback main must not run when single-search loses the comment list")

    async def fake_sleep(_seconds):
        return None

    monkeypatch.setattr(user_schema, "create_page_for_account", fake_create_page_for_account)
    monkeypatch.setattr(user_schema, "_ensure_account_login_ready", fake_login_ready)
    monkeypatch.setattr(user_schema, "needs_enrichment", lambda _account: False)
    monkeypatch.setattr(user_schema, "_ensure_page_on_target_media", AsyncMock(return_value=True))
    monkeypatch.setattr(user_schema, "_open_comment_list_with_safe_retry", AsyncMock(return_value=True))
    monkeypatch.setattr(user_schema, "_find_main_anchor", fake_find_main_anchor)
    monkeypatch.setattr(user_schema, "_publish_all_variants_on_account", fail_publish_all)
    monkeypatch.setattr(user_schema.asyncio, "sleep", fake_sleep)

    result = await user_schema._reply_with_variant_on_all_mains(
        SimpleNamespace(),
        SimpleNamespace(id=77, proxy=None),
        "https://www.tiktok.com/@demo/video/1234567890123456789",
        [
            user_schema.PublishedMain(
                text="main one",
                comment_id="cid-main",
                network_confirmed=True,
                local_confirmed=True,
                method="hybrid_dom",
                account_id=10,
                tiktok_username=None,
            )
        ],
        "reply text",
        variant_index=1,
        correlation_id="task_single_list_loss",
        fallback_main_texts=["main one", "main two", "main three"],
    )

    assert result.searched is False
    assert result.fallback_attempted is False
    assert result.fallback_mains == []
    assert len(result.replies) == 1
    assert result.replies[0].error == "comment_list_not_open"
    assert result.replies[0].attempted is False


@pytest.mark.unit
@pytest.mark.asyncio
async def test_find_main_anchor_prefers_known_comment_id(monkeypatch):
    from executor import user_schema

    class FakeRoot:
        async def is_visible(self):
            return True

    class FakeCommentList:
        def __init__(self):
            self.queries = []

        async def is_visible(self):
            return True

        async def query_selector(self, selector):
            self.queries.append(selector)
            if 'data-cid="cid-123"' in selector:
                return FakeRoot()
            return None

    class FakePage:
        def __init__(self):
            self.comment_list = FakeCommentList()

        async def query_selector(self, selector):
            if "CommentList" in selector:
                return self.comment_list
            return None

    async def fail_text_search(*_args, **_kwargs):
        raise AssertionError("text search should not run when comment_id is known")

    monkeypatch.setattr(user_schema, "find_comment_in_list", fail_text_search)

    main = user_schema.PublishedMain(
        text="main text",
        comment_id="cid-123",
        network_confirmed=True,
        local_confirmed=True,
        method="hybrid_dom",
        account_id=10,
        tiktok_username=None,
    )

    text, cid = await user_schema._find_main_anchor(FakePage(), [main])

    assert text == "main text"
    assert cid == "cid-123"


@pytest.mark.unit
@pytest.mark.asyncio
async def test_find_main_anchor_falls_back_to_text_when_known_comment_id_missing(monkeypatch):
    from executor import user_schema

    class FakeCommentList:
        async def is_visible(self):
            return True

        async def query_selector(self, _selector):
            return None

    class FakePage:
        def __init__(self):
            self.comment_list = FakeCommentList()

        async def query_selector(self, selector):
            if "CommentList" in selector:
                return self.comment_list
            return None

    searched = []

    async def fake_find_comment(*, expected_text, **_kwargs):
        searched.append(expected_text)
        return True, {}

    async def fake_batch(*_args, **_kwargs):
        return {}, {}

    monkeypatch.setattr(user_schema, "find_comment_in_list", fake_find_comment)
    monkeypatch.setattr(user_schema, "find_comments_in_list_batch", fake_batch)

    main = user_schema.PublishedMain(
        text="main two",
        comment_id="cid-2",
        network_confirmed=True,
        local_confirmed=True,
        method="hybrid_dom",
        account_id=10,
        tiktok_username=None,
    )

    anchor = await user_schema._find_main_anchor(FakePage(), [main])
    text, cid = anchor

    assert searched == ["main two"]
    assert text == "main two"
    assert cid is None
    assert anchor.found_by == "text"
    assert anchor.open_text == "main two"


@pytest.mark.unit
@pytest.mark.asyncio
async def test_find_main_anchor_scrolls_known_comment_id_before_text_fallback(monkeypatch):
    from executor import user_schema

    class FakeCommentList:
        async def is_visible(self):
            return True

        async def query_selector(self, _selector):
            return None

    class FakePage:
        def __init__(self):
            self.comment_list = FakeCommentList()

        async def query_selector(self, selector):
            if "CommentList" in selector:
                return self.comment_list
            return None

    batch_calls = []

    async def fake_batch(*, expected_texts, expected_comment_ids, **kwargs):
        batch_calls.append((expected_texts, expected_comment_ids, kwargs))
        return {
            0: {
                "comment_id": "cid-2",
                "matched_by": "id",
                "raw_text": "main two",
                "scroll": 3,
                "position": 2,
            }
        }, {"found_count": 1}

    async def fail_text_search(*_args, **_kwargs):
        raise AssertionError("text search should not run after scrolled CID hit")

    monkeypatch.setattr(user_schema, "find_comments_in_list_batch", fake_batch)
    monkeypatch.setattr(user_schema, "find_comment_in_list", fail_text_search)

    main = user_schema.PublishedMain(
        text="main two",
        comment_id="cid-2",
        network_confirmed=True,
        local_confirmed=True,
        method="hybrid_dom",
        account_id=10,
        tiktok_username=None,
    )

    anchor = await user_schema._find_main_anchor(FakePage(), [main], timeout_seconds=12.0)

    assert batch_calls
    assert batch_calls[0][0] == ["main two"]
    assert batch_calls[0][1] == ["cid-2"]
    assert anchor.matched_text == "main two"
    assert anchor.parent_comment_id == "cid-2"
    assert anchor.found_by == "id"
    assert anchor.id_verified is True


@pytest.mark.unit
@pytest.mark.asyncio
async def test_find_main_anchor_propagates_comment_list_not_open(monkeypatch):
    from executor import user_schema

    class FakePage:
        async def query_selector(self, _selector):
            return None

    async def fake_find_comment(*_args, **_kwargs):
        return False, {"error": "comment_list_not_open"}

    monkeypatch.setattr(user_schema, "find_comment_in_list", fake_find_comment)

    main = user_schema.PublishedMain(
        text="main one",
        comment_id=None,
        network_confirmed=True,
        local_confirmed=True,
        method="hybrid_dom",
        account_id=10,
        tiktok_username=None,
    )

    anchor = await user_schema._find_main_anchor(FakePage(), [main])

    assert anchor.matched_text is None
    assert anchor.error == "comment_list_not_open"
    assert anchor.source_main is main


@pytest.mark.unit
@pytest.mark.asyncio
async def test_reply_ambiguous_publish_is_recorded_without_local_reply_verification(monkeypatch):
    from executor import user_schema
    from executor.publish_action import PublishResult

    class FakePage:
        url = "https://www.tiktok.com/@demo/video/1234567890123456789"

        def is_closed(self):
            return False

        async def close(self):
            return None

    class FakeContext:
        async def close(self):
            return None

    async def fake_create_page_for_account(_account):
        return FakeContext(), FakePage()

    async def fake_login_ready(*_args, **_kwargs):
        return True, None

    async def fake_find_main_anchor(*_args, **_kwargs):
        return "main one", "cid-main"

    async def fake_publish_reply(*_args, **_kwargs):
        return PublishResult(
            status="failed",
            error_reason="stealth_reject_empty_body",
            network_captured=True,
            dom_strategy="reply-dom",
            post_was_enabled_at_click=True,
        )

    async def fake_verify_reply(*_args, **_kwargs):
        raise AssertionError("reply visibility verification must not run in publish-only flow")

    async def fake_sleep(_seconds):
        return None

    failures = []

    monkeypatch.setattr(user_schema, "create_page_for_account", fake_create_page_for_account)
    monkeypatch.setattr(user_schema, "_ensure_account_login_ready", fake_login_ready)
    monkeypatch.setattr(user_schema, "needs_enrichment", lambda _account: False)
    monkeypatch.setattr(user_schema, "_ensure_page_on_target_media", AsyncMock(return_value=True))
    monkeypatch.setattr(user_schema, "_open_comment_list_with_safe_retry", AsyncMock(return_value=True))
    monkeypatch.setattr(user_schema, "_find_main_anchor", fake_find_main_anchor)
    monkeypatch.setattr(user_schema, "publish_reply_hybrid", fake_publish_reply)
    monkeypatch.setattr(user_schema, "_verify_reply_visible_near_parent", fake_verify_reply)
    monkeypatch.setattr(user_schema.asyncio, "sleep", fake_sleep)
    monkeypatch.setattr(user_schema, "_register_runtime_failure", lambda *_args, **_kwargs: failures.append(_args))

    done_parent_keys = set()
    result = await user_schema._reply_with_variant_on_all_mains(
        SimpleNamespace(),
        SimpleNamespace(id=77, proxy=None),
        "https://www.tiktok.com/@demo/video/1234567890123456789",
        [
            user_schema.PublishedMain(
                text="main one",
                comment_id="cid-main",
                network_confirmed=True,
                local_confirmed=True,
                method="hybrid_dom",
                account_id=10,
                tiktok_username=None,
            )
        ],
        "reply text",
        variant_index=1,
        correlation_id="task_1_reply",
        already_replied_parent_keys=done_parent_keys,
    )

    assert len(result.replies) == 1
    assert result.replies[0].success is False
    assert result.replies[0].submitted_unconfirmed is True
    assert result.replies[0].attempted is True
    assert result.replies[0].error == "stealth_reject_empty_body"
    assert result.replies[0].parent_comment_id == "cid-main"
    assert result.replies[0].method == "reply-dom"
    assert [p.text for p in result.found_mains] == ["main one"]
    assert "cid-main" in result.attempted_parent_keys
    assert "cid-main" in done_parent_keys
    assert not failures


@pytest.mark.unit
@pytest.mark.asyncio
async def test_reply_publish_failure_after_found_parent_counts_as_attempt(monkeypatch):
    from executor import user_schema
    from executor.publish_action import PublishResult

    class FakePage:
        url = "https://www.tiktok.com/@demo/video/1234567890123456789"

        def is_closed(self):
            return False

        async def close(self):
            return None

    class FakeContext:
        async def close(self):
            return None

    async def fake_create_page_for_account(_account):
        return FakeContext(), FakePage()

    async def fake_login_ready(*_args, **_kwargs):
        return True, None

    async def fake_find_main_anchor(*_args, **_kwargs):
        return user_schema.MainAnchor(
            matched_text="main one",
            parent_comment_id="cid-main",
            open_text="main one",
            found_by="id",
            id_verified=True,
        )

    async def fake_publish_reply(*_args, **_kwargs):
        return PublishResult(
            status="failed",
            error_reason="stealth_reject_empty_body",
            network_captured=True,
            dom_strategy="reply-dom",
            post_was_enabled_at_click=True,
        )

    async def fake_verify_reply(*_args, **_kwargs):
        return False

    async def fake_publish_all(*_args, **_kwargs):
        raise AssertionError("fallback main must not run when a parent was found")

    async def fake_sleep(_seconds):
        return None

    monkeypatch.setattr(user_schema, "create_page_for_account", fake_create_page_for_account)
    monkeypatch.setattr(user_schema, "_ensure_account_login_ready", fake_login_ready)
    monkeypatch.setattr(user_schema, "needs_enrichment", lambda _account: False)
    monkeypatch.setattr(user_schema, "_ensure_page_on_target_media", AsyncMock(return_value=True))
    monkeypatch.setattr(user_schema, "_open_comment_list_with_safe_retry", AsyncMock(return_value=True))
    monkeypatch.setattr(user_schema, "_find_main_anchor", fake_find_main_anchor)
    monkeypatch.setattr(user_schema, "publish_reply_hybrid", fake_publish_reply)
    monkeypatch.setattr(user_schema, "_verify_reply_visible_near_parent", fake_verify_reply)
    monkeypatch.setattr(user_schema, "_publish_all_variants_on_account", fake_publish_all)
    monkeypatch.setattr(user_schema.asyncio, "sleep", fake_sleep)
    monkeypatch.setattr(user_schema, "_register_runtime_failure", lambda *_args, **_kwargs: None)

    result = await user_schema._reply_with_variant_on_all_mains(
        SimpleNamespace(),
        SimpleNamespace(id=77, proxy=None),
        "https://www.tiktok.com/@demo/video/1234567890123456789",
        [
            user_schema.PublishedMain(
                text="main one",
                comment_id="cid-main",
                network_confirmed=True,
                local_confirmed=True,
                method="hybrid_dom",
                account_id=10,
                tiktok_username=None,
            )
        ],
        "reply text",
        variant_index=1,
        correlation_id="task_1_reply",
        fallback_main_texts=["main one", "main two", "main three"],
    )

    assert result.fallback_attempted is False
    assert [p.text for p in result.found_mains] == ["main one"]
    assert "cid-main" in result.attempted_parent_keys
    assert len(result.replies) == 1
    assert result.replies[0].attempted is True
    assert result.replies[0].success is False
    assert result.replies[0].submitted_unconfirmed is True
    assert result.replies[0].error == "stealth_reject_empty_body"


@pytest.mark.unit
@pytest.mark.asyncio
async def test_reply_text_found_parent_opens_by_text_not_dom_id(monkeypatch):
    from executor import user_schema
    from executor.publish_action import PublishResult

    class FakePage:
        url = "https://www.tiktok.com/@demo/video/1234567890123456789"

        def is_closed(self):
            return False

        async def close(self):
            return None

    class FakeContext:
        async def close(self):
            return None

    async def fake_create_page_for_account(_account):
        return FakeContext(), FakePage()

    async def fake_login_ready(*_args, **_kwargs):
        return True, None

    async def fake_find_main_anchor(*_args, **_kwargs):
        return user_schema.MainAnchor(
            matched_text="main one",
            parent_comment_id="dom-cid-from-text-search",
            open_text="main one visible in dom",
            found_by="text",
            id_verified=False,
        )

    publish_calls = []

    async def fake_publish_reply(*_args, **kwargs):
        publish_calls.append(kwargs)
        return PublishResult(
            status="failed",
            error_reason="stealth_reject_empty_body",
            network_captured=True,
            dom_strategy="reply-dom",
        )

    async def fake_sleep(_seconds):
        return None

    monkeypatch.setattr(user_schema, "create_page_for_account", fake_create_page_for_account)
    monkeypatch.setattr(user_schema, "_ensure_account_login_ready", fake_login_ready)
    monkeypatch.setattr(user_schema, "needs_enrichment", lambda _account: False)
    monkeypatch.setattr(user_schema, "_ensure_page_on_target_media", AsyncMock(return_value=True))
    monkeypatch.setattr(user_schema, "_open_comment_list_with_safe_retry", AsyncMock(return_value=True))
    monkeypatch.setattr(user_schema, "_find_main_anchor", fake_find_main_anchor)
    monkeypatch.setattr(user_schema, "publish_reply_hybrid", fake_publish_reply)
    monkeypatch.setattr(user_schema.asyncio, "sleep", fake_sleep)
    monkeypatch.setattr(user_schema, "_register_runtime_failure", lambda *_args, **_kwargs: None)

    result = await user_schema._reply_with_variant_on_all_mains(
        SimpleNamespace(),
        SimpleNamespace(id=77, proxy=None),
        "https://www.tiktok.com/@demo/video/1234567890123456789",
        [
            user_schema.PublishedMain(
                text="main one",
                comment_id="stale-known-cid",
                network_confirmed=True,
                local_confirmed=True,
                method="hybrid_dom",
                account_id=10,
                tiktok_username=None,
            )
        ],
        "reply text",
        variant_index=1,
        correlation_id="task_1_reply",
    )

    assert publish_calls
    assert publish_calls[0]["parent_comment_text"] == "main one visible in dom"
    assert publish_calls[0]["parent_comment_id"] is None
    assert result.replies[0].parent_comment_id == "dom-cid-from-text-search"
    assert result.replies[0].attempted is True


@pytest.mark.unit
@pytest.mark.asyncio
async def test_reply_uses_batch_parent_search_for_multiple_mains(monkeypatch):
    from executor import user_schema
    from executor.publish_action import PublishResult

    class FakePage:
        url = "https://www.tiktok.com/@demo/video/1234567890123456789"

        def is_closed(self):
            return False

        async def close(self):
            return None

    class FakeContext:
        async def close(self):
            return None

    mains = [
        user_schema.PublishedMain(
            text="main one",
            comment_id="cid-1",
            network_confirmed=True,
            local_confirmed=True,
            method="hybrid_dom",
            account_id=10,
            tiktok_username=None,
        ),
        user_schema.PublishedMain(
            text="main two",
            comment_id="cid-2",
            network_confirmed=True,
            local_confirmed=True,
            method="hybrid_dom",
            account_id=10,
            tiktok_username=None,
        ),
    ]

    async def fake_create_page_for_account(_account):
        return FakeContext(), FakePage()

    async def fake_login_ready(*_args, **_kwargs):
        return True, None

    async def fake_batch(_page, main_candidates, **_kwargs):
        assert [m.text for m in main_candidates] == ["main one", "main two"]
        return [
            user_schema.MainAnchor(
                matched_text="main one",
                parent_comment_id="cid-1",
                open_text="main one visible",
                found_by="text",
                scroll_top=120,
                source_main=main_candidates[0],
            ),
            user_schema.MainAnchor(
                matched_text="main two",
                parent_comment_id="cid-2",
                open_text="main two visible",
                found_by="text",
                scroll_top=360,
                source_main=main_candidates[1],
            ),
        ], {}

    async def fail_single_search(*_args, **_kwargs):
        raise AssertionError("single parent search should not run when batch found all parents")

    restore_calls = []

    async def fake_restore(_page, anchor, **_kwargs):
        restore_calls.append(anchor.scroll_top)

    publish_calls = []

    async def fake_publish_reply(*_args, **kwargs):
        publish_calls.append(kwargs)
        return PublishResult(
            status="success",
            comment_id=f"reply-{len(publish_calls)}",
            network_captured=True,
            dom_strategy="reply-dom",
        )

    async def fake_sleep(_seconds):
        return None

    monkeypatch.setattr(user_schema, "create_page_for_account", fake_create_page_for_account)
    monkeypatch.setattr(user_schema, "_ensure_account_login_ready", fake_login_ready)
    monkeypatch.setattr(user_schema, "needs_enrichment", lambda _account: False)
    monkeypatch.setattr(user_schema, "_ensure_page_on_target_media", AsyncMock(return_value=True))
    monkeypatch.setattr(user_schema, "_open_comment_list_with_safe_retry", AsyncMock(return_value=True))
    monkeypatch.setattr(user_schema, "_find_main_anchors_batch", fake_batch)
    monkeypatch.setattr(user_schema, "_find_main_anchor", fail_single_search)
    monkeypatch.setattr(user_schema, "_restore_comment_list_position_for_anchor", fake_restore)
    monkeypatch.setattr(user_schema, "publish_reply_hybrid", fake_publish_reply)
    monkeypatch.setattr(user_schema.asyncio, "sleep", fake_sleep)
    monkeypatch.setattr(user_schema, "_register_runtime_success", lambda *_args, **_kwargs: None)

    result = await user_schema._reply_with_variant_on_all_mains(
        SimpleNamespace(),
        SimpleNamespace(id=77, proxy=None),
        "https://www.tiktok.com/@demo/video/1234567890123456789",
        mains,
        "reply text",
        variant_index=1,
        correlation_id="task_batch_reply",
    )

    assert [p.text for p in result.found_mains] == ["main one", "main two"]
    assert [out.parent_comment_id for out in result.replies] == ["cid-1", "cid-2"]
    assert all(out.success for out in result.replies)
    assert restore_calls == [120, 360]
    assert [call["parent_comment_text"] for call in publish_calls] == [
        "main one visible",
        "main two visible",
    ]
    assert [call["parent_comment_id"] for call in publish_calls] == [None, None]


@pytest.mark.unit
@pytest.mark.asyncio
async def test_find_main_anchors_batch_maps_batch_hits_to_anchors(monkeypatch):
    from executor import user_schema

    mains = [
        user_schema.PublishedMain(
            text="main one",
            comment_id="cid-1",
            network_confirmed=True,
            local_confirmed=True,
            method="hybrid_dom",
            account_id=10,
            tiktok_username=None,
        ),
        user_schema.PublishedMain(
            text="main two",
            comment_id="cid-2",
            network_confirmed=True,
            local_confirmed=True,
            method="hybrid_dom",
            account_id=10,
            tiktok_username=None,
        ),
    ]
    calls = []

    async def fake_batch(**kwargs):
        calls.append(kwargs)
        return {
            0: {
                "raw_text": "main one visible",
                "comment_id": "fresh-cid-1",
                "matched_by": "text",
                "scroll": 3,
                "position": 4,
                "scroll_top": 300,
            },
            1: {
                "raw_text": "main two visible",
                "comment_id": "cid-2",
                "matched_by": "id",
                "scroll": 1,
                "position": 2,
                "scroll_top": 100,
            },
        }, {"found_count": 2}

    monkeypatch.setattr(user_schema, "find_comments_in_list_batch", fake_batch)

    anchors, info = await user_schema._find_main_anchors_batch(
        SimpleNamespace(),
        mains,
        timeout_seconds=5.0,
    )

    assert calls
    assert calls[0]["expected_texts"] == ["main one", "main two"]
    assert calls[0]["expected_comment_ids"] == ["cid-1", "cid-2"]
    assert calls[0]["ensure_open"] is False
    assert calls[0]["strict_scope"] is True
    assert info == {"found_count": 2}
    assert [anchor.matched_text for anchor in anchors] == ["main two", "main one"]
    assert anchors[0].found_by == "id"
    assert anchors[0].id_verified is True
    assert anchors[0].source_main is mains[1]
    assert anchors[0].parent_key == "cid-2"
    assert anchors[0].scroll_top == 100
    assert anchors[1].found_by == "text"
    assert anchors[1].id_verified is False
    assert anchors[1].source_main is mains[0]
    assert anchors[1].parent_key == "cid-1"


@pytest.mark.unit
@pytest.mark.asyncio
async def test_reply_batch_partial_result_falls_back_to_single_search(monkeypatch):
    from executor import user_schema
    from executor.publish_action import PublishResult

    class FakePage:
        url = "https://www.tiktok.com/@demo/video/1234567890123456789"

        def is_closed(self):
            return False

        async def close(self):
            return None

    class FakeContext:
        async def close(self):
            return None

    mains = [
        user_schema.PublishedMain(
            text="main one",
            comment_id="cid-1",
            network_confirmed=True,
            local_confirmed=True,
            method="hybrid_dom",
            account_id=10,
            tiktok_username=None,
        ),
        user_schema.PublishedMain(
            text="main two",
            comment_id="cid-2",
            network_confirmed=True,
            local_confirmed=True,
            method="hybrid_dom",
            account_id=10,
            tiktok_username=None,
        ),
    ]

    async def fake_create_page_for_account(_account):
        return FakeContext(), FakePage()

    async def fake_login_ready(*_args, **_kwargs):
        return True, None

    async def fake_batch(_page, main_candidates, **_kwargs):
        return [
            user_schema.MainAnchor(
                matched_text="main one",
                parent_comment_id="cid-1",
                open_text="main one visible",
                found_by="text",
                scroll_top=120,
                source_main=main_candidates[0],
            )
        ], {}

    single_calls = []

    async def fake_single(_page, main_candidates, **_kwargs):
        single_calls.append([m.text for m in main_candidates])
        return user_schema.MainAnchor(
            matched_text="main two",
            parent_comment_id="cid-2",
            open_text="main two visible",
            found_by="text",
            source_main=main_candidates[0],
        )

    publish_calls = []

    async def fake_publish_reply(*_args, **kwargs):
        publish_calls.append(kwargs)
        return PublishResult(
            status="success",
            comment_id=f"reply-{len(publish_calls)}",
            network_captured=True,
            dom_strategy="reply-dom",
        )

    async def fake_sleep(_seconds):
        return None

    monkeypatch.setattr(user_schema, "create_page_for_account", fake_create_page_for_account)
    monkeypatch.setattr(user_schema, "_ensure_account_login_ready", fake_login_ready)
    monkeypatch.setattr(user_schema, "needs_enrichment", lambda _account: False)
    monkeypatch.setattr(user_schema, "_ensure_page_on_target_media", AsyncMock(return_value=True))
    monkeypatch.setattr(user_schema, "_open_comment_list_with_safe_retry", AsyncMock(return_value=True))
    monkeypatch.setattr(user_schema, "_find_main_anchors_batch", fake_batch)
    monkeypatch.setattr(user_schema, "_find_main_anchor", fake_single)
    monkeypatch.setattr(user_schema, "publish_reply_hybrid", fake_publish_reply)
    monkeypatch.setattr(user_schema.asyncio, "sleep", fake_sleep)
    monkeypatch.setattr(user_schema, "_register_runtime_success", lambda *_args, **_kwargs: None)

    result = await user_schema._reply_with_variant_on_all_mains(
        SimpleNamespace(),
        SimpleNamespace(id=77, proxy=None),
        "https://www.tiktok.com/@demo/video/1234567890123456789",
        mains,
        "reply text",
        variant_index=1,
        correlation_id="task_batch_partial",
        fallback_main_texts=["main one", "main two", "main three"],
    )

    assert single_calls == [["main two"]]
    assert [out.parent_comment_id for out in result.replies] == ["cid-1", "cid-2"]
    assert result.fallback_attempted is False
    assert [call["parent_comment_text"] for call in publish_calls] == [
        "main one visible",
        "main two visible",
    ]


@pytest.mark.unit
@pytest.mark.asyncio
async def test_execute_advances_reply_variant_and_shrinks_active_mains(monkeypatch):
    from executor import user_schema

    main_account = SimpleNamespace(id=101, proxy=None)
    reply_accounts = [
        SimpleNamespace(id=201, proxy=None),
        SimpleNamespace(id=202, proxy=None),
    ]
    published = [
        user_schema.PublishedMain(
            text="main one",
            comment_id="cid-1",
            network_confirmed=True,
            local_confirmed=True,
            method="hybrid_dom",
            account_id=main_account.id,
            tiktok_username="main_user",
        ),
        user_schema.PublishedMain(
            text="main two",
            comment_id="cid-2",
            network_confirmed=True,
            local_confirmed=True,
            method="hybrid_dom",
            account_id=main_account.id,
            tiktok_username="main_user",
        ),
        user_schema.PublishedMain(
            text="main three",
            comment_id="cid-3",
            network_confirmed=True,
            local_confirmed=True,
            method="hybrid_dom",
            account_id=main_account.id,
            tiktok_username="main_user",
        ),
    ]
    calls = []
    excludes_seen = []

    def fake_get_main(*_args, **_kwargs):
        return main_account

    async def fake_try_main(*_args, **_kwargs):
        return list(published), None

    async def fake_confirm(_db, _video_url, published_mains, **_kwargs):
        return list(published_mains)

    def fake_get_reply(_db, *, exclude_ids, **_kwargs):
        excludes_seen.append(list(exclude_ids))
        for account in reply_accounts:
            if account.id not in exclude_ids:
                return account
        return None

    async def fake_reply(_db, account, _video_url, main_candidates, reply_text, **kwargs):
        variant_idx = kwargs["variant_index"]
        candidate_texts = [m.text for m in main_candidates]
        calls.append((variant_idx, account.id, candidate_texts, reply_text))
        found = [main_candidates[0]]
        if variant_idx == 1 and len(main_candidates) > 2:
            found = [main_candidates[0], main_candidates[2]]
        return user_schema.ReplyAttemptResult(
            replies=[
                user_schema.ReplyOutcome(
                    text=reply_text,
                    variant_index=variant_idx,
                    matched_main_text=main.text,
                    parent_comment_id=main.comment_id,
                    success=True,
                    method="hybrid_dom",
                    error=None,
                    account_id=account.id,
                    attempted=True,
                )
                for main in found
            ],
            found_mains=found,
            attempted_parent_keys={main.comment_id for main in found},
            searched=True,
        )

    async def fake_sleep(_seconds):
        return None

    async def fake_likes(*_args, **_kwargs):
        return None

    monkeypatch.setattr(user_schema, "get_free_account_for_main_comment", fake_get_main)
    monkeypatch.setattr(user_schema, "_try_main_account", fake_try_main)
    monkeypatch.setattr(user_schema, "_confirm_mains_by_observers", fake_confirm)
    monkeypatch.setattr(user_schema, "release_main_account_reservation", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(user_schema, "get_free_account_for_reply", fake_get_reply)
    monkeypatch.setattr(user_schema, "_reply_with_variant_on_all_mains", fake_reply)
    monkeypatch.setattr(user_schema, "_maybe_order_likes", fake_likes)
    monkeypatch.setattr(user_schema.asyncio, "sleep", fake_sleep)

    task = SimpleNamespace(settings={"replies": ["reply one", "reply two"]})
    result = await user_schema.execute_user_schema_full(
        SimpleNamespace(),
        task,
        "https://www.tiktok.com/@demo/video/1234567890123456789",
        ["main one", "main two", "main three"],
        254,
        boost_enabled=False,
        boost_quantity=0,
    )

    assert [(variant, account) for variant, account, _texts, _reply in calls] == [(1, 201), (2, 202)]
    assert excludes_seen[0] == [main_account.id]
    assert excludes_seen[1] == [main_account.id, 201]
    assert calls[0][2] == ["main one", "main two", "main three"]
    assert calls[1][2] == ["main one", "main three"]
    assert [reply.variant_index for reply in result.replies] == [1, 1, 2]
    assert all(reply.attempted for reply in result.replies)
    assert result.main_found_by_observer is True


@pytest.mark.unit
@pytest.mark.asyncio
async def test_execute_retries_same_reply_variant_after_fallback_mains(monkeypatch):
    from executor import user_schema

    main_account = SimpleNamespace(id=101, proxy=None)
    reply_accounts = [
        SimpleNamespace(id=201, proxy=None),
        SimpleNamespace(id=202, proxy=None),
        SimpleNamespace(id=203, proxy=None),
        SimpleNamespace(id=204, proxy=None),
    ]
    initial_mains = [
        user_schema.PublishedMain(
            text=text,
            comment_id=f"old-{idx}",
            network_confirmed=True,
            local_confirmed=True,
            method="hybrid_dom",
            account_id=main_account.id,
            tiktok_username="main_user",
        )
        for idx, text in enumerate(["main one", "main two", "main three"], start=1)
    ]
    fallback_mains = [
        user_schema.PublishedMain(
            text=text,
            comment_id=f"fallback-{idx}",
            network_confirmed=True,
            local_confirmed=True,
            method="hybrid_dom",
            account_id=201,
            tiktok_username="fallback_user",
        )
        for idx, text in enumerate(["main one", "main two", "main three"], start=1)
    ]
    calls = []

    async def fake_try_main(*_args, **_kwargs):
        return list(initial_mains), None

    async def fake_confirm(_db, _video_url, published_mains, **_kwargs):
        return []

    def fake_get_reply(_db, *, exclude_ids, **_kwargs):
        for account in reply_accounts:
            if account.id not in exclude_ids:
                return account
        return None

    async def fake_reply(_db, account, _video_url, main_candidates, reply_text, **kwargs):
        variant_idx = kwargs["variant_index"]
        if account.id in {201, 202}:
            assert kwargs["fallback_main_texts"] == ["main one", "main two", "main three"]
        else:
            assert kwargs["fallback_main_texts"] is None
        calls.append((variant_idx, account.id, [m.comment_id for m in main_candidates], reply_text))
        if account.id == 201:
            assert kwargs["fallback_allowed"] is False
            return user_schema.ReplyAttemptResult(
                replies=[],
                searched=True,
                confident_empty_search=True,
            )
        if account.id == 202:
            assert kwargs["fallback_allowed"] is True
            return user_schema.ReplyAttemptResult(
                replies=[
                    user_schema.ReplyOutcome(
                        text=reply_text,
                        variant_index=variant_idx,
                        matched_main_text=None,
                        parent_comment_id=None,
                        success=False,
                        method="fallback_main",
                        error="no_main_found_fallback_main_published",
                        account_id=account.id,
                    )
                ],
                fallback_mains=list(fallback_mains),
                fallback_attempted=True,
                searched=True,
            )
        found = [main_candidates[0]]
        return user_schema.ReplyAttemptResult(
            replies=[
                user_schema.ReplyOutcome(
                    text=reply_text,
                    variant_index=variant_idx,
                    matched_main_text=found[0].text,
                    parent_comment_id=found[0].comment_id,
                    success=True,
                    method="hybrid_dom",
                    error=None,
                    account_id=account.id,
                    attempted=True,
                )
            ],
            found_mains=found,
            attempted_parent_keys={found[0].comment_id},
            searched=True,
        )

    async def fake_sleep(_seconds):
        return None

    async def fake_likes(*_args, **_kwargs):
        return None

    monkeypatch.setattr(user_schema, "get_free_account_for_main_comment", lambda *_a, **_k: main_account)
    monkeypatch.setattr(user_schema, "_try_main_account", fake_try_main)
    monkeypatch.setattr(user_schema, "_confirm_mains_by_observers", fake_confirm)
    monkeypatch.setattr(user_schema, "release_main_account_reservation", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(user_schema, "get_free_account_for_reply", fake_get_reply)
    monkeypatch.setattr(user_schema, "_reply_with_variant_on_all_mains", fake_reply)
    monkeypatch.setattr(user_schema, "_maybe_order_likes", fake_likes)
    monkeypatch.setattr(user_schema.asyncio, "sleep", fake_sleep)

    task = SimpleNamespace(settings={"replies": ["reply one", "reply two"]})
    result = await user_schema.execute_user_schema_full(
        SimpleNamespace(),
        task,
        "https://www.tiktok.com/@demo/video/1234567890123456789",
        ["main one", "main two", "main three"],
        255,
        boost_enabled=False,
        boost_quantity=0,
    )

    assert [(variant, account) for variant, account, _cids, _reply in calls] == [
        (1, 201),
        (1, 202),
        (1, 203),
        (2, 204),
    ]
    assert calls[2][2] == ["fallback-1", "fallback-2", "fallback-3"]
    assert calls[3][2] == ["fallback-1"]
    assert [reply.variant_index for reply in result.replies] == [1, 2]
    assert result.fallback_main_via_reply is True
    assert result.main_tiktok_username == "fallback_user"


@pytest.mark.unit
@pytest.mark.asyncio
async def test_execute_retries_same_reply_variant_after_unsearchable_comment_list(monkeypatch):
    from executor import user_schema

    main_account = SimpleNamespace(id=101, proxy=None)
    reply_accounts = [
        SimpleNamespace(id=201, proxy=None),
        SimpleNamespace(id=202, proxy=None),
    ]
    published = [
        user_schema.PublishedMain(
            text=text,
            comment_id=f"main-{idx}",
            network_confirmed=True,
            local_confirmed=True,
            method="hybrid_dom",
            account_id=main_account.id,
            tiktok_username="main_user",
        )
        for idx, text in enumerate(["main one", "main two", "main three"], start=1)
    ]
    calls = []

    async def fake_try_main(*_args, **_kwargs):
        return list(published), None

    async def fake_confirm(_db, _video_url, published_mains, **_kwargs):
        return list(published_mains)

    def fake_get_reply(_db, *, exclude_ids, **_kwargs):
        for account in reply_accounts:
            if account.id not in exclude_ids:
                return account
        return None

    async def fake_reply(_db, account, _video_url, main_candidates, reply_text, **kwargs):
        variant_idx = kwargs["variant_index"]
        calls.append((variant_idx, account.id, [m.comment_id for m in main_candidates], reply_text))
        if account.id == 201:
            return user_schema.ReplyAttemptResult(
                replies=[
                    user_schema.ReplyOutcome(
                        text=reply_text,
                        variant_index=variant_idx,
                        matched_main_text=None,
                        parent_comment_id=None,
                        success=False,
                        method=None,
                        error="comment_list_not_open",
                        account_id=account.id,
                        attempted=False,
                    )
                ],
                searched=False,
            )
        found = [main_candidates[0]]
        return user_schema.ReplyAttemptResult(
            replies=[
                user_schema.ReplyOutcome(
                    text=reply_text,
                    variant_index=variant_idx,
                    matched_main_text=found[0].text,
                    parent_comment_id=found[0].comment_id,
                    success=False,
                    method="hybrid_dom",
                    error="stealth_reject_empty_body",
                    account_id=account.id,
                    attempted=True,
                    submitted_unconfirmed=True,
                )
            ],
            found_mains=found,
            attempted_parent_keys={found[0].comment_id},
            submitted_parent_keys_count=1,
            searched=True,
        )

    async def fake_sleep(_seconds):
        return None

    async def fake_likes(*_args, **_kwargs):
        return None

    monkeypatch.setattr(user_schema, "get_free_account_for_main_comment", lambda *_a, **_k: main_account)
    monkeypatch.setattr(user_schema, "_try_main_account", fake_try_main)
    monkeypatch.setattr(user_schema, "_confirm_mains_by_observers", fake_confirm)
    monkeypatch.setattr(user_schema, "release_main_account_reservation", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(user_schema, "get_free_account_for_reply", fake_get_reply)
    monkeypatch.setattr(user_schema, "_reply_with_variant_on_all_mains", fake_reply)
    monkeypatch.setattr(user_schema, "_maybe_order_likes", fake_likes)
    monkeypatch.setattr(user_schema.asyncio, "sleep", fake_sleep)

    task = SimpleNamespace(settings={"replies": ["reply one"]})
    result = await user_schema.execute_user_schema_full(
        SimpleNamespace(),
        task,
        "https://www.tiktok.com/@demo/video/1234567890123456789",
        ["main one", "main two", "main three"],
        257,
        boost_enabled=False,
        boost_quantity=0,
    )

    assert [(variant, account) for variant, account, _cids, _reply in calls] == [
        (1, 201),
        (1, 202),
    ]
    assert calls[0][2] == ["main-1", "main-2", "main-3"]
    assert calls[1][2] == ["main-1", "main-2", "main-3"]
    assert result.fallback_main_via_reply is False
    assert len(result.published_mains) == 3
    assert len(result.replies) == 1
    assert result.replies[0].account_id == 202


@pytest.mark.unit
@pytest.mark.asyncio
async def test_execute_main_only_publishes_each_variant_on_distinct_account(monkeypatch):
    from executor import user_schema

    accounts = [
        SimpleNamespace(id=101, proxy=None),
        SimpleNamespace(id=102, proxy=None),
        SimpleNamespace(id=103, proxy=None),
    ]
    calls = []
    released = []

    def fake_get_main(_db, *, exclude_ids, **_kwargs):
        for account in accounts:
            if account.id not in exclude_ids:
                return account
        return None

    async def fake_try_main(_db, account, _video_url, text_variants, **_kwargs):
        calls.append((account.id, list(text_variants)))
        text = text_variants[0]
        return [
            user_schema.PublishedMain(
                text=text,
                comment_id=f"cid-{account.id}",
                network_confirmed=True,
                local_confirmed=True,
                method="hybrid_dom",
                account_id=account.id,
                tiktok_username=f"user_{account.id}",
            )
        ], None

    async def fake_confirm(*_args, **_kwargs):
        raise AssertionError("main-only orders without boost must not start observer confirmation")

    async def fake_sleep(_seconds):
        return None

    monkeypatch.setattr(user_schema, "get_free_account_for_main_comment", fake_get_main)
    monkeypatch.setattr(user_schema, "_try_main_account", fake_try_main)
    monkeypatch.setattr(user_schema, "_confirm_mains_by_observers", fake_confirm)
    monkeypatch.setattr(user_schema, "release_main_account_reservation", lambda acc_id: released.append(acc_id))
    monkeypatch.setattr(user_schema, "_maybe_order_likes", AsyncMock(return_value=None))
    monkeypatch.setattr(user_schema.asyncio, "sleep", fake_sleep)

    task = SimpleNamespace(settings={"replies": [], "reply_count": 0})
    result = await user_schema.execute_user_schema_full(
        SimpleNamespace(),
        task,
        "https://www.tiktok.com/@demo/video/1234567890123456789",
        ["main one", "main two", "main three"],
        280,
        boost_enabled=False,
        boost_quantity=0,
    )

    assert calls == [
        (101, ["main one"]),
        (102, ["main two"]),
        (103, ["main three"]),
    ]
    assert [p.account_id for p in result.published_mains] == [101, 102, 103]
    assert result.main_account_id == 101
    assert result.main_tiktok_username == "user_101"
    assert result.expected_reply_variants == 0
    assert released == [101, 102, 103]


@pytest.mark.unit
@pytest.mark.asyncio
async def test_execute_main_only_skips_likes_even_when_enabled(monkeypatch):
    from executor import user_schema

    accounts = [
        SimpleNamespace(id=101, proxy=None),
        SimpleNamespace(id=102, proxy=None),
    ]
    calls = []

    def fake_get_main(_db, *, exclude_ids, **_kwargs):
        for account in accounts:
            if account.id not in exclude_ids:
                return account
        return None

    async def fake_try_main(_db, account, _video_url, text_variants, **_kwargs):
        calls.append((account.id, list(text_variants)))
        return [
            user_schema.PublishedMain(
                text=text_variants[0],
                comment_id=f"cid-{account.id}",
                network_confirmed=True,
                local_confirmed=True,
                method="hybrid_dom",
                account_id=account.id,
                tiktok_username=f"user_{account.id}",
            )
        ], None

    async def fake_confirm(*_args, **_kwargs):
        raise AssertionError("main-only orders must not confirm mains only for likes")

    async def fake_sleep(_seconds):
        return None

    likes_mock = AsyncMock(return_value=None)

    monkeypatch.setattr(user_schema, "get_free_account_for_main_comment", fake_get_main)
    monkeypatch.setattr(user_schema, "_try_main_account", fake_try_main)
    monkeypatch.setattr(user_schema, "_confirm_mains_by_observers", fake_confirm)
    monkeypatch.setattr(user_schema, "release_main_account_reservation", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(user_schema, "_maybe_order_likes", likes_mock)
    monkeypatch.setattr(user_schema.asyncio, "sleep", fake_sleep)

    task = SimpleNamespace(settings={"replies": [], "reply_count": 0})
    result = await user_schema.execute_user_schema_full(
        SimpleNamespace(),
        task,
        "https://www.tiktok.com/@demo/video/1234567890123456789",
        ["main one", "main two"],
        281,
        boost_enabled=True,
        boost_quantity=100,
    )

    assert calls == [
        (101, ["main one"]),
        (102, ["main two"]),
    ]
    assert likes_mock.await_count == 0
    assert result.boost_enabled is False
    assert result.boost_order_id is None


@pytest.mark.unit
@pytest.mark.asyncio
async def test_execute_full_skips_likes_when_no_reply_published(monkeypatch):
    from executor import user_schema

    main_account = SimpleNamespace(id=101, proxy=None)
    reply_account = SimpleNamespace(id=201, proxy=None)

    async def fake_try_main(*_args, **_kwargs):
        return [
            user_schema.PublishedMain(
                text="main one",
                comment_id="cid-main",
                network_confirmed=True,
                local_confirmed=True,
                method="hybrid_dom",
                account_id=main_account.id,
                tiktok_username="main_user",
            )
        ], None

    async def fake_confirm(_db, _video_url, published_mains, **_kwargs):
        return list(published_mains)

    def fake_get_reply(_db, *, exclude_ids, **_kwargs):
        return None if reply_account.id in exclude_ids else reply_account

    async def fake_reply(_db, account, _video_url, main_candidates, reply_text, **kwargs):
        return user_schema.ReplyAttemptResult(
            replies=[
                user_schema.ReplyOutcome(
                    text=reply_text,
                    variant_index=kwargs["variant_index"],
                    matched_main_text=main_candidates[0].text,
                    parent_comment_id=main_candidates[0].comment_id,
                    success=False,
                    method="hybrid_dom",
                    error="stealth_reject_empty_body",
                    account_id=account.id,
                    attempted=True,
                )
            ],
            found_mains=list(main_candidates),
            attempted_parent_keys={main_candidates[0].comment_id},
            searched=True,
        )

    async def fake_sleep(_seconds):
        return None

    likes_mock = AsyncMock(return_value=None)

    monkeypatch.setattr(user_schema, "get_free_account_for_main_comment", lambda *_a, **_k: main_account)
    monkeypatch.setattr(user_schema, "_try_main_account", fake_try_main)
    monkeypatch.setattr(user_schema, "_confirm_mains_by_observers", fake_confirm)
    monkeypatch.setattr(user_schema, "get_free_account_for_reply", fake_get_reply)
    monkeypatch.setattr(user_schema, "_reply_with_variant_on_all_mains", fake_reply)
    monkeypatch.setattr(user_schema, "release_main_account_reservation", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(user_schema, "_maybe_order_likes", likes_mock)
    monkeypatch.setattr(user_schema.asyncio, "sleep", fake_sleep)

    task = SimpleNamespace(settings={"replies": ["reply one"]})
    result = await user_schema.execute_user_schema_full(
        SimpleNamespace(),
        task,
        "https://www.tiktok.com/@demo/video/1234567890123456789",
        ["main one"],
        282,
        boost_enabled=True,
        boost_quantity=100,
    )

    assert len(result.replies) == 1
    assert result.replies[0].success is False
    assert likes_mock.await_count == 0
    assert result.boost_enabled is False
    assert result.boost_order_id is None


@pytest.mark.unit
@pytest.mark.asyncio
async def test_execute_main_only_retries_failed_variant_without_reusing_success_account(monkeypatch):
    from executor import user_schema

    accounts = [
        SimpleNamespace(id=101, proxy=None),
        SimpleNamespace(id=102, proxy=None),
        SimpleNamespace(id=103, proxy=None),
    ]
    calls = []

    def fake_get_main(_db, *, exclude_ids, **_kwargs):
        for account in accounts:
            if account.id not in exclude_ids:
                return account
        return None

    async def fake_try_main(_db, account, _video_url, text_variants, **_kwargs):
        text = text_variants[0]
        calls.append((account.id, text))
        if account.id == 102:
            return [], "typing_failed"
        return [
            user_schema.PublishedMain(
                text=text,
                comment_id=f"cid-{account.id}",
                network_confirmed=True,
                local_confirmed=True,
                method="hybrid_dom",
                account_id=account.id,
                tiktok_username=f"user_{account.id}",
            )
        ], None

    async def fake_sleep(_seconds):
        return None

    monkeypatch.setattr(user_schema, "get_free_account_for_main_comment", fake_get_main)
    monkeypatch.setattr(user_schema, "_try_main_account", fake_try_main)
    monkeypatch.setattr(user_schema, "release_main_account_reservation", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(user_schema, "_maybe_order_likes", AsyncMock(return_value=None))
    monkeypatch.setattr(user_schema.asyncio, "sleep", fake_sleep)

    task = SimpleNamespace(settings={"replies": [], "reply_count": 0})
    result = await user_schema.execute_user_schema_full(
        SimpleNamespace(),
        task,
        "https://www.tiktok.com/@demo/video/1234567890123456789",
        ["main one", "main two"],
        281,
        boost_enabled=False,
        boost_quantity=0,
    )

    assert calls == [
        (101, "main one"),
        (102, "main two"),
        (103, "main two"),
    ]
    assert [p.account_id for p in result.published_mains] == [101, 103]
    assert [p.text for p in result.published_mains] == ["main one", "main two"]
    assert result.error is None


@pytest.mark.unit
@pytest.mark.asyncio
async def test_execute_lets_reply_decide_when_main_observers_cannot_open_list(monkeypatch):
    from executor import user_schema

    main_account = SimpleNamespace(id=101, proxy=None)
    reply_account = SimpleNamespace(id=201, proxy=None)
    published = [
        user_schema.PublishedMain(
            text="main one",
            comment_id=None,
            network_confirmed=False,
            local_confirmed=True,
            method="hybrid_dom",
            account_id=main_account.id,
            tiktok_username="main_user",
        ),
        user_schema.PublishedMain(
            text="main two",
            comment_id="cid-2",
            network_confirmed=True,
            local_confirmed=True,
            method="hybrid_dom",
            account_id=main_account.id,
            tiktok_username="main_user",
        ),
        user_schema.PublishedMain(
            text="main three",
            comment_id="cid-3",
            network_confirmed=True,
            local_confirmed=True,
            method="hybrid_dom",
            account_id=main_account.id,
            tiktok_username="main_user",
        ),
    ]
    confirm_calls = []
    fallback_calls = []
    reply_calls = []

    async def fake_try_main(*_args, **_kwargs):
        return list(published), None

    async def fake_confirm(_db, _video_url, published_mains, **kwargs):
        confirm_calls.append([p.text for p in published_mains])
        aggregate = kwargs.get("aggregate_outcomes")
        if aggregate is not None:
            aggregate.extend(["observer_comment_list_not_open", "observer_comment_list_not_open"])
        return []

    async def fake_fallback(_db, _video_url, text_variants, **_kwargs):
        fallback_calls.append(list(text_variants))
        raise AssertionError("observer open failure must not trigger pre-reply repair")

    async def fake_reply(*args, **kwargs):
        reply_calls.append((args, kwargs))
        main_candidates = list(args[3])
        found = main_candidates[:1]
        return user_schema.ReplyAttemptResult(
            replies=[
                user_schema.ReplyOutcome(
                    text=args[4],
                    variant_index=kwargs["variant_index"],
                    matched_main_text=found[0].text,
                    parent_comment_id=found[0].comment_id,
                    success=True,
                    method="hybrid_dom",
                    error=None,
                    account_id=reply_account.id,
                    attempted=True,
                )
            ],
            found_mains=found,
            attempted_parent_keys={found[0].comment_id or user_schema._dedup_key(found[0].text)},
            searched=True,
        )

    async def fake_sleep(_seconds):
        return None

    monkeypatch.setattr(user_schema, "_MAIN_PUBLIC_CONFIRM_ROUNDS", 1)
    monkeypatch.setattr(user_schema, "get_free_account_for_main_comment", lambda *_a, **_k: main_account)
    monkeypatch.setattr(user_schema, "_try_main_account", fake_try_main)
    monkeypatch.setattr(user_schema, "_confirm_mains_by_observers", fake_confirm)
    monkeypatch.setattr(user_schema, "_run_fallback_main_attempts", fake_fallback)
    monkeypatch.setattr(user_schema, "_reply_with_variant_on_all_mains", fake_reply)
    monkeypatch.setattr(user_schema, "get_free_account_for_reply", lambda *_a, **_k: reply_account)
    monkeypatch.setattr(user_schema, "release_main_account_reservation", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(user_schema, "_maybe_order_likes", AsyncMock(return_value=None))
    monkeypatch.setattr(user_schema.asyncio, "sleep", fake_sleep)

    task = SimpleNamespace(settings={"replies": ["reply one"]})
    result = await user_schema.execute_user_schema_full(
        SimpleNamespace(),
        task,
        "https://www.tiktok.com/@demo/video/1234567890123456789",
        ["main one", "main two", "main three"],
        270,
        boost_enabled=False,
        boost_quantity=0,
    )

    assert confirm_calls == [["main one", "main two", "main three"]]
    assert fallback_calls == []
    assert len(reply_calls) == 1
    assert reply_calls[0][1]["fallback_main_texts"] == ["main one", "main two", "main three"]
    assert [reply.success for reply in result.replies] == [True]
    assert result.main_found_by_observer is True
    assert result.error is None


@pytest.mark.unit
@pytest.mark.asyncio
async def test_execute_bypasses_repair_when_observer_misses_published_mains(monkeypatch):
    from executor import user_schema

    main_account = SimpleNamespace(id=101, proxy=None)
    reply_account = SimpleNamespace(id=201, proxy=None)
    published = [
        user_schema.PublishedMain(
            text=text,
            comment_id=f"cid-{idx}" if idx > 1 else None,
            network_confirmed=idx > 1,
            local_confirmed=True,
            method="hybrid_dom",
            account_id=main_account.id,
            tiktok_username="main_user",
        )
        for idx, text in enumerate(["main one", "main two", "main three"], start=1)
    ]
    confirm_calls = []
    fallback_calls = []
    reply_calls = []

    async def fake_try_main(*_args, **_kwargs):
        return list(published), None

    async def fake_confirm(_db, _video_url, published_mains, **kwargs):
        confirm_calls.append([p.text for p in published_mains])
        aggregate = kwargs.get("aggregate_outcomes")
        if aggregate is not None:
            aggregate.extend(["observer_open_list_no_target", "observer_open_list_no_target"])
        return []

    async def fake_fallback(_db, _video_url, text_variants, **_kwargs):
        fallback_calls.append(list(text_variants))
        raise AssertionError("trust-filter observer misses must not trigger repair")

    def fake_get_reply(_db, *, exclude_ids, **_kwargs):
        return None if reply_account.id in exclude_ids else reply_account

    async def fake_reply(_db, account, _video_url, main_candidates, reply_text, **kwargs):
        reply_calls.append([m.text for m in main_candidates])
        return user_schema.ReplyAttemptResult(
            replies=[
                user_schema.ReplyOutcome(
                    text=reply_text,
                    variant_index=kwargs["variant_index"],
                    matched_main_text=main.text,
                    parent_comment_id=main.comment_id,
                    success=True,
                    method="hybrid_dom",
                    error=None,
                    account_id=account.id,
                    attempted=True,
                )
                for main in main_candidates
            ],
            found_mains=list(main_candidates),
            attempted_parent_keys={m.comment_id or user_schema._dedup_key(m.text) for m in main_candidates},
            searched=True,
        )

    async def fake_sleep(_seconds):
        return None

    monkeypatch.setattr(user_schema, "_MAIN_PUBLIC_CONFIRM_ROUNDS", 1)
    monkeypatch.setattr(user_schema, "get_free_account_for_main_comment", lambda *_a, **_k: main_account)
    monkeypatch.setattr(user_schema, "_try_main_account", fake_try_main)
    monkeypatch.setattr(user_schema, "_confirm_mains_by_observers", fake_confirm)
    monkeypatch.setattr(user_schema, "_run_fallback_main_attempts", fake_fallback)
    monkeypatch.setattr(user_schema, "get_free_account_for_reply", fake_get_reply)
    monkeypatch.setattr(user_schema, "_reply_with_variant_on_all_mains", fake_reply)
    monkeypatch.setattr(user_schema, "release_main_account_reservation", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(user_schema, "_maybe_order_likes", AsyncMock(return_value=None))
    monkeypatch.setattr(user_schema.asyncio, "sleep", fake_sleep)

    task = SimpleNamespace(settings={"replies": ["reply one"]})
    result = await user_schema.execute_user_schema_full(
        SimpleNamespace(),
        task,
        "https://www.tiktok.com/@demo/video/1234567890123456789",
        ["main one", "main two", "main three"],
        272,
        boost_enabled=False,
        boost_quantity=0,
    )

    assert confirm_calls == [["main one", "main two", "main three"]]
    assert fallback_calls == []
    assert reply_calls == [["main one", "main two", "main three"]]
    assert result.error is None
    assert result.main_found_by_observer is True
    assert any(warning.get("code") == "observer_reply_decides" for warning in result.warnings)


@pytest.mark.unit
@pytest.mark.asyncio
async def test_execute_lets_reply_decide_when_observer_opens_empty_public_comment_surface(monkeypatch):
    from executor import user_schema

    main_account = SimpleNamespace(id=101, proxy=None)
    reply_account = SimpleNamespace(id=401, proxy=None)
    published = [
        user_schema.PublishedMain(
            text=text,
            comment_id=f"cid-{idx}",
            network_confirmed=True,
            local_confirmed=True,
            method="hybrid_dom",
            account_id=main_account.id,
            tiktok_username="main_user",
        )
        for idx, text in enumerate(["main one", "main two", "main three"], start=1)
    ]
    fallback_published = [
        user_schema.PublishedMain(
            text=text,
            comment_id=f"fallback-{idx}",
            network_confirmed=True,
            local_confirmed=True,
            method="hybrid_dom",
            account_id=301,
            tiktok_username="fallback_user",
        )
        for idx, text in enumerate(["main one", "main two", "main three"], start=1)
    ]
    confirm_calls = []
    fallback_calls = []
    reply_calls = []

    async def fake_try_main(*_args, **_kwargs):
        return list(published), None

    async def fake_confirm(_db, _video_url, published_mains, **kwargs):
        confirm_calls.append([p.text for p in published_mains])
        aggregate = kwargs.get("aggregate_outcomes")
        if aggregate is not None:
            aggregate.append("observer_open_list_empty")
        return []

    async def fake_fallback(_db, _video_url, text_variants, **_kwargs):
        fallback_calls.append(list(text_variants))
        wanted = set(text_variants)
        return [main for main in fallback_published if main.text in wanted]

    async def fake_reply(*args, **kwargs):
        reply_calls.append((args, kwargs))
        main_candidates = list(args[3])
        found = main_candidates[:1]
        return user_schema.ReplyAttemptResult(
            replies=[
                user_schema.ReplyOutcome(
                    text=args[4],
                    variant_index=kwargs["variant_index"],
                    matched_main_text=found[0].text,
                    parent_comment_id=found[0].comment_id,
                    success=True,
                    method="hybrid_dom",
                    error=None,
                    account_id=reply_account.id,
                    attempted=True,
                )
            ],
            found_mains=found,
            attempted_parent_keys={found[0].comment_id},
            searched=True,
        )

    async def fake_sleep(_seconds):
        return None

    monkeypatch.setattr(user_schema, "_MAIN_PUBLIC_CONFIRM_ROUNDS", 1)
    monkeypatch.setattr(user_schema, "get_free_account_for_main_comment", lambda *_a, **_k: main_account)
    monkeypatch.setattr(user_schema, "_try_main_account", fake_try_main)
    monkeypatch.setattr(user_schema, "_confirm_mains_by_observers", fake_confirm)
    monkeypatch.setattr(user_schema, "_run_fallback_main_attempts", fake_fallback)
    monkeypatch.setattr(user_schema, "_reply_with_variant_on_all_mains", fake_reply)
    monkeypatch.setattr(user_schema, "get_free_account_for_reply", lambda *_a, **_k: reply_account)
    monkeypatch.setattr(user_schema, "release_main_account_reservation", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(user_schema, "_maybe_order_likes", AsyncMock(return_value=None))
    monkeypatch.setattr(user_schema.asyncio, "sleep", fake_sleep)

    task = SimpleNamespace(settings={"replies": ["reply one"]})
    result = await user_schema.execute_user_schema_full(
        SimpleNamespace(),
        task,
        "https://www.tiktok.com/@demo/video/1234567890123456789",
        ["main one", "main two", "main three"],
        270,
        boost_enabled=False,
        boost_quantity=0,
    )

    assert confirm_calls == [["main one", "main two", "main three"]]
    assert fallback_calls == []
    assert len(reply_calls) == 1
    assert reply_calls[0][1]["fallback_main_texts"] == ["main one", "main two", "main three"]
    assert result.fallback_main_via_reply is False
    assert [reply.success for reply in result.replies] == [True]


@pytest.mark.unit
def test_empty_comment_surface_requires_comment_specific_surface():
    from executor import user_schema

    generic_textbox = {
        "input_visible": True,
        "comment_input_visible": False,
        "list_visible": False,
        "surface_visible": True,
        "comment_items_visible": 0,
    }
    comment_input = {
        **generic_textbox,
        "comment_input_visible": True,
    }
    empty_list = {
        "input_visible": False,
        "comment_input_visible": False,
        "list_visible": True,
        "surface_visible": True,
        "comment_items_visible": 0,
        "list_source": {"strong_list": True},
    }
    non_empty_list = {
        **empty_list,
        "comment_items_visible": 1,
    }

    assert user_schema._empty_comment_surface_summary(generic_textbox) is None
    input_summary = user_schema._empty_comment_surface_summary(comment_input)
    list_summary = user_schema._empty_comment_surface_summary(empty_list)
    assert input_summary["comment_input"] is True
    assert list_summary["list"] is True
    assert user_schema._empty_comment_surface_outcome(input_summary) == "observer_open_input_only_empty"
    assert user_schema._empty_comment_surface_outcome(list_summary) == "observer_open_list_empty"
    assert user_schema._empty_comment_surface_summary(non_empty_list) is None


@pytest.mark.unit
def test_input_only_empty_surface_requires_two_observer_confirmations():
    from executor import user_schema

    assert user_schema._observer_outcomes_are_inconclusive(
        ["observer_open_input_only_empty"]
    ) is True
    assert user_schema._observer_outcomes_are_inconclusive(
        ["observer_open_input_only_empty", "observer_open_input_only_empty"]
    ) is False
    assert user_schema._observer_outcomes_are_inconclusive(
        ["observer_open_list_empty"]
    ) is False


@pytest.mark.unit
@pytest.mark.asyncio
async def test_execute_fallback_publishes_only_missing_mains_before_replies(monkeypatch):
    from executor import user_schema

    main_account = SimpleNamespace(id=101, proxy=None)
    reply_account = SimpleNamespace(id=201, proxy=None)
    initial_mains = [
        user_schema.PublishedMain(
            text="main one",
            comment_id=None,
            network_confirmed=False,
            local_confirmed=True,
            method="hybrid_dom",
            account_id=main_account.id,
            tiktok_username="main_user",
        ),
        user_schema.PublishedMain(
            text="main two",
            comment_id="hidden-2",
            network_confirmed=True,
            local_confirmed=True,
            method="hybrid_dom",
            account_id=main_account.id,
            tiktok_username="main_user",
        ),
        user_schema.PublishedMain(
            text="main three",
            comment_id="hidden-3",
            network_confirmed=True,
            local_confirmed=True,
            method="hybrid_dom",
            account_id=main_account.id,
            tiktok_username="main_user",
        ),
    ]
    fallback_mains = [
        user_schema.PublishedMain(
            text="main two",
            comment_id="fallback-2",
            network_confirmed=True,
            local_confirmed=True,
            method="hybrid_dom",
            account_id=301,
            tiktok_username="fallback_user",
        ),
        user_schema.PublishedMain(
            text="main three",
            comment_id="fallback-3",
            network_confirmed=True,
            local_confirmed=True,
            method="hybrid_dom",
            account_id=301,
            tiktok_username="fallback_user",
        ),
    ]
    confirm_calls = []
    fallback_calls = []
    reply_calls = []

    async def fake_try_main(*_args, **_kwargs):
        return list(initial_mains), None

    async def fake_confirm(_db, _video_url, published_mains, **kwargs):
        confirm_calls.append([p.text for p in published_mains])
        aggregate = kwargs.get("aggregate_outcomes")
        if aggregate is not None:
            aggregate.append("observer_confirmed")
        if len(confirm_calls) == 1:
            return [published_mains[0]]
        return list(published_mains)

    async def fake_fallback(_db, _video_url, text_variants, **_kwargs):
        fallback_calls.append(list(text_variants))
        return [main for main in fallback_mains if main.text in set(text_variants)]

    def fake_get_reply(_db, *, exclude_ids, **_kwargs):
        return None if reply_account.id in exclude_ids else reply_account

    async def fake_reply(_db, account, _video_url, main_candidates, reply_text, **kwargs):
        reply_calls.append(
            {
                "account": account.id,
                "main_texts": [m.text for m in main_candidates],
                "main_ids": [m.comment_id or "" for m in main_candidates],
                "reply_text": reply_text,
                "variant_index": kwargs["variant_index"],
            }
        )
        return user_schema.ReplyAttemptResult(
            replies=[
                user_schema.ReplyOutcome(
                    text=reply_text,
                    variant_index=kwargs["variant_index"],
                    matched_main_text=main.text,
                    parent_comment_id=main.comment_id,
                    success=True,
                    method="hybrid_dom",
                    error=None,
                    account_id=account.id,
                    attempted=True,
                )
                for main in main_candidates
            ],
            found_mains=list(main_candidates),
            attempted_parent_keys={m.comment_id or user_schema._dedup_key(m.text) for m in main_candidates},
            searched=True,
        )

    async def fake_sleep(_seconds):
        return None

    monkeypatch.setattr(user_schema, "_MAIN_PUBLIC_CONFIRM_ROUNDS", 2)
    monkeypatch.setattr(user_schema, "get_free_account_for_main_comment", lambda *_a, **_k: main_account)
    monkeypatch.setattr(user_schema, "_try_main_account", fake_try_main)
    monkeypatch.setattr(user_schema, "_confirm_mains_by_observers", fake_confirm)
    monkeypatch.setattr(user_schema, "_run_fallback_main_attempts", fake_fallback)
    monkeypatch.setattr(user_schema, "get_free_account_for_reply", fake_get_reply)
    monkeypatch.setattr(user_schema, "_reply_with_variant_on_all_mains", fake_reply)
    monkeypatch.setattr(user_schema, "release_main_account_reservation", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(user_schema, "_maybe_order_likes", AsyncMock(return_value=None))
    monkeypatch.setattr(user_schema.asyncio, "sleep", fake_sleep)

    task = SimpleNamespace(settings={"replies": ["reply one"]})
    result = await user_schema.execute_user_schema_full(
        SimpleNamespace(),
        task,
        "https://www.tiktok.com/@demo/video/1234567890123456789",
        ["main one", "main two", "main three"],
        271,
        boost_enabled=False,
        boost_quantity=0,
    )

    assert confirm_calls[0] == ["main one", "main two", "main three"]
    assert fallback_calls == [["main two", "main three"]]
    assert confirm_calls[1] == ["main two", "main three"]
    assert reply_calls[0]["main_texts"] == ["main one", "main two", "main three"]
    assert reply_calls[0]["main_ids"] == ["", "fallback-2", "fallback-3"]
    assert result.main_found_by_observer is True
    assert result.fallback_main_via_reply is True


@pytest.mark.unit
@pytest.mark.asyncio
async def test_execute_uses_browser_resolved_url_for_observers_and_replies(monkeypatch):
    from executor import user_schema

    short_url = "https://vt.tiktok.com/short/"
    canonical_url = "https://www.tiktok.com/@demo/video/1234567890123456789"
    main_account = SimpleNamespace(id=101, proxy=None)
    reply_account = SimpleNamespace(id=201, proxy=None)
    published = [
        user_schema.PublishedMain(
            text="main one",
            comment_id="cid-1",
            network_confirmed=True,
            local_confirmed=True,
            method="hybrid_dom",
            account_id=main_account.id,
            tiktok_username="main_user",
        )
    ]
    confirm_urls = []
    reply_urls = []

    async def fake_try_main(*_args, **kwargs):
        holder = kwargs.get("resolved_video_url_holder")
        if holder is not None:
            holder["video_url"] = canonical_url
        return list(published), None

    async def fake_confirm(_db, video_url, published_mains, **_kwargs):
        confirm_urls.append(video_url)
        return list(published_mains)

    def fake_get_reply(_db, *, exclude_ids, **_kwargs):
        return None if reply_account.id in exclude_ids else reply_account

    async def fake_reply(_db, _account, video_url, main_candidates, reply_text, **kwargs):
        reply_urls.append(video_url)
        return user_schema.ReplyAttemptResult(
            replies=[
                user_schema.ReplyOutcome(
                    text=reply_text,
                    variant_index=kwargs["variant_index"],
                    matched_main_text=main_candidates[0].text,
                    parent_comment_id=main_candidates[0].comment_id,
                    success=True,
                    method="hybrid_dom",
                    error=None,
                    account_id=reply_account.id,
                    attempted=True,
                )
            ],
            found_mains=list(main_candidates),
            attempted_parent_keys={main_candidates[0].comment_id},
            searched=True,
        )

    async def fake_sleep(_seconds):
        return None

    monkeypatch.setattr(user_schema, "get_free_account_for_main_comment", lambda *_a, **_k: main_account)
    monkeypatch.setattr(user_schema, "_try_main_account", fake_try_main)
    monkeypatch.setattr(user_schema, "_confirm_mains_by_observers", fake_confirm)
    monkeypatch.setattr(user_schema, "get_free_account_for_reply", fake_get_reply)
    monkeypatch.setattr(user_schema, "_reply_with_variant_on_all_mains", fake_reply)
    monkeypatch.setattr(user_schema, "release_main_account_reservation", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(user_schema, "_maybe_order_likes", AsyncMock(return_value=None))
    monkeypatch.setattr(user_schema.asyncio, "sleep", fake_sleep)

    task = SimpleNamespace(settings={"replies": ["reply one"]})
    result = await user_schema.execute_user_schema_full(
        SimpleNamespace(),
        task,
        short_url,
        ["main one"],
        272,
        boost_enabled=False,
        boost_quantity=0,
    )

    assert confirm_urls == [canonical_url]
    assert reply_urls == [canonical_url]
    assert result.replies and result.replies[0].success is True


@pytest.mark.unit
@pytest.mark.asyncio
async def test_parallel_mode_closes_pool_between_main_and_reply_batches(monkeypatch):
    from db import database as db_database
    from executor import browser_pool, user_schema

    @contextmanager
    def fake_db_context():
        yield SimpleNamespace(no_autoflush=SimpleNamespace())

    main_accounts = [SimpleNamespace(id=101 + i, proxy=None) for i in range(3)]
    reply_accounts = [SimpleNamespace(id=201 + i, proxy=None) for i in range(4)]
    selected_main_ids = []
    selected_reply_ids = []
    reply_calls = []
    reply_candidate_counts = []
    close_calls = []
    capacity_requests = []

    def fake_get_main(_db, *, exclude_ids=None, **_kwargs):
        excluded = set(exclude_ids or [])
        for account in main_accounts:
            if account.id not in excluded:
                selected_main_ids.append(account.id)
                return account
        return None

    async def fake_try_main(_db, account, _video_url, texts, **_kwargs):
        return [
            user_schema.PublishedMain(
                text=text,
                comment_id=f"main-{account.id}-{idx}",
                network_confirmed=True,
                local_confirmed=True,
                method="hybrid_dom",
                account_id=account.id,
                tiktok_username=f"user_{account.id}",
            )
            for idx, text in enumerate(texts, start=1)
        ], None

    def fake_get_reply(_db, exclude_ids=None, **_kwargs):
        excluded = set(exclude_ids or [])
        for account in reply_accounts:
            if account.id not in excluded:
                selected_reply_ids.append(account.id)
                return account
        return None

    async def fake_reply(_db, account, _video_url, main_candidates, reply_text, **kwargs):
        reply_calls.append((kwargs["variant_index"], account.id))
        reply_candidate_counts.append(len(main_candidates))
        return user_schema.ReplyAttemptResult(
            replies=[
                user_schema.ReplyOutcome(
                    text=reply_text,
                    variant_index=kwargs["variant_index"],
                    matched_main_text=main.text,
                    parent_comment_id=main.comment_id,
                    success=True,
                    method="hybrid_dom",
                    error=None,
                    account_id=account.id,
                    attempted=True,
                )
                for main in main_candidates
            ],
            found_mains=list(main_candidates),
            searched=True,
        )

    async def fake_close_pool():
        close_calls.append("close")

    monkeypatch.setattr(db_database, "get_sync_db_context", fake_db_context)
    monkeypatch.setattr(user_schema, "get_free_account_for_main_comment", fake_get_main)
    monkeypatch.setattr(user_schema, "_try_main_account", fake_try_main)
    monkeypatch.setattr(user_schema, "release_main_account_reservation", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(user_schema, "get_free_account_for_reply", fake_get_reply)
    monkeypatch.setattr(user_schema, "_reply_with_variant_on_all_mains", fake_reply)
    monkeypatch.setattr(user_schema, "_maybe_order_likes", AsyncMock(return_value=None))
    monkeypatch.setattr(browser_pool, "ensure_thread_browser_pool_capacity", lambda min_browsers: capacity_requests.append(min_browsers))
    monkeypatch.setattr(browser_pool, "close_browser_pool", fake_close_pool)
    monkeypatch.setattr(user_schema.asyncio, "sleep", AsyncMock(return_value=None))

    task = SimpleNamespace(settings={"replies": ["r1", "r2", "r3", "r4"]})
    result = await user_schema.execute_user_schema_parallel(
        SimpleNamespace(),
        task,
        "https://www.tiktok.com/@demo/video/1234567890123456789",
        ["m1", "m2", "m3"],
        501,
        boost_enabled=False,
        boost_quantity=0,
    )

    assert result.error is None
    assert selected_main_ids == [101, 102, 103]
    assert len(result.published_mains) == 9
    assert selected_reply_ids == [201, 202, 203, 204]
    assert [variant for variant, _account_id in reply_calls] == [1, 2, 3, 4]
    assert reply_candidate_counts == [9, 9, 9, 9]
    assert capacity_requests == [3, 3, 3]
    assert close_calls == ["close", "close", "close"]


@pytest.mark.unit
@pytest.mark.asyncio
async def test_parallel_mode_stops_if_browser_capacity_cannot_be_raised(monkeypatch):
    from db import database as db_database
    from executor import browser_pool, user_schema

    @contextmanager
    def fake_db_context():
        yield SimpleNamespace(no_autoflush=SimpleNamespace())

    def fail_if_selects_account(*_args, **_kwargs):
        raise AssertionError("parallel mode must not start account work without browser capacity")

    def fail_capacity(_min_browsers):
        raise RuntimeError("pool unavailable")

    monkeypatch.setattr(db_database, "get_sync_db_context", fake_db_context)
    monkeypatch.setattr(browser_pool, "ensure_thread_browser_pool_capacity", fail_capacity)
    monkeypatch.setattr(user_schema, "get_free_account_for_main_comment", fail_if_selects_account)
    monkeypatch.setattr(user_schema, "_maybe_order_likes", AsyncMock(return_value=None))

    task = SimpleNamespace(settings={"replies": ["r1"]})
    result = await user_schema.execute_user_schema_parallel(
        SimpleNamespace(),
        task,
        "https://www.tiktok.com/@demo/video/1234567890123456789",
        ["m1", "m2", "m3"],
        507,
        boost_enabled=False,
        boost_quantity=0,
    )

    assert result.error == "parallel_browser_capacity_unavailable"
    assert result.published_mains == []
    assert result.replies == []


@pytest.mark.unit
@pytest.mark.asyncio
async def test_parallel_mode_all_reply_accounts_missing_mains_fails_without_fallback(monkeypatch):
    from db import database as db_database
    from executor import browser_pool, user_schema

    @contextmanager
    def fake_db_context():
        yield SimpleNamespace(no_autoflush=SimpleNamespace())

    main_accounts = [SimpleNamespace(id=101 + i, proxy=None) for i in range(3)]
    reply_accounts = [SimpleNamespace(id=201 + i, proxy=None) for i in range(3)]
    fallback_seen = []
    close_calls = []

    def fake_get_main(_db, *, exclude_ids=None, **_kwargs):
        excluded = set(exclude_ids or [])
        return next((account for account in main_accounts if account.id not in excluded), None)

    async def fake_try_main(_db, account, _video_url, texts, **_kwargs):
        return [
            user_schema.PublishedMain(
                text=text,
                comment_id=f"main-{account.id}-{idx}",
                network_confirmed=True,
                local_confirmed=True,
                method="hybrid_dom",
                account_id=account.id,
                tiktok_username=f"user_{account.id}",
            )
            for idx, text in enumerate(texts, start=1)
        ], None

    def fake_get_reply(_db, exclude_ids=None, **_kwargs):
        excluded = set(exclude_ids or [])
        return next((account for account in reply_accounts if account.id not in excluded), None)

    async def fake_reply(_db, account, _video_url, _main_candidates, reply_text, **kwargs):
        fallback_seen.append(kwargs.get("fallback_main_texts"))
        return user_schema.ReplyAttemptResult(
            replies=[
                user_schema.ReplyOutcome(
                    text=reply_text,
                    variant_index=kwargs["variant_index"],
                    matched_main_text=None,
                    parent_comment_id=None,
                    success=False,
                    method=None,
                    error="no_main_found_by_reply_account",
                    account_id=account.id,
                    attempted=False,
                )
            ],
            searched=True,
            confident_empty_search=True,
        )

    async def fake_close_pool():
        close_calls.append("close")

    monkeypatch.setattr(db_database, "get_sync_db_context", fake_db_context)
    monkeypatch.setattr(user_schema, "get_free_account_for_main_comment", fake_get_main)
    monkeypatch.setattr(user_schema, "_try_main_account", fake_try_main)
    monkeypatch.setattr(user_schema, "release_main_account_reservation", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(user_schema, "get_free_account_for_reply", fake_get_reply)
    monkeypatch.setattr(user_schema, "_reply_with_variant_on_all_mains", fake_reply)
    monkeypatch.setattr(user_schema, "_maybe_order_likes", AsyncMock(return_value=None))
    monkeypatch.setattr(browser_pool, "ensure_thread_browser_pool_capacity", lambda _min_browsers: None)
    monkeypatch.setattr(browser_pool, "close_browser_pool", fake_close_pool)
    monkeypatch.setattr(user_schema.asyncio, "sleep", AsyncMock(return_value=None))

    task = SimpleNamespace(settings={"replies": ["r1", "r2", "r3"]})
    result = await user_schema.execute_user_schema_parallel(
        SimpleNamespace(),
        task,
        "https://www.tiktok.com/@demo/video/1234567890123456789",
        ["m1", "m2", "m3"],
        502,
        boost_enabled=False,
        boost_quantity=0,
    )

    assert result.error == "reply_mains_not_found"
    assert fallback_seen == [None, None, None]
    assert close_calls == ["close", "close"]


@pytest.mark.unit
@pytest.mark.asyncio
async def test_parallel_mode_retries_failed_main_slot_with_replacement_before_replies(monkeypatch):
    from db import database as db_database
    from executor import browser_pool, user_schema

    @contextmanager
    def fake_db_context():
        yield SimpleNamespace(no_autoflush=SimpleNamespace())

    main_accounts = [SimpleNamespace(id=101 + i, proxy=None) for i in range(4)]
    reply_account = SimpleNamespace(id=201, proxy=None)
    events = []
    close_calls = []

    def fake_get_main(_db, *, exclude_ids=None, **_kwargs):
        excluded = set(exclude_ids or [])
        return next((account for account in main_accounts if account.id not in excluded), None)

    async def fake_try_main(_db, account, _video_url, texts, **_kwargs):
        events.append(("main", account.id, tuple(texts)))
        if account.id == 102:
            return [], "captcha_challenge:visible"
        return [
            user_schema.PublishedMain(
                text=text,
                comment_id=f"cid-{account.id}-{text}",
                network_confirmed=True,
                local_confirmed=True,
                method="hybrid_dom",
                account_id=account.id,
                tiktok_username=f"user_{account.id}",
            )
            for text in texts
        ], None

    def fake_get_reply(_db, exclude_ids=None, **_kwargs):
        return reply_account

    async def fake_reply(_db, account, _video_url, main_candidates, reply_text, **kwargs):
        events.append(("reply", account.id, kwargs["variant_index"]))
        return user_schema.ReplyAttemptResult(
            replies=[
                user_schema.ReplyOutcome(
                    text=reply_text,
                    variant_index=kwargs["variant_index"],
                    matched_main_text=main.text,
                    parent_comment_id=main.comment_id,
                    success=True,
                    method="hybrid_dom",
                    error=None,
                    account_id=account.id,
                    attempted=True,
                )
                for main in main_candidates
            ],
            found_mains=list(main_candidates),
            searched=True,
        )

    async def fake_close_pool():
        close_calls.append("close")

    monkeypatch.setattr(db_database, "get_sync_db_context", fake_db_context)
    monkeypatch.setattr(user_schema, "get_free_account_for_main_comment", fake_get_main)
    monkeypatch.setattr(user_schema, "_try_main_account", fake_try_main)
    monkeypatch.setattr(user_schema, "_record_main_retry_failure", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(user_schema, "release_main_account_reservation", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(user_schema, "get_free_account_for_reply", fake_get_reply)
    monkeypatch.setattr(user_schema, "_reply_with_variant_on_all_mains", fake_reply)
    monkeypatch.setattr(user_schema, "_maybe_order_likes", AsyncMock(return_value=None))
    monkeypatch.setattr(browser_pool, "ensure_thread_browser_pool_capacity", lambda _min_browsers: None)
    monkeypatch.setattr(browser_pool, "close_browser_pool", fake_close_pool)
    monkeypatch.setattr(user_schema.asyncio, "sleep", AsyncMock(return_value=None))

    task = SimpleNamespace(settings={"replies": ["r1"]})
    result = await user_schema.execute_user_schema_parallel(
        SimpleNamespace(),
        task,
        "https://www.tiktok.com/@demo/video/1234567890123456789",
        ["m1", "m2", "m3"],
        503,
        boost_enabled=False,
        boost_quantity=0,
    )

    assert result.error is None
    assert [event for event in events if event[0] == "main"] == [
        ("main", 101, ("m1", "m2", "m3")),
        ("main", 102, ("m1", "m2", "m3")),
        ("main", 103, ("m1", "m2", "m3")),
        ("main", 104, ("m1", "m2", "m3")),
    ]
    assert events[-1][0] == "reply"
    assert [main.account_id for main in result.published_mains] == [101, 101, 101, 104, 104, 104, 103, 103, 103]
    assert [main.text for main in result.published_mains] == ["m1", "m2", "m3"] * 3
    assert close_calls == ["close", "close", "close"]


@pytest.mark.unit
@pytest.mark.asyncio
async def test_parallel_mode_keeps_partially_published_main_account_slot(monkeypatch):
    from db import database as db_database
    from executor import browser_pool, user_schema

    @contextmanager
    def fake_db_context():
        yield SimpleNamespace(no_autoflush=SimpleNamespace())

    main_accounts = [SimpleNamespace(id=101 + i, proxy=None) for i in range(4)]
    reply_account = SimpleNamespace(id=201, proxy=None)
    main_calls = []
    reply_candidate_counts = []

    def fake_get_main(_db, *, exclude_ids=None, **_kwargs):
        excluded = set(exclude_ids or [])
        return next((account for account in main_accounts if account.id not in excluded), None)

    async def fake_try_main(_db, account, _video_url, texts, **_kwargs):
        main_calls.append((account.id, tuple(texts)))
        published_texts = list(texts)
        error = None
        if account.id == 101:
            published_texts = list(texts[:2])
            error = "captcha_challenge:visible"
        return [
            user_schema.PublishedMain(
                text=text,
                comment_id=f"cid-{account.id}-{text}",
                network_confirmed=True,
                local_confirmed=True,
                method="hybrid_dom",
                account_id=account.id,
                tiktok_username=f"user_{account.id}",
            )
            for text in published_texts
        ], error

    def fake_get_reply(_db, exclude_ids=None, **_kwargs):
        return reply_account

    async def fake_reply(_db, account, _video_url, main_candidates, reply_text, **kwargs):
        reply_candidate_counts.append(len(main_candidates))
        return user_schema.ReplyAttemptResult(
            replies=[
                user_schema.ReplyOutcome(
                    text=reply_text,
                    variant_index=kwargs["variant_index"],
                    matched_main_text=main.text,
                    parent_comment_id=main.comment_id,
                    success=True,
                    method="hybrid_dom",
                    error=None,
                    account_id=account.id,
                    attempted=True,
                )
                for main in main_candidates
            ],
            found_mains=list(main_candidates),
            searched=True,
        )

    monkeypatch.setattr(db_database, "get_sync_db_context", fake_db_context)
    monkeypatch.setattr(user_schema, "get_free_account_for_main_comment", fake_get_main)
    monkeypatch.setattr(user_schema, "_try_main_account", fake_try_main)
    monkeypatch.setattr(user_schema, "_record_main_retry_failure", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(user_schema, "release_main_account_reservation", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(user_schema, "get_free_account_for_reply", fake_get_reply)
    monkeypatch.setattr(user_schema, "_reply_with_variant_on_all_mains", fake_reply)
    monkeypatch.setattr(user_schema, "_maybe_order_likes", AsyncMock(return_value=None))
    monkeypatch.setattr(browser_pool, "ensure_thread_browser_pool_capacity", lambda _min_browsers: None)
    monkeypatch.setattr(browser_pool, "close_browser_pool", AsyncMock(return_value=None))
    monkeypatch.setattr(user_schema.asyncio, "sleep", AsyncMock(return_value=None))

    task = SimpleNamespace(settings={"replies": ["r1"]})
    result = await user_schema.execute_user_schema_parallel(
        SimpleNamespace(),
        task,
        "https://www.tiktok.com/@demo/video/1234567890123456789",
        ["m1", "m2", "m3"],
        508,
        boost_enabled=False,
        boost_quantity=0,
    )

    assert result.error is None
    assert main_calls == [
        (101, ("m1", "m2", "m3")),
        (102, ("m1", "m2", "m3")),
        (103, ("m1", "m2", "m3")),
    ]
    assert [main.account_id for main in result.published_mains] == [101, 101, 102, 102, 102, 103, 103, 103]
    assert [main.text for main in result.published_mains] == ["m1", "m2", "m1", "m2", "m3", "m1", "m2", "m3"]
    assert reply_candidate_counts == [8]


@pytest.mark.unit
@pytest.mark.asyncio
async def test_parallel_mode_reply_variant_targets_all_duplicate_main_comments(monkeypatch):
    from db import database as db_database
    from executor import browser_pool, user_schema

    @contextmanager
    def fake_db_context():
        yield SimpleNamespace(no_autoflush=SimpleNamespace())

    main_accounts = [SimpleNamespace(id=101 + i, proxy=None) for i in range(3)]
    reply_account = SimpleNamespace(id=201, proxy=None)
    seen_reply_targets = []

    def fake_get_main(_db, *, exclude_ids=None, **_kwargs):
        excluded = set(exclude_ids or [])
        return next((account for account in main_accounts if account.id not in excluded), None)

    async def fake_try_main(_db, account, _video_url, texts, **_kwargs):
        return [
            user_schema.PublishedMain(
                text=text,
                comment_id=f"cid-{account.id}-{idx}",
                network_confirmed=True,
                local_confirmed=True,
                method="hybrid_dom",
                account_id=account.id,
                tiktok_username=f"user_{account.id}",
            )
            for idx, text in enumerate(texts, start=1)
        ], None

    def fake_get_reply(_db, exclude_ids=None, **_kwargs):
        return reply_account

    async def fake_reply(_db, account, _video_url, main_candidates, reply_text, **kwargs):
        seen_reply_targets.append([
            (main.account_id, main.text, main.comment_id)
            for main in main_candidates
        ])
        return user_schema.ReplyAttemptResult(
            replies=[
                user_schema.ReplyOutcome(
                    text=reply_text,
                    variant_index=kwargs["variant_index"],
                    matched_main_text=main.text,
                    parent_comment_id=main.comment_id,
                    success=True,
                    method="hybrid_dom",
                    error=None,
                    account_id=account.id,
                    attempted=True,
                )
                for main in main_candidates
            ],
            found_mains=list(main_candidates),
            searched=True,
        )

    monkeypatch.setattr(db_database, "get_sync_db_context", fake_db_context)
    monkeypatch.setattr(user_schema, "get_free_account_for_main_comment", fake_get_main)
    monkeypatch.setattr(user_schema, "_try_main_account", fake_try_main)
    monkeypatch.setattr(user_schema, "release_main_account_reservation", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(user_schema, "get_free_account_for_reply", fake_get_reply)
    monkeypatch.setattr(user_schema, "_reply_with_variant_on_all_mains", fake_reply)
    monkeypatch.setattr(user_schema, "_maybe_order_likes", AsyncMock(return_value=None))
    monkeypatch.setattr(browser_pool, "ensure_thread_browser_pool_capacity", lambda _min_browsers: None)
    monkeypatch.setattr(browser_pool, "close_browser_pool", AsyncMock(return_value=None))

    task = SimpleNamespace(settings={"replies": ["r1"]})
    result = await user_schema.execute_user_schema_parallel(
        SimpleNamespace(),
        task,
        "https://www.tiktok.com/@demo/video/1234567890123456789",
        ["m1", "m2", "m3"],
        509,
        boost_enabled=False,
        boost_quantity=0,
    )

    assert result.error is None
    assert len(result.published_mains) == 9
    assert len(seen_reply_targets) == 1
    assert len(seen_reply_targets[0]) == 9
    assert [text for _account_id, text, _cid in seen_reply_targets[0]] == ["m1", "m2", "m3"] * 3
    assert len(result.replies) == 9
    assert [outcome.parent_comment_id for outcome in result.replies] == [
        f"cid-{account_id}-{idx}"
        for account_id in (101, 102, 103)
        for idx in (1, 2, 3)
    ]


@pytest.mark.unit
@pytest.mark.asyncio
async def test_parallel_mode_stops_main_phase_after_two_empty_main_batches(monkeypatch):
    from db import database as db_database
    from executor import browser_pool, user_schema

    @contextmanager
    def fake_db_context():
        yield SimpleNamespace(no_autoflush=SimpleNamespace())

    main_accounts = [SimpleNamespace(id=101 + i, proxy=None) for i in range(8)]
    main_attempts = []
    close_calls = []

    def fake_get_main(_db, *, exclude_ids=None, **_kwargs):
        excluded = set(exclude_ids or [])
        return next((account for account in main_accounts if account.id not in excluded), None)

    async def fake_try_main(_db, account, _video_url, texts, **_kwargs):
        main_attempts.append((account.id, tuple(texts)))
        return [], "captcha_challenge:visible"

    async def fake_close_pool():
        close_calls.append("close")

    monkeypatch.setattr(user_schema._settings, "MAX_RETRY_ACCOUNTS", 30, raising=False)
    monkeypatch.setattr(user_schema._settings, "PARALLEL_COMMENTING_MAX_MAIN_ATTEMPTS", 30, raising=False)
    monkeypatch.setattr(user_schema._settings, "PARALLEL_COMMENTING_MAX_REPLY_ATTEMPTS", 30, raising=False)
    monkeypatch.setattr(db_database, "get_sync_db_context", fake_db_context)
    monkeypatch.setattr(user_schema, "get_free_account_for_main_comment", fake_get_main)
    monkeypatch.setattr(user_schema, "_try_main_account", fake_try_main)
    monkeypatch.setattr(user_schema, "_record_main_retry_failure", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(user_schema, "release_main_account_reservation", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(user_schema, "_maybe_order_likes", AsyncMock(return_value=None))
    monkeypatch.setattr(browser_pool, "ensure_thread_browser_pool_capacity", lambda _min_browsers: None)
    monkeypatch.setattr(browser_pool, "close_browser_pool", fake_close_pool)
    monkeypatch.setattr(user_schema.asyncio, "sleep", AsyncMock(return_value=None))

    task = SimpleNamespace(settings={"replies": ["r1"]})
    result = await user_schema.execute_user_schema_parallel(
        SimpleNamespace(),
        task,
        "https://www.tiktok.com/@demo/video/1234567890123456789",
        ["m1", "m2", "m3"],
        506,
        boost_enabled=False,
        boost_quantity=0,
    )

    assert result.error == "parallel_main_no_publications_after_two_batches"
    assert len(main_attempts) == 6
    assert close_calls == ["close", "close"]


@pytest.mark.unit
@pytest.mark.asyncio
async def test_parallel_mode_excludes_failed_main_accounts_from_reply_selection(monkeypatch):
    from db import database as db_database
    from executor import browser_pool, user_schema

    @contextmanager
    def fake_db_context():
        yield SimpleNamespace(no_autoflush=SimpleNamespace())

    main_accounts = [SimpleNamespace(id=101 + i, proxy=None) for i in range(4)]
    reply_accounts = [SimpleNamespace(id=101, proxy=None), SimpleNamespace(id=201, proxy=None)]
    reply_exclude_snapshots = []

    def fake_get_main(_db, *, exclude_ids=None, **_kwargs):
        excluded = set(exclude_ids or [])
        return next((account for account in main_accounts if account.id not in excluded), None)

    async def fake_try_main(_db, account, _video_url, texts, **_kwargs):
        if account.id == 101:
            return [], "captcha_challenge:visible"
        return [
            user_schema.PublishedMain(
                text=text,
                comment_id=f"cid-{account.id}-{text}",
                network_confirmed=True,
                local_confirmed=True,
                method="hybrid_dom",
                account_id=account.id,
                tiktok_username=f"user_{account.id}",
            )
            for text in texts
        ], None

    def fake_get_reply(_db, exclude_ids=None, **_kwargs):
        excluded = set(exclude_ids or [])
        reply_exclude_snapshots.append(excluded)
        return next((account for account in reply_accounts if account.id not in excluded), None)

    async def fake_reply(_db, account, _video_url, main_candidates, reply_text, **kwargs):
        return user_schema.ReplyAttemptResult(
            replies=[
                user_schema.ReplyOutcome(
                    text=reply_text,
                    variant_index=kwargs["variant_index"],
                    matched_main_text=main.text,
                    parent_comment_id=main.comment_id,
                    success=True,
                    method="hybrid_dom",
                    error=None,
                    account_id=account.id,
                    attempted=True,
                )
                for main in main_candidates
            ],
            found_mains=list(main_candidates),
            searched=True,
        )

    monkeypatch.setattr(user_schema._settings, "MAX_RETRY_ACCOUNTS", 30, raising=False)
    monkeypatch.setattr(user_schema._settings, "PARALLEL_COMMENTING_MAX_MAIN_ATTEMPTS", 30, raising=False)
    monkeypatch.setattr(user_schema._settings, "PARALLEL_COMMENTING_MAX_REPLY_ATTEMPTS", 30, raising=False)
    monkeypatch.setattr(db_database, "get_sync_db_context", fake_db_context)
    monkeypatch.setattr(user_schema, "get_free_account_for_main_comment", fake_get_main)
    monkeypatch.setattr(user_schema, "_try_main_account", fake_try_main)
    monkeypatch.setattr(user_schema, "_record_main_retry_failure", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(user_schema, "release_main_account_reservation", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(user_schema, "get_free_account_for_reply", fake_get_reply)
    monkeypatch.setattr(user_schema, "_reply_with_variant_on_all_mains", fake_reply)
    monkeypatch.setattr(user_schema, "_maybe_order_likes", AsyncMock(return_value=None))
    monkeypatch.setattr(browser_pool, "ensure_thread_browser_pool_capacity", lambda _min_browsers: None)
    monkeypatch.setattr(browser_pool, "close_browser_pool", AsyncMock(return_value=None))
    monkeypatch.setattr(user_schema.asyncio, "sleep", AsyncMock(return_value=None))

    task = SimpleNamespace(settings={"replies": ["reply one"]})
    result = await user_schema.execute_user_schema_parallel(
        SimpleNamespace(),
        task,
        "https://www.tiktok.com/@demo/video/1234567890123456789",
        ["m1", "m2", "m3"],
        507,
        boost_enabled=False,
        boost_quantity=0,
    )

    assert result.error is None
    assert reply_exclude_snapshots
    assert {101, 102, 103, 104}.issubset(reply_exclude_snapshots[0])


@pytest.mark.unit
@pytest.mark.asyncio
async def test_parallel_mode_caps_active_main_and_reply_attempts_at_three(monkeypatch):
    from db import database as db_database
    from executor import browser_pool, user_schema

    @contextmanager
    def fake_db_context():
        yield SimpleNamespace(no_autoflush=SimpleNamespace())

    main_accounts = [SimpleNamespace(id=101 + i, proxy=None) for i in range(3)]
    reply_accounts = [SimpleNamespace(id=201 + i, proxy=None) for i in range(7)]
    active_main = 0
    active_reply = 0
    max_active_main = 0
    max_active_reply = 0
    close_calls = []

    def fake_get_main(_db, *, exclude_ids=None, **_kwargs):
        excluded = set(exclude_ids or [])
        return next((account for account in main_accounts if account.id not in excluded), None)

    async def fake_try_main(_db, account, _video_url, texts, **_kwargs):
        nonlocal active_main, max_active_main
        active_main += 1
        max_active_main = max(max_active_main, active_main)
        await asyncio.sleep(0.01)
        active_main -= 1
        return [
            user_schema.PublishedMain(
                text=text,
                comment_id=f"cid-{account.id}-{text}",
                network_confirmed=True,
                local_confirmed=True,
                method="hybrid_dom",
                account_id=account.id,
                tiktok_username=f"user_{account.id}",
            )
            for text in texts
        ], None

    def fake_get_reply(_db, exclude_ids=None, **_kwargs):
        excluded = set(exclude_ids or [])
        return next((account for account in reply_accounts if account.id not in excluded), None)

    async def fake_reply(_db, account, _video_url, main_candidates, reply_text, **kwargs):
        nonlocal active_reply, max_active_reply
        active_reply += 1
        max_active_reply = max(max_active_reply, active_reply)
        await asyncio.sleep(0.01)
        active_reply -= 1
        return user_schema.ReplyAttemptResult(
            replies=[
                user_schema.ReplyOutcome(
                    text=reply_text,
                    variant_index=kwargs["variant_index"],
                    matched_main_text=main.text,
                    parent_comment_id=main.comment_id,
                    success=True,
                    method="hybrid_dom",
                    error=None,
                    account_id=account.id,
                    attempted=True,
                )
                for main in main_candidates
            ],
            found_mains=list(main_candidates),
            searched=True,
        )

    async def fake_close_pool():
        close_calls.append("close")

    monkeypatch.setattr(db_database, "get_sync_db_context", fake_db_context)
    monkeypatch.setattr(user_schema, "get_free_account_for_main_comment", fake_get_main)
    monkeypatch.setattr(user_schema, "_try_main_account", fake_try_main)
    monkeypatch.setattr(user_schema, "release_main_account_reservation", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(user_schema, "get_free_account_for_reply", fake_get_reply)
    monkeypatch.setattr(user_schema, "_reply_with_variant_on_all_mains", fake_reply)
    monkeypatch.setattr(user_schema, "_maybe_order_likes", AsyncMock(return_value=None))
    monkeypatch.setattr(browser_pool, "ensure_thread_browser_pool_capacity", lambda _min_browsers: None)
    monkeypatch.setattr(browser_pool, "close_browser_pool", fake_close_pool)

    task = SimpleNamespace(settings={"replies": ["r1", "r2", "r3", "r4", "r5", "r6", "r7"]})
    result = await user_schema.execute_user_schema_parallel(
        SimpleNamespace(),
        task,
        "https://www.tiktok.com/@demo/video/1234567890123456789",
        ["m1", "m2", "m3", "m4"],
        504,
        boost_enabled=False,
        boost_quantity=0,
    )

    assert result.error is None
    assert len(result.published_mains) == 9
    assert len(result.replies) == 63
    assert max_active_main == 3
    assert max_active_reply == 3
    assert close_calls == ["close", "close", "close", "close"]


@pytest.mark.unit
@pytest.mark.asyncio
async def test_parallel_mode_carries_submitted_parent_keys_across_reply_retries(monkeypatch):
    from db import database as db_database
    from executor import browser_pool, user_schema

    @contextmanager
    def fake_db_context():
        yield SimpleNamespace(no_autoflush=SimpleNamespace())

    main_account = SimpleNamespace(id=101, proxy=None)
    reply_accounts = [SimpleNamespace(id=201, proxy=None), SimpleNamespace(id=202, proxy=None)]
    reply_calls = []

    def fake_get_main(_db, *, exclude_ids=None, **_kwargs):
        return main_account

    async def fake_try_main(_db, account, _video_url, texts, **_kwargs):
        return [
            user_schema.PublishedMain(
                text=text,
                comment_id=f"cid-{account.id}-{text}",
                network_confirmed=True,
                local_confirmed=True,
                method="hybrid_dom",
                account_id=account.id,
                tiktok_username="main_user",
            )
            for text in texts
        ], None

    def fake_get_reply(_db, exclude_ids=None, **_kwargs):
        excluded = set(exclude_ids or [])
        return next((account for account in reply_accounts if account.id not in excluded), None)

    async def fake_reply(_db, account, _video_url, main_candidates, reply_text, **kwargs):
        submitted_keys = set(kwargs["already_replied_parent_keys"])
        reply_calls.append((account.id, submitted_keys))
        first, second = main_candidates[0], main_candidates[1]
        if account.id == 201:
            return user_schema.ReplyAttemptResult(
                replies=[
                    user_schema.ReplyOutcome(
                        text=reply_text,
                        variant_index=kwargs["variant_index"],
                        matched_main_text=first.text,
                        parent_comment_id=first.comment_id,
                        success=False,
                        method="hybrid_dom",
                        error="stealth_reject_empty_body",
                        account_id=account.id,
                        attempted=True,
                        submitted_unconfirmed=True,
                    )
                ],
                found_mains=[first, second],
                submitted_parent_keys_count=1,
                searched=True,
            )
        assert first.comment_id in submitted_keys
        return user_schema.ReplyAttemptResult(
            replies=[
                user_schema.ReplyOutcome(
                    text=reply_text,
                    variant_index=kwargs["variant_index"],
                    matched_main_text=second.text,
                    parent_comment_id=second.comment_id,
                    success=True,
                    method="hybrid_dom",
                    error=None,
                    account_id=account.id,
                    attempted=True,
                )
            ],
            found_mains=[first, second],
            searched=True,
        )

    async def fake_close_pool():
        return None

    monkeypatch.setattr(db_database, "get_sync_db_context", fake_db_context)
    monkeypatch.setattr(user_schema, "get_free_account_for_main_comment", fake_get_main)
    monkeypatch.setattr(user_schema, "_try_main_account", fake_try_main)
    monkeypatch.setattr(user_schema, "release_main_account_reservation", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(user_schema, "get_free_account_for_reply", fake_get_reply)
    monkeypatch.setattr(user_schema, "_reply_with_variant_on_all_mains", fake_reply)
    monkeypatch.setattr(user_schema, "_maybe_order_likes", AsyncMock(return_value=None))
    monkeypatch.setattr(browser_pool, "ensure_thread_browser_pool_capacity", lambda _min_browsers: None)
    monkeypatch.setattr(browser_pool, "close_browser_pool", fake_close_pool)
    monkeypatch.setattr(user_schema.asyncio, "sleep", AsyncMock(return_value=None))

    task = SimpleNamespace(settings={"replies": ["reply one"]})
    result = await user_schema.execute_user_schema_parallel(
        SimpleNamespace(),
        task,
        "https://www.tiktok.com/@demo/video/1234567890123456789",
        ["m1", "m2"],
        505,
        boost_enabled=False,
        boost_quantity=0,
    )

    assert result.error is None
    assert reply_calls[0] == (201, set())
    assert reply_calls[1][0] == 202
    assert "cid-101-m1" in reply_calls[1][1]
    assert "m1" not in reply_calls[1][1]
    assert [outcome.parent_comment_id for outcome in result.replies] == ["cid-101-m1", "cid-101-m2"]
    assert result.replies[0].submitted_unconfirmed is True


@pytest.mark.unit
@pytest.mark.asyncio
async def test_parallel_mode_retries_reply_variant_when_only_one_of_nine_mains_found(monkeypatch):
    from db import database as db_database
    from executor import browser_pool, user_schema

    @contextmanager
    def fake_db_context():
        yield SimpleNamespace(no_autoflush=SimpleNamespace())

    main_accounts = [SimpleNamespace(id=101 + i, proxy=None) for i in range(3)]
    reply_accounts = [SimpleNamespace(id=201), SimpleNamespace(id=202)]
    selected_main_ids = []
    reply_calls = []

    def fake_get_main(_db, *, exclude_ids=None, **_kwargs):
        excluded = set(exclude_ids or [])
        account = next((item for item in main_accounts if item.id not in excluded), None)
        if account:
            selected_main_ids.append(account.id)
        return account

    async def fake_try_main(_db, account, _video_url, texts, **_kwargs):
        return [
            user_schema.PublishedMain(
                text=text,
                comment_id=f"cid-{account.id}-{idx}",
                network_confirmed=True,
                local_confirmed=True,
                method="hybrid_dom",
                account_id=account.id,
                tiktok_username=f"user_{account.id}",
            )
            for idx, text in enumerate(texts, start=1)
        ], None

    def fake_get_reply(_db, exclude_ids=None, **_kwargs):
        excluded = set(exclude_ids or [])
        return next((item for item in reply_accounts if item.id not in excluded), None)

    async def fake_reply(_db, account, _video_url, main_candidates, reply_text, **kwargs):
        already = set(kwargs["already_replied_parent_keys"])
        reply_calls.append((account.id, set(already), len(main_candidates)))
        pending = [
            main
            for main in main_candidates
            if str(main.comment_id) not in already
        ]
        if account.id == 201:
            found = [pending[0]]
        else:
            assert len(pending) == 8
            found = list(pending)
        return user_schema.ReplyAttemptResult(
            replies=[
                user_schema.ReplyOutcome(
                    text=reply_text,
                    variant_index=kwargs["variant_index"],
                    matched_main_text=main.text,
                    parent_comment_id=main.comment_id,
                    success=True,
                    method="hybrid_dom",
                    error=None,
                    account_id=account.id,
                    attempted=True,
                )
                for main in found
            ],
            found_mains=found,
            searched=True,
        )

    async def fake_close_pool():
        return None

    monkeypatch.setattr(db_database, "get_sync_db_context", fake_db_context)
    monkeypatch.setattr(user_schema, "get_free_account_for_main_comment", fake_get_main)
    monkeypatch.setattr(user_schema, "_try_main_account", fake_try_main)
    monkeypatch.setattr(user_schema, "release_main_account_reservation", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(user_schema, "get_free_account_for_reply", fake_get_reply)
    monkeypatch.setattr(user_schema, "_reply_with_variant_on_all_mains", fake_reply)
    monkeypatch.setattr(user_schema, "_maybe_order_likes", AsyncMock(return_value=None))
    monkeypatch.setattr(browser_pool, "ensure_thread_browser_pool_capacity", lambda _min_browsers: None)
    monkeypatch.setattr(browser_pool, "close_browser_pool", fake_close_pool)
    monkeypatch.setattr(user_schema.asyncio, "sleep", AsyncMock(return_value=None))

    task = SimpleNamespace(settings={"replies": ["reply one"]})
    result = await user_schema.execute_user_schema_parallel(
        SimpleNamespace(),
        task,
        "https://www.tiktok.com/@demo/video/1234567890123456789",
        ["m1", "m2", "m3"],
        506,
        boost_enabled=False,
        boost_quantity=0,
    )

    assert result.error is None
    assert len(result.published_mains) == 9
    assert len(result.replies) == 9
    assert reply_calls[0] == (201, set(), 9)
    assert reply_calls[1][0] == 202
    assert reply_calls[1][2] == 9
    assert "cid-101-1" in reply_calls[1][1]


@pytest.mark.unit
def test_reply_outcome_inference_does_not_expand_duplicate_text_only():
    from executor import user_schema

    first = user_schema.PublishedMain(
        text="same text",
        comment_id=None,
        network_confirmed=False,
        local_confirmed=True,
        method="hybrid_dom",
        account_id=101,
        tiktok_username="user_101",
    )
    second = user_schema.PublishedMain(
        text="same text",
        comment_id=None,
        network_confirmed=False,
        local_confirmed=True,
        method="hybrid_dom",
        account_id=102,
        tiktok_username="user_102",
    )
    outcome = user_schema.ReplyOutcome(
        text="reply",
        variant_index=1,
        matched_main_text="same text",
        parent_comment_id=None,
        success=True,
        method="hybrid_dom",
        error=None,
        account_id=201,
        attempted=True,
    )

    assert user_schema._infer_found_mains_from_reply_outcomes([outcome], [first, second]) == []
    assert user_schema._reply_outcomes_submitted_parent_keys_for_expected_mains(
        [outcome],
        [first, second],
    ) == set()


@pytest.mark.unit
def test_reply_outcome_parent_key_maps_cid_main_without_text_expansion():
    from executor import user_schema

    first = user_schema.PublishedMain(
        text="same text",
        comment_id="cid-101",
        network_confirmed=True,
        local_confirmed=True,
        method="hybrid_dom",
        account_id=101,
        tiktok_username="user_101",
    )
    second = user_schema.PublishedMain(
        text="same text",
        comment_id="cid-102",
        network_confirmed=True,
        local_confirmed=True,
        method="hybrid_dom",
        account_id=102,
        tiktok_username="user_102",
    )
    outcome = user_schema.ReplyOutcome(
        text="reply",
        variant_index=1,
        matched_main_text="same text",
        parent_comment_id=None,
        success=True,
        method="hybrid_dom",
        error=None,
        account_id=201,
        attempted=True,
        parent_key="account:101:text:same text",
    )

    assert user_schema._infer_found_mains_from_reply_outcomes([outcome], [first, second]) == [first]
    assert user_schema._reply_outcomes_submitted_parent_keys_for_expected_mains(
        [outcome],
        [first, second],
    ) == {"cid-101"}


@pytest.mark.unit
@pytest.mark.asyncio
async def test_execute_retries_same_reply_variant_when_parent_found_but_publish_not_reached(monkeypatch):
    from executor import user_schema

    main_account = SimpleNamespace(id=101, proxy=None)
    reply_accounts = [
        SimpleNamespace(id=201, proxy=None),
        SimpleNamespace(id=202, proxy=None),
    ]
    published = [
        user_schema.PublishedMain(
            text="main one",
            comment_id="cid-1",
            network_confirmed=True,
            local_confirmed=True,
            method="hybrid_dom",
            account_id=main_account.id,
            tiktok_username="main_user",
        ),
        user_schema.PublishedMain(
            text="main two",
            comment_id="cid-2",
            network_confirmed=True,
            local_confirmed=True,
            method="hybrid_dom",
            account_id=main_account.id,
            tiktok_username="main_user",
        ),
    ]
    calls = []

    async def fake_try_main(*_args, **_kwargs):
        return list(published), None

    async def fake_confirm(_db, _video_url, published_mains, **_kwargs):
        return list(published_mains)

    def fake_get_reply(_db, *, exclude_ids, **_kwargs):
        for account in reply_accounts:
            if account.id not in exclude_ids:
                return account
        return None

    async def fake_reply(_db, account, _video_url, main_candidates, reply_text, **kwargs):
        variant_idx = kwargs["variant_index"]
        calls.append((variant_idx, account.id, [m.text for m in main_candidates]))
        found = [main_candidates[0]]
        if account.id == 201:
            return user_schema.ReplyAttemptResult(
                replies=[
                    user_schema.ReplyOutcome(
                        text=reply_text,
                        variant_index=variant_idx,
                        matched_main_text=found[0].text,
                        parent_comment_id=found[0].comment_id,
                        success=False,
                        method=None,
                        error="url_drift_to_other_video",
                        account_id=account.id,
                        attempted=False,
                    )
                ],
                found_mains=found,
                attempted_parent_keys=set(),
                searched=True,
            )
        return user_schema.ReplyAttemptResult(
            replies=[
                user_schema.ReplyOutcome(
                    text=reply_text,
                    variant_index=variant_idx,
                    matched_main_text=found[0].text,
                    parent_comment_id=found[0].comment_id,
                    success=False,
                    method="hybrid_dom",
                    error="stealth_reject_empty_body",
                    account_id=account.id,
                    attempted=True,
                    submitted_unconfirmed=True,
                )
            ],
            found_mains=found,
            attempted_parent_keys={found[0].comment_id},
            submitted_parent_keys_count=1,
            searched=True,
        )

    async def fake_sleep(_seconds):
        return None

    async def fake_likes(*_args, **_kwargs):
        return None

    monkeypatch.setattr(user_schema, "get_free_account_for_main_comment", lambda *_a, **_k: main_account)
    monkeypatch.setattr(user_schema, "_try_main_account", fake_try_main)
    monkeypatch.setattr(user_schema, "_confirm_mains_by_observers", fake_confirm)
    monkeypatch.setattr(user_schema, "release_main_account_reservation", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(user_schema, "get_free_account_for_reply", fake_get_reply)
    monkeypatch.setattr(user_schema, "_reply_with_variant_on_all_mains", fake_reply)
    monkeypatch.setattr(user_schema, "_maybe_order_likes", fake_likes)
    monkeypatch.setattr(user_schema.asyncio, "sleep", fake_sleep)

    task = SimpleNamespace(settings={"replies": ["reply one"]})
    result = await user_schema.execute_user_schema_full(
        SimpleNamespace(),
        task,
        "https://www.tiktok.com/@demo/video/1234567890123456789",
        ["main one", "main two"],
        256,
        boost_enabled=False,
        boost_quantity=0,
    )

    assert calls == [
        (1, 201, ["main one", "main two"]),
        (1, 202, ["main one"]),
    ]
    assert len(result.replies) == 1
    assert result.replies[0].account_id == 202
    assert result.replies[0].attempted is True


@pytest.mark.unit
@pytest.mark.asyncio
async def test_maybe_order_likes_prefers_active_main_username_after_fallback(monkeypatch):
    from executor import user_schema
    from services import boost_project_client

    ordered = []

    async def fake_order(video_url, username, *, quantity):
        ordered.append((video_url, username, quantity))
        return True, 555, None

    monkeypatch.setattr(boost_project_client, "is_boost_enabled", lambda: True)
    monkeypatch.setattr(boost_project_client, "order_tiktok_comment_likes", fake_order)

    result = user_schema.UserSchemaResult(boost_enabled=True, boost_quantity=50)
    result.main_found_by_observer = True
    result.main_tiktok_username = "fallback_user"
    result.published_mains.extend(
        [
            user_schema.PublishedMain(
                text="old main",
                comment_id="old-cid",
                network_confirmed=True,
                local_confirmed=True,
                method="hybrid_dom",
                account_id=101,
                tiktok_username="old_main_user",
            ),
            user_schema.PublishedMain(
                text="fallback main",
                comment_id="fallback-cid",
                network_confirmed=True,
                local_confirmed=True,
                method="hybrid_dom",
                account_id=201,
                tiktok_username="fallback_user",
            ),
        ]
    )

    await user_schema._maybe_order_likes(
        result,
        video_url="https://www.tiktok.com/@demo/video/1234567890123456789",
        task_id=254,
        boost_enabled=True,
        boost_quantity=50,
    )

    assert ordered == [
        (
            "https://www.tiktok.com/@demo/video/1234567890123456789",
            "fallback_user",
            50,
        )
    ]
    assert result.boost_order_id == 555


@pytest.mark.unit
def test_main_parent_keys_include_comment_id_and_text_fallback():
    from executor import user_schema

    main = user_schema.PublishedMain(
        text="Main Text",
        comment_id="cid-stored",
        network_confirmed=True,
        local_confirmed=True,
        method="hybrid_dom",
        account_id=10,
        tiktok_username=None,
    )

    keys = user_schema._main_parent_keys(main)

    assert "cid-stored" in keys
    assert user_schema._dedup_key("Main Text") in keys


@pytest.mark.unit
def test_main_submission_keys_prefer_comment_id_over_text():
    from executor import user_schema

    main = user_schema.PublishedMain(
        text="Repeated Main Text",
        comment_id="cid-stored",
        network_confirmed=True,
        local_confirmed=True,
        method="hybrid_dom",
        account_id=10,
        tiktok_username=None,
    )

    assert user_schema._main_submission_keys(main) == {"cid-stored"}


@pytest.mark.unit
def test_main_submission_keys_keep_same_text_distinct_without_comment_id():
    from executor import user_schema

    first = user_schema.PublishedMain(
        text="Repeated Main Text",
        comment_id=None,
        network_confirmed=False,
        local_confirmed=True,
        method="hybrid_dom",
        account_id=10,
        tiktok_username="author_one",
    )
    second = user_schema.PublishedMain(
        text="Repeated Main Text",
        comment_id=None,
        network_confirmed=False,
        local_confirmed=True,
        method="hybrid_dom",
        account_id=11,
        tiktok_username="author_two",
    )

    assert user_schema._main_submission_keys(first) == {
        f"account:10:text:{user_schema._dedup_key('Repeated Main Text')}"
    }
    assert user_schema._main_submission_keys(second) == {
        f"account:11:text:{user_schema._dedup_key('Repeated Main Text')}"
    }
    assert user_schema._main_submission_keys(first).isdisjoint(
        user_schema._main_submission_keys(second)
    )


@pytest.mark.unit
def test_merge_distinct_main_candidates_prefers_cid_for_same_account_text():
    from executor import user_schema

    without_cid = user_schema.PublishedMain(
        text="Repeated Main Text",
        comment_id=None,
        network_confirmed=False,
        local_confirmed=True,
        method="hybrid_dom",
        account_id=10,
        tiktok_username="author_one",
    )
    with_cid = user_schema.PublishedMain(
        text="Repeated Main Text",
        comment_id="cid-10",
        network_confirmed=True,
        local_confirmed=True,
        method="hybrid_dom",
        account_id=10,
        tiktok_username="author_one",
    )
    other_account = user_schema.PublishedMain(
        text="Repeated Main Text",
        comment_id=None,
        network_confirmed=False,
        local_confirmed=True,
        method="hybrid_dom",
        account_id=11,
        tiktok_username="author_two",
    )

    merged = user_schema._merge_distinct_main_candidates([without_cid, with_cid, other_account])

    assert len(merged) == 2
    assert merged[0].comment_id == "cid-10"
    assert merged[1].account_id == 11


@pytest.mark.unit
def test_reply_parent_search_budget_stays_default_for_small_candidate_set():
    from executor import user_schema

    mains = [
        user_schema.PublishedMain(
            text="main one",
            comment_id="cid-1",
            network_confirmed=True,
            local_confirmed=True,
            method="hybrid_dom",
            account_id=10,
            tiktok_username=None,
        )
    ]

    assert user_schema._reply_parent_search_budget(mains) == (
        user_schema._REPLY_MAIN_SEARCH_TIMEOUT_SECONDS,
        user_schema._REPLY_MAIN_SEARCH_PER_TEXT_SECONDS,
        user_schema._REPLY_MAIN_SEARCH_SCROLLS,
    )


@pytest.mark.unit
def test_reply_parent_search_budget_deepens_for_parallel_mains():
    from executor import user_schema

    mains = [
        user_schema.PublishedMain(
            text=f"main {idx}",
            comment_id=f"cid-{idx}",
            network_confirmed=True,
            local_confirmed=True,
            method="hybrid_dom",
            account_id=idx,
            tiktok_username=None,
        )
        for idx in range(8)
    ]

    total_seconds, pass_seconds, scrolls = user_schema._reply_parent_search_budget(mains)

    assert total_seconds == user_schema._REPLY_MAIN_SEARCH_DEEP_TIMEOUT_SECONDS
    assert pass_seconds == user_schema._REPLY_MAIN_SEARCH_DEEP_PER_TEXT_SECONDS
    assert scrolls == user_schema._REPLY_MAIN_SEARCH_DEEP_SCROLLS
    assert total_seconds > user_schema._REPLY_MAIN_SEARCH_TIMEOUT_SECONDS
    assert pass_seconds > user_schema._REPLY_MAIN_SEARCH_PER_TEXT_SECONDS
    assert scrolls > user_schema._REPLY_MAIN_SEARCH_SCROLLS


@pytest.mark.unit
@pytest.mark.asyncio
async def test_confirm_mains_by_observers_filters_to_public_texts(monkeypatch):
    from executor import user_schema

    published = [
        user_schema.PublishedMain(
            text="main one",
            comment_id="cid-1",
            network_confirmed=True,
            local_confirmed=True,
            method="hybrid_dom",
            account_id=10,
            tiktok_username=None,
        ),
        user_schema.PublishedMain(
            text="main two",
            comment_id="cid-2",
            network_confirmed=True,
            local_confirmed=True,
            method="hybrid_dom",
            account_id=10,
            tiktok_username=None,
        ),
    ]

    monkeypatch.setattr(
        user_schema,
        "get_free_account_for_reply",
        lambda *_args, **_kwargs: SimpleNamespace(id=77, proxy=None),
    )

    async def fake_observe(*_args, **_kwargs):
        return ["main two"], None

    monkeypatch.setattr(user_schema, "_observe_main_texts_with_account", fake_observe)

    used_reply_accounts = []
    confirmed = await user_schema._confirm_mains_by_observers(
        SimpleNamespace(),
        "https://www.tiktok.com/@demo/video/1234567890123456789",
        published,
        task_id=1,
        used_reply_accounts=used_reply_accounts,
        pool_owner_user_id=None,
        reason="unit",
    )

    assert [p.text for p in confirmed] == ["main two"]
    assert used_reply_accounts == [77]


@pytest.mark.unit
@pytest.mark.asyncio
async def test_confirm_mains_by_observers_can_continue_until_all_public_texts(monkeypatch):
    from executor import user_schema

    published = [
        user_schema.PublishedMain(
            text="main one",
            comment_id="cid-1",
            network_confirmed=True,
            local_confirmed=True,
            method="hybrid_dom",
            account_id=10,
            tiktok_username=None,
        ),
        user_schema.PublishedMain(
            text="main two",
            comment_id="cid-2",
            network_confirmed=True,
            local_confirmed=True,
            method="hybrid_dom",
            account_id=10,
            tiktok_username=None,
        ),
    ]
    observers = [SimpleNamespace(id=77, proxy=None), SimpleNamespace(id=78, proxy=None)]
    observe_calls = []

    def fake_get_reply(_db, *, exclude_ids, **_kwargs):
        for observer in observers:
            if observer.id not in exclude_ids:
                return observer
        return None

    async def fake_observe(_db, account, _video_url, _mains, **_kwargs):
        observe_calls.append(account.id)
        if account.id == 77:
            return ["main one"], None
        return ["main two"], None

    monkeypatch.setattr(user_schema, "get_free_account_for_reply", fake_get_reply)
    monkeypatch.setattr(user_schema, "_observe_main_texts_with_account", fake_observe)
    monkeypatch.setattr(user_schema.asyncio, "sleep", AsyncMock(return_value=None))

    used_reply_accounts = []
    confirmed = await user_schema._confirm_mains_by_observers(
        SimpleNamespace(),
        "https://www.tiktok.com/@demo/video/1234567890123456789",
        published,
        task_id=1,
        used_reply_accounts=used_reply_accounts,
        pool_owner_user_id=None,
        reason="unit",
        max_observers=2,
        require_all=True,
    )

    assert observe_calls == [77, 78]
    assert used_reply_accounts == [77, 78]
    assert [p.text for p in confirmed] == ["main one", "main two"]


@pytest.mark.unit
@pytest.mark.asyncio
async def test_confirm_mains_by_observers_records_observer_failure_without_page_scope(monkeypatch):
    from executor import user_schema

    published = [
        user_schema.PublishedMain(
            text="main one",
            comment_id="cid-1",
            network_confirmed=True,
            local_confirmed=True,
            method="hybrid_dom",
            account_id=10,
            tiktok_username=None,
        ),
    ]
    observer = SimpleNamespace(id=77, proxy=None)
    traces = []

    monkeypatch.setattr(
        user_schema,
        "get_free_account_for_reply",
        lambda *_args, **_kwargs: observer,
    )

    async def fake_observe(*_args, **_kwargs):
        return [], "observer_comment_list_not_open"

    monkeypatch.setattr(user_schema, "_observe_main_texts_with_account", fake_observe)
    monkeypatch.setattr(
        user_schema,
        "_log_user_trace",
        lambda stage, **kwargs: traces.append((stage, kwargs)),
    )

    outcomes = []
    confirmed = await user_schema._confirm_mains_by_observers(
        SimpleNamespace(),
        "https://www.tiktok.com/@demo/video/1234567890123456789",
        published,
        task_id=1,
        used_reply_accounts=[],
        pool_owner_user_id=None,
        reason="unit",
        max_observers=1,
        aggregate_outcomes=outcomes,
    )

    assert confirmed == []
    assert outcomes == ["observer_comment_list_not_open"]
    assert traces[0][0] == "observer_main_not_confirmed"
    assert traces[0][1]["correlation_id"] == "task_1_observer_unit_a1_acc77"
    assert traces[0][1]["account_id"] == 77
    assert traces[0][1]["observer_outcome"] == "observer_comment_list_not_open"
    assert "page" not in traces[0][1]


@pytest.mark.unit
@pytest.mark.asyncio
async def test_run_fallback_main_attempts_uses_reply_accounts(monkeypatch):
    from executor import user_schema

    account = SimpleNamespace(id=88, proxy=None)
    monkeypatch.setattr(
        user_schema,
        "get_free_account_for_reply",
        lambda *_args, **_kwargs: account,
    )

    async def fake_try_main(_db, got_account, _video_url, text_variants, **_kwargs):
        assert got_account is account
        return [
            user_schema.PublishedMain(
                text=text_variants[0],
                comment_id="cid-main",
                network_confirmed=True,
                local_confirmed=True,
                method="hybrid_dom",
                account_id=account.id,
                tiktok_username=None,
            )
        ], None

    monkeypatch.setattr(user_schema, "_try_main_account", fake_try_main)

    used_reply_accounts = []
    published = await user_schema._run_fallback_main_attempts(
        SimpleNamespace(),
        "https://www.tiktok.com/@demo/video/1234567890123456789",
        ["main one", "main two"],
        task_id=1,
        used_reply_accounts=used_reply_accounts,
        pool_owner_user_id=None,
        max_attempts=3,
        reason="unit",
    )

    assert [p.text for p in published] == ["main one"]
    assert used_reply_accounts == [88]


# ---------------------------------------------------------------------------
# proxy detection
# ---------------------------------------------------------------------------

@pytest.mark.unit
@pytest.mark.parametrize(
    "reason,expected",
    [
        ("net::ERR_TUNNEL_CONNECTION_FAILED", True),
        ("net::ERR_PROXY_CONNECTION_FAILED", True),
        # ERR_HTTP_RESPONSE_CODE_FAILURE — это 4xx/5xx ответ TikTok-сервера через
        # прокси, а не сбой самого прокси. Раньше каскадно вырубал пул.
        ("net::ERR_HTTP_RESPONSE_CODE_FAILURE", False),
        ("Connection refused by remote", True),
        ("TLS handshake timed out", True),
        ("WinError 10060 No connection", True),
        ("captcha_challenge:slider", False),
        ("input_not_found", False),
        ("post_button_stayed_disabled", False),
        ("", False),
    ],
)
def test_looks_like_proxy_failure(reason, expected):
    from executor.user_schema import _looks_like_proxy_failure
    assert _looks_like_proxy_failure(reason) is expected


@pytest.mark.unit
@pytest.mark.parametrize(
    "reason,expected",
    [
        ("goto_failed:net::ERR_HTTP_RESPONSE_CODE_FAILURE", True),
        ("url_stuck_about_blank", True),
        ("chrome-error://chromewebdata/", True),
        ("ambiguous_ui", False),
    ],
)
def test_is_navigation_ambiguous(reason, expected):
    from executor.user_schema import _is_navigation_ambiguous
    assert _is_navigation_ambiguous(reason) is expected


@pytest.mark.unit
def test_register_runtime_failure_does_not_mark_proxy_bad_for_captcha(monkeypatch):
    from executor import user_schema

    calls = {"soft": 0, "proxy": 0}

    def fake_soft_error(*_args, **_kwargs):
        calls["soft"] += 1

    def fake_proxy_bad(*_args, **_kwargs):
        calls["proxy"] += 1

    monkeypatch.setattr(user_schema, "mark_soft_error", fake_soft_error)
    monkeypatch.setattr(user_schema, "mark_proxy_bad", fake_proxy_bad)

    account = SimpleNamespace(id=102, proxy="http://proxy")
    user_schema._register_runtime_failure(SimpleNamespace(), account, "captcha_challenge:slider")
    user_schema._register_runtime_failure(
        SimpleNamespace(),
        account,
        "publish_failed:captcha_challenge:slider",
    )

    assert calls == {"soft": 2, "proxy": 0}


@pytest.mark.unit
def test_register_runtime_failure_cools_down_critical_enrichment_for_six_hours(monkeypatch):
    from executor import user_schema

    calls = []

    def fake_runtime_cooldown(_db, account_id, reason, **kwargs):
        calls.append((account_id, reason, kwargs))

    def fail_soft_error(*_args, **_kwargs):
        raise AssertionError("critical enrichment should use explicit runtime cooldown")

    monkeypatch.setattr(user_schema, "mark_runtime_cooldown", fake_runtime_cooldown)
    monkeypatch.setattr(user_schema, "mark_soft_error", fail_soft_error)

    account = SimpleNamespace(id=103, proxy="http://proxy")
    user_schema._register_runtime_failure(
        SimpleNamespace(),
        account,
        "enrichment_failed:critical_cookies_not_generated",
    )

    assert calls == [
        (
            103,
            "enrichment_failed:critical_cookies_not_generated",
            {
                "cooldown_minutes": 360,
                "category": user_schema.RUNTIME_CATEGORY_COOKIE_ENRICHMENT,
            },
        )
    ]


@pytest.mark.unit
@pytest.mark.asyncio
async def test_ensure_account_login_ready_does_not_retire_on_goto_failed(monkeypatch):
    from executor import user_schema
    from executor.cookie_health import CookieHealth, CookieVerdict

    calls = {"dead": 0, "ambiguous": 0, "proxy": 0}

    async def fake_open_video_and_check(*_args, **_kwargs):
        return CookieHealth(
            CookieVerdict.AMBIGUOUS,
            "goto_failed:net::ERR_HTTP_RESPONSE_CODE_FAILURE",
            None,
            False,
        )

    def fake_dead(*_args, **_kwargs):
        calls["dead"] += 1

    def fake_ambiguous(*_args, **_kwargs):
        calls["ambiguous"] += 1

    def fake_proxy_bad(*_args, **_kwargs):
        calls["proxy"] += 1

    monkeypatch.setattr(user_schema, "_open_video_and_check", fake_open_video_and_check)
    monkeypatch.setattr(user_schema, "register_dead_session", fake_dead)
    monkeypatch.setattr(user_schema, "register_ambiguous_session", fake_ambiguous)
    monkeypatch.setattr(user_schema, "mark_proxy_bad", fake_proxy_bad)

    account = SimpleNamespace(id=101, proxy=SimpleNamespace(id=7))
    ok, deny = await user_schema._ensure_account_login_ready(
        SimpleNamespace(),
        SimpleNamespace(),
        account,
        "https://vt.tiktok.com/abc/",
        timeout_ms=1000,
        correlation_id="test_nav",
    )

    assert ok is False
    assert deny.startswith("login_ambiguous:goto_failed:")
    # ERR_HTTP_RESPONSE_CODE_FAILURE больше не помечает прокси:
    # это HTTP 4xx/5xx ответ TikTok, а не сбой прокси.
    assert calls == {"dead": 0, "ambiguous": 1, "proxy": 0}


@pytest.mark.unit
@pytest.mark.asyncio
async def test_ensure_account_login_ready_captcha_cooldown_does_not_mark_proxy_bad(monkeypatch):
    from executor import user_schema
    from executor.cookie_health import CookieHealth, CookieVerdict

    calls = {"captcha": 0, "ambiguous": 0, "dead": 0, "proxy": 0}

    async def fake_open_video_and_check(*_args, **_kwargs):
        return CookieHealth(
            CookieVerdict.CAPTCHA,
            "captcha_challenge:slider",
            "https://www.tiktok.com/@u/video/1",
            True,
        )

    def fake_captcha(*_args, **_kwargs):
        calls["captcha"] += 1

    def fake_ambiguous(*_args, **_kwargs):
        calls["ambiguous"] += 1

    def fake_dead(*_args, **_kwargs):
        calls["dead"] += 1

    def fake_proxy_bad(*_args, **_kwargs):
        calls["proxy"] += 1

    monkeypatch.setattr(user_schema, "_open_video_and_check", fake_open_video_and_check)
    monkeypatch.setattr(user_schema, "register_captcha_session", fake_captcha)
    monkeypatch.setattr(user_schema, "register_ambiguous_session", fake_ambiguous)
    monkeypatch.setattr(user_schema, "register_dead_session", fake_dead)
    monkeypatch.setattr(user_schema, "mark_proxy_bad", fake_proxy_bad)

    account = SimpleNamespace(id=102, proxy="http://proxy")
    ok, deny = await user_schema._ensure_account_login_ready(
        SimpleNamespace(),
        SimpleNamespace(),
        account,
        "https://vt.tiktok.com/abc/",
        timeout_ms=1000,
        correlation_id="test_captcha",
    )

    assert ok is False
    assert deny == "captcha_challenge:slider"
    assert calls == {"captcha": 1, "ambiguous": 0, "dead": 0, "proxy": 0}


@pytest.mark.unit
@pytest.mark.asyncio
async def test_ensure_account_login_ready_blocked_ui_uses_blocked_register(monkeypatch):
    from executor import user_schema
    from executor.cookie_health import CookieHealth, CookieVerdict

    calls = {"blocked": 0, "ambiguous": 0, "dead": 0}

    async def fake_open_video_and_check(*_args, **_kwargs):
        return CookieHealth(
            CookieVerdict.BLOCKED,
            "blocked_ui:on_video_empty_dom_after_settle",
            "https://www.tiktok.com/@u/video/1",
            True,
        )

    def fake_blocked(*_args, **_kwargs):
        calls["blocked"] += 1

    def fake_ambiguous(*_args, **_kwargs):
        calls["ambiguous"] += 1

    def fake_dead(*_args, **_kwargs):
        calls["dead"] += 1

    monkeypatch.setattr(user_schema, "_open_video_and_check", fake_open_video_and_check)
    monkeypatch.setattr(user_schema, "register_blocked_session", fake_blocked)
    monkeypatch.setattr(user_schema, "register_ambiguous_session", fake_ambiguous)
    monkeypatch.setattr(user_schema, "register_dead_session", fake_dead)

    account = SimpleNamespace(id=103, proxy=None)
    ok, deny = await user_schema._ensure_account_login_ready(
        SimpleNamespace(),
        SimpleNamespace(),
        account,
        "https://vt.tiktok.com/abc/",
        timeout_ms=1000,
        correlation_id="test_blocked",
    )

    assert ok is False
    assert deny == "blocked_ui:on_video_empty_dom_after_settle"
    assert calls == {"blocked": 1, "ambiguous": 0, "dead": 0}


@pytest.mark.unit
@pytest.mark.asyncio
async def test_ensure_account_login_ready_requires_action_probe_for_weak_ready(monkeypatch):
    from executor import user_schema
    from executor.cookie_health import CookieHealth, CookieVerdict

    calls = {"alive": 0, "action_probe": 0}

    async def fake_open_video_and_check(*_args, **_kwargs):
        return CookieHealth(
            CookieVerdict.READY,
            "on_video_no_login_btn:video",
            "https://www.tiktok.com/@u/video/1",
            True,
        )

    async def fake_action_probe(*_args, **_kwargs):
        calls["action_probe"] += 1
        return False, "login_ambiguous:comment_input_not_available_after_cookie_probe"

    monkeypatch.setattr(user_schema, "_open_video_and_check", fake_open_video_and_check)
    monkeypatch.setattr(user_schema, "_confirm_comment_action_ready", fake_action_probe)
    monkeypatch.setattr(
        user_schema,
        "register_alive_session",
        lambda *_args, **_kwargs: calls.__setitem__("alive", calls["alive"] + 1),
    )

    ok, deny = await user_schema._ensure_account_login_ready(
        SimpleNamespace(),
        SimpleNamespace(),
        SimpleNamespace(id=104, proxy=None),
        "https://vt.tiktok.com/abc/",
        timeout_ms=1000,
        correlation_id="test_weak_ready",
    )

    assert ok is False
    assert deny == "login_ambiguous:comment_input_not_available_after_cookie_probe"
    assert calls == {"alive": 0, "action_probe": 1}


@pytest.mark.unit
@pytest.mark.asyncio
async def test_ensure_account_login_ready_accepts_weak_dom_after_valid_http_preflight(monkeypatch):
    from executor import user_schema
    from executor.cookie_health import CookieHealth, CookieVerdict

    calls = {"alive": 0, "action_probe": 0}

    async def fake_open_video_and_check(*_args, **_kwargs):
        return CookieHealth(
            CookieVerdict.AMBIGUOUS,
            "on_video_no_login_btn:video",
            "https://www.tiktok.com/@u/video/1",
            True,
        )

    async def fake_action_probe(*_args, **_kwargs):
        calls["action_probe"] += 1
        return False, "login_ambiguous:comment_input_not_available_after_cookie_probe"

    account = SimpleNamespace(id=105, proxy=None)
    monkeypatch.setattr(user_schema, "_open_video_and_check", fake_open_video_and_check)
    monkeypatch.setattr(user_schema, "_confirm_comment_action_ready", fake_action_probe)
    monkeypatch.setattr(
        user_schema,
        "register_alive_session",
        lambda *_args, **_kwargs: calls.__setitem__("alive", calls["alive"] + 1),
    )
    user_schema._HTTP_PREFLIGHT_VALID_CACHE[user_schema._http_preflight_cache_key(account)] = (
        time.monotonic(),
        "main_pre_browser",
    )

    try:
        ok, deny = await user_schema._ensure_account_login_ready(
            SimpleNamespace(),
            SimpleNamespace(),
            account,
            "https://vt.tiktok.com/abc/",
            timeout_ms=1000,
            correlation_id="test_valid_http_weak_dom",
        )
    finally:
        user_schema._HTTP_PREFLIGHT_VALID_CACHE.pop(user_schema._http_preflight_cache_key(account), None)

    assert ok is True
    assert deny is None
    assert calls == {"alive": 1, "action_probe": 0}


@pytest.mark.unit
def test_bad_cookie_health_results_invalidate_http_preflight_cache(monkeypatch):
    from executor import user_schema

    account = SimpleNamespace(id=106, proxy=None)
    key = user_schema._http_preflight_cache_key(account)
    user_schema._HTTP_PREFLIGHT_VALID_CACHE[key] = (time.monotonic(), "main_pre_browser")

    calls = []
    monkeypatch.setattr(
        user_schema,
        "register_dead_session",
        lambda *_args, **_kwargs: calls.append("dead") or False,
    )

    try:
        user_schema._register_dead_session(SimpleNamespace(), account, "visible_login_ui:test")
        assert key not in user_schema._HTTP_PREFLIGHT_VALID_CACHE
        assert calls == ["dead"]
    finally:
        user_schema._HTTP_PREFLIGHT_VALID_CACHE.pop(key, None)


@pytest.mark.unit
@pytest.mark.asyncio
async def test_action_ready_accepts_strong_login_marker_when_composer_probe_misses(monkeypatch):
    from executor import user_schema
    from executor.cookie_health import CookieHealth, CookieVerdict

    calls = {"alive": 0, "ambiguous": 0}

    async def fake_ensure_media(*_args, **_kwargs):
        return True

    async def fake_open_comments(*_args, **_kwargs):
        return False

    async def fake_probe(_page):
        return CookieHealth(
            CookieVerdict.READY,
            "strong_marker:profile-link:/@valid_user",
            "https://www.tiktok.com/@u/video/1",
            True,
        )

    monkeypatch.setattr(user_schema, "_ensure_page_on_target_media", fake_ensure_media)
    monkeypatch.setattr(user_schema, "_deny_if_visible_logged_out", AsyncMock(return_value=None))
    monkeypatch.setattr(user_schema, "open_comment_section", fake_open_comments)
    monkeypatch.setattr(user_schema, "probe_cookie_health", fake_probe)
    monkeypatch.setattr(
        user_schema,
        "register_alive_session",
        lambda *_args, **_kwargs: calls.__setitem__("alive", calls["alive"] + 1),
    )
    monkeypatch.setattr(
        user_schema,
        "register_ambiguous_session",
        lambda *_args, **_kwargs: calls.__setitem__("ambiguous", calls["ambiguous"] + 1),
    )

    ok, deny = await user_schema._confirm_comment_action_ready(
        SimpleNamespace(),
        SimpleNamespace(url="https://www.tiktok.com/@u/video/1"),
        SimpleNamespace(id=106, proxy=None),
        "https://www.tiktok.com/@u/video/1",
        timeout_ms=1000,
        correlation_id="test_strong_marker_no_composer",
        phase="login_probe_retry",
    )

    assert ok is True
    assert deny is None
    assert calls == {"alive": 1, "ambiguous": 0}


@pytest.mark.unit
@pytest.mark.asyncio
async def test_action_ready_missing_composer_is_inconclusive_pass_without_login_ui(monkeypatch):
    from executor import user_schema
    from executor.cookie_health import CookieHealth, CookieVerdict

    calls = {"alive": 0, "ambiguous": 0}

    async def fake_ensure_media(*_args, **_kwargs):
        return True

    async def fake_open_comments(*_args, **_kwargs):
        return False

    async def fake_probe(_page):
        return CookieHealth(
            CookieVerdict.AMBIGUOUS,
            "on_video_no_login_btn:video",
            "https://www.tiktok.com/@u/video/1",
            True,
        )

    monkeypatch.setattr(user_schema, "_ensure_page_on_target_media", fake_ensure_media)
    monkeypatch.setattr(user_schema, "_deny_if_visible_logged_out", AsyncMock(return_value=None))
    monkeypatch.setattr(user_schema, "open_comment_section", fake_open_comments)
    monkeypatch.setattr(user_schema, "probe_cookie_health", fake_probe)
    monkeypatch.setattr(
        user_schema,
        "register_alive_session",
        lambda *_args, **_kwargs: calls.__setitem__("alive", calls["alive"] + 1),
    )
    monkeypatch.setattr(
        user_schema,
        "register_ambiguous_session",
        lambda *_args, **_kwargs: calls.__setitem__("ambiguous", calls["ambiguous"] + 1),
    )

    ok, deny = await user_schema._confirm_comment_action_ready(
        SimpleNamespace(),
        SimpleNamespace(url="https://www.tiktok.com/@u/video/1"),
        SimpleNamespace(id=107, proxy=None),
        "https://www.tiktok.com/@u/video/1",
        timeout_ms=1000,
        correlation_id="test_missing_composer_inconclusive",
        phase="login_probe",
    )

    assert ok is True
    assert deny is None
    assert calls == {"alive": 1, "ambiguous": 0}


@pytest.mark.unit
@pytest.mark.asyncio
async def test_post_enrichment_probe_blocks_bad_ui(monkeypatch):
    from executor import user_schema
    from executor.cookie_health import CookieHealth, CookieVerdict

    calls = {"blocked": 0, "ambiguous": 0, "dead": 0}

    async def fake_probe(_page):
        return CookieHealth(
            CookieVerdict.BLOCKED,
            "blocked_ui:on_video_empty_dom_after_settle",
            "https://www.tiktok.com/@u/video/1",
            True,
        )

    async def no_sleep(_seconds):
        return None

    def fake_blocked(*_args, **_kwargs):
        calls["blocked"] += 1

    def fake_ambiguous(*_args, **_kwargs):
        calls["ambiguous"] += 1

    def fake_dead(*_args, **_kwargs):
        calls["dead"] += 1

    monkeypatch.setattr(user_schema, "probe_cookie_health", fake_probe)
    monkeypatch.setattr(user_schema.asyncio, "sleep", no_sleep)
    monkeypatch.setattr(user_schema, "dismiss_cookie_banners", AsyncMock())
    monkeypatch.setattr(user_schema, "suppress_keyboard_shortcuts_overlay", AsyncMock())
    monkeypatch.setattr(user_schema, "register_blocked_session", fake_blocked)
    monkeypatch.setattr(user_schema, "register_ambiguous_session", fake_ambiguous)
    monkeypatch.setattr(user_schema, "register_dead_session", fake_dead)

    deny = await user_schema._post_enrichment_deny_reason(
        SimpleNamespace(),
        SimpleNamespace(url="https://www.tiktok.com/@u/video/1"),
        SimpleNamespace(id=104, proxy=None),
        correlation_id="post_enrich",
        phase="reply_post_enrichment",
    )

    assert deny == "blocked_ui:on_video_empty_dom_after_settle"
    assert calls == {"blocked": 1, "ambiguous": 0, "dead": 0}


@pytest.mark.unit
@pytest.mark.asyncio
async def test_post_enrichment_probe_allows_ambiguous_login_marker(monkeypatch):
    from executor import user_schema
    from executor.cookie_health import CookieHealth, CookieVerdict

    calls = {"alive": 0, "ambiguous": 0, "blocked": 0, "dead": 0}

    async def fake_probe(_page):
        return CookieHealth(
            CookieVerdict.AMBIGUOUS,
            "on_video_no_login_btn:video",
            "https://www.tiktok.com/@u/video/1",
            True,
        )

    async def no_sleep(_seconds):
        return None

    monkeypatch.setattr(user_schema, "probe_cookie_health", fake_probe)
    monkeypatch.setattr(user_schema.asyncio, "sleep", no_sleep)
    monkeypatch.setattr(user_schema, "dismiss_cookie_banners", AsyncMock())
    monkeypatch.setattr(user_schema, "suppress_keyboard_shortcuts_overlay", AsyncMock())
    monkeypatch.setattr(user_schema, "register_alive_session", lambda *_args, **_kwargs: calls.__setitem__("alive", calls["alive"] + 1))
    monkeypatch.setattr(user_schema, "register_ambiguous_session", lambda *_args, **_kwargs: calls.__setitem__("ambiguous", calls["ambiguous"] + 1))
    monkeypatch.setattr(user_schema, "register_blocked_session", lambda *_args, **_kwargs: calls.__setitem__("blocked", calls["blocked"] + 1))
    monkeypatch.setattr(user_schema, "register_dead_session", lambda *_args, **_kwargs: calls.__setitem__("dead", calls["dead"] + 1))

    deny = await user_schema._post_enrichment_deny_reason(
        SimpleNamespace(),
        SimpleNamespace(url="https://www.tiktok.com/@u/video/1"),
        SimpleNamespace(id=104, proxy=None),
        correlation_id="post_enrich",
        phase="main_post_enrichment",
    )

    assert deny is None
    assert calls == {"alive": 0, "ambiguous": 0, "blocked": 0, "dead": 0}


@pytest.mark.unit
@pytest.mark.asyncio
async def test_persist_cookies_after_success_uses_live_browser_context(monkeypatch):
    from executor import user_schema

    saved = []

    async def fake_save(context, account, db):
        saved.append((context, account.id, db))
        return True

    monkeypatch.setattr(user_schema, "save_cookies_after_action", fake_save)

    context = object()
    db = object()
    await user_schema._persist_cookies_after_success(
        db,
        SimpleNamespace(context=context),
        SimpleNamespace(id=105),
        scope="main",
        correlation_id="persist_success",
    )

    assert saved == [(context, 105, db)]


@pytest.mark.unit
@pytest.mark.asyncio
async def test_visible_captcha_reason_waits_for_manual_clear(monkeypatch):
    from executor import user_schema

    detections = [(True, "slider"), (False, None), (False, None)]

    async def fake_detect(_page, wait_ms=0):
        return detections.pop(0) if detections else (False, None)

    async def no_sleep(_seconds):
        return None

    monkeypatch.setattr(user_schema, "detect_captcha", fake_detect)
    monkeypatch.setattr(user_schema.asyncio, "sleep", no_sleep)
    monkeypatch.setattr(user_schema, "_manual_captcha_wait_enabled", lambda: True)

    reason = await user_schema._visible_captcha_reason(
        SimpleNamespace(),
        phase="main_pre_publish",
    )

    assert reason is None
    assert detections == []


# ---------------------------------------------------------------------------
# about:blank race protection
# ---------------------------------------------------------------------------

class _Page:
    """Минимальный фейковый Page — отдаёт фиксированный URL."""
    def __init__(self, url):
        self.url = url


class _PageEvolvingUrl:
    """Page, у которой URL сначала about:blank, потом меняется."""
    def __init__(self, urls):
        self._urls = list(urls)
        self._idx = 0
    @property
    def url(self):
        u = self._urls[self._idx]
        if self._idx < len(self._urls) - 1:
            self._idx += 1
        return u


@pytest.mark.unit
@pytest.mark.asyncio
async def test_wait_for_url_settled_returns_quickly_when_already_set():
    from executor.user_schema import _wait_for_url_settled

    page = _Page("https://www.tiktok.com/@u/video/1")
    started = time.monotonic()
    result = await _wait_for_url_settled(page, max_wait_ms=4000)
    elapsed = time.monotonic() - started
    assert "video" in result
    assert elapsed < 0.5  # вернулась с первого тика


@pytest.mark.unit
@pytest.mark.asyncio
async def test_wait_for_url_settled_polls_until_url_appears():
    from executor.user_schema import _wait_for_url_settled

    page = _PageEvolvingUrl([
        "about:blank",
        "about:blank",
        "about:blank",
        "https://www.tiktok.com/@u/video/1",
    ])
    result = await _wait_for_url_settled(page, max_wait_ms=2000)
    assert "video" in result


@pytest.mark.unit
@pytest.mark.asyncio
async def test_wait_for_url_settled_gives_up_after_timeout():
    from executor.user_schema import _wait_for_url_settled

    page = _Page("about:blank")
    result = await _wait_for_url_settled(page, max_wait_ms=300)
    assert result == "about:blank"
