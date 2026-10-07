#!/usr/bin/env python3
"""Check journal output of invio services (SC-007): JSON log lines and no secrets.

usage: journalctl -u invio-run-due -o cat | check-journal-json.py [--allow-no-json]

Every line that starts with ``{`` must be a JSON object with string ``level`` and ``message``
keys. Other lines (plain stdout summaries such as ``nothing to retry``) are allowed. Without
``--allow-no-json`` at least one JSON line is required.

Secrets: ``INVIO_CHECK_SECRETS`` holds newline-separated values (empty entries are ignored,
values shorter than 8 characters are rejected). Each value is searched in its raw, URL-encoded,
backslash-escaped and JSON-escaped form in every line.

Exit codes: 0 pass, 1 violation, 2 usage error or no input, 3 unexpected internal error.
Messages contain line numbers and fixed reasons only, never the text of a line or a secret.
Standard library only; works with Python 3.11 (Debian 12 system Python).
"""

from __future__ import annotations

import json
import os
import sys
from urllib.parse import quote

MIN_SECRET_LENGTH = 8


def _variants(secret: str) -> set[str]:
    return {
        secret,
        quote(secret, safe=""),
        secret.replace("\\", "\\\\").replace('"', '\\"'),
        json.dumps(secret)[1:-1],
    }


def _load_secrets() -> list[set[str]] | None:
    """Return the variants of each configured secret, or ``None`` for a usage error."""
    raw = os.environ.get("INVIO_CHECK_SECRETS", "")
    secrets = [entry for entry in raw.split("\n") if entry.strip()]
    if any(len(entry) < MIN_SECRET_LENGTH for entry in secrets):
        print(
            f"error: each secret in INVIO_CHECK_SECRETS must have at least {MIN_SECRET_LENGTH} "
            "characters",
            file=sys.stderr,
        )
        return None
    return [_variants(entry) for entry in secrets]


def _check_json_line(line: str) -> str | None:
    """Return a reason if ``line`` (starting with ``{``) is not a valid log record."""
    try:
        record = json.loads(line)
    except ValueError:
        return "invalid JSON"
    if not isinstance(record, dict):
        return "JSON is not an object"
    for key in ("level", "message"):
        if not isinstance(record.get(key), str):
            return f"JSON object lacks a string {key!r} key"
    return None


def run(argv: list[str]) -> int:
    allow_no_json = False
    for arg in argv:
        if arg == "--allow-no-json":
            allow_no_json = True
        else:
            print("usage: check-journal-json.py [--allow-no-json]", file=sys.stderr)
            return 2
    secrets = _load_secrets()
    if secrets is None:
        return 2

    text = sys.stdin.buffer.read().decode("utf-8", errors="replace")
    lines = text.splitlines()
    if not any(line.strip() for line in lines):
        print("error: no log lines on stdin", file=sys.stderr)
        return 2

    failures = 0
    json_lines = 0
    for number, line in enumerate(lines, start=1):
        if not line.strip():
            continue
        if any(variant in line for variants in secrets for variant in variants):
            print(f"line {number}: a configured secret appears in the log", file=sys.stderr)
            failures += 1
        if line.startswith("{"):
            json_lines += 1
            reason = _check_json_line(line)
            if reason:
                print(f"line {number}: {reason}", file=sys.stderr)
                failures += 1
    if failures:
        return 1
    if json_lines == 0 and not allow_no_json:
        print("error: no JSON log lines found", file=sys.stderr)
        return 1
    return 0


def main() -> int:
    try:
        return run(sys.argv[1:])
    except Exception:  # never leak a traceback that could contain log text
        print("error: internal error while checking the journal output", file=sys.stderr)
        return 3


if __name__ == "__main__":
    sys.exit(main())
