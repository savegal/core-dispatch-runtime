from types import SimpleNamespace

import pytest

import services.account_service as account_service
import services.video_monitor_worker as monitor_worker


def test_monitor_proxy_fail_open_when_small_proxy_pool_is_all_temporarily_bad(monkeypatch):
    monkeypatch.setattr(account_service.settings, "USE_PROXY", True, raising=False)
    monkeypatch.setattr(account_service.settings, "MONITOR_PROXY_FAIL_OPEN_MAX_UNIQUE_PROXIES", 5, raising=False)
    monkeypatch.setattr(account_service, "get_bad_proxy_set", lambda proxies: set(proxies))
    accounts = [SimpleNamespace(proxy="proxy-a"), SimpleNamespace(proxy="proxy-b")]

    assert account_service._get_monitor_bad_proxy_set_for_accounts(accounts) == set()


def test_monitor_keeps_partial_bad_proxy_set(monkeypatch):
    monkeypatch.setattr(account_service.settings, "USE_PROXY", True, raising=False)
    monkeypatch.setattr(account_service.settings, "MONITOR_PROXY_FAIL_OPEN_MAX_UNIQUE_PROXIES", 5, raising=False)
    monkeypatch.setattr(account_service, "get_bad_proxy_set", lambda proxies: {"proxy-a"})
    accounts = [SimpleNamespace(proxy="proxy-a"), SimpleNamespace(proxy="proxy-b")]

    assert account_service._get_monitor_bad_proxy_set_for_accounts(accounts) == {"proxy-a"}


def test_monitor_does_not_mark_generic_timeout_as_bad_proxy(monkeypatch):
    monkeypatch.setattr(monitor_worker.settings, "MONITOR_MARK_PROXY_BAD_ON_TIMEOUT", False, raising=False)

    assert not monitor_worker._should_mark_proxy_bad_for_monitor(
        "Page.goto: net::ERR_TIMED_OUT at https://www.tiktok.com/@demo",
        error_type="network_error",
    )


def test_monitor_marks_hard_proxy_connection_failure_as_bad_proxy(monkeypatch):
    monkeypatch.setattr(monitor_worker.settings, "MONITOR_MARK_PROXY_BAD_ON_TIMEOUT", False, raising=False)

    assert monitor_worker._should_mark_proxy_bad_for_monitor(
        "Page.goto: net::ERR_PROXY_CONNECTION_FAILED at https://www.tiktok.com/@demo",
        error_type="network_error",
    )


def test_monitor_defers_tiktok_navigation_timeout_without_account_penalty():
    assert monitor_worker._should_defer_monitor_network_timeout(
        "Page.goto: net::ERR_TIMED_OUT at https://www.tiktok.com/@demo\n"
        '  - navigating to "https://www.tiktok.com/@demo", waiting until "domcontentloaded"',
        error_type="network_error",
    )


def test_monitor_does_not_defer_hard_proxy_failure_as_plain_timeout():
    assert not monitor_worker._should_defer_monitor_network_timeout(
        "Page.goto: net::ERR_PROXY_CONNECTION_FAILED at https://www.tiktok.com/@demo",
        error_type="network_error",
    )


def test_navigation_timeout_storm_activates_and_clears(monkeypatch):
    monitor_worker._clear_navigation_timeout_storm()
    monkeypatch.setattr(monitor_worker.settings, "MONITOR_NAVIGATION_TIMEOUT_WINDOW_MINUTES", 5, raising=False)
    monkeypatch.setattr(monitor_worker.settings, "MONITOR_NAVIGATION_TIMEOUT_STORM_THRESHOLD", 2, raising=False)
    monkeypatch.setattr(monitor_worker.settings, "MONITOR_NAVIGATION_TIMEOUT_STORM_DEFER_MINUTES", 3, raising=False)
    now = monitor_worker.datetime(2026, 5, 1, 12, 0, tzinfo=monitor_worker.timezone.utc)

    first = monitor_worker._record_navigation_timeout_event("alpha", now=now)
    second = monitor_worker._record_navigation_timeout_event(
        "beta",
        now=now + monitor_worker.timedelta(seconds=30),
    )

    assert first["storm_active"] is False
    assert second["storm_active"] is True
    assert monitor_worker._get_navigation_timeout_storm_until(now) is not None

    monitor_worker._record_monitor_navigation_success()

    assert monitor_worker._get_navigation_timeout_storm_until(now) is None


def test_tick_summary_distinguishes_deferred_from_checked():
    summary = monitor_worker._summarize_tick_results(
        [
            "network_storm_deferred",
            "network_storm_deferred",
            "network_timeout_deferred",
            "local_browser_unavailable",
            "no_accounts_available",
            "ok_no_new",
            None,
        ]
    )

    assert summary == {
        "checked": 1,
        "deferred_storm": 2,
        "deferred_timeout": 1,
        "deferred_proxy": 0,
        "deferred_browser": 1,
        "no_accounts": 1,
        "errors": 1,
    }


@pytest.mark.asyncio
async def test_local_browser_issue_resets_pool_after_threshold(monkeypatch):
    import executor.browser_pool as browser_pool

    resets = []

    class FakePool:
        async def close_all(self):
            resets.append("reset")

    async def fake_get_browser_pool():
        return FakePool()

    monitor_worker._local_browser_issue_events.clear()
    monitor_worker._last_browser_pool_reset_at = None
    monkeypatch.setattr(monitor_worker.settings, "MONITOR_BROWSER_INFRA_STORM_THRESHOLD", 2, raising=False)
    monkeypatch.setattr(monitor_worker.settings, "MONITOR_BROWSER_INFRA_RESET_COOLDOWN_MINUTES", 10, raising=False)
    monkeypatch.setattr(browser_pool, "get_browser_pool", fake_get_browser_pool)
    now = monitor_worker.datetime(2026, 5, 1, 12, 0, tzinfo=monitor_worker.timezone.utc)

    first = await monitor_worker._maybe_reset_browser_pool_after_local_issue(now=now, reason="Target page closed")
    second = await monitor_worker._maybe_reset_browser_pool_after_local_issue(
        now=now + monitor_worker.timedelta(seconds=10),
        reason="Target page closed",
    )

    assert first is False
    assert second is True
    assert resets == ["reset"]
