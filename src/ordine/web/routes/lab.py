"""Dry-run lab web routes and session store.

Owns HTTP for sandbox rehearsals. Must never execute steps directly — delegates to DryRunSession.
"""

from __future__ import annotations

import logging
import mimetypes
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Annotated, Any, cast
from urllib.parse import quote

from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.responses import FileResponse, HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from starlette import status

from ordine.core.dryrun import (
    DryRunSession,
    lab_ordinal_warnings,
    playbook_contains_shell_run,
    redirect_output_dirs,
)
from ordine.core.errors import LedgerError, RunnerError
from ordine.core.ledger import Ledger
from ordine.core.playbook import loads_playbook
from ordine.core.registry import StepRegistry
from ordine.web.security import resolve_artifact

logger = logging.getLogger(__name__)
router = APIRouter()

IMAGE_SUFFIXES = frozenset({".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp"})


@dataclass
class LabSessionRecord:
    """Active lab session bound to a production pipeline."""

    sid: str
    pipeline_id: int
    session: DryRunSession


class LabSessionStore:
    """In-memory lab sessions; one active session per pipeline."""

    def __init__(self) -> None:
        self._by_id: dict[str, LabSessionRecord] = {}
        self._by_pipeline: dict[int, str] = {}
        self._locks: dict[str, threading.Lock] = {}

    def lock_for(self, sid: str) -> threading.Lock:
        if sid not in self._locks:
            self._locks[sid] = threading.Lock()
        return self._locks[sid]

    def put(self, record: LabSessionRecord) -> None:
        if record.pipeline_id in self._by_pipeline:
            self.close(self._by_pipeline[record.pipeline_id])
        self._by_id[record.sid] = record
        self._by_pipeline[record.pipeline_id] = record.sid

    def get(self, sid: str) -> LabSessionRecord | None:
        return self._by_id.get(sid)

    def close(self, sid: str) -> None:
        record = self._by_id.pop(sid, None)
        self._locks.pop(sid, None)
        if record is None:
            return
        self._by_pipeline.pop(record.pipeline_id, None)
        record.session.close()

    def close_all(self) -> None:
        for sid in list(self._by_id):
            self.close(sid)


def _templates(request: Request) -> Jinja2Templates:
    return Jinja2Templates(directory=str(request.app.state.templates_dir))


def _ledger(request: Request) -> Ledger:
    return cast(Ledger, request.app.state.ledger)


def _registry(request: Request) -> StepRegistry:
    return cast(StepRegistry, request.app.state.registry)


def _store(request: Request) -> LabSessionStore:
    return cast(LabSessionStore, request.app.state.lab_sessions)


def _sandbox_root(request: Request) -> Path:
    return Path(request.app.state.config.workdir_root) / "lab"


def _pipeline_name(request: Request, pipeline_id: int) -> str:
    for summary in _ledger(request).list_pipelines():
        if summary.id == pipeline_id:
            return summary.name
    raise HTTPException(status_code=404, detail="pipeline not found")


def _record_or_404(request: Request, sid: str) -> LabSessionRecord:
    record = _store(request).get(sid)
    if record is None:
        raise HTTPException(status_code=404, detail="lab session not found")
    return record


def _redirect(path: str, *, flash: str | None = None, level: str = "info") -> RedirectResponse:
    if flash:
        separator = "&" if "?" in path else "?"
        return RedirectResponse(
            f"{path}{separator}flash={quote(flash)}&flash_level={quote(level)}",
            status_code=status.HTTP_303_SEE_OTHER,
        )
    return RedirectResponse(path, status_code=status.HTTP_303_SEE_OTHER)


def _artifact_rel(sandbox: Path, path: Path | None) -> str | None:
    if path is None:
        return None
    try:
        return str(path.resolve().relative_to(sandbox.resolve()))
    except ValueError:
        return None


def _log_tail(artifact: Path | None, *, limit: int = 40) -> str:
    if artifact is None:
        return ""
    log_path = artifact.parent / "log.txt"
    if not log_path.is_file():
        return ""
    lines = log_path.read_text(encoding="utf-8", errors="replace").splitlines()
    return "\n".join(lines[-limit:])


def _version_note(request: Request, pipeline_id: int, version_id: str) -> str | None:
    for row in _ledger(request).list_versions(pipeline_id):
        if row.public_id == version_id:
            return row.note
    return None


