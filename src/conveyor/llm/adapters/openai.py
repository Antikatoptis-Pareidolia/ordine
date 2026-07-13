"""OpenAI Chat Completions API adapter (raw REST).

Owns request shaping and response parsing for OpenAI and compatible servers. Must never read config.
"""

from __future__ import annotations

import time
from collections.abc import Callable, Sequence
from typing import Any

import httpx

from conveyor.llm.adapters import request_json
from conveyor.llm.errors import LLMResponseError
from conveyor.llm.types import Content, ImagePart, LLMResponse, Message, TextPart, Usage

_OPENAI_DEFAULT_BASE = "https://api.openai.com"


def chat_completions_url(base_url: str) -> str:
    """Join base_url with the chat completions path (handles trailing slash)."""
    return f"{base_url.rstrip('/')}/v1/chat/completions"


def _map_content(content: Content) -> str | list[dict[str, Any]]:
    if isinstance(content, str):
        return content
    blocks: list[dict[str, Any]] = []
    for part in content:
        if isinstance(part, TextPart):
            blocks.append({"type": "text", "text": part.text})
        elif isinstance(part, ImagePart):
            blocks.append(
                {
                    "type": "image_url",
                    "image_url": {
                        "url": f"data:{part.media_type};base64,{part.data_base64}",
                    },
                }
            )
    return blocks


class OpenAIClient:
    """OpenAI-compatible chat completions client."""

    provider: str

    def __init__(
        self,
        model: str,
        api_key: str,
        base_url: str | None = None,
        on_usage: Callable[[Usage], None] | None = None,
        *,
        provider: str = "openai",
    ) -> None:
        self.provider = provider
        self.model = model
        self._api_key = api_key or "none"
        resolved_base = base_url or _OPENAI_DEFAULT_BASE
        self._url = chat_completions_url(resolved_base)
        self._on_usage = on_usage or (lambda _usage: None)
        self._client = httpx.Client()

    def complete(
        self,
        messages: Sequence[Message],
        *,
        purpose: str,
        max_tokens: int | None = None,
        temperature: float = 0.2,
        timeout: float = 60.0,
    ) -> LLMResponse:
        del purpose
        api_messages = [
            {"role": message.role, "content": _map_content(message.content)} for message in messages
        ]
        body: dict[str, Any] = {
            "model": self.model,
            "messages": api_messages,
            "max_tokens": max_tokens or 1024,
            "temperature": temperature,
        }

        started = time.monotonic()
        payload = request_json(
            self._client,
            "POST",
            self._url,
            headers={
                "Authorization": f"Bearer {self._api_key}",
                "content-type": "application/json",
            },
            json_body=body,
            timeout=timeout,
        )
        duration_s = time.monotonic() - started

        choices = payload.get("choices")
        if not isinstance(choices, list) or not choices:
            raise LLMResponseError("missing choices in OpenAI response")
        first = choices[0]
        if not isinstance(first, dict):
            raise LLMResponseError("invalid choice in OpenAI response")
        message = first.get("message")
        if not isinstance(message, dict):
            raise LLMResponseError("missing message in OpenAI response")
        text = str(message.get("content", ""))

        usage_raw = payload.get("usage", {})
        if not isinstance(usage_raw, dict):
            raise LLMResponseError("missing usage in OpenAI response")
        usage = Usage(
            input_tokens=int(usage_raw.get("prompt_tokens", 0)),
            output_tokens=int(usage_raw.get("completion_tokens", 0)),
        )
        self._on_usage(usage)
        model = str(payload.get("model", self.model))
        return LLMResponse(text=text, model=model, usage=usage, duration_s=duration_s)


OpenAICompatibleClient = OpenAIClient
