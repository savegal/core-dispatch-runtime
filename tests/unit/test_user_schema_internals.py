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
import threading
import time
from contextlib import contextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest


@pytest.mark.unit
@pytest.mark.asyncio
async def test_safe_close_pooled_page_does_not_close_context_when_pool_declines(monkeypatch):
    from executor import browser_pool, user_schema

    class PooledPage:
        _pool_account_id = 123

        def __init__(self):
            self.closed = False

        def is_closed(self):
            return self.closed

        async def close(self):
            self.closed = True

    class Context:
        def __init__(self):
            self.closed = False

        async def close(self):
            self.closed = True

    async def fake_close_pool_page(_page, *, reason=None):
        return False

    monkeypatch.setattr(browser_pool, "close_pool_page", fake_close_pool_page)
    page = PooledPage()
    context = Context()

    await user_schema._safe_close(context, page, reason="unit_pool_declined")

    assert page.closed is True
    assert context.closed is False


@pytest.mark.unit
def test_like_parent_before_reply_policy_requires_confident_anchor(monkeypatch):
    from executor import user_schema

    monkeypatch.delenv("LIKE_PARENT_BEFORE_REPLY", raising=False)

    assert user_schema._should_like_parent_before_reply(
        user_schema.MainAnchor(
            matched_text="main",
            parent_comment_id="cid1",
            found_by="id",
            anchor_token="anchor1",
        ),
        fast_reply=True,
    ) is True
    assert user_schema._should_like_parent_before_reply(
        user_schema.MainAnchor(
            matched_text="main",
            parent_comment_id=None,
            found_by="text",
            anchor_token="anchor1",
        ),
        fast_reply=True,
    ) is True
    assert user_schema._should_like_parent_before_reply(
        user_schema.MainAnchor(
            matched_text="main",
            parent_comment_id=None,
            found_by="text",
            anchor_token=None,
        ),
        fast_reply=True,
    ) is False
    assert user_schema._should_like_parent_before_reply(
        user_schema.MainAnchor(
            matched_text="main",
            parent_comment_id="cid1",
            found_by="id",
            anchor_token="anchor1",
        )
    ) is True
    assert user_schema._should_like_parent_before_reply(
        user_schema.MainAnchor(
            matched_text="main",
            parent_comment_id=None,
            found_by="text",
            anchor_token="anchor1",
        )
    ) is True
    assert user_schema._should_like_parent_before_reply(
        user_schema.MainAnchor(
            matched_text="main",
            parent_comment_id=None,
            found_by="text",
            anchor_token=None,
        )
    ) is False
    assert user_schema._should_like_parent_before_reply(
        user_schema.MainAnchor(
            matched_text="main",
            parent_comment_id="cid1",
            found_by="text_author",
            anchor_token="anchor1",
            author_fallback_used=True,
        )
    ) is False


@pytest.mark.unit
def test_like_parent_before_reply_policy_can_be_disabled(monkeypatch):
    from executor import user_schema

    monkeypatch.setenv("LIKE_PARENT_BEFORE_REPLY", "0")

    assert user_schema._should_like_parent_before_reply(
        user_schema.MainAnchor(
            matched_text="main",
            parent_comment_id="cid1",
            found_by="id",
            anchor_token="anchor1",
        )
    ) is False


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
def test_action_ready_probe_budget_stays_short():
    from executor import user_schema

    assert user_schema._ACTION_READY_PROBE_SECONDS <= 5.0


@pytest.mark.unit
def test_next_inter_publish_delay_grows_per_publication():
    """Main-варианты идут с короткой стабильной паузой 4-7 секунд."""
    from executor.user_schema import _next_inter_publish_delay

    for _ in range(30):
        assert 4.0 <= _next_inter_publish_delay(0) <= 7.0
        assert 4.0 <= _next_inter_publish_delay(1) <= 7.0
        assert 4.0 <= _next_inter_publish_delay(2) <= 7.0


@pytest.mark.unit
def test_parallel_main_ready_timing_uses_fast_start_settings(monkeypatch):
    from executor import user_schema

    monkeypatch.setattr(user_schema._settings, "PARALLEL_MAIN_READY_IDLE_MIN_SECONDS", 0.4, raising=False)
    monkeypatch.setattr(user_schema._settings, "PARALLEL_MAIN_READY_IDLE_MAX_SECONDS", 1.1, raising=False)
    monkeypatch.setattr(user_schema._settings, "PARALLEL_REPLY_READY_IDLE_MIN_SECONDS", 0.5, raising=False)
    monkeypatch.setattr(user_schema._settings, "PARALLEL_REPLY_READY_IDLE_MAX_SECONDS", 1.2, raising=False)
    monkeypatch.setattr(user_schema._settings, "PARALLEL_MAIN_WARMUP_PROBABILITY", 0.2, raising=False)
    monkeypatch.setattr(user_schema._settings, "PARALLEL_MAIN_WARMUP_TIMEOUT_SECONDS", 2.5, raising=False)

    assert user_schema._main_ready_idle_range("task_1_parallel_main_v1_a1_acc10") == (0.4, 1.1)
    assert user_schema._reply_ready_idle_range("task_1_parallel_reply_v1_a1_acc10") == (0.5, 1.2)
    assert user_schema._reply_ready_idle_range("task_1_engagement_reply_v1_a1_acc10") == (0.5, 1.2)
    assert user_schema._main_warmup_probability("task_1_parallel_main_v1_a1_acc10") == 0.2
    assert user_schema._main_warmup_timeout_seconds("task_1_parallel_main_v1_a1_acc10") == 2.5


@pytest.mark.unit
def test_regular_main_ready_timing_keeps_legacy_values():
    from executor import user_schema

    assert user_schema._main_ready_idle_range("task_1_main_acc10") == (2.5, 5.5)
    assert user_schema._reply_ready_idle_range("task_1_reply_acc10") == (2.5, 5.5)
    assert user_schema._main_warmup_probability("task_1_main_acc10") == 0.5
    assert user_schema._main_warmup_timeout_seconds("task_1_main_acc10") == 6.0


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
        ("dom_error", "post_button_input_not_ready:input_empty", False),
        ("timeout", "reply_box_open_timeout", False),
        ("dom_error", "reply_box_not_open:reply_button_not_found", True),
        ("dom_error", "input_not_found: parent_anchor_not_found", True),
        ("timeout", "publish_deadline_exceeded:typing", False),
        ("timeout", "publish_deadline_exceeded:input_search", False),
        ("dom_error", "input_not_found: page_closed", True),
        ("dom_error", "Page.evaluate: Target page, context or browser has been closed", True),
        ("failed", "post_click_failed:driver_connection_closed", True),
        ("dom_error", "Connection closed while reading from the driver", True),
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
def test_browser_driver_unavailable_detection_is_specific():
    from executor import user_schema

    assert user_schema._looks_like_browser_driver_unavailable(
        "page_create_failed:Failed to create browser context: "
        "Browser.new_context: Connection closed while reading from the driver"
    )
    assert not user_schema._looks_like_browser_driver_unavailable(
        "page_create_failed:proxy timeout while creating browser context"
    )


@pytest.mark.unit
@pytest.mark.parametrize(
    "status,reason,expected",
    [
        ("dom_error", "post_button_not_found", False),
        ("dom_error", "post_button_stayed_disabled", False),
        ("dom_error", "post_button_input_not_ready:input_empty", False),
        ("dom_error", "reply_box_not_open:reply_button_not_found", False),
        ("dom_error", "reply_composer_not_resolved:reply_composer_triple_not_found", False),
        ("dom_error", "reply_post_not_enabled", False),
        ("timeout", "publish_deadline_exceeded:reply_composer_resolve", False),
        ("dom_error", "reply_parent_mismatch:expected=1 actual=2", True),
        ("redirected", "redirected_to_login", True),
        ("dom_error", "captcha_challenge:slider", True),
        ("dom_error", "Page.evaluate: Target page, context or browser has been closed", True),
    ],
)
def test_is_critical_reply_publish_failure_treats_composer_dom_failures_as_recoverable(status, reason, expected):
    from executor.user_schema import _is_critical_reply_publish_failure

    result = SimpleNamespace(status=status, error_reason=reason)
    assert _is_critical_reply_publish_failure(result) is expected


@pytest.mark.unit
@pytest.mark.parametrize(
    "status,reason,expected",
    [
        ("dom_error", "post_button_input_not_ready:input_empty", True),
        ("dom_error", "post_button_input_not_ready:placeholder_leak", True),
        ("timeout", "publish_deadline_exceeded:input_search", True),
        ("timeout", "publish_deadline_exceeded:typing", False),
        ("dom_error", "post_button_stayed_disabled", False),
    ],
)
def test_comment_composer_publish_failure_includes_uncommitted_post_input(status, reason, expected):
    from executor.user_schema import _is_comment_composer_publish_failure

    result = SimpleNamespace(status=status, error_reason=reason)
    assert _is_comment_composer_publish_failure(result) is expected


@pytest.mark.unit
def test_record_main_retry_failure_does_not_penalize_soft_publish_deadline(monkeypatch):
    from executor import user_schema

    calls = []
    attempt_calls = []
    monkeypatch.setattr(
        user_schema,
        "_register_runtime_failure",
        lambda _db, account, reason: calls.append((account.id, reason)),
    )
    monkeypatch.setattr(
        user_schema,
        "record_main_comment_attempt",
        lambda _db, account_id: attempt_calls.append(account_id),
    )

    account = SimpleNamespace(id=777)
    user_schema._record_main_retry_failure(
        SimpleNamespace(),
        account,
        "publish_deadline_exceeded:input_search",
    )

    assert calls == []
    assert attempt_calls == []


@pytest.mark.unit
def test_record_main_retry_failure_still_penalizes_real_publish_failure(monkeypatch):
    from executor import user_schema

    calls = []
    attempt_calls = []
    monkeypatch.setattr(
        user_schema,
        "_register_runtime_failure",
        lambda _db, account, reason: calls.append((account.id, reason)),
    )
    monkeypatch.setattr(
        user_schema,
        "record_main_comment_attempt",
        lambda _db, account_id: attempt_calls.append(account_id),
    )

    account = SimpleNamespace(id=778)
    user_schema._record_main_retry_failure(
        SimpleNamespace(),
        account,
        "post_button_not_found",
    )

    assert calls == [(778, "publish_failed:post_button_not_found")]
    assert attempt_calls == [778]


@pytest.mark.unit
@pytest.mark.parametrize(
    "reason,expected",
    [
        ("publish_deadline_exceeded:input_search", False),
        ("publish_deadline_exceeded:typing", False),
        ("dead_session:visible_login_ui", False),
        ("login_ambiguous:comment_input_not_available_after_cookie_probe", False),
        ("captcha_challenge:slider", False),
        ("post_button_not_found", True),
        ("input_not_found: comment_ui_not_found", False),
        ("post_button_input_not_ready:input_empty", False),
        (None, True),
    ],
)
def test_should_record_publish_runtime_failure(reason, expected):
    from executor import user_schema

    assert user_schema._should_record_publish_runtime_failure(reason) is expected


@pytest.mark.unit
@pytest.mark.parametrize(
    "reason,expected",
    [
        ("post_button_not_found", False),
        ("post_button_stayed_disabled", False),
        ("reply_box_not_open:reply_button_not_found", False),
        ("reply_composer_not_resolved:reply_composer_triple_not_found", False),
        ("reply_post_not_enabled", False),
        ("publish_deadline_exceeded:reply_composer_resolve", False),
        ("reply_parent_mismatch:expected=1 actual=2", True),
        ("captcha_challenge:slider", False),
        (None, True),
    ],
)
def test_should_record_reply_publish_runtime_failure_keeps_dom_failures_off_account_health(reason, expected):
    from executor import user_schema

    assert user_schema._should_record_reply_publish_runtime_failure(reason) is expected


@pytest.mark.unit
def test_user_schema_manual_captcha_wait_disabled_by_default(monkeypatch):
    from executor import user_schema

    monkeypatch.delenv("ALLOW_MANUAL_CAPTCHA_SOLVE", raising=False)
    monkeypatch.setattr(user_schema._settings, "PLAYWRIGHT_HEADLESS", False, raising=False)

    assert user_schema._manual_captcha_wait_enabled() is False


@pytest.mark.unit
def test_user_schema_manual_captcha_wait_requires_explicit_opt_in(monkeypatch):
    from executor import user_schema

    monkeypatch.setenv("ALLOW_MANUAL_CAPTCHA_SOLVE", "1")
    monkeypatch.setattr(user_schema._settings, "PLAYWRIGHT_HEADLESS", False, raising=False)

    assert user_schema._manual_captcha_wait_enabled() is True


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
                        "author": None,
                        "scroll": 2,
                        "position": 4,
                        "scroll_top": 1200,
                        "anchor_token": "anchor-author-fallback",
                        "fallback_reason": "author_missing",
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
    assert anchors[0].matched_author is None
    assert anchors[0].author_fallback_used is True
    assert anchors[0].anchor_token == "anchor-author-fallback"


@pytest.mark.unit
@pytest.mark.asyncio
async def test_find_main_anchors_batch_uses_exact_text_ratio_for_reply_parent(monkeypatch):
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
    assert calls[0]["require_author_match"] is True
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
@pytest.mark.asyncio
async def test_find_main_anchors_batch_rejects_text_only_author_fallback_for_duplicate_texts(monkeypatch):
    from executor import user_schema

    async def _fake_batch(**_kwargs):
        return {}, {
            "author_fallback_candidates": {
                0: [
                    {
                        "raw_text": "same parent text",
                        "comment_id": None,
                        "author": None,
                        "position": 1,
                        "scroll_top": 10,
                        "anchor_token": "duplicate-text-anchor",
                        "fallback_reason": "author_missing",
                    },
                ]
            }
        }

    monkeypatch.setattr(user_schema, "find_comments_in_list_batch", _fake_batch)

    anchors, _info = await user_schema._find_main_anchors_batch(
        page=object(),
        mains=[
            user_schema.PublishedMain(
                text="same parent text",
                comment_id=None,
                network_confirmed=False,
                local_confirmed=True,
                method="engagement_only_anchor",
                account_id=1,
                tiktok_username="first_user",
            ),
            user_schema.PublishedMain(
                text="same parent text",
                comment_id=None,
                network_confirmed=False,
                local_confirmed=True,
                method="engagement_only_anchor",
                account_id=2,
                tiktok_username="second_user",
            ),
        ],
        require_author_match=True,
        allow_author_fallback=True,
    )

    assert anchors == []


