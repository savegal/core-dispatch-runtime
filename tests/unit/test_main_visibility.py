from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from executor import main_visibility as visibility

pytestmark = [pytest.mark.unit, pytest.mark.asyncio]
CID = "7697345157284266772"
OTHER = str(int(CID) + 1)
NEW = str(int(CID) + 2)


def main(cid=CID, account=1, text="same text"):
    return SimpleNamespace(comment_id=cid, account_id=account, text=text,
                           variant_index=3, public_confirmed=False)


def evidence(observer, *, found=(), absent=(), complete=False, reason="ok"):
    return {"observer_id": observer, "found_ids": list(found), "absent_ids": list(absent),
            "complete": complete, "reason": reason}


async def check(mains, observe, replace=None, **kwargs):
    return await visibility.verify_main_publications(
        mains, observe=observe, replace=replace or AsyncMock(),
        initial_delay=0, recheck_delay=0, **kwargs,
    )


async def test_exact_id_not_same_text_or_cached_page(monkeypatch):
    page = SimpleNamespace(_cached_first_comment_page={"comments": [{"cid": CID}]})
    fetch = AsyncMock(return_value={"comments": [{"cid": OTHER, "text": "same text"}], "has_more": 0})
    monkeypatch.setattr(visibility, "fetch_comment_page", fetch)
    result = await visibility.scan_main_comment_ids(page, "video", [CID])
    assert result["found_ids"] == [] and result["absent_ids"] == [CID]
    fetch.assert_awaited_once_with(page, "video", 0, fresh=True)


async def test_scan_reads_next_page_and_ignores_reply_id(monkeypatch):
    fetch = AsyncMock(side_effect=[
        {"comments": [{"cid": CID, "reply_id": OTHER}], "has_more": 1, "cursor": 50},
        {"comments": [{"cid": CID, "reply_id": "0"}], "has_more": 0},
    ])
    monkeypatch.setattr(visibility, "fetch_comment_page", fetch)
    result = await visibility.scan_main_comment_ids(SimpleNamespace(), "video", [CID])
    assert result["found_ids"] == [CID] and not result["absent_ids"]
    assert fetch.await_args_list[1].args[-1] == 50


@pytest.mark.parametrize("payload,reason", [
    ({"comments": [], "has_more": 1, "cursor": 0}, "cursor_loop"),
    ({"comments": [], "has_more": 1, "cursor": 50}, "page_limit"),
    ({"comments": []}, "missing_has_more"),
    ({"comments": [None], "has_more": 0}, "invalid_rows"),
    ({"comments": None, "has_more": 0}, "invalid_comments"),
])
async def test_incomplete_scan_never_proves_absence(monkeypatch, payload, reason):
    monkeypatch.setattr(visibility, "fetch_comment_page", AsyncMock(return_value=payload))
    result = await visibility.scan_main_comment_ids(SimpleNamespace(), "v", [CID], max_pages=1)
    assert result["reason"] == reason
    assert result["absent_ids"] == [] and not result["complete"]


async def test_positive_ids_survive_later_api_failure(monkeypatch):
    monkeypatch.setattr(visibility, "fetch_comment_page", AsyncMock(side_effect=[
        {"comments": [{"cid": CID}], "has_more": 1, "cursor": 50}, None,
    ]))
    page = SimpleNamespace(_comment_list_http_meta={"reason": "empty_body"})
    result = await visibility.scan_main_comment_ids(page, "v", [CID, OTHER])
    assert result["found_ids"] == [CID] and result["absent_ids"] == []
    assert result["reason"] == "empty_body"


async def test_visible_comment_is_never_republished():
    publish = AsyncMock()
    mains = [main()]
    report = await check(mains, AsyncMock(return_value=evidence(9, found=[CID])), publish)
    assert report["status"] == "confirmed" and mains[0].public_confirmed
    publish.assert_not_awaited()


