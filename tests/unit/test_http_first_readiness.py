from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from executor import engagement_engine, user_schema
from executor.cookie_health import CookieHealth, CookieVerdict


pytestmark = [pytest.mark.unit, pytest.mark.asyncio]
MEDIA_ID = "7635267329407913230"
PHOTO_URL = f"https://www.tiktok.com/@demo/photo/{MEDIA_ID}"
CID = "7641761192486194454"


@pytest.fixture
def http_mode(monkeypatch):
    monkeypatch.setattr(user_schema._settings, "TIKTOK_NETWORK_FIRST", True)
    monkeypatch.setattr(user_schema._settings, "TIKTOK_NETWORK_IMPL_READY", True)
    monkeypatch.setattr(user_schema._settings, "PARALLEL_COMMENTING_DOM_FIRST", False)


@pytest.mark.parametrize("correlation", ["task_parallel_main_1", "task_parallel_reply_1", "task_engagement_reply_1"])
async def test_parallel_navigation_stays_fast_in_http_mode(http_mode, correlation):
    assert user_schema._is_parallel_workflow_correlation(correlation)
    assert not user_schema._parallel_dom_first_for_correlation(correlation)
    assert user_schema._parallel_fast_auth_gate_enabled(correlation)
    assert user_schema._initial_navigation_wait_until(correlation) == "commit"
    assert not user_schema._is_parallel_workflow_correlation("ordinary")


async def test_http_navigation_classifies_loading_without_opening_comments(monkeypatch, http_mode):
    page = SimpleNamespace(url=PHOTO_URL, goto=AsyncMock())
    health = CookieHealth(CookieVerdict.AMBIGUOUS, "page_loading:video", PHOTO_URL, True)
    probe = AsyncMock(return_value=health)
    opener = AsyncMock(side_effect=AssertionError("HTTP navigation must not open comments"))
    monkeypatch.setattr(user_schema, "_wait_for_url_settled", AsyncMock(return_value=PHOTO_URL))
    monkeypatch.setattr(user_schema, "_fast_cookie_health_probe", probe)
    monkeypatch.setattr(user_schema, "_open_comments_during_initial_load", opener)
    result = await user_schema._open_video_and_check(page, PHOTO_URL, timeout_ms=1000, correlation_id="task_parallel_main_1")
    assert result is health
    assert page.goto.await_args.kwargs["wait_until"] == "commit"
    assert probe.await_args.kwargs["classify_loading"] is True
    opener.assert_not_awaited()


@pytest.mark.parametrize("guard", [None, "captcha", "logout"])
async def test_http_preflight_and_browser_cookies_do_not_wait_for_dom_profile(monkeypatch, http_mode, guard):
    page = SimpleNamespace(url="about:blank", context=SimpleNamespace(cookies=AsyncMock(return_value=[
        {"name": "sessionid", "value": "session", "domain": ".tiktok.com"},
    ])))

    async def goto(url, **kwargs):
        page.url = url

    page.goto = AsyncMock(side_effect=goto)
    monkeypatch.setattr(user_schema, "_has_recent_http_preflight_valid", Mock(return_value=True))
    monkeypatch.setattr(user_schema, "_wait_for_url_settled", AsyncMock(return_value=PHOTO_URL))
    dom_probe = AsyncMock(side_effect=AssertionError("HTTP-valid session must not wait for a DOM profile/composer"))
    monkeypatch.setattr(user_schema, "_open_video_and_check", dom_probe)
    monkeypatch.setattr(user_schema, "_fast_visible_captcha_reason", AsyncMock(return_value="captcha_challenge:test" if guard == "captcha" else None))
    monkeypatch.setattr(user_schema, "_deny_if_visible_logged_out", AsyncMock(return_value="visible_login_ui:test" if guard == "logout" else None))
    monkeypatch.setattr(user_schema, "_register_captcha_session", Mock())
    monkeypatch.setattr(user_schema, "register_alive_session", Mock())

    ready, reason = await user_schema._ensure_account_login_ready(
        None, page, SimpleNamespace(id=7), PHOTO_URL, timeout_ms=1000, correlation_id="task_engagement_reply_1",
    )
    assert ready is (guard is None)
    assert reason == ({None: None, "captcha": "captcha_challenge:test", "logout": "visible_login_ui:test"}[guard])
    assert page.goto.await_args.kwargs["wait_until"] == "commit"
    dom_probe.assert_not_awaited()


