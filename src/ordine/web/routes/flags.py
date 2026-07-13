"""Flag-centric AI feature routes.

Owns thin HTTP wiring for diagnosing from the flags inbox.
"""

from __future__ import annotations

from pathlib import Path
from typing import Annotated, cast

from fastapi import APIRouter, Form, HTTPException, Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates

from ordine.core.config import AppConfig
from ordine.core.ledger import Ledger
from ordine.core.registry import StepRegistry
from ordine.llm.client import build_client
from ordine.llm.errors import LLMError, LLMNotConfiguredError
from ordine.llm.features.diagnosis import diagnose, save_diagnosis

router = APIRouter()


def _templates(request: Request) -> Jinja2Templates:
    return Jinja2Templates(directory=str(request.app.state.templates_dir))


def _ledger(request: Request) -> Ledger:
    return cast(Ledger, request.app.state.ledger)


def _config(request: Request) -> AppConfig:
    return cast(AppConfig, request.app.state.config)


def _registry(request: Request) -> StepRegistry:
    return cast(StepRegistry, request.app.state.registry)


@router.post("/flags/{flag_id}/ai/diagnose", response_class=HTMLResponse)
async def flag_diagnose(
    request: Request,
    flag_id: int,
    include_image: Annotated[str | None, Form()] = None,
) -> HTMLResponse:
    templates = _templates(request)
    ledger = _ledger(request)
    config = _config(request)
    flags = ledger.list_open_flags()
    flag = next((row for row in flags if row.id == flag_id), None)
    if flag is None or flag.task_id is None:
        raise HTTPException(status_code=404, detail="flag not found")
    try:
        client = build_client(config)
    except LLMNotConfiguredError:
        return templates.TemplateResponse(
            request,
            "partials/ai_diagnosis_card.html",
            {"request": request, "flag_id": flag_id, "diagnosis": None, "llm_configured": False},
        )
    try:
        result = diagnose(
            client,
            _registry(request),
            ledger,
            flag.task_id,
            config.workdir_root,
            include_image=include_image == "on",
        )
    except LLMError as exc:
        return templates.TemplateResponse(
            request,
            "partials/ai_diagnosis_card.html",
            {"request": request, "flag_id": flag_id, "diagnosis": None, "error": str(exc)},
        )
    task = ledger.get_task(flag.task_id)
    if task.workdir:
        save_diagnosis(Path(task.workdir), flag_id, result)
    return templates.TemplateResponse(
        request,
        "partials/ai_diagnosis_card.html",
        {"request": request, "flag_id": flag_id, "diagnosis": result},
    )
