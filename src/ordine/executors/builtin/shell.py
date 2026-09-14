"""Built-in shell execution step.

Owns shell.run. Must never import ledger, web, cli, or llm.
"""

from __future__ import annotations

import os
import re
import subprocess
from typing import ClassVar, Literal

from pydantic import BaseModel, ConfigDict, Field

from ordine.core.shell_policy import current_shell_mode
from ordine.core.steps import StepContext, StepResult
from ordine.core.workdir import is_safe_output_name, safe_output_path

_PLACEHOLDER = re.compile(r"\{([^{}]+)\}")
_ALLOWED_KEYS = frozenset({"input", "step_dir", "ordinal", "source"})
_HEREDOC = re.compile(r"(?<!<)<<-?(?!<)")
_ENV_PREFIX = "ORDINE_SHELL_"
_STDERR_TAIL = 300


class ShellRunParams(BaseModel):
    model_config = ConfigDict(extra="forbid")

    cmd: str
    timeout_seconds: float = Field(default=120.0, gt=0)
    expect_exit: int = 0
    output: str | None = None
    # Opt-in env hardening: "inherit" (default) passes os.environ; "allowlist" passes
    # only env_allowlist keys (plus Ordine placeholder vars). Empty allowlist → placeholders only.
    env_mode: Literal["inherit", "allowlist"] = "inherit"
    env_allowlist: list[str] = Field(default_factory=list)


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

        if current_shell_mode() == "stub":
            stub_note = ctx.step_dir / "shell_stubbed.txt"
            stub_note.write_text(
                f"shell.run stubbed (lab/dry-run); command was not executed\ncmd={params.cmd}\n",
                encoding="utf-8",
            )
            (ctx.step_dir / "stdout.txt").write_text("", encoding="utf-8")
            (ctx.step_dir / "stderr.txt").write_text(
                "ordine: shell.run stubbed — pass --allow-shell (CLI) or enable "
                "allow-shell execution in the lab to run for real\n",
                encoding="utf-8",
            )
            if params.output is None:
                return StepResult(
                    status="ok",
                    output_path=ctx.input_path,
                    message="shell.run stubbed (not executed)",
                )
            # Cannot invent declared outputs; fail clearly so authors see the stub.
            return StepResult(
                status="fail",
                message=(
                    "shell.run stubbed: cannot produce declared output "
                    f"{params.output!r}; re-run with --allow-shell / lab allow-shell"
                ),
            )

        substituted = _substitute_cmd(params.cmd, ctx)
        if isinstance(substituted, StepResult):
            return substituted
        command, placeholder_env = substituted

        stdout_path = ctx.step_dir / "stdout.txt"
        stderr_path = ctx.step_dir / "stderr.txt"
        child_env = _build_env(params, placeholder_env)

        try:
            completed = subprocess.run(
                command,
                shell=True,
                cwd=ctx.step_dir,
                capture_output=True,
                text=True,
                timeout=params.timeout_seconds,
                env=child_env,
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


def _build_env(params: ShellRunParams, placeholder_env: dict[str, str]) -> dict[str, str]:
    if params.env_mode == "inherit":
        return {**os.environ, **placeholder_env}
    allowed = {key: os.environ[key] for key in params.env_allowlist if key in os.environ}
    # Ensure a minimal PATH so trivial commands still work when allowlisted.
    if "PATH" not in allowed and "PATH" in os.environ and "PATH" not in params.env_allowlist:
        # Only auto-include PATH when the allowlist is non-empty and omitted PATH —
        # actually stay strict: only listed keys. Document that PATH must be listed.
        pass
    return {**allowed, **placeholder_env}


def _as_text(value: object) -> str:
    if value is None:
        return ""
    if isinstance(value, bytes):
        return value.decode("utf-8", errors="replace")
    return str(value)


def _quote_context(template: str, offset: int) -> str | None:
    """Return the active shell quote at *offset* for ordinary command strings."""
    quote: str | None = None
    escaped = False
    for char in template[:offset]:
        if escaped:
            escaped = False
            continue
        if char == "\\" and quote != "'":
            escaped = True
            continue
        if char == "'" and quote != '"':
            quote = None if quote == "'" else "'"
        elif char == '"' and quote != "'":
            quote = None if quote == '"' else '"'
    return quote


def _environment_reference(key: str, quote: str | None) -> str:
    reference = f"${{{_ENV_PREFIX}{key.upper()}}}"
    if quote == "'":
        return f"'\"{reference}\"'"
    if quote == '"':
        return reference
    return f'"{reference}"'


def _substitute_cmd(template: str, ctx: StepContext) -> tuple[str, dict[str, str]] | StepResult:
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

    if _PLACEHOLDER.search(template) and _HEREDOC.search(template):
        return StepResult(
            status="fail",
            message="shell placeholders are not supported in commands containing heredocs",
        )

    values: dict[str, str] = {
        "input": str(ctx.input_path) if ctx.input_path is not None else "",
        "step_dir": str(ctx.step_dir),
        "ordinal": "" if ctx.ordinal is None else str(ctx.ordinal),
        "source": ctx.source_ref,
    }
    pieces: list[str] = []
    cursor = 0
    for match in _PLACEHOLDER.finditer(template):
        pieces.append(template[cursor : match.start()])
        pieces.append(
            _environment_reference(match.group(1), _quote_context(template, match.start()))
        )
        cursor = match.end()
    pieces.append(template[cursor:])
    environment = {f"{_ENV_PREFIX}{key.upper()}": value for key, value in values.items()}
    return "".join(pieces), environment
