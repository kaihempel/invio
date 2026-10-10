"""``default_deps``: source dispatch, page fetch, provider binding, delivery mapping, closing."""

import logging
import time
from datetime import timedelta
from pathlib import Path
from typing import Any, ClassVar

import httpx2
import pytest

from invio.config.job import JobConfig
from invio.config.settings import MissingSettingError, Settings
from invio.llm.base import LLMAuthError, LLMConfigError
from invio.llm.fake import FakeProvider
from invio.llm.registry import ModelRegistry, load_registry
from invio.notify.email import DeliveryOutcome
from invio.pipeline import deps as deps_module
from invio.pipeline.deps import default_deps
from invio.scheduling.next_run import compute_next_run
from invio.sources.http import NotModified, SafeHttpClient
from invio.sources.youtube import YoutubeSource
from tests.http_helpers import FakeResolver, RecordingTransport
from tests.llm_helpers import write_registry
from tests.pipeline_helpers import article_html, make_job_config

RESOLVER = FakeResolver({"example.com": ["93.184.216.34"]})
FEED = """<?xml version="1.0"?><rss version="2.0"><channel><title>t</title>
<item><title>Post</title><link>https://example.com/post</link></item></channel></rss>"""


def _settings(**kwargs: Any) -> Settings:
    return Settings(
        _env_file=None,
        database_url="sqlite+pysqlite://",  # type: ignore[arg-type]
        http_respect_robots=False,
        http_host_interval_seconds=0.01,
        **kwargs,
    )


def _config(**overrides: Any) -> JobConfig:
    return JobConfig.model_validate(make_job_config(**overrides))


@pytest.fixture
def registry(tmp_path: Path) -> ModelRegistry:
    price: dict[str, object] = {
        "input_price_per_mtok": 1,
        "output_price_per_mtok": 2,
        "context_window": 1000,
    }
    return load_registry([write_registry(tmp_path, "mistral", {"m-fast": price, "m-smart": price})])


def _handler(request: httpx2.Request) -> httpx2.Response:
    if request.url.path == "/feed.xml":
        return httpx2.Response(
            200, headers={"Content-Type": "application/rss+xml"}, content=FEED.encode()
        )
    if request.url.path == "/page":
        return httpx2.Response(
            200, headers={"Content-Type": "text/html"}, content=article_html("Page").encode()
        )
    return httpx2.Response(404)


async def test_settings_become_concurrency_lock_ttl_and_clock() -> None:
    settings = _settings(max_parallel_items=7, run_lock_seconds=300)

    async with default_deps(settings, transport=RecordingTransport(_handler)) as deps:
        assert deps.concurrency == 7
        assert deps.lock_ttl == timedelta(seconds=300)
        assert deps.next_run is compute_next_run
        assert abs(deps.clock().timestamp() - time.time()) < 5


async def test_a_missing_database_url_is_a_missing_setting() -> None:
    with pytest.raises(MissingSettingError):
        async with default_deps(Settings(_env_file=None)):
            pass  # pragma: no cover


class _RecordingYoutube:
    """Stands in for ``YoutubeSource``: records its construction and the configs it fetches."""

    instances: ClassVar[list["_RecordingYoutube"]] = []

    def __init__(self, **kwargs: Any) -> None:
        self.kwargs = kwargs
        self.fetched: list[Any] = []
        self.closed = 0
        _RecordingYoutube.instances.append(self)

    from_settings = classmethod(YoutubeSource.from_settings.__func__)  # type: ignore[attr-defined]

    async def fetch(self, config: Any) -> list[Any]:
        self.fetched.append(config)
        return []

    def close(self) -> None:
        self.closed += 1


@pytest.fixture
def recording_youtube(monkeypatch: pytest.MonkeyPatch) -> type[_RecordingYoutube]:
    _RecordingYoutube.instances = []
    monkeypatch.setattr(deps_module, "YoutubeSource", _RecordingYoutube)
    return _RecordingYoutube


