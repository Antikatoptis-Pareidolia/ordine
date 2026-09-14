"""Regression tests for PipelineService.stop orphan-worker handling (A1)."""

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
from ordine.core.runner import PipelineRunner, PipelineService
from ordine.web.services import ServiceManager


class _BlockingRunner:
    """Runner stand-in whose run_once blocks until released (external to stop())."""

    def __init__(self) -> None:
        self.entered = threading.Event()
        self.release = threading.Event()

    def run_once(self) -> None:
        self.entered.set()
        self.release.wait(timeout=30.0)
        return None


def _manual_yaml(watch: Path) -> str:
    return f"""version: 1
name: stop-orphan
trigger:
  type: manual
  path: {watch}
  glob: "*.txt"
steps:
  - util.noop
"""


@pytest.fixture
def stop_env(tmp_path: Path):
    engine = create_engine_for(tmp_path / "ledger.db")
    init_db(engine)
    ledger = Ledger(engine)
    registry = StepRegistry.load()
    engines = EngineRegistry()
    engines.register(HeadlessEngine())
    watch = tmp_path / "in"
    watch.mkdir()
    yaml_text = _manual_yaml(watch)
    playbook = loads_playbook(yaml_text)
    pipeline_id, version = ledger.register_pipeline(playbook, yaml_text)
    runner = PipelineRunner(
        ledger=ledger,
        registry=registry,
        engines=engines,
        playbook=playbook,
        pipeline_id=pipeline_id,
        workdir_root=tmp_path / "work",
        playbook_version=version,
    )
    return ledger, playbook, pipeline_id, runner, registry, engines


def test_stop_keeps_alive_worker_and_reports_failure(stop_env) -> None:
    ledger, playbook, pipeline_id, _runner, _registry, _engines = stop_env
    blocking = _BlockingRunner()
    service = PipelineService(
        ledger=ledger,
        runner=blocking,  # type: ignore[arg-type]
        playbook=playbook,
        pipeline_id=pipeline_id,
        stop_join_timeout=0.2,
    )
    service.start()
    assert blocking.entered.wait(timeout=2.0)
    assert service.stop() is False
    assert service.stop_failed is True
    assert service.worker_alive is True
    worker = service._worker
    assert worker is not None and worker.is_alive()
    blocking.release.set()
    worker.join(timeout=2.0)


def test_service_manager_pause_surfaces_degraded(stop_env, tmp_path: Path) -> None:
    ledger, playbook, pipeline_id, _runner, registry, engines = stop_env
    config = AppConfig(db_path=tmp_path / "ledger.db", workdir_root=tmp_path / "work")
    manager = ServiceManager(config=config, ledger=ledger, registry=registry, engines=engines)
    blocking = _BlockingRunner()
    service = PipelineService(
        ledger=ledger,
        runner=blocking,  # type: ignore[arg-type]
        playbook=playbook,
        pipeline_id=pipeline_id,
        stop_join_timeout=0.2,
    )
    service.start()
    assert blocking.entered.wait(timeout=2.0)
    runtime = manager.runtime(pipeline_id)
    runtime._service = service
    runtime.status = "running"
    manager.pause(pipeline_id)
    assert manager.status(pipeline_id) == "degraded"
    assert runtime._service is service
    assert "stop_failed" in (runtime.start_error or "")
    blocking.release.set()
    time.sleep(0.3)
