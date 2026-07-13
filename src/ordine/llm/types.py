"""LLM message and response types.

Owns the provider-agnostic LLM contract. May import core; must never import cli or web.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from typing import Literal, Protocol


@dataclass(frozen=True)
class TextPart:
    text: str


@dataclass(frozen=True)
class ImagePart:
    media_type: str
    data_base64: str


Content = str | Sequence[TextPart | ImagePart]


@dataclass(frozen=True)
class Message:
    role: Literal["system", "user", "assistant"]
    content: Content


@dataclass(frozen=True)
class Usage:
    input_tokens: int
    output_tokens: int


@dataclass(frozen=True)
class LLMResponse:
    text: str
    model: str
    usage: Usage
    duration_s: float


class LLMClient(Protocol):
    @property
    def provider(self) -> str: ...

    @property
    def model(self) -> str: ...

    def complete(
        self,
        messages: Sequence[Message],
        *,
        purpose: str,
        max_tokens: int | None = None,
        temperature: float = 0.2,
        timeout: float = 60.0,
    ) -> LLMResponse: ...
