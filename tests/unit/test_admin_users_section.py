import pytest
from datetime import datetime, timezone
from decimal import Decimal
from starlette.requests import Request

from api.users import get_user, grant_user_full_access, list_users, revoke_user_full_access
from bot.handlers import admin as admin_handlers
from bot.handlers.admin import (
    _admin_users_page_callback,
    _build_admin_users_keyboard,
    _build_admin_users_text,
    _build_adaptive_blogger_search_result,
    _build_user_card_keyboard,
    _build_worker_blogger_search_result,
    _filter_adaptive_bloggers,
    _filter_worker_bloggers,
    _format_worker_blogger_card_text,
    _render_user_card_text,
    _worker_blogger_card_keyboard,
    _user_card_tab_callback,
)
from db.models import (
    Account,
    AdminAction,
    CommentTemplate,
    CreatorSource,
    CreatorVideo,
    Subscription,
    Task,
    TaskTemplate,
    Transaction,
    User,
)


def _test_request() -> Request:
    return Request(
        {
            "type": "http",
            "method": "GET",
            "path": "/api/users",
            "query_string": b"",
            "headers": [],
            "client": ("127.0.0.1", 12345),
        }
    )


def _button_callbacks(markup) -> list[str]:
    return [
        button.callback_data
        for row in markup.inline_keyboard
        for button in row
        if button.callback_data
    ]


def _button_texts(markup) -> list[str]:
    return [
        button.text
        for row in markup.inline_keyboard
        for button in row
    ]


def _button_urls(markup) -> list[str]:
    return [
        button.url
        for row in markup.inline_keyboard
        for button in row
        if button.url
    ]


def _users_page(**overrides) -> dict:
    data = {
        "items": [
            {
                "id": 10,
                "telegram_id": 90010,
                "username": "alice_worker",
                "role": "user",
                "balance": 123.45,
                "is_blocked": False,
                "was_approved_once": True,
                "is_pending_approval": False,
            },
            {
                "id": 11,
                "telegram_id": 90011,
                "username": "pending_bob",
                "role": "user",
                "balance": 0,
                "is_blocked": True,
                "was_approved_once": False,
                "is_pending_approval": True,
            },
        ],
        "total": 22,
        "limit": 10,
        "offset": 10,
        "page": 2,
        "total_pages": 3,
        "filters": {"status": "pending", "q": ""},
        "counts": {
            "total": 22,
            "active": 14,
            "pending": 3,
            "blocked": 4,
            "admins": 1,
            "positive_balance": 7,
        },
    }
    data.update(overrides)
    return data


def _user_detail(**overrides) -> dict:
    data = {
        "id": 10,
        "telegram_id": 90010,
        "username": "alice_worker",
        "role": "user",
        "balance": 123.45,
        "is_blocked": False,
        "was_approved_once": True,
        "is_pending_approval": False,
        "bot_access": True,
        "full_access_override": False,
        "full_access": False,
        "daily_video_limit": 50,
        "today_videos": 7,
        "video_notifications_enabled": True,
        "created_at": "2026-05-18T10:00:00+00:00",
        "updated_at": "2026-05-18T11:00:00+00:00",
        "summary": {
            "subscription": {"tier": "standard", "expire_at": "2026-06-01T00:00:00+00:00", "active": True},
            "orders": {
                "total": 4,
                "active": 1,
                "by_status": {"done": 2, "in_progress": 1, "error": 1},
                "total_cost": 90,
                "refunded_cost": 10,
                "final_cost": 80,
                "recent": [
                    {
                        "id": 501,
                        "status": "done",
                        "progress": 3,
                        "video_count": 3,
                        "final_cost": 25,
                        "origin": "monitoring",
                        "template_name": "Auto template",
                        "creator_username": "creator_demo",
                        "created_at": "2026-05-18T10:10:00+00:00",
                    }
                ],
            },
            "finance": {
                "transactions_by_type": {
                    "deposit": {"count": 1, "amount": 150},
                    "hold": {"count": 2, "amount": -90},
                    "refund": {"count": 1, "amount": 10},
                },
                "latest_transactions": [
                    {
                        "id": 77,
                        "task_id": 501,
                        "amount": -25,
                        "type": "hold",
                        "description": "Списание за заказ",
                        "created_at": "2026-05-18T10:15:00+00:00",
                    }
                ],
            },
            "templates": {
                "task_templates": 2,
                "comment_templates": 3,
                "recent_task_templates": [
                    {"id": 8, "name": "Main pack", "reply_count": 2, "likes": 5, "created_at": "2026-05-18T09:00:00+00:00"}
                ],
                "recent_comment_templates": [
                    {"id": 18, "name": "Reply text", "type": "reply", "created_at": "2026-05-18T09:30:00+00:00"}
                ],
            },
            "monitoring": {
                "creators_total": 6,
                "creators_enabled": 4,
                "creators_disabled": 2,
                "creators_by_mode": {"auto_comment": 3, "manual_review": 2, "notify_only": 1},
                "videos_by_status": {"new": 5, "queued": 2, "done": 8},
                "latest_creators": [
                    {
                        "id": 12,
                        "username": "rubalka",
                        "enabled": True,
                        "mode": "auto_comment",
                        "interval_minutes": 15,
                        "default_template_id": 8,
                        "updated_at": "2026-05-18T09:40:00+00:00",
                    }
                ],
                "video_notifications_enabled": True,
                "daily_video_limit": 50,
                "today_videos": 7,
            },
            "personal_accounts": {
                "total": 9,
                "by_status": {"active": 7, "error": 2},
                "today_main_comments": 11,
                "today_replies": 6,
                "recent": [
                    {
                        "id": 31,
                        "label": "phone-31",
                        "status": "active",
                        "country_code": "UA",
                        "today_main_comments": 2,
                        "today_replies": 1,
                        "last_validation_ok": "2026-05-18T07:00:00+00:00",
                    }
                ],
            },
            "admin_actions": {
                "recent": [
                    {
                        "id": 5,
                        "action_type": "grant_full_access",
                        "description": "Выдача полного доступа",
                        "created_at": "2026-05-18T08:00:00+00:00",
                    }
                ]
            },
        },
    }
    data.update(overrides)
    return data


