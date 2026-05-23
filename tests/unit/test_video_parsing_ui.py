import pytest

from bot.handlers.menu import _build_access_denied_text
from bot.handlers import video_parsing
from bot.handlers.video_parsing import (
    _build_creator_search_result_message,
    _build_creators_page_message,
    _filter_creators_by_query,
)
from bot.handlers.autopilot.main import build_main_text
from bot.handlers.autopilot.current import build_current_text, build_work_text
from bot.handlers.autopilot.card import build_card_text
from bot.handlers.autopilot.common import video_link
from bot.handlers.autopilot.report import build_summary_text, build_tasks_text
from bot.services import software_mode as software_mode_service
from bot.keyboards.common import get_main_menu_keyboard
from bot.keyboards.autopilot.card import get_card_keyboard
from bot.keyboards.autopilot.current import get_current_keyboard, get_work_keyboard
from bot.keyboards.autopilot.main import get_main_keyboard
from bot.keyboards.autopilot.report import get_report_keyboard
from bot.keyboards.video_parsing import (
    get_bulk_actions_keyboard,
    get_bulk_interval_keyboard,
    get_bulk_scopes_keyboard,
    get_creator_keyboard,
    get_creators_list_keyboard,
    get_video_parsing_menu_keyboard,
)
from bot.middleware.software_mode_middleware import SoftwareModeMiddleware


def _button_texts(markup) -> list[str]:
    return [
        button.text
        for row in markup.inline_keyboard
        for button in row
    ]


def _button_callbacks(markup) -> list[str]:
    return [
        button.callback_data
        for row in markup.inline_keyboard
        for button in row
    ]


def _button_callback_rows(markup) -> list[list[str]]:
    return [
        [button.callback_data for button in row]
        for row in markup.inline_keyboard
    ]


def test_parsing_menu_keyboard_shows_new_video_count():
    markup = get_video_parsing_menu_keyboard(new_count=3, parsing_mode=True)
    callbacks = _button_callbacks(markup)

    assert "📥 Новые видео (3)" in _button_texts(markup)
    assert "ap:main" in callbacks
    assert "vp:bulk" not in callbacks


def test_parsing_main_menu_hides_balance():
    markup = get_main_menu_keyboard(software_mode="parsing")
    texts = _button_texts(markup)

    assert "🎬 Парсинг видео" in texts
    assert all("Баланс" not in text for text in texts)


def test_parsing_admin_main_menu_uses_full_mode():
    markup = get_main_menu_keyboard(is_admin=True, software_mode="parsing")
    texts = _button_texts(markup)

    assert "➕ Полный заказ" in texts
    assert "💰 Баланс" in texts
    assert "🔧 Админ" in texts


@pytest.mark.unit
async def test_full_access_override_uses_full_mode_in_parsing(monkeypatch):
    async def fake_get_software_mode_settings() -> dict:
        return {"software_mode": "parsing"}

    async def fake_sync_telegram(telegram_id: int, username: str | None) -> dict:
        return {
            "id": 77,
            "telegram_id": telegram_id,
            "role": "user",
            "is_blocked": False,
            "full_access_override": True,
            "full_access": True,
        }

    software_mode_service.clear_software_mode_cache()
    software_mode_service.clear_admin_mode_cache()
    monkeypatch.setattr(
        software_mode_service.api_client,
        "get_software_mode_settings",
        fake_get_software_mode_settings,
    )
    monkeypatch.setattr(
        software_mode_service.api_client,
        "sync_telegram",
        fake_sync_telegram,
    )

    assert await software_mode_service.get_software_mode_for_user(123, use_cache=False) == "full"
    assert await software_mode_service.is_parsing_mode(use_cache=False, telegram_id=123) is False


