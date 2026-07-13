"""Masterplan chain acceptance: manifest → generate → move → cleanup."""

from __future__ import annotations

from datetime import timedelta
from pathlib import Path

import pytest
from PIL import Image

from ordine.core.db import create_engine_for, init_db
from ordine.core.engines import EngineRegistry, HeadlessEngine
from ordine.core.ledger import Ledger
from ordine.core.playbook import ManifestTrigger, loads_playbook
from ordine.core.registry import StepRegistry
from ordine.core.runner import PipelineRunner
from ordine.core.triggers import build_trigger_service
from ordine.llm.steps import reset_image_budget_for_tests
from tests.test_ledger_crash import SimulatedCrash

CHAIN_NAMES = [f"asset{i:02d}.png" for i in range(1, 21)]


@pytest.fixture(autouse=True)
def _restore_image_budget():
    reset_image_budget_for_tests()
    yield
    reset_image_budget_for_tests()


def _write_prompt_manifest(
    path: Path, names: list[str], *, prompts: dict[int, str] | None = None
) -> None:
    lines = ["name,prompt"]
    for index, name in enumerate(names, start=1):
        prompt = (prompts or {}).get(index, f"prompt for {name}")
        lines.append(f"{name},{prompt}")
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def _gen_yaml(*, manifest: Path, handoff: Path) -> str:
    return f"""version: 1
name: chain-gen
trigger:
  type: manifest
  path: {manifest}
  poll_seconds: 0
dedup: none
steps:
  - id: llm.generate_image
    params:
      manifest: {manifest}
      provider: mock
      size: 256x256
  - id: file.move
    params:
      dest: {handoff}
      on_collision: replace
"""


