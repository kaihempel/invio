"""``RunProgressView``: plain lines on a pipe, a live panel on a terminal; ``strip_control``."""

import io

import pytest
from rich.console import Console

from invio.cli.progress import RunProgressView
from invio.graph.ports import ProgressCounts
from invio.pipeline import ProgressEvent
from invio.textsafe import strip_control


def _stage(stage: str, **counts: int) -> ProgressEvent:
    snapshot = ProgressCounts(**counts).snapshot()
    return ProgressEvent(kind="stage", stage=stage, counts=snapshot)  # type: ignore[arg-type]


def _item(outcome: str, title: str, message: str | None = None, **counts: int) -> ProgressEvent:
    return ProgressEvent(
        kind="item",
        stage="score_relevance",
        counts=ProgressCounts(**counts).snapshot(),
        item_id=1,
        title=title,
        outcome=outcome,  # type: ignore[arg-type]
        message=message,
    )


def test_a_pipe_gets_one_line_per_stage_and_one_per_finished_batch(
    capsys: pytest.CaptureFixture[str],
) -> None:
    view = RunProgressView(Console(file=io.StringIO()))
    with view:
        view(_stage("deduplicate", found=12, new=5))
        view(_stage("keyword_prefilter", after_keyword_filter=4, selected=2))
        view(_item("relevant", "A", processed=1, selected=2, relevant=1))
        view(_item("failed", "B", "E: x", processed=2, selected=2, relevant=1, failed=1))
        view(_stage("persist"))

    assert capsys.readouterr().err.splitlines() == [
        "progress: deduplicate found=12 new=5",
        "progress: keyword_prefilter after_keyword_filter=4",
        "progress: items processed=2/2 relevant=1 failed=1",
        "progress: persist",
    ]


def test_verbose_pipe_lines_are_clean(capsys: pytest.CaptureFixture[str]) -> None:
    view = RunProgressView(Console(file=io.StringIO()), verbose=True)

    view(_item("failed", "\x1b[31mT\nx", "E: \x07bad", processed=1, selected=2, failed=1))

    lines = capsys.readouterr().err.splitlines()
    assert lines[0] == "item: failed  T x  E: bad"
    assert lines[1] == "progress: items processed=1/2 relevant=0 failed=1"


def test_a_terminal_gets_a_live_panel_that_is_stopped() -> None:
    sink = io.StringIO()
    console = Console(file=sink, force_terminal=True, width=100)
    view = RunProgressView(console, verbose=True)

    with view:
        view(_stage("deduplicate", found=3, new=2))
        assert view._live is not None
        view(_item("relevant", "[red]Title[/red]", processed=1, selected=2, relevant=1))

    assert view._live is None
    assert "[red]Title[/red]" in sink.getvalue()  # printed as text, never as markup


def test_the_panel_is_stopped_when_the_body_raises() -> None:
    view = RunProgressView(Console(file=io.StringIO(), force_terminal=True))

    with pytest.raises(KeyboardInterrupt), view:
        view(_stage("deduplicate"))
        raise KeyboardInterrupt

    assert view._live is None


def test_an_early_stop_reports_the_item_counts_before_the_next_stage(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Budget stop: 1 of 3 items ran, so no item event reached processed == selected."""
    view = RunProgressView(Console(file=io.StringIO()))

    view(_stage("keyword_prefilter", after_keyword_filter=3, selected=3))
    view(_item("relevant", "A", processed=1, selected=3, relevant=1))
    view(_stage("synthesize_digest", processed=1, selected=3, relevant=1))
    view(_stage("persist", processed=1, selected=3, relevant=1))

    assert capsys.readouterr().err.splitlines() == [
        "progress: keyword_prefilter after_keyword_filter=3",
        "progress: items processed=1/3 relevant=1 failed=0",
        "progress: synthesize_digest",
        "progress: persist",
    ]


def test_a_finished_batch_is_not_reported_twice(capsys: pytest.CaptureFixture[str]) -> None:
    view = RunProgressView(Console(file=io.StringIO()), verbose=True)

    view(_item("relevant", "A", processed=1, selected=1, relevant=1))
    view(_stage("synthesize_digest", processed=1, selected=1, relevant=1))

    assert capsys.readouterr().err.splitlines() == [
        "item: relevant  A",
        "progress: items processed=1/1 relevant=1 failed=0",
        "progress: synthesize_digest",
    ]


def test_a_run_without_selected_items_has_no_items_line(
    capsys: pytest.CaptureFixture[str],
) -> None:
    view = RunProgressView(Console(file=io.StringIO()))

    view(_stage("keyword_prefilter"))
    view(_stage("synthesize_digest"))

    assert "progress: items" not in capsys.readouterr().err


def test_a_pipe_never_starts_a_live_panel() -> None:
    view = RunProgressView(Console(file=io.StringIO()))

    with view:
        assert view._live is None


def test_the_panel_shows_the_stage_counts_and_the_item_bar() -> None:
    sink = io.StringIO()
    view = RunProgressView(Console(file=sink, force_terminal=True, width=120))

    with view:
        view(_stage("keyword_prefilter", found=9, new=4, after_keyword_filter=2, selected=2))
        view(_item("failed", "X", "E: m", processed=1, selected=2, failed=1, found=9, new=4))
        rendered = view._render()

    text = "\n".join(line.plain for line in rendered.renderables)  # type: ignore[attr-defined]
    assert "stage: keyword_prefilter" in text
    assert "found 9 · new 4 · after filter 0 · relevant 0 · failed 1" in text
    assert "processed 1/2" in text
    assert "item:" not in sink.getvalue()  # not verbose: no per-item lines on the terminal


def test_strip_control() -> None:
    assert strip_control("\x1b[31mred\x1b[0m\ttext\r\n\x00x") == "red text x"
    assert strip_control("\x1b]0;title\x07after") == "after"
    assert strip_control("abcdef", limit=3) == "abc"
    assert strip_control("a" + chr(0x2028) + "b") == "a b"


@pytest.mark.parametrize(
    ("raw", "clean"),
    [
        ("\x9b31mC1 CSI", "31mC1 CSI"),  # the C1 introducer itself is removed
        ("\x1b]8;;https://evil.example\x1b\\link\x1b]8;;\x1b\\", "link"),  # OSC 8 hyperlink
        ("bell\x07\x7fdel", "bell del"),
        ("[bold]markup[/bold]", "[bold]markup[/bold]"),  # markup is text, not stripped
        ("  spaced " + chr(0xA0) + " out  ", "spaced out"),  # a no-break space too
        ("", ""),
    ],
)
def test_strip_control_hostile_inputs(raw: str, clean: str) -> None:
    assert strip_control(raw) == clean


def test_strip_control_multiline_keeps_the_layout() -> None:
    text = "# T\r\n\r\tcol\x1b[31ma\x1b[0m\tb\x1b]0;x\x07\x00\x7f\x85\n\n  keep  "

    assert strip_control(text, multiline=True) == "# T\n\n\tcola\tb\n\n  keep  "
