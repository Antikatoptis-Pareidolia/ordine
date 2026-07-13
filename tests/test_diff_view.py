"""Structured diff summary and side-by-side diff view tests."""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from conveyor.core.config import load_config
from conveyor.core.db import create_engine_for, init_db
from conveyor.core.ledger import Ledger
from conveyor.core.playbook import Playbook, loads_playbook
from conveyor.web.app import create_app
from conveyor.web.diffing import side_by_side_rows, summarize_playbook_changes
from tests.test_branch_regression import _parse_form_fields_from_html
from tests.test_web import POST_HEADERS, _write_config

FLAGSHIP = Path("tests/fixtures/playbooks/valid/v02_flagship.yml")

COMPACT_STEP_YAML = """\
version: 1
name: compact-step
trigger:
  type: manual
  path: ~/input
steps:
  - image.white_to_alpha: {fuzz: 8}
"""

LONG_STEP_SAME_YAML = """\
version: 1
name: compact-step
trigger:
  type: manual
  path: ~/input
steps:
  - id: image.white_to_alpha
    params:
      fuzz: 8
"""

STEP_BRANCH_YAML = """\
version: 1
name: compact-step
trigger:
  type: manual
  path: ~/input
steps:
  - id: image.white_to_alpha
    params:
      fuzz: 8
    on_failure:
      branches:
        - name: pillow-fallback
          retries: 0
          steps:
            - image.trim
"""


@pytest.fixture
def diff_client(tmp_path: Path) -> tuple[TestClient, Ledger]:
    config_file = _write_config(tmp_path)
    config = load_config(config_file)
    engine = create_engine_for(config.db_path)
    init_db(engine)
    ledger = Ledger(engine)
    client = TestClient(create_app(config))
    return client, ledger


def _manual_playbook(
    *,
    name: str = "diff-test",
    path: str = "~/input",
    step_lines: list[str] | None = None,
    extra_yaml: str = "",
) -> Playbook:
    steps = step_lines or ["  - image.trim"]
    yaml_text = (
        f"version: 1\n"
        f"name: {name}\n"
        f"trigger:\n"
        f"  type: manual\n"
        f"  path: {path}\n"
        f"steps:\n"
        f"{chr(10).join(steps)}\n"
        f"{extra_yaml}"
    )
    return loads_playbook(yaml_text)


def test_serialization_form_change_produces_zero_items() -> None:
    old = loads_playbook(COMPACT_STEP_YAML)
    new = loads_playbook(LONG_STEP_SAME_YAML)
    assert summarize_playbook_changes(old, new) == []
    assert summarize_playbook_changes(new, old) == []


def test_branch_added_to_compact_step_produces_single_summary_item() -> None:
    old = loads_playbook(COMPACT_STEP_YAML)
    new = loads_playbook(STEP_BRANCH_YAML)
    changes = summarize_playbook_changes(old, new)
    assert len(changes) == 1
    item = changes[0]
    assert item.kind == "added"
    assert item.scope == "branch"
    assert "pillow-fallback" in item.location
    assert "image.white_to_alpha" in item.location
    assert all(change.scope != "params" for change in changes)


def test_step_inserted_produces_single_item() -> None:
    old = _manual_playbook(step_lines=["  - image.trim"])
    new = _manual_playbook(step_lines=["  - image.trim", "  - image.export"])
    changes = summarize_playbook_changes(old, new)
    assert len(changes) == 1
    assert changes[0].kind == "added"
    assert changes[0].scope == "step"
    assert "image.export" in changes[0].description


def test_step_removed_produces_single_item() -> None:
    old = _manual_playbook(step_lines=["  - image.trim", "  - image.export"])
    new = _manual_playbook(step_lines=["  - image.trim"])
    changes = summarize_playbook_changes(old, new)
    assert len(changes) == 1
    assert changes[0].kind == "removed"
    assert changes[0].scope == "step"
    assert "image.export" in changes[0].description


def test_param_value_changed_produces_single_item() -> None:
    old = _manual_playbook(step_lines=["  - image.white_to_alpha: {fuzz: 8}"])
    new = _manual_playbook(step_lines=["  - image.white_to_alpha: {fuzz: 10}"])
    changes = summarize_playbook_changes(old, new)
    assert len(changes) == 1
    assert changes[0].kind == "changed"
    assert changes[0].scope == "params"
    assert "fuzz" in changes[0].location


