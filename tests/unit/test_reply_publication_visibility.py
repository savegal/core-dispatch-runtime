import asyncio
import copy
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock
from urllib.parse import parse_qs, urlparse

import pytest

from executor import reply_visibility as visibility

pytestmark = [pytest.mark.unit, pytest.mark.asyncio]
CID = "7697345157284266772"
NEW = str(int(CID) + 1)
OTHER = str(int(CID) + 2)
PARENT = "7697345157284266750"
PARENT2 = str(int(PARENT) + 1)
VIDEO = "7635267329407913230"
URL = f"https://www.tiktok.com/@demo/video/{VIDEO}"


def item(cid=CID, parent=PARENT, account=1, variant=1):
    return {"text": "hello", "reply_comment_id": cid, "parent_comment_id": parent,
            "account_id": account, "variant_index": variant, "success": True,
            "method": "http_user_schema", "reply_confirmation": "network_comment_id",
            "counts_as_submitted": True, "matched_main_text": "parent text"}


def result(*rows, expected=None):
    return {"status": "success", "core_status": "success", "publication_complete": True,
            "engagement": {"main": {}, "replies": {"items": list(rows), "ok": len(rows),
            "submitted": len(rows), "expected": expected if expected is not None else len(rows), "total": len(rows)}}}


def branch(*ids, parent=PARENT, more=0, cursor=20):
    return {"comments": [{"cid": cid, "reply_id": parent, "text": "hello"} for cid in ids],
            "has_more": more, "cursor": cursor}


@pytest.fixture
def adapter(monkeypatch):
    from executor import user_schema as schema, engagement_engine
    accounts = {i: SimpleNamespace(id=i, proxy=None, cookies=json.dumps({"uid_tt": str(i), "sessionid": f"session{i}"}))
                for i in (1, 2, 8, 9, 10, 20, 21)}
    selected = iter([accounts[i] for i in (8, 9, 20, 10, 21)])
    selector = Mock(side_effect=lambda *args, **kwargs: next(selected, None))
    monkeypatch.setattr(schema, "get_free_account_for_reply", selector)
    monkeypatch.setattr(schema, "_get_cached_http_preflight_username", lambda a: f"user{a.id}")
    monkeypatch.setattr(schema, "_preflight_cookie_http_gate", AsyncMock(return_value=None))
    monkeypatch.setattr(schema, "_ensure_account_login_ready", AsyncMock(return_value=(True, None)))
    monkeypatch.setattr(schema, "create_page_for_account", AsyncMock(return_value=(object(), SimpleNamespace(url=URL))))
    monkeypatch.setattr(schema, "_safe_close", AsyncMock())
    release = Mock()
    monkeypatch.setattr(schema, "release_reply_account_reservation", release)
    monkeypatch.setattr(engagement_engine, "warmup_page_for_http", AsyncMock(return_value=({}, True, {})))
    settings = visibility.get_settings()
    monkeypatch.setattr(settings, "REPLY_PUBLICATION_VISIBILITY_INITIAL_DELAY_SECONDS", 0)
    monkeypatch.setattr(settings, "REPLY_PUBLICATION_VISIBILITY_RECHECK_DELAY_SECONDS", 0)
    task = SimpleNamespace(id=12, settings={"verify_reply_visibility": True}, replies=["hello"], reply1="hello", reply2=None, reply3=None)
    db = SimpleNamespace(get=lambda model, key: accounts[key])
    publish = AsyncMock()
    monkeypatch.setattr(schema, "_reply_with_variant_on_all_mains", publish)
    scan = AsyncMock(return_value={"found_ids": [CID], "absent_ids": [], "complete": False, "reason": "all_found"})
    monkeypatch.setattr(visibility, "scan_reply_comment_ids", scan)
    snapshots = []
    monkeypatch.setattr(visibility, "save_reply_visibility_checkpoint", lambda db, task, url, row: snapshots.append(copy.deepcopy(row)))
    return SimpleNamespace(schema=schema, accounts=accounts, selector=selector, release=release,
                           task=task, db=db, publish=publish, scan=scan, snapshots=snapshots)


