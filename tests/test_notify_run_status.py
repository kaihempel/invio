"""Tests for ``run_status_after_delivery``: delivery problems make a run ``partial``."""

import pytest

from invio.domain import RunStatus
from invio.notify.email import DeliveryOutcome, run_status_after_delivery


def _outcome(
    *, sent: int = 0, failed: int = 0, skipped_empty: bool = False, error: str | None = None
) -> DeliveryOutcome:
    return DeliveryOutcome(
        sent=sent, failed=failed, skipped_empty=skipped_empty, notification_ids=(), error=error
    )


def test_failed_notifications_turn_succeeded_into_partial() -> None:
    assert run_status_after_delivery(RunStatus.SUCCEEDED, _outcome(failed=1)) is RunStatus.PARTIAL


def test_clean_delivery_keeps_succeeded() -> None:
    outcome = _outcome(sent=2)

    assert run_status_after_delivery(RunStatus.SUCCEEDED, outcome) is RunStatus.SUCCEEDED


def test_error_without_rows_turns_succeeded_into_partial() -> None:
    outcome = _outcome(error="digest 1 not found")

    assert run_status_after_delivery(RunStatus.SUCCEEDED, outcome) is RunStatus.PARTIAL


@pytest.mark.parametrize("planned", [RunStatus.PARTIAL, RunStatus.FAILED])
@pytest.mark.parametrize(
    "outcome", [_outcome(sent=1), _outcome(failed=2), _outcome(error="x")], ids=str
)
def test_other_planned_statuses_are_unchanged(planned: RunStatus, outcome: DeliveryOutcome) -> None:
    assert run_status_after_delivery(planned, outcome) is planned


def test_skipped_empty_never_changes_the_status() -> None:
    outcome = _outcome(skipped_empty=True)

    assert run_status_after_delivery(RunStatus.SUCCEEDED, outcome) is RunStatus.SUCCEEDED
