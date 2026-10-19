from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock
from urllib.parse import parse_qs, urlparse

import pytest

from executor import engagement_engine, user_schema
from executor.http.playwright_http_client import PlaywrightHttpClient
from executor.http.request_profile import RequestProfile
from executor.http.retry_policy import HttpErrorType, RetryPolicy
from executor.publish_action import PublishResult


pytestmark = [pytest.mark.unit, pytest.mark.asyncio]
URL = "https://www.tiktok.com/@demo/video/7635267329407913230"


@pytest.mark.parametrize("second_body,expected", [
    ('{"status_code":0,"comment":{"cid":"7641761192486194454"}}', True),
    ("", False),
])
async def test_empty_publish_response_has_one_bounded_http_retry(monkeypatch, second_body, expected):
    responses = [{"status": 200, "headers": {"content-type": "application/json"}, "text": body,
                  "url": "https://www.tiktok.com/api/comment/publish/?a_bogus=secret&msToken=secret"}
                 for body in ("", second_body)]
    page = SimpleNamespace(url=URL, evaluate=AsyncMock(side_effect=responses),
                           context=SimpleNamespace(cookies=AsyncMock(return_value=[])))
    sleep = AsyncMock()
    monkeypatch.setattr("executor.http.playwright_http_client.asyncio.sleep", sleep)
    request_context = SimpleNamespace(fetch=AsyncMock())
    client = PlaywrightHttpClient(request_context, page=page, retry_policy=RetryPolicy(max_retries=3))

    success, _, _, _ = await client._execute_request(RequestProfile.publish_main_profile(), {"aweme_id": "7635267329407913230", "text": "hello"})

    assert success is expected
    assert page.evaluate.await_count == 2
    sleep.assert_awaited_once_with(2.0)
    request_context.fetch.assert_not_awaited()
    assert client.last_request_meta["attempt_count"] == 2
    assert client.last_request_meta["empty_body_retry_count"] == 1
    assert client.last_request_meta["observed_security_params"] == {
        "X-Bogus": False, "X-Gnarly": False, "a_bogus": True, "msToken": True,
    }


@pytest.mark.parametrize("status,body,content_type,redirect", [
    (403, "", "application/json", False),
    (200, "<html>challenge</html>", "text/html", False),
    (200, "", "application/json", True),
    (200, "{}", "application/json", False),
])
async def test_empty_retry_does_not_apply_to_explicit_rejection_or_redirect(status, body, content_type, redirect):
    page = SimpleNamespace(url="https://www.tiktok.com/login" if redirect else URL,
                           context=SimpleNamespace(cookies=AsyncMock(return_value=[])),
                           evaluate=AsyncMock(return_value={"status": status, "headers": {"content-type": content_type}, "text": body}))
    client = PlaywrightHttpClient(SimpleNamespace(fetch=AsyncMock()), page=page, retry_policy=RetryPolicy(max_retries=3))
    success, _, _, _ = await client._execute_request(RequestProfile.publish_main_profile(), {"text": "hello"})
    assert not success
    page.evaluate.assert_awaited_once()


async def test_retry_disabled_policy_remains_disabled():
    page = SimpleNamespace(url=URL, context=SimpleNamespace(cookies=AsyncMock(return_value=[])),
                           evaluate=AsyncMock(return_value={"status": 200, "headers": {"content-type": "application/json"}, "text": ""}))
    client = PlaywrightHttpClient(SimpleNamespace(fetch=AsyncMock()), page=page, retry_policy=RetryPolicy(max_retries=0))
    success, _, _, _ = await client._execute_request(RequestProfile.publish_main_profile(), {"text": "hello"})
    assert not success
    page.evaluate.assert_awaited_once()


@pytest.mark.parametrize("load_timeout", [False, True])
async def test_warmup_waits_for_load_and_interceptor_before_success(monkeypatch, load_timeout):
    page = SimpleNamespace(url=URL, goto=AsyncMock(), reload=AsyncMock(),
                           context=SimpleNamespace(cookies=AsyncMock(return_value=[
                               {"name": name, "value": name, "domain": ".tiktok.com"}
                               for name in ("sessionid", "ttwid", "tt_csrf_token", "msToken")
                           ])))
    events = []

    async def wait_state(state, **kwargs):
        events.append(state)
        assert kwargs["timeout"] == 5000
        if state == "domcontentloaded" and load_timeout:
            raise TimeoutError("load timed out")

    async def ready(_page, *, timeout_ms):
        events.append("interceptor")
        assert timeout_ms == 200
        return True

    async def comments(_page, aweme_id, *, count):
        events.append("comment_get")
        return {"comments": []}

    page.wait_for_load_state = AsyncMock(side_effect=wait_state)
    monkeypatch.setattr(engagement_engine, "wait_for_runtime_signer_ready", ready)
    monkeypatch.setattr("executor.http.comment_discovery.fetch_comment_page", comments)
    monkeypatch.setattr(engagement_engine.asyncio, "sleep", AsyncMock())
    _, valid, info = await engagement_engine.warmup_page_for_http(page, URL)
    assert valid
    assert events == ["domcontentloaded", "interceptor"]
    assert info["comment_api_ready"] is True
    assert info["comment_api_skipped"] is True
    assert info["load_state"] == ("loading" if load_timeout else "domcontentloaded")