async def test_read_exact_id_in_exact_branch_and_paginate(monkeypatch):
    fetch = AsyncMock(side_effect=[branch(OTHER, more=1), branch(CID)])
    monkeypatch.setattr(visibility, "fetch_reply_page", fetch)
    report = await visibility.scan_reply_comment_ids(None, VIDEO, PARENT, [CID])
    assert report["found_ids"] == [CID] and report["absent_ids"] == []
    assert [c.args for c in fetch.await_args_list] == [(None, VIDEO, PARENT, 0), (None, VIDEO, PARENT, 20)]


@pytest.mark.parametrize("payload, reason", [
    ({"comments": [], "has_more": 1, "cursor": 20}, "page_limit"),
    ({"comments": []}, "missing_has_more"),
    ({"comments": [], "has_more": 1, "cursor": 0}, "cursor_loop"),
    ({"comments": None, "has_more": 0}, "invalid_comments"),
    ({"comments": [None], "has_more": 0}, "invalid_rows_or_parent"),
    (branch(CID, parent=PARENT2), "invalid_rows_or_parent"),
    ({"comments": [{"cid": CID}], "has_more": 0}, "invalid_rows_or_parent"),
])
async def test_incomplete_or_wrong_parent_never_authorizes_write(monkeypatch, payload, reason):
    monkeypatch.setattr(visibility, "fetch_reply_page", AsyncMock(return_value=payload))
    report = await visibility.scan_reply_comment_ids(None, VIDEO, PARENT, [CID], max_pages=1)
    assert report["reason"] == reason and not report["complete"] and not report["absent_ids"]


async def test_same_text_different_id_is_missing_only_after_complete_read(monkeypatch):
    monkeypatch.setattr(visibility, "fetch_reply_page", AsyncMock(return_value=branch(OTHER)))
    report = await visibility.scan_reply_comment_ids(None, VIDEO, PARENT, [CID])
    assert report["absent_ids"] == [CID] and report["complete"]


async def test_positive_survives_later_api_failure(monkeypatch):
    monkeypatch.setattr(visibility, "fetch_reply_page", AsyncMock(side_effect=[branch(CID, more=1), None]))
    page = SimpleNamespace(_reply_list_http_meta={"reason": "empty_body"})
    report = await visibility.scan_reply_comment_ids(page, VIDEO, PARENT, [CID, OTHER])
    assert report["found_ids"] == [CID] and not report["absent_ids"] and report["reason"] == "empty_body"


@pytest.mark.parametrize("key, value", [("verify_main_visibility", True), ("verify_reply_visibility", False)])
async def test_disabled_reply_mode_does_not_allocate_accounts(adapter, key, value):
    adapter.task.settings = {key: value}
    row = result(item())
    before = copy.deepcopy(row)
    await visibility.verify_order_reply_visibility(adapter.db, adapter.task, URL, row)
    assert row == before and not adapter.snapshots
    adapter.selector.assert_not_called()


@pytest.mark.parametrize("format", ["full", "engagement_only"])
async def test_visible_reply_is_confirmed_without_repost(adapter, format):
    row = result(item())
    row["mode"] = format
    if format == "full":
        row["engagement"]["main"] = {"variants_published": [{"comment_id": PARENT, "account_id": 2}]}
    if format == "engagement_only":
        entry = row["engagement"]["replies"]["items"][0]
        entry["reply_id"] = entry.pop("reply_comment_id")
    await visibility.verify_order_reply_visibility(adapter.db, adapter.task, URL, row, pool_owner_user_id=77)
    assert row[visibility.REPORT_KEY]["status"] == "confirmed" and row["status"] == "success"
    assert row["engagement"]["replies"]["items"][0]["public_confirmed"]
    adapter.publish.assert_not_awaited()
    adapter.release.assert_called_once_with(8)
    assert adapter.selector.call_args.kwargs["pool_owner_user_id"] == 77
    assert adapter.selector.call_args.kwargs["reserve"] is True
    assert 1 in adapter.selector.call_args.kwargs["exclude_ids"]
    assert adapter.snapshots[0][visibility.REPORT_KEY]["status"] == "checking"


