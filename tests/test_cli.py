"""CLI tests using Typer's CliRunner."""

from __future__ import annotations

import json
import signal
import subprocess
import sys
import time
from pathlib import Path

import pytest
from typer.testing import CliRunner

from conveyor.cli.main import app
from conveyor.core.db import create_engine_for, init_db
from conveyor.core.ledger import Ledger
from tests.test_image_steps import make_test_image
from tests.test_runner_e2e import ASSET_NAMES

RUNNER = CliRunner()
FIXTURE_PLAYBOOK = Path("tests/fixtures/playbooks/valid/v02_flagship.yml")


def _write_config(tmp_path: Path) -> Path:
    config_file = tmp_path / "config.toml"
    config_file.write_text(
        f"""[paths]
db = "{tmp_path / "conveyor.sqlite3"}"
workdir_root = "{tmp_path / "workdirs"}"
""",
        encoding="utf-8",
    )
    return config_file


def _invoke(config_file: Path, *args: str, **kwargs: object):
    return RUNNER.invoke(app, ["--config", str(config_file), *args], **kwargs)


def _game_assets_yaml(*, watch: Path, manifest: Path, output: Path, fuzz: int = 8) -> str:
    return f"""version: 1
name: cli-game-assets
trigger:
  type: manual
  path: {watch}
  glob: "*.png"
  ordinal_regex: 'img_(\\d+)\\.png'
steps:
  - image.validate
  - image.white_to_alpha:
      fuzz: {fuzz}
  - image.trim
  - file.rename_from_manifest:
      manifest: {manifest}
  - id: image.export
    params:
      dest: {output}
      use_reserved_name: true
"""


def _seed_five_images(watch: Path) -> None:
    watch.mkdir(parents=True, exist_ok=True)
    for ordinal in range(1, 6):
        path = watch / f"img_{ordinal:04d}.png"
        make_test_image(path)
        path.write_bytes(path.read_bytes() + bytes([ordinal]))


def _write_manifest(path: Path, names: list[str]) -> None:
    rows = "\n".join(names)
    path.write_text(f"name\n{rows}\n", encoding="utf-8")


def test_init_creates_config_and_dirs(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "data"))
    config_file = tmp_path / "config.toml"
    result = RUNNER.invoke(app, ["init", "--config", str(config_file)])
    assert result.exit_code == 0
    assert config_file.exists()
    assert (tmp_path / "data" / "conveyor" / "conveyor.sqlite3").parent.exists()


def test_init_second_time_exits_one(tmp_path: Path) -> None:
    config_file = tmp_path / "config.toml"
    first = RUNNER.invoke(app, ["init", "--config", str(config_file)])
    assert first.exit_code == 0
    second = RUNNER.invoke(app, ["init", "--config", str(config_file)])
    assert second.exit_code == 1


def test_check_valid_playbook(tmp_path: Path) -> None:
    config_file = _write_config(tmp_path)
    result = _invoke(config_file, "check", str(FIXTURE_PLAYBOOK))
    assert result.exit_code == 0
    assert result.stdout.strip() == "png-cleanup: valid (4 steps, trigger=folder_watch)"


def _game_assets_yaml_with_corrupt(*, watch: Path, manifest: Path, output: Path) -> str:
    return f"""version: 1
name: cli-corrupt-mix
trigger:
  type: manual
  path: {watch}
  glob: "*.png"
  ordinal_regex: 'img_(\\d+)\\.png'
steps:
  - image.validate
  - image.white_to_alpha:
      fuzz: 8
  - image.trim
  - file.rename_from_manifest:
      manifest: {manifest}
  - id: image.export
    params:
      dest: {output}
      use_reserved_name: true
"""


def _seed_images_with_corrupt(watch: Path, *, corrupt_ordinals: set[int]) -> None:
    watch.mkdir(parents=True, exist_ok=True)
    for ordinal in range(1, 6):
        path = watch / f"img_{ordinal:04d}.png"
        make_test_image(path)
        if ordinal in corrupt_ordinals:
            path.write_bytes(b"truncated" + bytes([ordinal]))
        else:
            path.write_bytes(path.read_bytes() + bytes([ordinal]))


def test_status_plain_shows_all_nonzero_counts(tmp_path: Path) -> None:
    config_file = _write_config(tmp_path)
    watch = tmp_path / "in"
    manifest = tmp_path / "assets.csv"
    output = tmp_path / "out"
    _seed_images_with_corrupt(watch, corrupt_ordinals={2, 3})
    _write_manifest(manifest, ASSET_NAMES[:5])
    playbook = tmp_path / "playbook.yml"
    playbook.write_text(
        _game_assets_yaml_with_corrupt(watch=watch, manifest=manifest, output=output),
        encoding="utf-8",
    )
    assert _invoke(config_file, "run", str(playbook), "--oneshot").exit_code == 0
    result = _invoke(config_file, "status")
    assert result.exit_code == 0
    line = result.stdout.strip()
    assert "done=3" in line
    assert "skipped=2" in line
    assert "pending=0" not in line


