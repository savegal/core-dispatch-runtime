import pytest


@pytest.mark.unit
def test_coerce_likes_target_mode_accepts_known_modes():
    from services.likes_config import (
        BOOST_LIKES_TARGET_EACH_FOUND,
        BOOST_LIKES_TARGET_SINGLE_TOP,
        coerce_likes_target_mode,
    )

    assert coerce_likes_target_mode("single_top") == BOOST_LIKES_TARGET_SINGLE_TOP
    assert coerce_likes_target_mode(" each_found ") == BOOST_LIKES_TARGET_EACH_FOUND


@pytest.mark.unit
def test_coerce_likes_target_mode_falls_back_to_safe_default():
    from services.likes_config import BOOST_LIKES_TARGET_SINGLE_TOP, coerce_likes_target_mode

    assert coerce_likes_target_mode("unknown") == BOOST_LIKES_TARGET_SINGLE_TOP
    assert coerce_likes_target_mode(None) == BOOST_LIKES_TARGET_SINGLE_TOP
