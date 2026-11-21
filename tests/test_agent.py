# SPDX-License-Identifier: MIT
# Copyright (c) 2026 David Aluu
import pytest

from phil.agent import Agent, RunStatus
from phil.config import Settings
from phil.model import ModelError
from tests.helpers import APP, FakeDevice, FakeModel, hierarchy, node

SEND = f"{APP}:id/send"


def make_agent(responses, *, confirm=None, screens=None, **overrides):
    settings = Settings(api_key="k", confirm_risky=confirm is not None, **overrides)
    device = FakeDevice(screens)
    model = FakeModel(responses)
    agent = Agent(device, model, settings, confirm=confirm, sleep=lambda _s: None)
    return agent, device, model


def test_completes_when_last_plan_step_is_done():
    agent, device, model = make_agent(
        [
            {"plan": ["Tap send"]},
            {"action": "click", "target": SEND, "planStepDone": True},
        ]
    )
    result = agent.run("send it")
    assert result.status is RunStatus.COMPLETED and result.steps == 1
    assert device.taps == [(990, 2300)]
    assert "\u2192 1. Tap send" in model.prompts[1]
    assert "Current Step (1/1): Tap send" in model.prompts[1]
    assert "Current UI Tree (full \u2014 first step)" in model.prompts[1]


def test_done_action_ends_the_run():
    agent, device, _ = make_agent(
        [{"plan": ["Look"]}, {"action": "done", "summary": "All good"}]
    )
    result = agent.run("look around")
    assert result.success and result.message == "All good" and device.taps == []


def test_unusable_plan_falls_back_to_goal():
    agent, _, model = make_agent(
        [{"plan": "not a list"}, {"action": "done", "summary": "ok"}]
    )
    assert agent.run("do the thing").success
    assert "Current Step (1/1): do the thing" in model.prompts[1]


def test_unchanged_click_is_treated_as_completed():
    click = {"action": "click", "target": SEND}
    agent, device, _ = make_agent([{"plan": ["x"]}] + [click] * 10, stuck_threshold=2)
    result = agent.run("goal")
    assert result.status is RunStatus.COMPLETED
    assert len(device.taps) == 1


def test_unchanged_toggle_click_is_not_repeated_and_advances_plan():
    toggle_screen = hierarchy(
        node(
            "android.widget.Switch",
            rid=f"{APP}:id/dark_mode_toggle",
            text="Dark mode",
            clickable=True,
            bounds="[100,100][300,200]",
        )
    )
    click_toggle = {"action": "click", "target": "dark_mode_toggle"}
    agent, device, model = make_agent(
        [
            {"plan": ["Turn on dark mode", "Continue"]},
            click_toggle,
            click_toggle,
        ],
        screens=[toggle_screen],
    )

    result = agent.run("turn on dark mode")

    assert result.status is RunStatus.COMPLETED
    assert device.taps == [(200, 150)]
    assert "Current Step (2/2): Continue" in model.prompts[2]
    assert (
        "clicked; no visible UI change; toggles often don't change the UI tree; "
        "treat as applied."
    ) in model.prompts[2]


def test_model_reported_stuck_is_returned():
    agent, _, _ = make_agent([{"plan": ["x"]}, {"action": "stuck", "reason": "blocked"}])
    result = agent.run("goal")
    assert result.status is RunStatus.STUCK and result.message == "blocked"


def test_step_limit():
    agent, _, _ = make_agent([{"plan": ["x"]}, {"action": "wait"}, {"action": "wait"}], max_steps=2)
    assert agent.run("goal").status is RunStatus.MAX_STEPS


def test_fatal_model_error_fails_immediately():
    agent, _, _ = make_agent(
        [{"plan": ["x"]}, ModelError("bad key", fatal=True)]
    )
    result = agent.run("goal")
    assert result.status is RunStatus.FAILED and "bad key" in result.message


def test_fatal_planner_error_fails():
    agent, _, _ = make_agent([ModelError("no such model", fatal=True)])
    result = agent.run("goal")
    assert result.status is RunStatus.FAILED and result.steps == 0


def test_repeated_failures_give_up():
    bad = {"action": "click", "target": "missing"}
    agent, _, _ = make_agent([{"plan": ["x"]}] + [bad] * 10, stuck_threshold=50)
    result = agent.run("goal")
    assert result.status is RunStatus.FAILED and "not found" in result.message


def test_invalid_actions_are_retried_then_recover():
    agent, _, model = make_agent(
        [{"plan": ["x"]}, {"action": "teleport"}, {"action": "done", "summary": "ok"}]
    )
    assert agent.run("goal").success
    assert "invalid reply" in model.prompts[2]


def test_risky_click_needs_approval_and_decline_aborts():
    asked = []
    agent, device, _ = make_agent(
        [{"plan": ["x"]}, {"action": "click", "target": SEND}],
        confirm=lambda message: asked.append(message) or False,
    )
    result = agent.run("goal")
    assert result.status is RunStatus.ABORTED
    assert device.taps == [] and "send" in asked[0]


def test_risky_click_runs_when_approved():
    agent, device, _ = make_agent(
        [{"plan": ["x"]}, {"action": "click", "target": SEND, "planStepDone": True}],
        confirm=lambda _m: True,
    )
    assert agent.run("goal").success and len(device.taps) == 1


def test_confirm_action_then_proceed_without_second_prompt():
    asked = []
    agent, device, model = make_agent(
        [
            {"plan": ["x"]},
            {"action": "confirm", "summary": "Send the message?"},
            {"action": "click", "target": SEND, "planStepDone": True},
        ],
        confirm=lambda message: asked.append(message) or True,
    )
    assert agent.run("goal").success
    assert len(asked) == 1 and "Send the message?" in asked[0]
    assert "The user has confirmed. Proceed with the final steps." in model.prompts[2]
    assert len(device.taps) == 1


def test_yes_mode_auto_approves_everything():
    agent, device, _ = make_agent(
        [
            {"plan": ["x"]},
            {"action": "confirm", "summary": "ok?"},
            {"action": "click", "target": SEND, "planStepDone": True},
        ]
    )
    assert agent.run("goal").success and len(device.taps) == 1


def test_confirm_callback_required_when_enabled():
    with pytest.raises(ValueError):
        Agent(FakeDevice(), FakeModel([]), Settings(api_key="k", confirm_risky=True))


def test_empty_goal_is_rejected():
    agent, _, _ = make_agent([])
    with pytest.raises(ValueError):
        agent.run("   ")
