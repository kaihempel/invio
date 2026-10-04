"""Model registry: prices and context windows merged from ``models.d/<provider>.yaml`` files.

Registry files are versioned (``schema_version: 1``) and parsed strictly; every problem is a
:class:`~invio.llm.base.ModelRegistryError` naming the file. Costs are computed with
:class:`~decimal.Decimal` and quantized to the precision of ``llm_usage.cost_usd``.
"""

import functools
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from decimal import ROUND_HALF_UP, Decimal
from pathlib import Path
from typing import Annotated, Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, StrictInt, ValidationError, field_validator

import invio.llm
from invio.llm.base import ModelRegistryError, Usage

_PRICE_UNIT = Decimal(1_000_000)
_COST_PRECISION = Decimal("0.000001")


@dataclass(frozen=True, slots=True)
class ModelInfo:
    """One registry entry; prices are USD per 1,000,000 tokens."""

    model_id: str
    provider: str
    input_price_per_mtok: Decimal
    output_price_per_mtok: Decimal
    context_window: int


class _ModelEntry(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    input_price_per_mtok: Decimal = Field(ge=0, allow_inf_nan=False)
    output_price_per_mtok: Decimal = Field(ge=0, allow_inf_nan=False)
    context_window: StrictInt = Field(gt=0)

    @field_validator("input_price_per_mtok", "output_price_per_mtok", mode="before")
    @classmethod
    def _exact_decimal(cls, value: object) -> object:
        # YAML floats such as 0.1 must not carry binary rounding noise into the price.
        return Decimal(str(value)) if isinstance(value, float) else value


class _RegistryFile(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal[1]
    provider: str = Field(min_length=1)
    models: dict[Annotated[str, Field(min_length=1)], _ModelEntry]


class ModelRegistry:
    """Immutable mapping ``model_id -> ModelInfo``."""

    def __init__(self, models: Mapping[str, ModelInfo]) -> None:
        self._models = dict(models)

    def get(self, model_id: str) -> ModelInfo | None:
        """Return the entry of ``model_id`` or ``None`` if it is not registered."""
        return self._models.get(model_id)

    def model_ids(self) -> frozenset[str]:
        """Return all registered model ids."""
        return frozenset(self._models)

    def models_for(self, provider: str) -> list[str]:
        """Return the sorted ids registered for ``provider`` (empty if it has none)."""
        return sorted(m.model_id for m in self._models.values() if m.provider == provider)

    def cost(self, model_id: str, usage: Usage) -> Decimal | None:
        """Return the USD cost of ``usage`` (6 decimals) or ``None`` for an unknown model."""
        info = self._models.get(model_id)
        if info is None:
            return None
        total = (
            usage.input_tokens * info.input_price_per_mtok
            + usage.output_tokens * info.output_price_per_mtok
        ) / _PRICE_UNIT
        return total.quantize(_COST_PRECISION, rounding=ROUND_HALF_UP)


def _parse_file(path: Path) -> _RegistryFile:
    try:
        document: object = yaml.safe_load(path.read_text(encoding="utf-8"))
    except (yaml.YAMLError, UnicodeDecodeError, OSError) as exc:
        raise ModelRegistryError(f"{path}: cannot read registry file: {exc}") from exc
    try:
        parsed = _RegistryFile.model_validate(document)
    except ValidationError as exc:
        problems = "; ".join(
            f"{'.'.join(str(part) for part in err['loc']) or '(root)'}: {err['msg']}"
            for err in exc.errors(include_input=False, include_url=False)
        )
        raise ModelRegistryError(f"{path}: invalid registry file: {problems}") from exc
    if parsed.provider != path.stem:
        raise ModelRegistryError(
            f"{path}: provider '{parsed.provider}' does not match file name '{path.stem}'"
        )
    return parsed


def load_registry(dirs: Sequence[Path] | None = None) -> ModelRegistry:
    """Merge every ``*.yaml`` file of ``dirs`` (default: ``models.d`` of the package path)."""
    if dirs is None:
        dirs = [Path(entry) / "models.d" for entry in invio.llm.__path__]
    models: dict[str, ModelInfo] = {}
    origin: dict[str, Path] = {}
    for directory in dirs:
        for path in sorted(directory.glob("*.yaml")):
            parsed = _parse_file(path)
            for model_id, entry in parsed.models.items():
                if model_id in origin:
                    raise ModelRegistryError(
                        f"model '{model_id}' is defined in both {origin[model_id]} and {path}"
                    )
                origin[model_id] = path
                models[model_id] = ModelInfo(
                    model_id=model_id,
                    provider=parsed.provider,
                    input_price_per_mtok=entry.input_price_per_mtok,
                    output_price_per_mtok=entry.output_price_per_mtok,
                    context_window=entry.context_window,
                )
    return ModelRegistry(models)


@functools.cache
def default_registry() -> ModelRegistry:
    """Return the registry of the installed package (cached; ``cache_clear()`` reloads)."""
    return load_registry()
