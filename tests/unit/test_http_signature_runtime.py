from unittest.mock import AsyncMock, Mock
from types import SimpleNamespace

import pytest

from executor.http.playwright_http_client import PlaywrightHttpClient
from executor.http.request_profile import RequestProfile
from executor.http.retry_policy import HttpErrorType, RetryPolicy
from executor.http.signature_runtime import build_unsigned_request_url, wait_for_runtime_signer_ready
from executor.http import signature_runtime


class _DummyResponse:
    def __init__(self, status: int, body: str, headers: dict[str, str] | None = None):
        self.status = status
        self._body = body
        self.headers = headers or {"content-type": "application/json"}

    async def text(self) -> str:
        return self._body


class _CaptureRequestContext:
    def __init__(self, response: _DummyResponse):
        self._response = response
        self.last_url = None
        self.last_headers = None
        self.last_form = None
        self.calls = 0

    async def fetch(self, url, *args, **kwargs):
        self.calls += 1
        self.last_url = url
        self.last_headers = kwargs.get("headers")
        self.last_form = kwargs.get("form")
        return self._response


class _SignaturePage:
    def __init__(self, evaluation_result, response=None):
        self.evaluation_result = evaluation_result
        self.context = SimpleNamespace(cookies=AsyncMock(return_value=[]))
        self.calls: list[str] = []
        self.fetch_calls = []
        self.last_request = None
        self.response = response or _DummyResponse(200, '{"status_code":0,"comment":{"cid":"7641761192486195000"}}')

    async def evaluate(self, _js_code, arg, *, isolated_context=True):
        if isinstance(arg, dict):
            assert isolated_context is False
            self.fetch_calls.append(arg)
            self.last_request = arg
            return {"status": self.response.status, "headers": self.response.headers, "text": await self.response.text()}
        self.calls.append(arg)
        return self.evaluation_result


class _ReadyPage:
    def __init__(self, wait_result=True, evaluate_result=False):
        self.wait_result = wait_result
        self.evaluate_result = evaluate_result
        self.wait_calls = 0
        self.evaluate_calls = 0

    async def wait_for_function(self, _js_code, timeout):
        self.wait_calls += 1
        if not self.wait_result:
            raise TimeoutError(f"wait timed out after {timeout}ms")
        return True

    async def evaluate(self, _js_code, *, isolated_context=True):
        assert isolated_context is False
        self.evaluate_calls += 1
        return self.evaluate_result


@pytest.mark.unit
def test_build_headers_drops_placeholder_secsdk_csrf_when_cookie_missing():
    profile = RequestProfile.publish_main_profile()

    headers = profile.build_headers(cookies={})

    assert "x-secsdk-csrf-token" not in headers


@pytest.mark.unit
@pytest.mark.asyncio
@pytest.mark.parametrize("reply", [False, True])
@pytest.mark.parametrize("path", ["video", "photo"])
@pytest.mark.parametrize("host", ["vt.tiktok.com", "vm.tiktok.com"])
async def test_shortlink_publish_uses_resolved_media_referer(reply, path, host):
    from urllib.parse import parse_qs, urlparse
    current_url = f"https://www.tiktok.com/@demo/{path}/7635267329407913230"
    context = _CaptureRequestContext(_DummyResponse(200, '{"status_code":0,"comment":{"cid":"7641761192486195009"}}'))
    client = PlaywrightHttpClient(context, retry_policy=RetryPolicy(max_retries=0), runtime_profile={"current_url": current_url})
    if reply:
        result = await client.reply_comment("7635267329407913230", "7641761192486195000", "hello", video_url=f"https://{host}/fixture/")
    else:
        result = await client.publish_main_comment("7635267329407913230", "hello", video_url=f"https://{host}/fixture/")
    assert result[0]
    query = parse_qs(urlparse(context.last_url).query)
    assert query["referer"] == query["root_referer"] == [current_url]


@pytest.mark.unit
def test_build_unsigned_request_url_ignores_cookie_signatures():
    profile = RequestProfile.publish_main_profile()

    url = build_unsigned_request_url(
        profile,
        {"aweme_id": "123", "text": "hello"},
        cookies={
            "device_id": "device_1",
            "X-Bogus": "stale_bogus",
            "X-Gnarly": "stale_gnarly",
        },
    )

    assert "device_id=device_1" in url
    assert "X-Bogus=" not in url
    assert "X-Gnarly=" not in url
    assert "text=hello" not in url


