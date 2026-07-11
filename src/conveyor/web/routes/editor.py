"""Pipeline editor, version history, diff, and revert routes.

Owns editor HTTP flow only. Must never implement pipeline execution.
"""

from __future__ import annotations

import difflib
import logging
from collections.abc import Mapping
from typing import Annotated, Any, Literal, cast
from urllib.parse import quote

from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from starlette import status

from conveyor.core.errors import FieldError
from conveyor.core.ledger import Ledger, VersionInfo
from conveyor.core.playbook import Playbook, dump_playbook, loads_playbook
from conveyor.core.registry import StepRegistry
from conveyor.web.forms import playbook_to_form, validate_editor_content
from conveyor.web.services import ServiceManager

logger = logging.getLogger(__name__)
router = APIRouter()

STARTER_YAML = """\
# Conveyor pipeline starter — comments are not preserved on save.
version: 1
name: my-pipeline
trigger:
  type: folder_watch
  path: ~/input
  glob: "*.png"
  settle_seconds: 2
engine: headless
steps:
  - image.white_to_alpha:
      fuzz: 8
  - image.trim
  - image.export:
      dest: ~/output
      format: png
"""


def _templates(request: Request) -> Jinja2Templates:
    return Jinja2Templates(directory=str(request.app.state.templates_dir))


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
        separator = "&" if "?" in path else "?"
        return RedirectResponse(
            f"{path}{separator}flash={quote(flash)}&flash_level={quote(level)}",
            status_code=status.HTTP_303_SEE_OTHER,
        )
    return RedirectResponse(path, status_code=status.HTTP_303_SEE_OTHER)


async def _form_mapping(request: Request) -> dict[str, str]:
    raw = await request.form()
    return {key: value for key, value in raw.multi_items() if isinstance(value, str)}


def _errors_by_path(errors: list[FieldError]) -> dict[str, str]:
    return {error.path: error.message for error in errors}


def _step_indices(form: Mapping[str, str]) -> list[int]:
    indices: set[int] = set()
    for key in form:
        if key.startswith("steps-") and key.endswith("-id"):
            middle = key.removeprefix("steps-").removesuffix("-id")
            if middle.isdigit():
                indices.add(int(middle))
    return sorted(indices)


def _next_index(indices: list[int]) -> int:
    return (indices[-1] + 1) if indices else 0


def _validate_content(
    registry: StepRegistry,
    *,
    tab: Literal["form", "yaml"],
    form: Mapping[str, str] | None = None,
    yaml_text: str | None = None,
) -> tuple[Playbook | None, str, list[FieldError]]:
    return validate_editor_content(registry, tab=tab, form=form, yaml_text=yaml_text)


def _editor_context(
    request: Request,
    *,
    pipeline_id: int | None,
    tab: Literal["form", "yaml"],
    form_fields: dict[str, str],
    yaml_text: str,
    base_version: str | None,
    current_version: str | None,
    errors: list[FieldError],
    branch_banner: dict[str, str] | None = None,
    save_error: str | None = None,
) -> dict[str, Any]:
    registry = _registry(request)
    engine = form_fields.get("engine", "headless")
    step_ids = registry.ids()
    engine_mismatch = {
        step_id
        for step_id, engines, _origin in registry.list_step_metadata()
        if engine not in engines
    }
    return {
        "request": request,
        "pipeline_id": pipeline_id,
        "tab": tab,
        "form_fields": form_fields,
        "yaml_text": yaml_text,
        "base_version": base_version or "",
        "current_version": current_version,
        "step_indices": _step_indices(form_fields),
        "step_ids": step_ids,
        "engine_mismatch": engine_mismatch,
        "errors_by_path": _errors_by_path(errors),
        "errors": errors,
        "branch_banner": branch_banner,
        "save_error": save_error,
        **_flash(request),
    }


def _load_editor_from_version(
    request: Request,
    pipeline_id: int,
    version_public_id: str,
) -> dict[str, Any]:
    ledger = _ledger(request)
    yaml_text = ledger.get_version_yaml(pipeline_id, version_public_id)
    playbook = loads_playbook(yaml_text)
    form_fields = playbook_to_form(playbook)
    form_fields["base_version"] = version_public_id
    current_version, _ = ledger.get_current_playbook(pipeline_id)
    return _editor_context(
        request,
        pipeline_id=pipeline_id,
        tab="form",
        form_fields=form_fields,
        yaml_text=yaml_text,
        base_version=version_public_id,
        current_version=current_version,
        errors=[],
    )


