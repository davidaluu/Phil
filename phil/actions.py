# SPDX-License-Identifier: MIT
# Phil - Phone Human Interaction Layer
# Copyright (c) 2026 David Aluu
# Licensed under the MIT License. See the LICENSE file in the project root.
"""Actions: the JSON vocabulary the model speaks, and the code that performs it on the phone."""

from __future__ import annotations

import json
import logging
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Any

from phil.adb import Device, DeviceError
from phil.screen import Node, ScreenTracker
from phil.vision import Screenshot

logger = logging.getLogger(__name__)

CONTROL_ACTIONS = frozenset({"confirm", "done", "stuck", "replan", "take_screenshot"})
KEY_ACTIONS = frozenset({"home", "back", "recents", "enter"})
STATUSBAR_ACTIONS: dict[str, str] = {
    "notifications": "expand-notifications",
    "quick_settings": "expand-settings",
    "dismiss_notification_shade": "collapse",
}
TARGET_REQUIRED = frozenset(
    {"click", "long_click", "type", "expand", "collapse", "dismiss", "focus", "set_progress"}
)
DEVICE_ACTIONS = (
    TARGET_REQUIRED
    | KEY_ACTIONS
    | frozenset(STATUSBAR_ACTIONS)
    | frozenset({"scroll_forward", "scroll_backward", "launch_app", "wait", "tap_xy"})
)
VALID_ACTIONS = DEVICE_ACTIONS | CONTROL_ACTIONS

_SWIPE_MS = 400
_DISMISS_SWIPE_MS = 250
_WAIT_SECONDS = 2.0
_FOCUS_SETTLE_SECONDS = 0.4
_MAX_SCROLL_SPAN = 0.6  # fraction of a region's height covered by amount=1.0
_SUMMARY_TEXT_CHARS = 30


class ActionError(ValueError):
    """Raised when the model's reply is not a valid action."""


def _clamp(value: float, low: float, high: float) -> float:
    return max(low, min(high, value))


def _as_float(payload: Mapping[str, Any], name: str) -> float | None:
    raw = payload.get(name)
    if raw is None or isinstance(raw, bool):
        return None
    try:
        return float(raw)
    except (TypeError, ValueError):
        raise ActionError(f"'{name}' must be a number, got {raw!r}.") from None


def _as_bool(raw: Any) -> bool:
    if isinstance(raw, str):
        return raw.strip().lower() == "true"
    return raw is True


def _as_optional_str(payload: Mapping[str, Any], name: str) -> str | None:
    raw = payload.get(name)
    if raw is None:
        return None
    if not isinstance(raw, str):
        raise ActionError(f"'{name}' must be a string, got {raw!r}.")
    return raw


@dataclass(frozen=True)
class Action:
    """One validated action chosen by the model."""

    name: str
    target: str | None = None
    text: str | None = None
    package: str | None = None
    amount: float | None = None
    value: float | None = None
    x: float | None = None
    y: float | None = None
    summary: str = ""
    reason: str = ""
    plan_step_done: bool = False

    @classmethod
    def from_payload(cls, payload: Mapping[str, Any]) -> Action:
        """Validate a decoded JSON reply and turn it into an ``Action``.

        Raises:
            ActionError: if the action is unknown or required fields are missing.
        """
        name = str(payload.get("action", "")).strip().lower()
        if name not in VALID_ACTIONS:
            raise ActionError(f"Unknown action '{name}'. Valid actions: {sorted(VALID_ACTIONS)}.")

        raw_target = payload.get("target")
        target: str | None
        if isinstance(raw_target, str):
            target = raw_target.strip() or None
        elif isinstance(raw_target, Mapping):
            # Tolerate {"id": ..., "text": ...} from models that ignore the schema.
            candidate = raw_target.get("id") or raw_target.get("text") or raw_target.get("desc")
            target = str(candidate).strip() if candidate else None
        elif raw_target is None:
            target = None
        else:
            raise ActionError(f"'target' must be a string, got {raw_target!r}.")

        text = _as_optional_str(payload, "text")
        package = _as_optional_str(payload, "package")
        if name in TARGET_REQUIRED and target is None:
            raise ActionError(f"Action '{name}' needs a 'target'.")
        if name == "type" and text is None:
            raise ActionError("Action 'type' needs a 'text' value.")
        if name == "launch_app" and not package:
            raise ActionError("Action 'launch_app' needs a 'package' value.")
        x = y = None
        if name == "tap_xy":
            x = _as_float(payload, "x")
            y = _as_float(payload, "y")
            if x is None or y is None:
                raise ActionError("Action 'tap_xy' needs numeric 'x' and 'y' coordinates.")

        plan_done = payload.get("planStepDone", payload.get("plan_step_done"))
        return cls(
            name=name,
            target=target,
            text=text,
            package=package,
            amount=_as_float(payload, "amount"),
            value=_as_float(payload, "value"),
            x=x,
            y=y,
            summary=str(payload.get("summary") or payload.get("message") or "").strip(),
            reason=str(payload.get("reason") or "").strip(),
            plan_step_done=_as_bool(plan_done),
        )

    def as_dict(self) -> dict[str, Any]:
        """The action's meaningful fields, used for history and repeat detection."""
        data: dict[str, Any] = {"action": self.name}
        for key in ("target", "text", "package", "amount", "value", "x", "y"):
            value = getattr(self, key)
            if value is not None:
                data[key] = value
        return data

    @property
    def signature(self) -> str:
        """Stable identity used to detect the same action repeating."""
        return json.dumps(self.as_dict(), sort_keys=True)

    def summary_line(self) -> str:
        """Short human-readable form for terminal logs."""
        parts = [self.name]
        if self.target:
            parts.append(f"target={self.target!r}")
        if self.text is not None:
            shown = self.text[:_SUMMARY_TEXT_CHARS]
            ellipsis = "\u2026" if len(self.text) > _SUMMARY_TEXT_CHARS else ""
            parts.append(f'text="{shown}{ellipsis}"')
        if self.package:
            parts.append(f"package={self.package}")
        return " ".join(parts)