@pytest.mark.parametrize("method,unconfirmed", [("http_user_schema", False), ("http_user_schema:network", False), ("main-dom", True)])
async def test_empty_http_reply_or_main_is_not_counted_as_submitted(method, unconfirmed):
    result = PublishResult(status="failed", error_reason="stealth_reject_empty_body", dom_strategy=method,
                           network_request_attempted=True, post_was_enabled_at_click=True)
    assert user_schema._main_publish_submitted_unconfirmed("task_parallel_main_1", result) is unconfirmed
    assert user_schema._reply_publish_submitted_unconfirmed(result) is False


async def test_empty_http_result_stops_main_series_before_dom(monkeypatch):
    monkeypatch.setattr(user_schema._settings, "TIKTOK_NETWORK_FIRST", True)
    monkeypatch.setattr(user_schema._settings, "TIKTOK_NETWORK_IMPL_READY", True)
    monkeypatch.setattr(user_schema._settings, "PARALLEL_COMMENTING_DOM_FIRST", False)
    client = SimpleNamespace(last_request_meta={"request_attempted": True}, publish_main_comment=AsyncMock(return_value=(
        False, None, "TikTok stealth-reject: empty JSON response", HttpErrorType.FORBIDDEN,
    )))
    session = user_schema._UserSchemaHttpSession(user_schema.MAIN_SCOPE, 1, client, {}, "7635267329407913230", URL)
    monkeypatch.setattr(user_schema, "extract_tiktok_username", AsyncMock(return_value="demo"))
    monkeypatch.setattr(user_schema, "_prepare_user_schema_http_session", AsyncMock(return_value=session))
    monkeypatch.setattr(engagement_engine, "_record_http_account_failure_result", Mock())
    dom = AsyncMock(side_effect=AssertionError("Empty HTTP response must not enable DOM fallback"))
    monkeypatch.setattr(user_schema, "publish_comment_hybrid", dom)
    published, reason = await user_schema._publish_all_variants_on_account(
        None, SimpleNamespace(url=URL), SimpleNamespace(id=1), ["first", "second"],
        correlation_id="task_parallel_main_1", expected_video_url=URL,
    )
    assert published == []
    assert reason == "stealth_reject_empty_body"
    client.publish_main_comment.assert_awaited_once()
    dom.assert_not_awaited()


async def test_live_cookie_rotation_applies_to_retry_and_later_variants(monkeypatch):
    def records(version):
        return [{"name": name, "value": f"{name}-{version}", "domain": ".tiktok.com"}
                for name in ("msToken", "tt_csrf_token", "WebIdLastTime")] + [
                    {"name": "msToken", "value": "ads-token", "domain": "ads.tiktok.com"},
                ]

    success_body = '{"status_code":0,"comment":{"cid":"7641761192486194454"}}'
    responses = [{"status": 200, "headers": {"content-type": "application/json"}, "text": body}
                 for body in ("", success_body, success_body, success_body)]
    cookie_reader = AsyncMock(side_effect=[records(i) for i in range(1, 5)])
    page = SimpleNamespace(url=URL, context=SimpleNamespace(cookies=cookie_reader),
                           evaluate=AsyncMock(side_effect=responses))
    request_context = SimpleNamespace(fetch=AsyncMock())
    client = PlaywrightHttpClient(request_context, page=page, retry_policy=RetryPolicy(max_retries=1))
    monkeypatch.setattr("executor.http.playwright_http_client.asyncio.sleep", AsyncMock())
    original_cookies = {name: "db-old" for name in ("msToken", "tt_csrf_token", "WebIdLastTime")}
    original_cookies["sessionid"] = "session-fallback"

    for text in ("first", "second", "third"):
        result = await client.publish_main_comment("7635267329407913230", text, cookies=original_cookies)
        assert result[0], result[2]

    assert cookie_reader.await_count == 4
    requests = [call.args[1] for call in page.evaluate.await_args_list]
    for version, request in enumerate(requests, start=1):
        query = parse_qs(urlparse(request["url"]).query)
        assert query["msToken"] == [f"msToken-{version}"]
        assert query["WebIdLastTime"] == [f"WebIdLastTime-{version}"]
        assert not any(key.lower().startswith("x-secsdk-") for key in request["headers"])
        assert request["headers"]["tt-csrf-token"] == f"tt_csrf_token-{version}"
        assert "Cookie" not in request["headers"]
    assert [request["form_data"]["text"] for request in requests] == ["first", "first", "second", "third"]
    assert original_cookies["msToken"] == "db-old"
    request_context.fetch.assert_not_awaited()


