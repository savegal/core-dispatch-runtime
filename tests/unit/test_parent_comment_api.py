import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from urllib.parse import parse_qs, urlparse

from executor.http import comment_discovery as api
from services.execution_attempts import is_pre_publish_account_failure, pre_publish_outcomes
from services.cookie_validator import normalize_cookies_for_browser

pytestmark = [pytest.mark.unit, pytest.mark.asyncio]
CID = "7641761192486194454"


def api_page(response):
    async def evaluate(script, arg=None, **kwargs):
        assert kwargs["isolated_context"] is False
        if arg is None:
            return {"user_agent": "browser-UA", "browser_language": "en-GB", "region": "GB", "timezone": "Europe/London"}
        return {"url":"https://www.tiktok.com/api/comment/list/?X-Bogus=local&X-Gnarly=local", **response}
    return SimpleNamespace(url="https://www.tiktok.com/@demo/photo/123", evaluate=AsyncMock(side_effect=evaluate),
                           context=SimpleNamespace(cookies=AsyncMock(return_value=[]), _account_proxy_in_use=True))


@pytest.mark.parametrize("first", [
    {"status":200, "text":"", "url":"https://www.tiktok.com/api/comment/list/?X-Bogus=x&X-Gnarly=y"},
    {"status":200, "text":"", "url":"https://www.tiktok.com/api/comment/list/"},
    {"status":200, "text":'{"comments":[]}', "url":"https://www.tiktok.com/api/comment/list/?X-Bogus=x"},
    {"status":200, "text":'{"comments":[]}', "url":"https://www.tiktok.com/api/comment/list/?X-Gnarly=y"},
])
async def test_comment_get_retries_initial_empty_or_unsigned_response_once(monkeypatch, first):
    second = {"status":200, "text":'{"comments":[]}', "url":"https://www.tiktok.com/api/comment/list/?X-Bogus=x&X-Gnarly=y"}
    page = api_page(first)
    original = page.evaluate.side_effect
    responses = iter([first, second])

    async def evaluate(script, arg=None, **kwargs):
        return await original(script, arg, **kwargs) if arg is None else next(responses)

    page.evaluate.side_effect = evaluate
    delay = AsyncMock()
    monkeypatch.setattr(api.asyncio, "sleep", delay)
    assert await api.fetch_comment_page(page, "123", cursor=50) == {"comments":[]}
    delay.assert_awaited_once_with(2.0)
    calls = [call for call in page.evaluate.await_args_list if len(call.args) > 1]
    assert len(calls) == 2 and calls[0].args[1] == calls[1].args[1]
    assert page._comment_list_http_meta["attempts"] == 2
    assert page._comment_list_http_meta["retry_count"] == 1


async def test_comment_get_persistent_empty_body_stops_after_two_retries(monkeypatch):
    page = api_page({"status":200, "text":"", "url":"https://www.tiktok.com/api/comment/list/"})
    delay = AsyncMock()
    monkeypatch.setattr(api.asyncio, "sleep", delay)
    assert await api.fetch_comment_page(page, "123") is None
    assert delay.await_count == 2
    assert [call.args for call in delay.await_args_list] == [(2.0,), (2.0,)]
    assert page.evaluate.await_count == 4  # Runtime read plus exactly three GETs.
    assert page._comment_list_http_meta["reason"] == "empty_body"
    assert page._comment_list_http_meta["retry_count"] == 2


@pytest.mark.parametrize("status", [401,403,429,500])
async def test_comment_get_does_not_retry_http_errors(monkeypatch, status):
    page = api_page({"status":status, "text":"", "url":"https://www.tiktok.com/api/comment/list/"})
    delay = AsyncMock()
    monkeypatch.setattr(api.asyncio, "sleep", delay)
    assert await api.fetch_comment_page(page, "123") is None
    delay.assert_not_awaited()
    assert page.evaluate.await_count == 2


def row(text="hello world", author="demo", cid=CID):
    return {"cid": cid, "text": text, "user": {"unique_id": author, "nickname": "Demo Nick"}}


