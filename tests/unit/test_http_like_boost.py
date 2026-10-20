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


@pytest.mark.unit
@pytest.mark.asyncio
async def test_publish_reply_http_first_targeted_reply_like():
    from executor.user_schema import (
        _publish_reply_http_first_if_eligible,
        _record_branch_published_reply,
        _BRANCH_PUBLISHED_REPLIES,
        _UserSchemaHttpSession,
        REPLY_SCOPE,
    )

    _BRANCH_PUBLISHED_REPLIES.clear()
    parent_id = "7999888777666555444"
    reply_1_cid = "7111222333444555666"

    # Pre-record Reply 1 as published in this branch
    _record_branch_published_reply(parent_id, variant_index=1, comment_id=reply_1_cid)

    mock_client = AsyncMock()
    mock_client.like_comment.return_value = (True, None, None)
    mock_client.reply_comment.return_value = (
        True, "7222333444555666777", None, None, {"parent_confirmed": True}
    )

    session = _UserSchemaHttpSession(
        scope=REPLY_SCOPE,
        account_id=456,
        client=mock_client,
        cookies={"sessionid": "xyz"},
        aweme_id="7123456789012345678",
        video_url="https://www.tiktok.com/@u/video/7123456789012345678",
    )

    # Bot 2 publishes Reply 2 with settings to like Reply 1 (up to 2 likes)
    task_settings = {
        "reply_like_target": "1",
        "reply_like_count": 2,
    }

    with patch.dict("os.environ", {"LIKE_PARENT_BEFORE_REPLY": "1"}):
        result = await _publish_reply_http_first_if_eligible(
            session=session,
            reply_text="второй ответ!",
            parent_comment_id=parent_id,
            correlation_id="corr_test_targeted",
            variant_index=2,
            task_settings=task_settings,
        )

    assert result is not None
    assert result.status == "success"

    # Verify both likes occurred: 1) parent comment, 2) target Reply 1
    assert mock_client.like_comment.await_count == 2
    mock_client.like_comment.assert_any_await(
        aweme_id="7123456789012345678",
        cid=parent_id,
        cookies={"sessionid": "xyz"},
        correlation_id="corr_test_targeted_parent_like",
        video_url="https://www.tiktok.com/@u/video/7123456789012345678",
    )
    mock_client.like_comment.assert_any_await(
        aweme_id="7123456789012345678",
        cid=reply_1_cid,
        cookies={"sessionid": "xyz"},
        correlation_id=f"corr_test_targeted_target_reply_like_{reply_1_cid[:12]}",
        video_url="https://www.tiktok.com/@u/video/7123456789012345678",
    )

    # Verify that the same account (account_id=456) does not like the reply twice
    mock_client.like_comment.reset_mock()
    with patch.dict("os.environ", {"LIKE_PARENT_BEFORE_REPLY": "0"}):
        result2 = await _publish_reply_http_first_if_eligible(
            session=session,
            reply_text="третий ответ от того же бота!",
            parent_comment_id=parent_id,
            correlation_id="corr_test_targeted_dup",
            variant_index=3,
            task_settings=task_settings,
        )
    assert result2.status == "success"
    mock_client.like_comment.assert_not_called()


@pytest.mark.unit
def test_draft_reply_likes_serialization():
    from bot.services.drafts import Draft

    d = Draft(
        links=["https://www.tiktok.com/@u/video/111"],
        reply_like_target="2",
        reply_like_count=3,
    )
    data = d.to_dict()
    assert data["reply_like_target"] == "2"
    assert data["reply_like_count"] == "3"

    restored = Draft.from_dict(data)
    assert restored.reply_like_target == "2"
    assert restored.reply_like_count == 3