def test_task_plain_shows_attempts_flags_and_error(tmp_path: Path) -> None:
    config_file = _write_config(tmp_path)
    watch = tmp_path / "in"
    manifest = tmp_path / "assets.csv"
    output = tmp_path / "out"
    _seed_images_with_corrupt(watch, corrupt_ordinals={2})
    _write_manifest(manifest, ASSET_NAMES[:5])
    playbook = tmp_path / "playbook.yml"
    playbook.write_text(
        _game_assets_yaml_with_corrupt(watch=watch, manifest=manifest, output=output),
        encoding="utf-8",
    )
    assert _invoke(config_file, "run", str(playbook), "--oneshot").exit_code == 0
    skipped = json.loads(
        _invoke(config_file, "tasks", "cli-corrupt-mix", "--status", "skipped", "--json").stdout
    )
    task_id = skipped["tasks"][0]["id"]
    result = _invoke(config_file, "task", str(task_id))
    assert result.exit_code == 0
    text = result.stdout
    assert "attempts:" in text
    assert "flags:" in text
    assert "corrupt_input" in text
    assert "skip:" in text or "error:" in text

    done = json.loads(
        _invoke(config_file, "tasks", "cli-corrupt-mix", "--status", "done", "--json").stdout
    )
    done_result = _invoke(config_file, "task", str(done["tasks"][0]["id"]))
    assert "attempts:" in done_result.stdout


def test_check_unknown_step_json(tmp_path: Path) -> None:
    config_file = _write_config(tmp_path)
    bad = tmp_path / "bad.yml"
    bad.write_text(
        """version: 1
name: bad
trigger:
  type: manual
  path: /tmp
steps:
  - id: unknown.step
""",
        encoding="utf-8",
    )
    result = _invoke(config_file, "check", str(bad), "--json")
    assert result.exit_code == 1
    payload = json.loads(result.stdout)
    assert payload == {
        "valid": False,
        "problems": [{"path": "steps.0.id", "message": "unknown step id: unknown.step"}],
    }


def test_run_oneshot_game_assets(tmp_path: Path) -> None:
    config_file = _write_config(tmp_path)
    watch = tmp_path / "in"
    manifest = tmp_path / "assets.csv"
    output = tmp_path / "out"
    _seed_five_images(watch)
    _write_manifest(manifest, ASSET_NAMES[:5])
    playbook = tmp_path / "playbook.yml"
    playbook.write_text(
        _game_assets_yaml(watch=watch, manifest=manifest, output=output),
        encoding="utf-8",
    )
    result = _invoke(config_file, "run", str(playbook), "--oneshot", "--json")
    assert result.exit_code == 0
    summary = json.loads(result.stdout)
    assert summary["processed"] == 5
    status = _invoke(config_file, "status", "--json")
    payload = json.loads(status.stdout)
    assert payload["pipelines"][0]["counts"]["done"] == 5
    for name in ASSET_NAMES[:5]:
        assert (output / name).exists()


def test_run_oneshot_exactly_once(tmp_path: Path) -> None:
    config_file = _write_config(tmp_path)
    watch = tmp_path / "in"
    manifest = tmp_path / "assets.csv"
    output = tmp_path / "out"
    _seed_five_images(watch)
    _write_manifest(manifest, ASSET_NAMES[:5])
    playbook = tmp_path / "playbook.yml"
    playbook.write_text(
        _game_assets_yaml(watch=watch, manifest=manifest, output=output),
        encoding="utf-8",
    )
    assert _invoke(config_file, "run", str(playbook), "--oneshot", "--json").exit_code == 0
    second = _invoke(config_file, "run", str(playbook), "--oneshot", "--json")
    assert second.exit_code == 0
    assert json.loads(second.stdout)["processed"] == 0


