import asyncio
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from db.models import Account
from executor import engagement_engine, user_schema
from executor.publish_action import PublishResult
from services.cookie_validator import parse_cookies_to_dict


pytestmark = [pytest.mark.unit, pytest.mark.asyncio]
VIDEO_ID = "7635267329407913230"
VIDEO_URL = f"https://www.tiktok.com/@demo/video/{VIDEO_ID}"
PARENT_ID = "7641761192486194454"
COOKIES = [{"name": name, "value": value, "domain": ".tiktok.com"} for name, value in
           (("sessionid", "session"), ("ttwid", "wid"), ("tt_csrf_token", "csrf"), ("msToken", "web"))]


@pytest.mark.parametrize("current_url, reuse", [
    (VIDEO_URL + "?source=test", True),
    (f"https://www.tiktok.com/video/{VIDEO_ID}", True),
    ("https://www.tiktok.com/@other/video/1111111111111111111", False),
    (f"https://www.tiktok.com/login?next=/video/{VIDEO_ID}", False),
    (f"https://example.com/video/{VIDEO_ID}", False),
    ("about:blank", False),
])
async def test_warmup_only_reuses_the_current_target_video(monkeypatch, current_url, reuse):
    page = SimpleNamespace(
        url=current_url, context=SimpleNamespace(cookies=AsyncMock(return_value=COOKIES)),
        wait_for_load_state=AsyncMock(), reload=AsyncMock(),
    )

    async def goto(url, **_kwargs):
        page.url = url

    page.goto = AsyncMock(side_effect=goto)
    monkeypatch.setattr(engagement_engine, "wait_for_runtime_signer_ready", AsyncMock(return_value=False))
    monkeypatch.setattr(engagement_engine.asyncio, "sleep", AsyncMock())

    _, valid, info = await engagement_engine.warmup_page_for_http(page, VIDEO_URL)

    assert valid is True
    assert info["reused_page"] is reuse
    assert page.goto.await_count == int(not reuse)
    page.reload.assert_not_awaited()


async def test_video_cookie_sync_saves_fresh_cookies_before_deciding_to_enrich(db_session):
    account = Account(label="sync-on-video", cookies='{"sid_guard":"' + "b" * 32 + '|1700000000|86400"}')
    db_session.add(account)
    db_session.commit()
    records = [
        {"name": "sid_guard", "value": "b" * 32 + "|1700000000|86400", "domain": ".tiktok.com"},
        *[c for c in COOKIES if c["name"] != "sessionid"],
    ]
    context = SimpleNamespace(cookies=AsyncMock(return_value=records), add_cookies=AsyncMock())
    assert user_schema.needs_enrichment(account)

    needed = await user_schema._video_cookies_need_enrichment(SimpleNamespace(context=context), account, db_session)

    assert needed is False
    assert user_schema.needs_enrichment(account) is False
    context.add_cookies.assert_awaited_once()
    saved = parse_cookies_to_dict(account.cookies)
    assert saved["ttwid"] == "wid" and saved["tt_csrf_token"] == "csrf"
    assert saved["sessionid"] == "b" * 32


async def test_live_missing_cookie_requires_enrichment_even_when_database_is_rich():
    account = SimpleNamespace(id=1, cookies=json.dumps(COOKIES), cookie_file=None)
    page = SimpleNamespace(context=SimpleNamespace(cookies=AsyncMock(return_value=[c for c in COOKIES if c["name"] != "tt_csrf_token"])))
    assert user_schema.needs_enrichment(account) is False
    assert await user_schema._video_cookies_need_enrichment(page, account, None) is True


