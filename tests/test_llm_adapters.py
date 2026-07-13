"""LLM adapter, retry, budget, and factory tests (httpx.MockTransport only)."""

from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path
from typing import Any

import httpx
import pytest

from conveyor.core.config import AppConfig
from conveyor.llm.adapters import request_json
from conveyor.llm.adapters.anthropic import AnthropicClient
from conveyor.llm.adapters.openai import OpenAIClient, chat_completions_url
from conveyor.llm.client import TokenBudget, _BudgetClient, build_client
from conveyor.llm.errors import (
    LLMAuthError,
    LLMBudgetError,
    LLMRateLimitError,
    LLMResponseError,
    LLMTimeoutError,
)
from conveyor.llm.types import ImagePart, Message, TextPart


def _anthropic_ok() -> dict[str, Any]:
    return {
        "model": "claude-test",
        "content": [{"type": "text", "text": "hello"}],
        "usage": {"input_tokens": 5, "output_tokens": 3},
    }


def _openai_ok() -> dict[str, Any]:
    return {
        "model": "gpt-test",
        "choices": [{"message": {"role": "assistant", "content": "hello"}}],
        "usage": {"prompt_tokens": 4, "completion_tokens": 2},
    }


def _mock_transport(handler) -> httpx.MockTransport:
    return httpx.MockTransport(handler)


def test_anthropic_request_shape() -> None:
    captured: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["url"] = str(request.url)
        captured["headers"] = dict(request.headers)
        captured["body"] = json.loads(request.content)
        return httpx.Response(200, json=_anthropic_ok())

    client = AnthropicClient("claude-sonnet", "secret-key")
    client._client = httpx.Client(transport=_mock_transport(handler))
    response = client.complete(
        [
            Message(role="system", content="sys"),
            Message(
                role="user",
                content=[
                    TextPart("look"),
                    ImagePart("image/png", "aGVsbG8="),
                ],
            ),
        ],
        purpose="test",
        max_tokens=99,
        temperature=0.1,
    )

    assert captured["url"] == "https://api.anthropic.com/v1/messages"
    assert captured["headers"]["x-api-key"] == "secret-key"
    assert captured["headers"]["anthropic-version"] == "2023-06-01"
    body = captured["body"]
    assert body["model"] == "claude-sonnet"
    assert body["system"] == "sys"
    assert body["max_tokens"] == 99
    user_content = body["messages"][0]["content"]
    assert user_content[1]["type"] == "image"
    assert user_content[1]["source"]["media_type"] == "image/png"
    assert response.text == "hello"
    assert response.usage.input_tokens == 5


def test_openai_request_shape_and_base_url_slash() -> None:
    captured: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["url"] = str(request.url)
        captured["headers"] = dict(request.headers)
        captured["body"] = json.loads(request.content)
        return httpx.Response(200, json=_openai_ok())

    base = "http://localhost:11434/v1/"
    client = OpenAIClient("llama3", "local-key", base_url=base, provider="openai_compatible")
    client._client = httpx.Client(transport=_mock_transport(handler))
    client.complete([Message(role="user", content="hi")], purpose="test")

    assert captured["url"] == chat_completions_url("http://localhost:11434/v1/")
    assert captured["headers"]["authorization"] == "Bearer local-key"
    assert captured["body"]["model"] == "llama3"


def test_openai_compatible_without_key_uses_none_bearer() -> None:
    captured: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["auth"] = request.headers.get("authorization")
        return httpx.Response(200, json=_openai_ok())

    client = OpenAIClient(
        "llama3", "", base_url="http://localhost:11434/v1", provider="openai_compatible"
    )
    client._client = httpx.Client(transport=_mock_transport(handler))
    client.complete([Message(role="user", content="hi")], purpose="test")
    assert captured["auth"] == "Bearer none"


def test_malformed_body_raises_response_error() -> None:
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text="not-json")

    client = AnthropicClient("m", "k")
    client._client = httpx.Client(transport=_mock_transport(handler))
    with pytest.raises(LLMResponseError):
        client.complete([Message(role="user", content="x")], purpose="test")


def test_401_maps_to_auth() -> None:
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(401, text="nope")

    client = OpenAIClient("m", "k")
    client._client = httpx.Client(transport=_mock_transport(handler))
    with pytest.raises(LLMAuthError):
        client.complete([Message(role="user", content="x")], purpose="test")


def test_429_retries_three_times_then_rate_limit(monkeypatch: pytest.MonkeyPatch) -> None:
    attempts = {"n": 0}
    sleeps: list[float] = []

    def handler(_request: httpx.Request) -> httpx.Response:
        attempts["n"] += 1
        return httpx.Response(429, headers={"Retry-After": "2"}, text="slow down")

    monkeypatch.setattr("conveyor.llm.adapters.time.sleep", lambda s: sleeps.append(s))
    http = httpx.Client(transport=_mock_transport(handler))
    with pytest.raises(LLMRateLimitError):
        request_json(
            http,
            "POST",
            "https://example.test",
            headers={},
            json_body={},
            timeout=5.0,
            sleep=lambda s: sleeps.append(s),
        )
    assert attempts["n"] == 3
    assert sleeps == [2.0, 2.0]


def test_timeout_maps_to_timeout() -> None:
    def handler(_request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("timed out")

    http = httpx.Client(transport=_mock_transport(handler))
    with pytest.raises(LLMTimeoutError):
        request_json(http, "POST", "https://example.test", headers={}, json_body={}, timeout=0.01)


def test_budget_refuses_before_http() -> None:
    calls = {"n": 0}

    def handler(_request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        payload = _openai_ok()
        payload["usage"] = {"prompt_tokens": 30, "completion_tokens": 20}
        return httpx.Response(200, json=payload)

    budget = TokenBudget(100)
    inner = OpenAIClient("m", "k")
    inner._client = httpx.Client(transport=_mock_transport(handler))
    wrapped = _BudgetClient(inner=inner, budget=budget, default_max_tokens=80)
    wrapped.complete([Message(role="user", content="a")], purpose="first", max_tokens=80)
    with pytest.raises(LLMBudgetError):
        wrapped.complete([Message(role="user", content="b")], purpose="second", max_tokens=80)
    assert calls["n"] == 1


def test_switch_provider_config_only(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("conveyor.llm.client.get_key", lambda _provider: "test-key")
    base = AppConfig(
        db_path=Path("/tmp/db.sqlite"),
        workdir_root=Path("/tmp/work"),
    )
    anthropic = build_client(replace(base, llm_provider="anthropic", llm_model="claude"))
    openai = build_client(replace(base, llm_provider="openai", llm_model="gpt-4"))
    compatible = build_client(
        replace(
            base,
            llm_provider="openai_compatible",
            llm_model="llama3",
            llm_base_url="http://localhost:11434/v1",
        )
    )
    assert anthropic.provider == "anthropic"
    assert anthropic.model == "claude"
    assert openai.provider == "openai"
    assert openai.model == "gpt-4"
    assert compatible.provider == "openai_compatible"
    assert compatible.model == "llama3"
