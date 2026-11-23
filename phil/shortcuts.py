# SPDX-License-Identifier: MIT
# Phil - Phone Human Interaction Layer
# Copyright (c) 2026 David Aluu
# Licensed under the MIT License. See the LICENSE file in the project root.
"""Store and replay recorded phone-action shortcuts."""

from __future__ import annotations

import json
import logging
import re
import tempfile
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from phil.actions import Action, ActionError, ExecutionResult
from phil.adb import DeviceError
from phil.screen import ScreenError, ScreenTracker

logger = logging.getLogger(__name__)

SHORTCUT_VERSION = 1
_INVALID_SLUG_CHARS = re.compile(r"[^a-z0-9]+")


class ShortcutError(ValueError):
    """Raised when a shortcut name or file is invalid."""


@dataclass(frozen=True)
class RunCapture:
    """Actions and metadata captured from one completed or incomplete run."""

    goal: str
    plan: list[str]
    steps: list[dict[str, Any]]
    completed: bool
    password_type_targeted: bool = False
    success_criteria: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class Shortcut:
    """A shortcut loaded from its JSON representation."""

    name: str
    goal: str
    plan: list[str]
    steps: list[dict[str, Any]]
    created: str
    aliases: list[str]
    version: int = SHORTCUT_VERSION
    success_criteria: list[str] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        """Return the persisted JSON shape."""
        return {
            "version": self.version,
            "name": self.name,
            "aliases": self.aliases,
            "goal": self.goal,
            "plan": self.plan,
            "created": self.created,
            "steps": self.steps,
            "success_criteria": list(self.success_criteria),
        }


@dataclass(frozen=True)
class ReplayOutcome:
    """Outcome of the replay-only phase."""

    completed: bool
    aborted: bool
    replayed_steps: list[dict[str, Any]]
    history: list[str]
    reason: str = ""


def safe_slug(name: str) -> str:
    """Turn a shortcut name into a lowercase, path-safe filename stem."""
    slug = _INVALID_SLUG_CHARS.sub("-", name.strip().lower()).strip("-")
    if not slug:
        raise ShortcutError("Shortcut name must contain at least one letter or digit.")
    return slug


def record_step(action: Action, tracker: ScreenTracker) -> dict[str, Any]:
    """Capture a validated action and the relevant keys on its pre-action screen."""
    climb = action.name in {"click", "long_click", "expand", "collapse", "dismiss"}
    return {
        "action": action.as_dict(),
        "expects": tracker.expected_keys(action.target, climb=climb),
    }


