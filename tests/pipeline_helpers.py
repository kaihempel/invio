"""Shared helpers for the ``tests/test_pipeline_*.py`` and graph tests (#21).

* :class:`RoutedFakeProvider`: an ``LLMProvider`` whose answers are routed by purpose (told apart
  by the system prompt of the node that sent the request) and, per item, by title. Fan-out makes
  the order of requests nondeterministic, so a single scripted queue cannot serve a run.
* :class:`FakePorts`: fake source and page fetch ports plus a :class:`RecordingNotifier`.
* :func:`make_job_config`, :func:`store_job`, :func:`make_deps`, :func:`snapshot`.
"""

import asyncio
import copy
import json
import re
import time
from collections import Counter, deque
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from pydantic import BaseModel, HttpUrl
from sqlalchemy import Engine, inspect, select
from sqlalchemy.orm import Session, sessionmaker

from invio.config.job import JobConfig, LLMConfig, ScheduleConfig, SourceConfig
from invio.db.models import Digest, Item, Job, LlmUsage, Notification, Run
from invio.db.repositories import JobRepository, RunRepository
from invio.db.session import session_factory, session_scope
from invio.domain import Candidate, RunStatus
from invio.graph.nodes import relevance, summarize_item, synthesize
from invio.graph.ports import DeliveryReport, ProviderBinding, RunDeps
from invio.llm.base import LLMError, LLMProvider, Usage, structured_with_repair
from invio.llm.fake import FakeReply, FakeScriptExhaustedError, FakeStep
from invio.llm.registry import ModelRegistry, load_registry
from invio.retry import RetrySettings, Sleep
from tests.async_helpers import RecordingSleep
from tests.db_helpers import make_candidate
from tests.llm_helpers import FIXTURE_REGISTRY_DIR

ARTICLES_DIR = Path(__file__).parent / "fixtures" / "articles"
FAST_MODEL = "fakeco-free"
SMART_MODEL = "fakeco-priced"
PROVIDER_NAME = "fakeco"
DEFAULT_USAGE = Usage(10, 5)
DEFAULT_NEXT_RUN_AT = datetime(2026, 10, 4, 6, 0, tzinfo=UTC)  # before ``fake_clock()``

# --- Replies --------------------------------------------------------------------------------


def relevance_reply(score: float = 0.9, usage: Usage = DEFAULT_USAGE) -> FakeReply:
    """A valid relevance answer with ``score``."""
    text = json.dumps({"score": score, "reason": "matches", "key_points": ["a"]})
    return FakeReply(text, usage)


def summary_reply(usage: Usage = DEFAULT_USAGE) -> FakeReply:
    """A valid item summary answer."""
    text = json.dumps({"headline": "Head", "bullets": ["a", "b", "c"], "why_relevant": "Matters."})
    return FakeReply(text, usage)


def digest_reply(usage: Usage = DEFAULT_USAGE) -> FakeReply:
    """A digest answer that passes the synthesis checks (intro and one heading, no links)."""
    return FakeReply("An intro about what is new.\n\n## Highlights\n\nThe items below.", usage)


# --- Purpose-routed provider ------------------------------------------------------------------

_TITLE = re.compile(r"<title>(.*?)</title>", re.DOTALL)
Route = Callable[[str], FakeStep] | Sequence[FakeStep] | FakeStep


def _system_prefix(system: str) -> str:
    return system.split("<interest>")[0]


def _purpose_prefixes() -> dict[str, str]:
    summary = summarize_item.ItemSummary(headline="h", bullets=["a", "b", "c"], why_relevant="w")
    entry = synthesize.DigestEntry(
        item_id=1,
        url="https://example.com/x",
        title="t",
        relevance=None,
        published_at=None,
        summary=summary,
    )
    prefixes = {
        _system_prefix(relevance.build_messages("t", None, None, "x")[0]): "relevance",
        _system_prefix(synthesize.build_messages([entry], interest="x", language="en")[0]): (
            "synthesize"
        ),
    }
    for kind in ("short", "chunk", "combine"):
        system = summarize_item.build_messages(
            kind,
            title="t",
            content="c",
            interest="x",
            language="en",
            part=1,
            parts=2,
        )[0]
        prefixes[_system_prefix(system)] = "summarize"
    return prefixes


@dataclass(frozen=True, slots=True)
class ProviderCall:
    """One recorded provider request."""

    purpose: str
    title: str
    model: str
    user: str
    at: float


