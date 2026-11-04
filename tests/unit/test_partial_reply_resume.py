"""Partial progress, legacy no-send inference and continuation without network IO."""
import asyncio
from copy import deepcopy
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from services.publication_progress import (
    action_key, delivery_state, merge_url_result, publication_scope, record_publication,
    execute_reply_resume,
)
from services.task_replay import reply_resume_plan, retryable_video_urls, seed_reply_resume
from services.task_execution_ledger import build_url_execution_ledger

URL = "https://www.tiktok.com/@test/video/7635267329407913230"
PARENT = "7641761192486194454"


def receipt(index, parent=PARENT):
    return {"text": f"reply {index}", "variant_index": index, "parent_comment_id": parent,
            "reply_id": str(7641761192486194500 + index), "success": True,
            "network_confirmed": True, "publication_attempted": True}


def source(error="http_preflight:inconclusive", **extra):
    rows = [receipt(1), {"text": "reply 2", "variant_index": 2, "parent_comment_id": PARENT,
                        "error": error, "attempts": 6, **extra}, receipt(3)]
    return {"id": 1247, "kind": "engagement_only", "status": "error", "video_urls": [URL],
            "settings": {"replies": ["reply 1", "reply 2", "reply 3"], "reply_count": 3,
                         "url_results": {URL: {"status": "fail", "comment_id": PARENT,
                             "engagement": {"replies": {"expected": 3, "items": rows}}}}}}


@pytest.mark.parametrize("reason", ["http_preflight:inconclusive", "target_document_changed",
    "parent_api_unavailable:empty_body", "no_free_reply_accounts", "cookies_not_http_ready", "stopped_by_user"])
def test_legacy_preparation_failures_resume_only_original_variant_two(reason):
    task = source(reason)
    plan = reply_resume_plan(task, URL)
    assert [a["variant_index"] for a in plan["actions"]] == [2]
    assert retryable_video_urls(task) == [URL]


@pytest.mark.parametrize("extra", [{"publication_attempted": True}, {"submitted_unconfirmed": True},
    {"reply_id": "7641761192486194599"}])
def test_submission_evidence_wins_over_preparation_error(extra):
    assert reply_resume_plan(source(**extra), URL) is None


@pytest.mark.parametrize("reason", ["stealth_reject_empty_body", "parallel_reply_attempt_timeout:20"])
def test_ambiguous_legacy_failure_is_not_automatically_resent(reason):
    assert reply_resume_plan(source(reason), URL) is None


def test_all_failed_legacy_posts_cannot_fall_back_to_whole_video_replay():
    task = source("stealth_reject_empty_body")
    rows = task["settings"]["url_results"][URL]["engagement"]["replies"]["items"]
    task["settings"]["url_results"][URL]["engagement"]["replies"]["items"] = [rows[1]]
    assert retryable_video_urls(task) == []


def test_explicit_no_send_stop_is_retryable():
    assert reply_resume_plan(source("stopped_by_user", publication_attempted=False), URL)


def test_parent_is_part_of_action_identity():
    task = source()
    row = receipt(2, "7641761192486194400")
    task["settings"]["url_results"][URL]["engagement"]["replies"]["items"].append(row)
    assert reply_resume_plan(task, URL)["actions"][0]["parent_comment_id"] == PARENT


def test_full_order_expands_known_unsent_round_to_each_remaining_parent():
    task = source()
    task["kind"] = "full"
    row = task["settings"]["url_results"][URL]
    other = "7641761192486194400"
    row["engagement"]["main"] = {"variants_published": [
        {"comment_id": PARENT, "text": "a", "network_confirmed": True},
        {"comment_id": other, "text": "b", "network_confirmed": True}]}
    row["engagement"]["replies"]["items"] = [
        {"text": "reply 2", "variant_index": 2, "error": "no_free_reply_accounts"}, receipt(2)]
    plan = reply_resume_plan(task, URL)
    assert [a["parent_comment_id"] for a in plan["actions"]] == [other]


def test_server_seeds_receipts_after_runtime_settings_are_removed():
    settings = {"_reply_resume": {"forged": True}}
    seed_reply_resume(source(), settings, [URL])
    assert list(settings["_reply_resume"]) == [URL]
    assert settings["_reply_resume"][URL]["source"]["comment_id"] == PARENT


def test_stale_finalizer_keeps_both_saved_receipts():
    old = {"engagement": {"replies": {"items": [receipt(1), receipt(3)]}}}
    final = {"status": "fail", "engagement": {"replies": {"items": [
        {"text": "reply 1", "variant_index": 1, "parent_comment_id": PARENT,
         "success": False, "publication_attempted": False, "error": "stopped_by_user"}]}}}
    merged = merge_url_result(old, final)
    assert {r["variant_index"] for r in merged["engagement"]["replies"]["items"]} == {1, 3}
    assert merged["engagement"]["replies"]["ok"] == 2


