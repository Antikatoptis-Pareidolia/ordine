"""A6: worker heartbeat visibility in status --json."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

from typer.testing import CliRunner

from ordine.cli.main import app
from ordine.core.heartbeat import write_heartbeat


def test_status_json_includes_workers(tmp_path: Path) -> None:
    config = tmp_path / "config.toml"
    db = tmp_path / "ordine.sqlite3"
    work = tmp_path / "work"
    config.write_text(
        f"""[paths]
db = "{db}"
workdir_root = "{work}"
""",
        encoding="utf-8",
    )
    write_heartbeat(
        db,
        pipeline_id=7,
        pipeline_name="demo",
        alive=True,
        last_activity=datetime(2026, 1, 2, 3, 4, 5, tzinfo=UTC),
        stop_failed=False,
        pid=1,
    )
    runner = CliRunner()
    result = runner.invoke(app, ["--config", str(config), "status", "--json"])
    assert result.exit_code == 0, result.output
    assert '"workers"' in result.output
    assert '"pipeline_id": 7' in result.output or '"pipeline_id":7' in result.output
    assert "demo" in result.output