def test_admin_users_page_callback_is_short_and_stable():
    callback = _admin_users_page_callback(12, "pending")

    assert callback == "admin:users:p:12:pending"
    assert len(callback) <= 64


def test_admin_users_keyboard_has_filters_rows_and_navigation():
    markup = _build_admin_users_keyboard(_users_page())
    callbacks = _button_callbacks(markup)
    texts = _button_texts(markup)

    assert "admin:users:search:pending" in callbacks
    assert "admin:users:p:0:all" in callbacks
    assert "admin:users:p:0:pending" in callbacks
    assert "admin:users:p:0:active" in callbacks
    assert "admin:user:10" in callbacks
    assert "admin:user:11" in callbacks
    assert "admin:users:p:0:pending" in callbacks
    assert "admin:users:p:2:pending" in callbacks
    assert any("alice_worker" in text for text in texts)
    assert all(len(callback) <= 64 for callback in callbacks)


def test_admin_users_keyboard_can_clear_active_search():
    markup = _build_admin_users_keyboard(_users_page(filters={"status": "active", "q": "alice"}))
    callbacks = _button_callbacks(markup)

    assert "admin:users:search:active" in callbacks
    assert "admin:users:clear:active" in callbacks


def test_admin_users_text_uses_page_offset_and_counts():
    text = _build_admin_users_text(_users_page())

    assert "`11.`" in text
    assert "`12.`" in text
    assert "alice_worker" in text
    assert "`123.45`" in text
    assert "`22`" in text
    assert "`2/3`" in text
    assert "Поиск:" in text


def test_user_card_keyboard_has_tabs_contact_and_admin_actions():
    markup = _build_user_card_keyboard(_user_detail(), "overview")
    callbacks = _button_callbacks(markup)
    urls = _button_urls(markup)

    assert _user_card_tab_callback(10, "finance") == "admin:ucard:10:finance"
    assert _user_card_tab_callback(10, "orders", 2) == "admin:ucard:10:orders:2"
    assert "admin:ucard:10:orders" in callbacks
    assert "admin:ucard:10:monitoring" in callbacks
    assert "admin:balance:add:10" in callbacks
    assert "admin:balance:subtract:10" in callbacks
    assert "admin:access:grant:confirm:10" in callbacks
    assert "admin:block:confirm:10" in callbacks
    assert "admin:user:orders:10:0" in callbacks
    assert "admin:wbloggers:10:0" in callbacks
    assert "https://t.me/alice_worker" in urls
    assert all(len(callback) <= 64 for callback in callbacks)