def _version_tree_rows(versions: list[VersionInfo]) -> list[dict[str, Any]]:
    by_parent: dict[str | None, list[VersionInfo]] = {}
    known = {version.public_id for version in versions}
    for version in versions:
        by_parent.setdefault(version.parent_public_id, []).append(version)
    for siblings in by_parent.values():
        siblings.sort(key=lambda item: item.created_at)
    roots = [
        version
        for version in versions
        if version.parent_public_id is None or version.parent_public_id not in known
    ]
    roots.sort(key=lambda item: item.created_at)
    rows: list[dict[str, Any]] = []

    def walk(node: VersionInfo, depth: int) -> None:
        rows.append({"version": node, "depth": depth})
        for child in by_parent.get(node.public_id, []):
            walk(child, depth + 1)

    for root in roots:
        walk(root, 0)
    return rows


def _pipeline_summary(request: Request, pipeline_id: int) -> tuple[str, str | None]:
    ledger = _ledger(request)
    summaries = {summary.id: summary for summary in ledger.list_pipelines()}
    summary = summaries.get(pipeline_id)
    if summary is None:
        raise HTTPException(status_code=404, detail="pipeline not found")
    return summary.name, summary.current_version


@router.get("/pipelines/new", response_class=HTMLResponse)
async def editor_new(request: Request) -> HTMLResponse:
    playbook = loads_playbook(STARTER_YAML, source="<starter>")
    form_fields = playbook_to_form(playbook)
    form_fields["base_version"] = ""
    yaml_text = dump_playbook(playbook)
    templates = _templates(request)
    return templates.TemplateResponse(
        request,
        "editor.html",
        _editor_context(
            request,
            pipeline_id=None,
            tab="form",
            form_fields=form_fields,
            yaml_text=yaml_text,
            base_version=None,
            current_version=None,
            errors=[],
        ),
    )


@router.get("/pipelines/{pipeline_id}/edit", response_class=HTMLResponse)
async def editor_edit(
    request: Request,
    pipeline_id: int,
    version: Annotated[str | None, Query()] = None,
) -> HTMLResponse:
    ledger = _ledger(request)
    _pipeline_summary(request, pipeline_id)
    if version is None:
        version, _yaml = ledger.get_current_playbook(pipeline_id)
    templates = _templates(request)
    return templates.TemplateResponse(
        request,
        "editor.html",
        _load_editor_from_version(request, pipeline_id, version),
    )


@router.post("/pipelines/new/edit/validate", response_class=HTMLResponse)
async def editor_validate_new(request: Request) -> HTMLResponse:
    return await _editor_validate_impl(request, None)


@router.post("/pipelines/{pipeline_id}/edit/validate", response_class=HTMLResponse)
async def editor_validate_existing(request: Request, pipeline_id: int) -> HTMLResponse:
    return await _editor_validate_impl(request, pipeline_id)


async def _editor_validate_impl(request: Request, pipeline_id: int | None) -> HTMLResponse:
    form = await _form_mapping(request)
    tab = cast(Literal["form", "yaml"], form.get("tab", "form"))
    yaml_text = form.get("yaml_text", "")
    registry = _registry(request)
    _, canonical_yaml, errors = _validate_content(registry, tab=tab, form=form, yaml_text=yaml_text)
    current_version = None
    if pipeline_id is not None:
        _, current_version = _ledger(request).get_current_playbook(pipeline_id)
    templates = _templates(request)
    return templates.TemplateResponse(
        request,
        "editor.html",
        _editor_context(
            request,
            pipeline_id=pipeline_id,
            tab=tab,
            form_fields=form,
            yaml_text=canonical_yaml if not errors else yaml_text,
            base_version=form.get("base_version") or None,
            current_version=current_version,
            errors=errors,
        ),
    )


@router.post("/pipelines/new/edit/to-yaml", response_class=HTMLResponse)
async def editor_to_yaml_new(request: Request) -> HTMLResponse:
    return await _editor_to_yaml_impl(request, None)


@router.post("/pipelines/{pipeline_id}/edit/to-yaml", response_class=HTMLResponse)
async def editor_to_yaml_existing(request: Request, pipeline_id: int) -> HTMLResponse:
    return await _editor_to_yaml_impl(request, pipeline_id)