@pytest.mark.parametrize("interrupt", [None, "cleanup", "stop"])
async def test_two_negative_reads_then_one_durable_replacement(adapter, interrupt):
    adapter.scan.side_effect = [
        {"found_ids": [], "absent_ids": [CID], "complete": True},
        {"found_ids": [], "absent_ids": [CID], "complete": True},
        {"found_ids": [NEW], "absent_ids": [], "complete": False},
    ]
    stopped = False
    async def publish(db, account, url, candidates, text, **kwargs):
        nonlocal stopped
        assert adapter.scan.await_count == 2
        assert candidates[0].comment_id == PARENT and text == "hello" and not kwargs["fallback_allowed"]
        outcome = adapter.schema.ReplyOutcome("hello", 1, "parent text", PARENT, True,
                    "http_user_schema", None, account.id, reply_comment_id=NEW, reply_confirmation="network_comment_id")
        kwargs["on_submitted_outcome"](outcome)
        assert adapter.snapshots[-1][visibility.REPORT_KEY]["pending_receipts"][0]["reply_comment_id"] == NEW
        if interrupt == "cleanup":
            raise TimeoutError("cleanup interrupted after successful POST")
        if interrupt == "stop":
            stopped = True
            await asyncio.sleep(5)
        return adapter.schema.ReplyAttemptResult(replies=[outcome])
    adapter.publish.side_effect = publish
    row = result(item())
    await visibility.verify_order_reply_visibility(adapter.db, adapter.task, URL, row, stop_requested=lambda: stopped)
    current = row["engagement"]["replies"]["items"][0]
    assert current["reply_comment_id"] == NEW and current["parent_comment_id"] == PARENT
    assert row[visibility.REPORT_KEY]["pending_receipts"][0]["replaces_comment_id"] == CID
    assert row[visibility.REPORT_KEY]["confirmed"] == (1 if interrupt is None else 0)
    assert row[visibility.REPORT_KEY]["replacements"] == 1
    adapter.publish.assert_awaited_once()
    released = [c.args[0] for c in adapter.release.call_args_list]
    assert released == ([8, 9, 20, 10] if interrupt is None else [8, 9, 20])


@pytest.mark.parametrize("count", [2, 4])
async def test_repeated_full_absence_stops_reply_adapter_and_saves_receipts(adapter, monkeypatch, count):
    for account_id in (3, 4, 11, 22, 23):
        adapter.accounts[account_id] = SimpleNamespace(
            id=account_id, proxy=None,
            cookies=json.dumps({"uid_tt": str(account_id), "sessionid": f"session{account_id}"}),
        )
    replacement_accounts = list(range(20, 20 + count))
    selection = iter([8, 9, *replacement_accounts, 10, 11])
    adapter.selector.side_effect = lambda *args, **kwargs: adapter.accounts[next(selection)]
    monkeypatch.setattr(visibility.get_settings(), "REPLY_PUBLICATION_VISIBILITY_REPAIR_ROUNDS", 3)

    async def scan(page, video, parent, ids, **kwargs):
        return {"found_ids": [], "absent_ids": list(ids), "complete": True}

    async def publish(db, account, url, candidates, text, **kwargs):
        outcome = adapter.schema.ReplyOutcome(
            text, 1, "parent text", PARENT, True, "http_user_schema", None, account.id,
            reply_comment_id=str(int(CID) + 100 + account.id), reply_confirmation="network_comment_id",
        )
        kwargs["on_submitted_outcome"](outcome)
        return adapter.schema.ReplyAttemptResult(replies=[outcome])

    adapter.scan.side_effect = scan
    adapter.publish.side_effect = publish
    original_ids = [str(int(CID) + index) for index in range(count)]
    row = result(*(item(cid, account=index + 1, variant=index + 1) for index, cid in enumerate(original_ids)))
    await visibility.verify_order_reply_visibility(adapter.db, adapter.task, URL, row)

    report = row[visibility.REPORT_KEY]
    assert report["reason"] == "repeated_full_absence"
    assert (report["confirmed"], report["missing"], report["unknown"]) == (0, count, 0)
    assert row["status"] == "submitted_unconfirmed"
    assert adapter.publish.await_count == report["replacements"] == count
    assert adapter.scan.await_count == 4
    assert [call.args[0] for call in adapter.release.call_args_list] == [8, 9, *replacement_accounts, 10, 11]
    assert [entry["comment_id"] for entry in report["history"]] == original_ids
    assert len(report["pending_receipts"]) == count
    assert adapter.snapshots[-1][visibility.REPORT_KEY]["reason"] == "repeated_full_absence"
    assert all(entry["parent_comment_id"] == PARENT for entry in report["items"])


