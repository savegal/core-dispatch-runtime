import inspect

import pytest

import services.video_parser as video_parser


@pytest.mark.unit
def test_tiktok_body_status_rate_limit_is_detected():
    assert video_parser._is_tiktok_api_rate_limited_payload({"status_code": 8}) is True
    assert video_parser._is_tiktok_api_rate_limited_payload({"statusCode": "20012"}) is True
    assert video_parser._is_tiktok_api_rate_limited_payload({"status_msg": "Too many requests"}) is True
    assert video_parser._is_tiktok_api_rate_limited_payload({"status_code": 0}) is False


@pytest.mark.unit
def test_aweme_id_rejects_short_generic_ids():
    assert video_parser._get_aweme_id_from_item({"id": "1234567890"}) is None
    assert video_parser._get_aweme_id_from_item({"aweme_id": "7638486164327959829"}) == "7638486164327959829"


@pytest.mark.unit
def test_pinned_and_microsecond_create_time_variants():
    assert video_parser._item_is_pinned({"isPinned": True}) is True
    assert video_parser._item_is_pinned({"isTop": "1"}) is True
    assert video_parser._get_create_time_from_item({"createTime": "1778490000000000"}) == 1778490000


@pytest.mark.unit
def test_missing_author_allowed_only_when_payload_owner_is_confirmed():
    username = "demo_creator"
    assert video_parser._payload_mentions_username(
        {"user": {"uniqueId": username}},
        username,
    ) is True
    assert video_parser._payload_mentions_username(
        {"itemList": [{"author": {"uniqueId": username}}]},
        username,
    ) is False
    assert video_parser._author_matches_item(
        {"aweme_id": "7638486164327959829"},
        username,
        allow_missing_author=False,
    ) is False
    assert video_parser._author_matches_item(
        {"aweme_id": "7638486164327959829"},
        username,
        allow_missing_author=True,
    ) is True


@pytest.mark.unit
def test_quick_dead_detection_has_cookie_context_guard():
    source = inspect.getsource(video_parser.quick_detect_dead_account_for_parsing)

    assert "dismiss_cookie_banners" in source
    assert "inCookieContext" in source
    assert "depth < 20" in source
