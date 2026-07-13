"""End-to-end LLM feature tests with canned responses (masterplan acceptances)."""

from __future__ import annotations

import os
import shutil
import tempfile
from collections.abc import Sequence
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from ordine.core.config import AppConfig, load_config
from ordine.core.db import create_engine_for, init_db
from ordine.core.dryrun import DryRunSession
from ordine.core.engines import EngineRegistry, HeadlessEngine
from ordine.core.ledger import Ledger
from ordine.core.playbook import ManualTrigger, loads_playbook
from ordine.core.registry import StepRegistry
from ordine.core.runner import PipelineRunner
from ordine.core.triggers import ManualScanService, ledger_sink
from ordine.llm.client import build_client
from ordine.llm.features.branches import apply_branch, suggest_branch
from ordine.llm.features.drafting import draft_playbook
from ordine.llm.types import LLMResponse, Message, Usage
from ordine.web.app import create_app
from tests.test_image_steps import make_test_image

FIXTURES = Path(__file__).parent / "fixtures" / "llm"
POST_HEADERS = {"HX-Request": "true", "Origin": "http://testserver"}


def _fixture(name: str) -> str:
    return (FIXTURES / name).read_text(encoding="utf-8")


class CannedLLMClient:
    provider = "mock"
    model = "mock-model"

    def __init__(self, responses: list[str]) -> None:
        self._responses = list(responses)

    def complete(
        self,
        messages: Sequence[Message],
        *,
        purpose: str,
        max_tokens: int | None = None,
        temperature: float = 0.2,
        timeout: float = 60.0,
    ) -> LLMResponse:
        del messages, purpose, max_tokens, temperature, timeout
        return LLMResponse(
            text=self._responses.pop(0),
            model=self.model,
            usage=Usage(5, 3),
            duration_s=0.1,
        )


def _write_config(tmp_path: Path) -> Path:
    config_file = tmp_path / "config.toml"
    config_file.write_text(
        f"""[paths]
db = "{tmp_path / "ordine.sqlite3"}"
workdir_root = "{tmp_path / "workdirs"}"

[web]
host = "127.0.0.1"
port = 8484

[llm]
provider = "openai"
model = "mock"
""",
        encoding="utf-8",
    )
    return config_file


def test_draft_acceptance_runs_green_in_dryrun(tmp_path: Path) -> None:
    registry = StepRegistry.load()
    engines = EngineRegistry()
    engines.register(HeadlessEngine())
    client = CannedLLMClient([_fixture("flagship_draft.yaml.txt")])
    description = "watch ~/in, clear white background, rename from assets.csv, export to ~/out"
    result = draft_playbook(client, registry, description)
    assert result.playbook is not None
    assert not registry.check_playbook(result.playbook)

    sample = tmp_path / "samples"
    sample.mkdir()
    make_test_image(sample / "a.png")
    sandbox_parent = Path(tempfile.mkdtemp(prefix="draft-e2e-"))
    session = DryRunSession.create(
        playbook=result.playbook,
        version_public_id="draft-test",
        sample_dir=sample,
        glob="*.png",
        registry=registry,
        engines=engines,
        sandbox_root=sandbox_parent,
        yaml_text=result.yaml_text,
        max_samples=2,
    )
    try:
        shutil.copy(sample / "a.png", sample / "b.png")
        session.run_all()
        report = session.report()
        assert all(task["status"] == "done" for task in report["tasks"][:2])
    finally:
        session.close()