@pytest.mark.parametrize("scope", [user_schema.MAIN_SCOPE, user_schema.REPLY_SCOPE])
async def test_prepare_preserves_captcha_reason_for_retry_budget(monkeypatch, http_mode, scope):
    page = SimpleNamespace(url=PHOTO_URL)
    monkeypatch.setattr(user_schema, "get_http_execution_strategy", Mock(return_value={"strategy": "http_first"}))
    monkeypatch.setattr(engagement_engine, "_should_skip_http_for_account", Mock(return_value=None))
    monkeypatch.setattr(engagement_engine, "_record_http_account_result", Mock())
    monkeypatch.setattr(engagement_engine, "warmup_page_for_http", AsyncMock(return_value=(
        {"sessionid": "session", "ttwid": "wid"}, True, {"current_url": PHOTO_URL},
    )))
    monkeypatch.setattr(user_schema, "_visible_captcha_reason", AsyncMock(return_value="captcha_challenge:slider"))
    session = await user_schema._prepare_user_schema_http_session(
        page, SimpleNamespace(id=7), PHOTO_URL, scope=scope, correlation_id="prepare_http",
    )
    assert session is None
    assert user_schema._http_preparation_error(page, scope) == "captcha_challenge:slider"


@pytest.mark.parametrize("reason,preflight,verdict", [
    ("strong_marker:profile-link:/@demo", False, CookieVerdict.READY),
    ("on_video_no_login_btn:video", True, CookieVerdict.AMBIGUOUS),
    ("on_video_no_login_btn_unconfirmed", True, CookieVerdict.READY),
])
@pytest.mark.parametrize("guard", [None, "captcha", "logout"])
async def test_http_auth_skips_composer_but_keeps_guards(monkeypatch, http_mode, reason, preflight, verdict, guard):
    health = CookieHealth(verdict, reason, PHOTO_URL, True)
    monkeypatch.setattr(user_schema, "_open_video_and_check", AsyncMock(return_value=health))
    monkeypatch.setattr(user_schema, "_has_recent_http_preflight_valid", Mock(return_value=preflight))
    composer = AsyncMock(side_effect=AssertionError("HTTP auth must not open a composer"))
    monkeypatch.setattr(user_schema, "_confirm_comment_action_ready", composer)
    monkeypatch.setattr(user_schema, "_accept_action_ready_surface", composer)
    monkeypatch.setattr(user_schema, "_fast_visible_captcha_reason", AsyncMock(return_value="captcha_challenge:test" if guard == "captcha" else None))
    monkeypatch.setattr(user_schema, "_deny_if_visible_logged_out", AsyncMock(return_value="dead_session:visible_login_ui" if guard == "logout" else None))
    alive = Mock()
    monkeypatch.setattr(user_schema, "register_alive_session", alive)
    monkeypatch.setattr(user_schema, "_register_captcha_session", Mock())

    ok, denied = await user_schema._ensure_account_login_ready(
        None, SimpleNamespace(url=PHOTO_URL), SimpleNamespace(id=1), PHOTO_URL,
        timeout_ms=1000, correlation_id="task_parallel_reply_1",
    )
    assert ok is (guard is None)
    assert (denied is None) is (guard is None)
    assert alive.call_count == int(guard is None)
    composer.assert_not_awaited()


def main(text, cid=CID, confirmed=True):
    return user_schema.PublishedMain(text, cid, confirmed, False, "http", 1, None)


