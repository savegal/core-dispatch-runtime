import pytest

from db.models import Account, Proxy
import services.proxy_assignment_service as proxy_assignment


@pytest.mark.unit
def test_rebalance_keeps_temporarily_bad_mobile_proxy_targets(monkeypatch, db_session):
    mobile_a = Proxy(
        proxy="http://mobile-a.example:10001",
        status="active",
        is_mobile="Y",
        rotation_url="https://rotate-a.example",
        fail_count=0,
    )
    mobile_b = Proxy(
        proxy="http://mobile-b.example:10001",
        status="active",
        is_mobile="Y",
        rotation_url="https://rotate-b.example",
        fail_count=0,
    )
    accounts = [
        Account(
            label=f"rebalance_account_{idx}",
            cookies='{"sessionid":"sid","ttwid":"link","msToken":"token"}',
            proxy=mobile_a.proxy if idx < 2 else mobile_b.proxy,
            status="active",
            today_main_comments=0,
            today_replies=0,
            error_count_recent=0,
            validation_fail_count=0,
        )
        for idx in range(4)
    ]
    db_session.add_all([mobile_a, mobile_b, *accounts])
    db_session.commit()

    monkeypatch.setattr(proxy_assignment, "get_bad_proxy_set", lambda proxies: {mobile_a.proxy})
    monkeypatch.setattr(
        proxy_assignment,
        "_is_rebalance_eligible_account",
        lambda account, now: (True, "healthy", 100),
    )

    result = proxy_assignment.rebalance_working_account_proxies(
        db_session,
        mobile_weight=1,
        force_proxy_type_filter="mobile",
        use_geo_matching=False,
        dry_run=True,
    )

    assert result["active_proxies"] == 2
    assert result["changed"] == 0
    assert result["ignored_temporarily_bad_proxies"] == 0
    assert result["kept_temporarily_bad_mobile_proxies"] == 1
    assert result["targets_by_proxy_id"] == {mobile_a.id: 2, mobile_b.id: 2}
