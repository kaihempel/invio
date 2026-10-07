"""Edge cases of the shell helpers in ``deploy/scripts`` (FR-010, FR-017, SC-001).

``verify-units.sh`` is driven through a stub ``systemd-analyze`` placed first on ``PATH``, so its
exit-code propagation and the temporary root it builds are tested on every platform. One test
runs the real ``systemd-analyze`` against a deliberately broken unit and is skipped where systemd
is not installed (macOS).
"""

from __future__ import annotations

import os
import shutil
import stat
import subprocess
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
SCRIPTS = REPO / "deploy" / "scripts"
SYSTEMD = REPO / "deploy" / "systemd"
VERIFY_UNITS = SCRIPTS / "verify-units.sh"
NO_SECRETS = SCRIPTS / "check-no-secrets.sh"
BASH = shutil.which("bash") or "/bin/bash"
UNITS = sorted(path.name for path in SYSTEMD.iterdir())


def _run(argv: list[str], env: dict[str, str] | None = None) -> subprocess.CompletedProcess[str]:
    return subprocess.run(argv, capture_output=True, text=True, env=env, check=False)


def _write_executable(path: Path, text: str) -> Path:
    path.write_text(text, encoding="utf-8")
    path.chmod(path.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    return path


# --- check-no-secrets.sh (FR-017, quickstart #14) ----------------------------------------------


@pytest.fixture
def log(tmp_path: Path) -> Path:
    path = tmp_path / "converge.log"
    path.write_text(
        "TASK [invio : Write the env file] ok\n"
        "password=abc.secret-1\n"
        "token: -dash-secret\n"
        "url: mysql+pymysql://invio:pa%2Fss@localhost\n",
        encoding="utf-8",
    )
    return path


def no_secrets(log: Path | str, *secrets: str) -> subprocess.CompletedProcess[str]:
    return _run([str(NO_SECRETS), str(log), *secrets])


def test_no_secrets_script_is_executable_bash() -> None:
    assert os.access(NO_SECRETS, os.X_OK)
    assert NO_SECRETS.read_text(encoding="utf-8").startswith("#!/usr/bin/env bash\n")


@pytest.mark.parametrize("argv", [[], ["only-a-log"]], ids=["no-args", "no-secret"])
def test_no_secrets_usage_error_without_log_and_secret(argv: list[str]) -> None:
    result = _run([str(NO_SECRETS), *argv])
    assert result.returncode == 2
    assert "usage:" in result.stderr


def test_no_secrets_missing_log_is_a_usage_error(tmp_path: Path) -> None:
    result = no_secrets(tmp_path / "absent.log", "abc.secret-1")
    assert result.returncode == 2
    assert "is not a file" in result.stderr


def test_no_secrets_directory_as_log_is_a_usage_error(tmp_path: Path) -> None:
    result = no_secrets(tmp_path, "abc.secret-1")
    assert result.returncode == 2


def test_no_secrets_matches_literally_not_as_a_regex(log: Path) -> None:
    # "abc.secret-1" is in the log; "abcXsecret-1" would match the regex "abc.secret-1" too.
    assert no_secrets(log, "abc.secret-1").returncode == 1
    assert no_secrets(log, "abc[.]secret-1").returncode == 0
    assert no_secrets(log, "a.*1").returncode == 0


def test_no_secrets_secret_starting_with_a_dash_is_not_an_option(log: Path) -> None:
    result = no_secrets(log, "-dash-secret")
    assert result.returncode == 1
    assert "-dash-secret" not in result.stdout + result.stderr


def test_no_secrets_reports_every_hit_by_index_and_never_the_value(log: Path) -> None:
    result = no_secrets(log, "abc.secret-1", "absent-secret", "-dash-secret")
    assert result.returncode == 1
    assert result.stdout == ""
    assert "secret #1 " in result.stderr
    assert "secret #3 " in result.stderr
    assert "secret #2 " not in result.stderr
    for secret in ("abc.secret-1", "absent-secret", "-dash-secret"):
        assert secret not in result.stderr


def test_no_secrets_empty_secret_is_skipped_but_still_counted(log: Path) -> None:
    result = no_secrets(log, "", "abc.secret-1")
    assert result.returncode == 1
    assert "secret #2 " in result.stderr


def test_no_secrets_clean_log_prints_nothing(log: Path) -> None:
    result = no_secrets(log, "not-in-the-log", "also-absent")
    assert (result.returncode, result.stdout, result.stderr) == (0, "", "")


def test_no_secrets_finds_a_secret_in_a_log_with_binary_bytes(tmp_path: Path) -> None:
    path = tmp_path / "converge.log"
    path.write_bytes(b"\x00\x01ANSI \x1b[0;32mok\x1b[0m db=ci-secret-db\xff\n")
    assert no_secrets(path, "ci-secret-db").returncode == 1


def test_no_secrets_does_not_decode_url_encoding(log: Path) -> None:
    # Documents the limit: only the raw form is searched. The CI secrets therefore must not
    # contain characters that urlencode changes (see test_deploy_workflows).
    assert no_secrets(log, "pa/ss").returncode == 0


# --- verify-units.sh (FR-010, SC-001, quickstart #1) -------------------------------------------


@pytest.fixture
def stub_analyze(tmp_path: Path) -> Path:
    """A fake ``systemd-analyze`` that records its arguments and the temporary root's files."""
    bin_dir = tmp_path / "stub-bin"
    bin_dir.mkdir()
    _write_executable(
        bin_dir / "systemd-analyze",
        "#!/bin/sh\n"
        'printf "%s\\n" "$@" > "$STUB_ARGS"\n'
        'for arg in "$@"; do\n'
        '  case "$arg" in --root=*) root="${arg#--root=}";'
        ' (cd "$root" && find . -type f -o -type l | sort) > "$STUB_TREE";'
        ' test -x "$root/opt/invio/.venv/bin/invio" && echo exec-ok >> "$STUB_TREE";; esac\n'
        "done\n"
        'printf "%s" "$STUB_OUTPUT"\n'
        'exit "$STUB_STATUS"\n',
    )
    return bin_dir


def verify_units(
    stub: Path, tmp_path: Path, *, output: str = "", status: int = 0, script: Path = VERIFY_UNITS
) -> subprocess.CompletedProcess[str]:
    env = {
        **os.environ,
        "PATH": f"{stub}{os.pathsep}{os.environ.get('PATH', '')}",
        "STUB_ARGS": str(tmp_path / "args.txt"),
        "STUB_TREE": str(tmp_path / "tree.txt"),
        "STUB_OUTPUT": output,
        "STUB_STATUS": str(status),
    }
    return _run([BASH, str(script)], env=env)


def test_verify_units_clean_run_passes_all_units_with_a_root(
    stub_analyze: Path, tmp_path: Path
) -> None:
    result = verify_units(stub_analyze, tmp_path)
    assert (result.returncode, result.stdout, result.stderr) == (0, "", "")
    args = (tmp_path / "args.txt").read_text(encoding="utf-8").splitlines()
    assert args[0] == "verify"
    (root_arg,) = [arg for arg in args if arg.startswith("--root=")]
    root = root_arg.removeprefix("--root=")
    verified = sorted(Path(arg).name for arg in args[2:])
    assert verified == UNITS, "every shipped unit must be verified"
    assert all(arg.startswith(f"{root}/etc/systemd/system/") for arg in args[2:])
    tree = (tmp_path / "tree.txt").read_text(encoding="utf-8").splitlines()
    assert "exec-ok" in tree, "the stub ExecStart binary must exist and be executable"
    assert {f"./etc/systemd/system/{name}" for name in UNITS} <= set(tree)
    assert not Path(root).exists(), "the temporary root must be removed on exit"


@pytest.mark.parametrize("status", [1, 3])
def test_verify_units_propagates_the_exit_code_and_prints_the_output(
    stub_analyze: Path, tmp_path: Path, status: int
) -> None:
    result = verify_units(stub_analyze, tmp_path, output="invio-x.service: bad\n", status=status)
    assert result.returncode == status
    assert "invio-x.service: bad" in result.stdout


def test_verify_units_fails_on_a_warning_with_exit_code_zero(
    stub_analyze: Path, tmp_path: Path
) -> None:
    warning = "/etc/systemd/system/invio-run-due.service:9: Unknown key name 'Foo', ignoring."
    result = verify_units(stub_analyze, tmp_path, output=warning)
    assert result.returncode == 1
    assert warning in result.stdout


def test_verify_units_fails_without_a_unit(stub_analyze: Path, tmp_path: Path) -> None:
    tree = tmp_path / "repo"
    (tree / "deploy" / "scripts").mkdir(parents=True)
    (tree / "deploy" / "systemd").mkdir()
    script = Path(shutil.copy2(VERIFY_UNITS, tree / "deploy" / "scripts"))
    result = verify_units(stub_analyze, tmp_path, script=script)
    assert result.returncode == 2
    assert "no units found" in result.stderr
    assert not (tmp_path / "args.txt").exists(), "systemd-analyze must not run"


def test_verify_units_rejects_unknown_arguments(stub_analyze: Path, tmp_path: Path) -> None:
    result = _run([BASH, str(VERIFY_UNITS), "--bogus"])
    assert result.returncode == 2
    assert "usage:" in result.stderr


@pytest.fixture
def path_without_analyze(tmp_path: Path) -> dict[str, str]:
    """An environment whose PATH only has ``dirname`` (needed before the tool check)."""
    bin_dir = tmp_path / "bare-bin"
    bin_dir.mkdir()
    dirname = shutil.which("dirname")
    assert dirname is not None
    (bin_dir / "dirname").symlink_to(dirname)
    return {"PATH": str(bin_dir), "HOME": str(tmp_path)}


def test_verify_units_without_systemd_analyze_is_an_error(
    path_without_analyze: dict[str, str],
) -> None:
    result = _run([BASH, str(VERIFY_UNITS)], env=path_without_analyze)
    assert result.returncode == 2
    assert "systemd-analyze not found" in result.stderr


def test_verify_units_if_available_skips_without_systemd_analyze(
    path_without_analyze: dict[str, str],
) -> None:
    result = _run([BASH, str(VERIFY_UNITS), "--if-available"], env=path_without_analyze)
    assert result.returncode == 0
    assert result.stdout == ""
    assert "skipped" in result.stderr


@pytest.mark.skipif(shutil.which("systemd-analyze") is None, reason="needs systemd-analyze")
@pytest.mark.parametrize(
    ("body", "expect_output"),
    [
        ("[Service]\nType=oneshot\nExecStart=/bin/true\nFrobnicate=yes\n", "Frobnicate"),
        ("[Service]\nType=oneshot\n", "ExecStart"),
    ],
    ids=["unknown-key-warning", "missing-execstart-error"],
)
def test_verify_units_real_systemd_rejects_a_broken_unit(
    tmp_path: Path, body: str, expect_output: str
) -> None:
    tree = tmp_path / "repo"
    (tree / "deploy" / "scripts").mkdir(parents=True)
    shutil.copytree(SYSTEMD, tree / "deploy" / "systemd")
    (tree / "deploy" / "systemd" / "invio-broken.service").write_text(
        f"[Unit]\nDescription=broken\n\n{body}", encoding="utf-8"
    )
    script = shutil.copy2(VERIFY_UNITS, tree / "deploy" / "scripts")
    result = _run([BASH, str(script)])
    assert result.returncode != 0
    assert expect_output in result.stdout


@pytest.mark.skipif(shutil.which("systemd-analyze") is None, reason="needs systemd-analyze")
def test_verify_units_real_systemd_accepts_the_shipped_units() -> None:
    result = _run([BASH, str(VERIFY_UNITS)])
    assert (result.returncode, result.stdout, result.stderr) == (0, "", "")
