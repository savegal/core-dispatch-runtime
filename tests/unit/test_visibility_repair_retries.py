"""Offline replacement budgets and dispatch evidence for both visibility adapters."""
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from executor import engagement_engine, main_visibility, reply_visibility, user_schema as schema


pytestmark = [pytest.mark.unit, pytest.mark.asyncio]
CID = "7697345157284266772"
NEW = str(int(CID) + 1)
PARENT = "7697345157284266750"
URL = "https://www.tiktok.com/@demo/video/7635267329407913230"


@pytest.fixture(params=["main", "reply"])
def repair(request, monkeypatch):
    mode = request.param
    accounts = {i: SimpleNamespace(id=i, proxy=None, cookies=json.dumps({"uid_tt": str(i), "sessionid": f"session-{i}"}))
                for i in (1, 8, 9, 10, *range(20, 60))}
    state = SimpleNamespace(mode=mode, accounts=accounts, dead=set(), failures={}, published=False)
    candidates = iter(accounts[i] for i in range(20, 60))
    observers = iter(accounts[i] for i in (8, 9, 10))

    def select_reply(*args, **kwargs):
        if mode == "main" or not state.observed_quorum:
            return next(observers, None)
        return next(observers, None) if state.published else next(candidates, None)

    state.observed_quorum = False
    monkeypatch.setattr(schema, "get_free_account_for_reply", Mock(side_effect=select_reply))
    monkeypatch.setattr(schema, "get_free_account_for_main_comment", Mock(side_effect=lambda *a, **k: next(candidates, None)))
    monkeypatch.setattr(schema, "_get_cached_http_preflight_username", lambda a: f"user{json.loads(a.cookies)['uid_tt']}")
    monkeypatch.setattr(schema, "_get_cached_http_preflight_user_id", lambda a: json.loads(a.cookies)["uid_tt"])

    async def preflight(db, account, **kwargs):
        return "dead_session:http_preflight" if account.id in state.dead else None

    monkeypatch.setattr(schema, "_preflight_cookie_http_gate", AsyncMock(side_effect=preflight))

    async def create(account):
        return object(), SimpleNamespace(url=URL, account_id=account.id)

    monkeypatch.setattr(schema, "create_page_for_account", AsyncMock(side_effect=create))
    monkeypatch.setattr(schema, "_ensure_account_login_ready", AsyncMock(return_value=(True, None)))
    monkeypatch.setattr(schema, "_safe_close", AsyncMock())
    monkeypatch.setattr(schema, "_save_main_visibility_checkpoint", Mock())
    monkeypatch.setattr(reply_visibility, "save_reply_visibility_checkpoint", Mock())
    monkeypatch.setattr(engagement_engine, "warmup_page_for_http", AsyncMock(return_value=({}, True, {})))
    state.release_main, state.release_reply = Mock(), Mock()
    monkeypatch.setattr(schema, "release_main_account_reservation", state.release_main)
    monkeypatch.setattr(schema, "release_reply_account_reservation", state.release_reply)
    settings = schema._settings
    monkeypatch.setattr(settings, "TIKTOK_NETWORK_FIRST", True)
    monkeypatch.setattr(settings, "TIKTOK_NETWORK_IMPL_READY", True)
    monkeypatch.setattr(settings, "MAX_RETRY_ACCOUNTS", 30)
    for prefix in ("MAIN_VISIBILITY", "REPLY_PUBLICATION_VISIBILITY"):
        for suffix, value in (("INITIAL_DELAY_SECONDS", 0), ("RECHECK_DELAY_SECONDS", 0),
                              ("REPAIR_ROUNDS", 1), ("OBSERVER_ATTEMPTS", 6)):
            monkeypatch.setattr(settings, f"{prefix}_{suffix}", value)

    async def scan(page, *args, **kwargs):
        ids = args[-1]
        state.observed_quorum = page.account_id == 9 or state.observed_quorum
        return {"found_ids": list(ids) if state.published else [],
                "absent_ids": [] if state.published else list(ids), "complete": True}

    monkeypatch.setattr(main_visibility, "scan_main_comment_ids", AsyncMock(side_effect=scan))
    monkeypatch.setattr(reply_visibility, "scan_reply_comment_ids", AsyncMock(side_effect=scan))

    async def publish(db, account, url, *args, **kwargs):
        progress = kwargs["resolved_video_url_holder" if mode == "main" else "parent_search_progress_sink"]
        failure = state.failures.get(account.id)
        progress["publication_attempted"] = failure != "pre"
        if failure:
            reason = "goto_failed:fixture" if failure == "pre" else "empty_body"
            if mode == "main":
                return [], reason
            return schema.ReplyAttemptResult(fallback_error=reason)
        state.published = True
        if mode == "main":
            receipt = schema.PublishedMain("hello", NEW, True, False, "http", account.id, f"user{account.id}")
            kwargs["on_published"](receipt)
            return [receipt], None
        receipt = schema.ReplyOutcome("hello", 1, "parent", PARENT, True, "http", None, account.id,
                                      reply_comment_id=NEW, publication_attempted=True)
        kwargs["on_submitted_outcome"](receipt)
        return schema.ReplyAttemptResult(replies=[receipt])

    state.publish = AsyncMock(side_effect=publish)
    monkeypatch.setattr(schema, "_try_main_account" if mode == "main" else "_reply_with_variant_on_all_mains", state.publish)
    db = SimpleNamespace(get=lambda model, key: accounts[key])

    async def run():
        if mode == "main":
            result = schema.UserSchemaResult(published_mains=[schema.PublishedMain("hello", CID, True, False, "http", 1, "user1")])
            await schema._verify_order_main_visibility(db, SimpleNamespace(settings={"verify_main_visibility": True}),
                                                       URL, result, task_id=1, pool_owner_user_id=None)
            return result.main_visibility
        result = {"engagement": {"main": {}, "replies": {"expected": 1, "submitted": 1, "items": [{
            "text": "hello", "reply_comment_id": CID, "parent_comment_id": PARENT, "account_id": 1,
        }]}}}
        await reply_visibility.verify_order_reply_visibility(db, SimpleNamespace(id=1, settings={"verify_reply_visibility": True}, replies=["hello"]), URL, result)
        return result[reply_visibility.REPORT_KEY]

    state.run = run
    return state


