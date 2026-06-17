"""Unit tests for the user-schema workflow result classification.

Эти тесты проверяют ту единственную часть user_schema, которую можно
протестировать без живого браузера: преобразование внутреннего состояния
в словарь для UI и решение по статусу заказа.
"""
import pytest
from types import SimpleNamespace

import executor.user_schema as user_schema
from executor.user_schema import (
    PublishedMain,
    ReplyOutcome,
    UserSchemaResult,
)


@pytest.mark.unit
def test_user_schema_status_success_when_observer_found_main():
    res = UserSchemaResult(boost_enabled=False, boost_quantity=0)
    res.published_mains.append(
        PublishedMain(
            text="hello world",
            comment_id="123",
            network_confirmed=True,
            local_confirmed=True,
            method="hybrid_dom",
            account_id=10,
            tiktok_username="erjowkaaa",
        )
    )
    res.main_found_by_observer = True
    dct = res.to_dict()
    assert dct["status"] == "success"
    assert dct["engagement"]["main"]["verified"] is True
    assert dct["engagement"]["main"]["variants_published"][0]["text"] == "hello world"


@pytest.mark.unit
def test_user_schema_status_submitted_unconfirmed_when_observer_did_not_find():
    res = UserSchemaResult(boost_enabled=False, boost_quantity=0)
    res.published_mains.append(
        PublishedMain(
            text="hello",
            comment_id=None,
            network_confirmed=False,
            local_confirmed=True,
            method="hybrid_dom",
            account_id=10,
            tiktok_username=None,
        )
    )
    res.main_found_by_observer = False
    dct = res.to_dict()
    assert dct["status"] == "submitted_unconfirmed"
    assert dct["error"] == "main_public_confirmation_unconfirmed"
    assert dct["warnings"][0]["code"] == "main_submitted_unconfirmed"
    assert dct["engagement"]["main"]["verified"] is False
    assert dct["engagement"]["main"]["submitted_unconfirmed"] is True
    assert dct["engagement"]["main"]["visibility_status"] == "submitted_unconfirmed"


@pytest.mark.unit
def test_user_schema_status_fail_when_nothing_published():
    res = UserSchemaResult(boost_enabled=False, boost_quantity=0)
    res.error = "no_main_published"
    dct = res.to_dict()
    assert dct["status"] == "fail"
    assert dct["error"] == "no_main_published"
    assert dct["engagement"]["main"]["success"] is False


@pytest.mark.unit
def test_user_schema_replies_serialized_with_variant_index():
    res = UserSchemaResult(boost_enabled=False, boost_quantity=0)
    res.published_mains.append(
        PublishedMain(
            text="m1",
            comment_id="cid",
            network_confirmed=False,
            local_confirmed=True,
            method="hybrid_dom",
            account_id=10,
            tiktok_username=None,
        )
    )
    res.replies.append(
        ReplyOutcome(
            text="r1",
            variant_index=1,
            matched_main_text="m1",
            parent_comment_id="cid",
            success=True,
            method="hybrid_dom",
            error=None,
            account_id=20,
        )
    )
    res.replies.append(
        ReplyOutcome(
            text="r2",
            variant_index=2,
            matched_main_text="m1",
            parent_comment_id="cid",
            success=False,
            method=None,
            error="dead_session",
            account_id=21,
        )
    )
    dct = res.to_dict()
    items = dct["engagement"]["replies"]["items"]
    assert items[0]["variant_index"] == 1
    assert items[0]["success"] is True
    assert items[1]["success"] is False
    assert dct["engagement"]["replies"]["ok"] == 1
    assert dct["engagement"]["replies"]["total"] == 2


@pytest.mark.unit
def test_user_schema_fallback_main_via_reply_marked_in_output():
    res = UserSchemaResult(boost_enabled=False, boost_quantity=0)
    res.published_mains.append(
        PublishedMain(
            text="m1",
            comment_id="cid",
            network_confirmed=False,
            local_confirmed=True,
            method="hybrid_dom",
            account_id=20,
            tiktok_username=None,
        )
    )
    res.fallback_main_via_reply = True
    dct = res.to_dict()
    assert dct["engagement"]["main"]["fallback_via_reply"] is True
    # При fallback observer ещё ничего не подтвердил → submitted_unconfirmed.
    assert dct["status"] == "submitted_unconfirmed"


@pytest.mark.unit
def test_user_schema_preserves_non_fail_error_for_unconfirmed_result():
    res = UserSchemaResult(boost_enabled=False, boost_quantity=0)
    res.published_mains.append(
        PublishedMain(
            text="hello",
            comment_id="cid",
            network_confirmed=True,
            local_confirmed=True,
            method="hybrid_dom",
            account_id=10,
            tiktok_username=None,
        )
    )
    res.error = "main_public_repair_failed"

    dct = res.to_dict()

    assert dct["status"] == "submitted_unconfirmed"
    assert dct["error"] == "main_public_repair_failed"
    assert dct["engagement"]["main"]["error"] == "main_public_repair_failed"


