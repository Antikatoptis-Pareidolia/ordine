"""Bounded context assembly for LLM features.

Owns prompt context and catalog rendering. Must never call LLM providers directly.
"""

from __future__ import annotations

import base64
import json
import os
import re
from pathlib import Path

from conveyor.core.ledger import Ledger
from conveyor.core.playbook import loads_playbook
from conveyor.core.registry import StepRegistry
from conveyor.llm.types import ImagePart

MAX_CONTEXT_CHARS = 30_000
_TRUNCATED = "[...truncated]"
_LOG_TAIL_LINES = 100
_KEY_LIKE = re.compile(r"sk-[A-Za-z0-9]{8,}")


def step_catalog(registry: StepRegistry) -> str:
    """Compact JSON catalog: step id -> engines + param JSON Schema."""
    entries: dict[str, dict[str, object]] = {}
    for step_id, engines, _origin in registry.list_step_metadata():
        entries[step_id] = {
            "engines": sorted(engines),
            "params_schema": registry.param_schema(step_id),
        }
    return json.dumps(entries, indent=2, sort_keys=True)


def _basename_only(path: str) -> str:
    return Path(path).name


def _redact_secrets(text: str) -> str:
    return _KEY_LIKE.sub("<redacted>", text)


def _truncate_text(text: str, budget: int) -> str:
    if len(text) <= budget:
        return text
    if budget <= len(_TRUNCATED):
        return _TRUNCATED
    keep = budget - len(_TRUNCATED)
    return text[:keep] + _TRUNCATED


def _apply_char_cap(parts: list[str]) -> str:
    """Join parts and enforce MAX_CONTEXT_CHARS, dropping oldest parts first."""
    remaining = MAX_CONTEXT_CHARS
    kept: list[str] = []
    for part in reversed(parts):
        if remaining <= 0:
            break
        if len(part) <= remaining:
            kept.append(part)
            remaining -= len(part)
        else:
            kept.append(_truncate_text(part, remaining))
            remaining = 0
    kept.reverse()
    return "\n\n".join(kept)


def _read_log_tail(step_dir: Path, *, max_lines: int = _LOG_TAIL_LINES) -> str:
    log_path = step_dir / "log.txt"
    if not log_path.exists():
        return ""
    lines = log_path.read_text(encoding="utf-8", errors="replace").splitlines()
    return "\n".join(lines[-max_lines:])


def failure_context(
    ledger: Ledger,
    task_id: int,
    workdir_root: Path,
    *,
    include_image: bool,
) -> tuple[str, list[ImagePart]]:
    """Assemble failure context text and optional input image parts."""
    task = ledger.get_task(task_id)
    _public_id, yaml_text = ledger.get_current_playbook(task.pipeline_id)
    playbook = loads_playbook(yaml_text)
    attempts = ledger.list_branch_attempts(task_id)
    flags = [
        flag for flag in ledger.open_flags(pipeline_id=task.pipeline_id) if flag.task_id == task_id
    ]

    parts: list[str] = [
        f"playbook_yaml:\n{_redact_secrets(yaml_text)}",
        (
            "task:\n"
            f"  id: {task.id}\n"
            f"  ordinal: {task.ordinal}\n"
            f"  source: {_basename_only(task.source_ref)}\n"
            f"  status: {task.status}\n"
            f"  error: {_redact_secrets(task.error or '')}"
        ),
    ]

    if attempts:
        attempt_lines = [
            f"- #{a.attempt_no} branch={a.branch_name or 'primary'} ok={a.ok} "
            f"last={a.last_step_id or '-'} err={_redact_secrets(a.error or '')}"
            for a in attempts
        ]
        parts.append("branch_attempts:\n" + "\n".join(attempt_lines))

    failing_step_id = attempts[-1].last_step_id if attempts else None
    if failing_step_id is None and playbook.steps:
        failing_step_id = playbook.steps[0].id
    if failing_step_id:
        for index, step in enumerate(playbook.steps):
            if step.id == failing_step_id:
                parts.append(
                    "failing_step:\n"
                    f"  index: {index}\n"
                    f"  id: {step.id}\n"
                    f"  params: {json.dumps(step.params, sort_keys=True)}"
                )
                break

    log_chunks: list[str] = []
    workdir = Path(task.workdir) if task.workdir else None
    if workdir and workdir.exists():
        for step_dir in sorted(workdir.iterdir()):
            if not step_dir.is_dir():
                continue
            tail = _read_log_tail(step_dir)
            if tail:
                log_chunks.append(f"log {step_dir.name}:\n{_redact_secrets(tail)}")
    if log_chunks:
        parts.extend(log_chunks)

    if flags:
        flag_lines = [f"- L{f.level} {f.kind}: {_redact_secrets(f.message)}" for f in flags]
        parts.append("open_flags:\n" + "\n".join(flag_lines))

    text = _apply_char_cap(parts)
    images: list[ImagePart] = []
    if include_image:
        source = Path(task.source_ref).expanduser()
        if source.is_file() and source.suffix.lower() in {
            ".png",
            ".jpg",
            ".jpeg",
            ".gif",
            ".webp",
            ".bmp",
        }:
            data = base64.b64encode(source.read_bytes()).decode("ascii")
            media = "image/png" if source.suffix.lower() == ".png" else f"image/{source.suffix[1:]}"
            images.append(ImagePart(media_type=media, data_base64=data))

    # Scrub accidental secrets from environment that must never reach prompts.
    for _key, value in os.environ.items():
        if _KEY_LIKE.search(value):
            text = text.replace(value, "<redacted>")
    return text, images
