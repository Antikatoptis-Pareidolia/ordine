"""CLI output formatting: plain tables and JSON emission.

Owns stdout/stderr separation for the CLI. Must never import ledger business logic.
"""

from __future__ import annotations

import json
import sys
from collections.abc import Mapping
from datetime import UTC, datetime


def iso_timestamp(value: datetime | None) -> str | None:
    """Format a timezone-aware datetime as ISO-8601 UTC."""
    if value is None:
        return None
    return value.astimezone(UTC).isoformat()


def emit_json(payload: object) -> None:
    """Write exactly one JSON object to stdout."""
    sys.stdout.write(json.dumps(payload, default=_json_default))
    sys.stdout.write("\n")
    sys.stdout.flush()


def _json_default(value: object) -> object:
    if isinstance(value, datetime):
        return iso_timestamp(value)
    if isinstance(value, set):
        return sorted(value)
    raise TypeError(f"not JSON serializable: {type(value)!r}")


def print_table(headers: list[str], rows: list[list[str]]) -> None:
    """Print a plain-text column table to stdout."""
    if not rows:
        sys.stdout.write("(no rows)\n")
        return
    widths = [len(header) for header in headers]
    for row in rows:
        for index, cell in enumerate(row):
            widths[index] = max(widths[index], len(cell))
    header_line = "  ".join(header.ljust(widths[i]) for i, header in enumerate(headers))
    sys.stdout.write(header_line + "\n")
    sys.stdout.write("  ".join("-" * width for width in widths) + "\n")
    for row in rows:
        sys.stdout.write("  ".join(cell.ljust(widths[i]) for i, cell in enumerate(row)) + "\n")


def print_line(message: str) -> None:
    """Print a single human-readable line to stdout."""
    sys.stdout.write(message + "\n")


def format_age(created_at: datetime) -> str:
    """Return a short human age string for *created_at*."""
    delta = datetime.now(tz=UTC) - created_at.astimezone(UTC)
    seconds = int(delta.total_seconds())
    if seconds < 60:
        return f"{seconds}s"
    minutes = seconds // 60
    if minutes < 60:
        return f"{minutes}m"
    hours = minutes // 60
    if hours < 48:
        return f"{hours}h"
    days = hours // 24
    return f"{days}d"


def format_status_counts(counts: Mapping[str, int]) -> str:
    """Format non-zero task status counts for plain-text status output."""
    order = ("pending", "processing", "done", "skipped", "failed", "flagged")
    parts = [f"{status}={counts[status]}" for status in order if counts.get(status, 0) > 0]
    return " ".join(parts)
