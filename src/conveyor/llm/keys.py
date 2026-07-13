"""API key resolution and keyring storage for LLM providers.

Owns key precedence (keyring > env > .env). Must never import adapters or cli.
"""

from __future__ import annotations

import os
from pathlib import Path

import keyring
from keyring.errors import KeyringError

from conveyor.core.config import DEFAULT_CONFIG_DIR
from conveyor.llm.errors import LLMError

SERVICE = "conveyor"
ENV_NAMES = {
    "anthropic": "ANTHROPIC_API_KEY",
    "openai": "OPENAI_API_KEY",
    "openai_compatible": "CONVEYOR_LLM_API_KEY",
}


def _read_dotenv(path: Path) -> dict[str, str]:
    if not path.exists():
        return {}
    values: dict[str, str] = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        if "=" not in stripped:
            continue
        key, value = stripped.split("=", 1)
        values[key.strip()] = value.strip().strip('"').strip("'")
    return values


def _keyring_error_message(provider: str) -> str:
    env_name = ENV_NAMES.get(provider, "CONVEYOR_LLM_API_KEY")
    return f"keyring unavailable; use env var {env_name}"


def _resolve_key(provider: str) -> tuple[str | None, str | None]:
    """Resolve an API key and its source label (keyring, env var, or .env file)."""
    try:
        stored = keyring.get_password(SERVICE, provider)
        if stored:
            return stored, "keyring"
    except KeyringError as exc:
        raise LLMError(_keyring_error_message(provider)) from exc

    env_name = ENV_NAMES.get(provider)
    if env_name:
        env_val = os.environ.get(env_name)
        if env_val:
            return env_val, "env var"

    dotenv = _read_dotenv(DEFAULT_CONFIG_DIR / ".env")
    if env_name and env_name in dotenv:
        return dotenv[env_name], ".env file"
    return None, None


def get_key(provider: str) -> str | None:
    """Resolve an API key: keyring, then env var, then CONFIG_DIR/.env."""
    key, _source = _resolve_key(provider)
    return key


def key_presence_label(provider: str) -> str:
    """Return a settings-safe presence label, e.g. 'yes (env var)' or 'no'."""
    if provider in ("", "none"):
        return "no"
    key, source = _resolve_key(provider)
    if key is None:
        return "no"
    return f"yes ({source})"


def set_key(provider: str, key: str) -> None:
    """Store a provider key in the system keyring."""
    try:
        keyring.set_password(SERVICE, provider, key)
    except KeyringError as exc:
        raise LLMError(_keyring_error_message(provider)) from exc


def clear_key(provider: str) -> None:
    """Remove a provider key from the system keyring."""
    try:
        keyring.delete_password(SERVICE, provider)
    except KeyringError as exc:
        raise LLMError(_keyring_error_message(provider)) from exc
