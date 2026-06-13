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
def test_order_overview_counts_unconfirmed_reply_submissions_as_sent():
    task = {
        "id": 795,
        "status": "done",
        "progress": 1,
        "video_urls": ["https://vt.tiktok.com/demo/"],
        "reply1": "reply",
        "settings": {
            "origin": "engagement_only",
            "engagement_comment_text": "target comment",
            "url_results": {
                "https://vt.tiktok.com/demo/": {
                    "status": "submitted_unconfirmed",
                    "warnings": [{"code": "reply_submitted_unconfirmed"}],
                    "engagement": {
                        "main": {"posted": True, "verified": True},
                        "replies": {
                            "ok": 1,
                            "total": 3,
                            "submitted_unconfirmed": 2,
                            "items": [
                                {"success": True, "visibility_status": "confirmed_local"},
                                {"submitted_unconfirmed": True, "visibility_status": "submitted_unconfirmed"},
                                {"submitted_unconfirmed": True, "visibility_status": "submitted_unconfirmed"},
                            ],
                        },
                        "likes": {},
                    },
                }
            },
        },
    }

    overview = _render_order_detail(task, "overview")

    assert "Ответы: отправлено 3, вариантов заказано 3" in overview
    assert "подтверждено 1" in overview
    assert "без публичной проверки 2" in overview


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
        "status": "pending",
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
        button.callback_data == "orders:cancel_pending:ask:all:0"
        for row in keyboard.inline_keyboard
        for button in row
    )
    assert any(
        button.callback_data == "orders:page:running:0" and "В работе" in button.text
        for row in keyboard.inline_keyboard
        for button in row
    )


