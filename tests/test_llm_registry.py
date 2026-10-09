"""Tests for the model registry: loading, strict validation and cost computation."""

import os
import re
import sys
from decimal import Decimal
from pathlib import Path

import pytest

import invio.llm
from invio.llm.base import LLMConfigError, ModelRegistryError, Usage
from invio.llm.registry import ModelInfo, ModelRegistry, default_registry, load_registry
from tests.llm_helpers import FIXTURE_REGISTRY_DIR, write_registry

PRICED = "fakeco-priced"
FREE = "fakeco-free"
ENTRY: dict[str, object] = {
    "input_price_per_mtok": 1,
    "output_price_per_mtok": 2,
    "context_window": 1000,
}


def _write_raw(directory: Path, name: str, text: str) -> Path:
    models_dir = directory / "models.d"
    models_dir.mkdir(parents=True, exist_ok=True)
    (models_dir / name).write_text(text, encoding="utf-8")
    return models_dir


def test_fixture_registry_merges_all_files() -> None:
    registry = load_registry([FIXTURE_REGISTRY_DIR])

    assert registry.model_ids() == {PRICED, FREE, "other-model"}
    assert registry.get(PRICED) == ModelInfo(
        model_id=PRICED,
        provider="fakeco",
        input_price_per_mtok=Decimal("2.5"),
        output_price_per_mtok=Decimal("10"),
        context_window=128000,
    )
    other = registry.get("other-model")
    assert other is not None
    assert other.provider == "other"


def test_get_unknown_model_is_none() -> None:
    assert load_registry([FIXTURE_REGISTRY_DIR]).get("nope") is None


def test_cost_for_one_million_tokens_is_the_sum_of_prices() -> None:
    registry = load_registry([FIXTURE_REGISTRY_DIR])

    assert registry.cost(PRICED, Usage(1_000_000, 1_000_000)) == Decimal("12.5")


def test_cost_is_quantized_half_up() -> None:
    registry = load_registry([FIXTURE_REGISTRY_DIR])

    cost = registry.cost(PRICED, Usage(1234, 567))

    # (1234 * 2.5 + 567 * 10) / 1e6 = 0.008755
    assert cost == Decimal("0.008755")
    assert cost is not None
    assert cost.as_tuple().exponent == -6


def test_cost_rounds_half_up_at_the_sixth_decimal() -> None:
    registry = load_registry([FIXTURE_REGISTRY_DIR])

    # 1 * 2.5 / 1e6 = 0.0000025 -> 0.000003
    assert registry.cost(PRICED, Usage(1, 0)) == Decimal("0.000003")


def test_cost_for_zero_tokens_is_zero() -> None:
    cost = load_registry([FIXTURE_REGISTRY_DIR]).cost(PRICED, Usage(0, 0))

    assert cost == Decimal("0.000000")
    assert cost is not None
    assert str(cost) == "0.000000"


def test_cost_for_huge_counts_is_exact() -> None:
    cost = load_registry([FIXTURE_REGISTRY_DIR]).cost(PRICED, Usage(10**12, 10**12))

    assert cost == Decimal("12500000.000000")


def test_zero_priced_model_costs_zero() -> None:
    assert load_registry([FIXTURE_REGISTRY_DIR]).cost(FREE, Usage(500, 500)) == Decimal("0.000000")


def test_unknown_model_cost_is_none() -> None:
    assert load_registry([FIXTURE_REGISTRY_DIR]).cost("nope", Usage(1, 1)) is None


def test_float_prices_stay_exact(tmp_path: Path) -> None:
    models_dir = _write_raw(
        tmp_path,
        "p.yaml",
        "schema_version: 1\nprovider: p\nmodels:\n  m:\n    input_price_per_mtok: 0.1\n"
        "    output_price_per_mtok: 0.2\n    context_window: 10\n",
    )

    info = load_registry([models_dir]).get("m")

    assert info is not None
    assert info.input_price_per_mtok == Decimal("0.1")
    assert info.output_price_per_mtok == Decimal("0.2")


