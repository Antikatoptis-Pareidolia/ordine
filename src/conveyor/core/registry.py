"""Step plugin discovery and playbook step validation.

Owns step registration and param validation. Must never execute pipeline loops or import web/cli.
"""

from __future__ import annotations

import logging
from collections.abc import Iterable
from importlib.metadata import EntryPoint, entry_points
from typing import Any, ClassVar, Protocol, cast

from pydantic import BaseModel, ValidationError

from conveyor.core.errors import FieldError, StepError, StepParamError, UnknownStepError
from conveyor.core.playbook import STEP_ID_RE, Playbook, StepSpec

logger = logging.getLogger(__name__)

ENTRY_POINT_GROUP = "conveyor.steps"


class StepClass(Protocol):
    id: ClassVar[str]
    engines: ClassVar[frozenset[str]]
    Params: ClassVar[type[BaseModel]]
    OUTPUT_DIR_PARAMS: ClassVar[frozenset[str]]

    def run(self, ctx: object, params: BaseModel) -> object: ...


def _assert_step_contract(step_cls: type[StepClass]) -> None:
    for attr in ("id", "engines", "Params"):
        if not hasattr(step_cls, attr):
            raise StepError(f"step class {step_cls!r} missing required attribute {attr!r}")
    step_id = step_cls.id
    if not isinstance(step_id, str) or not STEP_ID_RE.match(step_id):
        raise StepError(f"step class {step_cls!r} has invalid id {step_id!r}")
    params_cls = step_cls.Params
    if not isinstance(params_cls, type) or not issubclass(params_cls, BaseModel):
        raise StepError(f"step {step_id}: Params must be a pydantic BaseModel subclass")
    extra = params_cls.model_config.get("extra")
    if extra != "forbid":
        raise StepError(f"step {step_id}: Params must use extra='forbid'")


class StepRegistry:
    """Registry of step plugins discovered via entry points or direct registration."""

    def __init__(self) -> None:
        self._steps: dict[str, type[StepClass]] = {}
        self._sources: dict[str, str] = {}

    @classmethod
    def load(cls, extra_entry_points: Iterable[EntryPoint] | None = None) -> StepRegistry:
        """Discover steps from entry points and optional extras (test seam)."""
        registry = cls()
        seen: dict[str, EntryPoint] = {}
        eps = entry_points(group=ENTRY_POINT_GROUP)
        all_eps: list[EntryPoint] = list(eps)
        if extra_entry_points is not None:
            all_eps.extend(extra_entry_points)
        for ep in all_eps:
            if ep.name in seen:
                logger.warning(
                    "duplicate step entry point %r: keeping %s, ignoring %s",
                    ep.name,
                    seen[ep.name].value,
                    ep.value,
                )
                continue
            seen[ep.name] = ep
            step_cls = cast(type[StepClass], ep.load())
            registry.register(step_cls, source=ep.value)
        return registry

    def register(self, step_cls: type[StepClass], *, source: str | None = None) -> None:
        """Register a step class directly (tests, embedding)."""
        _assert_step_contract(step_cls)
        step_id = step_cls.id
        if step_id in self._steps:
            logger.warning(
                "duplicate step id %r: keeping %s, ignoring %s",
                step_id,
                self._sources[step_id],
                source or step_cls.__module__,
            )
            return
        self._steps[step_id] = step_cls
        self._sources[step_id] = source or f"{step_cls.__module__}.{step_cls.__name__}"

    def get(self, step_id: str) -> type[StepClass]:
        """Return a registered step class."""
        try:
            return self._steps[step_id]
        except KeyError as exc:
            raise UnknownStepError(step_id) from exc

    def ids(self) -> list[str]:
        """Return registered step ids sorted."""
        return sorted(self._steps)

    def validate_params(self, step_id: str, params: dict[str, Any]) -> BaseModel:
        """Validate params against the step's Params model."""
        step_cls = self.get(step_id)
        try:
            return step_cls.Params.model_validate(params)
        except ValidationError as exc:
            errors = [
                FieldError(path=".".join(str(part) for part in err["loc"]), message=err["msg"])
                for err in exc.errors()
            ]
            raise StepParamError(step_id, errors) from exc

    def param_schema(self, step_id: str) -> dict[str, Any]:
        """Return JSON Schema for a step's Params model."""
        step_cls = self.get(step_id)
        return step_cls.Params.model_json_schema()

    def check_playbook(self, playbook: Playbook) -> list[FieldError]:
        """Validate all step ids, params, and engine compatibility in a playbook."""
        errors: list[FieldError] = []
        self._check_step_list(playbook.steps, "steps", playbook.engine, errors)
        for branch_index, branch in enumerate(playbook.on_failure.branches):
            prefix = f"on_failure.branches.{branch_index}.steps"
            self._check_step_list(branch.steps, prefix, playbook.engine, errors)
        return errors

    def _check_step_list(
        self,
        steps: list[StepSpec],
        path_prefix: str,
        engine_name: str,
        errors: list[FieldError],
    ) -> None:
        for index, step in enumerate(steps):
            step_path = f"{path_prefix}.{index}"
            try:
                step_cls = self.get(step.id)
            except UnknownStepError:
                errors.append(FieldError(f"{step_path}.id", f"unknown step id: {step.id}"))
                continue
            try:
                self.validate_params(step.id, step.params)
            except StepParamError as exc:
                for field_error in exc.errors:
                    param_path = (
                        f"{step_path}.params"
                        if not field_error.path
                        else f"{step_path}.params.{field_error.path}"
                    )
                    errors.append(FieldError(param_path, field_error.message))
            if engine_name not in step_cls.engines:
                errors.append(
                    FieldError(
                        f"{step_path}.id",
                        f"step {step.id} does not support engine {engine_name}",
                    )
                )
            if step.on_failure is not None:
                for branch_index, branch in enumerate(step.on_failure.branches):
                    branch_prefix = f"{step_path}.on_failure.branches.{branch_index}.steps"
                    self._check_step_list(branch.steps, branch_prefix, engine_name, errors)
