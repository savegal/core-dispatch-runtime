from decimal import Decimal

import pytest

from db.models import CreatorSource, CreatorVideo, Task, User
from services.autopilot.report import (
    collect_finance,
    collect_summary,
    get_task_card,
    human_error_reason,
    list_recent_tasks,
    list_tasks,
    task_publication_counts,
    task_publications,
)


@pytest.mark.unit
@pytest.mark.asyncio
async def test_autopilot_report_service_counts_publications(async_db_session):
    user = User(telegram_id=93001, username="ap_report_user", role="user", balance=1000, is_blocked=False)
    async_db_session.add(user)
    await async_db_session.flush()

    creator = CreatorSource(user_id=user.id, username="ap_creator", enabled=True, interval_minutes=30, mode="auto_comment")
    async_db_session.add(creator)
    await async_db_session.flush()

    task = Task(
        user_id=user.id,
        status="partial",
        main_comment="main",
        video_urls=["https://www.tiktok.com/@ap_creator/video/1"],
        settings={
            "origin": "autopilot",
            "creator_username": "ap_creator",
            "creator_source_id": creator.id,
            "template_name": "Auto #1",
            "url_results": {
                "https://www.tiktok.com/@ap_creator/video/1": {
                    "engagement": {
                        "main": {
                            "variants_published": [
                                {"comment_id": "m1"},
                                {"submitted_unconfirmed": True},
                            ]
                        },
                        "replies": {
                            "items": [{"reply_id": "r1"}, {"submitted_unconfirmed": True}],
                            "total": 3,
                        },
                    }
                }
            },
        },
        total_cost=Decimal("3.20"),
        refunded_cost=Decimal("1.20"),
        error_reason="captcha_challenge",
    )
    async_db_session.add(task)
    await async_db_session.flush()
    async_db_session.add(
        CreatorVideo(
            creator_id=creator.id,
            video_id="1",
            url="https://www.tiktok.com/@ap_creator/video/1",
            status="done",
            task_id=task.id,
        )
    )
    await async_db_session.commit()

    summary = await collect_summary(async_db_session, user.id, "24h")
    assert summary["total"] == 1
    assert summary["published_mains"] == 2
    assert summary["published_replies"] == 2
    assert summary["net_cost"] == 2.0

    tasks = await list_tasks(async_db_session, user.id, "24h", status="partial")
    assert tasks["total"] == 1
    assert tasks["items"][0]["task_id"] == task.id
    assert tasks["items"][0]["mains_published"] == 2
    assert tasks["items"][0]["mains_unconfirmed"] == 1
    assert tasks["items"][0]["needs_review"] is True
    assert "captcha" in tasks["items"][0]["error_summary"].lower()

    card = await get_task_card(async_db_session, user.id, task.id)
    assert card["template_name"] == "Auto #1"
    assert card["creator_username"] == "ap_creator"
    assert card["replies_expected"] == 3
    assert card["mains_confirmed"] == 1
    assert card["mains_unconfirmed"] == 1
    assert card["replies_confirmed"] == 1
    assert card["replies_unconfirmed"] == 1
    assert card["publications"]["mains"][1]["status"] == "unconfirmed"

    review = await list_tasks(async_db_session, user.id, "24h", status="review")
    assert review["total"] == 1
    assert review["items"][0]["task_id"] == task.id

    recent = await list_recent_tasks(async_db_session, user.id, limit=3)
    assert recent[0]["task_id"] == task.id
    assert recent[0]["url"] == "https://www.tiktok.com/@ap_creator/video/1"

    finance = await collect_finance(async_db_session, user.id, "24h")
    assert finance["task_count"] == 1
    assert finance["publication_count"] == 4


@pytest.mark.unit
def test_autopilot_report_counts_use_expected_main_field():
    task = Task(
        user_id=1,
        status="error",
        main_comment="main",
        video_urls=["https://www.tiktok.com/@demo/video/1"],
        settings={
            "origin": "autopilot",
            "url_results": {
                "https://www.tiktok.com/@demo/video/1": {
                    "engagement": {
                        "main": {
                            "expected": 9,
                            "variants_published": [
                                {"comment_id": "m1"},
                                {"comment_id": "m2"},
                                {"comment_id": "m3"},
                            ],
                        },
                        "replies": {"items": [], "requested_variants": 6},
                    }
                }
            },
        },
    )

    counts = task_publication_counts(task)

    assert counts["mains_published"] == 3
    assert counts["mains_expected"] == 9
    assert counts["replies_expected"] == 6