@pytest.mark.parametrize(
    ("entry", "expected"),
    [
        ({**ENTRY, "surprise": 1}, "surprise"),
        ({"input_price_per_mtok": 1, "output_price_per_mtok": 2}, "models.m.context_window"),
        ({**ENTRY, "input_price_per_mtok": -1}, "models.m.input_price_per_mtok"),
        ({**ENTRY, "output_price_per_mtok": "free"}, "models.m.output_price_per_mtok"),
        ({**ENTRY, "context_window": 0}, "models.m.context_window"),
    ],
    ids=["unknown-key", "missing-field", "negative-price", "bad-price", "zero-context"],
)
def test_invalid_entry_names_file_model_and_field(
    tmp_path: Path, entry: dict[str, object], expected: str
) -> None:
    models_dir = write_registry(tmp_path, "p", {"m": entry})

    with pytest.raises(ModelRegistryError) as info:
        load_registry([models_dir])

    assert "p.yaml" in str(info.value)
    assert expected in str(info.value)


@pytest.mark.parametrize("raw", ["true", "5.0", "'5'"])
def test_context_window_must_be_a_real_integer(tmp_path: Path, raw: str) -> None:
    models_dir = _write_raw(
        tmp_path,
        "p.yaml",
        "schema_version: 1\nprovider: p\nmodels:\n  m:\n    input_price_per_mtok: 1\n"
        f"    output_price_per_mtok: 1\n    context_window: {raw}\n",
    )

    with pytest.raises(ModelRegistryError, match=r"models\.m\.context_window"):
        load_registry([models_dir])


@pytest.mark.parametrize("raw", [".inf", ".nan", "-.inf"])
@pytest.mark.parametrize("field", ["input_price_per_mtok", "output_price_per_mtok"])
def test_non_finite_prices_are_rejected(tmp_path: Path, field: str, raw: str) -> None:
    prices = {"input_price_per_mtok": "1", "output_price_per_mtok": "1", field: raw}
    models_dir = _write_raw(
        tmp_path,
        "p.yaml",
        "schema_version: 1\nprovider: p\nmodels:\n  m:\n"
        f"    input_price_per_mtok: {prices['input_price_per_mtok']}\n"
        f"    output_price_per_mtok: {prices['output_price_per_mtok']}\n"
        "    context_window: 5\n",
    )

    with pytest.raises(ModelRegistryError, match=rf"models\.m\.{field}"):
        load_registry([models_dir])


def test_directory_named_like_a_registry_file_is_rejected(tmp_path: Path) -> None:
    models_dir = tmp_path / "models.d"
    (models_dir / "x.yaml").mkdir(parents=True)

    with pytest.raises(ModelRegistryError, match=r"x\.yaml"):
        load_registry([models_dir])


@pytest.mark.skipif(
    sys.platform == "win32" or (hasattr(os, "geteuid") and os.geteuid() == 0),
    reason="permission bits are not enforced for root / on Windows",
)
def test_unreadable_file_is_rejected(tmp_path: Path) -> None:
    models_dir = _write_raw(tmp_path, "x.yaml", "schema_version: 1\nprovider: x\nmodels: {}\n")
    path = models_dir / "x.yaml"
    path.chmod(0)
    try:
        with pytest.raises(ModelRegistryError, match=r"x\.yaml"):
            load_registry([models_dir])
    finally:
        path.chmod(0o600)


@pytest.mark.parametrize(
    ("text", "field"),
    [
        ("schema_version: 1\nprovider: p\nmodels: {}\nowner: me\n", "owner"),
        ("schema_version: 1\nmodels: {}\n", "provider"),
        ("schema_version: 1\nprovider: p\n", "models"),
        ("provider: p\nmodels: {}\n", "schema_version"),
    ],
    ids=["unknown-top-level-key", "missing-provider", "missing-models", "missing-version"],
)
def test_invalid_file_level_field_names_file_and_field(
    tmp_path: Path, text: str, field: str
) -> None:
    models_dir = _write_raw(tmp_path, "p.yaml", text)

    with pytest.raises(ModelRegistryError) as info:
        load_registry([models_dir])

    assert "p.yaml" in str(info.value)
    assert field in str(info.value)


def test_yml_extension_is_not_read(tmp_path: Path) -> None:
    models_dir = _write_raw(tmp_path, "p.yml", "this: is: not: valid\n")

    assert load_registry([models_dir]).model_ids() == frozenset()


def test_wrong_schema_version_is_rejected(tmp_path: Path) -> None:
    models_dir = _write_raw(tmp_path, "p.yaml", "schema_version: 2\nprovider: p\nmodels: {}\n")

    with pytest.raises(ModelRegistryError, match=r"p\.yaml.*schema_version"):
        load_registry([models_dir])


