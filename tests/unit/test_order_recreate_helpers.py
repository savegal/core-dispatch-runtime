import pytest

from bot.handlers.orders import (
    _account_pool_label,
    _build_order_detail_keyboard,
    _build_orders_keyboard,
    _build_recreated_order_settings,
    _detail_callback,
    _failed_urls_for_retry,
    _orders_page_callback,
    _order_detail_page_count,
    _parse_order_detail_callback,
    _parse_orders_page_callback,
    _parse_stop_order_callback,
    _render_order_detail,
    _render_orders_list_text,
    _stop_order_callback,
    _task_matches_filter,
    _text_variants_for_recreated_order,
)
from bot.keyboards.composer import format_composer_summary, get_composer_keyboard
from bot.services.drafts import Draft


@pytest.mark.unit
def test_recreated_order_settings_preserve_personal_pool_without_execution_artifacts():
    settings = {
        "account_pool_mode": "personal",
        "delays": False,
        "shuffle": True,
        "rotate_accounts": False,
        "reply_mode": "A",
        "reply_count": 5,
        "reply4": "extra reply",
        "url_results": {"u": {"status": "fail"}},
        "failed_urls": ["u"],
        "idempotency_key": "old",
        "client_task_fingerprint": "old",
    }

    cloned = _build_recreated_order_settings(settings, origin="retry_failed")

    assert cloned["account_pool_mode"] == "personal"
    assert cloned["origin"] == "retry_failed"
    assert cloned["reply_count"] == 5
    assert cloned["reply4"] == "extra reply"
    assert "url_results" not in cloned
    assert "failed_urls" not in cloned
    assert "idempotency_key" not in cloned
    assert "client_task_fingerprint" not in cloned


@pytest.mark.unit
def test_failed_urls_for_retry_falls_back_to_url_results():
    task = {
        "settings": {
            "url_results": {
                "ok": {"status": "success"},
                "bad": {"status": "fail"},
                "bad2": {"status": "error"},
            }
        }
    }

    assert _failed_urls_for_retry(task) == ["bad", "bad2"]


@pytest.mark.unit
def test_failed_urls_for_retry_ignores_stale_submitted_unconfirmed_failed_url():
    task = {
        "settings": {
            "failed_urls": ["ok", "unconfirmed", "bad"],
            "url_results": {
                "ok": {"status": "success"},
                "unconfirmed": {"status": "submitted_unconfirmed"},
                "bad": {"status": "fail"},
            },
        }
    }

    assert _failed_urls_for_retry(task) == ["bad"]


@pytest.mark.unit
def test_order_overview_hides_stale_raw_reason_for_submitted_unconfirmed():
    task = {
        "id": 793,
        "status": "error",
        "display_reason": (
            "Обработано 0 из 1 видео. Основная причина: https://vt.tiktok.com/demo/: "
            "submitted unconfirmed (enrichment failed:critical cookies not generated)"
        ),
        "error_reason": (
            "Успешно: 0/1 ссылок.\nОшибки:\nhttps://vt.tiktok.com/demo/: "
            "submitted_unconfirmed (enrichment_failed:critical_cookies_not_generated)"
        ),
        "progress": 1,
        "video_urls": ["https://vt.tiktok.com/demo/"],
        "main_comment": "main",
        "reply1": "reply",
        "settings": {
            "failed_urls": ["https://vt.tiktok.com/demo/"],
            "url_results": {
                "https://vt.tiktok.com/demo/": {
                    "status": "submitted_unconfirmed",
                    "error": "enrichment_failed:critical_cookies_not_generated",
                    "warnings": [
                        {
                            "code": "reply_submitted_unconfirmed",
                            "message": "Reply comment was submitted but not publicly verified",
                        }
                    ],
                    "engagement": {"main": {"posted": True}, "replies": {}, "likes": {}},
                }
            },
        },
    }

    overview = _render_order_detail(task, "overview")

    assert "1 ок / 0 ошибок" in overview
    assert "Причина:" not in overview
    assert "Важно:" in overview
    assert "enrichment" not in overview
    assert "critical" not in overview
    assert "Reply comment" not in overview
    assert _failed_urls_for_retry(task) == []


