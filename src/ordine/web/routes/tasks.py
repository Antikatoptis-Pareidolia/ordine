"""Task-centric AI feature routes (diagnose, suggest/approve branch).

Owns thin HTTP wiring for LLM features on tasks. Must never auto-apply branches.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Annotated, cast

from fastapi import APIRouter, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from starlette import status

from ordine.core.config import AppConfig
from ordine.core.errors import LedgerError
from ordine.core.ledger import Ledger
from ordine.core.playbook import loads_playbook
from ordine.core.registry import StepRegistry
from ordine.llm.client import build_client
from ordine.llm.errors import LLMError, LLMNotConfiguredError
from ordine.llm.features.branches import BranchSuggestion, apply_branch, suggest_branch
from ordine.llm.features.diagnosis import diagnose, load_diagnosis, save_diagnosis
from ordine.web.diffing import summarize_playbook_changes

logger = logging.getLogger(__name__)
router = APIRouter()


@dataclass
class PendingBranch:
    pipeline_id: int
    suggestion: BranchSuggestion


class BranchSuggestionStore:
    """Process-local pending branch suggestions keyed by task id."""

    def __init__(self) -> None:
        self._pending: dict[int, PendingBranch] = {}

    def put(self, task_id: int, *, pipeline_id: int, suggestion: BranchSuggestion) -> None:
        self._pending[task_id] = PendingBranch(pipeline_id=pipeline_id, suggestion=suggestion)

    def get(self, task_id: int) -> PendingBranch | None:
        return self._pending.get(task_id)

    def discard(self, task_id: int) -> None:
        self._pending.pop(task_id, None)


def _templates(request: Request) -> Jinja2Templates:
    return Jinja2Templates(directory=str(request.app.state.templates_dir))


def _config(request: Request) -> AppConfig:
    return cast(AppConfig, request.app.state.config)


def _ledger(request: Request) -> Ledger:
    return cast(Ledger, request.app.state.ledger)


def _registry(request: Request) -> StepRegistry:
    return cast(StepRegistry, request.app.state.registry)


def _branch_store(request: Request) -> BranchSuggestionStore:
    return cast(BranchSuggestionStore, request.app.state.branch_suggestions)


def _not_configured_response(request: Request, template: str, **extra: object) -> HTMLResponse:
    templates = _templates(request)
    return templates.TemplateResponse(
        request,
        template,
        {"request": request, "llm_configured": False, **extra},
        status_code=200,
    )


def _first_open_flag(ledger: Ledger, task_id: int, pipeline_id: int) -> int | None:
    flags = [f for f in ledger.open_flags(pipeline_id=pipeline_id) if f.task_id == task_id]
    return flags[0].id if flags else None


@router.post("/tasks/{task_id}/ai/diagnose", response_class=HTMLResponse)
async def task_diagnose(
    request: Request,
    task_id: int,
    include_image: Annotated[str | None, Form()] = None,
) -> HTMLResponse:
    templates = _templates(request)
    ledger = _ledger(request)
    config = _config(request)
    try:
        client = build_client(config)
    except LLMNotConfiguredError:
        return _not_configured_response(
            request,
            "partials/ai_diagnosis_card.html",
            flag_id=0,
            diagnosis=None,
        )
    try:
        task = ledger.get_task(task_id)
    except LedgerError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    flag_id = _first_open_flag(ledger, task_id, task.pipeline_id) or 0
    try:
        result = diagnose(
            client,
            _registry(request),
            ledger,
            task_id,
            config.workdir_root,
            include_image=include_image == "on",
        )
    except LLMError as exc:
        return templates.TemplateResponse(
            request,
            "partials/ai_diagnosis_card.html",
            {
                "request": request,
                "flag_id": flag_id,
                "diagnosis": None,
                "error": str(exc),
            },
        )
    if task.workdir and flag_id:
        save_diagnosis(Path(task.workdir), flag_id, result)
    return templates.TemplateResponse(
        request,
        "partials/ai_diagnosis_card.html",
        {"request": request, "flag_id": flag_id, "diagnosis": result},
    )


@router.post("/tasks/{task_id}/ai/suggest-branch", response_class=HTMLResponse)
async def task_suggest_branch(request: Request, task_id: int) -> HTMLResponse:
    templates = _templates(request)
    ledger = _ledger(request)
    config = _config(request)
    try:
        client = build_client(config)
    except LLMNotConfiguredError:
        return _not_configured_response(
            request,
            "partials/ai_branch_approval.html",
            task_id=task_id,
            suggestion=None,
            changes=[],
            problems=[],
        )
    try:
        task = ledger.get_task(task_id)
    except LedgerError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    suggestion = suggest_branch(
        client,
        _registry(request),
        ledger,
        task_id,
        config.workdir_root,
    )
    _branch_store(request).put(task_id, pipeline_id=task.pipeline_id, suggestion=suggestion)
    changes = []
    if suggestion.new_playbook is not None:
        _pid, yaml_text = ledger.get_current_playbook(task.pipeline_id)
        old = loads_playbook(yaml_text)
        changes = summarize_playbook_changes(old, suggestion.new_playbook)
    return templates.TemplateResponse(
        request,
        "partials/ai_branch_approval.html",
        {
            "request": request,
            "task_id": task_id,
            "suggestion": suggestion,
            "changes": changes,
            "problems": suggestion.problems,
            "llm_configured": True,
        },
    )


@router.post("/tasks/{task_id}/ai/approve-branch")
async def task_approve_branch(request: Request, task_id: int) -> RedirectResponse:
    ledger = _ledger(request)
    pending = _branch_store(request).get(task_id)
    if pending is None or pending.suggestion.new_playbook is None:
        return RedirectResponse(
            f"/tasks/{task_id}",
            status_code=status.HTTP_303_SEE_OTHER,
        )
    note = f"AI branch: {pending.suggestion.branch.name}"
    version = apply_branch(
        ledger,
        pending.pipeline_id,
        pending.suggestion,
        note=note,
    )
    _branch_store(request).discard(task_id)
    return RedirectResponse(
        f"/pipelines/{pending.pipeline_id}/versions?flash=Approved+{version}",
        status_code=status.HTTP_303_SEE_OTHER,
    )


@router.post("/tasks/{task_id}/ai/open-branch-editor")
async def task_open_branch_editor(request: Request, task_id: int) -> RedirectResponse:
    pending = _branch_store(request).get(task_id)
    if pending is None:
        return RedirectResponse(f"/tasks/{task_id}", status_code=status.HTTP_303_SEE_OTHER)
    return RedirectResponse(
        f"/pipelines/{pending.pipeline_id}/edit?ai_branch_task={task_id}",
        status_code=status.HTTP_303_SEE_OTHER,
    )


@router.post("/tasks/{task_id}/ai/discard-branch")
async def task_discard_branch(request: Request, task_id: int) -> RedirectResponse:
    _branch_store(request).discard(task_id)
    return RedirectResponse(f"/tasks/{task_id}", status_code=status.HTTP_303_SEE_OTHER)


def diagnosis_for_task(ledger: Ledger, task_id: int, workdir: Path | None) -> object | None:
    """Load persisted diagnosis for display on task detail."""
    if workdir is None:
        return None
    task = ledger.get_task(task_id)
    flag_id = _first_open_flag(ledger, task_id, task.pipeline_id)
    if flag_id is None:
        return None
    return load_diagnosis(workdir, flag_id)
