"""Editor, version history, diff, and revert web tests."""

from __future__ import annotations

import re
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from conveyor.core.config import load_config
from conveyor.core.db import create_engine_for, init_db
from conveyor.core.ledger import Ledger
from conveyor.core.playbook import dump_playbook, load_playbook, loads_playbook
from conveyor.web.app import create_app
from conveyor.web.forms import playbook_to_form
from conveyor.web.routes.editor import STARTER_YAML
from tests.test_branch_regression import _parse_form_fields_from_html
from tests.test_web import POST_HEADERS, _write_config

FLAGSHIP = Path("tests/fixtures/playbooks/valid/v02_flagship.yml")

FLOW_STYLE_PASTE_YAML = """\
version: 1
name: semantic-diff-paste
trigger:
  type: folder_watch
  path: ~/input
  glob: '*.png'
  settle_seconds: 2
steps:
  - image.white_to_alpha: {fuzz: 8}
  - image.trim
"""


@pytest.fixture
def editor_client(tmp_path: Path) -> tuple[TestClient, Ledger]:
    config_file = _write_config(tmp_path)
    config = load_config(config_file)
    engine = create_engine_for(config.db_path)
    init_db(engine)
    ledger = Ledger(engine)
    client = TestClient(create_app(config))
    return client, ledger


def _register_flagship(
    client: TestClient, ledger: Ledger, *, name: str = "png-cleanup"
) -> tuple[int, str]:
    yaml_text = FLAGSHIP.read_text(encoding="utf-8").replace("name: png-cleanup", f"name: {name}")
    playbook = loads_playbook(yaml_text)
    pipeline_id, version_id = ledger.register_pipeline(playbook, yaml_text)
    return pipeline_id, version_id


def _form_from_playbook(path: Path) -> dict[str, str]:
    playbook = load_playbook(path)
    form = playbook_to_form(playbook)
    form["tab"] = "form"
    return form


def test_masterplan_acceptance_flow(editor_client: tuple[TestClient, Ledger]) -> None:
    client, ledger = editor_client

    form = _form_from_playbook(FLAGSHIP)
    form["name"] = "editor-masterplan"
    form["editor_save"] = "1"
    created = client.post("/pipelines", data=form, headers=POST_HEADERS, follow_redirects=False)
    assert created.status_code == 303
    pipeline_id = ledger.find_pipeline_id("editor-masterplan")
    assert pipeline_id is not None
    v1, yaml_v1 = ledger.get_current_playbook(pipeline_id)
    assert v1 == "pv_0001"
    assert len(ledger.list_versions(pipeline_id)) == 1

    edit_form = _form_from_playbook(FLAGSHIP)
    edit_form["name"] = "editor-masterplan"
    edit_form["steps-0-params"] = "fuzz: 10"
    edit_form["base_version"] = v1
    edit_form["tab"] = "form"
    saved = client.post(
        f"/pipelines/{pipeline_id}/versions",
        data=edit_form,
        headers=POST_HEADERS,
        follow_redirects=False,
    )
    assert saved.status_code == 303
    v2, _ = ledger.get_current_playbook(pipeline_id)
    assert v2 == "pv_0002"
    assert ledger.get_current_playbook(pipeline_id)[0] == v2

    broken = dict(edit_form)
    broken["tab"] = "yaml"
    broken["yaml_text"] = "version: 1\nname: editor-masterplan\nsteps:\n  bad indent\n"
    validate = client.post(
        f"/pipelines/{pipeline_id}/edit/validate",
        data=broken,
        headers=POST_HEADERS,
    )
    assert validate.status_code == 200
    assert "yaml_text" in validate.text or "error" in validate.text.lower()
    assert len(ledger.list_versions(pipeline_id)) == 2

    revert = client.post(
        f"/pipelines/{pipeline_id}/versions/{v1}/revert",
        headers=POST_HEADERS,
        follow_redirects=False,
    )
    assert revert.status_code == 303
    v3, yaml_v3 = ledger.get_current_playbook(pipeline_id)
    assert v3 == "pv_0003"
    assert yaml_v3.strip() == yaml_v1.strip()
    versions = ledger.list_versions(pipeline_id)
    v3_row = next(row for row in versions if row.public_id == v3)
    assert v3_row.parent_public_id == v2


def test_branch_from_version(editor_client: tuple[TestClient, Ledger]) -> None:
    client, ledger = editor_client
    pipeline_id, v1 = _register_flagship(client, ledger, name="branch-test")
    edit_form = _form_from_playbook(FLAGSHIP)
    edit_form["name"] = "branch-test"
    edit_form["steps-0-params"] = "fuzz: 10"
    edit_form["base_version"] = v1
    edit_form["tab"] = "form"
    client.post(f"/pipelines/{pipeline_id}/versions", data=edit_form, headers=POST_HEADERS)
    v2, _ = ledger.get_current_playbook(pipeline_id)

    branch_form = _form_from_playbook(FLAGSHIP)
    branch_form["name"] = "branch-test"
    branch_form["steps-0-params"] = "fuzz: 12"
    branch_form["base_version"] = v1
    branch_form["tab"] = "form"
    response = client.post(
        f"/pipelines/{pipeline_id}/versions",
        data=branch_form,
        headers=POST_HEADERS,
    )
    assert response.status_code == 200
    assert "branch of" in response.text
    assert ledger.get_current_playbook(pipeline_id)[0] == v2
    saved_id = "pv_0003"
    branch_row = next(row for row in ledger.list_versions(pipeline_id) if row.public_id == saved_id)
    assert branch_row.parent_public_id == v1

    make_current = client.post(
        f"/pipelines/{pipeline_id}/versions/{saved_id}/make-current",
        headers=POST_HEADERS,
        follow_redirects=False,
    )
    assert make_current.status_code == 303
    assert ledger.get_current_playbook(pipeline_id)[0] == saved_id


