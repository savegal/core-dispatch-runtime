"""Navigation and attempt-budget regressions from HTTP-first task 1219."""
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from executor import engagement_engine, user_schema
from services.execution_attempts import is_pre_publish_account_failure, pre_publish_outcomes


pytestmark = [pytest.mark.unit, pytest.mark.asyncio]
URL = "https://www.tiktok.com/@demo/video/7635267329407913230"


def cookies(*names):
    return [{"name": name, "value": name, "domain": ".tiktok.com"} for name in names]


@pytest.fixture
def warmup(monkeypatch):
    clock = [0.0]
    sleeps = []

    async def sleep(seconds):
        sleeps.append(seconds)
        clock[0] += seconds

    page = SimpleNamespace(
        url=URL, goto=AsyncMock(), reload=AsyncMock(), wait_for_load_state=AsyncMock(),
        evaluate=AsyncMock(return_value={"current_url": URL, "navigation_url": URL, "age_ms": 5000, "dom_ready_ms": 500}),
        context=SimpleNamespace(cookies=AsyncMock(return_value=cookies("sessionid", "ttwid", "tt_csrf_token", "msToken"))),
    )
    comments = AsyncMock(return_value={"comments": []})
    monkeypatch.setattr(engagement_engine.asyncio, "sleep", sleep)
    monkeypatch.setattr(engagement_engine.time, "monotonic", lambda: clock[0])
    monkeypatch.setattr(engagement_engine, "wait_for_runtime_signer_ready", AsyncMock(return_value=True))
    monkeypatch.setattr("executor.http.comment_discovery.fetch_comment_page", comments)
    return page, comments, sleeps


@pytest.mark.parametrize("path", ["video", "photo"])
@pytest.mark.parametrize("age_ms,dom_ready_ms", [(0, 0), (140, 0), (2500, 2400), (4000, 3900), (5000, 500)])
async def test_first_comment_get_and_exit_require_a_mature_target_page(warmup, path, age_ms, dom_ready_ms):
    page, comments, sleeps = warmup
    url = URL.replace("/video/", f"/{path}/")
    page.url = url
    page.evaluate.return_value = {"current_url": url, "navigation_url": url, "age_ms": age_ms, "dom_ready_ms": dom_ready_ms}
    page.context.cookies.return_value = cookies("sessionid", "ttwid", "msToken")
    dom_age = (age_ms - dom_ready_ms) / 1000 if dom_ready_ms else 0

    async def fetch(*args, **kwargs):
        assert sum(sleeps) + age_ms / 1000 >= 3.2 - 1e-6
        assert sum(sleeps) + dom_age >= 1.8 - 1e-6
        return {"comments": []}

    comments.side_effect = fetch
    _, valid, info = await engagement_engine.warmup_page_for_http(page, url, max_retries=1)
    assert valid and info["target_page_age_ms"] >= 3200
    assert info["target_dom_age_ms"] >= 1800
    assert sum(sleeps) == pytest.approx(max(0, 3.2 - age_ms / 1000, 1.8 - dom_age))
    page.wait_for_load_state.assert_awaited_once_with("domcontentloaded", timeout=5000)
    comments.assert_awaited_once()


async def test_old_document_from_another_video_cannot_bypass_minimum_age(warmup):
    page, comments, sleeps = warmup
    page.evaluate.return_value = {"current_url": URL, "navigation_url": URL.replace("7635267329407913230", "1111111111111111111"), "age_ms": 100000}
    _, valid, info = await engagement_engine.warmup_page_for_http(page, URL, max_retries=1)
    assert valid and info["minimum_age_wait_ms"] == 3200
    assert sum(sleeps) == pytest.approx(3.2)
    comments.assert_not_awaited()


async def test_redirect_during_minimum_age_wait_cannot_reach_comment_api(monkeypatch, warmup):
    page, comments, sleeps = warmup
    page.evaluate.return_value = {"current_url": URL, "navigation_url": URL, "age_ms": 0}
    page.context.cookies.return_value = cookies("sessionid", "ttwid", "msToken")
    original_sleep = engagement_engine.asyncio.sleep

    async def redirect(seconds):
        await original_sleep(seconds)
        page.url = "https://www.tiktok.com/login"

    monkeypatch.setattr(engagement_engine.asyncio, "sleep", redirect)
    _, valid, info = await engagement_engine.warmup_page_for_http(page, URL, max_retries=1)
    assert not valid and info["reason_code"] == "redirect_not_video"
    assert sum(sleeps) == pytest.approx(3.2)
    comments.assert_not_awaited()