async def test_http_auth_accepts_strong_marker_after_loading_retry(monkeypatch, http_mode):
    loading = CookieHealth(CookieVerdict.AMBIGUOUS, "page_loading:video", PHOTO_URL, True)
    ready = CookieHealth(CookieVerdict.READY, "strong_marker:profile-link:/@demo", PHOTO_URL, True)
    monkeypatch.setattr(user_schema, "_open_video_and_check", AsyncMock(return_value=loading))
    monkeypatch.setattr(user_schema, "_fast_cookie_health_probe", AsyncMock(return_value=ready))
    monkeypatch.setattr(user_schema, "_has_recent_http_preflight_valid", Mock(return_value=False))
    monkeypatch.setattr(user_schema, "_fast_visible_captcha_reason", AsyncMock(return_value=None))
    monkeypatch.setattr(user_schema, "_deny_if_visible_logged_out", AsyncMock(return_value=None))
    monkeypatch.setattr(user_schema, "register_alive_session", Mock())
    composer = AsyncMock(side_effect=AssertionError("Retry must not open a composer"))
    monkeypatch.setattr(user_schema, "_confirm_comment_action_ready", composer)
    page = SimpleNamespace(url=PHOTO_URL, reload=AsyncMock())
    assert await user_schema._ensure_account_login_ready(
        None, page, SimpleNamespace(id=1), PHOTO_URL, timeout_ms=1000,
        correlation_id="task_parallel_main_1",
    ) == (True, None)
    page.reload.assert_not_awaited()
    composer.assert_not_awaited()


@pytest.mark.parametrize("case", ["confirmed", "missing_text", "invalid_cid", "not_network", "duplicate_cid", "dom_first"])
async def test_observers_skipped_only_for_complete_network_parents(monkeypatch, http_mode, case):
    parents = [main("first"), main("second", str(int(CID) + 1))]
    if case == "missing_text":
        parents.pop()
    if case == "invalid_cid":
        parents[1] = main("second", "fake")
    if case == "not_network":
        parents[1] = main("second", str(int(CID) + 1), False)
    if case == "duplicate_cid":
        parents[1] = main("second")
    if case == "dom_first":
        monkeypatch.setattr(user_schema._settings, "PARALLEL_COMMENTING_DOM_FIRST", True)
    # An unconfirmed later duplicate must not hide a valid network parent.
    if case == "confirmed":
        parents.append(main("first", "fake", False))
    observer = AsyncMock(side_effect=RuntimeError("observer invoked"))
    monkeypatch.setattr(user_schema, "_confirm_mains_by_observers", observer)
    result = user_schema.UserSchemaResult(published_mains=parents)
    call = user_schema._prepare_public_mains_before_replies(
        None, PHOTO_URL, ["first", "second", "first"], result,
        task_id=1, used_reply_accounts=[], pool_owner_user_id=None,
    )
    if case in {"confirmed", "dom_first"}:
        active = await call
        assert [p.text for p in active] == ["first", "second"]
        assert len({p.comment_id for p in active}) == 2
        assert not result.main_found_by_observer
        observer.assert_not_awaited()
    else:
        assert await call == []
        assert result.error == "main_http_confirmation_incomplete"
        observer.assert_not_awaited()


@pytest.mark.parametrize("reuse", [True, False])
async def test_photo_warmup_preserves_media_type(monkeypatch, reuse):
    cookies = [{"name": name, "value": name, "domain": ".tiktok.com"}
               for name in ("sessionid", "ttwid", "tt_csrf_token", "msToken")]
    page = SimpleNamespace(url=PHOTO_URL if reuse else "about:blank",
                           context=SimpleNamespace(cookies=AsyncMock(return_value=cookies)),
                           wait_for_load_state=AsyncMock(), reload=AsyncMock())

    async def goto(url, **kwargs):
        page.url = url

    page.goto = AsyncMock(side_effect=goto)
    monkeypatch.setattr(engagement_engine, "wait_for_runtime_signer_ready", AsyncMock(return_value=False))
    monkeypatch.setattr(engagement_engine.asyncio, "sleep", AsyncMock())
    _, valid, info = await engagement_engine.warmup_page_for_http(page, PHOTO_URL)
    assert valid
    assert info["target_url"] == PHOTO_URL
    assert info["reused_page"] is reuse
    assert page.goto.await_count == int(not reuse)
    page.reload.assert_not_awaited()


@pytest.mark.parametrize("redirect", [f"https://example.com/photo/{MEDIA_ID}", "https://www.tiktok.com/@other/photo/1111111111111111111"])
async def test_warmup_rejects_external_or_wrong_media(monkeypatch, redirect):
    page = SimpleNamespace(url="about:blank", goto=AsyncMock(), reload=AsyncMock())

    async def goto(*args, **kwargs):
        page.url = redirect

    page.goto.side_effect = goto
    _, valid, info = await engagement_engine.warmup_page_for_http(page, PHOTO_URL, max_retries=1)
    assert not valid
    assert info["reason_code"] == "redirect_not_video"
