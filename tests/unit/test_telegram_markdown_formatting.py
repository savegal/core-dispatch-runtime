from bot.services.formatting import markdown_handle, markdown_inline_code, markdown_to_plain_text


def test_markdown_handle_keeps_underscore_visible_without_escape_slash():
    assert markdown_handle("name_with_underscore") == "`@name_with_underscore`"


def test_markdown_inline_code_escapes_code_delimiters_only():
    assert markdown_inline_code("a_b`c\\d") == "`a_b\\`c\\\\d`"


def test_markdown_to_plain_text_removes_escape_slashes():
    assert markdown_to_plain_text(r"@name\_with\_underscore and \*text\*") == "@name_with_underscore and text"
