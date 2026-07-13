"""Natural-language playbook drafting with validation ladder.

Owns draft/revise/repair flows. Must never save playbooks or start pipeline runs.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from ordine.core.errors import FieldError
from ordine.core.playbook import Playbook, loads_playbook
from ordine.core.registry import StepRegistry
from ordine.llm import prompts
from ordine.llm.features.context import step_catalog
from ordine.llm.types import LLMClient, Message

_FENCE_RE = re.compile(r"^```(?:yaml)?\s*\n?(.*?)```\s*$", re.DOTALL | re.IGNORECASE)


@dataclass(frozen=True)
class DraftResult:
    yaml_text: str
    playbook: Playbook | None
    problems: list[FieldError]
    repaired: bool
    raw: str


def _strip_fences(text: str) -> str:
    stripped = text.strip()
    match = _FENCE_RE.match(stripped)
    if match:
        return match.group(1).strip()
    if stripped.startswith("```"):
        return stripped.strip("`").removeprefix("yaml").strip()
    return stripped


def _validate_yaml(
    registry: StepRegistry, yaml_text: str
) -> tuple[Playbook | None, list[FieldError]]:
    try:
        playbook = loads_playbook(yaml_text)
    except Exception as exc:
        return None, [FieldError("yaml_text", str(exc))]
    return playbook, registry.check_playbook(playbook)


def _format_problems(problems: list[FieldError]) -> str:
    return "\n".join(f"{err.path}: {err.message}" for err in problems)


def draft_playbook(
    client: LLMClient,
    registry: StepRegistry,
    description: str,
    *,
    current_yaml: str | None = None,
) -> DraftResult:
    """Draft or revise a playbook from natural language."""
    catalog = step_catalog(registry)
    purpose = "revise_playbook" if current_yaml else "draft_playbook"
    user_parts = [
        f"Step catalog:\n{catalog}",
        f"Description:\n{description}",
    ]
    if current_yaml is not None:
        user_parts.append(prompts.DRAFT_REVISE_SUFFIX.format(current_yaml=current_yaml))
    messages = [
        Message(role="system", content=prompts.DRAFT_SYSTEM),
        Message(role="user", content="\n\n".join(user_parts)),
    ]
    response = client.complete(messages, purpose=purpose)
    raw = response.text
    yaml_text = _strip_fences(raw)
    playbook, problems = _validate_yaml(registry, yaml_text)
    repaired = False
    if problems:
        repair_messages = [
            *messages,
            Message(role="assistant", content=raw),
            Message(
                role="user",
                content=prompts.DRAFT_REPAIR_SUFFIX.format(
                    errors=_format_problems(problems),
                    yaml_text=yaml_text,
                ),
            ),
        ]
        repair = client.complete(repair_messages, purpose="repair_playbook")
        raw = repair.text
        yaml_text = _strip_fences(raw)
        playbook, problems = _validate_yaml(registry, yaml_text)
        repaired = True
    return DraftResult(
        yaml_text=yaml_text,
        playbook=None if problems else playbook,
        problems=problems,
        repaired=repaired,
        raw=raw,
    )
