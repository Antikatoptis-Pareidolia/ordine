"""Recovery branch suggestion and explicit approval apply flow.

Owns branch grafting/validation. apply_branch is the ONLY path that mutates playbook versions.
"""

from __future__ import annotations

import difflib
import json
import re
from dataclasses import dataclass
from pathlib import Path

from ordine.core.errors import FieldError
from ordine.core.ledger import Ledger
from ordine.core.playbook import (
    FailurePolicy,
    Playbook,
    RecoveryBranch,
    StepSpec,
    dump_playbook,
    loads_playbook,
)
from ordine.core.registry import StepRegistry
from ordine.llm import prompts
from ordine.llm.features.context import failure_context, step_catalog
from ordine.llm.types import LLMClient, Message

_FENCE_RE = re.compile(r"^```(?:json)?\s*\n?(.*?)```\s*$", re.DOTALL | re.IGNORECASE)


@dataclass(frozen=True)
class BranchSuggestion:
    branch: RecoveryBranch
    rationale: str
    target_step_index: int
    new_playbook: Playbook | None
    new_yaml: str
    diff: str
    raw: str
    problems: list[FieldError]


def _strip_json_fences(text: str) -> str:
    stripped = text.strip()
    match = _FENCE_RE.match(stripped)
    if match:
        return match.group(1).strip()
    return stripped


def _effective_policy(step: StepSpec, playbook: Playbook) -> FailurePolicy:
    return step.on_failure if step.on_failure is not None else playbook.on_failure


def _all_playbook_branch_names(playbook: Playbook) -> set[str]:
    names = {branch.name for branch in playbook.on_failure.branches}
    for step in playbook.steps:
        if step.on_failure is not None:
            names.update(branch.name for branch in step.on_failure.branches)
    return names


def _unique_branch_name(playbook: Playbook, desired: str) -> str:
    names = _all_playbook_branch_names(playbook)
    if desired not in names:
        return desired
    candidate = f"{desired}-2"
    suffix = 2
    while candidate in names:
        suffix += 1
        candidate = f"{desired}-{suffix}"
    return candidate


def _graft_branch(playbook: Playbook, *, step_index: int, branch: RecoveryBranch) -> Playbook:
    modified = playbook.model_copy(deep=True)
    step = modified.steps[step_index]
    if step.on_failure is None:
        effective = _effective_policy(step, playbook)
        step.on_failure = FailurePolicy(
            retries=effective.retries,
            branches=list(effective.branches),
            then=effective.then,
        )
    if step.on_failure is None:
        raise RuntimeError("internal error: on_failure policy missing after initialization")
    unique_name = _unique_branch_name(modified, branch.name)
    grafted = branch.model_copy(update={"name": unique_name})
    step.on_failure.branches.append(grafted)
    return modified


def _step_index_from_context(ledger: Ledger, task_id: int, playbook: Playbook) -> int:
    attempts = ledger.list_branch_attempts(task_id)
    if attempts and attempts[-1].last_step_id:
        last_id = attempts[-1].last_step_id
        for index, step in enumerate(playbook.steps):
            if step.id == last_id:
                return index
    return 0


def _parse_branch_payload(raw: str) -> tuple[RecoveryBranch, str]:
    payload = json.loads(_strip_json_fences(raw))
    if not isinstance(payload, dict):
        raise ValueError("branch JSON must be an object")
    branch_raw = payload.get("branch")
    rationale = payload.get("rationale")
    if not isinstance(branch_raw, dict) or not isinstance(rationale, str):
        raise ValueError("branch JSON missing branch or rationale")
    branch = RecoveryBranch.model_validate(branch_raw)
    return branch, rationale


def _validate_suggestion(
    registry: StepRegistry,
    playbook: Playbook,
    *,
    step_index: int,
    branch: RecoveryBranch,
    rationale: str,
    raw: str,
) -> BranchSuggestion:
    modified = _graft_branch(playbook, step_index=step_index, branch=branch)
    problems = registry.check_playbook(modified)
    new_yaml = dump_playbook(modified)
    diff_text = "\n".join(
        difflib.unified_diff(
            dump_playbook(playbook).splitlines(),
            new_yaml.splitlines(),
            fromfile="current",
            tofile="proposed",
            lineterm="",
        )
    )
    return BranchSuggestion(
        branch=branch,
        rationale=rationale,
        target_step_index=step_index,
        new_playbook=None if problems else modified,
        new_yaml=new_yaml,
        diff=diff_text,
        raw=raw,
        problems=problems,
    )


def suggest_branch(
    client: LLMClient,
    registry: StepRegistry,
    ledger: Ledger,
    task_id: int,
    workdir_root: Path,
) -> BranchSuggestion:
    """Suggest a recovery branch for a failing task."""
    task = ledger.get_task(task_id)
    _public_id, yaml_text = ledger.get_current_playbook(task.pipeline_id)
    playbook = loads_playbook(yaml_text)
    step_index = _step_index_from_context(ledger, task_id, playbook)
    step = playbook.steps[step_index]
    policy = _effective_policy(step, playbook)
    context, _images = failure_context(ledger, task_id, workdir_root, include_image=False)
    catalog = step_catalog(registry)
    messages = [
        Message(role="system", content=prompts.BRANCH_SYSTEM),
        Message(
            role="user",
            content=(
                f"Catalog:\n{catalog}\n\n"
                f"Failing step index: {step_index}\n"
                f"Failing step id: {step.id}\n"
                f"Effective on_failure: {policy.model_dump()}\n\n"
                f"Failure context:\n{context}"
            ),
        ),
    ]
    response = client.complete(messages, purpose="suggest_branch")
    raw = response.text
    try:
        branch, rationale = _parse_branch_payload(raw)
        return _validate_suggestion(
            registry, playbook, step_index=step_index, branch=branch, rationale=rationale, raw=raw
        )
    except (json.JSONDecodeError, ValueError) as exc:
        first_error = str(exc)
        repair = client.complete(
            [
                *messages,
                Message(role="assistant", content=raw),
                Message(
                    role="user",
                    content=prompts.BRANCH_REPAIR_SUFFIX.format(errors=first_error, raw=raw),
                ),
            ],
            purpose="repair_branch",
        )
        raw = repair.text
        try:
            branch, rationale = _parse_branch_payload(raw)
            result = _validate_suggestion(
                registry,
                playbook,
                step_index=step_index,
                branch=branch,
                rationale=rationale,
                raw=raw,
            )
            return result
        except (json.JSONDecodeError, ValueError) as exc2:
            return BranchSuggestion(
                branch=RecoveryBranch(name="invalid", steps=[StepSpec(id="util.noop")]),
                rationale="",
                target_step_index=step_index,
                new_playbook=None,
                new_yaml=yaml_text,
                diff="",
                raw=raw,
                problems=[FieldError("branch", str(exc2))],
            )


def apply_branch(
    ledger: Ledger,
    pipeline_id: int,
    suggestion: BranchSuggestion,
    *,
    note: str,
) -> str:
    """Register an approved branch suggestion as a new current playbook version."""
    if suggestion.new_playbook is None:
        raise ValueError("cannot apply invalid branch suggestion")
    parent_public_id, _ = ledger.get_current_playbook(pipeline_id)
    new_yaml = dump_playbook(suggestion.new_playbook)
    _pipeline_id, version = ledger.register_pipeline(
        suggestion.new_playbook,
        new_yaml,
        note=note,
        parent_public_id=parent_public_id,
        make_current=True,
    )
    del _pipeline_id
    return version
