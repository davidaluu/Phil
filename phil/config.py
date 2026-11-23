# SPDX-License-Identifier: MIT
# Phil - Phone Human Interaction Layer
# Copyright (c) 2026 David Aluu
# Licensed under the MIT License. See the LICENSE file in the project root.
"""Configuration: loads ``.env`` / environment variables into a typed object."""

from __future__ import annotations

import math
import os
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import TypeVar

from dotenv import load_dotenv

PROJECT_ROOT = Path(__file__).resolve().parent.parent

_VALID_LOG_LEVELS = ("DEBUG", "INFO", "WARNING", "ERROR")
_PLACEHOLDER_KEYS = frozenset({"", "your-key-here", "your-api-key-here", "changeme"})
_DEFAULT_IGNORED_PACKAGES = ("com.android.systemui",)

Number = TypeVar("Number", int, float)


class ConfigError(ValueError):
    """Raised when the configuration is missing or invalid."""


@dataclass(frozen=True)
class Settings:
    """All runtime settings. Only ``api_key`` is required to construct one."""

    api_key: str
    api_base_url: str = "https://api.openai.com/v1"
    model_name: str = "gpt-4o-mini"
    planner_model_name: str | None = None
    agent_model_name: str | None = None
    json_mode: bool = True
    temperature: float | None = None
    request_timeout: float = 60.0
    max_retries: int = 2
    device_serial: str | None = None
    adb_path: str = "adb"
    use_uiautomator2: bool = True
    ignored_packages: tuple[str, ...] = _DEFAULT_IGNORED_PACKAGES
    log_level: str = "INFO"
    log_file: str | None = None
    max_steps: int = 30
    stabilize_delay: float = 1.0
    shortcuts_dir: str = "shortcuts"
    shortcut_overlap: float = 0.75
    confirm_risky: bool = True
    always_send_full_tree: bool = False
    overlap_threshold: float = 0.4
    stuck_threshold: int = 4
    records_dir: str = "records"
    reports_dir: str = "reports"
    summary_mode: str = "failed"
    summary_model_name: str | None = None
    hide_private_text: bool = False
    retention_days: int = 7
    time_format: str = "24"
    report_output_dir: str | None = None
    vision_enabled: bool = False
    vision_model_name: str | None = None
    vision_max_size: int = 1024
    vision_jpeg_quality: int = 70
    vision_min_elements: int = 3
    vision_failure_streak: int = 2
    vision_max_per_run: int = 3
    vision_verify_done: bool = False
    vision_save_dir: str | None = None

    @property
    def planner_model(self) -> str:
        """Model used to turn a goal into a plan."""
        return self.planner_model_name or self.model_name

    @property
    def agent_model(self) -> str:
        """Model used to pick each next action."""
        return self.agent_model_name or self.model_name

    @property
    def vision_model(self) -> str:
        """Model used for image requests, falling back through the action and default models."""
        return self.vision_model_name or self.agent_model or self.model_name


def _read(name: str) -> str | None:
    """Return a stripped environment variable, or None if unset or blank."""
    value = os.environ.get(name)
    if value is None:
        return None
    value = value.strip()
    return value or None


def _read_bool(name: str, default: bool) -> bool:
    raw = _read(name)
    if raw is None:
        return default
    lowered = raw.lower()
    if lowered in {"1", "true", "yes", "on"}:
        return True
    if lowered in {"0", "false", "no", "off"}:
        return False
    raise ConfigError(f"{name} must be true or false (got {raw!r}).")


def _read_number(
    name: str,
    default: Number,
    cast: Callable[[str], Number],
    *,
    minimum: Number | None = None,
    maximum: Number | None = None,
) -> Number:
    raw = _read(name)
    if raw is None:
        return default
    try:
        value = cast(raw)
    except ValueError:
        raise ConfigError(f"{name} must be a number (got {raw!r}).") from None
    if isinstance(value, float) and not math.isfinite(value):
        raise ConfigError(f"{name} must be a finite number (got {raw!r}).")
    if minimum is not None and value < minimum:
        raise ConfigError(f"{name} must be at least {minimum} (got {value}).")
    if maximum is not None and value > maximum:
        raise ConfigError(f"{name} must be at most {maximum} (got {value}).")
    return value


def _read_packages(name: str, default: tuple[str, ...]) -> tuple[str, ...]:
    raw = _read(name)
    if raw is None:
        return default
    if raw.lower() == "none":
        return ()
    return tuple(part.strip() for part in raw.split(",") if part.strip())


