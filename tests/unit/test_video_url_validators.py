import pytest

from bot.services.validators import parse_links, validate_video_url


@pytest.mark.unit
def test_validate_video_url_accepts_real_tiktok_host():
    assert validate_video_url("https://www.tiktok.com/@demo/video/1234567890123456789") is True


@pytest.mark.unit
def test_validate_video_url_rejects_fake_tiktok_like_host():
    assert validate_video_url("https://eviltiktok.com/@demo/video/1234567890123456789") is False


@pytest.mark.unit
def test_parse_links_filters_fake_tiktok_like_host():
    text = (
        "ok https://www.tiktok.com/@demo/video/1234567890123456789 "
        "bad https://eviltiktok.com/@demo/video/1234567890123456789"
    )
    links = parse_links(text)
    assert links == ["https://www.tiktok.com/@demo/video/1234567890123456789"]
