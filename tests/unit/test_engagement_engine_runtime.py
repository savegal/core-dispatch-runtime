import asyncio
import time
from contextlib import contextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

import executor.engagement_engine as engagement_engine
from db.models import Account


class _DummyPage:
    def __init__(self):
        self._closed = False

    def is_closed(self):
        return self._closed

    async def close(self):
        self._closed = True


class _WarmupContext:
    def __init__(self, cookies):
        self._cookies = cookies

    async def cookies(self):
        return list(self._cookies)

    async def close(self):
        return None


class _WarmupPage:
    def __init__(self, cookies):
        self._closed = False
        self.context = _WarmupContext(cookies)
        self.url = "https://www.tiktok.com/video/1234567890123456789"
        self.goto_calls = 0
        self.reload_calls = 0

    def is_closed(self):
        return self._closed

    async def close(self):
        self._closed = True

    async def goto(self, url, wait_until="domcontentloaded", timeout=30000):
        self.goto_calls += 1
        self.url = url

    async def reload(self, wait_until="domcontentloaded"):
        self.reload_calls += 1

    async def wait_for_load_state(self, state):
        return None


class _DummyContext:
    async def close(self):
        return None


class _DummyHttpClient:
    async def publish_main_comment(self, **kwargs):
        return True, "cid_main_123", None, None


async def _async_tuple(*values):
    return values


@pytest.mark.unit
def test_build_warmup_video_url_preserves_resolved_username_url():
    resolved_url = "https://www.tiktok.com/@_mjmagic_/video/7635267329407913230?_r=1&_t=ZS-96CHmDcrzAF"

    assert (
        engagement_engine._build_warmup_video_url(resolved_url, "7635267329407913230")
        == resolved_url
    )


@pytest.mark.unit
@pytest.mark.asyncio
async def test_attempt_main_comment_uses_warmup_resolved_aweme_id(db_session, monkeypatch):
    """Shortlinks can resolve only after browser navigation. The HTTP publish
    path must use the aweme_id/current URL discovered by warmup instead of
    failing with missing_aweme_id on the original vt.tiktok.com URL.
    """
    account = Account(id=20, label="main", status="active", cookies='{"sessionid":"x"}', proxy="http://proxy-main")
    resolved_url = "https://www.tiktok.com/@demo/video/7635267329407913230"
    captured = {}
    engagement_engine._HTTP_ACCOUNT_HEALTH.pop(account.id, None)
    for _ in range(engagement_engine.HTTP_SKIP_AFTER_WARMUP_FAILS):
        engagement_engine._record_http_account_result(account.id, "warmup_fail")

    async def _fake_create_page_for_account(_account):
        return _DummyContext(), _DummyPage()

    async def _fake_warmup_page_for_http(*args, **kwargs):
        return (
            {"sessionid": "x"},
            True,
            {
                "reason_code": "ok",
                "current_url": resolved_url,
                "resolved_video_id": "7635267329407913230",
            },
        )

    class _CapturingHttpClient:
        async def publish_main_comment(self, **kwargs):
            captured.update(kwargs)
            return True, "cid_main_123", None, None

    async def _fake_from_page(*args, **kwargs):
        return _CapturingHttpClient()

    monkeypatch.setattr(engagement_engine, "get_http_execution_strategy", lambda *args, **kwargs: {"strategy": "http_first"})
    monkeypatch.setattr(engagement_engine, "_should_skip_http_for_account", lambda *args, **kwargs: None)
    monkeypatch.setattr(engagement_engine, "create_page_for_account", _fake_create_page_for_account)
    monkeypatch.setattr(engagement_engine, "warmup_page_for_http", _fake_warmup_page_for_http)
    monkeypatch.setattr(engagement_engine.PlaywrightHttpClient, "from_page", _fake_from_page)
    monkeypatch.setattr(engagement_engine, "_register_runtime_success", lambda *args, **kwargs: None)
    monkeypatch.setattr(engagement_engine, "_register_runtime_failure", lambda *args, **kwargs: None)

    comment_id, error, stage = await engagement_engine.attempt_main_comment_on_account(
        db_session,
        account,
        "https://vt.tiktok.com/ZS9n129QV/",
        "hello world",
        176,
        "task_176_main_a1",
    )

    assert comment_id == "cid_main_123"
    assert error is None
    assert stage == "http"
    assert captured["aweme_id"] == "7635267329407913230"
    assert captured["video_url"] == resolved_url
    assert engagement_engine._HTTP_ACCOUNT_HEALTH[account.id]["success"] == 1
    assert engagement_engine._HTTP_ACCOUNT_HEALTH[account.id]["warmup_fail"] == 0
    assert engagement_engine._should_skip_http_for_account(account.id) is None


@pytest.mark.unit
@pytest.mark.asyncio
async def test_attempt_main_comment_falls_back_to_dom_when_signer_not_ready(db_session, monkeypatch):
    account = Account(id=22, label="main", status="active", cookies='{"sessionid":"x"}', proxy="http://proxy-main")
    resolved_url = "https://www.tiktok.com/@demo/video/7635267329407913230"
    page = _WarmupPage([])
    outcome = {}
    failures = []
    captured = {}
    engagement_engine._HTTP_ACCOUNT_HEALTH.pop(account.id, None)

    async def _fake_sleep(_seconds):
        return None

    async def _fake_create_page_for_account(_account):
        return _DummyContext(), page

    async def _fake_warmup_page_for_http(*args, **kwargs):
        return (
            {"sessionid": "x"},
            False,
            {
                "reason_code": "signer_not_ready",
                "missing_critical": [],
                "current_url": resolved_url,
                "signer_ready": False,
            },
        )

    async def _unexpected_from_page(*args, **kwargs):
        raise AssertionError("HTTP client should not be created when signer is not ready")

    async def _fake_bootstrap_session(_page, video_url, **kwargs):
        captured["bootstrap_video_url"] = video_url
        return SimpleNamespace(
            status="ok",
            can_attempt_publish=True,
            cookies={"sessionid": "x"},
            auth_present=["sessionid"],
        )

    async def _fake_publish_comment_hybrid(*args, **kwargs):
        return SimpleNamespace(
            status="success",
            comment_id="dom_comment_123",
            dom_strategy="comment_box",
            elapsed_seconds=1.0,
            network_captured=True,
        )

    monkeypatch.setattr(engagement_engine, "get_http_execution_strategy", lambda *args, **kwargs: {"strategy": "http_first"})
    monkeypatch.setattr(engagement_engine, "_should_skip_http_for_account", lambda *args, **kwargs: None)
    monkeypatch.setattr(engagement_engine, "create_page_for_account", _fake_create_page_for_account)
    monkeypatch.setattr(engagement_engine, "warmup_page_for_http", _fake_warmup_page_for_http)
    monkeypatch.setattr(engagement_engine.PlaywrightHttpClient, "from_page", _unexpected_from_page)
    monkeypatch.setattr(engagement_engine, "needs_enrichment", lambda *args, **kwargs: False)
    monkeypatch.setattr(engagement_engine, "bootstrap_session", _fake_bootstrap_session)
    monkeypatch.setattr(engagement_engine, "publish_comment_hybrid", _fake_publish_comment_hybrid)
    monkeypatch.setattr(engagement_engine, "_register_runtime_success", lambda *args, **kwargs: None)
    monkeypatch.setattr(engagement_engine, "_register_runtime_failure", lambda *args, **kwargs: failures.append(args))
    monkeypatch.setattr(engagement_engine.asyncio, "sleep", _fake_sleep)

    comment_id, error, stage = await engagement_engine.attempt_main_comment_on_account(
        db_session,
        account,
        resolved_url,
        "hello world",
        178,
        "task_178_main_a1",
        outcome,
    )

    assert comment_id == "dom_comment_123"
    assert error is None
    assert stage == "hybrid"
    assert outcome["posted"] is True
    assert outcome["network_confirmed"] is True
    assert outcome["method"] == "hybrid_dom"
    assert captured["bootstrap_video_url"] == resolved_url
    assert failures == []
    assert engagement_engine._HTTP_ACCOUNT_HEALTH[account.id]["success"] == 1
    assert engagement_engine._HTTP_ACCOUNT_HEALTH[account.id]["warmup_fail"] == 0


@pytest.mark.unit
@pytest.mark.asyncio
async def test_attempt_main_comment_falls_back_to_dom_when_http_never_sent_request(db_session, monkeypatch):
    account = Account(id=24, label="main", status="active", cookies='{"sessionid":"x"}', proxy="http://proxy-main")
    resolved_url = "https://www.tiktok.com/@demo/video/7635267329407913230"
    page = _WarmupPage([])
    failures = []
    engagement_engine._HTTP_ACCOUNT_HEALTH.pop(account.id, None)

    async def _fake_sleep(_seconds):
        return None

    async def _fake_create_page_for_account(_account):
        return _DummyContext(), page

    async def _fake_warmup_page_for_http(*args, **kwargs):
        return {"sessionid": "x", "msToken": "token"}, True, {
            "reason_code": "ready",
            "current_url": resolved_url,
        }

    class _PreSubmitHttpClient:
        last_request_meta = {"request_attempted": False, "attempt_count": 0}

        async def publish_main_comment(self, **kwargs):
            return False, None, "unsigned_profile_blocked", None

    async def _fake_from_page(*args, **kwargs):
        return _PreSubmitHttpClient()

    async def _fake_bootstrap_session(_page, video_url, **kwargs):
        return SimpleNamespace(
            status="ok",
            can_attempt_publish=True,
            cookies={"sessionid": "x"},
            auth_present=["sessionid"],
        )

    async def _fake_publish_comment_hybrid(*args, **kwargs):
        return SimpleNamespace(
            status="success",
            comment_id="dom_comment_after_presubmit",
            dom_strategy="comment_box",
            elapsed_seconds=1.0,
            network_captured=True,
        )

    monkeypatch.setattr(engagement_engine, "get_http_execution_strategy", lambda *args, **kwargs: {"strategy": "http_first"})
    monkeypatch.setattr(engagement_engine, "_should_skip_http_for_account", lambda *args, **kwargs: None)
    monkeypatch.setattr(engagement_engine, "create_page_for_account", _fake_create_page_for_account)
    monkeypatch.setattr(engagement_engine, "warmup_page_for_http", _fake_warmup_page_for_http)
    monkeypatch.setattr(engagement_engine.PlaywrightHttpClient, "from_page", _fake_from_page)
    monkeypatch.setattr(engagement_engine, "needs_enrichment", lambda *args, **kwargs: False)
    monkeypatch.setattr(engagement_engine, "bootstrap_session", _fake_bootstrap_session)
    monkeypatch.setattr(engagement_engine, "publish_comment_hybrid", _fake_publish_comment_hybrid)
    monkeypatch.setattr(engagement_engine, "_register_runtime_success", lambda *args, **kwargs: None)
    monkeypatch.setattr(engagement_engine, "_register_runtime_failure", lambda *args, **kwargs: failures.append(args))
    monkeypatch.setattr(engagement_engine.asyncio, "sleep", _fake_sleep)

    comment_id, error, stage = await engagement_engine.attempt_main_comment_on_account(
        db_session,
        account,
        resolved_url,
        "hello world",
        180,
        "task_180_main_a1",
    )

    assert comment_id == "dom_comment_after_presubmit"
    assert error is None
    assert stage == "hybrid"
    assert failures == []
    assert engagement_engine._HTTP_ACCOUNT_HEALTH[account.id]["success"] == 1
    assert engagement_engine._HTTP_ACCOUNT_HEALTH[account.id]["warmup_fail"] == 0


@pytest.mark.unit
@pytest.mark.asyncio
async def test_attempt_main_comment_stops_after_attempted_http_failure(db_session, monkeypatch):
    account = Account(id=25, label="main", status="active", cookies='{"sessionid":"x"}', proxy="http://proxy-main")
    resolved_url = "https://www.tiktok.com/@demo/video/7635267329407913230"
    failures = []
    engagement_engine._HTTP_ACCOUNT_HEALTH.pop(account.id, None)

    async def _fake_create_page_for_account(_account):
        return _DummyContext(), _DummyPage()

    async def _fake_warmup_page_for_http(*args, **kwargs):
        return {"sessionid": "x", "msToken": "token"}, True, {
            "reason_code": "ready",
            "current_url": resolved_url,
        }

    class _AttemptedHttpClient:
        last_request_meta = {"request_attempted": True, "attempt_count": 1}

        async def publish_main_comment(self, **kwargs):
            return False, None, "missing confirmation after publish", None

    async def _fake_from_page(*args, **kwargs):
        return _AttemptedHttpClient()

    async def _unexpected_publish_comment_hybrid(*args, **kwargs):
        raise AssertionError("DOM fallback should not run after an attempted HTTP publish")

    monkeypatch.setattr(engagement_engine, "get_http_execution_strategy", lambda *args, **kwargs: {"strategy": "http_first"})
    monkeypatch.setattr(engagement_engine, "_should_skip_http_for_account", lambda *args, **kwargs: None)
    monkeypatch.setattr(engagement_engine, "create_page_for_account", _fake_create_page_for_account)
    monkeypatch.setattr(engagement_engine, "warmup_page_for_http", _fake_warmup_page_for_http)
    monkeypatch.setattr(engagement_engine.PlaywrightHttpClient, "from_page", _fake_from_page)
    monkeypatch.setattr(engagement_engine, "publish_comment_hybrid", _unexpected_publish_comment_hybrid)
    monkeypatch.setattr(engagement_engine, "_register_runtime_success", lambda *args, **kwargs: None)
    monkeypatch.setattr(engagement_engine, "_register_runtime_failure", lambda *args, **kwargs: failures.append(args))

    comment_id, error, stage = await engagement_engine.attempt_main_comment_on_account(
        db_session,
        account,
        resolved_url,
        "hello world",
        181,
        "task_181_main_a1",
    )

    assert comment_id is None
    assert error == "missing confirmation after publish"
    assert stage == "http"
    assert len(failures) == 1
    assert engagement_engine._HTTP_ACCOUNT_HEALTH[account.id]["http_fail"] == 1


@pytest.mark.unit
@pytest.mark.asyncio
async def test_attempt_main_comment_uses_dom_when_http_health_gate_skips_account(db_session, monkeypatch):
    account = Account(id=23, label="main", status="active", cookies='{"sessionid":"x"}', proxy="http://proxy-main")
    page = _WarmupPage([])

    async def _fake_sleep(_seconds):
        return None

    async def _fake_create_page_for_account(_account):
        return _DummyContext(), page

    async def _unexpected_warmup_page_for_http(*args, **kwargs):
        raise AssertionError("Warmup should not run when HTTP health gate skips the account")

    async def _unexpected_from_page(*args, **kwargs):
        raise AssertionError("HTTP client should not be created when HTTP health gate skips the account")

    async def _fake_bootstrap_session(*args, **kwargs):
        return SimpleNamespace(
            status="ok",
            can_attempt_publish=True,
            cookies={"sessionid": "x"},
            auth_present=["sessionid"],
        )

    async def _fake_publish_comment_hybrid(*args, **kwargs):
        return SimpleNamespace(
            status="success",
            comment_id="dom_comment_456",
            dom_strategy="comment_box",
            elapsed_seconds=1.0,
            network_captured=True,
        )

    monkeypatch.setattr(engagement_engine, "get_http_execution_strategy", lambda *args, **kwargs: {"strategy": "http_first"})
    monkeypatch.setattr(engagement_engine, "_should_skip_http_for_account", lambda *args, **kwargs: "recent_warmup_failures")
    monkeypatch.setattr(engagement_engine, "create_page_for_account", _fake_create_page_for_account)
    monkeypatch.setattr(engagement_engine, "warmup_page_for_http", _unexpected_warmup_page_for_http)
    monkeypatch.setattr(engagement_engine.PlaywrightHttpClient, "from_page", _unexpected_from_page)
    monkeypatch.setattr(engagement_engine, "needs_enrichment", lambda *args, **kwargs: False)
    monkeypatch.setattr(engagement_engine, "bootstrap_session", _fake_bootstrap_session)
    monkeypatch.setattr(engagement_engine, "publish_comment_hybrid", _fake_publish_comment_hybrid)
    monkeypatch.setattr(engagement_engine, "_register_runtime_success", lambda *args, **kwargs: None)
    monkeypatch.setattr(engagement_engine, "_register_runtime_failure", lambda *args, **kwargs: None)
    monkeypatch.setattr(engagement_engine.asyncio, "sleep", _fake_sleep)

    comment_id, error, stage = await engagement_engine.attempt_main_comment_on_account(
        db_session,
        account,
        "https://www.tiktok.com/@demo/video/7635267329407913230",
        "hello world",
        179,
        "task_179_main_a1",
    )

    assert comment_id == "dom_comment_456"
    assert error is None
    assert stage == "hybrid"


