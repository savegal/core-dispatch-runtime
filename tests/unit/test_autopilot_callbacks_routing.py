import pytest

from bot.handlers import autopilot
from bot.middleware.software_mode_middleware import SoftwareModeMiddleware


def _callback_names(router):
    names = [handler.callback.__name__ for handler in router.callback_query.handlers]
    for child in router.sub_routers:
        names.extend(_callback_names(child))
    return set(names)


@pytest.mark.unit
def test_autopilot_callbacks_routing_registered():
    names = _callback_names(autopilot.router)

    assert {"show_main", "limits_screen", "current_screen", "work_screen", "report_screen", "card_screen"}.issubset(names)
    assert {"do_cancel_all", "do_skip_queue", "do_retry", "do_hide", "do_notify_admin"}.issubset(names)
    assert {"alias_autopilot_report", "alias_current", "alias_mode", "alias_limits"}.issubset(names)


@pytest.mark.unit
def test_autopilot_callbacks_allowed_in_parsing_mode():
    middleware = SoftwareModeMiddleware(allow_video_monitoring=True)

    for callback_data in [
        "ap:main",
        "ap:main:skipq",
        "ap:limits",
        "ap:current",
        "ap:w",
        "ap:w:t:plan",
        "ap:w:cv:4220",
        "ap:w:st:4220",
        "ap:current:skipq",
        "ap:rep:tab:list",
        "ap:card:4220",
        "ap:c:2147483647:oc",
        "ap:a:r:2147483647",
        "ap:action:skipq:do",
        "ap:action:archive:ask",
    ]:
        assert middleware._is_allowed_callback(callback_data) is True
