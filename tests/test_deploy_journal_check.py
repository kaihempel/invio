"""Tests for ``deploy/scripts/check-journal-json.py`` (journal contract, SC-007).

Exit codes: 0 pass, 1 violation, 2 usage error or no input, 3 unexpected internal error.
The script must never print the text of an input line or a secret.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from urllib.parse import quote

import pytest

from tests.deploy_helpers import SCRIPTS

SCRIPT = SCRIPTS / "check-journal-json.py"

GOOD = json.dumps({"level": "INFO", "message": "run started", "run_id": "abc"})
SECRET = "s3cret-pass/word"


def run(
    stdin: str | bytes,
    secrets: str | None = None,
    *args: str,
) -> subprocess.CompletedProcess[str]:
    env = {k: v for k, v in os.environ.items() if k != "INVIO_CHECK_SECRETS"}
    if secrets is not None:
        env["INVIO_CHECK_SECRETS"] = secrets
    data = stdin if isinstance(stdin, bytes) else stdin.encode()
    result = subprocess.run(
        [sys.executable, str(SCRIPT), *args],
        input=data,
        capture_output=True,
        env=env,
        check=False,
    )
    return subprocess.CompletedProcess(
        result.args, result.returncode, result.stdout.decode(), result.stderr.decode()
    )


def output(result: subprocess.CompletedProcess[str]) -> str:
    return result.stdout + result.stderr


def test_script_is_executable_with_python3_shebang() -> None:
    assert os.access(SCRIPT, os.X_OK)
    assert SCRIPT.read_text(encoding="utf-8").startswith("#!/usr/bin/env python3\n")


def test_mixed_json_and_plain_lines_pass() -> None:
    result = run(f"{GOOD}\nnothing to retry\n{GOOD}\n")
    assert result.returncode == 0, output(result)


def test_invalid_json_line_names_the_line_number_only() -> None:
    result = run(f'{GOOD}\nplain\n{{"level": "INFO", "message": TOPSECRETTEXT\n')
    assert result.returncode == 1
    assert "line 3" in output(result)
    assert "TOPSECRETTEXT" not in output(result)


@pytest.mark.parametrize(
    "line",
    ['{"message": "no level"}', '{"level": "INFO"}', "{}", '{"level": "INFO", "message": 3}'],
)
def test_json_line_without_level_or_message_fails(line: str) -> None:
    result = run(f"{GOOD}\n{line}\n")
    assert result.returncode == 1
    assert "line 2" in output(result)


def test_empty_input_is_a_usage_error() -> None:
    result = run("")
    assert result.returncode == 2
    assert "no log lines" in output(result)


def test_only_blank_lines_is_a_usage_error() -> None:
    assert run("\n\n  \n").returncode == 2


def test_input_without_any_json_line_fails() -> None:
    result = run("nothing to retry\n")
    assert result.returncode == 1
    assert "no JSON" in output(result)


def test_allow_no_json_accepts_plain_only_output() -> None:
    assert run("nothing to retry\n", None, "--allow-no-json").returncode == 0


def test_allow_no_json_still_rejects_bad_json_and_empty_input() -> None:
    assert run("{broken\n", None, "--allow-no-json").returncode == 1
    assert run("", None, "--allow-no-json").returncode == 2


def test_unknown_argument_is_a_usage_error() -> None:
    assert run(GOOD, None, "--bogus").returncode == 2


@pytest.mark.parametrize("fmt", ["json", "plain"])
def test_secret_in_a_line_fails_without_printing_it(fmt: str) -> None:
    line = (
        json.dumps({"level": "INFO", "message": f"pw={SECRET}"})
        if fmt == "json"
        else f"leaked {SECRET} here"
    )
    result = run(f"{GOOD}\n{line}\n", SECRET)
    assert result.returncode == 1
    assert "line 2" in output(result)
    assert SECRET not in output(result)


ESCAPED_SECRET = 'a"b\\c-longsecret'


@pytest.mark.parametrize(
    ("secret", "variant"),
    [
        (SECRET, quote(SECRET, safe="")),
        (ESCAPED_SECRET, ESCAPED_SECRET.replace("\\", "\\\\").replace('"', '\\"')),
    ],
)
def test_encoded_variants_of_a_secret_are_detected(secret: str, variant: str) -> None:
    assert variant != secret
    result = run(f"{GOOD}\nurl=mysql://u:{variant}@h/db\n", secret)
    assert result.returncode == 1
    assert variant not in output(result)


def test_json_escaped_secret_is_detected() -> None:
    secret = 'pa"ss\\word-123'
    line = json.dumps({"level": "INFO", "message": secret})
    assert secret not in line
    assert run(f"{line}\n", secret).returncode == 1


def test_secrets_are_newline_delimited_and_empty_entries_are_ignored() -> None:
    result = run(f"{GOOD}\n", "\n\nabcdefgh-one\n\nabcdefgh-two\n")
    assert result.returncode == 0, output(result)
    assert run(f"{GOOD}\nuses abcdefgh-two\n", "\n\nabcdefgh-one\nabcdefgh-two").returncode == 1


def test_secret_absent_passes() -> None:
    assert run(f"{GOOD}\n", SECRET).returncode == 0


def test_short_secret_is_rejected_as_usage_error() -> None:
    result = run(f"{GOOD}\n", "zq7x")
    assert result.returncode == 2
    assert "zq7x" not in output(result)


def test_require_secrets_rejects_a_missing_or_empty_secret_list() -> None:
    for secrets in (None, "", "\n \n"):
        result = run(f"{GOOD}\n", secrets, "--require-secrets")
        assert result.returncode == 2, secrets
        assert "INVIO_CHECK_SECRETS is empty" in output(result)


def test_require_secrets_passes_with_secrets() -> None:
    assert run(f"{GOOD}\n", SECRET, "--require-secrets").returncode == 0


@pytest.mark.parametrize("separator", ["\u2028", "\u2029", "\x85"])
def test_unicode_line_separators_inside_a_record_do_not_split_it(separator: str) -> None:
    # A JSON logger with ensure_ascii=False writes these characters unescaped.
    record = json.dumps({"level": "INFO", "message": f"a{separator}b"}, ensure_ascii=False)
    assert separator in record
    result = run(f"{record}\n")
    assert result.returncode == 0, output(result)


def test_secret_next_to_a_unicode_separator_is_found() -> None:
    record = json.dumps({"level": "INFO", "message": f"x\u2028{SECRET}"}, ensure_ascii=False)
    assert run(f"{record}\n", SECRET).returncode == 1


def test_crlf_line_endings_are_accepted() -> None:
    assert run(f"{GOOD}\r\nnothing to retry\r\n").returncode == 0


def test_invalid_utf8_input_does_not_crash() -> None:
    result = run(GOOD.encode() + b"\n\xff\xfe plain bytes\n")
    assert result.returncode == 0, output(result)


def test_unexpected_error_exits_3_without_traceback() -> None:
    # A closed stdin makes reading raise; the script must report a fixed message.
    broken = subprocess.run(
        [
            sys.executable,
            "-c",
            "import os,runpy,sys; os.close(0); sys.argv=[sys.argv[0]]; "
            f"runpy.run_path({str(SCRIPT)!r}, run_name='__main__')",
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    assert broken.returncode == 3
    assert "Traceback" not in broken.stderr
    assert "internal error" in broken.stderr
