"""Unit tests for LLM features (canned client, no network)."""

from __future__ import annotations

import json
from collections.abc import Sequence
from pathlib import Path

import httpx
import pytest
from pydantic import BaseModel, ConfigDict

from ordine.core.config import AppConfig
from ordine.core.db import create_engine_for, init_db
from ordine.core.ledger import Ledger
from ordine.core.playbook import Playbook, RecoveryBranch, loads_playbook
from ordine.core.registry import StepRegistry
from ordine.llm.adapters.openai import OpenAIClient
from ordine.llm.client import TokenBudget, _BudgetClient, _LoggingClient, build_client
from ordine.llm.errors import LLMResponseError
from ordine.llm.features.branches import suggest_branch
from ordine.llm.features.context import MAX_CONTEXT_CHARS, failure_context, step_catalog
from ordine.llm.features.diagnosis import diagnose, load_diagnosis, save_diagnosis
from ordine.llm.features.drafting import draft_playbook
from ordine.llm.types import LLMResponse, Message, Usage

FIXTURES = Path(__file__).parent / "fixtures" / "llm"


def _fixture(name: str) -> str:
    return (FIXTURES / name).read_text(encoding="utf-8")


class CannedLLMClient:
    provider = "mock"
    model = "mock-model"

    def __init__(self, responses: list[str]) -> None:
        self._responses = list(responses)
        self.purposes: list[str] = []

    def complete(
        self,
        messages: Sequence[Message],
        *,
        purpose: str,
        max_tokens: int | None = None,
        temperature: float = 0.2,
        timeout: float = 60.0,
    ) -> LLMResponse:
        del messages, max_tokens, temperature, timeout
        self.purposes.append(purpose)
        text = self._responses.pop(0)
        return LLMResponse(text=text, model=self.model, usage=Usage(3, 2), duration_s=0.2)


class _FakeParams(BaseModel):
    model_config = ConfigDict(extra="forbid")


def _register_fake_steps(registry: StepRegistry, count: int) -> None:
    for index in range(count):

        class _FakeStep:
            id = f"util.fake_{index:02d}"
            engines = frozenset({"headless"})
            Params = _FakeParams
            OUTPUT_DIR_PARAMS = frozenset()

            def run(self, ctx: object, params: BaseModel) -> object:
                del ctx, params
                return None

        registry.register(_FakeStep, source="test")  # type: ignore[arg-type]


@pytest.fixture
def registry() -> StepRegistry:
    return StepRegistry.load()


def test_step_catalog_contains_registered_ids_and_schemas(registry: StepRegistry) -> None:
    catalog = step_catalog(registry)
    payload = json.loads(catalog)
    for step_id in registry.ids():
        assert step_id in payload
        assert "params_schema" in payload[step_id]


def test_step_catalog_stays_under_cap_with_many_fake_steps() -> None:
    registry = StepRegistry()
    _register_fake_steps(registry, 50)
    assert len(step_catalog(registry)) < MAX_CONTEXT_CHARS


def test_failure_context_truncates_and_redacts_secrets(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, registry: StepRegistry
) -> None:
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-abcdefghijklmnopqrstuvwxyz123456")
    eng = create_engine_for(tmp_path / "db.sqlite")
    init_db(eng)
    ledger = Ledger(eng)
    yaml_text = _fixture("flagship_draft.yaml.txt")
    playbook = loads_playbook(yaml_text)
    pipeline_id, _version = ledger.register_pipeline(playbook, yaml_text)
    task_id = ledger.create_task(pipeline_id, str(tmp_path / "in/img.png"), "k1") or 0
    workdir = tmp_path / "work" / "png-cleanup" / f"task_{task_id:06d}"
    step_dir = workdir / "01_image_white_to_alpha"
    step_dir.mkdir(parents=True)
    (step_dir / "log.txt").write_text(
        "line\n" * 500
        + (
            "sk-abcdefghijklmnopqrstuvwxyz123456\n"
            "Authorization: Bearer abcdefghijklmnopqrstuvwxyz.123456\n"
            "api_key='plain-assignment-secret'\n"
            "anthropic-api-key:providerprefixsecret123\n"
        ),
        encoding="utf-8",
    )
    ledger.set_workdir(task_id, workdir)
    text, images = failure_context(ledger, task_id, tmp_path / "work", include_image=False)
    assert len(text) <= MAX_CONTEXT_CHARS
    assert "sk-abcdefghijklmnopqrstuvwxyz123456" not in text
    assert "abcdefghijklmnopqrstuvwxyz.123456" not in text
    assert "plain-assignment-secret" not in text
    assert "providerprefixsecret123" not in text
    assert text.count("<redacted>") >= 4
    assert images == []


def test_draft_clean_yaml_valid(registry: StepRegistry) -> None:
    client = CannedLLMClient([_fixture("flagship_draft.yaml.txt")])
    result = draft_playbook(client, registry, "make a png pipeline")
    assert result.playbook is not None
    assert not result.problems
    assert result.repaired is False