def test_user_card_text_renders_overview_and_deep_tabs():
    detail = _user_detail()

    overview = _render_user_card_text(detail, "overview")
    assert "Карточка работника" in overview
    assert "alice_worker" in overview
    assert "Баланс" in overview
    assert "Автокомментинг" in overview
    assert "Личные аккаунты" in overview

    finance = _render_user_card_text(detail, "finance")
    assert "Финансы работника" in finance
    assert "Последние операции" in finance
    assert "Списание за заказ" in finance

    monitoring = _render_user_card_text(detail, "monitoring")
    assert "Мониторинг и автокомментинг" in monitoring
    assert "rubalka" in monitoring
    assert "автокоммент" in monitoring

    templates = _render_user_card_text(detail, "templates")
    assert "Шаблоны работника" in templates
    assert "Reply text" in templates

    accounts = _render_user_card_text(detail, "accounts")
    assert "Личный пул аккаунтов" in accounts
    assert "phone-31" in accounts


def test_user_card_full_access_override_and_page_navigation():
    detail = _user_detail(full_access_override=True, full_access=True)
    detail["_card_page"] = {
        "tab": "orders",
        "page": 1,
        "per_page": 5,
        "total": 12,
        "items": [
            {
                "id": 777,
                "status": "done",
                "progress": 1,
                "video_count": 1,
                "final_cost": 5,
                "origin": "manual",
                "template_name": "Paged template",
                "created_at": "2026-05-18T10:10:00+00:00",
            }
        ],
    }

    markup = _build_user_card_keyboard(detail, "orders")
    callbacks = _button_callbacks(markup)
    text = _render_user_card_text(detail, "orders")

    assert "admin:access:revoke:confirm:10" in callbacks
    assert "admin:ucard:10:orders" in callbacks
    assert "admin:ucard:10:orders:2" in callbacks
    assert "admin:ucard:noop" in callbacks
    assert "`2/3`" in text
    assert "Paged template" in text


@pytest.mark.unit
@pytest.mark.asyncio
async def test_adaptive_bloggers_loader_fetches_all_pages(monkeypatch):
    calls: list[tuple[int, int]] = []

    async def fake_list_adaptive_bloggers(limit: int, offset: int) -> list[dict]:
        calls.append((limit, offset))
        if offset == 0:
            return [{"id": idx, "username": f"creator_{idx}"} for idx in range(limit)]
        if offset == limit:
            return [{"id": limit + idx, "username": f"creator_tail_{idx}"} for idx in range(3)]
        return []

    monkeypatch.setattr(
        admin_handlers.api_client,
        "list_adaptive_bloggers",
        fake_list_adaptive_bloggers,
    )

    bloggers = await admin_handlers._load_all_adaptive_bloggers()

    assert len(bloggers) == admin_handlers.ADAPTIVE_BLOGGERS_FETCH_LIMIT + 3
    assert calls == [
        (admin_handlers.ADAPTIVE_BLOGGERS_FETCH_LIMIT, 0),
        (
            admin_handlers.ADAPTIVE_BLOGGERS_FETCH_LIMIT,
            admin_handlers.ADAPTIVE_BLOGGERS_FETCH_LIMIT,
        ),
    ]


def test_clear_worker_access_cache_flushes_bot_user_cache(monkeypatch):
    calls: list[tuple[str, int]] = []

    def fake_clear_access_cache(telegram_id: int) -> None:
        calls.append(("subscription", telegram_id))

    monkeypatch.setattr(
        "bot.services.subscription_service.clear_access_cache",
        fake_clear_access_cache,
    )
    monkeypatch.setattr(
        admin_handlers.api_client,
        "clear_user_cache",
        lambda telegram_id: calls.append(("api", telegram_id)),
    )
    monkeypatch.setattr(
        admin_handlers,
        "clear_admin_mode_cache",
        lambda telegram_id: calls.append(("mode", telegram_id)),
    )

    admin_handlers._clear_worker_access_cache({"telegram_id": 90010})

    assert calls == [
        ("subscription", 90010),
        ("api", 90010),
        ("mode", 90010),
    ]


def test_worker_blogger_search_and_card_helpers_are_actionable():
    creators = [
        {"id": 1, "username": "alpha_demo", "enabled": True, "mode": "notify_only"},
        {"id": 2, "username": "target", "enabled": True, "mode": "auto_comment", "new_videos_count": 3},
        {"id": 3, "username": "target_plus", "enabled": False, "mode": "manual_review"},
    ]

    matches = _filter_worker_bloggers(creators, "@target")
    assert [creator["id"] for creator in matches] == [2, 3]

    text = _format_worker_blogger_card_text(
        {
            "id": 2,
            "username": "target",
            "enabled": True,
            "mode": "auto_comment",
            "interval_minutes": 15,
            "new_videos_count": 3,
            "last_checked_at": None,
            "last_seen_video_id": "video-123456789",
            "template_ids": [10, 11],
            "default_template_id": 10,
            "created_at": "2026-05-18T10:00:00+00:00",
            "updated_at": "2026-05-18T11:00:00+00:00",
        },
        "`worker`",
    )
    markup = _worker_blogger_card_keyboard(10, 2, 4, "target")
    callbacks = _button_callbacks(markup)
    urls = _button_urls(markup)

    assert "Карточка блогера работника" in text
    assert "Автокомментинг" in text
    assert "admin:wblogger:10:2:4" in callbacks
    assert "admin:wbloggers:10:4" in callbacks
    assert "https://www.tiktok.com/@target" in urls


