"""Tests for trigger helpers, services, and the 100-file restart integration."""

from __future__ import annotations

import random
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

import pytest

from conveyor.core.db import create_engine_for, init_db
from conveyor.core.errors import TriggerError
from conveyor.core.ledger import Ledger
from conveyor.core.playbook import FolderWatchTrigger, ManifestTrigger, ManualTrigger
from conveyor.core.triggers import (
    FolderWatchService,
    ManualScanService,
    TaskCandidate,
    build_trigger_service,
    compute_dedup_key,
    extract_ordinal,
    ledger_sink,
)

FIXTURE_YAML = Path(__file__).parent / "fixtures" / "playbooks" / "valid" / "v01_minimal.yml"


@pytest.fixture
def engine(tmp_path: Path):
    eng = create_engine_for(tmp_path / "ledger.db")
    init_db(eng)
    return eng


@pytest.fixture
def ledger(engine) -> Ledger:
    return Ledger(engine)


def _register_pipeline(ledger: Ledger) -> int:
    from conveyor.core.playbook import load_playbook

    playbook = load_playbook(FIXTURE_YAML)
    pipeline_id, _ = ledger.register_pipeline(playbook, FIXTURE_YAML.read_text(encoding="utf-8"))
    return pipeline_id


def test_compute_dedup_key_sha256(tmp_path: Path) -> None:
    import hashlib

    path = tmp_path / "data.bin"
    data = b"hello conveyor"
    path.write_bytes(data)
    assert compute_dedup_key(path, "content_hash") == f"sha256:{hashlib.sha256(data).hexdigest()}"


def test_compute_dedup_key_known_hash(tmp_path: Path) -> None:
    import hashlib

    path = tmp_path / "known.bin"
    data = b"conveyor-step-6"
    path.write_bytes(data)
    expected = f"sha256:{hashlib.sha256(data).hexdigest()}"
    assert compute_dedup_key(path, "content_hash") == expected


def test_compute_dedup_key_filename_and_none(tmp_path: Path) -> None:
    path = tmp_path / "photo.png"
    path.write_bytes(b"x")
    assert compute_dedup_key(path, "filename") == "name:photo.png"
    assert compute_dedup_key(path, "none") is None


def test_compute_dedup_key_large_file_streaming(tmp_path: Path) -> None:
    path = tmp_path / "large.bin"
    with path.open("wb") as handle:
        handle.seek(10 * 1024 * 1024 - 1)
        handle.write(b"\0")
    key = compute_dedup_key(path, "content_hash")
    assert key is not None
    assert key.startswith("sha256:")


def test_extract_ordinal_match() -> None:
    assert extract_ordinal("img_0007.png", r"img_(\d+)\.png") == 7


def test_extract_ordinal_no_match(caplog: pytest.LogCaptureFixture) -> None:
    assert extract_ordinal("photo.png", r"img_(\d+)\.png") is None
    assert extract_ordinal("img_abc.png", r"img_(\d+)\.png") is None


def test_ignore_rules(tmp_path: Path, ledger: Ledger) -> None:
    watch = tmp_path / "watch"
    watch.mkdir()
    (watch / "visible.png").write_bytes(b"ok")
    (watch / ".hidden.png").write_bytes(b"no")
    (watch / ".tmp-abc123").write_bytes(b"no")
    sub = watch / "subdir"
    sub.mkdir()
    (sub / "nested.png").write_bytes(b"no")

    pipeline_id = _register_pipeline(ledger)
    emitted: list[TaskCandidate] = []

    def capture(candidate: TaskCandidate) -> int | None:
        emitted.append(candidate)
        return ledger.create_task(
            pipeline_id,
            candidate.source_ref,
            candidate.dedup_key,
            candidate.ordinal,
        )

    spec = ManualTrigger(type="manual", path=str(watch), glob="*")
    ManualScanService(spec, "filename", capture).run()
    names = {Path(c.source_ref).name for c in emitted}
    assert names == {"visible.png"}


def test_settle_emits_once_after_chunks(tmp_path: Path, ledger: Ledger) -> None:
    watch = tmp_path / "watch"
    watch.mkdir()
    pipeline_id = _register_pipeline(ledger)
    emitted: list[str] = []

    def capture(candidate: TaskCandidate) -> int | None:
        emitted.append(candidate.source_ref)
        return ledger.create_task(
            pipeline_id,
            candidate.source_ref,
            candidate.dedup_key,
            candidate.ordinal,
        )

    spec = FolderWatchTrigger(
        type="folder_watch",
        path=str(watch),
        glob="*",
        settle_seconds=0.3,
    )
    service = FolderWatchService(spec, "filename", capture, poll_interval=0.05, enable_poller=False)
    service.start()

    target = watch / "chunked.bin"
    try:
        with target.open("wb") as handle:
            for chunk in (b"a", b"b", b"c"):
                handle.write(chunk)
                handle.flush()
                time.sleep(0.1)
        service.drain(timeout=2.0)
    finally:
        service.stop()

    assert len(emitted) == 1
    assert emitted[0] == str(target.resolve())