@pytest.mark.unit
@pytest.mark.asyncio
async def test_attempt_main_comment_does_not_mark_reauth_on_stealth_reject(db_session, monkeypatch):
    account = Account(id=26, label="main", status="active", cookies='{"sid_guard":"x","ttwid":"y"}', proxy="http://proxy-main")
    failures = []
    reauth_marks = []

    async def _fake_sleep(_seconds):
        return None

    async def _fake_create_page_for_account(_account):
        return _DummyContext(), _DummyPage()

    async def _fake_bootstrap_session(*args, **kwargs):
        return SimpleNamespace(
            status="ready",
            can_attempt_publish=True,
            cookies={"sid_guard": "x", "ttwid": "y"},
            auth_present=["sid_guard"],
        )

    async def _fake_publish_comment_hybrid(*args, **kwargs):
        return SimpleNamespace(
            status="failed",
            error_reason="stealth_reject_empty_body",
            dom_strategy="comment_box",
            elapsed_seconds=1.0,
        )

    async def _fake_no_dom_recovery(*args, **kwargs):
        return None

    monkeypatch.setattr(engagement_engine, "get_http_execution_strategy", lambda *args, **kwargs: {"strategy": "dom_first"})
    monkeypatch.setattr(engagement_engine, "create_page_for_account", _fake_create_page_for_account)
    monkeypatch.setattr(engagement_engine, "needs_enrichment", lambda *args, **kwargs: False)
    monkeypatch.setattr(engagement_engine, "bootstrap_session", _fake_bootstrap_session)
    monkeypatch.setattr(engagement_engine, "publish_comment_hybrid", _fake_publish_comment_hybrid)
    monkeypatch.setattr(engagement_engine, "_recover_main_comment_after_ambiguous_publish", _fake_no_dom_recovery)
    monkeypatch.setattr(engagement_engine, "_register_runtime_success", lambda *args, **kwargs: None)
    monkeypatch.setattr(engagement_engine, "_register_runtime_failure", lambda *args, **kwargs: failures.append(args))
    monkeypatch.setattr(engagement_engine, "mark_needs_reauth", lambda *args, **kwargs: reauth_marks.append((args, kwargs)))
    monkeypatch.setattr(engagement_engine.asyncio, "sleep", _fake_sleep)

    comment_id, error, stage = await engagement_engine.attempt_main_comment_on_account(
        db_session,
        account,
        "https://www.tiktok.com/@demo/video/7635267329407913230",
        "hello world",
        182,
        "task_182_main_a1",
    )

    assert comment_id is None
    assert error == "stealth_reject"
    assert stage == "publish"
    assert reauth_marks == []
    assert failures
    assert failures[0][2] == "suspicious: stealth_reject_empty_body"


@pytest.mark.unit
@pytest.mark.asyncio
async def test_attempt_main_comment_recovers_dom_id_after_ambiguous_publish(db_session, monkeypatch):
    account = Account(id=260, label="main", status="active", cookies='{"sid_guard":"x","ttwid":"y"}', proxy="http://proxy-main")
    failures = []
    successes = []
    reauth_marks = []
    http_health = []
    outcome = {}

    async def _fake_sleep(_seconds):
        return None

    async def _fake_create_page_for_account(_account):
        return _DummyContext(), _DummyPage()

    async def _fake_bootstrap_session(*args, **kwargs):
        return SimpleNamespace(
            status="ready",
            can_attempt_publish=True,
            cookies={"sid_guard": "x", "ttwid": "y"},
            auth_present=["sid_guard"],
        )

    async def _fake_publish_comment_hybrid(*args, **kwargs):
        return SimpleNamespace(
            status="failed",
            error_reason="stealth_reject_empty_body",
            dom_strategy="comment_box",
            elapsed_seconds=1.0,
        )

    async def _fake_recover(*args, **kwargs):
        return "cid_recovered_dom"

    monkeypatch.setattr(engagement_engine, "get_http_execution_strategy", lambda *args, **kwargs: {"strategy": "dom_first"})
    monkeypatch.setattr(engagement_engine, "create_page_for_account", _fake_create_page_for_account)
    monkeypatch.setattr(engagement_engine, "needs_enrichment", lambda *args, **kwargs: False)
    monkeypatch.setattr(engagement_engine, "bootstrap_session", _fake_bootstrap_session)
    monkeypatch.setattr(engagement_engine, "publish_comment_hybrid", _fake_publish_comment_hybrid)
    monkeypatch.setattr(engagement_engine, "_recover_main_comment_after_ambiguous_publish", _fake_recover)
    monkeypatch.setattr(engagement_engine, "_record_http_account_result", lambda *args, **kwargs: http_health.append(args))
    monkeypatch.setattr(engagement_engine, "_register_runtime_success", lambda *args, **kwargs: successes.append(args))
    monkeypatch.setattr(engagement_engine, "_register_runtime_failure", lambda *args, **kwargs: failures.append(args))
    monkeypatch.setattr(engagement_engine, "mark_needs_reauth", lambda *args, **kwargs: reauth_marks.append((args, kwargs)))
    monkeypatch.setattr(engagement_engine.asyncio, "sleep", _fake_sleep)

    comment_id, error, stage = await engagement_engine.attempt_main_comment_on_account(
        db_session,
        account,
        "https://www.tiktok.com/@demo/video/7635267329407913230",
        "hello world",
        182,
        "task_182_main_a1",
        outcome_dict=outcome,
    )

    assert comment_id == "cid_recovered_dom"
    assert error is None
    assert stage == "hybrid"
    assert failures == []
    assert reauth_marks == []
    assert successes
    assert http_health == [(account.id, "success")]
    assert outcome["posted"] is True
    assert outcome["network_confirmed"] is False
    assert outcome["local_confirmed"] is True
    assert outcome["confirmation_source"] == "local_dom_after_ambiguous_publish"


@pytest.mark.unit
@pytest.mark.asyncio
async def test_attempt_main_comment_accepts_local_dom_match_without_id(db_session, monkeypatch):
    account = Account(id=262, label="main", status="active", cookies='{"sid_guard":"x","ttwid":"y"}', proxy="http://proxy-main")
    successes = []
    failures = []
    outcome = {}

    async def _fake_sleep(_seconds):
        return None

    async def _fake_create_page_for_account(_account):
        return _DummyContext(), _DummyPage()

    async def _fake_bootstrap_session(*args, **kwargs):
        return SimpleNamespace(
            status="ready",
            can_attempt_publish=True,
            cookies={"sid_guard": "x", "ttwid": "y"},
            auth_present=["sid_guard"],
        )

    async def _fake_publish_comment_hybrid(*args, **kwargs):
        return SimpleNamespace(
            status="failed",
            error_reason="stealth_reject_empty_body",
            dom_strategy="comment_box",
            elapsed_seconds=1.0,
        )

    async def _fake_recover(*args, **kwargs):
        return {
            "found": True,
            "comment_id": None,
            "text_normalized": "hello world",
            "selector": 'span[data-e2e="comment-level-1"]',
        }

    monkeypatch.setattr(engagement_engine, "get_http_execution_strategy", lambda *args, **kwargs: {"strategy": "dom_first"})
    monkeypatch.setattr(engagement_engine, "create_page_for_account", _fake_create_page_for_account)
    monkeypatch.setattr(engagement_engine, "needs_enrichment", lambda *args, **kwargs: False)
    monkeypatch.setattr(engagement_engine, "bootstrap_session", _fake_bootstrap_session)
    monkeypatch.setattr(engagement_engine, "publish_comment_hybrid", _fake_publish_comment_hybrid)
    monkeypatch.setattr(engagement_engine, "_recover_main_comment_after_ambiguous_publish", _fake_recover)
    monkeypatch.setattr(engagement_engine, "_register_runtime_success", lambda *args, **kwargs: successes.append(args))
    monkeypatch.setattr(engagement_engine, "_register_runtime_failure", lambda *args, **kwargs: failures.append(args))
    monkeypatch.setattr(engagement_engine.asyncio, "sleep", _fake_sleep)

    comment_id, error, stage = await engagement_engine.attempt_main_comment_on_account(
        db_session,
        account,
        "https://www.tiktok.com/@demo/video/7635267329407913230",
        "hello world",
        184,
        "task_184_main_a1",
        outcome_dict=outcome,
    )

    assert comment_id is None
    assert error is None
    assert stage == "hybrid_local_no_id"
    assert failures == []
    assert successes
    assert outcome["posted"] is True
    assert outcome["local_confirmed"] is True
    assert outcome["local_confirmation_without_id"] is True
    assert outcome["confirmation_source"] == "local_dom_text_match_no_id"


@pytest.mark.unit
@pytest.mark.asyncio
async def test_attempt_reply_recovers_dom_id_after_ambiguous_publish(db_session, monkeypatch):
    account = Account(id=261, label="reply", status="active", cookies='{"sid_guard":"x","ttwid":"y"}', proxy="http://proxy-reply")
    failures = []
    successes = []
    outcome = {}

    async def _fake_sleep(_seconds):
        return None

    async def _fake_create_page_for_account(_account):
        return _DummyContext(), _DummyPage()

    async def _fake_bootstrap_session(*args, **kwargs):
        return SimpleNamespace(
            status="ready",
            can_attempt_publish=True,
            cookies={"sid_guard": "x", "ttwid": "y"},
            auth_present=["sid_guard"],
        )

    async def _fake_publish_reply_hybrid(*args, **kwargs):
        return SimpleNamespace(
            status="failed",
            error_reason="stealth_reject_empty_body",
            dom_strategy="reply_box",
            elapsed_seconds=1.0,
        )

    async def _fake_recover(*args, **kwargs):
        return "reply_recovered_dom"

    monkeypatch.setattr(engagement_engine, "get_http_execution_strategy", lambda *args, **kwargs: {"strategy": "dom_first"})
    monkeypatch.setattr(engagement_engine, "create_page_for_account", _fake_create_page_for_account)
    monkeypatch.setattr(engagement_engine, "needs_enrichment", lambda *args, **kwargs: False)
    monkeypatch.setattr(engagement_engine, "bootstrap_session", _fake_bootstrap_session)
    monkeypatch.setattr(engagement_engine, "publish_reply_hybrid", _fake_publish_reply_hybrid)
    monkeypatch.setattr(engagement_engine, "_recover_reply_comment_after_ambiguous_publish", _fake_recover)
    monkeypatch.setattr(engagement_engine, "_register_runtime_success", lambda *args, **kwargs: successes.append(args))
    monkeypatch.setattr(engagement_engine, "_register_runtime_failure", lambda *args, **kwargs: failures.append(args))
    monkeypatch.setattr(engagement_engine.asyncio, "sleep", _fake_sleep)

    reply_id, error, stage = await engagement_engine.attempt_reply_on_account(
        db_session,
        account,
        "https://www.tiktok.com/@demo/video/7635267329407913230",
        "reply text",
        "main text",
        183,
        "task_183_reply_a1",
        parent_comment_id="parent_123",
        outcome_dict=outcome,
    )

    assert reply_id == "reply_recovered_dom"
    assert error is None
    assert stage == "hybrid"
    assert failures == []
    assert successes
    assert outcome["posted"] is True
    assert outcome["local_confirmed"] is True
    assert outcome["confirmation_source"] == "local_dom_after_ambiguous_reply_publish"
    assert outcome["parent_confirmed"] is True
    assert outcome["parent_confirmation_level"] == "ui_selected_parent"


@pytest.mark.unit
@pytest.mark.asyncio
async def test_attempt_reply_accepts_local_dom_match_without_id(db_session, monkeypatch):
    account = Account(id=262, label="reply", status="active", cookies='{"sid_guard":"x","ttwid":"y"}', proxy="http://proxy-reply")
    failures = []
    successes = []
    outcome = {}

    async def _fake_sleep(_seconds):
        return None

    async def _fake_create_page_for_account(_account):
        return _DummyContext(), _DummyPage()

    async def _fake_bootstrap_session(*args, **kwargs):
        return SimpleNamespace(
            status="ready",
            can_attempt_publish=True,
            cookies={"sid_guard": "x", "ttwid": "y"},
            auth_present=["sid_guard"],
        )

    async def _fake_publish_reply_hybrid(*args, **kwargs):
        return SimpleNamespace(
            status="failed",
            error_reason="stealth_reject_empty_body",
            dom_strategy="reply_box",
            elapsed_seconds=1.0,
        )

    async def _fake_recover(*args, **kwargs):
        return {
            "found": True,
            "comment_id": None,
            "position": 0,
            "selector": 'span[data-e2e="comment-level-2"]',
        }

    monkeypatch.setattr(engagement_engine, "get_http_execution_strategy", lambda *args, **kwargs: {"strategy": "dom_first"})
    monkeypatch.setattr(engagement_engine, "create_page_for_account", _fake_create_page_for_account)
    monkeypatch.setattr(engagement_engine, "needs_enrichment", lambda *args, **kwargs: False)
    monkeypatch.setattr(engagement_engine, "bootstrap_session", _fake_bootstrap_session)
    monkeypatch.setattr(engagement_engine, "publish_reply_hybrid", _fake_publish_reply_hybrid)
    monkeypatch.setattr(engagement_engine, "_recover_reply_comment_after_ambiguous_publish", _fake_recover)
    monkeypatch.setattr(engagement_engine, "_register_runtime_success", lambda *args, **kwargs: successes.append(args))
    monkeypatch.setattr(engagement_engine, "_register_runtime_failure", lambda *args, **kwargs: failures.append(args))
    monkeypatch.setattr(engagement_engine.asyncio, "sleep", _fake_sleep)

    reply_id, error, stage = await engagement_engine.attempt_reply_on_account(
        db_session,
        account,
        "https://www.tiktok.com/@demo/video/7635267329407913230",
        "reply text",
        "main text",
        185,
        "task_185_reply_a1",
        parent_comment_id="parent_123",
        outcome_dict=outcome,
    )

    assert reply_id is None
    assert error is None
    assert stage == "reply_local_no_id"
    assert failures == []
    assert successes
    assert outcome["posted"] is True
    assert outcome["local_confirmed"] is True
    assert outcome["local_confirmation_without_id"] is True
    assert outcome["confirmation_source"] == "local_dom_reply_text_match_no_id"
    assert outcome["parent_confirmed"] is True
    assert outcome["parent_confirmation_level"] == "ui_selected_parent"


@pytest.mark.unit
@pytest.mark.asyncio
async def test_attempt_main_comment_does_not_mark_reauth_when_input_hidden_by_captcha(db_session, monkeypatch):
    account = Account(id=27, label="main", status="active", cookies='{"sid_guard":"x","ttwid":"y"}', proxy="http://proxy-main")
    failures = []
    reauth_marks = []

    async def _fake_sleep(_seconds):
        return None

    async def _fake_create_page_for_account(_account):
        return _DummyContext(), _DummyPage()

    async def _fake_bootstrap_session(*args, **kwargs):
        return SimpleNamespace(
            status="ready",
            can_attempt_publish=True,
            cookies={"sid_guard": "x", "ttwid": "y"},
            auth_present=["sid_guard"],
        )

    async def _fake_publish_comment_hybrid(*args, **kwargs):
        return SimpleNamespace(
            status="dom_error",
            error_reason="input_not_found: timeout",
            dom_strategy=None,
            elapsed_seconds=1.0,
        )

    async def _fake_detect_captcha(*args, **kwargs):
        return True, "slider"

    monkeypatch.setattr(engagement_engine, "get_http_execution_strategy", lambda *args, **kwargs: {"strategy": "dom_first"})
    monkeypatch.setattr(engagement_engine, "create_page_for_account", _fake_create_page_for_account)
    monkeypatch.setattr(engagement_engine, "needs_enrichment", lambda *args, **kwargs: False)
    monkeypatch.setattr(engagement_engine, "bootstrap_session", _fake_bootstrap_session)
    monkeypatch.setattr(engagement_engine, "publish_comment_hybrid", _fake_publish_comment_hybrid)
    monkeypatch.setattr(engagement_engine, "detect_captcha", _fake_detect_captcha)
    monkeypatch.setattr(engagement_engine, "_register_runtime_success", lambda *args, **kwargs: None)
    monkeypatch.setattr(engagement_engine, "_register_runtime_failure", lambda *args, **kwargs: failures.append(args))
    monkeypatch.setattr(engagement_engine, "mark_needs_reauth", lambda *args, **kwargs: reauth_marks.append((args, kwargs)))
    monkeypatch.setattr(engagement_engine.asyncio, "sleep", _fake_sleep)

    comment_id, error, stage = await engagement_engine.attempt_main_comment_on_account(
        db_session,
        account,
        "https://www.tiktok.com/@demo/video/7635267329407913230",
        "hello world",
        183,
        "task_183_main_a1",
    )

    assert comment_id is None
    assert error == "captcha_challenge:slider"
    assert stage == "publish"
    assert reauth_marks == []
    assert failures
    assert failures[0][2] == "captcha_challenge:slider"


@pytest.mark.unit
@pytest.mark.asyncio
async def test_attempt_main_comment_does_not_mark_reauth_after_manual_captcha_clear_input_timeout(db_session, monkeypatch):
    account = Account(id=28, label="main", status="active", cookies='{"sid_guard":"x","ttwid":"y"}', proxy="http://proxy-main")
    failures = []
    reauth_marks = []

    async def _fake_sleep(_seconds):
        return None

    async def _fake_create_page_for_account(_account):
        return _DummyContext(), _DummyPage()

    async def _fake_bootstrap_session(*args, **kwargs):
        return SimpleNamespace(
            status="ready",
            can_attempt_publish=True,
            cookies={"sid_guard": "x", "ttwid": "y"},
            auth_present=["sid_guard"],
        )

    async def _fake_publish_comment_hybrid(*args, **kwargs):
        return SimpleNamespace(
            status="dom_error",
            error_reason="input_not_found_after_captcha_clear: timeout",
            dom_strategy=None,
            elapsed_seconds=1.0,
        )

    async def _fake_detect_captcha(*args, **kwargs):
        return False, None

    monkeypatch.setattr(engagement_engine, "get_http_execution_strategy", lambda *args, **kwargs: {"strategy": "dom_first"})
    monkeypatch.setattr(engagement_engine, "create_page_for_account", _fake_create_page_for_account)
    monkeypatch.setattr(engagement_engine, "needs_enrichment", lambda *args, **kwargs: False)
    monkeypatch.setattr(engagement_engine, "bootstrap_session", _fake_bootstrap_session)
    monkeypatch.setattr(engagement_engine, "publish_comment_hybrid", _fake_publish_comment_hybrid)
    monkeypatch.setattr(engagement_engine, "detect_captcha", _fake_detect_captcha)
    monkeypatch.setattr(engagement_engine, "_register_runtime_success", lambda *args, **kwargs: None)
    monkeypatch.setattr(engagement_engine, "_register_runtime_failure", lambda *args, **kwargs: failures.append(args))
    monkeypatch.setattr(engagement_engine, "mark_needs_reauth", lambda *args, **kwargs: reauth_marks.append((args, kwargs)))
    monkeypatch.setattr(engagement_engine.asyncio, "sleep", _fake_sleep)

    comment_id, error, stage = await engagement_engine.attempt_main_comment_on_account(
        db_session,
        account,
        "https://www.tiktok.com/@demo/video/7635267329407913230",
        "hello world",
        184,
        "task_184_main_a1",
    )

    assert comment_id is None
    assert error == "input_not_found_after_captcha_clear: timeout"
    assert stage == "publish"
    assert reauth_marks == []
    assert failures
    assert failures[0][2] == "captcha_after_clear_input_not_found"