@pytest.mark.unit
@pytest.mark.asyncio
async def test_execute_request_prefers_runtime_signature_over_cookie_signature():
    response = _DummyResponse(status=200, body='{"status_code":0,"comment":{"cid":"7641761192486195002"}}')
    request_context = _CaptureRequestContext(response)
    page = _SignaturePage(
        {
            "x_bogus": "runtime_bogus",
            "x_gnarly": "runtime_gnarly",
            "signer_available": True,
            "source": "window.byted_acrawler",
        }
    )
    client = PlaywrightHttpClient(
        request_context,
        retry_policy=RetryPolicy(max_retries=0),
        page=page,
    )

    success, response_data, error_msg, error_type = await client._execute_request(
        profile=RequestProfile.like_profile(),
        dynamic_params={"aweme_id": "123", "cid": "456"},
        cookies={
            "X-Bogus": "stale_cookie_bogus",
            "X-Gnarly": "stale_cookie_gnarly",
            "tt_csrf_token": "csrf_cookie_value",
        },
        correlation_id="runtime_signature_test",
    )

    assert success is True
    assert error_msg is None
    assert error_type is None
    assert page.calls, "runtime signer should receive unsigned URL"
    assert "stale_cookie_bogus" not in page.calls[0]
    assert "stale_cookie_gnarly" not in page.calls[0]
    assert "X-Bogus=runtime_bogus" in page.last_request["url"]
    assert "X-Gnarly=runtime_gnarly" in page.last_request["url"]
    assert "stale_cookie_bogus" not in page.last_request["url"]
    assert page.last_request["form_data"] == {"aweme_id": "123", "cid": "456", "digg_type": "1"}
    assert request_context.calls == 0
    assert response_data["__request_signature"]["mode"] == "runtime_signed"
    assert response_data["__request_meta"]["request_attempted"] is True
    assert client.last_request_meta["signature"]["mode"] == "runtime_signed"
    assert client.last_request_meta["request_attempted"] is True
    assert client.last_request_meta["attempt_count"] == 1
    assert client.last_request_meta["response_status"] == 200


@pytest.mark.unit
@pytest.mark.asyncio
async def test_execute_request_accepts_runtime_a_bogus_signature():
    response = _DummyResponse(status=200, body='{"status_code":0,"comment":{"cid":"7641761192486195001"}}')
    request_context = _CaptureRequestContext(response)
    page = _SignaturePage(
        {
            "x_bogus": None,
            "x_gnarly": None,
            "a_bogus": "runtime_a_bogus",
            "signer_available": True,
            "source": "window.byted_acrawler",
        }
    )
    client = PlaywrightHttpClient(
        request_context,
        retry_policy=RetryPolicy(max_retries=0),
        page=page,
    )

    success, response_data, error_msg, error_type = await client._execute_request(
        profile=RequestProfile.like_profile(),
        dynamic_params={"aweme_id": "123", "cid": "456"},
        cookies={},
        correlation_id="runtime_a_bogus_test",
    )

    assert success is True
    assert error_msg is None
    assert error_type is None
    assert "a_bogus=runtime_a_bogus" in page.last_request["url"]
    assert page.last_request["form_data"] == {"aweme_id": "123", "cid": "456", "digg_type": "1"}
    assert request_context.calls == 0
    assert response_data["__request_signature"]["mode"] == "runtime_signed"
    assert response_data["__request_signature"]["has_a_bogus"] is True
    assert response_data["__request_meta"]["request_attempted"] is True


@pytest.mark.unit
@pytest.mark.asyncio
async def test_execute_request_delegates_publish_signature_to_page_interceptor():
    response = _DummyResponse(status=200, body='{"status_code":0,"comment":{"cid":"7641761192486195003"}}')
    request_context = _CaptureRequestContext(response)
    page = _SignaturePage(
        {
            "x_bogus": None,
            "x_gnarly": None,
            "signer_available": True,
            "source": "window.byted_acrawler",
        }
    )
    client = PlaywrightHttpClient(
        request_context,
        retry_policy=RetryPolicy(max_retries=0),
        page=page,
    )

    success, response_data, error_msg, error_type = await client._execute_request(
        profile=RequestProfile.publish_main_profile(),
        dynamic_params={"aweme_id": "123", "text": "hello"},
        cookies={
            "X-Bogus": "stale_cookie_bogus",
            "X-Gnarly": "stale_cookie_gnarly",
        },
        correlation_id="unsigned_signature_test",
    )

    assert success is True
    assert response_data is not None
    assert error_type is None
    assert error_msg is None
    assert request_context.calls == 0
    assert page.calls == []
    assert "X-Bogus=" not in page.last_request["url"]
    assert "X-Gnarly=" not in page.last_request["url"]
    assert page.last_request["form_data"] == {"text": "hello", "text_extra": "[]"}
    assert client.last_request_meta["signature"]["mode"] == "browser_fetch"
    assert client.last_request_meta["transport"] == "window.fetch"
    assert client.last_request_meta["request_attempted"] is True
    assert client.last_request_meta["attempt_count"] == 1


