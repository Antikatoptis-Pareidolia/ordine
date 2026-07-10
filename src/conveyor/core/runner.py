"""Pipeline runner: task sequencing, retries, recovery branches, and flags.

Owns per-task execution and PipelineService composition. Must never implement step logic;
steps never see the ledger directly.
"""

from __future__ import annotations

import json
import logging
import threading
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Literal, cast

from conveyor.core.engines import EngineRegistry
from conveyor.core.errors import RunnerError
from conveyor.core.ledger import Ledger, TaskStatus, TaskView
from conveyor.core.naming import LedgerNamingService
from conveyor.core.playbook import Playbook, StepSpec
from conveyor.core.registry import StepRegistry
from conveyor.core.steps import Step, StepContext, StepResult
from conveyor.core.triggers import (
    FolderWatchService,
    ManualScanService,
    build_trigger_service,
    ledger_sink,
)
from conveyor.core.workdir import TaskWorkdir

logger = logging.getLogger(__name__)


def _utcnow() -> datetime:
    return datetime.now(tz=UTC)


def _iso(dt: datetime | None) -> str | None:
    return None if dt is None else dt.astimezone(UTC).isoformat()


@dataclass
class StepLogEntry:
    seq: int
    id: str
    branch: str | None
    attempt: int
    status: str
    output: str | None
    message: str | None
    started_at: datetime
    finished_at: datetime


