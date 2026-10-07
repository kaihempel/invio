"""Progress display of ``invio job run``: a live panel on a terminal, plain lines otherwise.

Everything goes to stderr, so stdout stays a clean document. Item titles and messages are
untrusted: control characters and ANSI escapes are stripped, and everything is printed as plain
``Text``, never as markup.
"""

from types import TracebackType
from typing import Self

import typer
from rich.console import Console, Group
from rich.live import Live
from rich.text import Text

from invio.pipeline import ProgressEvent, ProgressSnapshot
from invio.textsafe import strip_control

__all__ = ["RunProgressView"]

# Stage events that come before any item event; every later stage ends the item phase.
_BEFORE_ITEMS = frozenset({"load_job", "fetch_sources", "deduplicate", "keyword_prefilter"})


def _counts_line(counts: ProgressSnapshot) -> str:
    return (
        f"found {counts.found} · new {counts.new} · after filter {counts.after_keyword_filter}"
        f" · relevant {counts.relevant} · failed {counts.failed}"
    )


def _stage_line(event: ProgressEvent) -> str:
    counts = event.counts
    match event.stage:
        case "deduplicate":
            return f"progress: deduplicate found={counts.found} new={counts.new}"
        case "keyword_prefilter":
            return f"progress: keyword_prefilter after_keyword_filter={counts.after_keyword_filter}"
        case stage:
            return f"progress: {stage}"


def _items_line(counts: ProgressSnapshot) -> str:
    return (
        f"progress: items processed={counts.processed}/{counts.selected}"
        f" relevant={counts.relevant} failed={counts.failed}"
    )


def _item_line(event: ProgressEvent) -> str:
    line = f"item: {event.outcome}  {strip_control(event.title or '')}"
    if event.outcome == "failed" and event.message:
        line += f"  {strip_control(event.message)}"
    return line


class RunProgressView:
    """A ``RunObserver``. Use it as a context manager so the live panel is always stopped."""

    def __init__(self, console: Console, *, verbose: bool = False) -> None:
        self._console = console
        self._verbose = verbose
        self._live: Live | None = None
        self._stage = "starting"
        self._counts = ProgressSnapshot()
        self._last_items_line: str | None = None

    # --- lifecycle ------------------------------------------------------------------------

    def start(self) -> None:
        if self._console.is_terminal and self._live is None:
            self._live = Live(
                self._render(), console=self._console, transient=True, refresh_per_second=8
            )
            self._live.start()

    def stop(self) -> None:
        live, self._live = self._live, None
        if live is not None:
            live.stop()

    def __enter__(self) -> Self:
        self.start()
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        self.stop()

    # --- observer -------------------------------------------------------------------------

    def __call__(self, event: ProgressEvent) -> None:
        self._counts = event.counts
        if event.kind == "stage":
            self._stage = event.stage
        if self._live is not None:
            self._on_terminal(event)
        else:
            self._on_pipe(event)

    def _render(self) -> Group:
        lines = [Text(f"stage: {self._stage}"), Text(_counts_line(self._counts))]
        if self._counts.selected > 0:
            lines.append(Text(f"processed {self._counts.processed}/{self._counts.selected}"))
        return Group(*lines)

    def _on_terminal(self, event: ProgressEvent) -> None:
        assert self._live is not None
        if event.kind == "item" and self._verbose:
            self._live.console.print(Text(_item_line(event)))  # above the panel
        self._live.update(self._render())

    def _on_pipe(self, event: ProgressEvent) -> None:
        counts = event.counts
        if event.kind == "stage":
            if event.stage not in _BEFORE_ITEMS and counts.selected > 0:
                # A budget stop or a fatal error ends the items early (processed < selected):
                # report the final item counts before the next stage, once.
                self._echo_items(counts)
            typer.echo(_stage_line(event), err=True)
            return
        if self._verbose:
            typer.echo(_item_line(event), err=True)
        if self._verbose or counts.processed == counts.selected:
            self._echo_items(counts)

    def _echo_items(self, counts: ProgressSnapshot) -> None:
        line = _items_line(counts)
        if line != self._last_items_line:
            typer.echo(line, err=True)
            self._last_items_line = line