def test_two_distinct_receipts_for_one_action_are_both_retained():
    a = receipt(1)
    b = {**a, "reply_id": "7641761192486194599"}
    old = {"engagement": {"replies": {"items": [a]}}}
    new = {"engagement": {"replies": {"items": [b]}}}
    rows = merge_url_result(old, new)["engagement"]["replies"]["items"]
    assert {r["reply_id"] for r in rows} == {a["reply_id"], b["reply_id"]}


def test_low_level_receipt_is_enriched_without_duplicate_rows():
    detailed = receipt(2)
    early = {k: v for k, v in detailed.items() if k != "variant_index"}
    rows = merge_url_result({"engagement": {"replies": {"items": [early]}}},
                            {"engagement": {"replies": {"items": [detailed]}}})["engagement"]["replies"]["items"]
    assert len(rows) == 1 and rows[0]["variant_index"] == 2


def test_ledger_separates_receipts_from_visibility_and_estimates_rounds_once():
    row = source()["settings"]["url_results"][URL]
    items = row["engagement"]["replies"]["items"]
    items[0]["attempts"], items[2]["attempts"] = 1, 2
    items.append({**items[1], "parent_comment_id": "7641761192486194400"})
    ledger = build_url_execution_ledger(row)
    assert ledger["summary"]["attempts"]["total"] == 9
    assert ledger["summary"]["attempts"]["estimated"] is True
    replies = ledger["summary"]["replies"]
    assert replies["accepted_with_id"] == 2
    assert replies["visible_to_observer"] == 0
    assert replies["visibility_not_checked"] == 2
    row["execution_attempts"] = [{"attempt": 1, "success": True, "submitted": 1}]
    assert build_url_execution_ledger(row)["summary"]["attempts"]["total"] == 1


@pytest.fixture
def persisted(db_session):
    from db.models import User, Task
    user = User(telegram_id=991234)
    db_session.add(user)
    db_session.flush()
    task = Task(user_id=user.id, status="in_progress", kind="engagement_only", main_comment="anchor",
                video_urls=[URL], settings=deepcopy(source()["settings"]))
    db_session.add(task)
    db_session.commit()
    return db_session, task


@pytest.mark.asyncio
async def test_concurrent_checkpoints_and_stale_snapshot(persisted):
    db, task = persisted
    with publication_scope(db, task, URL):
        await asyncio.gather(record_publication("reply", receipt(1)), record_publication("reply", receipt(3)))
    db.refresh(task)
    rows = task.settings["url_results"][URL]["engagement"]["replies"]["items"]
    assert {r["reply_id"] for r in rows if r.get("reply_id")} == {receipt(1)["reply_id"], receipt(3)["reply_id"]}


@pytest.mark.asyncio
async def test_reply_receipt_is_saved_before_optional_likes_can_interrupt(persisted, monkeypatch):
    db, task = persisted
    from executor import user_schema as schema
    client = SimpleNamespace(reply_comment=AsyncMock(return_value=(
        True, receipt(2)["reply_id"], None, None, {"parent_confirmed": True, "parent_confirmation_level": "network_exact"},
    )),
                             like_comment=AsyncMock(side_effect=asyncio.CancelledError))
    session = SimpleNamespace(scope=schema.REPLY_SCOPE, disabled_reason=None, account_id=1,
                              client=client, aweme_id='7635267329407913230', cookies={}, video_url=URL)
    monkeypatch.setattr(schema, '_like_parent_before_reply_enabled', lambda: True)
    monkeypatch.setattr(schema, 'can_boost_parent', lambda *args: True)
    with publication_scope(db, task, URL), pytest.raises(asyncio.CancelledError):
        await schema._publish_reply_http_first_if_eligible(session, 'reply 2',
            parent_comment_id=PARENT, correlation_id='checkpoint-before-like', variant_index=2)
    db.refresh(task)
    rows = task.settings['url_results'][URL]['engagement']['replies']['items']
    assert next(r for r in rows if r['variant_index'] == 2)['reply_id'] == receipt(2)['reply_id']


@pytest.mark.asyncio
async def test_reply_receipt_for_wrong_parent_is_saved_as_unconfirmed(persisted, monkeypatch):
    from executor import user_schema as schema

    db, task = persisted
    wrong_parent = str(int(PARENT) + 1)
    client = SimpleNamespace(
        reply_comment=AsyncMock(return_value=(True, receipt(2)["reply_id"], None, None, {
            "parent_confirmed": False, "parent_confirmation_level": "network_parent_mismatch",
            "response_parent_id": wrong_parent,
        })),
        like_comment=AsyncMock(),
    )
    session = SimpleNamespace(scope=schema.REPLY_SCOPE, disabled_reason=None, account_id=1,
                              client=client, aweme_id="7635267329407913230", cookies={}, video_url=URL)
    monkeypatch.setattr(schema, "_like_parent_before_reply_enabled", lambda: True)
    with publication_scope(db, task, URL):
        result = await schema._publish_reply_http_first_if_eligible(
            session, "reply 2", parent_comment_id=PARENT, correlation_id="fixture", variant_index=2,
        )
    db.refresh(task)
    rows = task.settings["url_results"][URL]["engagement"]["replies"]["items"]
    row = next(r for r in rows if r["reply_id"] == receipt(2)["reply_id"])
    assert result.status == "failed" and row["submitted_unconfirmed"] and not row["success"]
    assert row["response_parent_id"] == wrong_parent
    client.like_comment.assert_not_awaited()


