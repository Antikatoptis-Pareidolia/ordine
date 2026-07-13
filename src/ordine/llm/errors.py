"""LLM-specific exceptions.

Owns LLM error types. Subclasses OrdineError; must never import adapters or cli.
"""

from __future__ import annotations

from ordine.core.errors import OrdineError


class LLMError(OrdineError):
    """Base class for LLM connector errors."""


class LLMNotConfiguredError(LLMError):
    """Raised when no provider/model/key is available for a call."""

    def __init__(self, detail: str = "") -> None:
        super().__init__(
            detail
            or (
                "LLM is not configured. Open the web settings page (/settings) or set "
                "[llm] provider/model in config.toml."
            )
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
