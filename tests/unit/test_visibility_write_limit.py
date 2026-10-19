"""Offline checks that repeated complete absence stops additional publications."""
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from executor.main_visibility import verify_main_publications
from executor.reply_visibility import ReplySlot


pytestmark = [pytest.mark.unit, pytest.mark.asyncio]
BASE = 7697345157284266700


def publication(index, kind="main"):
    if kind == "reply":
        return ReplySlot({"reply_comment_id": str(BASE + index), "account_id": index,
                          "text": "fixture", "variant_index": index}, index)
    return SimpleNamespace(comment_id=str(BASE + index), account_id=index, text="fixture",
                           public_confirmed=False, variant_index=index)


async def negative(pending, round_index, attempt):
    return {"observer_id": 9000 + round_index * 100 + attempt,
            "found_ids": [], "absent_ids": [item.comment_id for item in pending],
            "complete": True, "api_check_attempted": True}


async def verify(items, observe, replace, **kwargs):
    return await verify_main_publications(items, observe=observe, replace=replace,
                                          initial_delay=0, recheck_delay=0, **kwargs)


@pytest.mark.parametrize("kind", ["main", "reply"])
@pytest.mark.parametrize("count", [2, 4])
@pytest.mark.parametrize("round_limit", [2, 3])
async def test_repeated_full_absence_stops_before_second_replacement_batch(kind, count, round_limit):
    items = [publication(i, kind) for i in range(1, count + 1)]
    originals = [item.comment_id for item in items]
    writes = []

    async def replace(item, round_index):
        assert round_index == 1
        writes.append(item.comment_id)
        return publication(100 + len(writes), kind), None

    observe = AsyncMock(side_effect=negative)
    report = await verify(items, observe, replace, repair_rounds=round_limit,
                          log_label="REPLY_VISIBILITY" if kind == "reply" else "MAIN_VISIBILITY")

    assert report["reason"] == "repeated_full_absence"
    assert report["status"] == "unconfirmed"
    assert (report["confirmed"], report["missing"], report["unknown"]) == (0, count, 0)
    assert report["replacements"] == len(writes) == count
    assert writes == originals
    assert observe.await_count == 4
    assert [row["round"] for row in report["round_summaries"]] == [0, 1]
    assert all(row["missing"] == count and row["unknown"] == 0 for row in report["round_summaries"])
    assert [row["comment_id"] for row in report["history"]] == originals
    assert {row["replaced_by"] for row in report["history"]} == {item.comment_id for item in items}
    assert all(len(row["absent_observer_ids"]) == 2 for row in report["items"])


async def test_single_item_does_not_claim_batch_wide_absence():
    writes = AsyncMock(side_effect=[(publication(101), None), (publication(102), None)])
    report = await verify([publication(1)], negative, writes)
    assert report.get("reason") != "repeated_full_absence"
    assert writes.await_count == report["replacements"] == 2


async def test_partial_confirmation_does_not_claim_full_absence():
    async def observe(pending, round_index, attempt):
        evidence = await negative(pending, round_index, attempt)
        if round_index == 0:
            evidence["found_ids"] = [str(BASE + 1)]
            evidence["absent_ids"] = [cid for cid in evidence["absent_ids"] if cid != str(BASE + 1)]
        return evidence

    writes = AsyncMock(side_effect=[(publication(101), None), (publication(102), None)])
    report = await verify([publication(1), publication(2)], observe, writes)
    assert report["confirmed"] == 1 and report["replacements"] == 2
    assert report.get("reason") != "repeated_full_absence"


@pytest.mark.parametrize("failure", ["empty_body", "timeout", "page_limit"])
async def test_unavailable_replacement_reads_never_count_as_full_absence(failure):
    async def observe(pending, round_index, attempt):
        if round_index == 0:
            return await negative(pending, round_index, attempt)
        return {"observer_id": 9200 + attempt, "reason": failure, "complete": False}

    writes = AsyncMock(side_effect=[(publication(101), None), (publication(102), None)])
    report = await verify([publication(1), publication(2)], observe, writes, observer_attempts=2)
    assert report["unknown"] == 2 and report["missing"] == 0
    assert report.get("reason") != "repeated_full_absence"
    assert writes.await_count == 2


async def test_repeated_or_author_observer_cannot_supply_negative_quorum():
    calls = 0

    async def observe(pending, round_index, attempt):
        nonlocal calls
        calls += 1
        evidence = await negative(pending, round_index, attempt)
        evidence["observer_id"] = 1 if attempt == 0 else 8
        return evidence

    writes = AsyncMock()
    report = await verify([publication(1), publication(2)], observe, writes)
    assert calls == 6
    assert report["unknown"] == 2
    assert report.get("reason") != "repeated_full_absence"
    writes.assert_not_awaited()


async def test_incomplete_negative_rows_do_not_count_as_full_absence():
    async def observe(pending, round_index, attempt):
        evidence = await negative(pending, round_index, attempt)
        evidence["complete"] = False
        return evidence

    writes = AsyncMock()
    report = await verify([publication(1), publication(2)], observe, writes, observer_attempts=2)
    assert report["unknown"] == 2 and report["missing"] == 0
    assert report.get("reason") != "repeated_full_absence"
    writes.assert_not_awaited()


async def test_failed_replacement_is_not_labeled_as_a_complete_new_batch():
    writes = AsyncMock(side_effect=[(publication(101), None), (None, "no_network_response")])
    report = await verify([publication(1), publication(2)], negative, writes, repair_rounds=1)
    assert report["replacements"] == 1 and report["missing"] == 2
    assert report.get("reason") != "repeated_full_absence"
    assert report["history"][1]["replacement_failed"]


async def test_visible_first_replacement_prevents_full_absence_stop():
    async def observe(pending, round_index, attempt):
        evidence = await negative(pending, round_index, attempt)
        if round_index == 1:
            evidence["found_ids"] = [str(BASE + 101)]
            evidence["absent_ids"] = [cid for cid in evidence["absent_ids"] if cid != str(BASE + 101)]
        elif round_index == 2:
            evidence["found_ids"], evidence["absent_ids"] = evidence["absent_ids"], []
        return evidence

    writes = AsyncMock(side_effect=[(publication(101), None), (publication(102), None), (publication(103), None)])
    report = await verify([publication(1), publication(2)], observe, writes)
    assert report["status"] == "confirmed" and report["confirmed"] == 2
    assert report.get("reason") != "repeated_full_absence"


async def test_disabled_repairs_preserve_receipts_without_writes():
    writes = AsyncMock()
    items = [publication(1), publication(2)]
    ids = [item.comment_id for item in items]
    report = await verify(items, negative, writes, repair_rounds=0)
    assert report["missing"] == 2 and report["replacements"] == 0
    assert [item.comment_id for item in items] == ids
    writes.assert_not_awaited()
