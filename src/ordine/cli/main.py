"""Ordine command-line interface.

Owns argument parsing and output formatting only. Must never implement pipeline business logic.
"""

from __future__ import annotations

import logging
import signal
import sys
import time
from dataclasses import dataclass
from datetime import timedelta
from importlib.metadata import PackageNotFoundError
from importlib.metadata import version as pkg_version
from pathlib import Path
from typing import Annotated, Any, cast

import typer

from ordine.cli import output
from ordine.cli.example_scaffold import scaffold_example
from ordine.core.config import AppConfig, is_loopback_bind, load_config, write_default_config
from ordine.core.db import create_engine_for, init_db
from ordine.core.dryrun import DryRunSession, playbook_contains_shell_run
from ordine.core.engines import EngineRegistry
from ordine.core.errors import (
    ConfigError,
    IllegalTransitionError,
    InstanceLockError,
    LedgerError,
    PlaybookSyntaxError,
    PlaybookValidationError,
    RunnerError,
)
from ordine.core.heartbeat import pid_is_alive, read_heartbeats
from ordine.core.instance_lock import InstanceLock
from ordine.core.ledger import Ledger, PipelineSummary, TaskStatus, TaskView
from ordine.core.playbook import (
    FolderWatchTrigger,
    ManifestTrigger,
    ManualTrigger,
    Playbook,
    load_playbook,
)
from ordine.core.registry import StepRegistry
from ordine.core.retention import run_configured_cleanup
from ordine.core.runner import PipelineRunner, PipelineService
from ordine.core.triggers import (
    ManifestTriggerService,
    ManualScanService,
    build_trigger_service,
    ledger_sink,
)
from ordine.llm.client import build_client
from ordine.llm.errors import LLMAuthError, LLMError, LLMNotConfiguredError
from ordine.llm.features.branches import apply_branch, suggest_branch
from ordine.llm.features.diagnosis import diagnose
from ordine.llm.features.drafting import draft_playbook
from ordine.llm.types import Message

logger = logging.getLogger(__name__)

app = typer.Typer(no_args_is_help=True, add_completion=False)
llm_app = typer.Typer(no_args_is_help=True)
app.add_typer(llm_app, name="llm")


@dataclass
class AppContext:
    """Shared CLI state loaded from global options."""

    config: AppConfig


def _package_version() -> str:
    try:
        return pkg_version("ordine")
    except PackageNotFoundError:
        from ordine import __version__

        return __version__


def _version_callback(value: bool) -> None:
    if value:
        typer.echo(f"ordine {_package_version()}")
        raise typer.Exit()


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


def _acquire_writer_lock(config: AppConfig) -> InstanceLock:
    """Acquire the single-writer lock for serve/run; exit on contention."""
    lock = InstanceLock(config.db_path)
    try:
        lock.acquire()
    except InstanceLockError as exc:
        typer.echo(str(exc), err=True)
        raise typer.Exit(code=2) from exc
    return lock


def _refuse_non_loopback(bind_host: str, *, acknowledged: bool) -> None:
    """Exit if binding off-loopback without an explicit no-auth acknowledgment."""
    if is_loopback_bind(bind_host):
        return
    if acknowledged:
        typer.echo(
            "WARNING: binding to a non-local host with i_understand_no_auth — "
            "anyone who can reach the port can control pipelines (no authentication).",
            err=True,
        )
        return
    typer.echo(
        f"Refusing to bind {bind_host!r}: Ordine has no authentication. "
        "Pass --i-understand-no-auth or set web.i_understand_no_auth = true in config "
        "to acknowledge the risk. Prefer a reverse proxy with auth for non-local access.",
        err=True,
    )
    raise typer.Exit(code=2)