async def test_only_missing_physical_id_replaced_after_independent_quorum():
    mains = [main(), main(OTHER, account=2)]  # Identical texts, two physical slots.
    replacement = main(NEW, account=20)
    observe = AsyncMock(side_effect=[
        evidence(8, found=[CID], absent=[OTHER], complete=True),
        evidence(9, absent=[OTHER], complete=True),
        evidence(10, found=[NEW]),
    ])
    publish = AsyncMock(return_value=(replacement, None))
    report = await check(mains, observe, publish)
    assert report["confirmed"] == 2 and report["replacements"] == 1
    assert mains[0].comment_id == CID and mains[1].comment_id == NEW
    assert mains[1].variant_index == 3
    assert report["history"][0]["comment_id"] == OTHER
    assert report["history"][0]["absent_observer_ids"] == [8, 9]
    publish.assert_awaited_once()


@pytest.mark.parametrize("observer", [1, 8])
async def test_same_author_or_repeated_observer_cannot_authorize_replacement(observer):
    publish = AsyncMock()
    report = await check([main()], AsyncMock(return_value=evidence(observer, absent=[CID], complete=True)), publish)
    assert report["unknown"] == 1 and report["replacements"] == 0
    publish.assert_not_awaited()


async def test_authors_own_visibility_is_not_public_confirmation():
    report = await check([main()], AsyncMock(return_value=evidence(1, found=[CID])))
    assert report["confirmed"] == 0


@pytest.mark.parametrize("reason", ["empty_body", "timeout", "page_limit", "cursor_loop", "dead_session", "no_observer_accounts"])
async def test_unavailable_or_partial_read_never_triggers_write(reason):
    publish = AsyncMock()
    report = await check([main()], AsyncMock(return_value={"reason": reason}), publish)
    assert report["status"] == "unconfirmed" and report["unknown"] == 1
    publish.assert_not_awaited()


async def test_initial_failure_then_delayed_visibility_does_not_duplicate():
    observe = AsyncMock(side_effect=[{"reason": "empty_body"}, evidence(8, found=[CID])])
    publish = AsyncMock()
    report = await check([main()], observe, publish)
    assert report["confirmed"] == 1
    publish.assert_not_awaited()


async def test_repair_round_limit_is_enforced_and_all_receipts_retained():
    mains = [main()]
    observer_id = 100
    async def observe(pending, *_):
        nonlocal observer_id
        observer_id += 1
        return evidence(observer_id, absent=[p.comment_id for p in pending], complete=True)
    publish = AsyncMock(side_effect=[(main(OTHER, 2), None), (main(NEW, 3), None)])
    report = await check(mains, observe, publish, repair_rounds=2)
    assert publish.await_count == report["replacements"] == 2
    assert mains[0].comment_id == NEW and report["missing"] == 1
    assert [row["comment_id"] for row in report["history"]] == [CID, OTHER]


async def test_failed_or_ambiguous_replacement_stops_further_writes():
    observe = AsyncMock(side_effect=[evidence(8, absent=[CID], complete=True),
                                    evidence(9, absent=[CID], complete=True)])
    publish = AsyncMock(return_value=(None, "no_network_response"))
    report = await check([main()], observe, publish)
    assert report["status"] == "unconfirmed"
    assert report["history"][0]["replacement_failed"]
    assert publish.await_count == 1


@pytest.mark.parametrize("reason", ["no_replacement_accounts", "no_independent_replacement_accounts", "replacement_accounts_exhausted"])
async def test_account_selection_failure_can_retry_in_next_repair_round(reason):
    mains = [main()]

    async def observe(pending, round_index, attempt):
        return evidence(100 + round_index * 10 + attempt, absent=[m.comment_id for m in pending], complete=True)

    publish = AsyncMock(side_effect=[(None, reason), (main(NEW, 20), None)])
    report = await check(mains, observe, publish, repair_rounds=2)
    assert publish.await_count == 2
    assert [call.args[1] for call in publish.await_args_list] == [1, 2]
    assert report["replacements"] == 1 and mains[0].comment_id == NEW


async def test_stop_preserves_receipts_without_writes():
    observe, publish = AsyncMock(), AsyncMock()
    mains = [main()]
    report = await check(mains, observe, publish, stop_requested=lambda: True)
    assert report["reason"] == "stopped_by_user" and mains[0].comment_id == CID
    observe.assert_not_awaited()
    publish.assert_not_awaited()