@pytest.mark.parametrize(
    "source",
    [
        {"type": "youtube_channel", "channel_id": "UC1"},
        {"type": "youtube_playlist", "playlist_id": "PL1"},
    ],
)
async def test_fetch_source_routes_both_youtube_types_to_one_adapter(
    recording_youtube: type[_RecordingYoutube], source: dict[str, str]
) -> None:
    config = JobConfig.model_validate(make_job_config(sources=[source])).sources[0]

    async with default_deps(_settings(), transport=RecordingTransport(_handler)) as deps:
        assert await deps.fetch_source(config) == []

    (adapter,) = recording_youtube.instances
    assert adapter.fetched == [config]
    assert adapter.closed == 1  # its worker pool is shut down with the deps


async def test_youtube_settings_reach_the_adapter(
    recording_youtube: type[_RecordingYoutube], tmp_path: Path
) -> None:
    cookies = tmp_path / "cookies.txt"
    settings = _settings(
        youtube_cookies_file=cookies,
        youtube_proxy="http://u:secret@proxy.invalid:8080",
        youtube_timeout_seconds=12.5,
    )

    async with default_deps(settings, transport=RecordingTransport(_handler)):
        pass

    (adapter,) = recording_youtube.instances
    assert adapter.kwargs == {
        "cookies_file": cookies,
        "proxy": "http://u:secret@proxy.invalid:8080",
        "timeout": 12.5,
    }


@pytest.mark.parametrize("cookies", ["", "  "])
async def test_blank_youtube_cookies_and_proxy_count_as_unset(
    recording_youtube: type[_RecordingYoutube], monkeypatch: pytest.MonkeyPatch, cookies: str
) -> None:
    monkeypatch.setenv("INVIO_YOUTUBE_COOKIES_FILE", cookies)
    monkeypatch.setenv("INVIO_YOUTUBE_PROXY", "")

    async with default_deps(_settings(), transport=RecordingTransport(_handler)):
        pass

    (adapter,) = recording_youtube.instances
    assert adapter.kwargs["cookies_file"] is None
    assert adapter.kwargs["proxy"] is None


async def test_fetch_source_dispatches_rss_and_web() -> None:
    config = _config()
    rss = config.sources[0]
    web = JobConfig.model_validate(
        make_job_config(sources=[{"type": "web", "url": "https://example.com/page"}])
    ).sources[0]
    rss_config = JobConfig.model_validate(
        make_job_config(sources=[{"type": "rss", "url": "https://example.com/feed.xml"}])
    ).sources[0]
    assert rss.type == "rss"

    async with default_deps(
        _settings(), transport=RecordingTransport(_handler), resolver=RESOLVER
    ) as deps:
        feed = await deps.fetch_source(rss_config)
        page = await deps.fetch_source(web)

    assert [c.url for c in feed] == ["https://example.com/post"]
    assert len(page) == 1


async def test_fetch_page_returns_decoded_html_without_validators() -> None:
    transport = RecordingTransport(_handler)

    async with default_deps(_settings(), transport=transport, resolver=RESOLVER) as deps:
        html = await deps.fetch_page("https://example.com/page")

    assert "Page paragraph 1" in html
    assert "if-none-match" not in transport.requests[0].headers


async def test_fetch_page_treats_not_modified_as_a_bug(monkeypatch: pytest.MonkeyPatch) -> None:
    async def not_modified(self: SafeHttpClient, url: str, **kwargs: Any) -> NotModified:
        return NotModified(url=url, etag=None, last_modified=None)

    monkeypatch.setattr(SafeHttpClient, "get", not_modified)

    async with default_deps(_settings(), transport=RecordingTransport(_handler)) as deps:
        with pytest.raises(RuntimeError, match="NotModified"):
            await deps.fetch_page("https://example.com/page")


