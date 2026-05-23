from types import SimpleNamespace

import pytest

from services import boost_project_client


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
