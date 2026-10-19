"""Account and API budgets for the visibility failures in tasks 1225/1226."""
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from executor import engagement_engine, main_visibility, reply_visibility, user_schema as schema
from executor.http import comment_discovery as api


pytestmark = [pytest.mark.unit, pytest.mark.asyncio]
VIDEO = "7635267329407913230"
CID = "7697345157284266772"
PARENT = "7697345157284266750"
URL = f"https://www.tiktok.com/@demo/video/{VIDEO}"


def publication():
    return SimpleNamespace(comment_id=CID, account_id=1, text="text", variant_index=1, public_confirmed=False)


async def test_preflight_skips_allow_six_real_api_checks():
    failures = [{"observer_id": i, "api_check_attempted": False, "reason": "dead_session:http_preflight"} for i in range(8, 12)]
    checks = [{"observer_id": i, "api_check_attempted": True, "reason": "empty_body"} for i in range(12, 18)]
    observe = AsyncMock(side_effect=failures + checks)
    publish = AsyncMock()
    report = await main_visibility.verify_main_publications(
        [publication()], observe=observe, replace=publish, observer_attempts=6,
        max_candidate_accounts=30, initial_delay=0, recheck_delay=0,
    )
    assert observe.await_count == report["observer_candidates"] == 10
    assert report["observer_attempts"] == 6
    assert [row["observer_attempt"] for row in report["checks"]] == [None] * 4 + list(range(1, 7))
    assert report["unknown"] == 1 and report["missing"] == 0
    publish.assert_not_awaited()


async def test_all_dead_candidates_stop_at_separate_account_limit():
    observe = AsyncMock(return_value={"api_check_attempted": False, "reason": "dead_session:http_preflight"})
    publish = AsyncMock()
    report = await main_visibility.verify_main_publications(
        [publication()], observe=observe, replace=publish,
        max_candidate_accounts=30, initial_delay=0, recheck_delay=0,
    )
    assert observe.await_count == report["observer_candidates"] == 30
    assert report["observer_attempts"] == 0
    assert report["reason"] == "observer_candidate_limit"
    assert report["unknown"] == 1
    publish.assert_not_awaited()