class PipelineRunner:
    """Execute playbook steps for claimed ledger tasks."""

    def __init__(
        self,
        *,
        ledger: Ledger,
        registry: StepRegistry,
        engines: EngineRegistry,
        playbook: Playbook,
        pipeline_id: int,
        workdir_root: Path,
        playbook_version: str | None = None,
    ) -> None:
        problems = registry.check_playbook(playbook)
        if problems:
            detail = "; ".join(f"{e.path}: {e.message}" for e in problems)
            raise RunnerError(f"playbook validation failed: {detail}")
        try:
            engines.get(playbook.engine)
        except Exception as exc:
            raise RunnerError(f"unknown engine {playbook.engine!r}") from exc
        self._ledger = ledger
        self._registry = registry
        self._engines = engines
        self._playbook = playbook
        self._pipeline_id = pipeline_id
        self._workdir_root = workdir_root.expanduser()
        self._engine = engines.get(playbook.engine)
        self._playbook_version = playbook_version or "unknown"
        self._step_log: list[StepLogEntry] = []

    def run_once(self) -> TaskStatus | None:
        """Claim and run the next pending task, or return None when idle."""
        task = self._ledger.claim_next(self._pipeline_id)
        if task is None:
            return None
        return self.run_task(task)

    def run_until_idle(self) -> int:
        """Process tasks until the queue is empty; return count processed."""
        processed = 0
        while True:
            status = self.run_once()
            if status is None:
                return processed
            processed += 1

    def run_task(self, task: TaskView) -> TaskStatus:
        """Execute all primary steps for *task* and return the terminal status."""
        self._step_log = []
        try:
            return self._run_task_inner(task)
        except Exception:
            logger.exception("runner error on task %s", task.id)
            self._ledger.raise_flag(
                self._pipeline_id,
                task_id=task.id,
                level=1,
                kind="runner_error",
                message=f"runner error on task {task.id}",
            )
            self._ledger.transition(task.id, "failed", error="runner error")
            self._finalize_task_json(task.id, "failed")
            return "failed"

    def _run_task_inner(self, task: TaskView) -> TaskStatus:
        workdir = TaskWorkdir.create(self._workdir_root, self._playbook.name, task.id)
        self._ledger.set_workdir(task.id, workdir.path)
        self._write_task_json(task, workdir.path, status="processing")

        source = Path(task.source_ref)
        input_path = source if source.exists() else None

        for index, step in enumerate(self._playbook.steps, start=1):
            result = self._run_with_policy(task, workdir, step, index, input_path)
            if result.status == "ok":
                input_path = result.output_path if result.output_path is not None else input_path
                continue
            if result.status == "skip":
                self._ledger.raise_flag(
                    self._pipeline_id,
                    task_id=task.id,
                    level=1,
                    kind=result.flag_kind or "task_skipped",
                    message=f"step {step.id} skipped: {result.message}",
                )
                self._ledger.transition(task.id, "skipped")
                self._finalize_task_json(task.id, "skipped")
                return "skipped"
            policy = step.on_failure if step.on_failure is not None else self._playbook.on_failure
            terminal: TaskStatus = "flagged" if policy.then == "mark_failed" else "skipped"
            self._ledger.transition(task.id, terminal)
            self._finalize_task_json(task.id, terminal)
            return terminal

        self._ledger.transition(task.id, "done")
        self._finalize_task_json(task.id, "done")
        return "done"

    def _run_with_policy(
        self,
        task: TaskView,
        workdir: TaskWorkdir,
        step: StepSpec,
        index: int,
        step_input: Path | None,
    ) -> StepResult:
        policy = step.on_failure if step.on_failure is not None else self._playbook.on_failure
        groups: list[tuple[str | None, int, list[StepSpec]]] = [
            (None, policy.retries, [step]),
            *[(b.name, b.retries, b.steps) for b in policy.branches],
        ]
        last = StepResult(status="fail", message="no attempts executed")
        last_step_id = step.id
        for branch_no, (branch_name, retries, seq) in enumerate(groups):
            for attempt_no in range(1, retries + 2):
                attempt_id = self._ledger.start_attempt(task.id, branch_name, attempt_no)
                last, last_step_id = self._run_sequence(
                    task,
                    workdir,
                    seq,
                    index,
                    branch_name,
                    branch_no,
                    step_input,
                    attempt_no,
                )
                self._ledger.finish_attempt(
                    attempt_id,
                    ok=last.status == "ok",
                    last_step_id=last_step_id,
                    error=last.message,
                )
                if last.status == "ok":
                    if branch_name is not None:
                        self._ledger.set_current_branch(task.id, branch_name)
                    return last
                if last.status == "skip":
                    return last
            level = self._ledger.next_flag_level(task.id)
            self._ledger.raise_flag(
                self._pipeline_id,
                task_id=task.id,
                level=level,
                kind=last.flag_kind or "task_failure",
                message=(
                    f"step {step.id} [{branch_name or 'primary'}] exhausted after "
                    f"{retries + 1} attempt(s): {last.message}"
                ),
            )
        return last

    def _run_sequence(
        self,
        task: TaskView,
        workdir: TaskWorkdir,
        seq: list[StepSpec],
        primary_index: int,
        branch_name: str | None,
        branch_no: int,
        seq_input: Path | None,
        attempt_no: int,
    ) -> tuple[StepResult, str]:
        naming = LedgerNamingService(self._ledger, self._pipeline_id, task.id)
        current_input = seq_input
        last_step_id = seq[0].id

        for seq_index, step_spec in enumerate(seq, start=1):
            last_step_id = step_spec.id
            if branch_name is None:
                step_dir = workdir.step_dir(primary_index, step_spec.id)
            else:
                step_dir = workdir.step_dir(
                    seq_index,
                    step_spec.id,
                    branch=branch_name,
                    branch_no=branch_no,
                )
            step_logger = workdir.step_logger(step_dir)
            params = self._registry.validate_params(step_spec.id, step_spec.params)
            ctx = StepContext(
                task_id=task.id,
                pipeline_name=self._playbook.name,
                source_ref=task.source_ref,
                ordinal=task.ordinal,
                input_path=current_input,
                step_dir=step_dir,
                logger=step_logger,
                naming=naming,
            )
            started = _utcnow()
            step_impl = cast(type[Step], self._registry.get(step_spec.id))
            result = self._engine.run_step(step_impl, ctx, params)
            finished = _utcnow()
            self._step_log.append(
                StepLogEntry(
                    seq=primary_index if branch_name is None else seq_index,
                    id=step_spec.id,
                    branch=branch_name,
                    attempt=attempt_no,
                    status=result.status,
                    output=str(result.output_path) if result.output_path else None,
                    message=result.message,
                    started_at=started,
                    finished_at=finished,
                )
            )
            if result.status != "ok":
                return result, last_step_id
            current_input = result.output_path if result.output_path is not None else current_input

        return StepResult(status="ok", output_path=current_input), last_step_id

    def _write_task_json(self, task: TaskView, workdir: Path, *, status: str) -> None:
        payload = self._task_json_payload(task, status)
        (workdir / "task.json").write_text(
            json.dumps(payload, indent=2) + "\n",
            encoding="utf-8",
        )

    def _finalize_task_json(self, task_id: int, status: TaskStatus) -> None:
        task = self._ledger.get_task(task_id)
        if task.workdir is None:
            return
        self._write_task_json(task, Path(task.workdir), status=status)

    def _task_json_payload(self, task: TaskView, status: str) -> dict[str, Any]:
        steps_payload = [
            {
                "seq": entry.seq,
                "id": entry.id,
                "branch": entry.branch,
                "attempt": entry.attempt,
                "status": entry.status,
                "output": entry.output,
                "message": entry.message,
                "started_at": _iso(entry.started_at),
                "finished_at": _iso(entry.finished_at),
            }
            for entry in self._step_log
        ]
        return {
            "task_id": task.id,
            "pipeline": self._playbook.name,
            "playbook_version": self._playbook_version,
            "source_ref": task.source_ref,
            "ordinal": task.ordinal,
            "status": status,
            "steps": steps_payload,
            "created_at": _iso(task.created_at),
            "finished_at": _iso(_utcnow() if status != "processing" else None),
        }