async def test_api_failure_or_missing_parent_does_not_repost(adapter):
    adapter.scan.return_value = {"found_ids": [], "absent_ids": [], "complete": False, "reason": "empty_body"}
    row = result(item())
    await visibility.verify_order_reply_visibility(adapter.db, adapter.task, URL, row)
    assert row[visibility.REPORT_KEY]["status"] == "unconfirmed"
    assert row["error"] == "reply_visibility_unconfirmed"
    assert row["engagement"]["replies"]["submitted"] == 1
    adapter.publish.assert_not_awaited()


async def test_ambiguous_replacement_does_not_trigger_another_post(adapter):
    adapter.scan.return_value = {"found_ids": [], "absent_ids": [CID], "complete": True}
    adapter.publish.return_value = adapter.schema.ReplyAttemptResult(replies=[adapter.schema.ReplyOutcome(
        "hello", 1, "parent", PARENT, False, "http", "empty_body", 20,
        attempted=True, publication_attempted=True, submitted_unconfirmed=True,
    )])
    row = result(item())
    await visibility.verify_order_reply_visibility(adapter.db, adapter.task, URL, row)
    adapter.publish.assert_awaited_once()
    assert visibility.reply_id(row["engagement"]["replies"]["items"][0]) == CID
    assert row[visibility.REPORT_KEY]["status"] == "unconfirmed"


async def test_deadline_cancels_slow_observer_and_releases_lease(adapter, monkeypatch):
    import time
    monkeypatch.setattr(visibility.get_settings(), "REPLY_PUBLICATION_VISIBILITY_MAX_SECONDS", 1)
    adapter.schema._ensure_account_login_ready.side_effect = lambda *a, **k: None
    async def blocked(*args, **kwargs):
        await asyncio.sleep(30)
    adapter.schema._ensure_account_login_ready.side_effect = blocked
    started = time.monotonic()
    row = result(item())
    await visibility.verify_order_reply_visibility(adapter.db, adapter.task, URL, row)
    assert time.monotonic() - started < 2
    assert row[visibility.REPORT_KEY]["reason"] == "visibility_timeout"
    adapter.release.assert_called_once_with(8)
    adapter.publish.assert_not_awaited()


async def test_duplicate_cookie_import_not_independent(adapter):
    adapter.accounts[8].cookies = adapter.accounts[1].cookies
    adapter.accounts[9].cookies = adapter.accounts[1].cookies
    adapter.accounts[20].cookies = adapter.accounts[1].cookies
    adapter.accounts[10].cookies = adapter.accounts[1].cookies
    adapter.accounts[21].cookies = adapter.accounts[1].cookies
    row = result(item())
    await visibility.verify_order_reply_visibility(adapter.db, adapter.task, URL, row)
    assert row[visibility.REPORT_KEY]["confirmed"] == 0
    adapter.scan.assert_not_awaited()
    adapter.publish.assert_not_awaited()


async def test_one_browser_failure_releases_lease_and_next_observer_can_confirm(adapter, monkeypatch):
    adapter.schema.create_page_for_account.side_effect = [RuntimeError("fixture"), (object(), SimpleNamespace(url=URL))]
    row = result(item())
    await visibility.verify_order_reply_visibility(adapter.db, adapter.task, URL, row)
    assert row[visibility.REPORT_KEY]["status"] == "confirmed"
    assert [c.args[0] for c in adapter.release.call_args_list] == [8, 9]


