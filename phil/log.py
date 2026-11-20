# SPDX-License-Identifier: MIT
# Phil - Phone Human Interaction Layer
# Copyright (c) 2026 David Aluu
# Licensed under the MIT License. See the LICENSE file in the project root.
"""Terminal and file logging, with API-key redaction."""

from __future__ import annotations

import logging
from collections.abc import Iterable
from logging.handlers import RotatingFileHandler

from rich.console import Console
from rich.logging import RichHandler

LOGGER_NAME = "phil"

_FILE_FORMAT = "%(asctime)s %(levelname)-8s %(name)s: %(message)s"
_MAX_LOG_BYTES = 1_000_000
_BACKUP_COUNT = 3
_MIN_SECRET_LENGTH = 8


class RedactingFilter(logging.Filter):
    """Replaces known secrets (such as the API key) with ``***`` in log messages."""

    def __init__(self, secrets: Iterable[str] = ()) -> None:
        super().__init__()
        self._secrets = [s for s in secrets if s and len(s) >= _MIN_SECRET_LENGTH]

    def filter(self, record: logging.LogRecord) -> bool:
        if not self._secrets:
            return True
        message = record.getMessage()
        redacted = message
        for secret in self._secrets:
            redacted = redacted.replace(secret, "***")
        if redacted != message:
            record.msg = redacted
            record.args = ()
        return True


def setup_logging(
    level: str = "INFO",
    log_file: str | None = None,
    secrets: Iterable[str] = (),
) -> logging.Logger:
    """Configure the ``phil`` logger: a readable console and an optional rotating file.

    Args:
        level: One of DEBUG, INFO, WARNING, ERROR.
        log_file: Optional path for a persistent log.
        secrets: Values that must never appear in logs.
    """
    logger = logging.getLogger(LOGGER_NAME)
    logger.setLevel(level)
    logger.propagate = False

    for handler in list(logger.handlers):
        logger.removeHandler(handler)
        handler.close()

    redactor = RedactingFilter(secrets)

    # markup=False matters: UI lines contain "[Button]" which Rich would treat as markup.
    console_handler = RichHandler(
        console=Console(),
        show_path=False,
        markup=False,
        rich_tracebacks=(level == "DEBUG"),
        log_time_format="%H:%M:%S",
    )
    console_handler.addFilter(redactor)
    logger.addHandler(console_handler)

    if log_file:
        try:
            file_handler = RotatingFileHandler(
                log_file,
                maxBytes=_MAX_LOG_BYTES,
                backupCount=_BACKUP_COUNT,
                encoding="utf-8",
            )
        except OSError as exc:
            logger.warning("Could not open log file %s: %s", log_file, exc)
        else:
            file_handler.setFormatter(logging.Formatter(_FILE_FORMAT))
            file_handler.addFilter(redactor)
            logger.addHandler(file_handler)

    return logger
