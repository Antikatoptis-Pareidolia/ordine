"""Regression: form-tab save must not destroy recovery branches."""

from __future__ import annotations

import re
from html import unescape
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from ordine.core.config import load_config
from ordine.core.db import create_engine_for, init_db
from ordine.core.ledger import Ledger
from ordine.core.playbook import FailurePolicy, Playbook, loads_playbook
from ordine.web.app import create_app
from tests.test_web import POST_HEADERS, _write_config

BRANCHY_COMBINED_YAML = """\
version: 1
name: branchy-combined
description: original description
trigger:
  type: manual
  path: ~/input
steps:
  - id: image.white_to_alpha
    params:
      fuzz: 8
    on_failure:
      retries: 1
      branches:
        - name: retry-trim
          retries: 0
          steps:
            - image.trim
  - image.trim
on_failure:
  retries: 0
  branches:
    - name: branch-a
      retries: 2
      steps:
        - image.export:
            dest: ~/output-a
            format: png
  then: mark_failed
"""


def _parse_form_fields_from_html(html: str) -> dict[str, str]:
    """Extract name/value pairs the browser would submit from editor HTML."""
    fields: dict[str, str] = {}
    for match in re.finditer(r"<input\b[^>]*>", html):
        tag = match.group(0)
        name_match = re.search(r'\bname="([^"]+)"', tag)
        if name_match is None:
            continue
        name = name_match.group(1)
        value_match = re.search(r'\bvalue="([^"]*)"', tag)
        value = unescape(value_match.group(1)) if value_match else ""
        if 'type="checkbox"' in tag:
            if "checked" in tag:
                fields[name] = value or "on"
            continue
        if 'type="radio"' in tag:
            if "checked" in tag:
                fields[name] = value
            continue
        fields[name] = value
    for match in re.finditer(
        r'<textarea\b[^>]*name="([^"]+)"[^>]*>(.*?)</textarea>',
        html,
        re.DOTALL,
    ):
        fields[match.group(1)] = unescape(match.group(2).strip())
    for match in re.finditer(
        r'<select\b[^>]*name="([^"]+)"[^>]*>(.*?)</select>',
        html,
        re.DOTALL,
    ):
        body = match.group(2)
        selected = re.search(
            r'<option\b[^>]*value="([^"]*)"[^>]*\sselected',
            body,
        ) or re.search(r'<option\b[^>]*value="([^"]*)"[^>]*>\s*[^<]*\s*</option>', body)
        if selected:
            fields[match.group(1)] = unescape(selected.group(1))
    return fields


def _step_on_failure(playbook: Playbook, index: int) -> FailurePolicy | None:
    return playbook.steps[index].on_failure


@pytest.fixture
def branch_client(tmp_path: Path) -> tuple[TestClient, Ledger, Playbook]:
    config_file = _write_config(tmp_path)
    config = load_config(config_file)
    engine = create_engine_for(config.db_path)
    init_db(engine)
    ledger = Ledger(engine)
    client = TestClient(create_app(config))
    original = loads_playbook(BRANCHY_COMBINED_YAML)
    _pipeline_id, version_id = ledger.register_pipeline(original, BRANCHY_COMBINED_YAML)
    assert version_id == "pv_0001"
    return client, ledger, original


def test_form_save_preserves_recovery_branches(
    branch_client: tuple[TestClient, Ledger, Playbook],
) -> None:
    """GET /edit → change only description → save must keep all branch data."""
    client, ledger, original = branch_client
    pipeline_id = ledger.find_pipeline_id("branchy-combined")
    assert pipeline_id is not None
    v1, _ = ledger.get_current_playbook(pipeline_id)

    edit_page = client.get(f"/pipelines/{pipeline_id}/edit")
    assert edit_page.status_code == 200
    form = _parse_form_fields_from_html(edit_page.text)
    form["description"] = "updated description only"
    form["tab"] = "form"
    form["base_version"] = v1

    saved = client.post(
        f"/pipelines/{pipeline_id}/versions",
        data=form,
        headers=POST_HEADERS,
        follow_redirects=False,
    )
    assert saved.status_code == 303

    _, saved_yaml = ledger.get_current_playbook(pipeline_id)
    saved_playbook = loads_playbook(saved_yaml)
    assert saved_playbook.description == "updated description only"

    assert saved_playbook.on_failure == original.on_failure
    assert _step_on_failure(saved_playbook, 0) == _step_on_failure(original, 0)


