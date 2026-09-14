"""Application configuration load and defaults.

Owns XDG config paths and TOML parsing. Must never import cli, web, executors, or llm.
"""

from __future__ import annotations

import os
import tomllib
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from ordine.core.errors import ConfigError


def _config_dir() -> Path:
    return Path(os.environ.get("XDG_CONFIG_HOME", str(Path.home() / ".config"))) / "ordine"


def _data_dir() -> Path:
    return Path(os.environ.get("XDG_DATA_HOME", str(Path.home() / ".local" / "share"))) / "ordine"


# Single source of truth is the helper functions; constants are thin aliases for callers that import
# them (public surface).
DEFAULT_CONFIG_DIR = _config_dir()
DEFAULT_DATA_DIR = _data_dir()
DEFAULT_CONFIG_FILE = DEFAULT_CONFIG_DIR / "config.toml"


_ALLOWED_SECTIONS: dict[str, frozenset[str]] = {
    "paths": frozenset({"db", "workdir_root"}),
    "runner": frozenset({"stale_after_minutes", "reconcile_policy"}),
    "log": frozenset({"level"}),
    "web": frozenset({"host", "bind", "allowed_hosts", "port", "autostart_pipelines"}),
    "llm": frozenset(
        {"provider", "model", "base_url", "max_tokens", "session_token_cap", "session_image_cap"}
    ),
    "retention": frozenset({"days", "keep_failed", "on_serve_start"}),
}


WILDCARD_BIND_ADDRESSES = frozenset({"0.0.0.0", "::"})
LOOPBACK_HOST_ALIASES = frozenset({"127.0.0.1", "localhost", "::1"})


def is_wildcard_host(value: str) -> bool:
    """Return True when *value* is a wildcard bind/allowlist address."""
    return value.strip().lower().strip("[]") in WILDCARD_BIND_ADDRESSES


def normalize_allowed_hosts(hosts: list[str] | tuple[str, ...] | str) -> tuple[str, ...]:
    """Normalize a host allowlist from a list or comma-separated string."""
    if isinstance(hosts, str):
        parts = [part.strip() for part in hosts.split(",")]
    else:
        parts = [str(part).strip() for part in hosts]
    cleaned = tuple(part for part in parts if part)
    if not cleaned:
        raise ConfigError("web.allowed_hosts must contain at least one non-empty host")
    return cleaned


def default_allowed_hosts_for_bind(bind: str) -> tuple[str, ...]:
    """Derive a safe Host allowlist for *bind* (never wildcards)."""
    normalized = bind.strip().lower().strip("[]")
    if normalized in WILDCARD_BIND_ADDRESSES:
        return ("127.0.0.1", "localhost")
    if normalized in LOOPBACK_HOST_ALIASES:
        return ("127.0.0.1", "localhost")
    return (bind.strip(),)


@dataclass(frozen=True)
class AppConfig:
    """Resolved application configuration."""

    db_path: Path
    workdir_root: Path
    stale_after_minutes: int = 15
    reconcile_policy: Literal["retry", "fail"] = "retry"
    log_level: str = "INFO"
    web_bind: str = "127.0.0.1"
    web_allowed_hosts: tuple[str, ...] = ("127.0.0.1", "localhost")
    web_port: int = 8484
    autostart_pipelines: bool = False
    llm_provider: str = "none"
    llm_model: str = ""
    llm_base_url: str = ""
    llm_max_tokens: int = 1024
    llm_session_token_cap: int = 200_000
    llm_session_image_cap: int = 200
    retention_days: int = 30
    retention_keep_failed: bool = True
    retention_on_serve_start: bool = False
    config_file: Path | None = None


