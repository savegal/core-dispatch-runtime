import asyncio
from unittest.mock import AsyncMock, patch, MagicMock
import pytest

from executor.http.playwright_http_client import PlaywrightHttpClient
from executor.http.request_profile import RequestProfile


@pytest.mark.unit
@pytest.mark.asyncio
async def test_http_client_like_comment_success():
    mock_request_context = AsyncMock()
    mock_page = AsyncMock()
    client = PlaywrightHttpClient(request_context=mock_request_context, page=mock_page)

    captured_params = {}
    async def fake_execute_request(profile, dynamic_params, **kwargs):
        captured_params.update(dynamic_params)
        return True, {"status_code": 0, "status_msg": "ok"}, None, None

    client._execute_request = fake_execute_request

    success, err_msg, err_type = await client.like_comment(
        aweme_id="7123456789012345678",
        cid="7999888777666555444",
        video_url="https://www.tiktok.com/@user/video/7123456789012345678",
        digg_type=1,
    )

    assert success is True
    assert err_msg is None
    assert captured_params["aweme_id"] == "7123456789012345678"
    assert captured_params["cid"] == "7999888777666555444"
    assert captured_params["digg_type"] == "1"
    assert "referer" in captured_params


@pytest.mark.unit
@pytest.mark.asyncio
async def test_publish_reply_http_first_likes_parent_before_reply():
    from executor.user_schema import _publish_reply_http_first_if_eligible, _UserSchemaHttpSession, REPLY_SCOPE

    mock_client = AsyncMock()
    mock_client.like_comment.return_value = (True, None, None)
    mock_client.reply_comment.return_value = (
        True, "7888999000111222333", None, None, {"parent_confirmed": True}
    )

    session = _UserSchemaHttpSession(
        scope=REPLY_SCOPE,
        account_id=123,
        client=mock_client,
        cookies={"sessionid": "abc"},
        aweme_id="7123456789012345678",
        video_url="https://www.tiktok.com/@u/video/7123456789012345678",
    )

    with patch.dict("os.environ", {"LIKE_PARENT_BEFORE_REPLY": "1"}):
        result = await _publish_reply_http_first_if_eligible(
            session=session,
            reply_text="крутой ответ!",
            parent_comment_id="7999888777666555444",
            correlation_id="corr_test_1",
        )

    assert result is not None
    assert result.status == "success"
    # Ensure like_comment was called on the parent comment!
    mock_client.like_comment.assert_awaited_once_with(
        aweme_id="7123456789012345678",
        cid="7999888777666555444",
        cookies={"sessionid": "abc"},
        correlation_id="corr_test_1_parent_like",
        video_url="https://www.tiktok.com/@u/video/7123456789012345678",
    )
    # Ensure reply_comment was called after
    mock_client.reply_comment.assert_awaited_once()


@pytest.mark.unit
@pytest.mark.asyncio
async def test_publish_reply_http_first_skips_like_when_disabled():
    from executor.user_schema import _publish_reply_http_first_if_eligible, _UserSchemaHttpSession, REPLY_SCOPE

    mock_client = AsyncMock()
    mock_client.reply_comment.return_value = (
        True, "7888999000111222333", None, None, {"parent_confirmed": True}
    )

    session = _UserSchemaHttpSession(
        scope=REPLY_SCOPE,
        account_id=123,
        client=mock_client,
        cookies={"sessionid": "abc"},
        aweme_id="7123456789012345678",
        video_url="https://www.tiktok.com/@u/video/7123456789012345678",
    )

    with patch.dict("os.environ", {"LIKE_PARENT_BEFORE_REPLY": "0"}):
        result = await _publish_reply_http_first_if_eligible(
            session=session,
            reply_text="крутой ответ!",
            parent_comment_id="7999888777666555444",
            correlation_id="corr_test_2",
        )

    assert result is not None
    assert result.status == "success"
    mock_client.like_comment.assert_not_called()
    mock_client.reply_comment.assert_awaited_once()
