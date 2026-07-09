"""Engine abstraction and headless in-process execution.

Owns engine discovery and step invocation wrappers. Must never run pipeline loops.
"""

from __future__ import annotations

import logging
import traceback
from collections.abc import Iterable
from importlib.metadata import EntryPoint, entry_points
from typing import ClassVar, Protocol, runtime_checkable

from pydantic import BaseModel

from conveyor.core.errors import UnknownEngineError
from conveyor.core.steps import Step, StepContext, StepResult

logger = logging.getLogger(__name__)

ENGINE_ENTRY_POINT_GROUP = "conveyor.engines"


@runtime_checkable
class Engine(Protocol):
    """Engine that executes a single step invocation."""

    name: ClassVar[str]

    def run_step(self, step: Step | type[Step], ctx: StepContext, params: BaseModel) -> StepResult:
        """Run one step and return its result."""
        ...


class HeadlessEngine:
    """In-process step runner for headless pipelines."""

    name = "headless"

    def run_step(self, step: Step | type[Step], ctx: StepContext, params: BaseModel) -> StepResult:
        instance = step() if isinstance(step, type) else step
        try:
            return instance.run(ctx, params)
        except Exception as exc:
            ctx.logger.error("unexpected step error:\n%s", traceback.format_exc())
            return StepResult(status="fail", message=f"unexpected error: {exc!r}")


class EngineRegistry:
    """Registry of engines discovered via entry points or direct registration."""

    def __init__(self) -> None:
        self._engines: dict[str, Engine] = {}

    @classmethod
    def load(cls, extra_entry_points: Iterable[EntryPoint] | None = None) -> EngineRegistry:
        registry = cls()
        eps = entry_points(group=ENGINE_ENTRY_POINT_GROUP)
        all_eps: list[EntryPoint] = list(eps)
        if extra_entry_points is not None:
            all_eps.extend(extra_entry_points)
        for ep in all_eps:
            if ep.name in registry._engines:
                logger.warning("duplicate engine entry point %r ignored", ep.name)
                continue
            engine_obj = ep.load()
            registry.register(engine_obj if not isinstance(engine_obj, type) else engine_obj())
        return registry

    def register(self, engine: Engine) -> None:
        """Register an engine instance."""
        if not hasattr(engine, "name"):
            raise TypeError("engine must define a name")
        if engine.name in self._engines:
            logger.warning("duplicate engine %r ignored", engine.name)
            return
        self._engines[engine.name] = engine

    def get(self, name: str) -> Engine:
        """Return a registered engine."""
        try:
            return self._engines[name]
        except KeyError as exc:
            raise UnknownEngineError(name) from exc

    def names(self) -> list[str]:
        """Return registered engine names sorted."""
        return sorted(self._engines)