def test_run_auto_registration_version_churn(tmp_path: Path) -> None:
    config_file = _write_config(tmp_path)
    watch = tmp_path / "in"
    manifest = tmp_path / "assets.csv"
    output = tmp_path / "out"
    _seed_five_images(watch)
    _write_manifest(manifest, ASSET_NAMES[:5])
    playbook = tmp_path / "playbook.yml"
    yaml_text = _game_assets_yaml(watch=watch, manifest=manifest, output=output, fuzz=8)
    playbook.write_text(yaml_text, encoding="utf-8")
    _invoke(config_file, "run", str(playbook), "--oneshot")
    engine = create_engine_for(tmp_path / "conveyor.sqlite3")
    init_db(engine)
    ledger = Ledger(engine)
    pipeline_id = ledger.find_pipeline_id("cli-game-assets")
    assert pipeline_id is not None
    before = len(ledger.list_versions(pipeline_id))
    _invoke(config_file, "run", str(playbook), "--oneshot")
    assert len(ledger.list_versions(pipeline_id)) == before
    playbook.write_text(
        _game_assets_yaml(watch=watch, manifest=manifest, output=output, fuzz=9),
        encoding="utf-8",
    )
    _invoke(config_file, "run", str(playbook), "--oneshot")
    assert len(ledger.list_versions(pipeline_id)) == before + 1


def test_json_stdout_separates_logs_with_verbose(tmp_path: Path) -> None:
    config_file = _write_config(tmp_path)
    result = _invoke(config_file, "-v", "steps", "--json")
    assert result.exit_code == 0
    payload = json.loads(result.stdout)
    assert "steps" in payload
    assert result.stdout.strip().startswith("{")


def test_tasks_task_flags_json_keys(tmp_path: Path) -> None:
    config_file = _write_config(tmp_path)
    watch = tmp_path / "in"
    manifest = tmp_path / "assets.csv"
    output = tmp_path / "out"
    _seed_five_images(watch)
    _write_manifest(manifest, ASSET_NAMES[:5])
    playbook = tmp_path / "playbook.yml"
    playbook.write_text(
        _game_assets_yaml(watch=watch, manifest=manifest, output=output),
        encoding="utf-8",
    )
    _invoke(config_file, "run", str(playbook), "--oneshot")
    tasks = json.loads(_invoke(config_file, "tasks", "cli-game-assets", "--json").stdout)
    assert set(tasks.keys()) == {"pipeline", "tasks"}
    assert set(tasks["tasks"][0].keys()) == {
        "id",
        "ordinal",
        "status",
        "source",
        "updated_at",
    }
    task_id = tasks["tasks"][0]["id"]
    task = json.loads(_invoke(config_file, "task", str(task_id), "--json").stdout)
    assert set(task.keys()) == {
        "id",
        "pipeline_id",
        "status",
        "ordinal",
        "source_ref",
        "workdir",
        "current_branch",
        "attempts",
        "error",
        "created_at",
        "updated_at",
        "branch_attempts",
        "flags",
    }
    flags = json.loads(_invoke(config_file, "flags", "--json").stdout)
    assert set(flags.keys()) == {"flags"}


def test_retry_done_illegal_and_flagged_pending(tmp_path: Path) -> None:
    config_file = _write_config(tmp_path)
    watch = tmp_path / "in"
    manifest = tmp_path / "assets.csv"
    output = tmp_path / "out"
    _seed_five_images(watch)
    _write_manifest(manifest, ASSET_NAMES[:5])
    playbook = tmp_path / "playbook.yml"
    playbook.write_text(
        _game_assets_yaml(watch=watch, manifest=manifest, output=output),
        encoding="utf-8",
    )
    _invoke(config_file, "run", str(playbook), "--oneshot")
    tasks = json.loads(_invoke(config_file, "tasks", "cli-game-assets", "--json").stdout)
    done_id = next(item["id"] for item in tasks["tasks"] if item["status"] == "done")
    illegal = _invoke(config_file, "retry", str(done_id))
    assert illegal.exit_code == 1
    assert "illegal transition" in illegal.stderr

    extra = watch / "img_0006.png"
    make_test_image(extra)
    _invoke(config_file, "run", str(playbook), "--oneshot")
    flagged = json.loads(
        _invoke(config_file, "tasks", "cli-game-assets", "--status", "flagged", "--json").stdout
    )
    flagged_id = flagged["tasks"][0]["id"]
    ok = _invoke(config_file, "retry", str(flagged_id), "--json")
    assert ok.exit_code == 0
    assert json.loads(ok.stdout)["status"] == "pending"


