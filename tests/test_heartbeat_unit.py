"""Unit tests for ordine.core.heartbeat (A6 sidecar files).

These pin the branches the CLI status test does not reach: naive timestamps,
write failures that must never raise, corrupt files, clear-after-stop, and the
best-effort pid probe. They keep the module above the core coverage floor on
every platform, independent of what ``os.kill(pid, 0)`` does for pid 1.
"""

from __future__ import annotations

import json
import os
from datetime import UTC, datetime
from pathlib import Path

import pytest

from ordine.core import heartbeat
from ordine.core.heartbeat import (
    clear_heartbeat,
    heartbeat_dir_for,
    pid_is_alive,
    read_heartbeats,
    write_heartbeat,
)


def test_heartbeat_dir_sits_next_to_db(tmp_path: Path) -> None:
    db = tmp_path / "ordine.sqlite3"
    assert heartbeat_dir_for(db) == tmp_path.resolve() / "ordine.sqlite3.heartbeats"


def test_write_then_read_roundtrip(tmp_path: Path) -> None:
    db = tmp_path / "ordine.sqlite3"
    stamp = datetime(2026, 1, 2, 3, 4, 5, tzinfo=UTC)
    write_heartbeat(db, pipeline_id=3, pipeline_name="demo", alive=True, last_activity=stamp)
    [hb] = read_heartbeats(db)
    assert hb.pipeline_id == 3
    assert hb.pipeline_name == "demo"
    assert hb.pid == os.getpid()
    assert hb.alive is True
    assert hb.stop_failed is False
    assert hb.last_activity == stamp
    # The temp file used for the atomic replace must not linger.
    assert list(heartbeat_dir_for(db).glob("*.tmp")) == []


def test_write_defaults_to_now_and_normalises_naive_timestamp(tmp_path: Path) -> None:
    db = tmp_path / "ordine.sqlite3"
    before = datetime.now(tz=UTC)
    write_heartbeat(db, pipeline_id=1, pipeline_name="a", alive=True)
    [hb] = read_heartbeats(db)
    assert hb.last_activity.tzinfo is not None
    assert hb.last_activity >= before

    naive = datetime(2026, 5, 6, 7, 8, 9)
    write_heartbeat(db, pipeline_id=2, pipeline_name="b", alive=False, last_activity=naive)
    by_id = {hb.pipeline_id: hb for hb in read_heartbeats(db)}
    assert by_id[2].last_activity == naive.replace(tzinfo=UTC)
    assert by_id[2].alive is False


def test_write_swallows_os_errors(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    db = tmp_path / "ordine.sqlite3"

    def boom(self: Path, *args: object, **kwargs: object) -> int:
        raise OSError("disk full")

    monkeypatch.setattr(Path, "write_text", boom)
    # Observability must never break the worker: no exception, nothing left behind.
    write_heartbeat(db, pipeline_id=9, pipeline_name="x", alive=True)
    assert read_heartbeats(db) == []
    assert list(heartbeat_dir_for(db).glob("*")) == []


def test_read_skips_corrupt_and_incomplete_files(tmp_path: Path) -> None:
    db = tmp_path / "ordine.sqlite3"
    directory = heartbeat_dir_for(db)
    directory.mkdir(parents=True)
    (directory / "1.json").write_text("{not json", encoding="utf-8")
    (directory / "2.json").write_text(json.dumps({"pipeline_id": 2}), encoding="utf-8")
    (directory / "3.json").write_text(
        json.dumps({"pipeline_id": 3, "last_activity": "yesterday"}), encoding="utf-8"
    )
    (directory / "4.json").write_text(
        json.dumps({"pipeline_id": 4, "last_activity": "2026-01-01T00:00:00"}), encoding="utf-8"
    )
    [hb] = read_heartbeats(db)
    assert hb.pipeline_id == 4
    assert hb.pipeline_name == ""
    assert hb.pid == 0
    assert hb.last_activity == datetime(2026, 1, 1, tzinfo=UTC)


def test_read_without_directory_is_empty(tmp_path: Path) -> None:
    assert read_heartbeats(tmp_path / "missing.sqlite3") == []


def test_clear_removes_file_and_tolerates_absence(tmp_path: Path) -> None:
    db = tmp_path / "ordine.sqlite3"
    write_heartbeat(db, pipeline_id=5, pipeline_name="demo", alive=True)
    assert len(read_heartbeats(db)) == 1
    clear_heartbeat(db, 5)
    assert read_heartbeats(db) == []
    clear_heartbeat(db, 5)  # second call is a no-op, never raises
    clear_heartbeat(tmp_path / "missing.sqlite3", 1)


def test_pid_is_alive_branches(monkeypatch: pytest.MonkeyPatch) -> None:
    assert pid_is_alive(0) is False
    assert pid_is_alive(-1) is False
    assert pid_is_alive(os.getpid()) is True

    def raise_(exc: BaseException):  # tiny local factory
        def kill(pid: int, sig: int) -> None:
            raise exc

        return kill

    monkeypatch.setattr(heartbeat.os, "kill", raise_(ProcessLookupError()))
    assert pid_is_alive(12345) is False
    monkeypatch.setattr(heartbeat.os, "kill", raise_(PermissionError()))
    assert pid_is_alive(12345) is True
    monkeypatch.setattr(heartbeat.os, "kill", raise_(OSError("odd")))
    assert pid_is_alive(12345) is False
