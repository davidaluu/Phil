# SPDX-License-Identifier: MIT
# Phil - Phone Human Interaction Layer
# Copyright (c) 2026 David Aluu
# Licensed under the MIT License. See the LICENSE file in the project root.
"""Phone access layer.

Phil reads the screen and performs gestures through one ``Device`` object.
Underneath, it prefers uiautomator2 (fast, supports any text) and silently
degrades to plain ``adb`` commands if uiautomator2 is missing or fails. The
two backends are never used at the same moment: the backend is chosen at
connect time and only switches (once, permanently) after a failure.
"""

from __future__ import annotations

import logging
import re
import shlex
import subprocess
import time
from collections.abc import Callable, Sequence
from typing import Any

logger = logging.getLogger(__name__)

KEYCODES: dict[str, int] = {"home": 3, "back": 4, "enter": 66, "recents": 187}
STATUSBAR_COMMANDS = frozenset({"expand-notifications", "expand-settings", "collapse"})

_KEYCODE_DEL = 67
_KEYCODE_MOVE_END = 123
_CLEAR_KEY_PRESSES = 60
_PACKAGE_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_]*(\.[A-Za-z][A-Za-z0-9_]*)+$")
_COMMAND_TIMEOUT = 30.0
_DUMP_TIMEOUT = 25.0
_DUMP_ATTEMPTS = 3
_DUMP_RETRY_DELAY = 0.5


class DeviceError(RuntimeError):
    """Raised when the phone cannot be reached or a command fails."""


def _loggable(args: Sequence[str]) -> str:
    """Return an ADB argument list as text, hiding anything the user typed."""
    joined = " ".join(args)
    if "input text" in joined:
        return "shell input text <redacted>"
    return joined


def _run_adb(adb_path: str, args: Sequence[str], timeout: float) -> str:
    """Run one adb command and return stdout, raising ``DeviceError`` on any failure."""
    logger.debug("adb %s", _loggable(args))
    try:
        completed = subprocess.run(
            [adb_path, *args],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
            check=False,
        )
    except FileNotFoundError as exc:
        raise DeviceError(
            f"ADB executable '{adb_path}' was not found. Install Android platform-tools "
            "and make sure 'adb' is on your PATH (or set ADB_PATH in .env)."
        ) from exc
    except subprocess.TimeoutExpired as exc:
        raise DeviceError(
            f"ADB command timed out after {timeout:.0f}s: adb {_loggable(args)}"
        ) from exc
    if completed.returncode != 0:
        detail = (completed.stderr or completed.stdout).strip()
        raise DeviceError(f"ADB command failed (adb {_loggable(args)}): {detail}")
    return completed.stdout


def list_devices(adb_path: str = "adb", timeout: float = 10.0) -> list[tuple[str, str]]:
    """Return ``(serial, state)`` pairs reported by ``adb devices``."""
    output = _run_adb(adb_path, ["devices"], timeout)
    devices: list[tuple[str, str]] = []
    for line in output.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith(("List of devices", "*")):
            continue
        parts = stripped.split()
        if len(parts) >= 2:
            devices.append((parts[0], parts[1]))
    return devices


