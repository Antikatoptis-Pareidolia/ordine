"""Dry-run lab sessions: sandboxed rehearsal with step-through semantics.

Owns ephemeral ledger execution against copied samples. Must never touch the production database
or write outside the session sandbox (except via redirected OUTPUT_DIR_PARAMS).
"""

from __future__ import annotations

import logging
import shutil
import sqlite3
import uuid
from dataclasses import dataclass, field
from fnmatch import fnmatch
from pathlib import Path
from typing import Any, Literal, cast

from sqlalchemy import Engine, create_engine, event
from sqlalchemy.pool import StaticPool

from conveyor.core.db import init_db
from conveyor.core.engines import EngineRegistry
from conveyor.core.errors import RunnerError
from conveyor.core.ledger import Ledger, TaskView
from conveyor.core.playbook import FailurePolicy, Playbook, RecoveryBranch, StepSpec
from conveyor.core.registry import StepRegistry
from conveyor.core.runner import execute_step_sequence, failure_policy_groups
from conveyor.core.steps import StepResult
from conveyor.core.triggers import ordinal_for_trigger
from conveyor.core.workdir import TaskWorkdir

logger = logging.getLogger(__name__)

LabStepStatus = Literal["pending", "ok", "fail", "skip", "replayed"]
TaskOverallState = Literal["pending", "paused", "running", "done", "skipped", "failed"]


@dataclass(frozen=True)
class LabStepState:
    """One executed or pending primary step in a dry-run task."""

    seq: int
    step_id: str
    status: LabStepStatus
    message: str | None
    input_artifact: Path | None
    output_artifact: Path | None
    branch_results: tuple[tuple[str, str, str | None], ...] = ()


@dataclass(frozen=True)
class TaskSummary:
    """High-level dry-run task view for UI and CLI."""

    index: int
    sample_name: str
    ordinal: int | None
    pointer: int
    status: TaskOverallState


@dataclass
class _TaskRuntime:
    task_id: int
    sample_path: Path
    ordinal: int | None
    pointer: int
    status: TaskOverallState
    step_states: list[LabStepState] = field(default_factory=list)
    current_input: Path | None = None
    paused_message: str | None = None


def _create_memory_engine() -> Engine:
    engine = create_engine(
        "sqlite+pysqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
        future=True,
    )

    def _pragma(dbapi_connection: object, _record: object) -> None:
        if isinstance(dbapi_connection, sqlite3.Connection):
            dbapi_connection.execute("PRAGMA foreign_keys=ON")

    event.listen(engine, "connect", _pragma)
    init_db(engine)
    return engine


def _redirect_step_params(
    step: StepSpec,
    *,
    location: str,
    registry: StepRegistry,
    sandbox: Path,
    mappings: list[tuple[str, str]],
) -> StepSpec:
    new_params = dict(step.params)
    try:
        step_cls = registry.get(step.id)
    except KeyError:
        return step
    for param_name in step_cls.OUTPUT_DIR_PARAMS:
        if param_name not in new_params:
            continue
        original = str(new_params[param_name])
        basename = Path(original).expanduser().name or param_name
        redirected = sandbox / "outputs" / basename
        redirected.mkdir(parents=True, exist_ok=True)
        new_params[param_name] = str(redirected)
        mappings.append((f"{location} · {param_name}", f"{original} → {redirected}"))
    return step.model_copy(update={"params": new_params})


def _redirect_failure_policy(
    policy: FailurePolicy,
    *,
    location: str,
    registry: StepRegistry,
    sandbox: Path,
    mappings: list[tuple[str, str]],
) -> FailurePolicy:
    branches: list[RecoveryBranch] = []
    for branch in policy.branches:
        branch_steps = [
            _redirect_step_params(
                branch_step,
                location=f"{location}.branch.{branch.name}.{index}",
                registry=registry,
                sandbox=sandbox,
                mappings=mappings,
            )
            for index, branch_step in enumerate(branch.steps)
        ]
        branches.append(branch.model_copy(update={"steps": branch_steps}))
    return policy.model_copy(update={"branches": branches})


