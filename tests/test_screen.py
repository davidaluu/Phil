# SPDX-License-Identifier: MIT
# Copyright (c) 2026 David Aluu
import pytest

from phil.screen import ScreenError, ScreenTracker, parse_hierarchy
from tests.helpers import APP, hierarchy, node, sample_screen


def test_prune_removes_noise_and_keeps_signal():
    state = ScreenTracker().update(sample_screen())
    text = state.prompt_text
    assert "Chat with David" in text            # plain labels stay readable
    assert "12:00" not in text                   # systemui dropped
    assert '"q"' not in text                     # keyboard keys dropped...
    assert '"Enter"' in text                     # ...except action keys
    assert "[LinearLayout]" not in text          # empty layout dropped
    assert text.count("[ImageView]") == 1        # only the clickable image survives
    assert state.element_count == 5
    assert state.package == APP


def test_password_text_is_never_exposed():
    xml = hierarchy(
        node(
            "android.widget.EditText",
            rid=f"{APP}:id/pw",
            text="hunter2",
            password=True,
            clickable=True,
            bounds="[0,0][500,100]",
        )
    )
    state = ScreenTracker().update(xml)
    assert "hunter2" not in state.prompt_text
    assert "password" in state.prompt_text


def test_class_named_tags_are_supported():
    xml = (
        '<hierarchy rotation="0"><android.widget.Button package="com.a" text="Go" '
        'resource-id="" content-desc="" clickable="true" focusable="false" '
        'scrollable="false" password="false" checked="false" bounds="[0,0][50,50]"/></hierarchy>'
    )
    assert "[Button]" in ScreenTracker().update(xml).prompt_text


def test_malformed_xml_raises_screen_error():
    with pytest.raises(ScreenError):
        parse_hierarchy("<hierarchy><node")


def test_first_step_sends_full_tree():
    state = ScreenTracker().update(sample_screen())
    assert state.mode == "full"
    assert "first step" in state.prompt_text
    assert state.changed is True


def test_identical_screen_sends_full_tree_for_safety():
    tracker = ScreenTracker()
    tracker.update(sample_screen())
    state = tracker.update(sample_screen())
    assert state.changed is False
    assert state.mode == "full"
    assert "no visible changes" in state.prompt_text


def test_small_change_sends_diff_only():
    tracker = ScreenTracker()
    tracker.update(sample_screen())
    state = tracker.update(sample_screen(entry_text="Hello David", extra="Delivered"))
    assert state.mode == "diff"
    assert "UI Changes since last action (4 elements unchanged):" in state.prompt_text
    assert "ADDED elements:" in state.prompt_text and "Delivered" in state.prompt_text
    assert 'CHANGED elements:' in state.prompt_text
    assert '~ [id:com.example.app:id/entry] text: "" \u2192 "Hello David"' in state.prompt_text
    assert state.overlap == 1.0


def test_major_change_falls_back_to_full_tree():
    tracker = ScreenTracker()
    tracker.update(sample_screen())
    other = hierarchy(
        node(
            "android.widget.FrameLayout",
            bounds="[0,0][1080,2400]",
            children=node("android.widget.Button", text="Open Chrome", rid="launcher:id/icon", clickable=True),
        )
    )
    state = tracker.update(other)
    assert state.mode == "full"
    assert state.overlap is not None and state.overlap < 0.4


def test_package_change_sends_full_tree():
    tracker = ScreenTracker()
    tracker.update(sample_screen())
    other = hierarchy(node("android.widget.Button", pkg="com.other", text="Hi", clickable=True))
    state = tracker.update(other)
    assert state.mode == "full"
    assert "com.other" in state.prompt_text


def test_always_send_full_overrides_diff():
    tracker = ScreenTracker(always_send_full=True)
    tracker.update(sample_screen())
    assert tracker.update(sample_screen(entry_text="x")).mode == "full"


def _clickable_row_screen() -> str:
    row = node(
        "android.widget.LinearLayout",
        rid=f"{APP}:id/row",
        clickable=True,
        bounds="[0,500][1080,700]",
        children=node("android.widget.TextView", text="Settings", bounds="[100,550][500,650]"),
    )
    return hierarchy(node("android.widget.FrameLayout", bounds="[0,0][1080,2400]", children=row))


def test_find_climbs_to_clickable_ancestor():
    tracker = ScreenTracker()
    tracker.update(_clickable_row_screen())
    label = tracker.find("Settings")
    assert label is not None and not label.clickable
    row = tracker.find("Settings", climb=True)
    assert row is not None and row.clickable and row.center == (540, 600)


def test_find_by_id_suffix_and_missing_target():
    tracker = ScreenTracker()
    tracker.update(sample_screen())
    assert tracker.find("send") is not None
    assert tracker.find(f"{APP}:id/send") is not None
    assert tracker.find("does-not-exist") is None
    assert tracker.find("") is None and tracker.find(None) is None


def test_duplicate_elements_get_numbered_markers():
    first = node("android.widget.Button", text="A", rid="x:id/icon", clickable=True, bounds="[0,0][100,100]")
    second = node("android.widget.Button", text="B", rid="x:id/icon", clickable=True, bounds="[200,0][300,100]")
    tracker = ScreenTracker()
    state = tracker.update(hierarchy(first, second))
    assert state.prompt_text.count("#2") == 1
    assert tracker.find("x:id/icon#2").center == (250, 50)
    assert tracker.find("x:id/icon#9") is None


def test_screen_bounds_ignores_system_windows():
    tracker = ScreenTracker()
    tracker.update(sample_screen())
    assert tracker.screen_bounds() == (0, 0, 1080, 2400)