@pytest.mark.unit
def test_order_overview_recomputes_stale_failed_execution_summary():
    task = {
        "id": 794,
        "status": "error",
        "execution_summary": {
            "display_status": "failed",
            "status_label": "С ошибкой",
            "status_badge": "error",
            "videos": {"total": 1, "success": 0, "failed": 1, "with_warnings": 1, "pending": 0},
        },
        "progress": 1,
        "video_urls": ["https://vt.tiktok.com/demo/"],
        "main_comment": "main",
        "settings": {
            "url_results": {
                "https://vt.tiktok.com/demo/": {
                    "status": "submitted_unconfirmed",
                    "warnings": [{"code": "reply_submitted_unconfirmed"}],
                    "engagement": {"main": {"posted": True}, "replies": {}, "likes": {}},
                }
            }
        },
    }

    overview = _render_order_detail(task, "overview")

    assert "1 ок / 0 ошибок" in overview
    assert "С ошибкой" not in overview
    assert _task_matches_filter(task, "done") is True
    assert _task_matches_filter(task, "failed") is False


@pytest.mark.unit
def test_text_variants_for_recreated_order_does_not_clone_missing_variants():
    task = {"main_comment": "main", "settings": {"text_variants": ["one"]}}

    assert _text_variants_for_recreated_order(task) == []
    assert "<code>one</code>" in _render_order_detail(task | {"id": 1, "status": "done", "video_urls": []}, "texts")


@pytest.mark.unit
def test_draft_serializes_account_pool_mode():
    draft = Draft(account_pool_mode="personal")

    restored = Draft.from_dict(draft.to_dict())

    assert restored.account_pool_mode == "personal"
    assert _account_pool_label(restored.account_pool_mode) != _account_pool_label("shared")


@pytest.mark.unit
def test_composer_summary_and_keyboard_show_personal_pool():
    draft = Draft(account_pool_mode="personal")

    summary = format_composer_summary(draft)
    keyboard = get_composer_keyboard(draft)

    assert summary
    assert any(
        "personal" in button.callback_data or "pool" in button.callback_data
        for row in keyboard.inline_keyboard
        for button in row
        if button.callback_data
    )


@pytest.mark.unit
def test_orders_callbacks_preserve_page_filter_tab_and_content_page():
    callback_data = _detail_callback(123, "videos", page=2, filter_name="failed")

    assert callback_data == "order:detail:123:videos:2:failed"
    assert _parse_order_detail_callback(callback_data) == (123, "videos", 2, "failed", 0)
    assert _parse_order_detail_callback("order:detail:123") == (123, "overview", 0, "all", 0)
    assert _parse_order_detail_callback("order:detail:123:texts:2:failed:3") == (123, "texts", 2, "failed", 3)
    assert _parse_orders_page_callback(_orders_page_callback("active", 3)) == ("active", 3)
    assert _parse_orders_page_callback(_orders_page_callback("running", 1)) == ("running", 1)
    assert _parse_orders_page_callback("orders:page:4") == ("all", 4)
    stop_callback = _stop_order_callback("stop_prompt", 123, "texts", page=2, filter_name="failed", content_page=3)
    assert stop_callback == "order:stop_prompt:123:texts:2:failed:3"
    assert _parse_stop_order_callback(stop_callback) == (123, "texts", 2, "failed", 3)


@pytest.mark.unit
def test_orders_list_uses_compact_filter_header_and_grid_buttons():
    task_ok = {
        "id": 1,
        "status": "done",
        "progress": 1,
        "video_urls": ["https://www.tiktok.com/@u/video/1111111111111111111"],
        "main_comment": "done text",
        "settings": {"text_variants": ["a", "b", "c"], "creator_username": "creator_demo"},
    }
    task_failed = {
        "id": 2,
        "status": "error",
        "progress": 0,
        "video_urls": ["bad"],
        "main_comment": "failed text",
        "settings": {"failed_urls": ["bad"], "text_variants": ["a", "b", "c"]},
    }

    text = _render_orders_list_text([task_ok, task_failed], [task_ok, task_failed], [task_ok, task_failed], "all", 0, 1)
    keyboard = _build_orders_keyboard([task_ok, task_failed], page=0, total_pages=1, filter_name="all")

    assert "<b>" in text
    assert "1/1" in text
    assert "<code>a</code>" in text
    assert "<code>@creator_demo</code>" in text
    assert any(
        button.callback_data == "order:detail:1:overview:0:all"
        for row in keyboard.inline_keyboard
        for button in row
    )
    assert any(
        button.callback_data == "order:detail:2:overview:0:all"
        for row in keyboard.inline_keyboard
        for button in row
    )
    assert any(
        button.callback_data == "orders:page:running:0" and "В работе" in button.text
        for row in keyboard.inline_keyboard
        for button in row
    )