async def test_load_state_wait_is_not_credited_as_time_after_dom_ready(warmup):
    page, _, sleeps = warmup

    async def wait_for_dom(*args, **kwargs):
        await engagement_engine.asyncio.sleep(1.6)

    page.wait_for_load_state.side_effect = wait_for_dom
    page.evaluate.return_value = {"current_url": URL, "navigation_url": URL, "age_ms": 5000, "dom_ready_ms": 5000}
    _, valid, info = await engagement_engine.warmup_page_for_http(page, URL, max_retries=1)
    assert valid and info["target_dom_age_ms"] >= 1800
    assert sleeps == pytest.approx([1.6, 1.8])


async def test_existing_ttwid_waits_for_interceptor_before_first_comment_get(monkeypatch, warmup):
    page, comments, sleeps = warmup
    page.context.cookies.return_value = cookies("sessionid", "ttwid", "msToken")
    ready = AsyncMock(side_effect=[False, False, True])
    monkeypatch.setattr(engagement_engine, "wait_for_runtime_signer_ready", ready)

    async def fetch(*args, **kwargs):
        assert ready.await_count == 3
        assert sleeps == [0.2, 0.2]
        return {"comments": []}

    comments.side_effect = fetch
    _, valid, info = await engagement_engine.warmup_page_for_http(page, URL)
    assert valid and info["signer_ready"] and info["attempts"] == 1
    comments.assert_awaited_once()
    page.goto.assert_not_awaited()
    page.reload.assert_not_awaited()


async def test_existing_cookies_do_not_skip_bounded_signer_wait(monkeypatch, warmup):
    page, comments, sleeps = warmup
    ready = AsyncMock(return_value=False)
    monkeypatch.setattr(engagement_engine, "wait_for_runtime_signer_ready", ready)
    _, valid, info = await engagement_engine.warmup_page_for_http(page, URL, max_retries=1)
    assert valid and info["signer_ready"] is False
    assert sleeps == [0.2] * 30
    assert round(sum(sleeps), 2) == 6.0
    assert ready.await_count == 30
    comments.assert_not_awaited()


async def test_signer_poll_time_is_included_in_six_second_deadline(monkeypatch, warmup):
    page, comments, _ = warmup
    clock = [0.0]

    async def sleep(seconds):
        clock[0] += seconds

    async def ready(*args, timeout_ms):
        clock[0] += timeout_ms / 1000
        return False

    signer = AsyncMock(side_effect=ready)
    monkeypatch.setattr(engagement_engine.time, "monotonic", lambda: clock[0])
    monkeypatch.setattr(engagement_engine.asyncio, "sleep", sleep)
    monkeypatch.setattr(engagement_engine, "wait_for_runtime_signer_ready", signer)
    _, valid, info = await engagement_engine.warmup_page_for_http(page, URL, max_retries=1)
    assert valid and info["signer_ready"] is False
    assert round(clock[0], 6) == 6.0
    assert signer.await_count == 15
    comments.assert_not_awaited()


@pytest.mark.parametrize("missing", ["tt_csrf_token", "msToken", "both"])
async def test_comment_get_primes_missing_tokens_without_waiting(warmup, missing):
    page, comments, sleeps = warmup
    names = ["sessionid", "ttwid"]
    if missing != "both":
        names.append("msToken" if missing == "tt_csrf_token" else "tt_csrf_token")
    page.context.cookies.return_value = cookies(*names)

    async def fetch(*args, **kwargs):
        assert not sleeps
        page.context.cookies.return_value = cookies("sessionid", "ttwid", "tt_csrf_token", "msToken")
        return {"comments": [{"cid": "1"}]}

    comments.side_effect = fetch
    result, valid, info = await engagement_engine.warmup_page_for_http(page, URL)
    assert valid and result["ttwid"] and result["tt_csrf_token"]
    assert info["attempts"] == 1 and info["comment_api_ready"]
    assert 0.2 not in sleeps
    assert comments.await_count == int(missing != "msToken")
    if missing != "msToken":
        assert getattr(page, "_cached_first_comment_page", None) is not None
    else:
        assert info.get("comment_api_skipped") is True
    page.reload.assert_not_awaited()
    page.goto.assert_not_awaited()