def test_settle_deleted_mid_settle_never_emitted(tmp_path: Path, ledger: Ledger) -> None:
    watch = tmp_path / "watch"
    watch.mkdir()
    pipeline_id = _register_pipeline(ledger)
    emitted: list[str] = []

    def capture(candidate: TaskCandidate) -> int | None:
        emitted.append(candidate.source_ref)
        return ledger.create_task(
            pipeline_id,
            candidate.source_ref,
            candidate.dedup_key,
            candidate.ordinal,
        )

    spec = FolderWatchTrigger(
        type="folder_watch",
        path=str(watch),
        glob="*",
        settle_seconds=0.3,
    )
    service = FolderWatchService(spec, "filename", capture, poll_interval=0.05, enable_poller=False)
    service.start()

    target = watch / "vanish.bin"
    try:
        target.write_bytes(b"draft")
        time.sleep(0.05)
        target.unlink()
        service.drain(timeout=1.0)
    finally:
        service.stop()

    assert emitted == []


def test_reemit_on_modification_content_hash(tmp_path: Path, ledger: Ledger) -> None:
    watch = tmp_path / "watch"
    watch.mkdir()
    pipeline_id = _register_pipeline(ledger)
    keys: list[str | None] = []

    def capture(candidate: TaskCandidate) -> int | None:
        keys.append(candidate.dedup_key)
        return ledger.create_task(
            pipeline_id,
            candidate.source_ref,
            candidate.dedup_key,
            candidate.ordinal,
        )

    spec = FolderWatchTrigger(
        type="folder_watch",
        path=str(watch),
        glob="*",
        settle_seconds=0.2,
    )
    service = FolderWatchService(
        spec, "content_hash", capture, poll_interval=0.05, enable_poller=False
    )
    service.start()

    target = watch / "mutable.bin"
    try:
        target.write_bytes(b"v1")
        service._note_path(target)
        service.drain(timeout=2.0)
        target.write_bytes(b"v2")
        service._note_path(target)
        service.drain(timeout=2.0)
    finally:
        service.stop()

    assert len(keys) == 2
    assert keys[0] != keys[1]


def test_manual_scan_emits_and_dedup(tmp_path: Path, ledger: Ledger) -> None:
    watch = tmp_path / "watch"
    watch.mkdir()
    for i in range(5):
        (watch / f"file{i}.png").write_bytes(f"data{i}".encode())
    (watch / ".hidden.png").write_bytes(b"x")
    (watch / ".tmp-part").write_bytes(b"x")

    pipeline_id = _register_pipeline(ledger)
    sink = ledger_sink(ledger, pipeline_id)
    spec = ManualTrigger(type="manual", path=str(watch), glob="*.png")
    service = ManualScanService(spec, "filename", sink)
    assert service.run() == 5
    assert service.run() == 5
    assert ledger.counts(pipeline_id)["pending"] == 5


def test_arrival_order_sink_assigns_ordinals(ledger: Ledger) -> None:
    pipeline_id = _register_pipeline(ledger)
    sink = ledger_sink(ledger, pipeline_id, arrival_order=True)
    for i in range(3):
        sink(TaskCandidate(source_ref=f"/f{i}.png", dedup_key=f"k{i}", ordinal=None))
    tasks = ledger.list_tasks(pipeline_id, status="pending")
    ordinals = sorted(t.ordinal for t in tasks)
    assert ordinals == [1, 2, 3]


def test_arrival_order_continues_after_restart(engine, ledger: Ledger) -> None:
    pipeline_id = _register_pipeline(ledger)
    sink = ledger_sink(ledger, pipeline_id, arrival_order=True)
    for i in range(3):
        sink(TaskCandidate(source_ref=f"/a{i}.png", dedup_key=f"a{i}", ordinal=None))
    ledger2 = Ledger(engine)
    sink2 = ledger_sink(ledger2, pipeline_id, arrival_order=True)
    sink2(TaskCandidate(source_ref="/a3.png", dedup_key="a3", ordinal=None))
    tasks = ledger2.list_tasks(pipeline_id, status="pending", limit=10)
    ordinals = sorted(t.ordinal for t in tasks)
    assert ordinals == [1, 2, 3, 4]


def test_create_task_arrival_concurrent_race(engine, ledger: Ledger) -> None:
    pipeline_id = _register_pipeline(ledger)
    results: list[int | None] = []

    def worker(i: int) -> int | None:
        return ledger.create_task_arrival(pipeline_id, f"/race{i}.png", f"race{i}")

    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [pool.submit(worker, i) for i in range(2)]
        for future in as_completed(futures):
            results.append(future.result())

    assert all(r is not None for r in results)
    tasks = ledger.list_tasks(pipeline_id, status="pending", limit=10)
    ordinals = sorted(t.ordinal for t in tasks)
    assert ordinals == [1, 2]


