"""S13: single-writer advisory lock."""

from __future__ import annotations

from pathlib import Path

import pytest
from typer.testing import CliRunner

from ordine.cli.main import app
from ordine.core.errors import InstanceLockError
from ordine.core.instance_lock import InstanceLock


def test_second_lock_raises(tmp_path: Path) -> None:
    db = tmp_path / "ordine.sqlite3"
    db.touch()
    first = InstanceLock(db)
    first.acquire()
    try:
        second = InstanceLock(db)
        with pytest.raises(InstanceLockError, match="already holds the write lock"):
            second.acquire()
    finally:
        first.release()


def test_status_does_not_need_write_lock(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    config = tmp_path / "config.toml"
    db = tmp_path / "ordine.sqlite3"
    work = tmp_path / "work"
    config.write_text(
        f"""[paths]
db = "{db}"
workdir_root = "{work}"
""",
        encoding="utf-8",
    )
    monkeypatch.chdir(tmp_path)
    # Hold the writer lock; status must still succeed (read-only path).
    lock = InstanceLock(db)
    lock.acquire()
    try:
        runner = CliRunner()
        result = runner.invoke(app, ["--config", str(config), "status", "--json"])
        assert result.exit_code == 0, result.output
        assert '"pipelines"' in result.output
        assert '"workers"' in result.output
    finally:
        lock.release()


def test_lock_context_manager_and_release(tmp_path: Path) -> None:
    db = tmp_path / "ordine.sqlite3"
    db.touch()
    with InstanceLock(db) as lock:
        assert lock.held
        assert lock.path.exists()
    assert not lock.held
    # Re-acquire after release works.
    again = InstanceLock(db)
    again.acquire()
    again.release()
    again.release()  # idempotent


def test_lock_path_for_suffix(tmp_path: Path) -> None:
    from ordine.core.instance_lock import lock_path_for

    db = tmp_path / "ordine.sqlite3"
    assert lock_path_for(db).name == "ordine.sqlite3.lock"
