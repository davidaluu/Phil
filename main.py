# SPDX-License-Identifier: MIT
# Phil - Phone Human Interaction Layer
# Copyright (c) 2026 David Aluu
# Licensed under the MIT License. See the LICENSE file in the project root.
"""Phil command-line interface.

Usage::

    python main.py                       # interactive: type a task, Phil does it
    python main.py "turn on dark mode"   # one-shot
    python main.py --list-devices        # show connected phones
"""

from __future__ import annotations

import argparse
import dataclasses
import logging
import sys
import time
from collections.abc import Sequence

from rich.console import Console

from phil import __author__, __version__
from phil.adb import Device, DeviceError, list_devices
from phil.agent import Agent, RunResult, RunStatus
from phil.config import ConfigError, Settings, load_settings
from phil.log import setup_logging
from phil.model import ModelClient

EXIT_OK = 0
EXIT_TASK_FAILED = 1
EXIT_CONFIG = 2
EXIT_DEVICE = 3
EXIT_INTERRUPTED = 130

_EXIT_WORDS = frozenset({"exit", "quit", "q", ":q"})

logger = logging.getLogger("phil.main")


def _positive_int(raw: str) -> int:
    try:
        value = int(raw)
    except ValueError:
        raise argparse.ArgumentTypeError(f"{raw!r} is not a whole number.") from None
    if value < 1:
        raise argparse.ArgumentTypeError("must be 1 or more.")
    return value


def build_parser() -> argparse.ArgumentParser:
    """Create the command-line parser."""
    parser = argparse.ArgumentParser(
        prog="phil",
        description="Phil (Phone Human Interaction Layer): an AI agent that operates your Android phone.",
        epilog="Settings such as the API key live in a .env file. See README.md.",
    )
    parser.add_argument(
        "task",
        nargs="*",
        help="What Phil should do. Leave empty to start interactive mode.",
    )
    parser.add_argument("-s", "--serial", help="Phone serial (overrides DEVICE_SERIAL).")
    parser.add_argument(
        "--max-steps", type=_positive_int, help="Maximum steps per task (overrides MAX_STEPS)."
    )
    parser.add_argument(
        "-y",
        "--yes",
        action="store_true",
        help="Do not ask for confirmation before risky actions.",
    )
    parser.add_argument(
        "--log-level",
        type=str.upper,
        choices=("DEBUG", "INFO", "WARNING", "ERROR"),
        help="Terminal log detail (overrides LOG_LEVEL).",
    )
    parser.add_argument(
        "--list-devices", action="store_true", help="List connected phones and exit."
    )
    parser.add_argument("--no-banner", action="store_true", help="Skip the startup banner.")
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    return parser


def _apply_overrides(settings: Settings, args: argparse.Namespace) -> Settings:
    """Apply command-line flags on top of the loaded settings."""
    overrides: dict[str, object] = {}
    if args.serial:
        overrides["device_serial"] = args.serial
    if args.max_steps:
        overrides["max_steps"] = args.max_steps
    if args.yes:
        overrides["confirm_risky"] = False
    if args.log_level:
        overrides["log_level"] = args.log_level
    return dataclasses.replace(settings, **overrides) if overrides else settings


def _print_banner() -> bool:
    tagline = "Phone Human Interaction Layer"
    if not sys.stdout.isatty():
        print("Phil")
        return True
    interval = 3.0 / (2 * (len("Phil") + len(tagline)) + len("Phil"))
    try:
        console = Console(file=sys.stdout)
        for text in ("Phil", tagline):
            for character in text:
                sys.stdout.write(character)
                sys.stdout.flush()
                time.sleep(interval)
            for _ in text:
                sys.stdout.write("\b \b")
                sys.stdout.flush()
                time.sleep(interval)
        for character in "Phil":
            console.print(character, style="bold blue", end="")
            sys.stdout.flush()
            time.sleep(interval)
    except KeyboardInterrupt:
        print()
        return False

    print(f" - {tagline}  v{__version__}")
    print(f"Copyright (c) 2026 {__author__}  |  MIT License\n")
    return True


def _ask_confirmation(message: str) -> bool:
    """Ask the user a yes/no question in the terminal. Anything but yes means no."""
    print(f"\n! {message}")
    try:
        answer = input("  Allow this? [y/N] ")
    except (EOFError, KeyboardInterrupt):
        print()
        return False
    return answer.strip().lower() in {"y", "yes"}


def _show_devices(settings: Settings) -> int:
    try:
        devices = list_devices(settings.adb_path)
    except DeviceError as exc:
        logger.error("%s", exc)
        return EXIT_DEVICE
    if not devices:
        print("No devices found. Enable USB debugging and connect your phone.")
        return EXIT_OK
    print("Connected devices:")
    for serial, state in devices:
        print(f"  {serial}\t{state}")
    return EXIT_OK


def _run_task(agent: Agent, task: str) -> RunResult:
    """Run one task, turning Ctrl+C and unexpected errors into a result instead of a crash."""
    started = time.perf_counter()
    steps = 0
    success = False
    try:
        try:
            result = agent.run(task)
        except KeyboardInterrupt:
            logger.warning("Interrupted by user.")
            result = RunResult(RunStatus.ABORTED, 0, "Interrupted by user.")
        except Exception as exc:  # noqa: BLE001 - last-resort boundary so interactive mode survives
            logger.exception("Unexpected error while running the task")
            result = RunResult(RunStatus.FAILED, 0, f"Unexpected error: {exc}")
        steps = result.steps
        success = result.success
        print(f"\n[{result.status.value}] {result.message}  ({result.steps} steps)\n")
        return result
    finally:
        status = "✓ Done in" if success else "Stopped after"
        print(f"{status} {time.perf_counter() - started:.1f} seconds ({steps} steps)")


def _interactive(agent: Agent) -> int:
    print("Type a task and press Enter. Type 'exit' to quit. Ctrl+C stops a running task.\n")
    while True:
        try:
            task = input("Task> ").strip()
        except (EOFError, KeyboardInterrupt):
            print()
            return EXIT_OK
        if not task:
            continue
        if task.lower() in _EXIT_WORDS:
            return EXIT_OK
        _run_task(agent, task)


def main(argv: Sequence[str] | None = None) -> int:
    """Program entry point. Returns a process exit code."""
    args = build_parser().parse_args(argv)

    try:
        settings = load_settings(require_api_key=not args.list_devices)
    except ConfigError as exc:
        print(f"Configuration error: {exc}", file=sys.stderr)
        return EXIT_CONFIG
    settings = _apply_overrides(settings, args)
    setup_logging(settings.log_level, settings.log_file, secrets=[settings.api_key])

    if args.list_devices:
        return _show_devices(settings)

    if not args.no_banner and not _print_banner():
        return EXIT_INTERRUPTED

    device = Device(
        settings.device_serial,
        adb_path=settings.adb_path,
        prefer_uiautomator2=settings.use_uiautomator2,
    )
    try:
        device.connect()
    except DeviceError as exc:
        logger.error("%s", exc)
        return EXIT_DEVICE

    agent = Agent(
        device,
        ModelClient(settings),
        settings,
        confirm=_ask_confirmation if settings.confirm_risky else None,
    )

    if args.task:
        result = _run_task(agent, " ".join(args.task))
        if result.status is RunStatus.ABORTED and result.message.startswith("Interrupted"):
            return EXIT_INTERRUPTED
        return EXIT_OK if result.success else EXIT_TASK_FAILED
    return _interactive(agent)


if __name__ == "__main__":
    sys.exit(main())
