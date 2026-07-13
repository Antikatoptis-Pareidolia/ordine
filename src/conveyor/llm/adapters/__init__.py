"""Shared HTTP retry helper for LLM adapters.

Owns provider-agnostic REST retry and error mapping. Must never read config or keys.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from typing import Any

import httpx

from conveyor.llm.errors import LLMAuthError, LLMRateLimitError, LLMResponseError, LLMTimeoutError

_RETRIABLE = frozenset({429, 500, 502, 503, 504})
_BACKOFF_S = (1.0, 4.0)


def _body_snippet(response: httpx.Response, *, limit: int = 300) -> str:
    text = response.text
    if len(text) <= limit:
        return text
    return text[:limit]


def _retry_after_seconds(response: httpx.Response, default: float) -> float:
    header = response.headers.get("Retry-After")
    if header is None:
        return default
    try:
        return float(header)
    except ValueError:
        return default


def request_json(
    client: httpx.Client,
    method: str,
    url: str,
    *,
    headers: dict[str, str],
    json_body: dict[str, Any],
    timeout: float,
    sleep: Callable[[float], None] = time.sleep,
) -> dict[str, Any]:
    """Perform an HTTP request with retries and return parsed JSON.

    Args:
        client: httpx client (tests inject MockTransport here).
        method: HTTP method.
        url: Request URL.
        headers: Request headers.
        json_body: JSON request body.
        timeout: Per-attempt timeout in seconds.
        sleep: Injectable sleep for backoff tests.

    Returns:
        Parsed JSON object from the response body.

    Raises:
        LLMAuthError: On HTTP 401 or 403.
        LLMRateLimitError: On persistent 429 after retries.
        LLMTimeoutError: On httpx timeout.
        LLMResponseError: On other HTTP errors or malformed JSON.
    """
    last_response: httpx.Response | None = None
    attempts = 1 + len(_BACKOFF_S)
    for attempt in range(attempts):
        try:
            response = client.request(
                method,
                url,
                headers=headers,
                json=json_body,
                timeout=timeout,
            )
        except httpx.TimeoutException as exc:
            raise LLMTimeoutError(str(exc)) from exc

        last_response = response
        if response.status_code in (401, 403):
            raise LLMAuthError(f"authentication failed: HTTP {response.status_code}")

        if response.status_code in _RETRIABLE and attempt < len(_BACKOFF_S):
            delay = _retry_after_seconds(response, _BACKOFF_S[attempt])
            sleep(delay)
            continue

        if response.status_code == 429:
            raise LLMRateLimitError("rate limited after retries")

        if response.status_code >= 400:
            raise LLMResponseError(f"HTTP {response.status_code}: {_body_snippet(response)}")

        try:
            payload = response.json()
        except ValueError as exc:
            raise LLMResponseError(f"invalid JSON: {_body_snippet(response)}") from exc
        if not isinstance(payload, dict):
            raise LLMResponseError(f"expected JSON object: {_body_snippet(response)}")
        return payload

    if last_response is not None and last_response.status_code in _RETRIABLE:
        if last_response.status_code == 429:
            raise LLMRateLimitError("rate limited after retries")
        raise LLMResponseError(f"HTTP {last_response.status_code}: {_body_snippet(last_response)}")
    raise LLMResponseError("request failed without a response")
