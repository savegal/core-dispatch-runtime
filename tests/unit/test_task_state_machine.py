import pytest

from services.task_state_machine import (
    TaskStateTransitionError,
    can_transition_to,
    get_allowed_transitions,
    validate_status_transition,
)


@pytest.mark.unit
def test_running_task_can_be_stopped_and_stopped_is_terminal():
    assert validate_status_transition("in_progress", "stopped") is True
    assert can_transition_to("in_progress", "stopped") is True
    assert get_allowed_transitions("stopped") == set()

    with pytest.raises(TaskStateTransitionError):
        validate_status_transition("stopped", "pending")


@pytest.mark.unit
def test_pending_task_can_be_stopped_before_start():
    assert validate_status_transition("pending", "stopped") is True
    assert can_transition_to("pending", "stopped") is True