@pytest.mark.unit
def test_http_account_health_success_resets_prior_warmup_failures():
    engagement_engine._HTTP_ACCOUNT_HEALTH.pop(909, None)
    engagement_engine._HTTP_RUNTIME_SIGNER_HEALTH["warmup_fail"] = 0.0
    engagement_engine._HTTP_RUNTIME_SIGNER_HEALTH["cooldown_until"] = 0.0

    for _ in range(engagement_engine.HTTP_SKIP_AFTER_WARMUP_FAILS):
        engagement_engine._record_http_account_result(909, "warmup_fail")
    engagement_engine._record_ui_no_progress(909)

    assert engagement_engine._should_skip_http_for_account(909) == "recent_warmup_failures"

    engagement_engine._record_http_account_result(909, "success")

    assert engagement_engine._should_skip_http_for_account(909) is None
    assert engagement_engine._HTTP_ACCOUNT_HEALTH[909]["warmup_fail"] == 0
    assert engagement_engine._HTTP_ACCOUNT_HEALTH[909]["ui_no_progress"] == 0


@pytest.mark.unit
def test_http_runtime_signer_cooldown_skips_other_accounts_temporarily():
    engagement_engine._HTTP_ACCOUNT_HEALTH.pop(910, None)
    engagement_engine._HTTP_RUNTIME_SIGNER_HEALTH["warmup_fail"] = 0.0
    engagement_engine._HTTP_RUNTIME_SIGNER_HEALTH["cooldown_until"] = 0.0

    for _ in range(engagement_engine.HTTP_SIGNER_COOLDOWN_AFTER_WARMUP_FAILS):
        engagement_engine._record_http_account_result(None, "warmup_fail")

    assert engagement_engine._should_skip_http_for_account(910) == "runtime_signer_cooldown"

    engagement_engine._record_http_account_result(910, "success")

    assert engagement_engine._should_skip_http_for_account(910) is None


@pytest.mark.unit
@pytest.mark.asyncio
async def test_attempt_main_comment_does_not_mark_reauth_when_input_missing_but_browser_logged_in(db_session, monkeypatch):
    account = Account(id=29, label="main", status="active", cookies='{"sid_guard":"x","ttwid":"y"}', proxy="http://proxy-main")
    failures = []
    reauth_marks = []

    async def _fake_sleep(_seconds):
        return None

    async def _fake_create_page_for_account(_account):
        return _DummyContext(), _DummyPage()

    async def _fake_bootstrap_session(*args, **kwargs):
        return SimpleNamespace(
            status="ready",
            can_attempt_publish=True,
            cookies={"sid_guard": "x", "ttwid": "y"},
            auth_present=["sid_guard"],
        )

    async def _fake_publish_comment_hybrid(*args, **kwargs):
        return SimpleNamespace(
            status="dom_error",
            error_reason="input_not_found: timeout",
            dom_strategy=None,
            elapsed_seconds=1.0,
        )

    async def _fake_detect_captcha(*args, **kwargs):
        return False, None

    async def _fake_login_state(*args, **kwargs):
        return {"state": "logged_in", "loggedInCount": 2, "loginCount": 0}

    monkeypatch.setattr(engagement_engine, "get_http_execution_strategy", lambda *args, **kwargs: {"strategy": "dom_first"})
    monkeypatch.setattr(engagement_engine, "create_page_for_account", _fake_create_page_for_account)
    monkeypatch.setattr(engagement_engine, "needs_enrichment", lambda *args, **kwargs: False)
    monkeypatch.setattr(engagement_engine, "bootstrap_session", _fake_bootstrap_session)
    monkeypatch.setattr(engagement_engine, "publish_comment_hybrid", _fake_publish_comment_hybrid)
    monkeypatch.setattr(engagement_engine, "detect_captcha", _fake_detect_captcha)
    monkeypatch.setattr(engagement_engine, "get_browser_login_state", _fake_login_state)
    monkeypatch.setattr(engagement_engine, "_register_runtime_success", lambda *args, **kwargs: None)
    monkeypatch.setattr(engagement_engine, "_register_runtime_failure", lambda *args, **kwargs: failures.append(args))
    monkeypatch.setattr(engagement_engine, "mark_needs_reauth", lambda *args, **kwargs: reauth_marks.append((args, kwargs)))
    monkeypatch.setattr(engagement_engine.asyncio, "sleep", _fake_sleep)

    comment_id, error, stage = await engagement_engine.attempt_main_comment_on_account(
        db_session,
        account,
        "https://www.tiktok.com/@demo/video/7635267329407913230",
        "hello world",
        185,
        "task_185_main_a1",
    )

    assert comment_id is None
    assert error == "input_not_found: timeout"
    assert stage == "publish"
    assert reauth_marks == []
    assert failures == []


@pytest.mark.unit
@pytest.mark.asyncio
async def test_attempt_main_comment_cools_down_unknown_input_missing_immediately(db_session, monkeypatch):
    account = Account(id=229, label="main", status="active", cookies='{"sid_guard":"x","ttwid":"y"}', proxy="http://proxy-main")
    failures = []
    reauth_marks = []
    engagement_engine._HTTP_ACCOUNT_HEALTH.pop(account.id, None)

    async def _fake_sleep(_seconds):
        return None

    async def _fake_create_page_for_account(_account):
        return _DummyContext(), _DummyPage()

    async def _fake_bootstrap_session(*args, **kwargs):
        return SimpleNamespace(
            status="ready",
            can_attempt_publish=True,
            cookies={"sid_guard": "x", "ttwid": "y"},
            auth_present=["sid_guard"],
        )

    async def _fake_publish_comment_hybrid(*args, **kwargs):
        return SimpleNamespace(
            status="dom_error",
            error_reason="input_not_found: timeout",
            dom_strategy=None,
            elapsed_seconds=1.0,
        )

    async def _fake_detect_captcha(*args, **kwargs):
        return False, None

    async def _fake_login_state(*args, **kwargs):
        return {"state": "unknown", "loggedInCount": 0, "loginCount": 0}

    monkeypatch.setattr(engagement_engine, "get_http_execution_strategy", lambda *args, **kwargs: {"strategy": "dom_first"})
    monkeypatch.setattr(engagement_engine, "create_page_for_account", _fake_create_page_for_account)
    monkeypatch.setattr(engagement_engine, "needs_enrichment", lambda *args, **kwargs: False)
    monkeypatch.setattr(engagement_engine, "bootstrap_session", _fake_bootstrap_session)
    monkeypatch.setattr(engagement_engine, "publish_comment_hybrid", _fake_publish_comment_hybrid)
    monkeypatch.setattr(engagement_engine, "detect_captcha", _fake_detect_captcha)
    monkeypatch.setattr(engagement_engine, "get_browser_login_state", _fake_login_state)
    monkeypatch.setattr(engagement_engine, "_register_runtime_success", lambda *args, **kwargs: None)
    monkeypatch.setattr(engagement_engine, "_register_runtime_failure", lambda *args, **kwargs: failures.append(args))
    monkeypatch.setattr(engagement_engine, "mark_needs_reauth", lambda *args, **kwargs: reauth_marks.append((args, kwargs)))
    monkeypatch.setattr(engagement_engine.asyncio, "sleep", _fake_sleep)

    comment_id, error, stage = await engagement_engine.attempt_main_comment_on_account(
        db_session,
        account,
        "https://www.tiktok.com/@demo/video/7635267329407913230",
        "hello world",
        2290,
        "task_2290_main_a1",
    )

    assert comment_id is None
    assert error == "input_not_found: timeout"
    assert stage == "publish"
    assert reauth_marks == []
    assert failures
    assert failures[-1][2] == "unknown_ui_blocked:input_not_found:unknown"
    assert engagement_engine._HTTP_ACCOUNT_HEALTH[account.id]["ui_no_progress"] == 0


@pytest.mark.unit
@pytest.mark.asyncio
async def test_repeated_input_missing_with_logged_in_ui_gets_runtime_cooldown(db_session, monkeypatch):
    account = Account(id=129, label="main", status="active", cookies='{"sid_guard":"x","ttwid":"y"}', proxy="http://proxy-main")
    failures = []
    reauth_marks = []
    engagement_engine._HTTP_ACCOUNT_HEALTH.pop(account.id, None)

    async def _fake_sleep(_seconds):
        return None

    async def _fake_create_page_for_account(_account):
        return _DummyContext(), _DummyPage()

    async def _fake_bootstrap_session(*args, **kwargs):
        return SimpleNamespace(
            status="ready",
            can_attempt_publish=True,
            cookies={"sid_guard": "x", "ttwid": "y"},
            auth_present=["sid_guard"],
        )

    async def _fake_publish_comment_hybrid(*args, **kwargs):
        return SimpleNamespace(
            status="dom_error",
            error_reason="input_not_found: timeout",
            dom_strategy=None,
            elapsed_seconds=1.0,
        )

    async def _fake_detect_captcha(*args, **kwargs):
        return False, None

    async def _fake_login_state(*args, **kwargs):
        return {"state": "logged_in", "loggedInCount": 2, "loginCount": 0}

    monkeypatch.setattr(engagement_engine, "get_http_execution_strategy", lambda *args, **kwargs: {"strategy": "dom_first"})
    monkeypatch.setattr(engagement_engine, "create_page_for_account", _fake_create_page_for_account)
    monkeypatch.setattr(engagement_engine, "needs_enrichment", lambda *args, **kwargs: False)
    monkeypatch.setattr(engagement_engine, "bootstrap_session", _fake_bootstrap_session)
    monkeypatch.setattr(engagement_engine, "publish_comment_hybrid", _fake_publish_comment_hybrid)
    monkeypatch.setattr(engagement_engine, "detect_captcha", _fake_detect_captcha)
    monkeypatch.setattr(engagement_engine, "get_browser_login_state", _fake_login_state)
    monkeypatch.setattr(engagement_engine, "_register_runtime_success", lambda *args, **kwargs: None)
    monkeypatch.setattr(engagement_engine, "_register_runtime_failure", lambda *args, **kwargs: failures.append(args))
    monkeypatch.setattr(engagement_engine, "mark_needs_reauth", lambda *args, **kwargs: reauth_marks.append((args, kwargs)))
    monkeypatch.setattr(engagement_engine.asyncio, "sleep", _fake_sleep)

    for attempt in range(engagement_engine.UI_NO_PROGRESS_COOLDOWN_AFTER):
        comment_id, error, stage = await engagement_engine.attempt_main_comment_on_account(
            db_session,
            account,
            "https://www.tiktok.com/@demo/video/7635267329407913230",
            "hello world",
            1850 + attempt,
            f"task_1850_main_a{attempt}",
        )
        assert comment_id is None
        assert error == "input_not_found: timeout"
        assert stage == "publish"

    assert reauth_marks == []
    assert failures
    assert failures[-1][2] == "unknown_ui_blocked:input_not_found:logged_in"
    assert engagement_engine._HTTP_ACCOUNT_HEALTH[account.id]["ui_no_progress"] == 0


@pytest.mark.unit
@pytest.mark.asyncio
async def test_bootstrap_timeout_with_login_auth_is_runtime_failure_not_reauth(db_session, monkeypatch):
    account = Account(id=130, label="main", status="active", cookies='{"sid_guard":"x"}', proxy="http://proxy-main")
    failures = []
    reauth_marks = []

    async def _fake_sleep(_seconds):
        return None

    async def _fake_create_page_for_account(_account):
        return _DummyContext(), _DummyPage()

    async def _fake_bootstrap_session(*args, **kwargs):
        return SimpleNamespace(
            status="timeout",
            can_attempt_publish=False,
            cookies={"sid_guard": "x"},
            auth_present=["sid_guard"],
            generated_present=[],
        )

    monkeypatch.setattr(engagement_engine, "get_http_execution_strategy", lambda *args, **kwargs: {"strategy": "dom_first"})
    monkeypatch.setattr(engagement_engine, "create_page_for_account", _fake_create_page_for_account)
    monkeypatch.setattr(engagement_engine, "needs_enrichment", lambda *args, **kwargs: False)
    monkeypatch.setattr(engagement_engine, "bootstrap_session", _fake_bootstrap_session)
    monkeypatch.setattr(engagement_engine, "_register_runtime_failure", lambda *args, **kwargs: failures.append(args))
    monkeypatch.setattr(engagement_engine, "mark_needs_reauth", lambda *args, **kwargs: reauth_marks.append((args, kwargs)))
    monkeypatch.setattr(engagement_engine.asyncio, "sleep", _fake_sleep)

    comment_id, error, stage = await engagement_engine.attempt_main_comment_on_account(
        db_session,
        account,
        "https://www.tiktok.com/@demo/video/7635267329407913230",
        "hello world",
        190,
        "task_190_main_a1",
    )

    assert comment_id is None
    assert error == "bootstrap_timeout_incomplete_session"
    assert stage == "bootstrap"
    assert reauth_marks == []
    assert failures
    assert failures[0][2] == "bootstrap_timeout_incomplete_session"


@pytest.mark.unit
@pytest.mark.asyncio
async def test_attempt_main_comment_marks_reauth_when_input_missing_and_browser_logged_out(db_session, monkeypatch):
    account = Account(id=30, label="main", status="active", cookies='{"sid_guard":"x","ttwid":"y"}', proxy="http://proxy-main")
    reauth_marks = []

    async def _fake_sleep(_seconds):
        return None

    async def _fake_create_page_for_account(_account):
        return _DummyContext(), _DummyPage()

    async def _fake_bootstrap_session(*args, **kwargs):
        return SimpleNamespace(
            status="ready",
            can_attempt_publish=True,
            cookies={"sid_guard": "x", "ttwid": "y"},
            auth_present=["sid_guard"],
        )

    async def _fake_publish_comment_hybrid(*args, **kwargs):
        return SimpleNamespace(
            status="dom_error",
            error_reason="input_not_found: timeout",
            dom_strategy=None,
            elapsed_seconds=1.0,
        )

    async def _fake_detect_captcha(*args, **kwargs):
        return False, None

    async def _fake_login_state(*args, **kwargs):
        return {"state": "logged_out", "loggedInCount": 0, "loginCount": 1}

    monkeypatch.setattr(engagement_engine, "get_http_execution_strategy", lambda *args, **kwargs: {"strategy": "dom_first"})
    monkeypatch.setattr(engagement_engine, "create_page_for_account", _fake_create_page_for_account)
    monkeypatch.setattr(engagement_engine, "needs_enrichment", lambda *args, **kwargs: False)
    monkeypatch.setattr(engagement_engine, "bootstrap_session", _fake_bootstrap_session)
    monkeypatch.setattr(engagement_engine, "publish_comment_hybrid", _fake_publish_comment_hybrid)
    monkeypatch.setattr(engagement_engine, "detect_captcha", _fake_detect_captcha)
    monkeypatch.setattr(engagement_engine, "get_browser_login_state", _fake_login_state)
    monkeypatch.setattr(engagement_engine, "_register_runtime_success", lambda *args, **kwargs: None)
    monkeypatch.setattr(engagement_engine, "_register_runtime_failure", lambda *args, **kwargs: None)
    monkeypatch.setattr(engagement_engine, "mark_needs_reauth", lambda *args, **kwargs: reauth_marks.append((args, kwargs)))
    monkeypatch.setattr(engagement_engine.asyncio, "sleep", _fake_sleep)

    comment_id, error, stage = await engagement_engine.attempt_main_comment_on_account(
        db_session,
        account,
        "https://www.tiktok.com/@demo/video/7635267329407913230",
        "hello world",
        186,
        "task_186_main_a1",
    )

    assert comment_id is None
    assert error == "input_not_found: timeout"
    assert stage == "publish"
    assert reauth_marks
    assert reauth_marks[0][1]["reason"] == "input_not_found_logged_out"