@pytest.mark.unit
@pytest.mark.asyncio
async def test_find_main_anchors_batch_rejects_author_mismatch_fallback(monkeypatch):
    from executor import user_schema

    async def _fake_batch(**_kwargs):
        return {}, {
            "author_fallback_candidates": {
                0: [
                    {
                        "raw_text": "target comment",
                        "author": "wrong_user",
                        "position": 1,
                        "scroll_top": 10,
                        "fallback_reason": "author_mismatch",
                    },
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
def test_reply_composer_cleared_without_network_counts_as_submitted_unconfirmed():
    from executor.publish_action import PublishResult
    from executor.user_schema import _reply_publish_submitted_unconfirmed

    result = PublishResult(
        status="failed",
        error_reason="reply_direct_composer_cleared_after_submit",
        post_was_enabled_at_click=True,
        network_request_attempted=False,
    )

    assert _reply_publish_submitted_unconfirmed(result) is True


@pytest.mark.unit
def test_main_composer_cleared_without_network_counts_as_submitted_unconfirmed():
    from executor.publish_action import PublishResult
    from executor.user_schema import _main_publish_submitted_unconfirmed

    result = PublishResult(
        status="failed",
        error_reason="main_direct_composer_cleared_after_submit",
        post_was_enabled_at_click=True,
        network_request_attempted=False,
    )

    assert _main_publish_submitted_unconfirmed("task_1_main_acc44_v1", result) is True


@pytest.mark.unit
def test_dom_publish_requires_proven_clean_composer_for_same_page_reuse():
    from executor import user_schema
    from executor.publish_action import PublishResult

    http_result = PublishResult(
        status="success",
        comment_id="cid-http",
        dom_strategy="http_user_schema",
    )
    dom_clean = PublishResult(
        status="success",
        comment_id="cid-dom",
        dom_strategy="main-dom",
        composer_clean_after_submit=True,
    )
    dom_unknown = PublishResult(
        status="success",
        comment_id="cid-dom",
        dom_strategy="main-dom",
        composer_clean_after_submit=None,
    )

    assert user_schema._publish_result_allows_same_page_reuse(http_result) is True
    assert user_schema._publish_result_allows_same_page_reuse(dom_clean) is True
    assert user_schema._publish_result_allows_same_page_reuse(dom_unknown) is False


@pytest.mark.unit
def test_user_schema_result_persists_main_tiktok_username_in_payload():
    from executor import user_schema

    result = user_schema.UserSchemaResult(expected_main_publications=1)
    result.published_mains.append(
        user_schema.PublishedMain(
            text="main text",
            comment_id="main-cid",
            network_confirmed=True,
            local_confirmed=True,
            method="hybrid_dom",
            account_id=101,
            tiktok_username="main_author",
        )
    )

    payload = result.to_dict()
    variants = payload["engagement"]["main"]["variants_published"]

    assert variants[0]["tiktok_username"] == "main_author"


@pytest.mark.unit
def test_reply_http_parent_unconfirmed_counts_as_submitted_unconfirmed():
    from executor.publish_action import PublishResult
    from executor.user_schema import _reply_publish_submitted_unconfirmed

    result = PublishResult(
        status="failed",
        comment_id="reply-cid",
        error_reason="reply_parent_unconfirmed_after_submit",
        post_was_enabled_at_click=True,
        network_request_attempted=True,
        network_captured=True,
    )

    assert _reply_publish_submitted_unconfirmed(result) is True


@pytest.mark.unit
@pytest.mark.asyncio
async def test_reply_http_helper_parent_unconfirmed_returns_submitted_result(monkeypatch):
    from executor import engagement_engine, user_schema

    class FakeHttpClient:
        async def reply_comment(self, **kwargs):
            assert kwargs["parent_comment_id"] == "7641761192486194454"
            return (
                True,
                "7641761192486194455",
                None,
                None,
                {
                    "parent_confirmed": False,
                    "parent_confirmation_level": "network_missing_parent",
                },
            )

    recorded = []
    monkeypatch.setattr(
        engagement_engine,
        "_record_http_account_result",
        lambda account_id, status: recorded.append((account_id, status)),
    )

    session = user_schema._UserSchemaHttpSession(
        scope=user_schema.REPLY_SCOPE,
        account_id=55,
        client=FakeHttpClient(),
        cookies={"sessionid": "cookie"},
        aweme_id="7641761192486194454",
        video_url="https://www.tiktok.com/@demo/video/7641761192486194454",
    )

    result = await user_schema._publish_reply_http_first_if_eligible(
        session,
        "reply text",
        parent_comment_id="7641761192486194454",
        correlation_id="task_http_reply_helper",
    )

    assert result is not None
    assert result.status == "failed"
    assert result.comment_id == "7641761192486194455"
    assert result.error_reason == "reply_parent_unconfirmed_after_submit"
    assert result.network_request_attempted is True
    assert result.post_was_enabled_at_click is True
    assert result.dom_strategy == "http_user_schema:network_missing_parent"
    assert user_schema._reply_publish_submitted_unconfirmed(result) is True
    assert session.disabled_reason is None
    assert recorded == [(55, "success")]


@pytest.mark.unit
@pytest.mark.asyncio
async def test_reply_http_helper_skips_non_tiktok_parent_id():
    from executor import user_schema

    class FakeHttpClient:
        async def reply_comment(self, **_kwargs):
            raise AssertionError("HTTP client must not run for non-numeric DOM ids")

    session = user_schema._UserSchemaHttpSession(
        scope=user_schema.REPLY_SCOPE,
        account_id=55,
        client=FakeHttpClient(),
        cookies={"sessionid": "cookie"},
        aweme_id="7641761192486194454",
        video_url="https://www.tiktok.com/@demo/video/7641761192486194454",
    )

    result = await user_schema._publish_reply_http_first_if_eligible(
        session,
        "reply text",
        parent_comment_id="cid-main",
        correlation_id="task_http_reply_helper",
    )

    assert result is None
    assert session.disabled_reason is None


@pytest.mark.unit
@pytest.mark.asyncio
async def test_main_http_helper_returns_terminal_result_after_attempted_missing_confirmation(monkeypatch):
    from executor import engagement_engine, user_schema
    from executor.http.retry_policy import HttpErrorType

    class FakeHttpClient:
        last_request_meta = {"request_attempted": True, "attempt_count": 1}

        async def publish_main_comment(self, **kwargs):
            assert kwargs["aweme_id"] == "7641761192486194454"
            return (
                False,
                None,
                "Response OK but missing confirmation data (comment_id)",
                HttpErrorType.API_ERROR,
            )

    failures = []
    monkeypatch.setattr(
        engagement_engine,
        "_record_http_account_failure_result",
        lambda account_id, error_type: failures.append((account_id, error_type)),
    )

    session = user_schema._UserSchemaHttpSession(
        scope=user_schema.MAIN_SCOPE,
        account_id=56,
        client=FakeHttpClient(),
        cookies={"sessionid": "cookie"},
        aweme_id="7641761192486194454",
        video_url="https://www.tiktok.com/@demo/video/7641761192486194454",
    )

    result = await user_schema._publish_main_http_first_if_eligible(
        session,
        "main text",
        correlation_id="task_http_main_helper",
    )

    assert result is not None
    assert result.status == "failed"
    assert result.error_reason == "stealth_reject_empty_body"
    assert result.network_request_attempted is True
    assert result.post_was_enabled_at_click is True
    assert result.dom_strategy == "http_user_schema"
    assert session.disabled_reason == "Response OK but missing confirmation data (comment_id)"
    assert failures == [(56, HttpErrorType.API_ERROR)]


@pytest.mark.unit
@pytest.mark.asyncio
async def test_main_http_helper_allows_dom_fallback_for_pre_submit_signer_block(monkeypatch):
    from executor import engagement_engine, user_schema
    from executor.http.retry_policy import HttpErrorType

    class FakeHttpClient:
        last_request_meta = {"request_attempted": False}

        async def publish_main_comment(self, **_kwargs):
            return (
                False,
                None,
                "HTTP publish blocked: runtime signer did not produce a usable signature",
                HttpErrorType.API_ERROR,
            )

    failures = []
    monkeypatch.setattr(
        engagement_engine,
        "_record_http_account_failure_result",
        lambda account_id, error_type: failures.append((account_id, error_type)),
    )

    session = user_schema._UserSchemaHttpSession(
        scope=user_schema.MAIN_SCOPE,
        account_id=57,
        client=FakeHttpClient(),
        cookies={"sessionid": "cookie"},
        aweme_id="7641761192486194454",
        video_url="https://www.tiktok.com/@demo/video/7641761192486194454",
    )

    result = await user_schema._publish_main_http_first_if_eligible(
        session,
        "main text",
        correlation_id="task_http_main_helper",
    )

    assert result is None
    assert session.disabled_reason == "HTTP publish blocked: runtime signer did not produce a usable signature"
    assert failures == []


@pytest.mark.unit
@pytest.mark.asyncio
async def test_reply_http_helper_allows_dom_fallback_for_pre_submit_signer_block(monkeypatch):
    from executor import engagement_engine, user_schema
    from executor.http.retry_policy import HttpErrorType

    class FakeHttpClient:
        last_request_meta = {"request_attempted": False, "attempt_count": 0}

        async def reply_comment(self, **_kwargs):
            return (
                False,
                None,
                "HTTP reply blocked: runtime signer did not produce a usable signature",
                HttpErrorType.API_ERROR,
                {"request_meta": dict(self.last_request_meta)},
            )

    failures = []
    monkeypatch.setattr(
        engagement_engine,
        "_record_http_account_failure_result",
        lambda account_id, error_type: failures.append((account_id, error_type)),
    )

    session = user_schema._UserSchemaHttpSession(
        scope=user_schema.REPLY_SCOPE,
        account_id=59,
        client=FakeHttpClient(),
        cookies={"sessionid": "cookie"},
        aweme_id="7641761192486194454",
        video_url="https://www.tiktok.com/@demo/video/7641761192486194454",
    )

    result = await user_schema._publish_reply_http_first_if_eligible(
        session,
        "reply text",
        parent_comment_id="7641761192486194454",
        correlation_id="task_http_reply_pre_submit",
    )

    assert result is None
    assert session.disabled_reason == "HTTP reply blocked: runtime signer did not produce a usable signature"
    assert failures == []


@pytest.mark.unit
@pytest.mark.asyncio
async def test_main_http_helper_uses_request_attempted_meta_over_error_text(monkeypatch):
    from executor import engagement_engine, user_schema
    from executor.http.retry_policy import HttpErrorType

    class FakeHttpClient:
        last_request_meta = {"request_attempted": True, "attempt_count": 1}

        async def publish_main_comment(self, **_kwargs):
            return (
                False,
                None,
                "HTTP publish blocked: runtime signer did not produce a usable signature",
                HttpErrorType.API_ERROR,
            )

    monkeypatch.setattr(
        engagement_engine,
        "_record_http_account_failure_result",
        lambda *_args, **_kwargs: None,
    )

    session = user_schema._UserSchemaHttpSession(
        scope=user_schema.MAIN_SCOPE,
        account_id=57,
        client=FakeHttpClient(),
        cookies={"sessionid": "cookie"},
        aweme_id="7641761192486194454",
        video_url="https://www.tiktok.com/@demo/video/7641761192486194454",
    )

    result = await user_schema._publish_main_http_first_if_eligible(
        session,
        "main text",
        correlation_id="task_http_main_helper",
    )

    assert result is not None
    assert result.network_request_attempted is True
    assert result.error_reason == "http_main_failed:HTTP publish blocked: runtime signer did not produce a usable signature"


@pytest.mark.unit
@pytest.mark.asyncio
async def test_reply_http_helper_attempted_missing_confirmation_counts_without_dom_fallback(monkeypatch):
    from executor import engagement_engine, user_schema
    from executor.http.retry_policy import HttpErrorType

    class FakeHttpClient:
        last_request_meta = {"request_attempted": True, "attempt_count": 1}

        async def reply_comment(self, **kwargs):
            assert kwargs["parent_comment_id"] == "7641761192486194454"
            return (
                False,
                None,
                "Response OK but missing comment_id",
                HttpErrorType.API_ERROR,
                {"parent_confirmed": False},
            )

    failures = []
    monkeypatch.setattr(
        engagement_engine,
        "_record_http_account_failure_result",
        lambda account_id, error_type: failures.append((account_id, error_type)),
    )

    session = user_schema._UserSchemaHttpSession(
        scope=user_schema.REPLY_SCOPE,
        account_id=58,
        client=FakeHttpClient(),
        cookies={"sessionid": "cookie"},
        aweme_id="7641761192486194454",
        video_url="https://www.tiktok.com/@demo/video/7641761192486194454",
    )

    result = await user_schema._publish_reply_http_first_if_eligible(
        session,
        "reply text",
        parent_comment_id="7641761192486194454",
        correlation_id="task_http_reply_helper",
    )

    assert result is not None
    assert result.status == "failed"
    assert result.error_reason == "stealth_reject_empty_body"
    assert result.network_request_attempted is True
    assert result.post_was_enabled_at_click is True
    assert user_schema._reply_publish_submitted_unconfirmed(result) is True
    assert session.disabled_reason == "Response OK but missing comment_id"
    assert failures == [(58, HttpErrorType.API_ERROR)]


@pytest.mark.unit
def test_looks_like_tiktok_comment_id_rejects_dom_wrapper_ids():
    from executor.user_schema import _looks_like_tiktok_comment_id

    assert _looks_like_tiktok_comment_id("7641761192486194454") is True
    assert _looks_like_tiktok_comment_id('wrapper-"bad"') is False
    assert _looks_like_tiktok_comment_id("comment-7641761192486194454") is False
    assert _looks_like_tiktok_comment_id("") is False


@pytest.mark.unit
def test_user_schema_parent_id_lookup_does_not_trust_data_id():
    import inspect
    from executor import user_schema

    find_source = inspect.getsource(user_schema._find_main_anchor)
    verify_source = inspect.getsource(user_schema._verify_reply_visible_near_parent)

    assert 'f\'[data-id="{escaped}"]\'' not in find_source
    assert "root.getAttribute('data-id')" not in verify_source
    assert '[id="comment-{escaped}"]' in find_source
    assert "replyMatches(text, replyNeedle)" in verify_source
    assert "allowExpand" in verify_source
    assert "reply_expander_clicked" in verify_source
    assert "label === 'reply'" in verify_source


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
        "list_source": {"items": 15, "strong_items": 15},
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
async def test_parallel_reply_open_comments_short_circuits_existing_surface(monkeypatch):
    from executor import user_schema

    calls = {"open": 0, "goto": 0, "waits": []}
    target_url = "https://www.tiktok.com/@u/video/1234567890123456789"

    class _Page:
        url = target_url

        async def goto(self, *_args, **_kwargs):
            calls["goto"] += 1

    async def fake_wait(_page, **kwargs):
        calls["waits"].append(kwargs.get("timeout_seconds"))
        return True

    async def fail_open(*_args, **_kwargs):
        calls["open"] += 1
        raise AssertionError("ready reply surface should not be reopened")

    monkeypatch.setattr(user_schema, "_wait_for_searchable_comment_surface", fake_wait)
    monkeypatch.setattr(user_schema, "open_comment_section", fail_open)

    ok = await user_schema._open_comment_list_with_safe_retry(
        _Page(),
        target_url,
        target_url,
        timeout_ms=30000,
        correlation_id="task_1015_parallel_reply_v1_a1_acc1",
        phase="reply_open_comments",
        account_id=1,
        variant_index=1,
    )

    assert ok is True
    assert calls["open"] == 0
    assert calls["goto"] == 0
    assert calls["waits"] == [pytest.approx(1.1)]


@pytest.mark.unit
@pytest.mark.asyncio
async def test_parallel_reply_open_comments_has_hard_outer_deadline(monkeypatch):
    from executor import user_schema

    target_url = "https://www.tiktok.com/@u/video/1234567890123456789"
    captured = {"timeout": None, "traces": []}

    async def fake_fast_open(*args, **kwargs):
        return True

    async def fake_hard_timeout(awaitable, *, timeout):
        captured["timeout"] = timeout
        awaitable.close()
        raise asyncio.TimeoutError

    def fake_trace(stage, **kwargs):
        captured["traces"].append((stage, kwargs))

    monkeypatch.setattr(user_schema, "_open_comment_list_fast_for_parallel_reply", fake_fast_open)
    monkeypatch.setattr(user_schema, "_await_reply_hard_timeout", fake_hard_timeout)
    monkeypatch.setattr(user_schema, "_log_user_trace", fake_trace)

    ok = await user_schema._open_comment_list_with_safe_retry(
        SimpleNamespace(url=target_url),
        target_url,
        target_url,
        timeout_ms=30000,
        correlation_id="task_1015_parallel_reply_v1_a1_acc1",
        phase="reply_open_comments",
        account_id=1,
        variant_index=1,
    )

    assert ok is False
    assert captured["timeout"] == pytest.approx(9.5)
    assert captured["traces"][0][0] == "comment_list_fast_open_hard_timeout"


@pytest.mark.unit
@pytest.mark.asyncio
async def test_parallel_reply_open_comments_uses_short_reclick_without_reload(monkeypatch):
    from executor import user_schema

    calls = {"open_waits": [], "goto": 0}
    target_url = "https://www.tiktok.com/@u/video/1234567890123456789"

    class _Page:
        url = target_url

        async def goto(self, *_args, **_kwargs):
            calls["goto"] += 1

    async def fake_wait(*_args, **_kwargs):
        return False

    async def fake_open(*_args, **kwargs):
        calls["open_waits"].append(kwargs.get("max_wait"))
        return len(calls["open_waits"]) == 2

    monkeypatch.setattr(user_schema, "_wait_for_searchable_comment_surface", fake_wait)
    monkeypatch.setattr(user_schema, "open_comment_section", fake_open)

    ok = await user_schema._open_comment_list_with_safe_retry(
        _Page(),
        target_url,
        target_url,
        timeout_ms=30000,
        correlation_id="task_1015_parallel_reply_v1_a1_acc1",
        phase="reply_open_comments",
        account_id=1,
        variant_index=1,
    )

    assert ok is True
    assert calls["goto"] == 0
    assert len(calls["open_waits"]) == 2
    assert calls["open_waits"][0] <= 3.2
    assert calls["open_waits"][1] <= 1.8


@pytest.mark.unit
@pytest.mark.asyncio
async def test_parallel_reply_open_comments_skips_reload_without_empty_or_redirect(monkeypatch):
    from executor import user_schema

    calls = {"open": 0, "goto": 0}
    target_url = "https://www.tiktok.com/@u/video/1234567890123456789"

    class _Page:
        url = target_url

        async def goto(self, *_args, **_kwargs):
            calls["goto"] += 1

    async def fake_wait(*_args, **_kwargs):
        return False

    async def fake_open(*_args, **_kwargs):
        calls["open"] += 1
        return False

    async def fake_surface(_page):
        return {
            "input_visible": False,
            "comment_input_visible": False,
            "list_visible": False,
            "comment_items_visible": 0,
            "list_source": None,
        }

    monkeypatch.setattr(user_schema, "_wait_for_searchable_comment_surface", fake_wait)
    monkeypatch.setattr(user_schema, "open_comment_section", fake_open)
    monkeypatch.setattr(user_schema, "get_comment_surface_state", fake_surface)

    ok = await user_schema._open_comment_list_with_safe_retry(
        _Page(),
        target_url,
        target_url,
        timeout_ms=30000,
        correlation_id="task_1015_parallel_reply_v1_a1_acc1",
        phase="reply_open_comments",
        account_id=1,
        variant_index=1,
    )

    assert ok is False
    assert calls["open"] == 2
    assert calls["goto"] == 0


@pytest.mark.unit
@pytest.mark.asyncio
async def test_parallel_reply_open_comments_reloads_only_after_empty_surface(monkeypatch):
    from executor import user_schema

    calls = {"open_waits": [], "goto": 0, "sleep": []}
    target_url = "https://www.tiktok.com/@u/video/1234567890123456789"

    class _Page:
        url = target_url

        async def goto(self, *_args, **_kwargs):
            calls["goto"] += 1

    async def fake_wait(*_args, **_kwargs):
        return False

    async def fake_open(*_args, **kwargs):
        calls["open_waits"].append(kwargs.get("max_wait"))
        return len(calls["open_waits"]) == 3

    async def fake_surface(_page):
        return {
            "input_visible": False,
            "comment_input_visible": True,
            "list_visible": True,
            "comment_items_visible": 0,
            "list_source": {"items": 0, "strong_items": 0},
        }

    async def fake_sleep(seconds):
        calls["sleep"].append(seconds)

    monkeypatch.setattr(user_schema, "_wait_for_searchable_comment_surface", fake_wait)
    monkeypatch.setattr(user_schema, "open_comment_section", fake_open)
    monkeypatch.setattr(user_schema, "get_comment_surface_state", fake_surface)
    monkeypatch.setattr(user_schema.asyncio, "sleep", fake_sleep)

    ok = await user_schema._open_comment_list_with_safe_retry(
        _Page(),
        target_url,
        target_url,
        timeout_ms=30000,
        correlation_id="task_1015_parallel_reply_v1_a1_acc1",
        phase="reply_open_comments",
        account_id=1,
        variant_index=1,
    )

    assert ok is True
    assert calls["goto"] == 1
    assert len(calls["open_waits"]) == 3
    assert calls["open_waits"][0] <= 3.2
    assert calls["open_waits"][1] <= 1.8
    assert calls["open_waits"][2] <= 2.4
    assert calls["sleep"] and max(calls["sleep"]) <= 0.6


@pytest.mark.unit
@pytest.mark.asyncio
async def test_non_parallel_open_comments_keeps_legacy_retry_budget(monkeypatch):
    from executor import user_schema

    calls = {"open_waits": [], "goto": 0, "sleep": []}
    target_url = "https://www.tiktok.com/@u/video/1234567890123456789"

    class _Page:
        url = target_url

        async def goto(self, *_args, **_kwargs):
            calls["goto"] += 1

    async def fake_open(*_args, **kwargs):
        calls["open_waits"].append(kwargs.get("max_wait"))
        return len(calls["open_waits"]) == 2

    async def fake_ensure(*_args, **_kwargs):
        return True

    async def fake_sleep(seconds):
        calls["sleep"].append(seconds)

    monkeypatch.setattr(user_schema, "open_comment_section", fake_open)
    monkeypatch.setattr(user_schema, "_ensure_page_on_target_media", fake_ensure)
    monkeypatch.setattr(user_schema.asyncio, "sleep", fake_sleep)

    ok = await user_schema._open_comment_list_with_safe_retry(
        _Page(),
        target_url,
        target_url,
        timeout_ms=30000,
        correlation_id="observer_task_1015_acc1",
        phase="observer_open_comments",
        account_id=1,
    )

    assert ok is True
    assert calls["open_waits"] == [12.0, 12.0]
    assert calls["goto"] == 1
    assert calls["sleep"] == [1.8]


@pytest.mark.unit
def test_parallel_reply_fast_open_wraps_open_section_in_outer_timeout():
    from executor import user_schema
    import inspect

    source = inspect.getsource(user_schema._open_comment_list_fast_for_parallel_reply)

    assert "opened = await asyncio.wait_for(" in source
    assert "timeout=max(0.5, min(_remaining(), budget + 0.4))" in source


@pytest.mark.unit
@pytest.mark.asyncio
async def test_input_retry_recovery_keeps_ready_surface_without_reopen_or_reload(monkeypatch):
    from executor import user_schema

    calls = {"open": 0, "reload": 0, "suppress": 0, "dismiss": 0}

    class _Page:
        async def reload(self, *_args, **_kwargs):
            calls["reload"] += 1
            raise AssertionError("soft input recovery must not reload a ready comment surface")

    async def fake_surface(_page):
        return {
            "input_visible": True,
            "comment_input_visible": True,
            "list_visible": True,
            "comment_items_visible": 8,
        }

    async def fail_open(*_args, **_kwargs):
        calls["open"] += 1
        raise AssertionError("ready comment surface should not be reopened")

    async def fake_suppress(_page):
        calls["suppress"] += 1
        return 0

    async def fake_dismiss(_page):
        calls["dismiss"] += 1
        return 0

    monkeypatch.setattr(user_schema, "get_comment_surface_state", fake_surface)
    monkeypatch.setattr(user_schema, "open_comment_section", fail_open)
    monkeypatch.setattr(user_schema, "suppress_keyboard_shortcuts_overlay", fake_suppress)
    monkeypatch.setattr(user_schema, "dismiss_cookie_banners", fake_dismiss)

    ready = await user_schema._recover_page_before_input_retry(
        _Page(),
        correlation_id="recover_ready_surface",
    )

    assert ready is True
    assert calls["open"] == 0
    assert calls["reload"] == 0
    assert calls["suppress"] == 1
    assert calls["dismiss"] == 1


@pytest.mark.unit
@pytest.mark.asyncio
async def test_input_retry_recovery_cleans_main_via_exact_reset(monkeypatch):
    from executor import user_schema

    calls = {"reset": []}

    class _Page:
        url = "https://www.tiktok.com/@demo/video/1234567890123456789"

    async def fake_surface(_page):
        return {
            "comment_input_visible": True,
            "list_visible": True,
            "comment_items_visible": 4,
        }

    async def fake_reset(_page, **kwargs):
        calls["reset"].append(kwargs)
        return True

    async def fail_open(*_args, **_kwargs):
        raise AssertionError("ready comment surface should not be reopened")

    monkeypatch.setattr(user_schema, "get_comment_surface_state", fake_surface)
    monkeypatch.setattr(user_schema, "reset_main_comment_composer_for_next_variant", fake_reset)
    monkeypatch.setattr(user_schema, "open_comment_section", fail_open)
    monkeypatch.setattr(user_schema, "suppress_keyboard_shortcuts_overlay", AsyncMock(return_value=0))
    monkeypatch.setattr(user_schema, "dismiss_cookie_banners", AsyncMock(return_value=0))

    ready = await user_schema._recover_page_before_input_retry(
        _Page(),
        correlation_id="recover_clean_surface",
        expected_video_url="https://www.tiktok.com/@demo/video/1234567890123456789",
        require_clean_composer=True,
    )

    assert ready is True
    assert calls["reset"] == [
        {
            "expected_video_url": "https://www.tiktok.com/@demo/video/1234567890123456789",
            "correlation_id": "recover_clean_surface",
            "timeout_seconds": 2.2,
            "comment_surface_ready": True,
        }
    ]


@pytest.mark.unit
def test_input_retry_recovery_no_longer_uses_global_main_clear():
    from executor import user_schema

    source = inspect.getsource(user_schema._recover_page_before_input_retry)

    assert "reset_main_comment_composer_for_next_variant" in source
    assert "_clear_main_comment_composer_best_effort" not in source


@pytest.mark.unit
def test_main_local_confirmation_requires_author_in_comment_finder():
    from executor import comment_outcome

    source = inspect.getsource(comment_outcome.find_comment_in_list)

    assert "require_author_match: bool = False" in source
    assert "author_missing_skip" in source
    assert "if exp_author_norm and require_author_match and not row_author_norm:" in source


@pytest.mark.unit
def test_reply_inflight_lock_survives_until_publish_classification_callbacks():
    from executor import user_schema

    source = inspect.getsource(user_schema._reply_with_variant_on_all_mains)

    assert "publish_ready_for_classification = False" in source
    assert "and not publish_ready_for_classification" in source
    callbacks_block = source.split("if soft_submitted and on_soft_submitted_outcome is not None:", 1)[1]
    release_index = callbacks_block.index("active_inflight_reply_keys.discard(reply_attempt_key)")
    trace_index = callbacks_block.index('_log_user_trace(\n                    "reply_publish_result"')
    assert release_index < trace_index


@pytest.mark.unit
@pytest.mark.asyncio
async def test_reply_reset_does_not_press_ctrl_a_without_verified_focus():
    from executor import user_schema

    class Keyboard:
        def __init__(self):
            self.presses = []

        async def press(self, key):
            self.presses.append(key)

    class Page:
        url = "https://www.tiktok.com/@demo/video/1234567890123456789"

        def __init__(self):
            self.keyboard = Keyboard()
            self.evaluates = 0

        async def evaluate(self, *_args, **_kwargs):
            self.evaluates += 1
            return {
                "dirtyTextCount": 1,
                "dirtyPreview": ["stale reply"],
                "targetVisible": True,
                "focusOk": False,
            }

    page = Page()

    ok = await user_schema._reset_reply_page_after_confirmed_submit(
        page,
        correlation_id="reply_reset_focus_guard",
        expected_video_url="https://www.tiktok.com/@demo/video/1234567890123456789",
    )

    assert ok is False
    assert page.keyboard.presses == []
    assert page.evaluates == 1


@pytest.mark.unit
@pytest.mark.asyncio
async def test_publish_all_variants_retries_input_not_found_on_same_account(monkeypatch):
    from executor import user_schema
    from executor.publish_action import PublishResult

    calls = []
    recovered = {"count": 0}

    async def fake_publish(_page, text, **kwargs):
        calls.append((text, kwargs.get("correlation_id"), kwargs.get("comments_already_open")))
        if len(calls) == 1:
            return PublishResult(status="dom_error", error_reason="input_not_found: timeout")
        return PublishResult(
            status="success",
            comment_id="cid-1",
            network_captured=True,
            dom_strategy="data-e2e",
        )

    async def fake_recover(
        _page,
        *,
        correlation_id,
        expected_video_url=None,
        require_clean_composer=False,
    ):
        recovered["count"] += 1
        assert correlation_id == "task_1_v1"
        assert expected_video_url is None
        assert require_clean_composer is True
        return True

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
    assert calls == [
        ("hello", "task_1_v1", None),
        ("hello", "task_1_v1_input_retry", True),
    ]
    assert failure_reason is None
    assert len(published) == 1
    assert published[0].text == "hello"
    assert published[0].comment_id == "cid-1"


@pytest.mark.unit
@pytest.mark.asyncio
async def test_publish_all_variants_stops_after_composer_retry_failure(monkeypatch):
    from executor import user_schema
    from executor.publish_action import PublishResult

    calls = []

    async def fake_publish(_page, text, **kwargs):
        calls.append((text, kwargs.get("correlation_id"), kwargs.get("comments_already_open")))
        return PublishResult(status="timeout", error_reason="publish_deadline_exceeded:open_comments")

    async def fake_recover(
        _page,
        *,
        correlation_id,
        expected_video_url=None,
        require_clean_composer=False,
    ):
        assert correlation_id == "task_1_v1"
        assert expected_video_url is None
        assert require_clean_composer is True
        return False

    async def fake_username(_page):
        return "main_user"

    monkeypatch.setattr(user_schema, "publish_comment_hybrid", fake_publish)
    monkeypatch.setattr(user_schema, "_recover_page_before_input_retry", fake_recover)
    monkeypatch.setattr(user_schema, "extract_tiktok_username", fake_username)
    monkeypatch.setattr(user_schema, "_deny_if_visible_logged_out", AsyncMock(return_value=None))

    published, failure_reason = await user_schema._publish_all_variants_on_account(
        SimpleNamespace(),
        SimpleNamespace(),
        SimpleNamespace(id=10),
        ["hello", "world"],
        correlation_id="task_1",
    )

    assert published == []
    assert failure_reason == "publish_deadline_exceeded:open_comments"
    assert calls == [
        ("hello", "task_1_v1", None),
    ]


@pytest.mark.unit
@pytest.mark.asyncio
async def test_publish_all_variants_returns_before_http_prepare_when_empty_after_dedup(monkeypatch):
    from executor import user_schema

    async def fail_prepare(*_args, **_kwargs):
        raise AssertionError("HTTP prepare should not run when there are no text variants")

    async def fail_dom_publish(*_args, **_kwargs):
        raise AssertionError("DOM publish should not run when there are no text variants")

    async def fake_username(_page):
        return "main_user"

    monkeypatch.setattr(user_schema, "_prepare_user_schema_http_session", fail_prepare)
    monkeypatch.setattr(user_schema, "publish_comment_hybrid", fail_dom_publish)
    monkeypatch.setattr(user_schema, "extract_tiktok_username", fake_username)

    published, failure_reason = await user_schema._publish_all_variants_on_account(
        SimpleNamespace(),
        SimpleNamespace(url="https://www.tiktok.com/@demo/video/1234567890123456789"),
        SimpleNamespace(id=41),
        ["", "   ", None],
        correlation_id="task_http_empty",
        expected_video_url="https://www.tiktok.com/@demo/video/1234567890123456789",
    )

    assert published == []
    assert failure_reason is None


@pytest.mark.unit
@pytest.mark.asyncio
async def test_publish_all_variants_uses_http_first_when_adapter_succeeds(monkeypatch):
    from executor import user_schema
    from executor.publish_action import PublishResult

    http_session = object()
    http_calls = []
    successes = []

    async def fake_prepare(*_args, **_kwargs):
        return http_session

    async def fake_http(session, text, *, correlation_id):
        assert session is http_session
        http_calls.append((text, correlation_id))
        return PublishResult(
            status="success",
            comment_id=f"http-{len(http_calls)}",
            network_captured=True,
            network_request_attempted=True,
            dom_strategy="http_user_schema",
        )

    async def fail_dom_publish(*_args, **_kwargs):
        raise AssertionError("DOM fallback should not run after HTTP success")

    async def fake_username(_page):
        return "main_user"

    async def fake_sleep(_seconds):
        return None

    monkeypatch.setattr(user_schema._settings, "TIKTOK_NETWORK_FIRST", True, raising=False)
    monkeypatch.setattr(user_schema._settings, "TIKTOK_NETWORK_IMPL_READY", True, raising=False)
    monkeypatch.setattr(user_schema, "_prepare_user_schema_http_session", fake_prepare)
    monkeypatch.setattr(user_schema, "_publish_main_http_first_if_eligible", fake_http)
    monkeypatch.setattr(user_schema, "publish_comment_hybrid", fail_dom_publish)
    monkeypatch.setattr(user_schema, "extract_tiktok_username", fake_username)
    monkeypatch.setattr(user_schema.asyncio, "sleep", fake_sleep)
    monkeypatch.setattr(
        user_schema,
        "_register_runtime_success",
        lambda _db, account, *, scope: successes.append((account.id, scope)),
    )

    published, failure_reason = await user_schema._publish_all_variants_on_account(
        SimpleNamespace(),
        SimpleNamespace(url="https://www.tiktok.com/@demo/video/1234567890123456789"),
        SimpleNamespace(id=42),
        ["hello", "world"],
        correlation_id="task_http_main",
        expected_video_url="https://www.tiktok.com/@demo/video/1234567890123456789",
    )

    assert failure_reason is None
    assert http_calls == [
        ("hello", "task_http_main_v1"),
        ("world", "task_http_main_v2"),
    ]
    assert [main.comment_id for main in published] == ["http-1", "http-2"]
    assert all(main.network_confirmed for main in published)
    assert [main.method for main in published] == ["http_user_schema", "http_user_schema"]
    assert successes == [(42, "main"), (42, "main")]


@pytest.mark.unit
@pytest.mark.asyncio
async def test_publish_all_variants_parallel_dom_first_skips_http_prepare(monkeypatch):
    from executor import user_schema
    from executor.publish_action import PublishResult

    dom_calls = []

    async def fail_prepare(*_args, **_kwargs):
        raise AssertionError("Parallel DOM-first should not prepare HTTP signer")

    async def fake_dom(_page, text, **kwargs):
        dom_calls.append((text, kwargs.get("correlation_id")))
        return PublishResult(
            status="success",
            comment_id="dom-cid",
            network_captured=True,
            dom_strategy="main-dom",
        )

    async def fake_username(_page):
        return "main_user"

    monkeypatch.setattr(user_schema._settings, "PARALLEL_COMMENTING_DOM_FIRST", True, raising=False)
    monkeypatch.setattr(user_schema, "_prepare_user_schema_http_session", fail_prepare)
    monkeypatch.setattr(user_schema, "publish_comment_hybrid", fake_dom)
    monkeypatch.setattr(user_schema, "extract_tiktok_username", fake_username)
    monkeypatch.setattr(user_schema, "_register_runtime_success", lambda *_args, **_kwargs: None)

    published, failure_reason = await user_schema._publish_all_variants_on_account(
        SimpleNamespace(),
        SimpleNamespace(url="https://www.tiktok.com/@demo/video/1234567890123456789"),
        SimpleNamespace(id=45),
        ["hello"],
        correlation_id="task_1_parallel_main_v1_a1_acc45",
        expected_video_url="https://www.tiktok.com/@demo/video/1234567890123456789",
    )

    assert failure_reason is None
    assert dom_calls == [("hello", "task_1_parallel_main_v1_a1_acc45_v1")]
    assert len(published) == 1
    assert published[0].comment_id == "dom-cid"


@pytest.mark.unit
def test_parallel_dom_first_also_applies_to_reply_only_parallel(monkeypatch):
    from executor import user_schema

    monkeypatch.setattr(user_schema._settings, "PARALLEL_COMMENTING_DOM_FIRST", True, raising=False)

    assert user_schema._parallel_dom_first_for_correlation("task_12_parallel_reply_v1_a1_acc77")
    assert user_schema._parallel_dom_first_for_correlation("task_12_engagement_reply_v1_a1_acc77")
    assert not user_schema._parallel_dom_first_for_correlation("task_http_reply")


@pytest.mark.unit
@pytest.mark.asyncio
async def test_publish_all_variants_falls_back_to_dom_when_http_adapter_skips(monkeypatch):
    from executor import user_schema
    from executor.publish_action import PublishResult

    dom_calls = []

    async def fake_prepare(*_args, **_kwargs):
        return object()

    async def fake_http(*_args, **_kwargs):
        return None

    async def fake_dom(_page, text, **kwargs):
        dom_calls.append((text, kwargs.get("correlation_id")))
        return PublishResult(
            status="success",
            comment_id="dom-cid",
            network_captured=True,
            dom_strategy="main-dom",
        )

    async def fake_username(_page):
        return "main_user"

    monkeypatch.setattr(user_schema._settings, "TIKTOK_NETWORK_FIRST", True, raising=False)
    monkeypatch.setattr(user_schema._settings, "TIKTOK_NETWORK_IMPL_READY", True, raising=False)
    monkeypatch.setattr(user_schema, "_prepare_user_schema_http_session", fake_prepare)
    monkeypatch.setattr(user_schema, "_publish_main_http_first_if_eligible", fake_http)
    monkeypatch.setattr(user_schema, "publish_comment_hybrid", fake_dom)
    monkeypatch.setattr(user_schema, "extract_tiktok_username", fake_username)
    monkeypatch.setattr(user_schema, "_register_runtime_success", lambda *_args, **_kwargs: None)

    published, failure_reason = await user_schema._publish_all_variants_on_account(
        SimpleNamespace(),
        SimpleNamespace(url="https://www.tiktok.com/@demo/video/1234567890123456789"),
        SimpleNamespace(id=43),
        ["hello"],
        correlation_id="task_http_fallback",
        expected_video_url="https://www.tiktok.com/@demo/video/1234567890123456789",
    )

    assert failure_reason is None
    assert dom_calls == [("hello", "task_http_fallback_v1")]
    assert len(published) == 1
    assert published[0].comment_id == "dom-cid"


@pytest.mark.unit
@pytest.mark.asyncio
async def test_publish_all_variants_uses_dom_only_when_network_first_disabled(monkeypatch):
    from executor import user_schema
    from executor.publish_action import PublishResult

    dom_calls = []

    async def fail_prepare(*_args, **_kwargs):
        raise AssertionError("HTTP session must not be prepared when network-first is disabled")

    async def fail_http(*_args, **_kwargs):
        raise AssertionError("HTTP publish must not run when network-first is disabled")

    async def fake_dom(_page, text, **kwargs):
        dom_calls.append((text, kwargs.get("correlation_id")))
        return PublishResult(
            status="success",
            comment_id="dom-cid",
            network_captured=True,
            dom_strategy="main-dom",
        )

    async def fake_username(_page):
        return "main_user"

    monkeypatch.setattr(user_schema._settings, "TIKTOK_NETWORK_FIRST", False, raising=False)
    monkeypatch.setattr(user_schema._settings, "TIKTOK_NETWORK_IMPL_READY", True, raising=False)
    monkeypatch.setattr(user_schema, "_prepare_user_schema_http_session", fail_prepare)
    monkeypatch.setattr(user_schema, "_publish_main_http_first_if_eligible", fail_http)
    monkeypatch.setattr(user_schema, "publish_comment_hybrid", fake_dom)
    monkeypatch.setattr(user_schema, "extract_tiktok_username", fake_username)
    monkeypatch.setattr(user_schema, "_register_runtime_success", lambda *_args, **_kwargs: None)

    published, failure_reason = await user_schema._publish_all_variants_on_account(
        SimpleNamespace(),
        SimpleNamespace(url="https://www.tiktok.com/@demo/video/1234567890123456789"),
        SimpleNamespace(id=43),
        ["hello"],
        correlation_id="task_dom_only_main",
        expected_video_url="https://www.tiktok.com/@demo/video/1234567890123456789",
    )

    assert failure_reason is None
    assert dom_calls == [("hello", "task_dom_only_main_v1")]
    assert len(published) == 1
    assert published[0].comment_id == "dom-cid"


@pytest.mark.unit
@pytest.mark.asyncio
async def test_publish_all_variants_does_not_dom_fallback_after_attempted_http_failure(monkeypatch):
    from executor import user_schema
    from executor.publish_action import PublishResult

    http_session = object()

    async def fake_prepare(*_args, **_kwargs):
        return http_session

    async def fake_http(session, text, *, correlation_id):
        assert session is http_session
        assert text == "hello"
        assert correlation_id == "task_http_attempted_failure_v1"
        return PublishResult(
            status="failed",
            error_reason="stealth_reject_empty_body",
            network_request_attempted=True,
            dom_strategy="http_user_schema",
            post_was_enabled_at_click=True,
        )

    async def fail_dom_publish(*_args, **_kwargs):
        raise AssertionError("DOM fallback must not run after an attempted HTTP submit")

    async def fake_username(_page):
        return "main_user"

    monkeypatch.setattr(user_schema._settings, "TIKTOK_NETWORK_FIRST", True, raising=False)
    monkeypatch.setattr(user_schema._settings, "TIKTOK_NETWORK_IMPL_READY", True, raising=False)
    monkeypatch.setattr(user_schema, "_prepare_user_schema_http_session", fake_prepare)
    monkeypatch.setattr(user_schema, "_publish_main_http_first_if_eligible", fake_http)
    monkeypatch.setattr(user_schema, "publish_comment_hybrid", fail_dom_publish)
    monkeypatch.setattr(user_schema, "extract_tiktok_username", fake_username)
    monkeypatch.setattr(user_schema, "find_comment_in_list", AsyncMock(return_value=(False, {})))
    monkeypatch.setattr(user_schema, "_deny_if_visible_logged_out", AsyncMock(return_value=None))

    published, failure_reason = await user_schema._publish_all_variants_on_account(
        SimpleNamespace(),
        SimpleNamespace(url="https://www.tiktok.com/@demo/video/1234567890123456789"),
        SimpleNamespace(id=44),
        ["hello"],
        correlation_id="task_http_attempted_failure",
        expected_video_url="https://www.tiktok.com/@demo/video/1234567890123456789",
    )

    assert failure_reason is None
    assert len(published) == 1
    assert published[0].text == "hello"
    assert published[0].comment_id is None
    assert published[0].network_confirmed is False
    assert published[0].local_confirmed is False
    assert published[0].method == "http_user_schema"


@pytest.mark.unit
@pytest.mark.asyncio
async def test_parallel_main_publish_records_submitted_unconfirmed_after_network_submit(monkeypatch):
    from executor import user_schema
    from executor.publish_action import PublishResult

    async def fake_publish(_page, text, **kwargs):
        assert text == "hello"
        assert kwargs["correlation_id"] == "task_1_parallel_main_v1_a1_acc44_v1"
        return PublishResult(
            status="failed",
            error_reason="stealth_reject_empty_body",
            network_request_attempted=True,
            dom_strategy="side-panel",
            post_was_enabled_at_click=True,
            composer_clean_after_submit=True,
        )

    async def fake_username(_page):
        return "main_user"

    monkeypatch.setattr(user_schema, "publish_comment_hybrid", fake_publish)
    monkeypatch.setattr(user_schema, "extract_tiktok_username", fake_username)
    monkeypatch.setattr(user_schema, "find_comment_in_list", AsyncMock(return_value=(False, {})))
    monkeypatch.setattr(user_schema, "_deny_if_visible_logged_out", AsyncMock(return_value=None))
    monkeypatch.setattr(user_schema, "_register_runtime_success", lambda *_args, **_kwargs: None)

    published, failure_reason = await user_schema._publish_all_variants_on_account(
        SimpleNamespace(),
        SimpleNamespace(url="https://www.tiktok.com/@demo/video/1234567890123456789"),
        SimpleNamespace(id=44),
        ["hello"],
        correlation_id="task_1_parallel_main_v1_a1_acc44",
        expected_video_url="https://www.tiktok.com/@demo/video/1234567890123456789",
    )

    assert failure_reason is None
    assert len(published) == 1
    assert published[0].text == "hello"
    assert published[0].comment_id is None
    assert published[0].network_confirmed is False
    assert published[0].local_confirmed is False
    assert published[0].method == "side-panel"
    assert published[0].account_id == 44
    assert published[0].tiktok_username == "main_user"


@pytest.mark.unit
@pytest.mark.asyncio
async def test_parallel_main_direct_response_without_comment_id_counts_as_submitted_unconfirmed(monkeypatch):
    from executor import user_schema
    from executor.publish_action import PublishResult

    async def fake_publish(_page, text, **kwargs):
        assert text == "hello"
        assert kwargs["correlation_id"] == "task_1_parallel_main_v1_a1_acc44_v1"
        return PublishResult(
            status="failed",
            error_reason="main_direct_api_response_without_comment_id",
            network_captured=True,
            network_request_attempted=True,
            dom_strategy="main-direct-test",
            post_was_enabled_at_click=True,
        )

    async def fake_username(_page):
        return "main_user"

    monkeypatch.setattr(user_schema, "publish_comment_hybrid", fake_publish)
    monkeypatch.setattr(user_schema, "extract_tiktok_username", fake_username)
    monkeypatch.setattr(user_schema, "find_comment_in_list", AsyncMock(return_value=(False, {})))
    monkeypatch.setattr(user_schema, "_deny_if_visible_logged_out", AsyncMock(return_value=None))
    monkeypatch.setattr(user_schema, "_register_runtime_success", lambda *_args, **_kwargs: None)

    published, failure_reason = await user_schema._publish_all_variants_on_account(
        SimpleNamespace(),
        SimpleNamespace(url="https://www.tiktok.com/@demo/video/1234567890123456789"),
        SimpleNamespace(id=44),
        ["hello"],
        correlation_id="task_1_parallel_main_v1_a1_acc44",
        expected_video_url="https://www.tiktok.com/@demo/video/1234567890123456789",
    )

    assert failure_reason is None
    assert len(published) == 1
    assert published[0].text == "hello"
    assert published[0].comment_id is None
    assert published[0].network_confirmed is False
    assert published[0].local_confirmed is False
    assert published[0].method == "main-direct-test"


@pytest.mark.unit
@pytest.mark.asyncio
async def test_parallel_main_submit_without_response_continues_when_composer_clean(monkeypatch):
    from executor import user_schema
    from executor.publish_action import PublishResult

    calls = []

    async def fake_publish(_page, text, **kwargs):
        calls.append((text, kwargs["correlation_id"]))
        return PublishResult(
            status="timeout",
            error_reason="no_network_response_after_main_direct_post_click",
            network_request_attempted=True,
            dom_strategy="main-direct-test",
            post_was_enabled_at_click=True,
            composer_clean_after_submit=True,
        )

    async def fake_username(_page):
        return "main_user"

    monkeypatch.setattr(user_schema, "publish_comment_hybrid", fake_publish)
    monkeypatch.setattr(user_schema, "extract_tiktok_username", fake_username)
    monkeypatch.setattr(user_schema, "find_comment_in_list", AsyncMock(return_value=(False, {})))
    monkeypatch.setattr(user_schema, "_deny_if_visible_logged_out", AsyncMock(return_value=None))
    monkeypatch.setattr(user_schema, "_register_runtime_success", lambda *_args, **_kwargs: None)

    published, failure_reason = await user_schema._publish_all_variants_on_account(
        SimpleNamespace(),
        SimpleNamespace(url="https://www.tiktok.com/@demo/video/1234567890123456789"),
        SimpleNamespace(id=44),
        ["first", "second"],
        correlation_id="task_1_parallel_main_v1_a1_acc44",
        expected_video_url="https://www.tiktok.com/@demo/video/1234567890123456789",
    )

    assert calls == [
        ("first", "task_1_parallel_main_v1_a1_acc44_v1"),
        ("second", "task_1_parallel_main_v1_a1_acc44_v2"),
    ]
    assert failure_reason is None
    assert len(published) == 2
    assert published[0].text == "first"
    assert published[0].comment_id is None
    assert published[0].local_confirmed is False
    assert published[1].text == "second"


@pytest.mark.unit
@pytest.mark.asyncio
async def test_parallel_main_submit_without_response_stops_after_unconfirmed_when_reset_fails(monkeypatch):
    from executor import user_schema
    from executor.publish_action import PublishResult

    calls = []

    async def fake_publish(_page, text, **kwargs):
        calls.append((text, kwargs["correlation_id"]))
        return PublishResult(
            status="timeout",
            error_reason="no_network_response_after_main_direct_post_click",
            network_request_attempted=True,
            dom_strategy="main-direct-test",
            post_was_enabled_at_click=True,
            composer_clean_after_submit=False,
        )

    async def fake_username(_page):
        return "main_user"

    async def fake_reset(_page, **_kwargs):
        return False

    monkeypatch.setattr(user_schema, "publish_comment_hybrid", fake_publish)
    monkeypatch.setattr(user_schema, "extract_tiktok_username", fake_username)
    monkeypatch.setattr(user_schema, "reset_main_comment_composer_for_next_variant", fake_reset)
    monkeypatch.setattr(user_schema, "find_comment_in_list", AsyncMock(return_value=(False, {})))
    monkeypatch.setattr(user_schema, "_deny_if_visible_logged_out", AsyncMock(return_value=None))

    published, failure_reason = await user_schema._publish_all_variants_on_account(
        SimpleNamespace(),
        SimpleNamespace(url="https://www.tiktok.com/@demo/video/1234567890123456789"),
        SimpleNamespace(id=44),
        ["first", "second"],
        correlation_id="task_1_parallel_main_v1_a1_acc44",
        expected_video_url="https://www.tiktok.com/@demo/video/1234567890123456789",
    )

    assert calls == [("first", "task_1_parallel_main_v1_a1_acc44_v1")]
    assert failure_reason == (
        "main_partial:published=1/2;remaining=1;"
        "reason=account_series_stopped_after_unconfirmed_unsafe_composer"
    )
    assert len(published) == 1
    assert published[0].text == "first"


@pytest.mark.unit
@pytest.mark.asyncio
async def test_parallel_main_submit_without_clean_composer_continues_after_late_reset(monkeypatch):
    from executor import user_schema
    from executor.publish_action import PublishResult

    calls = []
    resets = {"count": 0}

    async def fake_publish(_page, text, **kwargs):
        calls.append((text, kwargs["correlation_id"]))
        return PublishResult(
            status="timeout",
            error_reason="no_network_response_after_main_direct_post_click",
            network_request_attempted=True,
            dom_strategy="main-direct-test",
            post_was_enabled_at_click=True,
            composer_clean_after_submit=False,
        )

    async def fake_username(_page):
        return "main_user"

    async def fake_reset(_page, **_kwargs):
        resets["count"] += 1
        return True

    monkeypatch.setattr(user_schema, "publish_comment_hybrid", fake_publish)
    monkeypatch.setattr(user_schema, "extract_tiktok_username", fake_username)
    monkeypatch.setattr(user_schema, "reset_main_comment_composer_for_next_variant", fake_reset)
    monkeypatch.setattr(user_schema, "find_comment_in_list", AsyncMock(return_value=(False, {})))
    monkeypatch.setattr(user_schema, "_deny_if_visible_logged_out", AsyncMock(return_value=None))

    published, failure_reason = await user_schema._publish_all_variants_on_account(
        SimpleNamespace(),
        SimpleNamespace(url="https://www.tiktok.com/@demo/video/1234567890123456789"),
        SimpleNamespace(id=44),
        ["first", "second"],
        correlation_id="task_1_parallel_main_v1_a1_acc44",
        expected_video_url="https://www.tiktok.com/@demo/video/1234567890123456789",
    )

    assert calls == [
        ("first", "task_1_parallel_main_v1_a1_acc44_v1"),
        ("second", "task_1_parallel_main_v1_a1_acc44_v2"),
    ]
    assert resets["count"] == 1
    assert failure_reason is None
    assert len(published) == 2
    assert published[0].text == "first"
    assert published[1].text == "second"


@pytest.mark.unit
@pytest.mark.asyncio
async def test_parallel_main_submit_without_clean_composer_does_not_recover_same_browser(monkeypatch):
    from executor import user_schema
    from executor.publish_action import PublishResult

    calls = []
    resets = {"count": 0}

    async def fake_publish(_page, text, **kwargs):
        calls.append((text, kwargs["correlation_id"]))
        return PublishResult(
            status="timeout",
            error_reason="no_network_response_after_main_direct_post_click",
            network_request_attempted=True,
            dom_strategy="main-direct-test",
            post_was_enabled_at_click=True,
            composer_clean_after_submit=False,
        )

    async def fake_username(_page):
        return "main_user"

    async def fake_reset(_page, **_kwargs):
        resets["count"] += 1
        return False

    monkeypatch.setattr(user_schema, "publish_comment_hybrid", fake_publish)
    monkeypatch.setattr(user_schema, "extract_tiktok_username", fake_username)
    monkeypatch.setattr(user_schema, "reset_main_comment_composer_for_next_variant", fake_reset)
    monkeypatch.setattr(user_schema, "find_comment_in_list", AsyncMock(return_value=(False, {})))
    monkeypatch.setattr(user_schema, "_deny_if_visible_logged_out", AsyncMock(return_value=None))

    published, failure_reason = await user_schema._publish_all_variants_on_account(
        SimpleNamespace(),
        SimpleNamespace(url="https://www.tiktok.com/@demo/video/1234567890123456789"),
        SimpleNamespace(id=44),
        ["first", "second"],
        correlation_id="task_1_parallel_main_v1_a1_acc44",
        expected_video_url="https://www.tiktok.com/@demo/video/1234567890123456789",
    )

    assert calls == [("first", "task_1_parallel_main_v1_a1_acc44_v1")]
    assert resets["count"] == 1
    assert failure_reason == (
        "main_partial:published=1/2;remaining=1;"
        "reason=account_series_stopped_after_unconfirmed_unsafe_composer"
    )
    assert len(published) == 1
    assert published[0].text == "first"


@pytest.mark.unit
@pytest.mark.asyncio
async def test_parallel_main_direct_no_response_accepts_author_scoped_local_match(monkeypatch):
    from executor import user_schema
    from executor.publish_action import PublishResult

    async def fake_publish(_page, text, **kwargs):
        expected_index = {"first": 1, "second": 2}[text]
        assert kwargs["correlation_id"] == f"task_1_parallel_main_v1_a1_acc44_v{expected_index}"
        return PublishResult(
            status="timeout",
            error_reason="no_network_response_after_main_direct_post_click",
            network_request_attempted=True,
            dom_strategy="main-direct-test",
            post_was_enabled_at_click=True,
            composer_clean_after_submit=True,
        )

    async def fake_username(_page):
        return "main_user"

    finder = AsyncMock(return_value=(True, {"comment_id": "author-scoped-cid"}))

    monkeypatch.setattr(user_schema, "publish_comment_hybrid", fake_publish)
    monkeypatch.setattr(user_schema, "extract_tiktok_username", fake_username)
    monkeypatch.setattr(user_schema, "find_comment_in_list", finder)
    monkeypatch.setattr(user_schema, "_deny_if_visible_logged_out", AsyncMock(return_value=None))

    published, failure_reason = await user_schema._publish_all_variants_on_account(
        SimpleNamespace(),
        SimpleNamespace(url="https://www.tiktok.com/@demo/video/1234567890123456789"),
        SimpleNamespace(id=44),
        ["first", "second"],
        correlation_id="task_1_parallel_main_v1_a1_acc44",
        expected_video_url="https://www.tiktok.com/@demo/video/1234567890123456789",
    )

    assert finder.await_count == 2
    assert all(call.kwargs.get("expected_author") == "main_user" for call in finder.await_args_list)
    assert all(call.kwargs.get("require_author_match") is True for call in finder.await_args_list)
    assert all(call.kwargs.get("strict_scope") is True for call in finder.await_args_list)
    assert all(call.kwargs.get("top_level_only") is True for call in finder.await_args_list)
    assert all(call.kwargs.get("min_match_ratio") == 1.0 for call in finder.await_args_list)
    assert all(call.kwargs.get("ensure_open") is False for call in finder.await_args_list)
    assert failure_reason is None
    assert len(published) == 2
    assert published[0].text == "first"
    assert published[0].comment_id == "author-scoped-cid"
    assert published[0].local_confirmed is True
    assert published[1].text == "second"
    assert published[1].comment_id == "author-scoped-cid"
    assert published[1].local_confirmed is True


@pytest.mark.unit
@pytest.mark.asyncio
async def test_parallel_main_local_only_dirty_composer_stops_account_series(monkeypatch):
    from executor import user_schema
    from executor.publish_action import PublishResult

    calls = []

    async def fake_publish(_page, text, **kwargs):
        calls.append((text, kwargs["correlation_id"]))
        return PublishResult(
            status="timeout",
            error_reason="no_network_response_after_publish_request",
            network_request_attempted=True,
            dom_strategy="data-e2e-inner-editable",
            post_was_enabled_at_click=True,
            composer_clean_after_submit=False,
        )

    async def fake_username(_page):
        return "main_user"

    reset = AsyncMock(return_value=False)
    finder = AsyncMock(return_value=(True, {"comment_id": "cid-first"}))

    monkeypatch.setattr(user_schema, "publish_comment_hybrid", fake_publish)
    monkeypatch.setattr(user_schema, "extract_tiktok_username", fake_username)
    monkeypatch.setattr(user_schema, "reset_main_comment_composer_for_next_variant", reset)
    monkeypatch.setattr(user_schema, "find_comment_in_list", finder)
    monkeypatch.setattr(user_schema, "_deny_if_visible_logged_out", AsyncMock(return_value=None))

    published, failure_reason = await user_schema._publish_all_variants_on_account(
        SimpleNamespace(),
        SimpleNamespace(url="https://www.tiktok.com/@demo/video/1234567890123456789"),
        SimpleNamespace(id=44),
        ["first", "second"],
        correlation_id="task_1_parallel_main_v1_a1_acc44",
        expected_video_url="https://www.tiktok.com/@demo/video/1234567890123456789",
    )

    assert calls == [("first", "task_1_parallel_main_v1_a1_acc44_v1")]
    assert reset.await_count == 1
    assert failure_reason == (
        "main_partial:published=1/2;remaining=1;"
        "reason=account_series_stopped_after_local_unsafe_composer"
    )
    assert len(published) == 1
    assert published[0].text == "first"
    assert published[0].comment_id == "cid-first"
    assert published[0].local_confirmed is True


@pytest.mark.unit
@pytest.mark.asyncio
async def test_parallel_main_local_confirmation_timeout_continues_when_composer_clean(monkeypatch):
    from executor import user_schema
    from executor.publish_action import PublishResult

    async def fake_publish(_page, text, **kwargs):
        expected_index = {"first": 1, "second": 2}[text]
        assert kwargs["correlation_id"] == f"task_1_parallel_main_v1_a1_acc44_v{expected_index}"
        return PublishResult(
            status="failed",
            error_reason="stealth_reject_empty_body",
            network_request_attempted=True,
            dom_strategy="side-panel",
            post_was_enabled_at_click=True,
            composer_clean_after_submit=True,
        )

    async def fake_username(_page):
        return "main_user"

    async def slow_find_comment(*_args, **_kwargs):
        await asyncio.sleep(60)
        return True, {"comment_id": "late-cid"}

    monkeypatch.setattr(user_schema, "publish_comment_hybrid", fake_publish)
    monkeypatch.setattr(user_schema, "extract_tiktok_username", fake_username)
    monkeypatch.setattr(user_schema, "find_comment_in_list", slow_find_comment)
    monkeypatch.setattr(user_schema, "_main_local_confirmation_timeout_seconds", lambda: 0.01)
    monkeypatch.setattr(user_schema, "_deny_if_visible_logged_out", AsyncMock(return_value=None))

    published, failure_reason = await user_schema._publish_all_variants_on_account(
        SimpleNamespace(),
        SimpleNamespace(url="https://www.tiktok.com/@demo/video/1234567890123456789"),
        SimpleNamespace(id=44),
        ["first", "second"],
        correlation_id="task_1_parallel_main_v1_a1_acc44",
        expected_video_url="https://www.tiktok.com/@demo/video/1234567890123456789",
    )

    assert failure_reason is None
    assert len(published) == 2
    assert published[0].text == "first"
    assert published[0].comment_id is None
    assert published[0].local_confirmed is False
    assert published[1].text == "second"


@pytest.mark.unit
@pytest.mark.asyncio
async def test_publish_all_variants_stops_after_dirty_typing_timeout(monkeypatch):
    from executor import user_schema
    from executor.publish_action import PublishResult

    calls = []

    async def fake_publish(_page, text, **kwargs):
        calls.append((text, kwargs["correlation_id"]))
        return PublishResult(status="timeout", error_reason="publish_deadline_exceeded:typing")

    async def fake_username(_page):
        return "main_user"

    monkeypatch.setattr(user_schema, "publish_comment_hybrid", fake_publish)
    monkeypatch.setattr(user_schema, "extract_tiktok_username", fake_username)
    monkeypatch.setattr(user_schema, "_deny_if_visible_logged_out", AsyncMock(return_value=None))

    published, failure_reason = await user_schema._publish_all_variants_on_account(
        SimpleNamespace(),
        SimpleNamespace(url="https://www.tiktok.com/@demo/video/1234567890123456789"),
        SimpleNamespace(id=44),
        ["first", "second"],
        correlation_id="task_1",
        expected_video_url="https://www.tiktok.com/@demo/video/1234567890123456789",
    )

    assert calls == [("first", "task_1_v1")]
    assert published == []
    assert failure_reason == "publish_deadline_exceeded:typing"


@pytest.mark.unit
@pytest.mark.asyncio
async def test_publish_all_variants_returns_comment_ui_failure_reason(monkeypatch):
    from executor import user_schema
    from executor.publish_action import PublishResult

    async def fake_publish(_page, _text, **_kwargs):
        return PublishResult(status="dom_error", error_reason="input_not_found: comment_ui_not_found")

    async def fake_recover(
        _page,
        *,
        correlation_id,
        expected_video_url=None,
        require_clean_composer=False,
    ):
        assert correlation_id == "task_2_v1"
        assert expected_video_url is None
        assert require_clean_composer is True
        return False

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
            composer_clean_after_submit=True,
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
                composer_clean_after_submit=True,
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
    assert failure_reason == "main_partial:published=1/3;remaining=2;reason=captcha_challenge:slider"
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

    monkeypatch.setattr(user_schema._settings, "TIKTOK_NETWORK_FIRST", True, raising=False)
    monkeypatch.setattr(user_schema._settings, "TIKTOK_NETWORK_IMPL_READY", True, raising=False)
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

    monkeypatch.setattr(user_schema._settings, "TIKTOK_NETWORK_FIRST", True, raising=False)
    monkeypatch.setattr(user_schema._settings, "TIKTOK_NETWORK_IMPL_READY", True, raising=False)
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
async def test_reply_visible_comment_list_search_timeouts_are_inconclusive(monkeypatch):
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

    monkeypatch.setattr(user_schema._settings, "TIKTOK_NETWORK_FIRST", True, raising=False)
    monkeypatch.setattr(user_schema._settings, "TIKTOK_NETWORK_IMPL_READY", True, raising=False)
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
    assert result.confident_empty_search is False
    assert result.saw_comment_rows is False
    assert result.parent_search_max_seen_rows == 0
    assert result.fallback_attempted is False
    assert result.fallback_mains == []


@pytest.mark.unit
@pytest.mark.asyncio
async def test_reply_visible_comment_list_search_timeouts_do_not_publish_fallback(monkeypatch):
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

    monkeypatch.setattr(user_schema._settings, "TIKTOK_NETWORK_FIRST", True, raising=False)
    monkeypatch.setattr(user_schema._settings, "TIKTOK_NETWORK_IMPL_READY", True, raising=False)
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
    assert result.confident_empty_search is False
    assert result.fallback_attempted is False
    assert result.fallback_mains == []
    assert result.replies == []


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

    monkeypatch.setattr(user_schema._settings, "TIKTOK_NETWORK_FIRST", True, raising=False)
    monkeypatch.setattr(user_schema._settings, "TIKTOK_NETWORK_IMPL_READY", True, raising=False)
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

    monkeypatch.setattr(user_schema._settings, "TIKTOK_NETWORK_FIRST", True, raising=False)
    monkeypatch.setattr(user_schema._settings, "TIKTOK_NETWORK_IMPL_READY", True, raising=False)
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
async def test_reply_zero_comment_count_is_confident_empty_without_open_list(monkeypatch):
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
        raise AssertionError("zero-comment video must not start parent scrolling")

    monkeypatch.setattr(user_schema._settings, "TIKTOK_NETWORK_FIRST", True, raising=False)
    monkeypatch.setattr(user_schema._settings, "TIKTOK_NETWORK_IMPL_READY", True, raising=False)
    monkeypatch.setattr(user_schema, "create_page_for_account", fake_create_page_for_account)
    monkeypatch.setattr(user_schema, "_ensure_account_login_ready", fake_login_ready)
    monkeypatch.setattr(user_schema, "needs_enrichment", lambda _account: False)
    monkeypatch.setattr(user_schema, "_ensure_page_on_target_media", AsyncMock(return_value=True))
    monkeypatch.setattr(user_schema, "_read_tiktok_total_comment_count", AsyncMock(return_value=0))
    monkeypatch.setattr(user_schema, "_open_comment_list_with_safe_retry", AsyncMock(return_value=False))
    monkeypatch.setattr(user_schema, "_visible_captcha_reason", AsyncMock(return_value=None))
    monkeypatch.setattr(user_schema, "_find_main_anchor", fail_find_main_anchor)
    monkeypatch.setattr(user_schema.asyncio, "sleep", AsyncMock(return_value=None))

    result = await user_schema._reply_with_variant_on_all_mains(
        SimpleNamespace(),
        SimpleNamespace(id=77, proxy=None),
        "https://www.tiktok.com/@demo/video/1234567890123456789",
        ["old local main"],
        "reply text",
        variant_index=1,
        correlation_id="task_zero_comments_reply",
        fallback_main_texts=None,
    )

    assert result.searched is True
    assert result.confident_empty_search is True
    assert result.saw_comment_rows is False
    assert result.parent_search_exhausted is True
    assert result.parent_search_scroll_container_scrollable is False
    assert result.parent_search_total_comment_count == 0
    assert result.parent_search_scroll_budget == user_schema._REPLY_MAIN_SEARCH_TO_END_MAX_SCROLLS
    assert result.replies == []


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
        def __init__(self):
            self.anchor_token = None

        async def is_visible(self):
            return True

        async def evaluate(self, _script, token):
            self.anchor_token = token
            return token

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

    anchor = await user_schema._find_main_anchor(FakePage(), [main])
    text, cid = anchor

    assert text == "main text"
    assert cid == "cid-123"
    assert anchor.anchor_token
    assert anchor.anchor_token.startswith("pa-direct-")


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
async def test_reply_ambiguous_publish_without_local_visibility_keeps_parent_pending(monkeypatch):
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
        return False

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
    assert result.replies[0].counts_as_submitted is False
    assert result.replies[0].soft_submitted is True
    assert result.replies[0].reply_attempt_key
    assert result.replies[0].reply_confirmation == "submitted_unconfirmed"
    assert result.replies[0].attempted is True
    assert result.replies[0].error == "stealth_reject_empty_body"
    assert result.replies[0].parent_comment_id == "cid-main"
    assert result.replies[0].method == "reply-dom"
    assert [p.text for p in result.found_mains] == ["main one"]
    assert "cid-main" in result.attempted_parent_keys
    assert result.soft_submitted_reply_keys == {result.replies[0].reply_attempt_key}
    assert "cid-main" not in done_parent_keys
    assert not failures


@pytest.mark.unit
@pytest.mark.asyncio
async def test_reply_soft_submitted_key_suppresses_duplicate_retry(monkeypatch):
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

    main = user_schema.PublishedMain(
        text="main one",
        comment_id="cid-main",
        network_confirmed=True,
        local_confirmed=True,
        method="hybrid_dom",
        account_id=10,
        tiktok_username=None,
    )
    soft_key = user_schema._reply_attempt_key("cid-main", "reply text")

    async def fake_create_page_for_account(_account):
        return FakeContext(), FakePage()

    async def fake_login_ready(*_args, **_kwargs):
        return True, None

    async def fail_find_main_anchor(*_args, **_kwargs):
        raise AssertionError("soft-submitted parent should be skipped before anchor search")

    async def fail_publish_reply(*_args, **_kwargs):
        raise AssertionError("soft-submitted parent should not be published again")

    async def fake_sleep(_seconds):
        return None

    monkeypatch.setattr(user_schema, "create_page_for_account", fake_create_page_for_account)
    monkeypatch.setattr(user_schema, "_ensure_account_login_ready", fake_login_ready)
    monkeypatch.setattr(user_schema, "needs_enrichment", lambda _account: False)
    monkeypatch.setattr(user_schema, "_ensure_page_on_target_media", AsyncMock(return_value=True))
    monkeypatch.setattr(user_schema, "_open_comment_list_with_safe_retry", AsyncMock(return_value=True))
    monkeypatch.setattr(user_schema, "_wait_for_searchable_comment_surface", AsyncMock(return_value=True))
    monkeypatch.setattr(user_schema, "_find_main_anchor", fail_find_main_anchor)
    monkeypatch.setattr(user_schema, "publish_reply_hybrid", fail_publish_reply)
    monkeypatch.setattr(user_schema.asyncio, "sleep", fake_sleep)

    result = await user_schema._reply_with_variant_on_all_mains(
        SimpleNamespace(),
        SimpleNamespace(id=88, proxy=None),
        "https://www.tiktok.com/@demo/video/1234567890123456789",
        [main],
        "reply text",
        variant_index=1,
        correlation_id="task_soft_skip",
        soft_submitted_reply_keys={soft_key},
    )

    assert result.replies == []
    assert result.found_mains == []
    assert result.searched is True


@pytest.mark.unit
@pytest.mark.asyncio
async def test_reply_already_present_local_skips_publish_and_counts_submitted(monkeypatch):
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
            matched_text="main one",
            parent_comment_id="cid-main",
            open_text="main one",
            found_by="id",
            id_verified=True,
            anchor_token="anchor-1",
        )

    async def fake_verify_reply(*_args, **_kwargs):
        return True

    async def fail_publish_reply(*_args, **_kwargs):
        raise AssertionError("already-present reply should not be published again")

    async def fake_sleep(_seconds):
        return None

    done_parent_keys = set()
    submitted = []

    monkeypatch.setattr(user_schema, "create_page_for_account", fake_create_page_for_account)
    monkeypatch.setattr(user_schema, "_ensure_account_login_ready", fake_login_ready)
    monkeypatch.setattr(user_schema, "needs_enrichment", lambda _account: False)
    monkeypatch.setattr(user_schema, "_ensure_page_on_target_media", AsyncMock(return_value=True))
    monkeypatch.setattr(user_schema, "_open_comment_list_with_safe_retry", AsyncMock(return_value=True))
    monkeypatch.setattr(user_schema, "_wait_for_searchable_comment_surface", AsyncMock(return_value=True))
    monkeypatch.setattr(user_schema, "_find_main_anchor", fake_find_main_anchor)
    monkeypatch.setattr(user_schema, "_verify_reply_visible_near_parent", fake_verify_reply)
    monkeypatch.setattr(user_schema, "publish_reply_hybrid", fail_publish_reply)
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
        correlation_id="task_already_present",
        already_replied_parent_keys=done_parent_keys,
        on_submitted_outcome=submitted.append,
    )

    assert len(result.replies) == 1
    assert result.replies[0].success is True
    assert result.replies[0].attempted is False
    assert result.replies[0].already_present_local is True
    assert result.replies[0].local_confirmed is True
    assert result.replies[0].counts_as_submitted is True
    assert result.replies[0].reply_confirmation == "already_present_local"
    assert result.replies[0].method == "local_dedupe_skip"
    assert "cid-main" in done_parent_keys
    assert submitted == result.replies


@pytest.mark.unit
@pytest.mark.asyncio
async def test_reply_ambiguous_publish_with_local_visibility_counts_as_submitted(monkeypatch):
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
            composer_clean_after_submit=True,
        )

    verify_calls = 0

    async def fake_verify_reply(*_args, **_kwargs):
        nonlocal verify_calls
        verify_calls += 1
        return verify_calls > 1

    async def fake_sleep(_seconds):
        return None

    successes = []
    done_parent_keys = set()

    monkeypatch.setattr(user_schema, "create_page_for_account", fake_create_page_for_account)
    monkeypatch.setattr(user_schema, "_ensure_account_login_ready", fake_login_ready)
    monkeypatch.setattr(user_schema, "needs_enrichment", lambda _account: False)
    monkeypatch.setattr(user_schema, "_ensure_page_on_target_media", AsyncMock(return_value=True))
    monkeypatch.setattr(user_schema, "_open_comment_list_with_safe_retry", AsyncMock(return_value=True))
    monkeypatch.setattr(user_schema, "_find_main_anchor", fake_find_main_anchor)
    monkeypatch.setattr(user_schema, "publish_reply_hybrid", fake_publish_reply)
    monkeypatch.setattr(user_schema, "_verify_reply_visible_near_parent", fake_verify_reply)
    monkeypatch.setattr(user_schema, "_should_like_parent_before_reply", lambda *_args, **_kwargs: False)
    monkeypatch.setattr(user_schema.asyncio, "sleep", fake_sleep)
    monkeypatch.setattr(user_schema, "_register_runtime_success", lambda *_args, **_kwargs: successes.append(_args))

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
    assert result.replies[0].success is True
    assert result.replies[0].submitted_unconfirmed is False
    assert result.replies[0].local_confirmed is True
    assert result.replies[0].counts_as_submitted is True
    assert result.replies[0].reply_confirmation == "local_visible_near_parent"
    assert "cid-main" in result.attempted_parent_keys
    assert "cid-main" in done_parent_keys
    assert successes


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
async def test_reply_batch_captcha_preserves_found_batch_parent(monkeypatch):
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

    main = user_schema.PublishedMain(
        text="main one",
        comment_id="cid-main",
        network_confirmed=True,
        local_confirmed=True,
        method="hybrid_dom",
        account_id=10,
        tiktok_username=None,
    )
    captcha_events = []

    async def fake_create_page_for_account(_account):
        return FakeContext(), FakePage()

    async def fake_login_ready(*_args, **_kwargs):
        return True, None

    async def fake_batch(_page, _mains, **_kwargs):
        return [
            user_schema.MainAnchor(
                matched_text="main one",
                parent_comment_id="cid-main",
                open_text="main one",
                found_by="id",
                id_verified=True,
                source_main=main,
                parent_key=user_schema._main_parent_key(main),
            )
        ], {
            "error": "captcha_challenge:slider",
            "found_count": 1,
            "missing_count": 0,
            "target_count": 1,
            "matched_indexes": [0],
            "missing_indexes": [],
        }

    async def fail_publish(*_args, **_kwargs):
        raise AssertionError("captcha after batch search must not publish")

    monkeypatch.setattr(user_schema, "create_page_for_account", fake_create_page_for_account)
    monkeypatch.setattr(user_schema, "_ensure_account_login_ready", fake_login_ready)
    monkeypatch.setattr(user_schema, "needs_enrichment", lambda _account: False)
    monkeypatch.setattr(user_schema, "_ensure_page_on_target_media", AsyncMock(return_value=True))
    monkeypatch.setattr(user_schema, "_open_comment_list_with_safe_retry", AsyncMock(return_value=True))
    monkeypatch.setattr(user_schema, "_wait_for_searchable_comment_surface", AsyncMock(return_value=True))
    monkeypatch.setattr(user_schema, "_find_main_anchors_batch", fake_batch)
    monkeypatch.setattr(user_schema, "publish_reply_hybrid", fail_publish)
    monkeypatch.setattr(user_schema.asyncio, "sleep", AsyncMock(return_value=None))
    monkeypatch.setattr(
        user_schema,
        "_register_reply_captcha_failure",
        lambda _db, account, reason: captcha_events.append((account.id, reason)),
    )

    result = await user_schema._reply_with_variant_on_all_mains(
        SimpleNamespace(),
        SimpleNamespace(id=77, proxy=None),
        "https://www.tiktok.com/@demo/video/1234567890123456789",
        [main],
        "reply text",
        variant_index=1,
        correlation_id="task_reply_batch_captcha",
    )

    assert captcha_events == [(77, "captcha_challenge:slider")]
    assert [p.text for p in result.found_mains] == ["main one"]
    assert len(result.replies) == 1
    assert result.replies[0].error == "captcha_challenge:slider"
    assert result.replies[0].attempted is False


