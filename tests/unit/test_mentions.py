import json
import pytest
from unittest.mock import AsyncMock, patch
from executor.http.mentions import (
    find_mentions,
    utf16_offset,
    build_text_extra,
    resolve_mention_user,
    prepare_mentions,
    extract_response_mentions,
    _cache_get,
    _cache_put,
    _MENTION_CACHE,
    _MAX_CACHE_SIZE,
    _DISK_CACHE_PATH,
)


@pytest.mark.unit
def test_find_mentions_basic():
    text = "Привет @cool_user! Как дела у @friend?"
    mentions = find_mentions(text)
    assert len(mentions) == 2
    assert mentions[0][2] == "cool_user"
    assert mentions[1][2] == "friend"


@pytest.mark.unit
def test_find_mentions_trailing_dot_and_multiple():
    text = "Смотри видео у @dancer. И еще у @singer_1."
    mentions = find_mentions(text)
    assert len(mentions) == 2
    assert mentions[0][2] == "dancer"
    assert mentions[1][2] == "singer_1"
    # Ensure end index strips the dot
    assert text[mentions[0][0]:mentions[0][1]] == "@dancer"
    assert text[mentions[1][0]:mentions[1][1]] == "@singer_1"


@pytest.mark.unit
def test_find_mentions_ignores_emails():
    text = "Пиши на support@tiktok.com или в лс @official_account"
    mentions = find_mentions(text)
    assert len(mentions) == 1
    assert mentions[0][2] == "official_account"


@pytest.mark.unit
def test_utf16_offset_with_emojis():
    # '🔥' is 1 code point in Python, but 2 code units in UTF-16 (surrogate pair)
    text = "🔥 @cooluser"
    py_at_idx = text.find("@")
    assert py_at_idx == 2  # '🔥' (1) + ' ' (1)
    js_offset = utf16_offset(text, py_at_idx)
    assert js_offset == 3  # '🔥' (2 UTF-16 code units) + ' ' (1) = 3


@pytest.mark.unit
def test_utf16_offset_double_emoji_phrase():
    text = "🔥🔥 Смотри у @planar15"
    py_at_idx = text.find("@")
    assert py_at_idx == 12
    js_offset = utf16_offset(text, py_at_idx)
    # 2*2 (emojis) + 1 (space) + 6 ('Смотри') + 1 (space) + 1 ('у') + 1 (space) = 14
    assert js_offset == 14


@pytest.mark.unit
def test_build_text_extra():
    text = "привет @cooluser"
    resolved = {
        "cooluser": {
            "user_id": "7123456789012345678",
            "sec_uid": "MS4wLjABAAAA...",
            "unique_id": "cooluser",
        }
    }
    entries = build_text_extra(text, resolved)
    assert len(entries) == 1
    entry = entries[0]
    assert entry["type"] == 0
    assert entry["user_id"] == "7123456789012345678"
    assert entry["sec_uid"] == "MS4wLjABAAAA..."
    assert entry["start"] == 7
    assert entry["end"] == 16
    assert text[entry["start"]:entry["end"]] == "@cooluser"


@pytest.mark.unit
def test_build_text_extra_skips_empty_sec_uid():
    text = "привет @cooluser"
    resolved = {
        "cooluser": {
            "user_id": "7123456789012345678",
            "sec_uid": "",  # missing sec_uid!
            "unique_id": "cooluser",
        }
    }
    entries = build_text_extra(text, resolved)
    assert len(entries) == 0


@pytest.mark.unit
@pytest.mark.asyncio
async def test_resolve_mention_user_success():
    mock_page = AsyncMock()
    mock_page.evaluate.return_value = {
        "status": "ok",
        "user": {
            "user_id": "7000000000000000001",
            "sec_uid": "MS4wLjABAAAA...",
            "unique_id": "tiktok_user",
            "source": "api_user_detail",
        }
    }

    info = await resolve_mention_user(mock_page, "@tiktok_user")
    assert info is not None
    assert info["user_id"] == "7000000000000000001"
    assert info["sec_uid"] == "MS4wLjABAAAA..."

    # Second call should hit in-memory cache without page.evaluate
    mock_page.evaluate.reset_mock()
    info_cached = await resolve_mention_user(mock_page, "tiktok_user")
    assert info_cached == info
    mock_page.evaluate.assert_not_called()