class RoutedFakeProvider:
    """An ``LLMProvider`` that answers by purpose (``relevance``, ``summarize``, ``synthesize``).

    Each route is a queue of steps (consumed in order), a callable ``title -> step`` (keyed by the
    item title in the user message) or one step that repeats. A step is a ``FakeReply`` or an
    ``LLMError`` to raise (any other exception instance is raised too, to inject bugs). ``hold``
    yields to the event loop that many times inside every request so concurrency is observable.
    """

    def __init__(
        self,
        *,
        relevance: Route | None = None,
        summarize: Route | None = None,
        synthesize: Route | None = None,
        hold: int = 0,
        trace: list[str] | None = None,
    ) -> None:
        self._routes: dict[str, Route] = {
            "relevance": relevance if relevance is not None else relevance_reply(),
            "summarize": summarize if summarize is not None else summary_reply(),
            "synthesize": synthesize if synthesize is not None else digest_reply(),
        }
        self._queues: dict[str, deque[FakeStep]] = {
            name: deque(route)
            for name, route in self._routes.items()
            if isinstance(route, Sequence)
        }
        self._prefixes = _purpose_prefixes()
        self._hold = hold
        self.trace = trace if trace is not None else []
        self.calls: list[ProviderCall] = []
        self.in_flight = 0
        self.max_in_flight = 0
        self.closed = False

    def route(self, purpose: str, route: Route) -> None:
        """Replace the route of ``purpose`` (``relevance``, ``summarize`` or ``synthesize``)."""
        if purpose not in self._routes:
            raise KeyError(purpose)
        self._routes[purpose] = route
        if isinstance(route, Sequence):
            self._queues[purpose] = deque(route)
        else:
            self._queues.pop(purpose, None)

    def requests_for(self, purpose: str) -> list[ProviderCall]:
        """The recorded requests of one purpose, in arrival order."""
        return [call for call in self.calls if call.purpose == purpose]

    def titles_for(self, purpose: str) -> Counter[str]:
        """How many requests each item title got for ``purpose``."""
        return Counter(call.title for call in self.requests_for(purpose))

    def _purpose(self, system: str) -> str:
        return self._prefixes.get(_system_prefix(system), "unknown")

    def _step(self, purpose: str, title: str) -> FakeStep:
        route = self._routes[purpose]
        if isinstance(route, Sequence):
            queue = self._queues[purpose]
            if not queue:
                raise FakeScriptExhaustedError(f"{purpose} route exhausted")
            return queue.popleft()
        if callable(route):
            return route(title)
        return route

    async def _request(
        self, system: str, user: str, *, model: str, temperature: float, max_tokens: int | None
    ) -> tuple[str, Usage]:
        purpose = self._purpose(system)
        match = _TITLE.search(user)
        title = match.group(1) if match else ""
        self.calls.append(ProviderCall(purpose, title, model, user, time.monotonic()))
        self.trace.append(purpose)
        self.in_flight += 1
        self.max_in_flight = max(self.max_in_flight, self.in_flight)
        try:
            for _ in range(self._hold):
                await asyncio.sleep(0)
            step = self._step(purpose, title)
        finally:
            self.in_flight -= 1
        if isinstance(step, BaseException):
            raise step
        assert isinstance(step, FakeReply)
        return step.text, step.usage

    async def complete(
        self, system: str, user: str, *, model: str, temperature: float, max_tokens: int
    ) -> tuple[str, Usage]:
        return await self._request(
            system, user, model=model, temperature=temperature, max_tokens=max_tokens
        )

    async def complete_structured[T: BaseModel](
        self, system: str, user: str, schema: type[T], *, model: str, temperature: float
    ) -> tuple[T, Usage]:
        async def request(system_text: str, user_text: str) -> tuple[str, Usage]:
            return await self._request(
                system_text, user_text, model=model, temperature=temperature, max_tokens=None
            )

        return await structured_with_repair(
            request, system, user, schema, provider=PROVIDER_NAME, model=model
        )

    async def aclose(self) -> None:
        self.closed = True


# --- Fake ports -------------------------------------------------------------------------------


class Attempts:
    """Per-attempt outcomes of a port call; the last outcome repeats."""

    def __init__(self, *outcomes: Any) -> None:
        if not outcomes:
            raise ValueError("Attempts needs at least one outcome")
        self._outcomes = deque(outcomes)

    def next(self) -> Any:
        if len(self._outcomes) > 1:
            return self._outcomes.popleft()
        return self._outcomes[0]


def _resolve(behaviour: Any) -> Any:
    return behaviour.next() if isinstance(behaviour, Attempts) else behaviour