@pytest.mark.parametrize("mode", ["main", "reply"])
async def test_both_adapters_skip_dead_accounts_and_release_all_leases(monkeypatch, mode):
    accounts = {i: SimpleNamespace(id=i, proxy=None, cookies=json.dumps({"uid_tt": str(i), "sessionid": f"session-{i}"}))
                for i in [1, *range(8, 18)]}
    selected = iter(accounts[i] for i in range(8, 18))
    select = Mock(side_effect=lambda *args, **kwargs: next(selected, None))
    monkeypatch.setattr(schema, "get_free_account_for_reply", select)

    async def preflight(db, account, **kwargs):
        return "dead_session:http_preflight:Passport response did not confirm authentication" if account.id < 12 else None

    monkeypatch.setattr(schema, "_preflight_cookie_http_gate", AsyncMock(side_effect=preflight))
    monkeypatch.setattr(schema, "_get_cached_http_preflight_username", lambda account: f"user{account.id}")
    create = AsyncMock(return_value=(object(), SimpleNamespace(url=URL)))
    monkeypatch.setattr(schema, "create_page_for_account", create)
    monkeypatch.setattr(schema, "_ensure_account_login_ready", AsyncMock(return_value=(True, None)))
    monkeypatch.setattr(schema, "_safe_close", AsyncMock())
    release = Mock()
    monkeypatch.setattr(schema, "release_reply_account_reservation", release)
    monkeypatch.setattr(engagement_engine, "warmup_page_for_http", AsyncMock(return_value=({}, True, {})))
    monkeypatch.setattr(schema._settings, "TIKTOK_NETWORK_FIRST", True)
    monkeypatch.setattr(schema._settings, "TIKTOK_NETWORK_IMPL_READY", True)
    monkeypatch.setattr(schema._settings, "MAX_RETRY_ACCOUNTS", 10)
    for prefix in ("MAIN_VISIBILITY", "REPLY_PUBLICATION_VISIBILITY"):
        monkeypatch.setattr(schema._settings, f"{prefix}_INITIAL_DELAY_SECONDS", 0)
        monkeypatch.setattr(schema._settings, f"{prefix}_RECHECK_DELAY_SECONDS", 0)
        monkeypatch.setattr(schema._settings, f"{prefix}_OBSERVER_ATTEMPTS", 6)
    db = SimpleNamespace(get=lambda model, key: accounts[key])
    scan = AsyncMock(return_value={"found_ids": [], "absent_ids": [], "complete": False, "reason": "empty_body"})
    if mode == "main":
        monkeypatch.setattr(main_visibility, "scan_main_comment_ids", scan)
        monkeypatch.setattr(schema, "_save_main_visibility_checkpoint", Mock())
        task = SimpleNamespace(id=1225, settings={"verify_main_visibility": True})
        result = schema.UserSchemaResult(published_mains=[schema.PublishedMain("text", CID, True, False, "http", 1, "user1")])
        await schema._verify_order_main_visibility(db, task, URL, result, task_id=1225, pool_owner_user_id=77)
        report = result.main_visibility
    else:
        monkeypatch.setattr(reply_visibility, "scan_reply_comment_ids", scan)
        monkeypatch.setattr(reply_visibility, "save_reply_visibility_checkpoint", Mock())
        task = SimpleNamespace(id=1226, settings={"verify_reply_visibility": True}, replies=["text"])
        result = {"status": "success", "engagement": {"main": {}, "replies": {"submitted": 1, "expected": 1, "items": [{
            "text": "text", "reply_comment_id": CID, "parent_comment_id": PARENT, "account_id": 1,
        }]}}}
        await reply_visibility.verify_order_reply_visibility(db, task, URL, result, pool_owner_user_id=77)
        report = result[reply_visibility.REPORT_KEY]
    assert select.call_count == release.call_count == 10
    assert {call.args[0] for call in release.call_args_list} == set(range(8, 18))
    assert create.await_count == scan.await_count == report["observer_attempts"] == 6
    assert report["observer_candidates"] == 10
    assert report["status"] == "unconfirmed" and report["unknown"] == 1
    assert all(row["api_check_attempted"] is False for row in report["checks"][:4])


@pytest.mark.parametrize("mode", ["main", "reply"])
@pytest.mark.parametrize("recover", [False, True])
async def test_scan_retries_twice_without_multiplying_retries_in_scanner(monkeypatch, mode, recover):
    monkeypatch.setattr(api, "_extract_runtime_profile", AsyncMock(return_value={}))
    sleep = AsyncMock()
    monkeypatch.setattr(api.asyncio, "sleep", sleep)
    scripts = []

    async def evaluate(script, url, **kwargs):
        assert kwargs["isolated_context"] is False
        scripts.append(script)
        payload = {"status_code": 0, "comments": [{"cid": CID, "reply_id": PARENT if mode == "reply" else "0"}], "has_more": 0}
        text = json.dumps(payload) if recover and len(scripts) == 3 else ""
        return {"status": 200, "text": text, "url": url + "&X-Bogus=fixture&X-Gnarly=fixture"}

    page = SimpleNamespace(url=URL, evaluate=AsyncMock(side_effect=evaluate), context=SimpleNamespace(cookies=AsyncMock(return_value=[])))
    if mode == "main":
        report = await main_visibility.scan_main_comment_ids(page, VIDEO, [CID])
    else:
        report = await reply_visibility.scan_reply_comment_ids(page, VIDEO, PARENT, [CID])
    assert len(scripts) == 3 and sleep.await_count == 2
    assert [call.args for call in sleep.await_args_list] == [(2.0,), (2.0,)]
    assert "scrollBy" not in scripts[0]
    assert all('window.scrollBy(0, 120)' in script and 'new Event("mousemove")' in script for script in scripts[1:])
    assert all('cache: "no-store"' in script for script in scripts)
    assert report["found_ids"] == ([CID] if recover else [])
    assert report["absent_ids"] == []
    assert report["reason"] == ("all_found" if recover else "empty_body")
