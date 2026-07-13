"""Tests for workdir retention cleanup."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from conveyor.core.db import create_engine_for, init_db
from conveyor.core.ledger import Ledger
from conveyor.core.retention import cleanup_workdirs


@pytest.fixture
def engine(tmp_path: Path):
    eng = create_engine_for(tmp_path / "ledger.db")
    init_db(eng)
    return eng


@pytest.fixture
def ledger(engine) -> Ledger:
    return Ledger(engine)


def _register(ledger: Ledger) -> int:
    from conveyor.core.playbook import loads_playbook

    yaml_text = """version: 1
name: retention-test
trigger:
  type: manual
  path: /tmp/in
steps:
  - util.noop
"""
    playbook = loads_playbook(yaml_text)
    pipeline_id, _ = ledger.register_pipeline(playbook, yaml_text)
    return pipeline_id


def _seed_task(
    ledger: Ledger,
    engine,
    pipeline_id: int,
    *,
    status: str,
    workdir: Path,
    finished_at: datetime | None,
) -> int:
    from sqlalchemy import update

    from conveyor.core.models import Task

    task_id = ledger.create_task(pipeline_id, "/in/file.png", f"key-{workdir.name}") or 0
    ledger.set_workdir(task_id, workdir)
    if status != "pending":
        ledger.transition(task_id, "processing")
    if status in {"done", "skipped", "failed", "flagged"}:
        ledger.transition(task_id, status)
    if finished_at is not None:
        with engine.begin() as conn:
            conn.execute(update(Task).where(Task.id == task_id).values(finished_at=finished_at))
    return task_id


def test_dry_run_deletes_nothing_on_disk(ledger: Ledger, engine, tmp_path: Path) -> None:
    pipeline_id = _register(ledger)
    workdir_root = tmp_path / "workdirs"
    old = datetime.now(tz=UTC) - timedelta(days=40)
    path = workdir_root / "demo" / "task_000001"
    path.mkdir(parents=True)
    (path / "artifact.txt").write_text("keep", encoding="utf-8")
    _seed_task(ledger, engine, pipeline_id, status="done", workdir=path, finished_at=old)

    report = cleanup_workdirs(
        ledger,
        workdir_root,
        older_than=timedelta(days=30),
        dry_run=True,
    )
    assert report.deleted == 1
    assert path.exists()
    assert ledger.get_task(1).workdir is not None


def test_keeps_flagged_and_failed_by_default(ledger: Ledger, engine, tmp_path: Path) -> None:
    pipeline_id = _register(ledger)
    workdir_root = tmp_path / "workdirs"
    old = datetime.now(tz=UTC) - timedelta(days=40)
    for status in ("flagged", "failed"):
        path = workdir_root / "demo" / f"task_{status}"
        path.mkdir(parents=True)
        (path / "x.txt").write_text("x", encoding="utf-8")
        _seed_task(ledger, engine, pipeline_id, status=status, workdir=path, finished_at=old)

    report = cleanup_workdirs(ledger, workdir_root, older_than=timedelta(days=30))
    assert report.deleted == 0
    assert report.kept_reasons.get("kept_flagged") == 1
    assert report.kept_reasons.get("kept_failed") == 1


def test_non_terminal_untouched(ledger: Ledger, engine, tmp_path: Path) -> None:
    pipeline_id = _register(ledger)
    workdir_root = tmp_path / "workdirs"
    path = workdir_root / "demo" / "task_pending"
    path.mkdir(parents=True)
    (path / "x.txt").write_text("x", encoding="utf-8")
    _seed_task(ledger, engine, pipeline_id, status="pending", workdir=path, finished_at=None)

    report = cleanup_workdirs(ledger, workdir_root, older_than=timedelta(days=1))
    assert report.deleted == 0
    assert path.exists()
    assert report.kept_reasons.get("non_terminal") == 1


def test_deletes_old_done_and_clears_workdir(ledger: Ledger, engine, tmp_path: Path) -> None:
    pipeline_id = _register(ledger)
    workdir_root = tmp_path / "workdirs"
    old = datetime.now(tz=UTC) - timedelta(days=40)
    path = workdir_root / "demo" / "task_done"
    path.mkdir(parents=True)
    payload = b"artifact-bytes"
    (path / "out.png").write_bytes(payload)
    task_id = _seed_task(ledger, engine, pipeline_id, status="done", workdir=path, finished_at=old)

    report = cleanup_workdirs(ledger, workdir_root, older_than=timedelta(days=30))
    assert report.deleted == 1
    assert report.bytes_freed >= len(payload)
    assert not path.exists()
    assert ledger.get_task(task_id).workdir is None


def test_include_failed_removes_only_failed(ledger: Ledger, engine, tmp_path: Path) -> None:
    pipeline_id = _register(ledger)
    workdir_root = tmp_path / "workdirs"
    old = datetime.now(tz=UTC) - timedelta(days=40)
    failed_path = workdir_root / "demo" / "task_failed"
    flagged_path = workdir_root / "demo" / "task_flagged"
    for path in (failed_path, flagged_path):
        path.mkdir(parents=True)
        (path / "x.txt").write_text("x", encoding="utf-8")
    _seed_task(ledger, engine, pipeline_id, status="failed", workdir=failed_path, finished_at=old)
    _seed_task(ledger, engine, pipeline_id, status="flagged", workdir=flagged_path, finished_at=old)

    report = cleanup_workdirs(
        ledger,
        workdir_root,
        older_than=timedelta(days=30),
        keep_statuses=frozenset({"flagged"}),
    )
    assert report.deleted == 1
    assert not failed_path.exists()
    assert flagged_path.exists()