def test_worker_blogger_search_results_are_paginated():
    creators = [
        {"id": idx, "username": f"target_{idx}", "enabled": True, "mode": "notify_only"}
        for idx in range(25)
    ]

    text, markup, page = _build_worker_blogger_search_result(
        creators,
        user_backend_id=10,
        owner_label="`worker`",
        query="target",
        page=1,
    )
    callbacks = _button_callbacks(markup)

    assert page == 1
    assert "`11-20`" in text
    assert "admin:wblogger:10:10:0" in callbacks
    assert "admin:wblogger:10:19:0" in callbacks
    assert "admin:wbloggers:search_page:10:0" in callbacks
    assert "admin:wbloggers:search_page:10:2" in callbacks
    assert all(callback and len(callback.encode("utf-8")) <= 64 for callback in callbacks)


def test_adaptive_blogger_search_results_are_paginated():
    bloggers = [
        {
            "id": idx,
            "username": f"target_{idx:02d}",
            "priority_mode": "auto",
            "status": "calibrating",
            "next_interval_minutes": 15,
        }
        for idx in range(25)
    ]

    matches = _filter_adaptive_bloggers(bloggers, "@target")
    text, markup, page = _build_adaptive_blogger_search_result(matches, query="target", page=1)
    callbacks = _button_callbacks(markup)

    assert page == 1
    assert "`11-20`" in text
    assert "`@target_10`" in text
    assert "\\_10" not in text
    assert "admin:adaptive:blogger:10:page:0" in callbacks
    assert "admin:adaptive:blogger:19:page:0" in callbacks
    assert "admin:adaptive:bloggers:search_page:0" in callbacks
    assert "admin:adaptive:bloggers:search_page:2" in callbacks
    assert all(callback and len(callback.encode("utf-8")) <= 64 for callback in callbacks)


@pytest.mark.unit
async def test_list_users_api_returns_pagination_counts_and_filters(async_db_session):
    users = [
        User(
            telegram_id=91001,
            username="active_worker",
            role="user",
            balance=100,
            is_blocked=False,
            was_approved_once=True,
        ),
        User(
            telegram_id=91002,
            username="blocked_worker",
            role="user",
            balance=0,
            is_blocked=True,
            was_approved_once=True,
        ),
        User(
            telegram_id=91003,
            username="pending_worker",
            role="user",
            balance=0,
            is_blocked=True,
            was_approved_once=False,
        ),
        User(
            telegram_id=91004,
            username="admin_user",
            role="admin",
            balance=10,
            is_blocked=False,
            was_approved_once=True,
        ),
    ]
    async_db_session.add_all(users)
    await async_db_session.commit()

    first_page = await list_users(
        request=_test_request(),
        limit=2,
        offset=0,
        status="all",
        q=None,
        db=async_db_session,
    )
    assert first_page["total"] == 4
    assert first_page["page"] == 1
    assert first_page["total_pages"] == 2
    assert len(first_page["items"]) == 2
    assert first_page["counts"]["active"] == 2
    assert first_page["counts"]["blocked"] == 1
    assert first_page["counts"]["pending"] == 1
    assert first_page["counts"]["admins"] == 1

    pending_page = await list_users(
        request=_test_request(),
        limit=10,
        offset=0,
        status="pending",
        q=None,
        db=async_db_session,
    )
    assert pending_page["total"] == 1
    assert pending_page["items"][0]["username"] == "pending_worker"
    assert pending_page["items"][0]["is_pending_approval"] is True

    search_page = await list_users(
        request=_test_request(),
        limit=10,
        offset=0,
        status="all",
        q="blocked",
        db=async_db_session,
    )
    assert search_page["total"] == 1
    assert search_page["items"][0]["username"] == "blocked_worker"


