# SPDX-License-Identifier: MIT
# Phil - Phone Human Interaction Layer
# Copyright (c) 2026 David Aluu
# Licensed under the MIT License. See the LICENSE file in the project root.
"""The agent loop: look at the screen, ask the model, do the action, repeat."""

from __future__ import annotations

import json
import logging
import re
import time
from collections import Counter
from collections.abc import Callable
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Protocol

from phil.actions import CONTROL_ACTIONS, Action, ActionError, ActionExecutor
from phil.adb import Device, DeviceError
from phil.config import Settings
from phil.model import ModelError, ModelImageUnsupportedError
from phil.prompts import (
    build_agent_prompt,
    build_agent_user_prompt,
    build_planner_prompt,
    build_planner_user_prompt,
    build_replan_prompt,
    build_replan_user_prompt,
    build_verification_prompt,
    build_verification_user_prompt,
)
from phil.runs import RunRecorder, RunStore, timestamp_now
from phil.screen import ScreenError, ScreenState, ScreenTracker
from phil.shortcuts import RunCapture, Shortcut, record_step, replay_shortcut
from phil.vision import Screenshot, pillow_available, prepare_screenshot, save_screenshot

logger = logging.getLogger(__name__)

MAX_PLAN_STEPS = 20
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
        self,
        system_prompt: str,
        user_prompt: str,
        *,
        model: str | None = None,
        images: list[bytes] | None = None,
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
    success_criteria: list[str] = field(default_factory=list)
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
    screenshot_attempts: int = 0
    last_screenshot: Screenshot | None = None
    pending_screenshot_reason: str | None = None
    device_action_count: int = 0
    last_screenshot_action_count: int = -1
    done_verification_attempted: bool = False
    replan_attempts: int = 0
    verification_attempts: int = 0
    repeat_vision_checkpoint: int = 0
    error_vision_checkpoint: int = 0


