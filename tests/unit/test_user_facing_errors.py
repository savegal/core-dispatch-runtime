import pytest

from services.user_facing_errors import (
    build_task_display_reason,
    build_task_execution_summary,
    build_video_status_summary,
    humanize_warning,
)
from services.task_execution_ledger import LEDGER_VERSION


@pytest.mark.unit
def test_build_task_execution_summary_aggregates_execution_details():
    task = {
        "status": "done",
        "video_urls": ["https://tiktok.test/1", "https://tiktok.test/2"],
        "settings": {
            "url_results": {
                "https://tiktok.test/1": {
                    "status": "success",
                    "warnings": [{"code": "boost_failed", "message": "Comment likes were not created"}],
                    "engagement": {
                        "main": {
                            "posted": True,
                            "verified": True,
                            "method": "http",
                            "comment_id": "1234567890",
                            "verify_mode": "fast_precheck",
                            "expected": 3,
                            "variants_published": [
                                {"network_confirmed": True},
                                {"local_confirmed": True},
                            ],
                        },
                        "replies": {
                            "ok": 1,
                            "total": 2,
                            "requested_variants": 3,
                            "submitted": 2,
                            "items": [
                                {
                                    "method": "http",
                                    "parent_confirmed": True,
                                    "visibility_status": "confirmed_local",
                                },
                                {
                                    "method": "dom_fallback",
                                    "parent_confirmed": False,
                                    "parent_confirmation_level": "ui_author_only",
                                    "visibility_status": "submitted_unconfirmed",
                                },
                            ],
                        },
                        "likes": {
                            "enabled": True,
                            "requested": True,
                            "quantity": 50,
                            "ok": True,
                            "target_mode": "each_found",
                            "orders": [
                                {"username": "main_a", "quantity": 50, "ok": True, "reply_count": 2},
                                {"username": "main_b", "quantity": 50, "ok": False, "reply_count": 1, "error": "network_error"},
                            ],
                        },
                    },
                },
                "https://tiktok.test/2": {
                    "status": "success",
                    "warnings": [],
                    "engagement": {
                        "main": {
                            "posted": True,
                            "verified": False,
                            "method": "dom_fallback",
                        },
                        "replies": {
                            "ok": 1,
                            "total": 1,
                            "items": [{
                                "method": "dom_fallback",
                                "parent_confirmed": True,
                                "visibility_status": "network_confirmed",
                            }],
                        },
                        "likes": {
                            "enabled": True,
                            "requested": True,
                            "quantity": 50,
                            "ok": False,
                            "error": "network_error",
                        },
                    },
                },
            }
        },
    }

    summary = build_task_execution_summary(task)

    assert summary["display_status"] == "done_warning"
    assert summary["videos"] == {
        "total": 2,
        "success": 2,
        "failed": 0,
        "with_warnings": 1,
        "pending": 0,
    }
    assert summary["main"]["posted"] is True
    assert summary["main"]["verified"] is True
    assert summary["main"]["method"] == "mixed"
    assert summary["main"]["comment_id"] == "1234567890"
    assert summary["main"]["expected"] == 4
    assert summary["main"]["submitted"] == 3
    assert summary["main"]["verified_count"] == 2
    assert summary["replies"]["ok"] == 2
    assert summary["replies"]["total"] == 3
    assert summary["replies"]["requested"] == 4
    assert summary["replies"]["submitted"] == 3
    assert summary["replies"]["parent_confirmed"] == 2
    assert summary["replies"]["parent_ui_confirmed"] == 1
    assert summary["replies"]["visibility_confirmed"] == 2
    assert summary["replies"]["submitted_unconfirmed"] == 1
    assert summary["replies"]["method"] == "mixed"
    assert summary["likes"]["enabled"] is True
    assert summary["likes"]["requested"] is True
    assert summary["likes"]["quantity"] == 50
    assert summary["likes"]["successful_urls"] == 1
    assert summary["likes"]["failed_urls"] == 1
    assert summary["likes"]["error"]
    assert summary["likes"]["target_mode"] == "each_found"
    assert summary["likes"]["orders_total"] == 2
    assert summary["likes"]["orders_ok"] == 1
    assert summary["likes"]["orders_failed"] == 1
    assert [item["username"] for item in summary["likes"]["targets"]] == ["main_a", "main_b"]
    assert summary["ledger"]["version"] == LEDGER_VERSION
    assert summary["coverage"]["parent_targets_total"] >= 1
    assert "error_categories" in summary["health"]


