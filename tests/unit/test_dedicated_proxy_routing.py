from datetime import datetime, timezone

import pytest

from db.models import Account, Proxy, User
from services.account_service import (
    get_free_account_for_main_comment,
    get_free_account_for_reply,
    release_main_account_reservation,
)
from services.dedicated_proxy_service import (
    activate_dedicated_proxy_for_user,
    reset_dedicated_proxy,
)
import services.proxy_assignment_service as proxy_assignment


@pytest.fixture(autouse=True)
def proxy_mode(monkeypatch):
    from config.settings import get_settings
    monkeypatch.setattr(get_settings(), "USE_PROXY", True)


@pytest.mark.unit
def test_direct_mode_does_not_activate_dedicated_route(monkeypatch):
    from unittest.mock import Mock
    from config.settings import get_settings
    monkeypatch.setattr(get_settings(), "USE_PROXY", False)
    db = Mock()
    assert activate_dedicated_proxy_for_user(db, 1) is None
    db.query.assert_not_called()


@pytest.mark.asyncio
async def test_explicit_active_dedicated_route_survives_global_direct_mode(monkeypatch):
    from types import SimpleNamespace
    from config.settings import get_settings
    from services import dedicated_proxy_service as routing
    from executor.http.http_context import HttpContext
    from executor.http.request_profile import resolve_request_geo
    from executor import browser_pool, tiktok_client
    from tests.unit.test_browser_pool_page_reuse import LaunchProbeBrowser
    from unittest.mock import AsyncMock
    from executor.browser_identity import desktop_user_agent

    monkeypatch.setattr(get_settings(), "USE_PROXY", False)
    route = routing.DedicatedProxyRoute(1, 1, "http://dedicated.invalid:8080", "PL", False, None)
    token = routing._CURRENT_ROUTE.set(route)
    account = SimpleNamespace(id=19, label="forced-route-test", proxy="http://stored.invalid:8000",
                              user_agent="iPhone Mobile", country_code="US")
    pool = browser_pool.BrowserPool(max_browsers=1)
    browser = LaunchProbeBrowser()
    launch = AsyncMock(return_value=browser)
    pool._playwright = SimpleNamespace(chromium=SimpleNamespace(launch=launch), stop=AsyncMock())
    monkeypatch.setattr(tiktok_client, "_get_cookies_for_account", lambda _: '{"sessionid":"forced-route-fixture"}')
    monkeypatch.setattr(tiktok_client, "_normalize_proxy_for_playwright", lambda value: {"server":value})
    try:
        routing.apply_dedicated_proxy(account)
        assert account.proxy == route.proxy and account.proxy_country_code == "PL"
        context = HttpContext.from_account(account, cookies={}, headers={"user-agent":"Chrome/120.0.0.0"})
        assert context.proxy == route.proxy and context.headers["user-agent"] == desktop_user_agent()
        geo = resolve_request_geo(cookies={"tt-target-idc":"useast5"})
        assert geo["region"] == "PL" and geo["tz_name"] == "Europe/Warsaw"
        await pool.get_page_for_account(account)
        assert launch.call_args.kwargs["proxy"]["server"] == route.proxy
        assert "--no-proxy-server" not in launch.call_args.kwargs["args"]
        assert browser.new_context_options[0]["timezone_id"] == "Europe/Warsaw"
    finally:
        await pool.close_all()
        routing.reset_dedicated_proxy(token)


@pytest.mark.unit
@pytest.mark.parametrize("scope", ["main", "reply"])
def test_direct_selection_ignores_proxy_filters_and_keeps_saved_assignment(db_session, monkeypatch, scope):
    from config.settings import get_settings
    from services import account_service
    monkeypatch.setattr(get_settings(), "USE_PROXY", False)
    user = User(telegram_id=12345678, role="user")
    proxy = Proxy(proxy="http://unused.invalid:9999", status="active", is_mobile="N")
    account = _shared_account("direct-test", proxy.proxy)
    db_session.add_all([user, proxy, account])
    db_session.flush()
    proxy.dedicated_user_id = user.id
    db_session.commit()
    saved_proxy = proxy.proxy
    account_id = account.id

    def no_proxy_filter(*args):
        raise AssertionError("Direct selection must not load proxy type preferences")

    monkeypatch.setattr(account_service, "get_proxy_type_filter", no_proxy_filter)
    monkeypatch.setattr(account_service, "get_proxy_prefer_mobile", no_proxy_filter)
    selector = get_free_account_for_main_comment if scope == "main" else get_free_account_for_reply
    selected = selector(db_session, exclude_proxies={saved_proxy})
    try:
        assert selected is not None and selected.id == account_id
        assert selected.proxy is None
        # A real unrelated update/commit must not erase the stored proxy.
        selected.label = "direct-test-updated"
        db_session.commit()
        db_session.expire_all()
        assert db_session.get(Account, account_id).proxy == saved_proxy
    finally:
        release_main_account_reservation(account_id)


