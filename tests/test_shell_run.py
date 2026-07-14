"""Tests for shell.run."""

from __future__ import annotations

from pathlib import Path

import pytest

from ordine.core.steps import StepContext
from ordine.core.workdir import TaskWorkdir
from ordine.executors.builtin.shell import ShellRunStep


def _ctx(
    tmp_path: Path,
    *,
    input_path: Path | None,
    ordinal: int | None = 1,
    source_ref: str = "/watch/doc_0001.md",
) -> StepContext:
    workdir = TaskWorkdir.create(tmp_path, "demo", 1)
    step_dir = workdir.step_dir(1, "shell.run")
    return StepContext(
        task_id=1,
        pipeline_name="demo",
        source_ref=source_ref,
        ordinal=ordinal,
        input_path=input_path,
        step_dir=step_dir,
        logger=workdir.step_logger(step_dir),
    )


@pytest.mark.parametrize("unsafe_name", ["../x", "/tmp/x", "a/b"])
def test_shell_run_rejects_unsafe_output_names(tmp_path: Path, unsafe_name: str) -> None:
    source = tmp_path / "in.md"
    source.write_text("body", encoding="utf-8")
    ctx = _ctx(tmp_path, input_path=source)
    params = ShellRunStep.Params(cmd="true", output=unsafe_name)

    result = ShellRunStep().run(ctx, params)

    assert result.status == "fail"
    assert result.flag_kind == "unsafe_name"
    assert result.message == f"unsafe output name from manifest/template: {unsafe_name}"


def test_shell_run_rejects_unknown_placeholders(tmp_path: Path) -> None:
    source = tmp_path / "in.md"
    source.write_text("body", encoding="utf-8")
    ctx = _ctx(tmp_path, input_path=source)
    params = ShellRunStep.Params(cmd='echo "{unknown}"')

    result = ShellRunStep().run(ctx, params)

    assert result.status == "fail"
    assert result.message == "unknown template placeholders: unknown"


def test_shell_run_substitutes_placeholders_and_passthrough(tmp_path: Path) -> None:
    source = tmp_path / "in.md"
    source.write_text("body", encoding="utf-8")
    ctx = _ctx(tmp_path, input_path=source, ordinal=3, source_ref="/watch/doc_0003.md")
    marker = ctx.step_dir / "marker.txt"
    params = ShellRunStep.Params(
        cmd=(
            'printf "%s|%s|%s|%s" "{input}" "{step_dir}" "{ordinal}" "{source}" > '
            '"{step_dir}/marker.txt"'
        )
    )

    result = ShellRunStep().run(ctx, params)

    assert result.status == "ok"
    assert result.output_path == source
    assert marker.read_text(encoding="utf-8") == (f"{source}|{ctx.step_dir}|3|/watch/doc_0003.md")
    assert (ctx.step_dir / "stdout.txt").exists()
    assert (ctx.step_dir / "stderr.txt").exists()


def test_shell_run_produces_output_file(tmp_path: Path) -> None:
    source = tmp_path / "in.md"
    source.write_text("content", encoding="utf-8")
    ctx = _ctx(tmp_path, input_path=source)
    params = ShellRunStep.Params(
        cmd='printf "%s\n" "# Published" | cat - "{input}" > "{step_dir}/stamped.md"',
        output="stamped.md",
    )

    result = ShellRunStep().run(ctx, params)

    assert result.status == "ok"
    assert result.output_path == ctx.step_dir / "stamped.md"
    assert result.output_path.read_text(encoding="utf-8") == "# Published\ncontent"


def test_shell_run_fails_on_nonzero_exit(tmp_path: Path) -> None:
    source = tmp_path / "in.md"
    source.write_text("body", encoding="utf-8")
    ctx = _ctx(tmp_path, input_path=source)
    params = ShellRunStep.Params(cmd='sh -c "echo boom 1>&2; exit 7"')

    result = ShellRunStep().run(ctx, params)

    assert result.status == "fail"
    assert result.message is not None
    assert "exit 7" in result.message
    assert "boom" in result.message
    assert (ctx.step_dir / "stderr.txt").read_text(encoding="utf-8") == "boom\n"


def test_shell_run_fails_when_output_missing(tmp_path: Path) -> None:
    source = tmp_path / "in.md"
    source.write_text("body", encoding="utf-8")
    ctx = _ctx(tmp_path, input_path=source)
    params = ShellRunStep.Params(cmd="true", output="missing.md")

    result = ShellRunStep().run(ctx, params)

    assert result.status == "fail"
    assert result.message == "expected output not produced: missing.md"


def test_shell_run_times_out(tmp_path: Path) -> None:
    source = tmp_path / "in.md"
    source.write_text("body", encoding="utf-8")
    ctx = _ctx(tmp_path, input_path=source)
    params = ShellRunStep.Params(cmd="sleep 2", timeout_seconds=0.1)

    result = ShellRunStep().run(ctx, params)

    assert result.status == "fail"
    assert result.message == "command timed out after 0.1s"