@pytest.mark.unit
@pytest.mark.asyncio
async def test_execute_hybrid_full_resolves_shortlink_before_attempts(db_session, monkeypatch):
    """HYBRID FULL should pass the canonical URL through the whole flow once
    resolve_video_url can expand a shortlink.
    """
    account = Account(id=21, label="main", status="active", cookies='{"sessionid":"x"}', proxy="http://proxy-main")
    task = SimpleNamespace(reply1="reply one", reply2=None, reply3=None, settings={"reply_count": 1})
    resolved_url = "https://www.tiktok.com/@demo/video/7635267329407913230"
    captured = {}

    async def _fake_resolve_video_url(url):
        assert url == "https://vt.tiktok.com/ZS9n129QV/"
        return resolved_url, "7635267329407913230"

    async def _fake_attempt_main_comment_on_account(db, account_arg, video_url, *args, **kwargs):
        captured["video_url"] = video_url
        return "cid_main_123", None, "http"

    async def _fake_verify_comment_dom(*args, **kwargs):
        captured["verify_video_url"] = kwargs.get("video_url")
        return True, "cid_main_123", "tester", "standard"

    async def _fake_order_boost_likes(**kwargs):
        captured["boost_video_url"] = kwargs.get("video_url")
        return "boost_order_42", None

    async def _fake_execute_replies_http(*args, **kwargs):
        captured["reply_video_url"] = kwargs.get("video_url")
        return [
            {
                "text": "reply one",
                "success": True,
                "method": "http",
                "error": None,
                "reply_id": "reply_123",
                "visibility_status": "confirmed",
            }
        ]

    monkeypatch.setattr(engagement_engine, "resolve_video_url", _fake_resolve_video_url)
    monkeypatch.setattr(engagement_engine, "get_free_account_for_main_comment", lambda *args, **kwargs: account)
    monkeypatch.setattr(engagement_engine, "attempt_main_comment_on_account", _fake_attempt_main_comment_on_account)
    monkeypatch.setattr(engagement_engine, "verify_comment_dom", _fake_verify_comment_dom)
    monkeypatch.setattr(engagement_engine, "execute_replies_http", _fake_execute_replies_http)
    monkeypatch.setattr(engagement_engine, "_get_task_boost_config", lambda *args, **kwargs: (True, 100))
    monkeypatch.setattr(engagement_engine, "_order_boost_likes", _fake_order_boost_likes)

    result = await engagement_engine.execute_hybrid_full(
        db_session,
        task,
        "https://vt.tiktok.com/ZS9n129QV/",
        "hello world",
        176,
    )

    assert result.main_comment["success"] is True
    assert captured["video_url"] == resolved_url
    assert captured["verify_video_url"] == resolved_url
    assert captured["reply_video_url"] == resolved_url
    assert captured["boost_video_url"] == resolved_url


@pytest.mark.unit
@pytest.mark.asyncio
async def test_execute_hybrid_full_releases_main_account_reservation_on_success(db_session, monkeypatch):
    account = Account(id=24, label="main", status="active", cookies='{"sessionid":"x"}', proxy="http://proxy-main")
    task = SimpleNamespace(reply1="reply one", reply2=None, reply3=None, settings={"reply_count": 1})
    released = []

    async def _fake_attempt_main_comment_on_account(*args, **kwargs):
        return "cid_main_123", None, "http"

    async def _fake_verify_comment_dom(*args, **kwargs):
        return True, "cid_main_123", "main", "fast_precheck"

    async def _fake_order_boost_likes(**kwargs):
        return None, None

    monkeypatch.setattr(engagement_engine, "resolve_video_url", lambda url: _async_tuple(url, "1234567890123456789"))
    monkeypatch.setattr(engagement_engine, "get_free_account_for_main_comment", lambda *args, **kwargs: account)
    monkeypatch.setattr(engagement_engine, "attempt_main_comment_on_account", _fake_attempt_main_comment_on_account)
    monkeypatch.setattr(engagement_engine, "verify_comment_dom", _fake_verify_comment_dom)
    monkeypatch.setattr(engagement_engine, "_order_boost_likes", _fake_order_boost_likes)
    monkeypatch.setattr(engagement_engine, "release_main_account_reservation", lambda account_id: released.append(account_id))

    result = await engagement_engine.execute_hybrid_full(
        db_session,
        task,
        "https://www.tiktok.com/@demo/video/1234567890123456789",
        "hello world",
        180,
    )

    assert result.main_comment["success"] is True
    assert released == [account.id]


@pytest.mark.unit
@pytest.mark.asyncio
async def test_execute_hybrid_full_accepts_local_main_without_id_without_replies(db_session, monkeypatch):
    account = Account(id=27, label="main", status="active", cookies='{"sessionid":"x"}', proxy="http://proxy-main")
    task = SimpleNamespace(reply1=None, reply2=None, reply3=None, settings={})
    released = []
    verify_calls = []

    async def _fake_attempt_main_comment_on_account(*args, **kwargs):
        outcome = kwargs.get("outcome_dict")
        outcome.update(
            {
                "posted": True,
                "network_confirmed": False,
                "local_confirmed": True,
                "local_confirmation_without_id": True,
                "confirmation_source": "local_dom_text_match_no_id",
                "method": "hybrid_dom",
            }
        )
        return None, None, "hybrid_local_no_id"

    async def _fake_verify_comment_dom(*args, **kwargs):
        verify_calls.append((args, kwargs))
        return False, None, None, "standard"

    async def _fake_order_boost_likes(**kwargs):
        return None, None

    monkeypatch.setattr(engagement_engine, "resolve_video_url", lambda url: _async_tuple(url, "1234567890123456789"))
    monkeypatch.setattr(engagement_engine, "get_free_account_for_main_comment", lambda *args, **kwargs: account)
    monkeypatch.setattr(engagement_engine, "attempt_main_comment_on_account", _fake_attempt_main_comment_on_account)
    monkeypatch.setattr(engagement_engine, "verify_comment_dom", _fake_verify_comment_dom)
    monkeypatch.setattr(engagement_engine, "_get_task_boost_config", lambda *args, **kwargs: (False, 100))
    monkeypatch.setattr(engagement_engine, "_order_boost_likes", _fake_order_boost_likes)
    monkeypatch.setattr(engagement_engine, "release_main_account_reservation", lambda account_id: released.append(account_id))

    result = await engagement_engine.execute_hybrid_full(
        db_session,
        task,
        "https://www.tiktok.com/@demo/video/1234567890123456789",
        "hello world",
        185,
    )

    payload = result.to_dict(source="manual", mode="full")

    assert len(verify_calls) == 0
    assert released == [account.id]
    assert result.main_comment["success"] is True
    assert result.main_comment["submitted_unconfirmed"] is True
    assert result.main_comment["comment_id"] is None
    assert result.main_comment["error"] == "main_submitted_unconfirmed_manual_check"
    assert payload["status"] == "submitted_unconfirmed"
    assert any(w.get("code") == "main_submitted_unconfirmed" for w in payload.get("warnings") or [])


@pytest.mark.unit
@pytest.mark.asyncio
async def test_execute_hybrid_full_accepts_local_main_without_id_after_public_verify(db_session, monkeypatch):
    account = Account(id=28, label="main", status="active", cookies='{"sessionid":"x"}', proxy="http://proxy-main")
    task = SimpleNamespace(reply1="reply one", reply2=None, reply3=None, settings={"reply_count": 1})
    released = []
    verify_calls = []
    reply_calls = []

    async def _fake_attempt_main_comment_on_account(*args, **kwargs):
        outcome = kwargs.get("outcome_dict")
        outcome.update(
            {
                "posted": True,
                "network_confirmed": False,
                "local_confirmed": True,
                "local_confirmation_without_id": True,
                "confirmation_source": "local_dom_text_match_no_id",
                "method": "hybrid_dom",
            }
        )
        return None, None, "hybrid_local_no_id"

    async def _fake_verify_comment_dom(*args, **kwargs):
        verify_calls.append((args, kwargs))
        return True, "cid_public_123", "main", "standard"

    async def _fake_order_boost_likes(**kwargs):
        return None, None

    async def _fake_execute_replies_http(*args, **kwargs):
        reply_calls.append(kwargs)
        return [
            {
                "text": "reply one",
                "success": True,
                "method": "http",
                "error": None,
                "reply_id": "reply_123",
                "visibility_status": "confirmed",
            }
        ]

    monkeypatch.setattr(engagement_engine, "resolve_video_url", lambda url: _async_tuple(url, "1234567890123456789"))
    monkeypatch.setattr(engagement_engine, "get_free_account_for_main_comment", lambda *args, **kwargs: account)
    monkeypatch.setattr(engagement_engine, "attempt_main_comment_on_account", _fake_attempt_main_comment_on_account)
    monkeypatch.setattr(engagement_engine, "verify_comment_dom", _fake_verify_comment_dom)
    monkeypatch.setattr(engagement_engine, "execute_replies_http", _fake_execute_replies_http)
    monkeypatch.setattr(engagement_engine, "_get_task_boost_config", lambda *args, **kwargs: (False, 100))
    monkeypatch.setattr(engagement_engine, "_order_boost_likes", _fake_order_boost_likes)
    monkeypatch.setattr(engagement_engine, "release_main_account_reservation", lambda account_id: released.append(account_id))

    result = await engagement_engine.execute_hybrid_full(
        db_session,
        task,
        "https://www.tiktok.com/@demo/video/1234567890123456789",
        "hello world",
        186,
    )

    payload = result.to_dict(source="manual", mode="full")

    assert len(verify_calls) == 1
    assert verify_calls[0][1]["require_author_match"] is True
    assert released == [account.id]
    assert result.main_comment["success"] is True
    assert result.main_comment["verified"] is True
    assert result.main_comment["submitted_unconfirmed"] is False
    assert result.main_comment["comment_id"] == "cid_public_123"
    assert result.comment_id == "cid_public_123"
    assert len(reply_calls) == 1
    assert reply_calls[0]["parent_comment_id"] == "cid_public_123"
    assert payload["status"] == "success"


@pytest.mark.unit
@pytest.mark.asyncio
async def test_execute_hybrid_full_replies_by_dom_when_public_main_has_no_id(db_session, monkeypatch):
    account = Account(id=29, label="main", status="active", cookies='{"sessionid":"x"}', proxy="http://proxy-main")
    task = SimpleNamespace(reply1="reply one", reply2=None, reply3=None, settings={"reply_count": 1})
    released = []
    reply_calls = []

    async def _fake_attempt_main_comment_on_account(*args, **kwargs):
        outcome = kwargs.get("outcome_dict")
        outcome.update(
            {
                "posted": True,
                "network_confirmed": False,
                "local_confirmed": True,
                "local_confirmation_without_id": True,
                "confirmation_source": "local_dom_text_match_no_id",
                "method": "hybrid_dom",
            }
        )
        return None, None, "hybrid_local_no_id"

    async def _fake_verify_comment_dom(*args, **kwargs):
        return True, None, "main", "standard"

    async def _fake_execute_replies_http(*args, **kwargs):
        reply_calls.append(kwargs)
        return [
            {
                "text": "reply one",
                "success": True,
                "method": "dom_text_parent",
                "error": None,
                "reply_id": "reply_123",
                "parent_confirmed": True,
                "parent_confirmation_level": "ui_author_only",
                "visibility_status": "confirmed_local",
            }
        ]

    async def _fake_order_boost_likes(**kwargs):
        return None, None

    monkeypatch.setattr(engagement_engine, "resolve_video_url", lambda url: _async_tuple(url, "1234567890123456789"))
    monkeypatch.setattr(engagement_engine, "get_free_account_for_main_comment", lambda *args, **kwargs: account)
    monkeypatch.setattr(engagement_engine, "attempt_main_comment_on_account", _fake_attempt_main_comment_on_account)
    monkeypatch.setattr(engagement_engine, "verify_comment_dom", _fake_verify_comment_dom)
    monkeypatch.setattr(engagement_engine, "execute_replies_http", _fake_execute_replies_http)
    monkeypatch.setattr(engagement_engine, "_get_task_boost_config", lambda *args, **kwargs: (False, 100))
    monkeypatch.setattr(engagement_engine, "_order_boost_likes", _fake_order_boost_likes)
    monkeypatch.setattr(engagement_engine, "release_main_account_reservation", lambda account_id: released.append(account_id))

    result = await engagement_engine.execute_hybrid_full(
        db_session,
        task,
        "https://www.tiktok.com/@demo/video/1234567890123456789",
        "hello world",
        187,
    )

    payload = result.to_dict(source="manual", mode="full")

    assert released == [account.id]
    assert result.main_comment["success"] is True
    assert result.main_comment["verified"] is True
    assert result.main_comment["comment_id"] is None
    assert result.comment_id is None
    assert len(reply_calls) == 1
    assert reply_calls[0]["parent_comment_id"] == ""
    assert reply_calls[0]["parent_author"] == "main"
    assert reply_calls[0]["main_comment_text"] == "hello world"
    assert result.replies[0]["success"] is True
    assert payload["status"] == "success"


@pytest.mark.unit
@pytest.mark.asyncio
async def test_execute_engagement_only_replies_by_dom_when_found_comment_has_no_id(db_session, monkeypatch):
    import executor.user_schema as user_schema

    task = SimpleNamespace(reply1="reply one", reply2=None, reply3=None, settings={"reply_count": 1})
    account = Account(id=261, label="reply-dom", status="active", cookies='{"sessionid":"x"}')
    reply_calls = []

    async def _fake_find_comment_for_engagement(*args, **kwargs):
        return True, None, "main_author"

    async def _fake_reply_with_variant_on_all_mains(
        _db,
        reply_account,
        _video_url,
        pending_parent,
        reply_text,
        *,
        variant_index,
        **_kwargs,
    ):
        parent = pending_parent[0]
        reply_calls.append(
            {
                "account_id": reply_account.id,
                "parent_comment_id": parent.comment_id or "",
                "parent_author": parent.tiktok_username,
                "main_comment_text": parent.text,
                "reply_text": reply_text,
                "variant_index": variant_index,
            }
        )
        return user_schema.ReplyAttemptResult(
            replies=[
                user_schema.ReplyOutcome(
                    text=reply_text,
                    variant_index=variant_index,
                    matched_main_text=parent.text,
                    parent_comment_id=None,
                    success=True,
                    method="dom_text_parent",
                    error=None,
                    account_id=reply_account.id,
                    attempted=True,
                )
            ]
        )

    async def _fake_order_boost_likes(**kwargs):
        return None, None

    monkeypatch.setattr(engagement_engine, "find_comment_for_engagement", _fake_find_comment_for_engagement)
    monkeypatch.setattr(engagement_engine, "get_free_account_for_reply", lambda *args, **kwargs: account)
    monkeypatch.setattr(user_schema, "_reply_with_variant_on_all_mains", _fake_reply_with_variant_on_all_mains)
    monkeypatch.setattr(engagement_engine, "_get_task_boost_config", lambda *args, **kwargs: (False, 100))
    monkeypatch.setattr(engagement_engine, "_order_boost_likes", _fake_order_boost_likes)

    result = await engagement_engine.execute_engagement_only(
        db_session,
        task,
        "https://www.tiktok.com/@demo/video/1234567890123456789",
        comment_id=None,
        username="main_author",
        comment_text="How much is my locker even worth bro \U0001f62d",
        task_id=188,
    )

    assert result.comment_found is True
    assert result.comment_id is None
    assert result.comment_author == "main_author"
    assert len(reply_calls) == 1
    assert reply_calls[0]["parent_comment_id"] == ""
    assert reply_calls[0]["parent_author"] == "main_author"
    assert reply_calls[0]["main_comment_text"] == "How much is my locker even worth bro \U0001f62d"
    assert result.replies[0]["method"] == "dom_text_parent"


@pytest.mark.unit
@pytest.mark.asyncio
async def test_engagement_only_full_flow_reports_submitted_unconfirmed_reply(db_session, monkeypatch):
    import executor.user_schema as user_schema

    account = Account(id=271, label="reply-dom", status="active", cookies='{"sessionid":"x"}')
    attempts = []

    async def _fake_reply_with_variant_on_all_mains(
        _db,
        reply_account,
        _video_url,
        pending_parent,
        reply_text,
        *,
        variant_index,
        already_replied_parent_keys,
        **_kwargs,
    ):
        parent = pending_parent[0]
        attempts.append(reply_account.id)
        already_replied_parent_keys.add(str(parent.comment_id))
        return user_schema.ReplyAttemptResult(
            replies=[
                user_schema.ReplyOutcome(
                    text=reply_text,
                    variant_index=variant_index,
                    matched_main_text=parent.text,
                    parent_comment_id=parent.comment_id,
                    success=False,
                    method="hybrid_dom",
                    error="stealth_reject_empty_body",
                    account_id=reply_account.id,
                    attempted=True,
                    submitted_unconfirmed=True,
                )
            ],
            searched=True,
        )

    monkeypatch.setattr(engagement_engine, "get_free_account_for_reply", lambda *args, **kwargs: account)
    monkeypatch.setattr(user_schema, "_reply_with_variant_on_all_mains", _fake_reply_with_variant_on_all_mains)

    results, resolved_parent_id, resolved_author = await engagement_engine.execute_engagement_only_replies_via_full_flow(
        db=db_session,
        video_url="https://www.tiktok.com/@demo/video/1234567890123456789",
        comment_text="target comment",
        comment_id="parent_123",
        comment_author="main_author",
        replies=["reply one"],
        task_id=199,
    )

    assert attempts == [271]
    assert resolved_parent_id == "parent_123"
    assert resolved_author == "main_author"
    assert len(results) == 1
    assert results[0]["success"] is False
    assert results[0]["submitted_unconfirmed"] is True
    assert results[0]["visibility_status"] == "submitted_unconfirmed"
    assert results[0]["error"] == "stealth_reject_empty_body"
    assert results[0]["parent_confirmed"] is True
    assert results[0]["attempts"] == 1


