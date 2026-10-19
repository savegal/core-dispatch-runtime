import pytest
from types import SimpleNamespace
from unittest.mock import AsyncMock

import executor.http.playwright_http_client as playwright_http_client
from executor.http.playwright_http_client import PlaywrightHttpClient
from executor.http.request_profile import RequestProfile
from executor.http.retry_policy import RetryPolicy, HttpErrorType
import executor.tiktok_client_async as tiktok_client_async


class _DummyResponse:
    def __init__(self, status: int, body: str, headers: dict[str, str] | None = None):
        self.status = status
        self._body = body
        self.headers = headers or {"content-type": "application/json"}

    async def text(self) -> str:
        return self._body


class _DummyRequestContext:
    def __init__(self, response: _DummyResponse):
        self._response = response
        self.calls = 0

    async def fetch(self, *args, **kwargs):
        self.calls += 1
        return self._response


class _TimeoutRequestContext:
    def __init__(self):
        self.calls = 0

    async def fetch(self, *args, **kwargs):
        self.calls += 1
        raise TimeoutError("Proxy connect timed out")


@pytest.mark.unit
@pytest.mark.asyncio
async def test_execute_request_publish_requires_comment_id():
    response = _DummyResponse(status=200, body='{"status_code":0}')
    request_context = _DummyRequestContext(response)
    client = PlaywrightHttpClient(request_context, retry_policy=RetryPolicy(max_retries=0))

    success, _, error_msg, error_type = await client._execute_request(
        profile=RequestProfile.publish_main_profile(),
        dynamic_params={"aweme_id": "123", "text": "hello"},
        cookies={},
        correlation_id="test_publish_no_comment_id",
    )

    assert success is False
    assert error_type == HttpErrorType.API_ERROR
    assert error_msg is not None
    assert "missing confirmation" in error_msg.lower()


@pytest.mark.unit
@pytest.mark.asyncio
async def test_execute_request_publish_does_not_recover_api_receipt_from_dom(monkeypatch):
    response = _DummyResponse(status=200, body='{"status_code":0}')
    request_context = _DummyRequestContext(response)
    client = PlaywrightHttpClient(
        request_context,
        retry_policy=RetryPolicy(max_retries=0),
        page=SimpleNamespace(context=SimpleNamespace(cookies=AsyncMock(return_value=[])), evaluate=AsyncMock(return_value={
            "status": 200, "headers": response.headers, "text": await response.text(),
        })),
    )

    async def _fake_recover(page, text):
        assert page is client.page
        assert text == "hello"
        return "cid_dom_recovered"

    monkeypatch.setattr(
        playwright_http_client,
        "_recover_main_comment_id_via_dom",
        _fake_recover,
    )

    success, response_data, error_msg, error_type = await client._execute_request(
        profile=RequestProfile.publish_main_profile(),
        dynamic_params={"aweme_id": "123", "text": "hello"},
        cookies={},
        correlation_id="test_publish_dom_recovery",
    )

    assert success is False
    assert error_type == HttpErrorType.API_ERROR
    assert error_msg and "missing confirmation" in error_msg
    assert response_data is None


@pytest.mark.unit
@pytest.mark.asyncio
async def test_execute_request_reply_does_not_use_main_comment_dom_recovery(monkeypatch):
    response = _DummyResponse(status=200, body='{"status_code":0}')
    request_context = _DummyRequestContext(response)
    client = PlaywrightHttpClient(
        request_context,
        retry_policy=RetryPolicy(max_retries=0),
        page=SimpleNamespace(context=SimpleNamespace(cookies=AsyncMock(return_value=[])), evaluate=AsyncMock(return_value={
            "status": 200, "headers": response.headers, "text": await response.text(),
        })),
    )

    async def _fake_recover(*args, **kwargs):
        raise AssertionError("main comment DOM recovery should not run for replies")

    monkeypatch.setattr(
        playwright_http_client,
        "_recover_main_comment_id_via_dom",
        _fake_recover,
    )

    success, _, error_msg, error_type = await client._execute_request(
        profile=RequestProfile.publish_reply_profile(),
        dynamic_params={"aweme_id": "123", "reply_id": "456", "text": "reply"},
        cookies={},
        correlation_id="test_reply_no_main_dom_recovery",
    )

    assert success is False
    assert error_type == HttpErrorType.API_ERROR
    assert error_msg is not None
    assert "missing confirmation" in error_msg.lower()


