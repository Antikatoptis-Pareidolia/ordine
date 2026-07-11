"""Shared web view helpers.

Owns template context builders and transition gating for the UI. Must never implement step logic.
"""

from __future__ import annotations

from conveyor.core.ledger import VALID_TRANSITIONS, TaskView


def can_retry(task: TaskView) -> bool:
    return "pending" in VALID_TRANSITIONS.get(task.status, frozenset())


def can_cancel(task: TaskView) -> bool:
    return "skipped" in VALID_TRANSITIONS.get(task.status, frozenset())
