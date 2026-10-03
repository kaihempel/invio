"""Tests for ``invio.log``: JSON formatting, run context and logging configuration."""

import io
import json
import logging
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


def test_configure_logging_is_idempotent_and_writes_json_to_stderr(
    capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    root = logging.getLogger()
    original_level = root.level
    monkeypatch.setenv("INVIO_LOG_LEVEL", "warning")
    try:
        configure_logging()
        configure_logging()
        json_handlers = [h for h in root.handlers if isinstance(h.formatter, JsonFormatter)]
        assert len(json_handlers) == 1
        assert root.level == logging.WARNING

        with run_context(job="cli") as run_id:
            logging.getLogger("invio.cfgtest").warning("visible")
            logging.getLogger("invio.cfgtest").info("filtered out")

        lines = [json.loads(line) for line in capsys.readouterr().err.splitlines()]
        assert [line["message"] for line in lines] == ["visible"]
        assert lines[0]["run_id"] == run_id

        configure_logging("debug")
        assert root.level == logging.DEBUG
    finally:
        for handler in [h for h in root.handlers if isinstance(h.formatter, JsonFormatter)]:
            root.removeHandler(handler)
        root.setLevel(original_level)