class RecordingNotifier:
    """A ``Notifier`` that records digest ids and answers with a configurable report."""

    def __init__(
        self, report: DeliveryReport | None = None, *, trace: list[str] | None = None
    ) -> None:
        self.report = report or DeliveryReport(sent=1, failed=0)
        self.trace = trace if trace is not None else []
        self.calls: list[int] = []

    async def __call__(self, digest_id: int) -> DeliveryReport:
        self.calls.append(digest_id)
        self.trace.append("notify")
        return self.report


async def settle(turns: int = 100) -> None:
    """Yield to the event loop ``turns`` times so pending coroutines run, without a real sleep."""
    for _ in range(turns):
        await asyncio.sleep(0)


def source_key(url: str) -> str:
    """The key :class:`FakePorts` uses for a source URL (normalised like ``HttpUrl``)."""
    return str(HttpUrl(url))


def article_html(title: str = "Article", paragraphs: int = 4) -> str:
    """An article page whose extracted text is longer than ``extract_text``'s ``min_chars``."""
    body = "".join(
        f"<p>{title} paragraph {n}: agent frameworks keep improving, and teams report that "
        "better tooling reduces failures in production deployments of long running agents.</p>"
        for n in range(1, paragraphs + 1)
    )
    return (
        f'<html lang="en"><head><title>{title}</title></head>'
        f"<body><article><h1>{title}</h1>{body}</article></body></html>"
    )


def fixture_article() -> str:
    """The HTML of ``tests/fixtures/articles/news_article.html``."""
    return (ARTICLES_DIR / "news_article.html").read_text(encoding="utf-8")


class FakePorts:
    """Fake ``fetch_source`` and ``fetch_page`` ports, and a recording notifier.

    ``sources`` maps a source URL to candidates, an exception or :class:`Attempts`; ``pages``
    maps an item URL to HTML, an exception or :class:`Attempts` (unknown pages are an article).
    ``gate`` blocks every page fetch until set; ``hold`` yields to the event loop that often
    inside each fetch, which makes the in-flight counters meaningful.
    """

    def __init__(
        self,
        sources: Mapping[str, Any] | None = None,
        pages: Mapping[str, Any] | None = None,
        *,
        notifier: RecordingNotifier | None = None,
        gate: asyncio.Event | None = None,
        hold: int = 0,
        trace: list[str] | None = None,
    ) -> None:
        self.trace = trace if trace is not None else []
        self.sources = {source_key(url): outcome for url, outcome in (sources or {}).items()}
        self.pages = dict(pages or {})
        self.notifier = notifier or RecordingNotifier()
        self.notifier.trace = self.trace
        self.gate = gate
        self.hold = hold
        self.calls: list[tuple[str, str]] = []  # ("source" | "page", url) in arrival order
        self.page_calls: Counter[str] = Counter()
        self.entered = asyncio.Event()  # set when the first page fetch starts
        self.in_flight = 0
        self.max_in_flight = 0
        self.source_in_flight = 0
        self.max_source_in_flight = 0
        self._in_flight_watchers: list[tuple[int, asyncio.Event]] = []

    def in_flight_reached(self, count: int) -> asyncio.Event:
        """An event set as soon as ``count`` page fetches are in flight at the same time.

        With ``gate`` unset, every page fetch blocks, so a test can await this event instead of
        relying on the event loop's scheduling to make the fetches overlap.
        """
        event = asyncio.Event()
        self._in_flight_watchers.append((count, event))
        return event

    async def fetch_source(self, config: SourceConfig) -> list[Candidate]:
        url = str(getattr(config, "url"))  # noqa: B009 - not every source type has a url
        self.calls.append(("source", url))
        self.trace.append("source")
        self.source_in_flight += 1
        self.max_source_in_flight = max(self.max_source_in_flight, self.source_in_flight)
        try:
            for _ in range(self.hold):
                await asyncio.sleep(0)
            outcome = _resolve(self.sources.get(url, []))
        finally:
            self.source_in_flight -= 1
        if isinstance(outcome, BaseException):
            raise outcome
        candidates: list[Candidate] = outcome
        return list(candidates)

    async def fetch_page(self, url: str) -> str:
        self.calls.append(("page", url))
        self.trace.append("page")
        self.page_calls[url] += 1
        self.entered.set()
        self.in_flight += 1
        self.max_in_flight = max(self.max_in_flight, self.in_flight)
        for count, event in self._in_flight_watchers:
            if self.in_flight >= count:
                event.set()
        try:
            if self.gate is not None:
                await self.gate.wait()
            for _ in range(self.hold):
                await asyncio.sleep(0)
            outcome = _resolve(self.pages.get(url))
        finally:
            self.in_flight -= 1
        if isinstance(outcome, BaseException):
            raise outcome
        if outcome is None:
            return article_html()
        html: str = outcome
        return html