@pytest.mark.unit
@pytest.mark.asyncio
async def test_execute_request_classifies_rate_limit_api_message():
    response = _DummyResponse(
        status=200,
        body='{"status_code":1001,"status_msg":"You are commenting too fast. Try again later."}',
    )
    request_context = _DummyRequestContext(response)
    client = PlaywrightHttpClient(request_context, retry_policy=RetryPolicy(max_retries=0))

    success, _, error_msg, error_type = await client._execute_request(
        profile=RequestProfile.publish_main_profile(),
        dynamic_params={"aweme_id": "123", "text": "hello"},
        cookies={},
        correlation_id="test_api_rate_limit",
    )

    assert success is False
    assert error_type == HttpErrorType.RATE_LIMIT
    assert error_msg is not None
    assert "too fast" in error_msg.lower()


@pytest.mark.unit
@pytest.mark.asyncio
async def test_execute_request_classifies_captcha_api_message():
    response = _DummyResponse(
        status=200,
        body='{"status_code":1002,"status_msg":"Please complete security verification captcha"}',
    )
    request_context = _DummyRequestContext(response)
    client = PlaywrightHttpClient(request_context, retry_policy=RetryPolicy(max_retries=0))

    success, _, error_msg, error_type = await client._execute_request(
        profile=RequestProfile.publish_main_profile(),
        dynamic_params={"aweme_id": "123", "text": "hello"},
        cookies={},
        correlation_id="test_api_captcha",
    )

    assert success is False
    assert error_type == HttpErrorType.CAPTCHA
    assert error_msg is not None
    assert "captcha" in error_msg.lower() or "verification" in error_msg.lower()


@pytest.mark.unit
@pytest.mark.asyncio
async def test_execute_request_short_circuits_local_retries_on_proxy_timeout():
    request_context = _TimeoutRequestContext()
    client = PlaywrightHttpClient(request_context, retry_policy=RetryPolicy(max_retries=3))

    success, _, error_msg, error_type = await client._execute_request(
        profile=RequestProfile.publish_main_profile(),
        dynamic_params={"aweme_id": "123", "text": "hello"},
        cookies={},
        correlation_id="test_proxy_timeout_short_circuit",
    )

    assert success is False
    assert error_type == HttpErrorType.TIMEOUT
    assert error_msg is not None
    assert request_context.calls == 1


@pytest.mark.unit
@pytest.mark.asyncio
async def test_reply_comment_fails_without_comment_id(monkeypatch):
    response = _DummyResponse(status=200, body='{"status_code":0}')
    request_context = _DummyRequestContext(response)
    client = PlaywrightHttpClient(request_context, retry_policy=RetryPolicy(max_retries=0))

    async def _fake_execute_request(*args, **kwargs):
        return True, {"status_code": 0}, None, None

    monkeypatch.setattr(client, "_execute_request", _fake_execute_request)

    success, comment_id, error_msg, error_type, meta = await client.reply_comment(
        aweme_id="123",
        parent_comment_id="456",
        text="reply text",
        cookies={},
        correlation_id="test_reply_without_comment_id",
    )

    assert success is False
    assert comment_id is None
    assert error_type == HttpErrorType.API_ERROR
    assert error_msg is not None
    assert "missing comment_id" in error_msg.lower()
    assert meta["parent_confirmed"] is False
    assert meta["parent_confirmation_level"] == "unconfirmed"