def test_trigger_path_changed_produces_single_item() -> None:
    old = _manual_playbook(path="~/input")
    new = _manual_playbook(path="~/other")
    changes = summarize_playbook_changes(old, new)
    assert len(changes) == 1
    assert changes[0].scope == "trigger"
    assert "path" in changes[0].location.lower()


def test_pipeline_branch_added_produces_single_item() -> None:
    old = _manual_playbook()
    new = _manual_playbook(
        extra_yaml="""on_failure:
  branches:
    - name: rescue
      retries: 0
      steps:
        - image.trim
"""
    )
    changes = summarize_playbook_changes(old, new)
    assert len(changes) == 1
    assert changes[0].kind == "added"
    assert changes[0].scope == "branch"
    assert "rescue" in changes[0].location


def test_side_by_side_rows_mark_add_delete_replace() -> None:
    left = ["a", "b", "c"]
    right = ["a", "x", "c"]
    rows = side_by_side_rows(left, right)
    kinds = {row.kind for row in rows}
    assert "equal" in kinds
    assert "replace" in kinds or ("delete" in kinds and "add" in kinds)


def test_diff_page_human_qa_scenario(diff_client: tuple[TestClient, Ledger]) -> None:
    client, ledger = diff_client
    pipeline_id, v1 = ledger.register_pipeline(loads_playbook(COMPACT_STEP_YAML), COMPACT_STEP_YAML)
    edit_page = client.get(f"/pipelines/{pipeline_id}/edit")
    form = _parse_form_fields_from_html(edit_page.text)
    add_branch = client.post(
        f"/pipelines/{pipeline_id}/edit/rows",
        data={
            **form,
            "row_action": "add-step-onfail-branch",
            "onfail_prefix": "steps-0-onfail",
            "branches_target_id": "steps-0-onfail-branches",
        },
        headers=POST_HEADERS,
    )
    form = {**form, **_parse_form_fields_from_html(add_branch.text)}
    form["steps-0-onfail-enabled"] = "on"
    form["steps-0-onfail-branches-0-name"] = "pillow-fallback"
    form["steps-0-onfail-branches-0-retries"] = "0"
    form["steps-0-onfail-branches-0-steps-0-id"] = "image.trim"
    form["steps-0-onfail-branches-0-steps-0-params"] = ""
    form["base_version"] = v1
    form["tab"] = "form"
    client.post(
        f"/pipelines/{pipeline_id}/versions",
        data=form,
        headers=POST_HEADERS,
        follow_redirects=False,
    )
    v2, _ = ledger.get_current_playbook(pipeline_id)
    diff = client.get(f"/pipelines/{pipeline_id}/versions/{v2}/diff")
    assert diff.status_code == 200
    assert "What changed" in diff.text
    assert "change-added" in diff.text
    assert "pillow-fallback" in diff.text
    assert "image.white_to_alpha" in diff.text
    assert diff.text.count("change-item") == 1
    assert "diff-side-by-side" in diff.text
    assert "diff-row-add" in diff.text
    assert '<code class="diff-add">' in diff.text


def test_diff_metadata_only_shows_notice_and_no_raw_diff(
    diff_client: tuple[TestClient, Ledger],
) -> None:
    client, ledger = diff_client
    yaml_text = FLAGSHIP.read_text(encoding="utf-8").replace("name: png-cleanup", "name: meta-diff")
    playbook = loads_playbook(yaml_text)
    pipeline_id, _v1 = ledger.register_pipeline(playbook, yaml_text, note="first")
    _, v2 = ledger.register_pipeline(playbook, yaml_text, note="second")
    diff = client.get(f"/pipelines/{pipeline_id}/versions/{v2}/diff")
    assert diff.status_code == 200
    assert "no content changes (metadata-only version)" in diff.text
    assert "diff-side-by-side" not in diff.text


def test_diff_unified_view_renders(diff_client: tuple[TestClient, Ledger]) -> None:
    client, ledger = diff_client
    old = loads_playbook(COMPACT_STEP_YAML)
    new = loads_playbook(STEP_BRANCH_YAML)
    pipeline_id, _v1 = ledger.register_pipeline(old, COMPACT_STEP_YAML)
    _, v2 = ledger.register_pipeline(new, STEP_BRANCH_YAML)
    diff = client.get(f"/pipelines/{pipeline_id}/versions/{v2}/diff?view=unified")
    assert diff.status_code == 200
    assert 'class="diff"' in diff.text
    assert "diff-side-by-side" not in diff.text
    assert "Unified view" in diff.text