@pytest.mark.unit
@pytest.mark.asyncio
async def test_execute_request_allows_publish_without_precomputed_signature():
    response = _DummyResponse(status=200, body='{"status_code":0,"comment":{"cid":"7641761192486195004"}}')
    request_context = _CaptureRequestContext(response)
    page = _SignaturePage(
        {
            "x_bogus": None,
            "x_gnarly": None,
            "a_bogus": None,
            "signer_available": True,
            "source": "window.byted_acrawler",
        }
    )
    client = PlaywrightHttpClient(
        request_context,
        retry_policy=RetryPolicy(max_retries=0),
        page=page,
    )

    success, response_data, error_msg, error_type = await client._execute_request(
        profile=RequestProfile.publish_main_profile(),
        dynamic_params={"aweme_id": "123", "text": "hello"},
        cookies={},
        correlation_id="blocked_unsigned_publish_test",
    )

    assert success is True
    assert response_data is not None
    assert error_type is None
    assert error_msg is None
    assert request_context.calls == 0
    assert len(page.fetch_calls) == 1
    assert client.last_request_meta["signature"]["mode"] == "browser_fetch"
    assert client.last_request_meta["request_attempted"] is True
    assert client.last_request_meta["attempt_count"] == 1


@pytest.mark.unit
@pytest.mark.asyncio
async def test_execute_request_allows_unsigned_like_on_page_path():
    response = _DummyResponse(status=200, body='{"status_code":0}')
    request_context = _CaptureRequestContext(response)
    page = _SignaturePage(
        {
            "x_bogus": None,
            "x_gnarly": None,
            "a_bogus": None,
            "signer_available": True,
            "source": "window.byted_acrawler",
        }
    )
    client = PlaywrightHttpClient(
        request_context,
        retry_policy=RetryPolicy(max_retries=0),
        page=page,
    )

    success, response_data, error_msg, error_type = await client._execute_request(
        profile=RequestProfile.like_profile(),
        dynamic_params={"aweme_id": "123", "cid": "456", "digg_type": "1"},
        cookies={},
        correlation_id="allowed_unsigned_like_test",
    )

    assert success is True
    assert response_data is not None
    assert error_msg is None
    assert error_type is None
    assert request_context.calls == 0
    assert len(page.fetch_calls) == 1
    assert response_data["__request_signature"]["mode"] == "unsigned"
    assert response_data["__request_meta"]["request_attempted"] is True
    assert client.last_request_meta["request_attempted"] is True


@pytest.mark.unit
@pytest.mark.asyncio
async def test_execute_request_publish_without_page_does_not_borrow_cookie_signature():
    response = _DummyResponse(status=200, body='{"status_code":0,"comment":{"cid":"7641761192486195005"}}')
    request_context = _CaptureRequestContext(response)
    client = PlaywrightHttpClient(
        request_context,
        retry_policy=RetryPolicy(max_retries=0),
        page=None,
    )

    success, response_data, error_msg, error_type = await client._execute_request(
        profile=RequestProfile.publish_main_profile(),
        dynamic_params={"aweme_id": "123", "text": "hello"},
        cookies={
            "X-Bogus": "cookie_bogus",
            "X-Gnarly": "cookie_gnarly",
        },
        correlation_id="borrowed_signature_test",
    )

    assert success is True
    assert error_msg is None
    assert error_type is None
    assert "X-Bogus=" not in request_context.last_url
    assert "X-Gnarly=" not in request_context.last_url
    assert request_context.last_form == {"text": "hello", "text_extra": "[]"}
    assert request_context.calls == 1
    assert response_data["__request_signature"]["mode"] == "unsigned"
    assert client.last_request_meta["transport"] == "request_context.fetch"


@pytest.mark.unit
@pytest.mark.asyncio
async def test_wait_for_runtime_signer_ready_checks_main_world():
    page = _ReadyPage(wait_result=True, evaluate_result=True)

    ready = await wait_for_runtime_signer_ready(page, timeout_ms=900)

    assert ready is True
    assert page.wait_calls == 0
    assert page.evaluate_calls == 1


