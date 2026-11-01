"""Reproduce external-audit discovery findings without live network traffic."""
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest
from sqlalchemy.orm import Session

from executor import engagement_engine as engine, keyword_replies as keyword
from executor.http import comment_discovery as discovery
from services.order_transport import set_task_order_transport, reset_task_order_transport
from db.models import Task
from tests.unit.test_keyword_replies import keyword_db, execution, URL, MEDIA, CIDS


pytestmark = [pytest.mark.unit, pytest.mark.asyncio]
SHORT = "https://vt.tiktok.com/Zfixture/"


@pytest.fixture
def discovery_accounts(monkeypatch):
    from executor import user_schema as schema
    accounts = [SimpleNamespace(id=index, proxy=None) for index in range(1, 15)]
    selector = Mock(side_effect=accounts + [None])
    monkeypatch.setattr(engine, "get_free_account_for_reply", selector)
    gate = AsyncMock(return_value=None)
    monkeypatch.setattr(schema, "_preflight_cookie_http_gate", gate)
    create = AsyncMock(return_value=(object(), SimpleNamespace(url=URL)))
    monkeypatch.setattr(engine, "create_page_for_account", create)
    monkeypatch.setattr(schema, "_ensure_account_login_ready", AsyncMock(return_value=(True, None)))
    monkeypatch.setattr(engine, "warmup_page_for_http", AsyncMock(return_value=({}, True, {})))
    close, release = AsyncMock(), Mock()
    monkeypatch.setattr(schema, "_safe_close", close)
    monkeypatch.setattr(schema, "release_reply_account_reservation", release)
    monkeypatch.setattr(keyword.settings, "MAX_RETRY_ACCOUNTS", 2)
    token = set_task_order_transport("http", SimpleNamespace(TIKTOK_NETWORK_IMPL_READY=True))
    try:
        yield SimpleNamespace(gate=gate, create=create, selector=selector, close=close, release=release)
    finally:
        reset_task_order_transport(token)


async def test_discovery_retries_transient_account_failure_and_retains_positive_matches(discovery_accounts, monkeypatch):
    collect = AsyncMock(side_effect=[
        ([{"cid": CIDS[0], "text": "цена"}], {"complete": False, "scanned": 1,
                                              "error": "keyword_comment_list_unavailable"}),
        ([{"cid": CIDS[1], "text": "цена"}], {"complete": True, "scanned": 1, "error": None}),
    ])
    monkeypatch.setattr(keyword, "collect_http_targets", collect)
    targets, scan = await keyword.discover_keyword_targets(None, URL, ["цена"], task_id=1)
    assert scan["complete"] is True and scan["error"] is None
    assert [row["cid"] for row in targets] == CIDS[:2]
    assert scan["scanned"] == 2
    assert collect.await_count == discovery_accounts.create.await_count == 2
    assert [call.args[0] for call in discovery_accounts.release.call_args_list] == [1, 2]


async def test_preflight_rejections_do_not_consume_real_browser_attempts(discovery_accounts, monkeypatch):
    discovery_accounts.gate.side_effect = ["dead_session"] * 3 + [None]
    collect = AsyncMock(return_value=([], {"complete": True, "scanned": 0, "error": None}))
    monkeypatch.setattr(keyword, "collect_http_targets", collect)
    _, scan = await keyword.discover_keyword_targets(None, URL, ["цена"], task_id=1)
    assert scan["complete"] is True
    assert discovery_accounts.gate.await_count == 4
    assert discovery_accounts.create.await_count == 1
    assert discovery_accounts.release.call_count == 4


