"""Structured JSON logging with per-run context (``job`` and ``run_id``).

Usage::

    configure_logging()
    with run_context(job="daily-digest") as run_id:
        logging.getLogger(__name__).info("started", extra={"sources": 3})
"""

import json
import logging
import sys
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from datetime import UTC, datetime
from typing import Any, TextIO

from invio.config.settings import get_settings

job_var: ContextVar[str | None] = ContextVar("invio_job", default=None)
run_id_var: ContextVar[str | None] = ContextVar("invio_run_id", default=None)

# Attributes every LogRecord has; anything else was passed via ``extra=``.
_STANDARD_ATTRS = frozenset(
    vars(logging.LogRecord("", logging.INFO, "", 0, "", None, None)).keys()
    | {"message", "asctime", "taskName"}
)


class JsonFormatter(logging.Formatter):
    """Format each record as a single-line JSON object."""

    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "timestamp": datetime.fromtimestamp(record.created, tz=UTC).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
        }
        job = job_var.get()
        if job is not None:
            payload["job"] = job
        run_id = run_id_var.get()
        if run_id is not None:
            payload["run_id"] = run_id
        if record.exc_info:
            payload["exc_info"] = self.formatException(record.exc_info)
        if record.stack_info:
            payload["stack_info"] = self.formatStack(record.stack_info)
        for key, value in vars(record).items():
            if key not in _STANDARD_ATTRS and key not in payload:
                payload[key] = value
        return json.dumps(payload, default=str, ensure_ascii=False)


@contextmanager
def run_context(job: str | None = None, run_id: str | None = None) -> Iterator[str]:
    """Bind ``job`` and ``run_id`` to all log records emitted inside the block.

    A random ``run_id`` is generated when none is given. Yields the active ``run_id``.
    """
    active_run_id = run_id or uuid.uuid4().hex
    job_token = job_var.set(job)
    run_token = run_id_var.set(active_run_id)
    try:
        yield active_run_id
    finally:
        run_id_var.reset(run_token)
        job_var.reset(job_token)


class _InvioHandler(logging.StreamHandler[TextIO]):
    """Marker subclass so :func:`configure_logging` can replace only its own handler."""


def configure_logging(level: str | None = None) -> None:
    """Install a JSON stderr handler on the root logger (idempotent).

    ``level`` defaults to ``get_settings().log_level``.
    """
    root = logging.getLogger()
    for handler in list(root.handlers):
        if isinstance(handler, _InvioHandler):
            root.removeHandler(handler)
            handler.close()
    handler = _InvioHandler(sys.stderr)
    handler.setFormatter(JsonFormatter())
    root.addHandler(handler)
    root.setLevel((level or get_settings().log_level).upper())
