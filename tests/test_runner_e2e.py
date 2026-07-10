"""End-to-end pipeline runner tests (game-assets scenario)."""

from __future__ import annotations

import time
from pathlib import Path

import pytest
from PIL import Image

from conveyor.core.db import create_engine_for, init_db
from conveyor.core.engines import EngineRegistry, HeadlessEngine
from conveyor.core.ledger import Ledger
from conveyor.core.playbook import ManualTrigger, loads_playbook
from conveyor.core.registry import StepRegistry
from conveyor.core.runner import PipelineRunner, PipelineService
from conveyor.core.triggers import ManualScanService, ledger_sink
from tests.test_image_steps import make_test_image

ASSET_NAMES = [
    "goat.png",
    "jug.png",
    "crown.png",
    "ring.png",
    "sword.png",
    "shield.png",
    "scroll.png",
    "gem.png",
]


def _smoke_yaml(*, watch: Path, manifest: Path, output: Path) -> str:
    return f"""version: 1
name: pipeline-smoke
trigger:
  type: folder_watch
  path: {watch}
  glob: "*.png"
  ordinal_regex: 'img_(\\d+)\\.png'
steps:
  - image.validate
  - image.white_to_alpha
  - image.trim
  - file.rename_from_manifest:
      manifest: {manifest}
  - id: image.export
    params:
      dest: {output}
      use_reserved_name: true
"""


def _game_assets_yaml(*, watch: Path, manifest: Path, output: Path) -> str:
    return f"""version: 1
name: game-assets
trigger:
  type: manual
  path: {watch}
  glob: "*.png"
  ordinal_regex: 'img_(\\d+)\\.png'
steps:
  - image.validate
  - image.white_to_alpha
  - image.trim
  - file.rename_from_manifest:
      manifest: {manifest}
  - id: image.export
    params:
      dest: {output}
      use_reserved_name: true
"""


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


def _seed_images(watch: Path, *, corrupt_ordinals: set[int] | None = None) -> dict[int, bytes]:
    corrupt_ordinals = corrupt_ordinals or set()
    originals: dict[int, bytes] = {}
    watch.mkdir(parents=True, exist_ok=True)
    for ordinal in range(1, 9):
        path = watch / f"img_{ordinal:04d}.png"
        make_test_image(path)
        unique = path.read_bytes() + bytes([ordinal])
        if ordinal in corrupt_ordinals:
            path.write_bytes(b"truncated" + bytes([ordinal]))
        else:
            path.write_bytes(unique)
        originals[ordinal] = path.read_bytes()
    return originals


def _write_manifest(path: Path, names: list[str]) -> None:
    rows = "\n".join(names)
    path.write_text(f"name\n{rows}\n", encoding="utf-8")


def _runner_for(
    ledger: Ledger,
    registry: StepRegistry,
    engines: EngineRegistry,
    yaml_text: str,
    workdir_root: Path,
) -> tuple[PipelineRunner, int]:
    playbook = loads_playbook(yaml_text)
    pipeline_id, version = ledger.register_pipeline(playbook, yaml_text)
    runner = PipelineRunner(
        ledger=ledger,
        registry=registry,
        engines=engines,
        playbook=playbook,
        pipeline_id=pipeline_id,
        workdir_root=workdir_root,
        playbook_version=version,
    )
    return runner, pipeline_id


def _scan_tasks(ledger: Ledger, pipeline_id: int, watch: Path, playbook_yaml: str) -> int:
    playbook = loads_playbook(playbook_yaml)
    sink = ledger_sink(ledger, pipeline_id)
    spec = ManualTrigger(
        type="manual",
        path=str(watch),
        glob="*.png",
        ordinal_regex=r"img_(\d+)\.png",
    )
    service = ManualScanService(spec, playbook.dedup, sink)
    return service.run()


def test_game_assets_corrupt_ordinals_and_export_names(
    tmp_path: Path, ledger: Ledger, registry: StepRegistry, engines: EngineRegistry
) -> None:
    watch = tmp_path / "in"
    manifest = tmp_path / "assets.csv"
    output = tmp_path / "out"
    originals = _seed_images(watch, corrupt_ordinals={3, 4, 5, 6})
    _write_manifest(manifest, ASSET_NAMES)
    yaml_text = _game_assets_yaml(watch=watch, manifest=manifest, output=output)

    runner, pipeline_id = _runner_for(ledger, registry, engines, yaml_text, tmp_path / "work")
    _scan_tasks(ledger, pipeline_id, watch, yaml_text)
    assert runner.run_until_idle() == 8

    counts = ledger.counts(pipeline_id)
    assert counts["done"] == 4
    assert counts["skipped"] == 4

    for ordinal in (3, 4, 5, 6):
        task = next(t for t in ledger.list_tasks(pipeline_id, limit=20) if t.ordinal == ordinal)
        assert task.status == "skipped"
    skip_flags = [f for f in ledger.open_flags(pipeline_id) if f.kind == "corrupt_input"]
    assert len(skip_flags) == 4

    assert (output / ASSET_NAMES[6]).exists()
    assert not (output / "img_0007.png").exists()

    for ordinal in (1, 2, 7, 8):
        name = ASSET_NAMES[ordinal - 1]
        exported = output / name
        assert exported.exists(), f"missing export for ordinal {ordinal}"
        with Image.open(exported) as img:
            assert img.size[0] < 64 and img.size[1] < 64
            assert img.size[0] > 10 and img.size[1] > 10

    for ordinal, data in originals.items():
        assert (watch / f"img_{ordinal:04d}.png").read_bytes() == data


