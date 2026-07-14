"""Built-in shell execution step.

Owns shell.run. Must never import ledger, web, cli, or llm.
"""

from __future__ import annotations

import re
import subprocess
from typing import ClassVar

from pydantic import BaseModel, ConfigDict, Field

from ordine.core.steps import StepContext, StepResult
from ordine.core.workdir import is_safe_output_name, safe_output_path

_PLACEHOLDER = re.compile(r"\{([^{}]+)\}")
_ALLOWED_KEYS = frozenset({"input", "step_dir", "ordinal", "source"})
_STDERR_TAIL = 300


class ShellRunParams(BaseModel):
    model_config = ConfigDict(extra="forbid")

    cmd: str
    timeout_seconds: float = Field(default=120.0, gt=0)
    expect_exit: int = 0
    output: str | None = None


class ShellRunStep:
    id = "shell.run"
    engines = frozenset({"headless"})
    Params = ShellRunParams
    OUTPUT_DIR_PARAMS: ClassVar[frozenset[str]] = frozenset()

    def run(self, ctx: StepContext, params: BaseModel) -> StepResult:
        if not isinstance(params, ShellRunParams):
            raise TypeError(f"expected ShellRunParams, got {type(params)!r}")

        if params.output is not None and not is_safe_output_name(params.output):
            return StepResult(
                status="fail",
                message=f"unsafe output name from manifest/template: {params.output}",
                flag_kind="unsafe_name",
            )

        substituted = _substitute_cmd(params.cmd, ctx)
        if isinstance(substituted, StepResult):
            return substituted

        stdout_path = ctx.step_dir / "stdout.txt"
        stderr_path = ctx.step_dir / "stderr.txt"

        try:
            completed = subprocess.run(
                substituted,
                shell=True,
                cwd=ctx.step_dir,
                capture_output=True,
                text=True,
                timeout=params.timeout_seconds,
            )
        except subprocess.TimeoutExpired as exc:
            stdout_path.write_text(_as_text(exc.stdout), encoding="utf-8")
            stderr_path.write_text(_as_text(exc.stderr), encoding="utf-8")
            return StepResult(
                status="fail",
                message=f"command timed out after {params.timeout_seconds}s",
            )

        stdout_path.write_text(completed.stdout or "", encoding="utf-8")
        stderr_path.write_text(completed.stderr or "", encoding="utf-8")

        if completed.returncode != params.expect_exit:
            tail = (completed.stderr or "")[-_STDERR_TAIL:]
            return StepResult(
                status="fail",
                message=(f"exit {completed.returncode} (expected {params.expect_exit}): {tail}"),
            )

        if params.output is None:
            return StepResult(status="ok", output_path=ctx.input_path)

        output_path = safe_output_path(ctx.step_dir, params.output)
        if output_path is None:
            return StepResult(
                status="fail",
                message=f"unsafe output name from manifest/template: {params.output}",
                flag_kind="unsafe_name",
            )
        if not output_path.exists():
            return StepResult(
                status="fail",
                message=f"expected output not produced: {params.output}",
            )
        return StepResult(status="ok", output_path=output_path)


def _as_text(value: object) -> str:
    if value is None:
        return ""
    if isinstance(value, bytes):
        return value.decode("utf-8", errors="replace")
    return str(value)


def _substitute_cmd(template: str, ctx: StepContext) -> str | StepResult:
    unknown: set[str] = set()
    for match in _PLACEHOLDER.finditer(template):
        key = match.group(1)
        if key not in _ALLOWED_KEYS:
            unknown.add(key)
    if unknown:
        return StepResult(
            status="fail",
            message=f"unknown template placeholders: {', '.join(sorted(unknown))}",
        )

    values = {
        "input": str(ctx.input_path) if ctx.input_path is not None else "",
        "step_dir": str(ctx.step_dir),
        "ordinal": "" if ctx.ordinal is None else str(ctx.ordinal),
        "source": ctx.source_ref,
    }
    try:
        return template.format(**values)
    except (KeyError, ValueError) as exc:
        return StepResult(status="fail", message=f"invalid command template: {exc}")
