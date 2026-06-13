import pytest

from executor.http.playwright_http_client import PlaywrightHttpClient
from executor.http.request_profile import RequestProfile
from executor.http.retry_policy import HttpErrorType, RetryPolicy
from executor.http.signature_runtime import build_unsigned_request_url, wait_for_runtime_signer_ready


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
    def __init__(self, evaluation_result):
        self.evaluation_result = evaluation_result
        self.calls: list[str] = []

    async def evaluate(self, _js_code, url):
        self.calls.append(url)
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

    async def evaluate(self, _js_code):
        self.evaluate_calls += 1
        return self.evaluate_result


@pytest.mark.unit
def test_build_headers_drops_placeholder_secsdk_csrf_when_cookie_missing():
    profile = RequestProfile.publish_main_profile()

    headers = profile.build_headers(cookies={})

    assert "x-secsdk-csrf-token" not in headers


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
    response = _DummyResponse(status=200, body='{"status_code":0,"comment":{"cid":"cid_runtime"}}')
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
        profile=RequestProfile.publish_main_profile(),
        dynamic_params={"aweme_id": "123", "text": "hello"},
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
    assert "X-Bogus=runtime_bogus" in request_context.last_url
    assert "X-Gnarly=runtime_gnarly" in request_context.last_url
    assert "stale_cookie_bogus" not in request_context.last_url
    assert request_context.last_form == {"text": "hello", "text_extra": "[]"}
    assert response_data["__request_signature"]["mode"] == "runtime_signed"
    assert response_data["__request_meta"]["request_attempted"] is True
    assert client.last_request_meta["signature"]["mode"] == "runtime_signed"
    assert client.last_request_meta["request_attempted"] is True
    assert client.last_request_meta["attempt_count"] == 1
    assert client.last_request_meta["response_status"] == 200


@pytest.mark.unit
@pytest.mark.asyncio
async def test_execute_request_accepts_runtime_a_bogus_signature():
    response = _DummyResponse(status=200, body='{"status_code":0,"comment":{"cid":"cid_runtime_a_bogus"}}')
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
        profile=RequestProfile.publish_main_profile(),
        dynamic_params={"aweme_id": "123", "text": "hello"},
        cookies={},
        correlation_id="runtime_a_bogus_test",
    )

    assert success is True
    assert error_msg is None
    assert error_type is None
    assert "a_bogus=runtime_a_bogus" in request_context.last_url
    assert request_context.last_form == {"text": "hello", "text_extra": "[]"}
    assert response_data["__request_signature"]["mode"] == "runtime_signed"
    assert response_data["__request_signature"]["has_a_bogus"] is True
    assert response_data["__request_meta"]["request_attempted"] is True


@pytest.mark.unit
@pytest.mark.asyncio
async def test_execute_request_blocks_sensitive_publish_when_page_signer_has_no_output():
    response = _DummyResponse(status=200, body='{"status_code":0,"comment":{"cid":"cid_unsigned"}}')
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

    assert success is False
    assert response_data is None
    assert error_type == HttpErrorType.API_ERROR
    assert error_msg is not None
    assert "blocked" in error_msg.lower()
    assert request_context.calls == 0
    assert client.last_request_meta["signature"]["mode"] == "unsigned"
    assert client.last_request_meta["signature"]["signer_available"] is True
    assert client.last_request_meta["request_attempted"] is False
    assert client.last_request_meta["attempt_count"] == 0


@pytest.mark.unit
@pytest.mark.asyncio
async def test_execute_request_blocks_unsigned_publish_on_page_path():
    response = _DummyResponse(status=200, body='{"status_code":0,"comment":{"cid":"cid_should_not_send"}}')
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

    assert success is False
    assert response_data is None
    assert error_type == HttpErrorType.API_ERROR
    assert error_msg is not None
    assert "blocked" in error_msg.lower()
    assert request_context.calls == 0
    assert client.last_request_meta["signature"]["mode"] == "unsigned"
    assert client.last_request_meta["request_attempted"] is False
    assert client.last_request_meta["attempt_count"] == 0


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
    assert request_context.calls == 1
    assert response_data["__request_signature"]["mode"] == "unsigned"
    assert response_data["__request_meta"]["request_attempted"] is True
    assert client.last_request_meta["request_attempted"] is True


@pytest.mark.unit
@pytest.mark.asyncio
async def test_execute_request_can_borrow_cookie_signature_without_page():
    response = _DummyResponse(status=200, body='{"status_code":0,"comment":{"cid":"cid_borrowed"}}')
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
    assert "X-Bogus=cookie_bogus" in request_context.last_url
    assert "X-Gnarly=cookie_gnarly" in request_context.last_url
    assert request_context.last_form == {"text": "hello", "text_extra": "[]"}
    assert response_data["__request_signature"]["mode"] == "borrowed_cookie"


@pytest.mark.unit
@pytest.mark.asyncio
async def test_wait_for_runtime_signer_ready_uses_wait_for_function():
    page = _ReadyPage(wait_result=True, evaluate_result=False)

    ready = await wait_for_runtime_signer_ready(page, timeout_ms=900)

    assert ready is True
    assert page.wait_calls == 1
    assert page.evaluate_calls == 0


@pytest.mark.unit
@pytest.mark.asyncio
async def test_wait_for_runtime_signer_ready_falls_back_to_evaluate():
    page = _ReadyPage(wait_result=False, evaluate_result=True)

    ready = await wait_for_runtime_signer_ready(page, timeout_ms=900)

    assert ready is True
    assert page.wait_calls == 1
    assert page.evaluate_calls == 1


@pytest.mark.unit
@pytest.mark.asyncio
async def test_execute_request_accepts_lowercase_explicit_signature_params():
    response = _DummyResponse(status=200, body='{"status_code":0,"comment":{"cid":"cid_explicit_lower"}}')
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
    response = _DummyResponse(status=200, body='{"status_code":0,"comment":{"cid":"cid_reply_form"}}')
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