@pytest.mark.parametrize("missing", ["ttwid", "tt_csrf_token"])
async def test_only_missing_ttwid_requires_a_bounded_six_second_wait(warmup, missing):
    page, comments, sleeps = warmup
    page.context.cookies.return_value = cookies("sessionid", "msToken", "ttwid" if missing == "tt_csrf_token" else "tt_csrf_token")
    _, valid, info = await engagement_engine.warmup_page_for_http(page, URL, max_retries=1)
    assert sleeps.count(0.2) == (30 if missing == "ttwid" else 0)
    if missing == "ttwid":
        assert round(sum(sleeps[:30]), 2) == 6.0
    else:
        assert sum(sleeps) < 0.5
    assert valid is (missing == "tt_csrf_token")
    assert comments.await_count == int(missing == "tt_csrf_token")
    if not valid:
        assert info["reason_code"] == "missing_ttwid"
    page.reload.assert_not_awaited()


async def test_first_get_waits_for_delayed_ttwid(warmup):
    page, comments, sleeps = warmup
    reads = []
    async def read():
        reads.append(1)
        return cookies("sessionid", "msToken") if len(reads) < 5 else cookies("sessionid", "msToken", "ttwid")
    async def fetch(*args, **kwargs):
        assert len(reads) == 5 and sleeps == [0.2] * 4
        return {"comments": []}
    page.context.cookies.side_effect = read
    comments.side_effect = fetch
    _, valid, info = await engagement_engine.warmup_page_for_http(page, URL)
    assert valid and info["comment_api_ready"] and info["attempts"] == 1
    comments.assert_awaited_once()
    page.reload.assert_not_awaited()


async def test_missing_ttwid_after_priming_is_bounded_and_does_not_repeat_get(warmup):
    page, comments, sleeps = warmup
    page.context.cookies.return_value = cookies("sessionid")
    comments.return_value = None
    _, valid, info = await engagement_engine.warmup_page_for_http(page, URL, max_retries=1)
    assert not valid and info["reason_code"] == "missing_ttwid"
    assert sleeps[:30] == [0.2] * 30
    comments.assert_not_awaited()


async def test_empty_priming_with_existing_ttwid_does_not_add_wait_or_duplicate_get(warmup):
    page, comments, sleeps = warmup
    page.context.cookies.return_value = cookies("sessionid", "ttwid")
    comments.return_value = None
    _, valid, info = await engagement_engine.warmup_page_for_http(page, URL)
    assert valid and not info["comment_api_ready"]
    assert 0.2 not in sleeps
    comments.assert_awaited_once()


@pytest.mark.parametrize("phase", ["signer", "priming_get", "ttwid_poll", "cookie_sync"])
@pytest.mark.parametrize("login", ["https://www.tiktok.com/login", "file://www.tiktok.com/@demo/video/7635267329407913230"])
async def test_warmup_rejects_redirects_during_preparation(monkeypatch, warmup, phase, login):
    page, comments, _ = warmup

    async def redirect(*args, **kwargs):
        page.url = login
        return {"comments": []}

    if phase == "signer":
        monkeypatch.setattr(engagement_engine, "wait_for_runtime_signer_ready", AsyncMock(side_effect=redirect))
    elif phase == "priming_get":
        page.context.cookies.return_value = cookies("sessionid", "ttwid")
        comments.side_effect = redirect
    elif phase == "ttwid_poll":
        reads = []

        async def read():
            reads.append(1)
            if len(reads) == 1:
                return cookies("sessionid", "msToken", "tt_csrf_token")
            page.url = login
            return cookies("sessionid", "msToken", "tt_csrf_token", "ttwid")

        page.context.cookies.side_effect = read
    else:
        async def sync(*args):
            page.url = login
            return {"sessionid": "session", "ttwid": "wid"}, False

        monkeypatch.setattr(engagement_engine, "synchronize_browser_cookies", sync)

    _, valid, info = await engagement_engine.warmup_page_for_http(page, URL, max_retries=1)
    assert not valid
    assert info["reason_code"] == "redirect_not_video" and info["current_url"] == login
    if phase in {"signer", "ttwid_poll"}:
        comments.assert_not_awaited()
    page.reload.assert_not_awaited()


