"""Production wiring of :class:`~invio.graph.ports.RunDeps`.

:func:`default_deps` builds the real adapters from the settings and closes them when the
``async with`` block ends: the database engine, the HTTP client, the web source (and its
browser) and every provider that :attr:`RunDeps.provider_for` created. ``provider_for`` builds
one provider per provider name and run, shared by the roles and fallbacks that name it.
"""

import logging
from collections.abc import AsyncIterator, Awaitable, Callable, Iterable
from contextlib import asynccontextmanager
from datetime import timedelta
from ipaddress import IPv4Network, IPv6Network
from typing import assert_never, get_args

import httpx2
from sqlalchemy import Engine

from invio.config.job import (
    LLMConfig,
    LLMRole,
    LLMRoleModel,
    RoleSelection,
    SourceConfig,
    YoutubeChannelSource,
    YoutubePlaylistSource,
)
from invio.config.settings import Settings
from invio.db.session import check_database_url, create_db_engine, session_factory
from invio.db.types import utcnow
from invio.domain import Candidate
from invio.graph.ports import (
    DeliveryReport,
    FallbackBinding,
    ProviderBinding,
    RoleBinding,
    RunDeps,
)
from invio.llm.base import LLMProvider
from invio.llm.factory import get_provider
from invio.llm.registry import ModelRegistry, default_registry
from invio.notify.email import deliver_digest
from invio.scheduling.backoff import retry_delay
from invio.scheduling.next_run import compute_next_run
from invio.sources.http import HttpClientConfig, NotModified, SafeHttpClient
from invio.sources.netguard import Resolver
from invio.sources.rss import RssFeedSource
from invio.sources.sitemap import SitemapUrlSource
from invio.sources.web import WebPageSource
from invio.sources.youtube import YoutubeSource

__all__ = ["default_deps"]

logger = logging.getLogger("invio.pipeline")


@asynccontextmanager
async def default_deps(
    settings: Settings,
    *,
    registry: ModelRegistry | None = None,
    transport: httpx2.AsyncBaseTransport | None = None,
    allow_networks: Iterable[IPv4Network | IPv6Network] = (),
    resolver: Resolver | None = None,
) -> AsyncIterator[RunDeps]:
    """Yield the production :class:`RunDeps`; close everything it opened on exit.

    ``registry`` defaults to the packaged model registry. ``transport``, ``allow_networks`` and
    ``resolver`` exist for tests (a mock transport behind the real, guarded HTTP client).
    """
    models = registry if registry is not None else default_registry()
    engine = create_db_engine(check_database_url(settings.require_secret("database_url")))
    providers: list[LLMProvider] = []
    web: WebPageSource | None = None
    youtube = YoutubeSource.from_settings(settings)
    try:
        client = SafeHttpClient(
            HttpClientConfig.from_settings(settings),
            allow_networks=allow_networks,
            resolver=resolver,
            transport=transport,
        )
        web = WebPageSource(client)
        rss = RssFeedSource(client)
        sitemap = SitemapUrlSource(client)
        web_source = web

        async def fetch_source(config: SourceConfig) -> list[Candidate]:
            if config.type == "rss":
                return await rss.fetch(config)
            if config.type == "web":
                return await web_source.fetch(config)
            if config.type == "sitemap":
                return await sitemap.fetch(config)
            if isinstance(config, YoutubeChannelSource | YoutubePlaylistSource):
                return await youtube.fetch(config)
            assert_never(config)

        async def fetch_page(url: str) -> str:
            # Unconditional: a page-mode item has the URL of its source, and a conditional
            # request would be answered 304 (research R6). NotModified here would be a bug.
            result = await client.get(url, conditional=False)
            if isinstance(result, NotModified):
                raise RuntimeError("unconditional page fetch returned NotModified")
            return result.text()

        def provider_for(llm_config: LLMConfig) -> ProviderBinding:
            selections = {role: llm_config.role(role) for role in get_args(LLMRole)}
            # Every model is checked before any provider is built, so registry problems
            # surface even when an API key is missing.
            for selection in selections.values():
                models.require(selection.model, selection.provider.value)
                if selection.fallback is not None:
                    models.require(selection.fallback.model, selection.fallback.provider.value)
            _warn_unconfigured_fallback(llm_config)
            built: dict[str, LLMProvider] = {}  # one provider per name and run

            def provider(name: str) -> LLMProvider:
                if name not in built:
                    built[name] = get_provider(name, settings, registry=models)
                    providers.append(built[name])
                return built[name]

            def bind(selection: RoleSelection) -> RoleBinding:
                name = selection.provider.value
                fallback = None
                if selection.fallback is not None:
                    fallback_name = selection.fallback.provider.value
                    fallback = FallbackBinding(
                        provider=provider(fallback_name),
                        provider_name=fallback_name,
                        model=selection.fallback.model,
                    )
                return RoleBinding(
                    provider=provider(name),
                    provider_name=name,
                    model=selection.model,
                    fallback=fallback,
                )

            return ProviderBinding(
                fast=bind(selections["fast"]), smart=bind(selections["smart"]), registry=models
            )

        factory = session_factory(engine)

        async def notify(digest_id: int) -> DeliveryReport:
            outcome = await deliver_digest(factory, digest_id, settings=settings)
            return DeliveryReport(sent=outcome.sent, failed=outcome.failed, error=outcome.error)

        async with client:
            yield RunDeps(
                session_factory=factory,
                fetch_source=fetch_source,
                fetch_page=fetch_page,
                provider_for=provider_for,
                notify=notify,
                next_run=compute_next_run,
                clock=utcnow,
                retry_delay=retry_delay,
                concurrency=settings.max_parallel_items,
                lock_ttl=timedelta(seconds=settings.run_lock_seconds),
            )
    finally:
        youtube.close()
        await _close_all(web, providers, engine)


def _warn_unconfigured_fallback(llm_config: LLMConfig) -> None:
    """Warn when the job-wide fallback is incomplete or unused.

    ``fallback_provider`` alone (the old config form) names no models, so the run goes on
    without a fallback for the roles that have no fallback of their own
    (``llm.fallback_unconfigured``). ``fallback_models`` is never used when every role has its
    own fallback (``llm.fallback_models_unused``).
    """
    if llm_config.fallback_provider is None:
        return
    roles = [role for role in get_args(LLMRole) if not _has_own_fallback(llm_config, role)]
    if llm_config.fallback_models is None and roles:
        logger.warning(
            "llm.fallback_unconfigured",
            extra={"fallback_provider": llm_config.fallback_provider.value, "roles": roles},
        )
    elif llm_config.fallback_models is not None and not roles:
        logger.warning(
            "llm.fallback_models_unused",
            extra={"fallback_provider": llm_config.fallback_provider.value},
        )


def _has_own_fallback(llm_config: LLMConfig, role: LLMRole) -> bool:
    entry = getattr(llm_config.models, role)
    return isinstance(entry, LLMRoleModel) and entry.fallback is not None


async def _close_all(
    web: WebPageSource | None, providers: list[LLMProvider], engine: Engine
) -> None:
    """Close the web source, every provider and the engine; one failure does not stop the rest."""
    closers: list[Callable[[], Awaitable[None]]] = [provider.aclose for provider in providers]
    if web is not None:
        closers.insert(0, web.aclose)
    for close in closers:
        try:
            await close()
        except Exception as err:
            logger.error("deps.close_failed", extra={"error": type(err).__name__})
    engine.dispose()
