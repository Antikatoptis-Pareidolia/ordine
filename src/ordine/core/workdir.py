"""Per-task artifact directory layout.

Owns the filesystem contract for step artifacts. Must never execute steps or touch the ledger.
"""

from __future__ import annotations

import logging
import re
from pathlib import Path

_UNSAFE_FS_CHARS = re.compile(r"[^a-z0-9._-]+")


def _sanitize_step_id(step_id: str) -> str:
    """Keep dots; replace anything outside [a-z0-9._-] with underscore."""
    return "".join(
        char if char in "abcdefghijklmnopqrstuvwxyz0123456789._-" else "_"
        for char in step_id.lower()
    )


def _sanitize_segment(value: str) -> str:
    cleaned = _UNSAFE_FS_CHARS.sub("_", value.lower())
    return cleaned.strip("_") or "unnamed"


class TaskWorkdir:
    """Task-scoped work directory with numbered step artifact folders."""

    def __init__(self, path: Path) -> None:
        self._path = path

    @classmethod
    def create(cls, root: Path, pipeline_name: str, task_id: int) -> TaskWorkdir:
        """Create the task work directory (idempotent)."""
        safe_pipeline = _sanitize_segment(pipeline_name)
        task_path = root.expanduser() / safe_pipeline / f"task_{task_id:06d}"
        task_path.mkdir(parents=True, exist_ok=True)
        return cls(task_path)

    @property
    def path(self) -> Path:
        return self._path

    def step_dir(
        self,
        index: int,
        step_id: str,
        branch: str | None = None,
        branch_no: int | None = None,
    ) -> Path:
        """Create and return a step artifact directory (idempotent)."""
        safe_step = _sanitize_step_id(step_id)
        step_name = f"{index:02d}_{safe_step}"

        if branch is not None:
            safe_branch = _sanitize_segment(branch)
            branch_prefix = (
                f"b{branch_no}_{safe_branch}" if branch_no is not None else f"b_{safe_branch}"
            )
            target = self._path / branch_prefix / step_name
        else:
            target = self._path / step_name
        target.mkdir(parents=True, exist_ok=True)
        return target

    def step_logger(self, step_dir: Path) -> logging.Logger:
        """Return a logger that writes INFO lines to ``step_dir/log.txt``."""
        log_file = (step_dir / "log.txt").resolve()
        logger_name = f"ordine.step.{step_dir.name}.{hash(log_file) & 0xFFFF:X}"
        logger = logging.getLogger(logger_name)
        logger.setLevel(logging.INFO)
        logger.propagate = False
        for handler in logger.handlers:
            if (
                isinstance(handler, logging.FileHandler)
                and Path(handler.baseFilename).resolve() == log_file
            ):
                return logger
        handler = logging.FileHandler(log_file, encoding="utf-8")
        handler.setLevel(logging.INFO)
        handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
        logger.addHandler(handler)
        return logger