@pytest.mark.parametrize("needs_linkage_cookies", [False, True])
@pytest.mark.parametrize("auth_age", [None, 1, 16, -1])
async def test_main_http_uses_target_warmup_without_dom_enrichment(monkeypatch, needs_linkage_cookies, auth_age):
    records = COOKIES if not needs_linkage_cookies else [c for c in COOKIES if c["name"] not in {"ttwid", "tt_csrf_token"}]
    context = SimpleNamespace(cookies=AsyncMock(return_value=records))
    page = SimpleNamespace(url=VIDEO_URL, context=context, goto=AsyncMock())
    if auth_age is not None:
        page._http_auth_verified_at = user_schema.time.monotonic() - auth_age
    account = SimpleNamespace(id=77, proxy=None, cookies=json.dumps(COOKIES), cookie_file=None)
    monkeypatch.setattr(user_schema._settings, "TIKTOK_NETWORK_FIRST", True)
    monkeypatch.setattr(user_schema._settings, "TIKTOK_NETWORK_IMPL_READY", True)
    monkeypatch.setattr(user_schema._settings, "PARALLEL_COMMENTING_DOM_FIRST", False)
    for name, result in {
        "_preflight_cookie_http_gate": None, "create_page_for_account": (context, page),
        "_ensure_account_login_ready": (True, None), "_fast_visible_captcha_reason": None,
        "_deny_if_visible_logged_out": None, "_ensure_page_on_target_media": True,
        "_persist_cookies_after_success": None, "_safe_close": None,
    }.items():
        monkeypatch.setattr(user_schema, name, AsyncMock(return_value=result))
    publish = AsyncMock(return_value=([main()], None))
    monkeypatch.setattr(user_schema, "_publish_all_variants_on_account", publish)
    monkeypatch.setattr(user_schema, "_video_cookies_need_enrichment", AsyncMock(return_value=needs_linkage_cookies))
    enrich = AsyncMock(side_effect=AssertionError("HTTP warmup must not navigate to foryou"))
    warmup = AsyncMock(side_effect=AssertionError("HTTP-first must not use DOM warmup"))
    monkeypatch.setattr(user_schema, "_enrich_cookies_with_hard_deadline", enrich)
    monkeypatch.setattr(user_schema, "like_random_comment_before_action", warmup)
    monkeypatch.setattr(user_schema.asyncio, "sleep", AsyncMock())

    published, error = await user_schema._try_main_account(SimpleNamespace(), account, VIDEO_URL, ["main"], correlation_id="http_main")

    assert len(published) == 1 and error is None
    publish.assert_awaited_once()
    page.goto.assert_not_awaited()
    enrich.assert_not_awaited()
    warmup.assert_not_awaited()
    expected_probes = 0 if auth_age == 1 else 2
    assert user_schema._fast_visible_captcha_reason.await_count == expected_probes
    assert user_schema._deny_if_visible_logged_out.await_count == expected_probes


async def test_dom_first_keeps_captcha_guard_even_with_recent_http_auth(monkeypatch):
    page = SimpleNamespace(url=VIDEO_URL, _http_auth_verified_at=user_schema.time.monotonic())
    account = SimpleNamespace(id=77, proxy=None)
    monkeypatch.setattr(user_schema, "_http_first_for_correlation", lambda *args: False)
    for name, value in {
        "_preflight_cookie_http_gate": None, "create_page_for_account": (SimpleNamespace(), page),
        "_ensure_account_login_ready": (True, None), "_video_cookies_need_enrichment": False,
        "_fast_visible_captcha_reason": "captcha_challenge:fixture", "_safe_close": None,
    }.items():
        monkeypatch.setattr(user_schema, name, AsyncMock(return_value=value))
    monkeypatch.setattr(user_schema, "_register_captcha_session", Mock())
    monkeypatch.setattr(user_schema.asyncio, "sleep", AsyncMock())
    published, reason = await user_schema._try_main_account(
        SimpleNamespace(), account, VIDEO_URL, ["main"], correlation_id="dom_main",
    )
    assert published == [] and reason == "captcha_challenge:fixture"
    user_schema._fast_visible_captcha_reason.assert_awaited_once()


