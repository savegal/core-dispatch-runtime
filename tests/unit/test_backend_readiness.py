"""Pool diagnostics describe route policy without exposing account credentials."""
import json

import pytest

from db.models import Account, Proxy
from services.backend_readiness import route_policy_summary


def test_route_policy_distinguishes_fixed_sticky_and_rotating_mobile(db_session):
    routes = {
        "fixed": "http://user:private-password@fixed.invalid:8000",
        "sticky": "http://user-session-{session}:private-password@sticky.invalid:8000",
        "mobile": "http://user:private-password@mobile.invalid:8000",
        "broken_mobile": "http://user:private-password@broken-mobile.invalid:8000",
        "dead": "http://user:private-password@dead.invalid:8000",
    }
    for name, route in routes.items():
        db_session.add(Proxy(proxy=route, status="dead" if name == "dead" else "active",
            is_mobile="Y" if "mobile" in name else "N",
            rotation_url="https://rotation.invalid/?key=private-password" if name == "mobile" else None))
    values = [routes["fixed"], routes["fixed"].removeprefix("http://"),
              routes["sticky"], routes["sticky"], routes["mobile"], routes["mobile"],
              routes["broken_mobile"], routes["dead"], None, "http://:bad"]
    db_session.add_all([Account(label=f"fixture-{i}", status="active", proxy=value)
                       for i, value in enumerate(values)])
    db_session.add(Account(label="inactive-account", status="error", proxy=routes["fixed"]))
    db_session.commit()
    report = route_policy_summary(db_session)
    assert report == dict(active_accounts=10, missing_routes=1, invalid_routes=1, inactive_routes=1,
        mobile_accounts=2, sticky_accounts=2, missing_rotation_accounts=1, fixed_residential_routes=1,
        shared_fixed_routes=1, accounts_on_shared_fixed_routes=2, excess_fixed_assignments=1)
    assert "private-password" not in json.dumps(report)
    assert all(type(value) is int for value in report.values())


@pytest.mark.asyncio
async def test_account_list_redacts_entire_proxy_credentials(async_db_session):
    from api.accounts import list_accounts
    async_db_session.add(Account(label="fixture", cookies="sessionid=private-cookie",
        proxy="http://private-user:private-password@proxy.invalid:8000", status="active"))
    await async_db_session.commit()
    result = await list_accounts(status=None, limit=50, offset=0, db=async_db_session)
    rendered = json.dumps(result)
    assert len(result) == 1 and "proxy.invalid" in rendered
    assert all(secret not in rendered for secret in ("private-user", "private-password", "private-cookie"))
