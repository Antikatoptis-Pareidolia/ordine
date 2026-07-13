"""Trigger services: folder watch, manual scan, and ledger sink.

Owns TaskCandidate production, settle detection, and startup rescan. Must never execute
pipeline steps or import from executors/web/cli/llm.
"""

from __future__ import annotations

import fnmatch
import hashlib
import logging
import re
import threading
import time
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from watchdog.events import FileSystemEvent, FileSystemEventHandler
from watchdog.observers import Observer
from watchdog.observers.api import BaseObserver

from conveyor.core.errors import ManifestError, TriggerError
from conveyor.core.ledger import Ledger
from conveyor.core.manifest import ManifestRow, load_manifest
from conveyor.core.playbook import FolderWatchTrigger, ManifestTrigger, ManualTrigger, Trigger

logger = logging.getLogger(__name__)

DedupMode = Literal["content_hash", "filename", "none"]
Sink = Callable[["TaskCandidate"], int | None]

_HASH_CHUNK = 1024 * 1024
_STOP_JOIN_TIMEOUT = 5.0
_MAX_POLLER_CRASHES = 5


@dataclass(frozen=True)
class TaskCandidate:
    """A file discovered by a trigger, ready for ledger insertion."""

    source_ref: str
    dedup_key: str | None
    ordinal: int | None


def should_ignore(path: Path) -> bool:
    """Return True for paths triggers must never ingest (applied after glob matching)."""
    if path.is_dir():
        return True
    name = path.name
    return name.startswith(".") or name.startswith(".tmp-")


def compute_dedup_key(path: Path, mode: DedupMode) -> str | None:
    """Compute a dedup key for *path* under the playbook dedup mode."""
    if mode == "none":
        return None
    if mode == "filename":
        return f"name:{path.name}"
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while chunk := handle.read(_HASH_CHUNK):
            digest.update(chunk)
    return f"sha256:{digest.hexdigest()}"


def extract_ordinal(filename: str, ordinal_regex: str | None) -> int | None:
    """Extract an ordinal from *filename* using *ordinal_regex*, or None."""
    if ordinal_regex is None:
        return None
    match = re.search(ordinal_regex, filename)
    if match is None:
        return None
    group = match.group(1)
    if not group.isdigit():
        return None
    return int(group)


def ordinal_for_trigger(filename: str, trigger: Trigger, *, arrival_index: int) -> int | None:
    """Resolve task ordinal the same way production triggers and the dry-run lab do."""
    ordinal_regex = getattr(trigger, "ordinal_regex", None)
    if ordinal_regex is not None:
        return extract_ordinal(filename, ordinal_regex)
    if getattr(trigger, "arrival_order_ordinals", False):
        return arrival_index + 1
    return None


def _poll_interval(settle_seconds: float) -> float:
    half = settle_seconds / 2
    return min(0.5, half if half > 0 else 0.05)


def _resolve_ordinal(
    filename: str,
    ordinal_regex: str | None,
    *,
    log: logging.Logger,
) -> int | None:
    if ordinal_regex is None:
        return None
    ordinal = extract_ordinal(filename, ordinal_regex)
    if ordinal is None:
        log.warning("no ordinal match for filename %r", filename)
    return ordinal


def _iter_scan_paths(watch_path: Path, glob_pattern: str) -> Iterator[Path]:
    """Yield top-level files matching *glob_pattern*, sorted by name."""
    if not watch_path.is_dir():
        return
    entries = [p for p in watch_path.iterdir() if p.is_file()]
    for path in sorted(entries, key=lambda p: p.name):
        if fnmatch.fnmatch(path.name, glob_pattern):
            yield path


def _build_candidate(
    path: Path,
    dedup_mode: DedupMode,
    ordinal_regex: str | None,
    *,
    log: logging.Logger,
) -> TaskCandidate | None:
    if should_ignore(path):
        return None
    if not path.is_file():
        return None
    try:
        dedup_key = compute_dedup_key(path, dedup_mode)
    except OSError as exc:
        log.warning("skipping unreadable file %s: %s", path, exc)
        return None
    ordinal = _resolve_ordinal(path.name, ordinal_regex, log=log)
    return TaskCandidate(
        source_ref=str(path.resolve()),
        dedup_key=dedup_key,
        ordinal=ordinal,
    )


def _emit_candidate(candidate: TaskCandidate, sink: Sink) -> int | None:
    task_id = sink(candidate)
    if task_id is None:
        logger.debug(
            "duplicate task dropped for %s (dedup_key=%r)",
            candidate.source_ref,
            candidate.dedup_key,
        )
    return task_id