@pytest.fixture
def reply_env(monkeypatch):
    context = SimpleNamespace(cookies=AsyncMock(return_value=COOKIES))
    page = SimpleNamespace(url=VIDEO_URL, context=context, goto=AsyncMock())
    account = SimpleNamespace(id=77, proxy=None, cookies=json.dumps(COOKIES), cookie_file=None)
    session = user_schema._UserSchemaHttpSession(
        scope=user_schema.REPLY_SCOPE, account_id=77, client=SimpleNamespace(last_request_meta={"request_attempted": True}),
        cookies={}, aweme_id=VIDEO_ID, video_url=VIDEO_URL,
    )
    monkeypatch.setattr(user_schema._settings, "TIKTOK_NETWORK_FIRST", True)
    monkeypatch.setattr(user_schema._settings, "TIKTOK_NETWORK_IMPL_READY", True)
    monkeypatch.setattr(user_schema._settings, "PARALLEL_COMMENTING_DOM_FIRST", False)
    mocks = {}
    for name, value in {
        "_preflight_cookie_http_gate": None, "create_page_for_account": (context, page),
        "_ensure_account_login_ready": (True, None), "_visible_captcha_reason": None,
        "_fast_visible_captcha_reason": None, "_deny_if_visible_logged_out": None,
        "_ensure_page_on_target_media": True, "_safe_close": None,
        "_persist_cookies_after_success": None, "_prepare_user_schema_http_session": session,
        "_open_comment_list_with_safe_retry": True, "_wait_for_searchable_comment_surface": True,
        "_read_tiktok_total_comment_count": 10,
        "_find_main_anchors_batch": ([], {"max_seen_rows": 1, "search_exhausted": True}),
        "_verify_reply_visible_near_parent": False,
        "_enrich_cookies_with_hard_deadline": None,
    }.items():
        mocks[name] = AsyncMock(return_value=value)
        monkeypatch.setattr(user_schema, name, mocks[name])
    for name in ("_register_runtime_success", "_register_runtime_failure", "record_reply_attempt"):
        mocks[name] = Mock()
        monkeypatch.setattr(user_schema, name, mocks[name])
    mocks["publish_reply_hybrid"] = AsyncMock(return_value=PublishResult(
        status="success", comment_id="dom-reply", network_captured=True, dom_strategy="http_user_schema:test_dom_fallback",
    ))
    monkeypatch.setattr(user_schema, "publish_reply_hybrid", mocks["publish_reply_hybrid"])
    mocks["http"] = AsyncMock(return_value=PublishResult(
        status="success", comment_id="http-reply", network_captured=True,
        network_request_attempted=True, dom_strategy="http_user_schema:network_exact",
    ))
    monkeypatch.setattr(user_schema, "_publish_reply_http_first_if_eligible", mocks["http"])
    monkeypatch.setattr(user_schema.asyncio, "sleep", AsyncMock())

    async def run(mains, **kwargs):
        return await user_schema._reply_with_variant_on_all_mains(
            SimpleNamespace(), account, VIDEO_URL, mains, "reply", variant_index=1,
            correlation_id="fast_http_reply", **kwargs,
        )

    return SimpleNamespace(run=run, mocks=mocks, page=page, account=account, session=session)


def main(cid=PARENT_ID, text="main", account_id=10):
    return user_schema.PublishedMain(text, cid, True, False, "http_user_schema", account_id, None)


@pytest.mark.parametrize("auth_age", [None, 1, 16, -1])
async def test_known_parent_http_reply_never_opens_or_searches_dom(reply_env, auth_age):
    if auth_age is not None:
        reply_env.page._http_auth_verified_at = user_schema.time.monotonic() - auth_age
    done, inflight = set(), set()
    observed = []

    def submitted(outcome):
        observed.append((outcome.reply_attempt_key in inflight, outcome.parent_comment_id in done))

    result = await reply_env.run([main()], already_replied_parent_keys=done, inflight_reply_keys=inflight, on_submitted_outcome=submitted)

    assert result.replies[0].success
    assert result.replies[0].counts_as_submitted
    assert result.searched is False
    assert done == {PARENT_ID} and inflight == set()
    assert observed == [(True, True)]
    for name in ("_open_comment_list_with_safe_retry", "_find_main_anchors_batch", "_verify_reply_visible_near_parent", "publish_reply_hybrid", "_enrich_cookies_with_hard_deadline"):
        reply_env.mocks[name].assert_not_awaited()
    reply_env.page.goto.assert_not_awaited()
    expected_probes = int(auth_age != 1)
    assert reply_env.mocks["_visible_captcha_reason"].await_count == expected_probes
    assert reply_env.mocks["_deny_if_visible_logged_out"].await_count == expected_probes


async def test_empty_http_reply_stops_account_without_counting_or_dom_fallback(reply_env):
    reply_env.mocks["http"].return_value = PublishResult(
        status="failed", error_reason="stealth_reject_empty_body", dom_strategy="http_user_schema",
        network_request_attempted=True, post_was_enabled_at_click=True,
    )
    soft = set()
    result = await reply_env.run([main(), main(str(int(PARENT_ID) + 1), "second")], soft_submitted_reply_keys=soft)
    assert len(result.replies) == 1
    assert not result.replies[0].success
    assert not result.replies[0].submitted_unconfirmed
    assert not result.replies[0].counts_as_submitted
    assert soft == set()
    reply_env.mocks["http"].assert_awaited_once()
    reply_env.mocks["publish_reply_hybrid"].assert_not_awaited()
    reply_env.mocks["_open_comment_list_with_safe_retry"].assert_not_awaited()


