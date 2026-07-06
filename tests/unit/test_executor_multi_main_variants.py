import pytest
import inspect

from executor import executor


@pytest.mark.unit
def test_main_variants_for_url_publishes_up_to_three_unique_variants():
    variants = executor._main_variants_for_url(
        text_variants=["one", "two", "three", "four"],
        task_settings={},
        task_id=10,
        url_index=0,
        video_count=1,
    )

    assert variants == ["one", "two", "three"]


@pytest.mark.unit
def test_main_variants_for_url_rotates_autopilot_offset():
    variants = executor._main_variants_for_url(
        text_variants=["one", "two", "three"],
        task_settings={"origin": "autopilot", "text_variant_offset": 1},
        task_id=10,
        url_index=0,
        video_count=1,
    )

    assert variants == ["two", "three", "one"]


@pytest.mark.unit
def test_main_variants_for_url_can_keep_legacy_single_variant():
    variants = executor._main_variants_for_url(
        text_variants=["one", "two", "three"],
        task_settings={"publish_all_text_variants": False},
        task_id=10,
        url_index=0,
        video_count=1,
    )

    assert variants == ["one"]


@pytest.mark.unit
def test_merge_full_variant_results_marks_partial_as_submitted_unconfirmed():
    merged = executor._merge_full_variant_results(
        [
            {
                "status": "success",
                "core_status": "success",
                "comment_id": "cid_1",
                "warnings": [],
                "engagement": {
                    "main": {"text_choice": "one", "comment_id": "cid_1", "verified": True},
                    "replies": {"ok": 0, "total": 0, "items": []},
                    "likes": {"ok": True, "enabled": False, "requested": False},
                },
            },
            {
                "status": "fail",
                "core_status": "fail",
                "error": "input_not_found",
                "warnings": [],
                "engagement": {
                    "main": {"text_choice": "two", "error": "input_not_found"},
                    "replies": {"ok": 0, "total": 0, "items": []},
                    "likes": {"ok": True, "enabled": False, "requested": False},
                },
            },
        ]
    )

    assert merged["status"] == "submitted_unconfirmed"
    assert merged["comment_id"] == "cid_1"
    assert merged["main_variants_summary"] == {"total": 2, "submitted": 1, "failed": 1}
    assert merged["warnings"][0]["code"] == "main_variant_partial"
    assert merged["engagement"]["main_variants"][1]["error"] == "input_not_found"


@pytest.mark.unit
def test_process_task_preserves_user_stopped_status_before_finalization():
    source = inspect.getsource(executor.process_task)
    callback_start = source.index("def user_schema_stop_requested")
    callback_end = source.index("user_schema_runner", callback_start)
    user_schema_callback = source[callback_start:callback_end]

    assert "read_stop_marker()" in source
    assert 'task.status = "stopped"' in source
    assert 'task.error_reason = "stopped_by_user"' in source
    assert "finalized as stopped by user" in source
    assert "stop_requested=user_schema_stop_requested" in source
    assert "task_timeout_requested_now()" in user_schema_callback
    assert source.count("stop_requested=task_stop_requested_now") >= 2
    assert "def wait_for_full_url_lock" in source
    assert "full_url_lock.acquire(timeout=0.5)" in source
    assert "full_url_lock.acquire()" not in source


@pytest.mark.unit
def test_process_task_counts_submitted_unconfirmed_as_successful_url_with_warning():
    source = inspect.getsource(executor.process_task)

    assert executor._URL_SUCCESS_STATUSES == {"success", "submitted_unconfirmed"}
    assert executor._URL_UNCONFIRMED_STATUSES == {"submitted_unconfirmed"}
    assert "if result_status in _URL_SUCCESS_STATUSES" in source


def test_reply_visibility_exhaustion_is_fatal_for_remaining_order_urls():
    assert executor._reply_visibility_exhausted(
        {"error": "remaining_main_comments_not_visible_after_four_accounts"}
    ) is True
    assert executor._reply_visibility_exhausted(
        {"error": "reply_publication_incomplete"}
    ) is False

    source = inspect.getsource(executor.process_task)
    assert source.count("if _reply_visibility_exhausted(result_dict):") == 2
    assert "unconfirmed_urls.append(video_url)" in source


def test_merge_full_results_preserves_fatal_reply_visibility_after_shadowed_variant():
    fatal_error = "remaining_main_comments_not_visible_after_four_accounts"
    merged = executor._merge_full_variant_results(
        [
            {
                "status": "fail",
                "core_status": "fail",
                "error": "text_variant_shadowed",
                "warnings": [],
                "engagement": {"main": {"success": False}},
            },
            {
                "status": "fail",
                "core_status": "fail",
                "error": fatal_error,
                "warnings": [],
                "engagement": {
                    "main": {"success": True, "posted": True},
                    "replies": {"ok": 0, "total": 1},
                },
            },
        ]
    )

    assert merged["status"] == "fail"
    assert merged["core_status"] == "fail"
    assert merged["error"] == fatal_error
    assert executor._reply_visibility_exhausted(merged) is True


@pytest.mark.unit
def test_process_task_engagement_only_does_not_fall_through_to_full_flow():
    source = inspect.getsource(executor.process_task)
    engagement_start = source.index('if task_kind == "engagement_only":')
    full_start = source.index("HYBRID FULL", engagement_start)
    engagement_block = source[engagement_start:full_start]

    assert "db.commit()" in engagement_block
    assert "if _reply_visibility_exhausted(result_dict):" in engagement_block
    assert "break\n                    continue" in engagement_block
