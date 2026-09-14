"""Single-writer instance lock for the ledger database (S13).

Owns an exclusive flock on a sibling ``.lock`` file next to the SQLite DB so
only one ``ordine serve`` / ``ordine run`` writer may own a given database.
Read-only status commands must not acquire this lock. Must never import cli,
web, executors, or llm.
"""

from __future__ import annotations

import atexit
import os
from pathlib import Path
from types import TracebackType

from ordine.core.errors import InstanceLockError

try:
    import fcntl
except ImportError:  # pragma: no cover - non-POSIX
    fcntl = None  # type: ignore[assignment]


def lock_path_for(db_path: Path) -> Path:
    """Return the advisory lock file path for *db_path*."""
    expanded = db_path.expanduser().resolve()
    return expanded.with_suffix(expanded.suffix + ".lock")


class InstanceLock:
    """Exclusive, non-blocking flock held for the lifetime of a writer process."""

    def __init__(self, db_path: Path) -> None:
        self.db_path = db_path.expanduser().resolve()
        self.path = lock_path_for(self.db_path)
        self._fd: int | None = None
        self._held = False

    @property
    def held(self) -> bool:
        """True when this process currently holds the lock."""
        return self._held

    def acquire(self) -> None:
        """Acquire the exclusive lock or raise ``InstanceLockError``.

        Raises:
            InstanceLockError: When another writer already holds the lock, or
                flock is unavailable on this platform.
        """
        if self._held:
            return
        if fcntl is None:
            raise InstanceLockError(
                "single-instance lock requires POSIX fcntl; cannot guard this database"
            )
        self.path.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(str(self.path), os.O_RDWR | os.O_CREAT, 0o644)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            # Best-effort read of the holder pid for a clearer message.
            holder = ""
            try:
                raw = os.read(fd, 64).decode("utf-8", errors="replace").strip()
                if raw:
                    holder = f" (held by pid {raw})"
            except OSError:
                holder = ""
            os.close(fd)
            raise InstanceLockError(
                f"another ordine process already holds the write lock for "
                f"{self.db_path}{holder}; only one writer per database is allowed"
            ) from exc
        try:
            os.ftruncate(fd, 0)
            os.lseek(fd, 0, os.SEEK_SET)
            os.write(fd, f"{os.getpid()}\n".encode())
        except OSError:
            # Lock is held even if pid write fails; continue.
            pass
        self._fd = fd
        self._held = True
        atexit.register(self.release)

    def release(self) -> None:
        """Release the lock if held (idempotent)."""
        if not self._held or self._fd is None:
            return
        fd = self._fd
        self._fd = None
        self._held = False
        try:
            if fcntl is not None:
                fcntl.flock(fd, fcntl.LOCK_UN)
        finally:
            os.close(fd)

    def __enter__(self) -> InstanceLock:
        self.acquire()
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        self.release()
