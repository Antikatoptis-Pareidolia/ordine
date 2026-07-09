"""Tests for built-in steps and headless engine execution."""

from __future__ import annotations

from pathlib import Path

import pytest
from pydantic import BaseModel, ConfigDict

from conveyor.core.engines import HeadlessEngine
from conveyor.core.registry import StepRegistry
from conveyor.core.steps import StepContext, StepResult
from conveyor.core.workdir import TaskWorkdir
from conveyor.executors.builtin.steps import CopyStep, FailStep, NoopStep


@pytest.fixture
def registry() -> StepRegistry:
    reg = StepRegistry()
    reg.register(NoopStep)
    reg.register(FailStep)
    reg.register(CopyStep)
    return reg


def _ctx(tmp_path: Path, *, input_path: Path | None = None) -> StepContext:
    workdir = TaskWorkdir.create(tmp_path, "demo", 1)
    step_dir = workdir.step_dir(1, "util.copy")
    logger = workdir.step_logger(step_dir)
    return StepContext(
        task_id=1,
        pipeline_name="demo",
        source_ref="/src/a.txt",
        ordinal=None,
        input_path=input_path,
        step_dir=step_dir,
        logger=logger,
    )


def test_builtin_steps_pass_registration_contract(registry: StepRegistry) -> None:
    for step_id in ("util.noop", "util.fail", "util.copy"):
        step_cls = registry.get(step_id)
        assert step_cls.id == step_id
        assert "headless" in step_cls.engines


def test_util_copy_happy_path(tmp_path: Path, registry: StepRegistry) -> None:
    source = tmp_path / "input.txt"
    source.write_text("hello", encoding="utf-8")
    ctx = _ctx(tmp_path, input_path=source)
    params = registry.validate_params("util.copy", {})
    result = CopyStep().run(ctx, params)
    assert result.status == "ok"
    assert result.output_path is not None
    assert result.output_path.parent == ctx.step_dir
    assert result.output_path.read_text(encoding="utf-8") == "hello"


def test_util_copy_missing_input(tmp_path: Path, registry: StepRegistry) -> None:
    ctx = _ctx(tmp_path, input_path=None)
    params = registry.validate_params("util.copy", {})
    result = CopyStep().run(ctx, params)
    assert result.status == "fail"
    assert result.message is not None


def test_util_copy_missing_file(tmp_path: Path, registry: StepRegistry) -> None:
    missing = tmp_path / "does-not-exist.txt"
    ctx = _ctx(tmp_path, input_path=missing)
    params = registry.validate_params("util.copy", {})
    result = CopyStep().run(ctx, params)
    assert result.status == "fail"
    assert result.message is not None
    assert "input not found" in result.message
    assert str(missing) in result.message


def test_util_fail_times_counter(tmp_path: Path, registry: StepRegistry) -> None:
    ctx = _ctx(tmp_path)
    params = registry.validate_params("util.fail", {"message": "nope", "times": 2})
    step = FailStep()
    assert step.run(ctx, params).status == "fail"
    assert step.run(ctx, params).status == "fail"
    assert step.run(ctx, params).status == "ok"


def test_headless_engine_converts_buggy_step_to_fail(tmp_path: Path) -> None:
    class BuggyParams(BaseModel):
        model_config = ConfigDict(extra="forbid")

    class BuggyStep:
        id = "test.buggy"
        engines = frozenset({"headless"})
        Params = BuggyParams
        OUTPUT_DIR_PARAMS = frozenset()

        def run(self, ctx: StepContext, params: BaseModel) -> StepResult:
            del ctx, params
            raise RuntimeError("boom")

    workdir = TaskWorkdir.create(tmp_path, "demo", 2)
    step_dir = workdir.step_dir(1, "test.buggy")
    logger = workdir.step_logger(step_dir)
    ctx = StepContext(
        task_id=2,
        pipeline_name="demo",
        source_ref="/src/a.txt",
        ordinal=None,
        input_path=None,
        step_dir=step_dir,
        logger=logger,
    )
    engine = HeadlessEngine()
    result = engine.run_step(BuggyStep, ctx, BuggyParams())
    assert result.status == "fail"
    assert "unexpected error" in (result.message or "")
    log_text = (step_dir / "log.txt").read_text(encoding="utf-8")
    assert "RuntimeError" in log_text or "boom" in log_text
