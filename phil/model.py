# SPDX-License-Identifier: MIT
# Phil - Phone Human Interaction Layer
# Copyright (c) 2026 David Aluu
# Licensed under the MIT License. See the LICENSE file in the project root.
"""The model call. Works with any OpenAI-compatible API; address, key and model come from settings."""

from __future__ import annotations

import json
import logging
from typing import Any

import openai
from openai import OpenAI

from phil.config import Settings

logger = logging.getLogger(__name__)

_DEBUG_REPLY_CHARS = 1_000


class ModelError(RuntimeError):
    """A failed model call. ``fatal`` errors will not fix themselves, so retrying is pointless."""

    def __init__(self, message: str, *, fatal: bool = False) -> None:
        super().__init__(message)
        self.fatal = fatal


class ModelResponseError(ModelError):
    """The model answered, but the answer was empty or not usable JSON."""


def _extract_json_object(text: str) -> str | None:
    """Return the first balanced ``{...}`` in ``text``, ignoring braces inside strings."""
    start = text.find("{")
    if start == -1:
        return None
    depth = 0
    in_string = False
    escaped = False
    for index in range(start, len(text)):
        char = text[index]
        if in_string:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == '"':
                in_string = False
            continue
        if char == '"':
            in_string = True
        elif char == "{":
            depth += 1
        elif char == "}":
            depth -= 1
            if depth == 0:
                return text[start : index + 1]
    return None


def parse_json_object(raw: str) -> dict[str, Any]:
    """Decode a JSON object from model output, tolerating fences and surrounding prose.

    Raises:
        ModelResponseError: if no JSON object can be decoded.
    """
    text = raw.strip()
    candidates = [text]
    extracted = _extract_json_object(text)
    if extracted is not None and extracted != text:
        candidates.append(extracted)
    for candidate in candidates:
        try:
            value = json.loads(candidate)
        except json.JSONDecodeError:
            continue
        if isinstance(value, dict):
            return value
    raise ModelResponseError("The model did not return a valid JSON object.")


class ModelClient:
    """Thin wrapper around an OpenAI-compatible chat completions endpoint."""

    def __init__(self, settings: Settings, client: Any | None = None) -> None:
        self._settings = settings
        self._client = client or OpenAI(
            api_key=settings.api_key,
            base_url=settings.api_base_url,
            timeout=settings.request_timeout,
            max_retries=settings.max_retries,
        )

    def complete(self, system_prompt: str, user_prompt: str, *, model: str | None = None) -> str:
        """Send one chat request and return the reply text.

        Raises:
            ModelError: on any API failure or empty reply.
        """
        model_name = model or self._settings.model_name
        request: dict[str, Any] = {
            "model": model_name,
            "messages": [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt},
            ],
        }
        if self._settings.json_mode:
            request["response_format"] = {"type": "json_object"}
        if self._settings.temperature is not None:
            request["temperature"] = self._settings.temperature

        logger.debug("Calling model %s", model_name)
        try:
            response = self._client.chat.completions.create(**request)
        except openai.OpenAIError as exc:
            raise self._translate(exc, model_name) from exc

        choices = getattr(response, "choices", None)
        content = choices[0].message.content if choices else None
        if not content or not content.strip():
            raise ModelResponseError("The model returned an empty reply.")
        logger.debug("Model reply: %s", content[:_DEBUG_REPLY_CHARS])
        return content

    def complete_json(
        self, system_prompt: str, user_prompt: str, *, model: str | None = None
    ) -> dict[str, Any]:
        """Like ``complete`` but decodes the reply as a JSON object."""
        return parse_json_object(self.complete(system_prompt, user_prompt, model=model))

    def _translate(self, exc: openai.OpenAIError, model_name: str) -> ModelError:
        base_url = self._settings.api_base_url
        if isinstance(exc, (openai.AuthenticationError, openai.PermissionDeniedError)):
            return ModelError(
                "The API rejected your key. Check API_KEY in .env (and that it matches "
                "the provider at API_BASE_URL).",
                fatal=True,
            )
        if isinstance(exc, openai.NotFoundError):
            return ModelError(
                f"Model or endpoint not found. Check MODEL_NAME ('{model_name}') and "
                f"API_BASE_URL ('{base_url}').",
                fatal=True,
            )
        if isinstance(exc, openai.BadRequestError):
            return ModelError(
                f"The API rejected the request: {exc}. If your provider does not support "
                "JSON mode, set JSON_MODE=false in .env.",
                fatal=True,
            )
        if isinstance(exc, openai.RateLimitError):
            return ModelError("The API rate limit was hit. Wait a moment or slow down.")
        if isinstance(exc, openai.APITimeoutError):
            return ModelError(
                f"The API did not answer in {self._settings.request_timeout:.0f}s. "
                "Try raising REQUEST_TIMEOUT in .env."
            )
        if isinstance(exc, openai.APIConnectionError):
            return ModelError(f"Could not reach the API at {base_url}. Check your internet connection.")
        if isinstance(exc, openai.APIStatusError):
            return ModelError(
                f"The API returned an error (HTTP {exc.status_code}): {exc.message}",
                fatal=exc.status_code < 500,
            )
        return ModelError(f"Model request failed: {exc}")