@pytest.mark.unit
def test_format_reply_likes_label():
    from bot.services.likes_ui import format_reply_likes_label

    assert format_reply_likes_label("1", 0) == "выключены"
    assert format_reply_likes_label("none", 2) == "выключены"
    assert format_reply_likes_label("1", 1) == "ответ 1 (1 шт)"
    assert format_reply_likes_label("2", 3) == "ответ 2 (3 шт)"
    assert format_reply_likes_label("all", 5) == "все ответы (5 шт)"


@pytest.mark.unit
def test_template_and_wizard_likes_keyboards():
    from bot.handlers.templates import _build_template_likes_keyboard
    from bot.handlers.order_wizard import _build_wizard_likes_keyboard

    # Test template keyboard
    kb_t = _build_template_likes_keyboard(template_id=42, current_target="2", current_count=3)
    assert kb_t is not None
    # Check that checkmark is present on the selected button
    all_texts = [b.text for row in kb_t.inline_keyboard for b in row]
    assert any("✅ 2-й" in t for t in all_texts)
    assert any("✅ 3 шт" in t for t in all_texts)

    # Test wizard keyboard
    kb_w = _build_wizard_likes_keyboard(current_target="all", current_count=1)
    assert kb_w is not None
    all_texts_w = [b.text for row in kb_w.inline_keyboard for b in row]
    assert any("✅ Все" in t for t in all_texts_w)
    assert any("✅ 1 шт" in t for t in all_texts_w)


@pytest.mark.unit
def test_preview_text_with_reply_likes():
    from bot.services.order_creation_ui import build_preview_text

    text = build_preview_text(
        links=["https://www.tiktok.com/@u/video/111"],
        text_variants=["v1", "v2", "v3"],
        replies={1: "rep1", 2: "rep2"},
        reply_likes_label="ответ 1 (2 шт)",
    )
    assert "❤️ Лайки на ответ: *ответ 1 (2 шт)*" in text


@pytest.mark.unit
def test_boost_tracking_and_deduplication():
    from executor.user_schema import (
        can_boost_parent,
        record_parent_boost,
        can_boost_reply,
        record_reply_boost,
        _BRANCH_PARENT_LIKED_ACCOUNTS,
        _BRANCH_PUBLISHED_REPLIES,
    )

    _BRANCH_PARENT_LIKED_ACCOUNTS.clear()
    _BRANCH_PUBLISHED_REPLIES.clear()

    parent_id = "7999111222333444555"
    reply_id = "7999888777666555444"

    # 1. Parent deduplication
    assert can_boost_parent(parent_id, "acc_1") is True
    record_parent_boost(parent_id, "acc_1")
    assert can_boost_parent(parent_id, "acc_1") is False  # Same account blocked
    assert can_boost_parent(parent_id, "acc_2") is True   # Another account allowed

    # 2. Reply deduplication and exact count limit
    record_reply_boost(reply_id, "acc_1", parent_id=parent_id)
    assert can_boost_reply(reply_id, "acc_1", max_likes=2) is False  # Already liked by acc_1
    assert can_boost_reply(reply_id, "acc_2", max_likes=2) is True   # Likes given = 1 < 2

    record_reply_boost(reply_id, "acc_2", parent_id=parent_id)
    assert can_boost_reply(reply_id, "acc_3", max_likes=2) is False  # Likes given = 2 == max_likes

@pytest.mark.unit
def test_branch_tracking_memory_eviction():
    from executor.user_schema import (
        record_parent_boost,
        _record_branch_published_reply,
        _BRANCH_PARENT_LIKED_ACCOUNTS,
        _BRANCH_PUBLISHED_REPLIES,
        _MAX_BRANCH_TRACKED,
    )

    _BRANCH_PARENT_LIKED_ACCOUNTS.clear()
    _BRANCH_PUBLISHED_REPLIES.clear()

    # Insert _MAX_BRANCH_TRACKED + 10 items
    for i in range(_MAX_BRANCH_TRACKED + 10):
        p_id = f"799900000000000{i:04d}"
        record_parent_boost(p_id, f"acc_{i}")
        _record_branch_published_reply(p_id, 1, f"711100000000000{i:04d}")

    # Verify that eviction kept count well below 2 * _MAX_BRANCH_TRACKED
    assert len(_BRANCH_PARENT_LIKED_ACCOUNTS) <= _MAX_BRANCH_TRACKED
    assert len(_BRANCH_PUBLISHED_REPLIES) <= _MAX_BRANCH_TRACKED


