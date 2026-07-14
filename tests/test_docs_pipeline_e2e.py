"""End-to-end test for the shipped docs-pipeline example."""

from __future__ import annotations

from pathlib import Path

import pytest

from ordine.core.db import create_engine_for, init_db
from ordine.core.engines import EngineRegistry, HeadlessEngine
from ordine.core.ledger import Ledger
from ordine.core.playbook import loads_playbook
from ordine.core.registry import StepRegistry
from ordine.core.runner import PipelineRunner
from ordine.core.triggers import ManualScanService, ledger_sink

REPO_ROOT = Path(__file__).resolve().parents[1]
EXAMPLE_DIR = REPO_ROOT / "examples/docs-pipeline"
PLAYBOOK_PATH = EXAMPLE_DIR / "pipeline.yml"


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
    return StepRegistry.load()


@pytest.fixture
def engines() -> EngineRegistry:
    reg = EngineRegistry()
    reg.register(HeadlessEngine())
    return reg


def test_shipped_docs_pipeline_runs_oneshot(
    tmp_path: Path,
    ledger: Ledger,
    registry: StepRegistry,
    engines: EngineRegistry,
) -> None:
    publish = tmp_path / "publish"
    yaml_text = PLAYBOOK_PATH.read_text(encoding="utf-8").replace(
        "examples/docs-pipeline/publish",
        str(publish),
    )
    playbook = loads_playbook(yaml_text)
    pipeline_id, version = ledger.register_pipeline(playbook, yaml_text)
    sink = ledger_sink(ledger, pipeline_id)
    assert ManualScanService(playbook.trigger, playbook.dedup, sink).run() == 3

    runner = PipelineRunner(
        ledger=ledger,
        registry=registry,
        engines=engines,
        playbook=playbook,
        pipeline_id=pipeline_id,
        workdir_root=tmp_path / "workdirs",
        playbook_version=version,
    )
    assert runner.run_until_idle() == 3

    published = {path.name: path.read_text(encoding="utf-8") for path in publish.glob("*.md")}
    assert set(published) == {"intro.md", "guide.md", "notes.md"}
    for body in published.values():
        assert body.startswith("# Published\n")
