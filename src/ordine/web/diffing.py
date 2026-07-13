"""Structured playbook change summaries and side-by-side diff presentation.

Owns model-level diff semantics for the web UI. Must never touch the ledger or mutate playbooks.
"""

from __future__ import annotations

import difflib
from dataclasses import dataclass
from typing import Any, Literal

from ordine.core.playbook import FailurePolicy, Playbook, RecoveryBranch, StepSpec

ChangeKind = Literal["added", "removed", "changed"]
ChangeScope = Literal["trigger", "step", "branch", "params", "policy", "pipeline"]
RowKind = Literal["equal", "add", "delete", "replace"]


@dataclass(frozen=True)
class ChangeItem:
    """One human-readable playbook change between two parsed versions."""

    kind: ChangeKind
    scope: ChangeScope
    location: str
    description: str


@dataclass(frozen=True)
class SideBySideRow:
    """One row in a side-by-side YAML line diff table."""

    old_line_no: int | None
    old_text: str
    new_line_no: int | None
    new_text: str
    kind: RowKind


def _is_default_policy(policy: FailurePolicy) -> bool:
    return policy.retries == 0 and not policy.branches and policy.then == "mark_failed"


def _step_label(index: int, step_id: str) -> str:
    return f"Step {index + 1} (steps.{index}) · {step_id}"


def _append_scalar_change(
    items: list[ChangeItem],
    *,
    scope: ChangeScope,
    location: str,
    label: str,
    old_value: object,
    new_value: object,
) -> None:
    if old_value == new_value:
        return
    items.append(
        ChangeItem(
            kind="changed",
            scope=scope,
            location=location,
            description=f"{label} changed from {old_value!r} to {new_value!r}.",
        )
    )


def _compare_trigger(old: Playbook, new: Playbook, items: list[ChangeItem]) -> None:
    old_dump = old.trigger.model_dump()
    new_dump = new.trigger.model_dump()
    if old_dump.get("type") != new_dump.get("type"):
        items.append(
            ChangeItem(
                kind="changed",
                scope="trigger",
                location="Trigger",
                description=(
                    f"Trigger type changed from {old_dump.get('type')!r} "
                    f"to {new_dump.get('type')!r}."
                ),
            )
        )
    for key in sorted(set(old_dump) | set(new_dump)):
        if key == "type":
            continue
        old_val = old_dump.get(key)
        new_val = new_dump.get(key)
        if old_val != new_val:
            items.append(
                ChangeItem(
                    kind="changed",
                    scope="trigger",
                    location=f"Trigger · {key}",
                    description=f"Trigger {key} changed from {old_val!r} to {new_val!r}.",
                )
            )


def _compare_params(
    old_params: dict[str, Any],
    new_params: dict[str, Any],
    *,
    location: str,
    items: list[ChangeItem],
) -> None:
    for key in sorted(set(old_params) | set(new_params)):
        param_location = f"{location} · params.{key}"
        if key not in old_params:
            items.append(
                ChangeItem(
                    kind="added",
                    scope="params",
                    location=param_location,
                    description=f"Added param {key}.",
                )
            )
        elif key not in new_params:
            items.append(
                ChangeItem(
                    kind="removed",
                    scope="params",
                    location=param_location,
                    description=f"Removed param {key}.",
                )
            )
        elif old_params[key] != new_params[key]:
            items.append(
                ChangeItem(
                    kind="changed",
                    scope="params",
                    location=param_location,
                    description=(
                        f"Param {key} changed from {old_params[key]!r} to {new_params[key]!r}."
                    ),
                )
            )


def _compare_branch_steps(
    old_steps: list[StepSpec],
    new_steps: list[StepSpec],
    *,
    location: str,
    items: list[ChangeItem],
) -> None:
    old_ids = [step.id for step in old_steps]
    new_ids = [step.id for step in new_steps]
    matcher = difflib.SequenceMatcher(None, old_ids, new_ids)
    for tag, i1, i2, j1, j2 in matcher.get_opcodes():
        if tag == "equal":
            for offset in range(i2 - i1):
                old_step = old_steps[i1 + offset]
                new_step = new_steps[j1 + offset]
                step_loc = f"{location} · branch step {j1 + offset + 1}"
                _compare_params(
                    old_step.params,
                    new_step.params,
                    location=step_loc,
                    items=items,
                )
        elif tag == "insert":
            for index in range(j1, j2):
                step = new_steps[index]
                items.append(
                    ChangeItem(
                        kind="added",
                        scope="step",
                        location=f"{location} · branch step {index + 1}",
                        description=f"Added branch step {step.id}.",
                    )
                )
        elif tag == "delete":
            for index in range(i1, i2):
                step = old_steps[index]
                items.append(
                    ChangeItem(
                        kind="removed",
                        scope="step",
                        location=f"{location} · branch step {index + 1}",
                        description=f"Removed branch step {step.id}.",
                    )
                )
        elif tag == "replace":
            for index in range(i1, i2):
                step = old_steps[index]
                items.append(
                    ChangeItem(
                        kind="removed",
                        scope="step",
                        location=f"{location} · branch step {index + 1}",
                        description=f"Removed branch step {step.id}.",
                    )
                )
            for index in range(j1, j2):
                step = new_steps[index]
                items.append(
                    ChangeItem(
                        kind="added",
                        scope="step",
                        location=f"{location} · branch step {index + 1}",
                        description=f"Added branch step {step.id}.",
                    )
                )