# --- Jobs and dependencies --------------------------------------------------------------------


def make_candidates(count: int, *, prefix: str = "https://example.com/post") -> list[Candidate]:
    """``count`` article candidates titled ``Article 1`` ..., newest first."""
    base = datetime(2026, 10, 1, tzinfo=UTC)
    return [
        make_candidate(
            f"{prefix}-{n}",
            title=f"Article {n}",
            published_at=base - timedelta(hours=n),
            teaser=f"Teaser {n}",
            content_hash=f"{n:064d}",
        )
        for n in range(1, count + 1)
    ]


def _base_job_data() -> dict[str, Any]:
    return {
        "language": "en",
        "schedule": {
            "frequency": "weekly",
            "time": "07:30",
            "weekday": "monday",
            "timezone": "Europe/Berlin",
        },
        "notification": {"to": ["research@example.com"], "subject": "invio digest"},
        "sources": [{"type": "rss", "url": "https://example.com/feed.xml"}],
        "search": {"semantic_description": "LLM agent frameworks"},
        "llm": {
            "provider": "mistral",
            "models": {"fast": FAST_MODEL, "smart": SMART_MODEL},
        },
    }


def make_job_config(**overrides: Any) -> dict[str, Any]:
    """The raw mapping of a valid job (RSS source, ``language="en"``).

    A key containing ``__`` sets a nested value: ``search__min_relevance=2``. Any other key
    replaces the top-level section. The mapping is returned unvalidated so a test can break it.
    """
    data = _base_job_data()
    for key, value in overrides.items():
        *parents, last = key.split("__")
        node = data
        for part in parents:
            node = node.setdefault(part, {})
        node[last] = copy.deepcopy(value)
    return data


def store_job(
    factory: sessionmaker[Session],
    config: Mapping[str, Any] | JobConfig,
    *,
    name: str = "research",
    enabled: bool = True,
    next_run_at: datetime | None = DEFAULT_NEXT_RUN_AT,
    locked_until: datetime | None = None,
) -> int:
    """Commit a job row (``config`` may be invalid on purpose) and return its id."""
    data = config.model_dump(mode="json") if isinstance(config, JobConfig) else dict(config)
    with session_scope(factory) as session:
        job = JobRepository(session).add(
            Job(
                name=name,
                enabled=enabled,
                config=data,
                next_run_at=next_run_at,
                locked_until=locked_until,
            )
        )
        return job.id


def fixture_registry() -> ModelRegistry:
    """The registry of ``tests/fixtures/llm/models.d`` (``fakeco-priced``, ``fakeco-free``)."""
    return load_registry([FIXTURE_REGISTRY_DIR])


def plus_one_hour(schedule: ScheduleConfig, after: datetime) -> datetime:
    """Default ``next_run`` of :func:`make_deps`."""
    return after + timedelta(hours=1)


def make_deps(
    factory: sessionmaker[Session],
    ports: FakePorts,
    provider: LLMProvider,
    *,
    clock: Callable[[], datetime],
    concurrency: int = 4,
    retry: RetrySettings | None = None,
    sleep: Sleep | None = None,
    next_run: Callable[[ScheduleConfig, datetime], datetime] = plus_one_hour,
    provider_for: Callable[[LLMConfig], ProviderBinding] | None = None,
    lock_ttl: timedelta = timedelta(hours=2),
) -> RunDeps:
    """``RunDeps`` over the fake ports and ``provider`` (fixture registry, no waiting)."""
    registry = fixture_registry()

    def bind(config: LLMConfig) -> ProviderBinding:
        return ProviderBinding(
            provider=provider,
            provider_name=PROVIDER_NAME,
            fast_model=config.models.fast,
            smart_model=config.models.smart,
            registry=registry,
        )

    return RunDeps(
        session_factory=factory,
        fetch_source=ports.fetch_source,
        fetch_page=ports.fetch_page,
        provider_for=provider_for or bind,
        notify=ports.notifier,
        next_run=next_run,
        clock=clock,
        concurrency=concurrency,
        retry=retry or RetrySettings(jitter=False),
        lock_ttl=lock_ttl,
        sleep=sleep or RecordingSleep(),
    )


@dataclass
class Env:
    """A stored job plus the fakes a run needs; ``trace`` records the call order of all of them."""

    factory: sessionmaker[Session]
    job_id: int
    ports: FakePorts
    provider: RoutedFakeProvider
    deps: RunDeps
    sleep: RecordingSleep
    config: dict[str, Any]
    trace: list[str]


