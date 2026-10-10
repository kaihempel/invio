"""``invio llm``: LLM provider tools."""

import asyncio
import time
from typing import Annotated

import typer

from invio.cli.errors import format_llm_error
from invio.config.settings import get_settings
from invio.llm import registry as llm_registry
from invio.llm.base import LLMAuthError, LLMError, LLMProvider, Usage
from invio.llm.factory import get_provider

app = typer.Typer(help="LLM provider tools.", no_args_is_help=True)

_CHECK_TIMEOUT_CAP_S = 20.0
_CHECK_MAX_TOKENS = 5


def _fail(message: str, code: int) -> typer.Exit:
    typer.echo(message, err=True)
    return typer.Exit(code=code)


async def _check(client: LLMProvider, model: str) -> Usage:
    try:
        _, usage = await client.complete(
            "Connectivity check.",
            "Reply with OK.",
            model=model,
            temperature=0,
            max_tokens=_CHECK_MAX_TOKENS,
        )
    finally:
        await client.aclose()
    return usage


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
    try:
        registry = llm_registry.default_registry()
        settings = get_settings()
        capped = settings.model_copy(
            update={"llm_timeout_seconds": min(settings.llm_timeout_seconds, _CHECK_TIMEOUT_CAP_S)}
        )
        client = get_provider(provider, capped, registry=registry)
        if model is None:
            cheapest = registry.cheapest(provider)
            if cheapest is None:
                raise _fail(
                    f"Configuration error: no models registered for LLM provider '{provider}'", 2
                )
            model = cheapest.model_id
        else:
            registry.require(model, provider)
    except (LLMAuthError, ValueError) as exc:  # LLMConfigError is a ValueError
        raise _fail(f"Configuration error: {exc}", 2) from exc

    started = time.perf_counter()
    try:
        usage = asyncio.run(_check(client, model))
    except LLMError as exc:
        raise _fail(format_llm_error(exc), 1) from exc
    duration_ms = round((time.perf_counter() - started) * 1000, 1)
    typer.echo(
        f"ok provider={provider} model={model} input_tokens={usage.input_tokens} "
        f"output_tokens={usage.output_tokens} duration_ms={duration_ms}"
    )