def test_branch_fragment_add_remove_reindexes(
    branch_client: tuple[TestClient, Ledger, Playbook],
) -> None:
    client, ledger, _original = branch_client
    pipeline_id = ledger.find_pipeline_id("branchy-combined")
    assert pipeline_id is not None
    edit_page = client.get(f"/pipelines/{pipeline_id}/edit")
    form = _parse_form_fields_from_html(edit_page.text)

    add_branch = client.post(
        f"/pipelines/{pipeline_id}/edit/rows",
        data={
            **form,
            "row_action": "add-pipeline-onfail-branch",
            "onfail_prefix": "onfail",
            "branches_target_id": "pipeline-onfail-branches",
        },
        headers=POST_HEADERS,
    )
    assert add_branch.status_code == 200
    assert "onfail-branches-1-name" in add_branch.text

    updated = _parse_form_fields_from_html(add_branch.text)
    updated["row_action"] = "remove-pipeline-onfail-branch"
    updated["onfail_prefix"] = "onfail"
    updated["branches_target_id"] = "pipeline-onfail-branches"
    updated["branch_index"] = "1"
    removed = client.post(
        f"/pipelines/{pipeline_id}/edit/rows",
        data=updated,
        headers=POST_HEADERS,
    )
    assert removed.status_code == 200
    assert "onfail-branches-1-name" not in removed.text
    assert "onfail-branches-0-name" in removed.text

    form = _parse_form_fields_from_html(edit_page.text)
    add_step_branch = client.post(
        f"/pipelines/{pipeline_id}/edit/rows",
        data={
            **form,
            "row_action": "add-step-onfail-branch-step",
            "onfail_prefix": "steps-0-onfail",
            "branch_index": "0",
            "branch_key": "steps-0-onfail-branches-0",
        },
        headers=POST_HEADERS,
    )
    assert add_step_branch.status_code == 200
    assert "steps-0-onfail-branches-0-steps-1-id" in add_step_branch.text

    step_form = _parse_form_fields_from_html(add_step_branch.text)
    step_form["row_action"] = "remove-step-onfail-branch-step"
    step_form["onfail_prefix"] = "steps-0-onfail"
    step_form["branch_index"] = "0"
    step_form["branch_key"] = "steps-0-onfail-branches-0"
    step_form["branch_step_index"] = "1"
    removed_step = client.post(
        f"/pipelines/{pipeline_id}/edit/rows",
        data=step_form,
        headers=POST_HEADERS,
    )
    assert removed_step.status_code == 200
    assert "steps-0-onfail-branches-0-steps-1-id" not in removed_step.text
    assert "steps-0-onfail-branches-0-steps-0-id" in removed_step.text


def test_form_tab_branchy_round_trip_add_remove_branch(
    branch_client: tuple[TestClient, Ledger, Playbook],
) -> None:
    """Add a pipeline branch in the form tab, save, reopen, remove it, save again."""
    client, ledger, original = branch_client
    pipeline_id = ledger.find_pipeline_id("branchy-combined")
    assert pipeline_id is not None
    v1, _ = ledger.get_current_playbook(pipeline_id)

    edit_page = client.get(f"/pipelines/{pipeline_id}/edit")
    base_form = _parse_form_fields_from_html(edit_page.text)
    add_branch = client.post(
        f"/pipelines/{pipeline_id}/edit/rows",
        data={
            **base_form,
            "row_action": "add-pipeline-onfail-branch",
            "onfail_prefix": "onfail",
            "branches_target_id": "pipeline-onfail-branches",
        },
        headers=POST_HEADERS,
    )
    form = {**base_form, **_parse_form_fields_from_html(add_branch.text)}
    form["onfail-branches-1-name"] = "branch-c"
    form["onfail-branches-1-retries"] = "0"
    form["onfail-branches-1-steps-0-id"] = "image.trim"
    form["onfail-branches-1-steps-0-params"] = ""
    form["description"] = "added branch c"
    form["tab"] = "form"
    form["base_version"] = v1
    client.post(
        f"/pipelines/{pipeline_id}/versions",
        data=form,
        headers=POST_HEADERS,
        follow_redirects=False,
    )
    v2, _ = ledger.get_current_playbook(pipeline_id)
    saved = loads_playbook(ledger.get_version_yaml(pipeline_id, v2))
    assert len(saved.on_failure.branches) == 2
    assert saved.on_failure.branches[1].name == "branch-c"

    reopen = client.get(f"/pipelines/{pipeline_id}/edit")
    base_form = _parse_form_fields_from_html(reopen.text)
    removed = client.post(
        f"/pipelines/{pipeline_id}/edit/rows",
        data={
            **base_form,
            "row_action": "remove-pipeline-onfail-branch",
            "onfail_prefix": "onfail",
            "branches_target_id": "pipeline-onfail-branches",
            "branch_index": "1",
        },
        headers=POST_HEADERS,
    )
    assert removed.status_code == 200
    form = dict(base_form)
    for key in list(form):
        if key.startswith("onfail-branches-1-"):
            del form[key]
    form["tab"] = "form"
    form["base_version"] = v2
    client.post(
        f"/pipelines/{pipeline_id}/versions",
        data=form,
        headers=POST_HEADERS,
        follow_redirects=False,
    )
    _, final_yaml = ledger.get_current_playbook(pipeline_id)
    final = loads_playbook(final_yaml)
    assert final.on_failure == original.on_failure
    assert _step_on_failure(final, 0) == _step_on_failure(original, 0)