def test_invalid_yaml_is_rejected(tmp_path: Path) -> None:
    models_dir = _write_raw(tmp_path, "p.yaml", "models: [unclosed\n")

    with pytest.raises(ModelRegistryError, match=r"p\.yaml"):
        load_registry([models_dir])


def test_undecodable_file_is_rejected(tmp_path: Path) -> None:
    models_dir = tmp_path / "models.d"
    models_dir.mkdir()
    (models_dir / "p.yaml").write_bytes(b"\xff\xfe\x00")

    with pytest.raises(ModelRegistryError, match=r"p\.yaml"):
        load_registry([models_dir])


def test_non_mapping_document_is_rejected(tmp_path: Path) -> None:
    models_dir = _write_raw(tmp_path, "p.yaml", "- just\n- a list\n")

    with pytest.raises(ModelRegistryError, match=r"p\.yaml"):
        load_registry([models_dir])


def test_empty_model_id_is_rejected(tmp_path: Path) -> None:
    models_dir = write_registry(tmp_path, "p", {"": ENTRY})

    with pytest.raises(ModelRegistryError, match=r"p\.yaml"):
        load_registry([models_dir])


def test_provider_must_match_file_stem(tmp_path: Path) -> None:
    models_dir = _write_raw(tmp_path, "p.yaml", "schema_version: 1\nprovider: q\nmodels: {}\n")

    with pytest.raises(ModelRegistryError) as info:
        load_registry([models_dir])

    assert "p.yaml" in str(info.value)
    assert "'q'" in str(info.value)


def test_duplicate_model_id_across_files_names_id_and_files(tmp_path: Path) -> None:
    write_registry(tmp_path, "a", {"shared": ENTRY})
    models_dir = write_registry(tmp_path, "b", {"shared": ENTRY})

    with pytest.raises(ModelRegistryError) as info:
        load_registry([models_dir])

    message = str(info.value)
    assert "shared" in message
    assert "a.yaml" in message
    assert "b.yaml" in message


def test_missing_directory_is_an_empty_registry(tmp_path: Path) -> None:
    assert load_registry([tmp_path / "nope"]).model_ids() == frozenset()


def test_directory_without_yaml_is_an_empty_registry(tmp_path: Path) -> None:
    models_dir = _write_raw(tmp_path, "README.md", "# not a registry\n")

    assert load_registry([models_dir]).model_ids() == frozenset()


