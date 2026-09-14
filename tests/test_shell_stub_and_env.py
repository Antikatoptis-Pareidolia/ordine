"""S4/S5: lab/dry-run shell stub and env allowlist."""

from __future__ import annotations

from pathlib import Path

import pytest

from ordine.core.shell_policy import shell_mode
from ordine.core.steps import StepContext
from ordine.core.workdir import TaskWorkdir
from ordine.executors.builtin.shell import ShellRunStep


def _ctx(tmp_path: Path) -> StepContext:
    workdir = TaskWorkdir.create(tmp_path, "demo", 1)
    step_dir = workdir.step_dir(1, "shell.run")
    source = tmp_path / "in.md"
    source.write_text("body", encoding="utf-8")
    return StepContext(
        task_id=1,
        pipeline_name="demo",
        source_ref=str(source),
        ordinal=1,
        input_path=source,
        step_dir=step_dir,
        logger=workdir.step_logger(step_dir),
    )


def test_shell_stub_skips_subprocess(tmp_path: Path) -> None:
    ctx = _ctx(tmp_path)
    marker = ctx.step_dir / "should-not-exist.txt"
    params = ShellRunStep.Params(cmd=f'echo hi > "{marker.name}"')
    with shell_mode("stub"):
        result = ShellRunStep().run(ctx, params)
    assert result.status == "ok"
    assert result.message and "stubbed" in result.message
    assert not marker.exists()
    assert (ctx.step_dir / "shell_stubbed.txt").exists()


def test_shell_env_allowlist_hides_secrets(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ORDINE_TEST_SECRET", "s3cr3t")
    monkeypatch.setenv("ORDINE_TEST_OK", "ok")
    ctx = _ctx(tmp_path)
    out = ctx.step_dir / "env.txt"
    params = ShellRunStep.Params(
        cmd=f'printf "%s|%s" "$ORDINE_TEST_SECRET" "$ORDINE_TEST_OK" > "{out.name}"',
        env_mode="allowlist",
        env_allowlist=["ORDINE_TEST_OK", "PATH"],
    )
    result = ShellRunStep().run(ctx, params)
    assert result.status == "ok"
    text = out.read_text(encoding="utf-8")
    assert "s3cr3t" not in text
    assert "ok" in text
