"""Structured JSON logging with per-run context (``job`` and ``run_id``).

Usage::

    configure_logging()
    with run_context(job="daily-digest") as run_id:
        logging.getLogger(__name__).info("started", extra={"sources": 3})

``extra`` keys that clash with a built-in field (``job``, ``run_id``, ``level``, ...) are
emitted with an ``extra_`` prefix instead of being dropped.
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

from invio.config.settings import get_settings, parse_log_level

job_var: ContextVar[str | None] = ContextVar("invio_job", default=None)
run_id_var: ContextVar[str | None] = ContextVar("invio_run_id", default=None)

# Attributes every LogRecord has; anything else was passed via ``extra=``.
_STANDARD_ATTRS = frozenset(
    vars(logging.LogRecord("", logging.INFO, "", 0, "", None, None)).keys()
    | {"message", "asctime", "taskName"}
)

# Keys written by JsonFormatter itself; colliding ``extra`` keys get ``EXTRA_PREFIX``.
RESERVED_KEYS = frozenset(
    {"timestamp", "level", "logger", "message", "job", "run_id", "exc_info", "stack_info"}
)
EXTRA_PREFIX = "extra_"

_JSON_ERRORS = (TypeError, ValueError, RecursionError)


def _safe_str(value: object) -> str:
    """``str(value)`` that never raises (used as ``json.dumps`` fallback)."""
    try:
        return str(value)
    except Exception:
        return f"<unprintable {type(value).__name__}>"


def _safe_repr(value: object) -> str:
    """``repr(value)`` that never raises."""
    try:
        return repr(value)
    except Exception:
        return f"<unprintable {type(value).__name__}>"


def _dumps(value: object) -> str:
    return json.dumps(value, default=_safe_str, ensure_ascii=False, allow_nan=False)


def _to_json(payload: dict[str, Any]) -> str:
    """Serialise ``payload``; values that cannot be strict JSON are replaced by their ``repr``.

    Covers non-string dict keys, circular references and NaN/Infinity, so a bad ``extra``
    value never costs the whole log line.
    """
    try:
        return _dumps(payload)
    except _JSON_ERRORS:
        safe: dict[str, Any] = {}
        for key, value in payload.items():
            try:
                _dumps(value)
                safe[key] = value
            except _JSON_ERRORS:
                safe[key] = _safe_repr(value)
        return _dumps(safe)


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
            if key in _STANDARD_ATTRS:
                continue
            payload[f"{EXTRA_PREFIX}{key}" if key in RESERVED_KEYS else key] = value
        return _to_json(payload)


@contextmanager
def run_context(job: str | None = None, run_id: str | None = None) -> Iterator[str]:
    """Bind ``job`` and ``run_id`` to all log records emitted inside the block.

    A random ``run_id`` is generated when none is given. When ``job`` is omitted, the job of
    an enclosing run context is kept. Yields the active ``run_id``.
    """
    active_job = job if job is not None else job_var.get()
    active_run_id = run_id if run_id is not None else uuid.uuid4().hex
    job_token = job_var.set(active_job)
    run_token = run_id_var.set(active_run_id)
    try:
        yield active_run_id
    finally:
        run_id_var.reset(run_token)
        job_var.reset(job_token)


class _InvioHandler(logging.StreamHandler[TextIO]):
    """Stderr handler that :func:`configure_logging` can recognise and replace.

    It looks up ``sys.stderr`` on every emit, so it keeps working when the stream is swapped
    after configuration (pytest capture, ``CliRunner``).
    """

    def __init__(self) -> None:
        super().__init__(sys.stderr)

    def emit(self, record: logging.LogRecord) -> None:
        self.stream = sys.stderr
        super().emit(record)


def configure_logging(level: str | None = None) -> None:
    """Install a JSON stderr handler on the root logger (idempotent).

    ``level`` defaults to ``get_settings().log_level``. The level is validated before any
    handler is touched, so an invalid value raises :class:`ValueError` (or pydantic's
    ``ValidationError`` for a bad ``INVIO_LOG_LEVEL``) and leaves logging unchanged.
    """
    resolved = parse_log_level(level) if level is not None else get_settings().log_level
    root = logging.getLogger()
    for handler in list(root.handlers):
        if isinstance(handler, _InvioHandler):
            root.removeHandler(handler)
            handler.close()
    handler = _InvioHandler()
    handler.setFormatter(JsonFormatter())
    root.addHandler(handler)
    root.setLevel(resolved)
