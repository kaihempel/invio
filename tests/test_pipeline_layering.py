"""S29: architecture rules for the run orchestration (graph, pipeline and retry layering)."""

import sys
from pathlib import Path

import invio
import invio.graph
from tests.test_graph_layering import SRC, _imports_of, _is_within

ROOT = Path(invio.__path__[0])


def _modules(package: str) -> list[Path]:
    paths = sorted((ROOT / package).rglob("*.py"))
    assert paths, f"no modules found under {package}"
    return paths


def test_graph_never_imports_notify_scheduling_cli_or_pipeline() -> None:
    banned = ("invio.notify", "invio.scheduling", "invio.cli", "invio.pipeline")
    for path in _modules("graph"):
        for name in _imports_of(path):
            assert not _is_within(name, banned), f"{path.relative_to(ROOT)} imports {name}"


def test_graph_has_the_new_modules() -> None:
    names = {path.name for path in _modules("graph")}
    assert {"state.py", "ports.py", "stages.py", "build.py", "scope.py"} <= names


def test_only_the_cli_may_import_the_pipeline_package() -> None:
    # The CLI is the one entry point above the pipeline; no lower layer may reach up to it.
    lower = ("db", "llm", "sources", "notify", "scheduling", "services", "graph")
    for package in lower:
        for path in _modules(package):
            for name in _imports_of(path):
                assert not _is_within(name, ("invio.pipeline",)), (
                    f"{path.relative_to(ROOT)} imports {name}"
                )


def test_retry_is_a_leaf_module() -> None:
    path = ROOT / "retry.py"
    for name in _imports_of(path):
        top = name.split(".")[0]
        assert top != "invio", f"retry.py imports {name}"
        assert top in sys.stdlib_module_names, f"retry.py imports third-party {name}"


def test_source_root_is_the_parent_of_the_package() -> None:
    assert ROOT.parent == SRC


def test_new_graph_modules_import_nothing_from_the_upper_layers() -> None:
    banned = ("invio.cli", "invio.pipeline", "invio.notify", "invio.scheduling")
    for module in ("errors.py", "ports.py"):
        for name in _imports_of(ROOT / "graph" / module):
            assert not _is_within(name, banned), f"graph/{module} imports {name}"


def test_the_cli_reaches_the_run_through_the_pipeline_and_services_only() -> None:
    for module in (
        "run_output.py",
        "progress.py",
        "commands/run.py",
        "commands/job.py",
        "commands/run_due.py",
    ):
        for name in _imports_of(ROOT / "cli" / module):
            assert not _is_within(name, ("invio.db", "invio.graph")), f"cli/{module} imports {name}"


def test_textsafe_is_a_leaf_module() -> None:
    for name in _imports_of(ROOT / "textsafe.py"):
        assert name.split(".")[0] != "invio", f"textsafe.py imports {name}"
