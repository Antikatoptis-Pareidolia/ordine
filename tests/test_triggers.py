"""Tests for trigger helpers, services, and the 100-file restart integration."""

from __future__ import annotations

import random
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

import pytest

from ordine.core.db import create_engine_for, init_db
from ordine.core.ledger import Ledger
from ordine.core.playbook import FolderWatchTrigger, ManifestTrigger, ManualTrigger
from ordine.core.triggers import (
    FolderWatchService,
    ManifestTriggerService,
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
    from ordine.core.playbook import load_playbook

    playbook = load_playbook(FIXTURE_YAML)
    pipeline_id, _ = ledger.register_pipeline(playbook, FIXTURE_YAML.read_text(encoding="utf-8"))
    return pipeline_id


def test_compute_dedup_key_sha256(tmp_path: Path) -> None:
    import hashlib

    path = tmp_path / "data.bin"
    data = b"hello ordine"
    path.write_bytes(data)
    assert compute_dedup_key(path, "content_hash") == f"sha256:{hashlib.sha256(data).hexdigest()}"


def test_compute_dedup_key_known_hash(tmp_path: Path) -> None:
    import hashlib

    path = tmp_path / "known.bin"
    data = b"ordine-step-6"
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


def test_build_trigger_service_manifest(tmp_path: Path, ledger: Ledger) -> None:
    manifest = tmp_path / "assets.csv"
    manifest.write_text("name,prompt\na.png,one\n", encoding="utf-8")
    spec = ManifestTrigger(type="manifest", path=str(manifest))
    pipeline_id = _register_pipeline(ledger)
    service = build_trigger_service(
        spec,
        "none",
        ledger=ledger,
        pipeline_id=pipeline_id,
    )
    assert isinstance(service, ManifestTriggerService)


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


def test_startup_blind_window_file_caught_after_observer_before_rescan(
    tmp_path: Path, ledger: Ledger
) -> None:
    """A file arriving after the observer attaches but before rescan must not be lost."""
    watch = tmp_path / "watch"
    watch.mkdir()
    pipeline_id = _register_pipeline(ledger)
    settle_seconds = 0.2
    spec = FolderWatchTrigger(
        type="folder_watch",
        path=str(watch),
        glob="*",
        settle_seconds=settle_seconds,
    )
    straggler = watch / "straggler.png"

    def hook() -> None:
        straggler.write_bytes(b"arrived-during-startup-gap")

    sink = ledger_sink(ledger, pipeline_id)
    service = FolderWatchService(
        spec,
        "content_hash",
        sink,
        poll_interval=settle_seconds / 4,
        startup_hook_after_observer=hook,
    )
    service.start()
    service.drain(timeout=settle_seconds * 10)
    service.stop()

    tasks = ledger.list_tasks(pipeline_id, status="pending")
    assert len(tasks) == 1
    assert Path(tasks[0].source_ref).name == "straggler.png"


@pytest.mark.integration
def test_folder_watch_100_files_with_restart(tmp_path: Path) -> None:
    seed = 20260714
    rng = random.Random(seed)
    print(f"folder-watch stress seed={seed}")
    file_count = 100
    settle_seconds = 0.2
    poll_interval = settle_seconds / 4
    chunk_sleep = settle_seconds / 10
    # Budgets scale with settle_seconds and file_count: draining N files can take ~N settle windows.
    written_wait = settle_seconds * file_count * 2
    join_timeout = settle_seconds * file_count * 2
    drain_timeout = settle_seconds * file_count * 3

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
        settle_seconds=settle_seconds,
        ordinal_regex=r"img_(\d+)\.png",
    )
    sink = ledger_sink(ledger, pipeline_id)
    service = FolderWatchService(spec, "content_hash", sink, poll_interval=poll_interval)
    service.start()

    written = threading.Event()
    stop_writer = threading.Event()
    order = list(range(1, file_count + 1))
    rng.shuffle(order)

    def writer() -> None:
        for n in order:
            if stop_writer.is_set():
                return
            name = f"img_{n:04d}.png"
            target = watch / name
            payload = f"payload-{n}".encode()
            chunks = rng.randint(1, 3)
            part_size = max(1, len(payload) // chunks)
            with target.open("wb") as handle:
                for i in range(chunks):
                    start = i * part_size
                    end = len(payload) if i == chunks - 1 else (i + 1) * part_size
                    handle.write(payload[start:end])
                    handle.flush()
                    time.sleep(chunk_sleep)
            if n == 5:
                (watch / ".hidden.png").write_bytes(b"decoy")
                (watch / ".tmp-decoy").write_bytes(b"decoy")
            if n == 40:
                written.set()
        stop_writer.set()

    writer_thread = threading.Thread(target=writer, name="writer", daemon=True)
    writer_thread.start()
    assert written.wait(timeout=written_wait)

    service.stop()
    ledger2 = Ledger(engine)
    sink2 = ledger_sink(ledger2, pipeline_id)
    service2 = FolderWatchService(spec, "content_hash", sink2, poll_interval=poll_interval)
    service2.start()

    writer_thread.join(timeout=join_timeout)
    assert not writer_thread.is_alive()
    service2.drain(timeout=drain_timeout)
    service2.stop()

    tasks = ledger2.list_tasks(pipeline_id, status="pending", limit=file_count + 10)
    expected_names = {f"img_{n:04d}.png" for n in range(1, file_count + 1)}
    actual_names = {Path(task.source_ref).name for task in tasks}
    missing = sorted(expected_names - actual_names)
    extra = sorted(actual_names - expected_names)
    assert len(tasks) == file_count, (
        f"expected {file_count} tasks, got {len(tasks)}; missing={missing!r} extra={extra!r}"
    )
    dedup_keys = [t.dedup_key for t in tasks]
    assert len(set(dedup_keys)) == file_count
    for task in tasks:
        name = Path(task.source_ref).name
        expected = int(name.replace("img_", "").replace(".png", ""))
        assert task.ordinal == expected
    refs = {Path(t.source_ref).name for t in tasks}
    assert ".hidden.png" not in refs
    assert ".tmp-decoy" not in refs


def test_should_ignore_directory_and_hidden(tmp_path: Path) -> None:
    from ordine.core.triggers import should_ignore

    directory = tmp_path / "subdir"
    directory.mkdir()
    assert should_ignore(directory) is True
    visible = tmp_path / "ok.png"
    visible.write_bytes(b"x")
    assert should_ignore(visible) is False
    hidden = tmp_path / ".secret.png"
    hidden.write_bytes(b"x")
    assert should_ignore(hidden) is True


def test_extract_ordinal_none_regex_and_nondigit_group() -> None:
    assert extract_ordinal("anything.png", None) is None
    # Match succeeds but capture group is non-digit -> None (isdigit branch).
    assert extract_ordinal("img_abc.png", r"img_(.+)\.png") is None


def test_ordinal_for_trigger_regex_arrival_and_none() -> None:
    from ordine.core.triggers import ordinal_for_trigger

    with_regex = ManualTrigger(
        type="manual", path="/tmp", glob="*", ordinal_regex=r"img_(\d+)\.png"
    )
    assert ordinal_for_trigger("img_0009.png", with_regex, arrival_index=0) == 9
    arrival = FolderWatchTrigger(
        type="folder_watch",
        path="/tmp",
        glob="*",
        settle_seconds=0.1,
        arrival_order_ordinals=True,
    )
    assert ordinal_for_trigger("a.png", arrival, arrival_index=2) == 3
    plain = ManualTrigger(type="manual", path="/tmp", glob="*")
    assert ordinal_for_trigger("a.png", plain, arrival_index=0) is None


def test_scan_directory_missing_path_and_unmatched_ordinal(tmp_path: Path) -> None:
    from ordine.core.triggers import scan_directory

    missing = tmp_path / "no-such-dir"
    assert scan_directory(missing, "*", "filename", None, lambda c: 1) == 0

    watch = tmp_path / "watch"
    watch.mkdir()
    (watch / "photo.png").write_bytes(b"x")
    emitted: list[TaskCandidate] = []

    def capture(candidate: TaskCandidate) -> int | None:
        emitted.append(candidate)
        return 1

    count = scan_directory(
        watch, "*.png", "filename", r"img_(\d+)\.png", capture, settle_seconds=0.0
    )
    assert count == 0
    assert emitted == []


def test_build_trigger_service_error_paths(tmp_path: Path, ledger: Ledger) -> None:
    from ordine.core.errors import TriggerError

    manual = ManualTrigger(type="manual", path=str(tmp_path), glob="*")
    with pytest.raises(TriggerError, match="sink is required"):
        build_trigger_service(manual, "none")

    manifest = ManifestTrigger(type="manifest", path=str(tmp_path / "m.csv"))
    with pytest.raises(TriggerError, match="requires ledger and pipeline_id"):
        build_trigger_service(manifest, "none")

    class _Unsupported:
        pass

    with pytest.raises(TriggerError, match="unsupported trigger type"):
        build_trigger_service(_Unsupported(), "none", sink=lambda c: None)  # type: ignore[arg-type]


def test_folder_watch_start_stop_idempotent(tmp_path: Path, ledger: Ledger) -> None:
    watch = tmp_path / "watch"
    watch.mkdir()
    pipeline_id = _register_pipeline(ledger)
    sink = ledger_sink(ledger, pipeline_id)
    spec = FolderWatchTrigger(type="folder_watch", path=str(watch), glob="*", settle_seconds=0.1)
    service = FolderWatchService(spec, "filename", sink, poll_interval=0.05, enable_poller=False)
    service.stop()  # not started
    service.start()
    service.start()  # already started
    service.stop()
    service.stop()  # idempotent


def test_watch_handler_on_moved_and_bytes_path(tmp_path: Path, ledger: Ledger) -> None:
    from ordine.core.triggers import _WatchHandler

    watch = tmp_path / "watch"
    watch.mkdir()
    pipeline_id = _register_pipeline(ledger)
    sink = ledger_sink(ledger, pipeline_id)
    spec = FolderWatchTrigger(
        type="folder_watch", path=str(watch), glob="*.bin", settle_seconds=0.05
    )
    service = FolderWatchService(spec, "filename", sink, poll_interval=0.05, enable_poller=False)
    handler = _WatchHandler(service)

    src = watch / "old.bin"
    dest = watch / "new.bin"
    src.write_bytes(b"payload")
    dest.write_bytes(b"payload")

    class _Evt:
        def __init__(self, src_path, dest_path=None):
            self.src_path = src_path
            self.dest_path = dest_path

    # bytes path exercises _as_path decode branch
    handler.on_created(_Evt(str(src).encode()))
    assert src.resolve() in service._settle

    handler.on_moved(_Evt(str(src), str(dest)))
    assert src.resolve() not in service._settle
    assert dest.resolve() in service._settle

    handler.on_modified(_Evt(None))  # ignored None path


def test_manifest_sink_skips_incomplete_and_oob_ordinal(tmp_path: Path, ledger: Ledger) -> None:
    from ordine.core.triggers import manifest_sink

    manifest = tmp_path / "assets.csv"
    manifest.write_text("name,prompt\na.png,one\n", encoding="utf-8")
    pipeline_id = _register_pipeline(ledger)
    sink = manifest_sink(ledger, pipeline_id, manifest)

    assert sink(TaskCandidate(source_ref="x", dedup_key=None, ordinal=1)) is None
    assert sink(TaskCandidate(source_ref="x", dedup_key="k", ordinal=None)) is None
    assert sink(TaskCandidate(source_ref="x", dedup_key="k", ordinal=99)) is None


def test_manifest_sink_missing_path_stat(tmp_path: Path, ledger: Ledger) -> None:
    from ordine.core.triggers import manifest_sink

    missing = tmp_path / "gone.csv"
    pipeline_id = _register_pipeline(ledger)
    sink = manifest_sink(ledger, pipeline_id, missing)
    # OSError on stat -> mtime -1; load_manifest fails -> ManifestError path
    assert sink(TaskCandidate(source_ref="x", dedup_key="k", ordinal=1)) is None


def test_build_candidate_unreadable_file(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    import logging

    from ordine.core.triggers import _build_candidate

    path = tmp_path / "gone.bin"
    path.write_bytes(b"x")

    def boom(*_a, **_k):
        raise OSError("unreadable")

    monkeypatch.setattr(
        "ordine.core.triggers.compute_dedup_key",
        boom,
    )
    assert _build_candidate(path, "filename", None, log=logging.getLogger("t")) is None


def test_poller_stops_after_repeated_crashes(
    tmp_path: Path, ledger: Ledger, monkeypatch: pytest.MonkeyPatch
) -> None:
    import ordine.core.triggers as triggers_mod

    watch = tmp_path / "watch"
    watch.mkdir()
    pipeline_id = _register_pipeline(ledger)
    sink = ledger_sink(ledger, pipeline_id)
    spec = FolderWatchTrigger(type="folder_watch", path=str(watch), glob="*", settle_seconds=0.05)
    monkeypatch.setattr(triggers_mod, "_MAX_POLLER_CRASHES", 2)
    service = FolderWatchService(spec, "filename", sink, poll_interval=0.01)

    def boom() -> None:
        raise RuntimeError("poll boom")

    stopped: list[bool] = []

    def fake_stop() -> None:
        stopped.append(True)
        service._stop.set()
        service._started = False

    monkeypatch.setattr(service, "_poll_once", boom)
    monkeypatch.setattr(service, "stop", fake_stop)
    service._started = True
    service._poller_loop()
    assert stopped == [True]
    assert service._poller_crashes >= 2


def test_manifest_service_start_stop_idempotent(tmp_path: Path, ledger: Ledger) -> None:
    manifest = tmp_path / "assets.csv"
    manifest.write_text("name,prompt\na.png,one\n", encoding="utf-8")
    pipeline_id = _register_pipeline(ledger)
    service = build_trigger_service(
        ManifestTrigger(type="manifest", path=str(manifest), poll_seconds=0),
        "none",
        ledger=ledger,
        pipeline_id=pipeline_id,
    )
    assert isinstance(service, ManifestTriggerService)
    service.stop()
    service.start()
    service.start()
    service.stop()
    service.stop()


def test_rescan_skips_ignored_dotfiles(tmp_path: Path, ledger: Ledger) -> None:
    watch = tmp_path / "watch"
    watch.mkdir()
    (watch / "ok.png").write_bytes(b"x")
    (watch / ".hidden.png").write_bytes(b"x")
    pipeline_id = _register_pipeline(ledger)
    sink = ledger_sink(ledger, pipeline_id)
    spec = FolderWatchTrigger(type="folder_watch", path=str(watch), glob="*", settle_seconds=0.1)
    service = FolderWatchService(spec, "filename", sink, poll_interval=0.05, enable_poller=False)
    seeded = service.rescan()
    assert seeded == 1
    assert any(p.name == "ok.png" for p in service._settle)
    assert all(not p.name.startswith(".") for p in service._settle)


def test_drain_timeout_clears_started_flag(tmp_path: Path, ledger: Ledger) -> None:
    watch = tmp_path / "watch"
    watch.mkdir()
    pipeline_id = _register_pipeline(ledger)
    sink = ledger_sink(ledger, pipeline_id)
    spec = FolderWatchTrigger(type="folder_watch", path=str(watch), glob="*", settle_seconds=5.0)
    service = FolderWatchService(spec, "filename", sink, poll_interval=0.05, enable_poller=False)
    service._started = True
    target = watch / "slow.bin"
    target.write_bytes(b"x")
    service._note_path(target)
    service.drain(timeout=0.15)
    assert service._started is False
