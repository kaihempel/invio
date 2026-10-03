"""Tests for ``invio.log``: JSON formatting, run context and logging configuration."""

import io
import json
import logging
import sys
from collections.abc import Iterator
from typing import Any

import pytest

from invio.log import JsonFormatter, configure_logging, job_var, run_context, run_id_var


@pytest.fixture
def log_stream() -> Iterator[io.StringIO]:
    """Attach a JsonFormatter handler writing to a StringIO to a dedicated test logger."""
    stream = io.StringIO()
    handler = logging.StreamHandler(stream)
    handler.setFormatter(JsonFormatter())
    logger = logging.getLogger("invio.test")
    original_level = logger.level
    logger.addHandler(handler)
    logger.setLevel(logging.DEBUG)
    yield stream
    logger.removeHandler(handler)
    logger.setLevel(original_level)


def _records(stream: io.StringIO) -> list[dict[str, Any]]:
    return [json.loads(line) for line in stream.getvalue().splitlines()]


def test_log_line_is_valid_json_with_core_fields(log_stream: io.StringIO) -> None:
    logging.getLogger("invio.test").info("hello %s", "world")

    (record,) = _records(log_stream)
    assert record["message"] == "hello world"
    assert record["level"] == "INFO"
    assert record["logger"] == "invio.test"
    assert record["timestamp"].endswith("+00:00")
    assert "run_id" not in record
    assert "job" not in record


def test_run_context_adds_job_and_run_id(log_stream: io.StringIO) -> None:
    with run_context(job="digest") as run_id:
        logging.getLogger("invio.test").info("inside")

    (record,) = _records(log_stream)
    assert record["run_id"] == run_id
    assert len(run_id) == 32
    assert record["job"] == "digest"


def test_run_context_uses_given_run_id_and_resets_on_exit(log_stream: io.StringIO) -> None:
    with run_context(job="outer", run_id="run-1") as run_id:
        assert run_id == "run-1"
        with run_context(job="inner"):
            assert job_var.get() == "inner"
        assert (job_var.get(), run_id_var.get()) == ("outer", "run-1")

    assert job_var.get() is None
    assert run_id_var.get() is None
    logging.getLogger("invio.test").info("after")
    assert "run_id" not in _records(log_stream)[0]


def test_run_context_resets_after_exception() -> None:
    with pytest.raises(ValueError), run_context(job="boom"):
        raise ValueError("fail")

    assert job_var.get() is None
    assert run_id_var.get() is None


def test_exception_info_is_included(log_stream: io.StringIO) -> None:
    try:
        raise RuntimeError("kaputt")
    except RuntimeError:
        logging.getLogger("invio.test").exception("failed")

    (record,) = _records(log_stream)
    assert record["level"] == "ERROR"
    assert "RuntimeError: kaputt" in record["exc_info"]


def test_extra_fields_are_included(log_stream: io.StringIO) -> None:
    logging.getLogger("invio.test").info("fetched", extra={"source": "rss", "count": 3, "obj": {1}})

    (record,) = _records(log_stream)
    assert record["source"] == "rss"
    assert record["count"] == 3
    assert record["obj"] == "{1}"  # non-JSON values are stringified
    assert "args" not in record
    assert "levelno" not in record


def _json_handlers(root: logging.Logger) -> list[logging.Handler]:
    return [h for h in root.handlers if isinstance(h.formatter, JsonFormatter)]


def test_configure_logging_is_idempotent_and_writes_json_to_stderr(
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
    restore_root_logger: logging.Logger,
) -> None:
    root = restore_root_logger
    monkeypatch.setenv("INVIO_LOG_LEVEL", "warning")

    configure_logging()
    configure_logging()
    assert len(_json_handlers(root)) == 1
    assert root.level == logging.WARNING

    with run_context(job="cli") as run_id:
        logging.getLogger("invio.cfgtest").warning("visible")
        logging.getLogger("invio.cfgtest").info("filtered out")

    lines = [json.loads(line) for line in capsys.readouterr().err.splitlines()]
    assert [line["message"] for line in lines] == ["visible"]
    assert lines[0]["run_id"] == run_id

    configure_logging("debug")
    assert root.level == logging.DEBUG


