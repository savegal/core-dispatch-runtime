from types import SimpleNamespace

import httpx
import pytest

from services import boost_project_client


@pytest.mark.asyncio
async def test_boost_order_timeout_does_not_create_a_second_order(monkeypatch):
    original_client = httpx.AsyncClient
    orders = []

    def handler(request):
        assert request.url.params["action"] == "add"
        orders.append(request)
        raise httpx.ReadTimeout("receipt lost", request=request)

    monkeypatch.setattr(boost_project_client, "get_settings", lambda: SimpleNamespace(
        BOOST_PROJECT_API_KEY="fixture", BOOST_PROJECT_BASE_URL="https://provider.invalid/api",
        BOOST_PROJECT_TIKTOK_LIKES_SERVICE_ID=5, BOOST_PROJECT_LIKES_QUANTITY=50,
    ))
    monkeypatch.setattr(httpx, "AsyncClient", lambda **kwargs: original_client(
        transport=httpx.MockTransport(handler), **kwargs,
    ))
    result = await boost_project_client.order_tiktok_comment_likes(
        "https://www.tiktok.com/@demo/video/1234567890123456789", "demo", 50,
    )
    assert result == (False, None, boost_project_client.ORDER_OUTCOME_UNKNOWN)
    assert len(orders) == 1


@pytest.mark.asyncio
async def test_boost_balance_distinguishes_provider_error_from_zero(monkeypatch):
    original_client = httpx.AsyncClient
    responses = iter(({"error": "invalid key"}, {"balance": "0.00"}))
    monkeypatch.setattr(boost_project_client, "_api_key", lambda: "fixture")
    monkeypatch.setattr(httpx, "AsyncClient", lambda **kwargs: original_client(
        transport=httpx.MockTransport(lambda request: httpx.Response(200, json=next(responses))), **kwargs,
    ))
    assert await boost_project_client.get_balance() == (False, None, "invalid key")
    assert await boost_project_client.get_balance() == (True, 0.0, None)


@pytest.mark.unit
def test_likes_quantity_uses_product_range(monkeypatch):
    monkeypatch.setattr(
        boost_project_client,
        "get_settings",
        lambda: SimpleNamespace(BOOST_PROJECT_LIKES_QUANTITY=100),
    )

    assert boost_project_client._coerce_likes_quantity(None) == 100
    assert boost_project_client._coerce_likes_quantity("bad") == 100
    assert boost_project_client._coerce_likes_quantity(49) == 50
    assert boost_project_client._coerce_likes_quantity(50) == 50
    assert boost_project_client._coerce_likes_quantity(5000) == 5000
    assert boost_project_client._coerce_likes_quantity(5001) == 5000


@pytest.mark.unit
def test_extract_order_id_accepts_common_api_aliases():
    assert boost_project_client._extract_order_id({"order": "123"}) == 123
    assert boost_project_client._extract_order_id({"order_id": 456}) == 456
    assert boost_project_client._extract_order_id({"id": "789"}) == 789
    assert boost_project_client._extract_order_id({"order": "bad"}) is None


@pytest.mark.unit
@pytest.mark.asyncio
async def test_order_tiktok_comment_likes_uses_documented_get_params(monkeypatch):
    calls = []

    monkeypatch.setattr(
        boost_project_client,
        "get_settings",
        lambda: SimpleNamespace(
            BOOST_PROJECT_API_KEY="test-key",
            BOOST_PROJECT_BASE_URL="https://boost-project.pro/api/v1",
            BOOST_PROJECT_TIKTOK_LIKES_SERVICE_ID=5,
            BOOST_PROJECT_LIKES_QUANTITY=100,
        ),
    )

    class FakeResponse:
        status_code = 200
        text = '{"order": 321}'

        def json(self):
            return {"order": 321}

    class FakeClient:
        def __init__(self, *args, **kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb):
            return None

        async def request(self, method, url, *, params=None, data=None):
            calls.append((method, url, params, data))
            return FakeResponse()

    monkeypatch.setattr(boost_project_client.httpx, "AsyncClient", FakeClient)

    ok, order_id, err = await boost_project_client.order_tiktok_comment_likes(
        "https://www.tiktok.com/@demo/video/1234567890123456789",
        "@target_user",
        quantity=50,
    )

    assert ok is True
    assert order_id == 321
    assert err is None
    assert calls == [
        (
            "GET",
            "https://boost-project.pro/api/v1",
            {
                "key": "test-key",
                "action": "add",
                "service": 5,
                "link": "https://www.tiktok.com/@demo/video/1234567890123456789",
                "username": "target_user",
                "quantity": 50,
            },
            None,
        )
    ]