@pytest.mark.unit
@pytest.mark.asyncio
async def test_wait_for_runtime_signer_ready_does_not_depend_on_wait_for_function():
    page = _ReadyPage(wait_result=False, evaluate_result=True)

    ready = await wait_for_runtime_signer_ready(page, timeout_ms=900)

    assert ready is True
    assert page.wait_calls == 0
    assert page.evaluate_calls == 1


@pytest.mark.unit
@pytest.mark.asyncio
@pytest.mark.parametrize("timeout_ms,initially_ready,ready_after_wake,expected_wakes", [
    (199, False, False, 0), (200, False, False, 1),
    (500, False, True, 1), (500, True, False, 0),
])
async def test_signer_wait_wakes_once_only_after_false_check_and_with_time_budget(
    timeout_ms, initially_ready, ready_after_wake, expected_wakes,
):
    ready, wakes = initially_ready, []

    async def evaluate(script, *, isolated_context=True):
        nonlocal ready
        assert isolated_context is False
        if script == signature_runtime._SIGNER_READY_JS:
            return ready
        wakes.append(script)
        ready = ready_after_wake

    page = SimpleNamespace(evaluate=evaluate)
    result = await wait_for_runtime_signer_ready(page, timeout_ms=timeout_ms)
    assert result is (initially_ready or ready_after_wake)
    assert len(wakes) == expected_wakes


@pytest.mark.unit
@pytest.mark.asyncio
async def test_signer_wake_exception_does_not_repeat_or_report_ready():
    wakes = []

    async def evaluate(script, *, isolated_context=True):
        assert isolated_context is False
        if script == signature_runtime._SIGNER_READY_JS:
            return False
        wakes.append(script)
        raise RuntimeError("Document changed during activity")

    assert await wait_for_runtime_signer_ready(SimpleNamespace(evaluate=evaluate), timeout_ms=250) is False
    assert len(wakes) == 1


@pytest.mark.unit
@pytest.mark.asyncio
async def test_execute_request_accepts_lowercase_explicit_signature_params():
    response = _DummyResponse(status=200, body='{"status_code":0,"comment":{"cid":"7641761192486195006"}}')
    request_context = _CaptureRequestContext(response)
    client = PlaywrightHttpClient(
        request_context,
        retry_policy=RetryPolicy(max_retries=0),
        page=None,
    )

    success, response_data, error_msg, error_type = await client._execute_request(
        profile=RequestProfile.publish_main_profile(),
        dynamic_params={
            "aweme_id": "123",
            "text": "hello",
            "x-bogus": "lower_bogus",
            "x-gnarly": "lower_gnarly",
        },
        cookies={},
        correlation_id="explicit_lower_signature_test",
    )

    assert success is True
    assert error_msg is None
    assert error_type is None
    assert "X-Bogus=lower_bogus" in request_context.last_url
    assert "X-Gnarly=lower_gnarly" in request_context.last_url
    assert request_context.last_form == {"text": "hello", "text_extra": "[]"}
    assert response_data["__request_signature"]["mode"] == "explicit_provided"


@pytest.mark.unit
@pytest.mark.asyncio
async def test_execute_request_reply_uses_form_body_for_reply_payload():
    response = _DummyResponse(status=200, body='{"status_code":0,"comment":{"cid":"7641761192486195007"}}')
    request_context = _CaptureRequestContext(response)
    client = PlaywrightHttpClient(
        request_context,
        retry_policy=RetryPolicy(max_retries=0),
        page=None,
    )

    success, response_data, error_msg, error_type = await client._execute_request(
        profile=RequestProfile.publish_reply_profile(),
        dynamic_params={
            "aweme_id": "123",
            "reply_id": "456",
            "reply_to_reply_id": "789",
            "text": "reply hello",
        },
        cookies={},
        correlation_id="reply_form_test",
    )

    assert success is True
    assert error_msg is None
    assert error_type is None
    assert "text=reply+hello" not in request_context.last_url
    assert request_context.last_form == {
        "reply_id": "456",
        "reply_to_reply_id": "789",
        "text": "reply hello",
        "text_extra": "[]",
    }
    assert response_data["__request_signature"]["has_form_body"] is True


