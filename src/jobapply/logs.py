"""Structured logging on top of the standard library.

`log.info("collected", source="greenhouse", count=12)` renders as key=value pairs,
or as one JSON object per line when `JOBAPPLY_LOG_JSON=true`.
"""

from __future__ import annotations

import json
import logging
import sys
from datetime import UTC, datetime
from typing import Any

_RESERVED = set(logging.makeLogRecord({}).__dict__) | {"message", "asctime"}


class _KeyValueFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        extra = {k: v for k, v in record.__dict__.items() if k not in _RESERVED}
        ts = datetime.fromtimestamp(record.created, UTC).strftime("%Y-%m-%dT%H:%M:%S")
        pairs = " ".join(f"{k}={v!r}" for k, v in extra.items())
        line = f"{ts} {record.levelname:<7} {record.name}: {record.getMessage()}"
        if pairs:
            line += " " + pairs
        if record.exc_info:
            line += "\n" + self.formatException(record.exc_info)
        return line


class _JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "ts": datetime.fromtimestamp(record.created, UTC).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "event": record.getMessage(),
        }
        payload.update({k: v for k, v in record.__dict__.items() if k not in _RESERVED})
        if record.exc_info:
            payload["exc"] = self.formatException(record.exc_info)
        return json.dumps(payload, default=str)


class BoundLogger(logging.LoggerAdapter):
    """Logger that accepts keyword fields: `log.info("event", key=value)`."""

    def _log_kw(self, level: int, msg: str, args: tuple, kwargs: dict[str, Any]) -> None:
        exc_info = kwargs.pop("exc_info", None)
        fields = {**(self.extra or {}), **kwargs}
        self.logger.log(level, msg, *args, extra=fields, exc_info=exc_info, stacklevel=3)

    def debug(self, msg, *args, **kw): self._log_kw(logging.DEBUG, msg, args, kw)  # noqa: E704
    def info(self, msg, *args, **kw): self._log_kw(logging.INFO, msg, args, kw)  # noqa: E704
    def warning(self, msg, *args, **kw): self._log_kw(logging.WARNING, msg, args, kw)  # noqa: E704
    def error(self, msg, *args, **kw): self._log_kw(logging.ERROR, msg, args, kw)  # noqa: E704

    def exception(self, msg, *args, **kw):
        kw.setdefault("exc_info", True)
        self._log_kw(logging.ERROR, msg, args, kw)

    def bind(self, **fields: Any) -> BoundLogger:
        return BoundLogger(self.logger, {**(self.extra or {}), **fields})


def configure_logging(level: str = "INFO", json_output: bool = False) -> None:
    handler = logging.StreamHandler(sys.stderr)
    handler.setFormatter(_JsonFormatter() if json_output else _KeyValueFormatter())
    root = logging.getLogger()
    root.handlers[:] = [handler]
    root.setLevel(level.upper())
    for noisy in ("httpx", "httpcore", "apscheduler.executors.default", "fontTools"):
        logging.getLogger(noisy).setLevel(logging.WARNING)


def get_logger(name: str) -> BoundLogger:
    return BoundLogger(logging.getLogger(name), {})
