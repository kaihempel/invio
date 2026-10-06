"""S29: architecture rules for the run orchestration (graph, pipeline and retry layering)."""

import sys
from pathlib import Path

import invio
import invio.graph
from tests.test_graph_layering import SRC, _imports_of, _is_within

ROOT = Path(invio.__path__[0])
ALLOWED_RETRY_IMPORTS = ("invio.llm.base", "invio.sources.errors")


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
    # Vacuous for invio.cli until #22: this checks that no lower layer reaches up to it.
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
        if top == "invio":
            assert _is_within(name, ALLOWED_RETRY_IMPORTS) or name == "invio", (
                f"retry.py imports {name}"
            )
        else:
            assert top in sys.stdlib_module_names, f"retry.py imports third-party {name}"


def test_source_root_is_the_parent_of_the_package() -> None:
    assert ROOT.parent == SRC