@pytest.mark.integration
def test_run_sigint_graceful_stop(tmp_path: Path) -> None:
    config_file = _write_config(tmp_path)
    watch = tmp_path / "watch"
    manifest = tmp_path / "assets.csv"
    output = tmp_path / "out"
    watch.mkdir()
    _write_manifest(manifest, ASSET_NAMES[:1])
    playbook = tmp_path / "watch.yml"
    playbook.write_text(
        f"""version: 1
name: cli-watch
trigger:
  type: folder_watch
  path: {watch}
  glob: "*.png"
  ordinal_regex: 'img_(\\d+)\\.png'
  settle_seconds: 0.5
steps:
  - image.validate
  - file.rename_from_manifest:
      manifest: {manifest}
  - id: image.export
    params:
      dest: {output}
      use_reserved_name: true
""",
        encoding="utf-8",
    )
    proc = subprocess.Popen(
        [
            sys.executable,
            "-m",
            "conveyor.cli.main",
            "--config",
            str(config_file),
            "run",
            str(playbook),
        ],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    try:
        path = watch / "img_0001.png"
        make_test_image(path)
        path.write_bytes(path.read_bytes() + b"1")
        deadline = time.monotonic() + 15.0
        while time.monotonic() < deadline:
            status = _invoke(config_file, "status", "--json")
            try:
                if status.exit_code != 0:
                    raise ValueError("status not ready")
                pipelines = json.loads(status.stdout)["pipelines"]
                if not pipelines:
                    raise ValueError("pipeline not registered")
                counts = pipelines[0]["counts"]
            except (ValueError, json.JSONDecodeError, KeyError, IndexError, TypeError):
                time.sleep(0.2)
                continue
            if counts.get("done", 0) >= 1:
                break
            time.sleep(0.2)
        proc.send_signal(signal.SIGINT)
        try:
            stdout, stderr = proc.communicate(timeout=5)
        except subprocess.TimeoutExpired as exc:
            proc.kill()
            stdout, stderr = proc.communicate()
            raise AssertionError(
                f"process did not exit within 5s after SIGINT\nstdout:\n{stdout}\nstderr:\n{stderr}"
            ) from exc
        if proc.returncode != 0:
            raise AssertionError(
                f"unexpected exit code {proc.returncode}\nstdout:\n{stdout}\nstderr:\n{stderr}"
            )
    finally:
        if proc.poll() is None:
            proc.kill()


def test_cli_dry_run_json_leaves_prod_db_untouched(tmp_path: Path) -> None:
    import sqlite3

    from conveyor.core.db import create_engine_for, init_db

    config_file = _write_config(tmp_path)
    samples = tmp_path / "samples"
    samples.mkdir()
    (samples / "one.txt").write_text("data", encoding="utf-8")
    playbook_file = tmp_path / "dryrun.yml"
    playbook_file.write_text(
        """version: 1
name: cli-dry-run
trigger: {type: manual, path: ~/in}
steps:
  - util.copy
  - util.fail: {message: boom, times: -1}
""",
        encoding="utf-8",
    )
    db_path = tmp_path / "conveyor.sqlite3"
    engine = create_engine_for(db_path)
    init_db(engine)
    with sqlite3.connect(db_path) as conn:
        before = conn.execute("SELECT COUNT(*) FROM pipelines").fetchone()[0]

    result = _invoke(
        config_file,
        "dry-run",
        str(playbook_file),
        "--sample",
        str(samples),
        "--json",
    )
    assert result.exit_code == 1
    payload = json.loads(result.stdout)
    assert payload["tasks"][0]["steps"][-1]["status"] == "fail"

    with sqlite3.connect(db_path) as conn:
        after = conn.execute("SELECT COUNT(*) FROM pipelines").fetchone()[0]
    assert before == after


def test_cli_dry_run_no_ordinal_failure_message_table_and_json(tmp_path: Path) -> None:
    samples = tmp_path / "samples"
    samples.mkdir()
    manifest = tmp_path / "assets.csv"
    manifest.write_text("name\ngoat.png\n", encoding="utf-8")
    (samples / "input.png").write_bytes(b"\x89PNG\r\n")
    playbook_file = tmp_path / "ordinal-less.yml"
    playbook_file.write_text(
        f"""version: 1
name: ordinal-less
trigger:
  type: manual
  path: ~/in
steps:
  - util.noop
  - util.noop
  - util.noop
  - file.rename_from_manifest:
      manifest: {manifest}
""",
        encoding="utf-8",
    )
    config_file = _write_config(tmp_path)

    table = _invoke(
        config_file,
        "dry-run",
        str(playbook_file),
        "--sample",
        str(samples),
    )
    assert table.exit_code == 1
    assert "task has no ordinal" in table.stdout

    payload = json.loads(
        _invoke(
            config_file,
            "dry-run",
            str(playbook_file),
            "--sample",
            str(samples),
            "--json",
        ).stdout
    )
    step_four = payload["tasks"][0]["steps"][3]
    assert step_four["id"] == "file.rename_from_manifest"
    assert "task has no ordinal" in step_four["message"]
