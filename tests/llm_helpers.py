"""Shared helpers for the LLM layer tests."""

from pathlib import Path

import yaml
from pydantic import BaseModel, Field

from invio.config.settings import Settings

VALID_SCORE_JSON = '{"score": 0.8, "reason": "relevant"}'
FIXTURE_REGISTRY_DIR = Path(__file__).parent / "fixtures" / "llm" / "models.d"


class Score(BaseModel):
    """Structured answer schema used across the LLM tests."""

    score: float = Field(ge=0, le=1)
    reason: str


def write_registry(directory: Path, provider: str, models: dict[str, dict[str, object]]) -> Path:
    """Write ``<directory>/models.d/<provider>.yaml`` and return the ``models.d`` directory."""
    models_dir = directory / "models.d"
    models_dir.mkdir(parents=True, exist_ok=True)
    document = {"schema_version": 1, "provider": provider, "models": models}
    (models_dir / f"{provider}.yaml").write_text(yaml.safe_dump(document), encoding="utf-8")
    return models_dir


def make_settings(**kwargs: object) -> Settings:
    """Build ``Settings`` without reading any ``.env`` file."""
    return Settings(_env_file=None, **kwargs)  # type: ignore[arg-type]