@pytest.mark.unit
def test_orders_running_filter_shows_only_in_progress_tasks():
    pending_task = {"id": 1, "status": "pending", "progress": 0, "video_urls": [], "main_comment": "pending"}
    running_task = {"id": 2, "status": "in_progress", "progress": 1, "video_urls": ["u"], "main_comment": "running"}
    done_task = {"id": 3, "status": "done", "progress": 1, "video_urls": ["u"], "main_comment": "done"}

    assert _task_matches_filter(running_task, "running") is True
    assert _task_matches_filter(pending_task, "running") is False
    assert _task_matches_filter(done_task, "running") is False


@pytest.mark.unit
def test_order_detail_tabs_show_full_links_texts_and_actions():
    task = {
        "id": 10,
        "status": "pending",
        "progress": 0,
        "video_urls": ["https://www.tiktok.com/@demo/video/7635267329407913230"],
        "main_comment": "Main comment",
        "reply1": "Reply one",
        "likes": 100,
        "settings": {
            "text_variants": ["one", "two", "three"],
            "failed_urls": ["https://www.tiktok.com/@demo/video/7635267329407913230"],
            "url_results": {
                "https://www.tiktok.com/@demo/video/7635267329407913230": {"status": "fail", "error": "input_not_found"}
            },
        },
    }

    overview = _render_order_detail(task, "overview")
    videos = _render_order_detail(task, "videos")
    texts = _render_order_detail(task, "texts")
    keyboard = _build_order_detail_keyboard(task, "texts", page=2, filter_name="failed")

    assert "<code>one</code>" in overview
    assert "https://www.tiktok.com/@demo/video/7635267329407913230" in videos
    assert "<code>one</code>" in texts
    assert "<code>three</code>" in texts
    assert "<code>Reply one</code>" in texts
    assert any(
        button.callback_data == "orders:page:failed:2"
        for row in keyboard.inline_keyboard
        for button in row
    )
    assert any(
        button.callback_data == "order:texts:10"
        for row in keyboard.inline_keyboard
        for button in row
    )
    assert any(
        button.callback_data == "order:copy:10"
        for row in keyboard.inline_keyboard
        for button in row
    )


@pytest.mark.unit
def test_engagement_only_order_display_does_not_show_fake_main_comments():
    task = {
        "id": 11,
        "kind": "engagement_only",
        "status": "pending",
        "progress": 0,
        "video_urls": ["https://www.tiktok.com/@demo/video/7635267329407913230"],
        "main_comment": "anchor text",
        "reply1": "Reply A",
        "settings": {
            "origin": "engagement_only",
            "template_usage": "replies_only",
            "engagement_comment_text": "anchor text",
            "text_variants": ["anchor text", "anchor text", "anchor text"],
            "replies": ["Reply A", "Reply B"],
            "reply_count": 2,
        },
    }

    overview = _render_order_detail(task, "overview")
    texts = _render_order_detail(task, "texts")
    list_text = _render_orders_list_text([task], [task], [task], "all", 0, 1)

    assert "Главные комментарии" not in overview
    assert "Главные комментарии" not in texts
    assert "0 комм." not in list_text
    assert "только ответы" in overview
    assert "только ответы" in texts
    assert "<b>Комментарий 1</b>" not in texts
    assert "<b>Комментарий для поиска</b>" in texts
    assert "<code>anchor text</code>" in texts
    assert "<code>Reply A</code>" in texts
    assert "<code>Reply B</code>" in texts


@pytest.mark.unit
def test_order_texts_paginate_without_hidden_more_counter():
    task = {
        "id": 20,
        "status": "done",
        "progress": 1,
        "video_urls": ["https://www.tiktok.com/@demo/video/1"],
        "main_comment": "comment 1",
        "settings": {
            "text_variants": [f"comment {idx} " + ("x" * 500) for idx in range(1, 11)],
            "replies": [f"reply {idx} " + ("y" * 500) for idx in range(1, 21)],
            "reply_count": 20,
        },
    }

    page_count = _order_detail_page_count(task, "texts")
    combined = "\n".join(_render_order_detail(task, "texts", page) for page in range(page_count))
    keyboard = _build_order_detail_keyboard(task, "texts", page=0, filter_name="all", content_page=0)

    assert "comment 10" in combined
    assert "reply 20" in combined
    assert "ещ" not in combined
    assert any(
        button.callback_data == "order:detail:20:texts:0:all:1"
        for row in keyboard.inline_keyboard
        for button in row
    )


