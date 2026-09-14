"""S1: refuse non-loopback bind without explicit ack."""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from typer.testing import CliRunner

from ordine.cli.main import app
from ordine.core.config import is_loopback_bind, load_config
from ordine.web.app import create_app


def test_is_loopback_bind_helpers() -> None:
    assert is_loopback_bind("127.0.0.1")
    assert is_loopback_bind("localhost")
    assert is_loopback_bind("::1")
    assert is_loopback_bind("[::1]")
    assert not is_loopback_bind("0.0.0.0")
    assert not is_loopback_bind("192.168.1.10")


def test_serve_refuses_non_loopback_without_ack(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = tmp_path / "config.toml"
    config.write_text(
        f"""[paths]
db = "{tmp_path / "ordine.sqlite3"}"
workdir_root = "{tmp_path / "work"}"

[web]
bind = "0.0.0.0"
allowed_hosts = ["127.0.0.1", "localhost"]
port = 18484
i_understand_no_auth = false
""",
        encoding="utf-8",
    )
    runner = CliRunner()
    result = runner.invoke(app, ["--config", str(config), "serve"])
    assert result.exit_code == 2
    assert "Refusing to bind" in result.output


def test_settings_refuses_non_loopback_without_ack(tmp_path: Path) -> None:
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
    client = TestClient(create_app(load_config(config_file)))
    headers = {"HX-Request": "true", "Origin": "http://testserver"}
    denied = client.post(
        "/settings",
        data={
            "stale_after_minutes": "15",
            "reconcile_policy": "retry",
            "web_bind": "0.0.0.0",
            "web_allowed_hosts": "testserver, localhost",
            "web_port": "8484",
        },
        headers=headers,
    )
    assert denied.status_code == 200
    assert "Refusing non-loopback bind" in denied.text

    allowed = client.post(
        "/settings",
        data={
            "stale_after_minutes": "15",
            "reconcile_policy": "retry",
            "web_bind": "0.0.0.0",
            "web_allowed_hosts": "testserver, localhost",
            "web_port": "8484",
            "i_understand_no_auth": "on",
        },
        headers=headers,
    )
    assert allowed.status_code == 200
    assert "Settings saved" in allowed.text
    reloaded = load_config(config_file)
    assert reloaded.web_bind == "0.0.0.0"
    assert reloaded.i_understand_no_auth is True
