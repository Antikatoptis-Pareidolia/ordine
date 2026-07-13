"""Dry-run session core tests — isolation trio first."""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import ClassVar

import pytest
from pydantic import BaseModel, ConfigDict

from conveyor.core.db import create_engine_for, init_db
from conveyor.core.dryrun import DryRunSession, redirect_output_dirs
from conveyor.core.engines import EngineRegistry, HeadlessEngine
from conveyor.core.ledger import Ledger
from conveyor.core.playbook import loads_playbook
from conveyor.core.registry import StepRegistry
from conveyor.core.steps import StepContext, StepResult
from conveyor.executors.builtin.file_steps import MoveStep
from conveyor.executors.builtin.steps import CopyStep, FailStep, NoopStep


class WriteDestParams(BaseModel):
    model_config = ConfigDict(extra="forbid")

    dest: str
    marker: str = "written"


class WriteDestStep:
    id = "test.write_dest"
    engines = frozenset({"headless"})
    Params = WriteDestParams
    OUTPUT_DIR_PARAMS: ClassVar[frozenset[str]] = frozenset({"dest"})

    def run(self, ctx: StepContext, params: BaseModel) -> StepResult:
        assert isinstance(params, WriteDestParams)
        dest_dir = Path(params.dest).expanduser()
        dest_dir.mkdir(parents=True, exist_ok=True)
        target = dest_dir / "marker.txt"
        target.write_text(params.marker, encoding="utf-8")
        return StepResult(status="ok", output_path=target)


@pytest.fixture
def registry() -> StepRegistry:
    reg = StepRegistry()
    reg.register(NoopStep)
    reg.register(CopyStep)
    reg.register(FailStep)
    reg.register(MoveStep)
    reg.register(WriteDestStep)
    return reg


@pytest.fixture
def engines() -> EngineRegistry:
    reg = EngineRegistry()
    reg.register(HeadlessEngine())
    return reg


def _five_step_yaml(*, fail_message: str = "step 3 fails") -> str:
    return f"""version: 1
name: dryrun-five
trigger:
  type: manual
  path: ~/input
steps:
  - util.noop
  - util.copy
  - util.fail:
      message: {fail_message}
      times: -1
  - util.noop
  - util.noop
"""


def _session(
    tmp_path: Path,
    registry: StepRegistry,
    engines: EngineRegistry,
    *,
    yaml_text: str,
    samples: list[Path],
    prod_db: Path | None = None,
) -> tuple[DryRunSession, Ledger | None]:
    sample_dir = samples[0].parent
    playbook = loads_playbook(yaml_text)
    sandbox_root = tmp_path / "sandboxes"
    session = DryRunSession.create(
        playbook=playbook,
        version_public_id="pv_lab",
        sample_dir=sample_dir,
        glob="*",
        registry=registry,
        engines=engines,
        sandbox_root=sandbox_root,
        yaml_text=yaml_text,
        max_samples=len(samples),
    )
    prod_ledger = None
    if prod_db is not None:
        prod_engine = create_engine_for(prod_db)
        init_db(prod_engine)
        prod_ledger = Ledger(prod_engine)
        prod_ledger.register_pipeline(playbook, yaml_text, note="production")
    return session, prod_ledger


def _sample_bytes(sample_dir: Path) -> dict[str, bytes]:
    return {path.name: path.read_bytes() for path in sorted(sample_dir.iterdir()) if path.is_file()}


def _db_snapshot(db_path: Path) -> tuple[float, dict[str, int]]:
    mtime = db_path.stat().st_mtime
    with sqlite3.connect(db_path) as conn:
        table_counts: dict[str, int] = {}
        for table in ("pipelines", "playbook_versions", "tasks", "branch_attempts", "flags"):
            try:
                table_counts[table] = conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
            except sqlite3.OperationalError:
                table_counts[table] = 0
    return mtime, table_counts