async def test_shortlink_warmup_keeps_the_first_resolved_video_as_its_target(warmup):
    page, comments, _ = warmup
    page.context.cookies.return_value = cookies("sessionid", "ttwid")

    async def switch_video(*args, **kwargs):
        page.url = "https://www.tiktok.com/@other/video/1111111111111111111"
        return {"comments": []}

    comments.side_effect = switch_video
    _, valid, info = await engagement_engine.warmup_page_for_http(
        page, "https://vt.tiktok.com/local-test/", max_retries=1,
    )
    assert not valid
    assert info["reason_code"] == "redirect_not_video"
    assert info["current_url"] == page.url


@pytest.mark.parametrize("goto_error", [None, "Page.goto: net::ERR_ABORTED", "Page.goto: net::ERR_CONNECTION_RESET"])
async def test_retry_waits_for_ttwid_without_page_reload_and_recovers_aborted_goto(warmup, goto_error):
    page, comments, sleeps = warmup
    page.url = "about:blank"
    nav_calls = [0]

    async def read():
        return cookies("sessionid", "ttwid", "tt_csrf_token", "msToken") if nav_calls[0] >= (2 if goto_error else 1) else cookies("sessionid")

    async def goto(url, **kwargs):
        assert url == URL
        assert kwargs == {"wait_until": "domcontentloaded", "timeout": 30000}
        nav_calls[0] += 1
        page.url = URL
        if nav_calls[0] == 1 and goto_error:
            raise RuntimeError(goto_error)

    page.context.cookies.side_effect = read
    page.goto.side_effect = goto
    _, valid, info = await engagement_engine.warmup_page_for_http(page, URL)
    assert valid is (goto_error is None or "ERR_ABORTED" in goto_error)
    assert page.goto.await_count == (2 if goto_error and "ERR_ABORTED" in goto_error else 1)
    assert comments.await_count == 0
    page.reload.assert_not_awaited()
    if valid:
        assert info["reason_code"] == "ready"
        assert info["attempts"] == 1
        assert info.get("comment_api_skipped") is True
        if goto_error and "ERR_ABORTED" in goto_error:
            assert 1.0 in sleeps
    else:
        assert info["reason_code"] == "warmup_exception"


async def test_retry_on_target_page_polls_ttwid_without_reload_or_goto(warmup):
    page, comments, sleeps = warmup
    attempt_reads = [0]

    async def read():
        attempt_reads[0] += 1
        # First attempt (31 poll reads + 1 sync read = 32) has no ttwid; second attempt gets ttwid immediately without page.reload
        return cookies("sessionid", "ttwid", "tt_csrf_token", "msToken") if attempt_reads[0] > 32 else cookies("sessionid")

    page.context.cookies.side_effect = read
    _, valid, info = await engagement_engine.warmup_page_for_http(page, URL, max_retries=2)
    assert valid is True
    assert info["reason_code"] == "ready" and info["attempts"] == 2
    assert info.get("comment_api_skipped") is True
    page.reload.assert_not_awaited()
    page.goto.assert_not_awaited()
    comments.assert_not_awaited()


