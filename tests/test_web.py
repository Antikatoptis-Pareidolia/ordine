"""Web UI route tests using Starlette TestClient."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from conveyor.core.config import load_config
from conveyor.core.db import create_engine_for, init_db
from conveyor.core.engines import EngineRegistry, HeadlessEngine
from conveyor.core.ledger import Ledger
from conveyor.core.playbook import ManualTrigger, loads_playbook
from conveyor.core.registry import StepRegistry
from conveyor.core.runner import PipelineRunner
from conveyor.core.triggers import ManualScanService, ledger_sink
from conveyor.web.app import create_app
from tests.test_runner_e2e import ASSET_NAMES, _game_assets_yaml, _seed_images, _write_manifest

POST_HEADERS = {"HX-Request": "true", "Origin": "http://127.0.0.1:8484"}


def _write_config(tmp_path: Path) -> Path:
    config_file = tmp_path / "config.toml"
    config_file.write_text(
        f"""[paths]
db = "{tmp_path / "conveyor.sqlite3"}"
workdir_root = "{tmp_path / "workdirs"}"

[web]
host = "127.0.0.1"
port = 8484
autostart_pipelines = false
""",
        encoding="utf-8",
    )
    return config_file


def _flagged_yaml(*, watch: Path) -> str:
    return f"""version: 1
name: web-flagged
trigger:
  type: manual
  path: {watch}
  glob: "*.png"
on_failure:
  retries: 0
  then: mark_failed
  branches:
    - name: b1
      steps:
        - util.fail:
            message: branch one
    - name: b2
      steps:
        - util.fail:
            message: branch two
steps:
  - util.fail:
      message: always fails
"""


def _seed_web_db(tmp_path: Path) -> tuple[int, int, int, int]:
    """Return pipeline_id, done_id, flagged_id, pending_id."""
    config_file = _write_config(tmp_path)
    config = load_config(config_file)
    engine = create_engine_for(config.db_path)
    init_db(engine)
    ledger = Ledger(engine)
    registry = StepRegistry.load()
    engines = EngineRegistry()
    engines.register(HeadlessEngine())

    watch = tmp_path / "in"
    manifest = tmp_path / "assets.csv"
    output = tmp_path / "out"
    _seed_images(watch, corrupt_ordinals=set())
    _write_manifest(manifest, ASSET_NAMES[:1])
    yaml_text = _game_assets_yaml(watch=watch, manifest=manifest, output=output)
    playbook = loads_playbook(yaml_text)
    pipeline_id, version = ledger.register_pipeline(playbook, yaml_text)
    runner = PipelineRunner(
        ledger=ledger,
        registry=registry,
        engines=engines,
        playbook=playbook,
        pipeline_id=pipeline_id,
        workdir_root=config.workdir_root,
        playbook_version=version,
    )
    trigger = playbook.trigger
    assert isinstance(trigger, ManualTrigger)
    ManualScanService(trigger, playbook.dedup, ledger_sink(ledger, pipeline_id)).run()
    runner.run_until_idle()
    done_id = ledger.list_tasks(pipeline_id, status="done", limit=1)[0].id

    flagged_watch = tmp_path / "flagged"
    flagged_watch.mkdir()
    (flagged_watch / "item.png").write_bytes(b"not-a-real-png")
    flagged_yaml = _flagged_yaml(watch=flagged_watch)
    flagged_playbook = loads_playbook(flagged_yaml)
    flagged_pipe_id, flagged_version = ledger.register_pipeline(flagged_playbook, flagged_yaml)
    flagged_runner = PipelineRunner(
        ledger=ledger,
        registry=registry,
        engines=engines,
        playbook=flagged_playbook,
        pipeline_id=flagged_pipe_id,
        workdir_root=config.workdir_root,
        playbook_version=flagged_version,
    )
    flagged_trigger = flagged_playbook.trigger
    assert isinstance(flagged_trigger, ManualTrigger)
    ManualScanService(
        flagged_trigger, flagged_playbook.dedup, ledger_sink(ledger, flagged_pipe_id)
    ).run()
    flagged_runner.run_until_idle()
    flagged_id = ledger.list_tasks(flagged_pipe_id, status="flagged", limit=1)[0].id

    pending_id = ledger.create_task(
        pipeline_id, str(watch / "pending.png"), dedup_key="pending-only"
    )
    assert pending_id is not None
    return pipeline_id, done_id, flagged_id, pending_id


@pytest.fixture
def web_env(tmp_path: Path) -> tuple[TestClient, tuple[int, int, int, int]]:
    ids = _seed_web_db(tmp_path)
    config = load_config(_write_config(tmp_path))
    client = TestClient(create_app(config))
    return client, ids


@pytest.fixture
def web_client(web_env: tuple[TestClient, tuple[int, int, int, int]]) -> TestClient:
    return web_env[0]


@pytest.fixture
def seeded_ids(web_env: tuple[TestClient, tuple[int, int, int, int]]) -> tuple[int, int, int, int]:
    return web_env[1]


def test_dashboard_and_partials(web_client: TestClient) -> None:
    home = web_client.get("/")
    assert home.status_code == 200
    assert "Dashboard" in home.text
    assert "chip-done" in home.text or "done" in home.text
    partial = web_client.get("/partials/pipelines")
    assert partial.status_code == 200
    assert "pipeline-card" in partial.text


def test_register_valid_flagship_redirects(web_client: TestClient) -> None:
    yaml_text = Path("tests/fixtures/playbooks/valid/v02_flagship.yml").read_text(encoding="utf-8")
    response = web_client.post(
        "/pipelines",
        data={"yaml_text": yaml_text},
        headers=POST_HEADERS,
        follow_redirects=False,
    )
    assert response.status_code == 303
    home = web_client.get("/")
    assert "png-cleanup" in home.text


def test_register_invalid_shows_problems(web_client: TestClient) -> None:
    bad = """version: 1