def _load_playbook_text(path: Path) -> tuple[Playbook, str]:
    path = path.expanduser()
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
    if isinstance(playbook.trigger, ManifestTrigger):
        service = build_trigger_service(
            playbook.trigger,
            playbook.dedup,
            ledger=ledger,
            pipeline_id=pipeline_id,
        )
        if not isinstance(service, ManifestTriggerService):
            raise RuntimeError(
                f"internal error: manifest trigger returned unexpected service {type(service)!r}"
            )
        return service.run()
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
    version: Annotated[
        bool | None,
        typer.Option(
            "--version",
            callback=_version_callback,
            is_eager=True,
            help="Show version and exit",
        ),
    ] = None,
    config_path: Annotated[
        Path | None,
        typer.Option("--config", help="Path to config TOML"),
    ] = None,
    verbose: Annotated[
        bool, typer.Option("-v", "--verbose", help="Debug logging on stderr")
    ] = False,
) -> None:
    """Ordine automation CLI."""
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
    from ordine.core.config import DEFAULT_CONFIG_FILE

    if not isinstance(ctx.obj, AppContext):
        typer.echo("internal error: missing CLI context", err=True)
        raise typer.Exit(code=2)
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
    if not isinstance(ctx.obj, AppContext):
        typer.echo("internal error: missing CLI context", err=True)
        raise typer.Exit(code=2)
    registry = StepRegistry.load()
    try:
        playbook, _ = _load_playbook_text(playbook_path)
    except (PlaybookSyntaxError, PlaybookValidationError, OSError) as exc:
        message = (
            f"playbook not found: {playbook_path}"
            if isinstance(exc, FileNotFoundError)
            else str(exc)
        )
        if as_json:
            output.emit_json({"valid": False, "problems": [{"path": "$", "message": message}]})
        else:
            typer.echo(message, err=True)
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
    if not isinstance(ctx.obj, AppContext):
        typer.echo("internal error: missing CLI context", err=True)
        raise typer.Exit(code=2)
    config = ctx.obj.config
    writer_lock = _acquire_writer_lock(config)
    try:
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
                db_path=config.db_path,
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
                output.emit_json(
                    {"pipeline": playbook.name, "version": version, "status": "stopped"}
                )
            else:
                output.print_line(f"stopped {playbook.name} ({version})")
        except (RunnerError, LedgerError) as exc:
            typer.echo(str(exc), err=True)
            raise typer.Exit(code=2) from exc
    finally:
        writer_lock.release()