def test_learning_loop_acceptance(tmp_path: Path) -> None:
    eng = create_engine_for(tmp_path / "ledger.db")
    init_db(eng)
    ledger = Ledger(eng)
    registry = StepRegistry.load()
    engines = EngineRegistry()
    engines.register(HeadlessEngine())

    watch = tmp_path / "in"
    watch.mkdir()
    yaml_text = f"""version: 1
name: learn-loop
trigger:
  type: manual
  path: {watch}
  glob: "*.png"
steps:
  - util.noop
  - id: util.fail
    params:
      times: -1
on_failure: {{ retries: 0, then: mark_failed }}
"""
    playbook = loads_playbook(yaml_text)
    pipeline_id, version = ledger.register_pipeline(playbook, yaml_text)
    runner = PipelineRunner(
        ledger=ledger,
        registry=registry,
        engines=engines,
        playbook=playbook,
        pipeline_id=pipeline_id,
        workdir_root=tmp_path / "work",
        playbook_version=version,
    )
    make_test_image(watch / "img_0001.png")
    (watch / "img_0001.png").write_bytes((watch / "img_0001.png").read_bytes() + b"1")
    sink = ledger_sink(ledger, pipeline_id)
    ManualScanService(
        ManualTrigger(type="manual", path=str(watch), glob="*.png"),
        playbook.dedup,
        sink,
    ).run()
    assert runner.run_until_idle() == 1
    task = ledger.list_tasks(pipeline_id, limit=10)[0]
    assert task.status == "flagged"

    client = CannedLLMClient([_fixture("branch_suggestion.json.txt")])
    suggestion = suggest_branch(client, registry, ledger, task.id, tmp_path / "work")
    assert suggestion.new_playbook is not None
    assert "ai-fix" in suggestion.diff

    parent_version, _ = ledger.get_current_playbook(pipeline_id)
    new_version = apply_branch(
        ledger,
        pipeline_id,
        suggestion,
        note="AI branch: ai-fix",
    )
    versions = ledger.list_versions(pipeline_id)
    current = next(v for v in versions if v.public_id == new_version)
    assert current.parent_public_id == parent_version
    assert current.note == "AI branch: ai-fix"

    new_public, new_yaml = ledger.get_current_playbook(pipeline_id)
    new_playbook = loads_playbook(new_yaml)
    runner2 = PipelineRunner(
        ledger=ledger,
        registry=registry,
        engines=engines,
        playbook=new_playbook,
        pipeline_id=pipeline_id,
        workdir_root=tmp_path / "work",
        playbook_version=new_public,
    )
    make_test_image(watch / "img_0002.png")
    (watch / "img_0002.png").write_bytes((watch / "img_0002.png").read_bytes() + b"2")
    ManualScanService(
        ManualTrigger(type="manual", path=str(watch), glob="*.png"),
        playbook.dedup,
        sink,
    ).run()
    assert runner2.run_until_idle() == 1
    new_task = next(t for t in ledger.list_tasks(pipeline_id, limit=10) if t.id != task.id)
    assert new_task.status == "done"
    assert new_task.current_branch == "ai-fix"

    ledger.transition(task.id, "pending")
    assert runner2.run_until_idle() == 1
    retried = ledger.get_task(task.id)
    assert retried.status == "done"
    assert retried.current_branch == "ai-fix"


