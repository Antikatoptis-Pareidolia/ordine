"""Provider-agnostic LLM connector package."""

from ordine.llm.client import TokenBudget, build_client
from ordine.llm.errors import (
    LLMAuthError,
    LLMBudgetError,
    LLMError,
    LLMNotConfiguredError,
    LLMRateLimitError,
    LLMResponseError,
    LLMTimeoutError,
)
from ordine.llm.types import LLMClient, LLMResponse, Message, Usage

__all__ = [
    "LLMAuthError",
    "LLMBudgetError",
    "LLMClient",
    "LLMError",
    "LLMNotConfiguredError",
    "LLMRateLimitError",
    "LLMResponse",
    "LLMResponseError",
    "LLMTimeoutError",
    "Message",
    "TokenBudget",
    "Usage",
    "build_client",
]