def test_configure_logging_rejects_invalid_level_without_side_effects(
    restore_root_logger: logging.Logger,
) -> None:
    root = restore_root_logger
    configure_logging("info")
    (handler,) = _json_handlers(root)

    with pytest.raises(ValueError, match="invalid log level 'LOUD'"):
        configure_logging("LOUD")

    assert _json_handlers(root) == [handler]
    assert root.level == logging.INFO


def test_configure_logging_follows_replaced_stderr(
    monkeypatch: pytest.MonkeyPatch, restore_root_logger: logging.Logger
) -> None:
    configure_logging("info")
    replacement = io.StringIO()
    monkeypatch.setattr(sys, "stderr", replacement)

    logging.getLogger("invio.cfgtest").info("late stream")

    assert json.loads(replacement.getvalue())["message"] == "late stream"


def test_nested_run_context_keeps_outer_job_when_omitted() -> None:
    with run_context(job="digest", run_id="outer"):
        with run_context(run_id="inner") as inner_id:
            assert (job_var.get(), run_id_var.get(), inner_id) == ("digest", "inner", "inner")
        assert run_id_var.get() == "outer"


def test_run_context_keeps_explicit_empty_run_id() -> None:
    with run_context(run_id="") as run_id:
        assert run_id == ""
        assert run_id_var.get() == ""


@pytest.mark.parametrize("in_run", [False, True])
def test_reserved_extra_keys_are_prefixed_not_dropped(
    log_stream: io.StringIO, in_run: bool
) -> None:
    extra = {"job": "x", "run_id": "y", "level": "z", "timestamp": "t", "logger": "l"}
    if in_run:
        with run_context(job="real-job", run_id="real-run"):
            logging.getLogger("invio.test").info("msg", extra=extra)
    else:
        logging.getLogger("invio.test").info("msg", extra=extra)

    (record,) = _records(log_stream)
    for key, value in extra.items():
        assert record[f"extra_{key}"] == value
    assert record["level"] == "INFO"
    assert record["logger"] == "invio.test"
    assert record.get("job") == ("real-job" if in_run else None)
    assert record.get("run_id") == ("real-run" if in_run else None)


def test_exception_and_stack_info_are_both_included(log_stream: io.StringIO) -> None:
    try:
        raise RuntimeError("kaputt")
    except RuntimeError:
        logging.getLogger("invio.test").error("failed", exc_info=True, stack_info=True)

    (record,) = _records(log_stream)
    assert "RuntimeError: kaputt" in record["exc_info"]
    assert record["stack_info"].startswith("Stack (most recent call last)")


class _Unprintable:
    def __str__(self) -> str:
        raise RuntimeError("no str")

    def __repr__(self) -> str:
        raise RuntimeError("no repr")


def test_values_that_are_not_strict_json_do_not_lose_the_line(log_stream: io.StringIO) -> None:
    circular: list[Any] = []
    circular.append(circular)

    logging.getLogger("invio.test").info(
        "odd values",
        extra={
            "tuple_keys": {(1, 2): "a"},
            "circular": circular,
            "nan": float("nan"),
            "inf": float("inf"),
            "unprintable": _Unprintable(),
            "ok": 1,
        },
    )

    raw = log_stream.getvalue()
    assert "NaN" not in raw and "Infinity" not in raw
    (record,) = _records(log_stream)
    assert record["message"] == "odd values"
    assert record["tuple_keys"] == "{(1, 2): 'a'}"
    assert record["circular"] == "[[...]]"
    assert record["nan"] == "nan"
    assert record["inf"] == "inf"
    assert record["unprintable"] == "<unprintable _Unprintable>"
    assert record["ok"] == 1