def test_web_not_configured_card(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    config_path = _write_config(tmp_path)
    config = load_config(config_path)
    config = AppConfig(
        db_path=config.db_path,
        workdir_root=config.workdir_root,
        llm_provider="none",
        config_file=config_path,
    )
    monkeypatch.setattr(
        "ordine.web.routes.editor.build_client",
        lambda _c: (_ for _ in ()).throw(
            __import__(
                "ordine.llm.errors", fromlist=["LLMNotConfiguredError"]
            ).LLMNotConfiguredError()
        ),
    )
    client = TestClient(create_app(config))
    response = client.post(
        "/pipelines/new/ai/draft",
        data={"description": "test"},
        headers=POST_HEADERS,
    )
    assert response.status_code == 200
    assert "Settings" in response.text


def _seed_flagged_learn_loop(
    ledger: Ledger,
    registry: StepRegistry,
    engines: EngineRegistry,
    workdir_root: Path,
    tmp_path: Path,
) -> tuple[int, int]:
    watch = tmp_path / "in"
    watch.mkdir()
    yaml_text = f"""version: 1
name: learn-loop
trigger:
  type: manual
  path: {watch}
  glob: "*.png"
steps:
  - util.noop
  - id: util.fail
    params:
      times: -1
on_failure: {{ retries: 0, then: mark_failed }}
"""
    playbook = loads_playbook(yaml_text)
    pipeline_id, version = ledger.register_pipeline(playbook, yaml_text)
    runner = PipelineRunner(
        ledger=ledger,
        registry=registry,
        engines=engines,
        playbook=playbook,
        pipeline_id=pipeline_id,
        workdir_root=workdir_root,
        playbook_version=version,
    )
    make_test_image(watch / "img_0001.png")
    (watch / "img_0001.png").write_bytes((watch / "img_0001.png").read_bytes() + b"1")
    sink = ledger_sink(ledger, pipeline_id)
    ManualScanService(
        ManualTrigger(type="manual", path=str(watch), glob="*.png"),
        playbook.dedup,
        sink,
    ).run()
    assert runner.run_until_idle() == 1
    task = ledger.list_tasks(pipeline_id, limit=1)[0]
    assert task.status == "flagged"
    return pipeline_id, task.id


def test_web_draft_clickthrough(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    config = load_config(_write_config(tmp_path))
    canned = CannedLLMClient([_fixture("flagship_draft.yaml.txt")])
    monkeypatch.setattr("ordine.web.routes.editor.build_client", lambda _c: canned)
    client = TestClient(create_app(config))
    response = client.post(
        "/pipelines/new/ai/draft",
        data={
            "description": (
                "watch ~/in, clear white background, rename from assets.csv, export to ~/out"
            )
        },
        headers=POST_HEADERS,
    )
    assert response.status_code == 200
    assert "Valid playbook draft" in response.text
    assert "png-cleanup" in response.text


def test_web_learning_loop_clickthrough(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    config = load_config(_write_config(tmp_path))
    app = create_app(config)
    ledger = app.state.ledger
    pipeline_id, task_id = _seed_flagged_learn_loop(
        ledger,
        app.state.registry,
        app.state.engines,
        config.workdir_root,
        tmp_path,
    )
    canned = CannedLLMClient([_fixture("branch_suggestion.json.txt")])
    monkeypatch.setattr("ordine.web.routes.tasks.build_client", lambda _c: canned)
    client = TestClient(app)
    suggest = client.post(f"/tasks/{task_id}/ai/suggest-branch", headers=POST_HEADERS)
    assert suggest.status_code == 200
    assert "Suggested recovery branch" in suggest.text
    assert "Approve" in suggest.text
    approve = client.post(
        f"/tasks/{task_id}/ai/approve-branch",
        headers=POST_HEADERS,
        follow_redirects=False,
    )
    assert approve.status_code == 303
    current_version, _ = ledger.get_current_playbook(pipeline_id)
    versions = ledger.list_versions(pipeline_id)
    current = next(v for v in versions if v.public_id == current_version)
    assert current.note == "AI branch: ai-fix"


@pytest.mark.llm_live
def test_live_draft_with_available_provider_key(tmp_path: Path) -> None:
    """Run a real draft only when an explicitly supported provider key is present."""
    if os.environ.get("ANTHROPIC_API_KEY"):
        provider, model = "anthropic", "claude-3-5-haiku-latest"
    elif os.environ.get("OPENAI_API_KEY"):
        provider, model = "openai", "gpt-4o-mini"
    else:
        pytest.skip("requires ANTHROPIC_API_KEY or OPENAI_API_KEY")
    client = build_client(
        AppConfig(
            db_path=tmp_path / "db.sqlite3",
            workdir_root=tmp_path / "workdirs",
            llm_provider=provider,
            llm_model=model,
        )
    )
    result = draft_playbook(client, StepRegistry.load(), "copy files from ~/in to ~/out")
    assert result.playbook is not None, result.problems