@pytest.mark.unit
def test_build_task_execution_summary_counts_legacy_verified_main_without_variants():
    task = {
        "status": "done",
        "video_urls": ["https://tiktok.test/1"],
        "settings": {
            "url_results": {
                "https://tiktok.test/1": {
                    "status": "success",
                    "engagement": {
                        "main": {"posted": True, "verified": True},
                        "replies": {},
                        "likes": {},
                    },
                }
            }
        },
    }

    summary = build_task_execution_summary(task)

    assert summary["main"]["posted"] is True
    assert summary["main"]["verified"] is True
    assert summary["main"]["expected"] == 1
    assert summary["main"]["submitted"] == 1
    assert summary["main"]["verified_count"] == 1


@pytest.mark.unit
def test_build_video_status_summary_marks_done_warning():
    summary = build_video_status_summary(
        "done",
        {"warnings": [{"code": "boost_failed", "message": "Comment likes were not created"}]},
    )

    assert summary["display_status"] == "done_warning"
    assert summary["status_label"] == "Выполнено с предупреждением"
    assert summary["has_warnings"] is True
    assert summary["display_reason"]


@pytest.mark.unit
def test_build_video_status_summary_marks_manual_skip():
    summary = build_video_status_summary("skipped", {})

    assert summary["display_status"] == "skipped_manual"
    assert summary["status_label"] == "Пропущено вручную"
    assert summary["status_badge"] == "neutral"


@pytest.mark.unit
def test_build_task_execution_summary_humanizes_reply_submitted_unconfirmed():
    task = {
        "status": "done",
        "video_urls": ["https://tiktok.test/1"],
        "settings": {
            "url_results": {
                "https://tiktok.test/1": {
                    "status": "submitted_unconfirmed",
                    "warnings": [
                        {
                            "code": "reply_submitted_unconfirmed",
                            "message": "Reply comment was submitted but not publicly verified",
                        }
                    ],
                    "engagement": {"main": {"posted": True}, "replies": {}, "likes": {}},
                }
            }
        },
    }

    summary = build_task_execution_summary(task)

    assert summary["display_status"] == "done_warning"
    assert "Reply comment" not in (summary["display_reason"] or "")
    assert summary["warnings"]
    assert "Reply comment" not in summary["warnings"][0]


@pytest.mark.unit
def test_humanize_warning_reports_parallel_main_partial_without_raw_message():
    warning = {
        "code": "parallel_main_partial",
        "submitted": 6,
        "expected": 9,
        "message": "6/9 main comments were submitted; replies will target the submitted parents",
    }

    short = humanize_warning(warning, short=True)
    long = humanize_warning(warning, short=False)

    assert short
    assert "6" in short and "9" in short
    assert "main comments" not in short
    assert "main comments" not in long
    assert "ответ" not in long.lower()


@pytest.mark.unit
@pytest.mark.parametrize(
    "warning_code",
    [
        "parallel_main_incomplete_replies_skipped",
        "parallel_main_incomplete_replies_blocked",
    ],
)
def test_humanize_warning_reports_parallel_main_incomplete_replies_skipped(warning_code):
    warning = {
        "code": warning_code,
        "submitted": 6,
        "expected": 9,
        "message": "Reply phase skipped because not all expected main comments were submitted",
    }

    short = humanize_warning(warning, short=True)
    long = humanize_warning(warning, short=False)

    assert short
    assert "6" in short and "9" in short
    assert "Reply phase" not in short
    assert "Reply phase" not in long


@pytest.mark.unit
def test_humanize_warning_reports_parallel_main_partial_replies_continued():
    warning = {
        "code": "parallel_main_partial_replies_continued",
        "submitted": 6,
        "expected": 9,
        "message": "Reply phase continued with the main comments that were submitted",
    }

    short = humanize_warning(warning, short=True)
    long = humanize_warning(warning, short=False)

    assert short
    assert "6" in short and "9" in short
    assert "Reply phase" not in short
    assert "Reply phase" not in long