async def test_batch_replies_under_same_parent_use_one_read(adapter):
    adapter.scan.return_value = {"found_ids": [CID, OTHER], "absent_ids": [], "complete": False}
    row = result(item(), item(OTHER, account=2, variant=2))
    await visibility.verify_order_reply_visibility(adapter.db, adapter.task, URL, row)
    adapter.scan.assert_awaited_once()
    assert adapter.scan.call_args.args[3] == [CID, OTHER]
    assert row[visibility.REPORT_KEY]["confirmed"] == 2


async def test_full_order_does_not_confirm_or_replace_reply_to_unrelated_parent(adapter):
    row = result(item(parent=PARENT2))
    row["mode"] = "full"
    row["engagement"]["main"] = {"variants_published": [{"comment_id": PARENT, "account_id": 2}]}
    await visibility.verify_order_reply_visibility(adapter.db, adapter.task, URL, row)
    assert row[visibility.REPORT_KEY]["status"] == "unconfirmed"
    assert row[visibility.REPORT_KEY]["invalid_targets"][0]["reason"] == "reply_parent_not_in_order"
    adapter.scan.assert_not_awaited()
    adapter.publish.assert_not_awaited()


async def test_legacy_full_result_checks_replies_from_every_main_variant(adapter):
    first, second = item(), item(OTHER, parent=PARENT2, account=2)
    row = result(first)
    row["mode"] = "full"
    row["engagement"]["main"] = {"comment_id": PARENT}
    row["main_variants"] = [{"comment_id": parent, "engagement": {
        "main": {"comment_id": parent}, "replies": {"items": [reply], "submitted": 1, "ok": 1}}}
        for parent, reply in ((PARENT, first), (PARENT2, second))]
    async def scan(page, video, parent, ids, **kwargs):
        return {"found_ids": ids, "absent_ids": [], "complete": False}
    adapter.scan.side_effect = scan
    await visibility.verify_order_reply_visibility(adapter.db, adapter.task, URL, row)
    assert row[visibility.REPORT_KEY]["expected"] == row[visibility.REPORT_KEY]["confirmed"] == 2
    assert {call.args[2] for call in adapter.scan.call_args_list} == {PARENT, PARENT2}
    assert len(row["engagement"]["replies"]["items"]) == 2


async def test_complete_branch_evidence_not_lost_when_other_branch_incomplete(adapter, monkeypatch):
    monkeypatch.setattr(visibility.get_settings(), "REPLY_PUBLICATION_VISIBILITY_OBSERVER_ATTEMPTS", 2)
    # Same observer checks two branches. Only the complete branch may be replaced.
    async def scan(page, video, parent, ids, **kwargs):
        if parent == PARENT:
            return {"found_ids": [], "absent_ids": ids, "complete": True}
        return {"found_ids": [], "absent_ids": [], "complete": False}
    adapter.scan.side_effect = scan
    async def unconfirmed_post(*args, **kwargs):
        kwargs["parent_search_progress_sink"]["publication_attempted"] = True
        return adapter.schema.ReplyAttemptResult()
    adapter.publish.side_effect = unconfirmed_post
    row = result(item(), item(OTHER, parent=PARENT2, account=2))
    await visibility.verify_order_reply_visibility(adapter.db, adapter.task, URL, row)
    adapter.publish.assert_awaited_once()
    assert adapter.publish.call_args.args[3][0].comment_id == PARENT


async def test_checkpoint_failure_stops_all_browser_actions(adapter, monkeypatch):
    monkeypatch.setattr(visibility, "save_reply_visibility_checkpoint", Mock(side_effect=RuntimeError("database unavailable")))
    row = result(item())
    await visibility.verify_order_reply_visibility(adapter.db, adapter.task, URL, row)
    assert row[visibility.REPORT_KEY]["reason"] == "checkpoint_failed"
    adapter.selector.assert_not_called()


async def test_missing_unpublished_variant_cannot_become_success(adapter):
    row = result(item(), expected=2)
    row.update(status="partial", core_status="partial", error="reply_publication_incomplete", publication_complete=False)
    await visibility.verify_order_reply_visibility(adapter.db, adapter.task, URL, row)
    assert row["status"] == "partial" and row[visibility.REPORT_KEY]["status"] == "unconfirmed"
    assert row[visibility.REPORT_KEY]["unknown"] == 1