def _shared_account(label: str, proxy: str | None) -> Account:
    return Account(
        label=label,
        status="active",
        cookies=(
            '{"sessionid":"session","sid_tt":"sid","sid_guard":"guard",'
            '"s_v_web_id":"verify","ttwid":"wid","msToken":"token"}'
        ),
        proxy=proxy,
        last_validation_ok=datetime.now(timezone.utc),
        today_main_comments=0,
        today_replies=0,
        error_count_recent=0,
        validation_fail_count=0,
        owner_user_id=None,
    )


@pytest.mark.unit
def test_shared_accounts_use_dedicated_proxy_without_persisting_it(db_session):
    user = User(telegram_id=1104900609, username="dedicated", role="user")
    regular = Proxy(proxy="http://regular.example:8000", status="active", is_mobile="N")
    dedicated = Proxy(
        proxy="http://login:password@dedicated.example:9000",
        status="active",
        is_mobile="N",
        dedicated_user_id=None,
        country_code="US",
    )
    account = _shared_account("shared_dedicated_route", regular.proxy)
    db_session.add_all([user, regular, dedicated, account])
    db_session.flush()
    dedicated.dedicated_user_id = user.id
    db_session.commit()

    token = activate_dedicated_proxy_for_user(db_session, user.id)
    try:
        selected = get_free_account_for_main_comment(
            db_session,
            exclude_proxies={dedicated.proxy},
        )
        assert selected is not None
        assert selected.id == account.id
        assert selected.proxy == dedicated.proxy
        release_main_account_reservation(selected.id)

        reply = get_free_account_for_reply(
            db_session,
            exclude_proxies={dedicated.proxy},
        )
        assert reply is not None
        assert reply.id == account.id
        assert reply.proxy == dedicated.proxy
        db_session.commit()
    finally:
        reset_dedicated_proxy(token)

    db_session.expire_all()
    assert db_session.get(Account, account.id).proxy == regular.proxy


@pytest.mark.unit
def test_dedicated_proxy_is_not_assigned_to_shared_accounts(db_session):
    user = User(telegram_id=1104900609, username="dedicated", role="user")
    regular = Proxy(proxy="http://regular-only.example:8000", status="active", is_mobile="N")
    dedicated = Proxy(
        proxy="http://dedicated-only.example:9000",
        status="active",
        is_mobile="N",
        country_code="US",
    )
    account = _shared_account("shared_assignment_target", None)
    db_session.add_all([user, regular, dedicated, account])
    db_session.flush()
    dedicated.dedicated_user_id = user.id
    db_session.commit()
    proxy_assignment.invalidate_proxy_cache()

    result = proxy_assignment.assign_proxies_to_accounts(
        db_session,
        only_accounts_without_proxy=True,
        reuse_existing=False,
        force_proxy_type_filter="residential",
        use_geo_matching=False,
    )

    db_session.refresh(account)
    assert result["assigned"] == 1
    assert account.proxy == regular.proxy


@pytest.mark.unit
def test_ordinary_orders_cannot_select_account_on_dedicated_proxy(db_session):
    user = User(telegram_id=1104900609, username="dedicated", role="user")
    dedicated = Proxy(
        proxy="http://dedicated-hidden.example:9000",
        status="active",
        is_mobile="N",
        country_code="US",
    )
    hidden_account = _shared_account("shared_on_dedicated_proxy", dedicated.proxy)
    regular_account = _shared_account("shared_on_regular_proxy", "http://regular-visible.example:8000")
    db_session.add_all([user, dedicated, hidden_account, regular_account])
    db_session.flush()
    dedicated.dedicated_user_id = user.id
    db_session.commit()

    selected = get_free_account_for_main_comment(db_session)

    assert selected is not None
    assert selected.id == regular_account.id
    release_main_account_reservation(selected.id)