@pytest.mark.unit
async def test_invalid_owner_id_does_not_break_parsing_mode_check(monkeypatch):
    import bot.config as bot_config

    async def fake_get_software_mode_settings() -> dict:
        return {"software_mode": "parsing"}

    async def fake_sync_telegram(telegram_id: int, username: str | None) -> dict:
        return {
            "id": 88,
            "telegram_id": telegram_id,
            "role": "user",
            "is_blocked": False,
            "full_access_override": False,
            "full_access": False,
        }

    software_mode_service.clear_software_mode_cache()
    software_mode_service.clear_admin_mode_cache()
    monkeypatch.setattr(bot_config, "load_config", lambda: {"OWNER_TELEGRAM_ID": ""})
    monkeypatch.setattr(
        software_mode_service.api_client,
        "get_software_mode_settings",
        fake_get_software_mode_settings,
    )
    monkeypatch.setattr(
        software_mode_service.api_client,
        "sync_telegram",
        fake_sync_telegram,
    )

    assert await software_mode_service.get_software_mode_for_user(321, use_cache=False) == "parsing"
    assert await software_mode_service.is_parsing_mode(use_cache=False, telegram_id=321) is True


def test_subscription_callback_is_not_allowed_in_parsing_mode():
    middleware = SoftwareModeMiddleware(allow_video_monitoring=True)

    assert middleware._is_allowed_callback("menu:subscription") is False


def test_autopilot_control_callbacks_are_allowed_in_parsing_mode():
    middleware = SoftwareModeMiddleware(allow_video_monitoring=True)

    assert middleware._is_allowed_callback("ap:main") is True
    assert middleware._is_allowed_callback("ap:limits:preset:normal") is True
    assert middleware._is_allowed_callback("ap:rep:tab:list") is True
    assert middleware._is_allowed_callback("vp:autopilot_control") is True
    assert middleware._is_allowed_callback("vp:autoctl:mode:paused") is True
    assert middleware._is_allowed_callback("vp:autoctl:queue") is True
    assert middleware._is_allowed_callback("vp:autoctl:safefix:do") is True
    assert middleware._is_allowed_callback("vp:autopilot_report") is True
    assert middleware._is_allowed_callback("vp:autopilot_report:cancel_all:ask:0") is True
    assert middleware._is_allowed_callback("vp:notifications:toggle") is True


def test_access_denied_text_does_not_blame_balance():
    text = _build_access_denied_text(blocked=False)

    assert "Доступ не подтверждён" in text
    assert "баланс" not in text.lower()
    assert "Недостаточно средств" not in text


def test_creators_page_shows_notify_mode_in_parsing_mode():
    text, page = _build_creators_page_message(
        [
            {
                "id": 1,
                "username": "demo_creator",
                "enabled": True,
                "mode": "auto_comment",
                "interval_minutes": 15,
                "new_videos_count": 2,
                "last_checked_at": None,
            }
        ],
        parsing_mode=True,
    )

    assert page == 0
    assert "`увед.`" in text
    assert "`авто`" not in text


def test_creator_keyboard_uses_compact_action_labels():
    markup = get_creator_keyboard(creator_id=10, enabled=True, parsing_mode=True)
    texts = _button_texts(markup)

    assert "⏱ Интервал" in texts
    assert "🗑 Удалить блогера" in texts


@pytest.mark.unit
async def test_load_all_creator_suggestions_uses_api_limit(monkeypatch):
    calls: list[tuple[int, int, int, bool]] = []

    async def fake_list_creator_suggestions(
        user_id: int,
        limit: int,
        offset: int,
        include_total: bool,
    ) -> dict:
        calls.append((user_id, limit, offset, include_total))
        total = 205
        items = [
            {"username": f"creator_{idx}", "users_count": 1, "last_checked_at": None}
            for idx in range(offset, min(offset + limit, total))
        ]
        return {"items": items, "total": total, "limit": limit, "offset": offset}

    monkeypatch.setattr(
        video_parsing.api_client,
        "list_creator_suggestions",
        fake_list_creator_suggestions,
    )

    suggestions = await video_parsing._load_all_creator_suggestions(user_id=5)

    assert len(suggestions) == 205
    assert calls == [
        (5, 100, 0, True),
        (5, 100, 100, True),
        (5, 100, 200, True),
    ]