def test_isolation_production_db_untouched(
    tmp_path: Path, registry: StepRegistry, engines: EngineRegistry
) -> None:
    prod_db = tmp_path / "prod.db"
    samples = tmp_path / "samples"
    samples.mkdir()
    (samples / "a.txt").write_text("alpha", encoding="utf-8")
    yaml_text = _five_step_yaml()

    prod_engine = create_engine_for(prod_db)
    init_db(prod_engine)
    prod_ledger = Ledger(prod_engine)
    prod_ledger.register_pipeline(loads_playbook(yaml_text), yaml_text, note="production")
    after_register_mtime, after_register_counts = _db_snapshot(prod_db)

    session, _ = _session(
        tmp_path,
        registry,
        engines,
        yaml_text=yaml_text,
        samples=[samples / "a.txt"],
    )
    session.run_all()
    session.close()
    final_mtime, final_counts = _db_snapshot(prod_db)

    assert after_register_counts == final_counts
    assert final_mtime == after_register_mtime


def test_isolation_source_samples_byte_identical(
    tmp_path: Path, registry: StepRegistry, engines: EngineRegistry
) -> None:
    samples = tmp_path / "samples"
    samples.mkdir()
    (samples / "one.txt").write_text("one", encoding="utf-8")
    (samples / "two.txt").write_text("two", encoding="utf-8")
    before = _sample_bytes(samples)
    session, _ = _session(
        tmp_path,
        registry,
        engines,
        yaml_text=_five_step_yaml(),
        samples=[samples / "one.txt", samples / "two.txt"],
    )
    session.run_to_end(0)
    session.close()
    assert _sample_bytes(samples) == before


def test_isolation_output_dir_redirected(
    tmp_path: Path, registry: StepRegistry, engines: EngineRegistry
) -> None:
    samples = tmp_path / "samples"
    samples.mkdir()
    sample = samples / "input.txt"
    sample.write_text("payload", encoding="utf-8")
    prod_output = tmp_path / "real-output"
    prod_output.mkdir()
    yaml_text = f"""version: 1
name: output-redirect
trigger:
  type: manual
  path: ~/input
steps:
  - test.write_dest:
      dest: {prod_output}
      marker: lab-marker
"""
    playbook = loads_playbook(yaml_text)
    sandbox_root = tmp_path / "sandboxes"
    session = DryRunSession.create(
        playbook=playbook,
        version_public_id="pv_lab",
        sample_dir=samples,
        glob="*",
        registry=registry,
        engines=engines,
        sandbox_root=sandbox_root,
        yaml_text=yaml_text,
    )
    assert session.output_redirections
    assert str(prod_output) in session.output_redirections[0][1]
    session.run_all()
    assert list(prod_output.iterdir()) == []
    redirected = session.sandbox / "outputs" / prod_output.name / "marker.txt"
    assert redirected.read_text(encoding="utf-8") == "lab-marker"
    session.close()


def test_redirect_mapping_recorded_on_playbook_copy(tmp_path: Path, registry: StepRegistry) -> None:
    playbook = loads_playbook(
        """version: 1
name: map-test
trigger: {type: manual, path: ~/in}
steps:
  - test.write_dest: {dest: /tmp/real-out}
"""
    )
    _, mappings = redirect_output_dirs(playbook, registry, tmp_path / "sandbox")
    assert mappings
    assert "dest" in mappings[0][0]


def test_step_through_pause_retry_and_branches(
    tmp_path: Path, registry: StepRegistry, engines: EngineRegistry
) -> None:
    samples = tmp_path / "samples"
    samples.mkdir()
    sample = samples / "data.txt"
    sample.write_text("data", encoding="utf-8")
    yaml_text = """version: 1
name: branch-lab
trigger: {type: manual, path: ~/in}
steps:
  - util.noop
  - util.copy
  - util.fail: {message: boom, times: -1}
  - util.noop
on_failure:
  retries: 0
  branches:
    - name: rescue
      retries: 0
      steps:
        - util.copy
"""
    session, _ = _session(
        tmp_path,
        registry,
        engines,
        yaml_text=yaml_text,
        samples=[sample],
    )
    assert session.run_next_step(0).status == "ok"
    assert session.run_next_step(0).status == "ok"
    failed = session.run_next_step(0)
    assert failed.status == "fail"
    assert session.tasks()[0].pointer == 2
    assert session.retry_step(0).status == "fail"
    recovered = session.run_branches(0)
    assert recovered.status == "ok"
    assert recovered.branch_results
    assert recovered.branch_results[0][0] == "rescue"
    assert session.tasks()[0].pointer == 3
    session.close()


