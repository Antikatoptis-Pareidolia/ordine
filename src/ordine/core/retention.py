"""Task workdir retention and cleanup.

Owns filesystem cleanup of terminal task workdirs. Must never import cli, web, executors, or llm.
"""

from __future__ import annotations

import logging
import shutil
from collections import Counter
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path

from ordine.core.config import AppConfig
from ordine.core.ledger import TERMINAL_TASK_STATUSES, Ledger, TaskView

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class CleanupReport:
    """Summary of a workdir cleanup pass."""

    scanned: int
    deleted: int
    bytes_freed: int
    kept_reasons: dict[str, int] = field(default_factory=dict)


def _utcnow() -> datetime:
    return datetime.now(tz=UTC)


def _dir_size(path: Path) -> int:
    total = 0
    for child in path.rglob("*"):
        if child.is_file():
            try:
                total += child.stat().st_size
            except OSError:
                continue
    return total


def _is_under_root(path: Path, root: Path) -> bool:
    try:
        path.resolve().relative_to(root.resolve())
    except ValueError:
        return False
    return True


def cleanup_workdirs(
    ledger: Ledger,
    workdir_root: Path,
    *,
    older_than: timedelta,
    keep_statuses: frozenset[str] = frozenset({"flagged", "failed"}),
    dry_run: bool = False,
) -> CleanupReport:
    """Delete terminal task workdirs older than the cutoff.

    Deletes only when the task is terminal, ``finished_at`` is before the cutoff, the status is not
    in ``keep_statuses``, and the on-disk path exists under ``workdir_root``. Never touches exports,
    non-terminal tasks, or the database rows beyond ``clear_workdir``. Sets ``tasks.workdir`` to
    NULL after a successful delete via ``ledger.clear_workdir``.
    """
    cutoff = _utcnow() - older_than
    resolved_root = workdir_root.expanduser().resolve()
    kept: Counter[str] = Counter()
    deleted = 0
    bytes_freed = 0
    candidates = ledger.list_retention_candidates()
    scanned = len(candidates)

    for task in candidates:
        reason = _skip_reason(task, cutoff=cutoff, keep_statuses=keep_statuses)
        if reason is not None:
            kept[reason] += 1
            continue

        if task.workdir is None:
            kept["no_workdir"] += 1
            continue
        workdir_path = Path(task.workdir).expanduser()
        if not workdir_path.exists():
            if not dry_run:
                ledger.clear_workdir(task.id)
            kept["missing_on_disk"] += 1
            continue

        if not _is_under_root(workdir_path, resolved_root):
            kept["outside_workdir_root"] += 1
            logger.warning("retention skipped task %s path outside root: %s", task.id, workdir_path)
            continue

        size = _dir_size(workdir_path)
        if dry_run:
            deleted += 1
            bytes_freed += size
            continue

        shutil.rmtree(workdir_path)
        ledger.clear_workdir(task.id)
        deleted += 1
        bytes_freed += size
        logger.info("retention deleted workdir for task %s (%s bytes)", task.id, size)

    return CleanupReport(
        scanned=scanned,
        deleted=deleted,
        bytes_freed=bytes_freed,
        kept_reasons=dict(kept),
    )


def _skip_reason(
    task: TaskView,
    *,
    cutoff: datetime,
    keep_statuses: frozenset[str],
) -> str | None:
    if task.status not in TERMINAL_TASK_STATUSES:
        return "non_terminal"
    if task.workdir is None:
        return "no_workdir"
    if task.status in keep_statuses:
        return f"kept_{task.status}"
    if task.finished_at is None:
        return "no_finished_at"
    finished = task.finished_at if task.finished_at.tzinfo else task.finished_at.replace(tzinfo=UTC)
    if finished >= cutoff:
        return "too_recent"
    return None


def keep_statuses_for_cleanup(
    config: AppConfig,
    *,
    include_failed: bool = False,
) -> frozenset[str]:
    """Resolve which terminal statuses to preserve during cleanup."""
    if include_failed:
        return frozenset({"flagged"})
    if config.retention_keep_failed:
        return frozenset({"flagged", "failed"})
    return frozenset({"flagged"})


def run_configured_cleanup(
    ledger: Ledger,
    config: AppConfig,
    *,
    days: int | None = None,
    include_failed: bool = False,
    dry_run: bool = False,
) -> CleanupReport:
    """Run retention cleanup using application config defaults."""
    return cleanup_workdirs(
        ledger,
        config.workdir_root,
        older_than=timedelta(days=days if days is not None else config.retention_days),
        keep_statuses=keep_statuses_for_cleanup(config, include_failed=include_failed),
        dry_run=dry_run,
    )
