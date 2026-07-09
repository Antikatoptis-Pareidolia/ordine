"""Test plugin exposing a single echo step for registry discovery tests."""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict

from conveyor.core.steps import StepContext, StepResult


class EchoParams(BaseModel):
    model_config = ConfigDict(extra="forbid")

    text: str


class EchoStep:
    id = "test.echo"
    engines = frozenset({"headless"})
    Params = EchoParams
    OUTPUT_DIR_PARAMS = frozenset()

    def run(self, ctx: StepContext, params: BaseModel) -> StepResult:
        assert isinstance(params, EchoParams)
        output = ctx.step_dir / "echo.txt"
        output.write_text(params.text, encoding="utf-8")
        return StepResult(status="ok", output_path=output)
