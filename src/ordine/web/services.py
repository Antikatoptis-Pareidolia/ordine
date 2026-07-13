"""In-process pipeline service lifecycle for ``ordine serve``.

Owns PipelineService instances and their running/paused state. Must never implement step logic.
"""

from __future__ import annotations

import logging
import threading
from dataclasses import dataclass, field
from datetime import timedelta
from typing import Literal

from ordine.core.config import AppConfig
from ordine.core.engines import EngineRegistry
from ordine.core.errors import FieldError, PlaybookSyntaxError, PlaybookValidationError
from ordine.core.ledger import Ledger
from ordine.core.playbook import loads_playbook
from ordine.core.registry import StepRegistry
from ordine.core.runner import PipelineRunner, PipelineService

logger = logging.getLogger(__name__)

RuntimeStatus = Literal["running", "paused"]
ActionPending = Literal["starting", "pausing"]


@dataclass
class PipelineRuntime:
    """Runtime view of one managed pipeline."""

    pipeline_id: int
    status: RuntimeStatus = "paused"
    running_version: str | None = None
    start_problems: list[FieldError] = field(default_factory=list)
    start_error: str | None = None
    action_pending: ActionPending | None = None
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
            if runtime.action_pending == "starting":
                return
            if runtime.status == "running" and runtime._service is not None:
                return
            runtime.action_pending = "starting"
            runtime.start_problems = []
            runtime.start_error = None
            old_service = runtime._service

        if old_service is not None:
            old_service.stop()

        try:
            version_id, yaml_text = self._ledger.get_current_playbook(pipeline_id)
        except Exception as exc:
            with self._lock:
                runtime = self.runtime(pipeline_id)
                runtime.start_error = str(exc)
                runtime.status = "paused"
                runtime.action_pending = None
            return
        try:
            playbook = loads_playbook(yaml_text, source=f"pipeline:{pipeline_id}")
        except (PlaybookSyntaxError, PlaybookValidationError) as exc:
            with self._lock:
                runtime = self.runtime(pipeline_id)
                runtime.start_error = str(exc)
                runtime.status = "paused"
                runtime.action_pending = None
            return

        problems = self._registry.check_playbook(playbook)
        if problems:
            with self._lock:
                runtime = self.runtime(pipeline_id)
                runtime.start_problems = problems
                runtime.status = "paused"
                runtime.running_version = version_id
                runtime.action_pending = None
            return

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
        with self._lock:
            runtime = self.runtime(pipeline_id)
            # If a pause raced while we were starting, honor it immediately.
            if runtime.action_pending == "pausing":
                pending_service = service
                runtime._service = None
                runtime.status = "paused"
                runtime.running_version = version_id
                runtime.action_pending = None
            else:
                pending_service = None
                runtime._service = service
                runtime.status = "running"
                runtime.running_version = version_id
                runtime.action_pending = None
        if pending_service is not None:
            pending_service.stop()

    def pause(self, pipeline_id: int) -> None:
        """Gracefully stop the pipeline service (in-flight task may finish)."""
        with self._lock:
            runtime = self._runtimes.get(pipeline_id)
            if runtime is None:
                return
            runtime.action_pending = "pausing"
            service = runtime._service
            runtime._service = None
            if service is None:
                runtime.status = "paused"
                runtime.action_pending = None
                return
        service.stop()
        with self._lock:
            runtime = self.runtime(pipeline_id)
            runtime.status = "paused"
            runtime.action_pending = None

    def action_pending_label(self, pipeline_id: int) -> ActionPending | None:
        """Return a pending action label until runtime status confirms the transition."""
        with self._lock:
            runtime = self.runtime(pipeline_id)
            if (runtime.action_pending == "starting" and runtime.status == "running") or (
                runtime.action_pending == "pausing" and runtime.status == "paused"
            ):
                runtime.action_pending = None
            return runtime.action_pending

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
