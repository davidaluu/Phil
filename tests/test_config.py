# SPDX-License-Identifier: MIT
# Copyright (c) 2026 David Aluu
import pytest

from phil import config
from phil.config import ConfigError, load_settings

_VARS = [
    "API_KEY", "API_BASE_URL", "MODEL_NAME", "PLANNER_MODEL_NAME", "AGENT_MODEL_NAME",
    "JSON_MODE", "TEMPERATURE", "MAX_STEPS", "CONFIRM_RISKY", "LOG_LEVEL",
    "IGNORED_PACKAGES", "STABILIZE_DELAY", "SHORTCUTS_DIR", "SHORTCUT_OVERLAP",
    "OVERLAP_THRESHOLD", "STUCK_THRESHOLD",
    "VISION_ENABLED", "VISION_MODEL_NAME", "VISION_MAX_SIZE", "VISION_JPEG_QUALITY",
    "VISION_MIN_ELEMENTS", "VISION_FAILURE_STREAK", "VISION_MAX_PER_RUN",
    "VISION_VERIFY_DONE", "VISION_SAVE_DIR",
]


@pytest.fixture(autouse=True)
def clean_env(monkeypatch):
    monkeypatch.setattr(config, "load_dotenv", lambda *a, **k: False)
    for name in _VARS:
        monkeypatch.delenv(name, raising=False)


def test_missing_or_placeholder_key_is_rejected(monkeypatch):
    with pytest.raises(ConfigError):
        load_settings()
    monkeypatch.setenv("API_KEY", "your-key-here")
    with pytest.raises(ConfigError):
        load_settings()


def test_defaults_and_model_fallbacks(monkeypatch):
    monkeypatch.setenv("API_KEY", "sk-real-key")
    monkeypatch.setenv("MODEL_NAME", "m")
    settings = load_settings()
    assert settings.planner_model == settings.agent_model == "m"
    assert settings.confirm_risky is True and settings.max_steps == 30
    assert settings.shortcuts_dir == "shortcuts" and settings.shortcut_overlap == 0.75
    assert settings.vision_enabled is False
    assert settings.vision_max_size == 1024 and settings.vision_jpeg_quality == 70
    assert settings.vision_min_elements == 3 and settings.vision_failure_streak == 2
    assert settings.vision_max_per_run == 3 and settings.vision_verify_done is False
    assert settings.vision_save_dir is None
    monkeypatch.setenv("AGENT_MODEL_NAME", "big")
    assert load_settings().agent_model == "big"
    assert load_settings().vision_model == "big"
    monkeypatch.setenv("VISION_MODEL_NAME", "vision")
    assert load_settings().vision_model == "vision"


@pytest.mark.parametrize(
    ("name", "value"),
    [
        ("VISION_MAX_SIZE", "255"),
        ("VISION_MAX_SIZE", "2049"),
        ("VISION_JPEG_QUALITY", "29"),
        ("VISION_JPEG_QUALITY", "96"),
        ("VISION_FAILURE_STREAK", "0"),
        ("VISION_MAX_PER_RUN", "21"),
    ],
)
def test_vision_ranges_are_validated(monkeypatch, name, value):
    monkeypatch.setenv("API_KEY", "sk-real-key")
    monkeypatch.setenv(name, value)
    with pytest.raises(ConfigError):
        load_settings()


def test_parsing_and_validation(monkeypatch):
    monkeypatch.setenv("API_KEY", "sk-real-key")
    monkeypatch.setenv("CONFIRM_RISKY", "off")
    monkeypatch.setenv("TEMPERATURE", "0.3")
    monkeypatch.setenv("IGNORED_PACKAGES", "none")
    settings = load_settings()
    assert settings.confirm_risky is False
    assert settings.temperature == 0.3
    assert settings.ignored_packages == ()
    monkeypatch.setenv("MAX_STEPS", "zero")
    with pytest.raises(ConfigError):
        load_settings()
    monkeypatch.setenv("MAX_STEPS", "10")
    monkeypatch.setenv("SHORTCUTS_DIR", "my-shortcuts")
    monkeypatch.setenv("SHORTCUT_OVERLAP", "0.8")
    settings = load_settings()
    assert settings.shortcuts_dir == "my-shortcuts" and settings.shortcut_overlap == 0.8
    monkeypatch.setenv("SHORTCUT_OVERLAP", "1.1")
    with pytest.raises(ConfigError):
        load_settings()
    monkeypatch.setenv("SHORTCUT_OVERLAP", "nan")
    with pytest.raises(ConfigError):
        load_settings()
    monkeypatch.setenv("SHORTCUT_OVERLAP", "0.75")
    monkeypatch.setenv("LOG_LEVEL", "LOUD")
    with pytest.raises(ConfigError):
        load_settings()