async def test_provider_for_binds_models_and_closes_providers_on_exit(
    monkeypatch: pytest.MonkeyPatch, registry: ModelRegistry
) -> None:
    closed: list[str] = []

    class Closing(FakeProvider):
        async def aclose(self) -> None:
            closed.append("closed")

    monkeypatch.setattr(deps_module, "get_provider", lambda name, settings, **kw: Closing([]))
    config = _config(llm={"provider": "mistral", "models": {"fast": "m-fast", "smart": "m-smart"}})

    async with default_deps(
        _settings(), registry=registry, transport=RecordingTransport(_handler)
    ) as deps:
        binding = deps.provider_for(config.llm)
        assert (binding.fast.provider_name, binding.fast.model) == ("mistral", "m-fast")
        assert (binding.smart.provider_name, binding.smart.model) == ("mistral", "m-smart")
        assert binding.fast.provider is binding.smart.provider  # one provider per name
        assert binding.fast.fallback is None and binding.smart.fallback is None
        assert binding.registry is registry
        assert closed == []

    assert closed == ["closed"]


async def test_provider_for_rejects_an_unregistered_smart_model(
    monkeypatch: pytest.MonkeyPatch, registry: ModelRegistry
) -> None:
    monkeypatch.setattr(deps_module, "get_provider", lambda name, settings, **kw: FakeProvider([]))
    config = _config(llm={"provider": "mistral", "models": {"fast": "m-fast", "smart": "nope"}})

    async with default_deps(
        _settings(), registry=registry, transport=RecordingTransport(_handler)
    ) as deps:
        with pytest.raises(LLMConfigError, match="nope"):
            deps.provider_for(config.llm)


