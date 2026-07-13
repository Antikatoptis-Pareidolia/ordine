"""ServiceManager lifecycle tests."""

from __future__ import annotations

import threading
import time
from pathlib import Path

import pytest

from ordine.core.config import AppConfig
from ordine.core.db import create_engine_for, init_db
from ordine.core.engines import EngineRegistry, HeadlessEngine
from ordine.core.ledger import Ledger
from ordine.core.playbook import loads_playbook
from ordine.core.registry import StepRegistry
from ordine.web.services import ServiceManager
from tests.test_image_steps import make_test_image


def _watch_yaml(watch: Path) -> str:
    return f"""version: 1
name: svc-watch
trigger:
  type: folder_watch
  path: {watch}
  glob: "*.png"
  ordinal_regex: 'img_(\\d+)\\.png'
  settle_seconds: 0.5
steps:
  - util.noop
"""


@pytest.fixture
def svc_env(tmp_path: Path):
    engine = create_engine_for(tmp_path / "ledger.db")
    init_db(engine)
    ledger = Ledger(engine)
    registry = StepRegistry.load()
    engines = EngineRegistry()
    engines.register(HeadlessEngine())
    config = AppConfig(
        db_path=tmp_path / "ledger.db",
        workdir_root=tmp_path / "work",
        stale_after_minutes=15,
        reconcile_policy="retry",
    )
    watch = tmp_path / "watch"
    watch.mkdir()
    yaml_text = _watch_yaml(watch)
    playbook = loads_playbook(yaml_text)
    pipeline_id, _ = ledger.register_pipeline(playbook, yaml_text)
    manager = ServiceManager(config=config, ledger=ledger, registry=registry, engines=engines)
    return manager, ledger, pipeline_id, watch


def test_start_processes_file_and_pause_blocks(svc_env) -> None:
    manager, ledger, pipeline_id, watch = svc_env
    manager.start(pipeline_id)
    assert manager.status(pipeline_id) == "running"
    path = watch / "img_0001.png"
    make_test_image(path)
    path.write_bytes(path.read_bytes() + b"1")
    deadline = time.monotonic() + 10.0
    while time.monotonic() < deadline:
        if ledger.counts(pipeline_id)["done"] >= 1:
            break
        time.sleep(0.2)
    assert ledger.counts(pipeline_id)["done"] == 1
    manager.pause(pipeline_id)
    assert manager.status(pipeline_id) == "paused"
    path2 = watch / "img_0002.png"
    make_test_image(path2)
    path2.write_bytes(path2.read_bytes() + b"2")
    time.sleep(1.5)
    assert ledger.counts(pipeline_id)["done"] == 1
    manager.start(pipeline_id)
    deadline = time.monotonic() + 10.0
    while time.monotonic() < deadline:
        if ledger.counts(pipeline_id)["done"] >= 2:
            break
        time.sleep(0.2)
    assert ledger.counts(pipeline_id)["done"] == 2


def test_start_invalid_playbook_surfaces_problems(svc_env) -> None:
    manager, ledger, pipeline_id, watch = svc_env
    good_yaml = _watch_yaml(watch)
    good = loads_playbook(good_yaml)
    bad_yaml = good_yaml.replace("util.noop", "unknown.step")
    ledger.register_pipeline(good, bad_yaml)
    manager.start(pipeline_id)
    assert manager.status(pipeline_id) == "paused"
    assert manager.start_problems(pipeline_id)


def test_shutdown_stops_all(svc_env) -> None:
    manager, _ledger, pipeline_id, _watch = svc_env
    manager.start(pipeline_id)
    started = time.monotonic()
    manager.shutdown()
    assert manager.status(pipeline_id) == "paused"
    assert time.monotonic() - started < 5.0


def test_pause_does_not_block_status(svc_env) -> None:
    manager, _ledger, pipeline_id, _watch = svc_env
    runtime = manager.runtime(pipeline_id)

    allow_stop = threading.Event()

    class SlowStopService:
        def stop(self) -> None:
            allow_stop.wait(timeout=2.0)

    runtime._service = SlowStopService()  # type: ignore[assignment]
    runtime.status = "running"

    pause_thread = threading.Thread(target=lambda: manager.pause(pipeline_id), daemon=True)
    pause_thread.start()

    start = time.monotonic()
    # status() should remain responsive even while pause is waiting on stop().
    assert manager.status(pipeline_id) in ("running", "paused")
    assert time.monotonic() - start < 0.1

    allow_stop.set()
    pause_thread.join(timeout=2.0)
    assert manager.status(pipeline_id) == "paused"
