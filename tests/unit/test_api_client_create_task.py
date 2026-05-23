import pytest

from bot.services.api_client import APIClient, APIRequestError


@pytest.mark.unit
@pytest.mark.asyncio
async def test_create_task_engagement_only_accepts_empty_main_comment(monkeypatch):
    client = APIClient()
    captured_payload: dict = {}

    async def _fake_request(method, endpoint, json=None, headers=None, timeout=None, retry_safe=True):
        captured_payload["json"] = json or {}
        return {"task_id": 123, "status": "pending"}

    monkeypatch.setattr(client, "_request", _fake_request)

    try:
        result = await client.create_task(
            user_id=1,
            main_comment="",
            video_urls=["https://www.tiktok.com/@demo/video/1234567890123456789"],
            kind="engagement_only",
            engagement_comment_id="cid_123",
            engagement_comment_text="anchor_text",
        )
    finally:
        await client.close()

    assert result["task_id"] == 123
    assert captured_payload["json"]["kind"] == "engagement_only"
    assert captured_payload["json"]["main_comment"] == "anchor_text"
    assert captured_payload["json"]["text_variants"][0] == "anchor_text"


@pytest.mark.unit
@pytest.mark.asyncio
async def test_create_task_engagement_only_can_target_exact_text_without_id_or_author(monkeypatch):
    client = APIClient()
    captured_payload: dict = {}

    async def _fake_request(method, endpoint, json=None, headers=None, timeout=None, retry_safe=True):
        captured_payload["json"] = json or {}
        return {"task_id": 124, "status": "pending"}

    monkeypatch.setattr(client, "_request", _fake_request)

    try:
        result = await client.create_task(
            user_id=1,
            main_comment="Target comment text",
            video_urls=["https://www.tiktok.com/@demo/video/1234567890123456789"],
            kind="engagement_only",
            engagement_comment_text="Target comment text",
            replies=["reply 1"],
            settings={"boost_likes_enabled": False},
        )
    finally:
        await client.close()

    payload = captured_payload["json"]
    assert result["task_id"] == 124
    assert payload["kind"] == "engagement_only"
    assert "engagement_comment_id" not in payload
    assert "engagement_username" not in payload
    assert payload["engagement_comment_text"] == "Target comment text"
    assert payload["settings"]["replies"] == ["reply 1"]
    assert payload["settings"]["boost_likes_enabled"] is False


@pytest.mark.unit
@pytest.mark.asyncio
async def test_create_task_full_still_requires_main_comment():
    client = APIClient()
    try:
        with pytest.raises(APIRequestError):
            await client.create_task(
                user_id=1,
                main_comment="",
                video_urls=["https://www.tiktok.com/@demo/video/1234567890123456789"],
                kind="full",
            )
    finally:
        await client.close()


@pytest.mark.unit
@pytest.mark.asyncio
async def test_create_task_full_rejects_missing_text_variants():
    client = APIClient()
    try:
        with pytest.raises(APIRequestError, match="3"):
            await client.create_task(
                user_id=1,
                main_comment="one",
                text_variants=["one"],
                video_urls=["https://www.tiktok.com/@demo/video/1234567890123456789"],
                kind="full",
            )
    finally:
        await client.close()


@pytest.mark.unit
@pytest.mark.asyncio
async def test_create_task_template_rejects_missing_text_variants():
    client = APIClient()
    try:
        with pytest.raises(APIRequestError, match="3"):
            await client.create_task_template(
                user_id=1,
                name="template",
                main_comment="one",
                text_variants=["one"],
            )
    finally:
        await client.close()


@pytest.mark.unit
@pytest.mark.asyncio
async def test_create_replies_only_task_template_accepts_replies_without_main_variants(monkeypatch):
    client = APIClient()
    captured_payload: dict = {}

    async def _fake_request(method, endpoint, json=None, headers=None, timeout=None, retry_safe=True):
        captured_payload["json"] = json or {}
        return {"id": 77}

    monkeypatch.setattr(client, "_request", _fake_request)

    try:
        result = await client.create_task_template(
            user_id=1,
            name="reply pack",
            main_comment="",
            replies=["reply 1", "reply 2"],
            template_type="replies_only",
        )
    finally:
        await client.close()

    payload = captured_payload["json"]
    assert result["id"] == 77
    assert payload["main_comment"] == "__replies_only_template__"
    assert payload["settings"]["template_type"] == "replies_only"
    assert payload["settings"]["text_variants"] == []
    assert payload["settings"]["replies"] == ["reply 1", "reply 2"]


@pytest.mark.unit
@pytest.mark.asyncio
async def test_create_replies_only_task_template_requires_replies():
    client = APIClient()
    try:
        with pytest.raises(APIRequestError, match="ответ"):
            await client.create_task_template(
                user_id=1,
                name="reply pack",
                main_comment="",
                replies=[],
                template_type="replies_only",
            )
    finally:
        await client.close()