@pytest.mark.unit
@pytest.mark.asyncio
async def test_reply_uses_http_first_for_id_verified_parent(monkeypatch):
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

    parent_cid = "7641761192486194454"
    mains = [
        user_schema.PublishedMain(
            text="main one",
            comment_id=parent_cid,
            network_confirmed=True,
            local_confirmed=True,
            method="http_user_schema",
            account_id=10,
            tiktok_username=None,
        )
    ]
    http_session = object()
    http_calls = []

    async def fake_create_page_for_account(_account):
        return FakeContext(), FakePage()

    async def fake_login_ready(*_args, **_kwargs):
        return True, None

    async def fake_batch(_page, main_candidates, **_kwargs):
        return [
            user_schema.MainAnchor(
                matched_text="main one",
                parent_comment_id=parent_cid,
                open_text="main one visible",
                found_by="id",
                id_verified=True,
                source_main=main_candidates[0],
            )
        ], {}

    async def fail_single_search(*_args, **_kwargs):
        raise AssertionError("single search should not run when batch found parent")

    async def fake_prepare(*_args, **_kwargs):
        return http_session

    async def fake_http(session, reply_text, *, parent_comment_id, correlation_id):
        assert session is http_session
        http_calls.append((reply_text, parent_comment_id, correlation_id))
        return PublishResult(
            status="success",
            comment_id="reply-http-1",
            network_captured=True,
            network_request_attempted=True,
            dom_strategy="http_user_schema:network_exact",
        )

    async def fail_dom_publish(*_args, **_kwargs):
        raise AssertionError("DOM reply fallback should not run after HTTP success")

    async def fail_restore(*_args, **_kwargs):
        raise AssertionError("HTTP success should not need DOM anchor restoration")

    async def fake_sleep(_seconds):
        return None

    monkeypatch.setattr(user_schema._settings, "TIKTOK_NETWORK_FIRST", True, raising=False)
    monkeypatch.setattr(user_schema._settings, "TIKTOK_NETWORK_IMPL_READY", True, raising=False)
    monkeypatch.setattr(user_schema, "create_page_for_account", fake_create_page_for_account)
    monkeypatch.setattr(user_schema, "_ensure_account_login_ready", fake_login_ready)
    monkeypatch.setattr(user_schema, "needs_enrichment", lambda _account: False)
    monkeypatch.setattr(user_schema, "_ensure_page_on_target_media", AsyncMock(return_value=True))
    monkeypatch.setattr(user_schema, "_open_comment_list_with_safe_retry", AsyncMock(return_value=True))
    monkeypatch.setattr(user_schema, "_wait_for_searchable_comment_surface", AsyncMock(return_value=True))
    monkeypatch.setattr(user_schema, "_find_main_anchors_batch", fake_batch)
    monkeypatch.setattr(user_schema, "_find_main_anchor", fail_single_search)
    monkeypatch.setattr(user_schema, "_prepare_user_schema_http_session", fake_prepare)
    monkeypatch.setattr(user_schema, "_publish_reply_http_first_if_eligible", fake_http)
    monkeypatch.setattr(user_schema, "_restore_comment_list_position_for_anchor", fail_restore)
    monkeypatch.setattr(user_schema, "publish_reply_hybrid", fail_dom_publish)
    monkeypatch.setattr(user_schema.asyncio, "sleep", fake_sleep)
    monkeypatch.setattr(user_schema, "_register_runtime_success", lambda *_args, **_kwargs: None)

    done_parent_keys = set()
    result = await user_schema._reply_with_variant_on_all_mains(
        SimpleNamespace(),
        SimpleNamespace(id=77, proxy=None),
        "https://www.tiktok.com/@demo/video/1234567890123456789",
        mains,
        "reply text",
        variant_index=1,
        correlation_id="task_http_reply",
        already_replied_parent_keys=done_parent_keys,
    )

    assert http_calls == [("reply text", parent_cid, f"task_http_reply_to_{parent_cid[:20]}")]
    assert len(result.replies) == 1
    assert result.replies[0].success is True
    assert result.replies[0].method == "http_user_schema:network_exact"
    assert result.replies[0].parent_comment_id == parent_cid
    assert parent_cid in result.attempted_parent_keys
    assert parent_cid in done_parent_keys


@pytest.mark.unit
@pytest.mark.asyncio
async def test_restore_comment_list_position_uses_current_batch_markers(monkeypatch):
    from executor import user_schema

    calls = []

    class _Page:
        async def evaluate(self, script, target_top):
            calls.append((script, target_top))
            return True

    monkeypatch.setattr(user_schema.asyncio, "sleep", AsyncMock(return_value=None))

    anchor = user_schema.MainAnchor(
        matched_text="main text",
        parent_comment_id=None,
        scroll_top=321.5,
    )

    await user_schema._restore_comment_list_position_for_anchor(
        _Page(),
        anchor,
        correlation_id="restore_marker_test",
    )

    assert len(calls) == 1
    script, target_top = calls[0]
    assert target_top == 321.5
    assert '[data-smart-comment-list-batch-scroll="1"]' in script
    assert '[data-smart-comment-list-batch-scope="1"]' in script
    assert '[data-smart-id="comment-list-batch-scroll"]' in script


@pytest.mark.unit
@pytest.mark.asyncio
async def test_reply_uses_dom_only_when_network_first_disabled(monkeypatch):
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

    parent_cid = "7641761192486194454"
    mains = [
        user_schema.PublishedMain(
            text="main one",
            comment_id=parent_cid,
            network_confirmed=True,
            local_confirmed=True,
            method="hybrid_dom",
            account_id=10,
            tiktok_username=None,
        )
    ]
    dom_calls = []
    restore_calls = []

    async def fake_create_page_for_account(_account):
        return FakeContext(), FakePage()

    async def fake_login_ready(*_args, **_kwargs):
        return True, None

    async def fake_batch(_page, main_candidates, **_kwargs):
        return [
            user_schema.MainAnchor(
                matched_text="main one",
                parent_comment_id=parent_cid,
                open_text="main one visible",
                found_by="id",
                id_verified=True,
                source_main=main_candidates[0],
            )
        ], {}

    async def fail_prepare(*_args, **_kwargs):
        raise AssertionError("HTTP session must not be prepared when network-first is disabled")

    async def fail_http(*_args, **_kwargs):
        raise AssertionError("HTTP reply publish must not run when network-first is disabled")

    async def fake_restore(_page, anchor, **_kwargs):
        restore_calls.append(anchor.parent_comment_id)

    async def fake_dom_publish(*_args, **kwargs):
        dom_calls.append(kwargs)
        return PublishResult(
            status="success",
            comment_id="reply-dom-1",
            network_captured=True,
            dom_strategy="reply-dom",
        )

    async def fake_sleep(_seconds):
        return None

    monkeypatch.setattr(user_schema._settings, "TIKTOK_NETWORK_FIRST", False, raising=False)
    monkeypatch.setattr(user_schema._settings, "TIKTOK_NETWORK_IMPL_READY", True, raising=False)
    monkeypatch.setattr(user_schema, "create_page_for_account", fake_create_page_for_account)
    monkeypatch.setattr(user_schema, "_ensure_account_login_ready", fake_login_ready)
    monkeypatch.setattr(user_schema, "needs_enrichment", lambda _account: False)
    monkeypatch.setattr(user_schema, "_ensure_page_on_target_media", AsyncMock(return_value=True))
    monkeypatch.setattr(user_schema, "_open_comment_list_with_safe_retry", AsyncMock(return_value=True))
    monkeypatch.setattr(user_schema, "_wait_for_searchable_comment_surface", AsyncMock(return_value=True))
    monkeypatch.setattr(user_schema, "_find_main_anchors_batch", fake_batch)
    monkeypatch.setattr(user_schema, "_find_main_anchor", AsyncMock(side_effect=AssertionError("single search should not run")))
    monkeypatch.setattr(user_schema, "_prepare_user_schema_http_session", fail_prepare)
    monkeypatch.setattr(user_schema, "_publish_reply_http_first_if_eligible", fail_http)
    monkeypatch.setattr(user_schema, "_restore_comment_list_position_for_anchor", fake_restore)
    monkeypatch.setattr(user_schema, "_should_like_parent_before_reply", lambda _anchor: False)
    monkeypatch.setattr(user_schema, "publish_reply_hybrid", fake_dom_publish)
    monkeypatch.setattr(user_schema.asyncio, "sleep", fake_sleep)
    monkeypatch.setattr(user_schema, "_register_runtime_success", lambda *_args, **_kwargs: None)

    done_parent_keys = set()
    result = await user_schema._reply_with_variant_on_all_mains(
        SimpleNamespace(),
        SimpleNamespace(id=77, proxy=None),
        "https://www.tiktok.com/@demo/video/1234567890123456789",
        mains,
        "reply text",
        variant_index=1,
        correlation_id="task_dom_only_reply",
        already_replied_parent_keys=done_parent_keys,
    )

    assert restore_calls == [parent_cid]
    assert len(dom_calls) == 1
    assert dom_calls[0]["parent_comment_id"] == parent_cid
    assert dom_calls[0]["comments_already_open"] is True
    assert len(result.replies) == 1
    assert result.replies[0].success is True
    assert result.replies[0].method == "reply-dom"
    assert parent_cid in result.attempted_parent_keys
    assert parent_cid in done_parent_keys


@pytest.mark.unit
@pytest.mark.asyncio
async def test_reply_http_parent_unconfirmed_is_not_retried_via_dom(monkeypatch):
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

    parent_cid = "7641761192486194454"
    mains = [
        user_schema.PublishedMain(
            text="main one",
            comment_id=parent_cid,
            network_confirmed=True,
            local_confirmed=True,
            method="http_user_schema",
            account_id=10,
            tiktok_username=None,
        )
    ]
    http_session = object()
    http_calls = []
    runtime_failures = []

    async def fake_create_page_for_account(_account):
        return FakeContext(), FakePage()

    async def fake_login_ready(*_args, **_kwargs):
        return True, None

    async def fake_batch(_page, main_candidates, **_kwargs):
        return [
            user_schema.MainAnchor(
                matched_text="main one",
                parent_comment_id=parent_cid,
                open_text="main one visible",
                found_by="id",
                id_verified=True,
                source_main=main_candidates[0],
            )
        ], {}

    async def fake_prepare(*_args, **_kwargs):
        return http_session

    async def fake_http(session, reply_text, *, parent_comment_id, correlation_id):
        assert session is http_session
        http_calls.append((reply_text, parent_comment_id, correlation_id))
        return PublishResult(
            status="failed",
            comment_id="7641761192486194455",
            error_reason="reply_parent_unconfirmed_after_submit",
            network_captured=True,
            network_request_attempted=True,
            dom_strategy="http_user_schema:network_missing_parent",
            post_was_enabled_at_click=True,
        )

    async def fail_dom_publish(*_args, **_kwargs):
        raise AssertionError("DOM reply fallback must not run after submitted HTTP reply")

    async def fail_restore(*_args, **_kwargs):
        raise AssertionError("submitted HTTP reply should not need DOM anchor restoration")

    async def fake_verify_reply(*_args, **_kwargs):
        return False

    async def fake_sleep(_seconds):
        return None

    monkeypatch.setattr(user_schema._settings, "TIKTOK_NETWORK_FIRST", True, raising=False)
    monkeypatch.setattr(user_schema._settings, "TIKTOK_NETWORK_IMPL_READY", True, raising=False)
    monkeypatch.setattr(user_schema, "create_page_for_account", fake_create_page_for_account)
    monkeypatch.setattr(user_schema, "_ensure_account_login_ready", fake_login_ready)
    monkeypatch.setattr(user_schema, "needs_enrichment", lambda _account: False)
    monkeypatch.setattr(user_schema, "_ensure_page_on_target_media", AsyncMock(return_value=True))
    monkeypatch.setattr(user_schema, "_open_comment_list_with_safe_retry", AsyncMock(return_value=True))
    monkeypatch.setattr(user_schema, "_wait_for_searchable_comment_surface", AsyncMock(return_value=True))
    monkeypatch.setattr(user_schema, "_find_main_anchors_batch", fake_batch)
    monkeypatch.setattr(user_schema, "_find_main_anchor", AsyncMock(side_effect=AssertionError("single search should not run")))
    monkeypatch.setattr(user_schema, "_prepare_user_schema_http_session", fake_prepare)
    monkeypatch.setattr(user_schema, "_publish_reply_http_first_if_eligible", fake_http)
    monkeypatch.setattr(user_schema, "_verify_reply_visible_near_parent", fake_verify_reply)
    monkeypatch.setattr(user_schema, "_restore_comment_list_position_for_anchor", fail_restore)
    monkeypatch.setattr(user_schema, "publish_reply_hybrid", fail_dom_publish)
    monkeypatch.setattr(user_schema.asyncio, "sleep", fake_sleep)
    monkeypatch.setattr(user_schema, "_register_runtime_success", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(
        user_schema,
        "_register_runtime_failure",
        lambda _db, account, reason: runtime_failures.append((account.id, reason)),
    )

    done_parent_keys = set()
    result = await user_schema._reply_with_variant_on_all_mains(
        SimpleNamespace(),
        SimpleNamespace(id=77, proxy=None),
        "https://www.tiktok.com/@demo/video/1234567890123456789",
        mains,
        "reply text",
        variant_index=1,
        correlation_id="task_http_reply_unconfirmed",
        already_replied_parent_keys=done_parent_keys,
    )

    assert http_calls == [("reply text", parent_cid, f"task_http_reply_unconfirmed_to_{parent_cid[:20]}")]
    assert len(result.replies) == 1
    assert result.replies[0].success is False
    assert result.replies[0].submitted_unconfirmed is True
    assert result.replies[0].counts_as_submitted is False
    assert result.replies[0].reply_confirmation == "submitted_unconfirmed"
    assert result.replies[0].error == "reply_parent_unconfirmed_after_submit"
    assert result.replies[0].parent_comment_id == parent_cid
    assert parent_cid in result.attempted_parent_keys
    assert parent_cid not in done_parent_keys
    assert runtime_failures == []


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
    monkeypatch.setattr(
        user_schema,
        "_publish_reply_http_first_if_eligible",
        AsyncMock(side_effect=AssertionError("HTTP must not run for text-only parent match")),
    )
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
            composer_clean_after_submit=True,
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
async def test_reply_batch_prioritizes_visible_screen_order_over_template_order(monkeypatch):
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
                matched_text="main two",
                parent_comment_id="cid-2",
                open_text="main two visible",
                found_by="text",
                scroll=0,
                position=0,
                scroll_top=0,
                source_main=main_candidates[1],
            ),
            user_schema.MainAnchor(
                matched_text="main one",
                parent_comment_id="cid-1",
                open_text="main one visible",
                found_by="text",
                scroll=0,
                position=12,
                scroll_top=0,
                source_main=main_candidates[0],
            ),
        ], {}

    async def fail_single_search(*_args, **_kwargs):
        raise AssertionError("single parent search should not run when batch found all parents")

    publish_calls = []

    async def fake_publish_reply(*_args, **kwargs):
        publish_calls.append(kwargs)
        return PublishResult(
            status="success",
            comment_id=f"reply-{len(publish_calls)}",
            network_captured=True,
            dom_strategy="reply-dom",
            composer_clean_after_submit=True,
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
    monkeypatch.setattr(user_schema, "_restore_comment_list_position_for_anchor", AsyncMock(return_value=None))
    monkeypatch.setattr(user_schema, "_should_like_parent_before_reply", lambda _anchor, **_kwargs: False)
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

    assert [out.parent_comment_id for out in result.replies] == ["cid-2", "cid-1"]
    assert [call["parent_comment_text"] for call in publish_calls] == [
        "main two visible",
        "main one visible",
    ]


@pytest.mark.unit
@pytest.mark.asyncio
async def test_reply_does_not_like_parent_before_failed_publish(monkeypatch):
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

    main = user_schema.PublishedMain(
        text="main one",
        comment_id="cid-1",
        network_confirmed=True,
        local_confirmed=True,
        method="hybrid_dom",
        account_id=10,
        tiktok_username=None,
    )

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
                anchor_token="anchor-1",
                source_main=main_candidates[0],
            )
        ], {}

    async def fake_publish_reply(*_args, **_kwargs):
        return PublishResult(
            status="dom_error",
            error_reason="reply_box_open_timeout",
            dom_strategy="reply-dom",
        )

    async def fail_like(*_args, **_kwargs):
        raise AssertionError("parent like must not run when the reply was not submitted")

    async def fake_sleep(_seconds):
        return None

    monkeypatch.setattr(user_schema, "create_page_for_account", fake_create_page_for_account)
    monkeypatch.setattr(user_schema, "_ensure_account_login_ready", fake_login_ready)
    monkeypatch.setattr(user_schema, "needs_enrichment", lambda _account: False)
    monkeypatch.setattr(user_schema, "_ensure_page_on_target_media", AsyncMock(return_value=True))
    monkeypatch.setattr(user_schema, "_open_comment_list_with_safe_retry", AsyncMock(return_value=True))
    monkeypatch.setattr(user_schema, "_find_main_anchors_batch", fake_batch)
    monkeypatch.setattr(user_schema, "_find_main_anchor", AsyncMock(side_effect=AssertionError("single search should not run")))
    monkeypatch.setattr(user_schema, "_restore_comment_list_position_for_anchor", AsyncMock(return_value=None))
    monkeypatch.setattr(user_schema, "_should_like_parent_before_reply", lambda _anchor, **_kwargs: True)
    monkeypatch.setattr(user_schema, "like_parent_comment_if_safe", fail_like)
    monkeypatch.setattr(user_schema, "publish_reply_hybrid", fake_publish_reply)
    monkeypatch.setattr(user_schema.asyncio, "sleep", fake_sleep)
    monkeypatch.setattr(user_schema, "_register_runtime_failure", lambda *_args, **_kwargs: None)

    result = await user_schema._reply_with_variant_on_all_mains(
        SimpleNamespace(),
        SimpleNamespace(id=77, proxy=None),
        "https://www.tiktok.com/@demo/video/1234567890123456789",
        [main],
        "reply text",
        variant_index=1,
        correlation_id="task_reply_no_pre_like",
    )

    assert len(result.replies) == 1
    assert result.replies[0].success is False
    assert result.replies[0].submitted_unconfirmed is False


@pytest.mark.unit
@pytest.mark.asyncio
async def test_parallel_reply_defers_parent_likes_until_after_cached_parent_pass(monkeypatch):
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
    sequence = []

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
                anchor_token="anchor-1",
                source_main=main_candidates[0],
            ),
            user_schema.MainAnchor(
                matched_text="main two",
                parent_comment_id="cid-2",
                open_text="main two visible",
                found_by="text",
                anchor_token="anchor-2",
                source_main=main_candidates[1],
            ),
        ], {}

    async def fake_publish_reply(*_args, **kwargs):
        sequence.append(f"publish:{kwargs.get('parent_comment_text')}")
        return PublishResult(
            status="success",
            comment_id=f"reply-{len(sequence)}",
            network_captured=True,
            network_request_attempted=True,
            dom_strategy="reply-dom",
            post_was_enabled_at_click=True,
            composer_clean_after_submit=True,
        )

    async def fake_like(*_args, **kwargs):
        sequence.append(f"like:{kwargs.get('parent_comment_text')}")
        return SimpleNamespace(success=True, reason=None, strategy="test-like")

    async def fake_sleep(_seconds):
        return None

    monkeypatch.setattr(user_schema, "create_page_for_account", fake_create_page_for_account)
    monkeypatch.setattr(user_schema, "_ensure_account_login_ready", fake_login_ready)
    monkeypatch.setattr(user_schema, "needs_enrichment", lambda _account: False)
    monkeypatch.setattr(user_schema, "_ensure_page_on_target_media", AsyncMock(return_value=True))
    monkeypatch.setattr(user_schema, "_open_comment_list_with_safe_retry", AsyncMock(return_value=True))
    monkeypatch.setattr(user_schema, "_wait_for_searchable_comment_surface", AsyncMock(return_value=True))
    monkeypatch.setattr(user_schema, "_find_main_anchors_batch", fake_batch)
    monkeypatch.setattr(user_schema, "_find_main_anchor", AsyncMock(side_effect=AssertionError("single search should not run")))
    monkeypatch.setattr(user_schema, "_restore_comment_list_position_for_anchor", AsyncMock(return_value=None))
    monkeypatch.setattr(user_schema, "_should_like_parent_before_reply", lambda _anchor, **_kwargs: True)
    monkeypatch.setattr(user_schema, "like_parent_comment_if_safe", fake_like)
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
        correlation_id="task_1_parallel_reply_v1_a1_acc77",
    )

    assert [out.parent_comment_id for out in result.replies] == ["cid-1", "cid-2"]
    assert sequence == [
        "publish:main one visible",
        "publish:main two visible",
        "like:main one visible",
        "like:main two visible",
    ]


@pytest.mark.unit
@pytest.mark.asyncio
async def test_reply_publishes_cached_batch_anchors_after_search_deadline(monkeypatch):
    from executor import user_schema
    from executor.publish_action import PublishResult

    class FakePage:
        url = "https://www.tiktok.com/@demo/video/1234567890123456789"

        def is_closed(self):
            return False

        async def evaluate(self, _script, args):
            token, expected = args
            text_by_token = {
                "anchor-1": "main one visible",
                "anchor-2": "main two visible",
            }
            text = text_by_token.get(token, "")
            return {"ok": bool(expected and expected in text), "reason": "ok"}

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

    clock = {"now": 0.0}

    def fake_monotonic():
        return clock["now"]

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
                anchor_token="anchor-1",
                source_main=main_candidates[0],
            ),
            user_schema.MainAnchor(
                matched_text="main two",
                parent_comment_id="cid-2",
                open_text="main two visible",
                found_by="text",
                anchor_token="anchor-2",
                source_main=main_candidates[1],
            ),
        ], {}

    async def fail_single_search(*_args, **_kwargs):
        raise AssertionError("cached batch anchors should not need single search")

    async def fake_restore(*_args, **_kwargs):
        return None

    publish_calls = []

    async def fake_publish_reply(*_args, **kwargs):
        publish_calls.append(kwargs)
        if len(publish_calls) == 1:
            clock["now"] += user_schema._REPLY_MAIN_SEARCH_TIMEOUT_SECONDS + 5.0
        return PublishResult(
            status="success",
            comment_id=f"reply-{len(publish_calls)}",
            network_captured=True,
            dom_strategy="reply-dom",
            composer_clean_after_submit=True,
        )

    async def fake_sleep(seconds):
        clock["now"] += float(seconds)

    monkeypatch.setattr(user_schema.time, "monotonic", fake_monotonic)
    monkeypatch.setattr(user_schema, "create_page_for_account", fake_create_page_for_account)
    monkeypatch.setattr(user_schema, "_ensure_account_login_ready", fake_login_ready)
    monkeypatch.setattr(user_schema, "needs_enrichment", lambda _account: False)
    monkeypatch.setattr(user_schema, "_ensure_page_on_target_media", AsyncMock(return_value=True))
    monkeypatch.setattr(user_schema, "_open_comment_list_with_safe_retry", AsyncMock(return_value=True))
    monkeypatch.setattr(user_schema, "_wait_for_searchable_comment_surface", AsyncMock(return_value=True))
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

    assert [out.parent_comment_id for out in result.replies] == ["cid-1", "cid-2"]
    assert [call["parent_comment_text"] for call in publish_calls] == [
        "main one visible",
        "main two visible",
    ]
    assert all(out.success for out in result.replies)


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
    assert info["found_count"] == 2
    assert info["missing_count"] == 0
    assert info["matched_indexes"] == [0, 1]
    assert info["missing_indexes"] == []
    assert [anchor.matched_text for anchor in anchors] == ["main two", "main one"]
    assert anchors[0].found_by == "id"
    assert anchors[0].id_verified is True
    assert anchors[0].source_main is mains[1]
    assert anchors[0].parent_key == "cid-2"
    assert anchors[0].scroll_top == 100
    assert anchors[1].found_by == "text"


