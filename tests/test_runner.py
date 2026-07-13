"""Mechanics tests for the pipeline runner."""

from __future__ import annotations

import json
from pathlib import Path
from typing import ClassVar

import pytest
from pydantic import BaseModel, ConfigDict
from sqlalchemy import select
from sqlalchemy.orm import Session

from conveyor.core.db import create_engine_for, init_db
from conveyor.core.engines import EngineRegistry, HeadlessEngine
from conveyor.core.errors import RunnerError
from conveyor.core.ledger import Ledger
from conveyor.core.models import BranchAttempt
from conveyor.core.playbook import loads_playbook
from conveyor.core.registry import StepRegistry
from conveyor.core.runner import PipelineRunner
from conveyor.core.steps import StepContext, StepResult
from conveyor.core.workdir import TaskWorkdir
from conveyor.executors.builtin.file_steps import RenameFromManifestStep
from conveyor.executors.builtin.steps import CopyStep, FailStep, NoopStep


class SkipParams(BaseModel):
    model_config = ConfigDict(extra="forbid")


class SkipStep:
    id = "test.skip"
    engines = frozenset({"headless"})
    Params = SkipParams
    OUTPUT_DIR_PARAMS: ClassVar[frozenset[str]] = frozenset()

    def run(self, ctx: StepContext, params: BaseModel) -> StepResult:
        del ctx, params
        return StepResult(status="skip", flag_kind="corrupt_input", message="corrupt")


class StubNaming:
    def __init__(self, names: dict[int, str] | None = None) -> None:
        self.names = names or {}
        self.bound: list[tuple[int, str]] = []

    def resolve(self, ordinal: int) -> str | None:
        return self.names.get(ordinal)

    def bind(self, ordinal: int, name: str) -> str:
        self.bound.append((ordinal, name))
        return self.names.setdefault(ordinal, name)


@pytest.fixture
def engine(tmp_path: Path):
    eng = create_engine_for(tmp_path / "ledger.db")
    init_db(eng)
    return eng


@pytest.fixture
def ledger(engine) -> Ledger:
    return Ledger(engine)


@pytest.fixture
def registry() -> StepRegistry:
    reg = StepRegistry()
    reg.register(NoopStep)
    reg.register(FailStep)
    reg.register(CopyStep)
    reg.register(SkipStep)
    reg.register(RenameFromManifestStep)
    return reg


@pytest.fixture
def engines() -> EngineRegistry:
    reg = EngineRegistry()
    reg.register(HeadlessEngine())
    return reg


def _playbook_yaml(*, steps: str, on_failure: str = "") -> str:
    block = f"\n{on_failure.rstrip()}\n" if on_failure else "\n"
    return f"""version: 1
name: test-pipeline
trigger: {{type: manual, path: /tmp/in}}
{block}steps:
{steps}
"""


def _register(ledger: Ledger, yaml_text: str) -> tuple[int, str, object]:
    playbook = loads_playbook(yaml_text)
    pipeline_id, version = ledger.register_pipeline(playbook, yaml_text)
    return pipeline_id, version, playbook


def _runner(
    ledger: Ledger,
    registry: StepRegistry,
    engines: EngineRegistry,
    playbook: object,
    pipeline_id: int,
    workdir_root: Path,
    version: str = "pv_test",
) -> PipelineRunner:
    return PipelineRunner(
        ledger=ledger,
        registry=registry,
        engines=engines,
        playbook=playbook,
        pipeline_id=pipeline_id,
        workdir_root=workdir_root,
        playbook_version=version,
    )


def _branch_attempts(engine, task_id: int) -> list[BranchAttempt]:
    with Session(engine) as session:
        return list(
            session.scalars(
                select(BranchAttempt)
                .where(BranchAttempt.task_id == task_id)
                .order_by(BranchAttempt.id)
            ).all()
        )


