"""FastAPI application factory for the Conveyor web UI.

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

from conveyor.core.config import AppConfig
from conveyor.core.db import create_engine_for, init_db
from conveyor.core.engines import EngineRegistry
from conveyor.core.ledger import Ledger
from conveyor.core.registry import StepRegistry
from conveyor.web.routes import dashboard as dashboard_routes
from conveyor.web.routes import editor as editor_routes
from conveyor.web.routes import router as core_router
from conveyor.web.security import post_is_allowed
from conveyor.web.services import ServiceManager

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

    @asynccontextmanager
    async def lifespan(_app: FastAPI) -> AsyncIterator[None]:
        pipeline_ids = [summary.id for summary in ledger.list_pipelines()]
        services.autostart_if_configured(pipeline_ids)
        yield
        services.shutdown()

    app = FastAPI(title="Conveyor", lifespan=lifespan)
    app.state.config = config
    app.state.ledger = ledger
    app.state.registry = registry
    app.state.engines = engines
    app.state.services = services
    app.state.templates_dir = TEMPLATES_DIR

    @app.middleware("http")
    async def post_guard(
        request: Request,
        call_next: Callable[[Request], Awaitable[Response]],
    ) -> Response:
        if request.method == "POST" and not post_is_allowed(request, serve_host=config.web_host):
            return JSONResponse({"detail": "Forbidden"}, status_code=403)
        response = await call_next(request)
        return response

    app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")
    app.include_router(dashboard_routes.router)
    app.include_router(editor_routes.router)
    app.include_router(core_router)
    return app