def redirect_output_dirs(
    playbook: Playbook,
    registry: StepRegistry,
    sandbox: Path,
) -> tuple[Playbook, list[tuple[str, str]]]:
    """Rewrite OUTPUT_DIR_PARAMS on a working copy; return mappings for display."""
    mappings: list[tuple[str, str]] = []
    steps = [
        _redirect_step_params(
            step,
            location=f"steps.{index}",
            registry=registry,
            sandbox=sandbox,
            mappings=mappings,
        )
        for index, step in enumerate(playbook.steps)
    ]
    for index, step in enumerate(steps):
        if step.on_failure is not None:
            steps[index] = step.model_copy(
                update={
                    "on_failure": _redirect_failure_policy(
                        step.on_failure,
                        location=f"steps.{index}.on_failure",
                        registry=registry,
                        sandbox=sandbox,
                        mappings=mappings,
                    )
                }
            )
    on_failure = _redirect_failure_policy(
        playbook.on_failure,
        location="pipeline.on_failure",
        registry=registry,
        sandbox=sandbox,
        mappings=mappings,
    )
    return playbook.model_copy(update={"steps": steps, "on_failure": on_failure}), mappings


def playbook_contains_shell_run(playbook: Playbook) -> bool:
    """Return True when any step id is shell.run (including branch steps)."""
    for step in playbook.steps:
        if step.id == "shell.run":
            return True
        if step.on_failure is not None:
            for branch in step.on_failure.branches:
                if any(branch_step.id == "shell.run" for branch_step in branch.steps):
                    return True
    for branch in playbook.on_failure.branches:
        if any(branch_step.id == "shell.run" for branch_step in branch.steps):
            return True
    return False


def _playbook_step_ids(playbook: Playbook) -> list[str]:
    ids: list[str] = []

    def walk_policy(policy: FailurePolicy | None) -> None:
        if policy is None:
            return
        for branch in policy.branches:
            for branch_step in branch.steps:
                ids.append(branch_step.id)
                walk_policy(branch_step.on_failure)

    for step in playbook.steps:
        ids.append(step.id)
        walk_policy(step.on_failure)
    walk_policy(playbook.on_failure)
    return ids


def ordinal_dependent_step_ids(playbook: Playbook) -> list[str]:
    """Step ids that require a task ordinal (manifest rename, future llm.* steps)."""
    seen: set[str] = set()
    ordered: list[str] = []
    for step_id in _playbook_step_ids(playbook):
        if step_id != "file.rename_from_manifest" and not step_id.startswith("llm."):
            continue
        if step_id not in seen:
            seen.add(step_id)
            ordered.append(step_id)
    return ordered


def trigger_has_ordinal_source(playbook: Playbook) -> bool:
    """Return True when the trigger can supply task ordinals."""
    trigger = playbook.trigger
    return getattr(trigger, "ordinal_regex", None) is not None or getattr(
        trigger, "arrival_order_ordinals", False
    )


def lab_ordinal_warnings(playbook: Playbook) -> list[str]:
    """Warnings for lab setup when ordinals will be None but steps need them."""
    if trigger_has_ordinal_source(playbook):
        return []
    dependent = ordinal_dependent_step_ids(playbook)
    if not dependent:
        return []
    steps_list = ", ".join(dependent)
    return [
        "These steps will fail: no ordinal source configured on the trigger "
        f"({steps_list}). Set ordinal_regex from the filename pattern or enable "
        "arrival_order_ordinals."
    ]