async def test_observer_exception_is_inconclusive():
    publish = AsyncMock()
    report = await check([main()], AsyncMock(side_effect=RuntimeError("test")), publish)
    assert report["unknown"] == 1 and report["reason"].startswith("verification_exception")
    publish.assert_not_awaited()


async def test_http_receipt_cannot_be_serialized_as_public(monkeypatch):
    from executor import user_schema as schema
    monkeypatch.setattr(schema, "_user_schema_http_first_enabled", lambda _: True)
    monkeypatch.setattr(schema._settings, "TIKTOK_NETWORK_FIRST", True)
    monkeypatch.setattr(schema._settings, "TIKTOK_NETWORK_IMPL_READY", True)
    mains = [schema.PublishedMain("text", CID, True, False, "http_user_schema", 1, "author")]
    result = schema.UserSchemaResult(published_mains=mains, expected_main_publications=1)
    payload = result.to_dict()
    assert payload["status"] == "success"  # Delivery mode remains available.
    assert result.http_main_complete and not result.main_found_by_observer
    assert payload["engagement"]["main"]["visibility_status"] == "network_confirmed"
    assert not payload["engagement"]["main"]["verified"]
    assert not payload["engagement"]["main"]["local_confirmed"]
    result.main_visibility = {"enabled": True, "status": "unconfirmed"}
    result.main_found_by_observer = True  # A legacy completion flag is insufficient.
    payload = result.to_dict()
    assert payload["status"] == "submitted_unconfirmed"
    mains[0].public_confirmed = True
    assert result.to_dict()["status"] == "success"
    assert result.to_dict()["engagement"]["main"]["verified"]


async def test_ledger_reports_network_delivery_separately_from_public_visibility():
    from services.task_execution_ledger import build_url_execution_ledger
    # Exercise through the same serialized result consumed by the executor.
    from executor.user_schema import PublishedMain, UserSchemaResult
    result = UserSchemaResult(published_mains=[PublishedMain("text", CID, True, False, "http_user_schema", 1, "a")],
                              expected_main_publications=1)
    ledger = build_url_execution_ledger(result.to_dict())
    assert ledger["summary"]["main"]["verified"] == 0
    assert ledger["summary"]["main"]["network_confirmed"] == 1


async def test_disabled_mode_never_selects_an_observer(monkeypatch):
    from executor import user_schema as schema
    select = Mock(side_effect=AssertionError("disabled mode must not consume accounts"))
    monkeypatch.setattr(schema, "get_free_account_for_reply", select)
    await schema._verify_order_main_visibility(None, SimpleNamespace(settings={}), "video",
                                               schema.UserSchemaResult(), task_id=1, pool_owner_user_id=7)
    select.assert_not_called()


@pytest.mark.parametrize("value,expected", [(True, True), (False, False), ("false", False), ("true", True), (None, False)])
async def test_api_normalizes_visibility_mode(value, expected):
    from api.tasks import TaskCreate
    from pydantic import ValidationError
    data = dict(user_id=1, main_comment="main", text_variants=["a", "b", "c"],
                video_urls=["https://www.tiktok.com/@demo/video/7635267329407913230"],
                settings={"verify_main_visibility": value})
    if value is None:
        with pytest.raises(ValidationError):
            TaskCreate(**data)
    else:
        assert TaskCreate(**data).settings["verify_main_visibility"] is expected


async def test_replies_only_cannot_republish_someone_elses_parent():
    from api.tasks import TaskCreate
    from pydantic import ValidationError
    with pytest.raises(ValidationError, match="only for full orders"):
        TaskCreate(user_id=1, text_variants=["a", "b", "c"], kind="engagement_only",
                   engagement_comment_text="parent", settings={"verify_main_visibility": True},
                   video_urls=["https://www.tiktok.com/@demo/video/7635267329407913230"])


async def test_preview_switch_preserves_other_buttons_and_stores_choice():
    from bot.services.main_visibility_ui import visibility_keyboard, set_visibility_option, CALLBACK_PREFIX
    from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup
    original = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="Start", callback_data="start")]])
    markup = visibility_keyboard(original)
    callback = SimpleNamespace(data=CALLBACK_PREFIX + "1", answer=AsyncMock(),
                               message=SimpleNamespace(reply_markup=markup, edit_reply_markup=AsyncMock()))
    state = SimpleNamespace(update_data=AsyncMock())
    await set_visibility_option(callback, state)
    state.update_data.assert_awaited_once_with(verify_main_visibility=True)
    updated = callback.message.edit_reply_markup.await_args.kwargs["reply_markup"]
    assert updated.inline_keyboard[0][0].callback_data == "start"
    assert updated.inline_keyboard[1][0].callback_data == CALLBACK_PREFIX + "0"


