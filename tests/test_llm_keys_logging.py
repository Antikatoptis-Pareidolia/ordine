"""LLM key resolution, JSONL logging, and settings privacy tests."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from keyring.errors import KeyringError

from conveyor.core.config import AppConfig, load_config
from conveyor.llm import logging as llm_logging
from conveyor.llm.client import _LoggingClient, build_client
from conveyor.llm.keys import ENV_NAMES, SERVICE, clear_key, get_key, set_key
from conveyor.llm.types import ImagePart, LLMResponse, Message, TextPart, Usage
from conveyor.web.app import create_app

POST_HEADERS = {"HX-Request": "true", "Origin": "http://127.0.0.1:8484"}


def _write_config(tmp_path: Path) -> Path:
    config_file = tmp_path / "config.toml"
    config_file.write_text(
        f"""[paths]
db = "{tmp_path / "conveyor.sqlite3"}"
workdir_root = "{tmp_path / "workdirs"}"

[web]
host = "127.0.0.1"
port = 8484

[llm]
provider = "anthropic"
model = "claude-test"
max_tokens = 1024
session_token_cap = 200000
""",
        encoding="utf-8",
    )
    return config_file


def test_key_precedence_keyring_over_env_over_dotenv(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    store: dict[tuple[str, str], str] = {}

    def fake_get(service: str, provider: str) -> str | None:
        return store.get((service, provider))

    def fake_set(service: str, provider: str, key: str) -> None:
        store[(service, provider)] = key

    monkeypatch.setattr("conveyor.llm.keys.keyring.get_password", fake_get)
    monkeypatch.setattr("conveyor.llm.keys.keyring.set_password", fake_set)
    monkeypatch.setattr("conveyor.llm.keys.DEFAULT_CONFIG_DIR", tmp_path)
    dotenv = tmp_path / ".env"
    dotenv.write_text(f"{ENV_NAMES['anthropic']}=from-dotenv\n", encoding="utf-8")
    monkeypatch.setenv(ENV_NAMES["anthropic"], "from-env")

    assert get_key("anthropic") == "from-env"
    set_key("anthropic", "from-keyring")
    assert get_key("anthropic") == "from-keyring"


def test_openai_compatible_without_key_is_legal(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("conveyor.llm.client.get_key", lambda _provider: None)
    client = build_client(
        AppConfig(
            db_path=Path("/tmp/db.sqlite"),
            workdir_root=Path("/tmp/work"),
            llm_provider="openai_compatible",
            llm_model="llama3",
            llm_base_url="http://localhost:11434/v1",
        )
    )
    assert client.provider == "openai_compatible"


def test_keyring_error_includes_env_name(monkeypatch: pytest.MonkeyPatch) -> None:
    def boom(*_args: object, **_kwargs: object) -> None:
        raise KeyringError("no backend")

    monkeypatch.setattr("conveyor.llm.keys.keyring.get_password", boom)
    with pytest.raises(Exception, match=ENV_NAMES["openai"]):
        get_key("openai")


def test_set_and_clear_key(monkeypatch: pytest.MonkeyPatch) -> None:
    store: dict[tuple[str, str], str] = {}

    monkeypatch.setattr(
        "conveyor.llm.keys.keyring.get_password",
        lambda service, provider: store.get((service, provider)),
    )
    monkeypatch.setattr(
        "conveyor.llm.keys.keyring.set_password",
        lambda service, provider, key: store.__setitem__((service, provider), key),
    )
    monkeypatch.setattr(
        "conveyor.llm.keys.keyring.delete_password",
        lambda service, provider: store.pop((service, provider), None),
    )
    set_key("anthropic", "sekrit")
    assert get_key("anthropic") == "sekrit"
    clear_key("anthropic")
    assert get_key("anthropic") is None


def test_jsonl_log_one_line_no_key_truncates(tmp_path: Path) -> None:
    data_dir = tmp_path / "data"
    long_text = "x" * 25_000
    response = LLMResponse(
        text=long_text,
        model="m",
        usage=Usage(1, 2),
        duration_s=0.5,
    )
    messages = [
        Message(
            role="user",
            content=[TextPart("hi"), ImagePart("image/png", "secretbase64keymaterial")],
        )
    ]

    class _Stub:
        provider = "openai"
        model = "m"

        def complete(self, *_args: object, **_kwargs: object) -> LLMResponse:
            return response

    logged = _LoggingClient(inner=_Stub(), data_dir=data_dir)
    logged.complete(messages, purpose="audit_test")

    log_files = list((data_dir / "llm_log").glob("*.jsonl"))
    assert len(log_files) == 1
    line = log_files[0].read_text(encoding="utf-8").strip()
    assert "super-secret-api-key-value" not in line
    record = json.loads(line)
    assert record["purpose"] == "audit_test"
    assert record["truncated"] is True
    assert len(record["response_text"]) == 20_000
    image_logged = record["messages"][0]["content"][1]["image"]
    assert "base64 chars" in image_logged
    assert "secretbase64keymaterial" not in line


def test_logging_failure_does_not_fail_call(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    def boom(*_args: object, **_kwargs: object) -> None:
        raise OSError("disk full")

    monkeypatch.setattr(llm_logging, "log_call", boom)
    response = LLMResponse("ok", "m", Usage(1, 1), 0.1)

    class _Stub:
        provider = "openai"
        model = "m"

        def complete(self, *_args: object, **_kwargs: object) -> LLMResponse:
            return response

    logged = _LoggingClient(inner=_Stub(), data_dir=tmp_path)
    result = logged.complete([Message(role="user", content="hi")], purpose="x")
    assert result.text == "ok"


def test_settings_key_presence_and_privacy(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    secret = "anthropic-secret-key-12345"
    store: dict[tuple[str, str], str] = {}

    def fake_get(service: str, provider: str) -> str | None:
        return store.get((service, provider))

    monkeypatch.setattr("conveyor.llm.keys.keyring.get_password", fake_get)
    monkeypatch.setattr(
        "conveyor.llm.keys.keyring.set_password",
        lambda service, provider, key: store.__setitem__((service, provider), key),
    )
    monkeypatch.setattr(
        "conveyor.llm.keys.keyring.delete_password",
        lambda service, provider: store.pop((service, provider), None),
    )
    monkeypatch.setattr("conveyor.llm.keys.DEFAULT_CONFIG_DIR", tmp_path)
    config_path = _write_config(tmp_path)
    config = load_config(config_path)
    client = TestClient(create_app(config))

    def assert_absent_and_label(html: str, label: str) -> None:
        assert secret not in html
        assert f"key present: {label}" in html

    # keyring
    response = client.post(
        "/settings/llm-key",
        data={"llm_provider": "anthropic", "api_key": secret, "action": "set"},
        headers=POST_HEADERS,
    )
    assert response.status_code == 200
    assert_absent_and_label(response.text, "yes (keyring)")
    assert_absent_and_label(client.get("/settings").text, "yes (keyring)")

    # env var (keyring cleared so env wins when present)
    store.clear()
    monkeypatch.setenv(ENV_NAMES["anthropic"], secret)
    assert_absent_and_label(client.get("/settings").text, "yes (env var)")

    # .env file
    monkeypatch.delenv(ENV_NAMES["anthropic"], raising=False)
    (tmp_path / ".env").write_text(f"{ENV_NAMES['anthropic']}={secret}\n", encoding="utf-8")
    assert_absent_and_label(client.get("/settings").text, "yes (.env file)")

    # clear keyring only — .env still visible
    store[(SERVICE, "anthropic")] = secret
    client.post(
        "/settings/llm-key",
        data={"llm_provider": "anthropic", "api_key": "", "action": "clear"},
        headers=POST_HEADERS,
    )
    assert_absent_and_label(client.get("/settings").text, "yes (.env file)")