def _cleanup_yaml(*, watch: Path, manifest: Path, output: Path) -> str:
    return f"""version: 1
name: chain-cleanup
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
      on_collision: replace
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


def _register(ledger: Ledger, yaml_text: str) -> tuple[int, str, object]:
    playbook = loads_playbook(yaml_text)
    pipeline_id, version = ledger.register_pipeline(playbook, yaml_text)
    return pipeline_id, version, playbook


def _scan_manifest(ledger: Ledger, pipeline_id: int, playbook: object) -> int:
    trigger = playbook.trigger
    assert isinstance(trigger, ManifestTrigger)
    service = build_trigger_service(
        trigger,
        playbook.dedup,
        ledger=ledger,
        pipeline_id=pipeline_id,
    )
    return service.run()


def _runner(
    ledger: Ledger,
    registry: StepRegistry,
    engines: EngineRegistry,
    playbook: object,
    pipeline_id: int,
    workdir_root: Path,
    version: str,
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


class _CrashAfterRunner(PipelineRunner):
    """Raise SimulatedCrash after *crash_after* tasks complete."""

    def __init__(self, *args, crash_after: int, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self._crash_after = crash_after
        self._completed = 0

    def run_once(self):
        if self._completed >= self._crash_after:
            raise SimulatedCrash(self._crash_after)
        status = super().run_once()
        if status is not None:
            self._completed += 1
        return status


def _handoff_files(handoff: Path) -> dict[str, bytes]:
    return {path.name: path.read_bytes() for path in sorted(handoff.glob("*.png"))}


def _output_files(output: Path) -> dict[str, bytes]:
    return {path.name: path.read_bytes() for path in sorted(output.glob("*.png"))}


def _register_cleanup_leg(
    ledger: Ledger,
    *,
    handoff: Path,
    manifest: Path,
    output: Path,
) -> tuple[int, str, object]:
    return _register(ledger, _cleanup_yaml(watch=handoff, manifest=manifest, output=output))


def _scan_cleanup(
    ledger: Ledger,
    registry: StepRegistry,
    engines: EngineRegistry,
    *,
    clean_id: int,
    clean_ver: str,
    clean_pb: object,
    workdir: Path,
) -> PipelineRunner:
    from ordine.core.triggers import ManualScanService, ledger_sink

    ManualScanService(
        clean_pb.trigger,
        clean_pb.dedup,
        ledger_sink(ledger, clean_id),
    ).run()
    return _runner(ledger, registry, engines, clean_pb, clean_id, workdir, clean_ver)


def _start_cleanup_leg(
    ledger: Ledger,
    registry: StepRegistry,
    engines: EngineRegistry,
    *,
    handoff: Path,
    manifest: Path,
    output: Path,
    workdir: Path,
) -> tuple[int, str, object, PipelineRunner]:
    clean_id, clean_ver, clean_pb = _register_cleanup_leg(
        ledger, handoff=handoff, manifest=manifest, output=output
    )
    clean_runner = _scan_cleanup(
        ledger,
        registry,
        engines,
        clean_id=clean_id,
        clean_ver=clean_ver,
        clean_pb=clean_pb,
        workdir=workdir,
    )
    return clean_id, clean_ver, clean_pb, clean_runner


def _run_cleanup_leg(
    ledger: Ledger,
    registry: StepRegistry,
    engines: EngineRegistry,
    *,
    handoff: Path,
    manifest: Path,
    output: Path,
    workdir: Path,
) -> int:
    _, _, _, clean_runner = _start_cleanup_leg(
        ledger,
        registry,
        engines,
        handoff=handoff,
        manifest=manifest,
        output=output,
        workdir=workdir,
    )
    return clean_runner.run_until_idle()


def test_chain_regeneration_replaces_same_filenames(
    tmp_path: Path,
    ledger: Ledger,
    registry: StepRegistry,
    engines: EngineRegistry,
) -> None:
    """Edited manifest prompt reruns both legs with replace collisions, not suffix strays."""
    reset_image_budget_for_tests(cap=500)
    names = CHAIN_NAMES[:8]
    manifest = tmp_path / "assets.csv"
    handoff = tmp_path / "renders"
    output = tmp_path / "game" / "assets"
    handoff.mkdir(parents=True)
    output.mkdir(parents=True)
    _write_prompt_manifest(manifest, names, prompts={1: "first prompt"})

    gen_id, gen_ver, gen_pb = _register(ledger, _gen_yaml(manifest=manifest, handoff=handoff))
    assert _scan_manifest(ledger, gen_id, gen_pb) == 8
    gen_runner = _runner(ledger, registry, engines, gen_pb, gen_id, tmp_path / "work-gen", gen_ver)
    assert gen_runner.run_until_idle() == 8

    clean_id, clean_ver, clean_pb, clean_runner = _start_cleanup_leg(
        ledger,
        registry,
        engines,
        handoff=handoff,
        manifest=manifest,
        output=output,
        workdir=tmp_path / "work-clean",
    )
    assert clean_runner.run_until_idle() == 8

    first_handoff = _handoff_files(handoff)
    first_output = _output_files(output)
    assert set(first_handoff) == {f"img_{i:04d}.png" for i in range(1, 9)}
    assert set(first_output) == set(names)

    text = manifest.read_text(encoding="utf-8")
    manifest.write_text(text.replace("first prompt", "second prompt"), encoding="utf-8")

    assert _scan_manifest(ledger, gen_id, gen_pb) == 1
    assert gen_runner.run_until_idle() == 1

    second_handoff = _handoff_files(handoff)
    assert set(second_handoff) == set(first_handoff)
    assert not any("-2" in name for name in second_handoff)
    assert second_handoff["img_0001.png"] != first_handoff["img_0001.png"]
    for index in range(2, 9):
        key = f"img_{index:04d}.png"
        assert second_handoff[key] == first_handoff[key]

    _scan_cleanup(
        ledger,
        registry,
        engines,
        clean_id=clean_id,
        clean_ver=clean_ver,
        clean_pb=clean_pb,
        workdir=tmp_path / "work-clean",
    )
    assert clean_runner.run_until_idle() == 1

    second_output = _output_files(output)
    assert set(second_output) == set(first_output)
    assert not any("-2" in name for name in second_output)
    assert second_output[names[0]] != first_output[names[0]]
    for index in range(2, 9):
        assert second_output[names[index - 1]] == first_output[names[index - 1]]


def test_chain_twenty_row_double_run_and_crash_recovery(
    tmp_path: Path, ledger: Ledger, registry: StepRegistry, engines: EngineRegistry
) -> None:
    reset_image_budget_for_tests(cap=500)
    manifest = tmp_path / "assets.csv"
    handoff = tmp_path / "renders"
    output = tmp_path / "game" / "assets"
    _write_prompt_manifest(manifest, CHAIN_NAMES)

    gen_id, gen_ver, gen_pb = _register(ledger, _gen_yaml(manifest=manifest, handoff=handoff))
    assert _scan_manifest(ledger, gen_id, gen_pb) == 20

    crash_runner = _CrashAfterRunner(
        ledger=ledger,
        registry=registry,
        engines=engines,
        playbook=gen_pb,
        pipeline_id=gen_id,
        workdir_root=tmp_path / "work",
        playbook_version=gen_ver,
        crash_after=8,
    )
    try:
        while True:
            status = crash_runner.run_once()
            if status is None:
                break
    except SimulatedCrash:
        ledger.reconcile(gen_id, stale_after=timedelta(0), policy="retry")

    runner = _runner(ledger, registry, engines, gen_pb, gen_id, tmp_path / "work", gen_ver)
    assert runner.run_until_idle() == 12
    assert ledger.counts(gen_id)["done"] == 20
    files = _handoff_files(handoff)
    assert len(files) == 20
    assert set(files) == {f"img_{i:04d}.png" for i in range(1, 21)}

    first_bytes = dict(files)
    assert _scan_manifest(ledger, gen_id, gen_pb) == 0
    assert runner.run_until_idle() == 0
    second_bytes = _handoff_files(handoff)
    assert second_bytes == first_bytes

    assert (
        _run_cleanup_leg(
            ledger,
            registry,
            engines,
            handoff=handoff,
            manifest=manifest,
            output=output,
            workdir=tmp_path / "work-clean",
        )
        == 20
    )
    exports = sorted(output.glob("*.png"))
    assert len(exports) == 20
    assert {p.name for p in exports} == set(CHAIN_NAMES)
    for path in exports:
        Image.open(path)


def test_chain_poison_rows_do_not_shift_downstream_names(
    tmp_path: Path, ledger: Ledger, registry: StepRegistry, engines: EngineRegistry
) -> None:
    reset_image_budget_for_tests(cap=500)
    manifest = tmp_path / "assets.csv"
    handoff = tmp_path / "renders"
    output = tmp_path / "game" / "assets"
    prompts = dict.fromkeys(range(3, 7), "")
    for i in range(1, 21):
        prompts.setdefault(i, f"prompt {i}")
    _write_prompt_manifest(manifest, CHAIN_NAMES, prompts=prompts)

    gen_id, gen_ver, gen_pb = _register(ledger, _gen_yaml(manifest=manifest, handoff=handoff))
    assert _scan_manifest(ledger, gen_id, gen_pb) == 20
    runner = _runner(ledger, registry, engines, gen_pb, gen_id, tmp_path / "work", gen_ver)
    assert runner.run_until_idle() == 20
    assert ledger.counts(gen_id)["flagged"] == 4
    assert ledger.counts(gen_id)["done"] == 16
    assert (handoff / "img_0007.png").exists()

    assert (
        _run_cleanup_leg(
            ledger,
            registry,
            engines,
            handoff=handoff,
            manifest=manifest,
            output=output,
            workdir=tmp_path / "work-clean",
        )
        == 16
    )
    assert (output / CHAIN_NAMES[6]).exists()