@pytest.mark.unit
@pytest.mark.asyncio
async def test_find_main_anchors_batch_keeps_partial_hits_on_timeout(monkeypatch):
    from executor import user_schema

    mains = [
        user_schema.PublishedMain(
            text="main one",
            comment_id=None,
            network_confirmed=True,
            local_confirmed=True,
            method="hybrid_dom",
            account_id=10,
            tiktok_username="author_one",
        ),
        user_schema.PublishedMain(
            text="main two",
            comment_id=None,
            network_confirmed=True,
            local_confirmed=True,
            method="hybrid_dom",
            account_id=11,
            tiktok_username="author_two",
        ),
    ]

    async def fake_batch(**kwargs):
        sink = kwargs["partial_found_sink"]
        sink[0] = {
            "raw_text": "main one visible",
            "comment_id": "fresh-cid-1",
            "matched_by": "text_author",
            "author": "author_one",
            "scroll": 0,
            "position": 1,
            "scroll_top": 0,
            "anchor_token": "anchor-one",
        }
        await asyncio.sleep(1.2)
        return dict(sink), {"found_count": len(sink)}

    monkeypatch.setattr(user_schema, "find_comments_in_list_batch", fake_batch)

    anchors, info = await user_schema._find_main_anchors_batch(
        SimpleNamespace(),
        mains,
        timeout_seconds=0.01,
        require_author_match=True,
    )

    assert len(anchors) == 1
    assert anchors[0].matched_text == "main one"
    assert anchors[0].parent_comment_id == "fresh-cid-1"
    assert anchors[0].found_by == "text_author"
    assert info["error"] == "batch_search_timeout_partial"
    assert info["matched_indexes"] == [0]
    assert info["missing_indexes"] == [1]
    assert info["missing_targets"][0]["text_preview"] == "main two"


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
            composer_clean_after_submit=True,
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
async def test_reply_confident_batch_miss_skips_redundant_single_search(monkeypatch):
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
            account_id=11,
            tiktok_username=None,
        ),
    ]
    batch_calls = []

    async def fake_create_page_for_account(_account):
        return FakeContext(), FakePage()

    async def fake_login_ready(*_args, **_kwargs):
        return True, None

    async def fake_batch(_page, main_candidates, **_kwargs):
        batch_calls.append([m.text for m in main_candidates])
        return [
            user_schema.MainAnchor(
                matched_text="main one",
                parent_comment_id="cid-1",
                open_text="main one visible",
                found_by="text",
                scroll_top=120,
                source_main=main_candidates[0],
            )
        ], {
            "found_count": 1,
            "missing_count": 1,
            "max_seen_rows": 8,
            "last_row_count": 4,
            "unique_rows_seen": 8,
        }

    async def fail_single(*_args, **_kwargs):
        raise AssertionError("single parent search should be skipped after confident batch miss")

    publish_calls = []

    async def fake_publish_reply(*_args, **kwargs):
        publish_calls.append(kwargs)
        return PublishResult(
            status="success",
            comment_id="reply-1",
            network_captured=True,
            dom_strategy="reply-dom",
        )

    monkeypatch.setattr(user_schema, "create_page_for_account", fake_create_page_for_account)
    monkeypatch.setattr(user_schema, "_ensure_account_login_ready", fake_login_ready)
    monkeypatch.setattr(user_schema, "needs_enrichment", lambda _account: False)
    monkeypatch.setattr(user_schema, "_ensure_page_on_target_media", AsyncMock(return_value=True))
    monkeypatch.setattr(user_schema, "_open_comment_list_with_safe_retry", AsyncMock(return_value=True))
    monkeypatch.setattr(user_schema, "_find_main_anchors_batch", fake_batch)
    monkeypatch.setattr(user_schema, "_find_main_anchor", fail_single)
    monkeypatch.setattr(user_schema, "publish_reply_hybrid", fake_publish_reply)
    monkeypatch.setattr(user_schema.asyncio, "sleep", AsyncMock(return_value=None))
    monkeypatch.setattr(user_schema, "_register_runtime_success", lambda *_args, **_kwargs: None)

    result = await user_schema._reply_with_variant_on_all_mains(
        SimpleNamespace(),
        SimpleNamespace(id=77, proxy=None),
        "https://www.tiktok.com/@demo/video/1234567890123456789",
        mains,
        "reply text",
        variant_index=1,
        correlation_id="task_batch_confident_miss",
    )

    assert batch_calls == [["main one", "main two"]]
    assert [out.parent_comment_id for out in result.replies] == ["cid-1"]
    assert result.found_mains[0].comment_id == "cid-1"
    assert publish_calls[0]["parent_comment_text"] == "main one visible"


@pytest.mark.unit
@pytest.mark.asyncio
async def test_reply_parent_search_uses_shared_cached_comment_id_before_batch(monkeypatch):
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

    main_without_cid = user_schema.PublishedMain(
        text="main one",
        comment_id=None,
        network_confirmed=True,
        local_confirmed=True,
        method="hybrid_dom",
        account_id=10,
        tiktok_username=None,
    )
    cached_main = user_schema.PublishedMain(
        text="main one",
        comment_id="cached-cid-1",
        network_confirmed=True,
        local_confirmed=True,
        method="hybrid_dom",
        account_id=10,
        tiktok_username=None,
    )
    shared_cache = user_schema._SharedParentSearchCache()
    await user_schema._remember_shared_parent_search_cache(shared_cache, [cached_main])
    batch_comment_ids = []

    async def fake_create_page_for_account(_account):
        return FakeContext(), FakePage()

    async def fake_login_ready(*_args, **_kwargs):
        return True, None

    async def fake_batch(_page, main_candidates, **_kwargs):
        batch_comment_ids.append([m.comment_id for m in main_candidates])
        return [
            user_schema.MainAnchor(
                matched_text="main one",
                parent_comment_id="cached-cid-1",
                open_text="main one visible",
                found_by="id",
                id_verified=True,
                scroll_top=0,
                source_main=main_candidates[0],
            )
        ], {"found_count": 1, "max_seen_rows": 1, "unique_rows_seen": 1}

    async def fail_single(*_args, **_kwargs):
        raise AssertionError("batch should find the cached id without single fallback")

    async def fake_publish_reply(*_args, **_kwargs):
        return PublishResult(
            status="success",
            comment_id="reply-1",
            network_captured=True,
            dom_strategy="reply-dom",
        )

    monkeypatch.setattr(user_schema, "create_page_for_account", fake_create_page_for_account)
    monkeypatch.setattr(user_schema, "_ensure_account_login_ready", fake_login_ready)
    monkeypatch.setattr(user_schema, "needs_enrichment", lambda _account: False)
    monkeypatch.setattr(user_schema, "_ensure_page_on_target_media", AsyncMock(return_value=True))
    monkeypatch.setattr(user_schema, "_open_comment_list_with_safe_retry", AsyncMock(return_value=True))
    monkeypatch.setattr(user_schema, "_find_main_anchors_batch", fake_batch)
    monkeypatch.setattr(user_schema, "_find_main_anchor", fail_single)
    monkeypatch.setattr(user_schema, "publish_reply_hybrid", fake_publish_reply)
    monkeypatch.setattr(user_schema.asyncio, "sleep", AsyncMock(return_value=None))
    monkeypatch.setattr(user_schema, "_register_runtime_success", lambda *_args, **_kwargs: None)

    result = await user_schema._reply_with_variant_on_all_mains(
        SimpleNamespace(),
        SimpleNamespace(id=77, proxy=None),
        "https://www.tiktok.com/@demo/video/1234567890123456789",
        [main_without_cid],
        "reply text",
        variant_index=1,
        correlation_id="task_shared_parent_cache",
        shared_parent_cache=shared_cache,
    )

    assert batch_comment_ids == [["cached-cid-1"]]
    assert result.replies[0].parent_comment_id == "cached-cid-1"