@pytest.mark.parametrize("author", ["@demo", "DEMO", "Demo Nick"])
async def test_paginated_phrase_search_requires_requested_author(monkeypatch, author):
    fetch = AsyncMock(side_effect=[
        {"comments": [{**row(author="wrong"), "user": {"unique_id": "wrong", "nickname": "Other Nick"}}], "cursor": 50, "has_more": 1},
        {"comments": [row(text="Well, HELLO 🔥 world today")], "cursor": 100, "has_more": 0},
    ])
    monkeypatch.setattr(api, "fetch_comment_page", fetch)
    matches, info = await api.find_parent_comments(None, "video", [SimpleNamespace(text="hello world", tiktok_username=author)], phrase_match=True)
    assert matches[0]["cid"] == CID
    assert info == {"available": True, "scanned": 2, "exhausted": True}
    assert [call.args[2] for call in fetch.await_args_list] == [0, 50]


@pytest.mark.parametrize("bad", [row(cid="fake"), row(cid="123"), row(text="well hello world"), {**row(), "reply_id": "123"}])
async def test_exact_search_rejects_wrong_or_invalid_parents(monkeypatch, bad):
    monkeypatch.setattr(api, "fetch_comment_page", AsyncMock(return_value={"comments": [bad], "has_more": 0}))
    matches, info = await api.find_parent_comments(None, "video", [SimpleNamespace(text="hello world", tiktok_username="demo")])
    assert not matches and info["available"] and info["exhausted"]


@pytest.mark.parametrize("max_pages,expected", [(None, 10), (1, 1), (15, 15), (100, 20)])
async def test_api_search_respects_requested_pages_up_to_twenty(monkeypatch, max_pages, expected):
    fetch = AsyncMock(side_effect=[{"comments": [], "has_more": 1, "cursor": x} for x in range(1, 22)])
    monkeypatch.setattr(api, "fetch_comment_page", fetch)
    _, info = await api.find_parent_comments(None, "video", [SimpleNamespace(text="hello", tiktok_username=None)],
                                             **({"max_pages": max_pages} if max_pages is not None else {}))
    assert fetch.await_count == expected
    assert info["available"] and not info["exhausted"]


@pytest.mark.parametrize("status,body,valid", [
    (200, '{"comments":[]}', True),
    (200, '{"status_code":0,"comments":null}', True),
    (200, '{"status_code":0,"data":{"comments":null}}', True),
    (200, '{"status_code":7,"comments":null}', False),
    (200, '{"comments":{}}', False),
    (200, '{}', False), (200, '', False), (403, '{}', False),
])
async def test_fetch_page_requires_valid_json_list_in_main_world(status, body, valid):
    page = api_page({"status": status, "text": body})
    payload = await api.fetch_comment_page(page, "123")
    assert (payload is not None) is valid
    assert page.evaluate.await_args.kwargs["isolated_context"] is False
    assert parse_qs(urlparse(page.evaluate.await_args.args[1]).query)["count"] == ["20"]


@pytest.mark.parametrize("nested", [False, True])
async def test_null_comments_are_available_and_exhausted(nested):
    payload = {"comments": None, "has_more": 0}
    data = {"status_code": 0, "data": payload} if nested else {"status_code": 0, **payload}
    page = api_page({"status": 200, "text": json.dumps(data)})
    matches, info = await api.find_parent_comments(page, "video", [SimpleNamespace(text="hello", tiktok_username=None)])
    assert matches == {}
    assert info == {"available": True, "scanned": 0, "exhausted": True}
    assert page.evaluate.await_count == 2