def test_draft_numbered_files_fixture_includes_ordinal_regex(registry: StepRegistry) -> None:
    description = "watch ~/in for img_0001.png style files, rename from assets.csv, export to ~/out"
    client = CannedLLMClient([_fixture("ordinal_draft_with_regex.yaml.txt")])
    result = draft_playbook(client, registry, description)
    assert result.playbook is not None
    assert not result.problems
    trigger = result.playbook.trigger
    assert getattr(trigger, "ordinal_regex", None) is not None


def test_draft_full_stack_mock_transport_logs_and_charges_budget(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, registry: StepRegistry
) -> None:
    """Feature call through real build_client stack: budget, logging, MockTransport HTTP."""
    yaml_fixture = _fixture("flagship_draft.yaml.txt")

    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "model": "gpt-test",
                "choices": [{"message": {"role": "assistant", "content": yaml_fixture}}],
                "usage": {"prompt_tokens": 100, "completion_tokens": 50},
            },
        )

    data_dir = tmp_path / "data"
    data_dir.mkdir()
    config = AppConfig(
        db_path=data_dir / "ordine.sqlite3",
        workdir_root=tmp_path / "work",
        llm_provider="openai",
        llm_model="gpt-test",
        llm_max_tokens=4096,
        llm_session_token_cap=50_000,
    )
    monkeypatch.setattr("ordine.llm.client.get_key", lambda _provider: "test-key")

    budget = TokenBudget(50_000)
    client = build_client(config, budget=budget)
    assert isinstance(client, _BudgetClient)
    logging_client = client.inner
    assert isinstance(logging_client, _LoggingClient)
    adapter = logging_client.inner
    assert isinstance(adapter, OpenAIClient)
    adapter._client = httpx.Client(transport=httpx.MockTransport(handler))

    result = draft_playbook(client, registry, "make flagship pipeline")
    assert result.playbook is not None
    assert not result.problems
    assert budget.used == 150

    log_files = list((data_dir / "llm_log").glob("*.jsonl"))
    assert len(log_files) == 1
    records = [
        json.loads(line) for line in log_files[0].read_text(encoding="utf-8").splitlines() if line
    ]
    assert any(record["purpose"] == "draft_playbook" for record in records)


def test_draft_strips_fences(registry: StepRegistry) -> None:
    client = CannedLLMClient([_fixture("fenced_draft.yaml.txt")])
    result = draft_playbook(client, registry, "draft")
    assert result.playbook is not None


def test_draft_repair_ladder_two_calls(registry: StepRegistry) -> None:
    client = CannedLLMClient([_fixture("invalid_draft.yaml.txt"), _fixture("fixed_draft.yaml.txt")])
    result = draft_playbook(client, registry, "draft")
    assert result.repaired is True
    assert len(client.purposes) == 2
    assert result.playbook is not None


def test_draft_invalid_twice_returns_problems_not_exception(registry: StepRegistry) -> None:
    bad = _fixture("invalid_draft.yaml.txt")
    client = CannedLLMClient([bad, bad])
    result = draft_playbook(client, registry, "draft")
    assert result.playbook is None
    assert result.problems


def test_diagnosis_happy_path_and_persistence(tmp_path: Path, registry: StepRegistry) -> None:
    eng = create_engine_for(tmp_path / "db.sqlite")
    init_db(eng)
    ledger = Ledger(eng)
    yaml_text = """version: 1
name: t
trigger: { type: manual, path: ~/in }
steps: [util.noop]
"""
    playbook = loads_playbook(yaml_text)
    pipeline_id, _ = ledger.register_pipeline(playbook, yaml_text)
    task_id = ledger.create_task(pipeline_id, "x.png", "d1") or 0
    workdir = tmp_path / "work" / "t" / f"task_{task_id:06d}"
    workdir.mkdir(parents=True)
    ledger.set_workdir(task_id, workdir)
    ledger.raise_flag(pipeline_id, task_id=task_id, level=1, kind="step_fail", message="fail")
    client = CannedLLMClient([_fixture("diagnosis_ok.json.txt")])
    result = diagnose(client, registry, ledger, task_id, tmp_path / "work")
    assert result.cause
    save_diagnosis(workdir, 1, result)
    loaded = load_diagnosis(workdir, 1)
    assert loaded is not None
    assert loaded.cause == result.cause


def test_diagnosis_strips_fenced_json(tmp_path: Path, registry: StepRegistry) -> None:
    eng = create_engine_for(tmp_path / "db.sqlite")
    init_db(eng)
    ledger = Ledger(eng)
    yaml_text = """version: 1
name: t
trigger: { type: manual, path: ~/in }
steps: [util.noop]
"""
    playbook = loads_playbook(yaml_text)
    pipeline_id, _ = ledger.register_pipeline(playbook, yaml_text)
    task_id = ledger.create_task(pipeline_id, "x.png", "d1") or 0
    workdir = tmp_path / "work" / "t" / f"task_{task_id:06d}"
    workdir.mkdir(parents=True)
    ledger.set_workdir(task_id, workdir)
    fenced = f"```json\n{_fixture('diagnosis_ok.json.txt')}\n```"
    client = CannedLLMClient([fenced])
    result = diagnose(client, registry, ledger, task_id, tmp_path / "work")
    assert result.cause
    assert result.confidence in {"low", "medium", "high"}


