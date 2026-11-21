# SPDX-License-Identifier: MIT
# Phil - Phone Human Interaction Layer
# Copyright (c) 2026 David Aluu
# Licensed under the MIT License. See the LICENSE file in the project root.
"""Phil command-line interface.

Usage::

    python main.py                       # interactive: type a task, Phil does it
    python main.py "turn on dark mode"   # one-shot
    python main.py --list-devices        # show connected phones
    python main.py --list-shortcuts      # show saved shortcuts
    python main.py --run "dark mode"     # replay a saved shortcut
    python main.py --delete "dark mode"  # delete a saved shortcut
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
from phil.shortcuts import Shortcut, ShortcutError, ShortcutStore

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
        help="Do not ask for confirmation before risky actions or shortcut deletion.",
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
    shortcut_commands = parser.add_mutually_exclusive_group()
    shortcut_commands.add_argument(
        "--list-shortcuts", action="store_true", help="List saved shortcuts and exit."
    )
    shortcut_commands.add_argument(
        "--run", dest="run_shortcut", metavar="NAME", help="Replay a saved shortcut."
    )
    shortcut_commands.add_argument(
        "--delete", dest="delete_shortcut", metavar="NAME",
        help="Delete a saved shortcut (asks for confirmation unless --yes is used).",
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
    interval = 1.2 / (2 * len(tagline) + len("Phil"))
    console = Console(file=sys.stdout)
    try:
        sys.stdout.write("\r\x1b[2K\x1b[?25l")
        sys.stdout.flush()
        for character in tagline:
            console.print(character, end="")
            sys.stdout.flush()
            time.sleep(interval)
        time.sleep(0.25)
        for _ in tagline:
            sys.stdout.write("\b \b")
            sys.stdout.flush()
            time.sleep(interval)
        sys.stdout.write("\r\x1b[2K")
        sys.stdout.flush()
        for character in "Phil":
            console.print(character, style="bold blue", end="")
            sys.stdout.flush()
            time.sleep(interval)
    except KeyboardInterrupt:
        sys.stdout.write("\r\x1b[2K")
        sys.stdout.flush()
        print()
        return False
    finally:
        sys.stdout.write("\x1b[?25h")
        sys.stdout.flush()

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


def _run_task(
    agent: Agent, task: str, *, shortcut: Shortcut | None = None
) -> RunResult:
    """Run one task, turning Ctrl+C and unexpected errors into a result instead of a crash."""
    started = time.perf_counter()
    steps = 0
    success = False
    try:
        try:
            result = agent.run_shortcut(shortcut) if shortcut else agent.run(task)
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


def _show_shortcuts(store: ShortcutStore) -> None:
    shortcuts = store.list_shortcuts()
    if not shortcuts:
        print("No shortcuts saved.")
        return
    print("Saved shortcuts:")
    for number, shortcut in enumerate(shortcuts, 1):
        print(f"  {number}. {shortcut.name}")


def _confirm_delete() -> bool:
    """Ask the exact yes/no question used for shortcut deletion."""
    try:
        answer = input("Delete this shortcut? [y/N] ")
    except (EOFError, KeyboardInterrupt):
        print()
        return False
    return answer.strip().lower() in {"y", "yes"}


def _delete_shortcut(
    store: ShortcutStore,
    reference: str,
    *,
    skip_confirmation: bool = False,
) -> bool:
    """Confirm and delete a shortcut, returning whether it was removed."""
    try:
        shortcut = store.load(reference)
    except ShortcutError as exc:
        print(f"Shortcut not found: {exc}. Use 'list' to see saved shortcuts.")
        return False

    step_count = len(shortcut.steps)
    step_label = "step" if step_count == 1 else "steps"
    print(f"Shortcut: {shortcut.name} ({step_count} {step_label})")
    if not skip_confirmation and not _confirm_delete():
        print("Shortcut was not deleted.")
        return False

    try:
        store.delete(shortcut.name)
    except ShortcutError as exc:
        print(f"Could not delete shortcut: {exc}")
        return False
    print(f"Deleted shortcut {shortcut.name!r}.")
    _show_shortcuts(store)
    return True


def _save_shortcut(agent: Agent, store: ShortcutStore, name: str) -> None:
    run = agent.last_run
    if run is None:
        print("Cannot save: there is no last run.")
        return
    if not run.completed:
        print("Cannot save: the last run did not end completed.")
        return
    if run.password_type_targeted:
        print("Cannot save: the run typed into a password field.")
        return
    try:
        overwrite = False
        if store.exists(name):
            if not _ask_confirmation(f"Shortcut {name!r} already exists. Overwrite it?"):
                print("Shortcut was not overwritten.")
                return
            overwrite = True
        store.save(name, run, overwrite=overwrite)
    except ShortcutError as exc:
        print(f"Cannot save shortcut: {exc}")
        return
    print(f"Saved shortcut {name!r}. Typed text is stored in plain text in the shortcut file.")


def _interactive(agent: Agent, store: ShortcutStore) -> int:
    print(
        "Type a task and press Enter. Use 'save <name>', 'list', 'run <name|number>', "
        "or 'delete <name|number>'. "
        "Type 'exit' to quit. Ctrl+C stops a running task.\n"
    )
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
        command, _, argument = task.partition(" ")
        argument = argument.strip()
        if command.lower() == "list" and not argument:
            _show_shortcuts(store)
            continue
        if command.lower() == "save":
            if argument:
                _save_shortcut(agent, store, argument)
            else:
                print("Usage: save <name>")
            continue
        if command.lower() == "run":
            if not argument:
                print("Usage: run <name|number>")
                continue
            try:
                shortcut = store.load(argument)
            except ShortcutError as exc:
                print(f"Shortcut error: {exc}")
                continue
            _run_task(agent, shortcut.goal, shortcut=shortcut)
            continue
        if command.lower() == "delete":
            if argument:
                _delete_shortcut(store, argument)
            else:
                print("Usage: delete <name|number>")
            continue
        _run_task(agent, task)


def main(argv: Sequence[str] | None = None) -> int:
    """Program entry point. Returns a process exit code."""
    parser = build_parser()
    args = parser.parse_args(argv)
    if (args.run_shortcut or args.delete_shortcut) and args.task:
        parser.error("Use either a task or a shortcut command, not both.")

    try:
        settings = load_settings(
            require_api_key=not (
                args.list_devices
                or args.list_shortcuts
                or args.run_shortcut
                or args.delete_shortcut
            )
        )
    except ConfigError as exc:
        print(f"Configuration error: {exc}", file=sys.stderr)
        return EXIT_CONFIG
    settings = _apply_overrides(settings, args)
    setup_logging(settings.log_level, settings.log_file, secrets=[settings.api_key])

    if args.list_devices:
        return _show_devices(settings)
    store = ShortcutStore(settings.shortcuts_dir)
    if args.list_shortcuts:
        _show_shortcuts(store)
        return EXIT_OK
    if args.delete_shortcut:
        _delete_shortcut(store, args.delete_shortcut, skip_confirmation=args.yes)
        return EXIT_OK

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
    if args.run_shortcut:
        try:
            shortcut = store.load(args.run_shortcut)
        except ShortcutError as exc:
            logger.error("%s", exc)
            return EXIT_TASK_FAILED
        result = _run_task(agent, shortcut.goal, shortcut=shortcut)
        return EXIT_OK if result.success else EXIT_TASK_FAILED
    return _interactive(agent, store)


if __name__ == "__main__":
    sys.exit(main())