def test_default_dirs_follow_the_package_path(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    write_registry(tmp_path, "pathco", {"pathco-model": ENTRY})
    monkeypatch.setattr(invio.llm, "__path__", [str(tmp_path), *invio.llm.__path__])

    assert "pathco-model" in load_registry().model_ids()


def test_default_registry_is_cached_until_cleared(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    default_registry.cache_clear()
    try:
        assert default_registry() is default_registry()
        write_registry(tmp_path, "later", {"later-model": ENTRY})
        monkeypatch.setattr(invio.llm, "__path__", [str(tmp_path), *invio.llm.__path__])

        assert "later-model" not in default_registry().model_ids()
        default_registry.cache_clear()
        assert "later-model" in default_registry().model_ids()
    finally:
        default_registry.cache_clear()


def test_no_model_ids_in_source() -> None:
    """FR-017 / SC-005: model ids live in registry files and job configs only.

    Vacuous while no registry file ships; the well-known-id guard below covers that gap.
    """
    source_root = Path(invio.__file__).parent
    sources = {path: path.read_text(encoding="utf-8") for path in source_root.rglob("*.py")}

    for model_id in load_registry().model_ids():
        literal = re.compile(r"""(['"])""" + re.escape(model_id) + r"""\1""")
        offenders = [str(p) for p, text in sources.items() if literal.search(text)]
        assert not offenders, f"model id {model_id!r} hard-coded in {offenders}"


def test_no_well_known_model_id_literals_in_source() -> None:
    """FR-017 / SC-005 guard that does not depend on the shipped registry being non-empty."""
    source_root = Path(invio.__file__).parent
    pattern = re.compile(
        r"""['"](?:gpt-|o[134]-|claude-|gemini-|llama|mistral-|open-mistral|ministral|"""
        r"""codestral|magistral|pixtral|mixtral)[^'"]*['"]"""
    )
    sources = list(source_root.rglob("*.py"))

    assert sources
    offenders = {
        str(path): pattern.findall(path.read_text(encoding="utf-8"))
        for path in sources
        if pattern.search(path.read_text(encoding="utf-8"))
    }
    assert not offenders


def _three_models(tmp_path: Path) -> ModelRegistry:
    write_registry(
        tmp_path,
        "acme",
        {
            "acme-b": {**ENTRY, "input_price_per_mtok": 1, "output_price_per_mtok": 1},
            "acme-a": {**ENTRY, "input_price_per_mtok": 0, "output_price_per_mtok": 2},
            "acme-c": {**ENTRY, "input_price_per_mtok": 5, "output_price_per_mtok": 5},
        },
    )
    models_dir = write_registry(tmp_path, "zeta", {"zeta-1": {**ENTRY, "input_price_per_mtok": 0}})
    return load_registry([models_dir])


def test_models_for_returns_only_that_provider_sorted_by_id(tmp_path: Path) -> None:
    registry = _three_models(tmp_path)

    acme = [info.model_id for info in registry.models_for("acme")]

    assert acme == ["acme-a", "acme-b", "acme-c"]
    assert [info.model_id for info in registry.models_for("zeta")] == ["zeta-1"]
    assert registry.models_for("nobody") == []


def test_cheapest_sums_both_prices_and_breaks_ties_by_id(tmp_path: Path) -> None:
    registry = _three_models(tmp_path)

    cheapest = registry.cheapest("acme")

    assert cheapest is not None
    assert cheapest.model_id == "acme-a"  # 0 + 2 ties with 1 + 1; "acme-a" sorts first
    assert registry.cheapest("nobody") is None


def test_require_returns_the_entry_of_that_provider(tmp_path: Path) -> None:
    registry = _three_models(tmp_path)

    assert registry.require("acme-b", "acme").model_id == "acme-b"
    with pytest.raises(LLMConfigError, match="'acme-b' is not registered for LLM provider 'zeta'"):
        registry.require("acme-b", "zeta")
    with pytest.raises(LLMConfigError, match="'nope' is not registered for LLM provider 'acme'"):
        registry.require("nope", "acme")


def test_models_for_returns_sorted_ids_of_one_provider() -> None:
    registry = load_registry([FIXTURE_REGISTRY_DIR])

    assert [info.model_id for info in registry.models_for("fakeco")] == sorted([PRICED, FREE])


def test_models_for_unknown_provider_is_empty() -> None:
    assert load_registry([FIXTURE_REGISTRY_DIR]).models_for("nobody") == []


def test_max_output_tokens_is_accepted_and_exposed(tmp_path: Path) -> None:
    models_dir = write_registry(tmp_path, "p", {"m": {**ENTRY, "max_output_tokens": 64000}})

    info = load_registry([models_dir]).get("m")

    assert info is not None
    assert info.max_output_tokens == 64000


def test_max_output_tokens_defaults_to_none(tmp_path: Path) -> None:
    models_dir = write_registry(tmp_path, "p", {"m": ENTRY})

    info = load_registry([models_dir]).get("m")

    assert info is not None
    assert info.max_output_tokens is None


def test_existing_registry_files_load_without_max_output_tokens() -> None:
    registry = default_registry()

    for provider in ("mistral", "openai"):
        models = registry.models_for(provider)
        assert models
        assert all(info.max_output_tokens is None for info in models)


@pytest.mark.parametrize("raw", ["0", "-5", "5.0", "'5'", "true"])
def test_invalid_max_output_tokens_is_rejected(tmp_path: Path, raw: str) -> None:
    models_dir = _write_raw(
        tmp_path,
        "p.yaml",
        "schema_version: 1\nprovider: p\nmodels:\n  m:\n    input_price_per_mtok: 1\n"
        f"    output_price_per_mtok: 1\n    context_window: 5\n    max_output_tokens: {raw}\n",
    )

    with pytest.raises(ModelRegistryError, match=r"p\.yaml.*models\.m\.max_output_tokens"):
        load_registry([models_dir])


def test_every_anthropic_model_defines_max_output_tokens() -> None:
    models = default_registry().models_for("anthropic")

    assert len(models) == 2
    assert all(info.max_output_tokens and info.max_output_tokens > 0 for info in models)
