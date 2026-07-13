"""Tests for application configuration load and defaults."""

from __future__ import annotations

from pathlib import Path

import pytest

from ordine.core.config import load_config, write_default_config
from ordine.core.errors import ConfigError


def test_load_defaults_without_file(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    monkeypatch.delenv("ORDINE_CONFIG", raising=False)
    config = load_config()
    assert config.stale_after_minutes == 15
    assert config.reconcile_policy == "retry"
    assert config.log_level == "INFO"
    assert config.db_path.name == "ordine.sqlite3"
    assert config.workdir_root.name == "workdirs"


def test_explicit_config_precedence(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    explicit = tmp_path / "explicit.toml"
    explicit.write_text(
        f"""[paths]
db = "{tmp_path / "one.sqlite3"}"
workdir_root = "{tmp_path / "work-one"}"
""",
        encoding="utf-8",
    )
    env_file = tmp_path / "env.toml"
    env_file.write_text(
        f"""[paths]
db = "{tmp_path / "two.sqlite3"}"
workdir_root = "{tmp_path / "work-two"}"
""",
        encoding="utf-8",
    )
    monkeypatch.setenv("ORDINE_CONFIG", str(env_file))
    config = load_config(explicit)
    assert config.db_path == tmp_path / "one.sqlite3"
    assert config.workdir_root == tmp_path / "work-one"


def test_env_config_when_no_explicit(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    env_file = tmp_path / "env.toml"
    env_file.write_text(
        f"""[paths]
db = "{tmp_path / "env.sqlite3"}"
workdir_root = "{tmp_path / "work-env"}"
""",
        encoding="utf-8",
    )
    monkeypatch.setenv("ORDINE_CONFIG", str(env_file))
    config = load_config()
    assert config.db_path == tmp_path / "env.sqlite3"


def test_missing_explicit_or_env_config_raises(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    explicit = tmp_path / "missing-explicit.toml"
    with pytest.raises(ConfigError, match=f"config file not found: {explicit}"):
        load_config(explicit)

    env_file = tmp_path / "missing-env.toml"
    monkeypatch.setenv("ORDINE_CONFIG", str(env_file))
    with pytest.raises(ConfigError, match=f"config file not found: {env_file}"):
        load_config()


def test_unknown_key_raises_config_error(tmp_path: Path) -> None:
    path = tmp_path / "bad.toml"
    path.write_text(
        """[paths]
db = "/tmp/db.sqlite3"
extra = "nope"
""",
        encoding="utf-8",
    )
    with pytest.raises(ConfigError, match="unknown config keys"):
        load_config(path)


def test_write_default_config_refuses_overwrite(tmp_path: Path) -> None:
    path = tmp_path / "config.toml"
    path.write_text("[log]\nlevel = 'INFO'\n", encoding="utf-8")
    with pytest.raises(ConfigError, match="already exists"):
        write_default_config(path)


def test_write_default_config_round_trip(tmp_path: Path) -> None:
    path = tmp_path / "config.toml"
    write_default_config(path)
    config = load_config(path)
    assert config.stale_after_minutes == 15
    assert config.reconcile_policy == "retry"
    assert config.log_level == "INFO"


def test_paths_expanduser(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))
    path = tmp_path / "config.toml"
    path.write_text(
        """[paths]
db = "~/custom.db"
workdir_root = "~/custom-workdirs"
""",
        encoding="utf-8",
    )
    config = load_config(path)
    assert config.db_path == tmp_path / "custom.db"
    assert config.workdir_root == tmp_path / "custom-workdirs"