async def _editor_to_yaml_impl(request: Request, pipeline_id: int | None) -> HTMLResponse:
    form = await _form_mapping(request)
    registry = _registry(request)
    _, yaml_text, errors = _validate_content(registry, tab="form", form=form)
    current_version = None
    if pipeline_id is not None:
        _, current_version = _ledger(request).get_current_playbook(pipeline_id)
    templates = _templates(request)
    return templates.TemplateResponse(
        request,
        "editor.html",
        _editor_context(
            request,
            pipeline_id=pipeline_id,
            tab="yaml" if not errors else "form",
            form_fields=form,
            yaml_text=yaml_text,
            base_version=form.get("base_version") or None,
            current_version=current_version,
            errors=errors,
        ),
    )


@router.post("/pipelines/new/edit/to-form", response_class=HTMLResponse)
async def editor_to_form_new(request: Request) -> HTMLResponse:
    return await _editor_to_form_impl(request, None)


@router.post("/pipelines/{pipeline_id}/edit/to-form", response_class=HTMLResponse)
async def editor_to_form_existing(request: Request, pipeline_id: int) -> HTMLResponse:
    return await _editor_to_form_impl(request, pipeline_id)


async def _editor_to_form_impl(request: Request, pipeline_id: int | None) -> HTMLResponse:
    form = await _form_mapping(request)
    yaml_text = form.get("yaml_text", "")
    registry = _registry(request)
    playbook, canonical_yaml, errors = _validate_content(registry, tab="yaml", yaml_text=yaml_text)
    form_fields = form
    if playbook is not None and not errors:
        form_fields = playbook_to_form(playbook)
        form_fields["base_version"] = form.get("base_version", "")
        form_fields["note"] = form.get("note", "")
        yaml_text = canonical_yaml
    templates = _templates(request)
    return templates.TemplateResponse(
        request,
        "editor.html",
        _editor_context(
            request,
            pipeline_id=pipeline_id,
            tab="form" if not errors else "yaml",
            form_fields=form_fields,
            yaml_text=yaml_text if errors else canonical_yaml,
            base_version=form.get("base_version") or None,
            current_version=(
                _ledger(request).get_current_playbook(pipeline_id)[0]
                if pipeline_id is not None
                else None
            ),
            errors=errors,
        ),
    )


@router.post("/pipelines/new/edit/rows", response_class=HTMLResponse)
async def editor_rows_new(request: Request) -> HTMLResponse:
    return await _editor_rows_impl(request, None)


@router.post("/pipelines/{pipeline_id}/edit/rows", response_class=HTMLResponse)
async def editor_rows_existing(request: Request, pipeline_id: int) -> HTMLResponse:
    return await _editor_rows_impl(request, pipeline_id)


async def _editor_rows_impl(request: Request, pipeline_id: int | None) -> HTMLResponse:
    form = await _form_mapping(request)
    action = form.get("row_action", "")
    indices = _step_indices(form)
    updated = dict(form)
    if action == "add-step":
        idx = _next_index(indices)
        updated[f"steps-{idx}-id"] = ""
        updated[f"steps-{idx}-params"] = ""
    elif action == "remove-step":
        remove = form.get("row_index", "")
        if remove.isdigit():
            prefix = f"steps-{remove}-"
            for key in list(updated):
                if key.startswith(prefix):
                    del updated[key]
    templates = _templates(request)
    registry = _registry(request)
    engine = updated.get("engine", "headless")
    engine_mismatch = {
        step_id
        for step_id, engines, _origin in registry.list_step_metadata()
        if engine not in engines
    }
    return templates.TemplateResponse(
        request,
        "partials/editor_steps.html",
        {
            "request": request,
            "form_fields": updated,
            "step_indices": _step_indices(updated),
            "step_ids": registry.ids(),
            "engine_mismatch": engine_mismatch,
            "errors_by_path": _errors_by_path([]),
            "pipeline_id": pipeline_id,
        },
    )