def test_validate_unknown_step_anchored(editor_client: tuple[TestClient, Ledger]) -> None:
    client, ledger = editor_client
    pipeline_id, v1 = _register_flagship(client, ledger, name="validate-test")
    form = _form_from_playbook(FLAGSHIP)
    form["name"] = "validate-test"
    form["steps-1-id"] = "not.a.real.step"
    form["base_version"] = v1
    form["tab"] = "form"
    before = len(ledger.list_versions(pipeline_id))
    response = client.post(
        f"/pipelines/{pipeline_id}/edit/validate",
        data=form,
        headers=POST_HEADERS,
    )
    assert response.status_code == 200
    assert "steps.1.id" in response.text
    assert len(ledger.list_versions(pipeline_id)) == before


def test_tab_switching_both_directions(editor_client: tuple[TestClient, Ledger]) -> None:
    client, ledger = editor_client
    pipeline_id, v1 = _register_flagship(client, ledger, name="tab-switch")
    form = _form_from_playbook(FLAGSHIP)
    form["name"] = "tab-switch"
    form["base_version"] = v1
    form["tab"] = "form"
    to_yaml = client.post(
        f"/pipelines/{pipeline_id}/edit/to-yaml",
        data=form,
        headers=POST_HEADERS,
    )
    assert to_yaml.status_code == 200
    assert "fuzz: 8" in to_yaml.text

    yaml_form = dict(form)
    yaml_form["tab"] = "yaml"
    yaml_form["yaml_text"] = dump_playbook(load_playbook(FLAGSHIP))
    to_form = client.post(
        f"/pipelines/{pipeline_id}/edit/to-form",
        data=yaml_form,
        headers=POST_HEADERS,
    )
    assert to_form.status_code == 200
    assert 'name="steps-0-id"' in to_form.text


def test_tab_switch_refuses_invalid(editor_client: tuple[TestClient, Ledger]) -> None:
    client, ledger = editor_client
    pipeline_id, v1 = _register_flagship(client, ledger, name="tab-invalid")
    form = _form_from_playbook(FLAGSHIP)
    form["name"] = "tab-invalid"
    form["base_version"] = v1
    form["tab"] = "yaml"
    form["yaml_text"] = "version: 1\nname: bad\nsteps:\n  - bad"
    to_form = client.post(
        f"/pipelines/{pipeline_id}/edit/to-form",
        data=form,
        headers=POST_HEADERS,
    )
    assert to_form.status_code == 200
    assert "error" in to_form.text.lower() or "steps" in to_form.text


def test_row_add_remove_reindexes(editor_client: tuple[TestClient, Ledger]) -> None:
    client, ledger = editor_client
    pipeline_id, v1 = _register_flagship(client, ledger, name="rows-test")
    form = _form_from_playbook(FLAGSHIP)
    form["name"] = "rows-test"
    form["base_version"] = v1
    form["row_action"] = "add-step"
    added = client.post(
        f"/pipelines/{pipeline_id}/edit/rows",
        data=form,
        headers=POST_HEADERS,
    )
    assert added.status_code == 200
    assert "steps-4-id" in added.text or "steps-5-id" in added.text

    remove_form = dict(form)
    remove_form["row_action"] = "remove-step"
    remove_form["row_index"] = "0"
    removed = client.post(
        f"/pipelines/{pipeline_id}/edit/rows",
        data=remove_form,
        headers=POST_HEADERS,
    )
    assert removed.status_code == 200
    assert 'name="steps-0-id"' not in removed.text
    assert "image.trim" in removed.text


def test_diff_shows_changed_line(editor_client: tuple[TestClient, Ledger]) -> None:
    client, ledger = editor_client
    pipeline_id, v1 = _register_flagship(client, ledger, name="diff-test")
    edit_form = _form_from_playbook(FLAGSHIP)
    edit_form["name"] = "diff-test"
    edit_form["steps-0-params"] = "fuzz: 10"
    edit_form["base_version"] = v1
    edit_form["tab"] = "form"
    client.post(f"/pipelines/{pipeline_id}/versions", data=edit_form, headers=POST_HEADERS)
    v2, _ = ledger.get_current_playbook(pipeline_id)
    diff = client.get(f"/pipelines/{pipeline_id}/versions/{v2}/diff")
    assert diff.status_code == 200
    assert "fuzz" in diff.text
    assert "diff-row-replace" in diff.text or "diff-row-add" in diff.text


