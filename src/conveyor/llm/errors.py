"""LLM-specific exceptions.

Owns LLM error types. Subclasses ConveyorError; must never import adapters or cli.
"""

from __future__ import annotations

from conveyor.core.errors import ConveyorError


class LLMError(ConveyorError):
    """Base class for LLM connector errors."""


class LLMNotConfiguredError(LLMError):
    """Raised when no provider/model/key is available for a call."""

    def __init__(self, detail: str = "") -> None:
        extra = f" {detail}" if detail else ""
        super().__init__(
            "LLM is not configured. Open the web settings page (/settings) or set "
            "[llm] provider/model in config.toml. API keys: ANTHROPIC_API_KEY, "
            f"OPENAI_API_KEY, or CONVEYOR_LLM_API_KEY (openai_compatible).{extra}"
        )


class LLMAuthError(LLMError):
    """Raised on HTTP 401/403 from the provider."""


class LLMRateLimitError(LLMError):
    """Raised when rate limits persist after retries."""


class LLMTimeoutError(LLMError):
    """Raised when the HTTP request times out."""


class LLMResponseError(LLMError):
    """Raised when the provider returns an unparseable or malformed body."""


class LLMBudgetError(LLMError):
    """Raised when a call would exceed the process token budget."""

    def __init__(self, *, used: int, cap: int, reservation: int) -> None:
        self.used = used
        self.cap = cap
        self.reservation = reservation
        super().__init__(
            f"token budget exceeded: {used} used of {cap} cap; "
            f"call reserves up to {reservation} output tokens"
        )