def test_happy_path_three_steps(
    tmp_path: Path, ledger: Ledger, registry: StepRegistry, engines: EngineRegistry
) -> None:
    yaml_text = _playbook_yaml(
        steps="  - util.noop\n  - util.copy\n  - util.noop\n",
    )
    pipeline_id, version, playbook = _register(ledger, yaml_text)
    src = tmp_path / "input.txt"
    src.write_text("hello", encoding="utf-8")
    task_id = ledger.create_task(pipeline_id, str(src), "k1")
    assert task_id is not None

    runner = _runner(ledger, registry, engines, playbook, pipeline_id, tmp_path / "work", version)
    assert runner.run_until_idle() == 1

    task = ledger.get_task(task_id)
    assert task.status == "done"
    workdir = Path(task.workdir)
    task_json = json.loads((workdir / "task.json").read_text(encoding="utf-8"))
    assert task_json["status"] == "done"
    assert task_json["playbook_version"] == version
    assert len(task_json["steps"]) == 3
    assert task_json["steps"][1]["id"] == "util.copy"
    assert task_json["steps"][1]["status"] == "ok"


def test_retries_util_fail_times_one(
    engine,
    tmp_path: Path,
    ledger: Ledger,
    registry: StepRegistry,
    engines: EngineRegistry,
) -> None:
    yaml_text = _playbook_yaml(
        steps="  - id: util.fail\n    params: {message: nope, times: 1}\n",
        on_failure="on_failure:\n  retries: 1",
    )
    pipeline_id, _, playbook = _register(ledger, yaml_text)
    src = tmp_path / "in.txt"
    src.write_text("x", encoding="utf-8")
    task_id = ledger.create_task(pipeline_id, str(src), "k1")
    assert task_id is not None

    runner = _runner(ledger, registry, engines, playbook, pipeline_id, tmp_path / "work")
    assert runner.run_until_idle() == 1
    assert ledger.get_task(task_id).status == "done"
    attempts = _branch_attempts(engine, task_id)
    primary = [a for a in attempts if a.branch_name is None]
    assert len(primary) == 2
    assert ledger.open_flags(pipeline_id) == []


def test_branch_success_feeds_next_primary(
    tmp_path: Path, ledger: Ledger, registry: StepRegistry, engines: EngineRegistry
) -> None:
    yaml_text = _playbook_yaml(
        steps="  - id: util.fail\n    params: {times: -1}\n  - util.noop\n",
        on_failure="""on_failure:
  retries: 0
  branches:
    - name: branch1
      retries: 0
      steps:
        - id: util.fail
          params: {times: -1}
    - name: branch2
      retries: 0
      steps: [util.copy]
""",
    )
    pipeline_id, _, playbook = _register(ledger, yaml_text)
    src = tmp_path / "in.txt"
    src.write_text("payload", encoding="utf-8")
    task_id = ledger.create_task(pipeline_id, str(src), "k1")
    assert task_id is not None

    runner = _runner(ledger, registry, engines, playbook, pipeline_id, tmp_path / "work")
    assert runner.run_until_idle() == 1

    task = ledger.get_task(task_id)
    assert task.status == "done"
    assert task.current_branch == "branch2"
    workdir = Path(task.workdir)
    assert any(p.name.startswith("b2_branch2") for p in workdir.iterdir())
    flags = ledger.open_flags(pipeline_id)
    assert len(flags) == 2
    assert {f.level for f in flags} == {1, 2}


def test_multi_step_primary_exhaustion_raises_flag_level_one(
    tmp_path: Path, ledger: Ledger, registry: StepRegistry, engines: EngineRegistry
) -> None:
    """Primary exhaustion on step N must not be masked by earlier ok primary attempts."""
    yaml_text = _playbook_yaml(
        steps="""  - util.noop
  - util.noop
  - id: util.fail
    params: {times: -1}
""",
        on_failure="on_failure:\n  retries: 0\n  then: mark_failed",
    )
    pipeline_id, _, playbook = _register(ledger, yaml_text)
    task_id = ledger.create_task(pipeline_id, str(tmp_path / "f.txt"), "k1")
    assert task_id is not None
    (tmp_path / "f.txt").write_text("x", encoding="utf-8")
    runner = _runner(ledger, registry, engines, playbook, pipeline_id, tmp_path / "work")
    assert runner.run_until_idle() == 1
    assert ledger.get_task(task_id).status == "flagged"
    flags = ledger.open_flags(pipeline_id)
    assert len(flags) == 1
    assert flags[0].level == 1
    assert ledger.next_flag_level(task_id, step_id="util.fail", branch_names=[]) == 1


