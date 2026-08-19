"""Unit tests for built-in file steps."""

from __future__ import annotations

import os
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

from ordine.core.steps import StepContext, StepResult
from ordine.core.workdir import TaskWorkdir
from ordine.executors.builtin.file_steps import MoveStep


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


def test_file_move_concurrent_suffix_never_overwrites(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    sources = []
    contexts = []
    for index, payload in enumerate((b"first", b"second"), start=1):
        src = tmp_path / f"inbox-{index}" / "artifact.txt"
        src.parent.mkdir()
        src.write_bytes(payload)
        sources.append(src)
        contexts.append(_ctx(tmp_path / f"work-{index}", input_path=src))
    dest = tmp_path / "out"

    real_link = os.link
    barrier = threading.Barrier(2)
    call_lock = threading.Lock()
    calls = 0

    def synchronized_link(src: os.PathLike[str], dst: os.PathLike[str]) -> None:
        nonlocal calls
        with call_lock:
            calls += 1
            should_wait = calls <= 2
        if should_wait:
            barrier.wait(timeout=5)
        real_link(src, dst)

    monkeypatch.setattr(os, "link", synchronized_link)
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(
            pool.map(
                lambda ctx: MoveStep().run(
                    ctx, MoveStep.Params(dest=str(dest), on_collision="suffix")
                ),
                contexts,
            )
        )

    assert {result.status for result in results} == {"ok"}
    assert {result.output_path.name for result in results if result.output_path} == {
        "artifact.txt",
        "artifact-2.txt",
    }
    assert {path.read_bytes() for path in dest.iterdir()} == {b"first", b"second"}
    assert all(not source.exists() for source in sources)


def test_file_move_late_fail_collision_preserves_both_files(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    src = tmp_path / "inbox" / "artifact.txt"
    src.parent.mkdir()
    src.write_bytes(b"new")
    dest = tmp_path / "out"
    ctx = _ctx(tmp_path, input_path=src)

    def collide(_tmp: Path, final: Path, *, replace: bool) -> None:
        assert not replace
        final.write_bytes(b"other-worker")
        raise FileExistsError

    monkeypatch.setattr("ordine.executors.builtin.file_steps._publish", collide)
    result = MoveStep().run(ctx, MoveStep.Params(dest=str(dest), on_collision="fail"))

    assert result.status == "fail"
    assert (dest / "artifact.txt").read_bytes() == b"other-worker"
    assert src.read_bytes() == b"new"
    assert list(dest.glob(".tmp-*")) == []


def test_file_move_late_suffix_rejects_unsafe_candidate(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    src = tmp_path / "inbox" / "artifact.txt"
    src.parent.mkdir()
    src.write_bytes(b"new")
    dest = tmp_path / "out"
    ctx = _ctx(tmp_path, input_path=src)
    calls = 0

    def collision(*_args: object) -> Path | StepResult:
        nonlocal calls
        calls += 1
        if calls == 1:
            return dest / "artifact.txt"
        return StepResult(status="fail", flag_kind="unsafe_name", message="unsafe")

    def collide(_tmp: Path, _final: Path, *, replace: bool) -> None:
        assert not replace
        raise FileExistsError

    monkeypatch.setattr("ordine.executors.builtin.file_steps._collision_path", collision)
    monkeypatch.setattr("ordine.executors.builtin.file_steps._publish", collide)

    result = MoveStep().run(ctx, MoveStep.Params(dest=str(dest), on_collision="suffix"))

    assert result.status == "fail"
    assert result.flag_kind == "unsafe_name"
    assert src.read_bytes() == b"new"
    assert list(dest.glob(".tmp-*")) == []


def test_file_move_final_publish_failure_preserves_source(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    src = tmp_path / "inbox" / "artifact.bin"
    src.parent.mkdir()
    src.write_bytes(b"payload")
    dest = tmp_path / "out"
    ctx = _ctx(tmp_path, input_path=src)

    def fake_link(src_path: str | os.PathLike[str], dst_path: str | os.PathLike[str]) -> None:
        del src_path, dst_path
        raise OSError("simulated final rename failure")

    monkeypatch.setattr(os, "link", fake_link)
    result = MoveStep().run(ctx, MoveStep.Params(dest=str(dest)))
    assert result.status == "fail"
    assert src.read_bytes() == b"payload"
    assert not (dest / "artifact.bin").exists()
    assert list(dest.glob(".tmp-*")) == []
