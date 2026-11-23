# SPDX-License-Identifier: MIT
# Phil - Phone Human Interaction Layer
# Copyright (c) 2026 David Aluu
# Licensed under the MIT License. See the LICENSE file in the project root.
import json

import pytest

from main import _delete_shortcut, build_parser
from phil.agent import Agent, RunStatus
from phil.config import Settings
from phil.screen import ScreenTracker
from phil.shortcuts import (
    RunCapture,
    Shortcut,
    ShortcutError,
    ShortcutStore,
    safe_slug,
)
from tests.helpers import APP, FakeDevice, FakeModel, hierarchy, node, sample_screen

SEND = f"{APP}:id/send"


def _shortcut(
    *,
    name: str = "sample",
    expects: list[str] | None = None,
    steps: list[dict] | None = None,
) -> Shortcut:
    return Shortcut(
        name=name,
        aliases=[],
        goal="Tap the send control",
        plan=["Tap send", "Finish"],
        created="2026-01-01T00:00:00+00:00",
        steps=steps
        if steps is not None
        else [
            {
                "action": {"action": "click", "target": SEND},
                "expects": expects or [f"id:{SEND}"],
            }
        ],
    )


def _agent(
    responses: list | None = None,
    *,
    screens: list[str] | None = None,
    confirm=None,
    **settings,
):
    device = FakeDevice(screens)
    model = FakeModel(responses or [])
    agent = Agent(
        device,
        model,
        Settings(
            api_key="test-key",
            confirm_risky=confirm is not None,
            **settings,
        ),
        confirm=confirm,
        sleep=lambda _seconds: None,
    )
    return agent, device, model


def test_round_trip_and_safe_name_sanitizing(tmp_path):
    store = ShortcutStore(tmp_path)
    capture = RunCapture(
        "Open display",
        ["Open display"],
        [{"action": {"action": "back"}, "expects": ["id:settings:id/display"]}],
        True,
    )

    assert safe_slug("../x") == "x"
    saved = store.save("../x", capture)
    loaded = store.load("x")

    assert saved.as_dict() == loaded.as_dict()
    assert list(tmp_path.iterdir()) == [tmp_path / "x.json"]
    with pytest.raises(ShortcutError):
        store.save("///", capture)


@pytest.mark.parametrize(
    "capture",
    [
        RunCapture("goal", ["step"], [], False),
        RunCapture("goal", ["step"], [], True, password_type_targeted=True),
    ],
)
def test_refuses_to_save_failed_or_password_runs(tmp_path, capture):
    with pytest.raises(ShortcutError):
        ShortcutStore(tmp_path).save("unsafe", capture)
    assert list(tmp_path.iterdir()) == []


def test_refuses_to_save_tap_xy_shortcut(tmp_path):
    capture = RunCapture(
        "tap hidden control",
        ["Tap hidden control"],
        [{"action": {"action": "tap_xy", "x": 20, "y": 30}, "expects": []}],
        True,
    )

    with pytest.raises(ShortcutError, match="screen-specific.*empty tree"):
        ShortcutStore(tmp_path).save("unsafe", capture)
    assert list(tmp_path.iterdir()) == []


def test_run_records_successful_actions_and_password_target(tmp_path):
    password_screen = hierarchy(
        node(
            "android.widget.EditText",
            rid=f"{APP}:id/password",
            clickable=True,
            focusable=True,
            password=True,
            bounds="[0,0][500,100]",
        )
    )
    agent, _, _ = _agent(
        [
            {"plan": ["Type password"]},
            {
                "action": "type",
                "target": f"{APP}:id/password",
                "text": "secret text",
            },
            {"action": "done"},
            {"status": "satisfied", "evidence": ["Verified"], "remaining_requirements": [], "reason": "Verified"},
        ],
        screens=[password_screen],
    )

    result = agent.run("type a password")

    assert result.status is RunStatus.COMPLETED
    assert agent.last_run is not None
    assert agent.last_run.password_type_targeted
    assert len(agent.last_run.steps) == 1
    with pytest.raises(ShortcutError, match="password"):
        ShortcutStore(tmp_path).save("unsafe", agent.last_run)


def test_successful_replay_makes_no_model_calls():
    agent, device, model = _agent(
        [{"status": "satisfied", "evidence": ["Goal visible"], "remaining_requirements": [], "reason": "Verified"}],
        vision_enabled=True,
    )

    result = agent.run_shortcut(_shortcut())

    assert result.status is RunStatus.COMPLETED
    assert result.steps == 1
    assert device.taps == [(990, 2300)]
    assert len(model.prompts) == 1
    assert device.screenshot_calls == 1


def test_low_overlap_falls_back_with_replayed_history():
    second_step = {
        "action": {"action": "back"},
        "expects": ["id:not:present"],
    }
    agent, device, model = _agent(
        [
            {"action": "done", "summary": "Finished by AI"},
            {"status": "satisfied", "evidence": ["Goal visible"], "remaining_requirements": [], "reason": "Verified"},
        ],
    )
    shortcut = _shortcut(steps=[_shortcut().steps[0], second_step])

    result = agent.run_shortcut(shortcut)

    assert result.status is RunStatus.COMPLETED
    assert len(device.taps) == 1
    assert len(model.prompts) == 2
    assert "Action History:" in model.prompts[0]
    assert "-> OK:" in model.prompts[0]


