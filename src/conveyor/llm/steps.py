"""LLM image-generation steps and provider seam.

Owns llm.generate_image and image providers. Must never import web or cli.
Publishing to handoff folders is the caller's job — compose with file.move.
"""

from __future__ import annotations

import base64
import hashlib
import io
import json
import logging
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import ClassVar, Literal

import httpx
from PIL import Image, ImageDraw
from pydantic import BaseModel, ConfigDict, Field

from conveyor.core.config import DEFAULT_DATA_DIR, load_config
from conveyor.core.errors import ManifestError
from conveyor.core.manifest import load_manifest
from conveyor.core.steps import StepContext, StepResult
from conveyor.llm.adapters.openai import _OPENAI_DEFAULT_BASE
from conveyor.llm.keys import get_key

logger = logging.getLogger(__name__)

ImageProviderName = Literal["openai", "mock"]


class ImageBudgetError(Exception):
    """Raised when the process image budget would be exceeded."""

    def __init__(self, *, used: int, cap: int) -> None:
        self.used = used
        self.cap = cap
        super().__init__(f"image budget exceeded: {used} used of {cap} cap")


class ImageBudget:
    """Thread-safe cumulative image cap for a process or test scope."""

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

    def reserve_one(self) -> None:
        with self._lock:
            if self._used >= self._cap:
                raise ImageBudgetError(used=self._used, cap=self._cap)
            self._used += 1


_process_image_budget: ImageBudget | None = None


def get_image_budget(*, cap: int | None = None) -> ImageBudget:
    """Return the process-wide image budget, creating it on first use."""
    global _process_image_budget
    if _process_image_budget is None:
        resolved_cap = cap if cap is not None else load_config().llm_session_image_cap
        _process_image_budget = ImageBudget(resolved_cap)
    return _process_image_budget


def reset_image_budget_for_tests(cap: int = 200) -> ImageBudget:
    """Replace the process image budget (tests only)."""
    global _process_image_budget
    _process_image_budget = ImageBudget(cap)
    return _process_image_budget


class GenerateImageParams(BaseModel):
    model_config = ConfigDict(extra="forbid")

    manifest: str
    provider: ImageProviderName = "openai"
    model: str = "gpt-image-1"
    size: str = "1024x1024"
    background: Literal["default", "white", "transparent"] = "default"
    filename_template: str = "img_{ordinal:04d}"
    timeout_seconds: float = Field(default=120.0, gt=0)


@dataclass(frozen=True)
class ImageGenerationOutcome:
    png_bytes: bytes
    duration_s: float


@dataclass(frozen=True)
class ImageGenerationFailure:
    message: str
    flag_kind: str | None = None


def _parse_size(size: str) -> tuple[int, int]:
    parts = size.lower().split("x")
    if len(parts) != 2:
        raise ValueError(f"invalid size: {size!r}")
    return int(parts[0]), int(parts[1])


def render_mock_image(*, size: str, prompt: str, ordinal: int) -> bytes:
    """Deterministic PNG keyed by ordinal, prompt, and size."""
    width, height = _parse_size(size)
    digest = hashlib.sha256(prompt.encode()).digest()
    accent = (digest[0], digest[1], digest[2])
    image = Image.new("RGB", (width, height), "white")
    draw = ImageDraw.Draw(image)
    band_height = min(40, height)
    draw.rectangle((0, 0, width, band_height), fill=accent)
    draw.text((16, 16), f"ordinal={ordinal}", fill="black")
    draw.text((16, 48), "mock provider", fill="black")
    prompt_line = prompt if len(prompt) <= 60 else f"{prompt[:57]}..."
    draw.text((16, 80), prompt_line, fill="black")
    buffer = io.BytesIO()
    image.save(buffer, format="PNG", optimize=True)
    return buffer.getvalue()


def _background_fields(background: str) -> dict[str, str]:
    if background == "default":
        return {}
    if background == "transparent":
        return {"background": "transparent"}
    return {"background": "white"}


def _openai_images_url(base_url: str) -> str:
    return f"{base_url.rstrip('/')}/v1/images/generations"


def generate_openai_image(
    *,
    model: str,
    prompt: str,
    size: str,
    background: str,
    timeout_seconds: float,
    client: httpx.Client | None = None,
    base_url: str = _OPENAI_DEFAULT_BASE,
    **_kwargs: object,
) -> ImageGenerationOutcome | ImageGenerationFailure:
    """Call OpenAI Images API and return PNG bytes or a structured failure."""
    api_key = get_key("openai")
    if not api_key:
        return ImageGenerationFailure(message="no API key for provider openai")

    body: dict[str, object] = {
        "model": model,
        "prompt": prompt,
        "size": size,
        "n": 1,
        "response_format": "b64_json",
        **_background_fields(background),
    }
    owned = client is None
    http = client or httpx.Client()
    started = time.monotonic()
    try:
        response = http.post(
            _openai_images_url(base_url),
            headers={
                "Authorization": f"Bearer {api_key}",
                "content-type": "application/json",
            },
            json=body,
            timeout=timeout_seconds,
        )
    except httpx.TimeoutException as exc:
        return ImageGenerationFailure(message=f"request timed out: {exc}")
    except httpx.HTTPError as exc:
        return ImageGenerationFailure(message=str(exc))
    finally:
        if owned:
            http.close()

    duration_s = time.monotonic() - started
    if response.status_code in (401, 403):
        return ImageGenerationFailure(message=f"authentication failed: HTTP {response.status_code}")
    if response.status_code == 400:
        try:
            payload = response.json()
        except ValueError:
            payload = {}
        error = payload.get("error") if isinstance(payload, dict) else None
        reason = ""
        if isinstance(error, dict):
            reason = str(error.get("message") or error.get("code") or "")
        snippet = reason or response.text[:300]
        lowered = snippet.lower()
        if "policy" in lowered or "safety" in lowered or "content" in lowered:
            return ImageGenerationFailure(
                message=snippet or "generation refused by provider",
                flag_kind="generation_refused",
            )
        return ImageGenerationFailure(message=snippet or "HTTP 400 from image provider")
    if response.status_code >= 400:
        return ImageGenerationFailure(message=f"HTTP {response.status_code}: {response.text[:300]}")

    try:
        payload = response.json()
    except ValueError as exc:
        return ImageGenerationFailure(message=f"invalid JSON response: {exc}")

    data = payload.get("data") if isinstance(payload, dict) else None
    if not isinstance(data, list) or not data:
        return ImageGenerationFailure(message="missing data in image response")
    first = data[0]
    if not isinstance(first, dict):
        return ImageGenerationFailure(message="invalid image payload")
    encoded = first.get("b64_json")
    if not isinstance(encoded, str):
        return ImageGenerationFailure(message="missing b64_json in image response")
    try:
        png_bytes = base64.b64decode(encoded)
    except (ValueError, TypeError) as exc:
        return ImageGenerationFailure(message=f"invalid base64 image data: {exc}")
    return ImageGenerationOutcome(png_bytes=png_bytes, duration_s=duration_s)