@pytest.mark.unit
async def test_create_video_creator_for_bulk_retries_rate_limit(monkeypatch):
    calls: list[dict] = []
    sleeps: list[float] = []

    async def fake_create_video_creator(**kwargs) -> dict:
        calls.append(kwargs)
        if len(calls) == 1:
            raise video_parsing.APIRequestError("Rate limit exceeded: 1 per 1 second")
        return {"id": 123, "username": kwargs["username"]}

    async def fake_sleep(seconds: float) -> None:
        sleeps.append(seconds)

    monkeypatch.setattr(video_parsing.api_client, "create_video_creator", fake_create_video_creator)
    monkeypatch.setattr(video_parsing.asyncio, "sleep", fake_sleep)

    result = await video_parsing._create_video_creator_for_bulk(
        user_id=5,
        username="creator_one",
        creator_mode="notify_only",
    )

    assert result == {"id": 123, "username": "creator_one"}
    assert len(calls) == 2
    assert calls[0]["user_id"] == 5
    assert calls[0]["username"] == "creator_one"
    assert calls[0]["mode"] == "notify_only"
    assert calls[0]["auto_start"] is False
    assert sleeps == [video_parsing.CREATOR_BULK_CREATE_DELAY_SECONDS]


def test_video_parsing_menu_has_global_notification_toggle():
    parsing_markup = get_video_parsing_menu_keyboard(parsing_mode=True, notifications_enabled=True)
    full_markup = get_video_parsing_menu_keyboard(parsing_mode=False, notifications_enabled=False)

    assert "vp:notifications:toggle" in _button_callbacks(parsing_markup)
    assert "vp:notifications:toggle" in _button_callbacks(full_markup)
    assert "🔕 Выключить уведы" in _button_texts(parsing_markup)
    assert "🔔 Включить уведы" in _button_texts(full_markup)


def test_video_parsing_menu_has_autopilot_report_button_in_full_mode():
    parsing_markup = get_video_parsing_menu_keyboard(parsing_mode=True)
    full_markup = get_video_parsing_menu_keyboard(parsing_mode=False)

    assert "vp:autopilot_report" not in _button_callbacks(parsing_markup)
    assert "vp:autopilot_report" not in _button_callbacks(full_markup)
    assert "ap:main" in _button_callbacks(parsing_markup)
    assert "ap:main" in _button_callbacks(full_markup)


def test_autopilot_main_keyboard_uses_short_ap_callbacks():
    markup = get_main_keyboard(mode="active", state="warning", has_incident=True)
    callbacks = _button_callbacks(markup)

    assert "ap:main:notify_admin" in callbacks
    assert "ap:limits" in callbacks
    assert "ap:w" in callbacks
    assert "ap:rep" in callbacks
    assert all(callback and len(callback.encode("utf-8")) <= 30 for callback in callbacks)


def test_autopilot_main_keyboard_places_skip_next_to_journal_when_queue_exists():
    markup = get_main_keyboard(mode="active", state="ok", can_skip_queue=True)
    callbacks = _button_callbacks(markup)

    assert ["ap:main:skipq", "ap:rep"] in _button_callback_rows(markup)
    assert "ap:w" in callbacks
    assert len(callbacks) <= 6
    assert all(callback and len(callback.encode("utf-8")) <= 30 for callback in callbacks)


def test_autopilot_main_keyboard_keeps_incident_skip_layout_compact():
    markup = get_main_keyboard(mode="active", state="warning", has_incident=True, can_skip_queue=True)
    callbacks = _button_callbacks(markup)

    assert ["ap:main:skipq", "ap:rep"] in _button_callback_rows(markup)
    assert "ap:w" in callbacks
    assert "ap:main:notify_admin" in callbacks
    assert len(callbacks) <= 6
    assert all(callback and len(callback.encode("utf-8")) <= 30 for callback in callbacks)