async def test_resume_keeps_visibility_receipts_without_claiming_success():
    from executor.executor import _url_result_counts_as_success, _visibility_result_has_receipts
    row = {"status": "submitted_unconfirmed", "publication_complete": True,
           "main_visibility": {"enabled": True, "status": "unconfirmed"},
           "engagement": {"main": {"variants_published": [{"comment_id": CID, "network_confirmed": True}]}}}
    assert not _url_result_counts_as_success(row)
    assert _visibility_result_has_receipts(row)
    row["main_visibility"]["status"] = "confirmed"
    assert _url_result_counts_as_success(row)


@pytest.mark.parametrize("interrupted_cleanup", [False, True])
async def test_enabled_adapter_uses_independent_accounts_and_releases_reservations(monkeypatch, interrupted_cleanup):
    from executor import user_schema as schema, engagement_engine
    url = "https://www.tiktok.com/@demo/video/7635267329407913230"
    original = schema.PublishedMain("text", CID, True, False, "http_user_schema", 1, "writer")
    new = schema.PublishedMain("text", NEW, True, False, "http_user_schema", 20, "new_writer")
    result = schema.UserSchemaResult(published_mains=[original], expected_main_publications=1)
    accounts = [SimpleNamespace(id=i, proxy=None) for i in (8, 9, 10)]
    select = Mock(side_effect=accounts)
    monkeypatch.setattr(schema, "get_free_account_for_reply", select)
    select_main = Mock(return_value=SimpleNamespace(id=20, proxy=None))
    monkeypatch.setattr(schema, "get_free_account_for_main_comment", select_main)
    monkeypatch.setattr(schema, "_get_cached_http_preflight_username", lambda a: f"user{a.id}")
    monkeypatch.setattr(schema, "_preflight_cookie_http_gate", AsyncMock(return_value=None))
    monkeypatch.setattr(schema, "_ensure_account_login_ready", AsyncMock(return_value=(True, None)))
    monkeypatch.setattr(schema, "create_page_for_account", AsyncMock(return_value=(object(), SimpleNamespace(url=url))))
    monkeypatch.setattr(engagement_engine, "warmup_page_for_http", AsyncMock(return_value=({}, True, {})))
    scans = AsyncMock(side_effect=[
        evidence(8, absent=[CID], complete=True), evidence(9, absent=[CID], complete=True), evidence(10, found=[NEW]),
    ])
    monkeypatch.setattr(visibility, "scan_main_comment_ids", scans)
    publish = AsyncMock(return_value=([new], None))
    if interrupted_cleanup:
        async def interrupted(*args, **kwargs):
            kwargs["on_published"](new)
            raise TimeoutError("cleanup after receipt timed out")
        publish.side_effect = interrupted
    monkeypatch.setattr(schema, "_try_main_account", publish)
    release_main, release_reply, close = Mock(), Mock(), AsyncMock()
    monkeypatch.setattr(schema, "release_main_account_reservation", release_main)
    monkeypatch.setattr(schema, "release_reply_account_reservation", release_reply)
    monkeypatch.setattr(schema, "_safe_close", close)
    monkeypatch.setattr(schema._settings, "MAIN_VISIBILITY_INITIAL_DELAY_SECONDS", 0)
    monkeypatch.setattr(schema._settings, "MAIN_VISIBILITY_RECHECK_DELAY_SECONDS", 0)
    await schema._verify_order_main_visibility(
        None, SimpleNamespace(settings={"verify_main_visibility": True}), url, result,
        task_id=123, pool_owner_user_id=77, excluded_account_ids=[2],
    )
    assert result.main_visibility["status"] == ("unconfirmed" if interrupted_cleanup else "confirmed")
    assert result.published_mains[0].comment_id == NEW
    assert result.main_account_id == 20
    assert result.main_found_by_observer is (not interrupted_cleanup)
    assert all(call.kwargs["reserve"] and call.kwargs["pool_owner_user_id"] == 77 for call in select.call_args_list)
    assert {1, 2, 8, 9} <= set(select_main.call_args.kwargs["exclude_ids"])
    assert select_main.call_args.kwargs["pool_owner_user_id"] == 77
    release_main.assert_called_once_with(20)
    assert release_reply.call_count == close.await_count == (2 if interrupted_cleanup else 3)
    assert result.main_visibility["history"][0]["comment_id"] == CID