@pytest.mark.parametrize("current_url", ["", "about:blank", "https://www.tiktok.com/", "https://www.tiktok.com/login", "https://example.com/video/7635267329407913230", URL])
async def test_unresolved_shortlink_forces_navigation_from_non_media_page(monkeypatch, current_url):
    page = SimpleNamespace(url=current_url, context=SimpleNamespace(cookies=AsyncMock(return_value=cookies("sessionid"))))

    async def goto(url, **kwargs):
        page.url = URL

    page.goto = AsyncMock(side_effect=goto)
    monkeypatch.setattr(user_schema, "_has_recent_http_preflight_valid", Mock(return_value=True))
    monkeypatch.setattr(user_schema, "_http_first_for_correlation", Mock(return_value=True))
    monkeypatch.setattr(user_schema, "_wait_for_url_settled", AsyncMock())
    monkeypatch.setattr(user_schema, "_fast_visible_captcha_reason", AsyncMock(return_value=None))
    monkeypatch.setattr(user_schema, "_deny_if_visible_logged_out", AsyncMock(return_value=None))
    monkeypatch.setattr(user_schema, "register_alive_session", Mock())
    dom = AsyncMock(side_effect=AssertionError("Must accept actual browser session without DOM navigation"))
    monkeypatch.setattr(user_schema, "_open_video_and_check", dom)
    shortlink = "https://vt.tiktok.com/local-test/"
    assert await user_schema._ensure_account_login_ready(
        None, page, SimpleNamespace(id=1), shortlink, timeout_ms=15000, correlation_id="task_parallel_reply_1",
    ) == (True, None)
    assert page.goto.await_count == int(current_url != URL)
    if current_url != URL:
        page.goto.assert_awaited_once_with(shortlink, wait_until="commit", timeout=15000)


@pytest.mark.parametrize("reason", [None, "warmup_exception", "parent_api_unavailable:empty_body", "parent_api_unavailable:timeout", "http_session_not_ready", "unexpected_preparation_error"])
async def test_explicit_no_publication_refunds_any_preparation_error(reason):
    assert is_pre_publish_account_failure(reason, publication_attempted=False)
    assert not is_pre_publish_account_failure(reason, publication_attempted=True)
    assert pre_publish_outcomes([SimpleNamespace(error=reason, attempted=True, publication_attempted=False)])
    assert not pre_publish_outcomes([SimpleNamespace(error=reason, attempted=False, publication_attempted=True)])
    assert not pre_publish_outcomes([SimpleNamespace(error=reason, publication_attempted=False), SimpleNamespace(error=reason, publication_attempted=True)])


async def test_unknown_submission_metadata_does_not_refund_ambiguous_results():
    assert is_pre_publish_account_failure("warmup_exception")
    assert pre_publish_outcomes([SimpleNamespace(error="warmup_exception")])
    assert not is_pre_publish_account_failure("unknown_exception")
    assert not pre_publish_outcomes([SimpleNamespace(error="unknown_exception")])
    assert not pre_publish_outcomes([])


