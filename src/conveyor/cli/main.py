"""Conveyor command-line interface.

Owns argument parsing and output formatting only. Must never implement pipeline business logic.
"""

from __future__ import annotations

import logging
import signal
import sys
import time
from dataclasses import dataclass
from datetime import timedelta
from pathlib import Path
from typing import Annotated, Any, cast

import typer

from conveyor.cli import output
from conveyor.core.config import AppConfig, load_config, write_default_config
from conveyor.core.db import create_engine_for, init_db
from conveyor.core.dryrun import DryRunSession
from conveyor.core.engines import EngineRegistry
from conveyor.core.errors import (
    ConfigError,
    IllegalTransitionError,
    LedgerError,
    PlaybookSyntaxError,
    PlaybookValidationError,
    RunnerError,
)
from conveyor.core.ledger import Ledger, PipelineSummary, TaskStatus, TaskView
from conveyor.core.playbook import FolderWatchTrigger, ManualTrigger, Playbook, load_playbook
from conveyor.core.registry import StepRegistry
from conveyor.core.runner import PipelineRunner, PipelineService
from conveyor.core.triggers import ManualScanService, ledger_sink
from conveyor.llm.client import build_client
from conveyor.llm.errors import LLMAuthError, LLMError, LLMNotConfiguredError
from conveyor.llm.types import Message

logger = logging.getLogger(__name__)

app = typer.Typer(no_args_is_help=True, add_completion=False)
llm_app = typer.Typer(no_args_is_help=True)
app.add_typer(llm_app, name="llm")


@dataclass
class AppContext:
    """Shared CLI state loaded from global options."""

    config: AppConfig


def _configure_logging(config: AppConfig, verbose: bool) -> None:
    level = logging.DEBUG if verbose else getattr(logging, config.log_level.upper(), logging.INFO)
    logging.basicConfig(
        level=level,
        stream=sys.stderr,
        format="%(levelname)s %(name)s: %(message)s",
        force=True,
    )


def _open(config: AppConfig) -> tuple[Ledger, StepRegistry, EngineRegistry]:
    """Build ledger and plugin registries; initialize the database on first touch."""
    engine = create_engine_for(config.db_path)
    init_db(engine)
    return Ledger(engine), StepRegistry.load(), EngineRegistry.load()


def _load_playbook_text(path: Path) -> tuple[Playbook, str]:
    yaml_text = path.read_text(encoding="utf-8")
    playbook = load_playbook(path)
    return playbook, yaml_text


def _check_playbook(playbook: Playbook, registry: StepRegistry) -> list[dict[str, str]]:
    return [
        {"path": problem.path, "message": problem.message}
        for problem in registry.check_playbook(playbook)
    ]


def _ensure_registered(
    ledger: Ledger,
    playbook: Playbook,
    yaml_text: str,
    *,
    note: str | None,
) -> tuple[int, str]:
    pipeline_id = ledger.find_pipeline_id(playbook.name)
    if pipeline_id is None:
        return ledger.register_pipeline(playbook, yaml_text, note=note)
    _, current_yaml = ledger.get_current_playbook(pipeline_id)
    if current_yaml != yaml_text:
        return ledger.register_pipeline(playbook, yaml_text, note=note)
    public_id, _ = ledger.get_current_playbook(pipeline_id)
    return pipeline_id, public_id


def _manual_trigger(playbook: Playbook) -> ManualTrigger:
    trigger = playbook.trigger
    if isinstance(trigger, ManualTrigger):
        return trigger
    if isinstance(trigger, FolderWatchTrigger):
        return ManualTrigger(
            type="manual",
            path=trigger.path,
            glob=trigger.glob,
            ordinal_regex=trigger.ordinal_regex,
            arrival_order_ordinals=trigger.arrival_order_ordinals,
        )
    raise typer.BadParameter(f"unsupported trigger type for scan: {trigger.type}")


def _scan_playbook(ledger: Ledger, pipeline_id: int, playbook: Playbook) -> int:
    manual = _manual_trigger(playbook)
    arrival = manual.arrival_order_ordinals
    sink = ledger_sink(ledger, pipeline_id, arrival_order=arrival)
    return ManualScanService(manual, playbook.dedup, sink).run()