@pytest.mark.unit
def test_user_schema_status_unconfirmed_when_any_recorded_reply_failed():
    res = UserSchemaResult(boost_enabled=False, boost_quantity=0)
    res.published_mains.append(
        PublishedMain(
            text="m1",
            comment_id="cid",
            network_confirmed=True,
            local_confirmed=True,
            method="hybrid_dom",
            account_id=10,
            tiktok_username=None,
        )
    )
    res.main_found_by_observer = True
    res.replies.append(
        ReplyOutcome(
            text="r1",
            variant_index=1,
            matched_main_text="m1",
            parent_comment_id="cid",
            success=False,
            method=None,
            error="stealth_reject_empty_body",
            account_id=20,
            attempted=True,
        )
    )

    dct = res.to_dict()

    assert dct["status"] == "submitted_unconfirmed"
    assert dct["error"] == "reply_publication_incomplete"


@pytest.mark.unit
def test_user_schema_reply_submitted_unconfirmed_without_visibility_is_attempted_uncertain():
    res = UserSchemaResult(boost_enabled=False, boost_quantity=0)
    res.expected_reply_variants = 1
    res.published_mains.append(
        PublishedMain(
            text="m1",
            comment_id="cid",
            network_confirmed=True,
            local_confirmed=True,
            method="hybrid_dom",
            account_id=10,
            tiktok_username=None,
        )
    )
    res.main_found_by_observer = True
    res.replies.append(
        ReplyOutcome(
            text="r1",
            variant_index=1,
            matched_main_text="m1",
            parent_comment_id="cid",
            success=False,
            method="hybrid_dom",
            error="stealth_reject_empty_body",
            account_id=20,
            attempted=True,
            submitted_unconfirmed=True,
        )
    )

    dct = res.to_dict()

    assert dct["status"] == "submitted_unconfirmed"
    assert dct["error"] == "reply_publication_incomplete"
    assert dct["engagement"]["main"]["submitted_unconfirmed"] is False
    assert dct["engagement"]["main"]["visibility_status"] == "public_confirmed"
    assert dct["engagement"]["main"]["error"] is None
    assert dct["engagement"]["replies"]["submitted"] == 0
    assert dct["engagement"]["replies"]["submitted_unconfirmed"] == 1
    assert dct["engagement"]["replies"]["attempted_uncertain"] == 1
    assert dct["engagement"]["replies"]["items"][0]["counts_as_submitted"] is False
    assert dct["engagement"]["replies"]["items"][0]["attempted_uncertain"] is True
    assert [w["code"] for w in dct["warnings"]] == ["reply_submitted_unconfirmed"]


@pytest.mark.unit
def test_user_schema_status_unconfirmed_when_requested_reply_never_recorded():
    res = UserSchemaResult(boost_enabled=False, boost_quantity=0)
    res.expected_reply_variants = 1
    res.published_mains.append(
        PublishedMain(
            text="m1",
            comment_id="cid",
            network_confirmed=True,
            local_confirmed=True,
            method="hybrid_dom",
            account_id=10,
            tiktok_username=None,
        )
    )
    res.main_found_by_observer = True

    dct = res.to_dict()

    assert dct["status"] == "submitted_unconfirmed"
    assert dct["error"] == "reply_publication_incomplete"
    assert dct["engagement"]["replies"]["requested_variants"] == 1


@pytest.mark.unit
def test_user_schema_reply_summary_counts_not_started_variants():
    res = UserSchemaResult(boost_enabled=False, boost_quantity=0)
    res.expected_reply_variants = 10
    res.published_mains.append(
        PublishedMain(
            text="m1",
            comment_id="cid",
            network_confirmed=True,
            local_confirmed=True,
            method="hybrid_dom",
            account_id=10,
            tiktok_username=None,
        )
    )
    res.main_found_by_observer = True
    res.replies.extend(
        [
            ReplyOutcome(
                "r1", 1, "m1", "cid", False, "hybrid_dom", "stealth", 21,
                attempted=True, submitted_unconfirmed=True,
            ),
            ReplyOutcome(
                "r2", 2, "m1", "cid", False, "hybrid_dom", "stealth", 22,
                attempted=True, submitted_unconfirmed=True,
            ),
            ReplyOutcome(
                "r3", 3, "m1", "cid", False, "hybrid_dom", "stealth", 23,
                attempted=True, submitted_unconfirmed=True,
            ),
            ReplyOutcome(
                "r3", 3, "m2", "cid2", False, "hybrid_dom", "stealth", 24,
                attempted=True, submitted_unconfirmed=True,
            ),
        ]
    )

    replies = res.to_dict()["engagement"]["replies"]

    assert replies["requested_variants"] == 10
    assert replies["expected_submissions"] == 10
    assert replies["expected_per_variant"] == 1
    assert replies["missing"] == 10
    assert replies["total"] == 4
    assert replies["attempted"] == 4
    assert replies["submitted"] == 0
    assert replies["attempted_uncertain"] == 4
    assert replies["variant_outcomes"] == 3
    assert replies["variants_not_started"] == 7