def _session_context(
    request: Request,
    record: LabSessionRecord,
    *,
    task_ix: int = 0,
) -> dict[str, Any]:
    session = record.session
    steps = session.steps(task_ix)
    failed_ix = next(
        (index for index, step in enumerate(steps) if step.status == "fail"),
        None,
    )
    fix_anchor = f"steps-{failed_ix}" if failed_ix is not None else None
    current_step = steps[failed_ix] if failed_ix is not None else (steps[-1] if steps else None)
    artifact = None
    if current_step is not None:
        artifact = current_step.output_artifact or current_step.input_artifact
    task_summaries = session.tasks()
    task_status = task_summaries[task_ix].status if task_ix < len(task_summaries) else "pending"
    task_terminal = task_status in ("done", "skipped", "failed")
    return {
        "request": request,
        "sid": record.sid,
        "pipeline_id": record.pipeline_id,
        "pipeline_name": _pipeline_name(request, record.pipeline_id),
        "version_id": session.version_public_id,
        "version_note": _version_note(request, record.pipeline_id, session.version_public_id),
        "tasks": task_summaries,
        "task_ix": task_ix,
        "task_status": task_status,
        "task_terminal": task_terminal,
        "steps": steps,
        "failed_ix": failed_ix,
        "fix_anchor": fix_anchor,
        "current_step": current_step,
        "input_rel": _artifact_rel(
            session.sandbox, current_step.input_artifact if current_step else None
        ),
        "output_rel": _artifact_rel(
            session.sandbox, current_step.output_artifact if current_step else None
        ),
        "log_tail": _log_tail(artifact),
        "output_redirections": session.output_redirections,
        "sandbox": session.sandbox,
        "flash": request.query_params.get("flash"),
        "flash_level": request.query_params.get("flash_level", "info"),
    }


@router.get("/pipelines/{pipeline_id}/lab", response_class=HTMLResponse)
async def lab_setup(request: Request, pipeline_id: int) -> HTMLResponse:
    ledger = _ledger(request)
    registry = _registry(request)
    try:
        version_id, yaml_text = ledger.get_current_playbook(pipeline_id)
    except LedgerError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    playbook = loads_playbook(yaml_text)
    versions = ledger.list_versions(pipeline_id)
    sandbox_preview = _sandbox_root(request) / "preview"
    _, output_redirections = redirect_output_dirs(playbook, registry, sandbox_preview)
    version_notes = {row.public_id: row for row in versions}
    current_row = version_notes.get(version_id)
    templates = _templates(request)
    return templates.TemplateResponse(
        request,
        "lab_setup.html",
        {
            "request": request,
            "pipeline_id": pipeline_id,
            "pipeline_name": _pipeline_name(request, pipeline_id),
            "current_version": version_id,
            "current_version_note": current_row.note if current_row else None,
            "versions": versions,
            "shell_warning": playbook_contains_shell_run(playbook),
            "ordinal_warnings": lab_ordinal_warnings(playbook),
            "output_redirections": output_redirections,
            "flash": request.query_params.get("flash"),
            "flash_level": request.query_params.get("flash_level", "info"),
        },
    )


@router.post("/pipelines/{pipeline_id}/lab")
async def lab_create(request: Request, pipeline_id: int) -> RedirectResponse:
    form = await request.form()
    sample_dir = Path(str(form.get("sample_dir", ""))).expanduser()
    glob_pattern = str(form.get("glob", "*"))
    version_id = str(form.get("version_id", "")).strip()
    max_samples = int(str(form.get("max_samples", "20")))
    ledger = _ledger(request)
    if not version_id:
        version_id, yaml_text = ledger.get_current_playbook(pipeline_id)
    else:
        yaml_text = ledger.get_version_yaml(pipeline_id, version_id)
    playbook = loads_playbook(yaml_text)
    registry = _registry(request)
    engines = request.app.state.engines
    sandbox_root = _sandbox_root(request)
    sandbox_root.mkdir(parents=True, exist_ok=True)
    try:
        session = DryRunSession.create(
            playbook=playbook,
            version_public_id=version_id,
            sample_dir=sample_dir,
            glob=glob_pattern,
            registry=registry,
            engines=engines,
            sandbox_root=sandbox_root,
            yaml_text=yaml_text,
            max_samples=max_samples,
        )
    except (RunnerError, OSError) as exc:
        return _redirect(
            f"/pipelines/{pipeline_id}/lab",
            flash=str(exc),
            level="error",
        )
    record = LabSessionRecord(sid=session.session_id, pipeline_id=pipeline_id, session=session)
    _store(request).put(record)
    return _redirect(f"/lab/{session.session_id}")