def test_ladder_scoped_escalation_not_inflated_by_earlier_step(
    tmp_path: Path, ledger: Ledger, registry: StepRegistry, engines: EngineRegistry
) -> None:
    """Earlier step exhaustion + branch heal must not bump later step's first flag level."""
    yaml_text = _playbook_yaml(
        steps="""  - id: util.fail
    params: {message: step-x, times: -1}
    on_failure:
      retries: 0
      branches:
        - name: fix-x
          retries: 0
          steps: [util.noop]
  - id: util.fail
    params: {message: step-y, times: -1}
    on_failure:
      retries: 0
      branches:
        - name: fix-y
          retries: 0
          steps:
            - id: util.fail
              params: {times: -1}
""",
        on_failure="on_failure:\n  retries: 0\n  then: mark_failed",
    )
    pipeline_id, _, playbook = _register(ledger, yaml_text)
    src = tmp_path / "in.txt"
    src.write_text("payload", encoding="utf-8")
    task_id = ledger.create_task(pipeline_id, str(src), "k1")
    assert task_id is not None

    runner = _runner(ledger, registry, engines, playbook, pipeline_id, tmp_path / "work")
    assert runner.run_until_idle() == 1

    flags = sorted(ledger.open_flags(pipeline_id), key=lambda f: f.id)
    assert len(flags) == 3
    assert [f.level for f in flags] == [1, 1, 2]
    assert ledger.get_task(task_id).status == "flagged"


def test_all_exhausted_mark_failed_becomes_flagged(
    tmp_path: Path, ledger: Ledger, registry: StepRegistry, engines: EngineRegistry
) -> None:
    yaml_text = _playbook_yaml(
        steps="  - id: util.fail\n    params: {times: -1}\n",
        on_failure="""on_failure:
  retries: 0
  then: mark_failed
  branches:
    - name: b1
      retries: 0
      steps:
        - id: util.fail
          params: {times: -1}
    - name: b2
      retries: 0
      steps:
        - id: util.fail
          params: {times: -1}
""",
    )
    pipeline_id, _, playbook = _register(ledger, yaml_text)
    for i in range(2):
        ledger.create_task(pipeline_id, str(tmp_path / f"f{i}.txt"), f"k{i}")
    runner = _runner(ledger, registry, engines, playbook, pipeline_id, tmp_path / "work")
    assert runner.run_until_idle() == 2
    assert ledger.counts(pipeline_id)["flagged"] == 2
    assert ledger.counts(pipeline_id)["pending"] == 0
    flags = ledger.open_flags(pipeline_id)
    assert len(flags) >= 3


def test_exhaustion_then_skip_becomes_skipped(
    tmp_path: Path, ledger: Ledger, registry: StepRegistry, engines: EngineRegistry
) -> None:
    yaml_text = _playbook_yaml(
        steps="  - id: util.fail\n    params: {times: -1}\n",
        on_failure="on_failure:\n  retries: 0\n  then: skip",
    )
    pipeline_id, _, playbook = _register(ledger, yaml_text)
    task_id = ledger.create_task(pipeline_id, str(tmp_path / "f.txt"), "k1")
    assert task_id is not None
    runner = _runner(ledger, registry, engines, playbook, pipeline_id, tmp_path / "work")
    assert runner.run_until_idle() == 1
    assert ledger.get_task(task_id).status == "skipped"