@pytest.mark.unit
async def test_get_user_api_returns_admin_card_summary(async_db_session):
    user = User(
        telegram_id=92001,
        username="summary_worker",
        role="user",
        balance=Decimal("77.50"),
        is_blocked=False,
        was_approved_once=True,
        daily_video_limit=75,
        today_videos=4,
    )
    async_db_session.add(user)
    await async_db_session.flush()

    task = Task(
        user_id=user.id,
        status="done",
        main_comment="main",
        reply1="reply",
        video_urls=["https://tiktok.com/@demo/video/1", "https://tiktok.com/@demo/video/2"],
        settings={"origin": "monitoring", "template_id": 12, "template_name": "Monitor Pack", "creator_username": "creator_one"},
        progress=2,
        total_cost=Decimal("30.00"),
        refunded_cost=Decimal("5.00"),
        kind="full",
    )
    task_template = TaskTemplate(
        user_id=user.id,
        name="Monitor Pack",
        main_comment="main",
        reply1="reply 1",
        reply2="reply 2",
        likes=3,
    )
    comment_template = CommentTemplate(
        user_id=user.id,
        name="Short text",
        text="hello",
        type="main",
    )
    creator = CreatorSource(
        user_id=user.id,
        username="creator_one",
        enabled=True,
        mode="auto_comment",
        interval_minutes=15,
    )
    account = Account(
        label="personal-summary-1",
        status="active",
        owner_user_id=user.id,
        today_main_comments=3,
        today_replies=2,
    )
    transaction = Transaction(
        user_id=user.id,
        task=task,
        amount=Decimal("-30.00"),
        type="hold",
        description="Task hold",
    )
    subscription = Subscription(
        user_id=user.id,
        tier="standard",
        expire_at=datetime.now(timezone.utc),
    )
    admin_action = AdminAction(
        admin_telegram_id=1,
        admin_username="owner",
        action_type="grant_full_access",
        target_user_id=user.id,
        description="Granted",
    )
    async_db_session.add_all([
        task,
        task_template,
        comment_template,
        creator,
        account,
        transaction,
        subscription,
        admin_action,
    ])
    await async_db_session.flush()
    async_db_session.add(
        CreatorVideo(
            creator_id=creator.id,
            video_id="video-summary-1",
            url="https://tiktok.com/@creator_one/video/1",
            status="new",
        )
    )
    await async_db_session.commit()

    detail = await get_user(
        request=_test_request(),
        user_id=user.id,
        db=async_db_session,
    )

    assert detail["username"] == "summary_worker"
    assert detail["bot_access"] is True
    assert detail["full_access_override"] is False
    assert detail["full_access"] is False
    assert detail["daily_video_limit"] == 75
    assert detail["summary"]["orders"]["total"] == 1
    assert detail["summary"]["orders"]["by_status"]["done"] == 1
    assert detail["summary"]["orders"]["recent"][0]["video_count"] == 2
    assert detail["summary"]["finance"]["transactions_by_type"]["hold"]["amount"] == -30.0
    assert detail["summary"]["templates"]["task_templates"] == 1
    assert detail["summary"]["templates"]["comment_templates"] == 1
    assert detail["summary"]["templates"]["recent_comment_templates"][0]["name"] == "Short text"
    assert detail["summary"]["monitoring"]["creators_total"] == 1
    assert detail["summary"]["monitoring"]["videos_by_status"]["new"] == 1
    assert detail["summary"]["personal_accounts"]["total"] == 1
    assert detail["summary"]["personal_accounts"]["today_main_comments"] == 3
    assert detail["summary"]["personal_accounts"]["recent"][0]["label"] == "personal-summary-1"
    assert detail["summary"]["admin_actions"]["recent"][0]["action_type"] == "grant_full_access"


@pytest.mark.unit
async def test_full_access_api_is_override_not_blocking(async_db_session):
    user = User(
        telegram_id=93001,
        username="full_access_worker",
        role="user",
        balance=Decimal("0.00"),
        is_blocked=True,
        was_approved_once=False,
    )
    async_db_session.add(user)
    await async_db_session.commit()

    granted = await grant_user_full_access(
        request=_test_request(),
        user_id=user.id,
        db=async_db_session,
        _=None,
    )
    assert granted["is_blocked"] is False
    assert granted["telegram_id"] == 93001
    assert granted["username"] == "full_access_worker"
    assert granted["full_access_override"] is True
    assert granted["full_access"] is True

    revoked = await revoke_user_full_access(
        request=_test_request(),
        user_id=user.id,
        db=async_db_session,
        _=None,
    )
    assert revoked["is_blocked"] is False
    assert revoked["telegram_id"] == 93001
    assert revoked["full_access_override"] is False
    assert revoked["full_access"] is False