def test_build_trigger_service_manifest_raises() -> None:
    spec = ManifestTrigger(type="manifest", path="/tmp/m")
    with pytest.raises(TriggerError, match="step 14"):
        build_trigger_service(spec, "none", lambda _c: None)


def test_rescan_mid_write_emits_complete_file_once(tmp_path: Path, ledger: Ledger) -> None:
    import hashlib

    watch = tmp_path / "watch"
    watch.mkdir()
    pipeline_id = _register_pipeline(ledger)
    keys: list[str | None] = []

    def capture(candidate: TaskCandidate) -> int | None:
        keys.append(candidate.dedup_key)
        return ledger.create_task(
            pipeline_id,
            candidate.source_ref,
            candidate.dedup_key,
            candidate.ordinal,
        )

    spec = FolderWatchTrigger(
        type="folder_watch",
        path=str(watch),
        glob="*",
        settle_seconds=0.3,
    )
    service1 = FolderWatchService(
        spec,
        "content_hash",
        capture,
        poll_interval=0.05,
    )
    service1.start()

    target = watch / "midwrite.bin"
    complete = b"part1-part2-part3"

    def writer() -> None:
        with target.open("wb") as handle:
            for chunk in (b"part1-", b"part2-", b"part3"):
                handle.write(chunk)
                handle.flush()
                time.sleep(0.15)

    writer_thread = threading.Thread(target=writer, name="writer", daemon=True)
    writer_thread.start()
    time.sleep(0.1)

    service1.stop()
    service2 = FolderWatchService(
        spec,
        "content_hash",
        capture,
        poll_interval=0.05,
    )
    service2.start()

    writer_thread.join(timeout=5.0)
    assert not writer_thread.is_alive()
    service2.drain(timeout=5.0)
    service2.stop()

    expected = f"sha256:{hashlib.sha256(complete).hexdigest()}"
    assert keys == [expected]
    assert ledger.counts(pipeline_id)["pending"] == 1


@pytest.mark.integration
def test_folder_watch_100_files_with_restart(tmp_path: Path) -> None:
    watch = tmp_path / "watch"
    watch.mkdir()
    db_path = tmp_path / "ledger.db"
    engine = create_engine_for(db_path)
    init_db(engine)
    ledger = Ledger(engine)
    pipeline_id = _register_pipeline(ledger)

    spec = FolderWatchTrigger(
        type="folder_watch",
        path=str(watch),
        glob="*",
        settle_seconds=0.2,
        ordinal_regex=r"img_(\d+)\.png",
    )
    sink = ledger_sink(ledger, pipeline_id)
    service = FolderWatchService(spec, "content_hash", sink, poll_interval=0.05)
    service.start()

    written = threading.Event()
    stop_writer = threading.Event()
    order = list(range(1, 101))
    random.shuffle(order)

    def writer() -> None:
        for n in order:
            if stop_writer.is_set():
                return
            name = f"img_{n:04d}.png"
            target = watch / name
            payload = f"payload-{n}".encode()
            chunks = random.randint(1, 3)
            part_size = max(1, len(payload) // chunks)
            with target.open("wb") as handle:
                for i in range(chunks):
                    start = i * part_size
                    end = len(payload) if i == chunks - 1 else (i + 1) * part_size
                    handle.write(payload[start:end])
                    handle.flush()
                    time.sleep(0.02)
            if n == 5:
                (watch / ".hidden.png").write_bytes(b"decoy")
                (watch / ".tmp-decoy").write_bytes(b"decoy")
            if n == 40:
                written.set()
        stop_writer.set()

    writer_thread = threading.Thread(target=writer, name="writer", daemon=True)
    writer_thread.start()
    assert written.wait(timeout=15.0)

    service.stop()
    ledger2 = Ledger(engine)
    sink2 = ledger_sink(ledger2, pipeline_id)
    service2 = FolderWatchService(spec, "content_hash", sink2, poll_interval=0.05)
    service2.start()

    writer_thread.join(timeout=15.0)
    assert not writer_thread.is_alive()
    service2.drain(timeout=15.0)
    service2.stop()

    tasks = ledger2.list_tasks(pipeline_id, status="pending", limit=200)
    assert len(tasks) == 100
    dedup_keys = [t.dedup_key for t in tasks]
    assert len(set(dedup_keys)) == 100
    for task in tasks:
        name = Path(task.source_ref).name
        expected = int(name.replace("img_", "").replace(".png", ""))
        assert task.ordinal == expected
    refs = {Path(t.source_ref).name for t in tasks}
    assert ".hidden.png" not in refs
    assert ".tmp-decoy" not in refs
