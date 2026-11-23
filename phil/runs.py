# SPDX-License-Identifier: MIT
# Copyright (c) 2026 David Aluu
"""Best-effort JSON run recording and local run cleanup."""

from __future__ import annotations

import json
import logging
import tempfile
import time
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterable

logger = logging.getLogger(__name__)

RUN_FORMAT_VERSION = 1


def timestamp_now() -> str:
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")


def parse_timestamp(value: str) -> datetime:
    parsed = datetime.fromisoformat(value)
    if parsed.tzinfo is None:
        raise ValueError("Run timestamps must include a UTC offset.")
    return parsed


class RunRecordError(ValueError):
    pass


class RunStore:
    def __init__(self, directory: str | Path = "records") -> None:
        self.directory = Path(directory)

    def ensure(self) -> None:
        try:
            self.directory.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            logger.warning("Could not create run record folder: %s", exc)

    def save(self, record: dict[str, Any]) -> Path | None:
        self.ensure()
        path = self.directory / f"{record['run_id']}.json"
        try:
            with tempfile.NamedTemporaryFile(
                mode="w", encoding="utf-8", dir=self.directory,
                prefix=f".{record['run_id']}.", suffix=".tmp", delete=False,
            ) as file:
                json.dump(record, file, ensure_ascii=False, indent=2)
                file.write("\n")
                temporary = Path(file.name)
            temporary.replace(path)
            return path
        except (OSError, KeyError, TypeError, ValueError) as exc:
            logger.warning("Could not record run; continuing without changing the task result: %s", exc)
            return None

    def load(self, path: str | Path) -> dict[str, Any]:
        source = Path(path)
        try:
            raw = json.loads(source.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise RunRecordError(f"Could not read run record {source.name}: {exc}") from exc
        if not isinstance(raw, dict):
            raise RunRecordError(f"Run record {source.name} must contain a JSON object.")
        if raw.get("format_version") != RUN_FORMAT_VERSION:
            raise RunRecordError(
                f"Run record {source.name} has a missing or unknown format version "
                f"(got {raw.get('format_version')!r}; expected {RUN_FORMAT_VERSION})."
            )
        return raw

    def list_records(self) -> list[dict[str, Any]]:
        if not self.directory.exists():
            return []
        records: list[dict[str, Any]] = []
        for path in sorted(self.directory.glob("*.json"), reverse=True):
            try:
                records.append(self.load(path))
            except RunRecordError as exc:
                logger.warning("%s", exc)
        return records

    def cleanup(self, retention_days: int) -> int:
        cutoff = datetime.now(timezone.utc) - timedelta(days=retention_days)
        deleted = 0
        for path in self.directory.glob("*.json"):
            try:
                record = self.load(path)
                if record.get("keep", False):
                    continue
                end = parse_timestamp(record["end_time"])
                if end.astimezone(timezone.utc) < cutoff:
                    path.unlink()
                    deleted += 1
            except (RunRecordError, KeyError, ValueError, OSError):
                continue
        return deleted

    def keep(self, run_id: str, keep: bool = True) -> bool:
        for path in self.directory.glob("*.json"):
            try:
                record = self.load(path)
            except RunRecordError:
                continue
            if record.get("run_id") == run_id:
                record["keep"] = keep
                return self.save(record) is not None
        return False


class RunRecorder:
    """State helper that builds one complete run record."""

    def __init__(self, store: RunStore, goal: str, *, mode: str = "ai") -> None:
        self.store = store
        self.run_id = f"{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S')}-{uuid.uuid4().hex[:8]}"
        self.started = timestamp_now()
        self._started_perf = time.perf_counter()
        self.record: dict[str, Any] = {
            "format_version": RUN_FORMAT_VERSION,
            "run_id": self.run_id,
            "task": goal,
            "start_time": self.started,
            "end_time": None,
            "status": "failed",
            "status_message": "",
            "total_steps": 0,
            "total_seconds": 0.0,
            "total_tokens": 0,
            "run_type": {"kind": "ai" if mode == "ai" else "shortcut", "handover_step": None},
            "keep": False,
            "model_calls": [],
            "steps": [],
            "summary": None,
            "why_stopped": None,
            "last_action": None,
            "password_type_targeted": False,
        }

    def model_call(
        self,
        *,
        model: str,
        seconds: float,
        tokens_in: int = 0,
        tokens_out: int = 0,
        call_time: str | None = None,
    ) -> None:
        self.record["model_calls"].append({
            "time": call_time or timestamp_now(),
            "model": model,
            "tokens_in": tokens_in,
            "tokens_out": tokens_out,
            "seconds": round(seconds, 3),
        })
        self.record["total_tokens"] = sum(
            call["tokens_in"] + call["tokens_out"] for call in self.record["model_calls"]
        )

    def add_step(self, step: dict[str, Any]) -> None:
        self.record["steps"].append(step)

    def finish(
        self,
        *,
        status: str,
        message: str,
        steps: int,
        password_type_targeted: bool = False,
        run_type: dict[str, Any] | None = None,
        summary: dict[str, Any] | None = None,
        why_stopped: dict[str, Any] | None = None,
        last_action: str | None = None,
    ) -> dict[str, Any]:
        end = timestamp_now()
        elapsed = time.perf_counter() - self._started_perf
        self.record.update({
            "end_time": end,
            "status": status,
            "status_message": message,
            "total_steps": steps,
            "total_seconds": round(elapsed, 3),
            "password_type_targeted": password_type_targeted,
        })
        if run_type is not None:
            self.record["run_type"] = run_type
        self.record["summary"] = summary
        self.record["why_stopped"] = why_stopped
        self.record["last_action"] = last_action
        self.store.save(self.record)
        return self.record
