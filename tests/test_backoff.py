"""``invio.scheduling.backoff``: the retry delay after a failed run (#23)."""

from datetime import timedelta

import pytest

from invio.scheduling import backoff
from invio.scheduling.backoff import retry_delay


@pytest.mark.parametrize(
    ("streak", "hours"),
    [(1, 1), (2, 2), (3, 4), (4, 8), (5, 16), (6, 24), (7, 24), (100, 24), (10**9, 24)],
)
def test_the_delay_doubles_from_one_hour_up_to_a_day(streak: int, hours: int) -> None:
    assert retry_delay(streak) == timedelta(hours=hours)


@pytest.mark.parametrize("streak", [0, -1])
def test_a_streak_below_one_is_rejected(streak: int) -> None:
    with pytest.raises(ValueError, match="streak must be >= 1"):
        retry_delay(streak)


def test_the_policy_constants() -> None:
    assert timedelta(hours=1) == backoff.RETRY_BASE
    assert timedelta(hours=24) == backoff.RETRY_MAX
    assert backoff.RETRY_STREAK_CAP == 6


def test_the_graph_reads_exactly_as_many_failures_as_the_delay_grows_with() -> None:
    # graph must not import scheduling (test_pipeline_layering), so the cap is mirrored there.
    from invio.graph import stages

    assert stages._STREAK_READ_CAP == backoff.RETRY_STREAK_CAP