@pytest.mark.unit
@pytest.mark.asyncio
async def test_engagement_only_full_flow_runs_three_reply_variants_in_parallel(db_session, monkeypatch):
    import db.database as db_database
    import executor.browser_pool as browser_pool
    import executor.user_schema as user_schema

    accounts = [
        Account(id=381, label="reply-a", status="active", cookies='{"sessionid":"x"}'),
        Account(id=382, label="reply-b", status="active", cookies='{"sessionid":"x"}'),
        Account(id=383, label="reply-c", status="active", cookies='{"sessionid":"x"}'),
    ]
    started_variants = []
    capacity_requests = []
    close_calls = []
    release_batch = asyncio.Event()

    @contextmanager
    def _fake_db_context():
        yield db_session

    def _fake_get_free_account_for_reply(_db, exclude_ids=None, **_kwargs):
        excluded = set(exclude_ids or [])
        return next((account for account in accounts if account.id not in excluded), None)

    async def _fake_reply_with_variant_on_all_mains(
        _db,
        reply_account,
        _video_url,
        pending_parent,
        reply_text,
        *,
        variant_index,
        **_kwargs,
    ):
        parent = pending_parent[0]
        started_variants.append(variant_index)
        if len(started_variants) == 3:
            release_batch.set()
        await asyncio.wait_for(release_batch.wait(), timeout=1)
        return user_schema.ReplyAttemptResult(
            replies=[
                user_schema.ReplyOutcome(
                    text=reply_text,
                    variant_index=variant_index,
                    matched_main_text=parent.text,
                    parent_comment_id=parent.comment_id,
                    success=True,
                    method="hybrid_dom",
                    error=None,
                    account_id=reply_account.id,
                    attempted=True,
                )
            ],
            searched=True,
        )

    async def _fake_close_browser_pool():
        close_calls.append("close")

    monkeypatch.setattr(db_database, "get_sync_db_context", _fake_db_context)
    monkeypatch.setattr(engagement_engine, "get_free_account_for_reply", _fake_get_free_account_for_reply)
    monkeypatch.setattr(user_schema, "_reply_with_variant_on_all_mains", _fake_reply_with_variant_on_all_mains)
    monkeypatch.setattr(browser_pool, "ensure_thread_browser_pool_capacity", lambda value: capacity_requests.append(value))
    monkeypatch.setattr(browser_pool, "close_browser_pool", _fake_close_browser_pool)

    results, resolved_parent_id, resolved_author = await engagement_engine.execute_engagement_only_replies_via_full_flow(
        db=db_session,
        video_url="https://www.tiktok.com/@demo/video/1234567890123456789",
        comment_text="target comment",
        comment_id="parent_123",
        comment_author="main_author",
        replies=["reply one", "reply two", "reply three"],
        task_id=202,
    )

    assert started_variants == [1, 2, 3]
    assert capacity_requests == [4]
    assert close_calls == ["close"]
    assert resolved_parent_id == "parent_123"
    assert resolved_author == "main_author"
    assert [row["variant_index"] for row in results] == [1, 2, 3]
    assert all(row["success"] is True for row in results)
    assert {row["account_id"] for row in results} == {381, 382, 383}


@pytest.mark.unit
@pytest.mark.asyncio
async def test_engagement_only_dynamic_disabled_uses_three_wide_batch_capacity(db_session, monkeypatch):
    import db.database as db_database
    import executor.browser_pool as browser_pool
    import executor.user_schema as user_schema

    accounts = [
        Account(id=386 + i, label=f"reply-batch-{i}", status="active", cookies='{"sessionid":"x"}')
        for i in range(3)
    ]
    capacity_requests = []
    close_calls = []
    selection_kwargs = []

    @contextmanager
    def _fake_db_context():
        yield db_session

    def _fake_get_free_account_for_reply(_db, exclude_ids=None, **_kwargs):
        selection_kwargs.append(dict(_kwargs))
        excluded = set(exclude_ids or [])
        return next((account for account in accounts if account.id not in excluded), None)

    async def _fake_reply_with_variant_on_all_mains(
        _db,
        reply_account,
        _video_url,
        pending_parent,
        reply_text,
        *,
        variant_index,
        **_kwargs,
    ):
        parent = pending_parent[0]
        return user_schema.ReplyAttemptResult(
            replies=[
                user_schema.ReplyOutcome(
                    text=reply_text,
                    variant_index=variant_index,
                    matched_main_text=parent.text,
                    parent_comment_id=parent.comment_id,
                    success=True,
                    method="hybrid_dom",
                    error=None,
                    account_id=reply_account.id,
                    attempted=True,
                )
            ],
            searched=True,
        )

    async def _fake_close_browser_pool():
        close_calls.append("close")

    monkeypatch.setattr(engagement_engine.settings, "PARALLEL_DYNAMIC_WORKER_POOL_ENABLED", False, raising=False)
    monkeypatch.setattr(engagement_engine.settings, "PARALLEL_COMMENTING_TARGET_BROWSERS", 10, raising=False)
    monkeypatch.setattr(engagement_engine.settings, "PARALLEL_COMMENTING_MAX_BROWSERS", 4, raising=False)
    monkeypatch.setattr(db_database, "get_sync_db_context", _fake_db_context)
    monkeypatch.setattr(engagement_engine, "get_free_account_for_reply", _fake_get_free_account_for_reply)
    monkeypatch.setattr(user_schema, "_reply_with_variant_on_all_mains", _fake_reply_with_variant_on_all_mains)
    monkeypatch.setattr(browser_pool, "ensure_thread_browser_pool_capacity", lambda value: capacity_requests.append(value))
    monkeypatch.setattr(browser_pool, "close_browser_pool", _fake_close_browser_pool)

    results, _resolved_parent_id, _resolved_author = await engagement_engine.execute_engagement_only_replies_via_full_flow(
        db=db_session,
        video_url="https://www.tiktok.com/@demo/video/1234567890123456789",
        comment_text="target comment",
        comment_id="parent_123",
        comment_author="main_author",
        replies=["reply one", "reply two", "reply three"],
        task_id=205,
    )

    assert capacity_requests == [3]
    assert close_calls == ["close"]
    assert [row["variant_index"] for row in results] == [1, 2, 3]
    assert selection_kwargs
    assert all(
        kwargs.get("exclude_degraded_categories") == {"captcha", "page_blocked"}
        for kwargs in selection_kwargs
    )


@pytest.mark.unit
@pytest.mark.asyncio
async def test_engagement_only_full_flow_refills_worker_after_fast_failure(db_session, monkeypatch):
    import db.database as db_database
    import executor.browser_pool as browser_pool
    import executor.user_schema as user_schema

    accounts = [
        Account(id=391, label="reply-a", status="active", cookies='{"sessionid":"x"}', proxy="http://proxy-a:1000"),
        Account(id=392, label="reply-b", status="active", cookies='{"sessionid":"x"}', proxy="http://proxy-b:1000"),
        Account(id=393, label="reply-c", status="active", cookies='{"sessionid":"x"}', proxy="http://proxy-c:1000"),
        Account(id=394, label="reply-d", status="active", cookies='{"sessionid":"x"}', proxy="http://proxy-d:1000"),
        Account(id=395, label="reply-a2", status="active", cookies='{"sessionid":"x"}', proxy="http://proxy-a:1000"),
    ]
    events = []
    account_by_variant = {}
    release_slow_replies = asyncio.Event()
    variant_one_attempts = 0

    @contextmanager
    def _fake_db_context():
        yield db_session

    def _fake_get_free_account_for_reply(_db, exclude_ids=None, exclude_proxies=None, **_kwargs):
        excluded = set(exclude_ids or [])
        excluded_proxies = set(exclude_proxies or [])
        return next(
            (
                account
                for account in accounts
                if account.id not in excluded
                and str(account.proxy or "").strip() not in excluded_proxies
            ),
            None,
        )

    async def _fake_reply_with_variant_on_all_mains(
        _db,
        reply_account,
        _video_url,
        pending_parent,
        reply_text,
        *,
        variant_index,
        **_kwargs,
    ):
        nonlocal variant_one_attempts
        parent = pending_parent[0]
        account_by_variant[variant_index] = reply_account.id
        events.append(("start", variant_index))
        if variant_index == 1 and variant_one_attempts == 0:
            variant_one_attempts += 1
            events.append(("finish", variant_index))
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
                        account_id=reply_account.id,
                        attempted=False,
                    )
                ]
            )
        if variant_index == 4:
            release_slow_replies.set()
        elif variant_index in {2, 3}:
            await asyncio.wait_for(release_slow_replies.wait(), timeout=1)
        events.append(("finish", variant_index))
        return user_schema.ReplyAttemptResult(
            replies=[
                user_schema.ReplyOutcome(
                    text=reply_text,
                    variant_index=variant_index,
                    matched_main_text=parent.text,
                    parent_comment_id=parent.comment_id,
                    success=True,
                    method="hybrid_dom",
                    error=None,
                    account_id=reply_account.id,
                    attempted=True,
                )
            ],
            found_mains=[parent],
            searched=True,
        )

    monkeypatch.setattr(db_database, "get_sync_db_context", _fake_db_context)
    monkeypatch.setattr(engagement_engine, "get_free_account_for_reply", _fake_get_free_account_for_reply)
    monkeypatch.setattr(user_schema, "_reply_with_variant_on_all_mains", _fake_reply_with_variant_on_all_mains)
    monkeypatch.setattr(browser_pool, "ensure_thread_browser_pool_capacity", lambda _value: None)
    monkeypatch.setattr(browser_pool, "close_browser_pool", AsyncMock(return_value=None))

    results, _resolved_parent_id, _resolved_author = await engagement_engine.execute_engagement_only_replies_via_full_flow(
        db=db_session,
        video_url="https://www.tiktok.com/@demo/video/1234567890123456789",
        comment_text="target comment",
        comment_id="parent_123",
        comment_author="main_author",
        replies=["reply one", "reply two", "reply three", "reply four"],
        task_id=203,
    )

    assert {event for event in events if event[0] == "start"} >= {
        ("start", 1),
        ("start", 2),
        ("start", 3),
        ("start", 4),
    }
    assert events.index(("start", 4)) > events.index(("start", 1))
    assert events.index(("start", 4)) > events.index(("start", 2))
    assert events.index(("start", 4)) > events.index(("start", 3))
    assert events.index(("start", 4)) < events.index(("finish", 2))
    assert events.index(("start", 4)) < events.index(("finish", 3))
    assert any(row["variant_index"] == 4 and row["success"] is True for row in results)
    assert account_by_variant[4] == 394


@pytest.mark.unit
@pytest.mark.asyncio
async def test_engagement_only_full_flow_balances_four_workers_across_two_proxies(db_session, monkeypatch):
    import db.database as db_database
    import executor.browser_pool as browser_pool
    import executor.user_schema as user_schema

    accounts = [
        Account(id=421, label="reply-a-1", status="active", cookies='{"sessionid":"x"}', proxy="http://proxy-a:1000"),
        Account(id=422, label="reply-a-2", status="active", cookies='{"sessionid":"x"}', proxy="http://proxy-a:1000"),
        Account(id=423, label="reply-b-1", status="active", cookies='{"sessionid":"x"}', proxy="http://proxy-b:1000"),
        Account(id=424, label="reply-b-2", status="active", cookies='{"sessionid":"x"}', proxy="http://proxy-b:1000"),
    ]
    proxy_by_variant: dict[int, str] = {}

    @contextmanager
    def _fake_db_context():
        yield db_session

    def _fake_get_free_account_for_reply(_db, exclude_ids=None, exclude_proxies=None, **_kwargs):
        excluded = set(exclude_ids or [])
        excluded_proxies = set(exclude_proxies or [])
        return next(
            (
                account
                for account in accounts
                if account.id not in excluded
                and str(account.proxy or "").strip() not in excluded_proxies
            ),
            None,
        )

    async def _fake_reply_with_variant_on_all_mains(
        _db,
        reply_account,
        _video_url,
        pending_parent,
        reply_text,
        *,
        variant_index,
        **_kwargs,
    ):
        parent = pending_parent[0]
        proxy_by_variant[variant_index] = str(reply_account.proxy or "")
        return user_schema.ReplyAttemptResult(
            replies=[
                user_schema.ReplyOutcome(
                    text=reply_text,
                    variant_index=variant_index,
                    matched_main_text=parent.text,
                    parent_comment_id=parent.comment_id,
                    success=True,
                    method="hybrid_dom",
                    error=None,
                    account_id=reply_account.id,
                    attempted=True,
                )
            ],
            found_mains=[parent],
            searched=True,
        )

    monkeypatch.setattr(engagement_engine.settings, "PARALLEL_COMMENTING_TARGET_BROWSERS", 4, raising=False)
    monkeypatch.setattr(engagement_engine.settings, "PARALLEL_COMMENTING_MAX_BROWSERS", 4, raising=False)
    monkeypatch.setattr(db_database, "get_sync_db_context", _fake_db_context)
    monkeypatch.setattr(engagement_engine, "get_free_account_for_reply", _fake_get_free_account_for_reply)
    monkeypatch.setattr(user_schema, "_reply_with_variant_on_all_mains", _fake_reply_with_variant_on_all_mains)
    monkeypatch.setattr(browser_pool, "ensure_thread_browser_pool_capacity", lambda _value: None)
    monkeypatch.setattr(browser_pool, "close_browser_pool", AsyncMock(return_value=None))

    results, _resolved_parent_id, _resolved_author = await engagement_engine.execute_engagement_only_replies_via_full_flow(
        db=db_session,
        video_url="https://www.tiktok.com/@demo/video/1234567890123456789",
        comment_text="target comment",
        comment_id="parent_123",
        comment_author="main_author",
        replies=["reply one", "reply two", "reply three", "reply four"],
        task_id=204,
    )

    assert len(results) == 4
    assert len(proxy_by_variant) == 4
    assert list(proxy_by_variant.values()).count("http://proxy-a:1000") == 2
    assert list(proxy_by_variant.values()).count("http://proxy-b:1000") == 2


@pytest.mark.unit
@pytest.mark.asyncio
async def test_engagement_only_dynamic_capacity_defer_waits_before_reselecting(db_session, monkeypatch):
    import db.database as db_database
    import executor.browser_pool as browser_pool
    import executor.user_schema as user_schema

    shared_proxy = "http://proxy-shared:1000"
    accounts = [
        Account(id=441 + i, label=f"reply-shared-{i}", status="active", cookies='{"sessionid":"x"}', proxy=shared_proxy)
        for i in range(3)
    ]
    started: list[int] = []
    selection_calls: list[float] = []
    blocked_selection_calls = 0
    capacity_deferred = asyncio.Event()
    release_first_reply = asyncio.Event()

    @contextmanager
    def _fake_db_context():
        yield db_session

    def _fake_get_free_account_for_reply(_db, exclude_ids=None, exclude_proxies=None, **_kwargs):
        nonlocal blocked_selection_calls
        selection_calls.append(time.monotonic())
        if started == [1] and not release_first_reply.is_set():
            blocked_selection_calls += 1
            if blocked_selection_calls >= 3:
                capacity_deferred.set()
        excluded = set(exclude_ids or [])
        excluded_proxies = set(exclude_proxies or [])
        return next(
            (
                account
                for account in accounts
                if account.id not in excluded
                and str(account.proxy or "").strip() not in excluded_proxies
            ),
            None,
        )

    async def _fake_reply_with_variant_on_all_mains(
        _db,
        reply_account,
        _video_url,
        pending_parent,
        reply_text,
        *,
        variant_index,
        **_kwargs,
    ):
        parent = pending_parent[0]
        started.append(variant_index)
        if variant_index == 1:
            await asyncio.wait_for(release_first_reply.wait(), timeout=2)
        return user_schema.ReplyAttemptResult(
            replies=[
                user_schema.ReplyOutcome(
                    text=reply_text,
                    variant_index=variant_index,
                    matched_main_text=parent.text,
                    parent_comment_id=parent.comment_id,
                    success=True,
                    method="hybrid_dom",
                    error=None,
                    account_id=reply_account.id,
                    attempted=True,
                )
            ],
            found_mains=[parent],
            searched=True,
        )

    monkeypatch.setattr(engagement_engine.settings, "PARALLEL_COMMENTING_TARGET_BROWSERS", 2, raising=False)
    monkeypatch.setattr(engagement_engine.settings, "PARALLEL_COMMENTING_MAX_BROWSERS", 2, raising=False)
    monkeypatch.setattr(engagement_engine.settings, "PARALLEL_MAX_ACTIVE_BROWSERS_PER_PROXY", 1, raising=False)
    monkeypatch.setattr(db_database, "get_sync_db_context", _fake_db_context)
    monkeypatch.setattr(engagement_engine, "get_free_account_for_reply", _fake_get_free_account_for_reply)
    monkeypatch.setattr(user_schema, "_reply_with_variant_on_all_mains", _fake_reply_with_variant_on_all_mains)
    monkeypatch.setattr(browser_pool, "ensure_thread_browser_pool_capacity", lambda _value: None)
    monkeypatch.setattr(browser_pool, "close_browser_pool", AsyncMock(return_value=None))

    run_task = asyncio.create_task(
        engagement_engine.execute_engagement_only_replies_via_full_flow(
            db=db_session,
            video_url="https://www.tiktok.com/@demo/video/1234567890123456789",
            comment_text="target comment",
            comment_id="parent_123",
            comment_author="main_author",
            replies=["reply one", "reply two", "reply three"],
            task_id=207,
        )
    )
    try:
        await asyncio.wait_for(capacity_deferred.wait(), timeout=1)
        await asyncio.sleep(0.05)
        calls_after_defer = len(selection_calls)
        await asyncio.sleep(0.8)
        assert len(selection_calls) == calls_after_defer
        release_first_reply.set()
        results, _resolved_parent_id, _resolved_author = await asyncio.wait_for(run_task, timeout=3)
    finally:
        if not run_task.done():
            run_task.cancel()
            await asyncio.gather(run_task, return_exceptions=True)

    assert [row["variant_index"] for row in results] == [1, 2, 3]
    assert all(row["success"] is True for row in results)