@dataclass(frozen=True)
class ExecutionResult:
    """Outcome of performing an action."""

    ok: bool
    detail: str


def _label(node: Node) -> str:
    return node.text or node.desc or node.resource_id or node.short_class


class ActionExecutor:
    """Performs validated actions on a ``Device``, resolving targets with a ``ScreenTracker``."""

    def __init__(
        self,
        device: Device,
        tracker: ScreenTracker,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self._device = device
        self._tracker = tracker
        self._sleep = sleep
        self._handlers: dict[str, Callable[[Action], ExecutionResult]] = {
            "click": self._click,
            "long_click": self._long_click,
            "expand": self._click,
            "collapse": self._click,
            "focus": self._focus,
            "type": self._type,
            "dismiss": self._dismiss,
            "set_progress": self._set_progress,
            "scroll_forward": lambda action: self._scroll(action, forward=True),
            "scroll_backward": lambda action: self._scroll(action, forward=False),
            "launch_app": self._launch_app,
            "wait": self._wait,
        }
        for key_name in KEY_ACTIONS:
            self._handlers[key_name] = self._key
        for action_name in STATUSBAR_ACTIONS:
            self._handlers[action_name] = self._statusbar

    def execute(
        self, action: Action, *, screenshot: Screenshot | None = None
    ) -> ExecutionResult:
        """Perform ``action``. Device failures become a failed result instead of raising."""
        if action.name == "tap_xy":
            try:
                return self._tap_xy(action, screenshot)
            except DeviceError as exc:
                return ExecutionResult(False, str(exc))
        handler = self._handlers.get(action.name)
        if handler is None:
            return ExecutionResult(False, f"'{action.name}' is not a device action.")
        try:
            return handler(action)
        except DeviceError as exc:
            return ExecutionResult(False, str(exc))

    # ---------------------------------------------------------------- helpers

    def _resolve(self, action: Action, *, climb: bool) -> Node | ExecutionResult:
        node = self._tracker.find(action.target, climb=climb)
        if node is None:
            return ExecutionResult(
                False,
                f"Element not found: {action.target!r}. Use a resource id or text that "
                "appears in the UI tree.",
            )
        return node

    # --------------------------------------------------------------- handlers

    def _tap_xy(self, action: Action, screenshot: Screenshot | None) -> ExecutionResult:
        if screenshot is None:
            return ExecutionResult(False, "Cannot use tap_xy: no screenshot has been sent in this run.")
        assert action.x is not None and action.y is not None
        x, y = action.x, action.y
        if not (0 <= x < screenshot.width and 0 <= y < screenshot.height):
            return ExecutionResult(False, "tap_xy coordinates are outside the screenshot.")

        bounds = self._tracker.screen_bounds()
        if bounds is not None:
            left, top, right, bottom = bounds
            if (
                right - left != screenshot.original_width
                or bottom - top != screenshot.original_height
            ):
                return ExecutionResult(False, "Cannot use tap_xy: screen size changed since the screenshot.")
        else:
            left = top = 0
            right, bottom = screenshot.original_width, screenshot.original_height

        real_x = left + round(x * screenshot.scale_x)
        real_y = top + round(y * screenshot.scale_y)
        if not (left <= real_x < right and top <= real_y < bottom):
            return ExecutionResult(False, "tap_xy coordinates map outside the current screen.")
        self._device.tap(real_x, real_y)
        return ExecutionResult(True, f"Tapped screenshot coordinates ({x:g}, {y:g}) at ({real_x}, {real_y}).")

    def _click(self, action: Action) -> ExecutionResult:
        node = self._resolve(action, climb=True)
        if isinstance(node, ExecutionResult):
            return node
        x, y = node.center
        self._device.tap(x, y)
        return ExecutionResult(True, f"Tapped '{_label(node)}' at ({x}, {y}).")

    def _focus(self, action: Action) -> ExecutionResult:
        node = self._resolve(action, climb=False)
        if isinstance(node, ExecutionResult):
            return node
        x, y = node.center
        self._device.tap(x, y)
        return ExecutionResult(True, f"Focused '{_label(node)}'.")

    def _long_click(self, action: Action) -> ExecutionResult:
        node = self._resolve(action, climb=True)
        if isinstance(node, ExecutionResult):
            return node
        x, y = node.center
        self._device.long_press(x, y)
        return ExecutionResult(True, f"Long-pressed '{_label(node)}' at ({x}, {y}).")

    def _type(self, action: Action) -> ExecutionResult:
        node = self._resolve(action, climb=False)
        if isinstance(node, ExecutionResult):
            return node
        x, y = node.center
        self._device.tap(x, y)  # focus before typing
        self._sleep(_FOCUS_SETTLE_SECONDS)
        self._device.type_text(action.text or "", clear=True)
        return ExecutionResult(True, f"Typed {len(action.text or '')} characters into '{_label(node)}'.")

    def _dismiss(self, action: Action) -> ExecutionResult:
        node = self._resolve(action, climb=True)
        if isinstance(node, ExecutionResult):
            return node
        left, top, right, bottom = node.bounds
        width = right - left
        y = (top + bottom) // 2
        self._device.swipe(left + int(width * 0.15), y, right - int(width * 0.15), y, _DISMISS_SWIPE_MS)
        return ExecutionResult(True, f"Swiped '{_label(node)}' away.")

    def _set_progress(self, action: Action) -> ExecutionResult:
        node = self._resolve(action, climb=False)
        if isinstance(node, ExecutionResult):
            return node
        value = _clamp(action.value if action.value is not None else 0.5, 0.0, 1.0)
        left, top, right, bottom = node.bounds
        x = left + int((right - left) * value)
        y = (top + bottom) // 2
        self._device.tap(x, y)
        return ExecutionResult(True, f"Set '{_label(node)}' to {value:.0%}.")

    def _scroll(self, action: Action, *, forward: bool) -> ExecutionResult:
        if action.target:
            node = self._resolve(action, climb=False)
            if isinstance(node, ExecutionResult):
                return node
            region: tuple[int, int, int, int] | None = node.bounds
        else:
            region = self._tracker.screen_bounds()
        if region is None:
            return ExecutionResult(False, "Cannot scroll: screen size is unknown.")

        left, top, right, bottom = region
        amount = _clamp(action.amount if action.amount is not None else 1.0, 0.1, 1.0)
        span = int((bottom - top) * _MAX_SCROLL_SPAN * amount)
        middle = (top + bottom) // 2
        x = (left + right) // 2
        upper, lower = middle - span // 2, middle + span // 2
        # Scrolling forward moves the content up, so the finger travels upward.
        start_y, end_y = (lower, upper) if forward else (upper, lower)
        self._device.swipe(x, start_y, x, end_y, _SWIPE_MS)
        direction = "forward" if forward else "backward"
        return ExecutionResult(True, f"Scrolled {direction} ({amount:.0%}).")

    def _key(self, action: Action) -> ExecutionResult:
        self._device.press(action.name)
        return ExecutionResult(True, f"Pressed {action.name}.")

    def _statusbar(self, action: Action) -> ExecutionResult:
        self._device.statusbar(STATUSBAR_ACTIONS[action.name])
        return ExecutionResult(True, f"Ran {action.name}.")

    def _launch_app(self, action: Action) -> ExecutionResult:
        package = action.package or ""
        self._device.launch_app(package)
        return ExecutionResult(True, f"Launched {package}.")

    def _wait(self, action: Action) -> ExecutionResult:
        self._sleep(_WAIT_SECONDS)
        return ExecutionResult(True, f"Waited {_WAIT_SECONDS:.0f}s.")