ImageProvider = Callable[..., ImageGenerationOutcome | ImageGenerationFailure]

IMAGE_PROVIDERS: dict[ImageProviderName, ImageProvider] = {
    "mock": lambda **kwargs: ImageGenerationOutcome(
        png_bytes=render_mock_image(
            size=str(kwargs["size"]),
            prompt=str(kwargs["prompt"]),
            ordinal=int(kwargs["ordinal"]),
        ),
        duration_s=0.0,
    ),
    "openai": generate_openai_image,
}


def _log_image_call(
    *,
    data_dir: Path,
    provider: str,
    model: str,
    prompt: str,
    ordinal: int,
    image_bytes: bytes,
    duration_s: float,
) -> None:
    record = {
        "ts": datetime.now(tz=UTC).isoformat(),
        "provider": provider,
        "model": model,
        "purpose": "generate_image",
        "ordinal": ordinal,
        "prompt": prompt,
        "image_bytes": len(image_bytes),
        "duration_s": duration_s,
    }
    try:
        log_dir = data_dir / "llm_log"
        log_dir.mkdir(parents=True, exist_ok=True)
        month = datetime.now(tz=UTC).strftime("%Y-%m")
        path = log_dir / f"{month}.jsonl"
        with path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(record, ensure_ascii=False) + "\n")
            handle.flush()
    except OSError as exc:
        logger.warning("image audit log write failed: %s", exc)


class GenerateImageStep:
    """Generate one PNG from the task's manifest row via a pluggable image provider."""

    id = "llm.generate_image"
    engines = frozenset({"headless"})
    Params = GenerateImageParams
    OUTPUT_DIR_PARAMS: ClassVar[frozenset[str]] = frozenset()

    def run(self, ctx: StepContext, params: BaseModel) -> StepResult:
        assert isinstance(params, GenerateImageParams)
        if ctx.ordinal is None:
            return StepResult(
                status="fail",
                message=(
                    "task has no ordinal; configure ordinal_regex / arrival_order_ordinals "
                    "or a manifest trigger"
                ),
            )

        try:
            rows = load_manifest(Path(params.manifest).expanduser())
        except ManifestError as exc:
            return StepResult(status="fail", message=str(exc))

        if ctx.ordinal > len(rows):
            message = f"manifest has {len(rows)} rows, task ordinal is {ctx.ordinal}"
            return StepResult(status="fail", flag_kind="manifest_exhausted", message=message)

        row = rows[ctx.ordinal - 1]
        if not row.prompt:
            return StepResult(status="fail", message=f"row {ctx.ordinal} has no prompt")

        budget = get_image_budget()
        try:
            budget.reserve_one()
        except ImageBudgetError as exc:
            return StepResult(status="fail", message=str(exc))

        provider_fn = IMAGE_PROVIDERS.get(params.provider)
        if provider_fn is None:
            return StepResult(status="fail", message=f"unknown image provider {params.provider!r}")

        outcome = provider_fn(
            model=params.model,
            prompt=row.prompt,
            size=params.size,
            background=params.background,
            timeout_seconds=params.timeout_seconds,
            ordinal=ctx.ordinal,
        )
        if isinstance(outcome, ImageGenerationFailure):
            return StepResult(
                status="fail",
                message=outcome.message,
                flag_kind=outcome.flag_kind,
            )

        name_stem = Path(row.name).stem
        filename = f"{params.filename_template.format(ordinal=ctx.ordinal, name=name_stem)}.png"
        output_path = ctx.step_dir / filename
        try:
            output_path.write_bytes(outcome.png_bytes)
        except OSError as exc:
            return StepResult(status="fail", message=str(exc))

        config = load_config()
        data_dir = config.db_path.parent if config.db_path is not None else DEFAULT_DATA_DIR
        _log_image_call(
            data_dir=data_dir.expanduser(),
            provider=params.provider,
            model=params.model,
            prompt=row.prompt,
            ordinal=ctx.ordinal,
            image_bytes=outcome.png_bytes,
            duration_s=outcome.duration_s,
        )
        return StepResult(status="ok", output_path=output_path)
