import pytest

from services import monitor_incident_service as incidents
from services import notify_service


class FakeRedis:
    def __init__(self):
        self.values = {}

    def set(self, key, value, ex=None):
        self.values[key] = value
        return True

    def get(self, key):
        return self.values.get(key)

    def delete(self, key):
        self.values.pop(key, None)

    def scan_iter(self, pattern):
        prefix = pattern.rstrip("*")
        return [key for key in self.values if key.startswith(prefix)]


def test_classifies_real_monitor_errors():
    assert incidents.classify_monitor_error(
        "Page.goto: net::ERR_TIMED_OUT at https://www.tiktok.com/@demo",
        error_type="network_error",
    ) == incidents.INCIDENT_NAVIGATION_TIMEOUT_STORM
    assert incidents.classify_monitor_error(
        "Page.goto: net::ERR_PROXY_CONNECTION_FAILED at https://www.tiktok.com/@demo",
        error_type="network_error",
    ) == incidents.INCIDENT_HARD_PROXY_FAILURE
    assert incidents.classify_monitor_error(
        "Failed to create browser context: Target page, context or browser has been closed",
    ) == incidents.INCIDENT_LOCAL_BROWSER_INFRA
    assert incidents.classify_monitor_error(
        "TelegramNetworkError: Cannot connect to host api.telegram.org:443",
    ) == incidents.INCIDENT_TELEGRAM_DELIVERY
    assert incidents.classify_monitor_error(
        "SUSPICIOUS: Temporary page error detected: Something went wrong",
    ) == incidents.INCIDENT_TIKTOK_REGION_DEGRADATION


def test_incident_lifecycle_and_automation_state(monkeypatch):
    fake_redis = FakeRedis()
    monkeypatch.setattr(incidents, "get_redis_client", lambda: fake_redis)
    incidents.record_incident(
        incidents.INCIDENT_NAVIGATION_TIMEOUT_STORM,
        severity="warning",
        title="Monitor timeout storm",
        cause="4/4 timeouts",
        auto_action="paused",
        admin_action="wait",
        cooldown_until="2026-05-01T17:55:00+00:00",
    )

    active = incidents.list_monitor_incidents(include_recovered=False)
    assert active[0]["type"] == incidents.INCIDENT_NAVIGATION_TIMEOUT_STORM
    state = incidents.get_automation_state(active)
    assert state["mode"] == "network_pause"
    assert state["pause_until"] == "2026-05-01T17:55:00+00:00"

    assert incidents.resolve_incident(incidents.INCIDENT_NAVIGATION_TIMEOUT_STORM)
    current = incidents.list_monitor_incidents(include_recovered=True)
    assert current[0]["type"] == incidents.INCIDENT_RECOVERED


def test_tiktok_degradation_has_non_normal_automation_state():
    active = [
        {
            "type": incidents.INCIDENT_TIKTOK_REGION_DEGRADATION,
            "state": "active",
            "cooldown_until": "2026-05-01T18:10:00+00:00",
        }
    ]

    state = incidents.get_automation_state(active)

    assert state["mode"] == "tiktok_degraded_pause"
    assert state["pause_until"] == "2026-05-01T18:10:00+00:00"


def test_admin_alert_has_required_sections():
    text = incidents.build_admin_alert(
        severity="warning",
        title="Monitor test",
        what="timeout storm",
        why="ERR_TIMED_OUT",
        auto_action="paused",
        admin_action="wait",
        next_check="5m",
    )

    assert "Что происходит:" in text
    assert "Почему софт так решил:" in text
    assert "Что софт уже сделал:" in text
    assert "Что нужно админу:" in text
    assert "Следующая проверка:" in text


@pytest.mark.asyncio
async def test_owner_notification_markdown_fallback(monkeypatch):
    calls = []

    class FakeSession:
        async def close(self):
            pass

    class FakeBot:
        def __init__(self, token, session):
            self.session = FakeSession()

        async def send_message(self, owner_id, message, parse_mode=None, reply_markup=None, **_kwargs):
            calls.append((owner_id, message, parse_mode))
            if len(calls) == 1:
                raise notify_service.TelegramBadRequest(method=None, message="can't parse entities")

    monkeypatch.setitem(notify_service.config, "BOT_TOKEN", "token")
    monkeypatch.setattr(notify_service.settings, "OWNER_TELEGRAM_ID", 123, raising=False)
    monkeypatch.setattr(notify_service, "Bot", FakeBot)
    monkeypatch.setattr(notify_service, "resolve_incident", lambda *args, **kwargs: False)

    assert await notify_service.send_notification_to_owner("*broken_markdown") is True
    assert len(calls) == 2
    assert calls[0][2] == "Markdown"
    assert calls[1][2] is None