@pytest.mark.unit
def test_user_schema_reply_summary_reports_matrix_expected_submissions():
    res = UserSchemaResult(boost_enabled=False, boost_quantity=0)
    res.expected_main_publications = 9
    res.expected_reply_variants = 3
    res.published_mains.append(
        PublishedMain(
            text="m1",
            comment_id="cid-1",
            network_confirmed=True,
            local_confirmed=True,
            method="hybrid_dom",
            account_id=10,
            tiktok_username=None,
        )
    )
    res.main_found_by_observer = True
    for idx in range(1, 10):
        res.replies.append(
            ReplyOutcome(
                text="r1",
                variant_index=1,
                matched_main_text=f"m{idx}",
                parent_comment_id=f"cid-{idx}",
                success=True,
                method="hybrid_dom",
                error=None,
                account_id=20,
                attempted=True,
            )
        )

    dct = res.to_dict()
    replies = dct["engagement"]["replies"]

    assert dct["status"] == "submitted_unconfirmed"
    assert dct["error"] == "reply_publication_incomplete"
    assert replies["requested_variants"] == 3
    assert replies["expected_per_variant"] == 9
    assert replies["expected_submissions"] == 27
    assert replies["expected"] == 27
    assert replies["submitted"] == 9
    assert replies["missing"] == 18


@pytest.mark.unit
def test_user_schema_likes_block_keeps_real_username_consumer_format():
    res = UserSchemaResult(boost_enabled=True, boost_quantity=100)
    res.published_mains.append(
        PublishedMain(
            text="m1",
            comment_id="cid",
            network_confirmed=False,
            local_confirmed=True,
            method="hybrid_dom",
            account_id=10,
            tiktok_username="erjowkaaa",
        )
    )
    res.main_found_by_observer = True
    res.boost_order_id = 555
    dct = res.to_dict()
    likes = dct["engagement"]["likes"]
    assert likes["enabled"] is True
    assert likes["requested"] is True
    assert likes["order_id"] == 555
    assert likes["ok"] is True


@pytest.mark.unit
def test_user_schema_likes_not_started_is_not_reported_as_failed():
    from executor.executor import _format_likes_status

    res = UserSchemaResult(boost_enabled=True, boost_quantity=100)

    likes = res.to_dict()["engagement"]["likes"]

    assert likes["enabled"] is True
    assert likes["requested"] is True
    assert likes["attempted"] is False
    assert likes["error"] == "likes_not_started"
    assert _format_likes_status(likes) == "Likes: skipped"


@pytest.mark.unit
@pytest.mark.asyncio
async def test_publish_all_variants_streams_published_main(monkeypatch):
    page = SimpleNamespace(url="https://www.tiktok.com/@demo/video/123")
    account = SimpleNamespace(id=77)
    streamed = []
    call_count = {"publish": 0}

    async def _fake_extract_username(_page):
        return "demo_author"

    async def _fake_publish_comment_hybrid(*args, **kwargs):
        call_count["publish"] += 1
        return user_schema.PublishResult(
            status="success",
            comment_id=f"cid_{call_count['publish']}",
            network_captured=True,
            dom_strategy="data-e2e",
            composer_clean_after_submit=True,
        )

    async def _fake_logged_out(*args, **kwargs):
        return None

    monkeypatch.setattr(user_schema, "extract_tiktok_username", _fake_extract_username)
    monkeypatch.setattr(user_schema, "_parallel_dom_first_for_correlation", lambda _corr: True)
    monkeypatch.setattr(user_schema, "publish_comment_hybrid", _fake_publish_comment_hybrid)
    monkeypatch.setattr(user_schema, "_deny_if_visible_logged_out", _fake_logged_out)
    monkeypatch.setattr(user_schema, "_register_runtime_success", lambda *args, **kwargs: None)
    monkeypatch.setattr(user_schema, "_next_inter_publish_delay", lambda _count: 0.0)

    published, reason = await user_schema._publish_all_variants_on_account(
        None,
        page,
        account,
        ["one", "two"],
        correlation_id="task_1_parallel_main_v1_a1_acc77",
        expected_video_url=page.url,
        on_published=streamed.append,
    )

    assert reason is None
    assert [item.comment_id for item in published] == ["cid_1", "cid_2"]
    assert [item.comment_id for item in streamed] == ["cid_1", "cid_2"]