def manifest_row_dedup_key(row: ManifestRow) -> str:
    """Stable dedup key for one manifest row (ordinal + name + prompt)."""
    payload = f"{row.name}\n{row.prompt or ''}"
    digest = hashlib.sha256(payload.encode()).hexdigest()[:32]
    return f"mrow:{row.ordinal}:{digest}"


def manifest_sink(ledger: Ledger, pipeline_id: int, manifest_path: Path) -> Sink:
    """Sink that reserves ordinal→name bindings immediately after task creation."""
    inner = ledger_sink(ledger, pipeline_id)
    resolved = manifest_path.expanduser().resolve()

    def sink(candidate: TaskCandidate) -> int | None:
        task_id = inner(candidate)
        if task_id is None or candidate.ordinal is None:
            return task_id
        try:
            rows = load_manifest(resolved)
            row = rows[candidate.ordinal - 1]
            ledger.reserve_name(pipeline_id, candidate.ordinal, row.name, task_id)
        except (ManifestError, IndexError) as exc:
            logger.warning(
                "manifest reservation skipped for task %s ordinal %s: %s",
                task_id,
                candidate.ordinal,
                exc,
            )
        return task_id

    return sink


def ledger_sink(ledger: Ledger, pipeline_id: int, *, arrival_order: bool = False) -> Sink:
    """Return a sink that inserts candidates via the ledger."""

    def sink(candidate: TaskCandidate) -> int | None:
        if arrival_order and candidate.ordinal is None:
            return ledger.create_task_arrival(
                pipeline_id,
                candidate.source_ref,
                candidate.dedup_key,
            )
        return ledger.create_task(
            pipeline_id,
            candidate.source_ref,
            candidate.dedup_key,
            candidate.ordinal,
        )

    return sink


def _file_readable(path: Path) -> bool:
    try:
        with path.open("rb"):
            return True
    except OSError:
        return False


def _file_ready_for_emit(path: Path, *, settle_seconds: float) -> bool:
    """Return True when *path* is openable and size-stable for *settle_seconds*."""
    if should_ignore(path) or not path.is_file():
        return False
    wait = settle_seconds if settle_seconds > 0 else 0.05
    try:
        size = path.stat().st_size
    except OSError:
        return False
    time.sleep(wait)
    try:
        if path.stat().st_size != size:
            return False
    except OSError:
        return False
    return _file_readable(path)


def scan_directory(
    watch_path: Path,
    glob_pattern: str,
    dedup_mode: DedupMode,
    ordinal_regex: str | None,
    sink: Sink,
    *,
    log: logging.Logger | None = None,
    settle_seconds: float = 0.0,
) -> int:
    """Scan *watch_path* and emit settled candidates; return emit count."""
    emit_log = log or logger
    watch_path = watch_path.expanduser()
    emitted = 0
    for path in _iter_scan_paths(watch_path, glob_pattern):
        if not _file_ready_for_emit(path, settle_seconds=settle_seconds):
            continue
        candidate = _build_candidate(path, dedup_mode, ordinal_regex, log=emit_log)
        if candidate is None:
            continue
        _emit_candidate(candidate, sink)
        emitted += 1
    return emitted


class ManualScanService:
    """One-shot directory scanner."""

    def __init__(self, spec: ManualTrigger, dedup: DedupMode, sink: Sink) -> None:
        self._spec = spec
        self._dedup = dedup
        self._sink = sink
        self._log = logging.getLogger(f"{__name__}.manual.{spec.path}")

    def run(self) -> int:
        """Scan once and emit candidates; return the number emitted."""
        return scan_directory(
            Path(self._spec.path),
            self._spec.glob,
            self._dedup,
            self._spec.ordinal_regex,
            self._sink,
            log=self._log,
        )


def _as_path(value: str | bytes | None) -> Path | None:
    if value is None:
        return None
    return Path(value.decode() if isinstance(value, bytes) else value)


class _WatchHandler(FileSystemEventHandler):
    def __init__(self, service: FolderWatchService) -> None:
        self._service = service

    def on_created(self, event: FileSystemEvent) -> None:
        path = _as_path(event.src_path)
        if path is not None:
            self._service._note_path(path)

    def on_modified(self, event: FileSystemEvent) -> None:
        path = _as_path(event.src_path)
        if path is not None:
            self._service._note_path(path)

    def on_moved(self, event: FileSystemEvent) -> None:
        src = _as_path(event.src_path)
        if src is not None:
            self._service._forget_path(src)
        dest = _as_path(event.dest_path)
        if dest is not None:
            self._service._note_path(dest)


