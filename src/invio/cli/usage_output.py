"""Pure formatting for ``invio usage``: the table, the JSON document and the unpriced warning.

No I/O, no database. Job names and model ids come from the database, so the table and the
warning strip control characters (the table uses ``Text``, never markup); JSON escapes them.
"""

import json
from decimal import Decimal
from typing import Any, Final

from rich.table import Table
from rich.text import Text

from invio.services.usage import UsageGroup, UsageReport, UsageTotal
from invio.textsafe import strip_control

__all__ = ["format_usage_cost", "unpriced_warning", "usage_json", "usage_table"]

_COST_STEP: Final = Decimal("0.0001")
_COLUMNS: Final = ("Calls", "Input tokens", "Output tokens", "Cost (USD)")


def format_usage_cost(total: UsageTotal) -> str:
    """``$X`` (0.0001 steps), ``≥ $X`` when a model is unpriced, ``unknown`` when none is."""
    if total.cost_usd is None:
        return "unknown"
    cost = f"${total.cost_usd.quantize(_COST_STEP)}"
    return cost if total.cost_complete else f"≥ {cost}"


def _cells(label: str, total: UsageTotal) -> list[Text]:
    return [
        Text(strip_control(label)),
        Text(f"{total.calls:,}"),
        Text(f"{total.input_tokens:,}"),
        Text(f"{total.output_tokens:,}"),
        Text(format_usage_cost(total)),
    ]


def usage_table(report: UsageReport) -> Table:
    """One row per group, then the ``Total`` row."""
    table = Table(box=None)
    table.add_column(report.by.value.capitalize(), no_wrap=True)
    for column in _COLUMNS:
        table.add_column(column, no_wrap=True, justify="right")
    for group in report.groups:
        table.add_row(*_cells(group.key, group))
    table.add_row(*_cells("Total", report.total))
    return table


def _fields(total: UsageTotal) -> dict[str, Any]:
    return {
        "calls": total.calls,
        "input_tokens": total.input_tokens,
        "output_tokens": total.output_tokens,
        "cost_usd": str(total.cost_usd) if total.cost_usd is not None else None,
        "cost_complete": total.cost_complete,
        "unpriced_models": list(total.unpriced_models),
    }


def _group(group: UsageGroup) -> dict[str, Any]:
    return {"key": group.key, **_fields(group)}


def usage_json(report: UsageReport) -> str:
    """The report as one JSON document; costs are exact decimal strings (or ``null``)."""
    document = {
        "by": report.by.value,
        "job": report.job,
        "since": report.since.isoformat() if report.since is not None else None,
        "groups": [_group(group) for group in report.groups],
        "total": _fields(report.total),
        "unpriced_models": list(report.unpriced_models),
    }
    return json.dumps(document, indent=2, ensure_ascii=False)


def unpriced_warning(report: UsageReport) -> str | None:
    """One line naming each unpriced model, or ``None`` when every model has a price."""
    if not report.unpriced_models:
        return None
    names = ", ".join(strip_control(model) for model in report.unpriced_models)
    return (
        f"Warning: no price in the model registry for {names}; "
        "their tokens are counted, their cost is not"
    )
