"""LLM client factory, token budget, and decorators.

Owns build_client and process-wide token budgeting. May read config and keys; not adapters' config.
"""

from __future__ import annotations

import logging
import threading
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import cast

from ordine.core.config import AppConfig
from ordine.llm import logging as llm_logging
from ordine.llm.adapters.anthropic import AnthropicClient
from ordine.llm.adapters.openai import OpenAIClient
from ordine.llm.errors import LLMBudgetError, LLMNotConfiguredError
from ordine.llm.keys import get_key
from ordine.llm.types import LLMClient, LLMResponse, Message, Usage

logger = logging.getLogger(__name__)


class TokenBudget:
    """Thread-safe cumulative token cap for a process or test scope."""

    def __init__(self, cap: int) -> None:
        self._cap = cap
        self._used = 0
        self._lock = threading.Lock()

    @property
    def used(self) -> int:
        with self._lock:
            return self._used

    @property
    def cap(self) -> int:
        return self._cap

    def reset(self) -> None:
        with self._lock:
            self._used = 0

    def check_reservation(self, reservation: int) -> None:
        with self._lock:
            if self._used + reservation > self._cap:
                raise LLMBudgetError(used=self._used, cap=self._cap, reservation=reservation)

    def charge(self, usage: Usage) -> None:
        with self._lock:
            self._used += usage.input_tokens + usage.output_tokens


@dataclass
class _BudgetClient:
    """Decorator that enforces token budget before delegating."""

    inner: LLMClient
    budget: TokenBudget
    default_max_tokens: int

    @property
    def provider(self) -> str:
        return self.inner.provider

    @property
    def model(self) -> str:
        return self.inner.model

    def complete(
        self,
        messages: Sequence[Message],
        *,
        purpose: str,
        max_tokens: int | None = None,
        temperature: float = 0.2,
        timeout: float = 60.0,
    ) -> LLMResponse:
        reservation = max_tokens if max_tokens is not None else self.default_max_tokens
        self.budget.check_reservation(reservation)
        response = self.inner.complete(
            messages,
            purpose=purpose,
            max_tokens=max_tokens,
            temperature=temperature,
            timeout=timeout,
        )
        self.budget.charge(response.usage)
        return response


@dataclass
class _LoggingClient:
    """Decorator that writes JSONL audit records after successful calls."""

    inner: LLMClient
    data_dir: Path

    @property
    def provider(self) -> str:
        return self.inner.provider

    @property
    def model(self) -> str:
        return self.inner.model

    def complete(
        self,
        messages: Sequence[Message],
        *,
        purpose: str,
        max_tokens: int | None = None,
        temperature: float = 0.2,
        timeout: float = 60.0,
    ) -> LLMResponse:
        response = self.inner.complete(
            messages,
            purpose=purpose,
            max_tokens=max_tokens,
            temperature=temperature,
            timeout=timeout,
        )
        try:
            llm_logging.log_call(
                data_dir=self.data_dir,
                provider=self.provider,
                model=self.model,
                purpose=purpose,
                messages=messages,
                response=response,
            )
        except OSError as exc:
            logger.warning("llm audit log write failed: %s", exc)
        return response


def _data_dir_for(config: AppConfig) -> Path:
    return config.db_path.parent


def build_client(config: AppConfig, *, budget: TokenBudget | None = None) -> LLMClient:
    """Build a configured LLM client with logging and budget decorators."""
    provider = (config.llm_provider or "").strip().lower()
    if provider in ("", "none"):
        raise LLMNotConfiguredError(
            "LLM provider is not configured. Set [llm].provider in config, or use the Settings page."
        )

    model = config.llm_model.strip()
    if not model:
        raise LLMNotConfiguredError(
            "LLM model is not configured. Set [llm].model in config, or use the Settings page."
        )

    api_key = get_key(provider)
    if provider in ("anthropic", "openai") and not api_key:
        raise LLMNotConfiguredError(
            "LLM API key is missing. Set it in the Settings page, or via env var "
            "ANTHROPIC_API_KEY / OPENAI_API_KEY (provider-specific) or ORDINE_LLM_API_KEY "
            "(openai_compatible), or in ~/.config/ordine/.env."
        )

    bearer = api_key or "none"
    token_budget = budget or TokenBudget(config.llm_session_token_cap)
    data_dir = _data_dir_for(config)

    if provider == "anthropic":
        adapter: LLMClient = AnthropicClient(model, bearer)
    elif provider == "openai":
        adapter = OpenAIClient(model, bearer, provider="openai")
    elif provider == "openai_compatible":
        base_url = config.llm_base_url.strip()
        if not base_url:
            raise LLMNotConfiguredError(
                "openai_compatible requires [llm].base_url. Set it in config, or use the Settings page."
            )
        adapter = OpenAIClient(
            model,
            bearer,
            base_url=base_url,
            provider="openai_compatible",
        )
    else:
        raise LLMNotConfiguredError(
            f"unknown LLM provider {provider!r}. Set [llm].provider to 'anthropic', 'openai', "
            "or 'openai_compatible' (or configure via the Settings page)."
        )

    logged = _LoggingClient(inner=adapter, data_dir=data_dir)
    return _BudgetClient(
        inner=logged,
        budget=token_budget,
        default_max_tokens=config.llm_max_tokens,
    )


def as_llm_client(client: object) -> LLMClient:
    """Narrow a built client to the LLMClient protocol."""
    return cast(LLMClient, client)
