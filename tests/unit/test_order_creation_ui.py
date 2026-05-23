from bot.keyboards.templates import format_template_preview
from bot.services.order_creation_ui import build_preview_text, build_success_text


def test_build_preview_text_shows_links_in_plain_text():
    text = build_preview_text(
        links=[
            "https://www.tiktok.com/@demo/video/111",
            "https://www.tiktok.com/@demo/video/222",
        ],
        text_variants=["Первый вариант", "Второй вариант", "Третий вариант"],
        replies={1: "Ответ 1"},
        estimated_cost_label="10.00 грн",
        balance_label="25.00 грн",
    )

    assert "https://www.tiktok.com/@demo/video/111" in text
    assert "https://www.tiktok.com/@demo/video/222" in text
    assert "`https://www.tiktok.com/@demo/video/111`" not in text
    assert "`Первый вариант`" in text
    assert "`Ответ 1`" in text
    assert "💸 Списание: *10.00 грн*" in text
    assert "👛 Баланс: *25.00 грн*" in text


def test_build_success_text_can_show_account_pool():
    text = build_success_text(
        task_id=123,
        links_count=2,
        reply_count=1,
        variants_count=3,
        likes=100,
        pool_label="personal pool",
    )

    assert "personal pool" in text


def test_template_preview_formats_comment_and_replies_as_monospace():
    text = format_template_preview(
        {
            "name": "template",
            "main_comment": "main",
            "settings": {
                "text_variants": ["main one", "main two", "main three"],
                "replies": ["reply one", "reply two"],
            },
        },
        reply_count=2,
    )

    assert "`main one`" in text
    assert "`main three`" in text
    assert "`reply one`" in text
    assert "`reply two`" in text