async def test_rejected_duplicate_replacement_never_replaces_original_slot(adapter):
    adapter.scan.return_value = {"found_ids": [], "absent_ids": [CID, OTHER], "complete": True}
    async def duplicate(db, account, url, candidates, text, **kwargs):
        outcome = adapter.schema.ReplyOutcome("hello", 1, "parent text", PARENT, True, "http", None,
                    account.id, reply_comment_id=OTHER, reply_confirmation="network_comment_id")
        kwargs["on_submitted_outcome"](outcome)
        return adapter.schema.ReplyAttemptResult(replies=[outcome])
    adapter.publish.side_effect = duplicate
    row = result(item(), item(OTHER, account=2))
    await visibility.verify_order_reply_visibility(adapter.db, adapter.task, URL, row)
    assert [visibility.reply_id(r) for r in row["engagement"]["replies"]["items"]] == [CID, OTHER]
    assert row[visibility.REPORT_KEY]["replacements"] == 0


async def test_interrupted_replacement_cannot_restore_a_superseded_reply_id(adapter, monkeypatch):
    monkeypatch.setattr(visibility.get_settings(), "REPLY_PUBLICATION_VISIBILITY_REPAIR_ROUNDS", 3)
    for account_id in (11, 12, 13, 22):
        adapter.accounts[account_id] = SimpleNamespace(id=account_id, proxy=None,
            cookies=json.dumps({"uid_tt": str(account_id), "sessionid": f"session{account_id}"}))
    sequence = iter(adapter.accounts[i] for i in (8, 9, 20, 10, 11, 21, 12, 13, 22))
    adapter.selector.side_effect = lambda *args, **kwargs: next(sequence, None)
    adapter.scan.side_effect = [{"found_ids": [], "absent_ids": [cid], "complete": True}
                               for cid in (CID, CID, NEW, NEW, OTHER, OTHER)]
    receipts = iter((NEW, OTHER, NEW))
    async def publish(db, account, url, candidates, text, **kwargs):
        cid = next(receipts)
        outcome = adapter.schema.ReplyOutcome("hello", 1, "parent", PARENT, True, "http", None,
                    account.id, reply_comment_id=cid, reply_confirmation="network_comment_id")
        kwargs["on_submitted_outcome"](outcome)
        if account.id == 22:
            raise TimeoutError("interrupted cleanup with a previously superseded ID")
        return adapter.schema.ReplyAttemptResult(replies=[outcome])
    adapter.publish.side_effect = publish
    row = result(item())
    await visibility.verify_order_reply_visibility(adapter.db, adapter.task, URL, row)
    assert row["engagement"]["replies"]["items"][0]["reply_comment_id"] == OTHER
    assert row[visibility.REPORT_KEY]["replacements"] == 2
    assert row[visibility.REPORT_KEY]["status"] == "unconfirmed"


async def test_empty_order_mode_no_account_allocation(adapter):
    adapter.task.replies = []
    adapter.task.reply1 = None
    row = result()
    await visibility.verify_order_reply_visibility(adapter.db, adapter.task, URL, row)
    assert row[visibility.REPORT_KEY]["status"] == "not_requested"
    adapter.selector.assert_not_called()


async def test_fresh_reply_get_has_branch_query_no_cache_and_requires_signature(monkeypatch):
    from executor.http import comment_discovery as api
    monkeypatch.setattr(api, "_extract_runtime_profile", AsyncMock(return_value={}))
    response = {"status": 200, "text": json.dumps({"status_code": 0, **branch(CID)}),
                "url": "https://www.tiktok.com/api/comment/list/reply/?X-Bogus=x&X-Gnarly=y"}
    page = SimpleNamespace(context=SimpleNamespace(cookies=AsyncMock(return_value=[])), evaluate=AsyncMock(return_value=response))
    assert await api.fetch_reply_page(page, VIDEO, PARENT)
    script, url = page.evaluate.call_args.args
    assert 'cache: "no-store"' in script and urlparse(url).path == "/api/comment/list/reply/"
    query = parse_qs(urlparse(url).query)
    assert query["item_id"] == [VIDEO] and query["comment_id"] == [PARENT]
    monkeypatch.setattr(api.asyncio, "sleep", AsyncMock())
    response["url"] = "https://www.tiktok.com/api/comment/list/reply/"
    assert await api.fetch_reply_page(page, VIDEO, PARENT) is None
    assert page._reply_list_http_meta["reason"] == "unsigned_response"


