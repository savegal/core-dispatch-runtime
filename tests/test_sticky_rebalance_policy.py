"""Rebalancing fills missing routes and preserves existing account bindings."""
import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from db.models import Account, Proxy
from services import proxy_assignment_service as rebalance
from services.proxy_normalizer import resolve_account_sticky_proxy


RAW_STICKY = "http://worker-{session}-{country}:password@gateway.example:8080"
RAW_MOBILE = "http://other.example:8080"


@pytest.fixture
def make_case(monkeypatch):
    monkeypatch.setattr(
        rebalance, "_is_rebalance_eligible_account",
        lambda *args, **kwargs: (True, "ready", 100),
    )
    monkeypatch.setattr(rebalance, "get_bad_proxy_set", lambda *_: set())
    sessions = []

    def create(*, gateway_status="active", gateway_present=True, account_country="UA"):
        engine = create_engine("sqlite+pysqlite:///:memory:")
        Proxy.__table__.create(engine)
        Account.__table__.create(engine)
        db = Session(engine)
        sessions.append((db, engine))
        source = Proxy(
            id=1, proxy=RAW_STICKY, status=gateway_status,
            is_mobile="N", country_code="UA",
        )
        mobile = Proxy(
            id=2, proxy=RAW_MOBILE, status="active",
            is_mobile="Y", country_code="DE", rotation_url="https://other.example/rotate",
        )
        account = Account(
            id=42, label="sticky-42", cookies="sessionid=abc",
            status="active", error_count_recent=0,
            country_code=account_country,
            proxy=resolve_account_sticky_proxy(RAW_STICKY,
                type("AccountIdentity", (), {"id": 42, "country_code": "UA"})()),
        )
        db.add(mobile)
        if gateway_present:
            db.add(source)
        db.add(account)
        db.commit()
        return db, account

    yield create
    for db, engine in sessions:
        db.close()
        engine.dispose()


def _rebalance(db, **kwargs):
    return rebalance.rebalance_working_account_proxies(
        db, mobile_weight=40, use_geo_matching=False, **kwargs,
    )


def test_active_sticky_account_is_not_moved_for_weighted_targets(make_case):
    db, account = make_case()
    original = account.proxy
    result = _rebalance(db)
    assert account.proxy == original
    assert result["changed"] == 0
    assert result["pinned_sticky_accounts"] == 1


def test_redis_bad_does_not_release_sticky_pin(make_case, monkeypatch):
    db, account = make_case()
    original = account.proxy
    monkeypatch.setattr(rebalance, "get_bad_proxy_set", lambda *_: {RAW_STICKY})
    result = _rebalance(db)
    assert account.proxy == original
    assert result["pinned_accounts"] == 1
    assert result["ignored_temporarily_bad_proxies"] == 1


@pytest.mark.parametrize("status", ["disabled", "slow", "unknown"])
def test_gateway_not_in_active_pool_but_not_bad_preserves_sticky(make_case, status):
    db, account = make_case(gateway_status=status)
    original = account.proxy
    _rebalance(db, force_proxy_type_filter="mobile")
    assert account.proxy == original


def test_country_change_does_not_release_sticky_pin(make_case):
    db, account = make_case(account_country="DE")
    original = account.proxy
    result = _rebalance(db)
    assert result["pinned_sticky_accounts"] == 1
    assert account.proxy == original


@pytest.mark.parametrize("source", ["bad", "deleted"])
def test_rebalancing_keeps_binding_when_source_is_unavailable(make_case, source):
    db, account = make_case(gateway_status="bad", gateway_present=source != "deleted")
    original = account.proxy
    result = _rebalance(db)
    assert account.proxy == original
    assert result["changed"] == 0
    assert result["pinned_accounts"] == 1


def test_type_filter_does_not_release_an_active_residential_pin(make_case):
    db, account = make_case()
    original = account.proxy
    result = _rebalance(db, force_proxy_type_filter="mobile")
    assert account.proxy == original
    assert result["pinned_accounts"] == 1
    assert result["force_proxy_type_filter"] == "mobile"
    assert result["mobile_proxies"] == 1


def test_rebalancing_does_not_infer_account_country_from_proxy(make_case):
    db, account = make_case(gateway_status="bad", account_country=None)
    assert rebalance.clear_proxy_bindings(db, {1}) == 1
    db.commit()
    result = _rebalance(db)
    assert account.proxy == RAW_MOBILE
    assert account.country_code is None
    assert result["changed"] == 1