@pytest.mark.unit
@pytest.mark.asyncio
async def test_resolve_mention_user_transient_error_does_not_negative_cache():
    _MENTION_CACHE.pop("glitch_user", None)
    mock_page = AsyncMock()
    mock_page.evaluate.return_value = {
        "status": "transient_error",
        "reason": "api_empty_body",
    }

    info = await resolve_mention_user(mock_page, "@glitch_user")
    assert info is None

    # Crucial check: glitch_user MUST NOT be in cache!
    hit, cached = _cache_get("glitch_user")
    assert hit is False


@pytest.mark.unit
@pytest.mark.asyncio
async def test_resolve_mention_user_confirmed_not_found_negative_caches():
    _MENTION_CACHE.pop("banned_user", None)
    mock_page = AsyncMock()
    mock_page.evaluate.return_value = {
        "status": "not_found",
        "reason": "api_code_10202",
    }

    info = await resolve_mention_user(mock_page, "@banned_user")
    assert info is None

    # Confirmed not_found SHOULD be negative cached
    hit, cached = _cache_get("banned_user")
    assert hit is True
    assert cached is None


@pytest.mark.unit
@pytest.mark.asyncio
async def test_prepare_mentions_normalizes_casing():
    mock_page = AsyncMock()
    mock_page.evaluate.return_value = {
        "status": "ok",
        "user": {
            "user_id": "7111222333",
            "sec_uid": "sec_abc",
            "unique_id": "planar15",  # lowercase in TikTok
            "source": "api_user_detail",
        }
    }

    # Order input has uppercase '@Planar15'
    text = "видел у него @Planar15 круто"
    norm_text, text_extra_json, meta = await prepare_mentions(mock_page, text)

    assert meta["resolved"] == ["Planar15"]
    assert meta["entries"] == 1
    # Normalized text should have '@planar15'
    assert norm_text == "видел у него @planar15 круто"

    parsed = json.loads(text_extra_json)
    assert len(parsed) == 1
    assert parsed[0]["user_id"] == "7111222333"
    assert parsed[0]["sec_uid"] == "sec_abc"
    assert parsed[0]["start"] == norm_text.find("@planar15")
    assert parsed[0]["end"] == norm_text.find("@planar15") + len("@planar15")


@pytest.mark.unit
def test_extract_response_mentions():
    response = {
        "comment": {
            "text": "hi @user",
            "text_extra": [
                {"type": 0, "user_id": "99999", "start": 3, "end": 8},
                {"type": 1, "hashtag_name": "trend"},
            ]
        }
    }
    uids = extract_response_mentions(response)
    assert uids == ["99999"]


@pytest.mark.unit
def test_extract_response_mentions_nested_data_envelope():
    response = {
        "data": {
            "comment": {
                "text_extra": [
                    {"type": 0, "uid": "55555", "start": 0, "end": 5},
                ]
            }
        }
    }
    uids = extract_response_mentions(response)
    assert uids == ["55555"]