def _default_config() -> AppConfig:
    data_dir = _data_dir()
    return AppConfig(
        db_path=(data_dir / "ordine.sqlite3").expanduser(),
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
    llm = raw.get("llm", {})
    retention = raw.get("retention", {})
    if not isinstance(paths, dict):
        raise ConfigError("paths section must be a table")
    if not isinstance(runner, dict):
        raise ConfigError("runner section must be a table")
    if not isinstance(log, dict):
        raise ConfigError("log section must be a table")
    if not isinstance(web, dict):
        raise ConfigError("web section must be a table")
    if not isinstance(llm, dict):
        raise ConfigError("llm section must be a table")
    if not isinstance(retention, dict):
        raise ConfigError("retention section must be a table")

    db_path = Path(str(paths.get("db", defaults.db_path))).expanduser()
    workdir_root = Path(str(paths.get("workdir_root", defaults.workdir_root))).expanduser()

    stale_after = runner.get("stale_after_minutes", defaults.stale_after_minutes)
    if not isinstance(stale_after, int) or isinstance(stale_after, bool) or stale_after < 1:
        raise ConfigError("runner.stale_after_minutes must be a positive integer")

    reconcile = runner.get("reconcile_policy", defaults.reconcile_policy)
    if reconcile not in ("retry", "fail"):
        raise ConfigError("runner.reconcile_policy must be 'retry' or 'fail'")

    level = log.get("level", defaults.log_level)
    if not isinstance(level, str):
        raise ConfigError("log.level must be a string")

    # Prefer web.bind; accept legacy web.host as bind fallback.
    raw_bind = web.get("bind", web.get("host", defaults.web_bind))
    if not isinstance(raw_bind, str) or not raw_bind.strip():
        raise ConfigError("web.bind (or legacy web.host) must be a non-empty string")
    web_bind = raw_bind.strip()

    if "allowed_hosts" in web:
        raw_allowed = web["allowed_hosts"]
        if isinstance(raw_allowed, str) or (
            isinstance(raw_allowed, list) and all(isinstance(item, str) for item in raw_allowed)
        ):
            web_allowed_hosts = normalize_allowed_hosts(raw_allowed)
        else:
            raise ConfigError("web.allowed_hosts must be a string or list of strings")
    elif "host" in web and "bind" not in web:
        # Legacy single-field config: derive allowlist from host, never widening wildcards.
        web_allowed_hosts = default_allowed_hosts_for_bind(web_bind)
    else:
        web_allowed_hosts = (
            defaults.web_allowed_hosts
            if "bind" not in web
            else default_allowed_hosts_for_bind(web_bind)
        )

    for host in web_allowed_hosts:
        if is_wildcard_host(host):
            raise ConfigError(
                "web.allowed_hosts must not include wildcard addresses "
                f"{host!r}; use web.bind for listen address and list concrete Host names"
            )

    web_port = web.get("port", defaults.web_port)
    if not isinstance(web_port, int) or isinstance(web_port, bool) or not 1 <= web_port <= 65535:
        raise ConfigError("web.port must be an integer between 1 and 65535")

    autostart = web.get("autostart_pipelines", defaults.autostart_pipelines)
    if not isinstance(autostart, bool):
        raise ConfigError("web.autostart_pipelines must be a boolean")

    llm_provider = llm.get("provider", defaults.llm_provider)
    if not isinstance(llm_provider, str) or llm_provider not in {
        "none",
        "anthropic",
        "openai",
        "openai_compatible",
    }:
        raise ConfigError(
            "llm.provider must be 'none', 'anthropic', 'openai', or 'openai_compatible'"
        )

    llm_model = llm.get("model", defaults.llm_model)
    if not isinstance(llm_model, str):
        raise ConfigError("llm.model must be a string")

    llm_base_url = llm.get("base_url", defaults.llm_base_url)
    if not isinstance(llm_base_url, str):
        raise ConfigError("llm.base_url must be a string")

    llm_max_tokens = llm.get("max_tokens", defaults.llm_max_tokens)
    if (
        not isinstance(llm_max_tokens, int)
        or isinstance(llm_max_tokens, bool)
        or llm_max_tokens < 1
    ):
        raise ConfigError("llm.max_tokens must be a positive integer")

    llm_session_token_cap = llm.get("session_token_cap", defaults.llm_session_token_cap)
    if (
        not isinstance(llm_session_token_cap, int)
        or isinstance(llm_session_token_cap, bool)
        or llm_session_token_cap < 1
    ):
        raise ConfigError("llm.session_token_cap must be a positive integer")

    llm_session_image_cap = llm.get("session_image_cap", defaults.llm_session_image_cap)
    if (
        not isinstance(llm_session_image_cap, int)
        or isinstance(llm_session_image_cap, bool)
        or llm_session_image_cap < 1
    ):
        raise ConfigError("llm.session_image_cap must be a positive integer")

    retention_days = retention.get("days", defaults.retention_days)
    if (
        not isinstance(retention_days, int)
        or isinstance(retention_days, bool)
        or retention_days < 0
    ):
        raise ConfigError("retention.days must be a non-negative integer")

    retention_keep_failed = retention.get("keep_failed", defaults.retention_keep_failed)
    if not isinstance(retention_keep_failed, bool):
        raise ConfigError("retention.keep_failed must be a boolean")

    retention_on_serve_start = retention.get("on_serve_start", defaults.retention_on_serve_start)
    if not isinstance(retention_on_serve_start, bool):
        raise ConfigError("retention.on_serve_start must be a boolean")

    return AppConfig(
        db_path=db_path,
        workdir_root=workdir_root,
        stale_after_minutes=stale_after,
        reconcile_policy=reconcile,
        log_level=level,
        web_bind=web_bind,
        web_allowed_hosts=web_allowed_hosts,
        web_port=web_port,
        autostart_pipelines=autostart,
        llm_provider=llm_provider,
        llm_model=llm_model,
        llm_base_url=llm_base_url,
        llm_max_tokens=llm_max_tokens,
        llm_session_token_cap=llm_session_token_cap,
        llm_session_image_cap=llm_session_image_cap,
        retention_days=retention_days,
        retention_keep_failed=retention_keep_failed,
        retention_on_serve_start=retention_on_serve_start,
        config_file=config_file,
    )


def load_config(explicit: Path | None = None) -> AppConfig:
    """Load config from explicit path, $ORDINE_CONFIG, default file, or built-in defaults."""
    env = os.environ.get("ORDINE_CONFIG")
    path = explicit
    if path is None:
        path = Path(env).expanduser() if env else _config_dir() / "config.toml"
    if not path.exists():
        if explicit is not None or env:
            raise ConfigError(f"config file not found: {path}")
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
    web_bind: str,
    web_allowed_hosts: list[str] | tuple[str, ...] | str,
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
    allowed = list(normalize_allowed_hosts(web_allowed_hosts))
    web["bind"] = web_bind.strip()
    web["allowed_hosts"] = allowed
    web.pop("host", None)  # drop legacy key once split fields are written
    web["port"] = web_port
    web["autostart_pipelines"] = autostart_pipelines
    raw["runner"] = runner
    raw["web"] = web
    _parse_config(raw, config_file=expanded)
    tmp = expanded.with_suffix(".toml.tmp")
    tmp.write_text(tomli_w.dumps(raw), encoding="utf-8")
    tmp.replace(expanded)


def save_llm_settings(
    path: Path,
    *,
    llm_provider: str,
    llm_model: str,
    llm_base_url: str,
    llm_max_tokens: int,
    llm_session_token_cap: int,
) -> None:
    """Atomically update the [llm] section in the config TOML."""
    import tomli_w

    expanded = path.expanduser()
    if not expanded.exists():
        raise ConfigError(f"config file not found: {expanded}")
    raw = tomllib.loads(expanded.read_text(encoding="utf-8"))
    if not isinstance(raw, dict):
        raise ConfigError("config root must be a table")
    _validate_keys(raw)
    llm = dict(raw.get("llm", {}))
    llm["provider"] = llm_provider
    llm["model"] = llm_model
    llm["base_url"] = llm_base_url
    llm["max_tokens"] = llm_max_tokens
    llm["session_token_cap"] = llm_session_token_cap
    raw["llm"] = llm
    _parse_config(raw, config_file=expanded)
    tmp = expanded.with_suffix(".toml.tmp")
    tmp.write_text(tomli_w.dumps(raw), encoding="utf-8")
    tmp.replace(expanded)


def write_default_config(path: Path) -> None:
    """Write a commented default config template; refuse to overwrite an existing file."""
    expanded = path.expanduser()
    if expanded.exists():
        raise ConfigError(f"config already exists: {expanded}")
    expanded.parent.mkdir(parents=True, exist_ok=True)
    template = f"""# Ordine application config
# Paths expand ~ at load time.

[paths]
# db = "{_data_dir() / "ordine.sqlite3"}"
# workdir_root = "{_data_dir() / "workdirs"}"

[runner]
stale_after_minutes = 15
reconcile_policy = "retry"  # retry | fail

[log]
level = "INFO"

[web]
# bind is the listen address (restart required to apply).
bind = "127.0.0.1"
# allowed_hosts is the HTTP Host allowlist (never use 0.0.0.0 or :: here).
allowed_hosts = ["127.0.0.1", "localhost"]
port = 8484
autostart_pipelines = false

[llm]
provider = "none"
model = ""
base_url = ""
max_tokens = 1024
session_token_cap = 200000
session_image_cap = 200

[retention]
days = 30
keep_failed = true
on_serve_start = false
"""
    expanded.write_text(template, encoding="utf-8")