def test_skip_short_circuits(
    engine,
    tmp_path: Path,
    ledger: Ledger,
    registry: StepRegistry,
    engines: EngineRegistry,
) -> None:
    yaml_text = _playbook_yaml(steps="  - test.skip\n")
    pipeline_id, _, playbook = _register(ledger, yaml_text)
    task_id = ledger.create_task(pipeline_id, str(tmp_path / "f.txt"), "k1")
    assert task_id is not None
    runner = _runner(ledger, registry, engines, playbook, pipeline_id, tmp_path / "work")
    assert runner.run_until_idle() == 1
    assert ledger.get_task(task_id).status == "skipped"
    flags = ledger.open_flags(pipeline_id)
    assert len(flags) == 1
    assert flags[0].kind == "corrupt_input"
    assert flags[0].level == 1
    attempts = _branch_attempts(engine, task_id)
    assert len(attempts) == 1
    assert attempts[0].branch_name is None


def test_semantics_step_level_policy_replaces_pipeline_default(
    engine,
    tmp_path: Path,
    ledger: Ledger,
    registry: StepRegistry,
    engines: EngineRegistry,
) -> None:
    yaml_text = _playbook_yaml(
        steps="""  - id: util.fail
    params: {times: -1}
    on_failure:
      retries: 0
""",
        on_failure="""on_failure:
  retries: 0
  branches:
    - name: rescue
      retries: 0
      steps: [util.noop]
""",
    )
    pipeline_id, _, playbook = _register(ledger, yaml_text)
    task_id = ledger.create_task(pipeline_id, str(tmp_path / "f.txt"), "k1")
    assert task_id is not None
    runner = _runner(ledger, registry, engines, playbook, pipeline_id, tmp_path / "work")
    runner.run_until_idle()
    assert ledger.get_task(task_id).status == "flagged"
    assert ledger.open_flags(pipeline_id)[0].message.startswith("step util.fail [primary]")
    attempts = _branch_attempts(engine, task_id)
    assert len(attempts) == 1
    assert attempts[0].branch_name is None


def test_runner_error_continues_worker(
    tmp_path: Path, ledger: Ledger, registry: StepRegistry, engines: EngineRegistry, monkeypatch
) -> None:
    yaml_text = _playbook_yaml(steps="  - util.noop\n")
    pipeline_id, _, playbook = _register(ledger, yaml_text)
    ledger.create_task(pipeline_id, str(tmp_path / "a.txt"), "k1")
    ledger.create_task(pipeline_id, str(tmp_path / "b.txt"), "k2")

    def boom(*_args, **_kwargs):
        raise RuntimeError("boom")

    monkeypatch.setattr(HeadlessEngine, "run_step", boom)
    runner = _runner(ledger, registry, engines, playbook, pipeline_id, tmp_path / "work")
    assert runner.run_until_idle() == 2
    assert ledger.counts(pipeline_id)["failed"] == 2
    flags = ledger.open_flags(pipeline_id)
    assert all(f.kind == "runner_error" for f in flags)


def test_startup_validation_unknown_step(
    tmp_path: Path, ledger: Ledger, registry: StepRegistry, engines: EngineRegistry
) -> None:
    yaml_text = _playbook_yaml(steps="  - does.not.exist\n")
    pipeline_id, _, playbook = _register(ledger, yaml_text)
    with pytest.raises(RunnerError, match="validation failed"):
        _runner(ledger, registry, engines, playbook, pipeline_id, tmp_path / "work")


def test_semantics_recovery_branch_output_feeds_next_primary(
    tmp_path: Path, ledger: Ledger, registry: StepRegistry, engines: EngineRegistry
) -> None:
    """Semantics table: recovery branch succeeds → output feeds next primary step."""
    test_branch_success_feeds_next_primary(tmp_path, ledger, registry, engines)


def test_semantics_exhaustion_mark_failed_becomes_flagged(
    tmp_path: Path, ledger: Ledger, registry: StepRegistry, engines: EngineRegistry
) -> None:
    """Semantics table: exhaustion with then mark_failed → task flagged."""
    test_all_exhausted_mark_failed_becomes_flagged(tmp_path, ledger, registry, engines)


