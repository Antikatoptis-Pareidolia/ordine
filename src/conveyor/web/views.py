"""Shared web view helpers.

Owns template context builders and transition gating for the UI. Must never implement step logic.
"""

from __future__ import annotations

from conveyor.core.ledger import VALID_TRANSITIONS, TaskView


def version_label(
    version_id: str,
    note: str | None = None,
    *,
    parent_id: str | None = None,
) -> str:
    """Human-readable version id, e.g. ``pv_0007 — fix manifest path (from pv_0006)``."""
    label = version_id
    if note:
        label = f"{version_id} — {note}"
    if parent_id:
        label = f"{label} (from {parent_id})"
    return label


def can_retry(task: TaskView) -> bool:
    return "pending" in VALID_TRANSITIONS.get(task.status, frozenset())


def can_cancel(task: TaskView) -> bool:
    return "skipped" in VALID_TRANSITIONS.get(task.status, frozenset())