def build_env(
    engine: Engine,
    clock: Callable[[], datetime],
    next_run: Callable[[ScheduleConfig, datetime], datetime] = plus_one_hour,
    *,
    config: Mapping[str, Any] | None = None,
    candidates: Sequence[Candidate] | None = None,
    sources: Mapping[str, Any] | None = None,
    pages: Mapping[str, Any] | None = None,
    provider: RoutedFakeProvider | None = None,
    ports: FakePorts | None = None,
    concurrency: int = 4,
    retry: RetrySettings | None = None,
    enabled: bool = True,
    locked_until: datetime | None = None,
    next_run_at: datetime | None = DEFAULT_NEXT_RUN_AT,
    provider_for: Callable[[LLMConfig], ProviderBinding] | None = None,
) -> Env:
    """Store a job and wire fake ports, a routed provider and ``RunDeps`` around it.

    By default the job has one RSS source at ``https://example.com/feed.xml`` reporting three
    candidates; ``candidates`` replaces them and ``sources`` replaces the whole source map.
    """
    job_config = dict(config) if config is not None else make_job_config()
    if sources is None:
        sources = {
            "https://example.com/feed.xml": list(
                candidates if candidates is not None else make_candidates(3)
            )
        }
    ports = ports or FakePorts(sources, pages)
    trace = ports.trace
    provider = provider or RoutedFakeProvider()
    provider.trace = trace
    factory = session_factory(engine)
    job_id = store_job(
        factory, job_config, enabled=enabled, locked_until=locked_until, next_run_at=next_run_at
    )
    sleep = RecordingSleep()
    deps = make_deps(
        factory,
        ports,
        provider,
        clock=clock,
        concurrency=concurrency,
        retry=retry,
        sleep=sleep,
        next_run=next_run,
        provider_for=provider_for,
    )
    return Env(factory, job_id, ports, provider, deps, sleep, job_config, trace)


# --- Snapshots --------------------------------------------------------------------------------


def _rows(session: Session, model: type[Any], job_id: int) -> list[dict[str, Any]]:
    columns = [attr.key for attr in inspect(model).column_attrs]
    rows = session.query(model).filter(model.job_id == job_id).order_by(model.id).all()
    return [{name: getattr(row, name) for name in columns} for row in rows]


def snapshot(factory: sessionmaker[Session], job_id: int) -> dict[str, Any]:
    """Everything a dry run must leave alone: items, digests, usage, notifications, job times."""
    with session_scope(factory) as session:
        job = session.get(Job, job_id)
        assert job is not None
        return {
            "items": _rows(session, Item, job_id),
            "digests": _rows(session, Digest, job_id),
            "llm_usage": _rows(session, LlmUsage, job_id),
            "notifications": _rows(session, Notification, job_id),
            "next_run_at": job.next_run_at,
            "locked_until": job.locked_until,
        }


__all__ = [
    "Attempts",
    "Env",
    "FakePorts",
    "LLMError",
    "RecordingNotifier",
    "RecordingSleep",
    "RoutedFakeProvider",
    "article_html",
    "build_env",
    "digest_reply",
    "fixture_article",
    "make_candidates",
    "make_deps",
    "make_job_config",
    "relevance_reply",
    "snapshot",
    "store_job",
    "stored_runs",
    "summary_reply",
]


def stored_runs(factory: sessionmaker[Session], job_id: int | None = None) -> list[Run]:
    """The stored runs (of ``job_id``, if given), newest first, detached from the session."""
    stmt = select(Run).order_by(Run.started_at.desc(), Run.id.desc())
    if job_id is not None:
        stmt = stmt.where(Run.job_id == job_id)
    with session_scope(factory) as session:
        rows = list(session.scalars(stmt))
        session.expunge_all()
        return rows


def add_run(
    factory: sessionmaker[Session],
    job_id: int,
    status: RunStatus = RunStatus.SUCCEEDED,
    *,
    stats: dict[str, Any] | None = None,
    error: str | None = None,
    started_at: datetime | None = None,
    duration: timedelta | None = None,
) -> int:
    """Store a run of ``job_id``; any status but ``running`` is finished, ``duration`` after
    its start (default now)."""
    with session_scope(factory) as session:
        runs = RunRepository(session)
        run = runs.start(job_id, started_at=started_at)
        if status is not RunStatus.RUNNING:
            finished_at = run.started_at + duration if duration is not None else None
            runs.finish(run, status, stats=stats, error=error, finished_at=finished_at)
        return run.id
