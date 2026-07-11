"""HTML route handlers for the Conveyor web UI.

Owns HTTP parsing and template rendering only. Must never implement pipeline business logic.
"""

from __future__ import annotations

import json
import logging
from contextlib import suppress
from pathlib import Path
from typing import Annotated, Any, Literal, cast
from urllib.parse import quote

from fastapi import APIRouter, Form, HTTPException, Query, Request
from fastapi.responses import FileResponse, HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from starlette import status

from conveyor.cli import output as cli_output
from conveyor.core.config import AppConfig, load_config, save_web_runner_settings
from conveyor.core.errors import (
    ConfigError,
    IllegalTransitionError,
    LedgerError,
    PlaybookSyntaxError,
    PlaybookValidationError,
)
from conveyor.core.ledger import Ledger
from conveyor.core.playbook import loads_playbook
from conveyor.core.registry import StepRegistry
from conveyor.web.security import resolve_artifact
from conveyor.web.services import ServiceManager
from conveyor.web.views import can_cancel, can_retry

logger = logging.getLogger(__name__)

router = APIRouter()
IMAGE_SUFFIXES = {".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp"}


def _templates(request: Request) -> Jinja2Templates:
    return Jinja2Templates(directory=str(request.app.state.templates_dir))


def _config(request: Request) -> AppConfig:
    return cast(AppConfig, request.app.state.config)


def _ledger(request: Request) -> Ledger:
    return cast(Ledger, request.app.state.ledger)


def _registry(request: Request) -> StepRegistry:
    return cast(StepRegistry, request.app.state.registry)


def _services(request: Request) -> ServiceManager:
    return cast(ServiceManager, request.app.state.services)


def _flash(request: Request) -> dict[str, str | None]:
    return {
        "flash": request.query_params.get("flash"),
        "flash_level": request.query_params.get("flash_level", "info"),
    }


def _redirect(path: str, *, flash: str | None = None, level: str = "info") -> RedirectResponse:
    if flash:
        return RedirectResponse(
            f"{path}?flash={quote(flash)}&flash_level={quote(level)}",
            status_code=status.HTTP_303_SEE_OTHER,
        )
    return RedirectResponse(path, status_code=status.HTTP_303_SEE_OTHER)


def _pipeline_cards(request: Request) -> list[dict[str, Any]]:
    ledger = _ledger(request)
    services = _services(request)
    cards: list[dict[str, Any]] = []
    for summary in ledger.list_pipelines():
        counts = ledger.counts(summary.id)
        flags = ledger.list_open_flags(pipeline_id=summary.id)
        max_level = max((flag.level for flag in flags), default=0)
        runtime = services.runtime(summary.id)
        cards.append(
            {
                "id": summary.id,
                "name": summary.name,
                "current_version": summary.current_version,
                "running_version": runtime.running_version,
                "service_status": runtime.status,
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
            "cards": _pipeline_cards(request),
            **_flash(request),
        },
    )


@router.get("/partials/pipelines", response_class=HTMLResponse)
async def pipelines_partial(request: Request) -> HTMLResponse:
    templates = _templates(request)
    return templates.TemplateResponse(
        request,
        "partials/pipeline_cards.html",
        {"request": request, "cards": _pipeline_cards(request)},
    )


@router.post("/pipelines", response_model=None)
async def register_pipeline(
    request: Request, yaml_text: Annotated[str, Form()]
) -> HTMLResponse | RedirectResponse:
    ledger = _ledger(request)
    registry = _registry(request)
    templates = _templates(request)
    try:
        playbook = loads_playbook(yaml_text, source="<paste>")
    except (PlaybookSyntaxError, PlaybookValidationError) as exc:
        return templates.TemplateResponse(
            request,
            "dashboard.html",
            {
                "request": request,
                "cards": _pipeline_cards(request),
                "register_yaml": yaml_text,
                "register_error": str(exc),
                "register_problems": [],
                **_flash(request),
            },
            status_code=200,
        )
    problems = registry.check_playbook(playbook)
    if problems:
        return templates.TemplateResponse(
            request,
            "dashboard.html",
            {
                "request": request,
                "cards": _pipeline_cards(request),
                "register_yaml": yaml_text,
                "register_problems": problems,
                "register_error": None,
                **_flash(request),
            },
            status_code=200,
        )
    pipeline_id = ledger.find_pipeline_id(playbook.name)
    if pipeline_id is None:
        ledger.register_pipeline(playbook, yaml_text)
    else:
        _, current_yaml = ledger.get_current_playbook(pipeline_id)
        if current_yaml != yaml_text:
            ledger.register_pipeline(playbook, yaml_text)
    return _redirect("/", flash=f"Registered pipeline {playbook.name}")


@router.post("/pipelines/{pipeline_id}/start")
async def pipeline_start(request: Request, pipeline_id: int) -> RedirectResponse:
    _services(request).start(pipeline_id)
    return _redirect("/")


