"""Tests for task work directory layout."""

from __future__ import annotations

from pathlib import Path

from conveyor.core.workdir import TaskWorkdir


def test_create_is_idempotent(tmp_path: Path) -> None:
    first = TaskWorkdir.create(tmp_path, "demo-pipeline", 7)
    second = TaskWorkdir.create(tmp_path, "demo-pipeline", 7)
    assert first.path == second.path
    assert first.path.name == "task_000007"


def test_step_dir_layout_matches_contract(tmp_path: Path) -> None:
    workdir = TaskWorkdir.create(tmp_path, "png-cleanup", 42)
    alpha = workdir.step_dir(1, "image.white_to_alpha")
    trim = workdir.step_dir(2, "image.trim")
    branch = workdir.step_dir(1, "image.export", branch="fallback-pillow", branch_no=1)

    assert alpha == workdir.path / "01_image.white_to_alpha"
    assert trim == workdir.path / "02_image.trim"
    assert branch == workdir.path / "b1_fallback-pillow" / "01_image.export"
    assert alpha.exists() and trim.exists() and branch.exists()


def test_step_dir_sanitizes_unsafe_chars(tmp_path: Path) -> None:
    workdir = TaskWorkdir.create(tmp_path, "demo", 1)
    step_dir = workdir.step_dir(1, "Bad Step!")
    assert step_dir.name == "01_bad_step_"


def test_step_dir_is_idempotent(tmp_path: Path) -> None:
    workdir = TaskWorkdir.create(tmp_path, "demo", 1)
    first = workdir.step_dir(3, "util.noop")
    second = workdir.step_dir(3, "util.noop")
    assert first == second


def test_step_logger_writes_once(tmp_path: Path) -> None:
    workdir = TaskWorkdir.create(tmp_path, "demo", 5)
    step_dir = workdir.step_dir(1, "util.noop")
    logger = workdir.step_logger(step_dir)
    logger.info("first line")
    logger2 = workdir.step_logger(step_dir)
    logger2.info("second line")
    lines = (step_dir / "log.txt").read_text(encoding="utf-8").splitlines()
    assert len(lines) == 2
    assert "first line" in lines[0]
    assert "second line" in lines[1]