@pytest.mark.parametrize("account_id", [0, 10])
async def test_unknown_parent_resolves_via_api_and_shared_cache_without_dom(reply_env, monkeypatch, account_id):
    from executor.http import comment_discovery
    fetch = AsyncMock(return_value={"comments": [{"cid": PARENT_ID, "text": "main", "user": {"unique_id": "author"}}], "has_more": 0})
    monkeypatch.setattr(comment_discovery, "fetch_comment_page", fetch)
    cache = user_schema._SharedParentSearchCache()
    for _ in range(2):
        candidate = main(None, account_id=account_id)
        result = await reply_env.run([candidate], shared_parent_cache=cache)
        assert result.replies[0].success
        assert result.replies[0].parent_comment_id == PARENT_ID
    fetch.assert_awaited_once()
    reply_env.mocks["_open_comment_list_with_safe_retry"].assert_not_awaited()
    reply_env.mocks["publish_reply_hybrid"].assert_not_awaited()


async def test_valid_parent_api_empty_list_does_not_scroll_dom(reply_env, monkeypatch):
    from executor.http import comment_discovery
    monkeypatch.setattr(comment_discovery, "fetch_comment_page", AsyncMock(return_value={"comments": [], "has_more": 0}))
    result = await reply_env.run([main(None)])
    assert result.replies[0].error == "parent_not_found_via_api"
    assert not result.replies[0].counts_as_submitted
    reply_env.mocks["_open_comment_list_with_safe_retry"].assert_not_awaited()


@pytest.mark.parametrize("failure", ["failed", "pre_submit_refusal"])
async def test_api_parent_is_recorded_even_when_http_reply_fails(reply_env, monkeypatch, failure):
    from executor.http import comment_discovery
    monkeypatch.setattr(comment_discovery, "fetch_comment_page", AsyncMock(return_value={
        "comments": [{"cid": PARENT_ID, "text": "main", "user": {"unique_id": "api_author"}}], "has_more": 0,
    }))
    reply_env.mocks["http"].return_value = None if failure == "pre_submit_refusal" else PublishResult(
        status="failed", error_reason="stealth_reject_empty_body", dom_strategy="http_user_schema",
        network_request_attempted=True,
    )
    result = await reply_env.run([main(None)])
    assert [parent.comment_id for parent in result.found_mains] == [PARENT_ID]
    assert result.found_mains[0].tiktok_username == "api_author"
    if failure == "failed":
        assert not result.replies[0].success
        reply_env.mocks["publish_reply_hybrid"].assert_not_awaited()


async def test_unavailable_parent_api_stops_without_dom_fallback(reply_env, monkeypatch):
    from executor.http import comment_discovery
    monkeypatch.setattr(comment_discovery, "fetch_comment_page", AsyncMock(return_value=None))
    result = await reply_env.run([main(None)])
    assert result.replies[0].error.startswith("parent_api_unavailable:")
    reply_env.mocks["_open_comment_list_with_safe_retry"].assert_not_awaited()
    reply_env.mocks["publish_reply_hybrid"].assert_not_awaited()


@pytest.mark.parametrize("mode", ["already_done", "inflight", "soft_locked"])
async def test_direct_reply_respects_existing_parent_locks(reply_env, mode):
    attempt_key = user_schema._reply_attempt_key(PARENT_ID, "reply")
    result = await reply_env.run(
        [main()], already_replied_parent_keys={PARENT_ID} if mode == "already_done" else set(),
        inflight_reply_keys={attempt_key} if mode == "inflight" else set(),
        soft_submitted_reply_keys={attempt_key} if mode == "soft_locked" else set(),
    )
    assert result.replies == []
    reply_env.mocks["http"].assert_not_awaited()
    reply_env.mocks["_open_comment_list_with_safe_retry"].assert_not_awaited()
    reply_env.mocks["create_page_for_account"].assert_not_awaited()


async def test_direct_reply_keeps_distinct_ids_with_same_text(reply_env):
    other_id = "7641761192486194455"
    result = await reply_env.run([main(), main(other_id)])
    assert [outcome.parent_comment_id for outcome in result.replies] == [PARENT_ID, other_id]
    assert reply_env.mocks["_prepare_user_schema_http_session"].await_count == 1


async def test_direct_reply_deduplicates_repeated_parent_id(reply_env):
    result = await reply_env.run([main(), main()])
    assert len(result.replies) == 1
    assert reply_env.mocks["http"].await_count == 1