def test_game_assets_regenerate_ordinal_three_reclaims_name(
    tmp_path: Path, ledger: Ledger, registry: StepRegistry, engines: EngineRegistry
) -> None:
    watch = tmp_path / "in"
    manifest = tmp_path / "assets.csv"
    output = tmp_path / "out"
    _seed_images(watch, corrupt_ordinals={3, 4, 5, 6})
    _write_manifest(manifest, ASSET_NAMES)
    yaml_text = _game_assets_yaml(watch=watch, manifest=manifest, output=output)
    runner, pipeline_id = _runner_for(ledger, registry, engines, yaml_text, tmp_path / "work")
    _scan_tasks(ledger, pipeline_id, watch, yaml_text)
    runner.run_until_idle()

    regenerated = watch / "img_0003.png"
    make_test_image(regenerated)
    data = regenerated.read_bytes() + b"regenerated"
    regenerated.write_bytes(data)
    _scan_tasks(ledger, pipeline_id, watch, yaml_text)
    assert runner.run_until_idle() == 1

    assert (output / ASSET_NAMES[2]).exists()
    ord3_tasks = [t for t in ledger.list_tasks(pipeline_id, limit=50) if t.ordinal == 3]
    assert any(t.status == "skipped" for t in ord3_tasks)
    assert any(t.status == "done" for t in ord3_tasks)


def test_manifest_exhausted_ordinal_nine(
    tmp_path: Path, ledger: Ledger, registry: StepRegistry, engines: EngineRegistry
) -> None:
    watch = tmp_path / "in"
    manifest = tmp_path / "assets.csv"
    output = tmp_path / "out"
    _seed_images(watch)
    _write_manifest(manifest, ASSET_NAMES)
    yaml_text = _game_assets_yaml(watch=watch, manifest=manifest, output=output)
    runner, pipeline_id = _runner_for(ledger, registry, engines, yaml_text, tmp_path / "work")
    _scan_tasks(ledger, pipeline_id, watch, yaml_text)
    assert runner.run_until_idle() == 8

    extra = watch / "img_0009.png"
    make_test_image(extra)
    _scan_tasks(ledger, pipeline_id, watch, yaml_text)
    assert runner.run_until_idle() == 1
    task = next(t for t in ledger.list_tasks(pipeline_id, limit=20) if t.ordinal == 9)
    assert task.status == "flagged"
    flags = [f for f in ledger.open_flags(pipeline_id) if f.task_id == task.id]
    assert any(f.kind == "manifest_exhausted" for f in flags)


def test_pipeline_service_smoke(
    tmp_path: Path, ledger: Ledger, registry: StepRegistry, engines: EngineRegistry
) -> None:
    watch = tmp_path / "in"
    manifest = tmp_path / "assets.csv"
    output = tmp_path / "out"
    watch.mkdir()
    _write_manifest(manifest, ASSET_NAMES[:3])
    yaml_text = _smoke_yaml(watch=watch, manifest=manifest, output=output)
    playbook = loads_playbook(yaml_text)
    pipeline_id, version = ledger.register_pipeline(playbook, yaml_text)
    runner = PipelineRunner(
        ledger=ledger,
        registry=registry,
        engines=engines,
        playbook=playbook,
        pipeline_id=pipeline_id,
        workdir_root=tmp_path / "work",
        playbook_version=version,
    )
    service = PipelineService(
        ledger=ledger, runner=runner, playbook=playbook, pipeline_id=pipeline_id
    )
    service.start()
    try:
        for i in range(1, 4):
            path = watch / f"img_{i:04d}.png"
            make_test_image(path)
            path.write_bytes(path.read_bytes() + bytes([i]))
        deadline = time.monotonic() + 10.0
        while time.monotonic() < deadline:
            if ledger.counts(pipeline_id)["done"] == 3:
                break
            time.sleep(0.1)
        assert ledger.counts(pipeline_id)["done"] == 3
    finally:
        service.stop()