class DryRunSession:
    """One rehearsal of one playbook version on copied sample files. Not thread-safe."""

    def __init__(
        self,
        *,
        playbook: Playbook,
        version_public_id: str,
        registry: StepRegistry,
        engines: EngineRegistry,
        sandbox: Path,
        ledger: Ledger,
        pipeline_id: int,
        engine: object,
        tasks: list[_TaskRuntime],
        output_redirections: list[tuple[str, str]],
        sample_dir: Path,
        glob_pattern: str,
        yaml_text: str,
        session_id: str,
    ) -> None:
        problems = registry.check_playbook(playbook)
        if problems:
            detail = "; ".join(f"{e.path}: {e.message}" for e in problems)
            raise RunnerError(f"playbook validation failed: {detail}")
        try:
            engines.get(playbook.engine)
        except Exception as exc:
            raise RunnerError(f"unknown engine {playbook.engine!r}") from exc
        self._playbook = playbook
        self._version_public_id = version_public_id
        self._registry = registry
        self._engines = engines
        self._sandbox = sandbox
        self._ledger = ledger
        self._pipeline_id = pipeline_id
        self._engine = engine
        self._tasks = tasks
        self._output_redirections = output_redirections
        self._sample_dir = sample_dir
        self._glob_pattern = glob_pattern
        self._yaml_text = yaml_text
        self._session_id = session_id
        self._closed = False

    @property
    def session_id(self) -> str:
        return self._session_id

    @property
    def sandbox(self) -> Path:
        return self._sandbox

    @property
    def playbook(self) -> Playbook:
        return self._playbook

    @property
    def version_public_id(self) -> str:
        return self._version_public_id

    @property
    def output_redirections(self) -> list[tuple[str, str]]:
        return list(self._output_redirections)

    @classmethod
    def create(
        cls,
        *,
        playbook: Playbook,
        version_public_id: str,
        sample_dir: Path,
        glob: str,
        registry: StepRegistry,
        engines: EngineRegistry,
        sandbox_root: Path,
        yaml_text: str,
        max_samples: int = 20,
    ) -> DryRunSession:
        """Copy samples into a fresh sandbox and register an ephemeral ledger pipeline."""
        session_id = uuid.uuid4().hex[:8]
        sandbox = sandbox_root / f"session_{session_id}"
        samples_target = sandbox / "samples"
        samples_target.mkdir(parents=True, exist_ok=True)

        sample_dir = sample_dir.expanduser().resolve()
        matched = sorted(
            path for path in sample_dir.iterdir() if path.is_file() and fnmatch(path.name, glob)
        )[:max_samples]
        if not matched:
            raise RunnerError(f"no sample files match {glob!r} in {sample_dir}")

        copied: list[Path] = []
        for source in matched:
            dest = samples_target / source.name
            shutil.copy2(source, dest)
            copied.append(dest)

        playbook, redirections = redirect_output_dirs(playbook, registry, sandbox)
        ledger_engine = _create_memory_engine()
        ledger = Ledger(ledger_engine)
        pipeline_id, _registered_version = ledger.register_pipeline(
            playbook,
            yaml_text,
            note="dry-run session",
        )

        tasks: list[_TaskRuntime] = []
        for index, sample_path in enumerate(copied):
            ordinal = ordinal_for_trigger(sample_path.name, playbook.trigger, arrival_index=index)
            task_id = ledger.create_task(
                pipeline_id,
                str(sample_path),
                dedup_key=f"dryrun:{sample_path.name}",
                ordinal=ordinal,
            )
            if task_id is None:
                raise RunnerError(f"failed to create task for {sample_path.name}")
            tasks.append(
                _TaskRuntime(
                    task_id=task_id,
                    sample_path=sample_path,
                    ordinal=ordinal,
                    pointer=0,
                    status="pending",
                    current_input=sample_path,
                )
            )

        engine = engines.get(playbook.engine)
        return cls(
            playbook=playbook,
            version_public_id=version_public_id,
            registry=registry,
            engines=engines,
            sandbox=sandbox,
            ledger=ledger,
            pipeline_id=pipeline_id,
            engine=engine,
            tasks=tasks,
            output_redirections=redirections,
            sample_dir=sample_dir,
            glob_pattern=glob,
            yaml_text=yaml_text,
            session_id=session_id,
        )

    def _ensure_open(self) -> None:
        if self._closed:
            raise RunnerError("dry-run session is closed")

    def _task_view(self, task_ix: int) -> TaskView:
        runtime = self._tasks[task_ix]
        return self._ledger.get_task(runtime.task_id)

    def _workdir(self, task_ix: int) -> TaskWorkdir:
        runtime = self._tasks[task_ix]
        task = self._ledger.get_task(runtime.task_id)
        if task.workdir is not None:
            return TaskWorkdir(Path(task.workdir))
        workdir = TaskWorkdir.create(self._sandbox / "workdirs", self._playbook.name, task.id)
        self._ledger.set_workdir(task.id, workdir.path)
        return workdir

    def tasks(self) -> list[TaskSummary]:
        self._ensure_open()
        return [
            TaskSummary(
                index=index,
                sample_name=runtime.sample_path.name,
                ordinal=runtime.ordinal,
                pointer=runtime.pointer,
                status=runtime.status,
            )
            for index, runtime in enumerate(self._tasks)
        ]

    def steps(self, task_ix: int) -> list[LabStepState]:
        self._ensure_open()
        return list(self._tasks[task_ix].step_states)

    def fast_forward_plan(self, task_ix: int) -> int:
        """Return the index of the first non-ok step (the resume point)."""
        self._ensure_open()
        for index, state in enumerate(self._tasks[task_ix].step_states):
            if state.status not in ("ok", "replayed"):
                return index
        return self._tasks[task_ix].pointer

    def _append_step_state(
        self,
        runtime: _TaskRuntime,
        *,
        step_index: int,
        step_id: str,
        status: LabStepStatus,
        message: str | None,
        input_artifact: Path | None,
        output_artifact: Path | None,
        branch_results: tuple[tuple[str, str, str | None], ...] = (),
    ) -> LabStepState:
        state = LabStepState(
            seq=step_index + 1,
            step_id=step_id,
            status=status,
            message=message,
            input_artifact=input_artifact,
            output_artifact=output_artifact,
            branch_results=branch_results,
        )
        if step_index < len(runtime.step_states):
            runtime.step_states[step_index] = state
        else:
            runtime.step_states.append(state)
        return state

    def _execute_primary_once(
        self,
        task_ix: int,
        *,
        replayed: bool = False,
    ) -> LabStepState:
        runtime = self._tasks[task_ix]
        if runtime.pointer >= len(self._playbook.steps):
            raise RunnerError("no more steps to execute")
        step_index = runtime.pointer
        step = self._playbook.steps[step_index]
        task = self._task_view(task_ix)
        workdir = self._workdir(task_ix)
        step_input = runtime.current_input
        result, _ = execute_step_sequence(
            task=task,
            workdir=workdir,
            seq=[step],
            primary_index=step_index + 1,
            branch_name=None,
            branch_no=0,
            seq_input=step_input,
            attempt_no=1,
            ledger=self._ledger,
            registry=self._registry,
            engine=self._engine,
            playbook=self._playbook,
            pipeline_id=self._pipeline_id,
        )
        status: LabStepStatus = "replayed" if replayed else cast(LabStepStatus, result.status)
        if replayed and result.status == "ok":
            status = "replayed"
        elif result.status == "ok":
            status = "ok"
        elif result.status == "skip":
            status = "skip"
        else:
            status = "fail"

        lab_state = self._append_step_state(
            runtime,
            step_index=step_index,
            step_id=step.id,
            status=status,
            message=result.message,
            input_artifact=step_input,
            output_artifact=result.output_path,
        )

        if result.status == "ok":
            runtime.pointer += 1
            runtime.current_input = (
                result.output_path if result.output_path is not None else step_input
            )
            runtime.paused_message = None
            if runtime.pointer >= len(self._playbook.steps):
                runtime.status = "done"
            else:
                runtime.status = "pending"
        elif result.status == "skip":
            runtime.status = "skipped"
            runtime.paused_message = result.message
        else:
            runtime.status = "paused"
            runtime.paused_message = result.message
        return lab_state

    def run_next_step(self, task_ix: int) -> LabStepState:
        """Execute one primary step with no retries or branches."""
        self._ensure_open()
        runtime = self._tasks[task_ix]
        if runtime.status in ("done", "skipped", "failed"):
            raise RunnerError(f"task {task_ix} is terminal ({runtime.status})")
        if runtime.status == "paused":
            raise RunnerError("task is paused on failure; retry, run branches, or fix from here")
        runtime.status = "running"
        return self._execute_primary_once(task_ix)

    def retry_step(self, task_ix: int) -> LabStepState:
        """Re-run the current failed primary step once."""
        self._ensure_open()
        runtime = self._tasks[task_ix]
        if runtime.status != "paused":
            raise RunnerError("retry is only available when the task is paused on failure")
        runtime.status = "running"
        return self._execute_primary_once(task_ix)

    def run_branches(self, task_ix: int) -> LabStepState:
        """Execute recovery branches for the paused step using runner-equivalent policy."""
        self._ensure_open()
        runtime = self._tasks[task_ix]
        if runtime.status != "paused":
            raise RunnerError("run branches is only available when the task is paused on failure")
        step_index = runtime.pointer
        step = self._playbook.steps[step_index]
        task = self._task_view(task_ix)
        workdir = self._workdir(task_ix)
        step_input = runtime.current_input
        groups = failure_policy_groups(step, self._playbook)[1:]
        branch_results: list[tuple[str, str, str | None]] = []
        last = StepResult(status="fail", message=runtime.paused_message or "primary failed")
        for branch_no, (branch_name, retries, seq) in enumerate(groups, start=1):
            assert branch_name is not None
            branch_status = "fail"
            branch_message: str | None = None
            for attempt_no in range(1, retries + 2):
                attempt_id = self._ledger.start_attempt(task.id, branch_name, attempt_no)
                result, _ = execute_step_sequence(
                    task=task,
                    workdir=workdir,
                    seq=seq,
                    primary_index=step_index + 1,
                    branch_name=branch_name,
                    branch_no=branch_no,
                    seq_input=step_input,
                    attempt_no=attempt_no,
                    ledger=self._ledger,
                    registry=self._registry,
                    engine=self._engine,
                    playbook=self._playbook,
                    pipeline_id=self._pipeline_id,
                )
                self._ledger.finish_attempt(
                    attempt_id,
                    ok=result.status == "ok",
                    last_step_id=seq[-1].id,
                    error=result.message,
                )
                if result.status == "ok":
                    branch_status = "ok"
                    branch_message = None
                    last = result
                    self._ledger.set_current_branch(task.id, branch_name)
                    break
                branch_message = result.message
            branch_results.append((branch_name, branch_status, branch_message))
            if branch_status == "ok":
                break

        if last.status == "ok":
            runtime.pointer += 1
            runtime.current_input = last.output_path if last.output_path is not None else step_input
            runtime.status = "done" if runtime.pointer >= len(self._playbook.steps) else "pending"
            runtime.paused_message = None
            primary_status: LabStepStatus = "ok"
        else:
            runtime.status = "paused"
            primary_status = "fail"

        return self._append_step_state(
            runtime,
            step_index=step_index,
            step_id=step.id,
            status=primary_status,
            message=runtime.paused_message,
            input_artifact=step_input,
            output_artifact=last.output_path if last.status == "ok" else None,
            branch_results=tuple(branch_results),
        )

    def _run_primary_runner_equivalent(self, task_ix: int) -> LabStepState:
        """Advance one primary step using runner failure policy (retries + branches)."""
        runtime = self._tasks[task_ix]
        step_index = runtime.pointer
        step = self._playbook.steps[step_index]
        task = self._task_view(task_ix)
        workdir = self._workdir(task_ix)
        step_input = runtime.current_input
        groups = failure_policy_groups(step, self._playbook)
        branch_results: list[tuple[str, str, str | None]] = []
        last = StepResult(status="fail", message="no attempts executed")
        for branch_no, (branch_name, retries, seq) in enumerate(groups):
            branch_status = "fail"
            branch_message: str | None = None
            for attempt_no in range(1, retries + 2):
                if branch_name is not None:
                    attempt_id = self._ledger.start_attempt(task.id, branch_name, attempt_no)
                result, _ = execute_step_sequence(
                    task=task,
                    workdir=workdir,
                    seq=seq,
                    primary_index=step_index + 1,
                    branch_name=branch_name,
                    branch_no=branch_no,
                    seq_input=step_input,
                    attempt_no=attempt_no,
                    ledger=self._ledger,
                    registry=self._registry,
                    engine=self._engine,
                    playbook=self._playbook,
                    pipeline_id=self._pipeline_id,
                )
                if branch_name is not None:
                    self._ledger.finish_attempt(
                        attempt_id,
                        ok=result.status == "ok",
                        last_step_id=seq[-1].id,
                        error=result.message,
                    )
                if result.status == "ok":
                    branch_status = "ok"
                    branch_message = None
                    last = result
                    if branch_name is not None:
                        self._ledger.set_current_branch(task.id, branch_name)
                        branch_results.append((branch_name, branch_status, branch_message))
                    break
                if result.status == "skip":
                    runtime.status = "skipped"
                    runtime.paused_message = result.message
                    return self._append_step_state(
                        runtime,
                        step_index=step_index,
                        step_id=step.id,
                        status="skip",
                        message=result.message,
                        input_artifact=step_input,
                        output_artifact=None,
                        branch_results=tuple(branch_results),
                    )
                branch_message = result.message
            if branch_name is not None and branch_status != "ok":
                branch_results.append((branch_name, branch_status, branch_message))
            if last.status == "ok":
                break

        if last.status == "ok":
            runtime.pointer += 1
            runtime.current_input = last.output_path if last.output_path is not None else step_input
            runtime.status = "done" if runtime.pointer >= len(self._playbook.steps) else "pending"
            runtime.paused_message = None
            status: LabStepStatus = "ok"
        else:
            runtime.status = "failed"
            runtime.paused_message = last.message
            status = "fail"

        return self._append_step_state(
            runtime,
            step_index=step_index,
            step_id=step.id,
            status=status,
            message=last.message,
            input_artifact=step_input,
            output_artifact=last.output_path if last.status == "ok" else None,
            branch_results=tuple(branch_results),
        )

    def run_to_end(self, task_ix: int) -> None:
        """Runner-equivalent loop for one task: retries, auto-branches, stop on exhaustion."""
        self._ensure_open()
        runtime = self._tasks[task_ix]
        while runtime.status not in ("done", "skipped", "failed"):
            if runtime.pointer >= len(self._playbook.steps):
                runtime.status = "done"
                break
            if runtime.status == "paused":
                runtime.status = "running"
                state = self._run_primary_runner_equivalent(task_ix)
                if state.status != "ok":
                    break
                continue
            runtime.status = "running"
            state = self._run_primary_runner_equivalent(task_ix)
            if state.status != "ok":
                break

    def run_all(self) -> None:
        """Runner-equivalent execution for every task."""
        self._ensure_open()
        for index in range(len(self._tasks)):
            self.run_to_end(index)

    def report(self) -> dict[str, Any]:
        self._ensure_open()
        return {
            "playbook": self._playbook.name,
            "version": self._version_public_id,
            "sandbox": str(self._sandbox),
            "tasks": [
                {
                    "index": summary.index,
                    "sample": summary.sample_name,
                    "ordinal": summary.ordinal,
                    "status": summary.status,
                    "steps": [
                        {
                            "seq": step.seq,
                            "id": step.step_id,
                            "status": step.status,
                            "message": step.message,
                            "input": str(step.input_artifact) if step.input_artifact else None,
                            "output": str(step.output_artifact) if step.output_artifact else None,
                            "branches": [
                                {"name": name, "status": status, "message": message}
                                for name, status, message in step.branch_results
                            ],
                        }
                        for step in self.steps(summary.index)
                    ],
                }
                for summary in self.tasks()
            ],
        }

    @classmethod
    def resume(
        cls,
        old: DryRunSession,
        new_playbook: Playbook,
        new_version: str,
        *,
        yaml_text: str,
    ) -> DryRunSession:
        """Fresh sandbox on the same source samples; replay prefix under the new playbook."""
        old._ensure_open()
        resume_point = max(old.fast_forward_plan(index) for index in range(len(old._tasks)))
        session = cls.create(
            playbook=new_playbook,
            version_public_id=new_version,
            sample_dir=old._sample_dir,
            glob=old._glob_pattern,
            registry=old._registry,
            engines=old._engines,
            sandbox_root=old._sandbox.parent,
            yaml_text=yaml_text,
            max_samples=len(old._tasks),
        )
        for task_ix in range(len(session._tasks)):
            runtime = session._tasks[task_ix]
            while runtime.pointer < resume_point:
                state = session._execute_primary_once(task_ix, replayed=True)
                if state.status not in ("ok", "replayed"):
                    runtime.status = "paused"
                    return session
            if runtime.pointer < len(session._playbook.steps):
                runtime.status = "pending"
        return session

    def close(self) -> None:
        """Delete the sandbox tree and dispose the ephemeral engine."""
        if self._closed:
            return
        self._closed = True
        shutil.rmtree(self._sandbox, ignore_errors=True)
