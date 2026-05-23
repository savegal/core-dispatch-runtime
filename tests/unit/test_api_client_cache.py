import pytest
import asyncio
from urllib.parse import parse_qs, urlparse

from bot.services.api_client import APIClient


@pytest.mark.unit
@pytest.mark.asyncio
async def test_get_request_uses_short_lived_cache(monkeypatch):
    client = APIClient()
    calls: list[str] = []

    async def fake_request(method, url, json=None, headers=None, timeout=None):
        calls.append(url)

        class _Response:
            status_code = 200
            content = b'{"balance": 100}'
            text = '{"balance": 100}'

            def raise_for_status(self):
                return None

            def json(self):
                return {"balance": 100}

        return _Response()

    monkeypatch.setattr(client._client, "request", fake_request)

    try:
        first = await client.get_user(2)
        second = await client.get_user(2)
    finally:
        await client.close()

    assert first == {"balance": 100}
    assert second == {"balance": 100}
    assert len(calls) == 1


@pytest.mark.unit
@pytest.mark.asyncio
async def test_sync_telegram_does_not_drop_read_cache(monkeypatch):
    client = APIClient()
    calls: list[tuple[str, str]] = []

    async def fake_request(method, url, json=None, headers=None, timeout=None):
        calls.append((method, url))

        class _Response:
            def __init__(self, payload):
                self.status_code = 200
                self._payload = payload
                self.content = b"payload"
                self.text = "payload"

            def raise_for_status(self):
                return None

            def json(self):
                return self._payload

        if url.endswith("/api/users/2"):
            return _Response({"id": 2, "balance": 55})
        if url.endswith("/api/users/sync_telegram"):
            return _Response({"id": 2, "role": "user"})
        raise AssertionError(f"Unexpected URL: {url}")

    monkeypatch.setattr(client._client, "request", fake_request)

    try:
        await client.get_user(2)
        await client.sync_telegram(telegram_id=123, username="demo", use_cache=False)
        await client.get_user(2)
    finally:
        await client.close()

    get_user_calls = [url for method, url in calls if method == "GET" and url.endswith("/api/users/2")]
    assert len(get_user_calls) == 1


@pytest.mark.unit
@pytest.mark.asyncio
async def test_get_request_dedupes_inflight_requests(monkeypatch):
    client = APIClient()
    calls: list[str] = []

    async def fake_request(method, url, json=None, headers=None, timeout=None):
        calls.append(url)
        await asyncio.sleep(0.05)

        class _Response:
            status_code = 200
            content = b'{"id": 2, "balance": 100}'
            text = '{"id": 2, "balance": 100}'

            def raise_for_status(self):
                return None

            def json(self):
                return {"id": 2, "balance": 100}

        return _Response()

    monkeypatch.setattr(client._client, "request", fake_request)

    try:
        first, second, third = await asyncio.gather(
            client.get_user(2),
            client.get_user(2),
            client.get_user(2),
        )
    finally:
        await client.close()

    assert first == second == third == {"id": 2, "balance": 100}
    assert len(calls) == 1


@pytest.mark.unit
@pytest.mark.asyncio
async def test_mutation_invalidates_only_related_read_cache(monkeypatch):
    client = APIClient()
    calls: list[tuple[str, str]] = []

    async def fake_request(method, url, json=None, headers=None, timeout=None):
        calls.append((method, url))

        class _Response:
            def __init__(self, payload):
                self.status_code = 200
                self._payload = payload
                self.content = b"payload"
                self.text = "payload"

            def raise_for_status(self):
                return None

            def json(self):
                return self._payload

        if url.endswith("/api/users/2"):
            return _Response({"id": 2, "balance": 55})
        if url.endswith("/api/pricing") and method == "GET":
            return _Response({"price_success": 5})
        if url.endswith("/api/pricing") and method == "PUT":
            return _Response({"ok": True})
        raise AssertionError(f"Unexpected request: {method} {url}")

    monkeypatch.setattr(client._client, "request", fake_request)

    try:
        await client.get_user(2)
        await client.get_pricing()
        await client.update_pricing(7)
        await client.get_user(2)
        await client.get_pricing()
    finally:
        await client.close()

    get_user_calls = [url for method, url in calls if method == "GET" and url.endswith("/api/users/2")]
    pricing_get_calls = [url for method, url in calls if method == "GET" and url.endswith("/api/pricing")]

    assert len(get_user_calls) == 1
    assert len(pricing_get_calls) == 2


@pytest.mark.unit
@pytest.mark.asyncio
async def test_get_inbox_videos_paginates_over_api_limit(monkeypatch):
    client = APIClient()
    calls: list[str] = []

    async def fake_request(method, url, json=None, headers=None, timeout=None):
        calls.append(url)
        query = parse_qs(urlparse(url).query)
        limit = int(query["limit"][0])
        offset = int(query["offset"][0])
        assert limit <= 50

        total_available = 125
        end = min(offset + limit, total_available)
        videos = [{"id": i} for i in range(offset, end)]

        class _Response:
            status_code = 200
            content = b"payload"
            text = "payload"

            def raise_for_status(self):
                return None

            def json(self):
                return {
                    "total": total_available,
                    "by_status": {"new": total_available},
                    "videos": videos,
                }

        return _Response()

    monkeypatch.setattr(client._client, "request", fake_request)

    try:
        result = await client.get_inbox_videos(2, status="new", limit=120)
    finally:
        await client.close()

    assert len(result["videos"]) == 120
    assert [v["id"] for v in result["videos"][:3]] == [0, 1, 2]
    assert [v["id"] for v in result["videos"][-3:]] == [117, 118, 119]
    assert len(calls) == 3


@pytest.mark.unit
@pytest.mark.asyncio
async def test_skip_all_new_inbox_videos_uses_bulk_status_endpoint(monkeypatch):
    client = APIClient()
    calls: list[tuple[str, str, object | None]] = []

    async def fake_request(method, url, json=None, headers=None, timeout=None):
        calls.append((method, url, json))

        class _Response:
            status_code = 200
            content = b'{"ok": true, "skipped": 77}'
            text = '{"ok": true, "skipped": 77}'

            def raise_for_status(self):
                return None

            def json(self):
                return {"ok": True, "skipped": 77}

        return _Response()

    monkeypatch.setattr(client._client, "request", fake_request)

    try:
        result = await client.skip_all_new_inbox_videos(2)
    finally:
        await client.close()

    assert result == {"ok": True, "skipped": 77}
    assert len(calls) == 1
    method, url, payload = calls[0]
    assert method == "POST"
    assert url.endswith("/api/video-inbox/videos/batch/skip-new?user_id=2")
    assert payload is None