@pytest.mark.parametrize("dynamic", [True, False])
@pytest.mark.parametrize("eventual_success", [True, False])
async def test_reply_preparation_failures_preserve_publish_budget_but_stop_at_account_limit(monkeypatch, dynamic, eventual_success):
    from contextlib import contextmanager
    from db import database
    from executor import browser_pool

    @contextmanager
    def context():
        yield SimpleNamespace()

    selected = []
    parents = [user_schema.PublishedMain("main", "7641761192486194454", True, False, "http_user_schema", 1, "demo")]

    def select(*args, **kwargs):
        account = SimpleNamespace(id=100 + len(selected), proxy=None)
        selected.append(account)
        return account

    async def reply(db, account, url, mains, text, **kwargs):
        success = len(selected) == 3 and eventual_success
        return user_schema.ReplyAttemptResult(
            replies=[user_schema.ReplyOutcome(
                text=text, variant_index=kwargs["variant_index"], matched_main_text=parents[0].text if success else None,
                parent_comment_id=parents[0].comment_id if success else None, success=success, method="http_user_schema:network",
                account_id=account.id, error=None if success else "parent_api_unavailable:empty_body",
                attempted=success, publication_attempted=success,
                reply_comment_id="7641761192486194455" if success else None,
                reply_confirmation="network_comment_id" if success else None,
            )],
            found_mains=parents if success else [],
        )

    monkeypatch.setattr(database, "get_sync_db_context", context)
    monkeypatch.setattr(user_schema, "get_free_account_for_main_comment", Mock(return_value=SimpleNamespace(id=1, proxy=None)))
    monkeypatch.setattr(user_schema, "_try_main_account", AsyncMock(return_value=(parents, None)))
    monkeypatch.setattr(user_schema, "_prepare_public_mains_before_replies", AsyncMock(return_value=parents))
    monkeypatch.setattr(user_schema, "get_free_account_for_reply", select)
    monkeypatch.setattr(user_schema, "_reply_with_variant_on_all_mains", reply)
    for name in ("release_main_account_reservation", "release_reply_account_reservation", "_record_main_retry_failure"):
        monkeypatch.setattr(user_schema, name, Mock())
    monkeypatch.setattr(user_schema, "extend_main_account_reservation", Mock(return_value=True))
    monkeypatch.setattr(user_schema.asyncio, "sleep", AsyncMock())
    monkeypatch.setattr(browser_pool, "ensure_thread_browser_pool_capacity", Mock())
    monkeypatch.setattr(browser_pool, "close_browser_pool", AsyncMock())
    for name, value in {
        "USE_PROXY": False, "TIKTOK_NETWORK_FIRST": True, "TIKTOK_NETWORK_IMPL_READY": True,
        "PARALLEL_COMMENTING_DOM_FIRST": False, "MAX_RETRY_ACCOUNTS": 3, "PARALLEL_MAIN_ACCOUNT_SLOTS": 1,
        "PARALLEL_COMMENTING_MAX_MAIN_ATTEMPTS": 1, "PARALLEL_COMMENTING_MAX_REPLY_ATTEMPTS": 1,
        "PARALLEL_DYNAMIC_WORKER_POOL_ENABLED": dynamic,
    }.items():
        monkeypatch.setattr(user_schema._settings, name, value)

    result = await user_schema.execute_user_schema_parallel(
        None, SimpleNamespace(settings={"replies": ["reply"], "reply_count": 1}), URL,
        ["main"], 123, boost_enabled=False, boost_quantity=0,
    )
    assert len(selected) == 3
    assert any(row.success for row in result.replies) is eventual_success
    assert (result.to_dict()["status"] == "success") is eventual_success


@pytest.mark.parametrize("reason", ["visible_login_ui:header", "captcha_challenge:visible"])
async def test_warmup_rejects_visible_auth_challenges_before_publish(monkeypatch, warmup, reason):
    page, _, _ = warmup
    monkeypatch.setattr(user_schema, "_visible_logged_out_reason", AsyncMock(return_value=reason if reason.startswith("visible") else None))
    monkeypatch.setattr(user_schema, "_fast_visible_captcha_reason", AsyncMock(return_value=reason if reason.startswith("captcha") else None))
    _, valid, info = await engagement_engine.warmup_page_for_http(page, URL)
    assert not valid
    assert reason in info["reason_code"]
    page.reload.assert_not_awaited()


async def test_http_preflight_immediately_rejects_unconfirmed_passport_and_caches_username(monkeypatch):
    monkeypatch.setattr(user_schema, "_http_first_for_correlation", Mock(return_value=True))
    dead_marks = Mock()
    monkeypatch.setattr(user_schema, "_register_dead_session", dead_marks)
    validate = AsyncMock(return_value={"status": "inconclusive", "username": None,
                                       "error": "Passport response did not confirm authentication"})
    monkeypatch.setattr("services.cookie_validator.validate_account_http", validate)

    dead_acc = SimpleNamespace(id=2560, cookies='[{"name":"sessionid","value":"x"}]', cookie_file=None, proxy=None)
    reason = await user_schema._preflight_cookie_http_gate(
        None, dead_acc, correlation_id="task_1218_parallel_main_v1_a1_acc2560", phase="main",
    )
    assert reason and reason.startswith("dead_session:http_preflight:")
    dead_marks.assert_called_once()

    validate.return_value = {"status": "valid", "valid": True, "username": "fast_user_2563", "error": None}
    live_acc = SimpleNamespace(id=2563, cookies='[{"name":"sessionid","value":"y"}]', cookie_file=None, proxy=None)
    reason_live = await user_schema._preflight_cookie_http_gate(
        None, live_acc, correlation_id="task_1218_parallel_main_v1_a1_acc2563", phase="main",
    )
    assert reason_live is None
    assert user_schema._get_cached_http_preflight_username(live_acc) == "fast_user_2563"


