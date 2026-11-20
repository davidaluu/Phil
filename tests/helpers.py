# SPDX-License-Identifier: MIT
# Phil - Phone Human Interaction Layer
# Copyright (c) 2026 David Aluu
"""Shared test doubles and XML builders."""

from __future__ import annotations

from typing import Any

APP = "com.example.app"
KEYBOARD = "com.google.android.inputmethod.latin"


def _flag(value: bool) -> str:
    return "true" if value else "false"


def node(
    cls: str = "android.widget.TextView",
    *,
    pkg: str = APP,
    text: str = "",
    rid: str = "",
    desc: str = "",
    clickable: bool = False,
    focusable: bool = False,
    scrollable: bool = False,
    password: bool = False,
    checked: bool = False,
    bounds: str = "[0,0][100,100]",
    children: str = "",
) -> str:
    return (
        f'<node class="{cls}" package="{pkg}" text="{text}" resource-id="{rid}" '
        f'content-desc="{desc}" clickable="{_flag(clickable)}" focusable="{_flag(focusable)}" '
        f'scrollable="{_flag(scrollable)}" password="{_flag(password)}" '
        f'checked="{_flag(checked)}" bounds="{bounds}">{children}</node>'
    )


def hierarchy(*nodes: str) -> str:
    return (
        "<?xml version='1.0' encoding='UTF-8' standalone='yes' ?>"
        '<hierarchy rotation="0">' + "".join(nodes) + "</hierarchy>"
    )


def sample_screen(entry_text: str = "", extra: str = "") -> str:
    """A chat-like screen with noise: status bar, keyboard, empty layout, plain images."""
    body = (
        node("android.widget.LinearLayout", bounds="[0,100][1080,300]")
        + node("android.widget.TextView", text="Chat with David", bounds="[0,100][1080,200]")
        + node("android.widget.ImageView", bounds="[0,200][100,300]")
        + node(
            "android.widget.ImageView",
            rid=f"{APP}:id/attach",
            clickable=True,
            bounds="[900,200][1000,300]",
        )
        + node(
            "android.widget.EditText",
            rid=f"{APP}:id/entry",
            text=entry_text,
            clickable=True,
            focusable=True,
            bounds="[0,2200][900,2400]",
        )
        + node(
            "android.widget.ImageButton",
            rid=f"{APP}:id/send",
            clickable=True,
            bounds="[900,2200][1080,2400]",
        )
    )
    if extra:
        body += node("android.widget.TextView", text=extra, bounds="[0,300][1080,400]")
    keyboard = node(
        "android.widget.FrameLayout",
        pkg=KEYBOARD,
        bounds="[0,1400][1080,2400]",
        children=node(
            "android.widget.TextView", pkg=KEYBOARD, text="q", clickable=True, bounds="[0,1400][100,1500]"
        )
        + node(
            "android.widget.TextView",
            pkg=KEYBOARD,
            text="Enter",
            clickable=True,
            bounds="[900,2300][1080,2400]",
        ),
    )
    return hierarchy(
        node("android.widget.FrameLayout", bounds="[0,0][1080,2400]", children=body),
        node("android.widget.TextView", pkg="com.android.systemui", text="12:00", bounds="[0,0][200,80]"),
        keyboard,
    )


class FakeDevice:
    """Records every gesture; serves queued screen dumps (the last one repeats)."""

    def __init__(self, screens: list[str] | None = None) -> None:
        self.screens = list(screens or [sample_screen()])
        self.taps: list[tuple[int, int]] = []
        self.long_presses: list[tuple[int, int]] = []
        self.swipes: list[tuple[int, int, int, int]] = []
        self.pressed: list[str] = []
        self.typed: list[str] = []
        self.launched: list[str] = []
        self.statusbar_calls: list[str] = []

    def dump_ui(self) -> str:
        return self.screens.pop(0) if len(self.screens) > 1 else self.screens[0]

    def tap(self, x: int, y: int) -> None:
        self.taps.append((x, y))

    def long_press(self, x: int, y: int, duration_ms: int = 800) -> None:
        self.long_presses.append((x, y))

    def swipe(self, x1: int, y1: int, x2: int, y2: int, duration_ms: int = 300) -> None:
        self.swipes.append((x1, y1, x2, y2))

    def press(self, key: str) -> None:
        self.pressed.append(key)

    def type_text(self, text: str, clear: bool = True) -> None:
        self.typed.append(text)

    def launch_app(self, package: str) -> None:
        self.launched.append(package)

    def statusbar(self, command: str) -> None:
        self.statusbar_calls.append(command)


class FakeModel:
    """Returns scripted replies in order; an Exception item is raised instead."""

    def __init__(self, responses: list[Any]) -> None:
        self._responses = list(responses)
        self.prompts: list[str] = []

    def complete_json(
        self, system_prompt: str, user_prompt: str, *, model: str | None = None
    ) -> dict[str, Any]:
        self.prompts.append(user_prompt)
        item = self._responses.pop(0)
        if isinstance(item, Exception):
            raise item
        return item
