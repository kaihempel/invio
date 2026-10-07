"""The per-run error list stored in ``runs.stats["errors"]`` (research R1-R3).

:func:`stored_errors` is pure. It turns the run's :class:`~invio.graph.state.RunError` records
(classes only) into entries a person can act on: the sanitized message of the failed outcome and
the item's title and URL. Messages never carry provider, database or document text: they are the
already sanitized outcome texts, ``sanitized_error`` for the fatal error, and
``"<Class>: source failed"`` for a source (the source URL is never stored).
"""

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Final

from invio.domain import ItemStatus
from invio.graph.nodes.persist import sanitized_error
from invio.graph.nodes.relevance import RelevanceOutcome
from invio.graph.nodes.summarize_item import SummaryOutcome
from invio.graph.scope import ItemRef
from invio.graph.state import STAGE_ORDER, ItemResult, RunError, RunStage

__all__ = [
    "MAX_MESSAGE_CHARS",
    "MAX_STORED_ERRORS",
    "MAX_TITLE_CHARS",
    "StoredError",
    "stored_errors",
]

MAX_STORED_ERRORS: Final = 100
MAX_MESSAGE_CHARS: Final = 500
MAX_TITLE_CHARS: Final = 300


@dataclass(frozen=True, slots=True, kw_only=True)
class StoredError:
    """One entry of ``runs.stats["errors"]``; ``item_id`` and ``source`` are exclusive."""

    stage: RunStage
    error_class: str
    message: str
    item_id: int | None = None
    title: str | None = None
    url: str | None = None
    source: str | None = None

    def __post_init__(self) -> None:
        if not self.error_class.strip():
            raise ValueError("error_class must not be blank")
        if self.item_id is not None and self.source is not None:
            raise ValueError("a StoredError names an item or a source, not both")

    def to_json(self) -> dict[str, Any]:
        """The JSON object of the entry, without the fields that are ``None``."""
        fields: dict[str, Any] = {
            "stage": self.stage,
            "error_class": self.error_class,
            "message": self.message,
            "item_id": self.item_id,
            "title": self.title,
            "url": self.url,
            "source": self.source,
        }
        return {key: value for key, value in fields.items() if value is not None}


def _sort_key(error: RunError) -> tuple[int, int, str]:
    item = error.item_id if error.item_id is not None else -1
    return STAGE_ORDER.index(error.stage), item, error.source or ""


def _outcome_message(error: RunError, by_item: Mapping[int, ItemResult]) -> str | None:
    """The sanitized error text of the failed outcome ``error`` belongs to, if there is one."""
    result = by_item.get(error.item_id) if error.item_id is not None else None
    if result is None:
        return None
    outcome: RelevanceOutcome | SummaryOutcome | None
    if error.stage == "summarize_item":
        outcome = result.summary
    elif error.stage in ("extract_text", "score_relevance"):
        outcome = result.relevance
    else:
        return None
    if outcome is not None and outcome.status == ItemStatus.FAILED and outcome.error:
        return outcome.error
    return None


def stored_errors(
    errors: Sequence[RunError],
    items: Sequence[ItemResult],
    refs: Mapping[int, ItemRef],
    failure: BaseException | None,
) -> tuple[list[StoredError], int]:
    """Return ``(entries, omitted)``: sorted by stage order then item, de-duplicated, capped.

    ``failure`` is the original exception of the first fatal error; the first error (in sorted
    order) whose class equals its type takes its sanitized text.
    """
    by_item = {result.item_id: result for result in items}
    ordered = sorted(errors, key=_sort_key)
    fatal_class = type(failure).__name__ if failure is not None else None
    fatal_used = False
    entries: list[StoredError] = []
    for error in ordered:
        if failure is not None and not fatal_used and error.error_class == fatal_class:
            message = sanitized_error(failure)
            fatal_used = True
        elif error.source is not None:
            message = f"{error.error_class}: source failed"
        else:
            message = _outcome_message(error, by_item) or error.error_class
        ref = refs.get(error.item_id) if error.item_id is not None else None
        entries.append(
            StoredError(
                stage=error.stage,
                error_class=error.error_class,
                message=message[:MAX_MESSAGE_CHARS],
                item_id=error.item_id,
                title=ref.title[:MAX_TITLE_CHARS] if ref is not None else None,
                url=ref.url if ref is not None else None,
                source=error.source,
            )
        )
    unique = list(dict.fromkeys(entries))
    return unique[:MAX_STORED_ERRORS], max(0, len(unique) - MAX_STORED_ERRORS)