async def test_incomplete_scans_preserve_matches_when_browser_budget_is_exhausted(discovery_accounts, monkeypatch):
    collect = AsyncMock(side_effect=[
        ([{"cid": CIDS[0], "text": "цена"}], {"complete": False, "scanned": 50,
                                              "error": "keyword_comment_list_unavailable"}),
        ([{"cid": CIDS[1], "text": "цена"}], {"complete": False, "scanned": 20,
                                              "error": "keyword_comment_list_unavailable"}),
    ])
    monkeypatch.setattr(keyword, "collect_http_targets", collect)
    targets, scan = await keyword.discover_keyword_targets(None, URL, ["цена"], task_id=1)
    assert [row["cid"] for row in targets] == CIDS[:2]
    assert scan["complete"] is False and scan["scanned"] == 50
    assert discovery_accounts.create.await_count == 2


async def test_shortlink_ledger_survives_warmup_resolution_cache_loss_and_canonical_alias(keyword_db, execution):
    db, task, sql_engine = keyword_db
    collect, sender = execution
    async def resolve(db, url, pool_owner=None):
        return (SHORT, None) if url == SHORT else (URL, MEDIA)
    engine.resolve_order_video_url.side_effect = resolve
    collect.return_value = ([{"cid": cid, "text": "цена"} for cid in CIDS[:2]],
                            {"complete": True, "scanned": 50, "error": None,
                             "resolved_video_url": URL, "aweme_id": MEDIA})
    first = await keyword.execute_keyword_replies(db, task, SHORT, task_id=task.id)
    assert first.resolved_video_url == URL and first.aweme_id == MEDIA
    binding = task.settings["keyword_video_bindings"][SHORT]
    assert binding["storage_key"] == keyword.video_key(URL)
    assert binding["aweme_id"] == MEDIA
    # A fresh process/cache cannot resolve the shortlink; the committed binding can.
    engine.resolve_order_video_url.reset_mock()
    with Session(sql_engine, expire_on_commit=False) as recovered_db:
        recovered_task = recovered_db.get(Task, task.id)
        await keyword.execute_keyword_replies(recovered_db, recovered_task, SHORT, task_id=task.id)
    engine.resolve_order_video_url.assert_not_awaited()
    await keyword.execute_keyword_replies(db, task, URL, task_id=task.id)
    assert sender.await_count == 2 and collect.await_count == 1
    assert len(keyword._targets(db, task.id, binding["storage_key"])) == 2


async def test_legacy_raw_shortlink_journal_is_reused_without_moving_claimed_rows(keyword_db, execution):
    db, task, _ = keyword_db
    collect, sender = execution
    old_key = keyword.video_key(SHORT)
    await keyword.checkpoint(db, task, SHORT, "plan", storage_key=old_key,
        targets=[{"cid": cid, "text": "цена"} for cid in CIDS[:2]],
        scan={"complete": True, "scanned": 2}, replies=["Ответ"])
    assert await keyword.checkpoint(db, task, SHORT, "claim", storage_key=old_key, cid=CIDS[0])
    engine.resolve_order_video_url.return_value = (URL, MEDIA)
    await keyword.execute_keyword_replies(db, task, SHORT, task_id=task.id)
    assert collect.await_count == 0 and sender.await_count == 1
    assert task.settings["keyword_video_bindings"][SHORT]["storage_key"] == old_key
    assert [row.status for row in keyword._targets(db, task.id, old_key)] == ["dispatching", "success"]
    await keyword.execute_keyword_replies(db, task, URL, task_id=task.id)
    assert sender.await_count == 1 and not keyword._targets(db, task.id, keyword.video_key(URL))


@pytest.mark.parametrize("stored", [
    ["corrupt"],
    {SHORT: {"resolved_video_url": "https://example.com/video/" + MEDIA,
             "aweme_id": MEDIA, "storage_key": "invalid"}},
    {SHORT: {"resolved_video_url": URL, "aweme_id": MEDIA,
             "storage_url": URL.replace(MEDIA, "1234567890123456790"),
             "storage_key": keyword.video_key(URL.replace(MEDIA, "1234567890123456790"))}},
])
async def test_malformed_legacy_binding_is_ignored_and_replaced_safely(keyword_db, execution, stored):
    db, task, _ = keyword_db
    task.settings = {**task.settings, "keyword_video_bindings": stored}
    db.commit()
    _, sender = execution
    result = await keyword.execute_keyword_replies(db, task, URL, task_id=task.id)
    assert result.to_dict()["status"] == "success" and sender.await_count == 3
    assert task.settings["keyword_video_bindings"][URL]["aweme_id"] == MEDIA
    assert task.settings["keyword_video_bindings"][URL]["storage_key"] == keyword.video_key(URL)


