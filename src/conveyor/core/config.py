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
}


@dataclass(frozen=True)
class AppConfig:
    """Resolved application configuration."""

    db_path: Path
    workdir_root: Path
    stale_after_minutes: int = 15
    reconcile_policy: Literal["retry", "fail"] = "retry"
    log_level: str = "INFO"


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


def _parse_config(raw: dict[str, object]) -> AppConfig:
    _validate_keys(raw)
    defaults = _default_config()
    paths = raw.get("paths", {})
    runner = raw.get("runner", {})
    log = raw.get("log", {})
    assert isinstance(paths, dict)
    assert isinstance(runner, dict)
    assert isinstance(log, dict)

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

    return AppConfig(
        db_path=db_path,
        workdir_root=workdir_root,
        stale_after_minutes=stale_after,
        reconcile_policy=reconcile,
        log_level=level,
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
    return _parse_config(raw)


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
"""
    expanded.write_text(template, encoding="utf-8")
