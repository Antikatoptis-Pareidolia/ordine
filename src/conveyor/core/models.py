"""SQLAlchemy ORM models for the Conveyor ledger.

Owns table definitions only. Must never contain business logic or import from
executors/web/cli/llm.
"""

from __future__ import annotations

from datetime import UTC, datetime

from sqlalchemy import (
    Boolean,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


def _utcnow() -> datetime:
    return datetime.now(tz=UTC)


class Base(DeclarativeBase):
    """Declarative base for ledger ORM models."""


class Pipeline(Base):
    __tablename__ = "pipelines"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    name: Mapped[str] = mapped_column(String, unique=True, nullable=False)
    current_version_id: Mapped[int | None] = mapped_column(
        Integer,
        ForeignKey(
            "playbook_versions.id",
            use_alter=True,
            name="fk_pipelines_current_version_id",
        ),
        nullable=True,
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)

    versions: Mapped[list[PlaybookVersion]] = relationship(
        back_populates="pipeline",
        foreign_keys="PlaybookVersion.pipeline_id",
    )


class PlaybookVersion(Base):
    __tablename__ = "playbook_versions"
    __table_args__ = (Index("ix_playbook_versions_pipeline_created", "pipeline_id", "created_at"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    pipeline_id: Mapped[int] = mapped_column(Integer, ForeignKey("pipelines.id"), nullable=False)
    public_id: Mapped[str] = mapped_column(String, unique=True, nullable=False)
    parent_public_id: Mapped[str | None] = mapped_column(String, nullable=True)
    yaml_text: Mapped[str] = mapped_column(Text, nullable=False)
    note: Mapped[str | None] = mapped_column(String, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)

    pipeline: Mapped[Pipeline] = relationship(
        back_populates="versions",
        foreign_keys=[pipeline_id],
    )


class Task(Base):
    __tablename__ = "tasks"
    __table_args__ = (
        UniqueConstraint("pipeline_id", "dedup_key", name="uq_tasks_pipeline_dedup"),
        Index("ix_tasks_pipeline_status", "pipeline_id", "status"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    pipeline_id: Mapped[int] = mapped_column(Integer, ForeignKey("pipelines.id"), nullable=False)
    playbook_version_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("playbook_versions.id"), nullable=False
    )
    source_ref: Mapped[str] = mapped_column(String, nullable=False)
    ordinal: Mapped[int | None] = mapped_column(Integer, nullable=True)
    dedup_key: Mapped[str | None] = mapped_column(String, nullable=True)
    status: Mapped[str] = mapped_column(String, nullable=False, default="pending")
    attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    current_branch: Mapped[str | None] = mapped_column(String, nullable=True)
    workdir: Mapped[str | None] = mapped_column(String, nullable=True)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, onupdate=_utcnow
    )


class BranchAttempt(Base):
    __tablename__ = "branch_attempts"
    __table_args__ = (Index("ix_branch_attempts_task_id", "task_id"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    task_id: Mapped[int] = mapped_column(Integer, ForeignKey("tasks.id"), nullable=False)
    branch_name: Mapped[str | None] = mapped_column(String, nullable=True)
    attempt_no: Mapped[int] = mapped_column(Integer, nullable=False)
    last_step_id: Mapped[str | None] = mapped_column(String, nullable=True)
    ok: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)


class NameReservation(Base):
    __tablename__ = "name_reservations"
    __table_args__ = (
        UniqueConstraint("pipeline_id", "ordinal", name="uq_name_res_pipeline_ordinal"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    pipeline_id: Mapped[int] = mapped_column(Integer, ForeignKey("pipelines.id"), nullable=False)
    ordinal: Mapped[int] = mapped_column(Integer, nullable=False)
    name: Mapped[str] = mapped_column(String, nullable=False)
    task_id: Mapped[int | None] = mapped_column(Integer, ForeignKey("tasks.id"), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)


class Flag(Base):
    __tablename__ = "flags"
    __table_args__ = (Index("ix_flags_pipeline_resolved", "pipeline_id", "resolved_at"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    pipeline_id: Mapped[int] = mapped_column(Integer, ForeignKey("pipelines.id"), nullable=False)
    task_id: Mapped[int | None] = mapped_column(Integer, ForeignKey("tasks.id"), nullable=True)
    level: Mapped[int] = mapped_column(Integer, nullable=False)
    kind: Mapped[str] = mapped_column(String, nullable=False)
    message: Mapped[str] = mapped_column(Text, nullable=False)
    resolved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    resolution: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)