class ShortcutStore:
    """Read and write one JSON file per shortcut in a configured directory."""

    def __init__(self, directory: str | Path = "shortcuts") -> None:
        self.directory = Path(directory)

    def _path(self, name: str) -> Path:
        path = self.directory / f"{safe_slug(name)}.json"
        if path.resolve().parent != self.directory.resolve():
            raise ShortcutError("Shortcut path must remain inside the shortcuts directory.")
        return path

    def list_shortcuts(self) -> list[Shortcut]:
        """Return shortcuts in case-insensitive name order."""
        if not self.directory.exists():
            return []
        shortcuts = [self._read(path) for path in self.directory.glob("*.json")]
        return sorted(shortcuts, key=lambda shortcut: (shortcut.name.casefold(), shortcut.name))

    def exists(self, name: str) -> bool:
        """Return whether a shortcut with this slug already exists."""
        return self._path(name).exists()

    def save(
        self,
        name: str,
        run: RunCapture,
        *,
        overwrite: bool = False,
    ) -> Shortcut:
        """Persist a completed run, refusing unsafe or incomplete recordings."""
        safe_slug(name)
        if not run.completed:
            raise ShortcutError("Only a run that ended completed can be saved.")
        if run.password_type_targeted:
            raise ShortcutError("A run that typed into a password field cannot be saved.")
        if any(
            step.get("action", {}).get("action") == "tap_xy"
            for step in run.steps
            if isinstance(step, dict) and isinstance(step.get("action"), dict)
        ):
            raise ShortcutError(
                "A run with tap_xy steps cannot be saved: coordinates are screen-specific, "
                "and the element check would pass trivially on an empty tree."
            )

        path = self._path(name)
        if path.exists() and not overwrite:
            raise ShortcutError(f"A shortcut named {name!r} already exists.")

        shortcut = Shortcut(
            name=name.strip(),
            aliases=[],
            goal=run.goal,
            plan=list(run.plan),
            created=datetime.now(timezone.utc).isoformat(),
            steps=list(run.steps),
            success_criteria=list(run.success_criteria),
        )
        self.directory.mkdir(parents=True, exist_ok=True)
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            dir=self.directory,
            prefix=f".{path.stem}.",
            suffix=".tmp",
            delete=False,
        ) as file:
            json.dump(shortcut.as_dict(), file, ensure_ascii=False, indent=2)
            file.write("\n")
            temporary_path = Path(file.name)
        temporary_path.replace(path)
        return shortcut

    def load(self, reference: str | int) -> Shortcut:
        """Load a shortcut by 1-based list number or by name."""
        if isinstance(reference, int) or reference.strip().isdigit():
            try:
                number = int(reference)
            except (TypeError, ValueError):
                number = 0
            shortcuts = self.list_shortcuts()
            if number < 1 or number > len(shortcuts):
                raise ShortcutError(f"No shortcut numbered {reference}.")
            return shortcuts[number - 1]

        path = self._path(reference)
        if not path.is_file():
            raise ShortcutError(f"No shortcut named {reference!r}.")
        shortcut = self._read(path)
        if safe_slug(shortcut.name) != path.stem:
            raise ShortcutError(f"Shortcut file {path.name!r} has a mismatched name.")
        return shortcut

    def delete(self, reference: str | int) -> Shortcut:
        """Delete a shortcut JSON file by its 1-based list number or name."""
        shortcut = self.load(reference)
        path = self._path(shortcut.name)
        if path.suffix != ".json" or path.resolve().parent != self.directory.resolve():
            raise ShortcutError("Shortcut path must remain inside the shortcuts directory.")
        try:
            path.unlink()
        except OSError as exc:
            raise ShortcutError(f"Could not delete shortcut {shortcut.name!r}: {exc}") from exc
        logger.info("Deleted shortcut '%s'", shortcut.name)
        return shortcut

    def _read(self, path: Path) -> Shortcut:
        if path.resolve().parent != self.directory.resolve():
            raise ShortcutError(f"Shortcut file {path.name!r} is outside the shortcuts directory.")
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise ShortcutError(f"Could not read shortcut {path.name!r}: {exc}") from exc
        if not isinstance(raw, dict):
            raise ShortcutError(f"Shortcut {path.name!r} must contain a JSON object.")

        name = raw.get("name")
        goal = raw.get("goal")
        created = raw.get("created")
        plan = raw.get("plan")
        aliases = raw.get("aliases")
        steps = raw.get("steps")
        success_criteria = raw.get("success_criteria", [])
        if (
            raw.get("version") != SHORTCUT_VERSION
            or not isinstance(name, str)
            or not isinstance(goal, str)
            or not isinstance(created, str)
            or not isinstance(plan, list)
            or not all(isinstance(item, str) for item in plan)
            or not isinstance(aliases, list)
            or not all(isinstance(item, str) for item in aliases)
            or not isinstance(steps, list)
            or not isinstance(success_criteria, list)
            or not all(isinstance(item, str) for item in success_criteria)
        ):
            raise ShortcutError(f"Shortcut {path.name!r} has an unsupported or invalid format.")
        validated_steps: list[dict[str, Any]] = []
        for step in steps:
            if (
                not isinstance(step, dict)
                or not isinstance(step.get("action"), dict)
                or not isinstance(step.get("expects"), list)
                or not all(
                    isinstance(key, str) and not key.startswith("pos:")
                    for key in step["expects"]
                )
            ):
                raise ShortcutError(f"Shortcut {path.name!r} contains an invalid step.")
            validated_steps.append({"action": step["action"], "expects": step["expects"]})
        safe_slug(name)
        return Shortcut(
            name=name,
            goal=goal,
            plan=list(plan),
            steps=validated_steps,
            created=created,
            aliases=list(aliases),
            version=SHORTCUT_VERSION,
            success_criteria=list(success_criteria),
        )