def test_semantics_exhaustion_then_skip_becomes_skipped(
    tmp_path: Path, ledger: Ledger, registry: StepRegistry, engines: EngineRegistry
) -> None:
    """Semantics table: exhaustion with then skip → task skipped."""
    test_exhaustion_then_skip_becomes_skipped(tmp_path, ledger, registry, engines)


def test_semantics_skip_short_circuits(
    engine,
    tmp_path: Path,
    ledger: Ledger,
    registry: StepRegistry,
    engines: EngineRegistry,
) -> None:
    """Semantics table: skip result → immediate skipped, no retries or branches."""
    test_skip_short_circuits(engine, tmp_path, ledger, registry, engines)


def test_semantics_user_retries_flagged_task(
    engine,
    tmp_path: Path,
    ledger: Ledger,
    registry: StepRegistry,
    engines: EngineRegistry,
) -> None:
    """Semantics table: user retries flagged task → pending, escalation continues."""
    yaml_text = _playbook_yaml(
        steps="  - id: util.fail\n    params: {times: 1}\n",
        on_failure="on_failure:\n  retries: 0\n  then: mark_failed",
    )
    pipeline_id, _, playbook = _register(ledger, yaml_text)
    task_id = ledger.create_task(pipeline_id, str(tmp_path / "f.txt"), "k1")
    assert task_id is not None
    runner = _runner(ledger, registry, engines, playbook, pipeline_id, tmp_path / "work")
    runner.run_until_idle()
    assert ledger.get_task(task_id).status == "flagged"
    first_attempts = len(_branch_attempts(engine, task_id))

    ledger.transition(task_id, "pending")
    assert ledger.next_flag_level(task_id, step_id="util.fail", branch_names=[]) >= 1
    assert runner.run_until_idle() == 1
    assert ledger.get_task(task_id).status == "done"
    assert len(_branch_attempts(engine, task_id)) > first_attempts
    failed_attempts = [a for a in _branch_attempts(engine, task_id) if not a.ok]
    assert len(failed_attempts) >= 1


def test_rename_from_manifest_step_with_stub_naming(tmp_path: Path) -> None:
    manifest = tmp_path / "names.csv"
    manifest.write_text("name\ngoat.png\n", encoding="utf-8")
    src = tmp_path / "in.png"
    src.write_bytes(b"png")
    workdir = TaskWorkdir.create(tmp_path, "demo", 1)
    step_dir = workdir.step_dir(1, "file.rename_from_manifest")
    ctx = StepContext(
        task_id=1,
        pipeline_name="demo",
        source_ref=str(src),
        ordinal=1,
        input_path=src,
        step_dir=step_dir,
        logger=workdir.step_logger(step_dir),
        naming=StubNaming(),
    )
    step = RenameFromManifestStep()
    params = step.Params(manifest=str(manifest))
    result = step.run(ctx, params)
    assert result.status == "ok"
    assert result.output_path is not None
    assert result.output_path.name == "goat.png"
    assert result.output_path.read_bytes() == b"png"


def test_rename_from_manifest_missing_manifest_returns_clean_fail(tmp_path: Path) -> None:
    src = tmp_path / "in.png"
    src.write_bytes(b"png")
    workdir = TaskWorkdir.create(tmp_path, "demo", 1)
    step_dir = workdir.step_dir(1, "file.rename_from_manifest")
    ctx = StepContext(
        task_id=1,
        pipeline_name="demo",
        source_ref=str(src),
        ordinal=1,
        input_path=src,
        step_dir=step_dir,
        logger=workdir.step_logger(step_dir),
        naming=StubNaming(),
    )
    step = RenameFromManifestStep()
    params = step.Params(manifest=str(tmp_path / "missing.csv"))
    result = step.run(ctx, params)
    assert result.status == "fail"
    assert result.message is not None
    assert result.message.startswith("cannot read manifest")
    assert "unexpected error" not in result.message
