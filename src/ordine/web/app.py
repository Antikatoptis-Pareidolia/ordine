"""FastAPI application factory for the Ordine web UI.

Owns app wiring, middleware, and static/template mounts. Must never implement pipeline logic.
"""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles
from starlette.responses import Response

from ordine.core.config import AppConfig
from ordine.core.db import create_engine_for, init_db
from ordine.core.engines import EngineRegistry
from ordine.core.ledger import Ledger
from ordine.core.registry import StepRegistry
from ordine.web.routes import dashboard as dashboard_routes
from ordine.web.routes import editor as editor_routes
from ordine.web.routes import flags as flags_routes
from ordine.web.routes import lab as lab_routes
from ordine.web.routes import router as core_router
from ordine.web.routes import settings as settings_routes
from ordine.web.routes import tasks as tasks_routes
from ordine.web.routes.lab import LabSessionStore
from ordine.web.routes.tasks import BranchSuggestionStore
from ordine.web.security import post_is_allowed
from ordine.web.services import ServiceManager

logger = logging.getLogger(__name__)

STATIC_DIR = Path(__file__).parent / "static"
TEMPLATES_DIR = Path(__file__).parent / "templates"


def create_app(config: AppConfig) -> FastAPI:
    """Build the FastAPI app with lifespan-managed ServiceManager."""
    engine = create_engine_for(config.db_path)
    init_db(engine)
    ledger = Ledger(engine)
    registry = StepRegistry.load()
    engines = EngineRegistry.load()
    services = ServiceManager(config=config, ledger=ledger, registry=registry, engines=engines)
    lab_sessions = LabSessionStore()
    branch_suggestions = BranchSuggestionStore()

    @asynccontextmanager
    async def lifespan(_app: FastAPI) -> AsyncIterator[None]:
        if config.retention_on_serve_start:
            from ordine.core.retention import run_configured_cleanup

            report = run_configured_cleanup(ledger, config)
            logger.info(
                "retention on serve start: deleted=%s bytes_freed=%s kept=%s",
                report.deleted,
                report.bytes_freed,
                report.kept_reasons,
            )
        pipeline_ids = [summary.id for summary in ledger.list_pipelines()]
        services.autostart_if_configured(pipeline_ids)
        yield
        lab_sessions.close_all()
        services.shutdown()

    app = FastAPI(title="Ordine", lifespan=lifespan)
    app.state.config = config
    app.state.ledger = ledger
    app.state.registry = registry
    app.state.engines = engines
    app.state.services = services
    app.state.lab_sessions = lab_sessions
    app.state.branch_suggestions = branch_suggestions
    app.state.templates_dir = TEMPLATES_DIR

    @app.middleware("http")
    async def post_guard(
        request: Request,
        call_next: Callable[[Request], Awaitable[Response]],
    ) -> Response:
        if request.method == "POST" and not post_is_allowed(request):
            return JSONResponse({"detail": "Forbidden"}, status_code=403)
        response = await call_next(request)
        return response

    app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")
    app.include_router(dashboard_routes.router)
    app.include_router(editor_routes.router)
    app.include_router(lab_routes.router)
    app.include_router(tasks_routes.router)
    app.include_router(flags_routes.router)
    app.include_router(settings_routes.router)
    app.include_router(core_router)
    return app