@pytest.mark.unit
@pytest.mark.asyncio
async def test_engagement_only_dynamic_pool_marks_active_and_pending_replies_stopped(db_session, monkeypatch):
    import db.database as db_database
    import executor.browser_pool as browser_pool
    import executor.user_schema as user_schema

    accounts = [
        Account(id=401 + i, label=f"reply-stop-{i}", status="active", cookies='{"sessionid":"x"}')
        for i in range(3)
    ]
    started: list[int] = []
    cancelled: list[int] = []
    close_calls: list[str] = []

    @contextmanager
    def _fake_db_context():
        yield db_session

    def _fake_get_free_account_for_reply(_db, exclude_ids=None, **_kwargs):
        excluded = set(exclude_ids or [])
        return next((account for account in accounts if account.id not in excluded), None)

    async def _fake_reply_with_variant_on_all_mains(
        _db,
        _reply_account,
        _video_url,
        _pending_parent,
        _reply_text,
        *,
        variant_index,
        **_kwargs,
    ):
        started.append(variant_index)
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            cancelled.append(variant_index)
            raise

    async def _fake_close_browser_pool():
        close_calls.append("close")

    monkeypatch.setattr(engagement_engine.settings, "PARALLEL_COMMENTING_TARGET_BROWSERS", 3, raising=False)
    monkeypatch.setattr(engagement_engine.settings, "PARALLEL_COMMENTING_MAX_BROWSERS", 3, raising=False)
    monkeypatch.setattr(db_database, "get_sync_db_context", _fake_db_context)
    monkeypatch.setattr(engagement_engine, "get_free_account_for_reply", _fake_get_free_account_for_reply)
    monkeypatch.setattr(user_schema, "_reply_with_variant_on_all_mains", _fake_reply_with_variant_on_all_mains)
    monkeypatch.setattr(browser_pool, "ensure_thread_browser_pool_capacity", lambda _value: None)
    monkeypatch.setattr(browser_pool, "close_browser_pool", _fake_close_browser_pool)

    results, resolved_parent_id, resolved_author = await engagement_engine.execute_engagement_only_replies_via_full_flow(
        db=db_session,
        video_url="https://www.tiktok.com/@demo/video/1234567890123456789",
        comment_text="target comment",
        comment_id="parent_123",
        comment_author="main_author",
        replies=["reply one", "reply two", "reply three", "reply four"],
        task_id=204,
        stop_requested=lambda: len(started) >= 3,
    )

    assert resolved_parent_id == "parent_123"
    assert resolved_author == "main_author"
    assert set(started) == {1, 2, 3}
    assert set(cancelled) == {1, 2, 3}
    assert close_calls == ["close"]
    assert [row["variant_index"] for row in results] == [1, 2, 3, 4]
    assert {row["error"] for row in results} == {"stopped_by_user"}
    assert results[-1]["account_id"] is None


@pytest.mark.unit
@pytest.mark.asyncio
async def test_engagement_only_unexpected_reply_cancellation_reports_failure(db_session, monkeypatch):
    import db.database as db_database
    import executor.browser_pool as browser_pool
    import executor.user_schema as user_schema

    account = Account(id=431, label="reply-cancelled", status="active", cookies='{"sessionid":"x"}')

    @contextmanager
    def _fake_db_context():
        yield db_session

    def _fake_get_free_account_for_reply(*_args, **_kwargs):
        return account

    async def _fake_reply_with_variant_on_all_mains(*_args, **_kwargs):
        raise asyncio.CancelledError()

    async def _fake_close_browser_pool():
        return None

    monkeypatch.setattr(engagement_engine.settings, "MAX_RETRY_ACCOUNTS", 1, raising=False)
    monkeypatch.setattr(engagement_engine.settings, "PARALLEL_COMMENTING_TARGET_BROWSERS", 1, raising=False)
    monkeypatch.setattr(engagement_engine.settings, "PARALLEL_COMMENTING_MAX_BROWSERS", 1, raising=False)
    monkeypatch.setattr(db_database, "get_sync_db_context", _fake_db_context)
    monkeypatch.setattr(engagement_engine, "get_free_account_for_reply", _fake_get_free_account_for_reply)
    monkeypatch.setattr(user_schema, "_reply_with_variant_on_all_mains", _fake_reply_with_variant_on_all_mains)
    monkeypatch.setattr(browser_pool, "ensure_thread_browser_pool_capacity", lambda _value: None)
    monkeypatch.setattr(browser_pool, "close_browser_pool", _fake_close_browser_pool)

    results, resolved_parent_id, resolved_author = await engagement_engine.execute_engagement_only_replies_via_full_flow(
        db=db_session,
        video_url="https://www.tiktok.com/@demo/video/1234567890123456789",
        comment_text="target comment",
        comment_id="parent_123",
        comment_author="main_author",
        replies=["reply one"],
        task_id=206,
    )

    assert resolved_parent_id == "parent_123"
    assert resolved_author == "main_author"
    assert len(results) == 1
    assert results[0]["variant_index"] == 1
    assert results[0]["account_id"] == 431
    assert results[0]["error"] == "parallel_reply_cancelled"


@pytest.mark.unit
@pytest.mark.asyncio
async def test_engagement_only_terminal_surface_failure_reports_active_and_pending_rows(db_session, monkeypatch):
    import db.database as db_database
    import executor.browser_pool as browser_pool
    import executor.user_schema as user_schema

    accounts = [
        Account(id=451 + i, label=f"reply-surface-{i}", status="active", cookies='{"sessionid":"x"}')
        for i in range(5)
    ]
    cancelled: list[int] = []
    close_calls: list[str] = []

    @contextmanager
    def _fake_db_context():
        yield db_session

    def _fake_get_free_account_for_reply(_db, exclude_ids=None, **_kwargs):
        excluded = set(exclude_ids or [])
        return next((account for account in accounts if account.id not in excluded), None)

    async def _fake_reply_with_variant_on_all_mains(
        _db,
        reply_account,
        _video_url,
        _pending_parent,
        reply_text,
        *,
        variant_index,
        **_kwargs,
    ):
        if variant_index == 2:
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                cancelled.append(variant_index)
                raise
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
                    account_id=reply_account.id,
                    attempted=False,
                )
            ]
        )

    async def _fake_close_browser_pool():
        close_calls.append("close")

    monkeypatch.setattr(engagement_engine.settings, "PARALLEL_COMMENTING_TARGET_BROWSERS", 2, raising=False)
    monkeypatch.setattr(engagement_engine.settings, "PARALLEL_COMMENTING_MAX_BROWSERS", 2, raising=False)
    monkeypatch.setattr(db_database, "get_sync_db_context", _fake_db_context)
    monkeypatch.setattr(engagement_engine, "get_free_account_for_reply", _fake_get_free_account_for_reply)
    monkeypatch.setattr(user_schema, "_reply_with_variant_on_all_mains", _fake_reply_with_variant_on_all_mains)
    monkeypatch.setattr(browser_pool, "ensure_thread_browser_pool_capacity", lambda _value: None)
    monkeypatch.setattr(browser_pool, "close_browser_pool", _fake_close_browser_pool)

    results, _resolved_parent_id, _resolved_author = await engagement_engine.execute_engagement_only_replies_via_full_flow(
        db=db_session,
        video_url="https://www.tiktok.com/@demo/video/1234567890123456789",
        comment_text="target comment",
        comment_id="parent_123",
        comment_author="main_author",
        replies=["reply one", "reply two", "reply three", "reply four", "reply five"],
        task_id=205,
    )

    assert cancelled == [2]
    assert close_calls == ["close"]
    assert {row["variant_index"] for row in results} == {1, 2, 3, 4, 5}
    assert {row["error"] for row in results} == {"comment_list_not_open"}


@pytest.mark.unit
@pytest.mark.asyncio
async def test_engagement_only_author_fallback_is_last_attempt_only(db_session, monkeypatch):
    import executor.user_schema as user_schema

    accounts = [
        Account(id=281, label="reply-dom-a", status="active", cookies='{"sessionid":"x"}'),
        Account(id=282, label="reply-dom-b", status="active", cookies='{"sessionid":"x"}'),
    ]
    calls = []

    def _fake_get_free_account_for_reply(_db, exclude_ids=None, **_kwargs):
        excluded = set(exclude_ids or [])
        return next((account for account in accounts if account.id not in excluded), None)

    async def _fake_reply_with_variant_on_all_mains(
        _db,
        reply_account,
        _video_url,
        pending_parent,
        reply_text,
        *,
        variant_index,
        require_parent_author_match,
        allow_parent_author_fallback,
        **_kwargs,
    ):
        parent = pending_parent[0]
        calls.append(
            {
                "account_id": reply_account.id,
                "require_author": require_parent_author_match,
                "allow_fallback": allow_parent_author_fallback,
            }
        )
        if not allow_parent_author_fallback:
            return user_schema.ReplyAttemptResult(replies=[], searched=True)
        return user_schema.ReplyAttemptResult(
            replies=[
                user_schema.ReplyOutcome(
                    text=reply_text,
                    variant_index=variant_index,
                    matched_main_text=parent.text,
                    parent_comment_id=None,
                    success=True,
                    method="hybrid_dom",
                    error=None,
                    account_id=reply_account.id,
                    attempted=True,
                    requested_parent_author="target_user",
                    matched_parent_author="found_user",
                    parent_author_match_mode="text_author_fallback",
                    author_fallback_used=True,
                    author_fallback_reason="author_mismatch",
                )
            ],
            searched=True,
        )

    monkeypatch.setattr(engagement_engine.settings, "MAX_RETRY_ACCOUNTS", 2)
    monkeypatch.setattr(engagement_engine, "get_free_account_for_reply", _fake_get_free_account_for_reply)
    monkeypatch.setattr(user_schema, "_reply_with_variant_on_all_mains", _fake_reply_with_variant_on_all_mains)

    results, _resolved_parent_id, resolved_author = await engagement_engine.execute_engagement_only_replies_via_full_flow(
        db=db_session,
        video_url="https://www.tiktok.com/@demo/video/1234567890123456789",
        comment_text="target comment",
        comment_id=None,
        comment_author="target_user",
        replies=["reply one"],
        task_id=201,
    )

    assert calls == [
        {"account_id": 281, "require_author": True, "allow_fallback": False},
        {"account_id": 282, "require_author": True, "allow_fallback": True},
    ]
    assert resolved_author == "found_user"
    assert results[0]["success"] is True
    assert results[0]["author_fallback_used"] is True
    assert results[0]["requested_parent_author"] == "target_user"
    assert results[0]["matched_parent_author"] == "found_user"


@pytest.mark.unit
def test_engagement_result_to_dict_warns_about_author_fallback():
    result = engagement_engine.EngagementResult()
    result.comment_found = True
    result.replies = [
        {
            "text": "reply one",
            "success": True,
            "author_fallback_used": True,
            "requested_parent_author": "target_user",
            "matched_parent_author": "found_user",
        }
    ]

    payload = result.to_dict(source="quick_order", mode="engagement_only")

    warning = next(
        item
        for item in payload["warnings"]
        if item.get("code") == "engagement_parent_author_fallback"
    )
    assert warning["requested_author"] == "target_user"
    assert warning["matched_author"] == "found_user"


@pytest.mark.unit
@pytest.mark.asyncio
async def test_engagement_only_full_flow_stops_retries_after_stop_signal(db_session, monkeypatch):
    import executor.user_schema as user_schema

    account = Account(id=301, label="reply-dom", status="active", cookies='{"sessionid":"x"}')
    selected_accounts = []
    attempts = []

    def _fake_get_free_account_for_reply(*args, **kwargs):
        selected_accounts.append(account.id)
        return account

    async def _fake_reply_with_variant_on_all_mains(
        _db,
        reply_account,
        _video_url,
        _pending_parent,
        _reply_text,
        *,
        variant_index,
        **_kwargs,
    ):
        attempts.append((reply_account.id, variant_index))
        return user_schema.ReplyAttemptResult(replies=[], searched=True)

    monkeypatch.setattr(engagement_engine, "get_free_account_for_reply", _fake_get_free_account_for_reply)
    monkeypatch.setattr(user_schema, "_reply_with_variant_on_all_mains", _fake_reply_with_variant_on_all_mains)

    results, resolved_parent_id, resolved_author = await engagement_engine.execute_engagement_only_replies_via_full_flow(
        db=db_session,
        video_url="https://www.tiktok.com/@demo/video/1234567890123456789",
        comment_text="target comment",
        comment_id="parent_123",
        comment_author="main_author",
        replies=["reply one"],
        task_id=200,
        stop_requested=lambda: bool(attempts),
    )

    assert selected_accounts == [301]
    assert attempts == [(301, 1)]
    assert resolved_parent_id == "parent_123"
    assert resolved_author == "main_author"
    assert len(results) == 1
    assert results[0]["error"] == "stopped_by_user"
    assert results[0]["account_id"] == 301
    assert results[0]["attempts"] == 1


@pytest.mark.unit
@pytest.mark.asyncio
async def test_execute_engagement_only_skips_likes_when_replies_do_not_confirm_parent(db_session, monkeypatch):
    # No up-front author is provided, so likes-first cannot run and the flow
    # falls back to ordering likes AFTER replies. In that fallback path the
    # parent-confirmation safety gate still applies: if replies do not confirm
    # the parent, likes are skipped.
    task = SimpleNamespace(reply1="reply one", reply2=None, reply3=None, settings={"reply_count": 1})
    boost_calls = []

    async def _fake_replies_via_full_flow(**_kwargs):
        return [
            {
                "text": "reply one",
                "success": False,
                "method": None,
                "error": "engagement_reply_parent_not_found_or_comment_list_not_open",
                "parent_confirmed": False,
                "parent_confirmation_level": "unconfirmed",
                "visibility_status": "not_checked",
            }
        ], "parent_123", "main_author"

    async def _fake_order_boost_likes(**kwargs):
        boost_calls.append(kwargs)
        return "boost_should_not_run", None

    monkeypatch.setattr(engagement_engine, "_get_task_boost_config", lambda *args, **kwargs: (True, 100))
    monkeypatch.setattr(engagement_engine, "execute_engagement_only_replies_via_full_flow", _fake_replies_via_full_flow)
    monkeypatch.setattr(engagement_engine, "_order_boost_likes", _fake_order_boost_likes)

    result = await engagement_engine.execute_engagement_only(
        db_session,
        task,
        "https://www.tiktok.com/@demo/video/1234567890123456789",
        comment_id="parent_123",
        username=None,
        comment_text="target comment",
        task_id=189,
    )

    assert boost_calls == []
    assert result.boost_order_id is None
    assert result.boost_enabled is False
    assert any(w.get("code") == "likes_skipped_parent_not_confirmed" for w in result.warnings)


@pytest.mark.unit
@pytest.mark.asyncio
async def test_execute_engagement_only_orders_likes_before_replies_when_target_known(db_session, monkeypatch):
    # Likes-first: when an explicit target author is known up front for a
    # replies-only order, likes are ordered BEFORE the reply phase so the
    # target comment ranks higher and reply accounts find it faster.
    task = SimpleNamespace(reply1="reply one", reply2=None, reply3=None, settings={"reply_count": 1})
    call_order = []

    async def _fake_replies_via_full_flow(**_kwargs):
        call_order.append("replies")
        return [
            {
                "text": "reply one",
                "success": True,
                "method": "hybrid_dom",
                "error": None,
                "parent_confirmed": True,
                "parent_confirmation_level": "ui_text_exact",
                "visibility_status": "not_checked",
            }
        ], "parent_123", "resolved_author"

    async def _fake_order_boost_likes(**kwargs):
        call_order.append("likes")
        return "boost_order_99", None

    monkeypatch.setattr(engagement_engine, "_get_task_boost_config", lambda *args, **kwargs: (True, 100))
    monkeypatch.setattr(engagement_engine, "execute_engagement_only_replies_via_full_flow", _fake_replies_via_full_flow)
    monkeypatch.setattr(engagement_engine, "_order_boost_likes", _fake_order_boost_likes)

    result = await engagement_engine.execute_engagement_only(
        db_session,
        task,
        "https://www.tiktok.com/@demo/video/1234567890123456789",
        comment_id="parent_123",
        username="main_author",
        comment_text="target comment",
        task_id=192,
    )

    # Likes must be ordered before replies, exactly once, targeting the
    # up-front author (not the DOM-resolved author).
    assert call_order == ["likes", "replies"]
    assert result.boost_order_id == "boost_order_99"
    assert result.boost_enabled is True
    assert result.boost_orders == [
        {
            "order_id": "boost_order_99",
            "username": "main_author",
            "quantity": 100,
            "ok": True,
            "error": None,
            "reply_count": 0,
            "parent_key": "parent_123",
            "parent_text": "target comment",
            "selection_reason": "engagement_parent",
        }
    ]


