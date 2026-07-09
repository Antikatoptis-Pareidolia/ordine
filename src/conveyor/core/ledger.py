"""Ledger API: task state machine, exactly-once guarantees, and audit trail.

Owns all database mutations for pipelines, tasks, branch attempts, flags, and name
reservations. Must never execute steps, watch folders, or import from executors/web/cli/llm.
"""

from __future__ import annotations

import logging
import uuid
from collections.abc import Generator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Literal

from sqlalchemy import Engine, func, select, text, update
from sqlalchemy.engine import CursorResult
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from conveyor.core.db import session_factory
from conveyor.core.errors import IllegalTransitionError, LedgerError
from conveyor.core.models import (
    BranchAttempt,
    Flag,
    NameReservation,
    Pipeline,
    PlaybookVersion,
    Task,
)
from conveyor.core.playbook import Playbook

logger = logging.getLogger(__name__)

TaskStatus = Literal["pending", "processing", "done", "skipped", "failed", "flagged"]

VALID_TRANSITIONS: dict[str, frozenset[str]] = {
    "pending": frozenset({"processing", "skipped"}),
    "processing": frozenset({"done", "skipped", "failed", "flagged", "pending"}),
    "done": frozenset(),
    "skipped": frozenset(),
    "failed": frozenset({"pending"}),
    "flagged": frozenset({"pending"}),
}

_TERMINAL_FINISHED: frozenset[str] = frozenset({"done", "skipped", "failed", "flagged"})


@dataclass(frozen=True)
class TaskView:
    """Read-only snapshot of a ledger task."""

    id: int
    pipeline_id: int
    playbook_version_id: int
    source_ref: str
    ordinal: int | None
    dedup_key: str | None
    status: TaskStatus
    attempts: int
    current_branch: str | None
    workdir: str | None
    error: str | None
    started_at: datetime | None
    finished_at: datetime | None
    created_at: datetime
    updated_at: datetime


@dataclass(frozen=True)
class VersionInfo:
    """Read-only snapshot of a stored playbook version."""

    public_id: str
    parent_public_id: str | None
    created_at: datetime
    note: str | None


@dataclass(frozen=True)
class FlagView:
    """Read-only snapshot of a ledger flag."""

    id: int
    pipeline_id: int
    task_id: int | None
    level: int
    kind: str
    message: str
    resolved_at: datetime | None
    resolution: str | None
    created_at: datetime


def _utcnow() -> datetime:
    return datetime.now(tz=UTC)


def _task_view(task: Task) -> TaskView:
    return TaskView(
        id=task.id,
        pipeline_id=task.pipeline_id,
        playbook_version_id=task.playbook_version_id,
        source_ref=task.source_ref,
        ordinal=task.ordinal,
        dedup_key=task.dedup_key,
        status=task.status,  # type: ignore[arg-type]
        attempts=task.attempts,
        current_branch=task.current_branch,
        workdir=task.workdir,
        error=task.error,
        started_at=task.started_at,
        finished_at=task.finished_at,
        created_at=task.created_at,
        updated_at=task.updated_at,
    )


def _flag_view(flag: Flag) -> FlagView:
    return FlagView(
        id=flag.id,
        pipeline_id=flag.pipeline_id,
        task_id=flag.task_id,
        level=flag.level,
        kind=flag.kind,
        message=flag.message,
        resolved_at=flag.resolved_at,
        resolution=flag.resolution,
        created_at=flag.created_at,
    )


