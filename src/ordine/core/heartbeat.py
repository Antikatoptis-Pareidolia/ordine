"""Worker heartbeat sidecar files for observability (A6).

Owns write/read of per-pipeline heartbeat JSON next to the ledger DB so
``ordine status --json`` can diagnose orphan/degraded workers without taking
the write lock. Must never import cli, web, executors, or llm.
"""

from __future__ import annotations

import json
import os
from contextlib import suppress
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path


def heartbeat_dir_for(db_path: Path) -> Path:
    """Directory holding per-pipeline heartbeat files for *db_path*."""
    expanded = db_path.expanduser().resolve()
    return expanded.parent / f"{expanded.name}.heartbeats"


@dataclass(frozen=True)
class WorkerHeartbeat:
    """Snapshot of one pipeline worker's liveness."""

    pipeline_id: int
    pipeline_name: str
    pid: int
    alive: bool
    last_activity: datetime
    stop_failed: bool = False


def _path_for(db_path: Path, pipeline_id: int) -> Path:
    return heartbeat_dir_for(db_path) / f"{pipeline_id}.json"


def write_heartbeat(
    db_path: Path,
    *,
    pipeline_id: int,
    pipeline_name: str,
    alive: bool,
    last_activity: datetime | None = None,
    stop_failed: bool = False,
    pid: int | None = None,
) -> None:
    """Atomically write a heartbeat file for *pipeline_id*."""
    stamp = last_activity or datetime.now(tz=UTC)
    if stamp.tzinfo is None:
        stamp = stamp.replace(tzinfo=UTC)
    payload = {
        "pipeline_id": pipeline_id,
        "pipeline_name": pipeline_name,
        "pid": pid if pid is not None else os.getpid(),
        "alive": alive,
        "stop_failed": stop_failed,
        "last_activity": stamp.isoformat(),
    }
    directory = heartbeat_dir_for(db_path)
    directory.mkdir(parents=True, exist_ok=True)
    target = _path_for(db_path, pipeline_id)
    # Unique tmp name avoids races when pause/start overlap with a dying worker.
    tmp = target.with_name(f"{pipeline_id}.{os.getpid()}.heartbeat.tmp")
    try:
        tmp.write_text(json.dumps(payload, indent=None) + "\n", encoding="utf-8")
        tmp.replace(target)
    except OSError:
        with suppress(OSError):
            tmp.unlink(missing_ok=True)
        # Observability must never break the pipeline worker.


def clear_heartbeat(db_path: Path, pipeline_id: int) -> None:
    """Remove a heartbeat file after a clean stop (best-effort)."""
    path = _path_for(db_path, pipeline_id)
    with suppress(OSError):
        path.unlink(missing_ok=True)


def read_heartbeats(db_path: Path) -> list[WorkerHeartbeat]:
    """Load all heartbeat snapshots for *db_path* (ignores corrupt files)."""
    directory = heartbeat_dir_for(db_path)
    if not directory.is_dir():
        return []
    results: list[WorkerHeartbeat] = []
    for path in sorted(directory.glob("*.json")):
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
            stamp = datetime.fromisoformat(str(raw["last_activity"]))
            if stamp.tzinfo is None:
                stamp = stamp.replace(tzinfo=UTC)
            results.append(
                WorkerHeartbeat(
                    pipeline_id=int(raw["pipeline_id"]),
                    pipeline_name=str(raw.get("pipeline_name") or ""),
                    pid=int(raw.get("pid") or 0),
                    alive=bool(raw.get("alive")),
                    last_activity=stamp,
                    stop_failed=bool(raw.get("stop_failed")),
                )
            )
        except (OSError, KeyError, TypeError, ValueError, json.JSONDecodeError):
            continue
    return results


def pid_is_alive(pid: int) -> bool:
    """Return True when *pid* appears to be a live process (best-effort)."""
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return False
    return True
