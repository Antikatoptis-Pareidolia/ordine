"""Dashboard routes and pipeline card helpers.

Owns dashboard HTML only. Must never implement editor or version logic.
"""

from __future__ import annotations

from typing import Any, cast

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates

from ordine.core.ledger import Ledger
from ordine.web.services import ServiceManager
from ordine.web.views import version_label

router = APIRouter()


def _templates(request: Request) -> Jinja2Templates:
    return Jinja2Templates(directory=str(request.app.state.templates_dir))


def _ledger(request: Request) -> Ledger:
    return cast(Ledger, request.app.state.ledger)


def _services(request: Request) -> ServiceManager:
    return cast(ServiceManager, request.app.state.services)


def _flash(request: Request) -> dict[str, str | None]:
    return {
        "flash": request.query_params.get("flash"),
        "flash_level": request.query_params.get("flash_level", "info"),
    }


def pipeline_cards(request: Request) -> list[dict[str, Any]]:
    """Build dashboard card context for each registered pipeline."""
    ledger = _ledger(request)
    services = _services(request)
    cards: list[dict[str, Any]] = []
    for summary in ledger.list_pipelines():
        counts = ledger.counts(summary.id)
        flags = ledger.list_open_flags(pipeline_id=summary.id)
        max_level = max((flag.level for flag in flags), default=0)
        runtime = services.runtime(summary.id)
        action_pending = services.action_pending_label(summary.id)
        running_version = runtime.running_version
        current_version = summary.current_version
        version_rows = {row.public_id: row for row in ledger.list_versions(summary.id)}
        current_row = version_rows.get(current_version) if current_version else None
        running_row = version_rows.get(running_version) if running_version else None
        cards.append(
            {
                "id": summary.id,
                "name": summary.name,
                "current_version": current_version,
                "current_version_label": version_label(
                    current_version,
                    current_row.note if current_row else None,
                    parent_id=current_row.parent_public_id if current_row else None,
                )
                if current_version
                else None,
                "running_version": running_version,
                "running_version_label": version_label(
                    running_version,
                    running_row.note if running_row else None,
                )
                if running_version
                else None,
                "version_drift": bool(
                    running_version and current_version and running_version != current_version
                ),
                "service_status": runtime.status,
                "action_pending": action_pending,
                "counts": counts,
                "open_flags": len(flags),
                "max_flag_level": max_level,
                "start_problems": runtime.start_problems,
                "start_error": runtime.start_error,
            }
        )
    return cards


@router.get("/", response_class=HTMLResponse)
async def dashboard(request: Request) -> HTMLResponse:
    templates = _templates(request)
    return templates.TemplateResponse(
        request,
        "dashboard.html",
        {
            "request": request,
            "cards": pipeline_cards(request),
            **_flash(request),
        },
    )


@router.get("/partials/pipelines", response_class=HTMLResponse)
async def pipelines_partial(request: Request) -> HTMLResponse:
    templates = _templates(request)
    return templates.TemplateResponse(
        request,
        "partials/pipeline_cards.html",
        {"request": request, "cards": pipeline_cards(request)},
    )
