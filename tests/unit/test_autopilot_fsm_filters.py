import pytest

from bot.handlers.autopilot.common import get_report_filters, set_report_filters


class DummyState:
    def __init__(self):
        self.data = {}
        self.state = None

    async def get_data(self):
        return dict(self.data)

    async def update_data(self, **kwargs):
        self.data.update(kwargs)

    async def set_state(self, state):
        self.state = state


class DummyCallback:
    def __init__(self):
        self.answered = False

    async def answer(self, *args, **kwargs):
        self.answered = True


@pytest.mark.unit
@pytest.mark.asyncio
async def test_autopilot_fsm_filters_persist():
    state = DummyState()

    await set_report_filters(state, ap_period="7d")
    await set_report_filters(state, ap_view="list")
    filters = await get_report_filters(state)
    assert filters["ap_period"] == "7d"
    assert filters["ap_view"] == "list"

    await set_report_filters(state, ap_page=2)
    await set_report_filters(state, ap_view="creators")
    await set_report_filters(state, ap_view="list")
    filters = await get_report_filters(state)
    assert filters["ap_page"] == 2
    assert filters["ap_view"] == "list"


@pytest.mark.unit
@pytest.mark.asyncio
async def test_autopilot_limits_screen_preserves_report_filter_data(monkeypatch):
    from bot.handlers.autopilot import limits

    async def fake_show_limits(_callback):
        return None

    monkeypatch.setattr(limits, "show_limits", fake_show_limits)

    state = DummyState()
    await set_report_filters(state, ap_period="30d", ap_view="list", ap_page=3)

    callback = DummyCallback()
    await limits.limits_screen(callback, state)
    filters = await get_report_filters(state)

    assert callback.answered is True
    assert state.state is None
    assert filters["ap_period"] == "30d"
    assert filters["ap_view"] == "list"
    assert filters["ap_page"] == 3