def _manual_yaml(name: str, watch: Path) -> str:
    return f"""version: 1
name: {name}
trigger:
  type: manual
  path: {watch}
steps:
  - image.white_to_alpha:
      fuzz: 8
  - image.trim
"""


def test_running_version_drift_badge(
    editor_client: tuple[TestClient, Ledger], tmp_path: Path
) -> None:
    client, ledger = editor_client
    watch = tmp_path / "drift-in"
    watch.mkdir()
    yaml_text = _manual_yaml("drift-test", watch)
    playbook = loads_playbook(yaml_text)
    pipeline_id, v1 = ledger.register_pipeline(playbook, yaml_text)
    edit_form = playbook_to_form(playbook)
    edit_form["name"] = "drift-test"
    edit_form["steps-0-params"] = "fuzz: 10"
    edit_form["base_version"] = v1
    edit_form["tab"] = "form"
    client.post(f"/pipelines/{pipeline_id}/versions", data=edit_form, headers=POST_HEADERS)
    client.post(f"/pipelines/{pipeline_id}/start", headers=POST_HEADERS)
    edit_form["steps-0-params"] = "fuzz: 11"
    edit_form["base_version"] = ledger.get_current_playbook(pipeline_id)[0]
    client.post(f"/pipelines/{pipeline_id}/versions", data=edit_form, headers=POST_HEADERS)
    home = client.get("/")
    assert "restart to apply" in home.text


def test_new_pipeline_starter(editor_client: tuple[TestClient, Ledger]) -> None:
    client, _ledger = editor_client
    response = client.get("/pipelines/new")
    assert response.status_code == 200
    assert "my-pipeline" in response.text
    starter = playbook_to_form(loads_playbook(STARTER_YAML))
    starter["tab"] = "form"
    to_yaml = client.post("/pipelines/new/edit/to-yaml", data=starter, headers=POST_HEADERS)
    assert "Comments in YAML are not preserved" in to_yaml.text


def test_semantic_diff_shows_only_meaningful_changes(
    editor_client: tuple[TestClient, Ledger],
) -> None:
    client, ledger = editor_client
    registered = client.post(
        "/pipelines",
        data={"yaml_text": FLOW_STYLE_PASTE_YAML},
        headers=POST_HEADERS,
        follow_redirects=False,
    )
    assert registered.status_code == 303
    pipeline_id = ledger.find_pipeline_id("semantic-diff-paste")
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
    form["onfail-branches-1-name"] = "rescue-trim"
    form["onfail-branches-1-retries"] = "0"
    form["onfail-branches-1-steps-0-id"] = "image.trim"
    form["onfail-branches-1-steps-0-params"] = ""
    form["onfail-enabled"] = "on"
    form["tab"] = "form"
    form["base_version"] = v1
    client.post(
        f"/pipelines/{pipeline_id}/versions",
        data=form,
        headers=POST_HEADERS,
        follow_redirects=False,
    )
    v2, _ = ledger.get_current_playbook(pipeline_id)
    diff = client.get(f"/pipelines/{pipeline_id}/versions/{v2}/diff")
    assert diff.status_code == 200
    assert "(formatting normalized)" in diff.text
    assert "rescue-trim" in diff.text
    assert "diff-row-add" in diff.text
    changed_rows = re.findall(
        r'<tr class="diff-row-(?:add|delete|replace)">.*?</tr>',
        diff.text,
        flags=re.S,
    )
    for row in changed_rows:
        for needle in ("settle_seconds", "glob:", "{fuzz", "'*.png'"):
            assert needle not in row


def test_diff_metadata_only_version(editor_client: tuple[TestClient, Ledger]) -> None:
    client, ledger = editor_client
    yaml_text = FLAGSHIP.read_text(encoding="utf-8").replace("name: png-cleanup", "name: meta-only")
    playbook = loads_playbook(yaml_text)
    pipeline_id, v1 = ledger.register_pipeline(playbook, yaml_text, note="first note")
    _, v2 = ledger.register_pipeline(playbook, yaml_text, note="second note")
    assert v1 != v2
    diff = client.get(f"/pipelines/{pipeline_id}/versions/{v2}/diff")
    assert diff.status_code == 200
    assert "no content changes (metadata-only version)" in diff.text
    assert "diff-side-by-side" not in diff.text


def test_editor_form_labeling_and_empty_note(editor_client: tuple[TestClient, Ledger]) -> None:
    client, ledger = editor_client
    yaml_text = FLAGSHIP.read_text(encoding="utf-8").replace(
        "name: png-cleanup", "name: form-labels"
    )
    playbook = loads_playbook(yaml_text)
    pipeline_id, _ = ledger.register_pipeline(playbook, yaml_text, note="do not prefill")
    edit = client.get(f"/pipelines/{pipeline_id}/edit")
    assert edit.status_code == 200
    assert "Step 1 (steps.0)" in edit.text
    assert "Settle seconds (folder_watch only)" in edit.text
    assert "Poll seconds (manifest only)" in edit.text
    assert 'id="note" name="note" value=""' in edit.text