def test_autopilot_report_keyboard_uses_ap_callbacks_and_card_links():
    markup = get_report_keyboard(
        period="24h",
        tab="list",
        status="error",
        page=1,
        pages=3,
        task_rows=[{"task_id": 4220}],
    )
    callbacks = _button_callbacks(markup)

    assert "ap:rep:period:24h" in callbacks
    assert "ap:rep:tab:summary" in callbacks
    assert "ap:rep:list:f:in_progress" in callbacks
    assert "ap:rep:list:f:review" in callbacks
    assert "ap:rep:list:f:error" in callbacks
    assert "ap:card:4220" in callbacks
    assert "ap:rep:open5" in callbacks
    assert "ap:rep:list:p:0" in callbacks
    assert "ap:rep:list:p:2" in callbacks
    assert "ap:action:archive:ask" in callbacks
    assert "ap:main" in callbacks
    assert all(callback and len(callback.encode("utf-8")) <= 30 for callback in callbacks)


def test_autopilot_report_keyboard_uses_specific_creator_pagination_and_reset():
    creators_callbacks = _button_callbacks(
        get_report_keyboard(
            period="24h",
            tab="creators",
            page=1,
            pages=3,
            creator_rows=[{"creator_id": 12, "username": "demo"}],
        )
    )
    list_callbacks = _button_callbacks(
        get_report_keyboard(
            period="24h",
            tab="list",
            creator_filter=12,
            task_rows=[],
        )
    )

    assert "ap:rep:cre:p:0" in creators_callbacks
    assert "ap:rep:cre:p:2" in creators_callbacks
    assert "ap:rep:creator:0" in list_callbacks
    assert all(callback and len(callback.encode("utf-8")) <= 30 for callback in creators_callbacks + list_callbacks)


def test_autopilot_current_keyboard_uses_confirm_actions():
    callbacks = _button_callbacks(get_current_keyboard(has_active=True, queue_count=5))

    assert "ap:current:stop_active" in callbacks
    assert "ap:current:cancel_all" in callbacks
    assert "ap:current:skipq" in callbacks
    assert "ap:current" in callbacks
    assert "ap:main" in callbacks
    assert all(callback and len(callback.encode("utf-8")) <= 30 for callback in callbacks)


def test_autopilot_work_keyboard_exposes_tabs_item_actions_and_pagination():
    plan_callbacks = _button_callbacks(
        get_work_keyboard(
            tab="plan",
            page=0,
            pages=2,
            items=[{"video_id": 10, "task_id": 20}, {"task_id": 21}],
        )
    )
    run_callbacks = _button_callbacks(get_work_keyboard(tab="run", items=[{"task_id": 22}]))
    done_callbacks = _button_callbacks(get_work_keyboard(tab="done", items=[{"task_id": 23}]))

    assert "ap:w:t:plan" in plan_callbacks
    assert "ap:w:t:run" in plan_callbacks
    assert "ap:w:t:done" in plan_callbacks
    assert "ap:w:cv:10" in plan_callbacks
    assert "ap:w:ct:21" in plan_callbacks
    assert "ap:w:p:plan:1" in plan_callbacks
    assert "ap:w:st:22" in run_callbacks
    assert "ap:card:23" in done_callbacks
    assert all(callback and len(callback.encode("utf-8")) <= 30 for callback in plan_callbacks + run_callbacks + done_callbacks)


def test_autopilot_card_keyboard_exposes_retry_hide_and_creator():
    callbacks = _button_callbacks(get_card_keyboard(2147483647, status="error", cost=3.2, creator_id=12))

    assert "ap:c:2147483647:r" in callbacks
    assert "ap:c:2147483647:h" in callbacks
    assert "ap:c:2147483647:oc" in callbacks
    assert "ap:rep:tab:list" in callbacks
    assert all(callback and len(callback.encode("utf-8")) <= 30 for callback in callbacks)


