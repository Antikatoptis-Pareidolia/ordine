"""Host allowlist vs bind policy tests (S2/A3)."""

from __future__ import annotations

from pathlib import Path

from fastapi.testclient import TestClient

from ordine.core.config import load_config
from ordine.web.app import create_app
from ordine.web.security import _host_is_allowed


def test_host_is_allowed_never_widens_on_wildcard() -> None:
    assert _host_is_allowed("192.168.1.10", "0.0.0.0") is False
    assert _host_is_allowed("10.0.0.5", "::") is False
    assert _host_is_allowed("127.0.0.1", "0.0.0.0") is False
    assert _host_is_allowed("localhost", ["127.0.0.1", "localhost"]) is True
    assert _host_is_allowed("127.0.0.1", ["127.0.0.1"]) is True
    assert _host_is_allowed("evil.example", ["127.0.0.1"]) is False
    assert _host_is_allowed("mybox.local", ["mybox.local", "127.0.0.1"]) is True


def test_settings_refuses_wildcard_allowed_hosts(tmp_path: Path) -> None:
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
    client = TestClient(create_app(config))
    headers = {"HX-Request": "true", "Origin": "http://testserver"}
    response = client.post(
        "/settings",
        data={
            "stale_after_minutes": "15",
            "reconcile_policy": "retry",
            "web_bind": "0.0.0.0",
            "web_allowed_hosts": "0.0.0.0",
            "web_port": "8484",
        },
        headers=headers,
    )
    assert response.status_code == 200
    assert "Refusing to save" in response.text
    assert "0.0.0.0" in response.text
    # Disk must remain unchanged for allowed_hosts
    reloaded = load_config(config_file)
    assert reloaded.web_allowed_hosts == ("testserver",)


def test_settings_bind_change_flashes_restart(tmp_path: Path) -> None:
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
    client = TestClient(create_app(config))
    headers = {"HX-Request": "true", "Origin": "http://testserver"}
    response = client.post(
        "/settings",
        data={
            "stale_after_minutes": "15",
            "reconcile_policy": "retry",
            "web_bind": "127.0.0.1",
            "web_allowed_hosts": "testserver, localhost",
            "web_port": "8484",
        },
        headers=headers,
    )
    assert response.status_code == 200
    assert "Settings saved" in response.text
    assert "restart" in response.text.lower()
    reloaded = load_config(config_file)
    assert reloaded.web_bind == "127.0.0.1"
    assert "localhost" in reloaded.web_allowed_hosts
