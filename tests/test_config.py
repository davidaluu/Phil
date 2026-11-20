# SPDX-License-Identifier: MIT
# Copyright (c) 2026 David Aluu
import pytest

from phil import config
from phil.config import ConfigError, load_settings

_VARS = [
    "API_KEY", "API_BASE_URL", "MODEL_NAME", "PLANNER_MODEL_NAME", "AGENT_MODEL_NAME",
    "JSON_MODE", "TEMPERATURE", "MAX_STEPS", "CONFIRM_RISKY", "LOG_LEVEL",
    "IGNORED_PACKAGES", "STABILIZE_DELAY", "OVERLAP_THRESHOLD", "STUCK_THRESHOLD",
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
    monkeypatch.setenv("AGENT_MODEL_NAME", "big")
    assert load_settings().agent_model == "big"


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
    monkeypatch.setenv("LOG_LEVEL", "LOUD")
    with pytest.raises(ConfigError):
        load_settings()
