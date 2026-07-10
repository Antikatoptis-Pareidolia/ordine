"""Unit tests for built-in file steps."""

from __future__ import annotations

import errno
import os
from pathlib import Path

import pytest

from conveyor.core.steps import StepContext
from conveyor.core.workdir import TaskWorkdir
from conveyor.executors.builtin.file_steps import MoveStep


def _ctx(tmp_path: Path, *, input_path: Path, step_id: str = "file.move") -> StepContext:
    workdir = TaskWorkdir.create(tmp_path, "demo", 1)
    step_dir = workdir.step_dir(1, step_id)
    logger = workdir.step_logger(step_dir)
    return StepContext(
        task_id=1,
        pipeline_name="demo",
        source_ref=str(input_path),
        ordinal=None,
        input_path=input_path,
        step_dir=step_dir,
        logger=logger,
        naming=None,
    )


def test_file_move_happy_path(tmp_path: Path) -> None:
    src = tmp_path / "inbox" / "artifact.txt"
    src.parent.mkdir()
    src.write_text("payload", encoding="utf-8")
    dest = tmp_path / "out"
    ctx = _ctx(tmp_path, input_path=src)
    result = MoveStep().run(ctx, MoveStep.Params(dest=str(dest)))
    assert result.status == "ok"
    assert result.output_path == dest / "artifact.txt"
    assert result.output_path.read_text(encoding="utf-8") == "payload"
    assert not src.exists()
    assert list(dest.glob(".tmp-*")) == []


def test_file_move_collision_suffix(tmp_path: Path) -> None:
    src = tmp_path / "in.png"
    src.write_bytes(b"new")
    dest = tmp_path / "out"
    dest.mkdir()
    (dest / "in.png").write_bytes(b"old")
    ctx = _ctx(tmp_path, input_path=src)
    result = MoveStep().run(ctx, MoveStep.Params(dest=str(dest), on_collision="suffix"))
    assert result.status == "ok"
    assert result.output_path == dest / "in-2.png"
    assert result.output_path.read_bytes() == b"new"
    assert not src.exists()


def test_file_move_collision_replace(tmp_path: Path) -> None:
    src = tmp_path / "in.png"
    src.write_bytes(b"new")
    dest = tmp_path / "out"
    dest.mkdir()
    existing = dest / "in.png"
    existing.write_bytes(b"old")
    ctx = _ctx(tmp_path, input_path=src)
    result = MoveStep().run(ctx, MoveStep.Params(dest=str(dest), on_collision="replace"))
    assert result.status == "ok"
    assert result.output_path == existing
    assert existing.read_bytes() == b"new"
    assert not src.exists()
    assert list(dest.glob(".tmp-*")) == []


def test_file_move_collision_fail(tmp_path: Path) -> None:
    src = tmp_path / "in.png"
    src.write_bytes(b"new")
    dest = tmp_path / "out"
    dest.mkdir()
    (dest / "in.png").write_bytes(b"old")
    ctx = _ctx(tmp_path, input_path=src)
    result = MoveStep().run(ctx, MoveStep.Params(dest=str(dest), on_collision="fail"))
    assert result.status == "fail"
    assert result.message is not None
    assert "destination exists" in result.message
    assert src.exists()


def test_file_move_cross_device_copy_fallback(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    src = tmp_path / "inbox" / "artifact.bin"
    src.parent.mkdir()
    src.write_bytes(b"payload")
    dest = tmp_path / "out"
    ctx = _ctx(tmp_path, input_path=src)
    real_replace = os.replace
    calls = {"count": 0}

    def fake_replace(src_path: str | os.PathLike[str], dst_path: str | os.PathLike[str]) -> None:
        calls["count"] += 1
        if calls["count"] == 1:
            raise OSError(errno.EXDEV, "cross-device move")
        real_replace(src_path, dst_path)

    monkeypatch.setattr(os, "replace", fake_replace)
    result = MoveStep().run(ctx, MoveStep.Params(dest=str(dest)))
    assert result.status == "ok"
    assert result.output_path == dest / "artifact.bin"
    assert result.output_path.read_bytes() == b"payload"
    assert not src.exists()
    assert calls["count"] == 2
