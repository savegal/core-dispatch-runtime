"""SDK-only warmup must not be mistaken for proven signed HTTP traffic."""
import asyncio
from contextlib import contextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from executor import visibility_publisher as publisher
from executor.http import comment_discovery


pytestmark = [pytest.mark.unit, pytest.mark.asyncio]


async def test_token_handshake_is_bound_to_the_current_document_and_reason():
    from executor.http.page_runtime import page_document_key
    target = "7635267329407913230"
    page = SimpleNamespace(url=f"https://www.tiktok.com/@fixture/video/{target}")
    meta = {"reason": "empty_body", "signed_token_refresh": page_document_key(page)}
    page._comment_list_http_meta = meta
    assert comment_discovery.comment_token_handshake_ready(page, target)
    meta["reason"] = "http_status"
    assert not comment_discovery.comment_token_handshake_ready(page, target)
    meta["reason"] = "empty_body"
    page._http_document_generation += 1
    assert not comment_discovery.comment_token_handshake_ready(page, target)


@pytest.mark.parametrize("signed,headers_arrived", [(True, True), (False, True), (True, False)])
async def test_bootstrap_timeout_reuses_only_signed_response_headers(monkeypatch, signed, headers_arrived):
    target = "7635267329407913230"
    context = SimpleNamespace(cookies=AsyncMock(return_value=[]), add_cookies=AsyncMock())
    page = SimpleNamespace(url=f"https://www.tiktok.com/@fixture/video/{target}", context=context)
    monkeypatch.setattr(comment_discovery, "_read_request_metadata", AsyncMock(return_value=({}, {"msToken": "imported"})))

    @contextmanager
    def evidence(*args):
        yield {"request_security_params": {"X-Bogus": signed}}

    async def fetch(page, script, url, meta, **kwargs):
        assert kwargs["bootstrap"]
        response = {"unavailable_reason": "timeout"}
        if headers_arrived:
            response.update(status=200, url=url, text="", headers={"x-ms-token": "fresh"})
        return response

    monkeypatch.setattr(comment_discovery, "observe_request_security", evidence)
    monkeypatch.setattr(comment_discovery, "_fetch_with_native_activation", fetch)

    assert await comment_discovery.fetch_comment_page(page, target, bootstrap=True) is None
    assert page._comment_list_http_meta["reason"] == "timeout"
    assert comment_discovery.comment_token_handshake_ready(page, target) is (signed and headers_arrived)


@pytest.mark.parametrize("case,expected", [
    ("exact", True), ("old", False), ("wrong_uid", False), ("wrong_media", False),
    ("wrong_parent", False), ("wrong_text", False), ("ambiguous", False), ("same_second_without_baseline", False),
    ("same_second_with_baseline", True), ("existing_cid", False),
])
async def test_empty_post_recovery_requires_a_unique_recent_comment(case, expected):
    target, cid = "7635267329407913230", "7704740811904828192"
    row = {"cid": cid, "aweme_id": target, "text": "fixture", "create_time": 101, "user": {"uid": "12345"}}
    baseline = None
    if case in {"same_second_without_baseline", "same_second_with_baseline"}:
        row["create_time"] = 100
    if case == "same_second_with_baseline":
        baseline = {"comments": [], "has_more": 0}
    if case == "existing_cid":
        baseline = {"comments": [dict(row)], "has_more": 0}
    row.update({"old": {"create_time": 99}, "wrong_uid": {"user": {"uid": "23456"}},
                "wrong_media": {"aweme_id": "123"}, "wrong_parent": {"reply_id": "7704740811904828100"},
                "wrong_text": {"text": "Fixture"}}.get(case, {}))
    candidates = [row, {**row, "cid": "7704740811904828193"}] if case == "ambiguous" else [row]
    match = comment_discovery._match_recent_publication(
        {"comments": candidates}, aweme_id=target, parent_id="0", author_uid="12345",
        text="fixture", started_at=100.2, now=101.9, previous=baseline,
    )
    assert (match is row) is expected


