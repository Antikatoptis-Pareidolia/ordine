"""Trust UX for shell.run on register and AI approve (S5/S6/S7/D5)."""

from __future__ import annotations

from pathlib import Path

from fastapi.testclient import TestClient

from ordine.core.config import load_config
from ordine.core.db import create_engine_for, init_db
from ordine.core.dryrun import playbook_contains_shell_run, suggestion_adds_shell_run
from ordine.core.ledger import Ledger
from ordine.core.playbook import dump_playbook, loads_playbook
from ordine.llm.features.branches import BranchSuggestion
from ordine.web.app import create_app
from ordine.web.routes.tasks import BranchSuggestionStore

POST_HEADERS = {"HX-Request": "true", "Origin": "http://testserver"}

SHELL_YAML = """version: 1
name: trust-shell
trigger:
  type: manual
  path: ~/in
steps:
  - shell.run:
      cmd: echo hi
"""

SAFE_YAML = """version: 1
name: trust-safe
trigger:
  type: manual
  path: ~/in
steps:
  - util.noop
"""


def _client(tmp_path: Path) -> TestClient:
    config_file = tmp_path / "config.toml"
    config_file.write_text(
        f"""[paths]
db = "{tmp_path / "ordine.sqlite3"}"
workdir_root = "{tmp_path / "workdirs"}"

[web]
bind = "testserver"
allowed_hosts = ["testserver"]
port = 8484
""",
        encoding="utf-8",
    )
    return TestClient(create_app(load_config(config_file)))


def test_register_requires_shell_ack(tmp_path: Path) -> None:
    client = _client(tmp_path)
    denied = client.post(
        "/pipelines",
        data={"yaml_text": SHELL_YAML},
        headers=POST_HEADERS,
    )
    assert denied.status_code == 200
    assert "shell.run" in denied.text
    assert 'name="acknowledge_shell_run"' in denied.text
    assert "Confirm the danger callout" in denied.text

    allowed = client.post(
        "/pipelines",
        data={"yaml_text": SHELL_YAML, "acknowledge_shell_run": "on"},
        headers=POST_HEADERS,
        follow_redirects=False,
    )
    assert allowed.status_code == 303


def test_register_safe_yaml_needs_no_ack(tmp_path: Path) -> None:
    client = _client(tmp_path)
    response = client.post(
        "/pipelines",
        data={"yaml_text": SAFE_YAML},
        headers=POST_HEADERS,
        follow_redirects=False,
    )
    assert response.status_code == 303


def test_suggestion_adds_shell_run_helper() -> None:
    before = loads_playbook(SAFE_YAML)
    after = loads_playbook(SHELL_YAML.replace("trust-shell", "trust-safe"))
    assert playbook_contains_shell_run(after)
    assert suggestion_adds_shell_run(before, after)
    assert not suggestion_adds_shell_run(after, after)


def test_approve_branch_requires_shell_confirm(tmp_path: Path) -> None:
    config_file = tmp_path / "config.toml"
    config_file.write_text(
        f"""[paths]
db = "{tmp_path / "ordine.sqlite3"}"
workdir_root = "{tmp_path / "workdirs"}"

[web]
bind = "testserver"
allowed_hosts = ["testserver"]
port = 8484
""",
        encoding="utf-8",
    )
    config = load_config(config_file)
    engine = create_engine_for(config.db_path)
    init_db(engine)
    ledger = Ledger(engine)
    playbook = loads_playbook(SAFE_YAML)
    pipeline_id, _ = ledger.register_pipeline(playbook, SAFE_YAML)
    task_id = ledger.create_task(
        pipeline_id, source_ref=str(tmp_path / "x.txt"), dedup_key="a" * 64
    )
    assert task_id is not None
    grafted_yaml = """version: 1
name: trust-safe
trigger:
  type: manual
  path: ~/in
on_failure:
  retries: 0
  then: mark_failed
  branches:
    - name: ai-shell
      steps:
        - shell.run:
            cmd: echo hi
steps:
  - util.noop
"""
    new_playbook = loads_playbook(grafted_yaml)
    branch = new_playbook.on_failure.branches[0]
    new_yaml = dump_playbook(new_playbook)
    suggestion = BranchSuggestion(
        branch=branch,
        rationale="use shell",
        target_step_index=0,
        new_playbook=new_playbook,
        new_yaml=new_yaml,
        diff="",
        raw="{}",
        problems=[],
    )
    app = create_app(config)
    store: BranchSuggestionStore = app.state.branch_suggestions
    store.put(task_id, pipeline_id=pipeline_id, suggestion=suggestion)
    client = TestClient(app)

    denied = client.post(
        f"/tasks/{task_id}/ai/approve-branch",
        headers=POST_HEADERS,
    )
    assert denied.status_code == 200
    assert "shell.run" in denied.text
    assert 'name="confirm_shell_run"' in denied.text

    allowed = client.post(
        f"/tasks/{task_id}/ai/approve-branch",
        data={"confirm_shell_run": "on"},
        headers=POST_HEADERS,
        follow_redirects=False,
    )
    assert allowed.status_code == 303
    assert "versions" in allowed.headers["location"]
