import pytest

from services.task_execution_ledger import (
    LEDGER_VERSION,
    attach_url_execution_ledger,
    build_task_execution_ledger,
    build_url_execution_ledger,
)


@pytest.mark.unit
def test_url_execution_ledger_counts_coverage_and_author_fallbacks():
    result = {
        "status": "submitted_unconfirmed",
        "warnings": [{"code": "reply_submitted_unconfirmed"}],
        "engagement": {
            "main": {
                "posted": True,
                "expected": 9,
                "variants_published": [
                    {"text": "m1", "comment_id": "c1", "account_id": 101, "network_confirmed": True},
                    {"text": "m2", "comment_id": None, "account_id": 102, "local_confirmed": True},
                    {"text": "m3", "comment_id": None, "account_id": 103},
                ],
            },
            "replies": {
                "requested_variants": 5,
                "variant_outcomes": 3,
                "submitted": 2,
                "submitted_unconfirmed": 1,
                "items": [
                    {
                        "variant_index": 1,
                        "success": True,
                        "attempted": True,
                        "account_id": 201,
                        "matched_main_text": "target comment",
                        "parent_key": "parent-a",
                        "requested_parent_author": "wanted",
                        "matched_parent_author": "wanted",
                        "parent_author_match_mode": "text_author",
                    },
                    {
                        "variant_index": 2,
                        "success": False,
                        "submitted_unconfirmed": True,
                        "attempted": True,
                        "account_id": 202,
                        "matched_main_text": "target comment",
                        "parent_key": "parent-a",
                        "requested_parent_author": "wanted",
                        "matched_parent_author": "other",
                        "author_fallback_used": True,
                        "error": "stealth_reject_empty_body",
                    },
                    {
                        "variant_index": 3,
                        "success": False,
                        "attempted": True,
                        "account_id": 203,
                        "matched_main_text": "missing comment",
                        "parent_key": "parent-b",
                        "error": "parent_not_found",
                    },
                ],
            },
            "likes": {
                "enabled": True,
                "requested": True,
                "quantity": 50,
                "target_mode": "each_found",
                "orders": [{"username": "wanted", "quantity": 50, "reply_count": 2, "ok": True}],
            },
        },
    }

    ledger = build_url_execution_ledger(result, url="https://tiktok.test/video/1")

    assert ledger["summary"]["main"]["expected"] == 9
    assert ledger["summary"]["main"]["submitted"] == 3
    assert ledger["summary"]["main"]["verified"] == 2
    assert ledger["summary"]["replies"]["requested"] == 5
    assert ledger["summary"]["replies"]["submitted"] == 2
    assert ledger["summary"]["replies"]["variants_not_started"] == 2
    assert ledger["summary"]["replies"]["author_fallbacks"] == 1
    assert any(target["submitted"] == 2 for target in ledger["coverage"]["parent_targets"])
    assert ledger["health_signals"]["error_categories"]["stealth_reject"] == 1
    assert ledger["health_signals"]["error_categories"]["parent_missing"] == 1
    assert ledger["summary"]["likes"]["orders_ok"] == 1


@pytest.mark.unit
def test_task_execution_ledger_aggregates_url_ledgers_without_private_proxy_data():
    url_results = {
        "u1": {
            "status": "success",
            "engagement": {
                "main": {"posted": True, "expected": 1, "variants_published": [{"account_id": 1, "network_confirmed": True}]},
                "replies": {"requested_variants": 1, "items": [{"attempted": True, "success": True, "account_id": 2, "parent_key": "p1"}]},
                "likes": {},
            },
        },
        "u2": {
            "status": "fail",
            "engagement": {
                "main": {"posted": False, "expected": 1, "error": "comment_ui_not_found"},
                "replies": {"requested_variants": 1, "items": [{"attempted": True, "success": False, "account_id": 3, "error": "comment_list_not_open"}]},
                "likes": {},
            },
        },
    }

    task_ledger = build_task_execution_ledger(url_results)

    assert task_ledger["videos"] == {"total": 2, "success_like": 1, "failed": 1, "with_warnings": 0}
    assert task_ledger["summary"]["main"]["expected"] == 2
    assert task_ledger["summary"]["main"]["submitted"] == 1
    assert task_ledger["summary"]["replies"]["requested"] == 2
    assert task_ledger["summary"]["replies"]["submitted"] == 1
    assert task_ledger["health_signals"]["error_categories"]["comment_surface"] == 1
    assert "account_id" not in str(task_ledger)
    assert "proxy" not in str(task_ledger).lower()
    assert "cookie_keys" not in str(task_ledger).lower()


@pytest.mark.unit
def test_task_execution_ledger_counts_expected_parent_targets_from_main_variants():
    url_results = {
        "u1": {
            "status": "success",
            "engagement": {
                "main": {
                    "posted": True,
                    "expected": 3,
                    "variants_published": [
                        {"text": "same text", "comment_id": "cid1", "account_id": 1, "network_confirmed": True},
                        {"text": "same text", "comment_id": "cid2", "account_id": 2, "network_confirmed": True},
                        {"text": "same text", "comment_id": "cid3", "account_id": 3, "network_confirmed": True},
                    ],
                },
                "replies": {
                    "requested_variants": 1,
                    "items": [
                        {
                            "attempted": True,
                            "success": True,
                            "parent_comment_id": "cid2",
                            "matched_main_text": "same text",
                        }
                    ],
                },
                "likes": {},
            },
        }
    }

    task_ledger = build_task_execution_ledger(url_results)
    url_ledger = build_url_execution_ledger(url_results["u1"], url="u1")

    assert task_ledger["coverage"]["parent_targets_total"] == 3
    assert task_ledger["coverage"]["parent_targets_submitted"] == 1
    expected_targets = [target for target in url_ledger["coverage"]["parent_targets"] if target["expected"]]
    assert len(expected_targets) == 3
    assert sum(1 for target in expected_targets if target["submitted"]) == 1


@pytest.mark.unit
def test_attach_url_execution_ledger_mutates_result_compatibly():
    result = {
        "status": "success",
        "engagement": {
            "main": {"posted": True, "expected": 1, "variants_published": [{"network_confirmed": True}]},
            "replies": {},
            "likes": {},
        },
    }

    returned = attach_url_execution_ledger(result, url="u")

    assert returned is result
    assert result["execution_ledger"]["version"] == LEDGER_VERSION
    assert result["execution_ledger"]["summary"]["main"]["verified"] == 1


@pytest.mark.unit
def test_reply_parent_confirmed_does_not_mean_reply_was_published():
    result = {
        "status": "fail",
        "engagement": {
            "main": {"posted": True, "expected": 1},
            "replies": {
                "requested_variants": 2,
                "items": [
                    {
                        "variant_index": 1,
                        "attempted": True,
                        "success": False,
                        "parent_confirmed": True,
                        "visibility_status": "not_checked",
                        "error": "post_button_not_found",
                    },
                    {
                        "variant_index": 2,
                        "attempted": True,
                        "success": False,
                        "parent_confirmed": True,
                        "visibility_status": "network_confirmed",
                    },
                ],
            },
            "likes": {},
        },
    }

    ledger = build_url_execution_ledger(result)
    replies = ledger["summary"]["replies"]

    assert replies["submitted"] == 1
    assert replies["confirmed"] == 1
    assert replies["failed"] == 1
    assert ledger["coverage"]["reply_variants"][0]["status"] == "failed"
    assert ledger["coverage"]["reply_variants"][1]["status"] == "confirmed"
