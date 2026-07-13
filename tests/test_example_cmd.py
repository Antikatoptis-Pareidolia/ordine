"""CI guarantee for the quickstart: scaffold + run --oneshot."""

from __future__ import annotations

from pathlib import Path

from typer.testing import CliRunner

from ordine.cli.main import app


def test_example_scaffold_runs_oneshot(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg-config"))
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "xdg-data"))
    demo = tmp_path / "demo"
    runner = CliRunner()
    result = runner.invoke(app, ["example", str(demo)])
    assert result.exit_code == 0, result.output
    assert result.stdout.count(f"cd {demo}") == 1
    assert "ordine check png-cleanup.yml" in result.stdout
    assert "ordine run png-cleanup.yml --oneshot" in result.stdout
    assert "ordine serve" in result.stdout
    assert (demo / "png-cleanup.yml").is_file()
    assert len(list((demo / "samples").glob("img_*.png"))) == 6

    config = tmp_path / "ordine.toml"
    init = runner.invoke(app, ["init", "--config", str(config)])
    assert init.exit_code == 0, init.output

    run = runner.invoke(
        app,
        ["--config", str(config), "run", "--oneshot", str(demo / "png-cleanup.yml")],
    )
    assert run.exit_code == 0, run.output
    exports = sorted((demo / "exports").glob("*.png"))
    assert len(exports) == 6
    assert {path.name for path in exports} == {
        "goat.png",
        "jug.png",
        "crown.png",
        "ring.png",
        "sword.png",
        "shield.png",
    }