@router.post("/pipelines/{pipeline_id}/versions", response_model=None)
async def save_version(
    request: Request,
    pipeline_id: int,
) -> HTMLResponse | RedirectResponse:
    form = await _form_mapping(request)
    tab = cast(Literal["form", "yaml"], form.get("tab", "form"))
    yaml_text = form.get("yaml_text", "")
    registry = _registry(request)
    ledger = _ledger(request)
    current_version, _ = ledger.get_current_playbook(pipeline_id)
    base_version = form.get("base_version") or current_version
    note = (form.get("note") or "").strip() or None
    playbook, canonical_yaml, errors = _validate_content(
        registry, tab=tab, form=form, yaml_text=yaml_text
    )
    templates = _templates(request)
    if errors or playbook is None:
        return templates.TemplateResponse(
            request,
            "editor.html",
            _editor_context(
                request,
                pipeline_id=pipeline_id,
                tab=tab,
                form_fields=form,
                yaml_text=yaml_text,
                base_version=base_version,
                current_version=current_version,
                errors=errors,
            ),
        )
    make_current = base_version == current_version
    _, saved_id = ledger.register_pipeline(
        playbook,
        canonical_yaml,
        note=note,
        parent_public_id=base_version,
        make_current=make_current,
    )
    if make_current:
        return _redirect(
            f"/pipelines/{pipeline_id}/edit?version={saved_id}",
            flash=f"Saved {saved_id}",
        )
    branch_banner = {
        "saved": saved_id,
        "base": base_version,
        "current": current_version,
    }
    ctx = _load_editor_from_version(request, pipeline_id, saved_id)
    ctx["branch_banner"] = branch_banner
    ctx["form_fields"]["base_version"] = saved_id
    return templates.TemplateResponse(request, "editor.html", ctx)


@router.get("/pipelines/{pipeline_id}/versions", response_class=HTMLResponse)
async def version_history(request: Request, pipeline_id: int) -> HTMLResponse:
    ledger = _ledger(request)
    name, current_version = _pipeline_summary(request, pipeline_id)
    versions = ledger.list_versions(pipeline_id)
    running_version = _services(request).running_version(pipeline_id)
    templates = _templates(request)
    return templates.TemplateResponse(
        request,
        "versions.html",
        {
            "request": request,
            "pipeline_id": pipeline_id,
            "pipeline_name": name,
            "current_version": current_version,
            "running_version": running_version,
            "rows": _version_tree_rows(versions),
            **_flash(request),
        },
    )


@router.get("/pipelines/{pipeline_id}/versions/{version_id}/diff", response_class=HTMLResponse)
async def version_diff(
    request: Request,
    pipeline_id: int,
    version_id: str,
    against: Annotated[str | None, Query()] = None,
) -> HTMLResponse:
    ledger = _ledger(request)
    name, _current = _pipeline_summary(request, pipeline_id)
    versions = {row.public_id: row for row in ledger.list_versions(pipeline_id)}
    if version_id not in versions:
        raise HTTPException(status_code=404, detail="version not found")
    version_row = versions[version_id]
    against_id = against or version_row.parent_public_id
    against_yaml = "" if against_id is None else ledger.get_version_yaml(pipeline_id, against_id)
    version_yaml = ledger.get_version_yaml(pipeline_id, version_id)
    diff_lines = list(
        difflib.unified_diff(
            against_yaml.splitlines(),
            version_yaml.splitlines(),
            fromfile=against_id or "(empty)",
            tofile=version_id,
            lineterm="",
        )
    )
    templates = _templates(request)
    return templates.TemplateResponse(
        request,
        "diff.html",
        {
            "request": request,
            "pipeline_id": pipeline_id,
            "pipeline_name": name,
            "version_id": version_id,
            "against_id": against_id,
            "diff_lines": diff_lines,
            **_flash(request),
        },
    )


@router.post("/pipelines/{pipeline_id}/versions/{version_id}/make-current")
async def make_current(request: Request, pipeline_id: int, version_id: str) -> RedirectResponse:
    ledger = _ledger(request)
    _pipeline_summary(request, pipeline_id)
    ledger.set_current_version(pipeline_id, version_id)
    flash = f"{version_id} is now current"
    if _services(request).running_version(pipeline_id) is not None:
        flash = f"{version_id} is now current — restart pipeline to apply"
    return _redirect(f"/pipelines/{pipeline_id}/versions", flash=flash)


@router.post("/pipelines/{pipeline_id}/versions/{version_id}/revert")
async def revert_version(request: Request, pipeline_id: int, version_id: str) -> RedirectResponse:
    ledger = _ledger(request)
    current_version, _ = ledger.get_current_playbook(pipeline_id)
    yaml_text = ledger.get_version_yaml(pipeline_id, version_id)
    playbook = loads_playbook(yaml_text)
    _, new_id = ledger.register_pipeline(
        playbook,
        yaml_text,
        note=f"revert to {version_id}",
        parent_public_id=current_version,
        make_current=True,
    )
    return _redirect(
        f"/pipelines/{pipeline_id}/versions",
        flash=f"Reverted to {version_id} as {new_id}",
    )