class Device:
    """A connected Android phone."""

    def __init__(
        self,
        serial: str | None = None,
        *,
        adb_path: str = "adb",
        prefer_uiautomator2: bool = True,
    ) -> None:
        self._requested_serial = serial
        self._serial: str | None = None
        self._adb_path = adb_path
        self._prefer_u2 = prefer_uiautomator2
        self._u2: Any | None = None

    # ------------------------------------------------------------------ setup

    @property
    def serial(self) -> str | None:
        """Serial of the connected phone (None before ``connect``)."""
        return self._serial

    @property
    def backend(self) -> str:
        """Name of the backend currently in use."""
        return "uiautomator2" if self._u2 is not None else "adb"

    def connect(self) -> None:
        """Pick a phone and set up the best available backend.

        Raises:
            DeviceError: if ADB is missing or no usable phone is found.
        """
        self._serial = self._select_serial()
        logger.info("Using device %s", self._serial)
        if self._prefer_u2:
            self._connect_uiautomator2()
        else:
            logger.info("uiautomator2 is disabled; using plain ADB (slower screen reads).")
        logger.info("Screen backend: %s", self.backend)

    def _select_serial(self) -> str:
        devices = list_devices(self._adb_path)
        if self._requested_serial:
            for serial, state in devices:
                if serial == self._requested_serial:
                    if state != "device":
                        raise DeviceError(
                            f"Device {serial} is '{state}'. Unlock the phone and accept the "
                            "USB debugging prompt, then try again."
                        )
                    return serial
            raise DeviceError(
                f"Device {self._requested_serial} was not found. "
                "Run 'python main.py --list-devices' to see what is connected."
            )

        ready = [serial for serial, state in devices if state == "device"]
        if len(ready) == 1:
            return ready[0]
        if not ready:
            hint = ""
            if devices:
                states = ", ".join(f"{s} ({st})" for s, st in devices)
                hint = f" ADB sees: {states}. 'unauthorized' means you must accept the prompt on the phone."
            raise DeviceError(
                "No phone detected. Connect it with USB debugging enabled, then check "
                f"'adb devices'.{hint}"
            )
        raise DeviceError(
            f"Several phones are connected ({', '.join(ready)}). "
            "Choose one with --serial or DEVICE_SERIAL in .env."
        )

    def _connect_uiautomator2(self) -> None:
        try:
            import uiautomator2 as u2
        except ImportError:
            logger.warning(
                "uiautomator2 is not installed (pip install -r requirements.txt); "
                "falling back to plain ADB, which is slower."
            )
            return
        try:
            handle = u2.connect(self._serial)
            _ = handle.info  # forces the on-phone helper to start; raises if unreachable
        except Exception as exc:  # noqa: BLE001 - uiautomator2 raises many unrelated types
            logger.warning(
                "Could not start uiautomator2 (%s); falling back to plain ADB, which is slower.",
                exc,
            )
            return
        self._u2 = handle

    def _via_u2(self, label: str, call: Callable[[Any], Any]) -> tuple[bool, Any]:
        """Run ``call`` on uiautomator2 if it is active; disable it for good on failure."""
        if self._u2 is None:
            return False, None
        try:
            return True, call(self._u2)
        except Exception as exc:  # noqa: BLE001 - uiautomator2 raises many unrelated types
            logger.warning(
                "uiautomator2 %s failed (%s); switching to plain ADB for this session.",
                label,
                exc,
            )
            self._u2 = None
            return False, None

    def _adb(self, *args: str, timeout: float = _COMMAND_TIMEOUT) -> str:
        prefix = ["-s", self._serial] if self._serial else []
        return _run_adb(self._adb_path, [*prefix, *args], timeout)

    # ---------------------------------------------------------------- sensing

    def dump_ui(self) -> str:
        """Return the current accessibility hierarchy as XML.

        Raises:
            DeviceError: if the screen cannot be read.
        """
        ok, xml = self._via_u2("dump", lambda d: d.dump_hierarchy())
        if ok and isinstance(xml, str) and xml.strip():
            return xml
        return self._dump_via_adb()

    def _dump_via_adb(self) -> str:
        last_error = "no output"
        for attempt in range(1, _DUMP_ATTEMPTS + 1):
            try:
                output = self._adb(
                    "exec-out", "uiautomator", "dump", "/dev/tty", timeout=_DUMP_TIMEOUT
                )
            except DeviceError as exc:
                last_error = str(exc)
            else:
                start = output.find("<?xml")
                if start == -1:
                    start = output.find("<hierarchy")
                end = output.rfind("</hierarchy>")
                if start != -1 and end != -1:
                    return output[start : end + len("</hierarchy>")]
                last_error = output.strip()[:200] or "empty output"
            if attempt < _DUMP_ATTEMPTS:
                time.sleep(_DUMP_RETRY_DELAY)
        raise DeviceError(
            f"Could not read the screen with 'uiautomator dump' after {_DUMP_ATTEMPTS} "
            f"attempts: {last_error}"
        )

    # -------------------------------------------------------------- actuating

    def tap(self, x: int, y: int) -> None:
        """Tap at pixel coordinates."""
        x, y = int(x), int(y)
        ok, _ = self._via_u2("tap", lambda d: d.click(x, y))
        if not ok:
            self._adb("shell", f"input tap {x} {y}")

    def long_press(self, x: int, y: int, duration_ms: int = 800) -> None:
        """Press and hold at pixel coordinates."""
        x, y = int(x), int(y)
        ok, _ = self._via_u2(
            "long press", lambda d: d.long_click(x, y, duration=duration_ms / 1000)
        )
        if not ok:
            self._adb("shell", f"input swipe {x} {y} {x} {y} {int(duration_ms)}")

    def swipe(self, x1: int, y1: int, x2: int, y2: int, duration_ms: int = 300) -> None:
        """Swipe between two pixel coordinates."""
        x1, y1, x2, y2 = int(x1), int(y1), int(x2), int(y2)
        ok, _ = self._via_u2(
            "swipe", lambda d: d.swipe(x1, y1, x2, y2, duration=duration_ms / 1000)
        )
        if not ok:
            self._adb("shell", f"input swipe {x1} {y1} {x2} {y2} {int(duration_ms)}")

    def press(self, key: str) -> None:
        """Press a hardware/navigation key: home, back, enter or recents."""
        code = KEYCODES.get(key)
        if code is None:
            raise DeviceError(f"Unsupported key '{key}'. Supported: {', '.join(KEYCODES)}.")
        ok, _ = self._via_u2(f"press {key}", lambda d: d.press(code))
        if not ok:
            self._adb("shell", f"input keyevent {code}")

    def type_text(self, text: str, clear: bool = True) -> None:
        """Type into the focused field, replacing its content when ``clear`` is true.

        Plain-ADB typing supports ASCII without newlines only; uiautomator2 supports any text.
        """
        ok, _ = self._via_u2("type", lambda d: d.send_keys(text, clear=clear))
        if ok:
            return
        if not text.isascii() or "\n" in text or "\r" in text:
            raise DeviceError(
                "The plain-ADB fallback can only type ASCII text without line breaks. "
                "Install uiautomator2 (pip install -r requirements.txt) for full text support."
            )
        if clear:
            presses = " ".join([str(_KEYCODE_DEL)] * _CLEAR_KEY_PRESSES)
            self._adb("shell", f"input keyevent {_KEYCODE_MOVE_END} {presses}")
        escaped = text.replace(" ", "%s")
        self._adb("shell", f"input text {shlex.quote(escaped)}")

    def launch_app(self, package: str) -> None:
        """Start an app by package name (e.g. ``com.android.settings``)."""
        if not _PACKAGE_RE.match(package):
            raise DeviceError(f"'{package}' is not a valid Android package name.")
        ok, _ = self._via_u2("launch app", lambda d: d.app_start(package))
        if ok:
            return
        output = self._adb(
            "shell", f"monkey -p {package} -c android.intent.category.LAUNCHER 1"
        )
        if "No activities found" in output or "monkey aborted" in output:
            raise DeviceError(f"Could not launch '{package}': the app is not installed.")

    def statusbar(self, command: str) -> None:
        """Open or close the notification shade / quick settings."""
        if command not in STATUSBAR_COMMANDS:
            raise DeviceError(f"Unsupported status bar command '{command}'.")
        self._adb("shell", f"cmd statusbar {command}")
