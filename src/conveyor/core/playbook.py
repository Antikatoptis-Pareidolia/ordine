"""Playbook schema, loader, and JSON Schema export.

Owns the playbook contract. Must never execute steps, touch the filesystem beyond
reading the given path, or import from executors/web/cli/llm.
"""

from __future__ import annotations

import argparse
import json
import re
from pathlib import Path
from typing import Annotated, Any, Literal, Self

import yaml  # type: ignore[import-untyped]
from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator, model_validator

from conveyor.core.errors import FieldError, PlaybookSyntaxError, PlaybookValidationError

SCHEMA_VERSION = 1
STEP_ID_RE = re.compile(r"^[a-z][a-z0-9_]*(\.[a-z][a-z0-9_]*)+$")
SLUG_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,63}$")

_RESERVED_STEP_KEYS = frozenset({"id", "params", "on_failure"})
_STEP_FORMAT_ERROR = (
    "step must be a string, a single-key mapping, or an {id, params, on_failure} mapping"
)


def _normalize_step(step: object) -> dict[str, Any]:
    """Normalize a YAML step form to the long {id, params, on_failure} shape."""
    if isinstance(step, str):
        return {"id": step}
    if isinstance(step, dict):
        if "id" in step:
            return step
        if len(step) == 1:
            key, value = next(iter(step.items()))
            if key in _RESERVED_STEP_KEYS:
                return step
            if value is None:
                return {"id": key, "params": {}}
            if isinstance(value, dict):
                return {"id": key, "params": value}
        raise ValueError(_STEP_FORMAT_ERROR)
    raise ValueError(_STEP_FORMAT_ERROR)


def _normalize_steps_list(steps: object, *, path_prefix: str) -> list[object]:
    if not isinstance(steps, list):
        return steps  # type: ignore[return-value]
    normalized: list[object] = []
    for index, step in enumerate(steps):
        try:
            normalized.append(_normalize_step(step))
        except ValueError as exc:
            raise ValueError(f"{path_prefix}.{index}: {exc}") from exc
    return normalized


def _validate_slug(value: str, field_name: str) -> str:
    if not SLUG_RE.match(value):
        raise ValueError(f"invalid {field_name}: {value!r}")
    return value


def _validate_step_id(value: str) -> str:
    if not STEP_ID_RE.match(value):
        raise ValueError(f"invalid step id: {value!r}")
    return value


def _validate_ordinal_regex(value: str | None) -> str | None:
    if value is None:
        return value
    try:
        pattern = re.compile(value)
    except re.error as exc:
        raise ValueError(
            "ordinal_regex must be a valid regex with exactly one capture group"
        ) from exc
    if pattern.groups != 1:
        raise ValueError("ordinal_regex must be a valid regex with exactly one capture group")
    return value


class FailurePolicy(BaseModel):
    model_config = ConfigDict(extra="forbid")

    retries: int = Field(default=0, ge=0)
    branches: list[RecoveryBranch] = Field(default_factory=list)
    then: Literal["mark_failed", "skip"] = "mark_failed"

    @model_validator(mode="after")
    def check_unique_branch_names(self) -> Self:
        seen: set[str] = set()
        for branch in self.branches:
            if branch.name in seen:
                raise ValueError(f"duplicate recovery branch name: {branch.name}")
            seen.add(branch.name)
        return self