@pytest.mark.unit
def test_orders_keyboard_can_show_cancel_queue_from_pending_probe():
    done_task = {
        "id": 1,
        "status": "done",
        "progress": 1,
        "video_urls": ["https://www.tiktok.com/@demo/video/1"],
        "main_comment": "Done text",
        "settings": {},
    }

    keyboard = _build_orders_keyboard(
        [done_task],
        page=0,
        total_pages=1,
        filter_name="all",
        all_tasks=[done_task],
        has_pending_tasks=True,
    )

    assert any(
        button.text == "🧹 Отменить очередь"
        and button.callback_data == "orders:cancel_pending:ask:all:0"
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
def test_pending_order_detail_exposes_priority_button_once():
    task = {
        "id": 42,
        "status": "pending",
        "progress": 0,
        "video_urls": ["https://www.tiktok.com/@demo/video/1"],
        "main_comment": "Main comment",
        "settings": {"origin": "monitor_auto_comment"},
    }

    keyboard = _build_order_detail_keyboard(task, "overview", page=0, filter_name="all")
    callbacks = [
        button.callback_data
        for row in keyboard.inline_keyboard
        for button in row
    ]

    assert "order:priority:42:overview:0:all:0" in callbacks

    task["settings"]["queue_priority"] = "high"
    high_keyboard = _build_order_detail_keyboard(task, "overview", page=0, filter_name="all")
    high_callbacks = [
        button.callback_data
        for row in high_keyboard.inline_keyboard
        for button in row
    ]

    assert "order:priority:42:overview:0:all:0" not in high_callbacks
    assert "Приоритет: высокий" in _render_order_detail(task, "overview")


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
def test_order_overview_shows_publication_and_boost_like_targets():
    task = {
        "id": 12,
        "kind": "full",
        "status": "done",
        "progress": 1,
        "video_urls": ["https://www.tiktok.com/@demo/video/1"],
        "main_comment": "main",
        "settings": {
            "text_variants": ["main"],
            "replies": ["reply"],
            "reply_count": 1,
            "url_results": {
                "https://www.tiktok.com/@demo/video/1": {
                    "status": "submitted_unconfirmed",
                    "engagement": {
                        "main": {
                            "posted": True,
                            "expected": 3,
                            "variants_published": [
                                {"network_confirmed": True},
                                {"local_confirmed": False},
                            ],
                        },
                        "replies": {
                            "ok": 1,
                            "submitted": 2,
                            "requested_variants": 3,
                            "submitted_unconfirmed": 1,
                            "items": [],
                        },
                        "likes": {
                            "enabled": True,
                            "requested": True,
                            "quantity": 50,
                            "target_mode": "each_found",
                            "orders": [
                                {"username": "main_a", "quantity": 50, "ok": True, "reply_count": 2},
                                {"username": "main_b", "quantity": 50, "ok": False, "reply_count": 1, "error": "network_error"},
                            ],
                        },
                    },
                }
            },
        },
    }

    overview = _render_order_detail(task, "overview")
    videos = _render_order_detail(task, "videos")

    assert "main_a" in overview
    assert "main_b" in overview
    assert "Boost" in overview
    assert "Лайки: 50 частично" in overview
    assert "2, вариантов заказано 3" in overview
    assert "1/2" in overview
    assert "Ответы: отправлено 2, вариантов заказано 3, подтверждено 1, без публичной проверки 1" in overview
    assert "main_a" in videos


@pytest.mark.unit
def test_order_overview_uses_readable_card_sections_without_private_fields():
    task = {
        "id": 812,
        "kind": "full",
        "status": "done",
        "progress": 1,
        "video_urls": ["https://www.tiktok.com/@demo/video/readable"],
        "main_comment": "main",
        "settings": {
            "text_variants": ["main"],
            "replies": ["reply"],
            "reply_count": 1,
            "url_results": {
                "https://www.tiktok.com/@demo/video/readable": {
                    "status": "submitted_unconfirmed",
                    "engagement": {
                        "main": {
                            "expected": 3,
                            "variants_published": [
                                {"network_confirmed": True, "account_id": 1001, "proxy": "secret-proxy"},
                                {"local_confirmed": False, "account_id": 1002},
                            ],
                        },
                        "replies": {
                            "submitted": 1,
                            "requested_variants": 2,
                            "submitted_unconfirmed": 1,
                            "items": [],
                        },
                        "likes": {
                            "enabled": True,
                            "requested": True,
                            "quantity": 50,
                            "target_mode": "single_top",
                            "orders": [
                                {
                                    "username": "main_author",
                                    "quantity": 50,
                                    "reply_count": 1,
                                    "ok": True,
                                    "order_id": "provider-secret-id",
                                }
                            ],
                        },
                    },
                }
            },
        },
    }

    overview = _render_order_detail(task, "overview")

    assert "<b>Статус</b>" in overview
    assert "<b>Состав заказа</b>" in overview
    assert "<b>Итоги публикации</b>" in overview
    assert "<b>Видео</b>" in overview
    assert "Тексты главных: 1" in overview
    assert "Главные: отправлено 2/3" in overview
    assert "Лайки: 50" in overview
    assert "provider-secret-id" not in overview
    assert "secret-proxy" not in overview
    assert "account_id" not in overview


@pytest.mark.unit
def test_order_overview_shows_parallel_totals_parent_targets_and_no_private_fields():
    task = {
        "id": 13,
        "kind": "full",
        "status": "done",
        "progress": 1,
        "video_urls": ["https://www.tiktok.com/@demo/video/2"],
        "settings": {
            "text_variants": ["main one", "main two", "main three"],
            "replies": ["reply one", "reply two", "reply three"],
            "reply_count": 3,
            "boost_likes_enabled": True,
            "likes_count": 100,
            "url_results": {
                "https://www.tiktok.com/@demo/video/2": {
                    "status": "success",
                    "engagement": {
                        "main": {
                            "posted": True,
                            "expected": 9,
                            "variants_published": [
                                {"text": "main one", "account_id": 101, "network_confirmed": True},
                                {"text": "main two", "account_id": 102},
                                {"text": "main three", "account_id": 103},
                            ],
                        },
                        "replies": {
                            "requested_variants": 3,
                            "submitted": 2,
                            "submitted_unconfirmed": 1,
                            "items": [
                                {
                                    "attempted": True,
                                    "success": True,
                                    "matched_main_text": "main one",
                                    "requested_parent_author": "wanted",
                                    "matched_parent_author": "wanted",
                                    "parent_author_match_mode": "text_author",
                                },
                                {
                                    "attempted": True,
                                    "submitted_unconfirmed": True,
                                    "matched_main_text": "main two",
                                    "requested_parent_author": "wanted",
                                    "matched_parent_author": "other_author",
                                    "author_fallback_used": True,
                                },
                            ],
                        },
                        "likes": {
                            "enabled": True,
                            "requested": True,
                            "quantity": 100,
                            "target_mode": "single_top",
                            "orders": [
                                {
                                    "username": "wanted",
                                    "quantity": 100,
                                    "reply_count": 2,
                                    "parent_text": "main one",
                                    "selection_reason": "most_replies",
                                    "ok": True,
                                    "order_id": "private-provider-order",
                                }
                            ],
                        },
                    },
                }
            },
        },
    }

    overview = _render_order_detail(task, "overview")
    videos = _render_order_detail(task, "videos")

    assert "опубликовано 3/9" in overview
    assert "Главные: отправлено 3/9" in overview
    assert "Главные для ответов" in overview
    assert "искали @wanted, ответили @other_author" in overview
    assert "по тексту и username 1" in overview
    assert "по тексту без username 1" in overview
    assert "@wanted: 100 лайков, ответов у выбранного комментария 2" in overview
    assert "выбран комментарий с максимумом ответов" in overview
    assert "Комментарий: <code>main one</code>" in overview
    assert "@wanted 100 (2 отв.)" in videos
    assert "private-provider-order" not in overview
    assert "account_id" not in overview
    assert "proxy" not in overview.lower()


@pytest.mark.unit
def test_order_publication_map_groups_replies_by_main_without_private_fields():
    task = {
        "id": 14,
        "kind": "full",
        "status": "done",
        "progress": 1,
        "video_urls": ["https://www.tiktok.com/@demo/video/map"],
        "settings": {
            "text_variants": ["main one", "main two"],
            "replies": ["reply one", "reply two", "reply three"],
            "url_results": {
                "https://www.tiktok.com/@demo/video/map": {
                    "status": "submitted_unconfirmed",
                    "engagement": {
                        "main": {
                            "expected": 2,
                            "variants_published": [
                                {
                                    "text": "main one",
                                    "comment_id": "main-a-id",
                                    "account_id": 101,
                                    "tiktok_username": "main_author_a",
                                    "network_confirmed": True,
                                    "proxy": "private-proxy",
                                },
                                {
                                    "text": "main two",
                                    "comment_id": "main-b-id",
                                    "account_id": 102,
                                    "tiktok_username": "main_author_b",
                                },
                            ],
                        },
                        "replies": {
                            "requested_variants": 3,
                            "submitted": 3,
                            "submitted_unconfirmed": 2,
                            "items": [
                                {
                                    "text": "reply one",
                                    "success": True,
                                    "matched_main_text": "main one",
                                    "parent_key": "account:101:text:main one",
                                    "matched_parent_author": "main_author_a",
                                    "account_id": 201,
                                },
                                {
                                    "text": "reply two",
                                    "submitted_unconfirmed": True,
                                    "comment_id": "main-a-id",
                                    "matched_main_text": "main two",
                                    "parent_key": "account:102:text:main two",
                                    "requested_parent_author": "wanted_author",
                                    "matched_parent_author": "main_author_b",
                                    "author_fallback_used": True,
                                    "account_id": 202,
                                },
                                {
                                    "text": "reply three",
                                    "submitted_unconfirmed": True,
                                    "matched_main_text": "unknown main",
                                    "parent_key": "unknown main",
                                    "matched_parent_author": "unknown_author",
                                    "account_id": 203,
                                },
                                {
                                    "text": "failed reply",
                                    "attempted": True,
                                    "error": "stealth_reject_empty_body",
                                    "matched_main_text": "main one",
                                },
                            ],
                        },
                        "likes": {
                            "enabled": True,
                            "requested": True,
                            "quantity": 50,
                            "orders": [
                                {
                                    "username": "main_author_a",
                                    "quantity": 50,
                                    "reply_count": 1,
                                    "ok": True,
                                    "order_id": "private-provider-order",
                                }
                            ],
                        },
                    },
                }
            },
        },
    }

    text = _render_order_detail(task, "map")
    keyboard = _build_order_detail_keyboard(task, "map", page=0, filter_name="all")

    assert "Заказ #14 · Публикации" in text
    assert "<b>1. @main_author_a · ✅ подтверждено</b>" in text
    assert "<code>main one</code>" in text
    assert "<code>reply one</code>" in text
    assert "Лайки: 50 лайков заказано" in text
    assert "<b>2. @main_author_b · ☑️ отправлено без публичной проверки</b>" in text
    assert "ответили по совпадению текста" in text
    assert "искали @wanted_author" in text
    assert "<code>reply two</code>" in text
    second_card = text.split("<b>2. @main_author_b", 1)[1].split("<b>Ответы без точной привязки", 1)[0]
    assert "<code>reply two</code>" in second_card
    assert "Ответы без точной привязки" in text
    assert "<code>reply three</code>" in text
    assert "failed reply" not in text
    assert "private-provider-order" not in text
    assert "private-proxy" not in text
    assert "account_id" not in text
    assert any(
        button.callback_data == "order:detail:14:map:0:all"
        for row in keyboard.inline_keyboard
        for button in row
    )


@pytest.mark.unit
def test_engagement_only_publication_map_shows_target_comment_not_published_main():
    task = {
        "id": 16,
        "kind": "engagement_only",
        "status": "done",
        "progress": 1,
        "video_urls": ["https://www.tiktok.com/@demo/video/reply-only"],
        "main_comment": "target parent text",
        "settings": {
            "engagement_username": "wanted_author",
            "engagement_comment_text": "target parent text",
            "replies": ["reply one", "reply two"],
            "reply_count": 2,
            "url_results": {
                "https://www.tiktok.com/@demo/video/reply-only": {
                    "status": "submitted_unconfirmed",
                    "mode": "engagement_only",
                    "comment_id": "parent-cid",
                    "engagement": {
                        "main": {
                            "success": False,
                            "posted": False,
                            "text_choice": None,
                            "comment_id": None,
                        },
                        "replies": {
                            "submitted": 2,
                            "submitted_unconfirmed": 1,
                            "items": [
                                {
                                    "text": "reply one",
                                    "success": True,
                                    "parent_comment_id": "parent-cid",
                                    "matched_main_text": "target parent text",
                                    "requested_parent_author": "wanted_author",
                                    "matched_parent_author": "wanted_author",
                                },
                                {
                                    "text": "reply two",
                                    "submitted_unconfirmed": True,
                                    "matched_main_text": "target parent text",
                                    "requested_parent_author": "wanted_author",
                                    "matched_parent_author": "other_author",
                                    "author_fallback_used": True,
                                },
                            ],
                        },
                    },
                }
            },
        },
    }

    overview = _render_order_detail(task, "overview")
    text = _render_order_detail(task, "map")

    assert "Тип: только ответы" in overview
    assert "Автор поиска: <code>@wanted_author</code>" in overview
    assert "Заказ #16 · Публикации" in text
    assert "Комментарий для ответа" in text
    assert "<code>target parent text</code>" in text
    assert "<code>reply one</code>" in text
    assert "<code>reply two</code>" in text
    assert "username не нашли, ответили по совпадению текста" in text
    assert "Ответы без точной привязки" not in text
    assert "Главные:" not in text


@pytest.mark.unit
def test_order_publication_map_paginates_many_replies_under_soft_limit():
    reply_items = [
        {
            "text": f"reply {idx} " + ("x" * 260),
            "submitted_unconfirmed": True,
            "matched_main_text": "main one",
            "parent_key": "account:101:text:main one",
        }
        for idx in range(1, 21)
    ]
    task = {
        "id": 15,
        "kind": "full",
        "status": "done",
        "progress": 1,
        "video_urls": ["https://www.tiktok.com/@demo/video/map-long"],
        "settings": {
            "text_variants": ["main one"],
            "replies": [item["text"] for item in reply_items],
            "url_results": {
                "https://www.tiktok.com/@demo/video/map-long": {
                    "status": "submitted_unconfirmed",
                    "engagement": {
                        "main": {
                            "expected": 1,
                            "variants_published": [
                                {"text": "main one", "account_id": 101, "tiktok_username": "main_author"}
                            ],
                        },
                        "replies": {
                            "requested_variants": 20,
                            "submitted": 20,
                            "submitted_unconfirmed": 20,
                            "items": reply_items,
                        },
                    },
                }
            },
        },
    }

    page_count = _order_detail_page_count(task, "map")
    pages = [_render_order_detail(task, "map", page) for page in range(page_count)]
    keyboard = _build_order_detail_keyboard(task, "map", page=0, filter_name="all", content_page=0)

    assert page_count > 1
    assert "reply 1" in "\n".join(pages)
    assert "reply 20" in "\n".join(pages)
    assert max(len(page) for page in pages) < 3600
    assert any(
        button.callback_data == "order:detail:15:map:0:all:1"
        for row in keyboard.inline_keyboard
        for button in row
    )


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