def load_settings(*, require_api_key: bool = True) -> Settings:
    """Load settings from ``.env`` (project root, then current directory) and the environment.

    Real environment variables win over ``.env`` values.

    Raises:
        ConfigError: if a value is missing or invalid.
    """
    load_dotenv(PROJECT_ROOT / ".env")
    load_dotenv()

    api_key = _read("API_KEY") or ""
    if require_api_key and api_key.lower() in _PLACEHOLDER_KEYS:
        raise ConfigError(
            "API_KEY is missing. Copy .env.example to .env and put your real key in it."
        )

    log_level = (_read("LOG_LEVEL") or "INFO").upper()
    if log_level not in _VALID_LOG_LEVELS:
        raise ConfigError(f"LOG_LEVEL must be one of {', '.join(_VALID_LOG_LEVELS)}.")

    temperature_raw = _read("TEMPERATURE")
    temperature: float | None = None
    if temperature_raw is not None:
        temperature = _read_number("TEMPERATURE", 0.0, float, minimum=0.0, maximum=2.0)

    summary_mode = (_read("RUN_SUMMARY") or "failed").lower()
    if summary_mode not in {"failed", "every", "never"}:
        raise ConfigError("RUN_SUMMARY must be failed, every, or never.")
    time_format = (_read("TIME_FORMAT") or "24").lower()
    if time_format not in {"24", "12"}:
        raise ConfigError("TIME_FORMAT must be 24 or 12.")

    return Settings(
        api_key=api_key,
        api_base_url=_read("API_BASE_URL") or Settings.api_base_url,
        model_name=_read("MODEL_NAME") or Settings.model_name,
        planner_model_name=_read("PLANNER_MODEL_NAME"),
        agent_model_name=_read("AGENT_MODEL_NAME"),
        json_mode=_read_bool("JSON_MODE", True),
        temperature=temperature,
        request_timeout=_read_number("REQUEST_TIMEOUT", 60.0, float, minimum=1.0),
        max_retries=_read_number("MAX_RETRIES", 2, int, minimum=0, maximum=10),
        device_serial=_read("DEVICE_SERIAL"),
        adb_path=_read("ADB_PATH") or "adb",
        use_uiautomator2=_read_bool("USE_UIAUTOMATOR2", True),
        ignored_packages=_read_packages("IGNORED_PACKAGES", _DEFAULT_IGNORED_PACKAGES),
        log_level=log_level,
        log_file=_read("LOG_FILE"),
        max_steps=_read_number("MAX_STEPS", 30, int, minimum=1, maximum=500),
        stabilize_delay=_read_number("STABILIZE_DELAY", 1.0, float, minimum=0.0, maximum=10.0),
        shortcuts_dir=_read("SHORTCUTS_DIR") or "shortcuts",
        shortcut_overlap=_read_number(
            "SHORTCUT_OVERLAP", 0.75, float, minimum=0.0, maximum=1.0
        ),
        confirm_risky=_read_bool("CONFIRM_RISKY", True),
        always_send_full_tree=_read_bool("ALWAYS_SEND_FULL_TREE", False),
        overlap_threshold=_read_number("OVERLAP_THRESHOLD", 0.4, float, minimum=0.0, maximum=1.0),
        stuck_threshold=_read_number("STUCK_THRESHOLD", 4, int, minimum=1, maximum=50),
        records_dir=_read("RECORDS_DIR") or "records",
        reports_dir=_read("REPORTS_DIR") or "reports",
        summary_mode=summary_mode,
        summary_model_name=_read("SUMMARY_MODEL_NAME"),
        hide_private_text=_read_bool("HIDE_PRIVATE_TEXT", False),
        retention_days=_read_number("RETENTION_DAYS", 7, int, minimum=1, maximum=3650),
        time_format=time_format,
        report_output_dir=_read("REPORT_OUTPUT_DIR"),
        vision_enabled=_read_bool("VISION_ENABLED", False),
        vision_model_name=_read("VISION_MODEL_NAME"),
        vision_max_size=_read_number("VISION_MAX_SIZE", 1024, int, minimum=256, maximum=2048),
        vision_jpeg_quality=_read_number(
            "VISION_JPEG_QUALITY", 70, int, minimum=30, maximum=95
        ),
        vision_min_elements=_read_number(
            "VISION_MIN_ELEMENTS", 3, int, minimum=0, maximum=1000
        ),
        vision_failure_streak=_read_number(
            "VISION_FAILURE_STREAK", 2, int, minimum=1, maximum=50
        ),
        vision_max_per_run=_read_number(
            "VISION_MAX_PER_RUN", 3, int, minimum=0, maximum=20
        ),
        vision_verify_done=_read_bool("VISION_VERIFY_DONE", False),
        vision_save_dir=_read("VISION_SAVE_DIR"),
    )