def _compare_branches(
    old_branches: list[RecoveryBranch],
    new_branches: list[RecoveryBranch],
    *,
    location: str,
    items: list[ChangeItem],
) -> None:
    old_by_name = {branch.name: branch for branch in old_branches}
    new_by_name = {branch.name: branch for branch in new_branches}
    for name in sorted(set(old_by_name) - set(new_by_name)):
        items.append(
            ChangeItem(
                kind="removed",
                scope="branch",
                location=f"{location} · branch {name}",
                description=f"Removed recovery branch {name}.",
            )
        )
    for name in sorted(set(new_by_name) - set(old_by_name)):
        items.append(
            ChangeItem(
                kind="added",
                scope="branch",
                location=f"{location} · branch {name}",
                description=f"Added recovery branch {name}.",
            )
        )
    for name in sorted(set(old_by_name) & set(new_by_name)):
        old_branch = old_by_name[name]
        new_branch = new_by_name[name]
        branch_location = f"{location} · branch {name}"
        if old_branch.retries != new_branch.retries:
            items.append(
                ChangeItem(
                    kind="changed",
                    scope="branch",
                    location=branch_location,
                    description=(
                        f"Branch {name} retries changed from {old_branch.retries} "
                        f"to {new_branch.retries}."
                    ),
                )
            )
        _compare_branch_steps(
            old_branch.steps,
            new_branch.steps,
            location=branch_location,
            items=items,
        )


def _effective_policy(policy: FailurePolicy | None) -> FailurePolicy:
    return policy if policy is not None else FailurePolicy()


def _compare_failure_policy(
    old: FailurePolicy | None,
    new: FailurePolicy | None,
    *,
    location: str,
    items: list[ChangeItem],
) -> None:
    old_policy = _effective_policy(old)
    new_policy = _effective_policy(new)
    had_policy = old is not None and not _is_default_policy(old)
    has_policy = new is not None and not _is_default_policy(new)
    if not had_policy and has_policy and not new_policy.branches:
        items.append(
            ChangeItem(
                kind="added",
                scope="policy",
                location=location,
                description="Added recovery policy.",
            )
        )
    elif had_policy and not has_policy and not old_policy.branches:
        items.append(
            ChangeItem(
                kind="removed",
                scope="policy",
                location=location,
                description="Removed recovery policy.",
            )
        )
    if had_policy and has_policy:
        if old_policy.retries != new_policy.retries:
            items.append(
                ChangeItem(
                    kind="changed",
                    scope="policy",
                    location=location,
                    description=(
                        f"Recovery retries changed from {old_policy.retries} "
                        f"to {new_policy.retries}."
                    ),
                )
            )
        if old_policy.then != new_policy.then:
            items.append(
                ChangeItem(
                    kind="changed",
                    scope="policy",
                    location=location,
                    description=(
                        f"Recovery then changed from {old_policy.then!r} to {new_policy.then!r}."
                    ),
                )
            )
    if had_policy or has_policy:
        _compare_branches(old_policy.branches, new_policy.branches, location=location, items=items)


def _compare_matched_steps(
    old_step: StepSpec,
    new_step: StepSpec,
    *,
    new_index: int,
    items: list[ChangeItem],
) -> None:
    location = _step_label(new_index, new_step.id)
    _compare_params(old_step.params, new_step.params, location=location, items=items)
    _compare_failure_policy(
        old_step.on_failure,
        new_step.on_failure,
        location=location,
        items=items,
    )