def test_autopilot_card_keyboard_hides_retry_hide_for_non_terminal_orders():
    callbacks = _button_callbacks(get_card_keyboard(42, status="pending", cost=3.2, creator_id=12))

    assert "ap:c:42:r" not in callbacks
    assert "ap:c:42:h" not in callbacks
    assert "ap:c:42:oc" in callbacks


def test_autopilot_card_text_shows_publication_details_and_unconfirmed():
    text = build_card_text(
        {
            "task_id": 77,
            "status": "done",
            "status_label": "Готово",
            "created_at": "2026-05-22T14:30:00+00:00",
            "duration_seconds": 615,
            "creator_username": "blog",
            "url": "https://www.tiktok.com/@blog/video/1",
            "template_name": "Auto",
            "mains_published": 2,
            "mains_expected": 2,
            "mains_confirmed": 1,
            "mains_unconfirmed": 1,
            "replies_published": 1,
            "replies_expected": 1,
            "replies_confirmed": 1,
            "replies_unconfirmed": 0,
            "cost": 1.5,
            "net_cost": 1.5,
            "publications": {
                "mains": [
                    {"account_username": "acc1", "comment_id": "7401", "confirmed": True},
                    {"account_username": "acc2", "unconfirmed": True},
                ],
                "replies": [{"account_username": "acc3", "comment_id": "r1", "confirmed": True}],
            },
        }
    )

    assert "Главных: 2 из 2 (1 ✅, 1 🟠)" in text
    assert "Ответы (1 из 1 (1 ✅))" in text
    assert "@acc1" in text
    assert "ID `7401`" in text
    assert "отправлено, не подтверждено" in text


def test_autopilot_main_text_shows_queue_and_active_task():
    text = build_main_text(
        {
            "state": "ok",
            "state_icon": "✅",
            "state_label": "Работает",
            "queue_count": 5,
            "control": {"mode": "active"},
            "current_work": {"active_task": {"task_id": 4231, "creator_username": "blog_main_1"}},
            "recent_tasks": [
                {
                    "task_id": 4230,
                    "status": "done",
                    "creator_username": "blog_main_2",
                    "url": "https://www.tiktok.com/@blog_main_2/video/1",
                    "mains_published": 9,
                    "mains_expected": 9,
                    "mains_confirmed": 8,
                    "mains_unconfirmed": 1,
                    "needs_review": True,
                }
            ],
            "summary": {
                "total": 87,
                "status_counts": {"done": 62, "partial": 18, "error": 5, "stopped": 2},
                "published_mains": 261,
                "published_replies": 524,
                "net_cost": 273,
            },
        }
    )

    assert "4231" in text
    assert "4230" in text
    assert "видео →" in text
    assert "стоит проверить" in text
    assert "blog" in text
    assert "87" in text
    assert "273" in text


def test_autopilot_main_text_uses_specific_down_label():
    text = build_main_text(
        {
            "state": "down",
            "state_icon": "🔴",
            "state_label": "Executor не отвечает",
            "queue_count": 3,
            "control": {"mode": "active"},
            "current_work": {},
            "summary": {},
        }
    )

    assert "Executor" in text
    assert "Софт не работает" not in text
    assert "3" in text