@pytest.mark.unit
@pytest.mark.asyncio
async def test_execute_keeps_all_active_mains_after_partial_reply_found(monkeypatch):
    from executor import user_schema

    main_account = SimpleNamespace(id=101, proxy=None)
    reply_accounts = [
        SimpleNamespace(id=201, proxy=None),
        SimpleNamespace(id=202, proxy=None),
        SimpleNamespace(id=203, proxy=None),
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
        if variant_idx == 1 and account.id == 201 and len(main_candidates) > 2:
            found = [main_candidates[0], main_candidates[2]]
        elif variant_idx == 1 and account.id == 202 and len(main_candidates) > 1:
            found = [main_candidates[1]]
        else:
            found = list(main_candidates)
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

    assert [(variant, account) for variant, account, _texts, _reply in calls] == [
        (1, 201),
        (1, 202),
        (2, 203),
    ]
    assert excludes_seen[0] == [main_account.id]
    assert excludes_seen[1] == [main_account.id, 201]
    assert excludes_seen[2] == [main_account.id, 201, 202]
    assert calls[0][2] == ["main one", "main two", "main three"]
    assert calls[1][2] == ["main one", "main two", "main three"]
    assert calls[2][2] == ["main one", "main two", "main three"]
    assert [reply.variant_index for reply in result.replies] == [1, 1, 1, 2, 2, 2]
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
                saw_comment_rows=True,
                parent_search_max_seen_rows=3,
                parent_search_last_row_count=3,
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
        found = list(main_candidates)
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
    assert calls[3][2] == ["fallback-1", "fallback-2", "fallback-3"]
    assert [reply.variant_index for reply in result.replies] == [1, 1, 1, 2, 2, 2]
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
        found = list(main_candidates)
        return user_schema.ReplyAttemptResult(
            replies=[
                user_schema.ReplyOutcome(
                    text=args[4],
                    variant_index=kwargs["variant_index"],
                    matched_main_text=main.text,
                    parent_comment_id=main.comment_id,
                    success=True,
                    method="hybrid_dom",
                    error=None,
                    account_id=reply_account.id,
                    attempted=True,
                )
                for main in found
            ],
            found_mains=found,
            attempted_parent_keys={
                main.comment_id or user_schema._dedup_key(main.text)
                for main in found
            },
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
    assert [reply.success for reply in result.replies] == [True, True, True]
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
        found = list(main_candidates)
        return user_schema.ReplyAttemptResult(
            replies=[
                user_schema.ReplyOutcome(
                    text=args[4],
                    variant_index=kwargs["variant_index"],
                    matched_main_text=main.text,
                    parent_comment_id=main.comment_id,
                    success=True,
                    method="hybrid_dom",
                    error=None,
                    account_id=reply_account.id,
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
    assert [reply.success for reply in result.replies] == [True, True, True]


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
def test_main_action_ready_requires_comment_specific_publish_input():
    from executor import user_schema

    generic_textbox = {
        "input_visible": True,
        "comment_input_visible": False,
        "publish_input_candidate_visible": False,
    }
    publish_candidate = {
        **generic_textbox,
        "publish_input_candidate_visible": True,
    }
    comment_input = {
        **generic_textbox,
        "comment_input_visible": True,
    }

    assert user_schema._comment_surface_publish_ready(generic_textbox) is False
    assert user_schema._comment_surface_publish_ready(publish_candidate) is True
    assert user_schema._comment_surface_publish_ready(comment_input) is True


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
async def test_parallel_main_does_not_share_drifted_resolved_url_after_failure(monkeypatch):
    from db import database as db_database
    from executor import browser_pool, user_schema

    original_url = "https://www.tiktok.com/@demo/video/1234567890123456789"
    drifted_url = "https://www.tiktok.com/@other/video/9999999999999999999"
    accounts = [SimpleNamespace(id=101, proxy=None), SimpleNamespace(id=102, proxy=None)]
    call_urls = []

    @contextmanager
    def fake_db_context():
        yield SimpleNamespace(no_autoflush=SimpleNamespace())

    def fake_get_main(_db, *, exclude_ids=None, **_kwargs):
        excluded = set(exclude_ids or [])
        for account in accounts:
            if account.id not in excluded:
                return account
        return None

    async def fake_try_main(_db, account, video_url, texts, **kwargs):
        call_urls.append(video_url)
        holder = kwargs.get("resolved_video_url_holder")
        if account.id == 101:
            if holder is not None:
                holder["video_url"] = drifted_url
            return [], "url_drift_to_other_video"
        return [
            user_schema.PublishedMain(
                text=texts[0],
                comment_id="main-102-1",
                network_confirmed=True,
                local_confirmed=True,
                method="hybrid_dom",
                account_id=account.id,
                tiktok_username="main_user",
            )
        ], None

    monkeypatch.setattr(db_database, "get_sync_db_context", fake_db_context)
    monkeypatch.setattr(user_schema, "get_free_account_for_main_comment", fake_get_main)
    monkeypatch.setattr(user_schema, "_try_main_account", fake_try_main)
    monkeypatch.setattr(user_schema, "release_main_account_reservation", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(user_schema, "_maybe_order_likes", AsyncMock(return_value=None))
    monkeypatch.setattr(browser_pool, "ensure_thread_browser_pool_capacity", lambda _min_browsers: True)
    monkeypatch.setattr(browser_pool, "close_browser_pool", AsyncMock(return_value=None))
    monkeypatch.setattr(user_schema.asyncio, "sleep", AsyncMock(return_value=None))
    monkeypatch.setattr(user_schema._settings, "PARALLEL_DYNAMIC_WORKER_POOL_ENABLED", True, raising=False)
    monkeypatch.setattr(user_schema._settings, "PARALLEL_COMMENTING_TARGET_BROWSERS", 1, raising=False)
    monkeypatch.setattr(user_schema._settings, "PARALLEL_COMMENTING_MAX_BROWSERS", 1, raising=False)
    monkeypatch.setattr(user_schema._settings, "PARALLEL_COMMENTING_MAX_MAIN_ATTEMPTS", 2, raising=False)

    task = SimpleNamespace(settings={"replies": []})
    result = await user_schema.execute_user_schema_parallel(
        SimpleNamespace(),
        task,
        original_url,
        ["main one"],
        273,
        boost_enabled=False,
        boost_quantity=0,
    )

    assert call_urls == [original_url, original_url]
    assert result.published_mains
    assert result.published_mains[0].account_id == 102


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
    selected_main_reserved: set[int] = set()
    selected_main_lock = threading.Lock()

    def fake_get_main(_db, *, exclude_ids=None, **_kwargs):
        excluded = set(exclude_ids or [])
        with selected_main_lock:
            for account in main_accounts:
                if account.id not in excluded and account.id not in selected_main_reserved:
                    selected_main_reserved.add(account.id)
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
    assert capacity_requests == [3, 3]
    assert close_calls == ["close", "close"]


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
async def test_parallel_mode_dynamic_disabled_preserves_three_wide_batch_path(monkeypatch):
    from db import database as db_database
    from executor import browser_pool, user_schema

    @contextmanager
    def fake_db_context():
        yield SimpleNamespace(no_autoflush=SimpleNamespace())

    main_accounts = [SimpleNamespace(id=101 + i, proxy=None) for i in range(3)]
    reply_accounts = [SimpleNamespace(id=201 + i, proxy=None) for i in range(4)]
    capacity_requests = []
    close_calls = []
    reply_calls = []

    def fake_get_main(_db, *, exclude_ids=None, **_kwargs):
        excluded = set(exclude_ids or [])
        return next((account for account in main_accounts if account.id not in excluded), None)

    async def fake_try_main(_db, account, _video_url, texts, **_kwargs):
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
        reply_calls.append((kwargs["variant_index"], account.id, len(main_candidates)))
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

    monkeypatch.setattr(user_schema._settings, "PARALLEL_DYNAMIC_WORKER_POOL_ENABLED", False, raising=False)
    monkeypatch.setattr(user_schema._settings, "PARALLEL_COMMENTING_TARGET_BROWSERS", 10, raising=False)
    monkeypatch.setattr(user_schema._settings, "PARALLEL_COMMENTING_MAX_BROWSERS", 4, raising=False)
    monkeypatch.setattr(user_schema._settings, "PARALLEL_REPLY_START_DELAY_MIN_SECONDS", 0, raising=False)
    monkeypatch.setattr(user_schema._settings, "PARALLEL_REPLY_START_DELAY_MAX_SECONDS", 0, raising=False)
    monkeypatch.setattr(db_database, "get_sync_db_context", fake_db_context)
    monkeypatch.setattr(user_schema, "get_free_account_for_main_comment", fake_get_main)
    monkeypatch.setattr(user_schema, "_try_main_account", fake_try_main)
    monkeypatch.setattr(user_schema, "release_main_account_reservation", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(user_schema, "get_free_account_for_reply", fake_get_reply)
    monkeypatch.setattr(user_schema, "_reply_with_variant_on_all_mains", fake_reply)
    monkeypatch.setattr(user_schema, "_maybe_order_likes", AsyncMock(return_value=None))
    monkeypatch.setattr(browser_pool, "ensure_thread_browser_pool_capacity", lambda value: capacity_requests.append(value))
    monkeypatch.setattr(browser_pool, "close_browser_pool", fake_close_pool)
    monkeypatch.setattr(user_schema.asyncio, "sleep", AsyncMock(return_value=None))

    task = SimpleNamespace(settings={"replies": ["r1", "r2", "r3", "r4"]})
    result = await user_schema.execute_user_schema_parallel(
        SimpleNamespace(),
        task,
        "https://www.tiktok.com/@demo/video/1234567890123456789",
        ["m1", "m2", "m3"],
        512,
        boost_enabled=False,
        boost_quantity=0,
    )

    assert result.error is None
    assert capacity_requests == [3, 3, 3]
    assert close_calls == ["close", "close", "close"]
    assert [variant for variant, _account_id, _count in reply_calls] == [1, 2, 3, 4]


@pytest.mark.unit
def test_reply_visibility_empty_requires_real_comment_depth():
    from executor import user_schema

    base = dict(
        variant_index=1,
        text="reply",
        account_id=10,
        no_main_found=True,
        found_any_main=False,
        error="no_main_found_by_reply_account",
    )

    assert user_schema._reply_result_is_confident_visibility_empty(
        user_schema._ParallelReplyVariantResult(
            **base,
            parent_search_unique_rows_seen=20,
            parent_search_max_seen_rows=20,
        )
    ) is True
    assert user_schema._reply_result_is_confident_visibility_empty(
        user_schema._ParallelReplyVariantResult(
            **base,
            parent_search_unique_rows_seen=5,
            parent_search_max_seen_rows=40,
        )
    ) is True
    assert user_schema._reply_result_is_confident_visibility_empty(
        user_schema._ParallelReplyVariantResult(
            **base,
            parent_search_unique_rows_seen=5,
            parent_search_max_seen_rows=10,
        )
    ) is False
    assert user_schema._reply_result_is_confident_visibility_empty(
        user_schema._ParallelReplyVariantResult(
            **base,
            parent_search_unique_rows_seen=8,
            parent_search_max_seen_rows=8,
            parent_search_last_row_count=8,
            parent_search_exhausted=True,
            parent_search_scroll_container_scrollable=False,
        )
    ) is True
    assert user_schema._reply_result_is_confident_visibility_empty(
        user_schema._ParallelReplyVariantResult(
            **base,
            parent_search_unique_rows_seen=8,
            parent_search_max_seen_rows=8,
            parent_search_last_row_count=8,
            parent_search_exhausted=False,
            parent_search_scroll_container_scrollable=False,
        )
    ) is False
    assert user_schema._reply_result_is_confident_visibility_empty(
        user_schema._ParallelReplyVariantResult(
            **{**base, "found_any_main": True},
            parent_search_unique_rows_seen=20,
            parent_search_max_seen_rows=40,
        )
    ) is False
    assert user_schema._reply_result_is_confident_visibility_empty(
        user_schema._ParallelReplyVariantResult(
            **base,
            parent_search_unique_rows_seen=3,
            parent_search_max_seen_rows=3,
            parent_search_last_row_count=3,
            parent_search_exhausted=True,
            parent_search_scroll_container_scrollable=False,
            parent_search_total_comment_count=3,
            parent_search_scroll_budget=120,
        )
    ) is True
    assert user_schema._reply_result_is_confident_visibility_empty(
        user_schema._ParallelReplyVariantResult(
            **base,
            parent_search_exhausted=True,
            parent_search_scroll_container_scrollable=False,
            parent_search_total_comment_count=0,
            parent_search_scroll_budget=120,
        )
    ) is True
    assert user_schema._reply_result_is_confident_visibility_empty(
        user_schema._ParallelReplyVariantResult(
            **base,
            parent_search_unique_rows_seen=3,
            parent_search_max_seen_rows=3,
            parent_search_last_row_count=3,
            parent_search_exhausted=True,
            parent_search_scroll_container_scrollable=True,
            parent_search_total_comment_count=900,
            parent_search_scroll_budget=25,
        )
    ) is True
    assert user_schema._reply_result_is_confident_visibility_empty(
        user_schema._ParallelReplyVariantResult(
            **base,
            parent_search_unique_rows_seen=3,
            parent_search_max_seen_rows=3,
            parent_search_last_row_count=3,
            parent_search_exhausted=True,
            parent_search_scroll_container_scrollable=False,
            parent_search_total_comment_count=None,
            parent_search_scroll_budget=10,
        )
    ) is True


@pytest.mark.unit
def test_completed_batch_parent_search_does_not_restart_single_full_scan():
    from executor import user_schema

    completed_scan = {
        "missing_count": 1,
        "target_count": 1,
        "found_count": 0,
        "max_seen_rows": 20,
        "last_row_count": 10,
        "unique_rows_seen": 20,
        "search_exhausted": True,
    }

    assert user_schema._batch_parent_search_should_try_single_fallback(completed_scan) is False
    assert user_schema._batch_parent_search_should_try_single_fallback(
        {**completed_scan, "search_exhausted": False}
    ) is True


@pytest.mark.unit
def test_reply_visibility_quorum_tracks_four_accounts_across_proxies():
    from executor import user_schema
    import inspect

    source = inspect.getsource(user_schema.execute_user_schema_parallel)

    assert "reply_visibility_pre_accounts_by_proxy" in source
    assert "reply_visibility_post_accounts_by_proxy" in source
    assert "pre_rotation_by_proxy" in source
    assert "post_rotation_by_proxy" in source
    assert "reply_visibility_empty_accounts" in source
    assert "_REPLY_VISIBILITY_EMPTY_STOP_QUORUM" in source
    assert "len(reply_visibility_empty_accounts) >= _REPLY_VISIBILITY_EMPTY_STOP_QUORUM" in source
    assert "remaining_main_comments_not_visible_after_four_accounts" in source
    assert "visibility_actions: List[Tuple[str, _ParallelReplyVariantResult]]" in source
    assert "action_names = [action for action, _item in visibility_actions]" in source
    assert "(item for action, item in visibility_actions if action == \"rotate\")" in source


@pytest.mark.unit
def test_reply_planned_rotation_counts_only_current_attempt_submissions():
    from executor import user_schema
    import inspect

    result_source = inspect.getsource(user_schema._ParallelReplyVariantResult)
    source = inspect.getsource(user_schema.execute_user_schema_parallel)
    helper_source = source.split(
        "async def _record_successful_reply_proxy_completion", 1
    )[1].split("    def _reply_visibility_proxy_key", 1)[0]

    assert "submitted_this_attempt: bool = False" in result_source
    assert "submitted_this_attempt = False" in source
    assert "nonlocal submitted_this_attempt" in source
    assert "submitted_this_attempt = True" in source
    assert (
        "variant_result.submitted_this_attempt = bool(submitted_outcomes) or submitted_this_attempt"
        in source
    )
    assert 'bool(getattr(item, "submitted_this_attempt", False))' in source
    assert "submitted_this_attempt: bool" in helper_source
    assert "any(_reply_outcome_submitted" not in helper_source


@pytest.mark.unit
def test_reply_local_verifier_excludes_open_reply_editor_artifacts():
    from executor import user_schema
    import inspect

    source = inspect.getsource(user_schema._verify_reply_visible_near_parent)

    assert "editorArtifactSelector" in source
    assert "'[data-smart-reply-input=\"1\"]'" in source
    assert "'[data-smart-reply-composer=\"1\"]'" in source
    assert "'[contenteditable=\"true\"]'" in source
    assert "if (isEditorArtifact(node)) return;" in source
    assert "if (!row || isEditorArtifact(row)) return;" in source


@pytest.mark.unit
def test_dynamic_reply_surface_unavailable_breaker_not_blocked_by_historical_main_found():
    from executor import user_schema
    import inspect

    source = inspect.getsource(user_schema.execute_user_schema_parallel)
    marker = "consecutive_surface_unavailable_attempts >= max_surface_unavailable_attempts"
    block = source.split(marker, 1)[1].split("if item.no_main_found:", 1)[0]

    assert "not any_reply_account_found_main" not in block


@pytest.mark.unit
@pytest.mark.asyncio
async def test_parallel_mode_all_reply_accounts_missing_mains_requires_rotation_or_fails(monkeypatch):
    from db import database as db_database
    from executor import browser_pool, user_schema

    @contextmanager
    def fake_db_context():
        yield SimpleNamespace(no_autoflush=SimpleNamespace())

    main_accounts = [SimpleNamespace(id=101 + i, proxy=None) for i in range(3)]
    reply_accounts = [SimpleNamespace(id=201 + i, proxy=None) for i in range(4)]
    fallback_seen = []
    close_calls = []
    selected_main_ids: set[int] = set()
    select_lock = threading.Lock()

    def fake_get_main(_db, *, exclude_ids=None, **_kwargs):
        excluded = set(exclude_ids or [])
        with select_lock:
            candidate = next(
                (
                    account
                    for account in main_accounts
                    if account.id not in excluded and account.id not in selected_main_ids
                ),
                None,
            )
            if candidate is not None:
                selected_main_ids.add(candidate.id)
            return candidate

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
            saw_comment_rows=False,
            parent_search_max_seen_rows=0,
            parent_search_last_row_count=0,
            parent_search_unique_rows_seen=0,
            parent_search_exhausted=True,
            parent_search_scroll_container_scrollable=False,
            parent_search_total_comment_count=0,
            parent_search_scroll_budget=120,
        )

    async def fake_close_pool():
        close_calls.append("close")

    monkeypatch.setattr(user_schema._settings, "PARALLEL_COMMENTING_TARGET_BROWSERS", 3, raising=False)
    monkeypatch.setattr(user_schema._settings, "PARALLEL_COMMENTING_MAX_BROWSERS", 3, raising=False)
    monkeypatch.setattr(db_database, "get_sync_db_context", fake_db_context)
    monkeypatch.setattr(user_schema, "get_free_account_for_main_comment", fake_get_main)
    monkeypatch.setattr(user_schema, "_try_main_account", fake_try_main)
    monkeypatch.setattr(user_schema, "release_main_account_reservation", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(user_schema, "get_free_account_for_reply", fake_get_reply)
    monkeypatch.setattr(user_schema, "_reply_with_variant_on_all_mains", fake_reply)
    monkeypatch.setattr(user_schema, "_maybe_order_likes", AsyncMock(return_value=None))
    monkeypatch.setattr(user_schema._settings, "PARALLEL_REPLY_VARIANT_WORKERS", 1, raising=False)
    monkeypatch.setattr(browser_pool, "ensure_thread_browser_pool_capacity", lambda _min_browsers: None)
    monkeypatch.setattr(browser_pool, "close_browser_pool", fake_close_pool)
    monkeypatch.setattr(user_schema.asyncio, "sleep", AsyncMock(return_value=None))
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

    assert result.error == "remaining_main_comments_not_visible_after_four_accounts"
    assert result.reply_visibility["rotation_attempted"] is True
    assert result.reply_visibility["rotation_error"] == "missing_proxy_for_visibility_rotation"
    assert result.reply_visibility["confident_empty_accounts"] == 4
    assert fallback_seen == [None, None, None, None]
    assert close_calls == ["close", "close"]


@pytest.mark.unit
@pytest.mark.asyncio
async def test_reply_visibility_quorum_still_applies_after_some_mains_were_found(monkeypatch):
    from db import database as db_database
    from executor import browser_pool, user_schema

    @contextmanager
    def fake_db_context():
        yield SimpleNamespace(no_autoflush=SimpleNamespace())

    main_accounts = [SimpleNamespace(id=301 + i, proxy=None) for i in range(2)]
    reply_accounts = [SimpleNamespace(id=401 + i, proxy=None) for i in range(5)]
    selected_main_ids: set[int] = set()
    select_lock = threading.Lock()
    reply_calls = []

    def fake_get_main(_db, *, exclude_ids=None, **_kwargs):
        excluded = set(exclude_ids or [])
        with select_lock:
            candidate = next(
                (
                    account
                    for account in main_accounts
                    if account.id not in excluded and account.id not in selected_main_ids
                ),
                None,
            )
            if candidate is not None:
                selected_main_ids.add(candidate.id)
            return candidate

    async def fake_try_main(_db, account, _video_url, texts, **_kwargs):
        return [
            user_schema.PublishedMain(
                text=text,
                comment_id=f"main-{idx}",
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

    async def fake_reply(_db, account, _video_url, main_candidates, reply_text, **kwargs):
        reply_calls.append((account.id, kwargs["variant_index"]))
        if len(reply_calls) == 1:
            outcomes = [
                user_schema.ReplyOutcome(
                    text=reply_text,
                    variant_index=kwargs["variant_index"],
                    matched_main_text=main.text,
                    parent_comment_id=main.comment_id,
                    success=True,
                    method="unit_local",
                    error=None,
                    account_id=account.id,
                    attempted=True,
                    local_confirmed=True,
                    counts_as_submitted=True,
                )
                for main in main_candidates
            ]
            return user_schema.ReplyAttemptResult(
                replies=outcomes,
                found_mains=list(main_candidates),
                searched=True,
                saw_comment_rows=True,
                parent_search_max_seen_rows=40,
                parent_search_unique_rows_seen=20,
            )
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
            saw_comment_rows=True,
            parent_search_max_seen_rows=40,
            parent_search_last_row_count=40,
            parent_search_unique_rows_seen=20,
        )

    monkeypatch.setattr(user_schema._settings, "PARALLEL_DYNAMIC_WORKER_POOL_ENABLED", False, raising=False)
    monkeypatch.setattr(user_schema._settings, "PARALLEL_COMMENTING_TARGET_BROWSERS", 3, raising=False)
    monkeypatch.setattr(user_schema._settings, "PARALLEL_COMMENTING_MAX_BROWSERS", 3, raising=False)
    monkeypatch.setattr(user_schema._settings, "PARALLEL_REPLY_VARIANT_WORKERS", 1, raising=False)
    monkeypatch.setattr(user_schema._settings, "PARALLEL_REPLY_START_DELAY_MIN_SECONDS", 0, raising=False)
    monkeypatch.setattr(user_schema._settings, "PARALLEL_REPLY_START_DELAY_MAX_SECONDS", 0, raising=False)
    monkeypatch.setattr(db_database, "get_sync_db_context", fake_db_context)
    monkeypatch.setattr(user_schema, "get_free_account_for_main_comment", fake_get_main)
    monkeypatch.setattr(user_schema, "_try_main_account", fake_try_main)
    monkeypatch.setattr(user_schema, "release_main_account_reservation", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(user_schema, "get_free_account_for_reply", fake_get_reply)
    monkeypatch.setattr(user_schema, "_reply_with_variant_on_all_mains", fake_reply)
    monkeypatch.setattr(user_schema, "_maybe_order_likes", AsyncMock(return_value=None))
    monkeypatch.setattr(browser_pool, "ensure_thread_browser_pool_capacity", lambda _min_browsers: None)
    monkeypatch.setattr(browser_pool, "close_browser_pool", AsyncMock(return_value=None))
    monkeypatch.setattr(user_schema.asyncio, "sleep", AsyncMock(return_value=None))

    task = SimpleNamespace(settings={"replies": ["r1", "r2", "r3"]})
    result = await user_schema.execute_user_schema_parallel(
        SimpleNamespace(),
        task,
        "https://www.tiktok.com/@demo/video/1234567890123456789",
        ["m1", "m2"],
        503,
        boost_enabled=False,
        boost_quantity=0,
    )

    assert result.error == "remaining_main_comments_not_visible_after_four_accounts"
    assert result.main_found_by_observer is True
    assert result.reply_visibility["rotation_attempted"] is True
    assert result.reply_visibility["confident_empty_accounts"] == 4
    assert any(outcome.success for outcome in result.replies)


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
    main_events = [event for event in events if event[0] == "main"]
    assert ("main", 101, ("m1", "m2", "m3")) in main_events
    failed_event = next(event for event in main_events if event[1] == 102)
    assert any(event[1] != 102 and event[2] == failed_event[2] for event in main_events)
    assert any(event[2] == ("m2", "m3", "m1") for event in main_events)
    assert any(event[2] == ("m3", "m1", "m2") for event in main_events)
    assert sorted(event[1] for event in main_events) == [101, 102, 103, 104]
    assert events[-1][0] == "reply"
    assert len(result.published_mains) == 9
    assert sorted({main.account_id for main in result.published_mains}) == [101, 103, 104]
    assert sorted(main.text for main in result.published_mains) == ["m1", "m1", "m1", "m2", "m2", "m2", "m3", "m3", "m3"]
    assert close_calls == ["close", "close"]


@pytest.mark.unit
@pytest.mark.asyncio
async def test_parallel_mode_refills_main_worker_while_other_browsers_are_active(monkeypatch):
    from db import database as db_database
    from executor import browser_pool, user_schema

    @contextmanager
    def fake_db_context():
        yield SimpleNamespace(no_autoflush=SimpleNamespace())

    main_accounts = [
        SimpleNamespace(id=101 + i, proxy=f"http://proxy-{i}:1000")
        for i in range(4)
    ]
    events = []
    close_calls = []
    replacement_started = asyncio.Event()
    selected_ids: set[int] = set()
    select_lock = threading.Lock()

    def fake_get_main(_db, *, exclude_ids=None, exclude_proxies=None, **_kwargs):
        excluded = set(exclude_ids or [])
        excluded_proxies = {str(proxy).strip() for proxy in (exclude_proxies or []) if proxy}
        with select_lock:
            candidate = next(
                (
                    account
                    for account in main_accounts
                    if account.id not in excluded
                    and account.id not in selected_ids
                    and str(account.proxy or "").strip() not in excluded_proxies
                ),
                None,
            )
            if candidate is not None:
                selected_ids.add(candidate.id)
            return candidate

    async def fake_try_main(_db, account, _video_url, texts, **_kwargs):
        events.append(("start", account.id, tuple(texts)))
        if account.id == 102:
            events.append(("finish", account.id))
            return [], "captcha_challenge:visible"
        if account.id in {101, 103}:
            await asyncio.wait_for(replacement_started.wait(), timeout=1.0)
        if account.id == 104:
            replacement_started.set()
        events.append(("finish", account.id))
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

    async def fake_close_pool():
        close_calls.append("close")

    monkeypatch.setattr(user_schema._settings, "PARALLEL_COMMENTING_TARGET_BROWSERS", 3, raising=False)
    monkeypatch.setattr(user_schema._settings, "PARALLEL_COMMENTING_MAX_BROWSERS", 3, raising=False)
    monkeypatch.setattr(user_schema._settings, "PARALLEL_MAIN_WORKERS", 3, raising=False)
    monkeypatch.setattr(user_schema._settings, "PARALLEL_COMMENTING_MAX_MAIN_ATTEMPTS", 6, raising=False)
    monkeypatch.setattr(db_database, "get_sync_db_context", fake_db_context)
    monkeypatch.setattr(user_schema, "get_free_account_for_main_comment", fake_get_main)
    monkeypatch.setattr(user_schema, "_try_main_account", fake_try_main)
    monkeypatch.setattr(user_schema, "_record_main_retry_failure", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(user_schema, "release_main_account_reservation", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(user_schema, "_maybe_order_likes", AsyncMock(return_value=None))
    monkeypatch.setattr(browser_pool, "ensure_thread_browser_pool_capacity", lambda _min_browsers: None)
    monkeypatch.setattr(browser_pool, "close_browser_pool", fake_close_pool)

    result = await user_schema.execute_user_schema_parallel(
        SimpleNamespace(),
        SimpleNamespace(settings={"replies": [], "reply_count": 0}),
        "https://www.tiktok.com/@demo/video/1234567890123456789",
        ["m1", "m2", "m3"],
        506,
        boost_enabled=False,
        boost_quantity=0,
    )

    assert result.error is None
    replacement_start_index = events.index(("start", 104, ("m2", "m3", "m1")))
    assert replacement_start_index < events.index(("finish", 101))
    assert replacement_start_index < events.index(("finish", 103))
    assert sorted(main.text for main in result.published_mains) == ["m1", "m1", "m1", "m2", "m2", "m2", "m3", "m3", "m3"]
    assert close_calls == ["close"]


@pytest.mark.unit
@pytest.mark.asyncio
async def test_parallel_mode_times_out_stuck_main_worker_and_refills(monkeypatch):
    from db import database as db_database
    from executor import browser_pool, user_schema

    @contextmanager
    def fake_db_context():
        yield SimpleNamespace(no_autoflush=SimpleNamespace())

    accounts = [
        SimpleNamespace(id=101, proxy="http://proxy-a:1000"),
        SimpleNamespace(id=102, proxy="http://proxy-b:1000"),
        SimpleNamespace(id=103, proxy="http://proxy-c:1000"),
        SimpleNamespace(id=104, proxy="http://proxy-d:1000"),
    ]
    events = []

    def fake_get_main(_db, *, exclude_ids=None, exclude_proxies=None, **_kwargs):
        excluded = set(exclude_ids or [])
        excluded_proxies = {str(proxy).strip() for proxy in (exclude_proxies or []) if proxy}
        for account in accounts:
            if account.id not in excluded and str(account.proxy or "").strip() not in excluded_proxies:
                return account
        return None

    async def fake_try_main(_db, account, _video_url, texts, **_kwargs):
        events.append(("start", account.id, tuple(texts)))
        if account.id == 101:
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                events.append(("cancelled", account.id))
                raise
        events.append(("finish", account.id))
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

    monkeypatch.setattr(user_schema._settings, "PARALLEL_COMMENTING_TARGET_BROWSERS", 1, raising=False)
    monkeypatch.setattr(user_schema._settings, "PARALLEL_COMMENTING_MAX_BROWSERS", 1, raising=False)
    monkeypatch.setattr(user_schema._settings, "PARALLEL_COMMENTING_MAX_MAIN_ATTEMPTS", 2, raising=False)
    monkeypatch.setattr(user_schema._settings, "PARALLEL_MAIN_WORKER_ATTEMPT_TIMEOUT_SECONDS", 0.1, raising=False)
    monkeypatch.setattr(user_schema._settings, "PARALLEL_MAIN_REFILL_POLL_SECONDS", 0.05, raising=False)
    monkeypatch.setattr(db_database, "get_sync_db_context", fake_db_context)
    monkeypatch.setattr(user_schema, "get_free_account_for_main_comment", fake_get_main)
    monkeypatch.setattr(user_schema, "_try_main_account", fake_try_main)
    monkeypatch.setattr(user_schema, "_record_main_retry_failure", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(user_schema, "release_main_account_reservation", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(user_schema, "_maybe_order_likes", AsyncMock(return_value=None))
    monkeypatch.setattr(browser_pool, "ensure_thread_browser_pool_capacity", lambda _min_browsers: None)
    monkeypatch.setattr(browser_pool, "close_browser_pool", AsyncMock(return_value=None))

    result = await user_schema.execute_user_schema_parallel(
        SimpleNamespace(),
        SimpleNamespace(settings={"replies": [], "reply_count": 0}),
        "https://www.tiktok.com/@demo/video/1234567890123456789",
        ["m1"],
        508,
        boost_enabled=False,
        boost_quantity=0,
    )

    assert result.error is None
    assert sorted(main.account_id for main in result.published_mains) == [102, 103, 104]
    assert ("start", 101, ("m1",)) in events
    assert ("cancelled", 101) in events
    assert {event[1] for event in events if event[0] == "finish"} == {102, 103, 104}


@pytest.mark.unit
@pytest.mark.asyncio
async def test_parallel_mode_times_out_stuck_reply_worker_and_refills(monkeypatch):
    from db import database as db_database
    from executor import browser_pool, user_schema

    @contextmanager
    def fake_db_context():
        yield SimpleNamespace(no_autoflush=SimpleNamespace())

    main_accounts = [
        SimpleNamespace(id=101 + i, proxy=f"http://main-proxy-{i}:1000")
        for i in range(3)
    ]
    reply_accounts = [
        SimpleNamespace(id=201, proxy="http://reply-proxy-a:1000"),
        SimpleNamespace(id=202, proxy="http://reply-proxy-b:1000"),
    ]
    events = []

    def fake_get_main(_db, *, exclude_ids=None, **_kwargs):
        excluded = set(exclude_ids or [])
        return next((account for account in main_accounts if account.id not in excluded), None)

    async def fake_try_main(_db, account, _video_url, texts, **_kwargs):
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

    def fake_get_reply(_db, exclude_ids=None, exclude_proxies=None, **_kwargs):
        excluded = set(exclude_ids or [])
        excluded_proxies = {str(proxy).strip() for proxy in (exclude_proxies or []) if proxy}
        return next(
            (
                account
                for account in reply_accounts
                if account.id not in excluded
                and str(account.proxy or "").strip() not in excluded_proxies
            ),
            None,
        )

    async def fake_reply(_db, account, _video_url, main_candidates, reply_text, **kwargs):
        events.append(("start", account.id, kwargs["variant_index"]))
        if account.id == 201:
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                events.append(("cancelled", account.id, kwargs["variant_index"]))
                raise
        events.append(("finish", account.id, kwargs["variant_index"]))
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
                    parent_key=user_schema._main_parent_key(main),
                )
                for main in main_candidates
            ],
            found_mains=list(main_candidates),
            searched=True,
        )

    monkeypatch.setattr(user_schema._settings, "PARALLEL_COMMENTING_TARGET_BROWSERS", 1, raising=False)
    monkeypatch.setattr(user_schema._settings, "PARALLEL_COMMENTING_MAX_BROWSERS", 1, raising=False)
    monkeypatch.setattr(user_schema._settings, "PARALLEL_COMMENTING_MAX_MAIN_ATTEMPTS", 1, raising=False)
    monkeypatch.setattr(user_schema._settings, "PARALLEL_COMMENTING_MAX_REPLY_ATTEMPTS", 2, raising=False)
    monkeypatch.setattr(user_schema._settings, "PARALLEL_REPLY_WORKER_ATTEMPT_TIMEOUT_SECONDS", 0.1, raising=False)
    monkeypatch.setattr(user_schema._settings, "PARALLEL_REPLY_REFILL_POLL_SECONDS", 0.05, raising=False)
    monkeypatch.setattr(user_schema._settings, "PARALLEL_REPLY_START_DELAY_MIN_SECONDS", 0, raising=False)
    monkeypatch.setattr(user_schema._settings, "PARALLEL_REPLY_START_DELAY_MAX_SECONDS", 0, raising=False)
    monkeypatch.setattr(db_database, "get_sync_db_context", fake_db_context)
    monkeypatch.setattr(user_schema, "get_free_account_for_main_comment", fake_get_main)
    monkeypatch.setattr(user_schema, "_try_main_account", fake_try_main)
    monkeypatch.setattr(user_schema, "release_main_account_reservation", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(user_schema, "get_free_account_for_reply", fake_get_reply)
    monkeypatch.setattr(user_schema, "_reply_with_variant_on_all_mains", fake_reply)
    monkeypatch.setattr(user_schema, "_maybe_order_likes", AsyncMock(return_value=None))
    monkeypatch.setattr(browser_pool, "ensure_thread_browser_pool_capacity", lambda _min_browsers: None)
    monkeypatch.setattr(browser_pool, "close_browser_pool", AsyncMock(return_value=None))

    result = await user_schema.execute_user_schema_parallel(
        SimpleNamespace(),
        SimpleNamespace(settings={"replies": ["r1"], "reply_count": 1}),
        "https://www.tiktok.com/@demo/video/1234567890123456789",
        ["m1"],
        509,
        boost_enabled=False,
        boost_quantity=0,
    )

    assert result.error is None
    assert len(result.replies) == 3
    assert {reply.account_id for reply in result.replies} == {202}
    assert events == [
        ("start", 201, 1),
        ("cancelled", 201, 1),
        ("start", 202, 1),
        ("finish", 202, 1),
    ]


@pytest.mark.unit
@pytest.mark.asyncio
async def test_parallel_mode_selects_main_accounts_without_blocking_event_loop(monkeypatch):
    from db import database as db_database
    from executor import browser_pool, user_schema

    @contextmanager
    def fake_db_context():
        yield SimpleNamespace(no_autoflush=SimpleNamespace())

    main_accounts = [
        SimpleNamespace(id=101 + i, proxy=f"http://proxy-{i}:1000")
        for i in range(3)
    ]
    select_lock = threading.Lock()
    selected_ids: set[int] = set()
    active_selects = 0
    max_active_selects = 0
    main_starts = []

    def fake_get_main(_db, *, exclude_ids=None, exclude_proxies=None, **_kwargs):
        nonlocal active_selects, max_active_selects
        with select_lock:
            active_selects += 1
            max_active_selects = max(max_active_selects, active_selects)
        try:
            time.sleep(0.05)
            excluded = set(exclude_ids or [])
            excluded_proxies = {str(proxy).strip() for proxy in (exclude_proxies or []) if proxy}
            with select_lock:
                candidate = next(
                    (
                        account
                        for account in main_accounts
                        if account.id not in excluded
                        and account.id not in selected_ids
                        and str(account.proxy or "").strip() not in excluded_proxies
                    ),
                    None,
                )
                if candidate is not None:
                    selected_ids.add(candidate.id)
                return candidate
        finally:
            with select_lock:
                active_selects -= 1

    async def fake_try_main(_db, account, _video_url, texts, **_kwargs):
        main_starts.append((account.id, tuple(texts)))
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

    monkeypatch.setattr(user_schema._settings, "PARALLEL_COMMENTING_TARGET_BROWSERS", 3, raising=False)
    monkeypatch.setattr(user_schema._settings, "PARALLEL_COMMENTING_MAX_BROWSERS", 3, raising=False)
    monkeypatch.setattr(user_schema._settings, "PARALLEL_COMMENTING_MAX_MAIN_ATTEMPTS", 3, raising=False)
    monkeypatch.setattr(db_database, "get_sync_db_context", fake_db_context)
    monkeypatch.setattr(user_schema, "get_free_account_for_main_comment", fake_get_main)
    monkeypatch.setattr(user_schema, "_try_main_account", fake_try_main)
    monkeypatch.setattr(user_schema, "release_main_account_reservation", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(user_schema, "_maybe_order_likes", AsyncMock(return_value=None))
    monkeypatch.setattr(browser_pool, "ensure_thread_browser_pool_capacity", lambda _min_browsers: None)
    monkeypatch.setattr(browser_pool, "close_browser_pool", AsyncMock(return_value=None))

    result = await user_schema.execute_user_schema_parallel(
        SimpleNamespace(),
        SimpleNamespace(settings={"replies": [], "reply_count": 0}),
        "https://www.tiktok.com/@demo/video/1234567890123456789",
        ["m1", "m2", "m3"],
        507,
        boost_enabled=False,
        boost_quantity=0,
    )

    assert result.error is None
    assert max_active_selects > 1
    assert sorted(main.text for main in result.published_mains) == ["m1", "m1", "m1", "m2", "m2", "m2", "m3", "m3", "m3"]
    assert sorted(account_id for account_id, _texts in main_starts) == [101, 102, 103]
    assert sorted(texts for _account_id, texts in main_starts) == [
        ("m1", "m2", "m3"),
        ("m2", "m3", "m1"),
        ("m3", "m1", "m2"),
    ]


@pytest.mark.unit
@pytest.mark.asyncio
async def test_parallel_mode_passes_every_main_variant_from_each_slot_to_replies(monkeypatch):
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
    monkeypatch.setattr(user_schema._settings, "PARALLEL_REPLY_START_DELAY_MIN_SECONDS", 0.0, raising=False)
    monkeypatch.setattr(user_schema._settings, "PARALLEL_REPLY_START_DELAY_MAX_SECONDS", 0.0, raising=False)
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
    assert (101, ("m1", "m2", "m3")) in main_calls
    assert any(texts == ("m3",) for _account_id, texts in main_calls)
    assert len(result.published_mains) == 9
    assert [main.text for main in result.published_mains].count("m3") == 3
    assert reply_candidate_counts == [9]
    assert not any(item.get("code") == "parallel_main_partial" for item in result.warnings)


@pytest.mark.unit
@pytest.mark.asyncio
async def test_parallel_mode_continues_replies_when_one_main_slot_exhausts(monkeypatch):
    # If TikTok/proxy/account issues leave only a partial main set, replies should
    # still run against the recorded parents instead of stopping the order.
    from db import database as db_database
    from executor import browser_pool, user_schema

    @contextmanager
    def fake_db_context():
        yield SimpleNamespace(no_autoflush=SimpleNamespace())

    main_accounts = [SimpleNamespace(id=101 + i, proxy=None) for i in range(8)]
    reply_accounts = [SimpleNamespace(id=301, proxy=None)]
    reply_candidate_counts = []

    def fake_get_main(_db, *, exclude_ids=None, **_kwargs):
        excluded = set(exclude_ids or [])
        return next((account for account in main_accounts if account.id not in excluded), None)

    async def fake_try_main(_db, account, _video_url, texts, **kwargs):
        correlation_id = kwargs.get("correlation_id", "")
        if "_v2_" in correlation_id:
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
        return next((account for account in reply_accounts if account.id not in excluded), None)

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

    monkeypatch.setattr(user_schema._settings, "MAX_RETRY_ACCOUNTS", 2, raising=False)
    monkeypatch.setattr(user_schema._settings, "PARALLEL_COMMENTING_MAX_MAIN_ATTEMPTS", 2, raising=False)
    monkeypatch.setattr(user_schema._settings, "PARALLEL_REPLY_START_DELAY_MIN_SECONDS", 0, raising=False)
    monkeypatch.setattr(user_schema._settings, "PARALLEL_REPLY_START_DELAY_MAX_SECONDS", 0, raising=False)
    monkeypatch.setattr(db_database, "get_sync_db_context", fake_db_context)
    monkeypatch.setattr(user_schema, "get_free_account_for_main_comment", fake_get_main)
    monkeypatch.setattr(user_schema, "_try_main_account", fake_try_main)
    monkeypatch.setattr(user_schema, "_record_main_retry_failure", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(user_schema, "release_main_account_reservation", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(user_schema, "get_free_account_for_reply", fake_get_reply)
    monkeypatch.setattr(user_schema, "_reply_with_variant_on_all_mains", fake_reply)
    monkeypatch.setattr(user_schema, "_maybe_order_likes", AsyncMock(return_value=None))
    monkeypatch.setattr(user_schema._settings, "PARALLEL_REPLY_VARIANT_WORKERS", 1, raising=False)
    monkeypatch.setattr(browser_pool, "ensure_thread_browser_pool_capacity", lambda _min_browsers: None)
    monkeypatch.setattr(browser_pool, "close_browser_pool", AsyncMock(return_value=None))
    monkeypatch.setattr(user_schema.asyncio, "sleep", AsyncMock(return_value=None))

    task = SimpleNamespace(settings={"replies": ["r1"]})
    result = await user_schema.execute_user_schema_parallel(
        SimpleNamespace(),
        task,
        "https://www.tiktok.com/@demo/video/1234567890123456789",
        ["m1", "m2", "m3"],
        518,
        boost_enabled=False,
        boost_quantity=0,
    )

    assert result.error is None
    assert len(result.published_mains) == 6
    assert len(result.replies) == 6
    assert reply_candidate_counts == [6]
    assert any(item.get("code") == "parallel_main_partial" for item in result.warnings)
    continuing_warning = next(
        item
        for item in result.warnings
        if item.get("code") == "parallel_main_incomplete_replies_continuing"
    )
    assert continuing_warning["remaining_by_slot"] == {"2": ["m2", "m3", "m1"]}


@pytest.mark.unit
@pytest.mark.asyncio
async def test_parallel_mode_main_only_proceeds_with_partial_mains(monkeypatch):
    # No reply phase: the strict gate does NOT apply. A main-only order with a
    # partial set of published mains keeps the lenient behavior and succeeds.
    from db import database as db_database
    from executor import browser_pool, user_schema

    @contextmanager
    def fake_db_context():
        yield SimpleNamespace(no_autoflush=SimpleNamespace())

    main_accounts = [SimpleNamespace(id=101 + i, proxy=None) for i in range(8)]
    reply_calls = []

    def fake_get_main(_db, *, exclude_ids=None, **_kwargs):
        excluded = set(exclude_ids or [])
        return next((account for account in main_accounts if account.id not in excluded), None)

    async def fake_try_main(_db, account, _video_url, texts, **kwargs):
        correlation_id = kwargs.get("correlation_id", "")
        if "_v2_" in correlation_id:
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

    async def fake_reply(_db, account, _video_url, main_candidates, reply_text, **kwargs):
        reply_calls.append(len(main_candidates))
        return user_schema.ReplyAttemptResult(replies=[], found_mains=[], searched=True)

    monkeypatch.setattr(user_schema._settings, "MAX_RETRY_ACCOUNTS", 2, raising=False)
    monkeypatch.setattr(user_schema._settings, "PARALLEL_COMMENTING_MAX_MAIN_ATTEMPTS", 2, raising=False)
    monkeypatch.setattr(user_schema._settings, "PARALLEL_REPLY_START_DELAY_MIN_SECONDS", 0, raising=False)
    monkeypatch.setattr(user_schema._settings, "PARALLEL_REPLY_START_DELAY_MAX_SECONDS", 0, raising=False)
    monkeypatch.setattr(db_database, "get_sync_db_context", fake_db_context)
    monkeypatch.setattr(user_schema, "get_free_account_for_main_comment", fake_get_main)
    monkeypatch.setattr(user_schema, "_try_main_account", fake_try_main)
    monkeypatch.setattr(user_schema, "_record_main_retry_failure", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(user_schema, "release_main_account_reservation", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(user_schema, "_reply_with_variant_on_all_mains", fake_reply)
    monkeypatch.setattr(user_schema, "_maybe_order_likes", AsyncMock(return_value=None))
    monkeypatch.setattr(browser_pool, "ensure_thread_browser_pool_capacity", lambda _min_browsers: None)
    monkeypatch.setattr(browser_pool, "close_browser_pool", AsyncMock(return_value=None))
    monkeypatch.setattr(user_schema.asyncio, "sleep", AsyncMock(return_value=None))

    task = SimpleNamespace(settings={"replies": []})
    result = await user_schema.execute_user_schema_parallel(
        SimpleNamespace(),
        task,
        "https://www.tiktok.com/@demo/video/1234567890123456789",
        ["m1", "m2", "m3"],
        519,
        boost_enabled=False,
        boost_quantity=0,
    )

    assert result.error is None
    assert len(result.published_mains) == 6
    # No reply phase requested: reply workers never run, and the strict block
    # warning is not emitted.
    assert reply_calls == []
    assert any(item.get("code") == "parallel_main_partial" for item in result.warnings)
    partial_warning = next(
        item
        for item in result.warnings
        if item.get("code") == "parallel_main_partial"
    )
    assert partial_warning["remaining_by_slot"] == {"2": ["m2", "m3", "m1"]}
    assert not any(
        item.get("code") == "parallel_main_incomplete_replies_continuing"
        for item in result.warnings
    )


@pytest.mark.unit
@pytest.mark.asyncio
async def test_parallel_mode_reply_variant_targets_each_main_comment(monkeypatch):
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
    assert sorted(text for _account_id, text, _cid in seen_reply_targets[0]) == [
        "m1",
        "m1",
        "m1",
        "m2",
        "m2",
        "m2",
        "m3",
        "m3",
        "m3",
    ]
    assert len(result.replies) == 9
    assert sorted(outcome.parent_comment_id for outcome in result.replies) == sorted([
        f"cid-{account_id}-{idx}"
        for account_id in (101, 102, 103)
        for idx in (1, 2, 3)
    ])


@pytest.mark.unit
@pytest.mark.asyncio
async def test_parallel_mode_keeps_retrying_main_phase_after_empty_dynamic_threshold(monkeypatch):
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
    monkeypatch.setattr(user_schema._settings, "PARALLEL_REPLY_START_DELAY_MIN_SECONDS", 0.0, raising=False)
    monkeypatch.setattr(user_schema._settings, "PARALLEL_REPLY_START_DELAY_MAX_SECONDS", 0.0, raising=False)
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
    assert len(main_attempts) == len(main_accounts)
    assert len(main_attempts) > 6
    assert close_calls == ["close"]


@pytest.mark.unit
@pytest.mark.asyncio
async def test_parallel_mode_classifies_main_goto_failures_as_proxy_navigation(monkeypatch):
    from db import database as db_database
    from executor import browser_pool, user_schema

    @contextmanager
    def fake_db_context():
        yield SimpleNamespace(no_autoflush=SimpleNamespace())

    main_accounts = [SimpleNamespace(id=101 + i, proxy=f"http://proxy-{i}") for i in range(12)]
    main_attempts = []

    def fake_get_main(_db, *, exclude_ids=None, **_kwargs):
        excluded = set(exclude_ids or [])
        return next((account for account in main_accounts if account.id not in excluded), None)

    async def fake_try_main(_db, account, _video_url, texts, **_kwargs):
        main_attempts.append((account.id, tuple(texts)))
        return [], "login_ambiguous:goto_failed:Page.goto: net::ERR_EMPTY_RESPONSE"

    monkeypatch.setattr(user_schema._settings, "MAX_RETRY_ACCOUNTS", 30, raising=False)
    monkeypatch.setattr(user_schema._settings, "PARALLEL_COMMENTING_MAX_MAIN_ATTEMPTS", 30, raising=False)
    monkeypatch.setattr(user_schema._settings, "PARALLEL_COMMENTING_MAX_REPLY_ATTEMPTS", 30, raising=False)
    monkeypatch.setattr(db_database, "get_sync_db_context", fake_db_context)
    monkeypatch.setattr(user_schema, "get_free_account_for_main_comment", fake_get_main)
    monkeypatch.setattr(user_schema, "_try_main_account", fake_try_main)
    monkeypatch.setattr(user_schema, "_record_main_retry_failure", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(user_schema, "release_main_account_reservation", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(user_schema, "ensure_proxy_ip_rotated", AsyncMock(return_value="1.2.3.4"))
    monkeypatch.setattr(user_schema, "clear_proxy_bad", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(user_schema, "_maybe_order_likes", AsyncMock(return_value=None))
    monkeypatch.setattr(browser_pool, "ensure_thread_browser_pool_capacity", lambda _min_browsers: None)
    monkeypatch.setattr(browser_pool, "close_browser_pool", AsyncMock(return_value=None))
    monkeypatch.setattr(user_schema.asyncio, "sleep", AsyncMock(return_value=None))

    result = await user_schema.execute_user_schema_parallel(
        SimpleNamespace(),
        SimpleNamespace(settings={"replies": ["r1"]}),
        "https://www.tiktok.com/@demo/video/1234567890123456789",
        ["m1", "m2", "m3"],
        516,
        boost_enabled=False,
        boost_quantity=0,
    )

    assert result.error == "parallel_main_proxy_navigation_failed"
    assert 6 <= len(main_attempts) < len(main_accounts)


@pytest.mark.unit
@pytest.mark.asyncio
async def test_parallel_mode_single_main_uses_available_accounts_beyond_empty_threshold(monkeypatch):
    from db import database as db_database
    from executor import browser_pool, user_schema

    @contextmanager
    def fake_db_context():
        yield SimpleNamespace(no_autoflush=SimpleNamespace())

    main_accounts = [SimpleNamespace(id=101 + i, proxy=None) for i in range(8)]
    main_attempts = []

    def fake_get_main(_db, *, exclude_ids=None, **_kwargs):
        excluded = set(exclude_ids or [])
        return next((account for account in main_accounts if account.id not in excluded), None)

    async def fake_try_main(_db, account, _video_url, texts, **_kwargs):
        main_attempts.append((account.id, tuple(texts)))
        return [], "comment_list_not_open"

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
    monkeypatch.setattr(browser_pool, "close_browser_pool", AsyncMock(return_value=None))
    monkeypatch.setattr(user_schema.asyncio, "sleep", AsyncMock(return_value=None))

    result = await user_schema.execute_user_schema_parallel(
        SimpleNamespace(),
        SimpleNamespace(settings={"replies": ["r1"]}),
        "https://www.tiktok.com/@demo/video/1234567890123456789",
        ["only-main"],
        509,
        boost_enabled=False,
        boost_quantity=0,
    )

    assert result.error == "parallel_main_no_publications_after_two_batches"
    assert len(main_attempts) == len(main_accounts)
    assert len(main_attempts) > 6
    assert all(texts == ("only-main",) for _account_id, texts in main_attempts)


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
async def test_parallel_mode_caps_active_main_at_configured_main_workers_and_reply_at_target(monkeypatch):
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
    selected_main_ids: set[int] = set()
    select_lock = threading.Lock()

    def fake_get_main(_db, *, exclude_ids=None, **_kwargs):
        excluded = set(exclude_ids or [])
        with select_lock:
            candidate = next(
                (
                    account
                    for account in main_accounts
                    if account.id not in excluded and account.id not in selected_main_ids
                ),
                None,
            )
            if candidate is not None:
                selected_main_ids.add(candidate.id)
            return candidate

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

    monkeypatch.setattr(user_schema._settings, "PARALLEL_MAIN_WORKERS", 2, raising=False)
    monkeypatch.setattr(user_schema._settings, "PARALLEL_REPLY_VARIANT_WORKERS", 1, raising=False)
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
    assert max_active_main == 2
    assert max_active_reply == 1
    assert close_calls == ["close", "close"]


@pytest.mark.unit
@pytest.mark.asyncio
async def test_parallel_mode_limits_main_proxy_to_three_active_browsers(monkeypatch):
    from db import database as db_database
    from executor import browser_pool, user_schema

    @contextmanager
    def fake_db_context():
        yield SimpleNamespace(no_autoflush=SimpleNamespace())

    shared_proxy = "http://proxy-a:1000"
    main_accounts = [
        SimpleNamespace(id=101 + i, proxy=shared_proxy)
        for i in range(3)
    ]
    main_attempts = []
    selection_snapshots = []
    active_by_proxy: dict[str, int] = {}
    max_active_by_proxy: dict[str, int] = {}
    close_calls = []
    real_sleep = asyncio.sleep

    def fake_get_main(_db, *, exclude_ids=None, exclude_proxies=None, **_kwargs):
        excluded = set(exclude_ids or [])
        excluded_proxies = {str(proxy).strip() for proxy in (exclude_proxies or []) if proxy}
        selection_snapshots.append((tuple(sorted(excluded)), tuple(sorted(excluded_proxies))))
        return next(
            (
                account
                for account in main_accounts
                if account.id not in excluded
                and str(account.proxy or "").strip() not in excluded_proxies
            ),
            None,
        )

    async def fake_try_main(_db, account, _video_url, texts, **_kwargs):
        proxy = str(account.proxy or "")
        active_by_proxy[proxy] = active_by_proxy.get(proxy, 0) + 1
        max_active_by_proxy[proxy] = max(
            max_active_by_proxy.get(proxy, 0),
            active_by_proxy[proxy],
        )
        main_attempts.append((account.id, tuple(texts)))
        await real_sleep(0.02)
        active_by_proxy[proxy] -= 1
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

    async def fake_close_pool():
        close_calls.append("close")

    monkeypatch.setattr(user_schema._settings, "PARALLEL_COMMENTING_TARGET_BROWSERS", 3, raising=False)
    monkeypatch.setattr(user_schema._settings, "PARALLEL_COMMENTING_MAX_BROWSERS", 3, raising=False)
    monkeypatch.setattr(user_schema._settings, "PARALLEL_COMMENTING_MAX_MAIN_ATTEMPTS", 6, raising=False)
    monkeypatch.setattr(db_database, "get_sync_db_context", fake_db_context)
    monkeypatch.setattr(user_schema, "get_free_account_for_main_comment", fake_get_main)
    monkeypatch.setattr(user_schema, "_try_main_account", fake_try_main)
    monkeypatch.setattr(user_schema, "release_main_account_reservation", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(user_schema, "_maybe_order_likes", AsyncMock(return_value=None))
    monkeypatch.setattr(browser_pool, "ensure_thread_browser_pool_capacity", lambda _min_browsers: None)
    monkeypatch.setattr(browser_pool, "close_browser_pool", fake_close_pool)

    result = await user_schema.execute_user_schema_parallel(
        SimpleNamespace(),
        SimpleNamespace(settings={"replies": [], "reply_count": 0}),
        "https://www.tiktok.com/@demo/video/1234567890123456789",
        ["m1", "m2", "m3"],
        505,
        boost_enabled=False,
        boost_quantity=0,
    )

    assert result.error is None
    assert sorted(main.text for main in result.published_mains) == ["m1", "m1", "m1", "m2", "m2", "m2", "m3", "m3", "m3"]
    assert [attempt[0] for attempt in main_attempts] == [101, 102, 103]
    assert [attempt[1] for attempt in main_attempts] == [
        ("m1", "m2", "m3"),
        ("m2", "m3", "m1"),
        ("m3", "m1", "m2"),
    ]
    assert 1 <= max_active_by_proxy[shared_proxy] <= 3
    assert any(shared_proxy in excluded_proxies for _excluded_ids, excluded_proxies in selection_snapshots)
    assert close_calls == ["close"]


@pytest.mark.unit
@pytest.mark.asyncio
async def test_parallel_mode_rotates_mobile_proxy_before_third_main_browser(monkeypatch):
    from db import database as db_database
    from executor import browser_pool, user_schema

    shared_proxy = "http://mobile-proxy:1000"

    class FakeQuery:
        def filter(self, *_args, **_kwargs):
            return self

        def first(self):
            return SimpleNamespace(
                proxy=shared_proxy,
                is_mobile="Y",
                rotation_url="http://rotate.local",
            )

    class FakeDb:
        no_autoflush = SimpleNamespace()

        def query(self, *_args, **_kwargs):
            return FakeQuery()

    @contextmanager
    def fake_db_context():
        yield FakeDb()

    main_accounts = [
        SimpleNamespace(id=101 + i, proxy=shared_proxy)
        for i in range(3)
    ]
    selected_ids: set[int] = set()
    select_lock = threading.Lock()
    events = []
    started_accounts: set[int] = set()
    first_two_started = asyncio.Event()
    release_first_two = asyncio.Event()
    real_sleep = asyncio.sleep

    def fake_get_main(_db, *, exclude_ids=None, exclude_proxies=None, **_kwargs):
        excluded = set(exclude_ids or [])
        excluded_proxies = {str(proxy).strip() for proxy in (exclude_proxies or []) if proxy}
        with select_lock:
            candidate = next(
                (
                    account
                    for account in main_accounts
                    if account.id not in excluded
                    and account.id not in selected_ids
                    and str(account.proxy or "").strip() not in excluded_proxies
                ),
                None,
            )
            if candidate is not None:
                selected_ids.add(candidate.id)
            return candidate

    async def fake_try_main(_db, account, _video_url, texts, **_kwargs):
        events.append(("start", account.id))
        started_accounts.add(account.id)
        if {101, 102}.issubset(started_accounts):
            first_two_started.set()
        if account.id in {101, 102}:
            await release_first_two.wait()
        await real_sleep(0.01)
        events.append(("finish", account.id))
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

    async def fake_rotate(proxy_obj, _db):
        events.append(("rotate", proxy_obj.proxy))
        await real_sleep(0)
        return "203.0.113.10"

    monkeypatch.setattr(user_schema._settings, "PARALLEL_COMMENTING_TARGET_BROWSERS", 3, raising=False)
    monkeypatch.setattr(user_schema._settings, "PARALLEL_COMMENTING_MAX_BROWSERS", 3, raising=False)
    monkeypatch.setattr(user_schema._settings, "PARALLEL_MAIN_WORKERS", 3, raising=False)
    monkeypatch.setattr(user_schema._settings, "PARALLEL_MAIN_MAX_ACTIVE_BROWSERS_PER_PROXY", 3, raising=False)
    monkeypatch.setattr(user_schema._settings, "PARALLEL_MAIN_ROTATE_MOBILE_PROXY_AFTER_ACCOUNTS", 2, raising=False)
    monkeypatch.setattr(user_schema._settings, "PARALLEL_COMMENTING_MAX_MAIN_ATTEMPTS", 6, raising=False)
    monkeypatch.setattr(user_schema._settings, "PARALLEL_MAIN_REFILL_POLL_SECONDS", 0.05, raising=False)
    monkeypatch.setattr(user_schema._settings, "PARALLEL_PROXY_SLOT_REFILL_BACKOFF_SECONDS", 0.05, raising=False)
    monkeypatch.setattr(db_database, "get_sync_db_context", fake_db_context)
    monkeypatch.setattr(user_schema, "get_free_account_for_main_comment", fake_get_main)
    monkeypatch.setattr(user_schema, "_try_main_account", fake_try_main)
    monkeypatch.setattr(user_schema, "_record_main_retry_failure", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(
        user_schema,
        "release_main_account_reservation",
        lambda account_id, *_args, **_kwargs: selected_ids.discard(int(account_id or 0)),
    )
    monkeypatch.setattr(user_schema, "ensure_proxy_ip_rotated", fake_rotate)
    monkeypatch.setattr(user_schema, "_maybe_order_likes", AsyncMock(return_value=None))
    monkeypatch.setattr(browser_pool, "ensure_thread_browser_pool_capacity", lambda _min_browsers: None)
    monkeypatch.setattr(browser_pool, "close_browser_pool", AsyncMock(return_value=None))

    run_task = asyncio.create_task(
        user_schema.execute_user_schema_parallel(
            SimpleNamespace(),
            SimpleNamespace(settings={"replies": [], "reply_count": 0}),
            "https://www.tiktok.com/@demo/video/1234567890123456789",
            ["m1", "m2", "m3"],
            510,
            boost_enabled=False,
            boost_quantity=0,
        )
    )

    await asyncio.wait_for(first_two_started.wait(), timeout=1.0)
    await real_sleep(0.15)
    assert ("start", 103) not in events

    release_first_two.set()
    result = await asyncio.wait_for(run_task, timeout=3.0)

    assert result.error is None
    assert events.index(("rotate", shared_proxy)) < events.index(("start", 103))
    assert [event for event in events if event[0] == "start"] == [
        ("start", 101),
        ("start", 102),
        ("start", 103),
    ]
    assert sorted(main.account_id for main in result.published_mains) == [101, 101, 101, 102, 102, 102, 103, 103, 103]


@pytest.mark.unit
@pytest.mark.asyncio
async def test_parallel_mode_keeps_main_proxy_blocked_while_planned_rotation_is_in_flight(monkeypatch):
    from db import database as db_database
    from executor import browser_pool, user_schema

    shared_proxy = "http://mobile-proxy-race:1000"
    other_proxy = "http://other-proxy-race:1000"
    main_accounts = [
        SimpleNamespace(id=101, proxy=shared_proxy),
        SimpleNamespace(id=201, proxy=other_proxy),
        SimpleNamespace(id=102, proxy=shared_proxy),
        SimpleNamespace(id=103, proxy=shared_proxy),
        SimpleNamespace(id=202, proxy=other_proxy),
    ]

    class FakeQuery:
        def filter(self, *_args, **_kwargs):
            return self

        def first(self):
            return SimpleNamespace(
                proxy=shared_proxy,
                is_mobile="Y",
                rotation_url="http://rotate.local",
            )

    class FakeDb:
        no_autoflush = SimpleNamespace()

        def query(self, *_args, **_kwargs):
            return FakeQuery()

    @contextmanager
    def fake_db_context():
        yield FakeDb()

    selected_ids: set[int] = set()
    select_lock = threading.Lock()
    events = []
    release_shared = asyncio.Event()
    shared_pair_started = asyncio.Event()
    rotation_started = asyncio.Event()
    finish_rotation = asyncio.Event()
    real_sleep = asyncio.sleep

    def fake_get_main(_db, *, exclude_ids=None, exclude_proxies=None, **_kwargs):
        excluded = {int(item) for item in (exclude_ids or [])}
        excluded_proxies = {str(proxy).strip() for proxy in (exclude_proxies or []) if proxy}
        with select_lock:
            candidate = next(
                (
                    account
                    for account in main_accounts
                    if account.id not in excluded
                    and account.id not in selected_ids
                    and str(account.proxy or "").strip() not in excluded_proxies
                ),
                None,
            )
            if candidate is not None:
                selected_ids.add(candidate.id)
            return candidate

    async def fake_try_main(_db, account, _video_url, texts, **_kwargs):
        events.append(("start", account.id))
        shared_started = [event for event in events if event in {("start", 101), ("start", 102)}]
        if len(shared_started) == 2:
            shared_pair_started.set()
        if account.id in {101, 102}:
            await release_shared.wait()
        if account.id == 201:
            await rotation_started.wait()
            events.append(("fail", account.id))
            return [], "transient_other_proxy_failure"
        await real_sleep(0.01)
        events.append(("finish", account.id))
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

    async def fake_rotate(proxy_obj, _db):
        events.append(("rotate_start", proxy_obj.proxy))
        rotation_started.set()
        await finish_rotation.wait()
        events.append(("rotate_finish", proxy_obj.proxy))
        return "203.0.113.11"

    monkeypatch.setattr(user_schema._settings, "PARALLEL_MAIN_ACCOUNT_SLOTS", 4, raising=False)
    monkeypatch.setattr(user_schema._settings, "PARALLEL_COMMENTING_TARGET_BROWSERS", 3, raising=False)
    monkeypatch.setattr(user_schema._settings, "PARALLEL_COMMENTING_MAX_BROWSERS", 3, raising=False)
    monkeypatch.setattr(user_schema._settings, "PARALLEL_MAIN_WORKERS", 3, raising=False)
    monkeypatch.setattr(user_schema._settings, "PARALLEL_MAIN_MAX_ACTIVE_BROWSERS_PER_PROXY", 3, raising=False)
    monkeypatch.setattr(user_schema._settings, "PARALLEL_MAIN_ROTATE_MOBILE_PROXY_AFTER_ACCOUNTS", 2, raising=False)
    monkeypatch.setattr(user_schema._settings, "PARALLEL_COMMENTING_MAX_MAIN_ATTEMPTS", 6, raising=False)
    monkeypatch.setattr(user_schema._settings, "PARALLEL_MAIN_REFILL_POLL_SECONDS", 0.03, raising=False)
    monkeypatch.setattr(user_schema._settings, "PARALLEL_PROXY_SLOT_REFILL_BACKOFF_SECONDS", 0.03, raising=False)
    monkeypatch.setattr(db_database, "get_sync_db_context", fake_db_context)
    monkeypatch.setattr(user_schema, "get_free_account_for_main_comment", fake_get_main)
    monkeypatch.setattr(
        user_schema,
        "_proxy_is_rotatable_mobile_for_main",
        lambda _db, proxy_value: str(proxy_value or "").strip() == shared_proxy,
    )
    monkeypatch.setattr(user_schema, "_try_main_account", fake_try_main)
    monkeypatch.setattr(user_schema, "_record_main_retry_failure", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(
        user_schema,
        "release_main_account_reservation",
        lambda account_id, *_args, **_kwargs: selected_ids.discard(int(account_id or 0)),
    )
    monkeypatch.setattr(user_schema, "ensure_proxy_ip_rotated", fake_rotate)
    monkeypatch.setattr(user_schema, "_maybe_order_likes", AsyncMock(return_value=None))
    monkeypatch.setattr(browser_pool, "ensure_thread_browser_pool_capacity", lambda _min_browsers: None)
    monkeypatch.setattr(browser_pool, "close_browser_pool", AsyncMock(return_value=None))

    run_task = asyncio.create_task(
        user_schema.execute_user_schema_parallel(
            SimpleNamespace(),
            SimpleNamespace(settings={"replies": [], "reply_count": 0}),
            "https://www.tiktok.com/@demo/video/1234567890123456789",
            ["m1", "m2", "m3"],
            511,
            boost_enabled=False,
            boost_quantity=0,
        )
    )

    await asyncio.wait_for(shared_pair_started.wait(), timeout=1.0)
    release_shared.set()
    await asyncio.wait_for(rotation_started.wait(), timeout=1.0)
    await real_sleep(0.15)
    assert ("start", 103) not in events

    finish_rotation.set()
    result = await asyncio.wait_for(run_task, timeout=3.0)

    assert result.error is None
    assert events.index(("rotate_finish", shared_proxy)) < events.index(("start", 103))
    assert ("start", 202) in events


@pytest.mark.unit
@pytest.mark.asyncio
async def test_parallel_mode_rotates_mobile_proxy_after_three_reply_browsers(monkeypatch):
    from db import database as db_database
    from executor import browser_pool, user_schema

    shared_proxy = "http://reply-mobile-proxy:1000"

    class FakeQuery:
        def filter(self, *_args, **_kwargs):
            return self

        def first(self):
            return SimpleNamespace(
                proxy=shared_proxy,
                is_mobile="Y",
                rotation_url="http://rotate-reply.local",
            )

    class FakeDb:
        no_autoflush = SimpleNamespace()

        def query(self, *_args, **_kwargs):
            return FakeQuery()

    @contextmanager
    def fake_db_context():
        yield FakeDb()

    main_account = SimpleNamespace(id=101, proxy=None)
    reply_accounts = [
        SimpleNamespace(id=201 + i, proxy=shared_proxy)
        for i in range(4)
    ]
    selected_main = False
    selected_reply_ids: set[int] = set()
    select_lock = threading.Lock()
    events = []
    first_two_started = asyncio.Event()
    release_first_two = asyncio.Event()
    third_started = asyncio.Event()
    release_third = asyncio.Event()
    rotation_started = asyncio.Event()
    finish_rotation = asyncio.Event()
    real_sleep = asyncio.sleep

    def fake_get_main(_db, *, exclude_ids=None, **_kwargs):
        nonlocal selected_main
        if selected_main or main_account.id in set(exclude_ids or []):
            return None
        selected_main = True
        return main_account

    async def fake_try_main(_db, account, _video_url, texts, **_kwargs):
        return [
            user_schema.PublishedMain(
                text=text,
                comment_id=f"main-{idx}",
                network_confirmed=True,
                local_confirmed=True,
                method="hybrid_dom",
                account_id=account.id,
                tiktok_username=f"user_{account.id}",
            )
            for idx, text in enumerate(texts, start=1)
        ], None

    def fake_get_reply(_db, exclude_ids=None, exclude_proxies=None, **_kwargs):
        excluded = {int(item) for item in (exclude_ids or [])}
        excluded_proxies = {str(proxy).strip() for proxy in (exclude_proxies or []) if proxy}
        with select_lock:
            candidate = next(
                (
                    account
                    for account in reply_accounts
                    if account.id not in excluded
                    and account.id not in selected_reply_ids
                    and str(account.proxy or "").strip() not in excluded_proxies
                ),
                None,
            )
            if candidate is not None:
                selected_reply_ids.add(candidate.id)
            return candidate

    async def fake_reply(_db, account, _video_url, main_candidates, reply_text, **kwargs):
        events.append(("start", account.id))
        started_reply_ids = [event[1] for event in events if event[0] == "start"]
        if {201, 202}.issubset(started_reply_ids):
            first_two_started.set()
        if account.id in {201, 202}:
            await release_first_two.wait()
        if account.id == 203:
            third_started.set()
            await release_third.wait()
        await real_sleep(0.01)
        events.append(("finish", account.id))
        outcomes = [
            user_schema.ReplyOutcome(
                text=reply_text,
                variant_index=kwargs["variant_index"],
                matched_main_text=main.text,
                parent_comment_id=main.comment_id,
                parent_key=user_schema._main_parent_key(main),
                success=True,
                method="unit_local",
                error=None,
                account_id=account.id,
                attempted=True,
                local_confirmed=True,
                counts_as_submitted=True,
            )
            for main in main_candidates
        ]
        return user_schema.ReplyAttemptResult(
            replies=outcomes,
            found_mains=list(main_candidates),
            searched=True,
            saw_comment_rows=True,
            parent_search_max_seen_rows=10,
            parent_search_unique_rows_seen=10,
        )

    async def fake_rotate(proxy_obj, _db):
        events.append(("rotate_start", proxy_obj.proxy))
        rotation_started.set()
        await finish_rotation.wait()
        events.append(("rotate_finish", proxy_obj.proxy))
        return "203.0.113.21"

    monkeypatch.setattr(user_schema._settings, "PARALLEL_MAIN_ACCOUNT_SLOTS", 1, raising=False)
    monkeypatch.setattr(user_schema._settings, "PARALLEL_COMMENTING_TARGET_BROWSERS", 2, raising=False)
    monkeypatch.setattr(user_schema._settings, "PARALLEL_COMMENTING_MAX_BROWSERS", 2, raising=False)
    monkeypatch.setattr(user_schema._settings, "PARALLEL_MAIN_WORKERS", 1, raising=False)
    monkeypatch.setattr(user_schema._settings, "PARALLEL_REPLY_VARIANT_WORKERS", 2, raising=False)
    monkeypatch.setattr(user_schema._settings, "PARALLEL_REPLY_MAX_ACTIVE_BROWSERS_PER_PROXY", 3, raising=False)
    monkeypatch.setattr(user_schema._settings, "PARALLEL_REPLY_ROTATE_MOBILE_PROXY_AFTER_ACCOUNTS", 3, raising=False)
    monkeypatch.setattr(user_schema._settings, "PARALLEL_COMMENTING_MAX_REPLY_ATTEMPTS", 6, raising=False)
    monkeypatch.setattr(user_schema._settings, "PARALLEL_REPLY_REFILL_POLL_SECONDS", 0.03, raising=False)
    monkeypatch.setattr(user_schema._settings, "PARALLEL_PROXY_SLOT_REFILL_BACKOFF_SECONDS", 0.03, raising=False)
    monkeypatch.setattr(user_schema._settings, "PARALLEL_REPLY_START_DELAY_MIN_SECONDS", 0, raising=False)
    monkeypatch.setattr(user_schema._settings, "PARALLEL_REPLY_START_DELAY_MAX_SECONDS", 0, raising=False)
    monkeypatch.setattr(db_database, "get_sync_db_context", fake_db_context)
    monkeypatch.setattr(user_schema, "get_free_account_for_main_comment", fake_get_main)
    monkeypatch.setattr(user_schema, "_try_main_account", fake_try_main)
    monkeypatch.setattr(user_schema, "get_free_account_for_reply", fake_get_reply)
    monkeypatch.setattr(user_schema, "_reply_with_variant_on_all_mains", fake_reply)
    monkeypatch.setattr(user_schema, "_record_main_retry_failure", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(
        user_schema,
        "release_reply_account_reservation",
        lambda account_id, *_args, **_kwargs: selected_reply_ids.discard(int(account_id or 0)),
    )
    monkeypatch.setattr(user_schema, "release_main_account_reservation", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(user_schema, "ensure_proxy_ip_rotated", fake_rotate)
    monkeypatch.setattr(user_schema, "_maybe_order_likes", AsyncMock(return_value=None))
    monkeypatch.setattr(browser_pool, "ensure_thread_browser_pool_capacity", lambda _min_browsers: None)
    monkeypatch.setattr(browser_pool, "close_browser_pool", AsyncMock(return_value=None))

    run_task = asyncio.create_task(
        user_schema.execute_user_schema_parallel(
            FakeDb(),
            SimpleNamespace(settings={"replies": ["r1", "r2", "r3", "r4"], "reply_count": 4}),
            "https://www.tiktok.com/@demo/video/1234567890123456789",
            ["m1"],
            512,
            boost_enabled=False,
            boost_quantity=0,
        )
    )

    await asyncio.wait_for(first_two_started.wait(), timeout=1.0)
    release_first_two.set()
    await asyncio.wait_for(third_started.wait(), timeout=1.0)
    await real_sleep(0.15)
    assert ("start", 204) not in events

    release_third.set()
    await asyncio.wait_for(rotation_started.wait(), timeout=1.0)
    await real_sleep(0.15)
    assert ("start", 204) not in events

    finish_rotation.set()
    result = await asyncio.wait_for(run_task, timeout=3.0)

    assert result.error is None
    assert events.index(("rotate_finish", shared_proxy)) < events.index(("start", 204))
    assert [event for event in events if event[0] == "start"] == [
        ("start", 201),
        ("start", 202),
        ("start", 203),
        ("start", 204),
    ]
    assert sorted(outcome.account_id for outcome in result.replies if outcome.counts_as_submitted) == [
        201,
        202,
        203,
        204,
    ]


@pytest.mark.unit
@pytest.mark.asyncio
async def test_parallel_mode_does_not_spin_main_selection_while_proxy_slots_are_full(monkeypatch):
    from db import database as db_database
    from executor import browser_pool, user_schema

    @contextmanager
    def fake_db_context():
        yield SimpleNamespace(no_autoflush=SimpleNamespace())

    shared_proxy = "http://proxy-spin:1000"
    main_accounts = [
        SimpleNamespace(id=101 + i, proxy=shared_proxy)
        for i in range(3)
    ]
    selection_calls = 0
    started_accounts: set[int] = set()
    two_active_started = asyncio.Event()
    release_active = asyncio.Event()

    def fake_get_main(_db, *, exclude_ids=None, exclude_proxies=None, **_kwargs):
        nonlocal selection_calls
        selection_calls += 1
        excluded = set(exclude_ids or [])
        excluded_proxies = {str(proxy).strip() for proxy in (exclude_proxies or []) if proxy}
        return next(
            (
                account
                for account in main_accounts
                if account.id not in excluded
                and str(account.proxy or "").strip() not in excluded_proxies
            ),
            None,
        )

    async def fake_try_main(_db, account, _video_url, texts, **_kwargs):
        started_accounts.add(account.id)
        if len(started_accounts & {101, 102}) == 2:
            two_active_started.set()
        if account.id in {101, 102}:
            await release_active.wait()
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

    monkeypatch.setattr(user_schema._settings, "PARALLEL_COMMENTING_TARGET_BROWSERS", 3, raising=False)
    monkeypatch.setattr(user_schema._settings, "PARALLEL_COMMENTING_MAX_BROWSERS", 3, raising=False)
    monkeypatch.setattr(user_schema._settings, "PARALLEL_COMMENTING_MAX_MAIN_ATTEMPTS", 4, raising=False)
    monkeypatch.setattr(user_schema._settings, "PARALLEL_MAIN_REFILL_POLL_SECONDS", 0.05, raising=False)
    monkeypatch.setattr(user_schema._settings, "PARALLEL_PROXY_SLOT_REFILL_BACKOFF_SECONDS", 0.05, raising=False)
    monkeypatch.setattr(db_database, "get_sync_db_context", fake_db_context)
    monkeypatch.setattr(user_schema, "get_free_account_for_main_comment", fake_get_main)
    monkeypatch.setattr(user_schema, "_try_main_account", fake_try_main)
    monkeypatch.setattr(user_schema, "release_main_account_reservation", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(user_schema, "_maybe_order_likes", AsyncMock(return_value=None))
    monkeypatch.setattr(browser_pool, "ensure_thread_browser_pool_capacity", lambda _min_browsers: None)
    monkeypatch.setattr(browser_pool, "close_browser_pool", AsyncMock(return_value=None))

    run_task = asyncio.create_task(
        user_schema.execute_user_schema_parallel(
            SimpleNamespace(),
            SimpleNamespace(settings={"replies": [], "reply_count": 0}),
            "https://www.tiktok.com/@demo/video/1234567890123456789",
            ["m1", "m2", "m3"],
            509,
            boost_enabled=False,
            boost_quantity=0,
        )
    )

    await asyncio.wait_for(two_active_started.wait(), timeout=1.0)
    stable_selection_rounds = 0
    last_selection_calls = -1
    selection_deadline = time.monotonic() + 1.0
    while stable_selection_rounds < 3 and time.monotonic() < selection_deadline:
        await asyncio.sleep(0.01)
        if selection_calls == last_selection_calls:
            stable_selection_rounds += 1
        else:
            stable_selection_rounds = 0
            last_selection_calls = selection_calls
    calls_after_defer = selection_calls
    await asyncio.sleep(0.2)
    assert selection_calls == calls_after_defer

    release_active.set()
    result = await asyncio.wait_for(run_task, timeout=2.0)

    assert result.error is None
    assert sorted(started_accounts) == [101, 102, 103]
    assert sorted(main.text for main in result.published_mains) == ["m1", "m1", "m1", "m2", "m2", "m2", "m3", "m3", "m3"]


@pytest.mark.unit
@pytest.mark.asyncio
async def test_parallel_mode_defers_reply_selection_when_proxy_slots_are_full(monkeypatch):
    from db import database as db_database
    from executor import browser_pool, user_schema

    @contextmanager
    def fake_db_context():
        yield SimpleNamespace(no_autoflush=SimpleNamespace())

    shared_proxy = "http://reply-proxy:1000"
    main_accounts = [
        SimpleNamespace(id=101 + i, proxy=f"http://main-proxy-{i}:1000")
        for i in range(3)
    ]
    reply_accounts = [
        SimpleNamespace(id=201, proxy=shared_proxy),
        SimpleNamespace(id=202, proxy=shared_proxy),
        SimpleNamespace(id=203, proxy=shared_proxy),
    ]
    reply_account_by_variant: dict[int, int] = {}
    active_replies = 0
    max_active_replies = 0
    reply_events = []

    def fake_get_main(_db, *, exclude_ids=None, **_kwargs):
        excluded = set(exclude_ids or [])
        return next((account for account in main_accounts if account.id not in excluded), None)

    async def fake_try_main(_db, account, _video_url, texts, **_kwargs):
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

    def fake_get_reply(_db, exclude_ids=None, exclude_proxies=None, **_kwargs):
        excluded = set(exclude_ids or [])
        excluded_proxies = set(exclude_proxies or [])
        return next(
            (
                account
                for account in reply_accounts
                if account.id not in excluded
                and str(account.proxy or "").strip() not in excluded_proxies
            ),
            None,
        )

    async def fake_reply(_db, account, _video_url, main_candidates, reply_text, **kwargs):
        nonlocal active_replies, max_active_replies
        variant_index = kwargs["variant_index"]
        reply_account_by_variant[variant_index] = account.id
        reply_events.append(("start", variant_index))
        active_replies += 1
        max_active_replies = max(max_active_replies, active_replies)
        try:
            if variant_index in {1, 2}:
                await asyncio.sleep(0.03)
            return user_schema.ReplyAttemptResult(
                replies=[
                    user_schema.ReplyOutcome(
                        text=reply_text,
                        variant_index=variant_index,
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
        finally:
            reply_events.append(("finish", variant_index))
            active_replies -= 1

    monkeypatch.setattr(user_schema._settings, "PARALLEL_COMMENTING_TARGET_BROWSERS", 3, raising=False)
    monkeypatch.setattr(user_schema._settings, "PARALLEL_COMMENTING_MAX_BROWSERS", 3, raising=False)
    monkeypatch.setattr(user_schema._settings, "PARALLEL_REPLY_MAX_ACTIVE_BROWSERS_PER_PROXY", 1, raising=False)
    monkeypatch.setattr(user_schema._settings, "PARALLEL_REPLY_REFILL_POLL_SECONDS", 0.01, raising=False)
    monkeypatch.setattr(user_schema._settings, "PARALLEL_REPLY_START_DELAY_MIN_SECONDS", 0.0, raising=False)
    monkeypatch.setattr(user_schema._settings, "PARALLEL_REPLY_START_DELAY_MAX_SECONDS", 0.0, raising=False)
    monkeypatch.setattr(db_database, "get_sync_db_context", fake_db_context)
    monkeypatch.setattr(user_schema, "get_free_account_for_main_comment", fake_get_main)
    monkeypatch.setattr(user_schema, "_try_main_account", fake_try_main)
    monkeypatch.setattr(user_schema, "release_main_account_reservation", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(user_schema, "get_free_account_for_reply", fake_get_reply)
    monkeypatch.setattr(user_schema, "_reply_with_variant_on_all_mains", fake_reply)
    monkeypatch.setattr(user_schema, "_maybe_order_likes", AsyncMock(return_value=None))
    monkeypatch.setattr(browser_pool, "ensure_thread_browser_pool_capacity", lambda _min_browsers: None)
    monkeypatch.setattr(browser_pool, "close_browser_pool", AsyncMock(return_value=None))

    task = SimpleNamespace(settings={"replies": ["r1", "r2", "r3"]})
    result = await user_schema.execute_user_schema_parallel(
        SimpleNamespace(),
        task,
        "https://www.tiktok.com/@demo/video/1234567890123456789",
        ["m1"],
        506,
        boost_enabled=False,
        boost_quantity=0,
    )

    assert result.error is None
    assert all(outcome.error != "no_free_reply_accounts" for outcome in result.replies)
    assert len([outcome for outcome in result.replies if outcome.success]) == 9
    assert set(reply_account_by_variant) == {1, 2, 3}
    assert set(reply_account_by_variant.values()) == {201, 202, 203}
    assert max_active_replies == 1


@pytest.mark.unit
@pytest.mark.asyncio
async def test_parallel_mode_refills_reply_worker_after_fast_failure(monkeypatch):
    from db import database as db_database
    from executor import browser_pool, user_schema

    @contextmanager
    def fake_db_context():
        yield SimpleNamespace(no_autoflush=SimpleNamespace())

    main_accounts = [SimpleNamespace(id=101 + i, proxy=None) for i in range(3)]
    reply_accounts = [
        SimpleNamespace(id=201, proxy="http://proxy-a:1000"),
        SimpleNamespace(id=202, proxy="http://proxy-b:1000"),
        SimpleNamespace(id=203, proxy="http://proxy-c:1000"),
        SimpleNamespace(id=204, proxy="http://proxy-d:1000"),
        SimpleNamespace(id=205, proxy="http://proxy-a:1000"),
    ]
    reply_events = []
    reply_account_by_variant = {}
    variant_one_attempts = 0

    def fake_get_main(_db, *, exclude_ids=None, **_kwargs):
        excluded = set(exclude_ids or [])
        return next((account for account in main_accounts if account.id not in excluded), None)

    async def fake_try_main(_db, account, _video_url, texts, **_kwargs):
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

    def fake_get_reply(_db, exclude_ids=None, exclude_proxies=None, **_kwargs):
        excluded = set(exclude_ids or [])
        excluded_proxies = set(exclude_proxies or [])
        return next(
            (
                account
                for account in reply_accounts
                if account.id not in excluded
                and str(account.proxy or "").strip() not in excluded_proxies
            ),
            None,
        )

    async def fake_reply(_db, account, _video_url, main_candidates, reply_text, **kwargs):
        nonlocal variant_one_attempts
        variant_index = kwargs["variant_index"]
        reply_account_by_variant[variant_index] = account.id
        reply_events.append(("start", variant_index))
        if variant_index == 1 and variant_one_attempts == 0:
            variant_one_attempts += 1
            reply_events.append(("finish", variant_index))
            return user_schema.ReplyAttemptResult(
                replies=[
                    user_schema.ReplyOutcome(
                        text=reply_text,
                        variant_index=variant_index,
                        matched_main_text=None,
                        parent_comment_id=None,
                        success=False,
                        method=None,
                        error="comment_list_not_open",
                        account_id=account.id,
                        attempted=False,
                    )
                ]
            )
        reply_events.append(("finish", variant_index))
        return user_schema.ReplyAttemptResult(
            replies=[
                user_schema.ReplyOutcome(
                    text=reply_text,
                    variant_index=variant_index,
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
    monkeypatch.setattr(user_schema.asyncio, "sleep", AsyncMock(return_value=None))

    monkeypatch.setattr(user_schema._settings, "PARALLEL_REPLY_VARIANT_WORKERS", 1, raising=False)
    task = SimpleNamespace(settings={"replies": ["r1", "r2", "r3", "r4"]})
    result = await user_schema.execute_user_schema_parallel(
        SimpleNamespace(),
        task,
        "https://www.tiktok.com/@demo/video/1234567890123456789",
        ["m1", "m2", "m3"],
        510,
        boost_enabled=False,
        boost_quantity=0,
    )

    assert result.error is None
    assert {event for event in reply_events if event[0] == "start"} >= {
        ("start", 1),
        ("start", 2),
        ("start", 3),
        ("start", 4),
    }
    start_sequence = [variant for event, variant in reply_events if event == "start"]
    assert start_sequence == [1, 1, 2, 3, 4]
    assert reply_events.index(("start", 2)) > reply_events.index(("finish", 1))
    assert reply_events.index(("start", 3)) > reply_events.index(("finish", 2))
    assert reply_events.index(("start", 4)) > reply_events.index(("finish", 3))
    assert reply_account_by_variant[4] == 205


@pytest.mark.unit
@pytest.mark.asyncio
async def test_parallel_mode_balances_four_reply_workers_across_two_proxies(monkeypatch):
    from db import database as db_database
    from executor import browser_pool, user_schema

    @contextmanager
    def fake_db_context():
        yield SimpleNamespace(no_autoflush=SimpleNamespace())

    main_accounts = [SimpleNamespace(id=101 + i, proxy=f"http://main-proxy-{i}:1000") for i in range(3)]
    reply_accounts = [
        SimpleNamespace(id=201, proxy="http://proxy-a:1000"),
        SimpleNamespace(id=202, proxy="http://proxy-a:1000"),
        SimpleNamespace(id=203, proxy="http://proxy-b:1000"),
        SimpleNamespace(id=204, proxy="http://proxy-b:1000"),
    ]
    reply_proxy_by_variant: dict[int, str] = {}

    def fake_get_main(_db, *, exclude_ids=None, **_kwargs):
        excluded = set(exclude_ids or [])
        return next((account for account in main_accounts if account.id not in excluded), None)

    async def fake_try_main(_db, account, _video_url, texts, **_kwargs):
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

    def fake_get_reply(_db, exclude_ids=None, exclude_proxies=None, **_kwargs):
        excluded = set(exclude_ids or [])
        excluded_proxies = set(exclude_proxies or [])
        return next(
            (
                account
                for account in reply_accounts
                if account.id not in excluded
                and str(account.proxy or "").strip() not in excluded_proxies
            ),
            None,
        )

    async def fake_reply(_db, account, _video_url, main_candidates, reply_text, **kwargs):
        variant_index = kwargs["variant_index"]
        reply_proxy_by_variant[variant_index] = str(account.proxy or "")
        return user_schema.ReplyAttemptResult(
            replies=[
                user_schema.ReplyOutcome(
                    text=reply_text,
                    variant_index=variant_index,
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

    monkeypatch.setattr(user_schema._settings, "PARALLEL_COMMENTING_TARGET_BROWSERS", 4, raising=False)
    monkeypatch.setattr(user_schema._settings, "PARALLEL_COMMENTING_MAX_BROWSERS", 4, raising=False)
    monkeypatch.setattr(user_schema._settings, "PARALLEL_REPLY_VARIANT_WORKERS", 4, raising=False)
    monkeypatch.setattr(user_schema._settings, "PARALLEL_REPLY_START_DELAY_MIN_SECONDS", 0, raising=False)
    monkeypatch.setattr(user_schema._settings, "PARALLEL_REPLY_START_DELAY_MAX_SECONDS", 0, raising=False)
    monkeypatch.setattr(db_database, "get_sync_db_context", fake_db_context)
    monkeypatch.setattr(user_schema, "get_free_account_for_main_comment", fake_get_main)
    monkeypatch.setattr(user_schema, "_try_main_account", fake_try_main)
    monkeypatch.setattr(user_schema, "release_main_account_reservation", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(user_schema, "get_free_account_for_reply", fake_get_reply)
    monkeypatch.setattr(user_schema, "_reply_with_variant_on_all_mains", fake_reply)
    monkeypatch.setattr(user_schema, "_maybe_order_likes", AsyncMock(return_value=None))
    monkeypatch.setattr(browser_pool, "ensure_thread_browser_pool_capacity", lambda _min_browsers: None)
    monkeypatch.setattr(browser_pool, "close_browser_pool", AsyncMock(return_value=None))
    monkeypatch.setattr(user_schema.asyncio, "sleep", AsyncMock(return_value=None))

    task = SimpleNamespace(settings={"replies": ["r1", "r2", "r3", "r4"]})
    result = await user_schema.execute_user_schema_parallel(
        SimpleNamespace(),
        task,
        "https://www.tiktok.com/@demo/video/1234567890123456789",
        ["m1", "m2", "m3"],
        515,
        boost_enabled=False,
        boost_quantity=0,
    )

    assert result.error is None
    assert len(reply_proxy_by_variant) == 4
    assert list(reply_proxy_by_variant.values()).count("http://proxy-a:1000") == 2
    assert list(reply_proxy_by_variant.values()).count("http://proxy-b:1000") == 2


@pytest.mark.unit
@pytest.mark.asyncio
async def test_parallel_mode_stops_after_four_empty_reply_accounts_across_two_proxies(monkeypatch):
    from db import database as db_database
    from executor import browser_pool, user_schema

    @contextmanager
    def fake_db_context():
        yield SimpleNamespace(no_autoflush=SimpleNamespace())

    main_accounts = [SimpleNamespace(id=101 + i, proxy=f"http://main-proxy-{i}:1000") for i in range(3)]
    reply_accounts = [
        SimpleNamespace(id=201, proxy="http://proxy-a:1000"),
        SimpleNamespace(id=202, proxy="http://proxy-a:1000"),
        SimpleNamespace(id=203, proxy="http://proxy-b:1000"),
        SimpleNamespace(id=204, proxy="http://proxy-b:1000"),
    ]
    reply_attempts: list[tuple[int, str]] = []

    def fake_get_main(_db, *, exclude_ids=None, **_kwargs):
        excluded = set(exclude_ids or [])
        return next((account for account in main_accounts if account.id not in excluded), None)

    async def fake_try_main(_db, account, _video_url, texts, **_kwargs):
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

    def fake_get_reply(_db, exclude_ids=None, exclude_proxies=None, **_kwargs):
        excluded = set(exclude_ids or [])
        excluded_proxies = set(exclude_proxies or [])
        return next(
            (
                account
                for account in reply_accounts
                if account.id not in excluded
                and str(account.proxy or "").strip() not in excluded_proxies
            ),
            None,
        )

    async def fake_reply(_db, account, _video_url, _main_candidates, reply_text, **kwargs):
        reply_attempts.append((account.id, str(account.proxy or "")))
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
            saw_comment_rows=True,
            parent_search_max_seen_rows=40,
            parent_search_last_row_count=40,
            parent_search_unique_rows_seen=20,
        )

    monkeypatch.setattr(user_schema._settings, "PARALLEL_COMMENTING_TARGET_BROWSERS", 3, raising=False)
    monkeypatch.setattr(user_schema._settings, "PARALLEL_COMMENTING_MAX_BROWSERS", 3, raising=False)
    monkeypatch.setattr(user_schema._settings, "PARALLEL_REPLY_VARIANT_WORKERS", 2, raising=False)
    monkeypatch.setattr(user_schema._settings, "PARALLEL_REPLY_START_DELAY_MIN_SECONDS", 0, raising=False)
    monkeypatch.setattr(user_schema._settings, "PARALLEL_REPLY_START_DELAY_MAX_SECONDS", 0, raising=False)
    monkeypatch.setattr(db_database, "get_sync_db_context", fake_db_context)
    monkeypatch.setattr(user_schema, "get_free_account_for_main_comment", fake_get_main)
    monkeypatch.setattr(user_schema, "_try_main_account", fake_try_main)
    monkeypatch.setattr(user_schema, "release_main_account_reservation", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(user_schema, "get_free_account_for_reply", fake_get_reply)
    monkeypatch.setattr(user_schema, "_reply_with_variant_on_all_mains", fake_reply)
    monkeypatch.setattr(user_schema, "_maybe_order_likes", AsyncMock(return_value=None))
    monkeypatch.setattr(browser_pool, "ensure_thread_browser_pool_capacity", lambda _value: None)
    monkeypatch.setattr(browser_pool, "close_browser_pool", AsyncMock(return_value=None))
    monkeypatch.setattr(user_schema.asyncio, "sleep", AsyncMock(return_value=None))

    result = await user_schema.execute_user_schema_parallel(
        SimpleNamespace(),
        SimpleNamespace(settings={"replies": ["r1"]}),
        "https://www.tiktok.com/@demo/video/1234567890123456789",
        ["m1", "m2", "m3"],
        516,
        boost_enabled=False,
        boost_quantity=0,
    )

    assert result.error == "remaining_main_comments_not_visible_after_four_accounts"
    assert len(reply_attempts) == 4
    assert len({account_id for account_id, _proxy in reply_attempts}) == 4
    assert [proxy for _account_id, proxy in reply_attempts].count("http://proxy-a:1000") == 2
    assert [proxy for _account_id, proxy in reply_attempts].count("http://proxy-b:1000") == 2
    assert result.reply_visibility["confident_empty_accounts"] == 4
    assert result.to_dict()["status"] == "fail"


@pytest.mark.unit
@pytest.mark.asyncio
async def test_parallel_mode_stop_cancels_dynamic_reply_workers(monkeypatch):
    from db import database as db_database
    from executor import browser_pool, user_schema

    @contextmanager
    def fake_db_context():
        yield SimpleNamespace(no_autoflush=SimpleNamespace())

    main_accounts = [SimpleNamespace(id=101 + i, proxy=None) for i in range(3)]
    reply_accounts = [SimpleNamespace(id=201 + i, proxy=None) for i in range(3)]
    reply_started: list[int] = []
    reply_cancelled: list[int] = []
    close_calls: list[str] = []
    likes_called = False

    def fake_get_main(_db, *, exclude_ids=None, **_kwargs):
        excluded = set(exclude_ids or [])
        return next((account for account in main_accounts if account.id not in excluded), None)

    async def fake_try_main(_db, account, _video_url, texts, **_kwargs):
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

    async def fake_reply(_db, _account, _video_url, _main_candidates, _reply_text, **kwargs):
        variant_index = kwargs["variant_index"]
        reply_started.append(variant_index)
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            reply_cancelled.append(variant_index)
            raise

    async def fake_close_pool():
        close_calls.append("close")

    async def fake_likes(*_args, **_kwargs):
        nonlocal likes_called
        likes_called = True

    monkeypatch.setattr(db_database, "get_sync_db_context", fake_db_context)
    monkeypatch.setattr(user_schema, "get_free_account_for_main_comment", fake_get_main)
    monkeypatch.setattr(user_schema, "_try_main_account", fake_try_main)
    monkeypatch.setattr(user_schema, "release_main_account_reservation", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(user_schema, "get_free_account_for_reply", fake_get_reply)
    monkeypatch.setattr(user_schema, "_reply_with_variant_on_all_mains", fake_reply)
    monkeypatch.setattr(user_schema, "_maybe_order_likes", fake_likes)
    monkeypatch.setattr(browser_pool, "ensure_thread_browser_pool_capacity", lambda _min_browsers: None)
    monkeypatch.setattr(browser_pool, "close_browser_pool", fake_close_pool)

    task = SimpleNamespace(settings={"replies": ["r1", "r2", "r3"]})
    result = await user_schema.execute_user_schema_parallel(
        SimpleNamespace(),
        task,
        "https://www.tiktok.com/@demo/video/1234567890123456789",
        ["m1", "m2", "m3"],
        511,
        boost_enabled=True,
        boost_quantity=100,
        stop_requested=lambda: len(reply_started) >= 1,
    )

    assert result.error == "stopped_by_user"
    assert reply_started == [1]
    assert reply_cancelled == [1]
    assert close_calls == ["close", "close"]
    assert likes_called is False


@pytest.mark.unit
@pytest.mark.asyncio
async def test_parallel_mode_stop_after_reply_selection_does_not_start_reply(monkeypatch):
    from db import database as db_database
    from executor import browser_pool, user_schema

    @contextmanager
    def fake_db_context():
        yield SimpleNamespace(no_autoflush=SimpleNamespace())

    main_accounts = [SimpleNamespace(id=101 + i, proxy=None) for i in range(3)]
    reply_account = SimpleNamespace(id=201, proxy=None)
    stop_now = False
    released_reply_accounts: list[int] = []
    close_calls: list[str] = []

    def fake_get_main(_db, *, exclude_ids=None, **_kwargs):
        excluded = set(exclude_ids or [])
        return next((account for account in main_accounts if account.id not in excluded), None)

    async def fake_try_main(_db, account, _video_url, texts, **_kwargs):
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
        nonlocal stop_now
        stop_now = True
        excluded = set(exclude_ids or [])
        return None if reply_account.id in excluded else reply_account

    async def fake_reply(*_args, **_kwargs):
        raise AssertionError("reply publish must not start after stop flips during account selection")

    async def fake_close_pool():
        close_calls.append("close")

    monkeypatch.setattr(db_database, "get_sync_db_context", fake_db_context)
    monkeypatch.setattr(user_schema, "get_free_account_for_main_comment", fake_get_main)
    monkeypatch.setattr(user_schema, "_try_main_account", fake_try_main)
    monkeypatch.setattr(user_schema, "release_main_account_reservation", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(user_schema, "get_free_account_for_reply", fake_get_reply)
    monkeypatch.setattr(
        user_schema,
        "release_reply_account_reservation",
        lambda account_id: released_reply_accounts.append(account_id),
    )
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
        516,
        boost_enabled=False,
        boost_quantity=0,
        stop_requested=lambda: stop_now,
    )

    assert result.error == "stopped_by_user"
    assert released_reply_accounts == [201]
    assert close_calls == ["close", "close"]


@pytest.mark.unit
@pytest.mark.asyncio
async def test_parallel_mode_stop_preserves_partial_submitted_reply_outcomes(monkeypatch):
    from db import database as db_database
    from executor import browser_pool, user_schema

    @contextmanager
    def fake_db_context():
        yield SimpleNamespace(no_autoflush=SimpleNamespace())

    main_accounts = [SimpleNamespace(id=101 + i, proxy=None) for i in range(3)]
    reply_accounts = [SimpleNamespace(id=201, proxy=None)]
    stop_now = False
    close_calls: list[str] = []
    likes_called = False

    def fake_get_main(_db, *, exclude_ids=None, **_kwargs):
        excluded = set(exclude_ids or [])
        return next((account for account in main_accounts if account.id not in excluded), None)

    async def fake_try_main(_db, account, _video_url, texts, **_kwargs):
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
        nonlocal stop_now
        stop_now = True
        submitted_main = main_candidates[0]
        return user_schema.ReplyAttemptResult(
            replies=[
                user_schema.ReplyOutcome(
                    text=reply_text,
                    variant_index=kwargs["variant_index"],
                    matched_main_text=submitted_main.text,
                    parent_comment_id=submitted_main.comment_id,
                    success=False,
                    method="hybrid_dom",
                    error="stealth_reject_empty_body",
                    account_id=account.id,
                    attempted=True,
                    submitted_unconfirmed=True,
                    parent_key=user_schema._main_parent_key(submitted_main),
                    counts_as_submitted=True,
                )
            ],
            found_mains=[submitted_main],
            searched=True,
        )

    async def fake_close_pool():
        close_calls.append("close")

    async def fake_likes(*_args, **_kwargs):
        nonlocal likes_called
        likes_called = True

    monkeypatch.setattr(db_database, "get_sync_db_context", fake_db_context)
    monkeypatch.setattr(user_schema, "get_free_account_for_main_comment", fake_get_main)
    monkeypatch.setattr(user_schema, "_try_main_account", fake_try_main)
    monkeypatch.setattr(user_schema, "release_main_account_reservation", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(user_schema, "get_free_account_for_reply", fake_get_reply)
    monkeypatch.setattr(user_schema, "_reply_with_variant_on_all_mains", fake_reply)
    monkeypatch.setattr(user_schema, "_maybe_order_likes", fake_likes)
    monkeypatch.setattr(browser_pool, "ensure_thread_browser_pool_capacity", lambda _min_browsers: None)
    monkeypatch.setattr(browser_pool, "close_browser_pool", fake_close_pool)

    task = SimpleNamespace(settings={"replies": ["r1"]})
    result = await user_schema.execute_user_schema_parallel(
        SimpleNamespace(),
        task,
        "https://www.tiktok.com/@demo/video/1234567890123456789",
        ["m1", "m2", "m3"],
        512,
        boost_enabled=True,
        boost_quantity=100,
        stop_requested=lambda: stop_now,
    )

    assert result.error == "stopped_by_user"
    assert len(result.replies) == 1
    assert result.replies[0].submitted_unconfirmed is True
    assert result.replies[0].error == "stealth_reject_empty_body"
    assert close_calls == ["close", "close"]
    assert likes_called is False


@pytest.mark.unit
@pytest.mark.asyncio
async def test_parallel_mode_no_free_reply_does_not_cancel_active_dynamic_workers(monkeypatch):
    from db import database as db_database
    from executor import browser_pool, user_schema

    @contextmanager
    def fake_db_context():
        yield SimpleNamespace(no_autoflush=SimpleNamespace())

    main_accounts = [SimpleNamespace(id=101 + i, proxy=None) for i in range(3)]
    reply_accounts = [SimpleNamespace(id=201 + i, proxy=None) for i in range(3)]
    reply_select_calls = 0
    reply_started: list[int] = []
    reply_completed: list[int] = []
    reply_cancelled: list[int] = []
    close_calls: list[str] = []

    def fake_get_main(_db, *, exclude_ids=None, **_kwargs):
        excluded = set(exclude_ids or [])
        return next((account for account in main_accounts if account.id not in excluded), None)

    async def fake_try_main(_db, account, _video_url, texts, **_kwargs):
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
        nonlocal reply_select_calls
        reply_select_calls += 1
        if reply_select_calls == 1:
            return None
        excluded = set(exclude_ids or [])
        return next((account for account in reply_accounts if account.id not in excluded), None)

    async def fake_reply(_db, _account, _video_url, _main_candidates, _reply_text, **kwargs):
        variant_index = kwargs["variant_index"]
        reply_started.append(variant_index)
        try:
            await asyncio.sleep(0.01)
        except asyncio.CancelledError:
            reply_cancelled.append(variant_index)
            raise
        reply_completed.append(variant_index)
        return user_schema.ReplyAttemptResult(
            replies=[
                user_schema.ReplyOutcome(
                    text=_reply_text,
                    variant_index=variant_index,
                    matched_main_text=main.text,
                    parent_comment_id=main.comment_id,
                    success=True,
                    method="hybrid_dom",
                    error=None,
                    account_id=_account.id,
                    attempted=True,
                )
                for main in _main_candidates
            ],
            found_mains=list(_main_candidates),
            searched=True,
        )

    async def fake_close_pool():
        close_calls.append("close")

    monkeypatch.setattr(user_schema._settings, "PARALLEL_COMMENTING_TARGET_BROWSERS", 4, raising=False)
    monkeypatch.setattr(user_schema._settings, "PARALLEL_COMMENTING_MAX_BROWSERS", 4, raising=False)
    monkeypatch.setattr(user_schema._settings, "PARALLEL_REPLY_VARIANT_WORKERS", 4, raising=False)
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

    task = SimpleNamespace(settings={"replies": ["r1", "r2", "r3", "r4"]})
    result = await user_schema.execute_user_schema_parallel(
        SimpleNamespace(),
        task,
        "https://www.tiktok.com/@demo/video/1234567890123456789",
        ["m1", "m2", "m3"],
        513,
        boost_enabled=False,
        boost_quantity=0,
    )

    assert result.error == "reply_publication_incomplete"
    assert sorted(reply_started) == [2, 3, 4]
    assert set(reply_completed) == set(reply_started)
    assert reply_cancelled == []
    assert any(outcome.error == "no_free_reply_accounts" for outcome in result.replies)
    assert any(
        isinstance(warning, dict) and warning.get("code") == "parallel_reply_incomplete"
        for warning in result.warnings
    )
    assert close_calls == ["close", "close"]


@pytest.mark.unit
@pytest.mark.asyncio
async def test_parallel_mode_empty_threshold_lets_active_dynamic_workers_finish(monkeypatch):
    from db import database as db_database
    from executor import browser_pool, user_schema

    @contextmanager
    def fake_db_context():
        yield SimpleNamespace(no_autoflush=SimpleNamespace())

    main_accounts = [SimpleNamespace(id=101 + i, proxy=None) for i in range(8)]
    cancelled_main_accounts: list[int] = []
    close_calls: list[str] = []

    def fake_get_main(_db, *, exclude_ids=None, **_kwargs):
        excluded = set(exclude_ids or [])
        return next((account for account in main_accounts if account.id not in excluded), None)

    async def fake_try_main(_db, account, _video_url, _texts, **_kwargs):
        if account.id == 101:
            try:
                await asyncio.sleep(0.05)
            except asyncio.CancelledError:
                cancelled_main_accounts.append(account.id)
                raise
        return [], "input_not_found"

    async def fake_close_pool():
        close_calls.append("close")

    monkeypatch.setattr(user_schema._settings, "PARALLEL_COMMENTING_TARGET_BROWSERS", 3, raising=False)
    monkeypatch.setattr(user_schema._settings, "PARALLEL_COMMENTING_MAX_BROWSERS", 3, raising=False)
    monkeypatch.setattr(user_schema._settings, "PARALLEL_COMMENTING_MAX_MAIN_ATTEMPTS", 6, raising=False)
    monkeypatch.setattr(db_database, "get_sync_db_context", fake_db_context)
    monkeypatch.setattr(user_schema, "get_free_account_for_main_comment", fake_get_main)
    monkeypatch.setattr(user_schema, "_try_main_account", fake_try_main)
    monkeypatch.setattr(user_schema, "release_main_account_reservation", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(user_schema, "_maybe_order_likes", AsyncMock(return_value=None))
    monkeypatch.setattr(browser_pool, "ensure_thread_browser_pool_capacity", lambda _min_browsers: None)
    monkeypatch.setattr(browser_pool, "close_browser_pool", fake_close_pool)

    task = SimpleNamespace(settings={"replies": ["r1"]})
    result = await user_schema.execute_user_schema_parallel(
        SimpleNamespace(),
        task,
        "https://www.tiktok.com/@demo/video/1234567890123456789",
        ["m1", "m2", "m3"],
        514,
        boost_enabled=False,
        boost_quantity=0,
    )

    assert result.error == "parallel_main_no_publications_after_two_batches"
    assert cancelled_main_accounts == []
    assert close_calls == ["close"]


@pytest.mark.unit
@pytest.mark.asyncio
async def test_parallel_mode_preserves_active_main_publication_after_empty_threshold(monkeypatch):
    from db import database as db_database
    from executor import browser_pool, user_schema

    @contextmanager
    def fake_db_context():
        yield SimpleNamespace(no_autoflush=SimpleNamespace())

    main_accounts = [SimpleNamespace(id=101 + i, proxy=None) for i in range(8)]
    cancelled_main_accounts: list[int] = []
    close_calls: list[str] = []

    def fake_get_main(_db, *, exclude_ids=None, **_kwargs):
        excluded = set(exclude_ids or [])
        return next((account for account in main_accounts if account.id not in excluded), None)

    async def fake_try_main(_db, account, _video_url, texts, **_kwargs):
        if account.id == 101:
            try:
                await asyncio.sleep(0.05)
            except asyncio.CancelledError:
                cancelled_main_accounts.append(account.id)
                raise
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
        return [], "input_not_found"

    async def fake_close_pool():
        close_calls.append("close")

    monkeypatch.setattr(user_schema._settings, "PARALLEL_COMMENTING_TARGET_BROWSERS", 3, raising=False)
    monkeypatch.setattr(user_schema._settings, "PARALLEL_COMMENTING_MAX_BROWSERS", 3, raising=False)
    monkeypatch.setattr(user_schema._settings, "PARALLEL_COMMENTING_MAX_MAIN_ATTEMPTS", 6, raising=False)
    monkeypatch.setattr(db_database, "get_sync_db_context", fake_db_context)
    monkeypatch.setattr(user_schema, "get_free_account_for_main_comment", fake_get_main)
    monkeypatch.setattr(user_schema, "_try_main_account", fake_try_main)
    monkeypatch.setattr(user_schema, "release_main_account_reservation", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(user_schema, "_maybe_order_likes", AsyncMock(return_value=None))
    monkeypatch.setattr(browser_pool, "ensure_thread_browser_pool_capacity", lambda _min_browsers: None)
    monkeypatch.setattr(browser_pool, "close_browser_pool", fake_close_pool)

    task = SimpleNamespace(settings={"replies": [], "reply_count": 0})
    result = await user_schema.execute_user_schema_parallel(
        SimpleNamespace(),
        task,
        "https://www.tiktok.com/@demo/video/1234567890123456789",
        ["m1", "m2", "m3"],
        515,
        boost_enabled=False,
        boost_quantity=0,
    )

    assert result.error is None
    assert [main.account_id for main in result.published_mains] == [101, 101, 101]
    assert [main.text for main in result.published_mains] == ["m1", "m2", "m3"]
    assert cancelled_main_accounts == []
    assert close_calls == ["close"]


@pytest.mark.unit
@pytest.mark.asyncio
async def test_parallel_mode_carries_submitted_parent_keys_across_reply_retries(monkeypatch):
    from db import database as db_database
    from executor import browser_pool, user_schema

    @contextmanager
    def fake_db_context():
        yield SimpleNamespace(no_autoflush=SimpleNamespace())

    main_accounts = [
        SimpleNamespace(id=101, proxy=None),
        SimpleNamespace(id=102, proxy=None),
        SimpleNamespace(id=103, proxy=None),
    ]
    reply_accounts = [SimpleNamespace(id=201, proxy=None), SimpleNamespace(id=202, proxy=None)]
    reply_calls = []
    selected_main_ids: set[int] = set()
    select_lock = threading.Lock()

    def fake_get_main(_db, *, exclude_ids=None, **_kwargs):
        excluded = set(exclude_ids or [])
        with select_lock:
            candidate = next(
                (
                    account
                    for account in main_accounts
                    if account.id not in excluded and account.id not in selected_main_ids
                ),
                None,
            )
            if candidate is not None:
                selected_main_ids.add(candidate.id)
            return candidate

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
        first = main_candidates[0]
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
                        counts_as_submitted=True,
                    )
                ],
                found_mains=list(main_candidates),
                submitted_parent_keys_count=1,
                searched=True,
            )
        assert first.comment_id in submitted_keys
        pending = [
            main
            for main in main_candidates
            if str(main.comment_id or "").strip() not in submitted_keys
        ]
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
                for main in pending
            ],
            found_mains=list(main_candidates),
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
    assert [outcome.parent_comment_id for outcome in result.replies] == [
        "cid-101-m1",
        "cid-101-m2",
        "cid-102-m2",
        "cid-102-m1",
        "cid-103-m1",
        "cid-103-m2",
    ]
    assert result.replies[0].submitted_unconfirmed is True


@pytest.mark.unit
@pytest.mark.asyncio
async def test_parallel_reply_timeout_preserves_streamed_submitted_parent(monkeypatch):
    from db import database as db_database
    from executor import browser_pool, user_schema

    @contextmanager
    def fake_db_context():
        yield SimpleNamespace(no_autoflush=SimpleNamespace())

    main_accounts = [SimpleNamespace(id=101, proxy=None), SimpleNamespace(id=102, proxy=None), SimpleNamespace(id=103, proxy=None)]
    reply_accounts = [SimpleNamespace(id=201, proxy=None), SimpleNamespace(id=202, proxy=None)]
    selected_main_ids: set[int] = set()
    reply_calls = []

    def fake_get_main(_db, *, exclude_ids=None, **_kwargs):
        excluded = set(exclude_ids or [])
        candidate = next(
            (
                account
                for account in main_accounts
                if account.id not in excluded and account.id not in selected_main_ids
            ),
            None,
        )
        if candidate is not None:
            selected_main_ids.add(candidate.id)
        return candidate

    async def fake_try_main(_db, account, _video_url, texts, **_kwargs):
        return [
            user_schema.PublishedMain(
                text=text,
                comment_id=f"cid-{account.id}-{idx}",
                network_confirmed=True,
                local_confirmed=True,
                method="hybrid_dom",
                account_id=account.id,
                tiktok_username="main_user",
            )
            for idx, text in enumerate(texts, start=1)
        ], None

    def fake_get_reply(_db, exclude_ids=None, **_kwargs):
        excluded = set(exclude_ids or [])
        return next((account for account in reply_accounts if account.id not in excluded), None)

    async def fake_reply(_db, account, _video_url, main_candidates, reply_text, **kwargs):
        already = set(kwargs["already_replied_parent_keys"])
        reply_calls.append((account.id, already))
        first = main_candidates[0]
        if account.id == 201:
            outcome = user_schema.ReplyOutcome(
                text=reply_text,
                variant_index=kwargs["variant_index"],
                matched_main_text=first.text,
                parent_comment_id=first.comment_id,
                success=False,
                method="hybrid_dom",
                error="no_network_response_after_publish_request",
                account_id=account.id,
                attempted=True,
                submitted_unconfirmed=True,
                counts_as_submitted=True,
            )
            kwargs["on_submitted_outcome"](outcome)
            raise asyncio.TimeoutError()

        assert first.comment_id in already
        pending = [
            main
            for main in main_candidates
            if str(main.comment_id or "").strip() not in already
        ]
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
                for main in pending
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
    monkeypatch.setattr(user_schema.asyncio, "sleep", AsyncMock(return_value=None))

    task = SimpleNamespace(settings={"replies": ["reply one"]})
    result = await user_schema.execute_user_schema_parallel(
        SimpleNamespace(),
        task,
        "https://www.tiktok.com/@demo/video/1234567890123456789",
        ["m1", "m2"],
        507,
        boost_enabled=False,
        boost_quantity=0,
    )

    assert result.error is None
    assert reply_calls[0] == (201, set())
    assert reply_calls[1][0] == 202
    assert "cid-101-1" in reply_calls[1][1]
    parent_ids = [outcome.parent_comment_id for outcome in result.replies]
    assert parent_ids.count("cid-101-1") == 1
    assert len(parent_ids) == 6
    assert result.replies[0].submitted_unconfirmed is True


@pytest.mark.unit
@pytest.mark.asyncio
async def test_parallel_mode_retries_reply_variant_when_only_one_of_three_mains_found(monkeypatch):
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
def test_reply_parent_dedup_key_uses_observed_physical_parent_before_expected_account():
    from executor import user_schema

    first = user_schema.PublishedMain(
        text="same text",
        comment_id=None,
        network_confirmed=False,
        local_confirmed=True,
        method="hybrid_dom",
        account_id=101,
        tiktok_username="expected_101",
    )
    second = user_schema.PublishedMain(
        text="same text",
        comment_id=None,
        network_confirmed=False,
        local_confirmed=True,
        method="hybrid_dom",
        account_id=102,
        tiktok_username="expected_102",
    )
    physical_key = user_schema._physical_parent_key_from_observed(
        text="same text",
        author="visible_parent_author",
    )

    first_anchor = user_schema.MainAnchor(
        matched_text=first.text,
        parent_comment_id=None,
        open_text="same text",
        found_by="text",
        source_main=first,
        parent_key=user_schema._main_parent_key(first),
        matched_author="visible_parent_author",
        physical_parent_key=physical_key,
    )
    second_anchor = user_schema.MainAnchor(
        matched_text=second.text,
        parent_comment_id=None,
        open_text="same text",
        found_by="text",
        source_main=second,
        parent_key=user_schema._main_parent_key(second),
        matched_author="visible_parent_author",
        physical_parent_key=physical_key,
    )

    first_key = user_schema._reply_parent_dedup_key_for_anchor(first, first_anchor, anchor_text="same text")
    second_key = user_schema._reply_parent_dedup_key_for_anchor(second, second_anchor, anchor_text="same text")

    assert first_key == physical_key
    assert second_key == physical_key
    assert first_key != user_schema._main_parent_key(first)
    assert second_key != user_schema._main_parent_key(second)
    assert user_schema._reply_attempt_key(first_key, "reply text") == user_schema._reply_attempt_key(second_key, "reply text")


@pytest.mark.unit
def test_reply_parent_dedup_key_prefers_verified_id_over_physical_parent():
    from executor import user_schema

    main = user_schema.PublishedMain(
        text="same text",
        comment_id="cid-main",
        network_confirmed=True,
        local_confirmed=False,
        method="hybrid_dom",
        account_id=101,
        tiktok_username="expected_101",
    )
    anchor = user_schema.MainAnchor(
        matched_text=main.text,
        parent_comment_id=None,
        open_text="same text",
        found_by="id",
        source_main=main,
        parent_key=user_schema._main_parent_key(main),
        physical_parent_key=user_schema._physical_parent_key_from_observed(
            text="same text",
            author="visible_parent_author",
        ),
    )

    assert user_schema._reply_parent_dedup_key_for_anchor(main, anchor, anchor_text="same text") == "cid-main"


@pytest.mark.unit
def test_reply_visibility_probe_helpers_keep_baseline_count():
    from executor import user_schema

    assert user_schema._reply_visibility_probe_found({"found": True, "count": 2})
    assert user_schema._reply_visibility_probe_count({"found": False, "count": 2}) == 2
    assert user_schema._reply_visibility_probe_count(True) == 1
    assert user_schema._reply_visibility_probe_count(False) == 0


@pytest.mark.unit
def test_reply_attempt_key_for_outcome_never_stringifies_parent_key_set():
    from executor import user_schema

    outcome = user_schema.ReplyOutcome(
        text="reply text",
        variant_index=1,
        matched_main_text="main text",
        parent_comment_id="1234567890123",
        success=True,
        method="hybrid_dom",
        error=None,
        account_id=77,
        attempted=True,
    )

    assert user_schema._reply_attempt_key_for_outcome(outcome) == (
        "parent:1234567890123:reply:reply text"
    )


@pytest.mark.unit
def test_submitted_reply_attempt_keys_use_parent_and_reply_text():
    from executor import user_schema

    physical_key = user_schema._physical_parent_key_from_observed(
        text="main text",
        author="visible_author",
    )
    main = user_schema.PublishedMain(
        text="main text",
        comment_id=None,
        network_confirmed=False,
        local_confirmed=True,
        method="hybrid_dom",
        account_id=101,
        tiktok_username="visible_author",
        physical_parent_key=physical_key,
    )
    outcome = user_schema.ReplyOutcome(
        text="same reply",
        variant_index=1,
        matched_main_text="main text",
        parent_comment_id=None,
        success=True,
        method="hybrid_dom",
        error=None,
        account_id=201,
        attempted=True,
        physical_parent_key=physical_key,
    )

    keys = user_schema._reply_outcomes_submitted_reply_keys_for_expected_mains(
        [outcome],
        [main],
    )

    assert user_schema._reply_attempt_key(physical_key, "same reply") in keys
    assert all(key.endswith(":reply:same reply") for key in keys)


@pytest.mark.unit
def test_reply_local_confirmation_uses_fast_baseline_probe():
    from executor import user_schema
    import inspect

    source = inspect.getsource(user_schema._reply_with_variant_on_all_mains)

    assert "return_state=True" in source
    assert "reply_baseline_count = _reply_visibility_probe_count(already_present_probe)" in source
    assert "min_count_exclusive=reply_baseline_count" in source
    assert "fast=_is_parallel_reply_correlation(sub_corr)" in source


@pytest.mark.unit
def test_reply_loop_has_short_fresh_search_after_action_and_effective_status_trace():
    from executor import user_schema
    import inspect

    source = inspect.getsource(user_schema._reply_with_variant_on_all_mains)

    assert "short_fresh_search_after_action_used = False" in source
    assert "reply_parent_short_fresh_search_after_action" in source
    assert "transport_status=publish_result.status" in source
    assert "effective_status=effective_status" in source


@pytest.mark.unit
def test_parallel_reply_uses_task_global_attempt_locks_for_same_reply_text():
    from executor import user_schema
    import inspect

    parallel_source = inspect.getsource(user_schema.execute_user_schema_parallel)
    module_source = inspect.getsource(user_schema)

    assert "reply_task_attempt_locks: set[str] = set()" in parallel_source
    assert "reply_task_inflight_locks: set[str] = set()" in parallel_source
    assert "soft_submitted_reply_keys=reply_task_attempt_locks" in parallel_source
    assert "inflight_reply_keys=reply_task_inflight_locks" in parallel_source
    assert "_reply_outcomes_submitted_reply_keys_for_expected_mains" in parallel_source
    assert "full_reply_task_attempt_locks: set[str] = set()" in module_source
    assert "soft_submitted_reply_keys=full_reply_task_attempt_locks" in module_source
    assert "inflight_reply_keys=full_reply_task_inflight_locks" in module_source


@pytest.mark.unit
def test_publish_reply_retry_rebinds_target_and_caps_send_recovery():
    from executor import publish_action
    import inspect

    source = inspect.getsource(publish_action._publish_via_current_input)
    reply_source = inspect.getsource(publish_action.publish_reply_hybrid)

    assert "typing_retry_reply_target_rebound" in source
    assert "find_reply_composer_for_parent(" in source
    assert "reply_send_deadline = time.monotonic() + (3.5 if fast_publish else 4.5)" in source
    assert "min(_PARALLEL_POST_RESPONSE_WAIT_SECONDS, 1.4)" in source
    assert "1000 if reply_scoped and fast_publish else 3000" in source
    assert "1000 if reply_scoped and fast_publish else 5000" in source
    assert "2.2 if fast_publish else 3.0" in reply_source
    assert "reply_send_recover_send_only" in reply_source
    assert "_recover_reply_send_only(" in reply_source


@pytest.mark.unit
def test_reply_outcome_physical_parent_key_carries_submitted_state():
    from executor import user_schema

    physical_key = user_schema._physical_parent_key_from_observed(
        text="same text",
        author="visible_parent_author",
    )
    found_main = user_schema.PublishedMain(
        text="same text",
        comment_id=None,
        network_confirmed=False,
        local_confirmed=True,
        method="hybrid_dom",
        account_id=101,
        tiktok_username="expected_101",
        physical_parent_key=physical_key,
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
        parent_key=physical_key,
        physical_parent_key=physical_key,
    )

    submitted = user_schema._reply_outcomes_submitted_parent_keys_for_expected_mains(
        [outcome],
        [found_main],
    )

    assert submitted == {physical_key}
    assert user_schema._pending_expected_mains_for_reply_variant([found_main], submitted) == []


@pytest.mark.unit
def test_merge_distinct_main_candidates_preserves_new_physical_parent_key():
    from executor import user_schema

    physical_key = user_schema._physical_parent_key_from_observed(
        text="same text",
        author="visible_parent_author",
    )
    original = user_schema.PublishedMain(
        text="same text",
        comment_id=None,
        network_confirmed=False,
        local_confirmed=True,
        method="hybrid_dom",
        account_id=101,
        tiktok_username="expected_101",
    )
    found = user_schema.PublishedMain(
        text="same text",
        comment_id=None,
        network_confirmed=False,
        local_confirmed=True,
        method="hybrid_dom",
        account_id=101,
        tiktok_username="visible_parent_author",
        physical_parent_key=physical_key,
    )

    merged = user_schema._merge_distinct_main_candidates([original, found])

    assert len(merged) == 1
    assert merged[0].physical_parent_key == physical_key


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
def test_reply_outcome_bare_text_key_does_not_complete_duplicate_no_cid_parents():
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
        parent_key="same text",
    )

    submitted = user_schema._reply_outcomes_submitted_parent_keys_for_expected_mains(
        [outcome],
        [first, second],
    )

    assert submitted == set()
    assert user_schema._pending_expected_mains_for_reply_variant([first, second], submitted) == [first, second]


@pytest.mark.unit
def test_reply_outcome_bare_text_key_does_not_complete_unowned_duplicate_parents():
    from executor import user_schema

    first = user_schema.PublishedMain(
        text="same text",
        comment_id=None,
        network_confirmed=False,
        local_confirmed=True,
        method="hybrid_dom",
        account_id=None,
        tiktok_username=None,
    )
    second = user_schema.PublishedMain(
        text="same text",
        comment_id=None,
        network_confirmed=False,
        local_confirmed=True,
        method="hybrid_dom",
        account_id=None,
        tiktok_username=None,
    )
    outcome = user_schema.ReplyOutcome(
        text="reply",
        variant_index=1,
        matched_main_text=None,
        parent_comment_id=None,
        success=True,
        method="hybrid_dom",
        error=None,
        account_id=201,
        attempted=True,
        parent_key="same text",
    )

    submitted = user_schema._reply_outcomes_submitted_parent_keys_for_expected_mains(
        [outcome],
        [first, second],
    )

    assert submitted == set()
    assert user_schema._pending_expected_mains_for_reply_variant([first, second], {"same text"}) == [first, second]


@pytest.mark.unit
def test_reply_pending_parent_report_keeps_variant_pending_until_every_parent_is_covered():
    from executor import user_schema

    first = user_schema.PublishedMain(
        text="main one",
        comment_id="cid-1",
        network_confirmed=True,
        local_confirmed=True,
        method="hybrid_dom",
        account_id=101,
        tiktok_username="main_101",
    )
    second = user_schema.PublishedMain(
        text="main two",
        comment_id="cid-2",
        network_confirmed=True,
        local_confirmed=True,
        method="hybrid_dom",
        account_id=102,
        tiktok_username="main_102",
    )

    report = user_schema._reply_pending_parent_report(
        ["reply one"],
        [first, second],
        {1: {"cid-1"}},
    )

    assert report == [
        {
            "variant_index": 1,
            "reply_text": "reply one",
            "pending_count": 1,
            "pending_parents": [
                {
                    "text": "main two",
                    "comment_id": "cid-2",
                    "account_id": 102,
                    "tiktok_username": "main_102",
                    "parent_key": "cid-2",
                }
            ],
        }
    ]


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
        (1, 202, ["main one", "main two"]),
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
@pytest.mark.asyncio
async def test_maybe_order_likes_allows_submitted_main_without_observer(monkeypatch):
    from executor import user_schema
    from services import boost_project_client

    ordered = []

    async def fake_order(video_url, username, *, quantity):
        ordered.append((video_url, username, quantity))
        return True, 556, None

    monkeypatch.setattr(boost_project_client, "is_boost_enabled", lambda: True)
    monkeypatch.setattr(boost_project_client, "order_tiktok_comment_likes", fake_order)

    result = user_schema.UserSchemaResult(boost_enabled=True, boost_quantity=100)
    result.main_found_by_observer = False
    result.published_mains.append(
        user_schema.PublishedMain(
            text="submitted main",
            comment_id=None,
            network_confirmed=False,
            local_confirmed=True,
            method="hybrid_dom",
            account_id=101,
            tiktok_username="main_user",
        )
    )

    await user_schema._maybe_order_likes(
        result,
        video_url="https://www.tiktok.com/@demo/video/1234567890123456789",
        task_id=255,
        boost_enabled=True,
        boost_quantity=100,
    )

    assert ordered == [
        (
            "https://www.tiktok.com/@demo/video/1234567890123456789",
            "main_user",
            100,
        )
    ]
    assert result.boost_order_id == 556


@pytest.mark.unit
def test_unconfirmed_reply_without_visible_confirmation_does_not_count_for_likes():
    from executor import user_schema

    assert user_schema._reply_outcome_counts_for_likes(
        user_schema.ReplyOutcome(
            text="reply",
            variant_index=1,
            matched_main_text="main",
            parent_comment_id=None,
            success=False,
            method="hybrid_dom",
            error="stealth_reject_empty_body",
            account_id=201,
            attempted=True,
            submitted_unconfirmed=True,
        )
    ) is False
    assert user_schema._reply_outcome_counts_for_likes(
        user_schema.ReplyOutcome(
            text="reply",
            variant_index=1,
            matched_main_text="main",
            parent_comment_id=None,
            success=False,
            method="hybrid_dom",
            error=None,
            account_id=201,
            attempted=True,
            submitted_unconfirmed=True,
            local_confirmed=True,
            reply_confirmation="local_visible_near_parent",
            counts_as_submitted=True,
        )
    ) is True
    assert user_schema._reply_outcome_counts_for_likes(
        user_schema.ReplyOutcome(
            text="reply",
            variant_index=1,
            matched_main_text=None,
            parent_comment_id=None,
            success=False,
            method=None,
            error="comment_list_not_open",
            account_id=201,
            attempted=True,
        )
    ) is False


@pytest.mark.unit
@pytest.mark.asyncio
async def test_parallel_likes_each_found_orders_unique_answered_authors(monkeypatch):
    from executor import user_schema
    from services import boost_project_client

    ordered: list[tuple[str, int]] = []

    async def fake_order(_video_url, username, *, quantity):
        ordered.append((username, quantity))
        return True, len(ordered), None

    monkeypatch.setattr(boost_project_client, "is_boost_enabled", lambda: True)
    monkeypatch.setattr(boost_project_client, "order_tiktok_comment_likes", fake_order)

    result = user_schema.UserSchemaResult(boost_enabled=True, boost_quantity=50)
    result.main_found_by_observer = True
    result.published_mains.extend(
        [
            user_schema.PublishedMain("m1", "cid1", True, True, "hybrid_dom", 101, "main_a"),
            user_schema.PublishedMain("m2", "cid2", True, True, "hybrid_dom", 102, "main_b"),
            user_schema.PublishedMain("m3", "cid3", True, True, "hybrid_dom", 103, "main_c"),
        ]
    )
    result.replies.extend(
        [
            user_schema.ReplyOutcome("r1", 1, "m1", "cid1", True, "hybrid_dom", None, 201, matched_parent_author="main_a"),
            user_schema.ReplyOutcome("r2", 2, "m1", "cid1", False, "hybrid_dom", "stealth", 202, submitted_unconfirmed=True, matched_parent_author="@main_a"),
            user_schema.ReplyOutcome("r3", 3, "m2", "cid2", True, "hybrid_dom", None, 203, matched_parent_author="main_b"),
        ]
    )

    await user_schema._maybe_order_likes(
        result,
        video_url="https://www.tiktok.com/@demo/video/1234567890123456789",
        task_id=300,
        boost_enabled=True,
        boost_quantity=50,
        target_mode=user_schema.BOOST_LIKES_TARGET_EACH_FOUND,
        parallel_mode=True,
    )

    assert ordered == [("main_a", 50), ("main_b", 50)]
    assert result.boost_order_id == 1
    assert [item["username"] for item in result.boost_orders] == ["main_a", "main_b"]


@pytest.mark.unit
@pytest.mark.asyncio
async def test_parallel_likes_single_top_orders_most_answered_author(monkeypatch):
    from executor import user_schema
    from services import boost_project_client

    ordered: list[str] = []

    async def fake_order(_video_url, username, *, quantity):
        ordered.append(username)
        return True, 777, None

    monkeypatch.setattr(boost_project_client, "is_boost_enabled", lambda: True)
    monkeypatch.setattr(boost_project_client, "order_tiktok_comment_likes", fake_order)

    result = user_schema.UserSchemaResult(boost_enabled=True, boost_quantity=100)
    result.main_found_by_observer = True
    result.published_mains.extend(
        [
            user_schema.PublishedMain("m1", "cid1", True, True, "hybrid_dom", 101, "main_a"),
            user_schema.PublishedMain("m2", "cid2", True, True, "hybrid_dom", 102, "main_b"),
        ]
    )
    result.replies.extend(
        [
            user_schema.ReplyOutcome("r1", 1, "m1", "cid1", True, "hybrid_dom", None, 201, matched_parent_author="main_a"),
            user_schema.ReplyOutcome("r2", 2, "m2", "cid2", True, "hybrid_dom", None, 202, matched_parent_author="main_b"),
            user_schema.ReplyOutcome("r3", 3, "m2", "cid2", True, "hybrid_dom", None, 203, matched_parent_author="main_b"),
        ]
    )

    await user_schema._maybe_order_likes(
        result,
        video_url="https://www.tiktok.com/@demo/video/1234567890123456789",
        task_id=301,
        boost_enabled=True,
        boost_quantity=100,
        target_mode=user_schema.BOOST_LIKES_TARGET_SINGLE_TOP,
        parallel_mode=True,
    )

    assert ordered == ["main_b"]
    assert result.boost_order_id == 777
    assert result.boost_orders[0]["reply_count"] == 2
    assert result.boost_orders[0]["parent_text"] == "m2"
    assert result.boost_orders[0]["selection_reason"] == "most_replies"


@pytest.mark.unit
@pytest.mark.asyncio
async def test_parallel_likes_single_top_uses_best_parent_not_username_total(monkeypatch):
    from executor import user_schema
    from services import boost_project_client

    ordered: list[str] = []

    async def fake_order(_video_url, username, *, quantity):
        ordered.append(username)
        return True, 888, None

    monkeypatch.setattr(boost_project_client, "is_boost_enabled", lambda: True)
    monkeypatch.setattr(boost_project_client, "order_tiktok_comment_likes", fake_order)

    result = user_schema.UserSchemaResult(boost_enabled=True, boost_quantity=100)
    result.main_found_by_observer = True
    result.published_mains.extend(
        [
            user_schema.PublishedMain("a first", "cid-a1", True, True, "hybrid_dom", 101, "same_user"),
            user_schema.PublishedMain("a second", "cid-a2", True, True, "hybrid_dom", 101, "same_user"),
            user_schema.PublishedMain("b winner", "cid-b1", True, True, "hybrid_dom", 102, "winner_user"),
        ]
    )
    result.replies.extend(
        [
            user_schema.ReplyOutcome("r1", 1, "a first", "cid-a1", True, "hybrid_dom", None, 201, matched_parent_author="same_user"),
            user_schema.ReplyOutcome("r2", 2, "a second", "cid-a2", True, "hybrid_dom", None, 202, matched_parent_author="same_user"),
            user_schema.ReplyOutcome("r3", 3, "b winner", "cid-b1", True, "hybrid_dom", None, 203, matched_parent_author="winner_user"),
            user_schema.ReplyOutcome("r4", 4, "b winner", "cid-b1", True, "hybrid_dom", None, 204, matched_parent_author="winner_user"),
        ]
    )

    await user_schema._maybe_order_likes(
        result,
        video_url="https://www.tiktok.com/@demo/video/1234567890123456789",
        task_id=302,
        boost_enabled=True,
        boost_quantity=100,
        target_mode=user_schema.BOOST_LIKES_TARGET_SINGLE_TOP,
        parallel_mode=True,
    )

    assert ordered == ["winner_user"]
    assert result.boost_orders[0]["reply_count"] == 2
    assert result.boost_orders[0]["parent_text"] == "b winner"
    assert result.boost_orders[0]["parent_key"] == "cid-b1"


@pytest.mark.unit
def test_parallel_likes_each_found_keeps_one_best_parent_per_username():
    from executor import user_schema

    result = user_schema.UserSchemaResult(boost_enabled=True, boost_quantity=100)
    result.published_mains.extend(
        [
            user_schema.PublishedMain("low", "cid-low", True, True, "hybrid_dom", 101, "same_user"),
            user_schema.PublishedMain("high", "cid-high", True, True, "hybrid_dom", 101, "same_user"),
        ]
    )
    result.replies.extend(
        [
            user_schema.ReplyOutcome("r1", 1, "low", "cid-low", True, "hybrid_dom", None, 201, matched_parent_author="same_user"),
            user_schema.ReplyOutcome("r2", 2, "high", "cid-high", True, "hybrid_dom", None, 202, matched_parent_author="same_user"),
            user_schema.ReplyOutcome("r3", 3, "high", "cid-high", True, "hybrid_dom", None, 203, matched_parent_author="same_user"),
        ]
    )

    targets = user_schema._parallel_boost_like_targets_from_replies(
        result,
        target_mode=user_schema.BOOST_LIKES_TARGET_EACH_FOUND,
    )

    assert len(targets) == 1
    assert targets[0].username == "same_user"
    assert targets[0].reply_count == 2
    assert targets[0].parent_text == "high"
    assert targets[0].selection_reason == "per_username_most_replies"


@pytest.mark.unit
def test_parallel_likes_target_falls_back_to_expected_main_username_by_cid():
    from executor import user_schema

    result = user_schema.UserSchemaResult(boost_enabled=True, boost_quantity=100)
    result.published_mains.append(
        user_schema.PublishedMain("m1", "cid1", True, True, "hybrid_dom", 101, "main_a")
    )
    result.replies.append(
        user_schema.ReplyOutcome(
            "r1",
            1,
            "m1",
            "cid1",
            True,
            "hybrid_dom",
            None,
            201,
        )
    )

    targets = user_schema._parallel_boost_like_targets_from_replies(
        result,
        target_mode=user_schema.BOOST_LIKES_TARGET_EACH_FOUND,
    )

    assert [(target.username, target.reply_count) for target in targets] == [("main_a", 1)]
    assert targets[0].parent_text == "m1"


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
def test_reply_parent_batch_uses_the_total_remaining_search_deadline():
    from executor import user_schema

    source = inspect.getsource(user_schema._reply_with_variant_on_all_mains)

    assert "batch_timeout_seconds = max(1.0, batch_remaining_seconds)" in source
    assert "batch_timeout_seconds = min(\n                        search_pass_seconds" not in source


@pytest.mark.unit
@pytest.mark.parametrize(
    ("total_comment_count", "expected_scrolls"),
    [
        (0, 140),
        (99, 140),
        (100, 24),
        (500, 24),
        (501, 32),
        (1000, 32),
        (25_000, 32),
    ],
)
def test_reply_parent_search_budget_uses_video_comment_count_tiers(
    total_comment_count,
    expected_scrolls,
):
    from executor import user_schema

    total_seconds, pass_seconds, scrolls = user_schema._reply_parent_search_budget(
        [],
        total_comment_count,
    )

    assert scrolls == expected_scrolls
    assert total_seconds >= pass_seconds
    if total_comment_count < 100:
        assert total_seconds == user_schema._REPLY_MAIN_SEARCH_TO_END_TIMEOUT_SECONDS
    elif total_comment_count <= 500:
        assert total_seconds == user_schema._REPLY_MAIN_SEARCH_DEEP_TIMEOUT_SECONDS
    else:
        assert total_seconds == user_schema._REPLY_MAIN_SEARCH_LARGE_TIMEOUT_SECONDS


@pytest.mark.unit
@pytest.mark.parametrize(
    ("raw_value", "expected"),
    [
        ("98 comments", 98),
        ("1,234 comments", 1234),
        ("1.2K", 1200),
        ("1,2 \u0442\u044b\u0441. \u043a\u043e\u043c\u043c\u0435\u043d\u0442\u0430\u0440\u0438\u0435\u0432", 1200),
        ("2M", 2_000_000),
        ("comments", None),
    ],
)
def test_parse_tiktok_comment_count(raw_value, expected):
    from executor import user_schema

    assert user_schema._parse_tiktok_comment_count(raw_value) == expected


@pytest.mark.unit
@pytest.mark.asyncio
async def test_read_tiktok_total_comment_count_uses_first_scoped_valid_dom_counter():
    from executor import user_schema

    page = SimpleNamespace(
        evaluate=AsyncMock(return_value=["comments", "98 comments", "1.2K comments"]),
    )

    assert await user_schema._read_tiktok_total_comment_count(page) == 98


@pytest.mark.unit
def test_read_tiktok_total_comment_count_ranks_against_active_comment_surface():
    from executor import user_schema

    source = inspect.getsource(user_schema._read_tiktok_total_comment_count)

    assert 'data-smart-comment-surface-opened="1"' in source
    assert "document.querySelectorAll('video')" in source
    assert "left.distance - right.distance" in source
    assert "return max(parsed)" not in source


@pytest.mark.unit
def test_reply_comment_count_is_read_after_comment_list_open_attempt():
    from executor import user_schema

    source = inspect.getsource(user_schema._reply_with_variant_on_all_mains)
    open_call = source.index("comments_ready_for_search = await _open_comment_list_with_safe_retry(")
    count_read = source.index(
        "parent_search_total_comment_count = await _read_tiktok_total_comment_count(page)"
    )

    assert open_call < count_read


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


@pytest.mark.unit
def test_register_reply_captcha_failure_uses_captcha_path(monkeypatch):
    from executor import user_schema

    calls = {"captcha": [], "runtime": []}
    account = SimpleNamespace(id=501)

    monkeypatch.setattr(
        user_schema,
        "_register_captcha_session",
        lambda _db, acc, reason: calls["captcha"].append((acc.id, reason)),
    )
    monkeypatch.setattr(
        user_schema,
        "_register_runtime_failure",
        lambda _db, acc, reason: calls["runtime"].append((acc.id, reason)),
    )

    user_schema._register_reply_captcha_failure(SimpleNamespace(), account, "captcha_challenge:slider")

    assert calls == {"captcha": [(501, "captcha_challenge:slider")], "runtime": []}


@pytest.mark.unit
def test_http_first_gate_requires_network_first_and_impl_ready(monkeypatch):
    from executor import user_schema

    monkeypatch.setattr(user_schema._settings, "TIKTOK_NETWORK_FIRST", False, raising=False)
    monkeypatch.setattr(user_schema._settings, "TIKTOK_NETWORK_IMPL_READY", True, raising=False)

    assert not user_schema._user_schema_http_first_enabled(user_schema.MAIN_SCOPE)
    assert not user_schema._user_schema_http_first_enabled(user_schema.REPLY_SCOPE)

    monkeypatch.setattr(user_schema._settings, "TIKTOK_NETWORK_FIRST", True, raising=False)
    monkeypatch.setattr(user_schema._settings, "TIKTOK_NETWORK_IMPL_READY", False, raising=False)

    assert not user_schema._user_schema_http_first_enabled(user_schema.MAIN_SCOPE)
    assert not user_schema._user_schema_http_first_enabled(user_schema.REPLY_SCOPE)

    monkeypatch.setattr(user_schema._settings, "TIKTOK_NETWORK_FIRST", True, raising=False)
    monkeypatch.setattr(user_schema._settings, "TIKTOK_NETWORK_IMPL_READY", True, raising=False)

    assert user_schema._user_schema_http_first_enabled(user_schema.MAIN_SCOPE)
    assert user_schema._user_schema_http_first_enabled(user_schema.REPLY_SCOPE)
    assert not user_schema._user_schema_http_first_enabled("likes")


@pytest.mark.unit
@pytest.mark.asyncio
async def test_http_first_disabled_does_not_prepare_publish_session(monkeypatch):
    from executor import user_schema

    page = SimpleNamespace(url="https://www.tiktok.com/@u/video/1234567890123456789")
    account = SimpleNamespace(id=700)

    def fail_strategy_lookup(*_args, **_kwargs):
        raise AssertionError("HTTP strategy must not be evaluated when network-first is disabled")

    monkeypatch.setattr(user_schema._settings, "TIKTOK_NETWORK_FIRST", False, raising=False)
    monkeypatch.setattr(user_schema._settings, "TIKTOK_NETWORK_IMPL_READY", True, raising=False)
    monkeypatch.setattr(user_schema, "get_http_execution_strategy", fail_strategy_lookup)

    session = await user_schema._prepare_user_schema_http_session(
        page,
        account,
        "https://www.tiktok.com/@u/video/1234567890123456789",
        scope=user_schema.REPLY_SCOPE,
        correlation_id="http_disabled",
    )

    assert session is None


@pytest.mark.unit
@pytest.mark.asyncio
async def test_http_first_warmup_visible_captcha_blocks_session(monkeypatch):
    from executor import engagement_engine, user_schema

    page = SimpleNamespace(url="https://www.tiktok.com/@u/video/1234567890123456789")
    account = SimpleNamespace(id=701)

    async def fake_warmup(*_args, **_kwargs):
        return (
            {"sessionid": "ok"},
            True,
            {"current_url": "https://www.tiktok.com/@u/video/1234567890123456789"},
        )

    async def fail_from_page(*_args, **_kwargs):
        raise AssertionError("HTTP client must not be created while captcha is visible")

    monkeypatch.setattr(user_schema._settings, "TIKTOK_NETWORK_FIRST", True, raising=False)
    monkeypatch.setattr(user_schema._settings, "TIKTOK_NETWORK_IMPL_READY", True, raising=False)
    monkeypatch.setattr(user_schema, "get_http_execution_strategy", lambda *_args, **_kwargs: {"strategy": "http_first"})
    monkeypatch.setattr(engagement_engine, "_should_skip_http_for_account", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(engagement_engine, "_record_http_account_result", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(engagement_engine, "warmup_page_for_http", fake_warmup)
    monkeypatch.setattr(user_schema, "_visible_captcha_reason", AsyncMock(return_value="captcha_challenge:slider"))
    monkeypatch.setattr(user_schema.PlaywrightHttpClient, "from_page", fail_from_page)

    session = await user_schema._prepare_user_schema_http_session(
        page,
        account,
        "https://www.tiktok.com/@u/video/1234567890123456789",
        scope=user_schema.MAIN_SCOPE,
        correlation_id="http_captcha_gate",
    )

    assert session is None


@pytest.mark.unit
@pytest.mark.asyncio
async def test_http_first_warmup_visible_login_blocks_session(monkeypatch):
    from executor import engagement_engine, user_schema

    page = SimpleNamespace(url="https://www.tiktok.com/@u/video/1234567890123456789")
    account = SimpleNamespace(id=702)

    async def fake_warmup(*_args, **_kwargs):
        return (
            {"sessionid": "ok"},
            True,
            {"current_url": "https://www.tiktok.com/@u/video/1234567890123456789"},
        )

    async def fail_from_page(*_args, **_kwargs):
        raise AssertionError("HTTP client must not be created while login UI is visible")

    monkeypatch.setattr(user_schema._settings, "TIKTOK_NETWORK_FIRST", True, raising=False)
    monkeypatch.setattr(user_schema._settings, "TIKTOK_NETWORK_IMPL_READY", True, raising=False)
    monkeypatch.setattr(user_schema, "get_http_execution_strategy", lambda *_args, **_kwargs: {"strategy": "http_first"})
    monkeypatch.setattr(engagement_engine, "_should_skip_http_for_account", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(engagement_engine, "_record_http_account_result", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(engagement_engine, "warmup_page_for_http", fake_warmup)
    monkeypatch.setattr(user_schema, "_visible_captcha_reason", AsyncMock(return_value=None))
    monkeypatch.setattr(
        user_schema,
        "get_browser_login_state",
        AsyncMock(return_value={"state": "logged_out", "loginCount": 1}),
    )
    monkeypatch.setattr(user_schema.PlaywrightHttpClient, "from_page", fail_from_page)

    session = await user_schema._prepare_user_schema_http_session(
        page,
        account,
        "https://www.tiktok.com/@u/video/1234567890123456789",
        scope=user_schema.REPLY_SCOPE,
        correlation_id="http_login_gate",
    )

    assert session is None


# ---------------------------------------------------------------------------
# proxy detection
# ---------------------------------------------------------------------------

@pytest.mark.unit
@pytest.mark.parametrize(
    "reason,expected",
    [
        ("net::ERR_TUNNEL_CONNECTION_FAILED", True),
        ("net::ERR_PROXY_CONNECTION_FAILED", True),
        ("net::ERR_EMPTY_RESPONSE", True),
        ("passport:Connection timeout to host https://www.tiktok.com/", True),
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
        ("HTTP 403 when rotating proxy", True),
        ("407 Proxy Authentication Required", True),
        ("Forbidden rotation endpoint", True),
        ("rotation timed out", False),
        ("temporary provider timeout", False),
    ],
)
def test_rotation_failure_should_cool_proxy(reason, expected):
    from executor.user_schema import _rotation_failure_should_cool_proxy

    assert _rotation_failure_should_cool_proxy(reason) is expected


@pytest.mark.unit
@pytest.mark.parametrize(
    "reason,expected",
    [
        ("goto_failed:net::ERR_HTTP_RESPONSE_CODE_FAILURE", True),
        ("proxy_edge_access_denied:err_http_response_code_failure", True),
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
async def test_try_main_account_stops_after_enrichment_failure(monkeypatch):
    from executor import user_schema

    calls = {"publish": 0, "runtime": []}

    class _Page:
        url = "https://www.tiktok.com/@u/video/1234567890123456789"

        def is_closed(self):
            return False

        async def close(self):
            return None

    class _Context:
        async def close(self):
            return None

    async def fake_create_page(_account):
        return _Context(), _Page()

    async def fake_ready(*_args, **_kwargs):
        return True, None

    async def fake_enrich(*_args, **_kwargs):
        return SimpleNamespace(
            success=False,
            all_cookies_json=None,
            error="critical_cookies_not_generated",
            redirected=False,
            log_summary=lambda *_a, **_kw: None,
        )

    async def fail_publish(*_args, **_kwargs):
        calls["publish"] += 1
        raise AssertionError("main publish must not run after failed enrichment")

    monkeypatch.setattr(user_schema, "_preflight_cookie_http_gate", AsyncMock(return_value=None))
    monkeypatch.setattr(user_schema, "create_page_for_account", fake_create_page)
    monkeypatch.setattr(user_schema, "_ensure_account_login_ready", fake_ready)
    monkeypatch.setattr(user_schema, "needs_enrichment", lambda _account: True)
    monkeypatch.setattr(user_schema, "enrich_cookies_via_browser", fake_enrich)
    monkeypatch.setattr(user_schema, "_publish_all_variants_on_account", fail_publish)
    monkeypatch.setattr(
        user_schema,
        "_register_critical_enrichment_failure",
        lambda _db, acc, reason: calls["runtime"].append((acc.id, reason)),
    )

    account = SimpleNamespace(
        id=108,
        proxy=None,
        cookies='{"sessionid":"s","sid_guard":"g"}',
        cookie_file=None,
    )
    published, reason = await user_schema._try_main_account(
        SimpleNamespace(),
        account,
        "https://www.tiktok.com/@u/video/1234567890123456789",
        ["main text"],
        correlation_id="main_enrichment_failure_test",
    )

    assert published == []
    assert reason == "enrichment_failed:critical_cookies_not_generated"
    assert calls == {
        "publish": 0,
        "runtime": [(108, "enrichment_failed:critical_cookies_not_generated")],
    }


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
    assert deny == "proxy_edge_access_denied:err_http_response_code_failure"
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
async def test_ensure_account_login_ready_page_loading_retry_stays_loading_aware(monkeypatch):
    from executor import user_schema
    from executor.cookie_health import CookieHealth, CookieVerdict

    loading_reason = "page_loading:tiktok_shell_incomplete:on_video_no_login_btn:video"
    calls = {
        "fast": [],
        "reload": 0,
        "ambiguous": 0,
    }

    class _Page:
        url = "https://www.tiktok.com/@u/video/1"

        async def reload(self, **_kwargs):
            calls["reload"] += 1

    async def fake_open_video_and_check(*_args, **_kwargs):
        return CookieHealth(
            CookieVerdict.AMBIGUOUS,
            loading_reason,
            "https://www.tiktok.com/@u/video/1",
            True,
        )

    async def fake_fast_cookie_health_probe(_page, *, phase, settle_seconds=0.0, classify_loading=False):
        calls["fast"].append(
            {
                "phase": phase,
                "settle_seconds": settle_seconds,
                "classify_loading": classify_loading,
            }
        )
        return CookieHealth(
            CookieVerdict.AMBIGUOUS,
            loading_reason,
            "https://www.tiktok.com/@u/video/1",
            True,
        )

    monkeypatch.setattr(user_schema, "_open_video_and_check", fake_open_video_and_check)
    monkeypatch.setattr(user_schema, "_fast_cookie_health_probe", fake_fast_cookie_health_probe)
    monkeypatch.setattr(user_schema.asyncio, "sleep", AsyncMock(return_value=None))
    monkeypatch.setattr(
        user_schema,
        "register_ambiguous_session",
        lambda *_args, **_kwargs: calls.__setitem__("ambiguous", calls["ambiguous"] + 1),
    )

    ok, deny = await user_schema._ensure_account_login_ready(
        SimpleNamespace(),
        _Page(),
        SimpleNamespace(id=105, proxy=None),
        "https://vt.tiktok.com/abc/",
        timeout_ms=1000,
        correlation_id="task_1_parallel_main_v1_a1_acc105",
    )

    assert ok is False
    assert deny == f"login_ambiguous:{loading_reason}"
    assert calls["ambiguous"] == 1
    assert calls["reload"] == 0
    assert [call["phase"] for call in calls["fast"]] == [
        "login_probe_ambiguous_fast_retry",
        "login_probe_page_loading_extra_wait",
    ]
    assert all(call["classify_loading"] is True for call in calls["fast"])
    assert all(call["settle_seconds"] >= 2.0 for call in calls["fast"])


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
async def test_ensure_account_login_ready_requires_action_probe_for_strong_marker(monkeypatch):
    from executor import user_schema
    from executor.cookie_health import CookieHealth, CookieVerdict

    calls = {"alive": 0, "action_probe": 0}

    async def fake_open_video_and_check(*_args, **_kwargs):
        return CookieHealth(
            CookieVerdict.READY,
            "strong_marker:profile-link:/@valid_user",
            "https://www.tiktok.com/@u/video/1",
            True,
        )

    async def fail_action_probe(*_args, **_kwargs):
        calls["action_probe"] += 1
        return False, "login_ambiguous:comment_input_not_available_after_cookie_probe"

    monkeypatch.setattr(user_schema, "_open_video_and_check", fake_open_video_and_check)
    monkeypatch.setattr(user_schema, "_confirm_comment_action_ready", fail_action_probe)
    monkeypatch.setattr(
        user_schema,
        "register_alive_session",
        lambda *_args, **_kwargs: calls.__setitem__("alive", calls["alive"] + 1),
    )

    ok, deny = await user_schema._ensure_account_login_ready(
        SimpleNamespace(),
        SimpleNamespace(),
        SimpleNamespace(id=107, proxy=None),
        "https://vt.tiktok.com/abc/",
        timeout_ms=1000,
        correlation_id="test_strong_ready",
    )

    assert ok is False
    assert deny == "login_ambiguous:comment_input_not_available_after_cookie_probe"
    assert calls == {"alive": 0, "action_probe": 1}


@pytest.mark.unit
@pytest.mark.asyncio
async def test_parallel_main_accepts_strong_marker_without_action_ready_open(monkeypatch):
    from executor import user_schema
    from executor.cookie_health import CookieHealth, CookieVerdict

    calls = {"alive": 0, "surface_probe": 0, "action_probe": 0}

    async def fake_open_video_and_check(*_args, **_kwargs):
        return CookieHealth(
            CookieVerdict.READY,
            "strong_marker:profile-link:/@valid_user",
            "https://www.tiktok.com/@u/video/1",
            True,
        )

    async def fake_accept_surface(*_args, **_kwargs):
        calls["surface_probe"] += 1
        return False

    async def fail_action_probe(*_args, **_kwargs):
        calls["action_probe"] += 1
        raise AssertionError("parallel main strong marker should not run the short action-ready opener")

    monkeypatch.setattr(user_schema, "_open_video_and_check", fake_open_video_and_check)
    monkeypatch.setattr(user_schema, "_accept_action_ready_surface", fake_accept_surface)
    monkeypatch.setattr(user_schema, "_confirm_comment_action_ready", fail_action_probe)
    monkeypatch.setattr(
        user_schema,
        "register_alive_session",
        lambda *_args, **_kwargs: calls.__setitem__("alive", calls["alive"] + 1),
    )

    ok, deny = await user_schema._ensure_account_login_ready(
        SimpleNamespace(),
        SimpleNamespace(url="https://www.tiktok.com/@u/video/1"),
        SimpleNamespace(id=107, proxy=None),
        "https://vt.tiktok.com/abc/",
        timeout_ms=1000,
        correlation_id="task_1_parallel_main_v1_a1_acc107",
    )

    assert ok is True
    assert deny is None
    assert calls == {"alive": 1, "surface_probe": 1, "action_probe": 0}


@pytest.mark.unit
@pytest.mark.asyncio
async def test_parallel_reply_accepts_strong_marker_without_action_ready_open(monkeypatch):
    from executor import user_schema
    from executor.cookie_health import CookieHealth, CookieVerdict

    calls = {"alive": 0, "surface_probe": 0, "action_probe": 0}

    async def fake_open_video_and_check(*_args, **_kwargs):
        return CookieHealth(
            CookieVerdict.READY,
            "strong_marker:profile-link:/@valid_reply_user",
            "https://www.tiktok.com/@u/video/1",
            True,
        )

    async def fake_accept_surface(*_args, **_kwargs):
        calls["surface_probe"] += 1
        return False

    async def fail_action_probe(*_args, **_kwargs):
        calls["action_probe"] += 1
        raise AssertionError("parallel reply strong marker should not run the short action-ready opener")

    monkeypatch.setattr(user_schema, "_open_video_and_check", fake_open_video_and_check)
    monkeypatch.setattr(user_schema, "_accept_action_ready_surface", fake_accept_surface)
    monkeypatch.setattr(user_schema, "_confirm_comment_action_ready", fail_action_probe)
    monkeypatch.setattr(
        user_schema,
        "register_alive_session",
        lambda *_args, **_kwargs: calls.__setitem__("alive", calls["alive"] + 1),
    )

    ok, deny = await user_schema._ensure_account_login_ready(
        SimpleNamespace(),
        SimpleNamespace(url="https://www.tiktok.com/@u/video/1"),
        SimpleNamespace(id=207, proxy=None),
        "https://vt.tiktok.com/abc/",
        timeout_ms=1000,
        correlation_id="task_1_parallel_reply_v1_a1_acc207",
    )

    assert ok is True
    assert deny is None
    assert calls == {"alive": 1, "surface_probe": 1, "action_probe": 0}


@pytest.mark.unit
@pytest.mark.asyncio
async def test_ensure_account_login_ready_accepts_strong_marker_after_action_probe(monkeypatch):
    from executor import user_schema
    from executor.cookie_health import CookieHealth, CookieVerdict

    calls = {"alive": 0, "action_probe": 0}

    async def fake_open_video_and_check(*_args, **_kwargs):
        return CookieHealth(
            CookieVerdict.READY,
            "strong_marker:profile-link:/@valid_user",
            "https://www.tiktok.com/@u/video/1",
            True,
        )

    async def pass_action_probe(*_args, **_kwargs):
        calls["action_probe"] += 1
        return True, None

    monkeypatch.setattr(user_schema, "_open_video_and_check", fake_open_video_and_check)
    monkeypatch.setattr(user_schema, "_confirm_comment_action_ready", pass_action_probe)
    monkeypatch.setattr(
        user_schema,
        "register_alive_session",
        lambda *_args, **_kwargs: calls.__setitem__("alive", calls["alive"] + 1),
    )

    ok, deny = await user_schema._ensure_account_login_ready(
        SimpleNamespace(),
        SimpleNamespace(),
        SimpleNamespace(id=107, proxy=None),
        "https://vt.tiktok.com/abc/",
        timeout_ms=1000,
        correlation_id="test_strong_ready_with_action",
    )

    assert ok is True
    assert deny is None
    assert calls == {"alive": 0, "action_probe": 1}


@pytest.mark.unit
@pytest.mark.asyncio
async def test_ensure_account_login_ready_requires_action_probe_after_valid_http_preflight(monkeypatch):
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

    async def fail_action_probe(*_args, **_kwargs):
        calls["action_probe"] += 1
        return False, "login_ambiguous:comment_input_not_available_after_cookie_probe"

    account = SimpleNamespace(id=105, proxy=None)
    monkeypatch.setattr(user_schema, "_open_video_and_check", fake_open_video_and_check)
    monkeypatch.setattr(user_schema, "_confirm_comment_action_ready", fail_action_probe)
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
            correlation_id="test_valid_http_requires_action_ready",
        )
    finally:
        user_schema._HTTP_PREFLIGHT_VALID_CACHE.pop(user_schema._http_preflight_cache_key(account), None)

    assert ok is False
    assert deny == "login_ambiguous:comment_input_not_available_after_cookie_probe"
    assert calls == {"alive": 0, "action_probe": 1}


@pytest.mark.unit
@pytest.mark.asyncio
async def test_parallel_fast_auth_gate_rejects_video_only_marker(monkeypatch):
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

    async def fail_action_probe(*_args, **_kwargs):
        calls["action_probe"] += 1
        return False, "login_ambiguous:comment_input_not_available_after_cookie_probe"

    account = SimpleNamespace(id=106, proxy=None)
    monkeypatch.setattr(user_schema._settings, "PARALLEL_COMMENTING_FAST_AUTH_GATE", True, raising=False)
    monkeypatch.setattr(user_schema._settings, "PARALLEL_COMMENTING_DOM_FIRST", True, raising=False)
    monkeypatch.setattr(user_schema, "_open_video_and_check", fake_open_video_and_check)
    monkeypatch.setattr(user_schema, "_confirm_comment_action_ready", fail_action_probe)
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
            correlation_id="task_1_parallel_main_v1_a1_acc106",
        )
    finally:
        user_schema._HTTP_PREFLIGHT_VALID_CACHE.pop(user_schema._http_preflight_cache_key(account), None)

    assert ok is False
    assert deny == "login_ambiguous:comment_input_not_available_after_cookie_probe"
    assert calls == {"alive": 0, "action_probe": 1}


@pytest.mark.unit
@pytest.mark.asyncio
async def test_parallel_fast_auth_gate_accepts_comment_marker_after_valid_http_preflight(monkeypatch):
    from executor import user_schema
    from executor.cookie_health import CookieHealth, CookieVerdict

    calls = {"alive": 0, "action_probe": 0}

    async def fake_open_video_and_check(*_args, **_kwargs):
        return CookieHealth(
            CookieVerdict.AMBIGUOUS,
            'on_video_no_login_btn:[data-e2e="comment-icon"]',
            "https://www.tiktok.com/@u/video/1",
            True,
        )

    async def fail_action_probe(*_args, **_kwargs):
        calls["action_probe"] += 1
        raise AssertionError("comment-shell fast auth should skip composer probe")

    account = SimpleNamespace(id=109, proxy=None)
    monkeypatch.setattr(user_schema._settings, "PARALLEL_COMMENTING_FAST_AUTH_GATE", True, raising=False)
    monkeypatch.setattr(user_schema._settings, "PARALLEL_COMMENTING_DOM_FIRST", True, raising=False)
    monkeypatch.setattr(user_schema, "_open_video_and_check", fake_open_video_and_check)
    monkeypatch.setattr(user_schema, "_confirm_comment_action_ready", fail_action_probe)
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
            correlation_id="task_1_parallel_main_v1_a1_acc109",
        )
    finally:
        user_schema._HTTP_PREFLIGHT_VALID_CACHE.pop(user_schema._http_preflight_cache_key(account), None)

    assert ok is True
    assert deny is None
    assert calls == {"alive": 1, "action_probe": 0}


@pytest.mark.unit
@pytest.mark.asyncio
async def test_parallel_fast_auth_gate_does_not_accept_empty_dom(monkeypatch):
    from executor import user_schema
    from executor.cookie_health import CookieHealth, CookieVerdict

    calls = {"alive": 0, "action_probe": 0}

    async def fake_open_video_and_check(*_args, **_kwargs):
        return CookieHealth(
            CookieVerdict.AMBIGUOUS,
            "on_video_empty_dom_after_settle",
            "https://www.tiktok.com/@u/video/1",
            True,
        )

    async def fake_action_probe(*_args, **_kwargs):
        calls["action_probe"] += 1
        return False, "login_ambiguous:comment_input_not_available_after_cookie_probe"

    account = SimpleNamespace(id=108, proxy=None)
    monkeypatch.setattr(user_schema._settings, "PARALLEL_COMMENTING_FAST_AUTH_GATE", True, raising=False)
    monkeypatch.setattr(user_schema._settings, "PARALLEL_COMMENTING_DOM_FIRST", True, raising=False)
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
            correlation_id="task_1_parallel_main_v1_a1_acc108",
        )
    finally:
        user_schema._HTTP_PREFLIGHT_VALID_CACHE.pop(user_schema._http_preflight_cache_key(account), None)

    assert ok is False
    assert deny == "login_ambiguous:comment_input_not_available_after_cookie_probe"
    assert calls == {"alive": 0, "action_probe": 1}


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
async def test_action_ready_accepts_visible_composer_with_strong_login_marker(monkeypatch):
    from executor import user_schema
    from executor.cookie_health import CookieHealth, CookieVerdict

    calls = {"alive": 0, "ambiguous": 0, "open_comments": 0}

    async def fake_ensure_media(*_args, **_kwargs):
        return True

    async def fake_open_comments(*_args, **_kwargs):
        calls["open_comments"] += 1
        return True

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
        correlation_id="test_strong_marker_with_composer",
        phase="login_probe_retry",
    )

    assert ok is True
    assert deny is None
    assert calls == {"alive": 1, "ambiguous": 0, "open_comments": 1}


@pytest.mark.unit
@pytest.mark.asyncio
async def test_observer_open_failure_registers_visible_captcha(monkeypatch):
    from executor import user_schema

    calls = {"captcha": []}

    class _Page:
        url = "https://www.tiktok.com/@u/video/1"

        def is_closed(self):
            return False

        async def close(self):
            return None

    class _Context:
        async def close(self):
            return None

    account = SimpleNamespace(id=301, proxy=None)
    main = user_schema.PublishedMain(
        text="main text",
        comment_id=None,
        network_confirmed=False,
        local_confirmed=True,
        method="hybrid_dom",
        account_id=101,
        tiktok_username=None,
    )

    async def fake_create_page(_account):
        return _Context(), _Page()

    async def fake_ready(*_args, **_kwargs):
        return True, None

    async def fake_open(*_args, **_kwargs):
        return False

    async def fake_captcha(_page, *, phase):
        assert phase == "observer_open_comments_result"
        return "captcha_challenge:slider"

    monkeypatch.setattr(user_schema, "_preflight_cookie_http_gate", AsyncMock(return_value=None))
    monkeypatch.setattr(user_schema, "create_page_for_account", fake_create_page)
    monkeypatch.setattr(user_schema, "_ensure_account_login_ready", fake_ready)
    monkeypatch.setattr(user_schema, "needs_enrichment", lambda _account: False)
    monkeypatch.setattr(user_schema.asyncio, "sleep", AsyncMock(return_value=None))
    monkeypatch.setattr(user_schema.random, "uniform", lambda *_args: 0.0)
    monkeypatch.setattr(user_schema, "_deny_if_visible_logged_out", AsyncMock(return_value=None))
    monkeypatch.setattr(user_schema, "is_on_expected_media_url", lambda *_args, **_kwargs: True)
    monkeypatch.setattr(user_schema, "_open_comment_list_with_safe_retry", fake_open)
    monkeypatch.setattr(user_schema, "_visible_captcha_reason", fake_captcha)
    monkeypatch.setattr(
        user_schema,
        "_register_captcha_session",
        lambda _db, acc, reason: calls["captcha"].append((acc.id, reason)),
    )

    found, reason = await user_schema._observe_main_texts_with_account(
        SimpleNamespace(),
        account,
        "https://www.tiktok.com/@u/video/1",
        [main],
        correlation_id="observer_captcha_test",
    )

    assert found == []
    assert reason == "captcha_challenge:slider"
    assert calls["captcha"] == [(301, "captcha_challenge:slider")]


@pytest.mark.unit
@pytest.mark.asyncio
async def test_action_ready_rejects_strong_login_marker_when_composer_stays_missing(monkeypatch):
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

    assert ok is False
    assert deny == "login_ambiguous:comment_input_not_available_after_cookie_probe"
    assert calls == {"alive": 0, "ambiguous": 0}


@pytest.mark.unit
@pytest.mark.asyncio
async def test_parallel_action_ready_continues_when_ready_health_misses_composer(monkeypatch):
    from executor import user_schema
    from executor.cookie_health import CookieHealth, CookieVerdict

    calls = {"alive": 0, "ambiguous": 0, "open_comments": 0}

    async def fake_ensure_media(*_args, **_kwargs):
        return True

    async def fake_open_comments(*_args, **_kwargs):
        calls["open_comments"] += 1
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
        correlation_id="task_1_parallel_main_v1_a1_acc106",
        phase="login_probe_retry",
    )

    assert ok is True
    assert deny is None
    assert calls == {"alive": 1, "ambiguous": 0, "open_comments": 1}


@pytest.mark.unit
@pytest.mark.asyncio
async def test_parallel_reply_action_ready_continues_when_ready_health_misses_composer(monkeypatch):
    from executor import user_schema
    from executor.cookie_health import CookieHealth, CookieVerdict

    calls = {"alive": 0, "ambiguous": 0, "open_comments": 0}

    async def fake_ensure_media(*_args, **_kwargs):
        return True

    async def fake_open_comments(*_args, **_kwargs):
        calls["open_comments"] += 1
        return False

    async def fake_probe(_page):
        return CookieHealth(
            CookieVerdict.READY,
            "strong_marker:profile-link:/@valid_reply_user",
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
        SimpleNamespace(id=206, proxy=None),
        "https://www.tiktok.com/@u/video/1",
        timeout_ms=1000,
        correlation_id="task_1_parallel_reply_v1_a1_acc206",
        phase="login_probe_retry",
    )

    assert ok is True
    assert deny is None
    assert calls == {"alive": 1, "ambiguous": 0, "open_comments": 1}


@pytest.mark.unit
@pytest.mark.asyncio
async def test_action_ready_missing_composer_rejects_weak_browser_auth(monkeypatch):
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
        correlation_id="test_missing_composer_rejected",
        phase="login_probe",
    )

    assert ok is False
    assert deny == "login_ambiguous:comment_input_not_available_after_cookie_probe"
    assert calls == {"alive": 0, "ambiguous": 1}


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
async def test_post_enrichment_ambiguous_video_requires_action_ready_when_target_known(monkeypatch):
    from executor import user_schema
    from executor.cookie_health import CookieHealth, CookieVerdict

    calls = {"action_ready": 0}

    async def fake_probe(_page):
        return CookieHealth(
            CookieVerdict.AMBIGUOUS,
            "on_video_empty_dom_after_settle",
            "https://www.tiktok.com/@u/video/1",
            True,
        )

    async def fake_action_ready(*_args, **_kwargs):
        calls["action_ready"] += 1
        return False, "login_ambiguous:comment_input_not_available_after_cookie_probe"

    monkeypatch.setattr(user_schema, "probe_cookie_health", fake_probe)
    monkeypatch.setattr(user_schema.asyncio, "sleep", AsyncMock(return_value=None))
    monkeypatch.setattr(user_schema, "dismiss_cookie_banners", AsyncMock())
    monkeypatch.setattr(user_schema, "suppress_keyboard_shortcuts_overlay", AsyncMock())
    monkeypatch.setattr(user_schema, "_deny_if_visible_logged_out", AsyncMock(return_value=None))
    monkeypatch.setattr(user_schema, "_confirm_comment_action_ready", fake_action_ready)

    deny = await user_schema._post_enrichment_deny_reason(
        SimpleNamespace(),
        SimpleNamespace(url="https://www.tiktok.com/@u/video/1"),
        SimpleNamespace(id=104, proxy=None),
        correlation_id="post_enrich",
        phase="main_post_enrichment",
        target_video_url="https://www.tiktok.com/@u/video/1",
        timeout_ms=1000,
    )

    assert deny == "login_ambiguous:comment_input_not_available_after_cookie_probe"
    assert calls == {"action_ready": 1}


@pytest.mark.unit
@pytest.mark.asyncio
async def test_post_enrichment_reuses_recent_action_ready_proof(monkeypatch):
    from executor import user_schema
    from executor.cookie_health import CookieHealth, CookieVerdict

    calls = {"action_ready": 0, "alive": 0}
    video_url = "https://www.tiktok.com/@u/video/1234567890123456789"
    page = SimpleNamespace(url=video_url)

    async def fake_probe(_page):
        return CookieHealth(
            CookieVerdict.AMBIGUOUS,
            "on_video_empty_dom_after_settle",
            video_url,
            True,
        )

    async def fail_action_ready(*_args, **_kwargs):
        calls["action_ready"] += 1
        raise AssertionError("recent composer proof should skip the second action-ready probe")

    user_schema._remember_action_ready_proof(page, video_url, phase="login_probe")

    monkeypatch.setattr(user_schema, "probe_cookie_health", fake_probe)
    monkeypatch.setattr(user_schema.asyncio, "sleep", AsyncMock(return_value=None))
    monkeypatch.setattr(user_schema, "dismiss_cookie_banners", AsyncMock())
    monkeypatch.setattr(user_schema, "suppress_keyboard_shortcuts_overlay", AsyncMock())
    monkeypatch.setattr(user_schema, "_deny_if_visible_logged_out", AsyncMock(return_value=None))
    monkeypatch.setattr(
        user_schema,
        "get_comment_surface_state",
        AsyncMock(return_value={"comment_input_visible": True}),
    )
    monkeypatch.setattr(user_schema, "_confirm_comment_action_ready", fail_action_ready)
    monkeypatch.setattr(
        user_schema,
        "register_alive_session",
        lambda *_args, **_kwargs: calls.__setitem__("alive", calls["alive"] + 1),
    )

    deny = await user_schema._post_enrichment_deny_reason(
        SimpleNamespace(),
        page,
        SimpleNamespace(id=104, proxy=None),
        correlation_id="post_enrich",
        phase="main_post_enrichment",
        target_video_url=video_url,
        timeout_ms=1000,
    )

    assert deny is None
    assert calls == {"action_ready": 0, "alive": 1}


@pytest.mark.unit
@pytest.mark.asyncio
async def test_post_enrichment_stale_action_ready_proof_requires_current_dom(monkeypatch):
    from executor import user_schema
    from executor.cookie_health import CookieHealth, CookieVerdict

    calls = {"action_ready": 0, "alive": 0}
    video_url = "https://www.tiktok.com/@u/video/1234567890123456789"
    page = SimpleNamespace(url=video_url)

    async def fake_probe(_page):
        return CookieHealth(
            CookieVerdict.AMBIGUOUS,
            "on_video_empty_dom_after_settle",
            video_url,
            True,
        )

    async def fake_action_ready(*_args, **_kwargs):
        calls["action_ready"] += 1
        return False, "login_ambiguous:comment_input_not_available_after_cookie_probe"

    user_schema._remember_action_ready_proof(page, video_url, phase="login_probe")

    monkeypatch.setattr(user_schema, "probe_cookie_health", fake_probe)
    monkeypatch.setattr(user_schema.asyncio, "sleep", AsyncMock(return_value=None))
    monkeypatch.setattr(user_schema, "dismiss_cookie_banners", AsyncMock())
    monkeypatch.setattr(user_schema, "suppress_keyboard_shortcuts_overlay", AsyncMock())
    monkeypatch.setattr(user_schema, "_deny_if_visible_logged_out", AsyncMock(return_value=None))
    monkeypatch.setattr(
        user_schema,
        "get_comment_surface_state",
        AsyncMock(
            return_value={
                "input_visible": True,
                "comment_input_visible": False,
                "publish_input_candidate_visible": False,
            }
        ),
    )
    monkeypatch.setattr(user_schema, "_confirm_comment_action_ready", fake_action_ready)
    monkeypatch.setattr(
        user_schema,
        "register_alive_session",
        lambda *_args, **_kwargs: calls.__setitem__("alive", calls["alive"] + 1),
    )

    deny = await user_schema._post_enrichment_deny_reason(
        SimpleNamespace(),
        page,
        SimpleNamespace(id=104, proxy=None),
        correlation_id="post_enrich",
        phase="main_post_enrichment",
        target_video_url=video_url,
        timeout_ms=1000,
    )

    assert deny == "login_ambiguous:comment_input_not_available_after_cookie_probe"
    assert calls == {"action_ready": 1, "alive": 0}


@pytest.mark.unit
@pytest.mark.asyncio
async def test_enrichment_wrapper_returns_timeout_result(monkeypatch):
    from executor import user_schema

    async def slow_enrich(*_args, **_kwargs):
        await asyncio.sleep(10)

    monkeypatch.setattr(user_schema, "enrich_cookies_via_browser", slow_enrich)
    monkeypatch.setattr(user_schema, "_ENRICHMENT_HARD_TIMEOUT_PADDING_SECONDS", 0.01)

    result = await user_schema._enrich_cookies_with_hard_deadline(
        SimpleNamespace(),
        {},
        correlation_id="enrich_timeout_test",
        timeout_seconds=0.01,
    )

    assert result.success is False
    assert result.error == "enrichment_hard_timeout"
    assert result.elapsed_seconds < 1.0


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


@pytest.mark.unit
@pytest.mark.asyncio
async def test_open_video_and_check_returns_fast_ready_without_heavy_probe(monkeypatch):
    from executor import user_schema
    from executor.cookie_health import CookieVerdict

    calls = {"goto": 0, "probe": 0}

    class _VideoPage:
        url = "about:blank"

        async def goto(self, url, **_kwargs):
            calls["goto"] += 1
            self.url = url

    async def fake_quick(page, **_kwargs):
        return SimpleNamespace(
            state="ready",
            reason="strong_marker:profile-link:/@fast",
            current_url=page.url,
            on_video=True,
        )

    async def fail_probe(_page):
        calls["probe"] += 1
        raise AssertionError("fast ready should not call heavy probe")

    monkeypatch.setattr(user_schema, "_visible_captcha_reason", AsyncMock(return_value=None))
    monkeypatch.setattr(user_schema, "_visible_logged_out_reason", AsyncMock(return_value=None))
    monkeypatch.setattr(user_schema, "quick_video_login_check", fake_quick)
    monkeypatch.setattr(user_schema, "probe_cookie_health", fail_probe)

    health = await user_schema._open_video_and_check(
        _VideoPage(),
        "https://www.tiktok.com/@u/video/1",
        timeout_ms=1000,
        correlation_id="fast_ready_test",
    )

    assert health.verdict is CookieVerdict.READY
    assert health.reason == "strong_marker:profile-link:/@fast"
    assert calls == {"goto": 1, "probe": 0}


@pytest.mark.unit
@pytest.mark.asyncio
async def test_open_video_and_check_reports_fast_captcha_before_login_probe(monkeypatch):
    from executor import user_schema
    from executor.cookie_health import CookieVerdict

    class _VideoPage:
        url = "about:blank"

        async def goto(self, url, **_kwargs):
            self.url = url

    async def fail_quick(*_args, **_kwargs):
        raise AssertionError("captcha should short-circuit quick login probe")

    monkeypatch.setattr(
        user_schema,
        "_visible_captcha_reason",
        AsyncMock(return_value="captcha_challenge:slider"),
    )
    monkeypatch.setattr(user_schema, "quick_video_login_check", fail_quick)

    health = await user_schema._open_video_and_check(
        _VideoPage(),
        "https://www.tiktok.com/@u/video/1",
        timeout_ms=1000,
        correlation_id="fast_captcha_test",
    )

    assert health.verdict is CookieVerdict.CAPTCHA
    assert health.reason == "captcha_challenge:slider"


@pytest.mark.unit
@pytest.mark.asyncio
async def test_open_video_and_check_logged_out_state_overrides_weak_fast_video_probe(monkeypatch):
    from executor import user_schema
    from executor.cookie_health import CookieVerdict

    class _VideoPage:
        url = "about:blank"

        async def goto(self, url, **_kwargs):
            self.url = url

    async def fake_quick(page, **_kwargs):
        return SimpleNamespace(
            state="unknown",
            reason="on_video_no_login_btn:video",
            current_url=page.url,
            on_video=True,
        )

    async def fail_probe(_page):
        raise AssertionError("visible logged-out UI should short-circuit heavy probe")

    monkeypatch.setattr(user_schema, "_visible_captcha_reason", AsyncMock(return_value=None))
    monkeypatch.setattr(user_schema, "quick_video_login_check", fake_quick)
    monkeypatch.setattr(
        user_schema,
        "_visible_logged_out_reason",
        AsyncMock(return_value="visible_login_ui:initial_video_probe_fast:login=1"),
    )
    monkeypatch.setattr(user_schema, "probe_cookie_health", fail_probe)

    health = await user_schema._open_video_and_check(
        _VideoPage(),
        "https://www.tiktok.com/@u/video/1",
        timeout_ms=1000,
        correlation_id="fast_logged_out_override_test",
    )

    assert health.verdict is CookieVerdict.DEAD
    assert health.reason == "visible_login_ui:initial_video_probe_fast:login=1"


@pytest.mark.unit
@pytest.mark.asyncio
async def test_fast_cookie_health_probe_classifies_video_only_as_loading(monkeypatch):
    from executor import user_schema
    from executor.cookie_health import CookieVerdict

    class _VideoPage:
        url = "https://www.tiktok.com/@u/video/1"

    async def fake_quick(page, **_kwargs):
        return SimpleNamespace(
            state="unknown",
            reason="on_video_no_login_btn:video",
            current_url=page.url,
            on_video=True,
        )

    monkeypatch.setattr(user_schema, "_visible_captcha_reason", AsyncMock(return_value=None))
    monkeypatch.setattr(user_schema, "_visible_logged_out_reason", AsyncMock(return_value=None))
    monkeypatch.setattr(user_schema, "quick_video_login_check", fake_quick)

    health = await user_schema._fast_cookie_health_probe(
        _VideoPage(),
        phase="test",
        settle_seconds=0.01,
        classify_loading=True,
    )

    assert health is not None
    assert health.verdict is CookieVerdict.AMBIGUOUS
    assert health.reason == "page_loading:tiktok_shell_incomplete:on_video_no_login_btn:video"


@pytest.mark.unit
@pytest.mark.asyncio
async def test_fast_cookie_health_probe_returns_comment_marker_without_loading_delay(monkeypatch):
    from executor import user_schema
    from executor.cookie_health import CookieVerdict

    class _VideoPage:
        url = "https://www.tiktok.com/@u/video/1"

    async def fake_quick(page, **_kwargs):
        return SimpleNamespace(
            state="unknown",
            reason='on_video_no_login_btn:[data-e2e="comment-icon"]',
            current_url=page.url,
            on_video=True,
        )

    monkeypatch.setattr(user_schema, "_visible_captcha_reason", AsyncMock(return_value=None))
    monkeypatch.setattr(user_schema, "_visible_logged_out_reason", AsyncMock(return_value=None))
    monkeypatch.setattr(user_schema, "quick_video_login_check", fake_quick)

    health = await user_schema._fast_cookie_health_probe(
        _VideoPage(),
        phase="test",
        settle_seconds=5.0,
        classify_loading=True,
    )

    assert health is not None
    assert health.verdict is CookieVerdict.AMBIGUOUS
    assert health.reason == 'on_video_no_login_btn:[data-e2e="comment-icon"]'