@pytest.mark.unit
def test_order_videos_paginate_full_visible_links():
    urls = [f"https://www.tiktok.com/@demo/video/{idx:03d}" for idx in range(1, 30)]
    task = {
        "id": 21,
        "status": "done",
        "progress": len(urls),
        "video_urls": urls,
        "main_comment": "main",
        "settings": {
            "text_variants": ["main", "alt", "third"],
            "url_results": {url: {"status": "success"} for url in urls},
        },
    }

    page_count = _order_detail_page_count(task, "videos")
    combined = "\n".join(_render_order_detail(task, "videos", page) for page in range(page_count))

    assert urls[0] in combined
    assert urls[-1] in combined
    assert "video/..." not in combined


@pytest.mark.unit
def test_order_texts_paginate_escape_heavy_content_under_telegram_limit():
    task = {
        "id": 22,
        "status": "done",
        "progress": 1,
        "video_urls": ["https://www.tiktok.com/@demo/video/1"],
        "main_comment": "main",
        "settings": {
            "text_variants": ["<&>" * 800, "second", "third"],
            "replies": ["&" * 1200],
            "reply_count": 1,
        },
    }

    page_count = _order_detail_page_count(task, "texts")
    lengths = [len(_render_order_detail(task, "texts", page)) for page in range(page_count)]

    assert page_count > 1
    assert max(lengths) < 3600


@pytest.mark.unit
def test_order_overview_paginate_long_first_comment_and_reply():
    task = {
        "id": 23,
        "status": "pending",
        "progress": 0,
        "video_urls": ["https://www.tiktok.com/@demo/video/1"],
        "main_comment": "&" * 2200,
        "reply1": "&" * 2200,
        "settings": {"text_variants": ["&" * 2200, "second", "third"]},
    }

    page_count = _order_detail_page_count(task, "overview")
    lengths = [_render_order_detail(task, "overview", page) for page in range(page_count)]
    keyboard = _build_order_detail_keyboard(task, "overview", page=0, filter_name="all", content_page=0)

    assert page_count > 1
    assert max(len(text) for text in lengths) < 3600
    assert any(
        button.callback_data == "order:detail:23:overview:0:all:1"
        for row in keyboard.inline_keyboard
        for button in row
    )


@pytest.mark.unit
def test_order_detail_stop_action_only_for_running_orders_and_copy_allowed_after_stop():
    running_task = {
        "id": 30,
        "status": "in_progress",
        "progress": 1,
        "video_urls": ["https://www.tiktok.com/@demo/video/1", "https://www.tiktok.com/@demo/video/2"],
        "main_comment": "main",
        "settings": {"text_variants": ["main", "alt", "third"]},
    }
    stopped_task = running_task | {
        "id": 31,
        "status": "stopped",
        "error_reason": "stopped_by_user",
        "settings": {
            "text_variants": ["main", "alt", "third"],
            "failed_urls": ["https://www.tiktok.com/@demo/video/2"],
            "stopped_by_user": True,
        },
    }

    running_keyboard = _build_order_detail_keyboard(running_task, "overview", page=0, filter_name="active")
    stopped_keyboard = _build_order_detail_keyboard(stopped_task, "overview", page=0, filter_name="stopped")
    stopped_buttons = [
        button
        for row in stopped_keyboard.inline_keyboard
        for button in row
    ]

    assert any(
        button.callback_data == "order:stop_prompt:30:overview:0:active:0"
        for row in running_keyboard.inline_keyboard
        for button in row
    )
    assert not any(button.callback_data and button.callback_data.startswith("order:stop_prompt:") for button in stopped_buttons)
    assert not any(button.callback_data and button.callback_data.startswith("order:retry_failed:") for button in stopped_buttons)
    assert any(button.callback_data == "order:copy:31" for button in stopped_buttons)
    assert not any(button.callback_data == "menu:new_order" for button in stopped_buttons)
    assert "Остановлено" in _render_order_detail(stopped_task, "overview")


@pytest.mark.unit
def test_order_detail_for_video_inbox_origin_has_inbox_return_button():
    task = {
        "id": 40,
        "status": "pending",
        "progress": 0,
        "video_urls": ["https://www.tiktok.com/@demo/video/1"],
        "main_comment": "main",
        "settings": {
            "origin": "new_videos",
            "text_variants": ["main", "alt", "third"],
        },
    }

    keyboard = _build_order_detail_keyboard(task, "overview", page=0, filter_name="all")
    buttons = [button for row in keyboard.inline_keyboard for button in row]

    assert any(button.text == "📥 Входящие" and button.callback_data == "vp:inbox" for button in buttons)