@app.command()
def status(
    ctx: typer.Context,
    as_json: Annotated[bool, typer.Option("--json", help="Emit JSON to stdout")] = False,
) -> None:
    """Show pipeline summaries and task counts."""
    if not isinstance(ctx.obj, AppContext):
        typer.echo("internal error: missing CLI context", err=True)
        raise typer.Exit(code=2)
    ledger, _, _ = _open(ctx.obj.config)
    summaries: list[tuple[PipelineSummary, dict[TaskStatus, int], int, int]] = []
    for summary in ledger.list_pipelines():
        counts = ledger.counts(summary.id)
        flags = ledger.list_open_flags(pipeline_id=summary.id)
        max_level = max((flag.level for flag in flags), default=0)
        summaries.append((summary, counts, len(flags), max_level))
    heartbeats = read_heartbeats(ctx.obj.config.db_path)
    workers = [
        {
            "pipeline_id": hb.pipeline_id,
            "pipeline_name": hb.pipeline_name,
            "pid": hb.pid,
            "alive": hb.alive and pid_is_alive(hb.pid),
            "stop_failed": hb.stop_failed,
            "last_activity": hb.last_activity.isoformat(),
        }
        for hb in heartbeats
    ]
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
                ],
                "workers": workers,
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
    for worker in workers:
        alive = "alive" if worker["alive"] else "stale"
        output.print_line(
            f"worker pipeline={worker['pipeline_name'] or worker['pipeline_id']} "
            f"pid={worker['pid']} {alive} stop_failed={worker['stop_failed']} "
            f"last={worker['last_activity']}"
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
    if not isinstance(ctx.obj, AppContext):
        typer.echo("internal error: missing CLI context", err=True)
        raise typer.Exit(code=2)
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
    if not isinstance(ctx.obj, AppContext):
        typer.echo("internal error: missing CLI context", err=True)
        raise typer.Exit(code=2)
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
    if not isinstance(ctx.obj, AppContext):
        typer.echo("internal error: missing CLI context", err=True)
        raise typer.Exit(code=2)
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
    if not isinstance(ctx.obj, AppContext):
        typer.echo("internal error: missing CLI context", err=True)
        raise typer.Exit(code=2)
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
    if not isinstance(ctx.obj, AppContext):
        typer.echo("internal error: missing CLI context", err=True)
        raise typer.Exit(code=2)
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
    if not isinstance(ctx.obj, AppContext):
        typer.echo("internal error: missing CLI context", err=True)
        raise typer.Exit(code=2)
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
    allow_shell: Annotated[
        bool,
        typer.Option(
            "--allow-shell",
            help="Execute shell.run for real (default: stub / no-op in dry-run)",
        ),
    ] = False,
    as_json: Annotated[bool, typer.Option("--json", help="Emit JSON report to stdout")] = False,
) -> None:
    """Run a sandboxed dry-run rehearsal; never touches the production ledger."""
    import tempfile

    if not isinstance(ctx.obj, AppContext):
        typer.echo("internal error: missing CLI context", err=True)
        raise typer.Exit(code=2)
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
    sandbox_parent = Path(tempfile.mkdtemp(prefix="ordine-dry-run-"))
    if playbook_contains_shell_run(playbook) and not allow_shell:
        typer.echo(
            "NOTE: shell.run will be stubbed; pass --allow-shell to execute commands.",
            err=True,
        )
    session = DryRunSession.create(
        playbook=playbook,
        version_public_id="cli-dry-run",
        sample_dir=sample,
        glob=glob,
        registry=registry,
        engines=engines,
        sandbox_root=sandbox_parent,
        yaml_text=yaml_text,
        allow_shell=allow_shell,
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
    if not isinstance(ctx.obj, AppContext):
        typer.echo("internal error: missing CLI context", err=True)
        raise typer.Exit(code=2)
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


@app.command("draft")
def draft_cmd(
    ctx: typer.Context,
    description: str,
    pipeline_name: Annotated[
        str | None, typer.Option("--pipeline", help="Revise an existing pipeline by name")
    ] = None,
    out: Annotated[Path | None, typer.Option("--out", help="Write YAML to file")] = None,
) -> None:
    """Draft a playbook from natural language (stdout or --out; never saves)."""
    if not isinstance(ctx.obj, AppContext):
        typer.echo("internal error: missing CLI context", err=True)
        raise typer.Exit(code=2)
    config = ctx.obj.config
    registry = StepRegistry.load()
    try:
        client = build_client(config)
    except LLMNotConfiguredError as exc:
        typer.echo(str(exc), err=True)
        raise typer.Exit(code=1) from exc
    current_yaml = None
    if pipeline_name:
        ledger, _, _ = _open(config)
        pipeline_id = ledger.find_pipeline_id(pipeline_name)
        if pipeline_id is None:
            typer.echo(f"unknown pipeline: {pipeline_name}", err=True)
            raise typer.Exit(code=2)
        _version, current_yaml = ledger.get_current_playbook(pipeline_id)
    try:
        result = draft_playbook(client, registry, description, current_yaml=current_yaml)
    except LLMError as exc:
        typer.echo(str(exc), err=True)
        raise typer.Exit(code=2) from exc
    if result.problems:
        for problem in result.problems:
            typer.echo(f"{problem.path}: {problem.message}", err=True)
    destination = out or Path("-")
    if str(destination) == "-":
        typer.echo(result.yaml_text)
    else:
        destination.write_text(result.yaml_text, encoding="utf-8")
    raise typer.Exit(code=0 if result.playbook is not None else 1)


@app.command("diagnose")
def diagnose_cmd(
    ctx: typer.Context,
    task_id: int,
    as_json: Annotated[bool, typer.Option("--json", help="Emit JSON to stdout")] = False,
) -> None:
    """Diagnose a flagged or failed task using LLM context."""
    if not isinstance(ctx.obj, AppContext):
        typer.echo("internal error: missing CLI context", err=True)
        raise typer.Exit(code=2)
    config = ctx.obj.config
    ledger, _, _ = _open(config)
    try:
        client = build_client(config)
    except LLMNotConfiguredError as exc:
        typer.echo(str(exc), err=True)
        raise typer.Exit(code=1) from exc
    try:
        result = diagnose(
            client,
            StepRegistry.load(),
            ledger,
            task_id,
            config.workdir_root,
        )
    except LLMError as exc:
        typer.echo(str(exc), err=True)
        raise typer.Exit(code=2) from exc
    payload = {
        "cause": result.cause,
        "confidence": result.confidence,
        "evidence": result.evidence,
        "suggestions": result.suggestions,
        "fixable_by_branch": result.fixable_by_branch,
    }
    if as_json:
        output.emit_json(payload)
    else:
        typer.echo(f"cause: {result.cause} ({result.confidence})")
        for item in result.evidence:
            typer.echo(f"  evidence: {item}")
        for item in result.suggestions:
            typer.echo(f"  suggestion: {item}")
    raise typer.Exit(code=0)


@app.command("approve-branch")
def approve_branch_cmd(
    ctx: typer.Context,
    task_id: int,
    apply: Annotated[
        bool,
        typer.Option("--apply", help="Apply the suggestion as a new current version"),
    ] = False,
    yes: Annotated[
        bool,
        typer.Option("--yes", help="Skip interactive confirm when applying"),
    ] = False,
    as_json: Annotated[bool, typer.Option("--json", help="Emit JSON to stdout")] = False,
) -> None:
    """Suggest (and optionally apply) an AI recovery branch for a task.

    Parity with the web AI Approve flow: suggest_branch then apply_branch.
    """
    if not isinstance(ctx.obj, AppContext):
        typer.echo("internal error: missing CLI context", err=True)
        raise typer.Exit(code=2)
    config = ctx.obj.config
    ledger, registry, _engines = _open(config)
    try:
        client = build_client(config)
    except LLMNotConfiguredError as exc:
        typer.echo(str(exc), err=True)
        raise typer.Exit(code=1) from exc
    try:
        suggestion = suggest_branch(client, registry, ledger, task_id, config.workdir_root)
    except (LLMError, LedgerError) as exc:
        typer.echo(str(exc), err=True)
        raise typer.Exit(code=2) from exc

    shell_risk = bool(
        suggestion.new_playbook is not None and playbook_contains_shell_run(suggestion.new_playbook)
    )
    payload = {
        "task_id": task_id,
        "branch": suggestion.branch.name,
        "rationale": suggestion.rationale,
        "target_step_index": suggestion.target_step_index,
        "problems": [{"path": p.path, "message": p.message} for p in suggestion.problems],
        "diff": suggestion.diff,
        "valid": suggestion.new_playbook is not None,
        "shell_run": shell_risk,
        "applied": False,
        "version": None,
    }
    if not apply:
        if as_json:
            output.emit_json(payload)
        else:
            typer.echo(f"branch: {suggestion.branch.name}")
            typer.echo(f"rationale: {suggestion.rationale}")
            if suggestion.problems:
                for problem in suggestion.problems:
                    typer.echo(f"problem: {problem.path}: {problem.message}", err=True)
            else:
                typer.echo(suggestion.diff or "(no diff)")
            if shell_risk:
                typer.echo("WARNING: suggested playbook includes shell.run", err=True)
            typer.echo("Re-run with --apply [--yes] to register as current version.", err=True)
        raise typer.Exit(code=0 if suggestion.new_playbook is not None else 1)

    if suggestion.new_playbook is None:
        typer.echo("cannot apply invalid branch suggestion", err=True)
        raise typer.Exit(code=1)

    if shell_risk and not yes:
        typer.echo(
            "Suggested branch includes shell.run; pass --yes to confirm apply.",
            err=True,
        )
        raise typer.Exit(code=1)
    if not yes:
        typer.echo("Pass --yes to apply the suggested branch without a TTY confirm.", err=True)
        raise typer.Exit(code=1)

    task = ledger.get_task(task_id)
    note = f"AI branch: {suggestion.branch.name}"
    try:
        version = apply_branch(ledger, task.pipeline_id, suggestion, note=note)
    except (ValueError, LedgerError) as exc:
        typer.echo(str(exc), err=True)
        raise typer.Exit(code=2) from exc
    payload["applied"] = True
    payload["version"] = version
    if as_json:
        output.emit_json(payload)
    else:
        typer.echo(f"approved {suggestion.branch.name} as {version}")
    raise typer.Exit(code=0)


@app.command()
def cleanup(
    ctx: typer.Context,
    days: Annotated[int | None, typer.Option("--days", help="Age threshold in days")] = None,
    include_failed: Annotated[
        bool,
        typer.Option("--include-failed", help="Also delete failed task workdirs (keeps flagged)"),
    ] = False,
    dry_run: Annotated[bool, typer.Option("--dry-run", help="Report without deleting")] = False,
    as_json: Annotated[bool, typer.Option("--json", help="Emit JSON to stdout")] = False,
) -> None:
    """Delete old terminal task workdirs (exports and the DB are untouched)."""
    if not isinstance(ctx.obj, AppContext):
        typer.echo("internal error: missing CLI context", err=True)
        raise typer.Exit(code=2)
    config = ctx.obj.config
    ledger, _, _ = _open(config)
    report = run_configured_cleanup(
        ledger,
        config,
        days=days,
        include_failed=include_failed,
        dry_run=dry_run,
    )
    payload = {
        "scanned": report.scanned,
        "deleted": report.deleted,
        "bytes_freed": report.bytes_freed,
        "kept_reasons": report.kept_reasons,
        "dry_run": dry_run,
    }
    if as_json:
        output.emit_json(payload)
    else:
        typer.echo(f"scanned: {report.scanned}")
        typer.echo(f"deleted: {report.deleted}")
        typer.echo(f"bytes_freed: {report.bytes_freed}")
        if report.kept_reasons:
            typer.echo("kept:")
            for reason, count in sorted(report.kept_reasons.items()):
                typer.echo(f"  {reason}: {count}")
    raise typer.Exit(code=0)


@app.command()
def example(
    ctx: typer.Context,
    directory: Annotated[
        Path | None,
        typer.Argument(help="Target directory (default: ~/ordine-demo)"),
    ] = None,
) -> None:
    """Scaffold a self-contained demo with sample images and playbooks."""
    if not isinstance(ctx.obj, AppContext):
        typer.echo("internal error: missing CLI context", err=True)
        raise typer.Exit(code=2)
    target = (directory or Path("~/ordine-demo")).expanduser()
    try:
        next_commands = scaffold_example(target)
    except ConfigError as exc:
        typer.echo(str(exc), err=True)
        raise typer.Exit(code=1) from exc
    typer.echo(f"Created demo at {target.resolve()}")
    typer.echo("")
    typer.echo("Next:")
    typer.echo(f"  cd {target}")
    for command in next_commands:
        typer.echo(f"  {command}")
    raise typer.Exit(code=0)


@app.command()
def serve(
    ctx: typer.Context,
    host: Annotated[
        str | None,
        typer.Option(
            "--host", help="Listen/bind address override (does not change Host allowlist)"
        ),
    ] = None,
    port: Annotated[int | None, typer.Option("--port", help="Bind port")] = None,
    i_understand_no_auth: Annotated[
        bool,
        typer.Option(
            "--i-understand-no-auth",
            help="Acknowledge binding off-loopback with no authentication",
        ),
    ] = False,
) -> None:
    """Start the web UI and pipeline service manager."""
    import uvicorn

    from ordine.web.app import create_app

    if not isinstance(ctx.obj, AppContext):
        typer.echo("internal error: missing CLI context", err=True)
        raise typer.Exit(code=2)
    config = ctx.obj.config
    writer_lock = _acquire_writer_lock(config)
    # --host/--port override the listen address only; Host allowlisting stays in config.
    bind_host = host if host is not None else config.web_bind
    bind_port = port if port is not None else config.web_port
    try:
        _refuse_non_loopback(
            bind_host,
            acknowledged=i_understand_no_auth or config.i_understand_no_auth,
        )
        if config.retention_on_serve_start:
            typer.echo(
                "NOTE: retention cleanup runs at web startup when "
                "retention.on_serve_start is true.",
                err=True,
            )
        app = create_app(config)
        try:
            uvicorn.run(app, host=bind_host, port=bind_port, log_level="info")
        except SystemExit as exc:
            if exc.code:
                typer.echo(
                    f"Could not bind {bind_host}:{bind_port}; try ordine serve --port PORT.",
                    err=True,
                )
            raise typer.Exit(code=int(exc.code or 0)) from exc
    finally:
        writer_lock.release()


def main() -> None:
    """Console script entry point."""
    app()


if __name__ == "__main__":
    main()