@pytest.mark.asyncio
async def test_resume_reuses_executor_with_original_index_and_skips_second_invocation(persisted, monkeypatch):
    db, task = persisted
    settings = deepcopy(task.settings)
    seed_reply_resume(task, settings, [URL])
    task.settings = settings
    db.commit()
    from executor import engagement_engine
    send = AsyncMock(return_value=([receipt(2)], PARENT, None))
    monkeypatch.setattr(engagement_engine, "execute_engagement_only_replies_via_full_flow", send)
    result = await execute_reply_resume(db, task, URL)
    assert send.await_args.kwargs["variant_indices"] == [2]
    assert send.await_args.kwargs["replies"] == ["reply 2"]
    assert result["engagement"]["replies"]["ok"] == 3
    assert result["status"] == "success"
    await execute_reply_resume(db, task, URL)
    assert send.await_count == 1


@pytest.mark.asyncio
async def test_interrupted_resume_keeps_unknown_intent(persisted, monkeypatch):
    db, task = persisted
    settings = deepcopy(task.settings)
    seed_reply_resume(task, settings, [URL])
    task.settings = settings
    db.commit()
    from executor import engagement_engine
    send = AsyncMock(side_effect=asyncio.CancelledError)
    monkeypatch.setattr(engagement_engine, "execute_engagement_only_replies_via_full_flow", send)
    with pytest.raises(asyncio.CancelledError):
        await execute_reply_resume(db, task, URL)
    await execute_reply_resume(db, task, URL)
    assert send.await_count == 1


@pytest.mark.asyncio
async def test_two_simultaneous_resume_calls_claim_the_action_once(persisted, monkeypatch):
    db, task = persisted
    settings = deepcopy(task.settings)
    seed_reply_resume(task, settings, [URL])
    task.settings = settings
    db.commit()
    from executor import engagement_engine
    async def publish(*args, **kwargs):
        await asyncio.sleep(0.05)
        return [receipt(2)], PARENT, None
    send = AsyncMock(side_effect=publish)
    monkeypatch.setattr(engagement_engine, "execute_engagement_only_replies_via_full_flow", send)
    await asyncio.gather(execute_reply_resume(db, task, URL), execute_reply_resume(db, task, URL))
    assert send.await_count == 1


@pytest.mark.asyncio
async def test_resume_preparation_failure_clears_only_its_own_intent(persisted, monkeypatch):
    db, task = persisted
    settings = deepcopy(task.settings)
    seed_reply_resume(task, settings, [URL])
    task.settings = settings
    db.commit()
    from executor import engagement_engine
    failure = {"text": "reply 2", "variant_index": 2, "parent_comment_id": PARENT,
               "error": "no_free_reply_accounts", "publication_attempted": False}
    send = AsyncMock(return_value=([failure], PARENT, None))
    monkeypatch.setattr(engagement_engine, "execute_engagement_only_replies_via_full_flow", send)
    result = await execute_reply_resume(db, task, URL)
    row = next(r for r in result["engagement"]["replies"]["items"] if r["variant_index"] == 2)
    assert delivery_state(row) == "not_sent"
    assert reply_resume_plan(task, URL)


def test_new_payloads_have_explicit_preparation_and_uncertain_flags():
    from services.publication_progress import explicit_publication_flags
    result = {"engagement": {"main": {"error": "http_preflight:inconclusive"}, "replies": {"items": [
        {"error": "parent_api_unavailable:empty_body"}, {"error": "stealth_reject_empty_body"}]}}}
    explicit_publication_flags(result)
    assert result["engagement"]["main"]["publication_attempted"] is False
    assert [r["publication_attempted"] for r in result["engagement"]["replies"]["items"]] == [False, True]


def test_visible_parent_selection_preserves_other_receipts():
    from executor.user_schema import eligible_reply_mains, PublishedMain, UserSchemaResult
    a = PublishedMain("a", PARENT, True, False, "http", 1, "author", public_confirmed=True)
    b = PublishedMain("b", "7641761192486194400", True, False, "http", 2, "author2")
    result = UserSchemaResult(published_mains=[a, b], main_visibility={"enabled": True})
    assert eligible_reply_mains(result) == [a]
    assert result.published_mains == [a, b]
