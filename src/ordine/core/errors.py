"""Exception hierarchy for Ordine core.

Owns: all core-level error types. Must never import from other ordine modules.
"""

from __future__ import annotations

from dataclasses import dataclass


class OrdineError(Exception):
    """Base class for all Ordine errors."""


class PlaybookError(OrdineError):
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


class LedgerError(OrdineError):
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


class StepError(OrdineError):
    """Base class for step-domain errors."""


class UnknownStepError(StepError):
    """Raised when a step id is not registered."""

    def __init__(self, step_id: str) -> None:
        self.step_id = step_id
        super().__init__(f"unknown step id: {step_id}")


class StepParamError(StepError):
    """Raised when step parameters fail validation."""

    def __init__(self, step_id: str, errors: list[FieldError]) -> None:
        self.step_id = step_id
        self.errors = errors
        detail = "; ".join(f"{e.path}: {e.message}" for e in errors)
        super().__init__(f"step {step_id}: invalid params: {detail}")


class UnknownEngineError(OrdineError):
    """Raised when an engine name is not registered."""

    def __init__(self, engine_name: str) -> None:
        self.engine_name = engine_name
        super().__init__(f"unknown engine: {engine_name}")


class EngineMismatchError(OrdineError):
    """Raised when a step does not support the requested engine."""

    def __init__(self, step_id: str, engine: str) -> None:
        self.step_id = step_id
        self.engine = engine
        super().__init__(f"step {step_id} does not support engine {engine}")


class TriggerError(OrdineError):
    """Base class for trigger configuration and runtime errors."""


class ManifestError(OrdineError):
    """Raised when a job manifest cannot be loaded or parsed."""


class RunnerError(OrdineError):
    """Raised when the pipeline runner cannot start or execute."""


class ConfigError(OrdineError):
    """Raised when application configuration is invalid or conflicts."""
