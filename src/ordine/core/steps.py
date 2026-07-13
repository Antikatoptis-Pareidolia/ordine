"""Step contract: context, result, and plugin protocol.

Owns the typed step interface. Must never import from executors, web, cli, llm, or ledger.

Rules for step authors:
- ``run`` must not raise for expected failures — return ``status="fail"`` with a message.
  Raising is reserved for bugs; engines convert unexpected exceptions into fail results.
- Steps never touch paths outside ``input_path`` (read-only), ``step_dir`` (read-write), and
  explicit param paths. Never modify ``input_path`` in place.
- Steps never import the ledger, web, cli, or llm modules.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path
from typing import ClassVar, Literal, Protocol, runtime_checkable

from pydantic import BaseModel


class NamingService(Protocol):
    """Ordinal→name bindings for position-keyed naming (implemented by the runner in Step 7)."""

    def resolve(self, ordinal: int) -> str | None:
        """Return a reserved name for *ordinal*, if any."""
        ...

    def bind(self, ordinal: int, name: str) -> str:
        """Idempotent reserve: existing reservation wins over *name*."""
        ...


@dataclass(frozen=True)
class StepContext:
    """Runtime context passed to every step invocation."""

    task_id: int
    pipeline_name: str
    source_ref: str
    ordinal: int | None
    input_path: Path | None
    step_dir: Path
    logger: logging.Logger
    naming: NamingService | None = None


@dataclass(frozen=True)
class StepResult:
    """Outcome of a single step invocation."""

    status: Literal["ok", "fail", "skip"]
    output_path: Path | None = None
    message: str | None = None
    flag_kind: str | None = None


@runtime_checkable
class Step(Protocol):
    """Plugin step contract discovered via entry points."""

    id: ClassVar[str]
    engines: ClassVar[frozenset[str]]
    Params: ClassVar[type[BaseModel]]
    OUTPUT_DIR_PARAMS: ClassVar[frozenset[str]]

    def run(self, ctx: StepContext, params: BaseModel) -> StepResult: ...