def test_autopilot_current_text_shows_active_queue_and_outbox():
    text = build_current_text(
        {
            "active_task": {
                "task_id": 4231,
                "creator_username": "blog_main_1",
                "template_name": "Gamer #2",
                "video_urls": ["https://www.tiktok.com/@blog_main_1/video/1"],
                "mains_published": 5,
                "mains_expected": 9,
                "mains_confirmed": 4,
                "mains_unconfirmed": 1,
            },
            "pending_count": 2,
            "queue_head": [
                {"task_id": 4232, "creator_username": "blog2", "video_urls": ["https://www.tiktok.com/@blog2/video/1"]},
                {"task_id": 4233, "creator_username": "blog3"},
            ],
            "outbox_active": [{"creator_username": "blog10", "video_count": 3, "video_urls": ["https://www.tiktok.com/@blog10/video/1"]}],
        }
    )

    assert "4231" in text
    assert "Gamer #2" in text
    assert "5 из 9" in text
    assert "https://www.tiktok.com/@blog_main_1/video/1" in text
    assert "4232" in text
    assert "blog10" in text
    assert "Скоро запустится" in text


def test_autopilot_current_text_shows_processing_tasks_without_active_slot():
    text = build_current_text(
        {
            "active_task": None,
            "processing_count": 1,
            "processing_tasks": [{"task_id": 4232, "creator_username": "blog2", "updated_at": "2026-05-21T10:00:00+00:00"}],
            "pending_count": 0,
            "queue_head": [],
            "outbox_active": [],
        }
    )

    assert "Сейчас тихо" not in text
    assert "*В работе без активного слота (1)*" in text
    assert "`#4232`" in text


def test_autopilot_work_text_sections_are_informative():
    planned = build_work_text(
        {
            "items": [
                {
                    "video_id": 10,
                    "task_id": 20,
                    "creator_username": "blog",
                    "url": "https://www.tiktok.com/@blog/video/1",
                    "status": "queued",
                    "label": "Заказ уже создан",
                    "discovered_at": "2026-05-22T10:00:00+00:00",
                }
            ],
            "total": 1,
            "page": 0,
            "pages": 1,
        },
        "plan",
    )
    running = build_work_text(
        {
            "items": [
                {
                    "task_id": 21,
                    "creator_username": "blog",
                    "url": "https://www.tiktok.com/@blog/video/2",
                    "stage": "Публикует главные комментарии",
                    "mains_published": 5,
                    "mains_expected": 9,
                    "mains_confirmed": 5,
                    "replies_published": 0,
                    "replies_expected": 9,
                    "video_count": 1,
                    "progress_percent": 0,
                }
            ],
            "total": 1,
        },
        "run",
    )
    done = build_work_text(
        {
            "items": [
                {
                    "task_id": 22,
                    "status": "done",
                    "creator_username": "blog",
                    "url": "https://www.tiktok.com/@blog/video/3",
                    "mains_published": 9,
                    "mains_expected": 9,
                    "replies_published": 9,
                    "replies_expected": 9,
                    "net_cost": 3.2,
                }
            ],
            "total": 1,
            "page": 0,
            "pages": 1,
        },
        "done",
    )

    assert "Работа · Планируется" in planned
    assert "✖ 1" in planned
    assert "Заказ уже создан" in planned
    assert "Работа · В работе" in running
    assert "Публикует главные" in running
    assert "5 из 9" in running
    assert "Работа · Обработано" in done
    assert "3.20₴" in done


def test_autopilot_report_text_builders_show_real_counts():
    summary_text = build_summary_text(
        {
            "total": 3,
            "status_counts": {"done": 1, "partial": 1, "error": 1},
            "published_mains": None,
            "published_replies": None,
            "cost": 10,
            "refunded": 2,
            "net_cost": 8,
            "top_errors": [{"reason": "captcha_challenge", "count": 2}],
        }
    )
    tasks_text = build_tasks_text(
        {
            "items": [
                {
                    "task_id": 4220,
                    "status": "error",
                    "creator_username": "blog_main_3",
                    "created_at": "2026-05-21T14:25:00",
                    "mains_published": 0,
                    "mains_expected": 9,
                    "replies_published": 0,
                    "replies_expected": 9,
                    "mains_confirmed": 0,
                    "mains_unconfirmed": 0,
                    "cost": 3.2,
                    "url": "https://www.tiktok.com/@blog_main_3/video/1",
                    "error_summary": "captcha",
                    "needs_review": True,
                }
            ],
            "total": 1,
            "page": 0,
            "pages": 1,
        }
    )

    assert "3" in summary_text
    assert "captcha" in summary_text
    assert "4220" in tasks_text
    assert "видео →" in tasks_text
    assert "проверить" in tasks_text
    assert "blog" in tasks_text


