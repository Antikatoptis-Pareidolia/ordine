"""Manifest trigger service tests."""

from __future__ import annotations

import time
from pathlib import Path

import pytest

from conveyor.core.db import create_engine_for, init_db
from conveyor.core.ledger import Ledger
from conveyor.core.manifest import load_manifest
from conveyor.core.playbook import ManifestTrigger
from conveyor.core.triggers import (
    ManifestTriggerService,
    build_trigger_service,
    ledger_sink,
    manifest_row_dedup_key,
    manifest_sink,
)


@pytest.fixture
def engine(tmp_path: Path):
    eng = create_engine_for(tmp_path / "ledger.db")
    init_db(eng)
    return eng


@pytest.fixture
def ledger(engine) -> Ledger:
    return Ledger(engine)


def _register(ledger: Ledger, name: str = "manifest-pipe") -> int:
    yaml_text = f"""version: 1
name: {name}
trigger:
  type: manifest
  path: ~/assets.csv
steps: [util.noop]
"""
    from conveyor.core.playbook import loads_playbook

    playbook = loads_playbook(yaml_text)
    pipeline_id, _ = ledger.register_pipeline(playbook, yaml_text)
    return pipeline_id


def _write_manifest(path: Path, rows: list[tuple[str, str]]) -> None:
    lines = ["name,prompt", *(f"{name},{prompt}" for name, prompt in rows)]
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def test_manifest_scan_creates_tasks_and_reservations(tmp_path: Path, ledger: Ledger) -> None:
    manifest = tmp_path / "assets.csv"
    rows = [(f"asset{i}.png", f"prompt {i}") for i in range(1, 6)]
    _write_manifest(manifest, rows)
    pipeline_id = _register(ledger)
    spec = ManifestTrigger(type="manifest", path=str(manifest), poll_seconds=0)
    service = build_trigger_service(
        spec,
        "none",
        ledger_sink(ledger, pipeline_id),
        ledger=ledger,
        pipeline_id=pipeline_id,
    )
    assert service.run() == 5
    tasks = ledger.list_tasks(pipeline_id, limit=10)
    assert len(tasks) == 5
    assert sorted(t.ordinal for t in tasks) == [1, 2, 3, 4, 5]
    loaded = load_manifest(manifest)
    for task in tasks:
        assert task.ordinal is not None
        row = loaded[task.ordinal - 1]
        expected = manifest_row_dedup_key(row)
        assert task.dedup_key == expected
        assert ledger.reserved_name(pipeline_id, task.ordinal) == row.name


def test_manifest_rescan_no_duplicates(tmp_path: Path, ledger: Ledger) -> None:
    manifest = tmp_path / "assets.csv"
    _write_manifest(manifest, [("a.png", "one"), ("b.png", "two")])
    pipeline_id = _register(ledger)
    spec = ManifestTrigger(type="manifest", path=str(manifest), poll_seconds=0)
    service = build_trigger_service(
        spec,
        "none",
        ledger_sink(ledger, pipeline_id),
        ledger=ledger,
        pipeline_id=pipeline_id,
    )
    assert service.run() == 2
    assert service.run() == 0
    assert len(ledger.list_tasks(pipeline_id, limit=10)) == 2


def test_manifest_prompt_edit_creates_one_new_task_same_ordinal(
    tmp_path: Path, ledger: Ledger, caplog: pytest.LogCaptureFixture
) -> None:
    manifest = tmp_path / "assets.csv"
    _write_manifest(manifest, [("a.png", "one"), ("b.png", "two"), ("c.png", "three")])
    pipeline_id = _register(ledger)
    spec = ManifestTrigger(type="manifest", path=str(manifest), poll_seconds=0.1)
    service = ManifestTriggerService(
        spec,
        "none",
        manifest_sink(ledger, pipeline_id, manifest),
        ledger=ledger,
        pipeline_id=pipeline_id,
    )
    service.start()
    time.sleep(0.05)
    manifest.write_text(
        manifest.read_text(encoding="utf-8").replace("three", "three-edited"),
        encoding="utf-8",
    )
    time.sleep(0.25)
    service.stop()

    tasks = ledger.list_tasks(pipeline_id, limit=10)
    assert len(tasks) == 4
    ordinal3 = [t for t in tasks if t.ordinal == 3]
    assert len(ordinal3) == 2
    assert ledger.reserved_name(pipeline_id, 3) == "c.png"


def test_manifest_append_row_creates_new_task(tmp_path: Path, ledger: Ledger) -> None:
    manifest = tmp_path / "assets.csv"
    _write_manifest(manifest, [("a.png", "one")])
    pipeline_id = _register(ledger)
    spec = ManifestTrigger(type="manifest", path=str(manifest), poll_seconds=0)
    service = build_trigger_service(
        spec,
        "none",
        ledger_sink(ledger, pipeline_id),
        ledger=ledger,
        pipeline_id=pipeline_id,
    )
    assert service.run() == 1
    manifest.write_text(manifest.read_text(encoding="utf-8") + "b.png,two\n", encoding="utf-8")
    assert service.run() == 1
    assert len(ledger.list_tasks(pipeline_id, limit=10)) == 2


def test_manifest_unreadable_flags_once_then_recovers(tmp_path: Path, ledger: Ledger) -> None:
    manifest = tmp_path / "assets.csv"
    _write_manifest(manifest, [("a.png", "one")])
    pipeline_id = _register(ledger)
    spec = ManifestTrigger(type="manifest", path=str(manifest), poll_seconds=0.1)
    service = ManifestTriggerService(
        spec,
        "none",
        manifest_sink(ledger, pipeline_id, manifest),
        ledger=ledger,
        pipeline_id=pipeline_id,
    )
    service.start()
    manifest.write_text("broken\n", encoding="utf-8")
    time.sleep(0.25)
    flags = ledger.open_flags(pipeline_id)
    assert len(flags) == 1
    assert flags[0].kind == "manifest_unreadable"
    time.sleep(0.25)
    assert len(ledger.open_flags(pipeline_id)) == 1
    _write_manifest(manifest, [("a.png", "one"), ("b.png", "two")])
    time.sleep(0.25)
    service.stop()
    assert len(ledger.list_tasks(pipeline_id, limit=10)) >= 1


def test_poll_seconds_zero_no_poller_thread(tmp_path: Path, ledger: Ledger) -> None:
    manifest = tmp_path / "assets.csv"
    _write_manifest(manifest, [("a.png", "one")])
    pipeline_id = _register(ledger)
    spec = ManifestTrigger(type="manifest", path=str(manifest), poll_seconds=0)
    service = ManifestTriggerService(
        spec,
        "none",
        manifest_sink(ledger, pipeline_id, manifest),
        ledger=ledger,
        pipeline_id=pipeline_id,
    )
    service.start()
    assert service._poller_thread is None
    service.stop()
