"""A7: CLI approve-branch parity with web AI Approve."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from typer.testing import CliRunner

from ordine.cli.main import app
from ordine.core.db import create_engine_for, init_db
from ordine.core.ledger import Ledger
from ordine.core.playbook import dump_playbook, loads_playbook
from ordine.llm.features.branches import BranchSuggestion

SAFE_YAML = """version: 1
name: approve-cli
trigger:
  type: manual
  path: ~/in
steps:
  - util.fail:
      message: boom
"""

GRAFTED_YAML = """version: 1
name: approve-cli
trigger:
  type: manual
  path: ~/in
steps:
  - id: util.fail
    params:
      message: boom
    on_failure:
      retries: 0
      then: mark_failed
      branches:
        - name: recover
          steps:
            - util.noop
"""


def _seed(tmp_path: Path) -> tuple[Path, int]:
    config_file = tmp_path / "config.toml"
    db = tmp_path / "ordine.sqlite3"
    work = tmp_path / "work"
    config_file.write_text(
        f"""[paths]
db = "{db}"
workdir_root = "{work}"

[llm]
provider = "openai"
model = "gpt-test"
""",
        encoding="utf-8",
    )
    engine = create_engine_for(db)
    init_db(engine)
    ledger = Ledger(engine)
    playbook = loads_playbook(SAFE_YAML)
    pipeline_id, _ = ledger.register_pipeline(playbook, SAFE_YAML)
    task_id = ledger.create_task(pipeline_id, "/in/a.png", "k1")
    assert task_id is not None
    claimed = ledger.claim_next(pipeline_id)
    assert claimed is not None
    ledger.transition(claimed.id, "failed", error="boom")
    return config_file, claimed.id


def test_approve_branch_suggest_and_apply(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    config_file, task_id = _seed(tmp_path)

    def fake_suggest(*_args: Any, **_kwargs: Any) -> BranchSuggestion:
        grafted = loads_playbook(GRAFTED_YAML)
        assert grafted.steps[0].on_failure is not None
        branch = grafted.steps[0].on_failure.branches[0]
        return BranchSuggestion(
            branch=branch,
            rationale="retry with noop",
            target_step_index=0,
            new_playbook=grafted,
            new_yaml=dump_playbook(grafted),
            diff="+ recover",
            raw="{}",
            problems=[],
        )

    monkeypatch.setattr("ordine.cli.main.suggest_branch", fake_suggest)
    monkeypatch.setattr("ordine.cli.main.build_client", lambda _config: object())

    runner = CliRunner()
    suggest = runner.invoke(
        app, ["--config", str(config_file), "approve-branch", str(task_id), "--json"]
    )
    assert suggest.exit_code == 0, suggest.output
    assert "recover" in suggest.output

    apply = runner.invoke(
        app,
        [
            "--config",
            str(config_file),
            "approve-branch",
            str(task_id),
            "--apply",
            "--yes",
            "--json",
        ],
    )
    assert apply.exit_code == 0, apply.output
    assert "true" in apply.output
    assert "applied" in apply.output
