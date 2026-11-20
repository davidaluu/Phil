# SPDX-License-Identifier: MIT
# Copyright (c) 2026 David Aluu
from types import SimpleNamespace

import pytest

from phil.config import Settings
from phil.model import ModelClient, ModelResponseError, parse_json_object


def test_parse_plain_fenced_and_chatty_json():
    assert parse_json_object('{"a": 1}') == {"a": 1}
    assert parse_json_object('```json\n{"a": 1}\n```') == {"a": 1}
    assert parse_json_object('Sure! Here you go: {"a": {"b": "}"}} hope that helps') == {"a": {"b": "}"}}


@pytest.mark.parametrize("raw", ["", "no json here", "[1, 2]", '{"a": '])
def test_parse_rejects_non_objects(raw):
    with pytest.raises(ModelResponseError):
        parse_json_object(raw)


def make_client(content, settings=None, calls=None):
    response = SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content=content))])

    def create(**kwargs):
        if calls is not None:
            calls.append(kwargs)
        return response

    fake = SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=create)))
    return ModelClient(settings or Settings(api_key="k"), client=fake)


def test_complete_json_round_trip_and_request_shape():
    calls = []
    client = make_client('{"plan": ["x"]}', calls=calls)
    assert client.complete_json("sys", "user", model="m1") == {"plan": ["x"]}
    request = calls[0]
    assert request["model"] == "m1"
    assert request["response_format"] == {"type": "json_object"}
    assert "temperature" not in request


def test_json_mode_and_temperature_are_configurable():
    calls = []
    client = make_client("{}", Settings(api_key="k", json_mode=False, temperature=0.2), calls)
    client.complete("s", "u")
    assert "response_format" not in calls[0]
    assert calls[0]["temperature"] == 0.2


def test_empty_reply_raises():
    with pytest.raises(ModelResponseError):
        make_client("   ").complete("s", "u")
