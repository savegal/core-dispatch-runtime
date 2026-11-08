import pytest

from bot.services.validators import parse_links, validate_video_url
from services.tiktok_urls import is_tiktok_shortlink, media_id_from_url


@pytest.mark.parametrize("url", [
    r"https://127.0.0.1:4443\@www.tiktok.com/video/1234567890123456789",
    "https://u@www.tiktok.com/@demo/video/1234567890123456789",
    "https://www.tiktok.com:4443/@demo/video/1234567890123456789",
    "https://www.tiktok.com/foryou?next=/video/1234567890123456789",
])
def test_media_url_rejects_ambiguous_or_non_media_addresses(url):
    assert not validate_video_url(url)
    assert media_id_from_url(url) is None


def test_photo_and_new_style_shortlink_share_order_and_resolver_rules():
    photo = "https://www.tiktok.com/@demo/photo/1234567890123456789"
    short = "https://www.tiktok.com/t/ZDemo/?next=/video/1234567890123456789"
    assert validate_video_url(photo) and media_id_from_url(photo) == "1234567890123456789"
    assert validate_video_url(short) and is_tiktok_shortlink(short) and media_id_from_url(short) is None


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


def test_quantity_editor_keeps_first_media_link_and_accepts_links_without_protocol():
    from bot.handlers.quantity_orders import parse_links as parse_order_links

    text = ("www.tiktok.com/@demo/video/1234567890123456789?from=share\n"
            "https://m.tiktok.com/@demo/video/1234567890123456789\n"
            "vt.tiktok.com/ZDemo/\n"
            "https://vt.tiktok.com/ZDemo/?from=share")
    assert parse_order_links(text) == [
        "https://www.tiktok.com/@demo/video/1234567890123456789?from=share",
        "https://vt.tiktok.com/ZDemo/",
    ]
