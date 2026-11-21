# SPDX-License-Identifier: MIT
# Phil - Phone Human Interaction Layer
# Copyright (c) 2026 David Aluu
# Licensed under the MIT License. See the LICENSE file in the project root.
"""The agent loop: look at the screen, ask the model, do the action, repeat."""

from __future__ import annotations

import logging
import re
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Protocol

from phil.actions import Action, ActionError, ActionExecutor
from phil.adb import Device, DeviceError
from phil.config import Settings
from phil.model import ModelError
from phil.prompts import (
    build_agent_prompt,
    build_agent_user_prompt,
    build_planner_prompt,
    build_planner_user_prompt,
)
from phil.screen import ScreenError, ScreenState, ScreenTracker
from phil.shortcuts import RunCapture, Shortcut, record_step, replay_shortcut

logger = logging.getLogger(__name__)

MAX_PLAN_STEPS = 10
HISTORY_WINDOW = 10
MAX_CONSECUTIVE_ERRORS = 4
_OBSERVE_RETRY_SECONDS = 1.0

# Words on a button that suggest an action that is hard to undo.
_RISKY_PATTERN = re.compile(
    r"(?<![a-z])(send|pay|buy|purchase|order|checkout|delete|remove|erase|transfer|"
    r"submit|post|publish|uninstall|reset)(?![a-z])"
)

# Extra settling time after actions that trigger bigger screen changes (multiplier on the base delay).
_DELAY_MULTIPLIERS: dict[str, float] = {
    "launch_app": 1.5,
    "home": 1.3,
    "recents": 1.3,
    "notifications": 1.3,
    "quick_settings": 1.3,
    "type": 0.7,
}


class SupportsJsonCompletion(Protocol):
    """Anything that can answer a prompt with a JSON object (``ModelClient`` or a test double)."""

    def complete_json(
        self, system_prompt: str, user_prompt: str, *, model: str | None = None
    ) -> dict[str, Any]: ...


class RunStatus(str, Enum):
    """How a run ended."""

    COMPLETED = "completed"
    STUCK = "stuck"
    FAILED = "failed"
    MAX_STEPS = "max_steps"
    ABORTED = "aborted"


@dataclass(frozen=True)
class RunResult:
    """Final outcome of one task."""

    status: RunStatus
    steps: int
    message: str

    @property
    def success(self) -> bool:
        return self.status is RunStatus.COMPLETED


@dataclass
class _RunContext:
    """Mutable bookkeeping for a single run."""

    goal: str
    plan: list[str]
    plan_index: int = 0
    history: list[str] = field(default_factory=list)
    errors: int = 0
    last_signature: str | None = None
    repeats: int = 0
    just_confirmed: bool = False
    user_approved: bool = False
    unchanged_click_signature: str | None = None
    recorded_steps: list[dict[str, Any]] = field(default_factory=list)
    password_type_targeted: bool = False
    completed_steps: int = 0


