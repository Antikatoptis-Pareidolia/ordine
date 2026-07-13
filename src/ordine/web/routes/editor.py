"""Pipeline editor, version history, diff, and revert routes.

Owns editor HTTP flow only. Must never implement pipeline execution.
"""

from __future__ import annotations

import difflib
import logging
from collections.abc import Mapping
from typing import Annotated, Any, Literal, cast
from urllib.parse import quote

from fastapi import APIRouter, Form, HTTPException, Query, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from starlette import status

from ordine.core.config import AppConfig
from ordine.core.errors import FieldError, PlaybookSyntaxError, PlaybookValidationError
from ordine.core.ledger import Ledger, VersionInfo
from ordine.core.playbook import Playbook, dump_playbook, loads_playbook
from ordine.core.registry import StepRegistry
from ordine.llm.client import build_client
from ordine.llm.errors import LLMError, LLMNotConfiguredError
from ordine.llm.features.drafting import draft_playbook
from ordine.web.diffing import ChangeItem, side_by_side_rows, summarize_playbook_changes
from ordine.web.forms import (
    branch_step_indices,
    onfail_branch_indices,
    playbook_to_form,
    validate_editor_content,
)
from ordine.web.routes.tasks import BranchSuggestionStore
from ordine.web.services import ServiceManager
from ordine.web.views import version_label

logger = logging.getLogger(__name__)
router = APIRouter()

