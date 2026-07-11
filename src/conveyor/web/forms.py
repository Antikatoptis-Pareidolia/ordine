"""Form-data conversion for the pipeline editor.

Owns flat HTML field naming ⟷ playbook dict conversion. Must never render templates
or touch the ledger.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from typing import Any, Literal

import yaml  # type: ignore[import-untyped]

from conveyor.core.errors import FieldError
from conveyor.core.playbook import FailurePolicy, Playbook, StepSpec, dump_playbook


def _is_default_failure_policy(policy: FailurePolicy) -> bool:
    return policy.retries == 0 and not policy.branches and policy.then == "mark_failed"


class FormConversionError(Exception):
    """Raised when form fields cannot be converted to a playbook dict."""

    def __init__(self, errors: list[FieldError]) -> None:
        self.errors = errors
        super().__init__(errors[0].message if errors else "form conversion failed")


def _collect_indices(form: Mapping[str, str], prefix: str) -> list[int]:
    pattern = re.compile(rf"^{re.escape(prefix)}-(\d+)(?:-|$)")
    indices: set[int] = set()
    for key in form:
        match = pattern.match(key)
        if match:
            indices.add(int(match.group(1)))
    return sorted(indices)


def onfail_branch_indices(form: Mapping[str, str], prefix: str) -> list[int]:
    """Return sorted branch indices for a failure-policy prefix (e.g. ``steps-0-onfail``)."""
    return _collect_indices(form, f"{prefix}-branches")


def branch_step_indices(form: Mapping[str, str], branch_key: str) -> list[int]:
    """Return sorted step indices inside a branch (e.g. ``steps-0-onfail-branches-1``)."""
    return _collect_indices(form, f"{branch_key}-steps")


def _parse_yaml_params(text: str, path: str) -> dict[str, Any]:
    stripped = text.strip()
    if not stripped:
        return {}
    try:
        parsed = yaml.safe_load(stripped)
    except yaml.YAMLError as exc:
        raise FormConversionError([FieldError(path, f"invalid YAML: {exc}")]) from exc
    if parsed is None:
        return {}
    if not isinstance(parsed, dict):
        raise FormConversionError([FieldError(path, "params must be a YAML mapping")])
    return parsed


def _failure_dict_from_form(
    form: Mapping[str, str],
    prefix: str,
    *,
    error_prefix: str,
) -> dict[str, Any]:
    retries_raw = form.get(f"{prefix}-retries", "0") or "0"
    try:
        retries = int(retries_raw)
    except ValueError as exc:
        raise FormConversionError(
            [FieldError(f"{error_prefix}.retries", "must be an integer")]
        ) from exc
    then = form.get(f"{prefix}-then", "mark_failed") or "mark_failed"
    branches = _parse_branch_list(form, prefix, error_prefix=error_prefix)
    result: dict[str, Any] = {"retries": retries, "branches": branches, "then": then}
    return result


def _parse_branch_list(
    form: Mapping[str, str],
    prefix: str,
    *,
    error_prefix: str,
) -> list[dict[str, Any]]:
    branch_prefix = f"{prefix}-branches"
    indices = _collect_indices(form, branch_prefix)
    branches: list[dict[str, Any]] = []
    for branch_index in indices:
        branch_key = f"{branch_prefix}-{branch_index}"
        name = form.get(f"{branch_key}-name", "").strip()
        if not name:
            continue
        retries_raw = form.get(f"{branch_key}-retries", "0") or "0"
        try:
            retries = int(retries_raw)
        except ValueError as exc:
            raise FormConversionError(
                [
                    FieldError(
                        f"{error_prefix}.branches.{branch_index}.retries",
                        "must be an integer",
                    )
                ]
            ) from exc
        step_indices = _collect_indices(form, f"{branch_key}-steps")
        steps: list[dict[str, Any]] = []
        for step_index in step_indices:
            step_id = form.get(f"{branch_key}-steps-{step_index}-id", "").strip()
            if not step_id:
                continue
            params_path = f"{error_prefix}.branches.{branch_index}.steps.{step_index}.params"
            params = _parse_yaml_params(
                form.get(f"{branch_key}-steps-{step_index}-params", ""),
                params_path,
            )
            steps.append({"id": step_id, "params": params})
        if steps:
            branches.append({"name": name, "retries": retries, "steps": steps})
    return branches


def _parse_trigger(form: Mapping[str, str]) -> dict[str, Any]:
    trigger_type = (form.get("trigger-type") or "manual").strip()
    path = (form.get("trigger-path") or "").strip()
    result: dict[str, Any] = {"type": trigger_type, "path": path}
    glob_val = (form.get("trigger-glob") or "*").strip() or "*"
    if glob_val != "*":
        result["glob"] = glob_val
    if trigger_type == "folder_watch":
        settle_raw = form.get("trigger-settle_seconds", "2") or "2"
        try:
            result["settle_seconds"] = float(settle_raw)
        except ValueError as exc:
            raise FormConversionError(
                [FieldError("trigger.settle_seconds", "must be a number")]
            ) from exc
    if trigger_type == "manifest":
        poll_raw = form.get("trigger-poll_seconds", "30") or "30"
        try:
            result["poll_seconds"] = float(poll_raw)
        except ValueError as exc:
            raise FormConversionError(
                [FieldError("trigger.poll_seconds", "must be a number")]
            ) from exc
    ordinal = (form.get("trigger-ordinal_regex") or "").strip()
    if ordinal:
        result["ordinal_regex"] = ordinal
    if form.get("trigger-arrival_order_ordinals") == "on":
        result["arrival_order_ordinals"] = True
    return result


def _parse_steps(form: Mapping[str, str]) -> list[dict[str, Any]]:
    indices = _collect_indices(form, "steps")
    steps: list[dict[str, Any]] = []
    for step_index in indices:
        step_id = form.get(f"steps-{step_index}-id", "").strip()
        if not step_id:
            continue
        params = _parse_yaml_params(
            form.get(f"steps-{step_index}-params", ""),
            f"steps.{step_index}.params",
        )
        step_dict: dict[str, Any] = {"id": step_id, "params": params}
        if form.get(f"steps-{step_index}-onfail-enabled") == "on":
            step_dict["on_failure"] = _failure_dict_from_form(
                form,
                f"steps-{step_index}-onfail",
                error_prefix=f"steps.{step_index}.on_failure",
            )
        steps.append(step_dict)
    if not steps:
        raise FormConversionError([FieldError("steps", "at least one step is required")])
    return steps


def form_to_dict(form: Mapping[str, str]) -> dict[str, Any]:
    """Build a playbook dict from flat editor form field names."""
    name = (form.get("name") or "").strip()
    if not name:
        raise FormConversionError([FieldError("name", "name is required")])
    data: dict[str, Any] = {
        "version": 1,
        "name": name,
        "trigger": _parse_trigger(form),
        "steps": _parse_steps(form),
    }
    description = (form.get("description") or "").strip()
    if description:
        data["description"] = description
    engine = (form.get("engine") or "headless").strip() or "headless"
    if engine != "headless":
        data["engine"] = engine
    dedup = (form.get("dedup") or "content_hash").strip() or "content_hash"
    if dedup != "content_hash":
        data["dedup"] = dedup
    if form.get("onfail-enabled") == "on":
        data["on_failure"] = _failure_dict_from_form(form, "onfail", error_prefix="on_failure")
    return data


def _params_to_text(params: dict[str, Any]) -> str:
    if not params:
        return ""
    dumped: str = yaml.safe_dump(params, sort_keys=False, default_flow_style=False)
    return dumped.strip()


def _failure_to_form(prefix: str, policy: FailurePolicy) -> dict[str, str]:
    if _is_default_failure_policy(policy):
        return {}
    result: dict[str, str] = {
        f"{prefix}-enabled": "on",
        f"{prefix}-retries": str(policy.retries),
        f"{prefix}-then": policy.then,
    }
    for branch_index, branch in enumerate(policy.branches):
        branch_key = f"{prefix}-branches-{branch_index}"
        result[f"{branch_key}-name"] = branch.name
        result[f"{branch_key}-retries"] = str(branch.retries)
        for step_index, step in enumerate(branch.steps):
            result[f"{branch_key}-steps-{step_index}-id"] = step.id
            result[f"{branch_key}-steps-{step_index}-params"] = _params_to_text(step.params)
    return result


def _step_to_form(step_index: int, step: StepSpec) -> dict[str, str]:
    result: dict[str, str] = {
        f"steps-{step_index}-id": step.id,
        f"steps-{step_index}-params": _params_to_text(step.params),
    }
    if step.on_failure is not None:
        result.update(_failure_to_form(f"steps-{step_index}-onfail", step.on_failure))
    return result


def playbook_to_form(playbook: Playbook) -> dict[str, str]:
    """Convert a Playbook to flat form field values for template rendering."""
    trigger = playbook.trigger
    result: dict[str, str] = {
        "name": playbook.name,
        "engine": playbook.engine,
        "dedup": playbook.dedup,
        "trigger-type": trigger.type,
        "trigger-path": trigger.path,
        "trigger-glob": trigger.glob if hasattr(trigger, "glob") else "*",
    }
    if playbook.description:
        result["description"] = playbook.description
    if trigger.type == "folder_watch":
        result["trigger-settle_seconds"] = str(trigger.settle_seconds)
    if trigger.type == "manifest":
        result["trigger-poll_seconds"] = str(trigger.poll_seconds)
    if hasattr(trigger, "ordinal_regex") and trigger.ordinal_regex is not None:
        result["trigger-ordinal_regex"] = trigger.ordinal_regex
    if hasattr(trigger, "arrival_order_ordinals") and trigger.arrival_order_ordinals:
        result["trigger-arrival_order_ordinals"] = "on"
    for step_index, step in enumerate(playbook.steps):
        result.update(_step_to_form(step_index, step))
    result.update(_failure_to_form("onfail", playbook.on_failure))
    return result


def playbook_dict_to_yaml(data: dict[str, Any]) -> str:
    """Validate dict as Playbook and return canonical dumped YAML."""
    playbook = Playbook.model_validate(data)
    return dump_playbook(playbook)


def parse_editor_yaml(yaml_text: str) -> dict[str, Any]:
    """Parse YAML tab content into a playbook dict, surfacing syntax errors."""
    from conveyor.core.playbook import loads_playbook

    playbook = loads_playbook(yaml_text, source="<editor>")
    return playbook.model_dump(mode="python")


def form_to_playbook(form: Mapping[str, str]) -> Playbook:
    """Convert form fields to a validated Playbook model."""
    return Playbook.model_validate(form_to_dict(form))


def playbook_to_yaml(playbook: Playbook) -> str:
    """Serialize a playbook for the YAML editor tab."""
    return dump_playbook(playbook)


def validate_editor_content(
    registry: object,
    *,
    tab: Literal["form", "yaml"],
    form: Mapping[str, str] | None = None,
    yaml_text: str | None = None,
) -> tuple[Playbook | None, str, list[FieldError]]:
    """Validate editor form or YAML tab content without persisting a version."""
    from conveyor.core.errors import PlaybookSyntaxError, PlaybookValidationError
    from conveyor.core.playbook import loads_playbook
    from conveyor.core.registry import StepRegistry

    assert isinstance(registry, StepRegistry)
    errors: list[FieldError] = []
    text = yaml_text or ""
    try:
        if tab == "yaml":
            playbook = loads_playbook(text, source="<editor>")
            text = dump_playbook(playbook)
        else:
            data = form_to_dict(form or {})
            playbook = Playbook.model_validate(data)
            text = dump_playbook(playbook)
    except FormConversionError as exc:
        return None, text, exc.errors
    except PlaybookSyntaxError as exc:
        return None, text, [FieldError("yaml_text", str(exc))]
    except PlaybookValidationError as exc:
        return None, text, list(exc.errors)
    errors.extend(registry.check_playbook(playbook))
    return playbook, text, errors