class Agent:
    """Runs a goal to completion on a connected phone."""

    def __init__(
        self,
        device: Device,
        model: SupportsJsonCompletion,
        settings: Settings,
        *,
        confirm: Callable[[str], bool] | None = None,
        tracker: ScreenTracker | None = None,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        if settings.confirm_risky and confirm is None:
            raise ValueError("A confirm callback is required when confirm_risky is enabled.")
        self._device = device
        self._model = model
        self._settings = settings
        self._confirm = confirm
        self._sleep = sleep
        self._tracker = tracker or ScreenTracker(
            ignored_packages=settings.ignored_packages,
            overlap_threshold=settings.overlap_threshold,
            always_send_full=settings.always_send_full_tree,
        )
        self._executor = ActionExecutor(device, self._tracker, sleep)
        self._planner_prompt = build_planner_prompt()
        self._agent_prompt = build_agent_prompt(settings.stuck_threshold)
        self._last_run: RunCapture | None = None

    # ------------------------------------------------------------------ public

    @property
    def last_run(self) -> RunCapture | None:
        """The most recent run, including whether it completed successfully."""
        return self._last_run

    def run(self, goal: str) -> RunResult:
        """Plan and execute ``goal``. Never raises for expected failures; see ``RunResult``."""
        goal = goal.strip()
        if not goal:
            raise ValueError("The goal must not be empty.")

        self._last_run = None
        self._tracker.reset()
        logger.info("Goal: %s", goal)
        try:
            plan = self._make_plan(goal)
        except ModelError as exc:
            logger.error("Could not create a plan: %s", exc)
            self._last_run = RunCapture(goal, [], [], False)
            return RunResult(RunStatus.FAILED, 0, f"Model error while planning: {exc}")

        logger.info("Plan:\n%s", "\n".join(f"  {i}. {step}" for i, step in enumerate(plan, 1)))
        ctx = _RunContext(goal=goal, plan=plan)
        result = self._loop(ctx)
        self._capture_run(ctx, result)
        return result

    def run_shortcut(self, shortcut: Shortcut) -> RunResult:
        """Replay a shortcut, falling back to the AI from the first unsafe step."""
        plan = list(shortcut.plan) or [shortcut.goal]
        ctx = _RunContext(goal=shortcut.goal, plan=plan)
        outcome = replay_shortcut(
            shortcut,
            self._tracker,
            self._device.dump_ui,
            self._executor.execute,
            lambda action: self._authorize_shortcut_action(ctx, action),
            self._sleep,
            overlap_threshold=self._settings.shortcut_overlap,
            stabilize_delay=self._settings.stabilize_delay,
        )
        ctx.recorded_steps.extend(outcome.replayed_steps)
        ctx.history.extend(outcome.history)
        ctx.completed_steps = len(outcome.replayed_steps)

        if outcome.completed:
            result = RunResult(RunStatus.COMPLETED, ctx.completed_steps, "Shortcut completed.")
        elif outcome.aborted:
            result = RunResult(RunStatus.ABORTED, ctx.completed_steps, outcome.reason)
        else:
            ctx.plan_index = min(ctx.completed_steps, len(ctx.plan) - 1)
            result = self._loop(ctx)

        self._capture_run(ctx, result)
        return result

    def _capture_run(self, ctx: _RunContext, result: RunResult) -> None:
        self._last_run = RunCapture(
            goal=ctx.goal,
            plan=list(ctx.plan),
            steps=list(ctx.recorded_steps),
            completed=result.success,
            password_type_targeted=ctx.password_type_targeted,
        )

    def _authorize_shortcut_action(self, ctx: _RunContext, action: Action) -> bool:
        if action.name == "type":
            node = self._tracker.find(action.target)
            if node is not None and node.password:
                ctx.password_type_targeted = True
        reason = self._risk_reason(ctx, action)
        if reason is None:
            return True
        assert self._confirm is not None
        if self._confirm(f"{reason}\n  Phil wants to run: {action.summary_line()}"):
            return True
        logger.warning("Action declined by the user.")
        return False

    # ---------------------------------------------------------------- planning

    def _make_plan(self, goal: str) -> list[str]:
        """Ask the planner model for steps; fall back to a one-step plan if its reply is unusable."""
        try:
            payload = self._model.complete_json(
                self._planner_prompt,
                build_planner_user_prompt(goal),
                model=self._settings.planner_model,
            )
        except ModelError as exc:
            if exc.fatal:
                raise
            logger.warning("Planner reply was unusable (%s); using the goal as a single step.", exc)
            return [goal]

        raw_plan = payload.get("plan")
        if not isinstance(raw_plan, list):
            logger.warning("Planner returned no plan list; using the goal as a single step.")
            return [goal]
        steps = [str(step).strip() for step in raw_plan if str(step).strip()]
        if not steps:
            logger.warning("Planner returned an empty plan; using the goal as a single step.")
            return [goal]
        return steps[:MAX_PLAN_STEPS]

    # -------------------------------------------------------------------- loop

    def _loop(self, ctx: _RunContext) -> RunResult:
        max_steps = self._settings.max_steps
        for step in range(ctx.completed_steps + 1, max_steps + 1):
            logger.info(
                "Step %d/%d | plan step %d/%d: %s",
                step,
                max_steps,
                ctx.plan_index + 1,
                len(ctx.plan),
                ctx.plan[ctx.plan_index],
            )

            screen = self._observe(ctx)
            if isinstance(screen, RunResult):
                return RunResult(screen.status, step, screen.message)
            if screen is None:
                continue
            if ctx.unchanged_click_signature is not None and screen.changed:
                ctx.unchanged_click_signature = None
            logger.info("Screen: %s", screen.describe())

            action = self._decide(ctx, screen)
            if isinstance(action, RunResult):
                return RunResult(action.status, step, action.message)
            if action is None:
                continue
            logger.info(
                "Action: %s%s",
                action.summary_line(),
                f"  ({action.reason})" if action.reason else "",
            )

            outcome = self._handle(ctx, action, screen, step)
            if outcome is not None:
                return outcome

        logger.warning("Reached the step limit (%d) before finishing.", max_steps)
        return RunResult(
            RunStatus.MAX_STEPS,
            max_steps,
            f"Stopped after {max_steps} steps without finishing. Raise MAX_STEPS in .env to allow more.",
        )

    def _fail_or_retry(self, ctx: _RunContext, what: str, exc: Exception) -> RunResult | None:
        """Count a failure; give up after too many in a row."""
        ctx.errors += 1
        logger.error("%s (%d/%d): %s", what, ctx.errors, MAX_CONSECUTIVE_ERRORS, exc)
        if ctx.errors >= MAX_CONSECUTIVE_ERRORS:
            return RunResult(RunStatus.FAILED, 0, f"{what}: {exc}")
        return None

    def _observe(self, ctx: _RunContext) -> ScreenState | RunResult | None:
        """Read the screen. Returns None to retry, or a RunResult to stop."""
        try:
            return self._tracker.update(self._device.dump_ui())
        except (DeviceError, ScreenError) as exc:
            failure = self._fail_or_retry(ctx, "Could not read the screen", exc)
            if failure is not None:
                return failure
            self._sleep(_OBSERVE_RETRY_SECONDS)
            return None

    def _decide(self, ctx: _RunContext, screen: ScreenState) -> Action | RunResult | None:
        """Ask the model for the next action. Returns None to retry, or a RunResult to stop."""
        prompt = build_agent_user_prompt(
            ctx.goal,
            ctx.plan,
            ctx.plan_index,
            ctx.history[-HISTORY_WINDOW:],
            screen.prompt_text,
            just_confirmed=ctx.just_confirmed,
        )
        ctx.just_confirmed = False
        try:
            payload = self._model.complete_json(
                self._agent_prompt, prompt, model=self._settings.agent_model
            )
            return Action.from_payload(payload)
        except ModelError as exc:
            if exc.fatal:
                logger.error("Model error: %s", exc)
                return RunResult(RunStatus.FAILED, 0, f"Model error: {exc}")
            return self._fail_or_retry(ctx, "The model call failed", exc)
        except ActionError as exc:
            ctx.history.append(f"{len(ctx.history) + 1}. invalid reply -> {exc}")
            return self._fail_or_retry(ctx, "The model returned an invalid action", exc)

    def _handle(
        self, ctx: _RunContext, action: Action, screen: ScreenState, step: int
    ) -> RunResult | None:
        """Carry out one decided action. Returns a RunResult when the run should stop."""
        if action.name == "done":
            message = action.summary or "Goal reached."
            logger.info("Done: %s", message)
            return RunResult(RunStatus.COMPLETED, step, message)

        if action.name == "stuck":
            message = action.reason or action.summary or "The model reported it is stuck."
            logger.warning("Stuck: %s", message)
            return RunResult(RunStatus.STUCK, step, message)

        if action.name == "confirm":
            return self._handle_confirm(ctx, action, step)

        if (
            action.name == "click"
            and ctx.unchanged_click_signature is not None
            and action.signature == ctx.unchanged_click_signature
        ):
            ctx.history.append(
                f"{len(ctx.history) + 1}. {action.signature} -> "
                "Already clicked; the UI tree doesn't show this change. "
                "Treat it as done and go to the next step."
            )
            logger.info("Skipped duplicate click; UI was unchanged after the previous tap.")
            return self._advance_plan(ctx, step)

        if self._is_repeating(ctx, action, screen):
            message = (
                f"Repeated '{action.summary_line()}' more than {self._settings.stuck_threshold} "
                "times without the screen changing."
            )
            logger.warning("Stuck: %s", message)
            return RunResult(RunStatus.STUCK, step, message)

        reason = self._risk_reason(ctx, action)
        if reason is not None:
            assert self._confirm is not None  # guaranteed by __init__ when confirm_risky is set
            if not self._confirm(f"{reason}\n  Phil wants to run: {action.summary_line()}"):
                logger.warning("Action declined by the user.")
                return RunResult(RunStatus.ABORTED, step, "You declined a risky action.")

        if action.name == "type":
            node = self._tracker.find(action.target)
            if node is not None and node.password:
                ctx.password_type_targeted = True
        captured_step = record_step(action, self._tracker)
        result = self._executor.execute(action)
        level = logging.INFO if result.ok else logging.WARNING
        logger.log(level, "Result: %s", result.detail)
        verdict = "OK" if result.ok else "FAILED"
        ctx.history.append(
            f"{len(ctx.history) + 1}. {action.signature} -> {verdict}: {result.detail}"
        )

        if result.ok:
            ctx.recorded_steps.append(captured_step)
            ctx.completed_steps += 1
            ctx.errors = 0
            if action.name == "click":
                post_click = self._observe_after_click(ctx, action)
                if post_click is not None and not post_click.changed:
                    ctx.unchanged_click_signature = action.signature
                    ctx.history.append(
                        f"{len(ctx.history) + 1}. "
                        "Already clicked; the UI tree doesn't show this change. "
                        "Treat it as done and go to the next step."
                    )
                    ctx.history.append(
                        f"{len(ctx.history) + 1}. "
                        "clicked; no visible UI change; toggles often don't change the UI tree; "
                        "treat as applied."
                    )
                    return self._advance_plan(ctx, step)

                ctx.unchanged_click_signature = None

            if action.name != "click":
                ctx.unchanged_click_signature = None

            if action.plan_step_done:
                return self._advance_plan(ctx, step)
        else:
            failure = self._fail_or_retry(ctx, "The action failed", RuntimeError(result.detail))
            if failure is not None:
                return RunResult(failure.status, step, failure.message)

        if action.name != "click":
            self._sleep(self._settings.stabilize_delay * _DELAY_MULTIPLIERS.get(action.name, 1.0))
        return None

    def _observe_after_click(
        self, ctx: _RunContext, action: Action
    ) -> ScreenState | None:
        """Wait for the click to settle and read the resulting UI once."""
        self._sleep(
            self._settings.stabilize_delay
            * _DELAY_MULTIPLIERS.get(action.name, 1.0)
        )
        try:
            return self._tracker.update(self._device.dump_ui())
        except (DeviceError, ScreenError) as exc:
            logger.warning("Could not verify the screen after click: %s", exc)
            return None

    def _advance_plan(self, ctx: _RunContext, step: int) -> RunResult | None:
        """Complete the current plan step and stop if it was the final step."""
        ctx.plan_index += 1
        if ctx.plan_index >= len(ctx.plan):
            logger.info("Final plan step completed.")
            return RunResult(RunStatus.COMPLETED, step, "All plan steps completed.")
        logger.info(
            "Plan step done. Next: %d/%d %s",
            ctx.plan_index + 1,
            len(ctx.plan),
            ctx.plan[ctx.plan_index],
        )
        return None

    def _handle_confirm(self, ctx: _RunContext, action: Action, step: int) -> RunResult | None:
        summary = action.summary or "Phil needs your confirmation before continuing."
        if self._settings.confirm_risky:
            assert self._confirm is not None  # guaranteed by __init__
            if not self._confirm(f"Phil asks: {summary}"):
                logger.warning("Confirmation declined by the user.")
                return RunResult(RunStatus.ABORTED, step, "You declined to continue.")
        ctx.user_approved = True
        ctx.just_confirmed = True
        ctx.history.append(f"{len(ctx.history) + 1}. confirm -> user approved")
        return None

    def _is_repeating(self, ctx: _RunContext, action: Action, screen: ScreenState) -> bool:
        """True once the same action has been proposed too often with no change on screen."""
        if action.name != "wait" and action.signature == ctx.last_signature and not screen.changed:
            ctx.repeats += 1
        else:
            ctx.repeats = 1
        ctx.last_signature = action.signature
        return ctx.repeats > self._settings.stuck_threshold

    def _risk_reason(self, ctx: _RunContext, action: Action) -> str | None:
        """Why this action needs the user's approval, or None if it can just run."""
        if not self._settings.confirm_risky or ctx.user_approved:
            return None
        if action.name not in {"click", "long_click"}:
            return None
        node = self._tracker.find(action.target, climb=True)
        if node is None:
            return None
        label = f"{node.text} {node.desc} {node.resource_id}".lower()
        match = _RISKY_PATTERN.search(label)
        if match is None:
            return None
        return f"This looks like a '{match.group(1)}' control and may be hard to undo."
