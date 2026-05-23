import pytest

from bot.handlers.engagement_wizard import _parse_comment_locator


@pytest.mark.unit
def test_parse_comment_locator_extracts_username_and_text():
    username, text = _parse_comment_locator("@real.user_1: Can someone check my acc price?")

    assert username == "real.user_1"
    assert text == "Can someone check my acc price?"


@pytest.mark.unit
def test_parse_comment_locator_leaves_plain_text_unchanged():
    username, text = _parse_comment_locator("Can someone check my acc price?")

    assert username is None
    assert text == "Can someone check my acc price?"


@pytest.mark.unit
def test_parse_comment_locator_does_not_treat_plain_colon_as_username():
    username, text = _parse_comment_locator("Question: where can I check this?")

    assert username is None
    assert text == "Question: where can I check this?"
