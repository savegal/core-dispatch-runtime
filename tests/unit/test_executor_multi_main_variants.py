import pytest
import inspect
import copy

from executor import executor


@pytest.mark.unit
@pytest.mark.parametrize("published,expected,replies,status", [
    (6, 9, 0, "partial"), (9, 9, 0, "success"), (0, 9, 0, "fail"), (3, 3, 1, "partial"),
])
def test_merge_preserves_already_aggregated_user_schema_result(monkeypatch, published, expected, replies, status):
    from executor import user_schema
    from services.task_execution_ledger import build_url_execution_ledger

    monkeypatch.setattr(user_schema._settings, "TIKTOK_NETWORK_FIRST", True)
    monkeypatch.setattr(user_schema._settings, "TIKTOK_NETWORK_IMPL_READY", True)
    result = user_schema.UserSchemaResult(expected_main_publications=expected, expected_reply_variants=replies)
    result.published_mains = [user_schema.PublishedMain(
        text=f"main {index}", comment_id=str(7696976534629237517 + index), network_confirmed=True,
        local_confirmed=False, method="http_user_schema", account_id=index + 1, tiktok_username="demo",
    ) for index in range(published)]
    if not published:
        result.error = "no_main_published"
    source = result.to_dict()
    original = copy.deepcopy(source)
    assert source["status"] == status

    merged = executor._merge_full_variant_results([source])

    assert merged["status"] == status
    assert merged["error"] == source["error"]
    assert merged["publication_complete"] == source["publication_complete"]
    assert merged["engagement"]["main"]["expected"] == expected
    assert merged["engagement"]["main"]["submitted"] == published
    assert merged["engagement"]["main"]["missing"] == expected - published
    assert executor._url_result_counts_as_success(merged) is (status == "success")
    ledger = build_url_execution_ledger(merged)
    assert ledger["summary"]["main"]["expected"] == expected
    assert ledger["summary"]["main"]["submitted"] == published
    assert source == original
    merged["engagement"]["main"]["expected"] = -1
    assert source == original


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
def test_merge_preserves_aggregate_counts_while_enforcing_fatal_reply_failure():
    source = {
        "status": "success", "core_status": "success", "publication_complete": True,
        "error": "remaining_main_comments_not_visible_after_four_accounts",
        "engagement": {"main": {"expected": 9, "submitted": 6, "missing": 3,
                                  "variants_published": [{"comment_id": str(index)} for index in range(6)]}},
    }
    merged = executor._merge_full_variant_results([source])
    assert merged["status"] == "fail"
    assert merged["core_status"] == "fail"
    assert merged["publication_complete"] is False
    assert merged["engagement"]["main"]["expected"] == 9
    assert merged["engagement"]["main"]["submitted"] == 6
    assert not executor._url_result_counts_as_success(merged)


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
def test_merge_full_variant_results_marks_partial_as_incomplete():
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

    assert merged["status"] == "partial"
    assert merged["publication_complete"] is False
    assert merged["comment_id"] == "cid_1"
    assert merged["main_variants_summary"] == {"total": 2, "submitted": 1, "failed": 1}
    assert merged["warnings"][0]["code"] == "main_variant_partial"
    assert merged["engagement"]["main_variants"][1]["error"] == "input_not_found"
    assert merged["engagement"]["main"]["complete"] is False


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
    assert "if _url_result_counts_as_success(result_dict)" in source
    assert executor._url_result_counts_as_success(
        {"status": "submitted_unconfirmed", "publication_complete": True}
    ) is True
    assert executor._url_result_counts_as_success(
        {
            "status": "submitted_unconfirmed",
            "error": "reply_publication_incomplete",
            "publication_complete": False,
        }
    ) is False


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