async def test_unknown_parent_api_failure_does_not_erase_known_parent_http_success(reply_env, monkeypatch):
    from executor.http import comment_discovery
    monkeypatch.setattr(comment_discovery, "fetch_comment_page", AsyncMock(return_value=None))
    events = []
    original_http = reply_env.mocks["http"].return_value

    async def http(*_args, **_kwargs):
        events.append("http")
        return original_http

    async def batch(_page, candidates, **_kwargs):
        events.append("search")
        assert [m.text for m in candidates] == ["unknown"]
        return [], {"max_seen_rows": 1, "search_exhausted": True, "found_count": 0}

    reply_env.mocks["http"].side_effect = http
    reply_env.mocks["_find_main_anchors_batch"].side_effect = batch
    result = await reply_env.run([main(), main(None, "unknown", 11)])
    assert result.replies[0].success
    assert events == ["http"]
    assert result.replies[-1].error.startswith("parent_api_unavailable:")


@pytest.mark.parametrize("prepare_fails", [True, False])
async def test_no_dom_fallback_after_http_pre_submit_refusal(reply_env, prepare_fails):
    if prepare_fails:
        reply_env.mocks["_prepare_user_schema_http_session"].return_value = None
    else:
        reply_env.mocks["http"].return_value = None
    reply_env.mocks["_find_main_anchors_batch"].return_value = ([], {"max_seen_rows": 1, "search_exhausted": True})

    result = await reply_env.run([main()])

    assert not result.replies[0].success
    assert not result.replies[0].publication_attempted
    reply_env.mocks["_open_comment_list_with_safe_retry"].assert_not_awaited()
    reply_env.mocks["_find_main_anchors_batch"].assert_not_awaited()
    reply_env.mocks["publish_reply_hybrid"].assert_not_awaited()


async def test_ambiguous_http_reply_is_soft_locked_without_dom_retry(reply_env):
    reply_env.mocks["http"].return_value = PublishResult(
        status="timeout", error_reason="no_network_response_after_publish_request",
        network_request_attempted=True, post_was_enabled_at_click=True, dom_strategy="http_user_schema",
    )
    locks, inflight = set(), set()
    observed = []
    callback = Mock(side_effect=lambda outcome: observed.append(outcome.reply_attempt_key in inflight))
    result = await reply_env.run([main()], soft_submitted_reply_keys=locks, inflight_reply_keys=inflight, on_soft_submitted_outcome=callback)
    assert result.replies[0].soft_submitted and not result.replies[0].success
    assert user_schema._reply_attempt_key(PARENT_ID, "reply") in locks
    assert not inflight
    callback.assert_called_once()
    assert observed == [True]
    reply_env.mocks["publish_reply_hybrid"].assert_not_awaited()
    reply_env.mocks["_open_comment_list_with_safe_retry"].assert_not_awaited()


async def test_cancellation_releases_inflight_direct_reply_lock(reply_env):
    reply_env.mocks["http"].side_effect = asyncio.CancelledError()
    inflight = set()
    with pytest.raises(asyncio.CancelledError):
        await reply_env.run([main()], inflight_reply_keys=inflight)
    assert not inflight


async def test_direct_reply_rechecks_locks_after_http_preparation(reply_env):
    soft_locks = set()

    async def prepare(*_args, **_kwargs):
        soft_locks.add(user_schema._reply_attempt_key(PARENT_ID, "reply"))
        return reply_env.session

    reply_env.mocks["_prepare_user_schema_http_session"].side_effect = prepare
    result = await reply_env.run([main()], soft_submitted_reply_keys=soft_locks)
    assert result.replies == []
    reply_env.mocks["http"].assert_not_awaited()
    reply_env.mocks["_open_comment_list_with_safe_retry"].assert_not_awaited()


async def test_unexpected_exception_after_http_attempt_does_not_retry_via_dom(reply_env):
    reply_env.mocks["http"].side_effect = RuntimeError("response handler failed")
    locks = set()
    result = await reply_env.run([main()], soft_submitted_reply_keys=locks)
    assert result.replies[0].soft_submitted
    assert user_schema._reply_attempt_key(PARENT_ID, "reply") in locks
    reply_env.mocks["publish_reply_hybrid"].assert_not_awaited()
    reply_env.mocks["_open_comment_list_with_safe_retry"].assert_not_awaited()