@pytest.mark.unit
def test_autopilot_publications_respect_aggregate_main_unconfirmed_flag():
    task = Task(
        user_id=1,
        status="done",
        main_comment="main",
        video_urls=["https://www.tiktok.com/@demo/video/1"],
        settings={
            "origin": "autopilot",
            "url_results": {
                "https://www.tiktok.com/@demo/video/1": {
                    "engagement": {
                        "main": {
                            "submitted_unconfirmed": True,
                            "visibility_status": "submitted_unconfirmed",
                            "expected": 1,
                            "variants_published": [
                                {
                                    "comment_id": None,
                                    "local_confirmed": True,
                                    "network_confirmed": False,
                                    "account_id": 10,
                                }
                            ],
                        },
                        "replies": {
                            "ok": 0,
                            "submitted_unconfirmed": 1,
                            "items": [],
                            "total": 1,
                        },
                    }
                }
            },
        },
    )

    publications = task_publications(task)
    summary = publications["summary"]

    assert summary["mains_confirmed"] == 0
    assert summary["mains_unconfirmed"] == 1
    assert summary["replies_unconfirmed"] == 1
    assert summary["needs_review"] is True
    assert publications["mains"][0]["status"] == "unconfirmed"


@pytest.mark.unit
@pytest.mark.parametrize(
    ("reason", "marker"),
    [
        ("submitted_unconfirmed (reply_mains_not_found)", "Ответчики"),
        ("enrichment_failed:critical_cookies_not_generated", "cookies"),
        ("dead_session:visible_login_button:text:log in", "логина"),
        ("parallel_main_no_publications_after_two_batches", "двух"),
        ("reply_publication_unconfirmed", "публичное подтверждение"),
        ("Задача зависла в статусе 'in_progress' более 60 минут", "завис"),
    ],
)
def test_autopilot_error_summaries_cover_parallel_errors(reason, marker):
    summary = human_error_reason(reason)

    assert marker.lower() in summary.lower()
    assert reason.replace("_", " ")[:80] != summary[:80]


@pytest.mark.unit
@pytest.mark.asyncio
async def test_autopilot_report_includes_manual_retry_tasks(async_db_session):
    user = User(telegram_id=93002, username="ap_retry_report", role="user", balance=1000, is_blocked=False)
    async_db_session.add(user)
    await async_db_session.flush()

    task = Task(
        user_id=user.id,
        status="pending",
        main_comment="retry",
        video_urls=["https://www.tiktok.com/@ap_retry/video/1"],
        settings={
            "origin": "manual_retry",
            "retry_of_task_id": 100,
            "creator_username": "ap_retry",
            "creator_source_id": 12,
        },
        total_cost=Decimal("5.00"),
    )
    async_db_session.add(task)
    active_task = Task(
        user_id=user.id,
        status="in_progress",
        main_comment="active",
        video_urls=["https://www.tiktok.com/@ap_retry/video/2"],
        settings={
            "origin": "manual_retry",
            "retry_of_task_id": 101,
            "creator_username": "ap_retry",
            "creator_source_id": 12,
        },
        total_cost=Decimal("5.00"),
    )
    async_db_session.add(active_task)
    await async_db_session.commit()

    summary = await collect_summary(async_db_session, user.id, "24h")
    tasks = await list_tasks(async_db_session, user.id, "24h")
    active_tasks = await list_tasks(async_db_session, user.id, "24h", status="in_progress")
    card = await get_task_card(async_db_session, user.id, task.id)

    assert summary["total"] == 2
    assert tasks["total"] == 2
    assert {item["task_id"] for item in tasks["items"]} == {task.id, active_task.id}
    assert active_tasks["total"] == 1
    assert active_tasks["items"][0]["task_id"] == active_task.id
    assert card["origin"] == "manual_retry"
    assert "100" in card["origin_label"]
