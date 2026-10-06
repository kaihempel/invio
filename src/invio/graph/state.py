"""State of the research graph: run state, item state, errors and the item fan-out payload.

The state holds plain values only (ids and frozen dataclasses), never ORM objects: nodes load
``Item`` rows from the run's work session by id (research R7). ``items`` and ``errors`` use an
``operator.add`` reducer so the parallel ``Send`` branches merge without losing entries; the
merge order is unspecified, consumers must not depend on it.
"""

import operator
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Annotated, Literal, Required, TypedDict, get_args

from invio.config.job import JobConfig
from invio.domain import Candidate, ItemType, RunStatus
from invio.graph.nodes.persist import DigestDraft, StageCounts
from invio.graph.nodes.relevance import RelevanceOutcome
from invio.graph.nodes.summarize_item import SummaryOutcome

__all__ = [
    "STAGE_ORDER",
    "ItemResult",
    "ItemState",
    "ItemTask",
    "ItemUpdate",
    "RunError",
    "RunStage",
    "RunState",
    "error_of",
    "keep_first",
]

RunStage = Literal[
    "load_job",
    "fetch_sources",
    "deduplicate",
    "keyword_prefilter",
    "extract_text",
    "score_relevance",
    "summarize_item",
    "synthesize_digest",
    "persist",
    "notify",
    "finalize",
]

STAGE_ORDER: tuple[RunStage, ...] = get_args(RunStage)  # execution order, used to sort errors


@dataclass(frozen=True, slots=True, kw_only=True)
class RunError:
    """One recorded error. Holds the error class only, never provider, database or document text.

    ``item_id`` is set for item-level errors and ``source`` (the key ``"<index>:<type>"``,
    never the URL) for source errors; at most one of the two.
    """

    stage: RunStage
    error_class: str
    item_id: int | None = None
    source: str | None = None

    def __post_init__(self) -> None:
        if self.item_id is not None and self.source is not None:
            raise ValueError("a RunError names an item or a source, not both")
        if not self.error_class.strip():
            raise ValueError("error_class must not be blank")


def error_of(
    stage: RunStage,
    err: BaseException,
    *,
    item_id: int | None = None,
    source: str | None = None,
) -> RunError:
    """Return the :class:`RunError` for ``err``; only ``type(err).__name__`` is kept."""
    return RunError(stage=stage, error_class=type(err).__name__, item_id=item_id, source=source)


@dataclass(frozen=True, slots=True)
class ItemTask:
    """The ``Send`` payload of the fan-out: which item to process and how."""

    item_id: int
    kind: ItemType


@dataclass(frozen=True, slots=True, kw_only=True)
class ItemResult:
    """What processing one item produced (appended to ``RunState.items``)."""

    item_id: int
    relevance: RelevanceOutcome | None = None
    summary: SummaryOutcome | None = None


class ItemState(TypedDict, total=False):
    """State of the ``process_item`` subgraph.

    Each node records its own name in ``stage`` on entry, so the error boundary can name the
    node that raised.
    """

    item_id: Required[int]
    kind: Required[ItemType]
    stage: RunStage
    relevance: RelevanceOutcome | None
    summary: SummaryOutcome | None


class ItemUpdate(TypedDict, total=False):
    """A partial update of :class:`ItemState`, what the nodes of the subgraph return."""

    stage: RunStage
    relevance: RelevanceOutcome | None
    summary: SummaryOutcome | None


def keep_first[T](current: T | None, new: T | None) -> T | None:
    """Reducer: the first value written wins (the first fatal error of a run is kept)."""
    return current if current is not None else new


class RunState(TypedDict, total=False):
    """State of one run. Only ``items``, ``errors`` and ``fatal`` merge; others: last write wins."""

    job_id: int
    run_id: int
    dry_run: bool
    config: JobConfig
    sources_total: int
    sources_failed: int
    candidates: Mapping[str, list[Candidate]]  # key = source key
    counts: StageCounts
    selected: list[int]  # item ids that passed the prefilter
    taken: list[int]  # item ids deduplication took
    items: Annotated[list[ItemResult], operator.add]
    errors: Annotated[list[RunError], operator.add]
    digest: DigestDraft | None
    digest_id: int | None
    status: RunStatus | None
    fatal: Annotated[RunError | None, keep_first]  # set by a guarded stage that raised