class Ledger:
    """Public API for the Conveyor SQLite ledger."""

    def __init__(self, engine: Engine) -> None:
        self._engine = engine
        self._session_factory = session_factory(engine)

    @contextmanager
    def _session(self) -> Generator[Session, None, None]:
        session = self._session_factory()
        try:
            yield session
            session.commit()
        except Exception:
            session.rollback()
            raise
        finally:
            session.close()

    @contextmanager
    def _immediate(self) -> Generator[Session, None, None]:
        session = self._session_factory()
        try:
            session.execute(text("BEGIN IMMEDIATE"))
            yield session
            session.commit()
        except Exception:
            session.rollback()
            raise
        finally:
            session.close()

    def _get_pipeline(self, session: Session, pipeline_id: int) -> Pipeline:
        pipeline = session.get(Pipeline, pipeline_id)
        if pipeline is None:
            raise LedgerError(f"unknown pipeline id {pipeline_id}")
        return pipeline

    def _get_task_row(self, session: Session, task_id: int) -> Task:
        task = session.get(Task, task_id)
        if task is None:
            raise LedgerError(f"unknown task id {task_id}")
        return task

    def _version_by_public_id(
        self, session: Session, pipeline_id: int, public_id: str
    ) -> PlaybookVersion:
        version = session.scalar(
            select(PlaybookVersion).where(
                PlaybookVersion.pipeline_id == pipeline_id,
                PlaybookVersion.public_id == public_id,
            )
        )
        if version is None:
            raise LedgerError(f"unknown playbook version {public_id!r} for pipeline {pipeline_id}")
        return version

    def register_pipeline(
        self,
        playbook: Playbook,
        yaml_text: str,
        note: str | None = None,
        *,
        parent_public_id: str | None = None,
        make_current: bool = True,
    ) -> tuple[int, str]:
        """Create pipeline if new, store an immutable playbook version, optionally set current."""
        with self._session() as session:
            pipeline = session.scalar(select(Pipeline).where(Pipeline.name == playbook.name))
            previous_public_id: str | None = None
            if pipeline is None:
                pipeline = Pipeline(name=playbook.name)
                session.add(pipeline)
                session.flush()
            elif pipeline.current_version_id is not None:
                previous = session.get(PlaybookVersion, pipeline.current_version_id)
                if previous is not None:
                    previous_public_id = previous.public_id

            if parent_public_id is None and playbook.meta is not None:
                parent_public_id = playbook.meta.parent_version_id
            if parent_public_id is None:
                parent_public_id = previous_public_id

            placeholder = f"tmp_{uuid.uuid4().hex}"
            version = PlaybookVersion(
                pipeline_id=pipeline.id,
                public_id=placeholder,
                parent_public_id=parent_public_id,
                yaml_text=yaml_text,
                note=note,
            )
            session.add(version)
            session.flush()
            version.public_id = f"pv_{version.id:04d}"

            if make_current:
                pipeline.current_version_id = version.id

            return pipeline.id, version.public_id

    def set_current_version(self, pipeline_id: int, public_id: str) -> None:
        """Point the pipeline at an existing stored version."""
        with self._session() as session:
            pipeline = self._get_pipeline(session, pipeline_id)
            version = self._version_by_public_id(session, pipeline_id, public_id)
            pipeline.current_version_id = version.id

    def get_version_yaml(self, pipeline_id: int, public_id: str) -> str:
        """Return yaml_text for a stored playbook version."""
        with self._session() as session:
            version = self._version_by_public_id(session, pipeline_id, public_id)
            return version.yaml_text

    def get_current_playbook(self, pipeline_id: int) -> tuple[str, str]:
        """Return (public_id, yaml_text) for the pipeline's current version."""
        with self._session() as session:
            pipeline = self._get_pipeline(session, pipeline_id)
            if pipeline.current_version_id is None:
                raise LedgerError(f"pipeline {pipeline_id} has no current version")
            version = session.get(PlaybookVersion, pipeline.current_version_id)
            if version is None:
                raise LedgerError(f"pipeline {pipeline_id} current version row missing")
            return version.public_id, version.yaml_text

    def list_versions(self, pipeline_id: int) -> list[VersionInfo]:
        """List playbook versions newest-first."""
        with self._session() as session:
            self._get_pipeline(session, pipeline_id)
            rows = session.scalars(
                select(PlaybookVersion)
                .where(PlaybookVersion.pipeline_id == pipeline_id)
                .order_by(PlaybookVersion.created_at.desc())
            ).all()
            return [
                VersionInfo(
                    public_id=row.public_id,
                    parent_public_id=row.parent_public_id,
                    created_at=row.created_at,
                    note=row.note,
                )
                for row in rows
            ]

    def create_task(
        self,
        pipeline_id: int,
        source_ref: str,
        dedup_key: str | None,
        ordinal: int | None = None,
    ) -> int | None:
        """Insert a pending task; return None when dedup_key already exists for the pipeline."""
        session = self._session_factory()
        try:
            pipeline = self._get_pipeline(session, pipeline_id)
            if pipeline.current_version_id is None:
                raise LedgerError(f"pipeline {pipeline_id} has no current version")
            task = Task(
                pipeline_id=pipeline_id,
                playbook_version_id=pipeline.current_version_id,
                source_ref=source_ref,
                ordinal=ordinal,
                dedup_key=dedup_key,
                status="pending",
            )
            session.add(task)
            session.commit()
            return task.id
        except IntegrityError:
            # Broad catch is intentional: dedup is the only unique constraint reachable from valid inputs.
            session.rollback()
            return None
        finally:
            session.close()

    def claim_next(self, pipeline_id: int) -> TaskView | None:
        """Atomically claim the oldest pending task for processing."""
        with self._immediate() as session:
            for _ in range(2):
                task = session.scalar(
                    select(Task)
                    .where(Task.pipeline_id == pipeline_id, Task.status == "pending")
                    .order_by(Task.id)
                    .limit(1)
                )
                if task is None:
                    return None
                now = _utcnow()
                updated = session.execute(
                    update(Task)
                    .where(Task.id == task.id, Task.status == "pending")
                    .values(
                        status="processing",
                        attempts=task.attempts + 1,
                        started_at=now,
                        updated_at=now,
                    )
                )
                if isinstance(updated, CursorResult) and updated.rowcount == 1:
                    session.refresh(task)
                    return _task_view(task)
            return None

    def transition(
        self,
        task_id: int,
        target: TaskStatus,
        *,
        error: str | None = None,
    ) -> None:
        """Move a task to *target* if the transition is legal."""
        with self._session() as session:
            task = self._get_task_row(session, task_id)
            allowed = VALID_TRANSITIONS.get(task.status, frozenset())
            if target not in allowed:
                raise IllegalTransitionError(task_id, task.status, target)
            now = _utcnow()
            task.status = target
            task.error = error
            task.updated_at = now
            if target in _TERMINAL_FINISHED:
                task.finished_at = now
            if target == "pending":
                task.finished_at = None
            if target == "processing":
                task.started_at = now

    def set_workdir(self, task_id: int, workdir: Path) -> None:
        """Persist the per-task work directory path."""
        with self._session() as session:
            task = self._get_task_row(session, task_id)
            task.workdir = str(workdir.expanduser())
            task.updated_at = _utcnow()

    def get_task(self, task_id: int) -> TaskView:
        """Return a task snapshot."""
        with self._session() as session:
            return _task_view(self._get_task_row(session, task_id))

    def list_tasks(
        self,
        pipeline_id: int,
        status: TaskStatus | None = None,
        limit: int = 100,
        offset: int = 0,
    ) -> list[TaskView]:
        """List tasks for a pipeline with optional status filter."""
        with self._session() as session:
            query = select(Task).where(Task.pipeline_id == pipeline_id)
            if status is not None:
                query = query.where(Task.status == status)
            query = query.order_by(Task.id).limit(limit).offset(offset)
            return [_task_view(row) for row in session.scalars(query).all()]

    def counts(self, pipeline_id: int) -> dict[TaskStatus, int]:
        """Return per-status task counts for a pipeline."""
        statuses: list[TaskStatus] = [
            "pending",
            "processing",
            "done",
            "skipped",
            "failed",
            "flagged",
        ]
        result: dict[TaskStatus, int] = dict.fromkeys(statuses, 0)
        with self._session() as session:
            rows = session.execute(
                select(Task.status, func.count())
                .where(Task.pipeline_id == pipeline_id)
                .group_by(Task.status)
            ).all()
            for status, count in rows:
                if status in result:
                    result[status] = int(count)
        return result

    def start_attempt(self, task_id: int, branch_name: str | None, attempt_no: int) -> int:
        """Record the start of a branch attempt."""
        with self._session() as session:
            self._get_task_row(session, task_id)
            attempt = BranchAttempt(
                task_id=task_id,
                branch_name=branch_name,
                attempt_no=attempt_no,
                ok=False,
            )
            session.add(attempt)
            session.flush()
            return attempt.id

    def finish_attempt(
        self,
        attempt_id: int,
        *,
        ok: bool,
        last_step_id: str | None,
        error: str | None,
    ) -> None:
        """Record the completion of a branch attempt."""
        with self._session() as session:
            attempt = session.get(BranchAttempt, attempt_id)
            if attempt is None:
                raise LedgerError(f"unknown branch attempt id {attempt_id}")
            attempt.ok = ok
            attempt.last_step_id = last_step_id
            attempt.error = error
            attempt.finished_at = _utcnow()

    def exhausted_branches(self, task_id: int) -> int:
        """Count branch groups with attempts but no successful completion."""
        with self._session() as session:
            attempts = session.scalars(
                select(BranchAttempt).where(BranchAttempt.task_id == task_id)
            ).all()
            by_branch: dict[str | None, list[BranchAttempt]] = {}
            for attempt in attempts:
                by_branch.setdefault(attempt.branch_name, []).append(attempt)
            exhausted = 0
            for group in by_branch.values():
                if group and not any(a.ok for a in group):
                    exhausted += 1
            return exhausted

    def next_flag_level(self, task_id: int) -> int:
        """Return the flag escalation level for a task."""
        return self.exhausted_branches(task_id)

    def raise_flag(
        self,
        pipeline_id: int,
        *,
        task_id: int | None,
        level: int,
        kind: str,
        message: str,
    ) -> int:
        """Create a new flag row."""
        with self._session() as session:
            flag = Flag(
                pipeline_id=pipeline_id,
                task_id=task_id,
                level=level,
                kind=kind,
                message=message,
            )
            session.add(flag)
            session.flush()
            return flag.id

    def resolve_flag(self, flag_id: int, resolution: str) -> None:
        """Mark a flag resolved."""
        with self._session() as session:
            flag = session.get(Flag, flag_id)
            if flag is None:
                raise LedgerError(f"unknown flag id {flag_id}")
            flag.resolved_at = _utcnow()
            flag.resolution = resolution

    def open_flags(self, pipeline_id: int, min_level: int = 0) -> list[FlagView]:
        """Return unresolved flags at or above *min_level*, highest level first."""
        with self._session() as session:
            rows = session.scalars(
                select(Flag)
                .where(
                    Flag.pipeline_id == pipeline_id,
                    Flag.resolved_at.is_(None),
                    Flag.level >= min_level,
                )
                .order_by(Flag.level.desc(), Flag.created_at.desc())
            ).all()
            return [_flag_view(row) for row in rows]

    def next_arrival_ordinal(self, pipeline_id: int) -> int:
        """Return the next arrival-order ordinal for a pipeline (race-safe)."""
        with self._immediate() as session:
            current_max = session.scalar(
                select(func.max(Task.ordinal)).where(Task.pipeline_id == pipeline_id)
            )
            return 1 if current_max is None else int(current_max) + 1

    def reserve_name(
        self,
        pipeline_id: int,
        ordinal: int,
        name: str,
        task_id: int | None,
    ) -> str:
        """Reserve an ordinal→name binding; idempotent on ordinal."""
        with self._session() as session:
            existing = session.scalar(
                select(NameReservation).where(
                    NameReservation.pipeline_id == pipeline_id,
                    NameReservation.ordinal == ordinal,
                )
            )
            if existing is not None:
                if existing.name != name:
                    logger.warning(
                        "ordinal %s already reserved as %r; ignoring new name %r",
                        ordinal,
                        existing.name,
                        name,
                    )
                return existing.name
            reservation = NameReservation(
                pipeline_id=pipeline_id,
                ordinal=ordinal,
                name=name,
                task_id=task_id,
            )
            session.add(reservation)
            session.flush()
            return name

    def reserved_name(self, pipeline_id: int, ordinal: int) -> str | None:
        """Return a reserved name for an ordinal, if any."""
        with self._session() as session:
            row = session.scalar(
                select(NameReservation).where(
                    NameReservation.pipeline_id == pipeline_id,
                    NameReservation.ordinal == ordinal,
                )
            )
            return None if row is None else row.name

    def reconcile(
        self,
        pipeline_id: int,
        *,
        stale_after: timedelta,
        policy: Literal["retry", "fail"],
    ) -> int:
        """Recover stale processing tasks after a crash or hang."""
        cutoff = _utcnow() - stale_after
        touched = 0
        with self._session() as session:
            stale_tasks = session.scalars(
                select(Task).where(
                    Task.pipeline_id == pipeline_id,
                    Task.status == "processing",
                    Task.updated_at < cutoff,
                )
            ).all()
            for task in stale_tasks:
                if policy == "retry":
                    task.status = "pending"
                    task.finished_at = None
                    task.updated_at = _utcnow()
                else:
                    task.status = "failed"
                    task.error = "stale processing"
                    task.finished_at = _utcnow()
                    task.updated_at = _utcnow()
                    flag = Flag(
                        pipeline_id=pipeline_id,
                        task_id=task.id,
                        level=1,
                        kind="stale_processing",
                        message=f"task {task.id} stale in processing",
                    )
                    session.add(flag)
                touched += 1
        return touched

    def integrity_check(self) -> str:
        """Run PRAGMA integrity_check (for tests)."""
        with self._engine.connect() as conn:
            return str(conn.execute(text("PRAGMA integrity_check")).scalar_one())
