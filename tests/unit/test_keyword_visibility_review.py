"""Exercise keyword orders through the real optional visibility adapter."""
import copy

import pytest

from executor import reply_visibility as visibility
from tests.unit.test_reply_publication_visibility import adapter, item, result, CID, URL


pytestmark = [pytest.mark.unit, pytest.mark.asyncio]


def keyword_result(*rows, complete=True):
    row = result(*rows)
    row.update(mode="keyword_replies", keyword_replies={"complete": complete})
    if not complete:
        row.update(status="partial", core_status="partial", publication_complete=False,
                   error="keyword_scan_limit")
    return row


async def test_keyword_visibility_never_publishes_a_second_reply_to_the_same_parent(adapter, monkeypatch):
    monkeypatch.setenv("AUTO_BOOST_LIKES", "0")
    adapter.task.kind = "keyword_replies"
    adapter.scan.return_value = {"found_ids": [], "absent_ids": [CID], "complete": True}
    row = keyword_result(item())
    original = copy.deepcopy(row["engagement"]["replies"]["items"])
    for _ in range(2):
        await visibility.verify_order_reply_visibility(adapter.db, adapter.task, URL, row)
        adapter.publish.assert_not_awaited()
        assert row[visibility.REPORT_KEY]["replacements"] == 0
        assert row["publication_complete"] is False
        assert row["engagement"]["replies"]["items"][0]["reply_comment_id"] == original[0]["reply_comment_id"]
    adapter.publish.assert_not_awaited()


async def test_keyword_visibility_with_no_targets_does_not_expect_text_variant_count(adapter):
    adapter.task.kind = "keyword_replies"
    row = keyword_result()
    row.update(status="fail", core_status="fail", publication_complete=False, error="keyword_no_matches")
    await visibility.verify_order_reply_visibility(adapter.db, adapter.task, URL, row)
    assert row[visibility.REPORT_KEY] == {"enabled": True, "status": "not_requested", "expected": 0,
                                        "confirmed": 0, "replacements": 0}
    assert row["error"] == "keyword_no_matches"
    adapter.selector.assert_not_called()


@pytest.mark.parametrize("complete", [False, True])
async def test_keyword_public_visibility_keeps_scan_completion_separate(adapter, monkeypatch, complete):
    monkeypatch.setenv("AUTO_BOOST_LIKES", "0")
    adapter.task.kind = "keyword_replies"
    row = keyword_result(item(), complete=complete)
    await visibility.verify_order_reply_visibility(adapter.db, adapter.task, URL, row)
    assert row[visibility.REPORT_KEY]["status"] == "confirmed"
    assert row["status"] == ("success" if complete else "partial")
    assert row["publication_complete"] is complete
    adapter.publish.assert_not_awaited()