def test_lab_vs_runner_semantics(
    tmp_path: Path, registry: StepRegistry, engines: EngineRegistry
) -> None:
    samples = tmp_path / "samples"
    samples.mkdir()
    sample = samples / "x.txt"
    sample.write_text("x", encoding="utf-8")
    yaml_text = """version: 1
name: retry-contrast
trigger: {type: manual, path: ~/in}
steps:
  - util.fail: {message: nope, times: 1}
on_failure:
  retries: 2
"""
    session, _ = _session(
        tmp_path,
        registry,
        engines,
        yaml_text=yaml_text,
        samples=[sample],
    )
    state = session.run_next_step(0)
    assert state.status == "fail"
    assert session.tasks()[0].status == "paused"
    session.run_to_end(0)
    assert session.tasks()[0].status == "done"
    session.close()


def test_ordinals_from_regex(
    tmp_path: Path, registry: StepRegistry, engines: EngineRegistry
) -> None:
    samples = tmp_path / "samples"
    samples.mkdir()
    (samples / "img_0003.png").write_bytes(b"png")
    yaml_text = """version: 1
name: ordinal-lab
trigger:
  type: manual
  path: ~/in
  ordinal_regex: 'img_(\\d+)\\.png'
steps:
  - util.noop
"""
    session, _ = _session(
        tmp_path,
        registry,
        engines,
        yaml_text=yaml_text,
        samples=[samples / "img_0003.png"],
    )
    assert session.tasks()[0].ordinal == 3
    session.close()


def test_resume_replays_prefix_and_continues(
    tmp_path: Path, registry: StepRegistry, engines: EngineRegistry
) -> None:
    samples = tmp_path / "samples"
    samples.mkdir()
    sample = samples / "file.txt"
    sample.write_text("go", encoding="utf-8")
    yaml_v1 = _five_step_yaml()
    session, _ = _session(
        tmp_path,
        registry,
        engines,
        yaml_text=yaml_v1,
        samples=[sample],
    )
    session.run_next_step(0)
    session.run_next_step(0)
    assert session.run_next_step(0).status == "fail"
    yaml_v2 = """version: 1
name: dryrun-five
trigger:
  type: manual
  path: ~/input
steps:
  - util.noop
  - util.copy
  - util.noop
  - util.noop
  - util.noop
"""
    resumed = DryRunSession.resume(
        session,
        loads_playbook(yaml_v2),
        "pv_0002",
        yaml_text=yaml_v2,
    )
    states = resumed.steps(0)
    assert states[0].status == "replayed"
    assert states[1].status == "replayed"
    resumed.run_to_end(0)
    assert resumed.tasks()[0].status == "done"
    assert all(step.status in ("ok", "replayed") for step in resumed.steps(0)[:-1])
    session.close()
    resumed.close()


def test_resume_stops_when_replayed_prefix_fails(
    tmp_path: Path, registry: StepRegistry, engines: EngineRegistry
) -> None:
    samples = tmp_path / "samples"
    samples.mkdir()
    sample = samples / "file.txt"
    sample.write_text("go", encoding="utf-8")
    yaml_v1 = _five_step_yaml()
    session, _ = _session(
        tmp_path,
        registry,
        engines,
        yaml_text=yaml_v1,
        samples=[sample],
    )
    session.run_next_step(0)
    session.run_next_step(0)
    assert session.run_next_step(0).status == "fail"
    yaml_v2 = yaml_v1.replace("util.noop", "util.fail: {message: broke prefix, times: -1}", 1)
    resumed = DryRunSession.resume(
        session,
        loads_playbook(yaml_v2),
        "pv_0002",
        yaml_text=yaml_v2,
    )
    assert resumed.tasks()[0].status == "paused"
    assert resumed.steps(0)[0].status == "fail"
    session.close()
    resumed.close()


def test_close_removes_sandbox_and_report_shape(
    tmp_path: Path, registry: StepRegistry, engines: EngineRegistry
) -> None:
    samples = tmp_path / "samples"
    samples.mkdir()
    sample = samples / "a.txt"
    sample.write_text("a", encoding="utf-8")
    session, _ = _session(
        tmp_path,
        registry,
        engines,
        yaml_text="""version: 1
name: close-test
trigger: {type: manual, path: ~/in}
steps:
  - util.noop
""",
        samples=[sample],
    )
    session.run_all()
    sandbox = session.sandbox
    report = session.report()
    session.close()
    assert not sandbox.exists()
    assert report["playbook"] == "close-test"
    assert report["tasks"][0]["steps"][0]["status"] == "ok"
    json.dumps(report)