async def test_observer_reservation_is_released_when_page_creation_fails(monkeypatch):
    from executor import user_schema as schema
    result = schema.UserSchemaResult(published_mains=[schema.PublishedMain("t", CID, True, False, "http", 1, "author")])
    monkeypatch.setattr(schema, "get_free_account_for_reply", Mock(side_effect=[SimpleNamespace(id=8), None]))
    monkeypatch.setattr(schema, "_preflight_cookie_http_gate", AsyncMock(return_value=None))
    monkeypatch.setattr(schema, "create_page_for_account", AsyncMock(side_effect=RuntimeError("fixture")))
    monkeypatch.setattr(schema, "_safe_close", AsyncMock())
    release = Mock()
    monkeypatch.setattr(schema, "release_reply_account_reservation", release)
    monkeypatch.setattr(schema._settings, "MAIN_VISIBILITY_INITIAL_DELAY_SECONDS", 0)
    await schema._verify_order_main_visibility(None, SimpleNamespace(settings={"verify_main_visibility": True}),
                                               "video", result, task_id=1, pool_owner_user_id=77)
    release.assert_called_once_with(8)
    assert result.error == "main_visibility_unconfirmed"
    assert result.main_visibility["unknown"] == 1


async def test_visibility_timeout_cancels_observer_and_does_not_publish():
    import asyncio
    cancelled = []
    async def observe(*_):
        try:
            await asyncio.sleep(30)
        finally:
            cancelled.append(True)
    publish = AsyncMock()
    report = await check([main()], observe, publish, max_seconds=1)
    assert report["reason"] == "visibility_timeout" and cancelled
    publish.assert_not_awaited()


@pytest.mark.parametrize("api_status", [None, False, "0", 0])
async def test_fresh_read_requires_success_api_status_and_disables_http_cache(monkeypatch, api_status):
    import json
    from executor.http import comment_discovery as api
    monkeypatch.setattr(api, "_extract_runtime_profile", AsyncMock(return_value={}))
    body = {"comments": [], "has_more": 0}
    if api_status is not None:
        body["status_code"] = api_status
    page = SimpleNamespace(context=SimpleNamespace(cookies=AsyncMock(return_value=[])), evaluate=AsyncMock(return_value={
        "status": 200, "text": json.dumps(body),
        "url": "https://www.tiktok.com/api/comment/list/?X-Bogus=x&X-Gnarly=y",
    }))
    result = await api.fetch_comment_page(page, "video", fresh=True)
    if type(api_status) is int:
        assert result["comments"] == []
    else:
        assert result is None and page._comment_list_http_meta["reason"] == "missing_api_status"
    assert 'cache: "no-store"' in page.evaluate.await_args.args[0]


async def test_order_ui_does_not_count_api_receipts_as_public_confirmation():
    from bot.handlers.orders import _main_publication_status, _render_order_settings
    from services.user_facing_errors import build_task_execution_summary, humanize_error_text, humanize_warning
    variant = {"text": "hello", "comment_id": CID, "network_confirmed": True, "public_confirmed": False}
    task = {"id": 1, "settings": {"verify_main_visibility": True, "url_results": {"url": {
        "status": "submitted_unconfirmed", "main_visibility": {
            "enabled": True, "confirmed": 0, "expected": 1, "replacements": 2,
        }, "engagement": {"main": {"posted": True, "expected": 1, "variants_published": [variant]}}
    }}}}
    assert build_task_execution_summary(task)["main"]["verified_count"] == 0
    assert "не подтверждена" in _main_publication_status(variant)
    assert "0/1; замен: 2" in _render_order_settings(task)
    assert "не подтверждена" in humanize_error_text("main_visibility_unconfirmed", short=True)
    assert "Ответы не запускались" in humanize_warning({"code": "main_visibility_unconfirmed"})


