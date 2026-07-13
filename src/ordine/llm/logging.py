"""JSONL audit logging for LLM calls.

Owns monthly log files under DATA_DIR/llm_log/. Failures to log never fail the call.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from ordine.core.config import DEFAULT_DATA_DIR
from ordine.llm.types import Content, ImagePart, LLMResponse, Message, TextPart

logger = logging.getLogger(__name__)

_RESPONSE_TRUNCATE = 20_000


def _summarize_content(content: Content) -> str | list[dict[str, str]]:
    if isinstance(content, str):
        return content
    rows: list[dict[str, str]] = []
    for part in content:
        if isinstance(part, TextPart):
            rows.append({"text": part.text})
        elif isinstance(part, ImagePart):
            rows.append({"image": (f"<{len(part.data_base64)} base64 chars, {part.media_type}>")})
    return rows


def _serialize_messages(messages: Sequence[Message]) -> list[dict[str, Any]]:
    return [
        {"role": message.role, "content": _summarize_content(message.content)}
        for message in messages
    ]


def _truncate_response(text: str) -> tuple[str, bool]:
    if len(text) <= _RESPONSE_TRUNCATE:
        return text, False
    return text[:_RESPONSE_TRUNCATE], True


def log_call(
    *,
    data_dir: Path,
    provider: str,
    model: str,
    purpose: str,
    messages: Sequence[Message],
    response: LLMResponse,
) -> None:
    """Append one JSONL audit record for a completed LLM call."""
    try:
        response_text, truncated = _truncate_response(response.text)
        record: dict[str, Any] = {
            "ts": datetime.now(tz=UTC).isoformat(),
            "provider": provider,
            "model": model,
            "purpose": purpose,
            "duration_s": response.duration_s,
            "usage": {
                "input_tokens": response.usage.input_tokens,
                "output_tokens": response.usage.output_tokens,
            },
            "messages": _serialize_messages(messages),
            "response_text": response_text,
        }
        if truncated:
            record["truncated"] = True

        log_dir = data_dir / "llm_log"
        log_dir.mkdir(parents=True, exist_ok=True)
        month = datetime.now(tz=UTC).strftime("%Y-%m")
        path = log_dir / f"{month}.jsonl"
        with path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(record, ensure_ascii=False) + "\n")
            handle.flush()
    except OSError as exc:
        logger.warning("llm audit log write failed: %s", exc)


def default_data_dir() -> Path:
    """Return the default XDG data directory for Ordine."""
    return DEFAULT_DATA_DIR