async def test_comment_list_uses_full_runtime_profile_and_fresh_web_cookies():
    page = api_page({"status": 200, "text": '{"status_code":0,"comments":[]}'})
    page.context.cookies.side_effect = [[
        {"name": "msToken", "value": f"web-{version}", "domain": ".tiktok.com"},
        {"name": "msToken", "value": "ads-token", "domain": "ads.tiktok.com"},
        {"name": "WebIdLastTime", "value": f"time-{version}", "domain": ".tiktok.com"},
        {"name": "odin_tt", "value": "odin-device", "domain": ".tiktok.com"},
    ] for version in (1, 2)]
    for version, cursor in enumerate((0, 50), start=1):
        assert await api.fetch_comment_page(page, "123", cursor) is not None
        query = parse_qs(urlparse(page.evaluate.await_args.args[1]).query)
        assert query["msToken"] == [f"web-{version}"]
        assert query["WebIdLastTime"] == [f"time-{version}"]
        assert query["odinId"] == ["odin-device"]
        assert query["browser_version"] == ["browser-UA"]
        assert query["region"] == ["GB"]
        assert query["tz_name"] == ["Europe/London"]
        assert query["app_language"] == ["en-GB"]
        assert query["cursor"] == [str(cursor)]
        assert "text" not in query and "text_extra" not in query and "X-Bogus" not in query


@pytest.mark.parametrize("status,body,reason", [
    (200, "", "empty_body"), (200, "<html>challenge</html>", "invalid_json"),
    (403, "", "http_status"), (200, '{"status_code":7}', "api_status"),
    (200, '{}', "missing_comments"),
])
async def test_comment_list_diagnostics_explain_failure_without_logging_tokens(caplog, status, body, reason):
    page = api_page({"status": status, "text": body})
    page.context.cookies.return_value = [{"name": "msToken", "value": "secret-token-value", "domain": ".tiktok.com"}]
    assert await api.fetch_comment_page(page, "123") is None
    assert page._comment_list_http_meta["reason"] == reason
    assert page._comment_list_http_meta["http_status"] == status
    assert "secret-token-value" not in caplog.text


async def test_parent_can_be_found_on_tenth_page(monkeypatch):
    fetch = AsyncMock(side_effect=[
        *[{"comments": [], "has_more": 1, "cursor": i} for i in range(1, 10)],
        {"comments": [row()], "has_more": 0},
    ])
    monkeypatch.setattr(api, "fetch_comment_page", fetch)
    matches, info = await api.find_parent_comments(None, "video", [SimpleNamespace(text="hello world", tiktok_username="demo")])
    assert matches[0]["cid"] == CID
    assert fetch.await_count == 10
    assert info["exhausted"] is True


async def test_old_antifraud_records_removed_only_on_loading():
    names = ["_waftokenid", "csrf_session_id", "x-web-secsdk-uid", "perf_feed_cache", "tt_session_tlb_tag", "tt_csrf_token"]
    records = [{"name": name, "value": "old"} for name in [*names, "sessionid", "ttwid"]]
    loaded = normalize_cookies_for_browser(records, drop_stale_antifraud=True)
    assert {c["name"] for c in loaded} == {"sessionid", "ttwid"}
    assert normalize_cookies_for_browser(records) == loaded
    assert {c["name"] for c in normalize_cookies_for_browser(records, drop_stale_antifraud=False)} >= set(names)
    from executor.tiktok_client import parse_cookies
    assert {c["name"] for c in parse_cookies(json.dumps(records))} == {"sessionid", "ttwid"}


@pytest.mark.parametrize("reason", ["dead_session:expired", "visible_login_ui:test", "captcha_challenge:slider", "goto_failed:ERR_HTTP_RESPONSE_CODE_FAILURE", "proxy_edge_access_denied"])
async def test_only_pre_submit_failures_refund_publish_budget(reason):
    assert is_pre_publish_account_failure(reason)
    assert not is_pre_publish_account_failure(reason, publication_attempted=True)
    assert pre_publish_outcomes([SimpleNamespace(error=reason, attempted=True, publication_attempted=False)])
    assert not pre_publish_outcomes([SimpleNamespace(error=reason, attempted=True, publication_attempted=True)])