def test_diagnosis_malformed_twice_raises(tmp_path: Path, registry: StepRegistry) -> None:
    eng = create_engine_for(tmp_path / "db.sqlite")
    init_db(eng)
    ledger = Ledger(eng)
    yaml_text = """version: 1
name: t
trigger: { type: manual, path: ~/in }
steps: [util.noop]
"""
    playbook = loads_playbook(yaml_text)
    pipeline_id, _ = ledger.register_pipeline(playbook, yaml_text)
    task_id = ledger.create_task(pipeline_id, "x.png", "d1") or 0
    workdir = tmp_path / "work" / "t" / f"task_{task_id:06d}"
    workdir.mkdir(parents=True)
    ledger.set_workdir(task_id, workdir)
    client = CannedLLMClient(["not json", "still not json"])
    with pytest.raises(LLMResponseError):
        diagnose(client, registry, ledger, task_id, tmp_path / "work")


def test_branch_name_collision_suffix(registry: StepRegistry) -> None:
    playbook = Playbook.model_validate(
        {
            "version": 1,
            "name": "b",
            "trigger": {"type": "manual", "path": "~/in"},
            "steps": [
                {
                    "id": "util.noop",
                    "on_failure": {
                        "branches": [{"name": "ai-fix", "steps": [{"id": "util.copy"}]}]
                    },
                }
            ],
        }
    )
    branch = RecoveryBranch.model_validate({"name": "ai-fix", "steps": [{"id": "util.copy"}]})
    from ordine.llm.features import branches as branches_mod

    modified = branches_mod._graft_branch(playbook, step_index=0, branch=branch)
    assert modified.steps[0].on_failure is not None
    names = [b.name for b in modified.steps[0].on_failure.branches]
    assert "ai-fix-2" in names


def test_branch_name_collision_suffix_playbook_wide(registry: StepRegistry) -> None:
    """Suffix must avoid names on other steps, not only the graft target."""
    playbook = Playbook.model_validate(
        {
            "version": 1,
            "name": "b",
            "trigger": {"type": "manual", "path": "~/in"},
            "steps": [
                {
                    "id": "util.noop",
                    "on_failure": {
                        "branches": [{"name": "ai-fix", "steps": [{"id": "util.copy"}]}]
                    },
                },
                {"id": "util.fail"},
            ],
        }
    )
    branch = RecoveryBranch.model_validate({"name": "ai-fix", "steps": [{"id": "util.noop"}]})
    from ordine.llm.features import branches as branches_mod

    modified = branches_mod._graft_branch(playbook, step_index=1, branch=branch)
    step_policy = modified.steps[1].on_failure
    assert step_policy is not None
    names = [b.name for b in step_policy.branches]
    assert "ai-fix-2" in names


def test_branch_step_without_on_failure_gains_policy(registry: StepRegistry) -> None:
    playbook = loads_playbook(
        """version: 1
name: b
trigger: { type: manual, path: ~/in }
on_failure: { retries: 2, then: skip }
steps:
  - util.noop
  - util.fail
"""
    )
    branch = RecoveryBranch.model_validate({"name": "ai-fix", "steps": [{"id": "util.copy"}]})
    from ordine.llm.features import branches as branches_mod

    modified = branches_mod._graft_branch(playbook, step_index=1, branch=branch)
    step_policy = modified.steps[1].on_failure
    assert step_policy is not None
    assert step_policy.retries == 2
    assert any(b.name == "ai-fix" for b in step_policy.branches)


def test_branch_unknown_step_surfaces_errors_after_repair(
    tmp_path: Path, registry: StepRegistry
) -> None:
    eng = create_engine_for(tmp_path / "db.sqlite")
    init_db(eng)
    ledger = Ledger(eng)
    yaml_text = """version: 1
name: learn
trigger: { type: manual, path: ~/in }
steps: [util.noop, util.fail]
"""
    playbook = loads_playbook(yaml_text)
    pipeline_id, _ = ledger.register_pipeline(playbook, yaml_text)
    task_id = ledger.create_task(pipeline_id, "x.png", "d1") or 0
    workdir = tmp_path / "work" / "learn" / f"task_{task_id:06d}"
    workdir.mkdir(parents=True)
    ledger.set_workdir(task_id, workdir)
    ledger.start_attempt(task_id, None, 1)
    ledger.finish_attempt(1, ok=False, last_step_id="util.fail", error="boom")
    bad = json.dumps(
        {
            "branch": {"name": "ai-fix", "steps": [{"id": "not.real"}]},
            "rationale": "bad",
        }
    )
    client = CannedLLMClient([bad, bad])
    suggestion = suggest_branch(client, registry, ledger, task_id, tmp_path / "work")
    assert suggestion.new_playbook is None
    assert suggestion.problems
