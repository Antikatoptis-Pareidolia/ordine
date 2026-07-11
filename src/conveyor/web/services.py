"""In-process pipeline service lifecycle for ``conveyor serve``.

Owns PipelineService instances and their running/paused state. Must never implement step logic.
"""

from __future__ import annotations

import logging
import threading
from dataclasses import dataclass, field
from datetime import timedelta
from typing import Literal

from conveyor.core.config import AppConfig
from conveyor.core.engines import EngineRegistry
from conveyor.core.errors import FieldError, PlaybookSyntaxError, PlaybookValidationError
from conveyor.core.ledger import Ledger
from conveyor.core.playbook import loads_playbook
from conveyor.core.registry import StepRegistry
from conveyor.core.runner import PipelineRunner, PipelineService

logger = logging.getLogger(__name__)

RuntimeStatus = Literal["running", "paused"]


@dataclass
class PipelineRuntime:
    """Runtime view of one managed pipeline."""

    pipeline_id: int
    status: RuntimeStatus = "paused"
    running_version: str | None = None
    start_problems: list[FieldError] = field(default_factory=list)
    start_error: str | None = None
    _service: PipelineService | None = field(default=None, repr=False)


class ServiceManager:
    """Thread-safe manager of PipelineService instances inside the serve process."""

    def __init__(
        self,
        *,
        config: AppConfig,
        ledger: Ledger,
        registry: StepRegistry,
        engines: EngineRegistry,
    ) -> None:
        self._config = config
        self._ledger = ledger
        self._registry = registry
        self._engines = engines
        self._lock = threading.RLock()
        self._runtimes: dict[int, PipelineRuntime] = {}

    def runtime(self, pipeline_id: int) -> PipelineRuntime:
        with self._lock:
            return self._runtimes.setdefault(pipeline_id, PipelineRuntime(pipeline_id=pipeline_id))

    def status(self, pipeline_id: int) -> RuntimeStatus:
        return self.runtime(pipeline_id).status

    def running_version(self, pipeline_id: int) -> str | None:
        return self.runtime(pipeline_id).running_version

    def start_problems(self, pipeline_id: int) -> list[FieldError]:
        return list(self.runtime(pipeline_id).start_problems)

    def start(self, pipeline_id: int) -> None:
        """Load the current playbook version, validate, and start PipelineService."""
        with self._lock:
            runtime = self._runtimes.setdefault(
                pipeline_id, PipelineRuntime(pipeline_id=pipeline_id)
            )
            if runtime.status == "running" and runtime._service is not None:
                return
            runtime.start_problems = []
            runtime.start_error = None
            try:
                version_id, yaml_text = self._ledger.get_current_playbook(pipeline_id)
            except Exception as exc:
                runtime.start_error = str(exc)
                runtime.status = "paused"
                return
            try:
                playbook = loads_playbook(yaml_text, source=f"pipeline:{pipeline_id}")
            except (PlaybookSyntaxError, PlaybookValidationError) as exc:
                runtime.start_error = str(exc)
                runtime.status = "paused"
                return
            problems = self._registry.check_playbook(playbook)
            if problems:
                runtime.start_problems = problems
                runtime.status = "paused"
                runtime.running_version = version_id
                return
            if runtime._service is not None:
                runtime._service.stop()
            stale_after = timedelta(minutes=self._config.stale_after_minutes)
            runner = PipelineRunner(
                ledger=self._ledger,
                registry=self._registry,
                engines=self._engines,
                playbook=playbook,
                pipeline_id=pipeline_id,
                workdir_root=self._config.workdir_root,
                playbook_version=version_id,
            )
            service = PipelineService(
                ledger=self._ledger,
                runner=runner,
                playbook=playbook,
                pipeline_id=pipeline_id,
                stale_after=stale_after,
                reconcile_policy=self._config.reconcile_policy,
            )
            service.start()
            runtime._service = service
            runtime.status = "running"
            runtime.running_version = version_id

    def pause(self, pipeline_id: int) -> None:
        """Gracefully stop the pipeline service (in-flight task may finish)."""
        with self._lock:
            runtime = self._runtimes.get(pipeline_id)
            if runtime is None or runtime._service is None:
                if runtime is not None:
                    runtime.status = "paused"
                return
            runtime._service.stop()
            runtime._service = None
            runtime.status = "paused"

    def shutdown(self) -> None:
        """Stop every running pipeline service."""
        with self._lock:
            ids = list(self._runtimes)
        for pipeline_id in ids:
            self.pause(pipeline_id)

    def autostart_if_configured(self, pipeline_ids: list[int]) -> None:
        if not self._config.autostart_pipelines:
            return
        for pipeline_id in pipeline_ids:
            self.start(pipeline_id)