@router.get("/lab/{sid}", response_class=HTMLResponse)
async def lab_session(
    request: Request,
    sid: str,
    task_ix: Annotated[int, Query()] = 0,
) -> HTMLResponse:
    record = _record_or_404(request, sid)
    templates = _templates(request)
    return templates.TemplateResponse(
        request,
        "lab_session.html",
        _session_context(request, record, task_ix=task_ix),
    )


@router.post("/lab/{sid}/tasks/{task_ix}/next")
async def lab_next(request: Request, sid: str, task_ix: int) -> RedirectResponse:
    record = _record_or_404(request, sid)
    with _store(request).lock_for(sid):
        try:
            record.session.run_next_step(task_ix)
        except RunnerError as exc:
            return _redirect(f"/lab/{sid}?task_ix={task_ix}", flash=str(exc), level="error")
    return _redirect(f"/lab/{sid}?task_ix={task_ix}")


@router.post("/lab/{sid}/tasks/{task_ix}/retry")
async def lab_retry(request: Request, sid: str, task_ix: int) -> RedirectResponse:
    record = _record_or_404(request, sid)
    with _store(request).lock_for(sid):
        try:
            record.session.retry_step(task_ix)
        except RunnerError as exc:
            return _redirect(f"/lab/{sid}?task_ix={task_ix}", flash=str(exc), level="error")
    return _redirect(f"/lab/{sid}?task_ix={task_ix}")


@router.post("/lab/{sid}/tasks/{task_ix}/branches")
async def lab_branches(request: Request, sid: str, task_ix: int) -> RedirectResponse:
    record = _record_or_404(request, sid)
    with _store(request).lock_for(sid):
        try:
            record.session.run_branches(task_ix)
        except RunnerError as exc:
            return _redirect(f"/lab/{sid}?task_ix={task_ix}", flash=str(exc), level="error")
    return _redirect(f"/lab/{sid}?task_ix={task_ix}")


@router.post("/lab/{sid}/tasks/{task_ix}/to-end")
async def lab_to_end(request: Request, sid: str, task_ix: int) -> RedirectResponse:
    record = _record_or_404(request, sid)
    with _store(request).lock_for(sid):
        record.session.run_to_end(task_ix)
    return _redirect(f"/lab/{sid}?task_ix={task_ix}")


@router.post("/lab/{sid}/run-all")
async def lab_run_all(request: Request, sid: str) -> RedirectResponse:
    record = _record_or_404(request, sid)
    with _store(request).lock_for(sid):
        record.session.run_all()
    return _redirect(f"/lab/{sid}")


@router.post("/lab/{sid}/resume")
async def lab_resume(request: Request, sid: str) -> RedirectResponse:
    record = _record_or_404(request, sid)
    form = await request.form()
    version_id = str(form.get("version_id", "")).strip()
    if not version_id:
        raise HTTPException(status_code=400, detail="version_id required")
    ledger = _ledger(request)
    yaml_text = ledger.get_version_yaml(record.pipeline_id, version_id)
    new_playbook = loads_playbook(yaml_text)
    with _store(request).lock_for(sid):
        old_session = record.session
        resumed = DryRunSession.resume(
            old_session,
            new_playbook,
            version_id,
            yaml_text=yaml_text,
        )
        _store(request).close(sid)
        new_record = LabSessionRecord(
            sid=resumed.session_id,
            pipeline_id=record.pipeline_id,
            session=resumed,
        )
        _store(request).put(new_record)
        old_session.close()
    return _redirect(f"/lab/{resumed.session_id}", flash=f"Resumed lab on {version_id}")


@router.post("/lab/{sid}/close")
async def lab_close(request: Request, sid: str) -> RedirectResponse:
    record = _record_or_404(request, sid)
    pipeline_id = record.pipeline_id
    _store(request).close(sid)
    return _redirect(f"/pipelines/{pipeline_id}/lab", flash="Lab session closed")


@router.get("/lab/{sid}/artifacts/{rel_path:path}")
async def lab_artifact(sid: str, rel_path: str, request: Request) -> FileResponse:
    record = _record_or_404(request, sid)
    resolved = resolve_artifact(record.session.sandbox, rel_path)
    if resolved is None:
        raise HTTPException(status_code=404, detail="not found")
    suffix = resolved.suffix.lower()
    if suffix == ".txt" or resolved.name == "log.txt":
        return FileResponse(resolved, media_type="text/plain")
    if suffix in IMAGE_SUFFIXES:
        return FileResponse(resolved)
    media_type = mimetypes.guess_type(resolved.name)[0] or "application/octet-stream"
    return FileResponse(resolved, media_type=media_type, filename=resolved.name)
