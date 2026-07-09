"""Exception hierarchy for Conveyor core.

Owns: all core-level error types. Must never import from other conveyor modules.
"""

from __future__ import annotations

from dataclasses import dataclass


class ConveyorError(Exception):
    """Base class for all Conveyor errors."""


class PlaybookError(ConveyorError):
    """Base class for playbook loading/validation errors."""


@dataclass(frozen=True)
class FieldError:
    """One validation failure at a dotted field path, e.g. 'steps.2.on_failure.retries'."""

    path: str
    message: str


class PlaybookSyntaxError(PlaybookError):
    """YAML could not be parsed. Carries best-known position."""

    def __init__(self, source: str, problem: str, line: int | None, column: int | None) -> None:
        self.source, self.problem, self.line, self.column = source, problem, line, column
        pos = f" at line {line}, column {column}" if line is not None else ""
        super().__init__(f"{source}: YAML syntax error{pos}: {problem}")


class PlaybookValidationError(PlaybookError):
    """YAML parsed but the document violates the playbook schema."""

    def __init__(self, source: str, errors: list[FieldError]) -> None:
        self.source, self.errors = source, errors
        detail = "; ".join(f"{e.path}: {e.message}" for e in errors)
        super().__init__(f"{source}: invalid playbook: {detail}")


class LedgerError(ConveyorError):
    """Base class for ledger errors."""


class IllegalTransitionError(LedgerError):
    """Raised when a task state transition is not allowed."""

    def __init__(self, task_id: int, current: str, target: str) -> None:
        self.task_id = task_id
        self.current = current
        self.target = target
        super().__init__(f"task {task_id}: illegal transition {current} -> {target}")


class SchemaVersionError(LedgerError):
    """Raised when the database schema version is unsupported."""
