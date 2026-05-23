from services.likes_config import (
    BOOST_LIKES_DEFAULT,
    BOOST_LIKES_MAX,
    BOOST_LIKES_MIN,
    coerce_bool,
    coerce_likes_quantity,
    provider_available,
)

import pytest


def test_likes_quantity_uses_single_product_range():
    assert coerce_likes_quantity(None) == BOOST_LIKES_DEFAULT
    assert coerce_likes_quantity("bad") == BOOST_LIKES_DEFAULT
    assert coerce_likes_quantity(BOOST_LIKES_MIN - 1) == BOOST_LIKES_MIN
    assert coerce_likes_quantity(BOOST_LIKES_MIN) == BOOST_LIKES_MIN
    assert coerce_likes_quantity(BOOST_LIKES_MAX) == BOOST_LIKES_MAX
    assert coerce_likes_quantity(BOOST_LIKES_MAX + 1) == BOOST_LIKES_MAX
    assert coerce_likes_quantity("bad", "also bad") == BOOST_LIKES_DEFAULT


def test_likes_bool_and_provider_helpers():
    assert coerce_bool("true", False) is True
    assert coerce_bool("off", True) is False
    assert coerce_bool(None, True) is True
    assert provider_available(" key ") is True
    assert provider_available("") is False


@pytest.mark.asyncio
async def test_order_likes_label_depends_on_replies(monkeypatch):
    from bot.services import likes_ui

    async def fake_global_label():
        return "100"

    monkeypatch.setattr(likes_ui, "get_likes_display_label", fake_global_label)

    assert await likes_ui.get_likes_display_label_for_replies(0) == "выключены"
    assert await likes_ui.get_likes_display_label_for_replies({}) == "выключены"
    assert await likes_ui.get_likes_display_label_for_replies({1: "reply"}) == "100"