def replay_shortcut(
    shortcut: Shortcut,
    tracker: ScreenTracker,
    read_screen: Callable[[], str],
    execute: Callable[[Action], ExecutionResult],
    authorize: Callable[[Action], bool],
    sleep: Callable[[float], None],
    *,
    overlap_threshold: float,
    stabilize_delay: float,
) -> ReplayOutcome:
    """Replay matching shortcut steps and stop at the first step needing AI fallback."""
    started = time.perf_counter()
    logger.info("Replaying shortcut '%s' (%d steps)", shortcut.name, len(shortcut.steps))
    outcome = _replay_steps(
        shortcut,
        tracker,
        read_screen,
        execute,
        authorize,
        sleep,
        overlap_threshold=overlap_threshold,
        stabilize_delay=stabilize_delay,
    )
    logger.info(
        "Shortcut replay finished in %.2f seconds (%d steps replayed)",
        time.perf_counter() - started,
        len(outcome.replayed_steps),
    )
    return outcome


def _replay_steps(
    shortcut: Shortcut,
    tracker: ScreenTracker,
    read_screen: Callable[[], str],
    execute: Callable[[Action], ExecutionResult],
    authorize: Callable[[Action], bool],
    sleep: Callable[[float], None],
    *,
    overlap_threshold: float,
    stabilize_delay: float,
) -> ReplayOutcome:
    tracker.reset()
    replayed: list[dict[str, Any]] = []
    history: list[str] = []
    for index, step in enumerate(shortcut.steps):
        try:
            tracker.update(read_screen())
        except (DeviceError, ScreenError) as exc:
            return _fallback(shortcut, index, replayed, history, f"could not read screen: {exc}")

        expects = step["expects"]
        fraction = tracker.overlap_fraction(expects)
        logger.info(
            "Shortcut step %d/%d: screen match %.0f%%",
            index + 1,
            len(shortcut.steps),
            fraction * 100,
        )
        if fraction < overlap_threshold:
            return _fallback(
                shortcut, index, replayed, history, f"screen matched only {fraction:.0%}"
            )

        try:
            action = Action.from_payload(step["action"])
        except ActionError as exc:
            return _fallback(shortcut, index, replayed, history, f"invalid saved action: {exc}")

        if not authorize(action):
            return ReplayOutcome(
                completed=False,
                aborted=True,
                replayed_steps=replayed,
                history=history,
                reason="You declined a risky action.",
            )

        result = execute(action)
        if not result.ok:
            logger.warning("Shortcut action failed: %s", result.detail)
            return _fallback(shortcut, index, replayed, history, result.detail)

        replayed.append({"action": action.as_dict(), "expects": list(expects)})
        history.append(f"{len(history) + 1}. {action.signature} -> OK: {result.detail}")
        if index + 1 < len(shortcut.steps):
            sleep(stabilize_delay)

    return ReplayOutcome(True, False, replayed, history)


def _fallback(
    shortcut: Shortcut,
    index: int,
    replayed: list[dict[str, Any]],
    history: list[str],
    reason: str,
) -> ReplayOutcome:
    logger.warning(
        "Shortcut replay falling back to the AI at step %d/%d: %s",
        index + 1,
        len(shortcut.steps),
        reason,
    )
    return ReplayOutcome(False, False, replayed, history, reason)
