"""Tests for application configuration load and defaults."""

from __future__ import annotations

from pathlib import Path

import pytest

from ordine.core.config import (
    load_config,
    save_llm_settings,
    save_web_runner_settings,
    write_default_config,
)
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


@pytest.mark.parametrize("section", ["paths", "runner", "log", "web", "llm", "retention"])
def test_malformed_section_is_rejected(tmp_path: Path, section: str) -> None:
    path = tmp_path / f"bad-{section}.toml"
    path.write_text(f'{section} = "not-a-table"\n', encoding="utf-8")
    with pytest.raises(ConfigError, match=section):
        load_config(path)


@pytest.mark.parametrize(
    ("body", "message"),
    [
        ("[runner]\nstale_after_minutes = 'soon'\n", "stale_after_minutes"),
        ("[runner]\nreconcile_policy = 'maybe'\n", "reconcile_policy"),
        ("[log]\nlevel = 3\n", "log.level"),
        ("[web]\nhost = 3\n", "web.host"),
        ("[web]\nport = '8484'\n", "web.port"),
        ("[web]\nautostart_pipelines = 'yes'\n", "autostart_pipelines"),
        ("[llm]\nprovider = 3\n", "llm.provider"),
        ("[llm]\nmodel = 3\n", "llm.model"),
        ("[llm]\nbase_url = 3\n", "llm.base_url"),
        ("[llm]\nmax_tokens = 'many'\n", "llm.max_tokens"),
        ("[llm]\nsession_token_cap = 'many'\n", "session_token_cap"),
        ("[llm]\nsession_image_cap = 'many'\n", "session_image_cap"),
        ("[retention]\ndays = -1\n", "retention.days"),
        ("[retention]\nkeep_failed = 'yes'\n", "retention.keep_failed"),
        ("[retention]\non_serve_start = 'yes'\n", "on_serve_start"),
    ],
)
def test_malformed_config_values_are_actionable(tmp_path: Path, body: str, message: str) -> None:
    path = tmp_path / "bad-value.toml"
    path.write_text(body, encoding="utf-8")
    with pytest.raises(ConfigError, match=message):
        load_config(path)


def test_config_read_errors_are_wrapped(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    path = tmp_path / "unreadable.toml"
    path.write_text("", encoding="utf-8")
    real_read_text = Path.read_text

    def denied(self: Path, *args: object, **kwargs: object) -> str:
        if self == path:
            raise PermissionError("denied")
        return real_read_text(self, *args, **kwargs)

    monkeypatch.setattr(Path, "read_text", denied)
    with pytest.raises(ConfigError, match=r"cannot read config.*denied"):
        load_config(path)


def test_invalid_toml_is_wrapped(tmp_path: Path) -> None:
    path = tmp_path / "invalid.toml"
    path.write_text("[runner\n", encoding="utf-8")
    with pytest.raises(ConfigError, match="invalid TOML"):
        load_config(path)


def test_settings_save_round_trips_preserve_sections(tmp_path: Path) -> None:
    path = tmp_path / "config.toml"
    path.write_text(
        f"""[paths]
db = "{tmp_path / "db.sqlite3"}"
workdir_root = "{tmp_path / "workdirs"}"

[retention]
days = 7
""",
        encoding="utf-8",
    )
    save_web_runner_settings(
        path,
        stale_after_minutes=22,
        reconcile_policy="fail",
        web_host="localhost",
        web_port=9000,
        autostart_pipelines=True,
    )
    save_llm_settings(
        path,
        llm_provider="openai",
        llm_model="gpt-test",
        llm_base_url="https://api.openai.com",
        llm_max_tokens=2048,
        llm_session_token_cap=42_000,
    )

    config = load_config(path)
    assert config.stale_after_minutes == 22
    assert config.reconcile_policy == "fail"
    assert (config.web_host, config.web_port, config.autostart_pipelines) == (
        "localhost",
        9000,
        True,
    )
    assert (config.llm_provider, config.llm_model) == ("openai", "gpt-test")
    assert config.llm_max_tokens == 2048
    assert config.llm_session_token_cap == 42_000
    assert config.retention_days == 7


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