@router.post("/pipelines/{pipeline_id}/pause")
async def pipeline_pause(request: Request, pipeline_id: int) -> RedirectResponse:
    _services(request).pause(pipeline_id)
    return _redirect("/")


@router.get("/pipelines/{pipeline_id}/tasks", response_class=HTMLResponse)
async def pipeline_tasks(
    request: Request,
    pipeline_id: int,
    status_filter: Annotated[str | None, Query(alias="status")] = None,
    page: Annotated[int, Query(ge=1)] = 1,
) -> HTMLResponse:
    ledger = _ledger(request)
    per_page = 50
    offset = (page - 1) * per_page
    summaries = {s.id: s for s in ledger.list_pipelines()}
    summary = summaries.get(pipeline_id)
    if summary is None:
        raise HTTPException(status_code=404, detail="pipeline not found")
    tasks = ledger.list_tasks(
        pipeline_id,
        status=cast(
            Literal["pending", "processing", "done", "skipped", "failed", "flagged"] | None,
            status_filter,
        ),
        limit=per_page,
        offset=offset,
    )
    templates = _templates(request)
    return templates.TemplateResponse(
        request,
        "pipeline_tasks.html",
        {
            "request": request,
            "pipeline": summary,
            "tasks": tasks,
            "status_filter": status_filter,
            "page": page,
            **_flash(request),
        },
    )


def _load_task_json(workdir: Path) -> dict[str, Any] | None:
    path = workdir / "task.json"
    if not path.is_file():
        return None
    data = json.loads(path.read_text(encoding="utf-8"))
    if isinstance(data, dict):
        return data
    return None


def _log_tail(step_dir: Path, *, lines: int = 100) -> str:
    log_file = step_dir / "log.txt"
    if not log_file.is_file():
        return ""
    content = log_file.read_text(encoding="utf-8", errors="replace").splitlines()
    return "\n".join(content[-lines:])


@router.get("/tasks/{task_id}", response_class=HTMLResponse)
async def task_detail(request: Request, task_id: int) -> HTMLResponse:
    ledger = _ledger(request)
    try:
        task = ledger.get_task(task_id)
    except LedgerError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    attempts = ledger.list_branch_attempts(task_id)
    flags = [
        flag
        for flag in ledger.list_open_flags(pipeline_id=task.pipeline_id)
        if flag.task_id == task_id
    ]
    task_json: dict[str, Any] | None = None
    step_logs: list[dict[str, Any]] = []
    workdir = Path(task.workdir) if task.workdir else None
    source_is_image = Path(task.source_ref).suffix.lower() in IMAGE_SUFFIXES
    last_output_rel: str | None = None
    source_artifact: str | None = None
    if workdir is not None:
        task_json = _load_task_json(workdir)
        if task_json:
            for step in task_json.get("steps", []):
                branch = step.get("branch")
                seq = step.get("seq", 0)
                step_id = str(step.get("id", "step"))
                safe_step = step_id.replace(".", "_")
                if branch:
                    safe_branch = branch.replace("/", "_")
                    attempt = step.get("attempt")
                    prefix = f"b{attempt}_{safe_branch}" if attempt else f"b_{safe_branch}"
                    step_dir = workdir / prefix / f"{seq:02d}_{safe_step}"
                else:
                    step_dir = workdir / f"{seq:02d}_{safe_step}"
                step_logs.append(
                    {
                        "step": step,
                        "log": _log_tail(step_dir),
                    }
                )
                output_path = step.get("output")
                if step.get("status") == "ok" and output_path:
                    out = Path(str(output_path))
                    if out.suffix.lower() in IMAGE_SUFFIXES:
                        with suppress(ValueError):
                            last_output_rel = str(out.resolve().relative_to(workdir.resolve()))
        if source_is_image:
            source_path = Path(task.source_ref)
            try:
                source_artifact = str(source_path.resolve().relative_to(workdir.resolve()))
            except ValueError:
                source_artifact = None

    templates = _templates(request)
    return templates.TemplateResponse(
        request,
        "task_detail.html",
        {
            "request": request,
            "task": task,
            "attempts": attempts,
            "flags": flags,
            "task_json": task_json,
            "step_logs": step_logs,
            "source_is_image": source_is_image,
            "source_artifact": source_artifact,
            "last_output_rel": last_output_rel,
            "can_retry": can_retry(task),
            "can_cancel": can_cancel(task),
            **_flash(request),
        },
    )


@router.post("/tasks/{task_id}/retry")
async def task_retry(request: Request, task_id: int) -> RedirectResponse:
    ledger = _ledger(request)
    try:
        ledger.transition(task_id, "pending")
    except IllegalTransitionError as exc:
        return _redirect(f"/tasks/{task_id}", flash=str(exc), level="error")
    except LedgerError as exc:
        return _redirect("/", flash=str(exc), level="error")
    return _redirect(f"/tasks/{task_id}", flash="Task re-queued")


