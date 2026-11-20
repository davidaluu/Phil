# SPDX-License-Identifier: MIT
# Copyright (c) 2026 David Aluu
import pytest

from phil.actions import Action, ActionError, ActionExecutor
from phil.screen import ScreenTracker
from tests.helpers import APP, FakeDevice, sample_screen


def make_executor():
    tracker = ScreenTracker()
    tracker.update(sample_screen())
    device = FakeDevice()
    return ActionExecutor(device, tracker, sleep=lambda _s: None), device


def test_from_payload_parses_fields():
    action = Action.from_payload(
        {"action": "scroll_forward", "target": " list ", "amount": "0.5", "planStepDone": "true"}
    )
    assert action.name == "scroll_forward"
    assert action.target == "list"
    assert action.amount == 0.5
    assert action.plan_step_done is True


@pytest.mark.parametrize(
    "payload",
    [
        {"action": "teleport"},
        {"action": "click"},
        {"action": "type", "target": "x"},
        {"action": "launch_app"},
        {"action": "click", "target": 5},
        {"action": "scroll_forward", "amount": "lots"},
    ],
)
def test_from_payload_rejects_invalid_actions(payload):
    with pytest.raises(ActionError):
        Action.from_payload(payload)


def test_from_payload_tolerates_dict_target():
    action = Action.from_payload({"action": "click", "target": {"id": "x:id/y", "text": "Y"}})
    assert action.target == "x:id/y"


def test_click_taps_center_of_target():
    executor, device = make_executor()
    result = executor.execute(Action(name="click", target=f"{APP}:id/send"))
    assert result.ok
    assert device.taps == [(990, 2300)]


def test_click_missing_element_fails_cleanly():
    executor, device = make_executor()
    result = executor.execute(Action(name="click", target="nope"))
    assert not result.ok and "not found" in result.detail
    assert device.taps == []


def test_type_focuses_then_types():
    executor, device = make_executor()
    result = executor.execute(Action(name="type", target=f"{APP}:id/entry", text="Hello"))
    assert result.ok
    assert device.taps == [(450, 2300)]
    assert device.typed == ["Hello"]


def test_scroll_forward_swipes_upward_and_backward_downward():
    executor, device = make_executor()
    executor.execute(Action(name="scroll_forward"))
    executor.execute(Action(name="scroll_backward"))
    (fx1, fy1, fx2, fy2), (bx1, by1, bx2, by2) = device.swipes
    assert fy1 > fy2        # finger moves up
    assert by1 < by2        # finger moves down
    assert fx1 == fx2 == bx1 == bx2 == 540


def test_global_and_statusbar_actions():
    executor, device = make_executor()
    executor.execute(Action(name="home"))
    executor.execute(Action(name="enter"))
    executor.execute(Action(name="notifications"))
    assert device.pressed == ["home", "enter"]
    assert device.statusbar_calls == ["expand-notifications"]


def test_launch_app_and_long_click():
    executor, device = make_executor()
    executor.execute(Action(name="launch_app", package="com.android.settings"))
    executor.execute(Action(name="long_click", target=f"{APP}:id/send"))
    assert device.launched == ["com.android.settings"]
    assert device.long_presses == [(990, 2300)]


def test_set_progress_taps_proportionally():
    executor, device = make_executor()
    executor.execute(Action(name="set_progress", target=f"{APP}:id/entry", value=0.5))
    assert device.taps == [(450, 2300)]
