"""Built-in utility steps for testing and minimal pipelines.

Owns generic util.* steps. Must never import ledger, web, cli, or llm.
"""

from __future__ import annotations

import shutil
from typing import ClassVar

from pydantic import BaseModel, ConfigDict

from ordine.core.steps import StepContext, StepResult


class NoopParams(BaseModel):
    model_config = ConfigDict(extra="forbid")


class NoopStep:
    id = "util.noop"
    engines = frozenset({"headless"})
    Params = NoopParams
    OUTPUT_DIR_PARAMS: ClassVar[frozenset[str]] = frozenset()

    def run(self, ctx: StepContext, params: BaseModel) -> StepResult:
        del ctx, params
        return StepResult(status="ok")


class FailParams(BaseModel):
    model_config = ConfigDict(extra="forbid")

    message: str = "intentional failure"
    times: int = -1


class FailStep:
    id = "util.fail"
    engines = frozenset({"headless"})
    Params = FailParams
    OUTPUT_DIR_PARAMS: ClassVar[frozenset[str]] = frozenset()

    def run(self, ctx: StepContext, params: BaseModel) -> StepResult:
        assert isinstance(params, FailParams)
        if params.times < 0:
            return StepResult(status="fail", message=params.message)
        counter_path = ctx.step_dir.parent / f".{self.id}.counter"
        count = 0
        if counter_path.exists():
            count = int(counter_path.read_text(encoding="utf-8"))
        if count < params.times:
            counter_path.write_text(str(count + 1), encoding="utf-8")
            return StepResult(status="fail", message=params.message)
        return StepResult(status="ok")


class CopyParams(BaseModel):
    model_config = ConfigDict(extra="forbid")


class CopyStep:
    id = "util.copy"
    engines = frozenset({"headless"})
    Params = CopyParams
    OUTPUT_DIR_PARAMS: ClassVar[frozenset[str]] = frozenset()

    def run(self, ctx: StepContext, params: BaseModel) -> StepResult:
        del params
        if ctx.input_path is None:
            return StepResult(status="fail", message="input_path is required")
        if not ctx.input_path.exists():
            return StepResult(status="fail", message=f"input not found: {ctx.input_path}")
        dest = ctx.step_dir / ctx.input_path.name
        shutil.copy2(ctx.input_path, dest)
        return StepResult(status="ok", output_path=dest)