def test_autopilot_video_link_escapes_parentheses_in_url():
    link = video_link("https://www.tiktok.com/@blog/video/1?note=(test)", "видео →")

    assert "%28test%29" in link
    assert link.startswith("[видео")


def test_bulk_actions_are_hidden_in_parsing_mode_lists():
    creators = [
        {
            "id": 1,
            "username": "demo_creator",
            "enabled": True,
            "mode": "auto_comment",
            "interval_minutes": 15,
        }
    ]
    parsing_callbacks = _button_callbacks(get_creators_list_keyboard(creators, parsing_mode=True))
    full_callbacks = _button_callbacks(get_creators_list_keyboard(creators, parsing_mode=False))
    bulk_callbacks = _button_callbacks(get_bulk_actions_keyboard(parsing_mode=True))

    assert "vp:bulk" not in parsing_callbacks
    assert "vp:bulk" in full_callbacks
    assert bulk_callbacks == ["vp:menu"]
    assert "vp:list:search" in parsing_callbacks
    assert "vp:list:search" in full_callbacks


def test_creator_keyboard_has_autopilot_queue_cancel_in_full_mode_only():
    full_callbacks = _button_callbacks(get_creator_keyboard(creator_id=42, enabled=True, parsing_mode=False))
    parsing_callbacks = _button_callbacks(get_creator_keyboard(creator_id=42, enabled=True, parsing_mode=True))

    assert "vp:c:42:apc" in full_callbacks
    assert "vp:c:42:apc" not in parsing_callbacks
    assert all(callback and len(callback.encode("utf-8")) <= 30 for callback in full_callbacks if callback.startswith("vp:c:"))


def test_creator_search_filter_prioritizes_exact_and_prefix_matches():
    creators = [
        {"id": 1, "username": "demo_middle_match"},
        {"id": 2, "username": "target"},
        {"id": 3, "username": "target_extra"},
        {"id": 4, "username": "other"},
    ]

    matches = _filter_creators_by_query(creators, "@target")

    assert [creator["id"] for creator in matches] == [2, 3]


def test_creator_search_results_are_paginated_and_open_cards():
    creators = [
        {"id": idx, "username": f"target_{idx}", "enabled": True, "mode": "notify_only"}
        for idx in range(25)
    ]

    text, markup, page = _build_creator_search_result_message(creators, "target", page=1)
    callbacks = _button_callbacks(markup)

    assert page == 1
    assert "`11-20`" in text
    assert "vp:creator:10:detail" in callbacks
    assert "vp:creator:19:detail" in callbacks
    assert "vp:list:search:page:0" in callbacks
    assert "vp:list:search:page:2" in callbacks
    assert all(callback and len(callback.encode("utf-8")) <= 64 for callback in callbacks)


def test_bulk_action_keyboards_have_safe_callbacks():
    callbacks = (
        _button_callbacks(get_bulk_actions_keyboard(parsing_mode=False))
        + _button_callbacks(get_bulk_scopes_keyboard("auto_tpl"))
        + _button_callbacks(get_bulk_interval_keyboard("interval", "all"))
    )

    assert "vp:bulk:a:auto_tpl" in callbacks
    assert "vp:bulk:a:interval" in callbacks
    assert "vp:bulk:s:auto_tpl:without_default_template" in callbacks
    assert "vp:bulk:i:interval:all:60" in callbacks
    assert all(callback and len(callback.encode("utf-8")) <= 64 for callback in callbacks)