@dataclass
class _VisionStats:
    screenshots_sent: int = 0
    reasons: Counter[str] = field(default_factory=Counter)
    estimated_extra_tokens: int = 0
    extra_seconds: float = 0.0
    text_prompt_tokens: list[int] = field(default_factory=list)


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
        self._last_run: RunCapture | None = None
        self._run_store = RunStore(settings.records_dir)
        self._recorder: RunRecorder | None = None
        self._vision_enabled = False
        self._vision_stats = _VisionStats()
        self._pillow_missing_warned = False
        self._reset_vision(check_pillow=False)

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
        self._reset_vision()
        self._recorder = RunRecorder(self._run_store, goal, mode="ai")
        self._run_store.ensure()
        self._tracker.reset()
        logger.info("Goal: %s", goal)
        try:
            plan = self._make_plan(goal)
        except ModelError as exc:
            logger.error("Could not create a plan: %s", exc)
            self._last_run = RunCapture(goal, [], [], False)
            result = RunResult(RunStatus.FAILED, 0, f"Model error while planning: {exc}")
            self._finish_record(ctx=None, result=result)
            self._log_vision_summary()
            return result

        criteria = getattr(self, "_last_success_criteria", [])
        logger.info("[PLANNER] Generated %d initial steps.", len(plan))
        logger.info("[PLANNER] Defined %d success criteria.", len(criteria))
        for criterion in criteria:
            logger.info("[PLANNER] Success criterion: %s", criterion)
        logger.info("Plan:\n%s", "\n".join(f"  {i}. {step}" for i, step in enumerate(plan, 1)))
        ctx = _RunContext(goal=goal, plan=plan, success_criteria=criteria)
        result = self._loop(ctx)
        self._capture_run(ctx, result)
        self._finish_record(ctx=ctx, result=result)
        self._log_vision_summary()
        return result

    def run_shortcut(self, shortcut: Shortcut) -> RunResult:
        """Replay a shortcut, falling back to the AI from the first unsafe step."""
        self._reset_vision()
        self._recorder = RunRecorder(self._run_store, shortcut.goal, mode="shortcut")
        self._run_store.ensure()
        plan = list(shortcut.plan) or [shortcut.goal]
        ctx = _RunContext(
            goal=shortcut.goal,
            plan=plan,
            success_criteria=list(shortcut.success_criteria) or [shortcut.goal],
        )
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
            ctx.plan_index = min(len(ctx.plan) - 1, ctx.completed_steps)
            screen = self._observe(ctx)
            if isinstance(screen, RunResult):
                result = screen
            else:
                fresh_screen = screen or self._tracker.update(self._device.dump_ui())
                verified = self._verify_goal(ctx, fresh_screen, ctx.completed_steps)
                if isinstance(verified, RunResult):
                    result = verified
                elif verified is None:
                    result = self._loop(ctx)
                else:
                    result = RunResult(
                        RunStatus.STUCK,
                        ctx.completed_steps,
                        "Shortcut goal could not be verified.",
                    )
        elif outcome.aborted:
            result = RunResult(RunStatus.ABORTED, ctx.completed_steps, outcome.reason)
        else:
            ctx.plan_index = min(ctx.completed_steps, len(ctx.plan) - 1)
            self._reset_vision()
            ctx.screenshot_attempts = 0
            ctx.last_screenshot = None
            ctx.device_action_count = 0
            ctx.last_screenshot_action_count = -1
            result = self._loop(ctx)

        self._capture_run(ctx, result)
        handover_step = (
            len(outcome.replayed_steps) + 1
            if not outcome.completed and not outcome.aborted
            else None
        )
        self._finish_record(
            ctx=ctx,
            result=result,
            run_type={
                "kind": "shortcut",
                "handover_step": handover_step,
                "handover_reason": outcome.reason or None,
            },
        )
        self._log_vision_summary()
        return result

    def _reset_vision(self, *, check_pillow: bool = True) -> None:
        """Start fresh optional-vision state for a run or shortcut handover."""
        self._vision_stats = _VisionStats()
        if check_pillow:
            self._vision_enabled = self._settings.vision_enabled
        if check_pillow and self._vision_enabled and not pillow_available():
            if not self._pillow_missing_warned:
                logger.warning(
                    "Pillow is not installed; disabling optional screenshots for this run."
                )
                self._pillow_missing_warned = True
            self._vision_enabled = False
        elif check_pillow:
            self._pillow_missing_warned = False
        self._agent_prompt = build_agent_prompt(
            self._settings.stuck_threshold, vision=self._vision_enabled
        )

    def _log_vision_summary(self) -> None:
        if not self._settings.vision_enabled:
            return
        reasons = ", ".join(
            f"{reason}={count}" for reason, count in sorted(self._vision_stats.reasons.items())
        ) or "none"
        logger.info(
            "Vision summary: screenshots=%d (sent), reasons: %s, "
            "estimated extra tokens=%d, extra seconds=%.2f",
            self._vision_stats.screenshots_sent,
            reasons,
            self._vision_stats.estimated_extra_tokens,
            self._vision_stats.extra_seconds,
        )

    def _capture_action_step(
        self,
        ctx: _RunContext,
        action: Action,
        screen: ScreenState,
        number: int,
        ok: bool,
        detail: str,
        guards: dict[str, Any],
        screen_after: dict[str, Any] | None,
        started_perf: float,
    ) -> None:
        if self._recorder is None:
            return
        action_data = action.as_dict()
        action_data["reason"] = action.reason
        action_data["summary_line"] = action.summary_line()
        if action.name == "type":
            node = self._tracker.find(action.target)
            if node is not None and node.password:
                action_data.pop("text", None)
        if self._settings.hide_private_text:
            action_data.pop("text", None)
            action_data.pop("text", None)
            elements = []
            for element in self._tracker.element_snapshot():
                element["text"] = "[hidden]" if element.get("text") else ""
                element["description"] = "[hidden]" if element.get("description") else ""
                elements.append(element)
        else:
            elements = self._tracker.element_snapshot()
        step = {
            "number": number,
            "time": timestamp_now(),
            "duration_seconds": round(time.perf_counter() - started_perf, 3),
            "screen": {
                "mode": screen.mode,
                "full_tree": screen.mode == "full",
                "changed": screen.changed,
                "reason": screen.reason,
                "elements": elements,
            },
            "action": action_data,
            "result": {
                "ok": ok,
                "status": "OK" if ok else "FAILED",
                "error": None if ok else detail,
                "detail": detail,
            },
            "guards": guards,
        }
        if screen_after is not None:
            step["screen_after"] = screen_after
        previous_steps = self._recorder.record["steps"]
        if not ok and previous_steps:
            for earlier in reversed(previous_steps):
                if earlier.get("result", {}).get("ok"):
                    break
            else:
                pass
        if ok and previous_steps and not previous_steps[-1].get("result", {}).get("ok"):
            step["recovered"] = True
        self._recorder.add_step(step)

    def _capture_run(self, ctx: _RunContext, result: RunResult) -> None:
        self._last_run = RunCapture(
            goal=ctx.goal,
            plan=list(ctx.plan),
            steps=list(ctx.recorded_steps),
            completed=result.success,
            password_type_targeted=ctx.password_type_targeted,
            success_criteria=list(ctx.success_criteria),
        )

    def _complete_recorded_json(
        self,
        system_prompt: str,
        user_prompt: str,
        *,
        model: str,
        purpose: str = "action",
        images: list[bytes] | None = None,
    ) -> dict[str, Any]:
        started = time.perf_counter()
        try:
            payload = self._model.complete_json(
                system_prompt, user_prompt, model=model, images=images
            )
            return payload
        finally:
            usage = getattr(self._model, "last_usage", {}) or {}
            prompt_tokens = int(usage.get("input_tokens", 0) or 0)
            if not images and prompt_tokens:
                self._vision_stats.text_prompt_tokens.append(prompt_tokens)
            if self._recorder is not None:
                self._recorder.model_call(
                    model=model,
                    seconds=time.perf_counter() - started,
                    tokens_in=prompt_tokens,
                    tokens_out=int(usage.get("output_tokens", 0) or 0),
                )

    def _vision_can_capture(self, ctx: _RunContext) -> bool:
        return (
            self._vision_enabled
            and ctx.screenshot_attempts < self._settings.vision_max_per_run
        )

    def _capture_screenshot(
        self, ctx: _RunContext, reason: str, step: int
    ) -> Screenshot | None:
        """Capture and prepare one screenshot, respecting the task-local hard cap."""
        if not self._vision_can_capture(ctx):
            return None
        ctx.screenshot_attempts += 1
        ctx.pending_screenshot_reason = reason
        started = time.perf_counter()
        try:
            raw = self._device.screenshot()
            screenshot, unusable_reason = prepare_screenshot(
                raw,
                max_size=self._settings.vision_max_size,
                jpeg_quality=self._settings.vision_jpeg_quality,
            )
        except (DeviceError, OSError, ValueError) as exc:
            self._vision_stats.extra_seconds += time.perf_counter() - started
            logger.warning("Could not capture a usable screenshot (%s): %s", reason, exc)
            return None

        self._vision_stats.extra_seconds += time.perf_counter() - started
        if screenshot is None:
            logger.info("Screenshot skipped (%s, step %d): %s", reason, step, unusable_reason)
            return None

        if self._settings.vision_save_dir:
            try:
                save_screenshot(
                    screenshot,
                    self._settings.vision_save_dir,
                    step,
                    ctx.screenshot_attempts,
                )
            except OSError as exc:
                logger.warning("Could not save screenshot to VISION_SAVE_DIR: %s", exc)

        logger.info(
            "Screenshot (%s, step %d): sent %dx%d (%.1f KB), original %dx%d, "
            "scale %.3fx%.3f",
            reason,
            step,
            screenshot.width,
            screenshot.height,
            len(screenshot.bytes) / 1024,
            screenshot.original_width,
            screenshot.original_height,
            screenshot.scale_x,
            screenshot.scale_y,
        )
        return screenshot

    def _record_sent_screenshot(
        self, ctx: _RunContext, screenshot: Screenshot, call_seconds: float
    ) -> None:
        ctx.last_screenshot = screenshot
        ctx.last_screenshot_action_count = ctx.device_action_count
        self._vision_stats.screenshots_sent += 1
        if ctx.pending_screenshot_reason:
            self._vision_stats.reasons[ctx.pending_screenshot_reason] += 1
        ctx.pending_screenshot_reason = None
        self._vision_stats.extra_seconds += call_seconds
        usage = getattr(self._model, "last_usage", {}) or {}
        prompt_tokens = int(usage.get("input_tokens", 0) or 0)
        if prompt_tokens:
            if self._vision_stats.text_prompt_tokens:
                average = sum(self._vision_stats.text_prompt_tokens) / len(
                    self._vision_stats.text_prompt_tokens
                )
                estimate = max(0, round(prompt_tokens - average))
                self._vision_stats.estimated_extra_tokens += estimate
                estimate_text = str(estimate)
            else:
                estimate_text = "unavailable (no text-only usage baseline)"
            logger.info(
                "Vision request usage: prompt tokens=%d; ESTIMATE of image cost=%s extra tokens",
                prompt_tokens,
                estimate_text,
            )

    def _finish_record(
        self,
        *,
        ctx: _RunContext | None,
        result: RunResult,
        run_type: dict[str, Any] | None = None,
    ) -> None:
        if self._recorder is None:
            return
        should_summarize = (
            self._settings.summary_mode == "every"
            or (self._settings.summary_mode == "failed" and not result.success)
        )
        if run_type and run_type.get("kind") == "shortcut" and run_type.get("handover_step") is None:
            should_summarize = False
        summary: dict[str, Any] | None = None
        if should_summarize:
            summary = self._make_ai_summary(result)
        if summary is None:
            status_text = {
                "completed": "Phil completed the task.",
                "stuck": "Phil stopped because it was stuck.",
                "failed": "Phil stopped after an action or model problem.",
                "max_steps": "Phil reached the step limit.",
                "aborted": "The run was interrupted or stopped.",
            }.get(result.status.value, "Phil stopped before completing the task.")
            summary = {"text": status_text, "ai_written": False}

        why = self._why_recorded(ctx, result)
        last_action = (
            ctx.recorded_steps[-1].get("action", {}).get("summary_line")
            if ctx and ctx.recorded_steps
            else None
        )
        if last_action is None and ctx is not None:
            if ctx.recorded_steps:
                action = ctx.recorded_steps[-1].get("action", {})
                last_action = json.dumps(action, ensure_ascii=False)
            else:
                last_action = "No action was attempted."
        self._recorder.finish(
            status=result.status.value,
            message=result.message,
            steps=result.steps,
            password_type_targeted=ctx.password_type_targeted if ctx else False,
            run_type=run_type,
            summary=summary,
            why_stopped=why,
            last_action=last_action,
        )

    def _make_ai_summary(self, result: RunResult) -> dict[str, Any] | None:
        if self._recorder is None:
            return None
        record_json = json.dumps(self._recorder.record, ensure_ascii=False)
        prompt = (
            "Write a plain-English summary of this run in at most 3 sentences. "
            "Describe only facts in the record. Do not give advice or guesses. "
            "Use the record exactly for the final reason.\n" + record_json
        )
        model = self._settings.summary_model_name or self._settings.model_name
        started = time.perf_counter()
        try:
            if hasattr(self._model, "complete"):
                text = self._model.complete(
                    "Summarize the run factually in at most 3 sentences.",
                    prompt,
                    model=model,
                )
            else:
                payload = self._model.complete_json(
                    "Summarize the run factually in at most 3 sentences.",
                    prompt,
                    model=model,
                )
                text = str(payload.get("summary") or "")
            if not text.strip():
                raise RuntimeError("empty summary")
            sentences = [part.strip() for part in re.split(r"(?<=[.!?])\s+", text.strip()) if part.strip()]
            text = " ".join(sentences[:3])
            usage = getattr(self._model, "last_usage", {}) or {}
            self._recorder.model_call(
                model=model,
                seconds=time.perf_counter() - started,
                tokens_in=int(usage.get("input_tokens", 0) or 0),
                tokens_out=int(usage.get("output_tokens", 0) or 0),
            )
            return {"text": text, "ai_written": True}
        except Exception as exc:
            logger.warning("Could not create AI run summary; using fallback: %s", exc)
            return None

    def _why_recorded(self, ctx: _RunContext | None, result: RunResult) -> dict[str, Any]:
        if result.success:
            return {}
        last_action = "No action was attempted."
        if ctx and ctx.recorded_steps:
            action = ctx.recorded_steps[-1].get("action", {})
            last_action = action.get("summary_line") or json.dumps(action, ensure_ascii=False)
        message = result.message
        if result.status is RunStatus.STUCK:
            if "Repeated '" in message:
                sentence = f"The repeat guard stopped Phil: {message}"
            else:
                sentence = f"The model said it was stuck: {message}"
        elif result.status is RunStatus.MAX_STEPS:
            sentence = f"The step limit was reached: {message}"
        elif result.status is RunStatus.ABORTED:
            if "risky" in message.lower():
                sentence = f"You declined a risky action. Phil attempted: {last_action}. "
                sentence += "It was flagged as risky."
            elif "interrupt" in message.lower():
                sentence = "The user interrupted the run."
            else:
                sentence = message
        elif "Could not read the screen" in message:
            sentence = f"Phil could not read the screen: {message}"
        elif "model" in message.lower() or "key" in message.lower() or "api" in message.lower():
            sentence = f"A model or API problem stopped the run: {message}"
        elif "repeated failures" in message.lower() or "action failed" in message.lower():
            sentence = f"Too many failures happened in a row: {message}"
        else:
            sentence = message
        return {"sentence": sentence, "last_action": last_action}

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
        self._last_success_criteria: list[str] = []
        try:
            payload = self._complete_recorded_json(
                self._planner_prompt,
                build_planner_user_prompt(goal),
                model=self._settings.planner_model,
                purpose="plan",
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
        raw_criteria = payload.get("success_criteria")
        if isinstance(raw_criteria, list):
            self._last_success_criteria = [
                str(item).strip()
                for item in raw_criteria
                if isinstance(item, str) and item.strip()
            ]
        if not self._last_success_criteria:
            logger.warning(
                "Planner returned no reliable success criteria; "
                "verification will remain conservative."
            )
        return steps[:self._settings.max_plan_steps]

    def _replan(self, ctx: _RunContext, screen: ScreenState, step: int, reason: str) -> RunResult | None:
        """Revise the remaining plan without changing the original goal or success criteria."""
        if ctx.replan_attempts >= self._settings.max_replan_attempts:
            logger.warning(
                "[PLANNER] Replan limit reached (%d/%d).",
                ctx.replan_attempts,
                self._settings.max_replan_attempts,
            )
            return RunResult(
                RunStatus.STUCK,
                step,
                f"Cannot continue reliably after {ctx.replan_attempts} replanning attempts. {reason}",
            )

        ctx.replan_attempts += 1
        logger.info(
            "[PLANNER] Revising remaining plan (replan %d/%d): %s",
            ctx.replan_attempts,
            self._settings.max_replan_attempts,
            reason,
        )
        try:
            payload = self._complete_recorded_json(
                build_replan_prompt(),
                build_replan_user_prompt(
                    ctx.goal,
                    ctx.success_criteria,
                    ctx.plan,
                    ctx.plan_index,
                    ctx.history[-HISTORY_WINDOW:],
                    self._tracker.full_tree_text(),
                    reason,
                ),
                model=self._settings.planner_model,
                purpose="replan",
            )
        except ModelError as exc:
            logger.error("[PLANNER] Replanning failed: %s", exc)
            if exc.fatal:
                return RunResult(RunStatus.FAILED, step, f"Model error while replanning: {exc}")
            return RunResult(
                RunStatus.STUCK,
                step,
                f"Could not revise the plan safely: {exc}",
            )

        raw_plan = payload.get("plan")
        if not isinstance(raw_plan, list):
            return RunResult(RunStatus.STUCK, step, "Replanner returned a malformed plan.")
        replacement = [
            str(item).strip()
            for item in raw_plan
            if isinstance(item, str) and item.strip()
        ][:self._settings.max_plan_steps]
        if not replacement:
            return RunResult(RunStatus.STUCK, step, "Replanner returned no usable remaining steps.")

        completed = ctx.plan[:ctx.plan_index]
        ctx.plan = completed + replacement
        ctx.plan_index = len(completed)
        if ctx.plan_index >= len(ctx.plan):
            ctx.plan_index = max(0, len(ctx.plan) - 1)
        logger.info("[PLANNER] Added/revised %d remaining steps.", len(replacement))
        logger.info(
            "[PLANNER] Replans used %d, remaining %d.",
            ctx.replan_attempts,
            max(0, self._settings.max_replan_attempts - ctx.replan_attempts),
        )
        return None

    def _verify_goal(
        self, ctx: _RunContext, screen: ScreenState, step: int
    ) -> RunResult | None:
        """Independently verify the immutable original goal against fresh UI evidence."""
        logger.info("[VERIFIER] Checking the original user goal.")
        ctx.verification_attempts += 1
        if ctx.verification_attempts > self._settings.max_verification_attempts:
            logger.warning(
                "[VERIFIER] Verification limit reached (%d).",
                ctx.verification_attempts - 1,
            )
            return RunResult(
                RunStatus.STUCK,
                step,
                "Goal could not be verified after the configured verification attempts.",
            )

        try:
            fresh = self._observe(ctx)
        except Exception:
            fresh = None
        if isinstance(fresh, RunResult):
            return fresh
        if fresh is not None:
            screen = fresh

        screenshot = self._capture_screenshot(ctx, "verify_done", step) if self._vision_enabled else None
        try:
            payload = self._complete_recorded_json(
                build_verification_prompt(),
                build_verification_user_prompt(
                    ctx.goal,
                    ctx.success_criteria,
                    ctx.history[-HISTORY_WINDOW:],
                    self._tracker.full_tree_text(),
                ),
                model=self._settings.agent_model,
                purpose="verify",
                images=[screenshot.bytes] if screenshot else None,
            )
        except ModelError as exc:
            logger.error("[VERIFIER] Verification model call failed: %s", exc)
            if exc.fatal:
                return RunResult(RunStatus.FAILED, step, f"Model error during verification: {exc}")
            return RunResult(RunStatus.STUCK, step, f"Goal verification failed: {exc}")

        status = str(payload.get("status", "")).strip().lower()
        evidence = payload.get("evidence")
        remaining = payload.get("remaining_requirements")
        evidence_list = [
            str(item).strip()
            for item in evidence
            if isinstance(item, str) and item.strip()
        ] if isinstance(evidence, list) else []
        remaining_list = [
            str(item).strip()
            for item in remaining
            if isinstance(item, str) and item.strip()
        ] if isinstance(remaining, list) else []
        reason = str(payload.get("reason") or "").strip()

        logger.info("[VERIFIER] Status: %s.", status.upper() or "UNCERTAIN")
        for item in evidence_list:
            logger.info("[VERIFIER] Evidence: %s", item)
        for item in remaining_list:
            logger.info("[VERIFIER] Missing requirement: %s", item)

        if status == "satisfied" and evidence_list and not remaining_list:
            logger.info("[PHIL] Goal completed successfully.")
            return RunResult(RunStatus.COMPLETED, step, reason or "Verified: the original goal was achieved.")
        if status == "incomplete":
            missing = "; ".join(remaining_list) or reason or "The original goal remains incomplete."
            replan_result = self._replan(ctx, screen, step, missing)
            if replan_result is not None:
                return replan_result
            ctx.history.append(f"{len(ctx.history) + 1}. verification incomplete -> replanned: {missing}")
            return None
        if status == "uncertain":
            ctx.history.append(
                f"{len(ctx.history) + 1}. verification uncertain -> "
                f"{reason or 'insufficient observable evidence'}"
            )
        else:
            logger.warning("[VERIFIER] Malformed verification response.")
        if ctx.verification_attempts >= self._settings.max_verification_attempts:
            return RunResult(
                RunStatus.STUCK,
                step,
                "Goal remains unverified: observable evidence was insufficient.",
            )
        logger.info("[VERIFIER] Attempting recovery or another observation.")
        return None

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

            action = self._decide(ctx, screen, step)
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

    def _decide(
        self, ctx: _RunContext, screen: ScreenState, step: int
    ) -> Action | RunResult | None:
        """Ask the model for the next action. Returns None to retry, or a RunResult to stop."""
        screenshot: Screenshot | None = None
        reason: str | None = None
        if self._vision_can_capture(ctx):
            if screen.element_count < self._settings.vision_min_elements:
                reason = "empty_tree"
            elif (
                ctx.errors >= self._settings.vision_failure_streak
                and ctx.errors > ctx.error_vision_checkpoint
            ):
                reason = "failure_streak"
                ctx.error_vision_checkpoint = ctx.errors
            elif (
                ctx.repeats >= self._settings.vision_failure_streak
                and ctx.repeats > ctx.repeat_vision_checkpoint
            ):
                reason = "failure_streak"
                ctx.repeat_vision_checkpoint = ctx.repeats
        if reason is not None:
            screenshot = self._capture_screenshot(ctx, reason, step)

        just_confirmed = ctx.just_confirmed
        ctx.just_confirmed = False
        while True:
            user_prompt = build_agent_user_prompt(
                ctx.goal,
                ctx.plan,
                ctx.plan_index,
                ctx.history[-HISTORY_WINDOW:],
                self._tracker.full_tree_text() if screenshot else screen.prompt_text,
                just_confirmed=just_confirmed,
                success_criteria=ctx.success_criteria,
            )
            just_confirmed = False
            started = time.perf_counter()
            image_call = screenshot is not None
            try:
                payload = self._complete_recorded_json(
                    self._agent_prompt,
                    user_prompt,
                    model=self._settings.vision_model if image_call else self._settings.agent_model,
                    images=[screenshot.bytes] if screenshot else None,
                )
            except ModelImageUnsupportedError:
                self._vision_enabled = False
                self._agent_prompt = build_agent_prompt(self._settings.stuck_threshold)
                logger.warning(
                    "The API rejected screenshot input; disabling vision for the rest of this run."
                )
                screenshot = None
                continue
            except ModelError as exc:
                if exc.fatal:
                    logger.error("Model error: %s", exc)
                    return RunResult(RunStatus.FAILED, 0, f"Model error: {exc}")
                return self._fail_or_retry(ctx, "The model call failed", exc)
            if image_call:
                self._record_sent_screenshot(ctx, screenshot, time.perf_counter() - started)
                screenshot = None
            try:
                action = Action.from_payload(payload)
            except ActionError as exc:
                ctx.history.append(f"{len(ctx.history) + 1}. invalid reply -> {exc}")
                self._capture_action_step(
                    ctx,
                    Action(name="invalid"),
                    screen,
                    len(ctx.recorded_steps) + ctx.errors + 1,
                    False,
                    str(exc),
                    {"invalid_action": True},
                    None,
                    started,
                )
                return self._fail_or_retry(ctx, "The model returned an invalid action", exc)

            if action.name != "take_screenshot":
                return action

            screenshot = self._capture_screenshot(
                ctx, "model_request", step
            )
            if screenshot is None:
                if not self._vision_enabled:
                    note = "screenshots are disabled"
                elif ctx.screenshot_attempts >= self._settings.vision_max_per_run:
                    note = "screenshot limit reached"
                else:
                    note = "no usable screenshot was available"
                ctx.history.append(f"{len(ctx.history) + 1}. {note}.")
                return None
            ctx.history.append(f"{len(ctx.history) + 1}. screenshot supplied for this step.")

    def _handle(
        self, ctx: _RunContext, action: Action, screen: ScreenState, step: int
    ) -> RunResult | None:
        """Carry out one decided action. Returns a RunResult when the run should stop."""
        step_started = time.perf_counter()
        if action.name == "take_screenshot":
            note = (
                "screenshots are disabled"
                if not self._vision_enabled
                else "screenshot limit reached"
            )
            ctx.history.append(f"{len(ctx.history) + 1}. {note}.")
            return None

        if action.name == "done":
            message = action.summary or "Goal reached."
            logger.info("[EXECUTOR] Done action received; starting independent verification.")
            self._capture_action_step(
                ctx, action, screen, step, True, message, {}, None, step_started
            )
            return self._verify_goal(ctx, screen, step)

        if action.name == "replan":
            reason = (
                action.reason
                or action.summary
                or "The execution agent detected that the plan needs revision."
            )
            logger.info("[EXECUTOR] Unexpected state detected; replanning requested.")
            self._capture_action_step(
                ctx, action, screen, step, True, reason, {"replan": True}, None, step_started
            )
            return self._replan(ctx, screen, step, reason)

        if action.name == "stuck":
            message = action.reason or action.summary or "The model reported it is stuck."
            logger.warning("Stuck: %s", message)
            self._capture_action_step(
                ctx, action, screen, step, False, message, {"model_stuck": True}, None, step_started
            )
            return RunResult(RunStatus.STUCK, step, message)

        if action.name == "confirm":
            return self._handle_confirm(ctx, action, step)

        if (
            action.name == "click"
            and ctx.unchanged_click_signature is not None
            and action.signature == ctx.unchanged_click_signature
        ):
            ctx.repeats += 1
            ctx.history.append(
                f"{len(ctx.history) + 1}. {action.signature} -> "
                "Already clicked; the UI tree remains unchanged. Inspect the resulting state "
                "without assuming success."
            )
            logger.info("[EXECUTOR] Unchanged click remains ambiguous; action not advanced.")
            self._capture_action_step(
                ctx,
                action,
                screen,
                step,
                False,
                "Repeated unchanged click skipped.",
                {"unchanged_click": True, "duplicate_tap_skipped": True},
                None,
                step_started,
            )
            if ctx.repeats >= self._settings.stuck_threshold:
                return RunResult(
                    RunStatus.STUCK,
                    step,
                    "The same click remains unchanged and the resulting state could not be established.",
                )
            return None

        if self._is_repeating(ctx, action, screen):
            if (
                self._vision_can_capture(ctx)
                and ctx.repeats >= self._settings.vision_failure_streak
            ):
                return None
            message = (
                f"Repeated '{action.summary_line()}' more than {self._settings.stuck_threshold} "
                "times without the screen changing."
            )
            logger.warning("Stuck: %s", message)
            self._capture_action_step(
                ctx,
                action,
                screen,
                step,
                False,
                message,
                {"repeat_guard": True, "stuck_counter": ctx.repeats},
                None,
                step_started,
            )
            return RunResult(RunStatus.STUCK, step, message)

        risky_confirmed = False
        reason = self._risk_reason(ctx, action)
        if reason is not None:
            assert self._confirm is not None  # guaranteed by __init__ when confirm_risky is set
            if not self._confirm(f"{reason}\n  Phil wants to run: {action.summary_line()}"):
                logger.warning("Action declined by the user.")
                self._capture_action_step(
                    ctx,
                    action,
                    screen,
                    step,
                    False,
                    "User declined a risky action.",
                    {"risky_declined": True},
                    None,
                    step_started,
                )
                return RunResult(RunStatus.ABORTED, step, "You declined a risky action.")
            risky_confirmed = True

        if action.name == "type":
            node = self._tracker.find(action.target)
            if node is not None and node.password:
                ctx.password_type_targeted = True
        captured_step = record_step(action, self._tracker)
        result = self._executor.execute(action, screenshot=ctx.last_screenshot)
        ctx.device_action_count += 1
        level = logging.INFO if result.ok else logging.WARNING
        logger.log(level, "Result: %s", result.detail)
        verdict = "OK" if result.ok else "FAILED"
        ctx.history.append(
            f"{len(ctx.history) + 1}. {action.signature} -> {verdict}: {result.detail}"
        )

        guards: dict[str, Any] = {"risky_confirmed": risky_confirmed}

        if result.ok:
            ctx.recorded_steps.append(captured_step)
            ctx.completed_steps += 1
            ctx.errors = 0
            if action.name == "click":
                post_click = self._observe_after_click(ctx, action)
                if post_click is not None and not post_click.changed:
                    self._capture_action_step(
                        ctx, action, screen, step, True, result.detail, guards,
                        {"changed": False, "mode": post_click.mode},
                        step_started,
                    )
                    ctx.unchanged_click_signature = action.signature
                    ctx.repeats = max(ctx.repeats, 1)
                    ctx.history.append(
                        f"{len(ctx.history) + 1}. "
                        "Already clicked; the UI tree doesn't show this change. "
                        "Treat the result as ambiguous and inspect the current state."
                    )
                    logger.warning(
                        "[EXECUTOR] Click produced no visible UI change; state is ambiguous."
                    )
                    if action.plan_step_done:
                        logger.info(
                            "[EXECUTOR] Plan step done ignored after unchanged click; no success assumed."
                        )
                    return None

                ctx.unchanged_click_signature = None
                self._capture_action_step(
                    ctx, action, screen, step, True, result.detail, guards,
                    None if post_click is None else {"changed": post_click.changed, "mode": post_click.mode},
                    step_started,
                )
            else:
                self._capture_action_step(
                    ctx, action, screen, step, True, result.detail, guards,
                    None, step_started,
                )
            if action.name != "click":
                ctx.unchanged_click_signature = None

            if action.plan_step_done:
                return self._advance_plan(ctx, step)
        else:
            self._capture_action_step(
                ctx, action, screen, step, False, result.detail, guards,
                None, step_started,
            )
            failure = self._fail_or_retry(ctx, "The action failed", RuntimeError(result.detail))
            if failure is not None:
                if (
                    self._vision_can_capture(ctx)
                    and ctx.errors >= self._settings.vision_failure_streak
                ):
                    return None
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
        """Address the current plan step; verify when it was the final step."""
        ctx.plan_index += 1
        if ctx.plan_index >= len(ctx.plan):
            logger.info("[EXECUTOR] Remaining plan completed.")
            logger.info("[VERIFIER] Plan exhaustion reached; verifying the original goal.")
            try:
                fresh = self._observe(ctx)
            except Exception:
                fresh = None
            if isinstance(fresh, RunResult):
                return fresh
            if fresh is None:
                fresh = self._tracker.update(self._device.dump_ui())
            return self._verify_goal(ctx, fresh, step)
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

    def _verify_done(
        self, ctx: _RunContext, screen: ScreenState, step: int
    ) -> Action | RunResult | None:
        """Ask once, with a screenshot, whether a proposed completion is actually visible."""
        ctx.done_verification_attempted = True
        screenshot = self._capture_screenshot(ctx, "verify_done", step)
        if screenshot is None:
            return None
        ctx.history.append(f"{len(ctx.history) + 1}. verifying the reported completion.")
        prompt = build_agent_user_prompt(
            ctx.goal,
            ctx.plan,
            ctx.plan_index,
            ctx.history[-HISTORY_WINDOW:],
            self._tracker.full_tree_text(),
        ) + (
            "\nThe model just reported done. Inspect the screenshot and full UI tree. "
            "Return done only if the goal is visibly complete; otherwise return the next "
            "safe action. This is the one completion verification for this run."
        )
        image = screenshot
        while True:
            started = time.perf_counter()
            image_call = image is not None
            try:
                payload = self._complete_recorded_json(
                    self._agent_prompt,
                    prompt if image_call else build_agent_user_prompt(
                        ctx.goal,
                        ctx.plan,
                        ctx.plan_index,
                        ctx.history[-HISTORY_WINDOW:],
                        screen.prompt_text,
                    ) + "\nThe model just reported done. Is the goal visibly complete? "
                    "Return done only if it is; otherwise return the next safe action.",
                    model=self._settings.vision_model if image_call else self._settings.agent_model,
                    images=[image.bytes] if image else None,
                )
            except ModelImageUnsupportedError:
                self._vision_enabled = False
                self._agent_prompt = build_agent_prompt(self._settings.stuck_threshold)
                logger.warning(
                    "The API rejected screenshot input; disabling vision for the rest of this run."
                )
                image = None
                continue
            except ModelError as exc:
                logger.error("Could not verify the reported completion: %s", exc)
                failure = self._fail_or_retry(ctx, "Completion verification failed", exc)
                return failure or RunResult(
                    RunStatus.FAILED, step, f"Could not verify completion: {exc}"
                )

            if image_call:
                self._record_sent_screenshot(ctx, image, time.perf_counter() - started)
            try:
                return Action.from_payload(payload)
            except ActionError as exc:
                logger.warning("Completion verification returned an invalid action: %s", exc)
                failure = self._fail_or_retry(ctx, "Completion verification failed", exc)
                return failure or RunResult(
                    RunStatus.FAILED, step, f"Could not verify completion: {exc}"
                )

    def _is_repeating(self, ctx: _RunContext, action: Action, screen: ScreenState) -> bool:
        """True once the same action has been proposed too often with no change on screen."""
        if action.name in CONTROL_ACTIONS:
            return False
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
        if action.name == "tap_xy":
            plan_text = (
                ctx.plan[ctx.plan_index]
                if 0 <= ctx.plan_index < len(ctx.plan)
                else ""
            )
            match = _RISKY_PATTERN.search(f"{action.reason} {plan_text}".lower())
            if match is not None:
                return f"This may be a '{match.group(1)}' action and could be hard to undo."
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