@pytest.mark.unit
@pytest.mark.asyncio
async def test_reply_comment_success_with_nested_comment_id(monkeypatch):
    response = _DummyResponse(status=200, body='{"status_code":0}')
    request_context = _DummyRequestContext(response)
    client = PlaywrightHttpClient(request_context, retry_policy=RetryPolicy(max_retries=0))

    async def _fake_execute_request(*args, **kwargs):
        return True, {"status_code": 0, "data": {"comment": {"cid": "7641761192486196000"}}}, None, None

    monkeypatch.setattr(client, "_execute_request", _fake_execute_request)

    success, comment_id, error_msg, error_type, meta = await client.reply_comment(
        aweme_id="123",
        parent_comment_id="456",
        text="reply text",
        cookies={},
        correlation_id="test_reply_with_comment_id",
    )

    assert success is True
    assert comment_id == "7641761192486196000"
    assert error_msg is None
    assert error_type is None
    assert meta["parent_confirmed"] is False
    assert meta["parent_confirmation_level"] == "unconfirmed"


@pytest.mark.unit
@pytest.mark.asyncio
async def test_reply_comment_accepts_extra_query_params(monkeypatch):
    response = _DummyResponse(status=200, body='{"status_code":0}')
    request_context = _DummyRequestContext(response)
    client = PlaywrightHttpClient(request_context, retry_policy=RetryPolicy(max_retries=0))

    captured_dynamic_params = {}

    async def _fake_execute_request(*args, **kwargs):
        nonlocal captured_dynamic_params
        captured_dynamic_params = kwargs.get("dynamic_params", {})
        return True, {"status_code": 0, "comment": {"cid": "7641761192486196001"}}, None, None

    monkeypatch.setattr(client, "_execute_request", _fake_execute_request)

    success, comment_id, error_msg, error_type, meta = await client.reply_comment(
        aweme_id="123",
        parent_comment_id="456",
        text="reply text",
        cookies={},
        correlation_id="test_reply_extra_params",
        **{"X-Bogus": "bogus_123"}
    )

    assert success is True
    assert comment_id == "7641761192486196001"
    assert error_msg is None
    assert error_type is None
    assert meta["parent_confirmed"] is False
    assert meta["parent_confirmation_level"] == "unconfirmed"
    assert captured_dynamic_params.get("X-Bogus") == "bogus_123"


@pytest.mark.unit
@pytest.mark.asyncio
async def test_reply_comment_sets_network_exact_when_parent_matches(monkeypatch):
    response = _DummyResponse(status=200, body='{"status_code":0}')
    request_context = _DummyRequestContext(response)
    client = PlaywrightHttpClient(request_context, retry_policy=RetryPolicy(max_retries=0))

    async def _fake_execute_request(*args, **kwargs):
        return True, {"status_code": 0, "comment": {"cid": "7641761192486196002", "reply_id": "parent_456"}}, None, None

    monkeypatch.setattr(client, "_execute_request", _fake_execute_request)

    success, comment_id, error_msg, error_type, meta = await client.reply_comment(
        aweme_id="123",
        parent_comment_id="parent_456",
        text="reply text",
        cookies={},
        correlation_id="test_reply_parent_exact",
    )

    assert success is True
    assert comment_id == "7641761192486196002"
    assert error_msg is None
    assert error_type is None
    assert meta["parent_confirmed"] is True
    assert meta["parent_confirmation_level"] == "network_exact"


@pytest.mark.unit
@pytest.mark.asyncio
async def test_reply_comment_sets_network_reply_to_reply_when_reply_target_matches(monkeypatch):
    response = _DummyResponse(status=200, body='{"status_code":0}')
    request_context = _DummyRequestContext(response)
    client = PlaywrightHttpClient(request_context, retry_policy=RetryPolicy(max_retries=0))

    async def _fake_execute_request(*args, **kwargs):
        return True, {
            "status_code": 0,
            "comment": {
                "cid": "7641761192486196003",
                "reply_id": "root_111",
                "reply_to_reply_id": "reply_222",
            },
        }, None, None

    monkeypatch.setattr(client, "_execute_request", _fake_execute_request)

    success, comment_id, error_msg, error_type, meta = await client.reply_comment(
        aweme_id="123",
        parent_comment_id="reply_222",
        text="reply text",
        cookies={},
        correlation_id="test_reply_parent_reply_to_reply",
    )

    assert success is True
    assert comment_id == "7641761192486196003"
    assert error_msg is None
    assert error_type is None
    assert meta["parent_confirmed"] is True
    assert meta["response_parent_id"] == "root_111"
    assert meta["response_reply_to_reply_id"] == "reply_222"
    assert meta["parent_confirmation_level"] == "network_reply_to_reply"


