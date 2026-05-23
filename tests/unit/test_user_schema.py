"""Unit tests for the user-schema workflow result classification.

Эти тесты проверяют ту единственную часть user_schema, которую можно
протестировать без живого браузера: преобразование внутреннего состояния
в словарь для UI и решение по статусу заказа.
"""
import pytest

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
def test_user_schema_reply_submitted_unconfirmed_does_not_mark_main_unconfirmed():
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
    assert dct["error"] == "reply_publication_unconfirmed"
    assert dct["engagement"]["main"]["submitted_unconfirmed"] is False
    assert dct["engagement"]["main"]["visibility_status"] == "public_confirmed"
    assert dct["engagement"]["main"]["error"] is None
    assert dct["engagement"]["replies"]["submitted"] == 1
    assert dct["engagement"]["replies"]["submitted_unconfirmed"] == 1
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