@pytest.mark.parametrize("kind", ["full", "engagement_only"])
@pytest.mark.parametrize("value, expected", [(True, True), (False, False), ("false", False), ("1", True)])
async def test_api_normalizes_independent_reply_toggle(kind, value, expected):
    from api.tasks import TaskCreate
    task = TaskCreate(user_id=1, main_comment="hello", text_variants=["a", "b", "c"], video_urls=[URL],
                      kind=kind, engagement_comment_id=PARENT, settings={"verify_reply_visibility": value})
    assert task.settings["verify_reply_visibility"] is expected


async def test_independent_keyboard_toggles_preserve_each_other():
    from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton
    from bot.services.main_visibility_ui import visibility_keyboard, set_visibility_option, set_reply_visibility_option
    markup = visibility_keyboard(InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="Go", callback_data="go")]]), True, False)
    state = SimpleNamespace(update_data=AsyncMock())
    callback = SimpleNamespace(data="order:reply_visibility:1", answer=AsyncMock(),
                message=SimpleNamespace(reply_markup=markup, edit_reply_markup=AsyncMock()))
    await set_reply_visibility_option(callback, state)
    state.update_data.assert_awaited_with(verify_reply_visibility=True)
    markup = callback.message.edit_reply_markup.call_args.kwargs["reply_markup"]
    assert [r[0].callback_data for r in markup.inline_keyboard] == ["go", "order:main_visibility:0", "order:reply_visibility:0"]
    callback.message.reply_markup = markup
    callback.data = "order:main_visibility:0"
    await set_visibility_option(callback, state)
    markup = callback.message.edit_reply_markup.call_args.kwargs["reply_markup"]
    assert [r[0].callback_data for r in markup.inline_keyboard] == ["go", "order:main_visibility:1", "order:reply_visibility:0"]


async def test_preview_buttons_default_off_and_keep_explicit_options():
    from bot.handlers.quick_order import _build_new_template_preview_text_and_keyboard
    from bot.keyboards.templates import get_template_preview_keyboard
    _, keyboard = _build_new_template_preview_text_and_keyboard(["main"], {1: "reply"}, 1, [URL])
    buttons = [b.callback_data for row in keyboard.inline_keyboard for b in row]
    assert "order:main_visibility:1" in buttons and "order:reply_visibility:1" in buttons
    keyboard = get_template_preview_keyboard(12, reply_count=1, verify_reply_visibility=True)
    buttons = [b.callback_data for row in keyboard.inline_keyboard for b in row]
    assert "order:main_visibility:1" in buttons and "order:reply_visibility:0" in buttons


async def test_replies_only_confirmation_sends_toggle_to_api(monkeypatch):
    from bot.handlers import engagement_wizard as wizard
    monkeypatch.setattr(wizard, "get_user_backend_id", AsyncMock(return_value=7))
    monkeypatch.setattr(wizard, "_boost_likes_ui_config", AsyncMock(return_value=(False, 0)))
    create = AsyncMock(return_value={"task_id": 13})
    monkeypatch.setattr(wizard.api_client, "create_task", create)
    state = SimpleNamespace(get_data=AsyncMock(return_value={"video_url": URL, "comment_text": "parent",
        "replies": ["hello"], "account_pool_mode": "shared", "verify_reply_visibility": True}), clear=AsyncMock())
    callback = SimpleNamespace(from_user=SimpleNamespace(id=123), answer=AsyncMock(), message=SimpleNamespace(edit_text=AsyncMock()))
    await wizard.confirm_engagement_order(callback, state)
    create.assert_awaited_once()
    assert create.call_args.kwargs["settings"]["verify_reply_visibility"] is True
    assert create.call_args.kwargs["kind"] == "engagement_only"
    state.clear.assert_awaited_once()