@pytest.mark.unit
@pytest.mark.asyncio
async def test_execute_engagement_only_orders_likes_when_reply_confirms_parent(db_session, monkeypatch):
    task = SimpleNamespace(reply1="reply one", reply2=None, reply3=None, settings={"reply_count": 1})
    boost_calls = []

    async def _fake_replies_via_full_flow(**_kwargs):
        return [
            {
                "text": "reply one",
                "success": False,
                "method": "hybrid_dom",
                "error": "captcha_challenge:slider",
                "parent_confirmed": True,
                "parent_confirmation_level": "ui_text_exact",
                "visibility_status": "not_checked",
            }
        ], "parent_123", "main_author"

    async def _fake_order_boost_likes(**kwargs):
        boost_calls.append(kwargs)
        return "boost_order_42", None

    monkeypatch.setattr(engagement_engine, "_get_task_boost_config", lambda *args, **kwargs: (True, 100))
    monkeypatch.setattr(engagement_engine, "execute_engagement_only_replies_via_full_flow", _fake_replies_via_full_flow)
    monkeypatch.setattr(engagement_engine, "_order_boost_likes", _fake_order_boost_likes)

    result = await engagement_engine.execute_engagement_only(
        db_session,
        task,
        "https://www.tiktok.com/@demo/video/1234567890123456789",
        comment_id="parent_123",
        username="main_author",
        comment_text="target comment",
        task_id=190,
    )

    assert len(boost_calls) == 1
    assert boost_calls[0]["comment_author_username"] == "main_author"
    assert result.boost_order_id == "boost_order_42"
    assert result.boost_orders == [
        {
            "order_id": "boost_order_42",
            "username": "main_author",
            "quantity": 100,
            "ok": True,
            "error": None,
            "reply_count": 0,
            "parent_key": "parent_123",
            "parent_text": "target comment",
            "selection_reason": "engagement_parent",
        }
    ]
    likes_payload = result.to_dict(mode="engagement_only")["engagement"]["likes"]
    assert likes_payload["orders"][0]["username"] == "main_author"
    assert likes_payload["target_mode"] == engagement_engine.BOOST_LIKES_TARGET_SINGLE_TOP
    assert result.boost_enabled is True


@pytest.mark.unit
@pytest.mark.asyncio
async def test_execute_engagement_only_skips_likes_without_replies(db_session, monkeypatch):
    task = SimpleNamespace(reply1=None, reply2=None, reply3=None, settings={"reply_count": 0})
    boost_calls = []

    async def _unexpected_replies_via_full_flow(**_kwargs):
        raise AssertionError("likes-only engagement orders must not run reply flow")

    async def _fake_order_boost_likes(**kwargs):
        boost_calls.append(kwargs)
        return "boost_order_43", None

    monkeypatch.setattr(engagement_engine, "_get_task_boost_config", lambda *args, **kwargs: (True, 100))
    monkeypatch.setattr(engagement_engine, "execute_engagement_only_replies_via_full_flow", _unexpected_replies_via_full_flow)
    monkeypatch.setattr(engagement_engine, "_order_boost_likes", _fake_order_boost_likes)

    result = await engagement_engine.execute_engagement_only(
        db_session,
        task,
        "https://www.tiktok.com/@demo/video/1234567890123456789",
        comment_id="parent_123",
        username="main_author",
        comment_text="target comment",
        task_id=191,
    )

    assert boost_calls == []
    assert result.boost_order_id is None
    assert result.boost_enabled is False


@pytest.mark.unit
@pytest.mark.asyncio
async def test_execute_hybrid_full_releases_main_account_reservation_on_failure(db_session, monkeypatch):
    account = Account(id=25, label="main", status="active", cookies='{"sessionid":"x"}', proxy="http://proxy-main")
    task = SimpleNamespace(reply1=None, reply2=None, reply3=None, settings={})
    released = []

    def _fake_get_free_account_for_main_comment(*args, **kwargs):
        exclude_ids = set(kwargs.get("exclude_ids") or [])
        return None if account.id in exclude_ids else account

    async def _fake_attempt_main_comment_on_account(*args, **kwargs):
        return None, "publish_failed", "publish"

    monkeypatch.setattr(engagement_engine, "resolve_video_url", lambda url: _async_tuple(url, "1234567890123456789"))
    monkeypatch.setattr(engagement_engine, "get_free_account_for_main_comment", _fake_get_free_account_for_main_comment)
    monkeypatch.setattr(engagement_engine, "attempt_main_comment_on_account", _fake_attempt_main_comment_on_account)
    monkeypatch.setattr(engagement_engine, "release_main_account_reservation", lambda account_id: released.append(account_id))

    result = await engagement_engine.execute_hybrid_full(
        db_session,
        task,
        "https://www.tiktok.com/@demo/video/1234567890123456789",
        "hello world",
        181,
    )

    assert result.main_comment["success"] is False
    assert result.main_comment["error"] == "publish_failed"
    assert released == [account.id]


def _patch_main_publish_path(monkeypatch, account, *, verify_outcome):
    """Wire up the common stubs needed to run execute_hybrid_full as a unit test.

    `verify_outcome` controls what `verify_comment_dom` returns. Use a single
    tuple to mean "always return that"; the tests assert on the number of
    calls separately so we don't need a sequence here.
    """
    async def _fake_sleep(_seconds):
        return None

    async def _fake_order_boost_likes(**kwargs):
        return "boost_order_42", None

    async def _fake_create_page_for_account(_account):
        return _DummyContext(), _DummyPage()

    async def _fake_warmup_page_for_http(*args, **kwargs):
        return {"sessionid": "x"}, True, {"reason_code": "ok"}

    async def _fake_from_page(*args, **kwargs):
        return _DummyHttpClient()

    verify_calls = {"count": 0}

    async def _fake_verify_comment_dom(*args, **kwargs):
        verify_calls["count"] += 1
        return verify_outcome

    monkeypatch.setattr(engagement_engine, "get_free_account_for_main_comment", lambda *args, **kwargs: account)
    monkeypatch.setattr(engagement_engine, "get_http_execution_strategy", lambda *args, **kwargs: {"strategy": "http_first"})
    monkeypatch.setattr(engagement_engine, "_should_skip_http_for_account", lambda *args, **kwargs: None)
    monkeypatch.setattr(engagement_engine, "_get_task_boost_config", lambda *args, **kwargs: (True, 100))
    monkeypatch.setattr(engagement_engine, "create_page_for_account", _fake_create_page_for_account)
    monkeypatch.setattr(engagement_engine, "warmup_page_for_http", _fake_warmup_page_for_http)
    monkeypatch.setattr(engagement_engine.PlaywrightHttpClient, "from_page", _fake_from_page)
    monkeypatch.setattr(engagement_engine, "verify_comment_dom", _fake_verify_comment_dom)
    monkeypatch.setattr(engagement_engine, "_register_runtime_success", lambda *args, **kwargs: None)
    monkeypatch.setattr(engagement_engine, "_order_boost_likes", _fake_order_boost_likes)
    monkeypatch.setattr(engagement_engine, "log", lambda *args, **kwargs: None)
    monkeypatch.setattr(engagement_engine.asyncio, "sleep", _fake_sleep)
    return verify_calls


@pytest.mark.unit
def test_dom_timeouts_do_not_mark_proxy_bad():
    assert engagement_engine._looks_like_proxy_runtime_issue(
        None,
        "input_not_found_after_captcha_clear: timeout",
    ) is False
    assert engagement_engine._looks_like_proxy_runtime_issue(
        None,
        "no_network_request_after_post_click",
    ) is False
    assert engagement_engine._looks_like_proxy_runtime_issue(
        engagement_engine.HttpErrorType.TIMEOUT,
        "request timed out",
    ) is True
    assert engagement_engine._looks_like_proxy_runtime_issue(
        None,
        "net::ERR_HTTP_RESPONSE_CODE_FAILURE",
    ) is True


@pytest.mark.unit
def test_parent_not_found_detection_handles_localized_status_messages():
    assert engagement_engine._is_parent_not_found_error("comment doesn't exist") is True
    assert engagement_engine._is_parent_not_found_error("Комментарий не существует") is True
    assert engagement_engine._is_parent_not_found_error("评论不存在") is True
    assert engagement_engine._is_parent_not_found_error("コメントが削除されました") is True
    assert engagement_engine._is_parent_not_found_error("댓글을 찾을 수 없습니다") is True
    assert engagement_engine._is_parent_not_found_error("captcha challenge required") is False
    assert engagement_engine._is_parent_not_found_error("network timeout") is False


@pytest.mark.unit
@pytest.mark.asyncio
async def test_execute_hybrid_full_marks_verified_when_dom_confirms(db_session, monkeypatch):
    """Happy path: HTTP publishes a main comment AND a viewer account observes
    it via DOM. The whole task is marked verified=True / success=True with no
    submitted_unconfirmed flag."""
    account = Account(id=10, label="main", status="active", cookies='{"sessionid":"x"}', proxy="http://proxy-main")
    task = SimpleNamespace(reply1="reply one", reply2=None, reply3=None, settings={"reply_count": 1})

    verify_calls = _patch_main_publish_path(
        monkeypatch,
        account,
        verify_outcome=(True, "cid_main_123", "tester", "standard"),
    )

    async def _fake_execute_replies_http(*args, **kwargs):
        return [
            {
                "text": "reply one",
                "success": True,
                "method": "http",
                "error": None,
                "reply_id": "reply_123",
                "visibility_status": "confirmed",
            }
        ]

    monkeypatch.setattr(engagement_engine, "execute_replies_http", _fake_execute_replies_http)

    result = await engagement_engine.execute_hybrid_full(
        db_session,
        task,
        "https://www.tiktok.com/@user/video/1234567890123456789",
        "hello world",
        99,
    )

    # New flow uses ONE verify_comment_dom call (with retry_schedule inside);
    # we replaced the old 3-stage ladder so the count must be 1, not >=2.
    assert verify_calls["count"] == 1
    assert result.main_comment["verified"] is True
    assert result.main_comment["success"] is True
    assert result.main_comment.get("submitted_unconfirmed") is False
    assert result.main_comment["comment_id"] == "cid_main_123"
    assert result.comment_author == "tester"
    assert result.main_comment["verify_mode"] == "standard"
    # Outcome wiring fix (was a pre-existing bug): the publish step must
    # set posted=True and network_confirmed=True so EngagementResult.to_dict()
    # classifies the URL as success rather than fail. Without this the
    # executor counted successful publishes as failures and refunded users.
    assert result.main_comment["posted"] is True
    assert result.main_comment["network_confirmed"] is True
    assert result.main_comment["method"] == "http"
    assert result.main_comment["text_choice"] == "hello world"

    # End-to-end: to_dict must agree this is a success.
    payload = result.to_dict(source="manual", mode="full")
    assert payload["status"] == "success"


@pytest.mark.unit
def test_get_task_boost_config_global_off_overrides_task_enabled(db_session, monkeypatch):
    monkeypatch.setattr(engagement_engine.settings, "BOOST_PROJECT_API_KEY", "key", raising=False)
    monkeypatch.setattr(engagement_engine.settings, "BOOST_PROJECT_LIKES_QUANTITY", 100, raising=False)

    def _fake_get_setting(_db, key, default=None):
        if key == engagement_engine.BOOST_LIKES_ENABLED_KEY:
            return False
        if key == engagement_engine.BOOST_LIKES_QUANTITY_KEY:
            return 100
        return default

    monkeypatch.setattr(engagement_engine, "get_setting", _fake_get_setting)
    task = SimpleNamespace(settings={"boost_likes_enabled": True, "likes_count": 250}, likes=250)

    enabled, quantity = engagement_engine._get_task_boost_config(db_session, task)

    assert enabled is False
    assert quantity == 250


@pytest.mark.unit
@pytest.mark.asyncio
async def test_execute_hybrid_full_skips_likes_when_all_replies_fail(db_session, monkeypatch):
    account = Account(id=12, label="main", status="active", cookies='{"sessionid":"x"}', proxy="http://proxy-main")
    task = SimpleNamespace(reply1="reply one", reply2=None, reply3=None, settings={"reply_count": 1})
    boost_calls = {"count": 0}

    _patch_main_publish_path(
        monkeypatch,
        account,
        verify_outcome=(True, "cid_main_123", "tester", "standard"),
    )

    async def _fake_execute_replies_http(*args, **kwargs):
        return [
            {
                "text": "reply one",
                "success": False,
                "method": "http",
                "error": "publish_failed",
            }
        ]

    async def _fake_order_boost_likes(**kwargs):
        boost_calls["count"] += 1
        return "boost_should_not_run", None

    monkeypatch.setattr(engagement_engine, "execute_replies_http", _fake_execute_replies_http)
    monkeypatch.setattr(engagement_engine, "_order_boost_likes", _fake_order_boost_likes)

    result = await engagement_engine.execute_hybrid_full(
        db_session,
        task,
        "https://www.tiktok.com/@user/video/1234567890123456789",
        "hello world",
        101,
    )

    assert boost_calls["count"] == 0
    assert result.boost_order_id is None
    assert result.boost_enabled is False


@pytest.mark.unit
@pytest.mark.asyncio
async def test_execute_hybrid_full_optimistic_continue_when_verify_fails(db_session, monkeypatch):
    """When the publisher returns a comment_id but DOM viewers can't confirm
    (TikTok lazy render / shadowbanned viewer), we MUST NOT abort — we mark
    submitted_unconfirmed=True and let downstream replies + boost run.

    This is the core P1 invariant. The previous behaviour was to exit the
    whole task at this point, losing the boost order and any reply work even
    though the main comment was actually published."""
    account = Account(id=11, label="main", status="active", cookies='{"sessionid":"x"}', proxy="http://proxy-main")
    task = SimpleNamespace(reply1="reply one", reply2=None, reply3=None, settings={"reply_count": 1})

    verify_calls = _patch_main_publish_path(
        monkeypatch,
        account,
        verify_outcome=(False, None, None, "standard"),
    )

    async def _fake_execute_replies_http(*args, **kwargs):
        return [
            {
                "text": "reply one",
                "success": True,
                "method": "http",
                "error": None,
                "reply_id": "reply_123",
                "visibility_status": "confirmed",
            }
        ]

    monkeypatch.setattr(engagement_engine, "execute_replies_http", _fake_execute_replies_http)

    result = await engagement_engine.execute_hybrid_full(
        db_session,
        task,
        "https://www.tiktok.com/@user/video/1234567890123456789",
        "hello world",
        100,
    )

    assert verify_calls["count"] == 1
    assert result.main_comment["success"] is True
    assert result.main_comment["verified"] is False
    assert result.main_comment.get("submitted_unconfirmed") is True
    assert result.main_comment["comment_id"] == "cid_main_123"
    # Outcome wiring: HTTP path always sets network_confirmed=True; without
    # this, to_dict() classifies the optimistic continue as plain "fail"
    # instead of "submitted_unconfirmed".
    assert result.main_comment["posted"] is True
    assert result.main_comment["network_confirmed"] is True
    # Likes must wait for a publicly verified main comment. A comment_id alone
    # can still be moderation-delayed or hidden, so we do not order likes here.
    assert result.boost_order_id is None
    assert result.boost_enabled is False

    # End-to-end: to_dict must report submitted_unconfirmed (NOT fail).
    payload = result.to_dict(source="manual", mode="full")
    assert payload["status"] == "submitted_unconfirmed"
    assert any(w.get("code") == "main_submitted_unconfirmed" for w in payload.get("warnings") or [])


@pytest.mark.unit
@pytest.mark.asyncio
async def test_execute_hybrid_full_aborts_when_smart_check_invalidates_parent(db_session, monkeypatch):
    """Smart-check via the first reply is the single source of truth when DOM
    verify is inconclusive: if the HTTP reply API explicitly says the parent
    comment is gone, we treat the whole task as a failure and skip boost.

    The fake execute_replies_http stub mimics the parent_not_found marker that
    real execute_replies_http sets when the first reply fails with that signal.
    """
    account = Account(id=12, label="main", status="active", cookies='{"sessionid":"x"}', proxy="http://proxy-main")
    task = SimpleNamespace(reply1="fast reply", reply2=None, reply3=None, settings={})

    _patch_main_publish_path(
        monkeypatch,
        account,
        verify_outcome=(False, None, None, "standard"),
    )

    boost_calls = {"count": 0}

    async def _fake_order_boost_likes(**kwargs):
        boost_calls["count"] += 1
        return "boost_should_not_run", None

    async def _fake_execute_replies_http(*args, **kwargs):
        # parent_unconfirmed must be propagated by execute_hybrid_full when
        # DOM verification failed.
        assert kwargs.get("parent_unconfirmed") is True
        return [
            {
                "text": "fast reply",
                "success": False,
                "method": None,
                "error": "Comment doesn't exist",
                "reply_id": None,
                "http_error_type": "parent_not_found",
                "parent_confirmed": False,
                "parent_confirmation_level": "unconfirmed",
                "visibility_status": "failed",
                "local_visible": None,
                "verify_mode": None,
                "attempts": 1,
                "fallback_used": False,
                "warmup_reason": None,
            },
        ]

    monkeypatch.setattr(engagement_engine, "execute_replies_http", _fake_execute_replies_http)
    monkeypatch.setattr(engagement_engine, "_order_boost_likes", _fake_order_boost_likes)

    result = await engagement_engine.execute_hybrid_full(
        db_session,
        task,
        "https://www.tiktok.com/@user/video/1234567890123456789",
        "hello world",
        101,
    )

    assert result.main_comment["success"] is False
    assert result.main_comment["verified"] is False
    assert result.main_comment["error"] == "main_invalidated_by_smart_check"
    assert result.comment_found is False
    # boost MUST NOT run when smart-check invalidates the parent
    assert boost_calls["count"] == 0
    assert result.boost_order_id is None
    payload = result.to_dict(source="manual", mode="full")
    assert payload["status"] == "fail"
    assert payload["error"] == "main_invalidated_by_smart_check"
    assert not any(w.get("code") == "main_submitted_unconfirmed" for w in payload.get("warnings") or [])