class FolderWatchService:
    """Watch a folder, settle files, and emit TaskCandidates."""

    def __init__(
        self,
        spec: FolderWatchTrigger,
        dedup: DedupMode,
        sink: Sink,
        *,
        poll_interval: float | None = None,
        enable_poller: bool = True,
        startup_hook_after_observer: Callable[[], None] | None = None,
    ) -> None:
        self._spec = spec
        self._dedup = dedup
        self._sink = sink
        self._watch_path = Path(spec.path).expanduser()
        self._poll_interval = (
            poll_interval if poll_interval is not None else _poll_interval(spec.settle_seconds)
        )
        self._enable_poller = enable_poller
        self._startup_hook_after_observer = startup_hook_after_observer
        self._log = logging.getLogger(f"{__name__}.watch.{spec.path}")
        self._lock = threading.Lock()
        self._settle: dict[Path, tuple[int, float]] = {}
        self._stop = threading.Event()
        self._poller_thread: threading.Thread | None = None
        self._observer: BaseObserver | None = None
        self._started = False
        self._poller_crashes = 0

    def _matches_glob(self, path: Path) -> bool:
        return path.parent == self._watch_path and fnmatch.fnmatch(path.name, self._spec.glob)

    def _note_path(self, path: Path) -> None:
        try:
            if not self._matches_glob(path) or should_ignore(path):
                return
            if not path.is_file():
                return
            size = path.stat().st_size
        except OSError:
            return
        with self._lock:
            self._settle[path.resolve()] = (size, time.monotonic())

    def _forget_path(self, path: Path) -> None:
        with self._lock:
            self._settle.pop(path.resolve(), None)

    def _emit_settled(self, path: Path) -> None:
        candidate = _build_candidate(
            path,
            self._dedup,
            self._spec.ordinal_regex,
            log=self._log,
        )
        if candidate is not None:
            _emit_candidate(candidate, self._sink)

    def _poller_loop(self) -> None:
        while not self._stop.is_set():
            try:
                self._poll_once()
                self._poller_crashes = 0
            except Exception:
                self._poller_crashes += 1
                self._log.exception("settle poller error (crash %s)", self._poller_crashes)
                if self._poller_crashes >= _MAX_POLLER_CRASHES:
                    self._log.critical(
                        "settle poller crashed %s times; stopping folder watch",
                        _MAX_POLLER_CRASHES,
                    )
                    self.stop()
                    return
            self._stop.wait(self._poll_interval)

    def _poll_once(self) -> None:
        now = time.monotonic()
        ready: list[Path] = []
        with self._lock:
            for path, (last_size, last_change) in list(self._settle.items()):
                try:
                    if not path.exists():
                        self._settle.pop(path, None)
                        continue
                    size = path.stat().st_size
                except OSError:
                    self._settle.pop(path, None)
                    continue
                if size != last_size:
                    self._settle[path] = (size, now)
                    continue
                if now - last_change < self._spec.settle_seconds:
                    continue
                if not _file_readable(path):
                    continue
                ready.append(path)
                self._settle.pop(path, None)
        for path in ready:
            self._emit_settled(path)

    def rescan(self) -> int:
        """Seed matching on-disk files into the settle tracker; return paths seeded."""
        seeded = 0
        for path in _iter_scan_paths(self._watch_path, self._spec.glob):
            if should_ignore(path):
                continue
            self._note_path(path)
            seeded += 1
        return seeded

    def start(self) -> None:
        """Attach the observer, seed on-disk files into the settle tracker, then start the poller."""
        if self._started:
            return
        self._started = True
        self._stop.clear()
        handler = _WatchHandler(self)
        self._observer = Observer()
        self._observer.schedule(handler, str(self._watch_path), recursive=False)
        self._observer.start()
        if self._startup_hook_after_observer is not None:
            self._startup_hook_after_observer()
        self.rescan()
        if self._enable_poller:
            self._poller_thread = threading.Thread(
                target=self._poller_loop,
                name="folder-watch-settle",
                daemon=True,
            )
            self._poller_thread.start()

    def stop(self) -> None:
        """Stop observer and poller; idempotent."""
        if not self._started:
            return
        self._stop.set()
        if self._observer is not None:
            self._observer.stop()
            self._observer.join(timeout=_STOP_JOIN_TIMEOUT)
            self._observer = None
        if self._poller_thread is not None:
            self._poller_thread.join(timeout=_STOP_JOIN_TIMEOUT)
            self._poller_thread = None
        with self._lock:
            self._settle.clear()
        self._started = False

    def drain(self, timeout: float) -> None:
        """Block until the settle queue is empty or *timeout* elapses."""
        deadline = time.monotonic() + timeout
        empty_since: float | None = None
        quiet_for = self._spec.settle_seconds + self._poll_interval
        while time.monotonic() < deadline:
            self._poll_once()
            with self._lock:
                empty = not self._settle
            if empty:
                if empty_since is None:
                    empty_since = time.monotonic()
                elif time.monotonic() - empty_since >= quiet_for:
                    return
            else:
                empty_since = None
            time.sleep(self._poll_interval)

        self._started = False