class PipelineService:
    """Compose trigger ingestion, reconcile, and the sequential worker loop."""

    def __init__(
        self,
        *,
        ledger: Ledger,
        runner: PipelineRunner,
        playbook: Playbook,
        pipeline_id: int,
        stale_after: timedelta = timedelta(minutes=15),
        reconcile_policy: Literal["retry", "fail"] = "retry",
    ) -> None:
        self._ledger = ledger
        self._runner = runner
        self._playbook = playbook
        self._pipeline_id = pipeline_id
        self._stale_after = stale_after
        self._reconcile_policy = reconcile_policy
        self._stop = threading.Event()
        self._worker: threading.Thread | None = None
        self._trigger: FolderWatchService | ManualScanService | None = None
        self._started = False

    def start(self) -> None:
        """Reconcile stale tasks, start the trigger, and begin the worker loop."""
        if self._started:
            return
        self._started = True
        self._stop.clear()
        self._ledger.reconcile(
            self._pipeline_id,
            stale_after=self._stale_after,
            policy=self._reconcile_policy,
        )
        arrival = getattr(self._playbook.trigger, "arrival_order_ordinals", False)
        sink = ledger_sink(self._ledger, self._pipeline_id, arrival_order=arrival)
        trigger = build_trigger_service(self._playbook.trigger, self._playbook.dedup, sink)
        self._trigger = trigger
        if isinstance(trigger, FolderWatchService):
            trigger.start()
        self._worker = threading.Thread(
            target=self._worker_loop, name="pipeline-worker", daemon=True
        )
        self._worker.start()

    def stop(self) -> None:
        """Stop gracefully after the in-flight task completes."""
        if not self._started:
            return
        self._stop.set()
        if self._worker is not None:
            self._worker.join(timeout=120.0)
            self._worker = None
        if isinstance(self._trigger, FolderWatchService):
            self._trigger.stop()
        self._trigger = None
        self._started = False

    def _worker_loop(self) -> None:
        while not self._stop.is_set():
            try:
                status = self._runner.run_once()
            except Exception:
                logger.exception("unexpected worker error")
                status = None
            if status is None and not self._stop.is_set():
                self._stop.wait(0.5)