@pytest.mark.unit
def test_humanize_warning_reports_engagement_author_fallback():
    warning = {
        "code": "engagement_parent_author_fallback",
        "requested_author": "target_user",
        "matched_author": "found_user",
    }

    short = humanize_warning(warning, short=True)
    long = humanize_warning(warning, short=False)

    assert short
    assert "@target_user" in short
    assert "@found_user" in long
    assert "engagement_parent_author_fallback" not in long


@pytest.mark.unit
def test_build_task_execution_summary_treats_submitted_unconfirmed_as_done_warning():
    task = {
        "status": "error",
        "video_urls": ["https://tiktok.test/1"],
        "settings": {
            "url_results": {
                "https://tiktok.test/1": {
                    "status": "submitted_unconfirmed",
                    "error": "main_public_confirmation_unconfirmed",
                    "warnings": [
                        {
                            "code": "main_submitted_unconfirmed",
                            "message": "Comment was submitted but not independently verified via DOM yet",
                        }
                    ],
                    "engagement": {
                        "main": {
                            "posted": True,
                            "verified": False,
                            "method": "http",
                            "comment_id": "cid_123",
                        },
                        "replies": {"ok": 0, "total": 0, "items": []},
                        "likes": {
                            "enabled": False,
                            "requested": False,
                            "quantity": 0,
                            "ok": True,
                        },
                    },
                }
            }
        },
    }

    summary = build_task_execution_summary(task)

    assert summary["display_status"] == "done_warning"
    assert summary["videos"]["success"] == 1
    assert summary["videos"]["failed"] == 0
    assert summary["has_warnings"] is True
    assert "main_public_confirmation_unconfirmed" not in (summary["display_reason"] or "")


@pytest.mark.unit
def test_build_task_execution_summary_counts_legacy_unconfirmed_replies_as_submitted():
    task = {
        "status": "done",
        "video_urls": ["https://tiktok.test/1"],
        "settings": {
            "url_results": {
                "https://tiktok.test/1": {
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
            }
        },
    }

    summary = build_task_execution_summary(task)

    assert summary["replies"]["submitted"] == 3
    assert summary["replies"]["submitted_unconfirmed"] == 2
    assert summary["replies"]["visibility_confirmed"] == 1


@pytest.mark.unit
def test_timeout_stopped_task_keeps_clean_timeout_reason_with_saved_results():
    task = {
        "status": "stopped",
        "error_reason": "task_timeout",
        "video_urls": ["https://tiktok.test/1"],
        "settings": {
            "timeout_stopped": True,
            "timeout_seconds": 1800,
            "url_results": {
                "https://tiktok.test/1": {
                    "status": "submitted_unconfirmed",
                    "warnings": [{"code": "main_submitted_unconfirmed"}],
                    "engagement": {"main": {"posted": True}, "replies": {}, "likes": {}},
                }
            },
        },
    }

    reason = build_task_display_reason(task, short=False)
    summary = build_task_execution_summary(task)

    assert reason
    assert "task_timeout" not in reason
    assert "30" in reason
    assert summary["display_status"] == "stopped"
    assert summary["videos"]["success"] == 1
    assert summary["videos"]["failed"] == 0


@pytest.mark.unit
def test_build_task_execution_summary_keeps_real_failed_video_failed():
    task = {
        "status": "error",
        "video_urls": ["https://tiktok.test/1", "https://tiktok.test/2"],
        "settings": {
            "url_results": {
                "https://tiktok.test/1": {
                    "status": "submitted_unconfirmed",
                    "warnings": [{"code": "main_submitted_unconfirmed"}],
                    "engagement": {"main": {"posted": True}, "replies": {}, "likes": {}},
                },
                "https://tiktok.test/2": {
                    "status": "fail",
                    "error": "comment_input_not_found",
                    "warnings": [],
                    "engagement": {"main": {"posted": False}, "replies": {}, "likes": {}},
                },
            }
        },
    }

    summary = build_task_execution_summary(task)

    assert summary["display_status"] == "failed"
    assert summary["videos"]["success"] == 1
    assert summary["videos"]["failed"] == 1