@pytest.mark.unit
@pytest.mark.asyncio
async def test_reply_comment_accepts_network_root_match_for_top_level_parent(monkeypatch):
    response = _DummyResponse(status=200, body='{"status_code":0}')
    request_context = _DummyRequestContext(response)
    client = PlaywrightHttpClient(request_context, retry_policy=RetryPolicy(max_retries=0))

    async def _fake_execute_request(*args, **kwargs):
        return True, {
            "status_code": 0,
            "data": {
                "comment": {
                    "cid": "7641761192486196004",
                    "root_comment_id": "parent_456",
                }
            },
        }, None, None

    monkeypatch.setattr(client, "_execute_request", _fake_execute_request)

    success, comment_id, error_msg, error_type, meta = await client.reply_comment(
        aweme_id="123",
        parent_comment_id="parent_456",
        text="reply text",
        cookies={},
        correlation_id="test_reply_parent_root_match",
    )

    assert success is True
    assert comment_id == "7641761192486196004"
    assert error_msg is None
    assert error_type is None
    assert meta["parent_confirmed"] is True
    assert meta["response_root_comment_id"] == "parent_456"
    assert meta["parent_confirmation_level"] == "network_root_match"


@pytest.mark.unit
@pytest.mark.asyncio
async def test_reply_comment_treats_parent_mismatch_with_comment_id_as_submitted_unconfirmed(monkeypatch):
    response = _DummyResponse(status=200, body='{"status_code":0}')
    request_context = _DummyRequestContext(response)
    client = PlaywrightHttpClient(request_context, retry_policy=RetryPolicy(max_retries=0))

    async def _fake_execute_request(*args, **kwargs):
        return True, {
            "status_code": 0,
            "comment": {
                "cid": "7641761192486196005",
                "reply_id": "other_parent_999",
            },
        }, None, None

    monkeypatch.setattr(client, "_execute_request", _fake_execute_request)

    success, comment_id, error_msg, error_type, meta = await client.reply_comment(
        aweme_id="123",
        parent_comment_id="parent_456",
        text="reply text",
        cookies={},
        correlation_id="test_reply_parent_mismatch",
    )

    assert success is True
    assert comment_id == "7641761192486196005"
    assert error_msg is None
    assert error_type is None
    assert meta["parent_confirmed"] is False
    assert meta["parent_mismatch"] is True
    assert meta["response_parent_id"] == "other_parent_999"
    assert meta["parent_confirmation_level"] == "network_parent_mismatch"


@pytest.mark.unit
@pytest.mark.asyncio
async def test_post_reply_allows_parent_id_without_main_comment_text(monkeypatch):
    async def _raise_create_page(*args, **kwargs):
        raise RuntimeError("sentinel_create_page")

    monkeypatch.setattr(tiktok_client_async, "create_page_for_account", _raise_create_page)

    account = SimpleNamespace(id=1, label="test_account", proxy=None)

    result = await tiktok_client_async.post_reply(
        account=account,
        video_url="https://www.tiktok.com/@demo/video/1234567890",
        main_comment_text="",
        reply_text="test reply",
        task_id=1,
        parent_comment_id="parent_123",
    )

    assert result.success is False
    assert result.details is not None
    assert "sentinel_create_page" in result.details
    assert "Main comment text is required" not in result.details


@pytest.mark.unit
@pytest.mark.asyncio
async def test_post_reply_still_requires_locator_data():
    account = SimpleNamespace(id=1, label="test_account", proxy=None)

    result = await tiktok_client_async.post_reply(
        account=account,
        video_url="https://www.tiktok.com/@demo/video/1234567890",
        main_comment_text="",
        reply_text="test reply",
        task_id=1,
        parent_comment_id=None,
        expected_comment_id=None,
    )

    assert result.success is False
    assert result.details is not None
    assert "unless parent_comment_id/expected_comment_id is provided" in result.details
