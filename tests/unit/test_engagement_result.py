import pytest

from executor.engagement_engine import EngagementResult


@pytest.fixture(autouse=True)
def http_mode(monkeypatch):
    from executor import engagement_engine

    monkeypatch.setattr(engagement_engine.settings, "TIKTOK_NETWORK_FIRST", True)
    monkeypatch.setattr(engagement_engine.settings, "TIKTOK_NETWORK_IMPL_READY", True)
    monkeypatch.setattr("services.boost_project_client.is_boost_enabled", lambda: False)


@pytest.fixture
def confirmed_http_main():
    result = EngagementResult()
    result.main_comment.update(
        posted=True,
        comment_id="7635267329407913230",
        method="http",
        network_confirmed=True,
        verified=True,
        success=True,
    )
    return result


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
@pytest.mark.parametrize("reply_id", [None, "7635267329407913231"])
def test_engagement_result_treats_unconfirmed_reply_as_submitted_unconfirmed(
    confirmed_http_main, reply_id
):
    """Неподтверждённый ответ не завершает заказ с подтверждённым HTTP-main."""
    result = confirmed_http_main
    result.expected_reply_variants = 1
    result.replies = [
        {
            "success": False,
            "method": "http",
            "network_confirmed": False,
            "local_confirmed": True,
            "submitted_unconfirmed": True,
            "error": "reply_parent_unconfirmed_after_submit",
            "visibility_status": "submitted_unconfirmed",
            "reply_id": reply_id,
        }
    ]

    payload = result.to_dict(source="manual", mode="full")

    assert payload["status"] == "submitted_unconfirmed"
    assert payload["core_status"] == "submitted_unconfirmed"
    assert payload["error"] is None
    assert payload["engagement"]["main"]["success"] is True
    assert payload["engagement"]["main"]["network_confirmed"] is True
    assert payload["engagement"]["replies"]["ok"] == 0
    # Неподтверждённая отправка остаётся видна в статистике, но не засчитывается как успех.
    assert payload["engagement"]["replies"]["submitted_unconfirmed"] == 1
    assert payload["engagement"]["replies"]["items"][0]["local_confirmed"] is False
    assert payload["warnings"][0]["code"] == "reply_submitted_unconfirmed"


@pytest.mark.unit
@pytest.mark.parametrize("comment_id", [None, "main_123"])
def test_engagement_result_rejects_invalid_http_main_id(confirmed_http_main, comment_id):
    """Флаги успеха не заменяют валидный ID главного комментария от API."""
    result = confirmed_http_main
    result.main_comment["comment_id"] = comment_id

    payload = result.to_dict(source="manual", mode="full")

    assert payload["status"] == "fail"
    assert payload["core_status"] == "fail"
    assert payload["engagement"]["main"]["success"] is False
    assert payload["engagement"]["main"]["network_confirmed"] is False


@pytest.mark.unit
@pytest.mark.parametrize("reason", ["stealth_reject_empty_body", "http_session_not_ready"])
def test_engagement_result_fails_when_http_reply_failed(confirmed_http_main, reason):
    """Отказ HTTP-публикации не становится неподтверждённой отправкой или успехом."""
    result = confirmed_http_main
    result.expected_reply_variants = 1
    result.replies = [{"success": False, "method": "http", "error": reason}]

    payload = result.to_dict(source="manual", mode="full")

    assert payload["status"] == "fail"
    assert payload["error"] == reason
    assert payload["engagement"]["main"]["success"] is True
    assert payload["engagement"]["replies"]["ok"] == 0
    assert payload["engagement"]["replies"]["submitted"] == 0
    assert payload["engagement"]["replies"]["items"][0]["local_confirmed"] is False


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
    assert payload["engagement"]["replies"]["submitted"] == 1
    assert payload["engagement"]["replies"]["submitted_unconfirmed"] == 1
    assert payload["warnings"][0]["code"] == "reply_submitted_unconfirmed"
    assert payload["execution_ledger"]["summary"]["replies"]["submitted"] == 1
    assert payload["execution_ledger"]["health_signals"]["error_categories"].get("stealth_reject") == 1