@pytest.mark.unit
@pytest.mark.asyncio
async def test_execute_replies_http_bypasses_http_for_dom_first_strategy(db_session, monkeypatch):
    http_candidate = Account(id=21, label="reply-http", status="active", cookies='{"sessionid":"weak"}')
    fallback_account = Account(id=22, label="reply-dom", status="active", cookies='{"sessionid":"dom"}')

    accounts = [http_candidate, fallback_account]

    def _fake_get_free_account_for_reply(*args, **kwargs):
        exclude_ids = set(kwargs.get("exclude_ids") or [])
        for account in accounts:
            if account.id not in exclude_ids:
                return account
        return None

    async def _unexpected_from_page(*args, **kwargs):
        raise AssertionError("HTTP client should not be created for dom_first strategy")

    async def _fake_post_reply(**kwargs):
        return SimpleNamespace(
            success=True,
            comment_id="reply_dom_123",
            parent_confirmation_level="network_exact",
            local_visible=True,
            visibility_status="confirmed_local",
            visibility_checked=True,
            details=None,
        )

    def _fake_strategy(account, scope):
        if account.id == http_candidate.id:
            return {"strategy": "dom_first", "session_bucket": "dom_only", "state_bucket": "healthy"}
        return {"strategy": "http_first", "session_bucket": "http_workable", "state_bucket": "healthy"}

    monkeypatch.setattr(engagement_engine, "get_free_account_for_reply", _fake_get_free_account_for_reply)
    monkeypatch.setattr(engagement_engine, "get_http_execution_strategy", _fake_strategy)
    monkeypatch.setattr(engagement_engine.PlaywrightHttpClient, "from_page", _unexpected_from_page)
    monkeypatch.setattr(engagement_engine, "post_reply", _fake_post_reply, raising=False)
    monkeypatch.setattr("executor.tiktok_client_async.post_reply", _fake_post_reply)
    monkeypatch.setattr(engagement_engine, "_register_runtime_success", lambda *args, **kwargs: None)
    monkeypatch.setattr(engagement_engine, "_register_runtime_failure", lambda *args, **kwargs: None)
    monkeypatch.setattr(engagement_engine, "log", lambda *args, **kwargs: None)

    results = await engagement_engine.execute_replies_http(
        db_session,
        "https://www.tiktok.com/@user/video/1234567890123456789",
        "parent_123",
        ["reply text"],
        task_id=7,
        main_comment_text="main comment",
    )

    assert len(results) == 1
    assert results[0]["success"] is True
    assert results[0]["method"] == "dom_fallback"
    assert results[0]["reply_id"] == "reply_dom_123"
    assert results[0]["attempts"] == 1
    assert results[0]["fallback_used"] is True


@pytest.mark.unit
@pytest.mark.asyncio
async def test_execute_replies_http_uses_dom_parent_when_parent_id_missing(db_session, monkeypatch):
    account = Account(id=23, label="reply-dom", status="active", cookies='{"sessionid":"dom"}')
    captured = {}

    async def _unexpected_from_page(*args, **kwargs):
        raise AssertionError("HTTP client should not be created when parent id is missing")

    async def _fake_post_reply(**kwargs):
        captured.update(kwargs)
        return SimpleNamespace(
            success=True,
            comment_id="reply_dom_text_parent_123",
            parent_confirmation_level="not_applicable",
            local_visible=True,
            visibility_status="confirmed_local",
            details=None,
        )

    monkeypatch.setattr(engagement_engine, "get_free_account_for_reply", lambda *args, **kwargs: account)
    monkeypatch.setattr(engagement_engine, "get_http_execution_strategy", lambda *args, **kwargs: {"strategy": "http_first"})
    monkeypatch.setattr(engagement_engine.PlaywrightHttpClient, "from_page", _unexpected_from_page)
    monkeypatch.setattr(engagement_engine, "post_reply", _fake_post_reply, raising=False)
    monkeypatch.setattr("executor.tiktok_client_async.post_reply", _fake_post_reply)
    monkeypatch.setattr(engagement_engine, "_register_runtime_success", lambda *args, **kwargs: None)
    monkeypatch.setattr(engagement_engine, "_register_runtime_failure", lambda *args, **kwargs: None)
    monkeypatch.setattr(engagement_engine, "log", lambda *args, **kwargs: None)

    results = await engagement_engine.execute_replies_http(
        db_session,
        "https://www.tiktok.com/@user/video/1234567890123456789",
        "",
        ["reply text"],
        task_id=17,
        main_comment_text="main comment",
        parent_author="main",
    )

    assert len(results) == 1
    assert captured["main_comment_text"] == "main comment"
    assert captured["expected_author"] == "main"
    assert captured["expected_comment_id"] is None
    assert captured["parent_comment_id"] is None
    assert results[0]["success"] is True
    assert results[0]["method"] == "dom_text_parent"
    assert results[0]["reply_id"] == "reply_dom_text_parent_123"
    assert results[0]["parent_confirmation_level"] == "ui_author_only"
    assert results[0]["parent_confirmed"] is True
    assert results[0]["verify_mode"] == "dom_text_author_parent"
    assert results[0]["attempts"] == 1


@pytest.mark.unit
@pytest.mark.asyncio
async def test_execute_replies_http_uses_http_for_http_first_strategy(db_session, monkeypatch):
    account = Account(id=31, label="reply-http", status="active", cookies='{"sessionid":"x"}', proxy="http://proxy-reply")
    http_calls = []

    class _ReplyHttpClient:
        async def reply_comment(self, **kwargs):
            http_calls.append(kwargs)
            return True, "reply_http_123", None, None, {
                "parent_confirmed": True,
                "parent_confirmation_level": "network_exact",
            }

    async def _fake_create_page_for_account(_account):
        return _DummyContext(), _DummyPage()

    async def _fake_warmup_page_for_http(*args, **kwargs):
        return {"sessionid": "x", "msToken": "token"}, True, {"reason_code": "ready"}

    async def _fake_from_page(*args, **kwargs):
        return _ReplyHttpClient()

    async def _unexpected_publish_reply_hybrid(*args, **kwargs):
        raise AssertionError("DOM reply publish should not run for http_first strategy")

    monkeypatch.setattr(engagement_engine, "get_free_account_for_reply", lambda *args, **kwargs: account)
    monkeypatch.setattr(engagement_engine, "get_http_execution_strategy", lambda *args, **kwargs: {"strategy": "http_first"})
    monkeypatch.setattr(engagement_engine, "_should_skip_http_for_account", lambda *args, **kwargs: None)
    monkeypatch.setattr(engagement_engine, "create_page_for_account", _fake_create_page_for_account)
    monkeypatch.setattr(engagement_engine, "warmup_page_for_http", _fake_warmup_page_for_http)
    monkeypatch.setattr(engagement_engine.PlaywrightHttpClient, "from_page", _fake_from_page)
    monkeypatch.setattr(engagement_engine, "publish_reply_hybrid", _unexpected_publish_reply_hybrid)
    monkeypatch.setattr(engagement_engine, "_register_runtime_success", lambda *args, **kwargs: None)
    monkeypatch.setattr(engagement_engine, "_register_runtime_failure", lambda *args, **kwargs: None)
    monkeypatch.setattr(engagement_engine, "log", lambda *args, **kwargs: None)

    results = await engagement_engine.execute_replies_http(
        db_session,
        "https://www.tiktok.com/@user/video/1234567890123456789",
        "parent_123",
        ["reply text"],
        task_id=8,
        main_comment_text="main comment",
    )

    assert len(results) == 1
    assert results[0]["success"] is True
    assert results[0]["method"] == "http"
    assert results[0]["reply_id"] == "reply_http_123"
    assert results[0]["parent_confirmed"] is True
    assert results[0]["parent_confirmation_level"] == "network_exact"
    assert results[0]["attempts"] == 1
    assert results[0]["fallback_used"] is False
    assert http_calls[0]["parent_comment_id"] == "parent_123"
    assert http_calls[0]["text"] == "reply text"


@pytest.mark.unit
@pytest.mark.asyncio
async def test_execute_replies_http_preserves_unconfirmed_parent_meta(db_session, monkeypatch):
    account = Account(id=32, label="reply-http", status="active", cookies='{"sessionid":"x"}', proxy="http://proxy-reply")

    class _ReplyHttpClient:
        async def reply_comment(self, **kwargs):
            return True, "reply_http_unconfirmed", None, None, {
                "parent_confirmed": False,
                "parent_confirmation_level": "unconfirmed",
                "response_parent_id": None,
                "response_reply_to_reply_id": None,
                "response_root_comment_id": None,
            }

    async def _fake_create_page_for_account(_account):
        return _DummyContext(), _DummyPage()

    async def _fake_warmup_page_for_http(*args, **kwargs):
        return {"sessionid": "x", "msToken": "token"}, True, {"reason_code": "ready"}

    async def _fake_from_page(*args, **kwargs):
        return _ReplyHttpClient()

    monkeypatch.setattr(engagement_engine, "get_free_account_for_reply", lambda *args, **kwargs: account)
    monkeypatch.setattr(engagement_engine, "get_http_execution_strategy", lambda *args, **kwargs: {"strategy": "http_first"})
    monkeypatch.setattr(engagement_engine, "_should_skip_http_for_account", lambda *args, **kwargs: None)
    monkeypatch.setattr(engagement_engine, "create_page_for_account", _fake_create_page_for_account)
    monkeypatch.setattr(engagement_engine, "warmup_page_for_http", _fake_warmup_page_for_http)
    monkeypatch.setattr(engagement_engine.PlaywrightHttpClient, "from_page", _fake_from_page)
    monkeypatch.setattr(engagement_engine, "_register_runtime_success", lambda *args, **kwargs: None)
    monkeypatch.setattr(engagement_engine, "_register_runtime_failure", lambda *args, **kwargs: None)
    monkeypatch.setattr(engagement_engine, "log", lambda *args, **kwargs: None)

    results = await engagement_engine.execute_replies_http(
        db_session,
        "https://www.tiktok.com/@user/video/1234567890123456789",
        "parent_123",
        ["reply text"],
        task_id=9,
        main_comment_text="main comment",
        parent_unconfirmed=True,
    )

    assert len(results) == 1
    assert results[0]["success"] is False
    assert results[0]["submitted_unconfirmed"] is True
    assert results[0]["method"] == "http"
    assert results[0]["reply_id"] == "reply_http_unconfirmed"
    assert results[0]["parent_confirmed"] is False
    assert results[0]["parent_confirmation_level"] == "unconfirmed"
    assert results[0]["visibility_status"] == "submitted_unconfirmed"
    assert results[0]["verify_mode"] == "http_parent_unconfirmed"


@pytest.mark.unit
@pytest.mark.asyncio
async def test_execute_replies_http_stops_after_local_reply_without_id(db_session, monkeypatch):
    account = Account(id=33, label="reply-dom", status="active", cookies='{"sessionid":"x"}', proxy="http://proxy-reply")
    attempts = []

    async def _fake_attempt_reply_on_account(*args, **kwargs):
        attempts.append(kwargs.get("account").id)
        outcome = kwargs.get("outcome_dict")
        outcome.update(
            {
                "posted": True,
                "local_confirmed": True,
                "local_confirmation_without_id": True,
                "confirmation_source": "local_dom_reply_text_match_no_id",
                "parent_confirmed": True,
                "parent_confirmation_level": "ui_selected_parent",
                "local_recovery": {"found": True, "comment_id": None},
            }
        )
        return None, None, "reply_local_no_id"

    monkeypatch.setattr(engagement_engine, "get_free_account_for_reply", lambda *args, **kwargs: account)
    monkeypatch.setattr(engagement_engine, "get_http_execution_strategy", lambda *args, **kwargs: {"strategy": "http_first"})
    monkeypatch.setattr(engagement_engine, "attempt_reply_on_account", _fake_attempt_reply_on_account)
    monkeypatch.setattr(engagement_engine, "_register_runtime_success", lambda *args, **kwargs: None)
    monkeypatch.setattr(engagement_engine, "_register_runtime_failure", lambda *args, **kwargs: None)
    monkeypatch.setattr(engagement_engine, "log", lambda *args, **kwargs: None)

    results = await engagement_engine.execute_replies_http(
        db_session,
        "https://www.tiktok.com/@user/video/1234567890123456789",
        "parent_123",
        ["reply text"],
        task_id=10,
        main_comment_text="main comment",
    )

    assert attempts == [33]
    assert len(results) == 1
    assert results[0]["success"] is False
    assert results[0]["submitted_unconfirmed"] is True
    assert results[0]["reply_id"] is None
    assert results[0]["visibility_status"] == "submitted_unconfirmed"
    assert results[0]["verify_mode"] == "local_dom_text_match_no_id"
    assert results[0]["error"] == "reply_submitted_local_dom_text_match_no_id"


@pytest.mark.unit
@pytest.mark.asyncio
async def test_warmup_page_for_http_uses_resolved_username_video_url(monkeypatch):
    page = _WarmupPage(
        [
            {"name": "sessionid", "value": "session"},
            {"name": "msToken", "value": "mstoken"},
            {"name": "verifyFp", "value": "verify"},
            {"name": "device_id", "value": "device"},
            {"name": "ttwid", "value": "ttwid"},
            {"name": "odin_tt", "value": "odin"},
            {"name": "tt_csrf_token", "value": "csrf"},
        ]
    )
    resolved_url = "https://www.tiktok.com/@_mjmagic_/video/7635267329407913230?_r=1&_t=ZS-96CHmDcrzAF"

    async def _fake_sleep(_seconds):
        return None

    async def _fake_wait_for_runtime_signer_ready(*args, **kwargs):
        return True

    monkeypatch.setattr(engagement_engine, "wait_for_runtime_signer_ready", _fake_wait_for_runtime_signer_ready)
    monkeypatch.setattr(engagement_engine, "log_tokens_safely", lambda *args, **kwargs: None)
    monkeypatch.setattr(engagement_engine.asyncio, "sleep", _fake_sleep)

    cookies_dict, profile_valid, warmup_info = await engagement_engine.warmup_page_for_http(
        page,
        resolved_url,
        correlation_id="warmup_resolved_url",
        max_retries=2,
    )

    assert cookies_dict["sessionid"] == "session"
    assert profile_valid is True
    assert warmup_info["reason_code"] == "ready"
    assert warmup_info["target_url"] == resolved_url
    assert page.url == resolved_url
    assert page.goto_calls == 1
    assert page.reload_calls == 0


@pytest.mark.unit
@pytest.mark.asyncio
async def test_warmup_page_for_http_fails_when_signer_never_becomes_ready(monkeypatch):
    page = _WarmupPage(
        [
            {"name": "verifyFp", "value": "verify"},
            {"name": "device_id", "value": "device"},
            {"name": "sessionid", "value": "session"},
        ]
    )

    async def _fake_sleep(_seconds):
        return None

    async def _fake_wait_for_runtime_signer_ready(*args, **kwargs):
        return False

    monkeypatch.setattr(engagement_engine, "extract_video_id", lambda _url: "1234567890123456789")
    monkeypatch.setattr(engagement_engine, "build_canonical_video_url", lambda _url, _aweme_id=None: "https://www.tiktok.com/video/1234567890123456789")
    monkeypatch.setattr(engagement_engine, "wait_for_runtime_signer_ready", _fake_wait_for_runtime_signer_ready)
    monkeypatch.setattr(engagement_engine, "log_tokens_safely", lambda *args, **kwargs: None)
    monkeypatch.setattr(engagement_engine.asyncio, "sleep", _fake_sleep)

    cookies_dict, profile_valid, warmup_info = await engagement_engine.warmup_page_for_http(
        page,
        "https://www.tiktok.com/@demo/video/1234567890123456789",
        correlation_id="warmup_signer_fail",
        max_retries=2,
    )

    assert cookies_dict["sessionid"] == "session"
    assert profile_valid is False
    assert warmup_info["reason_code"] == "signer_not_ready"
    assert warmup_info["signer_ready"] is False
    assert warmup_info["degraded_http"] is True
    assert page.reload_calls == 1


@pytest.mark.unit
@pytest.mark.asyncio
async def test_warmup_page_for_http_retries_until_signer_is_ready(monkeypatch):
    page = _WarmupPage(
        [
            {"name": "verifyFp", "value": "verify"},
            {"name": "device_id", "value": "device"},
            {"name": "sessionid", "value": "session"},
            {"name": "msToken", "value": "mstoken"},
        ]
    )
    signer_states = iter([False, True])

    async def _fake_sleep(_seconds):
        return None

    async def _fake_wait_for_runtime_signer_ready(*args, **kwargs):
        return next(signer_states)

    monkeypatch.setattr(engagement_engine, "extract_video_id", lambda _url: "1234567890123456789")
    monkeypatch.setattr(engagement_engine, "build_canonical_video_url", lambda _url, _aweme_id=None: "https://www.tiktok.com/video/1234567890123456789")
    monkeypatch.setattr(engagement_engine, "wait_for_runtime_signer_ready", _fake_wait_for_runtime_signer_ready)
    monkeypatch.setattr(engagement_engine, "log_tokens_safely", lambda *args, **kwargs: None)
    monkeypatch.setattr(engagement_engine.asyncio, "sleep", _fake_sleep)

    cookies_dict, profile_valid, warmup_info = await engagement_engine.warmup_page_for_http(
        page,
        "https://www.tiktok.com/@demo/video/1234567890123456789",
        correlation_id="warmup_signer_retry",
        max_retries=2,
    )

    assert cookies_dict["sessionid"] == "session"
    assert profile_valid is True
    assert warmup_info["reason_code"] == "ready"
    assert warmup_info["signer_ready"] is True
    assert warmup_info["attempts"] == 2
    assert page.reload_calls == 1