async def test_sdk_only_warmup_primes_read_only_get(monkeypatch):
    page = SimpleNamespace()
    readiness = AsyncMock(side_effect=[False, True])
    fetch = AsyncMock(return_value={"comments": []})
    monkeypatch.setattr(publisher, "wait_for_runtime_signer_ready", readiness)
    monkeypatch.setattr(comment_discovery, "fetch_comment_page", fetch)

    assert await publisher._publication_readiness_error(
        page, "7635267329407913230", correlation_id="quantity-test"
    ) is None
    fetch.assert_awaited_once_with(page, "7635267329407913230", count=20)
    assert readiness.await_count == 2
    assert all(call.kwargs["require_signed_dispatch"] is True
               for call in readiness.await_args_list)


async def test_existing_signed_dispatch_skips_extra_get(monkeypatch):
    readiness = AsyncMock(return_value=True)
    fetch = AsyncMock()
    monkeypatch.setattr(publisher, "wait_for_runtime_signer_ready", readiness)
    monkeypatch.setattr(comment_discovery, "fetch_comment_page", fetch)

    assert await publisher._publication_readiness_error(
        SimpleNamespace(), "7635267329407913230", correlation_id="quantity-test"
    ) is None
    fetch.assert_not_awaited()
    readiness.assert_awaited_once()


async def test_unsigned_or_failed_get_never_authorizes_publication(monkeypatch):
    readiness = AsyncMock(side_effect=[False, False])
    fetch = AsyncMock(side_effect=RuntimeError("transport unavailable"))
    monkeypatch.setattr(publisher, "wait_for_runtime_signer_ready", readiness)
    monkeypatch.setattr(comment_discovery, "fetch_comment_page", fetch)

    assert await publisher._publication_readiness_error(
        SimpleNamespace(), "7635267329407913230", correlation_id="quantity-test"
    ) == "runtime_signer_not_ready"
    assert readiness.await_count == 2
    fetch.assert_awaited_once()


async def test_stop_cancels_pending_get_without_faking_readiness(monkeypatch):
    page = SimpleNamespace()
    readiness = AsyncMock(return_value=False)
    fetch = AsyncMock(side_effect=asyncio.CancelledError())
    monkeypatch.setattr(publisher, "wait_for_runtime_signer_ready", readiness)
    monkeypatch.setattr(comment_discovery, "fetch_comment_page", fetch)

    with pytest.raises(asyncio.CancelledError):
        await publisher._publication_readiness_error(
            page, "7635267329407913230", correlation_id="quantity-cancel"
        )
    fetch.assert_awaited_once()
    assert readiness.await_count == 1


@pytest.mark.parametrize("cached_id,signed,expected", [
    ("7635267329407913230", True, None),
    ("7635267329407913230", False, "comment_readiness_unconfirmed:empty_body"),
    ("7635267329407913231", True, "comment_readiness_unconfirmed:empty_body"),
])
async def test_quantity_reuses_only_the_signed_target_document_read(monkeypatch, cached_id, signed, expected):
    target = "7635267329407913230"
    page = SimpleNamespace(url=f"https://www.tiktok.com/@fixture/video/{target}",
                           _comment_list_http_meta={"reason": "empty_body"})
    comment_discovery.cache_first_comment_page(page, cached_id, {"comments": []}, signed=signed)
    fetch = AsyncMock(return_value=None)
    monkeypatch.setattr(publisher, "wait_for_runtime_signer_ready", AsyncMock(return_value=True))
    monkeypatch.setattr(comment_discovery, "fetch_comment_page", fetch)

    assert await publisher._publication_readiness_error(
        page, target, correlation_id="quantity-test", require_comment_readiness=True
    ) == expected
    if expected:
        fetch.assert_awaited_once_with(page, target, count=20, bootstrap=True)
    else:
        fetch.assert_not_awaited()
