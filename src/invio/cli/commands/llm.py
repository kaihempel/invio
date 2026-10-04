"""``invio llm``: LLM provider tools."""

import asyncio
import time
from decimal import Decimal
from typing import Annotated

import typer

from invio.config.settings import get_settings
from invio.llm import registry as llm_registry
from invio.llm.base import LLMAuthError, LLMConfigError, LLMError
from invio.llm.factory import get_provider
from invio.llm.registry import ModelInfo

app = typer.Typer(help="LLM provider tools.", no_args_is_help=True)

_CHECK_TIMEOUT_CAP_S = 20.0
_CHECK_MAX_TOKENS = 5


def _fail(message: str, code: int) -> typer.Exit:
    typer.echo(message, err=True)
    return typer.Exit(code=code)


def _price(info: ModelInfo) -> tuple[Decimal, str]:
    return (info.input_price_per_mtok + info.output_price_per_mtok, info.model_id)


@app.command()
def test(
    provider: Annotated[str, typer.Argument(help="Registered LLM provider name.")],
    model: Annotated[
        str | None,
        typer.Option("--model", help="Registered model id (default: the provider's cheapest)."),
    ] = None,
) -> None:
    """Send one tiny request to PROVIDER to check key, model and connectivity.

    Exit codes: 0 success, 1 the provider call failed, 2 configuration error.
    """
    registry = llm_registry.default_registry()
    try:
        settings = get_settings()
        capped = settings.model_copy(
            update={"llm_timeout_seconds": min(settings.llm_timeout_seconds, _CHECK_TIMEOUT_CAP_S)}
        )
        client = get_provider(provider, capped, registry=registry)
    except (LLMConfigError, LLMAuthError, ValueError) as exc:
        raise _fail(f"Configuration error: {exc}", 2) from exc

    candidates = [info for mid in registry.model_ids() if (info := registry.get(mid))]
    candidates = [info for info in candidates if info.provider == provider]
    if not candidates:
        raise _fail(f"Configuration error: no models registered for LLM provider '{provider}'", 2)
    if model is None:
        model = min(candidates, key=_price).model_id
    elif model not in {info.model_id for info in candidates}:
        raise _fail(
            f"Configuration error: model '{model}' is not registered for LLM provider '{provider}'",
            2,
        )

    started = time.perf_counter()
    try:
        _, usage = asyncio.run(
            client.complete(
                "Connectivity check.",
                "Reply with OK.",
                model=model,
                temperature=0,
                max_tokens=_CHECK_MAX_TOKENS,
            )
        )
    except LLMError as exc:
        raise _fail(f"Error: {type(exc).__name__}: {exc}", 1) from exc
    duration_ms = round((time.perf_counter() - started) * 1000, 1)
    typer.echo(
        f"ok provider={provider} model={model} input_tokens={usage.input_tokens} "
        f"output_tokens={usage.output_tokens} duration_ms={duration_ms}"
    )