@pytest.mark.parametrize("use_proxy", [False, True])
async def test_shortlink_direct_resolution_requires_explicit_direct_mode(monkeypatch, use_proxy):
    from executor import engagement_engine
    monkeypatch.setattr(engagement_engine.settings, "USE_PROXY", use_proxy)
    target = "https://www.tiktok.com/@demo/video/7635267329407913230"
    client = SimpleNamespace(head=AsyncMock(return_value=SimpleNamespace(url=target)), get=AsyncMock())
    context = AsyncMock()
    context.__aenter__.return_value = client
    from unittest.mock import Mock
    factory = Mock(return_value=context)
    monkeypatch.setattr(engagement_engine.httpx, "AsyncClient", factory)
    original = "https://vt.tiktok.com/demo/"
    url, _ = await engagement_engine.resolve_video_url(original)
    assert url == (original if use_proxy else target)
    assert factory.call_count == int(not use_proxy)
    if not use_proxy:
        assert factory.call_args.kwargs["proxy"] is None
        assert factory.call_args.kwargs["trust_env"] is False


@pytest.mark.parametrize("dynamic", [True, False])
@pytest.mark.parametrize("eventual_success", [True, False])
@pytest.mark.parametrize("reason", ["dead_session:visible_login_ui", "warmup_exception", "parent_api_unavailable:empty_body"])
async def test_main_replaces_dead_accounts_within_separate_account_limit(monkeypatch, dynamic, eventual_success, reason):
    from contextlib import contextmanager
    from unittest.mock import Mock
    from db import database
    from executor import browser_pool, user_schema

    @contextmanager
    def context():
        yield SimpleNamespace()

    selected = []

    def select(*args, **kwargs):
        account = SimpleNamespace(id=len(selected) + 1, proxy=None)
        selected.append(account)
        return account

    async def publish(db, account, url, texts, **kwargs):
        if account.id < 3 or not eventual_success:
            return [], reason
        return [user_schema.PublishedMain(text, str(int(CID) + i), True, False, "http_user_schema", account.id, None)
                for i, text in enumerate(texts)], None

    monkeypatch.setattr(database, "get_sync_db_context", context)
    monkeypatch.setattr(user_schema, "get_free_account_for_main_comment", select)
    monkeypatch.setattr(user_schema, "_try_main_account", publish)
    monkeypatch.setattr(user_schema, "_record_main_retry_failure", Mock())
    monkeypatch.setattr(user_schema, "release_main_account_reservation", Mock())
    monkeypatch.setattr(user_schema, "extend_main_account_reservation", lambda *a, **k: True)
    monkeypatch.setattr(user_schema.random, "uniform", lambda *a: 0)
    monkeypatch.setattr(browser_pool, "ensure_thread_browser_pool_capacity", lambda *a: None)
    monkeypatch.setattr(browser_pool, "close_browser_pool", AsyncMock())
    for name, value in {"USE_PROXY": False, "MAX_RETRY_ACCOUNTS": 3, "PARALLEL_MAIN_ACCOUNT_SLOTS": 1,
                        "PARALLEL_COMMENTING_MAX_MAIN_ATTEMPTS": 1, "PARALLEL_DYNAMIC_WORKER_POOL_ENABLED": dynamic}.items():
        monkeypatch.setattr(user_schema._settings, name, value)
    result = await user_schema.execute_user_schema_parallel(
        None, SimpleNamespace(settings={"replies": []}), "https://www.tiktok.com/@demo/video/7635267329407913230",
        ["first", "second", "third"], 123, boost_enabled=False, boost_quantity=0,
    )
    assert len(selected) == 3
    assert len(result.published_mains) == (3 if eventual_success else 0)
    assert (result.error is None) is eventual_success


@pytest.mark.parametrize("explicit", [False, True])
async def test_request_profile_preserves_device_cookie_aliases(explicit):
    from executor.http.request_profile import RequestProfile
    cookies = {"s_v_web_id": "fallback-fp", "odin_tt": "fallback-odin"}
    if explicit:
        cookies.update(verifyFp="explicit-fp", odinId="explicit-odin")
    params = RequestProfile.publish_reply_profile().build_query_params({}, cookies)
    assert params["verifyFp"] == ("explicit-fp" if explicit else "fallback-fp")
    assert params["odinId"] == ("explicit-odin" if explicit else "fallback-odin")