name: bad-web
trigger:
  type: manual
  path: /tmp
steps:
  - id: unknown.step
"""
    response = web_client.post(
        "/pipelines",
        data={"yaml_text": bad},
        headers=POST_HEADERS,
    )
    assert response.status_code == 200
    assert "steps.0.id" in response.text
    assert "unknown step id" in response.text


def test_task_detail_and_flags_order(
    web_client: TestClient, seeded_ids: tuple[int, int, int, int]
) -> None:
    _pipeline_id, done_id, flagged_id, _pending = seeded_ids
    detail = web_client.get(f"/tasks/{done_id}")
    assert detail.status_code == 200
    assert "Steps" in detail.text
    flagged_detail = web_client.get(f"/tasks/{flagged_id}")
    assert flagged_detail.status_code == 200
    flags = web_client.get("/flags")
    assert flags.status_code == 200
    assert "Flags inbox" in flags.text


def test_retry_flagged_and_illegal_done(
    web_client: TestClient, seeded_ids: tuple[int, int, int, int]
) -> None:
    _pipeline_id, done_id, flagged_id, _pending = seeded_ids
    ledger = web_client.app.state.ledger
    flagged_pipe = ledger.get_task(flagged_id).pipeline_id
    ok = web_client.post(
        f"/tasks/{flagged_id}/retry",
        headers=POST_HEADERS,
        follow_redirects=False,
    )
    assert ok.status_code == 303
    task = web_client.get(f"/pipelines/{flagged_pipe}/tasks?status=pending")
    assert str(flagged_id) in task.text
    bad = web_client.post(
        f"/tasks/{done_id}/retry",
        headers=POST_HEADERS,
        follow_redirects=False,
    )
    assert bad.status_code == 303
    follow = web_client.get(bad.headers["location"])
    assert "illegal transition" in follow.text


def test_cancel_pending(web_client: TestClient, seeded_ids: tuple[int, int, int, int]) -> None:
    pipeline_id, _done, _flagged, pending_id = seeded_ids
    response = web_client.post(
        f"/tasks/{pending_id}/cancel",
        headers=POST_HEADERS,
        follow_redirects=False,
    )
    assert response.status_code == 303
    tasks = web_client.get(f"/pipelines/{pipeline_id}/tasks?status=skipped")
    assert str(pending_id) in tasks.text


def test_artifact_and_traversal(
    web_client: TestClient, seeded_ids: tuple[int, int, int, int]
) -> None:
    _pipeline_id, done_id, _flagged, _pending = seeded_ids
    ledger = web_client.app.state.ledger
    task = ledger.get_task(done_id)
    assert task.workdir is not None
    workdir = Path(task.workdir)
    task_json = json.loads((workdir / "task.json").read_text(encoding="utf-8"))
    rel: str | None = None
    for step in task_json.get("steps", []):
        if step.get("output"):
            out = Path(step["output"])
            try:
                rel = str(out.resolve().relative_to(workdir.resolve()))
                break
            except ValueError:
                continue
    if rel:
        ok = web_client.get(f"/artifacts/{done_id}/{rel}")
        assert ok.status_code == 200
        assert ok.headers["content-type"].startswith("image/")
    for bad_path, codes in (
        ("../../../../etc/passwd", {404}),
        ("/etc/passwd", {404, 422}),
        ("../task.json", {404, 422}),
    ):
        blocked = web_client.get(f"/artifacts/{done_id}/{bad_path}")
        assert blocked.status_code in codes


def test_post_forbidden_without_hx_or_foreign_origin(
    web_client: TestClient, seeded_ids: tuple[int, int, int, int]
) -> None:
    _pipeline_id, done_id, _flagged, _pending = seeded_ids
    assert web_client.post(f"/tasks/{done_id}/retry").status_code == 403
    assert (
        web_client.post(
            f"/tasks/{done_id}/retry",
            headers={"Origin": "http://evil.example"},
        ).status_code
        == 403
    )


def test_settings_write_back(tmp_path: Path) -> None:
    _seed_web_db(tmp_path)
    config = load_config(_write_config(tmp_path))
    client = TestClient(create_app(config))
    config_path = _write_config(tmp_path)
    before = config_path.read_text(encoding="utf-8")
    response = client.post(
        "/settings",
        data={
            "stale_after_minutes": "20",
            "reconcile_policy": "retry",
            "web_host": "127.0.0.1",
            "web_port": "8484",
        },
        headers=POST_HEADERS,
    )
    assert response.status_code == 200
    assert "Settings saved" in response.text
    after = config_path.read_text(encoding="utf-8")
    assert "20" in after
    assert before != after
    invalid = client.post(
        "/settings",
        data={
            "stale_after_minutes": "0",
            "reconcile_policy": "retry",
            "web_host": "127.0.0.1",
            "web_port": "8484",
        },
        headers=POST_HEADERS,
    )
    assert "at least 1" in invalid.text
    assert "20" in config_path.read_text(encoding="utf-8")