@pytest.mark.unit
@pytest.mark.asyncio
@pytest.mark.parametrize("profile_factory", [RequestProfile.publish_main_profile, RequestProfile.publish_reply_profile])
@pytest.mark.parametrize("page_present", [False, True])
async def test_publish_never_harvests_or_borrows_request_signatures(monkeypatch, profile_factory, page_present):
    runtime = AsyncMock()
    harvest = AsyncMock()
    borrow = Mock()
    monkeypatch.setattr(signature_runtime, "generate_runtime_signature_bundle", runtime)
    monkeypatch.setattr(signature_runtime, "harvest_signature_from_traffic", harvest)
    monkeypatch.setattr(signature_runtime, "borrow_signature_bundle_from_cookies", borrow)

    _, url, bundle = await signature_runtime.resolve_request_signature(
        page=object() if page_present else None,
        profile=profile_factory(), dynamic_params={"aweme_id": "123", "text": "hello"},
        cookies={"X-Bogus": "stale", "a_bogus": "stale"}, runtime_profile={}, allow_cookie_fallback=True,
    )

    assert "X-Bogus=" not in url and "a_bogus=" not in url
    assert bundle.mode == ("browser_fetch" if page_present else "unsigned")
    runtime.assert_not_awaited()
    harvest.assert_not_awaited()
    borrow.assert_not_called()


@pytest.mark.unit
@pytest.mark.asyncio
async def test_nonpublish_cookie_signature_fallback_is_preserved():
    context = _CaptureRequestContext(_DummyResponse(200, '{"status_code":0}'))
    client = PlaywrightHttpClient(context, retry_policy=RetryPolicy(max_retries=0))
    success, response, _, _ = await client._execute_request(
        RequestProfile.like_profile(), {"aweme_id": "123", "cid": "456"}, cookies={"X-Bogus": "cookie_signature"},
    )
    assert success is True
    assert "X-Bogus=cookie_signature" in context.last_url
    assert response["__request_signature"]["mode"] == "borrowed_cookie"


@pytest.mark.unit
@pytest.mark.asyncio
@pytest.mark.parametrize("failure, expected", [
    (TimeoutError("Request timeout during window.fetch"), HttpErrorType.TIMEOUT),
    (RuntimeError("Failed to fetch"), HttpErrorType.NETWORK_ERROR),
    (RuntimeError("Execution context was destroyed"), HttpErrorType.NETWORK_ERROR),
])
async def test_browser_transport_failure_does_not_fall_back_to_context(failure, expected):
    context = _CaptureRequestContext(_DummyResponse(200, '{"status_code":0,"comment":{"cid":"7641761192486195008"}}'))
    page = _SignaturePage({})
    page.evaluate = AsyncMock(side_effect=failure)
    client = PlaywrightHttpClient(context, retry_policy=RetryPolicy(max_retries=0), page=page)
    success, response, _, error_type = await client._execute_request(
        RequestProfile.publish_main_profile(), {"aweme_id": "123", "text": "hello"},
    )
    assert success is False
    assert response is None
    assert error_type == expected
    assert context.calls == 0
    page.evaluate.assert_awaited_once()


@pytest.mark.unit
@pytest.mark.asyncio
@pytest.mark.parametrize("status, body, headers, expected", [
    (200, "", {"content-type": "application/json"}, HttpErrorType.FORBIDDEN),
    (200, "invalid", {"content-type": "application/json"}, HttpErrorType.API_ERROR),
    (200, "<html>challenge</html>", {"content-type": "text/html"}, HttpErrorType.CAPTCHA),
    (403, "{}", {"content-type": "application/json"}, HttpErrorType.FORBIDDEN),
    (429, "{}", {"content-type": "application/json"}, HttpErrorType.RATE_LIMIT),
])
async def test_browser_response_keeps_existing_error_classification(status, body, headers, expected):
    context = _CaptureRequestContext(_DummyResponse(200, "{}"))
    page = _SignaturePage({}, response=_DummyResponse(status, body, headers))
    client = PlaywrightHttpClient(context, retry_policy=RetryPolicy(max_retries=0), page=page)
    success, response, _, error_type = await client._execute_request(
        RequestProfile.publish_main_profile(), {"aweme_id": "123", "text": "hello"},
    )
    assert success is False
    assert response is None
    assert error_type == expected
    assert context.calls == 0
    assert client.last_request_meta["response_status"] == status


