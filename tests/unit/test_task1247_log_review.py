"""Reproduce the recorded 2/3 result without any live publications."""
import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from executor import engagement_engine as engine
from executor.browser_reads import await_browser_read, _pending_reads
from services.task_execution_ledger import has_confirmed_publications, has_submission_evidence
from services.order_transport import set_task_order_transport, reset_task_order_transport


def receipt(index=1):
    return {"text": f"reply {index}", "variant_index": index, "success": True,
            "network_confirmed": True, "method": "http", "reply_id": str(7641761192486194454 + index)}


def partial_result():
    result = engine.EngagementResult()
    result.comment_found = True
    result.comment_id = "7699899442673468212"
    result.expected_reply_variants = 3
    result.replies = [receipt(1), {"text": "reply 2", "variant_index": 2, "success": False,
        "error": "stealth_reject_empty_body", "method": "http", "network_confirmed": False}, receipt(3)]
    return result


@pytest.mark.asyncio
async def test_timed_out_read_remains_observed_until_page_close():
    future = asyncio.get_running_loop().create_future()
    async def driver_read():
        await asyncio.wait({future})
        return future.result()
    with pytest.raises(asyncio.TimeoutError):
        await await_browser_read(driver_read, timeout=.01)
    assert not future.cancelled()
    future.set_exception(RuntimeError("Target page closed"))
    for _ in range(5): await asyncio.sleep(0)
    assert not _pending_reads
    assert future._log_traceback is False


@pytest.mark.asyncio
async def test_external_cancellation_is_immediate_and_read_is_drained():
    released = asyncio.Event()
    started = asyncio.Event()
    async def read():
        started.set()
        await released.wait()
        raise RuntimeError("context closed")
    caller = asyncio.create_task(await_browser_read(read))
    await started.wait()
    caller.cancel()
    with pytest.raises(asyncio.CancelledError): await caller
    assert _pending_reads
    released.set()
    for _ in range(5): await asyncio.sleep(0)
    assert not _pending_reads


def test_two_confirmed_replies_are_partial_not_total_failure():
    token = set_task_order_transport("http", SimpleNamespace(TIKTOK_NETWORK_IMPL_READY=True))
    try:
        row = partial_result().to_dict(mode="engagement_only")
        assert row["status"] == row["core_status"] == "partial"
        assert row["engagement"]["replies"]["ok"] == 2
        assert row["engagement"]["replies"]["expected"] == 3
        assert row["error"] == "stealth_reject_empty_body"
        assert has_confirmed_publications(row)
        from executor.executor import _completion_status_with_errors, _url_result_counts_as_success
        assert _completion_status_with_errors(0, {"video": row}) == "partial"
        assert not _url_result_counts_as_success(row)
    finally:
        reset_task_order_transport(token)


def test_parent_found_with_zero_reply_receipts_stays_failed():
    result = partial_result()
    result.replies = [{"text": "reply", "success": False, "error": "stealth_reject_empty_body"}]
    row = result.to_dict(mode="engagement_only")
    assert row["status"] == "fail"
    assert not has_confirmed_publications(row)
    from executor.executor import _completion_status_with_errors
    assert _completion_status_with_errors(0, {"video": row}) == "error"


@pytest.mark.parametrize("row", [None, {}, {"comment_id": "7699899442673468212"},
    {"engagement": {"main": {"comment_id": "7699899442673468212", "network_confirmed": True, "posted": False}}},
    {"engagement": {"main": [], "replies": []}},
    {"engagement": {"replies": {"items": [{"network_confirmed": True, "reply_id": "undefined"}]}}}])
def test_receipt_recognition_rejects_parent_ids_and_malformed_results(row):
    assert not has_confirmed_publications(row)
    assert not has_submission_evidence(row)


def test_old_error_task_displays_partial_without_rewriting_history():
    from services.user_facing_errors import build_task_execution_summary
    from bot.handlers.orders import _failed_urls_for_retry, _overview_notice_lines
    row = partial_result().to_dict(mode="engagement_only")
    row["status"] = "fail"  # Old recorded order 1247.
    task = {"id": 1247, "status": "error", "kind": "engagement_only", "video_urls": ["video"],
            "settings": {"replies": ["reply 1", "reply 2", "reply 3"], "reply_count": 3,
                         "failed_urls": ["video"], "url_results": {"video": row}}}
    assert build_task_execution_summary(task)["display_status"] == "partial"
    assert task["status"] == "error"
    assert _failed_urls_for_retry(task) == []
    assert "дубли" in "\n".join(_overview_notice_lines(task))


@pytest.mark.parametrize("explicit", [True, False])
def test_whole_video_retry_excludes_receipts_in_both_list_paths(explicit):
    from bot.handlers.orders import _failed_urls_for_retry
    rows = {"partial": {"status": "fail", "engagement": {"replies": {"items": [receipt()]}}},
            "unconfirmed": {"status": "fail", "engagement": {"replies": {"items": [{"submitted_unconfirmed": True}]}}},
            "safe": {"status": "fail", "engagement": {"replies": {"items": [{"success": False}]}}}}
    settings = {"url_results": rows}
    if explicit: settings["failed_urls"] = list(rows)
    assert _failed_urls_for_retry({"settings": settings}) == ["safe"]


@pytest.mark.asyncio
async def test_warmup_reports_runtime_profile_timeout_without_fake_readiness(monkeypatch):
    from executor.http import playwright_http_client
    url = "https://www.tiktok.com/@demo/video/7635267329407913230"
    cookies = [{"name": name, "value": name, "domain": ".tiktok.com"}
               for name in ("sessionid", "ttwid", "tt_csrf_token", "msToken")]
    page = SimpleNamespace(url=url, goto=AsyncMock(), wait_for_load_state=AsyncMock(),
        evaluate=AsyncMock(return_value={"current_url": url, "navigation_url": url, "age_ms": 5000, "dom_ready_ms": 500}),
        context=SimpleNamespace(cookies=AsyncMock(return_value=cookies), add_cookies=AsyncMock()))
    monkeypatch.setattr(engine, "wait_for_runtime_signer_ready", AsyncMock(return_value=True))
    monkeypatch.setattr(playwright_http_client, "_extract_runtime_profile", AsyncMock(side_effect=asyncio.TimeoutError))
    _, valid, info = await engine.warmup_page_for_http(page, url, max_retries=1)
    assert not valid
    assert info["reason_code"] == "warmup_runtime_profile_timeout"