async def test_prepare_cookies_preserves_fresh_csrf_within_30_minutes():
    from datetime import datetime, timedelta
    from services.cookie_validator import prepare_cookies_for_context

    raw = [
        {"name": "sessionid", "value": "sess1", "domain": ".tiktok.com"},
        {"name": "ttwid", "value": "fresh_ttwid", "domain": ".tiktok.com"},
        {"name": "tt_csrf_token", "value": "fresh_csrf", "domain": ".tiktok.com"},
    ]
    fresh_account = SimpleNamespace(id=10, last_validation_ok=datetime.utcnow() - timedelta(minutes=10))
    fresh_names = {c["name"] for c in prepare_cookies_for_context(fresh_account, raw)}
    assert {"sessionid", "ttwid", "tt_csrf_token"} <= fresh_names

    stale_account = SimpleNamespace(id=11, last_validation_ok=datetime.utcnow() - timedelta(minutes=45))
    stale_names = {c["name"] for c in prepare_cookies_for_context(stale_account, raw)}
    assert {"sessionid", "ttwid"} <= stale_names
    assert "tt_csrf_token" not in stale_names


async def test_find_parent_comments_reuses_warmup_cached_first_page(monkeypatch):
    from executor.http import comment_discovery

    aweme_id = "7635267329407913230"
    page = SimpleNamespace(
        url=URL,
        _cached_first_comment_page=(aweme_id, {
            "comments": [{"cid": "7641761192486194454", "text": "Target main comment", "reply_id": "0",
                          "user": {"unique_id": "u1"}}],
            "has_more": 0,
            "cursor": 20,
        }),
    )
    fetch_mock = AsyncMock(side_effect=AssertionError("Should reuse cached first comment page without network GET"))
    monkeypatch.setattr(comment_discovery, "fetch_comment_page", fetch_mock)
    mains = [SimpleNamespace(text="Target main comment", tiktok_username="u1")]
    matches, meta = await comment_discovery.find_parent_comments(page, aweme_id, mains, max_pages=1)
    assert matches[0]["cid"] == "7641761192486194454"
    assert meta["available"] and meta["exhausted"]
    assert page._cached_first_comment_page is None
    fetch_mock.assert_not_awaited()


async def test_browser_pool_blocks_background_feeds_and_telemetry_without_blocking_html_or_css():
    from executor.browser_pool import _should_abort_resource

    assert _should_abort_resource("document", URL) is False
    assert _should_abort_resource("stylesheet", "https://sf16-website-login.neutral.ttwstatic.com/obj/tiktok_web_login_static/tt.css") is False
    assert _should_abort_resource("media", "https://v16-webapp-prime.tiktok.com/video/tos/useast2a/video.mp4") is True
    assert _should_abort_resource("fetch", "https://www.tiktok.com/api/recommend/item_list/?aid=1988") is False
    assert _should_abort_resource("xhr", "https://www.tiktok.com/api/post/item_list/?aid=1988") is False
    assert _should_abort_resource("fetch", "https://www.tiktok.com/api/im/multi_conversation/") is False
    assert _should_abort_resource("fetch", "https://www.tiktok.com/api/fullpage/item_list/") is False
    assert _should_abort_resource("ping", "https://mon.tiktokv.com/monitor_browser/collect/batch/") is False
    assert _should_abort_resource("fetch", "https://mcs.tiktokv.com/v1/list") is False
    assert _should_abort_resource("xhr", "https://log.tiktokv.com/service/2/app_log/") is False
    assert _should_abort_resource("fetch", "https://www.tiktok.com/api/comment/publish/?aid=1988") is False
    assert _should_abort_resource("fetch", "https://www.tiktok.com/api/comment/list/?aid=1988") is False


async def test_delay_ranges_keep_same_account_safe_and_cross_account_fast():
    from config.settings import get_settings

    s = get_settings()
    assert user_schema._INTER_MAIN_VARIANT_DELAY_RANGE == (2.5, 4.0)
    assert user_schema._RETRY_ACCOUNT_DELAY_RANGE == (0.1, 0.3)
    assert s.PARALLEL_REPLY_START_DELAY_MIN_SECONDS == 0.1
    assert s.PARALLEL_REPLY_START_DELAY_MAX_SECONDS == 0.3