async def test_reply_mode_receipts_are_not_success_and_prevent_blind_resume():
    from executor.executor import _url_result_counts_as_success, _visibility_result_has_receipts, _visibility_resume_error
    row = result(item())
    row[visibility.REPORT_KEY] = {"enabled": True, "status": "checking"}
    assert _visibility_result_has_receipts(row) and not _url_result_counts_as_success(row)
    assert _visibility_resume_error(row) == "reply_visibility_unconfirmed"
    row[visibility.REPORT_KEY]["status"] = "confirmed"
    assert _url_result_counts_as_success(row)


async def test_public_counts_override_network_receipts(adapter):
    from services.task_execution_ledger import build_url_execution_ledger
    from services.user_facing_errors import humanize_error_text
    row = result(item())
    row["engagement"]["replies"]["items"][0]["public_confirmed"] = False
    row["engagement"]["replies"]["ok"] = 0
    ledger = build_url_execution_ledger(row)
    assert ledger["summary"]["replies"]["confirmed"] == 0
    assert ledger["summary"]["replies"]["submitted"] == 1
    assert "не подтверждена" in humanize_error_text("reply_visibility_unconfirmed", short=True)


async def test_unconfirmed_public_check_cannot_appear_as_completed_task():
    from services.user_facing_errors import build_task_execution_summary
    from services.task_execution_ledger import build_url_execution_ledger
    row = result({**item(), "public_confirmed": False})
    row.update(status="submitted_unconfirmed", error="reply_visibility_unconfirmed")
    row[visibility.REPORT_KEY] = {"enabled": True, "status": "unconfirmed", "expected": 1, "confirmed": 0}
    assert not build_url_execution_ledger(row)["success_like"]
    task = {"status": "error", "video_urls": [URL], "settings": {"url_results": {URL: row}}}
    summary = build_task_execution_summary(task)
    assert summary["display_status"] == "failed" and summary["videos"]["success"] == 0


async def test_old_cached_receipt_success_is_rebuilt_for_visibility_mode():
    from services.task_execution_ledger import build_task_execution_ledger
    row = result({**item(), "public_confirmed": False})
    row[visibility.REPORT_KEY] = {"enabled": True, "status": "checking"}
    row["execution_ledger"] = {"version": 3, "success_like": True, "summary": {}}
    ledger = build_task_execution_ledger({URL: row})
    assert ledger["videos"]["success_like"] == 0 and ledger["videos"]["failed"] == 1


async def test_order_map_does_not_call_hidden_reply_confirmed():
    from bot.handlers.orders import _build_publication_map_rows, _render_order_settings
    row = result({**item(), "public_confirmed": False})
    row["engagement"]["main"] = {"variants_published": [{"text": "parent text", "comment_id": PARENT,
        "network_confirmed": True, "account_id": 2}]}
    row[visibility.REPORT_KEY] = {"enabled": True, "confirmed": 0, "expected": 1, "replacements": 2}
    task = {"id": 12, "kind": "full", "settings": {"verify_reply_visibility": True, "url_results": {URL: row}}}
    rows, _, _ = _build_publication_map_rows(task)
    assert "не подтверждена" in rows[0]["replies"][0]["status"]
    assert "0/1" in _render_order_settings(task) and "замен: 2" in _render_order_settings(task)


async def test_checkpoint_retains_other_urls_and_shortlink_key(db_session, sample_task):
    from db.models import Task
    from sqlalchemy.orm import Session
    sample_task.settings = {"verify_reply_visibility": True, "other": "keep", "url_results": {"other_url": {"status": "success"}}}
    db_session.commit()
    short = "https://vt.tiktok.com/original/"
    row = result(item())
    row[visibility.REPORT_KEY] = {"enabled": True, "status": "checking", "pending_receipts": [{"reply_comment_id": NEW}]}
    visibility.save_reply_visibility_checkpoint(db_session, sample_task, short, row)
    with Session(db_session.bind) as reader:
        saved = reader.get(Task, sample_task.id).settings
        assert saved["other"] == "keep" and saved["url_results"]["other_url"]["status"] == "success"
        assert saved["url_results"][short][visibility.REPORT_KEY]["pending_receipts"][0]["reply_comment_id"] == NEW
        assert saved["url_results"][short]["execution_ledger"]["success_like"] is False