async def test_dead_candidates_do_not_spend_six_browser_attempts(repair):
    repair.dead.update(range(20, 28))
    report = await repair.run()
    assert report["status"] == "confirmed"
    assert [call.args[1].id for call in repair.publish.await_args_list] == [28]
    release = repair.release_main if repair.mode == "main" else repair.release_reply
    assert set(range(20, 29)) <= {call.args[0] for call in release.call_args_list}


async def test_pre_publish_failure_retries_without_claiming_publisher_identity(repair):
    repair.failures[20] = "pre"
    repair.accounts[21].cookies = repair.accounts[20].cookies
    report = await repair.run()
    assert report["status"] == "confirmed"
    assert [call.args[1].id for call in repair.publish.await_args_list] == [20, 21]


async def test_duplicate_identities_do_not_spend_browser_attempts(repair):
    for account_id in range(20, 28):
        repair.accounts[account_id].cookies = repair.accounts[1].cookies
    report = await repair.run()
    assert report["status"] == "confirmed"
    assert [call.args[1].id for call in repair.publish.await_args_list] == [28]


async def test_dispatched_post_with_empty_response_never_retries(repair):
    repair.failures[20] = "post"
    report = await repair.run()
    assert report["status"] == "unconfirmed" and report["replacements"] == 0
    assert [call.args[1].id for call in repair.publish.await_args_list] == [20]


async def test_browser_retry_budget_remains_six_after_preflight_skips(repair):
    repair.dead.update(range(20, 28))
    repair.failures.update({i: "pre" for i in range(28, 34)})
    report = await repair.run()
    assert report["status"] == "unconfirmed"
    assert [call.args[1].id for call in repair.publish.await_args_list] == list(range(28, 34))


async def test_dead_candidates_stop_at_separate_account_limit(repair, monkeypatch):
    monkeypatch.setattr(schema._settings, "MAX_RETRY_ACCOUNTS", 10)
    repair.dead.update(range(20, 60))
    report = await repair.run()
    assert report["status"] == "unconfirmed"
    repair.publish.assert_not_awaited()
    release = repair.release_main if repair.mode == "main" else repair.release_reply
    assert {call.args[0] for call in release.call_args_list if call.args[0] >= 20} == set(range(20, 30))