def _build_runner(
    ledger: Ledger,
    registry: StepRegistry,
    engines: EngineRegistry,
    playbook: Playbook,
    pipeline_id: int,
    version: str,
    workdir_root: Path,
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


@app.callback()
def cli(
    ctx: typer.Context,
    config_path: Annotated[
        Path | None,
        typer.Option("--config", help="Path to config TOML"),
    ] = None,
    verbose: Annotated[
        bool, typer.Option("-v", "--verbose", help="Debug logging on stderr")
    ] = False,
) -> None:
    """Conveyor automation CLI."""
    try:
        config = load_config(config_path)
    except ConfigError as exc:
        typer.echo(str(exc), err=True)
        raise typer.Exit(code=2) from exc
    _configure_logging(config, verbose)
    ctx.obj = AppContext(config=config)


@app.command()
def init(
    ctx: typer.Context,
    config_path: Annotated[
        Path | None,
        typer.Option("--config", help="Write config to this path instead of the default"),
    ] = None,
) -> None:
    """Create config file, database, and workdir directories."""
    from conveyor.core.config import DEFAULT_CONFIG_FILE

    assert isinstance(ctx.obj, AppContext)
    config_file = config_path.expanduser() if config_path is not None else DEFAULT_CONFIG_FILE
    try:
        write_default_config(config_file)
    except ConfigError as exc:
        typer.echo(str(exc), err=True)
        raise typer.Exit(code=1) from exc
    config = load_config(config_file)
    config.db_path.parent.mkdir(parents=True, exist_ok=True)
    config.workdir_root.mkdir(parents=True, exist_ok=True)
    _open(config)
    output.print_line(f"config: {config_file}")
    output.print_line(f"database: {config.db_path}")
    output.print_line(f"workdirs: {config.workdir_root}")


@app.command()
def check(
    ctx: typer.Context,
    playbook_path: Path,
    as_json: Annotated[bool, typer.Option("--json", help="Emit JSON to stdout")] = False,
) -> None:
    """Validate a playbook file."""
    assert isinstance(ctx.obj, AppContext)
    _, registry, _ = _open(ctx.obj.config)
    try:
        playbook, _ = _load_playbook_text(playbook_path)
    except (PlaybookSyntaxError, PlaybookValidationError, OSError) as exc:
        if as_json:
            output.emit_json({"valid": False, "problems": [{"path": "$", "message": str(exc)}]})
        else:
            typer.echo(str(exc), err=True)
        raise typer.Exit(code=2) from exc
    problems = _check_playbook(playbook, registry)
    if as_json:
        output.emit_json({"valid": not problems, "problems": problems})
    elif problems:
        for problem in problems:
            output.print_line(f"{problem['path']}: {problem['message']}")
    else:
        output.print_line(
            f"{playbook.name}: valid ({len(playbook.steps)} steps, trigger={playbook.trigger.type})"
        )
    if problems:
        raise typer.Exit(code=1)


@app.command()
def run(
    ctx: typer.Context,
    playbook_path: Path,
    oneshot: Annotated[
        bool, typer.Option("--oneshot", help="Scan once, drain queue, exit")
    ] = False,
    note: Annotated[str | None, typer.Option("--note", help="Playbook version note")] = None,
    as_json: Annotated[bool, typer.Option("--json", help="Emit JSON summary to stdout")] = False,
) -> None:
    """Run a playbook pipeline."""
    assert isinstance(ctx.obj, AppContext)
    config = ctx.obj.config
    ledger, registry, engines = _open(config)
    try:
        playbook, yaml_text = _load_playbook_text(playbook_path)
    except (PlaybookSyntaxError, PlaybookValidationError, OSError) as exc:
        typer.echo(str(exc), err=True)
        raise typer.Exit(code=2) from exc
    problems = _check_playbook(playbook, registry)
    if problems:
        if as_json:
            output.emit_json({"valid": False, "problems": problems})
        else:
            for problem in problems:
                typer.echo(f"{problem['path']}: {problem['message']}", err=True)
        raise typer.Exit(code=1)
    try:
        pipeline_id, version = _ensure_registered(ledger, playbook, yaml_text, note=note)
        stale_after = timedelta(minutes=config.stale_after_minutes)
        ledger.reconcile(pipeline_id, stale_after=stale_after, policy=config.reconcile_policy)
        runner = _build_runner(
            ledger, registry, engines, playbook, pipeline_id, version, config.workdir_root
        )
        if oneshot:
            scanned = _scan_playbook(ledger, pipeline_id, playbook)
            processed = runner.run_until_idle()
            summary = {
                "pipeline": playbook.name,
                "version": version,
                "scanned": scanned,
                "processed": processed,
            }
            if as_json:
                output.emit_json(summary)
            else:
                output.print_line(
                    f"{playbook.name} ({version}): scanned {scanned}, processed {processed}"
                )
            return
        service = PipelineService(
            ledger=ledger,
            runner=runner,
            playbook=playbook,
            pipeline_id=pipeline_id,
            stale_after=stale_after,
            reconcile_policy=config.reconcile_policy,
        )
        shutting_down = False
        running = True

        def _handle_signal(_signum: int, _frame: object) -> None:
            nonlocal shutting_down, running
            if shutting_down:
                raise SystemExit(130)
            shutting_down = True
            running = False
            logger.info("shutdown requested; finishing in-flight task")
            service.stop()

        signal.signal(signal.SIGINT, _handle_signal)
        signal.signal(signal.SIGTERM, _handle_signal)
        service.start()
        while running:
            time.sleep(0.2)
        if as_json:
            output.emit_json({"pipeline": playbook.name, "version": version, "status": "stopped"})
        else:
            output.print_line(f"stopped {playbook.name} ({version})")
    except (RunnerError, LedgerError) as exc:
        typer.echo(str(exc), err=True)
        raise typer.Exit(code=2) from exc


@app.command()
def status(
    ctx: typer.Context,
    as_json: Annotated[bool, typer.Option("--json", help="Emit JSON to stdout")] = False,
) -> None:
    """Show pipeline summaries and task counts."""
    assert isinstance(ctx.obj, AppContext)
    ledger, _, _ = _open(ctx.obj.config)
    summaries: list[tuple[PipelineSummary, dict[TaskStatus, int], int, int]] = []
    for summary in ledger.list_pipelines():
        counts = ledger.counts(summary.id)
        flags = ledger.list_open_flags(pipeline_id=summary.id)
        max_level = max((flag.level for flag in flags), default=0)
        summaries.append((summary, counts, len(flags), max_level))
    if as_json:
        output.emit_json(
            {
                "pipelines": [
                    {
                        "name": summary.name,
                        "current_version": summary.current_version,
                        "counts": counts,
                        "open_flags": open_flags,
                        "max_flag_level": max_level,
                    }
                    for summary, counts, open_flags, max_level in summaries
                ]
            }
        )
        return
    if not summaries:
        output.print_line("(no pipelines)")
        return
    for summary, counts, open_flags, max_level in summaries:
        count_text = output.format_status_counts(cast(dict[str, int], counts))
        output.print_line(
            f"{summary.name} {summary.current_version or '-'} "
            f"{count_text} flags={open_flags} max_level={max_level}"
        )


@app.command("tasks")
def tasks_cmd(
    ctx: typer.Context,
    pipeline_name: str,
    status_filter: Annotated[
        TaskStatus | None,
        typer.Option("--status", help="Filter by task status"),
    ] = None,
    limit: Annotated[int, typer.Option("--limit", help="Max rows")] = 100,
    as_json: Annotated[bool, typer.Option("--json", help="Emit JSON to stdout")] = False,
) -> None:
    """List tasks for a pipeline."""
    assert isinstance(ctx.obj, AppContext)
    ledger, _, _ = _open(ctx.obj.config)
    pipeline_id = ledger.find_pipeline_id(pipeline_name)
    if pipeline_id is None:
        typer.echo(f"unknown pipeline: {pipeline_name}", err=True)
        raise typer.Exit(code=1)
    rows = ledger.list_tasks(pipeline_id, status=status_filter, limit=limit)
    payload = [
        {
            "id": task.id,
            "ordinal": task.ordinal,
            "status": task.status,
            "source": Path(task.source_ref).name,
            "updated_at": output.iso_timestamp(task.updated_at),
        }
        for task in rows
    ]
    if as_json:
        output.emit_json({"pipeline": pipeline_name, "tasks": payload})
        return
    output.print_table(
        ["id", "ordinal", "status", "source", "updated_at"],
        [
            [
                str(item["id"]),
                "" if item["ordinal"] is None else str(item["ordinal"]),
                str(item["status"]),
                str(item["source"]),
                str(item["updated_at"] or ""),
            ]
            for item in payload
        ],
    )


def _task_detail_message(
    task: TaskView,
    attempts: list[dict[str, Any]],
    flags: list[dict[str, Any]],
) -> None:
    """Print a human-readable skip/error line when the task or its attempts carry one."""
    if task.error:
        output.print_line(f"error: {task.error}")
        return
    for item in attempts:
        attempt_error = item.get("error")
        if attempt_error:
            label = "skip" if task.status == "skipped" else "error"
            output.print_line(f"{label}: {attempt_error}")
            return
    if flags and task.status in ("skipped", "failed", "flagged"):
        label = "skip" if task.status == "skipped" else "error"
        output.print_line(f"{label}: {flags[0]['message']}")


@app.command("task")
def task_cmd(
    ctx: typer.Context,
    task_id: int,
    as_json: Annotated[bool, typer.Option("--json", help="Emit JSON to stdout")] = False,
) -> None:
    """Show one task with branch attempts and flags."""
    assert isinstance(ctx.obj, AppContext)
    ledger, _, _ = _open(ctx.obj.config)
    try:
        task = ledger.get_task(task_id)
    except LedgerError as exc:
        typer.echo(str(exc), err=True)
        raise typer.Exit(code=1) from exc
    attempts = [
        {
            "id": attempt.id,
            "branch": attempt.branch_name,
            "attempt": attempt.attempt_no,
            "ok": attempt.ok,
            "last_step_id": attempt.last_step_id,
            "error": attempt.error,
            "finished_at": output.iso_timestamp(attempt.finished_at),
        }
        for attempt in ledger.list_branch_attempts(task_id)
    ]
    flags = [
        {
            "id": flag.id,
            "level": flag.level,
            "kind": flag.kind,
            "message": flag.message,
            "created_at": output.iso_timestamp(flag.created_at),
        }
        for flag in ledger.list_open_flags(pipeline_id=task.pipeline_id)
        if flag.task_id == task_id
    ]
    payload = {
        "id": task.id,
        "pipeline_id": task.pipeline_id,
        "status": task.status,
        "ordinal": task.ordinal,
        "source_ref": task.source_ref,
        "workdir": task.workdir,
        "current_branch": task.current_branch,
        "attempts": task.attempts,
        "error": task.error,
        "created_at": output.iso_timestamp(task.created_at),
        "updated_at": output.iso_timestamp(task.updated_at),
        "branch_attempts": attempts,
        "flags": flags,
    }
    if as_json:
        output.emit_json(payload)
        return
    output.print_line(f"task {task.id} status={task.status} ordinal={task.ordinal}")
    output.print_line(f"source: {task.source_ref}")
    output.print_line(f"workdir: {task.workdir or '-'}")
    _task_detail_message(task, attempts, flags)
    if attempts:
        output.print_line("attempts:")
        for item in attempts:
            branch = item["branch"] or "-"
            output.print_line(
                f"  #{item['attempt']} branch={branch} ok={item['ok']} "
                f"last={item['last_step_id'] or '-'} error={item['error'] or '-'}"
            )
    if flags:
        output.print_line("flags:")
        for item in flags:
            output.print_line(
                f"  #{item['id']} level={item['level']} kind={item['kind']} {item['message']}"
            )


@app.command()
def retry(
    ctx: typer.Context,
    task_id: int,
    as_json: Annotated[bool, typer.Option("--json", help="Emit JSON to stdout")] = False,
) -> None:
    """Re-queue a failed or flagged task."""
    assert isinstance(ctx.obj, AppContext)
    ledger, _, _ = _open(ctx.obj.config)
    try:
        ledger.transition(task_id, "pending")
        task = ledger.get_task(task_id)
    except IllegalTransitionError as exc:
        typer.echo(str(exc), err=True)
        raise typer.Exit(code=1) from exc
    except LedgerError as exc:
        typer.echo(str(exc), err=True)
        raise typer.Exit(code=1) from exc
    if as_json:
        output.emit_json({"id": task.id, "status": task.status})
    else:
        output.print_line(f"task {task.id} -> {task.status}")


@app.command()
def flags(
    ctx: typer.Context,
    pipeline_name: Annotated[str | None, typer.Option("--pipeline", help="Pipeline name")] = None,
    min_level: Annotated[int, typer.Option("--min-level", help="Minimum flag level")] = 0,
    as_json: Annotated[bool, typer.Option("--json", help="Emit JSON to stdout")] = False,
) -> None:
    """List open flags."""
    assert isinstance(ctx.obj, AppContext)
    ledger, _, _ = _open(ctx.obj.config)
    pipeline_id: int | None = None
    if pipeline_name is not None:
        pipeline_id = ledger.find_pipeline_id(pipeline_name)
        if pipeline_id is None:
            typer.echo(f"unknown pipeline: {pipeline_name}", err=True)
            raise typer.Exit(code=1)
    rows = ledger.list_open_flags(pipeline_id=pipeline_id, min_level=min_level)
    payload = [
        {
            "id": flag.id,
            "pipeline_id": flag.pipeline_id,
            "level": flag.level,
            "kind": flag.kind,
            "task_id": flag.task_id,
            "message": flag.message,
            "age": output.format_age(flag.created_at),
            "created_at": output.iso_timestamp(flag.created_at),
        }
        for flag in rows
    ]
    if as_json:
        output.emit_json({"flags": payload})
        return
    output.print_table(
        ["id", "level", "kind", "task", "age", "message"],
        [
            [
                str(item["id"]),
                str(item["level"]),
                str(item["kind"]),
                "" if item["task_id"] is None else str(item["task_id"]),
                str(item["age"]),
                str(item["message"]),
            ]
            for item in payload
        ],
    )


@app.command("resolve-flag")
def resolve_flag_cmd(
    ctx: typer.Context,
    flag_id: int,
    note: Annotated[str, typer.Option("--note", help="Resolution note")],
    as_json: Annotated[bool, typer.Option("--json", help="Emit JSON to stdout")] = False,
) -> None:
    """Resolve an open flag."""
    assert isinstance(ctx.obj, AppContext)
    ledger, _, _ = _open(ctx.obj.config)
    try:
        ledger.resolve_flag(flag_id, note)
    except LedgerError as exc:
        typer.echo(str(exc), err=True)
        raise typer.Exit(code=1) from exc
    if as_json:
        output.emit_json({"id": flag_id, "resolved": True, "resolution": note})
    else:
        output.print_line(f"flag {flag_id} resolved")


@app.command()
def steps(
    ctx: typer.Context,
    as_json: Annotated[bool, typer.Option("--json", help="Emit JSON to stdout")] = False,
) -> None:
    """List registered step plugins."""
    assert isinstance(ctx.obj, AppContext)
    _, registry, _ = _open(ctx.obj.config)
    payload = [
        {"id": step_id, "engines": sorted(engines), "origin": origin}
        for step_id, engines, origin in registry.list_step_metadata()
    ]
    if as_json:
        output.emit_json({"steps": payload})
        return
    output.print_table(
        ["id", "engines", "origin"],
        [[str(item["id"]), ",".join(item["engines"]), str(item["origin"])] for item in payload],
    )


@app.command("dry-run")
def dry_run(
    ctx: typer.Context,
    playbook_path: Path,
    sample: Annotated[
        Path,
        typer.Option("--sample", exists=True, file_okay=False, dir_okay=True, readable=True),
    ],
    glob: Annotated[str, typer.Option("--glob", help="Sample filename glob")] = "*",
    as_json: Annotated[bool, typer.Option("--json", help="Emit JSON report to stdout")] = False,
) -> None:
    """Run a sandboxed dry-run rehearsal; never touches the production ledger."""
    import tempfile

    assert isinstance(ctx.obj, AppContext)
    try:
        playbook, yaml_text = _load_playbook_text(playbook_path)
    except (PlaybookSyntaxError, PlaybookValidationError, OSError) as exc:
        typer.echo(str(exc), err=True)
        raise typer.Exit(code=2) from exc

    problems = _check_playbook(playbook, StepRegistry.load())
    if problems:
        for problem in problems:
            typer.echo(f"{problem['path']}: {problem['message']}", err=True)
        raise typer.Exit(code=2)

    registry = StepRegistry.load()
    engines = EngineRegistry.load()
    sandbox_parent = Path(tempfile.mkdtemp(prefix="conveyor-dry-run-"))
    session = DryRunSession.create(
        playbook=playbook,
        version_public_id="cli-dry-run",
        sample_dir=sample,
        glob=glob,
        registry=registry,
        engines=engines,
        sandbox_root=sandbox_parent,
        yaml_text=yaml_text,
    )
    try:
        session.run_all()
        report = session.report()
    finally:
        session.close()

    if as_json:
        output.emit_json(report)
    else:
        rows: list[list[str]] = []
        for task in report["tasks"]:
            for step in task["steps"]:
                rows.append(
                    [
                        str(task["sample"]),
                        str(step["seq"]),
                        str(step["id"]),
                        str(step["status"]),
                        str(step.get("message") or ""),
                    ]
                )
        output.print_table(["sample", "seq", "step", "status", "message"], rows)

    any_bad = any(
        step["status"] in ("fail", "skip") for task in report["tasks"] for step in task["steps"]
    ) or any(task["status"] in ("failed", "skipped") for task in report["tasks"])
    raise typer.Exit(code=1 if any_bad else 0)


@llm_app.command("check")
def llm_check(
    ctx: typer.Context,
    as_json: Annotated[bool, typer.Option("--json", help="Emit JSON to stdout")] = False,
) -> None:
    """Smoke-test the configured LLM provider with a minimal completion."""
    assert isinstance(ctx.obj, AppContext)
    config = ctx.obj.config
    try:
        client = build_client(config)
        response = client.complete(
            [Message(role="user", content="Reply with the single word: ok")],
            purpose="llm_check",
            max_tokens=8,
        )
    except LLMNotConfiguredError as exc:
        typer.echo(str(exc), err=True)
        raise typer.Exit(code=1) from exc
    except LLMAuthError as exc:
        typer.echo(str(exc), err=True)
        raise typer.Exit(code=1) from exc
    except LLMError as exc:
        typer.echo(str(exc), err=True)
        raise typer.Exit(code=2) from exc

    payload = {
        "provider": client.provider,
        "model": response.model,
        "duration_s": response.duration_s,
        "usage": {
            "input_tokens": response.usage.input_tokens,
            "output_tokens": response.usage.output_tokens,
        },
        "text": response.text,
    }
    if as_json:
        output.emit_json(payload)
    else:
        typer.echo(
            f"provider={client.provider} model={response.model} "
            f"latency={response.duration_s:.2f}s "
            f"tokens={response.usage.input_tokens}+{response.usage.output_tokens} "
            f"text={response.text!r}"
        )
    raise typer.Exit(code=0)


@app.command()
def serve(
    ctx: typer.Context,
    host: Annotated[str | None, typer.Option("--host", help="Bind host")] = None,
    port: Annotated[int | None, typer.Option("--port", help="Bind port")] = None,
) -> None:
    """Start the web UI and pipeline service manager."""
    import uvicorn

    from conveyor.web.app import create_app

    assert isinstance(ctx.obj, AppContext)
    config = ctx.obj.config
    bind_host = host if host is not None else config.web_host
    bind_port = port if port is not None else config.web_port
    if bind_host not in ("127.0.0.1", "localhost"):
        typer.echo(
            "WARNING: binding to a non-local host without authentication — "
            "anyone on the network can control pipelines.",
            err=True,
        )
    app = create_app(config)
    uvicorn.run(app, host=bind_host, port=bind_port, log_level="info")


def main() -> None:
    """Console script entry point."""
    app()


if __name__ == "__main__":
    main()