async def test_notify_maps_the_delivery_outcome(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[int] = []

    async def fake_deliver(factory: Any, digest_id: int, *, settings: Settings) -> DeliveryOutcome:
        calls.append(digest_id)
        return DeliveryOutcome(
            sent=2, failed=1, skipped_empty=False, notification_ids=(1, 2, 3), error="smtp down"
        )

    monkeypatch.setattr(deps_module, "deliver_digest", fake_deliver)

    async with default_deps(_settings(), transport=RecordingTransport(_handler)) as deps:
        report = await deps.notify(42)

    assert calls == [42]
    assert (report.sent, report.failed, report.error) == (2, 1, "smtp down")


async def test_everything_is_closed_even_when_the_block_raises(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    disposed: list[bool] = []
    from sqlalchemy.engine import Engine

    original = Engine.dispose

    def tracking(self: Engine, close: bool = True) -> None:
        disposed.append(True)
        original(self, close)

    monkeypatch.setattr(Engine, "dispose", tracking)

    with pytest.raises(RuntimeError, match="boom"):
        async with default_deps(_settings(), transport=RecordingTransport(_handler)):
            raise RuntimeError("boom")

    assert disposed == [True]


async def test_every_provider_is_closed_even_when_one_close_fails(
    monkeypatch: pytest.MonkeyPatch, registry: ModelRegistry
) -> None:
    closed: list[str] = []

    class Failing(FakeProvider):
        async def aclose(self) -> None:
            closed.append("failing")
            raise OSError("socket")

    class Fine(FakeProvider):
        async def aclose(self) -> None:
            closed.append("fine")

    queue: list[FakeProvider] = [Failing([]), Fine([])]
    monkeypatch.setattr(deps_module, "get_provider", lambda name, settings, **kw: queue.pop(0))
    config = _config(llm={"provider": "mistral", "models": {"fast": "m-fast", "smart": "m-smart"}})

    async with default_deps(
        _settings(), registry=registry, transport=RecordingTransport(_handler)
    ) as deps:
        deps.provider_for(config.llm)
        deps.provider_for(config.llm)

    assert closed == ["failing", "fine"]


async def test_the_engine_is_disposed_when_building_the_client_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from sqlalchemy.engine import Engine

    disposed: list[bool] = []
    original = Engine.dispose

    def tracking(self: Engine, close: bool = True) -> None:
        disposed.append(True)
        original(self, close)

    def broken(*args: Any, **kwargs: Any) -> None:
        raise RuntimeError("no client")

    monkeypatch.setattr(Engine, "dispose", tracking)
    monkeypatch.setattr(deps_module, "SafeHttpClient", broken)

    with pytest.raises(RuntimeError, match="no client"):
        async with default_deps(_settings()):
            pass  # pragma: no cover

    assert disposed == [True]


# --- Per-role providers and fallback (#34) ----------------------------------------------------


@pytest.fixture
def two_providers(tmp_path: Path) -> ModelRegistry:
    price: dict[str, object] = {
        "input_price_per_mtok": 1,
        "output_price_per_mtok": 2,
        "context_window": 1000,
    }
    write_registry(tmp_path, "mistral", {"m-fast": price, "m-smart": price})
    return load_registry([write_registry(tmp_path, "openai", {"o-fast": price, "o-smart": price})])


def _named_providers(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """Replace ``get_provider`` with fakes named after the provider; return the names built."""
    built: list[str] = []

    def fake_get_provider(name: str, settings: Settings, **kw: Any) -> FakeProvider:
        built.append(name)
        return FakeProvider([], name=name)

    monkeypatch.setattr(deps_module, "get_provider", fake_get_provider)
    return built


async def test_provider_for_binds_each_role_and_fallback_once_per_provider(
    monkeypatch: pytest.MonkeyPatch, two_providers: ModelRegistry
) -> None:
    built = _named_providers(monkeypatch)
    config = _config(
        llm={
            "provider": "mistral",
            "models": {
                "fast": "m-fast",
                "smart": {
                    "provider": "openai",
                    "model": "o-smart",
                    "fallback": {"provider": "mistral", "model": "m-smart"},
                },
            },
            "fallback_provider": "openai",
            "fallback_models": {"fast": "o-fast", "smart": "o-fast"},
        }
    )

    async with default_deps(
        _settings(), registry=two_providers, transport=RecordingTransport(_handler)
    ) as deps:
        binding = deps.provider_for(config.llm)

    fast, smart = binding.fast, binding.smart
    assert fast.fallback is not None and smart.fallback is not None
    assert (fast.provider_name, fast.model) == ("mistral", "m-fast")
    assert (fast.fallback.provider_name, fast.fallback.model) == ("openai", "o-fast")
    assert (smart.provider_name, smart.model) == ("openai", "o-smart")
    assert (smart.fallback.provider_name, smart.fallback.model) == ("mistral", "m-smart")
    assert fast.provider is smart.fallback.provider
    assert smart.provider is fast.fallback.provider
    assert sorted(built) == ["mistral", "openai"]


async def test_provider_for_rejects_an_unregistered_fallback_model_before_building(
    monkeypatch: pytest.MonkeyPatch, two_providers: ModelRegistry
) -> None:
    built = _named_providers(monkeypatch)
    config = _config(
        llm={
            "provider": "mistral",
            "models": {"fast": "m-fast", "smart": "m-smart"},
            "fallback_provider": "openai",
            "fallback_models": {"fast": "o-fast", "smart": "m-smart"},  # not an openai model
        }
    )

    async with default_deps(
        _settings(), registry=two_providers, transport=RecordingTransport(_handler)
    ) as deps:
        with pytest.raises(LLMConfigError, match="'m-smart' is not registered for LLM provider"):
            deps.provider_for(config.llm)

    assert built == []


async def test_a_fallback_provider_without_models_warns_and_binds_no_fallback(
    monkeypatch: pytest.MonkeyPatch, registry: ModelRegistry, caplog: pytest.LogCaptureFixture
) -> None:
    built = _named_providers(monkeypatch)
    config = _config(
        llm={
            "provider": "mistral",
            "models": {"fast": "m-fast", "smart": "m-smart"},
            "fallback_provider": "openai",
        }
    )

    with caplog.at_level(logging.WARNING, logger="invio.pipeline"):
        async with default_deps(
            _settings(), registry=registry, transport=RecordingTransport(_handler)
        ) as deps:
            binding = deps.provider_for(config.llm)

    (record,) = [r for r in caplog.records if r.getMessage() == "llm.fallback_unconfigured"]
    assert record.__dict__["fallback_provider"] == "openai"
    assert record.__dict__["roles"] == ["fast", "smart"]
    assert binding.fast.fallback is None
    assert binding.smart.fallback is None
    assert built == ["mistral"]


async def test_no_warning_when_every_role_has_its_own_fallback(
    monkeypatch: pytest.MonkeyPatch, two_providers: ModelRegistry, caplog: pytest.LogCaptureFixture
) -> None:
    _named_providers(monkeypatch)
    own = {"provider": "openai", "model": "o-fast"}
    config = _config(
        llm={
            "provider": "mistral",
            "models": {
                "fast": {"model": "m-fast", "fallback": own},
                "smart": {"model": "m-smart", "fallback": own},
            },
            "fallback_provider": "openai",
        }
    )

    with caplog.at_level(logging.WARNING, logger="invio.pipeline"):
        async with default_deps(
            _settings(), registry=two_providers, transport=RecordingTransport(_handler)
        ) as deps:
            deps.provider_for(config.llm)

    assert "llm.fallback_unconfigured" not in [r.getMessage() for r in caplog.records]


async def test_unused_fallback_models_warn(
    monkeypatch: pytest.MonkeyPatch, two_providers: ModelRegistry, caplog: pytest.LogCaptureFixture
) -> None:
    _named_providers(monkeypatch)
    own = {"provider": "openai", "model": "o-fast"}
    config = _config(
        llm={
            "provider": "mistral",
            "models": {
                "fast": {"model": "m-fast", "fallback": own},
                "smart": {"model": "m-smart", "fallback": own},
            },
            "fallback_provider": "openai",
            "fallback_models": {"fast": "o-fast", "smart": "o-smart"},
        }
    )

    with caplog.at_level(logging.WARNING, logger="invio.pipeline"):
        async with default_deps(
            _settings(), registry=two_providers, transport=RecordingTransport(_handler)
        ) as deps:
            deps.provider_for(config.llm)

    messages = [r.getMessage() for r in caplog.records]
    (record,) = [r for r in caplog.records if r.getMessage() == "llm.fallback_models_unused"]
    assert record.__dict__["fallback_provider"] == "openai"
    assert "llm.fallback_unconfigured" not in messages


async def test_no_unused_warning_when_a_role_uses_fallback_models(
    monkeypatch: pytest.MonkeyPatch, two_providers: ModelRegistry, caplog: pytest.LogCaptureFixture
) -> None:
    _named_providers(monkeypatch)
    config = _config(
        llm={
            "provider": "mistral",
            "models": {
                "fast": {"model": "m-fast", "fallback": {"provider": "openai", "model": "o-fast"}},
                "smart": "m-smart",
            },
            "fallback_provider": "openai",
            "fallback_models": {"fast": "o-fast", "smart": "o-smart"},
        }
    )

    with caplog.at_level(logging.WARNING, logger="invio.pipeline"):
        async with default_deps(
            _settings(), registry=two_providers, transport=RecordingTransport(_handler)
        ) as deps:
            deps.provider_for(config.llm)

    assert "llm.fallback_models_unused" not in [r.getMessage() for r in caplog.records]


async def test_a_missing_fallback_api_key_fails_when_binding(two_providers: ModelRegistry) -> None:
    config = _config(
        llm={
            "provider": "mistral",
            "models": {"fast": "m-fast", "smart": "m-smart"},
            "fallback_provider": "openai",
            "fallback_models": {"fast": "o-fast", "smart": "o-smart"},
        }
    )
    settings = _settings(mistral_api_key="sk-test", openai_api_key=None)

    async with default_deps(
        settings, registry=two_providers, transport=RecordingTransport(_handler)
    ) as deps:
        with pytest.raises(LLMAuthError, match="INVIO_OPENAI_API_KEY"):
            deps.provider_for(config.llm)