@pytest.mark.unit
@pytest.mark.asyncio
async def test_client_reply_comment_automatically_injects_text_extra():
    from executor.http.playwright_http_client import PlaywrightHttpClient

    mock_page = AsyncMock()
    mock_page.evaluate.return_value = {
        "status": "ok",
        "user": {
            "user_id": "88888888",
            "sec_uid": "sec_888",
            "unique_id": "myfriend",
            "source": "api_user_detail",
        }
    }
    client = PlaywrightHttpClient(request_context=AsyncMock(), page=mock_page)

    captured_dynamic_params = {}
    async def fake_execute_request(profile, dynamic_params, **kwargs):
        captured_dynamic_params.update(dynamic_params)
        return True, {
            "comment": {
                "cid": "7123456789012345678",
                "reply_id": "7123456789099999999",
                "text_extra": [{"type": 0, "user_id": "88888888"}],
            }
        }, None, None

    client._execute_request = fake_execute_request

    success, cid, err, err_type, meta = await client.reply_comment(
        aweme_id="71234567890",
        parent_comment_id="7123456789099999999",
        text="видел у него @myfriend супер",
    )

    assert success is True
    assert "text_extra" in captured_dynamic_params
    extra = json.loads(captured_dynamic_params["text_extra"])
    assert len(extra) == 1
    assert extra[0]["user_id"] == "88888888"
    assert extra[0]["sec_uid"] == "sec_888"
    assert extra[0]["type"] == 0
    assert client.last_request_meta["mentions"]["retained_uids"] == ["88888888"]


@pytest.mark.unit
@pytest.mark.asyncio
async def test_client_publish_aborts_when_mentions_unresolved():
    from executor.http.playwright_http_client import PlaywrightHttpClient

    mock_page = AsyncMock()
    # Simulate failed resolution
    mock_page.evaluate.return_value = {
        "status": "transient_error",
        "reason": "timeout",
    }
    client = PlaywrightHttpClient(request_context=AsyncMock(), page=mock_page)
    _MENTION_CACHE.pop("unknown_user", None)

    executed = False
    async def fake_execute_request(*args, **kwargs):
        nonlocal executed
        executed = True
        return True, {}, None, None

    client._execute_request = fake_execute_request

    success, cid, err, err_type = await client.publish_main_comment(
        aweme_id="71234567890",
        text="видел у него @unknown_user супер",
    )

    # Must abort BEFORE execute_request
    assert success is False
    assert executed is False
    assert "unresolved" in err.lower()
    assert client.last_request_meta.get("request_attempted") is False


@pytest.mark.unit
@pytest.mark.asyncio
async def test_client_publish_main_comment_automatically_injects_text_extra():
    from executor.http.playwright_http_client import PlaywrightHttpClient

    mock_page = AsyncMock()
    mock_page.evaluate.return_value = {
        "status": "ok",
        "user": {
            "user_id": "77777777",
            "sec_uid": "sec_777",
            "unique_id": "creator",
            "source": "api_user_detail",
        }
    }
    client = PlaywrightHttpClient(request_context=AsyncMock(), page=mock_page)

    captured_dynamic_params = {}
    async def fake_execute_request(profile, dynamic_params, **kwargs):
        captured_dynamic_params.update(dynamic_params)
        return True, {
            "data": {
                "comment": {
                    "cid": "7123456789012345678",
                    "text_extra": [{"type": 0, "user_id": "77777777"}],
                }
            }
        }, None, None

    client._execute_request = fake_execute_request

    success, cid, err, err_type = await client.publish_main_comment(
        aweme_id="71234567890",
        text="подпишись на @creator!",
    )

    assert success is True
    assert cid == "7123456789012345678"
    assert "text_extra" in captured_dynamic_params
    extra = json.loads(captured_dynamic_params["text_extra"])
    assert len(extra) == 1
    assert extra[0]["user_id"] == "77777777"
    assert extra[0]["sec_uid"] == "sec_777"
    assert client.last_request_meta["mentions"]["retained_uids"] == ["77777777"]


@pytest.mark.unit
def test_mention_cache_bounded_size():
    _MENTION_CACHE.clear()
    for i in range(_MAX_CACHE_SIZE + 50):
        _cache_put(f"user_{i}", {"user_id": str(i), "sec_uid": f"sec_{i}"})

    assert len(_MENTION_CACHE) < _MAX_CACHE_SIZE + 50
    hit, val = _cache_get(f"user_{_MAX_CACHE_SIZE + 49}")
    assert hit is True
    assert val["user_id"] == str(_MAX_CACHE_SIZE + 49)
    _MENTION_CACHE.clear()
