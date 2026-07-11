"""Application configuration load and defaults.

Owns XDG config paths and TOML parsing. Must never import cli, web, executors, or llm.
"""

from __future__ import annotations

import os
import tomllib
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from conveyor.core.errors import ConfigError

DEFAULT_CONFIG_DIR = (
    Path(os.environ.get("XDG_CONFIG_HOME", str(Path.home() / ".config"))) / "conveyor"
)
DEFAULT_DATA_DIR = (
    Path(os.environ.get("XDG_DATA_HOME", str(Path.home() / ".local" / "share"))) / "conveyor"
)
DEFAULT_CONFIG_FILE = DEFAULT_CONFIG_DIR / "config.toml"


def _config_dir() -> Path:
    return Path(os.environ.get("XDG_CONFIG_HOME", str(Path.home() / ".config"))) / "conveyor"


def _data_dir() -> Path:
    return Path(os.environ.get("XDG_DATA_HOME", str(Path.home() / ".local" / "share"))) / "conveyor"


_ALLOWED_SECTIONS: dict[str, frozenset[str]] = {
    "paths": frozenset({"db", "workdir_root"}),
    "runner": frozenset({"stale_after_minutes", "reconcile_policy"}),
    "log": frozenset({"level"}),
    "web": frozenset({"host", "port", "autostart_pipelines"}),
}


@dataclass(frozen=True)
class AppConfig:
    """Resolved application configuration."""

    db_path: Path
    workdir_root: Path
    stale_after_minutes: int = 15
    reconcile_policy: Literal["retry", "fail"] = "retry"
    log_level: str = "INFO"
    web_host: str = "127.0.0.1"
    web_port: int = 8484
    autostart_pipelines: bool = False
    config_file: Path | None = None


def _default_config() -> AppConfig:
    data_dir = _data_dir()
    return AppConfig(
        db_path=(data_dir / "conveyor.sqlite3").expanduser(),
        workdir_root=(data_dir / "workdirs").expanduser(),
    )


def _validate_keys(raw: dict[str, object]) -> None:
    unknown: list[str] = []
    for section, keys in raw.items():
        if not isinstance(keys, dict):
            unknown.append(section)
            continue
        allowed = _ALLOWED_SECTIONS.get(section)
        if allowed is None:
            unknown.append(section)
            continue
        for key in keys:
            if key not in allowed:
                unknown.append(f"{section}.{key}")
    if unknown:
        raise ConfigError(f"unknown config keys: {', '.join(sorted(unknown))}")


def _parse_config(raw: dict[str, object], *, config_file: Path | None) -> AppConfig:
    _validate_keys(raw)
    defaults = _default_config()
    paths = raw.get("paths", {})
    runner = raw.get("runner", {})
    log = raw.get("log", {})
    web = raw.get("web", {})
    assert isinstance(paths, dict)
    assert isinstance(runner, dict)
    assert isinstance(log, dict)
    assert isinstance(web, dict)

    db_path = Path(str(paths.get("db", defaults.db_path))).expanduser()
    workdir_root = Path(str(paths.get("workdir_root", defaults.workdir_root))).expanduser()

    stale_after = runner.get("stale_after_minutes", defaults.stale_after_minutes)
    if not isinstance(stale_after, int):
        raise ConfigError("runner.stale_after_minutes must be an integer")

    reconcile = runner.get("reconcile_policy", defaults.reconcile_policy)
    if reconcile not in ("retry", "fail"):
        raise ConfigError("runner.reconcile_policy must be 'retry' or 'fail'")

    level = log.get("level", defaults.log_level)
    if not isinstance(level, str):
        raise ConfigError("log.level must be a string")

    web_host = web.get("host", defaults.web_host)
    if not isinstance(web_host, str):
        raise ConfigError("web.host must be a string")

    web_port = web.get("port", defaults.web_port)
    if not isinstance(web_port, int):
        raise ConfigError("web.port must be an integer")

    autostart = web.get("autostart_pipelines", defaults.autostart_pipelines)
    if not isinstance(autostart, bool):
        raise ConfigError("web.autostart_pipelines must be a boolean")

    return AppConfig(
        db_path=db_path,
        workdir_root=workdir_root,
        stale_after_minutes=stale_after,
        reconcile_policy=reconcile,
        log_level=level,
        web_host=web_host,
        web_port=web_port,
        autostart_pipelines=autostart,
        config_file=config_file,
    )


def load_config(explicit: Path | None = None) -> AppConfig:
    """Load config from explicit path, $CONVEYOR_CONFIG, default file, or built-in defaults."""
    path = explicit
    if path is None:
        env = os.environ.get("CONVEYOR_CONFIG")
        path = Path(env).expanduser() if env else _config_dir() / "config.toml"
    if not path.exists():
        return _default_config()
    try:
        raw = tomllib.loads(path.read_text(encoding="utf-8"))
    except OSError as exc:
        raise ConfigError(f"cannot read config {path}: {exc}") from exc
    except tomllib.TOMLDecodeError as exc:
        raise ConfigError(f"invalid TOML in {path}: {exc}") from exc
    if not isinstance(raw, dict):
        raise ConfigError(f"config root must be a table: {path}")
    return _parse_config(raw, config_file=path.expanduser())


def save_web_runner_settings(
    path: Path,
    *,
    stale_after_minutes: int,
    reconcile_policy: str,
    web_host: str,
    web_port: int,
    autostart_pipelines: bool,
) -> None:
    """Atomically update runner and web sections in the config TOML."""
    import tomli_w

    expanded = path.expanduser()
    if not expanded.exists():
        raise ConfigError(f"config file not found: {expanded}")
    raw = tomllib.loads(expanded.read_text(encoding="utf-8"))
    if not isinstance(raw, dict):
        raise ConfigError("config root must be a table")
    _validate_keys(raw)
    runner = dict(raw.get("runner", {}))
    web = dict(raw.get("web", {}))
    runner["stale_after_minutes"] = stale_after_minutes
    runner["reconcile_policy"] = reconcile_policy
    web["host"] = web_host
    web["port"] = web_port
    web["autostart_pipelines"] = autostart_pipelines
    raw["runner"] = runner
    raw["web"] = web
    tmp = expanded.with_suffix(".toml.tmp")
    tmp.write_text(tomli_w.dumps(raw), encoding="utf-8")
    tmp.replace(expanded)


def write_default_config(path: Path) -> None:
    """Write a commented default config template; refuse to overwrite an existing file."""
    expanded = path.expanduser()
    if expanded.exists():
        raise ConfigError(f"config already exists: {expanded}")
    expanded.parent.mkdir(parents=True, exist_ok=True)
    template = f"""# Conveyor application config
# Paths expand ~ at load time.

[paths]
# db = "{_data_dir() / "conveyor.sqlite3"}"
# workdir_root = "{_data_dir() / "workdirs"}"

[runner]
stale_after_minutes = 15
reconcile_policy = "retry"  # retry | fail

[log]
level = "INFO"

[web]
host = "127.0.0.1"
port = 8484
autostart_pipelines = false
"""
    expanded.write_text(template, encoding="utf-8")