def test_shortcut_fallback_can_use_vision():
    second_step = {"action": {"action": "back"}, "expects": ["id:not:present"]}
    agent, device, model = _agent(
        [
            {"action": "done", "summary": "Finished by AI"},
            {"status": "satisfied", "evidence": ["Goal visible"], "remaining_requirements": [], "reason": "Verified"},
        ],
        vision_enabled=True,
        vision_min_elements=10,
    )
    shortcut = _shortcut(steps=[_shortcut().steps[0], second_step])

    result = agent.run_shortcut(shortcut)

    assert result.success
    assert device.screenshot_calls == 2
    assert len(model.prompts) == 2


def test_overlap_exactly_at_threshold_replays():
    expected = [
        f"id:{SEND}",
        f"id:{APP}:id/attach",
        f"id:{APP}:id/entry",
        "id:missing",
    ]
    tracker = ScreenTracker()
    tracker.update(sample_screen())
    assert tracker.overlap_fraction(expected) == 0.75

    agent, device, model = _agent(
        [{"status": "satisfied", "evidence": ["Goal visible"], "remaining_requirements": [], "reason": "Verified"}],
        shortcut_overlap=0.75,
    )
    result = agent.run_shortcut(_shortcut(expects=expected))

    assert result.status is RunStatus.COMPLETED
    assert len(device.taps) == 1
    assert len(model.prompts) == 1


def test_risky_click_during_replay_requires_confirmation_and_decline_aborts():
    asked: list[str] = []
    agent, device, model = _agent(
        confirm=lambda message: asked.append(message) or False,
    )

    result = agent.run_shortcut(_shortcut())

    assert result.status is RunStatus.ABORTED
    assert asked and "send" in asked[0].lower()
    assert device.taps == []
    assert model.prompts == []


def test_listing_order_and_load_by_number_or_name(tmp_path):
    store = ShortcutStore(tmp_path)
    capture = RunCapture("goal", ["step"], [], True)
    store.save("zeta", capture)
    store.save("Alpha", capture)

    assert [item.name for item in store.list_shortcuts()] == ["Alpha", "zeta"]
    assert store.load(1).name == "Alpha"
    assert store.load("zeta").name == "zeta"


def test_delete_by_name_and_number(tmp_path):
    store = ShortcutStore(tmp_path)
    capture = RunCapture("goal", ["step"], [], True)
    store.save("Alpha", capture)
    store.save("Beta", capture)

    assert store.delete("Alpha").name == "Alpha"
    assert [item.name for item in store.list_shortcuts()] == ["Beta"]
    assert store.delete(1).name == "Beta"
    assert store.list_shortcuts() == []


def test_cancelled_delete_keeps_file_and_shows_name_and_step_count(
    tmp_path, monkeypatch, capsys
):
    store = ShortcutStore(tmp_path)
    store.save("Keep Me", RunCapture("goal", ["step"], [{"action": {}, "expects": []}], True))
    monkeypatch.setattr("builtins.input", lambda prompt: "no")

    assert not _delete_shortcut(store, "Keep Me")

    output = capsys.readouterr().out
    assert "Keep Me (1 step)" in output
    assert "Shortcut was not deleted." in output
    assert store.exists("Keep Me")


def test_missing_delete_is_clean_and_suggests_list(tmp_path, capsys):
    store = ShortcutStore(tmp_path)

    assert not _delete_shortcut(store, "missing")

    output = capsys.readouterr().out
    assert "Shortcut not found:" in output
    assert "Use 'list'" in output


def test_parent_traversal_cannot_delete_outside_json(tmp_path):
    shortcut_dir = tmp_path / "shortcuts"
    shortcut_dir.mkdir()
    outside = tmp_path / "outside.json"
    outside.write_text("keep this file", encoding="utf-8")
    store = ShortcutStore(shortcut_dir)

    with pytest.raises(ShortcutError):
        store.delete("../outside")

    assert outside.read_text(encoding="utf-8") == "keep this file"


def test_cli_delete_can_skip_confirmation_with_yes(tmp_path, capsys):
    store = ShortcutStore(tmp_path)
    store.save("No Prompt", RunCapture("goal", ["step"], [], True))

    assert _delete_shortcut(store, "No Prompt", skip_confirmation=True)

    assert "Deleted shortcut 'No Prompt'." in capsys.readouterr().out
    assert store.list_shortcuts() == []


def test_cli_parses_delete_option_with_yes():
    args = build_parser().parse_args(["--delete", "dark mode", "--yes"])

    assert args.delete_shortcut == "dark mode"
    assert args.yes


def test_shortcut_json_has_only_documented_top_level_fields(tmp_path):
    store = ShortcutStore(tmp_path)
    store.save(
        "minimal",
        RunCapture(
            "goal",
            ["step"],
            [{"action": {"action": "back"}, "expects": []}],
            True,
        ),
    )

    data = json.loads((tmp_path / "minimal.json").read_text())

    assert set(data) == {
        "version",
        "name",
        "aliases",
        "goal",
        "plan",
        "created",
        "steps",
        "success_criteria",
    }
