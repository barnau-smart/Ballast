"""Structured logging configuration.

Emits key=value style log lines. This module deliberately configures only the
log format/level — it must never log secrets, tokens, or connection strings.
Application code is responsible for keeping sensitive values out of messages.
"""

from __future__ import annotations

import logging


class KeyValueFormatter(logging.Formatter):
    """Render log records as ``ts=... level=... logger=... msg=...`` lines."""

    def format(self, record: logging.LogRecord) -> str:
        base = (
            f"ts={self.formatTime(record, '%Y-%m-%dT%H:%M:%S%z')} "
            f"level={record.levelname} "
            f"logger={record.name} "
            f'msg="{record.getMessage()}"'
        )
        if record.exc_info:
            base += f" exc={self.formatException(record.exc_info)!r}"
        return base


def configure_logging(level: int = logging.INFO) -> None:
    """Install the key=value formatter on the root logger (idempotent)."""
    root = logging.getLogger()
    root.setLevel(level)

    # Replace existing handlers so re-invocation (tests, reload) stays clean.
    for handler in list(root.handlers):
        root.removeHandler(handler)

    handler = logging.StreamHandler()
    handler.setFormatter(KeyValueFormatter())
    root.addHandler(handler)