@router.post("/tasks/{task_id}/cancel")
async def task_cancel(request: Request, task_id: int) -> RedirectResponse:
    ledger = _ledger(request)
    try:
        ledger.transition(task_id, "skipped")
    except IllegalTransitionError as exc:
        return _redirect(f"/tasks/{task_id}", flash=str(exc), level="error")
    except LedgerError as exc:
        return _redirect("/", flash=str(exc), level="error")
    return _redirect(f"/tasks/{task_id}", flash="Task cancelled")


@router.get("/artifacts/{task_id}/{rel_path:path}")
async def artifact(task_id: int, rel_path: str, request: Request) -> FileResponse:
    ledger = _ledger(request)
    try:
        task = ledger.get_task(task_id)
    except LedgerError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    if task.workdir is None:
        raise HTTPException(status_code=404, detail="no workdir")
    resolved = resolve_artifact(Path(task.workdir), rel_path)
    if resolved is None:
        raise HTTPException(status_code=404, detail="not found")
    suffix = resolved.suffix.lower()
    if suffix == ".txt" or resolved.name == "log.txt":
        return FileResponse(resolved, media_type="text/plain")
    if suffix in IMAGE_SUFFIXES:
        return FileResponse(resolved)
    return FileResponse(resolved, media_type="application/octet-stream", filename=resolved.name)


@router.get("/flags", response_class=HTMLResponse)
async def flags_inbox(request: Request) -> HTMLResponse:
    ledger = _ledger(request)
    flags = ledger.list_open_flags()
    pipelines = {s.id: s.name for s in ledger.list_pipelines()}
    rows = [
        {
            "flag": flag,
            "pipeline_name": pipelines.get(flag.pipeline_id, str(flag.pipeline_id)),
            "age": cli_output.format_age(flag.created_at),
        }
        for flag in flags
    ]
    templates = _templates(request)
    return templates.TemplateResponse(
        request,
        "flags.html",
        {"request": request, "rows": rows, **_flash(request)},
    )


@router.post("/flags/{flag_id}/resolve")
async def flag_resolve(
    request: Request,
    flag_id: int,
    note: Annotated[str, Form()],
) -> RedirectResponse:
    ledger = _ledger(request)
    try:
        ledger.resolve_flag(flag_id, note)
    except LedgerError as exc:
        return _redirect("/flags", flash=str(exc), level="error")
    return _redirect("/flags", flash="Flag resolved")


@router.get("/settings", response_class=HTMLResponse)
async def settings_get(request: Request) -> HTMLResponse:
    config = _config(request)
    templates = _templates(request)
    return templates.TemplateResponse(
        request,
        "settings.html",
        {"request": request, "config": config, "error": None, **_flash(request)},
    )


@router.post("/settings")
async def settings_post(
    request: Request,
    stale_after_minutes: Annotated[int, Form()],
    reconcile_policy: Annotated[str, Form()],
    web_host: Annotated[str, Form()],
    web_port: Annotated[int, Form()],
    autostart_pipelines: Annotated[str | None, Form()] = None,
) -> HTMLResponse:
    config = _config(request)
    templates = _templates(request)
    autostart = autostart_pipelines == "on"
    if reconcile_policy not in ("retry", "fail"):
        return templates.TemplateResponse(
            request,
            "settings.html",
            {
                "request": request,
                "config": config,
                "error": "reconcile_policy must be retry or fail",
                **_flash(request),
            },
            status_code=200,
        )
    if stale_after_minutes < 1:
        return templates.TemplateResponse(
            request,
            "settings.html",
            {
                "request": request,
                "config": config,
                "error": "stale_after_minutes must be at least 1",
                **_flash(request),
            },
            status_code=200,
        )
    if config.config_file is None:
        return templates.TemplateResponse(
            request,
            "settings.html",
            {
                "request": request,
                "config": config,
                "error": "No config file on disk; create one with conveyor init",
                **_flash(request),
            },
            status_code=200,
        )
    try:
        save_web_runner_settings(
            config.config_file,
            stale_after_minutes=stale_after_minutes,
            reconcile_policy=reconcile_policy,
            web_host=web_host,
            web_port=web_port,
            autostart_pipelines=autostart,
        )
    except ConfigError as exc:
        return templates.TemplateResponse(
            request,
            "settings.html",
            {
                "request": request,
                "config": config,
                "error": str(exc),
                **_flash(request),
            },
            status_code=200,
        )
    updated = load_config(config.config_file)
    request.app.state.config = updated
    return templates.TemplateResponse(
        request,
        "settings.html",
        {
            "request": request,
            "config": updated,
            "error": None,
            "saved": True,
            **_flash(request),
        },
    )