async def test_stop_interrupts_inflight_observer_promptly():
    import asyncio
    stopped = False
    closed = []
    async def observe(*_):
        nonlocal stopped
        stopped = True
        try:
            await asyncio.sleep(30)
        finally:
            closed.append(True)
    report = await asyncio.wait_for(check([main()], observe, stop_requested=lambda: stopped, max_seconds=5), timeout=0.8)
    assert report["reason"] == "stopped_by_user" and closed


async def test_duplicate_original_ids_do_not_fill_two_slots():
    observe = AsyncMock(return_value=evidence(8, found=[CID]))
    publish = AsyncMock()
    report = await check([main(), main(account=2)], observe, publish)
    assert report["status"] == "unconfirmed" and report["reason"] == "duplicate_comment_ids"
    observe.assert_not_awaited()
    publish.assert_not_awaited()


async def test_strict_mode_does_not_accept_legacy_dom_completion_flag(monkeypatch):
    from executor import user_schema as schema
    monkeypatch.setattr(schema, "_user_schema_http_first_enabled", lambda _: False)
    monkeypatch.setattr(schema, "http_first_enabled", lambda _: False)
    result = schema.UserSchemaResult(
        published_mains=[schema.PublishedMain("text", CID, True, True, "hybrid_dom", 1, "author")],
        expected_main_publications=1, main_found_by_observer=True,
        main_visibility={"enabled": True, "status": "unconfirmed"},
    )
    payload = result.to_dict()
    assert not payload["engagement"]["main"]["verified"]
    assert payload["status"] == "submitted_unconfirmed"


async def test_rejected_duplicate_receipt_is_not_restored_by_adapter(monkeypatch):
    from executor import user_schema as schema
    mains = [schema.PublishedMain("a", CID, True, False, "http", 1, "writer1"),
             schema.PublishedMain("b", OTHER, True, False, "http", 2, "writer2")]
    result = schema.UserSchemaResult(published_mains=mains, expected_main_publications=2)
    duplicate = schema.PublishedMain("a", OTHER, True, False, "http", 20, "writer20")
    monkeypatch.setattr(schema, "get_free_account_for_main_comment", Mock(return_value=SimpleNamespace(id=20)))
    monkeypatch.setattr(schema, "_preflight_cookie_http_gate", AsyncMock(return_value=None))
    monkeypatch.setattr(schema, "_get_cached_http_preflight_username", lambda _: "writer20")
    monkeypatch.setattr(schema, "_try_main_account", AsyncMock(return_value=([duplicate], None)))
    monkeypatch.setattr(schema, "release_main_account_reservation", Mock())
    async def rejected(mains, **kwargs):
        await kwargs["replace"](mains[0], 1)
        return {"enabled": True, "status": "unconfirmed", "checks": [], "history": [{"replacement_failed": True}],
                "replacements": 0, "confirmed": 0, "missing": 2, "unknown": 0,
                "items": [{"status": "not_found"}, {"status": "not_found"}]}
    monkeypatch.setattr(visibility, "verify_main_publications", rejected)
    await schema._verify_order_main_visibility(None, SimpleNamespace(settings={"verify_main_visibility": True}),
                                               "video", result, task_id=1, pool_owner_user_id=None)
    assert [m.comment_id for m in result.published_mains] == [CID, OTHER]
    assert result.main_visibility["replacements"] == 0


