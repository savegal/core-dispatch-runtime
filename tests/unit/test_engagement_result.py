import pytest

from executor.engagement_engine import EngagementResult


@pytest.mark.unit
def test_engagement_result_marks_submitted_unconfirmed_without_fail():
    result = EngagementResult()
    result.main_comment.update(
        {
            "posted": True,
            "comment_id": "cid_123",
            "network_confirmed": True,
            "verified": False,
            "success": False,
            "error": "Comment was submitted but not independently verified via DOM yet",
            "submitted_unconfirmed": True,
        }
    )

    payload = result.to_dict(source="manual", mode="full")

    assert payload["status"] == "submitted_unconfirmed"
    assert payload["core_status"] == "submitted_unconfirmed"
    assert payload["error"] is None
    assert payload["warnings"]
    assert payload["warnings"][0]["code"] == "main_submitted_unconfirmed"


@pytest.mark.unit
def test_engagement_result_treats_local_recovery_as_submitted_unconfirmed():
    result = EngagementResult()
    result.main_comment.update(
        {
            "posted": True,
            "comment_id": "cid_local_123",
            "network_confirmed": False,
            "local_confirmed": True,
            "confirmation_source": "local_dom_after_ambiguous_publish",
            "verified": False,
            "success": True,
            "submitted_unconfirmed": True,
        }
    )

    payload = result.to_dict(source="manual", mode="full")

    assert payload["status"] == "submitted_unconfirmed"
    assert payload["core_status"] == "submitted_unconfirmed"
    assert payload["error"] is None
    assert payload["warnings"][0]["code"] == "main_submitted_unconfirmed"


@pytest.mark.unit
def test_engagement_result_treats_local_recovery_without_id_as_submitted_unconfirmed():
    result = EngagementResult()
    result.main_comment.update(
        {
            "posted": True,
            "comment_id": None,
            "network_confirmed": False,
            "local_confirmed": True,
            "local_confirmation_without_id": True,
            "confirmation_source": "local_dom_text_match_no_id",
            "verified": False,
            "success": True,
            "submitted_unconfirmed": True,
        }
    )

    payload = result.to_dict(source="manual", mode="full")

    assert payload["status"] == "submitted_unconfirmed"
    assert payload["core_status"] == "submitted_unconfirmed"
    assert payload["comment_id"] is None
    assert payload["error"] is None
    assert payload["warnings"][0]["code"] == "main_submitted_unconfirmed"


@pytest.mark.unit
def test_engagement_result_fails_when_replies_failed_even_if_main_unconfirmed():
    result = EngagementResult()
    result.main_comment.update(
        {
            "posted": True,
            "comment_id": None,
            "network_confirmed": False,
            "local_confirmed": True,
            "local_confirmation_without_id": True,
            "confirmation_source": "local_dom_text_match_no_id",
            "verified": False,
            "success": True,
            "submitted_unconfirmed": True,
            "error": "main_comment_id_missing_after_local_recovery",
        }
    )
    result.replies = [
        {
            "success": False,
            "error": "main_comment_id_missing_after_local_recovery",
            "visibility_status": "skipped",
        }
    ]

    payload = result.to_dict(source="manual", mode="full")

    assert payload["status"] == "fail"
    assert payload["error"] == "main_comment_id_missing_after_local_recovery"


@pytest.mark.unit
def test_engagement_result_treats_unconfirmed_reply_as_submitted_unconfirmed():
    result = EngagementResult()
    result.main_comment.update(
        {
            "posted": True,
            "comment_id": "main_123",
            "network_confirmed": True,
            "verified": True,
            "success": True,
        }
    )
    result.replies = [
        {
            "success": False,
            "submitted_unconfirmed": True,
            "error": "reply_parent_unconfirmed_after_submit",
            "visibility_status": "submitted_unconfirmed",
            "reply_id": "reply_123",
        }
    ]

    payload = result.to_dict(source="manual", mode="full")

    assert payload["status"] == "submitted_unconfirmed"
    assert payload["core_status"] == "submitted_unconfirmed"
    assert payload["error"] is None
    assert payload["engagement"]["replies"]["ok"] == 0
    assert payload["engagement"]["replies"]["submitted_unconfirmed"] == 1
    assert payload["warnings"][0]["code"] == "reply_submitted_unconfirmed"


@pytest.mark.unit
def test_engagement_only_text_parent_with_unconfirmed_reply_is_not_failed():
    result = EngagementResult()
    result.comment_found = True
    result.comment_id = None
    result.comment_author = "target_author"
    result.replies = [
        {
            "success": False,
            "submitted_unconfirmed": True,
            "error": "stealth_reject_empty_body",
            "visibility_status": "submitted_unconfirmed",
            "parent_confirmed": True,
            "matched_by": "text_author",
        }
    ]

    payload = result.to_dict(source="quick_order", mode="engagement_only")

    assert payload["status"] == "submitted_unconfirmed"
    assert payload["core_status"] == "submitted_unconfirmed"
    assert payload["error"] is None
    assert payload["comment_id"] is None
    assert payload["engagement"]["replies"]["submitted_unconfirmed"] == 1
    assert payload["warnings"][0]["code"] == "reply_submitted_unconfirmed"