class StepSpec(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str
    params: dict[str, Any] = Field(default_factory=dict)
    on_failure: FailurePolicy | None = None

    @field_validator("id")
    @classmethod
    def validate_id(cls, value: str) -> str:
        return _validate_step_id(value)


class RecoveryBranch(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str
    retries: int = Field(default=0, ge=0)
    steps: list[StepSpec] = Field(min_length=1)

    @field_validator("name")
    @classmethod
    def validate_name(cls, value: str) -> str:
        return _validate_slug(value, "branch name")

    @field_validator("steps", mode="before")
    @classmethod
    def normalize_steps(cls, value: object) -> object:
        if not isinstance(value, list):
            return value
        return _normalize_steps_list(value, path_prefix="steps")

    @model_validator(mode="after")
    def check_no_nested_on_failure(self) -> Self:
        for step in self.steps:
            if step.on_failure is not None:
                raise ValueError("recovery branch steps may not define on_failure (no nesting)")
        return self


class FolderWatchTrigger(BaseModel):
    model_config = ConfigDict(extra="forbid")

    type: Literal["folder_watch"]
    path: str
    glob: str = "*"
    settle_seconds: float = Field(default=2.0, ge=0)
    ordinal_regex: str | None = None
    arrival_order_ordinals: bool = False

    @field_validator("ordinal_regex")
    @classmethod
    def validate_ordinal_regex(cls, value: str | None) -> str | None:
        return _validate_ordinal_regex(value)

    @model_validator(mode="after")
    def check_ordinal_source(self) -> Self:
        if self.ordinal_regex is not None and self.arrival_order_ordinals:
            raise ValueError("choose either ordinal_regex or arrival_order_ordinals, not both")
        return self


class ManualTrigger(BaseModel):
    model_config = ConfigDict(extra="forbid")

    type: Literal["manual"]
    path: str
    glob: str = "*"
    ordinal_regex: str | None = None
    arrival_order_ordinals: bool = False

    @field_validator("ordinal_regex")
    @classmethod
    def validate_ordinal_regex(cls, value: str | None) -> str | None:
        return _validate_ordinal_regex(value)

    @model_validator(mode="after")
    def check_ordinal_source(self) -> Self:
        if self.ordinal_regex is not None and self.arrival_order_ordinals:
            raise ValueError("choose either ordinal_regex or arrival_order_ordinals, not both")
        return self


class ManifestTrigger(BaseModel):
    model_config = ConfigDict(extra="forbid")

    type: Literal["manifest"]
    path: str
    poll_seconds: float = Field(default=30.0, ge=0)


Trigger = Annotated[
    FolderWatchTrigger | ManualTrigger | ManifestTrigger,
    Field(discriminator="type"),
]


class PlaybookMeta(BaseModel):
    model_config = ConfigDict(extra="forbid")

    version_id: str | None = None
    parent_version_id: str | None = None


class Playbook(BaseModel):
    model_config = ConfigDict(extra="forbid")

    version: Literal[1]
    name: str
    description: str | None = None
    trigger: Trigger
    dedup: Literal["content_hash", "filename", "none"] = "content_hash"
    engine: str = "headless"
    steps: list[StepSpec] = Field(min_length=1)
    on_failure: FailurePolicy = Field(default_factory=FailurePolicy)
    meta: PlaybookMeta | None = None

    @field_validator("name")
    @classmethod
    def validate_name(cls, value: str) -> str:
        return _validate_slug(value, "playbook name")

    @field_validator("engine")
    @classmethod
    def validate_engine(cls, value: str) -> str:
        return _validate_slug(value, "engine")

    @field_validator("steps", mode="before")
    @classmethod
    def normalize_steps(cls, value: object) -> object:
        if not isinstance(value, list):
            return value
        return _normalize_steps_list(value, path_prefix="steps")

    @model_validator(mode="after")
    def check_playbook_wide_branch_names(self) -> Self:
        seen: set[str] = set()
        policies: list[tuple[FailurePolicy, str]] = [(self.on_failure, "playbook")]
        for index, step in enumerate(self.steps):
            if step.on_failure is not None:
                policies.append((step.on_failure, f"steps.{index}"))
        for policy, _location in policies:
            for branch in policy.branches:
                if branch.name in seen:
                    raise ValueError(
                        f"recovery branch name {branch.name!r} is used by more than one step; "
                        "branch names must be unique across the playbook"
                    )
                seen.add(branch.name)
        return self


FailurePolicy.model_rebuild()
StepSpec.model_rebuild()
RecoveryBranch.model_rebuild()


def _validation_errors_from_pydantic(exc: ValidationError) -> list[FieldError]:
    return [
        FieldError(path=".".join(str(part) for part in err["loc"]), message=err["msg"])
        for err in exc.errors()
    ]


def loads_playbook(text: str, source: str = "<string>") -> Playbook:
    """Parse and validate playbook YAML from a string."""
    try:
        data = yaml.safe_load(text)
    except yaml.MarkedYAMLError as exc:
        line: int | None = None
        column: int | None = None
        if exc.problem_mark is not None:
            line = exc.problem_mark.line + 1
            column = exc.problem_mark.column + 1
        problem = exc.problem if exc.problem is not None else str(exc)
        raise PlaybookSyntaxError(source, problem, line, column) from exc
    except yaml.YAMLError as exc:
        raise PlaybookSyntaxError(source, str(exc), None, None) from exc

    if not isinstance(data, dict):
        raise PlaybookValidationError(
            source,
            [FieldError(path="$", message="playbook must be a YAML mapping")],
        )

    try:
        return Playbook.model_validate(data)
    except ValidationError as exc:
        raise PlaybookValidationError(source, _validation_errors_from_pydantic(exc)) from exc


def load_playbook(path: Path) -> Playbook:
    """Read and validate a playbook from a filesystem path."""
    expanded = path.expanduser()
    text = expanded.read_text(encoding="utf-8")
    return loads_playbook(text, source=str(expanded))


def _is_default_failure_policy(policy: FailurePolicy) -> bool:
    return policy.retries == 0 and not policy.branches and policy.then == "mark_failed"


def _dump_failure_policy_dict(policy: FailurePolicy) -> dict[str, Any]:
    result: dict[str, Any] = {}
    if policy.retries != 0:
        result["retries"] = policy.retries
    if policy.branches:
        result["branches"] = [_dump_recovery_branch(branch) for branch in policy.branches]
    if policy.then != "mark_failed":
        result["then"] = policy.then
    return result


def _dump_recovery_branch(branch: RecoveryBranch) -> dict[str, Any]:
    result: dict[str, Any] = {"name": branch.name}
    if branch.retries != 0:
        result["retries"] = branch.retries
    result["steps"] = [_dump_step_spec(step) for step in branch.steps]
    return result


def _dump_step_spec(step: StepSpec) -> str | dict[str, Any]:
    has_params = bool(step.params)
    has_on_failure = step.on_failure is not None
    if not has_params and not has_on_failure:
        return step.id
    if has_on_failure:
        assert step.on_failure is not None
        long_form: dict[str, Any] = {"id": step.id}
        if has_params:
            long_form["params"] = step.params
        long_form["on_failure"] = _dump_failure_policy_dict(step.on_failure)
        return long_form
    return {step.id: step.params}


def _dump_trigger_dict(trigger: Trigger) -> dict[str, Any]:
    if trigger.type == "folder_watch":
        result: dict[str, Any] = {
            "type": trigger.type,
            "path": trigger.path,
        }
        if trigger.glob != "*":
            result["glob"] = trigger.glob
        if trigger.settle_seconds != 2.0:
            result["settle_seconds"] = trigger.settle_seconds
        if trigger.ordinal_regex is not None:
            result["ordinal_regex"] = trigger.ordinal_regex
        if trigger.arrival_order_ordinals:
            result["arrival_order_ordinals"] = True
        return result
    if trigger.type == "manual":
        result = {
            "type": trigger.type,
            "path": trigger.path,
        }
        if trigger.glob != "*":
            result["glob"] = trigger.glob
        if trigger.ordinal_regex is not None:
            result["ordinal_regex"] = trigger.ordinal_regex
        if trigger.arrival_order_ordinals:
            result["arrival_order_ordinals"] = True
        return result
    result = {
        "type": trigger.type,
        "path": trigger.path,
    }
    if trigger.poll_seconds != 30.0:
        result["poll_seconds"] = trigger.poll_seconds
    return result


def _playbook_to_dump_dict(playbook: Playbook) -> dict[str, Any]:
    data: dict[str, Any] = {
        "version": playbook.version,
        "name": playbook.name,
    }
    if playbook.description is not None:
        data["description"] = playbook.description
    data["trigger"] = _dump_trigger_dict(playbook.trigger)
    if playbook.dedup != "content_hash":
        data["dedup"] = playbook.dedup
    if playbook.engine != "headless":
        data["engine"] = playbook.engine
    data["steps"] = [_dump_step_spec(step) for step in playbook.steps]
    if not _is_default_failure_policy(playbook.on_failure):
        data["on_failure"] = _dump_failure_policy_dict(playbook.on_failure)
    if playbook.meta is not None:
        data["meta"] = playbook.meta.model_dump(exclude_none=True)
    return data


def dump_playbook(playbook: Playbook) -> str:
    """Serialize a validated playbook to YAML with stable key order and compact step forms.

    Round-trip guarantee: ``loads_playbook(dump_playbook(pb)) == pb`` for valid playbooks.
    Comments are not preserved (PyYAML safe_load discards them).
    """
    data = _playbook_to_dump_dict(playbook)
    dumped: str = yaml.safe_dump(
        data,
        sort_keys=False,
        default_flow_style=False,
        allow_unicode=True,
    )
    return dumped


def emit_json_schema(dest: Path) -> None:
    """Write the Playbook JSON Schema to *dest*."""
    schema = Playbook.model_json_schema()
    dest.write_text(json.dumps(schema, indent=2) + "\n", encoding="utf-8")


def _main() -> None:
    parser = argparse.ArgumentParser(description="Conveyor playbook schema tools")
    parser.add_argument(
        "--emit-schema",
        metavar="PATH",
        required=True,
        help="Write Playbook JSON Schema to PATH",
    )
    args = parser.parse_args()
    emit_json_schema(Path(args.emit_schema))


if __name__ == "__main__":
    _main()