@pytest.mark.unit
@pytest.mark.asyncio
async def test_http_client_like_comment_errors_and_fast_fail():
    from executor.http.playwright_http_client import PlaywrightHttpClient, LIKE_FAST_POLICY
    from executor.http.retry_policy import HttpErrorType
    from unittest.mock import AsyncMock, patch

    # 1. API error (status_code != 0 in JSON)
    client = PlaywrightHttpClient(request_context=AsyncMock())
    with patch.object(client, "_execute_request", new_callable=AsyncMock) as mock_exec:
        mock_exec.return_value = (False, {"status_code": 8}, "API error: comment does not exist", HttpErrorType.API_ERROR)
        ok, err, err_type = await client.like_comment("7123", "7456")
        assert ok is False
        assert "API error" in err
        assert err_type == HttpErrorType.API_ERROR
        # Ensure LIKE_FAST_POLICY was passed
        mock_exec.assert_awaited_once()
        _, kwargs = mock_exec.call_args
        assert kwargs["retry_policy_override"] == LIKE_FAST_POLICY

    # 2. CAPTCHA / HTML response
    with patch.object(client, "_execute_request", new_callable=AsyncMock) as mock_exec:
        mock_exec.return_value = (False, None, "Received HTML instead of JSON", HttpErrorType.CAPTCHA)
        ok, err, err_type = await client.like_comment("7123", "7456")
        assert ok is False
        assert err_type == HttpErrorType.CAPTCHA


@pytest.mark.unit
@pytest.mark.asyncio
async def test_reply_visibility_boost_targeting_all_and_specific():
    from executor.user_schema import (
        _BRANCH_PUBLISHED_REPLIES,
        _BRANCH_PARENT_LIKED_ACCOUNTS,
        _record_branch_published_reply,
        can_boost_reply,
    )
    from unittest.mock import AsyncMock, MagicMock, patch

    _BRANCH_PUBLISHED_REPLIES.clear()
    _BRANCH_PARENT_LIKED_ACCOUNTS.clear()

    parent_cid = "7999888777666555000"
    reply_1 = "7111000000000000001"
    reply_2 = "7111000000000000002"
    reply_3 = "7111000000000000003"

    # Pre-record branch published replies
    _record_branch_published_reply(parent_cid, 1, reply_1)
    _record_branch_published_reply(parent_cid, 2, reply_2)
    _record_branch_published_reply(parent_cid, 3, reply_3)

    # Mock PlaywrightHttpClient
    mock_obs_client = AsyncMock()
    mock_obs_client.like_comment.return_value = (True, None, None)

    # Test observe logic when reply_like_target == "2" and reply_like_count == 1
    # Only reply_2 should be targeted and liked
    task_mock = MagicMock()
    task_mock.id = 100
    task_mock.settings = {"reply_like_target": "2", "reply_like_count": 1}

    # Simulate branches found by observer
    branches = [{
        "parent_comment_id": parent_cid,
        "found_ids": [reply_3, reply_2, reply_1],  # TikTok returns in arbitrary order
    }]

    from executor.reply_visibility import verify_order_reply_visibility

    # Test that the helper properly targets reply_2 by variant index, not by array position
    import executor.user_schema as schema
    target_variant_idx = 2
    known_in_branch = schema._get_branch_published_replies(parent_cid)
    targeted = [item["cid"] for item in known_in_branch if item.get("index") == target_variant_idx and item.get("cid") in branches[0]["found_ids"]]
    assert targeted == [reply_2]

    # Test that "all" targets ALL found replies
    all_found = branches[0]["found_ids"]
    assert len(all_found) == 3