@pytest.mark.unit
@pytest.mark.asyncio
async def test_create_task_full_preserves_explicit_text_variants(monkeypatch):
    client = APIClient()
    captured_payload: dict = {}

    async def _fake_request(method, endpoint, json=None, headers=None, timeout=None, retry_safe=True):
        captured_payload["json"] = json or {}
        return {"task_id": 456, "status": "pending"}

    monkeypatch.setattr(client, "_request", _fake_request)

    try:
        await client.create_task(
            user_id=1,
            main_comment="one",
            text_variants=["one", "two", "three"],
            video_urls=["https://www.tiktok.com/@demo/video/1234567890123456789"],
            kind="full",
        )
    finally:
        await client.close()

    assert captured_payload["json"]["text_variants"] == ["one", "two", "three"]


@pytest.mark.unit
@pytest.mark.asyncio
async def test_create_task_accepts_twenty_replies(monkeypatch):
    client = APIClient()
    captured_payload: dict = {}

    async def _fake_request(method, endpoint, json=None, headers=None, timeout=None, retry_safe=True):
        captured_payload["json"] = json or {}
        return {"task_id": 789, "status": "pending"}

    monkeypatch.setattr(client, "_request", _fake_request)
    replies = [f"reply {idx}" for idx in range(1, 21)]

    try:
        await client.create_task(
            user_id=1,
            main_comment="one",
            text_variants=["one", "two", "three"],
            video_urls=["https://www.tiktok.com/@demo/video/1234567890123456789"],
            kind="full",
            replies=replies,
        )
    finally:
        await client.close()

    payload = captured_payload["json"]
    assert payload["reply1"] == "reply 1"
    assert payload["reply2"] == "reply 2"
    assert payload["reply3"] == "reply 3"
    assert payload["settings"]["reply20"] == "reply 20"
    assert payload["settings"]["replies"] == replies
    assert payload["settings"]["reply_count"] == 20


@pytest.mark.unit
@pytest.mark.asyncio
async def test_create_task_trims_replies_to_max(monkeypatch):
    client = APIClient()
    captured_payload: dict = {}

    async def _fake_request(method, endpoint, json=None, headers=None, timeout=None, retry_safe=True):
        captured_payload["json"] = json or {}
        return {"task_id": 790, "status": "pending"}

    monkeypatch.setattr(client, "_request", _fake_request)

    try:
        await client.create_task(
            user_id=1,
            main_comment="one",
            text_variants=["one", "two", "three"],
            video_urls=["https://www.tiktok.com/@demo/video/1234567890123456789"],
            kind="full",
            replies=[f"reply {idx}" for idx in range(1, 25)],
        )
    finally:
        await client.close()

    payload = captured_payload["json"]
    assert len(payload["settings"]["replies"]) == 20
    assert "reply21" not in payload["settings"]


@pytest.mark.unit
@pytest.mark.asyncio
async def test_stop_task_posts_user_scoped_endpoint(monkeypatch):
    client = APIClient()
    captured: dict = {}

    async def _fake_request(method, endpoint, json=None, headers=None, timeout=None, retry_safe=True):
        captured["method"] = method
        captured["endpoint"] = endpoint
        captured["json"] = json
        return {"success": True, "task_id": 55, "status": "stopped"}

    monkeypatch.setattr(client, "_request", _fake_request)

    try:
        result = await client.stop_task(task_id=55, user_id=7)
    finally:
        await client.close()

    assert result["status"] == "stopped"
    assert captured["method"] == "POST"
    assert captured["endpoint"] == "/api/tasks/55/stop?user_id=7"
    assert captured["json"] == {}


@pytest.mark.unit
@pytest.mark.asyncio
async def test_full_access_methods_post_expected_endpoints(monkeypatch):
    client = APIClient()
    calls: list[tuple[str, str]] = []

    async def _fake_request(method, endpoint, json=None, headers=None, timeout=None, retry_safe=True):
        calls.append((method, endpoint))
        return {"success": True}

    monkeypatch.setattr(client, "_request", _fake_request)

    try:
        await client.grant_full_access(77)
        await client.revoke_full_access(77)
    finally:
        await client.close()

    assert calls == [
        ("POST", "/api/users/77/full-access/grant"),
        ("POST", "/api/users/77/full-access/revoke"),
    ]


@pytest.mark.unit
@pytest.mark.asyncio
async def test_full_access_methods_clear_cached_user(monkeypatch):
    client = APIClient()
    client._user_cache[90077] = ({"full_access": False}, 1.0)
    client._access_cache[90077] = (False, 1.0)

    async def _fake_request(method, endpoint, json=None, headers=None, timeout=None, retry_safe=True):
        return {"id": 77, "telegram_id": 90077, "full_access": True}

    monkeypatch.setattr(client, "_request", _fake_request)

    try:
        await client.grant_full_access(77)
    finally:
        await client.close()

    assert 90077 not in client._user_cache
    assert 90077 not in client._access_cache


@pytest.mark.unit
@pytest.mark.asyncio
async def test_full_access_methods_clear_all_user_cache_without_telegram_id(monkeypatch):
    client = APIClient()
    client._user_cache[90077] = ({"full_access": False}, 1.0)
    client._user_cache[90088] = ({"full_access": False}, 1.0)
    client._access_cache[90077] = (False, 1.0)

    async def _fake_request(method, endpoint, json=None, headers=None, timeout=None, retry_safe=True):
        return {"id": 77, "full_access": True}

    monkeypatch.setattr(client, "_request", _fake_request)

    try:
        await client.grant_full_access(77)
    finally:
        await client.close()

    assert client._user_cache == {}
    assert client._access_cache == {}