def test_valid_comment_ids_are_normalized_before_becoming_durable_target_keys():
    rows = keyword.select_targets([{"cid": " " + CIDS[0] + " ", "text": "цена"}], ["цена"], set())
    assert rows[0]["cid"] == CIDS[0]


@pytest.mark.parametrize("count,cached", [(50, False), (20, True)])
async def test_signed_first_page_read_uses_requested_count_without_native_activation(monkeypatch, count, cached):
    page = SimpleNamespace(on=Mock(), remove_listener=Mock(), _warmup_signer_ready=True,
                           _cached_first_comment_page={} if cached else None)
    url = f"https://www.tiktok.com/api/comment/list/?aweme_id={MEDIA}&cursor=0&count={count}"
    async def evaluate(script, url, **kwargs):
        if "const allowDirectFetch = false;" in script:
            return {"activation_only": True}
        return {"status": 200, "text": "{}", "url": url}
    page.evaluate = AsyncMock(side_effect=evaluate)
    monkeypatch.setattr(discovery, "activate_page_input", AsyncMock())
    monkeypatch.setattr(discovery, "_COMMENT_LIST_NATIVE_TIMEOUT_SECONDS", 0.001)
    result = await discovery._fetch_with_native_activation(page, discovery._COMMENT_LIST_FETCH_JS, url, {})
    assert result["status"] == 200 and page.evaluate.await_count == 1
    assert "count=" + str(count) in page.evaluate.await_args.args[1]
    page.on.assert_not_called()


async def test_native_only_bootstrap_still_activates_even_for_large_count(monkeypatch):
    page = SimpleNamespace(on=Mock(), remove_listener=Mock(), _warmup_signer_ready=False,
                           _cached_first_comment_page={})
    page.evaluate = AsyncMock(return_value={"activation_only": False})
    monkeypatch.setattr(discovery, "activate_page_input", AsyncMock())
    url = f"https://www.tiktok.com/api/comment/list/?aweme_id={MEDIA}&cursor=0&count=50"
    await discovery._fetch_with_native_activation(page, discovery._COMMENT_LIST_FETCH_JS, url, {}, native_only=True)
    assert "const allowDirectFetch = false;" in page.evaluate.await_args.args[0]
    assert page.on.call_count == 2


async def test_large_count_read_never_bypasses_explicitly_unready_signer(monkeypatch):
    from executor.http import signature_runtime
    page = SimpleNamespace(on=Mock(), remove_listener=Mock(), _warmup_signer_ready=False,
                           _cached_first_comment_page={})
    page.evaluate = AsyncMock(return_value={"activation_only": False})
    monkeypatch.setattr(discovery, "activate_page_input", AsyncMock())
    recheck = AsyncMock(return_value=False)
    monkeypatch.setattr(signature_runtime, "wait_for_runtime_signer_ready", recheck)
    url = f"https://www.tiktok.com/api/comment/list/?aweme_id={MEDIA}&cursor=0&count=50"
    result = await discovery._fetch_with_native_activation(page, discovery._COMMENT_LIST_FETCH_JS, url, {})
    assert result["unavailable_reason"] == "signer_not_ready"
    assert "const allowDirectFetch = false;" in page.evaluate.await_args.args[0]
    assert page.evaluate.await_count == 1 and page.on.call_count == 2
    recheck.assert_awaited_once()
