"""Anthropic Messages API adapter (raw REST).

Owns request shaping and response parsing for Anthropic. Must never read config or keys.
"""

from __future__ import annotations

import time
from collections.abc import Callable, Sequence
from typing import Any

import httpx

from ordine.llm.adapters import request_json
from ordine.llm.errors import LLMResponseError
from ordine.llm.types import Content, ImagePart, LLMResponse, Message, TextPart, Usage

_ANTHROPIC_URL = "https://api.anthropic.com/v1/messages"
_ANTHROPIC_VERSION = "2023-06-01"


def _map_content(content: Content) -> list[dict[str, Any]]:
    if isinstance(content, str):
        return [{"type": "text", "text": content}]
    blocks: list[dict[str, Any]] = []
    for part in content:
        if isinstance(part, TextPart):
            blocks.append({"type": "text", "text": part.text})
        elif isinstance(part, ImagePart):
            blocks.append(
                {
                    "type": "image",
                    "source": {
                        "type": "base64",
                        "media_type": part.media_type,
                        "data": part.data_base64,
                    },
                }
            )
    return blocks


def _split_messages(messages: Sequence[Message]) -> tuple[str | None, list[dict[str, Any]]]:
    system_parts: list[str] = []
    api_messages: list[dict[str, Any]] = []
    for message in messages:
        if message.role == "system":
            if isinstance(message.content, str):
                system_parts.append(message.content)
            else:
                for part in message.content:
                    if isinstance(part, TextPart):
                        system_parts.append(part.text)
            continue
        api_messages.append({"role": message.role, "content": _map_content(message.content)})
    system = "\n\n".join(system_parts) if system_parts else None
    return system, api_messages


class AnthropicClient:
    """Anthropic Messages API client."""

    provider = "anthropic"

    def __init__(
        self,
        model: str,
        api_key: str,
        base_url: str | None = None,
        on_usage: Callable[[Usage], None] | None = None,
    ) -> None:
        self.model = model
        self._api_key = api_key
        self._on_usage = on_usage or (lambda _usage: None)
        self._url = f"{base_url.rstrip('/')}/v1/messages" if base_url else _ANTHROPIC_URL
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
        del purpose  # logging decorator owns purpose
        system, api_messages = _split_messages(messages)
        body: dict[str, Any] = {
            "model": self.model,
            "messages": api_messages,
            "max_tokens": max_tokens or 1024,
            "temperature": temperature,
        }
        if system is not None:
            body["system"] = system

        started = time.monotonic()
        payload = request_json(
            self._client,
            "POST",
            self._url,
            headers={
                "x-api-key": self._api_key,
                "anthropic-version": _ANTHROPIC_VERSION,
                "content-type": "application/json",
            },
            json_body=body,
            timeout=timeout,
        )
        duration_s = time.monotonic() - started

        content = payload.get("content")
        if not isinstance(content, list):
            raise LLMResponseError("missing content array in Anthropic response")
        text_parts: list[str] = []
        for block in content:
            if isinstance(block, dict) and block.get("type") == "text":
                text_parts.append(str(block.get("text", "")))
        text = "".join(text_parts)

        usage_raw = payload.get("usage", {})
        if not isinstance(usage_raw, dict):
            raise LLMResponseError("missing usage in Anthropic response")
        usage = Usage(
            input_tokens=int(usage_raw.get("input_tokens", 0)),
            output_tokens=int(usage_raw.get("output_tokens", 0)),
        )
        self._on_usage(usage)
        model = str(payload.get("model", self.model))
        return LLMResponse(text=text, model=model, usage=usage, duration_s=duration_s)