@pytest.mark.unit
@pytest.mark.asyncio
async def test_browser_transport_preserves_csrf_and_uses_browser_identity_headers():
    page = _SignaturePage({})
    context = _CaptureRequestContext(_DummyResponse(200, "{}"))
    client = PlaywrightHttpClient(context, retry_policy=RetryPolicy(max_retries=0), page=page)
    success, _, _, _ = await client._execute_request(
        RequestProfile.publish_reply_profile(),
        {"aweme_id": "123", "reply_id": "456", "text": "Привет + & 😀"},
        cookies={"tt_csrf_token": "csrf"}, user_agent="manual-user-agent",
    )
    assert success is True
    args = page.last_request
    assert args["form_data"]["text"] == "Привет + & 😀"
    assert args["form_data"]["reply_id"] == "456"
    assert args["headers"]["tt-csrf-token"] == "csrf"
    assert not any(key.lower().startswith("x-secsdk-") for key in args["headers"])
    assert args["headers"]["content-type"] == "application/x-www-form-urlencoded; charset=UTF-8"
    assert not any(name in args["headers"] for name in ("origin", "referer", "cookie", "user-agent"))
    assert not any(name.startswith("sec-") for name in args["headers"])


@pytest.mark.unit
@pytest.mark.asyncio
@pytest.mark.parametrize("factory", [RequestProfile.publish_main_profile, RequestProfile.publish_reply_profile])
@pytest.mark.parametrize("explicit_token", [None, "explicit-token"])
async def test_native_comment_requests_include_ms_token_and_delegate_sdk_headers(factory, explicit_token):
    from urllib.parse import parse_qs, urlparse
    profile = factory()
    profile.static_headers.update({"X-SecSDK-CSRF-Request": "1", "x-secsdk-debug": "old", "cache-control": "no-cache"})
    page = _SignaturePage({})
    page.context.cookies.return_value = [
        {"name": "msToken", "value": "live-browser-token", "domain": ".tiktok.com"},
        {"name": "tt_csrf_token", "value": "live-csrf", "domain": ".tiktok.com"},
    ]
    context = _CaptureRequestContext(_DummyResponse(200, "{}"))
    client = PlaywrightHttpClient(context, retry_policy=RetryPolicy(max_retries=0), page=page)
    params = {"aweme_id": "123", "text": "hello"}
    if explicit_token is not None:
        params["msToken"] = explicit_token
    result = await client._execute_request(
        profile, params,
        cookies={"msToken": "old-db", "tt_csrf_token": "old-csrf"},
    )
    assert result[0]
    args = page.last_request
    assert parse_qs(urlparse(args["url"]).query)["msToken"] == [explicit_token or "live-browser-token"]
    assert set(args["headers"]) <= {"accept", "accept-language", "tt-csrf-token", "content-type"}
    assert args["headers"]["tt-csrf-token"] == "live-csrf"
    assert page.calls == []  # No separate URL-only signer or harvested traffic.
    assert context.calls == 0


@pytest.mark.unit
@pytest.mark.asyncio
async def test_request_context_without_page_still_uses_cookie_ms_token():
    from urllib.parse import parse_qs, urlparse
    context = _CaptureRequestContext(_DummyResponse(200, '{"status_code":0,"comment":{"cid":"7641761192486195009"}}'))
    client = PlaywrightHttpClient(context, retry_policy=RetryPolicy(max_retries=0))
    result = await client._execute_request(
        RequestProfile.publish_main_profile(), {"aweme_id": "123", "text": "hello"},
        cookies={"msToken": "direct-token", "tt_csrf_token": "csrf"},
    )
    assert result[0]
    assert parse_qs(urlparse(context.last_url).query)["msToken"] == ["direct-token"]
    assert context.last_headers["tt-csrf-token"] == "csrf"
    assert not any(key.lower().startswith("x-secsdk-") for key in context.last_headers)


@pytest.mark.unit
@pytest.mark.asyncio
@pytest.mark.parametrize("factory", [RequestProfile.comment_list_profile, RequestProfile.reply_list_profile])
async def test_native_list_signature_resolution_includes_cookie_ms_token(factory):
    from urllib.parse import parse_qs, urlparse
    page = _SignaturePage({})
    _, url, bundle = await signature_runtime.resolve_request_signature(
        page=page, profile=factory(),
        dynamic_params={"aweme_id": "123"},
        cookies={"msToken": "cookie-token"}, runtime_profile={}, allow_cookie_fallback=False,
    )
    assert parse_qs(urlparse(url).query)["msToken"] == ["cookie-token"]
    assert bundle.mode == "browser_fetch"
    assert bundle.source == "window.fetch"
    assert page.calls == []
