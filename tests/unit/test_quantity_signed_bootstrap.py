"""SDK-only warmup must not be mistaken for proven signed HTTP traffic."""
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from executor import visibility_publisher as publisher
from executor.http import comment_discovery


pytestmark = [pytest.mark.unit, pytest.mark.asyncio]


async def test_sdk_only_warmup_primes_read_only_get(monkeypatch):
    page = SimpleNamespace()
    readiness = AsyncMock(side_effect=[False, True])
    fetch = AsyncMock(return_value={"comments": []})
    monkeypatch.setattr(publisher, "wait_for_runtime_signer_ready", readiness)
    monkeypatch.setattr(comment_discovery, "fetch_comment_page", fetch)

    assert await publisher._ensure_signed_dispatch_ready(
        page, "7635267329407913230", correlation_id="quantity-test"
    )
    fetch.assert_awaited_once_with(page, "7635267329407913230", count=20)
    assert readiness.await_count == 2
    assert all(call.kwargs["require_signed_dispatch"] is True
               for call in readiness.await_args_list)


async def test_existing_signed_dispatch_skips_extra_get(monkeypatch):
    readiness = AsyncMock(return_value=True)
    fetch = AsyncMock()
    monkeypatch.setattr(publisher, "wait_for_runtime_signer_ready", readiness)
    monkeypatch.setattr(comment_discovery, "fetch_comment_page", fetch)

    assert await publisher._ensure_signed_dispatch_ready(
        SimpleNamespace(), "7635267329407913230", correlation_id="quantity-test"
    )
    fetch.assert_not_awaited()
    readiness.assert_awaited_once()


async def test_unsigned_or_failed_get_never_authorizes_publication(monkeypatch):
    readiness = AsyncMock(side_effect=[False, False])
    fetch = AsyncMock(side_effect=RuntimeError("transport unavailable"))
    monkeypatch.setattr(publisher, "wait_for_runtime_signer_ready", readiness)
    monkeypatch.setattr(comment_discovery, "fetch_comment_page", fetch)

    assert not await publisher._ensure_signed_dispatch_ready(
        SimpleNamespace(), "7635267329407913230", correlation_id="quantity-test"
    )
    assert readiness.await_count == 2
    fetch.assert_awaited_once()