STARTER_YAML = """\
# Ordine pipeline starter — comments are not preserved on save.
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
    templates = Jinja2Templates(directory=str(request.app.state.templates_dir))
    templates.env.globals["onfail_branch_indices"] = onfail_branch_indices
    templates.env.globals["branch_step_indices"] = branch_step_indices
    return templates


def _rows_base_url(pipeline_id: int | None) -> str:
    if pipeline_id is None:
        return "/pipelines/new/edit/rows"
    return f"/pipelines/{pipeline_id}/edit/rows"


def _delete_prefix_keys(data: dict[str, str], prefix: str) -> None:
    for key in list(data):
        if key.startswith(prefix):
            del data[key]


def _engine_mismatch(registry: StepRegistry, engine: str) -> set[str]:
    return {
        step_id
        for step_id, engines, _origin in registry.list_step_metadata()
        if engine not in engines
    }


def _onfail_scope_for_prefix(prefix: str) -> tuple[str, str]:
    if prefix.startswith("steps-"):
        step_index = prefix.removeprefix("steps-").removesuffix("-onfail")
        return "step", step_index
    return "pipeline", ""


def _onfail_error_prefix(prefix: str) -> str:
    if prefix.startswith("steps-"):
        step_index = prefix.removeprefix("steps-").removesuffix("-onfail")
        return f"steps.{step_index}.on_failure"
    return "on_failure"


def _onfail_branches_context(
    request: Request,
    form_fields: dict[str, str],
    *,
    prefix: str,
    branches_target_id: str,
    pipeline_id: int | None,
    errors_by_path: dict[str, str] | None = None,
) -> dict[str, Any]:
    onfail_scope, step_index = _onfail_scope_for_prefix(prefix)
    registry = _registry(request)
    engine = form_fields.get("engine", "headless")
    return {
        "request": request,
        "form_fields": form_fields,
        "prefix": prefix,
        "error_prefix": _onfail_error_prefix(prefix),
        "branches_target_id": branches_target_id,
        "onfail_scope": onfail_scope,
        "step_index": step_index,
        "rows_base_url": _rows_base_url(pipeline_id),
        "step_ids": registry.ids(),
        "engine_mismatch": _engine_mismatch(registry, engine),
        "errors_by_path": errors_by_path or {},
        "pipeline_id": pipeline_id,
    }


def _branch_steps_context(
    request: Request,
    form_fields: dict[str, str],
    *,
    prefix: str,
    branch_key: str,
    branch_idx: str,
    pipeline_id: int | None,
    errors_by_path: dict[str, str] | None = None,
) -> dict[str, Any]:
    onfail_scope, step_index = _onfail_scope_for_prefix(prefix)
    registry = _registry(request)
    engine = form_fields.get("engine", "headless")
    return {
        "request": request,
        "form_fields": form_fields,
        "prefix": prefix,
        "error_prefix": _onfail_error_prefix(prefix),
        "branch_key": branch_key,
        "branch_idx": branch_idx,
        "branch_step_indices": branch_step_indices(form_fields, branch_key),
        "onfail_scope": onfail_scope,
        "step_index": step_index,
        "rows_url": _rows_base_url(pipeline_id),
        "add_branch_step_action": f"add-{onfail_scope}-onfail-branch-step",
        "remove_branch_step_action": f"remove-{onfail_scope}-onfail-branch-step",
        "step_ids": registry.ids(),
        "engine_mismatch": _engine_mismatch(registry, engine),
        "errors_by_path": errors_by_path or {},
        "pipeline_id": pipeline_id,
    }


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


def _canonical_yaml_for_diff(yaml_text: str) -> tuple[str, bool]:
    """Parse and re-dump YAML for semantic comparison; fall back to raw text on parse failure."""
    if not yaml_text.strip():
        return "", True
    try:
        return dump_playbook(loads_playbook(yaml_text, source="<diff>")), True
    except (PlaybookSyntaxError, PlaybookValidationError):
        return yaml_text, False


def _unified_diff_lines(
    against_yaml: str,
    version_yaml: str,
    *,
    against_id: str | None,
    version_id: str,
) -> tuple[list[str], bool, bool]:
    """Return (diff lines, formatting_normalized, metadata_only)."""
    left_text, left_ok = _canonical_yaml_for_diff(against_yaml)
    right_text, right_ok = _canonical_yaml_for_diff(version_yaml)
    formatting_normalized = left_ok and right_ok
    left_lines = left_text.splitlines()
    right_lines = right_text.splitlines()
    if left_lines == right_lines:
        return [], formatting_normalized, True
    diff_lines = list(
        difflib.unified_diff(
            left_lines,
            right_lines,
            fromfile=against_id or "(empty)",
            tofile=version_id,
            lineterm="",
        )
    )
    return diff_lines, formatting_normalized, False


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


def _version_notes(request: Request, pipeline_id: int) -> dict[str, VersionInfo]:
    return {row.public_id: row for row in _ledger(request).list_versions(pipeline_id)}


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
    lab_resume_banner: dict[str, str] | None = None,
    anchor: str | None = None,
    from_lab: str | None = None,
    save_error: str | None = None,
    version_notes: dict[str, VersionInfo] | None = None,
) -> dict[str, Any]:
    registry = _registry(request)
    engine = form_fields.get("engine", "headless")
    step_ids = registry.ids()
    engine_mismatch = {
        step_id
        for step_id, engines, _origin in registry.list_step_metadata()
        if engine not in engines
    }
    notes = version_notes or {}
    base_row = notes.get(base_version or "")
    current_row = notes.get(current_version or "") if current_version else None
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
        "lab_resume_banner": lab_resume_banner,
        "anchor": anchor,
        "from_lab": from_lab,
        "save_error": save_error,
        "rows_base_url": _rows_base_url(pipeline_id),
        "base_version_label": version_label(
            base_version or "",
            base_row.note if base_row else None,
            parent_id=base_row.parent_public_id if base_row else None,
        )
        if base_version
        else None,
        "current_version_label": version_label(
            current_version,
            current_row.note if current_row else None,
            parent_id=current_row.parent_public_id if current_row else None,
        )
        if current_version
        else None,
        "draft_url": (
            f"/pipelines/{pipeline_id}/ai/draft"
            if pipeline_id is not None
            else "/pipelines/new/ai/draft"
        ),
        "llm_configured": _llm_configured(request),
        **_flash(request),
    }


def _llm_configured(request: Request) -> bool:
    try:
        build_client(cast(AppConfig, request.app.state.config))
        return True
    except LLMNotConfiguredError:
        return False


def _branch_store(request: Request) -> BranchSuggestionStore:
    return cast(BranchSuggestionStore, request.app.state.branch_suggestions)


def _load_editor_from_version(
    request: Request,
    pipeline_id: int,
    version_public_id: str,
    *,
    anchor: str | None = None,
    from_lab: str | None = None,
) -> dict[str, Any]:
    ledger = _ledger(request)
    yaml_text = ledger.get_version_yaml(pipeline_id, version_public_id)
    playbook = loads_playbook(yaml_text)
    form_fields = playbook_to_form(playbook)
    form_fields["base_version"] = version_public_id
    form_fields["note"] = ""
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
        anchor=anchor,
        from_lab=from_lab,
        version_notes=_version_notes(request, pipeline_id),
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
    form_fields["note"] = ""
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
    anchor: Annotated[str | None, Query()] = None,
    from_lab: Annotated[str | None, Query()] = None,
    ai_branch_task: Annotated[int | None, Query()] = None,
) -> HTMLResponse:
    ledger = _ledger(request)
    _pipeline_summary(request, pipeline_id)
    if version is None:
        version, _yaml = ledger.get_current_playbook(pipeline_id)
    templates = _templates(request)
    context = _load_editor_from_version(
        request,
        pipeline_id,
        version,
        anchor=anchor,
        from_lab=from_lab,
    )
    if ai_branch_task is not None:
        pending = _branch_store(request).get(ai_branch_task)
        if pending is not None and pending.pipeline_id == pipeline_id:
            context["yaml_text"] = pending.suggestion.new_yaml
            context["tab"] = "yaml"
    return templates.TemplateResponse(request, "editor.html", context)


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
    updated = dict(form)
    templates = _templates(request)
    registry = _registry(request)

    if action == "add-step":
        idx = _next_index(_step_indices(updated))
        updated[f"steps-{idx}-id"] = ""
        updated[f"steps-{idx}-params"] = ""
        return templates.TemplateResponse(
            request,
            "partials/editor_steps.html",
            {
                "request": request,
                "form_fields": updated,
                "step_indices": _step_indices(updated),
                "step_ids": registry.ids(),
                "engine_mismatch": _engine_mismatch(registry, updated.get("engine", "headless")),
                "errors_by_path": _errors_by_path([]),
                "pipeline_id": pipeline_id,
                "rows_base_url": _rows_base_url(pipeline_id),
            },
        )

    if action == "remove-step":
        remove = form.get("row_index", "")
        if remove.isdigit():
            _delete_prefix_keys(updated, f"steps-{remove}-")
        return templates.TemplateResponse(
            request,
            "partials/editor_steps.html",
            {
                "request": request,
                "form_fields": updated,
                "step_indices": _step_indices(updated),
                "step_ids": registry.ids(),
                "engine_mismatch": _engine_mismatch(registry, updated.get("engine", "headless")),
                "errors_by_path": _errors_by_path([]),
                "pipeline_id": pipeline_id,
                "rows_base_url": _rows_base_url(pipeline_id),
            },
        )

    prefix = form.get("onfail_prefix") or (
        f"steps-{form.get('step_index', '')}-onfail"
        if action.startswith(("add-step", "remove-step"))
        else "onfail"
    )
    branches_target_id = form.get("branches_target_id") or (
        f"steps-{form.get('step_index', '')}-onfail-branches"
        if prefix.startswith("steps-")
        else "pipeline-onfail-branches"
    )

    if action in {"add-step-onfail-branch", "add-pipeline-onfail-branch"}:
        if prefix.startswith("steps-"):
            step_index = prefix.removeprefix("steps-").removesuffix("-onfail")
            updated[f"steps-{step_index}-onfail-enabled"] = "on"
        else:
            updated["onfail-enabled"] = "on"
        branch_indices = onfail_branch_indices(updated, prefix)
        new_branch_index = _next_index(branch_indices)
        branch_key = f"{prefix}-branches-{new_branch_index}"
        updated[f"{branch_key}-name"] = ""
        updated[f"{branch_key}-retries"] = "0"
        return templates.TemplateResponse(
            request,
            "partials/onfail_branches.html",
            _onfail_branches_context(
                request,
                updated,
                prefix=prefix,
                branches_target_id=branches_target_id,
                pipeline_id=pipeline_id,
            ),
        )

    if action in {"remove-step-onfail-branch", "remove-pipeline-onfail-branch"}:
        branch_index = form.get("branch_index", "")
        if branch_index.isdigit():
            _delete_prefix_keys(updated, f"{prefix}-branches-{branch_index}-")
        return templates.TemplateResponse(
            request,
            "partials/onfail_branches.html",
            _onfail_branches_context(
                request,
                updated,
                prefix=prefix,
                branches_target_id=branches_target_id,
                pipeline_id=pipeline_id,
            ),
        )

    if action in {"add-step-onfail-branch-step", "add-pipeline-onfail-branch-step"}:
        branch_key = form.get("branch_key") or f"{prefix}-branches-{form.get('branch_index', '')}"
        step_indices = branch_step_indices(updated, branch_key)
        new_step_index = _next_index(step_indices)
        updated[f"{branch_key}-steps-{new_step_index}-id"] = ""
        updated[f"{branch_key}-steps-{new_step_index}-params"] = ""
        return templates.TemplateResponse(
            request,
            "partials/branch_steps.html",
            _branch_steps_context(
                request,
                updated,
                prefix=prefix,
                branch_key=branch_key,
                branch_idx=form.get("branch_index", ""),
                pipeline_id=pipeline_id,
            ),
        )

    if action in {"remove-step-onfail-branch-step", "remove-pipeline-onfail-branch-step"}:
        branch_key = form.get("branch_key", "")
        branch_step_index = form.get("branch_step_index", "")
        if branch_key and branch_step_index.isdigit():
            _delete_prefix_keys(updated, f"{branch_key}-steps-{branch_step_index}-")
        return templates.TemplateResponse(
            request,
            "partials/branch_steps.html",
            _branch_steps_context(
                request,
                updated,
                prefix=prefix,
                branch_key=branch_key,
                branch_idx=form.get("branch_index", ""),
                pipeline_id=pipeline_id,
            ),
        )

    return templates.TemplateResponse(
        request,
        "partials/editor_steps.html",
        {
            "request": request,
            "form_fields": updated,
            "step_indices": _step_indices(updated),
            "step_ids": registry.ids(),
            "engine_mismatch": _engine_mismatch(registry, updated.get("engine", "headless")),
            "errors_by_path": _errors_by_path([]),
            "pipeline_id": pipeline_id,
            "rows_base_url": _rows_base_url(pipeline_id),
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
    from_lab = (form.get("from_lab") or "").strip() or None
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
                from_lab=from_lab,
                version_notes=_version_notes(request, pipeline_id),
            ),
        )
    make_current = base_version == current_version and not from_lab
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
            flash=f"Saved {version_label(saved_id, note)}",
        )
    notes = _version_notes(request, pipeline_id)
    base_row = notes.get(base_version)
    current_row = notes.get(current_version or "")
    branch_banner = {
        "saved": saved_id,
        "saved_note": note,
        "base": base_version,
        "base_note": base_row.note if base_row else None,
        "current": current_version,
        "current_note": current_row.note if current_row else None,
    }
    ctx = _load_editor_from_version(request, pipeline_id, saved_id, from_lab=from_lab)
    ctx["branch_banner"] = branch_banner
    if from_lab:
        ctx["lab_resume_banner"] = {
            "sid": from_lab,
            "version_id": saved_id,
            "note": note,
            "parent_id": base_version,
        }
    ctx["form_fields"]["base_version"] = saved_id
    return templates.TemplateResponse(request, "editor.html", ctx)


@router.get("/pipelines/{pipeline_id}/versions", response_class=HTMLResponse)
async def version_history(request: Request, pipeline_id: int) -> HTMLResponse:
    ledger = _ledger(request)
    name, current_version = _pipeline_summary(request, pipeline_id)
    versions = ledger.list_versions(pipeline_id)
    running_version = _services(request).running_version(pipeline_id)
    version_rows = {row.public_id: row for row in versions}
    current_row = version_rows.get(current_version) if current_version else None
    running_row = version_rows.get(running_version) if running_version else None
    templates = _templates(request)
    return templates.TemplateResponse(
        request,
        "versions.html",
        {
            "request": request,
            "pipeline_id": pipeline_id,
            "pipeline_name": name,
            "current_version": current_version,
            "current_version_note": current_row.note if current_row else None,
            "running_version": running_version,
            "running_version_note": running_row.note if running_row else None,
            "rows": _version_tree_rows(versions),
            **_flash(request),
        },
    )


def _version_meta(row: VersionInfo | None) -> dict[str, str | None]:
    if row is None:
        return {
            "id": "(empty)",
            "note": None,
            "parent": None,
            "created_at": None,
        }
    return {
        "id": row.public_id,
        "note": row.note,
        "parent": row.parent_public_id,
        "created_at": str(row.created_at),
    }


@router.get("/pipelines/{pipeline_id}/versions/{version_id}/diff", response_class=HTMLResponse)
async def version_diff(
    request: Request,
    pipeline_id: int,
    version_id: str,
    against: Annotated[str | None, Query()] = None,
    view: Annotated[str | None, Query()] = None,
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
    diff_lines, formatting_normalized, metadata_only = _unified_diff_lines(
        against_yaml,
        version_yaml,
        against_id=against_id,
        version_id=version_id,
    )
    change_items: list[ChangeItem] = []
    if formatting_normalized and not metadata_only:
        old_playbook = loads_playbook(against_yaml, source="<diff>")
        new_playbook = loads_playbook(version_yaml, source="<diff>")
        change_items = summarize_playbook_changes(old_playbook, new_playbook)
    left_text, _left_ok = _canonical_yaml_for_diff(against_yaml)
    right_text, _right_ok = _canonical_yaml_for_diff(version_yaml)
    diff_view = "unified" if view == "unified" else "side-by-side"
    templates = _templates(request)
    return templates.TemplateResponse(
        request,
        "diff.html",
        {
            "request": request,
            "pipeline_id": pipeline_id,
            "pipeline_name": name,
            "against": _version_meta(versions.get(against_id) if against_id else None),
            "version": _version_meta(version_row),
            "against_query": against_id,
            "change_items": change_items,
            "diff_lines": diff_lines,
            "side_by_side_rows": side_by_side_rows(
                left_text.splitlines(),
                right_text.splitlines(),
            ),
            "diff_view": diff_view,
            "formatting_normalized": formatting_normalized,
            "metadata_only": metadata_only,
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


async def _ai_draft_impl(
    request: Request,
    *,
    pipeline_id: int | None,
    description: str,
    draft_url: str,
) -> HTMLResponse:
    templates = _templates(request)
    try:
        client = build_client(cast(AppConfig, request.app.state.config))
    except LLMNotConfiguredError:
        return templates.TemplateResponse(
            request,
            "partials/ai_draft_form.html",
            {"request": request, "llm_configured": False, "draft_url": draft_url},
        )
    current_yaml = None
    if pipeline_id is not None:
        _version, current_yaml = _ledger(request).get_current_playbook(pipeline_id)
    try:
        result = draft_playbook(
            client,
            _registry(request),
            description,
            current_yaml=current_yaml,
        )
    except LLMError as exc:
        return templates.TemplateResponse(
            request,
            "partials/ai_draft_form.html",
            {
                "request": request,
                "llm_configured": True,
                "draft_url": draft_url,
                "description": description,
                "error": str(exc),
            },
        )
    return templates.TemplateResponse(
        request,
        "partials/ai_draft_result.html",
        {
            "request": request,
            "yaml_text": result.yaml_text,
            "problems": result.problems,
            "repaired": result.repaired,
            "valid": result.playbook is not None,
            "draft_url": draft_url,
        },
    )


@router.post("/pipelines/new/ai/draft", response_class=HTMLResponse)
async def ai_draft_new(
    request: Request,
    description: Annotated[str, Form()],
) -> HTMLResponse:
    return await _ai_draft_impl(
        request,
        pipeline_id=None,
        description=description,
        draft_url="/pipelines/new/ai/draft",
    )


@router.post("/pipelines/{pipeline_id}/ai/draft", response_class=HTMLResponse)
async def ai_draft_existing(
    request: Request,
    pipeline_id: int,
    description: Annotated[str, Form()],
) -> HTMLResponse:
    return await _ai_draft_impl(
        request,
        pipeline_id=pipeline_id,
        description=description,
        draft_url=f"/pipelines/{pipeline_id}/ai/draft",
    )