class ManifestTriggerService:
    """Poll a job manifest and emit one task per row."""

    def __init__(
        self,
        spec: ManifestTrigger,
        dedup: DedupMode,
        sink: Sink,
        *,
        ledger: Ledger,
        pipeline_id: int,
    ) -> None:
        self._spec = spec
        self._dedup = dedup
        self._sink = sink
        self._ledger = ledger
        self._pipeline_id = pipeline_id
        self._manifest_path = Path(spec.path).expanduser().resolve()
        self._log = logging.getLogger(f"{__name__}.manifest.{spec.path}")
        self._stop = threading.Event()
        self._poller_thread: threading.Thread | None = None
        self._started = False
        self._last_good_mtime: float | None = None
        self._flagged_error_mtime: float | None = None

    def _manifest_mtime(self) -> float:
        try:
            return self._manifest_path.stat().st_mtime
        except OSError:
            return -1.0

    def _flag_unreadable(self, message: str) -> None:
        mtime = self._manifest_mtime()
        if self._flagged_error_mtime == mtime:
            return
        self._log.error("manifest unreadable: %s", message)
        self._ledger.raise_flag(
            self._pipeline_id,
            task_id=None,
            level=1,
            kind="manifest_unreadable",
            message=message,
        )
        self._flagged_error_mtime = mtime

    def _scan(self) -> int:
        """Read the manifest and emit candidates; return emit count."""
        try:
            rows = load_manifest(self._manifest_path)
        except ManifestError as exc:
            self._flag_unreadable(str(exc))
            return 0
        self._flagged_error_mtime = None
        self._last_good_mtime = self._manifest_mtime()
        abs_path = str(self._manifest_path)
        emitted = 0
        for row in rows:
            candidate = TaskCandidate(
                source_ref=f"manifest:{abs_path}#row{row.ordinal}",
                dedup_key=manifest_row_dedup_key(row),
                ordinal=row.ordinal,
            )
            if _emit_candidate(candidate, self._sink) is not None:
                emitted += 1
        return emitted

    def _poller_loop(self) -> None:
        while not self._stop.is_set():
            try:
                mtime = self._manifest_mtime()
                if self._last_good_mtime is None or mtime != self._last_good_mtime:
                    self._scan()
            except Exception:
                self._log.exception("manifest poller error")
            self._stop.wait(self._spec.poll_seconds)

    def run(self) -> int:
        """One-shot scan without starting the poller thread."""
        return self._scan()

    def start(self) -> None:
        """Initial scan; start poller when poll_seconds > 0."""
        if self._started:
            return
        self._started = True
        self._stop.clear()
        self._scan()
        if self._spec.poll_seconds > 0:
            self._poller_thread = threading.Thread(
                target=self._poller_loop,
                name="manifest-trigger-poller",
                daemon=True,
            )
            self._poller_thread.start()

    def stop(self) -> None:
        """Stop poller; idempotent."""
        if not self._started:
            return
        self._stop.set()
        if self._poller_thread is not None:
            self._poller_thread.join(timeout=_STOP_JOIN_TIMEOUT)
            self._poller_thread = None
        self._started = False


def build_trigger_service(
    spec: Trigger,
    dedup: DedupMode,
    sink: Sink,
    *,
    ledger: Ledger | None = None,
    pipeline_id: int | None = None,
    poll_interval: float | None = None,
) -> ManualScanService | FolderWatchService | ManifestTriggerService:
    """Construct a trigger service for *spec*."""
    if isinstance(spec, ManualTrigger):
        return ManualScanService(spec, dedup, sink)
    if isinstance(spec, FolderWatchTrigger):
        return FolderWatchService(spec, dedup, sink, poll_interval=poll_interval)
    if isinstance(spec, ManifestTrigger):
        if ledger is None or pipeline_id is None:
            raise TriggerError("manifest trigger requires ledger and pipeline_id")
        manifest_path = Path(spec.path).expanduser()
        manifest_path_sink = manifest_sink(ledger, pipeline_id, manifest_path)
        return ManifestTriggerService(
            spec,
            dedup,
            manifest_path_sink,
            ledger=ledger,
            pipeline_id=pipeline_id,
        )
    raise TriggerError(f"unsupported trigger type: {type(spec)!r}")