def _compare_steps(
    old_steps: list[StepSpec],
    new_steps: list[StepSpec],
    items: list[ChangeItem],
) -> None:
    old_ids = [step.id for step in old_steps]
    new_ids = [step.id for step in new_steps]
    matcher = difflib.SequenceMatcher(None, old_ids, new_ids)
    for tag, i1, i2, j1, j2 in matcher.get_opcodes():
        if tag == "equal":
            for offset in range(i2 - i1):
                _compare_matched_steps(
                    old_steps[i1 + offset],
                    new_steps[j1 + offset],
                    new_index=j1 + offset,
                    items=items,
                )
        elif tag == "insert":
            for index in range(j1, j2):
                step = new_steps[index]
                items.append(
                    ChangeItem(
                        kind="added",
                        scope="step",
                        location=_step_label(index, step.id),
                        description=f"Added step {step.id}.",
                    )
                )
        elif tag == "delete":
            for index in range(i1, i2):
                step = old_steps[index]
                items.append(
                    ChangeItem(
                        kind="removed",
                        scope="step",
                        location=_step_label(index, step.id),
                        description=f"Removed step {step.id}.",
                    )
                )
        elif tag == "replace":
            for index in range(i1, i2):
                step = old_steps[index]
                items.append(
                    ChangeItem(
                        kind="removed",
                        scope="step",
                        location=_step_label(index, step.id),
                        description=f"Removed step {step.id}.",
                    )
                )
            for index in range(j1, j2):
                step = new_steps[index]
                items.append(
                    ChangeItem(
                        kind="added",
                        scope="step",
                        location=_step_label(index, step.id),
                        description=f"Added step {step.id}.",
                    )
                )


def summarize_playbook_changes(old: Playbook, new: Playbook) -> list[ChangeItem]:
    """Summarize semantic differences between two parsed playbooks."""
    items: list[ChangeItem] = []
    _append_scalar_change(
        items,
        scope="pipeline",
        location="Name",
        label="Name",
        old_value=old.name,
        new_value=new.name,
    )
    _append_scalar_change(
        items,
        scope="pipeline",
        location="Description",
        label="Description",
        old_value=old.description,
        new_value=new.description,
    )
    _append_scalar_change(
        items,
        scope="pipeline",
        location="Engine",
        label="Engine",
        old_value=old.engine,
        new_value=new.engine,
    )
    _append_scalar_change(
        items,
        scope="pipeline",
        location="Dedup",
        label="Dedup",
        old_value=old.dedup,
        new_value=new.dedup,
    )
    _compare_trigger(old, new, items)
    _compare_steps(old.steps, new.steps, items)
    _compare_failure_policy(
        old.on_failure,
        new.on_failure,
        location="Pipeline on_failure",
        items=items,
    )
    return items


def side_by_side_rows(left_lines: list[str], right_lines: list[str]) -> list[SideBySideRow]:
    """Build aligned rows for a two-column YAML line diff table."""
    rows: list[SideBySideRow] = []
    matcher = difflib.SequenceMatcher(None, left_lines, right_lines)
    for tag, i1, i2, j1, j2 in matcher.get_opcodes():
        if tag == "equal":
            for offset in range(i2 - i1):
                rows.append(
                    SideBySideRow(
                        old_line_no=i1 + offset + 1,
                        old_text=left_lines[i1 + offset],
                        new_line_no=j1 + offset + 1,
                        new_text=right_lines[j1 + offset],
                        kind="equal",
                    )
                )
        elif tag == "delete":
            for offset in range(i2 - i1):
                rows.append(
                    SideBySideRow(
                        old_line_no=i1 + offset + 1,
                        old_text=left_lines[i1 + offset],
                        new_line_no=None,
                        new_text="",
                        kind="delete",
                    )
                )
        elif tag == "insert":
            for offset in range(j2 - j1):
                rows.append(
                    SideBySideRow(
                        old_line_no=None,
                        old_text="",
                        new_line_no=j1 + offset + 1,
                        new_text=right_lines[j1 + offset],
                        kind="add",
                    )
                )
        elif tag == "replace":
            old_chunk = left_lines[i1:i2]
            new_chunk = right_lines[j1:j2]
            span = max(len(old_chunk), len(new_chunk))
            for offset in range(span):
                old_text = old_chunk[offset] if offset < len(old_chunk) else ""
                new_text = new_chunk[offset] if offset < len(new_chunk) else ""
                old_line_no = i1 + offset + 1 if offset < len(old_chunk) else None
                new_line_no = j1 + offset + 1 if offset < len(new_chunk) else None
                if old_text and new_text:
                    kind: RowKind = "replace" if old_text != new_text else "equal"
                elif old_text:
                    kind = "delete"
                else:
                    kind = "add"
                rows.append(
                    SideBySideRow(
                        old_line_no=old_line_no,
                        old_text=old_text,
                        new_line_no=new_line_no,
                        new_text=new_text,
                        kind=kind,
                    )
                )
    return rows