@pytest.mark.parametrize("failure", ["missing_session", "skipped", "request_failed"])
async def test_main_http_first_stops_without_dom_for_all_http_failures(monkeypatch, failure):
    monkeypatch.setattr(user_schema._settings, "TIKTOK_NETWORK_FIRST", True)
    monkeypatch.setattr(user_schema._settings, "TIKTOK_NETWORK_IMPL_READY", True)
    monkeypatch.setattr(user_schema._settings, "PARALLEL_COMMENTING_DOM_FIRST", False)
    session = None if failure == "missing_session" else SimpleNamespace(disabled_reason="http_unavailable")
    result = PublishResult(status="failed", error_reason="request_failed", network_request_attempted=True)
    http = AsyncMock(return_value=result if failure == "request_failed" else None)
    dom = AsyncMock()
    monkeypatch.setattr(user_schema, "extract_tiktok_username", AsyncMock(return_value="demo"))
    monkeypatch.setattr(user_schema, "_prepare_user_schema_http_session", AsyncMock(return_value=session))
    monkeypatch.setattr(user_schema, "_publish_main_http_first_if_eligible", http)
    monkeypatch.setattr(user_schema, "publish_comment_hybrid", dom)
    page = SimpleNamespace(url=URL)

    published, reason = await user_schema._publish_all_variants_on_account(
        None, page, SimpleNamespace(id=1), ["first", "second", "third"],
        correlation_id="task_http_main", expected_video_url=URL,
    )

    assert published == []
    assert reason == {"missing_session": "http_session_not_ready", "skipped": "http_unavailable",
                      "request_failed": "request_failed"}[failure]
    assert http.await_count == (0 if failure == "missing_session" else 1)
    assert bool(getattr(page, "_publication_attempted", False)) is (failure == "request_failed")
    dom.assert_not_awaited()


async def test_http_failure_after_success_keeps_only_confirmed_variant(monkeypatch):
    monkeypatch.setattr(user_schema._settings, "TIKTOK_NETWORK_FIRST", True)
    monkeypatch.setattr(user_schema._settings, "TIKTOK_NETWORK_IMPL_READY", True)
    monkeypatch.setattr(user_schema._settings, "PARALLEL_COMMENTING_DOM_FIRST", False)
    monkeypatch.setattr(user_schema, "extract_tiktok_username", AsyncMock(return_value="demo"))
    monkeypatch.setattr(user_schema, "_prepare_user_schema_http_session", AsyncMock(return_value=SimpleNamespace(disabled_reason=None)))
    http = AsyncMock(side_effect=[
        PublishResult(status="success", comment_id="7641761192486194454", network_captured=True,
                      network_request_attempted=True, dom_strategy="http_user_schema"),
        PublishResult(status="failed", error_reason="http_error", network_request_attempted=True),
    ])
    dom = AsyncMock()
    callback = Mock()
    monkeypatch.setattr(user_schema, "_publish_main_http_first_if_eligible", http)
    monkeypatch.setattr(user_schema, "publish_comment_hybrid", dom)
    monkeypatch.setattr(user_schema, "_register_runtime_success", Mock())
    monkeypatch.setattr(user_schema, "_deny_if_visible_logged_out", AsyncMock(return_value=None))
    monkeypatch.setattr(user_schema.asyncio, "sleep", AsyncMock())

    published, reason = await user_schema._publish_all_variants_on_account(
        None, SimpleNamespace(url=URL), SimpleNamespace(id=1), ["first", "second", "third"],
        correlation_id="task_http_main", expected_video_url=URL, on_published=callback,
    )

    assert [item.text for item in published] == ["first"]
    assert "main_partial:published=1/3" in reason and "reason=http_error" in reason
    assert http.await_count == 2
    callback.assert_called_once_with(published[0])
    dom.assert_not_awaited()
