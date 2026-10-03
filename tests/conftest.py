"""Shared pytest fixtures."""

import logging
import os
from collections.abc import Iterator
from pathlib import Path

import pytest

from invio.config.settings import get_settings


@pytest.fixture(autouse=True)
def isolated_settings(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Keep the developer's real ``.env`` and ``INVIO_*`` variables out of every test."""
    for key in list(os.environ):
        if key.startswith("INVIO_"):
            monkeypatch.delenv(key)
    monkeypatch.chdir(tmp_path)
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


@pytest.fixture(autouse=True)
def restore_root_logger() -> Iterator[logging.Logger]:
    """Snapshot the root logger's handlers and level and restore them after the test.

    ``configure_logging()`` (also run by every CLI sub-command) mutates the root logger.
    """
    root = logging.getLogger()
    handlers, level = list(root.handlers), root.level
    yield root
    for handler in list(root.handlers):
        if handler not in handlers:
            root.removeHandler(handler)
            handler.close()
    for handler in handlers:
        if handler not in root.handlers:
            root.addHandler(handler)
    root.setLevel(level)
