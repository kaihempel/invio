"""Production wiring of :class:`~invio.graph.ports.RunDeps`.

:func:`default_deps` builds the real adapters from the settings and closes them when the
``async with`` block ends: the database engine, the HTTP client, the web source (and its
browser) and every provider that :attr:`RunDeps.provider_for` created.
"""

import logging
from collections.abc import AsyncIterator, Awaitable, Callable, Iterable
from contextlib import asynccontextmanager
from datetime import timedelta
from ipaddress import IPv4Network, IPv6Network
from typing import assert_never

import httpx2
from sqlalchemy import Engine

from invio.config.job import LLMConfig, SourceConfig, YoutubeChannelSource, YoutubePlaylistSource
from invio.config.settings import Settings
from invio.db.session import check_database_url, create_db_engine, session_factory
from invio.db.types import utcnow
from invio.domain import Candidate
from invio.graph.ports import DeliveryReport, ProviderBinding, RunDeps
from invio.llm.base import LLMProvider
from invio.llm.factory import resolve
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
    youtube: YoutubeSource | None = None
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
        youtube = youtube_source = _youtube_source(settings)
        web_source = web

        async def fetch_source(config: SourceConfig) -> list[Candidate]:
            if config.type == "rss":
                return await rss.fetch(config)
            if config.type == "web":
                return await web_source.fetch(config)
            if config.type == "sitemap":
                return await sitemap.fetch(config)
            if isinstance(config, YoutubeChannelSource | YoutubePlaylistSource):
                return await youtube_source.fetch(config)
            assert_never(config)

        async def fetch_page(url: str) -> str:
            # Unconditional: a page-mode item has the URL of its source, and a conditional
            # request would be answered 304 (research R6). NotModified here would be a bug.
            result = await client.get(url, conditional=False)
            if isinstance(result, NotModified):
                raise RuntimeError("unconditional page fetch returned NotModified")
            return result.text()

        def provider_for(llm_config: LLMConfig) -> ProviderBinding:
            provider, fast_model = resolve(llm_config, "fast", settings, registry=models)
            providers.append(provider)
            provider_name = llm_config.provider.value
            smart_model = llm_config.models.smart
            models.require(smart_model, provider_name)
            return ProviderBinding(
                provider=provider,
                provider_name=provider_name,
                fast_model=fast_model,
                smart_model=smart_model,
                registry=models,
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
        if youtube is not None:
            youtube.close()
        await _close_all(web, providers, engine)


def _youtube_source(settings: Settings) -> YoutubeSource:
    """The YouTube adapter (blank cookies/proxy values are already ``None`` in ``Settings``)."""
    proxy = settings.youtube_proxy
    return YoutubeSource(
        cookies_file=settings.youtube_cookies_file,
        proxy=proxy.get_secret_value() if proxy is not None else None,
        timeout=settings.youtube_timeout_seconds,
    )


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
