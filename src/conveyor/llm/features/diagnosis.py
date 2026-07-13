"""Failure diagnosis via LLM with workdir JSON persistence.

Owns diagnose/repair parsing and _diagnosis file IO. Must never mutate the ledger schema.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal

from conveyor.core.ledger import Ledger
from conveyor.core.registry import StepRegistry
from conveyor.llm import prompts
from conveyor.llm.errors import LLMResponseError
from conveyor.llm.features.context import failure_context, step_catalog
from conveyor.llm.types import ImagePart, LLMClient, Message, TextPart

Confidence = Literal["low", "medium", "high"]
_FENCE_RE = re.compile(r"^```(?:json)?\s*\n?(.*?)```\s*$", re.DOTALL | re.IGNORECASE)


@dataclass(frozen=True)
class Diagnosis:
    cause: str
    confidence: Confidence
    evidence: list[str]
    suggestions: list[str]
    fixable_by_branch: bool
    raw: str
    diagnosed_at: datetime | None = None
    model: str | None = None


def _strip_json_fences(text: str) -> str:
    stripped = text.strip()
    match = _FENCE_RE.match(stripped)
    if match:
        return match.group(1).strip()
    return stripped


def _parse_diagnosis(raw: str) -> Diagnosis:
    payload = json.loads(_strip_json_fences(raw))
    if not isinstance(payload, dict):
        raise LLMResponseError("diagnosis JSON must be an object")
    confidence = payload.get("confidence")
    if confidence not in ("low", "medium", "high"):
        raise LLMResponseError("diagnosis confidence must be low|medium|high")
    evidence = payload.get("evidence", [])
    suggestions = payload.get("suggestions", [])
    if not isinstance(evidence, list) or not all(isinstance(x, str) for x in evidence):
        raise LLMResponseError("diagnosis evidence must be a string array")
    if not isinstance(suggestions, list) or not all(isinstance(x, str) for x in suggestions):
        raise LLMResponseError("diagnosis suggestions must be a string array")
    cause = payload.get("cause")
    if not isinstance(cause, str):
        raise LLMResponseError("diagnosis cause must be a string")
    fixable = payload.get("fixable_by_branch")
    if not isinstance(fixable, bool):
        raise LLMResponseError("diagnosis fixable_by_branch must be a boolean")
    return Diagnosis(
        cause=cause,
        confidence=confidence,
        evidence=evidence,
        suggestions=suggestions,
        fixable_by_branch=fixable,
        raw=raw,
    )


def diagnose(
    client: LLMClient,
    registry: StepRegistry,
    ledger: Ledger,
    task_id: int,
    workdir_root: Path,
    *,
    include_image: bool = False,
) -> Diagnosis:
    """Diagnose a failing task; raises LLMResponseError if JSON stays malformed."""
    context, images = failure_context(ledger, task_id, workdir_root, include_image=include_image)
    catalog = step_catalog(registry)
    user_text = f"Catalog:\n{catalog}\n\nFailure context:\n{context}"
    user_content: str | list[TextPart | ImagePart] = user_text
    if images:
        user_content = [TextPart(user_text), *images]
    messages = [
        Message(role="system", content=prompts.DIAGNOSE_SYSTEM),
        Message(role="user", content=user_content),
    ]
    response = client.complete(messages, purpose="diagnose_failure")
    raw = response.text
    try:
        diagnosis = _parse_diagnosis(raw)
    except (json.JSONDecodeError, LLMResponseError):
        repair = client.complete(
            [
                *messages,
                Message(role="assistant", content=raw),
                Message(role="user", content=prompts.DIAGNOSE_REPAIR_SUFFIX.format(raw=raw)),
            ],
            purpose="diagnose_failure",
        )
        raw = repair.text
        try:
            diagnosis = _parse_diagnosis(raw)
        except (json.JSONDecodeError, LLMResponseError) as exc:
            raise LLMResponseError(f"diagnosis JSON invalid after repair: {exc}") from exc
    return Diagnosis(
        cause=diagnosis.cause,
        confidence=diagnosis.confidence,
        evidence=diagnosis.evidence,
        suggestions=diagnosis.suggestions,
        fixable_by_branch=diagnosis.fixable_by_branch,
        raw=raw,
        diagnosed_at=datetime.now(tz=UTC),
        model=client.model,
    )


def diagnosis_path(workdir: Path, flag_id: int) -> Path:
    return workdir / "_diagnosis" / f"flag_{flag_id}.json"


def save_diagnosis(workdir: Path, flag_id: int, diagnosis: Diagnosis) -> Path:
    path = diagnosis_path(workdir, flag_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "cause": diagnosis.cause,
        "confidence": diagnosis.confidence,
        "evidence": diagnosis.evidence,
        "suggestions": diagnosis.suggestions,
        "fixable_by_branch": diagnosis.fixable_by_branch,
        "raw": diagnosis.raw,
        "diagnosed_at": (diagnosis.diagnosed_at or datetime.now(tz=UTC)).isoformat(),
        "model": diagnosis.model,
    }
    path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    return path


def load_diagnosis(workdir: Path, flag_id: int) -> Diagnosis | None:
    path = diagnosis_path(workdir, flag_id)
    if not path.exists():
        return None
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        return None
    diagnosed_at = None
    if isinstance(payload.get("diagnosed_at"), str):
        diagnosed_at = datetime.fromisoformat(payload["diagnosed_at"])
    confidence_raw = payload.get("confidence", "low")
    confidence: Confidence = (
        confidence_raw if confidence_raw in {"low", "medium", "high"} else "low"
    )
    return Diagnosis(
        cause=str(payload.get("cause", "")),
        confidence=confidence,
        evidence=list(payload.get("evidence", [])),
        suggestions=list(payload.get("suggestions", [])),
        fixable_by_branch=bool(payload.get("fixable_by_branch", False)),
        raw=str(payload.get("raw", "")),
        diagnosed_at=diagnosed_at,
        model=str(payload.get("model")) if payload.get("model") else None,
    )
