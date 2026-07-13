"""CI guarantee for the quickstart: scaffold + run --oneshot."""

from __future__ import annotations

from pathlib import Path

from typer.testing import CliRunner

from ordine.cli.main import app


def test_example_scaffold_runs_oneshot(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg-config"))
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "xdg-data"))
    monkeypatch.chdir(tmp_path)
    demo = tmp_path / "ordine-demo"
    runner = CliRunner()
    result = runner.invoke(app, ["example", "./ordine-demo"])
    assert result.exit_code == 0, result.output
    assert result.stdout.count("cd ordine-demo") == 1
    assert "ordine check png-cleanup.yml" in result.stdout
    assert "ordine run png-cleanup.yml --oneshot" in result.stdout
    assert "ordine serve" in result.stdout
    assert (demo / "png-cleanup.yml").is_file()
    assert len(list((demo / "samples").glob("img_*.png"))) == 6

    check = runner.invoke(app, ["check", "./ordine-demo/png-cleanup.yml"])
    assert check.exit_code == 0, check.output

    run = runner.invoke(app, ["run", "./ordine-demo/png-cleanup.yml", "--oneshot"])
    assert run.exit_code == 0, run.output

    monkeypatch.setattr("uvicorn.run", lambda *_args, **_kwargs: None)
    serve = runner.invoke(app, ["serve"])
    assert serve.exit_code == 0, serve.output

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