async def test_receipts_are_committed_before_verification_starts(monkeypatch, db_session, sample_task):
    from executor import user_schema as schema
    from executor.executor import _visibility_result_has_receipts
    from db.models import Task
    from sqlalchemy.orm import Session
    short = "https://vt.tiktok.com/original/"
    canonical = "https://www.tiktok.com/@demo/video/7635267329407913230"
    sample_task.settings = {"verify_main_visibility": True, "other_setting": "keep", "url_results": {"other": {"status": "success"}}}
    sample_task.status = "running"
    db_session.commit()
    result = schema.UserSchemaResult(published_mains=[schema.PublishedMain("a", CID, True, False, "http", 1, "writer")],
                                     expected_main_publications=1)
    replacement = schema.PublishedMain("a", NEW, True, False, "http", 20, "writer20")
    monkeypatch.setattr(schema, "get_free_account_for_main_comment", Mock(return_value=SimpleNamespace(id=20)))
    monkeypatch.setattr(schema, "_preflight_cookie_http_gate", AsyncMock(return_value=None))
    monkeypatch.setattr(schema, "_get_cached_http_preflight_username", lambda _: "writer20")
    monkeypatch.setattr(schema, "release_main_account_reservation", Mock())

    async def receipt_before_cleanup(*args, **kwargs):
        kwargs["on_published"](replacement)
        with Session(db_session.bind) as reader:
            row = reader.get(Task, sample_task.id).settings["url_results"][short]
            assert row["main_visibility"]["pending_receipts"][0]["comment_id"] == NEW
        raise TimeoutError("interrupted cleanup")

    monkeypatch.setattr(schema, "_try_main_account", receipt_before_cleanup)
    async def inspect_committed(mains, **kwargs):
        with Session(db_session.bind) as reader:
            saved = reader.get(Task, sample_task.id).settings
            assert saved["other_setting"] == "keep" and saved["url_results"]["other"]["status"] == "success"
            assert canonical not in saved["url_results"]
            row = saved["url_results"][short]
            assert row["main_visibility"]["status"] == "checking"
            assert _visibility_result_has_receipts(row)
        with pytest.raises(TimeoutError):
            await kwargs["replace"](mains[0], 1)
        return {"enabled": True, "status": "unconfirmed", "checks": [], "history": [], "replacements": 0,
                "reason": "visibility_timeout",
                "confirmed": 0, "missing": 0, "unknown": 1, "items": [{"status": "inconclusive"}]}
    monkeypatch.setattr(visibility, "verify_main_publications", inspect_committed)
    await schema._verify_order_main_visibility(db_session, sample_task, canonical, result, task_id=sample_task.id,
                                               pool_owner_user_id=None, checkpoint_url=short)
    assert sample_task.status == "running"
    with Session(db_session.bind) as reader:
        row = reader.get(Task, sample_task.id).settings["url_results"][short]
        assert row["engagement"]["main"]["variants_published"][0]["comment_id"] == NEW


async def test_checkpoint_failure_prevents_more_reads_and_writes(monkeypatch):
    from executor import user_schema as schema
    result = schema.UserSchemaResult(published_mains=[schema.PublishedMain("t", CID, True, False, "http", 1, "writer")])
    monkeypatch.setattr(schema, "_save_main_visibility_checkpoint", Mock(side_effect=RuntimeError("storage unavailable")))
    observer, writer = Mock(), Mock()
    monkeypatch.setattr(schema, "get_free_account_for_reply", observer)
    monkeypatch.setattr(schema, "get_free_account_for_main_comment", writer)
    await schema._verify_order_main_visibility(None, SimpleNamespace(settings={"verify_main_visibility": True}),
                                               "video", result, task_id=1, pool_owner_user_id=None)
    assert result.main_visibility["reason"] == "visibility_checkpoint_failed"
    assert result.error == "main_visibility_unconfirmed"
    observer.assert_not_called()
    writer.assert_not_called()


async def test_nine_ids_are_checked_in_one_http_page(monkeypatch):
    ids = [str(int(CID) + i) for i in range(9)]
    fetch = AsyncMock(return_value={"comments": [{"cid": cid} for cid in ids], "has_more": 1, "cursor": 50})
    monkeypatch.setattr(visibility, "fetch_comment_page", fetch)
    result = await visibility.scan_main_comment_ids(SimpleNamespace(), "video", ids)
    assert set(result["found_ids"]) == set(ids)
    assert fetch.await_count == 1


async def test_saved_confirmed_mains_do_not_report_reply_failure_as_visibility_failure():
    from executor.executor import _visibility_resume_error
    row = {"main_visibility": {"enabled": True, "status": "confirmed"}, "error": "reply_publication_incomplete"}
    assert _visibility_resume_error(row) == "reply_publication_incomplete"
    row["main_visibility"]["status"] = "unconfirmed"
    assert _visibility_resume_error(row) == "main_visibility_unconfirmed"
